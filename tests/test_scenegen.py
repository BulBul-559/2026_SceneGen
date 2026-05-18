from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scenegen.assets import group_assets_by_class, load_assets, resolve_obj_file
from scenegen.cli import main
from scenegen.geometry import load_bistro_base_scene
from scenegen.paths import default_asset_manifest, default_bistro_base_dir, default_config_path, find_project_root


def iter_json_strings(value: object) -> list[str]:
    if isinstance(value, dict):
        strings: list[str] = []
        for child in value.values():
            strings.extend(iter_json_strings(child))
        return strings
    if isinstance(value, list):
        strings = []
        for child in value:
            strings.extend(iter_json_strings(child))
        return strings
    if isinstance(value, str):
        return [value]
    return []


def test_default_paths_point_to_packaged_data() -> None:
    root = find_project_root()

    assert default_bistro_base_dir(root) == root / "data" / "scene"
    assert default_asset_manifest(root) == root / "data" / "assets" / "manifest.json"
    assert default_config_path(root) == root / "config" / "default.yaml"
    assert (default_bistro_base_dir(root) / "scene.obj").is_file()
    assert default_asset_manifest(root).is_file()
    assert default_config_path(root).is_file()


def test_manifest_windows_obj_paths_resolve_to_local_assets() -> None:
    manifest = default_asset_manifest()
    payload = json.loads(manifest.read_text(encoding="utf-8"))

    obj_file = resolve_obj_file(payload[0], manifest)

    assert obj_file.is_file()
    assert "data/assets" in obj_file.as_posix()


def test_asset_pool_has_required_classes() -> None:
    assets = load_assets(default_asset_manifest())
    grouped = group_assets_by_class(assets)

    assert len(assets) >= 40
    assert all(grouped[name] for name in ("table", "seat", "tabletop", "floor"))


def test_bistro_base_scene_detection() -> None:
    base = load_bistro_base_scene(default_bistro_base_dir())

    assert base.scene_obj.name == "scene.obj"
    assert base.floor_z > 0
    assert base.floor_triangles
    assert base.support_surfaces
    assert base.static_obstacles


def test_generated_scene_outputs_and_sionna_load(tmp_path: Path) -> None:
    pytest.importorskip("sionna.rt")
    pytest.importorskip("trimesh")

    output_dir = tmp_path / "out"
    exit_code = main(
        [
            "--mode",
            "generated",
            "--scenes",
            "1",
            "--run-name",
            "smoke_generated",
            "--output-dir",
            str(output_dir),
            "--seed",
            "123",
            "--min-tables",
            "1",
            "--max-tables",
            "1",
            "--floor-extras",
            "1",
            "--max-attempts",
            "20",
            "--floorplan-sample-density-scale",
            "0.01",
            "--floorplan-min-sample-points",
            "1000",
            "--floorplan-max-sample-points",
            "2000",
            "--validate-sionna",
        ]
    )

    scene_dir = output_dir / "smoke_generated" / "scene_0000"
    assert exit_code == 0
    for filename in ("scene.obj", "scene.xml", "label.json", "placements.json"):
        assert (scene_dir / filename).is_file()
    for filename in (
        "preview.png",
        "side_view.png",
        "meta.json",
        "stack.npz",
        "geometry_raw.png",
    ):
        assert (scene_dir / "floorplan" / filename).is_file()
    for filename in ("semantic.png", "semantic.json"):
        assert (scene_dir / "floorplan" / filename).is_file()
    manifest = json.loads((output_dir / "smoke_generated" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sionna_validation_ok"] is True
    assert manifest["floorplan_ok"] is True
    assert manifest["floorplan_geometry_clean_requested"] is False
    assert manifest["floorplan_height_mode"] == "heights"
    assert manifest["floorplan_heights_m"] == [1.6]
    assert manifest["floorplan_semantic_requested"] is True
    assert manifest["summary_floorplan_raw"]["count"] == 1
    assert (output_dir / "smoke_generated" / "summary_floorplan_raw" / "scene_0000_geometry_raw.png").is_file()
    geometry_meta = json.loads((scene_dir / "floorplan" / "meta.json").read_text(encoding="utf-8"))
    assert geometry_meta["height_mode"] == "heights"
    assert geometry_meta["z_levels_m"] == [1.6]
    assert geometry_meta["num_levels"] == 1
    assert geometry_meta["geometry_clean"] is None
    semantic = json.loads((scene_dir / "floorplan" / "semantic.json").read_text(encoding="utf-8"))
    assert semantic["type"] == "semantic_floorplan"
    assert semantic["object_count"] > 0
    effective_config = yaml.safe_load((output_dir / "smoke_generated" / "effective_config.yaml").read_text())
    assert effective_config["pipeline"]["mode"] == "generated"
    assert effective_config["pipeline"]["seed"] == 123
    assert effective_config["floorplan"]["min_sample_points"] == 1000
    assert effective_config["floorplan"]["geometry_clean_enabled"] is False
    assert effective_config["floorplan"]["height_mode"] == "heights"
    assert effective_config["floorplan"]["heights_m"] == [1.6]
    assert effective_config["floorplan"]["semantic_enabled"] is True

    json_files = [
        output_dir / "smoke_generated" / "manifest.json",
        output_dir / "smoke_generated" / "manifest_generated.json",
        output_dir / "smoke_generated" / "summary_obj" / "copy_manifest.json",
        output_dir / "smoke_generated" / "summary_floorplan_raw" / "copy_manifest.json",
        scene_dir / "placements.json",
        scene_dir / "label.json",
        scene_dir / "floorplan" / "meta.json",
        scene_dir / "floorplan" / "semantic.json",
    ]
    for json_file in json_files:
        payload = json.loads(json_file.read_text(encoding="utf-8"))
        assert not [value for value in iter_json_strings(payload) if value.startswith("/")]
