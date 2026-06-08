# Performance TODO

本文件记录运行速度、内存、I/O、并行调度和大规模生产体积相关 TODO。

## Priority List

| Rank | ID | TODO | Summary |
|---:|---|---|---|
| 1 | PERF-DATASET-001 | 优化 map/dataset 生产调度 | 为大规模派生 map 和数据集构建确定 worker、I/O、resume 策略。 |
| 2 | PERF-GEO-001 | 增加几何投影空间加速 | 用 tile index、BVH 或候选面裁剪减少投影耗时。 |
| 3 | PERF-GEO-002 | 建立全量生产性能基准 | 按面积、三角面数量、家具密度和输出配置统计耗时与体积。 |

## Details

### PERF-DATASET-001: 优化 map/dataset 生产调度

Goal: 让 derived maps 和 vision dataset 构建在大规模 run 上可预测、可恢复、可观测。

Affected modules: `src/scenegen/postprocess/`、`src/scenegen/batch.py`、`scripts/generate_derived_maps.py`、`scripts/build_vision_dataset.py`。

Acceptance: 明确 worker 建议、I/O 压力指标、resume 行为、失败重试策略和耗时统计；生成和 build 阶段都能在日志中看到进度和 ETA 所需字段。

Notes: 当前 batch 已支持可配置 postprocess、derived maps 多 worker、resume 和阶段日志；后续重点是 worker 建议、I/O 压力指标、ETA 和大规模性能报告。

### PERF-GEO-001: 增加几何投影空间加速

Goal: 降低 `ray_height_filtered`、mesh furniture mask 和未来 ray casting backend 的几何处理成本。

Affected modules: floorplan projection、class mask furniture projection。

Acceptance: 按 XY tile 建三角面索引，或引入稳定 BVH；大场景中候选三角面数量随局部区域缩小，而不是每次处理全量 mesh。

Notes: 降低分辨率不一定能显著加速，因为当前主要成本经常在三角面遍历。

### PERF-GEO-002: 建立全量生产性能基准

Goal: 在不同面积、不同三角面数量、不同家具密度和不同输出配置上记录生产性能。

Affected modules: production logs、statistics、analysis scripts。

Acceptance: 每个场景记录 floorplan、class mask、label、derived maps、dataset copy 等阶段耗时；汇总输出平均、P50/P90/P99、失败原因和输出体积。

Notes: 这会帮助决定默认 projection、label variants、mask 配置和 worker 数。
