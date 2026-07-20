from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .assets import normalize_sionna_material_name, sionna_material_itu_type
from .exporters import (
    build_sionna_scene_xml,
    relative_posix,
    sanitize_path_segment,
    write_clean_obj_full_from_source,
)
from .geometry import clear_obj_mesh_cache, load_obj_mesh
from .models import SceneMeshArrays, SionnaAssetPart, SionnaXmlShape, Vec3
from .paths import portable_path


BLEND_SUFFIX = ".blend"


@dataclass(frozen=True)
class BlendConfig:
    root: Path
    scene_files: tuple[Path, ...] = ()
    scene_selection: str = "sequential"
    start_index: int = 0
    blender_path: str = "blender"
    positive_xy: bool = True
    default_material: str = "itu-concrete"
    export_timeout_s: float | None = 600.0


@dataclass(frozen=True)
class BlendScene:
    scene_id: str
    source_path: Path


@dataclass(frozen=True)
class BlendExportResult:
    exported_obj: Path
    exported_mtl: Path | None
    metadata_path: Path
    stdout_path: Path
    stderr_path: Path
    command: tuple[str, ...]
    elapsed_s: float


@dataclass(frozen=True)
class BlendBuildOutput:
    record: dict[str, object]
    floorplan_mesh_arrays: SceneMeshArrays | None
    bounds_xy: tuple[float, float, float, float]


def discover_blend_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"Blend root directory does not exist: {root}")
    return sorted((path.resolve() for path in root.rglob(f"*{BLEND_SUFFIX}") if path.is_file()), key=lambda item: item.as_posix())


def scene_id_for_blend(path: Path) -> str:
    return sanitize_path_segment(path.stem)


def resolve_blender_executable(blender_path: str) -> str:
    configured = str(blender_path).strip()
    if not configured:
        raise ValueError("blend.blender_path must not be empty")
    if Path(configured).name != configured or "/" in configured:
        executable = Path(configured).expanduser()
        if not executable.is_file():
            raise FileNotFoundError(f"Configured Blender executable does not exist: {executable}")
        return str(executable)
    resolved = shutil.which(configured)
    if resolved is None:
        raise FileNotFoundError(
            f"Blender executable {configured!r} was not found. Install Blender or set blend.blender_path."
        )
    return resolved


class BlendIndex:
    def __init__(self, config: BlendConfig):
        self.config = config
        if config.scene_files:
            files = [path.expanduser().resolve() for path in config.scene_files]
        else:
            files = discover_blend_files(config.root)
        if not files:
            raise FileNotFoundError(f"No .blend files found for blend mode under {config.root}")
        for path in files:
            if path.suffix.lower() != BLEND_SUFFIX:
                raise ValueError(f"blend scene file must end with .blend: {path}")
            if not path.is_file():
                raise FileNotFoundError(f"blend scene file does not exist: {path}")
        self.scenes = tuple(BlendScene(scene_id_for_blend(path), path) for path in files)
        self.scene_ids = tuple(scene.scene_id for scene in self.scenes)
        self.by_scene_id = {scene.scene_id: scene for scene in self.scenes}

    def load_scene(self, scene_id: str) -> BlendScene:
        try:
            return self.by_scene_id[scene_id]
        except KeyError as exc:
            raise KeyError(f"Unknown blend scene id: {scene_id}") from exc


def blender_export_script(output_obj: Path, metadata_path: Path) -> str:
    output_json = json.dumps(str(output_obj))
    metadata_json = json.dumps(str(metadata_path))
    return textwrap.dedent(
        f"""
        import json
        import traceback
        from pathlib import Path

        import bpy

        output_obj = Path({output_json})
        metadata_path = Path({metadata_json})
        output_obj.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)

        selected = []
        for obj in bpy.context.scene.objects:
            try:
                visible = obj.visible_get()
            except Exception:
                visible = not obj.hide_get()
            if obj.type == "MESH" and visible:
                selected.append(obj)

        bpy.ops.object.select_all(action="DESELECT")
        for obj in selected:
            obj.select_set(True)
        if selected:
            bpy.context.view_layer.objects.active = selected[0]

        try:
            if not selected:
                raise RuntimeError("No visible mesh objects found in the .blend file")
            if hasattr(bpy.ops.wm, "obj_export"):
                bpy.ops.wm.obj_export(
                    filepath=str(output_obj),
                    export_selected_objects=True,
                    export_materials=True,
                    apply_modifiers=True,
                    export_triangulated_mesh=True,
                    forward_axis="Y",
                    up_axis="Z",
                )
            else:
                bpy.ops.export_scene.obj(
                    filepath=str(output_obj),
                    use_selection=True,
                    use_materials=True,
                    use_mesh_modifiers=True,
                    use_triangles=True,
                    axis_forward="Y",
                    axis_up="Z",
                )
        except Exception as exc:
            metadata_path.write_text(
                json.dumps(
                    {{
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                        "mesh_object_count": len(selected),
                    }},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            raise

        metadata_path.write_text(
            json.dumps(
                {{
                    "ok": True,
                    "mesh_object_count": len(selected),
                    "mesh_objects": [
                        {{"name": obj.name, "type": obj.type, "material_count": len(obj.material_slots)}}
                        for obj in selected
                    ],
                    "output_obj": str(output_obj),
                }},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        """
    ).strip() + "\n"


def run_blender_export(scene: BlendScene, output_dir: Path, config: BlendConfig) -> BlendExportResult:
    source_dir = output_dir / "blend_source"
    source_dir.mkdir(parents=True, exist_ok=True)
    exported_obj = source_dir / "exported.obj"
    metadata_path = source_dir / "export_metadata.json"
    script_path = source_dir / "export_blend_to_obj.py"
    stdout_path = source_dir / "blender_stdout.log"
    stderr_path = source_dir / "blender_stderr.log"
    script_path.write_text(blender_export_script(exported_obj, metadata_path), encoding="utf-8")
    blender_executable = resolve_blender_executable(config.blender_path)
    command = (
        blender_executable,
        "--background",
        str(scene.source_path),
        "--python",
        str(script_path),
    )
    started = time.perf_counter()
    process = subprocess.run(
        list(command),
        text=True,
        capture_output=True,
        check=False,
        timeout=config.export_timeout_s,
    )
    elapsed_s = time.perf_counter() - started
    stdout_path.write_text(process.stdout or "", encoding="utf-8")
    stderr_path.write_text(process.stderr or "", encoding="utf-8")
    if process.returncode != 0:
        raise RuntimeError(
            f"Blender export failed for {scene.source_path} with exit code {process.returncode}. "
            f"See {stderr_path}"
        )
    if not exported_obj.is_file():
        raise RuntimeError(f"Blender export did not create OBJ: {exported_obj}")
    exported_mtl = exported_obj.with_suffix(".mtl")
    return BlendExportResult(
        exported_obj=exported_obj,
        exported_mtl=exported_mtl if exported_mtl.is_file() else None,
        metadata_path=metadata_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        command=command,
        elapsed_s=elapsed_s,
    )


def _parse_obj_vertex(line: str) -> tuple[float, float, float] | None:
    parts = line.split()
    if len(parts) < 4 or parts[0] != "v":
        return None
    return float(parts[1]), float(parts[2]), float(parts[3])


def obj_bbox(path: Path) -> tuple[Vec3, Vec3]:
    mins = [float("inf"), float("inf"), float("inf")]
    maxs = [float("-inf"), float("-inf"), float("-inf")]
    vertex_count = 0
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if not line.startswith("v "):
                continue
            vertex = _parse_obj_vertex(line)
            if vertex is None:
                continue
            vertex_count += 1
            for index, value in enumerate(vertex):
                mins[index] = min(mins[index], value)
                maxs[index] = max(maxs[index], value)
    if vertex_count == 0:
        raise ValueError(f"OBJ has no vertices: {path}")
    return (mins[0], mins[1], mins[2]), (maxs[0], maxs[1], maxs[2])


def write_normalized_scene_obj(source_obj: Path, target_obj: Path, positive_xy: bool) -> tuple[Vec3, Vec3, Vec3, Vec3, Vec3]:
    source_bbox_min, source_bbox_max = obj_bbox(source_obj)
    world_offset = (
        -source_bbox_min[0] if positive_xy else 0.0,
        -source_bbox_min[1] if positive_xy else 0.0,
        0.0,
    )
    target_obj.parent.mkdir(parents=True, exist_ok=True)
    with source_obj.open("r", encoding="utf-8", errors="ignore") as source, target_obj.open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as target:
        target.write("# Generated by SceneGen from Blender .blend\n")
        for line in source:
            if line.startswith("mtllib "):
                if (source_obj.with_suffix(".mtl")).is_file():
                    target.write("mtllib scene.mtl\n")
                continue
            if line.startswith("v "):
                vertex = _parse_obj_vertex(line)
                if vertex is None:
                    continue
                x, y, z = vertex
                target.write(f"v {x + world_offset[0]:.6f} {y + world_offset[1]:.6f} {z + world_offset[2]:.6f}\n")
            else:
                target.write(line if line.endswith("\n") else f"{line}\n")
    source_mtl = source_obj.with_suffix(".mtl")
    if source_mtl.is_file():
        shutil.copy2(source_mtl, target_obj.with_suffix(".mtl"))
    clear_obj_mesh_cache()
    bbox_min, bbox_max = obj_bbox(target_obj)
    return source_bbox_min, source_bbox_max, bbox_min, bbox_max, world_offset


def mesh_arrays_from_obj(path: Path, source: str) -> SceneMeshArrays:
    mesh = load_obj_mesh(path)
    triangles: list[tuple[int, int, int]] = []
    for face in mesh.faces:
        first = int(face[0])
        for index in range(1, len(face) - 1):
            triangles.append((first, int(face[index]), int(face[index + 1])))
    return SceneMeshArrays(
        vertices=np.asarray(mesh.vertices, dtype=np.float32),
        faces=np.asarray(triangles, dtype=np.int32).reshape((-1, 3)),
        source=source,
    )


def write_blend_sionna_assets(
    output_dir: Path,
    scene_obj: Path,
    material_name: str,
    write_sionna_assets: bool,
) -> dict[str, object]:
    material_name = normalize_sionna_material_name(material_name)
    if not write_sionna_assets:
        return {
            "shape_count": 0,
            "materials": [],
            "asset_parts": [],
            "skipped": True,
            "reason": "output.write_sionna_assets=false",
        }

    mesh = load_obj_mesh(scene_obj)
    assets_dir = output_dir / "assets" / "blend"
    material_suffix = sanitize_path_segment(sionna_material_itu_type(material_name))
    asset_path = assets_dir / f"scene__{material_suffix}.obj"
    write_clean_obj_full_from_source(asset_path, f"{output_dir.name}_{material_suffix}", scene_obj)
    part = SionnaAssetPart(
        filename=relative_posix(asset_path, output_dir),
        material_name=material_name,
        face_count=len(mesh.faces),
    )
    shape = SionnaXmlShape(
        shape_id=f"blend_{sanitize_path_segment(output_dir.name)}_{material_suffix}",
        filename=part.filename,
        material_name=material_name,
    )
    (output_dir / "scene.xml").write_text(build_sionna_scene_xml([shape]), encoding="utf-8")
    return {
        "assets_dir": portable_path(output_dir / "assets", output_dir),
        "scene_xml": portable_path(output_dir / "scene.xml", output_dir),
        "shape_count": 1,
        "asset_file_count": 1,
        "materials": [material_name],
        "base_scene_parts": [
            {
                "asset_obj": part.filename,
                "sionna_material_name": part.material_name,
                "itu_type": sionna_material_itu_type(part.material_name),
                "face_count": part.face_count,
            }
        ],
    }


def blend_scene_summary(
    scene: BlendScene,
    *,
    vertex_count: int,
    face_count: int,
    source_bbox_min: Vec3,
    source_bbox_max: Vec3,
    bbox_min: Vec3,
    bbox_max: Vec3,
    world_offset: Vec3,
    export: BlendExportResult,
) -> dict[str, object]:
    bbox_size = [float(bbox_max[index] - bbox_min[index]) for index in range(3)]
    source_bbox_size = [float(source_bbox_max[index] - source_bbox_min[index]) for index in range(3)]
    return {
        "scene_id": scene.scene_id,
        "source_file": str(scene.source_path),
        "vertex_count": int(vertex_count),
        "face_count": int(face_count),
        "bbox_min": [round(value, 6) for value in bbox_min],
        "bbox_max": [round(value, 6) for value in bbox_max],
        "bbox_size_m": [round(value, 6) for value in bbox_size],
        "xy_area_m2": round(max(0.0, bbox_size[0]) * max(0.0, bbox_size[1]), 6),
        "source_bbox_min": [round(value, 6) for value in source_bbox_min],
        "source_bbox_max": [round(value, 6) for value in source_bbox_max],
        "source_bbox_size_m": [round(value, 6) for value in source_bbox_size],
        "world_offset": [round(value, 6) for value in world_offset],
        "export": {
            "elapsed_s": round(export.elapsed_s, 6),
            "exported_obj": portable_path(export.exported_obj, export.exported_obj.parent.parent.parent),
            "exported_mtl": portable_path(export.exported_mtl, export.exported_obj.parent.parent.parent)
            if export.exported_mtl is not None
            else None,
            "metadata": portable_path(export.metadata_path, export.exported_obj.parent.parent.parent),
            "stdout": portable_path(export.stdout_path, export.exported_obj.parent.parent.parent),
            "stderr": portable_path(export.stderr_path, export.exported_obj.parent.parent.parent),
            "command": list(export.command),
        },
    }


def write_blend_source_metadata(output_dir: Path, scene: BlendScene, config: BlendConfig, summary: dict[str, object]) -> str:
    source_dir = output_dir / "blend_source"
    source_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = source_dir / "metadata.json"
    payload = {
        "scene_id": scene.scene_id,
        "source_file": str(scene.source_path),
        "blender_path": str(config.blender_path),
        "positive_xy": bool(config.positive_xy),
        "default_material": str(config.default_material),
        "summary": summary,
    }
    metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return portable_path(metadata_path, output_dir.parent)


def write_blend_scene_files(
    output_dir: Path,
    scene: BlendScene,
    scene_index: int,
    seed: int,
    config: BlendConfig,
    collect_floorplan_mesh_arrays: bool = True,
    write_scene_obj: bool = True,
    write_sionna_assets: bool = True,
) -> BlendBuildOutput:
    output_dir.mkdir(parents=True, exist_ok=True)
    timings_s: dict[str, float] = {}
    started = time.perf_counter()
    export = run_blender_export(scene, output_dir, config)
    timings_s["export_blend"] = round(time.perf_counter() - started, 6)

    started = time.perf_counter()
    scene_obj = output_dir / "scene.obj"
    source_bbox_min, source_bbox_max, bbox_min, bbox_max, world_offset = write_normalized_scene_obj(
        export.exported_obj,
        scene_obj,
        config.positive_xy,
    )
    timings_s["write_scene_obj"] = round(time.perf_counter() - started, 6)

    mesh = load_obj_mesh(scene_obj)
    summary = blend_scene_summary(
        scene,
        vertex_count=len(mesh.vertices),
        face_count=len(mesh.faces),
        source_bbox_min=source_bbox_min,
        source_bbox_max=source_bbox_max,
        bbox_min=bbox_min,
        bbox_max=bbox_max,
        world_offset=world_offset,
        export=export,
    )
    started = time.perf_counter()
    sionna_assets = write_blend_sionna_assets(output_dir, scene_obj, config.default_material, write_sionna_assets)
    timings_s["write_sionna_assets"] = round(time.perf_counter() - started, 6)

    source_metadata = write_blend_source_metadata(output_dir, scene, config, summary)
    floorplan_mesh_arrays = (
        mesh_arrays_from_obj(scene_obj, source="blend")
        if collect_floorplan_mesh_arrays and scene_obj.is_file()
        else None
    )
    if not write_scene_obj:
        scene_obj.unlink(missing_ok=True)
        (output_dir / "scene.mtl").unlink(missing_ok=True)
    placement_payload = {
        "scene_index": scene_index,
        "seed": seed,
        "mode": "blend",
        "blend_scene_id": scene.scene_id,
        "blend": summary,
        "source_metadata": source_metadata,
        "sionna_assets": sionna_assets,
        "placement_count": 0,
        "placements": [],
    }
    (output_dir / "placements.json").write_text(json.dumps(placement_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    record: dict[str, object] = {
        "scene_index": scene_index,
        "seed": seed,
        "mode": "blend",
        "scene_dir": portable_path(output_dir, output_dir.parent),
        "scene_obj": portable_path(scene_obj, output_dir.parent) if write_scene_obj else None,
        "scene_xml": portable_path(output_dir / "scene.xml", output_dir.parent) if write_sionna_assets else None,
        "assets_dir": portable_path(output_dir / "assets", output_dir.parent) if write_sionna_assets else None,
        "sionna_shape_count": sionna_assets["shape_count"],
        "sionna_materials": sionna_assets["materials"],
        "placement_count": 0,
        "blend_scene_id": scene.scene_id,
        "blend": summary,
        "source_scene": portable_path(scene.source_path, output_dir.parent),
        "source_metadata": source_metadata,
        "skipped_object_count": 0,
        "skipped_objects": [],
        "build_timings_s": timings_s,
    }
    if not write_sionna_assets:
        record["sionna_assets_skipped"] = True
    bounds_xy = (float(bbox_min[0]), float(bbox_min[1]), float(bbox_max[0]), float(bbox_max[1]))
    return BlendBuildOutput(record=record, floorplan_mesh_arrays=floorplan_mesh_arrays, bounds_xy=bounds_xy)
