from __future__ import annotations

FRONT3D_MODE = "front3d"
PROCEDURAL_FRONT3D_MODE = "procedural_front3d"
PROCEDURAL_FRONT3D_VISION_MODE = "procedural_front3d_vision"

FRONT3D_LIKE_MODES = frozenset(
    {
        FRONT3D_MODE,
        PROCEDURAL_FRONT3D_MODE,
        PROCEDURAL_FRONT3D_VISION_MODE,
    }
)
PROCEDURAL_FRONT3D_LIKE_MODES = frozenset(
    {
        PROCEDURAL_FRONT3D_MODE,
        PROCEDURAL_FRONT3D_VISION_MODE,
    }
)
SUPPORTED_PIPELINE_MODES = frozenset(
    {
        "generated",
        "bistro",
        *FRONT3D_LIKE_MODES,
    }
)


def is_front3d_like(mode: str) -> bool:
    return mode in FRONT3D_LIKE_MODES


def is_procedural_front3d_like(mode: str) -> bool:
    return mode in PROCEDURAL_FRONT3D_LIKE_MODES


def scene_prefix_for_mode(mode: str) -> str:
    if mode == FRONT3D_MODE:
        return "front3d"
    if mode == PROCEDURAL_FRONT3D_MODE:
        return "procedural_front3d"
    if mode == PROCEDURAL_FRONT3D_VISION_MODE:
        return "procedural_front3d_vision"
    raise ValueError(f"Mode has no Front3D-like scene prefix: {mode}")
