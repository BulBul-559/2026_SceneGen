from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scenegen.models import Asset

from .legacy import legacy_item_to_spec
from .materials import legacy_materials, spec_materials
from .paths import resolve_obj_file
from .schema import AssetSpec, is_clean_asset_mapping


def load_catalog_items(catalog_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and isinstance(payload.get("assets"), list):
        items = payload["assets"]
    else:
        raise ValueError(f"Asset catalog must be a list or contain an assets list: {catalog_path}")
    if not all(isinstance(item, dict) for item in items):
        raise ValueError(f"Asset catalog entries must be mappings: {catalog_path}")
    return items


def item_to_spec(item: dict[str, Any], catalog_path: Path) -> AssetSpec:
    if is_clean_asset_mapping(item):
        return AssetSpec.from_mapping(item)
    return AssetSpec.from_mapping(legacy_item_to_spec(item, catalog_path))


def load_asset_specs(catalog_path: Path) -> list[AssetSpec]:
    return [item_to_spec(item, catalog_path) for item in load_catalog_items(catalog_path)]


def spec_to_runtime_asset(spec: AssetSpec, catalog_path: Path) -> Asset | None:
    if not spec.placement.enabled or spec.placement.class_name == "skip":
        return None
    source_to_sionna, material_names = spec_materials(spec)
    width, length, height = spec.geometry.size
    return Asset(
        name=spec.name,
        export_name=spec.export_name,
        obj_file=resolve_obj_file(spec, catalog_path),
        width=width,
        length=length,
        height=height,
        placement_class=spec.placement.class_name,
        source_to_sionna_material=source_to_sionna,
        sionna_material_names=material_names,
    )


def legacy_item_to_runtime_asset(item: dict[str, Any], catalog_path: Path) -> Asset | None:
    spec = item_to_spec(item, catalog_path)
    if not spec.placement.enabled or spec.placement.class_name == "skip":
        return None
    source_to_sionna, material_names = legacy_materials(item)
    if not source_to_sionna and not material_names:
        source_to_sionna, material_names = spec_materials(spec)
    width, length, height = spec.geometry.size
    return Asset(
        name=spec.name,
        export_name=spec.export_name,
        obj_file=resolve_obj_file(item, catalog_path),
        width=width,
        length=length,
        height=height,
        placement_class=spec.placement.class_name,
        source_to_sionna_material=source_to_sionna,
        sionna_material_names=material_names,
    )


def load_assets(catalog_path: Path) -> list[Asset]:
    assets: list[Asset] = []
    for item in load_catalog_items(catalog_path):
        asset = spec_to_runtime_asset(item_to_spec(item, catalog_path), catalog_path)
        if asset is not None:
            assets.append(asset)
    return assets


def group_assets_by_class(assets: list[Asset]) -> dict[str, list[Asset]]:
    grouped: dict[str, list[Asset]] = {"table": [], "seat": [], "tabletop": [], "floor": []}
    for asset in assets:
        grouped.setdefault(asset.placement_class, []).append(asset)
    return grouped


def validate_asset_pool(assets_by_class: dict[str, list[Asset]]) -> None:
    missing = [name for name in ("table", "seat", "tabletop", "floor") if not assets_by_class.get(name)]
    if missing:
        raise ValueError(f"Asset catalog is missing required placement classes: {', '.join(missing)}")
