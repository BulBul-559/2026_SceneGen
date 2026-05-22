from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from PIL import Image, ImageDraw

from .models import PlacedAsset, Rect2D
from .paths import portable_path


SUPPORTED_INPUTS = {".glb", ".gltf", ".obj", ".ply", ".stl"}
DEFAULT_BIN_SIZE = 0.05
FIXED_ORIGIN_XY = np.array([0.0, 0.0], dtype=np.float64)
SEMANTIC_COLORS: dict[str, tuple[int, int, int]] = {
    "table": (66, 135, 245),
    "seat": (240, 151, 57),
    "floor": (76, 175, 80),
    "tabletop": (156, 94, 181),
    "unknown": (120, 120, 120),
}
SEMANTIC_BACKGROUND = (252, 252, 248)
SEMANTIC_SCENE_OUTLINE = (40, 40, 40)
SEMANTIC_FORBIDDEN = (210, 64, 64)


def floorplan_layer_filename(level_m: float) -> str:
    height_token = f"{level_m:.2f}".replace("-", "m").replace(".", "p")
    return f"floorplan_{height_token}.png"


@dataclass(frozen=True)
class FloorplanConfig:
    enabled: bool
    geometry_enabled: bool
    geometry_clean_enabled: bool
    geometry_clean_min_density: float
    geometry_clean_min_neighbors: int
    geometry_clean_min_z_m: float
    geometry_clean_max_abs_normal_z: float
    geometry_clean_opening_px: int
    geometry_clean_closing_px: int
    semantic_enabled: bool
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
    semantic_padding_m: float
    semantic_draw_labels: bool
    fail_on_error: bool

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> FloorplanConfig:
        return cls(
            enabled=bool(payload["enabled"]),
            geometry_enabled=bool(payload["geometry_enabled"]),
            geometry_clean_enabled=bool(payload["geometry_clean_enabled"]),
            geometry_clean_min_density=float(payload["geometry_clean_min_density"]),
            geometry_clean_min_neighbors=int(payload["geometry_clean_min_neighbors"]),
            geometry_clean_min_z_m=float(payload["geometry_clean_min_z_m"]),
            geometry_clean_max_abs_normal_z=float(payload["geometry_clean_max_abs_normal_z"]),
            geometry_clean_opening_px=int(payload["geometry_clean_opening_px"]),
            geometry_clean_closing_px=int(payload["geometry_clean_closing_px"]),
            semantic_enabled=bool(payload["semantic_enabled"]),
            resolution_m_per_pixel=float(payload["resolution_m_per_pixel"]),
            height_mode=str(payload["height_mode"]),
            heights_m=[float(height) for height in payload["heights_m"]],
            step_m=float(payload["step_m"]),
            top_z_m=None if payload["top_z_m"] is None else float(payload["top_z_m"]),
            bottom_z_m=float(payload["bottom_z_m"]),
            sample_density_scale=float(payload["sample_density_scale"]),
            min_sample_points=int(payload["min_sample_points"]),
            max_sample_points=int(payload["max_sample_points"]),
            preview_tile_size_px=int(payload["preview_tile_size_px"]),
            semantic_padding_m=float(payload["semantic_padding_m"]),
            semantic_draw_labels=bool(payload["semantic_draw_labels"]),
            fail_on_error=bool(payload["fail_on_error"]),
        )


def generate_floorplan_for_scene(
    scene_obj: Path,
    output_dir: Path,
    config: FloorplanConfig,
    placements: list[PlacedAsset] | None = None,
    bounds_xy: tuple[float, float, float, float] | None = None,
    forbidden_xy_rects: tuple[Rect2D, ...] = (),
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    path_root = scene_obj.parent
    record: dict[str, object] = {
        "scene": scene_obj.parent.name,
        "source": portable_path(scene_obj, path_root),
        "output_dir": portable_path(output_dir, path_root),
        "ok": True,
    }

    if config.geometry_enabled:
        if scene_obj.suffix.lower() not in SUPPORTED_INPUTS:
            raise ValueError(f"Unsupported floorplan input: {scene_obj}")
        preview_path = output_dir / "preview.png"
        side_view_path = output_dir / "side_view.png"
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
            clean_enabled=config.geometry_clean_enabled,
            clean_min_density=config.geometry_clean_min_density,
            clean_min_neighbors=config.geometry_clean_min_neighbors,
            clean_min_z=config.geometry_clean_min_z_m,
            clean_max_abs_normal_z=config.geometry_clean_max_abs_normal_z,
            clean_opening_px=config.geometry_clean_opening_px,
            clean_closing_px=config.geometry_clean_closing_px,
        )
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

    if config.semantic_enabled:
        if placements is None or bounds_xy is None:
            raise ValueError("Semantic floorplan requires placements and scene bounds.")
        semantic = generate_semantic_floorplan(
            output_dir=output_dir,
            placements=placements,
            bounds_xy=bounds_xy,
            forbidden_xy_rects=forbidden_xy_rects,
            resolution=config.resolution_m_per_pixel,
            padding_m=config.semantic_padding_m,
            draw_labels=config.semantic_draw_labels,
            path_root=path_root,
        )
        record["semantic"] = semantic
        record["semantic_preview"] = semantic["image"]
    return record


def generate_semantic_floorplan(
    output_dir: Path,
    placements: list[PlacedAsset],
    bounds_xy: tuple[float, float, float, float],
    forbidden_xy_rects: tuple[Rect2D, ...],
    resolution: float,
    padding_m: float,
    draw_labels: bool,
    path_root: Path | None = None,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    root = path_root or output_dir
    scene_min_x, scene_min_y, scene_max_x, scene_max_y = bounds_xy
    polygons = [placement_polygon_xy(placement) for placement in placements]
    all_x = [scene_min_x, scene_max_x]
    all_y = [scene_min_y, scene_max_y]
    for polygon in polygons:
        all_x.extend(point[0] for point in polygon)
        all_y.extend(point[1] for point in polygon)
    for rect in forbidden_xy_rects:
        all_x.extend((rect[0], rect[2]))
        all_y.extend((rect[1], rect[3]))

    min_x = min(all_x) - padding_m
    max_x = max(all_x) + padding_m
    min_y = min(all_y) - padding_m
    max_y = max(all_y) + padding_m
    width_px = max(1, int(math.ceil((max_x - min_x) / resolution)) + 1)
    height_px = max(1, int(math.ceil((max_y - min_y) / resolution)) + 1)

    image = Image.new("RGB", (width_px, height_px), SEMANTIC_BACKGROUND)
    draw = ImageDraw.Draw(image, "RGBA")

    scene_rect_px = [
        world_to_pixel((scene_min_x, scene_min_y), min_x, max_y, resolution),
        world_to_pixel((scene_max_x, scene_max_y), min_x, max_y, resolution),
    ]
    draw.rectangle(
        [
            min(scene_rect_px[0][0], scene_rect_px[1][0]),
            min(scene_rect_px[0][1], scene_rect_px[1][1]),
            max(scene_rect_px[0][0], scene_rect_px[1][0]),
            max(scene_rect_px[0][1], scene_rect_px[1][1]),
        ],
        outline=SEMANTIC_SCENE_OUTLINE + (255,),
        width=2,
    )

    for rect in forbidden_xy_rects:
        rect_px = [
            world_to_pixel((rect[0], rect[1]), min_x, max_y, resolution),
            world_to_pixel((rect[2], rect[3]), min_x, max_y, resolution),
        ]
        xy = [
            min(rect_px[0][0], rect_px[1][0]),
            min(rect_px[0][1], rect_px[1][1]),
            max(rect_px[0][0], rect_px[1][0]),
            max(rect_px[0][1], rect_px[1][1]),
        ]
        draw.rectangle(xy, fill=SEMANTIC_FORBIDDEN + (35,), outline=SEMANTIC_FORBIDDEN + (180,), width=2)
        for offset in range(-height_px, width_px, 18):
            draw.line((xy[0] + offset, xy[3], xy[0] + offset + (xy[3] - xy[1]), xy[1]), fill=SEMANTIC_FORBIDDEN + (80,))

    objects: list[dict[str, object]] = []
    draw_order = sorted(
        zip(placements, polygons, strict=True),
        key=lambda item: (item[0].z, item[0].asset.height, item[0].instance_name),
    )
    for placement, polygon in draw_order:
        placement_class = placement.asset.placement_class
        color = SEMANTIC_COLORS.get(placement_class, SEMANTIC_COLORS["unknown"])
        polygon_px = [world_to_pixel(point, min_x, max_y, resolution) for point in polygon]
        draw.polygon(polygon_px, fill=color + (100,), outline=color + (235,))
        draw.line(polygon_px + [polygon_px[0]], fill=color + (255,), width=2)
        if draw_labels:
            maybe_draw_label(draw, placement, polygon_px)
        objects.append(
            {
                "instance_name": placement.instance_name,
                "asset_name": placement.asset.name,
                "placement_class": placement_class,
                "support_type": placement.support_type,
                "parent": placement.parent,
                "center_xy_m": [round(placement.x, 6), round(placement.y, 6)],
                "z_m": round(placement.z, 6),
                "yaw_radians": round(placement.yaw, 6),
                "asset_size_m": [
                    round(placement.asset.width, 6),
                    round(placement.asset.length, 6),
                    round(placement.asset.height, 6),
                ],
                "polygon_xy_m": [[round(x, 6), round(y, 6)] for x, y in polygon],
                "bbox_xy_m": [
                    round(placement.min_x, 6),
                    round(placement.min_y, 6),
                    round(placement.max_x, 6),
                    round(placement.max_y, 6),
                ],
                "color_rgb": list(color),
            }
        )

    draw_semantic_legend(draw, width_px)
    image_path = output_dir / "semantic.png"
    json_path = output_dir / "semantic.json"
    image.save(image_path)
    payload = {
        "type": "semantic_floorplan",
        "image": portable_path(image_path, root),
        "resolution_m_per_pixel": resolution,
        "bounds_xy_m": [min_x, min_y, max_x, max_y],
        "scene_bounds_xy_m": [scene_min_x, scene_min_y, scene_max_x, scene_max_y],
        "image_size_px": [width_px, height_px],
        "forbidden_xy_rects": [list(rect) for rect in forbidden_xy_rects],
        "legend": {name: list(color) for name, color in SEMANTIC_COLORS.items() if name != "unknown"},
        "object_count": len(objects),
        "objects": objects,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "image": portable_path(image_path, root),
        "json": portable_path(json_path, root),
        "object_count": len(objects),
        "bounds_xy_m": payload["bounds_xy_m"],
        "image_size_px": payload["image_size_px"],
    }


def placement_polygon_xy(placement: PlacedAsset) -> list[tuple[float, float]]:
    half_x = placement.asset.width / 2.0
    half_y = placement.asset.length / 2.0
    cos_yaw = math.cos(placement.yaw)
    sin_yaw = math.sin(placement.yaw)
    polygon: list[tuple[float, float]] = []
    for local_x, local_y in ((-half_x, -half_y), (half_x, -half_y), (half_x, half_y), (-half_x, half_y)):
        polygon.append(
            (
                placement.x + cos_yaw * local_x - sin_yaw * local_y,
                placement.y + sin_yaw * local_x + cos_yaw * local_y,
            )
        )
    return polygon


def world_to_pixel(point: tuple[float, float], min_x: float, max_y: float, resolution: float) -> tuple[int, int]:
    x, y = point
    return int(round((x - min_x) / resolution)), int(round((max_y - y) / resolution))


def maybe_draw_label(draw: ImageDraw.ImageDraw, placement: PlacedAsset, polygon_px: list[tuple[int, int]]) -> None:
    xs = [point[0] for point in polygon_px]
    ys = [point[1] for point in polygon_px]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    if width < 46 or height < 16:
        return
    label = placement.asset.placement_class
    text_x = min(xs) + 4
    text_y = min(ys) + 3
    draw.rectangle((text_x - 2, text_y - 2, text_x + len(label) * 7 + 2, text_y + 12), fill=(255, 255, 255, 150))
    draw.text((text_x, text_y), label, fill=(20, 20, 20, 230))


def draw_semantic_legend(draw: ImageDraw.ImageDraw, width_px: int) -> None:
    x = 10
    y = 10
    for name in ("table", "seat", "floor", "tabletop"):
        color = SEMANTIC_COLORS[name]
        draw.rectangle((x, y, x + 14, y + 14), fill=color + (150,), outline=color + (255,))
        draw.text((x + 20, y), name, fill=(20, 20, 20, 230))
        y += 20
    draw.rectangle((width_px - 120, 10, width_px - 106, 24), fill=SEMANTIC_FORBIDDEN + (60,), outline=SEMANTIC_FORBIDDEN + (180,))
    draw.text((width_px - 100, 10), "forbidden", fill=(20, 20, 20, 230))


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
    clean_enabled: bool = False,
    clean_min_density: float = 2.0,
    clean_min_neighbors: int = 2,
    clean_min_z: float = 0.05,
    clean_max_abs_normal_z: float = 0.7,
    clean_opening_px: int = 0,
    clean_closing_px: int = 1,
) -> dict[str, object]:
    mesh_path, conversion_meta = prepare_mesh_path(input_path, output_dir)
    path_root = input_path.parent
    mesh = load_as_mesh(mesh_path)

    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    vertical_axis = int(np.argmin(np.ptp(vertices, axis=0)))
    reorder = [axis for axis in range(3) if axis != vertical_axis] + [vertical_axis]
    oriented_vertices = vertices[:, reorder]
    oriented_mesh = trimesh.Trimesh(vertices=oriented_vertices, faces=mesh.faces, process=False)

    sampled_points, sampled_normals, sample_meta = sample_surface_points(
        oriented_mesh,
        resolution,
        sample_density_scale,
        min_sample_points,
        max_sample_points,
        include_normals=clean_enabled,
    )
    all_points = np.vstack([oriented_vertices, sampled_points]).astype(np.float32, copy=False)

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

    rows, cols, z_vals = rasterize_points(
        points=shifted_points,
        origin_xy=xy_min,
        resolution=resolution,
        shape=shape,
    )
    pixel_min_z = build_pixel_min_height_map_from_raster(rows=rows, cols=cols, z_vals=z_vals, shape=shape)
    valid_projection = np.isfinite(pixel_min_z)

    stack = np.zeros((len(z_levels), height, width), dtype=np.uint8)
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

    if clean_enabled:
        shifted_sampled_points = sampled_points.astype(np.float32, copy=True)
        shifted_sampled_points[:, 2] -= effective_bottom
        assert sampled_normals is not None
        clean_points = shifted_sampled_points[np.abs(sampled_normals[:, 2]) <= clean_max_abs_normal_z]
        clean_rows, clean_cols, clean_z_vals = rasterize_points(
            points=clean_points,
            origin_xy=xy_min,
            resolution=resolution,
            shape=shape,
        )
    else:
        clean_rows = np.asarray([], dtype=np.int32)
        clean_cols = np.asarray([], dtype=np.int32)
        clean_z_vals = np.asarray([], dtype=np.float32)
    clean_layer_dir = output_dir / "clean_layers"
    projection_render = render_projection_stack(
        rows=rows,
        cols=cols,
        z_vals=z_vals,
        clean_rows=clean_rows,
        clean_cols=clean_cols,
        clean_z_vals=clean_z_vals,
        z_levels=z_levels,
        shape=shape,
        output_dir=output_dir,
        max_alpha=0.90,
        foreground_color=(40, 40, 40),
        clean_output_dir=clean_layer_dir if clean_enabled else None,
        clean_min_density=clean_min_density,
        clean_min_neighbors=clean_min_neighbors,
        clean_min_z=clean_min_z,
        clean_opening_px=clean_opening_px,
        clean_closing_px=clean_closing_px,
    )
    soft_plain_images_by_index = projection_render["raw_images"]
    clean_images_by_index = projection_render["clean_images"]
    soft_plain_panels = [(f"z<={level:.2f}", soft_plain_images_by_index[index]) for index, level in enumerate(z_levels)]
    clean_preview_path = output_dir / "geometry_clean_preview.png"
    clean_top_path = output_dir / "geometry_clean.png"
    raw_top_path = output_dir / "geometry_raw.png"

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
    soft_plain_images_by_index[0].save(raw_top_path)

    clean_record: dict[str, object] | None = None
    if clean_enabled:
        clean_panels = [(f"z<={level:.2f}", clean_images_by_index[index]) for index, level in enumerate(z_levels)]
        clean_preview = build_contact_sheet(
            clean_panels,
            title=f"clean scene: {input_path.name}",
            tile_size=preview_tile_size,
        )
        clean_preview.save(clean_preview_path)
        clean_images_by_index[0].save(clean_top_path)
        clean_record = {
            "image": portable_path(clean_top_path, path_root),
            "preview": portable_path(clean_preview_path, path_root),
            "layers_dir": portable_path(clean_layer_dir, path_root),
            "min_density": float(clean_min_density),
            "min_neighbors": int(clean_min_neighbors),
            "min_z_m": float(clean_min_z),
            "max_abs_normal_z": float(clean_max_abs_normal_z),
            "opening_px": int(clean_opening_px),
            "closing_px": int(clean_closing_px),
            "stats": projection_render["clean_stats"],
        }

    meta = {
        "approach": "height_sweep_projection",
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
        "sampling": sample_meta,
        "geometry_raw": portable_path(raw_top_path, path_root),
        "geometry_clean": clean_record,
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
        "z_levels_m": [float(level) for level in z_levels],
        "raw": portable_path(raw_top_path, path_root),
        "clean": clean_record,
    }


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
    clean_rows: np.ndarray,
    clean_cols: np.ndarray,
    clean_z_vals: np.ndarray,
    z_levels: list[float],
    shape: tuple[int, int],
    output_dir: Path,
    max_alpha: float,
    foreground_color: tuple[int, int, int],
    clean_output_dir: Path | None,
    clean_min_density: float,
    clean_min_neighbors: int,
    clean_min_z: float,
    clean_opening_px: int,
    clean_closing_px: int,
) -> dict[str, object]:
    raw_images_by_index: dict[int, Image.Image] = {}
    clean_images_by_index: dict[int, Image.Image] = {}
    clean_stats_by_index: dict[int, dict[str, object]] = {}

    if rows.size == 0:
        empty_density = np.zeros(shape, dtype=np.float32)
        for index, level in enumerate(z_levels):
            raw_image = render_density_projection(
                empty_density,
                max_alpha=max_alpha,
                foreground_color=foreground_color,
            )
            raw_image.save(output_dir / floorplan_layer_filename(float(level)))
            raw_images_by_index[index] = raw_image
            if clean_output_dir is not None:
                clean_output_dir.mkdir(parents=True, exist_ok=True)
                clean_image = render_clean_geometry_mask(np.zeros(shape, dtype=bool))
                clean_image.save(clean_output_dir / floorplan_layer_filename(float(level)))
                clean_images_by_index[index] = clean_image
                clean_stats_by_index[index] = clean_geometry_stats(
                    index,
                    level,
                    empty_density,
                    np.zeros(shape, dtype=bool),
                )
        return {
            "raw_images": raw_images_by_index,
            "clean_images": clean_images_by_index,
            "clean_stats": (
                [clean_stats_by_index[index] for index in range(len(z_levels))] if clean_output_dir is not None else []
            ),
        }

    if len(z_levels) == 1:
        level = float(z_levels[0])
        density = np.zeros(shape, dtype=np.float32)
        keep = z_vals <= level
        if np.any(keep):
            np.add.at(density, (rows[keep], cols[keep]), 1.0)
        raw_image = render_density_projection(density, max_alpha=max_alpha, foreground_color=foreground_color)
        raw_image.save(output_dir / floorplan_layer_filename(level))
        raw_images_by_index[0] = raw_image

        if clean_output_dir is not None:
            clean_output_dir.mkdir(parents=True, exist_ok=True)
            clean_density = np.zeros(shape, dtype=np.float32)
            clean_keep = (clean_z_vals >= clean_min_z) & (clean_z_vals <= level)
            if np.any(clean_keep):
                np.add.at(clean_density, (clean_rows[clean_keep], clean_cols[clean_keep]), 1.0)
            clean_mask = build_clean_geometry_mask(
                clean_density,
                min_density=clean_min_density,
                min_neighbors=clean_min_neighbors,
                opening_px=clean_opening_px,
                closing_px=clean_closing_px,
            )
            clean_image = render_clean_geometry_mask(clean_mask)
            clean_image.save(clean_output_dir / floorplan_layer_filename(level))
            clean_images_by_index[0] = clean_image
            clean_stats_by_index[0] = clean_geometry_stats(0, level, clean_density, clean_mask)

        return {
            "raw_images": raw_images_by_index,
            "clean_images": clean_images_by_index,
            "clean_stats": [clean_stats_by_index[0]] if clean_output_dir is not None else [],
        }

    order = np.argsort(z_vals, kind="mergesort")
    rows_sorted = rows[order]
    cols_sorted = cols[order]
    z_sorted = z_vals[order]
    density = np.zeros(shape, dtype=np.float32)
    clean_density = np.zeros(shape, dtype=np.float32)
    cursor = 0
    clean_order = np.argsort(clean_z_vals, kind="mergesort")
    clean_rows_sorted = clean_rows[clean_order]
    clean_cols_sorted = clean_cols[clean_order]
    clean_z_sorted = clean_z_vals[clean_order]
    clean_cursor = int(np.searchsorted(clean_z_sorted, clean_min_z, side="left"))

    if clean_output_dir is not None:
        clean_output_dir.mkdir(parents=True, exist_ok=True)

    ascending_levels = sorted((float(level), index) for index, level in enumerate(z_levels))
    for level, original_index in ascending_levels:
        next_cursor = int(np.searchsorted(z_sorted, level, side="right"))
        if next_cursor > cursor:
            np.add.at(density, (rows_sorted[cursor:next_cursor], cols_sorted[cursor:next_cursor]), 1.0)
            cursor = next_cursor
        if clean_output_dir is not None:
            next_clean_cursor = int(np.searchsorted(clean_z_sorted, level, side="right"))
            if next_clean_cursor > clean_cursor:
                np.add.at(
                    clean_density,
                    (
                        clean_rows_sorted[clean_cursor:next_clean_cursor],
                        clean_cols_sorted[clean_cursor:next_clean_cursor],
                    ),
                    1.0,
                )
                clean_cursor = next_clean_cursor

        raw_image = render_density_projection(density, max_alpha=max_alpha, foreground_color=foreground_color)
        raw_image.save(output_dir / floorplan_layer_filename(level))
        raw_images_by_index[original_index] = raw_image

        if clean_output_dir is not None:
            clean_mask = build_clean_geometry_mask(
                clean_density,
                min_density=clean_min_density,
                min_neighbors=clean_min_neighbors,
                opening_px=clean_opening_px,
                closing_px=clean_closing_px,
            )
            clean_image = render_clean_geometry_mask(clean_mask)
            clean_image.save(clean_output_dir / floorplan_layer_filename(level))
            clean_images_by_index[original_index] = clean_image
            clean_stats_by_index[original_index] = clean_geometry_stats(original_index, level, clean_density, clean_mask)

    return {
        "raw_images": raw_images_by_index,
        "clean_images": clean_images_by_index,
        "clean_stats": (
            [clean_stats_by_index[index] for index in range(len(z_levels))] if clean_output_dir is not None else []
        ),
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
        return {index: empty.copy() for index in range(len(z_levels))}

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


def build_clean_geometry_mask(
    density: np.ndarray,
    min_density: float,
    min_neighbors: int,
    opening_px: int,
    closing_px: int,
) -> np.ndarray:
    if min_density <= 0:
        mask = density > 0
    else:
        mask = density >= min_density

    if min_neighbors > 0:
        neighbor_count = count_binary_neighbors(mask, include_center=False)
        mask &= neighbor_count >= min_neighbors
    if opening_px > 0:
        mask = binary_open(mask, opening_px)
    if closing_px > 0:
        mask = binary_close(mask, closing_px)
    return mask


def clean_geometry_stats(index: int, level: float, density: np.ndarray, mask: np.ndarray) -> dict[str, object]:
    positive_density = density[density > 0]
    return {
        "index": int(index),
        "z_level_m": float(level),
        "raw_positive_pixels": int(positive_density.size),
        "clean_occupied_pixels": int(mask.sum()),
        "clean_occupied_ratio": float(mask.mean()) if mask.size else 0.0,
        "raw_density_mean": float(positive_density.mean()) if positive_density.size else 0.0,
        "raw_density_max": float(positive_density.max()) if positive_density.size else 0.0,
    }


def render_clean_geometry_mask(mask: np.ndarray) -> Image.Image:
    image = np.full(mask.shape + (3,), 255, dtype=np.uint8)
    image[mask] = (35, 35, 35)
    return Image.fromarray(np.flipud(image))


def binary_open(mask: np.ndarray, radius: int) -> np.ndarray:
    opened = mask
    for _ in range(radius):
        opened = binary_erode(opened)
    for _ in range(radius):
        opened = binary_dilate(opened)
    return opened


def binary_close(mask: np.ndarray, radius: int) -> np.ndarray:
    closed = mask
    for _ in range(radius):
        closed = binary_dilate(closed)
    for _ in range(radius):
        closed = binary_erode(closed)
    return closed


def binary_dilate(mask: np.ndarray) -> np.ndarray:
    return count_binary_neighbors(mask, include_center=True) > 0


def binary_erode(mask: np.ndarray) -> np.ndarray:
    return count_binary_neighbors(mask, include_center=True) == 9


def count_binary_neighbors(mask: np.ndarray, include_center: bool) -> np.ndarray:
    padded = np.pad(mask.astype(np.uint8, copy=False), 1)
    counts = np.zeros(mask.shape, dtype=np.uint8)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if not include_center and dy == 0 and dx == 0:
                continue
            counts += padded[1 + dy : 1 + dy + mask.shape[0], 1 + dx : 1 + dx + mask.shape[1]]
    return counts


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
