from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SCHEMA_VERSION = "1.0"
PLACEMENT_CLASSES = {"table", "seat", "tabletop", "floor", "skip"}


@dataclass(frozen=True)
class AssetFiles:
    obj: str
    preview: str | None = None

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> AssetFiles:
        obj = str(payload.get("obj", "")).strip()
        if not obj:
            raise ValueError("Asset files.obj must not be empty")
        preview = payload.get("preview")
        return cls(obj=obj, preview=None if preview in (None, "") else str(preview))


@dataclass(frozen=True)
class AssetPlacement:
    class_name: str
    enabled: bool
    support: tuple[str, ...]
    weight: float

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> AssetPlacement:
        class_name = str(payload.get("class", "")).strip()
        if class_name not in PLACEMENT_CLASSES:
            raise ValueError(f"Invalid placement.class: {class_name}")
        support = tuple(str(item) for item in payload.get("support", []) or [])
        return cls(
            class_name=class_name,
            enabled=bool(payload.get("enabled", class_name != "skip")),
            support=support,
            weight=float(payload.get("weight", 1.0)),
        )


@dataclass(frozen=True)
class AssetGeometry:
    units: str
    size: tuple[float, float, float]
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> AssetGeometry:
        size = payload.get("size", {})
        bbox = payload.get("bbox", {})
        return cls(
            units=str(payload.get("units", "meter")),
            size=(float(size["x"]), float(size["y"]), float(size["z"])),
            bbox_min=tuple(float(value) for value in bbox["min"]),
            bbox_max=tuple(float(value) for value in bbox["max"]),
        )


@dataclass(frozen=True)
class NormalizationSpec:
    coordinate_system: dict[str, str]
    grounded: bool
    xy_origin: str
    orientation: str
    applied_z_rotation_radians: float

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> NormalizationSpec:
        coordinate_system = payload.get("coordinate_system", {})
        return cls(
            coordinate_system={key: str(value) for key, value in coordinate_system.items()},
            grounded=bool(payload.get("grounded", True)),
            xy_origin=str(payload.get("xy_origin", "bbox_center")),
            orientation=str(payload.get("orientation", "unknown")),
            applied_z_rotation_radians=float(payload.get("applied_z_rotation_radians", 0.0)),
        )


@dataclass(frozen=True)
class MaterialMapping:
    source: str
    sionna: str
    itu_type: str
    confidence: str

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> MaterialMapping:
        source = str(payload.get("source", "")).strip()
        sionna = str(payload.get("sionna", "")).strip()
        itu_type = str(payload.get("itu_type", "")).strip()
        confidence = str(payload.get("confidence", "unknown")).strip() or "unknown"
        if not source or not sionna:
            raise ValueError("Material mapping requires source and sionna")
        return cls(source=source, sionna=sionna, itu_type=itu_type, confidence=confidence)


@dataclass(frozen=True)
class AssetMaterials:
    sionna: tuple[str, ...]
    source_to_sionna: tuple[MaterialMapping, ...]

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> AssetMaterials:
        sionna = tuple(str(item) for item in payload.get("sionna", []) or [])
        mappings = tuple(MaterialMapping.from_mapping(item) for item in payload.get("source_to_sionna", []) or [])
        return cls(sionna=sionna, source_to_sionna=mappings)


@dataclass(frozen=True)
class AssetSpec:
    schema_version: str
    dataset: str
    id: str
    name: str
    export_name: str
    files: AssetFiles
    placement: AssetPlacement
    geometry: AssetGeometry
    normalization: NormalizationSpec
    materials: AssetMaterials

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> AssetSpec:
        schema_version = str(payload.get("schema_version", "")).strip()
        if schema_version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported asset schema_version: {schema_version}")
        asset_id = str(payload.get("id", "")).strip()
        if not asset_id:
            raise ValueError("Asset id must not be empty")
        name = str(payload.get("name", asset_id)).strip()
        return cls(
            schema_version=schema_version,
            dataset=str(payload.get("dataset", "")).strip() or "unknown",
            id=asset_id,
            name=name,
            export_name=str(payload.get("export_name", asset_id)).strip() or asset_id,
            files=AssetFiles.from_mapping(payload["files"]),
            placement=AssetPlacement.from_mapping(payload["placement"]),
            geometry=AssetGeometry.from_mapping(payload["geometry"]),
            normalization=NormalizationSpec.from_mapping(payload["normalization"]),
            materials=AssetMaterials.from_mapping(payload["materials"]),
        )


def is_clean_asset_mapping(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("schema_version") == SCHEMA_VERSION
        and isinstance(payload.get("files"), dict)
        and isinstance(payload.get("geometry"), dict)
        and isinstance(payload.get("materials"), dict)
        and isinstance(payload.get("placement"), dict)
    )
