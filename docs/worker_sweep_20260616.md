# Worker 数量与调度策略测试记录（2026-06-16）

本文记录当前 `front3d_full_simulation` 任务模板下的 batch worker 数量和调度策略测试结果。

## 测试设置

- 分支：`codex/batch-worker-performance`
- 配置：`config/tasks/front3d_full_simulation.yaml`
- 场景选择：按 3D-FRONT 顺序队列从头开始
- 重试：`--max-retries 0`
- 输出根目录：`results`
- 测试前机器状态：112 个逻辑 CPU，存在较高后台负载，swap 接近满
- 已知数据失败：30-scene 测试中 `front3d_0020`、`front3d_0026`、`front3d_0027` 稳定失败

代表命令：

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

## 30-Scene 测试结果

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

所有 run 总输出约 `1.4G`，`batch/worker_runs` 约 `77M`。这是因为成功 scene 会从 worker 子目录 move 到 run 根目录，并且 child summary 已经跳过。

## 30-Scene 结论

- 保守默认可选：`static 16 workers`，已经明显快于 8 workers。
- 30-scene 内最快：`static 24 workers`。
- 30 workers 不比 24 workers 更快，说明已经出现一定资源争用。
- dynamic 在中等 worker 数有价值，例如 8 workers 从 `47.55s` 降到 `41.02s`，16 workers 从 `37.73s` 降到 `32.39s`。
- dynamic 在高 worker 数下优势不明显。24 workers 时 static 略快；30 workers 时两者接近。

不过，30-scene 测试过短。16+ workers 时，每个 worker 只分到一两个 scene，因此它不足以判断高 worker 数下的调度策略。

## 90-Scene 追加测试

为了更真实地观察长队列尾部问题，后续使用同一配置做了 90-scene sweep。

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

90-scene 中 11 个失败 scene 在所有 run 中完全一致：

- `front3d_0020`
- `front3d_0026`
- `front3d_0027`
- `front3d_0034`
- `front3d_0042`
- `front3d_0047`
- `front3d_0050`
- `front3d_0059`
- `front3d_0071`
- `front3d_0076`
- `front3d_0084`

这说明失败主要来自数据/precheck，而不是 worker 数或调度策略导致的不稳定。

## Hybrid 调度策略

Hybrid 调度是“静态分片 + 尾部偷任务”：

1. 开始时仍然按 static 分片：`shard_id = plan_index % workers`。
2. 每个 worker 优先消费自己的固定队列。
3. 当某个 worker 自己队列清空时，检查其他 worker 队列是否仍有待处理任务。
4. 如果存在，就从剩余任务最多的队列偷取 1 个任务。
5. 每次偷任务都会在 `batch/logs/events.jsonl` 的 `task_claimed` 事件中记录 `stolen: true` 和 `source_worker_id`。

这个策略的目的不是一开始就全局抢任务，而是在尾部出现 worker 空闲时自动修补静态分片的长尾。

90-scene 测试中，hybrid 在长队列下快于 static 和 dynamic，因此测试后将 `--scheduler hybrid` 设为 batch 默认策略。

## 当前建议

- 稳定生产设置：`--workers 24 --scheduler hybrid`
- 本轮 sweep 中观察到的最大吞吐设置：`--workers 32 --scheduler hybrid`
- 共享机器或高负载时的保守设置：`--workers 16 --scheduler hybrid`
- 需要严格复现固定分片或 debug worker 分片时使用 `--scheduler static`
- 需要全局共享队列对照时使用 `--scheduler dynamic`
- 如果任务模板、机器负载、label/floorplan 配置发生明显变化，正式大批量生产前应重新跑一小轮 30-90 scene pilot
