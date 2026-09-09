# Perspective reconstruction

`modelbench.reconstruction` is a standalone, JSON-only sub-engine for fitting a
single camera to known 3D-to-2D correspondences. It does not import Blender or
ModelingBench lifecycle code.

## Case contract

Cases use `"case_version": "1.0"` and must explicitly declare image handling:

```json
{
  "case_version": "1.0",
  "image_coordinates": {
    "space": "normalized", "origin": "top-left", "orientation": "upright",
    "crop": [0, 0, 1, 1]
  },
  "camera": {"model": "perspective", "intrinsics": {"focal_length": 0.8}},
  "correspondences": [{"world": [0, 0, 0], "image": [0.5, 0.5]}],
  "source_hashes": {"reference.png": "sha256-or-external-identity"}
}
```

Image points are normalized after the declared crop and use an upright,
top-left origin. Perspective fitting needs at least six non-coplanar point
samples. Orthographic fitting needs four. The solver makes several bounded
robust least-squares starts; it returns `underconstrained` instead of inventing
a camera when those requirements are not met.

`constraints` may contain `point`, `curve`, `silhouette`, or `conic` terms;
the latter three supply sampled point correspondences. `metric_segment` records
the measured 3D length and error; it constrains geometry only when explicit `geometry_scale.solve` is enabled. `occlusion` and unknown terms are invalid in v1 because generic visibility requires a mesh/depth model. A plane may carry four planar
world/image pairs under `rectification` to return its plane-to-image homography.

## API and CLI

```python
from modelbench.reconstruction import solve
result = solve(case)  # JSON-safe dict, never writes files
```

```
python -m modelbench.reconstruction solve case.json --output result.json
python -m modelbench.reconstruction solve case.json --bundle case-bundle --image reference.png
python -m modelbench.reconstruction replay case-bundle --output replay.json
python -m modelbench.reconstruction compare result.json replay.json
```

Bundles include `case.json`, copied optional input images, and a hash manifest.
Receipts include case/source hashes, solver and dependency versions, bounded
solver configuration, residuals, term evidence, and runtime. `compare` rejects
different case versions, solver versions, or source identities rather than
silently treating them as comparable.

The solver requires optional NumPy and SciPy. Pillow is only identified in the
receipt today; image decoding is deliberately outside the pure solve API.
## Camera output and Blender bridge

The solver uses a right-handed **solver camera** frame. Its projection is
`u = cx + f * Xc/Zc`, `v = cy + f * Yc/Zc`; therefore `+Xc` is image-right,
`+Yc` is image-down, and visible points have `+Zc`. Perspective output states
its extrinsic exactly as `Xc = R @ Xworld + t`, where `R` is Rodrigues of
`camera.world_to_camera.rotation_vector` and `t` is
`camera.world_to_camera.translation`. `focal_length` is normalized by the
post-crop image width, and `principal_point` is post-crop normalized `[cx, cy]`.
The principal point is fixed input in v1; it is not estimated. Orthographic
output uses the same rotation but has `scale` and `image_offset`; it does not
claim a recoverable camera world translation because that is not observable.

For Blender, camera local axes are right/up/back (the camera looks down local
`-Z`). Define `B = diag(1, -1, -1)`. For a perspective result, the Blender
camera world position is `C = -R.T @ t`, and the Blender local-to-world rotation
matrix is `R_blender = R.T @ B`. This is the only coordinate-axis conversion;
the bridge must apply its scene-unit transform to `C` consistently with all
world correspondences. Convert normalized focal length to Blender lens only
with an explicit sensor-width convention: if `sensor_width = S` scene camera
units, `lens = S * focal_length`. Map normalized principal point through the
chosen Blender render/sensor-fit convention and verify it by reprojecting the
input correspondences. Do not treat `image_offset` as an orthographic camera
world location.

Curve, silhouette, and conic constraints are supported only as explicit known
3D-to-2D samples and receive positive per-sample or per-term `weight`. Metric
segments are measurement receipts, not optimization scale anchors. Occlusion
and every unrecognized constraint type are invalid in v1, so a case cannot be
reported solved while silently ignoring one. The receipt lists up to three
multi-start residual hypotheses; it does not certify that they exhaust all
single-view ambiguities.

`image_coordinates.crop` is `[left, top, width, height]` in normalized full-image
coordinates. Its default `frame` is `"crop"`, meaning correspondences already
use the crop frame. Set `frame: "full_image"` to have v1 convert each supplied
normalized full-image point to the crop frame before fitting. Points outside the
declared crop are not a supported correspondence convention.

Set `geometry_scale: {"solve": true}` only when each `metric_segment` measures
the same uniformly scaled case-world geometry. The returned scale multiplies all
case-world point coordinates before camera fitting. Its receipt records the
source coordinate system, assumption, residual, and consistency tolerance; an
inconsistent set returns `status: "inconsistent"`. Planes, parallel, and
perpendicular relations do not constrain the camera in v1: planar four-point
rectification is supported, while parallel/perpendicular and arbitrary plane
constraints are rejected as unsupported.
Receipts include `implementation_sha256`, computed from the reconstruction
package Python sources. Receipt comparison requires the same case/source
identity and contract version, but permits differing implementation digests for
benchmarking; it reports digest equality plus residual and runtime deltas.

Bundles reject symlinked inputs, unsafe manifest paths, duplicate input
basenames, and changed image or case bytes. Copied inputs use opaque hash-based
filenames. Verifiable priors `shared_point`, `parallel`, `perpendicular`,
`symmetry`, and `repetition` are checked only from explicit known coordinates
and appear as `verified` or `violated` receipts. They never add free geometry or
camera parameters in v1.