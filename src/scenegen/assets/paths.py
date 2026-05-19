from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scenegen.paths import find_project_root

from .schema import AssetSpec


def portable_repo_path(path: Path, repo_root: Path) -> str:
    return os.path.relpath(path.resolve(), repo_root.resolve()).replace(os.sep, "/")


def resolve_asset_path(path_value: str | Path, catalog_path: Path) -> Path:
    raw = Path(str(path_value)).expanduser()
    if raw.is_absolute():
        resolved = raw.resolve()
        if resolved.exists():
            return resolved
        raise FileNotFoundError(f"Asset file not found: {resolved}")

    repo_root = find_project_root(catalog_path)
    candidates = [
        repo_root / raw,
        catalog_path.parent / raw,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"Asset file not found: {path_value}. Looked in: {searched}")


def resolve_obj_file(item: dict[str, Any] | AssetSpec, catalog_path: Path) -> Path:
    if isinstance(item, AssetSpec):
        return resolve_asset_path(item.files.obj, catalog_path)

    if "files" in item and isinstance(item["files"], dict):
        return resolve_asset_path(str(item["files"]["obj"]), catalog_path)

    export_name = str(item["export_name"])
    asset_name = str(item.get("asset_name", export_name))
    manifest_dir = catalog_path.parent
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
