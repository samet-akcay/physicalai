# Isolation Components: Design and Naming Evaluation

> Discussion notes on `Environment` / `WorkerEnv`, `GitProject`, `BundledProject`,
> `BundledPatches`, `PublishedPatches`, and `Enablement`.
> Based on the [shared-environments](https://github.com/samet-akcay/physicalai/blob/docs/shared-execution-environments/docs/development/design/shared-environments.md)
> design draft and the GR00T engine plugin POC.

## Verdict

The **isolation object model is good**—especially `ProjectSource` / `PatchSeries`, identity vs provenance, and layered patches. It is not “the elegant final form” of the whole stack yet: `WorkerEnv` still mixes declaration, provisioning recipe, and plugin metadata, and the design doc’s `Environment` facade still needs a clean cut between declare / prepare / run.

**Do not replace `Bundled*` with `Enablement*`.** Those words answer different questions.

---

## What the objects actually are

| Object | Layer | Job |
|---|---|---|
| `ProjectSource` (`BundledProject`, `GitProject`) | **Where code comes from** | Materialize a uv project tree |
| `PatchSeries` (`BundledPatches`, `PublishedPatches`) | **How overlays are obtained & pinned** | Verified patch bytes + optional tree pin |
| `WorkerEnv` → proposed `Environment` | **What to prepare & launch** | Source + Python/extras + worker + cache identity |
| `Enablement` | **What capability was validated** | Policy × operation × format, optionally pointing at an env |
| `PreparedEnvironment` | **Resolved cache row** | Interpreter + checkout after sync |
| Proposed `Provider` / `Session` | **How to provision / how to talk to a process** | uv/Pixi vs job lifetime |

That layering is the right shape. GR00T’s declaration shows why:

```python
UPSTREAM = GitProject(...)
ROBOTICS_AI_SUITE_OPENVINO = PublishedPatches(...)
NATIVE = WorkerEnv(..., project=UPSTREAM, ...)
OPENVINO = WorkerEnv(..., project=GitProject(..., patches=...), ...)
OPENVINO_ENABLEMENT = Enablement(..., env=OPENVINO)
```

One enablement can use published patches today and bundled patches tomorrow. The env and capability stay; only the series kind changes. That is the elegance worth keeping.

---

## What is already elegant

1. **Identity ≠ provenance** on `GitProject`  
   With a final `tree` pin, cache key = commit + tree. Moving Suite patches into the plugin keeps packages valid; provenance still records origin. That is the best idea in this design.

2. **Two project kinds, two patch kinds**  
   Delivery axis (`bundled` vs `git` / `published`) is orthogonal to “is this an enablement?” Correct.

3. **Layered `PatchSeries`**  
   Gradual ownership transfer (published → bundled file-by-file) without a second code path.

4. **Users never construct these**  
   Engines/adapters select; shared service prepares/launches. Public API stays stable.

---

## Where it is less elegant

1. **`WorkerEnv` is overloaded**  
   It holds source, sync recipe (`python`/`extras`), default worker path, and plugin identity (`framework`/`distribution`/`package`). Those change for different reasons. A cleaner cut would be something like:
   - **declaration**: source + sync profile  
   - **launch**: entrypoint chosen at `run`/`start` (you already override worker for export/infer)  
   - **plugin metadata**: kept with the engine/package, not the env key  

   Putting `worker` on the env made sense for “one script per env,” but GR00T already has three workers on two envs—so the field is really a default, not identity.

2. **Design-doc `Environment` vs POC `WorkerEnv`**  
   Facade with `run`/`start` is nice for callers, but it merges:
   - immutable declaration  
   - provider/tooling  
   - process session  

   Prefer: declaration stays data; `prepare()` → prepared; `run`/`start` return `Session`. Otherwise plugins start treating lifecycle as part of the pin.

3. **`Enablement.env` and `WorkerEnv.name` collide in practice**  
   OpenVINO’s env name *is* the enablement name. Fine operationally, but it invites “enablement environment” as a type, which blurs capability metadata with a project pin.

4. **Soft typing**  
   `project: Path | Traversable | ProjectSource` is convenient; it also hides that “plain path ⇒ bundled” is a policy decision. Explicit `BundledProject(...)` at declaration sites is clearer.

---

## Naming: `Bundled` vs `Enablement`

These are not rivals.

- **`Enablement`** = product/capability story (“extra validated support beyond stock”).  
- **`Bundled`** = packaging/ownership story (“bytes live in this plugin distribution”).

Renaming `BundledPatches` → `EnablementPatches` would be wrong: an enablement may use `PublishedPatches`, `BundledPatches`, or no patches; a bundled LeRobot `env/` is often the *default* env, not an enablement.

### Is `Bundled` the best prefix?

It is **accurate but slightly packaging-flavored**. Alternatives by intent:

| Prefix | Signal | Fit |
|---|---|---|
| **Bundled** | Shipped inside the wheel/resources | Good; matches “plugin distribution” |
| **Vendored** | Copied into *our* tree and reviewed here | Strong for patches; common OSS language |
| **Packaged** | Same idea as bundled | Slightly clearer for `*Project` |
| **Local** | Ambiguous (cwd vs ownership) | Weak |
| **Owned** / **Reviewed** | Governance, not location | Incomplete alone |
| **Plugin** | Lives under the plugin package | Clear, a bit noisy |

Preference for discussion:

- Keep the **pair** that encodes origin:
  - projects: `PackagedProject` / `GitProject` *(or keep `BundledProject`)*
  - patches: `PackagedPatches` / `PublishedPatches` *(or `VendoredPatches` / `PublishedPatches`)*
- Keep **`Enablement`** only for capability metadata.
- Prefer **`Environment`** over `WorkerEnv` for the shared service *only if* you also rename carefully elsewhere—plain “environment” collides with Pixi/conda/OS env. `WorkerEnvironment` or `IsolatedEnvironment` is uglier but less overloaded.

`PublishedPatches` is already the right counterpart: it names **external ownership + pin**, not “not bundled.”

---

## “Best design?” — short answer

**For source isolation: yes, this is close to the right design**—typed sources, verified patches, content keys, provenance split, layered ownership transfer. That is better than a single “env path + optional patch dir” blob.

**For the full shared service: not finished.** Next elegance gains come from sharpening boundaries, not inventing new nouns:

1. Keep source/patch types as they are (maybe rename `Bundled` → `Packaged`/`Vendored`).  
2. Slim `WorkerEnv`/`Environment` to “source + sync identity”; pass entrypoint at launch.  
3. Keep `Enablement` out of the isolation vocabulary.  
4. Separate `Provider` (install/activate) and `Session` (process + channels) from the declaration.

---

## Discussion prompts

1. Should origin naming emphasize **packaging** (`Bundled`/`Packaged`) or **ownership** (`Vendored`/`Published`)?  
2. Is `Enablement` staying a **capability record**, or do you want people to say “OpenVINO enablement” when they mean the **env**?  
3. Should `worker` leave the env declaration entirely, since train/export/infer already pick different scripts?  
4. For the public shared package, is the facade `Environment.run/start`, or `env.prepare()` + `session = start(env, entrypoint=...)`?

---

## Round 2 — answers and clarifications

### 1. What is `Bundled`? (and why Vendored/Published felt wrong)

**Plain meaning:** `Bundled` means the bytes **ship inside our plugin package** (wheel / package resources). They are reviewed and versioned with that plugin. It does **not** mean “we implemented the framework ourselves.”

| Kind | Where the files live | Example |
|---|---|---|
| `BundledProject` | Inside the plugin (e.g. `plugin/env/pyproject.toml` + `uv.lock`) | LeRobot worker project shipped with the plugin |
| `GitProject` | Fetched from an upstream Git repo at a pinned commit | Isaac-GR00T at `3df8b38…` |
| `BundledPatches` | `.patch` files stored inside the plugin | Suite patches copied into the plugin and reviewed here |
| `PublishedPatches` | Still owned elsewhere; downloaded (or mirrored) from another repo at a pinned revision + SHA-256 | Robotics AI Suite OpenVINO series from `robotics-ai-suite` |

So:

- **Bundled ≈ “included with our plugin distribution.”** Often yes, that means the files also live in *our* repo/worktree for that plugin—but the important contract is “shipped and reviewed with the plugin,” not “first-party framework code.”
- **Git / Published ≈ “still fetched from outside; we pin and verify.”**

`Vendored` was only an alternative word for the same “in our tree” idea; drop it if it does not help. `Published` is still useful as the opposite of bundled for patches (external owner + digest pins), but if the pair is confusing we can rename toward location language instead, e.g.:

- `PluginProject` / `GitProject`
- `PluginPatches` / `RemotePatches` (or keep `PublishedPatches`)

**Working recommendation:** keep `Bundled*` until a clearer pair is chosen; explain it as “shipped with the plugin,” not “implemented by us.”

### 2. What `Enablement` means (agreed)

**OpenVINO enablement** means: for a given policy (first-party or plugin), that policy can be **exported to OpenVINO** (validated capability). Same pattern for other extras (e.g. XPU training).

So `Enablement` is **capability evidence**, not a source type and not a synonym for the isolated environment:

```text
Enablement("…-openvino")
  → provides: policy X can export to openvino
  → may point at env=… when that capability needs different code/deps
```

The env (possibly with patches) is *how* the capability is built; the enablement is *that* the capability exists. Naming stays:

- say “OpenVINO enablement” for the capability  
- say “OpenVINO environment” (or the env’s name) for the isolated project used to produce it  

Do not rename `BundledPatches` / `GitProject` after enablements.

### 3. `worker` on the environment declaration

Agreed it feels like **lifecycle / launch**, not part of “what is this environment.”

In the POC, one env already has multiple scripts (train / export / infer). Putting a default `worker` on `WorkerEnv` mixes:

- **declaration identity:** source + Python + extras (+ pins) → cache key  
- **launch choice:** which entrypoint to run this time  

**Recommendation:** remove `worker` from the environment declaration. Pass the entrypoint at launch:

```python
prepared = env.prepare()
session = start(prepared, entrypoint=EXPORT_WORKER, ...)
# or finite job:
result = run(prepared, entrypoint=TRAIN_WORKER, args=...)
```

Plugin-owned paths (`TRAIN_WORKER`, `EXPORT_WORKER`, `INFER_WORKER`) stay next to the engine/adapter, not on the env pin.

### 4. Facade shape (agreed)

Prefer the split over a single `Environment.run/start` that owns everything:

```text
Environment          # declaration: source + sync profile (+ name/metadata as needed)
  → prepare()        # → PreparedEnvironment (cache, interpreter, project tree)
  → run/start(prepared, entrypoint=...)  # → Session / job result
Provider             # uv / Pixi / … (install/activate only)
Session              # process + channels + cleanup
```

Declaration stays data. Preparation is reusable. Process lifetime is explicit. Entrypoint is a launch argument.

### Revised short recommendations

1. Explain `Bundled` as **shipped with the plugin**; consider `Plugin*` / `Remote*` only if `Bundled`/`Published` still confuses readers. Drop `Vendored` from the discussion.  
2. Keep `Enablement` = validated capability (e.g. export to OpenVINO); keep it off the isolation type names.  
3. Drop `worker` from the env declaration; pass entrypoint at `run`/`start`.  
4. API shape: `prepare` + `run`/`start(..., entrypoint=...)` with separate `Provider` and `Session`.

---

## Round 3 — design rewrite (done, no issue tracker)

Rewrote `shared-environments.md` for a cold reader with Round 2 agreements. No GitHub issue published.

**Locations:**

- Worktree: `physicalai-worktrees/docs-shared-execution-environments/docs/development/design/shared-environments.md`
- Runtime mirror: `docs/development/design/shared-environments.md`

**Encoded agreements:**

1. `Bundled` = shipped with the plugin package (not “we implemented the framework”).
2. `Enablement` = validated capability (e.g. export to OpenVINO); separate from isolation source types.
3. No `worker` on the Environment declaration; entrypoint passed to `run` / `start`.
4. Lifecycle: `prepare` → `run` / `start(..., entrypoint=...)` with separate Provider and Session.
5. Primary test seam: shared isolation service boundary (Engine / Runtime / CLI are consumers only).