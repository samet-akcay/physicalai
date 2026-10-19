# Shared Isolation Layer for Python Plugins

> **Discussion draft.** All APIs below are pseudocode. `physicalai.isolation` is a working import path; implementation, tooling, and distribution location remain open.

## 1. What is needed, and why?

Some integrations need a separate interpreter/dependency set; others do not. The [LeRobot plugin][lerobot] already runs native training in a bundled, locked environment. [GR00T][gr00t] adds a pinned repository, patched OpenVINO environment, and persistent inference process. Their common infrastructure currently lives in `physicalai-train`, while GR00T's inference adapter also implements process management.

The proposal shares existing infrastructure across these consumers and removes deployment's dependency on training. It does not require moving compatible first-party APIs into workers.

The shared capability should provide:

- Locked environment preparation, including pinned repositories and local/published patches.
- Finite jobs for training/export and persistent processes that load a model once.
- Common startup, communication, timeout, cancellation, and cleanup behavior.
- Inference installation and operation **without `physicalai-train`**, for every framework, policy, or repository plugin that advertises inference.

This is dependency/process isolation, **not a security sandbox**. It is not another dependency solver, training API, or robot scheduling strategy.

### When would each consumer use this?

| Consumer                                                        | Isolation need                                                       | Current or proposed behavior                                                                                                         |
| --------------------------------------------------------------- | -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| First-party `Trainer.fit(...)`                                  | None when dependencies are compatible                                | Keep Lightning training in-process; no environment preparation or API change.                                                        |
| First-party exported ONNX/OpenVINO model                        | None when the backend/processors are compatible                      | Keep normal `InferenceModel` inference in-process.                                                                                   |
| First-party policy or export tool with conflicting dependencies | Conditional, not a demonstrated requirement for all current policies | Opt in to a dedicated environment/job for that policy or toolchain; extras alone cannot install conflicting versions together.       |
| LeRobot native training, [PR #10][lerobot]                      | Already implemented                                                  | Use the bundled project/lock and native device profile (CPU/CUDA/XPU); no repository patches required.                               |
| LeRobot Runtime inference                                       | Depends on the selected policy/dependencies                          | Current adapter runs in-process. Use a persistent child only for an incompatible dependency set; that migration is not claimed here. |
| GR00T native training and OpenVINO enablement                   | Already demonstrated in its prototype                                | Use upstream and patched project locks; inference retains a model in a child.                                                        |

### First-party API: unchanged unless there is a concrete conflict

```python
# Normal first-party path: no isolation layer needed.
trainer = Trainer(...)
trainer.fit(policy, datamodule=data)
with InferenceModel("exports/act-openvino") as model:
    actions = model.predict_action_chunk(observation)

# Optional future case: a policy/exporter needs an incompatible toolchain.
policy_environment.run(entrypoint=first_party_job, args=["--config", recipe_path])
```

In the optional case, the job constructs Trainer/policy objects inside the child. A live Lightning Trainer, callbacks, and datamodule are not transparently moved across processes. The inspected first-party Transformers ranges overlap at `5.15.1`; that does not establish compatibility for every optional backend/device combination.

## 2. Proposed approach

```text
Training Engine ─────┐
Inference adapter ───┼──> shared isolation contract ──> existing tool/library
Other package ───────┘      source + lock + process      uv / Pixi / ...
```

The plugin owns native configs, model logic, preprocessing, export recipes, and artifact formats. The shared layer must not import Engine, inference models, or robot code. Construction/discovery performs no installation; preparation is explicit or occurs once at load, never per prediction.

### LeRobot: a locked project, without repository patches

```python
# Pseudocode adapting the existing PR #10 flow.
lerobot_environment = Environment(
    source=BundledProject("plugin/env"),  # pyproject.toml + uv.lock
    provider=Uv(lock="uv.lock", extras=worker_extras_for(config.policy.device)),
)
config.save_pretrained(config_dir)  # official LeRobot TrainPipelineConfig
result = lerobot_environment.run(
    entrypoint=lerobot_worker,
    args=[f"--config_path={config_dir}/train_config.json"],
    on_event=forward_training_progress,
)
```

Keep native device selection and the host/worker config-version check from PR #10. This consumer reuses preparation, cache, logs, progress transport, cancellation, and shutdown; it does not need GR00T-specific patch or inference logic.

### GR00T: a repository with an additional patch environment

```python
# Pseudocode: plugin/environments.py
from physicalai.isolation import Environment, GitProject, BundledPatches, Uv

UPSTREAM = "https://github.com/NVIDIA/Isaac-GR00T.git"

native = Environment(
    source=GitProject(UPSTREAM, revision=PINNED_COMMIT),
    provider=Uv(python="3.10", lock="uv.lock"),
)

openvino = Environment(
    source=GitProject(
        UPSTREAM,
        revision=PINNED_COMMIT,
        patches=BundledPatches("plugin/patches/openvino", tree=REVIEWED_TREE),
    ),
    provider=Uv(python="3.12", lock="uv.lock"),  # lock from the patched project
)

openvino.prepare()  # optional pre-provisioning; reusable cached environment
```

`PublishedPatches(repository=..., revision=..., files=..., tree=...)` can reference another team's patch series instead. Ordered layers allow published patches plus local changes. Verify patch content and the resulting tree; record origin separately from environment identity.

### Use it from a training/export package

```python
# Pseudocode: plugin/engine.py
from plugin.environments import native, openvino

class Gr00tEngine(TrainingEngine):
    def train(self, config):
        result = native.run(
            entrypoint=train_worker,
            args=serialize_native_config(config),
            on_event=forward_training_progress,
        )
        return translate_training_result(result)

    def export(self, checkpoint, destination):
        return openvino.run(
            entrypoint=export_worker,
            args=[checkpoint, destination],
        )
```

The framework/plugin retains its config and training behavior. The shared layer only supervises the job; it does not define what a checkpoint or training step means.

### Use it from an inference adapter

```python
# Pseudocode: plugin/adapter.py — no training imports
from plugin.environments import openvino

class Gr00tAdapter:
    def load(self, model_path):
        self.session = openvino.start(
            entrypoint=inference_worker,
            startup_timeout=60,
            request_timeout=10,
        )
        try:
            self.session.request({"op": "load", "path": model_path})
        except Exception:
            self.session.close()
            raise

    def predict(self, observation):
        return self.session.request({"op": "predict", "inputs": observation})

    def reset(self):
        self.session.request({"op": "reset"})

    def close(self):
        self.session.close()
```

One child retains the model across requests. The adapter defines message semantics; the session supplies bounded communication and deterministic shutdown. JSON plus numeric buffers avoids transporting pickled Python objects. Other packages can use the same interface for converters, evaluators, or image-processing tools without importing Engine or `InferenceModel`.

### Keep user-facing installation simple

```bash
# Target published-package contract; training extra selects the plugin.
uv pip install "physicalai-train[gr00t]"

# Deployment: plugin plus its declared Runtime/shared dependencies, not training.
uv pip install physicalai-gr00t-policy-plugin
```

Each plugin owns its integration code, locks, workers, and patches/references. It reuses shared infrastructure rather than bundling a package manager or all framework/model assets. Training can be an optional extra. Every inference plugin must satisfy the train-free requirement, but compatible in-process plugins need no isolation machinery.

## 3. Existing tools

| Tool               | What it can supply                                                   | What needs checking for this use case                                                                                                                             |
| ------------------ | -------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [uv][uv]           | Python installation, lock consumption, environment synchronization   | Persistent sessions are outside its scope. Preserve upstream `uv.lock`; validate project/lock agreement separately from frozen sync.                              |
| [Pixi][pixi]       | Conda/PyPI environments, activation, tasks, multiple Python versions | Requires an owned Pixi lock. Test a resident worker, not a new process/model load per request.                                                                    |
| [Isolate][isolate] | Environment management and cross-environment execution               | Candidate to replace more of our implementation. Verify existing-lock support, persistent model state, serialization, lifecycle, source/license, and maintenance. |
| [execnet][execnet] | Persistent interpreter gateways and communication                    | Does not install environments; evaluate numeric buffers, deadlines, and cleanup.                                                                                  |
| [Ray][ray]         | Actors/tasks, runtime environments, distributed placement            | Appropriate if distributed scheduling is required; assess the additional operational footprint for a local process.                                               |
| OCI containers     | Native/OS dependencies and prebuilt images                           | Assess image distribution, device access, startup, and lifecycle; not required for the initial local-Python scope.                                                |

The [Pixi demo][pixi-demo] establishes multi-environment task selection and file handoff. It does not establish persistent inference performance. Compare alternatives using the same worker, model, observations, and protocol. These are candidates, not validated GR00T backends; uv is the existing prototype baseline.

## 4. Why retain anything if a tool already does this?

**A shared contract is needed; a custom implementation is not necessarily needed.** Existing tools should replace the responsibilities they already handle.

```python
# Pseudocode: change preparation/launch tooling, not framework behavior.
env = Environment(source=PINNED_PROJECT, provider=Uv(lock="uv.lock"))
env = Environment(source=PINNED_PROJECT, provider=Pixi(lock="pixi.lock", name="model"))

# If a library covers both provisioning and persistent sessions:
env = ExistingLibraryIntegration(REVIEWED_CONFIGURATION)
```

The remaining integration responsibilities are:

- Selecting plugin-owned sources, patch layers, profiles, and model assets.
- Associating exported artifacts with compatible environment identities/provenance.
- Connecting tool lifecycle/events to Engine or inference contracts.
- Enforcing deployment independence and testing failure/offline behavior.

If a library covers the required lifecycle, use it directly or through a small adapter. Do not preserve our own environment/process manager merely to keep an abstraction. Do not automatically translate locks or fall back between providers: a different lock requires compatibility validation.

## 5. Proposed next steps and feedback

**The case is a small extraction, not a platform built for hypothetical users:** LeRobot already needs locked native training; GR00T needs related preparation plus patching and deployment. First-party policies are not the sole justification, and isolation must not be imposed on them.

1. **Evaluate before expanding:** compare uv, Pixi, and one execution library on the same workers. Record how much code each replaces.
2. **Extract only proven common needs:** share preparation and supervision below training and inference. Keep domain APIs in consumers. Delay a separate distribution until independent consumers justify it ([packaging context][packaging]).
3. **Gate adoption on both integrations:** preserve LeRobot native config/CLI behavior, progress/cancellation, and numerical regressions; preserve GR00T/Cosmos train/export/infer, patch verification, resident models, and reset/close. Check clean inference installs without training and unchanged first-party in-process behavior.

If the extraction adds more policy/wrapper complexity than it removes, or an existing library meets the contract directly, do not maintain an additional general manager.

For review: **Which tool can satisfy these needs with the least owned code? Which additional consumers or platform constraints should shape the contract? Does independent installation justify a separate distribution now?**

[gr00t]: https://github.com/samet-akcay/physical-ai-studio/pull/23
[lerobot]: https://github.com/samet-akcay/physical-ai-studio/pull/10
[uv]: https://docs.astral.sh/uv/concepts/projects/sync/
[pixi]: https://pixi.sh/latest/tutorials/multi_environment/
[pixi-demo]: https://github.com/leoll2/pixi-demo
[isolate]: https://pypi.org/project/isolate/
[execnet]: https://execnet.readthedocs.io/
[ray]: https://docs.ray.io/en/latest/ray-core/handling-dependencies.html
[packaging]: https://github.com/openvinotoolkit/physicalai/blob/docs/design/docs/design/packaging/modular-packages-in-one-repo.md
