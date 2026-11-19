# OpenFOAM documentation: Under relaxation

Source: https://doc.openfoam.com/2606/tools/processing/numerics/solvers/under-relaxation/
Retrieved: 2026-11-18
Publisher: OpenCFD Ltd (OpenFOAM documentation v2606, doc.openfoam.com)
Licence: "Copyright © OpenCFD Ltd. Documentation licence: CC BY-NC-ND 4.0." (footer of the page)
Scope: excerpts relevant to Wing Studio

## Under relaxation

Under relaxation factors are applied to stabilise calculations by limiting the rate of change of both fields and equations.

## Field under-relaxation

$$Q^n = Q^o + \alpha \left( Q - Q^o \right)$$

## Equation under-relaxation

The linear solvers require that the coefficient matrix is at least diagonally equal, and preferably diagonally dominant, i.e. per row, the magnitude of the diagonal must be greater than or equal to the sum of the off-diagonal components.

## Usage

Specified in the fvSolution file under the relaxationFactors sub-dictionary

```
relaxationFactors
{
    fields
    {
    }
    equations
    {
    }
}
```

Values relate to

- 0: no under-relaxation (equivalent to not specified}
- 1: matrix forced to become diagonally equal
