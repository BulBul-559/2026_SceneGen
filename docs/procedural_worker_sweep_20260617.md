# Procedural Front3D Worker Sweep（2026-06-17）

本文记录 `procedural_front3d` 自动随机生成模式在正式仿真模板下的 hybrid worker 扫参结果，并和此前直接合成 `front3d` 的 batch 基准做时间对比。

## 测试设置

- 分支：`codex/procedural-scene-generation`
- 版本：`SceneGen 4.0.0`
- 配置：`config/tasks/procedural_front3d_full_simulation.yaml`
- 调度：`--scheduler hybrid`
- batch 重试：`--max-retries 0`
- 输出根目录：`results`，实际指向 `/data/sunmeiyuan/projects/SceneGen`
- 小规模组：30 scenes，workers = 1 / 2 / 4 / 8
- 大 worker 组：90 scenes，workers = 8 / 16 / 24 / 32
- 当前机器：112 logical CPUs，测试时 load average 约 `10.27 / 23.83 / 20.78`，内存可用约 `361GiB`，swap 接近满，`/data` 可用约 `5.7T`

任务模板保持正式生产设置：label、geometry sampling floorplan、class mask、mesh furniture mask 和 visual index 均开启；`label.ue.sampling.grid_m` 使用 `[0.1, 0.2, 0.4, 0.5]`，`label.ue.sampling.strategies` 使用 `[panel, walk]`。

代表命令：

```bash
uv run scenegen-batch \
  --config config/tasks/procedural_front3d_full_simulation.yaml \
  --workers 32 \
  --scheduler hybrid \
  --max-retries 0 \
  --set pipeline.scenes=90 \
  --set pipeline.run_name=procedural_hybrid_w32_90_20260617 \
  --set pipeline.clean=true
```

## Procedural Front3D Hybrid 结果

| Scenes | Workers | Wall Time (s) | Success / Fail | Success/min | Task Mean (s) | Task P95 (s) | Worker Max (s) | Worker Imbalance (s) | Worker Task Range | Stolen Tasks |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|
| 30 | 1 | 337.45 | 29 / 1 | 5.16 | 11.10 | 19.03 | 333.04 | 0.00 | 30-30 | 0 |
| 30 | 2 | 189.34 | 29 / 1 | 9.19 | 12.36 | 19.84 | 186.50 | 2.13 | 15-15 | 0 |
| 30 | 4 | 101.11 | 29 / 1 | 17.21 | 12.94 | 23.27 | 97.79 | 2.35 | 7-8 | 1 |
| 30 | 8 | 66.15 | 29 / 1 | 26.30 | 15.66 | 22.39 | 64.32 | 13.04 | 3-4 | 2 |
| 90 | 8 | 236.42 | 88 / 2 | 22.33 | 17.45 | 35.28 | 211.13 | 38.95 | 8-13 | 4 |
| 90 | 16 | 191.37 | 88 / 2 | 27.59 | 24.21 | 46.11 | 183.48 | 79.62 | 4-7 | 7 |
| 90 | 24 | 176.45 | 88 / 2 | 29.92 | 31.14 | 61.33 | 145.01 | 45.58 | 2-5 | 9 |
| 90 | 32 | 139.83 | 88 / 2 | 37.76 | 31.83 | 55.51 | 130.99 | 78.96 | 2-4 | 9 |

失败 scene 在不同 worker 数下稳定一致：

- 30-scene 组：`procedural_front3d_0012`
- 90-scene 组：`procedural_front3d_0012`、`procedural_front3d_0085`

这说明失败更像是确定性的 seed / precheck / 生成质量问题，不是 worker 并发导致的不稳定。因为本轮 `--max-retries 0`，任一 scene 失败都会让 batch 命令返回非零，但成功场景、manifest 和统计仍完整写出。

## 与直接 Front3D 合成对比

直接 `front3d` 数据来自 `docs/worker_sweep_20260616.md` 中同为 90-scene、hybrid scheduler 的结果。两组任务模板都打开正式 label/floorplan/class mask 链路，但 `procedural_front3d` 额外包含户型生成、建筑生成、语义 room profile、家具筛选、摆放和 precheck。

| Workers | Front3D Wall Time (s) | Procedural Wall Time (s) | Procedural / Front3D | Delta (s) |
|---:|---:|---:|---:|---:|
| 8 | 121.59 | 236.42 | 1.94x | +114.83 |
| 16 | 98.33 | 191.37 | 1.95x | +93.04 |
| 24 | 82.27 | 176.45 | 2.14x | +94.18 |
| 32 | 74.06 | 139.83 | 1.89x | +65.77 |

当前随机生成模式约为直接合成 `front3d` 的 `1.9x - 2.1x` 耗时。这是合理的：`front3d` 主要复现已有场景组合，`procedural_front3d` 还要在线生成户型和家具摆放。

## 观察

- 30-scene 小规模下，worker 从 1 增加到 8，墙钟从 `337.45s` 降到 `66.15s`，吞吐从 `5.16` 提升到 `26.30` success/min。
- 90-scene 中，8 -> 16 -> 24 worker 仍有收益，但收益逐渐变小：`236.42s` -> `191.37s` -> `176.45s`。
- 32 worker 在本轮最快：`139.83s`，约 `37.76` success/min。
- worker 增加后，单 task mean 明显变长，说明并行带来了 CPU/I/O 争用：90-scene 组从 8 worker 的 `17.45s` 增到 32 worker 的 `31.83s`。
- hybrid 的偷任务在 4 worker 以上开始出现；90-scene 组中 24/32 worker 都偷了 9 个任务，说明尾部动态修补是有效的。
- 每个 30-scene run 约 `1.3G`，每个 90-scene run 约 `3.8G`。按当前模板粗略估算，成功场景平均占用约 `43M`。

## 建议

- 当前机器空闲、需要最快完成时：`--workers 32 --scheduler hybrid`。
- 稳定大规模生产默认建议：`--workers 24 --scheduler hybrid`。24 worker 比 16 worker 快，但资源压力低于 32 worker。
- 共享机器或后台负载高时：`--workers 16 --scheduler hybrid`。
- 快速试跑和 QA：`--workers 8 --scheduler hybrid`，配合 30-90 scenes 检查 visual index、失败率和 report。

如果后续打开 `postprocess.maps` / dataset 构建，或切换到 `floorplan.geometry.projection: ray_height_filtered`，需要重新做 sweep；这些阶段会改变 CPU/I/O 比例。

按 90-scene 32-worker 结果估算，在当前模板和机器状态下：

- 2000 个成功场景约 `53` 分钟量级，不含失败补齐余量。
- 6000 个成功场景约 `2.6` 小时量级，不含失败补齐余量。

考虑本轮失败率约 `2.2%`，正式生产应预留额外 scene 数或开启合适的补齐策略，并用 `procedural_report.json` 抽查失败原因。
