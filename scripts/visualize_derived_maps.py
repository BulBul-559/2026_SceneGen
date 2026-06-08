#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont


LOS_COLOR = (33, 150, 243, 150)
NLOS_COLOR = (245, 166, 35, 140)
INVALID_COLOR = (255, 255, 255, 0)
BS_FILL = (38, 70, 83, 255)
BS_OUTLINE = (255, 255, 255, 255)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def scene_dir_from_input(path: Path) -> Path:
    path = path.resolve()
    if path.name == "maps":
        return path.parent
    return path


def load_background(scene_dir: Path, width: int, height: int, mode: str) -> Image.Image:
    candidates: list[Path]
    if mode == "mask":
        candidates = [
            scene_dir / "floorplan" / "class_mask_preview.png",
            scene_dir / "mask_preview.png",
        ]
    elif mode == "white":
        return Image.new("RGB", (width, height), (248, 249, 250))
    else:
        candidates = [
            scene_dir / "floorplan" / "floorplan_1p60.png",
            scene_dir / "floorplan.png",
            scene_dir / "floorplan" / "class_mask_preview.png",
            scene_dir / "mask_preview.png",
        ]
    for candidate in candidates:
        if candidate.is_file():
            image = Image.open(candidate).convert("RGB")
            if image.size != (width, height):
                image = image.resize((width, height), Image.Resampling.BILINEAR)
            return image
    return Image.new("RGB", (width, height), (248, 249, 250))


def nearest_resize(mask: np.ndarray, width: int, height: int) -> np.ndarray:
    image = Image.fromarray(mask.astype(np.uint8), mode="L").resize((width, height), Image.Resampling.NEAREST)
    return np.asarray(image)


def soften_background(base: Image.Image) -> Image.Image:
    white = Image.new("RGB", base.size, (255, 255, 255))
    return Image.blend(base, white, 0.28)


def alpha_composite_mask(base: Image.Image, mask: np.ndarray, color: tuple[int, int, int, int]) -> Image.Image:
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    rgba = np.asarray(overlay).copy()
    rgba[mask.astype(bool)] = color
    overlay = Image.fromarray(rgba, mode="RGBA")
    return Image.alpha_composite(base.convert("RGBA"), overlay)


def draw_bs_marker(image: Image.Image, x: float, y: float, label: str) -> None:
    draw = ImageDraw.Draw(image)
    radius = max(5, int(round(min(image.size) * 0.012)))
    x_i, y_i = int(round(x)), int(round(y))
    halo = radius + 4
    draw.ellipse((x_i - halo, y_i - halo, x_i + halo, y_i + halo), fill=(255, 255, 255, 225))
    draw.ellipse((x_i - radius, y_i - radius, x_i + radius, y_i + radius), fill=BS_FILL, outline=BS_OUTLINE, width=2)
    draw.line((x_i - radius - 3, y_i, x_i + radius + 3, y_i), fill=BS_OUTLINE, width=2)
    draw.line((x_i, y_i - radius - 3, x_i, y_i + radius + 3), fill=BS_OUTLINE, width=2)
    font = ImageFont.load_default()
    draw.text((x_i + halo + 2, y_i - halo), label, fill=(20, 20, 20), font=font)


def add_legend_header(
    image: Image.Image,
    *,
    title: str,
    los_count: int | None = None,
    nlos_count: int | None = None,
    valid_count: int | None = None,
) -> Image.Image:
    font = ImageFont.load_default()
    lines = [title]
    if los_count is not None and nlos_count is not None and valid_count is not None:
        ratio = los_count / valid_count if valid_count else 0.0
        lines.append(f"LOS {los_count}  NLOS {nlos_count}  valid {valid_count}  ratio {ratio:.3f}")

    header_h = 58 if los_count is None else 70
    canvas = Image.new("RGBA", (image.width, image.height + header_h), (255, 255, 255, 255))
    canvas.alpha_composite(image.convert("RGBA"), (0, header_h))
    draw = ImageDraw.Draw(canvas)
    width = min(image.width - 16, int(max(draw.textlength(line, font=font) for line in lines) + 24))
    height = header_h - 16
    draw.rounded_rectangle((8, 8, 8 + width, 8 + height), radius=6, fill=(255, 255, 255, 245), outline=(210, 210, 210, 255))
    y = 16
    for line in lines:
        draw.text((18, y), line, fill=(24, 24, 24), font=font)
        y += 15
    y += 4
    draw.rectangle((18, y, 32, y + 10), fill=LOS_COLOR)
    draw.text((38, y - 1), "LOS", fill=(24, 24, 24), font=font)
    draw.rectangle((78, y, 92, y + 10), fill=NLOS_COLOR)
    draw.text((98, y - 1), "NLOS", fill=(24, 24, 24), font=font)
    return canvas


def visualize_los(scene_dir: Path, output_dir: Path, bs_index: int, background: str) -> Path:
    propagation = np.load(scene_dir / "maps" / "propagation.npz")
    los_maps = propagation["los_maps"]
    wall_count_maps = propagation["wall_count_maps"]
    ue_valid_mask = propagation["ue_valid_mask"].astype(bool)
    bs_coords_px = propagation["bs_coords_px"]
    height = int(propagation["height"])
    width = int(propagation["width"])
    if bs_index < 0 or bs_index >= los_maps.shape[0]:
        raise IndexError(f"bs_index {bs_index} out of range 0..{los_maps.shape[0] - 1}")

    base = soften_background(load_background(scene_dir, width, height, background))
    los_grid = los_maps[bs_index].astype(bool)
    wall_count_grid = wall_count_maps[bs_index]
    los_full = nearest_resize(los_grid.astype(np.uint8), width, height).astype(bool)
    valid_full = nearest_resize(ue_valid_mask.astype(np.uint8), width, height).astype(bool)
    nlos_full = nearest_resize(((wall_count_grid > 0) & ue_valid_mask).astype(np.uint8), width, height).astype(bool)

    image = base.convert("RGBA")
    image = alpha_composite_mask(image, nlos_full & valid_full, NLOS_COLOR)
    image = alpha_composite_mask(image, los_full & valid_full, LOS_COLOR)
    bs_x, bs_y = bs_coords_px[bs_index]
    draw_bs_marker(image, float(bs_x), float(bs_y), f"BS {bs_index}")
    image = add_legend_header(
        image,
        title=f"{scene_dir.name} / BS {bs_index} LoS map",
        los_count=int((los_grid & ue_valid_mask).sum()),
        nlos_count=int(((wall_count_grid > 0) & ue_valid_mask).sum()),
        valid_count=int(ue_valid_mask.sum()),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"bs_{bs_index:03d}_los_map.png"
    image.convert("RGB").save(output_path)
    return output_path


def visualize_wall_count(scene_dir: Path, output_dir: Path, bs_index: int, background: str) -> Path:
    propagation = np.load(scene_dir / "maps" / "propagation.npz")
    wall_counts = propagation["wall_count_maps"][bs_index]
    ue_valid_mask = propagation["ue_valid_mask"].astype(bool)
    bs_coords_px = propagation["bs_coords_px"]
    height = int(propagation["height"])
    width = int(propagation["width"])
    base = soften_background(load_background(scene_dir, width, height, background)).convert("RGBA")
    full_counts = nearest_resize(wall_counts, width, height)
    full_valid = nearest_resize(ue_valid_mask.astype(np.uint8), width, height).astype(bool)
    colors = {
        0: (33, 150, 243, 125),
        1: (255, 193, 7, 145),
        2: (156, 39, 176, 145),
        3: (55, 71, 79, 165),
    }
    image = base
    for count, color in colors.items():
        image = alpha_composite_mask(image, (full_counts == count) & full_valid, color)
    bs_x, bs_y = bs_coords_px[bs_index]
    draw_bs_marker(image, float(bs_x), float(bs_y), f"BS {bs_index}")
    image = add_legend_header(image, title=f"{scene_dir.name} / BS {bs_index} wall-count map")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"bs_{bs_index:03d}_wall_count_map.png"
    image.convert("RGB").save(output_path)
    return output_path


def visualize_sdf(scene_dir: Path, output_dir: Path, background: str) -> Path:
    geometry = np.load(scene_dir / "maps" / "geometry.npz")
    sdf = geometry["sdf"].astype(np.float32)
    valid = geometry["sdf_valid_mask"].astype(bool)
    height = int(geometry["height"])
    width = int(geometry["width"])
    base = soften_background(load_background(scene_dir, width, height, background)).convert("RGBA")
    positive = np.clip(sdf, 0.0, 1.0)
    negative = np.clip(-sdf, 0.0, 1.0)
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[..., 0] = (negative * 130).astype(np.uint8)
    rgba[..., 1] = (positive * 120 + negative * 40).astype(np.uint8)
    rgba[..., 2] = (positive * 210 + negative * 170).astype(np.uint8)
    rgba[..., 3] = np.where(valid, 150, 0).astype(np.uint8)
    image = Image.alpha_composite(base, Image.fromarray(rgba, mode="RGBA"))
    image = add_legend_header(image, title=f"{scene_dir.name} / SDF")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "sdf_map.png"
    image.convert("RGB").save(output_path)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Visualize SceneGen derived maps on top of floorplan images.")
    parser.add_argument("scene_dir", type=Path, help="Scene directory or its maps/ directory.")
    parser.add_argument("--kind", choices=["los", "wall_count", "sdf", "all"], default="los", help="Map type to visualize.")
    parser.add_argument("--bs-index", type=int, default=0, help="BS index for LoS/wall-count visualizations.")
    parser.add_argument("--all-bs", action="store_true", help="Render every BS for LoS/wall-count visualizations.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Output directory. Defaults to <scene>/maps/preview.")
    parser.add_argument(
        "--background",
        choices=["floorplan", "mask", "white"],
        default="floorplan",
        help="Background image for overlays.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    scene_dir = scene_dir_from_input(args.scene_dir)
    output_dir = args.output_dir or scene_dir / "maps" / "preview"
    outputs: list[Path] = []
    if args.kind in {"los", "all"}:
        propagation = np.load(scene_dir / "maps" / "propagation.npz")
        bs_indices = range(propagation["los_maps"].shape[0]) if args.all_bs else [args.bs_index]
        outputs.extend(visualize_los(scene_dir, output_dir, index, args.background) for index in bs_indices)
    if args.kind in {"wall_count", "all"}:
        propagation = np.load(scene_dir / "maps" / "propagation.npz")
        bs_indices = range(propagation["wall_count_maps"].shape[0]) if args.all_bs else [args.bs_index]
        outputs.extend(visualize_wall_count(scene_dir, output_dir, index, args.background) for index in bs_indices)
    if args.kind in {"sdf", "all"}:
        outputs.append(visualize_sdf(scene_dir, output_dir, args.background))
    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
