# Structure TODO

本文件记录架构、配置、输出契约、数据流和主系统整合相关 TODO。

## Priority List

| Rank | ID | TODO | Summary |
|---:|---|---|---|
| 1 | STR-MASK-001 | 统一 label 与 class mask 的中间 mask | 让 indoor、wall、furniture、opening、free-space 判定共享同一套结果。 |
| 2 | STR-DATASET-001 | 完善正式数据集合并与重编号流程 | 将现有 merge 工具升级为更完整的数据集管理能力。 |
| 3 | STR-GEO-001 | 拆分 floorplan 分层投影 | 输出 architecture、wall、floor、furniture 等可复用层。 |
| 4 | STR-QUALITY-001 | 建立数据质量分级 | 在 precheck 之外输出 usable、needs_review、bad_* 等等级。 |
| 5 | STR-TEST-001 | 增加 floorplan/label 回归基准 | 用 fixture 和小 golden image/hash 防止重构改变语义。 |

## Details

### STR-MASK-001: 统一 label 与 class mask 的中间 mask

Goal: 把 label free-space 采样、class mask、opening/door 处理和 furniture mask 投影共享的中间结果抽象出来。

Affected modules: label 生成、class mask 生成、floorplan/opening 配置、Front3D source。

Acceptance: 同一场景中 label 与 class mask 使用相同的 indoor、wall、opening、furniture 和 free-space 基础 mask；panel/walk 只在明确的策略步骤上分叉。

Notes: 这可以减少“平面图看起来是门/墙，但 label 采样判断不同”的语义漂移。

### STR-DATASET-001: 完善正式数据集合并与重编号流程

Goal: 将现有 `scripts/merge_vision_datasets.py` 从单次工具升级为更完整的数据集管理能力，支持正式生产、补跑、质量过滤和可追溯重编号。

Affected modules: `scripts/merge_vision_datasets.py`、dataset builder、manifest schema、生产任务文档、未来 dataset management stage。

Acceptance: 可以声明多个 source dataset、按优先级补足目标数量、按质量等级或异常报告过滤、输出连续编号 `front3d_0000...front3d_NNNN`；每个样本 metadata 保留 source dataset、source scene、source scene id 和过滤原因；合并报告统计去重、跳过和补齐数量。

Notes: 当前已有独立 merge 脚本并已用于构建 3000 compact dataset；后续重点是把规则配置化、支持多 source 和质量过滤，而不是重新实现基础复制/重编号。

### STR-GEO-001: 拆分 floorplan 分层投影

Goal: 不再只对最终 `scene.obj` 做整体投影，而是将 architecture、wall、floor、furniture 等层明确拆分。

Affected modules: floorplan 生成、class mask、derived maps、dataset builder。

Acceptance: 下游可以直接读取分层 mask 或 projection meta；class mask 和 derived maps 能复用这些层，减少重复投影。

Notes: 这也有助于后续生成像素级家具 mask 和更精细的传播监督。

### STR-QUALITY-001: 建立数据质量分级

Goal: 在 precheck 成功/失败之外，输出更细的质量等级和原因标签。

Affected modules: `quality_report.json`、batch status、dataset builder。

Acceptance: 至少支持 `usable`、`needs_review`、`bad_geometry`、`bad_label`、`bad_material` 等等级；dataset build 可以按质量等级过滤。

Notes: 不必把所有 warning 都视为阻塞，重点是可筛选和可追踪；front3d 房间可用面积、拥挤度、可达性和 label/map 完整性可以作为质量特征。

### STR-TEST-001: 增加 floorplan/label 回归基准

Goal: 为 floorplan、class mask、label overlay 和 derived maps 增加稳定回归测试。

Affected modules: `tests/`、fixture 数据、CI/本地验证命令。

Acceptance: 覆盖薄墙、门洞、斜墙、楼梯、悬空物、天花板、高桌面等小 fixture；至少一部分输出有 golden hash 或结构化断言。

Notes: 不建议把大型真实 3D-FRONT 数据纳入 git；fixture 应保持小而可审计。
