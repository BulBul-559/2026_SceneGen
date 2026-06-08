#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

try:
    from scipy import ndimage
except Exception:  # pragma: no cover - optional acceleration path.
    ndimage = None


SCALAR_PRIORITY = [
    "scene_key",
    "scene_dir",
    "scene_id",
    "batch_status",
    "width_m",
    "depth_m",
    "bbox_area_m2",
    "indoor_area_m2",
    "bbox_fill_ratio",
    "aspect_ratio",
    "aspect_abs",
    "orientation",
    "nearest_aspect_name",
    "nearest_aspect_ratio",
    "nearest_aspect_error",
    "nearest_aspect_error_pct",
    "aspect_class",
    "size_class",
    "layout_class",
    "visual_complexity",
    "room_count",
    "room_area_min_m2",
    "room_area_max_m2",
    "room_area_mean_m2",
    "room_area_median_m2",
    "room_area_std_m2",
    "largest_room_ratio",
    "skipped_room_count",
    "placement_count",
    "furniture_count",
    "furniture_area_m2",
    "furniture_area_ratio",
    "furniture_density_per_m2",
    "wall_pixel_ratio",
    "free_pixel_ratio",
    "furniture_pixel_ratio",
    "outdoor_pixel_ratio",
    "mask_entropy",
    "edge_density",
    "free_component_count",
    "largest_free_component_ratio",
]

ASPECT_TARGETS = [
    ("1:3", 1.0 / 3.0),
    ("1:2", 1.0 / 2.0),
    ("9:16", 9.0 / 16.0),
    ("3:4", 3.0 / 4.0),
    ("1:1", 1.0),
    ("4:3", 4.0 / 3.0),
    ("16:9", 16.0 / 9.0),
    ("2:1", 2.0),
    ("3:1", 3.0),
]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class AnalysisLogger:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.output_dir / "analysis_events.jsonl"
        self.events_path.write_text("", encoding="utf-8")

    def log(self, event_type: str, **payload: Any) -> None:
        event = {
            "ts": utc_now_iso(),
            "event_type": event_type,
            **payload,
        }
        with self.events_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def round_float(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return compact_json(value)
    if value is None:
        return ""
    return value


def nearest_aspect_ratio(aspect_ratio: float | None) -> dict[str, float | str | None]:
    if aspect_ratio is None or aspect_ratio <= 0:
        return {
            "nearest_aspect_name": None,
            "nearest_aspect_ratio": None,
            "nearest_aspect_error": None,
            "nearest_aspect_error_pct": None,
        }
    name, target = min(ASPECT_TARGETS, key=lambda item: abs(math.log(aspect_ratio / item[1])))
    error = abs(aspect_ratio - target)
    return {
        "nearest_aspect_name": name,
        "nearest_aspect_ratio": target,
        "nearest_aspect_error": error,
        "nearest_aspect_error_pct": error / target,
    }


def classify_orientation(
    width_m: float | None,
    depth_m: float | None,
) -> tuple[str | None, float | None, float | None, str | None, dict[str, float | str | None]]:
    if not width_m or not depth_m:
        return None, None, None, None, nearest_aspect_ratio(None)
    aspect_ratio = width_m / depth_m
    aspect_abs = max(width_m, depth_m) / min(width_m, depth_m)
    if 0.9 <= aspect_ratio <= 1.1:
        orientation = "square"
    elif aspect_ratio > 1.1:
        orientation = "wide"
    else:
        orientation = "tall"

    if orientation == "square":
        aspect_class = "square"
    elif orientation == "wide":
        if aspect_abs < 1.55:
            aspect_class = "wide_4_3"
        elif aspect_abs < 2.2:
            aspect_class = "wide_16_9"
        else:
            aspect_class = "panoramic"
    else:
        if aspect_abs < 1.55:
            aspect_class = "tall_3_4"
        elif aspect_abs < 2.2:
            aspect_class = "tall_9_16"
        else:
            aspect_class = "vertical_strip"
    return orientation, aspect_ratio, aspect_abs, aspect_class, nearest_aspect_ratio(aspect_ratio)


def classify_size(indoor_area_m2: float | None) -> str | None:
    if indoor_area_m2 is None:
        return None
    if indoor_area_m2 < 40:
        return "small"
    if indoor_area_m2 < 90:
        return "medium"
    if indoor_area_m2 < 160:
        return "large"
    return "xlarge"


def classify_room_complexity(room_count: int | None) -> str | None:
    if room_count is None:
        return None
    if room_count <= 3:
        return "few_rooms"
    if room_count <= 7:
        return "medium_rooms"
    return "many_rooms"


def classify_furniture_complexity(furniture_ratio: float | None) -> str | None:
    if furniture_ratio is None:
        return None
    if furniture_ratio < 0.12:
        return "sparse"
    if furniture_ratio < 0.25:
        return "medium"
    return "dense"


def classify_visual_complexity(
    room_count: int | None,
    wall_ratio: float | None,
    furniture_ratio: float | None,
    free_component_count: int | None,
) -> str | None:
    if room_count is None and wall_ratio is None and furniture_ratio is None and free_component_count is None:
        return None
    room_count = room_count or 0
    wall_ratio = wall_ratio or 0.0
    furniture_ratio = furniture_ratio or 0.0
    free_component_count = free_component_count or 0
    if room_count >= 8 or wall_ratio >= 0.30 or furniture_ratio >= 0.25 or free_component_count >= 4:
        return "complex"
    if room_count <= 4 and wall_ratio < 0.20 and furniture_ratio < 0.15 and free_component_count <= 2:
        return "simple"
    return "moderate"


def mask_entropy(class_counts: dict[str, int]) -> float | None:
    total = sum(class_counts.values())
    if total <= 0:
        return None
    entropy = 0.0
    for count in class_counts.values():
        if count <= 0:
            continue
        p = count / total
        entropy -= p * math.log2(p)
    return entropy


def load_class_mask(scene_dir: Path, class_mask_meta: dict[str, Any]) -> np.ndarray | None:
    npy_rel = class_mask_meta.get("npy")
    if npy_rel:
        npy_path = scene_dir / str(npy_rel)
        if npy_path.exists():
            return np.load(npy_path)

    mask_rel = class_mask_meta.get("mask")
    if not mask_rel:
        return None
    mask_path = scene_dir / str(mask_rel)
    if not mask_path.exists():
        return None
    image = Image.open(mask_path)
    array = np.asarray(image)
    if array.ndim == 2:
        return array.astype(np.uint8)
    return None


def connected_component_stats(mask: np.ndarray, target_value: int) -> dict[str, int | float | None]:
    target = mask == target_value
    total = int(target.sum())
    if total == 0:
        return {
            "component_count": 0,
            "largest_component_pixels": 0,
            "largest_component_ratio": None,
        }

    if ndimage is not None:
        structure = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8)
        labels, component_count = ndimage.label(target, structure=structure)
        component_sizes = np.bincount(labels.ravel())
        largest = int(component_sizes[1:].max()) if component_count > 0 else 0
        return {
            "component_count": int(component_count),
            "largest_component_pixels": largest,
            "largest_component_ratio": largest / total if total else None,
        }

    visited = np.zeros(target.shape, dtype=bool)
    component_count = 0
    largest = 0
    height, width = target.shape
    ys, xs = np.nonzero(target)
    for start_y, start_x in zip(ys.tolist(), xs.tolist(), strict=True):
        if visited[start_y, start_x]:
            continue
        component_count += 1
        size = 0
        stack = [(start_y, start_x)]
        visited[start_y, start_x] = True
        while stack:
            y, x = stack.pop()
            size += 1
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if ny < 0 or ny >= height or nx < 0 or nx >= width:
                    continue
                if visited[ny, nx] or not target[ny, nx]:
                    continue
                visited[ny, nx] = True
                stack.append((ny, nx))
        largest = max(largest, size)

    return {
        "component_count": component_count,
        "largest_component_pixels": largest,
        "largest_component_ratio": largest / total if total else None,
    }


def edge_density(mask: np.ndarray) -> float | None:
    if mask.size == 0:
        return None
    horizontal = mask[:, 1:] != mask[:, :-1]
    vertical = mask[1:, :] != mask[:-1, :]
    edge_count = int(horizontal.sum() + vertical.sum())
    possible = horizontal.size + vertical.size
    if possible == 0:
        return None
    return edge_count / possible


def summarize_numbers(records: list[dict[str, Any]], fields: list[str]) -> dict[str, dict[str, float | int]]:
    summary: dict[str, dict[str, float | int]] = {}
    for field in fields:
        values = [safe_float(record.get(field)) for record in records]
        numbers = [value for value in values if value is not None]
        if not numbers:
            continue
        summary[field] = {
            "count": len(numbers),
            "min": round(min(numbers), 6),
            "max": round(max(numbers), 6),
            "mean": round(statistics.fmean(numbers), 6),
            "median": round(statistics.median(numbers), 6),
        }
    return summary


def summarize_scene_elapsed(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "p90": None,
            "p95": None,
        }
    sorted_values = sorted(values)
    p90_index = int(0.9 * (len(sorted_values) - 1))
    p95_index = int(0.95 * (len(sorted_values) - 1))
    return {
        "count": len(values),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "mean": round(statistics.fmean(values), 6),
        "median": round(statistics.median(values), 6),
        "p90": round(sorted_values[p90_index], 6),
        "p95": round(sorted_values[p95_index], 6),
    }


def pick_label_report(scene_dir: Path, preferred: str) -> tuple[Path | None, dict[str, Any] | None]:
    report_dir = scene_dir / "label" / "report"
    if not report_dir.exists():
        return None, None
    preferred_path = report_dir / f"{preferred}_report.json"
    if preferred_path.exists():
        return preferred_path, read_json(preferred_path)
    reports = sorted(report_dir.glob("label_*_report.json"))
    if not reports:
        return None, None
    return reports[0], read_json(reports[0])


def room_metrics(label_report: dict[str, Any] | None) -> dict[str, Any]:
    if not label_report:
        return {}
    rooms = label_report.get("rooms") or []
    room_areas: list[float] = []
    room_type_counts: Counter[str] = Counter()
    connected_area_m2 = 0.0
    connected_room_count = 0
    for room in rooms:
        area = safe_float(room.get("floor_area_m2"))
        is_corridor = bool(room.get("is_corridor"))
        if area is not None and is_corridor:
            connected_area_m2 += area
            connected_room_count += 1
            continue
        if area is not None:
            room_areas.append(area)
        room_type = str(room.get("room_type") or "Unknown")
        if not is_corridor:
            room_type_counts[room_type] += 1

    metrics: dict[str, Any] = {
        "room_count": len(room_areas),
        "skipped_room_count": label_report.get("skipped_room_count"),
        "room_type_counts": dict(sorted(room_type_counts.items())),
        "connected_area_m2": round_float(connected_area_m2),
        "connected_area_count": connected_room_count,
    }
    if room_areas:
        total = sum(room_areas)
        metrics.update(
            {
                "room_area_min_m2": round_float(min(room_areas)),
                "room_area_max_m2": round_float(max(room_areas)),
                "room_area_mean_m2": round_float(statistics.fmean(room_areas)),
                "room_area_median_m2": round_float(statistics.median(room_areas)),
                "room_area_std_m2": round_float(statistics.pstdev(room_areas) if len(room_areas) > 1 else 0.0),
                "largest_room_ratio": round_float(max(room_areas) / total if total else None),
            }
        )
    return metrics


def label_variant_metrics(scene_dir: Path) -> dict[str, Any]:
    report_dir = scene_dir / "label" / "report"
    if not report_dir.exists():
        return {}
    metrics: dict[str, Any] = {}
    for path in sorted(report_dir.glob("label_*_report.json")):
        name = path.stem.removesuffix("_report")
        data = read_json(path)
        metrics[f"{name}_ok"] = bool(data.get("ok"))
        metrics[f"{name}_ue_count"] = data.get("ue_count")
        metrics[f"{name}_bs_count"] = data.get("bs_count")
        if name.startswith("label_panel_"):
            grid = name.removeprefix("label_panel_")
            walk_path = report_dir / f"label_walk_{grid}_report.json"
            if walk_path.exists():
                walk = read_json(walk_path)
                panel_ue = safe_float(data.get("ue_count"))
                walk_ue = safe_float(walk.get("ue_count"))
                metrics[f"label_walk_panel_ratio_{grid}"] = round_float(ratio(walk_ue, panel_ue))
    return metrics


def analyze_scene(scene_dir: Path, batch_status: str | None, preferred_label: str, skip_components: bool) -> dict[str, Any] | None:
    statistics_path = scene_dir / "statistics.json"
    floorplan_meta_path = scene_dir / "floorplan" / "meta.json"
    class_mask_meta_path = scene_dir / "floorplan" / "class_mask_meta.json"
    placements_path = scene_dir / "placements.json"
    if not statistics_path.exists() or not class_mask_meta_path.exists():
        return None

    statistics_data = read_json(statistics_path)
    class_mask_meta = read_json(class_mask_meta_path)
    floorplan_meta = read_json(floorplan_meta_path) if floorplan_meta_path.exists() else {}
    placements = read_json(placements_path) if placements_path.exists() else {}
    _, preferred_report = pick_label_report(scene_dir, preferred_label)

    extent = class_mask_meta.get("extent_xy_m") or floorplan_meta.get("extent_xy_m") or [None, None]
    width_m = safe_float(extent[0] if len(extent) > 0 else None)
    depth_m = safe_float(extent[1] if len(extent) > 1 else None)
    resolution = safe_float(class_mask_meta.get("resolution_m_per_pixel"))
    class_counts = {str(k): int(v) for k, v in (class_mask_meta.get("class_counts") or {}).items()}
    total_pixels = sum(class_counts.values())
    pixel_area = resolution * resolution if resolution else None

    outdoor_pixels = class_counts.get("outdoor", 0)
    wall_pixels = class_counts.get("wall", 0)
    free_pixels = class_counts.get("free_space", 0)
    furniture_pixels = class_counts.get("furniture", 0)
    indoor_pixels = wall_pixels + free_pixels + furniture_pixels
    bbox_area = width_m * depth_m if width_m is not None and depth_m is not None else None
    indoor_area = indoor_pixels * pixel_area if pixel_area is not None else None
    furniture_area = furniture_pixels * pixel_area if pixel_area is not None else safe_float(statistics_data.get("total_footprint_area_m2"))

    orientation, aspect_ratio, aspect_abs, aspect_class, nearest_aspect = classify_orientation(width_m, depth_m)
    room_data = room_metrics(preferred_report)
    room_count = int(room_data.get("room_count") or 0)

    free_component_count = None
    largest_free_component_ratio = None
    edge_ratio = None
    if not skip_components:
        class_mask = load_class_mask(scene_dir, class_mask_meta)
        if class_mask is not None:
            free_components = connected_component_stats(class_mask, 2)
            free_component_count = free_components["component_count"]
            largest_free_component_ratio = free_components["largest_component_ratio"]
            edge_ratio = edge_density(class_mask)

    wall_ratio = ratio(wall_pixels, total_pixels)
    free_ratio = ratio(free_pixels, total_pixels)
    furniture_ratio_pixels = ratio(furniture_pixels, total_pixels)
    outdoor_ratio = ratio(outdoor_pixels, total_pixels)
    furniture_area_ratio = ratio(furniture_area, indoor_area)
    object_count_by_class = statistics_data.get("object_count_by_class") or {}
    furniture_count = int(class_mask_meta.get("furniture_count") or statistics_data.get("placement_count") or 0)

    size_class = classify_size(indoor_area)
    furniture_complexity = classify_furniture_complexity(furniture_area_ratio)
    room_complexity = classify_room_complexity(room_count)
    visual_complexity = classify_visual_complexity(room_count, wall_ratio, furniture_area_ratio, free_component_count)
    layout_class = "_".join(part for part in [size_class, aspect_class] if part)

    record: dict[str, Any] = {
        "scene_key": scene_dir.name,
        "scene_dir": str(scene_dir),
        "scene_id": class_mask_meta.get("scene_id") or placements.get("front3d_scene_id"),
        "batch_status": batch_status,
        "width_m": round_float(width_m),
        "depth_m": round_float(depth_m),
        "bbox_area_m2": round_float(bbox_area),
        "indoor_area_m2": round_float(indoor_area),
        "bbox_fill_ratio": round_float(ratio(indoor_area, bbox_area)),
        "aspect_ratio": round_float(aspect_ratio),
        "aspect_abs": round_float(aspect_abs),
        "orientation": orientation,
        "nearest_aspect_name": nearest_aspect["nearest_aspect_name"],
        "nearest_aspect_ratio": round_float(safe_float(nearest_aspect["nearest_aspect_ratio"])),
        "nearest_aspect_error": round_float(safe_float(nearest_aspect["nearest_aspect_error"])),
        "nearest_aspect_error_pct": round_float(safe_float(nearest_aspect["nearest_aspect_error_pct"])),
        "aspect_class": aspect_class,
        "size_class": size_class,
        "layout_class": layout_class or None,
        "room_complexity": room_complexity,
        "furniture_complexity": furniture_complexity,
        "visual_complexity": visual_complexity,
        "placement_count": statistics_data.get("placement_count"),
        "furniture_count": furniture_count,
        "furniture_area_m2": round_float(furniture_area),
        "furniture_area_ratio": round_float(furniture_area_ratio),
        "furniture_density_per_m2": round_float(ratio(furniture_count, indoor_area)),
        "wall_pixel_ratio": round_float(wall_ratio),
        "free_pixel_ratio": round_float(free_ratio),
        "furniture_pixel_ratio": round_float(furniture_ratio_pixels),
        "outdoor_pixel_ratio": round_float(outdoor_ratio),
        "mask_entropy": round_float(mask_entropy(class_counts)),
        "edge_density": round_float(edge_ratio),
        "free_component_count": free_component_count,
        "largest_free_component_ratio": round_float(largest_free_component_ratio),
        "class_counts": class_counts,
        "object_count_by_class": object_count_by_class,
    }
    record.update(room_data)
    record.update(label_variant_metrics(scene_dir))
    return record


def batch_status_by_task(run_dir: Path) -> dict[str, str]:
    state_path = run_dir / "batch" / "state.json"
    if not state_path.exists():
        return {}
    state = read_json(state_path)
    return {str(task_id): str(task.get("status")) for task_id, task in (state.get("tasks") or {}).items()}


def scene_dirs(run_dir: Path, scene_glob: str) -> list[Path]:
    return sorted(path for path in run_dir.glob(scene_glob) if path.is_dir())


def skip_reason(scene_dir: Path) -> str | None:
    if not (scene_dir / "statistics.json").exists():
        return "missing_statistics_json"
    if not (scene_dir / "floorplan" / "class_mask_meta.json").exists():
        return "missing_class_mask_meta_json"
    return None


def write_outputs(records: list[dict[str, Any]], output_dir: Path, run_dir: Path, skipped_count: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    fieldnames = list(SCALAR_PRIORITY)
    all_keys = sorted({key for record in records for key in record})
    fieldnames.extend(key for key in all_keys if key not in fieldnames)

    csv_path = output_dir / "scene_stats.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({key: csv_value(record.get(key)) for key in fieldnames})

    jsonl_path = output_dir / "scene_stats.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    categorical_fields = [
        "size_class",
        "orientation",
        "aspect_class",
        "nearest_aspect_name",
        "layout_class",
        "room_complexity",
        "furniture_complexity",
        "visual_complexity",
    ]
    summary = {
        "run_dir": str(run_dir),
        "scene_count": len(records),
        "skipped_scene_count": skipped_count,
        "categorical_counts": {
            field: dict(sorted(Counter(record.get(field) for record in records if record.get(field)).items()))
            for field in categorical_fields
        },
        "numeric_summary": summarize_numbers(
            records,
            [
                "width_m",
                "depth_m",
                "bbox_area_m2",
                "indoor_area_m2",
                "bbox_fill_ratio",
                "aspect_ratio",
                "aspect_abs",
                "nearest_aspect_ratio",
                "nearest_aspect_error",
                "nearest_aspect_error_pct",
                "room_count",
                "room_area_min_m2",
                "room_area_max_m2",
                "room_area_mean_m2",
                "largest_room_ratio",
                "furniture_area_ratio",
                "wall_pixel_ratio",
                "free_pixel_ratio",
                "furniture_pixel_ratio",
                "free_component_count",
                "edge_density",
                "mask_entropy",
            ],
        ),
    }
    write_json(output_dir / "summary.json", summary)
    write_markdown_report(output_dir / "report.md", summary)


def write_markdown_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Scene Dataset Statistics",
        "",
        f"- Run: `{summary['run_dir']}`",
        f"- Analyzed scenes: `{summary['scene_count']}`",
        f"- Skipped scenes: `{summary['skipped_scene_count']}`",
        "",
        "## Categorical Counts",
        "",
    ]
    for field, counts in summary["categorical_counts"].items():
        lines.append(f"### {field}")
        if not counts:
            lines.append("")
            lines.append("_No data_")
            lines.append("")
            continue
        lines.append("")
        lines.append("| value | count |")
        lines.append("| --- | ---: |")
        for key, value in counts.items():
            lines.append(f"| `{key}` | {value} |")
        lines.append("")

    lines.extend(["## Numeric Summary", "", "| metric | count | min | median | mean | max |", "| --- | ---: | ---: | ---: | ---: | ---: |"])
    for field, stats in summary["numeric_summary"].items():
        lines.append(
            f"| `{field}` | {stats['count']} | {stats['min']} | {stats['median']} | {stats['mean']} | {stats['max']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze SceneGen run outputs for visual-layout training statistics.")
    parser.add_argument("run_dir", type=Path, help="SceneGen run directory, e.g. results/front3d_production_...")
    parser.add_argument("--output-dir", type=Path, default=None, help="Directory for report files. Defaults to <run_dir>/analysis_report.")
    parser.add_argument("--scene-glob", default="front3d_*", help="Scene directory glob under run_dir.")
    parser.add_argument("--preferred-label", default="label_panel_0p1", help="Label report used for room-area statistics.")
    parser.add_argument("--limit", type=int, default=None, help="Analyze only the first N complete scene directories.")
    parser.add_argument("--skip-components", action="store_true", help="Skip connected-component and edge-density image analysis.")
    parser.add_argument("--log-every", type=int, default=50, help="Print progress every N analyzed scenes. Set 0 to disable.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    overall_start = time.perf_counter()
    started_at = utc_now_iso()
    run_dir = args.run_dir.resolve()
    output_dir = args.output_dir.resolve() if args.output_dir else run_dir / "analysis_report"
    logger = AnalysisLogger(output_dir)
    logger.log(
        "analysis_started",
        run_dir=str(run_dir),
        output_dir=str(output_dir),
        scene_glob=args.scene_glob,
        preferred_label=args.preferred_label,
        limit=args.limit,
        skip_components=args.skip_components,
    )

    discover_start = time.perf_counter()
    discovered_scene_dirs = scene_dirs(run_dir, args.scene_glob)
    discover_elapsed = time.perf_counter() - discover_start
    logger.log("scene_discovery_finished", discovered_count=len(discovered_scene_dirs), elapsed_s=round(discover_elapsed, 6))

    status_start = time.perf_counter()
    status_by_task = batch_status_by_task(run_dir)
    status_elapsed = time.perf_counter() - status_start
    logger.log("batch_status_loaded", task_count=len(status_by_task), elapsed_s=round(status_elapsed, 6))

    records: list[dict[str, Any]] = []
    skipped = 0
    errors = 0
    scene_elapsed_values: list[float] = []
    analysis_start = time.perf_counter()
    for scene_dir in discovered_scene_dirs:
        scene_start = time.perf_counter()
        reason = skip_reason(scene_dir)
        if reason:
            skipped += 1
            logger.log(
                "scene_skipped",
                scene_key=scene_dir.name,
                reason=reason,
                elapsed_s=round(time.perf_counter() - scene_start, 6),
            )
            continue
        try:
            record = analyze_scene(
                scene_dir,
                batch_status=status_by_task.get(scene_dir.name),
                preferred_label=args.preferred_label,
                skip_components=args.skip_components,
            )
        except Exception as exc:  # noqa: BLE001 - keep the report robust for partially generated production runs.
            errors += 1
            logger.log(
                "scene_error",
                scene_key=scene_dir.name,
                error_type=type(exc).__name__,
                error=str(exc),
                elapsed_s=round(time.perf_counter() - scene_start, 6),
            )
            continue
        if record is None:
            skipped += 1
            logger.log(
                "scene_skipped",
                scene_key=scene_dir.name,
                reason="incomplete_scene_outputs",
                elapsed_s=round(time.perf_counter() - scene_start, 6),
            )
            continue
        scene_elapsed = time.perf_counter() - scene_start
        scene_elapsed_values.append(scene_elapsed)
        records.append(record)
        logger.log(
            "scene_analyzed",
            scene_key=scene_dir.name,
            batch_status=record.get("batch_status"),
            scene_id=record.get("scene_id"),
            elapsed_s=round(scene_elapsed, 6),
            width_m=record.get("width_m"),
            depth_m=record.get("depth_m"),
            room_count=record.get("room_count"),
            visual_complexity=record.get("visual_complexity"),
        )
        if args.log_every > 0 and len(records) % args.log_every == 0:
            print(f"analyzed={len(records)} skipped={skipped} errors={errors} last_scene={scene_dir.name}")
        if args.limit is not None and len(records) >= args.limit:
            break
    analysis_elapsed = time.perf_counter() - analysis_start

    write_start = time.perf_counter()
    write_outputs(records, output_dir, run_dir, skipped)
    write_elapsed = time.perf_counter() - write_start

    total_elapsed = time.perf_counter() - overall_start
    scene_elapsed_summary = summarize_scene_elapsed(scene_elapsed_values)
    run_payload = {
        "run_dir": str(run_dir),
        "output_dir": str(output_dir),
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "analyzed_scene_count": len(records),
        "skipped_scene_count": skipped,
        "error_scene_count": errors,
        "discovered_scene_count": len(discovered_scene_dirs),
        "limit": args.limit,
        "skip_components": args.skip_components,
        "elapsed_s": round(total_elapsed, 6),
        "throughput_scenes_per_second": round(len(records) / total_elapsed, 6) if total_elapsed > 0 else None,
        "throughput_scenes_per_minute": round(len(records) / total_elapsed * 60.0, 6) if total_elapsed > 0 else None,
        "stage_timings_s": {
            "discover_scenes": round(discover_elapsed, 6),
            "load_batch_status": round(status_elapsed, 6),
            "analyze_scenes": round(analysis_elapsed, 6),
            "write_outputs": round(write_elapsed, 6),
            "total": round(total_elapsed, 6),
        },
        "scene_elapsed_s": scene_elapsed_summary,
        "outputs": {
            "events": str(logger.events_path),
            "run": str(output_dir / "analysis_run.json"),
            "csv": str(output_dir / "scene_stats.csv"),
            "jsonl": str(output_dir / "scene_stats.jsonl"),
            "summary": str(output_dir / "summary.json"),
            "report": str(output_dir / "report.md"),
        },
    }
    write_json(output_dir / "analysis_run.json", run_payload)
    logger.log(
        "analysis_finished",
        analyzed_scene_count=len(records),
        skipped_scene_count=skipped,
        error_scene_count=errors,
        elapsed_s=round(total_elapsed, 6),
        throughput_scenes_per_minute=run_payload["throughput_scenes_per_minute"],
    )
    print(f"analyzed_scenes={len(records)} skipped_scenes={skipped} output_dir={output_dir}")
    print(f"errors={errors} elapsed_s={total_elapsed:.3f} scenes_per_min={run_payload['throughput_scenes_per_minute']}")
    print(f"csv={output_dir / 'scene_stats.csv'}")
    print(f"summary={output_dir / 'summary.json'}")
    print(f"log={logger.events_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
