# raspberrygirl-presenter（聚合输出模块）

树莓娘的聚合输出模块：接收 waycast 核心派发的表现输出单元，完成语音合成与播放、字幕
揭示、形象表现与 PPT 呈现，并把"队列播完"事件（唯一输入帧，ADR-0005）送回核心。
单元进入**播放队列**，一切表现事件与开播同步（ADR-0008）。核心层术语（Agent、
Output unit、Output result、Unified input、Invocation 等）以
[waycast GLOSSARY](../waycast/GLOSSARY.md) 为准，此处只定义模块本地术语。

## Language

**宿主进程 (host process)**:
聚合输出模块中持有 waycast 连接与模块身份的那个进程：播放队列、TTS、播放时钟与
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

**播放队列 (playback queue)**:
宿主进程内有界的播放任务序列，按输出单元到达顺序排列；列表顺序即表演顺序，队头
（`tasks[0]`）即正在播放的任务，后台合成 worker 沿队列顺序向前串行预合成其余任务
（ADR-0008）。溢出即对该单元回失败 result。
_Avoid_: 单元队列、expression queue（以合成轮次为中心的旧管线术语，ADR-0008 起废弃）、缓冲区、消息池

**预合成 (pre-synth)**:
后台合成 worker 沿队列顺序合成最靠前的未合成任务并缓存整句 PCM：队头边合成边
播放（流式），队列全部合成完才空闲；引擎同时在飞合成数恒 ≤1。
_Avoid_: 预取（旧 lookahead 深度 1 的术语，已废弃）

**开播 (start of play)**:
任务的第一个音频字节交给播放器的瞬间；PPT 翻页、emotion、motion、字幕切换与口型
统一在开播时刻触发（ADR-0008）。无音频的纯视觉单元以成为队头被执行的时刻为开播。
_Avoid_: 开始合成（合成开始不等于出声）

**无缝接管 (seamless takeover)**:
控制单元 stop 布防后，stop 之后入队的新任务在其音频就绪（首个 PCM 字节可用）的
刹那停止正在播放的队头并立即开播，中间不产生静默空窗；接管的开播不等待句间停顿。
_Avoid_: 抢占式打断（stop 本身绝不掐音频）

**控制单元 (control unit)**:
只携带 `control:"stop"` 的输出单元：取消进行中的后台合成、丢弃所有**尚未开播**的
任务，并**不停止正在播放的队头**——它继续播，直到自然播完或被无缝接管（ADR-0008）。
打断 = 一个控制单元加新句单元，不存在独立的"替换"语义。
_Avoid_: replace 指令、clear-stop（旧架构已废弃术语）

**队列播完 (queue finished)**:
向 hub 发出的输入帧：控制单元丢弃了任务时立即发 interrupted 变体，播放队列真正
排空后再发完成变体，两者独立（ADR-0008）；它是"推进并讲解"持续 Invocation 循环
的心跳。
_Avoid_: 播放完成事件、idle 通知

**线性揭示 (linear reveal)**:
字幕方案：按实际播放时钟对文本均匀逐字（拉丁语按词）揭示，不依赖词级时间戳。
_Avoid_: 卡拉OK对齐（viseme/音素级对齐不是本方案）

**句间停顿 (sentence gap)**:
上一句音频播完到下一句开口之间的**最少**间隔（`PRESENTER_SENTENCE_GAP_SECONDS`，
默认 1.0s）：合成等待本身计入间隔、不叠加，每轮首句不等，无缝接管的开播不等，
期间屏上保持上一句全文字幕。
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
