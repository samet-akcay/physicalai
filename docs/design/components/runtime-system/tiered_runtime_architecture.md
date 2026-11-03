# Tiered Robot Runtime: System 0 / 1 / 2

**Status:** Proposal for design review.

This document describes how PhysicalAI runs three tiers of robot decision-making: System 0
(control), System 1 (reactive policy), and System 2 (deliberation). Models may come from
any backend, and the tiers may share one process or be spread across accelerators, KVM
guests, or separate machines.

The design adds no arguments to `RobotRuntime`. Every new capability is a component that
plugs into one of its existing slots: `robot`, `action_source`, or `callbacks`.

The document specifies architecture only. Wire formats, transports, and numeric limits
are deferred ([§14](#14-open-questions-and-deferred-decisions)). Code is pseudo-code
unless marked **existing**.

---

## Contents

1. [Summary](#1-summary)
2. [Motivation](#2-motivation)
3. [Terminology](#3-terminology)
4. [Architecture](#4-architecture)
5. [Examples](#5-examples)
6. [RobotRuntime](#6-robotruntime)
7. [Actions and Parts](#7-actions-and-parts)
8. [Goals and Skills](#8-goals-and-skills)
9. [Models and Backends](#9-models-and-backends)
10. [Links Across Partitions](#10-links-across-partitions)
11. [Deployment](#11-deployment)
12. [Safety, Time, and Recording](#12-safety-time-and-recording)
13. [Requirements Coverage](#13-requirements-coverage)
14. [Open Questions and Deferred Decisions](#14-open-questions-and-deferred-decisions)
15. [Implementation Phases](#15-implementation-phases)
16. [Decisions](#16-decisions)

---

## 1. Summary

`RobotRuntime` runs one fixed-rate loop. On each tick it reads the robot, asks its action
source for an action, passes the action through its callbacks, and sends it to the robot:

```python
# existing
RobotRuntime(robot=so101, action_source=PolicySource(model), fps=30, callbacks=[recorder]).run()
```

This proposal keeps that loop unchanged and builds the three tiers by composition:

```text
Agent ──goals──► Skills ──action_source──► RobotRuntime (System 1) ──robot──► RobotRuntime (System 0) ──robot──► SafetyGate ──► hardware
```

- An **agent** uses a reasoning model to decide what the robot should do next and
  submits **goals**. Agents are one kind of **client**; scripts, Studio, and humans
  submit goals directly.
- **`Skills`** runs each goal as a **skill** on leased parts of the robot. It is an action
  source, so it plugs into `RobotRuntime` like `PolicySource` does.
- The tier below a runtime is its **robot**. That robot can be hardware, another
  `RobotRuntime` in the same process, or a runtime in another partition.

| Tier         | Role                               | Rate                   | Examples                               |
| ------------ | ---------------------------------- | ---------------------- | -------------------------------------- |
| **System 2** | Plan, sequence, and verify         | Event-driven, 0.1–5 Hz | Cloud or local LLMs and VLMs           |
| **System 1** | Map observations to motion         | 10–50 Hz               | VLA, ACT, diffusion, navigation        |
| **System 0** | Track setpoints and enforce limits | 200 Hz – 1 kHz         | WBC, IK, impedance, vendor controllers |

Every link between components is an existing protocol (`Robot`, `ActionModel`, or the
skills API), and each protocol has an in-memory and a remote implementation. Moving a
component to another process, VM, or machine replaces one object with its remote
counterpart; nothing else changes.

---

## 2. Motivation

Recent robot systems separate slow reasoning from fast control. A language or
vision-language model plans and verifies, a learned policy produces motion, and a
high-rate controller tracks it. The System 1 and System 2 labels follow the dual-process
terminology common in this literature; System 0 denotes the control layer beneath them.

These tiers place different demands on the platform. Their models often do not fit in
the same memory. They need different operating-system and real-time guarantees. Some
robot vendors run them in separate KVM guests on a single SoC.

**Current state.** PhysicalAI provides `RobotRuntime`, a single fixed-rate loop with
pluggable action sources (`PolicySource`, `TeleopSource`) and callbacks; synchronous,
asynchronous, and real-time-chunking policy execution; a configuration system in which
YAML and the CLI mirror constructor signatures; and framework plugins with isolated
environments (in progress).

**Requirements.** The runtime must:

1. Run System 0, 1, and 2 on one robot.
2. Let a frontier model drive System 1 or System 0.
3. Run the tiers in one process, across accelerators on one host, in separate KVM guests,
   or on separate machines.
4. Accept models from any backend.
5. Let independent workloads use disjoint parts of the robot concurrently.
6. Support teleoperation, human-in-the-loop intervention, and DAgger.
7. Enforce safety that no action source, callback, or client can bypass.
8. Record a labeled episode for every run.
9. Keep existing scripts and configurations working unchanged.
10. Keep Python construction, YAML, and the CLI equivalent.
11. Keep framework dependencies inside plugins.

**Out of scope.** Hypervisors, hard real-time control in Python (loops at 200 Hz and
above remain native or vendor-provided), agent frameworks, general-purpose middleware,
and training.

---

## 3. Terminology

Prose uses lowercase for concepts (client, goal, skill). Code font denotes classes.
Tiers are written System 0, 1, and 2 in prose and `s0`, `s1`, and `s2` in code and
configuration. "Skill" means robot skill unless qualified otherwise.

**Core concepts**

| Term              | Definition                                                                                                                |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------- |
| **Client**        | Anything that submits goals to `Skills`: an agent, a script, Studio, or a human.                                          |
| **Agent**         | A client that uses a reasoning model to choose goals. The built-in `Agent` class is one; an external MCP host is another. |
| **Goal**          | One request: a skill name, its parameters, and an optional success check.                                                 |
| **Skill**         | A robot capability such as `pick`, `servo`, or `navigate_to`. Implements the `Skill` protocol.                            |
| **`Skills`**      | The action source that runs skills: it accepts goals, grants leases, and merges the actions of running skills.            |
| **`SkillHandle`** | The client's view of one running skill: status, feedback, update, cancel, and result.                                     |
| **Part**          | A named, commandable part of the robot, such as `arm`, `gripper`, `base`, or `whole_body`. Parts nest.                    |
| **Lease**         | Exclusive, time-limited write access to a set of parts, renewed by heartbeat.                                             |

**Existing components**

| Term               | Definition                                                                 |
| ------------------ | -------------------------------------------------------------------------- |
| **`RobotRuntime`** | Runs one fixed-rate loop: read the robot, query the action source, send.   |
| **`Robot`**        | The protocol for anything that accepts commands and reports state.         |
| **`ActionSource`** | The protocol for anything that produces an action on each tick.            |
| **Callback**       | A hook that observes, and may transform, each action inside the loop.      |
| **`ActionModel`**  | Maps observations to action chunks. Implemented today by `InferenceModel`. |

**New components**

| Term                                  | Definition                                                                                                         |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| **`SafetyGate`**                      | A `Robot` wrapper that enforces hard limits on every command before it reaches the robot.                          |
| **`LoopRobot` / `RemoteRobot`**       | Present a lower-tier runtime, in memory or in another partition, as a `Robot`.                                     |
| **`SetpointInbox` / `CommandServer`** | The action source of a lower-tier runtime: the latest setpoint from the tier above, in memory or over the network. |
| **`SkillsClient` / `SkillsServer`**   | Remote access to a `Skills` object. `McpServer` exposes the same skills as MCP tools.                              |
| **`ReasoningModel`**                  | Maps messages, images, and tool definitions to text or tool calls.                                                 |
| **`ModelSpec`**                       | Declares the inputs a model expects and the outputs it produces.                                                   |
| **`WorldState`**                      | The latest timestamped facts: poses, detections, and captions.                                                     |

**Deployment**

| Term            | Definition                                                     |
| --------------- | -------------------------------------------------------------- |
| **Partition**   | A unit of deployment: a process, container, VM, or machine.    |
| **Placement**   | Where a model runs: in-process, in a local worker, or remote.  |
| **Agent skill** | A coding-assistant `SKILL.md` file. Unrelated to robot skills. |

---

## 4. Architecture

```text
Client (Agent │ MCP host │ script │ Studio │ human)
   │  goals, through Skills or SkillsClient
   ▼
Skills                         leases, WorldState, access policy, skill events
   │  is the action_source of
   ▼
RobotRuntime (System 1)        10–50 Hz; models through the ActionModel protocol
   │  robot = LoopRobot or RemoteRobot
   ▼
RobotRuntime (System 0)        200 Hz+; action_source = SetpointInbox or CommandServer
   │  robot = SafetyGate(hardware)
   ▼
Hardware
```

### Composition through three protocols

Each link in the stack is an existing protocol, and each protocol has an in-memory and a
remote implementation:

| Protocol      | In memory                               | Across partitions                     | Carries                        |
| ------------- | --------------------------------------- | ------------------------------------- | ------------------------------ |
| `Robot`       | `LoopRobot` and `SetpointInbox`         | `RemoteRobot` and `CommandServer`     | Setpoints and robot state      |
| `ActionModel` | `InferenceModel`; isolated local worker | `RemoteActionModel` and policy server | Observations and action chunks |
| Skills API    | `Skills`                                | `SkillsClient` and `SkillsServer`     | Goals, status, and world state |

A component never knows whether its neighbor is in the same process or on another
machine.

### Rules

1. `RobotRuntime` runs one loop and gains no new constructor arguments.
2. The tier below a runtime is its `robot`. A vendor controller is also a `Robot`.
3. Whatever drives a runtime is its `action_source`: a policy, teleoperation, `Skills`,
   or setpoints from the tier above.
4. Callbacks observe and transform actions. They do not decide which action to send.
5. `SafetyGate` wraps the hardware, so every command passes through it regardless of
   its origin.
6. Every write is bounded by a lease and a deadline. A missed heartbeat or deadline
   causes a hold or a stop.
7. Every client goes through `Skills`. There is no privileged path.
8. Loops are placed by rate and models by memory. Moving a component across a partition
   boundary replaces it with its remote counterpart.

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

### 5.2 Teleoperation (existing)

```python
RobotRuntime(
    robot=SO101(port="/dev/ttyACM0"),
    action_source=TeleopSource(leader=SO101(port="/dev/ttyACM1", role="leader")),
    fps=30,
    callbacks=[recorder],
).run()
```

Single-purpose sessions such as a policy rollout or teleoperated data collection need
neither skills nor agents.

### 5.3 Scripted client

```python
skills = Skills([
    PolicySkill(name="pick", model=InferenceModel("./exports/pick"), claims={"arm", "gripper"},
                task="pick up the {object}", success=GripperClosedOnObject()),
    PolicySkill(name="place", model=InferenceModel("./exports/place"), claims={"arm", "gripper"},
                task="place it in the {location}", success=ObjectAt()),
])

with RobotRuntime(robot=SO101(port="/dev/ttyACM0"), action_source=skills, fps=30) as rt:
    rt.start()  # runs the loop in the background
    for obj, loc in [("red block", "left bin"), ("blue block", "right bin")]:
        if skills.submit(Goal("pick", {"object": obj})).result().status is Status.SUCCEEDED:
            skills.submit(Goal("place", {"location": loc})).result()
```

For each goal, `Skills` checks the access policy, grants a lease on the claimed parts,
attaches a `PolicySource`, and evaluates the success check. It then detaches the source
and emits the events that delimit the recorded episode.

The same runtime as YAML. `action_source` is an ordinary `class_path`:

```yaml
runtime:
  robot:
    class_path: physicalai.robot.so101.SO101
    init_args:
      port: /dev/ttyACM0
  fps: 30
  action_source:
    class_path: physicalai.skills.Skills
    init_args:
      skills:
        - class_path: physicalai.skills.PolicySkill
          init_args:
            name: pick
            model:
              class_path: physicalai.inference.InferenceModel
              init_args:
                export_dir: ./exports/pick
            claims: [arm, gripper]
            task: pick up the {object}
        # place is analogous
```

### 5.4 Agent or MCP host

The built-in `Agent` sends the instruction to a reasoning model with each skill offered
as a tool. It submits a goal for each tool call and returns the result to the model,
until the model reports that the task is done:

```python
Agent(OpenAICompatibleModel(base_url="http://localhost:30000/v1"), skills).run(
    "Put red blocks in the left bin and blue blocks in the right bin."
)
```

To let an external MCP host or a remote agent drive the same skills, `Skills` exposes
them through servers:

```python
skills = Skills([...], serve=[SkillsServer(port=7001), McpServer()])
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

Each call writes a setpoint with a deadline. `SafetyGate` clamps it, and the runtime holds
the arm if calls stop arriving. Without a System 1 policy, `Skills` can be the action
source of the System 0 runtime directly.

### 5.7 Mixed backends

Each skill owns its model, and the model's class determines where it runs:

```python
Skills([
    PolicySkill(name="pick", model=RemoteActionModel(endpoint="gpu-box"), ...),         # GR00T on a GPU server
    PolicySkill(name="place", model=WorkerActionModel("./exports/place", framework="lerobot"), ...),
    PolicySkill(name="inspect", model=InferenceModel("./exports/inspect", device="NPU"), ...),
])
Agent(OpenAICompatibleModel(base_url="http://localhost:30000/v1"), skills)               # SGLang, vLLM, llama.cpp
```

Framework code stays in plugins. Moving a model from a local worker to a remote server
changes one constructor call.

### 5.8 System 0 and System 1 on one host

A second `RobotRuntime` runs System 0. `LoopRobot` presents it to System 1 as a robot:

```python
s0 = RobotRuntime(robot=SafetyGate(G1(), limits), action_source=SetpointInbox(), fps=500)
s1 = RobotRuntime(robot=LoopRobot(s0), action_source=skills, fps=50, cameras=cameras)
s1.run()
```

`s1` owns `s0`. Connecting `s1` starts the System 0 loop, and disconnecting it stops
that loop. In YAML, the System 0 runtime is nested under `robot`:

```yaml
runtime:
  fps: 50
  robot:
    class_path: physicalai.runtime.LoopRobot
    init_args:
      runtime:
        robot:
          class_path: physicalai.runtime.SafetyGate
          init_args:
            robot: { class_path: physicalai.robot.unitree.G1 }
            limits: ./limits/g1.yaml
        action_source: { class_path: physicalai.runtime.SetpointInbox }
        fps: 500
  action_source:
    class_path: physicalai.skills.Skills
    init_args:
      skills: [] # as in 5.3
```

### 5.9 Humanoid across partitions

Each partition runs its own component. The in-memory links of 5.8 become remote links:

```python
# s0 partition (real-time guest)
RobotRuntime(robot=SafetyGate(G1(), limits), action_source=CommandServer(port=7000), fps=500).run()

# s1 partition
skills = Skills(
    [
        PolicySkill(name="manipulate", model=WorkerActionModel("./exports/bimanual", framework="gr00t"),
                    claims={"left_arm", "right_arm"}),
        DelegatedSkill(name="walk_to", controller=Nav2(), claims={"base"}),
        SetpointSkill(name="look_at", claims={"head"}),
    ],
    serve=[SkillsServer(port=7001)],
)
RobotRuntime(robot=RemoteRobot("s0.local:7000"), action_source=skills, fps=50).run()

# s2 partition (no device access)
Agent(planner, SkillsClient("s1.local:7001")).run("Take the box from the shelf.")
```

Skills on disjoint parts run concurrently under separate leases:

```python
skills.submit(Goal("look_at", {"target": "shelf"}))
skills.submit(Goal("walk_to", {"target": "shelf"})).result()
skills.submit(Goal("manipulate", {"task": "take the box"})).result()
```

`physicalai up` launches every partition from a deployment file that assigns
configuration files to hosts:

```yaml
partitions:
  s0: { host: rt-guest, config: s0.yaml }
  s1: { host: robot-main, config: s1.yaml }
  s2: { host: cloud, config: s2.yaml }
```

---

## 6. RobotRuntime

The constructor is unchanged. One method is added:

```python
class RobotRuntime:
    def __init__(self, robot, action_source, fps, cameras=None, callbacks=(), interpolator=None): ...  # existing
    @classmethod
    def from_config(cls, config: str | Path) -> Self: ...  # existing: jsonargparse over __init__
    def run(self, duration_s: float | None = None) -> None: ...  # existing: blocks
    def start(self) -> None: ...  # new: runs the loop in a background thread
```

Each tier capability enters through one of the existing slots:

| Slot            | Accepts                                                                       |
| --------------- | ----------------------------------------------------------------------------- |
| `robot`         | Hardware, `SafetyGate`, a vendor controller, `LoopRobot`, or `RemoteRobot`    |
| `action_source` | `PolicySource`, `TeleopSource`, `Skills`, `SetpointInbox`, or `CommandServer` |
| `callbacks`     | Recording, telemetry, and action filters                                      |
| `interpolator`  | Light System 0: splits each action into several robot commands (existing)     |

Because the configuration system parses the constructor signature, every composition in
this document can be written in Python, in YAML, or on the CLI.

---

## 7. Actions and Parts

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
positions. Part-keyed actions are the one change this design requires in the `Robot`
protocol and in `RobotRuntime`.

---

## 8. Goals and Skills

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
    def __init__(self, skills: Sequence[Skill], *, serve: Sequence[Server] = (),
                 access: AccessPolicy | None = None): ...
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

**Ticks.** On each tick, the runtime calls `Skills.update()`. `Skills` applies pending
attaches and detaches, queries each attached source for its parts, applies current
setpoints, holds parts that have no writer, and returns the merged part-keyed action.
All skills inside one `Skills` object therefore run at the host runtime's rate. Slower
sources hold between outputs, as `PolicySource` already does between action chunks.

**Leases.** Each part has at most one writer. Parts form a hierarchy, so a lease on
`whole_body` covers the arms and the base. A higher-priority goal may preempt a
lower-priority lease. If a lease misses its heartbeat, the skill is cancelled and the
parts hold position.

**`SkillHandle`.** A handle exposes `status`, `feedback`, `update`, `cancel`, and
`result`.

**`WorldState`.** `WorldState` stores the latest value of each fact, with one writer per
key and no history. `Skills` writes robot state and camera frames from each tick.
Perception components write detections and captions. Skills, success checks, and clients
read from it.

**Success checks.** A success check returns a confidence value rather than a boolean.
The following checks are illustrative:

```python
GripperClosedOnObject  # the gripper has closed on an object
ObjectAt               # a detector places the object in a target region
VLMJudge               # a ReasoningModel judges the outcome from images
```

---

## 9. Models and Backends

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

The model's class determines its placement:

| Placement    | Class (illustrative)        | Intended use                                                       |
| ------------ | --------------------------- | ------------------------------------------------------------------ |
| In-process   | `InferenceModel` (existing) | OpenVINO or ONNX models and other small models                     |
| Local worker | `WorkerActionModel`         | Models with conflicting dependencies, or that need crash isolation |
| Remote       | `RemoteActionModel`         | Models that do not fit on the local host                           |

A local worker is an inference server in a separate environment prepared by
`physicalai.isolate`, and it uses the same protocol as a remote server. Remote inference
is specified in the [policy server design](policy_server_design.md) but not yet
implemented. `InferenceModel` loads its weights in its constructor, so every in-process
model is loaded when the composition is built.

Remote System 1 inference is viable only when the action-chunk horizon exceeds the sum
of network round-trip time, inference time, and a safety margin. If a chunk arrives
late, the source holds and `Skills` reports starvation to the skill.

---

## 10. Links Across Partitions

### Robot command link (`RemoteRobot` and `CommandServer`)

`CommandServer` is the action source of a System 0 runtime in another partition.
`RemoteRobot` is its client and implements the `Robot` protocol for the tier above. The
link describes the available parts, accepts setpoints, and reports robot state. Each
setpoint carries a part, a mode, values, a timestamp, a deadline, and a lease.
`CommandServer` rejects setpoints that have expired or carry the wrong lease, and it
holds, decays, or stops when setpoints become stale. When several System 1 runtimes
share one System 0, `CommandServer` arbitrates between them by part.

A native or vendor controller can implement the same link directly when System 0 must
run beyond Python's real-time limits. `SetpointInbox` and `LoopRobot` provide the same
behavior in memory.

### Inference link (`RemoteActionModel` and a policy server)

The inference link describes a model, streams observations in and action chunks out,
and supports reset and health checks. Both sides timestamp their messages. It builds on
the existing [policy server design](policy_server_design.md).

### Skills link (`SkillsClient` and `SkillsServer`)

The skills link lists skills, submits goals, streams updates, and supports update,
cancel, and heartbeat calls. It also exposes `WorldState`, skill events, and an
emergency stop. Missed heartbeats expire the caller's leases. `McpServer` exposes the
same operations as MCP tools generated from the skill list.

---

## 11. Deployment

All layouts use the same components. They differ only in which implementation of each
link is chosen.

| Layout                         | Composition                                                                                                        |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| One process                    | One runtime; `robot` is hardware; the interpolator acts as System 0                                                |
| One host, several accelerators | System 0 through `LoopRobot`, or `CommandServer` over local IPC for a separate process; models assigned to devices |
| Robot, GPU server, and cloud   | Large action models are `RemoteActionModel`s; the agent runs in the cloud with a `SkillsClient`                    |
| KVM guests                     | One partition per guest, linked by `RemoteRobot` and `SkillsClient`; the System 2 guest has no device access       |
| Vendor System 0                | `robot` is the vendor driver, or `RemoteRobot` to the vendor's implementation of the robot command link            |

On a shared host, System 0 and System 2 should not share compute, and System 1 should
take priority over System 2. Jitter must be measured on the target platform.

---

## 12. Safety, Time, and Recording

Safety is layered. Each layer is enforced independently of the layers above it.

| Layer                | Component                        | Enforces                                                      |
| -------------------- | -------------------------------- | ------------------------------------------------------------- |
| Hardware / firmware  | Robot                            | Power, torque, and temperature limits                         |
| Safety gate          | `SafetyGate`                     | Joint, velocity, workspace, and force limits on every command |
| Stale-command hold   | `SetpointInbox`, `CommandServer` | Holds or stops when the tier above goes silent                |
| Leases and deadlines | `Skills`, `CommandServer`        | A single writer per part                                      |
| Access policy        | `Skills`                         | Which clients may run which skills                            |
| Partition boundary   | Deployment                       | The System 2 guest has no device access                       |

The `unrestricted` access mode removes permission checks only. Safety gates, leases,
and deadlines remain in force.

| Failure              | Response                                                    |
| -------------------- | ----------------------------------------------------------- |
| Client disconnects   | Its lease expires; the skill is cancelled; parts hold       |
| System 2 crashes     | As above; System 1 and System 0 continue                    |
| Remote model is late | The source holds and reports starvation; the skill may fail |
| System 1 goes silent | System 0 applies its stale-command hold                     |
| System 0 faults      | System 1 stops; running skills fail                         |

**Time.** All partitions share a clock. Deadlines are absolute, and `WorldState`
timestamps allow readers to detect stale facts.

**Recording.** `Skills` receives the runtime's callback bus when it connects, as every
action source does. It emits an event when a skill starts, is preempted, and ends. A
recording callback uses these events to produce one episode per skill run. The episode
contains the loop data in LeRobot format, the goal, the submitting client, the outcome,
model provenance, intervention segments, and the agent transcript when one exists.
Failed and cancelled runs are recorded in the same way as successful ones.

---

## 13. Requirements Coverage

| Requirement ([§2](#2-motivation))      | Mechanism                                                                                        |
| -------------------------------------- | ------------------------------------------------------------------------------------------------ |
| 1. System 0, 1, and 2 on one robot     | System 0 and System 1 are runtimes linked through `robot`; System 2 is an agent calling `Skills` |
| 2. Frontier model drives System 1 or 0 | Goals to policy skills; `ServoSkill` setpoints to System 0                                       |
| 3. Any deployment layout               | Each link has an in-memory and a remote implementation ([§11](#11-deployment))                   |
| 4. Models from any backend             | Models are owned by skills and agents; frameworks stay in plugins                                |
| 5. Concurrent work on disjoint parts   | Leases and part-keyed actions in `Skills`                                                        |
| 6. Teleoperation, HIL, and DAgger      | `TeleopSource`; `TeleopSkill` preemption; intervention segments in episodes                      |
| 7. Non-bypassable safety               | `SafetyGate` wraps the hardware; stale-command hold below every tier                             |
| 8. Labeled episode per run             | Skill events on the existing callback bus                                                        |
| 9. Existing code unchanged             | `RobotRuntime` constructor unchanged; flat actions remain valid                                  |
| 10. Python, YAML, and CLI equivalent   | Every component is a constructor argument of an existing slot                                    |
| 11. Frameworks in plugins              | `ActionModel` plugins and `physicalai.isolate` workers                                           |

---

## 14. Open Questions and Deferred Decisions

**Open questions**

1. **Rates within one tier.** All skills in one `Skills` object run at the host
   runtime's rate. Workloads that need separate rates or processes, such as navigation
   and manipulation, can run as two System 1 runtimes sharing one System 0. This
   requires part arbitration in `CommandServer`.
2. **Delegated controllers.** External controllers such as Nav2 must send commands
   through the robot command link so that `SafetyGate` and leases apply to them.
3. **Combining a fixed source with skills.** Whether a runtime may run a long-lived
   source (for example, teleoperation of the base) alongside skills, or whether such a
   source must itself be a skill.

**Deferred decisions**

| Topic                            | Needed by                     |
| -------------------------------- | ----------------------------- |
| Command modes and payloads       | Part-keyed actions            |
| Wire formats for the three links | Remote and VM deployment      |
| Lease durations and priorities   | `Skills`                      |
| Access-policy format             | Multiple clients              |
| Cross-partition transports       | Multi-host and KVM deployment |
| Real-time platform for System 0  | Hosted heavy System 0         |
| Clock synchronization            | Multi-partition deployment    |
| Deployment file format           | `physicalai up`               |
| Recording format extensions      | Data collection at scale      |
| Package location of `Agent`      | System 2 clients              |
| World-model interface            | Imagine-and-rank planning     |

---

## 15. Implementation Phases

| Phase | Scope                                                                   | Exit criterion                                            |
| ----- | ----------------------------------------------------------------------- | --------------------------------------------------------- |
| 1     | Part-keyed actions; `RobotRuntime.start()`                              | Existing tests and configurations pass                    |
| 2     | `SafetyGate`, `SetpointInbox`, `LoopRobot`                              | Two-runtime stack on one host; gate tests pass            |
| 3     | `Skills`, `Goal`, `PolicySkill`, `TeleopSkill`, leases, skill events    | Tabletop task from Python and YAML; HIL takeover recorded |
| 4     | Worker and remote models; `ModelSpec`; inference link                   | Identical actions across placements                       |
| 5     | `SkillsServer`, `SkillsClient`, `McpServer`; reasoning clients; `Agent` | Cloud agent and MCP host drive the same robot             |
| 6     | Setpoint skills; `CommandServer` and `RemoteRobot`                      | Agent drives System 0; System 0 holds on silence          |
| 7     | VM partitions; `physicalai up`                                          | Humanoid or mobile robot runs across guests               |
| 8     | Delegated navigation; world model                                       | Navigate, pick, and place in one task                     |

Phases 1 to 3 deliver value without the later phases.

---

## 16. Decisions

```text
story           Agent → Goal → Skill → SkillHandle; Skills drives RobotRuntime
clients         anything that submits goals; Agent is the built-in reasoning-model client
tiers           System 0 control, System 1 policy, System 2 deliberation
runtime         RobotRuntime runs one loop; its constructor is unchanged
composition     tiers compose through robot (below) and action_source (above)
links           Robot, ActionModel, and skills API, each in memory or remote
skills          Skills is an ActionSource; attached, setpoint, and delegated skills
actions         part-keyed command maps; flat arrays remain valid
concurrency     leases on hierarchical robot parts
models          owned by the skills and agents that use them; class sets placement
interop         ModelSpec checked at startup; plugins convert native formats
safety          SafetyGate wraps hardware; leases, deadlines, access policy
intervention    HIL is preemption by a higher-priority teleoperation skill
data            one labeled episode per skill run, from skill events
terminology     robot skill (Skill) is distinct from agent skill (SKILL.md)
```
