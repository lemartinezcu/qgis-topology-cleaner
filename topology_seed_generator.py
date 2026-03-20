"""
Synthetic Voronoi Topology Error Generator (PyQGIS)

Creates a reproducible in-memory polygon dataset with intentionally perturbed
Voronoi cells, useful for testing and benchmarking topology-cleaning workflows.

Workflow:
1) Generate random points
2) Build Voronoi polygons
3) Inject controlled coordinate perturbations and occasional duplicated vertices
4) Export result as an in-memory polygon layer

Output layer name:
- voronoi_topology_errors

Author: Leonardo Martínez
"""

from qgis.core import *
from qgis.PyQt.QtCore import QVariant
import processing
import random

# =============================================================================
# CONFIGURATION
# =============================================================================
CRS_AUTHID = "EPSG:4326"
POINT_LAYER_NAME = "points"
OUTPUT_LAYER_NAME = "voronoi_topology_errors"

NUM_POINTS = 1000
X_MIN, X_MAX = 0.0, 1.0
Y_MIN, Y_MAX = 0.0, 1.0

# Magnitude of random coordinate perturbation applied to Voronoi vertices
ERROR_MAGNITUDE = 0.0005

# Probability of inserting a duplicated vertex in a ring
DUPLICATE_VERTEX_PROBABILITY = 0.30

# Optional seed for reproducibility (set to None for non-deterministic runs)
RANDOM_SEED = None


# =============================================================================
# SETUP
# =============================================================================
if RANDOM_SEED is not None:
    random.seed(RANDOM_SEED)

# Create random points layer
point_layer = QgsVectorLayer(f"Point?crs={CRS_AUTHID}", POINT_LAYER_NAME, "memory")
prov_points = point_layer.dataProvider()

point_features = []
for _ in range(NUM_POINTS):
    x = random.uniform(X_MIN, X_MAX)
    y = random.uniform(Y_MIN, Y_MAX)

    feat = QgsFeature()
    feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
    point_features.append(feat)

prov_points.addFeatures(point_features)
point_layer.updateExtents()

# =============================================================================
# GENERATE VORONOI
# =============================================================================
voronoi = processing.run(
    "qgis:voronoipolygons",
    {
        "INPUT": point_layer,
        "BUFFER": 0,
        "OUTPUT": "memory:"
    }
)["OUTPUT"]

# =============================================================================
# INJECT TOPOLOGY NOISE
# =============================================================================
final_layer = QgsVectorLayer(f"Polygon?crs={CRS_AUTHID}", OUTPUT_LAYER_NAME, "memory")
prov_final = final_layer.dataProvider()

prov_final.addAttributes([QgsField("ID", QVariant.Int)])
final_layer.updateFields()

new_features = []

for i, f in enumerate(voronoi.getFeatures(), start=1):
    geom = f.geometry()
    if geom is None or geom.isEmpty():
        continue

    # Handle singlepart and multipart polygons
    polygons = []
    if geom.isMultipart():
        polygons = geom.asMultiPolygon()
    else:
        polygons = [geom.asPolygon()]

    out_multipoly = []

    for poly in polygons:
        out_poly = []
        for ring in poly:
            if not ring:
                continue

            new_ring = []
            for pt in ring:
                dx = random.choice([0.0, ERROR_MAGNITUDE, -ERROR_MAGNITUDE])
                dy = random.choice([0.0, ERROR_MAGNITUDE, -ERROR_MAGNITUDE])
                new_ring.append(QgsPointXY(pt.x() + dx, pt.y() + dy))

            # Randomly duplicate one interior vertex to simulate local defects
            if len(new_ring) > 3 and random.random() < DUPLICATE_VERTEX_PROBABILITY:
                idx = random.randint(1, len(new_ring) - 2)
                new_ring.insert(idx, QgsPointXY(new_ring[idx]))

            # Ensure ring closure
            if len(new_ring) > 0:
                if new_ring[0].x() != new_ring[-1].x() or new_ring[0].y() != new_ring[-1].y():
                    new_ring.append(QgsPointXY(new_ring[0]))

            out_poly.append(new_ring)

        if out_poly:
            out_multipoly.append(out_poly)

    if not out_multipoly:
        continue

    if len(out_multipoly) == 1:
        new_geom = QgsGeometry.fromPolygonXY(out_multipoly[0])
    else:
        new_geom = QgsGeometry.fromMultiPolygonXY(out_multipoly)

    new_feat = QgsFeature(final_layer.fields())
    new_feat.setGeometry(new_geom)
    new_feat.setAttributes([i])
    new_features.append(new_feat)

prov_final.addFeatures(new_features)
final_layer.updateExtents()

QgsProject.instance().addMapLayer(final_layer)

print(f"Created layer '{OUTPUT_LAYER_NAME}' with {len(new_features)} features.")
