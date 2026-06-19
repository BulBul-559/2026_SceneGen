# SceneGen Agent Handoff

这份文档是给后续对话或新 agent 快速接手用的浓缩版。更完整的说明看根目录 `README.md`、`config/README.md` 和 `docs/data_onboarding.md`。性能优化复盘看 `docs/performance_optimization_after_5015e71.md`，worker 数量和调度测试看 `docs/worker_sweep_20260616.md`。

## 项目一句话

SceneGen 是一个 Linux/uv 管理的轻量室内 3D 场景生成项目。它把空场景、资产目录或 3D-FRONT 已组合场景合成为标准输出：`scene.obj`、Sionna/Mitsuba 可加载的 `scene.xml`、`placements.json`、批量 `label/*.json`、floorplan、质量报告和统计报告。

## 当前主要模式

- `bistro`: 主实验模式。读取 `data/scene/scene.obj` 和 `data/catalogs/bistro.v1.json`，随机摆放桌椅、地面物体、桌面小物和 Bistro 已有台面小物。
- `generated`: 简单矩形房间 smoke/test 模式，保留用于快速验证生成链路。
- `front3d`: 复现并合成 3D-FRONT 原始已有组合场景。读取 `data/3D-Front/scenegen_manifest.json`，使用整理后的建筑结构和家具实例输出与 Bistro 一致的目录结构。
- `procedural_front3d`: 实验性自动生成模式。按 `procedural.layout` 采样多房间户型，默认 `mixed` 会按 `procedural.layout_weights` 在 `grid` / `split_tree` / `rect_union` / `room_graph` / `polygon_shell` / `corridor_spine` 中加权选择实际 layout，并在 scene record 里同时记录 `layout` 和 `configured_layout`；其中 `split_tree` 从完整 apartment footprint 递归切分房间，`rect_union` 可由连通房间矩形集合拼出 L/T/凹口式不规则外轮廓，外墙按 room union 的真实边界生成，不再用整体 bbox 补成矩形；`room_graph` 会先生成树状房间邻接关系，再把新房间挂到已有房间边上，形成更自由的错位和凹凸外轮廓；`polygon_shell` 会先采样带凹口的外部 shell，再在 shell 内切分矩形房间；`corridor_spine` 会生成走廊 spine + 两侧房间的公寓式拓扑，并在配置阶段检查 `Hallway`、`room_count` 和 Hallway 上限是否足够容纳必选房间。默认 room type 覆盖客厅、卧室、餐厅、书房、厨房、卫浴和走廊/玄关，并可通过 `procedural.required_room_types` 保证必选组成、通过 `procedural.room_type_weights` 控制剩余分布权重、通过 `procedural.room_type_max_counts` 限制厨房/餐厅/卫浴等类型的最大出现次数、通过 `procedural.room_type_assignment: geometry_fit` 复用 room type 几何规则把类型分配给更合适的房间。随后写出 Front3D-like 建筑 `procedural_source/scene.json` / `architecture.obj`，包括 floor、ceiling、wall、door、外墙 window mesh、room adjacency 和门洞 bbox；再按 `procedural.object_count` 计算每房家具数量，`object_count.by_room_type` 可按房型覆盖家具密度，并从 3D-FUTURE 资产池中按 `procedural.room_profiles` 的类别序列、`required_classes` 和 semantic filter 摆放家具。`procedural.asset_reuse` 默认限制同一 model 在 room/scene 内过度复用，候选池不足时可自动放宽并在 placement stats 里记录。位置采样由 `procedural.placement_policy` 控制，默认 `floor` 靠墙、`table` 靠中心、`seat` 自由采样，且 `placement_policy.by_room_type` 可按房型覆盖，例如厨房/卫浴更偏靠墙、客厅/餐厅桌椅更偏中心；客厅、餐厅、卧室、书房、厨房等可通过 `procedural.placement_groups` 先生成 anchor + companion 关系组，例如茶几/沙发、餐桌椅、床 + 床头柜、书桌椅或台面/椅凳。生成后会按 `procedural.precheck` 检查摆放数量完成率、room 必需类别是否到位、room adjacency 连通性、footprint fill ratio/凹口面积、通用 room 面积/长宽比和按 room type 的几何规则，失败时换 seed 重试同一编号；通过后复用 front3d 的 OBJ/XML、label、floorplan、class mask、quality 和 statistics 输出链路。
- `procedural_front3d_vision`: 复用 `procedural_front3d` 的户型、家具摆放、precheck、quality、label、floorplan 和 postprocess maps，但通过 `output.profile: vision_only` 跳过合成 `scene.obj`、`scene.xml`、`assets/` 和 OBJ summary。它面向 VisionEncoder 预训练数据，scene 目录前缀为 `procedural_front3d_vision_`，仍保留 `procedural_source/` 和 `placements.json` 作为小体积可追溯源记录。

`front3d` v1 只合成已有 3D-FRONT 场景，不做基于 3D-FRONT 资产池的随机重排。`procedural_front3d` 是正在开发的无限场景生成 baseline，当前仍是规则系统，不是完整 ProcTHOR/Infinigen 级别的语义约束生成器。

## 关键目录

- `src/scenegen/cli.py`: CLI 入口和主生成流程。
- `src/scenegen/config.py`: 配置默认值、YAML 读取、CLI 覆盖、类型归一化、字段和值校验。
- `src/scenegen/sources.py`: `generated`、`bistro`、`front3d`、`procedural_front3d`、`procedural_front3d_vision` 五种 source adapter。
- `src/scenegen/front3d.py`: 3D-FRONT manifest、scene JSON、实例矩阵、坐标转换和 asset 解析。
- `src/scenegen/procedural.py`: 自动生成类 Front3D 场景的第一版 baseline，包括 room layout、建筑 OBJ/JSON 写出、资产池筛选和家具摆放。
- `src/scenegen/exporters.py`: OBJ、XML、placements、manifest 和 summary 输出。
- `src/scenegen/labels.py`: BS/UE label v1.1 生成、验证和 floorplan overlay。
- `src/scenegen/floorplan.py`: 几何 floorplan 和 3D-FRONT 四分类 class mask。
- `src/scenegen/quality.py`: 质量检查和统计报告。
- `src/scenegen/runlog.py`: 单 run JSONL 事件、阶段耗时、state 快照和 traceback 输出。
- `src/scenegen/batch.py`: 生产管理入口，支持 `front3d`、`procedural_front3d` 和 `procedural_front3d_vision`，负责 scene plan、worker、resume、失败/重试队列和 batch manifest。
- `src/scenegen/assets/`: Bistro 资产契约、loader、legacy converter、材质映射。
- `tools/prepare_front3d_phase1.py`: 3D-FRONT 第一阶段离线整理脚本。
- `config/bistro.yaml`: Bistro 模式一级基础模板，也是默认 YAML 入口。
- `config/front3d.yaml`: 3D-FRONT 合成模式一级基础模板。
- `config/procedural_front3d.yaml`: 自动生成类 3D-FRONT 场景的一级基础模板。
- `config/procedural_front3d_vision.yaml`: 程序化视觉训练数据一级基础模板。
- `data/3D-Front/`: 本地 3D-FRONT 原始数据和整理结果，默认 git ignored。

## 配置链路

配置入口以 YAML 为主，CLI 可覆盖 YAML。

合并顺序：

1. `DEFAULT_CONFIG`。
2. 加载 `--config` 指定 YAML，默认是 `config/bistro.yaml`。
3. 应用 CLI `--set key.path=value` 覆盖。
4. 归一化路径和类型。
5. 校验未知字段和值范围。
6. 写出 `<run_dir>/effective_config.yaml`。

维护规则：

- `config/bistro.yaml`、`config/front3d.yaml`、`config/procedural_front3d.yaml` 和 `config/procedural_front3d_vision.yaml` 按工作流拆分，只保留各自任务相关字段；未写字段由 `DEFAULT_CONFIG` 补齐。
- `DEFAULT_CONFIG` 是完整 schema 和兜底默认值来源；新增字段时需要同步判断它属于哪个一级模板或具体 `config/tasks/*.yaml`，并更新 `config/README.md`。
- 需要实验配置时复制对应模板到其他位置，或通过 CLI 覆盖。
- 配置 v2 不兼容旧 YAML 字段和旧显式 CLI 参数。
- YAML 或 `--set` 写错字段会直接报错，不应静默忽略。

## 当前默认重点参数

`floorplan` 默认用于训练输入的 raw 几何投影：

- `floorplan.resolution_m: 0.05`
- `floorplan.geometry.projection: sampling`
- `floorplan.geometry.height.mode: heights`
- `floorplan.geometry.height.values_m: [1.6]`
- `floorplan.sampling.density_scale: 128.0`
- `floorplan.sampling.max_points: 4000000`
- `floorplan.class_mask.enabled: false`: 需要训练用四分类掩码时在 `front3d` 模式打开。输出 `floorplan/class_mask.png`、`class_mask_preview.png`、`class_mask.npy`、`class_mask.npz` 和 `class_mask_meta.json`，类别固定为 `0 outdoor`、`1 wall`、`2 free_space`、`3 furniture`。开口策略由共享 `front3d.openings.mode` 控制，默认 `doors`。家具层支持 `floorplan.class_mask.furniture_mode: bbox | mesh`，`mesh` 会加载家具 OBJ、应用实例 transform 并做高度过滤三角面投影生成像素级 footprint；`class_mask_meta.json` 记录内部阶段耗时。
- `floorplan.geometry.projection: ray_height_filtered`: 可选确定性高度过滤投影，不依赖随机表面采样密度，适合生成更稳定的像素级几何占据图。

`front3d` 默认策略：

- `arch_variant: normalized`: 建筑结构用 phase1 的 Z-up normalized 结果。
- `object_variant: raw`: 家具用 3D-FUTURE raw 模型，因为 3D-FRONT 原始位姿按 raw 尺寸设计。
- `positive_xy: true`: 整体平移到 XY 正象限，floorplan 左下保持 `(0, 0)`。
- `ground: true`: 家具 bbox 低于地面时做轻量 Z 抬升。
- `precheck.enabled: true`: 生成正式 label/floorplan 前先检查候选场景；家具过少、Z 范围异常或投影占比异常时跳过该 scene id 并自动补齐。

`label` 默认策略：

- label 输出版本由代码固定为 `1.1`。
- `ue.height_m: 1.6`
- `ue.sampling.domain: global_floor`
- `ue.sampling.strategies: [walk]`
- `ue.sampling.grid_m: [0.1]`
- `ue.sampling.mask_resolution_m: 0.05`
- `ue.sampling.wall_clearance_m: 0.2`
- `ue.walk.obstacle_strategy: below_ue_column`
- `ue.connected_area.room_id: "__corridor__"`
- `bs.strategy: wall_or_corner`
- `bs.count.strategy: fixed_per_room`
- `bs.count.per_room: 4`
- `bs.wall_clearance_m: 0.2`
- `overlay.enabled: true`

`label.ue.sampling.domain: global_floor` 是当前 front3d 推荐策略：`panel` 和 `walk` 先用固定的 `label.ue.sampling.mask_resolution_m` 在建筑 XY bbox 上构建全局可行域 mask，再按 `label.ue.sampling.grid_m` 抽样，随后扣 outdoor 和膨胀后的 wall，最后按 room floor mesh 分类，未归属点进入 `ConnectedArea` group。门洞/窗洞由 `front3d.openings` 统一控制。当前不再生成 room-only 版本，也不丢弃 connected area residual 点；需要严格旧行为时切换为 `room_floor`。单基站定位实验可用 `label.bs.center.enabled: true`，它会在普通 BS 之外生成一个建筑几何中心附近的 `BS_CENTER`。

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
uv run scenegen --config config/front3d.yaml --set pipeline.scenes=3 --set pipeline.run_name=front3d_preview
```

自动生成类 3D-FRONT 场景：

```bash
uv run scenegen --config config/procedural_front3d.yaml --set pipeline.scenes=1 --set pipeline.run_name=procedural_front3d_preview
```

正式 front3d batch 生产：

```bash
uv run scenegen-batch \
  --config config/tasks/front3d_full_simulation.yaml \
  --set pipeline.run_name=front3d_full_6813
```

`config/tasks/front3d_full_simulation.yaml` 是专门合成 Front3D 全量场景的生产模板，只保留 Front3D 相关配置。它默认 `pipeline.scenes: 6813`、`front3d.select: sequential`、`batch.workers: 24`、`batch.scheduler: hybrid`、`batch.task_timeout_s: 600`、`postprocess.maps.enabled: true`，label 默认生成 `[panel, walk] * [0.1, 0.2, 0.5]`。

正式 procedural_front3d batch 生产：

```bash
uv run scenegen-batch \
  --config config/tasks/procedural_front3d_full_simulation.yaml \
  --workers 4 \
  --scheduler hybrid \
  --max-retries 1 \
  --set pipeline.scenes=2000 \
  --set pipeline.run_name=procedural_front3d_production_2000
```

`config/tasks/procedural_front3d_full_simulation.yaml` 是随机生成数据的任务级模板，只保留 3D-FUTURE 家具池来源、`procedural` 户型/摆放规则、label、floorplan、postprocess 和 batch 等相关字段；不包含 Bistro 配置，也不包含复现已有 Front3D 场景时才需要的 scene selection / precheck 字段。它默认 `batch.workers: 24`、`batch.task_timeout_s: 600`、`postprocess.maps.enabled: true`，BS 数量使用和 Front3D 全量模板一致的 `area_adaptive` 面积自适应策略。

正式 procedural_front3d_vision batch 生产：

```bash
uv run scenegen-batch \
  --config config/tasks/procedural_front3d_vision_full_simulation.yaml \
  --set pipeline.scenes=2000 \
  --set pipeline.run_name=procedural_front3d_vision_production_2000
```

`config/tasks/procedural_front3d_vision_full_simulation.yaml` 是随机生成视觉训练数据的任务级模板，默认 `output.profile: vision_only`、`batch.workers: 48`，并显式固定当前生产用 `procedural` 户型与家具摆放参数；不写合成 `scene.obj`、`scene.xml`、`assets/` 或 OBJ summary，但仍生成 `procedural_source/`、`placements.json`、`label_panel_0p5`、floorplan/class mask 和 batch 后处理 maps。

`scenegen-batch` 默认读取 YAML 的 `batch.workers`、`batch.scheduler` 和 `batch.max_retries`；命令行 `--workers`、`--scheduler`、`--max-retries` 可临时覆盖。`hybrid` 调度会先固定分片，只有当 worker 自己队列清空且其他队列仍有待处理任务时，才从剩余任务最多的队列偷取尾部任务；`static` 严格保持固定分片；`dynamic` 使用共享任务队列。batch child 会设置 `runtime.skip_summary=true`，跳过自己的 `summary/` 汇总复制，最终由 batch 顶层统一生成 summary。成功 scene 发布时会从 `batch/worker_runs` move 到 run 根目录，worker 子 run 不再保留成功场景的完整重复副本，只保留日志、配置、小 manifest 和失败场景调试材料。batch 完成后会写 `manifest.json`、`manifest_batch.json` 和 `manifest_<mode>.json`。

正式生产模板会在 batch 末尾自动生成 derived maps；如果还要整理 compact vision dataset：

```bash
uv run scenegen-batch \
  --config config/tasks/front3d_full_simulation.yaml \
  --set pipeline.run_name=front3d_full_6813_maps \
  --set postprocess.dataset.enabled=true \
  --set postprocess.maps.bs_label.mode=name \
  --set postprocess.maps.bs_label.name=label_panel_0p1
```

`postprocess` 是 batch-only 配置；基础配置默认关闭，full 生产模板默认开启 maps。Front3D/procedural full 模板默认使用 `maps.bs_label.name=label_panel_0p1`；vision-only 生产模板只生成 `label_panel_0p5`，并用 `maps.bs_label.name=label_panel_0p5` 作为 BS 来源，避免把不同 UE 采样密度和策略的 label variant 中的 BS 重复合并。

同名任务恢复：

```bash
uv run scenegen-batch \
  --config config/tasks/front3d_full_simulation.yaml \
  --resume \
  --set pipeline.run_name=front3d_full_6813
```

快速 smoke，不生成 floorplan：

```bash
uv run scenegen --set pipeline.mode=generated --set pipeline.scenes=1 --set pipeline.run_name=smoke_generated --set floorplan.enabled=false
```

可选 Sionna XML 验证：

```bash
uv run scenegen --set pipeline.scenes=1 --set validation.sionna=true
```

## 标准输出结构

一次 run 默认写到 `results/<run_name>/`：

```text
effective_config.yaml
manifest.json
manifest_<mode>.json
statistics.json
visual_index.html
logs/
  events.jsonl
  timings.jsonl
  state/run_state.json
  workers/
  scenes/
summary/
  obj/
  floorplan/
  label_floorplan/
    0p1/
    0p5/
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
    preview.png
    side_view.png
    stack.npz
    meta.json
```

`visual_index.html` 是 run 级可视化索引页，会汇总每个 scene 的主 floorplan、class mask preview 和 label overlay，方便快速人工验图。

`front3d` 的 `placements.json` 中 `sionna_assets.timings_s` 会记录 Sionna XML 资产导出的细分耗时，包括建筑拆分、家具资产拆分和 XML 写入，用于定位 `build_scene` 阶段瓶颈。

`procedural_front3d` 的 scene record 中 `procedural.rooms[*].profile` / `profile_classes` 会记录每个房间使用的 furniture profile；`procedural.asset_pool_coverage` 会记录每个 room type + furniture class 在 semantic filter 后的候选资产数、空候选和 filter fallback；`procedural.footprint` 会记录 room union 面积、bbox 面积、fill ratio 和凹口面积，便于筛选 `rect_union` / `room_graph` / `polygon_shell` 这类不规则户型；`procedural.topology` 会记录 room graph 的 edge/component、degree、leaf/isolated/branch room 和 graph diameter，便于后续按户型连通复杂度筛选；`procedural.precheck` 可用 `min/max_footprint_fill_ratio` 和 `min/max_footprint_concavity_m2` 过滤外轮廓过稀、过满或凹口异常的样本，也可用 `min/max_topology_*` 阈值过滤 room graph 边数、叶子房间、分支房间和图直径，可用 `min_class_placement_ratio` 控制 table/seat/floor 等类别完成率，还可用 `min_unique_model_ratio` / `max_duplicate_model_count` 控制单场景资产复用程度；`procedural.adjacency` 会记录相邻 room、共享墙方向、门洞 bbox 和门中心点；`procedural.timings_s` 和 `procedural.placement_stats` 会记录 layout、建筑 mesh/source 写出、家具摆放、每房间 desired/placed/skipped 计数、desired/placed/skipped 资产类别分布、按 room type 统计的实际摆放类别、唯一模型比例、重复模型数量、跳过原因、关系组尝试/成功次数、候选尝试次数、精确 bbox 计算次数、粗略碰撞拒绝次数和 `door_keepout_reject_count`。run 根目录的 `procedural_report.json` 会汇总实际 layout/配置 layout 计数、room count/type/area/aspect、footprint fill ratio/凹口面积、topology degree/leaf/component/diameter、adjacency/window、家具完成率、资产池覆盖率、资产类别分布、资产多样性、关系组成功次数、precheck 通过率、room type 几何问题统计和 precheck skipped attempts 的 layout/error code 汇总；资产池覆盖率也会单独写入 `procedural_asset_pool_coverage.json`，适合批量生产后快速质检。当前摆放阶段先用资产目录尺寸做近似 footprint 过滤，只有候选通过房间边界、门洞 keepout 和粗略避碰后才加载 OBJ 计算精确 bbox；这是程序化模式第一轮性能优化的关键。

`label.overlays[*].timings_s` 会记录每张 label floorplan overlay 的读入、画布准备、点位绘制和缩放保存耗时。当前 overlay PNG 使用无损快速压缩级别 `png_compress_level: 1`；全配置下 overlay 通常瓶颈在 `resize_save`，不是 UE/BS 点绘制。

`floorplan_*.png`、`floorplan/preview.png`、`side_view.png` 和 class mask PNG 也使用无损快速压缩级别 `png_compress_level: 1` 写出。该设置只影响 PNG 编码时间和文件字节，不改变像素内容；实测收益很小，主要用于减少保存 preview / mask 时的固定开销。

`scenegen-batch` 额外输出：

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
  logs/queues/failures.jsonl
  logs/queues/retry.jsonl
  logs/queues/dead_letter.jsonl
  logs/scenes/<task>/attempt_*/task.traceback.*
  worker_runs/
manifest_batch.json
```

开启 `postprocess.maps.enabled` 后，每个 scene 下会有 `maps/geometry.npz`、`maps/pair_cache.npz`、`maps/metadata.json`。`pair_cache.npz` 是 VisionEncoder 预训练用的 BS-UE pair label cache，字段包含 `bs_xy_px`、`ue_xy_px`、`bs_xy_m`、`ue_xy_m`、`pair_los`、`pair_wall_count`、`pair_distance_m`、`pair_valid_mask`、`bs_index`、`ue_index`、`wall_count_raw`、`bucket`、`bs_snap_distance_m` 和 `bs_snapped`；旧版 dense `maps/propagation.npz` 默认关闭，可通过 `postprocess.maps.write_propagation=true` 额外写出。开启 `postprocess.dataset.enabled` 后，默认输出 `datasets/<run_name>_vision/`，每个样本只保留 `floorplan.png`、`mask.npy/png`、`mask_preview.png`、`geometry.npz`、`pair_cache.npz`、`label_bs.json` 和 `metadata.json`，`manifest.jsonl` 会记录 `image_path`、`mask_path`、`geometry_path`、`pair_cache_path`、尺寸、分辨率和 `split`。

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
- `--clean` / `pipeline.clean=true` 只清理当前 `output_dir/run_name`；不会清空整个 `output_dir`。
- 质量检查默认开启；`quality.fail_on_error: true` 时发现 error 会让命令返回非零，但仍会写出报告。
- semantic floorplan 和 geometry clean 已移除；当前主训练输入优先使用高密度几何高度层投影和可选 class mask。
- 3D-FRONT 的电磁材质目前主要靠类别/材质名映射，低置信度结果需要后续抽样校正。

## 后续方向

活跃 TODO 统一维护在 `docs/todo/README.md`。当前优先方向包括统一 label/class mask 中间结果、完善 dataset merge/quality filtering、增加 LoS/NLoS 统计和 RF proxy maps、基于 3D-FRONT 资产池随机生成场景，以及更细的材质标注审核。
