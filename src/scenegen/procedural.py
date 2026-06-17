from __future__ import annotations

import json
import math
import random
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


def make_room_layout(
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
    rooms: list[ProceduralRoom] = []
    room_index = 0
    y0 = 0.0
    for row in range(rows):
        x0 = 0.0
        for col in range(columns):
            if room_index >= room_count:
                break
            room_type = room_types[room_index % len(room_types)] if room_types else "Room"
            rooms.append(
                ProceduralRoom(
                    room_id=f"proc_room_{room_index:02d}",
                    room_type=room_type,
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

    # Internal boundaries are split into two wall segments with a door-sized gap.
    xs = sorted({room.x0 for room in rooms} | {room.x1 for room in rooms})
    ys = sorted({room.y0 for room in rooms} | {room.y1 for room in rooms})
    for x in xs[1:-1]:
        intervals = [(room.y0, room.y1) for room in rooms if room.x0 < x < room.x1 or abs(room.x0 - x) < 1e-6 or abs(room.x1 - x) < 1e-6]
        for y0, y1 in sorted(set(intervals)):
            length = y1 - y0
            gap = min(door_width, max(0.0, length - 2.0 * wall_thickness))
            gap_y0 = y0 + (length - gap) / 2.0
            gap_y1 = gap_y0 + gap
            if gap_y0 > y0:
                wall_specs.append((x - wall_thickness / 2.0, y0, x + wall_thickness / 2.0, gap_y0, "Wall"))
            if gap_y1 < y1:
                wall_specs.append((x - wall_thickness / 2.0, gap_y1, x + wall_thickness / 2.0, y1, "Wall"))
            door_vertices, door_faces = plane_mesh(x - wall_thickness / 2.0, gap_y0, 0.0, x + wall_thickness / 2.0, gap_y1)
            meshes.append(mesh_payload(f"door_vertical_{len(meshes):04d}", "Door", door_vertices, door_faces))
    for y in ys[1:-1]:
        intervals = [(room.x0, room.x1) for room in rooms if room.y0 < y < room.y1 or abs(room.y0 - y) < 1e-6 or abs(room.y1 - y) < 1e-6]
        for x0, x1 in sorted(set(intervals)):
            length = x1 - x0
            gap = min(door_width, max(0.0, length - 2.0 * wall_thickness))
            gap_x0 = x0 + (length - gap) / 2.0
            gap_x1 = gap_x0 + gap
            if gap_x0 > x0:
                wall_specs.append((x0, y - wall_thickness / 2.0, gap_x0, y + wall_thickness / 2.0, "Wall"))
            if gap_x1 < x1:
                wall_specs.append((gap_x1, y - wall_thickness / 2.0, x1, y + wall_thickness / 2.0, "Wall"))
            door_vertices, door_faces = plane_mesh(gap_x0, y - wall_thickness / 2.0, 0.0, gap_x1, y + wall_thickness / 2.0)
            meshes.append(mesh_payload(f"door_horizontal_{len(meshes):04d}", "Door", door_vertices, door_faces))

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
        text = room_type.lower()
        if "bed" in text and class_name == "floor":
            filtered = [entry for entry in entries if "bed" in str(entry.semantic.get("super_category", "")).lower()]
            return filtered or entries
        if "dining" in text and class_name == "table":
            filtered = [entry for entry in entries if "dining" in str(entry.semantic.get("category", "")).lower()]
            return filtered or entries
        if "living" in text and class_name == "seat":
            filtered = [entry for entry in entries if "sofa" in str(entry.semantic.get("super_category", "")).lower()]
            return filtered or entries
        return entries

    def desired_classes_for_room(self, room_type: str, rng: random.Random) -> list[str]:
        text = room_type.lower()
        if "bed" in text:
            base = ["floor", "table", "table", "seat"]
        elif "dining" in text:
            base = ["table", "seat", "seat", "seat", "seat"]
        elif "study" in text:
            base = ["table", "seat", "floor"]
        else:
            base = ["seat", "table", "floor", "seat", "table"]
        extra_count = rng.randint(self.args.procedural_objects_per_room[0], self.args.procedural_objects_per_room[1])
        extras = [rng.choice(["table", "seat", "floor"]) for _ in range(max(0, extra_count - len(base)))]
        return base[:extra_count] + extras

    def place_assets(self, rooms: list[ProceduralRoom], scene_index: int, rng: random.Random) -> tuple[list[PlacedAsset], list[dict[str, object]]]:
        placements: list[PlacedAsset] = []
        skipped: list[dict[str, object]] = []
        room_boxes: dict[str, list[tuple[float, float, float, float, float, float]]] = {room.room_id: [] for room in rooms}
        mesh_cache: dict[Path, list[Vec3]] = {}
        for room in rooms:
            for class_name in self.desired_classes_for_room(room.room_type, rng):
                candidates = self._entries_for_room(room.room_type, class_name)
                if not candidates:
                    skipped.append({"room_id": room.room_id, "class": class_name, "reason": "empty_asset_pool"})
                    continue
                placed = False
                for _attempt in range(int(self.args.procedural_max_attempts_per_object)):
                    entry = rng.choice(candidates)
                    yaw = rng.choice([0.0, math.pi / 2.0, math.pi, math.pi * 1.5])
                    local_bbox = procedural_asset_bbox(entry, yaw, mesh_cache)
                    width = local_bbox[1] - local_bbox[0]
                    length = local_bbox[3] - local_bbox[2]
                    margin = float(self.args.procedural_wall_margin_m)
                    if width >= room.width - 2.0 * margin or length >= room.length - 2.0 * margin:
                        continue
                    center_x = rng.uniform(room.x0 + margin + width / 2.0, room.x1 - margin - width / 2.0)
                    center_y = rng.uniform(room.y0 + margin + length / 2.0, room.y1 - margin - length / 2.0)
                    matrix, bbox = procedural_asset_transform_for_center(entry, yaw, center_x, center_y, mesh_cache)
                    if not room_contains_bbox(room, bbox, margin=0.0):
                        continue
                    if any(boxes_overlap_xy(bbox, other, float(self.args.procedural_object_margin_m)) for other in room_boxes[room.room_id]):
                        continue
                    placement_index = len(placements)
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
                            metadata={
                                "generator": "procedural_front3d",
                                "room_type": room.room_type,
                                "category": entry.semantic.get("category"),
                                "super_category": entry.semantic.get("super_category"),
                                "material": entry.semantic.get("material"),
                                "object_variant": self.index.config.object_variant,
                            },
                        )
                    )
                    room_boxes[room.room_id].append(bbox)
                    placed = True
                    break
                if not placed:
                    skipped.append({"room_id": room.room_id, "class": class_name, "reason": "placement_failed"})
        return placements, skipped

    def build_scene(self, scene_dir: Path, scene_index: int, rng: random.Random) -> ProceduralSceneBuild:
        rooms = make_room_layout(
            rng,
            tuple(int(value) for value in self.args.procedural_room_count),
            tuple(float(value) for value in self.args.procedural_room_width_m),
            tuple(float(value) for value in self.args.procedural_room_length_m),
            tuple(float(value) for value in self.args.procedural_room_height_m),
            tuple(str(value) for value in self.args.procedural_room_types),
        )
        meshes, room_children = architecture_meshes_for_rooms(
            rooms,
            wall_thickness=float(self.args.procedural_wall_thickness_m),
            door_width=float(self.args.procedural_door_width_m),
        )
        scene_id = f"procedural_{scene_index:04d}_{rng.randrange(1, 2**31):08x}"
        architecture_obj, source_json, metadata_json, bbox_min, bbox_max = write_procedural_source_files(
            scene_dir,
            scene_id,
            rooms,
            meshes,
            room_children,
        )
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
        placements, skipped = self.place_assets(rooms, scene_index, rng)
        report = {
            "scene_id": scene_id,
            "room_count": len(rooms),
            "rooms": [
                {
                    "room_id": room.room_id,
                    "room_type": room.room_type,
                    "bounds_xy": [room.x0, room.y0, room.x1, room.y1],
                    "area_m2": round(room.area, 3),
                }
                for room in rooms
            ],
            "asset_pool_counts": {key: len(value) for key, value in sorted(self.asset_pool.items())},
            "skipped_object_count": len(skipped),
            "skipped_objects": skipped,
        }
        return ProceduralSceneBuild(
            scene_id=scene_id,
            base_scene=base_scene,
            rooms=rooms,
            placements=placements,
            skipped_objects=skipped,
            generation_report=report,
        )
