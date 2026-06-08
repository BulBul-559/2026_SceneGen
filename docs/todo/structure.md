# Structure TODO

本文件记录架构、配置、输出契约、数据流和主系统整合相关 TODO。

## Priority List

| Rank | ID | TODO | Summary |
|---:|---|---|---|
| 1 | STR-MAPDATA-001 | 将派生 map 和视觉数据集构建接入主系统 | 把当前独立脚本升级为 SceneGen 可配置生产阶段。 |
| 2 | STR-MASK-001 | 统一 label 与 class mask 的中间 mask | 让 indoor、wall、furniture、opening、free-space 判定共享同一套结果。 |
| 3 | STR-DATASET-001 | 增加正式数据集合并与重编号流程 | 合并主任务和 continue 任务，形成真实数量的数据集。 |
| 4 | STR-GEO-001 | 拆分 floorplan 分层投影 | 输出 architecture、wall、floor、furniture 等可复用层。 |
| 5 | STR-QUALITY-001 | 建立数据质量分级 | 在 precheck 之外输出 usable、needs_review、bad_* 等等级。 |
| 6 | STR-TEST-001 | 增加 floorplan/label 回归基准 | 用 fixture 和小 golden image/hash 防止重构改变语义。 |

## Details

### STR-MAPDATA-001: 将派生 map 和视觉数据集构建接入主系统

Goal: 把当前独立的 `scripts/generate_derived_maps.py`、`scripts/build_vision_dataset.py` 和 `scripts/visualize_derived_maps.py` 能力整合进 SceneGen 主生产系统。

Affected modules: `src/scenegen/batch.py`、`src/scenegen/cli.py`、配置模板、生产日志系统、`scripts/` 数据集工具。

Acceptance: 主系统支持可配置阶段，例如 `scenes -> derived_maps -> vision_dataset -> merge_dataset`；每个阶段都有 manifest、JSONL event log、resume 状态、失败队列和耗时统计；可以在一个任务中完成场景生成、map 生成、数据集构建，或从已有 run 恢复继续处理。

Notes: 当前 3000 主任务和 320 continue 任务的后处理仍依赖独立脚本。主系统整合时要保留脚本入口，方便离线修复和单独调试。

### STR-MASK-001: 统一 label 与 class mask 的中间 mask

Goal: 把 label free-space 采样、class mask、opening/door 处理和 furniture mask 投影共享的中间结果抽象出来。

Affected modules: label 生成、class mask 生成、floorplan/opening 配置、Front3D source。

Acceptance: 同一场景中 label 与 class mask 使用相同的 indoor、wall、opening、furniture 和 free-space 基础 mask；panel/walk 只在明确的策略步骤上分叉。

Notes: 这可以减少“平面图看起来是门/墙，但 label 采样判断不同”的语义漂移。

### STR-DATASET-001: 增加正式数据集合并与重编号流程

Goal: 将多个已构建的数据集目录合并成一个目标数量明确、编号连续、来源可回溯的正式数据集。

Affected modules: dataset builder、manifest schema、生产任务文档。

Acceptance: 可以优先取主 run 成功样本，再从 continue run 补足到指定数量；输出 `front3d_0000...front3d_NNNN` 连续编号；每个样本 metadata 保留 `source_run` 和 `source_scene`；合并前检查 source scene id 重复。

Notes: 当前 3000 主任务成功 2717 个，320 continue 成功数足够补齐 3000，但需要单独 merge/renumber。

### STR-GEO-001: 拆分 floorplan 分层投影

Goal: 不再只对最终 `scene.obj` 做整体投影，而是将 architecture、wall、floor、furniture 等层明确拆分。

Affected modules: floorplan 生成、class mask、derived maps、dataset builder。

Acceptance: 下游可以直接读取分层 mask 或 projection meta；class mask 和 derived maps 能复用这些层，减少重复投影。

Notes: 这也有助于后续生成像素级家具 mask 和更精细的传播监督。

### STR-QUALITY-001: 建立数据质量分级

Goal: 在 precheck 成功/失败之外，输出更细的质量等级和原因标签。

Affected modules: `quality_report.json`、batch status、dataset builder。

Acceptance: 至少支持 `usable`、`needs_review`、`bad_geometry`、`bad_label`、`bad_material` 等等级；dataset build 可以按质量等级过滤。

Notes: 不必把所有 warning 都视为阻塞，重点是可筛选和可追踪。

### STR-TEST-001: 增加 floorplan/label 回归基准

Goal: 为 floorplan、class mask、label overlay 和 derived maps 增加稳定回归测试。

Affected modules: `tests/`、fixture 数据、CI/本地验证命令。

Acceptance: 覆盖薄墙、门洞、斜墙、楼梯、悬空物、天花板、高桌面等小 fixture；至少一部分输出有 golden hash 或结构化断言。

Notes: 不建议把大型真实 3D-FRONT 数据纳入 git；fixture 应保持小而可审计。
