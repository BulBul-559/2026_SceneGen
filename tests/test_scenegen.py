from __future__ import annotations

import json
import random
from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image

from scenegen import __version__
from scenegen.assets import AssetSpec, group_assets_by_class, legacy_item_to_spec, load_assets, resolve_obj_file
from scenegen.cli import evaluate_front3d_precheck, main, parse_args
from scenegen.config import DEFAULT_CONFIG, load_effective_config
from scenegen.floorplan import floorplan_layer_filename
from scenegen.front3d import choose_scene_ids, scenegen_transform_for_child
from scenegen.geometry import load_bistro_base_scene
from scenegen.labels import (
    LabelConfig,
    LabelObstacle,
    RoomLabelContext,
    assign_global_free_points,
    bs_count_for_room,
    choose_geometry_center_bs,
    corridor_context_from_global,
    generate_bs_points_for_room,
    generate_ue_points_for_room,
    label_variants,
)
from scenegen.models import SupportTriangle
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
    assert default_config_path(root) == root / "config" / "template.yaml"
    assert (default_bistro_base_dir(root) / "scene.obj").is_file()
    assert default_asset_catalog(root).is_file()
    assert default_asset_manifest(root).is_file()
    assert default_config_path(root).is_file()


def test_floorplan_layer_filename_uses_height_token() -> None:
    assert floorplan_layer_filename(1.6) == "floorplan_1p60.png"
    assert floorplan_layer_filename(2.0) == "floorplan_2p00.png"


def test_cli_version_matches_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert __version__ == "2.0.0"
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == "SceneGen 2.0.0"


def test_template_config_matches_code_defaults() -> None:
    root = find_project_root()

    assert yaml.safe_load((root / "config" / "template.yaml").read_text(encoding="utf-8")) == DEFAULT_CONFIG


@pytest.mark.parametrize("config_name", ["template.yaml"])
def test_project_configs_load_through_config_pipeline(config_name: str) -> None:
    root = find_project_root()

    effective, _overrides = load_effective_config(root / "config" / config_name, root, parse_args([]))

    assert effective["runtime"]["config_path"].endswith(f"config/{config_name}")
    assert "manifest" not in effective["assets"]


def test_unknown_config_field_is_rejected(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "bad_config.yaml"
    config_path.write_text("floorplan:\n  resolution: 0.1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="floorplan.resolution"):
        load_effective_config(config_path, root, parse_args([]))


def test_invalid_config_value_is_rejected(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "bad_config.yaml"
    config_path.write_text("floorplan:\n  sample_density_scale: 0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="sample_density_scale"):
        load_effective_config(config_path, root, parse_args([]))


def test_front3d_scene_selection_and_transform() -> None:
    rng = random.Random(7)
    assert choose_scene_ids(["a", "b"], ("b",), "random", 3, rng) == ["b", "b", "b"]
    assert choose_scene_ids(["a", "b"], (), "sequential", 3, rng) == ["a", "b", "a"]

    matrix = scenegen_transform_for_child({"pos": [1, 2, 3], "rot": [0, 0, 0, 1], "scale": [1, 1, 1]})
    assert [round(matrix[index], 6) for index in (3, 7, 11)] == [1, -3, 2]


def test_front3d_precheck_rejects_anomalous_statistics() -> None:
    args = parse_args(
        [
            "--mode",
            "front3d",
            "--front3d-precheck",
            "--front3d-precheck-min-placements",
            "1",
            "--front3d-precheck-max-z",
            "8",
            "--front3d-precheck-max-footprint-ratio",
            "5",
        ]
    )
    ok = evaluate_front3d_precheck(
        args,
        {
            "placement_count": 2,
            "z_range_m": [0.0, 2.0],
            "approx_footprint_ratio": 1.2,
        },
    )
    bad = evaluate_front3d_precheck(
        args,
        {
            "placement_count": 0,
            "z_range_m": [0.0, 82.9],
            "approx_footprint_ratio": 202.0,
        },
    )

    assert ok["ok"] is True
    assert bad["ok"] is False
    assert {error["code"] for error in bad["errors"]} == {
        "too_few_placements",
        "z_range_too_high",
        "footprint_ratio_too_high",
    }


def test_label_plane_grid_respects_floor_domain_and_ignores_obstacles() -> None:
    config = LabelConfig.from_mapping(
        {
            **DEFAULT_CONFIG["label"],
            "ue_strategy": "plane_grid",
            "grid_resolution_m": 1.0,
            "wall_clearance_m": 0.5,
            "ue_clearance_m": 0.0,
        }
    )
    tri = SupportTriangle(vertices=((0.0, 0.0, 0.0), (4.0, 0.0, 0.0), (0.0, 4.0, 0.0)), area=8.0, z=0.0)
    context = RoomLabelContext(
        room_index=0,
        room_id="room",
        room_type="test",
        floor_source="test",
        floor_triangles=(tri,),
        floor_z=0.0,
        ceiling_z=3.0,
        bounds_xy=(0.0, 0.0, 4.0, 4.0),
        obstacles=(LabelObstacle(1.9, 1.9, 2.1, 2.1, 0.0, 2.0, "blocking_box"),),
    )

    points = generate_ue_points_for_room(context, config)

    assert (3.0, 3.0) not in points
    assert (1.0, 1.0) in points
    assert (2.0, 2.0) in points
    assert points.stats["panel_obstacle_mode"] == "none"
    assert points.stats["panel_furniture_filter_enabled"] is False


def test_label_height_aware_obstacles_keep_points_above_low_objects() -> None:
    config = LabelConfig.from_mapping(
        {
            **DEFAULT_CONFIG["label"],
            "ue_height_m": 1.8,
            "ue_strategy": "free_space_grid",
            "grid_resolution_m": 1.0,
            "wall_clearance_m": 0.0,
            "ue_clearance_m": 0.0,
            "obstacle_strategy": "height_aware",
        }
    )
    tri = SupportTriangle(vertices=((0.0, 0.0, 0.0), (3.0, 0.0, 0.0), (0.0, 3.0, 0.0)), area=4.5, z=0.0)
    context = RoomLabelContext(
        room_index=0,
        room_id="room",
        room_type="test",
        floor_source="test",
        floor_triangles=(tri,),
        floor_z=0.0,
        ceiling_z=3.0,
        bounds_xy=(0.0, 0.0, 3.0, 3.0),
        obstacles=(
            LabelObstacle(0.9, 0.9, 1.1, 1.1, 0.0, 1.5, "table_below_ue"),
            LabelObstacle(1.9, 1.9, 2.1, 2.1, 1.7, 2.0, "shelf_at_ue"),
        ),
    )

    points = generate_ue_points_for_room(context, config)

    assert (1.0, 1.0) in points
    assert (2.0, 2.0) not in points


def test_label_footprint_column_obstacles_block_points_above_low_objects() -> None:
    config = LabelConfig.from_mapping(
        {
            **DEFAULT_CONFIG["label"],
            "ue_height_m": 1.8,
            "ue_strategy": "free_space_grid",
            "grid_resolution_m": 1.0,
            "wall_clearance_m": 0.0,
            "ue_clearance_m": 0.0,
            "obstacle_strategy": "footprint_column",
        }
    )
    tri = SupportTriangle(vertices=((0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 2.0, 0.0)), area=2.0, z=0.0)
    context = RoomLabelContext(
        room_index=0,
        room_id="room",
        room_type="test",
        floor_source="test",
        floor_triangles=(tri,),
        floor_z=0.0,
        ceiling_z=3.0,
        bounds_xy=(0.0, 0.0, 2.0, 2.0),
        obstacles=(LabelObstacle(0.9, 0.9, 1.1, 1.1, 0.0, 1.5, "table_below_ue"),),
    )

    points = generate_ue_points_for_room(context, config)

    assert (1.0, 1.0) not in points


def make_square_room_context(size_m: float) -> RoomLabelContext:
    tri_a = SupportTriangle(vertices=((0.0, 0.0, 0.0), (size_m, 0.0, 0.0), (0.0, size_m, 0.0)), area=size_m * size_m / 2.0, z=0.0)
    tri_b = SupportTriangle(vertices=((size_m, 0.0, 0.0), (size_m, size_m, 0.0), (0.0, size_m, 0.0)), area=size_m * size_m / 2.0, z=0.0)
    return RoomLabelContext(
        room_index=0,
        room_id="room",
        room_type="test",
        floor_source="test",
        floor_triangles=(tri_a, tri_b),
        floor_z=0.0,
        ceiling_z=3.0,
        bounds_xy=(0.0, 0.0, size_m, size_m),
        obstacles=(),
    )


def test_label_area_adaptive_bs_count_scales_with_room_area() -> None:
    config = LabelConfig.from_mapping(
        {
            **DEFAULT_CONFIG["label"],
            "bs_count_strategy": "area_adaptive",
            "bs_min_room_area_m2": 4.0,
            "bs_area_per_point_m2": 10.0,
            "bs_min_per_room": 1,
            "bs_max_per_room": 8,
        }
    )

    assert bs_count_for_room(make_square_room_context(1.5), config) == 0
    assert bs_count_for_room(make_square_room_context(3.0), config) == 1
    assert bs_count_for_room(make_square_room_context(8.0), config) == 7
    assert bs_count_for_room(make_square_room_context(12.0), config) == 8


def test_label_area_adaptive_bs_generation_uses_computed_count() -> None:
    config = LabelConfig.from_mapping(
        {
            **DEFAULT_CONFIG["label"],
            "bs_count_strategy": "area_adaptive",
            "bs_min_room_area_m2": 4.0,
            "bs_area_per_point_m2": 10.0,
            "bs_min_per_room": 1,
            "bs_max_per_room": 8,
            "grid_resolution_m": 1.0,
        }
    )
    context = make_square_room_context(8.0)
    free_points = [(float(x), float(y)) for x in range(1, 8) for y in range(1, 8)]

    bs_points = generate_bs_points_for_room(context, free_points, config)

    assert len(bs_points) == 7


def test_global_floor_assignment_keeps_corridor_points() -> None:
    config = LabelConfig.from_mapping(DEFAULT_CONFIG["label"])
    room_context = make_square_room_context(1.0)
    global_context = make_square_room_context(3.0)
    corridor_context = corridor_context_from_global(global_context, config)

    grouped = assign_global_free_points([(0.5, 0.5), (2.0, 2.0)], [room_context], corridor_context)
    by_room = {context.room_id: points for context, points in grouped}

    assert by_room["room"] == [(0.5, 0.5)]
    assert by_room["__corridor__"] == [(2.0, 2.0)]


def test_label_variants_combine_strategy_resolution_and_connected_area() -> None:
    config = LabelConfig.from_mapping(
        {
            **DEFAULT_CONFIG["label"],
            "batch_strategies": ["plane_grid", "free_space_grid"],
            "batch_grid_resolutions_m": [0.1],
            "batch_connected_area_enabled": [True, False],
        }
    )

    variants = label_variants(config)

    assert [variant.name for variant in variants] == [
        "label_panel_connected_0p1",
        "label_panel_room_0p1",
        "label_walk_connected_0p1",
        "label_walk_room_0p1",
    ]
    assert [variant.config.connected_area_enabled for variant in variants] == [True, False, True, False]


def test_geometry_center_bs_selects_near_center_free_point() -> None:
    config = LabelConfig.from_mapping(
        {
            **DEFAULT_CONFIG["label"],
            "bs_strategy": "geometry_center",
            "bs_wall_clearance_m": 0.2,
            "bs_center_initial_radius_m": 0.2,
            "bs_center_radius_step_m": 0.1,
            "bs_center_max_radius_m": 1.0,
        }
    )
    global_context = make_square_room_context(4.0)
    free_points = [(0.5, 0.5), (2.0, 2.0), (3.5, 3.5)]

    selected_context, bs_xyz, stats = choose_geometry_center_bs(global_context, [(global_context, free_points)], config)

    assert selected_context == global_context
    assert bs_xyz == (2.0, 2.0, 2.4)
    assert stats["ok"] is True
    assert stats["distance_to_center_m"] == 0.0


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


def test_partial_config_inherits_builtin_defaults(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "partial_config.yaml"
    config_path.write_text("pipeline:\n  run_name: sparse\nplacement:\n  min_tables: 2\n  max_tables: 4\n", encoding="utf-8")
    effective, _overrides = load_effective_config(config_path, root, parse_args([]))

    assert effective["pipeline"]["run_name"] == "sparse"
    assert effective["assets"]["catalog"].endswith("data/catalogs/bistro.v1.json")
    assert "manifest" not in effective["assets"]
    assert effective["bistro"]["forbidden_xy_rects"] == [[1.0, 11.0, 4.5, 16.0], [8.0, 8.0, 14.0, 10.0]]
    assert effective["floorplan"]["semantic_enabled"] is False


def test_legacy_assets_manifest_config_normalizes_to_catalog(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "legacy_config.yaml"
    config_path.write_text(
        "assets:\n  manifest: data/assets/manifest.json\npipeline:\n  scenes: 1\n",
        encoding="utf-8",
    )

    effective, _overrides = load_effective_config(config_path, root, parse_args([]))

    assert effective["assets"]["catalog"].endswith("data/assets/manifest.json")
    assert "manifest" not in effective["assets"]


def write_front3d_fixture_obj(path: Path, *, material: str, height_axis_y: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if height_axis_y:
        vertices = [
            (-0.2, 0.0, -0.2),
            (0.2, 0.0, -0.2),
            (0.2, 0.8, -0.2),
            (-0.2, 0.8, -0.2),
            (-0.2, 0.0, 0.2),
            (0.2, 0.0, 0.2),
            (0.2, 0.8, 0.2),
            (-0.2, 0.8, 0.2),
        ]
    else:
        vertices = [
            (0.0, 0.0, 0.0),
            (3.0, 0.0, 0.0),
            (3.0, 3.0, 0.0),
            (0.0, 3.0, 0.0),
            (0.0, 0.0, 2.5),
            (3.0, 0.0, 2.5),
            (3.0, 3.0, 2.5),
            (0.0, 3.0, 2.5),
        ]
    faces = [(1, 2, 3), (1, 3, 4), (5, 7, 6), (5, 8, 7), (1, 5, 6), (1, 6, 2)]
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"usemtl {material}\n")
        for x, y, z in vertices:
            handle.write(f"v {x} {y} {z}\n")
        for face in faces:
            handle.write("f " + " ".join(str(index) for index in face) + "\n")


def make_front3d_runtime_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "front3d"
    source_scene_dir = root / "3D-FRONT"
    scene_id = "scene-front3d-test"
    original_model_id = "model-original"
    replacement_model_id = "model-replacement"
    source_scene_dir.mkdir(parents=True)
    (source_scene_dir / f"{scene_id}.json").write_text(
        json.dumps(
            {
                "uid": scene_id,
                "furniture": [{"uid": "100/model", "jid": original_model_id}],
                "scene": {
                    "room": [
                        {
                            "type": "Bedroom",
                            "instanceid": "room/0",
                            "children": [
                                {
                                    "ref": "100/model",
                                    "instanceid": "furniture/0",
                                    "pos": [1.0, 0.0, -1.0],
                                    "rot": [0.0, 0.0, 0.0, 1.0],
                                    "scale": [1.0, 1.0, 1.0],
                                    "replace_jid": replacement_model_id,
                                },
                                {
                                    "ref": "floor/0",
                                    "instanceid": "mesh/0",
                                    "pos": [0.0, 0.0, 0.0],
                                    "rot": [0.0, 0.0, 0.0, 1.0],
                                    "scale": [1.0, 1.0, 1.0],
                                },
                            ],
                        }
                    ]
                },
                "mesh": [
                    {
                        "uid": "floor/0",
                        "type": "Floor",
                        "material": "floor_mat",
                        "xyz": [0.0, 0.0, 0.0, 3.0, 0.0, 0.0, 3.0, 0.0, -3.0, 0.0, 0.0, -3.0],
                        "faces": [0, 1, 2, 0, 2, 3],
                    },
                    {
                        "uid": "wall/0",
                        "type": "Wall",
                        "material": "wall_mat",
                        "xyz": [0.0, 0.0, 0.0, 0.0, 0.0, -3.0, 0.0, 2.5, -3.0, 0.0, 2.5, 0.0],
                        "faces": [0, 1, 2, 0, 2, 3],
                    },
                    {
                        "uid": "door/0",
                        "type": "Door",
                        "material": "door_mat",
                        "xyz": [0.0, 0.0, -1.7, 0.0, 0.0, -1.3, 0.0, 2.1, -1.3, 0.0, 2.1, -1.7],
                        "faces": [0, 1, 2, 0, 2, 3],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    arch_dir = root / "scenegen_architecture_normalized" / scene_id
    arch_obj = arch_dir / f"{scene_id}.obj"
    arch_json = arch_dir / f"{scene_id}.json"
    write_front3d_fixture_obj(arch_obj, material="WallInner")
    arch_json.write_text(
        json.dumps(
            {
                "id": scene_id,
                "files": {"obj": str(arch_obj), "source_scene": str(source_scene_dir / f"{scene_id}.json")},
                "geometry": {"bbox": {"min": [0.0, 0.0, 0.0], "max": [4.0, 3.0, 2.5]}},
                "materials": {
                    "sionna": ["itu-concrete"],
                    "source_to_sionna": [{"source": "WallInner", "sionna": "itu-concrete"}],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    object_dir = root / "scenegen_objects_raw" / replacement_model_id
    object_obj = object_dir / f"{replacement_model_id}.obj"
    object_json = object_dir / f"{replacement_model_id}.json"
    write_front3d_fixture_obj(object_obj, material="wood", height_axis_y=True)
    object_json.write_text(
        json.dumps(
            {
                "id": replacement_model_id,
                "name": "test chair",
                "files": {"obj": str(object_obj)},
                "semantic": {"category": "chair", "super_category": "Chair", "material": "wood"},
                "placement": {"class": "seat", "enabled": True, "support": ["floor"], "weight": 1.0},
                "geometry": {"size": {"x": 0.4, "y": 0.8, "z": 0.4}},
                "materials": {
                    "sionna": ["itu-wood"],
                    "source_to_sionna": [{"source": "wood", "sionna": "itu-wood"}],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manifest_path = root / "scenegen_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "by_scene_id": {
                    scene_id: {"normalized": {"json": str(arch_json), "obj": str(arch_obj), "preview": ""}}
                },
                "by_model_id": {
                    replacement_model_id: {
                        "raw": {"json": str(object_json), "obj": str(object_obj), "preview": ""}
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    config_path = tmp_path / "front3d.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "pipeline": {
                    "mode": "front3d",
                    "scenes": 1,
                    "seed": 123,
                    "output_dir": str(tmp_path / "out"),
                    "run_name": "smoke_front3d",
                },
                "front3d": {
                    "manifest": str(manifest_path),
                    "source_scene_dir": str(source_scene_dir),
                    "variant": "normalized",
                    "scene_ids": [scene_id],
                    "scene_selection": "random",
                    "use_replace_jid": True,
                    "skip_missing_objects": True,
                },
                "floorplan": {
                    "class_mask_enabled": True,
                    "sample_density_scale": 0.01,
                    "min_sample_points": 1000,
                    "max_sample_points": 2000,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path


def test_front3d_scene_outputs_match_standard_layout(tmp_path: Path) -> None:
    pytest.importorskip("trimesh")
    config_path = make_front3d_runtime_fixture(tmp_path)
    output_dir = tmp_path / "out"

    exit_code = main(["--config", str(config_path)])

    scene_dir = output_dir / "smoke_front3d" / "front3d_0000"
    assert exit_code == 0
    for filename in ("scene.obj", "scene.xml", "placements.json", "quality_report.json", "statistics.json"):
        assert (scene_dir / filename).is_file()
    for filename in ("floorplan_1p60.png", "geometry_raw.png", "preview.png", "side_view.png", "meta.json", "stack.npz"):
        assert (scene_dir / "floorplan" / filename).is_file()
    for filename in ("class_mask.png", "class_mask_preview.png", "class_mask.npy", "class_mask.npz", "class_mask_meta.json"):
        assert (scene_dir / "floorplan" / filename).is_file()
    class_mask = np.load(scene_dir / "floorplan" / "class_mask.npy")
    assert class_mask.dtype == np.uint8
    assert {0, 1, 2, 3}.issubset(set(np.unique(class_mask).tolist()))
    class_meta = json.loads((scene_dir / "floorplan" / "class_mask_meta.json").read_text(encoding="utf-8"))
    opening_col = int(round((0.0 - class_meta["origin_xy_m"][0]) / class_meta["resolution_m_per_pixel"]))
    opening_row = int(round(((class_meta["origin_xy_m"][1] + class_meta["extent_xy_m"][1]) - 1.5) / class_meta["resolution_m_per_pixel"]))
    assert class_mask[opening_row, opening_col] == 2
    assert class_meta["classes"]["0"]["name"] == "outdoor"
    assert class_meta["classes"]["1"]["name"] == "wall"
    assert class_meta["classes"]["2"]["name"] == "free_space"
    assert class_meta["classes"]["3"]["name"] == "furniture"
    assert class_meta["opening_mode"] == "doors"
    assert class_meta["opening_type_counts"]["door"] == 1
    assert class_meta["class_id_counts"]["1"] > 0
    assert class_meta["class_id_counts"]["2"] > 0
    assert class_meta["class_id_counts"]["3"] > 0
    assert (output_dir / "smoke_front3d" / "manifest_front3d.json").is_file()
    assert (output_dir / "smoke_front3d" / "summary_obj" / "front3d_0000.obj").is_file()
    assert (output_dir / "smoke_front3d" / "summary_floorplan_raw" / "front3d_0000_geometry_raw.png").is_file()

    placements = json.loads((scene_dir / "placements.json").read_text(encoding="utf-8"))
    assert placements["mode"] == "front3d"
    assert placements["placement_count"] == 1
    assert placements["skipped_object_count"] == 0
    placement = placements["placements"][0]
    assert placement["source_ids"]["model_id"] == "model-replacement"
    assert placement["source_ids"]["original_jid"] == "model-original"
    assert placement["metadata"]["used_replace_jid"] is True
    assert placement["translation"] == [1.0, 1.0, 0.0]
    assert "transform_matrix_4x4_row_major" in placement
    assert not (scene_dir / "label.json").exists()
    assert not (scene_dir / "label_report.json").exists()
    label = json.loads((scene_dir / "label" / "label_walk_0p1.json").read_text(encoding="utf-8"))
    assert label["label_version"] == "1.1"
    assert label["generator"] == "front3d_auto"
    assert len(label["groups"]) == 1
    assert label["groups"][0]["room_id"] == "room/0"
    assert label["groups"][0]["bs_positions"] == [point["position"] for point in label["groups"][0]["bs_points"]]
    assert label["groups"][0]["ue_positions"] == [
        [point["x"], point["y"], point["z"]] for point in label["groups"][0]["ue_points"]
    ]
    assert all("position" in point and "x" not in point and "y" not in point and "z" not in point for point in label["bs_points"])
    assert label["ue_points"]
    assert all(round(float(point["z"]), 3) == 1.6 for point in label["ue_points"])
    assert {point["strategy"] for point in label["ue_points"]} == {"free_space_grid"}
    assert len(label["groups"][0]["bs_points"]) <= 4
    assert (scene_dir / "label" / "label_walk_0p1.json").is_file()
    report = json.loads((scene_dir / "label" / "report" / "label_walk_0p1_report.json").read_text(encoding="utf-8"))
    assert report["bs_count"] == len(label["bs_points"])
    assert report["ue_count"] == len(label["ue_points"])
    assert report["group_count"] == len(label["groups"])
    room_sampling = report["rooms"][0]["ue_sampling"]
    assert room_sampling["sampling_source"] == "front3d_global_rect_subtractive_mask"
    assert room_sampling["sampling_pipeline_version"] == "2.0.0"
    assert room_sampling["door_opening_mode"] == "doors"
    assert room_sampling["windows_used_as_openings"] is False
    assert room_sampling["door_type_counts"]["door"] == 1
    assert room_sampling["door_after_wall_clearance_count"] > 0
    assert not (scene_dir / "label" / "label_walk_0p1_report.json").exists()
    assert not (scene_dir / "floorplan" / "label_overlay.png").exists()
    overlay_path = scene_dir / "label_floorplan" / "label_walk_0p1.png"
    assert overlay_path.is_file()
    overlay_rgb = np.asarray(Image.open(overlay_path).convert("RGB"))
    red_marker_pixels = (overlay_rgb[:, :, 0] > 180) & (overlay_rgb[:, :, 1] < 80) & (overlay_rgb[:, :, 2] < 80)
    assert not red_marker_pixels.any()

    manifest = json.loads((output_dir / "smoke_front3d" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "front3d"
    assert manifest["front3d_variant"] == "normalized"
    assert manifest["front3d_object_variant"] == "raw"
    assert manifest["front3d_skipped_object_count"] == 0
    assert manifest["label_variants"] == ["label_walk_0p1"]
    assert manifest["label_variant_count"] == 1
    assert manifest["summary_floorplan_raw"]["count"] == 1
    quality = json.loads((scene_dir / "quality_report.json").read_text(encoding="utf-8"))
    assert quality["ok"] is True

    json_files = [
        output_dir / "smoke_front3d" / "manifest.json",
        output_dir / "smoke_front3d" / "manifest_front3d.json",
        output_dir / "smoke_front3d" / "statistics.json",
        output_dir / "smoke_front3d" / "summary_obj" / "copy_manifest.json",
        output_dir / "smoke_front3d" / "summary_floorplan_raw" / "copy_manifest.json",
        scene_dir / "placements.json",
        scene_dir / "label" / "label_walk_0p1.json",
        scene_dir / "label" / "report" / "label_walk_0p1_report.json",
        scene_dir / "floorplan" / "meta.json",
        scene_dir / "floorplan" / "class_mask_meta.json",
        scene_dir / "quality_report.json",
        scene_dir / "statistics.json",
    ]
    for json_file in json_files:
        payload = json.loads(json_file.read_text(encoding="utf-8"))
        assert not [value for value in iter_json_strings(payload) if value.startswith("/")]


def test_front3d_batch_label_outputs(tmp_path: Path) -> None:
    pytest.importorskip("trimesh")
    config_path = make_front3d_runtime_fixture(tmp_path)
    output_dir = tmp_path / "out"

    exit_code = main(
        [
            "--config",
            str(config_path),
            "--label-batch-strategies",
            "plane_grid,free_space_grid",
            "--label-batch-grid-resolutions",
            "0.1,0.2",
        ]
    )

    scene_dir = output_dir / "smoke_front3d" / "front3d_0000"
    assert exit_code == 0
    expected_names = {"label_panel_0p1", "label_panel_0p2", "label_walk_0p1", "label_walk_0p2"}
    assert {path.stem for path in (scene_dir / "label").glob("label_*.json")} == expected_names
    assert not list((scene_dir / "label").glob("*_report.json"))
    assert {path.stem.removesuffix("_report") for path in (scene_dir / "label" / "report").glob("*_report.json")} == expected_names
    assert {path.stem for path in (scene_dir / "label_floorplan").glob("label_*.png")} == expected_names

    for name in expected_names:
        label_path = scene_dir / "label" / f"{name}.json"
        report_path = scene_dir / "label" / "report" / f"{name}_report.json"
        overlay_path = scene_dir / "label_floorplan" / f"{name}.png"
        assert label_path.is_file()
        assert report_path.is_file()
        assert overlay_path.is_file()
        label = json.loads(label_path.read_text(encoding="utf-8"))
        strategy = "plane_grid" if "_panel_" in name else "free_space_grid"
        assert {point["strategy"] for point in label["ue_points"]} == {strategy}

    assert not (scene_dir / "label.json").exists()
    assert not (scene_dir / "label_report.json").exists()
    panel_label = json.loads((scene_dir / "label" / "label_panel_0p1.json").read_text(encoding="utf-8"))
    walk_label = json.loads((scene_dir / "label" / "label_walk_0p1.json").read_text(encoding="utf-8"))
    assert len(panel_label["ue_points"]) >= len(walk_label["ue_points"])
    walk_report = json.loads((scene_dir / "label" / "report" / "label_walk_0p1_report.json").read_text(encoding="utf-8"))
    walk_sampling = walk_report["rooms"][0]["ue_sampling"]
    assert walk_report["bs_count"] == len(walk_label["bs_points"])
    assert walk_report["ue_count"] == len(walk_label["ue_points"])
    assert walk_report["group_count"] == len(walk_label["groups"])
    assert walk_sampling["sampling_source"] == "front3d_global_rect_subtractive_mask"
    assert walk_sampling["windows_used_as_openings"] is False
    assert walk_sampling["walk_obstacle_mode"] == "height_aware"
    assert walk_sampling["walk_blocking_obstacle_count"] == 1
    assert walk_sampling["obstacle_rejected_count"] >= 0

    manifest = json.loads((output_dir / "smoke_front3d" / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["label_variants"]) == expected_names
    assert manifest["label_variant_count"] == 4
    scene_record = manifest["scenes"][0]
    assert scene_record["label"]["variant_count"] == 4
    assert scene_record["label"]["primary"] == "label_panel_0p1"
    assert scene_record["label"]["report_dir"] == "front3d_0000/label/report"
    assert len(scene_record["label"]["overlays"]) == 4


def test_front3d_connected_area_batch_outputs_four_modes(tmp_path: Path) -> None:
    pytest.importorskip("trimesh")
    config_path = make_front3d_runtime_fixture(tmp_path)
    output_dir = tmp_path / "out"
    source_scene_path = tmp_path / "front3d" / "3D-FRONT" / "scene-front3d-test.json"
    source_scene = json.loads(source_scene_path.read_text(encoding="utf-8"))
    source_scene["mesh"].append(
        {
            "uid": "floor/connected",
            "type": "Floor",
            "material": "floor_mat",
            "xyz": [3.1, 0.0, -1.7, 3.9, 0.0, -1.7, 3.9, 0.0, -1.3, 3.1, 0.0, -1.3],
            "faces": [0, 1, 2, 0, 2, 3],
        }
    )
    source_scene_path.write_text(json.dumps(source_scene, ensure_ascii=False), encoding="utf-8")

    exit_code = main(
        [
            "--config",
            str(config_path),
            "--label-batch-strategies",
            "plane_grid,free_space_grid",
            "--label-batch-grid-resolutions",
            "0.1",
            "--label-batch-connected-area-enabled",
            "true,false",
            "--label-bs-strategy",
            "geometry_center",
        ]
    )

    scene_dir = output_dir / "smoke_front3d" / "front3d_0000"
    assert exit_code == 0
    expected_names = {
        "label_panel_connected_0p1",
        "label_panel_room_0p1",
        "label_walk_connected_0p1",
        "label_walk_room_0p1",
    }
    assert {path.stem for path in (scene_dir / "label").glob("label_*.json")} == expected_names
    assert {path.stem for path in (scene_dir / "label_floorplan").glob("label_*.png")} == expected_names
    panel_connected_label = json.loads((scene_dir / "label" / "label_panel_connected_0p1.json").read_text(encoding="utf-8"))
    panel_room_label = json.loads((scene_dir / "label" / "label_panel_room_0p1.json").read_text(encoding="utf-8"))
    walk_connected_label = json.loads((scene_dir / "label" / "label_walk_connected_0p1.json").read_text(encoding="utf-8"))
    walk_room_label = json.loads((scene_dir / "label" / "label_walk_room_0p1.json").read_text(encoding="utf-8"))
    corridor_groups = [group for group in panel_connected_label["groups"] if group["room_id"] == "__corridor__"]
    assert corridor_groups
    assert len(corridor_groups[0]["ue_points"]) > 0
    assert len(panel_connected_label["ue_points"]) > len(panel_room_label["ue_points"])
    assert len(walk_connected_label["ue_points"]) > len(walk_room_label["ue_points"])

    panel_connected = json.loads((scene_dir / "label" / "report" / "label_panel_connected_0p1_report.json").read_text(encoding="utf-8"))
    panel_room = json.loads((scene_dir / "label" / "report" / "label_panel_room_0p1_report.json").read_text(encoding="utf-8"))
    walk_connected = json.loads((scene_dir / "label" / "report" / "label_walk_connected_0p1_report.json").read_text(encoding="utf-8"))
    walk_room = json.loads((scene_dir / "label" / "report" / "label_walk_room_0p1_report.json").read_text(encoding="utf-8"))

    assert panel_connected["rooms"][0]["ue_sampling"]["sampling_source"] == "front3d_global_rect_subtractive_mask"
    assert panel_connected["rooms"][0]["ue_sampling"]["connected_area_enabled"] is True
    assert panel_connected["rooms"][0]["ue_sampling"]["panel_obstacle_mode"] == "none"
    assert panel_connected["rooms"][0]["ue_sampling"]["panel_furniture_filter_enabled"] is False
    assert walk_connected["rooms"][0]["ue_sampling"]["sampling_source"] == "front3d_global_rect_subtractive_mask"
    assert walk_connected["rooms"][0]["ue_sampling"]["connected_area_enabled"] is True
    assert panel_room["rooms"][0]["ue_sampling"]["sampling_source"] == "front3d_global_rect_subtractive_mask"
    assert panel_room["rooms"][0]["ue_sampling"]["connected_area_enabled"] is False
    assert walk_room["rooms"][0]["ue_sampling"]["sampling_source"] == "front3d_global_rect_subtractive_mask"
    assert walk_room["rooms"][0]["ue_sampling"]["connected_area_enabled"] is False

    manifest = json.loads((output_dir / "smoke_front3d" / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["label_variants"]) == expected_names
    assert manifest["label_batch_connected_area_enabled"] == [True, False]


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
    for filename in ("scene.obj", "scene.xml", "placements.json"):
        assert (scene_dir / filename).is_file()
    for filename in (
        "preview.png",
        "side_view.png",
        "meta.json",
        "stack.npz",
        "geometry_raw.png",
        "floorplan_1p60.png",
    ):
        assert (scene_dir / "floorplan" / filename).is_file()
    assert not (scene_dir / "label.json").exists()
    assert not (scene_dir / "label_report.json").exists()
    assert not (scene_dir / "floorplan" / "label_overlay.png").exists()
    assert (scene_dir / "label" / "label_walk_0p1.json").is_file()
    assert (scene_dir / "label" / "report" / "label_walk_0p1_report.json").is_file()
    assert (scene_dir / "label_floorplan" / "label_walk_0p1.png").is_file()
    assert not (scene_dir / "floorplan" / "000_z_1.60.png").exists()
    for filename in ("semantic.png", "semantic.json"):
        assert not (scene_dir / "floorplan" / filename).exists()
    for filename in ("quality_report.json", "statistics.json"):
        assert (scene_dir / filename).is_file()
    manifest = json.loads((output_dir / "smoke_generated" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sionna_validation_ok"] is True
    assert manifest["asset_catalog"].endswith("data/catalogs/bistro.v1.json")
    assert manifest["quality_requested"] is True
    assert manifest["quality_ok"] is True
    assert manifest["label_requested"] is True
    assert manifest["label_ok"] is True
    assert manifest["label_overlay_requested"] is True
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
    assert geometry_meta["projection_stats"][0]["image"].endswith("floorplan/floorplan_1p60.png")
    quality = json.loads((scene_dir / "quality_report.json").read_text(encoding="utf-8"))
    assert quality["ok"] is True
    assert quality["error_count"] == 0
    label = json.loads((scene_dir / "label" / "label_walk_0p1.json").read_text(encoding="utf-8"))
    assert label["label_version"] == "1.1"
    assert label["generator"] == "generated_auto"
    assert label["bs_positions"] == [point["position"] for point in label["bs_points"]]
    assert all("position" in point and "x" not in point and "y" not in point and "z" not in point for point in label["bs_points"])
    assert label["ue_positions"] == [[point["x"], point["y"], point["z"]] for point in label["ue_points"]]
    label_report = json.loads((scene_dir / "label" / "report" / "label_walk_0p1_report.json").read_text(encoding="utf-8"))
    assert label_report["bs_count"] == len(label["bs_points"])
    assert label_report["ue_count"] == len(label["ue_points"])
    statistics = json.loads((scene_dir / "statistics.json").read_text(encoding="utf-8"))
    assert statistics["placement_count"] > 0
    assert statistics["object_count_by_class"]
    effective_config = yaml.safe_load((output_dir / "smoke_generated" / "effective_config.yaml").read_text())
    assert effective_config["pipeline"]["mode"] == "generated"
    assert effective_config["pipeline"]["seed"] == 123
    assert "catalog" in effective_config["assets"]
    assert "manifest" not in effective_config["assets"]
    assert effective_config["floorplan"]["min_sample_points"] == 1000
    assert effective_config["floorplan"]["geometry_clean_enabled"] is False
    assert effective_config["floorplan"]["height_mode"] == "heights"
    assert effective_config["floorplan"]["heights_m"] == [1.6]
    assert effective_config["floorplan"]["semantic_enabled"] is False
    assert effective_config["quality"]["enabled"] is True
    assert effective_config["label"]["enabled"] is True
    assert effective_config["label"]["ue_strategy"] == "free_space_grid"
    assert effective_config["label"]["obstacle_strategy"] == "height_aware"

    json_files = [
        output_dir / "smoke_generated" / "manifest.json",
        output_dir / "smoke_generated" / "manifest_generated.json",
        output_dir / "smoke_generated" / "statistics.json",
        output_dir / "smoke_generated" / "summary_obj" / "copy_manifest.json",
        output_dir / "smoke_generated" / "summary_floorplan_raw" / "copy_manifest.json",
        scene_dir / "placements.json",
        scene_dir / "label" / "label_walk_0p1.json",
        scene_dir / "label" / "report" / "label_walk_0p1_report.json",
        scene_dir / "floorplan" / "meta.json",
        scene_dir / "quality_report.json",
        scene_dir / "statistics.json",
    ]
    for json_file in json_files:
        payload = json.loads(json_file.read_text(encoding="utf-8"))
        assert not [value for value in iter_json_strings(payload) if value.startswith("/")]
