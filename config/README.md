# SceneGen Config v2 Reference

`config/` 下提供两个模式专用模板：`bistro.yaml` 和 `front3d.yaml`。CLI 默认读取 `config/bistro.yaml`；需要合成 3D-FRONT 时使用 `--config config/front3d.yaml`。模板只保留对应模式常用的配置段，未写出的字段由代码内置默认值补齐。配置 v2 是破坏性版本：旧 YAML 字段和旧显式 CLI 参数不再兼容，未知字段会直接报错。

每次运行都会在 run 目录写出 `effective_config.yaml`，它记录最终真正生效的配置。

`config/tasks/front3d_full_simulation.yaml` 是后续大规模 front3d 仿真的任务模板，默认打开 label、geometry sampling floorplan、class mask 和 mesh furniture mask；`label.ue.sampling.strategies` 使用 `[panel, walk]`，`label.ue.sampling.grid_m` 使用 `[0.1, 0.2, 0.4, 0.5]` 四档。label 可行域 mask 固定用 `label.ue.sampling.mask_resolution_m` 构建，默认 `0.05m`，再从这张高精度 mask 中抽取不同 `grid_m` 的 UE 点。它还包含 batch-only 的 `postprocess` 段，默认关闭，需要时可生成 derived maps 并构建 compact vision dataset。

## 合并规则

配置优先级从低到高：

1. 代码内置默认值 `DEFAULT_CONFIG`
2. `--config` 指定的 YAML，默认 `config/bistro.yaml`
3. 一个或多个 CLI `--set key.path=value`
4. 路径和类型归一化
5. 字段名和取值校验

`--set` 的 value 使用 YAML 解析，支持 boolean、number、string、list：

```bash
uv run scenegen \
  --config config/front3d.yaml \
  --set pipeline.scenes=5 \
  --set label.ue.height_m=1.8 \
  --set 'label.ue.sampling.grid_m=[0.1,0.2,0.5]'
```

## pipeline

| 字段 | 可选值 / 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `mode` | `bistro` / `generated` / `front3d` | `bistro` | 生成模式。 |
| `scenes` | integer, `>=1` | `10` | 本次生成场景数量。 |
| `seed` | integer | `20260517` | 主随机种子。 |
| `output_dir` | path | `results` | run 输出根目录。 |
| `run_name` | string / `null` | `null` | run 目录名；为 `null` 时使用时间戳。 |
| `clean` | boolean | `false` | 同名 run 已存在时，是否只清理 `output_dir/run_name` 后重新生成；不会清理整个 `output_dir`。 |
| `index_start` | integer, `>=0` | `0` | 输出场景编号起点；例如设为 `3000` 时生成 `front3d_3000` 起的目录。 |

## assets

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `catalog` | path | `data/catalogs/bistro.v1.json` | Bistro/generated 使用的资产 catalog；`front3d` 不依赖这个 catalog。 |

## bistro

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `base_dir` | path | `data/scene` | 空 Bistro 场景目录，至少包含 `scene.obj`。 |
| `forbidden_xy` | list of `[x_min, y_min, x_max, y_max]` | 两个 Bistro 禁区 | Bistro 模式地面摆放禁区。 |

## front3d

| 字段 | 可选值 / 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `manifest` | path | `data/3D-Front/scenegen_manifest.json` | 3D-FRONT phase1 整理输出总 manifest。 |
| `source_dir` | path | `data/3D-Front/3D-FRONT` | 原始 3D-FRONT scene JSON 目录。 |
| `arch_variant` | `normalized` / `raw` | `normalized` | 建筑结构使用的整理版本。 |
| `object_variant` | `raw` / `normalized` | `raw` | 室内物体使用的整理版本。 |
| `scene_ids` | list of string | `[]` | 指定合成 scene id；为空时按 `select` 选择。 |
| `select` | `random` / `sequential` | `random` | 自动选场景策略。 |
| `start_index` | integer, `>=0` | `0` | `select: sequential` 时从 manifest 第几个 scene id 开始选择；用于补跑时避免和前一批重复。 |
| `use_replace_jid` | boolean | `true` | child 有 `replace_jid` 时优先使用 replacement 模型。 |
| `skip_missing_objects` | boolean | `true` | 缺失家具模型时跳过并记录。 |
| `positive_xy` | boolean | `true` | 将合成场景整体平移到 XY 正象限。 |
| `ground` | boolean | `true` | 家具 bbox 低于 floor 时做轻量 Z 抬升。 |

### front3d.precheck

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | 对候选 scene 做轻量异常预检，失败则跳过并补齐。 |
| `max_attempts_per_scene` | integer, `>=1` | `20` | 每个输出编号最多尝试多少个候选 scene。 |
| `min_placements` | integer, `>=0` | `1` | 至少保留多少个家具实例。 |
| `max_z_m` | float, `>0` | `8.0` | 候选家具 bbox 最大 Z 阈值。 |
| `max_footprint_ratio` | float, `>0` | `5.0` | 家具总投影面积 / 建筑 bbox 面积阈值。 |

### front3d.openings

这组字段供 label 全局采样和 floorplan 四分类 mask 共用，用来判定门洞/窗洞如何从墙体中扣除。

| 字段 | 可选值 / 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `mode` | `none` / `doors` / `windows` / `doors_and_windows` | `doors` | 哪些开口标为 free space。 |
| `dilation_m` | float, `>=0` | `0.0` | 对 opening cutter 的额外膨胀。 |
| `floor_tolerance_m` | float, `>=0` | `0.25` | 判断 `Hole/Pocket` 是否落地的容差。 |
| `min_height_m` | float, `>=0` | `1.6` | `Hole/Pocket` 被当成开口所需最小高度。 |
| `include_doors_as_wall` | boolean | `true` | 未被 `mode` 选中的门是否仍按墙/建筑阻挡处理。 |
| `include_windows_as_wall` | boolean | `true` | 未被 `mode` 选中的窗是否仍按墙/建筑阻挡处理。 |

## placement

只影响 `bistro` 和 `generated` 的随机摆放。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `tables` | `[min, max]` | `[4, 8]` | 每场桌子数量范围。 |
| `floor_extras` | integer, `>=0` | `6` | 地面额外物体数量。 |
| `tabletop_items` | `[min, max]` | `[3, 9]` | 每张桌面小物数量范围。 |
| `bistro_support_items` | integer, `>=0` | `18` | Bistro 已有台面/吧台上的额外小物数量。 |
| `max_attempts` | integer, `>=1` | `300` | 摆放采样最大尝试次数。 |

## validation

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `sionna` | boolean | `false` | 是否用 `sionna.rt.load_scene()` 验证 `scene.xml`。 |

## postprocess

`postprocess` 只由 `scenegen-batch` 使用；普通 `scenegen` 单场景入口不会执行这部分。默认全部关闭，因此不会改变原来的主生成流程。

### postprocess.maps

| 字段 | 可选值 / 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | `false` | 是否在 batch 场景生成后为成功 scene 生成 `maps/geometry.npz`、`maps/propagation.npz` 和 `maps/metadata.json`。 |
| `workers` | integer / `null` | `null` | maps 阶段 worker 数；为 `null` 时继承 `scenegen-batch --workers`。 |
| `scene_glob` | glob string | `front3d_*` | 在 run 目录中选择 scene 目录。 |
| `overwrite` | boolean | `false` | 已有完整 `maps/` 时是否重新生成；`false` 可用于 resume。 |
| `r_max_m` | float, `>0` | `3.0` | SDF 裁剪半径。 |
| `los_stride_px` | integer, `>=1` | `4` | LoS / wall-count 监督图的 UE 网格下采样步长。 |
| `snap_radius_m` | float, `>=0` | `0.25` | BS 落到非 free-like 像素时，吸附到最近有效像素的最大半径。 |

### postprocess.maps.bs_label

| 字段 | 可选值 / 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `mode` | `first` / `name` / `glob` | `first` | 选择哪个 label 文件作为 BS 来源。正式数据集建议用 `name`。 |
| `name` | string / `null` | `null` | `mode: name` 时使用的 label 文件名或 stem，例如 `label_panel_0p1`。 |
| `glob` | string / `null` | `null` | `mode: glob` 时的匹配模式，例如 `label_panel_*.json`。 |

### postprocess.dataset

| 字段 | 可选值 / 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | `false` | 是否把 run 结果整理成 compact vision dataset。 |
| `output_dir` | path | `datasets` | dataset 输出根目录。 |
| `name` | string / `null` | `null` | dataset 目录名；为 `null` 时使用 `<run_name>_vision`。 |
| `scene_glob` | glob string | `front3d_*` | 参与 dataset 构建的 scene 目录。 |
| `require_maps` | boolean | `true` | 是否要求每个 scene 已有 `maps/geometry.npz` 和 `maps/propagation.npz`。 |
| `overwrite` | boolean | `false` | dataset 中已有同名 scene 目录时是否覆盖。 |

## quality

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | 是否执行质量检查。 |
| `fail_on_error` | boolean | `true` | 质量检查有 error 时命令是否返回非零。 |
| `collision_padding_m` | float, `>=0` | `0.0` | 动态物体 AABB 碰撞检查额外间距。 |
| `bistro_static_clearance_m` | float, `>=0` | `0.0` | Bistro 地面物体与静态几何的避让距离。 |
| `support_tolerance_m` | float, `>=0` | `0.05` | 地面/桌面/台面支撑关系高度容差。 |

## label

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | 是否生成 `label/*.json`。 |
| `fail_on_error` | boolean | `true` | label 验证失败时命令是否返回非零。 |

### label.ue

| 字段 | 可选值 / 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `height_m` | float, `>0` | `1.6` | UE 相对 floor 的高度。 |

### label.ue.sampling

批量 label 入口。最终 label 数量为 `strategies * grid_m` 的笛卡尔积。

| 字段 | 可选值 / 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `domain` | `global_floor` / `room_floor` | `global_floor` | UE 采样域。`global_floor` 先全局采样再按 room 分类；`room_floor` 逐 room 采样。 |
| `grid_m` | list of float, `>0` | `[0.1]` | UE 网格间隔。 |
| `mask_resolution_m` | float, `>0` | `0.05` | 生成 label 可行区域 mask 的固定分辨率。不同 `grid_m` 只在这张 mask 上抽样，避免低密度采样时墙/门洞腐蚀不稳定。 |
| `wall_clearance_m` | float, `>=0` | `0.2` | UE 与墙/边界的避让距离。 |
| `min_component_area_m2` | float, `>=0` | `0.25` | 删除小于该面积的孤立自由空间。 |
| `strategies` | list: `panel` / `walk` | `[walk]` | `panel` 不扣家具；`walk` 扣除家具自由空间。 |

### label.ue.walk

只影响 `sampling.strategies` 中的 `walk`。

| 字段 | 可选值 / 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `furniture_clearance_m` | float, `>=0` | `0.1` | walk 策略扣除家具时对 bbox 的额外避让距离。 |
| `obstacle_strategy` | `below_ue_column` / `height_aware` / `footprint_column` | `below_ue_column` | walk 家具障碍物过滤方式。`below_ue_column` 会扣除 UE 高度以下超过低物体阈值的家具 footprint；`height_aware` 只检查 UE 所在高度层；`footprint_column` 会扣除整列 footprint。 |
| `ignore_low_obstacles_below_m` | float, `>=0` | `0.10` | walk 策略忽略低矮物体的高度阈值。 |
| `blocking_classes` | list | `[table, seat, floor]` | 哪些 placement class 会阻挡 walk UE。 |

### label.ue.connected_area

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `room_id` | string | `__corridor__` | 归属不到具体 room 的连通区域 id。 |
| `room_type` | string | `ConnectedArea` | 连通区域类型名。 |

命名规则：

- `label_panel_0p1`
- `label_walk_0p2`

`global_floor` 采样会始终保留未归属到具体 room 的 connected area group，不再生成 room-only 版本。

### label.bs

| 字段 | 可选值 / 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `strategy` | `wall_or_corner` | `wall_or_corner` | 普通 BS 位置策略。 |
| `height_m` | float, `>0` | `2.4` | BS 目标高度。 |
| `ceiling_margin_m` | float, `>=0` | `0.3` | BS 距离天花的最小距离。 |
| `wall_clearance_m` | float, `>=0` | `0.2` | BS 与墙/边界的避让距离。 |

### label.bs.count

| 字段 | 可选值 / 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `strategy` | `fixed_per_room` / `area_adaptive` | `fixed_per_room` | BS 数量策略。 |
| `per_room` | integer, `>=0` | `4` | 固定策略下每个 room 的 BS 数量。 |
| `min_per_room` | integer, `>=0` | `1` | 面积自适应策略下有效 room 最少 BS 数量。 |
| `max_per_room` | integer, `>= min_per_room` | `8` | 面积自适应策略下有效 room 最多 BS 数量。 |
| `min_room_area_m2` | float, `>=0` | `4.0` | 小于该面积的 room 不放 BS。 |
| `area_per_point_m2` | float, `>0` | `12.0` | 每多少平方米约增加一个 BS。 |

### label.bs.center

额外中心 BS 开关。启用后会在普通 BS 之外生成一个 `BS_CENTER`，用于单基站定位评估。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | 是否额外生成中心 BS。 |
| `initial_radius_m` | float, `>=0` | `0.2` | 初始搜索半径。 |
| `radius_step_m` | float, `>0` | `0.1` | 半径扩张步长。 |
| `max_radius_m` | float, `>= initial` | `2.0` | 最大搜索半径。 |

### label.overlay

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | 是否输出 `label_floorplan/*.png` 点位叠加图。 |

## floorplan

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | 是否生成 floorplan。 |
| `fail_on_error` | boolean | `true` | floorplan 失败时命令是否返回非零。 |
| `resolution_m` | float, `>0` | `0.05` | 平面图栅格分辨率。 |

### floorplan.geometry

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | 是否生成基于 `scene.obj` 的几何占据图。 |
| `projection` | `sampling` / `ray_height_filtered` | `sampling` | 几何投影方式。`sampling` 使用面积加权随机表面采样；`ray_height_filtered` 是确定性的高度过滤 column 投影。 |

### floorplan.geometry.height

| 字段 | 可选值 / 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `mode` | `heights` / `layers` | `heights` | 指定高度或逐层扫描。 |
| `values_m` | list of float | `[1.6]` | `mode: heights` 时的投影高度。 |
| `step_m` | float, `>0` | `0.2` | `mode: layers` 时层间隔。 |
| `top_m` | float / `null` | `null` | `mode: layers` 时扫描顶部；`null` 表示自动检测。 |
| `bottom_m` | float | `0.0` | 扫描底部高度。 |

### floorplan.class_mask

仅 `front3d` 支持。开口判定使用共享的 `front3d.openings`。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | boolean | `false` | 是否生成四分类训练掩码。 |
| `wall_dilation_m` | float, `>=0` | `0.0` | wall 类额外膨胀距离。 |
| `furniture_dilation_m` | float, `>=0` | `0.05` | furniture 类额外膨胀距离。 |
| `furniture_mode` | `bbox` / `mesh` | `mesh` | furniture 类生成方式。`mesh` 加载家具 OBJ、应用实例 transform 后做高度过滤三角面投影，生成像素级 footprint；`bbox` 使用每个家具的 XY 包围盒，速度更快但更粗。 |
| `furniture_height_m` | float / `null` | `null` | 仅 `furniture_mode: mesh` 生效。`null` 表示投影家具全高度；数字表示只统计 `0 <= z <= furniture_height_m` 的家具几何。 |

四分类固定为：`0 outdoor`、`1 wall`、`2 free_space`、`3 furniture`。

### floorplan.sampling

只影响 `floorplan.geometry.projection: sampling`。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `density_scale` | float, `>0` | `128.0` | 表面采样密度倍率。 |
| `min_points` | integer, `>=1` | `100000` | 表面采样点数量下限。 |
| `max_points` | integer, `>= min_points` | `4000000` | 表面采样点数量上限。 |

### floorplan.preview

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `tile_size_px` | integer, `>=1` | `360` | 分层预览图 tile 尺寸。 |

## 常用示例

生成 1 个 3D-FRONT 场景：

```bash
uv run scenegen \
  --config config/front3d.yaml \
  --set pipeline.scenes=1 \
  --set pipeline.run_name=front3d_smoke
```

同时生成 panel/walk、三种 UE 间隔：

```bash
uv run scenegen \
  --config config/front3d.yaml \
  --set 'label.ue.sampling.strategies=[panel,walk]' \
  --set 'label.ue.sampling.grid_m=[0.1,0.2,0.5]'
```

打开四分类 mask 并把门洞和窗洞都作为 free space：

```bash
uv run scenegen \
  --config config/front3d.yaml \
  --set floorplan.class_mask.enabled=true \
  --set front3d.openings.mode=doors_and_windows
```

使用确定性 height-filtered 几何投影：

```bash
uv run scenegen \
  --config config/front3d.yaml \
  --set floorplan.geometry.projection=ray_height_filtered
```

使用正式生产模板跑 4 worker batch：

```bash
uv run scenegen-batch \
  --config config/tasks/front3d_full_simulation.yaml \
  --workers 4 \
  --max-retries 1 \
  --set pipeline.scenes=2000 \
  --set pipeline.run_name=front3d_production_2000
```

`scenegen-batch` 不是新的 YAML 字段，而是生产管理入口。它会复用同一份配置和 `--set` 语法，并在 run 目录写出 `batch/scene_plan.jsonl`、`batch/state.json`、worker 日志、失败队列、重试队列和 `manifest_batch.json`。

在同一次 batch 后自动生成 maps 和 compact vision dataset：

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

开启后，run 目录会额外写出 `derived_maps_manifest.jsonl`、`derived_maps_report.json`、`batch/postprocess_state.json`、`batch/postprocess_events.jsonl`、`batch/postprocess_failures.jsonl` 和 `batch/postprocess_report.json`；dataset 默认输出到 `datasets/<run_name>_vision/`。
