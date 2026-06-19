from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .postprocess.derived_maps import generate_maps_for_scene


ClassMaskArtifacts = tuple[np.ndarray, dict[str, Any], dict[int, str]]


@dataclass(frozen=True)
class MapsConfig:
    enabled: bool
    fail_on_error: bool
    overwrite: bool
    r_max_m: float
    los_stride_px: int
    snap_radius_m: float
    write_propagation: bool
    bs_label_mode: str
    bs_label_name: str | None
    bs_label_glob: str | None
    pair_cache_enabled: bool
    target_pairs_per_scene: int
    ue_candidates_per_bs: int | None
    pair_cache_seed: int

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> MapsConfig:
        bs_label = payload["bs_label"]
        pair_cache = payload["pair_cache"]
        return cls(
            enabled=bool(payload["enabled"]),
            fail_on_error=bool(payload["fail_on_error"]),
            overwrite=bool(payload["overwrite"]),
            r_max_m=float(payload["r_max_m"]),
            los_stride_px=int(payload["los_stride_px"]),
            snap_radius_m=float(payload["snap_radius_m"]),
            write_propagation=bool(payload["write_propagation"]),
            bs_label_mode=str(bs_label["mode"]),
            bs_label_name=None if bs_label["name"] is None else str(bs_label["name"]),
            bs_label_glob=None if bs_label["glob"] is None else str(bs_label["glob"]),
            pair_cache_enabled=bool(pair_cache["enabled"]),
            target_pairs_per_scene=int(pair_cache["target_pairs_per_scene"]),
            ue_candidates_per_bs=(
                None if pair_cache["ue_candidates_per_bs"] is None else int(pair_cache["ue_candidates_per_bs"])
            ),
            pair_cache_seed=int(pair_cache["seed"]),
        )

    def bs_label_kwargs(self) -> dict[str, str | None]:
        if self.bs_label_mode == "name":
            return {"bs_label_name": self.bs_label_name, "bs_label_glob": None}
        if self.bs_label_mode == "glob":
            return {"bs_label_name": None, "bs_label_glob": self.bs_label_glob}
        return {"bs_label_name": None, "bs_label_glob": None}


def generate_scene_maps(
    scene_dir: Path,
    run_dir: Path,
    config: MapsConfig,
    *,
    class_mask_artifacts: ClassMaskArtifacts | None = None,
) -> dict[str, Any]:
    return generate_maps_for_scene(
        scene_dir,
        run_dir,
        r_max_m=config.r_max_m,
        los_stride_pixels=config.los_stride_px,
        snap_radius_m=config.snap_radius_m,
        pair_cache_enabled=config.pair_cache_enabled,
        target_pairs_per_scene=config.target_pairs_per_scene,
        ue_candidates_per_bs=config.ue_candidates_per_bs,
        pair_cache_seed=config.pair_cache_seed,
        write_propagation=config.write_propagation,
        overwrite=config.overwrite,
        class_mask_artifacts=class_mask_artifacts,
        **config.bs_label_kwargs(),
    )
