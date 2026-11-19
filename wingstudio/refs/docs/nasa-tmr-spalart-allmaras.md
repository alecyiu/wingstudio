# The Spalart-Allmaras Turbulence Model (Turbulence Modeling Resource)

Source: https://tmbwg.github.io/turbmodels/spalart.html
Retrieved: 2026-11-18
Publisher: NASA Langley Research Center, Turbulence Modeling Resource (pages served at tmbwg.github.io)
Licence: not stated on the page
Scope: excerpts relevant to Wing Studio

## "Standard" Spalart-Allmaras One-Equation Model (SA)

The following equations represents the most commonly-used implementation of the Spalart-Allmaras model (written in non-conservation form). The primary reference is:

- Spalart, P. R. and Allmaras, S. R., "A One-Equation Turbulence Model for Aerodynamic Flows," **Recherche Aerospatiale**, No. 1, 1994, pp. 5-21. (paper provided with permission of authors)

This journal reference is the official publication that arose from the AIAA Conference Paper AIAA-92-0439 (Reno, NV, January 1992). However, there are some differences, and the journal reference takes precedence. For example, AIAA-92-0439 used c_t3=1.1 and c_t4=2.0; these constants are now different (although for fully turbulent solutions, these sets of constants should make negligible difference). Note that this journal reference had a small typo (appendix only) in its definition of the constant *c_w1* (missing square on the kappa term in the denominator). The typo, which sometimes gets propagated to other reports/papers, is corrected below. The original reference made use of a trip term that most people do not include, because the model is most often employed for fully turbulent applications. Therefore, in this "standard" representation the trip term is being left out (see version (SA-Ia) below for the version including the trip term). As a consequence, the farfield boundary condition must be changed from that given in the above reference. The new farfield boundary condition is taken from the following references:

- Spalart, P. R., "Trends in Turbulence Treatments," **AIAA 2000-2306**, June 2000, https://doi.org/10.2514/6.2000-2306.
- Spalart, P. R. and Rumsey, C. L., "Effective Inflow Conditions for Turbulence Models in Aerodynamic Calculations," **AIAA Journal**, Vol. 45, No. 10, 2007, pp. 2544-2553, https://doi.org/10.2514/1.29373.

In all of the following, a "hat" is used over the turbulence field variable, rather than a "tilde" as given in the references, for the sole practical reason that the "tilde" showed up very poorly on the screen.

The one-equation model is given by the following equation:

$$\frac{\partial \tilde \nu}{\partial t} + u_j \frac{\partial \tilde \nu}{\partial x_j} = c_{b1}(1-f_{t2})\tilde S \tilde \nu - \left[c_{w1}f_w - \frac{c_{b1}}{\kappa^2}f_{t2}\right] \left(\frac{\tilde \nu}{d} \right)^2 + \frac{1}{\sigma} \left[ \frac{\partial}{\partial x_j} \left( \left( \nu + \tilde \nu \right) \frac{\partial \tilde \nu}{\partial x_j} \right) + c_{b2}\frac{\partial \tilde \nu}{\partial x_i} \frac{\partial \tilde \nu}{\partial x_i} \right]$$

and the turbulent eddy viscosity is computed from:

$$\mu_t = \rho \tilde \nu f_{v1}$$

where

$$f_{v1} = \frac{\chi^3}{\chi^3+c_{v1}^3}$$

$$\chi = \frac{\tilde \nu}{\nu}$$

and $\rho$ is the density, $\nu = \mu/\rho$ is the molecular kinematic viscosity, and $\mu$ is the molecular dynamic viscosity. Additional definitions are given by the following equations:

$$\tilde S = \Omega + \frac{\tilde \nu}{\kappa^2 d^2} f_{v2}$$

where $\Omega = \sqrt{2 W_{ij} W_{ij} }$ is the magnitude of the vorticity, *d* is the distance from the field point to the nearest wall, and

$$f_{v2} = 1 - \frac{\chi}{1+\chi f_{v1}}$$
$$f_w = g \left[ \frac{1+c_{w3}^6}{g^6 + c_{w3}^6} \right]^{1/6}$$

$$g = r + c_{w2}(r^6 - r)$$

$$r = {\rm min} \left[ \frac{\tilde \nu}{\tilde S \kappa^2 d^2}, 10 \right]$$

$$f_{t2} = c_{t3} {\rm exp} \left( -c_{t4} \chi^2 \right)$$

$$W_{ij} = \frac{1}{2} \left(\frac{\partial u_i}{\partial x_j} - \frac{\partial u_j}{\partial x_i} \right)$$

The boundary conditions are:

$$\tilde \nu_{wall} = 0$$
$$\tilde \nu_{farfield} = 3 \nu_{\infty} : to : 5 \nu_{\infty}$$

Note that these boundary conditions on the SA turbulence field variable correspond to turbulent kinematic viscosity values of:

$$\nu_{t,wall} = 0$$
$$\nu_{t,farfield} = 0.210438 \nu_{\infty} : to : 1.294234 \nu_{\infty}$$

The constants are:

$$c_{b1} = 0.1355$$
$$\sigma = 2/3$$
$$c_{b2} = 0.622$$
$$\kappa = 0.41$$

$$c_{w2} = 0.3$$
$$c_{w3} = 2$$
$$c_{v1} = 7.1$$
$$c_{t3} = 1.2$$
$$c_{t4} = 0.5$$

$$c_{w1} = \frac{c_{b1}}{\kappa^2} + \frac{1+c_{b2}}{\sigma}$$

Note that this model has source terms (production and destruction) that are non-zero in the freestream, even when vorticity is zero. The source terms are, however, very small: proportional to 1/d^2.
