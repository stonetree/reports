# you asked

message time: 2026-08-13 14:14:19

ok，到目前为止讨论得比较清晰了。现在做一次信息搜集和总结。

广泛搜集AMD、NVIDIA、INTEL、ARM等厂商，围绕kvcache在单机和集群规模下的传输、存储、管理，都有哪些软件方面的动作，包括自研软件、投资、收购、大力扶持开源软件等。

---

# gemini response

大模型推理从“算力受限”转向“内存/网络带宽受限”，围绕 KV Cache 在单机与集群维度的存储、传输与管理，成为各大芯片巨头软硬件协同（Hardware-Software Co-design）的核心战场。

---

| 厂商 | 单机内存/算子层优化 | 跨节点/集群传输与编排 | 存储分层与卸载 (Offload) | 生态与收并购/扶持动作 |
| :--- | :--- | :--- | :--- | :--- |
| **NVIDIA** | TensorRT-LLM（PagedAttention、FP8/FP4 KV 动态量化） | KVBM (KV Block Manager)、NIM 容器编排、NCCL / NVLink 跨节点共享 | NVSwitch C2C Pooling、GPUDirect Storage (GDS) 卸载至 NVMe | 主导 TensorRT-LLM；赞助/共建 FlashAttention 与 FlashInfer 社区 |
| **AMD** | ROCm 优化算子（PagedAttention/RadixAttention）、Nod.ai 自动图编译 | NIXL (Inference Transfer Engine) 传输协议、Silo AI 工程交付 | ROCm AIC (Infinity Context)、`hipFile` 直连 DMA、VAST Data 联合存储 | 收购 Nod.ai、Silo AI、ZT Systems；主导/重度贡献 **LMCache** 与 **vLLM** |
| **Intel** | IPEX-LLM、OpenVINO KV 压缩、Gaudi 专属 PagedAttention | DeepSpeed-FastGen / MII 适配、oneCCL 集群传输 | 借助大容量 Xeon DDR5 / CXL 内存池做 GPU/Gaudi KV Cache 溢出层 | 主导 OpenVINO/IPEX；深度参与 vLLM-Gaudi 分支与 DeepSpeed 社区 |
| **ARM** | KleidiAI 汇编库、SVE2/SME 指令集 KV 向量加速 | Neoverse CXL 3.0 机架级共享内存池 | 边缘/端侧（DRAM/NPU）与服务器端 CPU 内存做 KV 高密度压缩 | 深度贡献 llama.cpp、ExecuTorch 及 vLLM 的 ARM 架构原生后端 |

---

### 一、 各厂商技术动作深度拆解

**1. NVIDIA：垂直闭环与性能吞吐极致化**
* **单机与算子优化**：在 TensorRT-LLM 中率先支持 FlashAttention-3、FP8/FP4 精度 KV Cache，引入 Chunked Prefill 减少 KV Cache 物理碎片的预留浪费。
* **集群与传输管理**：推出 KVBM（KV Block Manager）与集群调度框架，利用 NVLink-Network 及 NCCL 实现 Prefill 与 Decode 节点的超低延迟 KV Block 零拷贝迁移。
* **企业级分发**：将全套优化封装至 **NIM 微服务**，面向企业客户提供高毛利、开箱即用的闭环部署体验。

**2. AMD：开源标准、分层存储与全栈工程落地**
* **底层 I/O 与传输**：开发 ROCm `hipFile` 驱动 API 实现 Host-Bypass DMA 传输；联合社区推出开源 **NIXL** 传输引擎，规范跨节点 KV 迁移标准。
* **分层管理与开源扶持**：深度投入开源项目 **LMCache**，构建从 HBM $\rightarrow$ CPU DRAM $\rightarrow$ CXL/NVMe 的多级 KV Cache 缓存与前缀匹配（Prefix Matching）体系；持续调优 vLLM 与 SGLang 的 ROCm 后端。
* **战略收并购**：收购 Nod.ai（IREE/MLIR 编译器自动化优化 KV 布局）、Silo AI（提供企业级 KV 架构落地与定制）和 ZT Systems（机架级硬件拓扑与分层传输网络设计）。

**3. Intel：异构内存协同与 CPU/Gaudi 混合卸载**
* **CPU 异构内存利用**：充分发挥至强（Xeon）处理器的大容量 DDR5 / HBM / CXL 内存优势，将 CPU 内存直接作为 Gaudi AI 加速卡或 PCIe GPU 的低成本 KV Cache 溢出池（Overflow Tier）。
* **软件栈建设**：通过 **IPEX-LLM** 和 **OpenVINO** 实现端侧与服务器 CPU 上的高比例 KV Cache 动态量化（如 INT4/INT8），并在开源框架 vLLM 和 DeepSpeed-FastGen 中维护 Gaudi 专属的分布式 Prefill/Decode 分离机制。

**4. ARM：端侧量化加速与 CXL 机架池化**
* **指令集与算子**：发布 **KleidiAI** 微架构加速库，专门针对 Neoverse（服务器端）和 Cortex-X/A（端侧）CPU 的 SVE2/SME 指令集优化 KV Cache 的转置、量化与 GEMM 计算。
* **开源与生态支持**：在 llama.cpp、ExecuTorch 和 vLLM 中主导 ARM 64 位 CPU 端的原生 KV Cache 管理机制；在云端配合 CXL 3.0 规范，推动基于 ARM CPU 的机架级共享 KV 内存池架构。

---

### 二、 行业公共演进趋势

1. **分离式架构（Disaggregated Serving）成为事实标准**：四家厂商无一例外均在推动 Prefill（计算密集型）与 Decode（访存密集型）节点分离，KV Cache 跨节点传输效率成为决定集群吞吐量的第一指标。
2. **硬件 Offload 替代 CPU 介入**：通过 RDMA、GDS、`hipFile` 等技术，KV Cache 的跨节点与跨介质迁移全面走向 Host-Bypass（绕过 CPU 内存与上下文切换）。
3. **软件路线的生态分化**：NVIDIA 凭借性能优势继续强化闭环商业软件（NIM），而 AMD、Intel、ARM 则高度依赖并联合扶持公共开源社区（vLLM、LMCache、SGLang），试图通过统一的开源标准打破硬件锁死。

---
Powered by [AI Exporter](https://saveai.net)