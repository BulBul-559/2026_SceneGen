from __future__ import annotations

from .classify import classify_asset
from .legacy import legacy_item_to_spec
from .loaders import group_assets_by_class, load_asset_specs, load_assets, validate_asset_pool
from .materials import (
    normalize_sionna_material_name,
    primary_sionna_material,
    resolve_face_sionna_material,
    sionna_material_itu_type,
)
from .paths import resolve_obj_file
from .schema import (
    AssetFiles,
    AssetGeometry,
    AssetMaterials,
    AssetPlacement,
    AssetSpec,
    MaterialMapping,
    NormalizationSpec,
)

__all__ = [
    "AssetFiles",
    "AssetGeometry",
    "AssetMaterials",
    "AssetPlacement",
    "AssetSpec",
    "MaterialMapping",
    "NormalizationSpec",
    "classify_asset",
    "group_assets_by_class",
    "legacy_item_to_spec",
    "load_asset_specs",
    "load_assets",
    "normalize_sionna_material_name",
    "primary_sionna_material",
    "resolve_face_sionna_material",
    "resolve_obj_file",
    "sionna_material_itu_type",
    "validate_asset_pool",
]
