# TTS 双引擎：云 CosyVoice 主用、audio.cpp 本地兜底、自动降级

聚合输出模块必须持续出声，而唯一已验证的合成路径（DashScope CosyVoice）依赖网络与
外部服务。我们决定：主引擎用 DashScope CosyVoice（`cosyvoice-v3-flash` + 音色复刻，
流式 PCM 16 kHz 单声道，行为已由旧核心真实运行验证）；兜底引擎用本机已构建的
audio.cpp（裸 HTTP `POST /audio/speech`，SSE 流式，外部服务假设，宿主只探活不负责
拉起）；运行时自动降级——云引擎连续失败即切本地、冷却探活后回云，并可用
`PRESENTER_TTS_ENGINE=auto|cosyvoice|audio_cpp` 手动强制。当前生效引擎随 status 上报。

被否决的选项：qwen3ttscpp（本机不存在引擎与模型，且不支持参考音频克隆，无从验证）；
vllm_omni（Qwen3-TTS 0.6B，克隆能力最强且有现成脚本，但 vLLM 全栈 + GPU 常驻，作为
"兜底"过重，保留为 `TtsEngine` 接口下的第二备选）。qwen3ttscpp 的排除意味着音色克隆
参考素材（工作区 `ref_audio.wav`/`ref_text.txt`）是跨引擎的音色单一事实源。

## Consequences

- 引擎一律经 `TtsEngine` 接口接入；兜底是接口上的第二个实现（M4），不阻塞表现主线。
- 兜底引擎的克隆音色与云音色的一致性只验证并记录事实，不承诺相同。
- 云引擎 API key 与本地端口等只进本地 `.env`（audio.cpp 默认 8080 端口已被本机嵌入
  服务占用，须换端口）。
