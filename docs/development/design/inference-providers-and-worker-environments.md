# Inference Providers and Worker Environments

> **Status:** Discussion draft. This document compares architecture options; it records no team decision.
>
> **Scope:** Runtime inference, first-party deployment components, framework/repository plugins, and isolated environments shared by training and deployment workflows.

## Summary

There are three related but independent decisions:

1. **Who supplies an implementation?** A built-in implementation or an installed provider/plugin.
2. **Where do its dependencies run?** In the host process, a separate Python environment/process, or a container/remote worker.
3. **How is the code distributed?** In one `physicalai` wheel, optional distributions, or another delivery format.

For example, first-party preprocessors can be providers without becoming separate PyPI packages. Pixi can provision a worker environment without defining Runtime's plugin API. Lazy imports can avoid importing unused code, but cannot make conflicting versions coexist in one interpreter.

A useful candidate to evaluate is a single Runtime distribution with lazy first-party providers and optional dependencies, alongside persistent worker processes for integrations with incompatible frameworks. The environment provider—uv, Pixi, a container, or an existing tool—can be evaluated separately.

## Current context

- Runtime's `pyproject.toml` currently makes Transformers, ONNX Runtime, OpenVINO, and OpenVINO Tokenizers base requirements. Some implementation modules are also imported through eager package initializers.
- Studio exports Runtime manifests that name preprocessors and postprocessors by stable type identifiers. Runtime owns the deployment implementations; `physicalai-train` owns the training implementation and export logic.
- Several deployment processors are lightweight NumPy/OpenCV code. For example, Pi05 image/prompt preparation does not itself require Transformers; `HFTokenizer` does. OpenVINO exports can use an exported tokenizer artifact instead.
- Studio Engine PRs [#10](https://github.com/samet-akcay/physical-ai-studio/pull/10) and [#23](https://github.com/samet-akcay/physical-ai-studio/pull/23) use Engine providers and locked worker environments. GR00T's Runtime adapter launches a worker so its Transformers version does not have to match the Runtime host.
- The current Engine `WorkerEnv` implementation is uv-oriented. Whether to retain that implementation, wrap an existing tool, or support a different provisioner is an open question.

Runtime executes the processors declared by a manifest (or explicitly supplied to the model). The current concern is that dependencies and imports are broader than the selected pipeline requires—not that every policy runs every built-in processor.

## Option 1: Providers for first-party and third-party implementations

Use provider registries for Runtime adapters/components and for training/export Engines. First-party and external implementations follow the same contracts. A provider does not have to be a separately published distribution: first-party provider code can ship inside `physicalai` or `physicalai-train`.

```text
Studio / training distribution
  Engine registry
    ├── first-party Trainer provider
    ├── LeRobot provider
    └── repository/framework providers

Runtime distribution: physicalai
  component registry                      adapter registry
    ├── first-party processors              ├── OpenVINO adapter
    ├── generic components                   ├── ONNX adapter
    └── installed plugin components          └── installed plugin adapters
```

A first-party Runtime provider could register stable manifest identifiers to lazy import paths. The entry-point registration function should itself be lightweight; import the implementation only when a manifest selects it. External distributions can register through the same entry-point groups. Engine discovery should likewise identify providers without importing every framework's training stack.

**Advantages**

- First-party code no longer needs a privileged implementation path in the loader.
- A provider can declare the capabilities it supports and its required optional dependencies.
- One plugin can contribute a training Engine and a Runtime adapter, with training-only dependencies kept optional.
- First-party implementations do not require one PyPI project per processor.

**Costs and constraints**

- More public contracts to version: identifiers, constructor parameters, capabilities, and provider compatibility.
- Discovery and error reporting need careful design; an unavailable component should name its provider/extra.
- In-process providers still share one dependency set. Provider registration does not isolate incompatible Transformers versions.
- Installed entry points are executable code and must be treated as trusted dependencies. Model artifacts should contain data and provenance, not plugin code.

Do not force every policy into a single abstraction. A composed pipeline of Runtime components is appropriate for exported ACT/Pi05/Molmo models; a framework adapter that owns the complete pipeline is appropriate for GR00T or another framework that must retain its native preprocessing and postprocessing.

## Option 2: One `physicalai` distribution, with lazy imports and extras

Keep the current package and first-party source tree in one Runtime wheel. Make feature dependencies optional and import implementations only on demand. Keep third-party plugins as separate packages only when they need independent ownership or installation.

```text
physicalai wheel
  ├── Runtime + generic inference API
  ├── first-party deployment component source
  ├── built-in provider registration (lazy)
  └── optional dependency profiles
        ├── openvino
        ├── onnx
        └── hf-tokenizer

separately installed plugin wheels (as needed)
  ├── GR00T Runtime adapter + worker entry point
  └── other external framework/repository integrations
```

Extras are examples; group them by real capability and tested dependency set, not necessarily one extra per policy. A convenience profile could install the normal first-party deployment stack.

**Advantages:** one main Runtime artifact, least distribution overhead, preserves existing code ownership and import paths, and allows a minimal host install when dependencies are genuinely optional.

**Costs and constraints:** the wheel still contains first-party source files; extras do not remove files. All meaningful dependency combinations need testing. Changing the dependencies installed by bare `pip install physicalai` changes the current installation contract, so a migration/profile strategy is needed.

This option can be combined with Option 1: first-party implementations inside the wheel can use the same provider registry as external plugins.

## Option 3: Pixi multi-environment tasks

Pixi can define a single project with multiple locked environments and tasks. The colleague's [pixi-demo](https://github.com/leoll2/pixi-demo) demonstrates three tasks using separate Python/PyTorch/NumPy environments and passing an image path and JSON detections between them:

```text
main process
  ├── pixi run preprocess  ──> preprocessed.jpg
  ├── pixi run detect      ──> detections.json
  └── pixi run annotate    ──> annotated.jpg
```

This is evidence that Pixi can manage incompatible task environments in one project. It is a useful starting point for a worker-environment experiment, not a complete Runtime worker design. The repository is a PoC rather than a packaged runtime library; GitHub currently reports no declared license, so review that before reusing its code. The demo starts a subprocess for each task and uses files as its handoff; the detector script loads the model during that invocation. A robot deployment generally wants a worker that loads the model once, persists across action requests, supports reset/shutdown, and returns errors promptly. The demo also documents Linux x86-64 as its requirement.

A Runtime adaptation would look more like:

```text
RobotRuntime / InferenceModel
  └── provider adapter
        └── start once: pixi run -e gr00t python -m worker
              ├── load model once
              ├── framed requests/responses over pipes or a local socket
              └── reset / close / report worker failure
```

Pixi would then provide and launch the environment; the adapter/protocol would still own worker lifecycle and typed observation/action exchange. File handoff remains suitable for offline or batch steps. For high-rate inference, benchmark pipes or shared memory with NumPy buffers rather than serializing image arrays as JSON or repeatedly writing them to disk.

**Advantages:** multiple Python and native/Conda environments in one project; a project lock and familiar `pixi run` interface; potentially useful when PyPI-only resolution is insufficient.

**Costs and constraints:** Pixi does not define Engine/provider APIs, process lifetime, model state, inference protocol, package provenance, or worker health semantics. Pixi-specific projects/locks would need to be packaged and verified with plugins. It is therefore a candidate environment backend, not a direct replacement for all of `WorkerEnv`.

## Option 4: Existing environment and process tools

These tools address different parts of the problem and can be combined.

| Tool | What it can provide | Main fit / limitation |
|---|---|---|
| [uv](https://docs.astral.sh/uv/concepts/projects/sync/) | Python project lock and environment sync | Current Engine direction; effective for PyPI projects. A worker adapter still needs to launch and manage a persistent process. |
| [Pixi](https://pixi.sh/latest/tutorials/multi_environment/) | Multi-environment project, Conda + PyPI resolution, tasks | Attractive for diverse Python/native environments; worker lifecycle and protocol remain separate. |
| [Isolate](https://pypi.org/project/isolate/) | Managed local/Conda/container/remote environments and execution | Worth a focused evaluation. Check its current source, lockfile integration, lifecycle, serialization, Python-version support, and maintenance before adopting it. Its published package documents pickle as the default serialization, which does not match the existing preference for an explicit, pickle-free inference protocol. |
| [execnet](https://execnet.readthedocs.io/en/latest/basics.html) | Persistent interpreter gateways and communication channels | Candidate for cross-interpreter process communication, not environment resolution or lock management. Check its serialization and lifecycle against Runtime's requirements. |
| [Ray runtime environments](https://docs.ray.io/en/latest/ray-core/handling-dependencies.html) | Actors/tasks, cached environments, distributed execution | Consider where distributed scheduling or a Ray-based deployment already exists. Adds a runtime and operational model that may be excessive for a local robot loop. |
| OCI containers | OS-level and Python dependency isolation | Strong deployment boundary for native libraries and reproducible images; adds image build/distribution, device/GPU access, and startup/operations work. |

An existing tool can eliminate environment-management code without eliminating integration code: Runtime or Engine still needs to select an environment, launch a worker, exchange data, check compatibility, and close or recover it.

## Option 5: Split distributions or extract `inferencekit`

A monorepo can build separate distributions for generic inference, Runtime, or shared worker utilities. This can create stricter dependency boundaries and independent consumers, but is not required for provider discovery or optional dependencies. A package split alone does not isolate two dependency versions in one process.

```text
one repository
  ├── inference core distribution
  ├── physicalai Runtime distribution → inference core
  └── optional provider distributions
```

Separate distributions are most useful when there is a real independent consumer/release need. Avoid publishing a separate wheel for each first-party component solely to make it lazy. If Anomalib and Geti need a shared environment layer and no existing tool fits, consider one neutral shared utility after validating their requirements; environment management belongs below both Engine and inference, not specifically inside `inferencekit`.

Avoid naming that shared layer just `workers`: the term also refers to Studio job workers, PyTorch data-loader workers, and Runtime background inference processes. If we own environment provisioning, a provisional module name such as `physicalai.python_envs` and API types like `PythonEnvironmentSpec` / `EnvironmentProvider` describe its responsibility more clearly. Keep process lifecycle and inference IPC with the Engine or provider that owns the worker. If this becomes a standalone cross-project package, select a neutral distribution name based on its actual ownership.

## Comparison by concern

| Concern | Lazy in one distribution | Providers/plugins | Separate worker env (uv/Pixi) | Container/remote service |
|---|---|---|---|---|
| Avoid installing unused optional dependencies | Yes, with optional requirements | Yes, if provider is not installed or has optional extras | Worker deps stay out of host | Yes |
| Avoid importing unused implementations | Yes, with lazy imports | Yes, with lazy provider loading | Yes, if implementation stays in worker | Yes |
| Run incompatible dependency versions | **No**, not in one interpreter | **No**, not by registration alone | Yes | Yes |
| Keep native framework preprocessing intact | Possible, if compatible in-process | Yes; whole-pipeline adapter can own it | Yes | Yes |
| Add distribution/runtime operations | Low | Provider contracts and packages | Provisioner + lifecycle + protocol | Highest |

These approaches are composable, not mutually exclusive. For example, first-party providers can ship in the single Runtime wheel, while a GR00T provider runs in a Pixi- or uv-provisioned worker.

## Evaluation questions

A short proof of concept should answer these before selecting a direction:

1. **Dependency isolation:** Can a host using Runtime's supported Python coexist with the worker Python and package versions required by GR00T?
2. **Steady-state inference:** Does one process remain alive and retain the loaded model across repeated action-chunk requests?
3. **Robot lifecycle:** Are reset, close, cancellation, startup errors, worker crashes, and timeouts observable and bounded?
4. **Transport:** Can the worker receive image/state/task observations and return numeric actions efficiently without pickle or per-request files?
5. **Deployment:** Can environments be prebuilt and used offline? Where do caches and model artifacts live? How are native libraries and GPU/robot devices exposed?
6. **Reproducibility:** Does the package record the worker project/lock, source revision, patches, and provider compatibility it needs?
7. **Cross-project reuse:** Do Anomalib and Geti need the same environment lifecycle and protocol, or only the same lockfile tool?
8. **Install profiles:** Can a clean Runtime-only install load a core/fake adapter; can ACT/Molmo/Pi05 load with their selected provider extras; can GR00T run with its worker dependencies absent from the host?

A fair Pixi comparison should run the same persistent-worker protocol as the uv prototype. Compare cold provisioning separately from warm startup, then measure resident memory and repeated-inference latency. The Pixi demo's one-shot task path is a useful demonstration of environment selection, but not a like-for-like Runtime benchmark.

## Candidate repository layout for evaluation

This keeps first-party deployment code in the Runtime distribution and treats it as a provider. It does not introduce a `physicalai-policy-components` wheel. The tool-first baseline also does not add a Runtime environment-manager subpackage: a plugin owns its Pixi/uv project and lock, while its adapter starts the worker. Add a thin shared integration only if a selected tool leaves repeated, concrete needs unmet.

```text
physicalai-runtime/
├── pyproject.toml                 # one physicalai distribution + capability extras
├── src/physicalai/
│   ├── runtime/                   # robot loop, action sources, execution
│   ├── robot/                     # robot interfaces/integrations
│   ├── capture/                   # camera interfaces/integrations
│   ├── inference/
│   │   ├── model.py               # generic manifest-driven orchestration
│   │   ├── manifest.py
│   │   ├── components/
│   │   │   ├── registry.py
│   │   │   ├── _builtins.py       # lightweight registrations
│   │   │   └── ...                # common + first-party NumPy processors
│   │   ├── adapters/              # contracts + lazy OpenVINO/ONNX modules
│   │   ├── runners/
│   │   └── callbacks/
├── packages/                      # independently useful hardware integrations
└── tests/

physicalai-studio/
├── library/src/physicalai/
│   ├── engine/                    # Engine/TrainingEngine + provider discovery
│   ├── train/                     # first-party Trainer, optional training deps
│   └── policies/                  # training/export implementations
└── plugins/policies/<plugin>/     # e.g. Engine + Runtime adapter + worker spec
```

The Studio exporter continues to emit manifest component IDs and artifacts; it does not bundle Python processor code into the model package. Runtime resolves those IDs using installed, trusted providers. Existing IDs and imports should be preserved through lazy aliases during migration.

## References

- [Studio Engine PR #10](https://github.com/samet-akcay/physical-ai-studio/pull/10)
- [Studio GR00T PR #23](https://github.com/samet-akcay/physical-ai-studio/pull/23)
- [Pixi multi-environment demo](https://github.com/leoll2/pixi-demo)
- [Pixi multi-environment docs](https://pixi.sh/latest/tutorials/multi_environment/)
- [Runtime modular-packages proposal](https://github.com/openvinotoolkit/physicalai/blob/docs/design/docs/design/packaging/modular-packages-in-one-repo.md)
- [Runtime security model](../../getting-started/security.md)
