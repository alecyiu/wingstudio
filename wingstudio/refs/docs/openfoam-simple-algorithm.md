# OpenFOAM documentation: SIMPLE algorithm

Source: https://doc.openfoam.com/2606/tools/processing/solvers/pressure-velocity/simple/
Retrieved: 2026-11-18
Publisher: OpenCFD Ltd (OpenFOAM documentation v2606, doc.openfoam.com)
Licence: "Copyright © OpenCFD Ltd. Documentation licence: CC BY-NC-ND 4.0." (footer of the page)
Scope: excerpts relevant to Wing Studio

## SIMPLE algorithm

> note
>
> This page is under construction.

## SIMPLE

- <B>S</B>emi-<B>I</B>mplicit <B>M</B>ethod for <B>P</B>ressure <B>L</B>inked <B>E</B>quations
- By Caretto et al. 9

The sequence for each iteration follows:

1. Advance to the next iteration $t = t^{n + 1}$
2. Initialise $\u^{n+1}$ and $p^{n+1}$ using latest available values of $\u$ and $p$
3. Construct the momentum equations
4. Under-relax the momentum matrix
5. Solve the momentum equations to obtain a prediction for $\u^{n+1}$
6. Construct the pressure equation
7. Solve the pressure equation for $p^{n+1}$
8. Correct the flux for $\phi^{n+1}$
9. Under-relax $p^{n+1}$
10. Correct the velocity

```
for $$ \u^{n+1} $$
```

11. If not converged, go back to step 2

## SIMPLEC

- By Van Doormaal and Raithby 82
