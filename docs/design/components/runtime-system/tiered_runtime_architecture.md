# Tiered Robot Runtime: System 0 / 1 / 2

**Status:** Proposal for design review.

How PhysicalAI runs System 0 (control), System 1 (reactive policy), and System 2
(deliberation): models from any backend; tiers in one process, across accelerators, KVM
guests, or separate machines.

Architecture only — wire formats, transports, config schema, and numeric limits are
deferred ([§13](#13-deferred-decisions)). Pseudo-code unless marked **existing**.

---

## Contents

1. [Summary](#1-summary)
2. [Motivation](#2-motivation)
3. [Terminology](#3-terminology)
4. [Architecture](#4-architecture)
5. [Examples](#5-examples)
6. [RobotRuntime and ControlLoop](#6-robotruntime-and-controlloop)
7. [Actions and Effectors](#7-actions-and-effectors)
8. [Goals and Skills](#8-goals-and-skills)
9. [Models and Backends](#9-models-and-backends)
10. [APIs Across Partitions](#10-apis-across-partitions)
11. [Deployment](#11-deployment)
12. [Safety, Time, and Recording](#12-safety-time-and-recording)
13. [Deferred Decisions](#13-deferred-decisions)
14. [Implementation Phases](#14-implementation-phases)
15. [Decisions](#15-decisions)

---

## 1. Summary

Today: one action source, one robot, one loop.

```python
# existing
RobotRuntime(robot=so101, action_source=PolicySource(model), fps=30).run()
```

Proposed seam:

```text
Agent  →  Goal  →  Skill  →  RobotRuntime
```

An **Agent** (script, Studio, MCP host, or built-in tool loop) submits a **Goal**. A
**Skill** pursues it and returns a **SkillHandle**. **RobotRuntime** runs the control
loops.

| Tier         | Role                     | Rate                   | Examples                              |
| ------------ | ------------------------ | ---------------------- | ------------------------------------- |
| **System 2** | Plan and check           | event-driven, 0.1–5 Hz | cloud / local LLM·VLM                 |
| **System 1** | Observation → motion     | 10–50 Hz               | VLA, ACT, diffusion, navigation       |
| **System 0** | Track, stabilize, limits | 200 Hz – 1 kHz         | WBC, IK, impedance, vendor controller |

Scaling (same story, more machinery):

- One `RobotRuntime` per **partition** (process / VM / machine).
- Skills claim **effectors** under **leases** (concurrent work on disjoint parts).
- Models: any framework; **placement** `inprocess` / `worker` / `remote`.
- Partition boundaries use three APIs: robot command, inference, skills.
- Layout changes are configuration.

---

## 2. Motivation

Stacks converge on slow reasoning over fast policy over high-rate control. Models often
do not share memory; tiers need different OS / RT properties; vendors often use separate
KVM guests on one SoC.

**Have:** single fixed-rate loop; sync / async / RTC policy execution; framework plugins
with isolated envs (in progress).

**Need:** multi-rate decision-makers; language goals → robot action; cross-partition
deploy; non-bypassable safety for agent-originated commands; labeled data from every run;
unchanged single-policy scripts; frameworks only in plugins.

**Out of scope:** hypervisors; hard RT in Python (≳200 Hz stays native/vendor); agent
frameworks; general middleware; training.

---

## 3. Terminology

**Story (lead with these)**

| Term               | Definition                                                                                               |
| ------------------ | -------------------------------------------------------------------------------------------------------- |
| **Agent**          | Anything that submits goals: script, Studio, MCP host, built-in tool loop. A role, not a required class. |
| **Goal**           | Skill name + params + success condition. One request to do something.                                    |
| **Skill**          | Robot capability (`pick`, `servo`, `navigate_to`). Class: `Skill`.                                       |
| **`SkillHandle`**  | Live run: status, feedback, update, cancel, result.                                                      |
| **`RobotRuntime`** | Main runtime; one per partition.                                                                         |

**Structure**

| Term                 | Definition                                                      |
| -------------------- | --------------------------------------------------------------- |
| **Tier**             | System 0 / 1 / 2 — responsibility and rate band, not a process. |
| **`ControlLoop`**    | Fixed-rate loop body; one per hosted tier.                      |
| **Effector**         | Named robot part (`arm`, `gripper`, `base`, `whole_body`, …).   |
| **`ActionSource`**   | **Existing** tick-level action producer (policy, teleop).       |
| **`WorldState`**     | Latest timestamped facts (poses, detections, captions).         |
| **`ActionModel`**    | Observation → action chunk (**existing** `InferenceModel`).     |
| **`ReasoningModel`** | Messages / images / tools → text or tool calls.                 |
| **`ModelSpec`**      | What a model expects and produces.                              |

**Scaling**

| Term              | Definition                                                               |
| ----------------- | ------------------------------------------------------------------------ |
| **Partition**     | Where code runs: process, container, VM, or machine.                     |
| **Lease**         | Exclusive, time-limited write access to effectors; renewed by heartbeat. |
| **Placement**     | Model location: `inprocess`, `worker`, `remote`.                         |
| **Safety gate**   | Final transform before hardware; hard limits.                            |
| **Access policy** | Which agents may run which skills.                                       |
| **Agent skill**   | Coding-assistant `SKILL.md`. Unrelated to robot skills.                  |

"Skill" means robot skill unless qualified. The component behind `rt.skills` is internal;
call it the skills service in prose.

---

## 4. Architecture

```text
Agent (script │ Studio │ MCP │ built-in loop │ human)
        │  Goal = skill + params + success
        ▼
   rt.skills          registry · leases · WorldState · access policy · recording
        │  Skill → ActionSource / setpoints / external controller
        ▼
System 1   ControlLoop @ 10–50 Hz  ── inference API ──► ActionModel
        │  robot command (as Robot)
        ▼
System 0   ControlLoop or vendor · safety gate · watchdog
        ▼
Hardware
```

1. Place loops by rate; models by memory. System 0 at the motors; System 1 on robot/edge;
   System 2 anywhere.
2. Each tier is a `Robot` to the tier above (vendor controller = `Robot`).
3. Three APIs cross partitions (in-process through cross-host).
4. Every Agent uses `rt.skills` — no privileged path.
5. Leases + deadlines; missed heartbeat / deadline → hold or stop.
6. Framework, `ModelSpec`, and placement are independent.

```python
# existing — unchanged
RobotRuntime(robot=so101, action_source=PolicySource(model), fps=30).run()

with RobotRuntime.from_config("robot.yaml") as rt:
    rt.skills.submit(Goal("pick", object="mug")).result()

RobotRuntime.from_config("robot.yaml", partition="s1").run()
rt = RuntimeClient("robot.local")   # same .skills / .world remotely
```

```bash
physicalai run robot.yaml
physicalai run robot.yaml --partition s0
physicalai up  robot.yaml
```

---

## 5. Examples

### 5.1 One policy (existing)

```python
RobotRuntime(
    robot=SO101(port="/dev/ttyACM0"),
    action_source=PolicySource(InferenceModel("./exports/act"), execution=AsyncExecution()),
    fps=30,
).run(duration_s=60)
```

One `ControlLoop`, one effector; no skills service.

### 5.2 Scripted Agent

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
        if rt.skills.submit(Goal("pick", object=obj)).result().status is Status.SUCCEEDED:
            rt.skills.submit(Goal("place", location=loc)).result()
```

Authorize → lease → attach `PolicySource` → success → detach → record.

### 5.3 Built-in Agent or MCP

```python
Agent(load_reasoning_model("anthropic", model="claude-…"), rt.skills).run(
    "Put red blocks left, blue blocks right."
)
```

```bash
physicalai run tabletop.yaml --serve-skills --mcp
```

MCP tools = skill registry. Disconnect → lease expires → hold.

### 5.4 Agent → System 0

```python
rt.skills.register(ServoSkill(name="servo", claims={"arm"}, max_step_m=0.01))
# observe → Goal("servo", dx=0.01) → … → stop
```

Deadline-bounded setpoints; gate clamps; watchdog holds if calls stop.

### 5.5 Mixed backends

```yaml
models:
  planner:     {kind: reasoning, backend: openai-compatible, base_url: http://localhost:30000/v1}
  pick_policy: {kind: action, framework: gr00t, placement: remote, endpoint: gpu-box}
  place_policy:{kind: action, framework: lerobot, placement: worker}
  detector:    {kind: action, framework: openvino, path: exports/detector, device: NPU}
```

Skills name models; frameworks stay in plugins. `worker` ↔ `remote` is config.

### 5.6 Humanoid across partitions

```yaml
robot:
  {
    driver: unitree_g1,
    effectors: [whole_body, left_arm, right_arm, base, head],
  }
skills:
  manipulate: { type: policy, model: bimanual, claims: [left_arm, right_arm] }
  walk_to: { type: delegated, driver: nav2, claims: [base] }
  look_at: { type: setpoint, claims: [head] }
partitions: # omit → one host
  s0: { hosts: [tiers.s0] }
  s1: { hosts: [tiers.s1, skills, models.bimanual] }
  s2: { hosts: [models.planner, agent] }
```

```python
rt.skills.submit(Goal("look_at", target="shelf"))
rt.skills.submit(Goal("walk_to", target="shelf")).result()
rt.skills.submit(Goal("manipulate", task="take the box")).result()
```

Disjoint effectors concurrent via leases. Same Python with or without `partitions`.

---

## 6. RobotRuntime and ControlLoop

```text
RobotRuntime            one per partition
 ├─ ControlLoop s0      if hosted here
 ├─ ControlLoop s1      if hosted here (Robot = System 0 local or remote)
 ├─ skills service      normally with System 1 → rt.skills
 ├─ models              in-process / worker / remote
 └─ links               other partitions
```

Current loop body → `ControlLoop`. Existing constructor kept for the single-loop case.

```python
class RobotRuntime:
    def __init__(self, robot, action_source, fps, ...): ...  # existing
    @classmethod
    def from_config(cls, config, *, partition: str | None = None) -> Self: ...
    skills: SkillsAPI
    world: WorldState
    loops: Mapping[str, ControlLoop]
```

**Tick additions:** attach/detach per lease at tick boundaries; deadline-bounded setpoints;
hold idle effectors; safety gate after callbacks.

| Tier                     | In one `RobotRuntime`? | Notes                      |
| ------------------------ | ---------------------- | -------------------------- |
| Light System 0           | yes                    | nested in System 1 (today) |
| Heavy System 0           | yes                    | separate process, RT       |
| Vendor / remote System 0 | connected              | robot command API          |
| System 1                 | yes                    | `ControlLoop`              |
| System 2 local / remote  | yes / connected        | event-driven / skills API  |

---

## 7. Actions and Effectors

Actions become effector → command maps. Flat `np.ndarray` still valid for single-effector
sources (`PolicySource` / `TeleopSource` unchanged).

```python
{"left_arm": Command(mode=EE_POSE, values=..., frame="base"), "base": Command(mode=TWIST, values=...)}
```

Optional `Robot.command_interfaces()`. Default: one effector, joint position. Upper tiers
drive System 0 via the same `Robot` protocol (`LoopRobot` / `RemoteRobot`).

---

## 8. Goals and Skills

Package `physicalai.skills` (no LLM dependency). Surface: `rt.skills`.

```python
@dataclass(frozen=True)
class Goal:
    skill: str                          # "pick"
    params: Mapping[str, Any]           # {"object": "mug"}
    success: SuccessCheck | None = None
    # convenience: Goal("pick", object="mug")

class Skill(Protocol):
    name: str
    description: str
    params: type[Params]                # → tool / form / CLI schema
    claims: frozenset[Effector]

    def can_start(self, goal: Goal, world: WorldState) -> Precondition: ...
    def start(self, goal: Goal, ctx: SkillContext) -> SkillRun: ...
```

| Type          | Actuation                           | Examples              |
| ------------- | ----------------------------------- | --------------------- |
| **Attached**  | `ActionSource` on a loop            | `PolicySkill`, teleop |
| **Setpoint**  | deadline-bounded setpoints per call | `ServoSkill`, look-at |
| **Delegated** | external controller; mirror status  | Nav2, vendor loco     |

```python
class PolicySkill:   # pick / place = configured instances, not subclasses
    def start(self, goal, ctx):
        source = PolicySource(ctx.models.action(self.model), task=self.task.format(**goal.params))
        ctx.loops["s1"].attach(source, effectors=self.claims, lease=ctx.lease)
        return AttachedRun(source, success=goal.success or self.success)
```

**Leases:** one writer per effector; parent/child (`whole_body` ⊃ arms/base); priority
preempts; no heartbeat → cancel + hold.

**`SkillHandle`:** `status` · `feedback` · `update` · `cancel` · `result`.

**`WorldState`:** latest facts; one writer per key; no history. Loops and perceivers write;
skills, success checks, Agents read.

**Success checks** return confidence:

```python
GripperClosedOnObject  # gripper closed on something
ObjectAt               # detector: object in region
VLMJudge               # ReasoningModel
```

---

## 9. Models and Backends

Roles (planner, manipulator, judge) bind to models — not separate base classes.

```python
class ActionModel(Protocol):     # existing InferenceModel
    def predict_action_chunk(self, observation) -> np.ndarray: ...
    def reset(self) -> None: ...

class ReasoningModel(Protocol):
    def complete(self, messages, *, tools=(), response_format=None) -> Completion: ...
```

**`ModelSpec`:** inputs/outputs and what the output commands; checked at startup. Plugins
convert LeRobot / GR00T native formats.

| Interface        | Backends                                                                                                      |
| ---------------- | ------------------------------------------------------------------------------------------------------------- |
| `ActionModel`    | OpenVINO, ONNX; LeRobot / GR00T plugins                                                                       |
| `ReasoningModel` | OpenAI-compatible (SGLang, vLLM, llama.cpp, Ollama); Anthropic; Gemini; in-process OpenVINO GenAI / llama.cpp |

| Placement   | Use                                |
| ----------- | ---------------------------------- |
| `inprocess` | OpenVINO/ONNX, small models        |
| `worker`    | conflicting deps / crash isolation |
| `remote`    | models that do not fit locally     |

`worker` ≡ local inference server; same protocol as `remote`. Remote inference designed,
not shipped. Wrap existing policy servers as `ActionModel`s when useful.

Remote System 1: chunk horizon > RTT + inference + margin; late → hold + starvation
feedback. Shared host: System 0 ∦ System 2 compute; System 1 priority over System 2
(measure jitter).

---

## 10. APIs Across Partitions

### Robot command (System 1/2 → System 0)

Describe effectors; write setpoints (effector, mode, values, stamp, deadline, lease); read
state. Expired / wrong-lease setpoints rejected; stale → hold/decay/stop; System 0 enforces
limits. Implementable without Python.

### Inference (loops ↔ action models)

Describe model; stream obs → action chunks; reset; health. Timestamps both sides. Basis:
existing policy-server design.

### Skills (Agents ↔ `rt.skills`)

List / submit Goal / stream updates / update / cancel / heartbeat / observe / events / stop.
Missed heartbeats expire leases. MCP generated from the skill list.

---

## 11. Deployment

| Layout                      | Difference                                            |
| --------------------------- | ----------------------------------------------------- |
| One process                 | System 1 only; System 0 = interpolation               |
| One host, multi-accelerator | System 0 process; models on devices                   |
| Robot + GPU + cloud         | large action models `remote`; reasoning hosted        |
| KVM guests                  | one partition per tier; System 2 has no device access |
| Vendor System 0             | vendor implements robot command API                   |

---

## 12. Safety, Time, and Recording

| Layer               | Enforces                             |
| ------------------- | ------------------------------------ |
| Hardware / firmware | power, torque, temperature           |
| Safety gate         | joint / velocity / workspace / force |
| Leases + deadlines  | single writer; stale → hold          |
| Access policy       | which Agents may run which skills    |
| Partition boundary  | System 2 guest: no device access     |

`unrestricted` access removes permission checks only; gates / leases / deadlines stay.

| Failure           | Response                         |
| ----------------- | -------------------------------- |
| Agent disconnect  | lease expires; cancel; hold      |
| System 2 crash    | same; System 1 / 0 continue      |
| Remote model late | hold; starvation; skill may fail |
| System 1 silent   | System 0 stale fallback          |
| System 0 fault    | System 1 stops; skills fail      |

**Time:** shared clock; absolute deadlines; `WorldState` stamps for staleness.

**Recording:** one episode per skill run (LeRobot loop data, Goal, Agent, outcome,
model provenance, events, agent transcript). Failures and cancels recorded equally.

---

## 13. Deferred Decisions

| Topic                         | When needed           |
| ----------------------------- | --------------------- |
| Command modes / payloads      | ControlLoop / actions |
| Config schema                 | multi-partition       |
| Wire formats (all three APIs) | remote / VM           |
| Lease TTLs / priorities       | skills service        |
| Access-policy format          | multi-Agent           |
| Cross-partition transports    | multi-host / KVM      |
| RT platform for System 0      | hosted heavy System 0 |
| Clock sync                    | multi-partition       |
| Recording format extensions   | data flywheel         |
| Built-in Agent package home   | System 2 clients      |
| World-model interface         | imagine-and-rank      |

---

## 14. Implementation Phases

| Phase | Scope                                                      | Exit criterion                   |
| ----- | ---------------------------------------------------------- | -------------------------------- |
| 1     | Extract `ControlLoop`                                      | existing tests pass              |
| 2     | Effector actions, attach/detach, deadlines, safety gate    | robots unchanged; gate tests     |
| 3     | Inference API, placement, `ModelSpec`                      | same actions across placements   |
| 4     | Skills service, leases, handles, recording; scripted Agent | multi-framework tabletop         |
| 5     | Reasoning clients, skills API, MCP, built-in Agent         | cloud + MCP drive same robot     |
| 6     | Setpoint skills; robot command across processes            | Agent → System 0; watchdog holds |
| 7     | Hosted System 0; VM partitions                             | humanoid / mobile across guests  |
| 8     | Delegated navigation; world model                          | navigate → pick → place          |

Phases 1–4 stand alone.

---

## 15. Decisions

```text
story           Agent → Goal → Skill → SkillHandle → RobotRuntime
tiers           System 0 control · System 1 policy · System 2 deliberation
runtime         one RobotRuntime per partition; ControlLoop per hosted tier
tier link       each tier is a Robot to the tier above
actions         effector-keyed maps; flat arrays still valid
skills          physicalai.skills / rt.skills; attached · setpoint · delegated
concurrency     effector leases (parent/child)
models          ActionModel + ReasoningModel; roles are bindings
interop         ModelSpec at startup; plugins convert native formats
placement       inprocess · worker · remote
APIs            robot command · inference · skills
safety          gate + leases + deadlines + access policy
data            one labeled episode per skill run
terminology     robot skill vs agent skill (SKILL.md)
```
