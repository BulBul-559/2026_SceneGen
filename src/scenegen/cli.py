from __future__ import annotations

import argparse
import json
import random
import shutil
from pathlib import Path

from . import __version__
from .assets import group_assets_by_class, load_assets, validate_asset_pool
from .config import config_to_namespace, load_effective_config, save_effective_config
from .exporters import (
    base_scene_summary,
    collect_raw_floorplans,
    collect_scene_objs,
    make_timestamp,
)
from .floorplan import FloorplanConfig, generate_floorplan_for_scene
from .labels import LabelConfig, generate_label_batch_for_scene, label_variants, write_label_overlay
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
    parser.add_argument("--version", action="version", version=f"SceneGen {__version__}")
    parser.add_argument("--config", type=Path, default=default_config_path(repo_root), help="Path to SceneGen YAML config.")
    parser.add_argument("--mode", choices=("generated", "bistro", "front3d"), default=None)
    parser.add_argument("--bistro-base-dir", type=Path, default=None)
    parser.add_argument("--asset-catalog", type=Path, default=None)
    parser.add_argument("--asset-manifest", type=Path, default=None, help="Deprecated alias for --asset-catalog.")
    parser.add_argument("--front3d-manifest", type=Path, default=None)
    parser.add_argument("--front3d-source-scene-dir", type=Path, default=None)
    parser.add_argument("--front3d-variant", choices=("raw", "normalized"), default=None)
    parser.add_argument("--front3d-object-variant", choices=("raw", "normalized"), default=None)
    parser.add_argument("--front3d-scene-ids", default=None, help="Comma-separated 3D-FRONT scene ids to synthesize.")
    parser.add_argument("--front3d-scene-selection", choices=("random", "sequential"), default=None)
    parser.add_argument("--front3d-use-replace-jid", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--front3d-skip-missing-objects", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--front3d-normalize-positive-xy", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--front3d-ground-objects", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--front3d-precheck", dest="front3d_precheck_enabled", action="store_true", default=None)
    parser.add_argument("--no-front3d-precheck", dest="front3d_precheck_enabled", action="store_false")
    parser.add_argument("--front3d-precheck-max-attempts-per-scene", type=int, default=None)
    parser.add_argument("--front3d-precheck-min-placements", type=int, default=None)
    parser.add_argument("--front3d-precheck-max-z", type=float, default=None)
    parser.add_argument("--front3d-precheck-max-footprint-ratio", type=float, default=None)
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
    parser.add_argument("--label", dest="label_enabled", action="store_true", default=None)
    parser.add_argument("--no-label", dest="label_enabled", action="store_false")
    parser.add_argument("--label-version", default=None)
    parser.add_argument("--label-ue-height", type=float, default=None)
    parser.add_argument("--label-sampling-domain", choices=("room_floor", "global_floor"), default=None)
    parser.add_argument("--label-ue-strategy", choices=("free_space_grid", "plane_grid"), default=None)
    parser.add_argument("--label-grid-resolution", type=float, default=None)
    parser.add_argument("--label-batch-strategies", default=None, help="Comma-separated label UE strategies.")
    parser.add_argument("--label-batch-grid-resolutions", default=None, help="Comma-separated label grid resolutions in meters.")
    parser.add_argument("--label-ue-clearance", type=float, default=None)
    parser.add_argument("--label-obstacle-strategy", choices=("height_aware", "footprint_column"), default=None)
    parser.add_argument("--label-walk-ignore-low-obstacles-below", type=float, default=None)
    parser.add_argument("--label-walk-blocking-classes", default=None, help="Comma-separated placement classes blocking walk labels.")
    parser.add_argument("--label-walk-min-component-area", type=float, default=None)
    parser.add_argument("--label-bs-strategy", choices=("wall_or_corner", "geometry_center"), default=None)
    parser.add_argument("--label-bs-count-strategy", choices=("fixed_per_room", "area_adaptive"), default=None)
    parser.add_argument("--label-bs-per-room", type=int, default=None)
    parser.add_argument("--label-bs-min-per-room", type=int, default=None)
    parser.add_argument("--label-bs-max-per-room", type=int, default=None)
    parser.add_argument("--label-bs-min-room-area", type=float, default=None)
    parser.add_argument("--label-bs-area-per-point", type=float, default=None)
    parser.add_argument("--label-bs-height", type=float, default=None)
    parser.add_argument("--label-bs-ceiling-margin", type=float, default=None)
    parser.add_argument("--label-bs-wall-clearance", type=float, default=None)
    parser.add_argument("--label-bs-center-initial-radius", type=float, default=None)
    parser.add_argument("--label-bs-center-radius-step", type=float, default=None)
    parser.add_argument("--label-bs-center-max-radius", type=float, default=None)
    parser.add_argument("--label-wall-clearance", type=float, default=None)
    parser.add_argument("--label-corridor-room-id", default=None)
    parser.add_argument("--label-corridor-room-type", default=None)
    parser.add_argument("--label-corridor-clearance", type=float, default=None)
    parser.add_argument("--label-overlay", dest="label_overlay_enabled", action="store_true", default=None)
    parser.add_argument("--no-label-overlay", dest="label_overlay_enabled", action="store_false")
    parser.add_argument("--label-fail-on-error", action=argparse.BooleanOptionalAction, default=None)
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
    parser.add_argument("--floorplan-class-mask", dest="floorplan_class_mask_enabled", action="store_true", default=None)
    parser.add_argument("--no-floorplan-class-mask", dest="floorplan_class_mask_enabled", action="store_false")
    parser.add_argument("--floorplan-class-mask-wall-dilation", type=float, default=None)
    parser.add_argument("--floorplan-class-mask-furniture-dilation", type=float, default=None)
    parser.add_argument(
        "--floorplan-class-mask-include-doors-as-wall",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    parser.add_argument(
        "--floorplan-class-mask-include-windows-as-wall",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
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


def main(argv: list[str] | None = None) -> int:
    cli_args = parse_args(argv)
    repo_root = find_project_root()
    config_path = cli_args.config if cli_args.config.is_absolute() else (repo_root / cli_args.config)
    effective_config, _overrides = load_effective_config(config_path.resolve(), repo_root, cli_args)
    args = config_to_namespace(effective_config)

    asset_catalog_path = require_file(args.asset_catalog, "asset catalog") if args.mode != "front3d" else args.asset_catalog
    if args.mode == "bistro":
        bistro_base_dir = require_dir(args.bistro_base_dir, "Bistro base scene directory")
    else:
        bistro_base_dir = None
    front3d_manifest_path = None
    if args.mode == "front3d":
        front3d_manifest_path = require_file(args.front3d_manifest, "3D-FRONT SceneGen manifest")
        args.front3d_manifest = front3d_manifest_path
        args.front3d_source_scene_dir = require_dir(args.front3d_source_scene_dir, "3D-FRONT source scene directory")

    run_name = args.run_name or make_timestamp()
    run_dir = prepare_run_dir(args.output_dir, run_name, args.clean)
    effective_config["pipeline"]["run_name"] = run_name
    effective_config["runtime"]["run_dir"] = str(run_dir)
    save_effective_config(run_dir / "effective_config.yaml", effective_config)
    floorplan_config = FloorplanConfig.from_mapping(effective_config["floorplan"])
    quality_config = QualityConfig.from_mapping(effective_config["quality"])
    label_config = LabelConfig.from_mapping(effective_config["label"])

    if args.mode == "front3d":
        assets_by_class = {}
    else:
        assets = load_assets(asset_catalog_path)
        assets_by_class = group_assets_by_class(assets)
        validate_asset_pool(assets_by_class)
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
        scene_index = len(scene_records)
        max_attempts = (
            int(args.front3d_precheck_max_attempts_per_scene)
            if args.mode == "front3d" and bool(args.front3d_precheck_enabled)
            else 1
        )
        accepted = False
        for attempt_index in range(max_attempts):
            scene_seed = master_rng.randrange(1, 2**31)
            rng = random.Random(scene_seed)
            scene_dir = run_dir / f"{scene_prefix}_{scene_index:04d}"
            if scene_dir.exists():
                shutil.rmtree(scene_dir)
            build = source.build_scene(scene_dir, scene_index, scene_seed, rng)
            placements = build.placements
            record = build.record

            statistics = scene_statistics(
                args.mode,
                placements,
                room=build.room,
                base_scene=build.base_scene,
                front3d_base_scene=build.front3d_base_scene,
            )
            record["statistics"] = statistics
            record["statistics_file"] = write_json_report(scene_dir / "statistics.json", statistics, run_dir)
            precheck = evaluate_front3d_precheck(args, statistics)
            record["precheck"] = precheck
            if not bool(precheck["ok"]):
                scene_id = str(record.get("front3d_scene_id") or "")
                precheck_skipped_scenes.append(
                    {
                        "target_index": scene_index,
                        "attempt": attempt_index + 1,
                        "scene_seed": scene_seed,
                        "front3d_scene_id": scene_id,
                        "errors": precheck["errors"],
                        "statistics": statistics,
                        "skipped_object_count": int(record.get("skipped_object_count", 0)),
                    }
                )
                mark_rejected = getattr(source, "mark_scene_rejected", None)
                if callable(mark_rejected):
                    mark_rejected(scene_id)
                shutil.rmtree(scene_dir, ignore_errors=True)
                error_codes = ",".join(str(item.get("code")) for item in precheck["errors"] if isinstance(item, dict))
                print(
                    f"[precheck skip] target={scene_index + 1}/{args.scenes} "
                    f"scene_id={scene_id or '<unknown>'} errors={error_codes}"
                )
                continue

            if quality_config.enabled:
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
                validation = validate_sionna_scene(scene_dir / "scene.xml", run_dir)
                record["sionna_validation"] = validation
                validation_failed = validation_failed or not bool(validation["ok"])
            if label_config.enabled:
                try:
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
                    record["label"] = {
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                    if label_config.fail_on_error:
                        scene_records.append(record)
                        stop_generation = True
                        accepted = True
                        break
            if floorplan_config.enabled:
                try:
                    record["floorplan"] = generate_floorplan_for_scene(
                        scene_dir / "scene.obj",
                        scene_dir / "floorplan",
                        floorplan_config,
                        placements=placements,
                        bounds_xy=build.bounds_xy,
                        forbidden_xy_rects=source.forbidden_xy_rects,
                        front3d_base_scene=build.front3d_base_scene,
                    )
                    label_value = record.get("label", {})
                    if label_config.enabled and label_config.overlay_enabled and isinstance(label_value, dict) and bool(
                        label_value.get("ok")
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
                    record["floorplan"] = {
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                    if floorplan_config.fail_on_error:
                        scene_records.append(record)
                        stop_generation = True
                        accepted = True
                        break
            scene_records.append(record)
            quality_note = ""
            if quality_config.enabled:
                quality = record.get("quality", {})
                if isinstance(quality, dict):
                    quality_note = f" quality_errors={quality.get('error_count', 0)}"
            print(f"[{scene_index + 1}/{args.scenes}] {scene_dir} placements={len(placements)}{quality_note}")
            accepted = True
            break
        if not accepted:
            precheck_failed = True
            print(f"3D-FRONT precheck could not fill scene index {scene_index} after {max_attempts} attempts.")
            break

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
        "asset_catalog": portable_path(asset_catalog_path, run_dir) if args.mode != "front3d" else None,
        "asset_class_counts": class_counts,
        "front3d_manifest": portable_path(front3d_manifest_path, run_dir) if front3d_manifest_path is not None else None,
        "front3d_variant": args.front3d_variant if args.mode == "front3d" else None,
        "front3d_object_variant": args.front3d_object_variant if args.mode == "front3d" else None,
        "front3d_scene_selection": args.front3d_scene_selection if args.mode == "front3d" else None,
        "front3d_scene_ids": args.front3d_scene_ids if args.mode == "front3d" else [],
        "front3d_skipped_object_count": (
            sum(int(record.get("skipped_object_count", 0)) for record in scene_records) if args.mode == "front3d" else 0
        ),
        "front3d_precheck": front3d_precheck_settings(args),
        "front3d_precheck_ok": (not precheck_failed) if args.mode == "front3d" else None,
        "front3d_precheck_skipped_count": len(precheck_skipped_scenes) if args.mode == "front3d" else 0,
        "front3d_precheck_skipped_scenes": precheck_skipped_scenes if args.mode == "front3d" else [],
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
        "floorplan_requested": bool(floorplan_config.enabled),
        "floorplan_ok": not floorplan_failed if floorplan_config.enabled else None,
        "floorplan_geometry_requested": bool(floorplan_config.enabled and floorplan_config.geometry_enabled),
        "floorplan_geometry_clean_requested": bool(
            floorplan_config.enabled and floorplan_config.geometry_enabled and floorplan_config.geometry_clean_enabled
        ),
        "floorplan_class_mask_requested": bool(floorplan_config.enabled and floorplan_config.class_mask_enabled),
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
    if precheck_failed:
        print("3D-FRONT precheck failed to backfill all requested scenes.")
        return 1
    if label_failed and label_config.fail_on_error:
        print("Label generation failed for at least one scene.")
        return 1
    if floorplan_failed and floorplan_config.fail_on_error:
        print("Floorplan generation failed for at least one scene.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
