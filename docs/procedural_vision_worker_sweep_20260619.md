# Procedural Vision-Only Worker Sweep 2026-06-19

本文记录 `procedural_front3d_vision` 模式在 `config/tasks/procedural_front3d_vision_full_simulation.yaml` 下的 batch worker 扫参结果。

## 环境和配置

- 版本：`SceneGen 4.1.0`
- 配置：`config/tasks/procedural_front3d_vision_full_simulation.yaml`
- 模式：`pipeline.mode: procedural_front3d_vision`
- 输出：`output.profile: vision_only`
- 调度：`batch.scheduler: hybrid`
- 扫参规则：从 `workers=24` 开始，每次 +8；每组 `pipeline.scenes = workers * 3`
- 判断信号：wall time、吞吐、产物大小、maps 耗时、`/proc/pressure/io` PSI

## 结果

| workers | scenes | run | elapsed_s | scenes/s | size | maps_elapsed_s | IO PSI after avg60 some/full | result |
| --- | ---: | --- | ---: | ---: | ---: | ---: | --- | --- |
| 24 | 72 | `procedural_front3d_vision_sweep_w24_n72_20260619_225727` | 61.39 | 1.17 | 485M | 6.10 | 0.88 / 0.62 | 通过 |
| 32 | 96 | `procedural_front3d_vision_sweep_w32_n96_20260619_225829` | 69.22 | 1.39 | 654M | 6.93 | 1.68 / 1.29 | 通过 |
| 40 | 120 | `procedural_front3d_vision_sweep_w40_n120_20260619_225938` | 85.56 | 1.40 | 832M | 7.88 | 2.13 / 1.60 | 通过 |
| 48 | 144 | `procedural_front3d_vision_sweep_w48_n144_20260619_230118` | 83.34 | 1.73 | 1016M | 9.94 | 2.45 / 1.74 | 通过 |
| 56 | 168 | `procedural_front3d_vision_sweep_w56_n168_20260619_230254` | 100.25 | 1.68 | 1.2G | 8.53 | 3.03 / 2.10 | 通过 |

## 结论

- 推荐稳定 worker：`48`
- 已测上限：`56`
- 默认不建议继续提高到 56 以上：56 worker 吞吐低于 48，IO PSI 继续升高，已经出现边际收益递减。
- 24 -> 32 有明显提升；32 -> 40 基本持平；48 是本轮最优点。
- 初始 60-scene 验证 run `procedural_front3d_vision_test_60_20260619_225525`：24 workers，60/60 成功，wall time `51.23s`，产物 `402M`，maps 60/60，且未生成 `scene.obj`、`scene.xml` 或 `assets/`。
- 最终模板 60-scene 验证 run `procedural_front3d_vision_final_test_60_20260619_230709`：48 workers，60/60 成功，wall time `37.49s`，产物 `402M`，maps/geometry/pair cache/class mask 60/60，且未生成 `scene.obj`、`scene.xml` 或 `assets/`。
- 后续根据 label 体积和 BS 稳定性审查，当前 vision-only 任务模板已收敛为只生成 `label_panel_0p5`，并用 `maps.bs_label.name: label_panel_0p5` 生产 maps；上面的耗时和体积是多 label 旧模板的历史基线。
