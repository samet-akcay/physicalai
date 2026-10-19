# Shared Isolation Layer for Python Plugins

> **Discussion draft.** All APIs below are pseudocode. `physicalai.isolation` is a working import path; implementation, tooling, and distribution location remain open.

## 1. What is needed, and why?

First-party policies originally used different Transformers versions; aligning them required implementation changes and testing. Their current common dependency range is a maintained constraint, not evidence that all policies naturally need the same versions. Separate policy/toolchain environments would allow independent upgrades instead of repeatedly adapting every implementation to one host dependency set.

There are also existing plugin consumers: the [LeRobot plugin][lerobot] runs native training in a bundled, locked environment; [GR00T][gr00t] adds a pinned repository, patched OpenVINO environment, and persistent inference. Their common infrastructure currently lives in `physicalai-train`, while GR00T's inference adapter also implements process management.

The proposal shares that infrastructure and removes deployment's dependency on training. Compatible first-party paths can remain in-process; policies that need different versions can use independently tested environments.

The shared capability should provide:

- Locked environment preparation, including pinned repositories and local/published patches.
- Finite jobs for training/export and persistent processes that load a model once.
- Common startup, communication, timeout, cancellation, and cleanup behavior.
- Inference installation and operation **without `physicalai-train`**, for every framework, policy, or repository plugin that advertises inference.

This is dependency/process isolation, **not a security sandbox**. It is not another dependency solver, training API, or robot scheduling strategy.

### When would each consumer use this?

| Consumer                                                   | Isolation need                                                   | Current or proposed behavior                                                                                                                                                        |
| ---------------------------------------------------------- | ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| First-party `Trainer.fit(...)`                             | Optional when its dependency set matches the current interpreter | Keep direct Lightning training in-process; isolation is not required for every call.                                                                                                |
| First-party exported ONNX/OpenVINO model                   | None when the backend/processors are compatible                  | Keep normal `InferenceModel` inference in-process.                                                                                                                                  |
| First-party policies with different native dependency sets | Avoid repeated cross-policy version alignment                    | Give each incompatible policy/toolchain a tested lock and child process; compatible policies can share an environment. This first-party path still needs implementation/validation. |
| LeRobot native training, [PR #10][lerobot]                 | Already implemented                                              | Use the bundled project/lock and native device profile (CPU/CUDA/XPU); no repository patches required.                                                                              |
| LeRobot Runtime inference                                  | Depends on the selected policy/dependencies                      | Current adapter runs in-process. Use a persistent child only for an incompatible dependency set; that migration is not claimed here.                                                |
| GR00T native training and OpenVINO enablement              | Already demonstrated in its prototype                            | Use upstream and patched project locks; inference retains a model in a child.                                                                                                       |

### First-party policies: retain their own tested dependency sets

```python
# Pseudocode: the two locks can require different Transformers/Torch versions.
policy_a = Environment(
    source=BundledProject("policies/a/env"),
    provider=Uv(lock="uv.lock"),
)
policy_b = Environment(
    source=BundledProject("policies/b/env"),
    provider=Uv(lock="uv.lock"),
)
policy_a.run(entrypoint=first_party_job, args=["--config", recipe_a])
policy_b.run(entrypoint=first_party_job, args=["--config", recipe_b])
```

```python
# first_party_job.py: executed in the selected child's environment.
trainer = Trainer(...)
trainer.fit(policy, datamodule=data)
```

A direct `Trainer.fit(...)` remains available in a compatible interpreter. For isolation, launch the whole job and construct Trainer, policy, datamodule, and callbacks in the child; do not serialize live Lightning objects into another process. Exported ONNX/OpenVINO inference need not retain the original training environment unless its preprocessing or adapter requires those framework dependencies.

**Packaging prerequisite:** process separation alone is insufficient if every child installs a package that still mandates one Transformers version. Move policy-specific requirements out of shared base dependencies and into the selected policy's extra/project. Runtime's mandatory Transformers requirement must also be optionalized or separated if its package is installed in a child needing another version. Do not install an all-policies extra in each child. Test shared Trainer/components against each lock: isolation removes the need to co-install versions, not framework API incompatibilities.

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

**The case is shared infrastructure for three needs:** reducing first-party cross-policy dependency-alignment work, retaining LeRobot's native locked training, and supporting GR00T's source/patch/deployment environments. Reuse the plugin infrastructure first, then validate independently versioned first-party jobs; do not impose child processes on compatible calls.

1. **Evaluate before expanding:** compare uv, Pixi, and one execution library on the same workers. Record how much code each replaces.
2. **Extract only proven common needs:** share preparation and supervision below training and inference. Keep domain APIs in consumers. Delay a separate distribution until independent consumers justify it ([packaging context][packaging]).
3. **Gate adoption on the consumers:** demonstrate two first-party jobs with genuinely incompatible dependency locks after the packaging changes; preserve direct Trainer behavior, LeRobot native config/CLI/progress/cancellation, and GR00T/Cosmos train/export/infer, patch verification, resident models, and reset/close. Check clean inference installs without training.

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
