from __future__ import annotations

import os
import shutil
from pathlib import Path

SIONNA_BASE_SCENE_MATERIAL = "itu-concrete"
SIONNA_DEFAULT_ASSET_MATERIAL = "itu-wood"


def find_project_root(start: Path | None = None) -> Path:
    candidates: list[Path] = []
    if start is not None:
        candidates.append(start.resolve())
    candidates.append(Path.cwd().resolve())
    candidates.extend(Path(__file__).resolve().parents)

    for candidate in candidates:
        if candidate.is_file():
            candidate = candidate.parent
        for parent in (candidate, *candidate.parents):
            if (parent / "data" / "scene" / "scene.obj").is_file() and (
                parent / "data" / "assets" / "manifest.json"
            ).is_file():
                return parent
            if (parent / "pyproject.toml").is_file() and (parent / "src" / "scenegen").is_dir():
                return parent
    return Path(__file__).resolve().parents[2]


def default_bistro_base_dir(repo_root: Path | None = None) -> Path:
    return (repo_root or find_project_root()) / "data" / "scene"


def default_asset_manifest(repo_root: Path | None = None) -> Path:
    return (repo_root or find_project_root()) / "data" / "assets" / "manifest.json"


def default_output_dir(repo_root: Path | None = None) -> Path:
    return (repo_root or find_project_root()) / "results"


def default_config_path(repo_root: Path | None = None) -> Path:
    return (repo_root or find_project_root()) / "config" / "default.yaml"


def require_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"{label} not found: {resolved}")
    return resolved


def require_dir(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise FileNotFoundError(f"{label} not found: {resolved}")
    return resolved


def clean_output_root(output_root: Path) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    for child in output_root.iterdir():
        if child.name.startswith("."):
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def portable_path(path: Path, root: Path) -> str:
    """Return a POSIX relative path for JSON metadata."""
    target = path.expanduser()
    base = root.expanduser()
    try:
        rel = target.resolve().relative_to(base.resolve())
        return rel.as_posix()
    except ValueError:
        return os.path.relpath(target.resolve(), base.resolve()).replace(os.sep, "/")
