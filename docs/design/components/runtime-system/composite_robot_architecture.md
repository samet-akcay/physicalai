# Agentic Autonomy Layer

This document defines the **agentic autonomy layer** for PhysicalAI: the deliberative
system that turns a language goal into grounded robot behavior by planning, imagining
outcomes in a world model, verifying, and dispatching skills to the reactive runtime.

It builds on the production single-rate runtime in
[`robot_runtime_architecture.md`](./robot_runtime_architecture.md) (Doc A). It does **not**
replace it. The whole autonomy stack still executes through **one `RobotRuntime` loop**;
this document defines the layer that sits *above* it and decides *what that loop should be
doing right now*.

It is intentionally future-facing. No code here should be implemented before a concrete
agentic integration (e.g. a Qwen-RobotSuite-style planner + VLA + world model, or a Unitree
G1 humanoid) is in scope. The purpose of writing it now is to ensure Doc A's contracts
(`Controller`, `Robot`, `RobotAction`, `Observation`) extend cleanly to full autonomy
without rework.

---

## Quick Navigation

- [1. The Dual-Process Architecture](#1-the-dual-process-architecture)
- [2. Scope and Non-Goals](#2-scope-and-non-goals)
- [3. Environment: One Substrate for Real, Sim, and World Model](#3-environment-one-substrate-for-real-sim-and-world-model)
- [4. Goal and Skill: The Seam Between the Loops](#4-goal-and-skill-the-seam-between-the-loops)
- [5. The Agent Loop](#5-the-agent-loop)
- [6. Worked Example: A Qwen-RobotSuite-Style Pipeline](#6-worked-example-a-qwen-robotsuite-style-pipeline)
- [7. CompositeController: Multi-Rate Effector Arbitration](#7-compositecontroller-multi-rate-effector-arbitration)
- [8. Execution and Scaling](#8-execution-and-scaling)
- [9. Safety Across Layers](#9-safety-across-layers)
- [10. Prior Art](#10-prior-art)
- [11. What This Doc Does NOT Define](#11-what-this-doc-does-not-define)
- [12. Decision Summary](#12-decision-summary)

---

## 1. The Dual-Process Architecture

Modern robot autonomy is a **two-loop hierarchy**. A slow deliberative process reasons about
goals; a fast reactive process controls the hardware. This is the "System 2 / System 1"
split used by hierarchical VLA stacks, and it is the same shape as the classic three-layer
(deliberator / sequencer / controller) robotics architectures.

```text
┌──────────────────────────────────────────────────────────────────────┐
│  AGENT   —  System 2  ·  deliberative  ·  ~0.1–1 Hz  ·  async        │
│                                                                      │
│    perceive → plan (LLM/VLM) → imagine (WorldModel) → verify         │
│            → commit Goal → dispatch Skill → monitor → replan         │
│                                                                      │
│    reads:  WorldView (fused state)                                   │
│    uses:   WorldModel.rollout()  for counterfactual verification     │
│    emits:  Goal, and the Skill that pursues it                       │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  Goal / Skill
                                │  (long-running, with status + feedback)
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  RobotRuntime   —  System 1  ·  reactive  ·  fixed FPS  ·  sync      │
│                                                                      │
│    runs the ACTIVE Controller that the current Skill installed       │
│    observation → controller.update() → safety → send_action          │
│                                                                      │
│    Doc A. UNCHANGED.                                                 │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼
        Environment (Robot │ Sim)            WorldModel (predictive only)
```

The single most important design decision:

```text
A Skill is realized as a Controller.
The Agent executes a Skill by calling RobotRuntime.swap_controller().
```

There is still exactly one outer loop. The Agent never touches hardware and never runs a
second control loop. It selects *which* `Controller` the one `RobotRuntime` should run next.
Everything Doc A already provides — fixed-rate timing, callbacks, recording, telemetry,
safety, error recovery — applies unchanged to fully autonomous behavior.

### Why the seam matters

The gap in a pure reactive design (e.g. a blackboard with per-tick arbitration) is that it
has no vocabulary for **long-running, goal-directed work with feedback**: "navigate to the
umbrella", "pick the mug", "if the grasp fails, retry from a new approach". That is the
`Goal` / `Skill` contract (§4). It is the robotics equivalent of a long-running action
(propose → executing → succeeded / failed), and it is what lets a general agent treat robot
capabilities as **callable tools**.

---

## 2. Scope and Non-Goals

### Scope

A reusable deliberative layer for robots whose behavior is driven by a planner or agent that
composes multiple decision-making systems:

- A **high-level planner / agent** (LLM or VLM) that decomposes language goals into skills.
- A **world model** used to imagine and rank candidate plans before spending real robot time.
- One or more **policies** (VLA manipulation, navigation) invoked as skills.
- Optional **multi-rate subsystems** (locomotion + arms + gaze) composed into a single tick
  for humanoids and mobile manipulators (§7).

The layer must:

1. Turn a `Goal` into robot behavior through skills executed by `RobotRuntime`.
2. Verify plans against a `WorldModel` without touching hardware.
3. Run planning off the control thread; never stall the reactive tick.
4. Monitor skill execution and replan on failure.
5. Treat real robot, simulator, and world model as the **same `Observation` / `Action`
   substrate** with different capabilities.
6. Stay framework-light: no mandatory ROS 2, no mandatory behavior-tree dependency.

### Non-Goals

- Replacing `RobotRuntime`. The outer loop, lifecycle, callbacks, safety, and dispatch stay
  in Doc A.
- Replacing low-level control (joint servoing, gait MPC, balance). Those live in the `Robot`
  driver or vendor stack.
- Defining the inference plumbing. That is `PolicyController` + `InferenceExecution`
  (Doc A §5). Skills reuse it.
- Becoming a distributed actor framework. The initial layer is single-process with
  per-subsystem worker escape hatches (§8).
- Training the planner, world model, or policies. This is a training/studio layer.

---

## 3. Environment: One Substrate for Real, Sim, and World Model

A Qwen-RobotSuite-style pipeline runs the same behavior **on hardware or in sim**, and uses a
**world model as a neural simulator** to rank plans. These are not three unrelated interfaces
— they are the same `Observation` / `Action` types with different *capabilities*. We model
this with small structural protocols (matching the existing `Robot` Protocol style), not a
single god-interface.

```python
class Observable(Protocol):
    def get_observation(self) -> Observation: ...

class Actuable(Protocol):
    def send_action(self, action: RobotAction, *, goal_time: float = ...) -> None: ...

class Resettable(Protocol):
    def reset(self, *, seed: int | None = None) -> Observation: ...

class WorldModel(Protocol):
    def rollout(
        self,
        obs: Observation,
        actions: Sequence[RobotAction],
    ) -> PredictedTrajectory: ...
```

Capability composition:

| Substrate         | Observable | Actuable | Resettable | WorldModel | Real-time |
| ----------------- | :--------: | :------: | :--------: | :--------: | :-------: |
| `Robot` (Doc A)   |     ✓      |    ✓     |            |            |    yes    |
| Simulator         |     ✓      |    ✓     |     ✓      |            |    no     |
| World model       |            |          |            |     ✓      |    no     |

The rules that keep this clean:

- **`RobotRuntime` depends only on `Observable + Actuable`** — exactly today's `Robot`. The
  reactive loop never sees `Resettable` or `WorldModel`. Sim and world model are consumed by
  the **Agent**, not the runtime.
- **`WorldModel.rollout()` is pure and side-effect free.** It takes an initial observation
  and a candidate action (or skill) sequence and returns a predicted trajectory plus a score.
  It never actuates anything. This is what makes "imagine before you act" safe to express.
- `Robot` is unchanged. A simulator is a drop-in `Observable + Actuable + Resettable` and can
  back the exact same `RobotRuntime` for offline rollout or CI.

`PredictedTrajectory` carries predicted observations, optional per-step scores, and a
terminal outcome estimate. Because a world model such as Qwen-RobotWorld is a *plausibility
engine, not a contact-accurate simulator*, the Agent treats rollout scores as a **ranking
signal for coarse plans**, then verifies the committed plan on hardware or sim (§5).

---

## 4. Goal and Skill: The Seam Between the Loops

This is the contract the old reactive-only design was missing. It is the robotics analogue of
a long-running action: a goal is requested, an execution runs, and it eventually succeeds,
fails, or is aborted — with feedback along the way.

### 4.1 Goal

A `Goal` is a typed, declarative intent with an explicit notion of success.

```python
@dataclass(frozen=True)
class Goal:
    kind: str                      # "navigate_to" | "pick" | "place" | ...
    params: Mapping[str, Any]      # {"object": "mug"} or {"x": 1.2, "y": 0.3, "heading": 0.0}
    success: SuccessCriterion      # predicate over WorldView / Observation
    deadline_s: float | None = None
    parent: GoalId | None = None   # for hierarchical decomposition
```

Goals form a tree: the planner decomposes a top-level language goal into child goals, each
satisfiable by a skill.

### 4.2 Skill

A `Skill` is a reusable capability that pursues one kind of `Goal`. Crucially, **a skill is
executed by producing a `Controller`** — the same `Controller` protocol `RobotRuntime`
already runs (Doc A §7).

```python
class Skill(Protocol):
    name: str
    goal_kinds: frozenset[str]                 # which Goal.kind values it handles

    def can_run(self, goal: Goal, world: WorldView) -> bool:
        """Precondition check against current fused state."""

    def controller(self, goal: Goal) -> Controller:
        """Return the Controller that RobotRuntime will run to pursue this goal."""

    def status(self) -> SkillStatus:
        """RUNNING | SUCCEEDED | FAILED | ABORTED — polled by the Agent."""

    def feedback(self) -> Mapping[str, Any]:
        """Structured progress for the planner (distance-to-goal, grasp confidence, ...)."""
```

Examples of the `Skill → Controller` mapping:

| Skill            | Goal.kind      | Controller it installs                                     |
| ---------------- | -------------- | ---------------------------------------------------------- |
| `NavigateSkill`  | `navigate_to`  | waypoint-tracking `Controller` (consumes nav waypoints)    |
| `PickSkill`      | `pick`         | `PolicyController(model=manip_vla, execution=...)`         |
| `PlaceSkill`     | `place`        | `PolicyController(...)` with a place-conditioned prompt    |
| `TeleopSkill`    | `teleop`       | `TeleopController`                                         |
| `HumanoidSkill`  | `whole_body`   | `CompositeController` (§7)                                  |

A skill may wrap Doc A's `PolicyController`, `InferenceExecution`, and `ActionQueue` verbatim.
The agentic layer does **not** reinvent inference plumbing; it schedules it.

### 4.3 Skill lifecycle

```text
requested ──▶ can_run? ──no──▶ rejected (planner picks another skill)
                 │yes
                 ▼
           controller(goal)
                 │
   RobotRuntime.swap_controller(controller)     ◀── the only execution mechanism
                 │
              RUNNING ──▶ SUCCEEDED   (success criterion met)
                 │    ├──▶ FAILED      (unrecoverable; agent replans)
                 │    └──▶ ABORTED     (agent preempts with a new goal)
                 ▼
           feedback() streamed to the planner throughout
```

### 4.4 SkillRegistry — robot capabilities as callable tools

```python
class SkillRegistry:
    def register(self, skill: Skill) -> None: ...
    def resolve(self, goal: Goal, world: WorldView) -> Skill | None: ...
    def as_tools(self) -> list[ToolSpec]:
        """Expose skills as tool schemas for an LLM/VLM agent to call."""
```

`as_tools()` is what lets a general agent (e.g. a Qwen planner) call `navigate_to`, `pick`,
and `place` as tools. The registry is the boundary between "the agent's tool vocabulary" and
"the runtime's executable controllers".

---

## 5. The Agent Loop

The Agent is the deliberative process. It is **not** on a fixed rate and it **never blocks
the control tick** — it runs in its own thread/process and communicates with `RobotRuntime`
only through `swap_controller()` and by reading runtime telemetry.

```python
class Agent:
    def __init__(
        self,
        planner: Planner,              # LLM/VLM: Goal + WorldView -> Plan (skill tree)
        registry: SkillRegistry,
        runtime: RobotRuntime,
        world_view: WorldView,         # fused perception/state (see §7)
        world_model: WorldModel | None = None,   # optional verification
    ) -> None: ...

    def pursue(self, goal: Goal) -> GoalResult:
        while not goal.success.met(self.world_view):
            plan = self.planner.plan(goal, self.world_view)      # decompose

            if self.world_model is not None:
                plan = self._imagine_and_rank(plan)              # verify (§5.1)

            skill = self.registry.resolve(plan.next_goal, self.world_view)
            if skill is None or not skill.can_run(plan.next_goal, self.world_view):
                if not self.planner.recover(plan, self.world_view):
                    return GoalResult.FAILED
                continue

            self.runtime.swap_controller(skill.controller(plan.next_goal))   # dispatch
            outcome = self._monitor(skill, plan.next_goal)                   # supervise

            if outcome is SkillStatus.FAILED:
                self.planner.observe_failure(skill, plan, self.world_view)   # replan next iter
        return GoalResult.SUCCEEDED
```

### 5.1 Imagine and verify (world-model in the loop)

```python
def _imagine_and_rank(self, plan: Plan) -> Plan:
    obs = self.runtime.robot.get_observation()
    scored = []
    for candidate in plan.candidates:                 # a few coarse alternatives
        actions = self._sketch_actions(candidate)     # skill-level action sketch
        traj = self.world_model.rollout(obs, actions) # pure, no hardware
        scored.append((candidate, traj.score))
    return plan.with_ranking(scored)                  # commit the best; keep the rest
```

This is the Qwen "imagine then act" pattern: roll a handful of coarse plans forward in the
world model, rank, commit the best, and **verify on hardware/sim during execution** rather
than trusting predicted pixels for contact-rich moments.

### 5.2 Monitoring and preemption

`_monitor` polls `skill.status()` and `skill.feedback()` and watches `RobotRuntime`
telemetry (the `TickEvent` / `LifecycleEvent` stream from Doc A). On success it returns; on
failure it hands control back to the planner; on a higher-priority interrupt (new user goal,
safety event) it preempts by swapping in a new controller. Preemption is just another
`swap_controller()` — the runtime already supports thread-safe controller swap (Doc A §9).

---

## 6. Worked Example: A Qwen-RobotSuite-Style Pipeline

Target: a general agent that navigates, manipulates, and uses a world model to plan — three
specialized models composed behind one language interface.

```python
# --- substrates -------------------------------------------------------------
robot       = G1Robot(...)                         # Observable + Actuable
world_model = RobotWorldModel.load("./exports/robot_world")   # WorldModel (rollout only)

# --- skills (each wraps a policy as a Controller) ---------------------------
registry = SkillRegistry()
registry.register(NavigateSkill(                   # RobotNav → waypoints → tracking Controller
    policy=InferenceModel.load("./exports/robot_nav"),
))
registry.register(PickSkill(                       # RobotManip → PolicyController
    policy=InferenceModel.load("./exports/robot_manip"),
    execution=AsyncInferenceExecution(transport="process"),
))
registry.register(PlaceSkill(policy=InferenceModel.load("./exports/robot_manip")))

# --- one reactive runtime ---------------------------------------------------
runtime = RobotRuntime(
    robot=robot,
    controller=IdleController(),                   # replaced by the agent per skill
    fps=30,
    safety=G1SafetyLayer(),
    callbacks=[RecordingCallback(...), TelemetryCallback(...)],
)

# --- one deliberative agent -------------------------------------------------
agent = Agent(
    planner=QwenPlanner(tools=registry.as_tools()),   # LLM/VLM, robot skills as tools
    registry=registry,
    runtime=runtime,
    world_view=WorldView(...),                        # fused perception/state
    world_model=world_model,                          # imagine + verify
)

with runtime:                                         # runtime loop on its own thread
    agent.pursue(Goal(
        kind="task",
        params={"instruction": "find the green umbrella at the cafe and bring it to me"},
        success=InstructionSatisfied(),
    ))
```

What happens:

1. `QwenPlanner` decomposes the instruction into child goals: `navigate_to(cafe)` →
   `find(umbrella)` → `pick(umbrella)` → `navigate_to(user)` → `place(...)`.
2. For branch points ("which approach to the umbrella?"), the agent rolls a few candidates
   through `RobotWorldModel.rollout()` and commits the best.
3. Each committed child goal resolves to a `Skill`, whose `Controller` is swapped into the
   single `RobotRuntime`. RobotNav drives a waypoint-tracking controller; RobotManip drives a
   `PolicyController`.
4. The runtime records, applies safety, and streams telemetry the whole time — the same
   machinery used for a plain policy rollout.
5. If `PickSkill` reports `FAILED`, the planner observes the failure and replans (new
   approach, or re-navigate) without ever stopping the runtime loop.

`RobotRuntime` did not change. Studio's `RobotControlWorker` did not change. All agentic
complexity lives in `Agent`, `Skill`, and the `WorldModel` — above the seam.

---

## 7. CompositeController: Multi-Rate Effector Arbitration

Some robots (humanoids, mobile manipulators) need several action-producing subsystems running
**concurrently at different rates** and fused into one command per tick — VLA arms at ~20 Hz,
locomotion at ~100–500 Hz, gaze at ~10 Hz. This is not a separate runtime and not the top of
the hierarchy. It is **one `Controller` implementation**, `CompositeController`, that a
`HumanoidSkill` installs like any other.

```python
class CompositeController:                # implements Doc A's Controller protocol
    def __init__(
        self,
        action_subsystems: Sequence[ActionSubsystem],   # produce effector-scoped fragments
        info_subsystems: Sequence[InfoSubsystem] = (),   # perception/world/planner feeds
        arbiter: ActionArbiter = DefaultActionArbiter(),
        scheduler: SubsystemScheduler = CooperativeScheduler(),
    ) -> None: ...

    def update(self, observation: Observation) -> RobotAction:
        self._state.publish("observation", observation)
        self._scheduler.tick(now())                       # runs 0..N subsystems this tick
        fragments = self._state.read_action_fragments()
        return self._arbiter.merge(fragments, observation, now())
```

Internally it uses three mechanisms — all **implementation details of this one controller**,
not top-level architecture:

- **Effector-scoped fragments.** Subsystems emit namespaced commands
  (`{"left_arm": {...}, "base": {"twist": ...}}`) per
  [`../robot-interface.md`](../robot-interface.md#namespaced-action-examples).
- **Arbiter.** Merges fragments by priority + freshness + leases (single-writer effectors
  like `base`), applying a per-effector degrade policy (`hold` / `safe` / `omit`) when a
  fragment is stale or missing.
- **Shared state + scheduler.** A typed, timestamped, in-process state cache (single writer
  per key, latest-value-only) plus a cooperative scheduler that runs each subsystem at its
  declared rate, with `execution="thread" | "process"` escape hatches for slow subsystems
  (heavy VLA, world-model updates) so the control tick never blocks.

```text
CompositeController.update() tick:
  publish observation
  scheduler.tick(now)                      # perception, planner, vla, locomotion, gaze
  fragments = read latest per subsystem
  return arbiter.merge(fragments)          # priority + freshness + leases + degrade
```

An `ActionSubsystem` for arms may itself wrap a `PolicyController` + `InferenceExecution` +
`ActionQueue`. Composite autonomy reuses the inference stack; it does not duplicate it.

> The shared-state cache here is deliberately small and private. It is a mechanism for
> fusing concurrent subsystems inside one controller — **not** a system-wide message bus and
> **not** the interface between the Agent and the runtime. That interface is `Goal` / `Skill`
> (§4). History and replay belong in the recording callback (Doc A §6), not this cache.

### Subsystem protocols

```python
class RuntimeSubsystem(Protocol):
    name: str
    rate_hz: float | None                      # None = event-driven
    def start(self, ctx: SubsystemContext) -> None: ...
    def stop(self) -> None: ...
    def health(self) -> SubsystemHealth: ...

class InfoSubsystem(RuntimeSubsystem, Protocol):
    def step(self, ctx: SubsystemContext) -> None: ...          # writes state, no action

class ActionSubsystem(RuntimeSubsystem, Protocol):
    effectors: frozenset[str]
    priority: int
    def step(self, ctx: SubsystemContext) -> ActionFragment: ...  # effector-scoped command
```

Failure handling is local: a subsystem that raises or stalls is marked `DEGRADED` / `FAILED`
and the arbiter applies that effector's degrade policy. The scheduler **never** kills the
control loop — loop termination is `RobotRuntime`'s job (Doc A §8).

---

## 8. Execution and Scaling

The layer starts single-process and grows outward without changing the contracts:

| Stage | Agent | World model | Skills / subsystems | State sharing |
| ----- | ----- | ----------- | ------------------- | ------------- |
| 1 (initial) | in-process thread | in-process | in-process controllers | in-process cache |
| 2 | in-process thread | GPU worker process | `execution="process"` per heavy skill | in-process cache + IPC to workers |
| 3 (future) | separate process/host | dedicated service | distributed subsystems | cross-process store (deferred) |

The contract that survives every stage: the Agent talks to `RobotRuntime` only through
`Goal`/`Skill` + `swap_controller()`, and every substrate is `Observable`/`Actuable`/
`WorldModel`. A 20B world model on its own GPU, a 4B VLA in a worker process, and locomotion
behind a ROS 2 adapter can all coexist without the reactive loop learning about any of it.

Cross-process / cross-host state sharing is deferred until a concrete integration needs it
(§11). Starting in-process is a deliberate simplification, not a ceiling.

---

## 9. Safety Across Layers

Safety is enforced at the **reactive** layer, always, regardless of what the agent decided:

```text
controller.update() → callbacks.before_send_action → SafetyLayer.filter → robot.send_action
```

- The **Agent** resolves *intent* (which skill, which plan). It is not a safety authority.
- The **arbiter** (inside `CompositeController`) resolves *conflicts* between subsystems. It
  is not a safety authority.
- **`SafetyLayer`** (Doc A) enforces *hard constraints* as the last gate before actuation. A
  `SafetyViolationError` stops the loop no matter which controller produced the action.

This layering means a mis-planned goal or a mis-arbitrated fragment still cannot drive the
robot outside its safety envelope. World-model verification (§5.1) is an *additional* upstream
filter, never a replacement for `SafetyLayer`.

---

## 10. Prior Art

What to borrow, what to avoid.

| System | Steal | Avoid |
| ------ | ----- | ----- |
| **Hierarchical VLA (System 2 / System 1)** | deliberative planner over reactive policy; language goals as the high-level interface | assuming one model does both jobs well |
| **ROS 2 actions / services** | long-running goal lifecycle (propose → feedback → result); the `Goal`/`Skill` contract | mandatory `rclpy` dependency; ROS message types in core |
| **Behavior trees (py_trees)** | tick/status model, skill-level arbitration | forcing every skill to be a BT node |
| **Classic 3T / three-layer** | deliberator / sequencer / controller separation | rigid layer boundaries that forbid reactive shortcuts |
| **Boston Dynamics SDK** | leases (single-writer effectors), e-stop, command authority | proprietary robot-specific assumptions |
| **World-model planners (Dreamer-style, RobotWorld)** | imagine-and-rank before acting; world model as a ranking signal | closing the loop on predicted pixels for contact-rich control |
| **LeRobot** | robot/policy/dataset ergonomics, naming | using it as an autonomy architecture (it isn't one) |

**ROS 2 verdict:** optional integration boundary, not the core runtime. Complex subsystems
(vendor locomotion) enter as a `RuntimeSubsystem` adapter inside a `CompositeController`; the
autonomy graph stays PhysicalAI-native.

---

## 11. What This Doc Does NOT Define

Deferred until a concrete need arises:

- **Planner internals.** The `Planner` (LLM/VLM prompting, decomposition, recovery policy) is
  a pluggable strategy, not fixed here.
- **World-model training or interface details** beyond the `rollout()` contract.
- **Typed `RobotAction` / `Effector` class hierarchy.** Use namespaced mappings
  ([`../robot-interface.md`](../robot-interface.md)).
- **Cross-process / cross-host state store.** Start in-process (§8).
- **A separate `AutonomyRuntime`.** Not introduced — one `RobotRuntime` loop with
  controller-swap is sufficient. A separate runtime becomes warranted only for
  PhysicalAI-owned multi-process subsystem lifecycles, a native ROS 2 graph that owns the
  loop, or hard real-time scheduling in a non-Python control process. If that day comes,
  `Skill`/`Controller` and `Goal` stay stable; only the executor beneath them changes.
- **Composite manifest schema** beyond the per-effector sketch in Doc A's ecosystem.
- **Real-time scheduling guarantees** from the Python scheduler. Time-critical control lives
  in the `Robot` driver / vendor stack.

---

## 12. Decision Summary

```text
architecture                       dual-process: deliberative Agent (System 2) over reactive RobotRuntime (System 1)
the seam                           Goal / Skill — long-running, with status + feedback (robot skills as callable tools)
skill execution                    a Skill produces a Controller; Agent runs it via RobotRuntime.swap_controller()
no new outer runtime               RobotRuntime stays the only control loop
environment                        capability protocols: Observable / Actuable / Resettable / WorldModel
real vs sim vs world model         same Observation/Action substrate; runtime needs only Observable+Actuable
verification                       WorldModel.rollout() is pure; Agent imagines + ranks, verifies on hardware/sim
multi-rate composite               CompositeController is ONE Controller (arbiter + effectors + scheduler inside)
shared state                       private, in-process cache inside CompositeController — a mechanism, not the architecture
safety                             enforced by SafetyLayer at the reactive layer, last gate before actuation
scaling                            in-process → worker process → distributed, contracts unchanged
implementation trigger             defer until a concrete agentic / composite-robot integration is in scope
```

This layer turns PhysicalAI from "run one policy on one robot" into "pursue a language goal
by planning, imagining, and dispatching skills" — while keeping Doc A's small, predictable
control loop completely intact. The agent plans; the world model imagines; skills execute as
controllers; `RobotRuntime` runs the loop; `SafetyLayer` has the final say.
