# The Menter Shear Stress Transport Turbulence Model (Turbulence Modeling Resource)

Source: https://tmbwg.github.io/turbmodels/sst.html
Retrieved: 2026-11-18
Publisher: NASA Langley Research Center, Turbulence Modeling Resource (pages served at tmbwg.github.io)
Licence: not stated on the page
Scope: excerpts relevant to Wing Studio

## "Standard" Menter SST Two-Equation Model (SST) and (SSTm)

The reference for the standard implementation of the Menter SST model is:

- Menter, F. R., "Two-Equation Eddy-Viscosity Turbulence Models for Engineering Applications," **AIAA Journal**, Vol. 32, No. 8, August 1994, pp. 1598-1605, https://doi.org/10.2514/3.12149.

This model is almost identical to the Menter baseline model. Only one constant ($\sigma_{k 1}$) and the expression for turbulent eddy viscosity are different. The two-equation model (written in conservation form) is given by the following:

$$\frac{\partial (\rho k)}{\partial t} + \frac{\partial (\rho u_j k)}{\partial x_j} = \cal P - \beta^* \rho \omega k + \frac{\partial}{\partial x_j} \left[\left(\mu + \sigma_k \mu_t \right)\frac{\partial k}{\partial x_j}\right]$$

$$\frac{\partial (\rho \omega)}{\partial t} + \frac{\partial (\rho u_j \omega)}{\partial x_j} = \frac{\gamma}{\nu_t} \cal P - \beta \rho \omega^2 + \frac{\partial}{\partial x_j} \left[ \left( \mu + \sigma_{\omega} \mu_t \right) \frac{\partial \omega}{\partial x_j} \right] + 2(1-F_1) \frac{\rho \sigma_{\omega 2}}{\omega} \frac{\partial k}{\partial x_j} \frac{\partial \omega}{\partial x_j}$$

Note that in the reference, the Lagrangian derivative was used, which is not identical with the proper form of these equations as written by the author and others elsewhere. The equations have been written above to be in proper conservation form, consistent with, e.g., Wilcox (in Turbulence Modeling for CFD, DCW Industries, Inc., La Canada, CA, 2006), Menter et al (in Turbulence, Heat and Mass Transfer 4, 2003, pp. 625-632), and Menter (in NASA TM 103975, 1992, https://ntrs.nasa.gov/citations/19930013620).

$$P = \tau_{ij} \frac{\partial u_i}{\partial x_j}$$

$$\tau_{ij} = \mu_t \left(2S_{ij} - \frac{2}{3} \frac{\partial u_k}{\partial x_k} \delta_{ij} \right) - \frac{2}{3} \rho k \delta_{ij}$$

$$S_{ij} = \frac{1}{2} \left( \frac{\partial u_i}{\partial x_j} + \frac{\partial u_j}{\partial x_i} \right)$$

and the turbulent eddy viscosity is computed from:

$$\mu_t = \frac{\rho a_1 k}{{\rm max} (a_1 \omega, \Omega F_2)}$$

Each of the constants is a blend of an inner (1) and outer (2) constant, blended via:

$$\phi = F_1 \phi_1 + (1-F_1) \phi_2$$

where $\phi_1$ represents constant 1 and $\phi_2$ represents constant 2. Additional functions are given by:

$$F_1 = {\rm tanh} \left({\rm arg}_1^4 \right)$$

$${\rm arg}_1 = {\rm min} \left[ {\rm max} \left( \frac{\sqrt{k}}{\beta^*\omega d}, \frac{500 \nu}{d^2 \omega} \right) , \frac{4 \rho \sigma_{\omega 2} k}{{\rm CD}_{k \omega} d^2} \right]$$

$${\rm CD}_{k \omega} = {\rm max} \left(2 \rho \sigma_{\omega 2} \frac{1}{\omega} \frac{\partial k}{\partial x_j} \frac{\partial \omega}{\partial x_j}, 10^{-20} \right)$$

$$F_2 = {\rm tanh} \left({\rm arg}_2^2 \right)$$

$${\rm arg}_2 = {\rm max} \left( 2 \frac{\sqrt{k}}{\beta^* \omega d}, \frac{500 \nu}{d^2 \omega} \right)$$

and $\rho$ is the density, $\nu_t = \mu_t/\rho$ is the turbulent kinematic viscosity, $\mu$ is the molecular dynamic viscosity, *d* is the distance from the field point to the nearest wall, and $\Omega = \sqrt{2 W_{ij} W_{ij} }$ is the vorticity magnitude, with

$$W_{ij} = \frac{1}{2} \left(\frac{\partial u_i}{\partial x_j} - \frac{\partial u_j}{\partial x_i} \right)$$

Note that it is generally recommended to use a production limiter (see Menter, F. R., "Zonal Two Equation k-omega Turbulence Models for Aerodynamic Flows," AIAA Paper 93-2906, July 1993, https://doi.org/10.2514/6.1993-2906). In this reference, the term *P* in the *k*-equation is replaced by:

$${\rm min}(\cal P, 20 \beta^* \rho \omega k)$$

The boundary conditions recommended in the original reference are:

$$\frac{U_{\infty}{L} < \omega_{\rm farfield} < 10 \frac{U_{\infty}{L}$$

$$\frac{10^{-5}U_{\infty}^2}{Re_L} < k_{\rm farfield} < \frac{0.1 U_{\infty}^2}{Re_L}$$

$$\omega_{wall} = 10 \frac{6 \nu}{\beta_1 (\Delta d_1)^2}$$

$$k_{wall} = 0$$

where "*L* is the approximate length of the computational domain," and the combination of the two farfield values should yield a freestream turbulent viscosity between 10^-5 and 10^-2 times freestream laminar viscosity. Thus, the farfield turbulence boundary conditions are somewhat open to interpretation. Note that the turbulence variables decay (sometimes dramatically) from their set values in the farfield for external aerodynamic problems. See the version (SST_sust) below for an alternative formulation that eliminates this decay, and provides more precise definitions for the boundary conditions.

The constants are:

$$\gamma_1 = \frac{\beta_1}{\beta^*} - \frac{\sigma_{\omega 1} \kappa^2}{\sqrt{\beta^*}}$$
$$\gamma_2 = \frac{\beta_2}{\beta^*} - \frac{\sigma_{\omega 2} \kappa^2}{\sqrt{\beta^*}}$$

$$\sigma_{k 1} = 0.85$$
$$\sigma_{\omega 1} = 0.5$$
$$\beta_1 = 0.075$$

$$\sigma_{k 2} = 1.0$$
$$\sigma_{\omega 2} = 0.856$$
$$\beta_2 = 0.0828$$

$$\beta^*=0.09$$
$$\kappa=0.41$$
$$a_1 = 0.31$$

[...]

## Menter SST Two-Equation Model with Controlled Decay (SST-sust) and (SST-sust-m)

This form of the SST model eliminates the non-physical decay of turbulence variables in the freestream for external aerodynamic problems, through the addition of sustaining terms to the equations. The reference is:

- Spalart, P. R. and Rumsey, C. L., "Effective Inflow Conditions for Turbulence Models in Aerodynamic Calculations," **AIAA Journal**, Vol. 45, No. 10, 2007, pp. 2544 - 2553, https://doi.org/10.2514/1.29373.

The equations are:

$$\frac{\partial (\rho k)}{\partial t} + \frac{\partial (\rho u_j k)}{\partial x_j} = \cal P - \beta^* \rho \omega k + \frac{\partial}{\partial x_j} \left[\left(\mu + \sigma_k \mu_t \right)\frac{\partial k}{\partial x_j}\right] +\beta^* \rho \omega_{\rm amb} k_{\rm amb}$$

$$\frac{\partial (\rho \omega)}{\partial t} + \frac{\partial (\rho u_j \omega)}{\partial x_j} = \frac{\gamma}{\nu_t} \cal P - \beta \rho \omega^2 + \frac{\partial}{\partial x_j} \left[ \left( \mu + \sigma_{\omega} \mu_t \right) \frac{\partial \omega}{\partial x_j} \right] + 2(1-F_1) \frac{\rho \sigma_{\omega 2}}{\omega} \frac{\partial k}{\partial x_j} \frac{\partial \omega}{\partial x_j} + \beta \rho \omega_{\rm amb}^2$$

where everything except the last term in each equation is identical to the standard model (SST). The recommended farfield boundary conditions are somewhat different:

$$\omega_{\rm farfield} = \omega_{\rm amb} = \frac{5 U_{\infty}}{L}$$

$$k_{\rm farfield} = k_{\rm amb} = 10^{-6} U_{\infty}^2$$

Here, *L* is no longer the "approximate length of the computational domain," like it was for (SST), but rather the defining length scale for the particular problem (usually associated with some feature or scale of the aerodynamic body of interest). In the equations, $\omega_{\rm amb}$ and $k_{\rm amb}$ are taken to be these farfield boundary values. The extra terms have the effect of exactly cancelling the destruction terms in the freestream when the turbulence levels are equal to the set ambient levels. Inside the boundary layer, they are generally orders of magnitude smaller than the destruction terms for reasonable freestream turbulence levels (say, Tu = 1% or less), and therefore have little effect. The farfield boundary condition $k_{\rm farfield} = 10^{-6} U_{\infty}^2$ corresponds to a freestream Tu level of 0.08165%.

Note that applying the sustaining terms with the inflow values of k and omega that have been typical in external aerodynamics (e.g., as used for the standard model (SST)) will not be effective. The reason is that these values have been adjusted to allow a drastic decay before the fluid approaches the body.
