from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .front3d import FRONT3D_TO_SCENEGEN, Front3DIndex, matrix_multiply, transformed_bbox
from .geometry import load_obj_mesh
from .models import Asset, Front3DBaseScene, PlacedAsset, Vec3
from .paths import find_project_root, portable_path


@dataclass(frozen=True)
class ProceduralRoom:
    room_id: str
    room_type: str
    x0: float
    y0: float
    x1: float
    y1: float
    height: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def length(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return self.width * self.length


@dataclass(frozen=True)
class ProceduralAssetEntry:
    model_id: str
    asset: Asset
    payload: dict[str, Any]
    semantic: dict[str, Any]


@dataclass(frozen=True)
class ProceduralSceneBuild:
    scene_id: str
    base_scene: Front3DBaseScene
    rooms: list[ProceduralRoom]
    placements: list[PlacedAsset]
    skipped_objects: list[dict[str, object]]
    generation_report: dict[str, object]


@dataclass(frozen=True)
class LayoutRegion:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def length(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return self.width * self.length


def rotation_z_matrix(yaw: float) -> tuple[float, ...]:
    cos_yaw = math.cos(yaw)
    sin_yaw = math.sin(yaw)
    return (
        cos_yaw,
        -sin_yaw,
        0.0,
        0.0,
        sin_yaw,
        cos_yaw,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )


def translation_matrix(x: float, y: float, z: float) -> tuple[float, ...]:
    return (
        1.0,
        0.0,
        0.0,
        x,
        0.0,
        1.0,
        0.0,
        y,
        0.0,
        0.0,
        1.0,
        z,
        0.0,
        0.0,
        0.0,
        1.0,
    )


def unit_transform_for_yaw(yaw: float) -> tuple[float, ...]:
    return matrix_multiply(rotation_z_matrix(yaw), FRONT3D_TO_SCENEGEN)


def procedural_asset_bbox(entry: ProceduralAssetEntry, yaw: float, mesh_cache: dict[Path, list[Vec3]]) -> tuple[float, ...]:
    vertices = mesh_cache.get(entry.asset.obj_file)
    if vertices is None:
        vertices = load_obj_mesh(entry.asset.obj_file).vertices
        mesh_cache[entry.asset.obj_file] = vertices
    return transformed_bbox(vertices, unit_transform_for_yaw(yaw))


def procedural_asset_footprint_size(entry: ProceduralAssetEntry, yaw: float) -> tuple[float, float]:
    """Approximate SceneGen XY footprint from catalog dimensions before loading OBJ vertices.

    3D-FUTURE raw/normalized objects are treated as local Y-up meshes. The procedural transform maps
    local X to SceneGen X and local Z to SceneGen Y before applying a yaw around SceneGen Z.
    """

    width_x = max(0.0, float(entry.asset.width))
    width_y = max(0.0, float(entry.asset.height))
    if abs(math.sin(yaw)) > abs(math.cos(yaw)):
        return width_y, width_x
    return width_x, width_y


def procedural_asset_approx_bbox(
    entry: ProceduralAssetEntry,
    yaw: float,
    center_x: float,
    center_y: float,
) -> tuple[float, float, float, float, float, float]:
    width, length = procedural_asset_footprint_size(entry, yaw)
    return (
        center_x - width / 2.0,
        center_x + width / 2.0,
        center_y - length / 2.0,
        center_y + length / 2.0,
        0.0,
        max(0.0, float(entry.asset.length)),
    )


def placement_policy_for_class(policies: dict[str, dict[str, Any]], class_name: str) -> dict[str, Any]:
    return dict(policies.get(class_name) or policies["default"])


def candidate_pose_for_policy(
    room: ProceduralRoom,
    entry: ProceduralAssetEntry,
    policy: dict[str, Any],
    margin: float,
    rng: random.Random,
) -> tuple[float, float, float, float, float, str]:
    zone = str(policy.get("zone", "anywhere"))
    if zone == "wall":
        side = rng.choice(("south", "north", "west", "east"))
        yaw_by_side = {
            "south": 0.0,
            "north": math.pi,
            "west": math.pi / 2.0,
            "east": math.pi * 1.5,
        }
        yaw = yaw_by_side[side]
        width, length = procedural_asset_footprint_size(entry, yaw)
        min_x = room.x0 + margin + width / 2.0
        max_x = room.x1 - margin - width / 2.0
        min_y = room.y0 + margin + length / 2.0
        max_y = room.y1 - margin - length / 2.0
        offset = float(policy.get("wall_offset_m", 0.0))
        if side == "south":
            return yaw, rng.uniform(min_x, max_x), min(min_y + offset, max_y), width, length, zone
        if side == "north":
            return yaw, rng.uniform(min_x, max_x), max(max_y - offset, min_y), width, length, zone
        if side == "west":
            return yaw, min(min_x + offset, max_x), rng.uniform(min_y, max_y), width, length, zone
        return yaw, max(max_x - offset, min_x), rng.uniform(min_y, max_y), width, length, zone

    yaw = rng.choice([0.0, math.pi / 2.0, math.pi, math.pi * 1.5])
    width, length = procedural_asset_footprint_size(entry, yaw)
    min_x = room.x0 + margin + width / 2.0
    max_x = room.x1 - margin - width / 2.0
    min_y = room.y0 + margin + length / 2.0
    max_y = room.y1 - margin - length / 2.0
    if zone == "center":
        radius = max(0.0, min(room.width, room.length) * float(policy.get("center_radius_ratio", 0.35)))
        center_x = (room.x0 + room.x1) / 2.0 + rng.uniform(-radius, radius)
        center_y = (room.y0 + room.y1) / 2.0 + rng.uniform(-radius, radius)
        return yaw, min(max(center_x, min_x), max_x), min(max(center_y, min_y), max_y), width, length, zone
    return yaw, rng.uniform(min_x, max_x), rng.uniform(min_y, max_y), width, length, "anywhere"


def procedural_asset_transform_for_center(
    entry: ProceduralAssetEntry,
    yaw: float,
    center_x: float,
    center_y: float,
    mesh_cache: dict[Path, list[Vec3]],
) -> tuple[tuple[float, ...], tuple[float, float, float, float, float, float]]:
    local_matrix = unit_transform_for_yaw(yaw)
    bbox = procedural_asset_bbox(entry, yaw, mesh_cache)
    local_center_x = (bbox[0] + bbox[1]) / 2.0
    local_center_y = (bbox[2] + bbox[3]) / 2.0
    translation = translation_matrix(center_x - local_center_x, center_y - local_center_y, -bbox[4])
    matrix = matrix_multiply(translation, local_matrix)
    vertices = mesh_cache[entry.asset.obj_file]
    world_bbox = transformed_bbox(vertices, matrix)
    return matrix, world_bbox


def boxes_overlap_xy(
    left: tuple[float, float, float, float, float, float],
    right: tuple[float, float, float, float, float, float],
    padding: float,
) -> bool:
    return not (
        left[1] + padding <= right[0]
        or right[1] + padding <= left[0]
        or left[3] + padding <= right[2]
        or right[3] + padding <= left[2]
    )


def room_contains_bbox(room: ProceduralRoom, bbox: tuple[float, float, float, float, float, float], margin: float) -> bool:
    return (
        bbox[0] >= room.x0 + margin
        and bbox[1] <= room.x1 - margin
        and bbox[2] >= room.y0 + margin
        and bbox[3] <= room.y1 - margin
    )


def mesh_payload(uid: str, mesh_type: str, vertices: list[Vec3], faces: list[list[int]]) -> dict[str, object]:
    triangulated: list[int] = []
    for face in faces:
        if len(face) < 3:
            continue
        first = face[0]
        for index in range(1, len(face) - 1):
            triangulated.extend([first, face[index], face[index + 1]])
    return {
        "uid": uid,
        "jid": "",
        "type": mesh_type,
        "xyz": [round(value, 6) for vertex in vertices for value in vertex],
        "faces": triangulated,
    }


def add_quad(vertices: list[Vec3], faces: list[list[int]], a: Vec3, b: Vec3, c: Vec3, d: Vec3) -> None:
    start = len(vertices)
    vertices.extend([a, b, c, d])
    faces.append([start, start + 1, start + 2])
    faces.append([start, start + 2, start + 3])


def box_mesh(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> tuple[list[Vec3], list[list[int]]]:
    vertices: list[Vec3] = []
    faces: list[list[int]] = []
    add_quad(vertices, faces, (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0))
    add_quad(vertices, faces, (x0, y0, z1), (x0, y1, z1), (x1, y1, z1), (x1, y0, z1))
    add_quad(vertices, faces, (x0, y0, z0), (x0, y0, z1), (x1, y0, z1), (x1, y0, z0))
    add_quad(vertices, faces, (x1, y0, z0), (x1, y0, z1), (x1, y1, z1), (x1, y1, z0))
    add_quad(vertices, faces, (x1, y1, z0), (x1, y1, z1), (x0, y1, z1), (x0, y1, z0))
    add_quad(vertices, faces, (x0, y1, z0), (x0, y1, z1), (x0, y0, z1), (x0, y0, z0))
    return vertices, faces


def plane_mesh(x0: float, y0: float, z: float, x1: float, y1: float) -> tuple[list[Vec3], list[list[int]]]:
    vertices = [(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)]
    return vertices, [[0, 1, 2], [0, 2, 3]]


def write_architecture_obj(path: Path, rooms: list[ProceduralRoom], meshes: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("# Generated by SceneGen procedural_front3d\n")
        vertex_offset = 0
        for mesh in meshes:
            mesh_type = str(mesh["type"]).lower()
            material = "floor" if "floor" in mesh_type else "ceiling" if "ceiling" in mesh_type else "wall"
            handle.write(f"o {mesh['uid']}\n")
            handle.write(f"usemtl {material}\n")
            xyz = list(mesh["xyz"])
            for index in range(0, len(xyz), 3):
                handle.write(f"v {float(xyz[index]):.6f} {float(xyz[index + 1]):.6f} {float(xyz[index + 2]):.6f}\n")
            faces = list(mesh["faces"])
            for index in range(0, len(faces), 3):
                a, b, c = int(faces[index]), int(faces[index + 1]), int(faces[index + 2])
                handle.write(f"f {vertex_offset + a + 1} {vertex_offset + b + 1} {vertex_offset + c + 1}\n")
            vertex_offset += len(xyz) // 3
        handle.write(f"# room_count {len(rooms)}\n")


def room_type_sequence(room_count: int, room_types: tuple[str, ...], rng: random.Random, *, shuffle: bool) -> list[str]:
    if not room_types:
        return ["Room" for _ in range(room_count)]
    if not shuffle:
        return [room_types[index % len(room_types)] for index in range(room_count)]
    sequence: list[str] = []
    while len(sequence) < room_count:
        batch = list(room_types)
        rng.shuffle(batch)
        sequence.extend(batch)
    return sequence[:room_count]


def make_grid_room_layout(
    rng: random.Random,
    room_count_range: tuple[int, int],
    room_width_range: tuple[float, float],
    room_length_range: tuple[float, float],
    height_range: tuple[float, float],
    room_types: tuple[str, ...],
) -> list[ProceduralRoom]:
    room_count = rng.randint(room_count_range[0], room_count_range[1])
    columns = max(1, math.ceil(math.sqrt(room_count)))
    rows = max(1, math.ceil(room_count / columns))
    column_widths = [round(rng.uniform(*room_width_range), 3) for _ in range(columns)]
    row_lengths = [round(rng.uniform(*room_length_range), 3) for _ in range(rows)]
    height = round(rng.uniform(*height_range), 3)
    type_sequence = room_type_sequence(room_count, room_types, rng, shuffle=False)
    rooms: list[ProceduralRoom] = []
    room_index = 0
    y0 = 0.0
    for row in range(rows):
        x0 = 0.0
        for col in range(columns):
            if room_index >= room_count:
                break
            rooms.append(
                ProceduralRoom(
                    room_id=f"proc_room_{room_index:02d}",
                    room_type=type_sequence[room_index],
                    x0=round(x0, 6),
                    y0=round(y0, 6),
                    x1=round(x0 + column_widths[col], 6),
                    y1=round(y0 + row_lengths[row], 6),
                    height=height,
                )
            )
            x0 += column_widths[col]
            room_index += 1
        y0 += row_lengths[row]
    return rooms


def split_region_once(
    rng: random.Random,
    region: LayoutRegion,
    room_width_range: tuple[float, float],
    room_length_range: tuple[float, float],
) -> tuple[LayoutRegion, LayoutRegion] | None:
    min_width = float(room_width_range[0])
    min_length = float(room_length_range[0])
    can_split_x = region.width >= 2.0 * min_width
    can_split_y = region.length >= 2.0 * min_length
    if not can_split_x and not can_split_y:
        return None
    if can_split_x and can_split_y:
        split_x = region.width / max(region.length, 1e-9) >= rng.uniform(0.75, 1.25)
    else:
        split_x = can_split_x
    ratio = rng.uniform(0.38, 0.62)
    if split_x:
        cut = region.x0 + region.width * ratio
        cut = min(max(cut, region.x0 + min_width), region.x1 - min_width)
        cut = round(cut, 6)
        return (
            LayoutRegion(region.x0, region.y0, cut, region.y1),
            LayoutRegion(cut, region.y0, region.x1, region.y1),
        )
    cut = region.y0 + region.length * ratio
    cut = min(max(cut, region.y0 + min_length), region.y1 - min_length)
    cut = round(cut, 6)
    return (
        LayoutRegion(region.x0, region.y0, region.x1, cut),
        LayoutRegion(region.x0, cut, region.x1, region.y1),
    )


def make_split_tree_room_layout(
    rng: random.Random,
    room_count_range: tuple[int, int],
    room_width_range: tuple[float, float],
    room_length_range: tuple[float, float],
    height_range: tuple[float, float],
    room_types: tuple[str, ...],
) -> list[ProceduralRoom]:
    room_count = rng.randint(room_count_range[0], room_count_range[1])
    scale = math.sqrt(room_count)
    root_width = round(rng.uniform(room_width_range[0] * scale, room_width_range[1] * scale), 3)
    root_length = round(rng.uniform(room_length_range[0] * scale, room_length_range[1] * scale), 3)
    regions = [LayoutRegion(0.0, 0.0, root_width, root_length)]
    while len(regions) < room_count:
        candidates = sorted(enumerate(regions), key=lambda item: item[1].area, reverse=True)
        split_result: tuple[int, tuple[LayoutRegion, LayoutRegion]] | None = None
        for index, region in candidates:
            split = split_region_once(rng, region, room_width_range, room_length_range)
            if split is not None:
                split_result = (index, split)
                break
        if split_result is None:
            # Configuration is too tight for the sampled count; returning fewer rooms is better than
            # manufacturing invalid zero-area regions.
            break
        index, (left, right) = split_result
        regions.pop(index)
        regions.extend([left, right])
    regions = sorted(regions, key=lambda region: (region.y0, region.x0))
    height = round(rng.uniform(*height_range), 3)
    type_sequence = room_type_sequence(len(regions), room_types, rng, shuffle=True)
    return [
        ProceduralRoom(
            room_id=f"proc_room_{index:02d}",
            room_type=type_sequence[index],
            x0=round(region.x0, 6),
            y0=round(region.y0, 6),
            x1=round(region.x1, 6),
            y1=round(region.y1, 6),
            height=height,
        )
        for index, region in enumerate(regions)
    ]


def make_room_layout(
    rng: random.Random,
    layout: str,
    room_count_range: tuple[int, int],
    room_width_range: tuple[float, float],
    room_length_range: tuple[float, float],
    height_range: tuple[float, float],
    room_types: tuple[str, ...],
) -> list[ProceduralRoom]:
    if layout == "grid":
        return make_grid_room_layout(rng, room_count_range, room_width_range, room_length_range, height_range, room_types)
    if layout == "split_tree":
        return make_split_tree_room_layout(rng, room_count_range, room_width_range, room_length_range, height_range, room_types)
    raise ValueError(f"Unsupported procedural layout: {layout}")


def select_room_profile_spec(room_type: str, profiles: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Select the configured furnishing profile for a procedural room type."""

    if room_type in profiles:
        return room_type, profiles[room_type]
    lowered = room_type.lower()
    for name, profile in profiles.items():
        if name.lower() == lowered:
            return name, profile
    for name, profile in profiles.items():
        name_lowered = name.lower()
        if name != "default" and (name_lowered in lowered or lowered in name_lowered):
            return name, profile
    return "default", profiles["default"]


def select_room_profile(room_type: str, profiles: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    """Select a room profile name and class sequence."""

    profile_name, profile = select_room_profile_spec(room_type, profiles)
    return profile_name, list(profile["classes"])


def desired_classes_from_profile(
    room_type: str,
    profiles: dict[str, dict[str, Any]],
    target_count: int,
    rng: random.Random,
) -> list[str]:
    """Expand a room furnishing profile to the sampled object count."""

    _profile_name, classes = select_room_profile(room_type, profiles)
    if target_count <= len(classes):
        return classes[:target_count]
    return classes + [rng.choice(classes) for _ in range(target_count - len(classes))]


def object_count_for_room_area(room_area: float, object_count_config: dict[str, Any], rng: random.Random) -> int:
    strategy = str(object_count_config["strategy"])
    if strategy == "range":
        min_count, max_count = (int(value) for value in object_count_config["range"])
        return rng.randint(min_count, max_count)
    if strategy == "area_adaptive":
        min_count = int(object_count_config["min"])
        max_count = int(object_count_config["max"])
        area_per_object = float(object_count_config["area_per_object_m2"])
        jitter_min, jitter_max = (int(value) for value in object_count_config["jitter"])
        base_count = int(round(float(room_area) / area_per_object))
        jitter = rng.randint(jitter_min, jitter_max)
        return min(max(base_count + jitter, min_count), max_count)
    raise ValueError(f"Unsupported procedural object count strategy: {strategy}")


def semantic_matches_filter(semantic: dict[str, Any], class_filter: dict[str, list[str]]) -> bool:
    for field_name, terms in class_filter.items():
        value = str(semantic.get(field_name, "")).lower()
        if not any(term in value for term in terms):
            return False
    return True


def entries_matching_profile_filter(
    entries: list[ProceduralAssetEntry],
    profile: dict[str, Any],
    class_name: str,
) -> tuple[list[ProceduralAssetEntry], bool]:
    class_filter = dict(profile.get("filters") or {}).get(class_name, {})
    if not class_filter:
        return entries, False
    filtered = [entry for entry in entries if semantic_matches_filter(entry.semantic, class_filter)]
    return (filtered or entries), bool(filtered)


def select_room_group_specs(room_type: str, placement_groups: dict[str, Any]) -> list[dict[str, Any]]:
    """Select relationship placement groups for a room type."""

    if not placement_groups.get("enabled", True):
        return []
    groups_by_room = dict(placement_groups.get("room_types") or {})
    if room_type in groups_by_room:
        return list(groups_by_room[room_type])
    lowered = room_type.lower()
    for name, specs in groups_by_room.items():
        if str(name).lower() == lowered:
            return list(specs)
    for name, specs in groups_by_room.items():
        name_lowered = str(name).lower()
        if name_lowered in lowered or lowered in name_lowered:
            return list(specs)
    return []


def count_class(classes: list[str], class_name: str) -> int:
    return sum(1 for item in classes if item == class_name)


def remove_class(classes: list[str], class_name: str, count: int = 1) -> None:
    for _index in range(count):
        classes.remove(class_name)


def companion_directions(count: int) -> list[tuple[float, float]]:
    if count <= 0:
        return []
    if count == 1:
        return [(0.0, -1.0)]
    if count == 2:
        return [(-1.0, 0.0), (1.0, 0.0)]
    if count == 3:
        return [(math.cos(index * math.tau / 3.0), math.sin(index * math.tau / 3.0)) for index in range(3)]
    cardinal = [(0.0, -1.0), (1.0, 0.0), (0.0, 1.0), (-1.0, 0.0)]
    if count <= 4:
        return cardinal[:count]
    return [(math.cos(index * math.tau / count), math.sin(index * math.tau / count)) for index in range(count)]


def overlapping_interval(a0: float, a1: float, b0: float, b1: float) -> tuple[float, float] | None:
    start = max(a0, b0)
    end = min(a1, b1)
    if end - start <= 1e-6:
        return None
    return start, end


def append_wall_with_center_door(
    meshes: list[dict[str, object]],
    wall_specs: list[tuple[float, float, float, float, str]],
    *,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    door_width: float,
    horizontal: bool,
) -> None:
    length = (x1 - x0) if horizontal else (y1 - y0)
    gap = min(max(0.0, door_width), max(0.0, length - 2.0e-6))
    if gap <= 0.0:
        wall_specs.append((x0, y0, x1, y1, "Wall"))
        return
    if horizontal:
        gap_x0 = x0 + (length - gap) / 2.0
        gap_x1 = gap_x0 + gap
        if gap_x0 > x0:
            wall_specs.append((x0, y0, gap_x0, y1, "Wall"))
        if gap_x1 < x1:
            wall_specs.append((gap_x1, y0, x1, y1, "Wall"))
        door_vertices, door_faces = plane_mesh(gap_x0, y0, 0.0, gap_x1, y1)
        meshes.append(mesh_payload(f"door_horizontal_{len(meshes):04d}", "Door", door_vertices, door_faces))
        return
    gap_y0 = y0 + (length - gap) / 2.0
    gap_y1 = gap_y0 + gap
    if gap_y0 > y0:
        wall_specs.append((x0, y0, x1, gap_y0, "Wall"))
    if gap_y1 < y1:
        wall_specs.append((x0, gap_y1, x1, y1, "Wall"))
    door_vertices, door_faces = plane_mesh(x0, gap_y0, 0.0, x1, gap_y1)
    meshes.append(mesh_payload(f"door_vertical_{len(meshes):04d}", "Door", door_vertices, door_faces))


def architecture_meshes_for_rooms(
    rooms: list[ProceduralRoom],
    wall_thickness: float,
    door_width: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    meshes: list[dict[str, object]] = []
    room_children: list[dict[str, object]] = []
    if not rooms:
        return meshes, room_children
    min_x = min(room.x0 for room in rooms)
    min_y = min(room.y0 for room in rooms)
    max_x = max(room.x1 for room in rooms)
    max_y = max(room.y1 for room in rooms)
    height = max(room.height for room in rooms)

    for room_index, room in enumerate(rooms):
        floor_vertices, floor_faces = plane_mesh(room.x0, room.y0, 0.0, room.x1, room.y1)
        ceiling_vertices, ceiling_faces = plane_mesh(room.x0, room.y0, room.height, room.x1, room.y1)
        floor_uid = f"{room.room_id}/floor"
        ceiling_uid = f"{room.room_id}/ceiling"
        meshes.append(mesh_payload(floor_uid, "Floor", floor_vertices, floor_faces))
        meshes.append(mesh_payload(ceiling_uid, "Ceiling", ceiling_vertices, ceiling_faces))
        room_children.append(
            {
                "room_index": room_index,
                "children": [
                    {"ref": floor_uid, "instanceid": f"{floor_uid}/instance", "pos": [0, 0, 0], "rot": [0, 0, 0, 1], "scale": [1, 1, 1]},
                    {
                        "ref": ceiling_uid,
                        "instanceid": f"{ceiling_uid}/instance",
                        "pos": [0, 0, 0],
                        "rot": [0, 0, 0, 1],
                        "scale": [1, 1, 1],
                    },
                ],
            }
        )

    wall_specs: list[tuple[float, float, float, float, str]] = [
        (min_x, min_y, max_x, min_y + wall_thickness, "Wall"),
        (min_x, max_y - wall_thickness, max_x, max_y, "Wall"),
        (min_x, min_y, min_x + wall_thickness, max_y, "Wall"),
        (max_x - wall_thickness, min_y, max_x, max_y, "Wall"),
    ]

    # Internal walls are generated only on true shared room edges. This supports split-tree
    # layouts where not every room boundary aligns to a global grid line.
    for left_index, left_room in enumerate(rooms):
        for right_room in rooms[left_index + 1 :]:
            if abs(left_room.x1 - right_room.x0) < 1e-6 or abs(right_room.x1 - left_room.x0) < 1e-6:
                x = left_room.x1 if abs(left_room.x1 - right_room.x0) < 1e-6 else right_room.x1
                interval = overlapping_interval(left_room.y0, left_room.y1, right_room.y0, right_room.y1)
                if interval is None:
                    continue
                y0, y1 = interval
                append_wall_with_center_door(
                    meshes,
                    wall_specs,
                    x0=x - wall_thickness / 2.0,
                    y0=y0,
                    x1=x + wall_thickness / 2.0,
                    y1=y1,
                    door_width=door_width,
                    horizontal=False,
                )
            if abs(left_room.y1 - right_room.y0) < 1e-6 or abs(right_room.y1 - left_room.y0) < 1e-6:
                y = left_room.y1 if abs(left_room.y1 - right_room.y0) < 1e-6 else right_room.y1
                interval = overlapping_interval(left_room.x0, left_room.x1, right_room.x0, right_room.x1)
                if interval is None:
                    continue
                x0, x1 = interval
                append_wall_with_center_door(
                    meshes,
                    wall_specs,
                    x0=x0,
                    y0=y - wall_thickness / 2.0,
                    x1=x1,
                    y1=y + wall_thickness / 2.0,
                    door_width=door_width,
                    horizontal=True,
                )

    for wall_index, (x0, y0, x1, y1, mesh_type) in enumerate(wall_specs):
        if x1 <= x0 or y1 <= y0:
            continue
        vertices, faces = box_mesh(x0, y0, 0.0, x1, y1, height)
        meshes.append(mesh_payload(f"wall_{wall_index:04d}", mesh_type, vertices, faces))
    return meshes, room_children


def write_procedural_source_files(
    scene_dir: Path,
    scene_id: str,
    rooms: list[ProceduralRoom],
    meshes: list[dict[str, object]],
    room_children: list[dict[str, object]],
) -> tuple[Path, Path, Path, tuple[float, float, float], tuple[float, float, float]]:
    source_dir = scene_dir / "procedural_source"
    source_dir.mkdir(parents=True, exist_ok=True)
    architecture_obj = source_dir / "architecture.obj"
    source_json = source_dir / "scene.json"
    metadata_json = source_dir / "architecture.json"
    write_architecture_obj(architecture_obj, rooms, meshes)
    bbox_min = (
        min(min(float(value) for value in mesh["xyz"][0::3]) for mesh in meshes),
        min(min(float(value) for value in mesh["xyz"][1::3]) for mesh in meshes),
        min(min(float(value) for value in mesh["xyz"][2::3]) for mesh in meshes),
    )
    bbox_max = (
        max(max(float(value) for value in mesh["xyz"][0::3]) for mesh in meshes),
        max(max(float(value) for value in mesh["xyz"][1::3]) for mesh in meshes),
        max(max(float(value) for value in mesh["xyz"][2::3]) for mesh in meshes),
    )
    rooms_payload: list[dict[str, object]] = []
    children_by_index = {int(item["room_index"]): item["children"] for item in room_children}
    for index, room in enumerate(rooms):
        rooms_payload.append(
            {
                "type": room.room_type,
                "instanceid": room.room_id,
                "size": round(room.area, 3),
                "pos": [0, 0, 0],
                "rot": [0, 0, 0, 1],
                "scale": [1, 1, 1],
                "children": children_by_index.get(index, []),
            }
        )
    source_payload = {
        "uid": scene_id,
        "version": "scenegen.procedural_front3d.v1",
        "furniture": [],
        "mesh": meshes,
        "scene": {"room": rooms_payload},
    }
    source_json.write_text(json.dumps(source_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata_payload = {
        "schema_version": "scenegen.procedural_front3d.v1",
        "dataset": "scenegen-procedural",
        "asset_kind": "architecture",
        "variant": "raw",
        "id": scene_id,
        "files": {"obj": portable_path(architecture_obj, find_project_root())},
        "geometry": {
            "bbox": {"min": list(bbox_min), "max": list(bbox_max)},
            "size": {"x": bbox_max[0] - bbox_min[0], "y": bbox_max[1] - bbox_min[1], "z": bbox_max[2] - bbox_min[2]},
        },
        "materials": {
            "sionna": ["itu-concrete"],
            "source_to_sionna": [
                {"source": "floor", "sionna": "itu-concrete", "itu_type": "concrete", "confidence": "high"},
                {"source": "ceiling", "sionna": "itu-concrete", "itu_type": "concrete", "confidence": "high"},
                {"source": "wall", "sionna": "itu-concrete", "itu_type": "concrete", "confidence": "high"},
            ],
        },
        "procedural": {
            "room_count": len(rooms),
            "rooms": [
                {
                    "room_id": room.room_id,
                    "room_type": room.room_type,
                    "bounds_xy": [room.x0, room.y0, room.x1, room.y1],
                    "height_m": room.height,
                }
                for room in rooms
            ],
        },
    }
    metadata_json.write_text(json.dumps(metadata_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return architecture_obj, source_json, metadata_json, bbox_min, bbox_max


class ProceduralFront3DGenerator:
    def __init__(self, index: Front3DIndex, args: Any):
        self.index = index
        self.args = args
        self.repo_root = find_project_root()
        self.asset_pool = self._build_asset_pool()

    def _build_asset_pool(self) -> dict[str, list[ProceduralAssetEntry]]:
        pool: dict[str, list[ProceduralAssetEntry]] = {"table": [], "seat": [], "floor": []}
        max_assets = int(self.args.procedural_asset_pool_limit)
        for model_id in sorted(self.index.by_model_id):
            if all(len(items) >= max_assets for items in pool.values()):
                break
            payload = self.index.object_payload(model_id)
            if not payload:
                continue
            asset = self.index.object_asset(model_id)
            if asset is None or asset.placement_class not in pool:
                continue
            if asset.width <= 0 or asset.length <= 0 or asset.height <= 0:
                continue
            entry = ProceduralAssetEntry(
                model_id=model_id,
                asset=asset,
                payload=payload,
                semantic=dict(payload.get("semantic") or {}),
            )
            items = pool[asset.placement_class]
            if len(items) < max_assets:
                items.append(entry)
        return pool

    def _entries_for_room(self, room_type: str, class_name: str) -> list[ProceduralAssetEntry]:
        entries = self.asset_pool.get(class_name, [])
        if not entries:
            return []
        _profile_name, profile = select_room_profile_spec(room_type, self.args.procedural_room_profiles)
        filtered, _matched = entries_matching_profile_filter(entries, profile, class_name)
        return filtered

    def room_profile_for_room(self, room_type: str) -> tuple[str, list[str]]:
        return select_room_profile(room_type, self.args.procedural_room_profiles)

    def room_profile_detail_for_room(self, room_type: str) -> tuple[str, list[str], dict[str, dict[str, list[str]]]]:
        profile_name, profile = select_room_profile_spec(room_type, self.args.procedural_room_profiles)
        return profile_name, list(profile["classes"]), dict(profile.get("filters") or {})

    def desired_classes_for_room(self, room: ProceduralRoom, rng: random.Random) -> list[str]:
        target_count = object_count_for_room_area(room.area, self.args.procedural_object_count, rng)
        return desired_classes_from_profile(
            room.room_type,
            self.args.procedural_room_profiles,
            target_count,
            rng,
        )

    def _append_placement(
        self,
        placements: list[PlacedAsset],
        *,
        scene_index: int,
        room: ProceduralRoom,
        entry: ProceduralAssetEntry,
        matrix: tuple[float, ...],
        bbox: tuple[float, float, float, float, float, float],
        yaw: float,
        metadata: dict[str, object] | None = None,
    ) -> None:
        placement_index = len(placements)
        placement_metadata: dict[str, object] = {
            "generator": "procedural_front3d",
            "room_type": room.room_type,
            "category": entry.semantic.get("category"),
            "super_category": entry.semantic.get("super_category"),
            "material": entry.semantic.get("material"),
            "object_variant": self.index.config.object_variant,
        }
        if metadata:
            placement_metadata.update(metadata)
        placements.append(
            PlacedAsset(
                asset=entry.asset,
                instance_name=f"procedural_{scene_index:04d}_{placement_index:04d}_{entry.asset.export_name}",
                x=matrix[3],
                y=matrix[7],
                z=matrix[11],
                yaw=yaw,
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
                    "scene_id": f"procedural_{scene_index:04d}",
                    "room_instanceid": room.room_id,
                    "model_id": entry.model_id,
                },
                metadata=placement_metadata,
            )
        )

    def _try_place_group(
        self,
        room: ProceduralRoom,
        spec: dict[str, Any],
        companion_count: int,
        scene_index: int,
        rng: random.Random,
        room_boxes: list[tuple[float, float, float, float, float, float]],
        placements: list[PlacedAsset],
        mesh_cache: dict[Path, list[Vec3]],
        stats: dict[str, object],
    ) -> bool:
        anchor_class = str(spec["anchor_class"])
        companion_class = str(spec["companion_class"])
        anchor_candidates = self._entries_for_room(room.room_type, anchor_class)
        companion_candidates = self._entries_for_room(room.room_type, companion_class)
        if not anchor_candidates or companion_count > 0 and not companion_candidates:
            return False
        margin = float(self.args.procedural_wall_margin_m)
        object_margin = float(self.args.procedural_object_margin_m)
        max_attempts = int(spec["max_attempts"])
        gap_min, gap_max = (float(value) for value in spec["companion_gap_m"])
        for _attempt in range(max_attempts):
            stats["relation_group_attempt_count"] = int(stats["relation_group_attempt_count"]) + 1
            entry = rng.choice(anchor_candidates)
            policy = placement_policy_for_class(self.args.procedural_placement_policy, anchor_class)
            yaw, center_x, center_y, width, length, _zone = candidate_pose_for_policy(room, entry, policy, margin, rng)
            if width >= room.width - 2.0 * margin or length >= room.length - 2.0 * margin:
                stats["size_reject_count"] = int(stats["size_reject_count"]) + 1
                continue
            approx_bbox = procedural_asset_approx_bbox(entry, yaw, center_x, center_y)
            if any(boxes_overlap_xy(approx_bbox, other, object_margin) for other in room_boxes):
                stats["approx_collision_reject_count"] = int(stats["approx_collision_reject_count"]) + 1
                continue
            stats["exact_bbox_count"] = int(stats["exact_bbox_count"]) + 1
            matrix, bbox = procedural_asset_transform_for_center(entry, yaw, center_x, center_y, mesh_cache)
            if not room_contains_bbox(room, bbox, margin=0.0):
                stats["exact_room_reject_count"] = int(stats["exact_room_reject_count"]) + 1
                continue
            if any(boxes_overlap_xy(bbox, other, object_margin) for other in room_boxes):
                stats["exact_collision_reject_count"] = int(stats["exact_collision_reject_count"]) + 1
                continue
            group_boxes = [bbox]
            group_items: list[tuple[ProceduralAssetEntry, tuple[float, ...], tuple[float, float, float, float, float, float], float, str]] = [
                (entry, matrix, bbox, yaw, "anchor")
            ]
            anchor_center_x = (bbox[0] + bbox[1]) / 2.0
            anchor_center_y = (bbox[2] + bbox[3]) / 2.0
            anchor_width = bbox[1] - bbox[0]
            anchor_length = bbox[3] - bbox[2]
            directions = companion_directions(companion_count)
            rng.shuffle(directions)
            group_failed = False
            for direction_x, direction_y in directions:
                companion = rng.choice(companion_candidates)
                companion_yaw = math.atan2(-direction_y, -direction_x)
                companion_width, companion_length = procedural_asset_footprint_size(companion, companion_yaw)
                anchor_half = abs(direction_x) * anchor_width / 2.0 + abs(direction_y) * anchor_length / 2.0
                companion_half = abs(direction_x) * companion_width / 2.0 + abs(direction_y) * companion_length / 2.0
                distance = anchor_half + companion_half + rng.uniform(gap_min, gap_max)
                companion_x = anchor_center_x + direction_x * distance
                companion_y = anchor_center_y + direction_y * distance
                companion_approx_bbox = procedural_asset_approx_bbox(companion, companion_yaw, companion_x, companion_y)
                if not room_contains_bbox(room, companion_approx_bbox, margin=0.0):
                    group_failed = True
                    break
                if any(boxes_overlap_xy(companion_approx_bbox, other, object_margin) for other in [*room_boxes, *group_boxes]):
                    group_failed = True
                    break
                stats["exact_bbox_count"] = int(stats["exact_bbox_count"]) + 1
                companion_matrix, companion_bbox = procedural_asset_transform_for_center(
                    companion,
                    companion_yaw,
                    companion_x,
                    companion_y,
                    mesh_cache,
                )
                if not room_contains_bbox(room, companion_bbox, margin=0.0):
                    group_failed = True
                    break
                if any(boxes_overlap_xy(companion_bbox, other, object_margin) for other in [*room_boxes, *group_boxes]):
                    group_failed = True
                    break
                group_boxes.append(companion_bbox)
                group_items.append((companion, companion_matrix, companion_bbox, companion_yaw, "companion"))
            if group_failed:
                continue
            group_index = int(stats["relation_group_success_count"])
            group_id = f"{room.room_id}/{spec['name']}/{group_index:02d}"
            for item_entry, item_matrix, item_bbox, item_yaw, role in group_items:
                self._append_placement(
                    placements,
                    scene_index=scene_index,
                    room=room,
                    entry=item_entry,
                    matrix=item_matrix,
                    bbox=item_bbox,
                    yaw=item_yaw,
                    metadata={
                        "placement_group": spec["name"],
                        "placement_group_id": group_id,
                        "placement_group_role": role,
                    },
                )
            room_boxes.extend(group_boxes)
            stats["relation_group_success_count"] = group_index + 1
            stats["relation_group_placement_count"] = int(stats["relation_group_placement_count"]) + len(group_items)
            group_counts = stats["relation_group_name_counts"]
            if isinstance(group_counts, dict):
                group_counts[str(spec["name"])] = int(group_counts.get(str(spec["name"]), 0)) + 1
            return True
        return False

    def _place_room_groups(
        self,
        room: ProceduralRoom,
        desired_classes: list[str],
        scene_index: int,
        rng: random.Random,
        room_boxes: list[tuple[float, float, float, float, float, float]],
        placements: list[PlacedAsset],
        mesh_cache: dict[Path, list[Vec3]],
        stats: dict[str, object],
    ) -> None:
        for spec in select_room_group_specs(room.room_type, self.args.procedural_placement_groups):
            anchor_class = str(spec["anchor_class"])
            companion_class = str(spec["companion_class"])
            if count_class(desired_classes, anchor_class) < 1:
                continue
            min_companions, max_companions = (int(value) for value in spec["companion_count"])
            available_companions = count_class(desired_classes, companion_class)
            if anchor_class == companion_class:
                available_companions -= 1
            if available_companions < min_companions:
                continue
            companion_count = rng.randint(min_companions, min(max_companions, available_companions))
            placed = self._try_place_group(
                room,
                spec,
                companion_count,
                scene_index,
                rng,
                room_boxes,
                placements,
                mesh_cache,
                stats,
            )
            if not placed:
                stats["relation_group_failure_count"] = int(stats["relation_group_failure_count"]) + 1
                continue
            remove_class(desired_classes, anchor_class)
            remove_class(desired_classes, companion_class, companion_count)

    def place_assets(
        self,
        rooms: list[ProceduralRoom],
        scene_index: int,
        rng: random.Random,
    ) -> tuple[list[PlacedAsset], list[dict[str, object]], dict[str, object]]:
        placements: list[PlacedAsset] = []
        skipped: list[dict[str, object]] = []
        desired_object_counts: dict[str, int] = {}
        stats: dict[str, object] = {
            "attempt_count": 0,
            "exact_bbox_count": 0,
            "size_reject_count": 0,
            "approx_collision_reject_count": 0,
            "exact_room_reject_count": 0,
            "exact_collision_reject_count": 0,
            "relation_group_attempt_count": 0,
            "relation_group_success_count": 0,
            "relation_group_failure_count": 0,
            "relation_group_placement_count": 0,
            "relation_group_name_counts": {},
            "desired_object_counts": desired_object_counts,
            "policy_zone_counts": {},
        }
        room_boxes: dict[str, list[tuple[float, float, float, float, float, float]]] = {room.room_id: [] for room in rooms}
        mesh_cache: dict[Path, list[Vec3]] = {}
        for room in rooms:
            desired_classes = self.desired_classes_for_room(room, rng)
            desired_object_counts[room.room_id] = len(desired_classes)
            self._place_room_groups(
                room,
                desired_classes,
                scene_index,
                rng,
                room_boxes[room.room_id],
                placements,
                mesh_cache,
                stats,
            )
            for class_name in desired_classes:
                candidates = self._entries_for_room(room.room_type, class_name)
                if not candidates:
                    skipped.append({"room_id": room.room_id, "class": class_name, "reason": "empty_asset_pool"})
                    continue
                placed = False
                for _attempt in range(int(self.args.procedural_max_attempts_per_object)):
                    stats["attempt_count"] = int(stats["attempt_count"]) + 1
                    entry = rng.choice(candidates)
                    margin = float(self.args.procedural_wall_margin_m)
                    policy = placement_policy_for_class(self.args.procedural_placement_policy, class_name)
                    yaw, center_x, center_y, width, length, zone = candidate_pose_for_policy(room, entry, policy, margin, rng)
                    if width >= room.width - 2.0 * margin or length >= room.length - 2.0 * margin:
                        stats["size_reject_count"] = int(stats["size_reject_count"]) + 1
                        continue
                    approx_bbox = procedural_asset_approx_bbox(entry, yaw, center_x, center_y)
                    if any(
                        boxes_overlap_xy(approx_bbox, other, float(self.args.procedural_object_margin_m))
                        for other in room_boxes[room.room_id]
                    ):
                        stats["approx_collision_reject_count"] = int(stats["approx_collision_reject_count"]) + 1
                        continue
                    stats["exact_bbox_count"] = int(stats["exact_bbox_count"]) + 1
                    matrix, bbox = procedural_asset_transform_for_center(entry, yaw, center_x, center_y, mesh_cache)
                    if not room_contains_bbox(room, bbox, margin=0.0):
                        stats["exact_room_reject_count"] = int(stats["exact_room_reject_count"]) + 1
                        continue
                    if any(boxes_overlap_xy(bbox, other, float(self.args.procedural_object_margin_m)) for other in room_boxes[room.room_id]):
                        stats["exact_collision_reject_count"] = int(stats["exact_collision_reject_count"]) + 1
                        continue
                    self._append_placement(
                        placements,
                        scene_index=scene_index,
                        room=room,
                        entry=entry,
                        matrix=matrix,
                        bbox=bbox,
                        yaw=yaw,
                    )
                    room_boxes[room.room_id].append(bbox)
                    zone_counts = stats["policy_zone_counts"]
                    if isinstance(zone_counts, dict):
                        zone_counts[zone] = int(zone_counts.get(zone, 0)) + 1
                    placed = True
                    break
                if not placed:
                    skipped.append({"room_id": room.room_id, "class": class_name, "reason": "placement_failed"})
        return placements, skipped, stats

    def build_scene(self, scene_dir: Path, scene_index: int, rng: random.Random) -> ProceduralSceneBuild:
        total_start = time.perf_counter()
        timings: dict[str, float] = {}
        stage_start = time.perf_counter()
        rooms = make_room_layout(
            rng,
            str(self.args.procedural_layout),
            tuple(int(value) for value in self.args.procedural_room_count),
            tuple(float(value) for value in self.args.procedural_room_width_m),
            tuple(float(value) for value in self.args.procedural_room_length_m),
            tuple(float(value) for value in self.args.procedural_room_height_m),
            tuple(str(value) for value in self.args.procedural_room_types),
        )
        timings["layout"] = time.perf_counter() - stage_start
        stage_start = time.perf_counter()
        meshes, room_children = architecture_meshes_for_rooms(
            rooms,
            wall_thickness=float(self.args.procedural_wall_thickness_m),
            door_width=float(self.args.procedural_door_width_m),
        )
        scene_id = f"procedural_{scene_index:04d}_{rng.randrange(1, 2**31):08x}"
        timings["architecture_mesh"] = time.perf_counter() - stage_start
        stage_start = time.perf_counter()
        architecture_obj, source_json, metadata_json, bbox_min, bbox_max = write_procedural_source_files(
            scene_dir,
            scene_id,
            rooms,
            meshes,
            room_children,
        )
        timings["write_procedural_source"] = time.perf_counter() - stage_start
        base_scene = Front3DBaseScene(
            scene_id=scene_id,
            scene_obj=architecture_obj,
            source_scene_json=source_json,
            metadata_json=metadata_json,
            bbox_min=bbox_min,
            bbox_max=bbox_max,
            source_bbox_min=bbox_min,
            source_bbox_max=bbox_max,
            world_offset=(0.0, 0.0, 0.0),
            source_to_sionna_material={"floor": "itu-concrete", "ceiling": "itu-concrete", "wall": "itu-concrete"},
            sionna_material_names=("itu-concrete",),
        )
        stage_start = time.perf_counter()
        placements, skipped, placement_stats = self.place_assets(rooms, scene_index, rng)
        timings["placement"] = time.perf_counter() - stage_start
        timings["total"] = time.perf_counter() - total_start
        report = {
            "scene_id": scene_id,
            "layout": str(self.args.procedural_layout),
            "room_count": len(rooms),
            "rooms": [
                self._room_report(
                    room,
                    desired_object_count=dict(placement_stats.get("desired_object_counts") or {}).get(room.room_id),
                )
                for room in rooms
            ],
            "asset_pool_counts": {key: len(value) for key, value in sorted(self.asset_pool.items())},
            "skipped_object_count": len(skipped),
            "skipped_objects": skipped,
            "placement_stats": placement_stats,
            "timings_s": {key: round(value, 6) for key, value in timings.items()},
        }
        return ProceduralSceneBuild(
            scene_id=scene_id,
            base_scene=base_scene,
            rooms=rooms,
            placements=placements,
            skipped_objects=skipped,
            generation_report=report,
        )

    def _room_report(self, room: ProceduralRoom, desired_object_count: int | None = None) -> dict[str, object]:
        profile_name, profile_classes, profile_filters = self.room_profile_detail_for_room(room.room_type)
        return {
            "room_id": room.room_id,
            "room_type": room.room_type,
            "profile": profile_name,
            "profile_classes": profile_classes,
            "profile_filters": profile_filters,
            "desired_object_count": desired_object_count,
            "bounds_xy": [room.x0, room.y0, room.x1, room.y1],
            "area_m2": round(room.area, 3),
        }
