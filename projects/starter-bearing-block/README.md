# Starter Project: 8mm REX Twin-Bearing Pillow Block

A minimal end-to-end example demonstrating:
1. Parametric custom part modeling in `build123d` (`BearingPillowBlock`) on a 16 mm M4 grid with counterbored mounting holes and bearing bores.
2. Loading verified COTS goBILDA 8 mm REX flanged bearings (`1611-0514-4008`) from `cots/` using `load_cots_part()`.
3. Exporting multi-part `.step`, printable `.stl` (`fabrication/prints/`), BOM, `*_constraints.json`, and compiling a jointed SolidCraft `.sc` assembly + animated motion GIF.

## Run It

```powershell
.\.venv\Scripts\python.exe tools\cad_pipeline.py build projects\starter-bearing-block\src\build_bearing_block.py
.\.venv\Scripts\python.exe tools\cad_pipeline.py render projects\starter-bearing-block\cad\starter_bearing_block.step --out projects\starter-bearing-block\docs
.\.venv\Scripts\python.exe tools\cad_pipeline.py slice-audit projects\starter-bearing-block\fabrication\prints\bearing_pillow_block.stl
```
