# 视觉多任务预训练模型设计

## 1. 目标与总体结构

本视觉模块用于多模态室内定位系统的视觉侧预训练。最终模型会与 CSI / 电磁模态融合，因此视觉模块的目标不是单纯把某一个密集预测任务做到最高分，而是让共享视觉编码器输出包含室内空间结构信息的 patch / spatial tokens。

视觉预训练阶段固定为 3 类任务、4 个输出：

1. 四分类语义 mask 分割。
2. SDF / signed distance field 密集预测。
3. 给定单个 BS 点的 LoS map 密集预测。
4. 给定同一个 BS 点的 wall-count map 密集预测。

整体结构如下：

```text
floorplan image
  -> BackboneAdapter
  -> multi-scale features {C1, C2, C3, C4}

{C1, C2, C3, C4}
  -> Geometry DPT Decoder
      -> mask head
      -> SDF head
      -> visual_tokens for downstream fusion

{C1, C2, C3, C4} + BS condition maps
  -> Conditional DPT Decoder
      -> LoS head
      -> wall-count head
```

其中：

- `Geometry DPT Decoder` 和 `Conditional DPT Decoder` 是两套参数独立的 decoder。
- 两套 decoder 的结构可以相同或相近，但不共享参数。
- `mask head` 与 `SDF head` 共享 `Geometry DPT Decoder` 输出的几何特征，只在最后 prediction head 分开。
- `LoS head` 与 `wall-count head` 共享 `Conditional DPT Decoder` 输出的传播特征，只在最后 prediction head 分开。
- 预训练完成后，默认保留 `BackboneAdapter + Geometry DPT Decoder + token projection` 作为视觉编码器；四个任务 prediction heads 可丢弃。

## 2. 输入、Padding 与物理比例尺

平面图保持原始物理比例尺，不强制 resize 到固定图像尺寸。默认：

```text
meter_per_pixel = 0.05 m / pixel
```

不同场景的 `H` 和 `W` 可以不同。训练时通过 batch collate 做 padding：

```text
image:            [B, C, H_pad, W_pad]
valid_pixel_mask: [B, 1, H_pad, W_pad]
orig_size:        [(H_1, W_1), ..., (H_B, W_B)]
meter_per_pixel:  [B] or scalar
```

要求：

- 不改变 `meter_per_pixel`。
- `H_pad` 和 `W_pad` pad 到当前 backbone 的 `size_divisor` 倍数。
- padding 区域在所有 loss 中必须 ignore。
- 输出 map 用于评估或保存时，应裁回原始 `H_i, W_i` 或对应的 downsample grid。

建议默认：

```text
image channel:
  C = 1 for grayscale floorplan
  or C = 3 if using ImageNet/DINO pretrained models that expect RGB

padding value:
  image padding = background/outdoor-like value
  valid_pixel_mask padding = 0
```

如果使用 RGB pretrained backbone，可将灰度图复制为 3 通道，或在最前面增加 `1x1 conv` / stem adapter 从 1 通道映射到 3 通道或 backbone hidden dim。

## 3. Backbone Adapter 统一接口

为了方便消融，所有 backbone 必须包装成同一个接口：

```python
class BackboneAdapter(nn.Module):
    size_divisor: int
    out_channels: dict[str, int]
    out_strides: dict[str, int]

    def forward(
        self,
        image: Tensor,             # [B, C, H_pad, W_pad]
        valid_mask: Tensor | None,  # [B, 1, H_pad, W_pad]
    ) -> dict[str, Tensor]:
        return {
            "C1": ...,  # stride 4 preferred
            "C2": ...,  # stride 8 preferred
            "C3": ...,  # stride 16 preferred
            "C4": ...,  # stride 32 preferred
        }
```

标准特征约定：

```text
C1: [B, C1, ceil(H_pad / 4),  ceil(W_pad / 4)]
C2: [B, C2, ceil(H_pad / 8),  ceil(W_pad / 8)]
C3: [B, C3, ceil(H_pad / 16), ceil(W_pad / 16)]
C4: [B, C4, ceil(H_pad / 32), ceil(W_pad / 32)]
```

如果某个 backbone 没有天然的四层输出，adapter 必须补齐：

- ViT / DINOv2：将 patch tokens reshape 成 feature map，再通过 projection / pooling / interpolation 构造多尺度特征。
- U-Net / ResUNet：使用 encoder stages 作为 `C1-C4`。
- SegFormer / Swin / ConvNeXt：直接使用 stage features。

实现时，DPT decoder 不直接依赖具体 backbone 类型，只依赖 `BackboneAdapter` 输出的统一 feature dict。

## 4. DPT Decoder 设计

### 4.1 Geometry DPT Decoder

`Geometry DPT Decoder` 接收 backbone 多尺度特征，输出统一空间特征图：

```text
{C1, C2, C3, C4}
  -> feature projection
  -> DPT fusion blocks
  -> F_geom
```

建议默认：

```text
dpt_hidden_dim = 256
token_stride = 16 pixels
F_geom: [B, 256, ceil(H_pad / 16), ceil(W_pad / 16)]
```

`F_geom` 有两个用途：

1. 作为 `mask head` 和 `SDF head` 的输入。
2. 通过 flatten + projection 输出给后续 CSI 融合模型。

下游视觉 tokens：

```text
visual_tokens:    [B, N, C_token]
token_valid_mask: [B, N]
N = ceil(H_pad / token_stride) * ceil(W_pad / token_stride)
C_token = 256 or 512
```

默认：

```text
token_stride = 16 pixels
meter_per_pixel = 0.05 m
每个 token 约覆盖 0.8 m x 0.8 m
C_token = 256
```

`token_valid_mask` 从 `valid_pixel_mask` 下采样得到。只要一个 token 对应区域内包含有效像素即可标为 valid，或采用更严格的有效像素比例阈值，例如 `valid_ratio >= 0.5`。

### 4.2 Conditional DPT Decoder

`Conditional DPT Decoder` 接收 backbone 多尺度特征和 BS 条件图，输出传播相关特征：

```text
{C1, C2, C3, C4} + BS condition maps
  -> multi-scale condition concat
  -> feature projection
  -> DPT fusion blocks
  -> F_prop
```

建议默认：

```text
F_prop: [B, 256, ceil(H_pad / s_los), ceil(W_pad / s_los)]
s_los = 4 or 8 pixels
```

第一版推荐：

```text
s_los = 4 pixels
meter_per_pixel = 0.05 m
LoS / wall-count grid 间距约 0.2 m
```

如果显存或存储压力较大，可改用：

```text
s_los = 8 pixels
grid 间距约 0.4 m
```

### 4.3 BS Condition Maps

给定每个样本的一个 BS 点：

```text
bs_point_px = (x_bs, y_bs)
bs_point_m  = (x_bs * meter_per_pixel, y_bs * meter_per_pixel)
```

构造四个条件图：

```text
bs_heatmap
dx_map = (x - x_bs) * meter_per_pixel
dy_map = (y - y_bs) * meter_per_pixel
log_distance_map = log(sqrt(dx_map^2 + dy_map^2) + eps)
```

`bs_heatmap` 建议使用 2D Gaussian：

```text
sigma_m = 0.2 m
sigma_px = sigma_m / meter_per_pixel
```

条件注入方式固定为 multi-scale concat：

```text
for each feature scale i:
    cond_i = resize([bs_heatmap, dx_map, dy_map, log_distance_map],
                    spatial_size(C_i))
    C_i_cond = concat(C_i, cond_i)
    C_i_cond = Conv1x1(C_i_cond, dpt_hidden_dim)
```

注意：

- `dx_map/dy_map/log_distance_map` 用物理单位，避免不同图像尺寸影响数值尺度。
- padding 区域对应的 condition map 可以正常计算，但 loss 由 valid mask 屏蔽。
- 如果一个 batch 内每张图只采样一个 BS，`bs_point_px` shape 为 `[B, 2]`。
- 如果后续支持每图多个 BS，可将样本展开为 `[B*K, ...]`，或复用 backbone feature 后对 conditional decoder 分批处理。

## 5. 四个输出 Head

### 5.1 Mask Head

输入：

```text
F_geom
```

输出：

```text
mask_logits: [B, 4, H_pad, W_pad]
```

四类：

```text
0 outdoor
1 wall
2 free_space
3 furniture
```

推荐 head：

```text
Conv3x3 -> GN/BN -> GELU/ReLU -> Conv1x1(4)
```

loss：

```text
CrossEntropy + Dice
```

padding 区域 ignore。

### 5.2 SDF Head

输入：

```text
F_geom
```

输出：

```text
sdf_pred: [B, 1, H_pad, W_pad]
```

推荐 head：

```text
Conv3x3 -> GN/BN -> GELU/ReLU -> Conv1x1(1)
```

loss：

```text
SmoothL1 / Huber
```

只在 `sdf_valid_mask=1` 且 `valid_pixel_mask=1` 的区域计算。

### 5.3 LoS Head

输入：

```text
F_prop
```

输出：

```text
los_logits: [B, 1, H_los, W_los]
H_los = ceil(H_pad / s_los)
W_los = ceil(W_pad / s_los)
```

推荐 head：

```text
Conv3x3 -> GN/BN -> GELU/ReLU -> Conv1x1(1)
```

loss：

```text
BCEWithLogitsLoss or FocalLoss
```

只在 `ue_valid_mask=1` 的 grid 上计算。

### 5.4 Wall-count Head

输入：

```text
F_prop
```

输出：

```text
wall_count_logits: [B, 4, H_los, W_los]
```

类别：

```text
0 walls crossed
1 wall crossed
2 walls crossed
3+ walls crossed
```

推荐 head：

```text
Conv3x3 -> GN/BN -> GELU/ReLU -> Conv1x1(4)
```

loss：

```text
CrossEntropy
```

只在 `ue_valid_mask=1` 的 grid 上计算。

## 6. VisualEncoder 统一接口

建议对外暴露统一模型接口：

```python
class VisualEncoder(nn.Module):
    def forward(
        self,
        image: Tensor,                 # [B, C, H_pad, W_pad]
        valid_pixel_mask: Tensor,      # [B, 1, H_pad, W_pad]
        meter_per_pixel: Tensor,       # [B] or scalar
        bs_point_px: Tensor | None,    # [B, 2], required for LoS / wall-count
        output_tasks: bool = True,
    ) -> dict[str, Tensor]:
        ...
```

返回：

```python
{
    "visual_tokens": visual_tokens,          # [B, N, C_token]
    "token_valid_mask": token_valid_mask,    # [B, N]
    "token_grid_shape": (H_token, W_token),

    # returned only when output_tasks=True
    "mask_logits": mask_logits,              # [B, 4, H_pad, W_pad]
    "sdf_pred": sdf_pred,                    # [B, 1, H_pad, W_pad]
    "los_logits": los_logits,                # [B, 1, H_los, W_los]
    "wall_count_logits": wall_count_logits,  # [B, 4, H_los, W_los]
}
```

当 `bs_point_px=None` 时：

- `mask_logits`、`sdf_pred`、`visual_tokens` 仍可输出。
- `los_logits` 和 `wall_count_logits` 不输出，或返回 `None`。

## 7. Backbone 消融计划

第一轮消融建议固定所有 decoder/head，只更换 backbone adapter。

| Backbone | 定位 | 建议用途 | 单卡 4090 预期 |
|---|---|---|---|
| `ResUNet` | 轻量 CNN baseline | 验证数据和任务是否正确 | 最轻，batch 较大 |
| `ConvNeXt-T + DPT` | 现代 CNN baseline | 局部几何和稳定训练对照 | 轻到中等 |
| `SegFormer-B1 + DPT` | dense Transformer 主线 | 第一主力模型 | 单卡友好 |
| `SegFormer-B2 + DPT` | 更强 dense Transformer | 主力增强版 | 单卡可训，batch 较小 |
| `Swin-T + DPT` | 层级 Transformer 对照 | 验证窗口注意力结构 | 单卡可训 |
| `Swin-S + DPT` | 更强层级 Transformer | 第二阶段对照 | 建议 2-4 卡 |
| `DINOv2-S + DPT` | 表征优先主线 | 验证 patch token 表征能力 | 单卡可训 |
| `DINOv2-B + DPT` | 更强表征模型 | 最终候选之一 | 建议 2-4 卡 |

第一阶段建议：

```text
ResUNet
ConvNeXt-T + DPT
SegFormer-B1 + DPT
DINOv2-S + DPT
```

第二阶段根据第一阶段结果扩展：

```text
SegFormer-B2 + DPT
Swin-T/S + DPT
DINOv2-B + DPT
```

消融时必须保持以下配置一致：

- 输入物理尺度。
- padding 策略。
- DPT hidden dim。
- token stride。
- LoS / wall-count stride。
- 四个任务 loss 权重。
- BS 采样策略。
- train/val/test scene split。

## 8. DatasetAdapter 边界

模型设计不绑定具体数据集目录结构。不同数据来源需要单独实现 `DatasetAdapter`，将原始文件转成统一 sample schema：

```python
{
    "scene_id": str,
    "image": Tensor,                 # [C, H, W]
    "mask": Tensor,                  # [H, W]
    "sdf": Tensor,                   # [1, H, W]
    "sdf_valid_mask": Tensor,        # [1, H, W]
    "bs_point_px": Tensor,           # [2]
    "los_map": Tensor,               # [1, H_los, W_los]
    "wall_count_map": Tensor,        # [H_los, W_los]
    "ue_valid_mask": Tensor,         # [H_los, W_los]
    "meter_per_pixel": float,
    "orig_size": tuple[int, int],
}
```

训练时每次从一个 scene 的预生成 BS 列表中随机取一个 BS 样本。这样同一个场景在不同 epoch 可以提供不同的条件化 LoS / wall-count 监督。

## 9. Loss 组合

推荐初始 loss：

```text
L_total =
  1.0 * L_mask
+ 0.3 * L_sdf
+ 0.5 * L_los
+ 0.3 * L_wall_count
```

第一轮不建议使用过复杂的动态 loss balancing。等单任务指标和下游定位指标稳定后，再考虑 uncertainty weighting、GradNorm 或 task-specific sampling。

## 10. Shape 与功能测试

实现后必须覆盖以下 shape tests：

1. 不同 `H/W` 的样本可以 padding 成 batch。
2. `mask_logits` 和 `sdf_pred` 可裁回每个样本原始 `H_i/W_i`。
3. `los_logits` 和 `wall_count_logits` 的 spatial shape 与 `s_los` 对齐。
4. `visual_tokens.shape[1] == token_valid_mask.shape[1]`。
5. padding token 不进入下游融合模型，也不参与 loss。
6. `bs_point_px=None` 时，模型仍能输出 `visual_tokens/mask/sdf`。
7. 更换 backbone 后，DPT decoder 和四个 heads 不需要改接口。

## 11. 默认假设

- 预训练任务固定为 3 类任务、4 个输出。
- 模型保留固定物理比例尺，不做统一 resize。
- 第一版 `meter_per_pixel=0.05`。
- 第一版将家具视作自由空间，不作为 RF 遮挡物。
- DPT 使用两套 decoder：geometry decoder 和 conditional decoder。
- 最终下游融合模型使用 patch / spatial tokens，而不是单个全局 token。
- 最终模型默认保留 `Backbone + Geometry DPT Decoder + token projection`。
