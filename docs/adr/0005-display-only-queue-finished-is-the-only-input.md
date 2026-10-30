# 聚合输出模块是纯展示：唯一输入帧是 queue_finished

树莓娘的聚合输出模块（前端窗口 + 语音）定位为**纯展示面**：观众不看也不碰它，
一切"参与"（提问、打断、推进）都经由各自的输入模块（语音、弹幕等）进入核心。
因此本模块**不存在任何用户可操作元素**，Godot 呈现进程不监听、不上报任何用户
操作（点击、半屏翻页请求等全部移除）；presentation bridge 的上行只剩连接时的
`hello`，宿主收到其它上行帧一律忽略。

本模块发往 hub 的输入帧由此只剩一种：`queue_finished`（含 interrupted 变体）。
它的职责是**推进讲解、推进 Invocation**：Agent 讲完一轮后静默，靠这个心跳醒来
决定下一步（ADR-0004 中关于 queue_finished 是循环心跳的论证原样保留）。

取代 ADR-0004 中 `user_interaction` 一类输入帧的设计。waycast ADR-0010 输入侧
列举的"前端交互（用户点击、请求）"自此不为本模块产生；核心侧 wire 契约不变
（input 帧类型通用，只是本模块不再发送此类帧），翻页/进度/引擎等状态仍一律走
status query（ADR-0004）。

## Consequences

- Godot 端删除 `_unhandled_input` 与 `user_event` 上报；bridge 协议删去
  `user_event` 消息与宿主的 user event handler 装配。
- capability 的 input 描述只声明 `queue_finished`，Agent 不会期待前端交互事件。
- PPT 翻页的决定权本就在 Agent（`ppt` 单元字段）；移除点击请求后，前端对
  deck 的唯一影响不复存在，语义不变。
- waycast GLOSSARY 的 Frontend interaction 术语对本模块不再适用（该词若日后
  被其他前端模块启用需重新定义范围）。
