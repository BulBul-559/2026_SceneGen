from __future__ import annotations

import json
import os
import socket
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux production path has fcntl.
    fcntl = None  # type: ignore[assignment]


LOG_SCHEMA_VERSION = "scenegen.log.v1"
TIMING_SCHEMA_VERSION = "scenegen.timing.v1"
TRACEBACK_SCHEMA_VERSION = "scenegen.traceback.v1"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def append_jsonl(path: Path, payload: dict[str, Any], *, lock: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        if lock and fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            if lock and fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def timing_summary(records: list[dict[str, object]]) -> dict[str, object]:
    by_stage: dict[str, list[float]] = {}
    for record in records:
        timings = record.get("timings_s")
        if not isinstance(timings, dict):
            continue
        for stage, duration in timings.items():
            if isinstance(duration, int | float):
                by_stage.setdefault(str(stage), []).append(float(duration))
    summary: dict[str, object] = {}
    for stage, values in sorted(by_stage.items()):
        if not values:
            continue
        summary[stage] = {
            "count": len(values),
            "total_s": round(sum(values), 6),
            "mean_s": round(sum(values) / len(values), 6),
            "min_s": round(min(values), 6),
            "max_s": round(max(values), 6),
        }
    return summary


class RunLogger:
    def __init__(
        self,
        run_dir: Path,
        *,
        run_name: str,
        mode: str,
        run_id: str | None = None,
        worker_id: str | None = None,
    ):
        self.run_dir = run_dir
        self.run_name = run_name
        self.mode = mode
        self.run_id = run_id or str(uuid.uuid4())
        self.worker_id = worker_id or f"{socket.gethostname()}-pid{os.getpid()}"
        self.logs_dir = run_dir / "logs"
        self.events_path = self.logs_dir / "events.jsonl"
        self.timings_path = self.logs_dir / "timings.jsonl"
        self.state_path = self.logs_dir / "state" / "run_state.json"
        self.worker_log_path = self.logs_dir / "workers" / f"{self.worker_id}.jsonl"
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def event(
        self,
        event_type: str,
        *,
        level: str = "info",
        message: str | None = None,
        stage: str | None = None,
        status: str | None = None,
        scene_key: str | None = None,
        scene_index: int | None = None,
        attempt_no: int | None = None,
        metrics: dict[str, Any] | None = None,
        paths: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        traceback_file: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": LOG_SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "run_id": self.run_id,
            "run_name": self.run_name,
            "mode": self.mode,
            "ts": utc_now_iso(),
            "monotonic_ns": time.monotonic_ns(),
            "level": level,
            "event_type": event_type,
            "component": "scenegen",
            "worker_id": self.worker_id,
            "pid": os.getpid(),
            "host": socket.gethostname(),
        }
        if message is not None:
            payload["message"] = message
        if stage is not None:
            payload["stage"] = stage
        if status is not None:
            payload["status"] = status
        if scene_key is not None:
            payload["scene_key"] = scene_key
        if scene_index is not None:
            payload["scene_index"] = int(scene_index)
        if attempt_no is not None:
            payload["attempt_no"] = int(attempt_no)
        if metrics:
            payload["metrics"] = metrics
        if paths:
            payload["paths"] = paths
        if error:
            payload["error"] = error
        if traceback_file:
            payload["traceback_file"] = traceback_file
        if extra:
            payload.update(extra)
        append_jsonl(self.events_path, payload)
        append_jsonl(self.worker_log_path, payload)
        return payload

    @contextmanager
    def stage(
        self,
        timings: dict[str, float],
        stage: str,
        *,
        scene_key: str | None = None,
        scene_index: int | None = None,
        attempt_no: int | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        start_perf = time.perf_counter()
        start_ts = utc_now_iso()
        span_id = str(uuid.uuid4())
        self.event(
            "stage_started",
            stage=stage,
            status="running",
            scene_key=scene_key,
            scene_index=scene_index,
            attempt_no=attempt_no,
            metrics=metrics,
            extra={"span_id": span_id},
        )
        status = "ok"
        error: dict[str, Any] | None = None
        try:
            yield
        except Exception as exc:
            status = "failed"
            error = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            end_ts = utc_now_iso()
            duration_s = round(time.perf_counter() - start_perf, 6)
            timings[stage] = duration_s
            timing_payload: dict[str, Any] = {
                "schema_version": TIMING_SCHEMA_VERSION,
                "span_id": span_id,
                "run_id": self.run_id,
                "run_name": self.run_name,
                "mode": self.mode,
                "worker_id": self.worker_id,
                "name": f"{scene_key}.{stage}" if scene_key else stage,
                "category": "stage",
                "stage": stage,
                "status": status,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "duration_ms": round(duration_s * 1000.0, 3),
            }
            if scene_key is not None:
                timing_payload["scene_key"] = scene_key
            if scene_index is not None:
                timing_payload["scene_index"] = int(scene_index)
            if attempt_no is not None:
                timing_payload["attempt_no"] = int(attempt_no)
            if metrics:
                timing_payload["metrics"] = metrics
            if error:
                timing_payload["error"] = error
            append_jsonl(self.timings_path, timing_payload)
            self.event(
                "stage_completed" if status == "ok" else "stage_failed",
                level="info" if status == "ok" else "error",
                stage=stage,
                status=status,
                scene_key=scene_key,
                scene_index=scene_index,
                attempt_no=attempt_no,
                metrics={"duration_s": duration_s, **(metrics or {})},
                error=error,
                extra={"span_id": span_id},
            )

    def write_traceback(
        self,
        *,
        scene_key: str,
        attempt_no: int,
        stage: str,
        exc: BaseException,
        context: dict[str, Any] | None = None,
    ) -> str:
        trace_dir = self.logs_dir / "scenes" / scene_key / f"attempt_{attempt_no:02d}"
        trace_dir.mkdir(parents=True, exist_ok=True)
        traceback_text = traceback.format_exc()
        txt_path = trace_dir / f"{stage}.traceback.txt"
        json_path = trace_dir / f"{stage}.traceback.json"
        txt_path.write_text(traceback_text, encoding="utf-8")
        payload = {
            "schema_version": TRACEBACK_SCHEMA_VERSION,
            "run_id": self.run_id,
            "run_name": self.run_name,
            "mode": self.mode,
            "worker_id": self.worker_id,
            "scene_key": scene_key,
            "attempt_no": int(attempt_no),
            "stage": stage,
            "ts": utc_now_iso(),
            "exception": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
            "traceback_text_file": str(txt_path.relative_to(self.run_dir)),
            "traceback": traceback_text,
            "context": context or {},
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(json_path.relative_to(self.run_dir))

    def write_state(self, payload: dict[str, Any]) -> None:
        state = {
            "schema_version": "scenegen.state.v1",
            "run_id": self.run_id,
            "run_name": self.run_name,
            "mode": self.mode,
            "updated_at": utc_now_iso(),
            **payload,
        }
        atomic_write_json(self.state_path, state)
