#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path

from scenegen.deepmimo_reconstruct import write_reconstructed_deepmimo_source


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build approximate DeepMIMO indoor geometry sources.")
    parser.add_argument("--source-root", type=Path, required=True, help="DeepMIMO source root containing i1_2p5/i2_28b.")
    parser.add_argument("--output-root", type=Path, required=True, help="Directory for reconstructed source scenes.")
    parser.add_argument(
        "--scene-id",
        action="append",
        dest="scene_ids",
        default=[],
        help="Scene id to reconstruct. Defaults to i2_28b and i1_2p5.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    scene_ids = args.scene_ids or ["i2_28b", "i1_2p5"]
    results = []
    for scene_id in scene_ids:
        output = write_reconstructed_deepmimo_source(args.source_root / scene_id, args.output_root)
        results.append(
            {
                "scene_id": output.scene_id,
                "source_scene_id": output.source_scene_id,
                "output_dir": str(output.output_dir),
                "vertex_count": int(output.vertices.shape[0]),
                "object_count": len(output.objects),
                "report": output.report,
            }
        )
    print(json.dumps({"ok": True, "scenes": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
