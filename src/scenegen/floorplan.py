from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from PIL import Image, ImageDraw, ImageFilter

from .geometry import load_obj_mesh, transform_vertices
from .models import Front3DBaseScene, Front3DOpeningConfig, PlacedAsset, Rect2D
from .paths import portable_path


SUPPORTED_INPUTS = {".glb", ".gltf", ".obj", ".ply", ".stl"}
DEFAULT_BIN_SIZE = 0.05
FIXED_ORIGIN_XY = np.array([0.0, 0.0], dtype=np.float64)
CLASS_MASK_LABELS: dict[int, str] = {
    0: "outdoor",
    1: "wall",
    2: "free_space",
    3: "furniture",
}
CLASS_MASK_COLORS: dict[int, tuple[int, int, int]] = {
    0: (245, 245, 245),
    1: (50, 50, 50),
    2: (104, 179, 232),
    3: (220, 139, 55),
}


def floorplan_layer_filename(level_m: float) -> str:
    height_token = f"{level_m:.2f}".replace("-", "m").replace(".", "p")
    return f"floorplan_{height_token}.png"


def world_to_pixel(point: tuple[float, float], min_x: float, max_y: float, resolution: float) -> tuple[int, int]:
    x, y = point
    return int(round((x - min_x) / resolution)), int(round((max_y - y) / resolution))


@dataclass(frozen=True)
class FloorplanConfig:
    enabled: bool
    geometry_enabled: bool
    geometry_projection: str
    class_mask_enabled: bool
    class_mask_wall_dilation_m: float
    class_mask_furniture_dilation_m: float
    class_mask_furniture_mode: str
    class_mask_furniture_height_m: float | None
    openings: Front3DOpeningConfig
    resolution_m_per_pixel: float
    height_mode: str
    heights_m: list[float]
    step_m: float
    top_z_m: float | None
    bottom_z_m: float
    sample_density_scale: float
    min_sample_points: int
    max_sample_points: int
    preview_tile_size_px: int
    fail_on_error: bool

    @classmethod
    def from_mapping(cls, payload: dict[str, Any], openings: dict[str, Any] | Front3DOpeningConfig | None = None) -> FloorplanConfig:
        geometry = payload["geometry"]
        class_mask = payload["class_mask"]
        height = geometry["height"]
        sampling = payload["sampling"]
        preview = payload["preview"]
        return cls(
            enabled=bool(payload["enabled"]),
            geometry_enabled=bool(geometry["enabled"]),
            geometry_projection=str(geometry["projection"]),
            class_mask_enabled=bool(class_mask["enabled"]),
            class_mask_wall_dilation_m=float(class_mask["wall_dilation_m"]),
            class_mask_furniture_dilation_m=float(class_mask["furniture_dilation_m"]),
            class_mask_furniture_mode=str(class_mask["furniture_mode"]),
            class_mask_furniture_height_m=(
                None if class_mask["furniture_height_m"] is None else float(class_mask["furniture_height_m"])
            ),
            openings=floorplan_opening_config_from_mapping(openings),
            resolution_m_per_pixel=float(payload["resolution_m"]),
            height_mode=str(height["mode"]),
            heights_m=[float(height_value) for height_value in height["values_m"]],
            step_m=float(height["step_m"]),
            top_z_m=None if height["top_m"] is None else float(height["top_m"]),
            bottom_z_m=float(height["bottom_m"]),
            sample_density_scale=float(sampling["density_scale"]),
            min_sample_points=int(sampling["min_points"]),
            max_sample_points=int(sampling["max_points"]),
            preview_tile_size_px=int(preview["tile_size_px"]),
            fail_on_error=bool(payload["fail_on_error"]),
        )


def floorplan_opening_config_from_mapping(payload: dict[str, Any] | Front3DOpeningConfig | None) -> Front3DOpeningConfig:
    if payload is None:
        return Front3DOpeningConfig()
    if isinstance(payload, Front3DOpeningConfig):
        return payload
    return Front3DOpeningConfig(
        mode=str(payload["mode"]),
        dilation_m=float(payload["dilation_m"]),
        floor_tolerance_m=float(payload["floor_tolerance_m"]),
        min_height_m=float(payload["min_height_m"]),
        include_doors_as_wall=bool(payload["include_doors_as_wall"]),
        include_windows_as_wall=bool(payload["include_windows_as_wall"]),
    )


def generate_floorplan_for_scene(
    scene_obj: Path,
    output_dir: Path,
    config: FloorplanConfig,
    placements: list[PlacedAsset] | None = None,
    bounds_xy: tuple[float, float, float, float] | None = None,
    forbidden_xy_rects: tuple[Rect2D, ...] = (),
    front3d_base_scene: Front3DBaseScene | None = None,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    path_root = scene_obj.parent
    record: dict[str, object] = {
        "scene": scene_obj.parent.name,
        "source": portable_path(scene_obj, path_root),
        "output_dir": portable_path(output_dir, path_root),
        "ok": True,
    }
    timings_s: dict[str, float] = {}

    if config.geometry_enabled:
        if scene_obj.suffix.lower() not in SUPPORTED_INPUTS:
            raise ValueError(f"Unsupported floorplan input: {scene_obj}")
        preview_path = output_dir / "preview.png"
        side_view_path = output_dir / "side_view.png"
        start = time.perf_counter()
        geometry_record = process_scene(
            input_path=scene_obj,
            output_dir=output_dir,
            resolution=config.resolution_m_per_pixel,
            step=config.step_m,
            manual_top_z=config.top_z_m,
            bottom_z=config.bottom_z_m,
            sample_density_scale=config.sample_density_scale,
            min_sample_points=config.min_sample_points,
            max_sample_points=config.max_sample_points,
            preview_output_path=preview_path,
            side_view_output_path=side_view_path,
            preview_tile_size=config.preview_tile_size_px,
            height_mode=config.height_mode,
            heights_m=config.heights_m,
            projection_mode=config.geometry_projection,
        )
        timings_s["floorplan_geometry"] = round(time.perf_counter() - start, 6)
        record.update(
            {
                "num_levels": geometry_record["num_levels"],
                "top_z_m": geometry_record["top_z_m"],
                "bottom_z_m": geometry_record["bottom_z_m"],
                "step_m": geometry_record["step_m"],
                "height_mode": geometry_record["height_mode"],
                "z_levels_m": geometry_record["z_levels_m"],
                "preview": portable_path(preview_path, path_root),
                "side_view": portable_path(side_view_path, path_root),
                "stack": portable_path(output_dir / "stack.npz", path_root),
                "meta": portable_path(output_dir / "meta.json", path_root),
                "geometry": {
                    **geometry_record,
                    "preview": portable_path(preview_path, path_root),
                    "side_view": portable_path(side_view_path, path_root),
                    "stack": portable_path(output_dir / "stack.npz", path_root),
                    "meta": portable_path(output_dir / "meta.json", path_root),
                },
            }
        )

    if config.class_mask_enabled:
        if front3d_base_scene is None:
            raise ValueError("Front3D class mask requires a front3d base scene.")
        if placements is None:
            raise ValueError("Front3D class mask requires placements.")
        start = time.perf_counter()
        class_mask = generate_front3d_class_mask(
            output_dir=output_dir,
            base_scene=front3d_base_scene,
            placements=placements,
            resolution=config.resolution_m_per_pixel,
            wall_dilation_m=config.class_mask_wall_dilation_m,
            furniture_dilation_m=config.class_mask_furniture_dilation_m,
            furniture_mode=config.class_mask_furniture_mode,
            furniture_height_m=config.class_mask_furniture_height_m,
            opening_mode=config.openings.mode,
            opening_dilation_m=config.openings.dilation_m,
            opening_floor_tolerance_m=config.openings.floor_tolerance_m,
            opening_min_height_m=config.openings.min_height_m,
            include_doors_as_wall=config.openings.include_doors_as_wall,
            include_windows_as_wall=config.openings.include_windows_as_wall,
            path_root=path_root,
        )
        timings_s["class_mask"] = round(time.perf_counter() - start, 6)
        record["class_mask"] = class_mask
        record["class_mask_preview"] = class_mask["preview"]
    if timings_s:
        record["timings_s"] = timings_s
    return record


def generate_front3d_class_mask(
    output_dir: Path,
    base_scene: Front3DBaseScene,
    placements: list[PlacedAsset],
    resolution: float,
    wall_dilation_m: float,
    furniture_dilation_m: float,
    furniture_mode: str,
    furniture_height_m: float | None,
    opening_mode: str,
    opening_dilation_m: float,
    opening_floor_tolerance_m: float,
    opening_min_height_m: float,
    include_doors_as_wall: bool,
    include_windows_as_wall: bool,
    path_root: Path | None = None,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    root = path_root or output_dir
    scene_data = json.loads(base_scene.source_scene_json.read_text(encoding="utf-8"))
    metadata = json.loads(base_scene.metadata_json.read_text(encoding="utf-8"))
    variant = str(metadata.get("variant") or "normalized")

    min_x = 0.0
    min_y = 0.0
    max_x = max(base_scene.bbox_max[0], *(placement.max_x for placement in placements), resolution)
    max_y = max(base_scene.bbox_max[1], *(placement.max_y for placement in placements), resolution)
    width_px = max(1, int(math.ceil((max_x - min_x) / resolution)) + 1)
    height_px = max(1, int(math.ceil((max_y - min_y) / resolution)) + 1)
    image_size = (width_px, height_px)

    floor_image = Image.new("L", image_size, 0)
    wall_image = Image.new("L", image_size, 0)
    opening_image = Image.new("L", image_size, 0)
    floor_draw = ImageDraw.Draw(floor_image)
    wall_draw = ImageDraw.Draw(wall_image)
    opening_draw = ImageDraw.Draw(opening_image)
    mesh_type_counts: dict[str, int] = {}
    floor_mesh_count = 0
    wall_mesh_count = 0
    opening_mesh_count = 0
    opening_type_counts: dict[str, int] = {}

    for mesh in scene_data.get("mesh") or []:
        if not isinstance(mesh, dict):
            continue
        mesh_type = str(mesh.get("type") or "Unknown")
        mesh_type_counts[mesh_type] = mesh_type_counts.get(mesh_type, 0) + 1
        target_draw: ImageDraw.ImageDraw | None
        opening_kind = front3d_opening_kind(
            mesh,
            mesh_type,
            variant=variant,
            base_scene=base_scene,
            opening_mode=opening_mode,
            floor_tolerance_m=opening_floor_tolerance_m,
            min_height_m=opening_min_height_m,
        )
        if opening_kind is not None:
            opening_mesh_count += 1
            opening_type_counts[opening_kind] = opening_type_counts.get(opening_kind, 0) + 1
            draw_front3d_mesh_projection(
                opening_draw,
                mesh,
                variant=variant,
                base_scene=base_scene,
                min_x=min_x,
                max_y=max_y,
                resolution=resolution,
            )

        if "floor" in mesh_type.lower():
            target_draw = floor_draw
            floor_mesh_count += 1
        elif front3d_mesh_type_is_wall(
            mesh_type,
            include_doors_as_wall=include_doors_as_wall,
            include_windows_as_wall=include_windows_as_wall,
        ):
            if opening_kind is not None:
                target_draw = None
            else:
                target_draw = wall_draw
                wall_mesh_count += 1
        else:
            target_draw = None
        if target_draw is None:
            continue
        draw_front3d_mesh_projection(
            target_draw,
            mesh,
            variant=variant,
            base_scene=base_scene,
            min_x=min_x,
            max_y=max_y,
            resolution=resolution,
        )

    if opening_dilation_m > 0:
        opening_image = dilate_binary_image(opening_image, opening_dilation_m, resolution)
    opening_layer = np.asarray(opening_image, dtype=np.uint8) > 0
    wall_without_openings_layer = (np.asarray(wall_image, dtype=np.uint8) > 0) & ~opening_layer
    wall_image = Image.fromarray((wall_without_openings_layer.astype(np.uint8) * 255), mode="L")
    if wall_dilation_m > 0:
        wall_image = dilate_binary_image(wall_image, wall_dilation_m, resolution)

    furniture_image, furniture_meta = build_front3d_furniture_mask_image(
        placements=placements,
        image_size=image_size,
        min_x=min_x,
        min_y=min_y,
        max_y=max_y,
        resolution=resolution,
        mode=furniture_mode,
        height_m=furniture_height_m,
    )
    if furniture_dilation_m > 0:
        furniture_image = dilate_binary_image(furniture_image, furniture_dilation_m, resolution)

    floor_layer = np.asarray(floor_image, dtype=np.uint8) > 0
    wall_layer = np.asarray(wall_image, dtype=np.uint8) > 0
    furniture_layer = np.asarray(furniture_image, dtype=np.uint8) > 0
    indoor_layer = floor_layer | opening_layer
    class_mask = np.zeros((height_px, width_px), dtype=np.uint8)
    class_mask[indoor_layer] = 2
    class_mask[wall_layer] = 1
    class_mask[furniture_layer & indoor_layer & ~wall_layer] = 3

    mask_path = output_dir / "class_mask.png"
    preview_path = output_dir / "class_mask_preview.png"
    npy_path = output_dir / "class_mask.npy"
    npz_path = output_dir / "class_mask.npz"
    meta_path = output_dir / "class_mask_meta.json"
    Image.fromarray(class_mask, mode="L").save(mask_path)
    np.save(npy_path, class_mask)
    np.savez_compressed(
        npz_path,
        mask=class_mask,
        resolution_m_per_pixel=np.asarray([resolution], dtype=np.float32),
        origin_xy_m=np.asarray([min_x, min_y], dtype=np.float32),
        class_ids=np.asarray(sorted(CLASS_MASK_LABELS), dtype=np.uint8),
        class_names=np.asarray([CLASS_MASK_LABELS[index] for index in sorted(CLASS_MASK_LABELS)]),
    )
    preview = render_class_mask_preview(class_mask)
    preview.save(preview_path)

    class_counts = {CLASS_MASK_LABELS[index]: int((class_mask == index).sum()) for index in sorted(CLASS_MASK_LABELS)}
    class_id_counts = {str(index): int((class_mask == index).sum()) for index in sorted(CLASS_MASK_LABELS)}
    meta = {
        "type": "front3d_class_mask",
        "scene_id": base_scene.scene_id,
        "mask": portable_path(mask_path, root),
        "preview": portable_path(preview_path, root),
        "npy": portable_path(npy_path, root),
        "npz": portable_path(npz_path, root),
        "resolution_m_per_pixel": float(resolution),
        "origin_xy_m": [float(min_x), float(min_y)],
        "extent_xy_m": [float(max_x - min_x), float(max_y - min_y)],
        "grid_shape": [int(height_px), int(width_px)],
        "classes": {
            str(index): {"name": CLASS_MASK_LABELS[index], "color_rgb": list(CLASS_MASK_COLORS[index])}
            for index in sorted(CLASS_MASK_LABELS)
        },
        "class_counts": class_counts,
        "class_id_counts": class_id_counts,
        "priority": ["outdoor", "free_space", "furniture", "wall"],
        "opening_priority": "selected openings are marked as free_space before wall dilation; wall dilation may narrow or close them and no post-dilation restore is applied",
        "source_scene": portable_path(base_scene.source_scene_json, root),
        "source_metadata": portable_path(base_scene.metadata_json, root),
        "architecture_variant": variant,
        "world_offset": [float(value) for value in base_scene.world_offset],
        "floor_mesh_count": int(floor_mesh_count),
        "wall_mesh_count": int(wall_mesh_count),
        "opening_mesh_count": int(opening_mesh_count),
        "opening_type_counts": opening_type_counts,
        "furniture_count": len(placements),
        "furniture_mode": furniture_mode,
        "furniture_height_m": None if furniture_height_m is None else float(furniture_height_m),
        "furniture_mask": furniture_meta,
        "mesh_type_counts": mesh_type_counts,
        "wall_dilation_m": float(wall_dilation_m),
        "furniture_dilation_m": float(furniture_dilation_m),
        "opening_mode": opening_mode,
        "opening_dilation_m": float(opening_dilation_m),
        "opening_floor_tolerance_m": float(opening_floor_tolerance_m),
        "opening_min_height_m": float(opening_min_height_m),
        "include_doors_as_wall": bool(include_doors_as_wall),
        "include_windows_as_wall": bool(include_windows_as_wall),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "mask": portable_path(mask_path, root),
        "preview": portable_path(preview_path, root),
        "npy": portable_path(npy_path, root),
        "npz": portable_path(npz_path, root),
        "meta": portable_path(meta_path, root),
        "resolution_m_per_pixel": float(resolution),
        "grid_shape": [int(height_px), int(width_px)],
        "class_counts": class_counts,
        "class_id_counts": class_id_counts,
    }


def front3d_mesh_type_is_wall(
    mesh_type: str,
    *,
    include_doors_as_wall: bool,
    include_windows_as_wall: bool,
) -> bool:
    text = mesh_type.lower()
    if any(token in text for token in ("wall", "column", "pillar")):
        return True
    if include_doors_as_wall and "door" in text:
        return True
    if include_windows_as_wall and "window" in text:
        return True
    return False


def front3d_opening_kind(
    mesh: dict[str, Any],
    mesh_type: str,
    *,
    variant: str,
    base_scene: Front3DBaseScene,
    opening_mode: str,
    floor_tolerance_m: float,
    min_height_m: float,
) -> str | None:
    if opening_mode == "none":
        return None
    text = mesh_type.lower()
    allow_doors = opening_mode in {"doors", "doors_and_windows"}
    allow_windows = opening_mode in {"windows", "doors_and_windows"}
    z_min, z_max = front3d_mesh_z_bounds(mesh, variant, base_scene)
    height = z_max - z_min

    if allow_doors and "door" in text:
        return "door"
    if allow_windows and ("window" in text or "baywindow" in text):
        return "window"
    if "hole" in text or "pocket" in text:
        starts_at_floor = z_min <= floor_tolerance_m
        tall_enough = height >= min_height_m or z_max >= min_height_m
        if allow_doors and starts_at_floor and tall_enough:
            return "door_auxiliary"
        if allow_windows and not starts_at_floor and tall_enough:
            return "window_auxiliary"
    return None


def front3d_mesh_z_bounds(mesh: dict[str, Any], variant: str, base_scene: Front3DBaseScene) -> tuple[float, float]:
    vertices = front3d_architecture_vertices(mesh, variant, base_scene)
    if not vertices:
        return 0.0, 0.0
    z_values = [vertex[2] for vertex in vertices]
    return min(z_values), max(z_values)


def draw_front3d_mesh_projection(
    draw: ImageDraw.ImageDraw,
    mesh: dict[str, Any],
    *,
    variant: str,
    base_scene: Front3DBaseScene,
    min_x: float,
    max_y: float,
    resolution: float,
) -> None:
    vertices = front3d_architecture_vertices(mesh, variant, base_scene)
    faces = [int(value) for value in mesh.get("faces") or []]
    for index in range(0, len(faces), 3):
        if index + 2 >= len(faces):
            break
        a_i, b_i, c_i = faces[index], faces[index + 1], faces[index + 2]
        if min(a_i, b_i, c_i) < 0 or max(a_i, b_i, c_i) >= len(vertices):
            continue
        polygon = [
            world_to_pixel((vertices[a_i][0], vertices[a_i][1]), min_x, max_y, resolution),
            world_to_pixel((vertices[b_i][0], vertices[b_i][1]), min_x, max_y, resolution),
            world_to_pixel((vertices[c_i][0], vertices[c_i][1]), min_x, max_y, resolution),
        ]
        draw.polygon(polygon, fill=255)


def front3d_architecture_vertices(
    mesh: dict[str, Any],
    variant: str,
    base_scene: Front3DBaseScene,
) -> list[tuple[float, float, float]]:
    xyz = mesh.get("xyz") or []
    vertices: list[tuple[float, float, float]] = []
    for index in range(0, len(xyz), 3):
        if index + 2 >= len(xyz):
            break
        x, y, z = float(xyz[index]), float(xyz[index + 1]), float(xyz[index + 2])
        if variant == "normalized":
            point = (x, -z, y)
        else:
            point = (x, y, z)
        vertices.append(
            (
                point[0] + base_scene.world_offset[0],
                point[1] + base_scene.world_offset[1],
                point[2] + base_scene.world_offset[2],
            )
        )
    return vertices


def dilate_binary_image(image: Image.Image, dilation_m: float, resolution: float) -> Image.Image:
    radius_px = int(round(dilation_m / resolution))
    if radius_px <= 0:
        return image
    return image.filter(ImageFilter.MaxFilter(radius_px * 2 + 1))


def render_class_mask_preview(class_mask: np.ndarray) -> Image.Image:
    rgb = np.zeros((*class_mask.shape, 3), dtype=np.uint8)
    for class_id, color in CLASS_MASK_COLORS.items():
        rgb[class_mask == class_id] = color
    return Image.fromarray(rgb, mode="RGB")


def build_front3d_furniture_mask_image(
    placements: list[PlacedAsset],
    image_size: tuple[int, int],
    min_x: float,
    min_y: float,
    max_y: float,
    resolution: float,
    mode: str,
    height_m: float | None,
) -> tuple[Image.Image, dict[str, object]]:
    if mode == "bbox":
        image = Image.new("L", image_size, 0)
        draw = ImageDraw.Draw(image)
        for placement in placements:
            draw_bbox_furniture_mask(draw, placement, min_x, max_y, resolution)
        return image, {
            "mode": mode,
            "object_count": len(placements),
            "bbox_object_count": len(placements),
            "mesh_object_count": 0,
            "fallback_bbox_count": 0,
            "height_m": None if height_m is None else float(height_m),
        }
    if mode != "mesh":
        raise ValueError("furniture_mode must be 'bbox' or 'mesh'")
    return build_front3d_mesh_furniture_mask_image(
        placements=placements,
        image_size=image_size,
        min_x=min_x,
        min_y=min_y,
        max_y=max_y,
        resolution=resolution,
        height_m=height_m,
    )


def draw_bbox_furniture_mask(
    draw: ImageDraw.ImageDraw,
    placement: PlacedAsset,
    min_x: float,
    max_y: float,
    resolution: float,
) -> None:
    polygon = [
        world_to_pixel((placement.min_x, placement.min_y), min_x, max_y, resolution),
        world_to_pixel((placement.max_x, placement.min_y), min_x, max_y, resolution),
        world_to_pixel((placement.max_x, placement.max_y), min_x, max_y, resolution),
        world_to_pixel((placement.min_x, placement.max_y), min_x, max_y, resolution),
    ]
    draw.polygon(polygon, fill=255)


def build_front3d_mesh_furniture_mask_image(
    placements: list[PlacedAsset],
    image_size: tuple[int, int],
    min_x: float,
    min_y: float,
    max_y: float,
    resolution: float,
    height_m: float | None,
) -> tuple[Image.Image, dict[str, object]]:
    shape = (image_size[1], image_size[0])
    vertices_chunks: list[np.ndarray] = []
    triangle_faces: list[tuple[int, int, int]] = []
    mesh_cache: dict[Path, object] = {}
    fallback_image = Image.new("L", image_size, 0)
    fallback_draw = ImageDraw.Draw(fallback_image)
    failed_objects: list[dict[str, str]] = []
    vertex_offset = 0
    mesh_object_count = 0
    target_height = height_m

    for placement in placements:
        try:
            mesh = mesh_cache.get(placement.asset.obj_file)
            if mesh is None:
                mesh = load_obj_mesh(placement.asset.obj_file)
                mesh_cache[placement.asset.obj_file] = mesh
            transformed = np.asarray(transform_vertices(mesh, placement), dtype=np.float64)
            faces = triangulate_obj_faces(mesh.faces, vertex_offset)
        except Exception as exc:
            draw_bbox_furniture_mask(fallback_draw, placement, min_x, max_y, resolution)
            failed_objects.append(
                {
                    "instance": placement.instance_name,
                    "asset_obj": str(placement.asset.obj_file),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            continue
        if transformed.size == 0 or not faces:
            draw_bbox_furniture_mask(fallback_draw, placement, min_x, max_y, resolution)
            failed_objects.append(
                {
                    "instance": placement.instance_name,
                    "asset_obj": str(placement.asset.obj_file),
                    "error_type": "EmptyMesh",
                    "error": "object mesh has no usable vertices or faces",
                }
            )
            continue
        if height_m is None:
            object_max_z = float(np.max(transformed[:, 2]))
            target_height = object_max_z if target_height is None else max(target_height, object_max_z)
        vertices_chunks.append(transformed)
        triangle_faces.extend(faces)
        vertex_offset += len(transformed)
        mesh_object_count += 1

    if vertices_chunks and triangle_faces:
        vertices = np.vstack(vertices_chunks)
        faces_array = np.asarray(triangle_faces, dtype=np.int64)
        raster = rasterize_height_filtered_triangles(
            vertices=vertices,
            faces=faces_array,
            origin_xy=np.asarray([min_x, min_y], dtype=np.float64),
            resolution=resolution,
            shape=shape,
            z_levels=[float(target_height if target_height is not None else 0.0)],
            bottom_z=0.0,
        )
        mesh_layer = np.flipud((raster["stack"][0] > 0).astype(np.uint8)) * 255
        backend_meta = dict(raster["backend_meta"])
    else:
        mesh_layer = np.zeros(shape, dtype=np.uint8)
        backend_meta = {
            "method": "deterministic_height_filtered_triangle_raster",
            "triangle_count": 0,
            "painted_triangle_count": 0,
            "degenerate_triangle_count": 0,
            "z_rejected_triangle_count": 0,
            "bottom_z_m": 0.0,
            "max_target_height_m": float(target_height if target_height is not None else 0.0),
        }

    fallback_layer = np.asarray(fallback_image, dtype=np.uint8)
    combined_layer = np.maximum(mesh_layer, fallback_layer)
    image = Image.fromarray(combined_layer.astype(np.uint8), mode="L")
    return image, {
        "mode": "mesh",
        "object_count": len(placements),
        "bbox_object_count": 0,
        "mesh_object_count": int(mesh_object_count),
        "fallback_bbox_count": int(len(failed_objects)),
        "height_m": None if height_m is None else float(height_m),
        "effective_height_m": float(target_height if target_height is not None else 0.0),
        "failed_objects": failed_objects[:20],
        **backend_meta,
    }


def triangulate_obj_faces(faces: list[list[int]], vertex_offset: int = 0) -> list[tuple[int, int, int]]:
    triangles: list[tuple[int, int, int]] = []
    for face in faces:
        if len(face) < 3:
            continue
        first = int(face[0]) + vertex_offset
        for index in range(1, len(face) - 1):
            triangles.append((first, int(face[index]) + vertex_offset, int(face[index + 1]) + vertex_offset))
    return triangles


def process_scene(
    input_path: Path,
    output_dir: Path,
    resolution: float,
    step: float,
    manual_top_z: float | None,
    bottom_z: float,
    sample_density_scale: float,
    min_sample_points: int,
    max_sample_points: int,
    preview_output_path: Path,
    side_view_output_path: Path,
    preview_tile_size: int,
    height_mode: str = "layers",
    heights_m: list[float] | None = None,
    projection_mode: str = "sampling",
) -> dict[str, object]:
    if projection_mode not in {"sampling", "ray_height_filtered"}:
        raise ValueError("projection_mode must be 'sampling' or 'ray_height_filtered'")
    output_dir.mkdir(parents=True, exist_ok=True)
    mesh_path, conversion_meta = prepare_mesh_path(input_path, output_dir)
    path_root = input_path.parent
    mesh = load_as_mesh(mesh_path)

    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    vertical_axis = int(np.argmin(np.ptp(vertices, axis=0)))
    reorder = [axis for axis in range(3) if axis != vertical_axis] + [vertical_axis]
    oriented_vertices = vertices[:, reorder]
    oriented_mesh = trimesh.Trimesh(vertices=oriented_vertices, faces=mesh.faces, process=False)

    sampled_points: np.ndarray | None = None
    sample_meta: dict[str, object] | None = None
    if projection_mode == "sampling":
        sampled_points, _sampled_normals, sample_meta = sample_surface_points(
            oriented_mesh,
            resolution,
            sample_density_scale,
            min_sample_points,
            max_sample_points,
            include_normals=False,
        )
        all_points = np.vstack([oriented_vertices, sampled_points]).astype(np.float32, copy=False)
    else:
        all_points = oriented_vertices.astype(np.float32, copy=True)

    effective_bottom, effective_top, hist_meta = detect_effective_height_range(
        z_values=all_points[:, 2],
        bin_size=min(DEFAULT_BIN_SIZE, max(0.02, step / 2.0)),
    )

    shifted_vertices = oriented_vertices.copy()
    shifted_vertices[:, 2] -= effective_bottom
    shifted_points = all_points
    shifted_points[:, 2] -= effective_bottom

    xy_min, xy_max, origin_clipping_warning = compute_projection_bounds(
        points_xy=shifted_vertices[:, :2],
        resolution=resolution,
        scene_name=input_path.name,
    )
    width = int(np.ceil((xy_max[0] - xy_min[0]) / resolution)) + 1
    height = int(np.ceil((xy_max[1] - xy_min[1]) / resolution)) + 1
    shape = (height, width)

    detected_top_z = max(0.0, float(effective_top - effective_bottom))
    if height_mode == "layers":
        scan_top = float(manual_top_z) if manual_top_z is not None else detected_top_z
        scan_top = max(scan_top, bottom_z)
        z_levels = build_z_levels(top_z=scan_top, bottom_z=bottom_z, step=step)
    elif height_mode == "heights":
        z_levels = build_explicit_z_levels(heights_m or [], bottom_z=bottom_z)
        scan_top = max(z_levels)
    else:
        raise ValueError("height_mode must be 'layers' or 'heights'")

    side_view = render_side_projection(
        points=shifted_points,
        resolution=resolution,
        x_min=float(xy_min[0]),
        x_max=float(xy_max[0]),
        z_min=0.0,
        z_max=float(max(scan_top, float(shifted_points[:, 2].max()))),
    )
    side_view.save(side_view_output_path)

    if projection_mode == "sampling":
        assert sample_meta is not None
        rows, cols, z_vals = rasterize_points(
            points=shifted_points,
            origin_xy=xy_min,
            resolution=resolution,
            shape=shape,
        )
        pixel_min_z = build_pixel_min_height_map_from_raster(rows=rows, cols=cols, z_vals=z_vals, shape=shape)
        valid_projection = np.isfinite(pixel_min_z)
        projection_output = build_sampling_projection_output(
            rows=rows,
            cols=cols,
            z_vals=z_vals,
            z_levels=z_levels,
            shape=shape,
            output_dir=output_dir,
            path_root=path_root,
        )
        stack = projection_output["stack"]
        projection_stats = projection_output["projection_stats"]
        soft_plain_images_by_index = projection_output["images_by_index"]
        projection_backend_meta: dict[str, object] = {"sampling": sample_meta}
    else:
        projection_output = build_height_filtered_projection_output(
            mesh=oriented_mesh,
            vertices=shifted_vertices,
            origin_xy=xy_min,
            resolution=resolution,
            shape=shape,
            z_levels=z_levels,
            bottom_z=bottom_z,
            output_dir=output_dir,
            path_root=path_root,
        )
        stack = projection_output["stack"]
        pixel_min_z = projection_output["pixel_min_z"]
        valid_projection = np.isfinite(pixel_min_z)
        projection_stats = projection_output["projection_stats"]
        soft_plain_images_by_index = projection_output["images_by_index"]
        projection_backend_meta = {
            "ray_height_filtered": projection_output["backend_meta"],
            "sampling": None,
        }
    soft_plain_panels = [(f"z<={level:.2f}", soft_plain_images_by_index[index]) for index, level in enumerate(z_levels)]
    primary_layer_path = output_dir / floorplan_layer_filename(float(z_levels[0]))

    np.savez_compressed(
        output_dir / "stack.npz",
        stack=stack,
        z_levels_m=np.asarray(z_levels, dtype=np.float32),
        resolution_m_per_pixel=np.asarray([resolution], dtype=np.float32),
        origin_xy_m=np.asarray([float(xy_min[0]), float(xy_min[1])], dtype=np.float32),
        pixel_min_z_m=np.where(valid_projection, pixel_min_z, -1.0).astype(np.float32),
    )

    soft_plain_preview = build_contact_sheet(
        soft_plain_panels,
        title=f"scene: {input_path.name}",
        tile_size=preview_tile_size,
    )
    soft_plain_preview.save(preview_output_path)

    meta = {
        "approach": "height_sweep_projection",
        "projection_mode": projection_mode,
        "deterministic": projection_mode == "ray_height_filtered",
        "source": portable_path(input_path, path_root),
        "prepared_mesh": portable_path(mesh_path, path_root),
        "resolution_m_per_pixel": resolution,
        "patch_size_px_for_1m": int(round(1.0 / resolution)),
        "vertical_axis_original": axis_name(vertical_axis),
        "grid_shape": [int(height), int(width)],
        "origin_xy_m": [float(xy_min[0]), float(xy_min[1])],
        "extent_xy_m": [float(xy_max[0] - xy_min[0]), float(xy_max[1] - xy_min[1])],
        "effective_ground_m_original_units": float(effective_bottom),
        "effective_top_m_original_units": float(effective_top),
        "detected_top_z_m": float(detected_top_z),
        "height_mode": height_mode,
        "requested_heights_m": [float(level) for level in z_levels] if height_mode == "heights" else None,
        "scan_top_z_m": float(scan_top),
        "scan_bottom_z_m": float(bottom_z),
        "step_m": float(step),
        "num_levels": len(z_levels),
        "z_levels_m": [float(level) for level in z_levels],
        "origin_clipping_warning": origin_clipping_warning,
        "height_histogram": hist_meta,
        **projection_backend_meta,
        "primary_layer": portable_path(primary_layer_path, path_root),
        "conversion": conversion_meta,
        "projection_stats": projection_stats,
    }
    (output_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "scene": input_path.stem,
        "source": portable_path(input_path, path_root),
        "output_dir": portable_path(output_dir, path_root),
        "num_levels": len(z_levels),
        "top_z_m": float(scan_top),
        "bottom_z_m": float(bottom_z),
        "step_m": float(step),
        "height_mode": height_mode,
        "projection_mode": projection_mode,
        "z_levels_m": [float(level) for level in z_levels],
        "raw": portable_path(primary_layer_path, path_root),
    }


def build_sampling_projection_output(
    rows: np.ndarray,
    cols: np.ndarray,
    z_vals: np.ndarray,
    z_levels: list[float],
    shape: tuple[int, int],
    output_dir: Path,
    path_root: Path,
) -> dict[str, object]:
    stack = np.zeros((len(z_levels), shape[0], shape[1]), dtype=np.uint8)
    pixel_min_z = build_pixel_min_height_map_from_raster(rows=rows, cols=cols, z_vals=z_vals, shape=shape)
    valid_projection = np.isfinite(pixel_min_z)
    projection_stats: list[dict[str, object]] = []
    for index, level in enumerate(z_levels):
        mask = valid_projection & (pixel_min_z <= level)
        stack[index] = mask.astype(np.uint8)
        projection_stats.append(
            {
                "index": index,
                "z_level_m": float(level),
                "image": portable_path(output_dir / floorplan_layer_filename(float(level)), path_root),
                "occupied_pixels": int(mask.sum()),
            }
        )

    projection_render = render_projection_stack(
        rows=rows,
        cols=cols,
        z_vals=z_vals,
        z_levels=z_levels,
        shape=shape,
        output_dir=output_dir,
        max_alpha=0.90,
        foreground_color=(40, 40, 40),
    )
    return {
        "stack": stack,
        "projection_stats": projection_stats,
        "images_by_index": projection_render["raw_images"],
    }


def build_height_filtered_projection_output(
    mesh: trimesh.Trimesh,
    vertices: np.ndarray,
    origin_xy: np.ndarray,
    resolution: float,
    shape: tuple[int, int],
    z_levels: list[float],
    bottom_z: float,
    output_dir: Path,
    path_root: Path,
) -> dict[str, object]:
    faces = np.asarray(mesh.faces, dtype=np.int64)
    raster = rasterize_height_filtered_triangles(
        vertices=vertices,
        faces=faces,
        origin_xy=origin_xy,
        resolution=resolution,
        shape=shape,
        z_levels=z_levels,
        bottom_z=bottom_z,
    )
    stack = raster["stack"]
    densities = raster["densities"]
    pixel_min_z = raster["pixel_min_z"]

    projection_stats: list[dict[str, object]] = []
    images_by_index: dict[int, Image.Image] = {}
    for index, level in enumerate(z_levels):
        mask = stack[index] > 0
        image = render_density_projection(densities[index], max_alpha=0.90, foreground_color=(40, 40, 40))
        image.save(output_dir / floorplan_layer_filename(float(level)))
        images_by_index[index] = image
        projection_stats.append(
            {
                "index": index,
                "z_level_m": float(level),
                "image": portable_path(output_dir / floorplan_layer_filename(float(level)), path_root),
                "occupied_pixels": int(mask.sum()),
                "density_sum": float(densities[index].sum()),
            }
        )

    return {
        "stack": stack,
        "pixel_min_z": pixel_min_z,
        "projection_stats": projection_stats,
        "images_by_index": images_by_index,
        "backend_meta": raster["backend_meta"],
    }


def rasterize_height_filtered_triangles(
    vertices: np.ndarray,
    faces: np.ndarray,
    origin_xy: np.ndarray,
    resolution: float,
    shape: tuple[int, int],
    z_levels: list[float],
    bottom_z: float,
) -> dict[str, object]:
    densities = [np.zeros(shape, dtype=np.float32) for _ in z_levels]
    pixel_min_z = np.full(shape, np.inf, dtype=np.float32)
    max_level = max(z_levels)
    painted_triangle_count = 0
    degenerate_triangle_count = 0
    z_rejected_triangle_count = 0

    for face in faces:
        triangle = vertices[face]
        tri_z_min = float(np.min(triangle[:, 2]))
        tri_z_max = float(np.max(triangle[:, 2]))
        if tri_z_max < bottom_z or tri_z_min > max_level:
            z_rejected_triangle_count += 1
            continue
        painted = paint_height_filtered_triangle(
            triangle=triangle,
            origin_xy=origin_xy,
            resolution=resolution,
            shape=shape,
            z_levels=z_levels,
            bottom_z=bottom_z,
            max_level=max_level,
            pixel_min_z=pixel_min_z,
            densities=densities,
        )
        if painted:
            painted_triangle_count += 1
        else:
            degenerate_triangle_count += 1

    stack = np.zeros((len(z_levels), shape[0], shape[1]), dtype=np.uint8)
    for index, density in enumerate(densities):
        stack[index] = (density > 0).astype(np.uint8)
    return {
        "stack": stack,
        "densities": densities,
        "pixel_min_z": pixel_min_z,
        "backend_meta": {
            "method": "deterministic_height_filtered_triangle_raster",
            "triangle_count": int(len(faces)),
            "painted_triangle_count": int(painted_triangle_count),
            "degenerate_triangle_count": int(degenerate_triangle_count),
            "z_rejected_triangle_count": int(z_rejected_triangle_count),
            "bottom_z_m": float(bottom_z),
            "max_target_height_m": float(max_level),
        },
    }


def paint_height_filtered_triangle(
    triangle: np.ndarray,
    origin_xy: np.ndarray,
    resolution: float,
    shape: tuple[int, int],
    z_levels: list[float],
    bottom_z: float,
    max_level: float,
    pixel_min_z: np.ndarray,
    densities: list[np.ndarray],
) -> bool:
    xy = triangle[:, :2]
    area2 = cross_2d(xy[1] - xy[0], xy[2] - xy[0])
    if abs(area2) <= 1e-10:
        return paint_degenerate_height_filtered_triangle(
            triangle=triangle,
            origin_xy=origin_xy,
            resolution=resolution,
            shape=shape,
            z_levels=z_levels,
            bottom_z=bottom_z,
            max_level=max_level,
            pixel_min_z=pixel_min_z,
            densities=densities,
        )

    min_xy = np.min(xy, axis=0)
    max_xy = np.max(xy, axis=0)
    row_min, row_max, col_min, col_max = pixel_bbox_for_xy_bounds(min_xy, max_xy, origin_xy, resolution, shape, padding_px=1)
    if row_min > row_max or col_min > col_max:
        return False

    rows = np.arange(row_min, row_max + 1, dtype=np.int32)
    cols = np.arange(col_min, col_max + 1, dtype=np.int32)
    grid_cols, grid_rows = np.meshgrid(cols, rows)
    xs = origin_xy[0] + (grid_cols.astype(np.float64) + 0.5) * resolution
    ys = origin_xy[1] + (grid_rows.astype(np.float64) + 0.5) * resolution

    w0, w1, w2 = barycentric_weights(xs, ys, xy)
    inside = (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9)
    if not np.any(inside):
        return False
    z_values = w0[inside] * triangle[0, 2] + w1[inside] * triangle[1, 2] + w2[inside] * triangle[2, 2]
    hit_rows = grid_rows[inside].astype(np.int32, copy=False)
    hit_cols = grid_cols[inside].astype(np.int32, copy=False)
    return paint_height_filtered_samples(
        rows=hit_rows,
        cols=hit_cols,
        z_values=z_values.astype(np.float32, copy=False),
        z_levels=z_levels,
        bottom_z=bottom_z,
        max_level=max_level,
        pixel_min_z=pixel_min_z,
        densities=densities,
    )


def paint_degenerate_height_filtered_triangle(
    triangle: np.ndarray,
    origin_xy: np.ndarray,
    resolution: float,
    shape: tuple[int, int],
    z_levels: list[float],
    bottom_z: float,
    max_level: float,
    pixel_min_z: np.ndarray,
    densities: list[np.ndarray],
) -> bool:
    xy = triangle[:, :2]
    distances = np.array(
        [
            np.linalg.norm(xy[1] - xy[0]),
            np.linalg.norm(xy[2] - xy[1]),
            np.linalg.norm(xy[0] - xy[2]),
        ],
        dtype=np.float64,
    )
    edge_index = int(np.argmax(distances))
    edge_pairs = ((0, 1), (1, 2), (2, 0))
    start_i, end_i = edge_pairs[edge_index]
    start = xy[start_i]
    end = xy[end_i]
    z_min = max(float(np.min(triangle[:, 2])), bottom_z)
    z_max = float(np.max(triangle[:, 2]))
    if z_max < bottom_z or z_min > max_level:
        return False

    if distances[edge_index] <= 1e-10:
        row = int(math.floor((float(start[1]) - float(origin_xy[1])) / resolution))
        col = int(math.floor((float(start[0]) - float(origin_xy[0])) / resolution))
        if row < 0 or row >= shape[0] or col < 0 or col >= shape[1]:
            return False
        return paint_height_filtered_samples(
            rows=np.asarray([row], dtype=np.int32),
            cols=np.asarray([col], dtype=np.int32),
            z_values=np.asarray([z_min], dtype=np.float32),
            z_levels=z_levels,
            bottom_z=bottom_z,
            max_level=max_level,
            pixel_min_z=pixel_min_z,
            densities=densities,
        )

    min_xy = np.minimum(start, end) - resolution
    max_xy = np.maximum(start, end) + resolution
    row_min, row_max, col_min, col_max = pixel_bbox_for_xy_bounds(min_xy, max_xy, origin_xy, resolution, shape, padding_px=0)
    if row_min > row_max or col_min > col_max:
        return False
    rows = np.arange(row_min, row_max + 1, dtype=np.int32)
    cols = np.arange(col_min, col_max + 1, dtype=np.int32)
    grid_cols, grid_rows = np.meshgrid(cols, rows)
    xs = origin_xy[0] + (grid_cols.astype(np.float64) + 0.5) * resolution
    ys = origin_xy[1] + (grid_rows.astype(np.float64) + 0.5) * resolution
    distance = point_segment_distance(xs, ys, start, end)
    covered = distance <= resolution * 0.75
    if not np.any(covered):
        return False
    return paint_height_filtered_samples(
        rows=grid_rows[covered].astype(np.int32, copy=False),
        cols=grid_cols[covered].astype(np.int32, copy=False),
        z_values=np.full(int(covered.sum()), z_min, dtype=np.float32),
        z_levels=z_levels,
        bottom_z=bottom_z,
        max_level=max_level,
        pixel_min_z=pixel_min_z,
        densities=densities,
    )


def paint_height_filtered_samples(
    rows: np.ndarray,
    cols: np.ndarray,
    z_values: np.ndarray,
    z_levels: list[float],
    bottom_z: float,
    max_level: float,
    pixel_min_z: np.ndarray,
    densities: list[np.ndarray],
) -> bool:
    valid = (z_values >= bottom_z - 1e-7) & (z_values <= max_level + 1e-7)
    if not np.any(valid):
        return False
    rows = rows[valid]
    cols = cols[valid]
    z_values = np.maximum(z_values[valid], bottom_z).astype(np.float32, copy=False)
    np.minimum.at(pixel_min_z, (rows, cols), z_values)
    for index, level in enumerate(z_levels):
        level_hits = z_values <= level + 1e-7
        if np.any(level_hits):
            np.add.at(densities[index], (rows[level_hits], cols[level_hits]), 1.0)
    return True


def pixel_bbox_for_xy_bounds(
    min_xy: np.ndarray,
    max_xy: np.ndarray,
    origin_xy: np.ndarray,
    resolution: float,
    shape: tuple[int, int],
    padding_px: int,
) -> tuple[int, int, int, int]:
    col_min = int(math.floor((float(min_xy[0]) - float(origin_xy[0])) / resolution)) - padding_px
    col_max = int(math.floor((float(max_xy[0]) - float(origin_xy[0])) / resolution)) + padding_px
    row_min = int(math.floor((float(min_xy[1]) - float(origin_xy[1])) / resolution)) - padding_px
    row_max = int(math.floor((float(max_xy[1]) - float(origin_xy[1])) / resolution)) + padding_px
    col_min = max(0, col_min)
    row_min = max(0, row_min)
    col_max = min(shape[1] - 1, col_max)
    row_max = min(shape[0] - 1, row_max)
    return row_min, row_max, col_min, col_max


def barycentric_weights(x: np.ndarray, y: np.ndarray, triangle_xy: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x0, y0 = triangle_xy[0]
    x1, y1 = triangle_xy[1]
    x2, y2 = triangle_xy[2]
    denominator = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    w0 = ((y1 - y2) * (x - x2) + (x2 - x1) * (y - y2)) / denominator
    w1 = ((y2 - y0) * (x - x2) + (x0 - x2) * (y - y2)) / denominator
    w2 = 1.0 - w0 - w1
    return w0, w1, w2


def point_segment_distance(x: np.ndarray, y: np.ndarray, start: np.ndarray, end: np.ndarray) -> np.ndarray:
    seg_x = float(end[0] - start[0])
    seg_y = float(end[1] - start[1])
    length_sq = seg_x * seg_x + seg_y * seg_y
    if length_sq <= 1e-20:
        return np.hypot(x - float(start[0]), y - float(start[1]))
    t = ((x - float(start[0])) * seg_x + (y - float(start[1])) * seg_y) / length_sq
    t = np.clip(t, 0.0, 1.0)
    closest_x = float(start[0]) + t * seg_x
    closest_y = float(start[1]) + t * seg_y
    return np.hypot(x - closest_x, y - closest_y)


def cross_2d(a: np.ndarray, b: np.ndarray) -> float:
    return float(a[0] * b[1] - a[1] * b[0])


def prepare_mesh_path(input_path: Path, _output_dir: Path) -> tuple[Path, dict[str, object]]:
    suffix = input_path.suffix.lower()
    return input_path, {"source_type": suffix.lstrip("."), "conversion": "none"}


def compute_projection_bounds(
    points_xy: np.ndarray,
    resolution: float,
    scene_name: str,
) -> tuple[np.ndarray, np.ndarray, bool]:
    data_xy_min = points_xy.min(axis=0)
    data_xy_max = points_xy.max(axis=0)
    xy_min = FIXED_ORIGIN_XY.copy()
    origin_clipping_warning = bool(np.any(data_xy_min < xy_min))

    if origin_clipping_warning:
        print(
            f"[warn] {scene_name}: fixed projection origin ({xy_min[0]:.3f}, {xy_min[1]:.3f}) is above "
            f"mesh min ({data_xy_min[0]:.3f}, {data_xy_min[1]:.3f}); lower/left area will be clipped."
        )

    xy_max = np.maximum(data_xy_max, xy_min + resolution)
    return xy_min, xy_max, origin_clipping_warning


def load_as_mesh(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, force="scene")
    if isinstance(loaded, trimesh.Scene):
        meshes = [geom for geom in loaded.geometry.values() if isinstance(geom, trimesh.Trimesh)]
        if not meshes:
            raise RuntimeError(f"No mesh geometry found in {path.name}.")
        mesh = trimesh.util.concatenate(meshes) if len(meshes) > 1 else meshes[0].copy()
    else:
        mesh = loaded
    if not isinstance(mesh, trimesh.Trimesh):
        raise RuntimeError(f"Could not convert {path.name} to a single mesh.")
    if len(mesh.vertices) == 0:
        raise RuntimeError(f"Mesh {path.name} is empty.")
    return mesh


def sample_surface_points(
    mesh: trimesh.Trimesh,
    resolution: float,
    density_scale: float,
    min_sample_points: int,
    max_sample_points: int,
    include_normals: bool = False,
) -> tuple[np.ndarray, np.ndarray | None, dict[str, object]]:
    if density_scale <= 0:
        raise ValueError("sample_density_scale must be positive.")
    if min_sample_points < 1:
        raise ValueError("min_sample_points must be positive.")
    if max_sample_points < min_sample_points:
        raise ValueError("max_sample_points must be greater than or equal to min_sample_points.")

    estimated = int(mesh.area / max(resolution * resolution, 1e-6) * 0.35 * density_scale)
    sample_count = int(np.clip(estimated, min_sample_points, max_sample_points))
    samples, face_indices = trimesh.sample.sample_surface(mesh, sample_count)
    sample_normals = mesh.face_normals[face_indices].astype(np.float32, copy=False) if include_normals else None
    return samples.astype(np.float32, copy=False), sample_normals, {
        "surface_area_m2": float(mesh.area),
        "density_scale": float(density_scale),
        "estimated_points": int(estimated),
        "num_points": sample_count,
        "min_sample_points": int(min_sample_points),
        "max_sample_points": int(max_sample_points),
    }


def detect_effective_height_range(z_values: np.ndarray, bin_size: float) -> tuple[float, float, dict[str, object]]:
    z_min = float(z_values.min())
    z_max = float(z_values.max())
    if z_max <= z_min:
        return z_min, z_max, {
            "bin_size_m": float(bin_size),
            "count_threshold": 0,
            "effective_bin_count": 1,
        }

    edges = np.arange(z_min, z_max + bin_size, bin_size, dtype=np.float32)
    if edges.size < 2:
        edges = np.asarray([z_min, z_max], dtype=np.float32)
    hist, edges = np.histogram(z_values, bins=edges)
    count_threshold = max(24, int(math.ceil(hist.max() * 0.01)))
    valid_bins = np.flatnonzero(hist >= count_threshold)

    if valid_bins.size == 0:
        effective_bottom = float(np.quantile(z_values, 0.005))
        effective_top = float(np.quantile(z_values, 0.995))
        effective_bin_count = 0
    else:
        effective_bottom = float(edges[valid_bins[0]])
        effective_top = float(edges[valid_bins[-1] + 1])
        effective_bin_count = int(valid_bins.size)

    return effective_bottom, effective_top, {
        "bin_size_m": float(bin_size),
        "count_threshold": int(count_threshold),
        "effective_bin_count": effective_bin_count,
        "raw_min_z": z_min,
        "raw_max_z": z_max,
    }


def build_z_levels(top_z: float, bottom_z: float, step: float) -> list[float]:
    if step <= 0:
        raise ValueError("step must be positive.")
    if top_z < bottom_z:
        top_z = bottom_z

    levels: list[float] = []
    current = float(top_z)
    while current > bottom_z + 1e-9:
        levels.append(round(current, 6))
        current -= step
    if not levels or abs(levels[-1] - bottom_z) > 1e-6:
        levels.append(round(bottom_z, 6))
    return levels


def build_explicit_z_levels(heights_m: list[float], bottom_z: float) -> list[float]:
    if not heights_m:
        raise ValueError("heights_m must contain at least one height when height_mode is 'heights'.")
    levels: list[float] = []
    for height in heights_m:
        level = float(height)
        if level < bottom_z:
            raise ValueError("heights_m values must be greater than or equal to bottom_z.")
        levels.append(round(level, 6))
    return levels


def build_pixel_min_height_map(
    points: np.ndarray,
    origin_xy: np.ndarray,
    resolution: float,
    shape: tuple[int, int],
    paint_radius_px: int,
) -> np.ndarray:
    pixel_min_z = np.full(shape, np.inf, dtype=np.float32)
    cols = np.floor((points[:, 0] - origin_xy[0]) / resolution).astype(np.int32)
    rows = np.floor((points[:, 1] - origin_xy[1]) / resolution).astype(np.int32)
    z_values = points[:, 2].astype(np.float32, copy=False)

    for dy in range(-paint_radius_px, paint_radius_px + 1):
        for dx in range(-paint_radius_px, paint_radius_px + 1):
            rr = rows + dy
            cc = cols + dx
            valid = (rr >= 0) & (rr < shape[0]) & (cc >= 0) & (cc < shape[1])
            if not np.any(valid):
                continue
            np.minimum.at(pixel_min_z, (rr[valid], cc[valid]), z_values[valid])
    return pixel_min_z


def build_pixel_min_height_map_from_raster(
    rows: np.ndarray,
    cols: np.ndarray,
    z_vals: np.ndarray,
    shape: tuple[int, int],
) -> np.ndarray:
    pixel_min_z = np.full(shape, np.inf, dtype=np.float32)
    if rows.size:
        np.minimum.at(pixel_min_z, (rows, cols), z_vals)
    return pixel_min_z


def rasterize_points(
    points: np.ndarray,
    origin_xy: np.ndarray,
    resolution: float,
    shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    cols = np.floor((points[:, 0] - origin_xy[0]) / resolution).astype(np.int32)
    rows = np.floor((points[:, 1] - origin_xy[1]) / resolution).astype(np.int32)
    valid = (rows >= 0) & (rows < shape[0]) & (cols >= 0) & (cols < shape[1])
    return rows[valid], cols[valid], points[valid, 2].astype(np.float32, copy=False)


def render_projection_stack(
    rows: np.ndarray,
    cols: np.ndarray,
    z_vals: np.ndarray,
    z_levels: list[float],
    shape: tuple[int, int],
    output_dir: Path,
    max_alpha: float,
    foreground_color: tuple[int, int, int],
) -> dict[str, object]:
    return {
        "raw_images": render_soft_projection_stack(
            rows=rows,
            cols=cols,
            z_vals=z_vals,
            z_levels=z_levels,
            shape=shape,
            output_dir=output_dir,
            max_alpha=max_alpha,
            foreground_color=foreground_color,
        )
    }


def render_soft_projection_stack(
    rows: np.ndarray,
    cols: np.ndarray,
    z_vals: np.ndarray,
    z_levels: list[float],
    shape: tuple[int, int],
    output_dir: Path,
    max_alpha: float = 0.52,
    foreground_color: tuple[int, int, int] | None = None,
) -> dict[int, Image.Image]:
    if rows.size == 0:
        empty = render_density_projection(np.zeros(shape, dtype=np.float32), max_alpha=max_alpha, foreground_color=foreground_color)
        images_by_index = {}
        for index, level in enumerate(z_levels):
            image = empty.copy()
            image.save(output_dir / floorplan_layer_filename(float(level)))
            images_by_index[index] = image
        return images_by_index

    order = np.argsort(z_vals, kind="mergesort")
    rows_sorted = rows[order]
    cols_sorted = cols[order]
    z_sorted = z_vals[order]
    density = np.zeros(shape, dtype=np.float32)
    cursor = 0
    images_by_index: dict[int, Image.Image] = {}

    ascending_levels = sorted((float(level), index) for index, level in enumerate(z_levels))
    for level, original_index in ascending_levels:
        next_cursor = int(np.searchsorted(z_sorted, level, side="right"))
        if next_cursor > cursor:
            np.add.at(density, (rows_sorted[cursor:next_cursor], cols_sorted[cursor:next_cursor]), 1.0)
            cursor = next_cursor
        image = render_density_projection(density, max_alpha=max_alpha, foreground_color=foreground_color)
        image.save(output_dir / floorplan_layer_filename(level))
        images_by_index[original_index] = image
    return images_by_index


def render_side_projection(
    points: np.ndarray,
    resolution: float,
    x_min: float,
    x_max: float,
    z_min: float,
    z_max: float,
    max_alpha: float = 0.90,
    foreground_color: tuple[int, int, int] | None = None,
) -> Image.Image:
    if resolution <= 0:
        raise ValueError("resolution must be positive.")

    if x_max <= x_min:
        x_max = x_min + resolution
    if z_max <= z_min:
        z_max = z_min + resolution

    width = int(np.ceil((x_max - x_min) / resolution)) + 1
    height = int(np.ceil((z_max - z_min) / resolution)) + 1
    width = max(width, 1)
    height = max(height, 1)

    cols = np.floor((points[:, 0] - x_min) / resolution).astype(np.int32)
    rows = np.floor((points[:, 2] - z_min) / resolution).astype(np.int32)
    valid = (rows >= 0) & (rows < height) & (cols >= 0) & (cols < width)

    density = np.zeros((height, width), dtype=np.float32)
    if np.any(valid):
        np.add.at(density, (rows[valid], cols[valid]), 1.0)

    return render_density_projection(
        density=density,
        max_alpha=max_alpha,
        foreground_color=foreground_color if foreground_color is not None else (40, 40, 40),
    )


def render_density_projection(
    density: np.ndarray,
    max_alpha: float = 0.52,
    foreground_color: tuple[int, int, int] | None = None,
) -> Image.Image:
    fill_blur = density.astype(np.float32, copy=False)
    positive = fill_blur[fill_blur > 0]
    background = np.full(fill_blur.shape + (3,), 255, dtype=np.uint8)
    if positive.size == 0:
        return Image.fromarray(np.flipud(background))

    upper = float(np.quantile(positive, 0.992))
    upper = max(upper, float(positive.max()), 1.0)
    normalized = np.clip(np.log1p(fill_blur) / math.log1p(upper), 0.0, 1.0)

    foreground = np.array(foreground_color if foreground_color is not None else (150, 150, 150), dtype=np.float32)
    background_f = background.astype(np.float32)
    fill_alpha = np.power(normalized, 1.05) * max_alpha
    canvas = background_f * (1.0 - fill_alpha[..., None]) + foreground * fill_alpha[..., None]
    return Image.fromarray(np.flipud(canvas.astype(np.uint8)))


def build_contact_sheet(
    panels: list[tuple[str, Image.Image]],
    title: str | None = None,
    tile_size: int = 260,
) -> Image.Image:
    if not panels:
        return Image.new("RGB", (256, 256), (255, 255, 255))

    if tile_size <= 0:
        raise ValueError("preview tile size must be positive.")

    thumb_w = max(160, int(tile_size))
    thumb_h = max(160, int(tile_size))
    label_h = 24
    title_h = 32 if title else 0
    cols = 4
    rows = (len(panels) + cols - 1) // cols
    sheet = Image.new(
        "RGB",
        (cols * thumb_w, title_h + rows * (thumb_h + label_h)),
        (248, 248, 248),
    )
    draw = ImageDraw.Draw(sheet)

    if title:
        draw.text((10, 8), title, fill=(20, 20, 20))

    for index, (name, image) in enumerate(panels):
        col = index % cols
        row = index // cols
        x = col * thumb_w
        y = title_h + row * (thumb_h + label_h)

        thumb = image.copy()
        thumb.thumbnail((thumb_w - 10, thumb_h - 10))
        offset_x = x + (thumb_w - thumb.width) // 2
        offset_y = y + 4 + (thumb_h - thumb.height) // 2
        sheet.paste(thumb, (offset_x, offset_y))
        draw.rectangle([x, y, x + thumb_w - 1, y + thumb_h - 1], outline=(180, 180, 180), width=1)
        draw.text((x + 8, y + thumb_h + 4), name, fill=(20, 20, 20))

    return sheet


def axis_name(index: int) -> str:
    return "xyz"[index]
