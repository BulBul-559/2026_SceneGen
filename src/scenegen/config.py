from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .paths import default_config_path


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
        "forbidden_xy_rects": [],
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
    return normalized


def save_effective_config(path: Path, config: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def config_to_namespace(config: dict[str, Any]) -> argparse.Namespace:
    pipeline = config["pipeline"]
    assets = config["assets"]
    bistro = config["bistro"]
    placement = config["placement"]
    validation = config["validation"]
    quality = config["quality"]
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
    yaml_config = upgrade_config_aliases(load_yaml_config(config_path))
    base_config = deepcopy(DEFAULT_CONFIG)
    default_yaml_path = default_config_path(repo_root).resolve()
    if config_path.resolve() != default_yaml_path and default_yaml_path.is_file():
        base_config = deep_merge(base_config, upgrade_config_aliases(load_yaml_config(default_yaml_path)))
    merged = deep_merge(base_config, yaml_config)
    overrides = cli_overrides(args)
    effective = deep_merge(merged, overrides)
    effective.setdefault("runtime", {})
    effective["runtime"]["cli_overrides"] = overrides
    return normalize_effective_config(effective, repo_root, config_path), overrides
