"""政权地图配色：每个政权独占一色，相邻政权尽量使用差异明显的颜色。"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

_GOLDEN_ANGLE = 137.508


def _bbox(geometry: dict[str, Any]) -> tuple[float, float, float, float]:
    min_lng, min_lat = 180.0, 90.0
    max_lng, max_lat = -180.0, -90.0

    def visit(coords: Any) -> None:
        nonlocal min_lng, min_lat, max_lng, max_lat
        if isinstance(coords[0], (int, float)):
            lng, lat = float(coords[0]), float(coords[1])
            min_lng = min(min_lng, lng)
            max_lng = max(max_lng, lng)
            min_lat = min(min_lat, lat)
            max_lat = max(max_lat, lat)
            return
        for part in coords:
            visit(part)

    visit(geometry["coordinates"])
    return min_lng, min_lat, max_lng, max_lat


def _centroid(geometry: dict[str, Any]) -> tuple[float, float]:
    min_lng, min_lat, max_lng, max_lat = _bbox(geometry)
    return (min_lng + max_lng) / 2.0, (min_lat + max_lat) / 2.0


def _ring_edges(
    coords: list[list[float]],
    *,
    quantize: int = 3,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    edges: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for index in range(len(coords) - 1):
        p1 = (round(coords[index][0], quantize), round(coords[index][1], quantize))
        p2 = (round(coords[index + 1][0], quantize), round(coords[index + 1][1], quantize))
        edges.append((p1, p2) if p1 <= p2 else (p2, p1))
    return edges


def _geometry_edges(geometry: dict[str, Any]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return []
    if geom_type == "Polygon":
        return _ring_edges(coords[0])
    if geom_type == "MultiPolygon":
        edges: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for polygon in coords:
            edges.extend(_ring_edges(polygon[0]))
        return edges
    return []


def _grid_cells(geometry: dict[str, Any], cell_size: float = 0.25) -> set[tuple[int, int]]:
    min_lng, min_lat, max_lng, max_lat = _bbox(geometry)
    cells: set[tuple[int, int]] = set()
    x = int(min_lng / cell_size)
    while x * cell_size <= max_lng + 1e-9:
        y = int(min_lat / cell_size)
        while y * cell_size <= max_lat + 1e-9:
            cells.add((x, y))
            y += 1
        x += 1
    return cells


def _bboxes_touch(
    box_a: tuple[float, float, float, float],
    box_b: tuple[float, float, float, float],
    *,
    epsilon: float = 0.01,
) -> bool:
    return not (
        box_a[2] + epsilon < box_b[0]
        or box_b[2] + epsilon < box_a[0]
        or box_a[3] + epsilon < box_b[1]
        or box_b[3] + epsilon < box_a[1]
    )


def build_regime_adjacency(features: list[dict[str, Any]]) -> dict[str, set[str]]:
    """根据共享边界、相邻网格与地理邻近推断政权邻接关系。"""
    edge_names: dict[tuple[tuple[float, float], tuple[float, float]], set[str]] = defaultdict(set)
    name_cells: dict[str, set[tuple[int, int]]] = defaultdict(set)
    name_bbox: dict[str, tuple[float, float, float, float]] = {}
    centroids: dict[str, tuple[float, float]] = {}

    for feature in features:
        props = feature.get("properties") or {}
        name = (props.get("name_en") or "").strip()
        if not name:
            continue
        geometry = feature.get("geometry") or {}
        name_cells[name].update(_grid_cells(geometry))
        name_bbox[name] = _bbox(geometry)
        centroids[name] = _centroid(geometry)
        for edge in _geometry_edges(geometry):
            edge_names[edge].add(name)

    adjacency: dict[str, set[str]] = defaultdict(set)
    for names in edge_names.values():
        if len(names) < 2:
            continue
        for left in names:
            for right in names:
                if left != right:
                    adjacency[left].add(right)

    names = list(name_cells)
    for left_index, left in enumerate(names):
        left_box = name_bbox[left]
        left_cells = name_cells[left]
        for right in names[left_index + 1 :]:
            if right in adjacency[left]:
                continue
            if not _bboxes_touch(left_box, name_bbox[right]):
                continue
            if left_cells & name_cells[right]:
                adjacency[left].add(right)
                adjacency[right].add(left)

    # 地理上相近但边界数据未对齐的政权，也视为需要拉开色差
    for left_index, left in enumerate(names):
        left_lng, left_lat = centroids[left]
        for right in names[left_index + 1 :]:
            if right in adjacency[left]:
                continue
            right_lng, right_lat = centroids[right]
            if not _bboxes_touch(name_bbox[left], name_bbox[right]):
                continue
            lat_scale = math.cos(math.radians((left_lat + right_lat) / 2.0))
            distance = math.hypot(
                (right_lng - left_lng) * max(lat_scale, 0.2),
                right_lat - left_lat,
            )
            if distance <= 18.0:
                adjacency[left].add(right)
                adjacency[right].add(left)

    return adjacency


def _generate_unique_palette(count: int) -> list[tuple[float, int, int]]:
    """生成 count 个互不相同的 HSL 颜色。"""
    colors: list[tuple[float, int, int]] = []
    seen: set[tuple[float, int, int]] = set()
    index = 0
    while len(colors) < count:
        hue = (index * _GOLDEN_ANGLE) % 360.0
        band = index // 13
        saturation = 52 + (band % 4) * 10 + (index % 3) * 4
        lightness = 38 + ((band // 4) % 5) * 9 + (index % 2) * 7
        saturation = min(84, max(48, saturation))
        lightness = min(70, max(36, lightness))
        candidate = (hue, saturation, lightness)
        if candidate not in seen:
            seen.add(candidate)
            colors.append(candidate)
        index += 1
    return colors


def _hsl_to_rgb(hue: float, saturation: int, lightness: int) -> tuple[float, float, float]:
    hue_norm = (hue % 360.0) / 360.0
    sat = saturation / 100.0
    light = lightness / 100.0

    if sat == 0:
        return light, light, light

    def hue_to_rgb(p: float, q: float, t: float) -> float:
        if t < 0:
            t += 1
        if t > 1:
            t -= 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    q = light * (1 + sat) if light < 0.5 else light + sat - light * sat
    p = 2 * light - q
    red = hue_to_rgb(p, q, hue_norm + 1 / 3)
    green = hue_to_rgb(p, q, hue_norm)
    blue = hue_to_rgb(p, q, hue_norm - 1 / 3)
    return red, green, blue


def _color_distance(
    left: tuple[float, int, int],
    right: tuple[float, int, int],
) -> float:
    left_rgb = _hsl_to_rgb(*left)
    right_rgb = _hsl_to_rgb(*right)
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left_rgb, right_rgb)))


def _score_candidate(
    candidate: tuple[float, int, int],
    neighbor_colors: list[tuple[float, int, int]],
    assigned_colors: list[tuple[float, int, int]],
) -> float:
    neighbor_score = (
        min(_color_distance(candidate, color) for color in neighbor_colors)
        if neighbor_colors
        else 999.0
    )
    global_score = (
        min(_color_distance(candidate, color) for color in assigned_colors)
        if assigned_colors
        else 999.0
    )
    if neighbor_colors:
        return neighbor_score * 4.0 + global_score
    return global_score


def _improve_adjacent_contrast(
    assignment: dict[str, tuple[float, int, int]],
    adjacency: dict[str, set[str]],
) -> None:
    """交换非冲突政权颜色，提升相邻政权色差。"""
    names = list(assignment.keys())
    for _ in range(12):
        improved = False
        for left in names:
            for right in adjacency.get(left, ()):
                if left >= right:
                    continue
                if _color_distance(assignment[left], assignment[right]) >= 0.28:
                    continue
                for donor in names:
                    if donor in (left, right):
                        continue
                    if donor in adjacency.get(left, ()):
                        continue
                    if right in adjacency.get(donor, ()):
                        continue
                    donor_color = assignment[donor]
                    left_color = assignment[left]
                    new_left_right = _color_distance(donor_color, assignment[right])
                    old_left_right = _color_distance(left_color, assignment[right])
                    if new_left_right <= old_left_right:
                        continue
                    left_neighbor_dists = [
                        _color_distance(donor_color, assignment[neighbor])
                        for neighbor in adjacency.get(left, ())
                        if neighbor != right
                    ]
                    old_left_neighbor_dists = [
                        _color_distance(left_color, assignment[neighbor])
                        for neighbor in adjacency.get(left, ())
                        if neighbor != right
                    ]
                    if left_neighbor_dists and min(left_neighbor_dists) < min(old_left_neighbor_dists) * 0.85:
                        continue
                    donor_neighbor_dists = [
                        _color_distance(left_color, assignment[neighbor])
                        for neighbor in adjacency.get(donor, ())
                    ]
                    old_donor_neighbor_dists = [
                        _color_distance(donor_color, assignment[neighbor])
                        for neighbor in adjacency.get(donor, ())
                    ]
                    if donor_neighbor_dists and min(donor_neighbor_dists) < min(old_donor_neighbor_dists) * 0.85:
                        continue
                    assignment[left], assignment[donor] = donor_color, left_color
                    improved = True
                    break
                if improved:
                    break
            if improved:
                break
        if not improved:
            break


def _hsl_to_css(hue: float, saturation: int, lightness: int) -> str:
    return f"hsl({hue:.0f}, {saturation}%, {lightness}%)"


def assign_regime_color_map(
    adjacency: dict[str, set[str]],
    regime_names: set[str] | list[str],
) -> dict[str, str]:
    """为每个政权分配唯一的 CSS HSL 颜色。"""
    names = sorted(regime_names)
    palette = _generate_unique_palette(len(names))
    order = sorted(names, key=lambda name: (-len(adjacency.get(name, ())), name))
    assignment: dict[str, tuple[float, int, int]] = {}
    used_indices: set[int] = set()

    for name in order:
        neighbor_colors = [
            assignment[neighbor]
            for neighbor in adjacency.get(name, ())
            if neighbor in assignment
        ]
        assigned_colors = list(assignment.values())

        best_index = -1
        best_score = -1.0
        for index, candidate in enumerate(palette):
            if index in used_indices:
                continue
            score = _score_candidate(candidate, neighbor_colors, assigned_colors)
            if score > best_score:
                best_score = score
                best_index = index

        if best_index < 0:
            best_index = next(i for i in range(len(palette)) if i not in used_indices)

        used_indices.add(best_index)
        assignment[name] = palette[best_index]

    _improve_adjacent_contrast(assignment, adjacency)

    return {
        name: _hsl_to_css(hue, saturation, lightness)
        for name, (hue, saturation, lightness) in assignment.items()
    }


def assign_regime_colors(features: list[dict[str, Any]]) -> dict[str, str]:
    """根据 feature 列表计算政权配色。"""
    regime_names = {
        (feature.get("properties") or {}).get("name_en", "").strip()
        for feature in features
    }
    regime_names.discard("")
    adjacency = build_regime_adjacency(features)
    return assign_regime_color_map(adjacency, regime_names)
