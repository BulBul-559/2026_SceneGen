from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import Asset
from .paths import SIONNA_DEFAULT_ASSET_MATERIAL


TABLE_RE = re.compile(r"paris_table", re.I)
SEAT_RE = re.compile(r"(chair|barstool)", re.I)
TABLETOP_RE = re.compile(
    r"(glass|plate|cutlery|coaster|napkin|placemat|liquor|wine|beer|jar|cashregister|beertap)",
    re.I,
)
FLOOR_RE = re.compile(r"(bar_cart|coat_rack|flower_pot|doormat|wickerbasket|wine_cooler|cooler_cloth)", re.I)
SKIP_RE = re.compile(r"(building|ceiling|fan|painting|curtain|wall_light|radiator)", re.I)


def classify_asset(name: str) -> str:
    if SKIP_RE.search(name):
        return "skip"
    if TABLE_RE.search(name):
        return "table"
    if SEAT_RE.search(name):
        return "seat"
    if TABLETOP_RE.search(name):
        return "tabletop"
    if FLOOR_RE.search(name):
        return "floor"
    return "skip"


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


def asset_material_info(item: dict[str, Any]) -> tuple[dict[str, str], tuple[str, ...]]:
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


def resolve_obj_file(item: dict[str, Any], manifest_path: Path) -> Path:
    export_name = str(item["export_name"])
    asset_name = str(item.get("asset_name", export_name))
    manifest_dir = manifest_path.parent
    local_candidates = [
        manifest_dir / export_name / f"{export_name}.obj",
        manifest_dir / asset_name / f"{export_name}.obj",
        manifest_dir / asset_name / f"{asset_name}.obj",
    ]
    for candidate in local_candidates:
        if candidate.is_file():
            return candidate.resolve()

    raw_obj_file = item.get("obj_file")
    if raw_obj_file:
        obj_file = Path(str(raw_obj_file)).expanduser()
        if obj_file.is_file():
            return obj_file.resolve()

    searched = ", ".join(str(path) for path in local_candidates)
    raise FileNotFoundError(
        f"Asset OBJ not found for {export_name}. Looked in local manifest assets first: {searched}"
    )


def load_assets(manifest_path: Path) -> list[Asset]:
    manifest_path = manifest_path.expanduser().resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Asset manifest must contain a list: {manifest_path}")

    assets: list[Asset] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        dims = item["dimensions"]
        width = float(dims["width_x"])
        length = float(dims["length_y"])
        height = float(dims["height_z"])
        name = str(item["asset_name"])
        placement_class = classify_asset(name)
        if placement_class == "tabletop" and (width > 0.75 or length > 0.75 or height > 0.9):
            placement_class = "floor"
        if placement_class not in {"table", "seat", "floor", "tabletop"}:
            continue

        source_to_sionna, sionna_material_names = asset_material_info(item)
        assets.append(
            Asset(
                name=name,
                export_name=str(item["export_name"]),
                obj_file=resolve_obj_file(item, manifest_path),
                width=width,
                length=length,
                height=height,
                placement_class=placement_class,
                source_to_sionna_material=source_to_sionna,
                sionna_material_names=sionna_material_names,
            )
        )
    return assets


def group_assets_by_class(assets: list[Asset]) -> dict[str, list[Asset]]:
    return {
        "table": [asset for asset in assets if asset.placement_class == "table"],
        "seat": [asset for asset in assets if asset.placement_class == "seat"],
        "tabletop": [asset for asset in assets if asset.placement_class == "tabletop"],
        "floor": [asset for asset in assets if asset.placement_class == "floor"],
    }


def validate_asset_pool(assets_by_class: dict[str, list[Asset]]) -> None:
    missing = [name for name in ("table", "seat", "tabletop") if not assets_by_class[name]]
    if missing:
        raise ValueError(f"Missing required asset classes: {', '.join(missing)}")


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
