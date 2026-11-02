# Tiered Robot Runtime: System 0 / 1 / 2 on Any Hardware Layout

**Status:** Proposal — for design review.
**Supersedes:** [`composite_robot_architecture.md`](./composite_robot_architecture.md) (agentic autonomy layer).
**Amends:** [`robot_runtime_architecture.md`](./robot_runtime_architecture.md) and today's `physicalai.runtime`.
**Related:** [`policy_server_design.md`](./policy_server_design.md), Runtime PR #8 (`physicalai.isolate`),
Studio PRs #10 / #23 / #25 (framework plugins), the _Physical AI Agent_ proposal deck.

All code in this document is **pseudo-code** describing proposed APIs unless it is
explicitly marked as existing. Names are proposals and open to review.

---

## Quick Navigation

- [1. What This Is](#1-what-this-is)
- [2. Why We Need It](#2-why-we-need-it)
- [3. Concepts on One Page](#3-concepts-on-one-page)
- [4. The Big Picture](#4-the-big-picture)
- [5. Walkthrough: From One Policy to a Humanoid](#5-walkthrough-from-one-policy-to-a-humanoid)
- [6. RobotRuntime and ControlLoop](#6-robotruntime-and-controlloop)
- [7. Actions, Effectors, and Command Interfaces](#7-actions-effectors-and-command-interfaces)
- [8. The Executive: Behaviors, Handles, and Leases](#8-the-executive-behaviors-handles-and-leases)
- [9. Models and Backends](#9-models-and-backends)
- [10. The Three Contracts and Their Bindings](#10-the-three-contracts-and-their-bindings)
- [11. Deployment Topologies](#11-deployment-topologies)
- [12. Safety](#12-safety)
- [13. Time and Clocks](#13-time-and-clocks)
- [14. Recording and the Data Flywheel](#14-recording-and-the-data-flywheel)
- [15. Clients: Python, CLI, Studio, MCP, Built-in Agent](#15-clients-python-cli-studio-mcp-built-in-agent)
- [16. With and Without the Physical AI Agent Proposal](#16-with-and-without-the-physical-ai-agent-proposal)
- [17. What Changes in Today's Code](#17-what-changes-in-todays-code)
- [18. Build Order](#18-build-order)
- [19. Risks and Open Questions](#19-risks-and-open-questions)
- [20. Decision Summary](#20-decision-summary)
- [Appendix A. Prior Art](#appendix-a-prior-art)

---

## 1. What This Is

Today PhysicalAI runs **one policy on one robot in one loop**:

```python
# existing API
runtime = RobotRuntime(robot=so101, action_source=PolicySource(model), fps=30)
runtime.run()
```

This document proposes how the same runtime grows into a **tiered robot system**:

| Tier         | Job                                                  | Typical rate            | Example                                                     |
| ------------ | ---------------------------------------------------- | ----------------------- | ----------------------------------------------------------- |
| **System 2** | Deliberate: understand the task, plan, check results | event-driven, ~0.1–5 Hz | frontier LLM/VLM (cloud), local VLM via SGLang / llama.cpp  |
| **System 1** | React: turn observations into motion                 | 10–50 Hz                | VLA (GR00T, π0), ACT, diffusion policy, navigation policy   |
| **System 0** | Control: track setpoints, balance, enforce limits    | 200 Hz – 1 kHz          | whole-body controller, impedance / IK, vendor motor control |

…where **each tier's model can come from any framework or backend** (OpenVINO, ONNX,
LeRobot, Isaac-GR00T, an OpenAI-compatible server, a cloud API), and **each tier can run
anywhere**: the same process, another process, another accelerator, another VM on the
same box (KVM), or another machine.

The core idea in one sentence:

> One `RobotRuntime` per partition runs the loops placed there; an **Executive** runs
> **Behaviors** on leased effectors; everything crosses partition boundaries through
> **three small contracts**; and moving between "one laptop" and "robot + GPU box +
> cloud" or "three KVM guests" is a **config change, not a code change**.

---

## 2. Why We Need It

### 2.1 Where robotics is going

Current robot stacks converge on a three-tier hierarchy (System 2 / System 1 / System 0):
a slow reasoning model over a fast visuomotor policy over a very fast controller. Public
examples include hierarchical VLA systems (Figure Helix, NVIDIA GR00T, Gemini Robotics
ER + VLA) and the classic deliberator / sequencer / controller architectures. Customers
we talk to run these tiers **on separate compute** — often as **separate KVM guests on
one SoC** — because the models do not fit in one memory, because the tiers have
incompatible OS and real-time needs, and because isolation is the cheapest safety story.

### 2.2 Where we are today

| Capability                                               | Today                                                   |
| -------------------------------------------------------- | ------------------------------------------------------- |
| One fixed-rate loop, one `ActionSource`, one `Robot`     | yes (`RobotRuntime`)                                    |
| Cheap inner rate (interpolation between policy actions)  | yes (`interpolator`, `fps * N` ticks)                   |
| Policy inference sync / async / RTC                      | yes (`SyncExecution`, `AsyncExecution`, `RTCExecution`) |
| Framework models via plugins + isolated environments     | in progress (PR #8, Studio #10 / #23 / #25)             |
| Remote inference                                         | designed (`policy_server_design.md`), not shipped       |
| Several decision-makers at different rates               | no                                                      |
| A language goal turned into robot behavior               | no                                                      |
| Tiers on different processes / VMs / machines            | no                                                      |
| A non-bypassable safety layer for agent-driven actuation | no                                                      |

### 2.3 Requirements

| #   | Requirement                                                                                                                                 |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| R1  | Run System 0, System 1, and System 2 on a robot, in any combination (S2 may drive S1 **or** S0 directly).                                   |
| R2  | Each tier's model can come from **any backend**: cloud API, llama.cpp, SGLang, vLLM, OpenVINO, ONNX, LeRobot, GR00T, …                      |
| R3  | Tiers can run on **different machines, VMs, or accelerators** — and also all on one host. Same code.                                        |
| R4  | Many **clients** can drive the robot: Python script, CLI, Studio UI, an external coding assistant over MCP, a built-in agent loop, a human. |
| R5  | **Safety cannot be bypassed** by any client, model, or partition failure.                                                                   |
| R6  | Every autonomous action produces **labeled training data**.                                                                                 |
| R7  | The **simple case stays simple**: today's one-policy script and config keep working unchanged.                                              |
| R8  | Framework-specific code stays in **plugins**; the core never imports a framework.                                                           |

### 2.4 Non-goals

- A hypervisor, VM manager, or device-passthrough setup. That is the platform layer
  (KVM, ACRN, Intel ECI, vendor BSPs). We run _inside_ partitions.
- Hard real-time control loops in Python. Anything faster than ~200 Hz lives in firmware,
  C/C++, or a vendor stack behind our `Robot` protocol.
- An agent or prompt framework. The built-in agent is a small reference client (§15.5).
- A general-purpose robotics middleware. We define three narrow contracts and bridge to
  ROS 2 / Zenoh / DDS where useful.
- Training. That remains `physicalai-train` and Studio.

---

## 3. Concepts on One Page

| Term                  | Meaning                                                                                                                              |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| **Tier**              | System 0, 1, or 2 — a responsibility and a rate band, not a process.                                                                 |
| **Partition**         | Where code runs: a process, a container, a VM, or a machine.                                                                         |
| **`RobotRuntime`**    | The main runtime. **One per partition.** Hosts the loops, executive, and models assigned to that partition and connects to the rest. |
| **`ControlLoop`**     | A fixed-rate loop (today's `RobotRuntime` loop body). One per hosted tier (S0 and/or S1).                                            |
| **Effector**          | A named, independently commandable part: `arm`, `left_arm`, `gripper`, `base`, `head`, `whole_body`.                                 |
| **Command interface** | What an effector accepts: joint position, joint velocity, end-effector pose, twist, whole-body target.                               |
| **`ActionSource`**    | Existing protocol: produces an action each tick (policy, teleop, servo setpoints).                                                   |
| **Executive**         | Runs behaviors, grants leases, keeps `WorldState`, writes the event log. The single authority that mutates what loops are doing.     |
| **Behavior**          | A named capability (`pick`, `place`, `servo`, `navigate_to`) with typed params. Starting one yields a handle.                        |
| **`BehaviorHandle`**  | Supervision of one running behavior: `status`, `feedback`, `cancel`, `result`.                                                       |
| **Lease**             | Exclusive, time-limited write access to a set of effectors. Kept alive by heartbeats.                                                |
| **`ActionModel`**     | Observation → action chunk (VLA, ACT, WBC policy). Today's `InferenceModel`.                                                         |
| **`ReasoningModel`**  | Messages + images (+ tools) → text / tool calls (LLM, VLM).                                                                          |
| **`ModelSpec`**       | What a model consumes and produces: named features, shapes, frames, units, rates, command mode.                                      |
| **Placement**         | Where a model runs relative to its caller: `inprocess`, `worker` (isolated env on same host), `remote`.                              |
| **Binding**           | How a contract is carried between two partitions: direct call, shared memory, Unix socket, ivshmem, vsock, TCP.                      |
| **Safety gate**       | The guaranteed-last transform before hardware. Owns hard limits. Lives at the lowest software tier.                                  |
| **Intent policy**     | Which client may invoke which behavior, under which approval rules. Lives in the executive.                                          |

---

## 4. The Big Picture

### 4.1 Layers

```text
  Clients     Python script │ physicalai CLI │ Studio │ MCP (Claude Code, …) │ built-in agent │ human
              ──────────────┴────────────────┴────────┴──────────────────────┴────────────────┴──────
                                      │  Executive API  (contract #3)
  System 2    Executive               behaviors · leases · WorldState · intent policy · event log
  seam        (physicalai.autonomy)
              ─────────────────────────────────────────────────────────────────────────────────────
  System 1    ControlLoop @ 10–50 Hz  ActionSources attached per effector ── Inference API (contract #2) ──► ActionModel
                                      │  Robot command interface (contract #1)
  System 0    ControlLoop @ 50–200 Hz or vendor / C++ @ 1 kHz      WBC · IK · impedance · SAFETY GATE · watchdog
              ─────────────────────────────────────────────────────────────────────────────────────
  Hardware    motors · sensors · cameras
```

### 4.2 Six rules

1. **Loops are placed by rate; models are placed by memory.** S0 sits next to the motors,
   the S1 loop sits on the robot or local edge, S2 can be anywhere. A heavy model can live
   on a GPU box or in the cloud while the loop that calls it stays local.
2. **Every tier is a `Robot` to the tier above.** The S1 loop drives S0 through the same
   `Robot` protocol it uses for a bare arm today. When S0 is a vendor stack, the vendor
   driver _is_ that `Robot`.
3. **Three contracts, many bindings.** Only three things ever cross a partition boundary:
   the **Robot command interface**, the **Inference API**, and the **Executive API**. Each
   has bindings for in-process, same-host, cross-VM, and cross-host.
4. **The executive is the product; agents are clients.** The built-in agent, a coding
   assistant over MCP, Studio, and a script all drive the robot through the same API.
5. **Every command is leased and time-bounded.** Leases expire without heartbeats;
   setpoints carry deadlines; the lowest tier holds or safe-stops when either lapses.
6. **Framework × spec × placement are independent.** Which framework a model comes from,
   what data it needs, and where it runs are three separate config axes.

### 4.3 What the main runtime looks like to a user

```python
# 1. today — unchanged
RobotRuntime(robot=so101, action_source=PolicySource(model), fps=30).run()

# 2. everything from config, all tiers in this process / host
RobotRuntime.from_config("robot.yaml").run()

# 3. same file, split across partitions (VMs or machines)
RobotRuntime.from_config("robot.yaml", partition="s1").run()
```

```bash
physicalai run robot.yaml                      # single partition (default)
physicalai run robot.yaml --partition s0       # RT guest
physicalai run robot.yaml --partition s1       # policy guest
physicalai run robot.yaml --partition s2       # reasoning guest
physicalai up  robot.yaml                      # dev: launch every partition locally
```

---

## 5. Walkthrough: From One Policy to a Humanoid

Each step adds exactly one idea. Code is pseudo-code unless marked existing.

### 5.1 Step 0 — one policy, one robot (existing)

```python
from physicalai.inference import InferenceModel
from physicalai.robot.so101 import SO101
from physicalai.runtime import RobotRuntime, PolicySource, AsyncExecution

model = InferenceModel("./exports/act_policy")
runtime = RobotRuntime(
    robot=SO101(port="/dev/ttyACM0"),
    action_source=PolicySource(model, execution=AsyncExecution()),
    fps=30,
)
runtime.run(duration_s=60)
```

Nothing in this document changes this code path. Internally it becomes one `ControlLoop`
with the action source attached to the robot's single effector, no executive, no network.

### 5.2 Step 1 — two behaviors, scripted (no LLM)

Two manipulation policies, run one after the other by a script. This introduces the
**Executive**, **Behaviors**, and **handles**.

```python
from physicalai.runtime import RobotRuntime
from physicalai.autonomy import PolicyBehavior

runtime = RobotRuntime.from_config("tabletop.yaml")   # robot, cameras, models
ex = runtime.executive

ex.register(PolicyBehavior(
    name="pick",
    description="Pick up the named object from the table.",
    model="pick_policy",                 # model name from config
    claims={"arm", "gripper"},
    task="pick up the {object}",         # rendered from params into the language prompt
    success=GripperClosedOnObject(),
    timeout_s=20,
))
ex.register(PolicyBehavior(
    name="place",
    description="Place the held object at the named location.",
    model="place_policy",
    claims={"arm", "gripper"},
    task="place it on the {location}",
    success=ObjectAt(),
    timeout_s=20,
))

with runtime:                                       # connect, start loops, start executive
    for obj, loc in [("red block", "left bin"), ("blue block", "right bin")]:
        h = ex.submit("pick", {"object": obj})
        if h.result().status is not Status.SUCCEEDED:
            print("pick failed:", h.result().evidence)
            continue
        ex.submit("place", {"location": loc}).result()
```

What happened under the hood:

```text
submit("pick")  → intent policy ok → lease {arm, gripper} granted
                → PolicyBehavior.start() builds PolicySource(model=pick_policy, task=...)
                → S1 ControlLoop.attach(source, effectors={arm, gripper})   (at tick boundary)
                → each tick: source.update() → gate → robot.send_action()
                → SuccessCheck met → handle SUCCEEDED → detach → lease released → episode recorded
```

### 5.3 Step 2 — a frontier model plans (built-in agent)

Replace the script with a reasoning model. Nothing below the executive changes.

```python
from physicalai.autonomy.agents import ToolLoopAgent
from physicalai.inference.reasoning import load_reasoning_model

planner = load_reasoning_model("anthropic", model="claude-…")          # or any backend, §9.4
agent = ToolLoopAgent(planner, runtime.executive.client(name="agent"))

with runtime:
    result = agent.run("Sort the blocks: red ones to the left bin, blue ones to the right bin.")
    print(result.summary)
```

The agent sees the registered behaviors as tools (`pick`, `place`), plus queries
(`observe`, `describe_scene`), and decides the sequence itself. A failed `pick` comes
back as a tool result with evidence; replanning is the model's next step.

### 5.4 Step 3 — the planner is a coding assistant over MCP

Same robot, same behaviors; System 2 is now Claude Code (or any MCP host) on a laptop.

```bash
# on the robot
physicalai run tabletop.yaml --serve-executive 0.0.0.0:7400 --mcp
```

```json
// .mcp.json on the developer's laptop
{ "mcpServers": { "robot": { "url": "http://robot.local:7400/mcp" } } }
```

The MCP tool list is **generated from the behavior registry** (§15.4) — `pick`, `place`,
`observe`, `stop`, … — so the built-in agent and the coding assistant always see the same
vocabulary. If the laptop sleeps, its lease heartbeat stops, the lease expires, the
behavior is cancelled, and the robot holds (§12.2).

### 5.5 Step 4 — System 2 drives System 0 directly

There is no policy for "nudge the cup onto the tape mark". The planner moves the arm
itself, slowly, through a **setpoint behavior**. It does not get a raw motor channel.

```python
ex.register(ServoBehavior(
    name="servo",
    description="Move the end effector by a small delta (meters, radians) in the base frame.",
    claims={"arm"},
    mode=CommandMode.EE_POSE,
    max_step_m=0.01,                 # per call
    max_speed_mps=0.05,
    hold_after_s=2.0,                # deadline: hold if no new call
))
```

```text
agent: observe() → servo(dx=0.01) → observe() → servo(dx=0.01, dy=-0.005) → … → stop()
```

Each `servo` call writes one setpoint (with a deadline) into the S0 command interface; the
S0 controller interpolates, the safety gate clamps, and the watchdog holds the arm when
calls stop coming. The same behavior works whether S0 is our `ControlLoop` or a vendor
controller in a real-time VM.

### 5.6 Step 5 — mixed backends in one system

Models come from different frameworks and run in different places. Only config changes.

```yaml
models:
  planner: {kind: reasoning, backend: openai-compatible,
            base_url: http://localhost:30000/v1, model: qwen3-vl-8b}        # SGLang
  judge:   {kind: reasoning, backend: llama.cpp, path: models/judge.gguf, device: GPU.1}
  pick_policy:
           {kind: action, framework: gr00t, path: exports/gr00t-n17-ov,
            placement: worker, device: GPU.0}                                # isolated env (PR #8)
  place_policy:
           {kind: action, framework: lerobot, path: exports/act-place,
            placement: worker}                                               # LeRobot plugin
  detector:{kind: action, framework: openvino, path: exports/owlv2, device: NPU}
```

```python
runtime = RobotRuntime.from_config("tabletop.yaml")
# behaviors reference models by name; nothing in Python mentions a framework
```

### 5.7 Step 6 — the VLA lives on a GPU box

The 3B VLA does not fit on the robot. Serve it from a GPU box; the S1 loop stays local.

```bash
# on gpu-box
physicalai serve --model exports/gr00t-n17-ov --framework gr00t --listen 0.0.0.0:50051
```

```yaml
# robot config: one line changes
pick_policy:
  {
    kind: action,
    framework: gr00t,
    placement: remote,
    endpoint: grpc://gpu-box:50051,
  }
```

The S1 loop uses `AsyncExecution` / `RTCExecution` so the round trip hides behind the
current action chunk (§9.6). If chunks arrive late, the queue drains, the source holds, and
the behavior reports `feedback["starved"] = True`.

### 5.8 Step 7 — a humanoid: S0 whole-body control + S1 arms + navigation

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
    loop: { fps: 200, process: true, realtime: true, cpu_affinity: [2, 3] }
    controller: { kind: policy, model: wbc } # learned WBC: base twist + arm targets → joints
  s1:
    loop: { fps: 30 }

models:
  wbc:
    {
      kind: action,
      framework: onnx,
      path: exports/wbc.onnx,
      device: CPU,
      tier: s0,
    }
  bimanual:
    {
      kind: action,
      framework: gr00t,
      placement: remote,
      endpoint: grpc://gpu-box:50051,
    }

behaviors:
  manipulate: { type: policy, model: bimanual, claims: [left_arm, right_arm] }
  walk_to: { type: delegated, driver: nav2, claims: [base] }
  look_at: { type: setpoint, claims: [head], mode: JOINT_POSITION }
```

```python
# "look at the shelf while walking to it, then take the box"
ex.submit("look_at", {"target": "shelf"})          # lease {head}
ex.submit("walk_to", {"target": "shelf"}).result() # lease {base}, concurrently
ex.submit("manipulate", {"task": "take the box"}).result()   # lease {left_arm, right_arm}
```

Three behaviors run concurrently on disjoint effectors. No arbiter, no merge priorities:
leases guarantee one writer per effector (§8.5). The S0 WBC consumes whatever targets the
S1 loop and delegated navigation write, and the gate sits at its output.

### 5.9 Step 8 — the same humanoid on three KVM guests

```yaml
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
  s2-s1: { contract: executive, binding: vsock }
```

```bash
physicalai run humanoid.yaml --partition s0    # inside the RT guest
physicalai run humanoid.yaml --partition s1    # inside the policy guest
physicalai run humanoid.yaml --partition s2    # inside the reasoning guest
```

The S2 guest has **no motor bus and no cameras** — only the Executive API. A misbehaving
model or a shell-enabled coding assistant in that guest can reach actuators only through
leased behaviors and the S0 gate. Step 7's Python code is unchanged.

---

## 6. RobotRuntime and ControlLoop

### 6.1 Responsibilities

```text
RobotRuntime              one per partition; what users construct and run
 ├─ ControlLoop "s0"      if S0 is hosted here (child process, RT priority, owns the safety gate)
 ├─ ControlLoop "s1"      if S1 is hosted here (today's loop body); its Robot is S0 (local or stub)
 ├─ Executive             if hosted here (normally in the S1 partition)
 ├─ model hosts           in-process adapters, isolate workers, remote clients
 └─ links                 contract bindings to other partitions
```

Today's `RobotRuntime` class body — tick timing, resilient robot and camera I/O, callbacks,
interpolation, return-to-initial-state, shutdown — moves into `ControlLoop` with no
behavior change. `RobotRuntime` keeps its constructor for the single-loop case.

### 6.2 API

```python
class RobotRuntime:
    # single-loop convenience — existing signature, unchanged
    def __init__(
        self,
        robot: Robot,
        action_source: ActionSource,
        fps: float,
        cameras: Mapping[str, Camera] | None = None,
        callbacks: Sequence[Any] = (),
        interpolator: ActionInterpolator | None = None,
    ) -> None: ...

    @classmethod
    def from_config(cls, config: str | Path | SystemConfig, *, partition: str | None = None) -> Self:
        """Build the slice of the system assigned to `partition` (default: everything)."""

    # lifecycle
    def connect(self) -> None: ...          # robot/cameras, links, model hosts, loops (not started)
    def run(self, *, duration_s: float | None = None) -> RunReason: ...   # blocks until stop
    def start(self) -> None: ...            # non-blocking variant: loops + executive in background
    def stop(self) -> None: ...             # thread-safe; every loop ends at its next tick boundary
    def disconnect(self) -> None: ...

    # what this partition hosts
    @property
    def loops(self) -> Mapping[str, ControlLoop]: ...
    @property
    def executive(self) -> Executive: ...   # local instance or a client to a remote one
    def model(self, name: str) -> ActionModel | ReasoningModel: ...
    def events(self) -> EventStream: ...    # merged event stream of everything hosted here
```

### 6.3 Building a partition from config

```python
@classmethod
def from_config(cls, config, *, partition=None):
    system = SystemConfig.load(config)                     # validated, single source of truth
    part = system.partition(partition)                     # what runs here; default "all"

    links = {name: open_binding(link) for name, link in system.links_touching(part)}

    models = {
        name: load_model(spec, placement=spec.placement, device=spec.device)
        for name, spec in system.models_in(part)
    }

    robot = system.build_robot(part)                       # real driver if hardware is here
    loops = {}
    if part.hosts("s0"):
        loops["s0"] = ControlLoop(
            name="s0",
            robot=robot,
            fps=system.tiers.s0.fps,
            controller=system.build_s0_controller(models),  # e.g. WBC policy, IK, passthrough
            gate=SafetyGate.from_limits(system.robot.limits),
            command_server=CommandServer(links.get("s1-s0")),  # accepts S1/S2 setpoints
            process=system.tiers.s0.process,
            realtime=system.tiers.s0.realtime,
        )
        robot = loops["s0"].as_robot()                     # S0 exposed as a Robot to S1
    elif part.hosts("s1"):
        robot = RemoteRobot(links["s1-s0"]) if "s1-s0" in links else robot

    if part.hosts("s1"):
        loops["s1"] = ControlLoop(
            name="s1",
            robot=robot,                                   # S0, local or remote, or bare hardware
            fps=system.tiers.s1.fps,
            cameras=system.build_cameras(part),
            gate=SafetyGate.from_limits(system.robot.limits),   # defense in depth (§12)
        )

    executive = None
    if part.hosts("executive"):
        executive = Executive(loops=loops, models=models, world=WorldState(), policy=system.intent_policy)
        executive.register_all(system.build_behaviors(models))
    elif "executive" in links:
        executive = ExecutiveClient(links["executive"])

    return cls._from_parts(loops=loops, executive=executive, models=models, links=links)
```

### 6.4 The ControlLoop tick

The loop body is today's, with four additions marked `NEW`: effector-keyed actions, sources
attached per effector, setpoint deadlines, and a dedicated final gate.

```python
class ControlLoop:
    def _tick(self, step: int, *, new_cycle: bool) -> None:
        self._apply_pending_attachments()                       # NEW: attach/detach only here

        obs, frames = self._read_observation_resilient()        # existing resilient I/O
        self._world.publish_proprio(obs)                        # NEW: feeds WorldState

        action: dict[Effector, Command] = {}
        for effectors, source in self._attached.items():        # NEW: one source per lease
            if new_cycle:
                cmd = source.update(obs, frames, step)          # existing protocol
                self._last[effectors] = normalize(cmd, effectors, self._robot)  # ndarray → mapping
            action.update(self._interpolate(effectors, step))   # existing interpolator, per effector

        action.update(self._command_server.fresh_setpoints(now()))  # NEW: S1/S2 setpoints with deadlines
        action = self._fill_unclaimed(action)                   # NEW: hold pose for idle effectors

        action = self._bus.on_action_ready(action=action, step=step)   # existing callbacks
        action = self._gate.enforce(action, obs)                # NEW: guaranteed-last; may raise SafetyStop
        self._send_resilient(action)                            # existing
        self._bus.on_action_sent(action=action, step=step)
        self._emit(TickEvent(...))                              # existing event stream
```

```python
    # NEW — called by the executive, applied at the next tick boundary
    def attach(self, source: ActionSource, *, effectors: frozenset[Effector], lease: Lease) -> None: ...
    def detach(self, lease: Lease) -> None: ...

    def as_robot(self) -> Robot:
        """Expose this loop as a Robot to the tier above (S0 → S1)."""
        return LoopRobot(self)     # get_observation() = latest state; send_action() = write setpoint
```

`attach` / `detach` replace both "per-run action source" and "hot swap" from earlier
drafts: a new behavior attaches at a tick boundary; no `run()` restart, no gap.

### 6.5 What can live inside one RobotRuntime

| Tier                                           | Inside one `RobotRuntime`? | How                                                                |
| ---------------------------------------------- | -------------------------- | ------------------------------------------------------------------ |
| S0, cheap (interpolation, smoothing, PD)       | yes                        | nested ticks in the S1 loop, as today                              |
| S0, heavy or real-time (learned WBC, ≥ 200 Hz) | yes                        | `ControlLoop` in a child process with RT priority and pinned cores |
| S0 in a vendor stack, RTOS, or another VM      | connected                  | through the Robot command interface                                |
| S1                                             | yes                        | `ControlLoop`                                                      |
| S2 executive + built-in agent                  | yes                        | threads / asyncio; event-driven, not a fixed-rate loop             |
| S2 on another VM or in the cloud               | connected                  | through the Executive API                                          |

Python threads share the GIL. A heavy S0 loop in a thread next to S1 will jitter, which is
why `process: true` is the default for hosted S0 controllers above ~100 Hz.

---

## 7. Actions, Effectors, and Command Interfaces

### 7.1 Effector-keyed actions

Today an action is a flat `np.ndarray` end to end. Mobile manipulators and humanoids need
several independently owned parts. We make the change **now**, while the runtime API is
young, and keep the flat vector as the one-effector special case.

```python
Effector = str          # "arm", "gripper", "left_arm", "base", "head", "whole_body"

class CommandMode(Enum):
    JOINT_POSITION = "joint_position"
    JOINT_VELOCITY = "joint_velocity"
    EE_POSE        = "ee_pose"          # position + quaternion in a named frame
    TWIST          = "twist"            # base / EE velocity
    WHOLE_BODY     = "whole_body"       # controller-specific target bundle
    GRIPPER        = "gripper"          # width / effort

@dataclass(frozen=True)
class Command:
    mode: CommandMode
    values: np.ndarray
    frame: str | None = None            # for EE_POSE / TWIST
    deadline: float | None = None       # absolute time on the shared clock (§13)

Action = Mapping[Effector, Command]
```

```python
def normalize(raw: np.ndarray | Action, effectors: frozenset[Effector], robot: Robot) -> Action:
    if isinstance(raw, np.ndarray):                    # legacy / single-effector sources
        (only,) = effectors                            # flat vector requires exactly one effector
        return {only: Command(robot.default_mode(only), raw)}
    return raw
```

Existing `PolicySource` and `TeleopSource` keep returning `np.ndarray`; they are attached
to exactly one effector and normalized at the seam.

### 7.2 The Robot protocol grows one method

```python
class Robot(Protocol):                       # existing methods unchanged
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def get_observation(self) -> RobotObservation: ...
    def send_action(self, action: np.ndarray | Action, *, goal_time: float = 0.1) -> None: ...
    def is_connected(self) -> bool: ...

    # NEW (optional; default = one effector "robot" with JOINT_POSITION)
    def command_interfaces(self) -> Mapping[Effector, CommandInterface]: ...
```

```python
@dataclass(frozen=True)
class CommandInterface:
    modes: frozenset[CommandMode]
    dims: Mapping[CommandMode, int]            # e.g. JOINT_POSITION: 7, EE_POSE: 7
    joint_names: tuple[str, ...] = ()
    frames: tuple[str, ...] = ()               # valid frames for EE_POSE / TWIST
    via: Effector | None = None                # "left_arm via whole_body": routed through S0
```

### 7.3 Every tier is a Robot to the tier above

```python
class LoopRobot:                                # S0 ControlLoop seen from S1
    def __init__(self, loop: ControlLoop): self._loop = loop

    def command_interfaces(self):               # higher-level modes than the hardware
        return self._loop.controller.accepted_interfaces()   # e.g. EE_POSE, TWIST, WHOLE_BODY

    def get_observation(self):
        return self._loop.latest_state()        # lock-free latest value

    def send_action(self, action, *, goal_time=0.1):
        self._loop.command_server.write(action, deadline=now() + goal_time)
```

```python
class RemoteRobot:                              # S0 in another partition (VM, RTOS, host)
    def __init__(self, link: CommandLink): self._link = link

    def command_interfaces(self): return self._link.describe()
    def get_observation(self):    return self._link.latest_state()
    def send_action(self, action, *, goal_time=0.1):
        self._link.write_setpoints(action, deadline=now() + goal_time)
```

The S1 loop never knows which of `SO101`, `LoopRobot`, or `RemoteRobot` it is driving.

---

## 8. The Executive: Behaviors, Handles, and Leases

Package: `physicalai.autonomy`. It has **no LLM dependency**; agents live in the optional
`physicalai.autonomy.agents` extra.

### 8.1 Behavior

```python
class Behavior(Protocol):
    name: str
    description: str                       # shown to humans, Studio, and LLM tools
    params: type[Params]                   # dataclass → JSON schema → tool / form / CLI flags
    claims: frozenset[Effector]            # leased for the duration of a run
    tier: Literal["s0", "s1", "external"]  # where its actions go

    def can_start(self, params: Params, world: WorldState) -> Precondition: ...
    def start(self, params: Params, ctx: BehaviorContext) -> BehaviorRun: ...
```

```python
@dataclass
class BehaviorContext:
    lease: Lease
    world: WorldState
    loops: Mapping[str, ControlLoop]       # to attach sources
    models: ModelHost                      # to resolve model names
    events: EventSink                      # feedback + recording annotations
    clock: Clock
```

Three implementation kinds cover everything we know of:

| Kind          | Actuates by                                             | Examples                                           |
| ------------- | ------------------------------------------------------- | -------------------------------------------------- |
| **Attached**  | attaching an `ActionSource` to an S0 or S1 loop         | policy (VLA/ACT), teleop, scripted trajectory      |
| **Setpoint**  | writing deadline-bounded setpoints per call             | `servo`, `look_at`, `jog`, gripper open/close      |
| **Delegated** | driving an external controller and mirroring its status | Nav2 `navigate_to`, vendor locomotion, MoveIt plan |

### 8.2 Example: PolicyBehavior (attached)

```python
@dataclass
class PickParams(Params):
    object: str

class PolicyBehavior:
    def __init__(self, name, description, model, claims, task, success, timeout_s, params=PickParams,
                 execution: Execution | None = None, tier="s1"):
        ...

    def start(self, params, ctx):
        model = ctx.models.action(self.model)                  # ActionModel, any framework/placement
        source = PolicySource(                                 # existing class, unchanged
            model,
            execution=self.execution or ctx.models.default_execution(model),   # RTC for remote
            task=self.task.format(**asdict(params)),
        )
        ctx.loops[self.tier].attach(source, effectors=self.claims, lease=ctx.lease)
        return AttachedRun(
            source=source,
            success=self.success,
            timeout_s=self.timeout_s,
            on_end=lambda: ctx.loops[self.tier].detach(ctx.lease),
        )
```

### 8.3 Example: ServoBehavior (setpoint)

```python
@dataclass
class ServoParams(Params):
    dx: float = 0.0; dy: float = 0.0; dz: float = 0.0
    droll: float = 0.0; dpitch: float = 0.0; dyaw: float = 0.0

class ServoBehavior:
    params = ServoParams
    tier = "s0"

    def start(self, p, ctx):
        current = ctx.world.get("arm.ee_pose").value
        step = clamp_delta(p, self.max_step_m, self.max_step_rad)
        target = compose(current, step)
        ctx.loops["s0"].command_server.write(
            {"arm": Command(CommandMode.EE_POSE, target, frame="base", deadline=ctx.clock.now() + self.hold_after_s)},
            lease=ctx.lease,
        )
        return SetpointRun(
            done=lambda: pose_error(ctx.world.get("arm.ee_pose").value, target) < self.tolerance,
            timeout_s=self.max_duration_s,
        )
```

### 8.4 Example: Nav2Behavior (delegated)

```python
class Nav2Behavior:
    claims = frozenset({"base"})
    tier = "external"

    def start(self, p, ctx):
        goal = self._nav2.send_goal(pose_from(p, ctx.world))      # ROS 2 action client, optional extra
        return DelegatedRun(
            status=lambda: map_status(goal.status()),
            feedback=lambda: {"distance_remaining": goal.feedback().distance_remaining},
            cancel=goal.cancel,
        )
```

Delegated behaviors still take a lease: while Nav2 owns `base`, no policy can write to it.

### 8.5 Leases

```python
@dataclass(frozen=True)
class Lease:
    id: LeaseId
    effectors: frozenset[Effector]
    holder: ClientId                 # "agent", "mcp:laptop-42", "studio:user@…", "script"
    priority: Priority
    expires_at: float                # renewed by heartbeat

class LeaseManager:
    def acquire(self, effectors, holder, priority, ttl_s) -> Lease:
        with self._lock:
            conflicts = self._held_overlapping(effectors)
            if any(c.priority >= priority for c in conflicts):
                raise LeaseBusy(conflicts)
            for c in conflicts:                       # higher priority preempts
                self._revoke(c, reason="preempted")   # → its behavior is cancelled, loops detach
            lease = Lease(new_id(), effectors, holder, priority, now() + ttl_s)
            self._held[lease.id] = lease
            return lease

    def heartbeat(self, holder: ClientId) -> None:
        for lease in self._held_by(holder):
            self._extend(lease, now() + lease.ttl)

    def _reaper(self):                                # runs at ~10 Hz on the executive thread
        for lease in self._expired(now()):
            self._revoke(lease, reason="expired")     # client vanished → cancel → robot holds
```

`whole_body` is a parent of `left_arm`, `right_arm`, and `base`; leasing it conflicts with
leasing any child. This is how a coupled whole-body policy and independent arm/base
behaviors coexist without an arbiter.

### 8.6 BehaviorHandle

```python
class Status(Enum):
    PENDING = "pending"; RUNNING = "running"
    SUCCEEDED = "succeeded"; FAILED = "failed"; CANCELLED = "cancelled"

class BehaviorHandle:
    id: RunId
    behavior: str
    params: Params

    def status(self) -> Status: ...
    def feedback(self) -> Mapping[str, Any]: ...        # progress, queue depth, starved, distance…
    def cancel(self, reason: str = "") -> None: ...
    def update(self, params: Mapping[str, Any]) -> None: ...   # e.g. new task prompt; applied at a tick boundary
    def result(self, timeout_s: float | None = None) -> BehaviorResult: ...   # blocks until terminal
    def updates(self) -> Iterator[BehaviorUpdate]: ...   # status/feedback stream

@dataclass(frozen=True)
class BehaviorResult:
    status: Status
    evidence: Mapping[str, Any]          # success-check output, error, last feedback
    episode: EpisodeRef                  # recording of this run (§14)
    duration_s: float
```

### 8.7 Executive.submit, end to end

```python
class Executive:
    def submit(self, behavior: str, params: Mapping | Params, *,
               client: ClientId = "local", priority: Priority = Priority.NORMAL) -> BehaviorHandle:
        b = self._registry[behavior]
        p = b.params.parse(params)                                   # schema validation
        self._policy.authorize(client, b, p)                         # intent policy (§12.3)
        pre = b.can_start(p, self._world)
        if not pre.ok:
            return BehaviorHandle.rejected(b, p, pre.reason)

        lease = self._leases.acquire(b.claims, client, priority, ttl_s=self._ttl(client))
        handle = BehaviorHandle.new(b, p, lease)
        self._recorder.begin_episode(handle)                         # §14
        run = b.start(p, BehaviorContext(lease, self._world, self._loops, self._models, ...))
        self._supervisor.watch(handle, run)                          # status/feedback/success/timeout
        return handle
```

```python
class Supervisor:                     # one thread; polls cheap things, schedules expensive checks
    def _step(self):
        for handle, run in self._active():
            status = run.poll()                                      # source finished? delegated status?
            if status is Status.RUNNING and run.success:
                est = run.success.evaluate(self._world)              # may be a VLM judge (async)
                if est.met and est.confidence >= run.success.threshold:
                    status = Status.SUCCEEDED
            if status is Status.RUNNING and run.timed_out():
                status = Status.FAILED
            if status.terminal:
                run.end()                                            # detach sources, stop delegated
                self._leases.release(handle.lease)
                self._recorder.end_episode(handle, status, run.evidence())
                handle._finish(status, run.evidence())
```

### 8.8 WorldState and success checks

```python
class WorldState:
    def get(self, key: str) -> StateEntry | None: ...           # latest value, timestamp, source
    def snapshot(self, keys: Iterable[str] | None = None) -> Mapping[str, StateEntry]: ...
    def publish(self, key: str, value: Any, *, source: str, stamp: float) -> None: ...  # single writer per key
    def subscribe(self, key: str, fn: Callable[[StateEntry], None]) -> Subscription: ...

@dataclass(frozen=True)
class StateEntry:
    value: Any
    stamp: float           # shared clock
    source: str            # "s1.proprio", "detector", "judge", "nav2.localization"
```

```python
class SuccessCheck(Protocol):
    threshold: float
    def evaluate(self, world: WorldState) -> SuccessEstimate: ...

class VLMJudge:                                  # success decided by a reasoning model
    def __init__(self, model: ReasoningModel, question: str, camera: str, threshold=0.8): ...
    def evaluate(self, world):
        frame = world.get(f"camera.{self.camera}").value
        reply = self.model.complete([user(self.question, images=[frame])], response_format=YesNoConfidence)
        return SuccessEstimate(met=reply.yes, confidence=reply.confidence, evidence={"reason": reply.reason})
```

Perceivers (detectors, captioners, localization) are plain components that run at their
own rate and `publish()` into `WorldState`. They use the same model loaders as everything
else (§9).

---

## 9. Models and Backends

### 9.1 Two interfaces, many roles

"Planner", "navigator", "manipulator", and "judge" are **roles that behaviors bind to
models**, not classes. Models have only two interface shapes (plus an optional third):

```python
class ActionModel(Protocol):                  # = today's InferenceModel
    spec: ModelSpec
    def predict_action_chunk(self, observation: Mapping[str, np.ndarray]) -> np.ndarray: ...
    def reset(self) -> None: ...
    def close(self) -> None: ...

class ReasoningModel(Protocol):
    def complete(
        self,
        messages: Sequence[Message],          # text + images
        *,
        tools: Sequence[ToolSpec] = (),
        response_format: type | None = None,  # structured output when supported
        max_tokens: int | None = None,
    ) -> Completion: ...                      # text, tool_calls, usage, latency

class WorldModel(Protocol):                   # optional, later
    def rollout(self, obs: Observation, actions: Sequence[np.ndarray]) -> PredictedTrajectory: ...
```

### 9.2 ModelSpec: what data a model needs

The hardest cross-framework problem is not loading the model; it is feeding it. LeRobot
feature names, GR00T's `modality.json`, and our `Observation` all describe the same idea
differently. Every model declares a `ModelSpec`; the robot config declares what it
provides; binding is checked **at startup**, not mid-run.

```python
@dataclass(frozen=True)
class Feature:
    name: str                          # "images.wrist", "state.arm", "task"
    shape: tuple[int, ...]
    dtype: str                         # "uint8", "float32", "str"
    frame: str | None = None           # "camera_wrist", "base"
    units: str | None = None           # "rad", "m", "rgb"
    rate_hz: float | None = None

@dataclass(frozen=True)
class ModelSpec:
    inputs: Mapping[str, Feature]
    outputs: Mapping[str, Feature]     # usually one "action" feature
    command: Mapping[Effector, CommandMode]   # what the output means, per effector
    chunk_size: int = 1
    language: bool = False
```

```python
def bind(spec: ModelSpec, provided: ProvidedFeatures, mapping: FeatureMap) -> Binding:
    """Validate and build the obs → model-input and model-output → Action transforms."""
    missing = [f for f in spec.inputs if mapping.source_for(f) not in provided]
    if missing:
        raise BindError(f"model needs {missing}; robot provides {sorted(provided)}")
    for name, feat in spec.inputs.items():
        check_compatible(provided[mapping.source_for(name)], feat)   # shape, dtype, units, frame
    return Binding(
        to_model=build_input_transform(spec, mapping),               # resize, reorder, rename
        to_action=build_output_transform(spec, mapping),             # split vector per effector
    )
```

```yaml
# per-behavior feature mapping (only needed when names differ)
behaviors:
  pick:
    model: pick_policy
    features:
      images.ego_view: camera.head
      images.wrist: camera.wrist_r
      state.right_arm: robot.right_arm.joint_position
      action.right_arm: right_arm # output slice → effector
```

Plugins convert their native format into a `ModelSpec`:

```python
# in the LeRobot plugin
def spec_from_lerobot(config: PreTrainedConfig) -> ModelSpec: ...
# in the GR00T plugin
def spec_from_modality_json(path: Path, embodiment: str) -> ModelSpec: ...
```

### 9.3 Framework adapters (plugins)

The existing entry-point group stays the extension point:

```toml
# a plugin's pyproject.toml (existing mechanism)
[project.entry-points."physicalai.inference.adapters"]
gr00t = "physicalai_gr00t_policy_plugin.runtime:Gr00tAdapter"
```

```python
class RuntimeAdapter(ABC):                     # existing base; spec() is new
    def load(self, model_path: Path) -> None: ...
    def predict(self, inputs: dict[str, Any]) -> dict[str, np.ndarray]: ...
    def spec(self) -> ModelSpec: ...           # NEW
    def environment(self) -> Environment | None: ...   # NEW: isolate env if deps conflict (PR #8)
```

### 9.4 Reasoning backends

Almost every local and hosted LLM server speaks the OpenAI-compatible chat API, so one
client covers SGLang, vLLM, llama.cpp server, Ollama, LM Studio, and many hosted
providers. Native clients exist only where the native API is materially better.

```python
REASONING_BACKENDS = {                         # entry-point group: physicalai.reasoning.backends
    "openai-compatible": OpenAICompatible,     # SGLang, vLLM, llama.cpp server, Ollama, hosted
    "anthropic":         AnthropicClient,
    "gemini":            GeminiClient,
    "openvino-genai":    OpenVINOGenAI,        # in-process on CPU/GPU/NPU
    "llama.cpp":         LlamaCppInProcess,    # in-process GGUF
}

def load_reasoning_model(backend: str, **options) -> ReasoningModel:
    return REASONING_BACKENDS[backend](**options)
```

```python
planner = load_reasoning_model("openai-compatible", base_url="http://gpu-box:30000/v1", model="qwen3-vl-8b")
planner = load_reasoning_model("anthropic", model="claude-…")
planner = load_reasoning_model("openvino-genai", path="models/qwen3-vl-ov", device="GPU.1")
```

### 9.5 Placement: where a model runs

Placement is independent of framework. A worker is **a remote server on localhost**: the
isolate worker and `physicalai serve` run the same adapter code and speak the same
Inference API (§10.2); only the binding differs.

```python
def load_action_model(spec: ModelConfig) -> ActionModel:
    match spec.placement:
        case "inprocess":
            return InferenceModel(spec.path, adapter=spec.framework, device=spec.device)
        case "worker":
            env = adapter_environment(spec.framework)           # from plugin metadata
            prepared = isolate.prepare(env)                     # PR #8: cached, locked venv
            endpoint = isolate.start(prepared, entrypoint=SERVE_ENTRYPOINT,
                                     args=["--model", spec.path, "--listen", "unix:///run/pai/pick.sock"])
            return RemoteActionModel(endpoint, shm=True)        # Unix socket + shared-memory tensors
        case "remote":
            return RemoteActionModel(spec.endpoint)             # gRPC over TCP
```

| Placement   | Who starts it                                           | Binding                     | Typical use                                               |
| ----------- | ------------------------------------------------------- | --------------------------- | --------------------------------------------------------- |
| `inprocess` | the runtime                                             | direct call                 | OpenVINO/ONNX exports, small perception models            |
| `worker`    | the runtime, via `physicalai.isolate`                   | Unix socket + shared memory | LeRobot / GR00T native, conflicting deps, crash isolation |
| `remote`    | someone else (`physicalai serve`, a GPU box, a cluster) | gRPC over TCP               | models that do not fit locally                            |

### 9.6 Remote action models and latency

A remote S1 model works when the action chunk covers the round trip:

```text
chunk_horizon_s  >  network_rtt_s + inference_s + safety_margin_s
e.g. 16 actions @ 30 Hz = 0.53 s  >  0.02 + 0.15 + 0.10   ✓
     1 action   @ 30 Hz = 0.033 s >  0.02 + 0.15           ✗  → keep it local
```

`AsyncExecution` / `RTCExecution` already request the next chunk while the current one
plays. The new behavior is the starvation path:

```python
class PolicySource:                                # existing class, starvation path made explicit
    def update(self, robot_state, camera_frames, step):
        self._execution.maybe_request(obs)
        action = self._queue.pop_or_none()
        if action is None:                         # chunk late
            self._bus.emit(Starved(step=step))     # → handle.feedback()["starved"] = True
            return self._hold_action(robot_state)  # hold, never extrapolate blindly
        return action
```

### 9.7 Client adapters for existing policy servers

Many models are already deployed behind their own servers. We do not ask anyone to
re-host them; we ship clients that present them as `ActionModel`s:

```python
EXTERNAL_SERVERS = {
    "openpi":        OpenPIWebsocketClient,     # π0 / π0.5 policy servers
    "lerobot-async": LeRobotAsyncClient,        # LeRobot async inference (gRPC)
    "gr00t-zmq":     Gr00tZmqClient,            # Isaac-GR00T inference server
}
```

```yaml
pick_policy:
  {
    kind: action,
    server: openpi,
    endpoint: ws://gpu-box:8000,
    spec: specs/pi05_droid.yaml,
  }
```

### 9.8 Devices and accelerators

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

Rules when everything shares one host:

- S0 gets dedicated cores and RT priority and never shares an accelerator with S2.
- S1 gets its own accelerator when one exists; if it must share with S2, S1 has priority.
  GPUs preempt poorly — **measure** S1 jitter under S2 load before relying on sharing.
- S2 and perception take what is left; they are latency-tolerant by design.
- `device` maps to OpenVINO device names (`CPU`, `GPU.0`, `NPU`, `AUTO`), CUDA device ids, or
  llama.cpp offload settings, depending on the backend.

---

## 10. The Three Contracts and Their Bindings

### 10.1 Contract #1 — Robot command interface (S1/S2 ↔ S0)

Small, fixed-layout, implementable in C on an RTOS. This is the contract that must survive
a real-time guest or a vendor controller with no Python.

```c
/* pai_command.h — reference schema (version 1) */
typedef struct {
    uint32_t magic;          /* 'PAIC' */
    uint16_t version;        /* 1 */
    uint16_t effector_id;    /* index into the announced effector table */
    uint64_t seq;            /* monotonic per writer */
    int64_t  stamp_ns;       /* writer clock (shared, §13) */
    int64_t  deadline_ns;    /* S0 holds / safe-stops after this */
    uint64_t lease_id;       /* must match the active lease for this effector */
    uint16_t mode;           /* JOINT_POSITION | JOINT_VELOCITY | EE_POSE | TWIST | WHOLE_BODY | GRIPPER */
    uint16_t dim;
    uint32_t frame_id;       /* for EE_POSE / TWIST */
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
    float    position[64];
    float    velocity[64];
    float    effort[64];
} pai_state_t;

typedef struct {             /* announced once at connect */
    uint16_t n_effectors;
    struct { char name[32]; uint32_t modes_mask; uint16_t dims[8]; uint16_t parent; } effectors[16];
} pai_describe_t;
```

Semantics that are part of the contract, not implementation details:

- A setpoint with `deadline_ns < now` is ignored; an effector whose last valid setpoint has
  expired goes to its configured `on_stale` policy (`hold`, `decay`, `safe_stop`).
- A setpoint whose `lease_id` is not the active lease for that effector is rejected.
- The S0 side owns the safety gate; `status = GATE_CLAMPED` reports that a command was
  modified.

### 10.2 Contract #2 — Inference API (loops ↔ action models)

Extends the gRPC design in [`policy_server_design.md`](./policy_server_design.md).

```proto
service ActionModelService {
  rpc Describe(Empty) returns (ModelSpecProto);                // spec, chunk size, devices
  rpc Session(stream InferRequest) returns (stream InferReply); // bidirectional streaming
  rpc Reset(ResetRequest) returns (Empty);
  rpc Health(Empty) returns (HealthReply);
}

message InferRequest {
  uint64 seq = 1;
  int64  stamp_ns = 2;
  map<string, Tensor> inputs = 3;      // inline bytes, or a shared-memory handle (same host)
  string task = 4;
}

message InferReply {
  uint64 seq = 1;                      // echoes request
  int64  stamp_ns = 2;
  Tensor action_chunk = 3;             // [chunk, dim]
  double inference_s = 4;
}

message Tensor {
  repeated int64 shape = 1;
  string dtype = 2;
  oneof data { bytes inline = 3; ShmRef shm = 4; }
}
```

### 10.3 Contract #3 — Executive API (clients ↔ executive)

```proto
service Executive {
  rpc ListBehaviors(Empty) returns (BehaviorList);               // names, descriptions, JSON schemas, claims
  rpc Submit(SubmitRequest) returns (stream BehaviorUpdate);     // status + feedback until terminal
  rpc Cancel(CancelRequest) returns (Empty);
  rpc Heartbeat(stream ClientHeartbeat) returns (stream LeaseStatus);  // keeps the client's leases alive
  rpc Observe(ObserveRequest) returns (WorldSnapshot);           // state + downsampled images
  rpc Events(EventsFilter) returns (stream Event);               // full event log (§14)
  rpc Stop(StopRequest) returns (Empty);                         // cancel all, hold
}
```

An MCP adapter (§15.4) is generated on top of this API for coding assistants.

### 10.4 Bindings

| Contract                | Same process | Same OS, other process              | Across VMs on one host              | Across hosts                     |
| ----------------------- | ------------ | ----------------------------------- | ----------------------------------- | -------------------------------- |
| Robot command interface | direct call  | shared-memory ring                  | ivshmem ring, or vsock              | UDP; optional Zenoh / DDS bridge |
| Inference API           | direct call  | Unix socket + shared-memory tensors | virtio-net gRPC (+ ivshmem tensors) | gRPC over TCP                    |
| Executive API           | direct call  | Unix socket                         | vsock or virtio-net gRPC / MCP      | gRPC / MCP over TCP              |

```python
def open_binding(link: LinkConfig) -> Binding:
    match (link.contract, link.binding):
        case ("robot_command", "inprocess"): return InProcessCommandLink()
        case ("robot_command", "shm"):       return ShmRingCommandLink(link.name)
        case ("robot_command", "ivshmem"):   return IvshmemCommandLink(link.device)
        case ("robot_command", "udp"):       return UdpCommandLink(link.address)
        case ("inference", "unix"):          return GrpcChannel(f"unix://{link.path}", shm=True)
        case ("inference", "tcp"):           return GrpcChannel(link.address)
        case ("executive", "vsock"):         return GrpcChannel(f"vsock:{link.cid}:{link.port}")
        case ("executive", "tcp"):           return GrpcChannel(link.address)
        case _:                              raise ConfigError(f"unsupported binding {link}")
```

`binding: auto` picks the cheapest binding that the two partitions support.

---

## 11. Deployment Topologies

The same `robot.yaml` structure covers every topology; only `partitions`, `placement`,
`links`, and model `placement`/`endpoint`/`device` change.

### 11.1 One process (development, simple robots)

```yaml
robot: { driver: so101, port: /dev/ttyACM0 }
cameras: { wrist: { driver: uvc, index: 0 }, top: { driver: uvc, index: 2 } }
tiers: { s1: { fps: 30 } } # S0 = interpolator inside S1
models:
  pick_policy: { kind: action, framework: openvino, path: exports/act-pick }
  place_policy: { kind: action, framework: openvino, path: exports/act-place }
  planner: { kind: reasoning, backend: anthropic, model: claude-… }
behaviors:
  pick: { type: policy, model: pick_policy, claims: [arm, gripper] }
  place: { type: policy, model: place_policy, claims: [arm, gripper] }
agent: { model: planner }
```

### 11.2 One host, several accelerators

```yaml
tiers:
  s0: { fps: 200, process: true, realtime: true, cpu_affinity: [2, 3] }
  s1: { fps: 30 }
models:
  wbc:
    {
      kind: action,
      framework: onnx,
      path: exports/wbc.onnx,
      device: CPU,
      tier: s0,
    }
  pick:
    {
      kind: action,
      framework: gr00t,
      placement: worker,
      device: GPU.0,
      priority: high,
    }
  planner:
    {
      kind: reasoning,
      backend: openvino-genai,
      path: models/qwen3-vl-ov,
      device: GPU.1,
    }
  judge:
    {
      kind: reasoning,
      backend: llama.cpp,
      path: models/judge.gguf,
      device: GPU.1,
    }
```

### 11.3 Robot + GPU box + cloud

```yaml
partitions:
  robot: { kind: host, address: robot.local }
  gpu-box: { kind: host, address: gpu-box.lan }
placement:
  robot: [tiers.s0, tiers.s1, executive, agent]
  gpu-box: [serve.pick]
models:
  pick:
    {
      kind: action,
      framework: gr00t,
      placement: remote,
      endpoint: grpc://gpu-box.lan:50051,
    }
  planner: { kind: reasoning, backend: anthropic, model: claude-… } # cloud
links:
  robot-gpu: { contract: inference, binding: tcp }
```

### 11.4 KVM guests on one SoC

```yaml
partitions:
  s0: { kind: vm, os: rt-linux, cores: [2, 3], devices: [ethercat0] }
  s1: { kind: vm, devices: [gpu0, cam0, cam1] }
  s2: { kind: vm, devices: [gpu1], network: internet }
placement:
  s0: [tiers.s0, models.wbc]
  s1: [tiers.s1, executive, models.pick]
  s2: [models.planner, agent]
links:
  s1-s0: { contract: robot_command, binding: ivshmem, device: /dev/uio0 }
  s2-s1: { contract: executive, binding: vsock, cid: 3, port: 7400 }
clock: { sync: ptp_kvm }
```

### 11.5 Vendor S0 (no PhysicalAI code in the real-time domain)

```yaml
partitions:
  s0: { kind: external } # vendor RTOS / controller implements pai_command.h
  s1: { kind: host }
links:
  s1-s0: { contract: robot_command, binding: udp, address: 192.168.123.10:9100 }
```

The vendor implements the C schema (§10.1) once; everything above it is unchanged.

---

## 12. Safety

### 12.1 Layers

| Layer                                  | Owner                                  | Enforces                                                    | Bypassable by a client? |
| -------------------------------------- | -------------------------------------- | ----------------------------------------------------------- | ----------------------- |
| Hardware e-stop, motor firmware limits | robot                                  | power, torque, temperature                                  | no                      |
| **S0 safety gate** (authoritative)     | lowest software tier we run, or vendor | joint/EE/velocity/force limits, workspace, `on_stale`       | no                      |
| S1 safety gate (defense in depth)      | S1 `ControlLoop`                       | same limits, earlier                                        | no                      |
| Lease + deadline semantics             | executive + S0 command server          | single writer per effector; stale ⇒ hold                    | no                      |
| Intent policy                          | executive                              | which client may run which behavior, approvals, rate limits | configured per profile  |
| Partition boundary (KVM)               | platform                               | S2 guest has no device access                               | no                      |

The gate is a dedicated, guaranteed-last slot, not an ordinary callback:

```python
class SafetyGate:
    @classmethod
    def from_limits(cls, limits: RobotLimits) -> Self: ...

    def enforce(self, action: Action, obs: RobotObservation) -> Action:
        out = {}
        for effector, cmd in action.items():
            lim = self._limits[effector]
            safe = lim.clamp(cmd, obs)                  # position, velocity, accel, workspace, force
            if lim.violates_hard(cmd, obs):
                raise SafetyStop(effector, cmd, reason=lim.explain(cmd, obs))   # loop → safe shutdown
            if safe is not cmd:
                self._emit(GateClamped(effector, cmd, safe))
            out[effector] = safe
        return out
```

### 12.2 Distributed failure handling

| Failure                                     | Detected by                                       | Response                                                         |
| ------------------------------------------- | ------------------------------------------------- | ---------------------------------------------------------------- |
| Client (agent, laptop, MCP host) disappears | missed lease heartbeats                           | lease expires → behavior cancelled → effectors hold              |
| S2 guest crashes / OOMs                     | executive link down                               | same as above; S1 / S0 unaffected                                |
| Remote VLA late or down                     | action queue starvation                           | source holds; `feedback.starved`; behavior may fail after budget |
| S1 stops sending setpoints                  | S0 deadline watchdog                              | `on_stale` per effector (`hold`, `decay`, `safe_stop`)           |
| S0 fault                                    | S0 status `FAULT`                                 | S1 loop shuts down; executive fails all behaviors                |
| Clock skew between partitions               | stamp sanity checks at link open and periodically | refuse to start / degrade to `hold`                              |

### 12.3 Intent policy and profiles

```yaml
intent_policy:
  profiles:
    safeguarded:
      allow: [pick, place, observe, describe_scene, servo]
      servo: { max_step_m: 0.01, max_speed_mps: 0.05 }
      require_approval: [start_recording]
      rate_limit: { servo: 5/s }
    unrestricted:
      allow: ["*"] # removes intent limits; the S0/S1 gates still apply
  clients:
    "mcp:*": safeguarded
    "agent": safeguarded
    "studio:*": unrestricted
    "script": unrestricted
```

"Unrestricted" means unrestricted **intent**, never unrestricted **actuation**. The gate
and the lease/deadline rules hold in every profile.

---

## 13. Time and Clocks

- All partitions share one clock: `chrony` / PTP across hosts, `ptp_kvm` across guests.
- Every observation, setpoint, inference request/reply, and event carries `stamp_ns`.
- Setpoints carry absolute `deadline_ns`, not durations, so they mean the same thing on
  both sides of a link.
- `WorldState` entries carry their production stamp; readers own staleness policy
  ("detection < 500 ms old").
- Links measure offset at open and periodically; skew beyond a configured bound is a fault.

---

## 14. Recording and the Data Flywheel

Every behavior run is **one labeled episode**. The event log is the single source of
truth for recording, Studio's live view, MCP status, and agent run logs.

```python
@dataclass(frozen=True)
class EpisodeMeta:
    run_id: RunId
    behavior: str
    params: Mapping[str, Any]
    client: ClientId                  # who asked: agent, mcp:laptop-42, studio:user, script
    parent: RunId | None              # agent task that spawned it
    status: Status
    evidence: Mapping[str, Any]
    models: Mapping[str, ModelProvenance]   # per model used in this run
    effectors: frozenset[Effector]
    started_ns: int
    ended_ns: int

@dataclass(frozen=True)
class ModelProvenance:
    name: str                         # "pick_policy"
    framework: str                    # "gr00t"
    artifact_sha256: str | None       # None for cloud APIs
    endpoint: str | None              # "grpc://gpu-box:50051", "anthropic:claude-…"
    placement: str                    # inprocess | worker | remote
    device: str | None
```

```text
episode/
  meta.json            EpisodeMeta
  data/                LeRobot-format observations + actions at the loop rate
  events.jsonl         behavior updates, gate clamps, starvation, lease changes
  agent.jsonl          reasoning model messages + tool calls (if an agent was involved)
```

Cloud reasoning is not reproducible; the recorded transcript and provenance are the only
record of what decided each action. `FAILED` and `CANCELLED` runs are recorded with the
same fidelity as successes.

---

## 15. Clients: Python, CLI, Studio, MCP, Built-in Agent

All clients use the Executive API (direct call in-process, gRPC otherwise).

### 15.1 Python

```python
ex = RobotRuntime.from_config("robot.yaml").executive        # local
ex = ExecutiveClient("grpc://robot.local:7400", name="notebook")   # remote

for b in ex.behaviors():
    print(b.name, b.claims, b.params.json_schema())

h = ex.submit("pick", {"object": "mug"})
for update in h.updates():
    print(update.status, update.feedback)
```

### 15.2 CLI

```bash
physicalai behaviors list --executive robot.local:7400
physicalai behaviors run pick --object mug --executive robot.local:7400
physicalai behaviors cancel <run-id>
physicalai events --follow --executive robot.local:7400
physicalai stop --executive robot.local:7400
```

### 15.3 Studio

Studio's backend is a client: behavior catalog and forms from `ListBehaviors` schemas, live
status from `Submit` updates, recordings from the event log. Studio keeps owning projects,
datasets, jobs, and UI.

### 15.4 MCP adapter (generated)

```python
def build_mcp_server(ex: ExecutiveClient, profile: str) -> MCPServer:
    server = MCPServer("physicalai-robot")
    for b in ex.behaviors(profile=profile):                    # filtered by intent policy
        server.add_tool(
            name=b.name,
            description=b.description,
            input_schema=b.params_schema,
            handler=lambda args, b=b: ex.submit(b.name, args).result().to_tool_result(),
        )
    server.add_tool("observe", "Current robot state and camera images.", {}, lambda _: ex.observe().to_tool_result())
    server.add_tool("stop", "Cancel everything and hold.", {}, lambda _: ex.stop())
    server.on_session(lambda s: ex.heartbeat_while(s.alive))  # lease keep-alive tied to the session
    return server
```

No hand-written tool list: adding a behavior adds a tool for every client.

### 15.5 Built-in agent (reference System 2)

```python
class ToolLoopAgent:
    def __init__(self, model: ReasoningModel, ex: ExecutiveClient, *, max_steps: int = 50,
                 memory: AgentMemory | None = None):
        self.model, self.ex, self.max_steps, self.memory = model, ex, max_steps, memory

    def run(self, task: str) -> AgentResult:
        tools = [b.as_tool() for b in self.ex.behaviors()] + [OBSERVE_TOOL, DONE_TOOL]
        snap = self.ex.observe(images=True, max_side=448)
        messages = [
            system(ROBOT_SYSTEM_PROMPT, lessons=self.memory.lessons() if self.memory else ()),
            user(task, images=snap.images, state=snap.state_summary()),
        ]
        for _ in range(self.max_steps):
            reply = self.model.complete(messages, tools=tools)
            messages.append(reply.as_message())
            if not reply.tool_calls:
                return AgentResult(summary=reply.text, transcript=messages)
            for call in reply.tool_calls:
                if call.name == "done":
                    return AgentResult(summary=call.arguments["summary"], transcript=messages)
                if call.name == "observe":
                    out = self.ex.observe(images=True, max_side=448).to_tool_result()
                else:
                    out = self.ex.submit(call.name, call.arguments, client="agent").result().to_tool_result()
                messages.append(tool_result(call.id, out))
        self.ex.stop()
        return AgentResult(summary="step budget exhausted", transcript=messages)
```

Swapping this for a coding assistant, a behavior tree, or a human operator changes nothing
below the Executive API.

### 15.6 System 1 / System 2 supervisor pattern

"Run the VLA at 30 Hz and let a VLM check every 2 s whether to change the task":

```python
h = ex.submit("manipulate", {"task": "put red blocks in the left bin"})
while h.status() is Status.RUNNING:
    time.sleep(2.0)
    verdict = judge.complete([user("Is the robot stuck or done? Answer stuck/done/ok.",
                                   images=ex.observe().images)], response_format=Verdict)
    if verdict.label == "done":
        h.cancel("judge: done")
    elif verdict.label == "stuck":
        h.cancel("judge: stuck")
        h = ex.submit("manipulate", {"task": "push the blocks apart, then retry"})
```

`set_task` on a running policy (changing the prompt without detaching) is a behavior-level
update, `h.update({"task": ...})`, applied at a tick boundary.

---

## 16. With and Without the Physical AI Agent Proposal

The _Physical AI Agent_ deck proposes developer tooling: `physicalai-agent init`, harness
skills, a Studio MCP server, a Runtime MCP server with guarded profiles, an `agent run`
loop, run logs, and memory. It is not final. This design works **with or without** it.

### 16.1 Without it

The Python API, CLI, Studio, and the built-in agent (§15) drive the executive directly.
Nothing here depends on the deck.

### 16.2 With it

The deck becomes the **developer and coding-assistant layer** on top of the Executive API.

| Deck component                             | Maps to                                                | Change needed                                                                    |
| ------------------------------------------ | ------------------------------------------------------ | -------------------------------------------------------------------------------- |
| `mcp/runtime.py` tool list                 | generated MCP adapter (§15.4)                          | tools come from the behavior registry, not hand-written                          |
| `RobotSession`, `PolicyRunner`             | `ExecutiveClient`                                      | connect to an executive (possibly remote) instead of owning the robot in-process |
| `ActionGuard`                              | intent policy (§12.3) **+** S0/S1 safety gates (§12.1) | split: permissions in the executive, limits in the lowest tier                   |
| `start_policy`, `set_task`                 | `submit("<policy behavior>")`, `handle.update(...)`    | none beyond naming                                                               |
| `move_joints_delta`                        | `servo` / `jog` setpoint behavior (§8.3)               | goes through lease + gate, not direct actuation                                  |
| `agent/loop.py`, `ModelClient`             | `ToolLoopAgent`, `ReasoningModel` (§9.4, §15.5)        | shared with VLM judges and perceivers                                            |
| `agent/runs/`, `memory.md`                 | event log + `agent.jsonl` (§14)                        | read from the executive's event stream                                           |
| `limits.mode: unrestricted`                | profile `unrestricted`                                 | removes intent limits only; gates always on                                      |
| `init`, skills, templates, developer types | unchanged                                              | generate `robot.yaml` partitions/behaviors as part of `init`                     |

### 16.3 Naming

The coding-assistant ecosystem already uses "skill" for `SKILL.md` files (Runtime PR #181,
the deck's `pai-*` skills). This design therefore uses **Behavior** for robot capabilities
and reserves **skill** for agent/harness skills.

---

## 17. What Changes in Today's Code

| Area                                   | Change                                                                                                       | Breaking?                                                      |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------- |
| `physicalai.runtime.core.RobotRuntime` | loop body moves to `ControlLoop`; `RobotRuntime` becomes the partition host; constructor kept                | no for constructor users                                       |
| Actions                                | `np.ndarray \| Action`; flat arrays normalized to a single effector                                          | no for existing sources; yes for callbacks that assume ndarray |
| `Robot` protocol                       | optional `command_interfaces()`; `send_action` accepts `Action`                                              | no (optional)                                                  |
| `RuntimeCallback.on_action_ready`      | receives `Action` mapping                                                                                    | yes — small migration                                          |
| Safety                                 | dedicated `SafetyGate` slot after callbacks                                                                  | additive                                                       |
| `RobotRuntime.stop()`                  | already exists; extended to stop all hosted loops                                                            | no                                                             |
| Inference                              | `ModelSpec`, `RuntimeAdapter.spec()`, `placement`, `RemoteActionModel`                                       | additive                                                       |
| `physicalai.isolate` (PR #8)           | worker entrypoint becomes the Inference API server                                                           | additive                                                       |
| GR00T / LeRobot plugins (#10 / #23)    | pipe protocol replaced by the Inference API over Unix socket + shm                                           | plugin-internal                                                |
| New packages                           | `physicalai.autonomy` (executive), `physicalai.autonomy.agents` (optional), `physicalai.inference.reasoning` | additive                                                       |
| CLI                                    | `--partition`, `physicalai up`, `physicalai serve`, `physicalai behaviors …`                                 | additive                                                       |

---

## 18. Build Order

Each phase ends with a demo on real hardware and a measurable exit criterion.

| Phase | Build                                                                                                | Demo                                                  | Exit criterion                                              |
| ----- | ---------------------------------------------------------------------------------------------------- | ----------------------------------------------------- | ----------------------------------------------------------- |
| 1     | Extract `ControlLoop` from `RobotRuntime` (no behavior change)                                       | existing examples                                     | all runtime tests pass unchanged                            |
| 2     | Effector-keyed actions, `attach`/`detach`, deadlines, `SafetyGate` slot                              | SO-101 teleop + policy unchanged                      | callbacks migrated; gate tested with fault injection        |
| 3     | Inference API; isolate worker = localhost server; `placement`; `ModelSpec` for first-party + LeRobot | same policy in-process / worker / remote              | identical actions across placements; S1 jitter under remote |
| 4     | Executive, leases, handles, recording; scripted client                                               | tabletop sort with two policies from two frameworks   | episodes recorded per run; lease expiry holds robot         |
| 5     | `ReasoningModel` backends, Executive API, MCP adapter, `ToolLoopAgent`                               | cloud model and Claude Code both drive the same robot | identical tool list; client disconnect ⇒ hold within TTL    |
| 6     | `ServoBehavior` into S0; command interface schema + C reference server                               | S2 drives S0 directly ("nudge the cup")               | watchdog holds within deadline in every binding             |
| 7     | Hosted S0 `ControlLoop` (process, RT); KVM partitions; ivshmem/vsock bindings                        | humanoid or mobile manipulator, three guests          | S0 jitter budget met under S2 load                          |
| 8     | Delegated navigation; world model as a tool                                                          | navigate → pick → place                               | —                                                           |

Phases 1–4 are useful on their own, even if nothing after them ships.

---

## 19. Risks and Open Questions

| Risk / question                                          | Notes                                                                                                                                       |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| **Spec binding across frameworks** is the largest effort | most integration failures will be here; invest in `ModelSpec` converters and startup validation                                             |
| Remote S1 latency                                        | requires chunked models; per-step remote policies stay local                                                                                |
| GPU sharing between S1 and S2                            | GPUs preempt poorly; measure before promising                                                                                               |
| Callback migration to `Action` mappings                  | small but breaking; do it in phase 2 while the API is young                                                                                 |
| Command interface schema version 1                       | fixed 64-float payload may be too small for some whole-body targets; review with a humanoid vendor                                          |
| Transport for robot-command over hosts                   | UDP is enough for v1; Zenoh / DDS bridges when a customer needs them                                                                        |
| Executive placement                                      | default is the S1 partition; could an S2-hosted executive ever make sense? (current answer: no — it must be close to the loops it controls) |
| Concurrency inside the executive                         | one supervisor thread is enough for v1; revisit with many concurrent behaviors                                                              |
| Too much generality too early                            | each phase must be justified by its demo                                                                                                    |

---

## 20. Decision Summary

```text
tiers              S0 control · S1 reactive policy · S2 deliberation — responsibilities and rate bands, not processes
main runtime       one RobotRuntime per partition; hosts ControlLoops, the Executive, and models placed there
loops              ControlLoop = today's loop body; S0 heavy/RT loops in child processes; >200 Hz stays native/vendor
recursion          every tier is a Robot to the tier above (LoopRobot locally, RemoteRobot across partitions)
actions            effector-keyed Action mappings; flat ndarray = single-effector case
executive          physicalai.autonomy: Behaviors, BehaviorHandles, Leases, WorldState, intent policy, event log
behavior kinds     attached (ActionSource on a loop) · setpoint (deadline-bounded) · delegated (external controller)
concurrency        leases on effectors; one writer per effector; parent/child effectors for whole-body
models             ActionModel + ReasoningModel (+ WorldModel later); roles are bindings, not classes
interop            ModelSpec + startup binding; plugins convert native formats (LeRobot, GR00T) to ModelSpec
placement          inprocess · worker (isolate; a localhost server) · remote; independent of framework
contracts          Robot command interface (C-implementable) · Inference API (gRPC) · Executive API (gRPC + MCP)
bindings           direct · shm · Unix socket · ivshmem · vsock · TCP/UDP — picked per link
safety             gate at the lowest tier we run; leases expire; setpoints have deadlines; intent policy per client
clients            Python · CLI · Studio · MCP (generated) · built-in ToolLoopAgent · human — all equal
data               every behavior run = one labeled episode with model provenance
naming             Behavior (robot capability) vs skill (coding-assistant SKILL.md)
```

---

## Appendix A. Prior Art

| System                                                    | Borrow                                                 | Avoid                                                         |
| --------------------------------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------- |
| Hierarchical VLA (Helix, GR00T, Gemini Robotics ER + VLA) | S2 / S1 / S0 split; language at the top                | assuming one deployment layout                                |
| ros2_control                                              | controllers claim command interfaces; atomic switching | mandatory ROS in the core                                     |
| ROS 2 actions                                             | goal → handle → feedback → result → cancel             | ROS message types in our contracts                            |
| Boston Dynamics SDK                                       | leases, heartbeats, e-stop authority                   | robot-specific assumptions                                    |
| Mixed-criticality hypervisors (KVM, ACRN, Jailhouse)      | partitioning S0 from S2                                | building our own                                              |
| LeRobot async inference, openpi policy servers            | remote chunked inference                               | one server protocol per framework                             |
| LLM tool-calling agents                                   | behaviors as tools; failures as tool results           | letting a model reach actuation without the lease + gate path |
| Classic three-layer architectures (3T)                    | deliberator / sequencer / controller                   | rigid layers that forbid S2 → S0 shortcuts                    |
