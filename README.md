# ModelingBench

ModelingBench is an auditable orchestration harness for iterative Blender modeling agents. It snapshots task inputs, owns every checkpoint and submission, records evidence and human steering, and evaluates only a collection named `MODEL` in a clean Blender scene.

The production render profiles are deliberately strict: Blender 5.1, Cycles, OptiX on an RTX 3090, GPU denoising, and ACES 2.0. Missing production capabilities are errors, never silent fallbacks.

## Quick start

See [Choose or change the model and reasoning effort](#choose-or-change-the-model-and-reasoning-effort) before starting a run.

```powershell
python -m pip install -e .
modelbench validate-task car_trunk_four_bar
modelbench run car_trunk_four_bar --agent codex --verifier codex
modelbench serve
```

Open the loopback URL printed by `serve`. The **Review runs** tab places renders and reference images side by side. Hover a panel (or focus it with Tab), then use Left/Right to cycle within its selected category; navigation wraps at the ends. Use the revision selector to inspect earlier evidence and expand the previous-revision comparison when needed. Model and effort badges appear above the images; recorded run context is explicitly distinguished from provenance linked to the viewed revision. Findings, correction controls, and expandable model settings follow the viewer. The separate **New run** tab contains task and agent setup. Restart the reviewer server after updating its code, then reload the browser.

Publication is automatic when the orchestrator's gates pass; there is no human approval step.

## Choose or change the model and reasoning effort

Both roles default to **gpt-6-astra / high** in `agents/codex.toml`. The builder and verifier run in separate contexts.

```powershell
# Choose both roles when starting a run
modelbench run car_trunk_four_bar --agent codex --verifier codex `
  --builder-model gpt-6-astra --builder-effort high `
  --verifier-model gpt-6-astra --verifier-effort high

# Change one role for subsequent invocations in an existing run
modelbench set-model car_trunk_four_bar/run_000005 `
  --role builder --model gpt-6-astra --effort medium

# Change both roles
modelbench set-model car_trunk_four_bar/run_000005 `
  --role both --model gpt-6-astra --effort high
```

`set-model` requires `--role`, `--model`, and `--effort`. Choose `builder`, `verifier`, or `both`. Changes apply at the next invocation of the selected role, including paused and resumable runs. They never alter an invocation already running. Completed runs are immutable; use `modelbench revise <task/run>` to create a new run from its owned artifact.

Set project defaults and explicitly allowed combinations in `[models.builder]` and `[models.verifier]` in the agent profile. Each table has `model`, `effort`, and `allowed_models` (model name to effort array). Run overrides take precedence per field. New runs do not inherit model/effort from global Codex settings. Unknown combinations are rejected before invocation; model availability errors leave the run incomplete and never cause substitution.

`modelbench status <task/run>` and the review app show active invocation settings and pending role settings. The review package includes immutable `model-settings.initial.json`, the append-only change/invocation history in `model-settings.json`, requested settings, configuration revisions, timestamps and available usage. Runtime confirmation and legacy provenance display **Not recorded** when unavailable. Builder claims are separate from independent measurements and verifier judgments.

## Tire benchmark

The static `goodyear_optitrac_lsw_1400_30r46` task models the tire alone using the
included Titan catalog dimensions and manufacturer reference image. It measures
2.107692 m exterior diameter and 1.343660 m width directly from evaluated meshes;
the verifier inspects lug count, tread shape and depth, and sidewall details.
The supplied DW rim-profile description differs from Titan's DH50HB designation;
no rim is modeled.

```powershell
modelbench validate-task goodyear_optitrac_lsw_1400_30r46
modelbench run goodyear_optitrac_lsw_1400_30r46 --agent codex --verifier codex `
  --builder-model gpt-6-astra --builder-effort high `
  --verifier-model gpt-6-astra --verifier-effort high
```

## Shader settings

Use scene-linear Base Color RGB **(0.18, 0.18, 0.18)**: 18% gray. Default Roughness is **0.36**. Agents may vary roughness from **0.20 to 0.80** to suggest material identity, with rubber commonly toward the rough end; glass may extend down to **0.10**. An intentional roughness of exactly 0.5 requires the Boolean material property modelbench_roughness_override = true, distinguishing it from Blender's untouched default. These settings belong in every saved model's connected surface shaders, not just the review render.

Every task declares the same source-material checks. Clean Blender reads evaluated material assignments and active shader inputs; missing materials, unsupported or linked inputs that cannot be established, incorrect gray, and out-of-policy roughness produce findings. Review rendering rebuilds every surface as neutral gray while preserving allowed authored roughness and records the resolved material values in blender_result.json. The world background is scene-linear RGB (0.12, 0.12, 0.12), slightly darker than the model calibration gray. Older run snapshots retain their original policy; use a revision run to apply new checks and render settings.

## Automated verification and recovery

Each submitted revision owns a new artifact and hash, preserved scripts, measurements, diagnostic renders, verifier evidence and correction records. A snapshotted acceptance checklist binds the task brief. Every declared measurement is evaluated in clean Blender. The linkage is sampled at frames 1-80. Mesh clearance records state their sampling and method limits explicitly.

Corrections move from **open -> builder addressed -> verifier resolved**, with reopening. Checkpoints record feedback delivery; they never resolve feedback. The builder can use `modelbench-agent self-check --source <workspace.blend>` and `modelbench-agent respond --task <id> --message <specific response>`.

```powershell
modelbench status car_trunk_four_bar/run_000005
modelbench steer car_trunk_four_bar/run_000005 --message "Inspect the lid attachment"
modelbench pause car_trunk_four_bar/run_000005
modelbench resume car_trunk_four_bar/run_000005
modelbench verify car_trunk_four_bar/run_000005
modelbench cancel car_trunk_four_bar/run_000005
modelbench revise car_trunk_four_bar/run_000004
```

Tasks that enable assembly-aware exploration default to three divergent concepts, four refinement rounds, and two complete concept cycles. The independent auditor records `ACCEPT`, `REFINE`, `NEW_CONCEPT`, or `BEST_AVAILABLE`; only `ACCEPT` can publish, while `BEST_AVAILABLE` returns the strongest candidate with unresolved findings. Legacy repair-only tasks retain their uncapped revision behavior. Infrastructure operations retry twice, then remain incomplete and resumable. Optional `run --budget-seconds N` limits active orchestration work; exhaustion leaves the run interrupted and never publishes. Intentional pauses do not consume budget; an abrupt restart does not invent unflushed active time. Resume a budgeted interrupted run with `resume RUN_ID --additional-budget-seconds N` to record an explicit additional grant. Cancellation cannot publish. Legacy runs retain their actual historical evidence; reverify them through a new revision run.

Canonical publication retains the 14 final views. Extra diagnostics and the front-view motion sequence live separately. `generated/<task>/current/review/` is a self-contained package with relative file references, artifact hashes, acceptance, corrections, provenance and evidence. Generated files stay ignored by Git.

Agent processes receive explicit paths and can register evidence, measurements, and owned checkpoints with `modelbench-agent`. Run `modelbench-agent --help` for that private command surface.

Generated data is protected by a marker and confirmation phrase:

```powershell
modelbench reset --all --dry-run
modelbench reset --all --confirm DELETE-ALL-GENERATED
```

See [docs/design.md](docs/design.md) for the contracts and lifecycle.

## Verification

Core tests use the standard library. Install the test extra for browser checks
(which use a locally installed Microsoft Edge); Blender integration tests require
the configured Blender executable:

```powershell
python -m pip install -e ".[test]"
python -m unittest discover -s tests -v
python -m compileall -q modelbench tests
```

An end-to-end production render additionally requires Blender 5.1 and the exact
RTX 3090 OptiX/ACES capabilities declared by the checked-in render profiles.

## Process isolation

ModelingBench minimizes inherited environment values, binds agent-facing commands
to a random per-turn capability. The Codex project profile uses
`--approve-for-me`: a workspace-write sandbox with permission requests routed
through automatic review. This execution policy is snapshotted for each new run;
existing run profiles remain unchanged. The Python harness does not itself create a standalone OS security
container around arbitrary agent profiles or Blender. Deployments that process
untrusted agents or `.blend` files must provide an external filesystem and
network sandbox for those processes.

## Sparse briefs and modular reference reconstruction

Named-model briefs can now discover and independently audit their own dimensions, parts, identity priors, and acceptance requirements. Start from the reviewer Brief tab or `modelbench run --brief "Named model and variant" --agent codex --verifier codex`. Optional `--reference` files and `--notes-file` supply user evidence.

The independently versioned reconstruction engine fits single-view cameras and unknown landmark dimensions, reports ambiguity and conditional uncertainty, and generates planar rectifications. Install `python -m pip install -e ".[reconstruction]"` (`[reference]` remains supported). `modelbench-reconstruct` exposes validate, solve, replay, compare, and two-environment regression; executions own immutable input/result receipts and archived engine artifacts. See [discovered-specification workflow](docs/discovered-specifications.md) and [reconstruction engine](docs/reconstruction.md) for contracts, capabilities, and validation. Existing v1 cases and tasks retain compatibility; matched reference views supplement the canonical publication views.
