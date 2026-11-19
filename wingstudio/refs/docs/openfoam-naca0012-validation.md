# OpenFOAM documentation: Turbulent flow over NACA0012 airfoil (2D)

Source: https://doc.openfoam.com/2606/examples/verification-validation/turbulent/naca0012/
Retrieved: 2026-11-18
Publisher: OpenCFD Ltd (OpenFOAM documentation v2606, doc.openfoam.com)
Licence: "Copyright © OpenCFD Ltd. Documentation licence: CC BY-NC-ND 4.0." (footer of the page)
Scope: excerpts relevant to Wing Studio

## Overview

- Based on https://turbmodels.larc.nasa.gov/naca0012_val.html 53
- References: Gregory-O'Reilly (1970) 21, McCroskey (1987) 47, Ladson (1988) 32, Spalart-Allmaras (1994) 69, and Krist et al. (1997) 29
- See the resources section for additional data files

Flow physics:

- External flow
- Steady
- High Reynolds number
- Low Mach number, subsonic
- Newtonian, single-phase, incompressible, non-reacting

Solver:

- simpleFoam

Tutorial case:

- airFoil2D

## Physics and Numerics

Physical domain:

- The case is a two-dimensional airfoil located around the centre of a computational domain whose dimensions are considerably larger than the chord-length of the airfoil.
  - $x$: Longitudinal direction (mean flow direction)
  - $y$: Spanwise direction (statistically homogeneous direction)
  - $z$: Vertical direction (wall-normal direction)
  - $O$: Origin at the leading edge of the airfoil

Physical modelling:

- Reynolds number based on local chord length: $\text{Re}_c = U_x \, c \, \nu^{-1} \approx 6\times10^6$
  - Streamwise far-field flow speed: $U_x = 51.4815$ [m⋅s<sup>-1</sup>]
  - Characteristic length (Local chord length of the airfoil): $c = 1.0$ [m]
  - Kinematic viscosity of fluid: $\nu_\text{fluid} = 8.58 \times 10^{-6}$

```
\[m<sup>2</sup>⋅s<sup>-1</sup>\]
```

- Mach number: $\text{Ma} = U_x / U_s \approx 0.15$
  - Speed of sound: $U_s = 343.21$ [m⋅s<sup>-1</sup>]
- Turbulence model: Spalart Allmaras

Numerical domain modelling:

- Shape: extruded C-grid
- Dimensions: $(x, y, z) \approx (985.5, 1.0, 1015.6)$ [m]
- Sketch (View direction to $y$-positive):

[...]

Spatial domain discretisation:

- Mesh type: hexahedral cells in plot3d format
- Mesh converter: plot3dToFoam
- Number of cells, $N$ : $(N_x, N_y, N_z) = (257, 1, 897)$
- First wall-normal cell centre height: $\Delta_y^+ < 1$
- Mesh detail (View direction to $y$-positive):

[...]

Equation discretisation:

Spatial derivatives and variables:

- Gradient: Gauss linear
- Divergence:
  - default: Gauss linear
  - div(phi,U): bounded Gauss

```
[linearUpwind](https://doc.openfoam.com/2606/tools/processing/numerics/schemes/divergence/rtm/linearUpwind/) `grad(U)`
```

- div(phi,nuTilda): bounded Gauss linearUpwind grad(nuTilda)
- Laplacian: Gaussian linear corrected
- Surface-normal gradient: corrected

Temporal derivatives and variables:

- ddtSchemes: steadyState

Numerical boundary conditions:

- Velocity, $\mathbf{U}$

| Patch | Condition | Value [m⋅s<sup>-1</sup>] |
| --- | --- | --- |
| Inlet | freestreamVelocity | $\mathbf{U}_\alpha$ |
| Outlet | freestreamVelocity | $\mathbf{U}_\alpha$ |
| Sides $\text{(}y$-dir) | empty | - |
| Aerofoil | fixedValue | (0.0, 0.0, 0.0) |

| <b> α </b> | <b> U </b><sub>α</sub> |
| --- | --- |
| $\alpha = 0^o$ | (51.4815, 0.00, 0.0000) |
| $\alpha = 10^o$ | (50.6994, 0.00, 8.9397) |
| $\alpha = 15^o$ | (49.7273, 0.00, 13.3244) |

- Kinematic pressure, p

| Patch | Condition | Value [m<sup>2</sup>⋅s<sup>-2</sup>] |
| --- | --- | --- |
| Inlet | freestreamPressure | 0.0 |
| Outlet | freestreamPressure | 0.0 |
| Sides $\text{(}y$-dir) | empty | - |
| Aerofoil | zeroGradient | - |

- Turbulent kinematic viscosity, nut (i.e. $\nu_t$)

| Patch | Condition | Value [m<sup>2</sup>⋅s<sup>-1</sup>] |
| --- | --- | --- |
| Inlet | freestream | $8.58e^{-6} \approx \nu_\text{fluid}$ 54 |
| Outlet | freestream | $8.58e^{-6}\approx \nu_\text{fluid}$ 54 |
| Sides $\text{(}y$-dir) | empty | - |
| Aerofoil | fixedValue | 0.0 54 |

- Spalart-Allmaras model modified viscosity, nuTilda (i.e. $\tilde{\nu}$)

| Patch | Condition | Value [m<sup>2</sup>⋅s<sup>-1</sup>] |
| --- | --- | --- |
| Inlet | freestream | $3.432e^{-5} \approx 4 \nu_\text{fluid}$ 54 |
| Outlet | freestream | $3.432e^{-5}\approx 4 \nu_\text{fluid}$ 54 |
| Sides $\text{(}y$-dir) | empty | - |
| Aerofoil | fixedValue | 0.0 54 |

Solution algorithms and solvers:

- Pressure-velocity: SIMPLE
- Parallel decomposition of spatial domain and fields: Not applicable

- Linear solvers:

| Field | Linear Solver | Smoother | Tolerance (rel) |
| --- | --- | --- | --- |
| U | smooth | GaussSeidel | 0.01 |
| p | GAMG | GaussSeidel | 0.01 |
| nuTilda | smooth | GaussSeidel | 0.01 |
