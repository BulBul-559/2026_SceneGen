from __future__ import annotations

import argparse
import json
import random
import shutil
import time
from pathlib import Path

from . import __version__
from .assets import group_assets_by_class, load_assets, validate_asset_pool
from .config import config_to_namespace, load_effective_config, save_effective_config
from .exporters import (
    collect_label_floorplans,
    base_scene_summary,
    collect_raw_floorplans,
    collect_scene_objs,
    make_timestamp,
)
from .floorplan import FloorplanConfig, generate_floorplan_for_scene
from .labels import LabelConfig, generate_label_batch_for_scene, label_variants, write_label_overlay
from .paths import default_config_path, find_project_root, portable_path, require_dir, require_file
from .procedural import aggregate_procedural_run_report
from .quality import (
    QualityConfig,
    aggregate_run_statistics,
    check_scene_quality,
    scene_statistics,
    write_json_report,
)
from .runlog import RunLogger, timing_summary
from .sources import create_scene_source
from .validation import validate_sionna_scene


def build_parser() -> argparse.ArgumentParser:
    repo_root = find_project_root()
    parser = argparse.ArgumentParser(description="Generate Sionna-compatible procedural indoor scenes.")
    parser.add_argument("--version", action="version", version=f"SceneGen {__version__}")
    parser.add_argument("--config", type=Path, default=default_config_path(repo_root), help="Path to SceneGen YAML config.")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        dest="set_values",
        metavar="KEY=VALUE",
        help="Override a v2 config value with YAML parsing, e.g. --set pipeline.mode=front3d.",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def prepare_run_dir(output_root: Path, run_name: str, clean: bool) -> Path:
    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / run_name
    if run_dir.exists():
        if not clean:
            raise FileExistsError(f"Run directory already exists: {run_dir}. Use --clean or choose a different --run-name.")
        if run_dir.is_dir():
            shutil.rmtree(run_dir)
        else:
            run_dir.unlink()
    run_dir.mkdir(parents=True)
    return run_dir


def skipped_summary_manifest(summary_subdir: str, item_key: str) -> dict[str, object]:
    return {
        "summary_dir": f"summary/{summary_subdir}",
        "count": 0,
        item_key: [],
        "skipped": True,
        "reason": "runtime.skip_summary",
    }


def evaluate_front3d_precheck(args: argparse.Namespace, statistics: dict[str, object]) -> dict[str, object]:
    enabled = args.mode == "front3d" and bool(args.front3d_precheck_enabled)
    result: dict[str, object] = {"enabled": enabled, "ok": True, "errors": []}
    if not enabled:
        return result

    errors: list[dict[str, object]] = []
    placement_count = int(statistics.get("placement_count", 0))
    if placement_count < int(args.front3d_precheck_min_placements):
        errors.append(
            {
                "code": "too_few_placements",
                "value": placement_count,
                "threshold": int(args.front3d_precheck_min_placements),
            }
        )

    z_range = statistics.get("z_range_m")
    z_max = float(z_range[1]) if isinstance(z_range, (list, tuple)) and len(z_range) >= 2 else 0.0
    if z_max > float(args.front3d_precheck_max_z):
        errors.append(
            {
                "code": "z_range_too_high",
                "value": z_max,
                "threshold": float(args.front3d_precheck_max_z),
            }
        )

    footprint_ratio = float(statistics.get("approx_footprint_ratio", 0.0))
    if footprint_ratio > float(args.front3d_precheck_max_footprint_ratio):
        errors.append(
            {
                "code": "footprint_ratio_too_high",
                "value": footprint_ratio,
                "threshold": float(args.front3d_precheck_max_footprint_ratio),
            }
        )

    result["ok"] = not errors
    result["errors"] = errors
    return result


def evaluate_procedural_precheck(
    args: argparse.Namespace,
    statistics: dict[str, object],
    record: dict[str, object],
) -> dict[str, object]:
    enabled = args.mode == "procedural_front3d" and bool(args.procedural_precheck_enabled)
    result: dict[str, object] = {"enabled": enabled, "ok": True, "errors": []}
    if not enabled:
        return result

    errors: list[dict[str, object]] = []
    placement_count = int(statistics.get("placement_count", 0))
    if placement_count < int(args.procedural_precheck_min_placements):
        errors.append(
            {
                "code": "too_few_placements",
                "value": placement_count,
                "threshold": int(args.procedural_precheck_min_placements),
            }
        )

    procedural = record.get("procedural") if isinstance(record.get("procedural"), dict) else {}
    placement_stats = procedural.get("placement_stats") if isinstance(procedural.get("placement_stats"), dict) else {}
    desired_counts = placement_stats.get("desired_object_counts") if isinstance(placement_stats.get("desired_object_counts"), dict) else {}
    placed_counts = placement_stats.get("placed_object_counts") if isinstance(placement_stats.get("placed_object_counts"), dict) else {}
    desired_total = sum(int(value) for value in desired_counts.values()) if desired_counts else 0
    skipped_count = int(record.get("skipped_object_count") or (procedural.get("skipped_object_count", 0) if procedural else 0))
    footprint = procedural.get("footprint") if isinstance(procedural.get("footprint"), dict) else {}
    topology = procedural.get("topology") if isinstance(procedural.get("topology"), dict) else {}

    connectivity = evaluate_procedural_room_connectivity(procedural)
    if bool(getattr(args, "procedural_precheck_require_connected_rooms", True)) and not bool(connectivity["ok"]):
        errors.append(
            {
                "code": "rooms_not_connected",
                "room_count": connectivity["room_count"],
                "connected_room_count": connectivity["connected_room_count"],
                "component_count": connectivity["component_count"],
                "isolated_rooms": connectivity["isolated_rooms"],
            }
        )
    room_geometry = evaluate_procedural_room_geometry(
        procedural,
        min_area_m2=float(getattr(args, "procedural_precheck_min_room_area_m2", 0.0)),
        max_aspect_ratio=getattr(args, "procedural_precheck_max_room_aspect_ratio", None),
    )
    if room_geometry["small_rooms"]:
        errors.append(
            {
                "code": "room_area_too_small",
                "threshold": room_geometry["min_area_m2"],
                "rooms": room_geometry["small_rooms"],
            }
        )
    if room_geometry["elongated_rooms"]:
        errors.append(
            {
                "code": "room_aspect_ratio_too_high",
                "threshold": room_geometry["max_aspect_ratio"],
                "rooms": room_geometry["elongated_rooms"],
            }
        )
    room_type_geometry = evaluate_procedural_room_type_geometry(
        procedural,
        getattr(args, "procedural_precheck_room_type_geometry", None),
    )
    if room_type_geometry["too_small"]:
        errors.append({"code": "room_type_area_too_small", "rooms": room_type_geometry["too_small"]})
    if room_type_geometry["too_large"]:
        errors.append({"code": "room_type_area_too_large", "rooms": room_type_geometry["too_large"]})
    if room_type_geometry["too_elongated"]:
        errors.append({"code": "room_type_aspect_ratio_too_high", "rooms": room_type_geometry["too_elongated"]})

    footprint_fill_ratio = float(footprint.get("fill_ratio", 0.0) or 0.0) if footprint else 0.0
    footprint_concavity = float(footprint.get("concavity_area_m2", 0.0) or 0.0) if footprint else 0.0
    min_fill = getattr(args, "procedural_precheck_min_footprint_fill_ratio", None)
    max_fill = getattr(args, "procedural_precheck_max_footprint_fill_ratio", None)
    min_concavity = getattr(args, "procedural_precheck_min_footprint_concavity_m2", None)
    max_concavity = getattr(args, "procedural_precheck_max_footprint_concavity_m2", None)
    if min_fill is not None and footprint_fill_ratio < float(min_fill):
        errors.append(
            {
                "code": "footprint_fill_ratio_too_low",
                "value": round(footprint_fill_ratio, 6),
                "threshold": float(min_fill),
            }
        )
    if max_fill is not None and footprint_fill_ratio > float(max_fill):
        errors.append(
            {
                "code": "footprint_fill_ratio_too_high",
                "value": round(footprint_fill_ratio, 6),
                "threshold": float(max_fill),
            }
        )
    if min_concavity is not None and footprint_concavity < float(min_concavity):
        errors.append(
            {
                "code": "footprint_concavity_too_low",
                "value": round(footprint_concavity, 6),
                "threshold": float(min_concavity),
            }
        )
    if max_concavity is not None and footprint_concavity > float(max_concavity):
        errors.append(
            {
                "code": "footprint_concavity_too_high",
                "value": round(footprint_concavity, 6),
                "threshold": float(max_concavity),
            }
        )

    for metric_name, error_prefix in (
        ("edge_count", "topology_edge_count"),
        ("leaf_room_count", "topology_leaf_room_count"),
        ("branch_room_count", "topology_branch_room_count"),
        ("graph_diameter", "topology_graph_diameter"),
    ):
        value = int(topology.get(metric_name, 0) or 0) if topology else 0
        min_threshold = getattr(args, f"procedural_precheck_min_{error_prefix}", None)
        max_threshold = getattr(args, f"procedural_precheck_max_{error_prefix}", None)
        if min_threshold is not None and value < int(min_threshold):
            errors.append({"code": f"{error_prefix}_too_low", "value": value, "threshold": int(min_threshold)})
        if max_threshold is not None and value > int(max_threshold):
            errors.append({"code": f"{error_prefix}_too_high", "value": value, "threshold": int(max_threshold)})

    if desired_total > 0:
        placement_ratio = placement_count / desired_total
        skipped_ratio = skipped_count / desired_total
        if placement_ratio < float(args.procedural_precheck_min_placement_ratio):
            errors.append(
                {
                    "code": "placement_ratio_too_low",
                    "value": round(placement_ratio, 6),
                    "threshold": float(args.procedural_precheck_min_placement_ratio),
                    "placement_count": placement_count,
                    "desired_count": desired_total,
                }
            )
        if skipped_ratio > float(args.procedural_precheck_max_skipped_ratio):
            errors.append(
                {
                    "code": "skipped_ratio_too_high",
                    "value": round(skipped_ratio, 6),
                    "threshold": float(args.procedural_precheck_max_skipped_ratio),
                    "skipped_count": skipped_count,
                    "desired_count": desired_total,
                }
            )
        min_room_ratio = float(getattr(args, "procedural_precheck_min_room_placement_ratio", 0.0))
        if min_room_ratio > 0.0 and placed_counts:
            low_rooms: list[dict[str, object]] = []
            for room_id, desired_count_value in sorted(desired_counts.items()):
                desired_count = int(desired_count_value)
                if desired_count <= 0:
                    continue
                room_placed_count = int(placed_counts.get(room_id, 0))
                ratio = room_placed_count / desired_count
                if ratio < min_room_ratio:
                    low_rooms.append(
                        {
                            "room_id": room_id,
                            "value": round(ratio, 6),
                            "placement_count": room_placed_count,
                            "desired_count": desired_count,
                        }
                    )
            if low_rooms:
                errors.append(
                    {
                        "code": "room_placement_ratio_too_low",
                        "threshold": min_room_ratio,
                        "rooms": low_rooms,
                    }
                )

    result["ok"] = not errors
    result["errors"] = errors
    result["desired_object_count"] = desired_total
    result["placement_count"] = placement_count
    result["skipped_object_count"] = skipped_count
    result["placed_object_counts"] = dict(placed_counts) if placed_counts else {}
    result["room_connectivity"] = connectivity
    result["room_geometry"] = room_geometry
    result["room_type_geometry"] = room_type_geometry
    result["footprint"] = {
        "fill_ratio": round(footprint_fill_ratio, 6),
        "concavity_area_m2": round(footprint_concavity, 6),
    }
    result["topology"] = {
        "edge_count": int(topology.get("edge_count", 0) or 0) if topology else 0,
        "leaf_room_count": int(topology.get("leaf_room_count", 0) or 0) if topology else 0,
        "branch_room_count": int(topology.get("branch_room_count", 0) or 0) if topology else 0,
        "graph_diameter": int(topology.get("graph_diameter", 0) or 0) if topology else 0,
    }
    return result


def room_area_aspect_from_report(room: dict[str, object]) -> tuple[float | None, float | None]:
    area = room.get("area_m2")
    aspect = room.get("aspect_ratio")
    if isinstance(area, int | float) and isinstance(aspect, int | float):
        return float(area), float(aspect)
    bounds = room.get("bounds_xy")
    if not isinstance(bounds, list) or len(bounds) != 4:
        return (float(area) if isinstance(area, int | float) else None), (
            float(aspect) if isinstance(aspect, int | float) else None
        )
    x0, y0, x1, y1 = (float(value) for value in bounds)
    width = max(0.0, x1 - x0)
    length = max(0.0, y1 - y0)
    computed_area = width * length
    min_side = min(width, length)
    max_side = max(width, length)
    computed_aspect = max_side / min_side if min_side > 0 else None
    return (float(area) if isinstance(area, int | float) else computed_area), (
        float(aspect) if isinstance(aspect, int | float) else computed_aspect
    )


def evaluate_procedural_room_geometry(
    procedural: dict[str, object],
    *,
    min_area_m2: float,
    max_aspect_ratio: object,
) -> dict[str, object]:
    rooms_payload = procedural.get("rooms") if isinstance(procedural.get("rooms"), list) else []
    max_aspect = None if max_aspect_ratio is None else float(max_aspect_ratio)
    small_rooms: list[dict[str, object]] = []
    elongated_rooms: list[dict[str, object]] = []
    measured_rooms = 0
    area_values: list[float] = []
    aspect_values: list[float] = []
    for index, room in enumerate(rooms_payload):
        if not isinstance(room, dict):
            continue
        area, aspect = room_area_aspect_from_report(room)
        if area is None or aspect is None:
            continue
        measured_rooms += 1
        area_values.append(area)
        aspect_values.append(aspect)
        room_id = str(room.get("room_id") or f"room_{index}")
        if area < min_area_m2:
            small_rooms.append({"room_id": room_id, "area_m2": round(area, 6)})
        if max_aspect is not None and aspect > max_aspect:
            elongated_rooms.append({"room_id": room_id, "aspect_ratio": round(aspect, 6)})
    return {
        "ok": not small_rooms and not elongated_rooms,
        "room_count": len(rooms_payload),
        "measured_room_count": measured_rooms,
        "min_area_m2": min_area_m2,
        "max_aspect_ratio": max_aspect,
        "area_range_m2": [round(min(area_values), 6), round(max(area_values), 6)] if area_values else None,
        "aspect_ratio_range": [round(min(aspect_values), 6), round(max(aspect_values), 6)] if aspect_values else None,
        "small_rooms": small_rooms,
        "elongated_rooms": elongated_rooms,
    }


def evaluate_procedural_room_type_geometry(
    procedural: dict[str, object],
    rules: dict[str, dict[str, object]] | None,
) -> dict[str, object]:
    room_rules = rules or {}
    rooms_payload = procedural.get("rooms") if isinstance(procedural.get("rooms"), list) else []
    too_small: list[dict[str, object]] = []
    too_large: list[dict[str, object]] = []
    too_elongated: list[dict[str, object]] = []
    measured_rooms = 0
    checked_rooms = 0
    for index, room in enumerate(rooms_payload):
        if not isinstance(room, dict):
            continue
        area, aspect = room_area_aspect_from_report(room)
        if area is None or aspect is None:
            continue
        measured_rooms += 1
        room_id = str(room.get("room_id") or f"room_{index}")
        room_type = str(room.get("room_type") or "Unknown")
        rule = room_rules.get(room_type) or room_rules.get("default")
        if not isinstance(rule, dict):
            continue
        checked_rooms += 1
        min_area = rule.get("min_area_m2")
        max_area = rule.get("max_area_m2")
        max_aspect = rule.get("max_aspect_ratio")
        if isinstance(min_area, int | float) and area < float(min_area):
            too_small.append(
                {"room_id": room_id, "room_type": room_type, "area_m2": round(area, 6), "threshold": float(min_area)}
            )
        if isinstance(max_area, int | float) and area > float(max_area):
            too_large.append(
                {"room_id": room_id, "room_type": room_type, "area_m2": round(area, 6), "threshold": float(max_area)}
            )
        if isinstance(max_aspect, int | float) and aspect > float(max_aspect):
            too_elongated.append(
                {
                    "room_id": room_id,
                    "room_type": room_type,
                    "aspect_ratio": round(aspect, 6),
                    "threshold": float(max_aspect),
                }
            )
    return {
        "ok": not too_small and not too_large and not too_elongated,
        "enabled": bool(room_rules),
        "room_count": len(rooms_payload),
        "measured_room_count": measured_rooms,
        "checked_room_count": checked_rooms,
        "too_small": too_small,
        "too_large": too_large,
        "too_elongated": too_elongated,
    }


def evaluate_procedural_room_connectivity(procedural: dict[str, object]) -> dict[str, object]:
    rooms_payload = procedural.get("rooms") if isinstance(procedural.get("rooms"), list) else []
    room_ids = [
        str(room.get("room_id"))
        for room in rooms_payload
        if isinstance(room, dict) and str(room.get("room_id", "")).strip()
    ]
    room_count = int(procedural.get("room_count", len(room_ids)) or len(room_ids))
    if room_count <= 1:
        return {
            "ok": True,
            "room_count": room_count,
            "connected_room_count": room_count,
            "component_count": 1 if room_count == 1 else 0,
            "edge_count": 0,
            "isolated_rooms": [],
            "reason": "single_or_empty_room_graph",
        }
    if not room_ids or len(room_ids) != room_count:
        return {
            "ok": False,
            "room_count": room_count,
            "connected_room_count": len(room_ids),
            "component_count": 0,
            "edge_count": 0,
            "isolated_rooms": [],
            "reason": "missing_room_ids",
        }

    parent = {room_id: room_id for room_id in room_ids}
    degree = {room_id: 0 for room_id in room_ids}

    def find(room_id: str) -> str:
        while parent[room_id] != room_id:
            parent[room_id] = parent[parent[room_id]]
            room_id = parent[room_id]
        return room_id

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    edge_count = 0
    adjacency = procedural.get("adjacency") if isinstance(procedural.get("adjacency"), list) else []
    for edge in adjacency:
        if not isinstance(edge, dict):
            continue
        pair = edge.get("rooms")
        if not isinstance(pair, list) or len(pair) != 2:
            continue
        left, right = str(pair[0]), str(pair[1])
        if left not in parent or right not in parent:
            continue
        door_width = float(edge.get("door_width_m", 0.0) or 0.0)
        if door_width <= 1.0e-6:
            continue
        union(left, right)
        degree[left] += 1
        degree[right] += 1
        edge_count += 1

    components: dict[str, list[str]] = {}
    for room_id in room_ids:
        components.setdefault(find(room_id), []).append(room_id)
    largest_component = max((len(members) for members in components.values()), default=0)
    isolated_rooms = [room_id for room_id, count in degree.items() if count == 0]
    return {
        "ok": len(components) == 1,
        "room_count": room_count,
        "connected_room_count": largest_component,
        "component_count": len(components),
        "edge_count": edge_count,
        "isolated_rooms": isolated_rooms,
        "reason": "connected" if len(components) == 1 else "disconnected_room_graph",
    }


def evaluate_scene_precheck(args: argparse.Namespace, statistics: dict[str, object], record: dict[str, object]) -> dict[str, object]:
    if args.mode == "procedural_front3d":
        return evaluate_procedural_precheck(args, statistics, record)
    return evaluate_front3d_precheck(args, statistics)


def front3d_precheck_settings(args: argparse.Namespace) -> dict[str, object]:
    if args.mode != "front3d":
        return {}
    return {
        "enabled": bool(args.front3d_precheck_enabled),
        "max_attempts_per_scene": int(args.front3d_precheck_max_attempts_per_scene),
        "min_placements": int(args.front3d_precheck_min_placements),
        "max_z_m": float(args.front3d_precheck_max_z),
        "max_footprint_ratio": float(args.front3d_precheck_max_footprint_ratio),
    }


def procedural_precheck_settings(args: argparse.Namespace) -> dict[str, object]:
    if args.mode != "procedural_front3d":
        return {}
    return {
        "enabled": bool(args.procedural_precheck_enabled),
        "max_attempts_per_scene": int(args.procedural_precheck_max_attempts_per_scene),
        "min_placements": int(args.procedural_precheck_min_placements),
        "min_placement_ratio": float(args.procedural_precheck_min_placement_ratio),
        "min_room_placement_ratio": float(args.procedural_precheck_min_room_placement_ratio),
        "max_skipped_ratio": float(args.procedural_precheck_max_skipped_ratio),
        "require_connected_rooms": bool(args.procedural_precheck_require_connected_rooms),
        "min_room_area_m2": float(args.procedural_precheck_min_room_area_m2),
        "max_room_aspect_ratio": args.procedural_precheck_max_room_aspect_ratio,
        "min_footprint_fill_ratio": args.procedural_precheck_min_footprint_fill_ratio,
        "max_footprint_fill_ratio": args.procedural_precheck_max_footprint_fill_ratio,
        "min_footprint_concavity_m2": args.procedural_precheck_min_footprint_concavity_m2,
        "max_footprint_concavity_m2": args.procedural_precheck_max_footprint_concavity_m2,
        "min_topology_edge_count": args.procedural_precheck_min_topology_edge_count,
        "max_topology_edge_count": args.procedural_precheck_max_topology_edge_count,
        "min_topology_leaf_room_count": args.procedural_precheck_min_topology_leaf_room_count,
        "max_topology_leaf_room_count": args.procedural_precheck_max_topology_leaf_room_count,
        "min_topology_branch_room_count": args.procedural_precheck_min_topology_branch_room_count,
        "max_topology_branch_room_count": args.procedural_precheck_max_topology_branch_room_count,
        "min_topology_graph_diameter": args.procedural_precheck_min_topology_graph_diameter,
        "max_topology_graph_diameter": args.procedural_precheck_max_topology_graph_diameter,
        "room_type_geometry": getattr(args, "procedural_precheck_room_type_geometry", {}),
    }


def max_precheck_attempts(args: argparse.Namespace) -> int:
    if args.mode == "front3d" and bool(args.front3d_precheck_enabled):
        return int(args.front3d_precheck_max_attempts_per_scene)
    if args.mode == "procedural_front3d" and bool(args.procedural_precheck_enabled):
        return int(args.procedural_precheck_max_attempts_per_scene)
    return 1


def main(argv: list[str] | None = None) -> int:
    run_start = time.perf_counter()
    cli_args = parse_args(argv)
    repo_root = find_project_root()
    config_path = cli_args.config if cli_args.config.is_absolute() else (repo_root / cli_args.config)
    effective_config, _overrides = load_effective_config(config_path.resolve(), repo_root, cli_args)
    args = config_to_namespace(effective_config)

    front3d_like_modes = {"front3d", "procedural_front3d"}
    asset_catalog_path = require_file(args.asset_catalog, "asset catalog") if args.mode not in front3d_like_modes else args.asset_catalog
    if args.mode == "bistro":
        bistro_base_dir = require_dir(args.bistro_base_dir, "Bistro base scene directory")
    else:
        bistro_base_dir = None
    front3d_manifest_path = None
    if args.mode in front3d_like_modes:
        front3d_manifest_path = require_file(args.front3d_manifest, "3D-FRONT SceneGen manifest")
        args.front3d_manifest = front3d_manifest_path
        args.front3d_source_scene_dir = require_dir(args.front3d_source_scene_dir, "3D-FRONT source scene directory")

    run_name = args.run_name or make_timestamp()
    run_dir = prepare_run_dir(args.output_dir, run_name, args.clean)
    run_logger = RunLogger(run_dir, run_name=run_name, mode=args.mode)
    effective_config["pipeline"]["run_name"] = run_name
    effective_config["runtime"]["run_dir"] = str(run_dir)
    effective_config["runtime"]["run_id"] = run_logger.run_id
    effective_config["runtime"]["worker_id"] = run_logger.worker_id
    skip_summary = bool(effective_config["runtime"].get("skip_summary", False))
    save_effective_config(run_dir / "effective_config.yaml", effective_config)
    run_logger.event(
        "run_started",
        message="SceneGen run started",
        metrics={"requested_scenes": int(args.scenes), "seed": int(args.seed)},
        paths={"run_dir": "."},
    )
    front3d_openings = effective_config["front3d"]["openings"]
    floorplan_config = FloorplanConfig.from_mapping(effective_config["floorplan"], front3d_openings)
    quality_config = QualityConfig.from_mapping(effective_config["quality"])
    label_config = LabelConfig.from_mapping(effective_config["label"], front3d_openings)

    setup_timings: dict[str, float] = {}
    if args.mode in front3d_like_modes:
        assets_by_class = {}
    else:
        with run_logger.stage(setup_timings, "load_assets"):
            assets = load_assets(asset_catalog_path)
            assets_by_class = group_assets_by_class(assets)
            validate_asset_pool(assets_by_class)
    with run_logger.stage(setup_timings, "create_scene_source"):
        source = create_scene_source(args, assets_by_class, bistro_base_dir)
    scene_prefix = source.scene_prefix

    master_rng = random.Random(args.seed)
    scene_records: list[dict[str, object]] = []
    precheck_skipped_scenes: list[dict[str, object]] = []
    validation_failed = False
    floorplan_failed = False
    quality_failed = False
    label_failed = False
    precheck_failed = False
    stop_generation = False
    while len(scene_records) < args.scenes and not stop_generation:
        scene_index = int(args.scene_index_start) + len(scene_records)
        max_attempts = max_precheck_attempts(args)
        accepted = False
        for attempt_index in range(max_attempts):
            scene_seed = master_rng.randrange(1, 2**31)
            rng = random.Random(scene_seed)
            scene_dir = run_dir / f"{scene_prefix}_{scene_index:04d}"
            scene_key = scene_dir.name
            scene_timings: dict[str, float] = {}
            attempt_no = attempt_index + 1
            run_logger.event(
                "scene_started",
                scene_key=scene_key,
                scene_index=scene_index,
                attempt_no=attempt_no,
                metrics={"scene_seed": scene_seed},
                paths={"scene_dir": scene_key},
            )
            if scene_dir.exists():
                shutil.rmtree(scene_dir)
            with run_logger.stage(
                scene_timings,
                "build_scene",
                scene_key=scene_key,
                scene_index=scene_index,
                attempt_no=attempt_no,
            ):
                build = source.build_scene(scene_dir, scene_index, scene_seed, rng)
            placements = build.placements
            record = build.record
            record["timings_s"] = scene_timings

            with run_logger.stage(
                scene_timings,
                "statistics",
                scene_key=scene_key,
                scene_index=scene_index,
                attempt_no=attempt_no,
            ):
                statistics = scene_statistics(
                    args.mode,
                    placements,
                    room=build.room,
                    base_scene=build.base_scene,
                    front3d_base_scene=build.front3d_base_scene,
                )
                record["statistics"] = statistics
                record["statistics_file"] = write_json_report(scene_dir / "statistics.json", statistics, run_dir)
            with run_logger.stage(
                scene_timings,
                "precheck",
                scene_key=scene_key,
                scene_index=scene_index,
                attempt_no=attempt_no,
            ):
                precheck = evaluate_scene_precheck(args, statistics, record)
            record["precheck"] = precheck
            if not bool(precheck["ok"]):
                procedural_record = record.get("procedural") if isinstance(record.get("procedural"), dict) else {}
                scene_id = str(record.get("front3d_scene_id") or procedural_record.get("scene_id") or "")
                precheck_skipped_scenes.append(
                    {
                        "target_index": scene_index,
                        "attempt": attempt_index + 1,
                        "scene_seed": scene_seed,
                        "front3d_scene_id": scene_id if args.mode == "front3d" else "",
                        "source_scene_id": scene_id,
                        "layout": procedural_record.get("layout") if args.mode == "procedural_front3d" else None,
                        "configured_layout": procedural_record.get("configured_layout") if args.mode == "procedural_front3d" else None,
                        "topology": procedural_record.get("topology") if args.mode == "procedural_front3d" else None,
                        "errors": precheck["errors"],
                        "statistics": statistics,
                        "skipped_object_count": int(record.get("skipped_object_count", 0)),
                    }
                )
                mark_rejected = getattr(source, "mark_scene_rejected", None)
                if callable(mark_rejected):
                    mark_rejected(scene_id)
                shutil.rmtree(scene_dir, ignore_errors=True)
                run_logger.event(
                    "precheck_rejected",
                    level="warning",
                    scene_key=scene_key,
                    scene_index=scene_index,
                    attempt_no=attempt_no,
                    metrics={"scene_seed": scene_seed},
                    extra={
                        "source_scene_id": scene_id,
                        "precheck_errors": precheck["errors"],
                        "timings_s": scene_timings,
                    },
                )
                error_codes = ",".join(str(item.get("code")) for item in precheck["errors"] if isinstance(item, dict))
                print(
                    f"[precheck skip] target={scene_index + 1}/{args.scenes} "
                    f"scene_id={scene_id or '<unknown>'} errors={error_codes}"
                )
                continue

            if quality_config.enabled:
                with run_logger.stage(
                    scene_timings,
                    "quality",
                    scene_key=scene_key,
                    scene_index=scene_index,
                    attempt_no=attempt_no,
                ):
                    quality = check_scene_quality(
                        args.mode,
                        placements,
                        quality_config,
                        room=build.room,
                        base_scene=build.base_scene,
                        front3d_base_scene=build.front3d_base_scene,
                        forbidden_xy_rects=source.forbidden_xy_rects,
                        skipped_objects=(
                            record.get("skipped_objects") if isinstance(record.get("skipped_objects"), list) else None
                        ),
                    )
                    record["quality"] = quality
                    record["quality_report"] = write_json_report(scene_dir / "quality_report.json", quality, run_dir)
                quality_failed = quality_failed or not bool(quality["ok"])

            if args.validate_sionna:
                with run_logger.stage(
                    scene_timings,
                    "validate_sionna",
                    scene_key=scene_key,
                    scene_index=scene_index,
                    attempt_no=attempt_no,
                ):
                    validation = validate_sionna_scene(scene_dir / "scene.xml", run_dir)
                record["sionna_validation"] = validation
                validation_failed = validation_failed or not bool(validation["ok"])
            if label_config.enabled:
                try:
                    with run_logger.stage(
                        scene_timings,
                        "label",
                        scene_key=scene_key,
                        scene_index=scene_index,
                        attempt_no=attempt_no,
                    ):
                        label_record = generate_label_batch_for_scene(
                            mode=args.mode,
                            scene_dir=scene_dir,
                            config=label_config,
                            rng=rng,
                            path_root=run_dir,
                            room=build.room,
                            base_scene=build.base_scene,
                            front3d_base_scene=build.front3d_base_scene,
                            placements=placements,
                        )
                    record["label"] = label_record
                    label_failed = label_failed or not bool(label_record["ok"])
                except Exception as exc:
                    label_failed = True
                    traceback_file = run_logger.write_traceback(
                        scene_key=scene_key,
                        attempt_no=attempt_no,
                        stage="label",
                        exc=exc,
                        context={"scene_seed": scene_seed, "scene_dir": scene_key},
                    )
                    record["label"] = {
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "traceback_file": traceback_file,
                    }
                    run_logger.event(
                        "scene_failed",
                        level="error",
                        scene_key=scene_key,
                        scene_index=scene_index,
                        attempt_no=attempt_no,
                        stage="label",
                        status="failed",
                        error={"type": type(exc).__name__, "message": str(exc)},
                        traceback_file=traceback_file,
                    )
                    if label_config.fail_on_error:
                        record["timings_s"] = scene_timings
                        scene_records.append(record)
                        stop_generation = True
                        accepted = True
                        break
            if floorplan_config.enabled:
                try:
                    with run_logger.stage(
                        scene_timings,
                        "floorplan",
                        scene_key=scene_key,
                        scene_index=scene_index,
                        attempt_no=attempt_no,
                    ):
                        record["floorplan"] = generate_floorplan_for_scene(
                            scene_dir / "scene.obj",
                            scene_dir / "floorplan",
                            floorplan_config,
                            placements=placements,
                            bounds_xy=build.bounds_xy,
                            forbidden_xy_rects=source.forbidden_xy_rects,
                            front3d_base_scene=build.front3d_base_scene,
                            scene_mesh_arrays=build.floorplan_mesh_arrays,
                        )
                    floorplan_timings = record["floorplan"].get("timings_s") if isinstance(record.get("floorplan"), dict) else None
                    if isinstance(floorplan_timings, dict):
                        for timing_name, duration in floorplan_timings.items():
                            if isinstance(duration, int | float):
                                scene_timings[str(timing_name)] = float(duration)
                    label_value = record.get("label", {})
                    if label_config.enabled and label_config.overlay_enabled and isinstance(label_value, dict) and bool(
                        label_value.get("ok")
                    ):
                        with run_logger.stage(
                            scene_timings,
                            "label_overlay",
                            scene_key=scene_key,
                            scene_index=scene_index,
                            attempt_no=attempt_no,
                        ):
                            label_record = label_value
                            overlays: list[dict[str, object]] = []
                            (scene_dir / "floorplan" / "label_overlay.png").unlink(missing_ok=True)
                            label_floorplan_dir = scene_dir / "label_floorplan"
                            if label_floorplan_dir.is_dir():
                                for stale_path in label_floorplan_dir.glob("label_*.png"):
                                    stale_path.unlink()
                            for variant in label_record.get("variants", []):
                                if not isinstance(variant, dict):
                                    continue
                                label_path = run_dir / str(variant["label_file"])
                                overlay = write_label_overlay(
                                    scene_dir / "floorplan",
                                    label_path,
                                    run_dir,
                                    output_dir=label_floorplan_dir,
                                    output_name=str(variant["name"]),
                                )
                                overlay["name"] = variant["name"]
                                variant["overlay"] = overlay
                                overlays.append(overlay)
                            label_record["overlays"] = overlays
                            label_record["label_floorplan_dir"] = portable_path(scene_dir / "label_floorplan", run_dir)
                            record["floorplan"]["label_overlays"] = overlays
                except Exception as exc:
                    floorplan_failed = True
                    traceback_file = run_logger.write_traceback(
                        scene_key=scene_key,
                        attempt_no=attempt_no,
                        stage="floorplan",
                        exc=exc,
                        context={"scene_seed": scene_seed, "scene_dir": scene_key},
                    )
                    record["floorplan"] = {
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "traceback_file": traceback_file,
                    }
                    run_logger.event(
                        "scene_failed",
                        level="error",
                        scene_key=scene_key,
                        scene_index=scene_index,
                        attempt_no=attempt_no,
                        stage="floorplan",
                        status="failed",
                        error={"type": type(exc).__name__, "message": str(exc)},
                        traceback_file=traceback_file,
                    )
                    if floorplan_config.fail_on_error:
                        record["timings_s"] = scene_timings
                        scene_records.append(record)
                        stop_generation = True
                        accepted = True
                        break
            record["timings_s"] = scene_timings
            scene_records.append(record)
            quality_note = ""
            if quality_config.enabled:
                quality = record.get("quality", {})
                if isinstance(quality, dict):
                    quality_note = f" quality_errors={quality.get('error_count', 0)}"
            print(f"[{scene_index + 1}/{args.scenes}] {scene_dir} placements={len(placements)}{quality_note}")
            run_logger.event(
                "scene_completed",
                scene_key=scene_key,
                scene_index=scene_index,
                attempt_no=attempt_no,
                status="ok",
                metrics={
                    "scene_seed": scene_seed,
                    "placement_count": len(placements),
                    "duration_s": round(sum(scene_timings.values()), 6),
                },
                paths={"scene_dir": scene_key},
                extra={"timings_s": scene_timings},
            )
            run_logger.write_state(
                {
                    "status": "running",
                    "requested_scenes": int(args.scenes),
                    "completed_scenes": len(scene_records),
                    "failed_flags": {
                        "validation_failed": validation_failed,
                        "quality_failed": quality_failed,
                        "label_failed": label_failed,
                        "floorplan_failed": floorplan_failed,
                        "precheck_failed": precheck_failed,
                    },
                }
            )
            accepted = True
            break
        if not accepted:
            precheck_failed = True
            print(f"{args.mode} precheck could not fill scene index {scene_index} after {max_attempts} attempts.")
            break

    final_timings: dict[str, float] = {}
    with run_logger.stage(final_timings, "write_manifest"):
        if skip_summary:
            copy_manifest = skipped_summary_manifest("obj", "objects")
            raw_floorplan_manifest = skipped_summary_manifest("floorplan", "images")
            label_floorplan_manifest = {
                **skipped_summary_manifest("label_floorplan", "images"),
                "groups": {},
            }
        else:
            copy_manifest = collect_scene_objs(run_dir, scene_records, scene_prefix)
            raw_floorplan_manifest = collect_raw_floorplans(run_dir, scene_records, scene_prefix)
            label_floorplan_manifest = collect_label_floorplans(run_dir, scene_records, scene_prefix)
        run_statistics = aggregate_run_statistics(scene_records)
        statistics_file = write_json_report(run_dir / "statistics.json", run_statistics, run_dir)
        procedural_report: dict[str, object] | None = None
        procedural_report_file: str | None = None
        if args.mode == "procedural_front3d":
            procedural_report = aggregate_procedural_run_report(scene_records, precheck_skipped_scenes)
            procedural_report_file = write_json_report(run_dir / "procedural_report.json", procedural_report, run_dir)
    class_counts = {name: len(items) for name, items in sorted(assets_by_class.items())}
    procedural_skipped_object_count = (
        sum(int(record.get("skipped_object_count", 0)) for record in scene_records)
        if args.mode == "procedural_front3d"
        else 0
    )
    manifest: dict[str, object] = {
        "generator": "SceneGen",
        "mode": args.mode,
        "seed": args.seed,
        "run_name": run_name,
        "run_dir": ".",
        "asset_catalog": portable_path(asset_catalog_path, run_dir) if args.mode not in front3d_like_modes else None,
        "asset_class_counts": class_counts,
        "front3d_manifest": portable_path(front3d_manifest_path, run_dir) if front3d_manifest_path is not None else None,
        "front3d_variant": args.front3d_variant if args.mode in front3d_like_modes else None,
        "front3d_object_variant": args.front3d_object_variant if args.mode in front3d_like_modes else None,
        "front3d_scene_selection": args.front3d_scene_selection if args.mode == "front3d" else None,
        "front3d_scene_ids": args.front3d_scene_ids if args.mode == "front3d" else [],
        "front3d_skipped_object_count": (
            sum(int(record.get("skipped_object_count", 0)) for record in scene_records) if args.mode == "front3d" else 0
        ),
        "procedural_skipped_object_count": procedural_skipped_object_count,
        "front3d_precheck": front3d_precheck_settings(args),
        "front3d_precheck_ok": (not precheck_failed) if args.mode == "front3d" else None,
        "front3d_precheck_skipped_count": len(precheck_skipped_scenes) if args.mode == "front3d" else 0,
        "front3d_precheck_skipped_scenes": precheck_skipped_scenes if args.mode == "front3d" else [],
        "procedural_precheck": procedural_precheck_settings(args),
        "procedural_precheck_ok": (not precheck_failed) if args.mode == "procedural_front3d" else None,
        "procedural_precheck_skipped_count": len(precheck_skipped_scenes) if args.mode == "procedural_front3d" else 0,
        "procedural_precheck_skipped_scenes": precheck_skipped_scenes if args.mode == "procedural_front3d" else [],
        "forbidden_xy_rects": (
            [{"x_min": rect[0], "y_min": rect[1], "x_max": rect[2], "y_max": rect[3]} for rect in args.forbidden_xy_rects]
            if args.mode == "bistro"
            else []
        ),
        "forbidden_z_ignored": args.mode == "bistro",
        "summary_obj": copy_manifest,
        "summary_floorplan_raw": raw_floorplan_manifest,
        "summary_label_floorplan": label_floorplan_manifest,
        "summary": {
            "root": "summary",
            "obj": copy_manifest,
            "floorplan": raw_floorplan_manifest,
            "label_floorplan": label_floorplan_manifest,
            "skipped": skip_summary,
        },
        "sionna_validation_requested": bool(args.validate_sionna),
        "sionna_validation_ok": not validation_failed if args.validate_sionna else None,
        "quality_requested": bool(quality_config.enabled),
        "quality_ok": not quality_failed if quality_config.enabled else None,
        "quality_fail_on_error": bool(quality_config.fail_on_error),
        "label_requested": bool(label_config.enabled),
        "label_ok": not label_failed if label_config.enabled else None,
        "label_fail_on_error": bool(label_config.fail_on_error),
        "label_overlay_requested": bool(label_config.enabled and label_config.overlay_enabled),
        "label_batch_strategies": list(label_config.batch_strategies) if label_config.enabled else [],
        "label_batch_grid_resolutions_m": list(label_config.batch_grid_resolutions_m) if label_config.enabled else [],
        "label_variants": [variant.name for variant in label_variants(label_config)] if label_config.enabled else [],
        "label_variant_count": len(label_variants(label_config)) if label_config.enabled else 0,
        "statistics": run_statistics,
        "statistics_file": statistics_file,
        "procedural_report": procedural_report if args.mode == "procedural_front3d" else None,
        "procedural_report_file": procedural_report_file if args.mode == "procedural_front3d" else None,
        "timing_summary_s": timing_summary(scene_records),
        "run_timings_s": {
            **setup_timings,
            **final_timings,
            "total_run": round(time.perf_counter() - run_start, 6),
        },
        "logs": {
            "events": "logs/events.jsonl",
            "timings": "logs/timings.jsonl",
            "state": "logs/state/run_state.json",
            "workers": "logs/workers",
            "tracebacks": "logs/scenes",
        },
        "floorplan_requested": bool(floorplan_config.enabled),
        "floorplan_ok": not floorplan_failed if floorplan_config.enabled else None,
        "floorplan_geometry_requested": bool(floorplan_config.enabled and floorplan_config.geometry_enabled),
        "floorplan_geometry_projection": floorplan_config.geometry_projection if floorplan_config.enabled else None,
        "floorplan_class_mask_requested": bool(floorplan_config.enabled and floorplan_config.class_mask_enabled),
        "floorplan_class_mask_furniture_mode": (
            floorplan_config.class_mask_furniture_mode
            if floorplan_config.enabled and floorplan_config.class_mask_enabled
            else None
        ),
        "floorplan_height_mode": floorplan_config.height_mode if floorplan_config.enabled else None,
        "floorplan_heights_m": floorplan_config.heights_m if floorplan_config.enabled else None,
        "effective_config": portable_path(run_dir / "effective_config.yaml", run_dir),
        "scenes": scene_records,
    }
    if source.base_scene is not None:
        manifest["bistro_base_scene"] = base_scene_summary(source.base_scene, run_dir)

    mode_manifest_path = run_dir / f"manifest_{args.mode}.json"
    mode_manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    run_logger.event(
        "manifest_written",
        paths={"manifest": "manifest.json", "mode_manifest": mode_manifest_path.name},
        metrics={"scene_count": len(scene_records), "total_run_s": round(time.perf_counter() - run_start, 6)},
    )
    print(f"manifest: {mode_manifest_path}")

    exit_code = 0
    if validation_failed:
        print("Sionna validation failed for at least one scene.")
        exit_code = 1
    if quality_failed and quality_config.fail_on_error:
        print("Quality checks failed for at least one scene.")
        exit_code = 1
    if precheck_failed:
        print(f"{args.mode} precheck failed to backfill all requested scenes.")
        exit_code = 1
    if label_failed and label_config.fail_on_error:
        print("Label generation failed for at least one scene.")
        exit_code = 1
    if floorplan_failed and floorplan_config.fail_on_error:
        print("Floorplan generation failed for at least one scene.")
        exit_code = 1
    run_logger.write_state(
        {
            "status": "completed" if exit_code == 0 else "failed",
            "requested_scenes": int(args.scenes),
            "completed_scenes": len(scene_records),
            "exit_code": exit_code,
        }
    )
    run_logger.event(
        "run_completed",
        level="info" if exit_code == 0 else "error",
        status="ok" if exit_code == 0 else "failed",
        metrics={"exit_code": exit_code, "scene_count": len(scene_records), "total_run_s": round(time.perf_counter() - run_start, 6)},
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
