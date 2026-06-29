# 三项前沿技术调研对比报告

> 调研时间：2026-06-29

---

## 对比总览表

| 维度 | WebAssembly vs WebGPU (浏览器端 AI 推理) | RISC-V (边缘设备现状) | WebTransport (协议成熟度) |
|------|-------------------------------------------|------------------------|---------------------------|
| **核心定位** | WebGPU 专为 GPGPU 高性能计算；WebAssembly 用于 CPU 通用逻辑 | 开放、可定制的 AI-native ISA，适合边缘 AI 计算 | 基于 HTTP/3 的现代网络传输协议，替代 WebSocket |
| **关键技术特征** | GPU 硬件加速（Metal/Vulkan/DirectX）；支持 WGSL shader 语言；计算管线原生支持 | 向量化扩展 + 自定义指令；单 ISA 覆盖 MCU → 应用处理器；无 ISA 授权费用 | 支持可靠/不可靠传输；多流、单向流、乱序交付；基于 QUIC/HTTP/3 |
| **成熟度/生态** | WebGPU：Chrome/Firefox/Edge 支持，Safari 有限；WebAssembly：主流浏览器全面支持 | 生态快速增长，多供应商（SiFive、Western Digital 等）；Ubuntu/Android 支持；年度峰会活跃 | 2026-03 成为 Baseline 特性；主流浏览器支持；W3C 规范仍在演进 |
| **典型应用场景** | WebLLM：浏览器内 LLM 推理；WebGPU 推理引擎、3D 渲染、机器学习计算 | 智能穿戴、无人机、IoT 传感器；边缘 AI 推理；工业机器人 | 实时游戏、视频流、多玩家同步；低延迟数据传输 |

---

## 话题①：WebAssembly 与 WebGPU 在浏览器端 AI 推理上的差异

### 要点一：WebGPU 专为 GPGPU 设计，支持现代 GPU API
- **内容**：WebGPU 是 WebGL 的继任者，提供与现代 GPU（Metal/Vulkan/Direct3D 12）的直接兼容性，支持通用 GPU 计算（GPGPU），不再仅限于图形渲染。
- **出处**：MDN WebGPU API 文档
  - URL: https://developer.mozilla.org/en-US/docs/Web/API/WebGPU_API
  - 引文："WebGPU is the successor to WebGL, providing better compatibility with modern GPUs, support for general-purpose GPU computations, faster operations, and access to more advanced GPU features."

### 要点二：WebLLM 展示 WebGPU 在浏览器端 AI 推理的实际应用
- **内容**：WebLLM 是一个高性能浏览器端 LLM 推理引擎，完全在浏览器内运行，使用 WebGPU 进行硬件加速，无需服务器支持，支持 Llama 3、Phi 3、Gemma、Mistral 等主流模型。
- **出处**：GitHub - mlc-ai/web-llm
  - URL: https://github.com/mlc-ai/web-llm
  - 引文："WebLLM is a high-performance in-browser LLM inference engine that brings language model inference directly onto web browsers with hardware acceleration. Everything runs inside the browser with no server support and is accelerated with WebGPU."

### 要点三：WebAssembly 用于 CPU 计算，适合通用逻辑处理
- **内容**：在 WebLLM 架构中，结构化 JSON 生成等 CPU 密集型任务在 WebAssembly 层实现，展示两者互补关系：WebGPU 处理 GPU 计算，WebAssembly 处理 CPU 端逻辑。
- **出处**：WebLLM GitHub README
  - URL: https://github.com/mlc-ai/web-llm
  - 引文："Structured JSON Generation: WebLLM supports state-of-the-art JSON mode structured generation, implemented in the WebAssembly portion of the model library for optimal performance."

---

## 话题②：RISC-V 在边缘设备上的现状

### 要点一：RISC-V 是 AI-native ISA，适合边缘 AI 计算
- **内容**：RISC-V ISA 内置向量化支持，允许自定义指令和领域特定加速，从边缘推理到数据中心 Transformer 工作负载均可覆盖，特别适合 IoT/嵌入式场景的 AI 计算需求。
- **出处**：RISC-V International IoT/Embedded 页面
  - URL: https://riscv.org/iot-embedded/
  - 引文："AI Native: Built for vectorization, the RISC-V ISA provides custom instructions and domain-specific acceleration to support IoT AI compute from edge inference to data center transformer workloads."

### 要点二：无 ISA 费用，适合高体积长寿命边缘设备
- **内容**：RISC-V 作为开放标准 ISA，无需 ISA 授权费用，对高出货量、长生命周期的 IoT/嵌入式设备具有明显成本优势。
- **出处**：RISC-V International IoT/Embedded 页面
  - URL: https://riscv.org/iot-embedded/
  - 引文："No Fee ISA: Incurs no ISA-specific fees, so it can be cost-effective for high-volume and long lifespan processors often deployed in the IoT/Embedded segment."

### 要点三：生态快速增长，多供应商选择避免锁定
- **内容**：RISC-V 打破传统由少数供应商垄断的市场格局，提供更多选择，避免供应商锁定，支持技术主权和供应链韧性。2026 年 RISC-V Summit Europe 已成功举办。
- **出处**：RISC-V International 官网
  - URL: https://riscv.org/
  - 引文："Vendor Choice: Opens up a marketplace traditionally served by a small number of providers, enhancing user choice and preventing vendor lock-in."

---

## 话题③：WebTransport 协议的成熟度

### 要点一：2026年3月成为 Baseline 特性，跨浏览器支持
- **内容**：WebTransport 于 2026年3月 被标记为 Baseline 特性，表示在最新浏览器版本中稳定可用，部分功能可能在不同浏览器中有不同程度的支持。
- **出处**：MDN WebTransport 文档
  - URL: https://developer.mozilla.org/en-US/docs/Web/API/WebTransport
  - 引文："Since March 2026, this feature works across the latest devices and browser versions. This feature might not work in older devices or browsers."

### 要点二：基于 HTTP/3，支持可靠和不可靠传输
- **内容**：WebTransport 基于 HTTP/3（QUIC），提供双向、单向流以及数据报（datagram）传输，支持可靠和不可靠传输模式，可用于实时游戏、视频流等低延迟场景。
- **出处**：MDN WebTransport 文档
  - URL: https://developer.mozilla.org/en-US/docs/Web/API/WebTransport
  - 引文："The WebTransport interface... enables a user agent to connect to an HTTP/3 server, initiate reliable and unreliable transport in either or both directions."

### 要点三：W3C 规范仍在演进，协议可能变化
- **内容**：WebTransport 的 W3C 规范仍在进行中（work-in-progress），底层协议（HTTP/3 和 HTTP/2）规范也在演进，API 和协议在未来可能发生显著变化，生产部署需关注兼容性风险。
- **出处**：W3C WebTransport 规范
  - URL: https://w3c.github.io/webtransport/
  - 引文："Note: The API presented in this specification represents a preliminary proposal based on work-in-progress within the IETF WEBTRANS WG. Since the specifications are a work-in-progress, both the protocol and API are likely to change significantly going forward."

---

## 参考来源汇总

| 话题 | 主要来源 |
|------|----------|
| WebAssembly vs WebGPU | 1. MDN WebGPU API: https://developer.mozilla.org/en-US/docs/Web/API/WebGPU_API<br>2. WebLLM GitHub: https://github.com/mlc-ai/web-llm |
| RISC-V 边缘设备 | 1. RISC-V IoT/Embedded 页面: https://riscv.org/iot-embedded/<br>2. RISC-V 官网: https://riscv.org/ |
| WebTransport 协议 | 1. MDN WebTransport: https://developer.mozilla.org/en-US/docs/Web/API/WebTransport<br>2. W3C 规范: https://w3c.github.io/webtransport/ |

---

## 总结

三项技术分别代表了**浏览器端 AI 推理**、**边缘设备架构**和**网络传输协议**三个前沿领域的最新进展：

1. **WebGPU** 正在重塑浏览器端高性能计算能力，使 LLM 等大模型可在浏览器内直接推理
2. **RISC-V** 作为开放 ISA，正在边缘 AI 和 IoT 领域快速扩展生态
3. **WebTransport** 已进入跨浏览器稳定阶段，但仍需关注规范演进带来的兼容性风险