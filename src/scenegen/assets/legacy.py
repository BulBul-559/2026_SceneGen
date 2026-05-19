from __future__ import annotations

from pathlib import Path
from typing import Any

from scenegen.paths import find_project_root

from .classify import classify_asset
from .materials import normalize_sionna_material_name
from .paths import portable_repo_path
from .schema import SCHEMA_VERSION


def placement_support(class_name: str) -> list[str]:
    if class_name in {"table", "seat", "floor"}:
        return ["floor"]
    if class_name == "tabletop":
        return ["table", "counter"]
    return []


def local_file_path(
    item: dict[str, Any],
    manifest_path: Path,
    suffix: str,
    repo_root: Path,
) -> str:
    export_name = str(item["export_name"])
    asset_name = str(item.get("asset_name", export_name))
    candidates = [
        manifest_path.parent / export_name / f"{export_name}{suffix}",
        manifest_path.parent / asset_name / f"{export_name}{suffix}",
        manifest_path.parent / asset_name / f"{asset_name}{suffix}",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return portable_repo_path(candidate, repo_root)
    return portable_repo_path(candidates[0], repo_root)


def legacy_item_to_spec(item: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    repo_root = find_project_root(manifest_path)
    name = str(item["asset_name"])
    export_name = str(item["export_name"])
    dims = item["dimensions"]
    width = float(dims["width_x"])
    length = float(dims["length_y"])
    height = float(dims["height_z"])
    class_name = classify_asset(name)
    if class_name == "tabletop" and (width > 0.75 or length > 0.75 or height > 0.9):
        class_name = "floor"

    material_names: list[str] = []
    for raw_name in item.get("sionna_material_names", []) or []:
        material_name = normalize_sionna_material_name(raw_name)
        if material_name not in material_names:
            material_names.append(material_name)

    source_to_sionna: list[dict[str, str]] = []
    for mapping in item.get("sionna_material_mappings", []) or []:
        if not isinstance(mapping, dict):
            continue
        source = str(mapping.get("source_material", "")).strip()
        if not source:
            continue
        sionna = normalize_sionna_material_name(mapping.get("sionna_material_name") or mapping.get("itu_type"))
        if sionna not in material_names:
            material_names.append(sionna)
        source_to_sionna.append(
            {
                "source": source,
                "sionna": sionna,
                "itu_type": str(mapping.get("itu_type") or sionna.removeprefix("itu-")),
                "confidence": str(mapping.get("confidence") or "unknown"),
            }
        )

    if not material_names:
        material_names.append("itu-wood")

    coord = item.get("coordinate_system", {})
    normalization = item.get("normalization", {})
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": "bistro",
        "id": export_name,
        "name": name,
        "export_name": export_name,
        "files": {
            "obj": local_file_path(item, manifest_path, ".obj", repo_root),
            "preview": local_file_path(item, manifest_path, ".png", repo_root),
        },
        "placement": {
            "class": class_name,
            "enabled": class_name != "skip",
            "support": placement_support(class_name),
            "weight": 1.0,
        },
        "geometry": {
            "units": str(item.get("units", "meter")),
            "size": {
                "x": width,
                "y": length,
                "z": height,
            },
            "bbox": item["bounds"],
        },
        "normalization": {
            "coordinate_system": {
                "right": str(coord.get("right_axis", "+X")),
                "forward": str(coord.get("forward_axis", "+Y")),
                "up": str(coord.get("up_axis", "+Z")),
            },
            "grounded": str(normalization.get("grounding_policy", "")).lower().find("z=0") >= 0,
            "xy_origin": "bbox_center",
            "orientation": "principal_horizontal_axis_to_+Y",
            "applied_z_rotation_radians": float(normalization.get("applied_z_rotation_radians", 0.0)),
        },
        "materials": {
            "sionna": material_names,
            "source_to_sionna": source_to_sionna,
        },
    }
