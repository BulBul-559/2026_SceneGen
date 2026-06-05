# SceneGen Agent Handoff

这份文档是给后续对话或新 agent 快速接手用的浓缩版。更完整的说明看根目录 `README.md`、`config/README.md` 和 `docs/data_onboarding.md`。

## 项目一句话

SceneGen 是一个 Linux/uv 管理的轻量室内 3D 场景生成项目。它把空场景、资产目录或 3D-FRONT 已组合场景合成为标准输出：`scene.obj`、Sionna/Mitsuba 可加载的 `scene.xml`、`placements.json`、批量 `label/*.json`、floorplan、质量报告和统计报告。

## 当前主要模式

- `bistro`: 主实验模式。读取 `data/scene/scene.obj` 和 `data/catalogs/bistro.v1.json`，随机摆放桌椅、地面物体、桌面小物和 Bistro 已有台面小物。
- `generated`: 简单矩形房间 smoke/test 模式，保留用于快速验证生成链路。
- `front3d`: 复现并合成 3D-FRONT 原始已有组合场景。读取 `data/3D-Front/scenegen_manifest.json`，使用整理后的建筑结构和家具实例输出与 Bistro 一致的目录结构。

`front3d` v1 只合成已有 3D-FRONT 场景，不做基于 3D-FRONT 资产池的随机重排。

## 关键目录

- `src/scenegen/cli.py`: CLI 入口和主生成流程。
- `src/scenegen/config.py`: 配置默认值、YAML 读取、CLI 覆盖、类型归一化、字段和值校验。
- `src/scenegen/sources.py`: `generated`、`bistro`、`front3d` 三种 source adapter。
- `src/scenegen/front3d.py`: 3D-FRONT manifest、scene JSON、实例矩阵、坐标转换和 asset 解析。
- `src/scenegen/exporters.py`: OBJ、XML、placements、manifest 和 summary 输出。
- `src/scenegen/labels.py`: BS/UE label v1.1 生成、验证和 floorplan overlay。
- `src/scenegen/floorplan.py`: 几何 floorplan、clean 图、semantic floorplan。
- `src/scenegen/quality.py`: 质量检查和统计报告。
- `src/scenegen/assets/`: Bistro 资产契约、loader、legacy converter、材质映射。
- `tools/prepare_front3d_phase1.py`: 3D-FRONT 第一阶段离线整理脚本。
- `config/template.yaml`: 唯一保留的完整配置模板，也是默认 YAML 入口。
- `data/3D-Front/`: 本地 3D-FRONT 原始数据和整理结果，默认 git ignored。

## 配置链路

配置入口以 YAML 为主，CLI 可覆盖 YAML。

合并顺序：

1. `DEFAULT_CONFIG`。
2. 加载 `--config` 指定 YAML，默认是 `config/template.yaml`。
3. 应用 CLI 覆盖。
4. 归一化路径、类型和旧字段别名。
5. 校验未知字段和值范围。
6. 写出 `<run_dir>/effective_config.yaml`。

维护规则：

- `config/` 目录只保留 `template.yaml`，它应与 `src/scenegen/config.py` 里的 `DEFAULT_CONFIG` 保持一致。
- 需要实验配置时复制模板到其他位置，或通过 CLI 覆盖。
- 旧字段 `assets.manifest` 和旧 CLI `--asset-manifest` 仍兼容，但最终写出为 `assets.catalog`。
- YAML 写错字段会直接报错，不应静默忽略。

## 当前默认重点参数

`floorplan` 默认用于训练输入的 raw 几何投影：

- `resolution_m_per_pixel: 0.05`
- `height_mode: heights`
- `heights_m: [1.6]`
- `sample_density_scale: 128.0`
- `semantic_enabled: false`
- `geometry_clean_enabled: false`
- `class_mask_enabled: false`: 需要训练用四分类掩码时在 `front3d` 模式打开。输出 `floorplan/class_mask.png`、`class_mask_preview.png`、`class_mask.npy`、`class_mask.npz` 和 `class_mask_meta.json`，类别固定为 `0 outdoor`、`1 wall`、`2 free_space`、`3 furniture`。2.0.0 起默认 `class_mask_opening_mode: doors` 会把 3D-FRONT 原始 `Door/Hole/Pocket` 在墙体膨胀前标为 free space，不做膨胀后的门洞恢复；可切到 `windows` 或 `doors_and_windows`。

`front3d` 默认策略：

- `variant: normalized`: 建筑结构用 phase1 的 Z-up normalized 结果。
- `object_variant: raw`: 家具用 3D-FUTURE raw 模型，因为 3D-FRONT 原始位姿按 raw 尺寸设计。
- `normalize_positive_xy: true`: 整体平移到 XY 正象限，floorplan 左下保持 `(0, 0)`。
- `ground_objects: true`: 家具 bbox 低于地面时做轻量 Z 抬升。
- `precheck_enabled: true`: 生成正式 label/floorplan 前先检查候选场景；家具过少、Z 范围异常或投影占比异常时跳过该 scene id 并自动补齐。

`label` 默认策略：

- `version: "1.1"`
- `ue_height_m: 1.6`
- `sampling_domain: global_floor`
- `ue_strategy: free_space_grid`
- `grid_resolution_m: 0.1`
- `connected_area_enabled: true`
- `batch_connected_area_enabled: [true]`
- `obstacle_strategy: height_aware`
- `bs_strategy: wall_or_corner`
- `bs_count_strategy: fixed_per_room`
- `bs_per_room: 4`
- `bs_wall_clearance_m: 0.25`
- `corridor_room_id: "__corridor__"`
- `corridor_clearance_m: 0.05`
- `overlay_enabled: true`

`sampling_domain: global_floor` + `connected_area_enabled: true` 是当前 front3d 推荐策略：`plane_grid` 和 `free_space_grid` 先在建筑 XY bbox 的全局矩形网格上采样，再扣 outdoor 和膨胀后的 wall，随后按 room floor mesh 分类，未归属点进入 `ConnectedArea` group。门洞使用原始 `Door/Hole/Pocket` 在墙体膨胀前标为 free space，不做膨胀后的门洞恢复；label 的门洞候选区会按 `corridor_clearance_m` 收缩，窗户不作为 UE 采样开口。`batch_connected_area_enabled: [true, false]` 可同时生成 connected 和 room-only 两套 label。需要严格旧行为时切换为 `room_floor`。单基站定位实验可用 `bs_strategy: geometry_center`，它会在建筑几何中心附近搜索一个满足自由空间和 BS 离墙约束的 `BS0`。

## 常用命令

安装环境：

```bash
uv sync
```

运行测试和 lint：

```bash
uv run pytest
uv run ruff check .
```

Bistro 默认生成：

```bash
uv run scenegen
```

3D-FRONT 合成：

```bash
uv run scenegen --mode front3d --scenes 3 --run-name front3d_preview --output-dir results
```

快速 smoke，不生成 floorplan：

```bash
uv run scenegen --mode generated --scenes 1 --run-name smoke_generated --output-dir results --no-floorplan
```

可选 Sionna XML 验证：

```bash
uv run scenegen --scenes 1 --validate-sionna
```

## 标准输出结构

一次 run 默认写到 `results/<run_name>/`：

```text
effective_config.yaml
manifest.json
manifest_<mode>.json
statistics.json
summary_obj/
summary_floorplan_raw/
<mode_prefix>_0000/
  scene.obj
  scene.xml
  label/
    label_walk_0p1.json
    report/
      label_walk_0p1_report.json
  label_floorplan/
    label_walk_0p1.png
  placements.json
  quality_report.json
  statistics.json
  assets/
  floorplan/
    floorplan_1p60.png
    geometry_raw.png
    preview.png
    side_view.png
    stack.npz
    meta.json
```

如果 `floorplan.semantic_enabled: true`，还会生成 `floorplan/semantic.png` 和 `floorplan/semantic.json`。

## 3D-FRONT 数据阶段

第一阶段脚本把 `data/3D-Front` 下原始数据整理为四类目录：

- `scenegen_objects_raw`
- `scenegen_objects_normalized`
- `scenegen_architecture_raw`
- `scenegen_architecture_normalized`

总索引为 `data/3D-Front/scenegen_manifest.json`。`front3d` 模式依赖这个 manifest。

小样本整理示例：

```bash
uv run python tools/prepare_front3d_phase1.py \
  --source data/3D-Front \
  --output results/front3d_phase1_smoke \
  --limit-objects 5 \
  --limit-scenes 5 \
  --skip-disk-check
```

全量整理会复制大量文件，执行前确认磁盘空间。`data/3D-Front` 默认忽略，不要提交大数据。

## 已知约束和注意点

- `results/`、`data/3D-Front/`、临时输出目录默认 git ignored。
- `3d_scripts/` 是参考脚本目录，不是当前主流程的一部分。
- `--clean` 会清理整个 `output_dir` 下旧 run，使用共享结果目录时要谨慎。
- 质量检查默认开启；`quality.fail_on_error: true` 时发现 error 会让命令返回非零，但仍会写出报告。
- semantic floorplan 默认关闭，当前主训练输入优先使用高密度 raw 几何投影。
- 3D-FRONT 的电磁材质目前主要靠类别/材质名映射，低置信度结果需要后续抽样校正。

## 后续可能方向

- 基于 3D-FRONT 资产池做随机生成模式，而不只是复现已有组合场景。
- 更严格的 transmitter / receiver 采样策略。
- 数据集划分、批量生成 manifest 和训练集索引。
- 更细的电磁材质标注审核流程。
- 对 front3d 房间做更精确的可用区域、拥挤度和可达性检查。
