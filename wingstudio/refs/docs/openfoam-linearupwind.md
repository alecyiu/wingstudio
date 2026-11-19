# OpenFOAM documentation: Linear-upwind divergence scheme

Source: https://doc.openfoam.com/2606/tools/processing/numerics/schemes/divergence/rtm/linearUpwind/
Retrieved: 2026-11-18
Publisher: OpenCFD Ltd (OpenFOAM documentation v2606, doc.openfoam.com)
Licence: "Copyright © OpenCFD Ltd. Documentation licence: CC BY-NC-ND 4.0." (footer of the page)
Scope: excerpts relevant to Wing Studio

## Properties

- Employs upwind interpolation weights, with an explicit correction based on the local cell gradient
- Second order
- Unbounded
- As shown by Warming and beam 86

[...]

## Usage

The scheme is specified using:

```
divSchemes
{
    default         none;
    div(phi,U)      Gauss linearUpwind grad(U);
}
```
