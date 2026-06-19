from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from pathlib import Path

from .exporters import write_bistro_scene_files, write_front3d_scene_files, write_scene_files
from .front3d import Front3DConfig, Front3DIndex, build_scene_from_front3d
from .geometry import load_bistro_base_scene
from .modes import FRONT3D_MODE, is_procedural_front3d_like, scene_prefix_for_mode
from .models import Asset, BistroBaseScene, Front3DBaseScene, PlacedAsset, Rect2D, Room, SceneMeshArrays
from .placement import build_bistro_scene_placements, build_scene_placements
from .procedural import ProceduralFront3DGenerator


@dataclass(frozen=True)
class SceneBuildResult:
    placements: list[PlacedAsset]
    bounds_xy: Rect2D
    record: dict[str, object]
    room: Room | None = None
    base_scene: BistroBaseScene | None = None
    front3d_base_scene: Front3DBaseScene | None = None
    floorplan_mesh_arrays: SceneMeshArrays | None = None


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


class Front3DSceneSource:
    mode = FRONT3D_MODE
    scene_prefix = "front3d"
    base_scene: BistroBaseScene | None = None
    forbidden_xy_rects: tuple[Rect2D, ...] = ()

    def __init__(self, args: argparse.Namespace):
        config = Front3DConfig(
            manifest=args.front3d_manifest,
            source_scene_dir=args.front3d_source_scene_dir,
            variant=args.front3d_variant,
            object_variant=args.front3d_object_variant,
            scene_ids=tuple(args.front3d_scene_ids),
            scene_selection=args.front3d_scene_selection,
            start_index=args.front3d_start_index,
            use_replace_jid=args.front3d_use_replace_jid,
            skip_missing_objects=args.front3d_skip_missing_objects,
            normalize_positive_xy=args.front3d_normalize_positive_xy,
            ground_objects=args.front3d_ground_objects,
        )
        self.index = Front3DIndex(config)
        self.config = config
        self.collect_floorplan_mesh_arrays = bool(args.floorplan_enabled and args.floorplan_geometry_enabled)
        self._selection_rng = random.Random(args.seed)
        self._selection_cursor = int(config.start_index) if not config.scene_ids else 0
        self._rejected_scene_ids: set[str] = set()

    def next_scene_id(self) -> str:
        if self.config.scene_ids:
            candidates = [scene_id for scene_id in self.config.scene_ids if scene_id not in self._rejected_scene_ids]
        elif self.config.scene_selection == "sequential":
            candidates = [scene_id for scene_id in self.index.scene_ids if scene_id not in self._rejected_scene_ids]
        else:
            candidates = [scene_id for scene_id in self.index.scene_ids if scene_id not in self._rejected_scene_ids]
            if candidates:
                return self._selection_rng.choice(candidates)

        if not candidates:
            raise RuntimeError("No 3D-FRONT scene ids remain after precheck filtering.")
        scene_id = candidates[self._selection_cursor % len(candidates)]
        self._selection_cursor += 1
        return scene_id

    def mark_scene_rejected(self, scene_id: str) -> None:
        if scene_id:
            self._rejected_scene_ids.add(scene_id)

    def build_scene_for_scene_id(
        self,
        scene_dir: Path,
        scene_index: int,
        scene_seed: int,
        rng: random.Random,
        scene_id: str,
    ) -> SceneBuildResult:
        build = build_scene_from_front3d(self.index, scene_id, scene_index)
        record, floorplan_mesh_arrays = write_front3d_scene_files(
            scene_dir,
            build.base_scene,
            build.placements,
            build.skipped_objects,
            scene_index,
            scene_seed,
            rng,
            collect_floorplan_mesh_arrays=self.collect_floorplan_mesh_arrays,
        )
        return SceneBuildResult(
            placements=build.placements,
            bounds_xy=(
                build.base_scene.bbox_min[0],
                build.base_scene.bbox_min[1],
                build.base_scene.bbox_max[0],
                build.base_scene.bbox_max[1],
            ),
            record=record,
            front3d_base_scene=build.base_scene,
            floorplan_mesh_arrays=floorplan_mesh_arrays,
        )

    def build_scene(
        self,
        scene_dir: Path,
        scene_index: int,
        scene_seed: int,
        rng: random.Random,
    ) -> SceneBuildResult:
        return self.build_scene_for_scene_id(scene_dir, scene_index, scene_seed, rng, self.next_scene_id())


class ProceduralFront3DSceneSource:
    mode = "procedural_front3d"
    scene_prefix = "procedural_front3d"
    base_scene: BistroBaseScene | None = None
    forbidden_xy_rects: tuple[Rect2D, ...] = ()

    def __init__(self, args: argparse.Namespace):
        config = Front3DConfig(
            manifest=args.front3d_manifest,
            source_scene_dir=args.front3d_source_scene_dir,
            variant=args.front3d_variant,
            object_variant=args.front3d_object_variant,
            scene_ids=(),
            scene_selection="random",
            start_index=0,
            use_replace_jid=False,
            skip_missing_objects=True,
            normalize_positive_xy=False,
            ground_objects=False,
        )
        self.index = Front3DIndex(config)
        self.generator = ProceduralFront3DGenerator(self.index, args)
        self.collect_floorplan_mesh_arrays = bool(args.floorplan_enabled and args.floorplan_geometry_enabled)
        self.mode = str(args.mode)
        self.scene_prefix = scene_prefix_for_mode(self.mode)
        self.write_scene_obj = bool(getattr(args, "output_write_scene_obj", True))
        self.write_sionna_assets = bool(getattr(args, "output_write_sionna_assets", True))

    def build_scene(
        self,
        scene_dir: Path,
        scene_index: int,
        scene_seed: int,
        rng: random.Random,
    ) -> SceneBuildResult:
        build = self.generator.build_scene(scene_dir, scene_index, rng)
        record, floorplan_mesh_arrays = write_front3d_scene_files(
            scene_dir,
            build.base_scene,
            build.placements,
            build.skipped_objects,
            scene_index,
            scene_seed,
            rng,
            collect_floorplan_mesh_arrays=self.collect_floorplan_mesh_arrays,
            mode=self.mode,
            write_scene_obj=self.write_scene_obj,
            write_sionna_assets=self.write_sionna_assets,
        )
        record["procedural"] = build.generation_report
        return SceneBuildResult(
            placements=build.placements,
            bounds_xy=(
                build.base_scene.bbox_min[0],
                build.base_scene.bbox_min[1],
                build.base_scene.bbox_max[0],
                build.base_scene.bbox_max[1],
            ),
            record=record,
            front3d_base_scene=build.base_scene,
            floorplan_mesh_arrays=floorplan_mesh_arrays,
        )


def create_scene_source(
    args: argparse.Namespace,
    assets_by_class: dict[str, list[Asset]],
    bistro_base_dir: Path | None,
) -> GeneratedSceneSource | BistroSceneSource | Front3DSceneSource | ProceduralFront3DSceneSource:
    if args.mode == "generated":
        return GeneratedSceneSource(assets_by_class, args)
    if args.mode == "bistro":
        if bistro_base_dir is None:
            raise ValueError("Bistro mode requires a base scene directory")
        return BistroSceneSource(assets_by_class, args, bistro_base_dir)
    if args.mode == "front3d":
        return Front3DSceneSource(args)
    if is_procedural_front3d_like(str(args.mode)):
        return ProceduralFront3DSceneSource(args)
    raise ValueError(f"Unsupported scene generation mode: {args.mode}")
