from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scenegen.assets import AssetSpec, group_assets_by_class, legacy_item_to_spec, load_assets, resolve_obj_file
from scenegen.cli import main, parse_args
from scenegen.config import load_effective_config
from scenegen.geometry import load_bistro_base_scene
from scenegen.paths import (
    default_asset_catalog,
    default_asset_manifest,
    default_bistro_base_dir,
    default_config_path,
    find_project_root,
)


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
    assert default_asset_catalog(root) == root / "data" / "catalogs" / "bistro.v1.json"
    assert default_asset_manifest(root) == root / "data" / "assets" / "manifest.json"
    assert default_config_path(root) == root / "config" / "default.yaml"
    assert (default_bistro_base_dir(root) / "scene.obj").is_file()
    assert default_asset_catalog(root).is_file()
    assert default_asset_manifest(root).is_file()
    assert default_config_path(root).is_file()


def legacy_table_fixture() -> dict[str, object]:
    return {
        "asset_name": "Paris_Table_02A",
        "export_name": "Paris_Table_02A",
        "obj_file": r"C:\old_project\Paris_Table_02A.obj",
        "dimensions": {"width_x": 0.88, "length_y": 0.88, "height_z": 0.74},
        "bounds": {"min": [-0.44, -0.44, 0.0], "max": [0.44, 0.44, 0.74]},
        "units": "meter",
        "coordinate_system": {"right_axis": "+X", "forward_axis": "+Y", "up_axis": "+Z"},
        "normalization": {"grounding_policy": "lowest point moved to z=0", "applied_z_rotation_radians": 0.0},
        "sionna_material_names": ["itu-wood"],
        "sionna_material_mappings": [
            {
                "source_material": "wood",
                "sionna_material_name": "itu-wood",
                "itu_type": "wood",
                "confidence": "high",
            }
        ],
    }


def test_clean_catalog_schema_and_asset_json_sync() -> None:
    root = find_project_root()
    catalog = json.loads(default_asset_catalog(root).read_text(encoding="utf-8"))
    manifest = json.loads(default_asset_manifest(root).read_text(encoding="utf-8"))

    assert catalog == manifest
    assert len(catalog) == 72
    assert len({item["id"] for item in catalog}) == 72
    for item in catalog:
        AssetSpec.from_mapping(item)
        asset_json = root / "data" / "assets" / item["id"] / f"{item['id']}.json"
        assert json.loads(asset_json.read_text(encoding="utf-8")) == item


def test_catalog_paths_are_repo_relative() -> None:
    root = find_project_root()
    catalog = json.loads(default_asset_catalog(root).read_text(encoding="utf-8"))

    for item in catalog:
        for path_value in item["files"].values():
            if path_value:
                assert not Path(path_value).is_absolute()
                assert "\\" not in path_value


def test_legacy_windows_obj_paths_resolve_to_local_assets() -> None:
    manifest = default_asset_manifest()
    item = legacy_table_fixture()

    obj_file = resolve_obj_file(item, manifest)

    assert obj_file.is_file()
    assert "data/assets" in obj_file.as_posix()


def test_legacy_manifest_item_converts_to_clean_contract() -> None:
    spec = legacy_item_to_spec(legacy_table_fixture(), default_asset_manifest())

    assert spec["schema_version"] == "1.0"
    assert spec["files"]["obj"] == "data/assets/Paris_Table_02A/Paris_Table_02A.obj"
    assert spec["placement"]["class"] == "table"
    assert "obj_file" not in spec
    assert "source_scene" not in spec


def test_asset_pool_has_required_classes() -> None:
    assets = load_assets(default_asset_catalog())
    grouped = group_assets_by_class(assets)
    class_counts = {name: len(grouped[name]) for name in ("table", "seat", "tabletop", "floor")}

    assert len(assets) == 45
    assert class_counts == {"table": 2, "seat": 2, "tabletop": 32, "floor": 9}


def test_bistro_base_scene_detection() -> None:
    base = load_bistro_base_scene(default_bistro_base_dir())

    assert base.scene_obj.name == "scene.obj"
    assert base.floor_z > 0
    assert base.floor_triangles
    assert base.support_surfaces
    assert base.static_obstacles


def test_partial_config_inherits_default_bistro_forbidden_zones() -> None:
    root = find_project_root()
    effective, _overrides = load_effective_config(root / "config" / "sparse.yaml", root, parse_args([]))

    assert effective["pipeline"]["run_name"] == "sparse"
    assert effective["assets"]["catalog"].endswith("data/catalogs/bistro.v1.json")
    assert effective["bistro"]["forbidden_xy_rects"] == [[1.0, 11.0, 4.5, 16.0], [8.0, 8.0, 14.0, 10.0]]
    assert effective["floorplan"]["semantic_enabled"] is False


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
        assert not (scene_dir / "floorplan" / filename).exists()
    for filename in ("quality_report.json", "statistics.json"):
        assert (scene_dir / filename).is_file()
    manifest = json.loads((output_dir / "smoke_generated" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sionna_validation_ok"] is True
    assert manifest["asset_catalog"].endswith("data/catalogs/bistro.v1.json")
    assert manifest["quality_requested"] is True
    assert manifest["quality_ok"] is True
    assert manifest["statistics"]["scene_count"] == 1
    assert (output_dir / "smoke_generated" / "statistics.json").is_file()
    assert manifest["floorplan_ok"] is True
    assert manifest["floorplan_geometry_clean_requested"] is False
    assert manifest["floorplan_height_mode"] == "heights"
    assert manifest["floorplan_heights_m"] == [1.6]
    assert manifest["floorplan_semantic_requested"] is False
    assert manifest["summary_floorplan_raw"]["count"] == 1
    assert (output_dir / "smoke_generated" / "summary_floorplan_raw" / "scene_0000_geometry_raw.png").is_file()
    geometry_meta = json.loads((scene_dir / "floorplan" / "meta.json").read_text(encoding="utf-8"))
    assert geometry_meta["height_mode"] == "heights"
    assert geometry_meta["z_levels_m"] == [1.6]
    assert geometry_meta["num_levels"] == 1
    assert geometry_meta["geometry_clean"] is None
    quality = json.loads((scene_dir / "quality_report.json").read_text(encoding="utf-8"))
    assert quality["ok"] is True
    assert quality["error_count"] == 0
    statistics = json.loads((scene_dir / "statistics.json").read_text(encoding="utf-8"))
    assert statistics["placement_count"] > 0
    assert statistics["object_count_by_class"]
    effective_config = yaml.safe_load((output_dir / "smoke_generated" / "effective_config.yaml").read_text())
    assert effective_config["pipeline"]["mode"] == "generated"
    assert effective_config["pipeline"]["seed"] == 123
    assert effective_config["floorplan"]["min_sample_points"] == 1000
    assert effective_config["floorplan"]["geometry_clean_enabled"] is False
    assert effective_config["floorplan"]["height_mode"] == "heights"
    assert effective_config["floorplan"]["heights_m"] == [1.6]
    assert effective_config["floorplan"]["semantic_enabled"] is False
    assert effective_config["quality"]["enabled"] is True

    json_files = [
        output_dir / "smoke_generated" / "manifest.json",
        output_dir / "smoke_generated" / "manifest_generated.json",
        output_dir / "smoke_generated" / "statistics.json",
        output_dir / "smoke_generated" / "summary_obj" / "copy_manifest.json",
        output_dir / "smoke_generated" / "summary_floorplan_raw" / "copy_manifest.json",
        scene_dir / "placements.json",
        scene_dir / "label.json",
        scene_dir / "floorplan" / "meta.json",
        scene_dir / "quality_report.json",
        scene_dir / "statistics.json",
    ]
    for json_file in json_files:
        payload = json.loads(json_file.read_text(encoding="utf-8"))
        assert not [value for value in iter_json_strings(payload) if value.startswith("/")]
