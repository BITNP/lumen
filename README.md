# raspberrygirl-presenter

树莓娘（Raspberry Girl）的**聚合输出模块**（waycast ADR-0010）：一个 waycast 模块
（capability `kind="both"`），整合 TTS 语音合成与播放、Godot 图像前端（字幕、Live2D
形象、表情、动作）与 PPT 显示翻页。核心 Agent 派发表现输出单元过来，模块按语义执行、
立刻回 output result，并把"队列播完"事件（唯一输入帧，ADR-0005）送回 hub。

术语见 [GLOSSARY.md](GLOSSARY.md)；决策见 [docs/adr/](docs/adr/)。核心语义与 wire
契约以 [waycast](../waycast/)（[GLOSSARY](../waycast/GLOSSARY.md)、
[ADR-0008](../waycast/docs/adr/0008-module-network-protocol.md)、
[ADR-0010](../waycast/docs/adr/0010-aggregate-output-module.md)、
[ADR-0011](../waycast/docs/adr/0011-agent-decides-turn-taking.md)）为唯一权威。

## 形态（ADR-0002）

- **宿主进程**（本仓库 Python 部分）：waycast 模块身份。连 hub `/ws/modules`（首帧
  capability advertisement），收 `output_unit` 帧入**单元队列**顺序执行（合成→播放→
  字幕），逐单元回 `result` 帧；应答 `status_query`；发 `input` 帧（唯一一种
  `queue_finished`，ADR-0005）。断线按指数退避重连。
- **呈现进程**（`godot/`，Godot 4.6 + gd_cubism）：纯呈现、纯展示面（无任何用户
  可操作元素）。经本机 presentation bridge（`127.0.0.1:PRESENTER_BRIDGE_PORT`
  上的 WebSocket）接收字幕揭示、表情/动作、PPT 指令，上行只有连接握手。
  bridge 协议是模块内部实现，与 waycast 无关。

## 输出单元 schema（M1）

一次 Invocation 派发的是**单元列表，列表顺序=表演顺序，按句一单元**（ADR-0003）。
所有字段可选、至少一个：

| 字段 | 语义 |
| --- | --- |
| `speech` | 要朗读的一句文本（TTS + 播放 + 字幕文本来源） |
| `subtitle` | 字幕文本，缺省 = `speech` |
| `emotion` | 表情标签，驱动 Live2D 表情 clip（nod/shake_head/wink） |
| `motion` | 动作标签，驱动命名动作（wave/point/sweat/no） |
| `ppt` | 翻页指令 `{action: open_deck|goto|next|prev|close, deck, page}`，在该句开口前执行 |
| `control` | 目前仅 `"stop"`：清空单元队列并立即停止当前播放（ADR-0003） |

hub 在派发前按本模块声明的 schema 校验单元。

## 运行

```sh
uv sync --locked
cp .env.example .env   # 填入 hub 地址/token 与 DashScope 凭据
uv run --locked presenter
# 呈现窗口（未配 PRESENTER_GODOT_PATH 时手动启动）：
godot --path godot/
```

## 里程碑

- **M1（已完成，真实云合成冒烟待有网络/凭据环境执行）**：capability 握手 → 收单元 →
  CosyVoice 逐句流式合成（含预取）→ 本机出声 → Godot 窗口线性字幕 → result 回报 →
  `queue_finished` 输入帧回 hub。
- **M2（已完成）**：Live2D 形象（上游 gd_cubism v0.9.1 插件 + 树莓娘模型资产），
  `emotion`/`motion` 标签驱动表情 clip 与命名动作，口型跟随宿主播放标志
  （bridge `speech_state` 消息）；空闲呼吸/眨眼/头部微动为本地程序化动画。形象置于
  手设计舞台布局的待机位（见下节"舞台视觉"）。
- **M3（已完成）**：PPT 显示与翻页。deck 预先用 `tools/prepare_deck.py` 导出为逐页 PNG
  （soffice + pdftoppm，含 manifest 校验）；Agent 在单元的 `ppt` 字段携带
  `open_deck/goto/next/prev/close` 指令，随句按序执行；开片/收片播放舞台联动动画
  （见下节"舞台视觉"）；deck 状态进 status 与
  `queue_finished`，失败以异步 error result 回报且不断句。`PRESENTER_DECKS_DIR` 指定根目录。
- **M4（已完成，真实合成冒烟待模型下载后在有服务的环境执行）**：audio.cpp 本地兜底
  TTS + 自动降级（ADR-0001），详见下节。

## 舞台视觉

呈现窗口的版面**照搬并改自**旧 vtuber-frontend 仓库中人工设计的视觉（该仓库只读保留
为历史参考；美术资产 `background.svg`、`textarea.svg`、`unifont-17.0.03.otf` 直接复用）：

- **1920×1080** 基准分辨率，`canvas_items` 拉伸模式；`background.svg` 全屏背景。
- **待机位**：形象居中（960,1297，scale 0.45，模型原点补偿在子节点偏移内）；
  字幕气泡（`textarea.svg` 九宫格 + unifont 32 号）贴屏幕底部居中。
- **开片动画 `load_slide`**（1s）：1280×720 幻灯片从屏幕左侧外滑入停在左上
  （85,75），同时形象滑向右下角并放大（→1596,1328，scale 0.5），字幕气泡左移上移。
- **收片动画 `unload_slide`**：`load_slide` 的逆过程（改进点——旧设计没有收片动画，
  hide 时版面不会复位）。

## TTS 引擎与降级（ADR-0001）

`PRESENTER_TTS_ENGINE=auto|cosyvoice|audio_cpp`（默认 `auto`）：

- **cosyvoice**（云主用）：DashScope 音色复刻，需 API key 与克隆 voice id。
- **audio_cpp**（本地兜底）：HTTP 调外部 `audiocpp_server` 的
  `POST /v1/audio/speech`（SSE 流式 PCM），音色克隆走 `voice_ref` WAV +
  `reference_text`（留空则 `x_vector_only_mode`）；模型原生 24 kHz 线性重采样到
  模块统一的 16 kHz。audiocpp_server 是**外部服务**，须自行启动（默认探
  `127.0.0.1:8081`，8080 被本机嵌入服务占用），宿主只探 `/health`、绝不拉起它。
- **auto**：云为主。某句在**出声前**失败则透明地换另一引擎重试（听感无断档）；
  云连续失败 `PRESENTER_TTS_FALLBACK_AFTER`（默认 3）句后降级为本地优先；降级冷却
  `PRESENTER_TTS_RETRY_AFTER_SECONDS`（默认 60）秒后下一句先回探云端，成功即恢复。
  已出声后失败不重试（绝不重复播音），直接报错 result。status 的 `tts_engine`
  实时反映当前生效引擎。

## deck 准备

```sh
uv run --locked python tools/prepare_deck.py 讲义.pptx --name lecture1 --out decks
```

生成 `decks/lecture1/001.png…` 与 `manifest.json`，Agent 即可
`{"ppt": {"action": "open_deck", "deck": "lecture1"}}` 打开。前端是纯展示面：
翻页只由 Agent 的 `ppt` 指令驱动，窗口不接收任何点击操作（ADR-0004/0005）。

## 测试

- 离线单测（wire、schema/单元、字幕数学、队列语义、hub 客户端对 tests/ 内的
  test-only 假 hub 服务端）：
  `uv run --locked pytest -q -m "not real"`
- 真实冒烟（开发机：有 DashScope 凭据、有音频输出、可选 Godot 窗口与已启动的
  audiocpp_server）：`uv run --locked pytest -q -m real`。全链路（waycast 核心 +
  真实模型 + presenter）的验收步骤见 `tests/test_smoke_real.py` 模块文档。
- 一律真实实现验证；假引擎/无声播放器等占位只存在于 `tests/`。
