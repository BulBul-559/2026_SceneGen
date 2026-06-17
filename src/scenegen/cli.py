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
    desired_total = sum(int(value) for value in desired_counts.values()) if desired_counts else 0
    skipped_count = int(record.get("skipped_object_count") or (procedural.get("skipped_object_count", 0) if procedural else 0))

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

    result["ok"] = not errors
    result["errors"] = errors
    result["desired_object_count"] = desired_total
    result["placement_count"] = placement_count
    result["skipped_object_count"] = skipped_count
    return result


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
        "max_skipped_ratio": float(args.procedural_precheck_max_skipped_ratio),
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
