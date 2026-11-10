# raspberrygirl-presenter

树莓娘（Raspberry Girl）的**聚合输出模块**（waycast ADR-0010）：一个 waycast 模块
（capability `kind="both"`），整合 TTS 语音合成与播放、Godot 图像前端（字幕、Live2D
形象、表情、动作）与 PPT 显示翻页。核心 Agent 派发表现输出单元过来，模块按语义执行、
立刻回 output result，并把"队列播完"事件（唯一输入帧，ADR-0005）送回 hub。

本文是**用户文档**：如何配置并运行 presenter、输出单元契约、deck 准备。内部形态
（宿主/呈现双进程、bridge、降级策略、舞台视觉、里程碑、测试）见**开发者文档**
[DEVELOPMENT.md](DEVELOPMENT.md)；术语见 [GLOSSARY.md](GLOSSARY.md)，决策见
[docs/adr/](docs/adr/)。核心语义与 wire 契约以 [waycast](../waycast/)
（[GLOSSARY](../waycast/GLOSSARY.md)、
[ADR-0008](../waycast/docs/adr/0008-module-network-protocol.md)、
[ADR-0010](../waycast/docs/adr/0010-aggregate-output-module.md)、
[ADR-0011](../waycast/docs/adr/0011-agent-decides-turn-taking.md)）为唯一权威。

## 输出单元 schema（M1）

一次 Invocation 派发的是**单元列表，列表顺序=表演顺序，按句一单元**（ADR-0003）。
所有字段可选、至少一个：

| 字段 | 语义 |
| --- | --- |
| `speech` | 要朗读的一句文本（TTS + 播放 + 字幕文本来源） |
| `subtitle` | 字幕文本，缺省 = `speech` |
| `emotion` | 表情标签，驱动 Live2D 表情 clip（nod/shake_head/wink），在该句开播瞬间执行 |
| `motion` | 动作标签，驱动命名动作（wave/point/sweat/no），在该句开播瞬间执行 |
| `ppt` | 翻页指令 `{action: open_deck|goto|next|prev|close, deck, page}`，在该句开播瞬间执行 |
| `control` | 目前仅 `"stop"`（ADR-0008）：取消在途预合成并丢弃所有尚未开播的任务；正在播放的队头不被掐，stop 后的新句在其音频就绪的刹那无缝接管开播。打断必须首发 stop 单元 |

hub 在派发前按本模块声明的 schema 校验单元。单元进入有界的播放队列
（`PRESENTER_QUEUE_MAX`，默认 64）：队满时该单元被回 error result，队列本身
按到达顺序完整执行。

## 运行

```sh
uv sync --locked
cp .env.example .env   # 填入 hub 地址/token 与 TTS 凭据
uv run --locked presenter
# 呈现窗口（未配 PRESENTER_GODOT_PATH 时手动启动）：
godot --path godot/
```

配置全部走 `.env`（各键与默认值见 [.env.example](.env.example)），要点：

| 键 | 说明 |
| --- | --- |
| `WAYCAST_HUB_URL` / `WAYCAST_HUB_TOKEN` | hub 地址与连接令牌（必填 URL） |
| `PRESENTER_TTS_ENGINE` | `auto`（默认）\| `cosyvoice`（云）\| `audio_cpp`（本地） |
| `PRESENTER_DASHSCOPE_API_KEY` 等 | CosyVoice 云合成凭据与 voice id |
| `PRESENTER_AUDIO_CPP_URL` 等 | 本地 audio.cpp 服务地址、模型与音色参考（外部服务，须自行启动 `audiocpp_server`） |
| `PRESENTER_SENTENCE_GAP_SECONDS` | 句间最少停顿（默认 1.0，0 = 关闭） |
| `PRESENTER_QUEUE_MAX` | 播放队列容量（默认 64，队满的单元回 error result） |
| `PRESENTER_GODOT_PATH` | 配置后由宿主负责启动/重启 Godot 呈现窗口；留空须手动启动 |
| `PRESENTER_DECKS_DIR` | deck 根目录（留空则 `ppt` 指令报错不生效） |

TTS 引擎取值：`cosyvoice` 走 DashScope 云合成；`audio_cpp` 走本机 audio.cpp
服务（**本机部署定为 `audio_cpp`**：TTS 与 voice-input 的 ASR 都由
audio.cpp@8080 提供，须自行启动，宿主只探 `/health`、绝不拉起它）；`auto`
云为主、出声前失败透明换引擎、连续失败自动降级本地
（策略细节见 [DEVELOPMENT.md](DEVELOPMENT.md#tts-引擎与降级adr-0001)）。

排查时设 `PRESENTER_LOG_LEVEL=DEBUG` 可看单元接收、TTS 请求/合成耗时、逐句播放
完成与轮次结束；websockets/httpx 等依赖日志默认钉在 WARNING，需要时用
`PRESENTER_DEPS_LOG_LEVEL` 单独放开。

## deck 准备

```sh
uv run --locked python tools/prepare_deck.py 讲义.pptx --name lecture1 --out decks
# 只导出部分页，并用人工/视觉模型撰写的逐页文本覆盖自动抽取结果：
uv run --locked python tools/prepare_deck.py ../main.pdf --name main-intro --out decks \
    --last 20 --scripts tools/deck-scripts/main-intro.json --force
```

生成 `decks/lecture1/001.png…` 与 `manifest.json`，Agent 即可
`{"ppt": {"action": "open_deck", "deck": "lecture1"}}` 打开。前端是纯展示面：
翻页只由 Agent 的 `ppt` 指令驱动，窗口不接收任何点击操作（ADR-0004/0005）。

核心 Agent 的模型**没有视觉**，看不到页面内容，所以 manifest（schema
1.2.0）为每页携带两段文本（ADR-0006/0007），随 `queue_finished` 的 input
内容与 status 的 `page_summary`/`page_script` 送达 Agent：

- **页面内容（summaries）**：忠实描述这页展示什么（标题、条目、图表、当前
  overlay 状态）——默认用 pdftotext 抽取 PDF 文本层生成，扫描件或噪声大的
  deck 用视觉模型/人工重写。
- **建议讲稿（scripts）**：建议怎么讲这页（开场、语气、停顿、展开方式），只由
  人工或视觉模型撰写，供自由定制与注入特定指令，改讲法不会碰着内容事实。

`--scripts <json>` 的每页值是字符串（只给讲稿）或
`{"summary": "…", "script": "…"}` 对象（两段都给）。示例见
[`tools/deck-scripts/main-intro.json`](tools/deck-scripts/main-intro.json)
（main.pdf 前 20 页测试 deck，人工对照渲染页逐页撰写，覆盖 Beamer overlay
渐进页与页脚噪声）。
