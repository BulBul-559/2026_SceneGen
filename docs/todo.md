# SceneGen TODO

本文档记录当前已经讨论但尚未实现、或已经实现第一版但仍需要继续完善的事项。优先用于交接和后续开发排期。

## Floorplan Geometry Projection

当前状态：

- 默认投影方式仍为 `floorplan.geometry.projection: sampling`。
- 已新增可选投影方式 `ray_height_filtered`。
- `ray_height_filtered` 当前实现为确定性的 height-filtered triangle raster projection：遍历 mesh 三角面，投影到 XY 网格，按 `bottom_m <= z <= target_height` 判断像素占据。
- 输出仍保持 `floorplan_1p60.png`、`preview.png`、`side_view.png`、`stack.npz`、`meta.json`。

尚未实现或仍需完善：

1. 真实 top-down ray casting backend
   - 当前 `ray_height_filtered` 不依赖 `trimesh.ray`，不是严格意义上的逐像素垂直射线求交。
   - 后续可评估稳定引入 `embreex`、`pyembree` 或其他 BVH/ray backend。
   - 目标是对每个像素的 XY column 查询所有 `z` 命中点，再按目标高度过滤。

2. 保守栅格化
   - 当前主要基于像素中心判断三角形覆盖。
   - 非常细、非常斜、投影面积小于一个像素的三角面仍可能漏掉。
   - 竖墙退化三角形已经做了线段兜底，但还不是完整的 polygon-pixel overlap。
   - 后续应实现 conservative rasterization 或像素/三角形相交判定。

3. 空间加速
   - 当前新方法主要瓶颈仍是遍历和处理大量三角面。
   - 降低图像分辨率不一定显著加速，因为三角面处理成本仍在。
   - 后续可按 XY tile 建三角面索引，或引入 BVH，减少每次投影处理的候选面数量。

4. 分层投影
   - 当前 floorplan geometry 直接对最终 `scene.obj` 统一投影。
   - 后续可拆出 architecture、wall、floor、furniture 等层。
   - 分层后可以和 `class_mask`、label free-space 采样共享中间结果，减少重复逻辑。

5. 视觉规范固定
   - 新方法和 sampling 的灰度密度含义不完全一致。
   - 如果要作为正式模型训练输入，应固定图像渲染规范，例如二值/灰度、alpha、前景颜色、背景色、抗锯齿策略。
   - 需要决定训练主图到底使用 `sampling`、`ray_height_filtered` 还是 class mask。

6. 默认值策略
   - 当前为避免行为突变，默认仍保留 `sampling`。
   - 若后续验证新方法在更多场景上稳定，应评估是否将默认值改为 `ray_height_filtered`。

7. 全量生产性能评估
   - 已在单个真实 front3d 场景上做过小基准。
   - 仍需要在不同面积、不同三角面数量、不同家具密度的场景上做批量 benchmark。
   - 建议记录每个场景的三角面数量、分辨率、耗时、输出体积和 occupied pixel count。

8. 回归测试与基准图
   - 当前已有简单 fixture 测试和 front3d smoke。
   - 后续可增加更多几何 fixture：薄墙、门洞、斜墙、楼梯、悬空物、天花板、高桌面。
   - 可考虑引入小尺寸 golden image 或 hash 测试，防止投影算法重构时悄悄改变输出语义。

## Label And Mask Follow-ups

当前状态：

- front3d label 支持 panel/walk 两种 UE 采样策略。
- 支持中心 BS `BS_CENTER` 和房间内 wall/corner BS。
- front3d class mask 支持 outdoor、wall、free_space、furniture 四类。
- 门洞/开口逻辑由 `front3d.openings` 控制。

尚未实现或仍需完善：

1. 更可靠的门洞/开口验证
   - 目前门洞主要依赖 3D-FRONT mesh type 和开口投影。
   - 需要更多真实场景抽查，验证门洞区域是否与 floorplan、class mask、UE 采样一致。

2. Label 与 class mask 共享中间结果
   - 当前 label free-space 与 class mask 仍有各自的生成逻辑。
   - 后续可将 indoor/wall/furniture/opening mask 抽成共享模块，减少语义漂移。

3. BS 策略扩展
   - 当前有中心 BS 和 `wall_or_corner`。
   - 后续可增加 ceiling AP、wall-mounted AP、按房间功能布点、按覆盖半径优化布点等策略。

4. LOS/NLOS 验证
   - 当前 label validation 不做电磁传播层面的 ray test。
   - 后续可为 BS/UE 点位增加 LOS/NLOS 比例统计，辅助筛选更适合定位实验的场景。

## 3D-FRONT Data Pipeline Follow-ups

当前状态：

- phase1 已整理 3D-FRONT/3D-FUTURE 数据，生成 SceneGen 可索引的对象和建筑 manifest。
- front3d 模式可以复现 3D-FRONT 原始组合场景。

尚未实现或仍需完善：

1. 基于 3D-FRONT 资产的随机生成模式
   - 当前 front3d 只复现原始已有组合。
   - 后续可实现 `front3d_random` 或类似模式，从 3D-FUTURE 资产中随机布置家具。

2. 材质标注精细化
   - 当前 Sionna 材质映射仍是轻量规则。
   - 后续可结合 3D-FUTURE category、material、texture 信息做更细的电磁材质标注。

3. 数据质量分级
   - 当前 precheck 能跳过明显异常场景。
   - 后续可输出更细的质量等级，例如 usable、needs_review、bad_geometry、bad_label、bad_material。

## Production Dataset Follow-ups

1. 全量生成策略
   - 当前已有 30 个正式 front3d 场景结果。
   - 全量 6000+ 场景生成前，应确定 floorplan projection、label variants、class mask 是否开启、输出压缩策略。

2. 数据集划分
   - 当前项目只负责生成。
   - 后续可以另起脚本做 train/val/test split，避免和生成逻辑耦合。

3. 输出体积控制
   - 可评估是否默认保留 `scene.obj`、`scene.xml`、`label/*.json`、`floorplan/*.png`、`class_mask.npz`，而把中间预览图设为可选。
   - 对正式训练集可增加压缩/归档脚本。
