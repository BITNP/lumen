# raspberrygirl-presenter 开发者文档

本文面向**开发 presenter 的人**：进程形态与内部协议、TTS 降级策略、舞台视觉、
里程碑与测试。安装、配置、运行、输出单元契约与 deck 准备请看用户文档
[README.md](README.md)；术语见 [GLOSSARY.md](GLOSSARY.md)，决策见
[docs/adr/](docs/adr/)。

## 形态（ADR-0002）

- **宿主进程**（本仓库 Python 部分）：waycast 模块身份。连 hub `/ws/modules`（首帧
  capability advertisement），收 `output_unit` 帧入**播放队列**（ADR-0008）：后台合成
  worker 沿队列顺序串行预合成（引擎在飞 ≤1），播放 worker 只播队头、边合成边播；
  PPT/emotion/motion/字幕/口型统一在**开播**瞬间派发。逐单元回 `result` 帧；应答
  `status_query`；发 `input` 帧（唯一一种 `queue_finished`，ADR-0005）。断线按指数
  退避重连。控制单元 stop 不掐正在播放的队头，只丢弃未开播任务并布防无缝接管。
- **呈现进程**（`godot/`，Godot 4.6 + gd_cubism）：纯呈现、纯展示面（无任何用户
  可操作元素）。经本机 presentation bridge（`127.0.0.1:PRESENTER_BRIDGE_PORT`
  上的 WebSocket）接收字幕揭示、表情/动作、PPT 指令，上行只有连接握手。
  bridge 协议是模块内部实现，与 waycast 无关。
  字幕播完后保留在气泡上，直到下一条字幕开始揭示才被替换（轮次结束不清空）；
  字幕与该句的 `speech_state` 在第一个音频块开始播放时才切换/开口，整句合成
  等待期（含首块数秒延迟）屏上保留上一句。
- **句间停顿**：上一句播完到下一句开口之间至少保持 `PRESENTER_SENTENCE_GAP_SECONDS`
  （默认 1.0，0 = 关闭）的停顿，期间屏上仍是上一句全文字幕。这是**最少间隔**而非
  额外延迟：下一句合成等待本身计入间隔（合成慢于间隔就不再等），每轮首句不等，
  无缝接管的开播不等（接管以立即性优先，ADR-0008）。

## TTS 引擎与降级（ADR-0001）

`PRESENTER_TTS_ENGINE=auto|cosyvoice|audio_cpp`（默认 `auto`；**本机部署定为
`audio_cpp`**：TTS 与 voice-input 的 ASR 都由 audio.cpp@8080 提供）：

- **cosyvoice**（云）：DashScope 音色复刻，需 API key 与克隆 voice id。
- **audio_cpp**（本地）：HTTP 调外部 `audiocpp_server` 的
  `POST /v1/audio/speech`（SSE 流式 PCM），音色克隆走 `voice_ref` WAV +
  `reference_text`（留空则 `x_vector_only_mode`）；模型原生采样率（audio8_tts
  44.1 kHz / Qwen3-TTS 24 kHz）线性重采样到模块统一的 16 kHz。audiocpp_server
  是**外部服务**，须自行启动（本机探 `127.0.0.1:8080`），宿主只探 `/health`、
  绝不拉起它。
- **auto**：云为主。某句在**出声前**失败则透明地换另一引擎重试（听感无断档）；
  云连续失败 `PRESENTER_TTS_FALLBACK_AFTER`（默认 3）句后降级为本地优先；降级冷却
  `PRESENTER_TTS_RETRY_AFTER_SECONDS`（默认 60）秒后下一句先回探云端，成功即恢复。
  已出声后失败不重试（绝不重复播音），直接报错 result。status 的 `tts_engine`
  实时反映当前生效引擎。

## 舞台视觉

呈现窗口的版面**照搬并改自**旧 vtuber-frontend 仓库中人工设计的视觉（该仓库只读保留
为历史参考；美术资产 `background.svg`、`textarea.svg`、`unifont-17.0.03.otf` 直接复用）：

- **1920×1080** 基准分辨率，`canvas_items` 拉伸模式；`background.svg` 全屏背景。
- **待机位**：形象居中（960,1297，scale 0.45，模型原点补偿在子节点偏移内）；
  字幕气泡（`textarea.svg` 九宫格 + unifont 32 号）贴屏幕底部居中。
- **幻灯片版面**：宽固定 1280，高按页面 PNG 宽高比等比换算（`main.gd` 装载时设置
  控件尺寸），左上角锚定——换页只向下方伸缩，顶边不动；`TextureRect` 铺满绘制时
  rect 与纹理比例一致，非 16:9 页面不再被拉伸变形。
- **开片动画 `load_slide`**（1s）：幻灯片从屏幕左侧外滑入停在左上（85,75），
  同时形象滑向右下角并放大（→1596,1328，scale 0.5），字幕气泡左移上移。
- **收片动画 `unload_slide`**：`load_slide` 的逆过程（改进点——旧设计没有收片动画，
  hide 时版面不会复位）。

## 里程碑

- **M1（已完成，真实云合成冒烟待有网络/凭据环境执行）**：capability 握手 → 收单元 →
  CosyVoice 逐句流式合成（含预取：当前句的合成/播放等待期持续重试捡起下一句并起合成，
  与当前句播放并行，避免句间静音空窗）→ 本机出声 → Godot 窗口线性字幕 → result 回报 →
  `queue_finished` 输入帧回 hub。
- **M2（已完成）**：Live2D 形象（上游 gd_cubism v0.9.1 插件 + 树莓娘模型资产），
  `emotion`/`motion` 标签驱动表情 clip 与命名动作，口型跟随宿主播放标志
  （bridge `speech_state` 消息）；空闲呼吸/眨眼/头部微动为本地程序化动画；动作播完
  自动播放内部 `reset` 动作把参数淡回默认值（资产曲线终点非中性，且 gd_cubism 只
  持久化运动窗口内的写入，见 live2d_avatar.gd 注释）。形象置于
  手设计舞台布局的待机位（见上节"舞台视觉"）。
- **M3（已完成）**：PPT 显示与翻页。deck 预先用 `tools/prepare_deck.py` 导出为逐页 PNG
  （soffice + pdftoppm，含 manifest 校验，支持 `--first/--last` 页范围）；Agent 在单元的 `ppt` 字段携带
  `open_deck/goto/next/prev/close` 指令，随句按序执行；开片/收片播放舞台联动动画
  （见上节"舞台视觉"）；deck 状态进 status 与
  `queue_finished`，失败以异步 error result 回报且不断句。`PRESENTER_DECKS_DIR` 指定根目录。
- **M4（已完成，真实合成冒烟待模型下载后在有服务的环境执行）**：audio.cpp 本地兜底
  TTS + 自动降级（ADR-0001），详见上文。
- **M5（已完成，真实讲解冒烟待有服务的环境执行）**：逐页文本进 deck（ADR-0006/0007）。
  manifest 1.2.0 为每页携带**页面内容**（pdftotext 抽取，可覆盖）与**建议讲稿**
  （`--scripts` 撰写），两者随 `queue_finished` 与 status 送达无视觉的核心 Agent，
  让它能"看着页讲"，且人工定制讲稿与内容描述互不干扰。
- **M6（已完成）**：以播放为中心重写合成/播放/字幕管线（ADR-0008）：统一**播放队列**
  + 无深度上限的后台串行**预合成**（取代 M1 的 lookahead=1 预取）、一切表现事件与
  **开播**同步、stop 改为不掐正在播放队头的**无缝接管**。实现在 `presenter/playqueue.py`
  （任务表 + 合成 worker + 播放 worker，50ms tick 轮询协调），`app.py` 只剩 hub 胶水。
  实现取代 ADR-0003 的旧打断/轮次语义（ADR-0003 其余内容仍有效）。

## 测试

- 离线单测（wire、schema/单元、字幕数学、播放队列语义（stop/无缝接管/开播同步）、
  hub 客户端对 tests/ 内的 test-only 假 hub 服务端）：
  `uv run --locked pytest -q -m "not real"`
- 真实冒烟（开发机：有 DashScope 凭据、有音频输出、可选 Godot 窗口与已启动的
  audiocpp_server）：`uv run --locked pytest -q -m real`。全链路（waycast 核心 +
  真实模型 + presenter）的验收步骤见 `tests/test_smoke_real.py` 模块文档。
- 一律真实实现验证；假引擎/无声播放器等占位只存在于 `tests/`。
