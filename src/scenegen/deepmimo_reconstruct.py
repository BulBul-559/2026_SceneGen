from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .deepmimo import load_deepmimo_scene, load_mat_single_matrix, write_mat_single_matrix


@dataclass(frozen=True)
class ReconstructionOutput:
    scene_id: str
    source_scene_id: str
    output_dir: Path
    vertices: np.ndarray
    objects: list[dict[str, object]]
    report: dict[str, object]


def write_reconstructed_deepmimo_source(source_dir: Path, output_root: Path) -> ReconstructionOutput:
    source_dir = source_dir.resolve()
    output_root = output_root.resolve()
    if source_dir.name == "i2_28b":
        payload = reconstruct_i2_28b(source_dir)
    elif source_dir.name == "i1_2p5":
        payload = reconstruct_i1_2p5(source_dir)
    else:
        raise ValueError(f"Unsupported DeepMIMO indoor reconstruction scene: {source_dir.name}")

    scene_id = f"{source_dir.name}_reconstructed"
    output_dir = output_root / scene_id
    output_dir.mkdir(parents=True, exist_ok=True)
    write_mat_single_matrix(output_dir / "vertices.mat", "vertices", payload["vertices"].astype(np.float32))
    (output_dir / "objects.json").write_text(
        json.dumps(payload["objects"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    params = {
        "version": "scenegen_reconstructed_deepmimo_indoor_v1",
        "source_scene_id": source_dir.name,
        "source_dir": str(source_dir),
        "scene": {
            "n_vertices": int(payload["vertices"].shape[0]),
            "n_objects": len(payload["objects"]),
            "n_faces": int(sum(len(obj.get("face_vertex_idxs") or []) for obj in payload["objects"])),
        },
        "reconstruction": payload["report"],
    }
    (output_dir / "params.json").write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "reconstruction_report.json").write_text(
        json.dumps(payload["report"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return ReconstructionOutput(
        scene_id=scene_id,
        source_scene_id=source_dir.name,
        output_dir=output_dir,
        vertices=payload["vertices"],
        objects=payload["objects"],
        report=payload["report"],
    )


def reconstruct_i2_28b(source_dir: Path) -> dict[str, object]:
    source_scene = load_deepmimo_scene(source_dir, normalize_positive_xy=False)
    x0, y0, _z0 = source_scene.source_bbox_min
    x1, y1, z1 = source_scene.source_bbox_max
    room_height = min(max(float(z1), 2.8), 3.2)
    vertices: list[tuple[float, float, float]] = []
    objects: list[dict[str, object]] = []
    _append_room_shell(
        vertices,
        objects,
        x0=float(x0),
        x1=float(x1),
        y0=float(y0),
        y1=float(y1),
        height=room_height,
        wall_thickness=0.12,
        floor_thickness=0.04,
    )

    wall, evidence = _infer_i2_blockage(source_dir)
    _append_box(
        vertices,
        objects,
        name="blockage_wall",
        label="blockage",
        min_x=wall["min_x"],
        max_x=wall["max_x"],
        min_y=wall["min_y"],
        max_y=wall["max_y"],
        min_z=0.0,
        max_z=wall["height_m"],
    )
    report = {
        "method": "manual_primitives_with_inter_pos_check",
        "scene_id": "i2_28b",
        "room_bbox_source_m": _bbox_dict(float(x0), float(x1), float(y0), float(y1), 0.0, room_height),
        "primitives": {
            "outer_shell": {"type": "four_walls_and_floor", "height_m": room_height, "wall_thickness_m": 0.12},
            "blockage_wall": wall,
        },
        "evidence": {
            "blockage_wall": evidence,
            "note": "The blockage wall is inferred from object-id 2 interaction points in inter/inter_pos.",
        },
    }
    return {"vertices": np.asarray(vertices, dtype=np.float32), "objects": objects, "report": report}


def reconstruct_i1_2p5(source_dir: Path) -> dict[str, object]:
    source_scene = load_deepmimo_scene(source_dir, normalize_positive_xy=False)
    x0, y0, _z0 = source_scene.source_bbox_min
    x1, y1, z1 = source_scene.source_bbox_max
    room_height = min(max(float(z1), 2.8), 3.2)
    vertices: list[tuple[float, float, float]] = []
    objects: list[dict[str, object]] = []
    _append_room_shell(
        vertices,
        objects,
        x0=float(x0),
        x1=float(x1),
        y0=float(y0),
        y1=float(y1),
        height=room_height,
        wall_thickness=0.12,
        floor_thickness=0.04,
    )

    tables = [
        {
            "name": "table_left",
            "bbox_xy_m": {"min_x": -22.45, "max_x": -20.75, "min_y": 11.85, "max_y": 15.50},
        },
        {
            "name": "table_right",
            "bbox_xy_m": {"min_x": -19.15, "max_x": -18.05, "min_y": 12.20, "max_y": 15.50},
        },
    ]
    for table in tables:
        bbox = table["bbox_xy_m"]
        _append_table(
            vertices,
            objects,
            name=str(table["name"]),
            min_x=float(bbox["min_x"]),
            max_x=float(bbox["max_x"]),
            min_y=float(bbox["min_y"]),
            max_y=float(bbox["max_y"]),
            top_min_z=1.45,
            top_max_z=1.75,
            leg_width=0.12,
        )

    inter_points = _load_first_inter_pos_points(source_dir)
    table_evidence = {
        table["name"]: _point_coverage_for_xy_box(inter_points, table["bbox_xy_m"], margin_m=0.12)
        for table in tables
    }
    report = {
        "method": "manual_primitives_with_inter_pos_check",
        "scene_id": "i1_2p5",
        "room_bbox_source_m": _bbox_dict(float(x0), float(x1), float(y0), float(y1), 0.0, room_height),
        "primitives": {
            "outer_shell": {"type": "four_walls_and_floor", "height_m": room_height, "wall_thickness_m": 0.12},
            "tables": tables,
        },
        "evidence": {
            "tables": table_evidence,
            "note": "Table footprints are scene-specific defaults checked against inter_pos XY hits.",
        },
    }
    return {"vertices": np.asarray(vertices, dtype=np.float32), "objects": objects, "report": report}


def _append_room_shell(
    vertices: list[tuple[float, float, float]],
    objects: list[dict[str, object]],
    *,
    x0: float,
    x1: float,
    y0: float,
    y1: float,
    height: float,
    wall_thickness: float,
    floor_thickness: float,
) -> None:
    t = float(wall_thickness)
    _append_box(vertices, objects, name="floor", label="floor", min_x=x0, max_x=x1, min_y=y0, max_y=y1, min_z=-floor_thickness, max_z=0.0)
    _append_box(vertices, objects, name="wall_west", label="wall", min_x=x0, max_x=x0 + t, min_y=y0, max_y=y1, min_z=0.0, max_z=height)
    _append_box(vertices, objects, name="wall_east", label="wall", min_x=x1 - t, max_x=x1, min_y=y0, max_y=y1, min_z=0.0, max_z=height)
    _append_box(vertices, objects, name="wall_south", label="wall", min_x=x0, max_x=x1, min_y=y0, max_y=y0 + t, min_z=0.0, max_z=height)
    _append_box(vertices, objects, name="wall_north", label="wall", min_x=x0, max_x=x1, min_y=y1 - t, max_y=y1, min_z=0.0, max_z=height)


def _append_table(
    vertices: list[tuple[float, float, float]],
    objects: list[dict[str, object]],
    *,
    name: str,
    min_x: float,
    max_x: float,
    min_y: float,
    max_y: float,
    top_min_z: float,
    top_max_z: float,
    leg_width: float,
) -> None:
    _append_box(
        vertices,
        objects,
        name=f"{name}_top",
        label="furniture",
        min_x=min_x,
        max_x=max_x,
        min_y=min_y,
        max_y=max_y,
        min_z=top_min_z,
        max_z=top_max_z,
    )
    for suffix, leg_x0, leg_y0 in (
        ("sw", min_x, min_y),
        ("se", max_x - leg_width, min_y),
        ("nw", min_x, max_y - leg_width),
        ("ne", max_x - leg_width, max_y - leg_width),
    ):
        _append_box(
            vertices,
            objects,
            name=f"{name}_leg_{suffix}",
            label="furniture",
            min_x=leg_x0,
            max_x=leg_x0 + leg_width,
            min_y=leg_y0,
            max_y=leg_y0 + leg_width,
            min_z=0.0,
            max_z=top_min_z,
        )


def _append_box(
    vertices: list[tuple[float, float, float]],
    objects: list[dict[str, object]],
    *,
    name: str,
    label: str,
    min_x: float,
    max_x: float,
    min_y: float,
    max_y: float,
    min_z: float,
    max_z: float,
) -> None:
    if max_x <= min_x or max_y <= min_y or max_z <= min_z:
        raise ValueError(f"Invalid box primitive {name!r}")
    start = len(vertices)
    vertices.extend(
        [
            (min_x, min_y, min_z),
            (max_x, min_y, min_z),
            (max_x, max_y, min_z),
            (min_x, max_y, min_z),
            (min_x, min_y, max_z),
            (max_x, min_y, max_z),
            (max_x, max_y, max_z),
            (min_x, max_y, max_z),
        ]
    )
    faces = [
        [start + 0, start + 1, start + 2, start + 3],
        [start + 4, start + 7, start + 6, start + 5],
        [start + 0, start + 4, start + 5, start + 1],
        [start + 1, start + 5, start + 6, start + 2],
        [start + 2, start + 6, start + 7, start + 3],
        [start + 3, start + 7, start + 4, start + 0],
    ]
    objects.append(
        {
            "name": name,
            "label": label,
            "id": len(objects),
            "face_vertex_idxs": faces,
            "face_material_idxs": [0] * len(faces),
        }
    )


def _infer_i2_blockage(source_dir: Path) -> tuple[dict[str, float], dict[str, object]]:
    points = _load_inter_object_points(
        inter_path=source_dir / "inter_t001_tx000_r000.mat",
        inter_pos_path=source_dir / "inter_pos_t001_tx000_r000.mat",
        object_id=2,
    )
    if points.size == 0:
        wall = {"min_x": -3.0, "max_x": 0.1, "min_y": -0.55, "max_y": -0.15, "height_m": 2.5}
        return wall, {"ok": False, "reason": "no_object_2_interaction_points", "fallback": wall}

    y_values = points[:, 1]
    bin_width = 0.05
    y_min = float(np.min(y_values))
    bins = np.floor((y_values - y_min) / bin_width).astype(np.int32)
    dominant_bin = int(np.bincount(bins).argmax())
    dominant_y = y_min + (dominant_bin + 0.5) * bin_width
    near = points[np.abs(points[:, 1] - dominant_y) <= 0.35]
    if near.shape[0] < 100:
        near = points
    min_x = float(np.quantile(near[:, 0], 0.05))
    max_x = float(np.quantile(near[:, 0], 0.95))
    min_y = float(np.quantile(near[:, 1], 0.05))
    max_y = float(np.quantile(near[:, 1], 0.95))
    if max_y - min_y < 0.12:
        center_y = (min_y + max_y) / 2.0
        min_y = center_y - 0.08
        max_y = center_y + 0.08
    height = max(2.0, min(float(np.quantile(near[:, 2], 0.99)), 2.8))
    wall = {"min_x": min_x, "max_x": max_x, "min_y": min_y, "max_y": max_y, "height_m": height}
    evidence = {
        "ok": True,
        "inter_file": "inter_t001_tx000_r000.mat",
        "inter_pos_file": "inter_pos_t001_tx000_r000.mat",
        "object_id": 2,
        "object_point_count": int(points.shape[0]),
        "dominant_y_m": round(dominant_y, 6),
        "near_point_count": int(near.shape[0]),
        "near_bbox_m": _point_bbox(near),
        "wall_bbox_m": wall,
    }
    return wall, evidence


def _load_inter_object_points(inter_path: Path, inter_pos_path: Path, object_id: int) -> np.ndarray:
    inter = load_mat_single_matrix(inter_path, "inter").reshape(-1)
    inter_pos = load_mat_single_matrix(inter_pos_path, "inter_pos")
    positions = inter_pos.reshape((-1, inter_pos.shape[-2], 3))
    valid = np.isfinite(inter) & (inter > 0)
    codes = inter[valid].astype(np.int64)
    positions = positions[valid]
    if codes.size == 0:
        return np.zeros((0, 3), dtype=np.float32)
    lengths = np.floor(np.log10(codes)).astype(np.int64) + 1
    collected: list[np.ndarray] = []
    for bounce in range(positions.shape[1]):
        has_bounce = lengths > bounce
        if not np.any(has_bounce):
            continue
        powers = 10 ** (lengths[has_bounce] - bounce - 1)
        digits = (codes[has_bounce] // powers) % 10
        pts = positions[has_bounce, bounce, :]
        mask = (
            (digits == int(object_id))
            & np.isfinite(pts).all(axis=1)
            & (np.linalg.norm(pts, axis=1) > 1e-6)
        )
        if np.any(mask):
            collected.append(pts[mask].astype(np.float32, copy=False))
    if not collected:
        return np.zeros((0, 3), dtype=np.float32)
    return np.concatenate(collected, axis=0)


def _load_first_inter_pos_points(source_dir: Path) -> np.ndarray:
    candidates = sorted(source_dir.glob("inter_pos_*_r000.mat"))
    if not candidates:
        candidates = sorted(source_dir.glob("inter_pos_*.mat"))
    if not candidates:
        return np.zeros((0, 3), dtype=np.float32)
    points = load_mat_single_matrix(candidates[0], "inter_pos").reshape((-1, 3)).astype(np.float32, copy=False)
    return points[np.isfinite(points).all(axis=1) & (np.linalg.norm(points, axis=1) > 1e-6)]


def _point_coverage_for_xy_box(
    points: np.ndarray,
    bbox_xy: dict[str, float],
    *,
    margin_m: float,
) -> dict[str, object]:
    if points.size == 0:
        return {"ok": False, "reason": "no_inter_pos_points"}
    min_x = float(bbox_xy["min_x"]) - margin_m
    max_x = float(bbox_xy["max_x"]) + margin_m
    min_y = float(bbox_xy["min_y"]) - margin_m
    max_y = float(bbox_xy["max_y"]) + margin_m
    mask = (points[:, 0] >= min_x) & (points[:, 0] <= max_x) & (points[:, 1] >= min_y) & (points[:, 1] <= max_y)
    selected = points[mask]
    return {
        "ok": bool(selected.shape[0] > 0),
        "point_count": int(selected.shape[0]),
        "expanded_bbox_xy_m": {"min_x": min_x, "max_x": max_x, "min_y": min_y, "max_y": max_y},
        "point_bbox_m": _point_bbox(selected) if selected.shape[0] else None,
    }


def _point_bbox(points: np.ndarray) -> dict[str, list[float]]:
    if points.size == 0:
        return {"min": [], "max": []}
    return {
        "min": [round(float(value), 6) for value in np.min(points, axis=0).tolist()],
        "max": [round(float(value), 6) for value in np.max(points, axis=0).tolist()],
    }


def _bbox_dict(min_x: float, max_x: float, min_y: float, max_y: float, min_z: float, max_z: float) -> dict[str, float]:
    return {
        "min_x": float(min_x),
        "max_x": float(max_x),
        "min_y": float(min_y),
        "max_y": float(max_y),
        "min_z": float(min_z),
        "max_z": float(max_z),
    }
