> From: https://chatgpt.com/c/6aab83f3-e398-83ea-a60a-37e59edbee5a

# you asked

message time: 2026-09-17 14:09:41

根据2026年OSDI/NSDI/SIGCOMM/SIGMOD/FAST 等顶会中关于AI Infra的研究论文，给出一份关于KVCache的高质量技术发展趋势洞察报告，覆盖： 
* KVCache在当前在线推理中扮演角色的转变以及趋势，这些角色和趋势如何影响未来的在线推理流程； 
* KVCache在当前AI infra建设和运维中，承担了哪些关键职责，以及发展趋势，这些角色和趋势如何影响未来的在线推理流程； 
* 在当前建设高效AI infra目标下，围绕KVCache课题，都面临哪些核心问题和关键挑战，解决这些问题和挑战的目标是什么； 
* 当前都采用了哪些技术手段来解决上述问题和挑战； 
* 除了学术界，工业界围绕KVCache这个问题，目前都形成了哪些共识？INTEL/AMD/ARM/NVIDIA等芯片厂商都采取了哪些具体的动作。 
* 展开进一步推演，KVCache和LLM推理框架、记忆系统、agent等一起，未来将发展怎样的化学反应，进而对LLM的推理范式产生怎样的影响。

---

# chatgpt response

我会把这份报告按“论文证据 → 技术抽象 → 工业共识 → 推演”四层来组织。重点不会只罗列论文，而是回答一个更核心的问题：**KVCache正在从“模型运行时里的临时张量”演化成“AI Infra 的一级共享状态与数据平面”**，以及这会怎样重构在线推理。

目前论文线索已经出现了一个很清晰的分化：**OSDI/FAST 更关注“KV 怎么存、怎么搬、怎么预取”；NSDI/SIGCOMM 更关注“KV 一旦跨节点，就如何改变调度与网络”；SIGMOD/PACMMOD 则开始把 KV 当成可独立管理的数据对象，研究跨层存储、索引和复用语义。**  
下一步我会特别核对工业界动作，区分“厂商明确以 KVCache 为目标的产品/软件”与“只是底层 CXL/互联能力可被 KVCache 使用”，避免把相关性误写成厂商战略。

有两个值得强调的证据正在把结论进一步推深。第一，SIGMOD’26 的 **Beluga** 已经不是“把 KV 卸载到 CXL”这么简单，而是尝试用 CXL switch 把 KV 做成 GPU/CPU 可共同访问的共享内存池；第二，SIGCOMM’26 同一场 LLM Serving session 同时出现 **KVServe、DualPath、KVCodec**，说明研究焦点已经从“缓存命中率”明显转向 **KV 作为网络/存储 payload 后的通信效率、路径选择与资源调度**。

# 2026 年 KVCache 技术发展趋势洞察报告  
**研究范围：OSDI'26 / NSDI'26 / FAST'26 / SIGCOMM'26 / SIGMOD'26，以及 NVIDIA、AMD、Intel、Arm 2026 年公开技术路线**  
**信息截点：2026 年 9 月 17 日**

## 一、先给结论：2026 年真正发生的变化，不是“KVCache 怎么做得更大”，而是 KVCache 的系统身份发生了变化

综合 OSDI'26、NSDI'26、FAST'26、SIGCOMM'26、SIGMOD'26 的论文，我认为最重要的趋势可以压缩成一句话：

> **KVCache 正从 LLM Runtime 内部、request-local 的 GPU 临时张量，演化为 AI Infra 中可命名、可查询、可迁移、可分层、可调度、可共享的“派生执行状态（derived execution state）”。**

这个判断比“KVCache Offload”“Prefix Cache”“PD 分离”更本质。

2026 年五个顶会呈现出的研究重心已经非常一致：

| 顶会 | 2026 年代表工作 | 研究问题已经演变为什么 |
|---|---|---|
| OSDI | Strata、ECHO、DirectKV | KV 如何跨 HBM/DRAM/SSD 存放、预取、直接访问，并与 Scheduler 协同 |
| NSDI | SYMPHONY、DroidSpeak、Libra | KV 如何成为**可迁移的分布式执行状态**，从而解除 request 与某一 GPU 的绑定 |
| FAST | CacheSlide、Bidaw、SolidAttention | KV 如何与 SSD/存储系统、访问预测、位置语义协同 |
| SIGCOMM | KVServe、DualPath、KVCodec、Turbo | KV 一旦成为跨节点 payload，网络路径、压缩、拥塞和 NIC 带宽怎样进入推理关键路径 |
| SIGMOD | Beluga、FusionRAG、KVDrive、AlignedServe | KV 如何成为可管理的 data object，并进一步与 CXL、RAG、分层存储和查询/调度融合 |

OSDI'26 甚至专门设置了 **“KV Cache and Long Context”** session；Strata 已经把 HBM→DRAM→SSD 看成一个整体 Context Cache，ECHO 针对 Sparse Attention 重做 offload/prefetch，DirectKV 则直接利用 GH200/GB200 的 NVLink-C2C 让 GPU kernel 访问 CPU-resident KV。([USENIX](https://www.usenix.org/conference/osdi26/technical-sessions?utm_source=chatgpt.com))

NSDI'26 的 SYMPHONY 更进一步明确指出：传统 LLM serving 之所以“stateful”，根源就是 request 产生并依赖 KV；它通过 compute-memory disaggregation 把计算和 KV 状态分开。Libra 则可以在任意 token boundary 切开 request，通过 chunked KV transfer 让同一请求跨 GPU 继续执行。([USENIX](https://www.usenix.org/conference/nsdi26/presentation/agarwal))

所以我更愿意把 2026 年定义为：

> **KVCache 从 Cache 走向 Context State Fabric 的转折年。**

这里的 “Context State Fabric” 是我对这些工作的系统性抽象，不是某篇论文自己的术语。

---

# 二、从第一性原理看：为什么 KVCache 必然从模型优化问题上升成 AI Infra 问题

对于标准 Transformer/GQA 模型，一条 sequence 每 token 的 KV 大致为：

$$
S_{KV/token}
\approx 2 \times L \times H_{kv}\times D_{head}\times B
$$

其中：

- $L$：Transformer layer 数；
- $H_{kv}$：KV Head 数；
- $D_{head}$：每个 head 的维度；
- $B$：每元素字节数；
- `2` 分别来自 K 和 V。

于是：

$$
S_{KV}
\propto
N_{requests}\times N_{tokens}
$$

这产生了一个与权重截然不同的系统特征：

**Model Weight 基本是静态 working set，而 KVCache 是跟用户数、session 长度、上下文长度和 Agent fan-out 同时扩张的动态 working set。**

所以模型权重的问题主要是：

> “这个模型放不放得下？”

而 KV 的问题逐渐变成：

> “当前几万个活跃 session 的状态分别在哪里？哪些还值得留？哪个 GPU 即将需要？从哪里取最快？是否值得取回来而不是重新计算？”

一旦问题变成后者，就已经不再是 Attention Kernel 内部优化，而是一个典型的：

**distributed memory + storage + network + scheduling + metadata + lifecycle management 问题。**

这正是为什么 2026 年同一个 KVCache 课题同时出现在操作系统、网络、存储和数据库顶会。

---

# 三、KVCache 扮演角色的五次跃迁

## 3.1 第一阶段：Attention 的计算优化缓存

最初 KV 的角色非常简单：

> 保存已经计算过的 K/V，避免 autoregressive decode 每生成一个 token 都重新计算全部历史 token。

生命周期约等于：

**request start → prefill → decode → request end → free**

它是完全 request-local、GPU-local、runtime-private 的。

---

## 3.2 第二阶段：GPU Memory Management Object

PagedAttention 之后，KV 不再必须连续存储，而成为 block/page 管理对象。

这使得：

- continuous batching；
- block allocation；
- eviction；
- prefix caching；
- preemption；
- block sharing

成为可能。

从这里开始，KV 从 Tensor 变成了 Runtime Resource。

---

## 3.3 第三阶段：跨请求的可复用计算结果

Prefix Cache/Radix Cache 把 KV 的价值从：

> “减少同一次 decode 的计算”

扩展为：

> “避免不同请求重复执行相同 prefill。”

于是 KV 第一次拥有了跨 request 的生命周期。

FAST'26 CacheSlide 又进一步放宽“相同 prefix”的约束：Agent prompt 中很多固定段落绝对位置变化、但相对结构不变，CacheSlide 对这种 Relative-Position-Dependent Caching 做位置修正和部分重新计算，在其测试中获得 3.11–4.3× latency reduction 和 3.5–5.8× throughput improvement。([USENIX](https://www.usenix.org/conference/fast26/presentation/liu-yang))

SIGMOD'26 FusionRAG 则把边界进一步推进到 RAG：不同 retrieved chunk 并不是普通 prefix，它们之间存在新的 cross-chunk attention。FusionRAG 不再做简单的 cache hit/miss，而是**复用大多数 KV，只重新计算 attention-critical token**；论文报告在重新计算少于 15% token 时，相比 Full Attention 将 TTFT 降低 2.66–9.39×。([SIGMOD 2026](https://2026.sigmod.org/sigmod_program_detailed.shtml?utm_source=chatgpt.com))

这说明：

> **KV reuse 正从 binary semantics（hit/miss）走向 partial reuse semantics。**

---

## 3.4 第四阶段：分布式推理中的“执行状态”

这是 2026 年最重要的一步。

传统调度实际存在一个隐含约束：

> Request 在 GPU A 上生成了 KV → 后续最好还跑 GPU A。

否则就要：

- recompute；
- 或 transfer KV。

这就是典型的 **state affinity**。

NSDI'26 SYMPHONY 把 KV 与 compute 解耦，其根本意义不是“多了一层远程内存”，而是：

> **把 GPU 从“有状态 worker”逐步变成可替换的计算资源。**

SYMPHONY 使用 advisory request 提前移动 KV，再通过按 NN layer/request priority 的 KV 管理和与 serving framework cooperative memory management 隐藏访问开销；论文报告相对 vLLM 2.4× end-to-end latency 改善，并可在很小 latency increase 下服务 4× 请求。([USENIX](https://www.usenix.org/conference/nsdi26/presentation/agarwal))

Libra 更激进：

> request 本身可以在 token boundary 被切成多个 micro-request。

KV 就是让执行可以从 GPU A 延续到 GPU B 的“continuation state”。([USENIX](https://www.usenix.org/conference/nsdi26/presentation/ruan-libra))

因此未来：

**request ≠ GPU execution instance。**

更接近：

**request = movable computation + movable execution state。**

---

## 3.5 第五阶段：AI Infra 的一级 Data Class

这是工业界 2026 年最明显的新共识。

NVIDIA CMX 已经直接把 KV 定义成一种不同于传统 enterprise storage data 的数据类型：

- transient；
- derived；
- reconstructable；
- latency sensitive；
- high bandwidth；
- 不需要企业数据同等级的 durability/replication/background consistency。

NVIDIA 因此在传统 G1 GPU HBM、G2 DRAM、G3 local SSD、G4 shared storage 之间增加一个专门面向 Context/KV 的 **G3.5 CMX context tier**。([NVIDIA Developer](https://developer.nvidia.com/blog/?p=111143))

AMD ROCm Infinity Context 也做出了几乎同样的抽象：把网络连接存储直接定义为 **purpose-built KV cache tier**，位于 GPU HBM 和 CPU DRAM 之下，通过 RDMA/hipFile/AMD Infinity Storage 直接进入 GPU，尽量绕开 CPU memory bus。([ROCm Blogs](https://rocm.blogs.amd.com/software-tools-optimization/amd-infinity-context/README.html?utm_source=chatgpt.com))

当 NVIDIA 与 AMD 都开始用独立产品层表达这个问题时，它基本说明：

> **KVCache 已经不再只是 vLLM/SGLang 的内部 feature，而是在形成独立的 AI Infrastructure resource class。**

---

# 四、KVCache 在 AI Infra 中开始承担哪些职责

把上述论文合在一起，KV 目前至少承担七种不同责任。

| KVCache 的新职责 | 过去 | 2026 后的变化 |
|---|---|---|
| GPU Capacity Management | 占用 HBM | 决定 concurrency/admission/batch capacity |
| Recomputation Avoidance | decode 内避免重算 | 跨 request/session/worker 避免 prefill |
| Execution State | GPU 本地状态 | 可迁移、checkpoint、restore、handoff |
| Scheduling Signal | 基本不参与全局调度 | locality/tier/transfer cost 进入路由 |
| Network Payload | 少量 P→D | 大量跨 P/D、storage、remote memory 传输 |
| Storage Object | request 结束即删除 | DRAM/NVMe/remote/CXL 多层生命周期 |
| Agent Context | 单轮 session state | tool call、multi-turn、subagent 间反复 reuse |

其中最大的认知变化是第四项：

## **Cache placement 已经成为 Scheduling State。**

传统 KV-aware routing：

$$
worker=\arg\max CacheHit(worker)
$$

已经不够。

真正应该优化的是：

$$
worker^*=
\arg\min[
T_{queue}
+T_{missing\ prefill}
+T_{KV\ restore}
+T_{network}
+T_{storage}
+T_{interference}
]
$$

AMD 在 2026 年和 Moonshot AI 的 UMBP 工作里已经非常明确地表达这个变化：不再只问 **“谁有这个 prefix？”**，而是问：

> **“谁能最快地把这个 prefix 服务出来？”**

UMBP 把 HBM→host DRAM→shared pool→SSD 的 placement、访问历史、capacity/utilization、bandwidth、RPC latency 暴露给 router。AMD 的实验甚至出现一个很有启发性的现象：**cumulative hit ratio 基本不变，但通过 shareable L3 + loadback prefetch，p99 TTFT 最多下降 3.2×。**([AMD](https://www.amd.com/en/developer/resources/technical-articles/2026/rebuilding-agentic-ai-for-amd-gpu.html))

这说明未来一个非常重要的评价指标是：

> **Cache Hit Rate 本身正在失效。**

---

# 五、为什么“命中了 KV”不等于“性能更好”

从第一性原理看，一次 remote KV hit 值不值得使用，实际上满足：

$$
T_{lookup}
+T_{queue}
+T_{fetch}
+T_{restore}
+T_{stall}
<
T_{recompute}
$$

才有价值。

如果：

$$
T_{fetch}>T_{recompute}
$$

那么这是一个：

> **negative-value cache hit**

甚至会比 miss 更差。

OSDI'26 Strata 把这种现象称为包括 **delay hit** 在内的问题：KV 虽然存在，但 I/O、碎片化小传输和 concurrent load 会让 cache-loading stall 直接阻塞 prefill。Strata 因此不是简单扩大 cache，而是同时修改 GPU-assisted I/O 和 cache-aware scheduler；论文中相对 vLLM-LMCache 最高有 5× throughput improvement。([USENIX](https://www.usenix.org/conference/osdi26/presentation/xie-zhiqiang))

FAST'26 Bidaw 得出了同样的系统结论：Host DRAM + SSD 虽然容量大，但不考虑加载延迟时，KV loading 在其 workload 上可以令 latency 增长最多 3.8×。因此它让 serving scheduler 理解 KV 在哪一层、KV 有多大，同时让存储系统理解 future access pattern。([USENIX](https://www.usenix.org/conference/fast26/presentation/hu-shipeng))

所以未来 KV infra 的核心 KPI 不应该只是：

`HitRatio`

而更应该是类似：

$$
EffectiveReuseValue
=
SavedPrefillCost
-
LookupCost
-
TransferCost
-
RestoreCost
-
InterferenceCost
$$

或者最终直接落到：

- SLO-qualified goodput；
- TTFT P95/P99；
- TPOT；
- useful token / dollar；
- useful token / watt。

---

# 六、围绕 KVCache，现在真正难的十个问题

| 核心挑战 | 本质 | 目标 | 2026 代表技术 |
|---|---|---|---|
| **1. Capacity Wall** | KV 随 context/concurrency 线性增长 | HBM 不再决定 session 数 | HBM/DRAM/SSD/CXL 分层 |
| **2. Bandwidth Wall** | offload 后 capacity 问题转成 bandwidth 问题 | transfer 不进入 critical path | Prefetch、pipeline、zero-copy |
| **3. KV Fragmentation** | page/block 离散导致大量 small I/O | 把物理碎片聚成大传输 | Strata GPU-assisted I/O |
| **4. Cache Locality vs Load Balance** | 有 KV 的 GPU 可能正 overloaded | 同时考虑 queue + locality + transport | SYMPHONY、UMBP |
| **5. Network Hotspot** | PD/remote cache 将 KV 变成网络流量 | 平衡 P/D/storage NIC | DualPath |
| **6. Reuse Correctness** | position/context/model 改变后 KV 不再严格等价 | 安全扩大 reuse domain | CacheSlide、FusionRAG、DroidSpeak |
| **7. Sparse Attention Granularity** | 全量搬 KV 浪费，而真正 attention 只访问部分 | 按实际 attention demand 移动 | ECHO、SolidAttention、KVDrive |
| **8. Storage Placement/Eviction** | LRU 不知道 recompute cost | 根据 future value 管 KV | Bidaw、KVDrive |
| **9. Shared-state Metadata** | cluster-wide KV 需要 lookup/location/version/lifecycle | lookup 不进入 TTFT critical path | Distributed directory / prefix index |
| **10. Heterogeneous Hardware** | PCIe/RDMA/CXL/C2C/SSD 特性完全不同 | 自动选最佳 data path | NIXL/MoRI/CXL/zero-copy |

其中可以看到一条很清楚的演进路线：

> **Capacity → Bandwidth → Latency → Scheduling → Semantics。**

也就是说，单纯“加更多内存”只能解决第一层问题。

---

# 七、2026 年采用的核心技术手段

## 7.1 多层 KV Hierarchy：HBM 不再是唯一 KV Home

现在主流形态已经趋近于：

**HBM → CPU DRAM → CXL/shared DRAM → NVMe/Flash → Remote KV Store/Object Storage**

OSDI Strata、FAST Bidaw、SIGMOD KVDrive，以及 LMCache、NVIDIA KVBM、AMD Infera 都已经采用类似思路。LMCache 当前架构也明确提供 GPU、CPU DRAM、local storage、remote backend 的 hierarchy；NVIDIA KVBM 则定义 GPU G1 → host G2 → disk G3 → object storage G4。([arXiv](https://arxiv.org/abs/2605.18071))

但关键不是“有四层”。

真正困难的是：

> **Placement policy + Migration policy + Prefetch policy + Eviction policy + Routing policy 必须成为同一个闭环。**

---

## 7.2 Prefetch：把“远程 KV 延迟”移出关键路径

SYMPHONY 用 advisory requests。

ECHO 根据 sparse-attention index score 的可预测性做 intra-query/inter-query lossless prefetch。([USENIX](https://www.usenix.org/conference/osdi26/presentation/liu-guangda))

SolidAttention 利用 sparse attention 的 temporal locality 做 SSD speculative prefetch，并把细粒度 KV 聚成粗粒度 I/O block；在 128K context 的实验中，其 KV memory footprint 最多降低 98%。([USENIX](https://www.usenix.org/conference/fast26/presentation/zheng))

AMD UMBP 甚至直接利用 agent workload 的时间结构：

> tool call 期间就可以知道某些 session 很快会回来，因此提前把 KV 向 HBM 方向提升。

这意味着 Agent 提供了传统 cache system 没有的重要信息：

**future intent。**

---

# 八、SIGCOMM'26 透露出的一个关键信号：KVCache 正在变成网络问题

SIGCOMM'26 的 LLM Inference & Serving session 非常有代表性，同时出现：

- KVServe；
- DualPath；
- KVCodec；
- Turbo。

这不是巧合。([SIGCOMM Conferences](https://conferences.sigcomm.org/sigcomm/2026/accepted/?utm_source=chatgpt.com))

### KVServe

PD separation / remote KV store 后：

> KV 不再只是 GPU memory object，而是 explicit network/storage payload。

KVServe 因此根据：

- bandwidth；
- latency；
- SLO；
- quality budget

动态选择 KV compression profile，而不是静态压缩策略；论文报告在其测试中 PD serving JCT 最多改善 9.13×，KV-disaggregated serving TTFT 最多改善 32.8×。([SIGCOMM Conferences](https://conferences.sigcomm.org/sigcomm/2026/program/papers/?utm_source=chatgpt.com))

### DualPath

DualPath 观察到 Agentic workload 下一个非常具体的新瓶颈：

> Prefill 节点的 storage NIC 因为大量读取 KV 被打满，而 Decode 节点 storage NIC 却闲置。

于是增加：

**Storage → Decode → Prefill**

第二条 KV loading path，通过 compute RDMA network 把 D 上读入的 KV 转给 P。

这说明：

> **Prefill/Decode 分离之后，仅考虑 GPU 算力配比已经不够，P/D 的 NIC、storage bandwidth 也必须联合调度。**

论文在 production agentic workload 上报告 offline throughput 最多 1.87×、online throughput 平均 1.96× improvement 且不违反其 SLO。([SIGCOMM Conferences](https://conferences.sigcomm.org/sigcomm/2026/program/papers/?utm_source=chatgpt.com))

### KVCodec

甚至开始使用 GPU-native video codec 压缩 KV，以缩小 remote KV transfer；其实质说明：

> **KV serialization/encoding format 本身已经成为网络协议栈的一部分。** ([SIGCOMM Conferences](https://conferences.sigcomm.org/sigcomm/2026/program/papers/?utm_source=chatgpt.com))

再向下一步走，很自然就是：

**KV-aware congestion control / topology-aware routing / NIC/DPU offload / in-network aggregation。**

SIGCOMM'26 Turbo 已经探索把 long-context distributed attention 的 broadcast/aggregation 推入 network。([SIGCOMM Conferences](https://conferences.sigcomm.org/sigcomm/2026/program/papers/?utm_source=chatgpt.com))

---

# 九、CXL 为什么在 2026 年突然与 KVCache 强烈耦合

SIGMOD'26 Beluga 是一个很有代表性的转折点。

传统 remote KV pool：

**GPU → NIC → RDMA protocol → remote memory**

Beluga 希望变成：

**GPU/CPU → CXL switch → shared memory**

利用的是 load/store semantics，而不是 message-oriented remote transfer。Beluga 在商用 CXL switch-based memory pool 上实现 Beluga-KVCache；论文报告相对其 RDMA baseline，TTFT 降低 89.6%、vLLM throughput 提高 7.35×。([SIGMOD 2026](https://2026.sigmod.org/sigmod_papers.shtml?utm_source=chatgpt.com))

这里真正值得关注的不是论文里的数字，而是体系结构方向：

### RDMA KV Store

KV 是：

> remote object

需要：

`lookup → descriptor → transfer → completion → attach`

### CXL Shared Memory

理论上 KV 更接近：

> globally addressable memory object

于是未来可能出现：

$$
HBM \leftrightarrow CXL\ Memory
$$

而不是：

$$
HBM \leftrightarrow Network\ KV\ Service
$$

---

但我不认为可以由此推出：

> **“CXL 将取代 RDMA。”**

原因很简单。

CXL 最强的是：

- rack/local fabric；
- memory semantics；
- 低访问延迟；
- fine-grained access。

RDMA/network storage 最强的是：

- scale-out；
- 较大物理距离；
- topology flexibility；
- mature NIC/fabric；
- 跨 rack/cluster。

因此更可能的未来结构是：

**CXL = Scale-Up / rack shared context pool**  
**RDMA = Scale-Out KV fabric**  
**NVMe/Flash = capacity tier**

三者共存。

---

# 十、KV reuse 的语义边界正在快速扩张

2026 年另外一个很容易被忽视、但长期意义可能更大的趋势是：

> **研究重点正在从“KV 放哪里”延伸到“什么情况下两个 KV 可以认为等价”。**

### CacheSlide

放松了绝对 position 相同的限制。([USENIX](https://www.usenix.org/conference/fast26/presentation/liu-yang))

### FusionRAG

放松了 retrieved chunk 必须保持原始 prefix context 的限制，通过 partial recompute 恢复 cross-chunk context。([arXiv](https://arxiv.org/abs/2601.12904))

### DroidSpeak

甚至突破：

> **Model A 的 KV 只能 Model A 自己使用**

这个传统假设。

NSDI'26 DroidSpeak 发现，对**相同 architecture 的 fine-tuned model variants**，可以只重新计算少量 layer，复用其他 layer 的 KV；论文报告最多 4× throughput 和约 3.1× faster prefill，并保持其评测指标上的 negligible quality degradation。([USENIX](https://www.usenix.org/conference/nsdi26/presentation/liu-yuhan))

所以 reuse domain 正在从：

$$
same\ request
$$

扩展到：

$$
same\ prefix
$$

再到：

$$
shifted/recomposable\ context
$$

甚至：

$$
related\ model\ variants
$$

这是一条非常值得持续关注的研究线。

---

# 十一、但这里必须澄清一个重要概念：KVCache ≠ Agent 长期记忆

NVIDIA 在 CMX 的产品叙事里，把 KV 称为 agentic long-term memory/context memory，这是一个合理的基础设施类比。([NVIDIA Developer](https://developer.nvidia.com/blog/?p=111143))

但从严格技术语义来看，我认为不能把二者直接等同。

KVCache 有四个根本属性：

**它是 derived、model-dependent、position/context-dependent、representation-dependent。**

也就是说它通常绑定：

- model weights/version；
- Transformer architecture；
- RoPE/position；
- Attention implementation；
- KV layout；
- precision/quantization；
- TP/parallel layout；
- 有时还包括 adapter。

DroidSpeak 之所以值得发表，恰恰说明：

> **跨 model reuse 默认并不成立。**

CacheSlide/FusionRAG 同样说明：

> **上下文变化后 KV reuse 默认也并不成立。**

因此未来 Agent Memory 更可能形成：

| 层级 | 内容 | 典型特征 |
|---|---|---|
| **L0 Execution Memory** | KVCache / hidden state | 极快、模型相关、精确执行状态 |
| **L1 Working Semantic Memory** | RAG/vector/structured context | 可检索、相对模型独立 |
| **L2 Episodic Memory** | 对话/事件/trajectory | 长期保存 |
| **L3 Durable Knowledge** | DB/Object Store/Documents | canonical source of truth |

这四层之间会发生转换，而不是互相取代。

---

# 十二、Agentic AI 为什么会让 KVCache 的重要性再次跃升一个量级

AMD 2026 年和 Moonshot AI 对 Agentic coding workload 的观察很有代表性：

- session 很长；
- context 随 turn 持续增长；
- tool call 之间存在 gap；
- subagent 会 burst fan-out；
- prefix 重复率很高；
- working set 很快超过 HBM。

AMD 因而直接得出“agentic coding is cache-dominated and bursty”的工程判断。([AMD](https://www.amd.com/en/developer/resources/technical-articles/2026/rebuilding-agentic-ai-for-amd-gpu.html))

Agent 与 Chat 有一个本质区别。

传统 Chat：

`Prompt → Prefill → Decode → Human think → Next turn`

Agent：

`LLM → tool → LLM → code → LLM → retrieval → LLM → subagent × N → LLM`

一个业务任务可能触发数十甚至数百次 inference。

但这些调用之间往往共享大量：

- System Prompt；
- repository；
- task description；
- conversation trajectory；
- tool results；
- planning history。

因此 Agent 使 KVCache 从：

> request optimization

变成：

> **workflow state reuse。**

---

# 十三、未来在线推理流程因此会被重构

今天我们习惯画：

**Request → Tokenization → Prefill → Decode → Response**

我认为这张图未来是不完整的。

更可能逐渐演变为：

**Request / Agent Turn**

↓

**Context Identity / Session / Tenant Resolution**

↓

**Global KV Metadata Lookup**

↓

**State-aware Scheduler**

同时判断：

- HBM hit？
- local DRAM？
- CXL？
- local NVMe？
- remote replica？
- network congestion？
- recompute faster？
- SLO 是否允许？

↓

**Prewarm / Prefetch / Migrate**

↓

**Context Reconstruction**

可能是：

- full KV restore；
- partial restore；
- position correction；
- selected-layer recompute；
- missing-token prefill；
- remote sparse fetch。

↓

**Decode**

↓

**Async Spill / Replicate / Compress / Evict**

↓

**Agent Tool Execution**

同时利用 gap：

**prefetch 下一阶段 context**

↓

下一次 LLM invocation。

---

因此一个非常重要的变化是：

> **Prefill 不再必然是每个 inference request 都从零执行的固定阶段。**

对于高 reuse workload，它逐渐变成：

> **Context Cache Miss Handler / Materialization / Repair Stage。**

这可能是未来推理范式最深的改变之一。

---

# 十四、LLM Serving Framework 本身也会因此改变

vLLM 当前已经提供多种 KVConnector，包括 NIXL、LMCache、Mooncake、MoRI、offloading 等；其官方文档明确表示 production-level disaggregated prefilling 高度依赖第三方 KV infrastructure connector。([vLLM](https://docs.vllm.ai/en/latest/features/disagg_prefill/?utm_source=chatgpt.com))

MooncakeStoreConnector 甚至可以同时组合：

- P→D direct transfer；
- distributed KV store；
- cross-instance prefix sharing。([vLLM](https://docs.vllm.ai/en/latest/features/mooncake_store_connector_usage/?utm_source=chatgpt.com))

这意味着未来 Serving Engine 的责任边界可能逐渐变成：

### Execution Engine

vLLM / SGLang / TensorRT-LLM：

- batching；
- attention；
- model execution；
- local page table；
- CUDA/NPU graph。

### Context State Layer

独立负责：

- global prefix index；
- KV namespace；
- placement；
- lifecycle；
- hierarchy；
- prefetch；
- replication；
- eviction；
- cache consistency；
- multi-consumer sharing。

### Transport Layer

- NVLink/C2C；
- CXL；
- RDMA；
- PCIe；
- GPU Direct Storage；
- NVMe；
- TCP fallback。

因此长期来看：

> **LLM engine 与 KV state system 很可能像 Compute Engine 与 Distributed Storage 一样逐步解耦。**

---

# 十五、工业界究竟形成了什么共识

截至 2026 年 9 月，我认为至少形成了六个相当明确的共识：

### 1. HBM-only KV architecture 已经无法覆盖长上下文 + 高并发 + Agent

NVIDIA CMX、AMD AIC、SGLang HiCache、LMCache、KVBM 都已经默认接受多层 KV hierarchy。([NVIDIA](https://www.nvidia.com/en-us/data-center/ai-storage/cmx/?utm_source=chatgpt.com))

### 2. KV Storage 不能只是 passive byte store

Scheduler 必须知道：

**在哪里、热度、大小、带宽、loadback cost。**

AMD UMBP 明确走这一方向；OSDI Strata、FAST Bidaw 也给出相同学术证据。([AMD](https://www.amd.com/en/developer/resources/technical-articles/2026/rebuilding-agentic-ai-for-amd-gpu.html))

### 3. KV transfer 成本必须与 compute 一起优化

否则只是：

> 用 I/O stall 替代 recomputation。

### 4. PD separation 已成为重要架构方向，但 KV movement 是它的核心税项

vLLM 的官方实现本身就是通过 KVConnector 把 P 与 D 连接起来。([vLLM](https://docs.vllm.ai/en/latest/features/disagg_prefill/?utm_source=chatgpt.com))

### 5. Agentic workload 让 cluster-wide KV sharing 从“优化项”变成越来越重要的基础能力

NVIDIA、AMD 都把 long-context/multi-turn/agent 明确列为 KV context tier 的主要 target workload。([NVIDIA](https://www.nvidia.com/en-us/data-center/ai-storage/cmx/?utm_source=chatgpt.com))

### 6. KV 正在成为一个独立数据类型

这一点 NVIDIA 表达得最明确：

> KV 是 transient / derived / recomputable 的 AI-native data class，不应该完全复用传统 durable enterprise storage 的语义和成本模型。([NVIDIA Developer](https://developer.nvidia.com/blog/?p=111143))

---

# 十六、NVIDIA / AMD / Intel / Arm 分别正在做什么

这里必须把“真正 KV-specific 产品”与“可以用于 KV 的通用硬件”分开，否则容易得出错误结论。

| 厂商 | KV-specific 行动 | 硬件/系统动作 | 我对技术路线的归纳 |
|---|---|---|---|
| **NVIDIA** | Dynamo KVBM、NIXL、CMX | BlueField-4、Spectrum-X、NVLink-C2C、GDS | 最完整的 KV vertical stack |
| **AMD** | UMBP、ROCm AIC、Infera KVD、MoRI | MI355X、AINIC/Pensando、hipFile/AIS、RDMA | Open ecosystem + KV/storage/network co-design |
| **Intel** | KVCrush/OpenVINO GenAI | Xeon 6 CXL/Flat Memory、CPU memory capacity/AMX | 更偏 CPU/CXL memory + KV footprint reduction |
| **Arm** | SGLang/HiCache ecosystem enablement | AGI CPU、DDR5、PCIe Gen6、CXL 3.0 | CPU orchestration/data-plane foundation |

---

## NVIDIA：正在试图建立完整的 Context Memory Stack

目前最系统。

### Runtime

**Dynamo**

### KV Memory Manager

**KVBM**

支持统一管理：

- GPU；
- pinned CPU；
- remote RDMA；
- SSD；
- object/cloud storage。([NVIDIA Docs](https://docs.nvidia.com/dynamo/zh-CN/v1.3.0/components/kvbm?utm_source=chatgpt.com))

### Data Movement

**NIXL**

统一 API 覆盖：

- RDMA；
- GPU-initiated network；
- GPU Direct Storage；
- NVMe；
- object storage。

其明确用例包括 P/D KV transfer 和 long-context KV storage。([NVIDIA Developer](https://developer.nvidia.com/blog/?p=113426&utm_source=chatgpt.com))

### Context Storage

**CMX**

专门增加 G3.5 Context Memory Tier，并通过 BlueField-4 运行 KV I/O plane。NVIDIA 宣称其目标是在对应配置下实现最高 5× TPS 和 5× power efficiency improvement；这是厂商测试/产品声明，不应当当作跨平台普遍数字。([NVIDIA Developer](https://developer.nvidia.com/blog/?p=111143))

所以 NVIDIA 的方向可以总结为：

> **GPU → Runtime → KV Manager → Data Movement → DPU → Network → KV Storage**

全链路 co-design。

---

# 十七、AMD：正在形成与 NVIDIA 很相似、但更强调开放接口的路线

AMD 2026 年的变化其实非常大。

## UMBP

Unified Memory & Bandwidth Pool：

**HBM → Host DRAM → shared UMBP pool → SSD**

同时把 KV placement 信息反馈给 scheduler。([AMD](https://www.amd.com/en/developer/resources/technical-articles/2026/rebuilding-agentic-ai-for-amd-gpu.html))

## MoRI

负责 RDMA/transport。

## AMD Infinity Context

专门的 distributed KV storage tier，通过：

**network-attached storage → RDMA/hipFile/AIS → GPU HBM**

避免 CPU memory bus 成为中间 hop。([ROCm Blogs](https://rocm.blogs.amd.com/software-tools-optimization/amd-infinity-context/README.html?utm_source=chatgpt.com))

AMD 公布的路线图是：

- 2026 年 9 月：ROCm AIC Tech Preview；
- 2026 Q4：计划与 Infera DI platform 一起 launch。([ROCm Blogs](https://rocm.blogs.amd.com/software-tools-optimization/amd-infinity-context/README.html))

注意：截至今天，应当把 Q4 launch 仍视为其公开 roadmap，而不是已经完成的事件。

AMD 的一个特别值得关注的方向是：

> **把 KV cache telemetry → router → placement/eviction/prefetch 做成闭环。**

这可能比单独优化 RDMA bandwidth 更重要。

---

# 十八、Intel：目前更像是在攻“KV footprint + CPU/CXL memory substrate”

Intel 公开路线不像 NVIDIA/AMD 那样已经形成专门的 rack-level KV storage platform。

其较明确的 KV-specific 工作是 **KVCrush**。

Intel 采用 compact binary token representation + token pruning，宣称可将 KV cache 缩小最多约 4×、accuracy drop <1%，并已从 OpenVINO GenAI 2025.3 开始集成。([Intel Community](https://community.intel.com/t5/Blogs/Tech-Innovation/Artificial-Intelligence-AI/KVCrush-Rethinking-KV-Cache-Alternative-Representation-for/post/1716243))

另一方面，Intel 的硬件优势更集中在：

### Xeon + CXL Memory

Xeon 6/6+ 的 Flat Memory Mode 可以把 processor-attached DRAM 与 CXL-attached memory 向 OS 呈现为一个 flat address space，并由 hardware 自动管理 tier placement。([Intel](https://www.intel.com/content/www/us/en/support/articles/000102525/processors/intel-xeon-processors.html))

这与 Beluga 代表的研究方向天然相关：

> **把 CPU/CXL large-memory system 变成 KV 的 capacity tier。**

另外 Intel 2026 年也在验证 heterogeneous inference：例如把视觉 Encode 放到 Arc Pro B60，把 HBM 更丰富的 H200 保留给 prefill/decode；其技术文章明确把 multimodal 长输入带来的 KV pressure 视为拆分阶段的重要动机之一。([Intel Community](https://community.intel.com/t5/Blogs/Tech-Innovation/Data-Center/Lowering-Multimodal-Inference-Cost-with-Heterogeneous-E-PD/post/1752499?utm_source=chatgpt.com))

所以 Intel 当前更适合概括为：

> **减少 KV footprint + 扩大 CPU/CXL memory domain + heterogeneous orchestration。**

而不能说 Intel 已经有类似 CMX/AIC 的明确集群级 KV 产品。

---

# 十九、Arm：重点不是“做 KV Store”，而是争夺 AI Infra Host/Control/Data Plane

Arm 2026 年已经让 SGLang 成为 Neoverse 上的生产级目标。

Arm 自己的技术文章明确讨论：

- RadixAttention；
- hierarchical KV cache；
- HiCache；
- HBM → DRAM → Mooncake；
- cross-node KV reuse；
- KV spilling/disaggregation。([Arm Developer](https://developer.arm.com/community/arm-community-blogs/b/ai-blog/posts/bringing-sglang-high-performance-llm-inference-to-arm-neoverse?utm_source=chatgpt.com))

与此同时 AGI CPU 提供：

- 最多 136 Neoverse V3 cores；
- 12-channel DDR5；
- PCIe Gen6；
- CXL 3.0 Type-3；
- 高 memory bandwidth；
- 面向 agentic orchestration 的定位。([Arm](https://www.arm.com/zh-cn/products/cloud-datacenter/arm-agi-cpu?utm_source=chatgpt.com))

所以 Arm 的战略意义在于：

> 当 GPU 更聚焦 tensor computation 时，越来越复杂的 KV metadata、routing、storage、network、agent orchestration 工作会回到 CPU/DPU/Data Plane。

KVCache 的系统化反而会扩大 Host CPU 的责任边界。

---

# 二十、再向前推一步：KVCache × Agent × Memory System 会发生什么“化学反应”

这是我认为未来 2–4 年最值得关注的部分。

## 20.1 Agent 将使 KV 从“cache”变成“session working state”

未来一个 Agent session 可能形成树：

```text
                 Shared System Prompt
                        │
                  Shared Task KV
                        │
            ┌───────────┴───────────┐
         Agent A                  Agent B
            │                        │
       Tool Call A1             Tool Call B1
            │                        │
       Branch KV A             Branch KV B
```

这天然要求：

- fork；
- clone；
- copy-on-write；
- truncate；
- merge/splice；
- pin；
- TTL；
- priority；
- session ownership。

也就是说 KV store 最终会越来越不像简单 object store，而更像：

> **versioned execution-state store。**

---

# 二十一、Agent intent 很可能成为新的 Cache Hint API

传统 cache system 最大的问题之一：

> 它不知道未来。

但 Agent runtime 往往知道：

- parent agent 等待哪个 child；
- tool call 完成后会恢复哪个 context；
- 当前 branch 是否即将结束；
- 哪个 context 很快再次访问。

AMD UMBP 已经明确提出 TTL、session pinning、priority 等 agent-aware hint，虽然其中部分仍在开发。([AMD](https://www.amd.com/en/developer/resources/technical-articles/2026/rebuilding-agentic-ai-for-amd-gpu.html))

所以未来非常可能出现：

```text
Agent Runtime
     │
     │  retention / TTL / priority /
     │  expected-reuse / branch semantics
     ▼
KV State Manager
     │
     ├── Prefetch
     ├── Demote
     ├── Promote
     ├── Replicate
     └── Evict
```

这是 **application intent → memory hierarchy** 的跨层优化。

非常值得关注。

---

# 二十二、模型结构也会开始反过来适应 KV Infra

ECHO 和 SolidAttention 已经说明了这个方向。

Sparse Attention 之后：

> 模型本身告诉系统“我实际上只需要历史 KV 的这一小部分”。

于是传统：

**Storage → Load all KV → GPU → Attention selects**

可能逐渐变成：

**Attention index → select required KV → storage/network fetch → Attention**

也就是：

> **Attention selection 与 Memory Addressing 开始融合。**

OSDI'26 ECHO 正是在针对 native sparse attention 做 lossless prefetch。([USENIX](https://www.usenix.org/conference/osdi26/presentation/liu-guangda))

FAST SolidAttention 则将 sparse attention 与 SSD block/prefetch 协同。([USENIX](https://www.usenix.org/conference/fast26/presentation/zheng))

未来 MLA、Sparse Attention、KV quantization 的价值因此不只在降低 GPU HBM：

> 它们实际上会决定整个 AI cluster 每秒需要移动多少 Context State。

模型 architecture 和 Infra architecture 的界限会进一步模糊。

---

# 二十三、最终可能形成一个新的 LLM 推理操作系统抽象

把所有趋势合起来，可以得到一个非常有意思的最终形态：

### Compute Plane

GPU/NPU：

> “给我需要的 state，我执行模型。”

### Context State Plane

KVCache system：

> “我知道 state 是什么、在哪里、有哪些 replica、什么版本、多久不用、如何恢复。”

### Transport Plane

C2C/CXL/RDMA/PCIe/NVMe：

> “我决定 state 怎么过去。”

### Control Plane

CPU/DPU/Scheduler：

> “我决定 computation 去找 state，还是 state 去找 computation。”

### Memory Plane

Agent memory/RAG/DB：

> “我保存长期可解释、可检索、可持久化的知识和历史。”

---

这时在线 inference 真正要调度的就不再只是：

$$
GPU\ FLOPS
$$

而是：

$$
Compute + State + DataMovement
$$

三者的联合优化。

---

# 二十四、我认为 2026 年最重要的五个技术判断

**第一，KVCache 的核心矛盾已经从“容量不足”升级为“状态放置与移动的经济性”。** HBM 扩容只能延后问题；只要 context × concurrency × agent fan-out 持续增加，必须进入异构层次结构。

**第二，真正有价值的指标不是 Cache Hit Rate，而是 Effective Compute Avoidance。** 一个落在 remote SSD、需要 800 ms 才能 load back 的 hit，有可能不如 300 ms recompute；scheduler 必须拥有完整 cost model。Strata、Bidaw、SYMPHONY、DualPath 和 AMD UMBP 都在从不同角度指向这一结论。([USENIX](https://www.usenix.org/conference/osdi26/presentation/xie-zhiqiang))

**第三，KVCache 正在成为解除“Request–GPU state affinity”的关键。** 一旦 state 可以低成本迁移和共享，GPU pool 才真正能够按照 queue、SLO、topology 动态调度，而不是被 historical KV location 锁死。

**第四，未来的关键突破不只是 KV storage，而是 KV semantics。** CacheSlide、FusionRAG、DroidSpeak 分别突破 position、RAG chunk 和 model-variant reuse boundary。([USENIX](https://www.usenix.org/conference/fast26/presentation/liu-yang))

**第五，Agentic AI 会把 KVCache 与 Memory System 合流，但不会让二者等同。** KV 更可能成为最靠近 accelerator 的 **execution-memory L0/L1**，Semantic/Episodic Memory 则继续保存可跨模型、可持久化的信息。两者之间未来会形成 compiler/runtime-style 的转换、materialization 和 eviction 关系。

---

# 二十五、最后给出一个更凝练的趋势图

从 2023–2026 的路径看：

**KVCache**

`Attention Optimization`

↓

`GPU Memory Object`

↓

`Cross-request Prefix Cache`

↓

`Hierarchical Cache`

↓

`Distributed Execution State`

↓

`Schedulable Infrastructure Resource`

↓

`AI-native Context Data Class`

↓

**未来：Context State Fabric**

而推理流程则从：

**Prompt → Prefill → Decode**

向：

**Resolve Context → Locate State → Schedule Compute/State → Restore/Repair Missing Context → Decode → Persist/Demote State**

迁移。

---

## 最终判断

如果只把 KVCache 理解成“GPU 显存不够，所以把 KV 卸载到 CPU/SSD”，会低估这轮变化。

**2026 年真正浮现出来的是一个新的基础设施层：它位于 LLM Serving Engine 与 Memory/Storage/Network 之间，负责保存、定位、迁移、复用和调度模型已经完成的“计算状态”。**

它最终解决的不是：

> **“KVCache 放在哪里？”**

而是：

> **“在整个 AI cluster 中，已经完成过的计算应该以什么状态保存、在哪里保存、何时移动、何时复用、何时重算，以及计算下一步应该移动到状态旁边，还是状态移动到计算旁边？”**

这也是为什么我认为，**KVCache 将成为未来 AI Infra 中连接 LLM Runtime、Agent Runtime、Memory System、CPU/DPU、CXL/RDMA Fabric 和 AI Storage 的关键中间层。**

而一旦这个中间层成熟，最大的范式变化会是：

> **Prefill 从每次请求必经的“计算阶段”，逐步变成 Context State 缺失时才触发的“状态 Materialization/Repair 机制”。**

对长会话、RAG 和 Agent workload 来说，这可能比单纯提高 GPU FLOPS 更深刻地改变未来在线推理架构。