# ModelingBench v1 contracts

The harness separates clean project state (`tasks`, `agents`, `render_profiles`, and `schemas`) from disposable run state under a marker-protected `generated` root. A run owns and verifies copies of the task tree, schemas, agent and render profiles, and Blender evaluator before invoking an agent. Sensitive operations recheck that snapshot.

## Task frame and typed inputs

`task.toml` version 1 fixes metric unit scale, `+Z` up, `-Y` front, evaluation center, subject bounds, and optional anchors. An anchor is represented in a submitted `MODEL` collection by an object or empty named `ANCHOR_<name>` (the bare configured name is also accepted) and must lie within its tolerance. Geometry is never centered, scaled, or reoriented by the harness.

`inputs/manifest.toml` assigns stable IDs, roles, uses, and project-relative files. All files are copied into the task snapshot; `editable_seed` files are additionally delivered beneath `workspace/seeds/<input_id>/`. Supplied inputs are registered as evidence before the first agent turn.

## Agent boundary

Profiles contain an argv array. Commands are launched directly with `shell=False`, receive an assembled prompt on standard input, and write a result conforming to the snapshotted `schemas/agent_result.schema.json`. Paths, schemas, latest checkpoint, feedback log, and evidence registry are passed explicitly through argv placeholders, the prompt, and `MODELBENCH_*` environment variables. The child environment is allowlisted; profiles may opt in additional variable names with `pass_env`.

The private `modelbench-agent` command requires a random per-turn capability bound to one canonical active run. It registers researched files and measurement provenance or creates an owned checkpoint. Checkpoints render in staging and install atomically, append feedback-consumption events instead of rewriting feedback history, and roll back cleanly when preview rendering fails. Evaluation cameras are a fixed vocabulary; diagnostic cameras are separately declared JSON records and written only under `diagnostic/`.

## Evaluation and publication

The Blender process starts with factory settings and auto-execution disabled. The driver inspects dependencies, appends only `MODEL` into a clean scene, evaluates modifiers and instances, validates finite transforms, nonempty polygons, bounds, and anchors, replaces materials, and creates all cameras, lights, world, render, and color settings itself.

Both checked-in profiles are immutable capability contracts. Blender 5.1, exact RTX 3090 OptiX selection, Cycles, GPU OptiX denoising, and ACES 2.0 are mandatory. Missing capabilities fail the run. Tests inject a protocol-compatible render function; they do not alter production profiles.

A submission is copied and hashed before evaluation. Rendering occurs in a fresh attempt directory and is accepted only when the result is bound to that artifact hash and contains exactly the 14 nonempty, hash-matching standardized PNG views. Only a complete successful set is renamed to `renders/final`, mirrored under task `current/`, and published through an atomic `current.json` update. Resume checks the owned artifact hash, reuses a completed final render set when publication needs retrying, and never invokes the modeling agent.

## Safety

One orchestration/reset lock is allowed. Steering, pause requests, status reads, and authenticated child checkpoint operations remain available while the agent runs. Reset resolves its exact boundary, requires an exact known marker and confirmation, refuses nonempty unmarked roots, rejects links, junctions, or broad paths, and removes only children of `generated/`.

The harness sanitizes agent and Blender environments, and the checked-in Codex profile requests `workspace-write` sandboxing. It does not itself establish an OS security container for arbitrary profile commands or Blender. Production use with untrusted processes or files requires external filesystem and network isolation.
