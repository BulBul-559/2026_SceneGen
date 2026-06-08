#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_manifest_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def portable_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def file_stats(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": path.name,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def scene_dirs(run_dir: Path, scene_glob: str) -> list[Path]:
    return sorted(path for path in run_dir.glob(scene_glob) if path.is_dir())


def required_scene_files(scene_dir: Path, require_maps: bool) -> dict[str, Path]:
    files = {
        "floorplan": scene_dir / "floorplan" / "floorplan_1p60.png",
        "mask_npy": scene_dir / "floorplan" / "class_mask.npy",
        "mask_png": scene_dir / "floorplan" / "class_mask.png",
        "mask_preview": scene_dir / "floorplan" / "class_mask_preview.png",
        "class_mask_meta": scene_dir / "floorplan" / "class_mask_meta.json",
        "floorplan_meta": scene_dir / "floorplan" / "meta.json",
        "maps_metadata": scene_dir / "maps" / "metadata.json",
    }
    if require_maps:
        files["geometry"] = scene_dir / "maps" / "geometry.npz"
        files["propagation"] = scene_dir / "maps" / "propagation.npz"
    return files


def missing_required_files(scene_dir: Path, require_maps: bool) -> list[str]:
    missing: list[str] = []
    for name, path in required_scene_files(scene_dir, require_maps).items():
        if not path.is_file():
            missing.append(name)
    return missing


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def write_bs_label(scene_target: Path, maps_metadata: dict[str, Any]) -> dict[str, Any]:
    bs_points = maps_metadata.get("bs_points") or []
    payload = {
        "schema_version": "scenegen.vision_dataset.label_bs.v1",
        "scene_key": maps_metadata.get("scene_key"),
        "scene_id": maps_metadata.get("scene_id"),
        "bs_count": len(bs_points),
        "source_label_files": sorted(
            {
                source
                for point in bs_points
                if isinstance(point, dict)
                for source in point.get("source_label_files", [])
                if isinstance(source, str)
            }
        ),
        "bs_positions": [point.get("position_m") for point in bs_points if isinstance(point, dict)],
        "bs_points": bs_points,
    }
    write_json(scene_target / "label_bs.json", payload)
    return payload


def build_scene_dataset_entry(
    scene_dir: Path,
    target_root: Path,
    run_dir: Path,
    *,
    overwrite: bool,
) -> dict[str, Any]:
    target_scene_dir = target_root / scene_dir.name
    if target_scene_dir.exists() and not overwrite:
        return {
            "scene_key": scene_dir.name,
            "status": "skipped",
            "reason": "target_scene_dir_exists",
            "target_scene_dir": portable_path(target_scene_dir, target_root),
        }
    if target_scene_dir.exists() and overwrite:
        shutil.rmtree(target_scene_dir)
    target_scene_dir.mkdir(parents=True, exist_ok=True)

    files = required_scene_files(scene_dir, require_maps=True)
    class_meta = read_json(files["class_mask_meta"])
    floorplan_meta = read_json(files["floorplan_meta"])
    maps_metadata = read_json(files["maps_metadata"])

    copies = {
        "floorplan": (files["floorplan"], target_scene_dir / "floorplan.png"),
        "mask_npy": (files["mask_npy"], target_scene_dir / "mask.npy"),
        "mask_png": (files["mask_png"], target_scene_dir / "mask.png"),
        "mask_preview": (files["mask_preview"], target_scene_dir / "mask_preview.png"),
        "geometry": (files["geometry"], target_scene_dir / "geometry.npz"),
        "propagation": (files["propagation"], target_scene_dir / "propagation.npz"),
    }
    for source, destination in copies.values():
        copy_file(source, destination)
    label_payload = write_bs_label(target_scene_dir, maps_metadata)

    file_stat_payload = {name: file_stats(destination) for name, (_, destination) in copies.items()}
    metadata = {
        "schema_version": "scenegen.vision_dataset.scene.v1",
        "scene_key": scene_dir.name,
        "scene_id": class_meta.get("scene_id") or maps_metadata.get("scene_id"),
        "source": {
            "run_dir": str(run_dir),
            "scene_dir": portable_path(scene_dir, run_dir),
            "class_mask_meta": portable_path(files["class_mask_meta"], scene_dir),
            "floorplan_meta": portable_path(files["floorplan_meta"], scene_dir),
            "maps_metadata": portable_path(files["maps_metadata"], scene_dir),
        },
        "class_mapping": maps_metadata.get("class_mapping") or {
            str(key): value.get("name") for key, value in (class_meta.get("classes") or {}).items()
        },
        "resolution_m_per_pixel": maps_metadata.get("resolution_m_per_pixel") or class_meta.get("resolution_m_per_pixel"),
        "grid_shape": maps_metadata.get("grid_shape") or class_meta.get("grid_shape"),
        "origin_xy_m": maps_metadata.get("origin_xy_m") or class_meta.get("origin_xy_m"),
        "extent_xy_m": maps_metadata.get("extent_xy_m") or class_meta.get("extent_xy_m"),
        "floorplan": {
            "input": "floorplan.png",
            "source_projection_mode": floorplan_meta.get("projection_mode"),
            "source_height_m": 1.6,
        },
        "maps": {
            "geometry": "geometry.npz",
            "propagation": "propagation.npz",
            "parameters": maps_metadata.get("parameters"),
            "shapes": maps_metadata.get("shapes"),
        },
        "label": {
            "bs": "label_bs.json",
            "bs_count": label_payload["bs_count"],
        },
        "files": {
            "floorplan": "floorplan.png",
            "mask_npy": "mask.npy",
            "mask_png": "mask.png",
            "mask_preview": "mask_preview.png",
            "geometry": "geometry.npz",
            "propagation": "propagation.npz",
            "label_bs": "label_bs.json",
        },
        "file_stats": {
            **file_stat_payload,
            "label_bs": file_stats(target_scene_dir / "label_bs.json"),
        },
        "created_at": utc_now_iso(),
    }
    write_json(target_scene_dir / "metadata.json", metadata)
    return {
        "scene_key": scene_dir.name,
        "scene_id": metadata["scene_id"],
        "status": "copied",
        "target_scene_dir": portable_path(target_scene_dir, target_root),
        "height": int(metadata["grid_shape"][0]),
        "width": int(metadata["grid_shape"][1]),
        "meter_per_pixel": float(metadata["resolution_m_per_pixel"]),
        "bs_count": label_payload["bs_count"],
        "files": metadata["files"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a compact vision-training dataset from SceneGen run outputs.")
    parser.add_argument("run_dir", type=Path, help="SceneGen run directory.")
    parser.add_argument("dataset_dir", type=Path, help="Output dataset directory.")
    parser.add_argument("--scene-glob", default="front3d_*", help="Scene directory glob under run_dir.")
    parser.add_argument("--limit", type=int, default=None, help="Copy only the first N eligible scenes.")
    parser.add_argument("--require-maps", action="store_true", help="Require maps/geometry.npz and maps/propagation.npz.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing scene directories in dataset_dir.")
    parser.add_argument("--log-every", type=int, default=25, help="Print progress every N processed scenes. 0 disables.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started = time.perf_counter()
    run_dir = args.run_dir.resolve()
    dataset_dir = args.dataset_dir.resolve()
    dataset_dir.mkdir(parents=True, exist_ok=True)

    candidates = scene_dirs(run_dir, args.scene_glob)
    if args.limit is not None:
        candidates = candidates[: args.limit]

    records: list[dict[str, Any]] = []
    copied = 0
    skipped = 0
    failed = 0
    for index, scene_dir in enumerate(candidates, start=1):
        missing = missing_required_files(scene_dir, require_maps=args.require_maps)
        if missing:
            skipped += 1
            records.append({"scene_key": scene_dir.name, "status": "skipped", "reason": "missing_required_files", "missing": missing})
            continue
        try:
            record = build_scene_dataset_entry(scene_dir, dataset_dir, run_dir, overwrite=args.overwrite)
        except Exception as exc:  # noqa: BLE001 - keep dataset assembly robust over large runs.
            failed += 1
            records.append(
                {
                    "scene_key": scene_dir.name,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        else:
            records.append(record)
            if record["status"] == "copied":
                copied += 1
            else:
                skipped += 1
        if args.log_every and index % args.log_every == 0:
            print(f"processed={index}/{len(candidates)} copied={copied} skipped={skipped} failed={failed}")

    manifest_path = dataset_dir / "manifest.jsonl"
    write_manifest_jsonl(manifest_path, [record for record in records if record["status"] == "copied"])
    status_counts: dict[str, int] = {}
    for record in records:
        status_counts[str(record["status"])] = status_counts.get(str(record["status"]), 0) + 1
    summary = {
        "schema_version": "scenegen.vision_dataset.summary.v1",
        "created_at": utc_now_iso(),
        "source_run_dir": str(run_dir),
        "dataset_dir": str(dataset_dir),
        "scene_glob": args.scene_glob,
        "require_maps": args.require_maps,
        "status_counts": status_counts,
        "scene_count": status_counts.get("copied", 0),
        "manifest": "manifest.jsonl",
    }
    report = {
        **summary,
        "elapsed_s": round(time.perf_counter() - started, 6),
        "records": records,
    }
    write_json(dataset_dir / "summary.json", summary)
    write_json(dataset_dir / "build_report.json", report)
    print(
        "vision dataset:",
        f"copied={status_counts.get('copied', 0)}",
        f"skipped={status_counts.get('skipped', 0)}",
        f"failed={status_counts.get('failed', 0)}",
        f"manifest={manifest_path}",
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
