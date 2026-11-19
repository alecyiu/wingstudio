# OpenFOAM v2512 source: primitiveMeshCheck.C (default mesh-check thresholds)

Source: https://api.openfoam.com/2512/primitiveMeshCheck_8C_source.html
Retrieved: 2026-11-18
Publisher: OpenCFD Ltd (OpenFOAM API guide v2512, api.openfoam.com, generated with Doxygen)
Licence: "Copyright (C) 2011-2016 OpenFOAM Foundation", "Copyright (C) 2019-2022 OpenCFD Ltd." and "OpenFOAM is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version." (header of the source file, lines 8 to 17; the API page states no licence of its own)
Scope: excerpts relevant to Wing Studio

## primitiveMeshCheck.C

```
    1 /*---------------------------------------------------------------------------*\
    2   =========                 |
    3   \\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox
    4    \\    /   O peration     |
    5     \\  /    A nd           | www.openfoam.com
    6      \\/     M anipulation  |
    7 -------------------------------------------------------------------------------
    8     Copyright (C) 2011-2016 OpenFOAM Foundation
    9     Copyright (C) 2019-2022 OpenCFD Ltd.
   10 -------------------------------------------------------------------------------
   11 License
   12     This file is part of OpenFOAM.
   13
   14     OpenFOAM is free software: you can redistribute it and/or modify it
   15     under the terms of the GNU General Public License as published by
   16     the Free Software Foundation, either version 3 of the License, or
   17     (at your option) any later version.
   18
   19     OpenFOAM is distributed in the hope that it will be useful, but WITHOUT
   20     ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
   21     FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
   22     for more details.
   23
   24     You should have received a copy of the GNU General Public License
   25     along with OpenFOAM.  If not, see <http://www.gnu.org/licenses/>.
   26
   27 \*---------------------------------------------------------------------------*/
```

[...]

## Static Data Members

```
   37 // * * * * * * * * * * * * * * Static Data Members * * * * * * * * * * * * * //
   38
   39 Foam::scalar Foam::primitiveMesh::closedThreshold_ = 1.0e-6;
   40 Foam::scalar Foam::primitiveMesh::aspectThreshold_ = 1000;
   41 Foam::scalar Foam::primitiveMesh::nonOrthThreshold_ = 70; // deg
   42 Foam::scalar Foam::primitiveMesh::skewThreshold_ = 4;
   43 Foam::scalar Foam::primitiveMesh::planarCosAngle_ = 1.0e-6;
```

[...]

## Private Member Functions

[...]

```
  173         if (aspectRatio[celli] > aspectThreshold_)
  174         {
  175             if (aspectSetPtr)
  176             {
  177                 aspectSetPtr->insert(celli);
  178             }
  179
  180             nAspect++;
  181         }
```

[...]

```
  365 bool Foam::primitiveMesh::checkFaceOrthogonality
  366 (
  367     const vectorField& fAreas,
  368     const vectorField& cellCtrs,
  369     const bool report,
  370     labelHashSet* setPtr
  371 ) const
  372 {
  373     DebugInFunction << "Checking mesh non-orthogonality" << endl;
  374
  375     tmp<scalarField> tortho = primitiveMeshTools::faceOrthogonality
  376     (
  377         *this,
  378         fAreas,
  379         cellCtrs
  380     );
  381     const scalarField& ortho = tortho();
  382
  383     // Severe nonorthogonality threshold
  384     const scalar severeNonorthogonalityThreshold =
  385         ::cos(degToRad(nonOrthThreshold_));
```

[...]

```
  396     forAll(ortho, facei)
  397     {
  398         if (ortho[facei] < severeNonorthogonalityThreshold)
  399         {
  400             if (ortho[facei] > SMALL)
  401             {
  402                 if (setPtr)
  403                 {
  404                     setPtr->insert(facei);
  405                 }
  406
  407                 severeNonOrth++;
  408             }
  409             else
  410             {
  411                 if (setPtr)
  412                 {
  413                     setPtr->insert(facei);
  414                 }
  415
  416                 errorNonOrth++;
  417             }
  418         }
  419     }
```

[...]

```
  441         if (severeNonOrth > 0)
  442         {
  443             Info<< "   *Number of severely non-orthogonal faces: "
  444                 << severeNonOrth << "." << endl;
  445         }
  446     }
  447
  448     if (errorNonOrth > 0)
  449     {
  450         if (debug || report)
  451         {
  452             Info<< " ***Number of non-orthogonality errors: "
  453                 << errorNonOrth << "." << endl;
  454         }
  455
  456         return true;
  457     }
  458
  459     if (debug || report)
  460     {
  461         Info<< "    Non-orthogonality check OK." << endl;
  462     }
  463
  464     return false;
  465 }
```

[...]

```
  565 bool Foam::primitiveMesh::checkFaceSkewness
  566 (
  567     const pointField& points,
  568     const vectorField& fCtrs,
  569     const vectorField& fAreas,
  570     const vectorField& cellCtrs,
  571     const bool report,
  572     labelHashSet* setPtr
  573 ) const
  574 {
  575     DebugInFunction << "Checking face skewness" << endl;
  576
  577     // Warn if the skew correction vector is more than skewWarning times
  578     // larger than the face area vector
```

[...]

```
  593     forAll(skewness, facei)
  594     {
  595         // Check if the skewness vector is greater than the PN vector.
  596         // This does not cause trouble but is a good indication of a poor mesh.
  597         if (skewness[facei] > skewThreshold_)
  598         {
  599             if (setPtr)
  600             {
  601                 setPtr->insert(facei);
  602             }
  603
  604             nWarnSkew++;
  605         }
  606     }
```

[...]

```
  611     if (nWarnSkew > 0)
  612     {
  613         if (debug || report)
  614         {
  615             Info<< " ***Max skewness = " << maxSkew
  616                 << ", " << nWarnSkew << " highly skew faces detected"
  617                    " which may impair the quality of the results"
  618                 << endl;
  619         }
  620
  621         return true;
  622     }
  623
  624     if (debug || report)
  625     {
  626         Info<< "    Max skewness = " << maxSkew << " OK." << endl;
  627     }
  628
  629     return false;
  630 }
```
