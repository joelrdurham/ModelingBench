# ModelingBench

ModelingBench is an auditable orchestration harness for iterative Blender modeling agents. It snapshots task inputs, owns every checkpoint and submission, records evidence and human steering, and evaluates only a collection named `MODEL` in a clean Blender scene.

The production render profiles are deliberately strict: Blender 5.1, Cycles, OptiX on an RTX 3090, GPU denoising, and ACES 2.0. Missing production capabilities are errors, never silent fallbacks.

## Quick start

```powershell
python -m pip install -e .
modelbench validate-task example_drawing_object
modelbench run example_drawing_object --agent codex
modelbench status
```

Agent processes receive explicit paths and can register evidence, measurements, and owned checkpoints with `modelbench-agent`. Run `modelbench-agent --help` for that private command surface.

Generated data is protected by a marker and confirmation phrase:

```powershell
modelbench reset --all --dry-run
modelbench reset --all --confirm DELETE-ALL-GENERATED
```

See [docs/design.md](docs/design.md) for the contracts and lifecycle.

## Verification

The repository test suite uses only the standard library:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q modelbench tests
```

An end-to-end production render additionally requires Blender 5.1 and the exact
RTX 3090 OptiX/ACES capabilities declared by the checked-in render profiles.

## Process isolation

ModelingBench minimizes inherited environment values, binds agent-facing commands
to a random per-turn capability, and the Codex profile requests a workspace-write
sandbox. The Python harness does not itself create a standalone OS security
container around arbitrary agent profiles or Blender. Deployments that process
untrusted agents or `.blend` files must provide an external filesystem and
network sandbox for those processes.
