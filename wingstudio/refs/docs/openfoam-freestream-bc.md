# OpenFOAM documentation: freestream boundary condition

Source: https://doc.openfoam.com/2606/tools/processing/boundary-conditions/rtm/derived/inletOutlet/freestream/
Retrieved: 2026-11-18
Publisher: OpenCFD Ltd (OpenFOAM documentation v2606, doc.openfoam.com)
Licence: "Copyright © OpenCFD Ltd. Documentation licence: CC BY-NC-ND 4.0." (footer of the page)
Scope: excerpts relevant to Wing Studio

## Description

The freestream is a generic boundary condition that provides a free-stream condition. It is a mixed condition derived from the inletOutlet condition, whereby the mode of operation switches between fixed (free stream) value and zero gradient based on the sign of the flux.

## Usage

The condition requires entries in both the boundary and field files.

### Boundary file

```
<patchName>
{
    type            patch;
    ...
}
```

### Field file

```
<patchName>
{
    // Mandatory entries
    type            freestream;

    // Conditional entries

        // Option-1
        freestreamValue        <Field<Type>>;

        // Option-2
        freestreamBC           <fvPatchField<Type>>;

    // Optional entries
    phi             <word>;

    // Inherited entries;
    ...
}
```

where:

| Property | Description | Type | Required | Default |
| --- | --- | --- | --- | --- |
| type | Type name: freestream | word | yes | - |
| freestreamValue | Patch value | `Field<Type>` | choice | - |
| freestreamBC | Separate boundary condition | `fvPatchField<Type>` | choice | - |
| phi | Name of flux field | word | no | phi |

The inherited entries are elaborated in:

- Foam::inletOutletFvPatchField
