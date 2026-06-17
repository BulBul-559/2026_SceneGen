from __future__ import annotations

import hashlib
import json
import random
from argparse import Namespace
from collections import Counter
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest
import yaml
from PIL import Image

from scenegen import __version__
from scenegen.assets import AssetSpec, group_assets_by_class, legacy_item_to_spec, load_assets, resolve_obj_file
from scenegen.batch import main as batch_main
from scenegen.batch import parse_args as parse_batch_args
from scenegen.cli import evaluate_front3d_precheck, evaluate_procedural_precheck, main, parse_args, prepare_run_dir
from scenegen.config import DEFAULT_CONFIG, load_effective_config
from scenegen.exporters import write_clean_obj_full_from_source
from scenegen.floorplan import floorplan_layer_filename, generate_front3d_class_mask, process_scene
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
    point_in_triangles,
    points_in_triangles_mask,
)
from scenegen.models import Asset, Front3DBaseScene, PlacedAsset, SupportTriangle
from scenegen.paths import (
    default_asset_catalog,
    default_asset_manifest,
    default_bistro_base_dir,
    default_config_path,
    find_project_root,
)
from scenegen.procedural import (
    ProceduralAssetEntry,
    ProceduralRoom,
    aggregate_procedural_run_report,
    architecture_meshes_for_rooms,
    assign_room_types_to_areas,
    assign_room_types_to_geometry,
    candidates_for_asset_reuse,
    candidate_pose_for_policy,
    choose_procedural_layout,
    companion_directions,
    desired_classes_from_profile,
    door_keepout_boxes_for_rooms,
    entries_matching_profile_filter,
    make_room_layout,
    object_count_config_for_room,
    object_count_for_room_area,
    placement_policy_for_class,
    procedural_asset_approx_bbox,
    procedural_asset_footprint_size,
    rotate_direction,
    room_adjacencies_for_rooms,
    room_aspect_ratio,
    room_exterior_segments,
    rooms_footprint_metrics,
    room_type_sequence,
    select_room_group_specs,
    select_room_profile,
    write_procedural_source_files,
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


def assert_subset_of_default(payload: dict[str, object], defaults: dict[str, object]) -> None:
    for key, value in payload.items():
        assert key in defaults
        default_value = defaults[key]
        if isinstance(value, dict) and isinstance(default_value, dict):
            assert_subset_of_default(value, default_value)
        else:
            assert value == default_value


def assert_keys_subset_of_default(payload: dict[str, object], defaults: dict[str, object]) -> None:
    for key, value in payload.items():
        assert key in defaults
        default_value = defaults[key]
        if isinstance(value, dict) and isinstance(default_value, dict):
            assert_keys_subset_of_default(value, default_value)


def make_label_config(
    *,
    strategy: str | None = None,
    grid_m: float | None = None,
    **updates: object,
) -> LabelConfig:
    payload = deepcopy(DEFAULT_CONFIG["label"])
    if strategy is not None:
        payload["ue"]["sampling"]["strategies"] = [strategy]
    if grid_m is not None:
        payload["ue"]["sampling"]["grid_m"] = [grid_m]
    for key, value in updates.items():
        target = payload
        parts = key.split("__")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    return LabelConfig.from_mapping(payload, DEFAULT_CONFIG["front3d"]["openings"])


def test_default_paths_point_to_packaged_data() -> None:
    root = find_project_root()

    assert default_bistro_base_dir(root) == root / "data" / "scene"
    assert default_asset_catalog(root) == root / "data" / "catalogs" / "bistro.v1.json"
    assert default_asset_manifest(root) == root / "data" / "assets" / "manifest.json"
    assert default_config_path(root) == root / "config" / "bistro.yaml"
    assert (default_bistro_base_dir(root) / "scene.obj").is_file()
    assert default_asset_catalog(root).is_file()
    assert default_asset_manifest(root).is_file()
    assert default_config_path(root).is_file()


def test_floorplan_layer_filename_uses_height_token() -> None:
    assert floorplan_layer_filename(1.6) == "floorplan_1p60.png"
    assert floorplan_layer_filename(2.0) == "floorplan_2p00.png"


def test_write_clean_obj_full_from_source_removes_material_references(tmp_path: Path) -> None:
    source = tmp_path / "source.obj"
    output = tmp_path / "out" / "asset.obj"
    source.write_text(
        "\n".join(
            [
                "# source asset",
                "mtllib model.mtl",
                "o original",
                "g group",
                "usemtl wood",
                "v 0 0 0",
                "v 1 0 0",
                "v 0 1 0",
                "vt 0 0",
                "vn 0 0 1",
                "f 1/1/1 2/1/1 3/1/1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    write_clean_obj_full_from_source(output, "asset/wood", source)

    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["# Generated by SceneGen for Sionna RT", "o asset_wood"]
    assert "mtllib model.mtl" not in lines
    assert "usemtl wood" not in lines
    assert "o original" not in lines
    assert "g group" not in lines
    assert "v 0 0 0" in lines
    assert "vt 0 0" in lines
    assert "vn 0 0 1" in lines
    assert "f 1/1/1 2/1/1 3/1/1" in lines


def test_procedural_architecture_source_contract(tmp_path: Path) -> None:
    rooms = [
        ProceduralRoom("proc_room_00", "LivingRoom", 0.0, 0.0, 4.0, 3.0, 3.0),
        ProceduralRoom("proc_room_01", "Bedroom", 4.0, 0.0, 8.0, 3.0, 3.0),
    ]

    window_config = {
        "enabled": True,
        "room_probability": 1.0,
        "max_per_room": 1,
        "width_m": [1.0, 1.0],
        "height_m": [0.8, 0.8],
        "sill_height_m": [1.0, 1.0],
        "wall_margin_m": 0.5,
    }
    meshes, room_children = architecture_meshes_for_rooms(
        rooms,
        wall_thickness=0.2,
        door_width=1.0,
        window_config=window_config,
        rng=random.Random(7),
    )
    mesh_types = {str(mesh["type"]) for mesh in meshes}
    all_x = [float(value) for mesh in meshes for value in mesh["xyz"][0::3]]
    all_y = [float(value) for mesh in meshes for value in mesh["xyz"][1::3]]

    assert {"Floor", "Ceiling", "Wall", "Door", "Window"} <= mesh_types
    assert any("/window_" in str(child["ref"]) for item in room_children for child in item["children"])
    assert min(all_x) == pytest.approx(0.0)
    assert min(all_y) == pytest.approx(0.0)
    assert max(all_x) == pytest.approx(8.0)
    assert max(all_y) == pytest.approx(3.0)

    adjacencies = room_adjacencies_for_rooms(rooms, wall_thickness=0.2, door_width=1.0)
    assert len(adjacencies) == 1
    adjacency = adjacencies[0]
    assert adjacency["rooms"] == ["proc_room_00", "proc_room_01"]
    assert adjacency["orientation"] == "vertical"
    assert adjacency["door_width_m"] == pytest.approx(1.0)
    assert adjacency["door_center_xy"] == pytest.approx([4.0, 1.5])

    architecture_obj, source_json, metadata_json, bbox_min, bbox_max = write_procedural_source_files(
        tmp_path / "scene",
        "procedural_test_scene",
        rooms,
        meshes,
        room_children,
        adjacencies,
    )
    source_payload = json.loads(source_json.read_text(encoding="utf-8"))
    metadata = json.loads(metadata_json.read_text(encoding="utf-8"))
    obj_text = architecture_obj.read_text(encoding="utf-8")

    assert architecture_obj.is_file()
    assert "usemtl window" in obj_text
    assert source_payload["uid"] == "procedural_test_scene"
    assert len(source_payload["scene"]["room"]) == 2
    assert source_payload["procedural"]["adjacency_count"] == 1
    assert source_payload["procedural"]["adjacency"][0]["rooms"] == ["proc_room_00", "proc_room_01"]
    assert any(mesh["type"] == "Door" for mesh in source_payload["mesh"])
    assert any(mesh["type"] == "Window" for mesh in source_payload["mesh"])
    assert bbox_min == pytest.approx((0.0, 0.0, 0.0))
    assert bbox_max == pytest.approx((8.0, 3.0, 3.0))
    assert metadata["asset_kind"] == "architecture"
    assert metadata["procedural"]["room_count"] == 2
    assert metadata["procedural"]["adjacency_count"] == 1
    assert metadata["procedural"]["adjacency"][0]["door_bounds_xy"] == pytest.approx([3.9, 1.0, 4.1, 2.0])
    assert "itu-glass" in metadata["materials"]["sionna"]
    assert any(mapping["source"] == "window" and mapping["sionna"] == "itu-glass" for mapping in metadata["materials"]["source_to_sionna"])


def test_procedural_door_keepout_boxes_expand_adjacency_doors() -> None:
    rooms = [
        ProceduralRoom("proc_room_00", "LivingRoom", 0.0, 0.0, 4.0, 3.0, 3.0),
        ProceduralRoom("proc_room_01", "Bedroom", 4.0, 0.0, 8.0, 3.0, 3.0),
    ]
    adjacencies = room_adjacencies_for_rooms(rooms, wall_thickness=0.2, door_width=1.0)

    keepouts = door_keepout_boxes_for_rooms(rooms, adjacencies, clearance_m=0.35)

    assert set(keepouts) == {"proc_room_00", "proc_room_01"}
    assert len(keepouts["proc_room_00"]) == 1
    assert keepouts["proc_room_00"][0] == pytest.approx((3.55, 4.45, 0.65, 2.35, 0.0, 3.0))
    assert keepouts["proc_room_01"][0] == pytest.approx((3.55, 4.45, 0.65, 2.35, 0.0, 3.0))
    assert door_keepout_boxes_for_rooms(rooms, adjacencies, clearance_m=0.0) == {"proc_room_00": [], "proc_room_01": []}


def test_procedural_split_tree_layout_tiles_complete_positive_footprint() -> None:
    rooms = make_room_layout(
        random.Random(2026),
        "split_tree",
        (5, 5),
        (3.0, 5.0),
        (3.0, 5.0),
        (2.8, 2.8),
        ("LivingRoom", "Bedroom", "DiningRoom"),
    )
    assert len(rooms) == 5
    assert min(room.x0 for room in rooms) == pytest.approx(0.0)
    assert min(room.y0 for room in rooms) == pytest.approx(0.0)
    assert all(room.width >= 3.0 for room in rooms)
    assert all(room.length >= 3.0 for room in rooms)
    total_area = sum(room.area for room in rooms)
    bbox_area = (max(room.x1 for room in rooms) - min(room.x0 for room in rooms)) * (
        max(room.y1 for room in rooms) - min(room.y0 for room in rooms)
    )
    assert total_area == pytest.approx(bbox_area)
    for left_index, left_room in enumerate(rooms):
        for right_room in rooms[left_index + 1 :]:
            x_overlap = min(left_room.x1, right_room.x1) - max(left_room.x0, right_room.x0)
            y_overlap = min(left_room.y1, right_room.y1) - max(left_room.y0, right_room.y0)
            assert x_overlap <= 1e-6 or y_overlap <= 1e-6


def test_procedural_rect_union_layout_creates_irregular_connected_footprint() -> None:
    rooms = make_room_layout(
        random.Random(2027),
        "rect_union",
        (6, 6),
        (3.0, 5.0),
        (3.0, 5.0),
        (2.8, 2.8),
        ("LivingRoom", "Bedroom", "Kitchen", "Bathroom"),
        required_room_types={"LivingRoom": 1, "Bedroom": 1, "Kitchen": 1},
        room_type_assignment="geometry_fit",
        room_type_area_priority=("LivingRoom", "Bedroom", "Kitchen", "Bathroom"),
    )
    assert len(rooms) == 6
    assert {"LivingRoom", "Bedroom", "Kitchen"}.issubset({room.room_type for room in rooms})
    assert min(room.x0 for room in rooms) == pytest.approx(0.0)
    assert min(room.y0 for room in rooms) == pytest.approx(0.0)
    assert all(room.width >= 3.0 for room in rooms)
    assert all(room.length >= 3.0 for room in rooms)

    total_area = sum(room.area for room in rooms)
    bbox_area = (max(room.x1 for room in rooms) - min(room.x0 for room in rooms)) * (
        max(room.y1 for room in rooms) - min(room.y0 for room in rooms)
    )
    assert total_area < bbox_area

    adjacencies = room_adjacencies_for_rooms(rooms, wall_thickness=0.16, door_width=1.0)
    graph = {room.room_id: set() for room in rooms}
    for adjacency in adjacencies:
        left, right = adjacency["rooms"]
        graph[str(left)].add(str(right))
        graph[str(right)].add(str(left))
    visited = {rooms[0].room_id}
    frontier = [rooms[0].room_id]
    while frontier:
        room_id = frontier.pop()
        for neighbor in graph[room_id] - visited:
            visited.add(neighbor)
            frontier.append(neighbor)
    assert visited == set(graph)


def test_procedural_mixed_layout_uses_configured_weights() -> None:
    rng = random.Random(12)

    assert choose_procedural_layout("split_tree", {"rect_union": 1.0}, rng) == "split_tree"
    assert choose_procedural_layout("mixed", {"split_tree": 0.0, "rect_union": 1.0, "grid": 0.0}, rng) == "rect_union"
    assert (
        choose_procedural_layout(
            "mixed",
            {"split_tree": 0.0, "rect_union": 0.0, "corridor_spine": 1.0, "grid": 0.0},
            rng,
        )
        == "corridor_spine"
    )

    with pytest.raises(ValueError, match="layout_weights"):
        choose_procedural_layout("mixed", {"split_tree": 0.0, "rect_union": 0.0, "corridor_spine": 0.0, "grid": 0.0}, rng)


def test_procedural_corridor_spine_layout_connects_rooms_through_hallway() -> None:
    rooms = make_room_layout(
        random.Random(2031),
        "corridor_spine",
        (6, 6),
        (3.2, 5.8),
        (3.2, 6.4),
        (2.8, 2.8),
        ("LivingRoom", "Bedroom", "Kitchen", "Bathroom", "Hallway"),
        required_room_types={"LivingRoom": 1, "Bedroom": 1, "Kitchen": 1},
        room_type_assignment="geometry_fit",
        room_type_area_priority=("LivingRoom", "Bedroom", "Kitchen", "Bathroom", "Hallway"),
        room_type_max_counts={"LivingRoom": 2, "Bedroom": None, "Kitchen": 1, "Bathroom": 2, "Hallway": 2},
        room_type_weights={"LivingRoom": 2.0, "Bedroom": 2.0, "Kitchen": 1.0, "Bathroom": 0.8},
        room_type_geometry_rules=DEFAULT_CONFIG["procedural"]["precheck"]["room_type_geometry"],
    )
    hallway_ids = {room.room_id for room in rooms if room.room_type == "Hallway"}
    assert hallway_ids
    assert {"LivingRoom", "Bedroom", "Kitchen"}.issubset({room.room_type for room in rooms})
    for room in rooms:
        if room.room_type == "Hallway":
            assert room.area <= 30.0
            assert room_aspect_ratio(room.width, room.length) <= 4.0

    adjacencies = room_adjacencies_for_rooms(rooms, wall_thickness=0.16, door_width=1.0)
    graph = {room.room_id: set() for room in rooms}
    for adjacency in adjacencies:
        left, right = (str(value) for value in adjacency["rooms"])
        graph[left].add(right)
        graph[right].add(left)
    for room in rooms:
        if room.room_type != "Hallway":
            assert graph[room.room_id] & hallway_ids


def test_procedural_exterior_segments_follow_room_union_not_bbox() -> None:
    rooms = [
        ProceduralRoom("a", "LivingRoom", 0.0, 0.0, 4.0, 4.0, 3.0),
        ProceduralRoom("b", "Bedroom", 4.0, 0.0, 8.0, 4.0, 3.0),
        ProceduralRoom("c", "Kitchen", 0.0, 4.0, 4.0, 8.0, 3.0),
    ]

    segments = room_exterior_segments(rooms[0], rooms)

    assert segments["north"] == []
    assert segments["east"] == []
    assert segments["south"] == [(0.0, 4.0)]
    assert segments["west"] == [(0.0, 4.0)]


def test_procedural_footprint_metrics_measure_concavity() -> None:
    rooms = [
        ProceduralRoom("a", "LivingRoom", 0.0, 0.0, 4.0, 4.0, 3.0),
        ProceduralRoom("b", "Bedroom", 4.0, 0.0, 8.0, 4.0, 3.0),
        ProceduralRoom("c", "Kitchen", 0.0, 4.0, 4.0, 8.0, 3.0),
    ]

    metrics = rooms_footprint_metrics(rooms)

    assert metrics["room_area_m2"] == pytest.approx(48.0)
    assert metrics["bbox_area_m2"] == pytest.approx(64.0)
    assert metrics["fill_ratio"] == pytest.approx(0.75)
    assert metrics["concavity_area_m2"] == pytest.approx(16.0)
    assert metrics["bbox_xy"] == [0.0, 0.0, 8.0, 8.0]


def test_procedural_room_type_sequence_supports_weights() -> None:
    weighted = room_type_sequence(
        8,
        ("LivingRoom", "Bedroom", "Kitchen"),
        random.Random(9),
        shuffle=True,
        room_type_weights={"LivingRoom": 0.0, "Bedroom": 0.0, "Kitchen": 1.0},
    )
    legacy = room_type_sequence(5, ("LivingRoom", "Bedroom"), random.Random(1), shuffle=False)

    assert weighted == ["Kitchen"] * 8
    assert legacy == ["LivingRoom", "Bedroom", "LivingRoom", "Bedroom", "LivingRoom"]


def test_procedural_room_type_sequence_keeps_required_types() -> None:
    sequence = room_type_sequence(
        5,
        ("LivingRoom", "Bedroom", "Kitchen", "Bathroom"),
        random.Random(11),
        shuffle=True,
        required_room_types={"LivingRoom": 1, "Bedroom": 1, "Kitchen": 1},
        room_type_weights={"LivingRoom": 0.0, "Bedroom": 0.0, "Kitchen": 0.0, "Bathroom": 1.0},
    )

    assert len(sequence) == 5
    assert sequence.count("LivingRoom") == 1
    assert sequence.count("Bedroom") == 1
    assert sequence.count("Kitchen") == 1
    assert sequence.count("Bathroom") == 2


def test_procedural_room_type_sequence_respects_max_counts() -> None:
    sequence = room_type_sequence(
        6,
        ("LivingRoom", "Bedroom", "Kitchen", "Bathroom"),
        random.Random(12),
        shuffle=True,
        required_room_types={"LivingRoom": 1, "Bedroom": 1, "Kitchen": 1},
        room_type_max_counts={"LivingRoom": 1, "Bedroom": None, "Kitchen": 1, "Bathroom": 1},
        room_type_weights={"LivingRoom": 10.0, "Bedroom": 1.0, "Kitchen": 10.0, "Bathroom": 10.0},
    )

    assert len(sequence) == 6
    assert sequence.count("LivingRoom") == 1
    assert sequence.count("Kitchen") == 1
    assert sequence.count("Bathroom") == 1
    assert sequence.count("Bedroom") == 3


def test_procedural_room_type_area_priority_maps_large_rooms() -> None:
    assigned = assign_room_types_to_areas(
        ["Bathroom", "Bedroom", "LivingRoom"],
        [12.0, 30.0, 18.0],
        assignment="area_priority",
        area_priority=("LivingRoom", "Bedroom", "Bathroom"),
    )
    unchanged = assign_room_types_to_areas(
        ["Bathroom", "Bedroom", "LivingRoom"],
        [12.0, 30.0, 18.0],
        assignment="sequence",
        area_priority=("LivingRoom", "Bedroom", "Bathroom"),
    )

    assert assigned == ["Bathroom", "LivingRoom", "Bedroom"]
    assert unchanged == ["Bathroom", "Bedroom", "LivingRoom"]


def test_procedural_room_type_geometry_fit_uses_precheck_rules() -> None:
    assigned = assign_room_types_to_geometry(
        ["LivingRoom", "Bathroom", "Bedroom"],
        [(9.0, 1.2), (24.0, 1.1), (12.0, 1.3)],
        assignment="geometry_fit",
        area_priority=("LivingRoom", "Bedroom", "Bathroom"),
        geometry_rules={
            "LivingRoom": {"min_area_m2": 16.0, "max_area_m2": None, "max_aspect_ratio": 3.5},
            "Bedroom": {"min_area_m2": 10.0, "max_area_m2": None, "max_aspect_ratio": 3.5},
            "Bathroom": {"min_area_m2": 4.0, "max_area_m2": 10.0, "max_aspect_ratio": 4.0},
        },
    )

    assert assigned == ["Bathroom", "LivingRoom", "Bedroom"]


def test_procedural_asset_approx_footprint_uses_scenegen_xy() -> None:
    asset = Asset(
        name="asset",
        export_name="asset",
        obj_file=Path("asset.obj"),
        width=2.0,
        length=1.5,
        height=4.0,
        placement_class="table",
        source_to_sionna_material={},
        sionna_material_names=("itu-wood",),
    )
    entry = ProceduralAssetEntry(model_id="model", asset=asset, payload={}, semantic={})

    assert procedural_asset_footprint_size(entry, 0.0) == pytest.approx((2.0, 4.0))
    assert procedural_asset_footprint_size(entry, np.pi / 2.0) == pytest.approx((4.0, 2.0))
    assert procedural_asset_approx_bbox(entry, 0.0, 10.0, 20.0) == pytest.approx((9.0, 11.0, 18.0, 22.0, 0.0, 1.5))


def test_procedural_room_profiles_select_and_expand_classes() -> None:
    profiles = {
        "default": {"classes": ["seat"]},
        "Bedroom": {"classes": ["floor", "table"]},
        "LivingRoom": {"classes": ["seat", "table", "floor"]},
    }

    assert select_room_profile("MasterBedroom", profiles)[0] == "Bedroom"
    assert desired_classes_from_profile("Bedroom", profiles, 2, random.Random(1)) == ["floor", "table"]
    assert desired_classes_from_profile("Kitchen", profiles, 3, random.Random(2)) == ["seat", "seat", "seat"]


def test_procedural_object_count_supports_range_and_area_adaptive() -> None:
    assert object_count_for_room_area(
        10.0,
        {"strategy": "range", "range": [3, 3]},
        random.Random(1),
    ) == 3
    assert object_count_for_room_area(
        24.0,
        {"strategy": "area_adaptive", "min": 2, "max": 9, "area_per_object_m2": 4.0, "jitter": [0, 0]},
        random.Random(1),
    ) == 6
    assert object_count_for_room_area(
        200.0,
        {"strategy": "area_adaptive", "min": 2, "max": 9, "area_per_object_m2": 4.0, "jitter": [0, 0]},
        random.Random(1),
    ) == 9


def test_procedural_object_count_supports_room_type_overrides() -> None:
    config = {
        "strategy": "area_adaptive",
        "range": [3, 7],
        "min": 2,
        "max": 9,
        "area_per_object_m2": 4.0,
        "jitter": [0, 0],
        "by_room_type": {
            "Bathroom": {
                "strategy": "area_adaptive",
                "range": [3, 7],
                "min": 1,
                "max": 3,
                "area_per_object_m2": 8.0,
                "jitter": [0, 0],
            }
        },
    }

    bathroom_config = object_count_config_for_room("PrimaryBathroom", config)
    bedroom_config = object_count_config_for_room("Bedroom", config)

    assert object_count_for_room_area(24.0, bathroom_config, random.Random(1)) == 3
    assert object_count_for_room_area(24.0, bedroom_config, random.Random(1)) == 6


def test_procedural_placement_policy_samples_center_and_wall_zones() -> None:
    asset = Asset(
        name="asset",
        export_name="asset",
        obj_file=Path("asset.obj"),
        width=2.0,
        length=1.0,
        height=1.5,
        placement_class="floor",
        source_to_sionna_material={},
        sionna_material_names=("itu-wood",),
    )
    entry = ProceduralAssetEntry(model_id="model", asset=asset, payload={}, semantic={})
    room = ProceduralRoom("room", "Bedroom", 0.0, 0.0, 10.0, 10.0, 3.0)

    center_policy = placement_policy_for_class(
        {"default": {"zone": "anywhere"}, "table": {"zone": "center", "center_radius_ratio": 0.0}},
        "table",
    )
    _yaw, center_x, center_y, _width, _length, zone = candidate_pose_for_policy(
        room,
        entry,
        center_policy,
        margin=0.25,
        rng=random.Random(1),
    )

    assert zone == "center"
    assert center_x == pytest.approx(5.0)
    assert center_y == pytest.approx(5.0)

    wall_policy = placement_policy_for_class(
        {"default": {"zone": "anywhere"}, "floor": {"zone": "wall", "wall_offset_m": 0.0}},
        "floor",
    )
    _yaw, wall_x, wall_y, width, length, zone = candidate_pose_for_policy(
        room,
        entry,
        wall_policy,
        margin=0.25,
        rng=random.Random(2),
    )
    clearances = [
        wall_x - (room.x0 + 0.25 + width / 2.0),
        (room.x1 - 0.25 - width / 2.0) - wall_x,
        wall_y - (room.y0 + 0.25 + length / 2.0),
        (room.y1 - 0.25 - length / 2.0) - wall_y,
    ]

    assert zone == "wall"
    assert min(abs(value) for value in clearances) == pytest.approx(0.0)


def test_procedural_placement_policy_accepts_room_type_overrides() -> None:
    policies = {
        "default": {"zone": "anywhere", "wall_offset_m": 0.0, "center_radius_ratio": 0.35},
        "table": {"zone": "center", "wall_offset_m": 0.0, "center_radius_ratio": 0.25},
        "seat": {"zone": "anywhere", "wall_offset_m": 0.0, "center_radius_ratio": 0.35},
        "by_room_type": {
            "Kitchen": {
                "table": {"zone": "wall", "wall_offset_m": 0.08, "center_radius_ratio": 0.35},
                "default": {"zone": "center", "wall_offset_m": 0.0, "center_radius_ratio": 0.20},
            }
        },
    }

    kitchen_table = placement_policy_for_class(policies, "table", "Kitchen")
    kitchen_seat = placement_policy_for_class(policies, "seat", "Kitchen")
    living_table = placement_policy_for_class(policies, "table", "LivingRoom")

    assert kitchen_table["zone"] == "wall"
    assert kitchen_table["wall_offset_m"] == pytest.approx(0.08)
    assert kitchen_seat["zone"] == "center"
    assert kitchen_seat["center_radius_ratio"] == pytest.approx(0.20)
    assert living_table["zone"] == "center"
    assert living_table["center_radius_ratio"] == pytest.approx(0.25)


def test_procedural_placement_groups_select_by_room_type_and_directions() -> None:
    groups = {
        "enabled": True,
        "room_types": {
            "DiningRoom": [
                {
                    "name": "dining_table_set",
                    "anchor_class": "table",
                    "companion_class": "seat",
                    "companion_count": [2, 4],
                    "companion_gap_m": [0.1, 0.35],
                    "max_attempts": 30,
                }
            ]
        },
    }

    specs = select_room_group_specs("LargeDiningRoom", groups)
    assert specs[0]["name"] == "dining_table_set"
    living_specs = select_room_group_specs("LivingRoom", DEFAULT_CONFIG["procedural"]["placement_groups"])
    kitchen_specs = select_room_group_specs("CompactKitchen", DEFAULT_CONFIG["procedural"]["placement_groups"])
    assert living_specs[0]["name"] == "living_seating_set"
    assert kitchen_specs[0]["name"] == "counter_stool_pair"
    assert select_room_group_specs("Bedroom", DEFAULT_CONFIG["procedural"]["placement_groups"])[0]["name"] == "bed_side_tables"
    assert DEFAULT_CONFIG["procedural"]["room_profiles"]["Bedroom"]["filters"]["table"]["category"] == ["nightstand"]
    assert DEFAULT_CONFIG["procedural"]["room_profiles"]["LivingRoom"]["filters"]["table"]["category"] == [
        "coffee table",
        "tea table",
        "corner/side table",
        "side table",
    ]
    assert "Kitchen" in DEFAULT_CONFIG["procedural"]["room_types"]
    assert DEFAULT_CONFIG["procedural"]["room_profiles"]["Kitchen"]["filters"]["floor"]["category"] == ["cabinet", "kitchen", "shelf"]
    assert DEFAULT_CONFIG["procedural"]["room_profiles"]["Bathroom"]["filters"]["floor"]["super_category"] == ["toilet", "bath", "cabinet"]
    assert select_room_profile("EntryHallway", DEFAULT_CONFIG["procedural"]["room_profiles"])[0] == "Hallway"
    assert companion_directions(2) == [(-1.0, 0.0), (1.0, 0.0)]
    four_directions = companion_directions(4)
    assert len(four_directions) == 4
    assert (0.0, -1.0) in four_directions
    assert rotate_direction((1.0, 0.0), np.pi / 2.0) == pytest.approx((0.0, 1.0))


def test_procedural_asset_reuse_filters_room_and_scene_counts() -> None:
    asset = Asset(
        name="asset",
        export_name="asset",
        obj_file=Path("asset.obj"),
        width=1.0,
        length=1.0,
        height=1.0,
        placement_class="seat",
        source_to_sionna_material={},
        sionna_material_names=("itu-wood",),
    )
    sofa_a = ProceduralAssetEntry(model_id="sofa-a", asset=asset, payload={}, semantic={})
    sofa_b = ProceduralAssetEntry(model_id="sofa-b", asset=asset, payload={}, semantic={})

    allowed, relaxed = candidates_for_asset_reuse(
        [sofa_a, sofa_b],
        "room",
        Counter({"sofa-a": 1}),
        Counter({"sofa-b": 1}),
        {"max_per_scene": 1, "max_per_room": 1, "relax_if_needed": True},
    )

    assert allowed == [sofa_a, sofa_b]
    assert relaxed is True

    allowed, relaxed = candidates_for_asset_reuse(
        [sofa_a, sofa_b],
        "room",
        Counter({"sofa-a": 1}),
        Counter({"sofa-b": 1}),
        {"max_per_scene": 2, "max_per_room": 1, "relax_if_needed": True},
    )

    assert allowed == [sofa_a]
    assert relaxed is False


def test_procedural_room_profile_filters_match_asset_semantics() -> None:
    asset = Asset(
        name="asset",
        export_name="asset",
        obj_file=Path("asset.obj"),
        width=1.0,
        length=1.0,
        height=1.0,
        placement_class="seat",
        source_to_sionna_material={},
        sionna_material_names=("itu-wood",),
    )
    sofa = ProceduralAssetEntry(
        model_id="sofa",
        asset=asset,
        payload={},
        semantic={"category": "three-seat sofa", "super_category": "Sofa", "material": "fabric"},
    )
    chair = ProceduralAssetEntry(
        model_id="chair",
        asset=asset,
        payload={},
        semantic={"category": "dining chair", "super_category": "Chair", "material": "wood"},
    )

    filtered, matched = entries_matching_profile_filter(
        [chair, sofa],
        {"classes": ["seat"], "filters": {"seat": {"super_category": ["sofa"]}}},
        "seat",
    )
    fallback, fallback_matched = entries_matching_profile_filter(
        [chair, sofa],
        {"classes": ["seat"], "filters": {"seat": {"material": ["metal"]}}},
        "seat",
    )

    assert matched is True
    assert [entry.model_id for entry in filtered] == ["sofa"]
    assert fallback_matched is False
    assert fallback == [chair, sofa]


def write_height_filtered_fixture_obj(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []

    def add_quad(points: list[tuple[float, float, float]]) -> None:
        start = len(vertices) + 1
        vertices.extend(points)
        faces.append((start, start + 1, start + 2))
        faces.append((start, start + 2, start + 3))

    add_quad([(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0)])
    add_quad([(2, 0, 0.4), (3, 0, 0.4), (3, 1, 0.4), (2, 1, 0.4)])
    add_quad([(3, 0, 2.0), (4, 0, 2.0), (4, 1, 2.0), (3, 1, 2.0)])
    add_quad([(0, 3, 0), (0, 4, 0), (0, 4, 1.6), (0, 3, 1.6)])

    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for x, y, z in vertices:
            handle.write(f"v {x} {y} {z}\n")
        for face in faces:
            handle.write("f " + " ".join(str(index) for index in face) + "\n")


def run_height_filtered_fixture(input_obj: Path, output_dir: Path) -> dict[str, object]:
    return process_scene(
        input_path=input_obj,
        output_dir=output_dir,
        resolution=0.5,
        step=0.5,
        manual_top_z=None,
        bottom_z=0.0,
        sample_density_scale=1.0,
        min_sample_points=10,
        max_sample_points=100,
        preview_output_path=output_dir / "preview.png",
        side_view_output_path=output_dir / "side_view.png",
        preview_tile_size=160,
        height_mode="heights",
        heights_m=[0.3, 0.5, 1.6],
        projection_mode="ray_height_filtered",
    )


def test_ray_height_filtered_projection_respects_target_height_and_is_deterministic(tmp_path: Path) -> None:
    obj_path = tmp_path / "fixture.obj"
    write_height_filtered_fixture_obj(obj_path)

    first = run_height_filtered_fixture(obj_path, tmp_path / "first")
    run_height_filtered_fixture(obj_path, tmp_path / "second")

    assert first["raw"] == "first/floorplan_0p30.png"
    stack = np.load(tmp_path / "first" / "stack.npz")["stack"]
    assert stack.shape == (3, 9, 9)
    assert stack[0, 0, 0] == 1
    assert stack[0, 0, 4] == 0
    assert stack[1, 0, 4] == 1
    assert stack[2, 0, 6] == 0
    assert stack[2, 6, 0] == 1

    meta = json.loads((tmp_path / "first" / "meta.json").read_text(encoding="utf-8"))
    assert meta["projection_mode"] == "ray_height_filtered"
    assert meta["deterministic"] is True
    assert meta["ray_height_filtered"]["method"] == "deterministic_height_filtered_triangle_raster"
    assert meta["projection_stats"][1]["occupied_pixels"] > meta["projection_stats"][0]["occupied_pixels"]
    assert meta["projection_stats"][2]["occupied_pixels"] >= meta["projection_stats"][1]["occupied_pixels"]

    first_hash = hashlib.sha256((tmp_path / "first" / "floorplan_1p60.png").read_bytes()).hexdigest()
    second_hash = hashlib.sha256((tmp_path / "second" / "floorplan_1p60.png").read_bytes()).hexdigest()
    assert first_hash == second_hash


def write_l_shaped_furniture_obj(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "v 0 0 0.5",
                "v 2 0 0.5",
                "v 2 1 0.5",
                "v 1 1 0.5",
                "v 1 2 0.5",
                "v 0 2 0.5",
                "f 1 2 3 4",
                "f 1 4 5 6",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_points_in_triangles_mask_matches_scalar_check() -> None:
    triangles = (
        SupportTriangle(
            vertices=((0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 2.0, 0.0)),
            area=2.0,
            z=0.0,
        ),
        SupportTriangle(
            vertices=((2.0, 0.0, 0.0), (2.0, 2.0, 0.0), (0.0, 2.0, 0.0)),
            area=2.0,
            z=0.0,
        ),
    )
    points = np.asarray(
        [
            (0.25, 0.25),
            (1.5, 1.5),
            (2.0, 1.0),
            (2.2, 1.0),
            (-0.1, 0.0),
        ],
        dtype=np.float64,
    )
    expected = [point_in_triangles(float(x), float(y), triangles) for x, y in points]
    assert points_in_triangles_mask(points, triangles).tolist() == expected


def make_class_mask_fixture(tmp_path: Path) -> tuple[Front3DBaseScene, list[PlacedAsset]]:
    source_scene = tmp_path / "scene.json"
    metadata_json = tmp_path / "metadata.json"
    furniture_obj = tmp_path / "l_furniture.obj"
    write_l_shaped_furniture_obj(furniture_obj)
    source_scene.write_text(
        json.dumps(
            {
                "mesh": [
                    {
                        "type": "Floor",
                        "xyz": [0, 0, 0, 4, 0, 0, 4, 0, -4, 0, 0, -4],
                        "faces": [0, 1, 2, 0, 2, 3],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    metadata_json.write_text(json.dumps({"variant": "normalized"}), encoding="utf-8")
    base_scene = Front3DBaseScene(
        scene_id="fixture-scene",
        scene_obj=tmp_path / "architecture.obj",
        source_scene_json=source_scene,
        metadata_json=metadata_json,
        bbox_min=(0.0, 0.0, 0.0),
        bbox_max=(4.0, 4.0, 3.0),
        source_bbox_min=(0.0, 0.0, 0.0),
        source_bbox_max=(4.0, 4.0, 3.0),
        world_offset=(0.0, 0.0, 0.0),
        source_to_sionna_material={},
        sionna_material_names=("itu-wood",),
    )
    asset = Asset(
        name="l_furniture",
        export_name="l_furniture",
        obj_file=furniture_obj,
        width=2.0,
        length=2.0,
        height=0.5,
        placement_class="floor",
        source_to_sionna_material={},
        sionna_material_names=("itu-wood",),
    )
    placement = PlacedAsset(
        asset=asset,
        instance_name="l_furniture_0",
        x=1.0,
        y=1.0,
        z=0.0,
        yaw=0.0,
        support_type="front3d_scene",
        parent=None,
        min_x=1.0,
        max_x=3.0,
        min_y=1.0,
        max_y=3.0,
        min_z=0.5,
        max_z=0.5,
        transform_matrix_4x4_row_major=(
            1.0,
            0.0,
            0.0,
            1.0,
            0.0,
            1.0,
            0.0,
            1.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
        ),
    )
    return base_scene, [placement]


def test_front3d_class_mask_mesh_furniture_mode_uses_mesh_footprint(tmp_path: Path) -> None:
    base_scene, placements = make_class_mask_fixture(tmp_path)
    common = {
        "base_scene": base_scene,
        "placements": placements,
        "resolution": 1.0,
        "wall_dilation_m": 0.0,
        "furniture_dilation_m": 0.0,
        "opening_mode": "none",
        "opening_dilation_m": 0.0,
        "opening_floor_tolerance_m": 0.25,
        "opening_min_height_m": 1.6,
        "include_doors_as_wall": True,
        "include_windows_as_wall": True,
        "path_root": tmp_path,
    }

    generate_front3d_class_mask(
        output_dir=tmp_path / "bbox",
        furniture_mode="bbox",
        furniture_height_m=None,
        **common,
    )
    generate_front3d_class_mask(
        output_dir=tmp_path / "mesh",
        furniture_mode="mesh",
        furniture_height_m=1.6,
        **common,
    )

    bbox_mask = np.load(tmp_path / "bbox" / "class_mask.npy")
    mesh_mask = np.load(tmp_path / "mesh" / "class_mask.npy")
    bbox_furniture_pixels = int((bbox_mask == 3).sum())
    mesh_furniture_pixels = int((mesh_mask == 3).sum())
    assert mesh_furniture_pixels > 0
    assert mesh_furniture_pixels < bbox_furniture_pixels

    mesh_meta = json.loads((tmp_path / "mesh" / "class_mask_meta.json").read_text(encoding="utf-8"))
    assert mesh_meta["furniture_mode"] == "mesh"
    assert mesh_meta["furniture_mask"]["mesh_object_count"] == 1
    assert mesh_meta["furniture_mask"]["fallback_bbox_count"] == 0
    assert mesh_meta["furniture_mask"]["method"] == "pil_height_filtered_triangle_projection"
    assert "unique_projected_primitive_count" in mesh_meta["furniture_mask"]
    assert "build_furniture_mask" in mesh_meta["timings_s"]


def test_cli_version_matches_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"SceneGen {__version__}"


def test_bistro_config_is_mode_specific_default_overlay() -> None:
    root = find_project_root()
    payload = yaml.safe_load((root / "config" / "bistro.yaml").read_text(encoding="utf-8"))

    assert "front3d" not in payload
    assert payload["pipeline"]["mode"] == "bistro"
    assert_subset_of_default(payload, DEFAULT_CONFIG)


def test_front3d_config_is_mode_specific_default_overlay() -> None:
    root = find_project_root()
    payload = yaml.safe_load((root / "config" / "front3d.yaml").read_text(encoding="utf-8"))
    expected = deepcopy(DEFAULT_CONFIG["front3d"])

    assert "assets" not in payload
    assert "bistro" not in payload
    assert "placement" not in payload
    assert payload["pipeline"]["mode"] == "front3d"
    assert payload["front3d"] == expected
    overlay_payload = deepcopy(payload)
    overlay_payload["pipeline"]["mode"] = "bistro"
    assert_subset_of_default(overlay_payload, DEFAULT_CONFIG)


def test_procedural_front3d_config_is_mode_specific_default_overlay() -> None:
    root = find_project_root()
    payload = yaml.safe_load((root / "config" / "procedural_front3d.yaml").read_text(encoding="utf-8"))

    assert "assets" not in payload
    assert "bistro" not in payload
    assert "placement" not in payload
    assert payload["pipeline"]["mode"] == "procedural_front3d"
    assert payload["procedural"] == DEFAULT_CONFIG["procedural"]
    assert payload["floorplan"]["class_mask"]["enabled"] is True
    assert payload["floorplan"]["class_mask"]["furniture_height_m"] == 1.6
    assert_keys_subset_of_default(payload, DEFAULT_CONFIG)


@pytest.mark.parametrize("config_name", ["bistro.yaml", "front3d.yaml", "procedural_front3d.yaml"])
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
    config_path.write_text("floorplan:\n  sampling:\n    density_scale: 0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="floorplan.sampling.density_scale"):
        load_effective_config(config_path, root, parse_args([]))


def test_invalid_procedural_layout_is_rejected(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "bad_config.yaml"
    config_path.write_text("procedural:\n  layout: maze\n", encoding="utf-8")

    with pytest.raises(ValueError, match="procedural.layout"):
        load_effective_config(config_path, root, parse_args([]))


def test_procedural_mixed_layout_config_validates_weights(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "mixed_layout.yaml"
    config_path.write_text(
        "procedural:\n"
        "  layout: mixed\n"
        "  layout_weights:\n"
        "    split_tree: 0\n"
        "    rect_union: 1\n"
        "    corridor_spine: 0\n"
        "    grid: 0\n",
        encoding="utf-8",
    )

    effective, _overrides = load_effective_config(config_path, root, parse_args([]))

    assert effective["procedural"]["layout"] == "mixed"
    assert effective["procedural"]["layout_weights"] == {
        "split_tree": 0.0,
        "rect_union": 1.0,
        "corridor_spine": 0.0,
        "grid": 0.0,
    }

    bad_zero = tmp_path / "bad_mixed_zero.yaml"
    bad_zero.write_text(
        "procedural:\n"
        "  layout: mixed\n"
        "  layout_weights:\n"
        "    split_tree: 0\n"
        "    rect_union: 0\n"
        "    corridor_spine: 0\n"
        "    grid: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="layout_weights"):
        load_effective_config(bad_zero, root, parse_args([]))

    bad_unknown = tmp_path / "bad_mixed_unknown.yaml"
    bad_unknown.write_text(
        "procedural:\n"
        "  layout_weights:\n"
        "    polygon_shell: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Unknown config field"):
        load_effective_config(bad_unknown, root, parse_args([]))


def test_procedural_corridor_spine_config_requires_capacity(tmp_path: Path) -> None:
    root = find_project_root()
    missing_hallway = tmp_path / "corridor_missing_hallway.yaml"
    missing_hallway.write_text(
        "procedural:\n"
        "  layout: corridor_spine\n"
        "  room_types: [LivingRoom, Bedroom, Kitchen]\n"
        "  room_count: [4, 5]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must include 'Hallway'"):
        load_effective_config(missing_hallway, root, parse_args([]))

    hall_limit = tmp_path / "corridor_hall_limit.yaml"
    hall_limit.write_text(
        "procedural:\n"
        "  layout: corridor_spine\n"
        "  room_count: [5, 5]\n"
        "  room_type_max_counts:\n"
        "    LivingRoom: 1\n"
        "    Bedroom: null\n"
        "    Kitchen: 1\n"
        "    Hallway: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="room_type_max_counts.Hallway"):
        load_effective_config(hall_limit, root, parse_args([]))

    room_limit = tmp_path / "corridor_room_limit.yaml"
    room_limit.write_text(
        "procedural:\n"
        "  layout: corridor_spine\n"
        "  room_count: [4, 4]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="room_count max"):
        load_effective_config(room_limit, root, parse_args([]))

    mixed_without_corridor = tmp_path / "mixed_without_corridor.yaml"
    mixed_without_corridor.write_text(
        "procedural:\n"
        "  layout: mixed\n"
        "  room_count: [3, 3]\n"
        "  layout_weights:\n"
        "    split_tree: 1\n"
        "    rect_union: 0\n"
        "    corridor_spine: 0\n"
        "    grid: 0\n",
        encoding="utf-8",
    )
    effective, _overrides = load_effective_config(mixed_without_corridor, root, parse_args([]))
    assert effective["procedural"]["layout"] == "mixed"


def test_procedural_room_profiles_accept_custom_names_and_reject_bad_classes(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "procedural_profiles.yaml"
    config_path.write_text(
        "procedural:\n"
        "  room_profiles:\n"
        "    default:\n"
        "      classes: [seat]\n"
        "    Kitchen:\n"
        "      classes: [table, floor]\n",
        encoding="utf-8",
    )

    effective, _overrides = load_effective_config(config_path, root, parse_args([]))

    assert effective["procedural"]["room_profiles"]["Kitchen"]["classes"] == ["table", "floor"]
    assert effective["procedural"]["room_profiles"]["Kitchen"]["filters"]["floor"]["category"] == ["cabinet", "kitchen", "shelf"]

    bad_path = tmp_path / "bad_procedural_profiles.yaml"
    bad_path.write_text(
        "procedural:\n"
        "  room_profiles:\n"
        "    default:\n"
        "      classes: [lamp]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="procedural.room_profiles.default.classes"):
        load_effective_config(bad_path, root, parse_args([]))

    bad_filter_path = tmp_path / "bad_procedural_filter.yaml"
    bad_filter_path.write_text(
        "procedural:\n"
        "  room_profiles:\n"
        "    default:\n"
        "      classes: [seat]\n"
        "      filters:\n"
        "        seat:\n"
        "          unknown_field: [sofa]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="procedural.room_profiles.default.filters.seat.unknown_field"):
        load_effective_config(bad_filter_path, root, parse_args([]))

    bad_policy_path = tmp_path / "bad_procedural_policy.yaml"
    bad_policy_path.write_text(
        "procedural:\n"
        "  placement_policy:\n"
        "    table:\n"
        "      zone: diagonal\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="procedural.placement_policy.table.zone"):
        load_effective_config(bad_policy_path, root, parse_args([]))

    bad_room_policy_path = tmp_path / "bad_procedural_room_policy.yaml"
    bad_room_policy_path.write_text(
        "procedural:\n"
        "  placement_policy:\n"
        "    by_room_type:\n"
        "      Kitchen:\n"
        "        table:\n"
        "          zone: diagonal\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="procedural.placement_policy.by_room_type.Kitchen.table.zone"):
        load_effective_config(bad_room_policy_path, root, parse_args([]))

    bad_group_path = tmp_path / "bad_procedural_group.yaml"
    bad_group_path.write_text(
        "procedural:\n"
        "  placement_groups:\n"
        "    enabled: true\n"
        "    room_types:\n"
        "      DiningRoom:\n"
        "        - name: bad\n"
        "          anchor_class: lamp\n"
        "          companion_class: seat\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="procedural.placement_groups.room_types.DiningRoom"):
        load_effective_config(bad_group_path, root, parse_args([]))


def test_procedural_object_count_accepts_room_type_overrides(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "procedural_object_count_by_room_type.yaml"
    config_path.write_text(
        "procedural:\n"
        "  object_count:\n"
        "    strategy: area_adaptive\n"
        "    min: 2\n"
        "    max: 8\n"
        "    area_per_object_m2: 4.0\n"
        "    jitter: [0, 0]\n"
        "    by_room_type:\n"
        "      CustomStudio:\n"
        "        max: 4\n"
        "        area_per_object_m2: 8.0\n",
        encoding="utf-8",
    )

    effective, _overrides = load_effective_config(config_path, root, parse_args([]))
    studio = effective["procedural"]["object_count"]["by_room_type"]["CustomStudio"]

    assert studio["strategy"] == "area_adaptive"
    assert studio["min"] == 2
    assert studio["max"] == 4
    assert studio["area_per_object_m2"] == 8.0
    assert studio["jitter"] == [0, 0]


def test_procedural_asset_reuse_rejects_invalid_limits(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "bad_asset_reuse.yaml"
    config_path.write_text(
        "procedural:\n"
        "  asset_reuse:\n"
        "    max_per_room: 0\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="procedural.asset_reuse.max_per_room"):
        load_effective_config(config_path, root, parse_args([]))


def test_procedural_room_type_weights_reject_all_zero_for_configured_rooms(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "bad_room_type_weights.yaml"
    config_path.write_text(
        "procedural:\n"
        "  room_types: [Kitchen, Bathroom]\n"
        "  room_count: [2, 2]\n"
        "  required_room_types: null\n"
        "  room_type_weights:\n"
        "    Kitchen: 0\n"
        "    Bathroom: 0\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="procedural.room_type_weights"):
        load_effective_config(config_path, root, parse_args([]))


def test_procedural_required_room_types_reject_inconsistent_config(tmp_path: Path) -> None:
    root = find_project_root()
    unknown_path = tmp_path / "bad_required_room_type.yaml"
    unknown_path.write_text(
        "procedural:\n"
        "  room_types: [LivingRoom]\n"
        "  required_room_types:\n"
        "    Kitchen: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="procedural.required_room_types"):
        load_effective_config(unknown_path, root, parse_args([]))

    too_many_path = tmp_path / "too_many_required_room_types.yaml"
    too_many_path.write_text(
        "procedural:\n"
        "  room_count: [2, 2]\n"
        "  required_room_types:\n"
        "    LivingRoom: 1\n"
        "    Bedroom: 1\n"
        "    Kitchen: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="procedural.required_room_types"):
        load_effective_config(too_many_path, root, parse_args([]))

    fractional_path = tmp_path / "fractional_required_room_types.yaml"
    fractional_path.write_text(
        "procedural:\n"
        "  required_room_types:\n"
        "    LivingRoom: 1.5\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="procedural.required_room_types"):
        load_effective_config(fractional_path, root, parse_args([]))


def test_procedural_room_type_max_counts_filters_disabled_room_types(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "filtered_room_type_max_counts.yaml"
    config_path.write_text(
        "procedural:\n"
        "  room_types: [LivingRoom]\n"
        "  room_count: [1, 1]\n"
        "  required_room_types: null\n"
        "  room_type_max_counts:\n"
        "    LivingRoom: 1\n"
        "    Kitchen: 1\n",
        encoding="utf-8",
    )

    effective, _overrides = load_effective_config(config_path, root, parse_args([]))

    assert effective["procedural"]["room_type_max_counts"] == {"LivingRoom": 1}


def test_procedural_room_type_max_counts_reject_inconsistent_config(tmp_path: Path) -> None:
    root = find_project_root()

    required_exceeds_path = tmp_path / "required_exceeds_room_type_max_counts.yaml"
    required_exceeds_path.write_text(
        "procedural:\n"
        "  room_types: [LivingRoom, Bedroom, Kitchen, DiningRoom, StudyRoom, Bathroom, Hallway]\n"
        "  room_count: [2, 4]\n"
        "  required_room_types:\n"
        "    LivingRoom: 2\n"
        "    Bedroom: 1\n"
        "    Kitchen: 1\n"
        "  room_type_max_counts:\n"
        "    LivingRoom: 1\n"
        "    Bedroom: null\n"
        "    Kitchen: null\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="procedural.required_room_types.LivingRoom"):
        load_effective_config(required_exceeds_path, root, parse_args([]))

    low_capacity_path = tmp_path / "low_capacity_room_type_max_counts.yaml"
    low_capacity_path.write_text(
        "procedural:\n"
        "  room_types: [LivingRoom, Bedroom, Kitchen, DiningRoom, StudyRoom, Bathroom, Hallway]\n"
        "  room_count: [2, 8]\n"
        "  required_room_types: null\n"
        "  room_type_max_counts:\n"
        "    LivingRoom: 1\n"
        "    Bedroom: 1\n"
        "    Kitchen: 1\n"
        "    DiningRoom: 1\n"
        "    StudyRoom: 1\n"
        "    Bathroom: 1\n"
        "    Hallway: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="procedural.room_type_max_counts capacity"):
        load_effective_config(low_capacity_path, root, parse_args([]))


def test_procedural_room_type_assignment_rejects_invalid_mode(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "bad_room_type_assignment.yaml"
    config_path.write_text(
        "procedural:\n"
        "  room_type_assignment: largest_first\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="procedural.room_type_assignment"):
        load_effective_config(config_path, root, parse_args([]))


def test_procedural_sequence_assignment_allows_empty_area_priority(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "sequence_room_type_assignment.yaml"
    config_path.write_text(
        "procedural:\n"
        "  room_type_assignment: sequence\n"
        "  room_type_area_priority: []\n",
        encoding="utf-8",
    )

    effective, _ = load_effective_config(config_path, root, parse_args([]))
    assert effective["procedural"]["room_type_area_priority"] == []


def test_procedural_room_type_geometry_rejects_invalid_bounds(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "bad_room_type_geometry.yaml"
    config_path.write_text(
        "procedural:\n"
        "  precheck:\n"
        "    room_type_geometry:\n"
        "      LivingRoom:\n"
        "        min_area_m2: 20\n"
        "        max_area_m2: 10\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="procedural.precheck.room_type_geometry"):
        load_effective_config(config_path, root, parse_args([]))


def test_prepare_run_dir_clean_only_replaces_named_run(tmp_path: Path) -> None:
    output_root = tmp_path / "results"
    keep_run = output_root / "keep_run"
    target_run = output_root / "target_run"
    keep_run.mkdir(parents=True)
    target_run.mkdir()
    (keep_run / "keep.txt").write_text("keep", encoding="utf-8")
    (target_run / "old.txt").write_text("old", encoding="utf-8")

    run_dir = prepare_run_dir(output_root, "target_run", clean=True)

    assert run_dir == target_run.resolve()
    assert run_dir.is_dir()
    assert not (run_dir / "old.txt").exists()
    assert (keep_run / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_front3d_scene_selection_and_transform() -> None:
    rng = random.Random(7)
    assert choose_scene_ids(["a", "b"], ("b",), "random", 3, rng) == ["b", "b", "b"]
    assert choose_scene_ids(["a", "b"], (), "sequential", 3, rng) == ["a", "b", "a"]

    matrix = scenegen_transform_for_child({"pos": [1, 2, 3], "rot": [0, 0, 0, 1], "scale": [1, 1, 1]})
    assert [round(matrix[index], 6) for index in (3, 7, 11)] == [1, -3, 2]


def test_front3d_precheck_rejects_anomalous_statistics() -> None:
    args = Namespace(
        mode="front3d",
        front3d_precheck_enabled=True,
        front3d_precheck_min_placements=1,
        front3d_precheck_max_z=8,
        front3d_precheck_max_footprint_ratio=5,
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


def test_procedural_precheck_rejects_low_placement_ratio() -> None:
    args = Namespace(
        mode="procedural_front3d",
        procedural_precheck_enabled=True,
        procedural_precheck_min_placements=1,
        procedural_precheck_min_placement_ratio=0.5,
        procedural_precheck_max_skipped_ratio=0.8,
    )

    result = evaluate_procedural_precheck(
        args,
        {"placement_count": 3},
        {
            "skipped_object_count": 7,
            "procedural": {
                "placement_stats": {
                    "desired_object_counts": {
                        "room_a": 5,
                        "room_b": 5,
                    }
                }
            },
        },
    )

    assert result["ok"] is False
    assert result["desired_object_count"] == 10
    assert {error["code"] for error in result["errors"]} == {
        "placement_ratio_too_low",
    }


def test_procedural_precheck_rejects_low_room_placement_ratio() -> None:
    args = Namespace(
        mode="procedural_front3d",
        procedural_precheck_enabled=True,
        procedural_precheck_min_placements=1,
        procedural_precheck_min_placement_ratio=0.5,
        procedural_precheck_min_room_placement_ratio=0.34,
        procedural_precheck_max_skipped_ratio=0.8,
        procedural_precheck_require_connected_rooms=False,
        procedural_precheck_min_room_area_m2=0.0,
        procedural_precheck_max_room_aspect_ratio=None,
        procedural_precheck_room_type_geometry=None,
    )

    result = evaluate_procedural_precheck(
        args,
        {"placement_count": 6},
        {
            "skipped_object_count": 0,
            "procedural": {
                "placement_stats": {
                    "desired_object_counts": {"living": 4, "kitchen": 4},
                    "placed_object_counts": {"living": 4, "kitchen": 1},
                },
                "rooms": [
                    {"room_id": "living", "bounds_xy": [0, 0, 4, 4]},
                    {"room_id": "kitchen", "bounds_xy": [4, 0, 8, 4]},
                ],
                "adjacency": [{"rooms": ["living", "kitchen"], "door_width_m": 1.0}],
            },
        },
    )

    assert result["ok"] is False
    assert result["placed_object_counts"] == {"living": 4, "kitchen": 1}
    room_error = next(error for error in result["errors"] if error["code"] == "room_placement_ratio_too_low")
    assert room_error["threshold"] == pytest.approx(0.34)
    assert room_error["rooms"] == [
        {"room_id": "kitchen", "value": 0.25, "placement_count": 1, "desired_count": 4}
    ]


def test_procedural_precheck_rejects_disconnected_rooms() -> None:
    args = Namespace(
        mode="procedural_front3d",
        procedural_precheck_enabled=True,
        procedural_precheck_min_placements=1,
        procedural_precheck_min_placement_ratio=0.5,
        procedural_precheck_max_skipped_ratio=0.8,
        procedural_precheck_require_connected_rooms=True,
    )
    record = {
        "skipped_object_count": 0,
        "procedural": {
            "room_count": 3,
            "rooms": [{"room_id": "room_a"}, {"room_id": "room_b"}, {"room_id": "room_c"}],
            "adjacency": [{"rooms": ["room_a", "room_b"], "door_width_m": 1.0}],
            "placement_stats": {"desired_object_counts": {"room_a": 1, "room_b": 1, "room_c": 1}},
        },
    }

    result = evaluate_procedural_precheck(args, {"placement_count": 3}, record)

    assert result["ok"] is False
    assert {error["code"] for error in result["errors"]} == {"rooms_not_connected"}
    assert result["room_connectivity"]["component_count"] == 2
    assert result["room_connectivity"]["connected_room_count"] == 2
    assert result["room_connectivity"]["isolated_rooms"] == ["room_c"]


def test_procedural_precheck_accepts_connected_rooms() -> None:
    args = Namespace(
        mode="procedural_front3d",
        procedural_precheck_enabled=True,
        procedural_precheck_min_placements=1,
        procedural_precheck_min_placement_ratio=0.5,
        procedural_precheck_max_skipped_ratio=0.8,
        procedural_precheck_require_connected_rooms=True,
    )

    result = evaluate_procedural_precheck(
        args,
        {"placement_count": 3},
        {
            "skipped_object_count": 0,
            "procedural": {
                "room_count": 3,
                "rooms": [{"room_id": "room_a"}, {"room_id": "room_b"}, {"room_id": "room_c"}],
                "adjacency": [
                    {"rooms": ["room_a", "room_b"], "door_width_m": 1.0},
                    {"rooms": ["room_b", "room_c"], "door_width_m": 0.9},
                ],
                "placement_stats": {"desired_object_counts": {"room_a": 1, "room_b": 1, "room_c": 1}},
            },
        },
    )

    assert result["ok"] is True
    assert result["room_connectivity"]["ok"] is True
    assert result["room_connectivity"]["edge_count"] == 2


def test_procedural_precheck_rejects_bad_room_geometry() -> None:
    args = Namespace(
        mode="procedural_front3d",
        procedural_precheck_enabled=True,
        procedural_precheck_min_placements=1,
        procedural_precheck_min_placement_ratio=0.5,
        procedural_precheck_max_skipped_ratio=0.8,
        procedural_precheck_require_connected_rooms=False,
        procedural_precheck_min_room_area_m2=8.0,
        procedural_precheck_max_room_aspect_ratio=4.0,
    )

    result = evaluate_procedural_precheck(
        args,
        {"placement_count": 3},
        {
            "skipped_object_count": 0,
            "procedural": {
                "room_count": 2,
                "rooms": [
                    {"room_id": "tiny", "bounds_xy": [0.0, 0.0, 2.0, 3.0]},
                    {"room_id": "corridor_like", "area_m2": 20.0, "aspect_ratio": 5.0},
                ],
                "placement_stats": {"desired_object_counts": {"tiny": 1, "corridor_like": 1}},
            },
        },
    )

    assert result["ok"] is False
    assert {error["code"] for error in result["errors"]} == {
        "room_area_too_small",
        "room_aspect_ratio_too_high",
    }
    assert result["room_geometry"]["small_rooms"] == [{"room_id": "tiny", "area_m2": 6.0}]
    assert result["room_geometry"]["elongated_rooms"] == [{"room_id": "corridor_like", "aspect_ratio": 5.0}]
    assert result["room_geometry"]["area_range_m2"] == [6.0, 20.0]


def test_procedural_precheck_rejects_bad_room_type_geometry() -> None:
    args = Namespace(
        mode="procedural_front3d",
        procedural_precheck_enabled=True,
        procedural_precheck_min_placements=1,
        procedural_precheck_min_placement_ratio=0.5,
        procedural_precheck_max_skipped_ratio=0.8,
        procedural_precheck_require_connected_rooms=False,
        procedural_precheck_min_room_area_m2=0.0,
        procedural_precheck_max_room_aspect_ratio=None,
        procedural_precheck_room_type_geometry={
            "LivingRoom": {"min_area_m2": 16.0, "max_area_m2": None, "max_aspect_ratio": 3.5},
            "Bathroom": {"min_area_m2": 4.0, "max_area_m2": 12.0, "max_aspect_ratio": 4.0},
        },
    )

    result = evaluate_procedural_precheck(
        args,
        {"placement_count": 3},
        {
            "skipped_object_count": 0,
            "procedural": {
                "room_count": 2,
                "rooms": [
                    {"room_id": "small_living", "room_type": "LivingRoom", "area_m2": 12.0, "aspect_ratio": 1.5},
                    {"room_id": "huge_bath", "room_type": "Bathroom", "area_m2": 20.0, "aspect_ratio": 4.5},
                ],
                "placement_stats": {"desired_object_counts": {"small_living": 1, "huge_bath": 1}},
            },
        },
    )

    assert result["ok"] is False
    assert {error["code"] for error in result["errors"]} == {
        "room_type_area_too_small",
        "room_type_area_too_large",
        "room_type_aspect_ratio_too_high",
    }
    assert result["room_type_geometry"]["checked_room_count"] == 2


def test_aggregate_procedural_run_report_summarizes_structure() -> None:
    report = aggregate_procedural_run_report(
        [
            {
                "scene_index": 0,
                "scene_dir": "procedural_front3d_0000",
                "placement_count": 3,
                "skipped_object_count": 1,
                "precheck": {"ok": True, "errors": [], "room_type_geometry": {"ok": True}},
                "procedural": {
                    "scene_id": "scene-a",
                    "layout": "rect_union",
                    "configured_layout": "mixed",
                    "room_count": 2,
                    "footprint": {"room_area_m2": 21.0, "bbox_area_m2": 28.0, "fill_ratio": 0.75, "concavity_area_m2": 7.0},
                    "adjacency_count": 1,
                    "window_count": 1,
                    "rooms": [
                        {"room_id": "a", "room_type": "LivingRoom", "area_m2": 12.0, "aspect_ratio": 1.5},
                        {"room_id": "b", "room_type": "Bedroom", "area_m2": 9.0, "aspect_ratio": 1.2},
                    ],
                    "placement_stats": {
                        "desired_object_counts": {"a": 2, "b": 2},
                        "group_stats": {
                            "attempted": {"bed_side_tables": 1},
                            "succeeded": {"bed_side_tables": 1},
                        },
                    },
                },
            },
            {
                "scene_index": 1,
                "scene_dir": "procedural_front3d_0001",
                "placement_count": 2,
                "skipped_object_count": 0,
                "precheck": {
                    "ok": False,
                    "errors": [{"code": "room_type_area_too_small"}],
                    "room_type_geometry": {
                        "ok": False,
                        "too_small": [{"room_id": "c"}],
                        "too_large": [],
                        "too_elongated": [],
                    },
                },
                "procedural": {
                    "scene_id": "scene-b",
                    "layout": "split_tree",
                    "configured_layout": "mixed",
                    "room_count": 1,
                    "footprint": {"room_area_m2": 16.0, "bbox_area_m2": 16.0, "fill_ratio": 1.0, "concavity_area_m2": 0.0},
                    "adjacency_count": 0,
                    "window_count": 2,
                    "rooms": [
                        {"room_id": "c", "room_type": "LivingRoom", "area_m2": 16.0, "aspect_ratio": 1.0},
                    ],
                    "placement_stats": {"desired_object_counts": {"c": 2}},
                },
            },
        ]
    )

    assert report["schema_version"] == "scenegen.procedural.run_report.v1"
    assert report["scene_count"] == 2
    assert report["layout_counts_total"] == {"rect_union": 1, "split_tree": 1}
    assert report["configured_layout_counts_total"] == {"mixed": 2}
    assert report["room_count"] == {"min": 1.0, "max": 2.0, "mean": 1.5}
    assert report["room_type_counts_total"] == {"Bedroom": 1, "LivingRoom": 2}
    assert report["room_area_m2"] == {"min": 9.0, "max": 16.0, "mean": 12.333333}
    assert report["footprint_fill_ratio"] == {"min": 0.75, "max": 1.0, "mean": 0.875}
    assert report["footprint_concavity_area_m2"] == {"min": 0.0, "max": 7.0, "mean": 3.5}
    assert report["placement_ratio"] == {"min": 0.75, "max": 1.0, "mean": 0.875}
    assert report["placement_group_success_total"] == {"bed_side_tables": 1}
    assert report["precheck_ok_count"] == 1
    assert report["precheck_failed_count"] == 1
    assert report["precheck_ok_rate"] == 0.5
    assert report["precheck_error_counts"] == {"room_type_area_too_small": 1}
    assert report["room_type_geometry_failed_count"] == 1
    assert report["room_type_geometry_issue_counts"] == {"too_small": 1}
    assert report["scenes"][0]["room_type_counts"] == {"Bedroom": 1, "LivingRoom": 1}
    assert report["scenes"][0]["layout"] == "rect_union"
    assert report["scenes"][0]["configured_layout"] == "mixed"
    assert report["scenes"][0]["footprint"]["fill_ratio"] == 0.75
    assert report["scenes"][0]["room_type_geometry_ok"] is True
    assert report["scenes"][1]["room_type_geometry_ok"] is False


def test_label_plane_grid_respects_floor_domain_and_ignores_obstacles() -> None:
    config = make_label_config(
        strategy="panel",
        grid_m=1.0,
        ue__sampling__wall_clearance_m=0.5,
        ue__walk__furniture_clearance_m=0.0,
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
    config = make_label_config(
        strategy="walk",
        grid_m=1.0,
        ue__height_m=1.8,
        ue__sampling__wall_clearance_m=0.0,
        ue__walk__furniture_clearance_m=0.0,
        ue__walk__obstacle_strategy="height_aware",
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


def test_label_below_ue_column_obstacles_block_objects_below_ue() -> None:
    config = make_label_config(
        strategy="walk",
        grid_m=1.0,
        ue__height_m=1.8,
        ue__sampling__wall_clearance_m=0.0,
        ue__walk__furniture_clearance_m=0.0,
        ue__walk__obstacle_strategy="below_ue_column",
        ue__walk__ignore_low_obstacles_below_m=0.1,
    )
    tri_a = SupportTriangle(vertices=((0.0, 0.0, 0.0), (4.0, 0.0, 0.0), (0.0, 4.0, 0.0)), area=8.0, z=0.0)
    tri_b = SupportTriangle(vertices=((4.0, 0.0, 0.0), (4.0, 4.0, 0.0), (0.0, 4.0, 0.0)), area=8.0, z=0.0)
    context = RoomLabelContext(
        room_index=0,
        room_id="room",
        room_type="test",
        floor_source="test",
        floor_triangles=(tri_a, tri_b),
        floor_z=0.0,
        ceiling_z=3.0,
        bounds_xy=(0.0, 0.0, 4.0, 4.0),
        obstacles=(
            LabelObstacle(0.9, 0.9, 1.1, 1.1, 0.0, 1.5, "table_below_ue"),
            LabelObstacle(1.9, 1.9, 2.1, 2.1, 1.9, 2.2, "shelf_above_ue"),
            LabelObstacle(2.9, 2.9, 3.1, 3.1, 0.0, 0.05, "thin_low_object"),
        ),
    )

    points = generate_ue_points_for_room(context, config)

    assert (1.0, 1.0) not in points
    assert (2.0, 2.0) in points
    assert (3.0, 3.0) in points
    assert points.stats["walk_obstacle_mode"] == "below_ue_column"
    assert points.stats["walk_blocking_obstacle_count"] == 2
    assert points.stats["ignored_low_obstacle_count"] == 1


def test_label_footprint_column_obstacles_block_points_above_low_objects() -> None:
    config = make_label_config(
        strategy="walk",
        grid_m=1.0,
        ue__height_m=1.8,
        ue__sampling__wall_clearance_m=0.0,
        ue__walk__furniture_clearance_m=0.0,
        ue__walk__obstacle_strategy="footprint_column",
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
    config = make_label_config(
        bs__count__strategy="area_adaptive",
        bs__count__min_room_area_m2=4.0,
        bs__count__area_per_point_m2=10.0,
        bs__count__min_per_room=1,
        bs__count__max_per_room=8,
    )

    assert bs_count_for_room(make_square_room_context(1.5), config) == 0
    assert bs_count_for_room(make_square_room_context(3.0), config) == 1
    assert bs_count_for_room(make_square_room_context(8.0), config) == 7
    assert bs_count_for_room(make_square_room_context(12.0), config) == 8


def test_label_area_adaptive_bs_generation_uses_computed_count() -> None:
    config = make_label_config(
        grid_m=1.0,
        bs__count__strategy="area_adaptive",
        bs__count__min_room_area_m2=4.0,
        bs__count__area_per_point_m2=10.0,
        bs__count__min_per_room=1,
        bs__count__max_per_room=8,
    )
    context = make_square_room_context(8.0)
    free_points = [(float(x), float(y)) for x in range(1, 8) for y in range(1, 8)]

    bs_points = generate_bs_points_for_room(context, free_points, config)

    assert len(bs_points) == 7


def test_global_floor_assignment_keeps_corridor_points() -> None:
    config = make_label_config()
    room_context = make_square_room_context(1.0)
    global_context = make_square_room_context(3.0)
    corridor_context = corridor_context_from_global(global_context, config)

    grouped = assign_global_free_points([(0.5, 0.5), (2.0, 2.0)], [room_context], corridor_context)
    by_room = {context.room_id: points for context, points in grouped}

    assert by_room["room"] == [(0.5, 0.5)]
    assert by_room["__corridor__"] == [(2.0, 2.0)]


def test_label_variants_combine_strategy_and_resolution() -> None:
    payload = deepcopy(DEFAULT_CONFIG["label"])
    payload["ue"]["sampling"]["strategies"] = ["panel", "walk"]
    payload["ue"]["sampling"]["grid_m"] = [0.1, 0.2]
    config = LabelConfig.from_mapping(payload, DEFAULT_CONFIG["front3d"]["openings"])

    variants = label_variants(config)

    assert [variant.name for variant in variants] == [
        "label_panel_0p1",
        "label_panel_0p2",
        "label_walk_0p1",
        "label_walk_0p2",
    ]


def test_geometry_center_bs_selects_near_center_free_point() -> None:
    config = make_label_config(
        bs__center__enabled=True,
        bs__wall_clearance_m=0.2,
        bs__center__initial_radius_m=0.2,
        bs__center__radius_step_m=0.1,
        bs__center__max_radius_m=1.0,
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
    config_path.write_text("pipeline:\n  run_name: sparse\nplacement:\n  tables: [2, 4]\n", encoding="utf-8")
    effective, _overrides = load_effective_config(config_path, root, parse_args([]))

    assert effective["pipeline"]["run_name"] == "sparse"
    assert effective["assets"]["catalog"].endswith("data/catalogs/bistro.v1.json")
    assert "manifest" not in effective["assets"]
    assert effective["bistro"]["forbidden_xy"] == [[1.0, 11.0, 4.5, 16.0], [8.0, 8.0, 14.0, 10.0]]
    assert effective["floorplan"]["geometry"]["height"]["values_m"] == [1.6]
    assert effective["label"]["ue"]["sampling"]["mask_resolution_m"] == 0.05
    assert effective["floorplan"]["sampling"]["max_points"] == 4_000_000


def test_legacy_assets_manifest_config_is_rejected(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "legacy_config.yaml"
    config_path.write_text(
        "assets:\n  manifest: data/assets/manifest.json\npipeline:\n  scenes: 1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="assets.manifest"):
        load_effective_config(config_path, root, parse_args([]))


def test_cli_set_overrides_yaml_and_parses_types(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("pipeline:\n  mode: bistro\n  scenes: 2\nlabel:\n  ue:\n    height_m: 1.6\n", encoding="utf-8")
    args = parse_args(
        [
            "--config",
            str(config_path),
            "--set",
            "pipeline.mode=front3d",
            "--set",
            "pipeline.scenes=5",
            "--set",
            "label.ue.height_m=1.8",
            "--set",
            "label.ue.sampling.grid_m=[0.1,0.2,0.5]",
            "--set",
            "label.ue.sampling.mask_resolution_m=0.1",
            "--set",
            "floorplan.enabled=false",
            "--set",
            "postprocess.maps.enabled=true",
            "--set",
            "postprocess.maps.workers=2",
            "--set",
            "postprocess.maps.bs_label.mode=name",
            "--set",
            "postprocess.maps.bs_label.name=label_panel_0p1",
        ]
    )

    effective, overrides = load_effective_config(config_path, root, args)

    assert effective["pipeline"]["mode"] == "front3d"
    assert effective["pipeline"]["scenes"] == 5
    assert effective["label"]["ue"]["height_m"] == 1.8
    assert effective["label"]["ue"]["sampling"]["grid_m"] == [0.1, 0.2, 0.5]
    assert effective["label"]["ue"]["sampling"]["mask_resolution_m"] == 0.1
    assert effective["floorplan"]["enabled"] is False
    assert effective["postprocess"]["maps"]["enabled"] is True
    assert effective["postprocess"]["maps"]["workers"] == 2
    assert effective["postprocess"]["maps"]["bs_label"]["mode"] == "name"
    assert effective["postprocess"]["maps"]["bs_label"]["name"] == "label_panel_0p1"
    assert overrides["pipeline"]["mode"] == "front3d"


def test_postprocess_defaults_are_disabled() -> None:
    root = find_project_root()
    effective, _overrides = load_effective_config(root / "config" / "front3d.yaml", root, parse_args([]))

    assert effective["postprocess"]["maps"]["enabled"] is False
    assert effective["postprocess"]["dataset"]["enabled"] is False
    assert effective["postprocess"]["maps"]["bs_label"]["mode"] == "first"


def test_postprocess_named_bs_label_requires_name(tmp_path: Path) -> None:
    root = find_project_root()
    config_path = tmp_path / "bad_config.yaml"
    config_path.write_text(
        "postprocess:\n"
        "  maps:\n"
        "    enabled: true\n"
        "    bs_label:\n"
        "      mode: name\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="postprocess.maps.bs_label.name"):
        load_effective_config(config_path, root, parse_args([]))


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ("label.wall_clearance_m=0.2", "label.wall_clearance_m"),
        ("label.batch_strategies=[free_space_grid]", "label.batch_strategies"),
        ("front3d.source_scene_dir=data/3D-Front/3D-FRONT", "front3d.source_scene_dir"),
        ("floorplan.sample_density_scale=128", "floorplan.sample_density_scale"),
        ("postprocess.maps.los_stride_pixels=4", "postprocess.maps.los_stride_pixels"),
    ],
)
def test_cli_set_unknown_field_is_rejected(override: str, field: str) -> None:
    root = find_project_root()
    args = parse_args(["--set", override])

    with pytest.raises(ValueError, match=field):
        load_effective_config(root / "config" / "bistro.yaml", root, args)


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
                    "source_dir": str(source_scene_dir),
                    "arch_variant": "normalized",
                    "scene_ids": [scene_id],
                    "select": "random",
                    "use_replace_jid": True,
                    "skip_missing_objects": True,
                },
                "floorplan": {
                    "class_mask": {
                        "enabled": True,
                    },
                    "sampling": {
                        "density_scale": 0.01,
                        "min_points": 1000,
                        "max_points": 2000,
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path


def make_procedural_runtime_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "procedural"
    source_scene_dir = root / "3D-FRONT"
    source_scene_dir.mkdir(parents=True)
    model_id = "procedural-seat-model"
    object_dir = root / "scenegen_objects_raw" / model_id
    object_obj = object_dir / f"{model_id}.obj"
    object_json = object_dir / f"{model_id}.json"
    write_front3d_fixture_obj(object_obj, material="wood", height_axis_y=True)
    object_json.write_text(
        json.dumps(
            {
                "id": model_id,
                "name": "procedural test seat",
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
                "by_scene_id": {},
                "by_model_id": {model_id: {"raw": {"json": str(object_json), "obj": str(object_obj), "preview": ""}}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "procedural_front3d.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "pipeline": {
                    "mode": "procedural_front3d",
                    "scenes": 1,
                    "seed": 123,
                    "output_dir": str(tmp_path / "out"),
                    "run_name": "smoke_procedural_batch",
                },
                "front3d": {
                    "manifest": str(manifest_path),
                    "source_dir": str(source_scene_dir),
                    "arch_variant": "raw",
                    "object_variant": "raw",
                },
                "procedural": {
                    "room_count": [1, 1],
                    "room_width_m": [3.0, 3.0],
                    "room_length_m": [3.0, 3.0],
                    "room_height_m": [2.8, 2.8],
                    "room_types": ["LivingRoom"],
                    "required_room_types": None,
                    "windows": {
                        "enabled": True,
                        "room_probability": 1.0,
                        "max_per_room": 1,
                        "width_m": [1.0, 1.0],
                        "height_m": [0.8, 0.8],
                        "sill_height_m": [1.0, 1.0],
                        "wall_margin_m": 0.5,
                    },
                    "object_count": {"strategy": "range", "range": [1, 1]},
                    "asset_pool_limit": 5,
                    "precheck": {"room_type_geometry": None},
                },
                "label": {"enabled": False},
                "floorplan": {"enabled": False},
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
    for filename in ("floorplan_1p60.png", "preview.png", "side_view.png", "meta.json", "stack.npz"):
        assert (scene_dir / "floorplan" / filename).is_file()
    assert not (scene_dir / "floorplan" / "geometry_raw.png").exists()
    floorplan_meta = json.loads((scene_dir / "floorplan" / "meta.json").read_text(encoding="utf-8"))
    assert floorplan_meta["sampling"]["sampler"] == "numpy_area_weighted"
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
    assert (output_dir / "smoke_front3d" / "summary" / "obj" / "front3d_0000.obj").is_file()
    assert (output_dir / "smoke_front3d" / "summary" / "floorplan" / "front3d_0000_floorplan_1p60.png").is_file()
    assert (output_dir / "smoke_front3d" / "logs" / "events.jsonl").is_file()
    assert (output_dir / "smoke_front3d" / "logs" / "timings.jsonl").is_file()
    assert (output_dir / "smoke_front3d" / "logs" / "state" / "run_state.json").is_file()

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
    assert any(point["label"] == "BS_CENTER" for point in label["groups"][0]["bs_points"])
    assert len(label["groups"][0]["bs_points"]) <= 5
    assert (scene_dir / "label" / "label_walk_0p1.json").is_file()
    report = json.loads((scene_dir / "label" / "report" / "label_walk_0p1_report.json").read_text(encoding="utf-8"))
    assert report["bs_count"] == len(label["bs_points"])
    assert report["ue_count"] == len(label["ue_points"])
    assert report["group_count"] == len(label["groups"])
    room_sampling = report["rooms"][0]["ue_sampling"]
    assert room_sampling["sampling_source"] == "front3d_global_rect_subtractive_mask"
    assert room_sampling["sampling_pipeline_version"] == "2.1.0"
    assert room_sampling["mask_resolution_m"] == 0.05
    assert "timings_s" in report
    assert "center_bs" in report["timings_s"]
    assert "timings_s" in room_sampling
    assert "sample_grid" in room_sampling["timings_s"]
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
    assert manifest["summary"]["floorplan"]["count"] == 1
    assert manifest["summary"]["label_floorplan"]["count"] == 1
    assert manifest["summary"]["label_floorplan"]["groups"]["0p1"]["count"] == 1
    assert (
        output_dir / "smoke_front3d" / "summary" / "label_floorplan" / "0p1" / "front3d_0000_label_walk_0p1.png"
    ).is_file()
    assert manifest["logs"]["events"] == "logs/events.jsonl"
    assert "timing_summary_s" in manifest
    assert "floorplan" in manifest["timing_summary_s"]
    assert "timings_s" in manifest["scenes"][0]
    assert "floorplan_geometry" in manifest["scenes"][0]["timings_s"]
    quality = json.loads((scene_dir / "quality_report.json").read_text(encoding="utf-8"))
    assert quality["ok"] is True

    json_files = [
        output_dir / "smoke_front3d" / "manifest.json",
        output_dir / "smoke_front3d" / "manifest_front3d.json",
        output_dir / "smoke_front3d" / "statistics.json",
        output_dir / "smoke_front3d" / "summary" / "obj" / "copy_manifest.json",
        output_dir / "smoke_front3d" / "summary" / "floorplan" / "copy_manifest.json",
        output_dir / "smoke_front3d" / "summary" / "label_floorplan" / "copy_manifest.json",
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


def test_front3d_batch_scheduler_defaults_to_hybrid(tmp_path: Path) -> None:
    args = parse_batch_args(["--config", str(tmp_path / "front3d.yaml")])

    assert args.scheduler == "hybrid"


@pytest.mark.parametrize("scheduler", ["static", "dynamic", "hybrid"])
def test_front3d_batch_runner_writes_plan_state_and_worker_logs(tmp_path: Path, scheduler: str) -> None:
    pytest.importorskip("trimesh")
    config_path = make_front3d_runtime_fixture(tmp_path)
    output_dir = tmp_path / "out"

    exit_code = batch_main(
        [
            "--config",
            str(config_path),
            "--workers",
            "2",
            "--scheduler",
            scheduler,
            "--max-retries",
            "0",
            "--set",
            "pipeline.scenes=2",
            "--set",
            f"pipeline.run_name=batch_smoke_{scheduler}",
        ]
    )

    run_dir = output_dir / f"batch_smoke_{scheduler}"
    assert exit_code == 0
    assert (run_dir / "front3d_0000" / "scene.obj").is_file()
    assert (run_dir / "front3d_0001" / "scene.obj").is_file()
    assert (run_dir / "manifest_batch.json").is_file()
    assert (run_dir / "manifest_front3d.json").is_file()
    assert (run_dir / "summary" / "obj" / "front3d_0000.obj").is_file()
    assert (run_dir / "summary" / "obj" / "front3d_0001.obj").is_file()
    assert (run_dir / "summary" / "label_floorplan" / "0p1" / "front3d_0000_label_walk_0p1.png").is_file()
    assert (run_dir / "summary" / "label_floorplan" / "0p1" / "front3d_0001_label_walk_0p1.png").is_file()
    plan_lines = (run_dir / "batch" / "scene_plan.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(plan_lines) == 2
    state = json.loads((run_dir / "batch" / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "completed"
    assert state["succeeded"] == 2
    assert all(task["status"] == "succeeded" for task in state["tasks"].values())
    assert (run_dir / "batch" / "logs" / "events.jsonl").is_file()
    assert (run_dir / "batch" / "logs" / "timings.jsonl").is_file()
    assert (run_dir / "batch" / "logs" / "workers" / "worker_000.log").is_file()
    assert (run_dir / "batch" / "logs" / "workers" / "worker_001.log").is_file()
    assert not list((run_dir / "batch" / "worker_runs").glob("**/summary"))
    manifest = json.loads((run_dir / "manifest_batch.json").read_text(encoding="utf-8"))
    assert manifest["batch"] is True
    assert manifest["workers"] == 2
    assert manifest["scheduler"] == scheduler
    assert manifest["succeeded_scenes"] == 2
    assert manifest["failed_scenes"] == 0
    assert all("batch_publish_s" in scene for scene in manifest["scenes"])
    assert all("batch_child_run_timings_s" in scene for scene in manifest["scenes"])
    assert all("batch_subprocess_overhead_s" in scene for scene in manifest["scenes"])
    assert any(json.loads(line)["stage"] == "publish_scene" for line in (run_dir / "batch" / "logs" / "timings.jsonl").read_text().splitlines())


def test_procedural_front3d_batch_runner_uses_procedural_scene_prefix(tmp_path: Path) -> None:
    config_path = make_procedural_runtime_fixture(tmp_path)
    output_dir = tmp_path / "out"

    exit_code = batch_main(
        [
            "--config",
            str(config_path),
            "--workers",
            "2",
            "--scheduler",
            "hybrid",
            "--max-retries",
            "0",
            "--set",
            "pipeline.scenes=2",
        ]
    )

    run_dir = output_dir / "smoke_procedural_batch"
    assert exit_code == 0
    assert (run_dir / "procedural_front3d_0000" / "scene.obj").is_file()
    assert (run_dir / "procedural_front3d_0001" / "scene.obj").is_file()
    assert (run_dir / "manifest_batch.json").is_file()
    assert (run_dir / "manifest_procedural_front3d.json").is_file()
    assert (run_dir / "procedural_report.json").is_file()
    assert not (run_dir / "manifest_front3d.json").exists()
    assert (run_dir / "summary" / "obj" / "procedural_front3d_0000.obj").is_file()
    plan = [json.loads(line) for line in (run_dir / "batch" / "scene_plan.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [task["scene_key"] for task in plan] == ["procedural_front3d_0000", "procedural_front3d_0001"]
    assert {task["mode"] for task in plan} == {"procedural_front3d"}
    state = json.loads((run_dir / "batch" / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "completed"
    assert state["succeeded"] == 2
    manifest = json.loads((run_dir / "manifest_batch.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "procedural_front3d"
    assert manifest["succeeded_scenes"] == 2
    assert manifest["procedural_report_file"] == "procedural_report.json"
    assert manifest["procedural_report"]["scene_count"] == 2
    assert manifest["procedural_report"]["room_count"]["min"] >= 1
    report = json.loads((run_dir / "procedural_report.json").read_text(encoding="utf-8"))
    assert report["scene_count"] == 2
    assert report["room_type_counts_total"]
    assert all("procedural" in scene for scene in manifest["scenes"])
    assert all(scene["procedural"]["window_count"] >= 1 for scene in manifest["scenes"])
    assert all("itu-glass" in scene["sionna_materials"] for scene in manifest["scenes"])
    assert all(scene["scene_dir"].startswith("procedural_front3d_") for scene in manifest["scenes"])


def test_front3d_batch_label_outputs(tmp_path: Path) -> None:
    pytest.importorskip("trimesh")
    config_path = make_front3d_runtime_fixture(tmp_path)
    output_dir = tmp_path / "out"

    exit_code = main(
        [
            "--config",
            str(config_path),
            "--set",
            "label.ue.sampling.strategies=[panel,walk]",
            "--set",
            "label.ue.sampling.grid_m=[0.1,0.2]",
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
    assert walk_sampling["walk_obstacle_mode"] == "below_ue_column"
    assert walk_sampling["walk_blocking_obstacle_count"] == 1
    assert walk_sampling["obstacle_rejected_count"] >= 0

    manifest = json.loads((output_dir / "smoke_front3d" / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["label_variants"]) == expected_names
    assert manifest["label_variant_count"] == 4
    scene_record = manifest["scenes"][0]
    assert scene_record["label"]["variant_count"] == 4
    assert scene_record["label"]["primary"] == "label_panel_0p1"
    assert "substage_timings_s" in scene_record["label"]
    assert "center_bs" in scene_record["label"]["substage_timings_s"]
    assert all("timings_s" in variant for variant in scene_record["label"]["variants"])
    assert scene_record["label"]["report_dir"] == "front3d_0000/label/report"
    assert len(scene_record["label"]["overlays"]) == 4


def test_front3d_ray_height_filtered_floorplan_smoke(tmp_path: Path) -> None:
    pytest.importorskip("trimesh")
    config_path = make_front3d_runtime_fixture(tmp_path)
    output_dir = tmp_path / "out"

    exit_code = main(
        [
            "--config",
            str(config_path),
            "--set",
            "label.enabled=false",
            "--set",
            "floorplan.geometry.projection=ray_height_filtered",
        ]
    )

    scene_dir = output_dir / "smoke_front3d" / "front3d_0000"
    assert exit_code == 0
    assert (scene_dir / "floorplan" / "floorplan_1p60.png").is_file()
    assert (scene_dir / "floorplan" / "stack.npz").is_file()
    meta = json.loads((scene_dir / "floorplan" / "meta.json").read_text(encoding="utf-8"))
    assert meta["projection_mode"] == "ray_height_filtered"
    assert meta["deterministic"] is True
    assert "timings_s" in meta
    assert "build_projection" in meta["timings_s"]
    assert meta["projection_stats"][0]["occupied_pixels"] > 0
    manifest = json.loads((output_dir / "smoke_front3d" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["floorplan_geometry_projection"] == "ray_height_filtered"


def test_front3d_global_sampling_keeps_connected_area_in_each_label(tmp_path: Path) -> None:
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
            "--set",
            "label.ue.sampling.strategies=[panel,walk]",
            "--set",
            "label.ue.sampling.grid_m=[0.1]",
            "--set",
            "label.bs.center.enabled=true",
        ]
    )

    scene_dir = output_dir / "smoke_front3d" / "front3d_0000"
    assert exit_code == 0
    expected_names = {
        "label_panel_0p1",
        "label_walk_0p1",
    }
    assert {path.stem for path in (scene_dir / "label").glob("label_*.json")} == expected_names
    assert {path.stem for path in (scene_dir / "label_floorplan").glob("label_*.png")} == expected_names
    panel_label = json.loads((scene_dir / "label" / "label_panel_0p1.json").read_text(encoding="utf-8"))
    walk_label = json.loads((scene_dir / "label" / "label_walk_0p1.json").read_text(encoding="utf-8"))
    for label in (panel_label, walk_label):
        corridor_groups = [group for group in label["groups"] if group["room_id"] == "__corridor__"]
        assert corridor_groups
        assert len(corridor_groups[0]["ue_points"]) > 0
        assert any(point["label"] == "BS_CENTER" for point in label["bs_points"])

    panel_report = json.loads((scene_dir / "label" / "report" / "label_panel_0p1_report.json").read_text(encoding="utf-8"))
    walk_report = json.loads((scene_dir / "label" / "report" / "label_walk_0p1_report.json").read_text(encoding="utf-8"))

    assert panel_report["rooms"][0]["ue_sampling"]["sampling_source"] == "front3d_global_rect_subtractive_mask"
    assert panel_report["rooms"][0]["ue_sampling"]["panel_obstacle_mode"] == "none"
    assert panel_report["rooms"][0]["ue_sampling"]["panel_furniture_filter_enabled"] is False
    assert walk_report["rooms"][0]["ue_sampling"]["sampling_source"] == "front3d_global_rect_subtractive_mask"

    manifest = json.loads((output_dir / "smoke_front3d" / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["label_variants"]) == expected_names


def test_generated_scene_outputs_and_sionna_load(tmp_path: Path) -> None:
    pytest.importorskip("sionna.rt")
    pytest.importorskip("trimesh")

    output_dir = tmp_path / "out"
    exit_code = main(
        [
            "--set",
            "pipeline.mode=generated",
            "--set",
            "pipeline.scenes=1",
            "--set",
            "pipeline.run_name=smoke_generated",
            "--set",
            f"pipeline.output_dir={output_dir}",
            "--set",
            "pipeline.seed=123",
            "--set",
            "placement.tables=[1,1]",
            "--set",
            "placement.floor_extras=1",
            "--set",
            "placement.max_attempts=20",
            "--set",
            "floorplan.sampling.density_scale=0.01",
            "--set",
            "floorplan.sampling.min_points=1000",
            "--set",
            "floorplan.sampling.max_points=2000",
            "--set",
            "validation.sionna=true",
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
    for filename in ("geometry_raw.png", "semantic.png", "semantic.json", "geometry_clean.png", "geometry_clean_preview.png"):
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
    assert manifest["floorplan_height_mode"] == "heights"
    assert manifest["floorplan_heights_m"] == [1.6]
    assert manifest["summary"]["floorplan"]["count"] == 1
    assert manifest["summary"]["label_floorplan"]["count"] == 1
    assert (output_dir / "smoke_generated" / "summary" / "floorplan" / "scene_0000_floorplan_1p60.png").is_file()
    assert (
        output_dir / "smoke_generated" / "summary" / "label_floorplan" / "0p1" / "scene_0000_label_walk_0p1.png"
    ).is_file()
    geometry_meta = json.loads((scene_dir / "floorplan" / "meta.json").read_text(encoding="utf-8"))
    assert geometry_meta["height_mode"] == "heights"
    assert geometry_meta["z_levels_m"] == [1.6]
    assert geometry_meta["num_levels"] == 1
    assert geometry_meta["primary_layer"].endswith("floorplan/floorplan_1p60.png")
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
    assert effective_config["floorplan"]["sampling"]["min_points"] == 1000
    assert effective_config["floorplan"]["geometry"]["height"]["mode"] == "heights"
    assert effective_config["floorplan"]["geometry"]["height"]["values_m"] == [1.6]
    assert effective_config["quality"]["enabled"] is True
    assert effective_config["label"]["enabled"] is True
    assert effective_config["label"]["ue"]["sampling"]["strategies"] == ["walk"]
    assert effective_config["label"]["ue"]["walk"]["obstacle_strategy"] == "below_ue_column"

    json_files = [
        output_dir / "smoke_generated" / "manifest.json",
        output_dir / "smoke_generated" / "manifest_generated.json",
        output_dir / "smoke_generated" / "statistics.json",
        output_dir / "smoke_generated" / "summary" / "obj" / "copy_manifest.json",
        output_dir / "smoke_generated" / "summary" / "floorplan" / "copy_manifest.json",
        output_dir / "smoke_generated" / "summary" / "label_floorplan" / "copy_manifest.json",
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
