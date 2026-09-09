# Goodyear Optitrac LSW 1400/30R46 tire

Create a static, unladen, tire-only Blender model of the Goodyear Optitrac LSW 1400/30R46 R-1W radial agricultural tire, catalog number G0PMS6. Use meters. Put the tire center at `(0, 0, 1.053846)`, place the axle/opening axis along Y, and save a single-frame scene at frame 1. The task has no animation requirement and no wheel, rim, hub, valve, tractor, ground deformation, or other support geometry.

The official Titan/Goodyear Farm Tires catalog values are controlling: overall width 52.90 in = **1.343660 m**, overall diameter 82.98 in = **2.107692 m**, tread depth 83/32 in = **0.06588125 m**, and 48 lugs. These are target dimensions for this model, with the task tolerances in `instructions/modeling.md`; they are modeling acceptance tolerances, not manufacturer manufacturing tolerances.

The `46` in the tire-size label is a nominal rim-diameter designation (1.168400 m). It is distinct from the 2.107692 m exterior tire diameter, but this task does not independently verify an exact bead or opening profile. The manufacturer catalog specifies recommended rim `DH50HB`; the supplied DWprofile label differs from that catalog notation. Do not resolve that discrepancy by inventing rim engineering: model no rim.

Place all visible model geometry in a non-empty `MODEL` collection, plus an object named `ANCHOR_tire_center` at the prescribed center. The measured overall extents apply to all evaluated mesh geometry in `MODEL`; do not add a duplicate proxy mesh solely for measurement. If sidewall and tread are separate real meshes, name them `TIRE_SIDEWALL` and `TIRE_TREAD_LUGS`. Keep the model self-contained with no external asset dependency.

Use the supplied catalog snapshot and the local manufacturer product-line image as visual evidence. If research finds a more specific manufacturer image, record its URL and use it only as supplemental visual evidence; it must not override the catalog dimensions.

Submit only after a review checkpoint and any pending feedback are complete. The final evaluation will render all 14 canonical views: `front`, `back`, `left`, `right`, `top`, `bottom`, and `azimuth_000`, `azimuth_045`, `azimuth_090`, `azimuth_135`, `azimuth_180`, `azimuth_225`, `azimuth_270`, `azimuth_315`.
