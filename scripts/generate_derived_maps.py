#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

try:
    from scipy import ndimage
except Exception:  # pragma: no cover - production environment should have scipy via the SceneGen stack.
    ndimage = None


CLASS_OUTDOOR = 0
CLASS_WALL = 1
CLASS_FREE_SPACE = 2
CLASS_FURNITURE = 3
DEFAULT_CLASS_NAMES = {
    CLASS_OUTDOOR: "outdoor",
    CLASS_WALL: "wall",
    CLASS_FREE_SPACE: "free_space",
    CLASS_FURNITURE: "furniture",
}


@dataclass(frozen=True)
class BsPoint:
    label: str
    position_m: tuple[float, float, float]
    source_label_files: tuple[str, ...]


@dataclass(frozen=True)
class SnappedBsPoint:
    label: str
    position_m: tuple[float, float, float]
    pixel_xy: tuple[int, int]
    source_label_files: tuple[str, ...]
    snapped: bool
    original_pixel_xy: tuple[int, int]
    snap_distance_m: float


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def portable_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def class_names_from_meta(meta: dict[str, Any]) -> dict[int, str]:
    classes = meta.get("classes") or {}
    names: dict[int, str] = {}
    for key, value in classes.items():
        try:
            class_id = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and isinstance(value.get("name"), str):
            names[class_id] = value["name"]
    return {**DEFAULT_CLASS_NAMES, **names}


def validate_class_mapping(class_names: dict[int, str]) -> None:
    expected = DEFAULT_CLASS_NAMES
    mismatches = {
        class_id: {"expected": name, "actual": class_names.get(class_id)}
        for class_id, name in expected.items()
        if class_names.get(class_id) != name
    }
    if mismatches:
        raise ValueError(f"class_mask class ids do not match SceneGen standard: {mismatches}")


def load_class_mask(scene_dir: Path) -> tuple[np.ndarray, dict[str, Any], dict[int, str]]:
    meta_path = scene_dir / "floorplan" / "class_mask_meta.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"missing {meta_path}")
    meta = read_json(meta_path)
    mask_rel = meta.get("npy") or "floorplan/class_mask.npy"
    mask_path = scene_dir / mask_rel
    if not mask_path.is_file():
        raise FileNotFoundError(f"missing {mask_path}")
    class_mask = np.load(mask_path)
    class_names = class_names_from_meta(meta)
    validate_class_mapping(class_names)
    if class_mask.ndim != 2:
        raise ValueError(f"class mask must be [H, W], got {class_mask.shape}")
    return class_mask.astype(np.uint8, copy=False), meta, class_names


def sdf_from_class_mask(class_mask: np.ndarray, r_max_m: float, meter_per_pixel: float) -> tuple[np.ndarray, np.ndarray]:
    if ndimage is None:
        raise RuntimeError("scipy.ndimage is required for SDF generation")
    wall = class_mask == CLASS_WALL
    outdoor = class_mask == CLASS_OUTDOOR
    dist_to_wall_px = ndimage.distance_transform_edt(~wall)
    dist_inside_wall_px = ndimage.distance_transform_edt(wall)
    sdf_m = np.zeros(class_mask.shape, dtype=np.float32)
    sdf_m[~wall] = dist_to_wall_px[~wall].astype(np.float32) * float(meter_per_pixel)
    sdf_m[wall] = -dist_inside_wall_px[wall].astype(np.float32) * float(meter_per_pixel)
    sdf_norm = np.clip(sdf_m, -r_max_m, r_max_m) / float(r_max_m)
    sdf_norm[outdoor] = 0.0
    sdf_valid_mask = (~outdoor).astype(np.uint8)
    return sdf_norm.astype(np.float16), sdf_valid_mask


def world_to_pixel(
    x_m: float,
    y_m: float,
    origin_xy_m: list[float],
    extent_xy_m: list[float],
    resolution_m: float,
) -> tuple[int, int]:
    min_x, min_y = float(origin_xy_m[0]), float(origin_xy_m[1])
    max_y = min_y + float(extent_xy_m[1])
    x_px = int(round((float(x_m) - min_x) / float(resolution_m)))
    y_px = int(round((max_y - float(y_m)) / float(resolution_m)))
    return x_px, y_px


def pixel_to_world(
    x_px: int,
    y_px: int,
    origin_xy_m: list[float],
    extent_xy_m: list[float],
    resolution_m: float,
) -> tuple[float, float]:
    min_x, min_y = float(origin_xy_m[0]), float(origin_xy_m[1])
    max_y = min_y + float(extent_xy_m[1])
    return min_x + float(x_px) * resolution_m, max_y - float(y_px) * resolution_m


def extract_position(point: dict[str, Any]) -> tuple[float, float, float] | None:
    position = point.get("position")
    if isinstance(position, list) and len(position) >= 3:
        return float(position[0]), float(position[1]), float(position[2])
    if {"x", "y", "z"} <= set(point):
        return float(point["x"]), float(point["y"]), float(point["z"])
    return None


def load_bs_points(scene_dir: Path) -> list[BsPoint]:
    label_dir = scene_dir / "label"
    if not label_dir.is_dir():
        return []
    by_position: dict[tuple[float, float, float], dict[str, Any]] = {}
    for label_file in sorted(label_dir.glob("*.json")):
        payload = read_json(label_file)
        for point in payload.get("bs_points") or []:
            if not isinstance(point, dict):
                continue
            position = extract_position(point)
            if position is None:
                continue
            key = tuple(round(value, 4) for value in position)
            record = by_position.setdefault(
                key,
                {
                    "label": str(point.get("label") or f"BS_{len(by_position)}"),
                    "position_m": position,
                    "source_label_files": set(),
                },
            )
            record["source_label_files"].add(f"label/{label_file.name}")
    bs_points: list[BsPoint] = []
    for record in by_position.values():
        bs_points.append(
            BsPoint(
                label=record["label"],
                position_m=tuple(float(value) for value in record["position_m"]),
                source_label_files=tuple(sorted(record["source_label_files"])),
            )
        )
    return bs_points


def nearest_valid_pixel(
    x_px: int,
    y_px: int,
    valid_mask: np.ndarray,
    max_radius_px: int,
) -> tuple[int, int, float] | None:
    height, width = valid_mask.shape
    if 0 <= x_px < width and 0 <= y_px < height and bool(valid_mask[y_px, x_px]):
        return x_px, y_px, 0.0
    if not valid_mask.any():
        return None
    if ndimage is not None and 0 <= x_px < width and 0 <= y_px < height:
        distances, indices = ndimage.distance_transform_edt(~valid_mask, return_indices=True)
        distance_px = float(distances[y_px, x_px])
        if distance_px <= max_radius_px:
            return int(indices[1, y_px, x_px]), int(indices[0, y_px, x_px]), distance_px
        return None

    x_min = max(0, x_px - max_radius_px)
    x_max = min(width - 1, x_px + max_radius_px)
    y_min = max(0, y_px - max_radius_px)
    y_max = min(height - 1, y_px + max_radius_px)
    best: tuple[int, int, float] | None = None
    for yy in range(y_min, y_max + 1):
        for xx in range(x_min, x_max + 1):
            if not bool(valid_mask[yy, xx]):
                continue
            distance_px = math.hypot(xx - x_px, yy - y_px)
            if distance_px <= max_radius_px and (best is None or distance_px < best[2]):
                best = (xx, yy, distance_px)
    return best


def snap_bs_points(
    bs_points: list[BsPoint],
    free_like_mask: np.ndarray,
    origin_xy_m: list[float],
    extent_xy_m: list[float],
    resolution_m: float,
    snap_radius_m: float,
) -> tuple[list[SnappedBsPoint], list[dict[str, Any]]]:
    max_radius_px = int(math.ceil(snap_radius_m / resolution_m))
    accepted: list[SnappedBsPoint] = []
    skipped: list[dict[str, Any]] = []
    for point in bs_points:
        x_px, y_px = world_to_pixel(point.position_m[0], point.position_m[1], origin_xy_m, extent_xy_m, resolution_m)
        nearest = nearest_valid_pixel(x_px, y_px, free_like_mask, max_radius_px)
        if nearest is None:
            skipped.append(
                {
                    "label": point.label,
                    "position_m": list(point.position_m),
                    "original_pixel_xy": [x_px, y_px],
                    "reason": "no_free_like_pixel_within_snap_radius",
                    "source_label_files": list(point.source_label_files),
                }
            )
            continue
        snapped_x, snapped_y, distance_px = nearest
        accepted.append(
            SnappedBsPoint(
                label=point.label,
                position_m=point.position_m,
                pixel_xy=(snapped_x, snapped_y),
                source_label_files=point.source_label_files,
                snapped=(snapped_x, snapped_y) != (x_px, y_px),
                original_pixel_xy=(x_px, y_px),
                snap_distance_m=float(distance_px) * resolution_m,
            )
        )
    return accepted, skipped


def ue_grid_mask(free_like_mask: np.ndarray, stride: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    height, width = free_like_mask.shape
    grid_h = int(math.ceil(height / stride))
    grid_w = int(math.ceil(width / stride))
    xs = np.minimum(np.floor(np.arange(grid_w) * stride + stride / 2.0).astype(np.int64), width - 1)
    ys = np.minimum(np.floor(np.arange(grid_h) * stride + stride / 2.0).astype(np.int64), height - 1)
    valid = free_like_mask[np.ix_(ys, xs)].astype(np.uint8)
    return valid, xs, ys


def dense_line_pixels(start_xy: tuple[int, int], end_xy: tuple[int, int]) -> list[tuple[int, int]]:
    x0, y0 = start_xy
    x1, y1 = end_xy
    steps = max(abs(x1 - x0), abs(y1 - y0)) * 2 + 1
    if steps <= 1:
        return [(x0, y0)]
    xs = np.rint(np.linspace(x0, x1, steps)).astype(np.int64)
    ys = np.rint(np.linspace(y0, y1, steps)).astype(np.int64)
    pixels: list[tuple[int, int]] = []
    previous: tuple[int, int] | None = None
    for x_value, y_value in zip(xs.tolist(), ys.tolist(), strict=True):
        pixel = (int(x_value), int(y_value))
        if pixel != previous:
            pixels.append(pixel)
            previous = pixel
    return pixels


def wall_segment_count(hits: list[bool]) -> int:
    count = 0
    in_wall = False
    for hit in hits:
        if hit and not in_wall:
            count += 1
            in_wall = True
        elif not hit:
            in_wall = False
    return min(count, 3)


def propagation_maps(
    wall_mask: np.ndarray,
    free_like_mask: np.ndarray,
    bs_pixels_xy: list[tuple[int, int]],
    stride: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ue_valid, xs, ys = ue_grid_mask(free_like_mask, stride)
    map_shape = (len(bs_pixels_xy), ue_valid.shape[0], ue_valid.shape[1])
    los_maps = np.zeros(map_shape, dtype=np.uint8)
    wall_count_maps = np.zeros(map_shape, dtype=np.uint8)
    height, width = wall_mask.shape
    for bs_index, bs_pixel in enumerate(bs_pixels_xy):
        for row, y_px in enumerate(ys.tolist()):
            for col, x_px in enumerate(xs.tolist()):
                if not bool(ue_valid[row, col]):
                    continue
                pixels = [
                    (x_value, y_value)
                    for x_value, y_value in dense_line_pixels(bs_pixel, (int(x_px), int(y_px)))
                    if 0 <= x_value < width and 0 <= y_value < height
                ]
                hits = [bool(wall_mask[y_value, x_value]) for x_value, y_value in pixels]
                count = wall_segment_count(hits)
                wall_count_maps[bs_index, row, col] = count
                los_maps[bs_index, row, col] = 1 if count == 0 else 0
    return los_maps, wall_count_maps, ue_valid


def checksum_summary(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": path.name,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def scene_dirs(run_dir: Path, scene_glob: str) -> list[Path]:
    return sorted(path for path in run_dir.glob(scene_glob) if path.is_dir())


def skip_reason(scene_dir: Path) -> str | None:
    required = [
        scene_dir / "floorplan" / "class_mask.npy",
        scene_dir / "floorplan" / "class_mask_meta.json",
        scene_dir / "floorplan" / "floorplan_1p60.png",
    ]
    for path in required:
        if not path.is_file():
            return f"missing_{path.relative_to(scene_dir).as_posix().replace('/', '_')}"
    if not (scene_dir / "label").is_dir():
        return "missing_label_dir"
    return None


def generate_maps_for_scene(
    scene_dir: Path,
    run_dir: Path,
    *,
    r_max_m: float,
    los_stride_pixels: int,
    snap_radius_m: float,
    overwrite: bool,
) -> dict[str, Any]:
    maps_dir = scene_dir / "maps"
    geometry_path = maps_dir / "geometry.npz"
    propagation_path = maps_dir / "propagation.npz"
    metadata_path = maps_dir / "metadata.json"
    if maps_dir.exists() and not overwrite and geometry_path.exists() and propagation_path.exists() and metadata_path.exists():
        return {
            "scene_key": scene_dir.name,
            "status": "skipped",
            "reason": "maps_already_exist",
            "maps_dir": portable_path(maps_dir, run_dir),
        }

    start = time.perf_counter()
    class_mask, class_meta, class_names = load_class_mask(scene_dir)
    resolution = float(class_meta["resolution_m_per_pixel"])
    origin_xy_m = [float(value) for value in class_meta["origin_xy_m"]]
    extent_xy_m = [float(value) for value in class_meta["extent_xy_m"]]
    free_like_mask = (class_mask == CLASS_FREE_SPACE) | (class_mask == CLASS_FURNITURE)
    wall_mask = class_mask == CLASS_WALL

    bs_points = load_bs_points(scene_dir)
    snapped_bs, skipped_bs = snap_bs_points(bs_points, free_like_mask, origin_xy_m, extent_xy_m, resolution, snap_radius_m)
    if not snapped_bs:
        raise ValueError("no valid BS points after snapping")

    sdf, sdf_valid_mask = sdf_from_class_mask(class_mask, r_max_m, resolution)
    los_maps, wall_count_maps, ue_valid_mask = propagation_maps(
        wall_mask,
        free_like_mask,
        [point.pixel_xy for point in snapped_bs],
        los_stride_pixels,
    )
    if np.any((los_maps == 1) & (wall_count_maps != 0)):
        raise ValueError("LoS/wall-count consistency failed: los=1 with wall_count!=0")
    if np.any((wall_count_maps > 0) & (los_maps != 0)):
        raise ValueError("LoS/wall-count consistency failed: wall_count>0 with los!=0")

    maps_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        geometry_path,
        sdf=sdf,
        sdf_valid_mask=sdf_valid_mask,
        meter_per_pixel=np.asarray(resolution, dtype=np.float32),
        r_max_m=np.asarray(r_max_m, dtype=np.float32),
        height=np.asarray(class_mask.shape[0], dtype=np.int32),
        width=np.asarray(class_mask.shape[1], dtype=np.int32),
    )
    bs_coords_px = np.asarray([point.pixel_xy for point in snapped_bs], dtype=np.float32)
    bs_coords_m = np.asarray(
        [pixel_to_world(point.pixel_xy[0], point.pixel_xy[1], origin_xy_m, extent_xy_m, resolution) for point in snapped_bs],
        dtype=np.float32,
    )
    np.savez_compressed(
        propagation_path,
        bs_coords_px=bs_coords_px,
        bs_coords_m=bs_coords_m,
        los_maps=los_maps,
        wall_count_maps=wall_count_maps,
        ue_valid_mask=ue_valid_mask,
        los_stride_pixels=np.asarray(los_stride_pixels, dtype=np.int32),
        meter_per_pixel=np.asarray(resolution, dtype=np.float32),
        height=np.asarray(class_mask.shape[0], dtype=np.int32),
        width=np.asarray(class_mask.shape[1], dtype=np.int32),
    )

    metadata = {
        "schema_version": "scenegen.derived_maps.v1",
        "scene_key": scene_dir.name,
        "scene_id": class_meta.get("scene_id"),
        "generated_at": utc_now_iso(),
        "source_scene_dir": portable_path(scene_dir, run_dir),
        "class_mapping": {str(key): value for key, value in sorted(class_names.items())},
        "resolution_m_per_pixel": resolution,
        "origin_xy_m": origin_xy_m,
        "extent_xy_m": extent_xy_m,
        "grid_shape": [int(class_mask.shape[0]), int(class_mask.shape[1])],
        "parameters": {
            "r_max_m": r_max_m,
            "los_stride_pixels": los_stride_pixels,
            "furniture_as_free": True,
            "snap_radius_m": snap_radius_m,
            "sdf_storage": "float16_normalized",
        },
        "bs_points": [
            {
                "label": point.label,
                "position_m": list(point.position_m),
                "pixel_xy": list(point.pixel_xy),
                "source_label_files": list(point.source_label_files),
                "snapped": point.snapped,
                "original_pixel_xy": list(point.original_pixel_xy),
                "snap_distance_m": round(point.snap_distance_m, 6),
            }
            for point in snapped_bs
        ],
        "skipped_bs_points": skipped_bs,
        "counts": {
            "bs": len(snapped_bs),
            "skipped_bs": len(skipped_bs),
            "ue_valid_grid": int(ue_valid_mask.sum()),
            "los_positive": int(los_maps.sum()),
            "wall_count_positive": int((wall_count_maps > 0).sum()),
            "sdf_valid_pixels": int(sdf_valid_mask.sum()),
        },
        "shapes": {
            "sdf": list(sdf.shape),
            "los_maps": list(los_maps.shape),
            "wall_count_maps": list(wall_count_maps.shape),
            "ue_valid_mask": list(ue_valid_mask.shape),
        },
        "files": {
            "geometry": portable_path(geometry_path, scene_dir),
            "propagation": portable_path(propagation_path, scene_dir),
        },
        "file_stats": {
            "geometry": checksum_summary(geometry_path),
            "propagation": checksum_summary(propagation_path),
        },
        "elapsed_s": round(time.perf_counter() - start, 6),
    }
    write_json(metadata_path, metadata)
    return {
        "scene_key": scene_dir.name,
        "scene_id": class_meta.get("scene_id"),
        "status": "generated",
        "maps_dir": portable_path(maps_dir, run_dir),
        "geometry": portable_path(geometry_path, run_dir),
        "propagation": portable_path(propagation_path, run_dir),
        "metadata": portable_path(metadata_path, run_dir),
        "bs_count": len(snapped_bs),
        "skipped_bs_count": len(skipped_bs),
        "ue_valid_grid_count": int(ue_valid_mask.sum()),
        "elapsed_s": round(time.perf_counter() - start, 6),
    }


def write_manifest_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def process_scene_job(job: dict[str, Any]) -> dict[str, Any]:
    scene_dir = Path(job["scene_dir"])
    run_dir = Path(job["run_dir"])
    index = int(job["index"])
    reason = skip_reason(scene_dir)
    if reason:
        return {"index": index, "scene_key": scene_dir.name, "status": "skipped", "reason": reason}
    try:
        record = generate_maps_for_scene(
            scene_dir,
            run_dir,
            r_max_m=float(job["r_max_m"]),
            los_stride_pixels=int(job["los_stride_pixels"]),
            snap_radius_m=float(job["snap_radius_m"]),
            overwrite=bool(job["overwrite"]),
        )
    except Exception as exc:  # noqa: BLE001 - keep batch post-processing robust.
        return {
            "index": index,
            "scene_key": scene_dir.name,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    record["index"] = index
    return record


def update_counts(record: dict[str, Any], counts: dict[str, int]) -> None:
    status = str(record["status"])
    counts[status] = counts.get(status, 0) + 1


def print_progress(done: int, total: int, counts: dict[str, int]) -> None:
    print(
        f"processed={done}/{total} "
        f"generated={counts.get('generated', 0)} "
        f"skipped={counts.get('skipped', 0)} "
        f"failed={counts.get('failed', 0)}",
        flush=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate SceneGen derived supervision maps from existing class masks.")
    parser.add_argument("run_dir", type=Path, help="SceneGen run directory.")
    parser.add_argument("--scene-glob", default="front3d_*", help="Scene directory glob under run_dir.")
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N scene directories after filtering.")
    parser.add_argument("--los-stride-pixels", type=int, default=4, help="LoS/wall-count UE grid stride in pixels.")
    parser.add_argument("--r-max-m", type=float, default=3.0, help="SDF clipping radius in meters.")
    parser.add_argument("--snap-radius-m", type=float, default=0.25, help="Max radius for snapping BS to free-like pixels.")
    parser.add_argument("--overwrite", action="store_true", help="Regenerate scene maps even if maps files already exist.")
    parser.add_argument("--log-every", type=int, default=25, help="Print progress every N generated/skipped scenes. 0 disables.")
    parser.add_argument("--workers", type=int, default=1, help="Number of parallel scene workers. Default keeps legacy sequential mode.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started = time.perf_counter()
    run_dir = args.run_dir.resolve()
    records: list[dict[str, Any]] = []
    candidates = scene_dirs(run_dir, args.scene_glob)
    if args.limit is not None:
        candidates = candidates[: args.limit]
    if args.workers < 1:
        raise ValueError("--workers must be >= 1")
    jobs = [
        {
            "index": index,
            "scene_dir": str(scene_dir),
            "run_dir": str(run_dir),
            "r_max_m": args.r_max_m,
            "los_stride_pixels": args.los_stride_pixels,
            "snap_radius_m": args.snap_radius_m,
            "overwrite": args.overwrite,
        }
        for index, scene_dir in enumerate(candidates, start=1)
    ]

    status_counts: dict[str, int] = {}
    if args.workers == 1:
        for done, job in enumerate(jobs, start=1):
            record = process_scene_job(job)
            update_counts(record, status_counts)
            records.append(record)
            if args.log_every and done % args.log_every == 0:
                print_progress(done, len(jobs), status_counts)
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(process_scene_job, job) for job in jobs]
            for done, future in enumerate(as_completed(futures), start=1):
                record = future.result()
                update_counts(record, status_counts)
                records.append(record)
                if args.log_every and done % args.log_every == 0:
                    print_progress(done, len(jobs), status_counts)
    if args.log_every and len(jobs) % args.log_every != 0:
        print_progress(len(jobs), len(jobs), status_counts)

    manifest_path = run_dir / "derived_maps_manifest.jsonl"
    report_path = run_dir / "derived_maps_report.json"
    records = sorted(records, key=lambda record: int(record.get("index", 0)))
    manifest_records = [{key: value for key, value in record.items() if key != "index"} for record in records]
    write_manifest_jsonl(manifest_path, manifest_records)
    report = {
        "schema_version": "scenegen.derived_maps.run.v1",
        "run_dir": str(run_dir),
        "generated_at": utc_now_iso(),
        "scene_glob": args.scene_glob,
        "limit": args.limit,
        "parameters": {
            "r_max_m": args.r_max_m,
            "los_stride_pixels": args.los_stride_pixels,
            "snap_radius_m": args.snap_radius_m,
            "furniture_as_free": True,
            "workers": args.workers,
        },
        "total_candidates": len(candidates),
        "status_counts": status_counts,
        "elapsed_s": round(time.perf_counter() - started, 6),
        "manifest": manifest_path.name,
    }
    write_json(report_path, report)
    print(
        "derived maps:",
        f"generated={status_counts.get('generated', 0)}",
        f"skipped={status_counts.get('skipped', 0)}",
        f"failed={status_counts.get('failed', 0)}",
        f"manifest={manifest_path}",
        flush=True,
    )
    return 1 if status_counts.get("failed", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
