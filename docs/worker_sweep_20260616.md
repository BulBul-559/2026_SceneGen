# Worker Sweep 2026-06-16

This note records a 30-scene batch worker sweep for the current `front3d_full_simulation` task template.

## Setup

- Branch: `codex/batch-worker-performance`
- Config: `config/tasks/front3d_full_simulation.yaml`
- Scenes: first 30 sequential Front3D scenes
- Retries: `--max-retries 0`
- Output root: `results`
- Machine state before sweep: 112 logical CPUs, high background load, swap nearly full
- Known data failures: `front3d_0020`, `front3d_0026`, `front3d_0027` failed consistently in all runs

Representative command:

```bash
uv run scenegen-batch \
  --config config/tasks/front3d_full_simulation.yaml \
  --workers 24 \
  --scheduler static \
  --max-retries 0 \
  --set pipeline.scenes=30 \
  --set pipeline.run_name=worker_sweep_static_w24_30_20260616 \
  --set pipeline.clean=true
```

## Results

| Scheduler | Workers | Wall Time (s) | Success / Fail | Success/min | Task Mean (s) | Task P95 (s) | Worker Max (s) | Worker Imbalance (s) | Worker Task Range |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| static | 1 | 236.59 | 27 / 3 | 6.85 | 7.79 | 17.82 | 233.57 | 0.00 | 30-30 |
| static | 2 | 137.86 | 27 / 3 | 11.75 | 8.07 | 16.78 | 135.57 | 28.95 | 15-15 |
| static | 4 | 73.09 | 27 / 3 | 22.16 | 8.49 | 17.81 | 71.49 | 20.45 | 7-8 |
| static | 8 | 47.55 | 27 / 3 | 34.07 | 10.03 | 19.51 | 45.20 | 23.42 | 3-4 |
| static | 12 | 41.82 | 27 / 3 | 38.74 | 9.58 | 21.39 | 39.33 | 29.73 | 2-3 |
| static | 16 | 37.73 | 27 / 3 | 42.94 | 9.24 | 19.88 | 35.05 | 29.75 | 1-2 |
| static | 20 | 38.19 | 27 / 3 | 42.42 | 11.37 | 23.60 | 36.24 | 30.31 | 1-2 |
| static | 24 | 28.63 | 27 / 3 | 56.58 | 10.62 | 21.44 | 26.78 | 20.88 | 1-2 |
| static | 30 | 30.36 | 27 / 3 | 53.36 | 11.96 | 24.37 | 28.35 | 22.07 | 1-1 |
| dynamic | 8 | 41.02 | 27 / 3 | 39.49 | 9.33 | 21.07 | 38.18 | 5.84 | 2-6 |
| dynamic | 16 | 32.39 | 27 / 3 | 50.02 | 9.64 | 22.64 | 30.37 | 17.88 | 1-3 |
| dynamic | 24 | 29.71 | 27 / 3 | 54.53 | 11.25 | 22.33 | 27.60 | 17.85 | 1-2 |
| dynamic | 30 | 29.55 | 27 / 3 | 54.82 | 12.20 | 22.51 | 27.27 | 21.39 | 1-1 |

All runs produced around `1.4G` total output and `77M` under `batch/worker_runs`, because successful worker scene folders are moved to the run root and child summaries are skipped.

## Interpretation

- Stable default choice: `static` with 16 workers is conservative and already gives strong speedup over 8 workers.
- Fastest observed choice: `static` with 24 workers was best in this 30-scene sweep.
- Upper bound from this test: 30 workers did not improve over 24 workers. It also increased per-task overhead and task P95, so full concurrency is not a better default.
- Dynamic scheduling is useful at moderate worker counts. It improved 8 workers from 47.55s to 41.02s and 16 workers from 37.73s to 32.39s by reducing worker idle time.
- Dynamic scheduling was not clearly better at high worker counts. At 24 workers, static was slightly faster than dynamic; at 30 workers both were close and slower than static 24.

## Recommendation

- For the 30-scene pilot, use `--workers 16 --scheduler static` as a conservative low-risk setting when sharing the machine with other heavy jobs.
- For the 30-scene pilot, `--workers 24 --scheduler static` was the fastest observed setting.
- A longer 90-scene follow-up below shows that the pilot was too short to judge dynamic or hybrid scheduling at higher worker counts.
- Re-run a smaller pilot before very large production jobs if the template, machine load, or label/floorplan settings change materially.

## 90-Scene Follow-Up

The 30-scene run was too short for workers above 16 because each worker received only one or two scenes. A second sweep used 90 scenes from the same sequential Front3D queue and the same config.

| Scheduler | Workers | Wall Time (s) | Success / Fail | Success/min | Task Mean (s) | Task P95 (s) | Worker Max (s) | Worker Imbalance (s) | Task Range | Stolen Tasks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| static | 8 | 162.62 | 79 / 11 | 29.15 | 12.27 | 19.38 | 157.31 | 38.56 | 11-12 | 0 |
| static | 16 | 111.15 | 79 / 11 | 42.65 | 13.86 | 22.19 | 105.29 | 51.60 | 5-6 | 0 |
| static | 24 | 95.05 | 79 / 11 | 49.87 | 17.28 | 27.20 | 90.32 | 51.54 | 3-4 | 0 |
| static | 32 | 88.80 | 79 / 11 | 53.38 | 18.90 | 28.17 | 81.40 | 57.69 | 2-3 | 0 |
| dynamic | 8 | 130.94 | 79 / 11 | 36.20 | 10.30 | 17.41 | 124.91 | 14.70 | 10-13 | 0 |
| dynamic | 16 | 95.26 | 79 / 11 | 49.76 | 13.94 | 24.41 | 89.86 | 17.42 | 4-7 | 0 |
| dynamic | 24 | 90.33 | 79 / 11 | 52.47 | 18.02 | 27.29 | 84.69 | 31.10 | 2-5 | 0 |
| dynamic | 32 | 76.62 | 79 / 11 | 61.86 | 18.08 | 29.15 | 64.53 | 28.55 | 1-4 | 0 |
| hybrid | 8 | 121.59 | 79 / 11 | 38.98 | 10.07 | 16.07 | 117.56 | 8.33 | 9-13 | 5 |
| hybrid | 16 | 98.33 | 79 / 11 | 48.21 | 14.38 | 23.94 | 91.79 | 17.53 | 4-7 | 7 |
| hybrid | 24 | 82.27 | 79 / 11 | 57.62 | 17.80 | 28.14 | 74.72 | 16.41 | 2-5 | 8 |
| hybrid | 32 | 74.06 | 79 / 11 | 64.00 | 18.57 | 31.25 | 67.58 | 29.56 | 1-4 | 6 |

The 11 failed scenes were identical across all 90-scene runs: `front3d_0020`, `front3d_0026`, `front3d_0027`, `front3d_0034`, `front3d_0042`, `front3d_0047`, `front3d_0050`, `front3d_0059`, `front3d_0071`, `front3d_0076`, and `front3d_0084`. This indicates the failures were data/precheck issues rather than worker-count instability.

## Hybrid Scheduler

Hybrid scheduling keeps the initial static sharding, then switches to work stealing only at the tail:

1. Each worker first consumes its own fixed shard queue.
2. When a worker's own queue is empty, it checks whether any other worker queue still has pending tasks.
3. If so, it steals one task from the queue with the largest remaining size.
4. Each stolen claim is recorded in `batch/logs/events.jsonl` as `task_claimed` with `stolen: true` and `source_worker_id`.

This made the long-queue case faster than both static and dynamic in the 90-scene sweep. `--scheduler hybrid` is therefore the default batch scheduler after this test.

Updated recommendation:

- Stable production setting: `--workers 24 --scheduler hybrid`.
- Maximum-throughput setting observed in this sweep: `--workers 32 --scheduler hybrid`.
- Conservative setting for shared/high-load machines: `--workers 16 --scheduler hybrid`.
- Keep `static` for strict reproducibility/debugging of fixed shard assignment.
- Keep `dynamic` for comparison and for queues where full shared scheduling is desired from the start.
