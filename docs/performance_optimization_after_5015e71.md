# Performance Optimization Summary After `5015e71`

This document preserves the performance work done after commit `5015e71d4e1f1165ce73672ab3337eb23c63d699` (`Refresh project TODO backlog`). It is intentionally self-contained because many intermediate `results/` runs are temporary and may be cleaned.

## Scope And Benchmark

The primary single-scene benchmark uses the same Front3D scene as the original timing probe:

- Scene id: `fff98d42-99a4-43fc-9639-5761cb4f87df`
- Mode: `front3d`
- Label variants: `panel` and `walk` at grid sizes `0.1`, `0.2`, `0.4`, `0.5`
- Label mask resolution: `0.05m`
- Floorplan: geometry `sampling`, `resolution_m=0.05`, `height=1.6m`
- Class mask: enabled, furniture mode `mesh`

Baseline run:

- Run: `debug_label_subtiming_0p05_visual_6812_20260616`
- Total: `63.11s`
- Key bottleneck: `label=40.37s`, especially `center_bs=29.28s`

Current comparison run:

- Run: `perf_doc_current_6812_20260616`
- Total: `6.75s`
- Same scene and same 8 label variants

## Executive Summary

| Stage | Baseline (s) | Current (s) | Speedup | Reduction |
| --- | ---: | ---: | ---: | ---: |
| total_run | 63.11 | 6.75 | 9.35x | 89.3% |
| build_scene | 3.16 | 2.48 | 1.27x | 21.5% |
| label | 40.37 | 0.87 | 46.64x | 97.9% |
| floorplan | 18.62 | 2.65 | 7.02x | 85.8% |
| floorplan_geometry | 18.62 | 2.07 | 9.01x | 88.9% |
| class_mask | n/a | 0.58 | n/a | n/a |
| label_overlay | 0.64 | 0.35 | 1.81x | 44.6% |

The largest wins were:

1. Replacing repeated per-variant label work with cached/global vectorized sampling and fast point assignment.
2. Reusing generated scene mesh arrays and capping/chunking floorplan sampling.
3. Vectorizing class mask and furniture mask rendering.
4. Reducing PNG compression overhead.
5. Adding batch-level worker scheduling, move-based publishing, skipped child summaries, and hybrid work stealing.

## Stage 1: Instrumentation And Baseline

The first important change was not optimization but observability.

What was recorded:

- Scene-level `timings_s`
- Label substage timings across variants
- Floorplan/class-mask timings
- Build-scene timings for OBJ/Sionna asset export
- Batch child subprocess overhead and publish time

Baseline label substage table:

| Label Substage | Baseline (s) |
| --- | ---: |
| build_contexts | 0.84 |
| global_sampling | 0.46 |
| assign_points | 6.46 |
| center_bs | 29.28 |
| build_groups_validate | 2.85 |
| write_outputs | 0.43 |
| total_variant | 40.34 |

This made it clear that `center_bs` and point assignment were the immediate label bottlenecks, while floorplan geometry was the second major bottleneck.

## Stage 2: Label Pipeline

Main optimizations:

- Built a fixed-resolution global sampling mask once per scene/config and reused it across all label variants.
- Vectorized global candidate generation and mask lookups.
- Replaced repeated room assignment scans with cached/vectorized point assignment.
- Optimized `center_bs` selection so it no longer rescans the full dense point set for every variant.
- Reduced duplicated label payload/report work.
- Kept `panel` and `walk` variants separate while sharing common upstream masks.

Label substage result:

| Label Substage | Baseline (s) | Current (s) | Speedup | Reduction |
| --- | ---: | ---: | ---: | ---: |
| build_contexts | 0.842 | 0.089 | 9.5x | 89.5% |
| global_sampling | 0.460 | 0.254 | 1.8x | 44.7% |
| assign_points | 6.465 | 0.151 | 42.8x | 97.7% |
| center_bs | 29.276 | 0.053 | 548.2x | 99.8% |
| build_groups_validate | 2.849 | 0.204 | 14.0x | 92.8% |
| write_outputs | 0.426 | 0.097 | 4.4x | 77.3% |
| total_variant | 40.342 | 0.861 | 46.8x | 97.9% |

Representative per-variant current timings:

| Variant | Current (s) |
| --- | ---: |
| label_panel_0p1 | 0.481 |
| label_panel_0p2 | 0.076 |
| label_panel_0p4 | 0.040 |
| label_panel_0p5 | 0.034 |
| label_walk_0p1 | 0.164 |
| label_walk_0p2 | 0.044 |
| label_walk_0p4 | 0.013 |
| label_walk_0p5 | 0.009 |

Interpretation:

- Dense `0.1m` variants still dominate label time, but they are now below one second.
- `walk` became cheaper than `panel_0p1` in this scene because the expensive global mask and BS work are shared, and the obstacle filtering is vectorized.

## Stage 3: Floorplan Geometry

Current production default:

```yaml
floorplan:
  resolution_m: 0.05
  geometry:
    projection: sampling
    height:
      mode: heights
      values_m: [1.6]
  sampling:
    density_scale: 128.0
    min_points: 100000
    max_points: 4000000
```

Main optimizations:

- Removed unnecessary default outputs such as semantic floorplan, clean geometry, and `geometry_raw.png`.
- Defaulted to a single explicit height layer, `floorplan_1p60.png`.
- Reused in-memory `SceneMeshArrays` generated while writing `scene.obj`, avoiding a second full OBJ parse.
- Capped high-density sampling at `4,000,000` points for the default template.
- Replaced slower surface sampling code with numpy area-weighted chunked sampling.
- Vectorized rasterization and pixel minimum-height map construction.
- Reduced PNG compression level for floorplan images.

Current floorplan geometry internals for the benchmark scene:

| Substage | Current (s) |
| --- | ---: |
| load_orient_mesh | 0.044 |
| sample_surface | 1.490 |
| combine_points | 0.022 |
| detect_height_range | 0.015 |
| prepare_bounds | 0.020 |
| side_view | 0.053 |
| rasterize_points | 0.195 |
| pixel_min_height | 0.108 |
| build_projection | 0.095 |
| write_stack | 0.003 |
| write_preview | 0.020 |
| floorplan_geometry total | 2.067 |

The optional `ray_height_filtered` projection was implemented and tested, but it is not the current production default. The default remains high-density `sampling` because it is visually acceptable, fast enough after optimization, and compatible with existing downstream expectations.

## Stage 4: Class Mask And Furniture Mesh Mask

Current default:

```yaml
floorplan:
  class_mask:
    enabled: true
    wall_dilation_m: 0.0
    furniture_dilation_m: 0.05
    furniture_mode: mesh
    furniture_height_m: 1.6
```

Main optimizations:

- Moved from bbox-only furniture masks to mesh footprint masks while keeping `furniture_mode: bbox | mesh`.
- Rasterized projected furniture triangles with height filtering.
- Added face/triangle caching and duplicate projected primitive filtering.
- Replaced set-heavy duplicate handling with vectorized/array-friendly paths.
- Kept door/opening logic shared with label and class mask paths.

Current class mask internals for the benchmark scene:

| Substage | Current (s) |
| --- | ---: |
| read_inputs | 0.060 |
| prepare_canvas | 0.000 |
| draw_architecture | 0.112 |
| process_openings_walls | 0.000 |
| build_furniture_mask | 0.397 |
| compose_layers | 0.000 |
| write_outputs | 0.005 |
| class_mask total | 0.580 |

Furniture-mask details from the current run:

- Furniture objects: `19`
- Mesh objects: `19`
- Triangle count: `181001`
- Painted projected triangles: `32693`
- Duplicate projected triangles skipped: `148289`

## Stage 5: Build Scene And Sionna Assets

Main optimizations:

- Vectorized Front3D object transforms with numpy.
- Reduced conversion overhead when writing combined `scene.obj`.
- Reused mesh arrays for floorplan generation.
- Added fast paths for single-material Sionna asset export.
- Recorded `write_scene_obj`, `write_sionna_assets`, and `write_placements_json` timings.

Current build timings for the benchmark scene:

| Build Substage | Current (s) |
| --- | ---: |
| write_scene_obj | 0.757 |
| write_sionna_assets | 1.022 |
| write_placements_json | 0.007 |
| build_scene total | 2.481 |

Build-scene speedup is smaller than label/floorplan because it was not the original dominant bottleneck. It remains a meaningful cost for very large batches, especially `write_sionna_assets`.

## Stage 6: Label Overlay And PNG IO

Main optimizations:

- Reduced PNG compression level for floorplan and overlay outputs.
- Avoided unnecessary payload expansion for overlay rendering.
- Cached/reused point positions needed by overlay.

Result:

| Stage | Baseline (s) | Current (s) | Speedup |
| --- | ---: | ---: | ---: |
| label_overlay | 0.637 | 0.353 | 1.81x |

This is a smaller absolute win than label/floorplan, but important because every label variant can produce a visualization image.

## Stage 7: Batch And Multi-Worker Production

Batch changes:

- Added worker-level logs, JSONL event logs, state table, retry/dead-letter queues, and tracebacks.
- Added `task_subprocess`, `publish_scene`, and child-run timing records.
- Changed successful scene publishing from `copytree` to `move`.
- Skipped child-run `summary/` generation; only the top-level batch run now builds summary folders.
- Added `static`, `dynamic`, and `hybrid` scheduling.
- Made `hybrid` the default after the 90-scene sweep.

30-scene publishing/storage comparison:

| Run | Scheduler | Wall Time (s) | Success / Fail | Total Size | worker_runs Size | Notes |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| batch_static30_round2_20260616 | static | 48.13 | 27 / 3 | 2.8G | 1.5G | old copytree behavior |
| batch_static30_skipchildsummary_20260616 | static | 44.09 | 27 / 3 | 1.4G | 76M | move publish + skipped child summary |
| batch_static30_current_20260616 | static | 44.10 | 27 / 3 | 1.4G | 77M | current timing fields verified |

The biggest batch win was storage and IO pressure: successful scenes are no longer duplicated under `batch/worker_runs`.

## 90-Scene Scheduler Sweep

The 30-scene worker sweep was too short for high worker counts, because 16+ workers receive only one or two tasks each. A second 90-scene sweep tested static, dynamic, and hybrid scheduling.

| Scheduler | Workers | Wall Time (s) | Success / Fail | Success/min | Worker Imbalance (s) |
| --- | ---: | ---: | ---: | ---: | ---: |
| static | 8 | 162.62 | 79 / 11 | 29.15 | 38.56 |
| static | 16 | 111.15 | 79 / 11 | 42.65 | 51.60 |
| static | 24 | 95.05 | 79 / 11 | 49.87 | 51.54 |
| static | 32 | 88.80 | 79 / 11 | 53.38 | 57.69 |
| dynamic | 8 | 130.94 | 79 / 11 | 36.20 | 14.70 |
| dynamic | 16 | 95.26 | 79 / 11 | 49.76 | 17.42 |
| dynamic | 24 | 90.33 | 79 / 11 | 52.47 | 31.10 |
| dynamic | 32 | 76.62 | 79 / 11 | 61.86 | 28.55 |
| hybrid | 8 | 121.59 | 79 / 11 | 38.98 | 8.33 |
| hybrid | 16 | 98.33 | 79 / 11 | 48.21 | 17.53 |
| hybrid | 24 | 82.27 | 79 / 11 | 57.62 | 16.41 |
| hybrid | 32 | 74.06 | 79 / 11 | 64.00 | 29.56 |

The 11 failed scenes were identical across scheduler/worker settings, which indicates data/precheck failures rather than worker-count instability.

Hybrid policy:

1. Start with static shard queues: `shard_id = plan_index % workers`.
2. A worker always consumes its own queue first.
3. If its queue is empty and other queues still have pending tasks, it steals one task from the queue with the most remaining tasks.
4. Stolen claims are logged in `batch/logs/events.jsonl` with `stolen: true` and `source_worker_id`.

Current recommendation:

- Stable production: `--workers 24 --scheduler hybrid`
- Maximum observed throughput in this sweep: `--workers 32 --scheduler hybrid`
- Conservative shared-machine setting: `--workers 16 --scheduler hybrid`
- Keep `static` for strict fixed-shard debugging
- Keep `dynamic` for comparison or fully shared queue behavior

## Current Production Defaults After Optimization

The full Front3D task template now represents the intended large-scale simulation setting:

- Label: panel + walk
- Label grids: `0.1`, `0.2`, `0.4`, `0.5`
- Label mask resolution: `0.05m`
- UE wall clearance: `0.2m`
- UE furniture clearance: `0.1m`
- Center BS: enabled
- Floorplan geometry: `sampling`
- Floorplan resolution: `0.05m`
- Class mask: enabled
- Furniture mask: `mesh`
- Furniture dilation: `0.05m`
- Batch scheduler: `hybrid`

## Reproduction Commands

Single-scene current benchmark:

```bash
uv run scenegen \
  --config config/tasks/front3d_full_simulation.yaml \
  --set pipeline.scenes=1 \
  --set pipeline.run_name=perf_doc_current_6812_20260616 \
  --set pipeline.clean=true \
  --set 'front3d.scene_ids=[fff98d42-99a4-43fc-9639-5761cb4f87df]'
```

90-scene hybrid sweep example:

```bash
uv run scenegen-batch \
  --config config/tasks/front3d_full_simulation.yaml \
  --workers 32 \
  --scheduler hybrid \
  --max-retries 0 \
  --set pipeline.scenes=90 \
  --set pipeline.run_name=worker_sweep90_hybrid_w32_20260616 \
  --set pipeline.clean=true
```

## Notes And Caveats

- The single-scene timings are for one representative Front3D scene. Different scenes vary with object count, room layout, mesh complexity, and label point counts.
- Intermediate result directories are not source of truth and may be cleaned. This document preserves the important measurements.
- `ray_height_filtered` floorplan projection remains available but is not the production default.
- The current bottleneck for single-scene runs is no longer label. The remaining large costs are scene/Sionna asset writing and floorplan sampling/class-mask rendering.
- Inner-scene parallelism is not currently enabled. Parallelism is primarily at the batch worker level.
