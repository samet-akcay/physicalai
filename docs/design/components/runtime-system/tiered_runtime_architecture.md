# Tiered Robot Runtime: System 0 / 1 / 2 on Any Hardware Layout

**Status:** Proposal for design review.
**Supersedes:** [`composite_robot_architecture.md`](./composite_robot_architecture.md).
**Amends:** [`robot_runtime_architecture.md`](./robot_runtime_architecture.md) and the current `physicalai.runtime` package.
**Related:** [`policy_server_design.md`](./policy_server_design.md); Runtime PR #8 (`physicalai.isolate`);
Studio PRs #10, #23, #25 (framework plugins); the _Physical AI Agent_ proposal deck.

Code blocks are pseudo-code for proposed APIs unless marked **existing**. All names are open
to review.

---

## Contents

1. [Summary](#1-summary)
2. [Motivation](#2-motivation)
3. [Terminology](#3-terminology)
4. [Architecture Overview](#4-architecture-overview)
5. [Examples](#5-examples)
6. [RobotRuntime and ControlLoop](#6-robotruntime-and-controlloop)
7. [Actions, Effectors, and Command Interfaces](#7-actions-effectors-and-command-interfaces)
8. [Robot Skills](#8-robot-skills)
9. [Models and Backends](#9-models-and-backends)
10. [Contracts and Bindings](#10-contracts-and-bindings)
11. [Deployment Configurations](#11-deployment-configurations)
12. [Safety](#12-safety)
13. [Time](#13-time)
14. [Recording](#14-recording)
15. [Clients](#15-clients)
16. [Relationship to Other Work](#16-relationship-to-other-work)
17. [Changes to Existing Code](#17-changes-to-existing-code)
18. [Implementation Phases](#18-implementation-phases)
19. [Risks and Open Questions](#19-risks-and-open-questions)
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
  **inference API**, and the **skills API**. Each has bindings from direct calls to TCP.
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

- Hypervisor setup, VM management, or device passthrough (platform layer: KVM, ACRN, Intel
  ECI, vendor BSPs).
- Hard real-time loops in Python. Loops above ~200 Hz stay in firmware, C/C++, or vendor
  stacks behind the `Robot` protocol.
- An agent or prompt framework. The built-in agent is a small reference client.
- A general-purpose middleware. Three narrow contracts, with optional ROS 2 / Zenoh / DDS bridges.
- Training (remains in `physicalai-train` and Studio).

---

## 3. Terminology

| Term                  | Definition                                                                                                                           |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| **Tier**              | System 0, 1, or 2: a responsibility and rate band, not a process.                                                                    |
| **Partition**         | An execution domain: process, container, VM, or machine.                                                                             |
| **`RobotRuntime`**    | The main runtime. One per partition. Hosts the loops, skills executive, and models placed there.                                     |
| **`ControlLoop`**     | A fixed-rate loop (the current `RobotRuntime` loop body). One per hosted tier.                                                       |
| **Effector**          | An independently commandable part: `arm`, `gripper`, `left_arm`, `base`, `head`, `whole_body`.                                       |
| **Command interface** | The command modes an effector accepts: joint position/velocity, end-effector pose, twist, whole-body target.                         |
| **`ActionSource`**    | **Existing** protocol producing an action each tick (policy, teleop).                                                                |
| **Robot skill**       | A named robot capability (`pick`, `servo`, `navigate_to`) with typed parameters. Class: `Skill`.                                     |
| **Agent skill**       | A `SKILL.md` instruction file for a coding assistant (repo `skills/`, `.agents/skills/`). Unrelated to robot skills.                 |
| **`SkillHandle`**     | Supervision of one skill run: status, feedback, cancel, update, result.                                                              |
| **Lease**             | Exclusive, time-limited write access to a set of effectors, renewed by heartbeat.                                                    |
| **Executive**         | Internal component that runs skills, grants leases, maintains `WorldState`, and writes the event log. Exposed as `runtime.skills`.   |
| **`WorldState`**      | Latest-value store of timestamped facts (poses, detections, captions). One writer per key. Called `WorldView` in the superseded doc. |
| **`ActionModel`**     | Observation → action chunk. The **existing** `InferenceModel`.                                                                       |
| **`ReasoningModel`**  | Messages and images (optionally tools) → text or tool calls.                                                                         |
| **`ModelSpec`**       | A model's declared inputs, outputs, shapes, frames, units, and command mode.                                                         |
| **Placement**         | Where a model runs relative to its caller: `inprocess`, `worker`, or `remote`.                                                       |
| **Binding**           | The transport used for a contract between two partitions.                                                                            |
| **Safety gate**       | The final transform before hardware; enforces hard limits.                                                                           |
| **Intent policy**     | Per-client rules on which skills may run and which require approval.                                                                 |

Throughout this document, "skill" means robot skill unless qualified.

---

## 4. Architecture Overview

### 4.1 Layers

```text
Clients    Python │ CLI │ Studio │ MCP host (e.g. Claude Code) │ built-in agent │ human
           ───────┴─────┴────────┴─────────────────────────────┴────────────────┴──────
                                  │ Skills API (contract 3)
System 2   Executive (physicalai.skills)
seam       skills · leases · WorldState · intent policy · event log
           ────────────────────────────────────────────────────────────────────────────
System 1   ControlLoop, 10–50 Hz; action sources per effector ── Inference API (contract 2) ──► ActionModel
                                  │ Robot command interface (contract 1)
System 0   ControlLoop, 50–200 Hz, or vendor/C++ up to 1 kHz; controller · safety gate · watchdog
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
3. **Three contracts cross partitions.** The robot command interface, the inference API,
   and the skills API. Each has bindings for in-process, same-host, cross-VM, and cross-host.
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
rt = RuntimeClient("grpc://robot.local:7400")
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
to the robot's only effector; no executive or network is involved.

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
    model="pick_policy",                         # model name from config
    claims={"arm", "gripper"},
    task="pick up the {object}",                 # language prompt, rendered from params
    success=GripperClosedOnObject(),
    timeout_s=20,
))
rt.skills.register(PolicySkill(
    name="place",
    description="Place the held object at the named location.",
    model="place_policy",
    claims={"arm", "gripper"},
    task="place it in the {location}",
    success=ObjectAt(),
    timeout_s=20,
))

with rt:
    for obj, loc in [("red block", "left bin"), ("blue block", "right bin")]:
        picked = rt.skills.submit("pick", {"object": obj}).result()
        if picked.status is not Status.SUCCEEDED:
            print("pick failed:", picked.evidence)
            continue
        rt.skills.submit("place", {"location": loc}).result()
```

Sequence for `submit("pick")`:

```text
intent policy check → lease {arm, gripper} → PolicySkill.start() builds PolicySource
→ S1 loop attaches the source to {arm, gripper} at the next tick
→ each tick: source.update() → safety gate → robot.send_action()
→ success check met → handle SUCCEEDED → source detached → lease released → episode saved
```

### 5.3 A reasoning model as planner

The script is replaced by a reasoning model. Nothing below the skills API changes.

```python
from physicalai.inference.reasoning import load_reasoning_model
from physicalai_agent import ToolLoopAgent      # reference agent; see §15.4

planner = load_reasoning_model("anthropic", model="claude-…")   # or any backend in §9.4
agent = ToolLoopAgent(planner, rt.skills.client(name="agent"))

with rt:
    print(agent.run("Put red blocks in the left bin and blue blocks in the right bin.").summary)
```

The agent receives the registered skills (`pick`, `place`) and queries (`observe`) as
tools. A failed skill returns its evidence as the tool result.

### 5.4 A coding assistant as planner (MCP)

```bash
# on the robot
physicalai run tabletop.yaml --serve-skills 0.0.0.0:7400 --mcp
```

```json
{ "mcpServers": { "robot": { "url": "http://robot.local:7400/mcp" } } }
```

The MCP tool list is generated from the skill registry (§15.3), so the built-in agent and
the coding assistant see the same tools. If the client disconnects, its lease heartbeats
stop, the lease expires, the skill is cancelled, and the robot holds.

### 5.5 System 2 commanding System 0

For tasks without a trained policy, a planner can move the arm through a setpoint skill.

```python
rt.skills.register(ServoSkill(
    name="servo",
    description="Move the end effector by a small delta in the base frame (m, rad).",
    claims={"arm"},
    mode=CommandMode.EE_POSE,
    max_step_m=0.01,
    max_speed_mps=0.05,
    hold_after_s=2.0,            # setpoint deadline; arm holds if no new call
))
```

```text
observe() → servo(dx=0.01) → observe() → servo(dx=0.01, dy=-0.005) → … → stop()
```

Each call writes one deadline-bounded setpoint to the System 0 command interface. The
controller interpolates, the gate clamps, and the watchdog holds when calls stop. This works
the same whether System 0 is a `ControlLoop` or a vendor controller.

### 5.6 Mixed backends and a remote model

```yaml
models:
  planner: {
      kind: reasoning,
      backend: openai-compatible,
      base_url: http://localhost:30000/v1,
      model: qwen3-vl-8b,
    } # SGLang
  judge:
    {
      kind: reasoning,
      backend: llama.cpp,
      path: models/judge.gguf,
      device: GPU.1,
    }
  pick_policy:
    {
      kind: action,
      framework: gr00t,
      placement: remote,
      endpoint: grpc://gpu-box:50051,
    }
  place_policy: { kind: action, framework: lerobot, placement: worker } # isolated env (PR #8)
  detector:
    { kind: action, framework: openvino, path: exports/owlv2, device: NPU }
```

```bash
# on gpu-box (proposed command)
physicalai serve --framework gr00t --model exports/gr00t-n17-ov --listen 0.0.0.0:50051
```

Skills refer to models by name; no Python code names a framework. Moving `pick_policy`
from `worker` to `remote` changes one config line.

### 5.7 Humanoid on three KVM guests

```yaml
robot:
  driver: unitree_g1
  effectors:
    whole_body: { modes: [WHOLE_BODY] }
    left_arm: { modes: [JOINT_POSITION, EE_POSE], via: whole_body }
    right_arm: { modes: [JOINT_POSITION, EE_POSE], via: whole_body }
    base: { modes: [TWIST], via: whole_body }
    head: { modes: [JOINT_POSITION] }

tiers:
  s0:
    {
      fps: 200,
      process: true,
      realtime: true,
      controller: { kind: policy, model: wbc },
    }
  s1: { fps: 30 }

models:
  wbc: { kind: action, framework: onnx, path: exports/wbc.onnx, device: CPU }
  bimanual: { kind: action, framework: gr00t, placement: worker, device: GPU.0 }
  planner:
    {
      kind: reasoning,
      backend: openvino-genai,
      path: models/qwen3-vl-ov,
      device: GPU.1,
    }

skills:
  manipulate: { type: policy, model: bimanual, claims: [left_arm, right_arm] }
  walk_to: { type: delegated, driver: nav2, claims: [base] }
  look_at: { type: setpoint, claims: [head], mode: JOINT_POSITION }

partitions:
  s0: { kind: vm, os: rt-linux, cores: [2, 3], devices: [ethercat0] }
  s1: { kind: vm, devices: [gpu0, cam_head, cam_wrist_l, cam_wrist_r] }
  s2: { kind: vm, devices: [gpu1], network: internet }

placement:
  s0: [tiers.s0, models.wbc]
  s1: [tiers.s1, executive, models.bimanual]
  s2: [models.planner, agent]

links:
  s1-s0: { contract: robot_command, binding: ivshmem }
  s2-s1: { contract: skills, binding: vsock }
```

```python
rt.skills.submit("look_at", {"target": "shelf"})               # lease {head}
rt.skills.submit("walk_to", {"target": "shelf"}).result()      # lease {base}, concurrent
rt.skills.submit("manipulate", {"task": "take the box"}).result()
```

```bash
physicalai run humanoid.yaml --partition s0    # in the RT guest
physicalai run humanoid.yaml --partition s1    # in the policy guest
physicalai run humanoid.yaml --partition s2    # in the reasoning guest
```

Skills on disjoint effectors run concurrently; leases ensure one writer per effector. The
System 2 guest has no access to the motor bus or cameras, only to the skills API. Without
the `partitions`, `placement`, and `links` sections, the same file runs on one host.

---

## 6. RobotRuntime and ControlLoop

### 6.1 Structure

```text
RobotRuntime               one per partition
 ├─ ControlLoop "s0"       if hosted here: child process, RT priority, owns the safety gate
 ├─ ControlLoop "s1"       if hosted here: current loop body; its Robot is S0 (local or remote)
 ├─ Executive              if hosted here (normally with S1); exposed as rt.skills
 ├─ model hosts            in-process adapters, isolate workers, remote clients
 └─ links                  contract bindings to other partitions
```

The current `RobotRuntime` loop body (tick timing, resilient robot and camera I/O,
callbacks, interpolation, return-to-initial-state, shutdown) moves into `ControlLoop`
without behavior changes. The existing constructor remains as the single-loop case.

### 6.2 API

```python
class RobotRuntime:
    def __init__(self, robot, action_source, fps, cameras=None, callbacks=(), interpolator=None): ...  # existing

    @classmethod
    def from_config(cls, config: str | Path | SystemConfig, *, partition: str | None = None) -> Self: ...

    def connect(self) -> None: ...       # existing; extended to links, model hosts, loops
    def run(self, *, duration_s: float | None = None) -> RunReason: ...   # blocking
    def start(self) -> None: ...         # non-blocking
    def stop(self) -> None: ...          # existing; stops all hosted loops at the next tick boundary
    def disconnect(self) -> None: ...    # existing

    @property
    def skills(self) -> SkillsAPI: ...   # local executive, or a client to a remote one
    @property
    def world(self) -> WorldState: ...
    @property
    def loops(self) -> Mapping[str, ControlLoop]: ...
    def model(self, name: str) -> ActionModel | ReasoningModel: ...
    def events(self) -> EventStream: ...
```

### 6.3 Building a partition

```python
@classmethod
def from_config(cls, config, *, partition=None):
    system = SystemConfig.load(config)
    part = system.partition(partition)                   # default: everything
    links = {name: open_binding(cfg) for name, cfg in system.links_touching(part)}
    models = {name: load_model(cfg) for name, cfg in system.models_in(part)}

    robot = system.build_robot(part)                     # hardware driver if attached here
    loops = {}

    if part.hosts("s0"):
        loops["s0"] = ControlLoop(
            name="s0", robot=robot, fps=system.tiers.s0.fps,
            controller=system.build_s0_controller(models),
            gate=SafetyGate.from_limits(system.robot.limits),
            command_server=CommandServer(links.get("s1-s0")),
            process=system.tiers.s0.process, realtime=system.tiers.s0.realtime,
        )
        robot = loops["s0"].as_robot()                   # S0 seen as a Robot by S1
    elif "s1-s0" in links:
        robot = RemoteRobot(links["s1-s0"])

    if part.hosts("s1"):
        loops["s1"] = ControlLoop(
            name="s1", robot=robot, fps=system.tiers.s1.fps,
            cameras=system.build_cameras(part),
            gate=SafetyGate.from_limits(system.robot.limits),
        )

    if part.hosts("executive"):
        skills = Executive(loops, models, WorldState(), system.intent_policy)
        skills.register_all(system.build_skills(models))
    else:
        skills = SkillsClient(links["skills"]) if "skills" in links else None

    return cls._from_parts(loops=loops, skills=skills, models=models, links=links)
```

### 6.4 Tick

Changes from the current loop are marked `NEW`.

```python
class ControlLoop:
    def _tick(self, step: int, *, new_cycle: bool) -> None:
        self._apply_pending_attachments()                        # NEW: attach/detach only at tick boundary
        obs, frames = self._read_observation_resilient()         # existing
        self._world.publish_proprio(obs)                         # NEW

        action: dict[Effector, Command] = {}
        for effectors, source in self._attached.items():         # NEW: one source per lease
            if new_cycle:
                raw = source.update(obs, frames, step)           # existing ActionSource protocol
                self._last[effectors] = normalize(raw, effectors, self._robot)
            action.update(self._interpolate(effectors, step))    # existing interpolator, per effector

        action.update(self._command_server.fresh_setpoints(now()))   # NEW: deadline-bounded setpoints
        action = self._hold_unclaimed(action)                    # NEW: idle effectors hold

        action = self._bus.on_action_ready(action=action, step=step)  # existing callbacks
        action = self._gate.enforce(action, obs)                 # NEW: final slot; may raise SafetyStop
        self._send_resilient(action)                             # existing
        self._bus.on_action_sent(action=action, step=step)
        self._emit(TickEvent(...))                               # existing

    def attach(self, source: ActionSource, *, effectors: frozenset[Effector], lease: Lease) -> None: ...  # NEW
    def detach(self, lease: Lease) -> None: ...                                                         # NEW
    def as_robot(self) -> Robot: ...    # NEW: LoopRobot (§7.3)
```

`attach`/`detach` take effect at a tick boundary, so switching skills does not restart
`run()` and does not leave a gap in commands.

### 6.5 What one RobotRuntime can host

| Tier                                     | Hosted?   | Mechanism                                                   |
| ---------------------------------------- | --------- | ----------------------------------------------------------- |
| S0, light (interpolation, smoothing, PD) | yes       | nested ticks in the S1 loop (current behavior)              |
| S0, heavy or real-time (learned WBC)     | yes       | `ControlLoop` in a child process, RT priority, pinned cores |
| S0 in vendor stack, RTOS, or another VM  | connected | robot command interface                                     |
| S1                                       | yes       | `ControlLoop`                                               |
| S2 executive and built-in agent          | yes       | threads/asyncio; event-driven                               |
| S2 in another VM or the cloud            | connected | skills API                                                  |

Threads in one Python process share the GIL; hosted System 0 controllers above ~100 Hz
default to `process: true`.

---

## 7. Actions, Effectors, and Command Interfaces

### 7.1 Effector-keyed actions

Actions are currently a flat `np.ndarray`. Multi-effector robots need independently owned
parts, so actions become mappings. A flat array remains valid for single-effector sources.

```python
Effector = str

class CommandMode(Enum):
    JOINT_POSITION = "joint_position"
    JOINT_VELOCITY = "joint_velocity"
    EE_POSE = "ee_pose"            # position + quaternion in a named frame
    TWIST = "twist"
    WHOLE_BODY = "whole_body"      # controller-specific target bundle
    GRIPPER = "gripper"

@dataclass(frozen=True)
class Command:
    mode: CommandMode
    values: np.ndarray
    frame: str | None = None
    deadline: float | None = None  # absolute time on the shared clock (§13)

Action = Mapping[Effector, Command]

def normalize(raw: np.ndarray | Action, effectors: frozenset[Effector], robot: Robot) -> Action:
    if isinstance(raw, np.ndarray):
        (only,) = effectors        # a flat array must target exactly one effector
        return {only: Command(robot.default_mode(only), raw)}
    return raw
```

Existing `PolicySource` and `TeleopSource` continue to return `np.ndarray`.

### 7.2 Robot protocol extension

```python
class Robot(Protocol):                          # existing methods unchanged
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def get_observation(self) -> RobotObservation: ...
    def send_action(self, action: np.ndarray | Action, *, goal_time: float = 0.1) -> None: ...
    def is_connected(self) -> bool: ...
    def command_interfaces(self) -> Mapping[Effector, CommandInterface]: ...   # NEW, optional

@dataclass(frozen=True)
class CommandInterface:
    modes: frozenset[CommandMode]
    dims: Mapping[CommandMode, int]
    joint_names: tuple[str, ...] = ()
    frames: tuple[str, ...] = ()
    via: Effector | None = None                 # e.g. left_arm via whole_body
```

Robots that do not implement `command_interfaces()` expose one effector with
`JOINT_POSITION`.

### 7.3 Tiers as Robot implementations

```python
class LoopRobot:                                # a local S0 ControlLoop, seen by S1
    def command_interfaces(self): return self._loop.controller.accepted_interfaces()
    def get_observation(self):    return self._loop.latest_state()
    def send_action(self, action, *, goal_time=0.1):
        self._loop.command_server.write(action, deadline=now() + goal_time)

class RemoteRobot:                              # S0 in another partition
    def command_interfaces(self): return self._link.describe()
    def get_observation(self):    return self._link.latest_state()
    def send_action(self, action, *, goal_time=0.1):
        self._link.write_setpoints(action, deadline=now() + goal_time)
```

The System 1 loop uses the same code whether its `Robot` is hardware, `LoopRobot`, or
`RemoteRobot`.

---

## 8. Robot Skills

Package: `physicalai.skills`. It has no LLM dependency. Plugins contribute skills through
the `physicalai.skills` entry-point group.

### 8.1 Skill protocol

```python
class Skill(Protocol):
    name: str
    description: str                        # shown in Studio, CLI help, and LLM tool specs
    params: type[Params]                    # dataclass → JSON schema → tool / form / CLI flags
    claims: frozenset[Effector]             # leased for the run
    tier: Literal["s0", "s1", "external"]   # where its commands go

    def can_start(self, params: Params, world: WorldState) -> Precondition: ...
    def start(self, params: Params, ctx: SkillContext) -> SkillRun: ...

@dataclass
class SkillContext:
    lease: Lease
    world: WorldState
    loops: Mapping[str, ControlLoop]
    models: ModelHost
    events: EventSink
    clock: Clock
```

Three implementation types:

| Type          | Commands the robot by                               | Examples                                    |
| ------------- | --------------------------------------------------- | ------------------------------------------- |
| **Attached**  | attaching an `ActionSource` to an S0 or S1 loop     | `PolicySkill` (VLA, ACT), teleop            |
| **Setpoint**  | writing deadline-bounded setpoints per call         | `ServoSkill`, `look_at`, gripper open/close |
| **Delegated** | driving an external controller and reporting status | `Nav2Skill`, vendor locomotion, MoveIt      |

### 8.2 PolicySkill (attached)

`PolicySkill` is a generic "run this policy" skill. Pick, place, and similar tasks are
configured instances rather than subclasses.

```python
class PolicySkill:
    def __init__(self, name, description, model, claims, task, success=None, timeout_s=30,
                 params=TaskParams, execution=None, tier="s1"): ...

    def start(self, params, ctx):
        model = ctx.models.action(self.model)                 # any framework, any placement
        source = PolicySource(                                # existing class
            model,
            execution=self.execution or ctx.models.default_execution(model),
            task=self.task.format(**asdict(params)),
        )
        ctx.loops[self.tier].attach(source, effectors=self.claims, lease=ctx.lease)
        return AttachedRun(
            source, success=self.success, timeout_s=self.timeout_s,
            on_end=lambda: ctx.loops[self.tier].detach(ctx.lease),
        )
```

### 8.3 ServoSkill (setpoint)

```python
@dataclass
class ServoParams(Params):
    dx: float = 0.0; dy: float = 0.0; dz: float = 0.0
    droll: float = 0.0; dpitch: float = 0.0; dyaw: float = 0.0

class ServoSkill:
    params = ServoParams
    tier = "s0"

    def start(self, p, ctx):
        target = compose(ctx.world.get("arm.ee_pose").value, clamp_delta(p, self.max_step_m))
        ctx.loops["s0"].command_server.write(
            {"arm": Command(CommandMode.EE_POSE, target, frame="base",
                            deadline=ctx.clock.now() + self.hold_after_s)},
            lease=ctx.lease,
        )
        return SetpointRun(done=lambda: pose_error(ctx.world.get("arm.ee_pose").value, target) < self.tolerance)
```

### 8.4 Nav2Skill (delegated)

```python
class Nav2Skill:
    claims = frozenset({"base"})
    tier = "external"

    def start(self, p, ctx):
        goal = self._nav2.send_goal(pose_from(p, ctx.world))     # ROS 2 action client (optional extra)
        return DelegatedRun(
            status=lambda: map_status(goal.status()),
            feedback=lambda: {"distance_remaining": goal.feedback().distance_remaining},
            cancel=goal.cancel,
        )
```

Delegated skills also hold a lease, so no other skill writes to `base` while Nav2 controls it.

### 8.5 Leases

```python
@dataclass(frozen=True)
class Lease:
    id: LeaseId
    effectors: frozenset[Effector]
    holder: ClientId          # "agent", "mcp:laptop-42", "studio:user", "script"
    priority: Priority
    expires_at: float

class LeaseManager:
    def acquire(self, effectors, holder, priority, ttl_s) -> Lease:
        with self._lock:
            conflicts = self._overlapping(effectors)            # includes parent/child effectors
            if any(c.priority >= priority for c in conflicts):
                raise LeaseBusy(conflicts)
            for c in conflicts:
                self._revoke(c, reason="preempted")             # cancels that skill run
            lease = Lease(new_id(), effectors, holder, priority, now() + ttl_s)
            self._held[lease.id] = lease
            return lease

    def heartbeat(self, holder: ClientId) -> None: ...          # extends all leases of the holder
    def _reap(self) -> None: ...                                # ~10 Hz; revokes expired leases → robot holds
```

`whole_body` is the parent of `left_arm`, `right_arm`, and `base`. Leasing a parent conflicts
with leasing any child, which lets a coupled whole-body policy and independent arm or base
skills coexist without an arbiter.

### 8.6 SkillHandle

```python
class Status(Enum):
    PENDING, RUNNING, SUCCEEDED, FAILED, CANCELLED = range(5)

class SkillHandle:
    id: RunId
    skill: str
    params: Params

    def status(self) -> Status: ...
    def feedback(self) -> Mapping[str, Any]: ...       # progress, queue depth, starvation, distance
    def update(self, params: Mapping[str, Any]) -> None: ...   # e.g. new task prompt, at a tick boundary
    def cancel(self, reason: str = "") -> None: ...
    def result(self, timeout_s: float | None = None) -> SkillResult: ...
    def updates(self) -> Iterator[SkillUpdate]: ...

@dataclass(frozen=True)
class SkillResult:
    status: Status
    evidence: Mapping[str, Any]
    episode: EpisodeRef
    duration_s: float
```

### 8.7 Submission and supervision

```python
class Executive:                                   # exposed as rt.skills
    def submit(self, skill: str, params: Mapping | None = None, *,
               client: ClientId = "local", priority: Priority = Priority.NORMAL) -> SkillHandle:
        s = self._registry[skill]
        p = s.params.parse(params or {})
        self._policy.authorize(client, s, p)                     # intent policy (§12.3)
        pre = s.can_start(p, self._world)
        if not pre.ok:
            return SkillHandle.rejected(s, p, pre.reason)
        lease = self._leases.acquire(s.claims, client, priority, ttl_s=self._ttl(client))
        handle = SkillHandle.new(s, p, lease)
        self._recorder.begin_episode(handle)
        run = s.start(p, SkillContext(lease, self._world, self._loops, self._models, ...))
        self._supervisor.watch(handle, run)
        return handle

class Supervisor:
    def _step(self) -> None:
        for handle, run in self._active():
            status = run.poll()
            if status is Status.RUNNING and run.success:
                est = run.success.evaluate(self._world)          # may be asynchronous (VLM)
                if est.met and est.confidence >= run.success.threshold:
                    status = Status.SUCCEEDED
            if status is Status.RUNNING and run.timed_out():
                status = Status.FAILED
            if status.terminal:
                run.end()                                         # detach or stop delegated controller
                self._leases.release(handle.lease)
                self._recorder.end_episode(handle, status, run.evidence())
                handle._finish(status, run.evidence())
```

### 8.8 WorldState

`WorldState` holds the latest value of each fact that skills, success checks, and clients
read. It is not a message bus and does not store history (recording does).

| Key             | Writer                   | Example value               |
| --------------- | ------------------------ | --------------------------- |
| `arm.ee_pose`   | S1 loop (proprioception) | pose in `base` frame        |
| `gripper.state` | S1 loop                  | width 0.031 m, effort 4.2 N |
| `detections`    | detector, 5 Hz           | `[mug @ (0.42, 0.10)]`      |
| `scene.caption` | VLM captioner, 0.2 Hz    | "mug left of the plate"     |
| `nav.pose`      | Nav2                     | base pose in `map` frame    |

```python
class WorldState:
    def get(self, key: str) -> StateEntry | None: ...
    def snapshot(self, keys: Iterable[str] | None = None) -> Mapping[str, StateEntry]: ...
    def publish(self, key: str, value: Any, *, source: str, stamp: float) -> None: ...   # one writer per key
    def subscribe(self, key: str, fn: Callable[[StateEntry], None]) -> Subscription: ...

@dataclass(frozen=True)
class StateEntry:
    value: Any
    stamp: float      # shared clock; readers decide staleness
    source: str
```

Perceivers (detectors, captioners, localization) run at their own rates and publish into
`WorldState`, using the model loaders in §9.

### 8.9 Success checks

```python
class SuccessCheck(Protocol):
    threshold: float
    def evaluate(self, world: WorldState) -> SuccessEstimate: ...

@dataclass(frozen=True)
class SuccessEstimate:
    met: bool
    confidence: float
    evidence: Mapping[str, Any] = field(default_factory=dict)
```

Example checks used in §5.2:

```python
class GripperClosedOnObject:                    # cheap: gripper closed but not empty
    threshold = 0.9
    def __init__(self, min_width_m=0.005, min_effort_n=2.0): ...
    def evaluate(self, world):
        g = world.get("gripper.state").value
        holding = g.width > self.min_width_m and g.effort > self.min_effort_n
        return SuccessEstimate(holding, 1.0 if holding else 0.0, {"width": g.width, "effort": g.effort})

class ObjectAt:                                 # detector-based: object inside a named region
    threshold = 0.7
    def evaluate(self, world):
        det = find(world.get("detections").value, self.object)
        region = world.get(f"regions.{self.location}").value
        inside = det is not None and region.contains(det.center)
        return SuccessEstimate(inside, det.score if inside else 0.0, {"detection": det})

class VLMJudge:                                 # expensive: asks a reasoning model
    def __init__(self, model: ReasoningModel, question: str, camera: str, threshold=0.8): ...
    def evaluate(self, world):
        frame = world.get(f"camera.{self.camera}").value
        r = self.model.complete([user(self.question, images=[frame])], response_format=YesNoConfidence)
        return SuccessEstimate(r.yes, r.confidence, {"reason": r.reason})
```

`ObjectAt` receives `object` and `location` from the skill parameters at start.

---

## 9. Models and Backends

### 9.1 Model interfaces

Planner, navigator, manipulator, and judge are roles that skills bind to models. Models
expose one of two interfaces (a third is optional):

```python
class ActionModel(Protocol):          # = existing InferenceModel
    spec: ModelSpec
    def predict_action_chunk(self, observation: Mapping[str, np.ndarray]) -> np.ndarray: ...
    def reset(self) -> None: ...
    def close(self) -> None: ...

class ReasoningModel(Protocol):
    def complete(self, messages: Sequence[Message], *, tools: Sequence[ToolSpec] = (),
                 response_format: type | None = None, max_tokens: int | None = None) -> Completion: ...

class WorldModel(Protocol):           # optional, later
    def rollout(self, obs: Observation, actions: Sequence[np.ndarray]) -> PredictedTrajectory: ...
```

### 9.2 ModelSpec and binding

Frameworks describe model inputs differently (LeRobot features, GR00T `modality.json`, the
PhysicalAI `Observation`). Each model declares a `ModelSpec`; the robot config declares
what it provides; compatibility is checked at startup.

```python
@dataclass(frozen=True)
class Feature:
    name: str                       # "images.wrist", "state.arm", "task"
    shape: tuple[int, ...]
    dtype: str
    frame: str | None = None
    units: str | None = None
    rate_hz: float | None = None

@dataclass(frozen=True)
class ModelSpec:
    inputs: Mapping[str, Feature]
    outputs: Mapping[str, Feature]
    command: Mapping[Effector, CommandMode]
    chunk_size: int = 1
    language: bool = False

def bind(spec: ModelSpec, provided: ProvidedFeatures, mapping: FeatureMap) -> Binding:
    missing = [f for f in spec.inputs if mapping.source_for(f) not in provided]
    if missing:
        raise BindError(f"model needs {missing}; robot provides {sorted(provided)}")
    for name, feat in spec.inputs.items():
        check_compatible(provided[mapping.source_for(name)], feat)   # shape, dtype, units, frame
    return Binding(to_model=input_transform(spec, mapping), to_action=output_transform(spec, mapping))
```

```yaml
skills:
  pick:
    model: pick_policy
    features: # only needed when names differ
      images.ego_view: camera.head
      images.wrist: camera.wrist_r
      state.right_arm: robot.right_arm.joint_position
      action.right_arm: right_arm
```

Plugins convert native formats: `spec_from_lerobot(config)`, `spec_from_modality_json(path, embodiment)`.

### 9.3 Framework adapters

The existing entry-point group remains the extension point; adapters gain two methods.

```toml
[project.entry-points."physicalai.inference.adapters"]   # existing group
gr00t = "physicalai_gr00t_policy_plugin.runtime:Gr00tAdapter"
```

```python
class RuntimeAdapter(ABC):                       # existing
    def load(self, model_path: Path) -> None: ...
    def predict(self, inputs: dict[str, Any]) -> dict[str, np.ndarray]: ...
    def spec(self) -> ModelSpec: ...             # NEW
    def environment(self) -> Environment | None: ...   # NEW: isolate env when deps conflict (PR #8)
```

### 9.4 Reasoning backends

Most local and hosted LLM servers implement the OpenAI-compatible chat API, so one client
covers SGLang, vLLM, the llama.cpp server, Ollama, and several hosted providers. Native
clients are added where the native API offers features the compatible API lacks.

```python
REASONING_BACKENDS = {                 # entry-point group: physicalai.reasoning.backends
    "openai-compatible": OpenAICompatible,
    "anthropic": AnthropicClient,
    "gemini": GeminiClient,
    "openvino-genai": OpenVINOGenAI,   # in-process; CPU/GPU/NPU
    "llama.cpp": LlamaCppInProcess,    # in-process GGUF
}

planner = load_reasoning_model("openai-compatible", base_url="http://gpu-box:30000/v1", model="qwen3-vl-8b")
```

### 9.5 Placement

Placement is independent of framework. An isolate worker is treated as an inference server
on the local host, so the worker and `physicalai serve` share adapter code and protocol.

```python
def load_action_model(cfg: ModelConfig) -> ActionModel:
    match cfg.placement:
        case "inprocess":
            return InferenceModel(cfg.path, adapter=cfg.framework, device=cfg.device)
        case "worker":
            prepared = isolate.prepare(adapter_environment(cfg.framework))     # PR #8
            endpoint = isolate.start(prepared, entrypoint=SERVE_ENTRYPOINT,
                                     args=["--model", cfg.path, "--listen", f"unix://{sock(cfg)}"])
            return RemoteActionModel(endpoint, shm=True)
        case "remote":
            return RemoteActionModel(cfg.endpoint)
```

| Placement   | Started by                    | Binding                     | Use                                       |
| ----------- | ----------------------------- | --------------------------- | ----------------------------------------- |
| `inprocess` | the runtime                   | direct call                 | OpenVINO/ONNX exports, small models       |
| `worker`    | the runtime via `isolate`     | Unix socket + shared memory | conflicting dependencies, crash isolation |
| `remote`    | external (`physicalai serve`) | gRPC over TCP               | models that do not fit locally            |

`RemoteActionModel` and `physicalai serve` are not implemented; they follow
`policy_server_design.md`.

### 9.6 Remote latency

A remote System 1 model is viable when one action chunk covers the round trip:

```text
chunk_horizon_s > network_rtt_s + inference_s + margin_s
16 actions @ 30 Hz = 0.53 s > 0.02 + 0.15 + 0.10    viable
 1 action  @ 30 Hz = 0.03 s < 0.02 + 0.15           keep local
```

`AsyncExecution` and `RTCExecution` already request the next chunk while the current one
plays. When a chunk is late, the source holds the last safe action and emits a `Starved`
event, surfaced as `handle.feedback()["starved"]`.

### 9.7 Existing policy servers

Clients for existing policy servers present them as `ActionModel`s without re-hosting:

```yaml
pick_policy:
  {
    kind: action,
    server: openpi,
    endpoint: ws://gpu-box:8000,
    spec: specs/pi05_droid.yaml,
  }
# also: server: lerobot-async (gRPC), server: gr00t-zmq
```

### 9.8 Devices on a shared host

```yaml
models:
  planner:
    { kind: reasoning, backend: openvino-genai, device: GPU.1, priority: low }
  pick:
    {
      kind: action,
      framework: gr00t,
      placement: worker,
      device: GPU.0,
      priority: high,
    }
  detector: { kind: action, framework: openvino, device: NPU }
  wbc: { kind: action, framework: onnx, device: CPU, tier: s0 }
```

- System 0 uses dedicated cores and RT priority and does not share an accelerator with System 2.
- System 1 uses its own accelerator when available; when shared with System 2, System 1 has
  priority. GPU preemption is limited, so System 1 jitter under System 2 load must be measured.
- `device` maps to OpenVINO device names, CUDA device IDs, or llama.cpp offload settings,
  depending on the backend.

---

## 10. Contracts and Bindings

### 10.1 Robot command interface (System 1/2 ↔ System 0)

Fixed-layout messages so that a real-time guest or vendor controller can implement the
System 0 side in C.

```c
/* pai_command.h, version 1 */
typedef struct {
    uint32_t magic;          /* 'PAIC' */
    uint16_t version;
    uint16_t effector_id;    /* index into the announced effector table */
    uint64_t seq;
    int64_t  stamp_ns;       /* shared clock */
    int64_t  deadline_ns;    /* after this, S0 applies the effector's on_stale policy */
    uint64_t lease_id;       /* must match the active lease for the effector */
    uint16_t mode;           /* JOINT_POSITION | JOINT_VELOCITY | EE_POSE | TWIST | WHOLE_BODY | GRIPPER */
    uint16_t dim;
    uint32_t frame_id;
    float    values[64];
} pai_setpoint_t;

typedef struct {
    uint32_t magic;          /* 'PAIS' */
    uint16_t version;
    uint16_t effector_id;
    uint64_t seq;
    int64_t  stamp_ns;
    uint32_t status;         /* OK | HOLDING | SAFE_STOP | FAULT | GATE_CLAMPED */
    uint16_t dim;
    float    position[64], velocity[64], effort[64];
} pai_state_t;
```

Contract semantics:

- Setpoints past `deadline_ns` are ignored. An effector without a valid setpoint applies its
  `on_stale` policy: `hold`, `decay`, or `safe_stop`.
- Setpoints with a `lease_id` that does not match the active lease are rejected.
- The System 0 side owns the authoritative safety gate and reports clamping in `status`.

### 10.2 Inference API (loops ↔ action models)

Extends the gRPC design in `policy_server_design.md`.

```proto
service ActionModelService {
  rpc Describe(Empty) returns (ModelSpecProto);
  rpc Session(stream InferRequest) returns (stream InferReply);
  rpc Reset(ResetRequest) returns (Empty);
  rpc Health(Empty) returns (HealthReply);
}
message InferRequest { uint64 seq = 1; int64 stamp_ns = 2; map<string, Tensor> inputs = 3; string task = 4; }
message InferReply   { uint64 seq = 1; int64 stamp_ns = 2; Tensor action_chunk = 3; double inference_s = 4; }
message Tensor       { repeated int64 shape = 1; string dtype = 2; oneof data { bytes inline = 3; ShmRef shm = 4; } }
```

### 10.3 Skills API (clients ↔ executive)

```proto
service Skills {
  rpc List(Empty) returns (SkillList);                         // names, descriptions, schemas, claims
  rpc Submit(SubmitRequest) returns (stream SkillUpdate);      // until terminal
  rpc Update(UpdateRequest) returns (Empty);
  rpc Cancel(CancelRequest) returns (Empty);
  rpc Heartbeat(stream ClientHeartbeat) returns (stream LeaseStatus);
  rpc Observe(ObserveRequest) returns (WorldSnapshot);         // state + downsampled images
  rpc Events(EventsFilter) returns (stream Event);
  rpc Stop(StopRequest) returns (Empty);                       // cancel all, hold
}
```

### 10.4 Bindings

| Contract                | Same process | Same OS                             | Across VMs (one host)            | Across hosts            |
| ----------------------- | ------------ | ----------------------------------- | -------------------------------- | ----------------------- |
| Robot command interface | direct call  | shared-memory ring                  | ivshmem ring or vsock            | UDP; optional Zenoh/DDS |
| Inference API           | direct call  | Unix socket + shared-memory tensors | virtio-net gRPC (+ ivshmem)      | gRPC/TCP                |
| Skills API              | direct call  | Unix socket                         | vsock or virtio-net (gRPC / MCP) | gRPC / MCP over TCP     |

`binding: auto` selects the lowest-overhead binding both partitions support.

---

## 11. Deployment Configurations

All configurations share one schema. Only `partitions`, `placement`, `links`, and model
`placement` / `endpoint` / `device` differ.

| Configuration                  | Key settings                                                                                  |
| ------------------------------ | --------------------------------------------------------------------------------------------- |
| One process                    | `tiers.s1` only (S0 = interpolator); all models `inprocess` or cloud reasoning                |
| One host, several accelerators | `tiers.s0.process: true`, `realtime: true`, `cpu_affinity`; per-model `device` (§9.8)         |
| Robot + GPU host + cloud       | `placement: remote` for large action models; cloud `ReasoningModel`; `links.*.binding: tcp`   |
| KVM guests on one SoC          | `partitions.*.kind: vm`; `ivshmem` for S1↔S0; `vsock` for S2↔S1; `clock.sync: ptp_kvm` (§5.7) |
| Vendor System 0                | `partitions.s0.kind: external`; vendor implements `pai_command.h`; `binding: udp`             |

```yaml
# vendor System 0 example
partitions:
  s0: { kind: external }
  s1: { kind: host }
links:
  s1-s0: { contract: robot_command, binding: udp, address: 192.168.123.10:9100 }
```

---

## 12. Safety

### 12.1 Layers

| Layer                            | Location                               | Enforces                                             | Client can bypass |
| -------------------------------- | -------------------------------------- | ---------------------------------------------------- | ----------------- |
| Hardware e-stop, firmware limits | robot                                  | power, torque, temperature                           | no                |
| Safety gate (authoritative)      | lowest software tier we run, or vendor | joint, velocity, workspace, force limits; `on_stale` | no                |
| Safety gate (secondary)          | S1 `ControlLoop`                       | same limits, earlier                                 | no                |
| Leases and deadlines             | executive, S0 command server           | one writer per effector; stale commands hold         | no                |
| Intent policy                    | executive                              | allowed skills per client, approvals, rate limits    | per profile       |
| Partition boundary               | hypervisor                             | S2 guest has no device access                        | no                |

```python
class SafetyGate:
    def enforce(self, action: Action, obs: RobotObservation) -> Action:
        out = {}
        for effector, cmd in action.items():
            lim = self._limits[effector]
            if lim.violates_hard(cmd, obs):
                raise SafetyStop(effector, lim.explain(cmd, obs))   # loop runs safe shutdown
            safe = lim.clamp(cmd, obs)
            if safe is not cmd:
                self._emit(GateClamped(effector, cmd, safe))
            out[effector] = safe
        return out
```

The gate is a dedicated slot after the callback chain, not a callback.

### 12.2 Failure handling

| Failure                          | Detection               | Response                                          |
| -------------------------------- | ----------------------- | ------------------------------------------------- |
| Client disconnects               | missed heartbeats       | lease expires; skill cancelled; effectors hold    |
| S2 partition crashes             | skills link down        | as above; S1 and S0 continue                      |
| Remote model late or unavailable | action queue starvation | source holds; `starved` feedback; skill may fail  |
| S1 stops sending                 | S0 deadline watchdog    | per-effector `on_stale` policy                    |
| S0 fault                         | S0 status `FAULT`       | S1 loop shuts down; executive fails active skills |
| Clock offset exceeds bound       | link offset checks      | refuse to start, or hold                          |

### 12.3 Intent policy

```yaml
intent_policy:
  profiles:
    safeguarded:
      allow: [pick, place, observe, servo]
      servo: { max_step_m: 0.01, max_speed_mps: 0.05 }
      require_approval: [start_recording]
      rate_limit: { servo: 5/s }
    unrestricted:
      allow: ["*"]
  clients:
    "mcp:*": safeguarded
    "agent": safeguarded
    "studio:*": unrestricted
    "script": unrestricted
```

The `unrestricted` profile removes intent restrictions only. Safety gates, leases, and
deadlines apply in every profile.

---

## 13. Time

- Partitions share a clock: chrony or PTP across hosts, `ptp_kvm` across guests.
- Observations, setpoints, inference messages, and events carry `stamp_ns`.
- Setpoint deadlines are absolute times.
- `WorldState` entries carry their production time; readers apply their own staleness limits.
- Links measure clock offset at startup and periodically; an offset above the configured
  bound is treated as a fault.

---

## 14. Recording

Each skill run is recorded as one episode. The event log is the common source for
recording, Studio's live view, MCP status, and agent run logs.

```python
@dataclass(frozen=True)
class EpisodeMeta:
    run_id: RunId
    skill: str
    params: Mapping[str, Any]
    client: ClientId
    parent: RunId | None                   # agent task that started the run
    status: Status
    evidence: Mapping[str, Any]
    models: Mapping[str, ModelProvenance]
    effectors: frozenset[Effector]
    started_ns: int
    ended_ns: int

@dataclass(frozen=True)
class ModelProvenance:
    name: str
    framework: str
    artifact_sha256: str | None            # None for hosted APIs
    endpoint: str | None
    placement: str
    device: str | None
```

```text
episode/
  meta.json       EpisodeMeta
  data/           LeRobot-format observations and actions at loop rate
  events.jsonl    skill updates, gate clamps, starvation, lease changes
  agent.jsonl     reasoning-model messages and tool calls, when an agent was involved
```

Failed and cancelled runs are recorded the same way as successful ones. For hosted reasoning
models, the transcript and provenance are the only record of the decisions made.

---

## 15. Clients

All clients use the skills API: direct calls in-process, gRPC otherwise.

### 15.1 Python

```python
rt = RuntimeClient("grpc://robot.local:7400", name="notebook")
for s in rt.skills.list():
    print(s.name, s.claims, s.params_schema)
h = rt.skills.submit("pick", {"object": "mug"})
for u in h.updates():
    print(u.status, u.feedback)
```

### 15.2 CLI

```bash
physicalai skills list     --connect robot.local:7400
physicalai skills run pick --object mug --connect robot.local:7400
physicalai skills cancel <run-id>
physicalai events --follow --connect robot.local:7400
physicalai stop            --connect robot.local:7400
```

### 15.3 MCP adapter

Generated from the registry; adding a skill adds a tool.

```python
def build_mcp_server(skills: SkillsClient, profile: str) -> MCPServer:
    server = MCPServer("robot")
    for s in skills.list(profile=profile):
        server.add_tool(s.name, s.description, s.params_schema,
                        lambda args, s=s: skills.submit(s.name, args).result().to_tool_result())
    server.add_tool("observe", "Robot state and camera images.", {}, lambda _: skills.observe().to_tool_result())
    server.add_tool("stop", "Cancel all skills and hold.", {}, lambda _: skills.stop())
    server.on_session(lambda session: skills.heartbeat_while(session.alive))
    return server
```

### 15.4 Reference agent

A minimal tool-calling loop. It could live in the `physicalai-agent` package rather than the runtime.

```python
class ToolLoopAgent:
    def __init__(self, model: ReasoningModel, skills: SkillsClient, max_steps: int = 50): ...

    def run(self, task: str) -> AgentResult:
        tools = [s.as_tool() for s in self.skills.list()] + [OBSERVE_TOOL, DONE_TOOL]
        snap = self.skills.observe(images=True, max_side=448)
        messages = [system(ROBOT_PROMPT), user(task, images=snap.images, state=snap.summary())]
        for _ in range(self.max_steps):
            reply = self.model.complete(messages, tools=tools)
            messages.append(reply.as_message())
            if not reply.tool_calls:
                return AgentResult(reply.text, messages)
            for call in reply.tool_calls:
                if call.name == "done":
                    return AgentResult(call.arguments["summary"], messages)
                out = (self.skills.observe(images=True).to_tool_result() if call.name == "observe"
                       else self.skills.submit(call.name, call.arguments, client="agent").result().to_tool_result())
                messages.append(tool_result(call.id, out))
        self.skills.stop()
        return AgentResult("step limit reached", messages)
```

### 15.5 Periodic supervision (System 1 / System 2)

```python
h = rt.skills.submit("manipulate", {"task": "put red blocks in the left bin"})
while h.status() is Status.RUNNING:
    time.sleep(2.0)
    v = judge.complete([user("Is the robot stuck, done, or progressing?", images=rt.skills.observe().images)],
                       response_format=Verdict)
    if v.label == "done":
        h.cancel("judge: done")
    elif v.label == "stuck":
        h.update({"task": "push the blocks apart, then continue"})
```

---

## 16. Relationship to Other Work

### 16.1 Superseded autonomy-layer doc

| Previous term                      | This document                                                | Change                                     |
| ---------------------------------- | ------------------------------------------------------------ | ------------------------------------------ |
| `physicalai.autonomy`              | `physicalai.skills`                                          | named after its contents                   |
| `Skill`                            | `Skill`                                                      | adds `claims`, `tier`, typed `params`      |
| `PickSkill`, `PlaceSkill`          | `PolicySkill(name="pick", …)`                                | configured instances instead of subclasses |
| `SkillExecution`                   | `SkillHandle`                                                | adds `update()`                            |
| `SkillRegistry`, `SkillDispatcher` | executive (`rt.skills`), `LeaseManager`                      | concurrent skills on disjoint effectors    |
| `Goal`                             | skill name + params                                          | success check moved onto the skill         |
| `WorldView`                        | `WorldState`                                                 | renamed                                    |
| `CompositeSource`, arbiter         | leases + per-effector attachment                             | no merge logic                             |
| one `RobotRuntime`, one loop       | one `RobotRuntime` per partition, one `ControlLoop` per tier | supports S0 and partitions                 |

Retained: the two-level deliberative/reactive split, the handle lifecycle (from ROS 2
actions), success as a confidence estimate, the final safety gate, and one episode per run.

### 16.2 Physical AI Agent proposal (deck)

The deck describes developer tooling: `physicalai-agent init`, agent skills, Studio and
Runtime MCP servers, guarded profiles, an `agent run` loop, run logs, and memory. This
design does not depend on it. If adopted, the deck's components map as follows:

| Deck component                 | Maps to                                        | Required change                          |
| ------------------------------ | ---------------------------------------------- | ---------------------------------------- |
| `mcp/runtime.py` tools         | generated MCP adapter (§15.3)                  | tools derived from the skill registry    |
| `RobotSession`, `PolicyRunner` | `SkillsClient`                                 | connect to a (possibly remote) executive |
| `ActionGuard`                  | intent policy (§12.3) and safety gates (§12.1) | split permissions from limits            |
| `start_policy`, `set_task`     | `submit("<policy skill>")`, `handle.update()`  | naming only                              |
| `move_joints_delta`            | `ServoSkill` (§8.3)                            | passes through lease and gate            |
| `agent/loop.py`, `ModelClient` | `ToolLoopAgent`, `ReasoningModel`              | shared with judges and perceivers        |
| `agent/runs/`, `memory.md`     | event log, `agent.jsonl` (§14)                 | read from the event stream               |
| `limits.mode: unrestricted`    | `unrestricted` profile                         | intent only; gates stay active           |

### 16.3 Runtime and Studio PRs

| Work                                 | Role in this design                                               |
| ------------------------------------ | ----------------------------------------------------------------- |
| Runtime PR #8 (`physicalai.isolate`) | `worker` placement; worker entrypoint becomes an inference server |
| Studio PR #10 (LeRobot engine)       | LeRobot adapter; adds `spec()` from LeRobot config                |
| Studio PR #23 (GR00T + OpenVINO)     | GR00T adapter; pipe protocol replaced by the inference API        |
| Studio PR #25 (isolate validation)   | validation baseline for worker placement                          |
| `policy_server_design.md`            | basis for the inference API and `remote` placement                |

### 16.4 Prior art

| Source                                    | Adopted                                    |
| ----------------------------------------- | ------------------------------------------ |
| Hierarchical VLA systems                  | S2 / S1 / S0 split                         |
| ros2_control                              | controllers claim command interfaces       |
| ROS 2 actions                             | goal → handle → feedback → result → cancel |
| Boston Dynamics SDK                       | leases with heartbeats                     |
| SayCan and LLM tool-calling agents        | skills as tools; failures as tool results  |
| Mixed-criticality hypervisors (KVM, ACRN) | partitioning S0 from S2                    |
| LeRobot async inference, openpi servers   | remote chunked inference                   |

---

## 17. Changes to Existing Code

| Area                              | Change                                                               | Breaking                           |
| --------------------------------- | -------------------------------------------------------------------- | ---------------------------------- |
| `RobotRuntime`                    | loop body moves to `ControlLoop`; constructor retained               | no, for constructor users          |
| Actions                           | `np.ndarray \| Action`; arrays normalized to one effector            | callbacks that assume `np.ndarray` |
| `Robot` protocol                  | optional `command_interfaces()`; `send_action` accepts `Action`      | no                                 |
| `RuntimeCallback.on_action_ready` | receives an `Action` mapping                                         | yes (small migration)              |
| Safety                            | dedicated `SafetyGate` slot                                          | no                                 |
| Inference                         | `ModelSpec`, `RuntimeAdapter.spec()`, placement, `RemoteActionModel` | no                                 |
| `physicalai.isolate` (PR #8)      | worker entrypoint serves the inference API                           | no                                 |
| LeRobot / GR00T plugins           | adopt inference API over Unix socket + shared memory                 | plugin-internal                    |
| New packages                      | `physicalai.skills`, `physicalai.inference.reasoning`                | no                                 |
| CLI                               | `--partition`, `up`, `serve`, `skills`, `events`, `stop`             | no                                 |

---

## 18. Implementation Phases

| Phase | Scope                                                                                | Demo                                            | Exit criterion                                          |
| ----- | ------------------------------------------------------------------------------------ | ----------------------------------------------- | ------------------------------------------------------- |
| 1     | Extract `ControlLoop` from `RobotRuntime`                                            | existing examples                               | existing runtime tests pass unchanged                   |
| 2     | Effector-keyed actions, `attach`/`detach`, deadlines, `SafetyGate`                   | SO-101 teleop and policy                        | callbacks migrated; gate fault-injection tests          |
| 3     | Inference API; worker as local server; placement; `ModelSpec` (first-party, LeRobot) | one policy in-process, worker, and remote       | identical actions across placements; S1 jitter measured |
| 4     | Executive, leases, handles, recording; scripted client                               | tabletop sort with policies from two frameworks | one episode per run; lease expiry holds robot           |
| 5     | Reasoning backends, skills API, MCP adapter, reference agent                         | cloud model and MCP host drive the same robot   | identical tool lists; disconnect holds within TTL       |
| 6     | `ServoSkill`; command interface schema; C reference server                           | S2 commands S0 directly                         | watchdog holds within deadline on every binding         |
| 7     | Hosted S0 loop (process, RT); KVM bindings (ivshmem, vsock)                          | humanoid or mobile manipulator on three guests  | S0 jitter within budget under S2 load                   |
| 8     | Delegated navigation; world model as a tool                                          | navigate → pick → place                         | —                                                       |

Phases 1–4 are useful independently of the later phases.

---

## 19. Risks and Open Questions

| Item                                     | Notes                                                                                                               |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Cross-framework spec binding             | Largest effort; most integration failures expected here.                                                            |
| Remote System 1 latency                  | Requires chunked models; single-step policies stay local.                                                           |
| GPU sharing between S1 and S2            | Limited preemption; requires measurement.                                                                           |
| `Action` mapping migration for callbacks | Small but breaking; schedule in phase 2.                                                                            |
| Command interface payload size           | 64 floats may be insufficient for some whole-body targets; review with a humanoid vendor.                           |
| Cross-host transport for robot commands  | UDP for version 1; Zenoh/DDS bridges on demand.                                                                     |
| Executive placement                      | Default is the S1 partition; placing it in S2 is not supported.                                                     |
| Robot skill vs agent skill terminology   | Mitigated by qualified terms in docs and separate locations (`physicalai.skills` package vs repo `skills/` folder). |
| Scope                                    | Each phase is justified by its demo before the next starts.                                                         |

---

## 20. Decisions

```text
tiers           S0 control, S1 reactive policy, S2 deliberation; rate bands, not processes
main runtime    one RobotRuntime per partition, hosting ControlLoops, the executive, and local models
loops           ControlLoop = current loop body; heavy S0 in a child process; >200 Hz stays native/vendor
tier interface  each tier is a Robot to the tier above (LoopRobot, RemoteRobot)
actions         effector-keyed mappings; flat arrays for single-effector sources
skills          physicalai.skills: Skill, SkillHandle, leases, WorldState, intent policy, event log; rt.skills
skill types     attached (action source), setpoint (deadline-bounded), delegated (external controller)
concurrency     effector leases with parent/child effectors; no arbiter
models          ActionModel and ReasoningModel (WorldModel later); roles are bindings
interop         ModelSpec with startup binding; plugins convert native formats
placement       inprocess, worker (isolate; local server), remote; independent of framework
contracts       robot command interface (C-implementable), inference API (gRPC), skills API (gRPC, MCP)
safety          authoritative gate at the lowest tier; lease expiry; setpoint deadlines; intent policy per client
data            one labeled episode per skill run, with model provenance
terminology     "robot skill" (physicalai.skills) vs "agent skill" (SKILL.md)
```
