from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


Box3D = tuple[float, float, float, float, float, float]
Rect2D = tuple[float, float, float, float]
Vec3 = tuple[float, float, float]


@dataclass(frozen=True)
class Asset:
    name: str
    export_name: str
    obj_file: Path
    width: float
    length: float
    height: float
    placement_class: str
    source_to_sionna_material: dict[str, str]
    sionna_material_names: tuple[str, ...]


@dataclass(frozen=True)
class ObjMesh:
    vertices: list[Vec3]
    faces: list[list[int]]


@dataclass(frozen=True)
class ObjMaterialMesh:
    vertices: list[Vec3]
    faces_by_material: dict[str | None, list[list[int]]]


@dataclass(frozen=True)
class SionnaAssetPart:
    filename: str
    material_name: str
    face_count: int


@dataclass(frozen=True)
class SionnaXmlShape:
    shape_id: str
    filename: str
    material_name: str
    transform: tuple[float, ...] | None = None


@dataclass(frozen=True)
class Room:
    width: float
    length: float
    height: float


@dataclass(frozen=True)
class SupportTriangle:
    vertices: tuple[Vec3, Vec3, Vec3]
    area: float
    z: float


@dataclass(frozen=True)
class StaticObstacle:
    min_x: float
    max_x: float
    min_y: float
    max_y: float
    min_z: float
    max_z: float


@dataclass(frozen=True)
class SupportSurface:
    z: float
    area: float
    triangles: list[SupportTriangle]
    min_x: float
    max_x: float
    min_y: float
    max_y: float


@dataclass(frozen=True)
class BistroBaseScene:
    base_dir: Path
    scene_obj: Path
    label_json: Path | None
    vertex_count: int
    bbox_min: Vec3
    bbox_max: Vec3
    floor_z: float
    floor_triangles: list[SupportTriangle]
    support_surfaces: list[SupportSurface]
    static_obstacles: list[StaticObstacle]
    obstacle_grid: dict[tuple[int, int], list[int]]
    obstacle_cell_size: float


@dataclass(frozen=True)
class Front3DBaseScene:
    scene_id: str
    scene_obj: Path
    source_scene_json: Path
    metadata_json: Path
    bbox_min: Vec3
    bbox_max: Vec3
    source_bbox_min: Vec3
    source_bbox_max: Vec3
    world_offset: Vec3
    source_to_sionna_material: dict[str, str]
    sionna_material_names: tuple[str, ...]


@dataclass
class PlacedAsset:
    asset: Asset
    instance_name: str
    x: float
    y: float
    z: float
    yaw: float
    support_type: str
    parent: str | None
    min_x: float
    max_x: float
    min_y: float
    max_y: float
    min_z: float
    max_z: float
    transform_matrix_4x4_row_major: tuple[float, ...] | None = None
    source_ids: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)
