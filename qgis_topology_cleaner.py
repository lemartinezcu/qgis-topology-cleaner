"""
QGIS Polygon Topology Cleaner (PyQGIS)

A reproducible topology-cleaning workflow for polygon layers in QGIS.
This script is designed for large datasets and combines robust geometry repair
with controlled local vertex cleanup to improve topological consistency while
minimizing shape distortion.

Main capabilities:
- Repairs invalid polygon geometries
- Harmonizes shared boundaries between adjacent polygons
- Removes local spurious vertices based on geometric rules
  (short-edge + near-collinearity + proximity to optional outline)
- Enforces per-feature area-change limits during internal cleanup
- Optionally snaps outer boundaries to a reference outline
  (without area restriction in the outline phase)

Typical use cases:
- Voronoi or tessellation post-processing
- Boundary normalization before spatial analysis/modeling
- Large-scale polygon quality assurance pipelines

Notes:
- Changes are written to the layer edit buffer (not auto-committed).
- If an outline layer is not found or is empty, outline snapping is skipped.
- All distances are interpreted in the input layer CRS units.
- The script can be executed iteratively until convergence (i.e., topology error counts no longer decrease between runs).

Author: Leonardo Martínez
"""

from qgis.core import *
from qgis.PyQt.QtCore import QVariant
import processing
import math

# =============================================================================
# CONFIGURATION
# =============================================================================
# Input polygon layer to be cleaned. Must be a polygon geometry layer loaded in the QGIS project.
INPUT_LAYER_NAME = "voronoi_topology_errors"

# Optional boundary/constraint polygon layer used to snap outer vertices.
# If not found (or empty), boundary snapping is skipped automatically.
OUTLINE_LAYER_NAME = "Outline"

# If True, only selected features from INPUT_LAYER_NAME are processed.
# If False, the entire layer is processed.
PROCESS_SELECTED_ONLY = True

# Base snapping tolerance in layer CRS units.
# This controls both internal topology alignment and optional outline snapping.
SNAP_TOLERANCE = 0.002

# Minimum short-edge threshold for candidate vertex cleanup (derived from SNAP_TOLERANCE by default).
SHORT_EDGE_FACTOR = 0.8  # d_min = SNAP_TOLERANCE * SHORT_EDGE_FACTOR

# Collinearity threshold factor for identifying near-linear spikes (derived from SNAP_TOLERANCE).
COLLINEAR_FACTOR = 0.15  # collinear_eps = SNAP_TOLERANCE * COLLINEAR_FACTOR

# Maximum allowed area change ratio (per feature) for INTERNAL cleanup steps only.
# Example: 0.002 = 0.2% maximum area change.
MAX_INTERNAL_AREA_DELTA_RATIO = 0.002

# Decimal precision applied after local cleanup to stabilize coordinates.
ROUND_DECIMALS = 6

# Geometry fixing method for native:fixgeometries
# 1 = "Structure" (recommended in most topology-cleanup scenarios)
FIX_METHOD = 1

# Snap behavior for native:snapgeometries
# 0 = Prefer closest point, 1 = Prefer closest vertex, etc. (QGIS native behavior codes)
SNAP_BEHAVIOR = 0

# Whether to perform optional outline snap when outline layer exists and has valid geometry.
ENABLE_OUTLINE_SNAP = True

# If True, script starts editing automatically if layer is not editable.
AUTO_START_EDITING = True

# If True, print additional run metadata.
VERBOSE = True

# Force-fit tuning for outline phase
FORCE_OUTLINE_FIT = True
OUTLINE_FORCE_FACTOR = 1.5  # effective threshold = SNAP_TOLERANCE * OUTLINE_FORCE_FACTOR

# If True, forced outline projection is applied ONLY to exterior rings.
# This helps preserve interior holes from unintended boundary projection.
FORCE_OUTLINE_OUTER_RING_ONLY = True


# =============================================================================
# PROCESSING CONTEXT
# =============================================================================
ctx = QgsProcessingContext()
ctx.setInvalidGeometryCheck(QgsFeatureRequest.GeometryNoCheck)
feedback = QgsProcessingFeedback()

def runalg(alg_id, params):
    return processing.run(alg_id, params, context=ctx, feedback=feedback)


# =============================================================================
# HELPERS
# =============================================================================
def compute_metrics(features):
    invalid = 0
    total_area = 0.0
    total_vertices = 0
    for f in features:
        g = f.geometry()
        if g is None or g.isEmpty():
            continue
        if not g.isGeosValid():
            invalid += 1
        total_area += g.area()
        if g.isMultipart():
            for part in g.asMultiPolygon():
                for ring in part:
                    total_vertices += len(ring)
        else:
            for ring in g.asPolygon():
                total_vertices += len(ring)
    return invalid, total_area, total_vertices

def round_geom(geom, decimals):
    if geom is None or geom.isEmpty():
        return geom

    def _round_ring(ring):
        rr = [QgsPointXY(round(p.x(), decimals), round(p.y(), decimals)) for p in ring]
        if len(rr) > 0 and (rr[0].x() != rr[-1].x() or rr[0].y() != rr[-1].y()):
            rr.append(QgsPointXY(rr[0]))
        return rr

    if geom.isMultipart():
        mp = geom.asMultiPolygon()
        out_mp = []
        for part in mp:
            out_part = []
            for ring in part:
                r = _round_ring(ring)
                if len(r) >= 4:
                    out_part.append(r)
            if out_part:
                out_mp.append(out_part)
        if not out_mp:
            return QgsGeometry()
        return QgsGeometry.fromMultiPolygonXY(out_mp)
    else:
        p = geom.asPolygon()
        out_p = []
        for ring in p:
            r = _round_ring(ring)
            if len(r) >= 4:
                out_p.append(r)
        if not out_p:
            return QgsGeometry()
        return QgsGeometry.fromPolygonXY(out_p)

def triangle_twice_area(a, b, c):
    return abs((b.x()-a.x())*(c.y()-a.y()) - (b.y()-a.y())*(c.x()-a.x()))

def ensure_closed_ring(open_pts):
    if len(open_pts) < 3:
        return None
    ring = open_pts + [QgsPointXY(open_pts[0])]
    cleaned = [ring[0]]
    for p in ring[1:]:
        if p.x() != cleaned[-1].x() or p.y() != cleaned[-1].y():
            cleaned.append(p)
    if len(cleaned) < 4:
        return None
    if cleaned[0].x() != cleaned[-1].x() or cleaned[0].y() != cleaned[-1].y():
        cleaned.append(QgsPointXY(cleaned[0]))
    if len(cleaned) < 4:
        return None
    return cleaned

def ring_cleanup_local(ring, outline_union_geom, snap_tol, d_min, colinear_eps):
    if ring is None or len(ring) < 5:
        return ring

    # No outline geometry => skip "near_outline" cleanup rule
    if outline_union_geom is None or outline_union_geom.isEmpty():
        return ring

    open_pts = ring[:-1]
    n = len(open_pts)
    keep = [True] * n

    for i in range(n):
        a = open_pts[(i - 1) % n]
        b = open_pts[i]
        c = open_pts[(i + 1) % n]

        ab = math.hypot(b.x() - a.x(), b.y() - a.y())
        bc = math.hypot(c.x() - b.x(), c.y() - b.y())
        tri2 = triangle_twice_area(a, b, c)

        b_geom = QgsGeometry.fromPointXY(b)
        near_outline = (b_geom.distance(outline_union_geom) <= snap_tol * 1.2)

        short_edge = (ab < d_min) or (bc < d_min)
        near_colinear = (tri2 < colinear_eps)

        if near_outline and short_edge and near_colinear:
            keep[i] = False

    new_open = [p for i, p in enumerate(open_pts) if keep[i]]
    new_ring = ensure_closed_ring(new_open)
    return new_ring if new_ring is not None else ring

def cleanup_geometry_local(geom, outline_union_geom, snap_tol, d_min, colinear_eps):
    if geom is None or geom.isEmpty():
        return geom

    if geom.isMultipart():
        mp = geom.asMultiPolygon()
        out_mp = []
        for part in mp:
            out_part = []
            for ring in part:
                rr = ring_cleanup_local(ring, outline_union_geom, snap_tol, d_min, colinear_eps)
                if rr is not None and len(rr) >= 4:
                    out_part.append(rr)
            if out_part:
                out_mp.append(out_part)
        if not out_mp:
            return QgsGeometry()
        return QgsGeometry.fromMultiPolygonXY(out_mp)
    else:
        poly = geom.asPolygon()
        out_poly = []
        for ring in poly:
            rr = ring_cleanup_local(ring, outline_union_geom, snap_tol, d_min, colinear_eps)
            if rr is not None and len(rr) >= 4:
                out_poly.append(rr)
        if not out_poly:
            return QgsGeometry()
        return QgsGeometry.fromPolygonXY(out_poly)

def force_outline_fit_geom(geom, outline_geom, threshold, outer_ring_only=True):
    if geom is None or geom.isEmpty() or outline_geom is None or outline_geom.isEmpty():
        return geom, 0

    moved = 0

    def _fit_ring(ring):
        nonlocal moved
        if ring is None or len(ring) < 4:
            return ring

        open_pts = ring[:-1]
        new_open = []

        for p in open_pts:
            pxy = QgsPointXY(p)
            pg = QgsGeometry.fromPointXY(pxy)
            d = pg.distance(outline_geom)

            if d <= threshold:
                _, closest, _, _ = outline_geom.closestSegmentWithContext(pxy)
                if closest is not None:
                    if closest.x() != pxy.x() or closest.y() != pxy.y():
                        moved += 1
                    new_open.append(QgsPointXY(closest))
                else:
                    new_open.append(pxy)
            else:
                new_open.append(pxy)

        out_ring = ensure_closed_ring(new_open)
        return out_ring if out_ring is not None else ring

    if geom.isMultipart():
        mp = geom.asMultiPolygon()
        out_mp = []
        for part in mp:
            out_part = []
            for ridx, ring in enumerate(part):
                # ridx == 0 is exterior ring in QGIS polygon ring ordering
                if outer_ring_only and ridx > 0:
                    rr = ring
                else:
                    rr = _fit_ring(ring)
                if rr is not None and len(rr) >= 4:
                    out_part.append(rr)
            if out_part:
                out_mp.append(out_part)

        if not out_mp:
            return QgsGeometry(), moved
        return QgsGeometry.fromMultiPolygonXY(out_mp), moved

    else:
        poly = geom.asPolygon()
        out_poly = []
        for ridx, ring in enumerate(poly):
            if outer_ring_only and ridx > 0:
                rr = ring
            else:
                rr = _fit_ring(ring)
            if rr is not None and len(rr) >= 4:
                out_poly.append(rr)

        if not out_poly:
            return QgsGeometry(), moved
        return QgsGeometry.fromPolygonXY(out_poly), moved


# =============================================================================
# VALIDATE INPUTS
# =============================================================================
layer_list = QgsProject.instance().mapLayersByName(INPUT_LAYER_NAME)
if not layer_list:
    raise Exception(f"Input layer '{INPUT_LAYER_NAME}' not found in project.")

layer = layer_list[0]
crs = layer.crs()

if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PolygonGeometry:
    raise Exception(f"Input layer '{INPUT_LAYER_NAME}' must be polygon geometry.")

outline_list = QgsProject.instance().mapLayersByName(OUTLINE_LAYER_NAME)
outline = outline_list[0] if len(outline_list) > 0 else None

outline_union_geom = None
outline_snap_enabled = False

if ENABLE_OUTLINE_SNAP and outline is not None:
    outline_geoms = [f.geometry() for f in outline.getFeatures() if f.geometry() and not f.geometry().isEmpty()]
    if len(outline_geoms) > 0:
        outline_union_geom = QgsGeometry.unaryUnion(outline_geoms)
        outline_snap_enabled = True

if PROCESS_SELECTED_ONLY and layer.selectedFeatureCount() > 0:
    selected_ids = layer.selectedFeatureIds()
    source_feats = list(layer.getFeatures(QgsFeatureRequest().setFilterFids(selected_ids)))
else:
    selected_ids = []
    source_feats = list(layer.getFeatures())

if len(source_feats) == 0:
    raise Exception("No features available to process (selection/layer is empty).")

if VERBOSE:
    print("===== RUN CONFIG =====")
    print(f"Input layer: {INPUT_LAYER_NAME}")
    print(f"Outline layer: {OUTLINE_LAYER_NAME}")
    print(f"Process selected only: {PROCESS_SELECTED_ONLY}")
    print(f"Selected count: {len(selected_ids)}")
    print(f"Snap tolerance: {SNAP_TOLERANCE}")
    print(f"Outline snap enabled: {outline_snap_enabled}")
    print(f"Force outline fit: {FORCE_OUTLINE_FIT}")
    print(f"Force outline outer ring only: {FORCE_OUTLINE_OUTER_RING_ONLY}")

invalid_before, area_before, vertices_before = compute_metrics(source_feats)

# Derived thresholds
D_MIN = SNAP_TOLERANCE * SHORT_EDGE_FACTOR
COLINEAR_EPS = SNAP_TOLERANCE * COLLINEAR_FACTOR

# =============================================================================
# BUILD SUBSET MEMORY LAYER WITH orig_fid
# =============================================================================
subset = QgsVectorLayer(f"Polygon?crs={crs.authid()}", "subset", "memory")
prov = subset.dataProvider()

fields = list(layer.fields())
fields.append(QgsField("orig_fid", QVariant.Int))
prov.addAttributes(fields)
subset.updateFields()

tmp_feats = []
for f in source_feats:
    nf = QgsFeature(subset.fields())
    nf.setGeometry(f.geometry())
    nf.setAttributes(f.attributes() + [f.id()])
    tmp_feats.append(nf)
prov.addFeatures(tmp_feats)

# =============================================================================
# PIPELINE
# Internal phase (with area guard)
#   1) fix
#   2) snap self
#   3) local cleanup near outline geometry
#   4) area guard
#   5) fix
#
# Outline phase (without area guard)
#   6) optional snap to outline
#   7) fix final
# =============================================================================
step1 = runalg("native:fixgeometries", {
    "INPUT": subset,
    "METHOD": FIX_METHOD,
    "OUTPUT": "memory:"
})["OUTPUT"]

step2 = runalg("native:snapgeometries", {
    "INPUT": step1,
    "REFERENCE_LAYER": step1,
    "TOLERANCE": SNAP_TOLERANCE,
    "BEHAVIOR": SNAP_BEHAVIOR,
    "OUTPUT": "memory:"
})["OUTPUT"]

# Internal cleanup + area guard
internal_clean = QgsVectorLayer(f"Polygon?crs={crs.authid()}", "internal_clean", "memory")
prov_i = internal_clean.dataProvider()
prov_i.addAttributes(step2.fields())
internal_clean.updateFields()

internal_feats = []
removed_vertex_events = 0
skipped_by_area_guard = 0

for f in step2.getFeatures():
    g_old = f.geometry()
    if g_old is None or g_old.isEmpty():
        continue

    old_area = g_old.area()
    old_vertices = 0
    if g_old.isMultipart():
        for part in g_old.asMultiPolygon():
            for ring in part:
                old_vertices += len(ring)
    else:
        for ring in g_old.asPolygon():
            old_vertices += len(ring)

    g_new = cleanup_geometry_local(g_old, outline_union_geom, SNAP_TOLERANCE, D_MIN, COLINEAR_EPS)
    g_new = round_geom(g_new, ROUND_DECIMALS)

    if g_new is None or g_new.isEmpty():
        g_new = g_old

    new_area = g_new.area()
    ratio = abs(new_area - old_area) / old_area if old_area > 0 else 0.0

    if ratio > MAX_INTERNAL_AREA_DELTA_RATIO:
        g_use = g_old
        skipped_by_area_guard += 1
    else:
        g_use = g_new
        new_vertices = 0
        if g_use.isMultipart():
            for part in g_use.asMultiPolygon():
                for ring in part:
                    new_vertices += len(ring)
        else:
            for ring in g_use.asPolygon():
                new_vertices += len(ring)
        if new_vertices < old_vertices:
            removed_vertex_events += (old_vertices - new_vertices)

    nf = QgsFeature(f)
    nf.setGeometry(g_use)
    internal_feats.append(nf)

prov_i.addFeatures(internal_feats)

step5 = runalg("native:fixgeometries", {
    "INPUT": internal_clean,
    "METHOD": FIX_METHOD,
    "OUTPUT": "memory:"
})["OUTPUT"]

# Outline phase (no area guard)
forced_outline_moves = 0
if outline_snap_enabled:
    step6 = runalg("native:snapgeometries", {
        "INPUT": step5,
        "REFERENCE_LAYER": outline,
        "TOLERANCE": SNAP_TOLERANCE,
        "BEHAVIOR": SNAP_BEHAVIOR,
        "OUTPUT": "memory:"
    })["OUTPUT"]

    if FORCE_OUTLINE_FIT and outline_union_geom is not None and not outline_union_geom.isEmpty():
        forced_layer = QgsVectorLayer(f"Polygon?crs={crs.authid()}", "forced_outline_fit", "memory")
        prov_f = forced_layer.dataProvider()
        prov_f.addAttributes(step6.fields())
        forced_layer.updateFields()

        threshold = SNAP_TOLERANCE * OUTLINE_FORCE_FACTOR
        out_feats = []

        for f in step6.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue

            g2, moved = force_outline_fit_geom(
                g, outline_union_geom, threshold,
                outer_ring_only=FORCE_OUTLINE_OUTER_RING_ONLY
            )
            forced_outline_moves += moved

            nf = QgsFeature(f)
            nf.setGeometry(g2 if g2 and not g2.isEmpty() else g)
            out_feats.append(nf)

        prov_f.addFeatures(out_feats)

        post_force = runalg("native:fixgeometries", {
            "INPUT": forced_layer,
            "METHOD": FIX_METHOD,
            "OUTPUT": "memory:"
        })["OUTPUT"]
    else:
        post_force = step6

    final = runalg("native:fixgeometries", {
        "INPUT": post_force,
        "METHOD": FIX_METHOD,
        "OUTPUT": "memory:"
    })["OUTPUT"]
    outline_msg = "Outline snap applied (no area restriction)."
else:
    final = runalg("native:fixgeometries", {
        "INPUT": step5,
        "METHOD": FIX_METHOD,
        "OUTPUT": "memory:"
    })["OUTPUT"]
    outline_msg = "Outline snap skipped (outline layer missing/empty or disabled)."

after_feats = list(final.getFeatures())
invalid_after, area_after, vertices_after = compute_metrics(after_feats)

# =============================================================================
# APPLY BACK TO SOURCE LAYER
# =============================================================================
if AUTO_START_EDITING and not layer.isEditable():
    layer.startEditing()

geom_by_fid = {}
for f in after_feats:
    ofid = f["orig_fid"]
    if ofid is not None:
        geom_by_fid[int(ofid)] = f.geometry()

changed = 0
for f in layer.getFeatures():
    if PROCESS_SELECTED_ONLY and selected_ids and f.id() not in selected_ids:
        continue
    ng = geom_by_fid.get(f.id())
    if ng is None or ng.isEmpty():
        continue
    if not f.geometry().equals(ng):
        ok = layer.changeGeometry(f.id(), ng)
        if ok:
            changed += 1
        else:
            print(f"WARNING: changeGeometry failed for FID {f.id()}")

layer.triggerRepaint()
iface.mapCanvas().refresh()

# =============================================================================
# REPORT
# =============================================================================
print("\n===== TOPOLOGY REPORT =====")
print(f"Invalid BEFORE: {invalid_before}")
print(f"Invalid AFTER:  {invalid_after}")
print(f"Vertices BEFORE: {vertices_before}")
print(f"Vertices AFTER:  {vertices_after}")
print(f"Area BEFORE: {area_before:.8f}")
print(f"Area AFTER:  {area_after:.8f}")
print(f"Features changed: {changed}")
print(f"Vertices removed (internal local rules): {removed_vertex_events}")
print(f"Skipped by internal area guard: {skipped_by_area_guard}")
print(f"Forced outline vertex moves: {forced_outline_moves}")
print("Changes applied to edit buffer (NOT committed).")
print(outline_msg)
