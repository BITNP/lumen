# 每页讲稿存入 deck，并随 queue_finished 喂给核心 Agent

核心 Agent 使用的语言模型**没有视觉**：它只能从 `ppt` 指令与 `deck/page/
total_pages` 状态知道"现在在第几页"，无法知道页面上写了什么，也就无法讲解。
为此把**逐页讲稿 (per-page script)** 作为 deck 的一等数据：

- **存储**：deck manifest（`manifest.json`）schema_version 升为 `1.1.0`，新增
  与 `pages` 等长的 `scripts` 字段（每页一段纯文本，上限
  `MAX_SCRIPT_CHARS` 字符）；`1.0.0`（无讲稿）继续可读。
- **生成**：`tools/prepare_deck.py` 导出时用 `pdftotext` 抽取 PDF 文本层作为
  默认讲稿；讲稿需要人工或视觉模型撰写时用 `--scripts <json>` 覆盖；
  `--first/--last` 支持只导出页范围。
- **送达 Agent**：`queue_finished` 的 input content 与 status 都带上当前展示页
  的讲稿（截断到固定预算）。Agent 每讲完一轮醒来，就同时拿到"第几页 + 这页讲
  什么"，据此推进翻页与讲解；不新增输入帧类型，也不新增 waycast 契约
  （ADR-0005 的唯一输入帧不变，ADR-0010 的"模块发回状态、Agent 推进讲解"
  正是这个通道的设计意图）。

## Considered Options

- **给 Agent 换视觉模型直接看图**：被否。部署模型无视觉是现状，且逐页图像
  token 成本远高于文本；讲稿是离线一次性成本。
- **新增 ppt "read_page" 工具/指令让 Agent 主动读页**：被否。模块是纯展示
  面（ADR-0005），输入帧只有 queue_finished；把讲稿搭在既有心跳与 status 上
  即可，无需扩张 wire 面。
- **讲稿存 manifest 之外的独立文件**：被否。manifest 已是 deck 的唯一校验入口
  （prepare 校验、open 时再校验），另立文件会分裂单一事实源。

## Consequences

- deck 打开时讲稿随行，翻页/查状态即得；无讲稿的旧 deck 行为不变。
- 讲稿质量决定讲解质量：文本层差的 PDF（扫描件等）应经视觉模型或人工补写
  `--scripts`，prepare 对空白页只留空串并在输出中提示。
- manifest 体积上限相应放宽（`MAX_MANIFEST_BYTES`），讲稿按页长有硬上限。
