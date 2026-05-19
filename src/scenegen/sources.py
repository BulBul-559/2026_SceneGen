from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from pathlib import Path

from .exporters import write_bistro_scene_files, write_scene_files
from .geometry import load_bistro_base_scene
from .models import Asset, BistroBaseScene, PlacedAsset, Rect2D, Room
from .placement import build_bistro_scene_placements, build_scene_placements


@dataclass(frozen=True)
class SceneBuildResult:
    placements: list[PlacedAsset]
    bounds_xy: Rect2D
    record: dict[str, object]
    room: Room | None = None
    base_scene: BistroBaseScene | None = None


class GeneratedSceneSource:
    mode = "generated"
    scene_prefix = "scene"
    base_scene: BistroBaseScene | None = None
    forbidden_xy_rects: tuple[Rect2D, ...] = ()

    def __init__(self, assets_by_class: dict[str, list[Asset]], args: argparse.Namespace):
        self.assets_by_class = assets_by_class
        self.args = args

    def build_scene(
        self,
        scene_dir: Path,
        scene_index: int,
        scene_seed: int,
        rng: random.Random,
    ) -> SceneBuildResult:
        room = Room(
            width=round(rng.uniform(7.0, 11.5), 3),
            length=round(rng.uniform(8.0, 14.0), 3),
            height=round(rng.uniform(3.0, 4.2), 3),
        )
        placements = build_scene_placements(rng, scene_index, room, self.assets_by_class, self.args)
        record = write_scene_files(scene_dir, room, placements, scene_index, scene_seed, rng)
        return SceneBuildResult(
            placements=placements,
            bounds_xy=(0.0, 0.0, room.width, room.length),
            record=record,
            room=room,
        )


class BistroSceneSource:
    mode = "bistro"
    scene_prefix = "bistro"

    def __init__(
        self,
        assets_by_class: dict[str, list[Asset]],
        args: argparse.Namespace,
        base_scene_dir: Path,
    ):
        self.assets_by_class = assets_by_class
        self.args = args
        self.base_scene = load_bistro_base_scene(base_scene_dir)
        self.forbidden_xy_rects = args.forbidden_xy_rects

    def build_scene(
        self,
        scene_dir: Path,
        scene_index: int,
        scene_seed: int,
        rng: random.Random,
    ) -> SceneBuildResult:
        placements = build_bistro_scene_placements(
            rng,
            scene_index,
            self.base_scene,
            self.assets_by_class,
            self.args,
            self.forbidden_xy_rects,
        )
        record = write_bistro_scene_files(
            scene_dir,
            self.base_scene,
            placements,
            scene_index,
            scene_seed,
            rng,
            self.forbidden_xy_rects,
        )
        return SceneBuildResult(
            placements=placements,
            bounds_xy=(
                self.base_scene.bbox_min[0],
                self.base_scene.bbox_min[1],
                self.base_scene.bbox_max[0],
                self.base_scene.bbox_max[1],
            ),
            record=record,
            base_scene=self.base_scene,
        )


def create_scene_source(
    args: argparse.Namespace,
    assets_by_class: dict[str, list[Asset]],
    bistro_base_dir: Path | None,
) -> GeneratedSceneSource | BistroSceneSource:
    if args.mode == "generated":
        return GeneratedSceneSource(assets_by_class, args)
    if args.mode == "bistro":
        if bistro_base_dir is None:
            raise ValueError("Bistro mode requires a base scene directory")
        return BistroSceneSource(assets_by_class, args, bistro_base_dir)
    raise ValueError(f"Unsupported scene generation mode: {args.mode}")
