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
the measured 3D length and error. `occlusion` is retained in the receipt as
validated-only because generic surface visibility needs a mesh/depth model.
Unknown terms are reported as unsupported. A plane may carry four planar
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
