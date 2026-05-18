# SceneGen

SceneGen 是一个面向 Linux 环境的轻量级室内场景生成项目。它基于空场景和归一化资产，随机生成带家具、桌椅、小物件的 3D 场景，并同步导出 Sionna/Mitsuba 可加载的场景文件和平面图。

当前主工作流是 Bistro 场景生成：以 `data/scene/scene.obj` 作为空 Bistro 场景，以 `data/assets/manifest.json` 管理资产，然后按规则随机摆放桌子、椅子、地面物体、桌面小物和已有台面上的小物。

## 主要功能

- 基于空 Bistro 场景生成新的室内布局。
- 支持 generated 模式，生成简单矩形房间布局。
- 自动读取本地资产 OBJ，忽略迁移遗留的 Windows 绝对路径。
- 输出组合后的 `scene.obj` 和 Sionna RT 可加载的 `scene.xml`。
- 输出 `placements.json`，记录每个资产的类别、位置、朝向、包围盒和材质信息。
- 输出 `label.json`，保留或生成 BS/UE 点位。
- 可选使用 Sionna RT 验证生成的 `scene.xml`。
- 每个场景同步生成 floorplan：
  - 指定高度或逐层高度投影图 `000_z_*.png`
  - 总览图 `floorplan/preview.png`
  - 可选去噪几何图 `floorplan/geometry_clean.png`
  - 侧视图 `floorplan/side_view.png`
  - 投影栈 `floorplan/stack.npz`
  - 元信息 `floorplan/meta.json`
  - 语义平面图 `floorplan/semantic.png`
  - 语义标注 `floorplan/semantic.json`
- 使用 YAML 作为主配置入口，CLI 参数可以覆盖 YAML。
- 每次运行都会保存最终生效配置 `effective_config.yaml`，方便复现。

## 目录结构

```text
SceneGen/
  config/
    default.yaml          # 默认运行配置
    README.md             # 配置字段说明
  data/
    scene/                # 空 Bistro 场景与 label
    assets/               # 资产 OBJ/PNG/JSON 与 manifest
  src/scenegen/
    assets.py             # 资产读取、分类、路径解析
    cli.py                # 命令行入口与主流程
    config.py             # YAML 读取、CLI 覆盖、有效配置保存
    exporters.py          # OBJ/XML/label/manifest 输出
    floorplan.py          # 3D 网格转二维平面图
    geometry.py           # OBJ、几何、支撑面、碰撞辅助逻辑
    models.py             # 数据结构
    paths.py              # 默认路径与常量
    placement.py          # 场景摆放规则
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

默认配置文件：

[config/default.yaml](/home/sunmeiyuan/projects/SceneGen/config/default.yaml)

配置字段说明：

[config/README.md](/home/sunmeiyuan/projects/SceneGen/config/README.md)

运行时默认读取 `config/default.yaml`。如果命令行传入参数，则命令行参数会覆盖 YAML 中对应字段。每次运行会在结果目录写出最终生效配置：

```text
<run_dir>/effective_config.yaml
```

这份文件是实际用于生成结果的配置，已经包含所有 CLI 覆盖后的值。

## 快速开始

使用默认配置生成 Bistro 场景：

```bash
uv run scenegen
```

指定配置文件：

```bash
uv run scenegen --config config/default.yaml
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
uv run scenegen --mode generated --scenes 1 --run-name smoke_generated --output-dir /tmp/scenegen-smoke --clean
```

## 输出结构

一次运行会生成一个 run 目录，默认在 `results/<timestamp>/` 下：

```text
results/<run_name>/
  effective_config.yaml
  manifest.json
  manifest_bistro.json 或 manifest_generated.json
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
    label.json
    placements.json
    assets/
    floorplan/
      000_z_*.png
      geometry_raw.png
      preview.png
      side_view.png
      stack.npz
      meta.json
      semantic.png
      semantic.json
  bistro_0001/
  ...
```

重要文件说明：

- `scene.obj`: 合并空场景与新摆放资产后的 OBJ。
- `scene.xml`: Sionna RT/Mitsuba 场景文件。
- `placements.json`: 资产摆放结果、包围盒、材质映射和父子关系。
- `label.json`: BS/UE 点位。
- `manifest.json`: 本次 run 的汇总信息。
- `effective_config.yaml`: 本次 run 实际生效的配置。
- `summary_obj/`: 每个场景 `scene.obj` 的汇总副本。
- `summary_floorplan_raw/`: 每个场景 `floorplan/geometry_raw.png` 的汇总副本。
- `floorplan/preview.png`: 指定高度或逐层投影总览。
- `floorplan/geometry_raw.png`: 第一张几何投影图。默认是 `1.6m` 高度的原始密度投影。
- `floorplan/geometry_clean.png`: 可选输出，对原始密度投影进行低密度过滤、孤立点过滤和小半径形态学连通后的几何占据图。
- `floorplan/geometry_clean_preview.png`: 可选输出，clean 图总览。
- `floorplan/side_view.png`: 侧视投影。
- `floorplan/stack.npz`: 二值投影栈和高度层数据。
- `floorplan/semantic.png`: 基于资产 placements 绘制的语义平面图。
- `floorplan/semantic.json`: 每个资产的类别、旋转矩形、多边形坐标、颜色和父子关系。

## Bistro 禁区

Bistro 模式支持 XY 禁放区，用于避免在指定区域摆放物体。默认配置在 `config/default.yaml`：

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

## Floorplan 原理

当前 floorplan 有两种输出，默认都会生成。

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

语义平面图更清晰、速度更快，也更适合后续做标注、路径规划或布局质量检查。

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
uv run scenegen --mode bistro --scenes 1 --run-name smoke_bistro --output-dir /tmp/scenegen-smoke --validate-sionna --clean
```

## 开发说明

- 默认入口是 `uv run scenegen`，不再保留根目录脚本入口。
- 新增配置项时，应同步更新：
  - `config/default.yaml`
  - `config/README.md`
  - `src/scenegen/config.py`
- 新增输出字段时，应同步检查：
  - `manifest.json`
  - `effective_config.yaml`
  - 测试用例
- floorplan 目前基于生成后的 OBJ，因此会反映最终几何结果；如果需要类别级标注，应在下一版基于 `placements.json` 或内存中的 `PlacedAsset` 生成语义图。
