# Tiered Robot Runtime: System 0 / 1 / 2

**Status:** Proposal for design review.

This document describes how PhysicalAI runs three tiers of robot decision-making: System 0
(control), System 1 (reactive policy), and System 2 (deliberation). Models may come from
any backend, and the tiers may share one process or be spread across accelerators, KVM
guests, or separate machines. The design is hardware-agnostic and does not depend on any
external middleware.

The document specifies architecture only. Message schemas, addressing, and numeric
limits are deferred ([§16](#16-open-questions-and-deferred-decisions)). Code is pseudo-code
unless marked **existing**.

---

## Contents

1. [Summary](#1-summary)
2. [Motivation](#2-motivation)
3. [Terminology](#3-terminology)
4. [Architecture](#4-architecture)
5. [Examples](#5-examples)
6. [ControlLoop](#6-controlloop)
7. [RobotRuntime](#7-robotruntime)
8. [Actions and Parts](#8-actions-and-parts)
9. [Goals and Skills](#9-goals-and-skills)
10. [Models and Backends](#10-models-and-backends)
11. [Links and Placement](#11-links-and-placement)
12. [Safety, Time, and Recording](#12-safety-time-and-recording)
13. [Integrations](#13-integrations)
14. [Migration](#14-migration)
15. [Requirements Coverage](#15-requirements-coverage)
16. [Open Questions and Deferred Decisions](#16-open-questions-and-deferred-decisions)
17. [Implementation Phases](#17-implementation-phases)
18. [Decisions](#18-decisions)

---

## 1. Summary

The design separates two responsibilities:

- A **`ControlLoop`** runs one tier at a fixed rate. On each tick it reads its robot, asks
  its action source for an action, passes the action through its callbacks, and sends
  it. This is the class that ships today as `RobotRuntime`, renamed
  ([§14](#14-migration)).
- A **`RobotRuntime`** runs a whole robot. It hosts the control loops and the other
  components, decides where each one runs, connects them, starts and stops them in
  order, and exposes them to the outside.

```python
# one tier: unchanged behavior, new name
ControlLoop(robot=so101, action_source=PolicySource(model), fps=30).run()

# a whole robot
RobotRuntime(
    loops={"s0": s0, "s1": s1},
    nodes={"planner": Agent(planner_model, skills)},
    placement={"s0": "rt-guest", "s1": "robot-main", "planner": "cloud"},  # omit to run locally
).run()
```

Decisions flow down through three tiers:

```text
Agent ──goals──► Skills ──drives──► ControlLoop s1 ──robot──► ControlLoop s0 ──robot──► SafetyGate ──► hardware
```

| Tier         | Role                               | Rate                   | Examples                               |
| ------------ | ---------------------------------- | ---------------------- | -------------------------------------- |
| **System 2** | Plan, sequence, and verify         | Event-driven, 0.1–5 Hz | Cloud or local LLMs and VLMs           |
| **System 1** | Map observations to motion         | 10–50 Hz               | VLA, ACT, diffusion, navigation        |
| **System 0** | Track setpoints and enforce limits | 200 Hz – 1 kHz         | WBC, IK, impedance, vendor controllers |

Components never know where they run. The runtime chooses the link between any two
components from their placement: in memory when they share a process, and over a
transport otherwise. The same robot description therefore runs in one process, across
accelerators, across KVM guests, or across machines.

---

## 2. Motivation

Recent robot systems separate slow reasoning from fast control. A language or
vision-language model plans and verifies, a learned policy produces motion, and a
high-rate controller tracks it. The System 1 and System 2 labels follow the dual-process
terminology common in this literature; System 0 denotes the control layer beneath them.

These tiers place different demands on the platform. Their models often do not fit in
the same memory. They need different operating-system and real-time guarantees. Some
robot vendors run them in separate KVM guests on a single SoC.

**Current state.** PhysicalAI provides a single fixed-rate loop with pluggable action
sources (`PolicySource`, `TeleopSource`) and callbacks; synchronous, asynchronous, and
real-time-chunking policy execution; a configuration system in which YAML and the CLI
mirror constructor signatures; Zenoh-based transports for shared cameras, shared robots
(`SharedRobot`), and remote inference (`RemoteInferenceModel` and `InferenceServer`); and
framework plugins with isolated environments (in progress).

**Requirements.** The runtime must:

1. Run System 0, 1, and 2 on one robot.
2. Let a frontier model drive System 1 or System 0.
3. Run the tiers in one process, across accelerators on one host, in separate KVM guests,
   or on separate machines, from one description of the robot.
4. Accept models from any backend, on any hardware.
5. Let independent workloads use disjoint parts of the robot concurrently.
6. Support teleoperation, human-in-the-loop intervention, and DAgger.
7. Enforce safety that no action source, callback, client, or network peer can bypass.
8. Record a labeled episode for every run.
9. Keep Python construction, YAML, and the CLI equivalent.
10. Integrate with ROS 2 and other ecosystems without depending on them.
11. Migrate existing code without an abrupt break.

**Out of scope.** Hypervisors, hard real-time control in Python (loops at 200 Hz and
above remain native or vendor-provided), agent frameworks, general-purpose middleware,
and training.

---

## 3. Terminology

Prose uses lowercase for concepts (client, goal, skill). Code font denotes classes.
Tiers are written System 0, 1, and 2 in prose and `s0`, `s1`, and `s2` in code and
configuration. "Skill" means robot skill unless qualified otherwise.

**Structure**

| Term               | Definition                                                                                     |
| ------------------ | ---------------------------------------------------------------------------------------------- |
| **Tier**           | System 0, 1, or 2. A responsibility and rate band, not a process.                              |
| **`ControlLoop`**  | Runs one tier at a fixed rate: read the robot, query the action source, apply callbacks, send. |
| **`RobotRuntime`** | Runs a whole robot: hosts loops and nodes, places and links them, and manages their lifecycle. |
| **Node**           | A component hosted by the runtime that is not a control loop, such as an agent or a perceiver. |
| **Partition**      | A unit of deployment: a process, container, VM, or machine.                                    |
| **Placement**      | The assignment of loops and nodes to partitions.                                               |
| **Link**           | A connection between two components, implemented in memory or over a transport.                |

**Goals and skills**

| Term              | Definition                                                                                                                |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------- |
| **Client**        | Anything that submits goals: an agent, a script, Studio, a ROS 2 application, or a human.                                 |
| **Agent**         | A client that uses a reasoning model to choose goals. The built-in `Agent` class is one; an external MCP host is another. |
| **Goal**          | One request: a skill name, its parameters, and an optional success check.                                                 |
| **Skill**         | A robot capability such as `pick`, `servo`, or `navigate_to`. Implements the `Skill` protocol.                            |
| **`Skills`**      | The action source that runs skills: it accepts goals, grants leases, and merges the actions of running skills.            |
| **`SkillHandle`** | The client's view of one running skill: status, feedback, update, cancel, and result.                                     |
| **Part**          | A named, commandable part of the robot, such as `arm`, `gripper`, `base`, or `whole_body`. Parts nest.                    |
| **Lease**         | Exclusive, time-limited write access to a set of parts, renewed by heartbeat.                                             |

**Components**

| Term                 | Definition                                                                                |
| -------------------- | ----------------------------------------------------------------------------------------- |
| **`Robot`**          | **Existing.** The protocol for anything that accepts commands and reports state.          |
| **`ActionSource`**   | **Existing.** The protocol for anything that produces an action on each tick.             |
| **Callback**         | **Existing.** A hook that observes, and may transform, each action inside a loop.         |
| **`ActionModel`**    | **Existing** as `InferenceModel`. Maps observations to action chunks.                     |
| **`ReasoningModel`** | Maps messages, images, and tool definitions to text or tool calls.                        |
| **`ModelSpec`**      | Declares the inputs a model expects and the outputs it produces.                          |
| **`SafetyGate`**     | A `Robot` wrapper that enforces hard limits on every command before it reaches the robot. |
| **`WorldState`**     | The latest timestamped facts: poses, detections, and captions.                            |
| **Agent skill**      | A coding-assistant `SKILL.md` file. Unrelated to robot skills.                            |

---

## 4. Architecture

```text
                 RobotRuntime: placement, links, endpoints, lifecycle, health
┌──────────────────────────────────────────────────────────────────────────────┐
│ Clients: Agent │ MCP host │ ROS 2 app │ script │ Studio │ human               │
│    │  goals                                                                  │
│    ▼                                                                         │
│ Skills (action source of s1): leases, WorldState, access policy, events      │
│    ▼                                                                         │
│ ControlLoop s1   10–50 Hz       models through ActionModel                   │
│    │  robot = ControlLoop s0                                                 │
│    ▼                                                                         │
│ ControlLoop s0   200 Hz+        robot = SafetyGate(hardware or vendor)       │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Rules

1. **Components are location-unaware.** Drivers, action sources, skills, models, and
   agents are plain objects. None of them chooses a transport.
2. **A `ControlLoop` runs one tier at one rate.** It holds no knowledge of other tiers.
3. **The tier below a loop is its robot.** That robot is hardware, a vendor controller,
   or another `ControlLoop`.
4. **The `RobotRuntime` owns everything that spans components:** placement, links,
   external endpoints, lifecycle order, health, and emergency stop.
5. **One description per robot.** Only placement changes between deployments.
6. **Callbacks observe and transform actions.** They do not decide which action to send.
7. **`SafetyGate` wraps the hardware,** so every command passes through it regardless of
   its origin.
8. **Every write is bounded by a lease and a deadline.** A missed heartbeat or deadline
   causes a hold or a stop.
9. **Every client goes through `Skills`.** There is no privileged path.
10. **Integrations sit at the edges and implement these protocols.** The core never
    depends on them.

### Execution model

Internally, the runtime treats the robot as a small graph. Each loop and node is a
vertex with a trigger: periodic (control loops and perceivers at their rate) or
event-driven (agents and servers). Each edge is a typed link: `Robot`, `ActionSource`,
`ActionModel`, or the skills API. Placement determines how each edge is implemented
([§11](#11-links-and-placement)). Users do not build this graph explicitly; it follows
from the object references in the robot description.

---

## 5. Examples

### 5.1 One policy

```python
ControlLoop(
    robot=SO101(port="/dev/ttyACM0"),
    action_source=PolicySource(InferenceModel("./exports/act"), execution=AsyncExecution()),
    fps=30,
).run(duration_s=60)
```

A single tier needs only a `ControlLoop`. This is the existing behavior under the new
name.

### 5.2 Teleoperation

```python
ControlLoop(
    robot=SO101(port="/dev/ttyACM0"),
    action_source=TeleopSource(leader=SO101(port="/dev/ttyACM1", role="leader")),
    fps=30,
    callbacks=[recorder],
).run()
```

### 5.3 Scripted client

```python
skills = Skills([
    PolicySkill(name="pick", model=InferenceModel("./exports/pick"), claims={"arm", "gripper"},
                task="pick up the {object}", success=GripperClosedOnObject()),
    PolicySkill(name="place", model=InferenceModel("./exports/place"), claims={"arm", "gripper"},
                task="place it in the {location}", success=ObjectAt()),
])

with ControlLoop(robot=SO101(port="/dev/ttyACM0"), action_source=skills, fps=30) as loop:
    loop.start()  # runs the loop in the background
    for obj, loc in [("red block", "left bin"), ("blue block", "right bin")]:
        if skills.submit(Goal("pick", {"object": obj})).result().status is Status.SUCCEEDED:
            skills.submit(Goal("place", {"location": loc})).result()
```

For each goal, `Skills` checks the access policy, grants a lease on the claimed parts,
attaches a `PolicySource`, and evaluates the success check. It then detaches the source
and emits the events that delimit the recorded episode.

### 5.4 Agent or MCP host

The built-in `Agent` sends the instruction to a reasoning model with each skill offered
as a tool. It submits a goal for each tool call and returns the result to the model,
until the model reports that the task is done:

```python
Agent(OpenAICompatibleModel(base_url="http://localhost:30000/v1"), skills).run(
    "Put red blocks in the left bin and blue blocks in the right bin."
)
```

To let an external MCP host, Studio, or a ROS 2 application drive the same skills, the
runtime exposes them through endpoints:

```python
RobotRuntime(
    loops={"s1": ControlLoop(robot=SO101(port="/dev/ttyACM0"), action_source=skills, fps=30)},
    serve=[SkillsServer(skills, port=7001), McpServer(skills)],
).run()
```

The MCP tool list is generated from the skill list. If a client disconnects, its lease
expires and the claimed parts hold position.

### 5.5 Human-in-the-loop and DAgger

Intervention is preemption. A teleoperation skill claims the same parts as the policy
skill at a higher priority:

```python
skills = Skills([
    PolicySkill(name="pick", model=..., claims={"arm", "gripper"}),
    TeleopSkill(name="takeover", leader=leader, claims={"arm", "gripper"}, priority=Priority.HUMAN),
])
pedal.on_press(lambda: skills.submit(Goal("takeover", {})))
```

When the operator presses the pedal, the takeover goal preempts the policy's lease.
When the operator releases it, the takeover ends and the policy skill resumes or is
resubmitted. The recorded episode marks the intervention segment, which provides the
labels DAgger requires.

### 5.6 Agent driving System 0

```python
skills = Skills([ServoSkill(name="servo", claims={"arm"}, max_step_m=0.01)])
# The agent observes, submits Goal("servo", {"dx": 0.01}), and repeats until done.
```

Each call writes a setpoint with a deadline. `SafetyGate` clamps it, and the loop holds
the arm if calls stop arriving. Without a System 1 policy, `Skills` can be the action
source of the System 0 loop directly.

### 5.7 Mixed backends

Each skill owns its model. A model runs in-process, is served by a server the runtime
does not manage, or is named in the description so that the runtime places it:

```python
inspect_model = InferenceModel("./exports/inspect", device="NPU")       # in-process (existing)
pick_model = RemoteInferenceModel(name="gr00t-pick", host="gpu-box")    # served externally (existing)
place_model = InferenceModel("./exports/place")                         # placed by the runtime

skills = Skills([
    PolicySkill(name="inspect", model=inspect_model, ...),
    PolicySkill(name="pick", model=pick_model, ...),
    PolicySkill(name="place", model=place_model, ...),
])

RobotRuntime(
    loops={"s1": ControlLoop(robot=so101, action_source=skills, fps=30)},
    nodes={
        "place_model": place_model,
        "planner": Agent(OpenAICompatibleModel(base_url="http://localhost:30000/v1"), skills),
    },
    placement={"place_model": "lerobot-env"},
)
```

`place_model` is placed in the `lerobot-env` partition, a process in a separate Python
environment prepared by `physicalai.isolate`. The runtime serves it there through
`InferenceServer` and gives the `place` skill a `RemoteInferenceModel` for it. Removing
the placement entry runs the model in-process instead. Framework code stays in plugins
in every case.

### 5.8 Humanoid, one description for every deployment

```python
s0 = ControlLoop(robot=SafetyGate(G1(), limits), fps=500)
bimanual = InferenceModel("./exports/bimanual")
skills = Skills([
    PolicySkill(name="manipulate", model=bimanual, claims={"left_arm", "right_arm"}),
    DelegatedSkill(name="walk_to", controller=Nav2(), claims={"base"}),
    SetpointSkill(name="look_at", claims={"head"}),
])
s1 = ControlLoop(robot=s0, action_source=skills, fps=50, cameras=cameras)

robot = RobotRuntime(
    loops={"s0": s0, "s1": s1},
    nodes={"bimanual": bimanual, "planner": Agent(planner_model, skills)},
    serve=[SkillsServer(skills, port=7001)],
    placement={"s0": "rt-guest", "s1": "robot-main", "bimanual": "gpu", "planner": "cloud"},
)
```

`s0` has no action source, so it takes setpoints from the loop above it. Without
`placement`, everything runs in one process with in-memory links. With `placement`,
each partition runs the same description and hosts only its own components:

```bash
physicalai run --config humanoid.yaml --run.partition=rt-guest
physicalai run --config humanoid.yaml --run.partition=robot-main
physicalai run --config humanoid.yaml --run.partition=gpu
physicalai run --config humanoid.yaml --run.partition=cloud
```

The runtime replaces each edge that crosses a partition with a remote link. The planner
in the cloud partition reaches `skills` through the skills link, `s1` reaches `s0`
through the robot command link, and the `manipulate` skill reaches `bimanual` through
the inference link. Skills on disjoint parts run concurrently under
separate leases:

```python
skills.submit(Goal("look_at", {"target": "shelf"}))
skills.submit(Goal("walk_to", {"target": "shelf"})).result()
skills.submit(Goal("manipulate", {"task": "take the box"})).result()
```

The same description in YAML. References between components are written by name; the
reference syntax is illustrative:

```yaml
runtime:
  loops:
    s0:
      robot:
        class_path: physicalai.runtime.SafetyGate
        init_args:
          robot: { class_path: physicalai.robot.unitree.G1 }
          limits: ./limits/g1.yaml
      fps: 500
    s1:
      robot: { ref: loops.s0 }
      action_source:
        class_path: physicalai.skills.Skills
        init_args:
          skills: [] # as above
      fps: 50
  nodes:
    bimanual:
      class_path: physicalai.inference.InferenceModel
      init_args:
        export_dir: ./exports/bimanual
    planner:
      class_path: physicalai.agent.Agent
      init_args:
        model: { class_path: physicalai.models.OpenAICompatibleModel }
        skills: { ref: loops.s1.action_source }
  placement:
    s0: rt-guest
    s1: robot-main
    bimanual: gpu
    planner: cloud
```

---

## 6. ControlLoop

`ControlLoop` is the class that ships today as `RobotRuntime`, with the same constructor
and lifecycle. It is runnable on its own.

```python
class ControlLoop:
    def __init__(self, robot, action_source=None, fps=30.0, cameras=None, callbacks=(),
                 interpolator=None): ...           # existing signature; action_source becomes optional
    @classmethod
    def from_config(cls, config: str | Path) -> Self: ...   # existing
    def run(self, duration_s: float | None = None) -> None: ...  # existing: blocks
    def start(self) -> None: ...                    # new: runs in a background thread
```

| Argument        | Accepts                                                                                   |
| --------------- | ----------------------------------------------------------------------------------------- |
| `robot`         | Hardware, `SafetyGate`, a vendor controller, a ROS 2 controller, or another `ControlLoop` |
| `action_source` | `PolicySource`, `TeleopSource`, `Skills`, a ROS 2 topic source, or none                   |
| `callbacks`     | Recording, telemetry, and action filters                                                  |
| `interpolator`  | Light System 0: splits each action into several robot commands (existing)                 |

A loop without an action source is a lower tier: it takes setpoints from the loop that
uses it as a robot, holds when they stop, and stops when they exceed their deadline.

---

## 7. RobotRuntime

`RobotRuntime` describes and runs a whole robot.

```python
class RobotRuntime:
    def __init__(
        self,
        loops: Mapping[str, ControlLoop],
        nodes: Mapping[str, Node] | None = None,          # agents, perceivers, monitors
        serve: Sequence[Server] = (),                     # skills API, MCP, ROS 2 actions
        placement: Mapping[str, str] | None = None,       # loop or node name -> partition
        callbacks: Sequence[Callback] = (),               # runtime events: health, recording
    ): ...
    @classmethod
    def from_config(cls, config: str | Path) -> Self: ...
    def run(self, partition: str | None = None) -> None: ...
    def stop(self, *, emergency: bool = False) -> None: ...
    health: Mapping[str, Health]
```

The runtime is responsible for:

| Responsibility | Behavior                                                                                         |
| -------------- | ------------------------------------------------------------------------------------------------ |
| Placement      | Instantiates and hosts only the loops and nodes assigned to the current partition                |
| Links          | Connects every pair of components in memory or over a transport ([§11](#11-links-and-placement)) |
| Endpoints      | Hosts the servers in `serve`; each server names the `Skills` object it exposes                   |
| Lifecycle      | Starts from the bottom tier up and stops from the top down                                       |
| Health         | Aggregates the liveness of every loop, node, and link                                            |
| Emergency stop | Propagates `stop(emergency=True)` to every partition; each `SafetyGate` holds or brakes          |

`run(partition=...)` is a method argument, so the existing configuration system parses
it from the CLI as it parses the constructor.

---

## 8. Actions and Parts

An action may be a mapping from part to command. A flat `np.ndarray` remains valid for
sources that command a single part, so `PolicySource` and `TeleopSource` are unchanged.

```python
{
    "left_arm": Command(mode=EE_POSE, values=..., frame="base"),
    "base": Command(mode=TWIST, values=...),
}
```

Robots may declare their parts and supported command modes through an optional
`Robot.command_interfaces()`. By default, a robot is one part that accepts joint
positions.

---

## 9. Goals and Skills

Skills live in the `physicalai.skills` package, which has no LLM dependency.

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
    claims: frozenset[Part]

    def can_start(self, goal: Goal, world: WorldState) -> Precondition: ...
    def start(self, goal: Goal, ctx: SkillContext) -> SkillRun: ...

class Skills(ActionSource):
    def __init__(self, skills: Sequence[Skill], *, access: AccessPolicy | None = None): ...
    def submit(self, goal: Goal, *, priority: Priority = Priority.NORMAL) -> SkillHandle: ...
    def register(self, skill: Skill) -> None: ...
    world: WorldState
    # ActionSource: update() merges the actions of running skills; idle parts hold
```

`Skill.start` returns a `SkillRun`, an internal object that `Skills` drives.
`Skills.submit` wraps the run in a `SkillHandle` and returns that handle to the client.

Skills fall into three types according to how they actuate the robot:

| Type          | Actuation                                                  | Examples                                |
| ------------- | ---------------------------------------------------------- | --------------------------------------- |
| **Attached**  | Attaches an `ActionSource` to its claimed parts            | `PolicySkill`, `TeleopSkill`            |
| **Setpoint**  | Writes one deadline-bounded setpoint per call              | `ServoSkill`, `SetpointSkill` (look-at) |
| **Delegated** | Hands off to an external controller and mirrors its status | Nav2, vendor locomotion                 |

`PolicySkill` is generic. Pick and place are configured instances of it, not subclasses:

```python
class PolicySkill:
    def start(self, goal, ctx):
        source = PolicySource(self.model, task=self.task.format(**goal.params))
        ctx.attach(source, parts=self.claims)
        return AttachedRun(source, success=goal.success or self.success)
```

**Ticks.** On each tick, the loop calls `Skills.update()`. `Skills` applies pending
attaches and detaches, queries each attached source for its parts, applies current
setpoints, holds parts that have no writer, and returns the merged part-keyed action.
All skills inside one `Skills` object therefore run at the host loop's rate. Slower
sources hold between outputs, as `PolicySource` already does between action chunks.

**Leases.** Each part has at most one writer. Parts form a hierarchy, so a lease on
`whole_body` covers the arms and the base. A higher-priority goal may preempt a
lower-priority lease. If a lease misses its heartbeat, the skill is cancelled and the
parts hold position.

**`SkillHandle`.** A handle exposes `status`, `feedback`, `update`, `cancel`, and
`result`.

**`WorldState`.** `WorldState` stores the latest value of each fact, with one writer per
key and no history. `Skills` writes robot state and camera frames from each tick.
Perceivers write detections and captions. Skills, success checks, and clients read
from it.

**Success checks.** A success check returns a confidence value rather than a boolean.
The following checks are illustrative:

```python
GripperClosedOnObject  # the gripper has closed on an object
ObjectAt               # a detector places the object in a target region
VLMJudge               # a ReasoningModel judges the outcome from images
```

---

## 10. Models and Backends

Models belong to the components that use them. A `PolicySkill` owns an `ActionModel`, as
`PolicySource` does today. An agent or a `VLMJudge` owns a `ReasoningModel`. Roles such
as planner, manipulator, and judge are therefore not separate base classes.

```python
class ActionModel(Protocol):     # existing as InferenceModel
    def predict_action_chunk(self, observation) -> np.ndarray: ...
    def reset(self) -> None: ...

class ReasoningModel(Protocol):
    def complete(self, messages, *, tools=(), response_format=None) -> Completion: ...
```

**`ModelSpec`.** A `ModelSpec` declares a model's inputs, its outputs, and the parts and
command modes its outputs drive. `Skills` checks each model's spec against the robot's
parts at startup. Plugins convert native formats, such as LeRobot features and GR00T
modality files, into a `ModelSpec`.

| Interface        | Backends                                                                                                               |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `ActionModel`    | OpenVINO and ONNX; LeRobot and GR00T through plugins                                                                   |
| `ReasoningModel` | OpenAI-compatible servers (SGLang, vLLM, llama.cpp, Ollama); Anthropic; Gemini; in-process OpenVINO GenAI or llama.cpp |

An action model runs in one of three ways:

| Location                          | Mechanism                                                                                                       | Intended use                                                    |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| In-process                        | `InferenceModel` (existing)                                                                                     | OpenVINO or ONNX models and other small models                  |
| Another partition, runtime-placed | A model named in `nodes`; the runtime serves it with `InferenceServer` and links it with `RemoteInferenceModel` | Conflicting dependencies, crash isolation, a dedicated GPU host |
| Served externally                 | `RemoteInferenceModel` (existing)                                                                               | Inference servers that the runtime does not manage              |

Remote inference already exists. `RemoteInferenceModel` is a subclass of
`InferenceModel`, and `InferenceServer` serves any `InferenceModel`, over Zenoh. Client
and server agree on a named deployment slot, and a handshake checks the policy identity
([remote inference guide](../../../how-to/runtime/run-inference-remotely.md)). Because
the client is an `InferenceModel`, `PolicySource`, `PolicySkill`, and every execution
mode use it unchanged. A runtime-placed model reuses the same pair: its node name is the
deployment slot.

A partition that hosts a model may be a process in a separate Python environment
prepared by `physicalai.isolate`, which is how framework plugins with conflicting
dependencies run. The server disables Zenoh shared memory, so same-host traffic uses
TCP; its latency must be measured for local partitions.

Remote System 1 inference is viable only when the action-chunk horizon exceeds the sum
of network round-trip time, inference time, and a safety margin. If a chunk arrives
late, the source holds and `Skills` reports starvation to the skill.

---

## 11. Links and Placement

Every edge in the robot description is one of three typed links. The runtime chooses
the implementation of each link from the placement of its two ends:

| Link               | Between                      | Same partition                | Different partitions                                      |
| ------------------ | ---------------------------- | ----------------------------- | --------------------------------------------------------- |
| Robot command link | A loop and the loop below it | Shared-memory setpoint buffer | Extends the existing `SharedRobot` transport              |
| Inference link     | A model user and a model     | Direct call                   | The existing `RemoteInferenceModel` and `InferenceServer` |
| Skills link        | A client and `Skills`        | Direct call                   | New: `SkillsServer` and `SkillsClient`                    |

All three links use Zenoh through `physicalai.transport`, the transport that cameras,
shared robots, and remote inference already use.

**Robot command link.** The existing `SharedRobot` transport provides an owner process
that holds the hardware, state subscription and action publishing for any number of
subscribers, discovery, and host-local exclusivity. The robot command link adds
part-keyed setpoints, each carrying a mode, values, a timestamp, a deadline, and a
lease. The lower loop rejects setpoints that have expired or carry the wrong lease, and
it holds, decays, or stops when setpoints become stale. When several upper loops share
one lower loop, the lower loop arbitrates between them by part. A native or vendor
controller can implement this link directly when System 0 must run beyond Python's
real-time limits.

**Inference link.** This link exists today. It streams observations to a named model
server and returns action chunks, validates the policy identity in a handshake, and
reports timing. The design adds a `ModelSpec` check at the handshake.

**Skills link.** It lists skills, submits goals, streams updates, and supports update,
cancel, and heartbeat calls. It also exposes `WorldState`, skill events, and an
emergency stop. Missed heartbeats expire the caller's leases. External clients that are
not part of the robot description use the same link through `SkillsClient`.

**Authentication.** The existing Zenoh transports provide no application-level
authentication. They rely on loopback binding, SSH tunnels, or network isolation. The
access policy and the partition boundary hold across hosts only if the robot command
and skills links authenticate their peers, so authentication is required for both
before cross-host deployment.

**Construction per partition.** When a YAML description runs with
`run(partition=...)`, the runtime instantiates only the components placed in that
partition. `InferenceModel` loads its weights in its constructor, so this keeps each
model's weights, and its dependencies, in the partition that runs it. A Python
description constructs every component in the process that builds it, so multi-partition
deployments use YAML.

**Deployment layouts.** All layouts use the same description:

| Layout                         | Placement                                                                            |
| ------------------------------ | ------------------------------------------------------------------------------------ |
| One process                    | None; the interpolator may act as System 0                                           |
| One host, several accelerators | System 0 in its own process for jitter isolation; models assigned to devices         |
| Robot, GPU server, and cloud   | Large action models remote; the planner in the cloud partition                       |
| KVM guests                     | One partition per guest; the System 2 guest holds no device drivers                  |
| Vendor System 0                | The `s0` robot is the vendor driver, or the vendor implements the robot command link |

On a shared host, System 0 and System 2 should not share compute, and System 1 should
take priority over System 2. Jitter must be measured on the target platform.

---

## 12. Safety, Time, and Recording

Safety is layered. Each layer is enforced independently of the layers above it.

| Layer                | Component                      | Enforces                                                      |
| -------------------- | ------------------------------ | ------------------------------------------------------------- |
| Hardware / firmware  | Robot                          | Power, torque, and temperature limits                         |
| Safety gate          | `SafetyGate`                   | Joint, velocity, workspace, and force limits on every command |
| Stale-command hold   | Lower loop                     | Holds or stops when the tier above goes silent                |
| Leases and deadlines | `Skills`, lower loop           | A single writer per part                                      |
| Access policy        | `Skills`                       | Which clients may run which skills                            |
| Link authentication  | Robot command and skills links | Only authenticated peers send setpoints or submit goals       |
| Partition boundary   | Placement                      | The System 2 partition holds no device drivers                |
| Emergency stop       | `RobotRuntime`                 | Propagates to every partition                                 |

The `unrestricted` access mode removes permission checks only. Safety gates, leases,
and deadlines remain in force.

| Failure              | Response                                                    |
| -------------------- | ----------------------------------------------------------- |
| Client disconnects   | Its lease expires; the skill is cancelled; parts hold       |
| System 2 crashes     | As above; System 1 and System 0 continue                    |
| Remote model is late | The source holds and reports starvation; the skill may fail |
| System 1 goes silent | System 0 applies its stale-command hold                     |
| System 0 faults      | System 1 stops; running skills fail                         |
| Link lost            | Treated as silence on the side that stops receiving         |

**Time.** All partitions share a clock. Deadlines are absolute, and `WorldState`
timestamps allow readers to detect stale facts.

**Recording.** `Skills` receives its loop's callback bus when it connects, as every
action source does. It emits an event when a skill starts, is preempted, and ends. A
recording callback uses these events to produce one episode per skill run. The episode
contains the loop data in LeRobot format, the goal, the submitting client, the outcome,
model provenance, intervention segments, and the agent transcript when one exists.
Failed and cancelled runs are recorded in the same way as successful ones.

---

## 13. Integrations

Integrations sit at the edges of the design and implement its protocols. The core does
not depend on any of them, and a deployment includes only the integrations it uses.

**ROS 2.** Each ROS 2 concept maps to an existing protocol:

| ROS 2 concept                              | Integration                                                  | Protocol       |
| ------------------------------------------ | ------------------------------------------------------------ | -------------- |
| `ros2_control` controller or ROS 2 driver  | `Ros2Robot`: a ROS 2 controller acts as System 0             | `Robot`        |
| Command topic (joystick, external planner) | `Ros2Source`: a topic drives a loop                          | `ActionSource` |
| Action server                              | `Ros2ActionServer`: each skill appears as a ROS 2 action     | Skills link    |
| Action client (Nav2, MoveIt)               | `DelegatedSkill`: an existing ROS 2 stack performs the skill | `Skill`        |
| TF and detection topics                    | A bridge that publishes and subscribes `WorldState` facts    | `WorldState`   |

ROS 2 actions have the same structure as goals and skill handles (goal, feedback,
result, and cancel), so a ROS 2 application can call PhysicalAI skills natively, and a
PhysicalAI client can call ROS 2 stacks as delegated skills.

**Other integrations.** Execution backends, vendor SDKs, and other middleware can be
added in the same way: as `Robot`, `ActionSource`, `ActionModel`, `Skill`, or server
implementations, or as alternative link transports.

---

## 14. Migration

The class that ships today as `RobotRuntime` is renamed to `ControlLoop`, and the name
`RobotRuntime` is reused for the whole-robot runtime. The change is staged so that no
single release breaks existing code:

| Stage | Change                                                                                                                                                                                                                                                                                       | Effect on existing code                      |
| ----- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------- |
| 1     | Add `ControlLoop`; `RobotRuntime` becomes an alias for it                                                                                                                                                                                                                                    | None                                         |
| 2     | Introduce the whole-robot `RobotRuntime`. For one minor release it still accepts `robot`, `action_source`, and `fps`, warns, and builds a one-loop runtime that forwards `run`, `stop`, `connect`, and the existing properties. Configurations that name the old class resolve the same way. | Deprecation warning                          |
| 3     | Remove the compatibility path                                                                                                                                                                                                                                                                | Code must use `ControlLoop` for single loops |

Studio and the documentation move to `ControlLoop` during stage 1.

---

## 15. Requirements Coverage

| Requirement ([§2](#2-motivation))       | Mechanism                                                                                                                      |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| 1. System 0, 1, and 2 on one robot      | Loops linked through `robot`; agents and other clients call `Skills`                                                           |
| 2. Frontier model drives System 1 or 0  | Goals to policy skills; `ServoSkill` setpoints to System 0                                                                     |
| 3. Any deployment from one description  | Placement selects in-memory or remote links ([§11](#11-links-and-placement))                                                   |
| 4. Models from any backend and hardware | Models are owned by skills and agents; frameworks stay in plugins; models run in-process, runtime-placed, or served externally |
| 5. Concurrent work on disjoint parts    | Leases and part-keyed actions in `Skills`                                                                                      |
| 6. Teleoperation, HIL, and DAgger       | `TeleopSource`; `TeleopSkill` preemption; intervention segments in episodes                                                    |
| 7. Non-bypassable safety                | `SafetyGate` wraps the hardware; stale-command hold below every tier; runtime emergency stop                                   |
| 8. Labeled episode per run              | Skill events on the existing callback bus                                                                                      |
| 9. Python, YAML, and CLI equivalent     | Every component is a constructor argument; `run(partition=...)` is parsed like the constructor                                 |
| 10. ROS 2 without dependency            | Edge integrations implementing existing protocols ([§13](#13-integrations))                                                    |
| 11. Gradual migration                   | Staged rename with a compatibility release ([§14](#14-migration))                                                              |

---

## 16. Open Questions and Deferred Decisions

**Open questions**

1. **Rates within one tier.** All skills in one `Skills` object run at the host loop's
   rate. Workloads that need separate rates or processes, such as navigation and
   manipulation, can run as two System 1 loops sharing one System 0, which arbitrates
   by part.
2. **Delegated controllers.** External controllers such as Nav2 must send their
   commands through the robot command link so that `SafetyGate` and leases apply.
3. **Combining a fixed source with skills.** Whether a loop may run a long-lived source
   (for example, teleoperation of the base) alongside skills, or whether such a source
   must itself be a skill.
4. **Python descriptions across partitions.** Python constructs every component
   eagerly, so multi-partition deployments use YAML. Whether Python descriptions also
   need lazy construction is open.

**Deferred decisions**

| Topic                                                  | Needed by                  |
| ------------------------------------------------------ | -------------------------- |
| Command modes and payloads                             | Part-keyed actions         |
| Reference syntax in YAML                               | Multi-loop descriptions    |
| Message schemas for the robot command and skills links | Multi-partition deployment |
| Partition addressing and discovery                     | Multi-partition deployment |
| Link authentication mechanism                          | Cross-host deployment      |
| Lease durations and priorities                         | `Skills`                   |
| Access-policy format                                   | Multiple clients           |
| Real-time platform for System 0                        | Hosted heavy System 0      |
| Clock synchronization                                  | Multi-partition deployment |
| Recording format extensions                            | Data collection at scale   |
| Package location of `Agent`                            | System 2 clients           |
| World-model interface                                  | Imagine-and-rank planning  |

---

## 17. Implementation Phases

| Phase | Scope                                                                                                         | Exit criterion                                            |
| ----- | ------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| 1     | `ControlLoop` with `RobotRuntime` alias; part-keyed actions; `start()`                                        | Existing tests, configurations, and Studio pass           |
| 2     | `SafetyGate`; a loop as the robot of another loop; single-partition `RobotRuntime`                            | System 0 and System 1 in one process; gate tests pass     |
| 3     | `Skills`, `Goal`, `PolicySkill`, `TeleopSkill`, leases, skill events                                          | Tabletop task from Python and YAML; HIL takeover recorded |
| 4     | Runtime-placed models through `InferenceServer`; isolated-environment partitions; `ModelSpec`                 | Identical actions in-process and served                   |
| 5     | `SkillsServer`, `SkillsClient`, `McpServer`; reasoning clients; `Agent`                                       | Cloud agent and MCP host drive the same robot             |
| 6     | Placement; robot command link on `SharedRobot`; remote skills link; link authentication; `run(partition=...)` | Same description runs in one process and across hosts     |
| 7     | KVM partitions; runtime health and emergency stop; ROS 2 integrations                                         | Humanoid across guests; ROS 2 application calls skills    |
| 8     | Delegated navigation; perceivers; world model                                                                 | Navigate, pick, and place in one task                     |

Phases 1 to 3 deliver value without the later phases. The compatibility path of the
old `RobotRuntime` constructor is removed one minor release after phase 2.

---

## 18. Decisions

```text
story           Agent → Goal → Skill → SkillHandle; Skills drives a ControlLoop
tiers           System 0 control, System 1 policy, System 2 deliberation
loop            ControlLoop runs one tier at one rate (the class formerly named RobotRuntime)
runtime         RobotRuntime runs a whole robot: placement, links, endpoints, lifecycle
composition     the tier below a loop is its robot; another loop can be that robot
description     one robot description for every deployment; placement selects partitions
links           robot command, inference, and skills; in memory or over Zenoh; authenticated
clients         anything that submits goals; Agent is the built-in reasoning-model client
skills          Skills is an ActionSource; attached, setpoint, and delegated skills
actions         part-keyed command maps; flat arrays remain valid
models          owned by skills and agents; in-process, runtime-placed, or served externally
safety          SafetyGate wraps hardware; leases, deadlines, access policy, emergency stop
intervention    HIL is preemption by a higher-priority teleoperation skill
integrations    at the edges, implementing existing protocols; the core depends on none
migration       staged rename with one compatibility release
terminology     robot skill (Skill) is distinct from agent skill (SKILL.md)
```
