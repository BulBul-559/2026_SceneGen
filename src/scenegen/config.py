from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

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
        "forbidden_xy_rects": [
            [1.0, 11.0, 4.5, 16.0],
            [8.0, 8.0, 14.0, 10.0],
        ],
    },
    "front3d": {
        "manifest": "data/3D-Front/scenegen_manifest.json",
        "source_scene_dir": "data/3D-Front/3D-FRONT",
        "variant": "normalized",
        "object_variant": "raw",
        "scene_ids": [],
        "scene_selection": "random",
        "use_replace_jid": True,
        "skip_missing_objects": True,
        "normalize_positive_xy": True,
        "ground_objects": True,
        "precheck_enabled": True,
        "precheck_max_attempts_per_scene": 20,
        "precheck_min_placements": 1,
        "precheck_max_z_m": 8.0,
        "precheck_max_footprint_ratio": 5.0,
    },
    "placement": {
        "min_tables": 4,
        "max_tables": 8,
        "floor_extras": 6,
        "min_tabletop_items": 3,
        "max_tabletop_items": 9,
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
        "version": "1.1",
        "ue_height_m": 1.6,
        "sampling_domain": "global_floor",
        "ue_strategy": "free_space_grid",
        "grid_resolution_m": 0.1,
        "batch_strategies": ["free_space_grid"],
        "batch_grid_resolutions_m": [0.1],
        "ue_clearance_m": 0.35,
        "obstacle_strategy": "height_aware",
        "walk_ignore_low_obstacles_below_m": 0.10,
        "walk_blocking_classes": ["table", "seat", "floor"],
        "walk_min_component_area_m2": 0.25,
        "bs_strategy": "wall_or_corner",
        "bs_count_strategy": "fixed_per_room",
        "bs_per_room": 4,
        "bs_min_per_room": 1,
        "bs_max_per_room": 8,
        "bs_min_room_area_m2": 4.0,
        "bs_area_per_point_m2": 12.0,
        "bs_height_m": 2.4,
        "bs_ceiling_margin_m": 0.3,
        "bs_wall_clearance_m": 0.25,
        "bs_center_initial_radius_m": 0.2,
        "bs_center_radius_step_m": 0.1,
        "bs_center_max_radius_m": 2.0,
        "wall_clearance_m": 0.25,
        "corridor_room_id": "__corridor__",
        "corridor_room_type": "ConnectedArea",
        "corridor_clearance_m": 0.05,
        "overlay_enabled": True,
        "fail_on_error": True,
    },
    "floorplan": {
        "enabled": True,
        "geometry_enabled": True,
        "geometry_clean_enabled": False,
        "geometry_clean_min_density": 2.0,
        "geometry_clean_min_neighbors": 2,
        "geometry_clean_min_z_m": 0.05,
        "geometry_clean_max_abs_normal_z": 0.7,
        "geometry_clean_opening_px": 0,
        "geometry_clean_closing_px": 1,
        "semantic_enabled": False,
        "class_mask_enabled": False,
        "class_mask_wall_dilation_m": 0.0,
        "class_mask_furniture_dilation_m": 0.0,
        "class_mask_opening_mode": "doors",
        "class_mask_opening_dilation_m": 0.0,
        "class_mask_opening_floor_tolerance_m": 0.25,
        "class_mask_opening_min_height_m": 1.6,
        "class_mask_include_doors_as_wall": True,
        "class_mask_include_windows_as_wall": True,
        "resolution_m_per_pixel": 0.05,
        "height_mode": "heights",
        "heights_m": [1.6],
        "step_m": 0.2,
        "top_z_m": None,
        "bottom_z_m": 0.0,
        "sample_density_scale": 128.0,
        "min_sample_points": 100_000,
        "max_sample_points": 25_000_000,
        "preview_tile_size_px": 360,
        "semantic_padding_m": 0.5,
        "semantic_draw_labels": True,
        "fail_on_error": True,
    },
}


CLI_OVERRIDE_MAP: dict[str, tuple[str, ...]] = {
    "mode": ("pipeline", "mode"),
    "scenes": ("pipeline", "scenes"),
    "seed": ("pipeline", "seed"),
    "output_dir": ("pipeline", "output_dir"),
    "run_name": ("pipeline", "run_name"),
    "clean": ("pipeline", "clean"),
    "asset_manifest": ("assets", "catalog"),
    "asset_catalog": ("assets", "catalog"),
    "bistro_base_dir": ("bistro", "base_dir"),
    "front3d_manifest": ("front3d", "manifest"),
    "front3d_source_scene_dir": ("front3d", "source_scene_dir"),
    "front3d_variant": ("front3d", "variant"),
    "front3d_object_variant": ("front3d", "object_variant"),
    "front3d_scene_ids": ("front3d", "scene_ids"),
    "front3d_scene_selection": ("front3d", "scene_selection"),
    "front3d_use_replace_jid": ("front3d", "use_replace_jid"),
    "front3d_skip_missing_objects": ("front3d", "skip_missing_objects"),
    "front3d_normalize_positive_xy": ("front3d", "normalize_positive_xy"),
    "front3d_ground_objects": ("front3d", "ground_objects"),
    "front3d_precheck_enabled": ("front3d", "precheck_enabled"),
    "front3d_precheck_max_attempts_per_scene": ("front3d", "precheck_max_attempts_per_scene"),
    "front3d_precheck_min_placements": ("front3d", "precheck_min_placements"),
    "front3d_precheck_max_z": ("front3d", "precheck_max_z_m"),
    "front3d_precheck_max_footprint_ratio": ("front3d", "precheck_max_footprint_ratio"),
    "min_tables": ("placement", "min_tables"),
    "max_tables": ("placement", "max_tables"),
    "floor_extras": ("placement", "floor_extras"),
    "min_tabletop_items": ("placement", "min_tabletop_items"),
    "max_tabletop_items": ("placement", "max_tabletop_items"),
    "bistro_support_items": ("placement", "bistro_support_items"),
    "max_attempts": ("placement", "max_attempts"),
    "validate_sionna": ("validation", "sionna"),
    "quality_enabled": ("quality", "enabled"),
    "quality_fail_on_error": ("quality", "fail_on_error"),
    "quality_collision_padding": ("quality", "collision_padding_m"),
    "quality_bistro_static_clearance": ("quality", "bistro_static_clearance_m"),
    "quality_support_tolerance": ("quality", "support_tolerance_m"),
    "label_enabled": ("label", "enabled"),
    "label_version": ("label", "version"),
    "label_ue_height": ("label", "ue_height_m"),
    "label_sampling_domain": ("label", "sampling_domain"),
    "label_ue_strategy": ("label", "ue_strategy"),
    "label_grid_resolution": ("label", "grid_resolution_m"),
    "label_batch_strategies": ("label", "batch_strategies"),
    "label_batch_grid_resolutions": ("label", "batch_grid_resolutions_m"),
    "label_ue_clearance": ("label", "ue_clearance_m"),
    "label_obstacle_strategy": ("label", "obstacle_strategy"),
    "label_walk_ignore_low_obstacles_below": ("label", "walk_ignore_low_obstacles_below_m"),
    "label_walk_blocking_classes": ("label", "walk_blocking_classes"),
    "label_walk_min_component_area": ("label", "walk_min_component_area_m2"),
    "label_bs_strategy": ("label", "bs_strategy"),
    "label_bs_count_strategy": ("label", "bs_count_strategy"),
    "label_bs_per_room": ("label", "bs_per_room"),
    "label_bs_min_per_room": ("label", "bs_min_per_room"),
    "label_bs_max_per_room": ("label", "bs_max_per_room"),
    "label_bs_min_room_area": ("label", "bs_min_room_area_m2"),
    "label_bs_area_per_point": ("label", "bs_area_per_point_m2"),
    "label_bs_height": ("label", "bs_height_m"),
    "label_bs_ceiling_margin": ("label", "bs_ceiling_margin_m"),
    "label_bs_wall_clearance": ("label", "bs_wall_clearance_m"),
    "label_bs_center_initial_radius": ("label", "bs_center_initial_radius_m"),
    "label_bs_center_radius_step": ("label", "bs_center_radius_step_m"),
    "label_bs_center_max_radius": ("label", "bs_center_max_radius_m"),
    "label_wall_clearance": ("label", "wall_clearance_m"),
    "label_corridor_room_id": ("label", "corridor_room_id"),
    "label_corridor_room_type": ("label", "corridor_room_type"),
    "label_corridor_clearance": ("label", "corridor_clearance_m"),
    "label_overlay_enabled": ("label", "overlay_enabled"),
    "label_fail_on_error": ("label", "fail_on_error"),
    "floorplan_enabled": ("floorplan", "enabled"),
    "floorplan_geometry_enabled": ("floorplan", "geometry_enabled"),
    "floorplan_geometry_clean_enabled": ("floorplan", "geometry_clean_enabled"),
    "floorplan_geometry_clean_min_density": ("floorplan", "geometry_clean_min_density"),
    "floorplan_geometry_clean_min_neighbors": ("floorplan", "geometry_clean_min_neighbors"),
    "floorplan_geometry_clean_min_z": ("floorplan", "geometry_clean_min_z_m"),
    "floorplan_geometry_clean_max_abs_normal_z": ("floorplan", "geometry_clean_max_abs_normal_z"),
    "floorplan_geometry_clean_opening_px": ("floorplan", "geometry_clean_opening_px"),
    "floorplan_geometry_clean_closing_px": ("floorplan", "geometry_clean_closing_px"),
    "floorplan_semantic_enabled": ("floorplan", "semantic_enabled"),
    "floorplan_class_mask_enabled": ("floorplan", "class_mask_enabled"),
    "floorplan_class_mask_wall_dilation": ("floorplan", "class_mask_wall_dilation_m"),
    "floorplan_class_mask_furniture_dilation": ("floorplan", "class_mask_furniture_dilation_m"),
    "floorplan_class_mask_opening_mode": ("floorplan", "class_mask_opening_mode"),
    "floorplan_class_mask_opening_dilation": ("floorplan", "class_mask_opening_dilation_m"),
    "floorplan_class_mask_opening_floor_tolerance": ("floorplan", "class_mask_opening_floor_tolerance_m"),
    "floorplan_class_mask_opening_min_height": ("floorplan", "class_mask_opening_min_height_m"),
    "floorplan_class_mask_include_doors_as_wall": ("floorplan", "class_mask_include_doors_as_wall"),
    "floorplan_class_mask_include_windows_as_wall": ("floorplan", "class_mask_include_windows_as_wall"),
    "floorplan_resolution": ("floorplan", "resolution_m_per_pixel"),
    "floorplan_height_mode": ("floorplan", "height_mode"),
    "floorplan_heights": ("floorplan", "heights_m"),
    "floorplan_step": ("floorplan", "step_m"),
    "floorplan_top_z": ("floorplan", "top_z_m"),
    "floorplan_bottom_z": ("floorplan", "bottom_z_m"),
    "floorplan_sample_density_scale": ("floorplan", "sample_density_scale"),
    "floorplan_min_sample_points": ("floorplan", "min_sample_points"),
    "floorplan_max_sample_points": ("floorplan", "max_sample_points"),
    "floorplan_preview_tile_size": ("floorplan", "preview_tile_size_px"),
    "floorplan_semantic_padding": ("floorplan", "semantic_padding_m"),
    "floorplan_semantic_draw_labels": ("floorplan", "semantic_draw_labels"),
    "floorplan_fail_on_error": ("floorplan", "fail_on_error"),
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


def validate_known_config_fields(config: dict[str, Any], config_path: Path) -> None:
    unknown = unknown_config_fields(config)
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise ValueError(f"Unknown config field(s) in {config_path}: {joined}")


def load_project_config(config_path: Path) -> dict[str, Any]:
    payload = upgrade_config_aliases(load_yaml_config(config_path))
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


def stringify_path(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return value


def upgrade_config_aliases(config: dict[str, Any]) -> dict[str, Any]:
    upgraded = deepcopy(config)
    assets = upgraded.get("assets")
    if isinstance(assets, dict):
        if "catalog" not in assets and "manifest" in assets:
            assets["catalog"] = assets["manifest"]
        assets.pop("manifest", None)
    return upgraded


def cli_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for attr, path in CLI_OVERRIDE_MAP.items():
        value = getattr(args, attr, None)
        if value is None:
            continue
        normalized = stringify_path(value)
        set_nested(overrides, path, normalized)
    return overrides


def resolve_path(repo_root: Path, value: Any) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (repo_root / path).resolve()


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
        raise ValueError("bistro.forbidden_xy_rects must be a list")
    rects: list[tuple[float, float, float, float]] = []
    for item in value:
        if not isinstance(item, list | tuple) or len(item) != 4:
            raise ValueError("Each bistro.forbidden_xy_rects item must be [x_min, y_min, x_max, y_max]")
        x_min, y_min, x_max, y_max = (float(part) for part in item)
        if x_max < x_min or y_max < y_min:
            raise ValueError(f"Invalid forbidden rectangle: {item}")
        rects.append((x_min, y_min, x_max, y_max))
    return tuple(rects)


def parse_float_sequence(value: Any, key: str) -> list[float]:
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list | tuple):
        parts = list(value)
    else:
        raise ValueError(f"{key} must be a list or comma-separated string")
    if not parts:
        raise ValueError(f"{key} must not be empty")
    return [float(part) for part in parts]


def parse_string_sequence(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list | tuple):
        parts = [str(part).strip() for part in value if str(part).strip()]
    else:
        raise ValueError(f"{key} must be a list or comma-separated string")
    return parts


def normalize_effective_config(config: dict[str, Any], repo_root: Path, config_path: Path) -> dict[str, Any]:
    normalized = deepcopy(config)
    normalized.setdefault("runtime", {})
    normalized["runtime"]["config_path"] = str(config_path.resolve())

    normalized["pipeline"]["output_dir"] = str(resolve_path(repo_root, normalized["pipeline"]["output_dir"]))
    assets = normalized.setdefault("assets", {})
    catalog_value = assets.get("catalog") or assets.get("manifest") or DEFAULT_CONFIG["assets"]["catalog"]
    assets["catalog"] = str(resolve_path(repo_root, catalog_value))
    assets.pop("manifest", None)
    normalized["bistro"]["base_dir"] = str(resolve_path(repo_root, normalized["bistro"]["base_dir"]))
    front3d = normalized.setdefault("front3d", deepcopy(DEFAULT_CONFIG["front3d"]))
    front3d["manifest"] = str(resolve_path(repo_root, front3d["manifest"]))
    front3d["source_scene_dir"] = str(resolve_path(repo_root, front3d["source_scene_dir"]))

    normalized["pipeline"]["mode"] = str(normalized["pipeline"]["mode"])
    normalized["pipeline"]["scenes"] = int(normalized["pipeline"]["scenes"])
    normalized["pipeline"]["seed"] = int(normalized["pipeline"]["seed"])
    normalized["pipeline"]["clean"] = as_bool(normalized["pipeline"]["clean"], "pipeline.clean")

    placement = normalized["placement"]
    for key in (
        "min_tables",
        "max_tables",
        "floor_extras",
        "min_tabletop_items",
        "max_tabletop_items",
        "bistro_support_items",
        "max_attempts",
    ):
        placement[key] = int(placement[key])

    normalized["validation"]["sionna"] = as_bool(normalized["validation"]["sionna"], "validation.sionna")

    quality = normalized["quality"]
    quality["enabled"] = as_bool(quality["enabled"], "quality.enabled")
    quality["fail_on_error"] = as_bool(quality["fail_on_error"], "quality.fail_on_error")
    quality["collision_padding_m"] = float(quality["collision_padding_m"])
    quality["bistro_static_clearance_m"] = float(quality["bistro_static_clearance_m"])
    quality["support_tolerance_m"] = float(quality["support_tolerance_m"])

    label = normalized["label"]
    label["enabled"] = as_bool(label["enabled"], "label.enabled")
    label["version"] = str(label["version"])
    label["ue_height_m"] = float(label["ue_height_m"])
    label["sampling_domain"] = str(label["sampling_domain"])
    label["ue_strategy"] = str(label["ue_strategy"])
    label["grid_resolution_m"] = float(label["grid_resolution_m"])
    label["batch_strategies"] = parse_string_sequence(label["batch_strategies"], "label.batch_strategies")
    label["batch_grid_resolutions_m"] = parse_float_sequence(
        label["batch_grid_resolutions_m"], "label.batch_grid_resolutions_m"
    )
    label["ue_clearance_m"] = float(label["ue_clearance_m"])
    label["obstacle_strategy"] = str(label["obstacle_strategy"])
    label["walk_ignore_low_obstacles_below_m"] = float(label["walk_ignore_low_obstacles_below_m"])
    label["walk_blocking_classes"] = parse_string_sequence(label["walk_blocking_classes"], "label.walk_blocking_classes")
    label["walk_min_component_area_m2"] = float(label["walk_min_component_area_m2"])
    label["bs_strategy"] = str(label["bs_strategy"])
    label["bs_count_strategy"] = str(label["bs_count_strategy"])
    label["bs_per_room"] = int(label["bs_per_room"])
    label["bs_min_per_room"] = int(label["bs_min_per_room"])
    label["bs_max_per_room"] = int(label["bs_max_per_room"])
    label["bs_min_room_area_m2"] = float(label["bs_min_room_area_m2"])
    label["bs_area_per_point_m2"] = float(label["bs_area_per_point_m2"])
    label["bs_height_m"] = float(label["bs_height_m"])
    label["bs_ceiling_margin_m"] = float(label["bs_ceiling_margin_m"])
    label["bs_wall_clearance_m"] = float(label["bs_wall_clearance_m"])
    label["bs_center_initial_radius_m"] = float(label["bs_center_initial_radius_m"])
    label["bs_center_radius_step_m"] = float(label["bs_center_radius_step_m"])
    label["bs_center_max_radius_m"] = float(label["bs_center_max_radius_m"])
    label["wall_clearance_m"] = float(label["wall_clearance_m"])
    label["corridor_room_id"] = str(label["corridor_room_id"])
    label["corridor_room_type"] = str(label["corridor_room_type"])
    label["corridor_clearance_m"] = float(label["corridor_clearance_m"])
    label["overlay_enabled"] = as_bool(label["overlay_enabled"], "label.overlay_enabled")
    label["fail_on_error"] = as_bool(label["fail_on_error"], "label.fail_on_error")

    floorplan = normalized["floorplan"]
    floorplan["enabled"] = as_bool(floorplan["enabled"], "floorplan.enabled")
    floorplan["geometry_enabled"] = as_bool(floorplan["geometry_enabled"], "floorplan.geometry_enabled")
    floorplan["geometry_clean_enabled"] = as_bool(
        floorplan["geometry_clean_enabled"], "floorplan.geometry_clean_enabled"
    )
    floorplan["geometry_clean_min_density"] = float(floorplan["geometry_clean_min_density"])
    floorplan["geometry_clean_min_neighbors"] = int(floorplan["geometry_clean_min_neighbors"])
    floorplan["geometry_clean_min_z_m"] = float(floorplan["geometry_clean_min_z_m"])
    floorplan["geometry_clean_max_abs_normal_z"] = float(floorplan["geometry_clean_max_abs_normal_z"])
    floorplan["geometry_clean_opening_px"] = int(floorplan["geometry_clean_opening_px"])
    floorplan["geometry_clean_closing_px"] = int(floorplan["geometry_clean_closing_px"])
    floorplan["semantic_enabled"] = as_bool(floorplan["semantic_enabled"], "floorplan.semantic_enabled")
    floorplan["class_mask_enabled"] = as_bool(floorplan["class_mask_enabled"], "floorplan.class_mask_enabled")
    floorplan["class_mask_wall_dilation_m"] = float(floorplan["class_mask_wall_dilation_m"])
    floorplan["class_mask_furniture_dilation_m"] = float(floorplan["class_mask_furniture_dilation_m"])
    floorplan["class_mask_opening_mode"] = str(floorplan["class_mask_opening_mode"])
    floorplan["class_mask_opening_dilation_m"] = float(floorplan["class_mask_opening_dilation_m"])
    floorplan["class_mask_opening_floor_tolerance_m"] = float(floorplan["class_mask_opening_floor_tolerance_m"])
    floorplan["class_mask_opening_min_height_m"] = float(floorplan["class_mask_opening_min_height_m"])
    floorplan["class_mask_include_doors_as_wall"] = as_bool(
        floorplan["class_mask_include_doors_as_wall"], "floorplan.class_mask_include_doors_as_wall"
    )
    floorplan["class_mask_include_windows_as_wall"] = as_bool(
        floorplan["class_mask_include_windows_as_wall"], "floorplan.class_mask_include_windows_as_wall"
    )
    floorplan["resolution_m_per_pixel"] = float(floorplan["resolution_m_per_pixel"])
    floorplan["height_mode"] = str(floorplan["height_mode"])
    floorplan["heights_m"] = parse_float_sequence(floorplan["heights_m"], "floorplan.heights_m")
    floorplan["step_m"] = float(floorplan["step_m"])
    floorplan["top_z_m"] = None if floorplan["top_z_m"] is None else float(floorplan["top_z_m"])
    floorplan["bottom_z_m"] = float(floorplan["bottom_z_m"])
    floorplan["sample_density_scale"] = float(floorplan["sample_density_scale"])
    floorplan["min_sample_points"] = int(floorplan["min_sample_points"])
    floorplan["max_sample_points"] = int(floorplan["max_sample_points"])
    floorplan["preview_tile_size_px"] = int(floorplan["preview_tile_size_px"])
    floorplan["semantic_padding_m"] = float(floorplan["semantic_padding_m"])
    floorplan["semantic_draw_labels"] = as_bool(floorplan["semantic_draw_labels"], "floorplan.semantic_draw_labels")
    floorplan["fail_on_error"] = as_bool(floorplan["fail_on_error"], "floorplan.fail_on_error")

    normalized["bistro"]["forbidden_xy_rects"] = [
        list(rect) for rect in parse_forbidden_rects(normalized["bistro"].get("forbidden_xy_rects"))
    ]
    front3d["variant"] = str(front3d["variant"])
    front3d["object_variant"] = str(front3d["object_variant"])
    front3d["scene_ids"] = parse_string_sequence(front3d.get("scene_ids"), "front3d.scene_ids")
    front3d["scene_selection"] = str(front3d["scene_selection"])
    front3d["use_replace_jid"] = as_bool(front3d["use_replace_jid"], "front3d.use_replace_jid")
    front3d["skip_missing_objects"] = as_bool(front3d["skip_missing_objects"], "front3d.skip_missing_objects")
    front3d["normalize_positive_xy"] = as_bool(front3d["normalize_positive_xy"], "front3d.normalize_positive_xy")
    front3d["ground_objects"] = as_bool(front3d["ground_objects"], "front3d.ground_objects")
    front3d["precheck_enabled"] = as_bool(front3d["precheck_enabled"], "front3d.precheck_enabled")
    front3d["precheck_max_attempts_per_scene"] = int(front3d["precheck_max_attempts_per_scene"])
    front3d["precheck_min_placements"] = int(front3d["precheck_min_placements"])
    front3d["precheck_max_z_m"] = float(front3d["precheck_max_z_m"])
    front3d["precheck_max_footprint_ratio"] = float(front3d["precheck_max_footprint_ratio"])
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
    if placement["min_tables"] < 0 or placement["max_tables"] < placement["min_tables"]:
        raise ValueError("placement.max_tables must be greater than or equal to placement.min_tables")
    if placement["floor_extras"] < 0:
        raise ValueError("placement.floor_extras must be non-negative")
    if placement["min_tabletop_items"] < 0 or placement["max_tabletop_items"] < placement["min_tabletop_items"]:
        raise ValueError("placement.max_tabletop_items must be greater than or equal to placement.min_tabletop_items")
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

    label = config["label"]
    if label["version"] != "1.1":
        raise ValueError("label.version must be '1.1'")
    if label["ue_height_m"] <= 0:
        raise ValueError("label.ue_height_m must be positive")
    if label["sampling_domain"] not in {"room_floor", "global_floor"}:
        raise ValueError("label.sampling_domain must be 'room_floor' or 'global_floor'")
    if label["ue_strategy"] not in {"free_space_grid", "plane_grid"}:
        raise ValueError("label.ue_strategy must be 'free_space_grid' or 'plane_grid'")
    if label["grid_resolution_m"] <= 0:
        raise ValueError("label.grid_resolution_m must be positive")
    if any(strategy not in {"free_space_grid", "plane_grid"} for strategy in label["batch_strategies"]):
        raise ValueError("label.batch_strategies values must be 'free_space_grid' or 'plane_grid'")
    if any(resolution <= 0 for resolution in label["batch_grid_resolutions_m"]):
        raise ValueError("label.batch_grid_resolutions_m values must be positive")
    if label["ue_clearance_m"] < 0:
        raise ValueError("label.ue_clearance_m must be non-negative")
    if label["obstacle_strategy"] not in {"height_aware", "footprint_column"}:
        raise ValueError("label.obstacle_strategy must be 'height_aware' or 'footprint_column'")
    if label["walk_ignore_low_obstacles_below_m"] < 0:
        raise ValueError("label.walk_ignore_low_obstacles_below_m must be non-negative")
    if not label["walk_blocking_classes"]:
        raise ValueError("label.walk_blocking_classes must not be empty")
    allowed_walk_classes = {"table", "seat", "tabletop", "floor", "skip"}
    if any(value not in allowed_walk_classes for value in label["walk_blocking_classes"]):
        raise ValueError("label.walk_blocking_classes values must be table, seat, tabletop, floor, or skip")
    if label["walk_min_component_area_m2"] < 0:
        raise ValueError("label.walk_min_component_area_m2 must be non-negative")
    if label["bs_strategy"] not in {"wall_or_corner", "geometry_center"}:
        raise ValueError("label.bs_strategy must be 'wall_or_corner' or 'geometry_center'")
    if label["bs_count_strategy"] not in {"fixed_per_room", "area_adaptive"}:
        raise ValueError("label.bs_count_strategy must be 'fixed_per_room' or 'area_adaptive'")
    if label["bs_per_room"] < 0:
        raise ValueError("label.bs_per_room must be non-negative")
    if label["bs_min_per_room"] < 0:
        raise ValueError("label.bs_min_per_room must be non-negative")
    if label["bs_max_per_room"] < label["bs_min_per_room"]:
        raise ValueError("label.bs_max_per_room must be greater than or equal to label.bs_min_per_room")
    if label["bs_min_room_area_m2"] < 0:
        raise ValueError("label.bs_min_room_area_m2 must be non-negative")
    if label["bs_area_per_point_m2"] <= 0:
        raise ValueError("label.bs_area_per_point_m2 must be positive")
    if label["bs_height_m"] <= 0:
        raise ValueError("label.bs_height_m must be positive")
    if label["bs_ceiling_margin_m"] < 0:
        raise ValueError("label.bs_ceiling_margin_m must be non-negative")
    if label["bs_wall_clearance_m"] < 0:
        raise ValueError("label.bs_wall_clearance_m must be non-negative")
    if label["bs_center_initial_radius_m"] < 0:
        raise ValueError("label.bs_center_initial_radius_m must be non-negative")
    if label["bs_center_radius_step_m"] <= 0:
        raise ValueError("label.bs_center_radius_step_m must be positive")
    if label["bs_center_max_radius_m"] < label["bs_center_initial_radius_m"]:
        raise ValueError("label.bs_center_max_radius_m must be greater than or equal to label.bs_center_initial_radius_m")
    if label["wall_clearance_m"] < 0:
        raise ValueError("label.wall_clearance_m must be non-negative")
    if not label["corridor_room_id"].strip():
        raise ValueError("label.corridor_room_id must not be empty")
    if not label["corridor_room_type"].strip():
        raise ValueError("label.corridor_room_type must not be empty")
    if label["corridor_clearance_m"] < 0:
        raise ValueError("label.corridor_clearance_m must be non-negative")

    floorplan = config["floorplan"]
    if floorplan["resolution_m_per_pixel"] <= 0:
        raise ValueError("floorplan.resolution_m_per_pixel must be positive")
    if floorplan["height_mode"] not in {"layers", "heights"}:
        raise ValueError("floorplan.height_mode must be 'layers' or 'heights'")
    if floorplan["height_mode"] == "heights":
        if not floorplan["heights_m"]:
            raise ValueError("floorplan.heights_m must contain at least one height when height_mode is 'heights'")
        if any(height < floorplan["bottom_z_m"] for height in floorplan["heights_m"]):
            raise ValueError("floorplan.heights_m values must be greater than or equal to floorplan.bottom_z_m")
    if floorplan["top_z_m"] is not None and floorplan["top_z_m"] < floorplan["bottom_z_m"]:
        raise ValueError("floorplan.top_z_m must be greater than or equal to floorplan.bottom_z_m")
    if floorplan["step_m"] <= 0:
        raise ValueError("floorplan.step_m must be positive")
    if floorplan["sample_density_scale"] <= 0:
        raise ValueError("floorplan.sample_density_scale must be positive")
    if floorplan["min_sample_points"] < 1:
        raise ValueError("floorplan.min_sample_points must be positive")
    if floorplan["max_sample_points"] < floorplan["min_sample_points"]:
        raise ValueError("floorplan.max_sample_points must be greater than or equal to floorplan.min_sample_points")
    if floorplan["enabled"] and not (
        floorplan["geometry_enabled"] or floorplan["semantic_enabled"] or floorplan["class_mask_enabled"]
    ):
        raise ValueError(
            "At least one of floorplan.geometry_enabled, floorplan.semantic_enabled, "
            "or floorplan.class_mask_enabled must be true"
        )
    if floorplan["geometry_clean_enabled"] and not floorplan["geometry_enabled"]:
        raise ValueError("floorplan.geometry_clean_enabled requires floorplan.geometry_enabled")
    if floorplan["geometry_clean_min_density"] < 0:
        raise ValueError("floorplan.geometry_clean_min_density must be non-negative")
    if floorplan["geometry_clean_min_neighbors"] < 0:
        raise ValueError("floorplan.geometry_clean_min_neighbors must be non-negative")
    if floorplan["geometry_clean_min_z_m"] < 0:
        raise ValueError("floorplan.geometry_clean_min_z_m must be non-negative")
    if not 0 <= floorplan["geometry_clean_max_abs_normal_z"] <= 1:
        raise ValueError("floorplan.geometry_clean_max_abs_normal_z must be between 0 and 1")
    if floorplan["geometry_clean_opening_px"] < 0:
        raise ValueError("floorplan.geometry_clean_opening_px must be non-negative")
    if floorplan["geometry_clean_closing_px"] < 0:
        raise ValueError("floorplan.geometry_clean_closing_px must be non-negative")
    if floorplan["preview_tile_size_px"] < 1:
        raise ValueError("floorplan.preview_tile_size_px must be positive")
    if floorplan["semantic_padding_m"] < 0:
        raise ValueError("floorplan.semantic_padding_m must be non-negative")
    if floorplan["class_mask_enabled"] and mode != "front3d":
        raise ValueError("floorplan.class_mask_enabled currently supports only front3d mode")
    if floorplan["class_mask_wall_dilation_m"] < 0:
        raise ValueError("floorplan.class_mask_wall_dilation_m must be non-negative")
    if floorplan["class_mask_furniture_dilation_m"] < 0:
        raise ValueError("floorplan.class_mask_furniture_dilation_m must be non-negative")
    if floorplan["class_mask_opening_mode"] not in {"none", "doors", "windows", "doors_and_windows"}:
        raise ValueError(
            "floorplan.class_mask_opening_mode must be 'none', 'doors', 'windows', or 'doors_and_windows'"
        )
    if floorplan["class_mask_opening_dilation_m"] < 0:
        raise ValueError("floorplan.class_mask_opening_dilation_m must be non-negative")
    if floorplan["class_mask_opening_floor_tolerance_m"] < 0:
        raise ValueError("floorplan.class_mask_opening_floor_tolerance_m must be non-negative")
    if floorplan["class_mask_opening_min_height_m"] < 0:
        raise ValueError("floorplan.class_mask_opening_min_height_m must be non-negative")

    front3d = config["front3d"]
    if front3d["variant"] not in {"raw", "normalized"}:
        raise ValueError("front3d.variant must be 'raw' or 'normalized'")
    if front3d["object_variant"] not in {"raw", "normalized"}:
        raise ValueError("front3d.object_variant must be 'raw' or 'normalized'")
    if front3d["scene_selection"] not in {"random", "sequential"}:
        raise ValueError("front3d.scene_selection must be 'random' or 'sequential'")
    if front3d["precheck_max_attempts_per_scene"] < 1:
        raise ValueError("front3d.precheck_max_attempts_per_scene must be at least 1")
    if front3d["precheck_min_placements"] < 0:
        raise ValueError("front3d.precheck_min_placements must be non-negative")
    if front3d["precheck_max_z_m"] <= 0:
        raise ValueError("front3d.precheck_max_z_m must be positive")
    if front3d["precheck_max_footprint_ratio"] <= 0:
        raise ValueError("front3d.precheck_max_footprint_ratio must be positive")


def save_effective_config(path: Path, config: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def config_to_namespace(config: dict[str, Any]) -> argparse.Namespace:
    pipeline = config["pipeline"]
    assets = config["assets"]
    bistro = config["bistro"]
    front3d = config["front3d"]
    placement = config["placement"]
    validation = config["validation"]
    quality = config["quality"]
    label = config["label"]
    floorplan = config["floorplan"]
    return argparse.Namespace(
        mode=pipeline["mode"],
        scenes=pipeline["scenes"],
        seed=pipeline["seed"],
        output_dir=Path(pipeline["output_dir"]),
        run_name=pipeline["run_name"],
        clean=pipeline["clean"],
        asset_catalog=Path(assets["catalog"]),
        asset_manifest=Path(assets["catalog"]),
        bistro_base_dir=Path(bistro["base_dir"]),
        forbidden_xy_rects=parse_forbidden_rects(bistro.get("forbidden_xy_rects")),
        front3d_manifest=Path(front3d["manifest"]),
        front3d_source_scene_dir=Path(front3d["source_scene_dir"]),
        front3d_variant=front3d["variant"],
        front3d_object_variant=front3d["object_variant"],
        front3d_scene_ids=front3d["scene_ids"],
        front3d_scene_selection=front3d["scene_selection"],
        front3d_use_replace_jid=front3d["use_replace_jid"],
        front3d_skip_missing_objects=front3d["skip_missing_objects"],
        front3d_normalize_positive_xy=front3d["normalize_positive_xy"],
        front3d_ground_objects=front3d["ground_objects"],
        front3d_precheck_enabled=front3d["precheck_enabled"],
        front3d_precheck_max_attempts_per_scene=front3d["precheck_max_attempts_per_scene"],
        front3d_precheck_min_placements=front3d["precheck_min_placements"],
        front3d_precheck_max_z=front3d["precheck_max_z_m"],
        front3d_precheck_max_footprint_ratio=front3d["precheck_max_footprint_ratio"],
        min_tables=placement["min_tables"],
        max_tables=placement["max_tables"],
        floor_extras=placement["floor_extras"],
        min_tabletop_items=placement["min_tabletop_items"],
        max_tabletop_items=placement["max_tabletop_items"],
        bistro_support_items=placement["bistro_support_items"],
        max_attempts=placement["max_attempts"],
        validate_sionna=validation["sionna"],
        quality_enabled=quality["enabled"],
        quality_fail_on_error=quality["fail_on_error"],
        quality_collision_padding=quality["collision_padding_m"],
        quality_bistro_static_clearance=quality["bistro_static_clearance_m"],
        quality_support_tolerance=quality["support_tolerance_m"],
        label_enabled=label["enabled"],
        label_version=label["version"],
        label_ue_height=label["ue_height_m"],
        label_sampling_domain=label["sampling_domain"],
        label_ue_strategy=label["ue_strategy"],
        label_grid_resolution=label["grid_resolution_m"],
        label_batch_strategies=label["batch_strategies"],
        label_batch_grid_resolutions=label["batch_grid_resolutions_m"],
        label_ue_clearance=label["ue_clearance_m"],
        label_obstacle_strategy=label["obstacle_strategy"],
        label_walk_ignore_low_obstacles_below=label["walk_ignore_low_obstacles_below_m"],
        label_walk_blocking_classes=label["walk_blocking_classes"],
        label_walk_min_component_area=label["walk_min_component_area_m2"],
        label_bs_strategy=label["bs_strategy"],
        label_bs_count_strategy=label["bs_count_strategy"],
        label_bs_per_room=label["bs_per_room"],
        label_bs_min_per_room=label["bs_min_per_room"],
        label_bs_max_per_room=label["bs_max_per_room"],
        label_bs_min_room_area=label["bs_min_room_area_m2"],
        label_bs_area_per_point=label["bs_area_per_point_m2"],
        label_bs_height=label["bs_height_m"],
        label_bs_ceiling_margin=label["bs_ceiling_margin_m"],
        label_bs_wall_clearance=label["bs_wall_clearance_m"],
        label_bs_center_initial_radius=label["bs_center_initial_radius_m"],
        label_bs_center_radius_step=label["bs_center_radius_step_m"],
        label_bs_center_max_radius=label["bs_center_max_radius_m"],
        label_wall_clearance=label["wall_clearance_m"],
        label_corridor_room_id=label["corridor_room_id"],
        label_corridor_room_type=label["corridor_room_type"],
        label_corridor_clearance=label["corridor_clearance_m"],
        label_overlay_enabled=label["overlay_enabled"],
        label_fail_on_error=label["fail_on_error"],
        floorplan_enabled=floorplan["enabled"],
        floorplan_geometry_enabled=floorplan["geometry_enabled"],
        floorplan_geometry_clean_enabled=floorplan["geometry_clean_enabled"],
        floorplan_geometry_clean_min_density=floorplan["geometry_clean_min_density"],
        floorplan_geometry_clean_min_neighbors=floorplan["geometry_clean_min_neighbors"],
        floorplan_geometry_clean_min_z=floorplan["geometry_clean_min_z_m"],
        floorplan_geometry_clean_max_abs_normal_z=floorplan["geometry_clean_max_abs_normal_z"],
        floorplan_geometry_clean_opening_px=floorplan["geometry_clean_opening_px"],
        floorplan_geometry_clean_closing_px=floorplan["geometry_clean_closing_px"],
        floorplan_semantic_enabled=floorplan["semantic_enabled"],
        floorplan_class_mask_enabled=floorplan["class_mask_enabled"],
        floorplan_class_mask_wall_dilation=floorplan["class_mask_wall_dilation_m"],
        floorplan_class_mask_furniture_dilation=floorplan["class_mask_furniture_dilation_m"],
        floorplan_class_mask_opening_mode=floorplan["class_mask_opening_mode"],
        floorplan_class_mask_opening_dilation=floorplan["class_mask_opening_dilation_m"],
        floorplan_class_mask_opening_floor_tolerance=floorplan["class_mask_opening_floor_tolerance_m"],
        floorplan_class_mask_opening_min_height=floorplan["class_mask_opening_min_height_m"],
        floorplan_class_mask_include_doors_as_wall=floorplan["class_mask_include_doors_as_wall"],
        floorplan_class_mask_include_windows_as_wall=floorplan["class_mask_include_windows_as_wall"],
        floorplan_resolution=floorplan["resolution_m_per_pixel"],
        floorplan_height_mode=floorplan["height_mode"],
        floorplan_heights=floorplan["heights_m"],
        floorplan_step=floorplan["step_m"],
        floorplan_top_z=floorplan["top_z_m"],
        floorplan_bottom_z=floorplan["bottom_z_m"],
        floorplan_sample_density_scale=floorplan["sample_density_scale"],
        floorplan_min_sample_points=floorplan["min_sample_points"],
        floorplan_max_sample_points=floorplan["max_sample_points"],
        floorplan_preview_tile_size=floorplan["preview_tile_size_px"],
        floorplan_semantic_padding=floorplan["semantic_padding_m"],
        floorplan_semantic_draw_labels=floorplan["semantic_draw_labels"],
        floorplan_fail_on_error=floorplan["fail_on_error"],
    )


def load_effective_config(config_path: Path, repo_root: Path, args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    yaml_config = load_project_config(config_path)
    merged = deep_merge(deepcopy(DEFAULT_CONFIG), yaml_config)
    overrides = cli_overrides(args)
    effective = deep_merge(merged, overrides)
    label_yaml = yaml_config.get("label", {}) if isinstance(yaml_config.get("label"), dict) else {}
    label_overrides = overrides.get("label", {}) if isinstance(overrides.get("label"), dict) else {}
    label_effective = effective.get("label", {}) if isinstance(effective.get("label"), dict) else {}
    if "batch_strategies" not in label_overrides and "ue_strategy" in label_overrides:
        label_effective["batch_strategies"] = [label_effective["ue_strategy"]]
    elif "batch_strategies" not in label_yaml and "ue_strategy" in label_yaml:
        label_effective["batch_strategies"] = [label_effective["ue_strategy"]]
    if "batch_grid_resolutions_m" not in label_overrides and "grid_resolution_m" in label_overrides:
        label_effective["batch_grid_resolutions_m"] = [label_effective["grid_resolution_m"]]
    elif "batch_grid_resolutions_m" not in label_yaml and "grid_resolution_m" in label_yaml:
        label_effective["batch_grid_resolutions_m"] = [label_effective["grid_resolution_m"]]
    effective.setdefault("runtime", {})
    effective["runtime"]["cli_overrides"] = overrides
    normalized = normalize_effective_config(effective, repo_root, config_path)
    validate_effective_config(normalized)
    return normalized, overrides
