# Tiered Robot Runtime: System 0 / 1 / 2

**Status:** Proposal for design review. Code is pseudo-code unless marked **existing**.

## Contents

1. [Why](#1-why)
2. [Requirements](#2-requirements)
3. [Overview](#3-overview)
4. [Walkthrough](#4-walkthrough)
5. [Components](#5-components)
6. [Communication Between Partitions](#6-communication-between-partitions)
7. [Efficiency](#7-efficiency)
8. [Safety and Recording](#8-safety-and-recording)
9. [Integrations](#9-integrations)
10. [Migration](#10-migration)
11. [Open Questions and Deferred Decisions](#11-open-questions-and-deferred-decisions)
12. [Implementation Phases](#12-implementation-phases)
13. [Decisions](#13-decisions)

---

## 1. Why

Robots combine three tiers of decision-making that run at different rates, often on
different hardware:

| Tier         | Role                               | Rate                   | Examples                               |
| ------------ | ---------------------------------- | ---------------------- | -------------------------------------- |
| **System 2** | Plan, sequence, and verify         | Event-driven, 0.1–5 Hz | LLMs and VLMs, local or in the cloud   |
| **System 1** | Map observations to motion         | 10–100 Hz              | VLA, ACT, diffusion, navigation        |
| **System 0** | Track setpoints and enforce limits | 200 Hz – 1 kHz         | WBC, IK, impedance, vendor controllers |

PhysicalAI runs one tier today: `RobotRuntime`, a fixed-rate loop with one action source
(a policy or teleoperation), callbacks, and YAML that mirrors the constructor. This
document extends it to all three tiers, in one process or across accelerators, KVM
guests, and machines, while keeping the Python API and running efficiently on
constrained hardware.

## 2. Requirements

The runtime must:

1. Run System 0, 1, and 2 on one robot. ([§4.7](#47-a-humanoid-on-one-host))
2. Let a frontier model drive System 1 or System 0.
   ([§4.5](#45-an-agent-or-an-mcp-host))
3. Run in one process, across accelerators, in KVM guests, or on separate machines,
   including partitions owned by a vendor.
   ([§4.8](#48-the-same-humanoid-across-partitions))
4. Accept models from any backend, on any hardware.
   ([§4.9](#49-models-from-any-backend))
5. Let workloads use disjoint parts of the robot concurrently.
   ([§5.4](#54-goals-and-skills))
6. Support teleoperation, human-in-the-loop intervention, and DAgger.
   ([§4.4](#44-human-takeover-hil-and-dagger))
7. Enforce safety that no component, client, or peer can bypass.
   ([§8](#8-safety-and-recording))
8. Run efficiently: native code on every high-rate path and no copies of large data.
   ([§7](#7-efficiency))
9. Bound the age of every action and verify the bound before motion.
   ([§4.11](#411-checking-a-deployment))
10. Record a labeled episode for every run. ([§8](#8-safety-and-recording))
11. Keep Python, YAML, and the CLI equivalent. ([§4.10](#410-yaml-and-the-cli))
12. Integrate with ROS 2 without depending on it. ([§9](#9-integrations))
13. Accept components written in other languages, such as C++.
    ([§9](#9-integrations))
14. Let people instruct the robot by speech, chat, or the CLI. ([§4.6](#46-speech))
15. Migrate existing code without an abrupt break. ([§10](#10-migration))

**Out of scope:** hypervisors, Python control loops above System 1 rates, agent
frameworks, general-purpose middleware, and training.

---

## 3. Overview

- **`ControlLoop`** runs one tier at a fixed rate. It is the class that ships today as
  `RobotRuntime`, renamed.
- **`NativeLoop`** runs System 0 in C++. Python only configures it.
- **`Skills`** turns goals from any client (an agent, a script, a person) into actions
  on leased robot parts.
- **`RobotRuntime`** runs everything in one partition: a process, VM, or machine.
- **Links are explicit.** Within a partition, a component receives the object it uses.
  Across partitions, it receives a client that names its peer. All partitions of a
  robot share a namespace for discovery, health, and emergency stop.
- **Time is checked.** Every value carries its capture time. The runtime derives a
  timing graph from the objects it is given and refuses to start if an action would be
  too old.

```text
 Agent, script, or person
          │ goals
          ▼
       Skills ──actions──► ControlLoop s1 ──setpoints──► NativeLoop s0 ──► hardware
       Python               Python, ≤ 100 Hz             C++, 200 Hz – 1 kHz, safety gate
```

| Term                | Meaning                                                                                     |
| ------------------- | ------------------------------------------------------------------------------------------- |
| **Tier**            | System 0, 1, or 2: a responsibility and rate band, not a process                            |
| **Partition**       | A unit of isolation: a process, container, VM, or machine                                   |
| **`ControlLoop`**   | One tier at a fixed rate: read the robot, ask the action source, apply callbacks, send      |
| **`NativeLoop`**    | System 0 in C++: safety gate, interpolation, hold, deadlines, and arbitration between parts |
| **`RobotRuntime`**  | Everything in one partition: loops, nodes, and servers, with lifecycle and health           |
| **Node**            | A component that is not a loop, such as an agent or a detector; own thread and rate         |
| **`ActionSource`**  | **Existing.** Produces an action on each tick: `PolicySource`, `TeleopSource`, `Skills`     |
| **Goal**            | One request: a skill name, its parameters, and an optional success check                    |
| **Skill**           | A capability such as `pick`; turns a goal into an action source for its parts               |
| **`Skills`**        | A robot's set of skills; accepts goals, grants leases, and runs them on a loop              |
| **Client**          | Anything that submits goals: `Agent`, an MCP host, a script, Studio, or a person            |
| **Part**            | A named, commandable part of the robot, such as `arm`, `base`, or `whole_body`; parts nest  |
| **Lease**           | Exclusive, time-limited write access to a set of parts                                      |
| **Link**            | A connection between components: an object, or a client and a server by name                |
| **Robot namespace** | The key space shared by one robot's partitions                                              |
| **Capture time**    | When the measurement behind a value was taken                                               |
| **`Conversation`**  | The ordered turns between people and an agent                                               |

"Skill" means robot skill. Coding-assistant `SKILL.md` files are unrelated.

---

## 4. Walkthrough

Each example adds one capability to the previous ones.

### 4.1 One policy

A single tier needs only a `ControlLoop`. This is the existing behavior under the new
name:

```python
from physicalai.capture import UVCCamera
from physicalai.inference import InferenceModel
from physicalai.robot import SO101
from physicalai.runtime import AsyncExecution, ControlLoop, PolicySource

ControlLoop(
    robot=SO101(port="/dev/ttyACM0"),
    action_source=PolicySource(
        InferenceModel("./exports/act", device="GPU"),
        execution=AsyncExecution(),
        task="pick up the red cube",
    ),
    cameras={"top": UVCCamera(device="/dev/video0", fps=30)},
    fps=30,
).run(duration_s=60)
```

### 4.2 Teleoperation and recording

```python
ControlLoop(
    robot=SO101(port="/dev/ttyACM0"),
    action_source=TeleopSource(leader=SO101(port="/dev/ttyACM1", role="leader")),
    callbacks=[DatasetRecorder("./datasets/pick")],
    fps=30,
).run()
```

### 4.3 Skills and a script

`Skills` is an action source that runs goals. Each skill names the parts it claims and
the check that decides when it is done:

```python
skills = Skills([
    PolicySkill(name="pick", model=InferenceModel("./exports/pick"),
                claims={"arm", "gripper"}, task="pick up the {object}",
                success=GripperClosedOnObject()),
    PolicySkill(name="place", model=InferenceModel("./exports/place"),
                claims={"arm", "gripper"}, task="place it in the {location}",
                success=ObjectAt()),
])

with ControlLoop(robot=SO101(port="/dev/ttyACM0"), action_source=skills, fps=30) as loop:
    loop.start()                                        # runs in a background thread
    pick = skills.submit(Goal("pick", {"object": "red block"}))
    if pick.result().status is Status.SUCCEEDED:
        skills.submit(Goal("place", {"location": "left bin"})).result()
```

For each goal, `Skills` grants a lease on `arm` and `gripper`, builds a `PolicySource`
with the formatted task, runs it until the success check passes, and records one
episode.

### 4.4 Human takeover, HIL, and DAgger

Intervention is preemption. A teleoperation skill claims the same parts at a higher
priority and ends when its success check passes, here when the pedal is released:

```python
pedal = Pedal("/dev/input/event3")
skills = Skills([
    PolicySkill(name="pick", model=InferenceModel("./exports/pick"), claims={"arm", "gripper"}),
    TeleopSkill(name="takeover", leader=SO101(port="/dev/ttyACM1", role="leader"),
                claims={"arm", "gripper"}, priority=Priority.HUMAN,
                success=Released(pedal)),
])
pedal.on_press(lambda: skills.submit(Goal("takeover", {})))
```

The recorded episode marks the intervention segment, which provides the labels DAgger
requires.

### 4.5 An agent or an MCP host

An `Agent` offers each skill to a reasoning model as a tool and submits a goal for each
tool call. A `RobotRuntime` hosts the loop, the agent, and the servers that expose the
skills:

```python
RobotRuntime(
    robot_name="tabletop-1",
    loops={"s1": ControlLoop(robot=SO101(port="/dev/ttyACM0"), action_source=skills, fps=30)},
    nodes={"planner": Agent(OpenAICompatibleModel(base_url="http://localhost:30000/v1"), skills)},
    serve=[SkillsServer(skills, name="tabletop"), McpServer(skills)],
).run()
```

Studio, an MCP host, or a script on another machine reaches the same skills by name:

```python
skills = SkillsClient(name="tabletop", robot_name="tabletop-1")
skills.submit(Goal("pick", {"object": "mug"})).result()
```

A model can also drive the arm directly in small steps. Each goal moves the arm by a
bounded offset, and the arm holds when goals stop arriving:

```python
skills = Skills([ServoSkill(name="servo", claims={"arm"}, max_step_m=0.01)])
skills.submit(Goal("servo", {"dx": 0.01}))
```

### 4.6 Speech

A `Voice` node turns speech into conversation turns and speaks the agent's replies.
Studio chat and the CLI attach to the same conversation:

```python
conversation = Conversation()
voice = Voice(
    microphone=Microphone("default"),
    speaker=Speaker("default"),
    stt=WhisperModel("./exports/whisper-small", device="NPU"),
    tts=TextToSpeechModel("./exports/tts"),
    conversation=conversation,
    skills=skills,                          # for stop phrases
    stop_phrases=("stop", "halt"),
)

RobotRuntime(
    robot_name="tabletop-1",
    loops={"s1": ControlLoop(robot=SO101(port="/dev/ttyACM0"), action_source=skills, fps=30)},
    nodes={"voice": voice,
           "planner": Agent(planner_model, skills, conversation=conversation)},
).run()
```

On a stop phrase, `Voice` calls `skills.stop()` directly instead of waiting for the
reasoning model.

### 4.7 A humanoid on one host

System 0 is a `NativeLoop` in C++. System 1 is a Python loop whose robot is System 0.
Skills on different parts run concurrently, a detector runs at its own rate, and a
budget bounds how old an action may be when it reaches System 0:

```python
cameras = {
    "head": UVCCamera(device="/dev/video0", fps=30),
    "wrist": UVCCamera(device="/dev/video2", fps=30),
}

s0 = NativeLoop(robot=G1Driver(), limits=G1_LIMITS, fps=500)            # C++

skills = Skills([
    PolicySkill(name="manipulate", model=InferenceModel("./exports/bimanual", device="GPU"),
                claims={"left_arm", "right_arm"}),
    DelegatedSkill(name="walk_to", controller=Nav2(), claims={"base"}),
    SetpointSkill(name="look_at", claims={"head"}),
])

s1 = ControlLoop(robot=s0, action_source=skills, cameras=cameras, fps=50)

detector = Detector(camera=cameras["head"],
                    model=InferenceModel("./exports/detector", device="NPU"),
                    rate=5, world=skills.world)

RobotRuntime(
    robot_name="g1-lab3",
    loops={"s0": s0, "s1": s1},
    nodes={"detector": detector, "planner": Agent(planner_model, skills)},
    serve=[SkillsServer(skills, name="humanoid")],
    budgets=[Budget(source="head", to="s0", max_age=0.30)],
).run()
```

```python
skills.submit(Goal("look_at", {"target": "shelf"}))                  # head
skills.submit(Goal("walk_to", {"target": "shelf"})).result()         # base
skills.submit(Goal("manipulate", {"task": "take the box"})).result() # both arms
```

No graph is written. The runtime derives one from the constructor arguments:

```text
camera "head" ──┬──► s1 (50 Hz) ──robot──► s0 (500 Hz, C++) ──► G1
camera "wrist" ─┘       ▲
                        │ action_source
                     Skills ◄── goals ── Agent "planner"
                        │                    ▲ reads facts
                        └── WorldState ◄─────┤
camera "head" ──► detector (5 Hz) ── writes detections
```

### 4.8 The same humanoid across partitions

The robot is split across a real-time guest, the main robot computer, a GPU host, and
the cloud. Each partition has its own short description, and each link that crosses a
partition names its peer:

```python
# rt-guest: System 0, in C++
s0 = NativeLoop(robot=G1Driver(), limits=G1_LIMITS, fps=500)
RobotRuntime(robot_name="g1-lab3", loops={"s0": s0}, serve=[RobotServer(s0, name="g1")]).run()

# robot-main: System 1, skills, and perception
skills = Skills([
    PolicySkill(name="manipulate", model=RemoteInferenceModel(name="bimanual"),
                claims={"left_arm", "right_arm"}),
    DelegatedSkill(name="walk_to", controller=Nav2(), claims={"base"}),
    SetpointSkill(name="look_at", claims={"head"}),
])
RobotRuntime(
    robot_name="g1-lab3",
    loops={"s1": ControlLoop(robot=SharedRobot.attach("g1"), action_source=skills,
                             cameras=cameras, fps=50)},
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

Compared with §4.7, four arguments change:

| Argument              | One host              | Across partitions                       |
| --------------------- | --------------------- | --------------------------------------- |
| Robot of `s1`         | `s0`                  | `SharedRobot.attach("g1")`              |
| Model of `manipulate` | `InferenceModel(...)` | `RemoteInferenceModel(name="bimanual")` |
| Skills of the agent   | `skills`              | `SkillsClient(name="humanoid", ...)`    |
| End of the budget     | the loop `"s0"`       | the robot link `"g1"`                   |

```text
 rt-guest              robot-main                       gpu                cloud
┌────────────────┐    ┌─────────────────────────────┐  ┌───────────────┐  ┌──────────────────┐
│ NativeLoop s0  │◄───│ ControlLoop s1, 50 Hz       │─►│ Inference-    │  │ Agent            │
│ 500 Hz, C++    │    │  robot = SharedRobot "g1"   │  │ Server        │  │  SkillsClient    │
│ RobotServer    │    │  Skills, detector           │  │ "bimanual"    │  │  ("humanoid")    │
│ "g1"           │    │ SkillsServer "humanoid"     │◄─┼───────────────┼──┤                  │
└────────────────┘    └─────────────────────────────┘  └───────────────┘  └──────────────────┘
   robot link                 inference link                   skills link
                 all four partitions share the robot namespace "g1-lab3"
```

### 4.9 Models from any backend

Each skill owns its model. Moving a model out of the process changes one argument:

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

### 4.10 YAML and the CLI

Every constructor argument is also a YAML field, so §4.5 can be written as a file. An
object used by several components, such as `skills`, is written once with an anchor and
referenced by alias. `from_config` constructs it once and passes the same instance to
every reference.

```yaml
runtime:
  robot_name: tabletop-1
  loops:
    s1:
      class_path: physicalai.runtime.ControlLoop
      init_args:
        robot:
          class_path: physicalai.robot.SO101
          init_args: { port: /dev/ttyACM0 }
        action_source: &skills
          class_path: physicalai.skills.Skills
          init_args:
            skills:
              - class_path: physicalai.skills.PolicySkill
                init_args:
                  name: pick
                  model:
                    class_path: physicalai.inference.InferenceModel
                    init_args: { export_dir: ./exports/pick }
                  claims: [arm, gripper]
                  task: "pick up the {object}"
        fps: 30
  nodes:
    planner:
      class_path: physicalai.agent.Agent
      init_args:
        model:
          class_path: physicalai.reasoning.OpenAICompatibleModel
          init_args: { base_url: "http://localhost:30000/v1" }
        skills: *skills
  serve:
    - class_path: physicalai.skills.SkillsServer
      init_args: { skills: *skills, name: tabletop }
```

```bash
physicalai run --config tabletop.yaml       # run the partition
physicalai check --config tabletop.yaml     # run the startup checks without moving
```

### 4.11 Checking a deployment

Before the first tick, the runtime checks types, input ages, budgets, and rates. A
failed check prevents the start:

```text
$ physicalai check --config robot-main.yaml
g1-lab3 / robot-main
  head (30 Hz)  → s1 manipulate          worst  64 ms   max_age 150 ms   ok
  s1 (50 Hz)    → s0 via robot link "g1" worst  91 ms   max_age  50 ms   FAIL
  budget head   → s0                     worst 112 ms   budget  300 ms   ok
  manipulate: ModelSpec expects camera "wrist"; s1 provides "head"       FAIL
```

The checks catch problems that would otherwise appear only as poor motion. For example,
a policy that emits 16-step chunks at 50 Hz executes the last step of each chunk
320 ms after the frame was captured, which violates a 300 ms budget.

---

## 5. Components

### 5.1 ControlLoop

`ControlLoop` is the class that ships today as `RobotRuntime`, with the same constructor
and lifecycle. It runs on its own or inside a `RobotRuntime`.

```python
class ControlLoop:
    def __init__(self, robot, action_source=None, fps=30.0, cameras=None, callbacks=(),
                 interpolator=None): ...                         # existing; action_source becomes optional
    @classmethod
    def from_config(cls, config: str | Path) -> Self: ...        # existing
    def run(self, duration_s: float | None = None) -> None: ...  # existing; blocks
    def start(self) -> None: ...                                 # new; runs in a background thread
```

| Argument        | Accepts                                                                                           |
| --------------- | ------------------------------------------------------------------------------------------------- |
| `robot`         | Hardware, a `NativeLoop`, another `ControlLoop`, a `SharedRobot`, or a vendor or ROS 2 controller |
| `action_source` | `PolicySource`, `TeleopSource`, `Skills`, a ROS 2 topic source, or none                           |
| `callbacks`     | Recording, telemetry, and action filters                                                          |
| `interpolator`  | Splits each action into several robot commands (existing)                                         |

- A loop without an action source is a lower tier. It takes setpoints from the loop
  above, holds when they stop, and rejects setpoints past their deadline.
- A Python loop runs at System 1 and System 2 rates, up to about 100 Hz
  ([§7](#7-efficiency)). A slow robot, such as a 30 Hz arm, may use a Python loop as
  its lowest tier, behind the native `SafetyGate` ([§8](#8-safety-and-recording)).
- `run()` and `start()` apply the startup checks of
  [§5.8](#58-time-and-freshness) before the first tick.

### 5.2 NativeLoop

```python
class NativeLoop:                                   # C++, exposed to Python
    def __init__(self, robot: NativeRobot, limits: Limits, fps: float,
                 interpolator: Interpolator | None = None): ...
    # Robot protocol toward the loop above (existing protocol)
    def get_observation(self) -> RobotObservation: ...
    def send_action(self, action, *, goal_time: float = 0.1) -> None: ...
```

A `NativeLoop` contains the safety gate, interpolation, the stale-command hold,
deadline and lease checks, and arbitration between parts. Toward the loop above, it
implements the Python `Robot` protocol, so a Python loop uses it as it would use
hardware. `RobotServer(s0, name="g1")` serves it natively to other partitions. Its
driver implements the native robot interface:

```cpp
namespace physicalai {

class Robot {
 public:
  virtual ~Robot() = default;
  virtual Observation read() = 0;                       // stamped with capture time
  virtual void write(const Command& command) = 0;
  virtual CommandInterfaces interfaces() const = 0;    // parts and command modes
};

}  // namespace physicalai
```

### 5.3 RobotRuntime

```python
class RobotRuntime:
    def __init__(
        self,
        robot_name: str,                                     # the robot namespace to join
        loops: Mapping[str, ControlLoop | NativeLoop] | None = None,
        nodes: Mapping[str, Node] | None = None,             # agents, detectors, voice
        serve: Sequence[Server] = (),                        # RobotServer, SkillsServer, McpServer
        callbacks: Sequence[Callback] = (),                  # health and recording events
        budgets: Sequence[Budget] = (),                      # end-to-end latency budgets
    ): ...
    @classmethod
    def from_config(cls, config: str | Path) -> Self: ...
    def check(self) -> TimingReport: ...                     # startup checks, without starting
    def run(self) -> None: ...
    def stop(self, *, emergency: bool = False) -> None: ...
    health: Mapping[str, Health]
```

The dictionaries name components; they do not connect them. Connections are the
objects passed to each constructor.

| Responsibility | Behavior                                                                               |
| -------------- | -------------------------------------------------------------------------------------- |
| Lifecycle      | Starts loops from the bottom tier up, then nodes, then servers; stops in reverse order |
| Dependencies   | Waits, with a timeout, for each named peer before starting the components that need it |
| Checks         | Derives the timing graph and checks types, ages, budgets, and rates before starting    |
| Health         | Publishes liveness, measured ages, CPU time, and jitter in the robot namespace         |
| Emergency stop | Stops its loops on `stop(emergency=True)` or on an emergency stop from the namespace   |

Every partition of a robot runs its own `RobotRuntime`, except partitions that contain
a single component, such as an `InferenceServer` or an `Agent`.

### 5.4 Goals and skills

Skills live in `physicalai.skills`, which has no LLM dependency.

```python
@dataclass(frozen=True)
class Goal:
    skill: str                           # "pick"
    params: Mapping[str, Any]            # {"object": "mug"}
    success: SuccessCheck | None = None  # overrides the skill's default check
    timeout_s: float | None = None

class Skill(Protocol):
    name: str
    description: str
    params_schema: type[Params]          # generates the tool, form, and CLI schemas
    claims: frozenset[Part]
    success: SuccessCheck | None         # None: runs until cancelled or preempted
    def can_start(self, goal: Goal, world: WorldState) -> Precondition: ...
    def source(self, goal: Goal) -> ActionSource: ...

class Skills(ActionSource):
    def __init__(self, skills: Sequence[Skill], *, access: AccessPolicy | None = None): ...
    def submit(self, goal: Goal, *, priority: Priority = Priority.NORMAL) -> SkillHandle: ...
    def register(self, skill: Skill) -> None: ...
    def stop(self) -> None: ...          # cancels every running goal; all parts hold
    world: WorldState

class SkillHandle:
    status: Status                       # pending, running, succeeded, failed, cancelled
    feedback: Feedback
    def update(self, params: Mapping[str, Any]) -> None: ...
    def cancel(self) -> None: ...
    def result(self, timeout_s: float | None = None) -> Result: ...
```

A skill turns a goal into an `ActionSource`. `Skills` runs that source on the skill's
claimed parts until the goal ends:

| Stage                | Mechanism                                                                         |
| -------------------- | --------------------------------------------------------------------------------- |
| Start                | Grant the lease, call `skill.source(goal)`, then `connect()` on the source        |
| Each tick            | Call `update()` on the source and keep the action for the claimed parts           |
| Success              | The goal's success check, or the skill's default, passes                          |
| Failure              | `update()` raises, or the goal's timeout expires                                  |
| Cancel or preemption | Call `disconnect()` on the source; the parts hold                                 |
| Feedback             | Elapsed time and the confidence of the success check; other facts in `WorldState` |

`PolicySkill` is generic. Pick and place are configured instances, not subclasses:

```python
class PolicySkill:
    def source(self, goal):
        return PolicySource(self.model, task=self.task.format(**goal.params))
```

The model is loaded once, when the skill is constructed; each goal receives a fresh
source. A new skill implements the same protocol:

```python
class LookAtSkill:
    name = "look_at"
    description = "Turn the head toward a named target."
    params_schema = LookAtParams                     # target: str
    claims = frozenset({"head"})
    success = HeadPointsAt(tolerance_deg=3)

    def can_start(self, goal, world):
        known = goal.params["target"] in world.objects
        return Precondition.ok() if known else Precondition.fail("unknown target")

    def source(self, goal):
        return PointHeadSource(target=goal.params["target"])
```

| Skill            | Source built for each goal                        | Default success check          |
| ---------------- | ------------------------------------------------- | ------------------------------ |
| `PolicySkill`    | `PolicySource` over the skill's model             | Configured per skill           |
| `TeleopSkill`    | `TeleopSource` for the leader device              | None, or a configured check    |
| `SetpointSkill`  | Emits the target setpoint                         | Target reached                 |
| `ServoSkill`     | Steps toward the requested offset                 | Target reached                 |
| `DelegatedSkill` | Forwards an external controller's commands (Nav2) | The controller reports success |

Completion is decided by a success check, never by the source, because a policy cannot
tell when its task is done. Every skill's commands pass through the loop, including an
external controller's, so the safety gate and leases apply to all of them.

**Leases.** Each part has at most one writer. A lease on `whole_body` covers the arms
and the base. A higher-priority goal may preempt a lower-priority lease. A lease whose
client misses its heartbeat expires, the goal is cancelled, and the parts hold.

**`WorldState`.** The latest value of each fact, such as a detection or a pose, with
its capture time. Each key has one writer. Readers declare the oldest fact they accept,
so a success check never passes on a stale detection. Frames are stored by reference.

**Success checks** return a confidence rather than a boolean:

```python
GripperClosedOnObject()    # the gripper has closed on an object
ObjectAt(region="left bin") # a detector places the object in a region
VLMJudge(model)            # a reasoning model judges the outcome from images
```

### 5.5 Parts and actions

An action may map parts to commands. A flat `np.ndarray` remains valid for a source that
commands one part, so `PolicySource` and `TeleopSource` are unchanged:

```python
{
    "left_arm": Command(mode=EE_POSE, values=..., frame="base"),
    "base": Command(mode=TWIST, values=...),
}
```

A robot may declare its parts and command modes through `Robot.command_interfaces()`.
By default, a robot is one part that accepts joint positions. At startup, part-keyed
actions are laid out as fixed slices of one array, so a tick builds no dictionaries
([§7](#7-efficiency)).

### 5.6 Models

Models belong to the components that use them. A `PolicySkill` owns an action model, as
`PolicySource` does today; an agent or a `VLMJudge` owns a reasoning model.

```python
class ActionModel(Protocol):                       # existing as InferenceModel
    def predict_action_chunk(self, observation) -> np.ndarray: ...
    def reset(self) -> None: ...

class ReasoningModel(Protocol):
    def complete(self, messages, *, tools=(), response_format=None) -> Completion: ...

class SpeechToTextModel(Protocol):
    def transcribe(self, audio: Stamped[Audio]) -> Transcript: ...

class TextToSpeechModel(Protocol):
    def synthesize(self, text: str) -> Audio: ...
```

| Interface           | Backends                                                                                       |
| ------------------- | ---------------------------------------------------------------------------------------------- |
| `ActionModel`       | OpenVINO and ONNX; LeRobot and GR00T through plugins                                           |
| `ReasoningModel`    | OpenAI-compatible servers (SGLang, vLLM, llama.cpp, Ollama); Anthropic; Gemini; OpenVINO GenAI |
| `SpeechToTextModel` | OpenVINO GenAI Whisper; OpenAI-compatible transcription servers                                |
| `TextToSpeechModel` | OpenVINO; OpenAI-compatible speech servers                                                     |

| Location          | Mechanism                                                         | Use                                                             |
| ----------------- | ----------------------------------------------------------------- | --------------------------------------------------------------- |
| In-process        | `InferenceModel` (existing)                                       | OpenVINO or ONNX models                                         |
| Another partition | `RemoteInferenceModel(name=...)` and `InferenceServer` (existing) | Conflicting dependencies, crash isolation, a dedicated GPU host |

`RemoteInferenceModel` is a subclass of `InferenceModel`, so every source and execution
mode uses it unchanged. A served model may run in a separate Python environment
prepared by `physicalai.isolate`.

**`ModelSpec`** declares a model's inputs and outputs, the parts and command modes it
drives, the rate it was trained at, and the oldest observation it accepts. Plugins
convert LeRobot features and GR00T modality files into a `ModelSpec`. The startup checks
compare it with the loop's cameras and the robot's parts, and its maximum observation
age becomes the default `max_age` of the source that runs the model.

### 5.7 Agent and conversation

```python
class Agent:
    def __init__(self, model: ReasoningModel, skills: Skills | SkillsClient, *,
                 conversation: Conversation | None = None): ...
    def run(self, instruction: str | None = None) -> None: ...

class Conversation:
    def __init__(self, name: str | None = None, robot_name: str | None = None): ...
    def add(self, speaker: str, text: str) -> Turn: ...
    def turns(self, since: int = 0) -> Iterator[Turn]: ...     # blocks for new turns
```

An agent takes an instruction from `run()` or reads turns from a conversation while it
works, and adds its replies, questions, and progress reports. Turns are events: they are
delivered in order and never dropped, so they do not pass through `WorldState`. An
unnamed conversation is local; a named one is shared through the robot namespace, so a
cloud agent hears a microphone on the robot:

```python
Conversation(name="main", robot_name="g1-lab3")
```

A reasoning model that accepts audio can take spoken turns without a separate
speech-to-text model.

### 5.8 Time and freshness

Every value carries the capture time of the measurement behind it. Frames and robot
observations already do (**existing**: `Frame.timestamp`, `RobotObservation.timestamp`).
Computed values inherit the oldest capture time of their observation inputs; goals and
task strings do not count.

```python
@dataclass(frozen=True)
class Stamped(Generic[T]):
    value: T
    captured_at: float          # capture time of the observations behind the value
    source: str                 # the component that produced it
```

Component authors declare each component's timing. Users see only the constructor
arguments that override the defaults.

```python
@dataclass(frozen=True)
class Timing:
    rate: float | None          # Hz; None for event-driven components
    latency: float              # seconds per step; declared, or measured at warm-up
    inputs: Mapping[str, Input]

@dataclass(frozen=True)
class Input:
    source: object              # the object this component reads from
    max_age: float | None       # oldest acceptable value; None for no limit
    observation: bool = True    # whether outputs inherit this input's capture time

class Timed(Protocol):
    def timing(self) -> Timing: ...
```

```python
PolicySource(model, max_age=0.15)                       # one limit for every observation
PolicySource(model, max_age={"wrist": 0.1, "state": 0.02})
```

| Component                                             | Rate                      | Latency                 | Inputs                                               |
| ----------------------------------------------------- | ------------------------- | ----------------------- | ---------------------------------------------------- |
| Camera                                                | Frame rate                | Declared by the backend | None                                                 |
| `ControlLoop`                                         | `fps`                     | Measured tick time      | Robot state, cameras, action source                  |
| `NativeLoop`                                          | `fps`                     | Measured natively       | Setpoints from above, driver state                   |
| `PolicySource`                                        | Loop rate, or policy rate | Measured at warm-up     | Cameras and state, `max_age` from `ModelSpec`        |
| `Skills`                                              | Loop rate                 | Measured tick time      | Sources of running goals; goals are not observations |
| Node                                                  | Its own rate              | Measured                | The objects passed to its constructor                |
| `SharedRobot`, `RemoteInferenceModel`, `SkillsClient` | Advertised by the peer    | Peer's, plus round trip | Advertised by the peer                               |

At startup, the runtime follows each component's inputs and builds a timing graph. An
edge exists exactly where one object was passed to another. For each input, the
worst-case age is the producer's worst-case output age plus one producer period; a
component's output age adds its own latency; a link adds its transit time.

| Check     | Fails when                                                                                 |
| --------- | ------------------------------------------------------------------------------------------ |
| Types     | A `ModelSpec` names an input the loop does not provide, or drives a part the robot lacks   |
| Freshness | The worst-case age of an input exceeds its `max_age`                                       |
| Budgets   | The worst-case age along a path exceeds its budget                                         |
| Rates     | A component's latency exceeds its period, or a Python loop exceeds the Python rate ceiling |

```python
Budget(source="head", to="s0", max_age=0.30)   # `to` is a loop, or a robot link to another partition
```

Each partition checks the paths that start in it. Servers advertise the timing of their
side during the handshake, so no global graph is needed.

At run time:

- A component holds when an observation input is stale, which the existing
  `ActionSource` contract permits. Persistent staleness fails the running goal.
- The deadline of a setpoint is its capture time plus the consumer's `max_age`.
- Measured ages, CPU time, and jitter are published as health and recorded with each
  episode.

---

## 6. Communication Between Partitions

All partitions communicate over Zenoh through `physicalai.transport`, which shared
cameras, shared robots, and remote inference already use. Each partition joins the
robot namespace given by `robot_name`. Names passed to clients and servers resolve to
keys in that namespace.

| Pattern             | Used for                                                                                               |
| ------------------- | ------------------------------------------------------------------------------------------------------ |
| Publish / subscribe | Latest-value streams: robot state, setpoints, feedback, `WorldState` facts, heartbeats, emergency stop |
| Query / reply       | Requests: handshakes, metadata, goal submission, cancellation, leases, inference                       |
| Reliable stream     | Conversation turns: ordered, with sequence numbers; late subscribers query recent turns                |

Messages are MessagePack with arrays tagged by dtype and shape; camera frames are a
packed binary header followed by pixels (**existing**). Each link has a versioned,
language-neutral schema published with conformance tests.

### Links

| Link          | Client → server                            | Messages                                                                                       |
| ------------- | ------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| Robot command | `SharedRobot` → `RobotServer`              | Query: metadata, leases. Publish: part-keyed setpoints up, robot state down                    |
| Inference     | `RemoteInferenceModel` → `InferenceServer` | Query: handshake, then one request per action chunk (existing)                                 |
| Skills        | `SkillsClient` → `SkillsServer`            | Query: list, submit, update, cancel. Publish: feedback, events, heartbeats, `WorldState` facts |

- **Robot command.** Extends the existing `SharedRobot` owner protocol with part-keyed
  setpoints, each carrying a mode, values, a capture time, a deadline, and a lease.
  Setpoints are never retransmitted; the server rejects late setpoints and wrong leases
  and arbitrates between several upper loops by part.
- **Inference.** Exists today, with a handshake on protocol version, policy identity,
  and manifest hash. The design adds a `ModelSpec` check.
- **Skills.** Submission is a query, so the client knows whether a goal was accepted.
  Client-generated goal identifiers make retries idempotent. Missed heartbeats expire
  leases.

### Robot-wide messages

| Message        | Publisher                    | Subscribers                   | Purpose                                          |
| -------------- | ---------------------------- | ----------------------------- | ------------------------------------------------ |
| Presence       | Every runtime and server     | Studio, CLI, other partitions | Discover the partitions and endpoints            |
| Health         | Every runtime                | Studio, monitors              | Liveness, ages, CPU time, and jitter             |
| Emergency stop | Any authorized peer          | Every runtime                 | Immediate stop; each safety gate holds or brakes |
| Conversation   | `Voice`, Studio, CLI, agents | Agents, `Voice`, Studio       | Ordered turns between people and agents          |

If an emergency stop is lost, the stale-command hold still stops every lower tier once
the tier above falls silent.

### Startup, reconnection, and time

- A runtime starts a component only when its named peers answer the handshake: metadata
  and parts for a robot link, `ModelSpec` for inference, the expected skills for a
  skills link. Every handshake also carries the server's timing.
- After a link is lost and restored, the upper tier must request a new lease. Motion
  never resumes automatically.
- Capture times are monotonic nanoseconds of the host that measured them. Each link
  measures clock offset and round-trip time in the handshake, refreshes them with
  heartbeats, and converts capture times to the receiver's clock. A link refuses to
  start, or reports degraded health, when the offset is too uncertain.

### Security and large data

- The existing transports rely on loopback binding, SSH tunnels, or network isolation.
  Across hosts, the robot command and skills links must authenticate peers, and
  key-level access control limits each partition to its keys. This enforces the
  partition boundary: System 2 may use the skills keys but not the robot command keys.
- Frames stay in the partition that consumes them where possible. Otherwise they use the
  shared-camera transport, with Zenoh shared memory between processes on one host.
  Shared memory between KVM guests is deferred.

---

## 7. Efficiency

Python composes the system and runs the slow tiers. A component is native when it runs
above about 100 Hz, is heavy per call, is safety-critical, or moves large data. Any
component may be native, through bindings or as a partition ([§9](#9-integrations)).

| Component                                                    | Runs in                                            |
| ------------------------------------------------------------ | -------------------------------------------------- |
| System 0 (`NativeLoop`)                                      | Native (required)                                  |
| Safety gate, stale-command hold, deadlines, watchdog, e-stop | Native (required), bound to Python loops           |
| Lease enforcement in `RobotServer`                           | Native                                             |
| Robot drivers, cameras, audio capture                        | Native, usually from vendor SDKs                   |
| Model inference and preprocessing                            | OpenVINO, ONNX Runtime, or plugin backends         |
| Transport and codecs                                         | Zenoh and native codecs                            |
| Perception (detectors, SLAM, depth)                          | Native when heavy: bindings or a partition         |
| Recording writer (video encoding, files)                     | Native, fed from a queue                           |
| Action queue, chunk smoothing, `InferenceServer`             | Python or native                                   |
| System 1 loops, `Skills`, success checks                     | Python, up to ~100 Hz; per-tick work is array work |
| System 2: agents, conversation, configuration                | Python                                             |

- **Rate ceiling.** The startup check rejects a Python loop above the ceiling, which is
  set per reference platform by benchmark.
- **Interpreter lock.** Components that share a process with a Python loop release the
  lock while they compute: native models, bindings, and I/O. A component that holds it,
  such as a pure-Python planner, runs in its own process behind a server.
- **Planned ticks.** Parts, inputs, and rates are known before the first tick, so the
  runtime preallocates buffers, lays out actions as array slices, and fixes the merge
  order. A tick performs array operations without allocating.
- **No copies.** Frames are passed as views (the shared-camera transport already decodes
  in place), `WorldState` stores references, preprocessing runs inside the model
  runtime, and same-host links use shared memory.
- **Recording** enqueues data on the tick and writes from another thread or process.
- **Benchmarks** of tick overhead, jitter, action age, and copies per frame run in
  continuous integration against targets, so a regression fails the build.

---

## 8. Safety and Recording

Each safety layer is enforced independently of the layers above it.

| Layer               | Component                        | Enforces                                                          |
| ------------------- | -------------------------------- | ----------------------------------------------------------------- |
| Hardware / firmware | Robot                            | Power, torque, and temperature limits; stop when the process dies |
| Safety gate         | Native `SafetyGate`              | Joint, velocity, workspace, and force limits on every command     |
| Watchdog            | Native thread of the safety gate | Hold when setpoints stop, even if the Python process stalls       |
| Stale-command hold  | Lower loop                       | Hold or stop when the tier above goes silent                      |
| Freshness           | Every consumer                   | Hold on stale inputs; reject setpoints past their deadline        |
| Startup checks      | `ControlLoop`, `RobotRuntime`    | No start when ages, budgets, or rates cannot be met               |
| Leases              | `Skills`, `RobotServer`          | One writer per part                                               |
| Access policy       | `Skills`                         | Which clients may run which skills                                |
| Link authentication | Robot command and skills links   | Only authenticated peers send setpoints or submit goals           |
| Partition boundary  | Key-level access control         | System 2 cannot reach the robot command keys                      |
| Emergency stop      | Native listener in every runtime | Reaches every runtime of the robot                                |

Every safety mechanism is native, whatever the loop rate. A Python loop driving a 30 Hz
arm uses the same native `SafetyGate` through bindings, so one implementation is
verified everywhere. The gate's native thread needs no interpreter lock, so it holds
the robot when a Python loop stalls on garbage collection, the lock, or a deadlock. If
the whole process dies, the robot's firmware or hardware stop must act.

```python
ControlLoop(robot=SafetyGate(SO101(port="/dev/ttyACM0"), limits=SO101_LIMITS),
            action_source=skills, fps=30)           # native gate and watchdog
```

The `unrestricted` access mode removes permission checks only; gates, leases, and
deadlines remain. `Voice` is a client with its own identity, because anyone nearby can
speak to the robot. Its stop phrases are a convenience, not a safety layer, since speech
recognition can miss or invent words.

| Failure              | Response                                                       |
| -------------------- | -------------------------------------------------------------- |
| Client disconnects   | Its lease expires, the goal is cancelled, and the parts hold   |
| System 2 crashes     | As above; System 1 and System 0 continue                       |
| Remote model is late | The source holds and reports starvation; the goal may fail     |
| System 1 goes silent | System 0 applies its stale-command hold                        |
| System 0 faults      | System 1 stops; running goals fail                             |
| Python loop stalls   | The native watchdog holds the robot                            |
| Process crashes      | Firmware or hardware stop                                      |
| Link lost            | Treated as silence; a new lease is required after reconnection |
| A check fails        | The runtime does not start and reports the input or path       |

**Recording.** `Skills` emits an event when a goal starts, is preempted, and ends. A
recording callback turns these events into one episode per goal, containing the loop
data in LeRobot format, the goal and its client, the outcome, model provenance,
intervention segments, the age of every action, and the agent transcript and
conversation. Failed and cancelled goals are recorded like successful ones.

---

## 9. Integrations

Integrations implement the design's protocols at its edges. The core depends on none of
them.

### ROS 2

```python
skills = Skills([DelegatedSkill(name="walk_to", controller=Nav2(action="/navigate_to_pose"),
                                claims={"base"})])
s1 = ControlLoop(robot=Ros2Robot(controller="/base_controller"), action_source=skills, fps=50)
RobotRuntime(robot_name="amr-2", loops={"s1": s1}, serve=[Ros2ActionServer(skills)]).run()
```

| ROS 2 concept                     | Integration                                              | Protocol       |
| --------------------------------- | -------------------------------------------------------- | -------------- |
| `ros2_control` controller, driver | `Ros2Robot`: a ROS 2 controller acts as System 0         | `Robot`        |
| Command topic                     | `Ros2Source`: a topic drives a loop                      | `ActionSource` |
| Action server                     | `Ros2ActionServer`: each skill appears as a ROS 2 action | Skills link    |
| Action client (Nav2, MoveIt)      | `DelegatedSkill`: its commands pass through the loop     | `Skill`        |
| TF and detection topics           | A bridge to `WorldState` facts                           | `WorldState`   |

ROS 2 actions have the same structure as goals and handles (goal, feedback, result,
cancel), so ROS 2 applications call PhysicalAI skills natively.

### Other languages

A native component joins in one of two ways:

- **In the same process**, through Python bindings (nanobind or pybind11) that implement
  a Python protocol: `Robot`, `ActionSource`, `ActionModel`, a camera, or a node. It is
  constructed and configured like any other object and reports its timing.
  `NativeLoop` is the main instance of this pattern:

  ```python
  s0 = NativeLoop(robot=NativeWholeBodyController(model="g1.urdf"), limits=G1_LIMITS, fps=1000)
  s1 = ControlLoop(robot=s0, action_source=skills, fps=50)
  ```

- **In another partition**, by implementing one side of a link through the Zenoh binding
  for its language. The Python side is unchanged:

  | Native partition implements                            | Python side uses                        |
  | ------------------------------------------------------ | --------------------------------------- |
  | Robot command server: System 0, WBC, vendor controller | `SharedRobot.attach("g1")`              |
  | Inference server                                       | `RemoteInferenceModel(name="bimanual")` |
  | `WorldState` facts: perception, SLAM                   | Success checks and agents               |
  | Skills client                                          | `SkillsServer(skills, name="humanoid")` |

A native partition behaves as a partition without running `RobotRuntime`. It completes
the handshakes, stamps capture times, publishes presence and health, obeys emergency
stop, and authenticates. The native library provides these, so vendors do not
reimplement them. Native loops do not run Python callbacks; recording subscribes to the
state they publish.

---

## 10. Migration

The class that ships today as `RobotRuntime` is renamed `ControlLoop`, and the name
`RobotRuntime` is reused for the partition runtime:

| Stage | Change                                                                                                                                                                            | Effect on existing code        |
| ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| 1     | Add `ControlLoop`; `RobotRuntime` becomes an alias for it; startup checks report only                                                                                             | None                           |
| 2     | Introduce the partition `RobotRuntime`; for one minor release it still accepts `robot`, `action_source`, and `fps` and builds a one-loop runtime; failed checks prevent the start | Deprecation warning            |
| 3     | Remove the compatibility path                                                                                                                                                     | Single loops use `ControlLoop` |

```python
# stage 1 and 2: unchanged code keeps working, with a warning in stage 2
RobotRuntime(robot=so101, action_source=PolicySource(model), fps=30).run()

# from stage 1: the new name
ControlLoop(robot=so101, action_source=PolicySource(model), fps=30).run()
```

Studio and the documentation move to `ControlLoop` in stage 1.

---

## 11. Open Questions and Deferred Decisions

**Open questions**

1. **Rates within one tier.** All skills in one `Skills` object run at the host loop's
   rate. Workloads that need separate rates can run as two System 1 loops sharing one
   System 0 through `RobotServer`, which arbitrates by part.
2. **Existing key layouts.** Whether the existing inference and shared-robot keys move
   under the robot namespace.
3. **Declared and measured latency.** Whether checks use declared worst-case latencies,
   latencies measured at warm-up, or both, and with what margin.
4. **Scope of the native kernel.** Which System 0 functions beyond gating,
   interpolation, and arbitration, such as IK or whole-body control, `NativeLoop`
   provides itself.

**Deferred decisions**

| Topic                                                            | Needed by                        |
| ---------------------------------------------------------------- | -------------------------------- |
| Command modes and payloads                                       | Part-keyed actions               |
| Link schemas and conformance tests                               | Multi-partition and native peers |
| Language (C++ or Rust), packaging, and ABI of the native library | `NativeLoop`, vendor partitions  |
| Benchmark targets per reference platform                         | Performance gates                |
| Key layout of the robot namespace                                | Multi-partition deployment       |
| Authentication and access-control mechanism                      | Cross-host deployment            |
| Lease durations, priorities, access policies                     | `Skills` with several clients    |
| Clock-offset bounds and default `max_age`                        | Startup checks                   |
| Timing of vendor and ROS 2 controllers                           | Integrations                     |
| Shared memory between KVM guests                                 | Guest-to-guest bandwidth         |
| Deployment file for `physicalai up`                              | Launching several partitions     |
| Recording format extensions                                      | Data collection at scale         |
| Audio backends, wake words, speaker identity                     | `Voice`                          |
| World-model interface                                            | Imagine-and-rank planning        |

---

## 12. Implementation Phases

| Phase | Scope                                                                                                                    | Exit criterion                                                       |
| ----- | ------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------- |
| 1     | `ControlLoop` alias; part-keyed actions; `start()`; capture times; benchmarks in CI                                      | Existing tests, configurations, and Studio pass; baselines recorded  |
| 2     | `NativeLoop`; native `SafetyGate` and watchdog; a loop as another loop's robot; partition `RobotRuntime`; startup checks | Native 500 Hz System 0 under a Python System 1; predicted ages hold  |
| 3     | `Skills`, `Goal`, `PolicySkill`, `TeleopSkill`, leases, skill events                                                     | Tabletop task from Python and YAML; HIL takeover recorded            |
| 4     | `ModelSpec`; remote and isolated-environment models in skills                                                            | Identical actions in-process and served                              |
| 5     | `SkillsServer`, `SkillsClient`, `McpServer`, `Agent`                                                                     | Cloud agent and MCP host drive the same robot                        |
| 6     | `RobotServer`; robot namespace; health; emergency stop; authentication; link schemas                                     | System 0 and System 1 on separate hosts; budgets checked across them |
| 7     | KVM partitions; key-level access control; ROS 2; a native System 0 partition                                             | Humanoid across guests with a C++ System 0; ROS 2 calls skills       |
| 8     | Delegated navigation; perceivers; world model                                                                            | Navigate, pick, and place in one task                                |
| 9     | `Conversation`, `Voice`, speech models                                                                                   | A spoken instruction drives a task; a spoken stop cancels it         |

Phases 1 to 3 deliver value without the later phases.

---

## 13. Decisions

```text
tiers           System 0 control, System 1 policy, System 2 deliberation
loop            ControlLoop runs one tier at one rate (the class shipped today as RobotRuntime)
system 0        NativeLoop in C++, or a vendor controller; Python only configures it
runtime         RobotRuntime runs one partition: loops, nodes, servers, lifecycle, health
composition     the tier below a loop is its robot: hardware, a loop, or a SharedRobot
links           an object in the same partition; a client by name across partitions
robot           a namespace shared by its partitions: discovery, health, emergency stop
skills          a skill turns a goal into an ActionSource; Skills runs it on leased parts
completion      decided by a success check, never by the source
clients         anything that submits goals; Agent is the built-in reasoning-model client
conversation    one ordered stream of turns; voice, chat, and CLI are adapters
models          owned by the components that use them; in-process or served by name
time            every value carries capture time; the timing graph is derived and checked
efficiency      Python composes, native code computes; planned ticks; no copies
safety          native gate, hold, watchdog, and e-stop; leases; access policy; authenticated links
native          any component; required for System 0 and safety, preferred for heavy or data-bound work
integrations    at the edges; ROS 2 and other languages implement the same protocols
migration       staged rename with one compatibility release
```
