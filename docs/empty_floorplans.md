# Front3D 空房间平面图

此工作流为已有场景生成配对的空房间图。它属于 `postprocess`，复用 `front3d` 的坐标和结构读取以及 `floorplan` 的投影、mask 和写出能力。原有场景生成、label、CFR、maps 和默认配置不受影响。

## 为什么需要专门入口

仅把家具 placements 置空仍会留下 phase1 architecture OBJ 中的橱柜和定制家具。重新计算空场景 bbox 又可能缩小画布；重新从随机采样估计地面高度，也可能移动投影的高度基准。普通 batch 的连续输出编号也不适合保留测试集的非连续 key。

本入口读取映射、原 run 的配置和逐场景记录，以历史 `world_offset` 变换原始结构 mesh，以原图元数据锁定画布和高度。它只输出新平面图与追溯记录。

## 输入与命令

`--mapping` 是非空 JSONL，每行至少包含 `scene_key` 和 `front3d_scene_id`；两列分别唯一，行顺序就是 manifest 的输出顺序。`scene_key` 必须符合 `front3d_` 加至少四位数字。其他字段（如测试集类别、原 H5 路径、顺序）原样保留。

`--source-run` 要求存在 `effective_config.yaml`，以及每个原场景的 `placements.json`、`scene_record_batch.json`、`floorplan/meta.json` 和主层 PNG。若原配置开启 class mask，还要求原 mask 元数据和 NPY。原 phase1 manifest、architecture metadata/OBJ 和原始 3D-FRONT JSON 必须可读。相对配置路径按项目根目录解析，输出必须与原 run 分离。

本次 339 测试场景的命令：

```bash
uv run python scripts/regenerate_empty_floorplans.py \
  --mapping /data/sunmeiyuan/projects/sionna/outputs/datasets/Front3d_2988_CFR_Only/rebuttal/test_scene_mapping.jsonl \
  --source-run /data/sunmeiyuan/projects/SceneGen/front3d_full_all_6813_20260617_215824 \
  --output-dir /data/sunmeiyuan/projects/sionna/outputs/datasets/Front3d_2988_CFR_Only/rebuttal/empty_floorplans \
  --workers 8
```

完成场景在临时目录写出并验证后发布为原 key 目录，失败会记录在 `logs/events.jsonl` 和 `summary.json`。存在输出时显式加 `--resume`：校验代码、配置、映射和源输入指纹，检查已有场景的文件校验和、帧及 mask，只生成尚未完成的场景。源输入变化或完成产物损坏会报错，不会静默复用。`summary.json` 的 `all_validated: true` 且 `completed == expected` 才代表全量成功；命令失败返回非零。

## 结构筛选与数值约定

`front3d.collect_front3d_structural_mesh_arrays` 从原始 JSON 的 `mesh` 中正向选择 `STRUCTURAL_MESH_TYPES`。结构类型策略 `front3d_structural_mesh_types.v1` 保留：

- `Floor`、`Ceiling`、`SlabSide`、`SlabBottom`。
- `WallTop`、`WallBottom`、`WallInner`、`WallOuter`、`Front`、`Back`。
- `Door`、`Window`、`BayWindow`、`Hole`、`Pocket`。
- `Beam`、`Column`、`Flue`、`SewerPipe`、`Baseboard`、`Cornice`。
- `CustomizedCeiling`、`SmartCustomizedCeiling`、`ExtrusionCustomizedCeilingModel`、`CustomizedFeatureWall`、`ExtrusionCustomizedBackgroundWall`、`Customized_wainscot`。

家具实例完全不加载。所有非白名单 mesh 均剔除，包括 `Cabinet`、`Cabinet/LightBand`、`CustomizedFurniture`、`CustomizedFixedFurniture`、`LightBand`、`CustomizedPersonalizedModel`、`CustomizedPlatform`、`CustomizedBackgroundModel` 和 `Unknown`。其中平台、未分类和个性化模型不能可靠认作基础结构，因此采取显式保守筛选，每场景记录保留/剔除类型与数量，便于后续复查。门窗和建筑饰面保持现有建筑定义，不引入新的开口算法。

原始 Y-up mesh 沿用 normalized 的 `(x, -z, y)` 转换；与 phase1 OBJ 相同，先保留六位小数，再加历史 `world_offset`，最后按已有 exporter 的 float32 mesh 数组约定进入投影。没有根据筛选后的 mesh 重新归一化。当前 phase1 bbox/offset 与历史记录只允许 `1e-9` 的数值计算误差，实际使用的是历史保存值。

`floorplan.process_scene` 的可选 `reference_meta` 锁定比例尺、原点、extent、grid_shape、有效地面/顶部、轴和高度扫描；原配置与参考元数据冲突时拒绝生成。`ProjectionFrame` 校验 `H/W == ceil(extent_y/x / resolution) + 1`。已有调用不传该参数时沿用原行为。class mask 支持独立 `reference_frame`，以保留原 mask 的精确 extent（它可能与几何 meta 存在 float32 尾差）。

本次原图统一使用 `sampling`、`0.05m/px`、Z-up 和 `[1.6m]`；采样密度及点数上下限取原配置。原始采样不保存种子，无法完全复现历史采样点；新批次用 `SHA256(schema_version + ':' + UUID)` 的前八字节（little-endian）作为种子，保证重跑和调度顺序不改变同一 scene 的像素。原有随机默认值仍为 `None`。元数据 `deterministic` 继续表示是否采用确定性三角面投影，新采样的可重复性由 `sample_seed` 表达。

坐标读取沿用现有约定：几何 stack 的行对应从 `origin_y` 起向上的网格，PNG 由 renderer 翻转为图像左上原点；不做额外旋转、resize 或重采样。物理范围、地面和比例尺全部从原图继承。

## 输出和验证

```text
empty_floorplans/
  images/front3d_0015.png             # 主层 PNG，便于直接接入现有测试流程
  front3d_0015/
    record.json                     # key/UUID、源指纹、筛选统计、种子、校验和
    floorplan/
      floorplan_1p60.png
      stack.npz
      meta.json
      preview.png
      side_view.png
      class_mask.npy / .npz / .png   # 原配置开启时生成
      class_mask_preview.png
      class_mask_meta.json
  manifest.jsonl                    # 原映射顺序，含 image_path 和 floorplan_dir
  input_scene_mapping.jsonl
  source_effective_config.yaml      # 原生成配置完整快照
  effective_config.yaml             # 本次后处理的执行参数与原投影配置
  run_contract.json                 # 源与代码指纹、具体筛选策略
  summary.json
  checksums.sha256
  logs/events.jsonl
```

原场景的 key（如 `front3d_0015`）不改变；原始 UUID 写入 manifest、逐场景 record 和几何 meta。PNG 直接用原 key 命名。manifest 的其他 source 路径字段保留输入映射的语义，`image_path` / `floorplan_dir` 相对于本次输出根目录。

每个场景验证所有高度层 PNG 与 stack 形状，以及原图的分辨率、XY 原点/extent、轴、地面/顶部、高度层和扫描参数。开启 mask 时，空 mask 必须逐像素等于 `where(original_mask == 3, 2, original_mask)`，从而证明墙体、室外区域和开口保留，家具类完全清零；几何投影的无家具性质同时由结构 mesh 白名单和零家具实例保证。校验和覆盖每个场景输出和整个 run 的文件。

参考测试：

```bash
uv run pytest -q tests/test_empty_floorplans.py
uv run pytest -q
uv run ruff check .
git diff --check
```

测试包含非连续 key 顺序、家具扩大原画布、剔除内建橱柜/固定家具/未知网格、锁定地面、重复采样、class mask 还原、UUID 错配、输出目录隔离和续跑完整性检查。

2026-10-26 本次生产验收：339/339 场景完成，失败 0；339 张主图与 canonical 测试集顺序、key、UUID 完整对应，所有帧参数与原图一致，全部空 mask 满足逐像素还原规则。独立复核了 4076 个输出文件校验和、2712 个源文件指纹。全量测试 162 项通过，`ruff check .` 和 `git diff --check` 通过。
