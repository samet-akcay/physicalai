# Tiered Robot Runtime: System 0 / 1 / 2

**Status:** Proposal for design review.

This document describes how PhysicalAI runs three tiers of robot decision-making: System 0
(control), System 1 (reactive policy), and System 2 (deliberation). Models may come from
any backend, and the tiers may share one process or be spread across accelerators, KVM
guests, or separate machines. Components may be written in other languages, such as
C++. The design is hardware-agnostic and does not depend on any external middleware.

The document specifies architecture only. Message schemas, key layouts, and numeric
limits are deferred ([§17](#17-open-questions-and-deferred-decisions)). Code is
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
11. [Time and Freshness](#11-time-and-freshness)
12. [Communication Between Partitions](#12-communication-between-partitions)
13. [Safety and Recording](#13-safety-and-recording)
14. [Integrations](#14-integrations)
15. [Migration](#15-migration)
16. [Requirements Coverage](#16-requirements-coverage)
17. [Open Questions and Deferred Decisions](#17-open-questions-and-deferred-decisions)
18. [Implementation Phases](#18-implementation-phases)
19. [Decisions](#19-decisions)

---

## 1. Summary

The design rests on five ideas:

- A **`ControlLoop`** runs one tier at a fixed rate. On each tick it reads its robot, asks
  its action source for an action, passes the action through its callbacks, and sends
  it. This is the class that ships today as `RobotRuntime`, renamed
  ([§15](#15-migration)).
- A **`RobotRuntime`** runs everything in one partition (a process, VM, or machine): its
  control loops, other components such as agents, and the servers that expose them. It
  starts them in order, stops them in order, and reports their health.
- **Components connect by object or by name.** In the same process, a component receives
  the object it depends on. In another process, it receives a client that refers to the
  object by name.
- **A robot is a namespace.** All partitions of one robot join a shared namespace named
  after the robot. Discovery, health, and emergency stop are messages in that namespace,
  so the whole robot can be observed and stopped without a central object.
- **Time is a contract.** Every value carries the capture time of the observations
  behind it. Components declare their rates, latencies, and the oldest inputs they
  accept. The runtime derives a timing graph from the objects it is given and verifies,
  before any motion, that every action will be fresh enough. Users never declare nodes
  or edges.

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
8. Bound the age of every action, and verify before motion that the deployment meets
   those bounds.
9. Record a labeled episode for every run.
10. Keep Python construction, YAML, and the CLI equivalent.
11. Integrate with ROS 2 and other ecosystems without depending on them.
12. Accept components written in other languages, such as C++, in the same process or
    in another partition.
13. Migrate existing code without an abrupt break.

**Out of scope.** Hypervisors, hard real-time control in Python (loops at 200 Hz and
above remain native or vendor-provided), agent frameworks, general-purpose middleware,
and training.

---

## 3. Terminology

Prose uses lowercase for concepts (client, goal, skill). Code font denotes classes.
Tiers are written System 0, 1, and 2 in prose and `s0`, `s1`, and `s2` in code and
configuration. "Skill" means robot skill unless qualified otherwise.

**Structure**

| Term                | Definition                                                                                                                           |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| **Tier**            | System 0, 1, or 2. A responsibility and rate band, not a process.                                                                    |
| **`ControlLoop`**   | Runs one tier at a fixed rate: read the robot, query the action source, apply callbacks, send.                                       |
| **`RobotRuntime`**  | Runs everything in one partition: loops, nodes, and servers, with their lifecycle.                                                   |
| **Node**            | A component hosted by a runtime that is not a control loop, such as an agent or a perceiver. Runs on its own thread at its own rate. |
| **Partition**       | A unit of isolation and deployment: a process, container, VM, or machine.                                                            |
| **Robot namespace** | The shared key space of one robot's partitions, named after the robot.                                                               |
| **Link**            | A connection between two components: an object reference, or a client and server by name.                                            |

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

**Time**

| Term               | Definition                                                                                                            |
| ------------------ | --------------------------------------------------------------------------------------------------------------------- |
| **Capture time**   | When the measurement behind a value was taken. A computed value inherits the oldest capture time of its observations. |
| **Age**            | The current time minus the capture time.                                                                              |
| **`max_age`**      | The oldest value a component accepts on an input.                                                                     |
| **Latency budget** | The largest acceptable age of an action at a loop, measured from capture at a named source.                           |
| **Timing graph**   | The rates, latencies, and inputs of a partition's components, derived by the runtime. Never written by users.         |

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
9. **Every value carries its capture time.** Ages are measured and bounded, not assumed.
10. **The timing graph is derived, not declared.** Its edges are the object references
    and named links of rule 4; the runtime checks it before starting.
11. **Every client goes through `Skills`.** There is no privileged path.
12. **Integrations sit at the edges and implement these protocols.** The core never
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
detector = Detector(camera=cameras["head"], model=InferenceModel("./exports/detector"),
                    rate=5, world=skills.world)

RobotRuntime(
    robot_name="g1-lab3",
    loops={"s0": s0, "s1": s1},
    nodes={"detector": detector, "planner": Agent(planner_model, skills)},
    serve=[SkillsServer(skills, name="humanoid")],
    budgets=[Budget(source="head", to="s0", max_age=0.30)],
).run()
```

`s0` has no action source, so it takes setpoints from `s1`, which uses it as its robot.
The runtime starts `s0` before `s1` and stops them in the reverse order. The detector
runs on its own thread at 5 Hz and writes stamped detections to `WorldState`, where
success checks and the agent read them.

No graph is written, yet the runtime derives one from the arguments: the head camera
feeds `s1` and the detector, `s1` drives `s0`, and the detector feeds `WorldState`.
Before starting, it verifies that actions reach `s0` within 300 ms of the head camera's
capture. Skills on disjoint parts run concurrently under separate leases:

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

# robot-main: System 1, skills, and perception
skills = Skills([
    PolicySkill(name="manipulate", model=RemoteInferenceModel(name="bimanual"),
                claims={"left_arm", "right_arm"}),
    DelegatedSkill(name="walk_to", controller=Nav2(), claims={"base"}),
    SetpointSkill(name="look_at", claims={"head"}),
])
s1 = ControlLoop(robot=SharedRobot.attach("g1"), action_source=skills, fps=50, cameras=cameras)
RobotRuntime(
    robot_name="g1-lab3",
    loops={"s1": s1},
    nodes={"detector": Detector(camera=cameras["head"], model=InferenceModel("./exports/detector"),
                                rate=5, world=skills.world)},
    serve=[SkillsServer(skills, name="humanoid")],
    budgets=[Budget(source="head", to="g1", max_age=0.30)],
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

Compared with 5.8, four arguments change: `s1` takes `SharedRobot.attach("g1")` instead
of `s0`, the skill takes `RemoteInferenceModel(name="bimanual")` instead of an
`InferenceModel`, the agent takes a `SkillsClient` instead of `skills`, and the budget
ends at the robot link `"g1"` instead of the loop `"s0"`. The timing checks are the
same: the robot link and the inference link advertise the remote side's timing in their
handshakes, so `robot-main` checks the budget across partitions.
Each partition can equally be a YAML file run with `physicalai run --config`. An optional
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

A loop runs on its own as well as inside a `RobotRuntime`, so `run()` and `start()`
apply the timing checks of [§11](#11-time-and-freshness) to the loop's own components
before the first tick.

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
        budgets: Sequence[Budget] = (),                   # end-to-end latency budgets
    ): ...
    @classmethod
    def from_config(cls, config: str | Path) -> Self: ...
    def check(self) -> TimingReport: ...                  # timing and type checks, without starting
    def run(self) -> None: ...
    def stop(self, *, emergency: bool = False) -> None: ...
    health: Mapping[str, Health]
```

| Responsibility | Behavior                                                                                              |
| -------------- | ----------------------------------------------------------------------------------------------------- |
| Lifecycle      | Starts loops from the bottom tier up, then nodes, then servers; stops in the reverse order            |
| Dependencies   | Waits, with a timeout, for every named peer its components require before starting them               |
| Timing         | Derives the timing graph, checks types, ages, budgets, and rates, and refuses to start on a failure   |
| Endpoints      | Hosts the servers in `serve`; each server names the object it exposes                                 |
| Health         | Reports liveness and measured ages of its loops, nodes, and links in the robot namespace              |
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

**`WorldState`.** `WorldState` stores the latest value of each fact as a `Stamped`
value, with one writer per key and no history. `Skills` writes robot state and camera
frames from each tick. Perceivers write detections and captions. Skills, success
checks, and clients read from it and declare the oldest fact they accept, so a success
check never passes on a stale detection.

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
command modes its outputs drive. For each observation input, it also declares the rate
the model was trained at and the oldest observation it accepts, which becomes the
default `max_age` of any source that runs the model. The startup checks of
[§11](#11-time-and-freshness) compare each spec with the loop's cameras and the robot's
parts. Plugins convert native formats, such as LeRobot features and GR00T
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
of network round-trip time, inference time, and a safety margin. The timing check
verifies this at startup from the measured round-trip and inference times
([§11](#11-time-and-freshness)). If a chunk arrives late at run time, the source holds
and `Skills` reports starvation to the skill.

---

## 11. Time and Freshness

Every action a robot executes is computed from observations of the past. The runtime
makes the age of each action explicit, bounds it, and verifies before any motion that
the deployment can meet those bounds. Users do not describe a graph to obtain these
checks. The runtime derives the graph from the objects it is given.

### Capture time

Every value carries the time at which the measurement behind it was taken. Frames and
robot observations already carry this time (**existing**: `Frame.timestamp` and
`RobotObservation.timestamp`). The design extends it to every value computed from them:

```python
@dataclass(frozen=True)
class Stamped(Generic[T]):
    value: T
    captured_at: float      # capture time of the observations the value was computed from
    source: str             # the component that produced the value
```

A computed value inherits the capture time of the oldest observation it was computed
from. Goals, task strings, and other slowly changing inputs do not contribute, so an
action computed from a fresh frame and a goal set two seconds earlier is as old as the
frame. The age of a value is the current time minus its capture time. Links carry
capture time rather than send time ([§12](#12-communication-between-partitions)).

### Timing declarations

Each component declares its rate, its latency, and the inputs it reads. Component
authors implement the declaration. Users see only the constructor arguments that
override its defaults.

```python
@dataclass(frozen=True)
class Timing:
    rate: float | None                  # Hz; None for event-driven components
    latency: float                      # seconds per step; declared, or measured at warm-up
    inputs: Mapping[str, Input]

@dataclass(frozen=True)
class Input:
    source: object                      # the object the component reads from
    max_age: float | None               # oldest acceptable value; None for no limit
    observation: bool = True            # whether outputs inherit this input's capture time

class Timed(Protocol):
    def timing(self) -> Timing: ...
```

| Component                                             | Rate                                    | Latency                              | Inputs                                                                                       |
| ----------------------------------------------------- | --------------------------------------- | ------------------------------------ | -------------------------------------------------------------------------------------------- |
| Camera                                                | Its frame rate                          | Declared by the backend              | None                                                                                         |
| `Robot`                                               | Its state rate                          | Declared by the driver               | Setpoints from the loop that drives it                                                       |
| `ControlLoop`                                         | `fps`                                   | Measured tick time                   | Its robot's state, its cameras, and its action source                                        |
| `PolicySource`                                        | Loop rate, or policy rate when async    | Measured at warm-up                  | Cameras and robot state, with `max_age` from the `ModelSpec`; the task is not an observation |
| `Skills`                                              | Loop rate                               | Measured tick time                   | The sources of running goals; goals are not observations                                     |
| Node                                                  | Its own rate                            | Measured                             | The objects passed to its constructor                                                        |
| `SharedRobot`, `RemoteInferenceModel`, `SkillsClient` | The peer's, advertised in the handshake | The peer's, plus measured round trip | The peer's inputs, as advertised                                                             |

`max_age` is overridden where the input is consumed, for example
`PolicySource(model, max_age=0.15)`. A single number applies to every observation
input; a mapping applies per input.

### The timing graph

At startup, the runtime follows each component's inputs and builds a timing graph. The
graph has an edge exactly where one object was passed to another: a camera to a loop, a
loop to the loop that uses it as a robot, a camera to a node, a model to a source. The
graph is internal. Users never write, configure, or place it.

For each input, the runtime computes a worst-case age. A value read from a producer is
at most the producer's worst-case output age plus one producer period. A component's
output age is the largest worst-case age among its observation inputs plus its own
latency. A link adds its measured transit time. The runtime then applies four checks:

| Check     | Fails when                                                                               |
| --------- | ---------------------------------------------------------------------------------------- |
| Types     | A model's `ModelSpec` names an input the loop does not provide, or drives a missing part |
| Freshness | The worst-case age of an input exceeds its `max_age`                                     |
| Budgets   | The worst-case age along a path exceeds the latency budget declared for it               |
| Rates     | A component's latency exceeds its period                                                 |

A latency budget bounds the age of an action at a loop, measured from capture at a named
source. `to` names a loop in the partition or a robot link to another partition:

```python
RobotRuntime(..., budgets=[Budget(source="head", to="s0", max_age=0.30)])
```

A runtime that fails a check does not start. `physicalai check` runs the same checks
without starting, from Python or YAML:

```text
$ physicalai check --config robot-main.yaml
g1-lab3 / robot-main
  head (30 Hz)  → s1 manipulate          worst  64 ms   max_age 150 ms   ok
  s1 (50 Hz)    → s0 via robot link "g1" worst  91 ms   max_age  50 ms   FAIL
  budget head   → s0                     worst 112 ms   budget  300 ms   ok
  manipulate: ModelSpec expects camera "wrist"; s1 provides "head"       FAIL
```

Each partition checks the paths that start in it. A server advertises the timing of its
side of a link during the handshake: rate, latency, and worst-case output age. A client
therefore includes the remote part of a path without a global graph.

### At run time

- **Stale inputs hold.** Each component sees the age of its inputs. A stale observation
  makes an action source hold rather than act on old data, which the existing
  `ActionSource` contract already permits. Persistent staleness fails the running goal.
- **Deadlines follow from ages.** The deadline of a setpoint is its capture time plus
  the consumer's `max_age`. Lower loops and `RobotServer` reject setpoints past their
  deadline.
- **Predictions are monitored.** Measured ages and latencies are published as health
  and recorded with each episode. A component whose measured latency exceeds its
  declaration is reported.

Action sources keep their existing signature: the robot observation and frames passed
to `update()` already carry capture times. A source may report the capture time of the
action it returns, as `PolicySource` does for an action from an earlier chunk.
Otherwise the loop assumes the capture time of the current observation.

**Threads.** Each loop and node in a partition runs on its own thread at its own rate,
so a 5 Hz detector and a 500 Hz loop do not share a schedule. Work that holds the
interpreter for long periods runs in another process behind a server, and the timing
graph spans that link through the handshake.

---

## 12. Communication Between Partitions

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

Messages are encoded as MessagePack with arrays tagged by dtype and shape, and camera
frames as a packed binary header followed by pixel data (**existing**). Neither
encoding depends on Python. Each link has a versioned message schema that is published
with conformance tests, so a peer in any language with a Zenoh binding can implement
either side of the link ([§14](#14-integrations)).

### Links

| Link               | Client → server                            | Messages and pattern                                                                                                                    |
| ------------------ | ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| Robot command link | `SharedRobot` → `RobotServer`              | Query: metadata (parts, command modes) and lease requests. Publish: part-keyed setpoints from the client, robot state from the server   |
| Inference link     | `RemoteInferenceModel` → `InferenceServer` | Query: handshake, then one request per action chunk (existing)                                                                          |
| Skills link        | `SkillsClient` → `SkillsServer`            | Query: list, submit, update, cancel. Publish: skill feedback and events from the server, heartbeats from the client, `WorldState` facts |

**Robot command link.** `RobotServer` extends the existing `SharedRobot` owner protocol.
The existing protocol provides an owner that holds the hardware, state publication,
action subscription, discovery, and host-local exclusivity. The robot command link adds
part-keyed setpoints, each carrying a mode, values, a capture time, a deadline, and a
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
| Health         | Every runtime            | Studio, monitors              | Liveness and measured ages of each loop, node, and link          |
| Emergency stop | Any authorized peer      | Every runtime                 | Immediate stop; each `SafetyGate` holds or brakes                |

The emergency stop is an active message and is not the only stop mechanism. If it is
lost, the stale-command hold still stops every lower tier once the tier above falls
silent.

### Startup and reconnection

Each runtime starts its components only when the peers they name are reachable. A
`SharedRobot` client first queries the server's metadata and checks that the parts and
command modes it needs exist. A `RemoteInferenceModel` completes its handshake. A
`SkillsClient` lists the skills it expects. Every handshake also exchanges the server's
timing (rate, latency, and worst-case output age) for the timing checks. If a peer is
missing after a timeout, the runtime reports the failure and does not start the
dependent component.

After a link is lost and restored, the upper tier must request a new lease before it can
command the robot again. Motion never resumes automatically after a reconnection.

### Time

Every message carries capture time, not send time. Capture times are monotonic times on
the host that took the measurement (**existing**), sent as integer nanoseconds, and
monotonic clocks on different hosts are unrelated. Each link therefore measures the clock offset and round-trip time
during the handshake, refreshes both with its heartbeats, and converts capture times to
the receiver's clock before ages are computed. Partitions on one SoC share the host
clock and need no conversion. Hosts that synchronize through PTP or NTP keep the offset
stable. A link refuses to start, or reports degraded health, when the uncertainty of the
offset exceeds its bound.

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

## 13. Safety and Recording

Safety is layered. Each layer is enforced independently of the layers above it.

| Layer                | Component                      | Enforces                                                      |
| -------------------- | ------------------------------ | ------------------------------------------------------------- |
| Hardware / firmware  | Robot                          | Power, torque, and temperature limits                         |
| Safety gate          | `SafetyGate`                   | Joint, velocity, workspace, and force limits on every command |
| Stale-command hold   | Lower loop                     | Holds or stops when the tier above goes silent                |
| Freshness            | Every consumer                 | Holds on stale inputs; rejects setpoints older than `max_age` |
| Timing check         | `ControlLoop`, `RobotRuntime`  | Refuses to start when ages or budgets cannot be met           |
| Leases and deadlines | `Skills`, `RobotServer`        | A single writer per part                                      |
| Access policy        | `Skills`                       | Which clients may run which skills                            |
| Link authentication  | Robot command and skills links | Only authenticated peers send setpoints or submit goals       |
| Partition boundary   | Key-level access control       | The System 2 partition cannot reach the robot command keys    |
| Emergency stop       | Robot namespace                | Reaches every runtime of the robot                            |

The `unrestricted` access mode removes permission checks only. Safety gates, leases,
and deadlines remain in force.

| Failure              | Response                                                         |
| -------------------- | ---------------------------------------------------------------- |
| Client disconnects   | Its lease expires; the skill is cancelled; parts hold            |
| System 2 crashes     | As above; System 1 and System 0 continue                         |
| Remote model is late | The source holds and reports starvation; the skill may fail      |
| System 1 goes silent | System 0 applies its stale-command hold                          |
| System 0 faults      | System 1 stops; running goals fail                               |
| Link lost            | Treated as silence; a new lease is required after reconnection   |
| Timing check fails   | The runtime does not start and reports the failing input or path |
| Measured age too old | The consumer holds; health reports the input and its age         |

**Recording.** `Skills` receives its loop's callback bus when it connects, as every
action source does. It emits an event when a skill starts, is preempted, and ends. A
recording callback uses these events to produce one episode per skill run. The episode
contains the loop data in LeRobot format, the goal, the submitting client, the outcome,
model provenance, intervention segments, the capture time and age of every action, and
the agent transcript when one exists. Failed and cancelled runs are recorded in the same
way as successful ones.

---

## 14. Integrations

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

**Components in other languages.** A component may be written in another language, such
as C++ for efficiency, in one of two ways.

_In the same process._ A native class exposed through Python bindings, such as nanobind
or pybind11, implements one of the Python protocols: `Robot`, `ActionSource`,
`ActionModel`, a camera, or a node. It is constructed, configured from YAML, and linked
like any other object, and its `timing()` reports its native rate and latency. A native
controller that runs its own high-rate loop appears as the `Robot` below a Python loop:

```python
wbc = NativeWholeBodyController(model="g1.urdf", rate=1000)   # C++, own 1 kHz thread
s1 = ControlLoop(robot=SafetyGate(wbc, limits), action_source=skills, fps=50)
```

A native component releases the interpreter lock while it computes, so that it does
not stall the loops that share its process.

_In another partition._ A native process implements one side of a link through the
Zenoh binding for its language. The Python side refers to it by name and is unchanged:

| Native partition implements                                                | Python side uses                         |
| -------------------------------------------------------------------------- | ---------------------------------------- |
| Server side of the robot command link: System 0, WBC, or vendor controller | `SharedRobot.attach("g1")`               |
| Server side of the inference link                                          | `RemoteInferenceModel(name="bimanual")`  |
| `WorldState` facts on the skills link: perception, SLAM                    | Success checks and agents read the facts |
| Client side of the skills link                                             | `SkillsServer(skills, name="humanoid")`  |

A native partition does not run `RobotRuntime`, but it behaves as a partition. It
completes the handshakes of its links, including protocol version, metadata, timing,
and clock offset. It stamps every message with capture time. It publishes presence and
health in the robot namespace and obeys emergency stop. It authenticates its peers and
uses only the keys its access rights allow.

Two constraints follow. First, every link schema is language-neutral and published with
conformance tests that native peers run. Second, a native loop does not run Python
callbacks, so recording subscribes to the state the loop publishes. The equivalence of
Python, YAML, and the CLI applies to Python partitions. A native partition has its own
configuration but uses the same robot, server, and part names.

**Other integrations.** Execution backends, vendor SDKs, and other middleware can be
added in the same way: as `Robot`, `ActionSource`, `ActionModel`, `Skill`, or server
implementations.

---

## 15. Migration

The class that ships today as `RobotRuntime` is renamed to `ControlLoop`, and the name
`RobotRuntime` is reused for the partition runtime. The change is staged so that no
single release breaks existing code:

| Stage | Change                                                                                                                                                                                                                                                                                     | Effect on existing code                      |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------- |
| 1     | Add `ControlLoop`; `RobotRuntime` becomes an alias for it                                                                                                                                                                                                                                  | None                                         |
| 2     | Introduce the partition `RobotRuntime`. For one minor release it still accepts `robot`, `action_source`, and `fps`, warns, and builds a one-loop runtime that forwards `run`, `stop`, `connect`, and the existing properties. Configurations that name the old class resolve the same way. | Deprecation warning                          |
| 3     | Remove the compatibility path                                                                                                                                                                                                                                                              | Code must use `ControlLoop` for single loops |

Studio and the documentation move to `ControlLoop` during stage 1.

The timing checks start in reporting mode. In stage 1, an existing loop prints its
timing report but is not prevented from starting. From stage 2, a failed check prevents
the start, and a configuration may opt out explicitly during the compatibility release.

---

## 16. Requirements Coverage

| Requirement ([§2](#2-motivation))       | Mechanism                                                                                       |
| --------------------------------------- | ----------------------------------------------------------------------------------------------- |
| 1. System 0, 1, and 2 on one robot      | Loops linked through `robot`; agents and other clients call `Skills`                            |
| 2. Frontier model drives System 1 or 0  | Goals to policy skills; `ServoSkill` setpoints to System 0                                      |
| 3. Any deployment layout                | Object links in one partition, named links across partitions, in one robot namespace            |
| 4. Models from any backend and hardware | Models owned by skills and agents; in-process or served by name; frameworks in plugins          |
| 5. Concurrent work on disjoint parts    | Leases and part-keyed actions in `Skills`                                                       |
| 6. Teleoperation, HIL, and DAgger       | `TeleopSource`; `TeleopSkill` preemption; intervention segments in episodes                     |
| 7. Non-bypassable safety                | `SafetyGate`; stale-command hold; leases; authenticated links; key-level access control         |
| 8. Bounded action age                   | Capture times; `max_age`; budgets; timing graph derived and checked at startup                  |
| 9. Labeled episode per run              | Skill events on the existing callback bus                                                       |
| 10. Python, YAML, and CLI equivalent    | Every component, link, and budget is a constructor argument                                     |
| 11. ROS 2 without dependency            | Edge integrations implementing existing protocols ([§14](#14-integrations))                     |
| 12. Components in other languages       | Bindings to the Python protocols; link schemas with conformance tests ([§14](#14-integrations)) |
| 13. Gradual migration                   | Staged rename and timing checks in reporting mode ([§15](#15-migration))                        |

---

## 17. Open Questions and Deferred Decisions

**Open questions**

1. **Rates within one tier.** Nodes run at their own rates, but all skills in one
   `Skills` object run at the host loop's rate. Workloads that need separate rates or
   processes, such as navigation and manipulation, can run as two System 1 loops sharing
   one System 0 through `RobotServer`, which arbitrates by part.
2. **Existing key layouts.** Whether the existing inference and shared-robot keys move
   under the robot namespace, or keep their current layout with names unique per robot.
3. **Declared and measured latency.** Whether the timing checks use declared worst-case
   latencies, latencies measured at warm-up, or both, and what margin a measurement
   requires.

**Deferred decisions**

| Topic                                               | Needed by                                |
| --------------------------------------------------- | ---------------------------------------- |
| Command modes and payloads                          | Part-keyed actions                       |
| Link schemas and conformance tests                  | Multi-partition deployment; native peers |
| Native library for the link protocols               | Vendor and native partitions             |
| Key layout of the robot namespace                   | Multi-partition deployment               |
| Authentication and access-control mechanism         | Cross-host deployment                    |
| Lease durations and priorities                      | `Skills`                                 |
| Access-policy format                                | Multiple clients                         |
| Real-time platform for System 0                     | Hosted heavy System 0                    |
| Clock-offset bounds                                 | Multi-partition deployment               |
| Default `max_age` per component type                | Timing checks                            |
| Timing declarations of vendor and ROS 2 controllers | Integrations                             |
| Shared memory between KVM guests                    | Guest-to-guest bandwidth                 |
| Deployment file format                              | `physicalai up`                          |
| Recording format extensions                         | Data collection at scale                 |
| Package location of `Agent`                         | System 2 clients                         |
| World-model interface                               | Imagine-and-rank planning                |

---

## 18. Implementation Phases

| Phase | Scope                                                                                                                                                                              | Exit criterion                                                                     |
| ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| 1     | `ControlLoop` with `RobotRuntime` alias; part-keyed actions; `start()`; capture times; timing report                                                                               | Existing tests, configurations, and Studio pass                                    |
| 2     | `SafetyGate`; a loop as the robot of another loop; partition `RobotRuntime`; timing checks and budgets                                                                             | System 0 and System 1 in one process; predicted ages match measured ages           |
| 3     | `Skills`, `Goal`, `PolicySkill`, `TeleopSkill`, leases, skill events                                                                                                               | Tabletop task from Python and YAML; HIL takeover recorded                          |
| 4     | `ModelSpec`; skills with `RemoteInferenceModel` and isolated-environment model servers                                                                                             | Identical actions in-process and served                                            |
| 5     | `SkillsServer`, `SkillsClient`, `McpServer`; reasoning clients; `Agent`                                                                                                            | Cloud agent and MCP host drive the same robot                                      |
| 6     | `RobotServer` on the `SharedRobot` protocol; robot namespace; presence, health, emergency stop; authentication; timing in handshakes; published link schemas and conformance tests | System 0 and System 1 in separate processes and hosts; budgets checked across them |
| 7     | KVM partitions; key-level access control; ROS 2 integrations; a native System 0 partition                                                                                          | Humanoid across guests with a C++ System 0; ROS 2 application calls skills         |
| 8     | Delegated navigation; perceivers; world model                                                                                                                                      | Navigate, pick, and place in one task                                              |

Phases 1 to 3 deliver value without the later phases. The compatibility path of the
old `RobotRuntime` constructor is removed one minor release after phase 2.

---

## 19. Decisions

```text
story           Agent → Goal → Skill → SkillHandle; Skills drives a ControlLoop
tiers           System 0 control, System 1 policy, System 2 deliberation
loop            ControlLoop runs one tier at one rate (the class formerly named RobotRuntime)
runtime         RobotRuntime runs one partition: loops, nodes, servers, lifecycle, health
robot           a namespace shared by its partitions; discovery, health, emergency stop
composition     the tier below a loop is its robot: hardware, a loop, or a SharedRobot
links           explicit: an object in the same partition, a client by name across partitions
transport       Zenoh; publish-subscribe for latest-value streams, queries for requests
time            every value carries capture time; components declare rate, latency, max_age
timing graph    derived from object references and links; checked before motion; never written
clients         anything that submits goals; Agent is the built-in reasoning-model client
skills          a skill turns a goal into an ActionSource; Skills runs it on the claimed parts
completion      decided by a success check, never by the source
actions         part-keyed command maps; flat arrays remain valid
models          owned by skills and agents; in-process or served by name
safety          SafetyGate; leases and deadlines; authenticated links; key-level access control
intervention    HIL is preemption by a higher-priority teleoperation skill
integrations    at the edges, implementing existing protocols; the core depends on none
languages       any: in-process through bindings to the protocols, or as a partition on the link schemas
migration       staged rename with one compatibility release
terminology     robot skill (Skill) is distinct from agent skill (SKILL.md)
```
