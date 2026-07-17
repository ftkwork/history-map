"""地图地理命中测试、HTML 模板与嵌入式 Leaflet 组件。"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QResizeEvent
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

from app.log import trace
from app.paths import ROOT

# --- geo_hit_test ---


def _ring_contains(lng: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > lat) != (yj > lat)) and (
            lng < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi
        ):
            inside = not inside
        j = i
    return inside


def _polygon_contains(lng: float, lat: float, rings: list[list[list[float]]]) -> bool:
    if not rings:
        return False
    if not _ring_contains(lng, lat, rings[0]):
        return False
    for hole in rings[1:]:
        if _ring_contains(lng, lat, hole):
            return False
    return True


def _geometry_contains(lng: float, lat: float, geometry: dict[str, Any]) -> bool:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon":
        return _polygon_contains(lng, lat, coords)
    if gtype == "MultiPolygon":
        return any(_polygon_contains(lng, lat, poly) for poly in coords)
    return False


def _bbox_area(geometry: dict[str, Any]) -> float:
    min_lng, max_lng = 180.0, -180.0
    min_lat, max_lat = 90.0, -90.0

    def visit(lng: float, lat: float) -> None:
        nonlocal min_lng, max_lng, min_lat, max_lat
        min_lng = min(min_lng, lng)
        max_lng = max(max_lng, lng)
        min_lat = min(min_lat, lat)
        max_lat = max(max_lat, lat)

    def walk_coords(obj: Any) -> None:
        if isinstance(obj, (int, float)):
            return
        if obj and isinstance(obj[0], (int, float)):
            visit(float(obj[0]), float(obj[1]))
            return
        for part in obj:
            walk_coords(part)

    walk_coords(geometry.get("coordinates"))
    return max(0.0, max_lng - min_lng) * max(0.0, max_lat - min_lat)


def _lng_variants(lng: float) -> list[float]:
    return [lng, lng - 360.0, lng + 360.0, lng - 720.0, lng + 720.0]


def _ring_area(ring: list[list[float]]) -> float:
    area = 0.0
    n = len(ring)
    if n < 3:
        return 0.0
    for i in range(n):
        j = (i + 1) % n
        area += ring[i][0] * ring[j][1] - ring[j][0] * ring[i][1]
    return abs(area) * 0.5


def _geometry_area(geometry: dict[str, Any]) -> float:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon" and coords:
        return _ring_area(coords[0])
    if gtype == "MultiPolygon":
        return sum(_ring_area(poly[0]) for poly in coords if poly)
    return _bbox_area(geometry)


def feature_contains_point(lat: float, lng: float, feature: dict) -> bool:
    geometry = feature.get("geometry")
    if not geometry:
        return False
    for lng_try in _lng_variants(lng):
        if _geometry_contains(lng_try, lat, geometry):
            return True
    return False


def feature_centroid(feature: dict) -> tuple[float, float]:
    """返回特征几何中心 (lat, lng)。"""
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates")
    if not coords:
        return 20.0, 0.0
    if geom.get("type") == "Polygon":
        ring = coords[0]
    elif geom.get("type") == "MultiPolygon":
        ring = coords[0][0]
    else:
        return 20.0, 0.0
    lat = sum(p[1] for p in ring) / len(ring)
    lng = sum(p[0] for p in ring) / len(ring)
    return lat, lng


def find_state_at(
    features_by_id: dict[str, dict],
    lat: float,
    lng: float,
    *,
    prefer_id: str | None = None,
) -> str | None:
    """返回点击位置处实际面积最小的政权 id。"""
    if prefer_id and prefer_id in features_by_id:
        if feature_contains_point(lat, lng, features_by_id[prefer_id]):
            return prefer_id

    best_id: str | None = None
    best_area = float("inf")

    for state_id, feature in features_by_id.items():
        if not feature_contains_point(lat, lng, feature):
            continue
        geometry = feature.get("geometry") or {}
        area = _geometry_area(geometry)
        if area < best_area:
            best_area = area
            best_id = state_id

    return best_id

# --- map_template ---

MAP_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link rel="stylesheet" href="data/static/leaflet/leaflet.css" />
  <script src="data/static/leaflet/leaflet.js"></script>
  <script src="qrc:///qtwebchannel/qwebchannel.js"></script>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    html, body, #map { width: 100%; height: 100%; background: #1a1f2e; }
    .leaflet-container {
      background: #1a1f2e;
      font-family: "Microsoft YaHei", sans-serif;
      -webkit-backface-visibility: hidden;
      backface-visibility: hidden;
    }
    .leaflet-pane.leaflet-labels-pane {
      pointer-events: none !important;
    }
    .labels-hidden .country-label-wrap { opacity: 0 !important; }
    .leaflet-control-attribution { font-size: 11px; }
    .country-label-wrap {
      background: transparent !important;
      border: none !important;
      box-shadow: none !important;
    }
    .country-label-wrap .country-label-text {
      display: inline-block;
      color: #9aa8bc;
      font-size: 12px;
      font-weight: bold;
      white-space: nowrap;
      text-shadow: 0 0 4px #1a1f2e, 0 0 8px #1a1f2e;
      pointer-events: none;
      user-select: none;
    }
    .country-label-wrap.tier-1 .country-label-text { font-size: 13px; color: #b8c5d8; }
    .country-label-wrap.selected-name .country-label-text {
      font-size: 14px;
      color: #f0c674;
    }
    .country-label-wrap.tier-2 .country-label-text { font-size: 11px; }
    .country-label-wrap.tier-3 .country-label-text { font-size: 10px; color: #7d8da3; }
    .leaflet-div-icon.country-label-wrap {
      background: transparent !important;
      border: none !important;
    }
    .regime-popup { min-width: 200px; line-height: 1.5; }
  </style>
</head>
<body>
  <div id="map"></div>
  <script>
    var map, bridge;
    var basemapLayer, territoryLayer, labelLayer;
    var storedTerritoryGeojson = null;
    var storedTerritorySnapshotYear = null;
    var highlightedStateId = null;
    var prevHighlightedStateId = null;
    var layersByStateId = {};
    var labelMarkers = [];
    var interactionLock = false;
    var userDragging = false;
    var initialViewSet = false;
    var suppressMoveEnd = false;
    var pendingMapEngineReady = false;
    var pendingBasemapReady = false;
    var pendingYearApplied = null;
    var snapshotsReady = false;
    var loadingYear = false;
    var requestedSnapshotYear = null;
    var yearLoadToken = 0;
    var activeLoadYear = null;
    var yearGeoCache = {};
    var offsetGeoCache = {};
    var snapshotYears = [];
    var viewSettleTimer = null;
    var isMapMoving = false;
    var mapRenderer = null;
    var territoryRenderer = null;
    var forceYearInstall = false;
    var lastSidebarSyncGen = 0;

    const DEFAULT_CENTER = [20, 0];
    const DEFAULT_ZOOM = 2;
    const LAT_MIN = -58;
    const LAT_MAX = 72;
    const WRAP_OFFSETS = [-720, -360, 0, 360, 720];
    const WRAP_CENTER_MIN = -360;
    const WRAP_CENTER_MAX = 360;
    const VIEW_SETTLE_MS = 120;

    new QWebChannel(qt.webChannelTransport, function(channel) {
      bridge = channel.objects.bridge;
      if (snapshotsReady && bridge && bridge.notifySnapshotsReady) {
        bridge.notifySnapshotsReady(1);
      }
      if (pendingMapEngineReady && bridge && bridge.notifyMapEngineReady) {
        pendingMapEngineReady = false;
        bridge.notifyMapEngineReady();
      }
      if (pendingBasemapReady && bridge && bridge.notifyBasemapReady) {
        pendingBasemapReady = false;
        bridge.notifyBasemapReady();
      }
      if (pendingYearApplied && bridge && bridge.notifyYearApplied) {
        bridge.notifyYearApplied(pendingYearApplied.year, pendingYearApplied.count);
        pendingYearApplied = null;
      } else if (
        storedTerritorySnapshotYear != null &&
        storedTerritoryGeojson &&
        bridge &&
        bridge.notifyYearApplied
      ) {
        bridge.notifyYearApplied(
          storedTerritorySnapshotYear,
          storedTerritoryGeojson.features.length
        );
      }
    });

    function notifyMapEngineReadyNow() {
      if (bridge && bridge.notifyMapEngineReady) {
        bridge.notifyMapEngineReady();
      } else {
        pendingMapEngineReady = true;
      }
    }

    function reportLoadProgress(percent, message) {
      if (bridge && bridge.notifyLoadProgress) {
        bridge.notifyLoadProgress(percent, message);
      }
    }

    function offsetCoords(coords, deltaLng) {
      if (typeof coords[0] === 'number') {
        return [coords[0] + deltaLng, coords[1]];
      }
      return coords.map(function(c) { return offsetCoords(c, deltaLng); });
    }

    function cloneCoords(coords) {
      if (typeof coords[0] === 'number') {
        return [coords[0], coords[1]];
      }
      return coords.map(cloneCoords);
    }

    function offsetFeature(feature, deltaLng) {
      var geom = feature.geometry;
      var coords = cloneCoords(geom.coordinates);
      if (deltaLng !== 0) {
        coords = offsetCoords(coords, deltaLng);
      }
      return {
        type: 'Feature',
        properties: feature.properties,
        geometry: {
          type: geom.type,
          coordinates: coords
        }
      };
    }

    function withLngOffsets(geojson, offsets) {
      var features = [];
      var i, j;
      if (!geojson || !geojson.features) return { type: 'FeatureCollection', features: [] };
      for (i = 0; i < geojson.features.length; i++) {
        for (j = 0; j < offsets.length; j++) {
          features.push(offsetFeature(geojson.features[i], offsets[j]));
        }
      }
      return { type: 'FeatureCollection', features: features };
    }

    var REGIME_GOLDEN_ANGLE = 137.508;

    function generateUniqueRegimePalette(count) {
      var palette = [];
      var seen = {};
      var index = 0;
      while (palette.length < count) {
        var hue = (index * REGIME_GOLDEN_ANGLE) % 360;
        var band = Math.floor(index / 13);
        var sat = 52 + (band % 4) * 10 + (index % 3) * 4;
        var light = 38 + (Math.floor(band / 4) % 5) * 9 + (index % 2) * 7;
        sat = Math.min(84, Math.max(48, sat));
        light = Math.min(70, Math.max(36, light));
        var key = hue + '|' + sat + '|' + light;
        if (!seen[key]) {
          seen[key] = true;
          palette.push({ h: hue, s: sat, l: light });
        }
        index += 1;
      }
      return palette;
    }

    function regimeHslToCss(color) {
      return 'hsl(' + Math.round(color.h) + ', ' + color.s + '%, ' + color.l + '%)';
    }

    function regimeHslToRgb(color) {
      var h = (color.h % 360) / 360;
      var s = color.s / 100;
      var l = color.l / 100;
      if (s === 0) return { r: l, g: l, b: l };
      var q = l < 0.5 ? l * (1 + s) : l + s - l * s;
      var p = 2 * l - q;
      function hueToRgb(pVal, qVal, t) {
        if (t < 0) t += 1;
        if (t > 1) t -= 1;
        if (t < 1 / 6) return pVal + (qVal - pVal) * 6 * t;
        if (t < 1 / 2) return qVal;
        if (t < 2 / 3) return pVal + (qVal - pVal) * (2 / 3 - t) * 6;
        return pVal;
      }
      return {
        r: hueToRgb(p, q, h + 1 / 3),
        g: hueToRgb(p, q, h),
        b: hueToRgb(p, q, h - 1 / 3)
      };
    }

    function regimeColorDistance(a, b) {
      var left = regimeHslToRgb(a);
      var right = regimeHslToRgb(b);
      var dr = left.r - right.r;
      var dg = left.g - right.g;
      var db = left.b - right.b;
      return Math.sqrt(dr * dr + dg * dg + db * db);
    }

    function scoreRegimeCandidate(candidate, neighborColors, assignedColors) {
      var neighborScore = 999;
      var globalScore = 999;
      var i;
      if (neighborColors.length) {
        neighborScore = neighborColors.reduce(function(minDist, color) {
          return Math.min(minDist, regimeColorDistance(candidate, color));
        }, 999);
      }
      if (assignedColors.length) {
        globalScore = assignedColors.reduce(function(minDist, color) {
          return Math.min(minDist, regimeColorDistance(candidate, color));
        }, 999);
      }
      if (neighborColors.length) return neighborScore * 4 + globalScore;
      return globalScore;
    }

    function regimeFeatureBBox(geometry) {
      var minLng = 180, minLat = 90, maxLng = -180, maxLat = -90;
      function visit(coords) {
        if (typeof coords[0] === 'number') {
          minLng = Math.min(minLng, coords[0]);
          maxLng = Math.max(maxLng, coords[0]);
          minLat = Math.min(minLat, coords[1]);
          maxLat = Math.max(maxLat, coords[1]);
          return;
        }
        coords.forEach(visit);
      }
      visit(geometry.coordinates);
      return { minLng: minLng, minLat: minLat, maxLng: maxLng, maxLat: maxLat };
    }

    function regimeRingEdges(coords) {
      var edges = [];
      var i, p1, p2;
      for (i = 0; i < coords.length - 1; i++) {
        p1 = [Math.round(coords[i][0] * 1000) / 1000, Math.round(coords[i][1] * 1000) / 1000];
        p2 = [Math.round(coords[i + 1][0] * 1000) / 1000, Math.round(coords[i + 1][1] * 1000) / 1000];
        edges.push(p1[0] < p2[0] || (p1[0] === p2[0] && p1[1] <= p2[1]) ? [p1, p2] : [p2, p1]);
      }
      return edges;
    }

    function regimeGeometryEdges(geometry) {
      var edges = [];
      if (geometry.type === 'Polygon') {
        return regimeRingEdges(geometry.coordinates[0]);
      }
      if (geometry.type === 'MultiPolygon') {
        geometry.coordinates.forEach(function(poly) {
          regimeRingEdges(poly[0]).forEach(function(edge) {
            edges.push(edge);
          });
        });
      }
      return edges;
    }

    function regimeGridCells(geometry, cellSize) {
      var box = regimeFeatureBBox(geometry);
      var cells = {};
      var x = Math.floor(box.minLng / cellSize);
      while (x * cellSize <= box.maxLng + 1e-9) {
        var y = Math.floor(box.minLat / cellSize);
        while (y * cellSize <= box.maxLat + 1e-9) {
          cells[x + ',' + y] = true;
          y += 1;
        }
        x += 1;
      }
      return cells;
    }

    function regimeBboxesTouch(a, b) {
      return !(
        a.maxLng + 0.01 < b.minLng ||
        b.maxLng + 0.01 < a.minLng ||
        a.maxLat + 0.01 < b.minLat ||
        b.maxLat + 0.01 < a.minLat
      );
    }

    function regimeFeatureCentroid(geometry) {
      var box = regimeFeatureBBox(geometry);
      return {
        lng: (box.minLng + box.maxLng) / 2,
        lat: (box.minLat + box.maxLat) / 2
      };
    }

    function buildRegimeAdjacency(features) {
      var edgeNames = {};
      var nameCells = {};
      var nameBBox = {};
      var centroids = {};
      var adjacency = {};
      var i, feature, props, name, geometry, edgeKey, names, left, right, j;

      function addAdj(a, b) {
        if (!adjacency[a]) adjacency[a] = {};
        if (!adjacency[b]) adjacency[b] = {};
        adjacency[a][b] = true;
        adjacency[b][a] = true;
      }

      for (i = 0; i < features.length; i++) {
        feature = features[i];
        props = feature.properties || {};
        name = props.name_en;
        if (!name) continue;
        geometry = feature.geometry || {};
        nameBBox[name] = regimeFeatureBBox(geometry);
        centroids[name] = regimeFeatureCentroid(geometry);
        if (!nameCells[name]) nameCells[name] = {};
        var cells = regimeGridCells(geometry, 0.25);
        Object.keys(cells).forEach(function(key) {
          nameCells[name][key] = true;
        });
        regimeGeometryEdges(geometry).forEach(function(edge) {
          edgeKey = edge[0][0] + ',' + edge[0][1] + '|' + edge[1][0] + ',' + edge[1][1];
          if (!edgeNames[edgeKey]) edgeNames[edgeKey] = {};
          edgeNames[edgeKey][name] = true;
        });
      }

      Object.keys(edgeNames).forEach(function(key) {
        names = Object.keys(edgeNames[key]);
        if (names.length < 2) return;
        for (i = 0; i < names.length; i++) {
          for (j = i + 1; j < names.length; j++) {
            addAdj(names[i], names[j]);
          }
        }
      });

      names = Object.keys(nameCells);
      for (i = 0; i < names.length; i++) {
        left = names[i];
        for (j = i + 1; j < names.length; j++) {
          right = names[j];
          if (adjacency[left] && adjacency[left][right]) continue;
          if (!regimeBboxesTouch(nameBBox[left], nameBBox[right])) continue;
          var shared = false;
          Object.keys(nameCells[left]).some(function(cellKey) {
            if (nameCells[right][cellKey]) {
              shared = true;
              return true;
            }
            return false;
          });
          if (shared) addAdj(left, right);
        }
      }

      for (i = 0; i < names.length; i++) {
        left = names[i];
        for (j = i + 1; j < names.length; j++) {
          right = names[j];
          if (adjacency[left] && adjacency[left][right]) continue;
          if (!regimeBboxesTouch(nameBBox[left], nameBBox[right])) continue;
          var latScale = Math.cos(((centroids[left].lat + centroids[right].lat) / 2) * Math.PI / 180);
          if (latScale < 0.2) latScale = 0.2;
          var dx = (centroids[right].lng - centroids[left].lng) * latScale;
          var dy = centroids[right].lat - centroids[left].lat;
          if (Math.sqrt(dx * dx + dy * dy) <= 18) addAdj(left, right);
        }
      }

      return adjacency;
    }

    function improveAdjacentRegimeContrast(assignment, adjacency) {
      var names = Object.keys(assignment);
      var round, left, right, donor, improved, idx, neighbor, newDist, oldDist;
      for (round = 0; round < 12; round++) {
        improved = false;
        for (idx = 0; idx < names.length; idx++) {
          left = names[idx];
          if (!adjacency[left]) continue;
          for (right in adjacency[left]) {
            if (left >= right) continue;
            if (regimeColorDistance(assignment[left], assignment[right]) >= 0.28) continue;
            for (idx = 0; idx < names.length; idx++) {
              donor = names[idx];
              if (donor === left || donor === right) continue;
              if (adjacency[left] && adjacency[left][donor]) continue;
              if (adjacency[donor] && adjacency[donor][right]) continue;
              newDist = regimeColorDistance(assignment[donor], assignment[right]);
              oldDist = regimeColorDistance(assignment[left], assignment[right]);
              if (newDist <= oldDist) continue;
              var leftOk = true;
              var donorOk = true;
              for (neighbor in adjacency[left]) {
                if (neighbor === right) continue;
                if (regimeColorDistance(assignment[donor], assignment[neighbor]) <
                    regimeColorDistance(assignment[left], assignment[neighbor]) * 0.85) {
                  leftOk = false;
                  break;
                }
              }
              if (!leftOk) continue;
              if (adjacency[donor]) {
                for (neighbor in adjacency[donor]) {
                  if (regimeColorDistance(assignment[left], assignment[neighbor]) <
                      regimeColorDistance(assignment[donor], assignment[neighbor]) * 0.85) {
                    donorOk = false;
                    break;
                  }
                }
              }
              if (!donorOk) continue;
              var swap = assignment[left];
              assignment[left] = assignment[donor];
              assignment[donor] = swap;
              improved = true;
              break;
            }
            if (improved) break;
          }
          if (improved) break;
        }
        if (!improved) break;
      }
    }

    function assignRegimeColorMap(adjacency, regimeNames) {
      var palette = generateUniqueRegimePalette(regimeNames.length);
      var order = regimeNames.slice().sort(function(a, b) {
        var da = adjacency[a] ? Object.keys(adjacency[a]).length : 0;
        var db = adjacency[b] ? Object.keys(adjacency[b]).length : 0;
        if (db !== da) return db - da;
        return a.localeCompare(b);
      });
      var assignment = {};
      var used = {};
      var idx, regimeName, neighborNames, neighborColors, assignedColors;
      var bestIndex, bestScore, score, colorIndex, candidate;

      order.forEach(function(regimeName) {
        neighborNames = adjacency[regimeName] ? Object.keys(adjacency[regimeName]) : [];
        neighborColors = [];
        assignedColors = [];
        for (idx = 0; idx < neighborNames.length; idx++) {
          if (assignment[neighborNames[idx]]) neighborColors.push(assignment[neighborNames[idx]]);
        }
        Object.keys(assignment).forEach(function(key) {
          assignedColors.push(assignment[key]);
        });

        bestIndex = -1;
        bestScore = -1;
        for (colorIndex = 0; colorIndex < palette.length; colorIndex++) {
          if (used[colorIndex]) continue;
          candidate = palette[colorIndex];
          score = scoreRegimeCandidate(candidate, neighborColors, assignedColors);
          if (score > bestScore) {
            bestScore = score;
            bestIndex = colorIndex;
          }
        }
        if (bestIndex < 0) {
          for (colorIndex = 0; colorIndex < palette.length; colorIndex++) {
            if (!used[colorIndex]) {
              bestIndex = colorIndex;
              break;
            }
          }
        }
        used[bestIndex] = true;
        assignment[regimeName] = palette[bestIndex];
      });

      improveAdjacentRegimeContrast(assignment, adjacency);

      var css = {};
      Object.keys(assignment).forEach(function(regimeName) {
        css[regimeName] = regimeHslToCss(assignment[regimeName]);
      });
      return css;
    }

    function assignRegimeColors(geojson) {
      if (!geojson || !geojson.features || !geojson.features.length) return geojson;
      var adjacency = buildRegimeAdjacency(geojson.features);
      var names = [];
      var seen = {};
      var i, feature, props, name;
      for (i = 0; i < geojson.features.length; i++) {
        name = (geojson.features[i].properties || {}).name_en;
        if (name && !seen[name]) {
          seen[name] = true;
          names.push(name);
        }
      }
      var colors = assignRegimeColorMap(adjacency, names);
      var out = {
        type: geojson.type,
        snapshot_year: geojson.snapshot_year,
        requested_year: geojson.requested_year,
        features: []
      };
      for (i = 0; i < geojson.features.length; i++) {
        feature = geojson.features[i];
        props = feature.properties || {};
        name = props.name_en;
        out.features.push({
          type: feature.type,
          properties: Object.assign({}, props, {
            color: name && colors[name] ? colors[name] : (props.color || '#ffffff')
          }),
          geometry: feature.geometry
        });
      }
      return out;
    }

    function territoryStyle(feature) {
      var color = feature.properties.color || '#ffffff';
      return {
        color: color,
        weight: 1.5,
        opacity: 1,
        fillColor: color,
        fillOpacity: 0.5,
        lineJoin: 'round',
        lineCap: 'round'
      };
    }

    function highlightStyle(feature) {
      var base = territoryStyle(feature);
      base.fillOpacity = 0.68;
      return base;
    }

    function featureCentroid(feature) {
      var g = feature.geometry;
      var points = [];
      if (g.type === 'Polygon') {
        g.coordinates[0].forEach(function(c) { points.push(c); });
      } else if (g.type === 'MultiPolygon') {
        g.coordinates.forEach(function(poly) {
          poly[0].forEach(function(c) { points.push(c); });
        });
      }
      if (!points.length) return { lat: 20, lng: 0 };
      var lng = 0, lat = 0, i;
      for (i = 0; i < points.length; i++) {
        lng += points[i][0];
        lat += points[i][1];
      }
      return { lat: lat / points.length, lng: lng / points.length };
    }

    function nearestLng(lng, centerLng) {
      return lng + Math.round((centerLng - lng) / 360) * 360;
    }

    function featureArea(feature) {
      var g = feature.geometry;
      var minLng = 180, maxLng = -180, minLat = 90, maxLat = -90;
      function visit(coords) {
        if (typeof coords[0] === 'number') {
          minLng = Math.min(minLng, coords[0]);
          maxLng = Math.max(maxLng, coords[0]);
          minLat = Math.min(minLat, coords[1]);
          maxLat = Math.max(maxLat, coords[1]);
          return;
        }
        coords.forEach(visit);
      }
      if (g.type === 'Polygon') visit(g.coordinates);
      else if (g.type === 'MultiPolygon') g.coordinates.forEach(function(poly) { visit(poly); });
      return Math.max(0, (maxLng - minLng) * (maxLat - minLat));
    }

    function escapeHtml(text) {
      return String(text || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    function makeLabelIcon(name, tier, selected) {
      var cls = 'country-label-wrap tier-' + tier;
      if (selected) cls += ' selected-name';
      return L.divIcon({
        className: cls,
        html: '<span class="country-label-text">' + escapeHtml(name) + '</span>',
        iconSize: null
      });
    }

    function minTierForZoom(zoom) {
      if (zoom < 2.75) return 1;
      if (zoom < 3.75) return 2;
      return 3;
    }

    function setLabelsHidden(hidden) {
      if (!map) return;
      var el = map.getContainer();
      if (hidden) el.classList.add('labels-hidden');
      else el.classList.remove('labels-hidden');
    }

    function clearRegimeLabels() {
      if (!labelLayer) return;
      labelLayer.clearLayers();
      labelMarkers = [];
    }

    function rebuildRegimeLabels() {
      if (!map || !labelLayer) return;
      clearRegimeLabels();
      if (!storedTerritoryGeojson || !storedTerritoryGeojson.features) return;

      var seen = {};
      var items = [];
      var i, feature, props, id, area;
      for (i = 0; i < storedTerritoryGeojson.features.length; i++) {
        feature = storedTerritoryGeojson.features[i];
        props = feature.properties || {};
        id = props.id;
        if (!id || seen[id]) continue;
        seen[id] = true;
        area = featureArea(feature);
        items.push({
          id: id,
          name: props.name_zh || props.name_en || id,
          area: area,
          lat: featureCentroid(feature).lat,
          lng: featureCentroid(feature).lng
        });
      }
      items.sort(function(a, b) { return b.area - a.area; });

      for (i = 0; i < items.length; i++) {
        var rank = i;
        var tier = rank < items.length * 0.25 ? 1 : (rank < items.length * 0.6 ? 2 : 3);
        var item = items[i];
        var selected = highlightedStateId === item.id;
        var marker = L.marker([item.lat, item.lng], {
          icon: makeLabelIcon(item.name, tier, selected),
          interactive: false,
          keyboard: false
        });
        labelLayer.addLayer(marker);
        labelMarkers.push({
          id: item.id,
          name: item.name,
          tier: tier,
          lat: item.lat,
          lng: item.lng,
          marker: marker
        });
      }
      updateRegimeLabelDisplay();
    }

    function updateRegimeLabelDisplay() {
      if (!map || !labelMarkers.length) return;
      var zoom = map.getZoom();
      var minTier = minTierForZoom(zoom);
      var centerLng = map.getCenter().lng;
      var i, item, displayLng, selected;
      for (i = 0; i < labelMarkers.length; i++) {
        item = labelMarkers[i];
        selected = highlightedStateId === item.id;
        displayLng = nearestLng(item.lng, centerLng);
        item.marker.setLatLng([item.lat, displayLng]);
        var visible = item.tier <= minTier;
        item.marker.setOpacity(visible ? 1 : 0);
        var el = item.marker.getElement();
        if (el) {
          el.classList.toggle('selected-name', selected);
          el.classList.toggle('tier-1', item.tier === 1);
          el.classList.toggle('tier-2', item.tier === 2);
          el.classList.toggle('tier-3', item.tier === 3);
        }
      }
    }
    window.updateRegimeLabelDisplay = updateRegimeLabelDisplay;

    function safeLayerRedraw(layer) {
      if (layer && layer._map && layer.redraw) {
        try {
          layer.redraw();
        } catch (err) {
          console.warn('layer redraw skipped', err);
        }
      }
    }

    function syncLayersAfterIdle() {
      if (!map || !map._loaded) return;
      safeLayerRedraw(basemapLayer);
      safeLayerRedraw(territoryLayer);
      updateRegimeLabelDisplay();
      if (interactionLock || userDragging) return;
      if (highlightedStateId) {
        applyHighlightStyles();
      }
    }
    window.syncLayersAfterIdle = syncLayersAfterIdle;

    function forceMapRedraw() {
      if (!map || !map._loaded) return;
      safeLayerRedraw(basemapLayer);
      if (territoryLayer && territoryLayer._map) {
        safeLayerRedraw(territoryLayer);
        territoryLayer.eachLayer(function(layer) {
          safeLayerRedraw(layer);
        });
      }
    }
    window.forceMapRedraw = forceMapRedraw;

    var redrawFrame = null;
    function scheduleMapRedraw() {
      if (!map) return;
      if (redrawFrame) cancelAnimationFrame(redrawFrame);
      redrawFrame = requestAnimationFrame(function() {
        redrawFrame = null;
        forceMapRedraw();
        if (map && map._loaded && map.invalidateSize) {
          try {
            map.invalidateSize({ animate: false });
          } catch (err) {
            console.warn('invalidateSize skipped', err);
          }
        }
      });
    }
    window.scheduleMapRedraw = scheduleMapRedraw;

    function traceLog(msg) {
      if (bridge && bridge.notifyTrace) {
        bridge.notifyTrace(msg);
      }
    }

    window.getMapDebugState = function() {
      return JSON.stringify({
        storedYear: storedTerritorySnapshotYear,
        requestedYear: requestedSnapshotYear,
        yearLoadToken: yearLoadToken,
        loadingYear: loadingYear,
        featureCount: storedTerritoryGeojson ? storedTerritoryGeojson.features.length : 0,
        labelCount: labelMarkers.length,
        userDragging: userDragging,
        isMapMoving: isMapMoving
      });
    };

    window.latLngToMapPoint = function(lat, lng) {
      if (!map) return JSON.stringify({ x: 0, y: 0 });
      var displayLng = nearestLng(lng, map.getCenter().lng);
      var pt = map.latLngToContainerPoint(L.latLng(lat, displayLng));
      return JSON.stringify({ x: Math.round(pt.x), y: Math.round(pt.y) });
    };

    window.syncFromSidebar = function(stateId, syncGen) {
      syncGen = syncGen || 0;
      if (syncGen < lastSidebarSyncGen) return;
      lastSidebarSyncGen = syncGen;
      if (!stateId) {
        clearHighlight();
        return;
      }
      highlightedStateId = stateId;
      applyHighlightStyles();
    };

    function onViewSettled() {
      if (interactionLock) return;
      isMapMoving = false;
      if (suppressMoveEnd) return;
      if (clampLatitude()) return;
      if (wrapMapLongitude()) return;
      syncLayersAfterIdle();
      updateRegimeLabelDisplay();
      if (bridge && bridge.notifyViewSettled) {
        bridge.notifyViewSettled();
      }
    }

    function scheduleViewSettled() {
      if (interactionLock) return;
      if (viewSettleTimer) clearTimeout(viewSettleTimer);
      viewSettleTimer = setTimeout(function() {
        viewSettleTimer = null;
        onViewSettled();
      }, VIEW_SETTLE_MS);
    }

    function clearHighlight() {
      highlightedStateId = null;
      interactionLock = false;
      if (viewSettleTimer) {
        clearTimeout(viewSettleTimer);
        viewSettleTimer = null;
      }
      applyHighlightStyles();
    }

    window.applyMapSelection = function(stateId, syncGen) {
      window.syncFromSidebar(stateId, syncGen);
    };

    function setLayerNormal(layer) {
      if (!layer || !layer.feature) return;
      layer.setStyle(territoryStyle(layer.feature));
    }

    function setLayerHighlighted(layer) {
      if (!layer || !layer.feature) return;
      layer.setStyle(highlightStyle(layer.feature));
    }

    function applyHighlightStyles() {
      if (prevHighlightedStateId && layersByStateId[prevHighlightedStateId]) {
        layersByStateId[prevHighlightedStateId].forEach(setLayerNormal);
      }
      if (highlightedStateId && layersByStateId[highlightedStateId]) {
        layersByStateId[highlightedStateId].forEach(setLayerHighlighted);
      }
      prevHighlightedStateId = highlightedStateId;
      updateRegimeLabelDisplay();
    }

    function resolveStateAtClick(latlng) {
      if (!territoryLayer || !map) return null;
      var bestId = null;
      var bestArea = Infinity;
      var seen = {};
      var layerPoint = map.latLngToLayerPoint(latlng);
      territoryLayer.eachLayer(function(layer) {
        var props = layer.feature && layer.feature.properties;
        if (!props || !props.id) return;
        var id = props.id;
        if (seen[id]) return;
        if (layer.getBounds && !layer.getBounds().contains(latlng)) return;
        if (layer._containsPoint && !layer._containsPoint(layerPoint)) return;
        seen[id] = true;
        var b = layer.getBounds();
        var area = (b.getNorth() - b.getSouth()) * (b.getEast() - b.getWest());
        if (area < bestArea) {
          bestArea = area;
          bestId = id;
        }
      });
      return bestId;
    }
    window.resolveStateAtClick = resolveStateAtClick;

    function onMapClick(e) {
      if (viewSettleTimer) {
        clearTimeout(viewSettleTimer);
        viewSettleTimer = null;
      }
      isMapMoving = false;
      if (!bridge || !bridge.onMapClicked) return;
      safeLayerRedraw(territoryLayer);
      var year = storedTerritorySnapshotYear != null ? storedTerritorySnapshotYear : -1;
      var hint = resolveStateAtClick(e.latlng) || '';
      bridge.onMapClicked(e.latlng.lat, e.latlng.lng, year, hint);
    }

    window.highlightState = function(stateId) {
      window.syncFromSidebar(stateId, lastSidebarSyncGen);
    };

    function installTerritory(geojson) {
      var year = geojson ? geojson.snapshot_year : null;
      if (year != null && year !== requestedSnapshotYear) {
        scheduleYearLoad();
        return;
      }
      storedTerritorySnapshotYear = year;
      storedTerritoryGeojson = geojson && geojson.features && geojson.features.length
        ? geojson
        : null;
      clearHighlight();
      layersByStateId = {};
      territoryLayer.clearLayers();
      if (storedTerritoryGeojson) {
        territoryLayer.addData(territoryDataForInstall(storedTerritoryGeojson));
      }
      rebuildRegimeLabels();
      scheduleMapRedraw();
      if (storedTerritorySnapshotYear != null) {
        notifyYearReady(
          storedTerritorySnapshotYear,
          storedTerritoryGeojson ? storedTerritoryGeojson.features.length : 0
        );
      }
    }

    function installTerritoryFast(geojson) {
      var year = geojson ? geojson.snapshot_year : null;
      if (year != null && year !== requestedSnapshotYear) return;
      storedTerritorySnapshotYear = year;
      storedTerritoryGeojson = geojson && geojson.features && geojson.features.length
        ? geojson
        : null;
      layersByStateId = {};
      territoryLayer.clearLayers();
      if (storedTerritoryGeojson) {
        territoryLayer.addData(territoryDataForInstall(storedTerritoryGeojson));
      }
      rebuildRegimeLabels();
      forceMapRedraw();
      if (storedTerritorySnapshotYear != null) {
        notifyYearReady(
          storedTerritorySnapshotYear,
          storedTerritoryGeojson ? storedTerritoryGeojson.features.length : 0
        );
      }
    }

    function installYearDataFast(geojson) {
      if (!map) initMap();
      var center = map.getCenter();
      var zoom = map.getZoom();
      installTerritoryFast(geojson);
      map.setView(center, zoom, { animate: false });
      requestAnimationFrame(function() {
        forceMapRedraw();
      });
    }

    function clampLatitude() {
      if (!map) return false;
      var c = map.getCenter();
      var lat = Math.max(LAT_MIN, Math.min(LAT_MAX, c.lat));
      if (Math.abs(lat - c.lat) < 0.01) return false;
      suppressMoveEnd = true;
      map.panTo([lat, c.lng], { animate: false });
      setTimeout(function() {
        suppressMoveEnd = false;
        updateRegimeLabelDisplay();
        scheduleViewSettled();
      }, 0);
      return true;
    }

    function wrapMapLongitude() {
      if (!map || suppressMoveEnd || interactionLock) return false;
      var c = map.getCenter();
      var lng = c.lng;
      var lat = c.lat;
      var wrapped = lng;
      while (wrapped < WRAP_CENTER_MIN) wrapped += 360;
      while (wrapped > WRAP_CENTER_MAX) wrapped -= 360;
      if (Math.abs(wrapped - lng) < 0.01) return false;
      suppressMoveEnd = true;
      map.setView([lat, wrapped], map.getZoom(), { animate: false });
      setTimeout(function() {
        suppressMoveEnd = false;
        updateRegimeLabelDisplay();
        scheduleViewSettled();
      }, 0);
      return true;
    }

    function loadBasemap() {
      reportLoadProgress(8, '加载陆地底图…');
      return fetch('data/basemap/world_land.geojson').then(function(r) {
        if (!r.ok) throw new Error('world_land HTTP ' + r.status);
        return r.json();
      }).then(function(land) {
        if (!map) return;
        basemapLayer.clearLayers();
        basemapLayer.addData(withLngOffsets(land, WRAP_OFFSETS));
        reportLoadProgress(22, '底图已就绪');
        if (bridge && bridge.notifyBasemapReady) {
          bridge.notifyBasemapReady();
        } else {
          pendingBasemapReady = true;
        }
      });
    }

    function fetchYear(year) {
      if (yearGeoCache[year]) {
        return Promise.resolve(yearGeoCache[year]);
      }
      return fetch('data/maps/' + year + '.json').then(function(r) {
        if (!r.ok) throw new Error('year ' + year + ' HTTP ' + r.status);
        return r.json();
      }).then(function(geojson) {
        yearGeoCache[year] = geojson;
        return geojson;
      });
    }

    function territoryDataForInstall(geojson) {
      var y = geojson.snapshot_year;
      if (!offsetGeoCache[y]) {
        offsetGeoCache[y] = withLngOffsets(assignRegimeColors(geojson), WRAP_OFFSETS);
      }
      return offsetGeoCache[y];
    }

    function prefetchNeighbors(year) {
      var idx = snapshotYears.indexOf(year);
      if (idx < 0) return;
      var d, ni, y;
      for (d = -1; d <= 1; d += 2) {
        ni = idx + d;
        if (ni >= 0 && ni < snapshotYears.length) {
          y = snapshotYears[ni];
          if (!yearGeoCache[y]) {
            fetchYear(y).catch(function() {});
          }
        }
      }
    }

    window.setSnapshotYears = function(years) {
      snapshotYears = years || [];
    };

    window.setSnapshotYearFromPayload = function(year, geojson, force) {
      yearGeoCache[year] = geojson;
      delete offsetGeoCache[year];
      requestedSnapshotYear = year;
      forceYearInstall = !!force;
      scheduleYearLoad();
    };

    window.setSnapshotYear = function(year, force) {
      if (!force && requestedSnapshotYear === year && year === storedTerritorySnapshotYear) {
        applyCachedYear(year);
        return 0;
      }
      requestedSnapshotYear = year;
      forceYearInstall = !!force;
      scheduleYearLoad();
      return 0;
    };

    window.setSnapshotYearFast = function(year) {
      if (year == null) return 0;
      if (year === storedTerritorySnapshotYear) return 0;
      requestedSnapshotYear = year;
      yearLoadToken += 1;
      var token = yearLoadToken;
      if (yearGeoCache[year]) {
        if (token !== yearLoadToken || requestedSnapshotYear !== year) return 0;
        installYearDataFast(yearGeoCache[year]);
        return 0;
      }
      fetchYear(year).then(function(geojson) {
        if (token !== yearLoadToken || requestedSnapshotYear !== year) {
          if (requestedSnapshotYear != null && requestedSnapshotYear !== year) {
            setSnapshotYearFast(requestedSnapshotYear);
          }
          return;
        }
        installYearDataFast(geojson);
      }).catch(function(err) {
        console.error('setSnapshotYearFast failed', err);
      });
      return 0;
    };

    window.warmYearCache = function() {
      var queue = snapshotYears.slice();
      var batch = 4;
      function pump() {
        var n = batch;
        while (n-- > 0 && queue.length) {
          (function(y) {
            if (!yearGeoCache[y]) fetchYear(y).catch(function() {});
          })(queue.shift());
        }
        if (queue.length) setTimeout(pump, 40);
      }
      pump();
    };

    function notifyYearReady(year, count) {
      if (bridge && bridge.notifyYearApplied) {
        bridge.notifyYearApplied(year, count);
      } else {
        pendingYearApplied = { year: year, count: count };
      }
    }

    function applyCachedYear(year) {
      if (year !== storedTerritorySnapshotYear || !storedTerritoryGeojson) {
        return false;
      }
      applyHighlightStyles();
      scheduleMapRedraw();
      notifyYearReady(year, storedTerritoryGeojson.features.length);
      return true;
    }

    function installYearData(geojson) {
      if (!map) initMap();
      var center = map.getCenter();
      var zoom = map.getZoom();
      installTerritory(geojson);
      if (!initialViewSet) {
        map.setView(DEFAULT_CENTER, DEFAULT_ZOOM, { animate: false });
        initialViewSet = true;
      } else {
        map.setView(center, zoom, { animate: false });
      }
      scheduleMapRedraw();
      if (!snapshotsReady) {
        snapshotsReady = true;
        if (bridge && bridge.notifySnapshotsReady) {
          bridge.notifySnapshotsReady(1);
        }
      }
      prefetchNeighbors(geojson.snapshot_year);
    }

    function scheduleYearLoad() {
      var year = requestedSnapshotYear;
      var force = forceYearInstall;
      forceYearInstall = false;
      if (year == null) return;
      if (!force && applyCachedYear(year)) {
        if (requestedSnapshotYear !== year) scheduleYearLoad();
        return;
      }
      if (loadingYear && activeLoadYear === year && !force) return;
      if (yearGeoCache[year]) {
        yearLoadToken += 1;
        var token = yearLoadToken;
        if (token !== yearLoadToken || requestedSnapshotYear !== year) return;
        installYearData(yearGeoCache[year]);
        if (requestedSnapshotYear !== year) scheduleYearLoad();
        return;
      }
      yearLoadToken += 1;
      applyYear(year, yearLoadToken);
    }

    function applyYear(year, token) {
      if (!map) initMap();
      if (token !== yearLoadToken) return Promise.resolve(-1);
      if (year === storedTerritorySnapshotYear && storedTerritoryGeojson) {
        applyCachedYear(year);
        if (requestedSnapshotYear !== year) scheduleYearLoad();
        return Promise.resolve(storedTerritoryGeojson.features.length);
      }
      loadingYear = true;
      activeLoadYear = year;
      return fetchYear(year).then(function(geojson) {
        loadingYear = false;
        activeLoadYear = null;
        if (token !== yearLoadToken || requestedSnapshotYear !== year) {
          if (requestedSnapshotYear != null) {
            scheduleYearLoad();
          }
          return -1;
        }
        installYearData(geojson);
        return geojson.features ? geojson.features.length : 0;
      }).catch(function(err) {
        loadingYear = false;
        activeLoadYear = null;
        console.error('load year failed', err);
        if (requestedSnapshotYear != null && requestedSnapshotYear !== year) {
          scheduleYearLoad();
        }
        if (bridge && bridge.notifySnapshotsReady) {
          bridge.notifySnapshotsReady(0);
        }
        return -1;
      });
    }

    function initMap() {
      if (map) return;

      map = L.map('map', {
        center: DEFAULT_CENTER,
        zoom: DEFAULT_ZOOM,
        minZoom: 2,
        maxZoom: 6,
        zoomSnap: 0.25,
        zoomDelta: 0.25,
        wheelPxPerZoomLevel: 80,
        zoomAnimation: false,
        fadeAnimation: false,
        markerZoomAnimation: false,
        worldCopyJump: false,
        inertia: false,
        doubleClickZoom: false,
        zoomControl: true,
        attributionControl: true,
        preferCanvas: true
      });

      mapRenderer = L.canvas({ padding: 0.5 });
      territoryRenderer = L.canvas({ padding: 0.5 });

      map.createPane('territoryPane');
      map.getPane('territoryPane').style.zIndex = '450';
      map.createPane('labelsPane');
      map.getPane('labelsPane').style.zIndex = '500';

      map.attributionControl.setPrefix('');
      map.attributionControl.addAttribution('本地底图 · Natural Earth');

      var vectorOpts = {
        renderer: mapRenderer,
        updateWhenIdle: false,
        updateWhenZooming: false
      };

      basemapLayer = L.geoJSON(null, Object.assign({
        smoothFactor: 1.5,
        interactive: false,
        style: {
          fillColor: '#3d4f66',
          fillOpacity: 1,
          color: '#2a3545',
          weight: 0.5
        }
      }, vectorOpts)).addTo(map);

      territoryLayer = L.geoJSON(null, Object.assign({
        pane: 'territoryPane',
        renderer: territoryRenderer,
        smoothFactor: 0,
        style: territoryStyle,
        onEachFeature: function(feature, layer) {
          var id = feature.properties && feature.properties.id;
          if (id) {
            if (!layersByStateId[id]) layersByStateId[id] = [];
            layersByStateId[id].push(layer);
          }
        }
      }, vectorOpts)).addTo(map);

      labelLayer = L.layerGroup({ pane: 'labelsPane' }).addTo(map);

      map.on('dragstart', function() { userDragging = true; });
      map.on('dragend', function() {
        userDragging = false;
        updateRegimeLabelDisplay();
      });
      map.on('click', onMapClick);
      map.on('movestart', function() { isMapMoving = true; });
      map.on('zoomstart', function() { isMapMoving = true; });
      map.on('moveend', function() {
        isMapMoving = false;
        updateRegimeLabelDisplay();
        scheduleViewSettled();
      });
      map.on('zoomend', function() {
        isMapMoving = false;
        updateRegimeLabelDisplay();
        scheduleViewSettled();
      });

      notifyMapEngineReadyNow();
      loadBasemap().then(function() {
        reportLoadProgress(25, '载入版图数据…');
      }).catch(function(err) {
        console.error('load basemap failed', err);
        reportLoadProgress(0, '陆地底图加载失败');
        if (bridge && bridge.notifyBasemapFailed) {
          bridge.notifyBasemapFailed(1);
        }
      });
    }

    initMap();
  </script>
</body>
</html>
"""

# --- map_widget ---


class MapBridge(QObject):
    mapClicked = Signal(float, float, int, str)
    viewSettled = Signal()
    snapshotsReady = Signal(bool)
    yearApplied = Signal(int, int)
    loadProgress = Signal(int, str)
    basemapReady = Signal()
    mapEngineReady = Signal()

    traceLogged = Signal(str)

    @Slot(str)
    def notifyTrace(self, msg: str) -> None:
        self.traceLogged.emit(msg)

    @Slot(float, float, int, str)
    def onMapClicked(
        self, lat: float, lng: float, map_year: int, state_id_hint: str
    ) -> None:
        self.mapClicked.emit(lat, lng, map_year, state_id_hint or "")

    @Slot()
    def notifyViewSettled(self) -> None:
        self.viewSettled.emit()

    @Slot(int)
    def notifySnapshotsReady(self, ok: int) -> None:
        self.snapshotsReady.emit(bool(ok))

    @Slot(int, int)
    def notifyYearApplied(self, year: int, layer_count: int) -> None:
        self.yearApplied.emit(year, layer_count)

    @Slot(int, str)
    def notifyLoadProgress(self, percent: int, message: str) -> None:
        self.loadProgress.emit(percent, message)

    @Slot()
    def notifyBasemapReady(self) -> None:
        self.basemapReady.emit()

    @Slot()
    def notifyMapEngineReady(self) -> None:
        self.mapEngineReady.emit()

    @Slot(int)
    def notifyBasemapFailed(self, _code: int) -> None:
        self.loadProgress.emit(0, "陆地底图加载失败（政权边界仍可显示）")


class MapWidget(QWebEngineView):
    mapClicked = Signal(float, float, int, str)
    viewSettled = Signal()
    snapshotsReady = Signal(bool)
    yearApplied = Signal(int, int)
    loadProgress = Signal(int, str)
    basemapReady = Signal()
    mapEngineReady = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._bridge = MapBridge()
        self._bridge.mapClicked.connect(self.mapClicked.emit)
        self._bridge.viewSettled.connect(self.viewSettled.emit)
        self._bridge.snapshotsReady.connect(self.snapshotsReady.emit)
        self._bridge.yearApplied.connect(self.yearApplied.emit)
        self._bridge.loadProgress.connect(self.loadProgress.emit)
        self._bridge.basemapReady.connect(self.basemapReady.emit)
        self._bridge.traceLogged.connect(
            lambda msg: trace("JS", msg)
        )

        channel = QWebChannel(self)
        channel.registerObject("bridge", self._bridge)
        self.page().setWebChannel(channel)

        settings = self.page().settings()
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False
        )
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, False)
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, False
        )

        base_url = QUrl.fromLocalFile(str(ROOT.resolve()) + "/")
        self.setHtml(MAP_HTML, base_url)
        self._engine_ready = False
        self._map_ready = False
        self._pending_year: int | None = None
        self._pending_payload: str | None = None
        self._pending_force = False
        self._last_requested_year: int | None = None
        self._pending_highlight: tuple[str | None, int, str, float, float] | None = None
        self._bridge.mapEngineReady.connect(self._on_engine_ready)

        self._coalesce_timer = QTimer(self)
        self._coalesce_timer.setSingleShot(True)
        self._coalesce_timer.setInterval(0)
        self._coalesce_timer.timeout.connect(self._flush_coalesced_year)
        self._coalesce_year: int | None = None

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(150)
        self._resize_timer.timeout.connect(self._on_resize_settled)

    def query_map_state(self, callback) -> None:
        if not self._engine_ready:
            callback("{}")
            return
        self.page().runJavaScript("getMapDebugState()", callback)

    def _on_engine_ready(self) -> None:
        self._engine_ready = True
        if self._pending_year is not None:
            year = self._pending_year
            payload = self._pending_payload
            force = self._pending_force
            self._pending_year = None
            self._pending_payload = None
            self._pending_force = False
            self.set_snapshot_year(year, payload, force=force)
        if self._pending_highlight is not None:
            state_id, sync_gen, name_zh, lat, lng = self._pending_highlight
            self._pending_highlight = None
            self.set_highlight(state_id, sync_gen, name_zh, lat, lng)
        self.mapEngineReady.emit()

    def mark_map_ready(self) -> None:
        self._map_ready = True

    def warm_year_cache(self) -> None:
        if not self._engine_ready:
            return
        self.page().runJavaScript("warmYearCache()")

    def set_snapshot_year_fast(self, year: int) -> None:
        """拖动时间轴：仅切换 JS 内存缓存中的版图，不传大 JSON。"""
        self._last_requested_year = year
        if not self._engine_ready:
            self._pending_year = year
            self._pending_payload = None
            self._pending_force = False
            return
        self._coalesce_year = year
        self._coalesce_timer.start()

    def _flush_coalesced_year(self) -> None:
        if self._coalesce_year is None or not self._engine_ready:
            return
        year = self._coalesce_year
        self._coalesce_year = None
        self.page().runJavaScript(f"setSnapshotYearFast({year})")

    def set_snapshot_year(
        self, year: int, payload: str | None = None, *, force: bool = False
    ) -> None:
        """切换年代；有 payload 时注入 JSON，否则走 JS 缓存/本地文件。"""
        self._coalesce_timer.stop()
        self._coalesce_year = None
        self._last_requested_year = year
        if not self._engine_ready:
            self._pending_year = year
            self._pending_payload = payload
            self._pending_force = force
            return
        self._send_snapshot_year(year, payload, force)

    def _send_snapshot_year(
        self, year: int, payload: str | None, force: bool
    ) -> None:
        force_js = "true" if force else "false"
        if payload:
            self.page().runJavaScript(
                "setSnapshotYearFromPayload("
                f"{year}, JSON.parse({json.dumps(payload)}), {force_js})"
            )
        else:
            self.page().runJavaScript(f"setSnapshotYear({year}, {force_js})")

    def retry_snapshot_year_if_needed(self, applied_year: int) -> None:
        target = self._last_requested_year
        trace(
            "MAP",
            f"retry_snapshot_year applied={applied_year} target={target}",
        )
        if target is not None and applied_year != target:
            self.page().runJavaScript(f"setSnapshotYear({target})")

    def reload_snapshots(self, on_done=None) -> None:
        self._engine_ready = False
        self._map_ready = False

        def _reload(_result) -> None:
            if on_done:
                on_done()

        self.page().runJavaScript("location.reload()", _reload)

    def set_highlight(
        self,
        state_id: str | None,
        sync_gen: int,
        name_zh: str = "",
        lat: float = 0.0,
        lng: float = 0.0,
    ) -> None:
        """更新地图高亮与选中政权名称标记。"""
        if not self._engine_ready:
            self._pending_highlight = (state_id, sync_gen, name_zh, lat, lng)
            return
        if not state_id:
            self.page().runJavaScript(f"syncFromSidebar(null, {sync_gen})")
            return
        sid = json.dumps(state_id, ensure_ascii=False)
        name = json.dumps(name_zh or "", ensure_ascii=False)
        self.page().runJavaScript(
            f"syncFromSidebar({sid}, {sync_gen}, {name}, {lat}, {lng})"
        )

    def lat_lng_to_map_point(self, lat: float, lng: float, callback) -> None:
        if not self._engine_ready:
            return
        self.page().runJavaScript(
            f"latLngToMapPoint({lat}, {lng})",
            callback,
        )

    def refresh_layers(self) -> None:
        if not self._engine_ready:
            return
        trace("MAP", "refresh_layers()")
        self.page().runJavaScript("scheduleMapRedraw()")

    def sync_from_sidebar(
        self,
        state_id: str | None,
        props: dict | None,
        lat: float,
        lng: float,
        sync_gen: int,
    ) -> None:
        """兼容旧接口。"""
        self.set_highlight(state_id if props else None, sync_gen, "", lat, lng)

    def _on_resize_settled(self) -> None:
        if not self._engine_ready:
            return
        self.page().runJavaScript(
            "if (map) { map.invalidateSize({ animate: false }); syncLayersAfterIdle(); }"
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._engine_ready:
            self._resize_timer.start()

__all__ = ["MAP_HTML", "MapBridge", "MapWidget", "feature_centroid", "feature_contains_point", "find_state_at"]
