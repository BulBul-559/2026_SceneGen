from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from .assets import group_assets_by_class, load_assets, validate_asset_pool
from .config import config_to_namespace, load_effective_config, save_effective_config
from .exporters import (
    base_scene_summary,
    collect_raw_floorplans,
    collect_scene_objs,
    make_timestamp,
)
from .floorplan import FloorplanConfig, generate_floorplan_for_scene
from .paths import clean_output_root, default_config_path, find_project_root, portable_path, require_dir, require_file
from .quality import (
    QualityConfig,
    aggregate_run_statistics,
    check_scene_quality,
    scene_statistics,
    write_json_report,
)
from .sources import create_scene_source
from .validation import validate_sionna_scene


def build_parser() -> argparse.ArgumentParser:
    repo_root = find_project_root()
    parser = argparse.ArgumentParser(description="Generate Sionna-compatible procedural indoor scenes.")
    parser.add_argument("--config", type=Path, default=default_config_path(repo_root), help="Path to SceneGen YAML config.")
    parser.add_argument("--mode", choices=("generated", "bistro"), default=None)
    parser.add_argument("--bistro-base-dir", type=Path, default=None)
    parser.add_argument("--asset-catalog", type=Path, default=None)
    parser.add_argument("--asset-manifest", type=Path, default=None, help="Deprecated alias for --asset-catalog.")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--run-name", default=None, help="Use a stable run directory name instead of a timestamp.")
    parser.add_argument("--scenes", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--min-tables", type=int, default=None)
    parser.add_argument("--max-tables", type=int, default=None)
    parser.add_argument("--floor-extras", type=int, default=None)
    parser.add_argument("--min-tabletop-items", type=int, default=None)
    parser.add_argument("--max-tabletop-items", type=int, default=None)
    parser.add_argument(
        "--bistro-support-items",
        type=int,
        default=None,
        help="Small objects to place on existing Bistro counter/bar/support surfaces.",
    )
    parser.add_argument("--max-attempts", type=int, default=None)
    parser.add_argument(
        "--clean",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Remove old output runs under --output-dir before generating the new run.",
    )
    parser.add_argument(
        "--validate-sionna",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Load every generated scene.xml with sionna.rt.load_scene(merge_shapes=False).",
    )
    parser.add_argument(
        "--quality",
        dest="quality_enabled",
        action="store_true",
        default=None,
        help="Run post-generation placement quality checks.",
    )
    parser.add_argument("--no-quality", dest="quality_enabled", action="store_false")
    parser.add_argument(
        "--quality-fail-on-error",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Return a non-zero exit code when quality checks find errors.",
    )
    parser.add_argument("--quality-collision-padding", type=float, default=None)
    parser.add_argument("--quality-bistro-static-clearance", type=float, default=None)
    parser.add_argument("--quality-support-tolerance", type=float, default=None)
    parser.add_argument("--floorplan", dest="floorplan_enabled", action="store_true", default=None)
    parser.add_argument("--no-floorplan", dest="floorplan_enabled", action="store_false")
    parser.add_argument("--floorplan-geometry", dest="floorplan_geometry_enabled", action="store_true", default=None)
    parser.add_argument("--no-floorplan-geometry", dest="floorplan_geometry_enabled", action="store_false")
    parser.add_argument(
        "--floorplan-geometry-clean",
        dest="floorplan_geometry_clean_enabled",
        action="store_true",
        default=None,
        help="Generate a denoised geometry occupancy image for model input experiments.",
    )
    parser.add_argument(
        "--no-floorplan-geometry-clean",
        dest="floorplan_geometry_clean_enabled",
        action="store_false",
    )
    parser.add_argument("--floorplan-geometry-clean-min-density", type=float, default=None)
    parser.add_argument("--floorplan-geometry-clean-min-neighbors", type=int, default=None)
    parser.add_argument("--floorplan-geometry-clean-min-z", type=float, default=None)
    parser.add_argument("--floorplan-geometry-clean-max-abs-normal-z", type=float, default=None)
    parser.add_argument("--floorplan-geometry-clean-opening-px", type=int, default=None)
    parser.add_argument("--floorplan-geometry-clean-closing-px", type=int, default=None)
    parser.add_argument("--semantic-floorplan", dest="floorplan_semantic_enabled", action="store_true", default=None)
    parser.add_argument("--no-semantic-floorplan", dest="floorplan_semantic_enabled", action="store_false")
    parser.add_argument("--floorplan-resolution", type=float, default=None)
    parser.add_argument("--floorplan-height-mode", choices=("layers", "heights"), default=None)
    parser.add_argument(
        "--floorplan-heights",
        default=None,
        help="Comma-separated projection heights in meters when floorplan.height_mode=heights, e.g. 1.2,1.6.",
    )
    parser.add_argument("--floorplan-step", type=float, default=None)
    parser.add_argument("--floorplan-top-z", type=float, default=None)
    parser.add_argument("--floorplan-bottom-z", type=float, default=None)
    parser.add_argument("--floorplan-sample-density-scale", type=float, default=None)
    parser.add_argument("--floorplan-min-sample-points", type=int, default=None)
    parser.add_argument("--floorplan-max-sample-points", type=int, default=None)
    parser.add_argument("--floorplan-preview-tile-size", type=int, default=None)
    parser.add_argument("--floorplan-semantic-padding", type=float, default=None)
    parser.add_argument(
        "--floorplan-semantic-draw-labels",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Draw asset labels on semantic floorplans when there is enough room.",
    )
    parser.add_argument(
        "--floorplan-fail-on-error",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Return a non-zero exit code if floorplan generation fails.",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if args.mode not in {"generated", "bistro"}:
        raise ValueError("--mode must be 'generated' or 'bistro'")
    if args.scenes < 1:
        raise ValueError("--scenes must be at least 1")
    if args.min_tables < 0 or args.max_tables < args.min_tables:
        raise ValueError("--max-tables must be greater than or equal to --min-tables")
    if args.min_tabletop_items < 0 or args.max_tabletop_items < args.min_tabletop_items:
        raise ValueError("--max-tabletop-items must be greater than or equal to --min-tabletop-items")
    if args.max_attempts < 1:
        raise ValueError("--max-attempts must be at least 1")
    if args.quality_collision_padding < 0:
        raise ValueError("quality.collision_padding_m must be non-negative")
    if args.quality_bistro_static_clearance < 0:
        raise ValueError("quality.bistro_static_clearance_m must be non-negative")
    if args.quality_support_tolerance < 0:
        raise ValueError("quality.support_tolerance_m must be non-negative")
    if args.floorplan_resolution <= 0:
        raise ValueError("floorplan.resolution_m_per_pixel must be positive")
    if args.floorplan_height_mode not in {"layers", "heights"}:
        raise ValueError("floorplan.height_mode must be 'layers' or 'heights'")
    if args.floorplan_height_mode == "heights":
        if not args.floorplan_heights:
            raise ValueError("floorplan.heights_m must contain at least one height when height_mode is 'heights'")
        if any(height < args.floorplan_bottom_z for height in args.floorplan_heights):
            raise ValueError("floorplan.heights_m values must be greater than or equal to floorplan.bottom_z_m")
    if args.floorplan_step <= 0:
        raise ValueError("floorplan.step_m must be positive")
    if args.floorplan_min_sample_points < 1:
        raise ValueError("floorplan.min_sample_points must be positive")
    if args.floorplan_max_sample_points < args.floorplan_min_sample_points:
        raise ValueError("floorplan.max_sample_points must be greater than or equal to min_sample_points")
    if args.floorplan_enabled and not (args.floorplan_geometry_enabled or args.floorplan_semantic_enabled):
        raise ValueError("At least one of floorplan.geometry_enabled or floorplan.semantic_enabled must be true")
    if args.floorplan_geometry_clean_enabled and not args.floorplan_geometry_enabled:
        raise ValueError("floorplan.geometry_clean_enabled requires floorplan.geometry_enabled")
    if args.floorplan_geometry_clean_min_density < 0:
        raise ValueError("floorplan.geometry_clean_min_density must be non-negative")
    if args.floorplan_geometry_clean_min_neighbors < 0:
        raise ValueError("floorplan.geometry_clean_min_neighbors must be non-negative")
    if args.floorplan_geometry_clean_min_z < 0:
        raise ValueError("floorplan.geometry_clean_min_z_m must be non-negative")
    if not 0 <= args.floorplan_geometry_clean_max_abs_normal_z <= 1:
        raise ValueError("floorplan.geometry_clean_max_abs_normal_z must be between 0 and 1")
    if args.floorplan_geometry_clean_opening_px < 0:
        raise ValueError("floorplan.geometry_clean_opening_px must be non-negative")
    if args.floorplan_geometry_clean_closing_px < 0:
        raise ValueError("floorplan.geometry_clean_closing_px must be non-negative")
    if args.floorplan_semantic_padding < 0:
        raise ValueError("floorplan.semantic_padding_m must be non-negative")


def prepare_run_dir(output_root: Path, run_name: str, clean: bool) -> Path:
    output_root = output_root.expanduser().resolve()
    if clean:
        clean_output_root(output_root)
    else:
        output_root.mkdir(parents=True, exist_ok=True)

    run_dir = output_root / run_name
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}. Use --clean or choose a different --run-name.")
    run_dir.mkdir(parents=True)
    return run_dir


def main(argv: list[str] | None = None) -> int:
    cli_args = parse_args(argv)
    repo_root = find_project_root()
    config_path = cli_args.config if cli_args.config.is_absolute() else (repo_root / cli_args.config)
    effective_config, _overrides = load_effective_config(config_path.resolve(), repo_root, cli_args)
    args = config_to_namespace(effective_config)
    validate_args(args)

    asset_catalog_path = require_file(args.asset_catalog, "asset catalog")
    if args.mode == "bistro":
        bistro_base_dir = require_dir(args.bistro_base_dir, "Bistro base scene directory")
    else:
        bistro_base_dir = None

    run_name = args.run_name or make_timestamp()
    run_dir = prepare_run_dir(args.output_dir, run_name, args.clean)
    effective_config["pipeline"]["run_name"] = run_name
    effective_config["runtime"]["run_dir"] = str(run_dir)
    save_effective_config(run_dir / "effective_config.yaml", effective_config)
    floorplan_config = FloorplanConfig.from_mapping(effective_config["floorplan"])
    quality_config = QualityConfig.from_mapping(effective_config["quality"])

    assets = load_assets(asset_catalog_path)
    assets_by_class = group_assets_by_class(assets)
    validate_asset_pool(assets_by_class)
    source = create_scene_source(args, assets_by_class, bistro_base_dir)
    scene_prefix = source.scene_prefix

    master_rng = random.Random(args.seed)
    scene_records: list[dict[str, object]] = []
    validation_failed = False
    floorplan_failed = False
    quality_failed = False
    for scene_index in range(args.scenes):
        scene_seed = master_rng.randrange(1, 2**31)
        rng = random.Random(scene_seed)
        scene_dir = run_dir / f"{scene_prefix}_{scene_index:04d}"
        build = source.build_scene(scene_dir, scene_index, scene_seed, rng)
        placements = build.placements
        record = build.record

        statistics = scene_statistics(
            args.mode,
            placements,
            room=build.room,
            base_scene=build.base_scene,
        )
        record["statistics"] = statistics
        record["statistics_file"] = write_json_report(scene_dir / "statistics.json", statistics, run_dir)

        if quality_config.enabled:
            quality = check_scene_quality(
                args.mode,
                placements,
                quality_config,
                room=build.room,
                base_scene=build.base_scene,
                forbidden_xy_rects=source.forbidden_xy_rects,
            )
            record["quality"] = quality
            record["quality_report"] = write_json_report(scene_dir / "quality_report.json", quality, run_dir)
            quality_failed = quality_failed or not bool(quality["ok"])

        if args.validate_sionna:
            validation = validate_sionna_scene(scene_dir / "scene.xml", run_dir)
            record["sionna_validation"] = validation
            validation_failed = validation_failed or not bool(validation["ok"])
        if floorplan_config.enabled:
            try:
                record["floorplan"] = generate_floorplan_for_scene(
                    scene_dir / "scene.obj",
                    scene_dir / "floorplan",
                    floorplan_config,
                    placements=placements,
                    bounds_xy=build.bounds_xy,
                    forbidden_xy_rects=source.forbidden_xy_rects,
                )
            except Exception as exc:
                floorplan_failed = True
                record["floorplan"] = {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                if floorplan_config.fail_on_error:
                    scene_records.append(record)
                    break
        scene_records.append(record)
        quality_note = ""
        if quality_config.enabled:
            quality = record.get("quality", {})
            if isinstance(quality, dict):
                quality_note = f" quality_errors={quality.get('error_count', 0)}"
        print(f"[{scene_index + 1}/{args.scenes}] {scene_dir} placements={len(placements)}{quality_note}")

    copy_manifest = collect_scene_objs(run_dir, scene_records, scene_prefix)
    raw_floorplan_manifest = collect_raw_floorplans(run_dir, scene_records, scene_prefix)
    run_statistics = aggregate_run_statistics(scene_records)
    statistics_file = write_json_report(run_dir / "statistics.json", run_statistics, run_dir)
    class_counts = {name: len(items) for name, items in sorted(assets_by_class.items())}
    manifest: dict[str, object] = {
        "generator": "SceneGen",
        "mode": args.mode,
        "seed": args.seed,
        "run_name": run_name,
        "run_dir": ".",
        "asset_catalog": portable_path(asset_catalog_path, run_dir),
        "asset_manifest": portable_path(asset_catalog_path, run_dir),
        "asset_class_counts": class_counts,
        "forbidden_xy_rects": (
            [{"x_min": rect[0], "y_min": rect[1], "x_max": rect[2], "y_max": rect[3]} for rect in args.forbidden_xy_rects]
            if args.mode == "bistro"
            else []
        ),
        "forbidden_z_ignored": args.mode == "bistro",
        "summary_obj": copy_manifest,
        "summary_floorplan_raw": raw_floorplan_manifest,
        "sionna_validation_requested": bool(args.validate_sionna),
        "sionna_validation_ok": not validation_failed if args.validate_sionna else None,
        "quality_requested": bool(quality_config.enabled),
        "quality_ok": not quality_failed if quality_config.enabled else None,
        "quality_fail_on_error": bool(quality_config.fail_on_error),
        "statistics": run_statistics,
        "statistics_file": statistics_file,
        "floorplan_requested": bool(floorplan_config.enabled),
        "floorplan_ok": not floorplan_failed if floorplan_config.enabled else None,
        "floorplan_geometry_requested": bool(floorplan_config.enabled and floorplan_config.geometry_enabled),
        "floorplan_geometry_clean_requested": bool(
            floorplan_config.enabled and floorplan_config.geometry_enabled and floorplan_config.geometry_clean_enabled
        ),
        "floorplan_height_mode": floorplan_config.height_mode if floorplan_config.enabled else None,
        "floorplan_heights_m": floorplan_config.heights_m if floorplan_config.enabled else None,
        "floorplan_semantic_requested": bool(floorplan_config.enabled and floorplan_config.semantic_enabled),
        "effective_config": portable_path(run_dir / "effective_config.yaml", run_dir),
        "scenes": scene_records,
    }
    if source.base_scene is not None:
        manifest["bistro_base_scene"] = base_scene_summary(source.base_scene, run_dir)

    mode_manifest_path = run_dir / f"manifest_{args.mode}.json"
    mode_manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifest: {mode_manifest_path}")

    if validation_failed:
        print("Sionna validation failed for at least one scene.")
        return 1
    if quality_failed and quality_config.fail_on_error:
        print("Quality checks failed for at least one scene.")
        return 1
    if floorplan_failed and floorplan_config.fail_on_error:
        print("Floorplan generation failed for at least one scene.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
