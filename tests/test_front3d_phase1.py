from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

from scenegen.geometry import load_obj_mesh


def write_obj(path: Path, scale: float = 1.0) -> None:
    path.write_text(
        "\n".join(
            [
                "o sample",
                "v 0 0 0",
                f"v {scale} 0 0",
                f"v 0 {scale} 0",
                "f 1 2 3",
                "",
            ]
        ),
        encoding="utf-8",
    )


def make_front3d_fixture(root: Path) -> Path:
    source = root / "3D-Front"
    model_id = "11111111-1111-4111-8111-111111111111"
    scene_id = "22222222-2222-4222-8222-222222222222"
    model_dir = source / "3D-FUTURE-model" / model_id
    model_dir.mkdir(parents=True)
    write_obj(model_dir / "raw_model.obj", 1.0)
    write_obj(model_dir / "normalized_model.obj", 2.0)
    (model_dir / "model.mtl").write_text("newmtl wood\n", encoding="utf-8")
    Image.new("RGB", (8, 8), (120, 80, 40)).save(model_dir / "texture.png")
    Image.new("RGB", (8, 8), (80, 120, 40)).save(model_dir / "image.jpg")
    (source / "3D-FUTURE-model" / "model_info.json").write_text(
        json.dumps(
            [
                {
                    "model_id": model_id,
                    "super-category": "Table",
                    "category": "Dining Table",
                    "style": "Modern",
                    "theme": "Smooth Net",
                    "material": "Wood",
                }
            ]
        ),
        encoding="utf-8",
    )
    texture_dir = source / "3D-FRONT-texture"
    texture_dir.mkdir(parents=True)
    (texture_dir / "texture_info.json").write_text("[]", encoding="utf-8")
    scene_dir = source / "3D-FRONT"
    scene_dir.mkdir(parents=True)
    (scene_dir / f"{scene_id}.json").write_text(
        json.dumps(
            {
                "uid": scene_id,
                "furniture": [{"uid": "1/model", "jid": model_id, "valid": True, "bbox": [1, 1, 1]}],
                "material": [],
                "mesh": [
                    {
                        "uid": "floor/0",
                        "type": "Floor",
                        "material": "floor_mat",
                        "xyz": [0, 0, 0, 1, 0, 0, 0, 0, 1],
                        "faces": [0, 1, 2],
                    }
                ],
                "scene": {
                    "room": [
                        {
                            "type": "DiningRoom",
                            "instanceid": "DiningRoom-1",
                            "children": [
                                {
                                    "ref": "1/model",
                                    "pos": [0, 0, 0],
                                    "rot": [0, 0, 0, 1],
                                    "scale": [1, 1, 1],
                                    "instanceid": "furniture/1",
                                }
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    return source


def test_prepare_front3d_phase1_smoke(tmp_path: Path) -> None:
    source = make_front3d_fixture(tmp_path)
    output = tmp_path / "prepared"

    result = subprocess.run(
        [
            sys.executable,
            "tools/prepare_front3d_phase1.py",
            "--source",
            str(source),
            "--output",
            str(output),
            "--limit-objects",
            "1",
            "--limit-scenes",
            "1",
            "--skip-disk-check",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "objects_raw=1" in result.stdout
    for directory in (
        "scenegen_objects_raw",
        "scenegen_objects_normalized",
        "scenegen_architecture_raw",
        "scenegen_architecture_normalized",
    ):
        assert (output / directory / "manifest.json").is_file()

    total_manifest = json.loads((output / "scenegen_manifest.json").read_text(encoding="utf-8"))
    model_id = "11111111-1111-4111-8111-111111111111"
    scene_id = "22222222-2222-4222-8222-222222222222"
    assert total_manifest["by_model_id"][model_id]["raw"]["obj"]
    assert total_manifest["by_scene_id"][scene_id]["normalized"]["obj"]

    object_payload = json.loads(
        (output / "scenegen_objects_raw" / model_id / f"{model_id}.json").read_text(encoding="utf-8")
    )
    assert object_payload["placement"]["class"] == "table"
    assert object_payload["materials"]["sionna"] == ["itu-wood"]
    assert not Path(object_payload["files"]["obj"]).is_absolute()

    arch_payload = json.loads(
        (output / "scenegen_architecture_normalized" / scene_id / f"{scene_id}.json").read_text(encoding="utf-8")
    )
    assert arch_payload["asset_kind"] == "architecture"
    assert arch_payload["normalization"]["scene_axis_transform"] == "front3d_y_up_to_scenegen_z_up"
    assert (output / "scenegen_architecture_normalized" / scene_id / "preview.png").is_file()
    assert load_obj_mesh(output / "scenegen_architecture_normalized" / scene_id / f"{scene_id}.obj").faces
