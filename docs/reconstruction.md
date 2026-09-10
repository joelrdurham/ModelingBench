# Perspective measurement reconstruction engine

`modelbench.reconstruction` is an independently callable, versioned single-view
landmark-and-constraint engine in the ModelingBench distribution. It fits cameras
and unknown landmark coordinates, rectifies declared planes, and reports the
dimensions or ratios that the supplied evidence supports. It does not infer asset
identity, detect features, generate meshes, or solve joint multi-view geometry.

Install `python -m pip install -e ".[reconstruction]"`. The existing `[reference]`
extra remains an alias with the same NumPy, SciPy and Pillow dependencies.

## Public API and versioning

```python
from modelbench.reconstruction import validate_case, solve, execute, replay

normalized = validate_case(case)     # raises ReconstructionError on invalid input
result = solve(case)                # in-memory JSON; never writes files
bundle = execute(case, "new-run", {"front": "reference.png"})
replayed = replay(bundle, "new-replay", python_executable=".venv/Scripts/python.exe")
```

The case and result contract versions are `2.0`; the engine version is `2.0.0`.
The receipt separately identifies the exact Python-source implementation digest.
`core` imports remain compatible. Historical `case_version: "1.0"` inputs use an
explicit compatibility executor, preserving independent normalized X/Y focal
semantics, crop handling, sampled constraints and existing status behavior. New
receipts identify this adaptation and hash the original case. Historical inputs
and receipts are never rewritten. Legacy convergence/uncertainty is explicitly
unavailable where the old contract did not record it.

The authoritative value/reference validation is `contracts.validate_case`;
`schemas/reconstruction_case_v2.schema.json` and the result schema describe the
interchange shape. Numerical modules have no Blender or lifecycle imports.
Filesystem execution, dependency archiving and image output are separate adapters.

## V2 coordinates and minimal case

World and camera frames are right handed. Extrinsics are
`X_camera = R @ X_world + translation`, with Rodrigues rotation vector `R`.
Camera axes are right/down/forward; visible perspective points have positive Z.
Pixel coordinates are continuous edge coordinates: the first pixel center is
`(0.5, 0.5)`. Intrinsics always describe the oriented crop in **pixels**:

```
perspective:   u = cx + f * Xc/Zc; v = cy + f * aspect_ratio * Yc/Zc
orthographic:  u = cx + s * Xc;    v = cy + s * aspect_ratio * Yc
```

`aspect_ratio` is the fixed ratio `fy/fx` (or `sy/sx`), default 1. Rectangular image
dimensions do not change this ratio. Focal length and orthographic scale must be
explicitly supplied. Principal point defaults to the crop center. Numbers/vectors
are fixed parameters; unknown camera parameters use `{initial, bounds}`. Omitted
extrinsics are unknown, with documented initial rotation zero, translation
`[0,0,5]`, rotation bounds ±2π and translation bounds ±1e6.

```json
{
  "case_version": "2.0",
  "units": "m",
  "world_frame": {"handedness": "right"},
  "images": [{"id": "front", "width": 640, "height": 480,
              "orientation": "upright", "crop": [0, 0, 1, 1]}],
  "landmarks": [
    {"id": "origin", "coordinates": [0, 0, 0]},
    {"id": "end", "coordinates": [null, 0, 0], "initial": [1, 0, 0],
     "bounds": [[0, -1, -1], [10, 1, 1]]}
  ],
  "observations": [
    {"id": "end_pixel", "landmark_id": "end", "image_id": "front",
     "image": [520, 240], "space": "pixels", "frame": "source", "sigma": 0.5}
  ],
  "camera": {
    "model": "perspective",
    "intrinsics": {"focal_length": 500, "principal_point": [320, 240]},
    "world_to_camera": {"rotation_vector": [0, 0, 0], "translation": [0, 0, 5]}
  },
  "measurements": [{"id": "length", "type": "distance", "landmarks": ["origin", "end"]}],
  "solver": {"seed": 7, "starts": 4, "uncertainty_samples": 32}
}
```

This example estimates 2 m **conditional on the fixed metric camera and line
assumptions**. An initial value alone supplies no scale evidence.

Each owned image has an ID, dimensions, and lowercase `sha256`. Synthetic in-memory
cases may omit the digest. Execution requires the actual bytes for every declared
digest. One image per case is supported. Orientation is `upright`, `rotate_90_cw`,
`rotate_180`, or `rotate_270_cw`. Crop is normalized `[left, top, width, height]`
in the oriented image. Observations declare `pixels`/`normalized` and
`source`/`oriented`/`crop`; the engine transports coordinates and diagonal
uncertainty through the invertible affine transform. V2 applies only the declared
orientation; it does not silently reinterpret EXIF orientation.

Landmark coordinates are fixed numbers or `null` unknowns, with initial values and
optional finite bounds. Default coordinate bounds are ±1e6 world units. Case units
are explicit; the default label `arbitrary` does not imply a metric calibration.
Known coordinates are supplied evidence/assumptions, not discovered facts.

## Constraints, measurements and limitations

All records have stable IDs. Observations require positive `sigma` or `tolerance`;
only an explicit statistical `sigma` supports perturbation intervals. Optional
positive `weight` scales the residual. Geometry terms use positive `tolerance`
(default 1e-6 in their declared units), optional `weight`, `required` (default true),
and optional `assumption_ids` referring to the case's `assumptions` array.

| Constraint | Required fields |
| --- | --- |
| `point_reprojection` | Same fields as an observation |
| `curve` | `samples`: explicitly paired landmark observations |
| `fixed_coordinate` | `landmark_id`, `coordinates` (null components ignored) |
| `distance` | `landmarks: [a,b]`, positive `value` |
| `ratio` | `segments: [[a,b],[c,d]]`, positive `value` |
| `coplanarity` | At least four distinct `landmarks` |
| `collinearity` | At least three distinct `landmarks` |
| `parallelism`, `perpendicularity` | `segments: [[a,b],[c,d]]` |

Distance constraints are active metric anchors when case units are metric.
Requested `measurements` support distance and ratio with the same landmark fields.
Unresolved measurements have `value: null`, with a labeled `conditional_value`
for diagnostics. A missing global scale can leave ratios identifiable.

Generic silhouette fitting, conic inference, visibility solving and lens-distortion
estimation are unsupported. Required unsupported terms fail validation. Optional
unsupported terms appear in `unsupported_constraints`; they contribute no evidence.
Candidate silhouette/visibility diagnostics in the harness are distinct operations.

## Identifiability, hypotheses and uncertainty

`mathematical_status` and `convergence_status` are separate. A converged optimizer
may still be underconstrained, inconsistent, ambiguous or geometrically invalid.
The engine checks positive depth for all landmarks and reports landmark-keyed
depths, active parameter bounds, standardized per-constraint residuals, and ranked
conflicts, including terms suppressed by robust loss.

Rank analysis uses finite differences of the raw standardized **evidence** residual
with scaled parameter columns. Numerical depth penalties, declared gauge-fixing
terms and search bounds never count as equations of measured evidence. A
`fixed_coordinate` with `role: "gauge"` stabilizes optimization but remains excluded
from rank and measurement identifiability; infeasible gauges invalidate the fit.
Reports separate rigid world-frame gauge, orthographic axial-camera placement
gauge, scale ambiguity and other unresolved parameter combinations. Orthographic
camera depth is unobservable and does not create distinct image hypotheses;
relative landmark depths remain part of each hypothesis. Degenerate geometric
bases are explicitly flagged.

Multistart is deterministic for the effective seed/settings. Physical hypotheses
are deduplicated using camera-frame geometry, including removal of an identified
scale gauge. Comparable valid alternatives remain separate. This is a finite search
and local Jacobian analysis, not a proof of globally unique reconstruction.

`solver.uncertainty_samples` enables seeded observation perturbation and refitting
for each valid hypothesis. An interval requires identifiable measurements, supplied
observation sigma, successful refits and retention of the same physical branch.
Refits must also stay inside `solver.uncertainty_branch_radius` (default 0.25 in
normalized camera-frame signature distance) and remain nondegenerate/gauge-feasible.
Intervals are never pooled across hypotheses. All are conditional on the supplied
correspondences, uncertainties, constraints and selected hypothesis; correspondence
or assumption mistakes are not calibrated by these intervals.

## Rectification

Every entry in `planes` returns an available/unavailable description. Use either
four or more coplanar landmark IDs, or `plane_points` and paired `image_points`
with explicit image `space`/`frame`. `output_scale` is pixels per plane unit
(default 100); `output_size` and plane bounds are explicit. The engine returns
`homography_plane_to_crop_pixels`. The execution adapter writes PNGs, validity
masks, and a reprojection overlay. Degenerate or oversized rectifications are
reported unavailable. Output is limited to 8192 per axis and 32 megapixels.

## Executions, replay and regression

```
modelbench-reconstruct validate case.json
modelbench-reconstruct solve case.json --destination run-001 --image front=reference.png
python -m modelbench.reconstruction replay run-001 --destination replay-001 --python .venv/Scripts/python.exe
modelbench-reconstruct compare run-001 replay-001
modelbench-reconstruct regress --suite suite.json --left-python old-env/python --right-python new-env/python --destination regression-001
```

`--bundle` remains an alias for `--destination`. Without a destination, solve/replay
uses a new unique directory. `--output` is an optional result summary copy.
Executions contain raw case, normalized diagnostic case, copied image inputs,
result, diagnostics, complete hash manifest, completion receipt, an installable
standalone engine wheel, exact dependency pins and Python/platform identity.
Image filenames derive from stable IDs/order and hashes, so duplicate source
basenames are safe. Invalid inputs, solver failures and interruptions retain
receipts once a staging directory exists. Publication of the directory is atomic;
completed executions cannot be overwritten.

Replay verifies file inventory, hashes, regular-file ownership, hard links,
symlinks/reparse ancestors and confined paths. It uses the original raw case.
Matching current code can replay directly; otherwise the archived engine is loaded
in the explicitly supplied matching Python environment. Different Python/platform
or dependency versions, or missing archived code, produce `unavailable`. Replay
never silently substitutes the installed engine and never installs dependencies.
The receipt is a checksum root; harness approval additionally binds its hash.

Compare accepts different engine versions/digests for identical case/source
identities and reports measurements, uncertainty, hypotheses, constraints, status
and runtime. Different cases require `--migration mapping.json` containing exact
`left_case_hash`, `right_case_hash`, `measurement_ids` mappings, and an explicit
`source_hashes: {left, right}` mapping if source identity changes.

A suite has `cases: [{id, case, images, ground_truth, tolerances}]`. `case` is a JSON
path or object; image paths are keyed by image ID. Relative paths resolve from the
suite file. `ground_truth` may specify `status` and measurement values or
`{value, tolerance}` objects. Truth is stored separately and never enters solver
inputs. Both explicit Python executables run isolated subprocess commands with
argument arrays. Regression stores both executions and a machine-readable report;
runtime is reported separately from correctness gates.

## Harness and review

The harness resolves image identities against owned evidence and executes cases
before specification audit. The auditor receives assumptions, residuals,
alternatives, unresolved measurements and `reconstruction_evidence_sha256`, which
must be echoed to approve the exact executions. The compiled contract binds the
execution, case, result, engine, feature-binding and selected-hypothesis identities.
`modelbench-agent submit-reconstruction --file case.json --evidence ev_0001` uses
the existing per-turn capability and records a claim pending specification audit.

The separate Blender bridge imports only `MODEL`, uses evaluated vertices for
bindings, explicitly converts pixel intrinsics and camera axes, and keeps the
approved camera fixed. Harness cases use meters in model-world coordinates
(+Z up, -Y front). Reference bindings use normalized crop image targets. Camera
or hypothesis changes require a specification amendment. Engine upgrades produce
new evidence; applying that evidence to candidates requires explicit reevaluation
in a revision run. Existing completed runs retain their evidence and history.
Explicit hypothesis selection carries that branch's complete measurements,
residuals, landmarks and observability assessment. Selected-branch images are
generated separately and labeled with the hypothesis ID; the original execution
and its diagnostics remain unchanged.

Each reference requirement compiles to separate reconstruction-support and
model-to-reference checks. A low reconstruction residual cannot satisfy model
agreement. Identity changes reject cached candidate assessments. Unsupported
comparisons are unassessed, with reconstruction-assumption findings separate from
model-geometry findings. Publication still requires the existing gate and auditor
acceptance. Matched views are additional diagnostics; the 14 canonical publication
views remain unchanged. The status API, viewer and exported package expose both
evidence categories, including rectification and overlays.

## Verification

```
python -m unittest discover -s tests -p "test_reconstruction*.py" -v
python -m unittest tests.test_discovery tests.test_reference tests.test_server -v
```

Set `MODELBENCH_BLENDER_INTEGRATION=1` to run the analytic Blender projection tests
(0.5-pixel limit). Engine tests use `unittest` and do not require Blender. The
acceptance suite covers coordinates, camera/depth/scale/ratios, uncertainty,
degeneracy, ambiguity, conflicts, bounds, failed convergence, tampering, archived
replay, regression truth isolation, cache identities, approval separation and
self-contained review export. Browser tests use the repository's installed Edge
and Playwright pattern. Full production rendering additionally requires the
existing configured Cycles/OptiX hardware profile.
