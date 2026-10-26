"""Regenerate architecture-only Front3D floorplans on their original metric canvases."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from PIL import Image

from ..config import DEFAULT_CONFIG, deep_merge
from ..floorplan import (
    FloorplanConfig,
    ProjectionFrame,
    floorplan_layer_filename,
    generate_front3d_class_mask,
    process_scene,
)
from ..front3d import STRUCTURAL_MESH_TYPES, Front3DConfig, Front3DIndex, collect_front3d_structural_mesh_arrays
from ..paths import find_project_root


SCHEMA_VERSION = "scenegen.empty_floorplans.v1"
FRAME_FIELDS = (
    "resolution_m_per_pixel", "origin_xy_m", "extent_xy_m", "grid_shape",
    "vertical_axis_original", "effective_ground_m_original_units", "effective_top_m_original_units",
    "height_mode", "z_levels_m", "scan_bottom_z_m", "scan_top_z_m", "step_m", "projection_mode",
)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_mapping(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError("Scene mapping is empty")
    keys = [row["scene_key"] for row in rows]
    ids = [row["front3d_scene_id"] for row in rows]
    if any(not isinstance(key, str) or re.fullmatch(r"front3d_\d{4,}", key) is None for key in keys):
        raise ValueError("Unsafe or invalid scene_key; expected front3d_NNNN")
    if len(set(keys)) != len(rows) or len(set(ids)) != len(rows):
        raise ValueError("Scene keys and Front3D scene IDs must both be unique")
    return rows


def load_source_config(source_run: Path, repo_root: Path) -> tuple[dict[str, Any], Front3DConfig, FloorplanConfig]:
    source = yaml.safe_load((source_run / "effective_config.yaml").read_text(encoding="utf-8"))
    if source["pipeline"]["mode"] != "front3d":
        raise ValueError("Architecture-only regeneration requires a front3d source run")
    front = deep_merge(DEFAULT_CONFIG["front3d"], source["front3d"])
    floor = deep_merge(DEFAULT_CONFIG["floorplan"], source["floorplan"])
    if floor["geometry"]["vertical_axis"] == "auto":
        floor["geometry"]["vertical_axis"] = "z"
    if not floor["enabled"] or not floor["geometry"]["enabled"]:
        raise ValueError("Source run must have geometry floorplans enabled")

    def resolve(value: str) -> Path:
        path = Path(value).expanduser()
        return (path if path.is_absolute() else repo_root / path).resolve()

    index_config = Front3DConfig(
        manifest=resolve(front["manifest"]), source_scene_dir=resolve(front["source_dir"]),
        variant=front["arch_variant"], object_variant=front["object_variant"], scene_ids=(),
        scene_selection="sequential", start_index=0, use_replace_jid=front["use_replace_jid"],
        skip_missing_objects=front["skip_missing_objects"], normalize_positive_xy=front["positive_xy"],
        ground_objects=front["ground"],
    )
    return source, index_config, FloorplanConfig.from_mapping(floor, front["openings"])


def prepare_scene(row: dict[str, Any], source_run: Path, index: Front3DIndex) -> dict[str, Any]:
    key, scene_id = row["scene_key"], row["front3d_scene_id"]
    source_scene = source_run / key
    placements = read_json(source_scene / "placements.json")
    record = read_json(source_scene / "scene_record_batch.json")
    if placements["front3d_scene_id"] != scene_id or record["front3d_scene_id"] != scene_id:
        raise ValueError(f"Source UUID does not match mapping for {key}")
    if record["scene_index"] != int(key.rsplit("_", 1)[1]):
        raise ValueError(f"Source scene index does not match key for {key}")
    base = index.base_scene(scene_id)
    saved_architecture = placements["architecture"]
    for field in ("bbox_min", "bbox_max", "world_offset"):
        if not np.allclose(getattr(base, field), saved_architecture[field], rtol=0, atol=1e-9):
            raise ValueError(f"Architecture {field} changed since original generation: {key}")
    meta_path = source_scene / "floorplan" / "meta.json"
    meta = read_json(meta_path)
    frame = ProjectionFrame.from_metadata(meta)
    layer = floorplan_layer_filename(meta["z_levels_m"][0])
    with Image.open(source_scene / "floorplan" / layer) as image:
        if image.size[::-1] != frame.shape:
            raise ValueError(f"Original image size differs from metadata for {key}")
    inputs = [meta_path, source_scene / "placements.json", source_scene / "scene_record_batch.json",
              base.scene_obj, base.metadata_json, base.source_scene_json]
    mask_meta_path = source_scene / "floorplan" / "class_mask_meta.json"
    if mask_meta_path.exists():
        inputs.extend([mask_meta_path, source_scene / "floorplan" / "class_mask.npy"])
    input_hashes = {str(path): sha256(path) for path in inputs}
    fingerprint = hashlib.sha256(json.dumps(input_hashes, sort_keys=True).encode()).hexdigest()
    return {**row, "source_fingerprint": fingerprint, "source_sha256": input_hashes}


def validate_scene(scene_dir: Path, source_scene: Path, config: FloorplanConfig) -> dict[str, Any]:
    record_path = scene_dir / "record.json"
    if record_path.exists():
        for name, digest in read_json(record_path)["output_sha256"].items():
            if sha256(scene_dir / name) != digest:
                raise ValueError(f"Completed scene checksum changed: {scene_dir.name}/{name}")
    meta = read_json(scene_dir / "floorplan" / "meta.json")
    reference = read_json(source_scene / "floorplan" / "meta.json")
    for field in FRAME_FIELDS:
        if meta[field] != reference[field]:
            raise ValueError(f"Regenerated {field} differs from original: {scene_dir.name}")
    frame = ProjectionFrame.from_metadata(meta)
    for level in reference["z_levels_m"]:
        with Image.open(scene_dir / "floorplan" / floorplan_layer_filename(level)) as image:
            image.load()
            if image.size[::-1] != frame.shape:
                raise ValueError("Regenerated image grid differs from reference")
    with np.load(scene_dir / "floorplan" / "stack.npz") as stack:
        if stack["stack"].shape != (len(reference["z_levels_m"]), *frame.shape):
            raise ValueError("Regenerated projection stack has wrong shape")
    checks: dict[str, Any] = {"frame_matches_original": True, "image_and_stack_shapes_match": True}
    if config.class_mask_enabled:
        mask_meta = read_json(scene_dir / "floorplan" / "class_mask_meta.json")
        original_meta = read_json(source_scene / "floorplan" / "class_mask_meta.json")
        for field in ("resolution_m_per_pixel", "origin_xy_m", "extent_xy_m", "grid_shape", "world_offset"):
            if mask_meta[field] != original_meta[field]:
                raise ValueError(f"Class mask {field} differs from original")
        mask = np.load(scene_dir / "floorplan" / "class_mask.npy", allow_pickle=False)
        original = np.load(source_scene / "floorplan" / "class_mask.npy", allow_pickle=False)
        expected = np.where(original == 3, 2, original)
        if not np.array_equal(mask, expected):
            raise ValueError("Empty mask changed architecture/outdoor pixels, or retained furniture")
        checks.update({"furniture_pixels": int((mask == 3).sum()), "architecture_mask_matches_original": True})
    return checks


def regenerate_scene(
    row: dict[str, Any], source_run: Path, output_dir: Path, index_config: Front3DConfig,
    floor_config: FloorplanConfig, repo_root: Path,
) -> dict[str, Any]:
    started = time.perf_counter()
    key, scene_id = row["scene_key"], row["front3d_scene_id"]
    source_scene = source_run / key
    base = Front3DIndex(index_config, repo_root).base_scene(scene_id)
    original_architecture = read_json(source_scene / "placements.json")["architecture"]
    base = replace(base, **{field: tuple(original_architecture[field])
                           for field in ("bbox_min", "bbox_max", "world_offset")})
    arrays, mesh_selection = collect_front3d_structural_mesh_arrays(base)
    seed = int.from_bytes(hashlib.sha256(f"{SCHEMA_VERSION}:{scene_id}".encode()).digest()[:8], "little")
    staging_root = output_dir / ".staging"
    staging_root.mkdir(exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f"{key}.", dir=staging_root))
    try:
        floor_dir = temporary / "floorplan"
        reference_meta = read_json(source_scene / "floorplan" / "meta.json")
        process_scene(
            input_path=temporary / "scene.obj", output_dir=floor_dir,
            resolution=floor_config.resolution_m_per_pixel, step=floor_config.step_m,
            manual_top_z=floor_config.top_z_m, bottom_z=floor_config.bottom_z_m,
            sample_density_scale=floor_config.sample_density_scale,
            min_sample_points=floor_config.min_sample_points, max_sample_points=floor_config.max_sample_points,
            preview_output_path=floor_dir / "preview.png", side_view_output_path=floor_dir / "side_view.png",
            preview_tile_size=floor_config.preview_tile_size_px, height_mode=floor_config.height_mode,
            heights_m=floor_config.heights_m, projection_mode=floor_config.geometry_projection,
            vertical_axis=floor_config.geometry_vertical_axis, scene_mesh_arrays=arrays,
            reference_meta=reference_meta, sample_seed=seed,
        )
        if floor_config.class_mask_enabled:
            generate_front3d_class_mask(
                output_dir=floor_dir, base_scene=base, placements=[], resolution=floor_config.resolution_m_per_pixel,
                wall_dilation_m=floor_config.class_mask_wall_dilation_m,
                furniture_dilation_m=floor_config.class_mask_furniture_dilation_m,
                furniture_mode=floor_config.class_mask_furniture_mode,
                furniture_height_m=floor_config.class_mask_furniture_height_m,
                opening_mode=floor_config.openings.mode, opening_dilation_m=floor_config.openings.dilation_m,
                opening_floor_tolerance_m=floor_config.openings.floor_tolerance_m,
                opening_min_height_m=floor_config.openings.min_height_m,
                include_doors_as_wall=floor_config.openings.include_doors_as_wall,
                include_windows_as_wall=floor_config.openings.include_windows_as_wall,
                path_root=temporary,
                reference_frame=ProjectionFrame.from_metadata(
                    read_json(source_scene / "floorplan" / "class_mask_meta.json")
                ),
            )
        checks = validate_scene(temporary, source_scene, floor_config)
        result = {
            **row, "schema_version": SCHEMA_VERSION, "content": "architecture_only", "placement_count": 0,
            "sample_seed": seed, "source_scene_dir": str(source_scene),
            "world_offset": list(base.world_offset), "architecture_obj": str(base.scene_obj),
            "mesh_selection": mesh_selection,
            "architecture_vertex_count": len(arrays.vertices), "architecture_triangle_count": len(arrays.faces),
            "image_path": f"images/{key}.png", "floorplan_dir": f"{key}/floorplan",
            "validation": checks, "elapsed_s": round(time.perf_counter() - started, 3),
        }
        # Store paths relative to the final scene, never to its temporary directory.
        meta_path = floor_dir / "meta.json"
        meta = read_json(meta_path)
        meta.update({"scene_key": key, "front3d_scene_id": scene_id, "content": "architecture_only",
                     "source": str(base.scene_obj), "prepared_mesh": str(base.scene_obj),
                     "source_architecture_obj": str(base.scene_obj), "source_reference_meta": str(
                         source_scene / "floorplan" / "meta.json")})
        write_json(meta_path, meta)
        if floor_config.class_mask_enabled:
            mask_meta_path = floor_dir / "class_mask_meta.json"
            mask_meta = read_json(mask_meta_path)
            mask_meta.update({"source_scene": str(base.source_scene_json),
                              "source_metadata": str(base.metadata_json)})
            write_json(mask_meta_path, mask_meta)
        result["output_sha256"] = {
            path.relative_to(temporary).as_posix(): sha256(path) for path in sorted(floor_dir.iterdir()) if path.is_file()
        }
        write_json(temporary / "record.json", result)
        os.rename(temporary, output_dir / key)
        return result
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def run_regeneration(
    mapping_path: Path, source_run: Path, output_dir: Path, workers: int = 4, resume: bool = False,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    repo_root = (repo_root or find_project_root()).resolve()
    mapping_path, source_run, output_dir = mapping_path.resolve(), source_run.resolve(), output_dir.resolve()
    if workers < 1:
        raise ValueError("workers must be positive")
    if output_dir == source_run or output_dir.is_relative_to(source_run) or source_run.is_relative_to(output_dir):
        raise ValueError("Output must be separate from the original source run")
    rows = load_mapping(mapping_path)
    source, index_config, floor_config = load_source_config(source_run, repo_root)
    index = Front3DIndex(index_config, repo_root)
    plan = [prepare_scene(row, source_run, index) for row in rows]
    run_contract = {
        "schema_version": SCHEMA_VERSION, "source_run": str(source_run), "mapping_path": str(mapping_path),
        "mapping_sha256": sha256(mapping_path), "source_config_sha256": sha256(source_run / "effective_config.yaml"),
        "content": "architecture_only", "reference_frame": "locked_to_original_per_scene_metadata",
        "structural_mesh_types": sorted(STRUCTURAL_MESH_TYPES),
        "sample_seed_policy": "SHA256(schema_version + ':' + front3d_scene_id), first 8 bytes little-endian",
        "front3d_manifest": str(index_config.manifest), "floorplan": asdict(floor_config),
        "implementation_sha256": {name: sha256(Path(__file__).resolve().parents[1] / name)
                                  for name in ("floorplan.py", "front3d.py", "postprocess/empty_floorplans.py")},
        "scene_sources": {row["scene_key"]: row["source_fingerprint"] for row in plan},
    }
    if output_dir.exists() and any(output_dir.iterdir()):
        if not resume:
            raise FileExistsError(f"Output exists; use --resume to validate and reuse completed scenes: {output_dir}")
        if read_json(output_dir / "run_contract.json") != run_contract:
            raise ValueError("Resume contract differs from source config/mapping or scene inputs")
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "run_contract.json", run_contract)
    (output_dir / "effective_config.yaml").write_text(yaml.safe_dump({
        "schema_version": SCHEMA_VERSION, "content": "architecture_only", "source_run": str(source_run),
        "workers": workers, "floorplan": source["floorplan"], "front3d": source["front3d"],
        "reference_frame": "locked_to_original_per_scene_metadata",
    }, sort_keys=False), encoding="utf-8")
    shutil.copy2(source_run / "effective_config.yaml", output_dir / "source_effective_config.yaml")
    shutil.copy2(mapping_path, output_dir / "input_scene_mapping.jsonl")
    results: dict[str, dict[str, Any]] = {}
    pending = []
    for row in plan:
        scene_dir = output_dir / row["scene_key"]
        if scene_dir.exists():
            record = read_json(scene_dir / "record.json")
            if record["source_fingerprint"] != row["source_fingerprint"] or record["front3d_scene_id"] != row["front3d_scene_id"]:
                raise ValueError(f"Completed scene identity differs: {row['scene_key']}")
            validate_scene(scene_dir, source_run / row["scene_key"], floor_config)
            results[row["scene_key"]] = record
        else:
            pending.append(row)
    failures = []
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    with (logs_dir / "events.jsonl").open("a", encoding="utf-8", buffering=1) as events:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(regenerate_scene, row, source_run, output_dir, index_config,
                                       floor_config, repo_root): row for row in pending}
            for future in as_completed(futures):
                row = futures[future]
                event = {"scene_key": row["scene_key"], "timestamp": datetime.now(UTC).isoformat()}
                try:
                    result = future.result()
                    results[row["scene_key"]] = result
                    event.update({"status": "completed", "elapsed_s": result["elapsed_s"]})
                except Exception as exc:
                    event.update({"status": "failed", "error": str(exc), "error_type": type(exc).__name__})
                    failures.append(event)
                events.write(json.dumps(event, ensure_ascii=False) + "\n")
                print(f"[{len(results)}/{len(plan)}] {row['scene_key']} {event['status']}", flush=True)
    ordered = [results[row["scene_key"]] for row in plan if row["scene_key"] in results]
    images = output_dir / "images"
    images.mkdir(exist_ok=True)
    for row in ordered:
        meta = read_json(output_dir / row["floorplan_dir"] / "meta.json")
        image = output_dir / row["floorplan_dir"] / floorplan_layer_filename(meta["z_levels_m"][0])
        shutil.copy2(image, output_dir / row["image_path"])
    (output_dir / "manifest.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in ordered), encoding="utf-8",
    )
    summary = {"schema_version": SCHEMA_VERSION, "expected": len(plan), "completed": len(ordered),
               "failed": len(failures), "failures": failures, "elapsed_s": round(time.perf_counter() - started, 3),
               "all_validated": len(ordered) == len(plan) and not failures, "source_run": str(source_run),
               "output_dir": str(output_dir)}
    write_json(output_dir / "summary.json", summary)
    (output_dir / "README.md").write_text(
        f"# Empty-room test floorplans\n\n"
        f"Completed: {len(ordered)} / {len(plan)}. See `summary.json` for validation status.\n\n"
        "Use `images/<original_scene_key>.png` for the primary floorplan. "
        "`manifest.jsonl` retains the original scene key, 3D-FRONT UUID, canonical order, and source paths.\n\n"
        "Each `<scene_key>/floorplan/` contains every original height layer, stack, metadata, previews, "
        "and an empty class mask when enabled in the source configuration. "
        "The original metric frame, ground height, projection, resolution, and height levels are preserved.\n\n"
        "Only explicitly structural mesh types are projected. Cabinets, customized furniture, lighting, "
        "unclassified meshes and all furniture instances are excluded; every scene's `record.json` "
        "lists retained/excluded mesh types and output checksums. See `run_contract.json` for the exact policy.\n\n"
        "The original sampling projection is retained. Its random points cannot reproduce the old "
        "unseeded sample exactly; the new run uses recorded per-UUID seeds for repeatability.\n\n"
        "Resume with the same command plus `--resume`. Resume checks source fingerprints, code/config/mapping "
        "identity, completed-file checksums, and reference-frame alignment.\n\n"
        f"Original run: `{source_run}`\n\nMapping: `{mapping_path}`\n",
        encoding="utf-8",
    )
    files = sorted(path for path in output_dir.rglob("*") if path.is_file() and path.name != "checksums.sha256")
    (output_dir / "checksums.sha256").write_text(
        "".join(f"{sha256(path)}  {path.relative_to(output_dir).as_posix()}\n" for path in files), encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping", type=Path, required=True, help="JSONL with scene_key and front3d_scene_id")
    parser.add_argument("--source-run", type=Path, required=True, help="Original Front3D run with effective_config.yaml")
    parser.add_argument("--output-dir", type=Path, required=True, help="New architecture-only floorplan run")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--resume", action="store_true", help="Check original inputs and validate every reused scene")
    args = parser.parse_args(argv)
    summary = run_regeneration(args.mapping, args.source_run, args.output_dir, args.workers, args.resume)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["all_validated"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
