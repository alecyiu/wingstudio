# 2D NACA 0012 Airfoil Validation Case (Turbulence Modeling Resource)

Source: https://tmbwg.github.io/turbmodels/naca0012_val.html
Retrieved: 2026-11-18
Publisher: NASA Langley Research Center, Turbulence Modeling Resource (pages served at tmbwg.github.io)
Licence: not stated on the page
Scope: excerpts relevant to Wing Studio

## 2DN00: 2D NACA 0012 Airfoil Validation Case

The purpose here is to provide a validation case for turbulence models. Unlike verification, which seeks to establish that a model has been implemented correctly, validation compares CFD results against data in an effort to establish a model's ability to reproduce physics. A large sequence of nested grids of the same family are provided here if desired. Data are also provided for comparison. For this particular "essentially incompressible" NACA 0012 airfoil case, the data are from experiments.

[...]

The turbulent NACA 0012 airfoil case should be run at essentially incompressible conditions (the recommendation here is to run M = 0.15 in compressible CFD codes). The Reynolds number per chord is Re = 6 million. Boundary layers should be fully turbulent over most of the airfoil. Inflow conditions for the turbulence variables should be reported. To minimize issues associated with effect of the farfield boundary (which can particularly influence drag and lift levels at high lift conditions), the farfield boundary in the grids provided have been located almost 500 chords away from the airfoil. Otherwise, a "farfield point vortex" boundary condition correction should be employed (see Thomas and Salas, AIAA Journal 24(7):1074-1080, 1986, https://doi.org/10.2514/3.9394). The following plot shows the layout of the provided NACA 0012 grids, along with typical boundary conditions. (Note that particular variations of the BCs at the farfield boundaries may also work and yield similar results for this problem.)

[...]

The quantities of interest for comparison are as follows:

- Lift coefficient (C_L) vs. angle of attack (alpha) (for 0 < alpha < a_max)
- Drag coefficient (C_D) vs. C_L for 0 < alpha < a_max)
- Surface pressure coefficient (C_p) vs. x/c (for alpha = 0, 10, 15)
- Surface skin friction coefficient (C_f) vs. x/c (for alpha = 0, 10, 15)

where a_max is the maximum angle of attack at which the CFD yields steady-state results with no force oscillations.

[...]

## Effect of Farfield Boundary

The plots show examples of the effect of farfield grid extent on lift and drag coefficients (using the 449 x 129 base grid) for high-lift conditions on the NACA 0012 airfoil at alpha=15 deg. A farfield extent of 500 c is far enough for the finite boundary to have only a minimal influence on the solution. With smaller farfield boundary extents such as 30 c, the boundary influence on the forces is greater (especially for drag) unless one employs a "farfield point vortex" (PV) boundary condition correction. See Thomas and Salas, AIAA Journal 24(7):1074-1080, 1986.

[...]

## Grids - NACA 0012 Airfoil Case

A series of 5 nested 2-D grids are provided. Each coarser grid is exactly every-other-point of the next finer grid, ranging from the finest 1793 x 513 to the coarsest 113 x 33 grid. The finest grid has minimum spacing at the wall of y=4 x 10^-7, giving an approximate average y+ between 0.1 and 0.2 over the airfoil at the Reynolds number run. The grid is stretched in the wall-normal direction, and the clustering is maintained in the wake region. The topology is a so-called "C-grid," with the grid wrapping around the airfoil from downstream farfield, around the lower surface to the upper, then back to the downstream farfield again; the grid connects to itself in a 1-to-1 fashion in the wake. There are 1025 points on the airfoil surface on the finest grid (65 points on the coarsest grid). There are 385 points along the wake from the airfoil trailing edge to the outflow boundary on the finest grid (25 points on the coarsest grid). The figures below show two views of the 449 x 129 grid.

[...]

## SA Model Results

Above SA results are from seven independent CFD codes: CFL3D (NASA LaRC, USA), FUN3D (NASA LaRC, USA), NTS (NTS, Russia), JOE (Stanford, USA), SUMB (Stanford, USA), TURNS (Stanford/Maryland, USA), and GGNS (Boeing, USA). Both CFL3D and FUN3D used freestream value of the SA turbulence field variable (relative to laminar)=3. Please read note 5 on Notes on running CFD page. The first six codes all used the same 897x257 grid, whereas GGNS is a grid-adaptive code. All seven codes gave very close results (max 1% difference in lift, 4% difference in drag). These small differences are due to discretization errors, iterative convergence differences, boundary condition differences, and/or possible code-to-code implementation-detail differences. (Limited tests with CFL3D indicate that the effects of discretization errors on lift and drag for the 897x257 grid are smaller than the max differences listed above. However, complete grid studies involving all codes would be required to learn the effects for each code.) See NACA 0012 Numerical Analysis Page for more discussion about numerical issues associated with the NACA 0012 airfoil case. Note that these are compressible code results at "essentially incompressible" conditions of M=0.15. There may be a very small influence of compressibility.

[...]

## SSTm Model Results

Above SSTm results are from three independent CFD codes: CFL3D (NASA LaRC, USA), FUN3D (NASA LaRC, USA), and NTS (NTS, Russia). Both CFL3D and FUN3D used freestream turbulence intensity=0.052% and freestream turbulent viscosity (relative to laminar)=0.009 (additional details can be found in the CFL3D User's Manual, Appendix H). Please read note 5 on Notes on running CFD page. The codes all used the same 897x257 grid, and gave very close results (max less than 1% difference in lift, 5% difference in drag). These small differences are due to discretization errors, iterative convergence differences, boundary condition differences, and/or possible code-to-code implementation-detail differences. (Limited tests with CFL3D indicate that the effects of discretization errors on lift and drag for the 897x257 grid are smaller than the max differences listed above. However, complete grid studies involving all codes would be required to learn the effects for each code.) See NACA 0012 Numerical Analysis Page for more discussion about numerical issues associated with the NACA 0012 airfoil case. Note that these are compressible code results at "essentially incompressible" conditions of M=0.15. There may be a very small influence of compressibility. Note that use of SST, SSTm, SST-V, or SST-Vm yield very close results for this case.
