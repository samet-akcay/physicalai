# Tiered Robot Runtime: System 0 / 1 / 2 on Any Hardware Layout

**Status:** Proposal for design review.
**Supersedes:** [`composite_robot_architecture.md`](./composite_robot_architecture.md).
**Amends:** [`robot_runtime_architecture.md`](./robot_runtime_architecture.md) and the current `physicalai.runtime` package.
**Related:** [`policy_server_design.md`](./policy_server_design.md); Runtime PR #8 (`physicalai.isolate`);
Studio PRs #10, #23, #25 (framework plugins); the _Physical AI Agent_ proposal deck.

This document defines the architecture: components, responsibilities, and the contracts
between them. It does not define wire formats, transports, configuration schema, or
numeric limits; those are listed in [§19](#19-deferred-decisions). Code blocks are
pseudo-code for proposed APIs unless marked **existing**, and configuration snippets are
illustrative.

---

## Contents

1. [Summary](#1-summary)
2. [Motivation](#2-motivation)
3. [Terminology](#3-terminology)
4. [Architecture Overview](#4-architecture-overview)
5. [Examples](#5-examples)
6. [RobotRuntime and ControlLoop](#6-robotruntime-and-controlloop)
7. [Actions and Effectors](#7-actions-and-effectors)
8. [Robot Skills](#8-robot-skills)
9. [Models and Backends](#9-models-and-backends)
10. [Contracts](#10-contracts)
11. [Deployment Configurations](#11-deployment-configurations)
12. [Safety](#12-safety)
13. [Time and Recording](#13-time-and-recording)
14. [Clients](#14-clients)
15. [Relationship to Other Work](#15-relationship-to-other-work)
16. [Changes to Existing Code](#16-changes-to-existing-code)
17. [Implementation Phases](#17-implementation-phases)
18. [Risks](#18-risks)
19. [Deferred Decisions](#19-deferred-decisions)
20. [Decisions](#20-decisions)

---

## 1. Summary

The current runtime runs one action source on one robot in one fixed-rate loop:

```python
# existing
runtime = RobotRuntime(robot=so101, action_source=PolicySource(model), fps=30)
runtime.run()
```

This document proposes how that runtime extends to a three-tier robot system:

| Tier         | Responsibility                             | Typical rate           | Examples                                      |
| ------------ | ------------------------------------------ | ---------------------- | --------------------------------------------- |
| **System 2** | Interpret the task, plan, check outcomes   | event-driven, 0.1–5 Hz | cloud LLM/VLM; local VLM via SGLang/llama.cpp |
| **System 1** | Map observations to motion                 | 10–50 Hz               | VLA (GR00T, π0), ACT, diffusion, navigation   |
| **System 0** | Track setpoints, stabilize, enforce limits | 200 Hz – 1 kHz         | whole-body control, IK, impedance, vendor     |

Each tier's model may come from any framework or serving backend, and each tier may run in
the same process, another process, another accelerator, another VM on the same host, or
another machine.

The proposal in brief:

- One `RobotRuntime` per **partition** (process, VM, or machine) hosts the loops and models
  assigned to it and connects to the others.
- **Robot skills** (`pick`, `place`, `servo`, `navigate_to`) run on **leased effectors**.
  All clients — scripts, Studio, coding assistants over MCP, a built-in agent — start them
  through one API.
- Three contracts cross partition boundaries: the **robot command interface**, the
  **inference API**, and the **skills API**.
- Moving between deployment layouts is a configuration change.

---

## 2. Motivation

### 2.1 Industry direction

Robot stacks are converging on a three-tier hierarchy: a slow reasoning model over a fast
visuomotor policy over a high-rate controller (for example Figure Helix, NVIDIA GR00T,
Gemini Robotics ER + VLA, and classic deliberator / sequencer / controller designs). Robot
vendors we work with run these tiers on separate compute — frequently as separate KVM guests
on one SoC — because the models do not fit in one memory, the tiers have different OS and
real-time requirements, and partitioning simplifies the safety case.

### 2.2 Current state

| Capability                                             | Status                                                          |
| ------------------------------------------------------ | --------------------------------------------------------------- |
| One fixed-rate loop, one `ActionSource`, one `Robot`   | implemented (`RobotRuntime`)                                    |
| Inner command rate via interpolation (`fps × N`)       | implemented (`interpolator`)                                    |
| Sync / async / RTC inference execution                 | implemented (`SyncExecution`, `AsyncExecution`, `RTCExecution`) |
| Framework models via plugins and isolated environments | in progress (PR #8; Studio #10, #23, #25)                       |
| Remote inference                                       | designed (`policy_server_design.md`); not implemented           |
| Multiple decision-makers at different rates            | not supported                                                   |
| Language task → robot actions                          | not supported                                                   |
| Tiers in different processes, VMs, or machines         | not supported                                                   |
| Safety layer for agent-originated commands             | not supported                                                   |

### 2.3 Requirements

| #   | Requirement                                                                                                         |
| --- | ------------------------------------------------------------------------------------------------------------------- |
| R1  | Support System 0, 1, and 2 in any combination, including System 2 commanding System 0 directly.                     |
| R2  | Any tier's model can come from any backend (cloud API, llama.cpp, SGLang, vLLM, OpenVINO, ONNX, LeRobot, GR00T, …). |
| R3  | Tiers can run on different machines, VMs, or accelerators, or all on one host, without code changes.                |
| R4  | Multiple client types can drive the robot: Python, CLI, Studio, MCP hosts, a built-in agent, a human.               |
| R5  | No client, model, or partition failure can bypass safety limits.                                                    |
| R6  | Every autonomous run produces labeled training data.                                                                |
| R7  | Existing single-policy scripts and configs work unchanged.                                                          |
| R8  | Framework-specific code stays in plugins; the core does not import frameworks.                                      |

### 2.4 Non-goals

- Hypervisor setup, VM management, or device passthrough (platform layer).
- Hard real-time loops in Python. Loops above roughly 200 Hz stay in firmware, C/C++, or
  vendor stacks behind the `Robot` protocol.
- An agent or prompt framework. The built-in agent is a small reference client.
- A general-purpose middleware.
- Training (remains in `physicalai-train` and Studio).

---

## 3. Terminology

| Term                  | Definition                                                                                                            |
| --------------------- | --------------------------------------------------------------------------------------------------------------------- |
| **Tier**              | System 0, 1, or 2: a responsibility and rate band, not a process.                                                     |
| **Partition**         | An execution domain: process, container, VM, or machine.                                                              |
| **`RobotRuntime`**    | The main runtime. One per partition. Hosts the loops, skills executive, and models placed there.                      |
| **`ControlLoop`**     | A fixed-rate loop (the current `RobotRuntime` loop body). One per hosted tier.                                        |
| **Effector**          | An independently commandable part: `arm`, `gripper`, `left_arm`, `base`, `head`, `whole_body`.                        |
| **Command interface** | The command modes an effector accepts, such as joint position, end-effector pose, or base velocity.                   |
| **`ActionSource`**    | **Existing** protocol producing an action each tick (policy, teleop).                                                 |
| **Robot skill**       | A named robot capability (`pick`, `servo`, `navigate_to`) with typed parameters. Class: `Skill`.                      |
| **Agent skill**       | A `SKILL.md` instruction file for a coding assistant (repo `skills/`, `.agents/skills/`). Unrelated to robot skills.  |
| **`SkillHandle`**     | Supervision of one skill run: status, feedback, cancel, update, result.                                               |
| **Lease**             | Exclusive, time-limited write access to a set of effectors, renewed by heartbeat.                                     |
| **Executive**         | Internal component that runs skills, grants leases, maintains `WorldState`, and records runs. Exposed as `rt.skills`. |
| **`WorldState`**      | Latest-value store of timestamped facts (poses, detections, captions). Called `WorldView` in the superseded doc.      |
| **`ActionModel`**     | Observation → action chunk. The **existing** `InferenceModel`.                                                        |
| **`ReasoningModel`**  | Messages and images (optionally tools) → text or tool calls.                                                          |
| **`ModelSpec`**       | A model's declared inputs, outputs, and what its outputs command.                                                     |
| **Placement**         | Where a model runs relative to its caller: `inprocess`, `worker`, or `remote`.                                        |
| **Safety gate**       | The final transform before hardware; enforces hard limits.                                                            |
| **Intent policy**     | Per-client rules on which skills may run and which require approval.                                                  |

Throughout this document, "skill" means robot skill unless qualified.

---

## 4. Architecture Overview

### 4.1 Layers

```text
Clients    Python │ CLI │ Studio │ MCP host (e.g. Claude Code) │ built-in agent │ human
           ───────┴─────┴────────┴─────────────────────────────┴────────────────┴──────
                                  │ skills API
System 2   Executive (physicalai.skills)
seam       skills · leases · WorldState · intent policy · recording
           ────────────────────────────────────────────────────────────────────────────
System 1   ControlLoop, 10–50 Hz; action sources per effector ── inference API ──► ActionModel
                                  │ robot command interface
System 0   ControlLoop or vendor controller; safety gate · watchdog
           ────────────────────────────────────────────────────────────────────────────
Hardware   motors · sensors · cameras
```

### 4.2 Design rules

1. **Place loops by rate and models by memory.** System 0 runs next to the motors; the
   System 1 loop runs on the robot or a local edge host; System 2 can run anywhere. A model
   can run remotely while the loop that calls it stays local.
2. **Each tier presents a `Robot` to the tier above.** The System 1 loop drives System 0
   through the same `Robot` protocol it uses for hardware today. A vendor controller is a
   `Robot` implementation.
3. **Three contracts cross partitions:** the robot command interface, the inference API,
   and the skills API. Each must work in-process, across processes, across VMs, and across
   hosts.
4. **All clients use the skills API.** The built-in agent has no privileged path.
5. **Commands are leased and time-bounded.** Leases expire without heartbeats; setpoints
   carry deadlines; the lowest tier holds or stops when either lapses.
6. **Framework, data spec, and placement are independent configuration axes.**

### 4.3 User-facing API

```python
# single loop, unchanged
RobotRuntime(robot=so101, action_source=PolicySource(model), fps=30).run()

# full system from config, all in this process/host
with RobotRuntime.from_config("robot.yaml") as rt:
    rt.skills.submit("pick", {"object": "mug"}).result()

# one partition of a split system
RobotRuntime.from_config("robot.yaml", partition="s1").run()

# remote client with the same .skills / .world / .events() surface
rt = RuntimeClient("robot.local")
```

```bash
physicalai run robot.yaml                    # single partition
physicalai run robot.yaml --partition s0     # one partition of a split system
physicalai up  robot.yaml                    # development: start all partitions locally
```

---

## 5. Examples

Each example adds one concept.

### 5.1 One policy (existing)

```python
from physicalai.inference import InferenceModel
from physicalai.robot.so101 import SO101
from physicalai.runtime import AsyncExecution, PolicySource, RobotRuntime

model = InferenceModel("./exports/act_policy")
runtime = RobotRuntime(
    robot=SO101(port="/dev/ttyACM0"),
    action_source=PolicySource(model, execution=AsyncExecution()),
    fps=30,
)
runtime.run(duration_s=60)
```

This path does not change. Internally it becomes one `ControlLoop` with the source attached
to the robot's only effector.

### 5.2 Scripted skills

Two manipulation policies sequenced by a script. Introduces skills, handles, and success checks.

```python
from physicalai.runtime import RobotRuntime
from physicalai.skills import PolicySkill, Status
from physicalai.skills.checks import GripperClosedOnObject, ObjectAt

rt = RobotRuntime.from_config("tabletop.yaml")

rt.skills.register(PolicySkill(
    name="pick",
    description="Pick up the named object from the table.",
    model="pick_policy",                 # model name from config
    claims={"arm", "gripper"},
    task="pick up the {object}",         # language prompt, rendered from params
    success=GripperClosedOnObject(),
))
rt.skills.register(PolicySkill(
    name="place",
    description="Place the held object at the named location.",
    model="place_policy",
    claims={"arm", "gripper"},
    task="place it in the {location}",
    success=ObjectAt(),
))

with rt:
    for obj, loc in [("red block", "left bin"), ("blue block", "right bin")]:
        picked = rt.skills.submit("pick", {"object": obj}).result()
        if picked.status is not Status.SUCCEEDED:
            print("pick failed:", picked.evidence)
            continue
        rt.skills.submit("place", {"location": loc}).result()
```

What `submit("pick")` does:

```text
check intent policy → lease {arm, gripper} → build a PolicySource for pick_policy
→ attach it to the System 1 loop for {arm, gripper}
→ success check met → handle SUCCEEDED → detach → release lease → save episode
```

### 5.3 A reasoning model as planner

The script is replaced by a reasoning model. Nothing below the skills API changes.

```python
planner = load_reasoning_model("anthropic", model="claude-…")   # or any backend in §9.3
agent = ToolLoopAgent(planner, rt.skills)

with rt:
    print(agent.run("Put red blocks in the left bin and blue blocks in the right bin.").summary)
```

The agent receives the registered skills (`pick`, `place`) and queries (`observe`) as
tools. A failed skill returns its evidence as the tool result.

### 5.4 A coding assistant as planner (MCP)

```bash
physicalai run tabletop.yaml --serve-skills --mcp      # on the robot
```

The MCP tool list is generated from the skill registry, so the built-in agent and the coding
assistant see the same tools. If the client disconnects, its lease expires, the skill is
cancelled, and the robot holds.

### 5.5 System 2 commanding System 0

For tasks without a trained policy, a planner can move the arm through a setpoint skill:

```python
rt.skills.register(ServoSkill(
    name="servo",
    description="Move the end effector by a small delta in the base frame.",
    claims={"arm"},
    max_step_m=0.01,
))
```

```text
observe() → servo(dx=0.01) → observe() → servo(dx=0.01, dy=-0.005) → … → stop()
```

Each call sends one deadline-bounded setpoint to System 0. The safety gate clamps it and the
watchdog holds the arm when calls stop. This works the same whether System 0 is a
`ControlLoop` or a vendor controller.

### 5.6 Mixed backends and a remote model

```yaml
models:
  planner: {
      kind: reasoning,
      backend: openai-compatible,
      base_url: http://localhost:30000/v1,
    } # SGLang
  judge: { kind: reasoning, backend: llama.cpp, path: models/judge.gguf }
  pick_policy:
    { kind: action, framework: gr00t, placement: remote, endpoint: gpu-box }
  place_policy: { kind: action, framework: lerobot, placement: worker } # isolated env (PR #8)
  detector:
    { kind: action, framework: openvino, path: exports/detector, device: NPU }
```

Skills refer to models by name; no Python code names a framework. Moving `pick_policy`
between `worker` and `remote` changes one line.

### 5.7 Humanoid across partitions

```yaml
robot:
  driver: unitree_g1
  effectors: [whole_body, left_arm, right_arm, base, head] # arms and base via whole_body

tiers:
  s0: { controller: wbc } # learned whole-body controller
  s1: { fps: 30 }

skills:
  manipulate: { type: policy, model: bimanual, claims: [left_arm, right_arm] }
  walk_to: { type: delegated, driver: nav2, claims: [base] }
  look_at: { type: setpoint, claims: [head] }

partitions: # omit to run everything on one host
  s0: { kind: vm, hosts: [tiers.s0] }
  s1: { kind: vm, hosts: [tiers.s1, executive, models.bimanual] }
  s2: { kind: vm, hosts: [models.planner, agent] }
```

```python
rt.skills.submit("look_at", {"target": "shelf"})                 # lease {head}
rt.skills.submit("walk_to", {"target": "shelf"}).result()        # lease {base}, concurrent
rt.skills.submit("manipulate", {"task": "take the box"}).result()
```

Skills on disjoint effectors run concurrently; leases ensure one writer per effector. The
System 2 partition reaches the robot only through the skills API. The Python code is the
same with or without `partitions`.

---

## 6. RobotRuntime and ControlLoop

### 6.1 Structure

```text
RobotRuntime               one per partition
 ├─ ControlLoop "s0"       if System 0 is hosted here
 ├─ ControlLoop "s1"       if System 1 is hosted here; its Robot is System 0 (local or remote)
 ├─ Executive              if hosted here (normally with System 1); exposed as rt.skills
 ├─ models                 in-process, isolated workers, or remote clients
 └─ links                  connections to other partitions
```

The current `RobotRuntime` loop body (timing, resilient I/O, callbacks, interpolation,
shutdown) moves into `ControlLoop` without behavior changes. The existing constructor remains
as the single-loop case.

### 6.2 API

```python
class RobotRuntime:
    def __init__(self, robot, action_source, fps, cameras=None, callbacks=(), interpolator=None): ...  # existing

    @classmethod
    def from_config(cls, config, *, partition: str | None = None) -> Self: ...

    def connect(self) -> None: ...        # existing
    def run(self, *, duration_s: float | None = None) -> RunReason: ...   # existing, blocking
    def start(self) -> None: ...          # non-blocking
    def stop(self) -> None: ...           # existing; stops all hosted loops
    def disconnect(self) -> None: ...     # existing

    skills: SkillsAPI                     # local executive or a client to a remote one
    world: WorldState
    loops: Mapping[str, ControlLoop]
    def events(self) -> EventStream: ...
```

`from_config` reads one system description, selects the part assigned to `partition`, starts
the loops and models placed there, and connects to the rest.

### 6.3 ControlLoop changes

The tick keeps its current structure. Four additions:

```python
def _tick(self, step):
    obs = self._read_observation()                     # existing
    action = {}
    for effectors, source in self._attached.items():   # NEW: one source per lease
        action |= normalize(source.update(obs, ...), effectors)
    action |= self._fresh_setpoints()                  # NEW: setpoints whose deadline has not passed
    action = self._hold_unclaimed(action)              # NEW: idle effectors hold
    action = self._callbacks.on_action_ready(action)   # existing
    action = self._gate.enforce(action, obs)           # NEW: always last
    self._robot.send_action(action)                    # existing
```

- `attach(source, effectors, lease)` and `detach(lease)` take effect at a tick boundary, so
  switching skills does not restart `run()`.
- `as_robot()` exposes the loop as a `Robot` to the tier above.

### 6.4 What one RobotRuntime can host

| Tier                                       | Hosted?   | Notes                                  |
| ------------------------------------------ | --------- | -------------------------------------- |
| System 0, light (interpolation, smoothing) | yes       | nested in the System 1 loop, as today  |
| System 0, heavy (learned controller)       | yes       | separate process; real-time scheduling |
| System 0 in a vendor stack or another VM   | connected | robot command interface                |
| System 1                                   | yes       | `ControlLoop`                          |
| System 2 executive and built-in agent      | yes       | event-driven, not a fixed-rate loop    |
| System 2 in another VM or the cloud        | connected | skills API                             |

---

## 7. Actions and Effectors

### 7.1 Effector-keyed actions

Actions are currently a flat `np.ndarray`. Multi-effector robots need independently owned
parts, so an action becomes a mapping from effector to command. A flat array remains valid
for sources that control a single effector, so `PolicySource` and `TeleopSource` do not change.

```python
action = {
    "left_arm": Command(mode=EE_POSE, values=..., frame="base"),
    "base":     Command(mode=TWIST, values=...),
}
```

Candidate command modes: joint position, joint velocity, end-effector pose, base or
end-effector twist, gripper, and controller-specific whole-body targets.

### 7.2 Robot protocol

The `Robot` protocol gains one optional method, `command_interfaces()`, describing each
effector and the modes it accepts. Robots without it expose a single effector commanded in
joint position, which is today's behavior.

### 7.3 Tiers as Robot implementations

```python
class LoopRobot:       # a local System 0 loop, seen by System 1
    def get_observation(self):        return self._loop.latest_state()
    def send_action(self, action, *, goal_time=0.1):
        self._loop.write_setpoints(action, deadline=now() + goal_time)

class RemoteRobot:     # System 0 in another partition
    def get_observation(self):        return self._link.latest_state()
    def send_action(self, action, *, goal_time=0.1):
        self._link.write_setpoints(action, deadline=now() + goal_time)
```

The System 1 loop uses the same code whether its `Robot` is hardware, `LoopRobot`, or
`RemoteRobot`.

---

## 8. Robot Skills

Package: `physicalai.skills`. It has no LLM dependency. Plugins can contribute skills
through entry points.

### 8.1 Skill protocol

```python
class Skill(Protocol):
    name: str
    description: str                      # shown in Studio, CLI help, and LLM tool specs
    params: type[Params]                  # typed parameters → schema for tools, forms, CLI
    claims: frozenset[Effector]           # leased for the run

    def can_start(self, params, world: WorldState) -> Precondition: ...
    def start(self, params, ctx: SkillContext) -> SkillRun: ...
```

`SkillContext` gives the skill its lease, `WorldState`, the hosted loops, and the configured
models.

Three skill types:

| Type          | Commands the robot by                           | Examples                         |
| ------------- | ----------------------------------------------- | -------------------------------- |
| **Attached**  | attaching an `ActionSource` to a loop           | `PolicySkill` (VLA, ACT), teleop |
| **Setpoint**  | sending deadline-bounded setpoints per call     | `ServoSkill`, look-at, gripper   |
| **Delegated** | driving an external controller and mirroring it | `Nav2Skill`, vendor locomotion   |

### 8.2 Examples

`PolicySkill` is a generic "run this policy" skill. Pick, place, and similar tasks are
configured instances, not subclasses.

```python
class PolicySkill:
    def start(self, params, ctx):
        source = PolicySource(                              # existing class
            ctx.models.action(self.model),                  # any framework, any placement
            task=self.task.format(**params),
        )
        ctx.loops["s1"].attach(source, effectors=self.claims, lease=ctx.lease)
        return AttachedRun(source, success=self.success)
```

```python
class ServoSkill:
    def start(self, params, ctx):
        target = offset(ctx.world.get("arm.ee_pose").value, clamp(params, self.max_step_m))
        ctx.loops["s0"].write_setpoints({"arm": Command(EE_POSE, target)}, lease=ctx.lease)
        return SetpointRun(done=lambda: reached(ctx.world, target))
```

```python
class Nav2Skill:
    def start(self, params, ctx):
        goal = self._nav2.send_goal(params.target)          # external controller
        return DelegatedRun(status=goal.status, feedback=goal.feedback, cancel=goal.cancel)
```

### 8.3 Leases

- A skill run holds a lease on the effectors it claims; one writer per effector.
- Effectors can have parents: `whole_body` contains `left_arm`, `right_arm`, and `base`.
  Leasing a parent conflicts with leasing any child.
- A higher-priority request preempts a lower-priority lease and cancels that run.
- Leases expire unless the holder sends heartbeats. Expiry cancels the run and the
  effectors hold.

### 8.4 SkillHandle

```python
class SkillHandle:
    def status(self) -> Status: ...                # PENDING | RUNNING | SUCCEEDED | FAILED | CANCELLED
    def feedback(self) -> Mapping[str, Any]: ...   # progress, starvation, distance remaining, ...
    def update(self, params) -> None: ...          # e.g. change the task prompt while running
    def cancel(self, reason: str = "") -> None: ...
    def result(self) -> SkillResult: ...           # blocks; status, evidence, episode reference
```

The executive supervises each run: it polls the run's status, evaluates its success check,
applies timeouts, and on completion detaches sources, releases the lease, and closes the
episode.

### 8.5 WorldState

`WorldState` holds the latest value of each fact that skills, success checks, and clients
read. It is not a message bus and keeps no history (recording does).

| Key             | Writer           | Example value                |
| --------------- | ---------------- | ---------------------------- |
| `arm.ee_pose`   | System 1 loop    | pose in the `base` frame     |
| `gripper.state` | System 1 loop    | width 0.031 m, effort 4.2 N  |
| `detections`    | detector         | `[mug @ (0.42, 0.10)]`       |
| `scene.caption` | VLM captioner    | "mug left of the plate"      |
| `nav.pose`      | navigation stack | base pose in the `map` frame |

```python
class WorldState:
    def get(self, key) -> StateEntry | None: ...          # value, timestamp, source
    def publish(self, key, value, *, source, stamp): ...  # one writer per key
    def subscribe(self, key, fn) -> Subscription: ...
```

Perceivers (detectors, captioners, localization) run at their own rates and publish into
`WorldState`.

### 8.6 Success checks

A success check returns an estimate with a confidence, not a boolean, because many checks
are model calls.

```python
class GripperClosedOnObject:            # cheap: gripper closed but not empty
    def evaluate(self, world):
        g = world.get("gripper.state").value
        holding = g.width > self.min_width and g.effort > self.min_effort
        return SuccessEstimate(met=holding, confidence=1.0 if holding else 0.0)

class ObjectAt:                         # detector-based: object inside a named region
    def evaluate(self, world):
        det = find(world.get("detections").value, self.object)
        inside = det is not None and self.region.contains(det.center)
        return SuccessEstimate(met=inside, confidence=det.score if inside else 0.0)

class VLMJudge:                         # expensive: asks a reasoning model
    def evaluate(self, world):
        r = self.model.complete([user(self.question, images=[world.get("camera.top").value])])
        return SuccessEstimate(met=r.yes, confidence=r.confidence)
```

---

## 9. Models and Backends

### 9.1 Model interfaces

Planner, navigator, manipulator, and judge are roles that skills bind to models. Models
expose one of two interfaces:

```python
class ActionModel(Protocol):          # = existing InferenceModel
    def predict_action_chunk(self, observation) -> np.ndarray: ...
    def reset(self) -> None: ...

class ReasoningModel(Protocol):
    def complete(self, messages, *, tools=(), response_format=None) -> Completion: ...
```

A world-model interface (`rollout(obs, actions)`) can be added when a concrete model is in scope.

### 9.2 ModelSpec

Frameworks describe model inputs differently (LeRobot features, GR00T `modality.json`, the
PhysicalAI `Observation`). Each model declares a `ModelSpec` — named inputs and outputs and
what the output commands — and the runtime checks it against what the robot provides at
startup rather than during a run. Plugins convert their native formats into a `ModelSpec`.
Where names differ, a skill's config maps them:

```yaml
skills:
  pick:
    model: pick_policy
    features:
      images.ego_view: camera.head
      images.wrist: camera.wrist_r
      action.right_arm: right_arm
```

### 9.3 Backends

| Interface        | Backends                                                                                                                      |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `ActionModel`    | OpenVINO, ONNX (existing adapters); LeRobot and GR00T via plugins (Studio #10, #23)                                           |
| `ReasoningModel` | OpenAI-compatible servers (SGLang, vLLM, llama.cpp server, Ollama); Anthropic; Gemini; in-process OpenVINO GenAI or llama.cpp |

Framework adapters keep using the existing `physicalai.inference.adapters` entry-point group.
Most LLM servers implement the OpenAI-compatible API, so one client covers most local and
hosted options.

### 9.4 Placement

Placement is independent of framework.

| Placement   | Started by                 | Use                                       |
| ----------- | -------------------------- | ----------------------------------------- |
| `inprocess` | the runtime                | OpenVINO/ONNX exports, small models       |
| `worker`    | the runtime, via `isolate` | conflicting dependencies, crash isolation |
| `remote`    | external (e.g. a GPU host) | models that do not fit locally            |

An isolated worker is treated as an inference server on the local host, so `worker` and
`remote` share adapter code and protocol. Remote inference is designed in
`policy_server_design.md` but not implemented.

Clients for existing policy servers (openpi, LeRobot async inference, GR00T's inference
server) can present those servers as `ActionModel`s without re-hosting the model.

### 9.5 Remote System 1 models

A remote System 1 model is viable when one action chunk covers the round trip:

```text
chunk duration > network round trip + inference time + margin
```

`AsyncExecution` and `RTCExecution` already request the next chunk while the current one
plays. When a chunk arrives late, the source holds and the skill reports starvation in its
feedback. Models that produce one action per call should stay local.

### 9.6 Shared hosts

When all models share one host, each model is assigned an accelerator. System 0 should not
share compute with System 2, and System 1 should have priority over System 2 on a shared
accelerator. GPU preemption is limited, so System 1 jitter under System 2 load must be
measured before relying on sharing.

---

## 10. Contracts

Each contract is defined by its operations and semantics. Wire formats and transports are
deferred (§19).

### 10.1 Robot command interface (System 1/2 ↔ System 0)

| Aspect     | Definition                                                                                                                                                                                     |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Operations | describe effectors and modes; write setpoints; read state                                                                                                                                      |
| Setpoints  | effector, mode, values, timestamp, deadline, lease                                                                                                                                             |
| Semantics  | expired setpoints are ignored; stale effectors apply a configured fallback (hold, decay, stop); setpoints without the active lease are rejected; System 0 enforces limits and reports clamping |
| Constraint | must be implementable without Python, so vendor or real-time controllers can provide it                                                                                                        |

### 10.2 Inference API (loops ↔ action models)

| Aspect     | Definition                                                                        |
| ---------- | --------------------------------------------------------------------------------- |
| Operations | describe model spec; stream observations and receive action chunks; reset; health |
| Semantics  | replies reference the request they answer; timestamps on both sides               |
| Basis      | gRPC streaming design in `policy_server_design.md`                                |

### 10.3 Skills API (clients ↔ executive)

| Aspect     | Definition                                                                                            |
| ---------- | ----------------------------------------------------------------------------------------------------- |
| Operations | list skills; submit; stream status and feedback; update; cancel; heartbeat; observe; events; stop all |
| Semantics  | a client's leases expire without heartbeats; observe returns state and downsampled images             |
| Adapters   | MCP, generated from the skill list                                                                    |

---

## 11. Deployment Configurations

All configurations use the same system description; only partitioning and model placement
differ.

| Configuration                  | What differs                                                                    |
| ------------------------------ | ------------------------------------------------------------------------------- |
| One process                    | System 1 loop only (System 0 = interpolation); models in-process or hosted APIs |
| One host, several accelerators | System 0 in its own process; models assigned to accelerators                    |
| Robot + GPU host + cloud       | large action models `remote`; reasoning model hosted                            |
| KVM guests on one SoC          | one partition per tier; System 2 guest has no device access                     |
| Vendor System 0                | vendor implements the robot command interface                                   |

---

## 12. Safety

| Layer                        | Location                                   | Enforces                                          | Client can bypass |
| ---------------------------- | ------------------------------------------ | ------------------------------------------------- | ----------------- |
| Hardware e-stop and firmware | robot                                      | power, torque, temperature                        | no                |
| Safety gate (authoritative)  | lowest software tier we run, or the vendor | joint, velocity, workspace, force limits          | no                |
| Safety gate (secondary)      | System 1 loop                              | same limits, earlier                              | no                |
| Leases and deadlines         | executive and System 0                     | one writer per effector; stale commands hold      | no                |
| Intent policy                | executive                                  | allowed skills per client, approvals, rate limits | per profile       |
| Partition boundary           | hypervisor                                 | System 2 guest has no device access               | no                |

The safety gate is a dedicated slot after the callback chain, not a callback. An
`unrestricted` intent profile removes intent restrictions only; gates, leases, and deadlines
always apply.

Failure handling:

| Failure                          | Response                                          |
| -------------------------------- | ------------------------------------------------- |
| Client disconnects               | lease expires; skill cancelled; effectors hold    |
| System 2 partition fails         | as above; System 1 and System 0 continue          |
| Remote model late or unavailable | source holds; starvation reported; skill may fail |
| System 1 stops sending           | System 0 applies its stale-command fallback       |
| System 0 fault                   | System 1 loop stops; active skills fail           |

---

## 13. Time and Recording

**Time.** Partitions share a synchronized clock. Observations, setpoints, inference messages,
and events carry timestamps, and setpoint deadlines are absolute times. `WorldState` entries
carry their production time; readers decide staleness.

**Recording.** Each skill run is recorded as one episode containing:

- observations and actions at loop rate, in LeRobot format;
- the skill, parameters, requesting client, outcome, and evidence;
- provenance of every model used (framework, artifact or endpoint, placement);
- events such as gate clamps, starvation, and lease changes;
- the reasoning-model transcript, when an agent was involved.

Failed and cancelled runs are recorded the same way as successful ones.

---

## 14. Clients

All clients use the skills API, locally or remotely.

```python
rt = RuntimeClient("robot.local")
for s in rt.skills.list():
    print(s.name, s.claims)
h = rt.skills.submit("pick", {"object": "mug"})
print(h.result().status)
```

```bash
physicalai skills list
physicalai skills run pick --object mug
physicalai stop
```

**MCP.** The MCP server is generated from the skill list plus `observe` and `stop`. Adding a
skill adds a tool. The MCP session keeps the client's leases alive.

**Reference agent.** A minimal tool-calling loop; it could live in the `physicalai-agent`
package rather than the runtime.

```python
def run(self, task):
    messages = [system(PROMPT), user(task, images=self.skills.observe().images)]
    while True:
        reply = self.model.complete(messages, tools=self.skills.as_tools())
        if not reply.tool_calls:
            return reply.text
        for call in reply.tool_calls:
            result = self.skills.submit(call.name, call.arguments).result()
            messages.append(tool_result(call.id, result))
```

**Periodic supervision** (System 1 policy with a System 2 check):

```python
h = rt.skills.submit("manipulate", {"task": "put red blocks in the left bin"})
while h.status() is Status.RUNNING:
    time.sleep(2.0)
    verdict = judge.complete([user("Stuck, done, or progressing?", images=rt.skills.observe().images)])
    if verdict.done:
        h.cancel()
    elif verdict.stuck:
        h.update({"task": "push the blocks apart, then continue"})
```

---

## 15. Relationship to Other Work

### 15.1 Superseded autonomy-layer doc

| Previous term                      | This document                    | Change                                     |
| ---------------------------------- | -------------------------------- | ------------------------------------------ |
| `physicalai.autonomy`              | `physicalai.skills`              | named after its contents                   |
| `Skill`                            | `Skill`                          | adds effector claims and typed parameters  |
| `PickSkill`, `PlaceSkill`          | `PolicySkill(name="pick", …)`    | configured instances instead of subclasses |
| `SkillExecution`                   | `SkillHandle`                    | adds `update()`                            |
| `SkillRegistry`, `SkillDispatcher` | executive (`rt.skills`), leases  | concurrent skills on disjoint effectors    |
| `Goal`                             | skill name + params              | success check moved onto the skill         |
| `WorldView`                        | `WorldState`                     | renamed                                    |
| `CompositeSource`, arbiter         | leases + per-effector attachment | no merge logic                             |
| one `RobotRuntime`, one loop       | one `RobotRuntime` per partition | supports System 0 and partitions           |

Retained: the deliberative/reactive split, the handle lifecycle (from ROS 2 actions),
success as a confidence estimate, the final safety gate, and one episode per run.

### 15.2 Physical AI Agent proposal (deck)

The deck describes developer tooling: `physicalai-agent init`, agent skills, Studio and
Runtime MCP servers, guarded profiles, an `agent run` loop, run logs, and memory. This design
does not depend on it. If adopted, its components map as follows:

| Deck component                 | Maps to                                             |
| ------------------------------ | --------------------------------------------------- |
| Runtime MCP tools              | generated from the skill list                       |
| `RobotSession`, `PolicyRunner` | skills API client (possibly remote)                 |
| `ActionGuard`                  | intent policy (permissions) + safety gates (limits) |
| `start_policy`, `set_task`     | `submit()` a policy skill, `handle.update()`        |
| `move_joints_delta`            | a setpoint skill such as `ServoSkill`               |
| `agent/loop.py`, `ModelClient` | reference agent, `ReasoningModel`                   |
| run logs, memory               | recorded episodes and transcripts                   |

### 15.3 Runtime and Studio PRs

| Work                                 | Role in this design                                          |
| ------------------------------------ | ------------------------------------------------------------ |
| Runtime PR #8 (`physicalai.isolate`) | `worker` placement                                           |
| Studio PR #10 (LeRobot)              | LeRobot adapter; source of a LeRobot `ModelSpec`             |
| Studio PR #23 (GR00T + OpenVINO)     | GR00T adapter; worker protocol aligns with the inference API |
| Studio PR #25 (isolate validation)   | validation baseline for worker placement                     |
| `policy_server_design.md`            | basis for the inference API and `remote` placement           |

---

## 16. Changes to Existing Code

| Area             | Change                                                 | Breaking                           |
| ---------------- | ------------------------------------------------------ | ---------------------------------- |
| `RobotRuntime`   | loop body moves to `ControlLoop`; constructor retained | no, for constructor users          |
| Actions          | flat array or effector mapping                         | callbacks that assume a flat array |
| `Robot` protocol | optional `command_interfaces()`                        | no                                 |
| Safety           | dedicated gate slot                                    | no                                 |
| Inference        | `ModelSpec`, placement, remote client                  | no                                 |
| New packages     | `physicalai.skills`, reasoning-model clients           | no                                 |
| CLI              | partitions, skills, stop                               | no                                 |

---

## 17. Implementation Phases

| Phase | Scope                                                             | Exit criterion                                    |
| ----- | ----------------------------------------------------------------- | ------------------------------------------------- |
| 1     | Extract `ControlLoop` from `RobotRuntime`                         | existing runtime tests pass unchanged             |
| 2     | Effector-keyed actions, attach/detach, deadlines, safety gate     | existing robots unchanged; gate fault tests       |
| 3     | Inference API, placement, `ModelSpec` (first-party, LeRobot)      | same policy gives same actions in every placement |
| 4     | Executive, leases, handles, recording; scripted client            | tabletop task with policies from two frameworks   |
| 5     | Reasoning-model clients, skills API, MCP adapter, reference agent | hosted model and MCP host drive the same robot    |
| 6     | Setpoint skills; robot command interface across processes         | System 2 commands System 0; watchdog holds        |
| 7     | Hosted System 0 loop; partitions across VMs                       | humanoid or mobile manipulator on separate guests |
| 8     | Delegated navigation; world model                                 | navigate → pick → place                           |

Phases 1–4 are useful on their own.

---

## 18. Risks

| Risk                                   | Notes                                                        |
| -------------------------------------- | ------------------------------------------------------------ |
| Cross-framework `ModelSpec` binding    | largest effort; most integration failures expected here      |
| Remote System 1 latency                | requires chunked models                                      |
| Accelerator sharing between S1 and S2  | requires measurement                                         |
| Action mapping migration for callbacks | small but breaking; best done while the runtime API is young |
| Robot skill vs agent skill terminology | mitigated by qualified terms and separate locations          |
| Scope                                  | each phase is justified by its demo before the next starts   |

---

## 19. Deferred Decisions

These are intentionally left open until a phase needs them.

| Topic                       | Open question                                                          | Needed by |
| --------------------------- | ---------------------------------------------------------------------- | --------- |
| Command modes               | exact set and payload of each mode, especially whole-body targets      | phase 2   |
| Configuration schema        | structure of the system description, partitions, and links             | phase 3   |
| Inference wire format       | message schema; tensor transfer on the same host                       | phase 3   |
| Skills API wire format      | gRPC, HTTP, or both; authentication                                    | phase 5   |
| Lease parameters            | TTLs, heartbeat rates, priority levels                                 | phase 4   |
| Intent policy format        | profile structure, approval flow                                       | phase 5   |
| Robot command wire format   | message layout; language-neutral schema                                | phase 6   |
| Cross-partition transports  | shared memory, VM-to-VM channels, network; ROS 2 / Zenoh / DDS bridges | phase 6–7 |
| Real-time platform          | RT Linux vs RTOS for System 0; scheduling and isolation settings       | phase 7   |
| Clock synchronization       | mechanism per deployment; tolerated offset                             | phase 6   |
| Recording format extensions | how skill metadata and transcripts extend the LeRobot format           | phase 4   |
| Reference agent location    | runtime package or `physicalai-agent`                                  | phase 5   |
| World-model interface       | rollout contract and use in planning                                   | phase 8   |

---

## 20. Decisions

```text
tiers           System 0 control, System 1 reactive policy, System 2 deliberation
main runtime    one RobotRuntime per partition, hosting its loops, executive, and models
loops           ControlLoop = current loop body; one per hosted tier
tier interface  each tier is a Robot to the tier above
actions         effector-keyed mappings; flat arrays still valid for single effectors
skills          physicalai.skills: Skill, SkillHandle, leases, WorldState; accessed as rt.skills
skill types     attached, setpoint, delegated
concurrency     effector leases, including parent/child effectors
models          ActionModel and ReasoningModel; roles are bindings, not classes
interop         ModelSpec checked at startup; plugins convert native formats
placement       inprocess, worker, remote; independent of framework
contracts       robot command interface, inference API, skills API
safety          gate at the lowest tier; leases expire; setpoints have deadlines; intent policy per client
data            one labeled episode per skill run, with model provenance
terminology     "robot skill" (physicalai.skills) vs "agent skill" (SKILL.md)
```
