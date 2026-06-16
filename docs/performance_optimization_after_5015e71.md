# `5015e71` 之后的性能优化复盘

本文记录 commit `5015e71d4e1f1165ce73672ab3337eb23c63d699`（`Refresh project TODO backlog`）之后完成的性能优化工作。它是一个可长期保留的复盘文档，因为中间产生的大量 `results/` 调试目录后续可能会被清理。

## 范围与基准

单场景主基准使用最初性能探针里的同一个 3D-FRONT 场景：

- scene id: `fff98d42-99a4-43fc-9639-5761cb4f87df`
- mode: `front3d`
- label variants: `panel` 和 `walk`
- label grid: `0.1`、`0.2`、`0.4`、`0.5`
- label mask resolution: `0.05m`
- floorplan: geometry `sampling`，`resolution_m=0.05`，高度 `1.6m`
- class mask: enabled，家具模式 `mesh`

初始基准 run：

- run: `debug_label_subtiming_0p05_visual_6812_20260616`
- 总耗时：`63.11s`
- 主要瓶颈：`label=40.37s`，其中 `center_bs=29.28s`

当前对比 run：

- run: `perf_doc_current_6812_20260616`
- 总耗时：`6.75s`
- 使用同一 scene 和同样 8 个 label variant

## 总览

| 阶段 | 基线 (s) | 当前 (s) | 加速比 | 降幅 |
| --- | ---: | ---: | ---: | ---: |
| total_run | 63.11 | 6.75 | 9.35x | 89.3% |
| build_scene | 3.16 | 2.48 | 1.27x | 21.5% |
| label | 40.37 | 0.87 | 46.64x | 97.9% |
| floorplan | 18.62 | 2.65 | 7.02x | 85.8% |
| floorplan_geometry | 18.62 | 2.07 | 9.01x | 88.9% |
| class_mask | n/a | 0.58 | n/a | n/a |
| label_overlay | 0.64 | 0.35 | 1.81x | 44.6% |

最大的收益来自：

1. 把重复的逐 variant label 工作改为缓存化、全局化、向量化的采样和分配。
2. 复用生成场景时已经得到的 mesh arrays，并限制 floorplan 采样上限。
3. 向量化 class mask 和家具 mesh mask 渲染。
4. 降低 PNG 压缩等级，减少写图耗时。
5. 增加 batch 级 worker 调度、move 发布、跳过 child summary、hybrid work stealing。

## 阶段一：计时探针与基线定位

第一步不是直接优化，而是补全观测能力。

新增或强化的记录包括：

- scene 级 `timings_s`
- label 各 variant 和子阶段耗时
- floorplan/class mask 内部耗时
- build scene 中 OBJ/Sionna asset 输出耗时
- batch child subprocess overhead 和 publish time

初始 label 子阶段耗时：

| Label 子阶段 | 基线 (s) |
| --- | ---: |
| build_contexts | 0.84 |
| global_sampling | 0.46 |
| assign_points | 6.46 |
| center_bs | 29.28 |
| build_groups_validate | 2.85 |
| write_outputs | 0.43 |
| total_variant | 40.34 |

这个基线明确说明：label 的核心瓶颈是 `center_bs` 和点位分配；第二大瓶颈是 floorplan geometry。

## 阶段二：Label 管线优化

主要优化：

- 每个 scene/config 只构建一次固定分辨率 global sampling mask，所有 label variant 复用。
- 全局候选点生成和 mask 查询改成向量化。
- 房间归属和点位分配从重复扫描改成缓存化、向量化分配。
- 优化 `center_bs`，避免每个 variant 重复扫描完整高密度点集。
- 减少重复 label payload 和 report 构造。
- 保留 `panel`/`walk` 区分，但共享上游 mask 和基础计算。

label 子阶段优化结果：

| Label 子阶段 | 基线 (s) | 当前 (s) | 加速比 | 降幅 |
| --- | ---: | ---: | ---: | ---: |
| build_contexts | 0.842 | 0.089 | 9.5x | 89.5% |
| global_sampling | 0.460 | 0.254 | 1.8x | 44.7% |
| assign_points | 6.465 | 0.151 | 42.8x | 97.7% |
| center_bs | 29.276 | 0.053 | 548.2x | 99.8% |
| build_groups_validate | 2.849 | 0.204 | 14.0x | 92.8% |
| write_outputs | 0.426 | 0.097 | 4.4x | 77.3% |
| total_variant | 40.342 | 0.861 | 46.8x | 97.9% |

当前各 label variant 的代表耗时：

| Variant | 当前 (s) |
| --- | ---: |
| label_panel_0p1 | 0.481 |
| label_panel_0p2 | 0.076 |
| label_panel_0p4 | 0.040 |
| label_panel_0p5 | 0.034 |
| label_walk_0p1 | 0.164 |
| label_walk_0p2 | 0.044 |
| label_walk_0p4 | 0.013 |
| label_walk_0p5 | 0.009 |

解释：

- 高密度 `0.1m` variant 仍然是 label 里最重的部分，但已经降到 1 秒以内。
- 在这个场景中，`walk` 比 `panel_0p1` 更快，因为 expensive 的 global mask 和 BS 工作已经共享，障碍物过滤也已向量化。

## 阶段三：Floorplan Geometry 优化

当前生产默认配置：

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

主要优化：

- 删除/关闭默认不再需要的输出，例如 semantic floorplan、clean geometry、`geometry_raw.png`。
- 默认只生成一个显式高度层：`floorplan_1p60.png`。
- 复用写 `scene.obj` 时产生的内存态 `SceneMeshArrays`，避免第二次完整解析 OBJ。
- 默认模板下将高密度采样上限限制到 `4,000,000` 点。
- 用 numpy 面积加权、分块采样替代较慢的表面采样路径。
- 向量化 rasterize 和 pixel minimum-height map 构建。
- 降低 floorplan PNG 压缩等级，减少写图耗时。

当前 benchmark scene 的 floorplan geometry 内部耗时：

| 子阶段 | 当前 (s) |
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

说明：

- `ray_height_filtered` 投影已经实现并测试过，但不是当前生产默认策略。
- 当前默认仍是高密度 `sampling`，因为优化后速度可接受、视觉效果稳定，并且兼容已有下游流程。

## 阶段四：Class Mask 与家具 Mesh Mask

当前默认配置：

```yaml
floorplan:
  class_mask:
    enabled: true
    wall_dilation_m: 0.0
    furniture_dilation_m: 0.05
    furniture_mode: mesh
    furniture_height_m: 1.6
```

主要优化：

- 家具 mask 从仅支持 bbox 扩展到 mesh footprint，同时保留 `furniture_mode: bbox | mesh`。
- 对家具投影三角面做高度过滤和 rasterize。
- 增加 face/triangle cache，并过滤重复投影 primitive。
- 用数组友好的方式替代大量 set 操作。
- door/opening 逻辑与 label/class mask 共用。

当前 benchmark scene 的 class mask 内部耗时：

| 子阶段 | 当前 (s) |
| --- | ---: |
| read_inputs | 0.060 |
| prepare_canvas | 0.000 |
| draw_architecture | 0.112 |
| process_openings_walls | 0.000 |
| build_furniture_mask | 0.397 |
| compose_layers | 0.000 |
| write_outputs | 0.005 |
| class_mask total | 0.580 |

当前家具 mask 细节：

- 家具对象数：`19`
- mesh 对象数：`19`
- 三角面数量：`181001`
- 实际绘制投影三角面：`32693`
- 跳过的重复投影三角面：`148289`

## 阶段五：Build Scene 与 Sionna Assets

主要优化：

- 用 numpy 向量化 Front3D 对象 transform。
- 降低合并 `scene.obj` 时的转换开销。
- 为 floorplan 复用 mesh arrays。
- 为单材质 Sionna asset export 增加 fast path。
- 记录 `write_scene_obj`、`write_sionna_assets`、`write_placements_json` 的耗时。

当前 benchmark scene 的 build timings：

| Build 子阶段 | 当前 (s) |
| --- | ---: |
| write_scene_obj | 0.757 |
| write_sionna_assets | 1.022 |
| write_placements_json | 0.007 |
| build_scene total | 2.481 |

build scene 的加速幅度没有 label/floorplan 大，因为它不是最初最大的瓶颈。不过在大规模 batch 中，`write_sionna_assets` 仍然是值得继续关注的固定成本。

## 阶段六：Label Overlay 与 PNG IO

主要优化：

- 降低 floorplan 和 overlay PNG 的压缩等级。
- 避免 overlay 渲染时构造过大的 payload。
- 缓存/复用 overlay 所需的位置数组。

结果：

| 阶段 | 基线 (s) | 当前 (s) | 加速比 |
| --- | ---: | ---: | ---: |
| label_overlay | 0.637 | 0.353 | 1.81x |

这部分绝对耗时不如 label/floorplan 大，但每个 label variant 都可能生成可视化图片，因此对大批量生产仍有意义。

## 阶段七：Batch 与多 Worker 生产

batch 优化包括：

- 增加 worker 级日志、JSONL event log、state table、retry/dead-letter queue 和 traceback。
- 增加 `task_subprocess`、`publish_scene` 和 child-run timing 记录。
- 成功 scene 发布从 `copytree` 改为 `move`。
- child run 跳过 `summary/` 生成，只在顶层 batch run 统一构建 summary。
- 增加 `static`、`dynamic`、`hybrid` 三种调度策略。
- 90-scene sweep 后，将 `hybrid` 设为默认调度策略。

30-scene 发布/存储对比：

| Run | Scheduler | Wall Time (s) | Success / Fail | Total Size | worker_runs Size | 说明 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| batch_static30_round2_20260616 | static | 48.13 | 27 / 3 | 2.8G | 1.5G | 旧 copytree 行为 |
| batch_static30_skipchildsummary_20260616 | static | 44.09 | 27 / 3 | 1.4G | 76M | move publish + 跳过 child summary |
| batch_static30_current_20260616 | static | 44.10 | 27 / 3 | 1.4G | 77M | 当前 timing 字段已验证 |

batch 最大收益是存储和 IO 压力降低：成功 scene 不再完整重复保存在 `batch/worker_runs` 下。

## 90-Scene 调度测试

30-scene worker sweep 对高 worker 数不够充分，因为 16+ worker 时每个 worker 只分到一两个任务。后续用同样配置跑了 90-scene sweep，比较 static、dynamic、hybrid。

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

11 个失败 scene 在所有调度/worker 设置下完全一致，说明这些失败来自数据或 precheck，而不是 worker 数量导致的不稳定。

Hybrid 策略：

1. 初始仍按 static 分片：`shard_id = plan_index % workers`。
2. worker 优先消费自己的队列。
3. 自己队列清空后，如果其他队列仍有任务，就从剩余任务最多的队列偷取 1 个任务。
4. 偷任务事件会写入 `batch/logs/events.jsonl`，包含 `stolen: true` 和 `source_worker_id`。

当前建议：

- 稳定生产：`--workers 24 --scheduler hybrid`
- 本轮测试观察到的最大吞吐：`--workers 32 --scheduler hybrid`
- 共享机器/高负载保守设置：`--workers 16 --scheduler hybrid`
- 保留 `static` 用于严格固定分片 debug
- 保留 `dynamic` 用于对照或需要全局共享队列的场景

## 优化后的当前生产默认

完整 Front3D 任务模板目前代表后续大规模仿真的默认设置：

- label: panel + walk
- label grids: `0.1`、`0.2`、`0.4`、`0.5`
- label mask resolution: `0.05m`
- UE wall clearance: `0.2m`
- UE furniture clearance: `0.1m`
- center BS: enabled
- floorplan geometry: `sampling`
- floorplan resolution: `0.05m`
- class mask: enabled
- furniture mask: `mesh`
- furniture dilation: `0.05m`
- batch scheduler: `hybrid`

## 复现命令

当前单场景 benchmark：

```bash
uv run scenegen \
  --config config/tasks/front3d_full_simulation.yaml \
  --set pipeline.scenes=1 \
  --set pipeline.run_name=perf_doc_current_6812_20260616 \
  --set pipeline.clean=true \
  --set 'front3d.scene_ids=[fff98d42-99a4-43fc-9639-5761cb4f87df]'
```

90-scene hybrid sweep 示例：

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

## 注意事项

- 单场景耗时只代表一个典型 3D-FRONT scene。不同 scene 会受到物体数量、房间布局、mesh 复杂度和 label 点数量影响。
- 中间结果目录不是 source of truth，可以清理；本文保留关键测量结果。
- `ray_height_filtered` floorplan 投影仍可使用，但不是当前生产默认策略。
- 当前单场景瓶颈已经不再是 label，剩余较大成本主要是 scene/Sionna asset 写出和 floorplan sampling/class-mask 渲染。
- 单 scene 内部目前没有开启并行；主要并行发生在 batch worker 层。
