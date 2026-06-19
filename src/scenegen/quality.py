from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .geometry import collides_with_bistro_static, inside_bistro_scene, inside_room, is_supported_on_floor, overlaps
from .modes import FRONT3D_MODE, is_front3d_like
from .models import BistroBaseScene, Box3D, Front3DBaseScene, PlacedAsset, Rect2D, Room
from .paths import portable_path
from .placement import overlaps_forbidden_xy


@dataclass(frozen=True)
class QualityConfig:
    enabled: bool
    fail_on_error: bool
    collision_padding_m: float
    bistro_static_clearance_m: float
    support_tolerance_m: float

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> QualityConfig:
        return cls(
            enabled=bool(payload["enabled"]),
            fail_on_error=bool(payload["fail_on_error"]),
            collision_padding_m=float(payload["collision_padding_m"]),
            bistro_static_clearance_m=float(payload["bistro_static_clearance_m"]),
            support_tolerance_m=float(payload["support_tolerance_m"]),
        )


def placed_box(placement: PlacedAsset) -> Box3D:
    return (
        placement.min_x,
        placement.max_x,
        placement.min_y,
        placement.max_y,
        placement.min_z,
        placement.max_z,
    )


def box_xy_area(box: Box3D) -> float:
    if not all(math.isfinite(float(value)) for value in box):
        return 0.0
    return max(0.0, box[1] - box[0]) * max(0.0, box[3] - box[2])


def rounded(value: float) -> float:
    return round(float(value), 6)


def issue(
    severity: str,
    code: str,
    message: str,
    instance_name: str | None = None,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "severity": severity,
        "code": code,
        "message": message,
    }
    if instance_name is not None:
        payload["instance_name"] = instance_name
    if details:
        payload["details"] = details
    return payload


def floor_area_for_scene(
    mode: str,
    room: Room | None,
    base_scene: BistroBaseScene | None,
    front3d_base_scene: Front3DBaseScene | None = None,
) -> float:
    if mode == "generated" and room is not None:
        return room.width * room.length
    if mode == "bistro" and base_scene is not None:
        return sum(triangle.area for triangle in base_scene.floor_triangles)
    if is_front3d_like(mode) and front3d_base_scene is not None:
        return box_xy_area(
            (
                front3d_base_scene.bbox_min[0],
                front3d_base_scene.bbox_max[0],
                front3d_base_scene.bbox_min[1],
                front3d_base_scene.bbox_max[1],
                front3d_base_scene.bbox_min[2],
                front3d_base_scene.bbox_max[2],
            )
        )
    return 0.0


def scene_statistics(
    mode: str,
    placements: list[PlacedAsset],
    room: Room | None = None,
    base_scene: BistroBaseScene | None = None,
    front3d_base_scene: Front3DBaseScene | None = None,
) -> dict[str, object]:
    class_counts = Counter(placement.asset.placement_class for placement in placements)
    support_counts = Counter(placement.support_type for placement in placements)
    footprint_by_class: Counter[str] = Counter()
    total_footprint = 0.0
    invalid_bbox_count = 0
    nonfinite_bbox_count = 0
    outside_architecture_bbox_count = 0
    max_placement_xy_extent = 0.0
    max_placement_z_extent = 0.0
    scene_box: Box3D | None = None
    if is_front3d_like(mode) and front3d_base_scene is not None:
        scene_box = (
            front3d_base_scene.bbox_min[0],
            front3d_base_scene.bbox_max[0],
            front3d_base_scene.bbox_min[1],
            front3d_base_scene.bbox_max[1],
            front3d_base_scene.bbox_min[2],
            front3d_base_scene.bbox_max[2],
        )
    for placement in placements:
        box = placed_box(placement)
        if not all(math.isfinite(float(value)) for value in box):
            nonfinite_bbox_count += 1
            invalid_bbox_count += 1
            continue
        x_extent = box[1] - box[0]
        y_extent = box[3] - box[2]
        z_extent = box[5] - box[4]
        max_placement_xy_extent = max(max_placement_xy_extent, x_extent, y_extent)
        max_placement_z_extent = max(max_placement_z_extent, z_extent)
        if x_extent <= 0.0 or y_extent <= 0.0 or z_extent <= 0.0:
            invalid_bbox_count += 1
        if scene_box is not None and (
            box[1] < scene_box[0]
            or box[0] > scene_box[1]
            or box[3] < scene_box[2]
            or box[2] > scene_box[3]
            or box[5] < scene_box[4]
            or box[4] > scene_box[5] + 3.0
        ):
            outside_architecture_bbox_count += 1
        area = box_xy_area(box)
        total_footprint += area
        footprint_by_class[placement.asset.placement_class] += area

    floor_area = floor_area_for_scene(mode, room, base_scene, front3d_base_scene)
    finite_min_z = [placement.min_z for placement in placements if math.isfinite(float(placement.min_z))]
    finite_max_z = [placement.max_z for placement in placements if math.isfinite(float(placement.max_z))]
    z_min = min(finite_min_z, default=0.0)
    z_max = max(finite_max_z, default=0.0)
    front3d_bbox: dict[str, object] | None = None
    if front3d_base_scene is not None:
        bbox_values = (*front3d_base_scene.bbox_min, *front3d_base_scene.bbox_max)
        bbox_finite = all(math.isfinite(float(value)) for value in bbox_values)
        size_x = front3d_base_scene.bbox_max[0] - front3d_base_scene.bbox_min[0]
        size_y = front3d_base_scene.bbox_max[1] - front3d_base_scene.bbox_min[1]
        size_z = front3d_base_scene.bbox_max[2] - front3d_base_scene.bbox_min[2]
        front3d_bbox = {
            "finite": bbox_finite,
            "min": [rounded(value) for value in front3d_base_scene.bbox_min],
            "max": [rounded(value) for value in front3d_base_scene.bbox_max],
            "size_m": [rounded(size_x), rounded(size_y), rounded(size_z)],
            "xy_area_m2": rounded(box_xy_area((0.0, size_x, 0.0, size_y, 0.0, size_z))),
        }
    return {
        "mode": mode,
        "placement_count": len(placements),
        "object_count_by_class": dict(sorted(class_counts.items())),
        "object_count_by_support_type": dict(sorted(support_counts.items())),
        "scene_floor_area_m2": rounded(floor_area),
        "total_footprint_area_m2": rounded(total_footprint),
        "approx_footprint_ratio": rounded(total_footprint / floor_area) if floor_area > 0 else 0.0,
        "footprint_area_by_class_m2": {
            name: rounded(area) for name, area in sorted(footprint_by_class.items())
        },
        "z_range_m": [rounded(z_min), rounded(z_max)],
        "invalid_placement_bbox_count": invalid_bbox_count,
        "nonfinite_placement_bbox_count": nonfinite_bbox_count,
        "outside_architecture_bbox_count": outside_architecture_bbox_count,
        "max_placement_xy_extent_m": rounded(max_placement_xy_extent),
        "max_placement_z_extent_m": rounded(max_placement_z_extent),
        "front3d_bbox": front3d_bbox,
    }


def _support_surface_contains(base_scene: BistroBaseScene, placement: PlacedAsset, tolerance: float) -> bool:
    for surface in base_scene.support_surfaces:
        if abs(placement.z - surface.z) > tolerance:
            continue
        if is_supported_on_floor(
            placement.x,
            placement.y,
            placement.asset.width / 2.0,
            placement.asset.length / 2.0,
            placement.yaw,
            surface.triangles,
        ):
            return True
    return False


def check_scene_quality(
    mode: str,
    placements: list[PlacedAsset],
    config: QualityConfig,
    room: Room | None = None,
    base_scene: BistroBaseScene | None = None,
    front3d_base_scene: Front3DBaseScene | None = None,
    forbidden_xy_rects: tuple[Rect2D, ...] = (),
    skipped_objects: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    issues: list[dict[str, object]] = []
    name_counts = Counter(placement.instance_name for placement in placements)
    for name, count in sorted(name_counts.items()):
        if count > 1:
            issues.append(issue("error", "duplicate_instance_name", f"Duplicate placement instance name: {name}", name))

    by_name = {placement.instance_name: placement for placement in placements}
    for placement in placements:
        box = placed_box(placement)
        if box[0] > box[1] or box[2] > box[3] or box[4] > box[5]:
            issues.append(issue("error", "invalid_bbox", "Placement has an invalid bounding box.", placement.instance_name))
            continue

        if mode == "generated" and room is not None:
            if not inside_room(box, room, margin=0.0) or box[4] < -config.support_tolerance_m or box[5] > room.height:
                issues.append(issue("error", "out_of_bounds", "Placement is outside the generated room.", placement.instance_name))
        elif mode == "bistro" and base_scene is not None:
            if not inside_bistro_scene(box, base_scene, margin=0.0):
                issues.append(issue("error", "out_of_bounds", "Placement is outside the Bistro base scene bounds.", placement.instance_name))
            if overlaps_forbidden_xy(box, forbidden_xy_rects):
                issues.append(
                    issue("error", "forbidden_zone_overlap", "Placement overlaps a configured Bistro forbidden zone.", placement.instance_name)
                )
            if placement.support_type in {"bistro_floor", "bistro_floor_near_table"} and collides_with_bistro_static(
                box,
                base_scene,
                clearance=config.bistro_static_clearance_m,
            ):
                issues.append(
                    issue("error", "bistro_static_collision", "Floor placement overlaps Bistro static geometry.", placement.instance_name)
                )
        elif is_front3d_like(mode) and front3d_base_scene is not None:
            scene_box = (
                front3d_base_scene.bbox_min[0],
                front3d_base_scene.bbox_max[0],
                front3d_base_scene.bbox_min[1],
                front3d_base_scene.bbox_max[1],
                front3d_base_scene.bbox_min[2],
                front3d_base_scene.bbox_max[2],
            )
            if box_xy_area(box) <= 0.0 or box[5] <= box[4]:
                issues.append(issue("error", "invalid_bbox", "3D-FRONT placement has an empty bounding box.", placement.instance_name))
            elif (
                box[1] < scene_box[0]
                or box[0] > scene_box[1]
                or box[3] < scene_box[2]
                or box[2] > scene_box[3]
                or box[5] < scene_box[4]
                or box[4] > scene_box[5] + 3.0
            ):
                issues.append(
                    issue("warning", "front3d_outside_architecture_bbox", "Placement is outside the architecture bbox.", placement.instance_name)
                )

        if placement.support_type in {"floor", "floor_near_table"}:
            if abs(placement.z) > config.support_tolerance_m:
                issues.append(issue("error", "floor_support_mismatch", "Floor placement is not on z=0.", placement.instance_name))
        elif placement.support_type in {"bistro_floor", "bistro_floor_near_table"} and base_scene is not None:
            if abs(placement.z - base_scene.floor_z) > config.support_tolerance_m:
                issues.append(
                    issue("error", "floor_support_mismatch", "Bistro floor placement is not on the detected floor.", placement.instance_name)
                )
            elif not is_supported_on_floor(
                placement.x,
                placement.y,
                placement.asset.width / 2.0,
                placement.asset.length / 2.0,
                placement.yaw,
                base_scene.floor_triangles,
            ):
                issues.append(
                    issue("error", "floor_support_missing", "Bistro floor placement is not fully supported by floor triangles.", placement.instance_name)
                )
        elif placement.support_type == "tabletop":
            parent = by_name.get(placement.parent or "")
            if parent is None:
                issues.append(issue("error", "missing_parent", "Tabletop placement references a missing parent.", placement.instance_name))
            else:
                expected_z = parent.max_z
                if abs(placement.z - expected_z) > config.support_tolerance_m:
                    issues.append(
                        issue(
                            "error",
                            "tabletop_support_mismatch",
                            "Tabletop placement is not on the parent top surface.",
                            placement.instance_name,
                            {"expected_z_m": rounded(expected_z), "actual_z_m": rounded(placement.z)},
                        )
                    )
                if (
                    placement.min_x < parent.min_x - config.support_tolerance_m
                    or placement.max_x > parent.max_x + config.support_tolerance_m
                    or placement.min_y < parent.min_y - config.support_tolerance_m
                    or placement.max_y > parent.max_y + config.support_tolerance_m
                ):
                    issues.append(
                        issue("warning", "tabletop_xy_overhang", "Tabletop placement overhangs its parent AABB.", placement.instance_name)
                    )
        elif placement.support_type == "front3d_scene":
            pass
        elif placement.support_type == "bistro_existing_support" and base_scene is not None:
            if not _support_surface_contains(base_scene, placement, config.support_tolerance_m):
                issues.append(
                    issue(
                        "error",
                        "support_surface_missing",
                        "Bistro support placement is not fully supported by a detected support surface.",
                        placement.instance_name,
                    )
                )

    if mode != FRONT3D_MODE:
        for left_index, left in enumerate(placements):
            left_box = placed_box(left)
            for right in placements[left_index + 1 :]:
                if overlaps(left_box, right, padding=config.collision_padding_m):
                    issues.append(
                        issue(
                            "error",
                            "placement_collision",
                            "Two generated placements overlap.",
                            left.instance_name,
                            {"other_instance_name": right.instance_name},
                        )
                    )

    error_count = sum(1 for item in issues if item["severity"] == "error")
    warning_count = sum(1 for item in issues if item["severity"] == "warning")
    return {
        "ok": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "config": {
            "collision_padding_m": config.collision_padding_m,
            "bistro_static_clearance_m": config.bistro_static_clearance_m,
            "support_tolerance_m": config.support_tolerance_m,
        },
        "skipped_object_count": len(skipped_objects or []),
        "skipped_objects": skipped_objects or [],
        "issues": issues,
    }


def aggregate_run_statistics(scene_records: list[dict[str, object]]) -> dict[str, object]:
    stats = [record["statistics"] for record in scene_records if isinstance(record.get("statistics"), dict)]
    placement_counts = [int(item["placement_count"]) for item in stats]
    class_totals: Counter[str] = Counter()
    support_totals: Counter[str] = Counter()
    footprint_ratios: list[float] = []
    for item in stats:
        class_totals.update(item.get("object_count_by_class", {}))
        support_totals.update(item.get("object_count_by_support_type", {}))
        footprint_ratios.append(float(item.get("approx_footprint_ratio", 0.0)))

    quality_records = [record.get("quality") for record in scene_records if isinstance(record.get("quality"), dict)]
    quality_error_count = sum(int(record.get("error_count", 0)) for record in quality_records)
    quality_warning_count = sum(int(record.get("warning_count", 0)) for record in quality_records)
    scene_count = len(stats)
    return {
        "scene_count": scene_count,
        "placement_count": {
            "min": min(placement_counts) if placement_counts else 0,
            "max": max(placement_counts) if placement_counts else 0,
            "mean": rounded(sum(placement_counts) / len(placement_counts)) if placement_counts else 0.0,
        },
        "object_count_by_class_total": dict(sorted(class_totals.items())),
        "object_count_by_class_mean": {
            name: rounded(count / scene_count) for name, count in sorted(class_totals.items())
        }
        if scene_count
        else {},
        "object_count_by_support_type_total": dict(sorted(support_totals.items())),
        "approx_footprint_ratio": {
            "min": rounded(min(footprint_ratios)) if footprint_ratios else 0.0,
            "max": rounded(max(footprint_ratios)) if footprint_ratios else 0.0,
            "mean": rounded(sum(footprint_ratios) / len(footprint_ratios)) if footprint_ratios else 0.0,
        },
        "quality": {
            "checked_scene_count": len(quality_records),
            "ok": quality_error_count == 0,
            "error_count": quality_error_count,
            "warning_count": quality_warning_count,
        },
        "scenes": [
            {
                "scene_index": record.get("scene_index"),
                "scene_dir": record.get("scene_dir"),
                "statistics": record.get("statistics"),
                "quality": record.get("quality"),
            }
            for record in scene_records
        ],
    }


def write_json_report(path: Path, payload: dict[str, object], path_root: Path | None = None) -> str:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return portable_path(path, path_root or path.parent)
