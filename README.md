# SceneGen

SceneGen 是一个面向 Linux 环境的轻量级室内场景生成项目。它基于空场景和归一化资产，随机生成带家具、桌椅、小物件的 3D 场景，并同步导出 Sionna/Mitsuba 可加载的场景文件和平面图。

当前项目版本：`1.0.0`。

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
  - 可选去噪几何图 `floorplan/geometry_clean.png`
  - 侧视图 `floorplan/side_view.png`
  - 投影栈 `floorplan/stack.npz`
  - 元信息 `floorplan/meta.json`
  - 批量 BS/UE 点位叠加图 `label_floorplan/*.png`
  - 可选语义平面图 `floorplan/semantic.png`
  - 可选语义标注 `floorplan/semantic.json`
- 使用 YAML 作为主配置入口，CLI 参数可以覆盖 YAML。
- 每次运行都会保存最终生效配置 `effective_config.yaml`，方便复现。

## 目录结构

```text
SceneGen/
  config/
    template.yaml         # 唯一保留的完整配置模板，也是默认 YAML 入口
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

完整配置模板与默认 YAML 入口：

[config/template.yaml](/home/sunmeiyuan/projects/SceneGen/config/template.yaml)

配置字段、可选值和 CLI 覆盖说明：

[config/README.md](/home/sunmeiyuan/projects/SceneGen/config/README.md)

运行时默认读取 `config/template.yaml`。如果命令行传入参数，则命令行参数会覆盖 YAML 中对应字段。每次运行会在结果目录写出最终生效配置：

```text
<run_dir>/effective_config.yaml
```

这份文件是实际用于生成结果的配置，已经包含所有 CLI 覆盖后的值。

资产目录默认使用：

```yaml
assets:
  catalog: data/catalogs/bistro.v1.json
```

命令行推荐使用 `--asset-catalog` 临时替换资产目录；旧参数 `--asset-manifest` 仍可使用，会作为兼容别名映射到同一个配置字段。

3D-FRONT 模式使用独立配置段，不依赖 Bistro 资产 catalog：

```yaml
front3d:
  manifest: data/3D-Front/scenegen_manifest.json
  source_scene_dir: data/3D-Front/3D-FRONT
  variant: normalized
  object_variant: raw
  normalize_positive_xy: true
  ground_objects: true
  precheck_enabled: true
  precheck_max_z_m: 8.0
  precheck_max_footprint_ratio: 5.0
```

配置合并顺序是：代码内置默认值、`--config` 指定 YAML、CLI 覆盖、路径与类型归一化。旧 YAML 字段 `assets.manifest` 可以继续输入，但写出的 `effective_config.yaml` 只保留最新字段 `assets.catalog`。`config/` 目录只保留 `template.yaml`，需要实验配置时建议复制到其他位置或通过 CLI 覆盖。

## 快速开始

使用默认配置生成 Bistro 场景：

```bash
uv run scenegen
```

指定配置文件：

```bash
uv run scenegen --config config/template.yaml
```

生成 10 个 Bistro 场景，并清理输出目录下旧 run：

```bash
uv run scenegen --scenes 10 --clean
```

指定随机种子，便于复现：

```bash
uv run scenegen --scenes 10 --seed 123
```

生成后验证 Sionna XML：

```bash
uv run scenegen --scenes 1 --validate-sionna
```

关闭 floorplan 生成：

```bash
uv run scenegen --scenes 1 --no-floorplan
```

生成 synthetic rectangular room：

```bash
uv run scenegen --mode generated --scenes 1 --run-name smoke_generated --output-dir results
```

合成 3D-FRONT 已组合场景：

```bash
uv run scenegen --mode front3d --scenes 1 --run-name smoke_front3d --output-dir results
```

## 输出结构

一次运行会生成一个 run 目录，默认在 `results/<timestamp>/` 下：

```text
results/<run_name>/
  effective_config.yaml
  statistics.json
  manifest.json
  manifest_bistro.json、manifest_generated.json 或 manifest_front3d.json
  summary_obj/
    copy_manifest.json
    bistro_0000.obj
    ...
  summary_floorplan_raw/
    copy_manifest.json
    bistro_0000_geometry_raw.png
    ...
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
      geometry_raw.png
      preview.png
      side_view.png
      stack.npz
      meta.json
      semantic.png        # 仅 semantic_enabled: true 时生成
      semantic.json       # 仅 semantic_enabled: true 时生成
      class_mask.png      # 仅 class_mask_enabled: true 时生成，uint8 类别图
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
- `summary_obj/`: 每个场景 `scene.obj` 的汇总副本。
- `summary_floorplan_raw/`: 每个场景 `floorplan/geometry_raw.png` 的汇总副本。
- `floorplan/preview.png`: 指定高度或逐层投影总览。
- `label_floorplan/*.png`: 每份 label 在 `geometry_raw.png` 上绘制 BS/UE 点位后的检查图。
- `floorplan/geometry_raw.png`: 第一张几何投影图。默认是 `1.6m` 高度的原始密度投影。
- `floorplan/geometry_clean.png`: 可选输出，对原始密度投影进行低密度过滤、孤立点过滤和小半径形态学连通后的几何占据图。
- `floorplan/geometry_clean_preview.png`: 可选输出，clean 图总览。
- `floorplan/side_view.png`: 侧视投影。
- `floorplan/stack.npz`: 二值投影栈和高度层数据。
- `floorplan/semantic.png`: 可选输出，基于资产 placements 绘制的语义平面图。
- `floorplan/semantic.json`: 可选输出，每个资产的类别、旋转矩形、多边形坐标、颜色和父子关系。
- `floorplan/class_mask.png`: 可选 front3d 四分类掩码，单通道 `uint8`，类别固定为 `0 outdoor`、`1 wall`、`2 free_space`、`3 furniture`；默认会用 3D-FRONT 原始 `Door/Hole/Pocket` 从 wall 中扣除门洞并标为 free space。
- `floorplan/class_mask_preview.png`: 四分类掩码彩色预览图。
- `floorplan/class_mask.npy` / `floorplan/class_mask.npz`: 训练读取用的数组格式，`npz` 额外带分辨率、origin 和类别名。
- `floorplan/class_mask_meta.json`: 四分类掩码的类别 legend、像素统计、建筑 mesh 统计和生成参数。

## Bistro 禁区

Bistro 模式支持 XY 禁放区，用于避免在指定区域摆放物体。禁区已经配置化，不再写死在代码中。默认配置在 `config/template.yaml`：

```yaml
bistro:
  forbidden_xy_rects:
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
- Bistro 物体是否进入 `forbidden_xy_rects`。
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
  version: "1.1"
  ue_height_m: 1.6
  sampling_domain: global_floor
  ue_strategy: free_space_grid
  grid_resolution_m: 0.1
  batch_strategies: [free_space_grid]
  batch_grid_resolutions_m: [0.1]
  connected_area_enabled: true
  batch_connected_area_enabled: [true]
  ue_clearance_m: 0.35
  obstacle_strategy: height_aware
  walk_ignore_low_obstacles_below_m: 0.10
  walk_blocking_classes: [table, seat, floor]
  walk_min_component_area_m2: 0.25
  bs_strategy: wall_or_corner
  bs_count_strategy: fixed_per_room
  bs_per_room: 4
  bs_min_per_room: 1
  bs_max_per_room: 8
  bs_min_room_area_m2: 4.0
  bs_area_per_point_m2: 12.0
  bs_height_m: 2.4
  bs_ceiling_margin_m: 0.3
  bs_wall_clearance_m: 0.25
  bs_center_initial_radius_m: 0.2
  bs_center_radius_step_m: 0.1
  bs_center_max_radius_m: 2.0
  wall_clearance_m: 0.25
  corridor_room_id: "__corridor__"
  corridor_room_type: ConnectedArea
  corridor_clearance_m: 0.05
  overlay_enabled: true
  fail_on_error: true
```

3D-FRONT 的 UE 默认使用 `sampling_domain: global_floor` 与 `connected_area_enabled: true`：`plane_grid` 和 `free_space_grid` 都会先基于 opening-aware class mask 在整个建筑自由空间采样，再把点按 room floor mesh 归属到各个 room；归属不到任何 room 但仍在 free space 上的点会进入 `corridor_room_id: "__corridor__"` 的 connected area group。这个采样会使用 3D-FRONT 原始 `Door/Hole/Pocket` 扣出门洞，但不会把窗户当成 UE 采样开口。旧的逐 room 采样可切回 `sampling_domain: room_floor`，或设置 `connected_area_enabled: false` 只保留 room 内点。

`plane_grid` 表示指定高度上的采样平面，默认使用 `obstacle_strategy: height_aware` 做高度感知过滤，UE 高于桌面等低矮物体时不再被占地投影清理；如需旧的整列占用行为，可切换为 `footprint_column`。

`free_space_grid` 表示更保守的可行走区域：它在 floor mask 基础上按家具 footprint 扣除障碍，不受 UE 高度影响；同时会忽略高度低于 `walk_ignore_low_obstacles_below_m` 的薄物体，并删除小于 `walk_min_component_area_m2` 的孤立小区域。默认参与 walk 扣除的 placement class 是 `table`、`seat` 和 `floor`。

label 支持批量生成：`batch_strategies`、`batch_grid_resolutions_m` 和 `batch_connected_area_enabled` 会做笛卡尔组合。例如：

```yaml
label:
  batch_strategies: [plane_grid, free_space_grid]
  batch_grid_resolutions_m: [0.1]
  batch_connected_area_enabled: [true, false]
```

上面会生成 4 份 label：`label_panel_connected_0p1`、`label_panel_room_0p1`、`label_walk_connected_0p1`、`label_walk_room_0p1`。其中 `connected` 表示保留 connected area group，`room` 表示只保留 room 内点。单值字段 `ue_strategy`、`grid_resolution_m` 和 `connected_area_enabled` 仍保留为兼容入口，如果没有显式设置对应批量字段，CLI/YAML 设置这些单值字段会同步到批量配置。只有一个 connected area 模式时，文件名仍沿用 `label_panel_0p1` / `label_walk_0p1`。

BS 默认每个有效 room 最多 4 个，优先放在墙边或角落附近；也可以把 `bs_count_strategy` 切换为 `area_adaptive`，按房间 floor 面积决定每个 room 的 BS 数量，小房间可不放，大房间可放更多。若要评测单基站定位性能，可以改用几何中心 BS：

```yaml
label:
  bs_strategy: geometry_center
  bs_wall_clearance_m: 0.2
  bs_center_initial_radius_m: 0.2
  bs_center_radius_step_m: 0.1
  bs_center_max_radius_m: 2.0
```

`geometry_center` 会以建筑 floor 的几何中心为目标，在中心附近逐步扩张搜索半径，从全局自由空间中选择一个满足 BS 离墙和家具避让约束的点，输出为 `BS0`。

每个场景会写出：

- `label/<name>.json`: root 和 group 级都包含详细 point 列表与快速读取用 positions 数组；`bs_points` 内单点坐标使用 `position: [x, y, z]`。
- `label/report/<name>_report.json`: 根部记录总 `bs_count`、`ue_count`、`group_count` 和采样汇总；`rooms` 内记录每个 room 的 floor source、UE/BS 数量、验证结果和跳过原因。
- `label_floorplan/<name>.png`: 将对应 label 的 UE/BS 绘制到 `geometry_raw.png` 上，便于人工检查。

## Floorplan 原理

当前 floorplan 有两种输出。默认生成几何占据图，语义平面图默认关闭，需要时可用 `semantic_enabled: true` 或 `--semantic-floorplan` 打开。

第一版是几何占据图，沿用了原 `2026_FloorplanGen` 的 mesh 投影逻辑：

1. 读取每个场景生成后的 `scene.obj`。
2. 用 `trimesh` 合并并解析网格。
3. 自动推断竖直轴。
4. 在网格表面采样点云。
5. 自动估计有效高度范围。
6. 按配置生成累计俯视投影：默认只生成 `1.6m` 一个高度；也可以切换回旧版逐层扫描。
7. 输出分层 PNG、预览图、侧视图、投影栈和元数据。

默认几何平面图使用高密度单高度方案：`resolution_m_per_pixel: 0.05`、`sample_density_scale: 128.0`、`heights_m: [1.6]`。这类输出偏几何占据图，不包含资产类别语义。由于原始投影来自随机表面采样，低密度时可能有点状采样噪声；当前默认通过提高采样密度减轻这类伪纹理。

第二版是语义平面图，直接使用 SceneGen 生成时的 `placements` 绘制资产旋转矩形：

1. 使用 generated 房间尺寸或 Bistro 空场景 bbox 作为场景边界。
2. 将每个 `PlacedAsset` 的中心点、尺寸和 yaw 转为 XY 平面旋转矩形。
3. 按资产类别着色：table、seat、floor、tabletop。
4. 绘制 Bistro 禁区。
5. 输出 `semantic.png` 和 `semantic.json`。

语义平面图更清晰、速度更快，也更适合后续做标注、路径规划或布局质量检查；当前默认关闭，避免在主流程中生成暂时不用的额外文件。

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
uv run scenegen --mode bistro --scenes 1 --run-name smoke_bistro --output-dir results --validate-sionna
```

## 开发说明

- 默认入口是 `uv run scenegen`，不再保留根目录脚本入口。
- 新增配置项时，应同步更新：
  - `config/template.yaml`
  - `README.md`
  - `src/scenegen/config.py`
- 新增输出字段时，应同步检查：
  - `manifest.json`
  - `effective_config.yaml`
  - 测试用例
- floorplan 几何图基于生成后的 OBJ，因此会反映最终几何结果；语义图基于内存中的 `PlacedAsset`，需要时可以通过配置打开。
