from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw


SCHEMA_VERSION = "front3d.phase1.v1"
OBJECT_RAW_DIR = "scenegen_objects_raw"
OBJECT_NORMALIZED_DIR = "scenegen_objects_normalized"
ARCH_RAW_DIR = "scenegen_architecture_raw"
ARCH_NORMALIZED_DIR = "scenegen_architecture_normalized"
REPORT_DIR = "scenegen_reports"

COORDINATE_TRANSFORMS: dict[str, dict[str, object]] = {
    "raw": {
        "coordinate_system": {"right": "+X", "up": "+Y", "forward": "+Z"},
        "scene_axis_transform": None,
        "matrix_4x4_row_major": None,
    },
    "normalized": {
        "coordinate_system": {"right": "+X", "forward": "-Z_to_+Y", "up": "+Y_to_+Z"},
        "scene_axis_transform": "front3d_y_up_to_scenegen_z_up",
        "matrix_4x4_row_major": [
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
        ],
    },
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare SceneGen-friendly 3D-FRONT phase-1 assets.")
    parser.add_argument("--source", type=Path, default=Path("data/3D-Front"))
    parser.add_argument("--output", type=Path, default=Path("data/3D-Front"))
    parser.add_argument("--copy-mode", choices=("copy",), default="copy")
    parser.add_argument("--architecture-granularity", choices=("scene",), default="scene")
    parser.add_argument("--scope", choices=("all",), default="all")
    parser.add_argument("--limit-objects", type=int, default=None, help="Optional smoke-test limit for object models.")
    parser.add_argument("--limit-scenes", type=int, default=None, help="Optional smoke-test limit for architecture scenes.")
    parser.add_argument("--skip-disk-check", action="store_true")
    parser.add_argument("--disk-buffer-gb", type=float, default=20.0)
    parser.add_argument("--preview-size", type=int, default=512)
    return parser


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sanitize_name(value: object) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._-")
    return cleaned or "item"


def repo_root() -> Path:
    for parent in [Path.cwd(), *Path.cwd().parents]:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "scenegen").is_dir():
            return parent
    return Path.cwd()


def portable_path(path: Path, root: Path) -> str:
    absolute = path.absolute()
    base = root.absolute()
    try:
        return absolute.relative_to(base).as_posix()
    except ValueError:
        return os.path.relpath(absolute, base).replace(os.sep, "/")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def copy_file(source: Path, dest: Path) -> str | None:
    if not source.is_file():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return dest.name


def copy_first_existing(candidates: list[Path], dest: Path) -> str | None:
    for candidate in candidates:
        copied = copy_file(candidate, dest)
        if copied is not None:
            return copied
    return None


def copy_texture(model_dir: Path, dest_dir: Path) -> str | None:
    textures = sorted(path for path in model_dir.glob("texture.*") if path.is_file())
    if not textures:
        return None
    texture = textures[0]
    dest = dest_dir / f"texture{texture.suffix.lower()}"
    copy_file(texture, dest)
    return dest.name


def obj_stats(path: Path) -> dict[str, Any]:
    min_xyz = [math.inf, math.inf, math.inf]
    max_xyz = [-math.inf, -math.inf, -math.inf]
    vertex_count = 0
    face_count = 0
    if not path.is_file():
        return {"vertex_count": 0, "face_count": 0, "bbox": None, "size": None}
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if line.startswith("v "):
                parts = line.split()
                if len(parts) < 4:
                    continue
                xyz = [float(parts[1]), float(parts[2]), float(parts[3])]
                vertex_count += 1
                for axis, value in enumerate(xyz):
                    min_xyz[axis] = min(min_xyz[axis], value)
                    max_xyz[axis] = max(max_xyz[axis], value)
            elif line.startswith("f "):
                face_count += 1
    if vertex_count == 0:
        return {"vertex_count": 0, "face_count": face_count, "bbox": None, "size": None}
    return {
        "vertex_count": vertex_count,
        "face_count": face_count,
        "bbox": {"min": min_xyz, "max": max_xyz},
        "size": {
            "x": max_xyz[0] - min_xyz[0],
            "y": max_xyz[1] - min_xyz[1],
            "z": max_xyz[2] - min_xyz[2],
        },
    }


def classify_object(super_category: object, category: object) -> tuple[str, tuple[str, ...], bool]:
    super_text = str(super_category or "").lower()
    category_text = str(category or "").lower()
    text = f"{super_text} {category_text}"
    if "lamp" in text and not any(part in text for part in ("floor lamp", "table lamp")):
        return "skip", (), False
    if "table" in text or "desk" in text:
        return "table", ("floor",), True
    if any(part in super_text for part in ("chair", "sofa", "stool")) or any(
        part in category_text for part in ("chair", "sofa", "stool")
    ):
        return "seat", ("floor",), True
    if not category:
        return "skip", (), False
    return "floor", ("floor",), True


def object_material_mapping(material: object, category: object) -> dict[str, str]:
    material_text = str(material or "").lower()
    category_text = str(category or "").lower()
    text = f"{material_text} {category_text}"
    if any(token in text for token in ("glass", "mirror")):
        return {"source": str(material or category or "unknown"), "sionna": "itu-glass", "itu_type": "glass", "confidence": "medium"}
    if "metal" in text:
        return {"source": str(material or category or "unknown"), "sionna": "itu-metal", "itu_type": "metal", "confidence": "medium"}
    if any(token in text for token in ("stone", "marble", "tile", "flooring", "paint")):
        return {
            "source": str(material or category or "unknown"),
            "sionna": "itu-concrete",
            "itu_type": "concrete",
            "confidence": "medium",
        }
    if any(token in text for token in ("wood", "board", "plywood", "bamboo", "rattan")):
        return {"source": str(material or category or "unknown"), "sionna": "itu-wood", "itu_type": "wood", "confidence": "medium"}
    if any(token in text for token in ("cloth", "leather", "suede")):
        return {"source": str(material or category or "unknown"), "sionna": "itu-wood", "itu_type": "wood", "confidence": "low"}
    return {"source": str(material or category or "unknown"), "sionna": "itu-wood", "itu_type": "wood", "confidence": "low"}


def architecture_material_for_mesh_type(mesh_type: object) -> dict[str, str]:
    text = str(mesh_type or "").lower()
    if "window" in text or "glass" in text:
        return {"source": str(mesh_type or "unknown"), "sionna": "itu-glass", "itu_type": "glass", "confidence": "medium"}
    if "door" in text or "cabinet" in text or "baseboard" in text:
        return {"source": str(mesh_type or "unknown"), "sionna": "itu-wood", "itu_type": "wood", "confidence": "medium"}
    return {"source": str(mesh_type or "unknown"), "sionna": "itu-concrete", "itu_type": "concrete", "confidence": "medium"}


def transform_point(point: tuple[float, float, float], variant: str) -> tuple[float, float, float]:
    x, y, z = point
    if variant == "normalized":
        return x, -z, y
    return x, y, z


def iter_mesh_vertices(mesh: dict[str, Any], variant: str) -> list[tuple[float, float, float]]:
    xyz = mesh.get("xyz") or []
    vertices: list[tuple[float, float, float]] = []
    for index in range(0, len(xyz), 3):
        if index + 2 >= len(xyz):
            break
        vertices.append(transform_point((float(xyz[index]), float(xyz[index + 1]), float(xyz[index + 2])), variant))
    return vertices


def update_bounds(bounds: list[list[float]], vertices: list[tuple[float, float, float]]) -> None:
    for vertex in vertices:
        for axis, value in enumerate(vertex):
            bounds[0][axis] = min(bounds[0][axis], value)
            bounds[1][axis] = max(bounds[1][axis], value)


def architecture_projection(vertex: tuple[float, float, float], variant: str) -> tuple[float, float]:
    if variant == "raw":
        return vertex[0], vertex[2]
    return vertex[0], vertex[1]


def write_architecture_obj(path: Path, scene_id: str, meshes: list[dict[str, Any]], variant: str) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    bounds = [[math.inf, math.inf, math.inf], [-math.inf, -math.inf, -math.inf]]
    vertex_count = 0
    face_count = 0
    mesh_uids: list[str] = []
    mesh_type_counts: Counter[str] = Counter()
    material_refs: Counter[str] = Counter()
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("# Generated by SceneGen 3D-FRONT phase-1 preparation\n")
        handle.write(f"o {sanitize_name(scene_id)}_{variant}_architecture\n")
        offset = 1
        for mesh_index, mesh in enumerate(meshes):
            vertices = iter_mesh_vertices(mesh, variant)
            if not vertices:
                continue
            faces = mesh.get("faces") or []
            uid = str(mesh.get("uid") or f"mesh_{mesh_index}")
            mesh_type = str(mesh.get("type") or "Unknown")
            material = str(mesh.get("material") or mesh_type)
            mesh_uids.append(uid)
            mesh_type_counts[mesh_type] += 1
            material_refs[material] += 1
            handle.write(f"g {sanitize_name(mesh_type)}_{sanitize_name(uid)}\n")
            handle.write(f"usemtl {sanitize_name(material)}\n")
            for vertex in vertices:
                handle.write(f"v {vertex[0]:.6f} {vertex[1]:.6f} {vertex[2]:.6f}\n")
            for face_index in range(0, len(faces), 3):
                if face_index + 2 >= len(faces):
                    break
                a, b, c = int(faces[face_index]), int(faces[face_index + 1]), int(faces[face_index + 2])
                handle.write(f"f {a + offset} {b + offset} {c + offset}\n")
                face_count += 1
            update_bounds(bounds, vertices)
            vertex_count += len(vertices)
            offset += len(vertices)
    bbox = None if vertex_count == 0 else {"min": bounds[0], "max": bounds[1]}
    size = None if vertex_count == 0 else {axis: bounds[1][i] - bounds[0][i] for i, axis in enumerate(("x", "y", "z"))}
    return {
        "vertex_count": vertex_count,
        "face_count": face_count,
        "bbox": bbox,
        "size": size,
        "mesh_uids": mesh_uids,
        "mesh_type_counts": dict(mesh_type_counts),
        "material_refs": dict(material_refs),
    }


def preview_color(mesh_type: object) -> tuple[int, int, int, int]:
    text = str(mesh_type or "").lower()
    if "floor" in text:
        return 214, 214, 214, 255
    if "wall" in text:
        return 60, 60, 60, 255
    if "door" in text:
        return 130, 95, 52, 255
    if "window" in text:
        return 72, 142, 183, 255
    return 150, 150, 150, 140


def write_architecture_preview(path: Path, meshes: list[dict[str, Any]], variant: str, size_px: int) -> bool:
    projected: list[tuple[str, list[tuple[float, float]], list[int]]] = []
    xs: list[float] = []
    ys: list[float] = []
    for mesh in meshes:
        vertices = iter_mesh_vertices(mesh, variant)
        faces = mesh.get("faces") or []
        points = [architecture_projection(vertex, variant) for vertex in vertices]
        if not points:
            continue
        projected.append((str(mesh.get("type") or "Unknown"), points, [int(value) for value in faces]))
        xs.extend(point[0] for point in points)
        ys.extend(point[1] for point in points)
    if not xs or not ys:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1e-6)
    span_y = max(max_y - min_y, 1e-6)
    margin = 12
    scale = min((size_px - margin * 2) / span_x, (size_px - margin * 2) / span_y)

    def to_px(point: tuple[float, float]) -> tuple[int, int]:
        x = int((point[0] - min_x) * scale + margin)
        y = int(size_px - ((point[1] - min_y) * scale + margin))
        return x, y

    image = Image.new("RGBA", (size_px, size_px), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    for mesh_type, points, faces in projected:
        color = preview_color(mesh_type)
        for index in range(0, len(faces), 3):
            if index + 2 >= len(faces):
                break
            polygon = [to_px(points[faces[index]]), to_px(points[faces[index + 1]]), to_px(points[faces[index + 2]])]
            draw.polygon(polygon, fill=color, outline=color)
    image.convert("RGB").save(path)
    return True


def room_summary(scene_data: dict[str, Any]) -> list[dict[str, Any]]:
    rooms = []
    for room in (scene_data.get("scene") or {}).get("room") or []:
        children = room.get("children") or []
        rooms.append(
            {
                "instanceid": room.get("instanceid"),
                "type": room.get("type"),
                "child_count": len(children),
                "children_refs": [child.get("ref") for child in children if child.get("ref")],
                "has_replace_jid": any(bool(child.get("replace_jid")) for child in children),
                "has_negative_scale": any(any(float(value) < 0 for value in child.get("scale", []) or []) for child in children),
            }
        )
    return rooms


def estimate_required_bytes(source: Path, model_infos: list[dict[str, Any]], scene_paths: list[Path]) -> int:
    total = 0
    model_root = source / "3D-FUTURE-model"
    for item in model_infos:
        model_id = str(item.get("model_id"))
        model_dir = model_root / model_id
        for filename in ("raw_model.obj", "normalized_model.obj", "model.mtl", "image.jpg"):
            path = model_dir / filename
            if path.is_file():
                total += path.stat().st_size
        for texture in model_dir.glob("texture.*"):
            if texture.is_file():
                total += texture.stat().st_size * 2
                break
        mtl = model_dir / "model.mtl"
        image = model_dir / "image.jpg"
        if mtl.is_file():
            total += mtl.stat().st_size
        if image.is_file():
            total += image.stat().st_size
    total += sum(path.stat().st_size * 2 for path in scene_paths if path.is_file())
    return int(total * 1.2)


def ensure_disk_space(output: Path, required_bytes: int, buffer_gb: float) -> None:
    usage = shutil.disk_usage(output if output.exists() else output.parent)
    required = required_bytes + int(buffer_gb * 1024**3)
    if usage.free < required:
        raise RuntimeError(
            f"Not enough free space for 3D-FRONT phase-1 preparation. "
            f"Required about {required / 1024**3:.1f} GiB including buffer, "
            f"available {usage.free / 1024**3:.1f} GiB."
        )


def prepare_object_variant(
    source: Path,
    output_dir: Path,
    item: dict[str, Any],
    variant: str,
    repo: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    model_id = str(item.get("model_id"))
    model_dir = source / "3D-FUTURE-model" / model_id
    out_dir = output_dir / model_id
    flags: list[str] = []
    source_obj_name = "raw_model.obj" if variant == "raw" else "normalized_model.obj"
    source_obj = model_dir / source_obj_name
    obj_name = f"{model_id}.obj"
    copied_obj = copy_file(source_obj, out_dir / obj_name)
    if copied_obj is None:
        flags.append(f"missing_{source_obj_name}")
    copied_mtl = copy_file(model_dir / "model.mtl", out_dir / "model.mtl")
    if copied_mtl is None:
        flags.append("missing_mtl")
    copied_texture = copy_texture(model_dir, out_dir)
    if copied_texture is None:
        flags.append("missing_texture")
    copied_preview = copy_first_existing([model_dir / "image.jpg", model_dir / "image.png"], out_dir / "preview.jpg")
    if copied_preview is None:
        flags.append("missing_preview")

    stats = obj_stats(out_dir / obj_name)
    if stats["vertex_count"] == 0:
        flags.append("empty_or_missing_obj")
    class_name, support, enabled = classify_object(item.get("super-category"), item.get("category"))
    material_mapping = object_material_mapping(item.get("material"), item.get("category"))
    files = {
        "obj": portable_path(out_dir / obj_name, repo) if copied_obj else None,
        "mtl": portable_path(out_dir / "model.mtl", repo) if copied_mtl else None,
        "texture": portable_path(out_dir / copied_texture, repo) if copied_texture else None,
        "preview": portable_path(out_dir / "preview.jpg", repo) if copied_preview else None,
        "source_obj": portable_path(source_obj, repo),
        "source_dir": portable_path(model_dir, repo),
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "dataset": "3d-front",
        "asset_kind": "object",
        "variant": variant,
        "id": model_id,
        "name": item.get("category") or model_id,
        "source_ids": {"model_id": model_id},
        "files": files,
        "semantic": {
            "super_category": item.get("super-category"),
            "category": item.get("category"),
            "style": item.get("style"),
            "theme": item.get("theme"),
            "material": item.get("material"),
        },
        "placement": {"class": class_name, "enabled": enabled, "support": list(support), "weight": 1.0},
        "geometry": {
            "units": "meter",
            "coordinate_system": COORDINATE_TRANSFORMS[variant]["coordinate_system"],
            "bbox": stats["bbox"],
            "size": stats["size"],
            "vertex_count": stats["vertex_count"],
            "face_count": stats["face_count"],
        },
        "normalization": {
            "variant": variant,
            "source_obj_name": source_obj_name,
            "scene_axis_transform": COORDINATE_TRANSFORMS[variant]["scene_axis_transform"],
            "matrix_4x4_row_major": COORDINATE_TRANSFORMS[variant]["matrix_4x4_row_major"],
        },
        "materials": {"sionna": [material_mapping["sionna"]], "source_to_sionna": [material_mapping]},
        "quality": {"ok": not flags, "flags": flags},
    }
    write_json(out_dir / f"{model_id}.json", payload)
    entry = {
        "id": model_id,
        "json": portable_path(out_dir / f"{model_id}.json", repo),
        "obj": files["obj"],
        "preview": files["preview"],
        "category": item.get("category"),
        "super_category": item.get("super-category"),
        "placement_class": class_name,
        "quality_ok": not flags,
        "quality_flags": flags,
    }
    failure = None if not flags else {"id": model_id, "flags": flags}
    return entry, failure


def prepare_architecture_variant(
    source: Path,
    output_dir: Path,
    scene_path: Path,
    variant: str,
    preview_size: int,
    repo: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    scene_id = scene_path.stem
    out_dir = output_dir / scene_id
    out_dir.mkdir(parents=True, exist_ok=True)
    flags: list[str] = []
    scene_data = read_json(scene_path)
    meshes = scene_data.get("mesh") or []
    if not meshes:
        flags.append("missing_mesh")
    obj_path = out_dir / f"{scene_id}.obj"
    stats = write_architecture_obj(obj_path, scene_id, meshes, variant)
    if stats["vertex_count"] == 0:
        flags.append("empty_architecture_obj")
    preview_path = out_dir / "preview.png"
    if not write_architecture_preview(preview_path, meshes, variant, preview_size):
        flags.append("missing_preview")
    mesh_materials = [architecture_material_for_mesh_type(mesh_type) for mesh_type in sorted(stats["mesh_type_counts"])]
    rooms = room_summary(scene_data)
    has_replace_jid = any(room["has_replace_jid"] for room in rooms)
    has_negative_scale = any(room["has_negative_scale"] for room in rooms)
    if has_replace_jid:
        flags.append("has_replace_jid")
    if has_negative_scale:
        flags.append("has_negative_scale")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "dataset": "3d-front",
        "asset_kind": "architecture",
        "variant": variant,
        "id": scene_id,
        "source_ids": {"scene_id": scene_id, "mesh_uids": stats["mesh_uids"]},
        "files": {
            "obj": portable_path(obj_path, repo),
            "preview": portable_path(preview_path, repo),
            "source_scene": portable_path(scene_path, repo),
        },
        "scene": {
            "uid": scene_data.get("uid"),
            "room_count": len(rooms),
            "rooms": rooms,
            "mesh_count": len(meshes),
            "mesh_type_counts": stats["mesh_type_counts"],
            "material_refs": stats["material_refs"],
        },
        "geometry": {
            "units": "meter",
            "coordinate_system": COORDINATE_TRANSFORMS[variant]["coordinate_system"],
            "bbox": stats["bbox"],
            "size": stats["size"],
            "vertex_count": stats["vertex_count"],
            "face_count": stats["face_count"],
        },
        "normalization": {
            "variant": variant,
            "scene_axis_transform": COORDINATE_TRANSFORMS[variant]["scene_axis_transform"],
            "matrix_4x4_row_major": COORDINATE_TRANSFORMS[variant]["matrix_4x4_row_major"],
        },
        "materials": {"sionna": sorted({item["sionna"] for item in mesh_materials}), "source_to_sionna": mesh_materials},
        "quality": {"ok": not flags, "flags": flags},
    }
    write_json(out_dir / f"{scene_id}.json", payload)
    entry = {
        "id": scene_id,
        "json": portable_path(out_dir / f"{scene_id}.json", repo),
        "obj": portable_path(obj_path, repo),
        "preview": portable_path(preview_path, repo),
        "room_count": len(rooms),
        "mesh_count": len(meshes),
        "quality_ok": not flags,
        "quality_flags": flags,
    }
    failure = None if not flags else {"id": scene_id, "flags": flags}
    return entry, failure


def write_manifest(
    root: Path,
    *,
    asset_kind: str,
    variant: str,
    entries: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    source: Path,
    repo: Path,
) -> dict[str, Any]:
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset": "3d-front",
        "asset_kind": asset_kind,
        "variant": variant,
        "generated_at": now_iso(),
        "source": portable_path(source, repo),
        "root_dir": portable_path(root, repo),
        "count": len(entries),
        "failed_count": len(failures),
        "entries": entries,
        "by_id": {entry["id"]: {"json": entry["json"], "obj": entry["obj"], "preview": entry["preview"]} for entry in entries},
        "failures": failures,
    }
    write_json(root / "manifest.json", manifest)
    return manifest


def build_report(manifests: dict[str, dict[str, Any]]) -> dict[str, Any]:
    flag_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    material_confidence: Counter[str] = Counter()
    for manifest in manifests.values():
        for failure in manifest["failures"]:
            flag_counts.update(failure.get("flags") or [])
        for entry in manifest["entries"]:
            if entry.get("category"):
                category_counts[str(entry["category"])] += 1
            for flag in entry.get("quality_flags") or []:
                flag_counts[flag] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso(),
        "counts": {
            key: {
                "entries": manifest["count"],
                "failed": manifest["failed_count"],
            }
            for key, manifest in manifests.items()
        },
        "quality_flag_counts": dict(flag_counts),
        "category_counts": dict(category_counts),
        "material_confidence_counts": dict(material_confidence),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    repo = repo_root()
    source = args.source
    output = args.output
    future_model_dir = source / "3D-FUTURE-model"
    front_scene_dir = source / "3D-FRONT"
    model_info_path = future_model_dir / "model_info.json"
    texture_info_path = source / "3D-FRONT-texture" / "texture_info.json"
    if not model_info_path.is_file():
        raise FileNotFoundError(f"Missing 3D-FUTURE model_info.json: {model_info_path}")
    if not front_scene_dir.is_dir():
        raise FileNotFoundError(f"Missing 3D-FRONT scene directory: {front_scene_dir}")

    model_infos = read_json(model_info_path)
    scene_paths = sorted(front_scene_dir.glob("*.json"))
    if args.limit_objects is not None:
        model_infos = model_infos[: args.limit_objects]
    if args.limit_scenes is not None:
        scene_paths = scene_paths[: args.limit_scenes]
    if not args.skip_disk_check:
        ensure_disk_space(output, estimate_required_bytes(source, model_infos, scene_paths), args.disk_buffer_gb)

    output.mkdir(parents=True, exist_ok=True)
    dir_map = {
        "objects_raw": output / OBJECT_RAW_DIR,
        "objects_normalized": output / OBJECT_NORMALIZED_DIR,
        "architecture_raw": output / ARCH_RAW_DIR,
        "architecture_normalized": output / ARCH_NORMALIZED_DIR,
    }
    for path in dir_map.values():
        path.mkdir(parents=True, exist_ok=True)

    object_entries: dict[str, list[dict[str, Any]]] = {"raw": [], "normalized": []}
    object_failures: dict[str, list[dict[str, Any]]] = {"raw": [], "normalized": []}
    for item in model_infos:
        for variant, target in (("raw", dir_map["objects_raw"]), ("normalized", dir_map["objects_normalized"])):
            entry, failure = prepare_object_variant(source, target, item, variant, repo)
            if entry:
                object_entries[variant].append(entry)
            if failure:
                object_failures[variant].append(failure)

    architecture_entries: dict[str, list[dict[str, Any]]] = {"raw": [], "normalized": []}
    architecture_failures: dict[str, list[dict[str, Any]]] = {"raw": [], "normalized": []}
    for scene_path in scene_paths:
        for variant, target in (("raw", dir_map["architecture_raw"]), ("normalized", dir_map["architecture_normalized"])):
            entry, failure = prepare_architecture_variant(source, target, scene_path, variant, args.preview_size, repo)
            if entry:
                architecture_entries[variant].append(entry)
            if failure:
                architecture_failures[variant].append(failure)

    manifests = {
        "objects_raw": write_manifest(
            dir_map["objects_raw"],
            asset_kind="object",
            variant="raw",
            entries=object_entries["raw"],
            failures=object_failures["raw"],
            source=source,
            repo=repo,
        ),
        "objects_normalized": write_manifest(
            dir_map["objects_normalized"],
            asset_kind="object",
            variant="normalized",
            entries=object_entries["normalized"],
            failures=object_failures["normalized"],
            source=source,
            repo=repo,
        ),
        "architecture_raw": write_manifest(
            dir_map["architecture_raw"],
            asset_kind="architecture",
            variant="raw",
            entries=architecture_entries["raw"],
            failures=architecture_failures["raw"],
            source=source,
            repo=repo,
        ),
        "architecture_normalized": write_manifest(
            dir_map["architecture_normalized"],
            asset_kind="architecture",
            variant="normalized",
            entries=architecture_entries["normalized"],
            failures=architecture_failures["normalized"],
            source=source,
            repo=repo,
        ),
    }

    report_dir = output / REPORT_DIR
    report = build_report(manifests)
    write_json(report_dir / "phase1_report.json", report)
    texture_info = read_json(texture_info_path) if texture_info_path.is_file() else []
    total_manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset": "3d-front",
        "generated_at": now_iso(),
        "source": portable_path(source, repo),
        "output": portable_path(output, repo),
        "copy_mode": args.copy_mode,
        "scope": args.scope,
        "architecture_granularity": args.architecture_granularity,
        "limits": {"objects": args.limit_objects, "scenes": args.limit_scenes},
        "directories": {key: portable_path(path, repo) for key, path in dir_map.items()},
        "manifests": {key: portable_path(path / "manifest.json", repo) for key, path in dir_map.items()},
        "counts": {
            "model_info": len(model_infos),
            "front_scenes": len(scene_paths),
            "texture_info": len(texture_info),
            **{key: manifest["count"] for key, manifest in manifests.items()},
        },
        "by_model_id": {
            entry["id"]: {
                "raw": manifests["objects_raw"]["by_id"].get(entry["id"]),
                "normalized": manifests["objects_normalized"]["by_id"].get(entry["id"]),
            }
            for entry in object_entries["raw"]
        },
        "by_scene_id": {
            entry["id"]: {
                "raw": manifests["architecture_raw"]["by_id"].get(entry["id"]),
                "normalized": manifests["architecture_normalized"]["by_id"].get(entry["id"]),
            }
            for entry in architecture_entries["raw"]
        },
        "report": portable_path(report_dir / "phase1_report.json", repo),
    }
    write_json(output / "scenegen_manifest.json", total_manifest)
    return total_manifest


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = run(args)
    print(f"objects_raw={manifest['counts']['objects_raw']}")
    print(f"objects_normalized={manifest['counts']['objects_normalized']}")
    print(f"architecture_raw={manifest['counts']['architecture_raw']}")
    print(f"architecture_normalized={manifest['counts']['architecture_normalized']}")
    print(f"manifest={manifest['output']}/scenegen_manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
