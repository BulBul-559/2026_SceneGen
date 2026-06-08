from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> Any:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


derived = load_script("generate_derived_maps")
dataset = load_script("build_vision_dataset")
merge = load_script("merge_vision_datasets")


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_scene_fixture(run_dir: Path) -> Path:
    scene_dir = run_dir / "front3d_0000"
    floorplan_dir = scene_dir / "floorplan"
    label_dir = scene_dir / "label"
    floorplan_dir.mkdir(parents=True)
    label_dir.mkdir()
    class_mask = np.full((8, 10), 2, dtype=np.uint8)
    class_mask[0, :] = 0
    class_mask[-1, :] = 0
    class_mask[:, 0] = 0
    class_mask[:, -1] = 0
    class_mask[1:7, 5] = 1
    class_mask[2, 2] = 3
    np.save(floorplan_dir / "class_mask.npy", class_mask)
    Image.fromarray(class_mask, mode="L").save(floorplan_dir / "class_mask.png")
    Image.new("RGB", (10, 8), (100, 100, 100)).save(floorplan_dir / "class_mask_preview.png")
    Image.new("L", (10, 8), 180).save(floorplan_dir / "floorplan_1p60.png")
    meta = {
        "scene_id": "scene-fixture",
        "npy": "floorplan/class_mask.npy",
        "resolution_m_per_pixel": 0.05,
        "origin_xy_m": [0.0, 0.0],
        "extent_xy_m": [0.5, 0.4],
        "grid_shape": [8, 10],
        "classes": {
            "0": {"name": "outdoor"},
            "1": {"name": "wall"},
            "2": {"name": "free_space"},
            "3": {"name": "furniture"},
        },
    }
    write_json(floorplan_dir / "class_mask_meta.json", meta)
    write_json(floorplan_dir / "meta.json", {"projection_mode": "sampling", "resolution_m_per_pixel": 0.05})
    # Pixel (2, 2) in SceneGen floorplan coordinates maps to world x=0.10, y=0.30.
    bs_position = [0.1, 0.3, 2.4]
    write_json(
        label_dir / "label_panel_0p1.json",
        {
            "bs_points": [
                {
                    "label": "BS_A",
                    "position": bs_position,
                    "room_id": "room-1",
                    "strategy": "fixture",
                }
            ],
            "bs_positions": [bs_position],
        },
    )
    return scene_dir


def test_sdf_uses_scenegen_class_ids_and_ignores_outdoor() -> None:
    if derived.ndimage is None:
        pytest.skip("scipy is not available")
    class_mask = np.array(
        [
            [0, 0, 0, 0, 0],
            [0, 2, 2, 1, 0],
            [0, 3, 2, 1, 0],
            [0, 0, 0, 0, 0],
        ],
        dtype=np.uint8,
    )
    sdf, valid = derived.sdf_from_class_mask(class_mask, r_max_m=1.0, meter_per_pixel=0.1)
    assert valid[0, 0] == 0
    assert valid[1, 1] == 1
    assert sdf[0, 0] == 0
    assert sdf[1, 1] > 0
    assert sdf[2, 1] > 0
    assert sdf[1, 3] < 0


def test_los_and_wall_count_maps_detect_single_and_double_walls() -> None:
    wall_mask = np.zeros((9, 11), dtype=bool)
    wall_mask[1:8, 4] = True
    wall_mask[1:8, 7] = True
    free_like = np.ones((9, 11), dtype=bool)
    los, wall_counts, ue_valid = derived.propagation_maps(wall_mask, free_like, [(2, 4)], stride=1)
    assert ue_valid.shape == (9, 11)
    assert los[0, 4, 2] == 1
    assert wall_counts[0, 4, 2] == 0
    assert los[0, 4, 5] == 0
    assert wall_counts[0, 4, 5] == 1
    assert los[0, 4, 9] == 0
    assert wall_counts[0, 4, 9] == 2
    assert np.all(los[wall_counts > 0] == 0)


def test_bs_world_to_pixel_conversion_and_snap() -> None:
    class_mask = np.full((8, 10), 2, dtype=np.uint8)
    class_mask[2, 2] = 3
    valid = (class_mask == 2) | (class_mask == 3)
    origin = [0.0, 0.0]
    extent = [0.5, 0.4]
    assert derived.world_to_pixel(0.1, 0.3, origin, extent, 0.05) == (2, 2)
    points = [derived.BsPoint("BS_A", (0.1, 0.3, 2.4), ("label/label_panel_0p1.json",))]
    snapped, skipped = derived.snap_bs_points(points, valid, origin, extent, 0.05, snap_radius_m=0.25)
    assert not skipped
    assert snapped[0].pixel_xy == (2, 2)
    assert snapped[0].snapped is False


def test_generate_maps_and_build_dataset_copy_only_training_files(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    scene_dir = make_scene_fixture(run_dir)
    record = derived.generate_maps_for_scene(
        scene_dir,
        run_dir,
        r_max_m=3.0,
        los_stride_pixels=1,
        snap_radius_m=0.25,
        overwrite=False,
    )
    assert record["status"] == "generated"
    geometry = np.load(scene_dir / "maps" / "geometry.npz")
    propagation = np.load(scene_dir / "maps" / "propagation.npz")
    assert geometry["sdf"].dtype == np.float16
    assert propagation["los_maps"].shape == (1, 8, 10)
    assert propagation["wall_count_maps"][0, 2, 7] > 0

    dataset_dir = tmp_path / "dataset"
    dataset_record = dataset.build_scene_dataset_entry(scene_dir, dataset_dir, run_dir, overwrite=False)
    assert dataset_record["status"] == "copied"
    target = dataset_dir / "front3d_0000"
    expected = {
        "floorplan.png",
        "mask.npy",
        "mask.png",
        "mask_preview.png",
        "geometry.npz",
        "propagation.npz",
        "label_bs.json",
        "metadata.json",
    }
    assert {path.name for path in target.iterdir()} == expected
    label_bs = json.loads((target / "label_bs.json").read_text(encoding="utf-8"))
    assert label_bs["bs_count"] == 1
    assert label_bs["bs_points"][0]["label"] == "BS_A"


def write_compact_dataset_scene(dataset_dir: Path, scene_key: str, scene_id: str) -> None:
    scene_dir = dataset_dir / scene_key
    scene_dir.mkdir(parents=True)
    for name in [
        "floorplan.png",
        "mask.npy",
        "mask.png",
        "mask_preview.png",
        "geometry.npz",
        "propagation.npz",
    ]:
        (scene_dir / name).write_bytes(f"{scene_key}:{name}".encode("utf-8"))
    write_json(
        scene_dir / "label_bs.json",
        {
            "schema_version": "scenegen.vision_dataset.label_bs.v1",
            "scene_key": scene_key,
            "scene_id": scene_id,
            "bs_count": 1,
            "bs_positions": [[0.0, 0.0, 2.4]],
            "bs_points": [{"label": "BS_CENTER", "position_m": [0.0, 0.0, 2.4]}],
        },
    )
    write_json(
        scene_dir / "metadata.json",
        {
            "schema_version": "scenegen.vision_dataset.scene.v1",
            "scene_key": scene_key,
            "scene_id": scene_id,
            "grid_shape": [4, 5],
            "resolution_m_per_pixel": 0.05,
        },
    )


def append_manifest_record(dataset_dir: Path, scene_key: str, scene_id: str) -> None:
    record = {
        "status": "copied",
        "scene_key": scene_key,
        "scene_id": scene_id,
        "target_scene_dir": scene_key,
        "height": 4,
        "width": 5,
        "meter_per_pixel": 0.05,
        "bs_count": 1,
        "files": {
            "floorplan": "floorplan.png",
            "mask_npy": "mask.npy",
            "mask_png": "mask.png",
            "mask_preview": "mask_preview.png",
            "geometry": "geometry.npz",
            "propagation": "propagation.npz",
            "label_bs": "label_bs.json",
        },
    }
    with (dataset_dir / "manifest.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def make_compact_dataset(dataset_dir: Path, scenes: list[tuple[str, str]]) -> None:
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "manifest.jsonl").write_text("", encoding="utf-8")
    for scene_key, scene_id in scenes:
        write_compact_dataset_scene(dataset_dir, scene_key, scene_id)
        append_manifest_record(dataset_dir, scene_key, scene_id)


def test_merge_vision_datasets_prefers_primary_and_renumbers(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    supplement = tmp_path / "supplement"
    output = tmp_path / "merged"
    make_compact_dataset(primary, [("front3d_0000", "scene-a"), ("front3d_0001", "scene-b")])
    make_compact_dataset(supplement, [("front3d_3000", "scene-c"), ("front3d_3001", "scene-d")])

    summary = merge.merge_datasets(
        primary,
        supplement,
        output,
        target_count=3,
        overwrite=False,
        skip_duplicate_scene_ids=True,
        log_every=0,
    )

    assert summary["scene_count"] == 3
    assert summary["role_counts"] == {"primary": 2, "supplement": 1}
    assert sorted(path.name for path in output.glob("front3d_*")) == ["front3d_0000", "front3d_0001", "front3d_0002"]
    metadata = json.loads((output / "front3d_0002" / "metadata.json").read_text(encoding="utf-8"))
    label = json.loads((output / "front3d_0002" / "label_bs.json").read_text(encoding="utf-8"))
    assert metadata["scene_key"] == "front3d_0002"
    assert metadata["merged_dataset"]["source_scene_key"] == "front3d_3000"
    assert metadata["merged_dataset"]["source_role"] == "supplement"
    assert label["scene_key"] == "front3d_0002"
    manifest_records = [json.loads(line) for line in (output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [record["scene_key"] for record in manifest_records] == ["front3d_0000", "front3d_0001", "front3d_0002"]
