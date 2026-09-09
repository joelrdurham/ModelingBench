# Discovered specifications and reference reconstruction

Sparse briefs are v2 tasks with `specification_mode = "discovered"`. They carry identity, user notes and owned inputs, not a prewritten asset checklist. Start from the reviewer Brief tab or:

```powershell
modelbench run --brief "Named product and variant" --notes-file notes.txt --reference reference.jpg --agent codex --verifier codex
modelbench run goodyear_optitrac_sparse --agent codex --verifier codex
```

The legacy detailed tire and linkage tasks retain their original v1 contracts. Generated brief tasks live under `generated/briefs/tasks`; their run artifacts use the existing generated task/run layout.

## Discovery and independent review

Discovery runs a separate verifier coverage assessment before the builder proposal is available. The builder then returns a structured specification and captured sources; the verifier audits identity, source applicability, feature coverage, representation, priors, uncertainty, and every requirement. Schema rejection returns actionable correction feedback. Unresolved identity questions leave the run interrupted and visible in the reviewer; steer with the missing information and resume.

The schema is `schemas/discovered_spec.schema.json`; runtime validation additionally checks relationships, ownership cycles, evidence identities, selector coverage and measurement capabilities. Facts, observations, derivations, priors, assumptions and unknowns have different provenance. Priors require a basis, neighboring constraints, alternatives, a contradiction test and uncertainty. Hidden details can be reconstructed plausibly; critical unknown requirements cannot be approved.

Requirements select independent extent, repeated-feature, topology, reference or visual inspection. Unsupported deterministic checks cannot be marked passed by a visual assertion. Custom inspection scripts remain agent evidence, not trusted evaluator code.

`discovery/versions` holds immutable specification, audit and compiled contract files. The original task snapshot and `acceptance.json` are never rewritten. `modelbench-agent validate-spec --file spec.json` checks an amendment; `propose-spec --file spec.json --reason "new evidence"` queues independent approval. Rejected amendments retain their provenance for correction. Submitted artifacts, measurements and verdicts bind the approved specification hash. Pending amendments block publication.

Canonical framing is frozen from the first independent evaluated bounds for a specification unless the specification defines fixed framing. It is a camera aid, not a dimensional acceptance limit. Dimensional constraints are checked separately.

## Modular reconstruction engine

Install `python -m pip install -e ".[reference]"`. The standalone package `modelbench.reconstruction` has no Blender or run-lifecycle imports. See [engine API and replay cases](reconstruction.md) for supported projections, constraint capabilities and coordinate conventions.

```powershell
python -m modelbench.reconstruction solve case.json --output result.json
python -m modelbench.reconstruction replay case-bundle
python -m modelbench.reconstruction compare old-result.json new-result.json
```

Cases and receipts include source/case hashes, engine implementation hash, dependency versions, optimization settings, identifiability, competing camera hypotheses and residual terms. Uniform scale can be derived from consistent metric segments. Known-geometry priors are verified separately. Arbitrary surface reconstruction and automatic lens distortion are not inferred by this engine. Underconstrained or unsupported cases remain explicit; agents can seek better references or propose auditable visual criteria.

The Blender bridge consumes the approved camera and evaluates actual model geometry. It writes neutral transparent matched views, real compositor depth EXR, surface-bound landmarks and visibility evidence. The harness compares these with audited annotations to produce point/curve errors, silhouettes, conic residuals, repeated-pattern phase, occlusion mismatches and overlays. Reconstruction residuals and model-to-reference residuals are separate records. Cameras are not silently refit to each model revision.

## Research and reproducibility

Live profiles declare image inspection and research capabilities. The checked-in Codex profile explicitly enables live web search and disables host skill discovery, plugins, apps, memories and parent AGENTS context. These settings are snapshotted per run; authentication remains provider-managed. The harness is not an additional OS security container.

Captured sources include original URL, owned bytes, retrieval time, hash, source location and transcription status. Source documents are evidence rather than execution instructions. Offline replay requires an explicitly network-disabled profile declaring the offline capability; the live profile cannot silently serve as offline. Engine case replay itself needs no agent or network.

Codex configuration reference: https://learn.chatgpt.com/docs/config-file/config-reference . CLI feature support was checked against the installed executable during implementation.

## Quality gates and tests

A discovered run publishes only with an approved intact specification, complete critical coverage, passing independent geometry/measurement/reference gates, an independent ACCEPT and the existing 14 canonical renders. Non-dominated candidates remain available as seeds; incompatible specification versions are not compared. BEST_AVAILABLE requires a documented stopping reason and never publishes. Existing budget, pause, cancellation and retry behavior remains in force.

Run `python -m unittest discover -s tests -v`. Reconstruction and residual tests require the reference extra. Browser checks use the existing test extra and Edge. Set `MODELBENCH_BLENDER_INTEGRATION=1` to run the rectangular, shifted camera bridge comparison against Blender. Small strict-profile smoke fixtures under `generated/validation/reference_bridge` verify perspective, orthographic, transparent render and depth output without adding asset-specific core code.
