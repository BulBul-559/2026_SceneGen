from __future__ import annotations

import json
import math
import random
import time
from collections import Counter
from dataclasses import dataclass
from itertools import permutations
from pathlib import Path
from typing import Any

from .front3d import FRONT3D_TO_SCENEGEN, Front3DIndex, matrix_multiply, transformed_bbox
from .geometry import load_obj_mesh
from .models import Asset, Front3DBaseScene, PlacedAsset, Vec3
from .paths import find_project_root, portable_path


@dataclass(frozen=True)
class ProceduralRoom:
    room_id: str
    room_type: str
    x0: float
    y0: float
    x1: float
    y1: float
    height: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def length(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return self.width * self.length


@dataclass(frozen=True)
class ProceduralAssetEntry:
    model_id: str
    asset: Asset
    payload: dict[str, Any]
    semantic: dict[str, Any]


@dataclass(frozen=True)
class ProceduralSceneBuild:
    scene_id: str
    base_scene: Front3DBaseScene
    rooms: list[ProceduralRoom]
    placements: list[PlacedAsset]
    skipped_objects: list[dict[str, object]]
    generation_report: dict[str, object]


@dataclass(frozen=True)
class LayoutRegion:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def length(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return self.width * self.length


def rounded(value: float) -> float:
    return round(float(value), 6)


def rooms_footprint_metrics(rooms: list[ProceduralRoom]) -> dict[str, object]:
    if not rooms:
        return {
            "room_area_m2": 0.0,
            "bbox_area_m2": 0.0,
            "bbox_xy": [0.0, 0.0, 0.0, 0.0],
            "fill_ratio": 0.0,
            "concavity_area_m2": 0.0,
        }
    min_x = min(room.x0 for room in rooms)
    min_y = min(room.y0 for room in rooms)
    max_x = max(room.x1 for room in rooms)
    max_y = max(room.y1 for room in rooms)
    room_area = sum(room.area for room in rooms)
    bbox_area = max(0.0, max_x - min_x) * max(0.0, max_y - min_y)
    return {
        "room_area_m2": rounded(room_area),
        "bbox_area_m2": rounded(bbox_area),
        "bbox_xy": [rounded(min_x), rounded(min_y), rounded(max_x), rounded(max_y)],
        "fill_ratio": rounded(room_area / bbox_area) if bbox_area > 0 else 0.0,
        "concavity_area_m2": rounded(max(0.0, bbox_area - room_area)),
    }


def numeric_summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"min": 0.0, "max": 0.0, "mean": 0.0}
    return {
        "min": rounded(min(values)),
        "max": rounded(max(values)),
        "mean": rounded(sum(values) / len(values)),
    }


def _bump_counter(container: dict[str, object], key: str, amount: int = 1) -> None:
    container[key] = int(container.get(key, 0) or 0) + amount


def _bump_nested_counter(container: dict[str, object], outer_key: str, inner_key: str, amount: int = 1) -> None:
    inner = container.get(outer_key)
    if not isinstance(inner, dict):
        inner = {}
        container[outer_key] = inner
    _bump_counter(inner, inner_key, amount)


def _bump_stats_counter(stats: dict[str, object], name: str, key: str, amount: int = 1) -> None:
    counter = stats.get(name)
    if isinstance(counter, dict):
        _bump_counter(counter, key, amount)


def _bump_stats_nested_counter(
    stats: dict[str, object],
    name: str,
    outer_key: str,
    inner_key: str,
    amount: int = 1,
) -> None:
    counter = stats.get(name)
    if isinstance(counter, dict):
        _bump_nested_counter(counter, outer_key, inner_key, amount)


def _merge_counter(target: Counter[str], values: object) -> None:
    if not isinstance(values, dict):
        return
    for key, count in values.items():
        target[str(key)] += int(count)


def _merge_nested_counter(target: dict[str, Counter[str]], values: object) -> None:
    if not isinstance(values, dict):
        return
    for outer_key, inner in values.items():
        if not isinstance(inner, dict):
            continue
        outer_counter = target.setdefault(str(outer_key), Counter())
        for inner_key, count in inner.items():
            outer_counter[str(inner_key)] += int(count)


def _sorted_nested_counter(values: dict[str, Counter[str]]) -> dict[str, dict[str, int]]:
    return {outer: dict(sorted(counter.items())) for outer, counter in sorted(values.items())}


def _record_desired_class_stats(stats: dict[str, object], room: ProceduralRoom, class_name: str) -> None:
    class_key = str(class_name)
    _bump_stats_counter(stats, "desired_class_counts", class_key)
    _bump_stats_nested_counter(stats, "desired_class_by_room_type", room.room_type, class_key)


def _record_placed_asset_stats(stats: dict[str, object], room: ProceduralRoom, entry: ProceduralAssetEntry) -> None:
    class_key = str(entry.asset.placement_class or "unknown")
    _bump_stats_counter(stats, "placed_class_counts", class_key)
    _bump_stats_counter(stats, "placed_room_type_counts", room.room_type)
    _bump_stats_nested_counter(stats, "placed_class_by_room_type", room.room_type, class_key)


def _record_skipped_class_stats(
    stats: dict[str, object],
    room: ProceduralRoom,
    class_name: str,
    reason: str,
) -> None:
    class_key = str(class_name)
    _bump_stats_counter(stats, "skipped_class_counts", class_key)
    _bump_stats_counter(stats, "skipped_reason_counts", reason)
    _bump_stats_nested_counter(stats, "skipped_class_by_room_type", room.room_type, class_key)


def aggregate_procedural_precheck_skip_summary(skipped_records: list[dict[str, object]] | None) -> dict[str, object]:
    skipped_records = skipped_records or []
    layout_totals: Counter[str] = Counter()
    configured_layout_totals: Counter[str] = Counter()
    error_totals: Counter[str] = Counter()
    for record in skipped_records:
        procedural = record.get("procedural") if isinstance(record.get("procedural"), dict) else {}
        layout = str(record.get("layout") or procedural.get("layout") or "unknown")
        configured_layout = str(record.get("configured_layout") or procedural.get("configured_layout") or layout)
        layout_totals[layout] += 1
        configured_layout_totals[configured_layout] += 1
        errors = record.get("errors") if isinstance(record.get("errors"), list) else []
        for error in errors:
            if isinstance(error, dict):
                error_totals[str(error.get("code") or "unknown")] += 1
    return {
        "attempt_count": len(skipped_records),
        "layout_counts": dict(sorted(layout_totals.items())),
        "configured_layout_counts": dict(sorted(configured_layout_totals.items())),
        "error_counts": dict(sorted(error_totals.items())),
    }


def aggregate_procedural_run_report(
    scene_records: list[dict[str, object]],
    precheck_skipped_scenes: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    procedural_records = [record for record in scene_records if isinstance(record.get("procedural"), dict)]
    room_counts: list[float] = []
    adjacency_counts: list[float] = []
    topology_edge_counts: list[float] = []
    topology_leaf_counts: list[float] = []
    topology_branch_counts: list[float] = []
    topology_component_counts: list[float] = []
    topology_max_degrees: list[float] = []
    topology_mean_degrees: list[float] = []
    topology_graph_diameters: list[float] = []
    window_counts: list[float] = []
    room_areas: list[float] = []
    room_aspects: list[float] = []
    footprint_fill_ratios: list[float] = []
    footprint_concavity_areas: list[float] = []
    desired_counts: list[float] = []
    placement_counts: list[float] = []
    skipped_counts: list[float] = []
    placement_ratios: list[float] = []
    type_totals: Counter[str] = Counter()
    layout_totals: Counter[str] = Counter()
    configured_layout_totals: Counter[str] = Counter()
    precheck_error_totals: Counter[str] = Counter()
    group_attempt_totals: Counter[str] = Counter()
    group_success_totals: Counter[str] = Counter()
    desired_class_totals: Counter[str] = Counter()
    placed_class_totals: Counter[str] = Counter()
    skipped_class_totals: Counter[str] = Counter()
    placed_room_type_totals: Counter[str] = Counter()
    skipped_reason_totals: Counter[str] = Counter()
    desired_class_by_room_type_totals: dict[str, Counter[str]] = {}
    placed_class_by_room_type_totals: dict[str, Counter[str]] = {}
    skipped_class_by_room_type_totals: dict[str, Counter[str]] = {}
    precheck_ok_count = 0
    precheck_failed_count = 0
    room_type_geometry_failed_count = 0
    room_type_geometry_issue_totals: Counter[str] = Counter()
    scene_summaries: list[dict[str, object]] = []

    for record in procedural_records:
        procedural = record["procedural"]
        if not isinstance(procedural, dict):
            continue
        rooms = procedural.get("rooms") if isinstance(procedural.get("rooms"), list) else []
        room_count = int(procedural.get("room_count", len(rooms)) or len(rooms))
        layout = str(procedural.get("layout") or "unknown")
        configured_layout = str(procedural.get("configured_layout") or layout)
        layout_totals[layout] += 1
        configured_layout_totals[configured_layout] += 1
        adjacency_count = int(procedural.get("adjacency_count", 0) or 0)
        window_count = int(procedural.get("window_count", 0) or 0)
        topology = procedural.get("topology") if isinstance(procedural.get("topology"), dict) else {}
        room_counts.append(float(room_count))
        adjacency_counts.append(float(adjacency_count))
        if topology:
            topology_edge_counts.append(float(topology.get("edge_count", 0) or 0))
            topology_leaf_counts.append(float(topology.get("leaf_room_count", 0) or 0))
            topology_branch_counts.append(float(topology.get("branch_room_count", 0) or 0))
            topology_component_counts.append(float(topology.get("component_count", 0) or 0))
            topology_max_degrees.append(float(topology.get("max_degree", 0) or 0))
            topology_mean_degrees.append(float(topology.get("mean_degree", 0.0) or 0.0))
            topology_graph_diameters.append(float(topology.get("graph_diameter", 0) or 0))
        window_counts.append(float(window_count))
        scene_room_areas: list[float] = []
        scene_room_aspects: list[float] = []
        scene_type_counts: Counter[str] = Counter()
        for room in rooms:
            if not isinstance(room, dict):
                continue
            room_type = str(room.get("room_type") or "Unknown")
            type_totals[room_type] += 1
            scene_type_counts[room_type] += 1
            if isinstance(room.get("area_m2"), int | float):
                area = float(room["area_m2"])
                room_areas.append(area)
                scene_room_areas.append(area)
            if isinstance(room.get("aspect_ratio"), int | float):
                aspect = float(room["aspect_ratio"])
                room_aspects.append(aspect)
                scene_room_aspects.append(aspect)

        placement_stats = procedural.get("placement_stats") if isinstance(procedural.get("placement_stats"), dict) else {}
        footprint = procedural.get("footprint") if isinstance(procedural.get("footprint"), dict) else {}
        if isinstance(footprint.get("fill_ratio"), int | float):
            footprint_fill_ratios.append(float(footprint["fill_ratio"]))
        if isinstance(footprint.get("concavity_area_m2"), int | float):
            footprint_concavity_areas.append(float(footprint["concavity_area_m2"]))
        desired_object_counts = (
            placement_stats.get("desired_object_counts")
            if isinstance(placement_stats.get("desired_object_counts"), dict)
            else {}
        )
        desired_total = sum(int(value) for value in desired_object_counts.values()) if desired_object_counts else 0
        placement_count = int(record.get("placement_count", 0) or 0)
        skipped_count = int(record.get("skipped_object_count", procedural.get("skipped_object_count", 0)) or 0)
        desired_counts.append(float(desired_total))
        placement_counts.append(float(placement_count))
        skipped_counts.append(float(skipped_count))
        if desired_total > 0:
            placement_ratios.append(placement_count / desired_total)

        desired_class_counts = (
            placement_stats.get("desired_class_counts")
            if isinstance(placement_stats.get("desired_class_counts"), dict)
            else {}
        )
        placed_class_counts = (
            placement_stats.get("placed_class_counts")
            if isinstance(placement_stats.get("placed_class_counts"), dict)
            else {}
        )
        skipped_class_counts = (
            placement_stats.get("skipped_class_counts")
            if isinstance(placement_stats.get("skipped_class_counts"), dict)
            else {}
        )
        placed_room_type_counts = (
            placement_stats.get("placed_room_type_counts")
            if isinstance(placement_stats.get("placed_room_type_counts"), dict)
            else {}
        )
        skipped_reason_counts = (
            placement_stats.get("skipped_reason_counts")
            if isinstance(placement_stats.get("skipped_reason_counts"), dict)
            else {}
        )
        _merge_counter(desired_class_totals, desired_class_counts)
        _merge_counter(placed_class_totals, placed_class_counts)
        _merge_counter(skipped_class_totals, skipped_class_counts)
        _merge_counter(placed_room_type_totals, placed_room_type_counts)
        _merge_counter(skipped_reason_totals, skipped_reason_counts)
        _merge_nested_counter(desired_class_by_room_type_totals, placement_stats.get("desired_class_by_room_type"))
        _merge_nested_counter(placed_class_by_room_type_totals, placement_stats.get("placed_class_by_room_type"))
        _merge_nested_counter(skipped_class_by_room_type_totals, placement_stats.get("skipped_class_by_room_type"))

        group_stats = placement_stats.get("group_stats") if isinstance(placement_stats.get("group_stats"), dict) else {}
        attempted = group_stats.get("attempted") if isinstance(group_stats.get("attempted"), dict) else {}
        succeeded = group_stats.get("succeeded") if isinstance(group_stats.get("succeeded"), dict) else {}
        for name, count in attempted.items():
            group_attempt_totals[str(name)] += int(count)
        for name, count in succeeded.items():
            group_success_totals[str(name)] += int(count)

        precheck = record.get("precheck") if isinstance(record.get("precheck"), dict) else {}
        precheck_ok = precheck.get("ok")
        if precheck_ok is True:
            precheck_ok_count += 1
        elif precheck_ok is False:
            precheck_failed_count += 1
        errors = precheck.get("errors") if isinstance(precheck.get("errors"), list) else []
        for error in errors:
            if isinstance(error, dict):
                precheck_error_totals[str(error.get("code") or "unknown")] += 1
        room_type_geometry = precheck.get("room_type_geometry") if isinstance(precheck.get("room_type_geometry"), dict) else {}
        if room_type_geometry:
            if room_type_geometry.get("ok") is False:
                room_type_geometry_failed_count += 1
            for issue_key in ("too_small", "too_large", "too_elongated"):
                issues = room_type_geometry.get(issue_key)
                if isinstance(issues, list) and issues:
                    room_type_geometry_issue_totals[issue_key] += len(issues)

        scene_summaries.append(
            {
                "scene_index": record.get("scene_index"),
                "scene_dir": record.get("scene_dir"),
                "scene_id": procedural.get("scene_id"),
                "layout": layout,
                "configured_layout": configured_layout,
                "room_count": room_count,
                "room_type_counts": dict(sorted(scene_type_counts.items())),
                "room_area_m2": numeric_summary(scene_room_areas),
                "room_aspect_ratio": numeric_summary(scene_room_aspects),
                "footprint": footprint,
                "topology": topology,
                "adjacency_count": adjacency_count,
                "window_count": window_count,
                "desired_object_count": desired_total,
                "placement_count": placement_count,
                "skipped_object_count": skipped_count,
                "placement_ratio": rounded(placement_count / desired_total) if desired_total > 0 else None,
                "desired_class_counts": dict(sorted((str(key), int(value)) for key, value in desired_class_counts.items())),
                "placed_class_counts": dict(sorted((str(key), int(value)) for key, value in placed_class_counts.items())),
                "skipped_class_counts": dict(sorted((str(key), int(value)) for key, value in skipped_class_counts.items())),
                "precheck_ok": precheck.get("ok"),
                "room_type_geometry_ok": room_type_geometry.get("ok") if room_type_geometry else None,
            }
        )

    scene_count = len(procedural_records)
    return {
        "schema_version": "scenegen.procedural.run_report.v1",
        "scene_count": scene_count,
        "layout_counts_total": dict(sorted(layout_totals.items())),
        "configured_layout_counts_total": dict(sorted(configured_layout_totals.items())),
        "room_count": numeric_summary(room_counts),
        "room_type_counts_total": dict(sorted(type_totals.items())),
        "room_type_counts_mean_per_scene": {
            name: rounded(count / scene_count) for name, count in sorted(type_totals.items())
        }
        if scene_count
        else {},
        "room_area_m2": numeric_summary(room_areas),
        "room_aspect_ratio": numeric_summary(room_aspects),
        "footprint_fill_ratio": numeric_summary(footprint_fill_ratios),
        "footprint_concavity_area_m2": numeric_summary(footprint_concavity_areas),
        "adjacency_count": numeric_summary(adjacency_counts),
        "topology": {
            "edge_count": numeric_summary(topology_edge_counts),
            "leaf_room_count": numeric_summary(topology_leaf_counts),
            "branch_room_count": numeric_summary(topology_branch_counts),
            "component_count": numeric_summary(topology_component_counts),
            "max_degree": numeric_summary(topology_max_degrees),
            "mean_degree": numeric_summary(topology_mean_degrees),
            "graph_diameter": numeric_summary(topology_graph_diameters),
        },
        "window_count": numeric_summary(window_counts),
        "desired_object_count": numeric_summary(desired_counts),
        "placement_count": numeric_summary(placement_counts),
        "skipped_object_count": numeric_summary(skipped_counts),
        "placement_ratio": numeric_summary(placement_ratios),
        "desired_class_counts_total": dict(sorted(desired_class_totals.items())),
        "placed_class_counts_total": dict(sorted(placed_class_totals.items())),
        "skipped_class_counts_total": dict(sorted(skipped_class_totals.items())),
        "placed_room_type_counts_total": dict(sorted(placed_room_type_totals.items())),
        "skipped_reason_counts_total": dict(sorted(skipped_reason_totals.items())),
        "desired_class_by_room_type_total": _sorted_nested_counter(desired_class_by_room_type_totals),
        "placed_class_by_room_type_total": _sorted_nested_counter(placed_class_by_room_type_totals),
        "skipped_class_by_room_type_total": _sorted_nested_counter(skipped_class_by_room_type_totals),
        "placement_group_attempts_total": dict(sorted(group_attempt_totals.items())),
        "placement_group_success_total": dict(sorted(group_success_totals.items())),
        "precheck_ok_count": precheck_ok_count,
        "precheck_failed_count": precheck_failed_count,
        "precheck_ok_rate": rounded(precheck_ok_count / scene_count) if scene_count else 0.0,
        "precheck_error_counts": dict(sorted(precheck_error_totals.items())),
        "precheck_skipped_attempts": aggregate_procedural_precheck_skip_summary(precheck_skipped_scenes),
        "room_type_geometry_failed_count": room_type_geometry_failed_count,
        "room_type_geometry_issue_counts": dict(sorted(room_type_geometry_issue_totals.items())),
        "scenes": scene_summaries,
    }


def rotation_z_matrix(yaw: float) -> tuple[float, ...]:
    cos_yaw = math.cos(yaw)
    sin_yaw = math.sin(yaw)
    return (
        cos_yaw,
        -sin_yaw,
        0.0,
        0.0,
        sin_yaw,
        cos_yaw,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )


def translation_matrix(x: float, y: float, z: float) -> tuple[float, ...]:
    return (
        1.0,
        0.0,
        0.0,
        x,
        0.0,
        1.0,
        0.0,
        y,
        0.0,
        0.0,
        1.0,
        z,
        0.0,
        0.0,
        0.0,
        1.0,
    )


def unit_transform_for_yaw(yaw: float) -> tuple[float, ...]:
    return matrix_multiply(rotation_z_matrix(yaw), FRONT3D_TO_SCENEGEN)


def procedural_asset_bbox(entry: ProceduralAssetEntry, yaw: float, mesh_cache: dict[Path, list[Vec3]]) -> tuple[float, ...]:
    vertices = mesh_cache.get(entry.asset.obj_file)
    if vertices is None:
        vertices = load_obj_mesh(entry.asset.obj_file).vertices
        mesh_cache[entry.asset.obj_file] = vertices
    return transformed_bbox(vertices, unit_transform_for_yaw(yaw))


def procedural_asset_footprint_size(entry: ProceduralAssetEntry, yaw: float) -> tuple[float, float]:
    """Approximate SceneGen XY footprint from catalog dimensions before loading OBJ vertices.

    3D-FUTURE raw/normalized objects are treated as local Y-up meshes. The procedural transform maps
    local X to SceneGen X and local Z to SceneGen Y before applying a yaw around SceneGen Z.
    """

    width_x = max(0.0, float(entry.asset.width))
    width_y = max(0.0, float(entry.asset.height))
    if abs(math.sin(yaw)) > abs(math.cos(yaw)):
        return width_y, width_x
    return width_x, width_y


def procedural_asset_approx_bbox(
    entry: ProceduralAssetEntry,
    yaw: float,
    center_x: float,
    center_y: float,
) -> tuple[float, float, float, float, float, float]:
    width, length = procedural_asset_footprint_size(entry, yaw)
    return (
        center_x - width / 2.0,
        center_x + width / 2.0,
        center_y - length / 2.0,
        center_y + length / 2.0,
        0.0,
        max(0.0, float(entry.asset.length)),
    )


def select_room_policy_overrides(room_type: str, policies: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Select optional placement policy overrides for a room type."""

    by_room_type = policies.get("by_room_type") if isinstance(policies.get("by_room_type"), dict) else {}
    if room_type in by_room_type:
        return dict(by_room_type[room_type])
    lowered = room_type.lower()
    for name, room_policies in by_room_type.items():
        if str(name).lower() == lowered:
            return dict(room_policies)
    for name, room_policies in by_room_type.items():
        name_lowered = str(name).lower()
        if name_lowered in lowered or lowered in name_lowered:
            return dict(room_policies)
    return {}


def placement_policy_for_class(policies: dict[str, Any], class_name: str, room_type: str | None = None) -> dict[str, Any]:
    base_policy = dict(policies.get(class_name) or policies["default"])
    if room_type is None:
        return base_policy
    room_policies = select_room_policy_overrides(room_type, policies)
    override = room_policies.get(class_name) or room_policies.get("default")
    if override:
        base_policy.update(override)
    return base_policy


def candidate_pose_for_policy(
    room: ProceduralRoom,
    entry: ProceduralAssetEntry,
    policy: dict[str, Any],
    margin: float,
    rng: random.Random,
) -> tuple[float, float, float, float, float, str]:
    zone = str(policy.get("zone", "anywhere"))
    if zone == "wall":
        side = rng.choice(("south", "north", "west", "east"))
        yaw_by_side = {
            "south": 0.0,
            "north": math.pi,
            "west": math.pi / 2.0,
            "east": math.pi * 1.5,
        }
        yaw = yaw_by_side[side]
        width, length = procedural_asset_footprint_size(entry, yaw)
        min_x = room.x0 + margin + width / 2.0
        max_x = room.x1 - margin - width / 2.0
        min_y = room.y0 + margin + length / 2.0
        max_y = room.y1 - margin - length / 2.0
        offset = float(policy.get("wall_offset_m", 0.0))
        if side == "south":
            return yaw, rng.uniform(min_x, max_x), min(min_y + offset, max_y), width, length, zone
        if side == "north":
            return yaw, rng.uniform(min_x, max_x), max(max_y - offset, min_y), width, length, zone
        if side == "west":
            return yaw, min(min_x + offset, max_x), rng.uniform(min_y, max_y), width, length, zone
        return yaw, max(max_x - offset, min_x), rng.uniform(min_y, max_y), width, length, zone

    yaw = rng.choice([0.0, math.pi / 2.0, math.pi, math.pi * 1.5])
    width, length = procedural_asset_footprint_size(entry, yaw)
    min_x = room.x0 + margin + width / 2.0
    max_x = room.x1 - margin - width / 2.0
    min_y = room.y0 + margin + length / 2.0
    max_y = room.y1 - margin - length / 2.0
    if zone == "center":
        radius = max(0.0, min(room.width, room.length) * float(policy.get("center_radius_ratio", 0.35)))
        center_x = (room.x0 + room.x1) / 2.0 + rng.uniform(-radius, radius)
        center_y = (room.y0 + room.y1) / 2.0 + rng.uniform(-radius, radius)
        return yaw, min(max(center_x, min_x), max_x), min(max(center_y, min_y), max_y), width, length, zone
    return yaw, rng.uniform(min_x, max_x), rng.uniform(min_y, max_y), width, length, "anywhere"


def procedural_asset_transform_for_center(
    entry: ProceduralAssetEntry,
    yaw: float,
    center_x: float,
    center_y: float,
    mesh_cache: dict[Path, list[Vec3]],
) -> tuple[tuple[float, ...], tuple[float, float, float, float, float, float]]:
    local_matrix = unit_transform_for_yaw(yaw)
    bbox = procedural_asset_bbox(entry, yaw, mesh_cache)
    local_center_x = (bbox[0] + bbox[1]) / 2.0
    local_center_y = (bbox[2] + bbox[3]) / 2.0
    translation = translation_matrix(center_x - local_center_x, center_y - local_center_y, -bbox[4])
    matrix = matrix_multiply(translation, local_matrix)
    vertices = mesh_cache[entry.asset.obj_file]
    world_bbox = transformed_bbox(vertices, matrix)
    return matrix, world_bbox


def boxes_overlap_xy(
    left: tuple[float, float, float, float, float, float],
    right: tuple[float, float, float, float, float, float],
    padding: float,
) -> bool:
    return not (
        left[1] + padding <= right[0]
        or right[1] + padding <= left[0]
        or left[3] + padding <= right[2]
        or right[3] + padding <= left[2]
    )


def room_contains_bbox(room: ProceduralRoom, bbox: tuple[float, float, float, float, float, float], margin: float) -> bool:
    return (
        bbox[0] >= room.x0 + margin
        and bbox[1] <= room.x1 - margin
        and bbox[2] >= room.y0 + margin
        and bbox[3] <= room.y1 - margin
    )


def mesh_payload(uid: str, mesh_type: str, vertices: list[Vec3], faces: list[list[int]]) -> dict[str, object]:
    triangulated: list[int] = []
    for face in faces:
        if len(face) < 3:
            continue
        first = face[0]
        for index in range(1, len(face) - 1):
            triangulated.extend([first, face[index], face[index + 1]])
    return {
        "uid": uid,
        "jid": "",
        "type": mesh_type,
        "xyz": [round(value, 6) for vertex in vertices for value in vertex],
        "faces": triangulated,
    }


def add_quad(vertices: list[Vec3], faces: list[list[int]], a: Vec3, b: Vec3, c: Vec3, d: Vec3) -> None:
    start = len(vertices)
    vertices.extend([a, b, c, d])
    faces.append([start, start + 1, start + 2])
    faces.append([start, start + 2, start + 3])


def box_mesh(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> tuple[list[Vec3], list[list[int]]]:
    vertices: list[Vec3] = []
    faces: list[list[int]] = []
    add_quad(vertices, faces, (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0))
    add_quad(vertices, faces, (x0, y0, z1), (x0, y1, z1), (x1, y1, z1), (x1, y0, z1))
    add_quad(vertices, faces, (x0, y0, z0), (x0, y0, z1), (x1, y0, z1), (x1, y0, z0))
    add_quad(vertices, faces, (x1, y0, z0), (x1, y0, z1), (x1, y1, z1), (x1, y1, z0))
    add_quad(vertices, faces, (x1, y1, z0), (x1, y1, z1), (x0, y1, z1), (x0, y1, z0))
    add_quad(vertices, faces, (x0, y1, z0), (x0, y1, z1), (x0, y0, z1), (x0, y0, z0))
    return vertices, faces


def plane_mesh(x0: float, y0: float, z: float, x1: float, y1: float) -> tuple[list[Vec3], list[list[int]]]:
    vertices = [(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)]
    return vertices, [[0, 1, 2], [0, 2, 3]]


def vertical_plane_mesh(
    *,
    constant_axis: str,
    constant_value: float,
    a0: float,
    a1: float,
    z0: float,
    z1: float,
) -> tuple[list[Vec3], list[list[int]]]:
    if constant_axis == "x":
        vertices = [
            (constant_value, a0, z0),
            (constant_value, a1, z0),
            (constant_value, a1, z1),
            (constant_value, a0, z1),
        ]
    elif constant_axis == "y":
        vertices = [
            (a0, constant_value, z0),
            (a1, constant_value, z0),
            (a1, constant_value, z1),
            (a0, constant_value, z1),
        ]
    else:
        raise ValueError("constant_axis must be 'x' or 'y'")
    return vertices, [[0, 1, 2], [0, 2, 3]]


def write_architecture_obj(path: Path, rooms: list[ProceduralRoom], meshes: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("# Generated by SceneGen procedural_front3d\n")
        vertex_offset = 0
        for mesh in meshes:
            mesh_type = str(mesh["type"]).lower()
            if "floor" in mesh_type:
                material = "floor"
            elif "ceiling" in mesh_type:
                material = "ceiling"
            elif "window" in mesh_type:
                material = "window"
            else:
                material = "wall"
            handle.write(f"o {mesh['uid']}\n")
            handle.write(f"usemtl {material}\n")
            xyz = list(mesh["xyz"])
            for index in range(0, len(xyz), 3):
                handle.write(f"v {float(xyz[index]):.6f} {float(xyz[index + 1]):.6f} {float(xyz[index + 2]):.6f}\n")
            faces = list(mesh["faces"])
            for index in range(0, len(faces), 3):
                a, b, c = int(faces[index]), int(faces[index + 1]), int(faces[index + 2])
                handle.write(f"f {vertex_offset + a + 1} {vertex_offset + b + 1} {vertex_offset + c + 1}\n")
            vertex_offset += len(xyz) // 3
        handle.write(f"# room_count {len(rooms)}\n")


def room_type_sequence(
    room_count: int,
    room_types: tuple[str, ...],
    rng: random.Random,
    *,
    shuffle: bool,
    required_room_types: dict[str, int] | None = None,
    room_type_max_counts: dict[str, int | None] | None = None,
    room_type_weights: dict[str, float] | None = None,
) -> list[str]:
    if not room_types:
        return ["Room" for _ in range(room_count)]
    counts: Counter[str] = Counter()
    sequence: list[str] = []

    def under_limit(room_type: str) -> bool:
        max_count = (room_type_max_counts or {}).get(room_type)
        return max_count is None or counts[room_type] < int(max_count)

    def append_room_type(room_type: str) -> None:
        sequence.append(room_type)
        counts[room_type] += 1

    if required_room_types:
        for room_type in room_types:
            for _ in range(max(0, int(required_room_types.get(room_type, 0)))):
                if len(sequence) >= room_count:
                    break
                append_room_type(room_type)

    cycle_index = 0
    while len(sequence) < room_count:
        candidates = [room_type for room_type in room_types if under_limit(room_type)]
        if not candidates:
            break

        weights = [max(0.0, float((room_type_weights or {}).get(room_type, 0.0))) for room_type in candidates]
        if room_type_weights and any(weight > 0.0 for weight in weights):
            append_room_type(rng.choices(candidates, weights=weights, k=1)[0])
            continue

        if shuffle:
            batch = list(candidates)
            rng.shuffle(batch)
            for room_type in batch:
                if len(sequence) >= room_count:
                    break
                if under_limit(room_type):
                    append_room_type(room_type)
            continue

        appended = False
        for _ in range(len(room_types)):
            room_type = room_types[cycle_index % len(room_types)]
            cycle_index += 1
            if under_limit(room_type):
                append_room_type(room_type)
                appended = True
                break
        if not appended:
            break

    if required_room_types and shuffle:
        rng.shuffle(sequence)
    return sequence[:room_count]


def assign_room_types_to_areas(
    type_sequence: list[str],
    room_areas: list[float],
    *,
    assignment: str,
    area_priority: tuple[str, ...],
) -> list[str]:
    if assignment != "area_priority" or len(type_sequence) != len(room_areas):
        return type_sequence
    priority_rank = {room_type: index for index, room_type in enumerate(area_priority)}
    fallback_rank = len(priority_rank)
    room_order = sorted(range(len(room_areas)), key=lambda index: (-room_areas[index], index))
    type_order = sorted(
        range(len(type_sequence)),
        key=lambda index: (priority_rank.get(type_sequence[index], fallback_rank), index),
    )
    assigned = list(type_sequence)
    for room_index, type_index in zip(room_order, type_order, strict=False):
        assigned[room_index] = type_sequence[type_index]
    return assigned


def room_aspect_ratio(width: float, length: float) -> float:
    width = max(float(width), 1e-9)
    length = max(float(length), 1e-9)
    return max(width / length, length / width)


def room_type_geometry_fit_cost(
    room_type: str,
    area: float,
    aspect_ratio: float,
    *,
    geometry_rules: dict[str, dict[str, float | None]] | None,
    area_priority_rank: dict[str, int],
) -> float:
    rule = (geometry_rules or {}).get(room_type) or (geometry_rules or {}).get("default")
    cost = 0.0
    if isinstance(rule, dict):
        min_area = rule.get("min_area_m2")
        max_area = rule.get("max_area_m2")
        max_aspect = rule.get("max_aspect_ratio")
        if isinstance(min_area, int | float) and area < float(min_area):
            cost += 1_000_000.0 + ((float(min_area) - area) / max(float(min_area), 1.0)) ** 2 * 1000.0
        if isinstance(max_area, int | float) and area > float(max_area):
            cost += 1_000_000.0 + ((area - float(max_area)) / max(float(max_area), 1.0)) ** 2 * 1000.0
        if isinstance(max_aspect, int | float) and aspect_ratio > float(max_aspect):
            cost += 500_000.0 + ((aspect_ratio - float(max_aspect)) / max(float(max_aspect), 1.0)) ** 2 * 1000.0

        if isinstance(min_area, int | float) and isinstance(max_area, int | float):
            target_area = (float(min_area) + float(max_area)) / 2.0
            cost += abs(area - target_area) / max(target_area, 1.0)
        elif isinstance(min_area, int | float):
            cost -= area * 0.001
        elif isinstance(max_area, int | float):
            cost += area * 0.001

    fallback_rank = len(area_priority_rank)
    cost += float(area_priority_rank.get(room_type, fallback_rank)) * area * 0.0001
    return cost


def assign_room_types_to_geometry(
    type_sequence: list[str],
    room_geometries: list[tuple[float, float]],
    *,
    assignment: str,
    area_priority: tuple[str, ...],
    geometry_rules: dict[str, dict[str, float | None]] | None = None,
) -> list[str]:
    if assignment == "sequence" or len(type_sequence) != len(room_geometries):
        return type_sequence
    if assignment == "area_priority":
        return assign_room_types_to_areas(
            type_sequence,
            [area for area, _aspect in room_geometries],
            assignment=assignment,
            area_priority=area_priority,
        )
    if assignment != "geometry_fit":
        return type_sequence

    priority_rank = {room_type: index for index, room_type in enumerate(area_priority)}

    def total_cost(type_order: tuple[int, ...]) -> float:
        return sum(
            room_type_geometry_fit_cost(
                type_sequence[type_index],
                area,
                aspect,
                geometry_rules=geometry_rules,
                area_priority_rank=priority_rank,
            )
            for (area, aspect), type_index in zip(room_geometries, type_order, strict=True)
        )

    if len(type_sequence) <= 8:
        best_order = min(permutations(range(len(type_sequence))), key=total_cost)
        return [type_sequence[type_index] for type_index in best_order]

    remaining_type_indices = set(range(len(type_sequence)))
    assigned = [""] * len(type_sequence)
    room_order = sorted(range(len(room_geometries)), key=lambda index: (-room_geometries[index][0], index))
    for room_index in room_order:
        area, aspect = room_geometries[room_index]
        best_type_index = min(
            remaining_type_indices,
            key=lambda type_index: (
                room_type_geometry_fit_cost(
                    type_sequence[type_index],
                    area,
                    aspect,
                    geometry_rules=geometry_rules,
                    area_priority_rank=priority_rank,
                ),
                type_index,
            ),
        )
        assigned[room_index] = type_sequence[best_type_index]
        remaining_type_indices.remove(best_type_index)
    return assigned


def make_grid_room_layout(
    rng: random.Random,
    room_count_range: tuple[int, int],
    room_width_range: tuple[float, float],
    room_length_range: tuple[float, float],
    height_range: tuple[float, float],
    room_types: tuple[str, ...],
    required_room_types: dict[str, int] | None = None,
    room_type_assignment: str = "sequence",
    room_type_area_priority: tuple[str, ...] = (),
    room_type_max_counts: dict[str, int | None] | None = None,
    room_type_weights: dict[str, float] | None = None,
    room_type_geometry_rules: dict[str, dict[str, float | None]] | None = None,
) -> list[ProceduralRoom]:
    required_total = sum(max(0, int(count)) for count in (required_room_types or {}).values())
    room_count = max(rng.randint(room_count_range[0], room_count_range[1]), required_total)
    columns = max(1, math.ceil(math.sqrt(room_count)))
    rows = max(1, math.ceil(room_count / columns))
    column_widths = [round(rng.uniform(*room_width_range), 3) for _ in range(columns)]
    row_lengths = [round(rng.uniform(*room_length_range), 3) for _ in range(rows)]
    height = round(rng.uniform(*height_range), 3)
    type_sequence = room_type_sequence(
        room_count,
        room_types,
        rng,
        shuffle=False,
        required_room_types=required_room_types,
        room_type_max_counts=room_type_max_counts,
        room_type_weights=room_type_weights,
    )
    room_geometries: list[tuple[float, float]] = []
    for row in range(rows):
        for col in range(columns):
            if len(room_geometries) >= room_count:
                break
            room_geometries.append(
                (column_widths[col] * row_lengths[row], room_aspect_ratio(column_widths[col], row_lengths[row]))
            )
    type_sequence = assign_room_types_to_geometry(
        type_sequence,
        room_geometries,
        assignment=room_type_assignment,
        area_priority=room_type_area_priority,
        geometry_rules=room_type_geometry_rules,
    )
    rooms: list[ProceduralRoom] = []
    room_index = 0
    y0 = 0.0
    for row in range(rows):
        x0 = 0.0
        for col in range(columns):
            if room_index >= room_count:
                break
            rooms.append(
                ProceduralRoom(
                    room_id=f"proc_room_{room_index:02d}",
                    room_type=type_sequence[room_index],
                    x0=round(x0, 6),
                    y0=round(y0, 6),
                    x1=round(x0 + column_widths[col], 6),
                    y1=round(y0 + row_lengths[row], 6),
                    height=height,
                )
            )
            x0 += column_widths[col]
            room_index += 1
        y0 += row_lengths[row]
    return rooms


def split_region_once(
    rng: random.Random,
    region: LayoutRegion,
    room_width_range: tuple[float, float],
    room_length_range: tuple[float, float],
) -> tuple[LayoutRegion, LayoutRegion] | None:
    min_width = float(room_width_range[0])
    min_length = float(room_length_range[0])
    can_split_x = region.width >= 2.0 * min_width
    can_split_y = region.length >= 2.0 * min_length
    if not can_split_x and not can_split_y:
        return None
    if can_split_x and can_split_y:
        split_x = region.width / max(region.length, 1e-9) >= rng.uniform(0.75, 1.25)
    else:
        split_x = can_split_x
    ratio = rng.uniform(0.38, 0.62)
    if split_x:
        cut = region.x0 + region.width * ratio
        cut = min(max(cut, region.x0 + min_width), region.x1 - min_width)
        cut = round(cut, 6)
        return (
            LayoutRegion(region.x0, region.y0, cut, region.y1),
            LayoutRegion(cut, region.y0, region.x1, region.y1),
        )
    cut = region.y0 + region.length * ratio
    cut = min(max(cut, region.y0 + min_length), region.y1 - min_length)
    cut = round(cut, 6)
    return (
        LayoutRegion(region.x0, region.y0, region.x1, cut),
        LayoutRegion(region.x0, cut, region.x1, region.y1),
    )


def make_split_tree_room_layout(
    rng: random.Random,
    room_count_range: tuple[int, int],
    room_width_range: tuple[float, float],
    room_length_range: tuple[float, float],
    height_range: tuple[float, float],
    room_types: tuple[str, ...],
    required_room_types: dict[str, int] | None = None,
    room_type_assignment: str = "sequence",
    room_type_area_priority: tuple[str, ...] = (),
    room_type_max_counts: dict[str, int | None] | None = None,
    room_type_weights: dict[str, float] | None = None,
    room_type_geometry_rules: dict[str, dict[str, float | None]] | None = None,
) -> list[ProceduralRoom]:
    required_total = sum(max(0, int(count)) for count in (required_room_types or {}).values())
    room_count = max(rng.randint(room_count_range[0], room_count_range[1]), required_total)
    scale = math.sqrt(room_count)
    root_width = round(rng.uniform(room_width_range[0] * scale, room_width_range[1] * scale), 3)
    root_length = round(rng.uniform(room_length_range[0] * scale, room_length_range[1] * scale), 3)
    regions = [LayoutRegion(0.0, 0.0, root_width, root_length)]
    while len(regions) < room_count:
        candidates = sorted(enumerate(regions), key=lambda item: item[1].area, reverse=True)
        split_result: tuple[int, tuple[LayoutRegion, LayoutRegion]] | None = None
        for index, region in candidates:
            split = split_region_once(rng, region, room_width_range, room_length_range)
            if split is not None:
                split_result = (index, split)
                break
        if split_result is None:
            # Configuration is too tight for the sampled count; returning fewer rooms is better than
            # manufacturing invalid zero-area regions.
            break
        index, (left, right) = split_result
        regions.pop(index)
        regions.extend([left, right])
    regions = sorted(regions, key=lambda region: (region.y0, region.x0))
    height = round(rng.uniform(*height_range), 3)
    type_sequence = room_type_sequence(
        len(regions),
        room_types,
        rng,
        shuffle=True,
        required_room_types=required_room_types,
        room_type_max_counts=room_type_max_counts,
        room_type_weights=room_type_weights,
    )
    type_sequence = assign_room_types_to_geometry(
        type_sequence,
        [(region.area, room_aspect_ratio(region.width, region.length)) for region in regions],
        assignment=room_type_assignment,
        area_priority=room_type_area_priority,
        geometry_rules=room_type_geometry_rules,
    )
    return [
        ProceduralRoom(
            room_id=f"proc_room_{index:02d}",
            room_type=type_sequence[index],
            x0=round(region.x0, 6),
            y0=round(region.y0, 6),
            x1=round(region.x1, 6),
            y1=round(region.y1, 6),
            height=height,
        )
        for index, region in enumerate(regions)
    ]


def grow_connected_grid_cells(
    rng: random.Random,
    *,
    rows: int,
    columns: int,
    target_count: int,
) -> set[tuple[int, int]]:
    start = (rng.randrange(rows), rng.randrange(columns))
    selected = {start}
    while len(selected) < target_count:
        frontier: list[tuple[int, int]] = []
        for row, col in selected:
            for candidate in ((row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)):
                c_row, c_col = candidate
                if 0 <= c_row < rows and 0 <= c_col < columns and candidate not in selected:
                    frontier.append(candidate)
        if not frontier:
            break
        selected.add(rng.choice(frontier))
    return selected


def cells_fill_bounding_rectangle(cells: set[tuple[int, int]]) -> bool:
    if not cells:
        return False
    rows = [row for row, _col in cells]
    cols = [col for _row, col in cells]
    return len(cells) == (max(rows) - min(rows) + 1) * (max(cols) - min(cols) + 1)


def make_rect_union_room_layout(
    rng: random.Random,
    room_count_range: tuple[int, int],
    room_width_range: tuple[float, float],
    room_length_range: tuple[float, float],
    height_range: tuple[float, float],
    room_types: tuple[str, ...],
    required_room_types: dict[str, int] | None = None,
    room_type_assignment: str = "sequence",
    room_type_area_priority: tuple[str, ...] = (),
    room_type_max_counts: dict[str, int | None] | None = None,
    room_type_weights: dict[str, float] | None = None,
    room_type_geometry_rules: dict[str, dict[str, float | None]] | None = None,
) -> list[ProceduralRoom]:
    required_total = sum(max(0, int(count)) for count in (required_room_types or {}).values())
    room_count = max(rng.randint(room_count_range[0], room_count_range[1]), required_total)
    capacity = max(room_count + 1, 4)
    columns = max(2, math.ceil(math.sqrt(capacity)))
    rows = max(2, math.ceil(capacity / columns))
    while rows * columns <= room_count:
        if columns <= rows:
            columns += 1
        else:
            rows += 1

    selected_cells: set[tuple[int, int]] = set()
    for _attempt in range(24):
        candidate = grow_connected_grid_cells(rng, rows=rows, columns=columns, target_count=room_count)
        if len(candidate) == room_count and not cells_fill_bounding_rectangle(candidate):
            selected_cells = candidate
            break
        if len(candidate) == room_count:
            selected_cells = candidate
    if not selected_cells:
        selected_cells = grow_connected_grid_cells(rng, rows=rows, columns=columns, target_count=room_count)

    column_widths = [round(rng.uniform(*room_width_range), 3) for _ in range(columns)]
    row_lengths = [round(rng.uniform(*room_length_range), 3) for _ in range(rows)]
    x_offsets = [0.0]
    for width in column_widths:
        x_offsets.append(round(x_offsets[-1] + width, 6))
    y_offsets = [0.0]
    for length in row_lengths:
        y_offsets.append(round(y_offsets[-1] + length, 6))

    regions = [
        LayoutRegion(
            x_offsets[col],
            y_offsets[row],
            x_offsets[col + 1],
            y_offsets[row + 1],
        )
        for row, col in sorted(selected_cells)
    ]
    height = round(rng.uniform(*height_range), 3)
    type_sequence = room_type_sequence(
        len(regions),
        room_types,
        rng,
        shuffle=True,
        required_room_types=required_room_types,
        room_type_max_counts=room_type_max_counts,
        room_type_weights=room_type_weights,
    )
    type_sequence = assign_room_types_to_geometry(
        type_sequence,
        [(region.area, room_aspect_ratio(region.width, region.length)) for region in regions],
        assignment=room_type_assignment,
        area_priority=room_type_area_priority,
        geometry_rules=room_type_geometry_rules,
    )
    return [
        ProceduralRoom(
            room_id=f"proc_room_{index:02d}",
            room_type=type_sequence[index],
            x0=round(region.x0, 6),
            y0=round(region.y0, 6),
            x1=round(region.x1, 6),
            y1=round(region.y1, 6),
            height=height,
        )
        for index, region in enumerate(regions)
    ]


def regions_to_procedural_rooms(
    regions: list[LayoutRegion],
    *,
    rng: random.Random,
    height_range: tuple[float, float],
    room_types: tuple[str, ...],
    required_room_types: dict[str, int] | None,
    room_type_assignment: str,
    room_type_area_priority: tuple[str, ...],
    room_type_max_counts: dict[str, int | None] | None,
    room_type_weights: dict[str, float] | None,
    room_type_geometry_rules: dict[str, dict[str, float | None]] | None,
) -> list[ProceduralRoom]:
    if not regions:
        return []
    min_x = min(region.x0 for region in regions)
    min_y = min(region.y0 for region in regions)
    normalized_regions = [
        LayoutRegion(
            round(region.x0 - min_x, 6),
            round(region.y0 - min_y, 6),
            round(region.x1 - min_x, 6),
            round(region.y1 - min_y, 6),
        )
        for region in regions
    ]
    normalized_regions = sorted(normalized_regions, key=lambda region: (region.y0, region.x0, region.y1, region.x1))
    height = round(rng.uniform(*height_range), 3)
    type_sequence = room_type_sequence(
        len(normalized_regions),
        room_types,
        rng,
        shuffle=True,
        required_room_types=required_room_types,
        room_type_max_counts=room_type_max_counts,
        room_type_weights=room_type_weights,
    )
    type_sequence = assign_room_types_to_geometry(
        type_sequence,
        [(region.area, room_aspect_ratio(region.width, region.length)) for region in normalized_regions],
        assignment=room_type_assignment,
        area_priority=room_type_area_priority,
        geometry_rules=room_type_geometry_rules,
    )
    return [
        ProceduralRoom(
            room_id=f"proc_room_{index:02d}",
            room_type=type_sequence[index],
            x0=round(region.x0, 6),
            y0=round(region.y0, 6),
            x1=round(region.x1, 6),
            y1=round(region.y1, 6),
            height=height,
        )
        for index, region in enumerate(normalized_regions)
    ]


def regions_overlap(left: LayoutRegion, right: LayoutRegion, *, epsilon: float = 1e-6) -> bool:
    return (
        min(left.x1, right.x1) - max(left.x0, right.x0) > epsilon
        and min(left.y1, right.y1) - max(left.y0, right.y0) > epsilon
    )


def attach_room_region(
    rng: random.Random,
    parent: LayoutRegion,
    *,
    side: str,
    width: float,
    length: float,
    min_shared_m: float,
) -> LayoutRegion | None:
    if side in {"east", "west"}:
        overlap_axis = parent.length
        new_axis = length
        min_shared = min(min_shared_m, overlap_axis, new_axis)
        if min_shared <= 0:
            return None
        low = parent.y0 - length + min_shared
        high = parent.y1 - min_shared
        if high < low:
            return None
        y0 = round(rng.uniform(low, high), 6)
        if side == "east":
            return LayoutRegion(parent.x1, y0, round(parent.x1 + width, 6), round(y0 + length, 6))
        return LayoutRegion(round(parent.x0 - width, 6), y0, parent.x0, round(y0 + length, 6))

    overlap_axis = parent.width
    new_axis = width
    min_shared = min(min_shared_m, overlap_axis, new_axis)
    if min_shared <= 0:
        return None
    low = parent.x0 - width + min_shared
    high = parent.x1 - min_shared
    if high < low:
        return None
    x0 = round(rng.uniform(low, high), 6)
    if side == "north":
        return LayoutRegion(x0, parent.y1, round(x0 + width, 6), round(parent.y1 + length, 6))
    if side == "south":
        return LayoutRegion(x0, round(parent.y0 - length, 6), round(x0 + width, 6), parent.y0)
    return None


def make_room_graph_layout(
    rng: random.Random,
    room_count_range: tuple[int, int],
    room_width_range: tuple[float, float],
    room_length_range: tuple[float, float],
    height_range: tuple[float, float],
    room_types: tuple[str, ...],
    required_room_types: dict[str, int] | None = None,
    room_type_assignment: str = "sequence",
    room_type_area_priority: tuple[str, ...] = (),
    room_type_max_counts: dict[str, int | None] | None = None,
    room_type_weights: dict[str, float] | None = None,
    room_type_geometry_rules: dict[str, dict[str, float | None]] | None = None,
) -> list[ProceduralRoom]:
    required_total = sum(max(0, int(count)) for count in (required_room_types or {}).values())
    room_count = max(rng.randint(room_count_range[0], room_count_range[1]), required_total)
    regions = [
        LayoutRegion(
            0.0,
            0.0,
            round(rng.uniform(*room_width_range), 3),
            round(rng.uniform(*room_length_range), 3),
        )
    ]
    sides = ("east", "west", "north", "south")
    min_shared_m = max(1.0, min(room_width_range[0], room_length_range[0]) * 0.25)
    max_attempts = max(120, room_count * 80)
    attempts = 0
    while len(regions) < room_count and attempts < max_attempts:
        attempts += 1
        parent = rng.choice(regions)
        side = rng.choice(sides)
        candidate = attach_room_region(
            rng,
            parent,
            side=side,
            width=round(rng.uniform(*room_width_range), 3),
            length=round(rng.uniform(*room_length_range), 3),
            min_shared_m=min_shared_m,
        )
        if candidate is None:
            continue
        if any(regions_overlap(candidate, region) for region in regions):
            continue
        regions.append(candidate)

    if len(regions) < room_count:
        return make_rect_union_room_layout(
            rng,
            room_count_range,
            room_width_range,
            room_length_range,
            height_range,
            room_types,
            required_room_types=required_room_types,
            room_type_assignment=room_type_assignment,
            room_type_area_priority=room_type_area_priority,
            room_type_max_counts=room_type_max_counts,
            room_type_weights=room_type_weights,
            room_type_geometry_rules=room_type_geometry_rules,
        )

    return regions_to_procedural_rooms(
        regions,
        rng=rng,
        height_range=height_range,
        room_types=room_types,
        required_room_types=required_room_types,
        room_type_assignment=room_type_assignment,
        room_type_area_priority=room_type_area_priority,
        room_type_max_counts=room_type_max_counts,
        room_type_weights=room_type_weights,
        room_type_geometry_rules=room_type_geometry_rules,
    )


def notched_shell_regions(
    rng: random.Random,
    *,
    width: float,
    length: float,
    min_width: float,
    min_length: float,
) -> list[LayoutRegion] | None:
    sides = ["east", "west", "north", "south"]
    rng.shuffle(sides)
    for side in sides:
        if side in {"east", "west"}:
            if width < min_width * 2.0 or length < min_length * 3.0:
                continue
            max_depth = min(width - min_width, width * 0.45)
            max_notch_length = min(length - 2.0 * min_length, length * 0.48)
            if max_depth < min_width * 0.45 or max_notch_length < min_length:
                continue
            depth = round(rng.uniform(min_width * 0.45, max_depth), 3)
            notch_length = round(rng.uniform(min_length, max_notch_length), 3)
            notch_y0 = round(rng.uniform(min_length, length - notch_length - min_length), 6)
            notch_y1 = round(notch_y0 + notch_length, 6)
            inner_x = round(width - depth, 6)
            if side == "east":
                return [
                    LayoutRegion(0.0, 0.0, width, notch_y0),
                    LayoutRegion(0.0, notch_y0, inner_x, notch_y1),
                    LayoutRegion(0.0, notch_y1, width, length),
                ]
            return [
                LayoutRegion(0.0, 0.0, width, notch_y0),
                LayoutRegion(depth, notch_y0, width, notch_y1),
                LayoutRegion(0.0, notch_y1, width, length),
            ]
        if length < min_length * 2.0 or width < min_width * 3.0:
            continue
        max_depth = min(length - min_length, length * 0.45)
        max_notch_width = min(width - 2.0 * min_width, width * 0.48)
        if max_depth < min_length * 0.45 or max_notch_width < min_width:
            continue
        depth = round(rng.uniform(min_length * 0.45, max_depth), 3)
        notch_width = round(rng.uniform(min_width, max_notch_width), 3)
        notch_x0 = round(rng.uniform(min_width, width - notch_width - min_width), 6)
        notch_x1 = round(notch_x0 + notch_width, 6)
        inner_y = round(length - depth, 6)
        if side == "north":
            return [
                LayoutRegion(0.0, 0.0, notch_x0, length),
                LayoutRegion(notch_x0, 0.0, notch_x1, inner_y),
                LayoutRegion(notch_x1, 0.0, width, length),
            ]
        return [
            LayoutRegion(0.0, 0.0, notch_x0, length),
            LayoutRegion(notch_x0, depth, notch_x1, length),
            LayoutRegion(notch_x1, 0.0, width, length),
        ]
    return None


def split_regions_to_count(
    rng: random.Random,
    regions: list[LayoutRegion],
    *,
    target_count: int,
    room_width_range: tuple[float, float],
    room_length_range: tuple[float, float],
) -> list[LayoutRegion] | None:
    result = list(regions)
    while len(result) < target_count:
        candidates = sorted(enumerate(result), key=lambda item: item[1].area, reverse=True)
        split_result: tuple[int, tuple[LayoutRegion, LayoutRegion]] | None = None
        for index, region in candidates:
            split = split_region_once(rng, region, room_width_range, room_length_range)
            if split is not None:
                split_result = (index, split)
                break
        if split_result is None:
            return None
        index, split = split_result
        result.pop(index)
        result.extend(split)
    return result


def make_polygon_shell_room_layout(
    rng: random.Random,
    room_count_range: tuple[int, int],
    room_width_range: tuple[float, float],
    room_length_range: tuple[float, float],
    height_range: tuple[float, float],
    room_types: tuple[str, ...],
    required_room_types: dict[str, int] | None = None,
    room_type_assignment: str = "sequence",
    room_type_area_priority: tuple[str, ...] = (),
    room_type_max_counts: dict[str, int | None] | None = None,
    room_type_weights: dict[str, float] | None = None,
    room_type_geometry_rules: dict[str, dict[str, float | None]] | None = None,
) -> list[ProceduralRoom]:
    required_total = sum(max(0, int(count)) for count in (required_room_types or {}).values())
    room_count = max(rng.randint(room_count_range[0], room_count_range[1]), required_total, 3)
    scale = math.sqrt(room_count)
    min_width = float(room_width_range[0])
    min_length = float(room_length_range[0])
    root_width_min = max(room_width_range[0] * scale, min_width * 3.2)
    root_length_min = max(room_length_range[0] * scale, min_length * 3.2)
    root_width = round(rng.uniform(root_width_min, max(root_width_min, room_width_range[1] * scale)), 3)
    root_length = round(rng.uniform(root_length_min, max(root_length_min, room_length_range[1] * scale)), 3)

    base_regions = notched_shell_regions(
        rng,
        width=root_width,
        length=root_length,
        min_width=min_width,
        min_length=min_length,
    )
    if base_regions is None:
        return make_split_tree_room_layout(
            rng,
            room_count_range,
            room_width_range,
            room_length_range,
            height_range,
            room_types,
            required_room_types=required_room_types,
            room_type_assignment=room_type_assignment,
            room_type_area_priority=room_type_area_priority,
            room_type_max_counts=room_type_max_counts,
            room_type_weights=room_type_weights,
            room_type_geometry_rules=room_type_geometry_rules,
        )

    regions = split_regions_to_count(
        rng,
        base_regions,
        target_count=room_count,
        room_width_range=room_width_range,
        room_length_range=room_length_range,
    )
    if regions is None:
        return make_split_tree_room_layout(
            rng,
            room_count_range,
            room_width_range,
            room_length_range,
            height_range,
            room_types,
            required_room_types=required_room_types,
            room_type_assignment=room_type_assignment,
            room_type_area_priority=room_type_area_priority,
            room_type_max_counts=room_type_max_counts,
            room_type_weights=room_type_weights,
            room_type_geometry_rules=room_type_geometry_rules,
        )

    return regions_to_procedural_rooms(
        regions,
        rng=rng,
        height_range=height_range,
        room_types=room_types,
        required_room_types=required_room_types,
        room_type_assignment=room_type_assignment,
        room_type_area_priority=room_type_area_priority,
        room_type_max_counts=room_type_max_counts,
        room_type_weights=room_type_weights,
        room_type_geometry_rules=room_type_geometry_rules,
    )


def make_corridor_spine_room_layout(
    rng: random.Random,
    room_count_range: tuple[int, int],
    room_width_range: tuple[float, float],
    room_length_range: tuple[float, float],
    height_range: tuple[float, float],
    room_types: tuple[str, ...],
    required_room_types: dict[str, int] | None = None,
    room_type_assignment: str = "sequence",
    room_type_area_priority: tuple[str, ...] = (),
    room_type_max_counts: dict[str, int | None] | None = None,
    room_type_weights: dict[str, float] | None = None,
    room_type_geometry_rules: dict[str, dict[str, float | None]] | None = None,
) -> list[ProceduralRoom]:
    hall_type = "Hallway"
    required = dict(required_room_types or {})
    required_hall_count = max(0, int(required.pop(hall_type, 0)))
    required_side_count = sum(max(0, int(count)) for count in required.values())
    max_room_count = int(room_count_range[1])
    hallway_limit = (room_type_max_counts or {}).get(hall_type)
    hallway_capacity = max_room_count if hallway_limit is None else max(1, int(hallway_limit))
    hall_count = min(
        max(math.ceil(required_side_count / 2.0), required_hall_count, 1),
        hallway_capacity,
    )
    room_count = max(rng.randint(room_count_range[0], room_count_range[1]), required_side_count + hall_count)
    room_count = min(room_count, hall_count * 3)
    side_count = max(0, room_count - hall_count)
    if side_count == 0:
        side_count = 1
        room_count = hall_count + side_count

    hall_widths = [round(rng.uniform(room_width_range[0], min(room_width_range[1], room_width_range[0] + 1.4)), 3) for _ in range(hall_count)]
    south_depth = round(rng.uniform(*room_length_range), 3)
    north_depth = round(rng.uniform(*room_length_range), 3)
    hallway_depth = round(rng.uniform(max(2.0, room_length_range[0] * 0.55), max(2.2, min(3.0, room_length_range[1] * 0.65))), 3)
    height = round(rng.uniform(*height_range), 3)

    side_regions: list[LayoutRegion] = []
    slots: list[tuple[str, int]] = [("south", index) for index in range(hall_count)] + [("north", index) for index in range(hall_count)]
    rng.shuffle(slots)
    slots = sorted(slots[:side_count], key=lambda item: (item[0] != "south", item[1]))

    x_offsets = [0.0]
    for width in hall_widths:
        x_offsets.append(round(x_offsets[-1] + width, 6))
    for side, index in slots:
        x0 = x_offsets[index]
        x1 = x_offsets[index + 1]
        if side == "south":
            side_regions.append(LayoutRegion(x0, 0.0, x1, south_depth))
        else:
            side_regions.append(LayoutRegion(x0, south_depth + hallway_depth, x1, south_depth + hallway_depth + north_depth))

    side_room_types = tuple(room_type for room_type in room_types if room_type != hall_type)
    side_max_counts = {
        room_type: max_count for room_type, max_count in (room_type_max_counts or {}).items() if room_type != hall_type
    }
    side_weights = {room_type: weight for room_type, weight in (room_type_weights or {}).items() if room_type != hall_type}
    type_sequence = room_type_sequence(
        len(side_regions),
        side_room_types or room_types,
        rng,
        shuffle=True,
        required_room_types=required,
        room_type_max_counts=side_max_counts,
        room_type_weights=side_weights,
    )
    type_sequence = assign_room_types_to_geometry(
        type_sequence,
        [(region.area, room_aspect_ratio(region.width, region.length)) for region in side_regions],
        assignment=room_type_assignment,
        area_priority=room_type_area_priority,
        geometry_rules=room_type_geometry_rules,
    )

    room_specs: list[tuple[str, LayoutRegion]] = []
    for index in range(hall_count):
        room_specs.append(
            (
                hall_type,
                LayoutRegion(x_offsets[index], south_depth, x_offsets[index + 1], south_depth + hallway_depth),
            )
        )
    room_specs.extend(zip(type_sequence, side_regions, strict=False))
    room_specs = sorted(room_specs, key=lambda item: (item[1].y0, item[1].x0))
    return [
        ProceduralRoom(
            room_id=f"proc_room_{index:02d}",
            room_type=room_type,
            x0=round(region.x0, 6),
            y0=round(region.y0, 6),
            x1=round(region.x1, 6),
            y1=round(region.y1, 6),
            height=height,
        )
        for index, (room_type, region) in enumerate(room_specs)
    ]


def make_room_layout(
    rng: random.Random,
    layout: str,
    room_count_range: tuple[int, int],
    room_width_range: tuple[float, float],
    room_length_range: tuple[float, float],
    height_range: tuple[float, float],
    room_types: tuple[str, ...],
    required_room_types: dict[str, int] | None = None,
    room_type_assignment: str = "sequence",
    room_type_area_priority: tuple[str, ...] = (),
    room_type_max_counts: dict[str, int | None] | None = None,
    room_type_weights: dict[str, float] | None = None,
    room_type_geometry_rules: dict[str, dict[str, float | None]] | None = None,
) -> list[ProceduralRoom]:
    if layout == "grid":
        return make_grid_room_layout(
            rng,
            room_count_range,
            room_width_range,
            room_length_range,
            height_range,
            room_types,
            required_room_types=required_room_types,
            room_type_assignment=room_type_assignment,
            room_type_area_priority=room_type_area_priority,
            room_type_max_counts=room_type_max_counts,
            room_type_weights=room_type_weights,
            room_type_geometry_rules=room_type_geometry_rules,
        )
    if layout == "split_tree":
        return make_split_tree_room_layout(
            rng,
            room_count_range,
            room_width_range,
            room_length_range,
            height_range,
            room_types,
            required_room_types=required_room_types,
            room_type_assignment=room_type_assignment,
            room_type_area_priority=room_type_area_priority,
            room_type_max_counts=room_type_max_counts,
            room_type_weights=room_type_weights,
            room_type_geometry_rules=room_type_geometry_rules,
        )
    if layout == "rect_union":
        return make_rect_union_room_layout(
            rng,
            room_count_range,
            room_width_range,
            room_length_range,
            height_range,
            room_types,
            required_room_types=required_room_types,
            room_type_assignment=room_type_assignment,
            room_type_area_priority=room_type_area_priority,
            room_type_max_counts=room_type_max_counts,
            room_type_weights=room_type_weights,
            room_type_geometry_rules=room_type_geometry_rules,
        )
    if layout == "room_graph":
        return make_room_graph_layout(
            rng,
            room_count_range,
            room_width_range,
            room_length_range,
            height_range,
            room_types,
            required_room_types=required_room_types,
            room_type_assignment=room_type_assignment,
            room_type_area_priority=room_type_area_priority,
            room_type_max_counts=room_type_max_counts,
            room_type_weights=room_type_weights,
            room_type_geometry_rules=room_type_geometry_rules,
        )
    if layout == "polygon_shell":
        return make_polygon_shell_room_layout(
            rng,
            room_count_range,
            room_width_range,
            room_length_range,
            height_range,
            room_types,
            required_room_types=required_room_types,
            room_type_assignment=room_type_assignment,
            room_type_area_priority=room_type_area_priority,
            room_type_max_counts=room_type_max_counts,
            room_type_weights=room_type_weights,
            room_type_geometry_rules=room_type_geometry_rules,
        )
    if layout == "corridor_spine":
        return make_corridor_spine_room_layout(
            rng,
            room_count_range,
            room_width_range,
            room_length_range,
            height_range,
            room_types,
            required_room_types=required_room_types,
            room_type_assignment=room_type_assignment,
            room_type_area_priority=room_type_area_priority,
            room_type_max_counts=room_type_max_counts,
            room_type_weights=room_type_weights,
            room_type_geometry_rules=room_type_geometry_rules,
        )
    raise ValueError(f"Unsupported procedural layout: {layout}")


def choose_procedural_layout(
    configured_layout: str,
    layout_weights: dict[str, float] | None,
    rng: random.Random,
) -> str:
    if configured_layout != "mixed":
        return configured_layout
    supported_layouts = ("grid", "split_tree", "rect_union", "room_graph", "polygon_shell", "corridor_spine")
    weights = [max(0.0, float((layout_weights or {}).get(layout, 0.0))) for layout in supported_layouts]
    if not any(weight > 0.0 for weight in weights):
        raise ValueError("procedural.layout_weights must assign a positive weight for mixed layout")
    return rng.choices(supported_layouts, weights=weights, k=1)[0]


def select_room_profile_spec(room_type: str, profiles: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Select the configured furnishing profile for a procedural room type."""

    if room_type in profiles:
        return room_type, profiles[room_type]
    lowered = room_type.lower()
    for name, profile in profiles.items():
        if name.lower() == lowered:
            return name, profile
    for name, profile in profiles.items():
        name_lowered = name.lower()
        if name != "default" and (name_lowered in lowered or lowered in name_lowered):
            return name, profile
    return "default", profiles["default"]


def select_room_profile(room_type: str, profiles: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    """Select a room profile name and class sequence."""

    profile_name, profile = select_room_profile_spec(room_type, profiles)
    return profile_name, list(profile["classes"])


def desired_classes_from_profile(
    room_type: str,
    profiles: dict[str, dict[str, Any]],
    target_count: int,
    rng: random.Random,
) -> list[str]:
    """Expand a room furnishing profile to the sampled object count."""

    _profile_name, classes = select_room_profile(room_type, profiles)
    if target_count <= len(classes):
        return classes[:target_count]
    return classes + [rng.choice(classes) for _ in range(target_count - len(classes))]


def object_count_for_room_area(room_area: float, object_count_config: dict[str, Any], rng: random.Random) -> int:
    strategy = str(object_count_config["strategy"])
    if strategy == "range":
        min_count, max_count = (int(value) for value in object_count_config["range"])
        return rng.randint(min_count, max_count)
    if strategy == "area_adaptive":
        min_count = int(object_count_config["min"])
        max_count = int(object_count_config["max"])
        area_per_object = float(object_count_config["area_per_object_m2"])
        jitter_min, jitter_max = (int(value) for value in object_count_config["jitter"])
        base_count = int(round(float(room_area) / area_per_object))
        jitter = rng.randint(jitter_min, jitter_max)
        return min(max(base_count + jitter, min_count), max_count)
    raise ValueError(f"Unsupported procedural object count strategy: {strategy}")


def object_count_config_for_room(room_type: str, object_count_config: dict[str, Any]) -> dict[str, Any]:
    by_room_type = object_count_config.get("by_room_type") if isinstance(object_count_config.get("by_room_type"), dict) else {}
    if room_type in by_room_type:
        return by_room_type[room_type]
    lowered = room_type.lower()
    for name, config in by_room_type.items():
        if str(name).lower() == lowered:
            return config
    for name, config in by_room_type.items():
        name_lowered = str(name).lower()
        if name_lowered in lowered or lowered in name_lowered:
            return config
    return {key: value for key, value in object_count_config.items() if key != "by_room_type"}


def candidates_for_asset_reuse(
    candidates: list[ProceduralAssetEntry],
    room_id: str,
    scene_model_counts: Counter[str],
    room_model_counts: Counter[str],
    asset_reuse_config: dict[str, Any],
) -> tuple[list[ProceduralAssetEntry], bool]:
    max_per_room = asset_reuse_config.get("max_per_room")
    max_per_scene = asset_reuse_config.get("max_per_scene")
    allowed = [
        entry
        for entry in candidates
        if (max_per_room is None or room_model_counts[entry.model_id] < int(max_per_room))
        and (max_per_scene is None or scene_model_counts[entry.model_id] < int(max_per_scene))
    ]
    if allowed:
        return allowed, False
    if bool(asset_reuse_config.get("relax_if_needed", True)):
        return candidates, True
    return [], False


def choose_entry_with_reuse(
    candidates: list[ProceduralAssetEntry],
    room_id: str,
    scene_model_counts: Counter[str],
    room_model_counts: Counter[str],
    asset_reuse_config: dict[str, Any],
    rng: random.Random,
    stats: dict[str, object],
) -> ProceduralAssetEntry | None:
    allowed, relaxed = candidates_for_asset_reuse(candidates, room_id, scene_model_counts, room_model_counts, asset_reuse_config)
    if relaxed:
        stats["asset_reuse_relaxed_count"] = int(stats["asset_reuse_relaxed_count"]) + 1
    if not allowed:
        stats["asset_reuse_limit_reject_count"] = int(stats["asset_reuse_limit_reject_count"]) + 1
        return None
    return rng.choice(allowed)


def record_asset_reuse(
    entry: ProceduralAssetEntry,
    scene_model_counts: Counter[str],
    room_model_counts: Counter[str],
) -> None:
    scene_model_counts[entry.model_id] += 1
    room_model_counts[entry.model_id] += 1


def semantic_matches_filter(semantic: dict[str, Any], class_filter: dict[str, list[str]]) -> bool:
    for field_name, terms in class_filter.items():
        value = str(semantic.get(field_name, "")).lower()
        if not any(term in value for term in terms):
            return False
    return True


def entries_matching_profile_filter(
    entries: list[ProceduralAssetEntry],
    profile: dict[str, Any],
    class_name: str,
) -> tuple[list[ProceduralAssetEntry], bool]:
    class_filter = dict(profile.get("filters") or {}).get(class_name, {})
    if not class_filter:
        return entries, False
    filtered = [entry for entry in entries if semantic_matches_filter(entry.semantic, class_filter)]
    return (filtered or entries), bool(filtered)


def select_room_group_specs(room_type: str, placement_groups: dict[str, Any]) -> list[dict[str, Any]]:
    """Select relationship placement groups for a room type."""

    if not placement_groups.get("enabled", True):
        return []
    groups_by_room = dict(placement_groups.get("room_types") or {})
    if room_type in groups_by_room:
        return list(groups_by_room[room_type])
    lowered = room_type.lower()
    for name, specs in groups_by_room.items():
        if str(name).lower() == lowered:
            return list(specs)
    for name, specs in groups_by_room.items():
        name_lowered = str(name).lower()
        if name_lowered in lowered or lowered in name_lowered:
            return list(specs)
    return []


def count_class(classes: list[str], class_name: str) -> int:
    return sum(1 for item in classes if item == class_name)


def remove_class(classes: list[str], class_name: str, count: int = 1) -> None:
    for _index in range(count):
        classes.remove(class_name)


def companion_directions(count: int) -> list[tuple[float, float]]:
    if count <= 0:
        return []
    if count == 1:
        return [(0.0, -1.0)]
    if count == 2:
        return [(-1.0, 0.0), (1.0, 0.0)]
    if count == 3:
        return [(math.cos(index * math.tau / 3.0), math.sin(index * math.tau / 3.0)) for index in range(3)]
    cardinal = [(0.0, -1.0), (1.0, 0.0), (0.0, 1.0), (-1.0, 0.0)]
    if count <= 4:
        return cardinal[:count]
    return [(math.cos(index * math.tau / count), math.sin(index * math.tau / count)) for index in range(count)]


def rotate_direction(direction: tuple[float, float], yaw: float) -> tuple[float, float]:
    cos_yaw = math.cos(yaw)
    sin_yaw = math.sin(yaw)
    x, y = direction
    return (x * cos_yaw - y * sin_yaw, x * sin_yaw + y * cos_yaw)


def overlapping_interval(a0: float, a1: float, b0: float, b1: float) -> tuple[float, float] | None:
    start = max(a0, b0)
    end = min(a1, b1)
    if end - start <= 1e-6:
        return None
    return start, end


def append_wall_with_center_door(
    meshes: list[dict[str, object]],
    wall_specs: list[tuple[float, float, float, float, str]],
    *,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    door_width: float,
    horizontal: bool,
) -> None:
    length = (x1 - x0) if horizontal else (y1 - y0)
    gap = min(max(0.0, door_width), max(0.0, length - 2.0e-6))
    if gap <= 0.0:
        wall_specs.append((x0, y0, x1, y1, "Wall"))
        return
    if horizontal:
        gap_x0 = x0 + (length - gap) / 2.0
        gap_x1 = gap_x0 + gap
        if gap_x0 > x0:
            wall_specs.append((x0, y0, gap_x0, y1, "Wall"))
        if gap_x1 < x1:
            wall_specs.append((gap_x1, y0, x1, y1, "Wall"))
        door_vertices, door_faces = plane_mesh(gap_x0, y0, 0.0, gap_x1, y1)
        meshes.append(mesh_payload(f"door_horizontal_{len(meshes):04d}", "Door", door_vertices, door_faces))
        return
    gap_y0 = y0 + (length - gap) / 2.0
    gap_y1 = gap_y0 + gap
    if gap_y0 > y0:
        wall_specs.append((x0, y0, x1, gap_y0, "Wall"))
    if gap_y1 < y1:
        wall_specs.append((x0, gap_y1, x1, y1, "Wall"))
    door_vertices, door_faces = plane_mesh(x0, gap_y0, 0.0, x1, gap_y1)
    meshes.append(mesh_payload(f"door_vertical_{len(meshes):04d}", "Door", door_vertices, door_faces))


def room_adjacencies_for_rooms(
    rooms: list[ProceduralRoom],
    *,
    wall_thickness: float,
    door_width: float,
) -> list[dict[str, object]]:
    adjacencies: list[dict[str, object]] = []
    for left_index, left_room in enumerate(rooms):
        for right_room in rooms[left_index + 1 :]:
            if abs(left_room.x1 - right_room.x0) < 1e-6 or abs(right_room.x1 - left_room.x0) < 1e-6:
                x = left_room.x1 if abs(left_room.x1 - right_room.x0) < 1e-6 else right_room.x1
                interval = overlapping_interval(left_room.y0, left_room.y1, right_room.y0, right_room.y1)
                if interval is None:
                    continue
                start, end = interval
                length = end - start
                gap = min(max(0.0, door_width), max(0.0, length - 2.0e-6))
                gap_start = start + (length - gap) / 2.0
                gap_end = gap_start + gap
                adjacencies.append(
                    {
                        "rooms": [left_room.room_id, right_room.room_id],
                        "room_types": [left_room.room_type, right_room.room_type],
                        "orientation": "vertical",
                        "wall_axis": "x",
                        "wall_center_m": round(x, 6),
                        "shared_interval_m": [round(start, 6), round(end, 6)],
                        "door_width_m": round(gap, 6),
                        "door_bounds_xy": [
                            round(x - wall_thickness / 2.0, 6),
                            round(gap_start, 6),
                            round(x + wall_thickness / 2.0, 6),
                            round(gap_end, 6),
                        ],
                        "door_center_xy": [round(x, 6), round((gap_start + gap_end) / 2.0, 6)],
                    }
                )
            if abs(left_room.y1 - right_room.y0) < 1e-6 or abs(right_room.y1 - left_room.y0) < 1e-6:
                y = left_room.y1 if abs(left_room.y1 - right_room.y0) < 1e-6 else right_room.y1
                interval = overlapping_interval(left_room.x0, left_room.x1, right_room.x0, right_room.x1)
                if interval is None:
                    continue
                start, end = interval
                length = end - start
                gap = min(max(0.0, door_width), max(0.0, length - 2.0e-6))
                gap_start = start + (length - gap) / 2.0
                gap_end = gap_start + gap
                adjacencies.append(
                    {
                        "rooms": [left_room.room_id, right_room.room_id],
                        "room_types": [left_room.room_type, right_room.room_type],
                        "orientation": "horizontal",
                        "wall_axis": "y",
                        "wall_center_m": round(y, 6),
                        "shared_interval_m": [round(start, 6), round(end, 6)],
                        "door_width_m": round(gap, 6),
                        "door_bounds_xy": [
                            round(gap_start, 6),
                            round(y - wall_thickness / 2.0, 6),
                            round(gap_end, 6),
                            round(y + wall_thickness / 2.0, 6),
                        ],
                        "door_center_xy": [round((gap_start + gap_end) / 2.0, 6), round(y, 6)],
                    }
                )
    return adjacencies


def room_topology_metrics(rooms: list[ProceduralRoom], adjacencies: list[dict[str, object]]) -> dict[str, object]:
    room_ids = [room.room_id for room in rooms]
    graph: dict[str, set[str]] = {room_id: set() for room_id in room_ids}
    edge_count = 0
    for adjacency in adjacencies:
        room_pair = adjacency.get("rooms")
        if not isinstance(room_pair, list | tuple) or len(room_pair) != 2:
            continue
        left, right = (str(value) for value in room_pair)
        if left not in graph or right not in graph or left == right:
            continue
        if right not in graph[left]:
            edge_count += 1
        graph[left].add(right)
        graph[right].add(left)

    visited: set[str] = set()
    component_sizes: list[int] = []
    for room_id in room_ids:
        if room_id in visited:
            continue
        component = {room_id}
        frontier = [room_id]
        visited.add(room_id)
        while frontier:
            current = frontier.pop()
            for neighbor in graph[current] - visited:
                visited.add(neighbor)
                component.add(neighbor)
                frontier.append(neighbor)
        component_sizes.append(len(component))

    degrees = [len(graph[room_id]) for room_id in room_ids]
    graph_diameter = 0
    for start in room_ids:
        distances = {start: 0}
        frontier = [start]
        while frontier:
            current = frontier.pop(0)
            for neighbor in graph[current]:
                if neighbor in distances:
                    continue
                distances[neighbor] = distances[current] + 1
                frontier.append(neighbor)
        if distances:
            graph_diameter = max(graph_diameter, max(distances.values()))

    return {
        "room_count": len(room_ids),
        "edge_count": edge_count,
        "component_count": len(component_sizes),
        "component_sizes": sorted(component_sizes, reverse=True),
        "is_connected": len(component_sizes) <= 1,
        "min_degree": min(degrees) if degrees else 0,
        "max_degree": max(degrees) if degrees else 0,
        "mean_degree": rounded(sum(degrees) / len(degrees)) if degrees else 0.0,
        "leaf_room_count": sum(1 for degree in degrees if degree == 1),
        "isolated_room_count": sum(1 for degree in degrees if degree == 0),
        "branch_room_count": sum(1 for degree in degrees if degree >= 3),
        "graph_diameter": graph_diameter,
    }


def door_keepout_boxes_for_rooms(
    rooms: list[ProceduralRoom],
    adjacencies: list[dict[str, object]],
    clearance_m: float,
) -> dict[str, list[tuple[float, float, float, float, float, float]]]:
    """Build per-room XY keepout boxes around procedural door openings."""

    boxes: dict[str, list[tuple[float, float, float, float, float, float]]] = {room.room_id: [] for room in rooms}
    if clearance_m <= 0.0:
        return boxes
    room_by_id = {room.room_id: room for room in rooms}
    for adjacency in adjacencies:
        door_width = float(adjacency.get("door_width_m", 0.0))
        bounds = adjacency.get("door_bounds_xy")
        room_ids = adjacency.get("rooms")
        if door_width <= 0.0 or not isinstance(bounds, list | tuple) or len(bounds) != 4 or not isinstance(room_ids, list | tuple):
            continue
        x_min, y_min, x_max, y_max = (float(value) for value in bounds)
        if x_max <= x_min or y_max <= y_min:
            continue
        for room_id in room_ids:
            room = room_by_id.get(str(room_id))
            if room is None:
                continue
            boxes[room.room_id].append(
                (
                    x_min - clearance_m,
                    x_max + clearance_m,
                    y_min - clearance_m,
                    y_max + clearance_m,
                    0.0,
                    room.height,
                )
            )
    return boxes


def exterior_window_sides(
    room: ProceduralRoom,
    rooms: list[ProceduralRoom],
) -> list[str]:
    sides: list[str] = []
    segments = room_exterior_segments(room, rooms)
    if segments["south"] and sum(end - start for start, end in segments["south"]) >= room.width - 1e-6:
        sides.append("south")
    if segments["north"] and sum(end - start for start, end in segments["north"]) >= room.width - 1e-6:
        sides.append("north")
    if segments["west"] and sum(end - start for start, end in segments["west"]) >= room.length - 1e-6:
        sides.append("west")
    if segments["east"] and sum(end - start for start, end in segments["east"]) >= room.length - 1e-6:
        sides.append("east")
    return sides


def subtract_intervals(
    base: tuple[float, float],
    blockers: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    segments = [base]
    for blocker_start, blocker_end in sorted(blockers):
        next_segments: list[tuple[float, float]] = []
        for start, end in segments:
            if blocker_end <= start + 1e-6 or blocker_start >= end - 1e-6:
                next_segments.append((start, end))
                continue
            if blocker_start > start + 1e-6:
                next_segments.append((start, min(blocker_start, end)))
            if blocker_end < end - 1e-6:
                next_segments.append((max(blocker_end, start), end))
        segments = next_segments
        if not segments:
            break
    return [(round(start, 6), round(end, 6)) for start, end in segments if end - start > 1e-6]


def room_exterior_segments(
    room: ProceduralRoom,
    rooms: list[ProceduralRoom],
) -> dict[str, list[tuple[float, float]]]:
    blockers: dict[str, list[tuple[float, float]]] = {"south": [], "north": [], "west": [], "east": []}
    for other in rooms:
        if other.room_id == room.room_id:
            continue
        if abs(other.y1 - room.y0) < 1e-6:
            interval = overlapping_interval(room.x0, room.x1, other.x0, other.x1)
            if interval is not None:
                blockers["south"].append(interval)
        if abs(other.y0 - room.y1) < 1e-6:
            interval = overlapping_interval(room.x0, room.x1, other.x0, other.x1)
            if interval is not None:
                blockers["north"].append(interval)
        if abs(other.x1 - room.x0) < 1e-6:
            interval = overlapping_interval(room.y0, room.y1, other.y0, other.y1)
            if interval is not None:
                blockers["west"].append(interval)
        if abs(other.x0 - room.x1) < 1e-6:
            interval = overlapping_interval(room.y0, room.y1, other.y0, other.y1)
            if interval is not None:
                blockers["east"].append(interval)
    return {
        "south": subtract_intervals((room.x0, room.x1), blockers["south"]),
        "north": subtract_intervals((room.x0, room.x1), blockers["north"]),
        "west": subtract_intervals((room.y0, room.y1), blockers["west"]),
        "east": subtract_intervals((room.y0, room.y1), blockers["east"]),
    }


def append_exterior_wall_specs(
    wall_specs: list[tuple[float, float, float, float, str]],
    *,
    room: ProceduralRoom,
    rooms: list[ProceduralRoom],
    wall_thickness: float,
) -> None:
    segments = room_exterior_segments(room, rooms)
    for start, end in segments["south"]:
        wall_specs.append((start, room.y0, end, room.y0 + wall_thickness, "Wall"))
    for start, end in segments["north"]:
        wall_specs.append((start, room.y1 - wall_thickness, end, room.y1, "Wall"))
    for start, end in segments["west"]:
        wall_specs.append((room.x0, start, room.x0 + wall_thickness, end, "Wall"))
    for start, end in segments["east"]:
        wall_specs.append((room.x1 - wall_thickness, start, room.x1, end, "Wall"))


def append_room_windows(
    meshes: list[dict[str, object]],
    room_children: list[dict[str, object]],
    *,
    room: ProceduralRoom,
    room_index: int,
    sides: list[str],
    wall_thickness: float,
    config: dict[str, Any],
    rng: random.Random,
) -> int:
    if not config.get("enabled", True) or not sides or rng.random() > float(config["room_probability"]):
        return 0
    max_per_room = min(int(config["max_per_room"]), len(sides))
    if max_per_room <= 0:
        return 0
    rng.shuffle(sides)
    selected_sides = sides[:max_per_room]
    width_min, width_max = (float(value) for value in config["width_m"])
    height_min, height_max = (float(value) for value in config["height_m"])
    sill_min, sill_max = (float(value) for value in config["sill_height_m"])
    edge_margin = float(config["wall_margin_m"])
    created = 0
    for side in selected_sides:
        horizontal = side in {"south", "north"}
        side_length = room.width if horizontal else room.length
        if side_length <= 2.0 * edge_margin + width_min:
            continue
        width = min(rng.uniform(width_min, width_max), side_length - 2.0 * edge_margin)
        z0 = min(rng.uniform(sill_min, sill_max), max(0.0, room.height - height_min))
        z1 = min(room.height - 0.15, z0 + rng.uniform(height_min, height_max))
        if z1 <= z0:
            continue
        center = rng.uniform(edge_margin + width / 2.0, side_length - edge_margin - width / 2.0)
        if side == "south":
            vertices, faces = vertical_plane_mesh(
                constant_axis="y",
                constant_value=room.y0 + wall_thickness / 2.0,
                a0=room.x0 + center - width / 2.0,
                a1=room.x0 + center + width / 2.0,
                z0=z0,
                z1=z1,
            )
        elif side == "north":
            vertices, faces = vertical_plane_mesh(
                constant_axis="y",
                constant_value=room.y1 - wall_thickness / 2.0,
                a0=room.x0 + center - width / 2.0,
                a1=room.x0 + center + width / 2.0,
                z0=z0,
                z1=z1,
            )
        elif side == "west":
            vertices, faces = vertical_plane_mesh(
                constant_axis="x",
                constant_value=room.x0 + wall_thickness / 2.0,
                a0=room.y0 + center - width / 2.0,
                a1=room.y0 + center + width / 2.0,
                z0=z0,
                z1=z1,
            )
        else:
            vertices, faces = vertical_plane_mesh(
                constant_axis="x",
                constant_value=room.x1 - wall_thickness / 2.0,
                a0=room.y0 + center - width / 2.0,
                a1=room.y0 + center + width / 2.0,
                z0=z0,
                z1=z1,
            )
        uid = f"{room.room_id}/window_{created:02d}_{side}"
        meshes.append(mesh_payload(uid, "Window", vertices, faces))
        room_children[room_index]["children"].append(
            {"ref": uid, "instanceid": f"{uid}/instance", "pos": [0, 0, 0], "rot": [0, 0, 0, 1], "scale": [1, 1, 1]}
        )
        created += 1
    return created


def architecture_meshes_for_rooms(
    rooms: list[ProceduralRoom],
    wall_thickness: float,
    door_width: float,
    window_config: dict[str, Any] | None = None,
    rng: random.Random | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    meshes: list[dict[str, object]] = []
    room_children: list[dict[str, object]] = []
    if not rooms:
        return meshes, room_children
    height = max(room.height for room in rooms)

    for room_index, room in enumerate(rooms):
        floor_vertices, floor_faces = plane_mesh(room.x0, room.y0, 0.0, room.x1, room.y1)
        ceiling_vertices, ceiling_faces = plane_mesh(room.x0, room.y0, room.height, room.x1, room.y1)
        floor_uid = f"{room.room_id}/floor"
        ceiling_uid = f"{room.room_id}/ceiling"
        meshes.append(mesh_payload(floor_uid, "Floor", floor_vertices, floor_faces))
        meshes.append(mesh_payload(ceiling_uid, "Ceiling", ceiling_vertices, ceiling_faces))
        room_children.append(
            {
                "room_index": room_index,
                "children": [
                    {"ref": floor_uid, "instanceid": f"{floor_uid}/instance", "pos": [0, 0, 0], "rot": [0, 0, 0, 1], "scale": [1, 1, 1]},
                    {
                        "ref": ceiling_uid,
                        "instanceid": f"{ceiling_uid}/instance",
                        "pos": [0, 0, 0],
                        "rot": [0, 0, 0, 1],
                        "scale": [1, 1, 1],
                    },
                ],
            }
        )

    if window_config is not None and rng is not None:
        for room_index, room in enumerate(rooms):
            append_room_windows(
                meshes,
                room_children,
                room=room,
                room_index=room_index,
                sides=exterior_window_sides(room, rooms),
                wall_thickness=wall_thickness,
                config=window_config,
                rng=rng,
            )

    wall_specs: list[tuple[float, float, float, float, str]] = []
    for room in rooms:
        append_exterior_wall_specs(wall_specs, room=room, rooms=rooms, wall_thickness=wall_thickness)

    # Internal walls are generated only on true shared room edges. This supports split-tree
    # layouts where not every room boundary aligns to a global grid line.
    for left_index, left_room in enumerate(rooms):
        for right_room in rooms[left_index + 1 :]:
            if abs(left_room.x1 - right_room.x0) < 1e-6 or abs(right_room.x1 - left_room.x0) < 1e-6:
                x = left_room.x1 if abs(left_room.x1 - right_room.x0) < 1e-6 else right_room.x1
                interval = overlapping_interval(left_room.y0, left_room.y1, right_room.y0, right_room.y1)
                if interval is None:
                    continue
                y0, y1 = interval
                append_wall_with_center_door(
                    meshes,
                    wall_specs,
                    x0=x - wall_thickness / 2.0,
                    y0=y0,
                    x1=x + wall_thickness / 2.0,
                    y1=y1,
                    door_width=door_width,
                    horizontal=False,
                )
            if abs(left_room.y1 - right_room.y0) < 1e-6 or abs(right_room.y1 - left_room.y0) < 1e-6:
                y = left_room.y1 if abs(left_room.y1 - right_room.y0) < 1e-6 else right_room.y1
                interval = overlapping_interval(left_room.x0, left_room.x1, right_room.x0, right_room.x1)
                if interval is None:
                    continue
                x0, x1 = interval
                append_wall_with_center_door(
                    meshes,
                    wall_specs,
                    x0=x0,
                    y0=y - wall_thickness / 2.0,
                    x1=x1,
                    y1=y + wall_thickness / 2.0,
                    door_width=door_width,
                    horizontal=True,
                )

    for wall_index, (x0, y0, x1, y1, mesh_type) in enumerate(wall_specs):
        if x1 <= x0 or y1 <= y0:
            continue
        vertices, faces = box_mesh(x0, y0, 0.0, x1, y1, height)
        meshes.append(mesh_payload(f"wall_{wall_index:04d}", mesh_type, vertices, faces))
    return meshes, room_children


def write_procedural_source_files(
    scene_dir: Path,
    scene_id: str,
    rooms: list[ProceduralRoom],
    meshes: list[dict[str, object]],
    room_children: list[dict[str, object]],
    adjacencies: list[dict[str, object]] | None = None,
) -> tuple[Path, Path, Path, tuple[float, float, float], tuple[float, float, float]]:
    source_dir = scene_dir / "procedural_source"
    source_dir.mkdir(parents=True, exist_ok=True)
    architecture_obj = source_dir / "architecture.obj"
    source_json = source_dir / "scene.json"
    metadata_json = source_dir / "architecture.json"
    write_architecture_obj(architecture_obj, rooms, meshes)
    bbox_min = (
        min(min(float(value) for value in mesh["xyz"][0::3]) for mesh in meshes),
        min(min(float(value) for value in mesh["xyz"][1::3]) for mesh in meshes),
        min(min(float(value) for value in mesh["xyz"][2::3]) for mesh in meshes),
    )
    bbox_max = (
        max(max(float(value) for value in mesh["xyz"][0::3]) for mesh in meshes),
        max(max(float(value) for value in mesh["xyz"][1::3]) for mesh in meshes),
        max(max(float(value) for value in mesh["xyz"][2::3]) for mesh in meshes),
    )
    rooms_payload: list[dict[str, object]] = []
    children_by_index = {int(item["room_index"]): item["children"] for item in room_children}
    footprint = rooms_footprint_metrics(rooms)
    for index, room in enumerate(rooms):
        rooms_payload.append(
            {
                "type": room.room_type,
                "instanceid": room.room_id,
                "size": round(room.area, 3),
                "pos": [0, 0, 0],
                "rot": [0, 0, 0, 1],
                "scale": [1, 1, 1],
                "children": children_by_index.get(index, []),
            }
        )
    source_payload = {
        "uid": scene_id,
        "version": "scenegen.procedural_front3d.v1",
        "furniture": [],
        "mesh": meshes,
        "scene": {"room": rooms_payload},
        "procedural": {"footprint": footprint, "adjacency_count": len(adjacencies or []), "adjacency": adjacencies or []},
    }
    source_json.write_text(json.dumps(source_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata_payload = {
        "schema_version": "scenegen.procedural_front3d.v1",
        "dataset": "scenegen-procedural",
        "asset_kind": "architecture",
        "variant": "raw",
        "id": scene_id,
        "files": {"obj": portable_path(architecture_obj, find_project_root())},
        "geometry": {
            "bbox": {"min": list(bbox_min), "max": list(bbox_max)},
            "size": {"x": bbox_max[0] - bbox_min[0], "y": bbox_max[1] - bbox_min[1], "z": bbox_max[2] - bbox_min[2]},
        },
        "materials": {
            "sionna": ["itu-concrete", "itu-glass"],
            "source_to_sionna": [
                {"source": "floor", "sionna": "itu-concrete", "itu_type": "concrete", "confidence": "high"},
                {"source": "ceiling", "sionna": "itu-concrete", "itu_type": "concrete", "confidence": "high"},
                {"source": "wall", "sionna": "itu-concrete", "itu_type": "concrete", "confidence": "high"},
                {"source": "window", "sionna": "itu-glass", "itu_type": "glass", "confidence": "medium"},
            ],
        },
        "procedural": {
            "room_count": len(rooms),
            "footprint": footprint,
            "adjacency_count": len(adjacencies or []),
            "adjacency": adjacencies or [],
            "rooms": [
                {
                    "room_id": room.room_id,
                    "room_type": room.room_type,
                    "bounds_xy": [room.x0, room.y0, room.x1, room.y1],
                    "height_m": room.height,
                }
                for room in rooms
            ],
        },
    }
    metadata_json.write_text(json.dumps(metadata_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return architecture_obj, source_json, metadata_json, bbox_min, bbox_max


class ProceduralFront3DGenerator:
    def __init__(self, index: Front3DIndex, args: Any):
        self.index = index
        self.args = args
        self.repo_root = find_project_root()
        self.asset_pool = self._build_asset_pool()

    def _build_asset_pool(self) -> dict[str, list[ProceduralAssetEntry]]:
        pool: dict[str, list[ProceduralAssetEntry]] = {"table": [], "seat": [], "floor": []}
        max_assets = int(self.args.procedural_asset_pool_limit)
        for model_id in sorted(self.index.by_model_id):
            if all(len(items) >= max_assets for items in pool.values()):
                break
            payload = self.index.object_payload(model_id)
            if not payload:
                continue
            asset = self.index.object_asset(model_id)
            if asset is None or asset.placement_class not in pool:
                continue
            if asset.width <= 0 or asset.length <= 0 or asset.height <= 0:
                continue
            entry = ProceduralAssetEntry(
                model_id=model_id,
                asset=asset,
                payload=payload,
                semantic=dict(payload.get("semantic") or {}),
            )
            items = pool[asset.placement_class]
            if len(items) < max_assets:
                items.append(entry)
        return pool

    def _entries_for_room(self, room_type: str, class_name: str) -> list[ProceduralAssetEntry]:
        entries = self.asset_pool.get(class_name, [])
        if not entries:
            return []
        _profile_name, profile = select_room_profile_spec(room_type, self.args.procedural_room_profiles)
        filtered, _matched = entries_matching_profile_filter(entries, profile, class_name)
        return filtered

    def room_profile_for_room(self, room_type: str) -> tuple[str, list[str]]:
        return select_room_profile(room_type, self.args.procedural_room_profiles)

    def room_profile_detail_for_room(self, room_type: str) -> tuple[str, list[str], dict[str, dict[str, list[str]]]]:
        profile_name, profile = select_room_profile_spec(room_type, self.args.procedural_room_profiles)
        return profile_name, list(profile["classes"]), dict(profile.get("filters") or {})

    def desired_classes_for_room(self, room: ProceduralRoom, rng: random.Random) -> list[str]:
        object_count_config = object_count_config_for_room(room.room_type, self.args.procedural_object_count)
        target_count = object_count_for_room_area(room.area, object_count_config, rng)
        return desired_classes_from_profile(
            room.room_type,
            self.args.procedural_room_profiles,
            target_count,
            rng,
        )

    def _append_placement(
        self,
        placements: list[PlacedAsset],
        *,
        scene_index: int,
        room: ProceduralRoom,
        entry: ProceduralAssetEntry,
        matrix: tuple[float, ...],
        bbox: tuple[float, float, float, float, float, float],
        yaw: float,
        metadata: dict[str, object] | None = None,
    ) -> None:
        placement_index = len(placements)
        placement_metadata: dict[str, object] = {
            "generator": "procedural_front3d",
            "room_type": room.room_type,
            "category": entry.semantic.get("category"),
            "super_category": entry.semantic.get("super_category"),
            "material": entry.semantic.get("material"),
            "object_variant": self.index.config.object_variant,
        }
        if metadata:
            placement_metadata.update(metadata)
        placements.append(
            PlacedAsset(
                asset=entry.asset,
                instance_name=f"procedural_{scene_index:04d}_{placement_index:04d}_{entry.asset.export_name}",
                x=matrix[3],
                y=matrix[7],
                z=matrix[11],
                yaw=yaw,
                support_type="front3d_scene",
                parent=None,
                min_x=bbox[0],
                max_x=bbox[1],
                min_y=bbox[2],
                max_y=bbox[3],
                min_z=bbox[4],
                max_z=bbox[5],
                transform_matrix_4x4_row_major=matrix,
                source_ids={
                    "scene_id": f"procedural_{scene_index:04d}",
                    "room_instanceid": room.room_id,
                    "model_id": entry.model_id,
                },
                metadata=placement_metadata,
            )
        )

    def _try_place_group(
        self,
        room: ProceduralRoom,
        spec: dict[str, Any],
        companion_count: int,
        scene_index: int,
        rng: random.Random,
        room_boxes: list[tuple[float, float, float, float, float, float]],
        door_keepout_boxes: list[tuple[float, float, float, float, float, float]],
        placements: list[PlacedAsset],
        mesh_cache: dict[Path, list[Vec3]],
        scene_model_counts: Counter[str],
        room_model_counts: Counter[str],
        stats: dict[str, object],
    ) -> bool:
        anchor_class = str(spec["anchor_class"])
        companion_class = str(spec["companion_class"])
        anchor_candidates = self._entries_for_room(room.room_type, anchor_class)
        companion_candidates = self._entries_for_room(room.room_type, companion_class)
        if not anchor_candidates or companion_count > 0 and not companion_candidates:
            return False
        margin = float(self.args.procedural_wall_margin_m)
        object_margin = float(self.args.procedural_object_margin_m)
        max_attempts = int(spec["max_attempts"])
        gap_min, gap_max = (float(value) for value in spec["companion_gap_m"])
        for _attempt in range(max_attempts):
            stats["relation_group_attempt_count"] = int(stats["relation_group_attempt_count"]) + 1
            attempt_scene_counts = Counter(scene_model_counts)
            attempt_room_counts = Counter(room_model_counts)
            entry = choose_entry_with_reuse(
                anchor_candidates,
                room.room_id,
                attempt_scene_counts,
                attempt_room_counts,
                self.args.procedural_asset_reuse,
                rng,
                stats,
            )
            if entry is None:
                return False
            record_asset_reuse(entry, attempt_scene_counts, attempt_room_counts)
            policy = placement_policy_for_class(self.args.procedural_placement_policy, anchor_class, room.room_type)
            yaw, center_x, center_y, width, length, _zone = candidate_pose_for_policy(room, entry, policy, margin, rng)
            if width >= room.width - 2.0 * margin or length >= room.length - 2.0 * margin:
                stats["size_reject_count"] = int(stats["size_reject_count"]) + 1
                continue
            approx_bbox = procedural_asset_approx_bbox(entry, yaw, center_x, center_y)
            if any(boxes_overlap_xy(approx_bbox, keepout, 0.0) for keepout in door_keepout_boxes):
                stats["door_keepout_reject_count"] = int(stats["door_keepout_reject_count"]) + 1
                continue
            if any(boxes_overlap_xy(approx_bbox, other, object_margin) for other in room_boxes):
                stats["approx_collision_reject_count"] = int(stats["approx_collision_reject_count"]) + 1
                continue
            stats["exact_bbox_count"] = int(stats["exact_bbox_count"]) + 1
            matrix, bbox = procedural_asset_transform_for_center(entry, yaw, center_x, center_y, mesh_cache)
            if not room_contains_bbox(room, bbox, margin=0.0):
                stats["exact_room_reject_count"] = int(stats["exact_room_reject_count"]) + 1
                continue
            if any(boxes_overlap_xy(bbox, keepout, 0.0) for keepout in door_keepout_boxes):
                stats["door_keepout_reject_count"] = int(stats["door_keepout_reject_count"]) + 1
                continue
            if any(boxes_overlap_xy(bbox, other, object_margin) for other in room_boxes):
                stats["exact_collision_reject_count"] = int(stats["exact_collision_reject_count"]) + 1
                continue
            group_boxes = [bbox]
            group_items: list[tuple[ProceduralAssetEntry, tuple[float, ...], tuple[float, float, float, float, float, float], float, str]] = [
                (entry, matrix, bbox, yaw, "anchor")
            ]
            anchor_center_x = (bbox[0] + bbox[1]) / 2.0
            anchor_center_y = (bbox[2] + bbox[3]) / 2.0
            anchor_width = bbox[1] - bbox[0]
            anchor_length = bbox[3] - bbox[2]
            directions = [rotate_direction(direction, yaw) for direction in companion_directions(companion_count)]
            rng.shuffle(directions)
            group_failed = False
            for direction_x, direction_y in directions:
                companion = choose_entry_with_reuse(
                    companion_candidates,
                    room.room_id,
                    attempt_scene_counts,
                    attempt_room_counts,
                    self.args.procedural_asset_reuse,
                    rng,
                    stats,
                )
                if companion is None:
                    group_failed = True
                    break
                record_asset_reuse(companion, attempt_scene_counts, attempt_room_counts)
                companion_yaw = math.atan2(-direction_y, -direction_x)
                companion_width, companion_length = procedural_asset_footprint_size(companion, companion_yaw)
                anchor_half = abs(direction_x) * anchor_width / 2.0 + abs(direction_y) * anchor_length / 2.0
                companion_half = abs(direction_x) * companion_width / 2.0 + abs(direction_y) * companion_length / 2.0
                distance = anchor_half + companion_half + rng.uniform(gap_min, gap_max)
                companion_x = anchor_center_x + direction_x * distance
                companion_y = anchor_center_y + direction_y * distance
                companion_approx_bbox = procedural_asset_approx_bbox(companion, companion_yaw, companion_x, companion_y)
                if not room_contains_bbox(room, companion_approx_bbox, margin=0.0):
                    group_failed = True
                    break
                if any(boxes_overlap_xy(companion_approx_bbox, keepout, 0.0) for keepout in door_keepout_boxes):
                    stats["door_keepout_reject_count"] = int(stats["door_keepout_reject_count"]) + 1
                    group_failed = True
                    break
                if any(boxes_overlap_xy(companion_approx_bbox, other, object_margin) for other in [*room_boxes, *group_boxes]):
                    group_failed = True
                    break
                stats["exact_bbox_count"] = int(stats["exact_bbox_count"]) + 1
                companion_matrix, companion_bbox = procedural_asset_transform_for_center(
                    companion,
                    companion_yaw,
                    companion_x,
                    companion_y,
                    mesh_cache,
                )
                if not room_contains_bbox(room, companion_bbox, margin=0.0):
                    group_failed = True
                    break
                if any(boxes_overlap_xy(companion_bbox, keepout, 0.0) for keepout in door_keepout_boxes):
                    stats["door_keepout_reject_count"] = int(stats["door_keepout_reject_count"]) + 1
                    group_failed = True
                    break
                if any(boxes_overlap_xy(companion_bbox, other, object_margin) for other in [*room_boxes, *group_boxes]):
                    group_failed = True
                    break
                group_boxes.append(companion_bbox)
                group_items.append((companion, companion_matrix, companion_bbox, companion_yaw, "companion"))
            if group_failed:
                continue
            group_index = int(stats["relation_group_success_count"])
            group_id = f"{room.room_id}/{spec['name']}/{group_index:02d}"
            for item_entry, item_matrix, item_bbox, item_yaw, role in group_items:
                self._append_placement(
                    placements,
                    scene_index=scene_index,
                    room=room,
                    entry=item_entry,
                    matrix=item_matrix,
                    bbox=item_bbox,
                    yaw=item_yaw,
                    metadata={
                        "placement_group": spec["name"],
                        "placement_group_id": group_id,
                        "placement_group_role": role,
                    },
                )
            scene_model_counts.clear()
            scene_model_counts.update(attempt_scene_counts)
            room_model_counts.clear()
            room_model_counts.update(attempt_room_counts)
            placed_counts = stats.get("placed_object_counts")
            if isinstance(placed_counts, dict):
                placed_counts[room.room_id] = int(placed_counts.get(room.room_id, 0)) + len(group_items)
            for item_entry, _item_matrix, _item_bbox, _item_yaw, _role in group_items:
                _record_placed_asset_stats(stats, room, item_entry)
            room_boxes.extend(group_boxes)
            stats["relation_group_success_count"] = group_index + 1
            stats["relation_group_placement_count"] = int(stats["relation_group_placement_count"]) + len(group_items)
            group_counts = stats["relation_group_name_counts"]
            if isinstance(group_counts, dict):
                group_counts[str(spec["name"])] = int(group_counts.get(str(spec["name"]), 0)) + 1
            return True
        return False

    def _place_room_groups(
        self,
        room: ProceduralRoom,
        desired_classes: list[str],
        scene_index: int,
        rng: random.Random,
        room_boxes: list[tuple[float, float, float, float, float, float]],
        door_keepout_boxes: list[tuple[float, float, float, float, float, float]],
        placements: list[PlacedAsset],
        mesh_cache: dict[Path, list[Vec3]],
        scene_model_counts: Counter[str],
        room_model_counts: Counter[str],
        stats: dict[str, object],
    ) -> None:
        for spec in select_room_group_specs(room.room_type, self.args.procedural_placement_groups):
            anchor_class = str(spec["anchor_class"])
            companion_class = str(spec["companion_class"])
            if count_class(desired_classes, anchor_class) < 1:
                continue
            min_companions, max_companions = (int(value) for value in spec["companion_count"])
            available_companions = count_class(desired_classes, companion_class)
            if anchor_class == companion_class:
                available_companions -= 1
            if available_companions < min_companions:
                continue
            companion_count = rng.randint(min_companions, min(max_companions, available_companions))
            placed = self._try_place_group(
                room,
                spec,
                companion_count,
                scene_index,
                rng,
                room_boxes,
                door_keepout_boxes,
                placements,
                mesh_cache,
                scene_model_counts,
                room_model_counts,
                stats,
            )
            if not placed:
                stats["relation_group_failure_count"] = int(stats["relation_group_failure_count"]) + 1
                continue
            remove_class(desired_classes, anchor_class)
            remove_class(desired_classes, companion_class, companion_count)

    def place_assets(
        self,
        rooms: list[ProceduralRoom],
        adjacencies: list[dict[str, object]],
        scene_index: int,
        rng: random.Random,
    ) -> tuple[list[PlacedAsset], list[dict[str, object]], dict[str, object]]:
        placements: list[PlacedAsset] = []
        skipped: list[dict[str, object]] = []
        desired_object_counts: dict[str, int] = {}
        placed_object_counts: dict[str, int] = {}
        skipped_object_counts: dict[str, int] = {}
        stats: dict[str, object] = {
            "attempt_count": 0,
            "exact_bbox_count": 0,
            "size_reject_count": 0,
            "approx_collision_reject_count": 0,
            "exact_room_reject_count": 0,
            "exact_collision_reject_count": 0,
            "relation_group_attempt_count": 0,
            "relation_group_success_count": 0,
            "relation_group_failure_count": 0,
            "relation_group_placement_count": 0,
            "relation_group_name_counts": {},
            "desired_object_counts": desired_object_counts,
            "placed_object_counts": placed_object_counts,
            "skipped_object_counts": skipped_object_counts,
            "desired_class_counts": {},
            "placed_class_counts": {},
            "skipped_class_counts": {},
            "placed_room_type_counts": {},
            "skipped_reason_counts": {},
            "desired_class_by_room_type": {},
            "placed_class_by_room_type": {},
            "skipped_class_by_room_type": {},
            "policy_zone_counts": {},
            "asset_reuse_relaxed_count": 0,
            "asset_reuse_limit_reject_count": 0,
            "door_keepout_clearance_m": float(self.args.procedural_door_clearance_m),
            "door_keepout_box_count": 0,
            "door_keepout_reject_count": 0,
        }
        room_boxes: dict[str, list[tuple[float, float, float, float, float, float]]] = {room.room_id: [] for room in rooms}
        door_keepout_boxes = door_keepout_boxes_for_rooms(rooms, adjacencies, float(self.args.procedural_door_clearance_m))
        stats["door_keepout_box_count"] = sum(len(boxes) for boxes in door_keepout_boxes.values())
        room_model_counts: dict[str, Counter[str]] = {room.room_id: Counter() for room in rooms}
        scene_model_counts: Counter[str] = Counter()
        mesh_cache: dict[Path, list[Vec3]] = {}
        for room in rooms:
            desired_classes = self.desired_classes_for_room(room, rng)
            desired_object_counts[room.room_id] = len(desired_classes)
            placed_object_counts[room.room_id] = 0
            skipped_object_counts[room.room_id] = 0
            for class_name in desired_classes:
                _record_desired_class_stats(stats, room, class_name)
            self._place_room_groups(
                room,
                desired_classes,
                scene_index,
                rng,
                room_boxes[room.room_id],
                door_keepout_boxes[room.room_id],
                placements,
                mesh_cache,
                scene_model_counts,
                room_model_counts[room.room_id],
                stats,
            )
            for class_name in desired_classes:
                candidates = self._entries_for_room(room.room_type, class_name)
                if not candidates:
                    skipped.append({"room_id": room.room_id, "class": class_name, "reason": "empty_asset_pool"})
                    skipped_object_counts[room.room_id] = int(skipped_object_counts.get(room.room_id, 0)) + 1
                    _record_skipped_class_stats(stats, room, class_name, "empty_asset_pool")
                    continue
                placed = False
                for _attempt in range(int(self.args.procedural_max_attempts_per_object)):
                    stats["attempt_count"] = int(stats["attempt_count"]) + 1
                    entry = choose_entry_with_reuse(
                        candidates,
                        room.room_id,
                        scene_model_counts,
                        room_model_counts[room.room_id],
                        self.args.procedural_asset_reuse,
                        rng,
                        stats,
                    )
                    if entry is None:
                        break
                    margin = float(self.args.procedural_wall_margin_m)
                    policy = placement_policy_for_class(self.args.procedural_placement_policy, class_name, room.room_type)
                    yaw, center_x, center_y, width, length, zone = candidate_pose_for_policy(room, entry, policy, margin, rng)
                    if width >= room.width - 2.0 * margin or length >= room.length - 2.0 * margin:
                        stats["size_reject_count"] = int(stats["size_reject_count"]) + 1
                        continue
                    approx_bbox = procedural_asset_approx_bbox(entry, yaw, center_x, center_y)
                    if any(boxes_overlap_xy(approx_bbox, keepout, 0.0) for keepout in door_keepout_boxes[room.room_id]):
                        stats["door_keepout_reject_count"] = int(stats["door_keepout_reject_count"]) + 1
                        continue
                    if any(
                        boxes_overlap_xy(approx_bbox, other, float(self.args.procedural_object_margin_m))
                        for other in room_boxes[room.room_id]
                    ):
                        stats["approx_collision_reject_count"] = int(stats["approx_collision_reject_count"]) + 1
                        continue
                    stats["exact_bbox_count"] = int(stats["exact_bbox_count"]) + 1
                    matrix, bbox = procedural_asset_transform_for_center(entry, yaw, center_x, center_y, mesh_cache)
                    if not room_contains_bbox(room, bbox, margin=0.0):
                        stats["exact_room_reject_count"] = int(stats["exact_room_reject_count"]) + 1
                        continue
                    if any(boxes_overlap_xy(bbox, keepout, 0.0) for keepout in door_keepout_boxes[room.room_id]):
                        stats["door_keepout_reject_count"] = int(stats["door_keepout_reject_count"]) + 1
                        continue
                    if any(boxes_overlap_xy(bbox, other, float(self.args.procedural_object_margin_m)) for other in room_boxes[room.room_id]):
                        stats["exact_collision_reject_count"] = int(stats["exact_collision_reject_count"]) + 1
                        continue
                    self._append_placement(
                        placements,
                        scene_index=scene_index,
                        room=room,
                        entry=entry,
                        matrix=matrix,
                        bbox=bbox,
                        yaw=yaw,
                    )
                    record_asset_reuse(entry, scene_model_counts, room_model_counts[room.room_id])
                    placed_object_counts[room.room_id] = int(placed_object_counts.get(room.room_id, 0)) + 1
                    _record_placed_asset_stats(stats, room, entry)
                    room_boxes[room.room_id].append(bbox)
                    zone_counts = stats["policy_zone_counts"]
                    if isinstance(zone_counts, dict):
                        zone_counts[zone] = int(zone_counts.get(zone, 0)) + 1
                    placed = True
                    break
                if not placed:
                    skipped.append({"room_id": room.room_id, "class": class_name, "reason": "placement_failed"})
                    skipped_object_counts[room.room_id] = int(skipped_object_counts.get(room.room_id, 0)) + 1
                    _record_skipped_class_stats(stats, room, class_name, "placement_failed")
        return placements, skipped, stats

    def build_scene(self, scene_dir: Path, scene_index: int, rng: random.Random) -> ProceduralSceneBuild:
        total_start = time.perf_counter()
        timings: dict[str, float] = {}
        stage_start = time.perf_counter()
        configured_layout = str(self.args.procedural_layout)
        selected_layout = choose_procedural_layout(configured_layout, dict(self.args.procedural_layout_weights), rng)
        rooms = make_room_layout(
            rng,
            selected_layout,
            tuple(int(value) for value in self.args.procedural_room_count),
            tuple(float(value) for value in self.args.procedural_room_width_m),
            tuple(float(value) for value in self.args.procedural_room_length_m),
            tuple(float(value) for value in self.args.procedural_room_height_m),
            tuple(str(value) for value in self.args.procedural_room_types),
            required_room_types=dict(self.args.procedural_required_room_types),
            room_type_assignment=str(self.args.procedural_room_type_assignment),
            room_type_area_priority=tuple(str(value) for value in self.args.procedural_room_type_area_priority),
            room_type_max_counts=dict(self.args.procedural_room_type_max_counts),
            room_type_weights=dict(self.args.procedural_room_type_weights),
            room_type_geometry_rules=dict(self.args.procedural_precheck_room_type_geometry),
        )
        timings["layout"] = time.perf_counter() - stage_start
        stage_start = time.perf_counter()
        meshes, room_children = architecture_meshes_for_rooms(
            rooms,
            wall_thickness=float(self.args.procedural_wall_thickness_m),
            door_width=float(self.args.procedural_door_width_m),
            window_config=dict(self.args.procedural_windows),
            rng=rng,
        )
        adjacencies = room_adjacencies_for_rooms(
            rooms,
            wall_thickness=float(self.args.procedural_wall_thickness_m),
            door_width=float(self.args.procedural_door_width_m),
        )
        scene_id = f"procedural_{scene_index:04d}_{rng.randrange(1, 2**31):08x}"
        timings["architecture_mesh"] = time.perf_counter() - stage_start
        stage_start = time.perf_counter()
        architecture_obj, source_json, metadata_json, bbox_min, bbox_max = write_procedural_source_files(
            scene_dir,
            scene_id,
            rooms,
            meshes,
            room_children,
            adjacencies,
        )
        timings["write_procedural_source"] = time.perf_counter() - stage_start
        base_scene = Front3DBaseScene(
            scene_id=scene_id,
            scene_obj=architecture_obj,
            source_scene_json=source_json,
            metadata_json=metadata_json,
            bbox_min=bbox_min,
            bbox_max=bbox_max,
            source_bbox_min=bbox_min,
            source_bbox_max=bbox_max,
            world_offset=(0.0, 0.0, 0.0),
            source_to_sionna_material={"floor": "itu-concrete", "ceiling": "itu-concrete", "wall": "itu-concrete", "window": "itu-glass"},
            sionna_material_names=("itu-concrete", "itu-glass"),
        )
        stage_start = time.perf_counter()
        placements, skipped, placement_stats = self.place_assets(rooms, adjacencies, scene_index, rng)
        timings["placement"] = time.perf_counter() - stage_start
        timings["total"] = time.perf_counter() - total_start
        report = {
            "scene_id": scene_id,
            "layout": selected_layout,
            "configured_layout": configured_layout,
            "layout_weights": dict(self.args.procedural_layout_weights),
            "room_count": len(rooms),
            "footprint": rooms_footprint_metrics(rooms),
            "topology": room_topology_metrics(rooms, adjacencies),
            "adjacency_count": len(adjacencies),
            "adjacency": adjacencies,
            "window_count": sum(1 for mesh in meshes if str(mesh["type"]).lower() == "window"),
            "rooms": [
                self._room_report(
                    room,
                    desired_object_count=dict(placement_stats.get("desired_object_counts") or {}).get(room.room_id),
                )
                for room in rooms
            ],
            "asset_pool_counts": {key: len(value) for key, value in sorted(self.asset_pool.items())},
            "skipped_object_count": len(skipped),
            "skipped_objects": skipped,
            "placement_stats": placement_stats,
            "timings_s": {key: round(value, 6) for key, value in timings.items()},
        }
        return ProceduralSceneBuild(
            scene_id=scene_id,
            base_scene=base_scene,
            rooms=rooms,
            placements=placements,
            skipped_objects=skipped,
            generation_report=report,
        )

    def _room_report(self, room: ProceduralRoom, desired_object_count: int | None = None) -> dict[str, object]:
        profile_name, profile_classes, profile_filters = self.room_profile_detail_for_room(room.room_type)
        min_side = min(room.width, room.length)
        max_side = max(room.width, room.length)
        return {
            "room_id": room.room_id,
            "room_type": room.room_type,
            "profile": profile_name,
            "profile_classes": profile_classes,
            "profile_filters": profile_filters,
            "desired_object_count": desired_object_count,
            "bounds_xy": [room.x0, room.y0, room.x1, room.y1],
            "size_xy_m": [round(room.width, 3), round(room.length, 3)],
            "area_m2": round(room.area, 3),
            "aspect_ratio": round(max_side / min_side, 3) if min_side > 0 else None,
        }
