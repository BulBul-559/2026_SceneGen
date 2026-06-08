# Feature TODO

本文件记录新能力、算法和实验功能。结构性整合和性能优化分别放在 `structure.md` 与 `performance.md`。

## Priority List

| Rank | ID | TODO | Summary |
|---:|---|---|---|
| 1 | FEAT-LABEL-002 | 增加 LoS/NLoS 点位验证 | 对 BS/UE 点位做轻量 ray test 或 map 级统计，辅助定位实验筛选。 |
| 2 | FEAT-FRONT3D-001 | 基于 3D-FRONT 资产池随机生成场景 | 从复现原始组合扩展到可控随机布局。 |
| 3 | FEAT-LABEL-001 | 扩展 BS 布点策略 | 增加 ceiling AP、wall-mounted AP、覆盖优化等策略。 |
| 4 | FEAT-RF-001 | 增加 RF proxy 派生监督图 | 在 LoS/wall-count 之外生成 wall thickness、proxy pathloss、material mask 等可选监督。 |
| 5 | FEAT-MAT-001 | 精细化电磁材质标注 | 利用 category、material、texture 信息提高 Sionna 材质置信度。 |
| 6 | FEAT-GEO-001 | 实现真实 top-down ray casting backend | 引入稳定 ray/BVH backend，支持逐像素 XY column 查询。 |
| 7 | FEAT-GEO-002 | 实现保守栅格化 | 降低细墙、斜墙、小三角在投影中的漏检概率。 |
| 8 | FEAT-DATASET-001 | 增加数据集划分脚本 | 为训练集生成 train/val/test split，保持和场景生成解耦。 |

## Details

### FEAT-LABEL-002: 增加 LoS/NLoS 点位验证

Goal: 为 label 中的 BS/UE 点位增加传播可见性统计，例如 LoS/NLoS 比例、每个 BS 覆盖的有效 UE 数量、跨房间可见性等。

Affected modules: `src/scenegen/labels.py`、`src/scenegen/postprocess/derived_maps.py`、`scripts/generate_derived_maps.py`。

Acceptance: 生成结果中能记录每个场景、每个 BS 或每个 label variant 的 LoS/NLoS 摘要；验证失败不应默认中断生成，但应进入质量报告。

Notes: 第一版可以复用派生 map 中的 LoS/wall-count 计算，不必引入 Sionna。

### FEAT-FRONT3D-001: 基于 3D-FRONT 资产池随机生成场景

Goal: 在现有 `front3d` 复现模式之外，新增基于 3D-FUTURE 资产池的随机生成模式。

Affected modules: `src/scenegen/sources.py`、`src/scenegen/front3d.py`、placement/quality 模块和配置模板。

Acceptance: 可以按房间类型、资产类别、碰撞和支撑规则随机摆放家具；输出仍保持 `scene.obj`、`scene.xml`、label、floorplan、class mask、manifest 等标准结构。

Notes: 需要继承现有 asset catalog/source adapter 抽象，不要把 3D-FRONT 特例散落到 CLI。

### FEAT-LABEL-001: 扩展 BS 布点策略

Goal: 在中心 BS 和现有 room wall/corner 之外，增加更丰富的室内 AP/基站布点策略。

Affected modules: label 生成模块、配置模板、label overlay 可视化。

Acceptance: 支持 ceiling AP、wall-mounted AP、按房间功能布点、按覆盖半径或几何中心优化布点；各策略可以和 `BS_CENTER` 同时启用。

Notes: 策略输出应保持 `bs_points`、`bs_positions` 和稳定 label 名称。

### FEAT-RF-001: 增加 RF proxy 派生监督图

Goal: 在当前 `geometry.npz` / `propagation.npz` 的 SDF、LoS、wall-count 之外，增加更接近无线传播规律的可选 proxy 监督。

Affected modules: `src/scenegen/postprocess/derived_maps.py`、class mask / material mask 输出、视觉数据集 schema。

Acceptance: 可配置生成 `wall_thickness_map`、`proxy_pathloss_map`、`material_mask` 等新 map；默认关闭，不破坏现有 compact dataset schema；metadata 记录 map 参数和类别/材质映射。

Notes: 第一版可以继续不调用 Sionna，基于 class mask、墙体厚度估计、BS 距离和材质类别构造轻量近似监督。

### FEAT-MAT-001: 精细化电磁材质标注

Goal: 提高 3D-FRONT/3D-FUTURE 资产和建筑结构的 Sionna 材质映射质量。

Affected modules: phase1 数据整理脚本、asset catalog、XML exporter、数据接入文档。

Acceptance: 输出材质映射包含可审计 confidence；低置信度类别可抽样复核；常见 wood、concrete、glass、metal、fabric、plastic 映射更稳定。

Notes: 不建议只靠名称硬猜，应保留人工校验入口。

### FEAT-GEO-001: 实现真实 top-down ray casting backend

Goal: 为 `floorplan.geometry.projection` 增加严格的逐像素垂直射线或 XY column 查询后端。

Affected modules: `src/scenegen/floorplan.py` 或未来几何投影子模块。

Acceptance: 对每个像素查询 `bottom_m <= z <= target_height` 范围内的命中；同一输入和配置多次输出完全一致；可用 fixture 验证不同高度下的占据变化。

Notes: 可评估 `trimesh.ray`、`embreex`、`pyembree` 或自建 BVH。

### FEAT-GEO-002: 实现保守栅格化

Goal: 减少当前三角形中心采样/线段兜底导致的细几何漏检。

Affected modules: floorplan projection、class mask furniture mesh projection。

Acceptance: 对小于一个像素宽度的墙、斜墙、窄门框等 fixture，输出不应出现明显断裂。

Notes: 可以先实现 triangle/pixel overlap 或 conservative rasterization，再考虑性能优化。

### FEAT-DATASET-001: 增加数据集划分脚本

Goal: 为构建好的视觉数据集生成可复现的 train/val/test 划分。

Affected modules: `scripts/` 下的数据集工具、未来 dataset manifest。

Acceptance: 支持固定 seed、按 source scene id 去重、按面积/房间数/长宽比分层抽样；输出 split 文件和摘要统计。

Notes: 仍保持生成逻辑和训练划分解耦。
