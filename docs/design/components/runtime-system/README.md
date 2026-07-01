# Runtime System Design

This directory describes how PhysicalAI runs a robot: the production single-rate runtime that drives today's policy / teleop / HIL / DAgger / recording workflows, and the deliberative agentic layer that turns language goals into grounded behavior for humanoids, mobile manipulators, and world-model-driven pipelines.

```text
Agent                System 2: plan (LLM/VLM) → imagine (WorldModel) → verify → dispatch Skill
Skill / Goal         the seam: long-running, goal-directed work with status + feedback
RobotRuntime         System 1: owns the robot loop, fixed FPS, sync
Controller           chooses the next RobotAction from an Observation
PolicyController     wraps InferenceModel + InferenceExecution + ActionQueue
PolicyRuntime        convenience factory: RobotRuntime + PolicyController
CompositeController  one Controller that fuses multi-rate effector subsystems (Doc B §7)
```

## Reading Order

For a design review:

1. [robot_runtime_architecture.md](./robot_runtime_architecture.md) — production single-rate runtime (Doc A)
2. [composite_robot_architecture.md](./composite_robot_architecture.md) — agentic autonomy layer: Agent + Environment + Goal/Skill (Doc B)
3. [policy_evaluation_design.md](./policy_evaluation_design.md) — exported-policy evaluation
4. [policy_server_design.md](./policy_server_design.md) — remote inference
5. [design_review_summary.md](./design_review_summary.md) — one-page summary

For implementation, follow the phases in Doc A §16, then Doc B once an agentic or composite-robot integration is concrete.

## Main Example

```python
from physicalai.inference import InferenceModel
from physicalai.runtime import PolicyRuntime, SyncInferenceExecution
from physicalai.robot.so101 import SO101

model = InferenceModel.load("./exports/act_policy")
robot = SO101(port="/dev/ttyACM0")

runtime = PolicyRuntime(
    robot=robot,
    model=model,
    execution=SyncInferenceExecution(mode="chunk"),
    fps=30,
)
runtime.run(duration_s=60)
```

CLI:

```bash
physicalai run --config so101_act.yaml --duration-s 60
```

## Documents

| File                                                                       | Use it for                                                          |
| -------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| [robot_runtime_architecture.md](./robot_runtime_architecture.md)           | **Doc A.** Production single-rate runtime; merges the old policy runtime design |
| [composite_robot_architecture.md](./composite_robot_architecture.md)       | **Doc B.** Agentic autonomy layer: deliberative Agent, Environment (real/sim/world-model), Goal/Skill, and CompositeController |
| [policy_runtime_design.md](./policy_runtime_design.md)                     | Deprecated stub redirecting to Doc A                                |
| [policy_evaluation_design.md](./policy_evaluation_design.md)               | Exported-policy evaluation, scope split, naming                     |
| [policy_server_design.md](./policy_server_design.md)                       | Remote inference with `PolicyServer` and `RemoteExecution`          |
| [design_review_summary.md](./design_review_summary.md)                     | One-page summary for colleagues                                     |
| [inference_comparison_report.md](./inference_comparison_report.md)         | Background gap analysis                                             |

## Related

- Action and observation evolution: [`../robot-interface.md`](../robot-interface.md)
- Canonical `Observation` contract: [`../observation.md`](../observation.md)
- Inference foundation: [`../inferencekit.md`](../inferencekit.md)
- Top-level design index: [`../../README.md`](../../README.md)

## Key Decisions

1. `RobotRuntime + Controller` is the architecture. `PolicyRuntime` is a one-line convenience factory.
2. `RobotRuntime.run()` is **synchronous**. Async lives behind the `Robot` adapter and inside `InferenceExecution`.
3. `InferenceExecution` (sync / async-thread / async-process / remote) is the **only** sync/async boundary the runtime needs.
4. `Controller` protocol stays minimal: `start / update / stop / reset` returning `Observation -> RobotAction`.
5. Multi-system autonomy is **not** a callback or a runtime variant. A deliberative `Agent` (System 2) sits above the loop and dispatches `Skill`s; each `Skill` is executed as a `Controller` via `RobotRuntime.swap_controller()` (Doc B).
6. ROS 2 is an optional integration boundary, not a foundation.
7. Current `RobotAction = np.ndarray`. The forward path is `np.ndarray | Mapping[str, Any]` (see [`../robot-interface.md`](../robot-interface.md)).
8. `physicalai run` and `physicalai serve` ship in the runtime distribution without Torch or Lightning.
