# SceneGen TODO Index

这里是 SceneGen 的唯一活跃 TODO 入口。条目按类别维护在同目录下，旧的 `docs/todo.md` 只保留跳转说明。

## Active Counts

| Category | File | Active Count | Scope |
|---|---|---:|---|
| Feature | [feature.md](feature.md) | 7 | 新能力、算法和实验功能。 |
| Structure | [structure.md](structure.md) | 6 | 架构、配置、输出契约和主流程整合。 |
| Performance | [performance.md](performance.md) | 3 | 运行速度、并行、I/O 和体积评估。 |

Total active TODOs: 16

## Priority Overview

| Rank | ID | Category | TODO | Summary |
|---:|---|---|---|---|
| 1 | STR-MAPDATA-001 | Structure | 将派生 map 和视觉数据集构建接入主系统 | 把当前独立脚本升级为可配置、可恢复、可合并的生产流程。 |
| 2 | STR-MASK-001 | Structure | 统一 label 与 class mask 的中间 mask | 避免开口、墙体、家具和 free-space 判定在不同模块中漂移。 |
| 3 | PERF-DATASET-001 | Performance | 优化 map/dataset 生产调度 | 给大规模派生 map 和数据集构建建立 worker、I/O、resume 策略。 |
| 4 | FEAT-LABEL-002 | Feature | 增加 LoS/NLoS 点位验证 | 为 BS/UE 点位输出传播可见性统计，辅助筛选定位实验样本。 |
| 5 | FEAT-FRONT3D-001 | Feature | 基于 3D-FRONT 资产池随机生成场景 | 从复现已有 3D-FRONT 组合扩展到随机生成多样场景。 |

## Maintenance Rules

- 新 TODO 先查重，再放入最匹配的类别文件。
- 稳定 ID 不重排；只调整 Priority List 顺序。
- 完成的条目从 active 列表移除，并在 [history.md](history.md) 记录。
- 大规模生产、输出格式、配置字段或脚本入口变化时，同步更新本目录和相关 README。
