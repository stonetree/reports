# SGLang / vLLM 双框架 + Mooncake 计算通信掩盖与基于 CXL 内存语义的稀疏 KVC 优化方案及验证指导书

> **文档版本**：V2.1（研发实施与方案设计定稿版）  
> **任务代号**：`M2-F01`（KVC 前沿航道一：按层计算-通信全掩盖双缓冲与基于注意力重要性的 CXL 稀疏抽稀拉取引擎）  
> **主责 Owner**：按层计算通信掩盖与稀疏 KVC 研发主责工程师（$1.0\text{ FTE}$）  
> **核心协同矩阵**：WZX（`M2-T03` 上层 Slab 分配与 SGL/UMR 边界描述符）、WXH（`M2-T05` $<5\mu\text{s}$ 选路与 $K^*$ 切分决策）、NXY（`M2-T04` 硬件物理能力矩阵与 MPAM QoS）、YY（`M2-F02` 鲲鹏 NEON 位平面流式压缩）、CY/YZN（`M2-T01/T02` 多通道亲和底座与 URMA/RDMA 传输）  
> **适用框架**：SGLang（`v0.5.x+` `HiCache` 架构） / vLLM（`V1` `KVConnector` 架构） + Mooncake Transfer Engine  
> **前置依赖**：面向国产 AI 硬件的统一异构 KVCache 存储与调度底座 / 原厂软硬件协同系统传输底座

---

## 零、 系统工程师极速导读与计算机体系结构概念映射表

> [!NOTE] **主责开发工程师技术导读**  
> 本优化方案涉及大模型推理系统（SGLang / vLLM）与分布式存储/总线（Mooncake / CXL）的深度协同。立足于系统级、Linux C++ 与底层硬件性能优化的视角，**大模型推理在系统物理本质上就是一个由多级流水线驱动的、显存带宽与跨互联 I/O 受限的高性能流式处理系统**。  
> 下表将方案中涉及的模型层术语，系统映射为计算机体系结构、操作系统与分布式网络经典概念：

### 0.1 核心概念系统学映射基准表

| AI 领域专有名词 | 通俗功能释义（首次出现规范释义） | 计算机体系结构 / 操作系统 / 网络经典概念映射 | 本方案中的物理角色与工程直觉 |
| :--- | :--- | :--- | :--- |
| **Token（词元）** | 文本被切分后的离散单位（约 $0.75$ 个英文单词或 $0.5\sim 1$ 个汉字），在模型底层表示为一个 16 位的整数 ID。 | 离散数据块、数据包净荷单元、16-bit 整数索引 | 系统的基本吞吐计量单位（Tokens/s）。 |
| **KVCache** | **大模型注意力键值缓存**（大模型自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免后续 Token 生成时重复计算注意力）。 | **LRU/Radix 缓存数据块、中间计算状态持久化页** | 本方案搬运的主体数据。单请求在长文本下可达数 GB 至数十 GB，是显存耗尽与网络传输的罪魁祸首。 |
| **Prefill（输入预处理 / 首字计算）** | 大模型处理用户 Prompt（提示词）的初始阶段，一次性并行计算所有输入 Token 的注意力并生成首个输出 Token。 | **批量批处理（Batch Processing）、高算力吞吐流水阶段** | 决定了用户等第一个字的时延（TTFT）。本方案第一挡在此阶段实施按层掩盖。 |
| **Decode（自回归逐字生成）** | 首字产生后，模型逐个生成后续 Token 的阶段。每步仅输入 1 个 Token，但需读取之前所有的历史 KVCache。 | **事件驱动单包流式处理（Event-Driven Streaming）、显存带宽受限阶段** | 决定了文字吐出的流畅度（TPOT）。本方案在此阶段实施异步写回与动态稀疏加速。 |
| **TTFT** | **Time To First Token（首字生成延迟 / 首 Token 响应时间）**，用户发送请求到屏幕吐出第一个字的时间。 | **系统服务冷启动耗时、第一包响应时间（First-Packet Latency）** | **本方案第一核心考核指标**（要求独立降低 $20\%\sim 30\%$，稀疏后降 $>40\%$）。 |
| **TPOT** | **Time Per Output Token（每个输出 Token 的生成耗时 / 单字生成延迟）**，连续生成过程中每吐出一个字的耗时。 | **稳态吞吐时延、数据流帧间隔（Inter-Frame Latency）** | **本方案第二核心考核指标**（要求后台数据搬运对其干扰率 $<3\%$）。 |
| **Saved-Prefill** | **首字生成预计算节省**（利用已缓存的 KV Cache 避免重复计算 Prompt，从而显著降低首 Token 延迟 TTFT）。 | **缓存命中读（Cache Hit Read-Through）** | 请求命中 Mooncake/CXL 历史缓存时，跳过 GPU 本地重算，直接从外部拉回显存。 |
| **Transformer 层（Layer 0..79）** | 大语言模型由若干相同结构的“层”纵向串联而成（如 70B 模型有 80 层）。数据必须按 Layer 0 $\to$ Layer 1 $\dots \to$ Layer 79 依次流转。 | **80 级严格串行执行的硬件流水线（80-Stage Hardware Pipeline）** | **按层掩盖的物理立足点**：GPU 算第 $l$ 层时只需要第 $l$ 层的 KVCache，为并发拉取第 $l+1$ 层提供了天然的时间窗口。 |
| **PagedAttention / Block Table** | **类似操作系统的虚拟内存分页技术**，将变长 KVCache 划分为固定大小物理块（如 16 Tokens/Block），通过页表管理映射。 | **OS 虚拟内存分页系统（Virtual Memory Page Table & TLB）** | GPU HBM 中的物理块不连续，通过类似页表的 `block_table` 进行逻辑寻址。 |
| **Attention 稀疏性 (Sparsity)** | 在几万个历史 Token 中，模型注意力仅高度聚焦在 $15\%\sim 30\%$ 的关键 Token 上，其余 Token 权重几乎为 0。 | **工作集（Working Set）精简、冷热数据分离、动态下采样** | **第二挡优化的物理立足点**：无需搬运 $100\%$ 的数据，只拉取高价值 Top-$k$ 块即可还原高精度。 |
| **CXL (`CXL.mem`)** | **通用计算互联总线**，支持 CPU 以原生 Load/Store 指令（Cacheline 粒度）直接访问池化扩展内存。 | **直接内存映射 I/O、无协议栈直接总线寻址、绕过 SMMU 的外设内存** | 解决离散小块 DMA 效率崩塌的终极硬件武器。 |

---

## 一、 全景架构定位与第一性原理推导

### 1.1 核心矛盾与双挡位递进设计哲学

#### 1.1.1 【问题背景与大模型推理物理直觉】
大语言模型（LLM）的生成机制是自回归的：输入序列为 $X = [x_1, x_2, \dots, x_T]$。当模型要生成下一个 Token $x_{T+1}$ 时，其核心的 Self-Attention 机制需要让当前位置的查询向量 $Q_{T+1}$ 与历史上**所有** $t \le T$ 的键向量 $K_t$ 做点积运算，再对值向量 $V_t$ 加权求和：
$$\text{Attention}(Q_{T+1}, K, V) = \text{Softmax}\left(\frac{Q_{T+1} K^\top}{\sqrt{d_k}}\right)V$$
* **显存与 I/O 爆炸的数学根源**：以典型的开源大模型 Qwen2.5-72B 或 Llama-3-70B（80 层 Transformer，8 个 KV Head，Head 维度 128，BF16 占用 2 字节）为例，单个 Token 在单层中产生的 K 与 V 占用显存为：
  $$\text{Bytes}_{\text{per\_token\_layer}} = 2 \times n_{\text{kv}} \times d_{\text{head}} \times \text{sizeof(BF16)} = 2 \times 8 \times 128 \times 2 = 4096\text{ Bytes} = 4\text{ KB}$$
  当模型有 $L=80$ 层，且单请求上下文长度达到 $S=32\text{K}$（32768 Tokens）时，单次请求的 KVCache 体积高达：
  $$V_{\text{req}} = 80 \times 32768 \times 4\text{ KB} \approx 10.74\text{ GB}$$
* **分布式外部存储池的必然性**：单张 GPU/NPU 高带宽显存（HBM）通常仅有 64GB~80GB，扣除模型权重自身的 140GB（8 卡张量并行 TP=8 下每卡固定占约 17.5GB），剩余显存只能支撑极少数长上下文请求并发。因此，将非活跃、跨会话或多轮对话的历史 KVCache 卸载（Offload）到由 Host DDR、CXL 扩展内存及远端节点构成的 Mooncake 统一异构存储底座中，并在发生前缀重合时拉取重用（Saved-Prefill），是支撑长文本服务的大势所趋。

#### 1.1.2 【两大本质物理瓶颈】
在现有工业级实现（SGLang HiCache 与 vLLM KVConnector）中，长序列（$16\text{K}\sim 128\text{K}$）从 Mooncake 分布式池拉回 KVCache 时面临两大物理瓶颈：
1. **时间轴上的串行暴露泡沫**：现有推理引擎采取“先等待整批（全 80 层）KVC 拉回显存 $\to$ 再启动 GPU/NPU 前向计算”的粗粒度同步模式。在典型 $40\text{ GB/s}$ 网络下，拉取 10GB 数据需要约 $250\text{ ms}$。在这 $250\text{ ms}$ 内，昂贵的 GPU 计算核心完全处于空闲等待（Idle）状态，此传输耗时 $100\%$ 直接暴露在端到端 TTFT 关键路径上。
2. **空间轴上的全量搬运冗余与稀疏小包风暴悖论**：算法研究表明，在超长序列注意力运算中，仅有 $15\%\sim 30\%$ 的重点 Token 主导了最终结果，其余 $70\%+$ 的 Token 权重接近于 0。然而，**这 $20\%$ 的重要数据在物理内存块（Block）上是高度离散分布的**。在传统基于 PCIe DMA 或标准 RDMA 的块设备架构中，拉取 800 个离散的 $4\text{ KB}$ 碎块，会引发极度严重的描述符提交开销、网卡 WQE 队列拥塞、IO Die SMMU TLB 频繁击穿以及 PCIe TLP 头部开销过大（有效载荷效率 $<40\%$），导致**拉取 $20\%$ 离散碎块的物理耗时甚至远超顺序拉取 $100\%$ 全量数据**！

#### 1.1.3 【双挡位正交递进设计哲学】
为此，`M2-F01` 确立了面向系统物理本质的 **“双挡位正交递进”** 实施路线：
* **第一挡（100% 比特级无损底座 —— 按层计算-通信全掩盖双缓冲引擎）**：
  * **设计思想**：不修改模型任何数学逻辑，保证计算精度 $100\%$ 比特级一致。
  * **破局原理**：打破“整批拉完才算”的粗粒度同步屏障，将调度粒度细化为“层（Layer）”。利用 GPU 正在计算第 $l$ 层的耗时窗口，并发在后台总线上拉取第 $l+1$ 层的 KVCache；结合前 $K^*$ 层本地零等待重算技术，彻底消灭首层启动气泡，达成 **流水重叠率 $>85\%$，独立降低 E2E TTFT $20\%\sim 30\%$**。
* **第二挡（高倍增益倍增器 —— 基于 CXL 内存语义的注意力重要性稀疏抽稀引擎）**：
  * **设计思想**：利用体系结构硬件特性打破算法稀疏带来的碎包惩罚。
  * **破局原理**：深度利用 **CXL.mem 字节/Cacheline（64B）级原生寻址、硬件解码直接绕过 IO Die SMMU 页表转换、CPU 寄存器级非临时流式聚簇** 的物理红利，在 Host 侧将离散的 Top-$k$（$25\%\sim 50\%$）高价值 KVC 块合并为连续大包后再推送至 GPU HBM。使 **跨介质传输量锐减 $50\%\sim 75\%$，通信耗时彻底压缩进单层计算阴影内（重叠率 $\to 100\%$），长序列 TTFT 再次跃升 $25\%\sim 40\%$，IO Die SMMU TLB Miss 降低 $>90\%$**。

---

### 1.2 端到端业务指标与硬件负载的第一性原理数学模型

设模型总层数为 $L$（如 80），单请求上下文长度为 $S$，命中缓存前缀长度为 $S_{\text{hit}}$，未命中增量长度为 $S_{\text{miss}} = S - S_{\text{hit}}$。

#### （1）基线串行架构下的 TTFT 模型（优化前痛点）
$$T_{\text{TTFT}}^{\text{serial}} = T_{\text{desc\_submit}} + \sum_{l=0}^{L-1} \frac{V_{\text{layer}}(S_{\text{hit}})}{B_{\text{eff}}(G_{\text{io}})} + \sum_{l=0}^{L-1} T_{\text{comp}}\left(l, S_{\text{hit}}, S_{\text{miss}}\right)$$
* **公式物理内涵剖析**：
  * $T_{\text{desc\_submit}}$：Host CPU 遍历构造 80 层散落内存描述符的开销。
  * $\frac{V_{\text{layer}}(S_{\text{hit}})}{B_{\text{eff}}(G_{\text{io}})}$：单层数据传输耗时。$B_{\text{eff}}(G_{\text{io}})$ 表示在连续 I/O 粒度为 $G_{\text{io}}$ 时的物理有效带宽。当数据碎片化（$G_{\text{io}} \le 8\text{ KB}$）时，受限于 PCIe TLP 包头占比（40B Header 传 64B 数据，效率仅 $61\%$）与 SMMU 查表抖动，$B_{\text{eff}}$ 会从物理线速的 $38\text{ GB/s}$ 断崖式下跌至 $<5\text{ GB/s}$。
  * $\sum T_{\text{comp}}$：GPU 在所有数据就绪后，执行全量计算的时间。
  * **致命缺陷**：数据拉取项与计算项是完全的数学加法关系（串行暴露），没有发生任何重叠。

#### （2）引入“第一挡（按层掩盖 + $K^*$ 混合执行）”与“第二挡（CXL 稀疏抽稀 $\alpha_{\text{sparse}}$）”后的终局 TTFT 模型
设稀疏保留比例为 $\alpha_{\text{sparse}} \in [0.25, 0.50]$（第一挡全量无损模式下 $\alpha_{\text{sparse}} = 1.0$），结合 YY 模块的位平面无损压缩比 $\gamma_{\text{comp}} \approx 1.85$，WXH 调度模块下发的动态重算切分层为 $K^* \in [0, L)$，则优化后的端到端 TTFT 为：
$$T_{\text{TTFT}}^{\text{opt}} = \underbrace{\sum_{l=0}^{K^*-1} T_{\text{recomp}}(l, S)}_{\text{前 } K^* \text{ 层零等待重算（完全掩盖后序预取启动延迟）}} + \sum_{l=K^*}^{L-1} \max\left( T_{\text{comp}}\left(l, \alpha_{\text{sparse}} S_{\text{hit}}, S_{\text{miss}}\right), \; T_{\text{xfer}}^{\text{CXL}}\left(l+1, \frac{\alpha_{\text{sparse}} V_{\text{layer}}}{\gamma_{\text{comp}}}\right) \right)$$

* **核心参数 $K^*$ 的物理意义（平息首层冷启动的平衡支点）**：
  若直接从第 0 层做按层拉取，GPU 必须先等第 0 层拉回来才能开工（产生 $15\sim 30\text{ ms}$ 的首层气泡）。
  **破局思路**：GPU 算力是现成的，对于前 $K^*$ 层，我们不要去网络上拉 KVCache，而是直接让 GPU 在本地利用输入文本重新做 Prefill 计算（重算耗时为 $T_{\text{recomp}}$）。在 GPU 忙于计算前 $K^*$ 层的这几十毫秒内，后台 DMA/CXL 传输通道马力全开，提前将第 $K^*$ 层及后续层的 KVCache 拉入显存！
  最优切分层 $K^*$ 由下式在 $<2\mu\text{s}$ 内闭式求解（让 GPU 算完第 $K^*-1$ 层与第 $K^*$ 层数据传输就绪实现**零停顿精准会师**）：
  $$K^* = \arg\min_{k \in [0, L]} \left| \sum_{l=0}^{k-1} T_{\text{recomp}}(l, S) - \left( T_{\text{startup}} + T_{\text{xfer}}^{\text{CXL}}\left(k, \frac{\alpha_{\text{sparse}} V_{\text{layer}}}{\gamma_{\text{comp}}}\right) \right) \right|$$

* **计算-通信流水重叠率（Overlap Ratio, $\eta_{\text{overlap}}$）定义**：
  $$\eta_{\text{overlap}} = \frac{T_{\text{comp\_total}} + T_{\text{xfer\_total}} - T_{\text{TTFT}}^{\text{opt}}}{\min\left(T_{\text{comp\_total}}, T_{\text{xfer\_total}}\right)} \times 100\%$$
  当开启第二挡 CXL 稀疏抽稀（$\alpha_{\text{sparse}} \le 0.35$）后，单层聚簇传输耗时 $T_{\text{xfer}}^{\text{CXL}}$ 被压降至 $<0.8\text{ ms}$，远小于单层 GPU 计算耗时 $T_{\text{comp}}$（$3\sim 12\text{ ms}$），物理上达成 $\max(T_{\text{comp}}, T_{\text{xfer}}^{\text{CXL}}) \equiv T_{\text{comp}}$，即 **网络与总线传输耗时被 $100\%$ 隐藏于计算阴影之下**！

---

### 1.3 跨模块正交协同边界与接口契约总图

```
+---------------------------------------------------------------------------------------------------+
|                                  上层推理调度与控制面 (SGLang / vLLM)                                 |
|  +---------------------------------------------+   +-------------------------------------------+  |
|  | WXH (M2-T05): <5us 选路与 K* 混合切分决策器     |   | WZX (M2-T03): 双池隔离 Slab 分配器          |  |
|  | - 读取 NXY 64B CapabilityMatrix             |   | - Prefill 16-Block 连续 Slab 池 (VA连续>90%)|  |
|  | - 输出: HybridExecPlan {K*, alpha_sparse}   |   | - 底层: 编译 LayerBoundaryDescriptor (UMR) |  |
|  +----------------------+----------------------+   +---------------------+---------------------+  |
+-------------------------|------------------------------------------------|------------------------+
                          v                                                v
+---------------------------------------------------------------------------------------------------+
|                    M2-F01 模块: 按层计算-通信双缓冲掩盖与 CXL 稀疏抽稀引擎 (本方案核心)                 |
|                                                                                                   |
|  [第一挡：按层双缓冲掩盖流水线]                         [第二挡：CXL.mem 内存语义稀疏聚簇加速]            |
|  • Layer 0..K*-1: GPU 零等待本地重算                   • Landmark 块级摘要常驻 DDR/CXL 热区 (<1.5%体积) |
|  • Layer l 计算 || Layer l+1/l+2 异步 DMA 预取        • CPU NEON 内积 <50us 筛选 Layer l+1 Top-k 索引 |
|  • C++ 算子流内嵌 Event 同步 (零 Python 阻塞)         • <16KB: CXL.mem ldnp/stnp 非临时流式聚簇       |
|  • Decode 异步写回 + 200us 微切片限流                 • 寄存器内直联 YY (M2-F02) NEON 位平面解交织     |
+-------------------------+------------------------------------------------+------------------------+
                          |                                                |
                          v                                                v
+---------------------------------------------------------------------------------------------------+
|                              底层硬件遥测、亲和内存池与高速传输底座                                   |
|  +-------------------------------+  +--------------------------------+  +----------------------+  |
|  | NXY (M2-T04): 硬件物理能力矩阵  |  | YZN (M2-T02) & CY (M2-T01):    |  | CXL 2.0/3.0 内存池   |  |
|  | - mmap 直读鲲鹏 DDRC/SMMU/RC  |  | - 1GB 巨页 SCCL-IODie-Port 亲和|  | - HDM 硬件直接译码   |  |
|  | - MPAM MBW_MAX 硬件带宽限流    |  | - Post/Poll 分离 + 多SGE大包   |  | - 绕过 IO Die SMMU   |  |
|  +-------------------------------+  +--------------------------------+  +----------------------+  |
+---------------------------------------------------------------------------------------------------+
```

#### 【协同边界职责划分与交接清单（供主责工程师快速对账）】
1. **输入依赖（本模块接收）**：
   * 来自 **WXH (`M2-T05`)**：输入当前请求的 `HybridExecPlan`，包括计算出的 $K^*$ 数值与建议稀疏比例 $\alpha_{\text{sparse}}$。
   * 来自 **WZX (`M2-T03`)**：输入各层在 HBM 上的连续虚拟地址段（Slab Handle）或 `LayerBoundaryDescriptor` 结构体指针。
   * 来自 **NXY (`M2-T04`)**：输入共享内存中的 64B 硬件物理能力矩阵（CapabilityMatrix），供运行时校验底层 PCIe 与 DDRC 拥塞度。
   * 来自 **YY (`M2-F02`)**：输入 NEON 汇编解压缩函数符号 `neon_bitplane_reconstruct_register()`。
2. **输出交付（本模块提供）**：
   * 输出 `LayerStreamSynchronizer` 跨流无锁状态机，驱动 GPU 计算流与传输流硬件级流水掩盖。
   * 输出 `CXLSparseGatherEngine`，将散落在 CXL 中的稀疏块聚簇写入主机连续内存并触发 DMA 直达显存。

---

## 二、 第一挡方案设计：SGLang / vLLM + Mooncake 按层计算-通信全掩盖双缓冲引擎

### 2.1 现有 SGLang 与 vLLM 架构中的串行卡点剖析

#### 2.1.1 【问题背景：推理引擎是怎样处理缓存命中的？】
为了在框架中修改代码，必须先理清一个外部请求在 SGLang 和 vLLM 中的执行轨迹：
1. **SGLang 的请求生命周期**：
   * 客户端发送 Prompt $\to$ SGLang HTTP 服务端解析 $\to$ 调度器（Scheduler）在 `RadixCache`（前缀树索引）中查找是否存在已缓存的 Key-Value。
   * 若命中外部存储（Mooncake）：控制流进入 `python/sglang/srt/mem_cache/hicache_controller.py` 的 `prefetch_from_storage()`。
   * **现有串行卡点**：当前代码直接调用 Mooncake 存储接口执行 `mooncake_store.get_batch()`，**在这个 Python 函数内部同步阻塞等待全量 80 层的数据全部搬运上卡**！在此期间，请求被冻结在等待队列，GPU 计算流根本没有收到任何算子发射指令（Kernel Launch），造成巨大的执行泡沫。
2. **vLLM V1 的请求生命周期**：
   * 调度器分配物理块 $\to$ 调用 `KVConnectorBase_V1.start_load_kv()` 发起数据传输 $\to$ 进入 `GPUModelRunner.execute_model()` 执行实际的前向计算。
   * **现有串行卡点**：虽然 vLLM 架构定义了逐层等待接口 `wait_for_layer_load(layer_name)`，但原生实现中缺乏底层跨流 Hardware Event 门控，往往在第 0 层前调用了全局的 `torch.cuda.synchronize()`，导致按层解耦流于表面，实际退化为粗粒度同步。

#### 2.1.2 【主责开发者与 AI Agent 协同排查卡点实操指引】
> [!TIP] **Agent 协同辅助指令（卡点复现与性能剖析）**  
> 你可以直接将以下 Prompt 发送给编码 Agent 协助你生成探针脚本，精确定位串行同步卡点：  
> ```text
> "请帮我编写一个基于 PyTorch CUDA Event 的轻量性能探针，挂载到 SGLang 的 HiCacheController.prefetch_from_storage() 
> 和 ModelRunner.forward_extend() 入口。采集并打印出从发出 Mooncake 拉取请求到 GPU 执行第一层 Attention 算子之间的
> 时间差（以微秒为单位），以此验证原生代码中是否存在全局同步阻塞卡点。"
> ```

---

### 2.2 核心机制一：双模式自适应 HBM 滑动窗

#### 2.2.1 【优化原理：为什么不需要为所有 80 层同时预分配显存？】
传统思路认为：既然要拉取 80 层的数据，就必须在 GPU HBM 中为这 80 层全部预先分配物理页（32K 上下文下需占用约 10GB 显存）。
**深入大模型前向计算物理机理，这一假设完全不成立**：
* 在 **Prefill 阶段**，模型是逐层计算的。当计算第 $l$ 层 Attention 时，它只需要第 $l$ 层的 KVCache。一旦第 $l$ 层的 Attention 算子执行结束，当前层的 KVCache **在本次 Prefill 阶段内永远不会再被读取**！
* 因此，在不需要将 KVCache 长期驻留显存的场景下（例如 Prefill 独立节点、或者单轮摘要任务），我们完全只需要**两层容量的显存作为临时旋转缓冲区（Ping-Pong Buffer）**：
  * **Slot 0 (Ping)**：供第 $l$ 层计算读取；
  * **Slot 1 (Pong)**：供后台传输流同时写入第 $l+1$ 层的数据。
  * 当第 $l$ 层算完、第 $l+1$ 层开始计算时，两个槽位角色互换，Slot 0 立即被覆写用于接收第 $l+2$ 层的数据！

为此，双缓冲引擎设计为根据业务特征自适应切换的 **双模式滑动窗架构**：

#### 模式 A：纯 $256\text{ MB}$ 瞬态 Ping-Pong 滑动窗（算完即弃模式）
* **适用场景**：Prefill 与 Decode 计算生成解耦架构（PD 分离）下的独立 Prefill 节点、长文本单轮理解问答、或 `ShadowKV` 显存按需卸载模式。
* **物理显存布局与计算公式**：
  固定在初始化时开辟 2 个槽位的环形缓冲区：
  $$\text{Size}_{\text{window}} = 2 \times \left(\frac{S_{\text{max}} \times 2 \times n_{\text{kv\_heads}} \times d_{\text{head}} \times \text{sizeof(BF16)}}{\text{TP\_Size}}\right)$$
  在 8 卡张量并行（$\text{TP}=8$）下，单层单卡仅占 $16\text{ MB}$，2 层 Ping-Pong 双缓冲在单卡上**仅需固定占用 $32\text{ MB}$ 显存（8 卡总计仅 $256\text{ MB}$）**。相比全量预占 10GB 显存，**显存开销直接降低 $97.5\%$**！

#### 模式 B：基于预分配 Slab 的 2-Layer In-Flight 门控滑动窗（Decode 驻留模式）
* **适用场景**：Prefill 算完后需要立即转入本地多轮 Decode 生成的常规服务场景。
* **问题痛点**：在此模式下，80 层的数据最终都必须保存在 HBM 中供后续逐字生成读取。但若在 Step 开始时一次性将 80 个 DMA/RDMA 请求全部发给底层硬件，会导致 PCIe Root Complex（RC）和网卡硬件队列瞬间被打爆，严重推高正在运行的其他在线 Decode 请求的时延。
* **门控机制（Paced Sliding-Window DMA）**：
  * 引入 **滑动窗口信号量（窗口深度 $W = 2$）**：后台传输流最多只能超前当前 GPU 正在计算的层数 2 层（即当 GPU 正在计算第 $l$ 层时，传输流只被允许搬运第 $l+1$ 和第 $l+2$ 层）。
  * 只有当第 $l$ 层计算完成并触发硬件事件 `layer_comp_done_event[l]` 后，传输引擎才被准许签发第 $l+3$ 层的搬运任务。
  * **物理效益**：将突发的暴风式数据搬运平滑为均匀的细水长流，彻底根除 PCIe 总线拥塞与对在线推理的干扰。

---

### 2.3 核心机制二：基于硬件物理能力矩阵的 $K^*$ 层间混合执行引擎

#### 2.3.1 【优化原理：消灭第 0 层冷启动泡沫】
即使采用了按层双缓冲掩盖，系统仍面临一个经典力学难题 —— **首层冷启动气泡（Layer-0 Cold-Start Bubble）**：
要计算第 0 层，必须先拉取第 0 层的 KVCache。在底层网络出现微突发拥塞时（可用带宽降至 $10\text{ GB/s}$），拉取第 0 层可能需要 $15\text{ ms}$。在这 $15\text{ ms}$ 内，GPU 仍被迫空转！

**打破常规的破局机理（$K^*$ 动态混合执行）**：
1. **GPU 算力的零成本平替**：大模型输入的原始文本（Prompt 字符串）非常小（几千字仅几十 KB），它早就存在于本地显存中。GPU 计算前几个 Transformer 层的速度极快！
2. **算移互换（Trade Compute for Latency）**：
   * 调度器（WXH `M2-T05`）实时读取底层硬件物理能力矩阵（CapabilityMatrix），得知当前通道带宽和排队情况，计算出一个临界层数 $K^*$（例如 $K^* = 6$）。
   * **执行策略反转**：对于前 $K^*$ 层（Layer $0 \dots K^*-1$），**传输流根本不去网络拉取缓存**！GPU 在第 $0\mu\text{s}$ 瞬间启动，直接把前 $K^*$ 层当作“没有命中缓存”，在本地用 Prompt 文本直接做前向重算（Recompute）！
   * 与此同时，后台传输流利用 GPU 算这前 $K^*$ 层的宝贵时间（约 $20\sim 25\text{ ms}$），在网络上全力提前预取 **第 $K^*$ 层及后续层** 的 KVCache。
3. **零停顿无缝会师**：
   当 GPU 算完第 $K^*-1$ 层准备进入第 $K^*$ 层时，后台拉取的第 $K^*$ 层数据刚好落入显存！GPU 顺滑切换为读取缓存做 Attention，**端到端 I/O 停顿彻底归零**。

```
时间轴对比 (Timeline) --->
[传统按层流水]  |--等待L0拉取(15ms)--|==算L0==|==算L1==|==算L2==| ... (存在15ms冷启动空白)
[K* 混合流水]   |==本地重算L0..L5 (20ms，零等待立即启动)==|==算L6(读缓存)==|==算L7(读缓存)==| ...
               |--后台并发拉取 L6、L7 缓存 (完美掩盖)--->| (0us 气泡无缝衔接!)
```

---

### 2.4 核心机制三：Decode 与逐层驱逐的零停顿异步写回

#### 2.4.1 【优化原理：前后台流量隔离与微秒级 QoS 管控】
在自回归 Decode 阶段，模型每生成一个新 Token，就会产生新的 Key 和 Value。当显存不足需要换出，或者需要将数据同步至 Mooncake 存储池时，若直接在主前向计算流中调用写回，会导致计算核心被 I/O 阻塞。
* **双流解耦**：在第 $l$ 层 Attention 算子执行完毕后，主计算流仅在 GPU 硬件上记录一个事件标记 `layer_kv_ready_event[l]`，然后**零延迟立即继续执行第 $l$ 层的后续 MLP 算子与第 $l+1$ 层**。
* **独立写回流与微切片（Micro-Slicing）**：由独立的低优先级传输流（`d2h_offload_stream`）监听该事件并执行写回。为防止后台写回流量占用全部 PCIe 带宽导致在线前台请求的 TPOT 抖动，单次 DMA 传输被强制切片为 $\le 512\text{ KB}$（在 PCIe 上占用时间严格 $<200\mu\text{s}$），配合底层的 ARM MPAM 硬件限流，确保前台业务受到的时延干扰率 $<3\%$。

---

### 2.5 跨流无锁同步状态机（规避 Python 调度阻塞）

#### 2.5.1 【优化原理：为什么必须消除逐层的 Python 交互？】
在 Python 解释器中，一次函数调用、全局解释器锁（GIL）释放与重新获取、以及调用 PyTorch C++ 绑定的开销约为 $5\sim 15\mu\text{s}$。如果 80 层之间每一层都要由 Python 代码来做“判断数据是否就绪 $\to$ 调用算子”，80 层累积的 Python 纯调度开销就高达 $1\sim 2\text{ ms}$，这足以抹平底层的优化收益！

**终极解法：Host 侧全图一次性下发 + 设备侧硬件事件门控（Hardware Event-Driven Execution）**：
* Python / Host C++ 线程在请求开始时，以极快的速度（$<0.5\text{ ms}$）一口气向设备驱动下发所有 80 层的计算指令与等待依赖，随后 Host 线程立即返回，不再参与逐层控制。
* **硬件级状态机流动**：真正的逐层同步完全交由 GPU/NPU 硬件命令队列中的 `cudaStreamWaitEvent` 与 `cudaEventRecord` 在硬件微架构层面执行。

```
时间轴 (Timeline) --->

[Host 发射线程] (总耗时 < 0.5 ms，一次性把 80 层算子全部发射进硬件队列，不阻塞!)
  |-- Launch Layer 0..K*-1 (重算) -->|
  |-- StreamWait(evt_xfer[K*]) -> Launch Layer K* -> Record(evt_comp[K*]) -->| ... 直至 Layer 79

[后台传输流 (xfer_stream)]
  |-- 拉取 Layer K* KVC --->| Record(evt_xfer[K*])
                            |-- 拉取 Layer K*+1 KVC --->| Record(evt_xfer[K*+1])
                                                        |-- Wait(evt_comp[K*]) -> 拉取 Layer K*+2 --->|

[GPU 计算流 (compute_stream)]
  |======== Compute Layer 0..K*-1 ========| (零等待，完美掩盖 Layer K* 传输!)
                                          | (硬件事件已就绪，0us 停顿直接穿透!)
                                          |==== Compute Layer K* ====| Record(evt_comp[K*])
                                                                     |==== Compute Layer K*+1 ====|
```

---

## 三、 第二挡方案设计：融合 CXL 内存语义的注意力重要性稀疏 KVC 抽稀引擎

### 3.1 为什么传统块 I/O 与标准 RDMA/PCIe 做不好稀疏 KVC？

#### 3.1.1 【问题背景：注意力稀疏性与“稀疏小包风暴”悖论】
在长文本推理中，注意力机制计算出的权重矩阵具有极端稀疏性：
$$\mathbf{A} = \text{Softmax}\left(\frac{\mathbf{Q} \mathbf{K}^\top}{\sqrt{d}}\right)$$
在 $32\text{K}\sim 64\text{K}$ 的序列中，绝大多数位置的注意力权重衰减为 0，真正对当前生成起决定性作用的只有约 $20\%$ 的 Token（称为重击者 Heavy-Hitters 和 注意力汇聚点 Attention Sinks）。
* **算法工程师的直觉**：既然只要 $20\%$ 的数据，那我们传输时只拉取这 $20\%$ 的 KVCache 块，传输时间不就减少了 $80\%$ 吗？
* **计算机体系结构的残酷现实**：清华马腾团队在 2026 年 6 月发表的 `SAC` 论文（`arXiv:2606.19746`）揭示了一个反直觉的残酷现象 —— **稀疏选出来的块在物理内存地址上是高度离散破碎的**！
  * 假设 64K 上下文切分为 4096 个块，Top-20% 意味着要抓取约 800 个离散块。
  * 在传统 PCIe DMA 或 RDMA 网络中，每个块单层仅有 $1\text{ KB}\sim 8\text{ KB}$。
  * **硬件系统崩溃三部曲**：
    1. **描述符风暴**：CPU 必须为这 800 个离散地址分别构建传输描述符（WQE/SGE），CPU 协议栈直接跑满；
    2. **IO Die SMMU TLB 击穿**：在国产芯片（如鲲鹏 920）架构下，外设发起的 DMA 必须经过 IO Die 上的系统内存管理单元（SMMUv3）进行虚实地址转换。800 个随机跳跃的地址会导致 SMMU 的 TLB 发生极高频的 Miss，硬件频繁发起慢速的内存遍历页表（Page Table Walk）；
    3. **PCIe 载荷效率暴跌**：PCIe 控制器发送大量 $1\text{ KB}$ 小包，导致 PCIe 事务层包头（TLP Header）占比畸高，且频繁触发接收端保序队列（Ordering Queue）阻塞。
  * **结论**：在传统 I/O 设备上，拉取 $20\%$ 离散碎块的物理耗时，反而比顺序拉取 $100\%$ 全量连续数据慢了近一倍！

---

### 3.2 CXL 内存语义（`CXL.mem`）赋能稀疏 KVC 的四大物理红利

#### 3.2.1 【优化原理：CXL.mem 为什么是离散稀疏访问的天选之子？】
CXL（Compute Express Link）是一种运行在 PCIe 物理电气特性之上的全新互联协议。其中的 **`CXL.mem` 协议** 将外部扩展内存池直接映射为 Host CPU 的物理内存地址空间。
对比传统架构，`CXL.mem` 带来了四大降维打击般的物理优势：

| 物理维度 | 传统 PCIe Block DMA / 标准 RDMA | 基于 `CXL.mem` 内存语义的稀疏访问 | 系统工程收益 |
| :--- | :--- | :--- | :--- |
| **最小访问粒度** | 页级 / 块级（$\ge 64\text{ KB}$ 才能跑满带宽，$<8\text{ KB}$ 带宽跌破 $20\%$） | **64 字节（64B Cacheline）字节级精准寻址** | 完美契合细粒度 Landmark 摘要（128B）与 DeepSeek MLA 潜向量（1KB），**零数据读放大**！ |
| **地址转换路径** | 必须由外设穿透 IO Die **SMMU (IOMMU)** 查表，离散访问触发严重 TLB Miss | 由 CPU MMU 与 CXL **HDM 硬件解码器** 直接物理线性寻址，**100% 绕过 IO Die SMMU** | **IO Die `smmuv3_pmcg/tlb_miss` 降低 $>90\%$**，彻底解除 IO 芯片瓶颈。 |
| **驱动与协议开销** | 构造 WQE/SGE、敲门铃（Doorbell）、硬件中断或轮询，单次微秒级 | CPU 原生 **Load/Store 指令（`ldnp`/`stnp`）**，硬件级纳秒流水（$180\sim 250\text{ ns}$） | 彻底消灭操作系统与网络协议栈开销，CPU 提交耗时趋近于 0。 |
| **计算与搬运融合** | 仅能做单纯的字节搬运，无法在数据流转途中做任何格式变换 | 数据流经 CPU NEON 向量寄存器时，**原地完成点积筛选 + 聚簇 + 位平面解压缩** | 避免数据在内存与寄存器之间多次无谓往返，节省 $2\sim 3$ 倍内存总线带宽！ |

---

### 3.3 算法-系统协同设计：无损块级 Landmark 摘要与动态稀疏水位

#### 3.3.1 【优化原理：如何在不拉取完整数据前预知哪些块重要？】
要实现稀疏拉取，最大的挑战在于：**在第 $l+1$ 层数据还在 CXL 里没拉出来之前，我们怎么知道哪 $20\%$ 的块重要？**
如果为了算权重把全量数据都拉出来，稀疏性就失去了意义！

**算法-系统协同的 Landmark 均值摘要方案**：
1. **写时极简摘要（Chunk Landmark Summary）**：
   * 在 KVCache 初次生成并卸载到 CXL 时，按每 $C = 16$ 个 Token 划分为一个微块（Mini-Chunk）。
   * 对该微块内 16 个 Token 的 Key 向量计算一个均值向量：
     $$\mathbf{m}_c = \frac{1}{16}\sum_{t \in c} \mathbf{k}_t$$
   * **极低空间开销**：均值向量仅占原微块体积的 **$1/16$（约 $6.25\%$）**。我们将这极小部分的 Landmark 摘要常驻在 Host DDR 或 CXL 的热区（Hot Metadata Zone）。
2. **层间注意力强相关定理**：
   学术界与工业界实测表明：在 Transformer 中，第 $l$ 层算出的 Query 向量 $\mathbf{q}_l$，与第 $l+1$ 层的注意力分布具有极高的相似相关性。
   因此，当 GPU 正在算第 $l$ 层时，Host CPU 利用 NEON 向量指令，让 $\mathbf{q}_l$ 与常驻热区的第 $l+1$ 层各个微块的均值向量 $\mathbf{m}_{l+1, c}$ 做快速点积内积运算：
   $$\text{Score}_c = \mathbf{q}_l \cdot \mathbf{m}_{l+1, c}^\top$$
   在 $<50\mu\text{s}$ 内，即可高精度预测出第 $l+1$ 层最重要的 Top-$k$ 块索引列表！
3. **两头锁死与自适应水位（防精度雪崩）**：
   * **两头锁死策略**：首部 **Attention Sink（前 4 个块，64 Tokens）** 记录了句首的全局语义锚点，尾部 **Local Window（最后 16 个块，256 Tokens）** 记录了最近上下文，这两部分**强制 $100\%$ 保留**，绝不抽稀！
   * **自适应稀疏水位 $\alpha_{\text{sparse}}$**：
     * 普通对话/长文问答：设置 $\alpha_{\text{sparse}} = 25\%$（数据量削减 $75\%$）；
     * 高精度代码任务（SWE-bench）：代码语法对丢失 Token 极度敏感，自动放宽至 $\alpha_{\text{sparse}} = 35\%\sim 50\%$，且对模型最底层的 4 层固定保持 $100\%$ 全量计算，确保 **代码任务精度损失严格 $<1.0\%$**。

---

### 3.4 CXL 硬件亲和稀疏聚簇双路径架构

#### 3.4.1 粒度交越律（Granularity Crossover Threshold: $G_{\text{cross}} = 16\text{ KB}$）
当 CPU 筛选出 Top-$k$ 块索引后，**严禁直接让外设去拉离散小块**。CXL 聚簇引擎首先检查合并后的平均连续物理块大小 $\bar{G}_{\text{chunk}}$：
* **若 $\bar{G}_{\text{chunk}} < 16\text{ KB}$（细粒度离散块）**：走 **路径 A**，利用 CPU 多核 ARMv8 NEON 汇编指令，通过 `CXL.mem` 做流式内存聚簇；
* **若 $\bar{G}_{\text{chunk}} \ge 16\text{ KB}$（粗粒度连续块）**：走 **路径 B**，直接将物理地址填入 WZX 的硬件 SGL 链表，发起标准硬件 DMA。

#### 3.4.2 路径 A 核心技术：ARMv8 非临时流式访存（防 L3 污染）+ 寄存器级解压融合
在国产鲲鹏 920 处理器上，传统 `memcpy` 会把数据大批量加载到 CPU 的三级缓存（L3/SLC Cache）中，引发剧烈的缓存污染（Cache Pollution），将在线业务的指令和元数据全部挤出缓存。
**微架构级汇编破局**：
1. **`prfm pldl1strm`（流式预取指令）**：超前 12 条 Cacheline 预取数据，掩盖 CXL 约 $200\text{ ns}$ 的跨总线延迟；
2. **`ldnp`（Non-Temporal Load，非临时流式加载）**：指示 CPU 硬件直接把 64B Cacheline 数据读入 NEON 128-bit 向量寄存器，**绝对不分配 L3 Cache 行**！
3. **寄存器内位平面解压融合**：若 CXL 中存储的是 YY 模块压缩后的数据，数据在寄存器内部直接调用 `vzip1q_u8` / `vzip2q_u8` 指令解压缩为 BF16，**不产生任何中间内存读写**！
4. **`stnp`（Non-Temporal Store，非临时流式写入）**：将聚簇解压后的连续数据顺序写入预先分配的 1GB 巨页 Staging 缓冲区。
5. **单一大包 DMA**：最后通知 GPU 发起**单次大包 DMA**，把这一大段连续数据拉入显存！

```
[CXL.mem 物理内存空间 (散落的 Top-k 离散块)]
       |
       | 1. 流式预取: prfm pldl1strm, [cxl_va_next]
       | 2. 非临时加载: ldnp q0, q1, [cxl_va_curr] (直接读进 NEON 寄存器，绕过 L3 缓存!)
       v
+---------------------------------------------------------------------------------------------------+
| 鲲鹏 920 ARMv8 NEON 128-bit 向量寄存器组 (v0 ~ v31) —— 原地计算与重构                                  |
|   • 3. 寄存器内直接还原 YY 压缩数据：调用 vzip 单指令解包为标准 BF16 张量                               |
+---------------------------------------------------------------------------------------------------+
       |
       | 4. 非临时写入: stnp q2, q3, [host_staging_buffer] (顺序写入 1GB 连续巨页，零 Cache 污染!)
       v
[Host 100% 物理连续的暂存缓冲区 (每层仅 1 个连续大块)]
       |
       | 5. 单一描述符大包 DMA / GPUDirect (仅 1 个 WQE，SMMU 零压力，总线带宽利用率 >92%!)
       v
[GPU/NPU HBM 显存槽位]
```

#### 3.4.3 路径 C：跨节点 Mooncake 远端命中时的“Target-Side CXL 聚簇 + 单包 RDMA 推送”
当数据位于远端存储节点时：
1. 本地消费端绝不发起数百个离散小块的 `RDMA READ`；
2. 本地仅向远端发送一个只有 **$256\text{ B}$ 的紧凑位图（Bitmask）**；
3. 由远端节点的后台线程在它本地的 CXL 池中完成流式聚簇，打成一个连续大包后，通过单次 `RDMA WRITE WITH IMM` 大包直接推入消费端显存！

---

### 3.5 稀疏加载后的框架侧动态页表重构（Dynamic Page Table Compaction）

#### 3.5.1 【优化原理：如何让现有 GPU 算子零修改直接跑在稀疏数据上？】
许多工程师误以为：既然拉入显存的块数减少到了 $30\%$，那底层的 CUDA / CANN Attention 算子代码（如 FlashAttention / FlashInfer）是不是必须重写？
**答案是：完全不需要！利用虚拟内存页表重映射思想即可零修改适配**：
1. **操作系统页表类比**：在 Linux 中，物理内存可以是不连续的，只要通过页表（Page Table）把它映射成连续的虚拟地址即可。
2. **PagedAttention 的 Block Table**：大模型推理框架也是通过一个名为 `block_table` 的整型数组来寻址物理块的。
   * 假设原来长文本对应 100 个块：`block_table = [物理块0, 物理块1, ..., 物理块99]`。
   * 现在稀疏拉取后只有 30 个块进入显存，我们只需在 Python 侧构建一个紧凑型的映射视图：
     `sparse_block_table = [物理块3, 物理块7, 物理块15, ..., 物理块98]`（长度仅为 30）。
   * 同时将序列有效长度告诉算子：`sparse_seq_len = 30 * block_size`。
3. **数学上的无缝自洽**：
   由于现代大模型的相对位置编码（RoPE，Rotary Position Embedding）在当初生成 Key 向量时就已经直接乘进张量数值内部了（Baked-in），即使中间抽掉了不重要的 Token 物理块，现有的 Attention 算子也能直接在该紧凑视图上正确运算！
   * **额外赠送收益**：由于进入显存的数据量削减了 $70\%$，**GPU Attention 算子自身的显存访存量和计算量也顺带降低了 $60\%\sim 70\%$，长序列的生成速度大幅加快**！

---

## 四、 SGLang / vLLM / Mooncake 重点代码修改位置与接口契约指导

### 4.1 核心数据结构与跨模块接口契约（C++ / Python 绑定层）

在 `mooncake-transfer-engine/include/kvc_overlap_sparse.h` 中统一定义以下三大核心接口契约：

```cpp
#pragma once
#include <cstdint>
#include <vector>

// 1. 由调度决策器 (M2-T05) 下发给本模块的混合执行计划
struct HybridLayerExecPlan {
    uint16_t k_star_layer;        // 前 K* 层执行本地零等待重算 (0 <= K* <= L)
    float sparse_ratio_budget;    // 稀疏保留比例 (0.25 ~ 1.0, 1.0 代表第一挡无损全量模式)
    uint8_t window_mode;          // 0 = 256MB 瞬态 Ping-Pong 窗 (算完即弃); 1 = 预分配 Slab 门控窗 (驻留转 Decode)
    uint32_t max_wqe_slice_kb;    // 单次 DMA 微切片上限 (默认 512KB，受 NXY 硬件 QoS 约束)
};

// 2. 由描述符编译模块 (M2-T03) 与本模块协同编译的单层边界描述符
struct LayerBoundaryDescriptor {
    uint16_t layer_id;            // 目标 Transformer 层号 (0 ~ 79)
    uint32_t num_selected_blocks; // 本层实际需要拉取的 Top-k 物理块数量
    uint64_t* src_cxl_addrs;      // 源端 CXL.mem 或远端存储池的物理/虚拟地址数组
    uint64_t dst_host_staging_va; // Host 侧 1GB 巨页连续 Staging 缓冲区虚拟地址
    uint64_t dst_hbm_base_addr;   // GPU/NPU 显存目标槽位地址 (或 Slab 物理地址)
    void* device_wait_event;      // 底层硬件同步事件句柄 (cudaEvent_t / aclrtEvent)
};

// 3. 本模块核心实现的跨流无锁事件同步器
class LayerStreamSynchronizer {
public:
    virtual ~LayerStreamSynchronizer() = default;
    // 在主计算流上插入对第 layer_id 层传输就绪事件的等待
    virtual void enqueue_layer_wait_on_compute_stream(int layer_id, void* compute_stream) = 0;
    // 当第 layer_id 层 Attention 计算完成时通知传输流释放/拉取后续层
    virtual void notify_layer_compute_done(int layer_id, void* compute_stream) = 0;
};
```

---

### 4.2 SGLang 框架重点代码修改位置分析

| 序号 | 目标文件路径（SGLang 仓库） | 定位类 / 函数 | 具体修改内容与逻辑指导 |
| :--- | :--- | :--- | :--- |
| **S1** | `python/sglang/srt/mem_cache/hicache_controller.py` | `HiCacheController`<br>`prefetch_from_storage()` | **【改造全量阻塞拉取为按层流式预取】**：<br>1. 废除原有的“等待 80 层全部拉完才返回”的阻塞式 `store.get_batch()`；<br>2. 接入 WXH 接口获取 `HybridLayerExecPlan`；<br>3. 跳过前 $K^*$ 层，仅将第 $K^*$ 和 $K^*+1$ 层描述符推入 C++ 滑动窗队列，立即标记请求就绪并放行进入 `ModelRunner`。 |
| **S2** | `python/sglang/srt/mem_cache/memory_pool.py` | `MHATokenToKVPool` / `MLATokenToKVPool` | **【开辟 256MB 瞬态双缓冲与 Slab 分配】**：<br>1. 新增 `self.ping_pong_kv_buffer`（形状为 `[2, 2, max_tokens, head_num, head_dim]`，单卡仅 $32\text{ MB}$）；<br>2. 新增 `get_kv_buffer_for_layer(layer_id, mode)` 接口，在模式 A 下循环交替返回 Slot 0 与 Slot 1。 |
| **S3** | `python/sglang/srt/models/*.py`<br>*(如 `llama.py`, `qwen2.py`)* | `DecoderLayer.forward()` | **【嵌入硬件跨流同步门锁与 $K^*$ 路由】**：<br>1. 若 `layer_id < K*`：强制执行本地 Full Prefill 重算逻辑，忽略远端缓存；<br>2. 若 `layer_id >= K*`：在调用 Attention 算子前插入 `layer_sync.enqueue_layer_wait_on_compute_stream(layer_id)`；<br>3. Attention 算子结束后立即调用 `layer_sync.notify_layer_compute_done(layer_id)` 唤醒后续层拉取。 |
| **S4** | `python/sglang/srt/managers/schedule_batch.py` | `ScheduleBatch.prepare_for_extend()` | **【支持前后两段双元数据准备】**：<br>在批处理元数据中准备两套前缀长度：前 $K^*$ 层前缀长度置 0（触发本地重算），后 $L-K^*$ 层前缀长度置为稀疏命中长度。 |

#### 【给 AI Agent 的精确实现 Prompt 模板（SGLang 改造）】
> [!TIP] **可以直接提供给 Agent 的实操指令**  
> ```text
> "请阅读 SGLang 仓库中的 python/sglang/srt/models/qwen2.py 与 radix_attention.py。
> 我们需要在 Qwen2DecoderLayer.forward() 中引入按层异步双缓冲同步支持：
> 1. 在 forward() 入口处接收外部传入的 layer_sync_handler 与 hybrid_plan 对象；
> 2. 当 self.layer_id < hybrid_plan.k_star_layer 时，绕过前缀缓存逻辑，执行全序列本地重算；
> 3. 当 self.layer_id >= hybrid_plan.k_star_layer 时，在 self.self_attn() 之前调用 
>    torch.cuda.current_stream().wait_event(layer_sync_handler.get_xfer_done_event(self.layer_id))；
> 4. 在 self.self_attn() 计算完成后，立即在当前 stream 上 record 该层的 compute_done_event。
> 请给出修改前后的代码 Diff，并保证不破坏没有开启该优化时的原有前向逻辑。"
> ```

---

### 4.3 vLLM（V1 架构）框架重点代码修改位置分析

| 序号 | 目标文件路径（vLLM 仓库） | 定位类 / 函数 | 具体修改内容与逻辑指导 |
| :--- | :--- | :--- | :--- |
| **V1** | `vllm/distributed/kv_transfer/kv_connector/v1/mooncake_connector.py` | `MooncakeConnectorV1`<br>`start_load_kv()` & `wait_for_layer_load()` | **【实现非阻塞按层滑动窗加载器】**：<br>1. `start_load_kv()` 仅下发前序滑动窗指令；<br>2. `wait_for_layer_load(layer_name)` 废除 Host 侧同步睡眠，下发底层 C++ `cudaStreamWaitEvent`，实现流水不卡顿。 |
| **V2** | `vllm/attention/layer.py` | `Attention.forward()` | **【稀疏 `block_table` 动态重映射】**：<br>检测到请求携带稀疏标志时，将传入的物理页表重定向为紧凑化后的 `sparse_block_table`，无缝适配底层 PagedAttention。 |
| **V3** | `vllm/v1/worker/gpu_model_runner.py` | `GPUModelRunner.execute_model()` | **【$K^*$ 混合批次状态切换】**：<br>在逐层前向循环中，动态维护 `is_recompute_layer` 状态标志，精准分流重算与缓存加载。 |

---

### 4.4 Mooncake Transfer Engine 与 CXL 稀疏聚簇层代码修改位置分析

| 序号 | 目标文件路径（Mooncake 仓库） | 定位类 / 函数 | 具体修改内容与逻辑指导 |
| :--- | :--- | :--- | :--- |
| **M1** | `mooncake-transfer-engine/src/cxl_sparse_gatherer.cpp`<br>*(新增核心源码)* | `CXLSparseGatherEngine`<br>`score_landmarks_neon()` & `gather_cxl_stream()` | **【落地 CXL.mem 流式聚簇与 NEON 汇编】**：<br>1. 通过 `/dev/dax` 或 $1\text{ GB}$ 巨页直接映射 CXL 内存物理地址；<br>2. 使用 NEON 向量点积在 $<50\mu\text{s}$ 内完成 Landmark 打分排序；<br>3. 编写 `ldnp`/`stnp` 汇编内联流式搬运，寄存器内直调 YY 的 `vzip` 解压。 |
| **M2** | `mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp` | `handleRemoteSparseGatherRPC()` | **【远端 Target-Side 聚簇与单包推送】**：<br>存储节点接收 $256\text{ B}$ 位图后，在本地 CXL 中流式聚簇，发起单次大包 `RDMA_WRITE_WITH_IMM` 回传。 |
| **M3** | `mooncake-store/src/offset_allocator.cpp` | `OffsetAllocator::allocate()` | **【元数据热区与 KVC 容量区分区】**：<br>划分前 $6\%$ 空间为 64B 对齐的 Landmark 热区，后 $94\%$ 为全量数据区。 |

---

## 五、 四周分步实施路线图与工程排期指导（W1 ~ W4）

严格对齐项目决战月进度节奏，主责研发团队按照以下四周脉冲节奏推进：

```
+---------------------------------------------------------------------------------------------------+
| W1: 协议冻结与微基准原型 |-->| W2: [第一挡达标] 全量按层无损掩盖 |-->| W3: [第二挡攻坚] CXL 稀疏聚簇 |-->| W4: 端到端消融对账结项 |
| • 冻结 C++ 头文件契约   |    | • 打通按层滑动窗流水线           |    | • 落地 Landmark 均值打分     |    | • E0~E4 五步消融实验    |
| • 跑通 CXL ldnp 微基准   |    | • 调通 K* 零等待混合执行         |    | • 跑通 NEON 流式聚簇与解压   |    | • SWE-bench 精度回归    |
| • 验证跨流 Event <3us   |    | • TTFT 实测降低 20%~25%         |    | • 跨网传输量削减 50%~75%    |    | • 输出终验对比报告      |
+---------------------------------------------------------------------------------------------------+
```

### 第 1 周（W1：11.02 ~ 11.08）—— 单点破局与跨模块契约冻结
* **核心任务**：
  1. 联合 WZX 与 WXH 冻结 4.1 节定义的头文件数据结构契约；
  2. 编写独立的微基准测试 `bench_cxl_cacheline_gather.cpp`：实测对比普通 `memcpy` vs. `ldnp/stnp` 流式聚簇对 CPU L3 缓存污染与带宽的影响；
  3. 搭建双流 `cudaEvent` / `aclrtEvent` 同步测试 Demo，测定跨流同步底层开销（确保 $<3\mu\text{s}$）。
* **W1 准出考核指标**：跨流 Event 同步时延 $<3\mu\text{s}$；CXL 流式聚簇单核吞吐 $>8\text{ GB/s}$，4 核并发吞吐 $>28\text{ GB/s}$，CPU L3 污染增量 $<2\%$。

### 第 2 周（W2：11.09 ~ 11.15）—— 【第一挡达标】全量无损按层计算-通信双缓冲掩盖实机打通
* **核心任务**：
  1. 改造 SGLang `HiCacheController` 与 vLLM `MooncakeConnectorV1`，将粗粒度阻塞拉取替换为逐层滑动窗拉取（$\alpha_{\text{sparse}} = 1.0$，全量无损）；
  2. 联调 WXH 的选路算法，跑通 **“前 $K^*$ 层零等待本地重算 $\parallel$ 后续层后台拉取”** 混合流水线；
  3. 利用 CPU 硬件周期计数器 `cntvct_el0` 导出计算与传输重叠的甘特图。
* **W2 准出考核指标**：在 $16\text{K}\sim 32\text{K}$ 上下文下，**计算-通信流水重叠率实测 $>85\%$**；固定物理带宽下 **端到端 TTFT 独立降低 $20\%\sim 25\%$**。

### 第 3 周（W3：11.16 ~ 11.22）—— 【第二挡攻坚】基于 CXL 内存语义的稀疏抽稀拉取上线
* **核心任务**：
  1. 在 KVCache 写回链路中嵌入 16-Token 块级 Landmark 均值提取并沉淀至热区；
  2. 启用 `CXLSparseGatherEngine`：层间 NEON 点积打分选出 Top-$k$（$25\%\sim 50\%$），执行非临时流式聚簇与寄存器内位平面解压；
  3. 在推理框架 Attention 层动态切换紧凑型 `sparse_block_table`。
* **W3 准出考核指标**：开启稀疏后，**网络/总线实际传输量削减 $50\%\sim 75\%$**；单层传输耗时缩减至计算耗时的 $1/2$ 以内（**重叠率逼近 $95\%+$**）；IO Die SMMU TLB Miss 较传统离散 DMA 下降 $>90\%$。

### 第 4 周（W4：11.23 ~ 11.29）—— 双挡位全链路消融对账与结项交付
* **核心任务**：
  1. 在 `HoloTestbed-4X` 环境上，跑完从 `E0`（原生基线）到 `E4`（第一挡 + 第二挡 CXL 稀疏终局）的严格控制变量消融实验；
  2. 跑通 `SWE-bench Verified` 代码任务回归，验证代码任务解决率波动 $<1.0\%$；
  3. 固化自动化测试脚本并输出最终结项评测报告。

---

## 六、 面向最终业务指标与硬件负载的测试验证与消融方案

### 6.1 测试环境与四大标准工作负载配置

* **硬件测试床基准**：4 节点集群，每节点配置 2 路鲲鹏 920 处理器、8 卡加速器（张量并行 TP=8），支持 PCIe 5.0 / CXL 2.0 内存扩展池，400G RoCE 网络。
* **四大基准评测套件**：
  1. **`Workload-1`（微基准与波形抓取 `test_layer_overlap_cxl_isolated.py`）**：固定 $32\text{K}$ 序列（命中 $28\text{K}$，未命中 $4\text{K}$），采集硬件时间戳绘制 80 层流水重叠甘特图；
  2. **`Workload-2`（长文本综合理解 `LongBench-32K/64K`）**：评估 $\alpha_{\text{sparse}} = 25\%$ 模式下的 TTFT 收益与回答准确率（F1 分数）；
  3. **`Workload-3`（代码智能体大任务 `SWE-bench Verified` 真实轨迹回放）**：验证自适应稀疏水位（$\alpha_{\text{sparse}} = 35\%\sim 50\%$）下代码修改 Patch 准确率与端到端任务耗时；
  4. **`Workload-4`（100G 大流量前后台混压压测 `Mixed-Prefill-Decode-Stress`）**：后台持续注入大容量 KVCache 拉取换出，前台并发跑 64 路在线 Decode，严格检验在线推理 TPOT 尾部时延稳定性。

---

### 6.2 业务级 E2E 指标与底层硬件遥测指标采集规范

| 指标分类 | 具体度量指标名称 | 采集工具 / 数据源 | 物理意义与考核门限 |
| :--- | :--- | :--- | :--- |
| **一、核心业务指标** | **1. E2E TTFT (P50 / P99)** | SGLang/vLLM Benchmark Client | 首 Token 时延：**第一挡独立降低 $20\%\sim 30\%$；叠加第二挡累计降低 $40\%\sim 55\%$** |
| | **2. 在线 Decode TPOT 劣化率** | Benchmark Client 实时统计 | 生成单字耗时：**混部压测下 TPOT 劣化率严格 $<3\%$；稀疏页表下 Decode 提速 $>25\%$** |
| | **3. 系统服务吞吐 (Goodput QPS)** | 满足 SLO 条件下的并发上限 | 消除 GPU 等待后：**第一挡 QPS 提升 $>15\%$；第二挡总吞吐提升 $35\%\sim 50\%$** |
| | **4. 业务任务精度保持率** | SWE-bench & LongBench 自动化评分 | 第一挡 **100% 比特级无损**；第二挡 **代码任务解决率波动 $<1.0\%$** |
| **二、软件流水指标** | **5. 计算-通信流水重叠率 ($\eta_{\text{overlap}}$)** | `cntvct_el0` 时钟 + 硬件 Event 时间轴 | 通信时间被计算掩盖的比例：**第一挡 $>85\%$；第二挡 $>95\%$** |
| | **6. HBM 显存峰值占用** | `torch.cuda.max_memory_allocated()` | 模式 A 临时缓冲从 **10GB 压降至 256MB（省 $97.5\%$）**；模式 B 显存节省 **$50\%\sim 70\%$** |
| **三、底层硬件负载** | **7. IO Die SMMU TLB Miss 率** | NXY 探针直读 `smmuv3_pmcg/tlb_miss` | 验证 CXL 绕过 SMMU 效果：**离散碎块查表 Miss 较传统 DMA 下降 $>90\%$** |
| | **8. PCIe Root Complex 载荷效率** | `hisi_pcie/tx_mwr_flux` 遥测 | 消除离散 TLP 小包后：**PCIe 有效载荷效率从 $<45\%$ 跃升至 $>92\%$** |
| | **9. CPU 核心利用率与 L3 污染** | `armv8_pmuv3/ll_cache_miss_rd` | 验证 `ldnp` 非临时流式访存效果：**聚簇仅占 $\le 4$ 个 CPU 核，L3 Cache Miss 增幅 $<2\%$** |

---

### 6.3 五步正交增量消融实验矩阵（Ablation Study Matrix）

为了严谨剥离每个机制的独立贡献，按照以下 `E0 -> E4` 阶梯逐级对账（测试基准：32K 上下文，28K 命中，4K 未命中，底层单通道可用带宽限制在 $15\text{ GB/s}$）：

| 实验代号 | 启用的技术组合配置 | 跨介质实际传输量 | 预期流水重叠率 | 预期 E2E TTFT (32K) | 预期并发 Decode TPOT | IO Die SMMU 与 PCIe 状态 | 核心工程归因结论 |
| :--- | :--- | :---: | :---: | :---: | :---: | :--- | :--- |
| **`E0`（原生基线）** | SGLang/vLLM 原生粗粒度整批阻塞拉取（无按层掩盖，无稀疏） | $100\%$ ($8.75\text{ GB}$) | $0\%$ (纯串行) | $\sim 775\text{ ms}$ | $38.5\text{ ms}$ (受突发流量冲击) | 基准值，大块数据突发打满 PCIe 队列 | 通信耗时 $100\%$ 暴露在关键路径上，GPU 严重空转。 |
| **`E1`（第一挡基础）** | `E0` + **2-Layer HBM 双缓冲按层异步掩盖** + 512KB 微切片流控 | $100\%$ ($8.75\text{ GB}$) | $\sim 78\%$ | $\sim 605\text{ ms}$<br>**($-21.9\%$)** | $29.2\text{ ms}$<br>*(干扰率 $<3\%$)* | 流量被平摊，PCIe 突发拥塞消除 | 中间层传输被计算掩盖，但存在首层冷启动等待气泡。 |
| **`E2`（第一挡完整）** | `E1` + **前 $K^*$ 层零等待本地重算混合流水**（第一挡完全体，100% 无损） | $85\%$ ($7.4\text{ GB}$)<br>*(跳过前 $K^*=12$ 层拉取)* | **$>86\%$** | **$\sim 535\text{ ms}$**<br>**($-31.0\%$)** | $29.0\text{ ms}$ | 首层 $0\mu\text{s}$ 等待，流水线全程无气泡 | **第一挡 100% 无损目标达成**：彻底消灭首层冷启动气泡，独立斩获 $31\%$ TTFT 收益！ |
| **`E3`（反面对照组）** | `E2` + **传统离散 DMA 稀疏拉取**（抽稀 30%，但**不走** CXL 聚簇） | $30\%$ ($2.6\text{ GB}$)<br>*(拆分为 64000 个碎块)* | $\sim 62\%$<br>*(传输变慢导致退化)* | $\sim 640\text{ ms}$<br>*(发生反优化劣化!)* | $45.0\text{ ms}$<br>*(总线严重拥塞)* | **SMMU TLB Miss 暴增 $14\times$，PCIe 效率跌至 $38\%$** | 实证复现 `SAC` 论文揭示的 **“稀疏小包风暴悖论”**，证明裸离散 DMA 不可行！ |
| **`E4`（第二挡终局）** | `E2` + **基于 `CXL.mem` 流式聚簇（+寄存器解压）的 Top-30% 稀疏抽稀引擎** | **$16.2\%$ ($1.42\text{ GB}$)**<br>*(30% 稀疏 $\times$ 54% 压缩，聚簇为每层 1 大块)* | **$>96\%$** | **$\sim 310\text{ ms}$**<br>**(较 `E0` 暴降 $-60.0\%$, 较 `E2` 再降 $-42.1\%$)** | **$21.5\text{ ms}$**<br>**(动态稀疏页表使 Decode 提速 $+25.8\%$)** | **SMMU Miss 较 `E3` 骤降 $94\%$，PCIe 效率 $>93\%$，L3 零污染** | **第二挡终局全面达成**：CXL.mem 彻底消灭离散小包风暴，单层传输耗时彻底融入计算阴影！ |

---

### 6.4 自动化回归验证脚本执行流

在测试环境中提供一键式测试脚本 `scripts/run_m2_f01_ablation_suite.sh`，通过动态设置环境变量完成五组实验的无缝轮转：
```bash
#!/bin/bash
set -e

echo "=== [Step 1] 拉起后台硬件 PMU 遥测守护进程 ==="
python3 tools/telemetry_daemon.py --mmap-pmu --interval-us=500 --output=pmu_trace.bin &
TELEMETRY_PID=$!

echo "=== [Step 2] 跑批五档正交消融实验 ==="
# 1. 跑 E0: 原生粗粒度基线
export MC_OVERLAP_ENABLE=0 MC_SPARSE_MODE=0
python3 benchmark/bench_serving.py --workload=workload_1 --output=result_e0.json

# 2. 跑 E1: 仅开启按层滑动窗掩盖
export MC_OVERLAP_ENABLE=1 MC_KSTAR_HYBRID=0 MC_SPARSE_MODE=0
python3 benchmark/bench_serving.py --workload=workload_1 --output=result_e1.json

# 3. 跑 E2: 第一挡完全体 (按层掩盖 + K* 零等待混合重算)
export MC_OVERLAP_ENABLE=1 MC_KSTAR_HYBRID=1 MC_SPARSE_MODE=0
python3 benchmark/bench_serving.py --workload=workload_1 --output=result_e2.json

# 4. 跑 E3: 反面对照组 (传统裸离散 DMA 稀疏)
export MC_OVERLAP_ENABLE=1 MC_KSTAR_HYBRID=1 MC_SPARSE_MODE=1 MC_CXL_GATHER=0
python3 benchmark/bench_serving.py --workload=workload_1 --output=result_e3.json

# 5. 跑 E4: 第二挡终局体 (CXL.mem 流式聚簇稀疏抽稀)
export MC_OVERLAP_ENABLE=1 MC_KSTAR_HYBRID=1 MC_SPARSE_MODE=1 MC_CXL_GATHER=1
python3 benchmark/bench_serving.py --workload=workload_1 --output=result_e4.json

echo "=== [Step 3] 停止遥测并生成甘特图与对账报告 ==="
kill -SIGINT $TELEMETRY_PID
python3 tools/generate_m2_f01_report.py \
    --pmu-trace=pmu_trace.bin \
    --results=result_e0.json,result_e1.json,result_e2.json,result_e3.json,result_e4.json \
    --output-dir=./reports/m2_f01_final/
echo "=== 评测完成！请查阅 reports/m2_f01_final/ 中的甘特图与消融汇总表 ==="
```
