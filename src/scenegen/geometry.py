from __future__ import annotations

import math
import random
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

from .models import BistroBaseScene, Box3D, ObjMaterialMesh, ObjMesh, PlacedAsset, Room, StaticObstacle
from .models import SupportSurface, SupportTriangle, Vec3


OBJ_MESH_CACHE_SIZE = 64


def parse_face_indices(parts: Iterable[str], vertex_count: int) -> list[int]:
    indices: list[int] = []
    for part in parts:
        token = part.split("/")[0]
        if not token:
            continue
        index = int(token)
        if index < 0:
            index = vertex_count + index + 1
        indices.append(index - 1)
    return indices


def vector_sub(a: Vec3, b: Vec3) -> Vec3:
    return a[0] - b[0], a[1] - b[1], a[2] - b[2]


def cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def vector_length(value: Vec3) -> float:
    return math.sqrt(value[0] * value[0] + value[1] * value[1] + value[2] * value[2])


def triangle_area_and_up_normal(a: Vec3, b: Vec3, c: Vec3) -> tuple[float, float]:
    normal = cross(vector_sub(b, a), vector_sub(c, a))
    normal_len = vector_length(normal)
    if normal_len == 0.0:
        return 0.0, 0.0
    return 0.5 * normal_len, normal[2] / normal_len


def triangle_bbox(vertices: tuple[Vec3, Vec3, Vec3]) -> StaticObstacle:
    xs = [vertex[0] for vertex in vertices]
    ys = [vertex[1] for vertex in vertices]
    zs = [vertex[2] for vertex in vertices]
    return StaticObstacle(min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))


def load_obj_mesh(path: Path) -> ObjMesh:
    return _load_obj_mesh_cached(str(path.expanduser().resolve()))


@lru_cache(maxsize=OBJ_MESH_CACHE_SIZE)
def _load_obj_mesh_cached(path_text: str) -> ObjMesh:
    path = Path(path_text)
    vertices: list[Vec3] = []
    faces: list[list[int]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if line.startswith("v "):
                _, x, y, z, *_ = line.split()
                vertices.append((float(x), float(y), float(z)))
            elif line.startswith("f "):
                face = parse_face_indices(line.split()[1:], len(vertices))
                if len(face) >= 3:
                    faces.append(face)
    if not vertices or not faces:
        raise ValueError(f"OBJ has no usable geometry: {path}")
    return ObjMesh(vertices=vertices, faces=faces)


def load_obj_material_mesh(path: Path) -> ObjMaterialMesh:
    return _load_obj_material_mesh_cached(str(path.expanduser().resolve()))


@lru_cache(maxsize=OBJ_MESH_CACHE_SIZE)
def _load_obj_material_mesh_cached(path_text: str) -> ObjMaterialMesh:
    path = Path(path_text)
    vertices: list[Vec3] = []
    faces_by_material: dict[str | None, list[list[int]]] = {}
    current_material: str | None = None
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if line.startswith("v "):
                _, x, y, z, *_ = line.split()
                vertices.append((float(x), float(y), float(z)))
            elif line.startswith("usemtl "):
                current_material = line.split(maxsplit=1)[1].strip() or None
            elif line.startswith("f "):
                face = parse_face_indices(line.split()[1:], len(vertices))
                if len(face) >= 3:
                    faces_by_material.setdefault(current_material, []).append(face)
    if not vertices or not any(faces_by_material.values()):
        raise ValueError(f"OBJ has no usable material geometry: {path}")
    return ObjMaterialMesh(vertices=vertices, faces_by_material=faces_by_material)


def add_quad(
    vertices: list[Vec3],
    faces: list[list[int]],
    a: Vec3,
    b: Vec3,
    c: Vec3,
    d: Vec3,
    double_sided: bool = True,
) -> None:
    start = len(vertices)
    vertices.extend((a, b, c, d))
    faces.append([start, start + 1, start + 2])
    faces.append([start, start + 2, start + 3])
    if double_sided:
        faces.append([start + 2, start + 1, start])
        faces.append([start + 3, start + 2, start])


def build_room_mesh(room: Room) -> ObjMesh:
    width, length, height = room.width, room.length, room.height
    vertices: list[Vec3] = []
    faces: list[list[int]] = []
    add_quad(vertices, faces, (0.0, 0.0, 0.0), (width, 0.0, 0.0), (width, length, 0.0), (0.0, length, 0.0))
    add_quad(vertices, faces, (0.0, 0.0, height), (0.0, length, height), (width, length, height), (width, 0.0, height))
    add_quad(vertices, faces, (0.0, 0.0, 0.0), (0.0, 0.0, height), (width, 0.0, height), (width, 0.0, 0.0))
    add_quad(vertices, faces, (width, 0.0, 0.0), (width, 0.0, height), (width, length, height), (width, length, 0.0))
    add_quad(vertices, faces, (width, length, 0.0), (width, length, height), (0.0, length, height), (0.0, length, 0.0))
    add_quad(vertices, faces, (0.0, length, 0.0), (0.0, length, height), (0.0, 0.0, height), (0.0, 0.0, 0.0))
    return ObjMesh(vertices=vertices, faces=faces)


def transform_point_with_matrix(point: Vec3, matrix: tuple[float, ...]) -> Vec3:
    if len(matrix) != 16:
        raise ValueError("Transform matrix must contain 16 row-major values.")
    x, y, z = point
    return (
        matrix[0] * x + matrix[1] * y + matrix[2] * z + matrix[3],
        matrix[4] * x + matrix[5] * y + matrix[6] * z + matrix[7],
        matrix[8] * x + matrix[9] * y + matrix[10] * z + matrix[11],
    )


def transform_vertices(mesh: ObjMesh, placed: PlacedAsset) -> list[Vec3]:
    if placed.transform_matrix_4x4_row_major is not None:
        return [transform_point_with_matrix(vertex, placed.transform_matrix_4x4_row_major) for vertex in mesh.vertices]

    cos_yaw = math.cos(placed.yaw)
    sin_yaw = math.sin(placed.yaw)
    transformed: list[Vec3] = []
    for x, y, z in mesh.vertices:
        transformed.append(
            (
                placed.x + cos_yaw * x - sin_yaw * y,
                placed.y + sin_yaw * x + cos_yaw * y,
                placed.z + z,
            )
        )
    return transformed


def build_obstacle_grid(
    obstacles: list[StaticObstacle],
    cell_size: float,
    clearance: float,
) -> dict[tuple[int, int], list[int]]:
    grid: dict[tuple[int, int], list[int]] = {}
    for index, obstacle in enumerate(obstacles):
        min_ix = math.floor((obstacle.min_x - clearance) / cell_size)
        max_ix = math.floor((obstacle.max_x + clearance) / cell_size)
        min_iy = math.floor((obstacle.min_y - clearance) / cell_size)
        max_iy = math.floor((obstacle.max_y + clearance) / cell_size)
        for ix in range(min_ix, max_ix + 1):
            for iy in range(min_iy, max_iy + 1):
                grid.setdefault((ix, iy), []).append(index)
    return grid


def build_support_surfaces(
    upward_triangles: list[SupportTriangle],
    floor_z: float,
    min_area: float = 0.20,
) -> list[SupportSurface]:
    bins: dict[float, list[SupportTriangle]] = {}
    for tri in upward_triangles:
        if tri.z < floor_z + 0.45 or tri.z > floor_z + 1.45:
            continue
        key = round(tri.z / 0.05) * 0.05
        bins.setdefault(key, []).append(tri)

    surfaces: list[SupportSurface] = []
    for _, triangles in sorted(bins.items()):
        area = sum(tri.area for tri in triangles)
        if area < min_area:
            continue
        xs = [vertex[0] for tri in triangles for vertex in tri.vertices]
        ys = [vertex[1] for tri in triangles for vertex in tri.vertices]
        z = sum(tri.z * tri.area for tri in triangles) / area
        surfaces.append(
            SupportSurface(
                z=z,
                area=area,
                triangles=triangles,
                min_x=min(xs),
                max_x=max(xs),
                min_y=min(ys),
                max_y=max(ys),
            )
        )
    return sorted(surfaces, key=lambda surface: surface.area, reverse=True)


def load_bistro_base_scene(base_dir: Path) -> BistroBaseScene:
    base_dir = base_dir.expanduser().resolve()
    scene_obj = base_dir / "scene.obj"
    if not scene_obj.is_file():
        raise FileNotFoundError(scene_obj)

    vertices: list[Vec3] = []
    upward: list[SupportTriangle] = []
    triangle_stats: list[tuple[SupportTriangle, float, StaticObstacle]] = []
    with scene_obj.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if line.startswith("v "):
                _, x, y, z, *_ = line.split()
                vertices.append((float(x), float(y), float(z)))
            elif line.startswith("f "):
                face = parse_face_indices(line.split()[1:], len(vertices))
                if len(face) < 3:
                    continue
                first = vertices[face[0]]
                for index in range(1, len(face) - 1):
                    tri = (first, vertices[face[index]], vertices[face[index + 1]])
                    area, normal_z = triangle_area_and_up_normal(*tri)
                    if area <= 0.0:
                        continue
                    support_tri = SupportTriangle(
                        vertices=tri,
                        area=area,
                        z=sum(vertex[2] for vertex in tri) / 3.0,
                    )
                    triangle_stats.append((support_tri, normal_z, triangle_bbox(tri)))
                    if normal_z > 0.92:
                        upward.append(support_tri)

    if not vertices:
        raise ValueError(f"No vertices found in Bistro base scene: {scene_obj}")

    low_bins: dict[float, float] = {}
    for tri in upward:
        if tri.z > 0.5:
            continue
        key = round(tri.z / 0.05) * 0.05
        low_bins[key] = low_bins.get(key, 0.0) + tri.area
    if not low_bins:
        raise ValueError(f"No low upward floor surface found in Bistro base scene: {scene_obj}")

    floor_bin = max(low_bins.items(), key=lambda item: item[1])[0]
    floor_triangles = [tri for tri in upward if abs(tri.z - floor_bin) <= 0.075]
    floor_area = sum(tri.area for tri in floor_triangles)
    if floor_area <= 0.0:
        raise ValueError(f"Detected Bistro floor bin has zero area: {floor_bin}")

    floor_z = sum(tri.z * tri.area for tri in floor_triangles) / floor_area
    support_surfaces = build_support_surfaces(upward, floor_z)
    obstacle_z_min = floor_z + 0.05
    obstacle_z_max = floor_z + 2.25
    static_obstacles: list[StaticObstacle] = []
    for tri, normal_z, bbox in triangle_stats:
        is_floor_like = normal_z > 0.92 and abs(tri.z - floor_z) <= 0.075
        if is_floor_like or bbox.max_z <= obstacle_z_min or bbox.min_z >= obstacle_z_max:
            continue
        if abs(normal_z) > 0.92 and bbox.min_z > floor_z + 1.6:
            continue
        static_obstacles.append(bbox)

    obstacle_cell_size = 0.5
    obstacle_grid = build_obstacle_grid(static_obstacles, obstacle_cell_size, clearance=0.20)
    bbox_min = tuple(min(vertex[index] for vertex in vertices) for index in range(3))
    bbox_max = tuple(max(vertex[index] for vertex in vertices) for index in range(3))
    label_json = base_dir / "label.json"
    return BistroBaseScene(
        base_dir=base_dir,
        scene_obj=scene_obj,
        label_json=label_json if label_json.is_file() else None,
        vertex_count=len(vertices),
        bbox_min=bbox_min,
        bbox_max=bbox_max,
        floor_z=floor_z,
        floor_triangles=floor_triangles,
        support_surfaces=support_surfaces,
        static_obstacles=static_obstacles,
        obstacle_grid=obstacle_grid,
        obstacle_cell_size=obstacle_cell_size,
    )


def room_bounds(room: Room, margin: float) -> tuple[float, float, float, float]:
    return margin, room.width - margin, margin, room.length - margin


def aabb_for(asset: object, x: float, y: float, z: float, yaw: float) -> Box3D:
    half_x = asset.width / 2.0
    half_y = asset.length / 2.0
    cos_yaw = abs(math.cos(yaw))
    sin_yaw = abs(math.sin(yaw))
    world_half_x = cos_yaw * half_x + sin_yaw * half_y
    world_half_y = sin_yaw * half_x + cos_yaw * half_y
    return x - world_half_x, x + world_half_x, y - world_half_y, y + world_half_y, z, z + asset.height


def inside_room(box: Box3D, room: Room, margin: float = 0.25) -> bool:
    return box[0] >= margin and box[1] <= room.width - margin and box[2] >= margin and box[3] <= room.length - margin


def overlaps(box: Box3D, other: PlacedAsset, padding: float) -> bool:
    return not (
        box[1] + padding <= other.min_x
        or box[0] - padding >= other.max_x
        or box[3] + padding <= other.min_y
        or box[2] - padding >= other.max_y
        or box[5] <= other.min_z + 1e-5
        or box[4] >= other.max_z - 1e-5
    )


def can_place(box: Box3D, placed: list[PlacedAsset], padding: float) -> bool:
    return not any(overlaps(box, other, padding) for other in placed)


def point_in_triangle_2d(point: tuple[float, float], tri: SupportTriangle, tolerance: float = 1e-6) -> bool:
    px, py = point
    (ax, ay, _), (bx, by, _), (cx, cy, _) = tri.vertices
    denom = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(denom) < 1e-12:
        return False
    u = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / denom
    v = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / denom
    w = 1.0 - u - v
    return u >= -tolerance and v >= -tolerance and w >= -tolerance


def corners_for_box(x: float, y: float, half_x: float, half_y: float, yaw: float) -> list[tuple[float, float]]:
    cos_yaw = math.cos(yaw)
    sin_yaw = math.sin(yaw)
    corners: list[tuple[float, float]] = []
    for local_x, local_y in ((-half_x, -half_y), (half_x, -half_y), (half_x, half_y), (-half_x, half_y)):
        corners.append((x + cos_yaw * local_x - sin_yaw * local_y, y + sin_yaw * local_x + cos_yaw * local_y))
    return corners


def is_supported_on_floor(
    x: float,
    y: float,
    half_x: float,
    half_y: float,
    yaw: float,
    floor_triangles: list[SupportTriangle],
) -> bool:
    for corner in corners_for_box(x, y, half_x, half_y, yaw):
        if not any(point_in_triangle_2d(corner, tri, tolerance=1e-4) for tri in floor_triangles):
            return False
    return True


def random_point_on_triangle(rng: random.Random, tri: SupportTriangle) -> tuple[float, float]:
    a, b, c = tri.vertices
    r1 = math.sqrt(rng.random())
    r2 = rng.random()
    x = (1.0 - r1) * a[0] + r1 * (1.0 - r2) * b[0] + r1 * r2 * c[0]
    y = (1.0 - r1) * a[1] + r1 * (1.0 - r2) * b[1] + r1 * r2 * c[1]
    return x, y


def sample_support_point(rng: random.Random, triangles: list[SupportTriangle]) -> tuple[float, float]:
    total_area = sum(tri.area for tri in triangles)
    pick = rng.random() * total_area
    accumulated = 0.0
    for tri in triangles:
        accumulated += tri.area
        if accumulated >= pick:
            return random_point_on_triangle(rng, tri)
    return random_point_on_triangle(rng, triangles[-1])


def sample_support_surface(rng: random.Random, surfaces: list[SupportSurface]) -> SupportSurface:
    total_area = sum(surface.area for surface in surfaces)
    pick = rng.random() * total_area
    accumulated = 0.0
    for surface in surfaces:
        accumulated += surface.area
        if accumulated >= pick:
            return surface
    return surfaces[-1]


def inside_bistro_scene(
    box: Box3D,
    base_scene: BistroBaseScene,
    margin: float = 0.15,
) -> bool:
    return (
        box[0] >= base_scene.bbox_min[0] + margin
        and box[1] <= base_scene.bbox_max[0] - margin
        and box[2] >= base_scene.bbox_min[1] + margin
        and box[3] <= base_scene.bbox_max[1] - margin
        and box[5] <= base_scene.bbox_max[2] + 0.05
    )


def expanded_aabb_overlaps_static(box: Box3D, obstacle: StaticObstacle, clearance: float) -> bool:
    return not (
        box[1] + clearance <= obstacle.min_x
        or box[0] - clearance >= obstacle.max_x
        or box[3] + clearance <= obstacle.min_y
        or box[2] - clearance >= obstacle.max_y
        or box[5] <= obstacle.min_z + 1e-5
        or box[4] >= obstacle.max_z - 1e-5
    )


def collides_with_bistro_static(
    box: Box3D,
    base_scene: BistroBaseScene,
    clearance: float = 0.12,
) -> bool:
    min_ix = math.floor((box[0] - clearance) / base_scene.obstacle_cell_size)
    max_ix = math.floor((box[1] + clearance) / base_scene.obstacle_cell_size)
    min_iy = math.floor((box[2] - clearance) / base_scene.obstacle_cell_size)
    max_iy = math.floor((box[3] + clearance) / base_scene.obstacle_cell_size)
    candidate_indices: set[int] = set()
    for ix in range(min_ix, max_ix + 1):
        for iy in range(min_iy, max_iy + 1):
            candidate_indices.update(base_scene.obstacle_grid.get((ix, iy), ()))
    return any(
        expanded_aabb_overlaps_static(box, base_scene.static_obstacles[index], clearance)
        for index in candidate_indices
    )
