# SceneGen Config Reference

`config/template.yaml` 是当前唯一保留的完整配置模板，也是 CLI 默认读取的 YAML。建议复制这份模板到其他位置作为实验配置，或直接用 CLI 参数覆盖其中字段。

每次运行都会在 run 目录写出 `effective_config.yaml`，它记录 YAML 与 CLI 覆盖后真正生效的配置。

## 合并规则

配置优先级从低到高：

1. 代码内置默认值 `DEFAULT_CONFIG`
2. `--config` 指定的 YAML，默认 `config/template.yaml`
3. CLI 覆盖参数
4. 路径、类型、旧字段别名归一化
5. 字段名和取值校验

未知字段会直接报错。旧字段 `assets.manifest` 和旧 CLI `--asset-manifest` 仍兼容，但最终会归一化为 `assets.catalog`。

## pipeline

| 字段 | 类型 / 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `mode` | `bistro` / `generated` / `front3d` | `bistro` | 生成模式。 |
| `scenes` | integer, `>=1` | `10` | 本次生成场景数量。 |
| `seed` | integer | `20260517` | 主随机种子。 |
| `output_dir` | path | `results` | run 输出根目录。 |
| `run_name` | string / `null` | `null` | run 目录名；不能包含路径分隔符。为 `null` 时用时间戳。 |
| `clean` | boolean | `false` | 生成前是否清理 `output_dir` 下已有内容。 |

CLI 覆盖：`--mode`、`--scenes`、`--seed`、`--output-dir`、`--run-name`、`--clean/--no-clean`。

## assets

| 字段 | 类型 / 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `catalog` | path | `data/catalogs/bistro.v1.json` | Bistro/generated 使用的资产 catalog。`front3d` 模式不依赖这个 Bistro catalog。 |

CLI 覆盖：`--asset-catalog`。兼容别名：`--asset-manifest`。

## bistro

| 字段 | 类型 / 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `base_dir` | path | `data/scene` | 空 Bistro 场景目录，至少包含 `scene.obj`，可包含手工 `label.json`。 |
| `forbidden_xy_rects` | list of `[x_min, y_min, x_max, y_max]` | 两个 Bistro 禁区 | Bistro 模式地面摆放禁区。 |

CLI 覆盖：`--bistro-base-dir`。禁区目前通过 YAML 配置。

## front3d

| 字段 | 类型 / 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `manifest` | path | `data/3D-Front/scenegen_manifest.json` | 3D-FRONT phase1 整理输出总 manifest。 |
| `source_scene_dir` | path | `data/3D-Front/3D-FRONT` | 原始 3D-FRONT scene JSON 目录。 |
| `variant` | `normalized` / `raw` | `normalized` | 建筑结构使用的整理版本。默认 normalized，已转为 SceneGen/Sionna 的 Z-up。 |
| `object_variant` | `raw` / `normalized` | `raw` | 室内物体使用的整理版本。默认 raw，因为 3D-FRONT 原始位姿通常按 raw 模型尺寸设计。 |
| `scene_ids` | list of string / comma-separated CLI string | `[]` | 指定合成的 scene id；为空时按 `scene_selection` 自动选择。 |
| `scene_selection` | `random` / `sequential` | `random` | `scene_ids` 为空时的自动选场景策略。 |
| `use_replace_jid` | boolean | `true` | child 有 `replace_jid` 时优先使用 replacement 模型。 |
| `skip_missing_objects` | boolean | `true` | 缺失家具模型时跳过并记录；为 `false` 时直接失败。 |
| `normalize_positive_xy` | boolean | `true` | 将合成场景整体平移到 XY 正象限，保持 floorplan 左下为 `(0, 0)`。 |
| `ground_objects` | boolean | `true` | 家具 bbox 低于 floor 时做轻量 Z 抬升。 |
| `precheck_enabled` | boolean | `true` | 写出 label/floorplan 前对候选场景做轻量异常预检；失败的 scene id 会被跳过并自动补齐。 |
| `precheck_max_attempts_per_scene` | integer, `>=1` | `20` | 每个输出编号最多尝试多少个候选 scene。 |
| `precheck_min_placements` | integer, `>=0` | `1` | 候选场景至少需要保留多少个家具实例。 |
| `precheck_max_z_m` | float, `>0` | `8.0` | 候选家具 bbox 的最大 Z 超过该值时判为异常。 |
| `precheck_max_footprint_ratio` | float, `>0` | `5.0` | 家具总投影面积 / 建筑 bbox 面积超过该值时判为异常。 |

CLI 覆盖：`--front3d-manifest`、`--front3d-source-scene-dir`、`--front3d-variant`、`--front3d-object-variant`、`--front3d-scene-ids`、`--front3d-scene-selection`、`--front3d-use-replace-jid/--no-front3d-use-replace-jid`、`--front3d-skip-missing-objects/--no-front3d-skip-missing-objects`、`--front3d-normalize-positive-xy/--no-front3d-normalize-positive-xy`、`--front3d-ground-objects/--no-front3d-ground-objects`、`--front3d-precheck/--no-front3d-precheck`、`--front3d-precheck-max-attempts-per-scene`、`--front3d-precheck-min-placements`、`--front3d-precheck-max-z`、`--front3d-precheck-max-footprint-ratio`。

## placement

只影响 `bistro` 和 `generated` 的随机摆放，不影响 `front3d` 已组合场景复现。

| 字段 | 类型 / 范围 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `min_tables` | integer, `>=0` | `4` | 每场最少桌子数。 |
| `max_tables` | integer, `>= min_tables` | `8` | 每场最多桌子数。 |
| `floor_extras` | integer, `>=0` | `6` | 地面额外物体数量。 |
| `min_tabletop_items` | integer, `>=0` | `3` | 每张桌面小物最少数量。 |
| `max_tabletop_items` | integer, `>= min_tabletop_items` | `9` | 每张桌面小物最多数量。 |
| `bistro_support_items` | integer, `>=0` | `18` | Bistro 已有台面/吧台上的额外小物数量。 |
| `max_attempts` | integer, `>=1` | `300` | 摆放采样最大尝试次数。 |

CLI 覆盖：`--min-tables`、`--max-tables`、`--floor-extras`、`--min-tabletop-items`、`--max-tabletop-items`、`--bistro-support-items`、`--max-attempts`。

## validation

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `sionna` | boolean | `false` | 是否生成后用 `sionna.rt.load_scene()` 验证 `scene.xml`。 |

CLI 覆盖：`--validate-sionna/--no-validate-sionna`。

## quality

| 字段 | 类型 / 范围 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | 是否执行质量检查。 |
| `fail_on_error` | boolean | `true` | 质量检查有 error 时命令是否返回非零。 |
| `collision_padding_m` | float, `>=0` | `0.0` | 动态物体 AABB 碰撞检查额外间距。 |
| `bistro_static_clearance_m` | float, `>=0` | `0.0` | Bistro 地面物体与静态几何的额外避让距离。 |
| `support_tolerance_m` | float, `>=0` | `0.05` | 地面/桌面/台面支撑关系高度容差。 |

CLI 覆盖：`--quality/--no-quality`、`--quality-fail-on-error/--no-quality-fail-on-error`、`--quality-collision-padding`、`--quality-bistro-static-clearance`、`--quality-support-tolerance`。

## label

| 字段 | 类型 / 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | 是否生成 `label/*.json` 和 `label/report/*_report.json`。 |
| `version` | `"1.1"` | `"1.1"` | 当前固定版本。 |
| `ue_height_m` | float, `>0` | `1.6` | UE 相对 floor 的高度。 |
| `sampling_domain` | `global_floor` / `room_floor` | `global_floor` | UE 采样域。`front3d + global_floor` 使用 2.0.0 全局矩形减法采样：先在建筑 XY bbox 网格上采样，再扣 outdoor 和膨胀后的 wall，最后按 room 归属。`room_floor` 为旧逻辑，每个 room 单独采样。 |
| `ue_strategy` | `free_space_grid` / `plane_grid` | `free_space_grid` | 单 label 兼容字段。若未显式设置 `batch_strategies`，CLI/YAML 设置该字段会同步到批量生成策略。 |
| `grid_resolution_m` | float, `>0` | `0.1` | 单 label 兼容字段。若未显式设置 `batch_grid_resolutions_m`，CLI/YAML 设置该字段会同步到批量采样间隔。 |
| `batch_strategies` | list: `free_space_grid` / `plane_grid` | `[free_space_grid]` | 批量 UE 采样策略。`free_space_grid` 使用可行走区域网格，输出名为 `label_walk_*`；`plane_grid` 在 room floor mesh 平面域内采样，输出名为 `label_panel_*`。 |
| `batch_grid_resolutions_m` | list of float, `>0` | `[0.1]` | 批量 UE 网格间隔。会与 `batch_strategies` 做笛卡尔组合，例如 `[0.1, 0.2]` 与两种策略会生成 4 份 label。 |
| `connected_area_enabled` | boolean | `true` | 单 label 兼容字段。是否保留 3D-FRONT connected area；开启时未归属 room 但仍在全局 free space 上的点进入 connected area group；关闭时只保留 room 内点。 |
| `batch_connected_area_enabled` | list of boolean | `[true]` | 批量 connected area 维度。会与 `batch_strategies`、`batch_grid_resolutions_m` 做笛卡尔组合，例如 `[true, false]` 可同时生成 connected 和 room-only 两套 label。 |
| `ue_clearance_m` | float, `>=0` | `0.35` | walk 家具过滤时对家具 bbox 的额外避让距离。 |
| `obstacle_strategy` | `height_aware` / `footprint_column` | `height_aware` | walk 家具障碍物过滤方式。`height_aware` 只过滤与 UE 高度相交的物体；`footprint_column` 按家具 XY 占地整列过滤。 |
| `walk_ignore_low_obstacles_below_m` | float, `>=0` | `0.10` | `free_space_grid` 专用。高度低于该阈值的低矮物体不作为可行走区域障碍，减少地毯、薄垫等误判。 |
| `walk_blocking_classes` | list: `table` / `seat` / `tabletop` / `floor` / `skip` | `[table, seat, floor]` | `free_space_grid` 专用。只有这些 placement class 会从 walk 区域扣除。 |
| `walk_min_component_area_m2` | float, `>=0` | `0.25` | `free_space_grid` 专用。删除面积小于该值的孤立小连通区域。 |
| `bs_strategy` | `wall_or_corner` / `geometry_center` | `wall_or_corner` | BS 位置策略。`wall_or_corner` 按房间墙边/角落布点；`geometry_center` 在建筑几何中心附近选择一个满足自由空间约束的 `BS0`。 |
| `bs_count_strategy` | `fixed_per_room` / `area_adaptive` | `fixed_per_room` | BS 数量策略。 |
| `bs_per_room` | integer, `>=0` | `4` | `fixed_per_room` 下每个 room 的 BS 数量。 |
| `bs_min_per_room` | integer, `>=0` | `1` | `area_adaptive` 下每个有效 room 最少 BS 数量。 |
| `bs_max_per_room` | integer, `>= bs_min_per_room` | `8` | `area_adaptive` 下每个有效 room 最多 BS 数量。 |
| `bs_min_room_area_m2` | float, `>=0` | `4.0` | `area_adaptive` 下小于该面积的 room 不放 BS。 |
| `bs_area_per_point_m2` | float, `>0` | `12.0` | `area_adaptive` 下每多少平方米约增加一个 BS。 |
| `bs_height_m` | float, `>0` | `2.4` | BS 目标高度。 |
| `bs_ceiling_margin_m` | float, `>=0` | `0.3` | BS 距离天花的最小距离。 |
| `bs_wall_clearance_m` | float, `>=0` | `0.25` | BS 与建筑 floor 边界的避让距离。`wall_or_corner` 和 `geometry_center` 都会使用。 |
| `bs_center_initial_radius_m` | float, `>=0` | `0.2` | `geometry_center` 搜索中心 BS 时的初始搜索半径。 |
| `bs_center_radius_step_m` | float, `>0` | `0.1` | `geometry_center` 搜索中心 BS 时的半径扩张步长。 |
| `bs_center_max_radius_m` | float, `>= initial` | `2.0` | `geometry_center` 搜索中心 BS 时的最大半径；超过后选择最近合法候选。 |
| `wall_clearance_m` | float, `>=0` | `0.25` | `room_floor` 采样域下 UE 与 room floor 边界的避让距离。 |
| `corridor_room_id` | string | `__corridor__` | `global_floor` 采样域下，不属于任何 room 但仍在 global floor 上的点使用的 room id。 |
| `corridor_room_type` | string | `ConnectedArea` | 全局采样后无法归属到具体 room 的 connected area group 的 room type。 |
| `corridor_clearance_m` | float, `>=0` | `0.05` | `global_floor` 采样域下 UE 与 global floor 边界和墙体的避让距离，默认更小以保留门洞/联通区域。 |
| `overlay_enabled` | boolean | `true` | 是否生成 label 可视化。批量图写入 `label_floorplan/`。 |
| `fail_on_error` | boolean | `true` | label 验证失败时命令是否返回非零。 |

批量 label 输出约定：

- 每个场景的所有 label JSON 写入 `label/`。只有一个 connected area 模式时沿用 `label_<strategy>_<resolution>.json`，例如 `label_panel_0p1.json`、`label_walk_0p2.json`；同时生成 connected 和 room-only 时使用 `label_<strategy>_<connected|room>_<resolution>.json`，例如 `label_panel_connected_0p1.json`、`label_panel_room_0p1.json`。
- 每份 label 对应一份报告，命名为 `label/report/<name>_report.json`。
- 每份 label 对应一张点位可视化，命名为 `label_floorplan/<name>.png`。
- root 和 group 级都包含 `bs_points`、`ue_points`、`bs_positions`、`ue_positions`。
- `bs_points` 单点坐标使用 `position: [x, y, z]`。
- `ue_points` 当前使用 `x` / `y` / `z` 字段。
- `bs_positions` / `ue_positions` 是快速读取用的坐标数组。
- report 根部包含总 `bs_count`、`ue_count`、`group_count`、`valid_room_count` 和可用时的 `ue_sampling_summary`；逐房间细节仍在 `rooms` 中。

`plane_grid` 和 `free_space_grid` 的区别：

- `plane_grid` 表示室内平面采样，只扣除 outdoor 和墙体间隔，不扣家具。
- `free_space_grid` 表示可行走区域，会在 `plane_grid` 基础上按 `obstacle_strategy` 扣除家具障碍；同时会忽略低于 `walk_ignore_low_obstacles_below_m` 的薄物体，并删除小于 `walk_min_component_area_m2` 的孤立区域。

采样域补充：

- `global_floor` 更适合 3D-FRONT。`plane_grid` 和 `free_space_grid` 都使用同一个全局矩形减法域：`Door/Hole/Pocket` 会先被标为 free space，随后墙体按 `corridor_clearance_m` 膨胀并自然收窄门洞；不做膨胀后的门洞恢复，也不在采样前做 room 分类。窗户不会作为 UE 采样开口。`connected_area_enabled: true` 时，归属不到 room 的点进入 connected area group。
- `room_floor` 更保守，适合需要严格房间内采样的实验，但门洞/联通处可能缺点。

四种组合示例：

```yaml
label:
  batch_strategies: [plane_grid, free_space_grid]
  batch_grid_resolutions_m: [0.1]
  batch_connected_area_enabled: [true, false]
```

会生成：`label_panel_connected_0p1`、`label_panel_room_0p1`、`label_walk_connected_0p1`、`label_walk_room_0p1`。

CLI 覆盖：`--label/--no-label`、`--label-version`、`--label-ue-height`、`--label-sampling-domain`、`--label-ue-strategy`、`--label-grid-resolution`、`--label-batch-strategies`、`--label-batch-grid-resolutions`、`--label-connected-area/--no-label-connected-area`、`--label-batch-connected-area-enabled`、`--label-ue-clearance`、`--label-obstacle-strategy`、`--label-walk-ignore-low-obstacles-below`、`--label-walk-blocking-classes`、`--label-walk-min-component-area`、`--label-bs-strategy`、`--label-bs-count-strategy`、`--label-bs-per-room`、`--label-bs-min-per-room`、`--label-bs-max-per-room`、`--label-bs-min-room-area`、`--label-bs-area-per-point`、`--label-bs-height`、`--label-bs-ceiling-margin`、`--label-bs-wall-clearance`、`--label-bs-center-initial-radius`、`--label-bs-center-radius-step`、`--label-bs-center-max-radius`、`--label-wall-clearance`、`--label-corridor-room-id`、`--label-corridor-room-type`、`--label-corridor-clearance`、`--label-overlay/--no-label-overlay`、`--label-fail-on-error/--no-label-fail-on-error`。

## floorplan

| 字段 | 类型 / 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | 是否生成 floorplan。 |
| `geometry_enabled` | boolean | `true` | 是否生成基于 `scene.obj` 采样的几何占据图。 |
| `geometry_clean_enabled` | boolean | `false` | 是否生成去噪后的 `geometry_clean.png`。 |
| `geometry_clean_min_density` | float | `2.0` | clean 图保留像素的最低累计采样密度。 |
| `geometry_clean_min_neighbors` | integer | `2` | clean 图保留像素所需的 8 邻域支撑像素数量。 |
| `geometry_clean_min_z_m` | float | `0.05` | clean 图忽略低于该高度的采样点。 |
| `geometry_clean_max_abs_normal_z` | float | `0.7` | clean 图保留表面法线竖直分量上限。 |
| `geometry_clean_opening_px` | integer | `0` | clean 图 opening 迭代半径。 |
| `geometry_clean_closing_px` | integer | `1` | clean 图 closing 迭代半径。 |
| `semantic_enabled` | boolean | `false` | 是否生成语义平面图。 |
| `class_mask_enabled` | boolean | `false` | 是否为 `front3d` 生成训练用四分类掩码。当前只支持 `front3d`。 |
| `class_mask_wall_dilation_m` | float, `>=0` | `0.0` | 生成四分类掩码时对 wall 类的额外膨胀距离。 |
| `class_mask_furniture_dilation_m` | float, `>=0` | `0.0` | 生成四分类掩码时对 furniture 类的额外膨胀距离。 |
| `class_mask_opening_mode` | `none` / `doors` / `windows` / `doors_and_windows` | `doors` | 哪些 3D-FRONT 开口会在墙体膨胀前标为 `free_space`。`doors` 会使用 `Door` 以及落地的 `Hole/Pocket`；`windows` 会使用 `Window/BayWindow` 以及非落地的 `Hole/Pocket`。 |
| `class_mask_opening_dilation_m` | float, `>=0` | `0.0` | 对开口 cutter 的额外膨胀距离，可用于弥合墙体栅格化误差。 |
| `class_mask_opening_floor_tolerance_m` | float, `>=0` | `0.25` | 判定 `Hole/Pocket` 是否落地的高度容差。 |
| `class_mask_opening_min_height_m` | float, `>=0` | `1.6` | `Hole/Pocket` 被当成可通行门洞/窗洞时所需的最小高度。 |
| `class_mask_include_doors_as_wall` | boolean | `true` | 是否把未被 `class_mask_opening_mode` 选中的 door mesh 归入 wall/建筑阻挡类；被选中的 opening 会在墙体膨胀前从 wall 中移除。 |
| `class_mask_include_windows_as_wall` | boolean | `true` | 是否把未被 `class_mask_opening_mode` 选中的 window mesh 归入 wall/建筑阻挡类；被选中的 opening 会在墙体膨胀前从 wall 中移除。 |
| `resolution_m_per_pixel` | float, `>0` | `0.05` | 平面图栅格分辨率。 |
| `height_mode` | `heights` / `layers` | `heights` | `heights` 渲染指定高度；`layers` 使用逐层扫描。 |
| `heights_m` | list of float | `[1.6]` | `height_mode: heights` 时的投影高度序列。 |
| `step_m` | float, `>0` | `0.2` | `height_mode: layers` 时层间隔。 |
| `top_z_m` | float / `null` | `null` | `height_mode: layers` 时扫描顶部；`null` 表示自动检测。 |
| `bottom_z_m` | float | `0.0` | 扫描底部高度。 |
| `sample_density_scale` | float, `>0` | `128.0` | 表面采样密度倍率。 |
| `min_sample_points` | integer, `>=1` | `100000` | 表面采样点数量下限。 |
| `max_sample_points` | integer, `>= min_sample_points` | `25000000` | 表面采样点数量上限。 |
| `preview_tile_size_px` | integer | `360` | 分层预览图 tile 尺寸。 |
| `semantic_padding_m` | float | `0.5` | 语义平面图场景外留白。 |
| `semantic_draw_labels` | boolean | `true` | 语义平面图是否绘制文字标签。 |
| `fail_on_error` | boolean | `true` | floorplan 失败时命令是否返回非零。 |

四分类掩码输出：

- `floorplan/class_mask.png`: `uint8` 单通道类别图，像素值固定为 `0/1/2/3`。
- `floorplan/class_mask_preview.png`: 彩色预览图，方便人工检查。
- `floorplan/class_mask.npy`: 训练读取用的原始 `uint8` 数组。
- `floorplan/class_mask.npz`: 压缩包，包含 mask、分辨率、origin 和类别名。
- `floorplan/class_mask_meta.json`: 类别 legend、像素计数、建筑 mesh 统计和参数记录。

类别固定为：`0 outdoor`、`1 wall`、`2 free_space`、`3 furniture`。生成流程为：floor 区域和选中的 opening 先成为自由空间，选中的 opening 在墙体膨胀前从 wall 中移除，墙体按配置膨胀后覆盖自由空间；不做膨胀后的 opening 恢复。最后家具在非墙的室内区域覆盖为 `furniture`。

CLI 覆盖：`--floorplan/--no-floorplan`、`--floorplan-geometry/--no-floorplan-geometry`、`--floorplan-geometry-clean/--no-floorplan-geometry-clean`、`--floorplan-geometry-clean-min-density`、`--floorplan-geometry-clean-min-neighbors`、`--floorplan-geometry-clean-min-z`、`--floorplan-geometry-clean-max-abs-normal-z`、`--floorplan-geometry-clean-opening-px`、`--floorplan-geometry-clean-closing-px`、`--semantic-floorplan/--no-semantic-floorplan`、`--floorplan-class-mask/--no-floorplan-class-mask`、`--floorplan-class-mask-wall-dilation`、`--floorplan-class-mask-furniture-dilation`、`--floorplan-class-mask-opening-mode`、`--floorplan-class-mask-opening-dilation`、`--floorplan-class-mask-opening-floor-tolerance`、`--floorplan-class-mask-opening-min-height`、`--floorplan-class-mask-include-doors-as-wall/--no-floorplan-class-mask-include-doors-as-wall`、`--floorplan-class-mask-include-windows-as-wall/--no-floorplan-class-mask-include-windows-as-wall`、`--floorplan-resolution`、`--floorplan-height-mode`、`--floorplan-heights`、`--floorplan-step`、`--floorplan-top-z`、`--floorplan-bottom-z`、`--floorplan-sample-density-scale`、`--floorplan-min-sample-points`、`--floorplan-max-sample-points`、`--floorplan-preview-tile-size`、`--floorplan-semantic-padding`、`--floorplan-semantic-draw-labels/--no-floorplan-semantic-draw-labels`、`--floorplan-fail-on-error/--no-floorplan-fail-on-error`。

## 常用片段

3D-FRONT 合成：

```yaml
pipeline:
  mode: front3d
  scenes: 3
```

UE 高密度平面采样，靠近墙和家具：

```yaml
label:
  ue_strategy: plane_grid
  ue_height_m: 1.8
  ue_clearance_m: 0.1
  wall_clearance_m: 0.1
  obstacle_strategy: height_aware
```

BS 按房间面积自适应：

```yaml
label:
  bs_count_strategy: area_adaptive
  bs_min_room_area_m2: 4.0
  bs_area_per_point_m2: 12.0
  bs_min_per_room: 1
  bs_max_per_room: 8
```

更高分辨率 floorplan：

```yaml
floorplan:
  resolution_m_per_pixel: 0.025
```
