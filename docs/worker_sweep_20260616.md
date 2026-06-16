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

- Use `--workers 16 --scheduler static` as the stable production default when sharing the machine with other heavy jobs.
- Use `--workers 24 --scheduler static` as the current maximum-throughput setting when the machine is not under severe memory or I/O pressure.
- Keep dynamic scheduling as an optional strategy, especially for medium worker counts or mixed-duration scene queues, but do not make it the default based on this test.
- Re-run a smaller pilot before very large production jobs if the template, machine load, or label/floorplan settings change materially.
