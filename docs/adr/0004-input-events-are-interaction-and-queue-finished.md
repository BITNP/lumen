# 输入帧只两种：user_interaction 与 queue_finished

> **状态：已被 ADR-0005 取代（2026-10-30）。** 聚合输出模块定为纯展示，
> `user_interaction` 输入帧已整体移除；`queue_finished` 部分仍然有效。

每个输入帧都触发一次 Invocation，所以往 hub 送的事件必须克制。ADR-0010 的输入侧例子
列举了"前端交互、翻页完成、当前页、进度"。我们决定把它们收窄为两种输入帧：

- `user_interaction`：前端用户点击/请求（含用户操作引起的 deck 变化），见
  waycast GLOSSARY 的 Frontend interaction。
- `queue_finished`：本轮派发的单元队列**全部播放完毕**（附完成句数、当前页码等摘要；
  被打断清空时为 interrupted 变体）。它是"推进并讲解"持续 Invocation 循环的心跳——
  Agent 讲完一页后静默，靠这个事件醒来决定下一步，没有它循环就断了。

翻页完成不单列：翻页是瞬时动作，成功/失败随该单元的 result 回报，其完成状态被
`queue_finished` 覆盖（翻页单元也在队列里，队列排空即翻页已过）。当前页、播放进度、
生效 TTS 引擎、presentation 可用性一律走 status query（Agent 的
`status_presenter` 工具），按需拉取而非推送轰炸。合成/播放失败走 result，不占输入。
