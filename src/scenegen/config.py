from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


LABEL_VERSION = "1.1"

DEFAULT_CONFIG: dict[str, Any] = {
    "pipeline": {
        "mode": "bistro",
        "scenes": 10,
        "seed": 20260517,
        "output_dir": "results",
        "run_name": None,
        "clean": False,
    },
    "assets": {
        "catalog": "data/catalogs/bistro.v1.json",
    },
    "bistro": {
        "base_dir": "data/scene",
        "forbidden_xy": [
            [1.0, 11.0, 4.5, 16.0],
            [8.0, 8.0, 14.0, 10.0],
        ],
    },
    "front3d": {
        "manifest": "data/3D-Front/scenegen_manifest.json",
        "source_dir": "data/3D-Front/3D-FRONT",
        "arch_variant": "normalized",
        "object_variant": "raw",
        "scene_ids": [],
        "select": "random",
        "use_replace_jid": True,
        "skip_missing_objects": True,
        "positive_xy": True,
        "ground": True,
        "precheck": {
            "enabled": True,
            "max_attempts_per_scene": 20,
            "min_placements": 1,
            "max_z_m": 8.0,
            "max_footprint_ratio": 5.0,
        },
        "openings": {
            "mode": "doors",
            "dilation_m": 0.0,
            "floor_tolerance_m": 0.25,
            "min_height_m": 1.6,
            "include_doors_as_wall": True,
            "include_windows_as_wall": True,
        },
    },
    "placement": {
        "tables": [4, 8],
        "floor_extras": 6,
        "tabletop_items": [3, 9],
        "bistro_support_items": 18,
        "max_attempts": 300,
    },
    "validation": {
        "sionna": False,
    },
    "quality": {
        "enabled": True,
        "fail_on_error": True,
        "collision_padding_m": 0.0,
        "bistro_static_clearance_m": 0.0,
        "support_tolerance_m": 0.05,
    },
    "label": {
        "enabled": True,
        "fail_on_error": True,
        "ue": {
            "height_m": 1.6,
            "sampling_domain": "global_floor",
            "wall_clearance_m": 0.2,
            "furniture_clearance_m": 0.35,
            "obstacle_strategy": "height_aware",
            "ignore_low_obstacles_below_m": 0.10,
            "blocking_classes": ["table", "seat", "floor"],
            "min_component_area_m2": 0.25,
            "connected_area": {
                "room_id": "__corridor__",
                "room_type": "ConnectedArea",
            },
            "variants": {
                "strategies": ["walk"],
                "grid_m": [0.1],
                "connected": [True],
            },
        },
        "bs": {
            "strategy": "wall_or_corner",
            "height_m": 2.4,
            "ceiling_margin_m": 0.3,
            "wall_clearance_m": 0.2,
            "count": {
                "strategy": "fixed_per_room",
                "per_room": 4,
                "min_per_room": 1,
                "max_per_room": 8,
                "min_room_area_m2": 4.0,
                "area_per_point_m2": 12.0,
            },
            "center": {
                "initial_radius_m": 0.2,
                "radius_step_m": 0.1,
                "max_radius_m": 2.0,
            },
        },
        "overlay": {
            "enabled": True,
        },
    },
    "floorplan": {
        "enabled": True,
        "fail_on_error": True,
        "resolution_m": 0.05,
        "geometry": {
            "enabled": True,
            "clean": {
                "enabled": False,
                "min_density": 2.0,
                "min_neighbors": 2,
                "min_z_m": 0.05,
                "max_abs_normal_z": 0.7,
                "opening_px": 0,
                "closing_px": 1,
            },
        },
        "semantic": {
            "enabled": False,
            "padding_m": 0.5,
            "draw_labels": True,
        },
        "class_mask": {
            "enabled": False,
            "wall_dilation_m": 0.0,
            "furniture_dilation_m": 0.0,
        },
        "height": {
            "mode": "heights",
            "values_m": [1.6],
            "step_m": 0.2,
            "top_m": None,
            "bottom_m": 0.0,
        },
        "sampling": {
            "density_scale": 128.0,
            "min_points": 100_000,
            "max_points": 25_000_000,
        },
        "preview": {
            "tile_size_px": 360,
        },
    },
}


def load_yaml_config(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Config root must be a mapping: {config_path}")
    return payload


def unknown_config_fields(config: dict[str, Any], schema: dict[str, Any] | None = None, prefix: tuple[str, ...] = ()) -> list[str]:
    schema = DEFAULT_CONFIG if schema is None else schema
    unknown: list[str] = []
    for key, value in config.items():
        path = (*prefix, str(key))
        if key not in schema:
            unknown.append(".".join(path))
            continue
        schema_value = schema[key]
        if isinstance(value, dict) and isinstance(schema_value, dict):
            unknown.extend(unknown_config_fields(value, schema_value, path))
    return unknown


def validate_known_config_fields(config: dict[str, Any], source: Path | str) -> None:
    unknown = unknown_config_fields(config)
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise ValueError(f"Unknown config field(s) in {source}: {joined}")


def load_project_config(config_path: Path) -> dict[str, Any]:
    payload = load_yaml_config(config_path)
    validate_known_config_fields(payload, config_path)
    return payload


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def set_nested(config: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = config
    for key in path[:-1]:
        child = current.setdefault(key, {})
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = value


def parse_set_override(raw: str) -> tuple[tuple[str, ...], Any]:
    if "=" not in raw:
        raise ValueError(f"--set override must use key.path=value syntax: {raw}")
    key, value_text = raw.split("=", 1)
    path = tuple(part.strip() for part in key.split(".") if part.strip())
    if not path:
        raise ValueError(f"--set override has an empty key path: {raw}")
    value = yaml.safe_load(value_text)
    if value is None and value_text == "":
        value = ""
    return path, value


def cli_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for raw in getattr(args, "set_values", None) or []:
        path, value = parse_set_override(str(raw))
        set_nested(overrides, path, value)
    validate_known_config_fields(overrides, "--set")
    return overrides


def resolve_path(repo_root: Path, value: Any) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.absolute()
    return (repo_root / path).absolute()


def as_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{key} must be a boolean")


def parse_forbidden_rects(value: Any) -> tuple[tuple[float, float, float, float], ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise ValueError("bistro.forbidden_xy must be a list")
    rects: list[tuple[float, float, float, float]] = []
    for item in value:
        if not isinstance(item, list | tuple) or len(item) != 4:
            raise ValueError("Each bistro.forbidden_xy item must be [x_min, y_min, x_max, y_max]")
        x_min, y_min, x_max, y_max = (float(part) for part in item)
        if x_max < x_min or y_max < y_min:
            raise ValueError(f"Invalid forbidden rectangle: {item}")
        rects.append((x_min, y_min, x_max, y_max))
    return tuple(rects)


def parse_sequence(value: Any, key: str) -> list[Any]:
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list | tuple):
        parts = list(value)
    else:
        raise ValueError(f"{key} must be a list or comma-separated string")
    if not parts:
        raise ValueError(f"{key} must not be empty")
    return parts


def parse_float_sequence(value: Any, key: str) -> list[float]:
    return [float(part) for part in parse_sequence(value, key)]


def parse_bool_sequence(value: Any, key: str) -> list[bool]:
    return [as_bool(part, key) for part in parse_sequence(value, key)]


def parse_string_sequence(value: Any, key: str) -> list[str]:
    return [str(part).strip() for part in parse_sequence(value, key) if str(part).strip()]


def parse_int_pair(value: Any, key: str) -> list[int]:
    parts = parse_sequence(value, key)
    if len(parts) != 2:
        raise ValueError(f"{key} must contain exactly two values")
    return [int(parts[0]), int(parts[1])]


def normalize_label_strategy(value: Any, key: str) -> str:
    text = str(value).strip()
    if text == "panel":
        return "panel"
    if text == "walk":
        return "walk"
    raise ValueError(f"{key} values must be 'panel' or 'walk'")


def normalize_effective_config(config: dict[str, Any], repo_root: Path, config_path: Path) -> dict[str, Any]:
    normalized = deepcopy(config)
    normalized.setdefault("runtime", {})
    normalized["runtime"]["config_path"] = str(config_path.resolve())

    pipeline = normalized["pipeline"]
    pipeline["output_dir"] = str(resolve_path(repo_root, pipeline["output_dir"]))
    pipeline["mode"] = str(pipeline["mode"])
    pipeline["scenes"] = int(pipeline["scenes"])
    pipeline["seed"] = int(pipeline["seed"])
    pipeline["clean"] = as_bool(pipeline["clean"], "pipeline.clean")

    assets = normalized["assets"]
    assets["catalog"] = str(resolve_path(repo_root, assets["catalog"]))

    bistro = normalized["bistro"]
    bistro["base_dir"] = str(resolve_path(repo_root, bistro["base_dir"]))
    bistro["forbidden_xy"] = [list(rect) for rect in parse_forbidden_rects(bistro.get("forbidden_xy"))]

    front3d = normalized["front3d"]
    front3d["manifest"] = str(resolve_path(repo_root, front3d["manifest"]))
    front3d["source_dir"] = str(resolve_path(repo_root, front3d["source_dir"]))
    front3d["arch_variant"] = str(front3d["arch_variant"])
    front3d["object_variant"] = str(front3d["object_variant"])
    front3d["scene_ids"] = parse_string_sequence(front3d.get("scene_ids"), "front3d.scene_ids") if front3d.get("scene_ids") else []
    front3d["select"] = str(front3d["select"])
    front3d["use_replace_jid"] = as_bool(front3d["use_replace_jid"], "front3d.use_replace_jid")
    front3d["skip_missing_objects"] = as_bool(front3d["skip_missing_objects"], "front3d.skip_missing_objects")
    front3d["positive_xy"] = as_bool(front3d["positive_xy"], "front3d.positive_xy")
    front3d["ground"] = as_bool(front3d["ground"], "front3d.ground")

    precheck = front3d["precheck"]
    precheck["enabled"] = as_bool(precheck["enabled"], "front3d.precheck.enabled")
    precheck["max_attempts_per_scene"] = int(precheck["max_attempts_per_scene"])
    precheck["min_placements"] = int(precheck["min_placements"])
    precheck["max_z_m"] = float(precheck["max_z_m"])
    precheck["max_footprint_ratio"] = float(precheck["max_footprint_ratio"])

    openings = front3d["openings"]
    openings["mode"] = str(openings["mode"])
    openings["dilation_m"] = float(openings["dilation_m"])
    openings["floor_tolerance_m"] = float(openings["floor_tolerance_m"])
    openings["min_height_m"] = float(openings["min_height_m"])
    openings["include_doors_as_wall"] = as_bool(openings["include_doors_as_wall"], "front3d.openings.include_doors_as_wall")
    openings["include_windows_as_wall"] = as_bool(
        openings["include_windows_as_wall"], "front3d.openings.include_windows_as_wall"
    )

    placement = normalized["placement"]
    placement["tables"] = parse_int_pair(placement["tables"], "placement.tables")
    placement["floor_extras"] = int(placement["floor_extras"])
    placement["tabletop_items"] = parse_int_pair(placement["tabletop_items"], "placement.tabletop_items")
    placement["bistro_support_items"] = int(placement["bistro_support_items"])
    placement["max_attempts"] = int(placement["max_attempts"])

    normalized["validation"]["sionna"] = as_bool(normalized["validation"]["sionna"], "validation.sionna")

    quality = normalized["quality"]
    quality["enabled"] = as_bool(quality["enabled"], "quality.enabled")
    quality["fail_on_error"] = as_bool(quality["fail_on_error"], "quality.fail_on_error")
    quality["collision_padding_m"] = float(quality["collision_padding_m"])
    quality["bistro_static_clearance_m"] = float(quality["bistro_static_clearance_m"])
    quality["support_tolerance_m"] = float(quality["support_tolerance_m"])

    label = normalized["label"]
    label["enabled"] = as_bool(label["enabled"], "label.enabled")
    label["fail_on_error"] = as_bool(label["fail_on_error"], "label.fail_on_error")
    ue = label["ue"]
    ue["height_m"] = float(ue["height_m"])
    ue["sampling_domain"] = str(ue["sampling_domain"])
    ue["wall_clearance_m"] = float(ue["wall_clearance_m"])
    ue["furniture_clearance_m"] = float(ue["furniture_clearance_m"])
    ue["obstacle_strategy"] = str(ue["obstacle_strategy"])
    ue["ignore_low_obstacles_below_m"] = float(ue["ignore_low_obstacles_below_m"])
    ue["blocking_classes"] = parse_string_sequence(ue["blocking_classes"], "label.ue.blocking_classes")
    ue["min_component_area_m2"] = float(ue["min_component_area_m2"])
    ue["connected_area"]["room_id"] = str(ue["connected_area"]["room_id"])
    ue["connected_area"]["room_type"] = str(ue["connected_area"]["room_type"])
    variants = ue["variants"]
    variants["strategies"] = [
        normalize_label_strategy(value, "label.ue.variants.strategies")
        for value in parse_string_sequence(variants["strategies"], "label.ue.variants.strategies")
    ]
    variants["grid_m"] = parse_float_sequence(variants["grid_m"], "label.ue.variants.grid_m")
    variants["connected"] = parse_bool_sequence(variants["connected"], "label.ue.variants.connected")

    bs = label["bs"]
    bs["strategy"] = str(bs["strategy"])
    bs["height_m"] = float(bs["height_m"])
    bs["ceiling_margin_m"] = float(bs["ceiling_margin_m"])
    bs["wall_clearance_m"] = float(bs["wall_clearance_m"])
    count = bs["count"]
    count["strategy"] = str(count["strategy"])
    count["per_room"] = int(count["per_room"])
    count["min_per_room"] = int(count["min_per_room"])
    count["max_per_room"] = int(count["max_per_room"])
    count["min_room_area_m2"] = float(count["min_room_area_m2"])
    count["area_per_point_m2"] = float(count["area_per_point_m2"])
    center = bs["center"]
    center["initial_radius_m"] = float(center["initial_radius_m"])
    center["radius_step_m"] = float(center["radius_step_m"])
    center["max_radius_m"] = float(center["max_radius_m"])
    label["overlay"]["enabled"] = as_bool(label["overlay"]["enabled"], "label.overlay.enabled")

    floorplan = normalized["floorplan"]
    floorplan["enabled"] = as_bool(floorplan["enabled"], "floorplan.enabled")
    floorplan["fail_on_error"] = as_bool(floorplan["fail_on_error"], "floorplan.fail_on_error")
    floorplan["resolution_m"] = float(floorplan["resolution_m"])
    geometry = floorplan["geometry"]
    geometry["enabled"] = as_bool(geometry["enabled"], "floorplan.geometry.enabled")
    clean = geometry["clean"]
    clean["enabled"] = as_bool(clean["enabled"], "floorplan.geometry.clean.enabled")
    clean["min_density"] = float(clean["min_density"])
    clean["min_neighbors"] = int(clean["min_neighbors"])
    clean["min_z_m"] = float(clean["min_z_m"])
    clean["max_abs_normal_z"] = float(clean["max_abs_normal_z"])
    clean["opening_px"] = int(clean["opening_px"])
    clean["closing_px"] = int(clean["closing_px"])
    semantic = floorplan["semantic"]
    semantic["enabled"] = as_bool(semantic["enabled"], "floorplan.semantic.enabled")
    semantic["padding_m"] = float(semantic["padding_m"])
    semantic["draw_labels"] = as_bool(semantic["draw_labels"], "floorplan.semantic.draw_labels")
    class_mask = floorplan["class_mask"]
    class_mask["enabled"] = as_bool(class_mask["enabled"], "floorplan.class_mask.enabled")
    class_mask["wall_dilation_m"] = float(class_mask["wall_dilation_m"])
    class_mask["furniture_dilation_m"] = float(class_mask["furniture_dilation_m"])
    height = floorplan["height"]
    height["mode"] = str(height["mode"])
    height["values_m"] = parse_float_sequence(height["values_m"], "floorplan.height.values_m")
    height["step_m"] = float(height["step_m"])
    height["top_m"] = None if height["top_m"] is None else float(height["top_m"])
    height["bottom_m"] = float(height["bottom_m"])
    sampling = floorplan["sampling"]
    sampling["density_scale"] = float(sampling["density_scale"])
    sampling["min_points"] = int(sampling["min_points"])
    sampling["max_points"] = int(sampling["max_points"])
    floorplan["preview"]["tile_size_px"] = int(floorplan["preview"]["tile_size_px"])
    return normalized


def validate_effective_config(config: dict[str, Any]) -> None:
    pipeline = config["pipeline"]
    mode = pipeline["mode"]
    if mode not in {"generated", "bistro", "front3d"}:
        raise ValueError("pipeline.mode must be 'generated', 'bistro', or 'front3d'")
    if pipeline["scenes"] < 1:
        raise ValueError("pipeline.scenes must be at least 1")
    run_name = pipeline.get("run_name")
    if run_name is not None:
        run_name = str(run_name)
        if not run_name.strip():
            raise ValueError("pipeline.run_name must not be empty")
        if "/" in run_name or "\\" in run_name:
            raise ValueError("pipeline.run_name must be a directory name, not a path")

    placement = config["placement"]
    if placement["tables"][0] < 0 or placement["tables"][1] < placement["tables"][0]:
        raise ValueError("placement.tables must be [min, max] with max >= min >= 0")
    if placement["floor_extras"] < 0:
        raise ValueError("placement.floor_extras must be non-negative")
    if placement["tabletop_items"][0] < 0 or placement["tabletop_items"][1] < placement["tabletop_items"][0]:
        raise ValueError("placement.tabletop_items must be [min, max] with max >= min >= 0")
    if placement["bistro_support_items"] < 0:
        raise ValueError("placement.bistro_support_items must be non-negative")
    if placement["max_attempts"] < 1:
        raise ValueError("placement.max_attempts must be at least 1")

    quality = config["quality"]
    if quality["collision_padding_m"] < 0:
        raise ValueError("quality.collision_padding_m must be non-negative")
    if quality["bistro_static_clearance_m"] < 0:
        raise ValueError("quality.bistro_static_clearance_m must be non-negative")
    if quality["support_tolerance_m"] < 0:
        raise ValueError("quality.support_tolerance_m must be non-negative")

    front3d = config["front3d"]
    if front3d["arch_variant"] not in {"raw", "normalized"}:
        raise ValueError("front3d.arch_variant must be 'raw' or 'normalized'")
    if front3d["object_variant"] not in {"raw", "normalized"}:
        raise ValueError("front3d.object_variant must be 'raw' or 'normalized'")
    if front3d["select"] not in {"random", "sequential"}:
        raise ValueError("front3d.select must be 'random' or 'sequential'")
    precheck = front3d["precheck"]
    if precheck["max_attempts_per_scene"] < 1:
        raise ValueError("front3d.precheck.max_attempts_per_scene must be at least 1")
    if precheck["min_placements"] < 0:
        raise ValueError("front3d.precheck.min_placements must be non-negative")
    if precheck["max_z_m"] <= 0:
        raise ValueError("front3d.precheck.max_z_m must be positive")
    if precheck["max_footprint_ratio"] <= 0:
        raise ValueError("front3d.precheck.max_footprint_ratio must be positive")
    openings = front3d["openings"]
    if openings["mode"] not in {"none", "doors", "windows", "doors_and_windows"}:
        raise ValueError("front3d.openings.mode must be 'none', 'doors', 'windows', or 'doors_and_windows'")
    if openings["dilation_m"] < 0:
        raise ValueError("front3d.openings.dilation_m must be non-negative")
    if openings["floor_tolerance_m"] < 0:
        raise ValueError("front3d.openings.floor_tolerance_m must be non-negative")
    if openings["min_height_m"] < 0:
        raise ValueError("front3d.openings.min_height_m must be non-negative")

    label = config["label"]
    ue = label["ue"]
    if ue["height_m"] <= 0:
        raise ValueError("label.ue.height_m must be positive")
    if ue["sampling_domain"] not in {"room_floor", "global_floor"}:
        raise ValueError("label.ue.sampling_domain must be 'room_floor' or 'global_floor'")
    if ue["wall_clearance_m"] < 0:
        raise ValueError("label.ue.wall_clearance_m must be non-negative")
    if ue["furniture_clearance_m"] < 0:
        raise ValueError("label.ue.furniture_clearance_m must be non-negative")
    if ue["obstacle_strategy"] not in {"height_aware", "footprint_column"}:
        raise ValueError("label.ue.obstacle_strategy must be 'height_aware' or 'footprint_column'")
    if ue["ignore_low_obstacles_below_m"] < 0:
        raise ValueError("label.ue.ignore_low_obstacles_below_m must be non-negative")
    allowed_walk_classes = {"table", "seat", "tabletop", "floor", "skip"}
    if not ue["blocking_classes"]:
        raise ValueError("label.ue.blocking_classes must not be empty")
    if any(value not in allowed_walk_classes for value in ue["blocking_classes"]):
        raise ValueError("label.ue.blocking_classes values must be table, seat, tabletop, floor, or skip")
    if ue["min_component_area_m2"] < 0:
        raise ValueError("label.ue.min_component_area_m2 must be non-negative")
    if not ue["connected_area"]["room_id"].strip():
        raise ValueError("label.ue.connected_area.room_id must not be empty")
    if not ue["connected_area"]["room_type"].strip():
        raise ValueError("label.ue.connected_area.room_type must not be empty")
    variants = ue["variants"]
    if not variants["strategies"]:
        raise ValueError("label.ue.variants.strategies must not be empty")
    if any(resolution <= 0 for resolution in variants["grid_m"]):
        raise ValueError("label.ue.variants.grid_m values must be positive")
    if not variants["connected"]:
        raise ValueError("label.ue.variants.connected must not be empty")
    bs = label["bs"]
    if bs["strategy"] not in {"wall_or_corner", "geometry_center"}:
        raise ValueError("label.bs.strategy must be 'wall_or_corner' or 'geometry_center'")
    if bs["height_m"] <= 0:
        raise ValueError("label.bs.height_m must be positive")
    if bs["ceiling_margin_m"] < 0:
        raise ValueError("label.bs.ceiling_margin_m must be non-negative")
    if bs["wall_clearance_m"] < 0:
        raise ValueError("label.bs.wall_clearance_m must be non-negative")
    count = bs["count"]
    if count["strategy"] not in {"fixed_per_room", "area_adaptive"}:
        raise ValueError("label.bs.count.strategy must be 'fixed_per_room' or 'area_adaptive'")
    if count["per_room"] < 0:
        raise ValueError("label.bs.count.per_room must be non-negative")
    if count["min_per_room"] < 0:
        raise ValueError("label.bs.count.min_per_room must be non-negative")
    if count["max_per_room"] < count["min_per_room"]:
        raise ValueError("label.bs.count.max_per_room must be greater than or equal to label.bs.count.min_per_room")
    if count["min_room_area_m2"] < 0:
        raise ValueError("label.bs.count.min_room_area_m2 must be non-negative")
    if count["area_per_point_m2"] <= 0:
        raise ValueError("label.bs.count.area_per_point_m2 must be positive")
    center = bs["center"]
    if center["initial_radius_m"] < 0:
        raise ValueError("label.bs.center.initial_radius_m must be non-negative")
    if center["radius_step_m"] <= 0:
        raise ValueError("label.bs.center.radius_step_m must be positive")
    if center["max_radius_m"] < center["initial_radius_m"]:
        raise ValueError("label.bs.center.max_radius_m must be greater than or equal to label.bs.center.initial_radius_m")

    floorplan = config["floorplan"]
    if floorplan["resolution_m"] <= 0:
        raise ValueError("floorplan.resolution_m must be positive")
    geometry = floorplan["geometry"]
    semantic = floorplan["semantic"]
    class_mask = floorplan["class_mask"]
    if floorplan["enabled"] and not (geometry["enabled"] or semantic["enabled"] or class_mask["enabled"]):
        raise ValueError("At least one of floorplan.geometry.enabled, floorplan.semantic.enabled, or floorplan.class_mask.enabled must be true")
    clean = geometry["clean"]
    if clean["enabled"] and not geometry["enabled"]:
        raise ValueError("floorplan.geometry.clean.enabled requires floorplan.geometry.enabled")
    if clean["min_density"] < 0:
        raise ValueError("floorplan.geometry.clean.min_density must be non-negative")
    if clean["min_neighbors"] < 0:
        raise ValueError("floorplan.geometry.clean.min_neighbors must be non-negative")
    if clean["min_z_m"] < 0:
        raise ValueError("floorplan.geometry.clean.min_z_m must be non-negative")
    if not 0 <= clean["max_abs_normal_z"] <= 1:
        raise ValueError("floorplan.geometry.clean.max_abs_normal_z must be between 0 and 1")
    if clean["opening_px"] < 0:
        raise ValueError("floorplan.geometry.clean.opening_px must be non-negative")
    if clean["closing_px"] < 0:
        raise ValueError("floorplan.geometry.clean.closing_px must be non-negative")
    if semantic["padding_m"] < 0:
        raise ValueError("floorplan.semantic.padding_m must be non-negative")
    if class_mask["enabled"] and mode != "front3d":
        raise ValueError("floorplan.class_mask.enabled currently supports only front3d mode")
    if class_mask["wall_dilation_m"] < 0:
        raise ValueError("floorplan.class_mask.wall_dilation_m must be non-negative")
    if class_mask["furniture_dilation_m"] < 0:
        raise ValueError("floorplan.class_mask.furniture_dilation_m must be non-negative")
    height = floorplan["height"]
    if height["mode"] not in {"layers", "heights"}:
        raise ValueError("floorplan.height.mode must be 'layers' or 'heights'")
    if height["mode"] == "heights":
        if not height["values_m"]:
            raise ValueError("floorplan.height.values_m must contain at least one height when mode is 'heights'")
        if any(value < height["bottom_m"] for value in height["values_m"]):
            raise ValueError("floorplan.height.values_m values must be greater than or equal to floorplan.height.bottom_m")
    if height["top_m"] is not None and height["top_m"] < height["bottom_m"]:
        raise ValueError("floorplan.height.top_m must be greater than or equal to floorplan.height.bottom_m")
    if height["step_m"] <= 0:
        raise ValueError("floorplan.height.step_m must be positive")
    sampling = floorplan["sampling"]
    if sampling["density_scale"] <= 0:
        raise ValueError("floorplan.sampling.density_scale must be positive")
    if sampling["min_points"] < 1:
        raise ValueError("floorplan.sampling.min_points must be positive")
    if sampling["max_points"] < sampling["min_points"]:
        raise ValueError("floorplan.sampling.max_points must be greater than or equal to floorplan.sampling.min_points")
    if floorplan["preview"]["tile_size_px"] < 1:
        raise ValueError("floorplan.preview.tile_size_px must be positive")


def save_effective_config(path: Path, config: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def internal_label_strategy(value: str) -> str:
    if value == "panel":
        return "plane_grid"
    if value == "walk":
        return "free_space_grid"
    raise ValueError(f"Unsupported label strategy: {value}")


def config_to_namespace(config: dict[str, Any]) -> argparse.Namespace:
    pipeline = config["pipeline"]
    assets = config["assets"]
    bistro = config["bistro"]
    front3d = config["front3d"]
    precheck = front3d["precheck"]
    placement = config["placement"]
    validation = config["validation"]
    quality = config["quality"]
    label = config["label"]
    ue = label["ue"]
    variants = ue["variants"]
    bs = label["bs"]
    count = bs["count"]
    center = bs["center"]
    floorplan = config["floorplan"]
    geometry = floorplan["geometry"]
    clean = geometry["clean"]
    semantic = floorplan["semantic"]
    class_mask = floorplan["class_mask"]
    height = floorplan["height"]
    sampling = floorplan["sampling"]
    openings = front3d["openings"]
    return argparse.Namespace(
        mode=pipeline["mode"],
        scenes=pipeline["scenes"],
        seed=pipeline["seed"],
        output_dir=Path(pipeline["output_dir"]),
        run_name=pipeline["run_name"],
        clean=pipeline["clean"],
        asset_catalog=Path(assets["catalog"]),
        bistro_base_dir=Path(bistro["base_dir"]),
        forbidden_xy_rects=parse_forbidden_rects(bistro.get("forbidden_xy")),
        front3d_manifest=Path(front3d["manifest"]),
        front3d_source_scene_dir=Path(front3d["source_dir"]),
        front3d_variant=front3d["arch_variant"],
        front3d_object_variant=front3d["object_variant"],
        front3d_scene_ids=front3d["scene_ids"],
        front3d_scene_selection=front3d["select"],
        front3d_use_replace_jid=front3d["use_replace_jid"],
        front3d_skip_missing_objects=front3d["skip_missing_objects"],
        front3d_normalize_positive_xy=front3d["positive_xy"],
        front3d_ground_objects=front3d["ground"],
        front3d_precheck_enabled=precheck["enabled"],
        front3d_precheck_max_attempts_per_scene=precheck["max_attempts_per_scene"],
        front3d_precheck_min_placements=precheck["min_placements"],
        front3d_precheck_max_z=precheck["max_z_m"],
        front3d_precheck_max_footprint_ratio=precheck["max_footprint_ratio"],
        min_tables=placement["tables"][0],
        max_tables=placement["tables"][1],
        floor_extras=placement["floor_extras"],
        min_tabletop_items=placement["tabletop_items"][0],
        max_tabletop_items=placement["tabletop_items"][1],
        bistro_support_items=placement["bistro_support_items"],
        max_attempts=placement["max_attempts"],
        validate_sionna=validation["sionna"],
        quality_enabled=quality["enabled"],
        quality_fail_on_error=quality["fail_on_error"],
        quality_collision_padding=quality["collision_padding_m"],
        quality_bistro_static_clearance=quality["bistro_static_clearance_m"],
        quality_support_tolerance=quality["support_tolerance_m"],
        label_enabled=label["enabled"],
        label_version=LABEL_VERSION,
        label_ue_height=ue["height_m"],
        label_sampling_domain=ue["sampling_domain"],
        label_ue_strategy=internal_label_strategy(variants["strategies"][0]),
        label_grid_resolution=variants["grid_m"][0],
        label_batch_strategies=[internal_label_strategy(value) for value in variants["strategies"]],
        label_batch_grid_resolutions=variants["grid_m"],
        label_connected_area_enabled=variants["connected"][0],
        label_batch_connected_area_enabled=variants["connected"],
        label_ue_clearance=ue["furniture_clearance_m"],
        label_obstacle_strategy=ue["obstacle_strategy"],
        label_walk_ignore_low_obstacles_below=ue["ignore_low_obstacles_below_m"],
        label_walk_blocking_classes=ue["blocking_classes"],
        label_walk_min_component_area=ue["min_component_area_m2"],
        label_bs_strategy=bs["strategy"],
        label_bs_count_strategy=count["strategy"],
        label_bs_per_room=count["per_room"],
        label_bs_min_per_room=count["min_per_room"],
        label_bs_max_per_room=count["max_per_room"],
        label_bs_min_room_area=count["min_room_area_m2"],
        label_bs_area_per_point=count["area_per_point_m2"],
        label_bs_height=bs["height_m"],
        label_bs_ceiling_margin=bs["ceiling_margin_m"],
        label_bs_wall_clearance=bs["wall_clearance_m"],
        label_bs_center_initial_radius=center["initial_radius_m"],
        label_bs_center_radius_step=center["radius_step_m"],
        label_bs_center_max_radius=center["max_radius_m"],
        label_wall_clearance=ue["wall_clearance_m"],
        label_corridor_room_id=ue["connected_area"]["room_id"],
        label_corridor_room_type=ue["connected_area"]["room_type"],
        label_corridor_clearance=ue["wall_clearance_m"],
        label_overlay_enabled=label["overlay"]["enabled"],
        label_fail_on_error=label["fail_on_error"],
        floorplan_enabled=floorplan["enabled"],
        floorplan_geometry_enabled=geometry["enabled"],
        floorplan_geometry_clean_enabled=clean["enabled"],
        floorplan_geometry_clean_min_density=clean["min_density"],
        floorplan_geometry_clean_min_neighbors=clean["min_neighbors"],
        floorplan_geometry_clean_min_z=clean["min_z_m"],
        floorplan_geometry_clean_max_abs_normal_z=clean["max_abs_normal_z"],
        floorplan_geometry_clean_opening_px=clean["opening_px"],
        floorplan_geometry_clean_closing_px=clean["closing_px"],
        floorplan_semantic_enabled=semantic["enabled"],
        floorplan_class_mask_enabled=class_mask["enabled"],
        floorplan_class_mask_wall_dilation=class_mask["wall_dilation_m"],
        floorplan_class_mask_furniture_dilation=class_mask["furniture_dilation_m"],
        floorplan_class_mask_opening_mode=openings["mode"],
        floorplan_class_mask_opening_dilation=openings["dilation_m"],
        floorplan_class_mask_opening_floor_tolerance=openings["floor_tolerance_m"],
        floorplan_class_mask_opening_min_height=openings["min_height_m"],
        floorplan_class_mask_include_doors_as_wall=openings["include_doors_as_wall"],
        floorplan_class_mask_include_windows_as_wall=openings["include_windows_as_wall"],
        floorplan_resolution=floorplan["resolution_m"],
        floorplan_height_mode=height["mode"],
        floorplan_heights=height["values_m"],
        floorplan_step=height["step_m"],
        floorplan_top_z=height["top_m"],
        floorplan_bottom_z=height["bottom_m"],
        floorplan_sample_density_scale=sampling["density_scale"],
        floorplan_min_sample_points=sampling["min_points"],
        floorplan_max_sample_points=sampling["max_points"],
        floorplan_preview_tile_size=floorplan["preview"]["tile_size_px"],
        floorplan_semantic_padding=semantic["padding_m"],
        floorplan_semantic_draw_labels=semantic["draw_labels"],
        floorplan_fail_on_error=floorplan["fail_on_error"],
    )


def load_effective_config(config_path: Path, repo_root: Path, args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    yaml_config = load_project_config(config_path)
    merged = deep_merge(deepcopy(DEFAULT_CONFIG), yaml_config)
    overrides = cli_overrides(args)
    effective = deep_merge(merged, overrides)
    effective.setdefault("runtime", {})
    effective["runtime"]["cli_overrides"] = overrides
    normalized = normalize_effective_config(effective, repo_root, config_path)
    validate_effective_config(normalized)
    return normalized, overrides
