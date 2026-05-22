from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .assets import sionna_material_itu_type
from .exporters import sanitize_path_segment
from .geometry import load_obj_mesh, transform_point_with_matrix
from .models import Asset, Box3D, Front3DBaseScene, PlacedAsset, Vec3
from .paths import find_project_root


FRONT3D_TO_SCENEGEN: tuple[float, ...] = (
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    -1.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
)


@dataclass(frozen=True)
class Front3DConfig:
    manifest: Path
    source_scene_dir: Path
    variant: str
    object_variant: str
    scene_ids: tuple[str, ...]
    scene_selection: str
    use_replace_jid: bool
    skip_missing_objects: bool
    normalize_positive_xy: bool
    ground_objects: bool


@dataclass(frozen=True)
class Front3DSceneBuild:
    scene_id: str
    base_scene: Front3DBaseScene
    placements: list[PlacedAsset]
    skipped_objects: list[dict[str, object]]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_repo_path(value: str | Path, repo_root: Path | None = None) -> Path:
    root = repo_root or find_project_root()
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def matrix_multiply(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[float, ...]:
    if len(left) != 16 or len(right) != 16:
        raise ValueError("Matrix multiplication requires two 4x4 row-major matrices.")
    values: list[float] = []
    for row in range(4):
        for col in range(4):
            values.append(sum(left[row * 4 + k] * right[k * 4 + col] for k in range(4)))
    return tuple(values)


def translation_matrix(pos: list[object] | tuple[object, ...] | None) -> tuple[float, ...]:
    values = [0.0, 0.0, 0.0] if pos is None else [float(value) for value in pos[:3]]
    while len(values) < 3:
        values.append(0.0)
    return (
        1.0,
        0.0,
        0.0,
        values[0],
        0.0,
        1.0,
        0.0,
        values[1],
        0.0,
        0.0,
        1.0,
        values[2],
        0.0,
        0.0,
        0.0,
        1.0,
    )


def offset_matrix(offset: Vec3) -> tuple[float, ...]:
    return translation_matrix([offset[0], offset[1], offset[2]])


def scale_matrix(scale: list[object] | tuple[object, ...] | None) -> tuple[float, ...]:
    values = [1.0, 1.0, 1.0] if scale is None else [float(value) for value in scale[:3]]
    while len(values) < 3:
        values.append(1.0)
    return (
        values[0],
        0.0,
        0.0,
        0.0,
        0.0,
        values[1],
        0.0,
        0.0,
        0.0,
        0.0,
        values[2],
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )


def quaternion_matrix(rot: list[object] | tuple[object, ...] | None) -> tuple[float, ...]:
    values = [0.0, 0.0, 0.0, 1.0] if rot is None else [float(value) for value in rot[:4]]
    while len(values) < 4:
        values.append(0.0)
    x, y, z, w = values
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm == 0.0:
        x, y, z, w = 0.0, 0.0, 0.0, 1.0
    else:
        x, y, z, w = x / norm, y / norm, z / norm, w / norm
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return (
        1.0 - 2.0 * (yy + zz),
        2.0 * (xy - wz),
        2.0 * (xz + wy),
        0.0,
        2.0 * (xy + wz),
        1.0 - 2.0 * (xx + zz),
        2.0 * (yz - wx),
        0.0,
        2.0 * (xz - wy),
        2.0 * (yz + wx),
        1.0 - 2.0 * (xx + yy),
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )


def front3d_local_to_world_matrix(child: dict[str, Any]) -> tuple[float, ...]:
    return matrix_multiply(
        translation_matrix(child.get("pos")),
        matrix_multiply(quaternion_matrix(child.get("rot")), scale_matrix(child.get("scale"))),
    )


def scenegen_transform_for_child(child: dict[str, Any], variant: str = "normalized") -> tuple[float, ...]:
    local_to_world = front3d_local_to_world_matrix(child)
    if variant == "raw":
        return local_to_world
    # The phase-1 object OBJ files are copied from 3D-FUTURE and remain Y-up.
    # Applying T * M maps object-local Y-up coordinates into SceneGen's Z-up world.
    return matrix_multiply(FRONT3D_TO_SCENEGEN, local_to_world)


def apply_world_offset(matrix: tuple[float, ...], offset: Vec3) -> tuple[float, ...]:
    return matrix_multiply(offset_matrix(offset), matrix)


def apply_ground_offset(matrix: tuple[float, ...], bbox: Box3D, floor_z: float = 0.0) -> tuple[float, ...]:
    if bbox[4] >= floor_z:
        return matrix
    return matrix_multiply(offset_matrix((0.0, 0.0, floor_z - bbox[4])), matrix)


def material_mapping_from_payload(payload: dict[str, Any]) -> tuple[dict[str, str], tuple[str, ...]]:
    mappings = payload.get("materials", {}).get("source_to_sionna") or []
    source_to_sionna: dict[str, str] = {}
    sionna_names: set[str] = set()
    for item in mappings:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "default")
        material = str(item.get("sionna") or "itu-wood")
        source_to_sionna[source] = material
        source_to_sionna[sanitize_path_segment(source)] = material
        sionna_names.add(material)
    for material in payload.get("materials", {}).get("sionna") or []:
        sionna_names.add(str(material))
    if not sionna_names:
        sionna_names.add("itu-wood")
    return source_to_sionna, tuple(sorted(sionna_names))


def asset_from_object_payload(payload: dict[str, Any], repo_root: Path | None = None) -> Asset | None:
    placement = payload.get("placement") or {}
    if not bool(placement.get("enabled", True)):
        return None
    files = payload.get("files") or {}
    obj_value = files.get("obj")
    if not obj_value:
        return None
    size = (payload.get("geometry") or {}).get("size") or {}
    source_to_sionna, sionna_names = material_mapping_from_payload(payload)
    return Asset(
        name=str(payload.get("name") or payload.get("id")),
        export_name=str(payload.get("id") or payload.get("name")),
        obj_file=resolve_repo_path(str(obj_value), repo_root),
        width=float(size.get("x") or 0.0),
        length=float(size.get("y") or 0.0),
        height=float(size.get("z") or 0.0),
        placement_class=str(placement.get("class") or "floor"),
        source_to_sionna_material=source_to_sionna,
        sionna_material_names=sionna_names,
    )


def base_scene_from_architecture_payload(
    scene_id: str,
    payload: dict[str, Any],
    source_scene_json: Path,
    metadata_json: Path,
    normalize_positive_xy: bool,
    repo_root: Path | None = None,
) -> Front3DBaseScene:
    files = payload.get("files") or {}
    obj_value = files.get("obj")
    if not obj_value:
        raise ValueError(f"3D-FRONT architecture payload is missing files.obj: {scene_id}")
    bbox = (payload.get("geometry") or {}).get("bbox") or {}
    source_bbox_min = tuple(float(value) for value in bbox.get("min", (0.0, 0.0, 0.0)))
    source_bbox_max = tuple(float(value) for value in bbox.get("max", (0.0, 0.0, 0.0)))
    if normalize_positive_xy:
        world_offset = (-source_bbox_min[0], -source_bbox_min[1], 0.0)
    else:
        world_offset = (0.0, 0.0, 0.0)
    bbox_min = (source_bbox_min[0] + world_offset[0], source_bbox_min[1] + world_offset[1], source_bbox_min[2])
    bbox_max = (source_bbox_max[0] + world_offset[0], source_bbox_max[1] + world_offset[1], source_bbox_max[2])
    source_to_sionna, sionna_names = material_mapping_from_payload(payload)
    return Front3DBaseScene(
        scene_id=scene_id,
        scene_obj=resolve_repo_path(str(obj_value), repo_root),
        source_scene_json=source_scene_json,
        metadata_json=metadata_json,
        bbox_min=bbox_min,
        bbox_max=bbox_max,
        source_bbox_min=source_bbox_min,  # type: ignore[arg-type]
        source_bbox_max=source_bbox_max,  # type: ignore[arg-type]
        world_offset=world_offset,
        source_to_sionna_material=source_to_sionna,
        sionna_material_names=sionna_names,
    )


def transformed_bbox(vertices: list[Vec3], matrix: tuple[float, ...]) -> Box3D:
    transformed = [transform_point_with_matrix(vertex, matrix) for vertex in vertices]
    xs = [point[0] for point in transformed]
    ys = [point[1] for point in transformed]
    zs = [point[2] for point in transformed]
    return min(xs), max(xs), min(ys), max(ys), min(zs), max(zs)


def choose_scene_ids(
    available_scene_ids: list[str],
    explicit_scene_ids: tuple[str, ...],
    selection: str,
    count: int,
    rng: random.Random,
) -> list[str]:
    if explicit_scene_ids:
        missing = [scene_id for scene_id in explicit_scene_ids if scene_id not in available_scene_ids]
        if missing:
            raise ValueError(f"Unknown 3D-FRONT scene id(s): {', '.join(missing)}")
        return [explicit_scene_ids[index % len(explicit_scene_ids)] for index in range(count)]
    if not available_scene_ids:
        raise ValueError("3D-FRONT manifest does not contain any scene ids.")
    if selection == "sequential":
        return [available_scene_ids[index % len(available_scene_ids)] for index in range(count)]
    if selection != "random":
        raise ValueError("front3d.scene_selection must be 'random' or 'sequential'")
    return [rng.choice(available_scene_ids) for _ in range(count)]


class Front3DIndex:
    def __init__(self, config: Front3DConfig, repo_root: Path | None = None):
        self.config = config
        self.repo_root = repo_root or find_project_root()
        self.manifest = read_json(config.manifest)
        self.by_scene_id: dict[str, dict[str, Any]] = self.manifest.get("by_scene_id") or {}
        self.by_model_id: dict[str, dict[str, Any]] = self.manifest.get("by_model_id") or {}
        self.object_payload_cache: dict[str, dict[str, Any] | None] = {}
        self.object_asset_cache: dict[str, Asset | None] = {}
        self.mesh_cache: dict[Path, list[Vec3]] = {}

    @property
    def scene_ids(self) -> list[str]:
        return sorted(self.by_scene_id)

    def variant_entry(self, mapping: dict[str, Any], item_id: str, variant: str) -> dict[str, Any]:
        entry = mapping.get(item_id)
        if not isinstance(entry, dict):
            raise KeyError(item_id)
        variant_entry = entry.get(variant)
        if not isinstance(variant_entry, dict):
            raise KeyError(f"{item_id}:{variant}")
        return variant_entry

    def object_payload(self, model_id: str) -> dict[str, Any] | None:
        if model_id in self.object_payload_cache:
            return self.object_payload_cache[model_id]
        try:
            entry = self.variant_entry(self.by_model_id, model_id, self.config.object_variant)
        except KeyError:
            self.object_payload_cache[model_id] = None
            return None
        json_path = resolve_repo_path(str(entry["json"]), self.repo_root)
        payload = read_json(json_path) if json_path.is_file() else None
        self.object_payload_cache[model_id] = payload
        return payload

    def object_asset(self, model_id: str) -> Asset | None:
        if model_id in self.object_asset_cache:
            return self.object_asset_cache[model_id]
        payload = self.object_payload(model_id)
        asset = asset_from_object_payload(payload, self.repo_root) if payload else None
        self.object_asset_cache[model_id] = asset
        return asset

    def object_vertices(self, asset: Asset) -> list[Vec3]:
        vertices = self.mesh_cache.get(asset.obj_file)
        if vertices is None:
            vertices = load_obj_mesh(asset.obj_file).vertices
            self.mesh_cache[asset.obj_file] = vertices
        return vertices

    def base_scene(self, scene_id: str) -> Front3DBaseScene:
        entry = self.variant_entry(self.by_scene_id, scene_id, self.config.variant)
        metadata_json = resolve_repo_path(str(entry["json"]), self.repo_root)
        source_scene_json = self.config.source_scene_dir / f"{scene_id}.json"
        if not source_scene_json.is_file():
            raise FileNotFoundError(f"Missing 3D-FRONT source scene JSON: {source_scene_json}")
        payload = read_json(metadata_json)
        return base_scene_from_architecture_payload(
            scene_id,
            payload,
            source_scene_json,
            metadata_json,
            self.config.normalize_positive_xy,
            self.repo_root,
        )


def furniture_jid_by_ref(scene_data: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in scene_data.get("furniture") or []:
        if not isinstance(item, dict):
            continue
        uid = item.get("uid")
        jid = item.get("jid")
        if uid and jid:
            mapping[str(uid)] = str(jid)
    return mapping


def iter_scene_children(scene_data: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    children: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for room in (scene_data.get("scene") or {}).get("room") or []:
        if not isinstance(room, dict):
            continue
        for child in room.get("children") or []:
            if isinstance(child, dict):
                children.append((room, child))
    return children


def make_front3d_instance_name(scene_index: int, child_index: int, child: dict[str, Any]) -> str:
    raw = child.get("instanceid") or child.get("ref") or f"object_{child_index}"
    return f"front3d_{scene_index:04d}_{child_index:04d}_{sanitize_path_segment(str(raw))}"


def build_scene_from_front3d(
    index: Front3DIndex,
    scene_id: str,
    scene_index: int,
) -> Front3DSceneBuild:
    base_scene = index.base_scene(scene_id)
    scene_data = read_json(base_scene.source_scene_json)
    ref_to_jid = furniture_jid_by_ref(scene_data)
    placements: list[PlacedAsset] = []
    skipped: list[dict[str, object]] = []

    for child_index, (room, child) in enumerate(iter_scene_children(scene_data)):
        ref = str(child.get("ref") or "")
        if ref not in ref_to_jid:
            continue
        original_jid = ref_to_jid.get(ref)
        replace_jid = child.get("replace_jid")
        model_id = str(replace_jid if index.config.use_replace_jid and replace_jid else original_jid or "")
        if not model_id:
            skipped.append({"ref": ref, "reason": "missing_model_id"})
            continue
        asset = index.object_asset(model_id)
        if asset is None or not asset.obj_file.is_file():
            skipped.append({"ref": ref, "model_id": model_id, "reason": "missing_object_asset"})
            if not index.config.skip_missing_objects:
                raise FileNotFoundError(f"Missing 3D-FRONT object asset for model id {model_id}")
            continue
        matrix = apply_world_offset(scenegen_transform_for_child(child, index.config.variant), base_scene.world_offset)
        bbox = transformed_bbox(index.object_vertices(asset), matrix)
        if index.config.ground_objects:
            grounded_matrix = apply_ground_offset(matrix, bbox)
            if grounded_matrix != matrix:
                matrix = grounded_matrix
                bbox = transformed_bbox(index.object_vertices(asset), matrix)
        x, y, z = matrix[3], matrix[7], matrix[11]
        object_payload = index.object_payload(model_id) or {}
        semantic = object_payload.get("semantic") or {}
        placements.append(
            PlacedAsset(
                asset=asset,
                instance_name=make_front3d_instance_name(scene_index, child_index, child),
                x=x,
                y=y,
                z=z,
                yaw=0.0,
                support_type="front3d_scene",
                parent=None,
                min_x=bbox[0],
                max_x=bbox[1],
                min_y=bbox[2],
                max_y=bbox[3],
                min_z=bbox[4],
                max_z=bbox[5],
                transform_matrix_4x4_row_major=matrix,
                source_ids={
                    "scene_id": scene_id,
                    "room_instanceid": str(room.get("instanceid") or ""),
                    "child_ref": ref,
                    "model_id": model_id,
                    "original_jid": original_jid or "",
                    "replace_jid": str(replace_jid or ""),
                },
                metadata={
                    "room_type": room.get("type"),
                    "source_instanceid": child.get("instanceid"),
                    "used_replace_jid": bool(index.config.use_replace_jid and replace_jid),
                    "source_pos": child.get("pos"),
                    "source_rot": child.get("rot"),
                    "source_scale": child.get("scale"),
                    "object_variant": index.config.object_variant,
                    "architecture_variant": index.config.variant,
                    "world_offset": list(base_scene.world_offset),
                    "category": semantic.get("category"),
                    "super_category": semantic.get("super_category"),
                    "material": semantic.get("material"),
                    "itu_materials": [sionna_material_itu_type(material) for material in asset.sionna_material_names],
                },
            )
        )
    return Front3DSceneBuild(scene_id=scene_id, base_scene=base_scene, placements=placements, skipped_objects=skipped)
