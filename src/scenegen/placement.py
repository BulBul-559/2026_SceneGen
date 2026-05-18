from __future__ import annotations

import argparse
import math
import random
from collections.abc import Sequence

from .geometry import aabb_for, can_place, collides_with_bistro_static, inside_bistro_scene, inside_room
from .geometry import is_supported_on_floor, room_bounds, sample_support_point, sample_support_surface
from .models import Asset, BistroBaseScene, Box3D, PlacedAsset, Rect2D, Room


def overlaps_forbidden_xy(box: Box3D, forbidden_xy_rects: Sequence[Rect2D] | None) -> bool:
    if not forbidden_xy_rects:
        return False
    return any(
        box[0] <= rect[2] and box[1] >= rect[0] and box[2] <= rect[3] and box[3] >= rect[1]
        for rect in forbidden_xy_rects
    )


def allowed_by_forbidden_xy(box: Box3D, forbidden_xy_rects: Sequence[Rect2D] | None) -> bool:
    return not overlaps_forbidden_xy(box, forbidden_xy_rects)


def create_placement(
    asset: Asset,
    instance_name: str,
    x: float,
    y: float,
    z: float,
    yaw: float,
    support_type: str,
    parent: str | None,
) -> PlacedAsset:
    min_x, max_x, min_y, max_y, min_z, max_z = aabb_for(asset, x, y, z, yaw)
    return PlacedAsset(
        asset=asset,
        instance_name=instance_name,
        x=x,
        y=y,
        z=z,
        yaw=yaw,
        support_type=support_type,
        parent=parent,
        min_x=min_x,
        max_x=max_x,
        min_y=min_y,
        max_y=max_y,
        min_z=min_z,
        max_z=max_z,
    )


def place_floor_asset(
    rng: random.Random,
    room: Room,
    asset: Asset,
    placed: list[PlacedAsset],
    instance_name: str,
    max_attempts: int,
    padding: float,
    forbidden_xy_rects: Sequence[Rect2D] | None = None,
    near_wall: bool = False,
) -> PlacedAsset | None:
    for _ in range(max_attempts):
        yaw = rng.choice((0.0, math.pi / 2.0, math.pi, -math.pi / 2.0)) + rng.uniform(-0.18, 0.18)
        box_margin = max(asset.width, asset.length) / 2.0 + 0.35
        min_x, max_x, min_y, max_y = room_bounds(room, box_margin)
        if min_x >= max_x or min_y >= max_y:
            return None
        if near_wall:
            side = rng.choice(("left", "right", "bottom", "top"))
            if side == "left":
                x = min_x
                y = rng.uniform(min_y, max_y)
            elif side == "right":
                x = max_x
                y = rng.uniform(min_y, max_y)
            elif side == "bottom":
                x = rng.uniform(min_x, max_x)
                y = min_y
            else:
                x = rng.uniform(min_x, max_x)
                y = max_y
        else:
            x = rng.uniform(min_x, max_x)
            y = rng.uniform(min_y, max_y)
        box = aabb_for(asset, x, y, 0.0, yaw)
        if (
            inside_room(box, room)
            and allowed_by_forbidden_xy(box, forbidden_xy_rects)
            and can_place(box, placed, padding)
        ):
            return create_placement(asset, instance_name, x, y, 0.0, yaw, "floor", None)
    return None


def place_bistro_floor_asset(
    rng: random.Random,
    base_scene: BistroBaseScene,
    asset: Asset,
    placed: list[PlacedAsset],
    instance_name: str,
    max_attempts: int,
    padding: float,
    forbidden_xy_rects: Sequence[Rect2D],
) -> PlacedAsset | None:
    for _ in range(max_attempts):
        yaw = rng.uniform(-math.pi, math.pi)
        x, y = sample_support_point(rng, base_scene.floor_triangles)
        if not is_supported_on_floor(x, y, asset.width / 2.0, asset.length / 2.0, yaw, base_scene.floor_triangles):
            continue
        box = aabb_for(asset, x, y, base_scene.floor_z, yaw)
        if (
            inside_bistro_scene(box, base_scene)
            and allowed_by_forbidden_xy(box, forbidden_xy_rects)
            and not collides_with_bistro_static(box, base_scene, clearance=0.12)
            and can_place(box, placed, padding)
        ):
            return create_placement(asset, instance_name, x, y, base_scene.floor_z, yaw, "bistro_floor", None)
    return None


def yaw_for_forward_vector(forward_x: float, forward_y: float) -> float:
    return math.atan2(-forward_x, forward_y)


def place_seats_around_table(
    rng: random.Random,
    room: Room,
    table: PlacedAsset,
    seat_assets: list[Asset],
    placed: list[PlacedAsset],
    forbidden_xy_rects: Sequence[Rect2D] | None = None,
) -> list[PlacedAsset]:
    if not seat_assets:
        return []
    local_dirs = [(0.0, 1.0), (1.0, 0.0), (0.0, -1.0), (-1.0, 0.0)]
    rng.shuffle(local_dirs)
    seats: list[PlacedAsset] = []
    for slot, (dx, dy) in enumerate(local_dirs[: rng.randint(2, 4)], start=1):
        seat = rng.choice(seat_assets)
        table_half = table.asset.length / 2.0 if abs(dy) > 0.5 else table.asset.width / 2.0
        offset = table_half + max(seat.width, seat.length) / 2.0 + rng.uniform(0.12, 0.25)
        cos_yaw = math.cos(table.yaw)
        sin_yaw = math.sin(table.yaw)
        world_dx = cos_yaw * dx - sin_yaw * dy
        world_dy = sin_yaw * dx + cos_yaw * dy
        x = table.x + world_dx * offset
        y = table.y + world_dy * offset
        yaw = yaw_for_forward_vector(-world_dx, -world_dy)
        name = f"{table.instance_name}_seat{slot}_{seat.export_name}"
        box = aabb_for(seat, x, y, 0.0, yaw)
        if (
            inside_room(box, room)
            and allowed_by_forbidden_xy(box, forbidden_xy_rects)
            and can_place(box, placed + seats, 0.06)
        ):
            seats.append(create_placement(seat, name, x, y, 0.0, yaw, "floor_near_table", table.instance_name))
    return seats


def place_bistro_seats_around_table(
    rng: random.Random,
    base_scene: BistroBaseScene,
    table: PlacedAsset,
    seat_assets: list[Asset],
    placed: list[PlacedAsset],
    forbidden_xy_rects: Sequence[Rect2D],
) -> list[PlacedAsset]:
    if not seat_assets:
        return []
    local_dirs = [(0.0, 1.0), (1.0, 0.0), (0.0, -1.0), (-1.0, 0.0)]
    rng.shuffle(local_dirs)
    seats: list[PlacedAsset] = []
    for slot, (dx, dy) in enumerate(local_dirs[: rng.randint(2, 4)], start=1):
        seat = rng.choice(seat_assets)
        table_half = table.asset.length / 2.0 if abs(dy) > 0.5 else table.asset.width / 2.0
        offset = table_half + max(seat.width, seat.length) / 2.0 + rng.uniform(0.12, 0.25)
        cos_yaw = math.cos(table.yaw)
        sin_yaw = math.sin(table.yaw)
        world_dx = cos_yaw * dx - sin_yaw * dy
        world_dy = sin_yaw * dx + cos_yaw * dy
        x = table.x + world_dx * offset
        y = table.y + world_dy * offset
        yaw = yaw_for_forward_vector(-world_dx, -world_dy)
        if not is_supported_on_floor(x, y, seat.width / 2.0, seat.length / 2.0, yaw, base_scene.floor_triangles):
            continue
        name = f"{table.instance_name}_seat{slot}_{seat.export_name}"
        box = aabb_for(seat, x, y, base_scene.floor_z, yaw)
        if (
            inside_bistro_scene(box, base_scene)
            and allowed_by_forbidden_xy(box, forbidden_xy_rects)
            and not collides_with_bistro_static(box, base_scene, clearance=0.10)
            and can_place(box, placed + seats, 0.06)
        ):
            seats.append(
                create_placement(seat, name, x, y, base_scene.floor_z, yaw, "bistro_floor_near_table", table.instance_name)
            )
    return seats


def place_tabletop_items(
    rng: random.Random,
    table: PlacedAsset,
    tabletop_assets: list[Asset],
    placed: list[PlacedAsset],
    min_items: int,
    max_items: int,
    max_attempts: int,
    forbidden_xy_rects: Sequence[Rect2D] | None = None,
) -> list[PlacedAsset]:
    if not tabletop_assets:
        return []
    suitable = [
        asset
        for asset in tabletop_assets
        if asset.width < table.asset.width * 0.55 and asset.length < table.asset.length * 0.55
    ]
    if not suitable:
        return []
    items: list[PlacedAsset] = []
    for slot in range(1, rng.randint(min_items, max_items) + 1):
        asset = rng.choice(suitable)
        local_limit_x = table.asset.width / 2.0 - asset.width / 2.0 - 0.06
        local_limit_y = table.asset.length / 2.0 - asset.length / 2.0 - 0.06
        if local_limit_x <= 0.0 or local_limit_y <= 0.0:
            continue
        for _ in range(max_attempts):
            local_x = rng.uniform(-local_limit_x, local_limit_x)
            local_y = rng.uniform(-local_limit_y, local_limit_y)
            cos_yaw = math.cos(table.yaw)
            sin_yaw = math.sin(table.yaw)
            x = table.x + cos_yaw * local_x - sin_yaw * local_y
            y = table.y + sin_yaw * local_x + cos_yaw * local_y
            z = table.z + table.asset.height + 0.002
            yaw = table.yaw + rng.uniform(-math.pi, math.pi)
            name = f"{table.instance_name}_top{slot}_{asset.export_name}"
            box = aabb_for(asset, x, y, z, yaw)
            if allowed_by_forbidden_xy(box, forbidden_xy_rects) and can_place(box, placed + items, 0.015):
                items.append(create_placement(asset, name, x, y, z, yaw, "tabletop", table.instance_name))
                break
    return items


def place_bistro_existing_support_items(
    rng: random.Random,
    scene_index: int,
    base_scene: BistroBaseScene,
    tabletop_assets: list[Asset],
    placed: list[PlacedAsset],
    count: int,
    max_attempts: int,
    forbidden_xy_rects: Sequence[Rect2D],
) -> list[PlacedAsset]:
    if not base_scene.support_surfaces or not tabletop_assets or count <= 0:
        return []
    suitable = [
        asset
        for asset in tabletop_assets
        if asset.width <= 0.48 and asset.length <= 0.48 and asset.height <= 0.75
    ]
    if not suitable:
        return []

    items: list[PlacedAsset] = []
    for slot in range(1, count + 1):
        asset = rng.choice(suitable)
        for _ in range(max_attempts):
            surface = sample_support_surface(rng, base_scene.support_surfaces)
            yaw = rng.uniform(-math.pi, math.pi)
            x, y = sample_support_point(rng, surface.triangles)
            if not is_supported_on_floor(x, y, asset.width / 2.0, asset.length / 2.0, yaw, surface.triangles):
                continue
            z = surface.z + 0.002
            box = aabb_for(asset, x, y, z, yaw)
            if not inside_bistro_scene(box, base_scene, margin=0.05):
                continue
            if not allowed_by_forbidden_xy(box, forbidden_xy_rects):
                continue
            if collides_with_bistro_static(box, base_scene, clearance=0.03):
                continue
            if not can_place(box, placed + items, 0.02):
                continue
            name = f"b{scene_index:04d}_support{slot}_{asset.export_name}"
            items.append(create_placement(asset, name, x, y, z, yaw, "bistro_existing_support", None))
            break
    return items


def build_scene_placements(
    rng: random.Random,
    scene_index: int,
    room: Room,
    assets_by_class: dict[str, list[Asset]],
    args: argparse.Namespace,
) -> list[PlacedAsset]:
    placed: list[PlacedAsset] = []
    table_count = rng.randint(args.min_tables, args.max_tables)
    for slot in range(1, table_count + 1):
        table = rng.choice(assets_by_class["table"])
        name = f"s{scene_index:04d}_table{slot}_{table.export_name}"
        placement = place_floor_asset(rng, room, table, placed, name, args.max_attempts, padding=0.18)
        if placement is None:
            continue
        placed.append(placement)
        placed.extend(place_seats_around_table(rng, room, placement, assets_by_class["seat"], placed))
        placed.extend(
            place_tabletop_items(
                rng,
                placement,
                assets_by_class["tabletop"],
                placed,
                args.min_tabletop_items,
                args.max_tabletop_items,
                args.max_attempts,
            )
        )

    for slot in range(1, args.floor_extras + 1):
        if not assets_by_class["floor"]:
            break
        asset = rng.choice(assets_by_class["floor"])
        name = f"s{scene_index:04d}_floor{slot}_{asset.export_name}"
        placement = place_floor_asset(
            rng,
            room,
            asset,
            placed,
            name,
            args.max_attempts,
            padding=0.12,
            near_wall=True,
        )
        if placement is not None:
            placed.append(placement)
    return placed


def build_bistro_scene_placements(
    rng: random.Random,
    scene_index: int,
    base_scene: BistroBaseScene,
    assets_by_class: dict[str, list[Asset]],
    args: argparse.Namespace,
    forbidden_xy_rects: Sequence[Rect2D],
) -> list[PlacedAsset]:
    placed: list[PlacedAsset] = []
    table_count = rng.randint(args.min_tables, args.max_tables)
    for slot in range(1, table_count + 1):
        table = rng.choice(assets_by_class["table"])
        name = f"b{scene_index:04d}_table{slot}_{table.export_name}"
        placement = place_bistro_floor_asset(
            rng,
            base_scene,
            table,
            placed,
            name,
            args.max_attempts,
            padding=0.18,
            forbidden_xy_rects=forbidden_xy_rects,
        )
        if placement is None:
            continue
        placed.append(placement)
        placed.extend(
            place_bistro_seats_around_table(
                rng,
                base_scene,
                placement,
                assets_by_class["seat"],
                placed,
                forbidden_xy_rects,
            )
        )
        placed.extend(
            place_tabletop_items(
                rng,
                placement,
                assets_by_class["tabletop"],
                placed,
                args.min_tabletop_items,
                args.max_tabletop_items,
                args.max_attempts,
                forbidden_xy_rects,
            )
        )

    for slot in range(1, args.floor_extras + 1):
        if not assets_by_class["floor"]:
            break
        asset = rng.choice(assets_by_class["floor"])
        name = f"b{scene_index:04d}_floor{slot}_{asset.export_name}"
        placement = place_bistro_floor_asset(
            rng,
            base_scene,
            asset,
            placed,
            name,
            args.max_attempts,
            padding=0.12,
            forbidden_xy_rects=forbidden_xy_rects,
        )
        if placement is not None:
            placed.append(placement)
    placed.extend(
        place_bistro_existing_support_items(
            rng,
            scene_index,
            base_scene,
            assets_by_class["tabletop"],
            placed,
            args.bistro_support_items,
            args.max_attempts,
            forbidden_xy_rects,
        )
    )
    return placed
