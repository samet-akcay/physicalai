# Tiered Robot Runtime: System 0 / 1 / 2

**Status:** Proposal for design review.

This document describes how PhysicalAI runs three tiers of robot decision-making: System 0
(control), System 1 (reactive policy), and System 2 (deliberation). Models may come from
any backend, and the tiers may share one process or be spread across accelerators, KVM
guests, or separate machines.

The document specifies architecture only. Wire formats, transports, the configuration
schema, and numeric limits are deferred ([§13](#13-deferred-decisions)). Code is
pseudo-code unless marked **existing**.

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
16. [References](#16-references)

---

## 1. Summary

The current runtime connects one action source to one robot through one fixed-rate loop:

```python
# existing
RobotRuntime(robot=so101, action_source=PolicySource(model), fps=30).run()
```

This proposal adds one seam above that loop:

```text
Agent  →  Goal  →  Skill  →  RobotRuntime
```

An agent (a script, Studio, an MCP host, or a built-in tool loop) submits a goal to the
skills service. The service starts the matching skill and returns a `SkillHandle` for
monitoring and cancellation. `RobotRuntime` continues to run the control loops.

| Tier         | Role                               | Rate                   | Examples                               |
| ------------ | ---------------------------------- | ---------------------- | -------------------------------------- |
| **System 2** | Plan, sequence, and verify         | Event-driven, 0.1–5 Hz | Cloud or local LLMs and VLMs           |
| **System 1** | Map observations to motion         | 10–50 Hz               | VLA, ACT, diffusion, navigation        |
| **System 0** | Track setpoints and enforce limits | 200 Hz – 1 kHz         | WBC, IK, impedance, vendor controllers |

The same model scales to larger systems without changing this seam:

- One `RobotRuntime` runs per partition (a process, VM, or machine).
- Skills claim effectors under leases, so work on disjoint parts of the robot can run
  concurrently.
- Models may come from any framework and run in one of three placements: `inprocess`,
  `worker`, or `remote`.
- Partitions communicate through three APIs: the robot command API, the inference API,
  and the skills API.
- Changing the deployment layout is a configuration change, not a code change.

---

## 2. Motivation

Recent robot systems separate slow reasoning from fast control. A language or
vision-language model plans and verifies, a learned policy produces motion, and a
high-rate controller tracks it [[1]](#16-references)–[[4]](#16-references). The System 1
and System 2 labels follow Kahneman [[5]](#16-references); System 0 denotes the control
layer beneath them.

These tiers place different demands on the platform. Their models often do not fit in
the same memory. They need different operating-system and real-time guarantees. Robot
vendors we have consulted run them in separate KVM guests on a single SoC.

**Current state.** PhysicalAI provides a single fixed-rate loop, synchronous,
asynchronous, and real-time-chunking policy execution [[6]](#16-references), and
framework plugins with isolated environments (in progress).

**Requirements.** The runtime must:

1. Run decision-makers at different rates.
2. Turn language goals into robot actions.
3. Deploy tiers across partitions.
4. Enforce safety that agent-originated commands cannot bypass.
5. Record labeled data from every run.
6. Keep existing single-policy scripts working unchanged.
7. Keep framework dependencies inside plugins.

**Out of scope.** Hypervisors, hard real-time control in Python (loops at 200 Hz and
above remain native or vendor-provided), agent frameworks, general-purpose middleware,
and training.

---

## 3. Terminology

Prose uses lowercase for concepts (agent, goal, skill). Code font denotes classes
(`Goal`, `Skill`, `SkillHandle`). Tiers are written System 0, 1, and 2 in prose and
`s0`, `s1`, and `s2` in configuration and code. "Skill" means robot skill unless
qualified otherwise.

**Core concepts**

| Term               | Definition                                                                                                           |
| ------------------ | -------------------------------------------------------------------------------------------------------------------- |
| **Agent**          | Any client that submits goals: a script, Studio, an MCP host, a built-in tool loop, or a human. A role, not a class. |
| **Goal**           | One request: a skill name, its parameters, and an optional success check.                                            |
| **Skill**          | A robot capability such as `pick`, `servo`, or `navigate_to`. Implements the `Skill` protocol.                       |
| **`SkillHandle`**  | The client's view of a running skill: status, feedback, update, cancel, and result.                                  |
| **Skills service** | The component that validates goals, grants leases, runs skills, and records episodes. Accessed as `rt.skills`.       |
| **`RobotRuntime`** | The main runtime. One instance runs per partition.                                                                   |

**Structure**

| Term                 | Definition                                                                    |
| -------------------- | ----------------------------------------------------------------------------- |
| **Tier**             | System 0, 1, or 2. A responsibility and rate band, not a process.             |
| **`ControlLoop`**    | A fixed-rate loop body. One runs per tier hosted in a partition.              |
| **Effector**         | A named part of the robot, such as `arm`, `gripper`, `base`, or `whole_body`. |
| **`ActionSource`**   | **Existing.** A per-tick action producer, such as a policy or teleoperation.  |
| **`WorldState`**     | The latest timestamped facts: poses, detections, and captions.                |
| **`ActionModel`**    | Maps observations to action chunks. **Existing** as `InferenceModel`.         |
| **`ReasoningModel`** | Maps messages, images, and tool definitions to text or tool calls.            |
| **`ModelSpec`**      | Declares the inputs a model expects and the outputs it produces.              |

**Distribution and safety**

| Term              | Definition                                                                        |
| ----------------- | --------------------------------------------------------------------------------- |
| **Partition**     | A unit of deployment: a process, container, VM, or machine.                       |
| **Lease**         | Exclusive, time-limited write access to a set of effectors, renewed by heartbeat. |
| **Placement**     | Where a model runs: `inprocess`, `worker`, or `remote`.                           |
| **Safety gate**   | The final transform before hardware. It enforces hard limits.                     |
| **Access policy** | Rules for which agents may run which skills.                                      |
| **Agent skill**   | A coding-assistant `SKILL.md` file. Unrelated to robot skills.                    |

---

## 4. Architecture

```text
Agent (script │ Studio │ MCP host │ built-in tool loop │ human)
        │  Goal: skill, params, success check
        ▼
Skills service (rt.skills): registry, leases, WorldState, access policy, recording
        │  Skill drives an ActionSource, setpoints, or an external controller
        ▼
System 1   ControlLoop at 10–50 Hz  ── inference API ──►  ActionModel
        │  robot command API (System 0 appears as a Robot)
        ▼
System 0   ControlLoop or vendor controller, safety gate, watchdog
        ▼
Hardware
```

The architecture follows six rules:

1. Loops are placed by rate and models by memory. System 0 runs next to the motors,
   System 1 on the robot or an edge device, and System 2 anywhere.
2. Each tier appears to the tier above as a `Robot`. A vendor controller is also a
   `Robot`.
3. Partitions communicate only through the three APIs. Each API works both in-process
   and across hosts.
4. Every agent goes through the skills service. There is no privileged path.
5. Every write is bounded by a lease and a deadline. A missed heartbeat or deadline
   causes a hold or a stop.
6. Framework, `ModelSpec`, and placement are independent choices.

The public surface keeps the existing constructor and adds configuration-driven entry
points:

```python
# existing: unchanged
RobotRuntime(robot=so101, action_source=PolicySource(model), fps=30).run()

# new: one partition, with skills
with RobotRuntime.from_config("robot.yaml") as rt:
    rt.skills.submit(Goal("pick", {"object": "mug"})).result()

# new: host one partition of a distributed deployment
RobotRuntime.from_config("robot.yaml", partition="s1").run()

# new: remote client with the same .skills and .world surface
rt = RuntimeClient("robot.local")
```

```bash
physicalai run robot.yaml                  # all tiers in one partition
physicalai run robot.yaml --partition s0   # one partition
physicalai up  robot.yaml                  # every partition in the configuration
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

This runs one `ControlLoop` with one effector. The skills service is not used.

### 5.2 Scripted agent

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
        if rt.skills.submit(Goal("pick", {"object": obj})).result().status is Status.SUCCEEDED:
            rt.skills.submit(Goal("place", {"location": loc})).result()
```

For each goal, the skills service checks the access policy, grants a lease on the
claimed effectors, attaches a `PolicySource` to the System 1 loop, and evaluates the
success check. It then detaches the source and records the episode.

### 5.3 Built-in agent or MCP host

The built-in agent is one reference implementation of the agent role:

```python
ToolLoopAgent(load_reasoning_model("anthropic", model="claude-…"), rt.skills).run(
    "Put red blocks in the left bin and blue blocks in the right bin."
)
```

An external MCP host can drive the same skills:

```bash
physicalai run tabletop.yaml --serve-skills --mcp
```

The MCP tool list is generated from the skill registry. If the host disconnects, its
lease expires and the claimed effectors hold position.

### 5.4 Agent driving System 0

```python
rt.skills.register(ServoSkill(name="servo", claims={"arm"}, max_step_m=0.01))
# The agent observes, submits Goal("servo", {"dx": 0.01}), and repeats until done.
```

Each call writes a setpoint with a deadline. The safety gate clamps the setpoint, and
the watchdog holds the arm if calls stop arriving.

### 5.5 Mixed backends

```yaml
models:
  planner:
    kind: reasoning
    backend: openai-compatible
    base_url: http://localhost:30000/v1
  pick_policy:
    kind: action
    framework: gr00t
    placement: remote
    endpoint: gpu-box
  place_policy:
    kind: action
    framework: lerobot
    placement: worker
  detector:
    kind: action
    framework: openvino
    path: exports/detector
    device: NPU
```

Skills refer to models by name, and framework code stays in plugins. Moving a model
between `worker` and `remote` requires only a configuration change.

### 5.6 Humanoid across partitions

```yaml
robot:
  driver: unitree_g1
  effectors: [whole_body, left_arm, right_arm, base, head]
skills:
  manipulate: { type: policy, model: bimanual, claims: [left_arm, right_arm] }
  walk_to: { type: delegated, driver: nav2, claims: [base] }
  look_at: { type: setpoint, claims: [head] }
partitions: # if omitted, everything runs on one host
  s0: { hosts: [tiers.s0] }
  s1: { hosts: [tiers.s1, skills, models.bimanual] }
  s2: { hosts: [models.planner, agent] }
```

```python
rt.skills.submit(Goal("look_at", {"target": "shelf"}))
rt.skills.submit(Goal("walk_to", {"target": "shelf"})).result()
rt.skills.submit(Goal("manipulate", {"task": "take the box"})).result()
```

Skills on disjoint effectors run concurrently under separate leases. The Python code is
the same whether or not the `partitions` section is present.

---

## 6. RobotRuntime and ControlLoop

```text
RobotRuntime              one per partition
 ├─ ControlLoop s0        if System 0 is hosted here
 ├─ ControlLoop s1        if System 1 is hosted here; its Robot is the local or remote System 0
 ├─ skills service        usually co-located with System 1; exposed as rt.skills
 ├─ models                in-process, worker, or remote
 └─ links                 connections to other partitions
```

The body of the current loop becomes `ControlLoop`. The existing constructor remains
for the single-loop case.

```python
class RobotRuntime:
    def __init__(self, robot, action_source, fps, ...): ...  # existing
    @classmethod
    def from_config(cls, config, *, partition: str | None = None) -> Self: ...
    skills: SkillsService
    world: WorldState
    loops: Mapping[str, ControlLoop]
```

`ControlLoop` extends the current tick in four ways. It attaches and detaches action
sources at tick boundaries according to leases. It applies deadline-bounded setpoints.
It holds effectors that have no writer. It applies the safety gate after all callbacks.

| Tier                      | Hosting                                                                   |
| ------------------------- | ------------------------------------------------------------------------- |
| Light System 0            | Inside the System 1 loop, as the interpolator does today                  |
| Heavy System 0            | A separate `ControlLoop` in a real-time process                           |
| Vendor or remote System 0 | Reached through the robot command API                                     |
| System 1                  | A `ControlLoop`                                                           |
| System 2                  | Event-driven: local in the same runtime, or remote through the skills API |

---

## 7. Actions and Effectors

An action becomes a mapping from effector to command. A flat `np.ndarray` remains valid
for single-effector sources, so `PolicySource` and `TeleopSource` are unchanged.

```python
{
    "left_arm": Command(mode=EE_POSE, values=..., frame="base"),
    "base": Command(mode=TWIST, values=...),
}
```

Robots may declare their supported command modes through an optional
`Robot.command_interfaces()`. By default, a robot has one effector that accepts joint
positions. Upper tiers drive System 0 through the same `Robot` protocol, implemented
locally by `LoopRobot` and remotely by `RemoteRobot`.

---

## 8. Goals and Skills

Skills live in the `physicalai.skills` package, which has no LLM dependency. Clients
access them through `rt.skills`.

```python
@dataclass(frozen=True)
class Goal:
    skill: str                          # "pick"
    params: Mapping[str, Any]           # {"object": "mug"}
    success: SuccessCheck | None = None # if None, the skill's default check applies

class Skill(Protocol):
    name: str
    description: str
    params_schema: type[Params]         # generates the tool, form, and CLI schemas
    claims: frozenset[Effector]

    def can_start(self, goal: Goal, world: WorldState) -> Precondition: ...
    def start(self, goal: Goal, ctx: SkillContext) -> SkillRun: ...
```

`Skill.start` returns a `SkillRun`, an internal object that the skills service drives.
`rt.skills.submit` wraps the run in a `SkillHandle` and returns that handle to the
client.

Skills fall into three types according to how they actuate the robot:

| Type          | Actuation                                                  | Examples                     |
| ------------- | ---------------------------------------------------------- | ---------------------------- |
| **Attached**  | Attaches an `ActionSource` to a loop                       | `PolicySkill`, teleoperation |
| **Setpoint**  | Writes one deadline-bounded setpoint per call              | `ServoSkill`, look-at        |
| **Delegated** | Hands off to an external controller and mirrors its status | Nav2, vendor locomotion      |

`PolicySkill` is generic. Pick and place are configured instances of it, not
subclasses:

```python
class PolicySkill:
    def start(self, goal, ctx):
        source = PolicySource(ctx.models.action(self.model), task=self.task.format(**goal.params))
        ctx.loops["s1"].attach(source, effectors=self.claims, lease=ctx.lease)
        return AttachedRun(source, success=goal.success or self.success)
```

**Leases.** Each effector has at most one writer. Effectors form a hierarchy, so a
lease on `whole_body` covers the arms and the base. A higher-priority goal may preempt
a lower-priority lease. If a lease misses its heartbeat, the skill is cancelled and the
effectors hold position.

**`SkillHandle`.** A handle exposes `status`, `feedback`, `update`, `cancel`, and
`result`.

**`WorldState`.** `WorldState` stores the latest value of each fact, with one writer per
key and no history. Loops and perception components write to it. Skills, success
checks, and agents read from it.

**Success checks.** A success check returns a confidence value rather than a boolean.
The following checks are illustrative:

```python
GripperClosedOnObject  # the gripper has closed on an object
ObjectAt               # a detector places the object in a target region
VLMJudge               # a ReasoningModel judges the outcome from images
```

---

## 9. Models and Backends

Roles such as planner, manipulator, and judge are bindings from a name to a model, not
separate base classes. Two model interfaces cover all roles:

```python
class ActionModel(Protocol):     # existing as InferenceModel
    def predict_action_chunk(self, observation) -> np.ndarray: ...
    def reset(self) -> None: ...

class ReasoningModel(Protocol):
    def complete(self, messages, *, tools=(), response_format=None) -> Completion: ...
```

**`ModelSpec`.** A `ModelSpec` declares a model's inputs, its outputs, and the
effectors and command modes its outputs drive. The runtime checks it at startup.
Plugins convert native formats, such as LeRobot features and GR00T modality files, into
a `ModelSpec`.

| Interface        | Backends                                                                                                               |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `ActionModel`    | OpenVINO and ONNX; LeRobot and GR00T through plugins                                                                   |
| `ReasoningModel` | OpenAI-compatible servers (SGLang, vLLM, llama.cpp, Ollama); Anthropic; Gemini; in-process OpenVINO GenAI or llama.cpp |

| Placement   | Intended use                                                       |
| ----------- | ------------------------------------------------------------------ |
| `inprocess` | OpenVINO or ONNX models and other small models                     |
| `worker`    | Models with conflicting dependencies, or that need crash isolation |
| `remote`    | Models that do not fit on the local host                           |

A `worker` is a local inference server that uses the same protocol as `remote`. Remote
inference is designed [[7]](#16-references) but not yet implemented. Existing
third-party policy servers can be wrapped as `ActionModel`s where useful.

Remote System 1 inference is viable only when the action-chunk horizon exceeds the sum
of network round-trip time, inference time, and a safety margin. If a chunk arrives
late, the loop holds and reports starvation to the skill. On a shared host, System 0
and System 2 should not share compute, and System 1 should take priority over System 2.
Jitter must be measured on the target platform.

---

## 10. APIs Across Partitions

### Robot command API (System 1 or 2 to System 0)

The robot command API describes the available effectors, accepts setpoints, and reports
robot state. Each setpoint carries an effector, a mode, values, a timestamp, a deadline,
and a lease. System 0 rejects setpoints that have expired or carry the wrong lease. When
setpoints become stale, it holds, decays, or stops. System 0 enforces limits regardless
of the caller. The API can be implemented without Python.

### Inference API (control loops to action models)

The inference API describes a model, streams observations in and action chunks out, and
supports reset and health checks. Both sides timestamp their messages. The API builds on
the existing policy-server design [[7]](#16-references).

### Skills API (agents to the skills service)

The skills API lists skills, submits goals, streams updates, and supports update,
cancel, and heartbeat calls. It also exposes `WorldState` observations, events, and an
emergency stop. Missed heartbeats expire the caller's leases. The MCP interface
[[8]](#16-references) is generated from the skill registry.

---

## 11. Deployment

| Layout                         | Distinguishing feature                                                     |
| ------------------------------ | -------------------------------------------------------------------------- |
| One process                    | System 1 only; System 0 is the interpolator                                |
| One host, several accelerators | System 0 in its own process; models assigned to devices                    |
| Robot, GPU server, and cloud   | Large action models run `remote`; reasoning models run on a hosted service |
| KVM guests                     | One partition per tier; the System 2 guest has no device access            |
| Vendor System 0                | The vendor implements the robot command API                                |

---

## 12. Safety, Time, and Recording

Safety is layered. Each layer is enforced independently of the layers above it.

| Layer                | Enforces                                                    |
| -------------------- | ----------------------------------------------------------- |
| Hardware / firmware  | Power, torque, and temperature limits                       |
| Safety gate          | Joint, velocity, workspace, and force limits                |
| Leases and deadlines | A single writer per effector; stale commands lead to a hold |
| Access policy        | Which agents may run which skills                           |
| Partition boundary   | The System 2 guest has no device access                     |

The `unrestricted` access mode removes permission checks only. Safety gates, leases,
and deadlines remain in force.

| Failure              | Response                                                  |
| -------------------- | --------------------------------------------------------- |
| Agent disconnects    | Its lease expires; the skill is cancelled; effectors hold |
| System 2 crashes     | As above; System 1 and System 0 continue                  |
| Remote model is late | The loop holds and reports starvation; the skill may fail |
| System 1 goes silent | System 0 applies its stale-command fallback               |
| System 0 faults      | System 1 stops; running skills fail                       |

**Time.** All partitions share a clock. Deadlines are absolute, and `WorldState`
timestamps allow readers to detect stale facts.

**Recording.** Each skill run produces one episode. The episode contains the loop data
in LeRobot format, the goal, the submitting agent, the outcome, model provenance,
events, and the agent transcript when one exists. Failed and cancelled runs are
recorded in the same way as successful ones.

---

## 13. Deferred Decisions

| Topic                                  | Needed by                     |
| -------------------------------------- | ----------------------------- |
| Command modes and payloads             | `ControlLoop` and actions     |
| Configuration schema                   | Multi-partition deployment    |
| Wire formats for the three APIs        | Remote and VM deployment      |
| Lease durations and priorities         | Skills service                |
| Access-policy format                   | Multiple agents               |
| Cross-partition transports             | Multi-host and KVM deployment |
| Real-time platform for System 0        | Hosted heavy System 0         |
| Clock synchronization                  | Multi-partition deployment    |
| Recording format extensions            | Data collection at scale      |
| Package location of the built-in agent | System 2 clients              |
| World-model interface                  | Imagine-and-rank planning     |

---

## 14. Implementation Phases

| Phase | Scope                                                       | Exit criterion                                    |
| ----- | ----------------------------------------------------------- | ------------------------------------------------- |
| 1     | Extract `ControlLoop`                                       | Existing tests pass                               |
| 2     | Effector actions, attach and detach, deadlines, safety gate | Existing robots unchanged; gate tests pass        |
| 3     | Inference API, placement, `ModelSpec`                       | Identical actions across placements               |
| 4     | Skills service, leases, handles, recording; scripted agent  | Tabletop task with models from several frameworks |
| 5     | Reasoning clients, skills API, MCP, built-in agent          | Cloud agent and MCP host drive the same robot     |
| 6     | Setpoint skills; robot command API across processes         | Agent drives System 0; watchdog holds on silence  |
| 7     | Hosted System 0; VM partitions                              | Humanoid or mobile robot runs across guests       |
| 8     | Delegated navigation; world model                           | Navigate, pick, and place in one task             |

Phases 1 to 4 deliver value without the later phases.

---

## 15. Decisions

```text
story           Agent → Goal → Skill → SkillHandle; RobotRuntime runs the loops
tiers           System 0 control, System 1 policy, System 2 deliberation
runtime         one RobotRuntime per partition; one ControlLoop per hosted tier
tier link       each tier is a Robot to the tier above
actions         effector-keyed command maps; flat arrays remain valid
skills          physicalai.skills, accessed as rt.skills; attached, setpoint, delegated
concurrency     hierarchical effector leases
models          ActionModel and ReasoningModel; roles are bindings
interop         ModelSpec checked at startup; plugins convert native formats
placement       inprocess, worker, remote
APIs            robot command API, inference API, skills API
safety          safety gate, leases, deadlines, access policy
data            one labeled episode per skill run
terminology     robot skill (Skill) is distinct from agent skill (SKILL.md)
```

---

## 16. References

1. Ahn, M. et al. "Do As I Can, Not As I Say: Grounding Language in Robotic Affordances." 2022. [arXiv:2204.01691](https://arxiv.org/abs/2204.01691)
2. Shi, L. X. et al. "Hi Robot: Open-Ended Instruction Following with Hierarchical
   Vision-Language-Action Models." 2025. [arXiv:2502.19417](https://arxiv.org/abs/2502.19417)
3. NVIDIA. "GR00T N1: An Open Foundation Model for Generalist Humanoid Robots." 2025.
   [arXiv:2503.14734](https://arxiv.org/abs/2503.14734)
4. Figure AI. "Helix: A Vision-Language-Action Model for Generalist Humanoid Control." 2025. [figure.ai/news/helix](https://www.figure.ai/news/helix)
5. Kahneman, D. _Thinking, Fast and Slow._ Farrar, Straus and Giroux, 2011.
6. Black, K. et al. "Real-Time Execution of Action Chunking Flow Policies." 2025.
   [arXiv:2506.07339](https://arxiv.org/abs/2506.07339)
7. [Policy server design](policy_server_design.md) (this repository).
8. Model Context Protocol. [modelcontextprotocol.io](https://modelcontextprotocol.io)
