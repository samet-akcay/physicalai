# Tiered Robot Runtime: System 0 / 1 / 2

**Status:** Proposal for design review.

This document describes how PhysicalAI runs three tiers of robot decision-making: System 0
(control), System 1 (reactive policy), and System 2 (deliberation). Models may come from
any backend, and the tiers may share one process or be spread across accelerators, KVM
guests, or separate machines. The design is hardware-agnostic and does not depend on any
external middleware.

The document specifies architecture only. Message schemas, key layouts, and numeric
limits are deferred ([§16](#16-open-questions-and-deferred-decisions)). Code is
pseudo-code unless marked **existing**.

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
11. [Communication Between Partitions](#11-communication-between-partitions)
12. [Safety and Recording](#12-safety-and-recording)
13. [Integrations](#13-integrations)
14. [Migration](#14-migration)
15. [Requirements Coverage](#15-requirements-coverage)
16. [Open Questions and Deferred Decisions](#16-open-questions-and-deferred-decisions)
17. [Implementation Phases](#17-implementation-phases)
18. [Decisions](#18-decisions)

---

## 1. Summary

The design rests on four ideas:

- A **`ControlLoop`** runs one tier at a fixed rate. On each tick it reads its robot, asks
  its action source for an action, passes the action through its callbacks, and sends
  it. This is the class that ships today as `RobotRuntime`, renamed
  ([§14](#14-migration)).
- A **`RobotRuntime`** runs everything in one partition (a process, VM, or machine): its
  control loops, other components such as agents, and the servers that expose them. It
  starts them in order, stops them in order, and reports their health.
- **Components connect by object or by name.** In the same process, a component receives
  the object it depends on. In another process, it receives a client that refers to the
  object by name.
- **A robot is a namespace.** All partitions of one robot join a shared namespace named
  after the robot. Discovery, health, and emergency stop are messages in that namespace,
  so the whole robot can be observed and stopped without a central object.

```python
# one tier
ControlLoop(robot=so101, action_source=PolicySource(model), fps=30).run()

# one partition with several components
RobotRuntime(
    robot_name="tabletop-1",
    loops={"s1": ControlLoop(robot=so101, action_source=skills, fps=30)},
    nodes={"planner": Agent(planner_model, skills)},
    serve=[SkillsServer(skills, name="tabletop")],
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

---

## 2. Motivation

Recent robot systems separate slow reasoning from fast control. A language or
vision-language model plans and verifies, a learned policy produces motion, and a
high-rate controller tracks it. The System 1 and System 2 labels follow the dual-process
terminology common in this literature; System 0 denotes the control layer beneath them.

These tiers place different demands on the platform. Their models often do not fit in
the same memory. They need different operating-system and real-time guarantees. Some
robot vendors run them in separate KVM guests on a single SoC, and the System 0 guest is
often supplied by the vendor.

**Current state.** PhysicalAI provides a single fixed-rate loop with pluggable action
sources (`PolicySource`, `TeleopSource`) and callbacks; synchronous, asynchronous, and
real-time-chunking policy execution; a configuration system in which YAML and the CLI
mirror constructor signatures; Zenoh-based transports for shared cameras, shared robots
(`SharedRobot`), and remote inference (`RemoteInferenceModel` and `InferenceServer`); and
framework plugins with isolated environments (in progress).

**Requirements.** The runtime must:

1. Run System 0, 1, and 2 on one robot.
2. Let a frontier model drive System 1 or System 0.
3. Run the tiers in one process, across accelerators on one host, in separate KVM
   guests, or on separate machines, including partitions owned by a vendor.
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

| Term                | Definition                                                                                     |
| ------------------- | ---------------------------------------------------------------------------------------------- |
| **Tier**            | System 0, 1, or 2. A responsibility and rate band, not a process.                              |
| **`ControlLoop`**   | Runs one tier at a fixed rate: read the robot, query the action source, apply callbacks, send. |
| **`RobotRuntime`**  | Runs everything in one partition: loops, nodes, and servers, with their lifecycle.             |
| **Node**            | A component hosted by a runtime that is not a control loop, such as an agent or a perceiver.   |
| **Partition**       | A unit of isolation and deployment: a process, container, VM, or machine.                      |
| **Robot namespace** | The shared key space of one robot's partitions, named after the robot.                         |
| **Link**            | A connection between two components: an object reference, or a client and server by name.      |

**Goals and skills**

| Term              | Definition                                                                                                                |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------- |
| **Client**        | Anything that submits goals: an agent, a script, Studio, a ROS 2 application, or a human.                                 |
| **Agent**         | A client that uses a reasoning model to choose goals. The built-in `Agent` class is one; an external MCP host is another. |
| **Goal**          | One request: a skill name, its parameters, and an optional success check.                                                 |
| **Skill**         | A robot capability such as `pick`, `servo`, or `navigate_to`. Turns a goal into an action source for its claimed parts.   |
| **`Skills`**      | A robot's set of skills. As a loop's action source, it accepts goals, grants leases, and runs each goal's source.         |
| **`SkillHandle`** | The client's view of one submitted goal: status, feedback, update, cancel, and result.                                    |
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

On a single host, one `RobotRuntime` holds every component and links them by object
reference:

```text
one partition
┌──────────────────────────────────────────────────────────────┐
│ RobotRuntime "tabletop-1"                                     │
│   nodes:  Agent ──► Skills                                    │
│   loops:  s1 (50 Hz, action_source=Skills) ──► s0 (500 Hz)   │
│           s0.robot = SafetyGate(hardware)                     │
│   models: InferenceModel (in-process)                         │
│   serve:  SkillsServer(name="tabletop"), McpServer            │
└──────────────────────────────────────────────────────────────┘
```

Across partitions, each partition runs its own part, and links cross partitions by name
inside the robot namespace:

```text
 rt-guest                 robot-main                         gpu                 cloud
┌──────────────────┐     ┌───────────────────────────────┐  ┌────────────────┐  ┌──────────────────┐
│ RobotRuntime     │     │ RobotRuntime                  │  │ InferenceServer │  │ Agent            │
│  s0, 500 Hz      │◄────│  s1, 50 Hz                    │─►│  "bimanual"     │  │  SkillsClient    │
│  SafetyGate(G1)  │robot│   robot = SharedRobot "g1"    │  └────────────────┘  │   ("humanoid")   │
│  RobotServer "g1"│ link│   Skills → RemoteInference-   │  inference link      └────────┬─────────┘
└──────────────────┘     │     Model("bimanual")         │                              │
                         │  SkillsServer "humanoid"      │◄────── skills link ──────────┘
                         └───────────────────────────────┘
              all four partitions share the robot namespace "g1-lab3"
```

### Rules

1. **A `ControlLoop` runs one tier at one rate.** It holds no knowledge of other tiers.
2. **The tier below a loop is its robot.** That robot is hardware, a vendor controller,
   another `ControlLoop` in the same process, or a `SharedRobot` client for a loop in
   another partition.
3. **A `RobotRuntime` manages one partition.** It never manages components in another
   partition.
4. **Links are explicit.** A component receives either the object it depends on or a
   client that names it. Nothing is rewired implicitly.
5. **A robot is a namespace.** Robot-wide discovery, health, and emergency stop are
   messages in the namespace, not methods of a central object.
6. **Callbacks observe and transform actions.** They do not decide which action to send.
7. **`SafetyGate` wraps the hardware,** so every command passes through it regardless of
   its origin.
8. **Every write is bounded by a lease and a deadline.** A missed heartbeat or deadline
   causes a hold or a stop.
9. **Every client goes through `Skills`.** There is no privileged path.
10. **Integrations sit at the edges and implement these protocols.** The core never
    depends on them.

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
builds a `PolicySource` from the skill, and runs it until the success check passes. It
then disconnects the source and emits the events that delimit the recorded episode.

### 5.4 Agent or MCP host

The built-in `Agent` sends the instruction to a reasoning model with each skill offered
as a tool. It submits a goal for each tool call and returns the result to the model,
until the model reports that the task is done:

```python
RobotRuntime(
    robot_name="tabletop-1",
    loops={"s1": ControlLoop(robot=SO101(port="/dev/ttyACM0"), action_source=skills, fps=30)},
    nodes={"planner": Agent(OpenAICompatibleModel(base_url="http://localhost:30000/v1"), skills)},
    serve=[SkillsServer(skills, name="tabletop"), McpServer(skills)],
).run()
```

An external MCP host, Studio, or a script on another machine drives the same skills
through the skills link:

```python
skills = SkillsClient(name="tabletop", robot_name="tabletop-1")
skills.submit(Goal("pick", {"object": "mug"})).result()
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

Each goal steps the arm toward the requested offset under a deadline. `SafetyGate`
clamps every step, and the loop holds the arm if goals stop arriving. Without a System 1 policy, `Skills` can be the action
source of the System 0 loop directly.

### 5.7 Mixed backends

Each skill owns its model. A model runs in-process, or in another partition that serves
it by name:

```python
skills = Skills([
    PolicySkill(name="inspect", model=InferenceModel("./exports/inspect", device="NPU"), ...),
    PolicySkill(name="pick", model=RemoteInferenceModel(name="gr00t-pick", host="gpu-box"), ...),
    PolicySkill(name="place", model=RemoteInferenceModel(name="lerobot-place"), ...),
])
```

```bash
# a GR00T server on a GPU host
physicalai inference serve --name gr00t-pick --export-dir ./exports/pick --device GPU
# a LeRobot server on the robot host, in its own Python environment
physicalai inference serve --name lerobot-place --export-dir ./exports/place
```

Framework code stays in plugins. Moving a model between in-process and served changes
one argument: `InferenceModel(...)` becomes `RemoteInferenceModel(name=...)`.

### 5.8 Humanoid on one host

```python
s0 = ControlLoop(robot=SafetyGate(G1(), limits), fps=500)
skills = Skills([
    PolicySkill(name="manipulate", model=InferenceModel("./exports/bimanual"),
                claims={"left_arm", "right_arm"}),
    DelegatedSkill(name="walk_to", controller=Nav2(), claims={"base"}),
    SetpointSkill(name="look_at", claims={"head"}),
])
s1 = ControlLoop(robot=s0, action_source=skills, fps=50, cameras=cameras)

RobotRuntime(
    robot_name="g1-lab3",
    loops={"s0": s0, "s1": s1},
    nodes={"planner": Agent(planner_model, skills)},
    serve=[SkillsServer(skills, name="humanoid")],
).run()
```

`s0` has no action source, so it takes setpoints from `s1`, which uses it as its robot.
The runtime starts `s0` before `s1` and stops them in the reverse order. Skills on
disjoint parts run concurrently under separate leases:

```python
skills.submit(Goal("look_at", {"target": "shelf"}))
skills.submit(Goal("walk_to", {"target": "shelf"})).result()
skills.submit(Goal("manipulate", {"task": "take the box"})).result()
```

### 5.9 Humanoid across partitions

The same robot split across a real-time guest, the main robot computer, a GPU host, and
the cloud. Each partition has its own short description, and every link that crosses a
partition names its peer:

```python
# rt-guest: System 0
s0 = ControlLoop(robot=SafetyGate(G1(), limits), fps=500)
RobotRuntime(robot_name="g1-lab3", loops={"s0": s0}, serve=[RobotServer(s0, name="g1")]).run()

# robot-main: System 1 and skills
skills = Skills([
    PolicySkill(name="manipulate", model=RemoteInferenceModel(name="bimanual"),
                claims={"left_arm", "right_arm"}),
    DelegatedSkill(name="walk_to", controller=Nav2(), claims={"base"}),
    SetpointSkill(name="look_at", claims={"head"}),
])
RobotRuntime(
    robot_name="g1-lab3",
    loops={"s1": ControlLoop(robot=SharedRobot.attach("g1"), action_source=skills, fps=50)},
    serve=[SkillsServer(skills, name="humanoid")],
).run()

# cloud: System 2
Agent(planner_model, SkillsClient(name="humanoid", robot_name="g1-lab3")).run(
    "Take the box from the shelf."
)
```

```bash
# gpu: the model server
physicalai inference serve --name bimanual --export-dir ./exports/bimanual --device GPU
```

Compared with 5.8, three arguments change: `s1` takes `SharedRobot.attach("g1")` instead
of `s0`, the skill takes `RemoteInferenceModel(name="bimanual")` instead of an
`InferenceModel`, and the agent takes a `SkillsClient` instead of `skills`. Each
partition can equally be a YAML file run with `physicalai run --config`. An optional
deployment file lists the partitions and their hosts so that `physicalai up` can launch
them together during development.

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

| Argument        | Accepts                                                                                                    |
| --------------- | ---------------------------------------------------------------------------------------------------------- |
| `robot`         | Hardware, `SafetyGate`, a vendor controller, a ROS 2 controller, another `ControlLoop`, or a `SharedRobot` |
| `action_source` | `PolicySource`, `TeleopSource`, `Skills`, a ROS 2 topic source, or none                                    |
| `callbacks`     | Recording, telemetry, and action filters                                                                   |
| `interpolator`  | Light System 0: splits each action into several robot commands (existing)                                  |

A loop without an action source is a lower tier. It takes setpoints from the loop that
uses it as a robot, in the same process directly or from another partition through a
`RobotServer`. It holds when setpoints stop and rejects setpoints past their deadline.

---

## 7. RobotRuntime

`RobotRuntime` runs everything in one partition.

```python
class RobotRuntime:
    def __init__(
        self,
        robot_name: str,                                  # the robot namespace this partition joins
        loops: Mapping[str, ControlLoop] | None = None,
        nodes: Mapping[str, Node] | None = None,          # agents, perceivers, monitors
        serve: Sequence[Server] = (),                     # RobotServer, SkillsServer, McpServer, ROS 2 actions
        callbacks: Sequence[Callback] = (),               # runtime events: health, recording
    ): ...
    @classmethod
    def from_config(cls, config: str | Path) -> Self: ...
    def run(self) -> None: ...
    def stop(self, *, emergency: bool = False) -> None: ...
    health: Mapping[str, Health]
```

| Responsibility | Behavior                                                                                              |
| -------------- | ----------------------------------------------------------------------------------------------------- |
| Lifecycle      | Starts loops from the bottom tier up, then nodes, then servers; stops in the reverse order            |
| Dependencies   | Waits, with a timeout, for every named peer its components require before starting them               |
| Endpoints      | Hosts the servers in `serve`; each server names the object it exposes                                 |
| Health         | Reports the liveness of its loops, nodes, and links, and publishes it in the robot namespace          |
| Emergency stop | Stops its loops on a local `stop(emergency=True)` or on an emergency stop received from the namespace |

Every partition of a multi-partition robot has its own `RobotRuntime`, except partitions
that contain a single component, such as an `InferenceServer` or an `Agent`, which need
no supervisor.

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
    timeout_s: float | None = None

class Skill(Protocol):
    name: str
    description: str
    params_schema: type[Params]         # generates the tool, form, and CLI schemas
    claims: frozenset[Part]
    success: SuccessCheck | None        # default check; None runs until cancelled

    def can_start(self, goal: Goal, world: WorldState) -> Precondition: ...
    def source(self, goal: Goal) -> ActionSource: ...

class Skills(ActionSource):
    def __init__(self, skills: Sequence[Skill], *, access: AccessPolicy | None = None): ...
    def submit(self, goal: Goal, *, priority: Priority = Priority.NORMAL) -> SkillHandle: ...
    def register(self, skill: Skill) -> None: ...
    world: WorldState
    # ActionSource: update() merges the actions of running sources; idle parts hold
```

`Skills` is a robot's set of skills: it accepts goals, grants leases, and runs skills on
the loop. A skill turns a goal into an `ActionSource`, and `Skills` runs that source on
the skill's claimed parts until the goal ends. `Skills.submit` returns a `SkillHandle`
to the client. `SkillsClient` exposes the same `submit` and `world` surface in another
partition.

Each stage of a goal maps onto the existing `ActionSource` protocol:

| Stage                | Handled by                                                                                          |
| -------------------- | --------------------------------------------------------------------------------------------------- |
| Start                | `Skills` grants the lease, calls `skill.source(goal)`, and then calls `connect()` on the source     |
| Each tick            | `Skills` calls `update()` on the source and keeps the action for the claimed parts                  |
| Success              | The goal's success check, or the skill's default, passes                                            |
| Failure              | `update()` raises, as the protocol already specifies, or the goal's timeout expires                 |
| Cancel or preemption | `Skills` calls `disconnect()` on the source and holds the parts                                     |
| Feedback             | `Skills` reports elapsed time and the confidence of the success check; other facts use `WorldState` |

The same mechanism covers every skill:

| Skill                           | Source built for each goal                                  | Default success check          |
| ------------------------------- | ----------------------------------------------------------- | ------------------------------ |
| `PolicySkill`                   | `PolicySource` over the skill's model, with the goal's task | Configured per skill           |
| `TeleopSkill`                   | `TeleopSource` for the leader device                        | None: runs until cancelled     |
| `SetpointSkill` (look-at)       | Emits the target setpoint                                   | Target reached                 |
| `ServoSkill`                    | Steps toward the requested offset                           | Target reached                 |
| `DelegatedSkill` (Nav2, MoveIt) | Forwards the external controller's commands                 | The controller reports success |

`PolicySkill` is generic. Pick and place are configured instances of it, not subclasses:

```python
class PolicySkill:
    def source(self, goal):
        return PolicySource(self.model, task=self.task.format(**goal.params))
```

The model is loaded once, when the skill is constructed. Each goal receives a fresh
source, so no state carries over between goals.

Two consequences follow. First, completion is decided by a success check, never by the
source: a policy cannot tell when its task is done. Second, every skill's commands pass
through the loop, including those of external controllers, so `SafetyGate` and leases
apply to all of them. A skill without a success check runs until it is cancelled or
preempted, which covers long-lived sources such as teleoperation of the base.

**Ticks.** On each tick, the loop calls `Skills.update()`. `Skills` starts and stops
sources for pending goals, calls `update()` on each running source, evaluates success
checks, holds parts that have no writer, and returns the merged part-keyed action. All
skills inside one `Skills` object therefore run at the host loop's rate. Slower sources
hold between outputs, as `PolicySource` already does between action chunks.

**Leases.** Each part has at most one writer. Parts form a hierarchy, so a lease on
`whole_body` covers the arms and the base. A higher-priority goal may preempt a
lower-priority lease. If a lease misses its heartbeat, the goal is cancelled and the
parts hold position.

**`SkillHandle`.** A handle exposes `status`, `feedback`, `update`, `cancel`, and
`result`. `update` replaces the goal's parameters; `Skills` builds a new source from the
skill under the same lease.

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

An action model runs in one of two ways:

| Location          | Mechanism                                                         | Intended use                                                    |
| ----------------- | ----------------------------------------------------------------- | --------------------------------------------------------------- |
| In-process        | `InferenceModel` (existing)                                       | OpenVINO or ONNX models and other small models                  |
| Another partition | `RemoteInferenceModel(name=...)` and `InferenceServer` (existing) | Conflicting dependencies, crash isolation, a dedicated GPU host |

`RemoteInferenceModel` is a subclass of `InferenceModel`, so `PolicySource`,
`PolicySkill`, and every execution mode use it unchanged
([remote inference guide](../../../how-to/runtime/run-inference-remotely.md)). A
partition that serves a model may be a process in a separate Python environment
prepared by `physicalai.isolate`, which is how framework plugins with conflicting
dependencies run.

Remote System 1 inference is viable only when the action-chunk horizon exceeds the sum
of network round-trip time, inference time, and a safety margin. If a chunk arrives
late, the source holds and `Skills` reports starvation to the skill.

---

## 11. Communication Between Partitions

### Transport and namespace

All partitions communicate over Zenoh through `physicalai.transport`, the transport that
shared cameras, shared robots, and remote inference already use. Each partition opens a
Zenoh session and joins the robot namespace given by `robot_name`. Names passed to
clients and servers, such as `"g1"` or `"humanoid"`, resolve to keys inside that
namespace.

Zenoh offers two communication patterns, and each message uses the one that matches its
meaning:

- **Publish and subscribe** for streams in which only the latest value matters: robot
  state, setpoints, skill feedback, `WorldState` facts, heartbeats, and emergency stop.
- **Query and reply** for requests that need an answer: handshakes, metadata, goal
  submission, cancellation, lease requests, and inference requests.

### Links

| Link               | Client → server                            | Messages and pattern                                                                                                                    |
| ------------------ | ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| Robot command link | `SharedRobot` → `RobotServer`              | Query: metadata (parts, command modes) and lease requests. Publish: part-keyed setpoints from the client, robot state from the server   |
| Inference link     | `RemoteInferenceModel` → `InferenceServer` | Query: handshake, then one request per action chunk (existing)                                                                          |
| Skills link        | `SkillsClient` → `SkillsServer`            | Query: list, submit, update, cancel. Publish: skill feedback and events from the server, heartbeats from the client, `WorldState` facts |

**Robot command link.** `RobotServer` extends the existing `SharedRobot` owner protocol.
The existing protocol provides an owner that holds the hardware, state publication,
action subscription, discovery, and host-local exclusivity. The robot command link adds
part-keyed setpoints, each carrying a mode, values, a timestamp, a deadline, and a
lease. Setpoints are published as latest-value messages and are never retransmitted: a
late setpoint is useless, and the server rejects any setpoint that has passed its
deadline or carries the wrong lease. When several upper loops share one lower loop, the
server arbitrates between them by part. A native or vendor controller can implement the
server side directly.

**Inference link.** This link exists today. A handshake checks protocol version, policy
identity, and optionally the manifest hash; each request then carries an observation and
returns an action chunk within a timeout. The design adds a `ModelSpec` check to the
handshake.

**Skills link.** Goal submission is a query, so the client knows whether the goal was
accepted. Each goal carries a client-generated identifier, which makes a retried
submission idempotent. Feedback, status, and events are published so that any number of
clients can observe a skill. The client publishes heartbeats for its leases; missed
heartbeats expire them.

### Robot-wide messages

Messages that concern the whole robot are published in the robot namespace, so no
central object is required:

| Message        | Publisher                | Subscribers                   | Purpose                                                          |
| -------------- | ------------------------ | ----------------------------- | ---------------------------------------------------------------- |
| Presence       | Every runtime and server | Studio, CLI, other partitions | Discovery of the partitions and endpoints that make up the robot |
| Health         | Every runtime            | Studio, monitors              | Liveness of each loop, node, and link                            |
| Emergency stop | Any authorized peer      | Every runtime                 | Immediate stop; each `SafetyGate` holds or brakes                |

The emergency stop is an active message and is not the only stop mechanism. If it is
lost, the stale-command hold still stops every lower tier once the tier above falls
silent.

### Startup and reconnection

Each runtime starts its components only when the peers they name are reachable. A
`SharedRobot` client first queries the server's metadata and checks that the parts and
command modes it needs exist. A `RemoteInferenceModel` completes its handshake. A
`SkillsClient` lists the skills it expects. If a peer is missing after a timeout, the
runtime reports the failure and does not start the dependent component.

After a link is lost and restored, the upper tier must request a new lease before it can
command the robot again. Motion never resumes automatically after a reconnection.

### Time

Deadlines and staleness checks require a shared clock. Partitions on one SoC share the
host clock; partitions on separate machines synchronize through PTP or NTP. Each
handshake measures clock offset and round-trip time, and a link refuses to start when
the offset exceeds its bound. All messages carry timestamps, and `WorldState` uses them
to detect stale facts.

### Security

The existing Zenoh transports provide no application-level authentication; they rely on
loopback binding, SSH tunnels, or network isolation. Across hosts, the robot command and
skills links must authenticate their peers, and key-level access control must restrict
each partition to the keys it needs. Access control is also what enforces the partition
boundary: the System 2 partition may use the skills keys but not the robot command keys.

### Large data

Camera frames stay in the partition that consumes them where possible. When frames must
cross partitions, they use the existing shared-camera transport or the image encoding
that `RemoteInferenceModel` already applies. Between KVM guests, the transport runs over
the virtual network; shared-memory transport between guests depends on hypervisor
support and is deferred.

---

## 12. Safety and Recording

Safety is layered. Each layer is enforced independently of the layers above it.

| Layer                | Component                      | Enforces                                                      |
| -------------------- | ------------------------------ | ------------------------------------------------------------- |
| Hardware / firmware  | Robot                          | Power, torque, and temperature limits                         |
| Safety gate          | `SafetyGate`                   | Joint, velocity, workspace, and force limits on every command |
| Stale-command hold   | Lower loop                     | Holds or stops when the tier above goes silent                |
| Leases and deadlines | `Skills`, `RobotServer`        | A single writer per part                                      |
| Access policy        | `Skills`                       | Which clients may run which skills                            |
| Link authentication  | Robot command and skills links | Only authenticated peers send setpoints or submit goals       |
| Partition boundary   | Key-level access control       | The System 2 partition cannot reach the robot command keys    |
| Emergency stop       | Robot namespace                | Reaches every runtime of the robot                            |

The `unrestricted` access mode removes permission checks only. Safety gates, leases,
and deadlines remain in force.

| Failure              | Response                                                       |
| -------------------- | -------------------------------------------------------------- |
| Client disconnects   | Its lease expires; the skill is cancelled; parts hold          |
| System 2 crashes     | As above; System 1 and System 0 continue                       |
| Remote model is late | The source holds and reports starvation; the skill may fail    |
| System 1 goes silent | System 0 applies its stale-command hold                        |
| System 0 faults      | System 1 stops; running goals fail                             |
| Link lost            | Treated as silence; a new lease is required after reconnection |

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

| ROS 2 concept                              | Integration                                                               | Protocol       |
| ------------------------------------------ | ------------------------------------------------------------------------- | -------------- |
| `ros2_control` controller or ROS 2 driver  | `Ros2Robot`: a ROS 2 controller acts as System 0                          | `Robot`        |
| Command topic (joystick, external planner) | `Ros2Source`: a topic drives a loop                                       | `ActionSource` |
| Action server                              | `Ros2ActionServer`: each skill appears as a ROS 2 action                  | Skills link    |
| Action client (Nav2, MoveIt)               | `DelegatedSkill`: a ROS 2 stack plans; its commands pass through the loop | `Skill`        |
| TF and detection topics                    | A bridge that publishes and subscribes `WorldState` facts                 | `WorldState`   |

ROS 2 actions have the same structure as goals and skill handles (goal, feedback,
result, and cancel), so a ROS 2 application can call PhysicalAI skills natively, and a
PhysicalAI client can call ROS 2 stacks as delegated skills.

**Other integrations.** Execution backends, vendor SDKs, and other middleware can be
added in the same way: as `Robot`, `ActionSource`, `ActionModel`, `Skill`, or server
implementations.

---

## 14. Migration

The class that ships today as `RobotRuntime` is renamed to `ControlLoop`, and the name
`RobotRuntime` is reused for the partition runtime. The change is staged so that no
single release breaks existing code:

| Stage | Change                                                                                                                                                                                                                                                                                     | Effect on existing code                      |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------- |
| 1     | Add `ControlLoop`; `RobotRuntime` becomes an alias for it                                                                                                                                                                                                                                  | None                                         |
| 2     | Introduce the partition `RobotRuntime`. For one minor release it still accepts `robot`, `action_source`, and `fps`, warns, and builds a one-loop runtime that forwards `run`, `stop`, `connect`, and the existing properties. Configurations that name the old class resolve the same way. | Deprecation warning                          |
| 3     | Remove the compatibility path                                                                                                                                                                                                                                                              | Code must use `ControlLoop` for single loops |

Studio and the documentation move to `ControlLoop` during stage 1.

---

## 15. Requirements Coverage

| Requirement ([§2](#2-motivation))       | Mechanism                                                                               |
| --------------------------------------- | --------------------------------------------------------------------------------------- |
| 1. System 0, 1, and 2 on one robot      | Loops linked through `robot`; agents and other clients call `Skills`                    |
| 2. Frontier model drives System 1 or 0  | Goals to policy skills; `ServoSkill` setpoints to System 0                              |
| 3. Any deployment layout                | Object links in one partition, named links across partitions, in one robot namespace    |
| 4. Models from any backend and hardware | Models owned by skills and agents; in-process or served by name; frameworks in plugins  |
| 5. Concurrent work on disjoint parts    | Leases and part-keyed actions in `Skills`                                               |
| 6. Teleoperation, HIL, and DAgger       | `TeleopSource`; `TeleopSkill` preemption; intervention segments in episodes             |
| 7. Non-bypassable safety                | `SafetyGate`; stale-command hold; leases; authenticated links; key-level access control |
| 8. Labeled episode per run              | Skill events on the existing callback bus                                               |
| 9. Python, YAML, and CLI equivalent     | Every component, including every link, is a constructor argument                        |
| 10. ROS 2 without dependency            | Edge integrations implementing existing protocols ([§13](#13-integrations))             |
| 11. Gradual migration                   | Staged rename with a compatibility release ([§14](#14-migration))                       |

---

## 16. Open Questions and Deferred Decisions

**Open questions**

1. **Rates within one tier.** All skills in one `Skills` object run at the host loop's
   rate. Workloads that need separate rates or processes, such as navigation and
   manipulation, can run as two System 1 loops sharing one System 0 through
   `RobotServer`, which arbitrates by part.
2. **Existing key layouts.** Whether the existing inference and shared-robot keys move
   under the robot namespace, or keep their current layout with names unique per robot.

**Deferred decisions**

| Topic                                                  | Needed by                  |
| ------------------------------------------------------ | -------------------------- |
| Command modes and payloads                             | Part-keyed actions         |
| Message schemas for the robot command and skills links | Multi-partition deployment |
| Key layout of the robot namespace                      | Multi-partition deployment |
| Authentication and access-control mechanism            | Cross-host deployment      |
| Lease durations and priorities                         | `Skills`                   |
| Access-policy format                                   | Multiple clients           |
| Real-time platform for System 0                        | Hosted heavy System 0      |
| Clock-offset bounds                                    | Multi-partition deployment |
| Shared memory between KVM guests                       | Guest-to-guest bandwidth   |
| Deployment file format                                 | `physicalai up`            |
| Recording format extensions                            | Data collection at scale   |
| Package location of `Agent`                            | System 2 clients           |
| World-model interface                                  | Imagine-and-rank planning  |

---

## 17. Implementation Phases

| Phase | Scope                                                                                                          | Exit criterion                                            |
| ----- | -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| 1     | `ControlLoop` with `RobotRuntime` alias; part-keyed actions; `start()`                                         | Existing tests, configurations, and Studio pass           |
| 2     | `SafetyGate`; a loop as the robot of another loop; partition `RobotRuntime`                                    | System 0 and System 1 in one process; gate tests pass     |
| 3     | `Skills`, `Goal`, `PolicySkill`, `TeleopSkill`, leases, skill events                                           | Tabletop task from Python and YAML; HIL takeover recorded |
| 4     | `ModelSpec`; skills with `RemoteInferenceModel` and isolated-environment model servers                         | Identical actions in-process and served                   |
| 5     | `SkillsServer`, `SkillsClient`, `McpServer`; reasoning clients; `Agent`                                        | Cloud agent and MCP host drive the same robot             |
| 6     | `RobotServer` on the `SharedRobot` protocol; robot namespace; presence, health, emergency stop; authentication | System 0 and System 1 in separate processes and hosts     |
| 7     | KVM partitions; key-level access control; ROS 2 integrations                                                   | Humanoid across guests; ROS 2 application calls skills    |
| 8     | Delegated navigation; perceivers; world model                                                                  | Navigate, pick, and place in one task                     |

Phases 1 to 3 deliver value without the later phases. The compatibility path of the
old `RobotRuntime` constructor is removed one minor release after phase 2.

---

## 18. Decisions

```text
story           Agent → Goal → Skill → SkillHandle; Skills drives a ControlLoop
tiers           System 0 control, System 1 policy, System 2 deliberation
loop            ControlLoop runs one tier at one rate (the class formerly named RobotRuntime)
runtime         RobotRuntime runs one partition: loops, nodes, servers, lifecycle, health
robot           a namespace shared by its partitions; discovery, health, emergency stop
composition     the tier below a loop is its robot: hardware, a loop, or a SharedRobot
links           explicit: an object in the same partition, a client by name across partitions
transport       Zenoh; publish-subscribe for latest-value streams, queries for requests
clients         anything that submits goals; Agent is the built-in reasoning-model client
skills          a skill turns a goal into an ActionSource; Skills runs it on the claimed parts
completion      decided by a success check, never by the source
actions         part-keyed command maps; flat arrays remain valid
models          owned by skills and agents; in-process or served by name
safety          SafetyGate; leases and deadlines; authenticated links; key-level access control
intervention    HIL is preemption by a higher-priority teleoperation skill
integrations    at the edges, implementing existing protocols; the core depends on none
migration       staged rename with one compatibility release
terminology     robot skill (Skill) is distinct from agent skill (SKILL.md)
```
