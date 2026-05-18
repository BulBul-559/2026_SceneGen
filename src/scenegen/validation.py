from __future__ import annotations

from pathlib import Path

from .paths import portable_path


def validate_sionna_scene(scene_xml: Path, path_root: Path | None = None) -> dict[str, object]:
    root = path_root or scene_xml.parent
    try:
        from sionna.rt import load_scene

        scene = load_scene(str(scene_xml), merge_shapes=False)
        return {
            "ok": True,
            "scene_xml": portable_path(scene_xml, root),
            "object_count": len(scene.objects),
            "radio_material_count": len(scene.radio_materials),
        }
    except Exception as exc:
        return {
            "ok": False,
            "scene_xml": portable_path(scene_xml, root),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
