from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from .floorplan import (
    dilate_binary_image,
    draw_front3d_mesh_projection,
    floorplan_layer_filename,
    front3d_mesh_type_is_wall,
    front3d_opening_kind,
)
from .front3d import apply_world_offset, read_json, scenegen_transform_for_child
from .geometry import point_in_triangle_2d, transform_point_with_matrix, triangle_area_and_up_normal
from .models import (
    BistroBaseScene,
    Front3DBaseScene,
    Front3DOpeningConfig,
    PlacedAsset,
    Rect2D,
    Room,
    SupportTriangle,
    Vec3,
)
from .paths import portable_path


@dataclass(frozen=True)
class LabelConfig:
    enabled: bool
    version: str
    ue_height_m: float
    sampling_domain: str
    ue_strategy: str
    grid_resolution_m: float
    sampling_mask_resolution_m: float
    batch_strategies: tuple[str, ...]
    batch_grid_resolutions_m: tuple[float, ...]
    ue_clearance_m: float
    obstacle_strategy: str
    walk_ignore_low_obstacles_below_m: float
    walk_blocking_classes: tuple[str, ...]
    walk_min_component_area_m2: float
    bs_strategy: str
    bs_count_strategy: str
    bs_per_room: int
    bs_min_per_room: int
    bs_max_per_room: int
    bs_min_room_area_m2: float
    bs_area_per_point_m2: float
    bs_height_m: float
    bs_ceiling_margin_m: float
    bs_wall_clearance_m: float
    bs_center_enabled: bool
    bs_center_initial_radius_m: float
    bs_center_radius_step_m: float
    bs_center_max_radius_m: float
    wall_clearance_m: float
    corridor_room_id: str
    corridor_room_type: str
    corridor_clearance_m: float
    overlay_enabled: bool
    fail_on_error: bool
    openings: Front3DOpeningConfig

    @classmethod
    def from_mapping(cls, payload: dict[str, Any], openings: dict[str, Any] | Front3DOpeningConfig | None = None) -> LabelConfig:
        ue = payload["ue"]
        bs = payload["bs"]
        sampling = ue["sampling"]
        walk = ue["walk"]
        count = bs["count"]
        center = bs["center"]
        connected_area = ue["connected_area"]
        overlay = payload["overlay"]
        opening_config = opening_config_from_mapping(openings)
        batch_strategies = tuple(label_strategy_to_internal(value) for value in sampling["strategies"])
        batch_grid_resolutions_m = tuple(float(value) for value in sampling["grid_m"])
        return cls(
            enabled=bool(payload["enabled"]),
            version="1.1",
            ue_height_m=float(ue["height_m"]),
            sampling_domain=str(sampling["domain"]),
            ue_strategy=batch_strategies[0],
            grid_resolution_m=batch_grid_resolutions_m[0],
            sampling_mask_resolution_m=float(sampling["mask_resolution_m"]),
            batch_strategies=batch_strategies,
            batch_grid_resolutions_m=batch_grid_resolutions_m,
            ue_clearance_m=float(walk["furniture_clearance_m"]),
            obstacle_strategy=str(walk["obstacle_strategy"]),
            walk_ignore_low_obstacles_below_m=float(walk["ignore_low_obstacles_below_m"]),
            walk_blocking_classes=tuple(str(value) for value in walk["blocking_classes"]),
            walk_min_component_area_m2=float(sampling["min_component_area_m2"]),
            bs_strategy=str(bs["strategy"]),
            bs_count_strategy=str(count["strategy"]),
            bs_per_room=int(count["per_room"]),
            bs_min_per_room=int(count["min_per_room"]),
            bs_max_per_room=int(count["max_per_room"]),
            bs_min_room_area_m2=float(count["min_room_area_m2"]),
            bs_area_per_point_m2=float(count["area_per_point_m2"]),
            bs_height_m=float(bs["height_m"]),
            bs_ceiling_margin_m=float(bs["ceiling_margin_m"]),
            bs_wall_clearance_m=float(bs["wall_clearance_m"]),
            bs_center_enabled=bool(center["enabled"]),
            bs_center_initial_radius_m=float(center["initial_radius_m"]),
            bs_center_radius_step_m=float(center["radius_step_m"]),
            bs_center_max_radius_m=float(center["max_radius_m"]),
            wall_clearance_m=float(sampling["wall_clearance_m"]),
            corridor_room_id=str(connected_area["room_id"]),
            corridor_room_type=str(connected_area["room_type"]),
            corridor_clearance_m=float(sampling["wall_clearance_m"]),
            overlay_enabled=bool(overlay["enabled"]),
            fail_on_error=bool(payload["fail_on_error"]),
            openings=opening_config,
        )


def label_strategy_to_internal(value: object) -> str:
    strategy = str(value).strip()
    if strategy == "panel":
        return "plane_grid"
    if strategy == "walk":
        return "free_space_grid"
    raise ValueError("label.ue.sampling.strategies values must be 'panel' or 'walk'")


def opening_config_from_mapping(payload: dict[str, Any] | Front3DOpeningConfig | None) -> Front3DOpeningConfig:
    if payload is None:
        return Front3DOpeningConfig()
    if isinstance(payload, Front3DOpeningConfig):
        return payload
    return Front3DOpeningConfig(
        mode=str(payload["mode"]),
        dilation_m=float(payload["dilation_m"]),
        floor_tolerance_m=float(payload["floor_tolerance_m"]),
        min_height_m=float(payload["min_height_m"]),
        include_doors_as_wall=bool(payload["include_doors_as_wall"]),
        include_windows_as_wall=bool(payload["include_windows_as_wall"]),
    )


@dataclass(frozen=True)
class RoomLabelContext:
    room_index: int
    room_id: str
    room_type: str
    floor_source: str
    floor_triangles: tuple[SupportTriangle, ...]
    floor_z: float
    ceiling_z: float | None
    bounds_xy: Rect2D
    obstacles: tuple[LabelObstacle, ...]
    boundary_clearance_m: float = 0.0
    is_corridor: bool = False


@dataclass(frozen=True)
class LabelObstacle:
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    min_z: float
    max_z: float
    source: str
    placement_class: str = "floor"


@dataclass(frozen=True)
class UeSamplingResult:
    points: list[tuple[float, float]]
    stats: dict[str, object]

    def __iter__(self):
        return iter(self.points)

    def __contains__(self, value: object) -> bool:
        return value in self.points

    def __len__(self) -> int:
        return len(self.points)


@dataclass(frozen=True)
class Front3DGlobalSamplingMask:
    resolution_m: float
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    width_px: int
    height_px: int
    indoor_layer: np.ndarray
    door_layer: np.ndarray
    wall_layer: np.ndarray
    panel_layer: np.ndarray
    stats: dict[str, object]


@dataclass
class LabelGenerationCache:
    front3d_global_masks: dict[tuple[object, ...], Front3DGlobalSamplingMask]
    front3d_room_contexts: dict[
        tuple[object, ...],
        tuple[tuple[RoomLabelContext, ...], RoomLabelContext | None, tuple[dict[str, object], ...]],
    ]

    def __init__(self) -> None:
        self.front3d_global_masks = {}
        self.front3d_room_contexts = {}


@dataclass(frozen=True)
class LabelVariant:
    name: str
    config: LabelConfig


def elapsed_s(start: float) -> float:
    return round(time.perf_counter() - start, 6)


def point_payload(
    *,
    x: float,
    y: float,
    z: float,
    label: str,
    room_id: str,
    room_type: str,
    strategy: str,
) -> dict[str, object]:
    return {
        "x": round(float(x), 3),
        "y": round(float(y), 3),
        "z": round(float(z), 3),
        "label": label,
        "room_id": room_id,
        "room_type": room_type,
        "strategy": strategy,
    }


def bs_point_payload(
    *,
    x: float,
    y: float,
    z: float,
    label: str,
    room_id: str,
    room_type: str,
    strategy: str,
) -> dict[str, object]:
    return {
        "position": [round(float(x), 3), round(float(y), 3), round(float(z), 3)],
        "label": label,
        "room_id": room_id,
        "room_type": room_type,
        "strategy": strategy,
    }


def point_position(point: dict[str, object]) -> tuple[float, float, float]:
    position = point.get("position")
    if isinstance(position, (list, tuple)) and len(position) >= 3:
        return float(position[0]), float(position[1]), float(position[2])
    return float(point["x"]), float(point["y"]), float(point["z"])


def positions_from_points(points: list[dict[str, object]]) -> list[list[float]]:
    return [[x, y, z] for x, y, z in (point_position(point) for point in points)]


def make_label_payload(
    *,
    scene_file: Path,
    path_root: Path,
    label_version: str,
    generator: str,
    groups: list[dict[str, object]],
    scene_id: str | None = None,
) -> dict[str, object]:
    bs_points: list[dict[str, object]] = []
    ue_points: list[dict[str, object]] = []
    for group in groups:
        bs_points.extend(group.get("bs_points", []))  # type: ignore[arg-type]
        ue_points.extend(group.get("ue_points", []))  # type: ignore[arg-type]
    payload: dict[str, object] = {
        "label_version": label_version,
        "generator": generator,
        "scene_file": portable_path(scene_file, path_root),
        "bs_points": bs_points,
        "ue_points": ue_points,
        "bs_positions": positions_from_points(bs_points),
        "ue_positions": positions_from_points(ue_points),
        "groups": groups,
    }
    if scene_id is not None:
        payload["scene_id"] = scene_id
    return payload


def group_with_positions(group: dict[str, object]) -> dict[str, object]:
    bs_points = list(group.get("bs_points", []))
    ue_points = list(group.get("ue_points", []))
    group["bs_positions"] = positions_from_points(bs_points)  # type: ignore[arg-type]
    group["ue_positions"] = positions_from_points(ue_points)  # type: ignore[arg-type]
    return group


def report_payload(
    *,
    mode: str,
    ok: bool,
    rooms: list[dict[str, object]],
    error_count: int = 0,
    warning_count: int = 0,
    skipped_room_count: int = 0,
) -> dict[str, object]:
    return {
        "ok": ok,
        "mode": mode,
        "error_count": error_count,
        "warning_count": warning_count,
        "room_count": len(rooms),
        "skipped_room_count": skipped_room_count,
        "rooms": rooms,
    }


def label_report_summary(payload: dict[str, object], report: dict[str, object]) -> dict[str, object]:
    rooms = [room for room in report.get("rooms", []) if isinstance(room, dict)]
    sampling_totals: dict[str, int] = {}
    for room in rooms:
        sampling = room.get("ue_sampling")
        if not isinstance(sampling, dict):
            continue
        for key, value in sampling.items():
            if isinstance(value, int):
                sampling_totals[key] = sampling_totals.get(key, 0) + value
    enriched = dict(report)
    bs_count = len(payload.get("bs_points", []))
    ue_count = len(payload.get("ue_points", []))
    enriched.update(
        {
            "group_count": len(payload.get("groups", [])),
            "bs_count": bs_count,
            "ue_count": ue_count,
            "point_count": bs_count + ue_count,
            "valid_room_count": sum(1 for room in rooms if not room.get("skipped")),
        }
    )
    if sampling_totals:
        enriched["ue_sampling_summary"] = sampling_totals
    return enriched


def write_label_outputs(
    scene_dir: Path,
    payload: dict[str, object],
    report: dict[str, object],
    path_root: Path,
) -> dict[str, object]:
    _ = (scene_dir, path_root)
    report = label_report_summary(payload, report)
    record = {
        "ok": bool(report["ok"]),
        "group_count": len(payload.get("groups", [])),
        "bs_count": len(payload.get("bs_points", [])),
        "ue_count": len(payload.get("ue_points", [])),
        "error_count": int(report.get("error_count", 0)),
        "warning_count": int(report.get("warning_count", 0)),
        "_payload": payload,
        "_report": report,
    }
    if isinstance(report.get("timings_s"), dict):
        record["timings_s"] = report["timings_s"]
    return record


def height_token(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    if not text:
        text = "0"
    return text.replace("-", "m").replace(".", "p")


def label_strategy_name(strategy: str) -> str:
    if strategy == "plane_grid":
        return "panel"
    if strategy == "free_space_grid":
        return "walk"
    raise ValueError(f"Unsupported label strategy: {strategy}")


def label_variants(config: LabelConfig) -> list[LabelVariant]:
    variants: list[LabelVariant] = []
    seen: set[str] = set()
    for strategy in config.batch_strategies:
        for resolution in config.batch_grid_resolutions_m:
            name = f"label_{label_strategy_name(strategy)}_{height_token(resolution)}"
            if name in seen:
                continue
            seen.add(name)
            variants.append(
                LabelVariant(
                    name=name,
                    config=replace(
                        config,
                        ue_strategy=strategy,
                        grid_resolution_m=resolution,
                    ),
                )
            )
    if not variants:
        variants.append(
            LabelVariant(
                name=f"label_{label_strategy_name(config.ue_strategy)}_{height_token(config.grid_resolution_m)}",
                config=config,
            )
        )
    return variants


def generate_generated_label(
    scene_dir: Path,
    room: Room,
    config: LabelConfig,
    rng: random.Random,
    path_root: Path,
) -> dict[str, object]:
    bs_z = max(0.0, min(config.bs_height_m, room.height - config.bs_ceiling_margin_m))
    bs_points = [
        bs_point_payload(
            x=0.5,
            y=0.5,
            z=bs_z,
            label="BS_0_0",
            room_id="generated_room",
            room_type="generated",
            strategy="corner",
        ),
        bs_point_payload(
            x=room.width - 0.5,
            y=0.5,
            z=bs_z,
            label="BS_0_1",
            room_id="generated_room",
            room_type="generated",
            strategy="corner",
        ),
        bs_point_payload(
            x=room.width - 0.5,
            y=room.length - 0.5,
            z=bs_z,
            label="BS_0_2",
            room_id="generated_room",
            room_type="generated",
            strategy="corner",
        ),
        bs_point_payload(
            x=0.5,
            y=room.length - 0.5,
            z=bs_z,
            label="BS_0_3",
            room_id="generated_room",
            room_type="generated",
            strategy="corner",
        ),
    ][: config.bs_per_room]
    ue_points = [
        point_payload(
            x=rng.uniform(1.0, max(1.0, room.width - 1.0)),
            y=rng.uniform(1.0, max(1.0, room.length - 1.0)),
            z=config.ue_height_m,
            label=f"UE_0_{index}",
            room_id="generated_room",
            room_type="generated",
            strategy="generated_random",
        )
        for index in range(12)
    ]
    group = group_with_positions(
        {
            "name": "generated_room",
            "room_id": "generated_room",
            "room_type": "generated",
            "strategy": "generated_default",
            "validation": {"ok": True, "errors": [], "warnings": []},
            "bs_points": bs_points,
            "ue_points": ue_points,
        }
    )
    payload = make_label_payload(
        scene_file=scene_dir / "scene.obj",
        path_root=scene_dir,
        label_version=config.version,
        generator="generated_auto",
        groups=[group],
    )
    report = report_payload(
        mode="generated",
        ok=True,
        rooms=[{"room_id": "generated_room", "ue_count": len(ue_points), "bs_count": len(bs_points), "ok": True}],
    )
    return write_label_outputs(scene_dir, payload, report, path_root)


def upgrade_point(
    point: dict[str, object],
    *,
    room_id: str,
    room_type: str,
    strategy: str,
) -> dict[str, object]:
    upgraded = dict(point)
    upgraded.setdefault("room_id", room_id)
    upgraded.setdefault("room_type", room_type)
    upgraded.setdefault("strategy", strategy)
    return upgraded


def upgrade_bs_point(
    point: dict[str, object],
    *,
    room_id: str,
    room_type: str,
    strategy: str,
) -> dict[str, object]:
    upgraded = upgrade_point(point, room_id=room_id, room_type=room_type, strategy=strategy)
    x, y, z = point_position(upgraded)
    return {
        "position": [round(x, 3), round(y, 3), round(z, 3)],
        "label": upgraded.get("label", ""),
        "room_id": upgraded["room_id"],
        "room_type": upgraded["room_type"],
        "strategy": upgraded["strategy"],
    }


def generate_bistro_label(
    scene_dir: Path,
    base_scene: BistroBaseScene,
    config: LabelConfig,
    rng: random.Random,
    path_root: Path,
) -> dict[str, object]:
    if base_scene.label_json is not None:
        raw_payload = json.loads(base_scene.label_json.read_text(encoding="utf-8"))
        raw_groups = raw_payload.get("groups") or []
        groups: list[dict[str, object]] = []
        for index, raw_group in enumerate(raw_groups):
            if not isinstance(raw_group, dict):
                continue
            room_id = str(raw_group.get("room_id") or f"bistro_group_{index}")
            room_type = str(raw_group.get("room_type") or "bistro")
            bs_points = [
                upgrade_bs_point(point, room_id=room_id, room_type=room_type, strategy="manual")
                for point in raw_group.get("bs_points", [])
                if isinstance(point, dict)
            ]
            ue_points = [
                upgrade_point(point, room_id=room_id, room_type=room_type, strategy="manual")
                for point in raw_group.get("ue_points", [])
                if isinstance(point, dict)
            ]
            groups.append(
                group_with_positions(
                    {
                        "name": raw_group.get("name") or room_id,
                        "note": raw_group.get("note", ""),
                        "room_id": room_id,
                        "room_type": room_type,
                        "strategy": "bistro_manual",
                        "validation": {"ok": True, "errors": [], "warnings": []},
                        "bs_points": bs_points,
                        "ue_points": ue_points,
                    }
                )
            )
    else:
        min_x, min_y, _ = base_scene.bbox_min
        max_x, max_y, max_z = base_scene.bbox_max
        bs_z = min(config.bs_height_m, max_z - config.bs_ceiling_margin_m)
        bs_points = [
            bs_point_payload(x=min_x + 1.0, y=min_y + 1.0, z=bs_z, label="BS_0_0", room_id="bistro", room_type="bistro", strategy="corner"),
            bs_point_payload(x=max_x - 1.0, y=min_y + 1.0, z=bs_z, label="BS_0_1", room_id="bistro", room_type="bistro", strategy="corner"),
            bs_point_payload(x=max_x - 1.0, y=max_y - 1.0, z=bs_z, label="BS_0_2", room_id="bistro", room_type="bistro", strategy="corner"),
            bs_point_payload(x=min_x + 1.0, y=max_y - 1.0, z=bs_z, label="BS_0_3", room_id="bistro", room_type="bistro", strategy="corner"),
        ][: config.bs_per_room]
        floor_triangles = tuple(base_scene.floor_triangles)
        ue_points = []
        for index in range(12):
            tri = rng.choice(floor_triangles)
            a, b, c = tri.vertices
            r1 = math.sqrt(rng.random())
            r2 = rng.random()
            x = (1 - r1) * a[0] + r1 * (1 - r2) * b[0] + r1 * r2 * c[0]
            y = (1 - r1) * a[1] + r1 * (1 - r2) * b[1] + r1 * r2 * c[1]
            ue_points.append(
                point_payload(
                    x=x,
                    y=y,
                    z=base_scene.floor_z + config.ue_height_m,
                    label=f"UE_0_{index}",
                    room_id="bistro",
                    room_type="bistro",
                    strategy="floor_random",
                )
            )
        groups = [
            group_with_positions(
                {
                    "name": "bistro_generated_default",
                    "room_id": "bistro",
                    "room_type": "bistro",
                    "strategy": "bistro_fallback",
                    "validation": {"ok": True, "errors": [], "warnings": []},
                    "bs_points": bs_points,
                    "ue_points": ue_points,
                }
            )
        ]
    payload = make_label_payload(
        scene_file=scene_dir / "scene.obj",
        path_root=scene_dir,
        label_version=config.version,
        generator="bistro_manual" if base_scene.label_json is not None else "bistro_auto",
        groups=groups,
    )
    rooms = [
        {"room_id": group["room_id"], "ue_count": len(group["ue_points"]), "bs_count": len(group["bs_points"]), "ok": True}
        for group in groups
    ]
    report = report_payload(mode="bistro", ok=True, rooms=rooms)
    return write_label_outputs(scene_dir, payload, report, path_root)


def front3d_mesh_vertices(mesh: dict[str, Any], child: dict[str, Any], variant: str, base_scene: Front3DBaseScene) -> list[Vec3]:
    xyz = mesh.get("xyz") or []
    matrix = apply_world_offset(scenegen_transform_for_child(child, variant), base_scene.world_offset)
    vertices: list[Vec3] = []
    for index in range(0, len(xyz), 3):
        if index + 2 >= len(xyz):
            break
        point = (float(xyz[index]), float(xyz[index + 1]), float(xyz[index + 2]))
        vertices.append(transform_point_with_matrix(point, matrix))
    return vertices


def support_triangles_from_mesh(
    mesh: dict[str, Any],
    child: dict[str, Any],
    variant: str,
    base_scene: Front3DBaseScene,
) -> list[SupportTriangle]:
    vertices = front3d_mesh_vertices(mesh, child, variant, base_scene)
    faces = [int(value) for value in mesh.get("faces") or []]
    triangles: list[SupportTriangle] = []
    for index in range(0, len(faces), 3):
        if index + 2 >= len(faces):
            break
        a_i, b_i, c_i = faces[index], faces[index + 1], faces[index + 2]
        if min(a_i, b_i, c_i) < 0 or max(a_i, b_i, c_i) >= len(vertices):
            continue
        tri_vertices = (vertices[a_i], vertices[b_i], vertices[c_i])
        area, normal_z = triangle_area_and_up_normal(*tri_vertices)
        if area <= 1e-8 or abs(normal_z) < 0.85:
            continue
        triangles.append(
            SupportTriangle(
                vertices=tri_vertices,
                area=area,
                z=sum(vertex[2] for vertex in tri_vertices) / 3.0,
            )
        )
    return triangles


def rect_from_points(points: list[tuple[float, float]], padding: float = 0.0) -> Rect2D:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs) - padding, min(ys) - padding, max(xs) + padding, max(ys) + padding


def triangles_bounds(triangles: tuple[SupportTriangle, ...]) -> Rect2D:
    points = [(vertex[0], vertex[1]) for tri in triangles for vertex in tri.vertices]
    return rect_from_points(points)


def point_in_triangles(x: float, y: float, triangles: tuple[SupportTriangle, ...]) -> bool:
    return any(point_in_triangle_2d((x, y), tri, tolerance=1e-6) for tri in triangles)


def points_in_triangles_mask(points_xy: np.ndarray, triangles: tuple[SupportTriangle, ...]) -> np.ndarray:
    inside = np.zeros(points_xy.shape[0], dtype=bool)
    if points_xy.size == 0:
        return inside
    remaining = np.ones(points_xy.shape[0], dtype=bool)
    x = points_xy[:, 0]
    y = points_xy[:, 1]
    tolerance = 1e-6
    for tri in triangles:
        candidate_indices = np.flatnonzero(remaining)
        if candidate_indices.size == 0:
            break
        vertices = tri.vertices
        ax, ay = vertices[0][0], vertices[0][1]
        bx, by = vertices[1][0], vertices[1][1]
        cx, cy = vertices[2][0], vertices[2][1]
        denominator = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(denominator) <= 1e-12:
            continue
        px = x[candidate_indices]
        py = y[candidate_indices]
        w0 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / denominator
        w1 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / denominator
        w2 = 1.0 - w0 - w1
        hit_local = (w0 >= -tolerance) & (w1 >= -tolerance) & (w2 >= -tolerance)
        if not np.any(hit_local):
            continue
        hit_indices = candidate_indices[hit_local]
        inside[hit_indices] = True
        remaining[hit_indices] = False
    return inside


def label_obstacles_for_placements(
    placements: list[PlacedAsset],
    clearance: float,
    room_id: str | None = None,
) -> tuple[LabelObstacle, ...]:
    obstacles: list[LabelObstacle] = []
    for placement in placements:
        if room_id is not None and placement.source_ids.get("room_instanceid") != room_id:
            continue
        obstacles.append(
            LabelObstacle(
                min_x=placement.min_x - clearance,
                min_y=placement.min_y - clearance,
                max_x=placement.max_x + clearance,
                max_y=placement.max_y + clearance,
                min_z=placement.min_z,
                max_z=placement.max_z,
                source=placement.instance_name,
                placement_class=placement.asset.placement_class,
            )
        )
    return tuple(obstacles)


def label_obstacles_for_room(placements: list[PlacedAsset], room_id: str, clearance: float) -> tuple[LabelObstacle, ...]:
    return label_obstacles_for_placements(placements, clearance, room_id)


def point_in_obstacle_xy(x: float, y: float, obstacle: LabelObstacle) -> bool:
    return obstacle.min_x <= x <= obstacle.max_x and obstacle.min_y <= y <= obstacle.max_y


def clamp_coordinate_for_label_output(value: float, min_value: float, max_value: float) -> float:
    rounded = round(value, 3)
    if rounded < min_value:
        return math.ceil(min_value * 1000.0) / 1000.0
    if rounded > max_value:
        return math.floor(max_value * 1000.0) / 1000.0
    return round(value, 6)


def point_in_obstacles(
    x: float,
    y: float,
    z: float,
    obstacles: tuple[LabelObstacle, ...],
    strategy: str,
    min_block_height_m: float = 0.0,
) -> bool:
    for obstacle in obstacles:
        if not point_in_obstacle_xy(x, y, obstacle):
            continue
        if strategy == "footprint_column":
            return True
        if strategy == "below_ue_column":
            lower_z = max(0.0, min_block_height_m)
            if obstacle.max_z + 1e-6 >= lower_z and obstacle.min_z - 1e-6 <= z:
                return True
            continue
        if obstacle.min_z - 1e-6 <= z <= obstacle.max_z + 1e-6:
            return True
    return False


def walk_blocking_obstacles(
    obstacles: tuple[LabelObstacle, ...],
    config: LabelConfig,
) -> tuple[LabelObstacle, ...]:
    blocking_classes = set(config.walk_blocking_classes)
    blockers: list[LabelObstacle] = []
    for obstacle in obstacles:
        if obstacle.placement_class not in blocking_classes:
            continue
        if is_low_walk_obstacle(obstacle, config):
            continue
        blockers.append(obstacle)
    return tuple(blockers)


def is_low_walk_obstacle(obstacle: LabelObstacle, config: LabelConfig) -> bool:
    threshold = config.walk_ignore_low_obstacles_below_m
    return obstacle.max_z < threshold or obstacle.max_z - obstacle.min_z < threshold


def point_in_walk_obstacles(x: float, y: float, z: float, obstacles: tuple[LabelObstacle, ...], config: LabelConfig) -> bool:
    return any(
        point_in_obstacles(
            x,
            y,
            z,
            (obstacle,),
            config.obstacle_strategy,
            min_block_height_m=config.walk_ignore_low_obstacles_below_m,
        )
        for obstacle in walk_blocking_obstacles(obstacles, config)
    )


def point_has_wall_clearance(x: float, y: float, triangles: tuple[SupportTriangle, ...], clearance: float) -> bool:
    if clearance <= 0:
        return True
    checks = ((x, y), (x - clearance, y), (x + clearance, y), (x, y - clearance), (x, y + clearance))
    return all(point_in_triangles(px, py, triangles) for px, py in checks)


def point_in_rect_xy(x: float, y: float, bounds: Rect2D, padding: float = 0.0) -> bool:
    min_x, min_y, max_x, max_y = bounds
    return min_x - padding <= x <= max_x + padding and min_y - padding <= y <= max_y + padding


def free_points_satisfy_bs_wall_clearance(context: RoomLabelContext, config: LabelConfig) -> bool:
    return config.sampling_domain == "global_floor" and config.bs_wall_clearance_m <= context.boundary_clearance_m + 1e-9


def frange_grid(min_value: float, max_value: float, step: float) -> list[float]:
    start = math.ceil((min_value - 1e-9) / step) * step
    values: list[float] = []
    current = start
    while current <= max_value + 1e-9:
        values.append(round(current, 6))
        current += step
    return values


def filter_small_components(
    points: list[tuple[float, float]],
    step: float,
    min_area_m2: float,
) -> tuple[list[tuple[float, float]], dict[str, object]]:
    if min_area_m2 <= 0 or not points:
        return points, {
            "component_count": 1 if points else 0,
            "removed_small_component_count": 0,
            "removed_small_component_point_count": 0,
        }

    point_by_cell = {(int(round(x / step)), int(round(y / step))): (x, y) for x, y in points}
    unvisited = set(point_by_cell)
    kept: list[tuple[float, float]] = []
    removed_components = 0
    removed_points = 0
    component_count = 0
    min_points = max(1, math.ceil(min_area_m2 / (step * step)))

    while unvisited:
        start = unvisited.pop()
        stack = [start]
        component = [start]
        while stack:
            cell_x, cell_y = stack.pop()
            for neighbor in ((cell_x - 1, cell_y), (cell_x + 1, cell_y), (cell_x, cell_y - 1), (cell_x, cell_y + 1)):
                if neighbor not in unvisited:
                    continue
                unvisited.remove(neighbor)
                stack.append(neighbor)
                component.append(neighbor)
        component_count += 1
        if len(component) < min_points:
            removed_components += 1
            removed_points += len(component)
            continue
        kept.extend(point_by_cell[cell] for cell in component)

    kept.sort()
    return kept, {
        "component_count": component_count,
        "removed_small_component_count": removed_components,
        "removed_small_component_point_count": removed_points,
        "min_component_point_count": min_points,
    }


def generate_ue_points_for_room(
    context: RoomLabelContext,
    config: LabelConfig,
) -> UeSamplingResult:
    min_x, min_y, max_x, max_y = context.bounds_xy
    point_z = context.floor_z + config.ue_height_m
    free_points: list[tuple[float, float]] = []
    floor_candidate_count = 0
    wall_clearance_rejected_count = 0
    obstacle_rejected_count = 0
    walk_blockers = walk_blocking_obstacles(context.obstacles, config)
    ignored_low_obstacle_count = sum(
        1
        for obstacle in context.obstacles
        if obstacle.placement_class in set(config.walk_blocking_classes)
        and is_low_walk_obstacle(obstacle, config)
    )
    ignored_class_obstacle_count = sum(
        1 for obstacle in context.obstacles if obstacle.placement_class not in set(config.walk_blocking_classes)
    )
    for x in frange_grid(min_x, max_x, config.grid_resolution_m):
        for y in frange_grid(min_y, max_y, config.grid_resolution_m):
            if not point_in_triangles(x, y, context.floor_triangles):
                continue
            floor_candidate_count += 1
            if not point_has_wall_clearance(x, y, context.floor_triangles, context.boundary_clearance_m):
                wall_clearance_rejected_count += 1
                continue
            if config.ue_strategy == "free_space_grid":
                blocked = any(
                    point_in_obstacles(
                        x,
                        y,
                        point_z,
                        (obstacle,),
                        config.obstacle_strategy,
                        min_block_height_m=config.walk_ignore_low_obstacles_below_m,
                    )
                    for obstacle in walk_blockers
                )
            else:
                blocked = False
            if blocked:
                obstacle_rejected_count += 1
                continue
            free_points.append((x, y))
    component_stats: dict[str, object] = {}
    if config.ue_strategy == "free_space_grid":
        free_points, component_stats = filter_small_components(
            free_points,
            config.grid_resolution_m,
            config.walk_min_component_area_m2,
        )
    stats: dict[str, object] = {
        "ue_strategy": config.ue_strategy,
        "sampling_domain": config.sampling_domain,
        "grid_resolution_m": config.grid_resolution_m,
        "boundary_clearance_m": context.boundary_clearance_m,
        "floor_candidate_count": floor_candidate_count,
        "wall_clearance_rejected_count": wall_clearance_rejected_count,
        "obstacle_rejected_count": obstacle_rejected_count,
        "kept_count": len(free_points),
    }
    if config.ue_strategy == "free_space_grid":
        stats.update(
            {
                "walk_obstacle_mode": config.obstacle_strategy,
                "walk_blocking_classes": list(config.walk_blocking_classes),
                "walk_ignore_low_obstacles_below_m": config.walk_ignore_low_obstacles_below_m,
                "walk_min_component_area_m2": config.walk_min_component_area_m2,
                "walk_blocking_obstacle_count": len(walk_blockers),
                "ignored_low_obstacle_count": ignored_low_obstacle_count,
                "ignored_class_obstacle_count": ignored_class_obstacle_count,
            }
        )
        stats.update(component_stats)
    else:
        stats["panel_obstacle_mode"] = "none"
        stats["panel_furniture_filter_enabled"] = False
    return UeSamplingResult(points=free_points, stats=stats)


def nearest_unique_points(
    targets: list[tuple[float, float]],
    candidates: list[tuple[float, float]],
    limit: int,
) -> list[tuple[float, float]]:
    selected: list[tuple[float, float]] = []
    used: set[tuple[float, float]] = set()
    for target in targets:
        best_point: tuple[float, float] | None = None
        best_key: tuple[float, float] | None = None
        best_distance = math.inf
        for point in candidates:
            key = (round(point[0], 6), round(point[1], 6))
            if key in used:
                continue
            distance = (point[0] - target[0]) ** 2 + (point[1] - target[1]) ** 2
            if distance < best_distance:
                best_distance = distance
                best_point = point
                best_key = key
        if best_point is not None and best_key is not None:
            used.add(best_key)
            selected.append(best_point)
        if len(selected) >= limit:
            break
    return selected


def room_floor_area_m2(context: RoomLabelContext) -> float:
    return sum(triangle.area for triangle in context.floor_triangles)


def bs_count_for_room(context: RoomLabelContext, config: LabelConfig) -> int:
    if config.bs_count_strategy == "fixed_per_room":
        return config.bs_per_room
    area_m2 = room_floor_area_m2(context)
    if area_m2 < config.bs_min_room_area_m2:
        return 0
    raw_count = math.ceil(area_m2 / config.bs_area_per_point_m2)
    return min(config.bs_max_per_room, max(config.bs_min_per_room, raw_count))


def generate_bs_points_for_room(
    context: RoomLabelContext,
    free_points: list[tuple[float, float]],
    config: LabelConfig,
) -> list[tuple[float, float, float]]:
    bs_count = bs_count_for_room(context, config)
    if not free_points or bs_count <= 0:
        return []
    min_x, min_y, max_x, max_y = context.bounds_xy
    inset = max(config.bs_wall_clearance_m, config.grid_resolution_m)
    targets = [
        (min_x + inset, min_y + inset),
        (max_x - inset, min_y + inset),
        (max_x - inset, max_y - inset),
        (min_x + inset, max_y - inset),
        ((min_x + max_x) / 2.0, min_y + inset),
        (max_x - inset, (min_y + max_y) / 2.0),
        ((min_x + max_x) / 2.0, max_y - inset),
        (min_x + inset, (min_y + max_y) / 2.0),
    ]
    ceiling_z = context.ceiling_z if context.ceiling_z is not None else context.floor_z + config.bs_height_m + 1.0
    bs_z = min(config.bs_height_m, ceiling_z - config.bs_ceiling_margin_m)
    bs_z = max(context.floor_z + 0.3, bs_z)
    needs_wall_clearance_check = not free_points_satisfy_bs_wall_clearance(context, config)
    bs_candidates = [
        (x, y)
        for x, y in free_points
        if (not needs_wall_clearance_check or point_has_wall_clearance(x, y, context.floor_triangles, config.bs_wall_clearance_m))
        and not point_in_obstacles(x, y, bs_z, context.obstacles, "footprint_column")
    ]
    if not bs_candidates:
        return []
    selected_xy = nearest_unique_points(targets, bs_candidates, bs_count)
    return [(x, y, bs_z) for x, y in selected_xy]


def bs_height_for_context(context: RoomLabelContext, config: LabelConfig) -> float:
    ceiling_z = context.ceiling_z if context.ceiling_z is not None else context.floor_z + config.bs_height_m + 1.0
    bs_z = min(config.bs_height_m, ceiling_z - config.bs_ceiling_margin_m)
    return max(context.floor_z + 0.3, bs_z)


def choose_geometry_center_bs(
    global_context: RoomLabelContext,
    grouped_free_points: list[tuple[RoomLabelContext, list[tuple[float, float]]]],
    config: LabelConfig,
) -> tuple[RoomLabelContext | None, tuple[float, float, float] | None, dict[str, object]]:
    min_x, min_y, max_x, max_y = global_context.bounds_xy
    center = ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
    all_candidates: list[tuple[float, float, RoomLabelContext]] = []
    seen: set[tuple[float, float]] = set()
    needs_wall_clearance_check = not free_points_satisfy_bs_wall_clearance(global_context, config)
    for context, points in grouped_free_points:
        for x, y in points:
            key = (round(x, 6), round(y, 6))
            if key in seen:
                continue
            seen.add(key)
            point_z = context.floor_z + config.ue_height_m
            if needs_wall_clearance_check and not point_has_wall_clearance(
                x,
                y,
                global_context.floor_triangles,
                config.bs_wall_clearance_m,
            ):
                continue
            if point_in_obstacles(x, y, point_z, global_context.obstacles, "footprint_column"):
                continue
            all_candidates.append((x, y, context))

    if not all_candidates:
        return (
            None,
            None,
            {
                "ok": False,
                "strategy": "geometry_center",
                "reason": "no_candidate_after_clearance",
                "scene_center_xy": [round(center[0], 3), round(center[1], 3)],
                "candidate_count": 0,
            },
        )

    selected = min(all_candidates, key=lambda candidate: (candidate[0] - center[0]) ** 2 + (candidate[1] - center[1]) ** 2)
    selected_distance = math.sqrt((selected[0] - center[0]) ** 2 + (selected[1] - center[1]) ** 2)
    selected_radius = selected_distance
    initial_radius = max(0.0, config.bs_center_initial_radius_m)
    radius_step = config.bs_center_radius_step_m
    if selected_distance <= config.bs_center_max_radius_m + 1e-9:
        if selected_distance <= initial_radius:
            selected_radius = initial_radius
        else:
            selected_radius = initial_radius + math.ceil((selected_distance - initial_radius) / radius_step) * radius_step

    x, y, context = selected
    bs_xyz = (x, y, bs_height_for_context(context, config))
    stats = {
        "ok": True,
        "strategy": "geometry_center",
        "scene_center_xy": [round(center[0], 3), round(center[1], 3)],
        "selected_xy": [round(x, 3), round(y, 3)],
        "selected_room_id": context.room_id,
        "selected_room_type": context.room_type,
        "candidate_count": len(all_candidates),
        "selected_radius_m": round(selected_radius, 3),
        "distance_to_center_m": round(math.sqrt((x - center[0]) ** 2 + (y - center[1]) ** 2), 3),
        "bs_wall_clearance_m": config.bs_wall_clearance_m,
        "initial_radius_m": config.bs_center_initial_radius_m,
        "radius_step_m": config.bs_center_radius_step_m,
        "max_radius_m": config.bs_center_max_radius_m,
    }
    return context, bs_xyz, stats


def estimate_floor_z(triangles: tuple[SupportTriangle, ...]) -> float:
    area = sum(tri.area for tri in triangles)
    if area <= 0:
        return min((tri.z for tri in triangles), default=0.0)
    return sum(tri.z * tri.area for tri in triangles) / area


def room_placement_bounds(placements: list[PlacedAsset], room_id: str, padding: float) -> Rect2D | None:
    points: list[tuple[float, float]] = []
    for placement in placements:
        if placement.source_ids.get("room_instanceid") != room_id:
            continue
        points.extend(((placement.min_x, placement.min_y), (placement.max_x, placement.max_y)))
    if not points:
        return None
    return rect_from_points(points, padding)


def clipped_triangles_for_bounds(triangles: tuple[SupportTriangle, ...], bounds: Rect2D) -> tuple[SupportTriangle, ...]:
    min_x, min_y, max_x, max_y = bounds
    clipped: list[SupportTriangle] = []
    for tri in triangles:
        tri_bounds = triangles_bounds((tri,))
        if tri_bounds[2] < min_x or tri_bounds[0] > max_x or tri_bounds[3] < min_y or tri_bounds[1] > max_y:
            continue
        clipped.append(tri)
    return tuple(clipped)


def build_front3d_room_contexts(
    base_scene: Front3DBaseScene,
    placements: list[PlacedAsset],
    config: LabelConfig,
) -> tuple[list[RoomLabelContext], RoomLabelContext | None, list[dict[str, object]]]:
    scene_data = read_json(base_scene.source_scene_json)
    metadata = read_json(base_scene.metadata_json)
    variant = str(metadata.get("variant") or "normalized")
    mesh_by_uid = {str(mesh.get("uid")): mesh for mesh in scene_data.get("mesh") or [] if isinstance(mesh, dict)}
    global_floor: list[SupportTriangle] = []
    room_source_items: list[tuple[int, dict[str, Any], list[SupportTriangle], list[Vec3]]] = []

    for room_index, room in enumerate((scene_data.get("scene") or {}).get("room") or []):
        if not isinstance(room, dict):
            continue
        room_triangles: list[SupportTriangle] = []
        ceiling_vertices: list[Vec3] = []
        for child in room.get("children") or []:
            if not isinstance(child, dict):
                continue
            mesh = mesh_by_uid.get(str(child.get("ref")))
            if mesh is None:
                continue
            mesh_type = str(mesh.get("type") or "").lower()
            if "floor" in mesh_type:
                triangles = support_triangles_from_mesh(mesh, child, variant, base_scene)
                room_triangles.extend(triangles)
                global_floor.extend(triangles)
            elif "ceiling" in mesh_type:
                ceiling_vertices.extend(front3d_mesh_vertices(mesh, child, variant, base_scene))
        room_source_items.append((room_index, room, room_triangles, ceiling_vertices))

    if not global_floor:
        global_floor = []
        for mesh in mesh_by_uid.values():
            if "floor" not in str(mesh.get("type") or "").lower():
                continue
            child = {"pos": [0, 0, 0], "rot": [0, 0, 0, 1], "scale": [1, 1, 1]}
            global_floor.extend(support_triangles_from_mesh(mesh, child, variant, base_scene))

    global_floor_tuple = tuple(global_floor)
    global_context: RoomLabelContext | None = None
    if global_floor_tuple:
        global_context = RoomLabelContext(
            room_index=-1,
            room_id="__global__",
            room_type="GlobalFloor",
            floor_source="global_floor_mesh",
            floor_triangles=global_floor_tuple,
            floor_z=estimate_floor_z(global_floor_tuple),
            ceiling_z=base_scene.bbox_max[2],
            bounds_xy=triangles_bounds(global_floor_tuple),
            obstacles=label_obstacles_for_placements(placements, config.ue_clearance_m),
            boundary_clearance_m=config.corridor_clearance_m,
        )

    contexts: list[RoomLabelContext] = []
    skipped: list[dict[str, object]] = []
    for room_index, room, room_triangles, ceiling_vertices in room_source_items:
        room_id = str(room.get("instanceid") or f"room_{room_index}")
        room_type = str(room.get("type") or "Unknown")
        floor_source = "room_floor_mesh"
        triangles = tuple(room_triangles)
        if not triangles:
            fallback_bounds = room_placement_bounds(placements, room_id, padding=1.0)
            if fallback_bounds is None or not global_floor_tuple:
                skipped.append({"room_id": room_id, "room_type": room_type, "reason": "missing_floor_mesh_and_fallback_bounds"})
                continue
            triangles = clipped_triangles_for_bounds(global_floor_tuple, fallback_bounds)
            floor_source = "fallback_furniture_bounds_global_floor"
            if not triangles:
                skipped.append({"room_id": room_id, "room_type": room_type, "reason": "empty_fallback_floor"})
                continue
        bounds = triangles_bounds(triangles)
        floor_z = estimate_floor_z(triangles)
        ceiling_z = max((vertex[2] for vertex in ceiling_vertices), default=base_scene.bbox_max[2])
        contexts.append(
            RoomLabelContext(
                room_index=room_index,
                room_id=room_id,
                room_type=room_type,
                floor_source=floor_source,
                floor_triangles=triangles,
                floor_z=floor_z,
                ceiling_z=ceiling_z,
                bounds_xy=bounds,
                obstacles=label_obstacles_for_room(placements, room_id, config.ue_clearance_m),
                boundary_clearance_m=config.wall_clearance_m,
            )
        )
    return contexts, global_context, skipped


def front3d_context_cache_key(base_scene: Front3DBaseScene, config: LabelConfig) -> tuple[object, ...]:
    return (
        base_scene.scene_id,
        round(config.ue_clearance_m, 6),
        round(config.wall_clearance_m, 6),
        round(config.corridor_clearance_m, 6),
    )


def corridor_context_from_global(global_context: RoomLabelContext, config: LabelConfig) -> RoomLabelContext:
    return replace(
        global_context,
        room_index=-2,
        room_id=config.corridor_room_id,
        room_type=config.corridor_room_type,
        floor_source="global_floor_residual",
        boundary_clearance_m=config.corridor_clearance_m,
        is_corridor=True,
    )


def assign_global_free_points(
    points: list[tuple[float, float]],
    room_contexts: list[RoomLabelContext],
    corridor_context: RoomLabelContext,
) -> list[tuple[RoomLabelContext, list[tuple[float, float]]]]:
    assignments: dict[str, list[tuple[float, float]]] = {context.room_id: [] for context in room_contexts}
    assignments[corridor_context.room_id] = []
    if not points:
        return [(context, []) for context in room_contexts]

    points_xy = np.asarray(points, dtype=np.float64)
    assigned = np.zeros(points_xy.shape[0], dtype=bool)
    for context in room_contexts:
        min_x, min_y, max_x, max_y = context.bounds_xy
        candidate_mask = (
            ~assigned
            & (points_xy[:, 0] >= min_x - 1e-6)
            & (points_xy[:, 0] <= max_x + 1e-6)
            & (points_xy[:, 1] >= min_y - 1e-6)
            & (points_xy[:, 1] <= max_y + 1e-6)
        )
        candidate_indices = np.flatnonzero(candidate_mask)
        if candidate_indices.size == 0:
            continue
        inside_mask = points_in_triangles_mask(points_xy[candidate_indices], context.floor_triangles)
        if not np.any(inside_mask):
            continue
        hit_indices = candidate_indices[inside_mask]
        assignments[context.room_id].extend(points[index] for index in hit_indices)
        assigned[hit_indices] = True

    corridor_indices = np.flatnonzero(~assigned)
    assignments[corridor_context.room_id].extend(points[index] for index in corridor_indices)
    grouped = [(context, assignments[context.room_id]) for context in room_contexts]
    corridor_points = assignments[corridor_context.room_id]
    if corridor_points:
        grouped.append((corridor_context, corridor_points))
    return grouped


def room_sampling_result_from_assigned_points(
    context: RoomLabelContext,
    global_result: UeSamplingResult,
    points: list[tuple[float, float]],
    config: LabelConfig,
) -> UeSamplingResult:
    stats = {
        "ue_strategy": config.ue_strategy,
        "sampling_domain": config.sampling_domain,
        "grid_resolution_m": config.grid_resolution_m,
        "boundary_clearance_m": context.boundary_clearance_m,
        "assigned_count": len(points),
        "floor_candidate_count": global_result.stats.get(
            "panel_candidate_count", global_result.stats.get("floor_candidate_count", 0)
        ),
        "wall_clearance_rejected_count": global_result.stats.get("wall_clearance_rejected_count", 0),
        "obstacle_rejected_count": global_result.stats.get("obstacle_rejected_count", 0),
        "kept_count": len(points),
        "global_rectangular_candidate_count": global_result.stats.get("rectangular_candidate_count", 0),
        "global_indoor_candidate_count": global_result.stats.get("indoor_candidate_count", 0),
        "global_panel_candidate_count": global_result.stats.get("panel_candidate_count", 0),
        "global_floor_candidate_count": global_result.stats.get(
            "panel_candidate_count", global_result.stats.get("floor_candidate_count", 0)
        ),
        "global_wall_clearance_rejected_count": global_result.stats.get("wall_clearance_rejected_count", 0),
        "global_obstacle_rejected_count": global_result.stats.get("obstacle_rejected_count", 0),
        "global_kept_count": global_result.stats.get("kept_count", 0),
    }
    if context.is_corridor:
        stats["corridor_room_id"] = context.room_id
        stats["corridor_clearance_m"] = config.corridor_clearance_m
    for key, value in global_result.stats.items():
        if key in stats or key in {
            "floor_candidate_count",
            "wall_clearance_rejected_count",
            "obstacle_rejected_count",
            "kept_count",
        }:
            continue
        stats[key] = value
    return UeSamplingResult(points=points, stats=stats)


def front3d_global_mask_cache_key(base_scene: Front3DBaseScene, config: LabelConfig) -> tuple[object, ...]:
    openings = config.openings
    return (
        base_scene.scene_id,
        round(config.sampling_mask_resolution_m, 6),
        round(config.corridor_clearance_m, 6),
        openings.mode,
        round(openings.dilation_m, 6),
        round(openings.floor_tolerance_m, 6),
        round(openings.min_height_m, 6),
        openings.include_doors_as_wall,
        openings.include_windows_as_wall,
    )


def build_front3d_global_sampling_mask(
    base_scene: Front3DBaseScene,
    config: LabelConfig,
    cache: LabelGenerationCache | None = None,
) -> Front3DGlobalSamplingMask:
    cache_key = front3d_global_mask_cache_key(base_scene, config)
    if cache is not None and cache_key in cache.front3d_global_masks:
        return cache.front3d_global_masks[cache_key]

    build_start = time.perf_counter()
    scene_data = read_json(base_scene.source_scene_json)
    metadata = read_json(base_scene.metadata_json)
    variant = str(metadata.get("variant") or "normalized")
    resolution = config.sampling_mask_resolution_m
    min_x = base_scene.bbox_min[0]
    min_y = base_scene.bbox_min[1]
    max_x = max(base_scene.bbox_max[0], min_x + resolution)
    max_y = max(base_scene.bbox_max[1], min_y + resolution)
    width_px = max(1, int(math.ceil((max_x - min_x) / resolution)) + 1)
    height_px = max(1, int(math.ceil((max_y - min_y) / resolution)) + 1)
    image_size = (width_px, height_px)

    indoor_image = Image.new("L", image_size, 0)
    wall_image = Image.new("L", image_size, 0)
    door_image = Image.new("L", image_size, 0)
    indoor_draw = ImageDraw.Draw(indoor_image)
    wall_draw = ImageDraw.Draw(wall_image)
    door_draw = ImageDraw.Draw(door_image)
    mesh_type_counts: dict[str, int] = {}
    floor_mesh_count = 0
    wall_mesh_count = 0
    door_mesh_count = 0
    door_type_counts: dict[str, int] = {}

    for mesh in scene_data.get("mesh") or []:
        if not isinstance(mesh, dict):
            continue
        mesh_type = str(mesh.get("type") or "Unknown")
        mesh_type_counts[mesh_type] = mesh_type_counts.get(mesh_type, 0) + 1
        door_kind = front3d_opening_kind(
            mesh,
            mesh_type,
            variant=variant,
            base_scene=base_scene,
            opening_mode=config.openings.mode,
            floor_tolerance_m=config.openings.floor_tolerance_m,
            min_height_m=config.openings.min_height_m,
        )
        if door_kind is not None:
            door_mesh_count += 1
            door_type_counts[door_kind] = door_type_counts.get(door_kind, 0) + 1
            draw_front3d_mesh_projection(
                door_draw,
                mesh,
                variant=variant,
                base_scene=base_scene,
                min_x=min_x,
                max_y=max_y,
                resolution=resolution,
            )
        target_draw: ImageDraw.ImageDraw | None
        if "floor" in mesh_type.lower():
            target_draw = indoor_draw
            floor_mesh_count += 1
        elif front3d_mesh_type_is_wall(
            mesh_type,
            include_doors_as_wall=config.openings.include_doors_as_wall,
            include_windows_as_wall=config.openings.include_windows_as_wall,
        ):
            target_draw = wall_draw
            wall_mesh_count += 1
        else:
            target_draw = None
        if target_draw is None:
            continue
        draw_front3d_mesh_projection(
            target_draw,
            mesh,
            variant=variant,
            base_scene=base_scene,
            min_x=min_x,
            max_y=max_y,
            resolution=resolution,
        )

    if config.openings.dilation_m > 0:
        door_image = dilate_binary_image(door_image, config.openings.dilation_m, resolution)

    indoor_layer = np.asarray(indoor_image, dtype=np.uint8) > 0
    door_layer = np.asarray(door_image, dtype=np.uint8) > 0
    wall_without_doors_layer = (np.asarray(wall_image, dtype=np.uint8) > 0) & ~door_layer
    wall_image = Image.fromarray((wall_without_doors_layer.astype(np.uint8) * 255), mode="L")
    if config.corridor_clearance_m > 0:
        wall_image = dilate_binary_image(wall_image, config.corridor_clearance_m, resolution)
    wall_layer = np.asarray(wall_image, dtype=np.uint8) > 0
    indoor_with_doors_layer = indoor_layer | door_layer
    panel_layer = indoor_with_doors_layer & ~wall_layer
    stats: dict[str, object] = {
        "mask_resolution_m": resolution,
        "mask_grid_shape": [height_px, width_px],
        "mask_rectangular_pixel_count": int(width_px * height_px),
        "mask_indoor_pixel_count": int(indoor_with_doors_layer.sum()),
        "mask_wall_rejected_pixel_count": int((indoor_with_doors_layer & wall_layer).sum()),
        "mask_panel_pixel_count": int(panel_layer.sum()),
        "mask_door_pixel_count": int(door_layer.sum()),
        "mask_door_after_wall_clearance_pixel_count": int((door_layer & panel_layer).sum()),
        "door_opening_mode": config.openings.mode,
        "opening_mode": config.openings.mode,
        "opening_dilation_m": config.openings.dilation_m,
        "opening_floor_tolerance_m": config.openings.floor_tolerance_m,
        "opening_min_height_m": config.openings.min_height_m,
        "opening_include_doors_as_wall": config.openings.include_doors_as_wall,
        "opening_include_windows_as_wall": config.openings.include_windows_as_wall,
        "door_marking_policy": "door_free_before_wall_dilation_no_restore",
        "windows_used_as_openings": config.openings.mode in {"windows", "doors_and_windows"},
        "floor_mesh_count": floor_mesh_count,
        "wall_mesh_count": wall_mesh_count,
        "door_mesh_count": door_mesh_count,
        "door_type_counts": door_type_counts,
        "mesh_type_counts": mesh_type_counts,
    }
    stats["mask_build_duration_s"] = elapsed_s(build_start)
    mask = Front3DGlobalSamplingMask(
        resolution_m=resolution,
        min_x=min_x,
        min_y=min_y,
        max_x=max_x,
        max_y=max_y,
        width_px=width_px,
        height_px=height_px,
        indoor_layer=indoor_with_doors_layer,
        door_layer=door_layer,
        wall_layer=wall_layer,
        panel_layer=panel_layer,
        stats=stats,
    )
    if cache is not None:
        cache.front3d_global_masks[cache_key] = mask
    return mask


def mask_pixel_for_xy(mask: Front3DGlobalSamplingMask, x: float, y: float) -> tuple[int, int]:
    col = int(round((x - mask.min_x) / mask.resolution_m))
    row = int(round((mask.max_y - y) / mask.resolution_m))
    return row, col


def mask_value_at(layer: np.ndarray, mask: Front3DGlobalSamplingMask, x: float, y: float) -> bool:
    row, col = mask_pixel_for_xy(mask, x, y)
    if row < 0 or col < 0 or row >= mask.height_px or col >= mask.width_px:
        return False
    return bool(layer[row, col])


def generate_front3d_global_subtractive_points(
    base_scene: Front3DBaseScene,
    placements: list[PlacedAsset],
    context: RoomLabelContext,
    config: LabelConfig,
    cache: LabelGenerationCache | None = None,
) -> UeSamplingResult:
    total_start = time.perf_counter()
    resolution = config.grid_resolution_m
    cache_key = front3d_global_mask_cache_key(base_scene, config)
    mask_cache_hit = cache is not None and cache_key in cache.front3d_global_masks
    mask_start = time.perf_counter()
    mask = build_front3d_global_sampling_mask(base_scene, config, cache)
    mask_get_duration_s = elapsed_s(mask_start)
    min_x = base_scene.bbox_min[0]
    min_y = base_scene.bbox_min[1]
    max_x = max(base_scene.bbox_max[0], min_x + resolution)
    max_y = max(base_scene.bbox_max[1], min_y + resolution)

    walk_blockers = walk_blocking_obstacles(context.obstacles, config)
    ignored_low_obstacle_count = sum(
        1
        for obstacle in context.obstacles
        if obstacle.placement_class in set(config.walk_blocking_classes)
        and is_low_walk_obstacle(obstacle, config)
    )
    ignored_class_obstacle_count = sum(
        1 for obstacle in context.obstacles if obstacle.placement_class not in set(config.walk_blocking_classes)
    )
    free_points: list[tuple[float, float]] = []
    sample_width = max(1, int(math.ceil((max_x - min_x) / resolution)) + 1)
    sample_height = max(1, int(math.ceil((max_y - min_y) / resolution)) + 1)
    rectangular_candidate_count = int(sample_width * sample_height)
    indoor_candidate_count = 0
    outdoor_rejected_count = 0
    wall_rejected_count = 0
    panel_candidate_count = 0
    door_candidate_count = 0
    door_after_wall_clearance_count = 0
    bounds_rejected_count = 0
    output_bounds_clamped_count = 0
    obstacle_rejected_count = 0
    point_z = context.floor_z + config.ue_height_m
    sample_start = time.perf_counter()
    for sample_row in range(sample_height):
        y = max_y - sample_row * resolution
        for sample_col in range(sample_width):
            x = min_x + sample_col * resolution
            in_indoor = mask_value_at(mask.indoor_layer, mask, x, y)
            if in_indoor:
                indoor_candidate_count += 1
            else:
                outdoor_rejected_count += 1
                continue
            in_door = mask_value_at(mask.door_layer, mask, x, y)
            if in_door:
                door_candidate_count += 1
            in_wall = mask_value_at(mask.wall_layer, mask, x, y)
            if in_wall:
                wall_rejected_count += 1
            in_panel = mask_value_at(mask.panel_layer, mask, x, y)
            if in_door and in_panel:
                door_after_wall_clearance_count += 1
            if not in_panel:
                continue
            panel_candidate_count += 1
            if not (
                base_scene.bbox_min[0] - 1e-6 <= x <= base_scene.bbox_max[0] + 1e-6
                and base_scene.bbox_min[1] - 1e-6 <= y <= base_scene.bbox_max[1] + 1e-6
            ):
                bounds_rejected_count += 1
                continue
            if config.ue_strategy == "free_space_grid":
                blocked = any(
                    point_in_obstacles(
                        x,
                        y,
                        point_z,
                        (obstacle,),
                        config.obstacle_strategy,
                        min_block_height_m=config.walk_ignore_low_obstacles_below_m,
                    )
                    for obstacle in walk_blockers
                )
            else:
                blocked = False
            if blocked:
                obstacle_rejected_count += 1
                continue
            safe_x = clamp_coordinate_for_label_output(x, base_scene.bbox_min[0], base_scene.bbox_max[0])
            safe_y = clamp_coordinate_for_label_output(y, base_scene.bbox_min[1], base_scene.bbox_max[1])
            if safe_x != round(x, 6) or safe_y != round(y, 6):
                output_bounds_clamped_count += 1
            free_points.append((safe_x, safe_y))
    sample_grid_duration_s = elapsed_s(sample_start)

    component_stats: dict[str, object] = {}
    component_start = time.perf_counter()
    if config.ue_strategy == "free_space_grid":
        free_points, component_stats = filter_small_components(
            free_points,
            config.grid_resolution_m,
            config.walk_min_component_area_m2,
        )
    component_filter_duration_s = elapsed_s(component_start)
    stats: dict[str, object] = {
        "ue_strategy": config.ue_strategy,
        "sampling_domain": config.sampling_domain,
        "sampling_source": "front3d_global_rect_subtractive_mask",
        "sampling_pipeline_version": "2.1.0",
        "grid_resolution_m": config.grid_resolution_m,
        "mask_resolution_m": config.sampling_mask_resolution_m,
        "boundary_clearance_m": config.corridor_clearance_m,
        "rectangular_candidate_count": rectangular_candidate_count,
        "indoor_candidate_count": indoor_candidate_count,
        "outdoor_rejected_count": outdoor_rejected_count,
        "wall_clearance_rejected_count": wall_rejected_count,
        "panel_candidate_count": panel_candidate_count,
        "door_candidate_count": door_candidate_count,
        "door_after_wall_clearance_count": door_after_wall_clearance_count,
        "bounds_rejected_count": bounds_rejected_count,
        "output_bounds_clamped_count": output_bounds_clamped_count,
        "obstacle_rejected_count": obstacle_rejected_count,
        "kept_count": len(free_points),
        "sample_grid_shape": [sample_height, sample_width],
        "grid_shape": [sample_height, sample_width],
    }
    stats.update(mask.stats)
    stats["mask_cache_hit"] = mask_cache_hit
    stats["mask_get_duration_s"] = mask_get_duration_s
    stats["timings_s"] = {
        "mask_get": mask_get_duration_s,
        "sample_grid": sample_grid_duration_s,
        "component_filter": component_filter_duration_s,
        "total": elapsed_s(total_start),
    }
    if config.ue_strategy == "free_space_grid":
        stats.update(
            {
                "walk_obstacle_mode": config.obstacle_strategy,
                "walk_blocking_classes": list(config.walk_blocking_classes),
                "walk_ignore_low_obstacles_below_m": config.walk_ignore_low_obstacles_below_m,
                "walk_min_component_area_m2": config.walk_min_component_area_m2,
                "walk_blocking_obstacle_count": len(walk_blockers),
                "ignored_low_obstacle_count": ignored_low_obstacle_count,
                "ignored_class_obstacle_count": ignored_class_obstacle_count,
            }
        )
        stats.update(component_stats)
    else:
        stats["panel_obstacle_mode"] = "none"
        stats["panel_furniture_filter_enabled"] = False
    return UeSamplingResult(points=free_points, stats=stats)


def validate_front3d_room_points(
    context: RoomLabelContext,
    ue_points: list[dict[str, object]],
    bs_points: list[dict[str, object]],
    base_scene: Front3DBaseScene,
    config: LabelConfig,
) -> dict[str, object]:
    errors: list[str] = []
    warnings: list[str] = []
    min_x, min_y, min_z = base_scene.bbox_min
    max_x, max_y, max_z = base_scene.bbox_max
    skip_floor_membership_check = config.sampling_domain == "global_floor"
    for point in ue_points:
        x, y, z = point_position(point)
        if not (min_x - 1e-6 <= x <= max_x + 1e-6 and min_y - 1e-6 <= y <= max_y + 1e-6):
            errors.append(f"{point['label']}: outside architecture bbox")
        if not (min_z - 1e-6 <= z <= max_z + 1e-6):
            errors.append(f"{point['label']}: invalid height")
        if skip_floor_membership_check:
            pass
        elif not point_in_triangles(x, y, context.floor_triangles):
            errors.append(f"{point['label']}: outside room floor mesh")
        elif config.sampling_domain != "global_floor" and not point_has_wall_clearance(
            x, y, context.floor_triangles, context.boundary_clearance_m
        ):
            errors.append(f"{point['label']}: violates wall clearance")
        if config.ue_strategy == "free_space_grid":
            blocked = point_in_walk_obstacles(x, y, z, context.obstacles, config)
        else:
            blocked = False
        if blocked:
            errors.append(f"{point['label']}: inside expanded furniture obstacle")
    for point in bs_points:
        x, y, z = point_position(point)
        if not (min_x - 1e-6 <= x <= max_x + 1e-6 and min_y - 1e-6 <= y <= max_y + 1e-6):
            errors.append(f"{point['label']}: outside architecture bbox")
        if skip_floor_membership_check:
            pass
        elif not point_in_triangles(x, y, context.floor_triangles):
            errors.append(f"{point['label']}: outside room floor mesh")
        elif config.sampling_domain != "global_floor" and not point_has_wall_clearance(
            x, y, context.floor_triangles, min(context.boundary_clearance_m, config.bs_wall_clearance_m)
        ):
            errors.append(f"{point['label']}: violates BS wall clearance")
        if point_in_obstacles(x, y, z, context.obstacles, "footprint_column"):
            errors.append(f"{point['label']}: inside expanded furniture obstacle")
        if not (context.floor_z <= z <= max_z + 1e-6):
            errors.append(f"{point['label']}: invalid BS height")
    if not ue_points:
        warnings.append("room has no UE points")
    if not bs_points:
        warnings.append("room has no BS points")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def generate_front3d_label(
    scene_dir: Path,
    base_scene: Front3DBaseScene,
    placements: list[PlacedAsset],
    config: LabelConfig,
    path_root: Path,
    cache: LabelGenerationCache | None = None,
) -> dict[str, object]:
    total_start = time.perf_counter()
    timings: dict[str, float] = {}
    contexts_start = time.perf_counter()
    context_cache_hit = False
    context_cache_key = front3d_context_cache_key(base_scene, config)
    if cache is not None and context_cache_key in cache.front3d_room_contexts:
        cached_contexts, global_context, cached_skipped = cache.front3d_room_contexts[context_cache_key]
        contexts = list(cached_contexts)
        skipped = [dict(item) for item in cached_skipped]
        context_cache_hit = True
    else:
        contexts, global_context, skipped = build_front3d_room_contexts(base_scene, placements, config)
        if cache is not None:
            cache.front3d_room_contexts[context_cache_key] = (
                tuple(contexts),
                global_context,
                tuple(dict(item) for item in skipped),
            )
    timings["build_contexts"] = elapsed_s(contexts_start)
    timings["context_cache_hit"] = context_cache_hit
    grouped_sampling: list[tuple[RoomLabelContext, UeSamplingResult]] = []
    if config.sampling_domain == "global_floor":
        if global_context is None:
            skipped.append({"room_id": "__global__", "room_type": "GlobalFloor", "reason": "missing_global_floor_mesh"})
        else:
            sampling_start = time.perf_counter()
            global_result = generate_front3d_global_subtractive_points(base_scene, placements, global_context, config, cache)
            timings["global_sampling"] = elapsed_s(sampling_start)
            room_contexts = [
                replace(context, obstacles=global_context.obstacles, boundary_clearance_m=config.corridor_clearance_m)
                for context in contexts
            ]
            corridor_context = corridor_context_from_global(global_context, config)
            assign_start = time.perf_counter()
            assigned_groups = assign_global_free_points(
                global_result.points,
                room_contexts,
                corridor_context,
            )
            grouped_sampling = [
                (context, room_sampling_result_from_assigned_points(context, global_result, points, config))
                for context, points in assigned_groups
            ]
            timings["assign_points"] = elapsed_s(assign_start)
    else:
        room_sampling_start = time.perf_counter()
        grouped_sampling = [(context, generate_ue_points_for_room(context, config)) for context in contexts]
        timings["room_sampling"] = elapsed_s(room_sampling_start)

    grouped_free_points = [(context, result.points) for context, result in grouped_sampling]
    center_bs_context: RoomLabelContext | None = None
    center_bs_xyz: tuple[float, float, float] | None = None
    bs_selection: dict[str, object] | None = None
    center_bs_start = time.perf_counter()
    if config.bs_center_enabled:
        if global_context is None:
            bs_selection = {"ok": False, "strategy": "geometry_center", "reason": "missing_global_floor_context"}
        else:
            center_bs_context, center_bs_xyz, bs_selection = choose_geometry_center_bs(global_context, grouped_free_points, config)
    timings["center_bs"] = elapsed_s(center_bs_start)

    groups: list[dict[str, object]] = []
    room_reports: list[dict[str, object]] = []
    error_count = 0
    warning_count = 0
    if bs_selection is not None and not bool(bs_selection.get("ok")):
        error_count += 1
    groups_start = time.perf_counter()
    for group_index, (context, ue_result) in enumerate(grouped_sampling):
        free_xy = ue_result.points
        ue_points = [
            point_payload(
                x=x,
                y=y,
                z=context.floor_z + config.ue_height_m,
                label=f"UE_{group_index}_{index}",
                room_id=context.room_id,
                room_type=context.room_type,
                strategy=config.ue_strategy,
            )
            for index, (x, y) in enumerate(free_xy)
        ]
        if context.is_corridor:
            bs_xyz = []
        else:
            bs_xyz = generate_bs_points_for_room(context, free_xy, config)
        bs_points = []
        for index, (x, y, z) in enumerate(bs_xyz):
            bs_points.append(
                bs_point_payload(
                    x=x,
                    y=y,
                    z=z,
                    label=f"BS_{group_index}_{index}",
                    room_id=context.room_id,
                    room_type=context.room_type,
                    strategy=config.bs_strategy,
                )
            )
        if center_bs_context is not None and center_bs_context.room_id == context.room_id and center_bs_xyz is not None:
            x, y, z = center_bs_xyz
            bs_points.insert(
                0,
                bs_point_payload(
                    x=x,
                    y=y,
                    z=z,
                    label="BS_CENTER",
                    room_id=context.room_id,
                    room_type=context.room_type,
                    strategy="geometry_center",
                ),
            )
        validation = validate_front3d_room_points(context, ue_points, bs_points, base_scene, config)
        error_count += len(validation["errors"])
        warning_count += len(validation["warnings"])
        floor_area_m2 = len(free_xy) * config.grid_resolution_m * config.grid_resolution_m if context.is_corridor else room_floor_area_m2(context)
        group_name = "front3d_connected_area" if context.is_corridor else f"front3d_room_{group_index}_{context.room_type}"
        group = group_with_positions(
            {
                "name": group_name,
                "room_id": context.room_id,
                "room_type": context.room_type,
                "strategy": "front3d_auto",
                "floor_source": context.floor_source,
                "floor_area_m2": round(floor_area_m2, 3),
                "sampling_domain": config.sampling_domain,
                "ue_strategy": config.ue_strategy,
                "obstacle_strategy": config.obstacle_strategy,
                "bs_strategy": config.bs_strategy,
                "bs_count_strategy": config.bs_count_strategy,
                "boundary_clearance_m": context.boundary_clearance_m,
                "ue_sampling": ue_result.stats,
                "validation": validation,
                "bs_points": bs_points,
                "ue_points": ue_points,
            }
        )
        groups.append(group)
        room_reports.append(
            {
                "room_id": context.room_id,
                "room_type": context.room_type,
                "floor_source": context.floor_source,
                "floor_area_m2": round(floor_area_m2, 3),
                "sampling_domain": config.sampling_domain,
                "ue_strategy": config.ue_strategy,
                "obstacle_strategy": config.obstacle_strategy,
                "bs_strategy": config.bs_strategy,
                "bs_count_strategy": config.bs_count_strategy,
                "boundary_clearance_m": context.boundary_clearance_m,
                "is_corridor": context.is_corridor,
                "bounds_xy": [round(value, 6) for value in context.bounds_xy],
                "floor_z": round(context.floor_z, 6),
                "ceiling_z": round(context.ceiling_z, 6) if context.ceiling_z is not None else None,
                "obstacle_count": len(context.obstacles),
                "ue_sampling": ue_result.stats,
                "ue_count": len(ue_points),
                "bs_count": len(bs_points),
                "validation": validation,
            }
        )
    timings["build_groups_validate"] = elapsed_s(groups_start)

    skipped_reports = [
        {"room_id": item["room_id"], "room_type": item["room_type"], "skipped": True, "reason": item["reason"]}
        for item in skipped
    ]
    ok = bool(groups) and error_count == 0 and any(group.get("ue_points") for group in groups)
    payload_start = time.perf_counter()
    payload = make_label_payload(
        scene_file=scene_dir / "scene.obj",
        path_root=scene_dir,
        label_version=config.version,
        generator="front3d_auto",
        scene_id=base_scene.scene_id,
        groups=groups,
    )
    timings["build_payload"] = elapsed_s(payload_start)
    report_start = time.perf_counter()
    report = report_payload(
        mode="front3d",
        ok=ok,
        rooms=room_reports + skipped_reports,
        error_count=error_count,
        warning_count=warning_count,
        skipped_room_count=len(skipped),
    )
    report["scene_id"] = base_scene.scene_id
    if bs_selection is not None:
        report["bs_selection"] = bs_selection
    timings["build_report"] = elapsed_s(report_start)
    timings["total_generate"] = elapsed_s(total_start)
    report["timings_s"] = timings
    return write_label_outputs(scene_dir, payload, report, path_root)


def generate_label_for_scene(
    *,
    mode: str,
    scene_dir: Path,
    config: LabelConfig,
    rng: random.Random,
    path_root: Path,
    room: Room | None = None,
    base_scene: BistroBaseScene | None = None,
    front3d_base_scene: Front3DBaseScene | None = None,
    placements: list[PlacedAsset] | None = None,
    cache: LabelGenerationCache | None = None,
) -> dict[str, object]:
    if mode == "generated":
        if room is None:
            raise ValueError("Generated label requires a room")
        return generate_generated_label(scene_dir, room, config, rng, path_root)
    if mode == "bistro":
        if base_scene is None:
            raise ValueError("Bistro label requires a base scene")
        return generate_bistro_label(scene_dir, base_scene, config, rng, path_root)
    if mode == "front3d":
        if front3d_base_scene is None:
            raise ValueError("3D-FRONT label requires a base scene")
        return generate_front3d_label(scene_dir, front3d_base_scene, placements or [], config, path_root, cache)
    raise ValueError(f"Unsupported label mode: {mode}")


def copy_label_variant_outputs(
    scene_dir: Path,
    variant: LabelVariant,
    record: dict[str, object],
    path_root: Path,
) -> dict[str, object]:
    label_dir = scene_dir / "label"
    report_dir = label_dir / "report"
    label_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    label_path = label_dir / f"{variant.name}.json"
    report_path = report_dir / f"{variant.name}_report.json"
    payload = record["_payload"]
    report = record["_report"]
    write_label_json(label_path, payload)
    write_label_json(report_path, report)
    public_record = {key: value for key, value in record.items() if not str(key).startswith("_")}
    return {
        **public_record,
        "name": variant.name,
        "ue_strategy": variant.config.ue_strategy,
        "grid_resolution_m": variant.config.grid_resolution_m,
        "label_file": portable_path(label_path, path_root),
        "report_file": portable_path(report_path, path_root),
    }


def write_label_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def aggregate_label_timings(records: list[dict[str, object]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for record in records:
        timings = record.get("timings_s")
        if not isinstance(timings, dict):
            continue
        for key, value in timings.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, int | float):
                totals[str(key)] = round(totals.get(str(key), 0.0) + float(value), 6)
    return totals


def generate_label_batch_for_scene(
    *,
    mode: str,
    scene_dir: Path,
    config: LabelConfig,
    rng: random.Random,
    path_root: Path,
    room: Room | None = None,
    base_scene: BistroBaseScene | None = None,
    front3d_base_scene: Front3DBaseScene | None = None,
    placements: list[PlacedAsset] | None = None,
) -> dict[str, object]:
    for stale_path in (scene_dir / "label.json", scene_dir / "label_report.json"):
        stale_path.unlink(missing_ok=True)
    label_dir = scene_dir / "label"
    report_dir = label_dir / "report"
    if label_dir.is_dir():
        for stale_path in label_dir.glob("label_*.json"):
            stale_path.unlink()
    if report_dir.is_dir():
        for stale_path in report_dir.glob("*_report.json"):
            stale_path.unlink()

    variants = label_variants(config)
    records: list[dict[str, object]] = []
    rng_state = rng.getstate()
    cache = LabelGenerationCache()
    for variant in variants:
        rng.setstate(rng_state)
        started_at = time.perf_counter()
        variant_record = generate_label_for_scene(
            mode=mode,
            scene_dir=scene_dir,
            config=variant.config,
            rng=rng,
            path_root=path_root,
            room=room,
            base_scene=base_scene,
            front3d_base_scene=front3d_base_scene,
            placements=placements,
            cache=cache,
        )
        generate_duration_s = elapsed_s(started_at)
        variant_record["duration_s"] = generate_duration_s
        copy_start = time.perf_counter()
        public_record = copy_label_variant_outputs(scene_dir, variant, variant_record, path_root)
        copy_duration_s = elapsed_s(copy_start)
        timings = dict(public_record.get("timings_s", {})) if isinstance(public_record.get("timings_s"), dict) else {}
        timings["write_outputs"] = copy_duration_s
        timings["total_variant"] = round(generate_duration_s + copy_duration_s, 6)
        public_record["duration_s"] = timings["total_variant"]
        public_record["timings_s"] = timings
        records.append(public_record)

    substage_timings_s = aggregate_label_timings(records)
    return {
        "ok": all(bool(record["ok"]) for record in records),
        "primary": records[0]["name"],
        "label_dir": portable_path(scene_dir / "label", path_root),
        "report_dir": portable_path(scene_dir / "label" / "report", path_root),
        "variant_count": len(records),
        "variants": records,
        "group_count": records[0].get("group_count", 0),
        "bs_count": records[0].get("bs_count", 0),
        "ue_count": records[0].get("ue_count", 0),
        "error_count": sum(int(record.get("error_count", 0)) for record in records),
        "warning_count": sum(int(record.get("warning_count", 0)) for record in records),
        "variant_timings_s": {str(record["name"]): float(record.get("duration_s", 0.0)) for record in records},
        "substage_timings_s": substage_timings_s,
    }


OVERLAY_RENDER_SCALE = 4
OVERLAY_STYLE = "scientific_v1"


def image_pixel_for_point(
    x: float,
    y: float,
    origin_x: float,
    origin_y: float,
    resolution: float,
    height: int,
) -> tuple[float, float]:
    col = (x - origin_x) / resolution
    row = height - 1 - (y - origin_y) / resolution
    return col, row


def scientific_overlay_base(base_path: Path) -> Image.Image:
    base_l = Image.open(base_path).convert("L")
    base_l = ImageOps.autocontrast(base_l, cutoff=1)
    base_rgb = ImageOps.colorize(base_l, black="#5f6368", white="#fbfbfb").convert("RGB")
    white = Image.new("RGB", base_rgb.size, (255, 255, 255))
    return Image.blend(white, base_rgb, 0.72).convert("RGBA")


def scientific_overlay_canvas(base_path: Path, render_scale: int) -> Image.Image:
    stat = base_path.stat()
    return _scientific_overlay_canvas_cached(
        str(base_path.expanduser().resolve()),
        int(stat.st_mtime_ns),
        int(stat.st_size),
        int(render_scale),
    )


@lru_cache(maxsize=32)
def _scientific_overlay_canvas_cached(
    base_path_text: str,
    _mtime_ns: int,
    _size: int,
    render_scale: int,
) -> Image.Image:
    base = scientific_overlay_base(Path(base_path_text))
    width, height = base.size
    resample = getattr(Image, "Resampling", Image).BICUBIC
    return base.resize((width * render_scale, height * render_scale), resample=resample)


def scaled_point(x: float, y: float, scale: int) -> tuple[float, float]:
    return x * scale, y * scale


def draw_ue_marker(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    radius: float,
    color: tuple[int, int, int],
    scale: int,
) -> None:
    sx, sy = scaled_point(x, y, scale)
    r = radius * scale
    draw.ellipse((sx - r, sy - r, sx + r, sy + r), fill=color + (165,))


def draw_bs_marker(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    radius: float,
    scale: int,
) -> None:
    sx, sy = scaled_point(x, y, scale)
    r = radius * scale
    points = [(sx, sy - r), (sx + r, sy), (sx, sy + r), (sx - r, sy)]
    outline_r = r + 0.9 * scale
    outline = [(sx, sy - outline_r), (sx + outline_r, sy), (sx, sy + outline_r), (sx - outline_r, sy)]
    draw.polygon(outline, fill=(255, 255, 255, 230))
    draw.polygon(points, fill=(26, 103, 190, 235), outline=(9, 55, 122, 230))


def write_label_overlay(
    floorplan_dir: Path,
    label_path: Path,
    path_root: Path,
    output_dir: Path | None = None,
    output_name: str = "label_overlay",
) -> dict[str, object]:
    meta_path = floorplan_dir / "meta.json"
    base_path = floorplan_primary_image_path(floorplan_dir, meta_path)
    if not meta_path.is_file() or not base_path.is_file() or not label_path.is_file():
        return {"ok": False, "reason": "missing_floorplan_or_label"}
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    label_payload = json.loads(label_path.read_text(encoding="utf-8"))
    origin_x, origin_y = (float(value) for value in meta["origin_xy_m"])
    resolution = float(meta["resolution_m_per_pixel"])
    render_scale = OVERLAY_RENDER_SCALE
    canvas = scientific_overlay_canvas(base_path, render_scale).copy()
    width = canvas.width // render_scale
    height = canvas.height // render_scale
    draw = ImageDraw.Draw(canvas, "RGBA")
    ue_radius_px = max(0.35, min(0.9, 0.02 / max(resolution, 1e-9)))
    bs_radius_px = max(3.0, min(5.5, 0.12 / max(resolution, 1e-9)))
    room_colors = [
        (38, 103, 190),
        (0, 133, 105),
        (118, 88, 180),
        (0, 136, 170),
        (204, 118, 26),
        (92, 138, 58),
        (188, 72, 126),
        (91, 101, 119),
    ]
    ue_count = 0
    bs_count = 0
    for group_index, group in enumerate(label_payload.get("groups") or []):
        if not isinstance(group, dict):
            continue
        ue_color = room_colors[group_index % len(room_colors)]
        for point in group.get("ue_points") or []:
            if not isinstance(point, dict):
                continue
            x, y, _z = point_position(point)
            px, py = image_pixel_for_point(x, y, origin_x, origin_y, resolution, height)
            if 0 <= px < width and 0 <= py < height:
                draw_ue_marker(draw, px, py, ue_radius_px, ue_color, render_scale)
                ue_count += 1
        for point in group.get("bs_points") or []:
            if not isinstance(point, dict):
                continue
            x, y, _z = point_position(point)
            px, py = image_pixel_for_point(x, y, origin_x, origin_y, resolution, height)
            if 0 <= px < width and 0 <= py < height:
                draw_bs_marker(draw, px, py, bs_radius_px, render_scale)
                bs_count += 1
    image = canvas.resize((width, height), resample=getattr(Image, "Resampling", Image).LANCZOS)
    output_dir = floorplan_dir if output_dir is None else output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{output_name}.png"
    image.save(output_path)
    return {
        "ok": True,
        "image": portable_path(output_path, path_root),
        "base_image": portable_path(base_path, path_root),
        "ue_drawn": ue_count,
        "bs_drawn": bs_count,
        "style": OVERLAY_STYLE,
        "render_scale": render_scale,
        "ue_radius_px": round(ue_radius_px, 3),
        "bs_radius_px": round(bs_radius_px, 3),
    }


def floorplan_primary_image_path(floorplan_dir: Path, meta_path: Path) -> Path:
    if not meta_path.is_file():
        return floorplan_dir / "floorplan_1p60.png"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return floorplan_dir / "floorplan_1p60.png"
    levels = meta.get("z_levels_m")
    if isinstance(levels, list) and levels:
        return floorplan_dir / floorplan_layer_filename(float(levels[0]))
    return floorplan_dir / "floorplan_1p60.png"
