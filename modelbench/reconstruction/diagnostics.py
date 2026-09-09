"""Planar homographies (pure math) and a separate image artifact adapter."""
from __future__ import annotations

import math
from pathlib import Path

from .contracts import ReconstructionError


def homography(plane_points, image_points):
    import numpy as np
    def normalized(points):
        points = np.asarray(points, dtype=float)
        center = points.mean(axis=0)
        scale = math.sqrt(2) / max(float(np.mean(np.linalg.norm(points-center, axis=1))), 1e-12)
        transform = np.array([[scale, 0, -scale*center[0]], [0, scale, -scale*center[1]], [0,0,1]])
        return (points-center)*scale, transform
    a, ta = normalized(plane_points)
    b, tb = normalized(image_points)
    rows = []
    for (x,y), (u,v) in zip(a,b):
        rows.extend([[x,y,1,0,0,0,-u*x,-u*y,-u], [0,0,0,x,y,1,-v*x,-v*y,-v]])
    matrix = np.asarray(rows)
    if np.linalg.matrix_rank(matrix, tol=1e-10) < 8:
        raise ReconstructionError("degenerate rectification point correspondences")
    _, _, vt = np.linalg.svd(matrix)
    h = np.linalg.inv(tb) @ vt[-1].reshape(3,3) @ ta
    if abs(np.linalg.det(h)) < 1e-15 or abs(h[2,2]) < 1e-15:
        raise ReconstructionError("singular rectification homography")
    return h / h[2,2]


def rectifications(case, camera, points):
    import numpy as np
    from .coordinates import to_crop_pixels
    from .projection import project
    out = []
    for plane in case["planes"]:
        record = {"plane_id": plane["id"], "image_id": case["images"][0]["id"],
                  "output_scale": plane["output_scale"], "output_scale_units": "pixels per plane unit"}
        try:
            r = plane.get("rectification", plane)
            if "plane_points" in r:
                xy = np.asarray(r["plane_points"])
                uv = [to_crop_pixels(p, space=r.get("space", "pixels"), frame=r.get("frame", "crop"), image=case["images"][0]) for p in r["image_points"]]
            else:
                world = np.asarray([points[i] for i in plane["landmarks"]])
                x = world[1] - world[0]
                normal = np.cross(x, world[2]-world[0])
                if min(np.linalg.norm(x), np.linalg.norm(normal)) < 1e-10:
                    raise ReconstructionError("degenerate plane basis")
                x /= np.linalg.norm(x); normal /= np.linalg.norm(normal)
                y = np.cross(normal, x)
                deviation = float(np.max(np.abs((world-world[0]) @ normal)))
                if deviation > float(plane.get("tolerance", 1e-6)):
                    raise ReconstructionError("declared plane landmarks are not coplanar")
                xy = np.column_stack(((world-world[0])@x, (world-world[0])@y))
                uv, depth = project(camera, world)
                if camera["model"] == "perspective" and np.any(depth <= 0):
                    raise ReconstructionError("plane lies behind the camera")
                record["basis"] = {"origin": world[0].tolist(), "x_axis": x.tolist(), "y_axis": y.tolist()}
            h = homography(xy, uv)
            low, high = xy.min(axis=0), xy.max(axis=0)
            size = np.ceil((high-low)*plane["output_scale"]).astype(int)
            if min(size) <= 0 or max(size) > 8192 or int(size[0])*int(size[1]) > 32_000_000:
                raise ReconstructionError("rectification output size must be positive and <=8192 per axis / 32MP")
            record.update(status="available", homography_plane_to_crop_pixels=h.tolist(),
                          plane_bounds=[low.tolist(), high.tolist()], output_size=size.tolist(), artifacts={})
        except (ReconstructionError, ValueError) as exc:
            record.update(status="unavailable", reason=str(exc))
        out.append(record)
    return out


def write_artifacts(case, result, input_images, destination):
    """Filesystem adapter. Return references; never mutate a saved case/receipt."""
    import numpy as np
    from PIL import Image, ImageDraw
    from scipy.ndimage import map_coordinates
    from .coordinates import from_crop_pixels, crop_size
    root = Path(destination); root.mkdir(parents=True, exist_ok=True)
    if case.get("case_version") != "2.0" or not input_images:
        return []
    im = case["images"][0]
    path = input_images.get(im["id"])
    if not path:
        return []
    with Image.open(path) as opened:
        source = np.asarray(opened.convert("RGB"))
    # Crop pixels to raw source pixels is affine even with orientation.
    kwargs = {"space": "pixels", "frame": "source", "image": im}
    origin = np.asarray(from_crop_pixels([0,0], **kwargs))
    matrix = np.column_stack([np.asarray(from_crop_pixels(p, **kwargs))-origin for p in ([1,0],[0,1])])
    artifacts = []
    for index, rect in enumerate(result.get("rectifications", [])):
        if rect.get("status") != "available": continue
        width, height = rect["output_size"]
        low, high = np.asarray(rect["plane_bounds"])
        ys, xs = np.mgrid[:height, :width]
        xy = np.stack((low[0]+(xs+.5)/rect["output_scale"], low[1]+(ys+.5)/rect["output_scale"], np.ones_like(xs))).reshape(3,-1)
        projected = np.asarray(rect["homography_plane_to_crop_pixels"]) @ xy
        safe = np.abs(projected[2]) > 1e-12
        uv = projected[:2] / np.where(safe, projected[2], 1)
        raw = matrix @ uv + origin[:,None]
        cw, ch = crop_size(im)
        valid = safe & (uv[0]>=0) & (uv[0]<cw) & (uv[1]>=0) & (uv[1]<ch) & (raw[0]>=0) & (raw[0]<source.shape[1]) & (raw[1]>=0) & (raw[1]<source.shape[0])
        coords = [raw[1]-.5, raw[0]-.5]
        pixels = np.stack([map_coordinates(source[:,:,c], coords, order=1, mode="constant") for c in range(3)], axis=-1)
        pixels[~valid] = 0
        name = f"plane_{index:04d}"
        Image.fromarray(pixels.reshape(height,width,3).astype("uint8")).save(root/(name+".png"))
        Image.fromarray(valid.reshape(height,width).astype("uint8")*255).save(root/(name+"_mask.png"))
        artifacts.append({"plane_id": rect["plane_id"], "image": name+".png", "validity_mask": name+"_mask.png", "output_scale": rect["output_scale"]})
    overlay = Image.fromarray(source.copy())
    draw = ImageDraw.Draw(overlay)
    for observation, projected in zip(case.get("observations", []), result.get("projections", [])):
        from .coordinates import to_crop_pixels
        observed = to_crop_pixels(observation["image"], space=observation.get("space", "pixels"), frame=observation.get("frame", "source"), image=im)
        a, b = matrix @ observed + origin, matrix @ projected + origin
        draw.line((*a, *b), fill="red", width=2)
        draw.ellipse((a[0]-3,a[1]-3,a[0]+3,a[1]+3), outline="green", width=2)
    overlay.save(root/"reprojection.png")
    artifacts.append({"type": "reprojection", "image": "reprojection.png"})
    return artifacts
