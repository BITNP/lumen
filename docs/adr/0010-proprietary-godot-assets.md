# godot 美术资产专有，不入库

## Status

accepted

## Context

呈现窗口依赖 `godot/assets/`（Live2D 模型 `.moc3` 与贴图、动作 json、版面 SVG、字体，约 11 MB、28 个文件）。这些美术资产为北京理工大学网络开拓者协会专有（树莓娘形象资产），不得发布到公开代码仓库；其余 godot 内容（`scripts/`、`scenes/`、`project.godot`、`addons/gd_cubism` 第三方 SDK 绑定）均为自有代码或开源第三方，可公开。仓库历史上曾将 `godot/assets/` 提交入库，但公开远端从未推送过这些内容。

## Decision

- `godot/assets/` 列入 `.gitignore`，只存本机，永不入库；新环境从网协内部获取该目录（README「运行」与 DEVELOPMENT「舞台视觉」注明）。
- 本地 git 历史用 `git filter-repo --path godot/assets --invert-paths` 将资产从全部历史提交中移除（2026-11-10 执行，推送到公开远端前完成，历史中资产 blob 计数为 0）。
- 代码、场景、配置与第三方 SDK 照常入库；`.godot/` 编辑器缓存维持原有忽略。

## Consequences

- 公开克隆无法直接启动呈现窗口（无资产），宿主与测试不受影响；这是接受的行为。
- 历史提交哈希因 filter-repo 全部重写；本地过滤前的完整历史备份在维护者机器（bundle），仅用于灾难恢复，不外发。
- 后续新增美术资产默认落在 `godot/assets/` 下即自动受此保护；往该目录添加文件时提交前确认 `git status` 不含它们。
