# SceneGen 配置说明

默认配置文件是 `config/default.yaml`。运行时默认读取它，命令行参数会覆盖 YAML；每次生成结果时，SceneGen 会把最终实际生效的配置写到 run 目录下的 `effective_config.yaml`。

当使用 `config/sparse.yaml`、`config/medium.yaml`、`config/dense.yaml` 这类局部配置时，SceneGen 会先加载 `config/default.yaml`，再用指定 YAML 覆盖其中字段。因此 Bistro 禁区、floorplan 默认参数、质量检查等通用设置仍会继承默认配置。

## 配置链路

SceneGen 的配置按下面顺序合并，后面的优先级更高：

1. 代码内置默认值 `DEFAULT_CONFIG`。
2. 如果 `--config` 指向的不是 `config/default.yaml`，先加载 `config/default.yaml` 作为项目默认模板。
3. 加载 `--config` 指定的 YAML，并覆盖前面的默认值。
4. 应用 CLI 参数覆盖，例如 `--scenes`、`--seed`、`--asset-catalog`、`--no-floorplan`。
5. 归一化路径和类型：相对路径解析为 repo 下的绝对路径，数字和布尔值转成运行时类型。
6. 写出 `<run_dir>/effective_config.yaml`，它记录实际生效配置。

兼容规则：旧 YAML 字段 `assets.manifest` 和旧 CLI 参数 `--asset-manifest` 仍可输入，但会被归一化为 `assets.catalog`。写出的 `effective_config.yaml` 只保留 `assets.catalog`。

## 预设配置

- `default.yaml`: 默认主实验配置，中等密度 Bistro 场景，默认生成 10 个。
- `template.yaml`: 完整字段模板，适合复制后作为新实验配置起点。
- `sparse.yaml`: 稀疏场景，低遮挡、低反射、多径相对简单，默认生成 3 个。
- `medium.yaml`: 中等场景，主实验分布，默认生成 3 个。
- `dense.yaml`: 稠密场景，高遮挡、高反射、多径更复杂，默认生成 3 个。

运行示例：

```bash
uv run scenegen --config config/sparse.yaml
uv run scenegen --config config/medium.yaml
uv run scenegen --config config/dense.yaml
```

## pipeline

- `mode`: `bistro` 或 `generated`。
- `scenes`: 本次生成场景数量。
- `seed`: 主随机种子。相同配置和 seed 可复现同一批场景。
- `output_dir`: run 输出根目录。
- `run_name`: run 目录名。为 `null` 时使用时间戳。
- `clean`: 生成前是否清理 `output_dir` 下旧 run。

## assets

- `catalog`: 标准资产 catalog 路径，默认 `data/catalogs/bistro.v1.json`。

兼容说明：旧 CLI 参数 `--asset-manifest` 和旧 YAML 字段 `assets.manifest` 仍然可用，但都会被映射到 `assets.catalog`。`data/assets/manifest.json` 也仍保留，但它只是兼容位置，内容应与 `data/catalogs/bistro.v1.json` 使用同一份清洗后的资产契约。

## bistro

- `base_dir`: 空 Bistro 场景目录，目录内需要 `scene.obj`，可选 `label.json`。
- `forbidden_xy_rects`: Bistro 禁区列表，格式为 `[x_min, y_min, x_max, y_max]`。只在 `mode: bistro` 时使用。

## placement

- `min_tables` / `max_tables`: 每个场景随机桌子数量范围。
- `floor_extras`: 地面额外物体数量。
- `min_tabletop_items` / `max_tabletop_items`: 每张桌子桌面小物数量范围。
- `bistro_support_items`: Bistro 现有台面/吧台上额外小物数量。
- `max_attempts`: 每次摆放采样的最大尝试次数。

## validation

- `sionna`: 是否在每个场景生成后用 `sionna.rt.load_scene()` 验证 `scene.xml`。

## quality

- `enabled`: 是否在每个场景生成后执行质量检查。默认开启。
- `fail_on_error`: 质量检查发现 error 时是否让整个命令返回失败。默认开启。
- `collision_padding_m`: 物体间 AABB 碰撞检查的额外间距。默认 `0.0`，只检查真实重叠。
- `bistro_static_clearance_m`: Bistro 地面物体与空场景静态几何的额外避让距离。默认 `0.0`，避免过严误报。
- `support_tolerance_m`: 地面/桌面/已有台面支撑关系检查的高度容差，单位米。

质量报告输出：

- 单场景：`<scene_dir>/quality_report.json`
- run 汇总：`<run_dir>/statistics.json`
- manifest 中同步记录 `quality_requested`、`quality_ok` 和统计摘要。

## floorplan

- `enabled`: 是否每个场景同步生成平面图。
- `geometry_enabled`: 是否生成第一版几何占据平面图，也就是基于 `scene.obj` 采样和高度扫描的投影图。
- `geometry_clean_enabled`: 是否额外生成去噪后的几何占据图 `geometry_clean.png`，用于降低随机采样点对模型训练的影响。默认关闭，默认训练输入优先使用高密度 raw 指定高度投影。
- `geometry_clean_min_density`: clean 图保留像素的最低累计采样密度，调高会减少散点但可能损失细小物体。
- `geometry_clean_min_neighbors`: clean 图保留像素所需的最少 8 邻域支撑像素数量，用于去掉孤立点。
- `geometry_clean_min_z_m`: clean 图忽略低于该高度的采样点，默认跳过近地面采样噪声。
- `geometry_clean_max_abs_normal_z`: clean 图保留的表面法线竖直分量上限；默认偏向保留墙体、家具侧面等竖直/倾斜表面，过滤地板、天花和桌面这类水平面。
- `geometry_clean_opening_px`: clean 图的 opening 迭代半径，用于去掉小碎块；默认 `0`，避免误删细墙和家具。
- `geometry_clean_closing_px`: clean 图的 closing 迭代半径，用于连通墙体和家具边缘的小缺口。
- `semantic_enabled`: 是否生成第二版语义平面图，也就是直接基于 SceneGen 的 `placements` 绘制资产矩形和类别标注。默认关闭。
- `resolution_m_per_pixel`: 平面图栅格分辨率，单位米/像素。
- `height_mode`: 几何平面图高度策略。`heights` 表示只渲染指定高度序列；`layers` 表示使用旧版逐层扫描。
- `heights_m`: `height_mode: heights` 时使用的高度序列，单位米。默认只渲染 `[1.6]`。
- `step_m`: `height_mode: layers` 时的高度扫描层间隔。
- `top_z_m`: `height_mode: layers` 时手动指定扫描顶部高度；为 `null` 时自动检测。
- `bottom_z_m`: 扫描底部高度。
- `sample_density_scale`: 表面采样密度倍率，越大越细但更慢。
- `min_sample_points` / `max_sample_points`: 表面采样点数量上下限。
- `preview_tile_size_px`: 分层预览图每个 tile 的尺寸。
- `semantic_padding_m`: 语义平面图在场景边界外额外留白，单位米。
- `semantic_draw_labels`: 是否在语义平面图中绘制类别文字标签。
- `fail_on_error`: 平面图生成失败时是否让整个 run 返回失败。

## 命令行覆盖示例

```bash
uv run scenegen --config config/default.yaml --scenes 3 --seed 123 --no-floorplan
```

上面的命令会读取 YAML，但最终生效配置中的 `pipeline.scenes`、`pipeline.seed` 和 `floorplan.enabled` 会被命令行覆盖。

只关闭几何占据平面图、打开语义平面图：

```bash
uv run scenegen --no-floorplan-geometry --semantic-floorplan
```

保留几何平面图，但关闭 clean 后处理：

```bash
uv run scenegen --floorplan-geometry --no-floorplan-geometry-clean
```

保留几何占据平面图、显式关闭语义平面图：

```bash
uv run scenegen --floorplan-geometry --no-semantic-floorplan
```

切换回旧版逐层扫描：

```bash
uv run scenegen --floorplan-height-mode layers --floorplan-top-z 1.6 --floorplan-step 0.2
```

渲染多个指定高度：

```bash
uv run scenegen --floorplan-height-mode heights --floorplan-heights 1.2,1.6,2.0
```

临时关闭质量检查：

```bash
uv run scenegen --no-quality
```

临时使用另一个资产 catalog：

```bash
uv run scenegen --asset-catalog data/catalogs/bistro.v1.json
```
