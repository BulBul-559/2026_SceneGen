# SceneGen Agent Handoff

这份文档是给后续对话或新 agent 快速接手用的浓缩版。更完整的说明看根目录 `README.md`、`config/README.md` 和 `docs/data_onboarding.md`。性能优化复盘看 `docs/performance_optimization_after_5015e71.md`，worker 数量和调度测试看 `docs/worker_sweep_20260616.md`。

## 项目一句话

SceneGen 是一个 Linux/uv 管理的轻量室内 3D 场景生成项目。它把空场景、资产目录或 3D-FRONT 已组合场景合成为标准输出：`scene.obj`、Sionna/Mitsuba 可加载的 `scene.xml`、`placements.json`、批量 `label/*.json`、floorplan、质量报告和统计报告。

## 当前主要模式

- `bistro`: 主实验模式。读取 `data/scene/scene.obj` 和 `data/catalogs/bistro.v1.json`，随机摆放桌椅、地面物体、桌面小物和 Bistro 已有台面小物。
- `generated`: 简单矩形房间 smoke/test 模式，保留用于快速验证生成链路。
- `front3d`: 复现并合成 3D-FRONT 原始已有组合场景。读取 `data/3D-Front/scenegen_manifest.json`，使用整理后的建筑结构和家具实例输出与 Bistro 一致的目录结构。
- `procedural_front3d`: 实验性自动生成模式。随机采样多房间矩形户型，写出 Front3D-like 建筑 `procedural_source/scene.json` / `architecture.obj`，再从 3D-FUTURE 资产池中按 placement class 和简单 room 语义摆放家具，复用 front3d 的 OBJ/XML、label、floorplan、class mask、quality 和 statistics 输出链路。

`front3d` v1 只合成已有 3D-FRONT 场景，不做基于 3D-FRONT 资产池的随机重排。`procedural_front3d` 是正在开发的无限场景生成 baseline，当前仍是规则系统，不是完整 ProcTHOR/Infinigen 级别的语义约束生成器。

## 关键目录

- `src/scenegen/cli.py`: CLI 入口和主生成流程。
- `src/scenegen/config.py`: 配置默认值、YAML 读取、CLI 覆盖、类型归一化、字段和值校验。
- `src/scenegen/sources.py`: `generated`、`bistro`、`front3d`、`procedural_front3d` 四种 source adapter。
- `src/scenegen/front3d.py`: 3D-FRONT manifest、scene JSON、实例矩阵、坐标转换和 asset 解析。
- `src/scenegen/procedural.py`: 自动生成类 Front3D 场景的第一版 baseline，包括 room layout、建筑 OBJ/JSON 写出、资产池筛选和家具摆放。
- `src/scenegen/exporters.py`: OBJ、XML、placements、manifest 和 summary 输出。
- `src/scenegen/labels.py`: BS/UE label v1.1 生成、验证和 floorplan overlay。
- `src/scenegen/floorplan.py`: 几何 floorplan 和 3D-FRONT 四分类 class mask。
- `src/scenegen/quality.py`: 质量检查和统计报告。
- `src/scenegen/runlog.py`: 单 run JSONL 事件、阶段耗时、state 快照和 traceback 输出。
- `src/scenegen/batch.py`: 生产管理入口，支持 `front3d` 和 `procedural_front3d`，负责 scene plan、worker、resume、失败/重试队列和 batch manifest。
- `src/scenegen/assets/`: Bistro 资产契约、loader、legacy converter、材质映射。
- `tools/prepare_front3d_phase1.py`: 3D-FRONT 第一阶段离线整理脚本。
- `config/bistro.yaml`: Bistro 专用模板，也是默认 YAML 入口。
- `config/front3d.yaml`: 3D-FRONT 专用模板。
- `config/procedural_front3d.yaml`: 自动生成类 3D-FRONT 场景的实验模板。
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

- `config/bistro.yaml`、`config/front3d.yaml` 和 `config/procedural_front3d.yaml` 是模式专用覆盖文件，不需要包含无关模式配置。
- `DEFAULT_CONFIG` 仍是完整 schema；模板缺失的字段会在配置合并时由默认值补齐。
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
  --workers 4 \
  --scheduler hybrid \
  --max-retries 1 \
  --set pipeline.scenes=2000 \
  --set pipeline.run_name=front3d_production_2000
```

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

`scenegen-batch --scheduler hybrid` 是默认调度策略：先固定分片，只有当 worker 自己队列清空且其他队列仍有待处理任务时，才从剩余任务最多的队列偷取尾部任务。`--scheduler static` 会严格保持固定分片；`--scheduler dynamic` 使用共享任务队列，空闲 worker 会继续领取后续 scene。正式大批量前建议用 30-90 scene 对比调度策略和 worker 数。batch child 会设置 `runtime.skip_summary=true`，跳过自己的 `summary/` 汇总复制，最终由 batch 顶层统一生成 summary。成功 scene 发布时会从 `batch/worker_runs` move 到 run 根目录，worker 子 run 不再保留成功场景的完整重复副本，只保留日志、配置、小 manifest 和失败场景调试材料。batch 完成后会写 `manifest.json`、`manifest_batch.json` 和 `manifest_<mode>.json`。

正式生产时也可以在 batch 末尾自动生成 derived maps 和 compact vision dataset：

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

`postprocess` 是 batch-only 配置，默认关闭。`maps.bs_label.name=label_panel_0p1` 是当前正式视觉数据集推荐的 BS 来源，避免把不同 UE 采样密度和策略的 label variant 中的 BS 重复合并。

同名任务恢复：

```bash
uv run scenegen-batch \
  --config config/tasks/front3d_full_simulation.yaml \
  --workers 4 \
  --resume \
  --set pipeline.scenes=2000 \
  --set pipeline.run_name=front3d_production_2000
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

`front3d` 的 `placements.json` 中 `sionna_assets.timings_s` 会记录 Sionna XML 资产导出的细分耗时，包括建筑拆分、家具资产拆分和 XML 写入，用于定位 `build_scene` 阶段瓶颈。

`procedural_front3d` 的 scene record 中 `procedural.timings_s` 和 `procedural.placement_stats` 会记录 layout、建筑 mesh/source 写出、家具摆放、候选尝试次数、精确 bbox 计算次数和粗略碰撞拒绝次数。当前摆放阶段先用资产目录尺寸做近似 footprint 过滤，只有候选通过房间边界和粗略避碰后才加载 OBJ 计算精确 bbox；这是程序化模式第一轮性能优化的关键。

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

开启 `postprocess.maps.enabled` 后，每个 scene 下会有 `maps/geometry.npz`、`maps/propagation.npz`、`maps/metadata.json`。开启 `postprocess.dataset.enabled` 后，默认输出 `datasets/<run_name>_vision/`，每个样本只保留 `floorplan.png`、`mask.npy/png`、`mask_preview.png`、`geometry.npz`、`propagation.npz`、`label_bs.json` 和 `metadata.json`。

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
