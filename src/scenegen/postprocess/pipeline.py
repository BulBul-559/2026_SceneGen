from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scenegen.paths import portable_path
from scenegen.runlog import append_jsonl, atomic_write_json

from .derived_maps import run_derived_maps
from .vision_dataset import run_build_vision_dataset


POSTPROCESS_SCHEMA_VERSION = "scenegen.postprocess.v1"


class PostprocessStageError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


@dataclass(frozen=True)
class PostprocessPaths:
    run_dir: Path
    state: Path
    events: Path
    report: Path
    failures: Path
    log_file: Path


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def postprocess_paths(run_dir: Path, run_name: str) -> PostprocessPaths:
    batch_dir = run_dir / "batch"
    return PostprocessPaths(
        run_dir=run_dir,
        state=batch_dir / "postprocess_state.json",
        events=batch_dir / "postprocess_events.jsonl",
        report=batch_dir / "postprocess_report.json",
        failures=batch_dir / "postprocess_failures.jsonl",
        log_file=run_dir.parent / "_logs" / run_name / "postprocess.log",
    )


def write_json(path: Path, payload: Any) -> None:
    atomic_write_json(path, payload)


def append_event(paths: PostprocessPaths, event_type: str, **payload: Any) -> None:
    append_jsonl(
        paths.events,
        {
            "schema_version": POSTPROCESS_SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "ts": now_iso(),
            "event_type": event_type,
            **payload,
        },
    )


def append_log(paths: PostprocessPaths, message: str) -> None:
    paths.log_file.parent.mkdir(parents=True, exist_ok=True)
    with paths.log_file.open("a", encoding="utf-8") as handle:
        handle.write(f"{now_iso()} {message}\n")


def append_failure(paths: PostprocessPaths, payload: dict[str, Any]) -> None:
    append_jsonl(paths.failures, {"schema_version": POSTPROCESS_SCHEMA_VERSION, "ts": now_iso(), **payload})


def stage_summary(report: dict[str, Any], report_file: Path, run_dir: Path) -> dict[str, Any]:
    try:
        report_path = portable_path(report_file, run_dir)
        report_file.resolve().relative_to(run_dir.resolve())
    except ValueError:
        report_path = str(report_file)
    return {
        "status_counts": report.get("status_counts") or {},
        "elapsed_s": report.get("elapsed_s"),
        "total_candidates": report.get("total_candidates"),
        "scene_count": report.get("scene_count"),
        "report": report_path,
    }


def blocking_map_records(report: dict[str, Any]) -> list[dict[str, Any]]:
    blocking: list[dict[str, Any]] = []
    for record in report.get("records") or []:
        status = record.get("status")
        reason = record.get("reason")
        if status == "failed" or (status == "skipped" and reason != "maps_already_exist"):
            blocking.append(record)
    return blocking


def blocking_dataset_records(report: dict[str, Any]) -> list[dict[str, Any]]:
    blocking: list[dict[str, Any]] = []
    for record in report.get("records") or []:
        status = record.get("status")
        reason = record.get("reason")
        if status == "failed" or (status == "skipped" and reason == "missing_required_files"):
            blocking.append(record)
    return blocking


def bs_label_kwargs(bs_label: dict[str, Any]) -> dict[str, str | None]:
    mode = str(bs_label["mode"])
    if mode == "name":
        return {"bs_label_name": str(bs_label["name"]), "bs_label_glob": None}
    if mode == "glob":
        return {"bs_label_name": None, "bs_label_glob": str(bs_label["glob"])}
    return {"bs_label_name": None, "bs_label_glob": None}


def run_batch_postprocess(
    *,
    run_dir: Path,
    effective_config: dict[str, Any],
    batch_workers: int,
) -> dict[str, Any]:
    config = effective_config.get("postprocess") or {}
    maps_config = config.get("maps") or {}
    dataset_config = config.get("dataset") or {}
    maps_enabled = bool(maps_config.get("enabled"))
    dataset_enabled = bool(dataset_config.get("enabled"))
    run_name = str(effective_config["pipeline"]["run_name"])
    paths = postprocess_paths(run_dir, run_name)
    report: dict[str, Any] = {
        "schema_version": POSTPROCESS_SCHEMA_VERSION,
        "run_dir": str(run_dir),
        "enabled": maps_enabled or dataset_enabled,
        "started_at": now_iso(),
        "status": "skipped",
        "stages": {},
        "files": {
            "state": portable_path(paths.state, run_dir),
            "events": portable_path(paths.events, run_dir),
            "failures": portable_path(paths.failures, run_dir),
            "log_file": str(paths.log_file),
        },
    }
    paths.state.parent.mkdir(parents=True, exist_ok=True)
    paths.events.parent.mkdir(parents=True, exist_ok=True)
    paths.failures.touch(exist_ok=True)
    if not report["enabled"]:
        write_json(paths.state, {**report, "updated_at": now_iso()})
        write_json(paths.report, {**report, "completed_at": now_iso()})
        return report

    started = time.perf_counter()
    report["status"] = "running"
    write_json(paths.state, {**report, "updated_at": now_iso()})
    append_event(paths, "postprocess_started", maps_enabled=maps_enabled, dataset_enabled=dataset_enabled)
    append_log(paths, f"postprocess started run_dir={run_dir}")

    try:
        if maps_enabled:
            stage_start = time.perf_counter()
            append_event(paths, "stage_started", stage="maps")
            append_log(paths, "stage maps started")
            maps_workers = int(maps_config["workers"] or batch_workers)
            pair_cache_config = maps_config.get("pair_cache") or {}
            maps_report = run_derived_maps(
                run_dir,
                scene_glob=str(maps_config["scene_glob"]),
                los_stride_pixels=int(maps_config["los_stride_px"]),
                r_max_m=float(maps_config["r_max_m"]),
                snap_radius_m=float(maps_config["snap_radius_m"]),
                pair_cache_enabled=bool(pair_cache_config.get("enabled", True)),
                target_pairs_per_scene=int(pair_cache_config.get("target_pairs_per_scene", 4096)),
                ue_candidates_per_bs=pair_cache_config.get("ue_candidates_per_bs"),
                pair_cache_seed=int(pair_cache_config.get("seed", 0)),
                write_propagation=bool(maps_config.get("write_propagation", False)),
                overwrite=bool(maps_config["overwrite"]),
                workers=maps_workers,
                log_every=0,
                log_fn=lambda message: append_log(paths, message),
                **bs_label_kwargs(maps_config["bs_label"]),
            )
            maps_stage = stage_summary(maps_report, run_dir / "derived_maps_report.json", run_dir)
            maps_stage["workers"] = maps_workers
            maps_stage["duration_s"] = round(time.perf_counter() - stage_start, 6)
            blocking = blocking_map_records(maps_report)
            for record in blocking:
                append_failure(paths, {"stage": "maps", **record})
            if blocking:
                maps_stage["status"] = "failed"
                report["stages"]["maps"] = maps_stage
                raise PostprocessStageError("maps", f"maps stage has {len(blocking)} blocking record(s)")
            maps_stage["status"] = "completed"
            report["stages"]["maps"] = maps_stage
            append_event(paths, "stage_completed", stage="maps", **maps_stage)
            append_log(paths, "stage maps completed")

        if dataset_enabled:
            stage_start = time.perf_counter()
            append_event(paths, "stage_started", stage="dataset")
            append_log(paths, "stage dataset started")
            dataset_name = dataset_config["name"] or f"{run_name}_vision"
            dataset_dir = Path(dataset_config["output_dir"]) / str(dataset_name)
            dataset_report = run_build_vision_dataset(
                run_dir,
                dataset_dir,
                scene_glob=str(dataset_config["scene_glob"]),
                require_maps=bool(dataset_config["require_maps"]),
                overwrite=bool(dataset_config["overwrite"]),
                log_every=0,
                log_fn=lambda message: append_log(paths, message),
            )
            dataset_stage = stage_summary(dataset_report, dataset_dir / "build_report.json", run_dir)
            dataset_stage["dataset_dir"] = str(dataset_dir)
            dataset_stage["duration_s"] = round(time.perf_counter() - stage_start, 6)
            blocking = blocking_dataset_records(dataset_report)
            for record in blocking:
                append_failure(paths, {"stage": "dataset", **record})
            if blocking:
                dataset_stage["status"] = "failed"
                report["stages"]["dataset"] = dataset_stage
                raise PostprocessStageError("dataset", f"dataset stage has {len(blocking)} blocking record(s)")
            dataset_stage["status"] = "completed"
            report["stages"]["dataset"] = dataset_stage
            append_event(paths, "stage_completed", stage="dataset", **dataset_stage)
            append_log(paths, "stage dataset completed")
    except Exception as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)
        report["elapsed_s"] = round(time.perf_counter() - started, 6)
        report["completed_at"] = now_iso()
        append_failure(paths, {"stage": getattr(exc, "stage", "postprocess"), "error_type": type(exc).__name__, "error": str(exc)})
        append_event(paths, "postprocess_failed", error_type=type(exc).__name__, error=str(exc))
        append_log(paths, f"postprocess failed error_type={type(exc).__name__} error={exc}")
        write_json(paths.state, {**report, "updated_at": now_iso()})
        write_json(paths.report, report)
        raise

    report["status"] = "completed"
    report["elapsed_s"] = round(time.perf_counter() - started, 6)
    report["completed_at"] = now_iso()
    append_event(paths, "postprocess_completed", elapsed_s=report["elapsed_s"])
    append_log(paths, f"postprocess completed elapsed_s={report['elapsed_s']}")
    write_json(paths.state, {**report, "updated_at": now_iso()})
    write_json(paths.report, report)
    return report
