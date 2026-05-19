from __future__ import annotations

from typing import Any

from scenegen.models import Asset
from scenegen.paths import SIONNA_DEFAULT_ASSET_MATERIAL

from .schema import AssetSpec


def normalize_sionna_material_name(value: object, fallback: str = SIONNA_DEFAULT_ASSET_MATERIAL) -> str:
    material = str(value or "").strip()
    if not material:
        return fallback
    if material.startswith("mat-"):
        material = material[4:]
    if material.startswith("itu_"):
        material = "itu-" + material[4:]
    elif not material.startswith("itu-"):
        material = f"itu-{material}"
    return material


def sionna_material_itu_type(material_name: str) -> str:
    material = normalize_sionna_material_name(material_name)
    if material.startswith("itu-"):
        return material[4:]
    if material.startswith("itu_"):
        return material[4:]
    return material


def legacy_materials(item: dict[str, Any]) -> tuple[dict[str, str], tuple[str, ...]]:
    source_to_sionna: dict[str, str] = {}
    ordered_names: list[str] = []
    for mapping in item.get("sionna_material_mappings", []) or []:
        if not isinstance(mapping, dict):
            continue
        source_material = str(mapping.get("source_material", "")).strip()
        material_name = normalize_sionna_material_name(
            mapping.get("sionna_material_name") or mapping.get("itu_type")
        )
        if source_material:
            source_to_sionna[source_material] = material_name
        if material_name not in ordered_names:
            ordered_names.append(material_name)

    for raw_name in item.get("sionna_material_names", []) or []:
        material_name = normalize_sionna_material_name(raw_name)
        if material_name not in ordered_names:
            ordered_names.append(material_name)

    if not ordered_names:
        ordered_names.append(SIONNA_DEFAULT_ASSET_MATERIAL)
    return source_to_sionna, tuple(ordered_names)


def spec_materials(spec: AssetSpec) -> tuple[dict[str, str], tuple[str, ...]]:
    source_to_sionna = {
        mapping.source: normalize_sionna_material_name(mapping.sionna)
        for mapping in spec.materials.source_to_sionna
    }
    ordered_names: list[str] = []
    for raw_name in spec.materials.sionna:
        material_name = normalize_sionna_material_name(raw_name)
        if material_name not in ordered_names:
            ordered_names.append(material_name)
    for material_name in source_to_sionna.values():
        if material_name not in ordered_names:
            ordered_names.append(material_name)
    if not ordered_names:
        ordered_names.append(SIONNA_DEFAULT_ASSET_MATERIAL)
    return source_to_sionna, tuple(ordered_names)


def primary_sionna_material(asset: Asset) -> str:
    if asset.sionna_material_names:
        return asset.sionna_material_names[0]
    return SIONNA_DEFAULT_ASSET_MATERIAL


def resolve_face_sionna_material(asset: Asset, source_material: str | None) -> str:
    if source_material is None:
        return primary_sionna_material(asset)
    if source_material in asset.source_to_sionna_material:
        return asset.source_to_sionna_material[source_material]
    if source_material.startswith("itu-") or source_material.startswith("itu_") or source_material.startswith("mat-itu"):
        return normalize_sionna_material_name(source_material)
    return primary_sionna_material(asset)
