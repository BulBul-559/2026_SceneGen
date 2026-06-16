# SceneGen

SceneGen 是一个面向 Linux 环境的轻量级室内场景生成项目。它基于空场景和归一化资产，随机生成带家具、桌椅、小物件的 3D 场景，并同步导出 Sionna/Mitsuba 可加载的场景文件和平面图。

当前项目版本：`2.1.1`。

当前主工作流包括 Bistro 场景生成和 3D-FRONT 已组合场景合成：Bistro 以 `data/scene/scene.obj` 作为空场景，以 `data/catalogs/bistro.v1.json` 管理资产契约；3D-FRONT 以第一阶段整理出的 `data/3D-Front/scenegen_manifest.json` 为索引，合并建筑结构和已有家具实例。`data/assets/manifest.json` 仍保留为兼容位置，但内容已经与 catalog 使用同一份清洗后的契约。

## 主要功能

- 基于空 Bistro 场景生成新的室内布局。
- 支持 generated 模式，生成简单矩形房间布局。
- 支持 front3d 模式，复现并合成 3D-FRONT 已组合好的房间场景。
- 自动读取本地资产 OBJ，资产路径使用 repo-relative POSIX 路径。
- 使用统一资产契约记录文件、摆放类别、几何尺寸、归一化信息和 Sionna 材质映射。
- 输出组合后的 `scene.obj` 和 Sionna RT 可加载的 `scene.xml`。
- 输出 `placements.json`，记录每个资产的类别、位置、朝向、包围盒和材质信息。
- 输出批量 `label/*.json` v1.1 和 `label/report/*_report.json`，记录 BS/UE 点位、分组、快速 positions 数组和验证结果。
- 可选使用 Sionna RT 验证生成的 `scene.xml`。
- 生成后自动执行轻量质量检查，发现越界、禁区重叠、碰撞、悬空或支撑关系异常。
- 输出 run 级统计报告，汇总物体数量、类别分布、支撑类型和近似占地率。
- 每个场景同步生成 floorplan：
  - 指定高度或逐层高度投影图 `floorplan_*.png`，例如 `floorplan_1p60.png`
  - 总览图 `floorplan/preview.png`
  - 侧视图 `floorplan/side_view.png`
  - 投影栈 `floorplan/stack.npz`
  - 元信息 `floorplan/meta.json`
  - 批量 BS/UE 点位叠加图 `label_floorplan/*.png`
  - 可选 3D-FRONT 四分类掩码 `floorplan/class_mask.*`
- 使用 YAML 作为主配置入口，CLI 通过统一 `--set key.path=value` 覆盖 YAML。
- 每次运行都会保存最终生效配置 `effective_config.yaml`，方便复现。

## 目录结构

```text
SceneGen/
  config/
    bistro.yaml           # Bistro 专用模板，也是默认 YAML 入口
    front3d.yaml          # 3D-FRONT 专用模板
  data/
    catalogs/             # 标准资产目录，例如 bistro.v1.json
    scene/                # 空 Bistro 场景与 label
    assets/               # 资产 OBJ/PNG/单资产 JSON 与兼容 manifest
    3D-Front/             # 本地 3D-FRONT 原始数据与 phase1 整理结果，默认被 git ignore
  docs/
    data_onboarding.md    # 新数据源接入说明
  src/scenegen/
    assets/               # 资产契约、加载、旧 manifest 转换、材质和路径解析
    cli.py                # 命令行入口与主流程
    config.py             # YAML 读取、CLI 覆盖、有效配置保存
    exporters.py          # OBJ/XML/label/manifest 输出
    floorplan.py          # 3D 网格转二维平面图
    labels.py             # BS/UE label 生成、验证和 floorplan overlay
    geometry.py           # OBJ、几何、支撑面、碰撞辅助逻辑
    models.py             # 数据结构
    paths.py              # 默认路径与常量
    placement.py          # 场景摆放规则
    quality.py            # 质量检查与统计报告
    sources.py            # generated/Bistro/3D-FRONT 数据源适配
    validation.py         # Sionna 加载验证
  tests/
    test_scenegen.py
  pyproject.toml
  uv.lock
```

`2026_FloorplanGen/` 是原始 floorplan 项目，目前核心逻辑已经迁移到 `src/scenegen/floorplan.py`，日常运行不需要单独调用它。

## 环境准备

项目使用 `uv` 管理环境，Python 固定为 3.12。

```bash
uv sync
```

主要依赖包括：

- `sionna` / `sionna-rt`
- `trimesh`
- `numpy`
- `pillow`
- `pyyaml`
- `pytest`
- `ruff`

## 配置方式

模式配置模板：

- [config/bistro.yaml](/home/sunmeiyuan/projects/SceneGen/config/bistro.yaml)
- [config/front3d.yaml](/home/sunmeiyuan/projects/SceneGen/config/front3d.yaml)

配置字段、可选值和 CLI 覆盖说明：

[config/README.md](/home/sunmeiyuan/projects/SceneGen/config/README.md)

运行时默认读取 `config/bistro.yaml`。3D-FRONT 合成建议用 `--config config/front3d.yaml`。两个 YAML 都是模式专用覆盖文件，只保留当前模式常用配置；代码内置默认值会补齐未写出的共享字段。CLI 覆盖统一使用可重复的 `--set key.path=value`，value 会按 YAML 解析。每次运行会在结果目录写出最终生效配置：

```text
<run_dir>/effective_config.yaml
```

这份文件是实际用于生成结果的配置，已经包含所有 CLI 覆盖后的值。

资产目录默认使用：

```yaml
assets:
  catalog: data/catalogs/bistro.v1.json
```

如需临时替换资产目录，使用：

```bash
uv run scenegen --set assets.catalog=data/catalogs/bistro.v1.json
```

3D-FRONT 模式使用独立配置段，不依赖 Bistro 资产 catalog：

```yaml
front3d:
  manifest: data/3D-Front/scenegen_manifest.json
  source_dir: data/3D-Front/3D-FRONT
  arch_variant: normalized
  object_variant: raw
  positive_xy: true
  ground: true
  precheck:
    enabled: true
    max_z_m: 8.0
    max_footprint_ratio: 5.0
  openings:
    mode: doors
```

配置合并顺序是：代码内置默认值、`--config` 指定 YAML、CLI `--set` 覆盖、路径与类型归一化、字段和值校验。配置 v2 不兼容旧 YAML 字段和旧显式 CLI 参数；未知字段会直接报错。需要实验配置时建议复制对应模式模板到其他位置，再通过 YAML 或 `--set` 覆盖。

## 快速开始

使用默认配置生成 Bistro 场景：

```bash
uv run scenegen
```

指定配置文件：

```bash
uv run scenegen --config config/bistro.yaml
```

生成 10 个 Bistro 场景，并在同名 run 已存在时只清理该 run：

```bash
uv run scenegen --set pipeline.scenes=10 --set pipeline.clean=true
```

指定随机种子，便于复现：

```bash
uv run scenegen --set pipeline.scenes=10 --set pipeline.seed=123
```

生成后验证 Sionna XML：

```bash
uv run scenegen --set pipeline.scenes=1 --set validation.sionna=true
```

关闭 floorplan 生成：

```bash
uv run scenegen --set pipeline.scenes=1 --set floorplan.enabled=false
```

生成 synthetic rectangular room：

```bash
uv run scenegen --set pipeline.mode=generated --set pipeline.scenes=1 --set pipeline.run_name=smoke_generated
```

合成 3D-FRONT 已组合场景：

```bash
uv run scenegen --config config/front3d.yaml --set pipeline.scenes=1 --set pipeline.run_name=smoke_front3d
```

正式 front3d 大规模生产建议使用任务模板：

```bash
uv run scenegen --config config/tasks/front3d_full_simulation.yaml --set pipeline.scenes=1 --set pipeline.run_name=front3d_full_sample
```

该模板打开 label、geometry sampling floorplan、class mask 和 mesh furniture mask，`label.ue.sampling.strategies` 默认包含 `[panel, walk]`，`label.ue.sampling.grid_m` 默认包含 `[0.1, 0.2, 0.4, 0.5]`。label 可行域 mask 默认以 `label.ue.sampling.mask_resolution_m: 0.05` 构建，不同 UE 间隔只在这张高精度 mask 上抽样。

如果要在同一个 batch 中同步生成 derived maps 并整理 compact vision dataset，开启 `postprocess`：

```bash
uv run scenegen-batch \
  --config config/tasks/front3d_full_simulation.yaml \
  --workers 8 \
  --max-retries 1 \
  --set pipeline.scenes=2000 \
  --set pipeline.run_name=front3d_production_2000 \
  --set postprocess.maps.enabled=true \
  --set postprocess.dataset.enabled=true \
  --set postprocess.maps.bs_label.mode=name \
  --set postprocess.maps.bs_label.name=label_panel_0p1
```

`postprocess` 默认关闭，只在 `scenegen-batch` 中执行；普通 `scenegen` 单场景入口不会触发。

## 生产运行与日志

普通 `scenegen` run 会在 run 目录下写入轻量诊断日志：

```text
logs/
  events.jsonl              # run/scene/stage 事件流
  timings.jsonl             # 阶段耗时记录
  state/run_state.json      # 当前/最终状态快照
  workers/<worker_id>.jsonl # 单进程 worker 结构化日志
  scenes/<scene>/attempt_*/ # label/floorplan 异常 traceback
```

每个 scene record 和最终 `manifest.json` 会记录 `timings_s`、`timing_summary_s` 和日志路径。阶段计时包含 `build_scene`、`statistics`、`precheck`、`quality`、`label`、`floorplan`、`floorplan_geometry`、`class_mask`、`label_overlay` 和 `write_manifest`。

大规模生产使用 `scenegen-batch`，它会先固化 `scene_plan.jsonl`，再按 worker 分片执行，支持 resume、失败队列、重试队列、worker 日志和统一 batch manifest：

```bash
uv run scenegen-batch \
  --config config/tasks/front3d_full_simulation.yaml \
  --workers 4 \
  --scheduler static \
  --max-retries 1 \
  --set pipeline.scenes=2000 \
  --set pipeline.run_name=front3d_production_2000
```

继续未完成的同名生产任务：

```bash
uv run scenegen-batch \
  --config config/tasks/front3d_full_simulation.yaml \
  --workers 4 \
  --resume \
  --set pipeline.scenes=2000 \
  --set pipeline.run_name=front3d_production_2000
```

batch run 额外输出：

```text
batch/
  scene_plan.jsonl
  state.json
  postprocess_state.json
  postprocess_events.jsonl
  postprocess_failures.jsonl
  postprocess_report.json
  logs/events.jsonl
  logs/timings.jsonl
  logs/workers/worker_*.log
  logs/workers/worker_*.jsonl
  logs/queues/failures.jsonl
  logs/queues/retry.jsonl
  logs/queues/dead_letter.jsonl
  logs/scenes/<task>/attempt_*/task.traceback.*
  worker_runs/
```

开启 `postprocess.maps.enabled` 后，每个成功 scene 会额外得到 `maps/geometry.npz`、`maps/propagation.npz` 和 `maps/metadata.json`。开启 `postprocess.dataset.enabled` 后，默认会在 `datasets/<run_name>_vision/` 下构建 compact vision dataset，只保留训练需要的 floorplan、mask、derived maps、BS label 和 metadata。

`manifest_batch.json`、`manifest_front3d.json` 和 `manifest.json` 会在 batch 完成后统一汇总最终发布到 run 根目录的 `front3d_0000/`、`front3d_0001/` 等标准场景目录。

`--scheduler static` 是默认调度策略，保持固定分片，资源占用更保守；`--scheduler dynamic` 会让空闲 worker 继续领取下一个 scene，可能减少长尾，但也可能在高负载机器上增加 CPU/IO 争用。正式大批量前建议先用 30 个 scene 对比两种策略。成功 scene 现在会从 `batch/worker_runs` 直接 move 到 run 根目录，`batch/worker_runs` 主要保留 worker 子 run 的日志、配置和失败场景调试信息，不再保存成功场景的完整重复副本。

## 输出结构

一次运行会生成一个 run 目录，默认在 `results/<timestamp>/` 下：

```text
results/<run_name>/
  effective_config.yaml
  statistics.json
  manifest.json
  manifest_bistro.json、manifest_generated.json 或 manifest_front3d.json
  summary/
    obj/
      copy_manifest.json
      bistro_0000.obj
      ...
    floorplan/
      copy_manifest.json
      bistro_0000_floorplan_1p60.png
      ...
    label_floorplan/
      copy_manifest.json
      0p1/
        bistro_0000_label_walk_0p1.png
      0p5/
        bistro_0000_label_panel_0p5.png
      ...
  logs/
    events.jsonl
    timings.jsonl
    state/run_state.json
    workers/
    scenes/
  bistro_0000/
    scene.obj
    scene.xml
    label/
      label_walk_0p1.json
      report/
        label_walk_0p1_report.json
      ...
    label_floorplan/
      label_walk_0p1.png
      ...
    placements.json
    statistics.json
    quality_report.json
    assets/
    floorplan/
      floorplan_*.png
      preview.png
      side_view.png
      stack.npz
      meta.json
      class_mask.png      # 仅 floorplan.class_mask.enabled: true 时生成，uint8 类别图
      class_mask_preview.png
      class_mask.npy
      class_mask.npz
      class_mask_meta.json
  bistro_0001/
  ...
```

重要文件说明：

- `scene.obj`: 合并空场景与新摆放资产后的 OBJ。
- `scene.xml`: Sionna RT/Mitsuba 场景文件。
- `placements.json`: 资产摆放结果、包围盒、材质映射和父子关系。
- `label/*.json`: 批量 BS/UE 点位。当前输出为 v1.1，root 和 group 级都包含 `bs_points`、`ue_points`、`bs_positions`、`ue_positions`；`bs_points` 内单点坐标使用 `position: [x, y, z]`。
- `label/report/*_report.json`: 每份 label 的生成与验证报告，根部记录总 `bs_count`、`ue_count`、`group_count` 和采样汇总，`rooms` 内记录逐房间点位数量、跳过原因、错误和警告。
- `manifest.json`: 本次 run 的汇总信息。
- `asset_catalog`: Bistro/generated run 在 `manifest.json` 中记录本次使用的资产 catalog；front3d run 不依赖 Bistro catalog，而是记录 `front3d_manifest`、`front3d_variant` 和 `front3d_object_variant`。
- `effective_config.yaml`: 本次 run 实际生效的配置。
- `statistics.json`: run 级统计报告，包含每个场景的物体数、类别计数、支撑类型和近似占地率。
- `quality_report.json`: 单场景质量检查报告，包含错误/警告列表。
- `summary/obj/`: 每个场景 `scene.obj` 的汇总副本。
- `summary/floorplan/`: 每个场景主高度层 `floorplan/floorplan_*.png` 的汇总副本。
- `summary/label_floorplan/<grid>/`: 每份 label overlay 的汇总副本，按 UE 采样间隔分组，例如 `0p1`、`0p2`、`0p5`。
- `floorplan/preview.png`: 指定高度或逐层投影总览。
- `label_floorplan/*.png`: 每份 label 在主高度层 `floorplan_*.png` 上绘制 BS/UE 点位后的检查图。
- `floorplan/side_view.png`: 侧视投影。
- `floorplan/stack.npz`: 二值投影栈和高度层数据。
- `floorplan/class_mask.png`: 可选 front3d 四分类掩码，单通道 `uint8`，类别固定为 `0 outdoor`、`1 wall`、`2 free_space`、`3 furniture`；当前会先把 3D-FRONT 原始 `Door/Hole/Pocket` 标为 free space，再执行墙体膨胀，门洞不会在膨胀后被额外恢复。家具层默认 `floorplan.class_mask.furniture_mode: mesh`，加载每个家具 OBJ 并应用实例 transform 后做高度过滤三角面投影，生成像素级 footprint；也可切到 `bbox` 加速。
- `floorplan/class_mask_preview.png`: 四分类掩码彩色预览图。
- `floorplan/class_mask.npy` / `floorplan/class_mask.npz`: 训练读取用的数组格式，`npz` 额外带分辨率、origin 和类别名。
- `floorplan/class_mask_meta.json`: 四分类掩码的类别 legend、像素统计、建筑 mesh 统计、生成参数和阶段耗时。

## Bistro 禁区

Bistro 模式支持 XY 禁放区，用于避免在指定区域摆放物体。禁区已经配置化，不再写死在代码中。默认配置在 `config/bistro.yaml`：

```yaml
bistro:
  forbidden_xy:
    - [1.0, 11.0, 4.5, 16.0]
    - [8.0, 8.0, 14.0, 10.0]
```

格式为：

```text
[x_min, y_min, x_max, y_max]
```

禁区只在 `mode: bistro` 时生效。生成结果的 `manifest.json` 也会记录本次使用的禁区。

## 质量检查与统计

默认开启质量检查，配置位于 `quality`：

```yaml
quality:
  enabled: true
  fail_on_error: true
  collision_padding_m: 0.0
  bistro_static_clearance_m: 0.0
  support_tolerance_m: 0.05
```

检查内容包括：

- 物体是否跑出房间或 Bistro 空场景边界。
- Bistro 物体是否进入 `bistro.forbidden_xy`。
- 生成物体之间是否发生 3D AABB 重叠。
- 地面物体是否在地面支撑面上。
- 桌面物体是否有父级桌子且高度匹配。
- Bistro 已有台面上的小物是否落在检测到的支撑面上。

如果 `fail_on_error: true`，发现质量错误时命令会返回非零状态，但仍会写出 `manifest.json`、单场景 `quality_report.json` 和 run 级 `statistics.json`，方便排查。

## Label 点位

默认开启 label 生成，配置位于 `label`。Bistro 会优先读取手工 `data/scene/label.json`，并升级为 v1.1 输出结构；3D-FRONT 会按 room 自动生成点位：

```yaml
label:
  enabled: true
  fail_on_error: true
  ue:
    height_m: 1.6
    sampling:
      domain: global_floor
      grid_m: [0.1]
      mask_resolution_m: 0.05
      wall_clearance_m: 0.2
      min_component_area_m2: 0.25
      strategies: [walk]
    walk:
      furniture_clearance_m: 0.1
      obstacle_strategy: below_ue_column
      ignore_low_obstacles_below_m: 0.10
      blocking_classes: [table, seat, floor]
    connected_area:
      room_id: "__corridor__"
      room_type: ConnectedArea
  bs:
    strategy: wall_or_corner
    height_m: 2.4
    ceiling_margin_m: 0.3
    wall_clearance_m: 0.2
    count:
      strategy: fixed_per_room
      per_room: 4
      min_per_room: 1
      max_per_room: 8
      min_room_area_m2: 4.0
      area_per_point_m2: 12.0
    center:
      initial_radius_m: 0.2
      radius_step_m: 0.1
      max_radius_m: 2.0
  overlay:
    enabled: true
```

3D-FRONT 的 UE 默认使用 `label.ue.sampling.domain: global_floor`：`panel` 和 `walk` 都会先用固定的 `label.ue.sampling.mask_resolution_m` 在建筑 XY bbox 上构建全局可行域 mask，再按 `label.ue.sampling.grid_m` 抽取点位，随后扣除 outdoor 和按 `label.ue.sampling.wall_clearance_m` 膨胀后的 wall，最后才把点按 room floor mesh 归属到各个 room；归属不到任何 room 但仍在 free space 上的点会进入 `label.ue.connected_area.room_id: "__corridor__"` 的 connected area group。默认 `label.ue.sampling.wall_clearance_m: 0.2`，这个采样会按 `front3d.openings.mode` 把 3D-FRONT 原始门洞在墙体膨胀前标为 free space，但不会在墙体膨胀后额外恢复门洞；门洞足够宽就自然保留采样点，否则会被膨胀后的 wall 吃掉。旧的逐 room 采样可切回 `label.ue.sampling.domain: room_floor`。

`panel` 表示室内平面采样：只扣除 outdoor 和墙体间隔，不扣家具。

`walk` 表示可行走区域：它在 `panel` 的基础上继续扣除家具障碍，默认使用 `label.ue.walk.obstacle_strategy: below_ue_column`，即只要采样点 XY 落入家具 footprint，且家具在 `label.ue.walk.ignore_low_obstacles_below_m` 到 UE 高度之间存在有效高度，就认为该点不可行走。旧的单高度层判断可切换为 `height_aware`；如需最保守的整列占用行为，可切换为 `footprint_column`。同时会忽略高度低于 `label.ue.walk.ignore_low_obstacles_below_m` 的薄物体，并删除小于 `label.ue.sampling.min_component_area_m2` 的孤立小区域。默认参与 walk 扣除的 placement class 是 `table`、`seat` 和 `floor`。

label 支持批量生成：`label.ue.sampling.strategies` 和 `label.ue.sampling.grid_m` 会做笛卡尔组合。例如：

```yaml
label:
  ue:
    sampling:
      strategies: [panel, walk]
      grid_m: [0.1, 0.2]
```

上面会生成 4 份 label：`label_panel_0p1`、`label_panel_0p2`、`label_walk_0p1`、`label_walk_0p2`。全局采样中的 connected area group 始终保留，不再生成 room-only 版本。

BS 默认每个有效 room 最多 4 个，优先放在墙边或角落附近；也可以把 `label.bs.count.strategy` 切换为 `area_adaptive`，按房间 floor 面积决定每个 room 的 BS 数量，小房间可不放，大房间可放更多。若要评测单基站定位性能，可以额外启用几何中心 BS：

```yaml
label:
  bs:
    strategy: wall_or_corner
    wall_clearance_m: 0.2
    center:
      enabled: true
      initial_radius_m: 0.2
      radius_step_m: 0.1
      max_radius_m: 2.0
```

中心 BS 会以建筑 floor 的几何中心为目标，在中心附近逐步扩张搜索半径，从全局自由空间中选择一个满足 BS 离墙和家具避让约束的点，输出为 `BS_CENTER`。它可以和普通 `wall_or_corner` BS 同时存在。

每个场景会写出：

- `label/<name>.json`: root 和 group 级都包含详细 point 列表与快速读取用 positions 数组；`bs_points` 内单点坐标使用 `position: [x, y, z]`。
- `label/report/<name>_report.json`: 根部记录总 `bs_count`、`ue_count`、`group_count` 和采样汇总；`rooms` 内记录每个 room 的 floor source、UE/BS 数量、验证结果和跳过原因。
- `label_floorplan/<name>.png`: 将对应 label 的 UE/BS 绘制到主高度层 `floorplan_*.png` 上，便于人工检查。

## Floorplan 原理

当前 floorplan 默认生成几何占据图；front3d 模式可选生成四分类 `class_mask`，用于训练时区分 outdoor、wall、free_space、furniture。`class_mask` 的 furniture 层默认使用 mesh footprint，若需要更快但更粗的输出，可设置 `floorplan.class_mask.furniture_mode: bbox`。

第一版是几何占据图，沿用了原 `2026_FloorplanGen` 的 mesh 投影逻辑：

1. 读取每个场景生成后的 `scene.obj`。
2. 用 `trimesh` 合并并解析网格。
3. 自动推断竖直轴。
4. 在网格表面采样点云。
5. 自动估计有效高度范围。
6. 按配置生成累计俯视投影：默认只生成 `1.6m` 一个高度；也可以切换回旧版逐层扫描。
7. 输出分层 PNG、预览图、侧视图、投影栈和元数据。

默认几何平面图使用高密度单高度方案：`floorplan.geometry.projection: sampling`、`floorplan.resolution_m: 0.05`、`floorplan.sampling.density_scale: 128.0`、`floorplan.sampling.max_points: 4000000`、`floorplan.geometry.height.values_m: [1.6]`。这类输出偏几何占据图，不包含资产类别语义。由于原始投影来自面积加权随机表面采样，低密度时可能有点状采样噪声；当前默认保留较高采样密度，但用 4M 点上限控制大场景生成耗时。

也可以切换到确定性的高度过滤投影：`floorplan.geometry.projection: ray_height_filtered`。该模式不使用随机表面采样，而是对 mesh 三角形做 XY column rasterization；每个像素只在 `bottom_m <= z <= target_height` 范围内存在几何时被标为 occupied，因此同一 `scene.obj` 和同一配置会得到稳定的 `floorplan_*.png`。

## 常用命令

安装或更新环境：

```bash
uv sync
```

查看命令行参数：

```bash
uv run scenegen --help
```

运行测试：

```bash
uv run pytest
```

运行静态检查：

```bash
uv run ruff check .
```

生成一个快速 smoke run：

```bash
uv run scenegen --set pipeline.mode=bistro --set pipeline.scenes=1 --set pipeline.run_name=smoke_bistro --set validation.sionna=true
```

## 开发说明

- 默认入口是 `uv run scenegen`，不再保留根目录脚本入口。
- 新增配置项时，应同步更新：
  - `config/bistro.yaml` 或 `config/front3d.yaml` 中真正相关的模式模板
  - `README.md`
  - `src/scenegen/config.py`
- 新增输出字段时，应同步检查：
  - `manifest.json`
  - `effective_config.yaml`
  - 测试用例
- floorplan 几何图基于生成后的 OBJ，因此会反映最终几何结果；语义图基于内存中的 `PlacedAsset`，需要时可以通过配置打开。
