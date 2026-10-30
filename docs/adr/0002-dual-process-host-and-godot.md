# 双进程形态：Python 宿主是模块身份，Godot 是呈现进程

waycast ADR-0010 把 TTS、播放与 Godot 前端合并为"一个"聚合输出模块，但落到进程层面
必须选形态。我们决定采用双进程：**Python 宿主进程**持有模块身份——waycast 连接
（capability `kind="both"`）、TTS 引擎、播放时钟、单元队列与全部协议帧的收发；
**Godot 呈现进程**只做呈现（字幕揭示、Live2D、PPT），经本机 presentation bridge 接收
指令、回传用户操作。理由：TTS SDK（dashscope）与播放（sounddevice/PortAudio）都在
Python 生态且已被旧核心真实验证；协议与队列逻辑可以无头单测 + 真实模型冒烟，不依赖
GUI；Godot 保持薄呈现层，崩溃可被宿主拉起降级运行。

被否决的选项：单进程 Godot（GDScript 直连 hub、裸写流式 TTS 协议、测试须进 Godot
运行时——每一样都比双进程贵）。

## Consequences

- "聚合输出模块"一词指宿主+呈现进程的整体；对 hub 暴露的 capability.name 由宿主
  声明（`presenter`）。呈现进程不是 waycast 模块，bridge 协议不受 wire 契约约束。
- 表现控制、字幕同步、播放进度全部封装在宿主进程内（ADR-0010），bridge 只是其内部
  总线。
