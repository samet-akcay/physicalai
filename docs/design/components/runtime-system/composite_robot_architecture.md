# Agentic Autonomy Layer (System 2)

This document defines the **agentic autonomy layer** for PhysicalAI: the deliberative
system that turns a language goal into grounded robot behavior by perceiving, planning,
optionally imagining outcomes in a world model, and dispatching skills to the reactive
runtime.

It builds on the single-rate reactive runtime introduced in PR #179
([`runtime-architecture.md`](./runtime-architecture.md), "Doc A"): one `RobotRuntime`
loop driving one `ActionSource` at a fixed rate. This document does **not** replace that
loop. The whole autonomy stack executes through it; this layer decides *what the loop
should be doing right now*.

It is intentionally future-facing. Nothing here should be implemented before a concrete
agentic integration (e.g. an LLM/VLM planner + VLA skills + world model, or a humanoid
platform) is in scope. The purpose of writing it now is to ensure Doc A's contracts
extend cleanly to full autonomy without rework — and to name the small amendments Doc A
needs (§2.1) while its API is still young.

---

## Quick Navigation

- [1. The Dual-Process Architecture](#1-the-dual-process-architecture)
- [2. Scope, Non-Goals, and Doc A Prerequisites](#2-scope-non-goals-and-doc-a-prerequisites)
- [3. Environment: One Substrate for Real, Sim, and World Model](#3-environment-one-substrate-for-real-sim-and-world-model)
- [4. WorldView: Perception and Fused State](#4-worldview-perception-and-fused-state)
- [5. Goal, Skill, and SkillExecution: The Seam Between the Loops](#5-goal-skill-and-skillexecution-the-seam-between-the-loops)
- [6. The Agent: An Event-Driven Tool Loop](#6-the-agent-an-event-driven-tool-loop)
- [7. The Data Flywheel](#7-the-data-flywheel)
- [8. CompositeSource: Multi-Rate Effector Arbitration](#8-compositesource-multi-rate-effector-arbitration)
- [9. Execution and Scaling](#9-execution-and-scaling)
- [10. Safety Across Layers](#10-safety-across-layers)
- [11. Prior Art](#11-prior-art)
- [12. What This Doc Does NOT Define](#12-what-this-doc-does-not-define)
- [13. Implementation Sequencing](#13-implementation-sequencing)
- [14. Decision Summary](#14-decision-summary)

---

## 1. The Dual-Process Architecture

Modern robot autonomy is a **two-loop hierarchy**. A slow deliberative process reasons
about goals; a fast reactive process controls the hardware. This is the "System 2 /
System 1" split used by hierarchical VLA stacks (Helix, π0-style planner+policy,
Gemini Robotics ER + VLA), and the same shape as the classic three-layer
deliberator / sequencer / controller architectures.

```text
┌──────────────────────────────────────────────────────────────────────┐
│  AGENT   —  System 2  ·  deliberative  ·  event-driven  ·  async     │
│                                                                      │
│    goals & interrupts in → LLM/VLM tool loop over skills             │
│    reads:  WorldView (fused state, §4)                               │
│    uses:   WorldModel.rollout() to imagine/rank candidate plans      │
│    emits:  skill dispatches; receives SkillExecution handles (§5)    │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  dispatch(goal) → SkillExecution
                                │  (status · feedback · cancel · result)
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  RobotRuntime   —  System 1  ·  reactive  ·  fixed FPS  ·  sync      │
│                                                                      │
│    runs the ActionSource that the current skill installed            │
│    read obs → source.update() → callbacks → safety → send_action     │
│                                                                      │
│    Doc A, with the §2.1 amendments. Otherwise UNCHANGED.             │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼
        Environment (Robot │ Sim)            WorldModel (predictive only)
```

The single most important design decision:

```text
A Skill is executed by producing an ActionSource.
The Agent runs it through the one RobotRuntime loop, and supervises it
through a SkillExecution handle.
```

There is exactly one outer control loop. The Agent never touches hardware and never
runs a second control loop. It selects *which* `ActionSource` the one `RobotRuntime`
runs next, and observes progress through the runtime's event stream. Everything Doc A
provides — fixed-rate timing, resilient IO, callbacks, telemetry — applies unchanged to
fully autonomous behavior.

### Why the seam matters

A pure reactive design has no vocabulary for **long-running, goal-directed work with
feedback**: "navigate to the umbrella", "pick the mug", "if the grasp fails, retry from
a new approach". That vocabulary is the `Goal` / `Skill` / `SkillExecution` contract
(§5). It is the robotics equivalent of a long-running action (dispatch → feedback →
result), and it is what lets a general agent treat robot capabilities as **callable
tools** (§6).

---

## 2. Scope, Non-Goals, and Doc A Prerequisites

### Scope

A reusable deliberative layer for robots whose behavior is driven by an agent composing
multiple decision-making systems:

- A **high-level agent** (LLM or VLM) that decomposes language goals into skill calls.
- A **perception/state layer** (`WorldView`, §4) the agent and success checks read.
- One or more **policies** (VLA manipulation, navigation) invoked as skills.
- An optional **world model** used to imagine and rank candidate plans before spending
  real robot time.
- Optional **multi-rate subsystems** (locomotion + arms + gaze) composed into a single
  tick for humanoids and mobile manipulators (§8).

The layer must:

1. Turn a `Goal` into robot behavior through skills executed by `RobotRuntime`.
2. Give the agent a supervision handle per execution: status, feedback, cancel, result.
3. Run deliberation off the control thread; never stall the reactive tick.
4. Accept goals and interrupts at any time — including mid-execution — and preempt.
5. Treat real robot, simulator, and world model as the **same observation/action
   substrate** with different capabilities (§3).
6. Record every skill execution as a labeled episode for training (§7).
7. Stay framework-light: no mandatory ROS 2, no mandatory behavior-tree dependency.

### Non-Goals

- Replacing `RobotRuntime`. The loop, lifecycle, resilience, and callbacks stay in Doc A.
- Replacing low-level control (joint servoing, gait MPC, balance). Those live in the
  `Robot` driver or vendor stack.
- Defining inference plumbing. That is `PolicySource` + `Execution` + `ActionQueue`
  (Doc A). Skills reuse it verbatim.
- Building a planner framework. The agent is an LLM tool loop (§6); we do not define
  `Plan` trees, recovery strategies, or prompt schemas here.
- Becoming a distributed actor framework. The initial layer is single-process with
  per-subsystem worker escape hatches (§9).
- Training the planner, world model, or policies. That is the Studio/training layer.

### 2.1 Prerequisites: small amendments to Doc A

This layer requires four additions to the Doc A runtime. All are small, all are
independently useful for non-agentic use, and all should land while the Doc A API is
still breaking:

| # | Amendment | Why System 2 needs it |
| - | --------- | --------------------- |
| 1 | `RobotRuntime.stop()` — thread-safe request to end the current `run()` at the next tick boundary | Preemption. The agent must be able to abort a running skill (new goal, safety event, teleop takeover). |
| 2 | `action_source` accepted as a `run()` parameter (robot/cameras stay connected across runs) | Skill dispatch. Each skill execution is one `run()` with that skill's source; the runtime object is long-lived, the source is per-execution. |
| 3 | A sanctioned completion signal from the source (e.g. a `SourceFinished` control-flow exception, or a `finished` flag the loop checks) | A skill that achieves its goal must be able to end the run without the agent having to infer completion externally. |
| 4 | A reserved **final safety-gate slot** between the callback chain and `send_action` | §10. Safety must be the guaranteed-last transform, not "a callback that hopes it runs last". |

Additionally, the runtime's event stream (`TickEvent` / `LifecycleEvent` /
`MetricsEvent`) becomes the **official observation channel** for everything above the
loop: skill status derivation (§5.4), agent monitoring (§6), and recording (§7) all
subscribe to it rather than reaching into the loop.

True mid-run source hot-swap is **not** required initially. Sequential per-skill
`run()` calls, with the robot holding pose between runs, are sufficient for a v1 agent;
seamless swap becomes worthwhile only when skill-to-skill transitions must be gap-free
(e.g. locomotion handoffs), and can be added later without changing this document's
contracts (§6.2).

---

## 3. Environment: One Substrate for Real, Sim, and World Model

An agentic pipeline runs the same behavior **on hardware or in sim**, and may use a
**world model as a neural simulator** to rank plans. These are not three unrelated
interfaces — they are the same observation/action types with different *capabilities*.
We model this with small structural protocols (matching the existing `Robot` Protocol
style), not a single god-interface.

```python
class Observable(Protocol):
    def get_observation(self) -> RobotObservation: ...

class Actuable(Protocol):
    def send_action(self, action: np.ndarray, *, goal_time: float = ...) -> None: ...

class Resettable(Protocol):
    def reset(self, *, seed: int | None = None) -> RobotObservation: ...

class WorldModel(Protocol):
    def rollout(
        self,
        obs: RobotObservation,
        actions: Sequence[np.ndarray],
    ) -> PredictedTrajectory: ...
```

Capability composition:

| Substrate         | Observable | Actuable | Resettable | WorldModel | Real-time |
| ----------------- | :--------: | :------: | :--------: | :--------: | :-------: |
| `Robot` (Doc A)   |     ✓      |    ✓     |            |            |    yes    |
| Simulator         |     ✓      |    ✓     |     ✓      |            |    no     |
| World model       |            |          |            |     ✓      |    no     |

The rules that keep this clean:

- **`RobotRuntime` depends only on `Observable + Actuable`** — exactly today's `Robot`.
  The reactive loop never sees `Resettable` or `WorldModel`. Sim and world model are
  consumed by the **Agent**, not the runtime.
- **`WorldModel.rollout()` is pure and side-effect free.** It takes an initial
  observation and a candidate action sequence and returns a predicted trajectory plus a
  score. It never actuates anything. This is what makes "imagine before you act" safe
  to express.
- `Robot` is unchanged. A simulator is a drop-in `Observable + Actuable + Resettable`
  and can back the exact same `RobotRuntime` for offline rollout or CI.

`PredictedTrajectory` carries predicted observations, optional per-step scores, and a
terminal outcome estimate. Because a learned world model is a *plausibility engine, not
a contact-accurate simulator*, the Agent treats rollout scores as a **ranking signal
for coarse plans**, then verifies the committed plan on hardware or sim during
execution.

> **Known future break.** The action type today is a flat `np.ndarray` end to end
> (`ActionSource.update`, callbacks, `Robot.send_action`). Humanoids and mobile
> manipulators will eventually want **namespaced, effector-scoped actions**
> (`{"left_arm": ..., "base": ...}`). §8 contains that inside one composite source for
> now, but if the `Robot` driver itself needs namespaced commands, the substrate type
> widens — a coordinated breaking change across every layer. Nothing new should grow
> dependent on "action is always a flat vector".

---

## 4. WorldView: Perception and Fused State

Everything deliberative reads fused state: the agent's context, `Skill.can_run()`
preconditions, and success checks. That state has a name, an owner, and a defined
freshness model — it is **not** an implicit by-product of the control loop.

```python
class WorldView(Protocol):
    def get(self, key: str) -> StateEntry | None:
        """Latest value for a state key, with timestamp and source."""

    def snapshot(self) -> Mapping[str, StateEntry]:
        """Consistent read of all current state."""

    def subscribe(self, key: str, fn: Callback) -> Subscription: ...
```

```python
@dataclass(frozen=True)
class StateEntry:
    value: Any            # pose, detection list, occupancy patch, scene caption, ...
    timestamp: float      # when it was produced (staleness is the reader's decision)
    source: str           # which perceiver wrote it
```

Design rules:

- **Single writer per key, latest-value-only.** WorldView is a typed state cache, not
  a message bus and not a history store. History and replay belong to recording (§7).
- **Perceivers are their own threads/processes at their own rates.** A detector at
  5 Hz, a scene captioner (VLM) at 0.2 Hz, proprioception forwarded from the runtime's
  `TickEvent` stream at tick rate. None of them run inside the control loop.
- **Readers own staleness policy.** A grasp precondition may require a detection
  < 500 ms old; a navigation goal check may accept seconds-old localization.
  `WorldView` reports age; it does not judge it.
- **Semantic perception is expensive and asynchronous.** "The mug is on the table" may
  come from a VLM call that takes seconds. The architecture must assume WorldView
  entries update at wildly different rates — which is exactly why the agent is
  event-driven (§6) rather than assuming a fresh world snapshot per decision.

### 4.1 Success detection is a perceiver, not a predicate

Deciding "did the pick succeed?" is itself often a model call (a VLM judge, a gripper
force heuristic, a detector re-query) — asynchronous, rate-limited, and noisy. We
therefore model success as a **check with a confidence, evaluated against WorldView**,
not a synchronous boolean:

```python
class SuccessCheck(Protocol):
    def evaluate(self, world: WorldView) -> SuccessEstimate:
        """May be cheap (threshold on a pose) or expensive (VLM judge)."""

@dataclass(frozen=True)
class SuccessEstimate:
    met: bool
    confidence: float          # 0..1; the agent decides what to do with 0.6
    evidence: Mapping[str, Any] = field(default_factory=dict)
```

Cheap checks can run per skill-status poll; expensive checks run at skill boundaries.
The agent — not the runtime, not the skill — decides how much confidence is enough
before declaring a goal met or dispatching a verification behavior (e.g. "look at the
gripper").

---

## 5. Goal, Skill, and SkillExecution: The Seam Between the Loops

This is the contract the reactive layer is missing on its own: the robotics analogue of
a long-running action. A goal is requested, an execution runs with feedback, and it
eventually succeeds, fails, or is cancelled.

### 5.1 Goal

A `Goal` is a typed, declarative intent with an explicit notion of success.

```python
@dataclass(frozen=True)
class Goal:
    kind: str                      # "navigate_to" | "pick" | "place" | ...
    params: Mapping[str, Any]      # {"object": "mug"} or {"x": 1.2, "y": 0.3}
    success: SuccessCheck          # evaluated against WorldView (§4.1)
    deadline_s: float | None = None
    parent: GoalId | None = None   # provenance when the agent decomposes a task
```

Goals form a tree only in the sense of provenance: the agent decomposes a top-level
language task into child goals, each satisfiable by one skill call. The tree is the
agent's context, not a persisted plan structure.

### 5.2 Skill — a stateless capability factory

A `Skill` is a reusable capability that pursues one kind of `Goal`. It is
**stateless with respect to executions**: registered once, dispatched many times.
Executing it produces two things — an `ActionSource` for the runtime, and a
`SkillExecution` handle for the agent.

```python
class Skill(Protocol):
    name: str
    goal_kinds: frozenset[str]          # which Goal.kind values it handles
    description: str                    # tool description shown to the LLM agent

    def can_run(self, goal: Goal, world: WorldView) -> bool:
        """Precondition check against current fused state."""

    def start(self, goal: Goal, ctx: SkillContext) -> SkillExecution:
        """Create this execution's ActionSource and its supervision handle."""
```

`SkillContext` gives the skill what it needs to build the execution: the `WorldView`,
the runtime's event-stream subscription point, and shared resources (e.g. a loaded
`InferenceModel` the skill was constructed with).

Examples of the `Skill → ActionSource` mapping:

| Skill            | Goal.kind      | ActionSource it installs                                  |
| ---------------- | -------------- | --------------------------------------------------------- |
| `NavigateSkill`  | `navigate_to`  | waypoint-tracking source (consumes nav waypoints)          |
| `PickSkill`      | `pick`         | `PolicySource(model=manip_vla, execution=...)`             |
| `PlaceSkill`     | `place`        | `PolicySource(...)` with a place-conditioned task prompt   |
| `TeleopSkill`    | `teleop`       | `TeleopSource`                                             |
| `HumanoidSkill`  | `whole_body`   | `CompositeSource` (§8)                                     |

A skill wraps Doc A's `PolicySource`, `Execution`, and `ActionQueue` verbatim. The
agentic layer does **not** reinvent inference plumbing; it schedules it.

### 5.3 SkillExecution — the supervision handle

**All per-execution state lives on the handle, never on the Skill.** This is what makes
sequential re-dispatch and (later) concurrent skills on disjoint effectors coherent.

```python
class SkillExecution(Protocol):
    id: ExecutionId
    goal: Goal

    @property
    def action_source(self) -> ActionSource:
        """What the dispatcher hands to RobotRuntime.run()."""

    def status(self) -> SkillStatus:
        """PENDING | RUNNING | SUCCEEDED | FAILED | CANCELLED."""

    def feedback(self) -> Mapping[str, Any]:
        """Structured progress (distance-to-goal, grasp confidence, queue depth...)."""

    def cancel(self) -> None:
        """Request preemption. Dispatcher stops the runtime run; status → CANCELLED."""

    def result(self) -> SkillResult:
        """Blocks until terminal. Outcome + evidence + recording reference (§7)."""
```

The lifecycle, borrowed deliberately from ROS 2 actions (the pattern, not the
dependency):

```text
skill.start(goal, ctx) ── SkillExecution(PENDING)
        │
Dispatcher: runtime.run(action_source=execution.action_source)   ◀── the only execution mechanism
        │
     RUNNING ──▶ SUCCEEDED   success check met; source signals finished (§2.1 #3)
        │    ├──▶ FAILED      unrecoverable; result() carries the evidence
        │    └──▶ CANCELLED   agent called cancel(); dispatcher called runtime.stop()
        ▼
   feedback() readable throughout; result() blocks until terminal
```

### 5.4 How status actually flows (the thread seam)

The `ActionSource` runs inside the runtime's tick thread; the agent polls the handle
from its own thread. The connection is explicit and one-directional:

```text
runtime tick thread                          agent thread
──────────────────────                       ─────────────────────
source.update() ──▶ TickEvent/MetricsEvent ──▶ SkillExecution subscribes,
                    (runtime event stream)      derives status/feedback
                                                (+ its own SuccessCheck polls
                                                 against WorldView)
```

The handle is a **consumer of the runtime event stream plus WorldView** — it never
reaches into the loop, and the loop never knows the handle exists. Cancellation goes
the other way through one sanctioned door: `cancel()` → dispatcher → `runtime.stop()`
(§2.1 #1). No other cross-thread channel exists.

### 5.5 SkillRegistry — robot capabilities as callable tools

```python
class SkillRegistry:
    def register(self, skill: Skill) -> None: ...
    def resolve(self, goal: Goal, world: WorldView) -> Skill | None: ...
    def as_tools(self) -> list[ToolSpec]:
        """Expose skills as tool schemas for the LLM/VLM agent (§6)."""
```

`as_tools()` derives each tool's schema from the skill's `goal_kinds`, params, and
`description`. The registry is the boundary between "the agent's tool vocabulary" and
"the runtime's executable sources".

---

## 6. The Agent: An Event-Driven Tool Loop

The Agent is deliberately **not** a bespoke planner framework. It is an LLM/VLM agent
loop whose tools are the registered skills plus perception queries — the same shape as
every working tool-using agent stack. Decomposition, recovery, and replanning are what
the model does natively when a tool call returns `FAILED` with feedback; we do not
encode them as `Plan` classes.

```python
class Agent:
    def __init__(
        self,
        llm: AgentModel,                 # tool-calling LLM/VLM
        registry: SkillRegistry,
        dispatcher: SkillDispatcher,     # owns runtime.run()/stop(); one skill at a time
        world_view: WorldView,
        world_model: WorldModel | None = None,
    ) -> None: ...

    def submit(self, goal: Goal, *, priority: Priority = Priority.NORMAL) -> GoalHandle:
        """Enqueue a goal. Non-blocking. Higher priority preempts the active skill."""

    def interrupt(self, event: Interrupt) -> None:
        """Safety event, teleop takeover, user cancellation. Preempts immediately."""
```

Structural rules:

- **Event-driven, not a blocking `pursue()` loop.** Goals and interrupts arrive on a
  queue at any time. A new high-priority goal or interrupt cancels the active
  `SkillExecution` (§5.3) and enters deliberation. Teleop takeover is an interrupt
  that dispatches `TeleopSkill` — first-class, not a special case.
- **The tool loop is the planner.** Each deliberation step: the agent model sees the
  task, a recent WorldView snapshot, and the transcript of prior skill results; it
  calls a skill tool; the tool call blocks (from the model's perspective) on
  `execution.result()`; the result — outcome, evidence, feedback — returns as the tool
  result. Failure handling *is* the next model step.
- **Perception queries are tools too.** `locate(object)`, `describe_scene()` — cheap
  reads answered from WorldView, expensive ones dispatched as perceiver work. This
  keeps "check before you act" inside the same tool vocabulary.
- **The agent maintains task context.** What was tried, what failed and why, what the
  scene looked like — this is the agent transcript plus WorldView snapshots at skill
  boundaries. It is context for the model, not a persisted data structure.
- **Deliberation never blocks the tick.** While the model thinks (seconds), the
  runtime either runs the still-active skill or an idle/hold source. The dispatcher
  guarantees the robot is always under *some* source's control or safely holding.

### 6.1 Imagine and verify (world model in the loop)

When a `WorldModel` is available, the agent uses it as one more tool:

```python
# exposed to the model as e.g. imagine(candidate_skill_calls) -> ranked outcomes
def imagine(self, candidates: Sequence[SkillCall]) -> Sequence[ScoredOutcome]:
    obs = self.world_view.snapshot_observation()
    return [
        ScoredOutcome(c, self.world_model.rollout(obs, self._sketch_actions(c)).score)
        for c in candidates
    ]
```

This is the "imagine then act" pattern: roll a handful of coarse alternatives forward,
rank, commit the best, and **verify during execution on hardware/sim** (via
`SuccessCheck` and feedback) rather than trusting predicted pixels for contact-rich
moments. The world model ranks; it never authorizes — §10.

### 6.2 The dispatcher

`SkillDispatcher` is the one component that touches the runtime. It serializes skill
executions onto the single loop:

```python
class SkillDispatcher:
    def dispatch(self, execution: SkillExecution) -> None:
        """runtime.run(action_source=execution.action_source) on the runtime thread."""

    def preempt(self) -> None:
        """runtime.stop(); active execution → CANCELLED; robot holds."""
```

One runtime, one active execution, one place that calls `run()`/`stop()`. When
gap-free skill transitions become necessary, hot-swap capability lands here without
touching `Agent`, `Skill`, or the handles.

---

## 7. The Data Flywheel

Recording is a **requirement of this layer, not a callback afterthought**. The
strategic reason to run autonomy inside a training-centric stack is that every
execution produces training data.

- Every `SkillExecution` is recorded as **one labeled episode**: the Doc A recording
  stream (observations, actions, timing) plus this layer's labels — `goal.kind`,
  `goal.params`, skill name, terminal status, `SuccessEstimate` evidence, and the
  agent's dispatch context.
- `SkillResult` carries a **reference to its recording**, so the agent transcript
  links every decision to its data.
- Failures are as valuable as successes: `FAILED` and `CANCELLED` episodes are
  recorded with the same fidelity and labeled with the failure evidence.
- The episode boundary **is** the execution boundary. No separate segmentation pass;
  the seam that structures control also structures the dataset.

This closes the loop back into Studio: autonomous operation → labeled episodes →
policy improvement → better skills — without any additional instrumentation.

---

## 8. CompositeSource: Multi-Rate Effector Arbitration

Some robots (humanoids, mobile manipulators) need several action-producing subsystems
running **concurrently at different rates**, fused into one command per tick — VLA arms
at ~20 Hz, locomotion at ~100–500 Hz, gaze at ~10 Hz. This is not a separate runtime
and not the top of the hierarchy. It is **one `ActionSource` implementation** that a
`HumanoidSkill` installs like any other.

```python
class CompositeSource:                    # implements Doc A's ActionSource protocol
    def __init__(
        self,
        action_subsystems: Sequence[ActionSubsystem],   # effector-scoped fragments
        info_subsystems: Sequence[InfoSubsystem] = (),   # perception/planner feeds
        arbiter: ActionArbiter = DefaultActionArbiter(),
        scheduler: SubsystemScheduler = CooperativeScheduler(),
    ) -> None: ...

    def update(self, robot_state, camera_frames, step) -> np.ndarray:
        self._state.publish("observation", (robot_state, camera_frames))
        self._scheduler.tick(now())                     # runs 0..N subsystems this tick
        fragments = self._state.read_action_fragments()
        return self._arbiter.merge(fragments, robot_state, now())
```

Internally it uses three mechanisms — all **implementation details of this one source**,
not top-level architecture:

- **Effector-scoped fragments.** Subsystems emit namespaced commands
  (`{"left_arm": {...}, "base": {"twist": ...}}`); the arbiter flattens the merged
  result to the robot's action vector at the seam (see the §3 note on the future
  namespaced-action break).
- **Arbiter.** Merges fragments by priority + freshness + leases (single-writer
  effectors like `base`), applying a per-effector degrade policy
  (`hold` / `safe` / `omit`) when a fragment is stale or missing.
- **Scheduler + private state cache.** A cooperative scheduler runs each subsystem at
  its declared rate, with `execution="thread" | "process"` escape hatches for slow
  subsystems (heavy VLA, world-model updates) so the control tick never blocks. The
  state cache is small and private — a fusion mechanism inside one source, **not** a
  system-wide bus and **not** the Agent↔runtime interface (that is §5).

```python
class RuntimeSubsystem(Protocol):
    name: str
    rate_hz: float | None                      # None = event-driven
    def start(self, ctx: SubsystemContext) -> None: ...
    def stop(self) -> None: ...
    def health(self) -> SubsystemHealth: ...

class InfoSubsystem(RuntimeSubsystem, Protocol):
    def step(self, ctx: SubsystemContext) -> None: ...            # writes state only

class ActionSubsystem(RuntimeSubsystem, Protocol):
    effectors: frozenset[str]
    priority: int
    def step(self, ctx: SubsystemContext) -> ActionFragment: ...  # effector-scoped
```

An `ActionSubsystem` for arms may itself wrap a `PolicySource` + `Execution` +
`ActionQueue`. Composite autonomy reuses the inference stack; it does not duplicate it.
Failure handling is local: a subsystem that raises or stalls is marked
`DEGRADED`/`FAILED` and the arbiter applies that effector's degrade policy. The
scheduler never kills the control loop — loop termination is `RobotRuntime`'s job.

---

## 9. Execution and Scaling

The layer starts single-process and grows outward without changing the contracts:

| Stage | Agent | Perceivers / WorldView | World model | Skills / subsystems |
| ----- | ----- | ---------------------- | ----------- | ------------------- |
| 1 (initial) | in-process thread | in-process threads | in-process | in-process sources |
| 2 | in-process thread | worker processes | GPU worker process | `execution="process"` per heavy skill |
| 3 (future) | separate process/host | dedicated services | dedicated service | distributed subsystems |

The contracts that survive every stage: the Agent talks to the runtime only through
`SkillDispatcher` (`run()`/`stop()`), supervises only through `SkillExecution` handles
and the event stream, and every substrate is `Observable`/`Actuable`/`WorldModel`.
A 20B world model on its own GPU, a 4B VLA in a worker process, and vendor locomotion
behind an adapter can all coexist without the reactive loop learning about any of it.

Cross-process/cross-host state sharing is deferred until a concrete integration needs
it. Starting in-process is a deliberate simplification, not a ceiling.

---

## 10. Safety Across Layers

Safety is enforced at the **reactive** layer, always, regardless of what the agent
decided:

```text
source.update() → callback chain (on_action_ready) → SAFETY GATE → robot.send_action
```

- The **Agent** resolves *intent* (which skill, which goal). Not a safety authority.
- The **arbiter** (inside `CompositeSource`) resolves *conflicts* between subsystems.
  Not a safety authority.
- The **safety gate** (Doc A amendment §2.1 #4) enforces *hard constraints* as the
  guaranteed-last transform before actuation — a dedicated slot, not an ordinary
  callback whose position in the chain is a configuration accident. A safety violation
  stops the loop no matter which source produced the action.

This layering means a mis-planned goal, a hallucinated tool call, or a mis-arbitrated
fragment still cannot drive the robot outside its safety envelope. World-model
verification (§6.1) is an *additional* upstream filter, never a replacement for the
gate. Interrupt-driven preemption (§6) adds a second reflex: safety events cancel the
active skill at the agent level *and* the gate constrains actuation at the tick level.

---

## 11. Prior Art

What to borrow, what to avoid.

| System | Steal | Avoid |
| ------ | ----- | ----- |
| **Hierarchical VLA (System 2 / System 1)** | deliberative model over reactive policy; language as the high-level interface | assuming one model does both jobs well |
| **ROS 2 actions** | goal → handle → feedback → result lifecycle; the `SkillExecution` pattern | mandatory `rclpy`; ROS message types in core |
| **LLM tool-calling agents** | skills as tool schemas; transcript as task memory; failure-as-tool-result replanning | letting the model reach actuation without the typed Goal/Skill boundary |
| **Behavior trees (py_trees)** | tick/status vocabulary for skill supervision | forcing every skill to be a BT node |
| **Classic 3T / three-layer** | deliberator / sequencer / controller separation | rigid layer boundaries that forbid reactive shortcuts |
| **Boston Dynamics SDK** | leases (single-writer effectors), e-stop, command authority | proprietary robot-specific assumptions |
| **World-model planners (Dreamer-style)** | imagine-and-rank before acting; world model as ranking signal | closing the loop on predicted pixels for contact-rich control |
| **LeRobot** | robot/policy/dataset ergonomics, naming | using it as an autonomy architecture (it isn't one) |

**ROS 2 verdict:** optional integration boundary, not the core runtime. Complex vendor
subsystems (locomotion) enter as a `RuntimeSubsystem` adapter inside `CompositeSource`;
the autonomy graph stays PhysicalAI-native.

---

## 12. What This Doc Does NOT Define

Deferred until a concrete need arises:

- **Agent model internals.** Prompting, context management, and model choice are
  pluggable; only the tool boundary (`SkillRegistry.as_tools()`) is fixed.
- **Perceiver implementations.** WorldView's contract is fixed (§4); which detectors,
  captioners, or SLAM stacks feed it is per-integration.
- **World-model training or interface details** beyond the `rollout()` contract.
- **Typed action / effector class hierarchy.** Namespaced mappings inside
  `CompositeSource`; flat vector at the robot seam until the substrate-wide break (§3).
- **Cross-process / cross-host state store.** Start in-process (§9).
- **A separate `AutonomyRuntime`.** Not introduced — one `RobotRuntime` loop with
  per-execution `run()` (and later hot-swap in the dispatcher) is sufficient. If a
  natively multi-process or hard-real-time executor is ever warranted, `Goal` / `Skill`
  / `SkillExecution` stay stable; only the dispatcher beneath them changes.
- **Concurrent skills on disjoint effectors.** The v1 dispatcher runs one skill at a
  time; the handle model already permits concurrency later without contract changes.
- **Real-time scheduling guarantees** from the Python scheduler. Time-critical control
  lives in the `Robot` driver / vendor stack.

---

## 13. Implementation Sequencing

When an agentic integration comes into scope, build in this order — each step is
independently testable and de-risks the next:

1. **Doc A amendments** (§2.1): `stop()`, per-run `action_source`, completion signal,
   safety-gate slot. Small PRs against the runtime.
2. **Scripted System 2 (no LLM).** A hardcoded skill sequence — e.g.
   `navigate → pick → place` with fixed params — driven through `Skill` /
   `SkillExecution` / `SkillDispatcher` on real hardware. This proves the entire seam
   (dispatch, feedback, cancel, completion, recording) with zero model variance. **Do
   not skip this step.**
3. **WorldView + one cheap perceiver + one `SuccessCheck`.** Enough for real
   preconditions and success detection on the scripted pipeline.
4. **The LLM tool loop.** Replace the script with the agent model over
   `registry.as_tools()`. The seam does not change.
5. **World model as a tool** (§6.1), when a concrete model exists.
6. **`CompositeSource`**, only when a humanoid / mobile manipulator is in scope.

---

## 14. Decision Summary

```text
architecture                dual-process: event-driven Agent (System 2) over reactive RobotRuntime (System 1)
the seam                    Goal / Skill / SkillExecution — dispatch returns a handle (status·feedback·cancel·result)
skill execution             Skill.start(goal) → ActionSource + handle; dispatcher runs it via runtime.run(action_source=...)
preemption                  handle.cancel() → dispatcher → runtime.stop(); interrupts are first-class agent inputs
no new outer runtime        RobotRuntime stays the only control loop; per-execution run(), hot-swap deferred to dispatcher
perception                  WorldView — named, owned, typed state cache; perceivers run at their own rates off-loop
success                     SuccessCheck → SuccessEstimate (confidence + evidence); often a model call, never assumed free
the planner                 an LLM tool loop over SkillRegistry.as_tools() — no bespoke Plan/recover framework
environment                 capability protocols: Observable / Actuable / Resettable / WorldModel
verification                WorldModel.rollout() is pure; imagine-and-rank is a tool; it ranks, never authorizes
data flywheel               every SkillExecution = one labeled episode (goal, outcome, evidence) — a requirement
multi-rate composite        CompositeSource is ONE ActionSource (arbiter + scheduler + private cache inside)
safety                      dedicated final gate before actuation (Doc A amendment); agent and arbiter are not authorities
scaling                     in-process → worker processes → distributed; contracts unchanged
sequencing                  Doc A amendments → scripted agent → WorldView → LLM tool loop → world model → composite
implementation trigger      defer until a concrete agentic / composite-robot integration is in scope
```

This layer turns PhysicalAI from "run one policy on one robot" into "pursue a language
goal by perceiving, deliberating, and dispatching skills" — while keeping Doc A's small,
predictable control loop completely intact, and turning every autonomous minute into
labeled training data. The agent deliberates; skills execute as action sources; handles
supervise; `RobotRuntime` runs the loop; the safety gate has the final say.
