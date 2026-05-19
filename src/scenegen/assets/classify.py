from __future__ import annotations

import re


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
