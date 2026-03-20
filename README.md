# QGIS Polygon Topology Cleaner (PyQGIS)

A practical topology-cleaning workflow for polygon layers in QGIS.

This script combines geometry repair, controlled snapping, and rule-based vertex cleanup to reduce topology errors while keeping shapes stable. It is designed for large datasets and can be run iteratively until results stop improving.

---

## What it does

- Fixes invalid polygon geometries
- Aligns shared boundaries between adjacent features
- Removes spurious vertices using simple geometric rules:
  - short edges
  - near-collinearity
  - proximity to an optional outline
- Limits area distortion during internal cleanup
- Optionally snaps outer boundaries to a reference layer

---

## Why use it

QGIS has tools for fixing geometries and snapping, but they work in isolation. This script puts those steps together and adds constraints so the cleanup stays local and predictable.

The goal is not perfect reconstruction, but fast and controlled error reduction.

---

## Typical use cases

- Voronoi / tessellation cleanup
- Digitized polygon datasets
- Pre-processing for spatial analysis
- QA/QC pipelines on large layers

---

## How to run

1. Open your project in QGIS  
2. Load your polygon layer  
3. (Optional) Load an outline/boundary layer  
4. Open the Python console  
5. Run the script  

All parameters are defined at the top of the file.

---

## Notes

- Changes are written to the edit buffer (not committed automatically)
- If no outline layer is found, boundary snapping is skipped
- All tolerances use the layer CRS units
- You can run the script multiple times until the result stabilizes

---

## Example

Topology error reduction on a synthetic dataset:

- Initial errors: ~1800  
- After 3 iterations: ~39  

---

## Limitations

This workflow focuses on local corrections.  
It does not rebuild topology globally on heavily degraded data.

---

## License

MIT
