# Shared Isolation Layer for Python Plugins

> **Discussion draft.** All APIs below are pseudocode. `physicalai.isolation` is a working import path; implementation, tooling, and distribution location remain open.

## 1. What is needed, and why?

Training and inference integrations need to run framework code without installing its dependencies into the application interpreter. GR00T is one example: its Python/Transformers requirements differ from the host, and its OpenVINO enablement needs a separately patched environment.

The [GR00T prototype][gr00t] already demonstrates this with uv, but environment preparation lives in `physicalai-train`, while its inference adapter implements a separate process manager. Repeating that implementation per plugin increases maintenance and makes deployment depend on training infrastructure.

The shared capability should provide:

- Locked environment preparation, including pinned repositories and local/published patches.
- Finite jobs for training/export and persistent processes that load a model once.
- Common startup, communication, timeout, cancellation, and cleanup behavior.
- Inference installation and operation **without `physicalai-train`**, for every framework, policy, or repository plugin that advertises inference.

This is dependency/process isolation, **not a security sandbox**. It is not another dependency solver, training API, or robot scheduling strategy.

## 2. Proposed approach

```text
Training Engine ─────┐
Inference adapter ───┼──> shared isolation contract ──> existing tool/library
Other package ───────┘      source + lock + process      uv / Pixi / ...
```

The plugin owns native configs, model logic, preprocessing, export recipes, and artifact formats. The shared layer must not import Engine, inference models, or robot code. Construction/discovery performs no installation; preparation is explicit or occurs once at load, never per prediction.

### Declare environments once in the plugin

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

Each plugin owns its integration code, locks, workers, and patches/references. It reuses shared infrastructure rather than bundling a package manager or all framework/model assets. Training can be an optional extra. Host and inference-child dependencies must both satisfy the train-free requirement.

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

1. **Evaluate before expanding:** compare uv, Pixi, and one execution library on the same persistent-worker test. Record how much code each replaces.
2. **Extract only the common remainder:** share preparation and supervision below training and inference. Keep domain APIs in plugins. The layer can initially ship in Runtime; an independent monorepo library or lightweight InferenceKit base remains a packaging choice ([design context][packaging]).
3. **Gate adoption on tests:** clean plugin installation without training, source/lock/patch verification, one model load per process, reset/close, bounded failure/cancellation, safe cache cleanup, read-only artifacts, offline operation, and the GR00T/Cosmos E2E regressions.

For review: **Which tool can satisfy these needs with the least owned code? Which additional consumers or platform constraints should shape the contract? Does independent installation justify a separate distribution now?**

[gr00t]: https://github.com/samet-akcay/physical-ai-studio/pull/23
[uv]: https://docs.astral.sh/uv/concepts/projects/sync/
[pixi]: https://pixi.sh/latest/tutorials/multi_environment/
[pixi-demo]: https://github.com/leoll2/pixi-demo
[isolate]: https://pypi.org/project/isolate/
[execnet]: https://execnet.readthedocs.io/
[ray]: https://docs.ray.io/en/latest/ray-core/handling-dependencies.html
[packaging]: https://github.com/openvinotoolkit/physicalai/blob/docs/design/docs/design/packaging/modular-packages-in-one-repo.md
