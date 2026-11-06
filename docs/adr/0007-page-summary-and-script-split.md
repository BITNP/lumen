# 页面内容与建议讲稿分离为两个逐页字段

细化 ADR-0006：单字段讲稿把"页面上有什么"与"建议怎么讲"混在一段文本里，
人工定制时两类信息互相踩踏（改讲法会误删内容事实，注入特定指令又会污染内容
描述）。对无视觉的核心 Agent，这是两种不同性质的输入，因此每页拆成两个平行
字段：

- **页面内容 (page summary，`summaries`)**：忠实描述该页展示的内容（标题、
  条目、图表、当前 overlay 状态），是 Agent 的"眼睛"。manifest schema_version
  升为 `1.2.0`；`--first/--last` 导出时默认由 pdftotext 文本层填充，人工或
  视觉模型的更好描述用 `--scripts` 的 `summary` 键覆盖。
- **建议讲稿 (speaker script，`scripts`)**：建议怎么讲这页（开场、语气、停顿、
  展开方式），可被人工任意改写或注入特定指令；来自 `--scripts` 的 `script`
  键（或纯字符串值），自动抽取不填充它。

`--scripts <json>` 的每页值可以是字符串（只给讲稿）或 `{"summary": …,
"script": …}` 对象（两者都给）。`queue_finished` 的 input content 与 status
同时携带两个字段（各自独立截断预算），1.0.0/1.1.0 的旧 deck 继续可读。

## Consequences

- Agent 每轮拿到"这页是什么 + 建议怎么讲"两层信息，人工/指令层与内容层互不
  干扰；deck 作者可以只改 `scripts` 调整风格而不动内容事实。
- 两字段皆可选：纯文本层 deck 只有 summary；纯人工 deck 可以只有 script。
- 逐页内容字段取代 ADR-0006 中"讲稿即唯一逐页文本"的存储形态；送达通道
  （queue_finished + status，ADR-0005 唯一输入帧不变）不变。
