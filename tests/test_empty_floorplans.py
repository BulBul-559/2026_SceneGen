from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
import trimesh
import yaml

from scenegen.config import DEFAULT_CONFIG
from scenegen.floorplan import ProjectionFrame, generate_front3d_class_mask, process_scene, sample_surface_points_numpy
from scenegen.front3d import Front3DIndex
from scenegen.models import SceneMeshArrays
from scenegen.postprocess.empty_floorplans import (
    load_mapping,
    load_source_config,
    regenerate_scene,
    run_regeneration,
    validate_scene,
    write_json,
)


def make_source(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "original"
    source.mkdir()
    vertices = np.array([[0, 0, -0.1], [2, 0, -0.1], [2, 2, -0.1], [0, 2, -0.1],
                         [0, 0, 2.9], [2, 0, 2.9]], dtype=np.float32)
    faces = np.array([[0, 1, 2], [0, 2, 3], [0, 1, 5], [0, 5, 4]])
    architecture = tmp_path / "architecture.obj"
    architecture.write_text(
        "".join(f"v {x} {y} {z}\n" for x, y, z in vertices)
        + "".join("f " + " ".join(str(i + 1) for i in face) + "\n" for face in faces), encoding="utf-8",
    )
    metadata = tmp_path / "architecture.json"
    write_json(metadata, {"variant": "normalized", "files": {"obj": str(architecture)},
                          "geometry": {"bbox": {"min": [0, 0, -0.1], "max": [2, 2, 2.9]}}})
    # Raw Front3D is Y-up. A floor only is sufficient for the semantic mask fixture.
    raw_mesh = {"type": "Floor", "xyz": [0, -0.1, 0, 2, -0.1, 0, 2, -0.1, -2, 0, -0.1, -2],
                "faces": [0, 1, 2, 0, 2, 3]}
    raw_wall = {"type": "WallInner", "xyz": [0, -0.1, 0, 2, -0.1, 0, 2, 2.9, 0, 0, 2.9, 0],
                "faces": [0, 1, 2, 0, 2, 3]}
    cabinet = {"type": "Cabinet", "xyz": [4, 0.5, 0, 5, 0.5, 0, 5, 0.5, -1], "faces": [0, 1, 2]}
    fixed_furniture = {**cabinet, "type": "CustomizedFixedFurniture"}
    unknown = {**cabinet, "type": "Unknown"}
    entries = {}
    rows = []
    for number in (42, 7):
        key, scene_id = f"front3d_{number:04d}", f"00000000-0000-0000-0000-{number:012d}"
        write_json(tmp_path / f"{scene_id}.json", {"mesh": [raw_mesh, raw_wall, cabinet, fixed_furniture, unknown]})
        entries[scene_id] = {"normalized": {"json": str(metadata)}}
        rows.append({"scene_key": key, "front3d_scene_id": scene_id})
        scene_dir = source / key
        scene_dir.mkdir()
        write_json(scene_dir / "scene_record_batch.json", {"scene_index": number, "front3d_scene_id": scene_id})
        write_json(scene_dir / "placements.json", {
            "front3d_scene_id": scene_id, "architecture": {"bbox_min": [0, 0, -0.1], "bbox_max": [2, 2, 2.9],
                                                         "world_offset": [0, 0, 0]},
            "placements": [{"model": "missing-furniture-model.obj"}],
        })
        # Furniture extends beyond architecture, testing preservation of the larger original canvas.
        full_vertices = np.concatenate([vertices, [[2.5, 0.5, 0.4], [3, 0.5, 0.4], [3, 1, 0.4]]])
        full_faces = np.concatenate([faces, [[6, 7, 8]]])
        process_scene(
            input_path=scene_dir / "scene.obj", output_dir=scene_dir / "floorplan", resolution=0.25,
            step=0.2, manual_top_z=None, bottom_z=0, sample_density_scale=1,
            min_sample_points=1000, max_sample_points=1000,
            preview_output_path=scene_dir / "floorplan/preview.png",
            side_view_output_path=scene_dir / "floorplan/side_view.png", preview_tile_size=80,
            height_mode="heights", heights_m=[1.6], projection_mode="sampling", vertical_axis="z",
            scene_mesh_arrays=SceneMeshArrays(full_vertices, full_faces, "fixture"), sample_seed=5,
        )
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"by_scene_id": entries})
    config = deepcopy(DEFAULT_CONFIG)
    config["pipeline"]["mode"] = "front3d"
    config["front3d"].update({"manifest": str(manifest), "source_dir": str(tmp_path)})
    config["floorplan"]["resolution_m"] = 0.25
    config["floorplan"]["sampling"].update({"density_scale": 1, "min_points": 1000, "max_points": 1000})
    config["floorplan"]["geometry"].update({"projection": "sampling", "vertical_axis": "z"})
    config["floorplan"]["class_mask"]["enabled"] = False
    (source / "effective_config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    mapping = tmp_path / "mapping.jsonl"
    mapping.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return source, mapping


def test_seeded_surface_sampling_is_repeatable() -> None:
    mesh = trimesh.creation.box()
    first, first_faces = sample_surface_points_numpy(mesh, 1000, seed=123)
    second, second_faces = sample_surface_points_numpy(mesh, 1000, seed=123)
    different, _ = sample_surface_points_numpy(mesh, 1000, seed=124)
    np.testing.assert_array_equal(first, second)
    np.testing.assert_array_equal(first_faces, second_faces)
    assert not np.array_equal(first, different)


def test_reference_frame_rejects_invalid_shape_or_resolution() -> None:
    meta = {"resolution_m_per_pixel": 0.5, "origin_xy_m": [0, 0], "extent_xy_m": [2, 3], "grid_shape": [7, 5]}
    frame = ProjectionFrame.from_metadata(meta)
    with pytest.raises(ValueError, match="resolution"):
        frame.validate_resolution(0.25)
    with pytest.raises(ValueError, match="grid_shape"):
        ProjectionFrame.from_metadata({**meta, "grid_shape": [5, 7]})


def test_regeneration_preserves_noncontiguous_keys_frames_and_removes_furniture(tmp_path: Path) -> None:
    source, mapping = make_source(tmp_path)
    output = tmp_path / "empty"
    summary = run_regeneration(mapping, source, output, workers=2, repo_root=tmp_path)
    assert summary["all_validated"] and summary["completed"] == 2
    records = [json.loads(line) for line in (output / "manifest.jsonl").read_text().splitlines()]
    assert [row["scene_key"] for row in records] == ["front3d_0042", "front3d_0007"]
    for row in records:
        key = row["scene_key"]
        assert row["placement_count"] == 0
        assert row["validation"]["frame_matches_original"]
        assert row["architecture_triangle_count"] == 4
        assert row["mesh_selection"]["excluded_mesh_type_counts"] == {
            "Cabinet": 1, "CustomizedFixedFurniture": 1, "Unknown": 1,
        }
        original = np.load(source / key / "floorplan/stack.npz")["stack"]
        empty = np.load(output / key / "floorplan/stack.npz")["stack"]
        assert empty.shape == original.shape == (1, 9, 13)
        assert original[:, :, 10:].sum() > 0
        assert empty[:, :, 10:].sum() == 0
        assert (output / row["image_path"]).is_file()
    # A second generation on unchanged inputs gives identical pixels, independent of scheduling.
    _, index_config, floor_config = load_source_config(source, tmp_path)
    repeat = tmp_path / "repeat"
    repeat.mkdir()
    regenerate_scene(records[0], source, repeat, index_config, floor_config, tmp_path)
    first = records[0]["scene_key"]
    assert (repeat / first / "floorplan/floorplan_1p60.png").read_bytes() == (
        output / first / "floorplan/floorplan_1p60.png"
    ).read_bytes()
    assert run_regeneration(mapping, source, output, resume=True, repo_root=tmp_path)["all_validated"]
    with pytest.raises(FileExistsError):
        run_regeneration(mapping, source, output, repo_root=tmp_path)
    # A corrupted completed scene must not be silently accepted on resume.
    (output / first / "floorplan/stack.npz").write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        run_regeneration(mapping, source, output, resume=True, repo_root=tmp_path)


def test_mapping_rejects_duplicate_ids_and_unsafe_keys(tmp_path: Path) -> None:
    mapping = tmp_path / "mapping.jsonl"
    for rows in ([{"scene_key": "../outside", "front3d_scene_id": "a"}],
                 [{"scene_key": "front3d_0001", "front3d_scene_id": "a"}] * 2):
        mapping.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        with pytest.raises(ValueError):
            load_mapping(mapping)


def test_empty_class_masks_keep_architecture_and_restore_furniture_to_free_space(tmp_path: Path) -> None:
    source, mapping = make_source(tmp_path)
    config_path = source / "effective_config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["floorplan"]["class_mask"]["enabled"] = True
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    _, index_config, floor_config = load_source_config(source, tmp_path)
    index = Front3DIndex(index_config, tmp_path)
    for row in load_mapping(mapping):
        scene_dir = source / row["scene_key"]
        frame = ProjectionFrame.from_metadata(json.loads((scene_dir / "floorplan/meta.json").read_text()))
        generate_front3d_class_mask(
            scene_dir / "floorplan", index.base_scene(row["front3d_scene_id"]), [], 0.25,
            0, 0, "bbox", 1.6, "doors", 0, 0.25, 1.6, True, True,
            path_root=scene_dir, reference_frame=frame,
        )
        mask_path = scene_dir / "floorplan/class_mask.npy"
        mask = np.load(mask_path)
        assert mask[4, 4] == 2
        mask[4, 4] = 3
        np.save(mask_path, mask)
    output = tmp_path / "empty"
    result = run_regeneration(mapping, source, output, workers=2, repo_root=tmp_path)
    assert result["all_validated"]
    row = json.loads((output / "manifest.jsonl").read_text().splitlines()[0])
    assert row["validation"]["furniture_pixels"] == 0
    assert row["validation"]["architecture_mask_matches_original"]
    mask = np.load(output / row["floorplan_dir"] / "class_mask.npy")
    assert mask[4, 4] == 2
    validate_scene(output / row["scene_key"], source / row["scene_key"], floor_config)


def test_uuid_mismatch_and_source_output_overlap_are_rejected(tmp_path: Path) -> None:
    source, mapping = make_source(tmp_path)
    with pytest.raises(ValueError, match="separate"):
        run_regeneration(mapping, source, source / "empty", repo_root=tmp_path)
    path = source / "front3d_0042" / "scene_record_batch.json"
    record = json.loads(path.read_text())
    record["front3d_scene_id"] = "wrong-scene"
    write_json(path, record)
    with pytest.raises(ValueError, match="UUID"):
        run_regeneration(mapping, source, tmp_path / "empty", repo_root=tmp_path)


def test_validation_rejects_changed_coordinate_frame(tmp_path: Path) -> None:
    source, mapping = make_source(tmp_path)
    output = tmp_path / "empty"
    run_regeneration(mapping, source, output, repo_root=tmp_path)
    _, _, config = load_source_config(source, tmp_path)
    path = output / "front3d_0042" / "floorplan/meta.json"
    meta = json.loads(path.read_text())
    meta["effective_ground_m_original_units"] += 0.1
    write_json(path, meta)
    with pytest.raises(ValueError, match="checksum|ground"):
        validate_scene(output / "front3d_0042", source / "front3d_0042", config)
