# OpenFOAM documentation: Wall functions

Source: https://doc.openfoam.com/2606/tools/processing/models/turbulence/ras/wall-functions/
Retrieved: 2026-11-18
Publisher: OpenCFD Ltd (OpenFOAM documentation v2606, doc.openfoam.com)
Licence: "Copyright © OpenCFD Ltd. Documentation licence: CC BY-NC-ND 4.0." (footer of the page)
Scope: excerpts relevant to Wing Studio

## Overview

Wall functions allow Reynolds-averaged simulations at high Reynolds number without resolving the thin viscous sub-layer adjacent to solid boundaries. Instead of placing many mesh layers in the region where molecular viscosity dominates, the first cell centre is located in the logarithmic layer and empirical law-of-the-wall relations supply the missing near-wall physics.

Wall functions are implemented as boundary conditions on turbulence fields (nut, epsilon, omega, etc.) applied to wall patches. They work together with a no-slip velocity condition on U and must be paired with a mesh whose near-wall spacing satisfies the intended $y^+$ range - see Mesh requirements.

## Near-wall flow

[...]

The profile divides naturally into regions:

| Region | Approximate $y^+$ | Character |
| --- | --- | --- |
| Viscous sub-layer | $y^+ \lesssim 5$ | $u^+ \approx y^+$; molecular viscosity dominates |
| Buffer layer | $5 \lesssim y^+ \lesssim 30$ | Transition between viscous and turbulent stress |
| Log (inertial) layer | $y^+ \gtrsim 30$ | $u^+ \approx \frac{1}{\kappa}\ln(y^+) + B$ |

Non-dimensional quantities are defined using the friction velocity $u_\tau$:

$$u^+ = \frac{u}{u_\tau}, \qquad y^+ = \frac{y u_\tau}{\nu}, \qquad u_\tau = \sqrt{\frac{\tau_w}{\rho}}$$

where $\tau_w$ is the wall shear stress, $\rho$ is density, and $\nu$ is kinematic viscosity. In the log layer, turbulent stress dominates and the velocity profile follows the logarithmic law with von Kármán constant $\kappa \approx 0.41$ and roughness parameter $E \approx 9.8$ (giving $B = \ln E / \kappa$).

Resolving this profile directly requires very fine near-wall meshing ($y^+ \sim 1$). Wall functions avoid that cost by assuming the cell centre adjacent to the wall lies in the log layer and that the profile between the wall and that point follows the law of the wall.

## Momentum transfer

[...]

OpenFOAM momentum wall functions (nutUWallFunction, nutkWallFunction, etc.) evaluate $y^+$ from the local flow and set $\nu_t = \mu_t / \rho$ on the nut patch field accordingly. The velocity field retains noSlip on U; the wall function modifies only the turbulent viscosity used in the wall-adjacent stress calculation, not the wall velocity itself.

### Velocity-based and $k$-based formulations

Two common routes to $y^+$ are available:

- Velocity-based (nutUWallFunction, nutUBlendedWallFunction, …): $y^+$ is computed from the magnitude of the tangential velocity at the first cell centre relative to the wall.
- $k$-based (nutkWallFunction, nutkRoughWallFunction): $u_\tau$ is estimated from the turbulent kinetic energy at the cell centre, $u_\tau = C_\mu^{1/4} k^{1/2}$, and hence $y^+ = y u_\tau / \nu$.

[...]

### Blending treatments

When the first cell centre falls in the buffer layer, neither the pure viscous nor the pure log-layer estimate is accurate. OpenFOAM wall functions support blending between the two predictions via the blending entry:

| blending | Behaviour |
| --- | --- |
| stepwise | Viscous below $y^+_\mathrm{lam}$, log-layer above (high-Re default) |
| max | $\nu_t = \max(\nu_{t,\mathrm{vis}}, \nu_{t,\mathrm{log}})$ |
| binomial | Smooth blend; used by nutUBlendedWallFunction and recommended with omegaWallFunction for automatic wall treatment |

[...]

## Scalar wall functions

Turbulence model scalars require consistent wall treatment alongside nut:

| Field | Typical wall BC | Role |
| --- | --- | --- |
| k | kqRWallFunction | Zero-gradient; not a wall function - assumes $k$ at the cell centre is representative in the log layer |
| epsilon | epsilonWallFunction | Sets $\epsilon$ and turbulent production $G$ from $y^+$ |
| omega | omegaWallFunction | Sets $\omega$ and $G$; use blending binomial with nutUBlendedWallFunction for full automatic wall treatment |
| k (low-Re) | kLowReWallFunction | Analytic sub-layer profile when resolving $y^+ \sim 1$ |

kqRWallFunction is a wrapper around zero-gradient and does not impose a value at the wall. In high-Re wall-function mode, turbulent kinetic energy is not forced to zero on the patch; near-wall damping is handled through $\nu_t$.

## High and low Reynolds number operation

High Reynolds number (wall-function mode) is intended for $30 \lesssim y^+ \lesssim 300$ on the first cell centre. The mesh skips the viscous sub-layer; blending stepwise or lowReCorrection false on epsilonWallFunction uses only the log-layer limit.

Low Reynolds number operation resolves the sub-layer ($y^+ \lesssim 1$, typically 10–20 wall-normal layers) and uses laminar viscosity near the wall. Enable low-Re corrections (blending binomial, lowReCorrection true, or dedicated conditions such as nutLowReWallFunction and kLowReWallFunction) when the first cell centre lies below the log layer.

Mismatch between the chosen wall treatment and the actual $y^+$ distribution is a common source of inaccurate drag or heat-transfer predictions and should be checked with a post-processing function object such as yPlus.
