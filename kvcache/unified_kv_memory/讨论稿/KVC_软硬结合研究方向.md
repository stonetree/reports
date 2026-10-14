# you asked

message time: 2026-10-13 11:24:05

你先试着将我们之前讨论的论文，再结合看还有没有补充，构建一张 **“业务场景 → 物理瓶颈 → physicalization mechanism → 微观指标 → TTFT/TPOT/goodput/cost”** 证据矩阵。

---

# chatgpt response

我会把这张矩阵做成“可用于技术决策”的版本，而不是文献综述：每篇论文只保留 **场景、真正的物理瓶颈、physicalization 手段、微观收益、端到端 KPI、证据等级**，并把“高收益但只在特定 operating point 下成立”的情况单独标出来。先补查一轮 arXiv/正式论文，重点找我们之前漏掉的 `layout / access granularity / PIM / CXL / HBF / SSD / CPU-GPU co-attention / shared-prefix` 工作。

补查后我会把几类“证据强、而且能直接落到业务指标”的工作加入主矩阵：**Strata、Tutti、Unified KV Pooling、ObjectCache、DirectKV、Beluga、DualPath、ECHO**。它们很有价值，因为很多都不是只报 memory-bandwidth，而是直接报 **TTFT、SLO 下 request rate、throughput、cost**；这正好能检验 physicalization 是否真的穿透到业务层。

矩阵基本收敛了。一个明显的分层是：**真正端到端收益最扎实的，集中在“KV restore/reuse 已经上关键路径”的场景**——长上下文 prefix reuse、Agent 多轮、高并发 sparse attention、SSD/CXL 外部层；而 **HBF/PIM/复杂物理布局** 的潜在收益很大，但当前证据更多还是体系结构模拟或特定硬件原型。下面我会把这两类证据严格分开，不把 10× 的仿真结果和 production 里的 20% 混在一起。

可以。把我们此前讨论过的工作与这一轮补扫的 2026 论文合并后，我认为已经能形成一张相当有用的 **KV Physicalization → 业务 KPI 证据矩阵**。

我这里刻意不按论文 venue 排，而按真正的业务链路排。证据等级定义为：

- **A**：正式顶会/期刊 + 真实硬件 + 端到端 serving，部分已有生产部署。
- **B**：真实 prototype / serving integration，有端到端结果，但生产证据有限。
- **C**：体系结构建模、仿真或未来硬件假设为主，数字不能与 A/B 等价比较。

---

## 1. KV Physicalization → 业务指标证据矩阵

| 业务场景 | 代表工作 | 真正的物理瓶颈 | Physicalization mechanism | 微观指标/直接改变 | 最外层 KPI | 证据 | 我的判断 |
|---|---|---|---|---|---|---|---|
| **生产级长上下文 / Prefix reuse** | **Mooncake** | KV 工作集超 HBM；DRAM/SSD/RDMA 分散，位置/带宽成为调度约束 | KV block 对象化；VRAM/DRAM/SSD 分层；多 NIC + topology-aware transfer；cache-aware scheduler | 提高 KV reuse、聚合存储/网络带宽 | real traces 下 SLO 内有效 request capacity **+59%~498%**；生产 A800/H800 分别支持约 **+115%/+107% requests** ([MADSys](https://madsys.cs.tsinghua.edu.cn/publication/mooncake-a-kvcache-centric-disaggregated-architecture-for-llm-serving/?utm_source=chatgpt.com)) | **A+** | 最重要的 production anchor：证明“KV 变成独立 memory hierarchy”本身已有业务价值 |
| **长上下文 hierarchical cache** | **Strata / OSDI'26** | GPU layer-first layout 导致跨 tier 搬运碎片化，小传输吃不满链路；load 阻塞 prefill | **GPU compute layout 与 transfer layout 解耦**；GPU-assisted on-the-fly relayout；layer-wise loading + cache-aware scheduling | 小碎片 I/O → 大传输；I/O 与 prefill/decoding overlap | throughput 最多 **5× vs vLLM-LMCache，3.75× vs TensorRT-LLM**；已部署 production ([USENIX](https://www.usenix.org/conference/osdi26/presentation/xie-zhiqiang?utm_source=chatgpt.com)) | **A+** | **目前最强的 physicalization 证据之一**：layout 不是内部实现细节，直接决定 hierarchical cache 能不能兑现 |
| **HOST DRAM 作为 GPU KV 扩展** | **DirectKV / OSDI'26** | CPU KV 必须 staging 到 HBM；多一次 buffer + 多一次搬运 | GPU kernel **直接访问 CPU-pinned KV**；CPU-memory-aware tiling；SMEM staging；projection-attention fusion | CPU↔GPU transfer volume **-50%**；GPU memory **-43%** | E2E 最多 **1.2×** ([USENIX](https://www.usenix.org/conference/osdi26/presentation/luo?utm_source=chatgpt.com)) | **A** | 很重要的反例：微观变化很大，E2E 只有约20%；说明 physicalization 必须看 Amdahl |
| **Native sparse attention + HOST offload** | **ECHO / OSDI'26** | sparse attention 算得少，但完整 KV 仍占 HBM；host miss recall 落在每层 critical path | Host full KV + GPU sparse working set；GPU-resident metadata；UVA direct access；prefetch 与 indexer fuse | KV recall latency 被 indexer computation overlap；HBM residency 降低 | long-context generation throughput 最多 **2.1×**；轻载 latency 接近 baseline ([USENIX](https://www.usenix.org/conference/osdi26/presentation/liu-guangda?utm_source=chatgpt.com)) | **A** | 核心收益不是“CPU 快”，而是 **HBM capacity unlocking → batch/concurrency↑** |
| **低内存 PC / SSD active KV** | **SolidAttention / FAST'26** | Sparse KV 在 SSD 上形成 tiny/random IO；逻辑 sparsity 无法转化为 SSD BW | 多 KV 合并成 coarse block；KV consolidation；speculative prefetch；SSD-aware compute/I/O orchestration | KV DRAM footprint 最多 **-98%**；显著减少 SSD blocking | 128K inference speed 最多 **3.1×**，保持精度 ([USENIX](https://www.usenix.org/conference/fast26/presentation/zheng?utm_source=chatgpt.com)) | **A** | 第一个非常清楚证明：**Logical access granularity ≠ Storage access granularity** |
| **SSD-backed production KV store** | **Tutti** | Paged KV → 海量 fragmented SSD IO；即便 GDS，每个 IO 仍有 CPU control overhead | GPU-native KV object；GPU io_uring；预映射物理 storage descriptor；GPU→NVMe 直接 control/data path | SSD bandwidth 接近饱和；GPU stalls 接近 0 | strict SLO 下 **TTFT -78.3%**；request rate **2×**；serving cost **-27%** ([arXiv](https://arxiv.org/abs/2605.03375?utm_source=chatgpt.com)) | **B+** | 对 HOST 很重要：证明 CPU 从数据/control hot path 退出本身可以形成业务收益 |
| **HOST DRAM + 多 SSD pooling** | **Unified KV Pooling** | 单 memory module / SSD 串行 restore；filesystem metadata/path 占大量时间 | KV 跨 memory/SSD 按 BW striping；直接记录 LBA；SPDK bypass filesystem；并行 retrieve | blocked I/O 最多约 **23.2×** 降低；去掉通用 FS 路径 | 128K TTFT 最多约 **4.1×** 改善 ([alphaXiv](https://www.alphaxiv.org/abs/2606.14779v1?utm_source=chatgpt.com)) | **B** | 很强的 HOST/OS 证据：这里瓶颈甚至不是介质，而是 **generic OS I/O semantics 不适合 KV** |
| **对象存储作为 KV runtime tier** | **ObjectCache** | KV 的 storage object order 与 GPU layer consumption order 不一致；远端传输无法 overlap | chunkwise persistent layout + **layerwise delivery view**；server-side gather；RDMA direct delivery | storage layout 与 delivery layout 解耦；transfer 被 layer compute 隐藏 | 64K context TTFT 仅比优化 local DRAM 高 **≤5.6%**；带宽受限时 added TTFT 改善 **1.2–1.8×** ([arXiv](https://arxiv.org/abs/2605.22850?utm_source=chatgpt.com)) | **B** | 又一次证明：**storage format 和 consumption format 不应强绑定** |
| **CXL shared KV pool / dense reuse** | **Beluga / SIGMOD/PACMMOD'26** | RDMA remote KV 需要 message protocol、同步、locality-aware scheduling；remote 和 local 是两个语义 | CXL switch global address space；load/store semantics；DAX mmap；software coherence；device interleaving | remote KV 从 message transfer 变成 memory access | 对 RDMA baseline：**TTFT -89.6%**、throughput **7.35×** ([arXiv](https://arxiv.org/abs/2511.20172?utm_source=chatgpt.com)) | **A-/B+** | 对 HOST 架构极关键：**Access semantics 本身改变系统架构** |
| **Sparse attention + disaggregated KV** | **SAC** | RDMA 必须 bulk/full-prefix fetch，而 sparse attention 实际只消费 Top-k KV | CXL global address space；cache-line load/store；layer-first layout；GPU coalesced direct load Top-k | Full-prefix transfer → fine-grained selected-KV access；消除本地完整副本 | vs RDMA：throughput **2.1×**；TTFT **9.7× lower**；TBT **1.8× lower** ([alphaXiv](https://www.alphaxiv.org/abs/2606.19746v1?utm_source=chatgpt.com)) | **B+** | **最直接回答“access semantic 是否影响业务 KPI”的论文之一** |
| **Agent multi-turn / SSD-backed CXL-HM** | **HyMCache** | TB级 prefix state 放不起 DRAM；SSD latency 又无法直接进入请求 critical path | SSD-backed CXL memory + bounded device DRAM **staging window**；prefix-aware prefetch；request-level NUMA placement | DRAM 不再是 capacity cache，而是 latency-hiding streaming window | 同 DRAM budget：single-node **3.0× vs LMCache**，PD **1.45×**；比 1TB Mooncake 慢约30%，但 DRAM **少16×** ([arXiv](https://arxiv.org/abs/2607.18141?utm_source=chatgpt.com)) | **B** | 这是典型 **cost ↔ latency ↔ physical staging** co-design |
| **Agent shared long prefix，HBM 内部** | **GraniKV** | prefix 希望 contiguous，private suffix 希望 token-level allocation；统一 PagedAttention granularity 同时不适合两者 | contiguous HOT prefix pool + token-granular COLD suffix pool；layout-aware backend dispatch | layout 独立贡献约 **1.05–1.15×**；但使 fat-GEMM/cascade backend 成为可能 | 16K prefix throughput **1.57–2.16×**；异构多 prefix 场景 **1.95×** ([arXiv](https://arxiv.org/abs/2608.15584?utm_source=chatgpt.com)) | **B** | 很有价值：明确区分 **direct optimization value** 与 **enablement value** |
| **CPU–GPU collaborative MoE long-context** | **InplaceKVCache / WriteScope** | CPU-resident KV：GPU transfer-friendly format 和 CPU inplace-compute format 冲突；切 execution owner 要重新搬数据 | 按 **device affinity × access pattern** 做 four-region physical layout；write-time residency 固定，runtime 只改 scheduling | CPU/GPU workload split 不再触发 KV relocation | ≥8K：A100 **1.5–2.5×**；V100 **1.4–1.7×**；支持 aggregate 1M-token inference ([arXiv](https://arxiv.org/abs/2609.14507?utm_source=chatgpt.com)) | **B** | 这篇对 CPU 最直接：**Physical format 可以编码 execution affinity** |
| **Commodity PIM production KV lifecycle** | **PATTON** | PIM GEMV 最优 layout、single-token KV append 最优 layout、capacity-efficient allocation 三者冲突 | hierarchical physical granules；K/V 分别布局；Value **Commit Zone** 从 write-friendly 转换成 GEMV-friendly | GEMV、append write、capacity 三方折中 | attention + runtime-induced prefill recompute 平均 **1.95×**；energy efficiency **4.83×** ([arXiv](https://arxiv.org/abs/2609.11392?utm_source=chatgpt.com)) | **C+/B-** | 体系结构意义很强，但暂时不能等价于生产 goodput |
| **HBF + sparse long-context** | **SPLASH** | Sparse token selection 遇到 NAND page granularity + plane imbalance：少选 token 不等于少读 flash | Logical KV page→physical flash page；plane striping；plane-balanced selection；HBM window + HBF history | 降低 read amplification 和 serialized flash rounds | 在 **100ms/token SLO** 下 decode throughput/GPU **3.5–11.4×**；精度差异 ≤4% ([arXiv](https://arxiv.org/abs/2609.23816?utm_source=chatgpt.com)) | **C** | 非常重要的未来 SoC 方向，但硬件仍是假设性的 |
| **Full-HBF KV** | **HBFlex** | KV fine-grained reads → plane imbalance；incremental writes 阻塞 read；mixed lifetime → GC amplification | Logical page / physical stripe / work tile / erase block **四粒度解耦**；placement-aware CTA scheduling；windowed writeback；lifetime packing | plane utilization↑、write/read interference↓、GC migration↓ | trace-driven simulation：最多 **1.58× vs FlashAccel、3.30× vs H3** ([ArcXiv](https://arcxiv.org/abs/2609.18675?utm_source=chatgpt.com)) | **C** | 我认为理论抽象价值甚至高于数字本身 |
| **Agentic PD + external KV storage** | **DualPath / SIGCOMM'26** | Prefill storage NIC 饱和，而 Decode storage NIC 空闲；物理 I/O 路径不匹配资源拓扑 | storage→prefill + storage→decode→prefill **双物理路径**；NIC-aware scheduling/traffic isolation | aggregate storage/network BW 得到利用 | offline throughput **最高1.87×**；online SLO serving throughput 平均 **1.96×** ([arXiv](https://arxiv.org/abs/2602.21548?utm_source=chatgpt.com)) | **A** | Physicalization 不只等于 memory layout，**network path topology 也是 KV physical access** |
| **Multi-turn interactive KV on DRAM+SSD** | **Bidaw / FAST'26** | compute scheduler 不知道 KV tier/size；storage replacement 不知道未来 session reuse | compute→storage 和 storage→compute 双向感知；tier-aware scheduling + reuse-aware placement | host hit↑、blocking↓、无效 cached bytes↓ | response latency 最多改善 **3.58×**；throughput **1.83×** ([USENIX](https://www.usenix.org/conference/fast26/presentation/hu-shipeng?utm_source=chatgpt.com)) | **A** | 不是最底层的 layout paper，但证明 **physical location 必须暴露给 scheduler** |

---

# 2. 把矩阵横向看，真正能穿透到业务层的其实是四条链

这比逐篇看论文更重要。

## 链路 A：`Physical layout → effective bandwidth → TTFT`

这是目前**证据最扎实**的一条。

典型：

$$
\text{fragmented KV}
\rightarrow
\text{tiny IO}
\rightarrow
BW_{effective}\ll BW_{peak}
\rightarrow
KV\ restore\ stall
\rightarrow
TTFT\uparrow
$$

对应：

- Strata；
- SolidAttention；
- Tutti；
- Unified KV Pooling；
- ObjectCache。

特别是 Strata 和 Tutti 已经说明：

> 介质峰值 bandwidth 根本不是主要问题。**你用什么 layout、什么 I/O transaction、谁 submit I/O、什么时候送达 GPU，决定的才是业务看到的 bandwidth。**

这条线对 HOST 最现实。

---

# 3. 链路 B：`Access semantics → bytes moved → TTFT/TBT`

SAC / Beluga 是最强证据。

RDMA 模型接近：

$$
Object
\rightarrow
Transfer
\rightarrow
Local\ Buffer
\rightarrow
Consume
$$

CXL 模型可以变成：

$$
Address
\rightarrow
Load
\rightarrow
Consume
$$

在 dense prefix reuse 中，这减少 control/synchronization overhead；

在 sparse attention 中意义更大，因为：

$$
Logical\ TopK
$$

终于可以变成：

$$
Physical\ TopK\ Reads
$$

而不是：

$$
Logical\ TopK
+
Physical\ FullPrefixFetch
$$

SAC 的 **2.1× throughput、9.7× TTFT、1.8× TBT** 是非常强的证据。([alphaXiv](https://www.alphaxiv.org/abs/2606.19746v1?utm_source=chatgpt.com))

这说明一个非常关键的结论：

> **KV 的访问协议本身已经成为 architecture choice，而不只是 communication library choice。**

---

# 4. 链路 C：`Physical capacity → operating point → Goodput`

这个最容易被忽略。

例如 ECHO、Mooncake、HyMCache、InplaceKVCache 的巨大价值，并不主要来自：

$$
T_{one\ request}\downarrow
$$

而是：

$$
Batch_{\max}\uparrow
$$

$$
Concurrency_{\max}\uparrow
$$

$$
Context_{\max}\uparrow
$$

结果：

$$
GPU\ utilization\uparrow
$$

最终：

$$
SLO\ Goodput\uparrow
$$

Mooncake 是最明显的 production 证据：它优化的不是一个 kernel 的 TPOT，而是**在满足 SLO 的情况下能接多少请求**。([MADSys](https://madsys.cs.tsinghua.edu.cn/publication/mooncake-a-kvcache-centric-disaggregated-architecture-for-llm-serving/?utm_source=chatgpt.com))

这类收益实际上比：

> TPOT -5%

重要得多。

因为它改变了整个系统 operating point。

---

# 5. 链路 D：`Physical layout → enable new execution mode → 间接 E2E 收益`

这是 GraniKV、PATTON、InplaceKVCache 最有意思的地方。

例如 GraniKV：

physical layout 自身：

$$
\sim1.05-1.15\times
$$

并不惊人。

但：

$$
Contiguous\ Prefix
$$

使得：

$$
Fat\ GEMM / Cascade
$$

成为可能，最终：

$$
\sim2\times
$$

throughput。([arXiv](https://arxiv.org/abs/2608.15584?utm_source=chatgpt.com))

所以对于 HOST/SoC 新特性，不能只问：

> 这个硬件 primitive 本身能快多少？

还应该问：

$$
\boxed{
Does\ it\ enable\ a\ fundamentally\ better\ execution\ mode?
}
$$

这个问题非常重要。

---

# 6. 反过来，也能看到哪些 physicalization 研究现在还“不够硬”

有三类需要警惕。

### 第一类：只改善 micro metric，没有改变 E2E bottleneck

DirectKV 就很典型：

$$
Transfer\ bytes -50\%
$$

$$
HBM\ usage -43\%
$$

但：

$$
E2E\le1.2\times
$$

这不是论文不好，而是说明：

> CPU↔GPU movement 只是整个请求的一部分。([USENIX](https://www.usenix.org/conference/osdi26/presentation/luo?utm_source=chatgpt.com))

所以如果我们未来做 CPU/SoC 优化，绝不能只拿：

> memcpy bandwidth +40%

证明价值。

---

### 第二类：巨大 speedup 主要来自“baseline 撞容量墙”

例如一些 PIM/HBF 工作可以出现：

$$
5\times,\ 10\times,\ 20\times
$$

但很大部分原因是：

$$
Baseline\ Batch=small
$$

而新系统：

$$
Batch\gg baseline
$$

这是合理的**enablement gain**，但不能说：

> 某个 address-mapping 技术本身提升 20×。

应该拆成：

$$
Physicalization
\rightarrow
Capacity\ unlock
\rightarrow
Batch\uparrow
\rightarrow
Throughput\uparrow
$$

而不是把所有 gain 都归给 layout。

---

### 第三类：未来介质 + 仿真结果

SPLASH / HBFlex 很有思想价值，但当前最大的价值是：

> 告诉我们未来 HBF 需要什么 architecture contract。

而不是：

> HBF 已经证明能给生产 vLLM 11×。

后者目前不能这么说。

---

# 7. 从这张矩阵，我认为出现了一个非常清晰的“收益密度排序”

如果按 **“现在值得 HOST 侧投入”** 而不是论文 novelty 排，我会这样判断：

| 研究方向 | 当前业务证据 | HOST 架构相关性 | 我建议的优先级 |
|---|---:|---:|---:|
| **KV restore path / I/O fast path** | 极强 | 极强 | **P0** |
| **Compute-layout / Transfer-layout 解耦** | 极强 | 极强 | **P0** |
| **CXL memory-semantic KV access** | 强 | 极强 | **P0** |
| **NUMA / bandwidth-aware KV placement** | 强 | 极强 | **P0/P1** |
| **GPU↔HOST zero-copy/direct access** | 中强 | 极强 | **P1** |
| **Storage/NIC topology-aware KV path** | 强 | 强 | **P1** |
| **CPU/GPU execution-affinity KV format** | 中 | 极强 | **P1** |
| **PIM-specific physical layout** | 中/模拟 | 强 | **P2 research** |
| **HBF plane/page/lifetime physicalization** | 模拟为主 | 极强 | **P2 strategic** |
| **纯 placement policy/LRU 变体** | 较弱 | 中 | **P3** |

这里我最关注前三个。

---

# 8. 对我们现在研究 HOST CPU/SoC 的方向，真正值得提炼的是这个模型

以前我们可能把 KV tier 描述成：

$$
HBM\rightarrow DDR\rightarrow CXL\rightarrow SSD
$$

现在这张矩阵说明，这个模型太粗。

应该变成：

$$
\boxed{
Logical\ KV
\xrightarrow{\text{representation}}
Physical\ Layout
\xrightarrow{\text{placement}}
Physical\ Medium
\xrightarrow{\text{access semantics}}
Data\ Path
\xrightarrow{\text{execution}}
Consumer
}
$$

其中每一个箭头都可能直接影响业务 KPI。

例如：

### Strata

$$
Layer\text{-}first
\rightarrow
Page\text{-}first\ transfer
\rightarrow
larger\ DMA
\rightarrow
TTFT/Throughput
$$

### SAC

$$
Sparse\ TopK
\rightarrow
cache-line\ load/store
\rightarrow
no\ full-prefix\ fetch
\rightarrow
TTFT/TBT
$$

### Tutti

$$
Paged\ KV
\rightarrow
GPU-native\ storage\ object
\rightarrow
GPU-submitted\ NVMe
\rightarrow
TTFT/Cost
$$

### HBFlex

$$
Logical\ page
\rightarrow
physical\ stripe
\rightarrow
plane-aware\ work\ tile
\rightarrow
HBF\ effective\ BW
$$

所以我认为我们之前说的 **KV Physicalization Layer** 可以进一步收敛成：

$$
\boxed{
\textbf{Representation + Placement + Access Contract}
}
$$

而不是仅仅叫 Placement。

---

# 9. 一个更重要的发现：当前证据已经足以证明这不是“隔靴搔痒”

如果只看一篇：

> “layout 优化 15%”

确实很容易质疑价值。

但横向看 Strata、SAC、Tutti、Beluga、SolidAttention、DualPath 后，可以看到一条相当一致的证据链：

$$
\text{错误的 physical representation/access}
$$

会造成：

$$
\text{I/O amplification}
+
\text{bandwidth underutilization}
+
\text{extra staging}
+
\text{serialization}
+
\text{queueing}
+
\text{capacity loss}
$$

最终已经反复出现：

$$
TTFT
$$

数倍下降、

$$
Throughput/Goodput
$$

1.5–5× 甚至更高，以及真实：

$$
Cost/request
$$

改善。

所以这条研究线我现在会给出比之前更高的评价：

> **KV physicalization 不是一个局部的 memory-layout 优化点，而是在 KV 从 GPU-local buffer 演化为 heterogeneous/disaggregated state 后，连接“上层 KV semantics”和“底层 HOST/SoC/memory/storage fabric”的关键接口。**

而且真正最成熟的部分，恰恰不是遥远的 HBF/PIM，而是已经出现在今天 HOST 上的：

$$
\boxed{
DDR/CXL/NVMe/NIC
+
NUMA
+
DMA
+
I/O\ stack
+
address\ mapping
}
$$

这一层。

这对我们后续找 **CPU/SoC 级真实创新点** 很关键：下一步应该把上面矩阵再转成一张 **“Physicalization primitive → 需要的 HOST/SoC 硬件能力 → 哪些已有、哪些缺失 → 可验证业务场景”** 的矩阵。那张表才会真正开始回答：**CPU/SoC 到底应该新增什么，而不是软件再写一层优化。**