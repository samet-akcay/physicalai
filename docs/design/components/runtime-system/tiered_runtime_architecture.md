# Tiered Robot Runtime: System 0 / 1 / 2

**Status:** Proposal for design review.

This document describes how PhysicalAI runs three tiers of robot decision-making: System 0
(control), System 1 (reactive policy), and System 2 (deliberation). Models may come from
any backend, and the tiers may share one process or be spread across accelerators, KVM
guests, or separate machines.

The document specifies architecture only. Wire formats, transports, and numeric limits
are deferred ([§13](#13-deferred-decisions)). Code is pseudo-code unless marked
**existing**.

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
monitoring and cancellation. `RobotRuntime` continues to run the control loop.

| Tier         | Role                               | Rate                   | Examples                               |
| ------------ | ---------------------------------- | ---------------------- | -------------------------------------- |
| **System 2** | Plan, sequence, and verify         | Event-driven, 0.1–5 Hz | Cloud or local LLMs and VLMs           |
| **System 1** | Map observations to motion         | 10–50 Hz               | VLA, ACT, diffusion, navigation        |
| **System 0** | Track setpoints and enforce limits | 200 Hz – 1 kHz         | WBC, IK, impedance, vendor controllers |

The same model scales to larger systems without changing this seam:

- Every new capability is a `RobotRuntime` constructor argument. YAML and the CLI parse
  the same signature through `from_config`, as they do today.
- One `RobotRuntime` runs per partition (a process, VM, or machine). Tiers compose
  through the existing `robot` argument: the System 0 below a runtime is a `Robot`,
  whether it is hardware, a local loop, or another partition.
- Skills claim effectors under leases, so work on disjoint parts of the robot can run
  concurrently.
- Models may come from any framework and run in one of three placements: `inprocess`,
  `worker`, or `remote`.
- Partitions communicate through three APIs: the robot command API, the inference API,
  and the skills API.

---

## 2. Motivation

Recent robot systems separate slow reasoning from fast control. A language or
vision-language model plans and verifies, a learned policy produces motion, and a
high-rate controller tracks it. The System 1 and System 2 labels follow the dual-process
terminology common in this literature; System 0 denotes the control layer beneath them.

These tiers place different demands on the platform. Their models often do not fit in
the same memory. They need different operating-system and real-time guarantees. Some
robot vendors run them in separate KVM guests on a single SoC.

**Current state.** PhysicalAI provides a single fixed-rate loop, synchronous,
asynchronous, and real-time-chunking policy execution, a configuration system in which
YAML and the CLI mirror constructor signatures, and framework plugins with isolated
environments (in progress).

**Requirements.** The runtime must:

1. Run decision-makers at different rates.
2. Turn language goals into robot actions.
3. Deploy tiers across partitions.
4. Enforce safety that agent-originated commands cannot bypass.
5. Record labeled data from every run.
6. Keep existing single-policy scripts and configurations working unchanged.
7. Keep Python construction, YAML, and the CLI equivalent.
8. Keep framework dependencies inside plugins.

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

| Term                            | Definition                                                                    |
| ------------------------------- | ----------------------------------------------------------------------------- |
| **Tier**                        | System 0, 1, or 2. A responsibility and rate band, not a process.             |
| **`ControlLoop`**               | The fixed-rate loop body that each `RobotRuntime` runs.                       |
| **`LoopRobot` / `RemoteRobot`** | Present a System 0 loop, local or in another partition, as a `Robot`.         |
| **Effector**                    | A named part of the robot, such as `arm`, `gripper`, `base`, or `whole_body`. |
| **`ActionSource`**              | **Existing.** A per-tick action producer, such as a policy or teleoperation.  |
| **`WorldState`**                | The latest timestamped facts: poses, detections, and captions.                |
| **`ActionModel`**               | Maps observations to action chunks. **Existing** as `InferenceModel`.         |
| **`ReasoningModel`**            | Maps messages, images, and tool definitions to text or tool calls.            |
| **`ModelRef`**                  | A named, lazily loaded model: framework, path or endpoint, and placement.     |
| **`ModelSpec`**                 | Declares the inputs a model expects and the outputs it produces.              |

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

The architecture follows seven rules:

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
7. The constructor is the API. `from_config` and the CLI parse the same signature, so no
   capability exists only in configuration.

The existing call remains valid. New capabilities are optional constructor arguments:

```python
# existing: unchanged
RobotRuntime(robot=so101, action_source=PolicySource(model), fps=30).run()

# new: skills through the same constructor
with RobotRuntime(robot=so101, fps=30, models=models, skills=skills) as rt:
    rt.start()  # runs the loop in the background; run() still blocks
    rt.skills.submit(Goal("pick", {"object": "mug"})).result()

# the same runtime from YAML holding the same constructor arguments
rt = RobotRuntime.from_config("tabletop.yaml")

# remote client with the same .skills and .world surface
rt = RuntimeClient("robot.local")
```

```bash
physicalai run --config tabletop.yaml                    # one runtime
physicalai run --config tabletop.yaml --runtime.fps=50   # override any constructor argument
physicalai up deployment.yaml                            # one runtime per partition
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
rt = RobotRuntime(
    robot=SO101(port="/dev/ttyACM0"),
    fps=30,
    models={
        "pick_policy": ModelRef(kind="action", path="./exports/pick"),
        "place_policy": ModelRef(kind="action", path="./exports/place"),
    },
    skills=[
        PolicySkill(name="pick", model="pick_policy", claims={"arm", "gripper"},
                    task="pick up the {object}", success=GripperClosedOnObject()),
        PolicySkill(name="place", model="place_policy", claims={"arm", "gripper"},
                    task="place it in the {location}", success=ObjectAt()),
    ],
)

with rt:
    rt.start()
    for obj, loc in [("red block", "left bin"), ("blue block", "right bin")]:
        if rt.skills.submit(Goal("pick", {"object": obj})).result().status is Status.SUCCEEDED:
            rt.skills.submit(Goal("place", {"location": loc})).result()
```

For each goal, the skills service checks the access policy, grants a lease on the
claimed effectors, attaches a `PolicySource` to the loop, and evaluates the success
check. It then detaches the source and records the episode.

The same runtime can be written as YAML and loaded with `from_config`:

```yaml
# tabletop.yaml
runtime:
  robot:
    class_path: physicalai.robot.so101.SO101
    init_args:
      port: /dev/ttyACM0
  fps: 30
  models:
    pick_policy:
      kind: action
      path: ./exports/pick
    place_policy:
      kind: action
      path: ./exports/place
  skills:
    - class_path: physicalai.skills.PolicySkill
      init_args:
        name: pick
        model: pick_policy
        claims: [arm, gripper]
        task: pick up the {object}
        success:
          class_path: physicalai.skills.checks.GripperClosedOnObject
    # place is analogous
```

### 5.3 Built-in agent or MCP host

The built-in agent is one reference implementation of the agent role:

```python
ToolLoopAgent(load_reasoning_model("anthropic", model="claude-…"), rt.skills).run(
    "Put red blocks in the left bin and blue blocks in the right bin."
)
```

An external MCP host can drive the same skills. The runtime exposes the skills API
through the `serve` argument:

```python
RobotRuntime(..., skills=skills, serve=[SkillsServer(port=7001), McpServer()]).run()
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
runtime:
  # robot, fps, and skills as in 5.2
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
between `worker` and `remote` changes one field.

### 5.6 Humanoid across partitions

Each partition is a separate `RobotRuntime` built through the same constructor. Tiers
connect through the `robot` argument:

```python
# s0 partition (real-time guest): remote setpoints are its action source
RobotRuntime(
    robot=G1(),
    action_source=CommandServer(port=7000),
    fps=500,
    callbacks=[SafetyGate(limits)],
).run()

# s1 partition: System 0 is a RemoteRobot
RobotRuntime(
    robot=RemoteRobot("s0.local:7000"),
    fps=50,
    models={"bimanual": ModelRef(kind="action", framework="gr00t", path="./exports/bimanual")},
    skills=[
        PolicySkill(name="manipulate", model="bimanual", claims={"left_arm", "right_arm"}),
        DelegatedSkill(name="walk_to", driver=Nav2(), claims={"base"}),
        SetpointSkill(name="look_at", claims={"head"}),
    ],
    serve=[SkillsServer(port=7001)],
).run()

# s2 partition: an agent connected to the skills API
ToolLoopAgent(planner, RuntimeClient("s1.local:7001").skills).run("Take the box from the shelf.")
```

On a single host, the s1 runtime takes `robot=LoopRobot(G1(), fps=500,
callbacks=[SafetyGate(limits)])` instead of `RemoteRobot`, and nothing else changes.
Each partition can equally be a YAML file. `physicalai up` reads a deployment file
that assigns those files to hosts:

```yaml
# deployment.yaml
partitions:
  s0: { host: rt-guest, config: s0.yaml }
  s1: { host: robot-main, config: s1.yaml }
  s2: { host: cloud, config: s2.yaml }
```

Skills on disjoint effectors run concurrently under separate leases:

```python
rt.skills.submit(Goal("look_at", {"target": "shelf"}))
rt.skills.submit(Goal("walk_to", {"target": "shelf"})).result()
rt.skills.submit(Goal("manipulate", {"task": "take the box"})).result()
```

---

## 6. RobotRuntime and ControlLoop

`RobotRuntime` is configured entirely through its constructor. `from_config` parses a
YAML file against that signature with jsonargparse, as it does today, so Python, YAML,
the CLI, and Studio describe a runtime in the same shape. Existing calls remain valid:
existing parameters keep their order, and every new parameter is optional.

```python
class RobotRuntime:
    def __init__(
        self,
        robot: Robot,                                  # existing; hardware, LoopRobot, or RemoteRobot
        action_source: ActionSource | None = None,     # existing; None when skills drive the loop
        fps: float = 30.0,                             # existing
        cameras: Mapping[str, Camera] | None = None,   # existing
        callbacks: Sequence[Callback] = (),            # existing
        interpolator: ActionInterpolator | None = None,  # existing
        models: Mapping[str, ModelRef] | None = None,  # new: named models for skills
        skills: Sequence[Skill] = (),                  # new: registered at construction
        serve: Sequence[Server] = (),                  # new: expose the skills API or MCP
    ): ...

    @classmethod
    def from_config(cls, config: str | Path) -> Self: ...  # existing: jsonargparse over __init__
    def run(self, duration_s: float | None = None) -> None: ...  # existing: blocks
    def start(self) -> None: ...                           # new: runs the loop in the background

    skills: SkillsService
    world: WorldState
```

```text
RobotRuntime            one per partition
 ├─ ControlLoop         its own loop: System 1, or System 0 in an s0 partition
 ├─ robot               hardware, LoopRobot (local System 0), or RemoteRobot
 ├─ skills service      rt.skills; present when skills are configured
 ├─ models              named ModelRefs, loaded lazily
 └─ servers             skills API or MCP, when exposed
```

The body of the current loop becomes `ControlLoop`. It extends the current tick in four
ways. It attaches and detaches action sources at tick boundaries according to leases.
It applies deadline-bounded setpoints. It holds effectors that have no writer. It
applies the safety gate after all callbacks.

Because each tier is a `Robot` to the tier above, tiers compose by construction rather
than through a separate partition mechanism:

| Tier                    | Hosting                                                                |
| ----------------------- | ---------------------------------------------------------------------- |
| Light System 0          | The interpolator inside the runtime's loop (existing)                  |
| Heavy System 0          | `robot=LoopRobot(...)`: a second loop in its own thread or process     |
| System 0 in a partition | `robot=RemoteRobot(...)`; that partition runs a `CommandServer` source |
| Vendor System 0         | A vendor `Robot` driver, or `RemoteRobot` to the vendor's API          |
| System 1                | The runtime's own `ControlLoop`                                        |
| System 2                | An in-process client of `rt.skills`, or `RuntimeClient` remotely       |

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

Skills live in the `physicalai.skills` package, which has no LLM dependency. They are
passed to the constructor through `skills` or added later with `rt.skills.register`.

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
| **Attached**  | Attaches an `ActionSource` to the loop                     | `PolicySkill`, teleoperation |
| **Setpoint**  | Writes one deadline-bounded setpoint per call              | `ServoSkill`, look-at        |
| **Delegated** | Hands off to an external controller and mirrors its status | Nav2, vendor locomotion      |

`PolicySkill` is generic. Pick and place are configured instances of it, not
subclasses:

```python
class PolicySkill:
    def start(self, goal, ctx):
        source = PolicySource(ctx.models.action(self.model), task=self.task.format(**goal.params))
        ctx.loop.attach(source, effectors=self.claims, lease=ctx.lease)
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

Roles such as planner, manipulator, and judge are bindings from a name in `models` to a
model, not separate base classes. Each entry is a `ModelRef`, which the runtime loads
when it connects. Two model interfaces cover all roles:

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
inference is specified in the [policy server design](policy_server_design.md) but not
yet implemented. Existing third-party policy servers can be wrapped as `ActionModel`s
where useful.

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
of the caller.

`RemoteRobot` is the client side. A Python System 0 partition serves the API with a
`CommandServer` action source. A native or vendor controller can implement the API
directly when System 0 must run beyond Python's real-time limits.

### Inference API (control loops to action models)

The inference API describes a model, streams observations in and action chunks out, and
supports reset and health checks. Both sides timestamp their messages. The API builds on
the existing [policy server design](policy_server_design.md).

### Skills API (agents to the skills service)

The skills API lists skills, submits goals, streams updates, and supports update,
cancel, and heartbeat calls. It also exposes `WorldState` observations, events, and an
emergency stop. Missed heartbeats expire the caller's leases. `SkillsServer` serves the
API and `RuntimeClient` consumes it. The MCP interface is generated from the skill
registry.

---

## 11. Deployment

Every layout uses the same constructor. Layouts differ in the `robot` argument, model
placement, and the number of runtimes.

| Layout                         | Configuration                                                                                               |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| One process                    | `robot` is hardware; System 0 is the interpolator                                                           |
| One host, several accelerators | `robot=LoopRobot(...)`; models assigned to devices                                                          |
| Robot, GPU server, and cloud   | Large action models use `remote`; reasoning models use a hosted service                                     |
| KVM guests                     | One runtime per guest, linked by `RemoteRobot` and `RuntimeClient`; the System 2 guest has no device access |
| Vendor System 0                | `robot` is the vendor driver, or `RemoteRobot` to the vendor's API                                          |

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
| `ModelRef` fields                      | Skills service                |
| Deployment file format                 | `physicalai up`               |
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

| Phase | Scope                                                                       | Exit criterion                                   |
| ----- | --------------------------------------------------------------------------- | ------------------------------------------------ |
| 1     | Extract `ControlLoop`; make `action_source` optional                        | Existing tests and configurations pass           |
| 2     | Effector actions, attach and detach, deadlines, safety gate                 | Existing robots unchanged; gate tests pass       |
| 3     | Inference API, placement, `ModelSpec`, `ModelRef`                           | Identical actions across placements              |
| 4     | Skills service, `models` and `skills` arguments, leases, handles, recording | Tabletop task from Python and from YAML          |
| 5     | Reasoning clients, `SkillsServer`, `RuntimeClient`, MCP, built-in agent     | Cloud agent and MCP host drive the same robot    |
| 6     | Setpoint skills; `CommandServer` and `RemoteRobot`                          | Agent drives System 0; watchdog holds on silence |
| 7     | `LoopRobot`; VM partitions; `physicalai up`                                 | Humanoid or mobile robot runs across guests      |
| 8     | Delegated navigation; world model                                           | Navigate, pick, and place in one task            |

Phases 1 to 4 deliver value without the later phases.

---

## 15. Decisions

```text
story           Agent → Goal → Skill → SkillHandle; RobotRuntime runs the loop
tiers           System 0 control, System 1 policy, System 2 deliberation
construction    the constructor is the API; from_config and the CLI parse the same signature
runtime         one RobotRuntime per partition; each runs one ControlLoop
tier link       each tier is a Robot to the tier above (LoopRobot, RemoteRobot, vendor driver)
actions         effector-keyed command maps; flat arrays remain valid
skills          physicalai.skills, accessed as rt.skills; attached, setpoint, delegated
concurrency     hierarchical effector leases
models          ActionModel and ReasoningModel; roles are named ModelRefs
interop         ModelSpec checked at startup; plugins convert native formats
placement       inprocess, worker, remote
APIs            robot command API, inference API, skills API
safety          safety gate, leases, deadlines, access policy
data            one labeled episode per skill run
terminology     robot skill (Skill) is distinct from agent skill (SKILL.md)
```
