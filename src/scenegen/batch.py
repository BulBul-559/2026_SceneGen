from __future__ import annotations

import argparse
import json
import os
import queue
import random
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import load_effective_config, save_effective_config
from .exporters import collect_label_floorplans, collect_raw_floorplans, collect_scene_objs, make_timestamp, write_visual_index
from .front3d import Front3DConfig, Front3DIndex, choose_scene_ids
from .modes import FRONT3D_MODE, FRONT3D_LIKE_MODES, is_procedural_front3d_like, scene_prefix_for_mode
from .paths import find_project_root, portable_path
from .postprocess.pipeline import run_batch_postprocess
from .procedural import aggregate_procedural_run_report
from .quality import aggregate_run_statistics, write_json_report
from .runlog import append_jsonl, atomic_write_json


BATCH_SCHEMA_VERSION = "scenegen.batch.v1"
SUPPORTED_BATCH_MODES = FRONT3D_LIKE_MODES


class TaskProcessError(RuntimeError):
    def __init__(self, returncode: int, command: list[str], stdout: str, stderr: str):
        super().__init__(f"scenegen task failed with exit code {returncode}")
        self.returncode = returncode
        self.command = command
        self.stdout = stdout
        self.stderr = stderr


class TaskTimeoutError(TaskProcessError):
    def __init__(
        self,
        *,
        timeout_s: float,
        returncode: int,
        command: list[str],
        stdout: str,
        stderr: str,
        killed_after_terminate: bool,
    ):
        super().__init__(returncode, command, stdout, stderr)
        self.timeout_s = timeout_s
        self.killed_after_terminate = killed_after_terminate

    def __str__(self) -> str:
        detail = "killed after terminate grace period" if self.killed_after_terminate else "terminated"
        return f"scenegen task timed out after {self.timeout_s:g}s and was {detail}"


@dataclass(frozen=True)
class TaskSubprocessResult:
    returncode: int
    stdout: str
    stderr: str


def run_task_subprocess(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: float | None,
    terminate_grace_s: float = 30.0,
) -> TaskSubprocessResult:
    if timeout_s is None:
        process = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False)
        return TaskSubprocessResult(returncode=process.returncode, stdout=process.stdout, stderr=process.stderr)

    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        process.terminate()
        killed_after_terminate = False
        try:
            stdout, stderr = process.communicate(timeout=terminate_grace_s)
        except subprocess.TimeoutExpired:
            process.kill()
            killed_after_terminate = True
            stdout, stderr = process.communicate()
        raise TaskTimeoutError(
            timeout_s=timeout_s,
            returncode=process.returncode if process.returncode is not None else -9,
            command=command,
            stdout=stdout or "",
            stderr=stderr or "",
            killed_after_terminate=killed_after_terminate,
        ) from None
    return TaskSubprocessResult(returncode=process.returncode, stdout=stdout or "", stderr=stderr or "")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run SceneGen production batches with workers, resume, and logs.")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="SceneGen YAML config. Batch v1 supports front3d, procedural_front3d, and procedural_front3d_vision.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Number of worker subprocess lanes. Overrides batch.workers from YAML when provided.",
    )
    parser.add_argument(
        "--scheduler",
        choices=("dynamic", "hybrid", "static"),
        default=None,
        help=(
            "Worker scheduling policy. Overrides batch.scheduler from YAML when provided. "
            "static keeps fixed shard assignment; dynamic uses one shared queue; hybrid starts with fixed shards "
            "and lets idle workers steal tail tasks."
        ),
    )
    parser.add_argument("--resume", action="store_true", help="Resume an existing batch run with the same run_name.")
    parser.add_argument(
        "--max-retries",
        type=int,
        default=None,
        help="Retries per scene task after subprocess failure. Overrides batch.max_retries from YAML when provided.",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        dest="set_values",
        metavar="KEY=VALUE",
        help="Override SceneGen config before planning, same syntax as scenegen --set.",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


@dataclass(frozen=True)
class BatchPaths:
    run_dir: Path
    batch_dir: Path
    logs_dir: Path
    events: Path
    timings: Path
    state: Path
    plan: Path
    failures: Path
    retry_queue: Path
    dead_letter: Path
    precheck_skips: Path
    worker_runs: Path
    workers: Path
    tracebacks: Path


def batch_paths(run_dir: Path) -> BatchPaths:
    batch_dir = run_dir / "batch"
    logs_dir = batch_dir / "logs"
    return BatchPaths(
        run_dir=run_dir,
        batch_dir=batch_dir,
        logs_dir=logs_dir,
        events=logs_dir / "events.jsonl",
        timings=logs_dir / "timings.jsonl",
        state=batch_dir / "state.json",
        plan=batch_dir / "scene_plan.jsonl",
        failures=logs_dir / "queues" / "failures.jsonl",
        retry_queue=logs_dir / "queues" / "retry.jsonl",
        dead_letter=logs_dir / "queues" / "dead_letter.jsonl",
        precheck_skips=logs_dir / "queues" / "precheck_skips.jsonl",
        worker_runs=batch_dir / "worker_runs",
        workers=logs_dir / "workers",
        tracebacks=logs_dir / "scenes",
    )


def event(paths: BatchPaths, event_type: str, **payload: Any) -> None:
    append_jsonl(
        paths.events,
        {
            "schema_version": BATCH_SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "ts": now_iso(),
            "event_type": event_type,
            **payload,
        },
    )


def write_state(paths: BatchPaths, state: dict[str, Any]) -> None:
    atomic_write_json(paths.state, {"schema_version": BATCH_SCHEMA_VERSION, "updated_at": now_iso(), **state})


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_plan(path: Path, tasks: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for task in tasks:
            handle.write(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n")


def load_plan(path: Path) -> list[dict[str, Any]]:
    return read_jsonl(path)


def plan_tasks(effective_config: dict[str, Any], workers: int) -> list[dict[str, Any]]:
    mode = str(effective_config["pipeline"]["mode"])
    if mode not in SUPPORTED_BATCH_MODES:
        raise ValueError(
            "scenegen-batch v1 supports only pipeline.mode=front3d, procedural_front3d, or procedural_front3d_vision"
        )
    count = int(effective_config["pipeline"]["scenes"])
    seed = int(effective_config["pipeline"]["seed"])
    output_index_start = int(effective_config["pipeline"]["index_start"])
    seed_rng = random.Random(seed)
    tasks: list[dict[str, Any]] = []

    if mode == FRONT3D_MODE:
        front3d = effective_config["front3d"]
        config = Front3DConfig(
            manifest=Path(front3d["manifest"]),
            source_scene_dir=Path(front3d["source_dir"]),
            variant=str(front3d["arch_variant"]),
            object_variant=str(front3d["object_variant"]),
            scene_ids=tuple(front3d["scene_ids"]),
            scene_selection=str(front3d["select"]),
            start_index=int(front3d["start_index"]),
            use_replace_jid=bool(front3d["use_replace_jid"]),
            skip_missing_objects=bool(front3d["skip_missing_objects"]),
            normalize_positive_xy=bool(front3d["positive_xy"]),
            ground_objects=bool(front3d["ground"]),
        )
        index = Front3DIndex(config)
        scene_ids = choose_scene_ids(
            index.scene_ids,
            config.scene_ids,
            config.scene_selection,
            count,
            random.Random(seed),
            start_index=config.start_index,
        )
        scene_prefix = scene_prefix_for_mode(mode)
    else:
        scene_ids = [f"procedural_seed_{output_index_start + index:06d}" for index in range(count)]
        scene_prefix = scene_prefix_for_mode(mode)

    for plan_index, scene_id in enumerate(scene_ids):
        target_index = output_index_start + plan_index
        scene_key = f"{scene_prefix}_{target_index:04d}"
        tasks.append(
            {
                "task_id": scene_key,
                "mode": mode,
                "plan_index": plan_index,
                "target_index": target_index,
                "scene_key": scene_key,
                "scene_prefix": scene_prefix,
                "scene_id": scene_id,
                "scene_seed": seed_rng.randrange(1, 2**31),
                "shard_id": plan_index % workers,
                "status_initial": "pending",
            }
        )
    return tasks


def recursive_replace(value: Any, old: str, new: str) -> Any:
    if isinstance(value, dict):
        return {key: recursive_replace(child, old, new) for key, child in value.items()}
    if isinstance(value, list):
        return [recursive_replace(child, old, new) for child in value]
    if isinstance(value, str):
        return value.replace(old, new)
    return value


def task_seed_for_attempt(task: dict[str, Any], attempt_no: int) -> int:
    base_seed = int(task["scene_seed"])
    if attempt_no <= 1 or not is_procedural_front3d_like(str(task.get("mode", ""))):
        return base_seed
    retry_rng = random.Random(f"{task['task_id']}:{base_seed}:{attempt_no}")
    return retry_rng.randrange(1, 2**31)


def command_for_task(
    *,
    config_path: Path,
    raw_set_values: list[str],
    paths: BatchPaths,
    worker_id: str,
    task: dict[str, Any],
    attempt_no: int = 1,
) -> list[str]:
    worker_output_dir = paths.worker_runs / worker_id
    scene_seed = task_seed_for_attempt(task, attempt_no)
    set_values = [
        *raw_set_values,
        f"pipeline.output_dir={worker_output_dir}",
        f"pipeline.run_name={task['task_id']}",
        "pipeline.scenes=1",
        "pipeline.index_start=0",
        "pipeline.clean=false",
        "runtime.batch_child=true",
        "runtime.skip_summary=true",
        f"pipeline.seed={scene_seed}",
    ]
    if task.get("mode") == FRONT3D_MODE:
        set_values.extend(
            [
                f"front3d.scene_ids={json.dumps([task['scene_id']])}",
                "front3d.select=sequential",
                "front3d.start_index=0",
            ]
        )
    command = [sys.executable, "-m", "scenegen.cli", "--config", str(config_path)]
    for value in set_values:
        command.extend(["--set", value])
    return command


def load_task_scene_record(
    worker_run_dir: Path,
    source_scene_key: str,
    final_scene_key: str,
    target_index: int,
) -> dict[str, Any]:
    manifest_path = worker_run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = manifest.get("scenes") or []
    if not records:
        raise RuntimeError(f"Worker manifest has no scene records: {manifest_path}")
    record = recursive_replace(records[0], source_scene_key, final_scene_key)
    record["scene_index"] = int(target_index)
    record["batch_task_id"] = final_scene_key
    record["batch_worker_run"] = str(worker_run_dir)
    record["batch_child_run_timings_s"] = manifest.get("run_timings_s") or {}
    record["batch_child_procedural_precheck_skipped_scenes"] = manifest.get("procedural_precheck_skipped_scenes") or []
    return record


def run_task(
    *,
    config_path: Path,
    repo_root: Path,
    raw_set_values: list[str],
    paths: BatchPaths,
    worker_id: str,
    task: dict[str, Any],
    attempt_no: int,
    task_timeout_s: float | None,
) -> dict[str, Any]:
    start = time.perf_counter()
    worker_plain_log = paths.workers / f"{worker_id}.log"
    worker_jsonl = paths.workers / f"{worker_id}.jsonl"
    paths.workers.mkdir(parents=True, exist_ok=True)
    command = command_for_task(
        config_path=config_path,
        raw_set_values=raw_set_values,
        paths=paths,
        worker_id=worker_id,
        task=task,
        attempt_no=attempt_no,
    )
    worker_task_run_dir = paths.worker_runs / worker_id / str(task["task_id"])
    if worker_task_run_dir.exists():
        shutil.rmtree(worker_task_run_dir)
    append_jsonl(
        worker_jsonl,
        {
            "schema_version": BATCH_SCHEMA_VERSION,
            "ts": now_iso(),
            "event_type": "task_started",
            "worker_id": worker_id,
            "task_id": task["task_id"],
            "attempt_no": attempt_no,
            "command": command,
        },
    )
    env = os.environ.copy()
    src_path = str(repo_root / "src")
    env["PYTHONPATH"] = f"{src_path}:{env['PYTHONPATH']}" if env.get("PYTHONPATH") else src_path
    try:
        process = run_task_subprocess(command, cwd=repo_root, env=env, timeout_s=task_timeout_s)
    except TaskTimeoutError as exc:
        duration_s = round(time.perf_counter() - start, 6)
        with worker_plain_log.open("a", encoding="utf-8") as handle:
            handle.write(f"\n=== {now_iso()} {task['task_id']} attempt {attempt_no} timeout {exc.returncode} ===\n")
            handle.write("COMMAND: " + " ".join(command) + "\n")
            handle.write(f"TIMEOUT_S: {exc.timeout_s:g}\n")
            handle.write(f"KILLED_AFTER_TERMINATE: {exc.killed_after_terminate}\n")
            handle.write("--- stdout ---\n")
            handle.write(exc.stdout)
            handle.write("\n--- stderr ---\n")
            handle.write(exc.stderr)
            handle.write("\n")
        append_jsonl(
            paths.timings,
            {
                "schema_version": "scenegen.batch.timing.v1",
                "ts": now_iso(),
                "worker_id": worker_id,
                "task_id": task["task_id"],
                "scene_key": task["scene_key"],
                "stage": "task_subprocess",
                "attempt_no": attempt_no,
                "status": "timeout",
                "duration_ms": round(duration_s * 1000.0, 3),
                "timeout_s": exc.timeout_s,
            },
        )
        raise
    duration_s = round(time.perf_counter() - start, 6)
    with worker_plain_log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n=== {now_iso()} {task['task_id']} attempt {attempt_no} exit {process.returncode} ===\n")
        handle.write("COMMAND: " + " ".join(command) + "\n")
        handle.write("--- stdout ---\n")
        handle.write(process.stdout)
        handle.write("\n--- stderr ---\n")
        handle.write(process.stderr)
        handle.write("\n")
    append_jsonl(
        paths.timings,
        {
            "schema_version": "scenegen.batch.timing.v1",
            "ts": now_iso(),
            "worker_id": worker_id,
            "task_id": task["task_id"],
            "scene_key": task["scene_key"],
            "stage": "task_subprocess",
            "attempt_no": attempt_no,
            "status": "ok" if process.returncode == 0 else "failed",
            "duration_ms": round(duration_s * 1000.0, 3),
        },
    )
    if process.returncode != 0:
        raise TaskProcessError(process.returncode, command, process.stdout, process.stderr)

    publish_start = time.perf_counter()
    worker_run_dir = worker_task_run_dir
    source_scene_key = f"{task.get('scene_prefix', 'front3d')}_0000"
    source_scene_dir = worker_run_dir / source_scene_key
    final_scene_dir = paths.run_dir / str(task["scene_key"])
    if not source_scene_dir.is_dir():
        raise FileNotFoundError(f"Expected worker scene directory not found: {source_scene_dir}")
    record = load_task_scene_record(
        worker_run_dir,
        source_scene_key,
        str(task["scene_key"]),
        int(task["target_index"]),
    )
    if final_scene_dir.exists():
        shutil.rmtree(final_scene_dir)
    shutil.move(str(source_scene_dir), str(final_scene_dir))
    worker_summary_dir = worker_run_dir / "summary"
    if worker_summary_dir.exists():
        shutil.rmtree(worker_summary_dir)
    publish_duration_s = round(time.perf_counter() - publish_start, 6)
    append_jsonl(
        paths.timings,
        {
            "schema_version": "scenegen.batch.timing.v1",
            "ts": now_iso(),
            "worker_id": worker_id,
            "task_id": task["task_id"],
            "scene_key": task["scene_key"],
            "stage": "publish_scene",
            "attempt_no": attempt_no,
            "status": "ok",
            "duration_ms": round(publish_duration_s * 1000.0, 3),
        },
    )
    record["scene_dir"] = str(task["scene_key"])
    record["batch_worker_id"] = worker_id
    record["batch_attempt_no"] = attempt_no
    record["batch_duration_s"] = duration_s
    record["batch_publish_s"] = publish_duration_s
    record["batch_total_duration_s"] = round(duration_s + publish_duration_s, 6)
    child_total_run = (record.get("batch_child_run_timings_s") or {}).get("total_run")
    if isinstance(child_total_run, int | float):
        record["batch_subprocess_overhead_s"] = round(duration_s - float(child_total_run), 6)
    append_jsonl(
        worker_jsonl,
        {
            "schema_version": BATCH_SCHEMA_VERSION,
            "ts": now_iso(),
            "event_type": "task_succeeded",
            "worker_id": worker_id,
            "task_id": task["task_id"],
            "attempt_no": attempt_no,
            "duration_s": duration_s,
        },
    )
    return record


def write_task_failure(paths: BatchPaths, worker_id: str, task: dict[str, Any], attempt_no: int, exc: BaseException) -> str:
    trace_dir = paths.tracebacks / str(task["task_id"]) / f"attempt_{attempt_no:02d}"
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = trace_dir / "task.traceback.txt"
    stdout_path = trace_dir / "stdout.txt"
    stderr_path = trace_dir / "stderr.txt"
    if isinstance(exc, TaskProcessError):
        stdout_path.write_text(exc.stdout, encoding="utf-8")
        stderr_path.write_text(exc.stderr, encoding="utf-8")
    trace_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
    payload = {
        "schema_version": "scenegen.batch.traceback.v1",
        "ts": now_iso(),
        "worker_id": worker_id,
        "task_id": task["task_id"],
        "scene_key": task["scene_key"],
        "scene_id": task.get("scene_id"),
        "attempt_no": attempt_no,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "traceback_file": str(trace_path.relative_to(paths.run_dir)),
        "stdout_file": str(stdout_path.relative_to(paths.run_dir)) if stdout_path.is_file() else None,
        "stderr_file": str(stderr_path.relative_to(paths.run_dir)) if stderr_path.is_file() else None,
        "returncode": exc.returncode if isinstance(exc, TaskProcessError) else None,
        "command": exc.command if isinstance(exc, TaskProcessError) else None,
        "timeout_s": exc.timeout_s if isinstance(exc, TaskTimeoutError) else None,
        "killed_after_terminate": exc.killed_after_terminate if isinstance(exc, TaskTimeoutError) else None,
        "stdout_tail": exc.stdout[-4000:] if isinstance(exc, TaskProcessError) else None,
        "stderr_tail": exc.stderr[-4000:] if isinstance(exc, TaskProcessError) else None,
    }
    (trace_dir / "task.traceback.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str((trace_dir / "task.traceback.json").relative_to(paths.run_dir))


def worker_loop(
    *,
    worker_index: int,
    task_queue: queue.Queue[dict[str, Any]],
    steal_queues: list[queue.Queue[dict[str, Any]]] | None,
    total_tasks: int,
    scheduler: str,
    config_path: Path,
    repo_root: Path,
    raw_set_values: list[str],
    paths: BatchPaths,
    max_retries: int,
    task_timeout_s: float | None,
    state: dict[str, Any],
    state_lock: threading.Lock,
) -> list[dict[str, Any]]:
    worker_id = f"worker_{worker_index:03d}"
    records: list[dict[str, Any]] = []
    event(paths, "worker_started", worker_id=worker_id, scheduler=scheduler, total_task_count=total_tasks)
    while True:
        task_source_queue = task_queue
        stolen = False
        source_worker_id = None
        try:
            task = task_queue.get_nowait()
        except queue.Empty:
            if scheduler != "hybrid" or steal_queues is None:
                break
            candidates = [
                (other_queue.qsize(), other_index, other_queue)
                for other_index, other_queue in enumerate(steal_queues)
                if other_index != worker_index and other_queue.qsize() > 0
            ]
            if not candidates:
                break
            task = None
            for _size, source_index, other_queue in sorted(candidates, reverse=True):
                try:
                    task = other_queue.get_nowait()
                except queue.Empty:
                    continue
                task_source_queue = other_queue
                stolen = True
                source_worker_id = f"worker_{source_index:03d}"
                break
            if task is None:
                break
        task_id = str(task["task_id"])
        event(
            paths,
            "task_claimed",
            worker_id=worker_id,
            task_id=task_id,
            queued_remaining=task_source_queue.qsize(),
            stolen=stolen,
            source_worker_id=source_worker_id,
        )
        try:
            with state_lock:
                status = state["tasks"].get(task_id, {}).get("status")
            if status == "succeeded":
                event(paths, "task_skipped_resume", worker_id=worker_id, task_id=task_id)
                final_scene_dir = paths.run_dir / str(task["scene_key"])
                record_path = final_scene_dir / "scene_record_batch.json"
                if record_path.is_file():
                    records.append(json.loads(record_path.read_text(encoding="utf-8")))
                continue
            attempt_no = 0
            while attempt_no <= max_retries:
                attempt_no += 1
                event(
                    paths,
                    "task_started",
                    worker_id=worker_id,
                    task_id=task_id,
                    attempt_no=attempt_no,
                    scene_id=task.get("scene_id"),
                )
                with state_lock:
                    state["tasks"][task_id] = {
                        **task,
                        "status": "running",
                        "worker_id": worker_id,
                        "attempt_no": attempt_no,
                        "updated_at": now_iso(),
                    }
                    write_state(paths, state)
                try:
                    record = run_task(
                        config_path=config_path,
                        repo_root=repo_root,
                        raw_set_values=raw_set_values,
                        paths=paths,
                        worker_id=worker_id,
                        task=task,
                        attempt_no=attempt_no,
                        task_timeout_s=task_timeout_s,
                    )
                except Exception as exc:
                    traceback_file = write_task_failure(paths, worker_id, task, attempt_no, exc)
                    queue_payload = {
                        "schema_version": "scenegen.batch.queue.v1",
                        "ts": now_iso(),
                        "worker_id": worker_id,
                        "task_id": task_id,
                        "scene_key": task["scene_key"],
                        "scene_id": task["scene_id"],
                        "attempt_no": attempt_no,
                        "max_retries": max_retries,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "traceback_file": traceback_file,
                    }
                    append_jsonl(paths.failures, queue_payload)
                    if attempt_no <= max_retries:
                        append_jsonl(paths.retry_queue, {**queue_payload, "event_type": "retry_enqueued"})
                        event(paths, "retry_enqueued", level="warning", **queue_payload)
                        continue
                    append_jsonl(paths.dead_letter, {**queue_payload, "event_type": "dead_lettered"})
                    event(paths, "task_failed", level="error", **queue_payload)
                    with state_lock:
                        state["tasks"][task_id] = {
                            **task,
                            "status": "failed",
                            "worker_id": worker_id,
                            "attempt_no": attempt_no,
                            "traceback_file": traceback_file,
                            "updated_at": now_iso(),
                        }
                        write_state(paths, state)
                    break
                else:
                    record_path = paths.run_dir / str(task["scene_key"]) / "scene_record_batch.json"
                    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                    records.append(record)
                    event(paths, "task_succeeded", worker_id=worker_id, task_id=task_id, attempt_no=attempt_no)
                    with state_lock:
                        state["tasks"][task_id] = {
                            **task,
                            "status": "succeeded",
                            "worker_id": worker_id,
                            "attempt_no": attempt_no,
                            "scene_record": portable_path(record_path, paths.run_dir),
                            "updated_at": now_iso(),
                        }
                        write_state(paths, state)
                    break
        finally:
            task_source_queue.task_done()
    event(paths, "worker_stopped", worker_id=worker_id, scheduler=scheduler, completed_count=len(records))
    return records


def build_final_manifest(
    *,
    paths: BatchPaths,
    effective_config: dict[str, Any],
    records: list[dict[str, Any]],
    workers: int,
    max_retries: int,
    task_timeout_s: float | None,
    scheduler: str,
) -> dict[str, Any]:
    mode = str(effective_config["pipeline"]["mode"])
    scene_prefix = scene_prefix_for_mode(mode)
    records = sorted(records, key=lambda item: int(str(item.get("batch_task_id", f"{scene_prefix}_999999")).split("_")[-1]))
    if bool(effective_config.get("output", {}).get("write_obj_summary", True)):
        copy_manifest = collect_scene_objs(paths.run_dir, records, scene_prefix)
    else:
        copy_manifest = {
            "summary_dir": "summary/obj",
            "count": 0,
            "objects": [],
            "skipped": True,
            "reason": "output.write_obj_summary=false",
        }
    raw_floorplan_manifest = collect_raw_floorplans(paths.run_dir, records, scene_prefix)
    label_floorplan_manifest = collect_label_floorplans(paths.run_dir, records, scene_prefix)
    visual_index_file = write_visual_index(paths.run_dir, records, scene_prefix)
    run_statistics = aggregate_run_statistics(records)
    statistics_file = write_json_report(paths.run_dir / "statistics.json", run_statistics, paths.run_dir)
    procedural_report: dict[str, object] | None = None
    procedural_report_file: str | None = None
    procedural_asset_pool_coverage_file: str | None = None
    procedural_precheck_skipped_scenes: list[dict[str, object]] = []
    if is_procedural_front3d_like(mode):
        for record in records:
            skipped = record.get("batch_child_procedural_precheck_skipped_scenes")
            if isinstance(skipped, list):
                procedural_precheck_skipped_scenes.extend(item for item in skipped if isinstance(item, dict))
    if is_procedural_front3d_like(mode):
        procedural_report = aggregate_procedural_run_report(records, procedural_precheck_skipped_scenes)
        procedural_report_file = write_json_report(paths.run_dir / "procedural_report.json", procedural_report, paths.run_dir)
        asset_pool_coverage = procedural_report.get("asset_pool_coverage")
        if isinstance(asset_pool_coverage, dict):
            procedural_asset_pool_coverage_file = write_json_report(
                paths.run_dir / "procedural_asset_pool_coverage.json",
                asset_pool_coverage,
                paths.run_dir,
            )
    manifest: dict[str, Any] = {
        "generator": "SceneGen",
        "batch": True,
        "schema_version": BATCH_SCHEMA_VERSION,
        "scenegen_version": __version__,
        "mode": mode,
        "output": effective_config.get("output", {}),
        "seed": effective_config["pipeline"]["seed"],
        "run_name": effective_config["pipeline"]["run_name"],
        "run_dir": ".",
        "workers": workers,
        "scheduler": scheduler,
        "max_retries": max_retries,
        "task_timeout_s": task_timeout_s,
        "requested_scenes": effective_config["pipeline"]["scenes"],
        "succeeded_scenes": len(records),
        "failed_scenes": len(read_jsonl(paths.dead_letter)),
        "front3d_manifest": portable_path(Path(effective_config["front3d"]["manifest"]), paths.run_dir),
        "summary_obj": copy_manifest,
        "summary_floorplan_raw": raw_floorplan_manifest,
        "summary_label_floorplan": label_floorplan_manifest,
        "summary": {
            "root": "summary",
            "obj": copy_manifest,
            "floorplan": raw_floorplan_manifest,
            "label_floorplan": label_floorplan_manifest,
        },
        "visual_index": visual_index_file,
        "statistics": run_statistics,
        "statistics_file": statistics_file,
        "procedural_report": procedural_report,
        "procedural_report_file": procedural_report_file,
        "procedural_asset_pool_coverage_file": procedural_asset_pool_coverage_file,
        "procedural_precheck_skipped_count": len(procedural_precheck_skipped_scenes)
        if is_procedural_front3d_like(mode)
        else 0,
        "procedural_precheck_skipped_scenes": procedural_precheck_skipped_scenes
        if is_procedural_front3d_like(mode)
        else [],
        "effective_config": "effective_config.yaml",
        "batch_logs": {
            "events": portable_path(paths.events, paths.run_dir),
            "timings": portable_path(paths.timings, paths.run_dir),
            "state": portable_path(paths.state, paths.run_dir),
            "plan": portable_path(paths.plan, paths.run_dir),
            "worker_logs": portable_path(paths.workers, paths.run_dir),
            "failures": portable_path(paths.failures, paths.run_dir),
            "retry_queue": portable_path(paths.retry_queue, paths.run_dir),
            "dead_letter": portable_path(paths.dead_letter, paths.run_dir),
        },
        "scenes": records,
    }
    write_manifest_files(paths, manifest)
    return manifest


def write_manifest_files(paths: BatchPaths, manifest: dict[str, Any]) -> None:
    mode_manifest = f"manifest_{manifest.get('mode', 'front3d')}.json"
    for name in ("manifest.json", mode_manifest, "manifest_batch.json"):
        (paths.run_dir / name).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = find_project_root()
    config_path = args.config if args.config.is_absolute() else repo_root / args.config
    effective_config, _overrides = load_effective_config(config_path.resolve(), repo_root, args)
    batch_config = effective_config["batch"]
    workers = int(args.workers) if args.workers is not None else int(batch_config["workers"])
    scheduler = str(args.scheduler) if args.scheduler is not None else str(batch_config["scheduler"])
    max_retries = int(args.max_retries) if args.max_retries is not None else int(batch_config["max_retries"])
    task_timeout_s = batch_config.get("task_timeout_s")
    task_timeout_s = None if task_timeout_s is None else float(task_timeout_s)
    if workers < 1:
        raise ValueError("--workers must be at least 1")
    if max_retries < 0:
        raise ValueError("--max-retries must be non-negative")
    if scheduler not in {"dynamic", "hybrid", "static"}:
        raise ValueError("--scheduler must be dynamic, hybrid, or static")
    effective_config["batch"] = {
        **batch_config,
        "workers": workers,
        "scheduler": scheduler,
        "max_retries": max_retries,
        "task_timeout_s": task_timeout_s,
    }
    run_name = effective_config["pipeline"]["run_name"] or make_timestamp()
    effective_config["pipeline"]["run_name"] = run_name
    run_dir = Path(effective_config["pipeline"]["output_dir"]) / run_name
    paths = batch_paths(run_dir)
    if run_dir.exists() and not args.resume:
        raise FileExistsError(f"Batch run already exists: {run_dir}. Use --resume or choose another pipeline.run_name.")
    run_dir.mkdir(parents=True, exist_ok=True)
    paths.batch_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    for queue_path in (paths.failures, paths.retry_queue, paths.dead_letter, paths.precheck_skips):
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.touch(exist_ok=True)
    effective_config.setdefault("runtime", {})
    effective_config["runtime"]["batch"] = {
        "enabled": True,
        "workers": workers,
        "scheduler": scheduler,
        "max_retries": max_retries,
        "task_timeout_s": task_timeout_s,
        "run_dir": str(run_dir),
    }
    save_effective_config(run_dir / "effective_config.yaml", effective_config)

    if args.resume and paths.plan.is_file():
        tasks = load_plan(paths.plan)
    else:
        tasks = plan_tasks(effective_config, workers)
        write_plan(paths.plan, tasks)

    existing_state = json.loads(paths.state.read_text(encoding="utf-8")) if args.resume and paths.state.is_file() else {}
    state = {
        "run_name": run_name,
        "run_dir": str(run_dir),
        "status": "running",
        "workers": workers,
        "max_retries": max_retries,
        "total": len(tasks),
        "started_at": existing_state.get("started_at") or now_iso(),
        "tasks": existing_state.get("tasks") or {},
    }
    write_state(paths, state)
    event(
        paths,
        "batch_started",
        run_name=run_name,
        run_dir=str(run_dir),
        workers=workers,
        scheduler=scheduler,
        task_count=len(tasks),
        resume=bool(args.resume),
    )

    worker_queues: list[queue.Queue[dict[str, Any]]]
    if scheduler in {"static", "hybrid"}:
        worker_queues = [queue.Queue() for _ in range(workers)]
        for task in tasks:
            worker_queues[int(task["shard_id"])].put(task)
    else:
        shared_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        for task in tasks:
            shared_queue.put(task)
        worker_queues = [shared_queue for _ in range(workers)]
    state_lock = threading.Lock()
    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                worker_loop,
                worker_index=worker_index,
                task_queue=worker_queues[worker_index],
                steal_queues=worker_queues if scheduler == "hybrid" else None,
                total_tasks=len(tasks),
                scheduler=scheduler,
                config_path=config_path.resolve(),
                repo_root=repo_root,
                raw_set_values=list(args.set_values or []),
                paths=paths,
                max_retries=max_retries,
                task_timeout_s=task_timeout_s,
                state=state,
                state_lock=state_lock,
            )
            for worker_index in range(workers)
        ]
        for future in as_completed(futures):
            records.extend(future.result())

    # Include succeeded records from previous resume runs that this invocation skipped.
    for task_state in state["tasks"].values():
        if not isinstance(task_state, dict) or task_state.get("status") != "succeeded":
            continue
        record_path = run_dir / str(task_state.get("scene_record", ""))
        if record_path.is_file() and not any(record.get("batch_task_id") == task_state.get("task_id") for record in records):
            records.append(json.loads(record_path.read_text(encoding="utf-8")))

    manifest = build_final_manifest(
        paths=paths,
        effective_config=effective_config,
        records=records,
        workers=workers,
        max_retries=max_retries,
        task_timeout_s=task_timeout_s,
        scheduler=scheduler,
    )
    failed_count = int(manifest["failed_scenes"])
    try:
        postprocess_report = run_batch_postprocess(
            run_dir=paths.run_dir,
            effective_config=effective_config,
            batch_workers=workers,
        )
    except Exception as exc:
        state["status"] = "failed"
        state["succeeded"] = len(records)
        state["failed"] = failed_count
        state["postprocess_status"] = "failed"
        state["postprocess_error_type"] = type(exc).__name__
        state["postprocess_error"] = str(exc)
        state["completed_at"] = now_iso()
        write_state(paths, state)
        event(paths, "batch_failed", status=state["status"], reason="postprocess_failed", error_type=type(exc).__name__, error=str(exc))
        print(f"batch manifest: {paths.run_dir / 'manifest_batch.json'}")
        print(f"postprocess failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if postprocess_report.get("enabled"):
        manifest["postprocess"] = {
            "status": postprocess_report.get("status"),
            "report": "batch/postprocess_report.json",
            "state": "batch/postprocess_state.json",
            "events": "batch/postprocess_events.jsonl",
            "failures": "batch/postprocess_failures.jsonl",
            "stages": postprocess_report.get("stages") or {},
            "files": postprocess_report.get("files") or {},
        }
        write_manifest_files(paths, manifest)

    state["status"] = "completed" if failed_count == 0 else "failed"
    state["succeeded"] = len(records)
    state["failed"] = failed_count
    state["postprocess_status"] = postprocess_report.get("status")
    state["completed_at"] = now_iso()
    write_state(paths, state)
    event(
        paths,
        "batch_completed",
        status=state["status"],
        succeeded=len(records),
        failed=failed_count,
        postprocess_status=postprocess_report.get("status"),
    )
    print(f"batch manifest: {paths.run_dir / 'manifest_batch.json'}")
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
