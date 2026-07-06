from __future__ import annotations

import json
import struct
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .assets import normalize_sionna_material_name, sionna_material_itu_type
from .exporters import (
    build_sionna_scene_xml,
    obj_face_lines,
    obj_vertex_lines,
    relative_posix,
    sanitize_path_segment,
    write_clean_obj_subset,
)
from .models import SceneMeshArrays, SionnaAssetPart, SionnaXmlShape, Vec3
from .paths import portable_path


DEEPMIMO_REQUIRED_FILES = ("vertices.mat", "objects.json", "params.json")


@dataclass(frozen=True)
class DeepMIMOConfig:
    root: Path
    scene_ids: tuple[str, ...] = ()
    scene_selection: str = "sequential"
    start_index: int = 0
    normalize_positive_xy: bool = True
    material_by_label: dict[str, str] | None = None
    default_material: str = "itu-concrete"


@dataclass(frozen=True)
class DeepMIMOObject:
    name: str
    label: str
    object_id: int
    faces: tuple[tuple[int, ...], ...]
    material_indices: tuple[int, ...]


@dataclass(frozen=True)
class DeepMIMOScene:
    scene_id: str
    scene_dir: Path
    source_vertices: np.ndarray
    vertices: np.ndarray
    objects: tuple[DeepMIMOObject, ...]
    params: dict[str, Any]
    world_offset: Vec3
    bbox_min: Vec3
    bbox_max: Vec3
    source_bbox_min: Vec3
    source_bbox_max: Vec3


@dataclass(frozen=True)
class DeepMIMOBuildOutput:
    record: dict[str, object]
    floorplan_mesh_arrays: SceneMeshArrays | None
    bounds_xy: tuple[float, float, float, float]


def _padded_size(size: int) -> int:
    return size + ((8 - size % 8) % 8)


def _read_tag(data: bytes, offset: int, endian: str) -> tuple[int, int, int, int]:
    if offset + 8 > len(data):
        raise ValueError("MAT-file tag is truncated")
    raw0, raw1 = struct.unpack_from(f"{endian}II", data, offset)
    small_size = raw0 >> 16
    if small_size:
        return raw0 & 0xFFFF, small_size, offset + 4, offset + 8
    payload_offset = offset + 8
    return raw0, raw1, payload_offset, payload_offset + _padded_size(raw1)


def _numeric_array_from_payload(data_type: int, payload: bytes, endian: str) -> np.ndarray:
    dtype_by_type = {
        5: np.dtype(f"{endian}i4"),
        6: np.dtype(f"{endian}u4"),
        7: np.dtype(f"{endian}f4"),
        9: np.dtype(f"{endian}f8"),
    }
    dtype = dtype_by_type.get(data_type)
    if dtype is None:
        raise ValueError(f"Unsupported MATLAB numeric data type: {data_type}")
    return np.frombuffer(payload, dtype=dtype).copy()


def load_mat_single_matrix(path: Path, variable_name: str) -> np.ndarray:
    """Read a numeric matrix from an uncompressed MATLAB v5 MAT-file."""

    data = path.read_bytes()
    if len(data) < 128:
        raise ValueError(f"MAT-file is too small: {path}")
    endian_marker = data[126:128]
    if endian_marker == b"IM":
        endian = "<"
    elif endian_marker == b"MI":
        endian = ">"
    else:
        raise ValueError(f"Unsupported MATLAB endian marker {endian_marker!r} in {path}")

    offset = 128
    while offset < len(data):
        data_type, nbytes, payload_offset, next_offset = _read_tag(data, offset, endian)
        payload = data[payload_offset : payload_offset + nbytes]
        if data_type == 15:
            raise ValueError(f"Compressed MATLAB elements are not supported in {path}")
        if data_type != 14:
            offset = next_offset
            continue

        inner = 0
        _flags_type, _flags_nbytes, _flags_offset, inner_next = _read_tag(payload, inner, endian)
        inner = inner_next
        dims_type, dims_nbytes, dims_offset, inner_next = _read_tag(payload, inner, endian)
        dims_raw = _numeric_array_from_payload(dims_type, payload[dims_offset : dims_offset + dims_nbytes], endian)
        dims = tuple(int(value) for value in dims_raw.tolist())
        inner = inner_next
        name_type, name_nbytes, name_offset, inner_next = _read_tag(payload, inner, endian)
        name_payload = payload[name_offset : name_offset + name_nbytes]
        if name_type not in {1, 2}:
            raise ValueError(f"Unsupported MATLAB variable name type {name_type} in {path}")
        name = name_payload.decode("ascii", errors="ignore")
        inner = inner_next
        if name != variable_name:
            offset = next_offset
            continue

        values_type, values_nbytes, values_offset, _values_next = _read_tag(payload, inner, endian)
        values = _numeric_array_from_payload(
            values_type,
            payload[values_offset : values_offset + values_nbytes],
            endian,
        )
        if not dims:
            return values
        expected = int(np.prod(dims))
        if values.size != expected:
            raise ValueError(
                f"MATLAB variable {variable_name!r} in {path} has shape {dims} but {values.size} values"
            )
        return values.reshape(dims, order="F")

        offset = next_offset

    raise KeyError(f"Variable {variable_name!r} not found in {path}")


def load_deepmimo_vertices(path: Path) -> np.ndarray:
    vertices = np.asarray(load_mat_single_matrix(path, "vertices"), dtype=np.float32)
    if vertices.ndim != 2 or vertices.shape[1] != 3:
        raise ValueError(f"DeepMIMO vertices must have shape [N, 3], got {vertices.shape} in {path}")
    if not np.isfinite(vertices).all():
        raise ValueError(f"DeepMIMO vertices contain NaN or Inf values: {path}")
    return vertices


def _parse_faces(raw_faces: Any, scene_dir: Path, object_name: str) -> tuple[tuple[int, ...], ...]:
    faces: list[tuple[int, ...]] = []
    if not isinstance(raw_faces, list):
        raise ValueError(f"DeepMIMO object {object_name!r} in {scene_dir} has non-list face_vertex_idxs")
    for face in raw_faces:
        if not isinstance(face, list):
            raise ValueError(f"DeepMIMO object {object_name!r} in {scene_dir} has a non-list face")
        faces.append(tuple(int(index) for index in face))
    return tuple(faces)


def load_deepmimo_objects(path: Path) -> tuple[DeepMIMOObject, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"DeepMIMO objects file must be a JSON list: {path}")
    objects: list[DeepMIMOObject] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError(f"DeepMIMO objects file contains a non-object entry: {path}")
        name = str(item.get("name") or f"object_{len(objects):04d}")
        faces = _parse_faces(item.get("face_vertex_idxs"), path.parent, name)
        raw_materials = item.get("face_material_idxs") or []
        if not isinstance(raw_materials, list):
            raise ValueError(f"DeepMIMO object {name!r} has non-list face_material_idxs in {path}")
        material_indices = tuple(int(value) for value in raw_materials)
        objects.append(
            DeepMIMOObject(
                name=name,
                label=str(item.get("label") or "object"),
                object_id=int(item.get("id", len(objects))),
                faces=faces,
                material_indices=material_indices,
            )
        )
    return tuple(objects)


def discover_deepmimo_scene_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"DeepMIMO root directory does not exist: {root}")
    scene_dirs = [
        path
        for path in sorted(root.iterdir(), key=lambda item: item.name)
        if path.is_dir() and all((path / filename).is_file() for filename in DEEPMIMO_REQUIRED_FILES)
    ]
    return scene_dirs


def _bbox_tuple(vertices: np.ndarray, reducer: Any) -> Vec3:
    values = reducer(vertices, axis=0)
    return float(values[0]), float(values[1]), float(values[2])


def load_deepmimo_scene(scene_dir: Path, normalize_positive_xy: bool = True) -> DeepMIMOScene:
    scene_dir = scene_dir.resolve()
    vertices = load_deepmimo_vertices(scene_dir / "vertices.mat")
    objects = load_deepmimo_objects(scene_dir / "objects.json")
    params = json.loads((scene_dir / "params.json").read_text(encoding="utf-8"))
    source_bbox_min = _bbox_tuple(vertices, np.min)
    source_bbox_max = _bbox_tuple(vertices, np.max)
    if normalize_positive_xy:
        offset = (-source_bbox_min[0], -source_bbox_min[1], 0.0)
    else:
        offset = (0.0, 0.0, 0.0)
    local_vertices = vertices.copy()
    local_vertices[:, 0] += np.float32(offset[0])
    local_vertices[:, 1] += np.float32(offset[1])
    local_vertices[:, 2] += np.float32(offset[2])
    bbox_min = _bbox_tuple(local_vertices, np.min)
    bbox_max = _bbox_tuple(local_vertices, np.max)
    return DeepMIMOScene(
        scene_id=scene_dir.name,
        scene_dir=scene_dir,
        source_vertices=vertices,
        vertices=local_vertices,
        objects=objects,
        params=params,
        world_offset=offset,
        bbox_min=bbox_min,
        bbox_max=bbox_max,
        source_bbox_min=source_bbox_min,
        source_bbox_max=source_bbox_max,
    )


def material_map_for_config(config: DeepMIMOConfig) -> dict[str, str]:
    configured = config.material_by_label or {}
    normalized = {
        str(label): normalize_sionna_material_name(material)
        for label, material in configured.items()
        if str(label).strip()
    }
    normalized.setdefault("buildings", "itu-concrete")
    normalized.setdefault("building", "itu-concrete")
    normalized.setdefault("terrain", "itu-concrete")
    normalized["default"] = normalize_sionna_material_name(config.default_material)
    return normalized


def _material_for_object(obj: DeepMIMOObject, material_map: dict[str, str]) -> str:
    return material_map.get(obj.label, material_map["default"])


def _valid_faces_for_object(obj: DeepMIMOObject, vertex_count: int) -> list[list[int]]:
    faces: list[list[int]] = []
    for face in obj.faces:
        if len(face) < 3:
            continue
        indices = [int(index) for index in face]
        if any(index < 0 or index >= vertex_count for index in indices):
            raise ValueError(f"DeepMIMO object {obj.name!r} has a face index outside the vertex array")
        faces.append(indices)
    return faces


def triangulate_faces(faces: list[list[int]]) -> np.ndarray:
    triangles: list[tuple[int, int, int]] = []
    for face in faces:
        first = int(face[0])
        for index in range(1, len(face) - 1):
            triangles.append((first, int(face[index]), int(face[index + 1])))
    return np.asarray(triangles, dtype=np.int32).reshape((-1, 3))


def all_valid_faces(objects: tuple[DeepMIMOObject, ...], vertex_count: int) -> list[list[int]]:
    faces: list[list[int]] = []
    for obj in objects:
        faces.extend(_valid_faces_for_object(obj, vertex_count))
    return faces


def write_deepmimo_scene_obj(path: Path, scene: DeepMIMOScene, material_map: dict[str, str]) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    vertex_list = [tuple(float(value) for value in row) for row in scene.vertices.tolist()]
    labels = Counter(obj.label for obj in scene.objects)
    face_count = 0
    skipped_edge_count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("# Generated by SceneGen from DeepMIMO geometry\n")
        handle.write("mtllib scene.mtl\n")
        handle.write(f"o deepmimo_{sanitize_path_segment(scene.scene_id)}\n")
        handle.writelines(obj_vertex_lines(vertex_list))
        for obj in scene.objects:
            faces = _valid_faces_for_object(obj, len(vertex_list))
            skipped_edge_count += len(obj.faces) - len(faces)
            if not faces:
                continue
            material_name = _material_for_object(obj, material_map)
            handle.write(f"g {sanitize_path_segment(obj.label)}_{sanitize_path_segment(obj.name)}\n")
            handle.write(f"usemtl {sanitize_path_segment(material_name)}\n")
            handle.writelines(obj_face_lines(faces))
            face_count += len(faces)

    materials = sorted({sanitize_path_segment(_material_for_object(obj, material_map)) for obj in scene.objects})
    with (path.parent / "scene.mtl").open("w", encoding="utf-8", newline="\n") as handle:
        for name in materials:
            handle.write(f"newmtl {name}\n")
            handle.write("Kd 0.7 0.7 0.7\n")
            handle.write("Ka 0.1 0.1 0.1\n")
            handle.write("Ks 0.0 0.0 0.0\n")
    return {
        "vertex_count": int(scene.vertices.shape[0]),
        "object_count": len(scene.objects),
        "face_count": face_count,
        "skipped_edge_face_count": skipped_edge_count,
        "label_counts": dict(sorted(labels.items())),
    }


def write_deepmimo_sionna_assets(
    output_dir: Path,
    scene: DeepMIMOScene,
    material_map: dict[str, str],
    write_sionna_assets: bool,
) -> dict[str, object]:
    if not write_sionna_assets:
        return {
            "shape_count": 0,
            "materials": [],
            "asset_parts": [],
            "skipped": True,
            "reason": "output.write_sionna_assets=false",
        }

    assets_dir = output_dir / "assets" / "deepmimo"
    vertex_list = [tuple(float(value) for value in row) for row in scene.vertices.tolist()]
    faces_by_label_material: dict[tuple[str, str], list[list[int]]] = {}
    for obj in scene.objects:
        material_name = _material_for_object(obj, material_map)
        faces = _valid_faces_for_object(obj, len(vertex_list))
        if faces:
            faces_by_label_material.setdefault((obj.label, material_name), []).extend(faces)

    shapes: list[SionnaXmlShape] = []
    parts: list[SionnaAssetPart] = []
    for index, ((label, material_name), faces) in enumerate(sorted(faces_by_label_material.items())):
        label_name = sanitize_path_segment(label)
        material_suffix = sanitize_path_segment(sionna_material_itu_type(material_name))
        path = assets_dir / f"{label_name}__{material_suffix}.obj"
        object_name = f"deepmimo_{scene.scene_id}_{label_name}_{material_suffix}"
        write_clean_obj_subset(path, object_name, vertex_list, faces)
        part = SionnaAssetPart(
            filename=relative_posix(path, output_dir),
            material_name=material_name,
            face_count=len(faces),
        )
        parts.append(part)
        shapes.append(
            SionnaXmlShape(
                shape_id=f"deepmimo_{index:04d}_{label_name}_{material_suffix}",
                filename=part.filename,
                material_name=material_name,
            )
        )

    (output_dir / "scene.xml").write_text(build_sionna_scene_xml(shapes), encoding="utf-8")
    return {
        "assets_dir": portable_path(output_dir / "assets", output_dir),
        "scene_xml": portable_path(output_dir / "scene.xml", output_dir),
        "shape_count": len(shapes),
        "asset_file_count": len(parts),
        "materials": sorted({shape.material_name for shape in shapes}),
        "base_scene_parts": [
            {
                "asset_obj": part.filename,
                "sionna_material_name": part.material_name,
                "itu_type": sionna_material_itu_type(part.material_name),
                "face_count": part.face_count,
            }
            for part in parts
        ],
    }


def deepmimo_scene_summary(scene: DeepMIMOScene, obj_stats: dict[str, object]) -> dict[str, object]:
    bbox_size = [float(scene.bbox_max[index] - scene.bbox_min[index]) for index in range(3)]
    source_bbox_size = [
        float(scene.source_bbox_max[index] - scene.source_bbox_min[index])
        for index in range(3)
    ]
    params_scene = scene.params.get("scene") if isinstance(scene.params.get("scene"), dict) else {}
    return {
        "scene_id": scene.scene_id,
        "source_dir": portable_path(scene.scene_dir, scene.scene_dir.parent),
        "vertices_mat": "vertices.mat",
        "objects_json": "objects.json",
        "params_json": "params.json",
        "object_count": int(obj_stats["object_count"]),
        "vertex_count": int(obj_stats["vertex_count"]),
        "face_count": int(obj_stats["face_count"]),
        "skipped_edge_face_count": int(obj_stats["skipped_edge_face_count"]),
        "label_counts": obj_stats["label_counts"],
        "bbox_min": [round(value, 6) for value in scene.bbox_min],
        "bbox_max": [round(value, 6) for value in scene.bbox_max],
        "bbox_size_m": [round(value, 6) for value in bbox_size],
        "xy_area_m2": round(max(0.0, bbox_size[0]) * max(0.0, bbox_size[1]), 6),
        "source_bbox_min": [round(value, 6) for value in scene.source_bbox_min],
        "source_bbox_max": [round(value, 6) for value in scene.source_bbox_max],
        "source_bbox_size_m": [round(value, 6) for value in source_bbox_size],
        "world_offset": [round(value, 6) for value in scene.world_offset],
        "params_scene": params_scene,
    }


def write_deepmimo_source_metadata(
    output_dir: Path,
    scene: DeepMIMOScene,
    material_map: dict[str, str],
) -> str:
    source_dir = output_dir / "deepmimo_source"
    source_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = source_dir / "metadata.json"
    payload = {
        "scene_id": scene.scene_id,
        "source_dir": str(scene.scene_dir),
        "source_files": {filename: str(scene.scene_dir / filename) for filename in DEEPMIMO_REQUIRED_FILES},
        "material_by_label": dict(sorted(material_map.items())),
        "world_offset": [float(value) for value in scene.world_offset],
    }
    metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return portable_path(metadata_path, output_dir.parent)


def write_deepmimo_scene_files(
    output_dir: Path,
    scene: DeepMIMOScene,
    scene_index: int,
    seed: int,
    material_map: dict[str, str],
    collect_floorplan_mesh_arrays: bool = True,
    write_scene_obj: bool = True,
    write_sionna_assets: bool = True,
) -> DeepMIMOBuildOutput:
    output_dir.mkdir(parents=True, exist_ok=True)
    timings_s: dict[str, float] = {}
    faces = all_valid_faces(scene.objects, int(scene.vertices.shape[0]))
    started = time.perf_counter()
    if write_scene_obj:
        obj_stats = write_deepmimo_scene_obj(output_dir / "scene.obj", scene, material_map)
    else:
        source_face_count = sum(len(obj.faces) for obj in scene.objects)
        obj_stats = {
            "vertex_count": int(scene.vertices.shape[0]),
            "object_count": len(scene.objects),
            "face_count": len(faces),
            "skipped_edge_face_count": source_face_count - len(faces),
            "label_counts": dict(sorted(Counter(obj.label for obj in scene.objects).items())),
        }
    timings_s["write_scene_obj"] = round(time.perf_counter() - started, 6)
    started = time.perf_counter()
    sionna_assets = write_deepmimo_sionna_assets(output_dir, scene, material_map, write_sionna_assets)
    timings_s["write_sionna_assets"] = round(time.perf_counter() - started, 6)
    source_metadata = write_deepmimo_source_metadata(output_dir, scene, material_map)
    triangles = triangulate_faces(faces)
    floorplan_mesh_arrays = (
        SceneMeshArrays(vertices=scene.vertices.astype(np.float32, copy=True), faces=triangles, source="deepmimo")
        if collect_floorplan_mesh_arrays
        else None
    )
    deepmimo_summary = deepmimo_scene_summary(scene, obj_stats)
    placement_payload = {
        "scene_index": scene_index,
        "seed": seed,
        "mode": "deepmimo",
        "deepmimo_scene_id": scene.scene_id,
        "deepmimo": deepmimo_summary,
        "source_metadata": source_metadata,
        "sionna_assets": sionna_assets,
        "placement_count": 0,
        "placements": [],
    }
    (output_dir / "placements.json").write_text(json.dumps(placement_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    record: dict[str, object] = {
        "scene_index": scene_index,
        "seed": seed,
        "mode": "deepmimo",
        "scene_dir": portable_path(output_dir, output_dir.parent),
        "scene_obj": portable_path(output_dir / "scene.obj", output_dir.parent) if write_scene_obj else None,
        "scene_xml": portable_path(output_dir / "scene.xml", output_dir.parent) if write_sionna_assets else None,
        "assets_dir": portable_path(output_dir / "assets", output_dir.parent) if write_sionna_assets else None,
        "sionna_shape_count": sionna_assets["shape_count"],
        "sionna_materials": sionna_assets["materials"],
        "placement_count": 0,
        "deepmimo_scene_id": scene.scene_id,
        "deepmimo": deepmimo_summary,
        "source_scene": portable_path(scene.scene_dir, output_dir.parent),
        "source_metadata": source_metadata,
        "skipped_object_count": 0,
        "skipped_objects": [],
        "build_timings_s": timings_s,
    }
    if not write_sionna_assets:
        record["sionna_assets_skipped"] = True
    bounds_xy = (
        float(scene.bbox_min[0]),
        float(scene.bbox_min[1]),
        float(scene.bbox_max[0]),
        float(scene.bbox_max[1]),
    )
    return DeepMIMOBuildOutput(record=record, floorplan_mesh_arrays=floorplan_mesh_arrays, bounds_xy=bounds_xy)


class DeepMIMOIndex:
    def __init__(self, config: DeepMIMOConfig):
        self.config = config
        discovered = {path.name: path for path in discover_deepmimo_scene_dirs(config.root)}
        if config.scene_ids:
            missing = [scene_id for scene_id in config.scene_ids if scene_id not in discovered]
            if missing:
                raise FileNotFoundError(
                    "DeepMIMO scene ids are missing or incomplete under "
                    f"{config.root}: {', '.join(missing)}"
                )
            self.scene_ids = list(config.scene_ids)
        else:
            self.scene_ids = sorted(discovered)
        if not self.scene_ids:
            required = ", ".join(DEEPMIMO_REQUIRED_FILES)
            raise FileNotFoundError(f"No DeepMIMO scene directories with {required} found under {config.root}")
        self.scene_dirs = {scene_id: discovered[scene_id] for scene_id in self.scene_ids}

    def scene_dir(self, scene_id: str) -> Path:
        return self.scene_dirs[scene_id]

    def load_scene(self, scene_id: str) -> DeepMIMOScene:
        return load_deepmimo_scene(self.scene_dir(scene_id), self.config.normalize_positive_xy)
