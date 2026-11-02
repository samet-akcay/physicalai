# Tiered Robot Runtime: System 0 / 1 / 2

**Status:** Proposal for design review.

Defines how PhysicalAI runs a robot across System 0 (control), System 1 (reactive
policy), and System 2 (deliberation), with models from any backend and tiers on any
hardware layout — one process, several accelerators, KVM guests, or separate machines.

This document covers components, responsibilities, and contracts. Wire formats,
transports, config schema, and numeric limits are deferred ([§14](#14-deferred-decisions)).
Code is pseudo-code unless marked **existing**. Config snippets are illustrative.

---

## Contents

1. [Summary](#1-summary)
2. [Motivation](#2-motivation)
3. [Terminology](#3-terminology)
4. [Architecture](#4-architecture)
5. [Examples](#5-examples)
6. [RobotRuntime and ControlLoop](#6-robotruntime-and-controlloop)
7. [Actions and Effectors](#7-actions-and-effectors)
8. [Robot Skills](#8-robot-skills)
9. [Models and Backends](#9-models-and-backends)
10. [Contracts](#10-contracts)
11. [Deployment](#11-deployment)
12. [Safety, Time, and Recording](#12-safety-time-and-recording)
13. [Clients](#13-clients)
14. [Deferred Decisions](#14-deferred-decisions)
15. [Implementation Phases](#15-implementation-phases)
16. [Decisions](#16-decisions)

---

## 1. Summary

Today PhysicalAI runs one action source on one robot in one fixed-rate loop:

```python
# existing
runtime = RobotRuntime(robot=so101, action_source=PolicySource(model), fps=30)
runtime.run()
```

This proposal extends that to a three-tier system:

| Tier         | Responsibility                             | Typical rate           | Examples                                     |
| ------------ | ------------------------------------------ | ---------------------- | -------------------------------------------- |
| **System 2** | Interpret the task, plan, check outcomes   | event-driven, 0.1–5 Hz | cloud LLM/VLM; local VLM (SGLang, llama.cpp) |
| **System 1** | Map observations to motion                 | 10–50 Hz               | VLA (GR00T, π0), ACT, diffusion, navigation  |
| **System 0** | Track setpoints, stabilize, enforce limits | 200 Hz – 1 kHz         | whole-body control, IK, impedance, vendor    |

Core ideas:

- One `RobotRuntime` per **partition** (process, VM, or machine) hosts the loops and
  models assigned there and connects to the rest.
- **Robot skills** (`pick`, `place`, `servo`, `navigate_to`) run on **leased effectors**.
  Scripts, Studio, MCP hosts, and a built-in agent all start them through one API.
- Three contracts cross partitions: **robot command interface**, **inference API**,
  **skills API**.
- Changing layout (one laptop ↔ robot + GPU box + cloud ↔ KVM guests) is configuration,
  not a redesign.

---

## 2. Motivation

Robot stacks converge on a slow reasoning model over a fast visuomotor policy over a
high-rate controller. Models often do not fit in one memory; tiers need different OS and
real-time properties; vendors commonly run them as separate KVM guests on one SoC.

PhysicalAI already supports a single fixed-rate loop, sync/async/RTC policy execution, and
(in progress) framework plugins with isolated environments. It does not yet support
multiple decision-makers at different rates, language goals turned into robot behavior,
cross-partition deployment, or a non-bypassable safety path for agent-originated commands.

**Requirements**

| #   | Requirement                                                                                   |
| --- | --------------------------------------------------------------------------------------------- |
| R1  | System 0 / 1 / 2 in any combination, including System 2 commanding System 0 directly          |
| R2  | Models from any backend (cloud API, llama.cpp, SGLang, OpenVINO, ONNX, LeRobot, GR00T, …)     |
| R3  | Tiers on different machines, VMs, or accelerators — or all on one host — without code changes |
| R4  | Multiple clients: Python, CLI, Studio, MCP, built-in agent, human                             |
| R5  | No client, model, or partition failure bypasses safety limits                                 |
| R6  | Every autonomous run produces labeled training data                                           |
| R7  | Existing single-policy scripts and configs keep working                                       |
| R8  | Framework-specific code stays in plugins                                                      |

**Non-goals:** hypervisor management; hard real-time loops in Python (above ~200 Hz stays
native/vendor); an agent framework; general robotics middleware; training.

---

## 3. Terminology

| Term                 | Definition                                                                 |
| -------------------- | -------------------------------------------------------------------------- |
| **Tier**             | System 0, 1, or 2 — a responsibility and rate band, not a process          |
| **Partition**        | Process, container, VM, or machine                                         |
| **`RobotRuntime`**   | Main runtime; one per partition                                            |
| **`ControlLoop`**    | Fixed-rate loop body; one per hosted tier                                  |
| **Effector**         | Independently commandable part (`arm`, `gripper`, `base`, `whole_body`, …) |
| **`ActionSource`**   | **Existing** tick-level action producer (policy, teleop)                   |
| **Robot skill**      | Named capability with typed params (`pick`, `servo`). Class: `Skill`       |
| **Agent skill**      | Coding-assistant `SKILL.md` file. Unrelated to robot skills                |
| **`SkillHandle`**    | Status, feedback, cancel, update, result for one run                       |
| **Lease**            | Exclusive, time-limited write access to effectors; renewed by heartbeat    |
| **Executive**        | Runs skills, leases, `WorldState`, recording. Exposed as `rt.skills`       |
| **`WorldState`**     | Latest-value store of timestamped facts (poses, detections, captions)      |
| **`ActionModel`**    | Observation → action chunk (**existing** `InferenceModel`)                 |
| **`ReasoningModel`** | Messages / images / tools → text or tool calls                             |
| **`ModelSpec`**      | Declared model inputs, outputs, and what the output commands               |
| **Placement**        | Where a model runs: `inprocess`, `worker`, or `remote`                     |
| **Safety gate**      | Final transform before hardware; hard limits                               |
| **Intent policy**    | Which clients may run which skills                                         |

"Skill" means robot skill unless qualified.

---

## 4. Architecture

```text
Clients    Python │ CLI │ Studio │ MCP host │ built-in agent │ human
           ───────┴─────┴────────┴──────────┴────────────────┴──────
                                  │ skills API
           Executive (physicalai.skills)
           skills · leases · WorldState · intent policy · recording
           ────────────────────────────────────────────────────────
System 1   ControlLoop @ 10–50 Hz  ── inference API ──► ActionModel
                                  │ robot command interface
System 0   ControlLoop or vendor controller · safety gate · watchdog
           ────────────────────────────────────────────────────────
Hardware   motors · sensors · cameras
```

**Rules**

1. Place loops by rate; place models by memory. System 0 next to motors; System 1 on robot
   or edge; System 2 anywhere. A model can be remote while its calling loop stays local.
2. Each tier presents a `Robot` to the tier above. A vendor controller is a `Robot`.
3. Three contracts cross partitions and must work in-process, across processes, VMs, and hosts.
4. All clients use the skills API. No privileged agent path.
5. Commands are leased and time-bounded. Missed heartbeats or deadlines → hold / stop.
6. Framework, data spec, and placement are independent axes.

**User-facing API**

```python
# existing single loop — unchanged
RobotRuntime(robot=so101, action_source=PolicySource(model), fps=30).run()

with RobotRuntime.from_config("robot.yaml") as rt:
    rt.skills.submit("pick", {"object": "mug"}).result()

RobotRuntime.from_config("robot.yaml", partition="s1").run()

rt = RuntimeClient("robot.local")   # same .skills / .world surface remotely
```

```bash
physicalai run robot.yaml
physicalai run robot.yaml --partition s0
physicalai up  robot.yaml            # start all partitions locally (dev)
```

---

## 5. Examples

### 5.1 One policy (existing)

```python
model = InferenceModel("./exports/act_policy")
RobotRuntime(
    robot=SO101(port="/dev/ttyACM0"),
    action_source=PolicySource(model, execution=AsyncExecution()),
    fps=30,
).run(duration_s=60)
```

Unchanged. Internally: one `ControlLoop`, one effector, no executive.

### 5.2 Scripted skills

```python
rt = RobotRuntime.from_config("tabletop.yaml")

rt.skills.register(PolicySkill(
    name="pick", model="pick_policy", claims={"arm", "gripper"},
    task="pick up the {object}", success=GripperClosedOnObject(),
))
rt.skills.register(PolicySkill(
    name="place", model="place_policy", claims={"arm", "gripper"},
    task="place it in the {location}", success=ObjectAt(),
))

with rt:
    for obj, loc in [("red block", "left bin"), ("blue block", "right bin")]:
        if rt.skills.submit("pick", {"object": obj}).result().status is Status.SUCCEEDED:
            rt.skills.submit("place", {"location": loc}).result()
```

Flow: authorize → lease effectors → attach `PolicySource` → success → detach → record episode.

### 5.3 Reasoning model or MCP as System 2

```python
agent = ToolLoopAgent(load_reasoning_model("anthropic", model="claude-…"), rt.skills)
agent.run("Put red blocks left, blue blocks right.")
```

```bash
physicalai run tabletop.yaml --serve-skills --mcp
```

MCP tools are generated from the skill registry. Client disconnect → lease expires → hold.

### 5.4 System 2 commanding System 0

```python
rt.skills.register(ServoSkill(name="servo", claims={"arm"}, max_step_m=0.01))
# observe → servo(dx=0.01) → observe → … → stop
```

Deadline-bounded setpoints into System 0; gate clamps; watchdog holds if calls stop.

### 5.5 Mixed backends and remote models

```yaml
models:
  planner:
    {
      kind: reasoning,
      backend: openai-compatible,
      base_url: http://localhost:30000/v1,
    }
  pick_policy:
    { kind: action, framework: gr00t, placement: remote, endpoint: gpu-box }
  place_policy: { kind: action, framework: lerobot, placement: worker }
  detector:
    { kind: action, framework: openvino, path: exports/detector, device: NPU }
```

Skills name models; frameworks stay in plugins. `worker` ↔ `remote` is a config change.

### 5.6 Humanoid across partitions

```yaml
robot:
  driver: unitree_g1
  effectors: [whole_body, left_arm, right_arm, base, head]

skills:
  manipulate: { type: policy, model: bimanual, claims: [left_arm, right_arm] }
  walk_to: { type: delegated, driver: nav2, claims: [base] }
  look_at: { type: setpoint, claims: [head] }

partitions: # omit to run everything on one host
  s0: { hosts: [tiers.s0] }
  s1: { hosts: [tiers.s1, executive, models.bimanual] }
  s2: { hosts: [models.planner, agent] }
```

```python
rt.skills.submit("look_at", {"target": "shelf"})
rt.skills.submit("walk_to", {"target": "shelf"}).result()
rt.skills.submit("manipulate", {"task": "take the box"}).result()
```

Disjoint effectors run concurrently via leases. System 2 reaches hardware only through the
skills API. Same Python with or without `partitions`.

---

## 6. RobotRuntime and ControlLoop

```text
RobotRuntime               one per partition
 ├─ ControlLoop "s0"       if System 0 is hosted here
 ├─ ControlLoop "s1"       if System 1 is hosted here (Robot = System 0 local or remote)
 ├─ Executive              normally with System 1; exposed as rt.skills
 ├─ models                 in-process, workers, or remote clients
 └─ links                  connections to other partitions
```

The current loop body (timing, resilient I/O, callbacks, interpolation, shutdown) moves into
`ControlLoop`. The existing constructor stays for the single-loop case.

```python
class RobotRuntime:
    def __init__(self, robot, action_source, fps, ...): ...   # existing
    @classmethod
    def from_config(cls, config, *, partition: str | None = None) -> Self: ...
    def connect / run / start / stop / disconnect(self): ...
    skills: SkillsAPI
    world: WorldState
    loops: Mapping[str, ControlLoop]
```

**Tick additions:** attach/detach sources per lease at tick boundaries; accept deadline-bounded
setpoints; hold idle effectors; run a dedicated safety gate after callbacks.

| Tier                           | Hosted in one `RobotRuntime`? | Notes                           |
| ------------------------------ | ----------------------------- | ------------------------------- |
| Light System 0 (interpolation) | yes                           | nested in System 1, as today    |
| Heavy System 0                 | yes                           | separate process, RT scheduling |
| Vendor / remote System 0       | connected                     | robot command interface         |
| System 1                       | yes                           | `ControlLoop`                   |
| System 2 (local)               | yes                           | event-driven                    |
| System 2 (remote / cloud)      | connected                     | skills API                      |

---

## 7. Actions and Effectors

Multi-effector robots need independently owned parts. An action becomes a mapping from
effector to command. A flat `np.ndarray` remains valid for single-effector sources, so
existing `PolicySource` / `TeleopSource` keep working.

```python
action = {
    "left_arm": Command(mode=EE_POSE, values=..., frame="base"),
    "base":     Command(mode=TWIST, values=...),
}
```

`Robot` gains optional `command_interfaces()`. Without it: one effector, joint position
(today's behavior). Each tier above System 0 drives System 0 through the same `Robot`
protocol — `LoopRobot` locally, `RemoteRobot` across partitions.

---

## 8. Robot Skills

Package: `physicalai.skills` (no LLM dependency). Accessed as `rt.skills`.

```python
class Skill(Protocol):
    name: str
    description: str
    params: type[Params]              # → tool / form / CLI schema
    claims: frozenset[Effector]

    def can_start(self, params, world: WorldState) -> Precondition: ...
    def start(self, params, ctx: SkillContext) -> SkillRun: ...
```

| Type          | How it actuates                      | Examples                |
| ------------- | ------------------------------------ | ----------------------- |
| **Attached**  | attaches an `ActionSource` to a loop | `PolicySkill`, teleop   |
| **Setpoint**  | deadline-bounded setpoints per call  | `ServoSkill`, look-at   |
| **Delegated** | external controller; mirrors status  | Nav2, vendor locomotion |

```python
# PolicySkill: generic "run this policy"; pick/place are configured instances
class PolicySkill:
    def start(self, params, ctx):
        source = PolicySource(ctx.models.action(self.model), task=self.task.format(**params))
        ctx.loops["s1"].attach(source, effectors=self.claims, lease=ctx.lease)
        return AttachedRun(source, success=self.success)
```

**Leases:** one writer per effector; parent/child (`whole_body` ⊃ arms/base); higher priority
preempts; heartbeats required or the run is cancelled and effectors hold.

**`SkillHandle`:** `status`, `feedback`, `update`, `cancel`, `result` (blocks until terminal).

**`WorldState`:** latest timestamped facts; one writer per key; no history (recording keeps
history). Written by loops and perceivers; read by skills, success checks, and clients.

**Success checks** return confidence, not a bare bool:

```python
class GripperClosedOnObject:   # gripper closed on something
    def evaluate(self, world): ...

class ObjectAt:                # detector: object in a region
    def evaluate(self, world): ...

class VLMJudge:                # asks a ReasoningModel
    def evaluate(self, world): ...
```

---

## 9. Models and Backends

Roles (planner, manipulator, judge) bind to models. Models have two interfaces:

```python
class ActionModel(Protocol):       # existing InferenceModel
    def predict_action_chunk(self, observation) -> np.ndarray: ...
    def reset(self) -> None: ...

class ReasoningModel(Protocol):
    def complete(self, messages, *, tools=(), response_format=None) -> Completion: ...
```

**`ModelSpec`** declares inputs/outputs and what the output commands. Checked against the
robot at startup. Plugins convert native formats (LeRobot features, GR00T `modality.json`).

| Interface        | Backends                                                                                                             |
| ---------------- | -------------------------------------------------------------------------------------------------------------------- |
| `ActionModel`    | OpenVINO, ONNX; LeRobot / GR00T plugins                                                                              |
| `ReasoningModel` | OpenAI-compatible (SGLang, vLLM, llama.cpp server, Ollama); Anthropic; Gemini; in-process OpenVINO GenAI / llama.cpp |

**Placement** (independent of framework):

| Placement   | Use                                            |
| ----------- | ---------------------------------------------- |
| `inprocess` | OpenVINO/ONNX, small models                    |
| `worker`    | conflicting deps / crash isolation (`isolate`) |
| `remote`    | models that do not fit locally                 |

`worker` and `remote` share protocol; a worker is a local inference server. Remote
inference is designed but not yet implemented. Existing policy servers (openpi, LeRobot
async, GR00T) can be wrapped as `ActionModel`s.

Remote System 1 needs chunk horizon > RTT + inference + margin. Late chunks → hold +
starvation feedback. On shared hosts: System 0 must not share compute with System 2;
System 1 has priority over System 2 on a shared accelerator (measure jitter).

---

## 10. Contracts

### Robot command interface (System 1/2 ↔ System 0)

Describe effectors; write setpoints (effector, mode, values, stamp, deadline, lease); read
state. Expired setpoints ignored; stale effectors apply hold/decay/stop; wrong lease
rejected; System 0 enforces limits. Must be implementable without Python.

### Inference API (loops ↔ action models)

Describe model; stream observations → action chunks; reset; health. Timestamps on both
sides. Basis: existing policy-server design.

### Skills API (clients ↔ executive)

List / submit / stream updates / update / cancel / heartbeat / observe / events / stop.
Missed heartbeats expire leases. MCP is generated from the skill list.

---

## 11. Deployment

Same system description; only partitioning and placement change.

| Layout                         | What differs                                          |
| ------------------------------ | ----------------------------------------------------- |
| One process                    | System 1 only; System 0 = interpolation               |
| One host, several accelerators | System 0 process; models assigned to devices          |
| Robot + GPU host + cloud       | large action models `remote`; reasoning hosted        |
| KVM guests                     | one partition per tier; System 2 has no device access |
| Vendor System 0                | vendor implements the robot command interface         |

---

## 12. Safety, Time, and Recording

**Safety layers** (none of the software layers are optional for actuation):

| Layer                       | Enforces                                    |
| --------------------------- | ------------------------------------------- |
| Hardware / firmware         | power, torque, temperature                  |
| Safety gate (authoritative) | joint / velocity / workspace / force limits |
| Leases + deadlines          | single writer; stale → hold                 |
| Intent policy               | which clients may run which skills          |
| Partition boundary          | System 2 guest has no device access         |

`unrestricted` intent removes permission checks only — gates, leases, and deadlines remain.

| Failure           | Response                                |
| ----------------- | --------------------------------------- |
| Client disconnect | lease expires; cancel; hold             |
| System 2 crash    | same; System 1 / 0 continue             |
| Remote model late | hold; report starvation; skill may fail |
| System 1 silent   | System 0 stale fallback                 |
| System 0 fault    | System 1 stops; active skills fail      |

**Time:** shared synchronized clock; absolute deadlines; `WorldState` stamps for staleness.

**Recording:** one episode per skill run — loop-rate data (LeRobot format), skill/params/
client/outcome, model provenance, events, and agent transcript when applicable. Failures
and cancellations are recorded the same way.

---

## 13. Clients

```python
rt = RuntimeClient("robot.local")
h = rt.skills.submit("pick", {"object": "mug"})
print(h.result().status)
```

```bash
physicalai skills list
physicalai skills run pick --object mug
physicalai stop
```

MCP tools = skill list + `observe` + `stop`. Reference agent: a thin tool-calling loop over
`rt.skills` (may live outside the runtime package). Periodic System 2 supervision of a
running System 1 skill uses `handle.update` / `cancel` on a timer.

---

## 14. Deferred Decisions

| Topic                                     | Needed when building  |
| ----------------------------------------- | --------------------- |
| Exact command modes / payloads            | ControlLoop / actions |
| Config schema                             | multi-partition runs  |
| Inference / skills / command wire formats | remote / VM links     |
| Lease TTLs, priorities                    | executive             |
| Intent-policy format                      | multi-client          |
| Cross-partition transports                | multi-host / KVM      |
| RT platform for System 0                  | hosted heavy System 0 |
| Clock sync mechanism                      | multi-partition       |
| Recording format extensions               | data flywheel         |
| Reference agent home                      | System 2 clients      |
| World-model interface                     | imagine-and-rank      |

---

## 15. Implementation Phases

| Phase | Scope                                                   | Exit criterion                           |
| ----- | ------------------------------------------------------- | ---------------------------------------- |
| 1     | Extract `ControlLoop`                                   | existing tests pass                      |
| 2     | Effector actions, attach/detach, deadlines, safety gate | existing robots unchanged; gate tests    |
| 3     | Inference API, placement, `ModelSpec`                   | same actions across placements           |
| 4     | Executive, leases, handles, recording; scripted client  | multi-framework tabletop task            |
| 5     | Reasoning clients, skills API, MCP, reference agent     | cloud model and MCP drive the same robot |
| 6     | Setpoint skills; robot command across processes         | System 2 → System 0; watchdog holds      |
| 7     | Hosted System 0; VM partitions                          | humanoid / mobile base across guests     |
| 8     | Delegated navigation; world model                       | navigate → pick → place                  |

Phases 1–4 are useful without the later ones.

---

## 16. Decisions

```text
tiers           System 0 control · System 1 policy · System 2 deliberation
runtime         one RobotRuntime per partition
loops           ControlLoop = current loop body; one per hosted tier
tier link       each tier is a Robot to the tier above
actions         effector-keyed mappings; flat arrays still valid
skills          physicalai.skills; rt.skills; attached / setpoint / delegated
concurrency     effector leases (incl. parent/child)
models          ActionModel + ReasoningModel; roles are bindings
interop         ModelSpec at startup; plugins convert native formats
placement       inprocess · worker · remote
contracts       robot command · inference · skills
safety          gate + leases + deadlines + intent policy
data            one labeled episode per skill run
terminology     robot skill vs agent skill (SKILL.md)
```
