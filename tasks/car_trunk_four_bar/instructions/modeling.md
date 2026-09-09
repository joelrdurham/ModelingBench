# Modeling and mechanism requirements

## Kinematics

- Treat A-B as the fixed ground link, A-C as the driven input link, C-D as the rigid lid coupler, and B-D as the follower.
- Resolve A-B directly in CHASSIS_DECK_SURROGATE. Do not create LINK_GROUND_AB plates or other duplicate decorative ground links; a separate AB subframe is outside this preferred benchmark strategy.
- Solve the upper circle intersection for D. Key the mechanism at every integer frame from 1 through 80 so all four center-to-center lengths remain constant during playback.
- Add a clearly named controller object with an `open_factor` custom property from 0.0 closed to 1.0 open. The animation and controller must make the intended range legible even if the implementation uses baked transforms.
- Make frame 1 closed, frame 63 a readable partially open inspection pose, and frame 80 open. Set the source scene frame range to 1-80.
- Avoid branch flips, crossed links, pin separation, and visible collisions across the sampled motion.

## Mechanical construction

- Use separate, physically plausible moving components. Do not fuse links, pins, washers, or the lid into one decorative mesh.
- Use paired 6 mm steel plate links arranged in double shear, with radiused or tapered load-path profiles rather than plain rectangular bars.
- Use nominal 12 mm pivot pins, approximately 24-30 mm outside-diameter reinforced eyes or bosses, and visible spacers/thrust washers or collars. Keep holes and lightening details away from pin-eye load paths.
- Cross-tie paired plates and give the body bracket a plausible boxed, folded, or welded support into a simple car-deck surrogate.
- Model a rigid trunk-lid surrogate attached to C-D, approximately 1.1 m long by 0.68 m wide, with a thin outer panel and reinforcing return or inner beam. Keep the lid and its hardware inside the task bounds through the full animation.
- Distinguish retained pins from permanent cross-ties. Apply modest bevels to manufactured edges and use smooth shading where appropriate.


## Material policy

- Every polygon-used material must use a directly connected Principled BSDF or Glass BSDF at Material Output. Set its untextured linear base-color RGB to (0.18, 0.18, 0.18) (plus or minus 0.001).
- Use roughness 0.36 by default. You may vary roughness from 0.20-0.80 to hint at material identity; rubber commonly uses the rougher end. A material named glass, a Glass BSDF, or a Principled BSDF with literal transmission may also use roughness down to 0.10. An intentional roughness of 0.5 requires the Boolean material property modelbench_roughness_override = true. Do not link color or roughness inputs: the source inspection rejects textures and procedural bypasses.

## Validation and review evidence

- Generate `linkage_validation.json` in the workspace. Record A, B, C, and D for every frame, the four measured link lengths, maximum absolute length error, assembly-branch continuity, and any clearance checks performed.
- Include in checkpoint observations a concise load-path review: pin-eye reinforcement, paired-plate stability, cross-ties, retention, likely interference, and the conceptual 77 N*m per-hinge gravity moment. State clearly that this is not structurally certified.
- Create any build scripts inside the workspace and invoke the exact Blender executable supplied in the run context or `MODELBENCH_BLENDER`. Blender must run with factory startup and auto-execution disabled.
- The final submission must be self-contained: no external libraries or unpacked external assets. Keep all visible geometry and rig/controller objects required by the mechanism under `MODEL`.
