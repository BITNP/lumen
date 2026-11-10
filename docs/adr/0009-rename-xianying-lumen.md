# 更名为显影（Lumen）

## Status

accepted

## Context

树莓娘体系确立诗意命名族（waycast ADR-0015）：核心 waycast 中文名「道枢」，三个模块仓库取「一模块一首诗」的中英双名。本模块取 **显影 / Lumen**：显影是潜影浮现为画面的一步——Agent 隐空间中的意图在此显影为声光；影戏是中国的原始渲染器；Lumen 为光通量单位。命名全文见 README「命名来历」。

## Decision

本仓库自 2026-11-10 起使用下列标识符，旧标识符全部废止（不再用于新设计）：

| 层面 | 旧 | 新 |
| --- | --- | --- |
| 仓库 / 目录 | raspberrygirl-presenter | lumen |
| Python 包名与目录 | `presenter`（src/presenter） | `lumen`（src/lumen） |
| pyproject 项目名 | raspberrygirl-presenter | lumen |
| CLI 命令 | `presenter` | `lumen` |
| 环境变量前缀 | `PRESENTER_` | `LUMEN_` |
| wire 模块名（MODULE_NAME、result/input 帧 module 与 source） | `presenter` | `lumen` |
| Godot 呈现窗口标题（project.godot config/name） | raspberrygirl-presenter | lumen |

本 ADR 之前的历史文档（docs/adr/ 旧条目、git 历史）保持原文不改写，按本表读解旧标识符。

## Consequences

- 部署环境的 .env 需把 `PRESENTER_*` 键改名为 `LUMEN_*`（本机已完成）；`.env.example` 同步。
- wire 模块名变化会进入核心全局记忆、能力目录与输出结果 feed（Agent 可见的模块名变为 `lumen`）；hub 侧无按模块名绑定的令牌，无需核心改动。
- GitHub 远端仓库改名是网页端手工操作，克隆 URL 需另行更新。
- waycast GLOSSARY 与各仓库间交叉引用同步改用新名。
