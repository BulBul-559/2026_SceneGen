from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
import yaml

from scenegen.batch import main as batch_main
from scenegen.cli import main
from scenegen.deepmimo import (
    DeepMIMOConfig,
    DeepMIMOIndex,
    load_deepmimo_scene,
    load_deepmimo_vertices,
    material_map_for_config,
    write_deepmimo_scene_files,
)


def _mat_element(data_type: int, payload: bytes) -> bytes:
    pad = b"\0" * ((8 - len(payload) % 8) % 8)
    return struct.pack("<II", data_type, len(payload)) + payload + pad


def _write_mat_matrix(path: Path, name: str, values: np.ndarray, data_type: int, class_id: int) -> None:
    header = bytearray(128)
    text = b"MATLAB 5.0 MAT-file, SceneGen DeepMIMO test fixture"
    header[: len(text)] = text
    header[124:126] = struct.pack("<H", 0x0100)
    header[126:128] = b"IM"
    payload = b"".join(
        [
            _mat_element(6, struct.pack("<II", class_id, 0)),
            _mat_element(5, struct.pack("<ii", values.shape[0], values.shape[1])),
            _mat_element(1, name.encode("ascii")),
            _mat_element(data_type, np.asfortranarray(values).tobytes(order="F")),
        ]
    )
    path.write_bytes(bytes(header) + _mat_element(14, payload))


def write_vertices_mat(path: Path, vertices: np.ndarray) -> None:
    _write_mat_matrix(path, "vertices", np.asarray(vertices, dtype=np.float32), data_type=7, class_id=7)


def write_faces_mat(path: Path, faces: np.ndarray) -> None:
    _write_mat_matrix(path, "faces", np.asarray(faces, dtype=np.int64), data_type=12, class_id=14)


def make_deepmimo_fixture(root: Path, scene_id: str = "test_o1") -> Path:
    scene_dir = root / scene_id
    scene_dir.mkdir(parents=True)
    vertices = np.asarray(
        [
            [10.0, 20.0, 0.0],
            [14.0, 20.0, 0.0],
            [14.0, 24.0, 0.0],
            [10.0, 24.0, 0.0],
            [10.0, 20.0, 3.0],
            [14.0, 20.0, 3.0],
            [14.0, 24.0, 3.0],
            [10.0, 24.0, 3.0],
            [8.0, 18.0, 0.0],
            [16.0, 18.0, 0.0],
            [16.0, 26.0, 0.0],
            [8.0, 26.0, 0.0],
        ],
        dtype=np.float32,
    )
    write_vertices_mat(scene_dir / "vertices.mat", vertices)
    objects = [
        {
            "name": "B0",
            "label": "buildings",
            "id": 0,
            "face_vertex_idxs": [
                [0, 1, 2, 3],
                [4, 5, 6, 7],
                [0, 1, 5, 4],
                [1, 2, 6, 5],
                [2, 3, 7, 6],
                [3, 0, 4, 7],
            ],
            "face_material_idxs": [0, 0, 0, 0, 0, 0],
        },
        {
            "name": "newTerrain",
            "label": "terrain",
            "id": 1,
            "face_vertex_idxs": [[8, 9, 10, 11], [8, 9]],
            "face_material_idxs": [0, 0],
        },
    ]
    (scene_dir / "objects.json").write_text(json.dumps(objects), encoding="utf-8")
    params = {
        "version": "test",
        "materials": {"material_0": {"id": 0, "name": "ITU concrete"}},
        "scene": {"n_objects": 2, "n_vertices": int(vertices.shape[0]), "n_faces": 8},
    }
    (scene_dir / "params.json").write_text(json.dumps(params), encoding="utf-8")
    return scene_dir


def make_deepmimo_face_id_fixture(root: Path, scene_id: str = "test_i1") -> Path:
    scene_dir = root / scene_id
    scene_dir.mkdir(parents=True)
    vertices = np.asarray(
        [
            [0.0, 0.0, 0.0],
            [4.0, 0.0, 0.0],
            [4.0, 3.0, 0.0],
            [0.0, 3.0, 0.0],
            [0.0, 0.0, 2.5],
            [4.0, 0.0, 2.5],
            [4.0, 3.0, 2.5],
            [0.0, 3.0, 2.5],
        ],
        dtype=np.float32,
    )
    faces = np.asarray(
        [
            [0, 1, 2],
            [0, 2, 3],
            [4, 5, 6],
            [4, 6, 7],
            [0, 1, 5],
            [0, 5, 4],
        ],
        dtype=np.int64,
    )
    write_vertices_mat(scene_dir / "vertices.mat", vertices)
    write_faces_mat(scene_dir / "faces.mat", faces)
    objects = [
        {"id": 0, "label": "floorplans", "faces": [[0, 1], [2, 3]]},
        {"id": 2, "label": "floorplans", "faces": [[4, 5]]},
    ]
    (scene_dir / "objects.json").write_text(json.dumps(objects), encoding="utf-8")
    params = {
        "version": "test",
        "materials": {"material_0": {"id": 0, "name": "ITU concrete"}},
        "scene": {"n_objects": 2, "n_vertices": int(vertices.shape[0]), "n_faces": int(faces.shape[0])},
    }
    (scene_dir / "params.json").write_text(json.dumps(params), encoding="utf-8")
    return scene_dir


def test_deepmimo_vertices_mat_reader_loads_single_matrix(tmp_path: Path) -> None:
    scene_dir = make_deepmimo_fixture(tmp_path)

    vertices = load_deepmimo_vertices(scene_dir / "vertices.mat")

    assert vertices.shape == (12, 3)
    assert vertices.dtype == np.float32
    assert vertices[0].tolist() == [10.0, 20.0, 0.0]
    assert vertices[-1].tolist() == [8.0, 26.0, 0.0]


def test_deepmimo_scene_writer_outputs_standard_geometry(tmp_path: Path) -> None:
    scene_dir = make_deepmimo_fixture(tmp_path / "root")
    scene = load_deepmimo_scene(scene_dir, normalize_positive_xy=True)
    output_dir = tmp_path / "out" / "deepmimo_0000"
    material_map = material_map_for_config(DeepMIMOConfig(root=tmp_path / "root"))

    build = write_deepmimo_scene_files(output_dir, scene, 0, 123, material_map)

    assert (output_dir / "scene.obj").is_file()
    assert (output_dir / "scene.xml").is_file()
    assert (output_dir / "assets" / "deepmimo" / "buildings__concrete.obj").is_file()
    assert (output_dir / "placements.json").is_file()
    assert build.record["deepmimo_scene_id"] == "test_o1"
    assert build.record["placement_count"] == 0
    assert build.record["sionna_shape_count"] == 2
    assert build.record["deepmimo"]["label_counts"] == {"buildings": 1, "terrain": 1}
    assert build.floorplan_mesh_arrays is not None
    assert build.floorplan_mesh_arrays.vertices.shape == (12, 3)
    assert build.floorplan_mesh_arrays.faces.shape == (14, 3)
    assert float(build.floorplan_mesh_arrays.vertices[:, 0].min()) == 0.0
    assert float(build.floorplan_mesh_arrays.vertices[:, 1].min()) == 0.0


def test_deepmimo_face_id_schema_expands_faces_mat(tmp_path: Path) -> None:
    scene_dir = make_deepmimo_face_id_fixture(tmp_path / "root")
    scene = load_deepmimo_scene(scene_dir, normalize_positive_xy=True)
    output_dir = tmp_path / "out" / "deepmimo_0000"
    material_map = material_map_for_config(DeepMIMOConfig(root=tmp_path / "root"))

    build = write_deepmimo_scene_files(output_dir, scene, 0, 123, material_map)

    assert len(scene.objects) == 2
    assert scene.objects[0].faces == ((0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7))
    assert build.record["deepmimo"]["label_counts"] == {"floorplans": 2}
    assert build.floorplan_mesh_arrays is not None
    assert build.floorplan_mesh_arrays.faces.shape == (6, 3)
    assert (output_dir / "assets" / "deepmimo" / "floorplans__concrete.obj").is_file()


def test_deepmimo_index_discovers_and_orders_scenes(tmp_path: Path) -> None:
    make_deepmimo_fixture(tmp_path, "b_scene")
    make_deepmimo_fixture(tmp_path, "a_scene")

    index = DeepMIMOIndex(DeepMIMOConfig(root=tmp_path))

    assert index.scene_ids == ["a_scene", "b_scene"]
    assert index.load_scene("a_scene").scene_id == "a_scene"


def test_deepmimo_cli_smoke_generates_floorplan_and_manifest(tmp_path: Path) -> None:
    root = tmp_path / "deepmimo"
    make_deepmimo_fixture(root)
    output_dir = tmp_path / "results"
    config_path = tmp_path / "deepmimo.yaml"
    config = {
        "pipeline": {
            "mode": "deepmimo",
            "scenes": 1,
            "seed": 7,
            "output_dir": str(output_dir),
            "run_name": "smoke_deepmimo",
            "clean": False,
            "index_start": 0,
        },
        "deepmimo": {"root": str(root), "select": "sequential"},
        "label": {"enabled": False},
        "quality": {"enabled": True, "fail_on_error": True},
        "floorplan": {
            "enabled": True,
            "resolution_m": 1.0,
            "geometry": {"enabled": True, "projection": "ray_height_filtered", "vertical_axis": "z"},
            "class_mask": {"enabled": False},
        },
        "validation": {"sionna": False},
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    exit_code = main(["--config", str(config_path)])

    run_dir = output_dir / "smoke_deepmimo"
    scene_dir = run_dir / "deepmimo_0000"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert scene_dir.is_dir()
    assert (scene_dir / "scene.obj").is_file()
    assert (scene_dir / "scene.xml").is_file()
    assert (scene_dir / "floorplan" / "floorplan_1p60.png").is_file()
    assert manifest["mode"] == "deepmimo"
    assert manifest["asset_catalog"] is None
    assert manifest["deepmimo_root"] is not None
    assert manifest["scenes"][0]["deepmimo_scene_id"] == "test_o1"


def test_deepmimo_batch_runner_locks_each_task_to_scene_id(tmp_path: Path) -> None:
    root = tmp_path / "deepmimo"
    make_deepmimo_fixture(root, "a_scene")
    make_deepmimo_fixture(root, "b_scene")
    output_dir = tmp_path / "results"
    config_path = tmp_path / "deepmimo_batch.yaml"
    config = {
        "pipeline": {
            "mode": "deepmimo",
            "scenes": 2,
            "seed": 11,
            "output_dir": str(output_dir),
            "run_name": "deepmimo_batch",
            "clean": False,
            "index_start": 0,
        },
        "deepmimo": {"root": str(root), "select": "sequential"},
        "label": {"enabled": False},
        "quality": {"enabled": True, "fail_on_error": True},
        "floorplan": {
            "enabled": True,
            "resolution_m": 1.0,
            "geometry": {"enabled": True, "projection": "ray_height_filtered", "vertical_axis": "z"},
            "class_mask": {"enabled": False},
        },
        "validation": {"sionna": False},
        "batch": {"workers": 1, "scheduler": "hybrid", "max_retries": 0, "task_timeout_s": 60},
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    exit_code = batch_main(["--config", str(config_path)])

    run_dir = output_dir / "deepmimo_batch"
    manifest = json.loads((run_dir / "manifest_batch.json").read_text(encoding="utf-8"))
    plan = [json.loads(line) for line in (run_dir / "batch" / "scene_plan.jsonl").read_text(encoding="utf-8").splitlines()]
    assert exit_code == 0
    assert [task["scene_id"] for task in plan] == ["a_scene", "b_scene"]
    assert [scene["deepmimo_scene_id"] for scene in manifest["scenes"]] == ["a_scene", "b_scene"]
    assert (run_dir / "deepmimo_0000" / "floorplan" / "floorplan_1p60.png").is_file()
    assert (run_dir / "deepmimo_0001" / "floorplan" / "floorplan_1p60.png").is_file()
