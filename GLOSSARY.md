# raspberrygirl-presenter（聚合输出模块）

树莓娘的聚合输出模块：接收 waycast 核心派发的表现输出单元，完成语音合成与播放、字幕
揭示、形象表现与 PPT 呈现，并把"队列播完"事件（唯一输入帧，ADR-0005）送回核心。
核心层术语（Agent、
Output unit、Output result、Unified input、Invocation 等）以
[waycast GLOSSARY](../waycast/GLOSSARY.md) 为准，此处只定义模块本地术语。

## Language

**宿主进程 (host process)**:
聚合输出模块中持有 waycast 连接与模块身份的那个进程：单元队列、TTS、播放时钟与
input/result 都由它发出。
_Avoid_: 核心进程、主控、backend

**呈现进程 (presentation process)**:
Godot 窗口进程，只负责呈现（字幕揭示、形象、PPT），是宿主进程的内部组件，不是独立的
waycast 模块。
_Avoid_: 前端模块、frontend module（旧架构已废弃的独立前端模块）

**presentation bridge**:
宿主进程与呈现进程之间的本机 WebSocket 通道；其消息是模块内部实现，不属于 waycast
wire 协议。
_Avoid_: waycast 通道、内部协议端点

**单元队列 (expression queue)**:
宿主进程内有界的待执行输出单元序列；列表顺序即表演顺序，溢出即对该单元回失败
result。
_Avoid_: 缓冲区、消息池

**控制单元 (control unit)**:
只携带 `control:"stop"` 的输出单元：清空单元队列并立即停止当前播放。打断 = 一个控制
单元加新句单元，不存在独立的"替换"语义。
_Avoid_: replace 指令、clear-stop（旧架构已废弃术语）

**队列播完 (queue finished)**:
本轮派发的单元全部播放完毕（或被打断清空）后向 hub 发出的输入帧；它是"推进并讲解"
持续 Invocation 循环的心跳。
_Avoid_: 播放完成事件、idle 通知

**线性揭示 (linear reveal)**:
字幕方案：按实际播放时钟对文本均匀逐字（拉丁语按词）揭示，不依赖词级时间戳。
_Avoid_: 卡拉OK对齐（viseme/音素级对齐不是本方案）

**句间停顿 (sentence gap)**:
上一句音频播完到下一句开口之间的**最少**间隔（`PRESENTER_SENTENCE_GAP_SECONDS`，
默认 1.0s）：合成等待本身计入间隔、不叠加，每轮首句不等，期间屏上保持上一句
全文字幕。
_Avoid_: 句间延迟（它是最少间隔而非附加延迟）

**deck**:
一套预导出为逐页 PNG 的讲解材料，按名切换；PPT 的显示与翻页执行都在本模块内。
manifest（schema 1.2.0）为每页携带**页面内容**与**建议讲稿**两段文本。
_Avoid_: PPT 模块（不存在独立 PPT 模块）、slides 目录（口语）

**讲稿与页面内容 (speaker script / page summary)**:
deck manifest 中与逐页 PNG 一一对应的两段纯文本（ADR-0006/0007）：**页面内容**
忠实描述这页展示什么（Agent 的"眼睛"，pdftotext 抽取文本层生成，可被视觉模型
或人工描述覆盖）；**建议讲稿**建议怎么讲这页（开场、语气、展开方式，供人工自
定义与注入特定指令，自动抽取不填充）。二者随 queue_finished 与 status 送达无
视觉的核心 Agent，分层互不踩踏。
_Avoid_: 备注（含糊）、字幕（是播放中的 speech 揭示文本，不是讲稿）
