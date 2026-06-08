# 派生监督 Map 生成规范

## 1. 目标

本文档定义视觉多任务预训练中需要生成的 Map，分为两组：

- 监督目标 Map：作为 loss target 离线生成并存储。
- BS 条件 Map：作为 `Conditional DPT Decoder` 的输入条件，默认训练时在线生成，必要时可缓存。

监督目标 Map 包括：

1. `SDF map`
2. `LoS map`
3. `wall-count map`

BS 条件 Map 至少包括：

4. `dx map`
5. `dy map`
6. `distance / log-distance map`

如果沿用模型设计文档中的推荐条件输入，还应额外生成：

7. `BS heatmap`

因此，最小配置可认为一共 6 类 Map；推荐配置是 7 类 Map。前三类是监督目标，后四类是 BS-conditioned head 的输入条件。它们都不需要人工标注，均可由已有四分类 mask、BS 点坐标和物理比例尺自动生成。

已有 mask 类别：

```text
0 outdoor
1 wall
2 free_space
3 furniture
```

第一版约定：

- `wall` 是主要几何边界和 RF 遮挡物。
- `free_space` 是合法 UE / BS 区域。
- `furniture` 当作 `free_space` 处理，不作为遮挡物。
- `outdoor` 不参与 SDF / LoS / wall-count 的有效 loss。
- `dx/dy/distance/BS heatmap` 不作为监督目标，只作为模型条件输入。
- 默认物理比例尺为 `meter_per_pixel = 0.05`。

## 2. 通用派生 Mask

从四分类 mask 派生以下二值图：

```python
OUTDOOR = 0
WALL = 1
FREE_SPACE = 2
FURNITURE = 3

wall_mask = mask == WALL
outdoor_mask = mask == OUTDOOR
free_like_mask = (mask == FREE_SPACE) | (mask == FURNITURE)
indoor_valid_mask = ~outdoor_mask
ue_valid_mask_full = free_like_mask
bs_valid_mask = free_like_mask
```

说明：

- `free_like_mask` 是第一版合法自由空间，包括原始自由空间和家具区域。
- `wall_mask` 只包含墙。
- `outdoor_mask` 在训练 loss 中 ignore。
- padding 区域不属于任何有效区域，由 batch collate 额外产生 `valid_pixel_mask` 控制。

## 3. SDF Map

### 3.1 定义

SDF 是 signed distance field，有符号距离场。它为每个像素提供到最近墙体边界的二维欧氏距离，并用符号区分墙内和非墙区域。

第一版定义：

```text
free_space / furniture-as-free: positive distance to nearest wall
wall: negative distance to nearest non-wall boundary
outdoor: ignore
```

注意：SDF 不是上下左右四个方向的距离，而是二维欧氏最短距离，包含斜向距离。

### 3.2 作用

mask 分割只告诉模型每个像素属于哪一类。SDF 额外提供连续几何信息：

- 离墙距离。
- 房间宽度和开阔程度。
- 走廊与房间的空间尺度差异。
- 墙体边界位置。
- 对室内定位有用的 UE 到墙距离先验。

### 3.3 生成流程

输入：

```text
mask: [H, W]
meter_per_pixel: float, default 0.05
r_max_m: float, default 3.0
```

步骤：

```python
wall = mask == WALL
outdoor = mask == OUTDOOR
free_like = (mask == FREE_SPACE) | (mask == FURNITURE)
indoor_valid = ~outdoor

# distance_transform_edt returns Euclidean distance in pixels.
dist_to_wall_px = distance_transform_edt(~wall)
dist_inside_wall_px = distance_transform_edt(wall)

sdf_m = zeros([H, W], float32)
sdf_m[~wall] = dist_to_wall_px[~wall] * meter_per_pixel
sdf_m[wall] = -dist_inside_wall_px[wall] * meter_per_pixel

sdf_valid_mask = indoor_valid
```

截断与归一化：

```python
sdf_clipped_m = clip(sdf_m, -r_max_m, r_max_m)
sdf_norm = sdf_clipped_m / r_max_m
```

推荐训练目标：

```text
sdf target stored as normalized value in [-1, 1]
outdoor ignored by sdf_valid_mask
```

### 3.4 数据示例

对于一个自由空间点：

```text
距离最近墙 1.2 m
r_max_m = 3.0
sdf_norm = 1.2 / 3.0 = 0.4
```

对于一个墙体内部点：

```text
到最近非墙边界 0.1 m
r_max_m = 3.0
sdf_norm = -0.1 / 3.0 = -0.033
```

对于室外点：

```text
sdf value 可填 0
sdf_valid_mask = 0
训练时不计算 loss
```

### 3.5 存储

推荐每个 scene 一个 geometry 文件：

```text
scene_id_geometry.npz
```

字段：

```text
sdf:              float16 or float32, [H, W]
sdf_valid_mask:   uint8 or bool, [H, W]
meter_per_pixel:  float32 scalar
r_max_m:          float32 scalar
```

建议：

- 如果存储压力较小，使用 `float32`。
- 如果数据量变大，使用 `float16` 存储归一化后的 `sdf`。
- `sdf_valid_mask` 必须保留，不能只依赖类别值。

## 4. LoS Map

### 4.1 定义

LoS map 是给定一个 BS 点后，对每个 UE 候选位置判断该 UE 与 BS 之间是否直线无遮挡。

```text
LoS = 1: BS 到 UE 的直线不穿过 wall
LoS = 0: BS 到 UE 的直线穿过至少一个 wall pixel
```

LoS map 是 BS-conditioned dense map。同一个场景换一个 BS 点，LoS map 会改变。

### 4.2 作用

LoS map 是跨 patch 的全局结构任务。它要求模型理解：

- 墙体是否位于 BS 和远处区域之间。
- 同一张平面图中不同 BS 位置对应不同可见区域。
- 哪些区域受遮挡，哪些区域直视可达。
- 与 CSI / RSS / pathloss 相关的基本传播几何。

相比随机两点 LoS 分类，LoS map 提供更密集的监督，每个 BS 点对应整张可见性地图。

### 4.3 BS 与 UE 网格

每个 scene 默认采样：

```text
K = 4-8 BS points
```

BS 必须满足：

```text
bs_valid_mask[y_bs, x_bs] = 1
```

UE 候选点不需要使用原始全分辨率像素，推荐下采样网格：

```text
s_los = 4 pixels
meter_per_pixel = 0.05
UE grid spacing = 0.2 m
```

可选轻量配置：

```text
s_los = 8 pixels
UE grid spacing = 0.4 m
```

UE 网格点坐标：

```python
x = j * s_los + s_los / 2
y = i * s_los + s_los / 2
```

其中 `i, j` 是 LoS map 的 grid index。

UE 有效区域：

```python
ue_valid_mask[i, j] = free_like_mask[round(y), round(x)]
```

### 4.4 生成流程

输入：

```text
wall_mask: [H, W]
free_like_mask: [H, W]
bs_point_px: (x_bs, y_bs)
s_los: int
```

对每个 UE grid point：

```python
if not ue_valid:
    los_map[i, j] = 0
    ue_valid_mask[i, j] = 0
    continue

line_pixels = rasterize_line_supercover((x_bs, y_bs), (x_ue, y_ue))
wall_hits = wall_mask[line_pixels]

los_map[i, j] = 1 if wall_hits.sum() == 0 else 0
ue_valid_mask[i, j] = 1
```

推荐使用 supercover line 或足够密集的 ray sampling。普通 Bresenham 可能在斜线穿过薄墙时漏采边角像素。

### 4.5 数据示例

```text
BS 在客厅

UE_A 在同一客厅，中间没有墙:
  los = 1

UE_B 在隔壁卧室，中间隔一面墙:
  los = 0

UE_C 在 outdoor:
  ue_valid_mask = 0
  不计算 loss

UE_D 在家具区域:
  第一版家具当 free_space
  正常计算 los
```

### 4.6 存储

LoS map 与 wall-count map 使用同一 propagation 文件存储：

```text
scene_id_propagation.npz
```

LoS 字段：

```text
bs_coords_px:       float32, [K, 2]
bs_coords_m:        float32, [K, 2]
los_maps:           uint8 or bool, [K, H_los, W_los]
ue_valid_mask:      uint8 or bool, [H_los, W_los]
los_stride_pixels:  int scalar
meter_per_pixel:    float32 scalar
```

`ue_valid_mask` 如果与 BS 无关，可以存 `[H_los, W_los]`。如果未来 BS 可放置区域或合法 UE 区域依赖 BS，再改为 `[K, H_los, W_los]`。

## 5. Wall-count Map

### 5.1 定义

wall-count map 与 LoS map 使用同一个 BS 点。它输出 BS 到每个 UE 候选位置的直线路径穿过了几段连续墙体。

类别截断为：

```text
0
1
2
3+
```

训练时作为 4 类分类任务。

### 5.2 作用

LoS 只告诉模型有没有遮挡，wall-count 进一步告诉模型遮挡强度的粗粒度等级。它对 RF 传播更有用：

- `LoS=0` 的区域中，穿 1 道墙和穿 3 道墙明显不同。
- wall-count 与 pathloss / RSS 衰减有直接几何相关性。
- 任务仍然不需要真实电磁仿真，完全由 mask 派生。

### 5.3 连续墙段计数

wall-count 不是墙像素数量，而是连续墙段数量。

例如 line 上的墙命中序列：

```text
000111100000011000
```

连续墙段数为：

```text
2
```

生成逻辑：

```python
hits = wall_mask[line_pixels]  # bool array along ray

count = 0
in_wall = False
for h in hits:
    if h and not in_wall:
        count += 1
        in_wall = True
    elif not h:
        in_wall = False

wall_count_class = min(count, 3)
```

如果使用 supercover line，每一步可能包含多个像素。实现时可以将每个 ray step 的多个像素取 OR，形成沿射线方向的一维 `hits` 序列，再做连续段计数。

### 5.4 输出和存储

输出：

```text
wall_count_map: uint8, [H_los, W_los]
value in {0, 1, 2, 3}
```

存储在 propagation 文件：

```text
scene_id_propagation.npz
```

字段：

```text
wall_count_maps: uint8, [K, H_los, W_los]
```

与 LoS 关系：

```text
los = 1 should imply wall_count = 0
wall_count > 0 should imply los = 0
```

数据生成后应加入一致性检查。

## 6. BS 采样策略

每个 scene 默认采样：

```text
K = 4-8
```

采样候选区域：

```text
free_like_mask = free_space | furniture
```

推荐混合策略：

1. 随机自由空间点。
2. 房间或连通自由空间区域中心附近。
3. 墙边附近自由空间点。
4. 走廊或狭长区域。
5. 角落附近。

第一版实现可以简单一些：

```text
70% random free-like pixels
30% wall-near free-like pixels
```

墙边点可通过 distance-to-wall 筛选：

```text
0.3 m <= distance_to_wall <= 1.0 m
```

如果场景自由空间很小，允许 fallback 到纯随机自由空间采样。

BS 坐标必须同时保存 pixel 与 meter 表示：

```text
bs_coords_px[k] = (x_px, y_px)
bs_coords_m[k] = (x_px * meter_per_pixel, y_px * meter_per_pixel)
```

SceneGen 当前 derived map 脚本不再合并所有 label variant 的 BS。默认行为是读取每个场景 `label/` 目录中排序后的第一个 label JSON，避免把不同 UE 采样密度或 `panel/walk` 策略下的重复 BS 全部累加到 propagation target。

正式 Front3D 视觉数据集建议显式指定稳定来源：

```bash
uv run python scripts/generate_derived_maps.py <run_dir> \
  --bs-label-name label_panel_0p1
```

如果某些历史数据命名不同，也可以依赖默认的“第一个 label JSON”行为，或用 `--bs-label-glob` 指定其他匹配规则。最终 `maps/metadata.json` 会记录 `parameters.bs_label_filter`，用于追溯 BS 来源。

## 7. BS Condition Maps

### 7.1 定义

BS condition maps 是给 `Conditional DPT Decoder` 使用的条件输入。它们不是监督目标，不参与 loss，而是告诉模型当前 LoS / wall-count 任务对应的是哪一个 BS 点。

最小条件输入：

```text
dx map
dy map
distance / log-distance map
```

推荐条件输入：

```text
BS heatmap
dx map
dy map
log-distance map
```

其中 `BS heatmap` 不是必须的，因为 `dx=0, dy=0` 或 `distance=0` 已经隐式指示 BS 位置。但实践上建议保留 `BS heatmap`，它能更直接地告诉 decoder 条件点的位置，训练通常更稳。

### 7.2 生成网格

condition maps 可在训练时根据 `bs_coords_px` 在线生成。默认生成在模型输入图像网格上：

```text
condition maps: [4, H_pad, W_pad]
```

然后在模型内部 resize 到每个 backbone feature scale：

```text
cond_i: [4, H_i, W_i]
```

如果只服务 LoS / wall-count head，也可以直接生成在 LoS 网格：

```text
condition maps: [4, H_los, W_los]
```

第一版推荐：

```text
先在 full image grid 生成，再按需要 resize
```

这样同一组 condition maps 可同时服务 DPT multi-scale condition injection。

### 7.3 dx / dy Map

给定 BS 点：

```text
bs_point_px = (x_bs, y_bs)
meter_per_pixel = 0.05
```

对每个像素或 grid 点 `(x, y)`：

```python
dx_map_m[y, x] = (x - x_bs) * meter_per_pixel
dy_map_m[y, x] = (y - y_bs) * meter_per_pixel
```

说明：

- `dx_map_m` 和 `dy_map_m` 使用物理单位 m。
- `dx_map_m < 0` 表示该点在 BS 左侧。
- `dy_map_m < 0` 表示该点在 BS 上方。
- 不建议只用 pixel 单位，因为场景尺寸变化时数值含义不稳定。

可选归一化：

```python
coord_clip_m = 20.0
dx_map = clip(dx_map_m, -coord_clip_m, coord_clip_m) / coord_clip_m
dy_map = clip(dy_map_m, -coord_clip_m, coord_clip_m) / coord_clip_m
```

如果当前数据集中最大室内跨度明显小于或大于 `20m`，应按数据分布调整 `coord_clip_m`。

### 7.4 Distance / Log-distance Map

原始距离：

```python
dist_map_m[y, x] = sqrt(dx_map_m[y, x] ** 2 + dy_map_m[y, x] ** 2)
```

模型输入默认使用 log-distance：

```python
eps_m = 0.05
log_distance_map = log(dist_map_m + eps_m)
```

推荐再做截断归一化：

```python
dist_clip_m = 30.0
dist_norm = clip(dist_map_m, 0.0, dist_clip_m) / dist_clip_m

log_dist_min = log(eps_m)
log_dist_max = log(dist_clip_m + eps_m)
log_distance_norm = (log_distance_map - log_dist_min) / (log_dist_max - log_dist_min)
```

默认给模型的 `distance` 条件通道使用：

```text
log_distance_norm
```

原因是无线传播中的路径损耗与 `log(distance)` 更接近，且 log 形式能压缩远距离数值范围。

### 7.5 BS Heatmap

推荐使用 2D Gaussian heatmap：

```python
sigma_m = 0.2
sigma_px = sigma_m / meter_per_pixel

bs_heatmap[y, x] = exp(-((x - x_bs) ** 2 + (y - y_bs) ** 2) / (2 * sigma_px ** 2))
```

说明：

- `bs_heatmap` 范围为 `[0, 1]`。
- BS 点附近最大，远离 BS 逐渐接近 0。
- `sigma_m` 可以理解为告诉模型 BS 条件点的大致位置范围。
- 如果 LoS grid stride 较大，heatmap 应在对应 grid 上重新计算或从 full-resolution heatmap 下采样。

### 7.6 是否需要离线存储

默认不离线存储 `dx/dy/distance/BS heatmap`，原因：

- 它们完全由 `bs_coords_px`、`meter_per_pixel` 和图像尺寸决定。
- 每个 scene 有 K 个 BS，如果全部存储会显著增加重复数据。
- 在线生成计算量很小，远低于 LoS / wall-count 的 ray casting。

推荐只存储：

```text
bs_coords_px
bs_coords_m
meter_per_pixel
height
width
```

训练时在线生成：

```text
bs_heatmap
dx_map
dy_map
log_distance_map
```

如果为了调试或加速必须缓存，可额外存储：

```text
condition_maps: float16, [K, 4, H_cond, W_cond]
condition_channels = ["bs_heatmap", "dx", "dy", "log_distance"]
condition_stride_pixels
coord_clip_m
dist_clip_m
```

但第一版不建议缓存。

## 8. 文件组织与 Manifest

推荐目录组织：

```text
derived_maps/
  geometry/
    scene_000001_geometry.npz
    scene_000002_geometry.npz
  propagation/
    scene_000001_propagation.npz
    scene_000002_propagation.npz
  manifest.jsonl
```

`manifest.jsonl` 每行一个 scene：

```json
{
  "scene_id": "scene_000001",
  "image_path": "images/scene_000001.png",
  "mask_path": "masks/scene_000001.png",
  "geometry_path": "derived_maps/geometry/scene_000001_geometry.npz",
  "propagation_path": "derived_maps/propagation/scene_000001_propagation.npz",
  "height": 640,
  "width": 896,
  "meter_per_pixel": 0.05
}
```

也可使用 `manifest.csv`，但 `jsonl` 更适合保留可扩展字段。

SceneGen 当前第一版脚本使用更贴近训练输入的 compact dataset 结构：

```text
front3d_0000/
  floorplan.png
  mask.npy
  mask.png
  mask_preview.png
  geometry.npz
  propagation.npz
  label_bs.json
  metadata.json
manifest.jsonl
summary.json
build_report.json
```

如果主生产任务和补跑任务分别构建了 compact dataset，可以用合并脚本重编号并补齐目标数量：

```bash
uv run python scripts/merge_vision_datasets.py \
  /path/to/primary_vision_dataset \
  /path/to/supplement_vision_dataset \
  /path/to/final_vision_dataset \
  --target-count 3000 \
  --overwrite
```

合并结果保持一级 scene 目录，例如 `front3d_0000` 到 `front3d_2999`。每个 scene 的 `metadata.json` 会增加 `merged_dataset` 字段，记录 `source_dataset_dir`、`source_scene_key`、`source_scene_id` 和 `source_role`，便于从最终数据集反查来源。

## 9. 推荐 NPZ Schema

### 9.1 Geometry NPZ

```text
sdf:              float16 or float32, [H, W]
sdf_valid_mask:   uint8, [H, W]
meter_per_pixel:  float32 scalar
r_max_m:          float32 scalar
height:           int scalar
width:            int scalar
```

### 9.2 Propagation NPZ

```text
bs_coords_px:       float32, [K, 2]
bs_coords_m:        float32, [K, 2]
los_maps:           uint8, [K, H_los, W_los]
wall_count_maps:    uint8, [K, H_los, W_los]
ue_valid_mask:      uint8, [H_los, W_los]
los_stride_pixels:  int scalar
meter_per_pixel:    float32 scalar
height:             int scalar
width:              int scalar
```

Shape:

```text
H_los = ceil(H / los_stride_pixels)
W_los = ceil(W / los_stride_pixels)
```

注意：默认 schema 不存储 `dx/dy/distance/BS heatmap`。这些 condition maps 由 Dataset 或模型 forward 根据 `bs_coords_px` 在线生成。

## 10. Dataset 读取行为

训练时，一个 scene 可以包含多个 BS 条件样本。推荐 Dataset 行为：

```python
scene = load_scene(scene_id)
k = random choice from [0, K)

sample = {
    "image": image,
    "mask": mask,
    "sdf": sdf,
    "sdf_valid_mask": sdf_valid_mask,
    "bs_point_px": bs_coords_px[k],
    "los_map": los_maps[k],
    "wall_count_map": wall_count_maps[k],
    "ue_valid_mask": ue_valid_mask,
    # Generated online from bs_point_px, meter_per_pixel, H, W:
    # "bs_heatmap", "dx_map", "dy_map", "log_distance_map"
    "meter_per_pixel": meter_per_pixel,
    "orig_size": (H, W),
}
```

同一个 scene 在不同 epoch 随机使用不同 BS，增强 conditional task 的多样性。

如果需要复现实验，记录：

```text
BS sampling seed
K
los_stride_pixels
r_max_m
class id mapping
meter_per_pixel
coord_clip_m
dist_clip_m
```

## 11. Sanity Checks

生成数据后必须做以下检查。

### 11.1 SDF Checks

1. 墙边界附近 SDF 接近 0。
2. 自由空间中心为正。
3. 墙体内部为负。
4. outdoor 区域 `sdf_valid_mask=0`。
5. 家具区域第一版按自由空间处理，SDF 为正。
6. SDF 数值被截断在 `[-1, 1]`，如果存归一化值。

### 11.2 LoS Checks

1. 没有墙的空房间中，所有 free-like UE 点对任意 BS 都应为 LoS。
2. 单墙隔断场景中，墙另一侧区域应为 NLoS。
3. BS 自身所在 grid 应为 LoS。
4. outdoor grid 的 `ue_valid_mask=0`。
5. 家具 grid 第一版为 valid UE。

### 11.3 Wall-count Checks

1. `los=1` 的位置必须 `wall_count=0`。
2. `wall_count>0` 的位置必须 `los=0`。
3. 单墙穿越应为 `1`。
4. 两面墙穿越应为 `2`。
5. 三面及以上应归为 `3`。

### 11.4 Visualization Checks

每生成一批数据，随机可视化：

```text
floorplan + mask
SDF heatmap
BS point overlay
LoS binary map
wall-count categorical map
ue_valid_mask
dx_map / dy_map / log_distance_map
bs_heatmap
```

可视化至少覆盖：

- 小户型。
- 大户型。
- 走廊型场景。
- 多房间场景。
- 非矩形或不规则边界场景。

### 11.5 Condition Map Checks

1. `dx_map` 在 BS 左侧为负、右侧为正。
2. `dy_map` 在 BS 上方为负、下方为正。
3. `distance / log-distance map` 在 BS 位置附近最小。
4. `BS heatmap` 在 BS 位置附近最大。
5. padding 区域可以生成 condition 值，但不能通过 loss 或 token mask 影响训练。

## 12. 训练时的 Loss Mask

各任务的有效区域：

```text
mask loss:
  valid_pixel_mask = 1

SDF loss:
  valid_pixel_mask = 1
  and sdf_valid_mask = 1

LoS loss:
  valid_los_grid = ue_valid_mask = 1
  and corresponding area not padding

wall-count loss:
  same as LoS

condition maps:
  not supervised directly
  used only as model inputs for LoS / wall-count heads
```

对于变尺寸 batch，LoS / wall-count 的 padding mask 需要从原始尺寸下采样得到：

```text
valid_los_mask = downsample_valid_mask(valid_pixel_mask, stride=s_los)
final_ue_valid_mask = ue_valid_mask & valid_los_mask
```

## 13. 默认参数

第一版默认参数：

```text
meter_per_pixel = 0.05
r_max_m = 3.0
K = 6 BS points per scene
los_stride_pixels = 4
furniture_as_free = true
wall_count_classes = [0, 1, 2, 3+]
sdf_storage = float16 normalized to [-1, 1]
los_storage = uint8
wall_count_storage = uint8
condition_maps = ["bs_heatmap", "dx", "dy", "log_distance"]
condition_generation = online
sigma_m = 0.2
coord_clip_m = 20.0
dist_clip_m = 30.0
eps_m = 0.05
```

如果后续发现 LoS / wall-count 文件过大，可优先调整：

```text
los_stride_pixels: 4 -> 8
K: 6 -> 4
```

如果后续希望更贴近真实 RF，可在不破坏当前 schema 的情况下新增：

```text
wall_thickness_map
proxy_pathloss_map
material_mask
```

这些不是第一版范围。
