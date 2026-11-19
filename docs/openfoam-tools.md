# OpenFOAM tools

`sim` and `of` run OpenFOAM v2512 (openfoam.com) in Docker, in the official image
`opencfd/openfoam-default:2512` unless `OF_IMAGE` names another. The repository
root is mounted at `/work` and `of` runs there, so `cases/<name>` is the same path
on the host and in the container.

| Command | Use it for | Output |
|---|---|---|
| `sim <subcommand>` | routine work on a case | one JSON object; exit code 0 only if `"ok": true` |
| `of <command...>` | anything `sim` does not cover | raw OpenFOAM output |

```
sim tutorials [WORDS...]          find a starting template (path and solver)
sim new NAME --from TUTORIAL      copy it to cases/NAME
sim info NAME                     solver, time control, files, fields, patches
sim get NAME FILE [ENTRY]         read a dictionary or one entry
sim set NAME FILE ENTRY VALUE     change one entry (returns old and new)
sim run NAME [opts] -- CMD ARGS   run in the case directory, logged to log.<CMD>
sim mesh NAME                     checkMesh: cells, non-orthogonality, skewness
sim status NAME                   solver log: state, residuals, Courant, errors
sim plot NAME [--fields U p] [--time T] [--window A0 A1 B0 B1]
                                  PNG of a 2D result, with min/max/mean per field
sim view NAME                     open the case in ParaView (macOS)
sim clean NAME                    delete mesh, results and logs; keep the setup
```

`sim run` options go before `--`: `--timeout SEC`, `--np N` (parallel), `--sh`
(run the command as a shell line), `--log NAME`, `--tail N`. Entries are
addressed with slashes: `SIMPLE/residualControl/p`, `boundaryField/inlet/value`,
`solvers/p/tolerance`.

## Workflow

1. **Choose a template.** Start from the closest tutorial (`sim tutorials
   incompressible`, `sim tutorials interFoam laminar`), not from scratch. The
   template fixes the solver: `simpleFoam` for steady incompressible flow,
   `icoFoam` (laminar) or `pimpleFoam` for transient incompressible flow,
   `interFoam` for a free surface, `rhoCentralFoam` for supersonic flow,
   `buoyantSimpleFoam` or `buoyantPimpleFoam` for buoyant heat transfer,
   `potentialFoam` for a quick initial field.
2. **Inspect.** `sim info`, then `sim get` on `system/blockMeshDict`,
   `system/controlDict`, `system/fvSchemes`, `system/fvSolution`, the fields in
   `0/` and the physical properties in `constant/`.
3. **Edit.** One `sim set` per decision. For changes `foamDictionary` cannot
   express (new patches, new blocks), edit the file under `cases/NAME/`.
4. **Mesh.** `sim run NAME -- blockMesh`, then `sim mesh NAME`. Do not run a
   solver on a mesh with `mesh_ok: false`.
5. **Initialise** a case that has `0.orig`:
   `sim run NAME --sh -- 'rm -rf 0 && cp -r 0.orig 0'`, then `setFields` if there
   is a `setFieldsDict`. If the case has an `Allrun`, read it: it lists the steps
   the tutorial expects.
6. **Run short first.** Set a small `endTime` (or use `--timeout`), run the
   solver and read `sim status`. Then run the full length.
7. **Judge.** `sim status` reports `state`:
   - `converged`: a steady solver met `residualControl`.
   - `finished`: reached `endTime`. For a steady solver this means it did not
     converge: raise `endTime` or fix the numerics.
   - `incomplete`: the log stops before `End` (still running, killed or timed out).
   - `failed`: fatal error or divergence; read `fatal_error`.

   Then run `sim plot` and check that the `fields` min/max are physically sensible.
8. **Iterate.** Change one thing at a time and keep each variant as its own case.

## Decision rules

- **Courant number** (transient): keep `courant.peak_max` below about 1 for
  `icoFoam` and `interFoam`, below about 0.5 for `rhoCentralFoam`. Refining the
  mesh 2x needs `deltaT` halved. Use `adjustTimeStep yes` with `maxCo` where
  the solver supports it.
- **Mesh quality**: non-orthogonality below 70 and skewness below 4 are safe.
  Above about 60 non-orthogonality, add `nNonOrthogonalCorrectors 1-2`.
- **Mesh size**: with 4 CPUs and 4 GB for Docker, stay under about 500k cells. A
  2D case with 10k-50k cells runs in seconds to minutes.
- **Divergence** (residuals rising, `failed`): lower the relaxation factors
  (`relaxationFactors/equations/U`), use a more diffusive scheme
  (`bounded Gauss upwind` for `div(phi,U)`), reduce `deltaT`.
- **Mesh independence**: repeat with about 2x cells per direction and compare a
  quantity of interest, not pictures.
- **2D** in OpenFOAM is one cell thick in one direction, with `empty` patches on
  the two faces normal to it. `sim plot` finds the thin direction, so the flow
  plane can be x-y, x-z or y-z. It does not handle 3D or axisymmetric wedge
  meshes.
- **Turbulence** is set in `constant/turbulenceProperties` (`simulationType`,
  `RAS/RASModel`). Each model needs its own fields in `0/`: `k` and `epsilon`,
  or `k` and `omega`, plus `nut`.

## Worked example

```bash
sim new cavityFine --from incompressible/icoFoam/cavity/cavity
sim set cavityFine system/blockMeshDict blocks "( hex (0 1 2 3 4 5 6 7) (40 40 1) simpleGrading (1 1 1) )"
sim run cavityFine -- blockMesh
sim run cavityFine -- icoFoam
sim status cavityFine          # courant.peak_max 1.85: too high
sim set cavityFine system/controlDict deltaT 0.0025
sim run cavityFine -- icoFoam
sim status cavityFine          # courant.peak_max 0.93: fine
```

## Reference runs

Each built from its tutorial with the tools above, on an Apple M1 with 4 CPUs
and 4 GB for Docker.

| Case | Solver | What it is | Cells | Run time |
|---|---|---|---|---|
| `pitzDaily` | simpleFoam | steady turbulent backward-facing step (k-epsilon) | 12,225 | 7 s, converged in 282 iterations |
| `cavity` | icoFoam | laminar lid-driven cavity | 400 | < 1 s |
| `cavityFine` | icoFoam | same, 40x40 mesh, halved time step | 1,600 | < 1 s |
| `damBreak` | interFoam | two-phase dam break (water/air) | 2,268 | 6 s |
| `forwardStep` | rhoCentralFoam | Mach 3 flow over a forward step | 16,128 | 60 s |
| `cylinder` | pimpleFoam | laminar vortex shedding behind a cylinder, Re = 100 | 34,764 | about 28 min for t = 0 to 1000 s |
| `naca4412` | simpleFoam | NACA 4412 aerofoil, Re = 2e6, 4 deg, Spalart-Allmaras, 2D in the x-z plane | 26,400 | 2 min for 3000 iterations |

- `cylinder`: from the `cylinder2D` tutorial (`blockMesh`, then
  `mirrorMesh -overwrite`; the mesh dictionary uses `#codeStream`). The initial
  field has a small transverse velocity so that shedding starts early. Saturated
  values from t = 600: Strouhal number 0.177, mean Cd 1.49, Cl amplitude 0.36.
  Unbounded-flow references are about 0.164, 1.33 and 0.33; this domain has 10%
  blockage and a short inlet, which raise all three.
- `naca4412`: the NACA0012 tutorial's `blockMeshDict` projected onto a profile
  written by `python -m foamtools.naca 4412 cases/naca4412`, then
  `transformPoints -scale '(1 0 1)'` and `extrudeMesh`. Cl 0.852 and Cm -0.098
  are plausible; Cd 0.0188 is too high because the far field is only 10 chords
  away with `freestream` conditions. The studio's case builder
  (`wingstudio/cfd/case.py`) uses a 30-chord domain with a fixed-velocity inlet
  and gives Cl 0.886, Cd 0.0131, Cm -0.097 for the same condition. XFoil gives
  0.864, 0.0112, -0.090 on the same closed-trailing-edge profile with transition
  forced at 5% chord, and 0.887, 0.0113, -0.098 on its built-in
  blunt-trailing-edge NACA section.

## Practical notes

- Each command starts a fresh container (about 0.3 s). Only files under the
  repository persist between calls.
- The container runs as your host user, so files in `cases/` stay yours and
  tutorials that use `#codeStream` compile.
- Long runs: start `sim run` in the background and poll `sim status`. It reads
  the log as it grows; the state is `incomplete` while the solver runs.
- `sim run` keeps the previous log of the same command as `log.<cmd>.prev`.
  `sim status` reads the latest one; `first_time` shows where it started.
- Parallel: add `system/decomposeParDict`, then `sim run NAME -- decomposePar`,
  `sim run NAME --np 4 -- simpleFoam` and `sim run NAME -- reconstructPar`
  before plotting.
- `sim plot --window AMIN AMAX BMIN BMAX` zooms in the two in-plane coordinates,
  for example `sim plot naca4412 --window -0.5 1.5 -0.5 0.5`; min/max/mean then
  cover only the visible cells. `--time 0.3` plots another time. With `scipy`
  installed the plot has streamlines.
- Some tutorials rely on files in a parent folder: their `Allrun` sits one level
  up, and `sim tutorials` reports it as `allrun_in_parent`. Read that script to
  see what the case needs.
- macOS file systems are case-insensitive: do not create files in a case whose
  names differ only by letter case.
- If Docker is not running, `of` and `sim` start Colima when it is installed
  (4 CPUs, 4 GB). For more: `colima stop && colima start --cpu 6 --memory 6`.
- Another OpenFOAM version: `OF_IMAGE=opencfd/openfoam-default:<tag> sim ...`.
