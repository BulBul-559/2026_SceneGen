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
        return str(path)


def load_manifest(dataset_dir: Path) -> list[dict[str, Any]]:
    manifest_path = dataset_dir / "manifest.jsonl"
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("status") != "copied":
            continue
        scene_key = str(record.get("target_scene_dir") or record.get("scene_key") or "")
        if not scene_key:
            raise ValueError(f"{manifest_path}:{line_number} does not contain a scene directory.")
        scene_dir = dataset_dir / scene_key
        if not scene_dir.is_dir():
            raise FileNotFoundError(f"Manifest scene directory does not exist: {scene_dir}")
        record = {
            **record,
            "source_dataset_dir": str(dataset_dir),
            "source_scene_key": scene_key,
            "source_scene_dir": scene_dir,
        }
        records.append(record)
    return records


def file_stats(path: Path, root: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": portable_path(path, root),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def copy_scene(source_scene_dir: Path, target_scene_dir: Path) -> None:
    if target_scene_dir.exists():
        shutil.rmtree(target_scene_dir)
    shutil.copytree(source_scene_dir, target_scene_dir)


def update_json_if_exists(path: Path, updates: dict[str, Any]) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = read_json(path)
    payload.update(updates)
    write_json(path, payload)
    return payload


def select_records(
    primary_records: list[dict[str, Any]],
    supplement_records: list[dict[str, Any]],
    target_count: int,
    *,
    skip_duplicate_scene_ids: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_scene_ids: set[str] = set()

    for record in primary_records:
        scene_id = record.get("scene_id")
        if skip_duplicate_scene_ids and isinstance(scene_id, str):
            if scene_id in seen_scene_ids:
                skipped.append({**record, "skip_reason": "duplicate_scene_id_in_primary"})
                continue
            seen_scene_ids.add(scene_id)
        selected.append({**record, "merge_source_role": "primary"})

    for record in supplement_records:
        if len(selected) >= target_count:
            break
        scene_id = record.get("scene_id")
        if skip_duplicate_scene_ids and isinstance(scene_id, str) and scene_id in seen_scene_ids:
            skipped.append({**record, "skip_reason": "duplicate_scene_id_in_supplement"})
            continue
        if skip_duplicate_scene_ids and isinstance(scene_id, str):
            seen_scene_ids.add(scene_id)
        selected.append({**record, "merge_source_role": "supplement"})

    return selected[:target_count], skipped


def merge_datasets(
    primary_dataset_dir: Path,
    supplement_dataset_dir: Path,
    output_dataset_dir: Path,
    *,
    target_count: int,
    overwrite: bool,
    skip_duplicate_scene_ids: bool,
    log_every: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    primary_dataset_dir = primary_dataset_dir.resolve()
    supplement_dataset_dir = supplement_dataset_dir.resolve()
    output_dataset_dir = output_dataset_dir.resolve()

    if output_dataset_dir.exists():
        if not overwrite:
            raise FileExistsError(f"Output dataset already exists: {output_dataset_dir}")
        shutil.rmtree(output_dataset_dir)
    output_dataset_dir.mkdir(parents=True)

    primary_records = load_manifest(primary_dataset_dir)
    supplement_records = load_manifest(supplement_dataset_dir)
    selected, skipped = select_records(
        primary_records,
        supplement_records,
        target_count,
        skip_duplicate_scene_ids=skip_duplicate_scene_ids,
    )
    if len(selected) < target_count:
        raise ValueError(
            f"Not enough scenes to build target dataset: selected={len(selected)} target_count={target_count}"
        )

    created_at = utc_now_iso()
    manifest_records: list[dict[str, Any]] = []
    copied = 0
    role_counts: dict[str, int] = {}
    file_stat_totals: dict[str, int] = {}
    for final_index, source_record in enumerate(selected):
        final_scene_key = f"front3d_{final_index:04d}"
        target_scene_dir = output_dataset_dir / final_scene_key
        source_scene_dir = Path(source_record["source_scene_dir"])
        copy_scene(source_scene_dir, target_scene_dir)

        merge_payload = {
            "created_at": created_at,
            "final_index": final_index,
            "final_scene_key": final_scene_key,
            "source_dataset_dir": str(Path(source_record["source_dataset_dir"]).resolve()),
            "source_scene_key": source_record["source_scene_key"],
            "source_scene_id": source_record.get("scene_id"),
            "source_role": source_record["merge_source_role"],
        }
        label = update_json_if_exists(
            target_scene_dir / "label_bs.json",
            {
                "scene_key": final_scene_key,
                "merged_dataset": {
                    "source_scene_key": source_record["source_scene_key"],
                    "source_role": source_record["merge_source_role"],
                },
            },
        )

        files = source_record.get("files") if isinstance(source_record.get("files"), dict) else {}
        payload_file_stats: dict[str, Any] = {}
        for logical_name, file_name in files.items():
            path = target_scene_dir / str(file_name)
            if path.is_file():
                payload_file_stats[str(logical_name)] = file_stats(path, target_scene_dir)
        if not payload_file_stats:
            payload_file_stats = {
                path.name: file_stats(path, target_scene_dir)
                for path in sorted(target_scene_dir.iterdir())
                if path.is_file() and path.name != "metadata.json"
            }
        metadata = update_json_if_exists(
            target_scene_dir / "metadata.json",
            {
                "scene_key": final_scene_key,
                "merged_dataset": merge_payload,
                "file_stats": payload_file_stats,
            },
        )

        scene_file_stats: dict[str, Any] = {}
        for file_name in sorted(path.name for path in target_scene_dir.iterdir() if path.is_file()):
            path = target_scene_dir / file_name
            stats = file_stats(path, output_dataset_dir)
            scene_file_stats[file_name] = stats
            file_stat_totals[file_name] = file_stat_totals.get(file_name, 0) + int(stats["size_bytes"])
        role = str(source_record["merge_source_role"])
        role_counts[role] = role_counts.get(role, 0) + 1
        copied += 1
        manifest_records.append(
            {
                "status": "copied",
                "scene_key": final_scene_key,
                "scene_id": source_record.get("scene_id"),
                "target_scene_dir": final_scene_key,
                "source_dataset_dir": str(Path(source_record["source_dataset_dir"]).resolve()),
                "source_scene_key": source_record["source_scene_key"],
                "source_role": role,
                "height": source_record.get("height") or (metadata or {}).get("grid_shape", [None, None])[0],
                "width": source_record.get("width") or (metadata or {}).get("grid_shape", [None, None])[1],
                "meter_per_pixel": source_record.get("meter_per_pixel")
                or (metadata or {}).get("resolution_m_per_pixel"),
                "bs_count": source_record.get("bs_count") or (label or {}).get("bs_count"),
                "image_path": f"{final_scene_key}/{files.get('floorplan', 'floorplan.png')}",
                "mask_path": f"{final_scene_key}/{files.get('mask_npy', 'mask.npy')}",
                "geometry_path": f"{final_scene_key}/{files.get('geometry', 'geometry.npz')}",
                "pair_cache_path": f"{final_scene_key}/{files.get('pair_cache', 'pair_cache.npz')}",
                "split": source_record.get("split", "train"),
                "files": files,
                "file_stats": scene_file_stats,
            }
        )
        if log_every and copied % log_every == 0:
            print(
                f"copied={copied}/{target_count} primary={role_counts.get('primary', 0)} supplement={role_counts.get('supplement', 0)}",
                flush=True,
            )

    manifest_path = output_dataset_dir / "manifest.jsonl"
    write_manifest_jsonl(manifest_path, manifest_records)
    summary = {
        "schema_version": "scenegen.vision_dataset.merge.summary.v1",
        "created_at": created_at,
        "dataset_dir": str(output_dataset_dir),
        "target_count": target_count,
        "scene_count": len(manifest_records),
        "manifest": "manifest.jsonl",
        "primary_dataset_dir": str(primary_dataset_dir),
        "supplement_dataset_dir": str(supplement_dataset_dir),
        "role_counts": role_counts,
        "skip_duplicate_scene_ids": skip_duplicate_scene_ids,
        "skipped_count": len(skipped),
        "file_size_totals_bytes": file_stat_totals,
    }
    report = {
        **summary,
        "elapsed_s": round(time.perf_counter() - started, 6),
        "records": manifest_records,
        "skipped_records": [
            {
                "source_dataset_dir": str(record.get("source_dataset_dir")),
                "source_scene_key": record.get("source_scene_key"),
                "scene_id": record.get("scene_id"),
                "skip_reason": record.get("skip_reason"),
            }
            for record in skipped
        ],
    }
    write_json(output_dataset_dir / "summary.json", summary)
    write_json(output_dataset_dir / "merge_report.json", report)
    print(
        "merged vision dataset:",
        f"copied={len(manifest_records)}",
        f"primary={role_counts.get('primary', 0)}",
        f"supplement={role_counts.get('supplement', 0)}",
        f"skipped={len(skipped)}",
        f"manifest={manifest_path}",
        flush=True,
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge compact SceneGen vision datasets into one re-numbered dataset.")
    parser.add_argument("primary_dataset_dir", type=Path, help="Primary compact vision dataset directory.")
    parser.add_argument("supplement_dataset_dir", type=Path, help="Supplement compact vision dataset directory.")
    parser.add_argument("output_dataset_dir", type=Path, help="Merged output dataset directory.")
    parser.add_argument("--target-count", type=int, default=3000, help="Final number of scenes to copy.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite output_dataset_dir if it already exists.")
    parser.add_argument(
        "--allow-duplicate-scene-ids",
        action="store_true",
        help="Do not skip supplement scenes whose scene_id already exists in the primary dataset.",
    )
    parser.add_argument("--log-every", type=int, default=100, help="Print progress every N copied scenes. 0 disables.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    merge_datasets(
        args.primary_dataset_dir,
        args.supplement_dataset_dir,
        args.output_dataset_dir,
        target_count=args.target_count,
        overwrite=args.overwrite,
        skip_duplicate_scene_ids=not args.allow_duplicate_scene_ids,
        log_every=args.log_every,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
