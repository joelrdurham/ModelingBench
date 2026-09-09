# Modeling and verification instructions

## Construction

- Build a smooth, inflated unladen agricultural-tire carcass with broad rounded shoulders, distinct sidewalls, bead/opening region, and a continuous R-1W-style agricultural tread. Keep its central plane at Y=0 and its axle/opening axis on Y.
- Reproduce 48 individual principal tread lugs in total around the tire (count each left/right shoulder lug separately, not each pair). Use the manufacturer description as qualitative guidance: continuous helical lug curvature and tuned asymmetric lug shape. Make individual lugs legible in the 14 canonical views; do not substitute a flat displacement-only tread or a repeated rectangular strip.
- Make the tread depth visibly approximately 0.06588125 m, measured radially from the tread base to a representative lug crown. Name each principal lug `TIRE_LUG_01` through `TIRE_LUG_48` (case-insensitive prefix matching is evaluated); `TIRE_SIDEWALL` may name the carcass. Do not create a duplicate complete-tire or measurement-proxy mesh.
- Include restrained embossed sidewall branding and size marking that reads `GOODYEAR`, `OPTITRAC`, and `LSW 1400/30R46`. Do not claim exact construction details, load rating, pressure, speed, or rim profile beyond the supplied catalog data.
- No rim or rim-like placeholder is permitted. The opening may show bead/interface geometry only; it must not be filled by a fabricated wheel component.

## Acceptance targets

- Independently measure all evaluated `MODEL` mesh geometry at frame 1. Its exterior X/Z diameter target is **2.107692 m** and its exterior Y width target is **1.343660 m**. The deterministic extent checks use a ±0.010 m modeling tolerance; this is not a statement of manufacturer tolerance.
- The tire must be unladen: preserve a circular exterior silhouette nominally tangent to Z=0 at the prescribed center height. Do not flatten the contact patch.
- The tire-center anchor is accepted within 0.020 m of `(0, 0, 1.053846)`. The declared evaluation envelope is a containment check only; it does not replace the target-extent inspection.
- The 46 in size term is a nominal designation (**1.168400 m**) and must not be conflated with the 2.107692 m exterior diameter. No exact bead or opening diameter is required because the available catalog evidence does not define that cross-section geometry.


## Material policy

- Every polygon-used material must use a directly connected Principled BSDF or Glass BSDF at Material Output. Set its untextured linear base-color RGB to (0.18, 0.18, 0.18) (plus or minus 0.001).
- Use roughness 0.36 by default. You may vary roughness from 0.20-0.80 to hint at material identity; rubber commonly uses the rougher end. A material named glass, a Glass BSDF, or a Principled BSDF with literal transmission may also use roughness down to 0.10. An intentional roughness of 0.5 requires the Boolean material property modelbench_roughness_override = true. Do not link color or roughness inputs: the source inspection rejects textures and procedural bypasses.

## Required evidence and verifier inspection

- Before final submission, create a checkpoint using at least `front`, `right`, `top`, and `azimuth_045`. In the checkpoint observation, record measured exterior diameter, width, representative radial tread depth, lug count, and whether the silhouette remains circular without a flattened contact patch.
- The deterministic driver also checks the 48 named lug meshes, their angular spacing, and radial crown projection depth. It cannot infer helical/asymmetric lug shape or bead geometry. These requirements require independent verifier-agent inspection of the model and canonical renders; do not mark them as machine-verified.
- The verifier must inspect: (1) exactly 48 individual principal tread lugs around the circumference, (2) helical/asymmetric agricultural-lug silhouette and continuity, (3) representative radial tread depth 0.06588125 m within a modeling tolerance of 0.008 m, (4) unladen circular silhouette, (5) tire-only scope with no fabricated rim, and (6) sidewall size/brand legibility without unsupported claims.
