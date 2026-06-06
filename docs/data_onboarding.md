# SceneGen 新数据源接入说明

这份文档说明如何把新的资产数据源接入 SceneGen。当前 Bistro 已经使用统一资产契约；3D-FRONT 已完成第一阶段数据整理，并已接入 `front3d` 运行模式，用于复现和合成 3D-FRONT 原始已有组合场景。

后续如果要接入新的数据源，建议分成两层处理：先整理可审计的资产、建筑结构和材质标注，再实现 source adapter，把数据转换成 SceneGen 统一的 `base scene + placements + label + metadata` 输出。

## 资产契约

标准 catalog 位于 `data/catalogs/`，Bistro 当前文件是：

```text
data/catalogs/bistro.v1.json
```

catalog 是资产对象列表。每个资产对象只保留运行时真正需要的信息：

- `schema_version`: 当前为 `1.0`。
- `dataset`: 数据源名称，例如 `bistro`、`3d-front`。
- `id`: 稳定唯一 ID，建议与资产目录名一致。
- `name`: 人类可读名称。
- `export_name`: OBJ/XML 输出时使用的稳定名称。
- `files.obj`: repo-relative POSIX OBJ 路径。
- `files.preview`: repo-relative POSIX 预览图路径，可为空。
- `placement.class`: `table`、`seat`、`tabletop`、`floor` 或 `skip`。
- `placement.enabled`: 是否参与随机摆放。
- `placement.support`: 可放置支撑面，例如 `floor`、`table`、`counter`。
- `placement.weight`: 随机采样权重。
- `geometry.units`: 几何单位，当前使用 `meter`。
- `geometry.size`: 资产 AABB 尺寸，字段为 `x`、`y`、`z`。
- `geometry.bbox`: 归一化后局部坐标包围盒，字段为 `min`、`max`。
- `normalization`: 坐标系、落地、XY 原点和朝向归一化信息。
- `materials.sionna`: 资产可能使用的 Sionna 材质列表。
- `materials.source_to_sionna`: OBJ `usemtl` 到 Sionna 材质的映射。

所有路径必须是 repo-relative POSIX 路径，不写绝对路径，也不保留 Windows 路径。

## 接入步骤

1. 把原始资产转换到 SceneGen 可读取的本地目录，例如 `data/assets/<asset_id>/<asset_id>.obj`。
2. 统一坐标系，建议使用 `+X` right、`+Y` forward、`+Z` up。
3. 把资产几何归一化到米制单位。
4. 把局部 XY 原点固定到 bbox center。
5. 把最低点移动到 `z=0`，便于 floor/table/counter 支撑放置。
6. 计算 `geometry.size` 和 `geometry.bbox`。
7. 根据语义或尺寸规则写入 `placement.class`。
8. 从 OBJ 材质名或数据集语义中生成 `materials.source_to_sionna`。
9. 生成 `data/catalogs/<dataset>.v1.json`。
10. 如需兼容旧入口，可同步写一份到该数据源资产目录下的 manifest。

## 3D-FRONT 当前接入状态

3D-FRONT 当前已经按两层接入：

- 数据整理层：`tools/prepare_front3d_phase1.py` 从 3D-FUTURE 和 3D-FRONT 原始数据中整理出物体、建筑结构、单项 JSON、预览图和 manifest。
- 场景合成层：`front3d` 模式读取 `data/3D-Front/scenegen_manifest.json`，选择原始 3D-FRONT scene id，并合并建筑结构和室内家具实例，输出与 Bistro 一致的 `scene.obj`、`scene.xml`、`placements.json`、`label.json`、`floorplan/`、质量报告和统计报告。

默认配置中，建筑结构使用 `variant: normalized`，室内物体使用 `object_variant: raw`。原因是 3D-FRONT 原始家具位姿按 3D-FUTURE `raw_model.obj` 的真实尺寸设计；如果把家具切到 `normalized_model.obj`，容易出现尺寸不匹配、拥挤或物体跑出房间的问题。

`front3d` 默认还会执行两个轻量修正：

- `front3d.positive_xy: true`: 将合成结果整体平移到 XY 正象限，保持 floorplan 左下角为 `(0, 0)`。
- `front3d.ground: true`: 对 bbox 低于地面的家具做 Z 方向抬升，避免模型局部原点导致物体沉入地面。

后续第二阶段如果要做“基于 3D-FRONT 资产池的随机生成”，可以继续复用第一阶段整理出的物体 JSON 和 manifest，但需要新增随机布局策略、支撑面规则和更严格的碰撞/可达性检查。

3D-FRONT 的物体名称、类别、bbox 和房间布局 JSON 可以用于推断 `placement.class`，但电磁材质不建议只靠名称硬猜。更稳妥的做法是先建立可审计的类别到 Sionna 材质映射表，例如 wood、concrete、metal、glass、plastic、fabric，并在 catalog 中记录 `confidence`。低置信度资产可以先标为默认材质，后续再人工抽样校正。

## 代码接入点

- 资产契约定义在 `src/scenegen/assets/schema.py`。
- catalog 加载和 runtime `Asset` 转换在 `src/scenegen/assets/loaders.py`。
- 旧 manifest 到新契约的一次性转换逻辑在 `src/scenegen/assets/legacy.py`。
- generated、Bistro 和 3D-FRONT source adapter 在 `src/scenegen/sources.py`。
- 3D-FRONT manifest、原始 scene JSON、实例矩阵和坐标转换辅助逻辑在 `src/scenegen/front3d.py`。

新增数据源时，优先复用 asset catalog 或 phase1 manifest 管线；只有场景来源、坐标转换和摆放逻辑不同的部分需要新增 source adapter。

## 3D-FRONT 第一阶段整理脚本

第一阶段只整理数据，不直接生成 SceneGen 场景。离线脚本：

```bash
uv run python tools/prepare_front3d_phase1.py \
  --source data/3D-Front \
  --output data/3D-Front \
  --copy-mode copy \
  --architecture-granularity scene \
  --scope all
```

脚本会在 `data/3D-Front` 下生成四个整理目录：

- `scenegen_objects_raw`
- `scenegen_objects_normalized`
- `scenegen_architecture_raw`
- `scenegen_architecture_normalized`

其中室内物品来自 3D-FUTURE 的 `raw_model.obj` 和 `normalized_model.obj`；建筑结构来自每个 `3D-FRONT/*.json` 的 `mesh` 数组，并按场景导出 OBJ。raw 建筑保持 3D-FRONT 的 `Y-up` 坐标，normalized 建筑转换为 SceneGen/Sionna 更适合的 `Z-up` 坐标。

全量运行会复制大量文件。正式执行前脚本会检查磁盘空间；开发时建议先跑小样本：

```bash
uv run python tools/prepare_front3d_phase1.py \
  --source data/3D-Front \
  --output results/front3d_phase1_smoke \
  --limit-objects 5 \
  --limit-scenes 5 \
  --skip-disk-check
```

整理完成后，可以用当前 `front3d` 模式合成已有组合场景：

```bash
uv run scenegen --set pipeline.mode=front3d --set pipeline.scenes=3 --set pipeline.run_name=front3d_preview
```
