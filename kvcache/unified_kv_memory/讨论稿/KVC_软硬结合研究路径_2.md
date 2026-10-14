# you asked

message time: 2026-10-13 14:05:34

在上述表格中再增加一列，给出1到2篇值得参考的论文，论文给出全称以及出处。

---

# chatgpt response

可以。我会保持上一张表的结构，只新增最后一列 **“参考论文（全称 + 出处）”**。这里我会优先选：一篇证明该 primitive 有端到端价值的系统论文 + 一篇更贴近底层机制/体系结构的论文；如果目前只有 arXiv 预印本，我会明确标注，不把它包装成已录用顶会论文。

可以。下面是在上一版 **KV Physicalization → HOST/SoC 硬件能力矩阵** 基础上新增“参考论文”列后的版本。

我按两个原则选论文：一篇尽量体现**端到端业务价值**，另一篇尽量体现**底层 physicalization / architecture mechanism**。截至 **2026 年 10 月 13 日**，尚未确认正式发表的工作明确标为 **arXiv 预印本**，不与 OSDI/FAST/ISCA 等正式论文混为一谈。

| Physicalization Primitive | 上层语义 / 物理瓶颈 | HOST/SoC 需要的能力 | 当前关键缺口 | 典型验证场景 | 最终 KPI | 优先级 | **值得参考的论文：全称 + 出处** |
|---|---|---|---|---|---|---|---|
| **1. Compute-layout / Transfer-layout 解耦** | GPU attention 偏好 layer-first；传输偏好 page/object-first、大连续块 | DMA 搬运过程中完成 stride/gather/scatter/relayout/address remap | Generic DMA 不理解 layer/head/token/K/V 结构 | HBM↔DDR/CXL/SSD 长 prefix restore | effective BW、CPU cycles/GB、TTFT、goodput | **P0** | **Strata: Hierarchical Context Caching for Long Context Language Model Serving** — **OSDI 2026**，直接把 GPU compute layout 与 host transfer layout 解耦。([USENIX](https://www.usenix.org/conference/osdi26/presentation/xie-zhiqiang?utm_source=chatgpt.com))；**PAM: Processing Across Memory Hierarchy for Efficient KV-centric LLM Serving System** — **arXiv:2602.11521, 2026 preprint**，重点包含异构 PIM 设备间的 format-aware relayout/migration。([arXiv](https://arxiv.org/abs/2602.11521?utm_source=chatgpt.com)) |
| **2. KV-aware Scatter/Gather DMA** | Paged KV 形成大量离散 segment，底层介质希望大 transaction | 一个 descriptor 描述大量 KV fragments，硬件 coalesce/gather/scatter/bulk DMA | SG 只理解 byte ranges，不理解 prefix/layer/deadline | fragmented KV restore | descriptors/request、IOPS、BW utilization、TTFT | **P0** | **SolidAttention: Low-Latency SSD-based Serving on Memory-Constrained PCs** — **FAST 2026**，核心就是将细粒度 sparse KV access 映射成 coarse SSD blocks。([USENIX](https://www.usenix.org/conference/fast26/presentation/zheng?utm_source=chatgpt.com))；**Strata: Hierarchical Context Caching for Long Context Language Model Serving** — **OSDI 2026**，解决 fragmented KV 导致的小传输问题。([USENIX](https://www.usenix.org/conference/osdi26/presentation/xie-zhiqiang?utm_source=chatgpt.com)) |
| **3. Fine-grained Global KV Addressability** | Sparse Attention 只要 Top-k，但 object/RDMA path 常需要粗粒度搬运 | GPU/CPU 对远端 KV 提供 cache-line/vector 级 load/store | “可寻址”不代表随机 sparse gather 能高效执行；缺 QoS/隔离/layout contract | 64K–1M sparse attention | physical/logical bytes、TBT、TTFT、concurrency | **P0** | **SAC: Disaggregated KV Cache System for Sparse Attention LLMs with CXL** — **arXiv:2606.19746, 2026 preprint**，Top-k KV 直接通过 CXL load/store 获取。([DBLP](https://dblp.dagstuhl.de/rec/journals/corr/abs-2606-19746.html?utm_source=chatgpt.com))；**Beluga: A CXL-Based Memory Architecture for Scalable and Efficient LLM KVCache Management** — **Proceedings of the ACM on Management of Data, 2026**，真实 CXL switch shared-memory pool。([DBLP](https://dblp.uni-trier.de/rec/journals/pacmmod/YangHLLZZLDKZXL26.html?utm_source=chatgpt.com)) |
| **4. Persistent KV Mapping / Stable Descriptor** | 每次 restore 重复 pin/register/address translation/file→LBA lookup | 长生命周期 `KVHandle → physical segments`；SVA/ATS/PASID；stable descriptor | 没有统一的 KV identity + layout + physical address + capability descriptor | 多轮 Agent、重复 prefix restore | setup latency、registration churn、P99 TTFT | **P0/P1** | **Tutti: Making SSD-Backed KV Cache Practical for Long-Context LLM Serving** — **arXiv:2605.03375, 2026 preprint**，预建立 GPU-visible storage mapping/object handle，避免热路径重复解析。([arXiv](https://arxiv.org/abs/2605.03375?utm_source=chatgpt.com))；**Beluga: A CXL-Based Memory Architecture for Scalable and Efficient LLM KVCache Management** — **PACMMOD 2026**，统一地址空间和直接映射是核心设计之一。([DBLP](https://dblp.uni-trier.de/rec/journals/pacmmod/YangHLLZZLDKZXL26.html?utm_source=chatgpt.com)) |
| **5. GPU/NPU-initiated I/O Fast Path** | 数据 DMA 可绕 CPU，但 submission/completion 仍可能由 CPU 驱动 | accelerator-visible SQ/CQ、doorbell、completion；GPU 批量提交 NVMe/RDMA | Data-path bypass 已成熟，**control-path bypass 尚不普遍** | SSD-backed KV restore | CPU%、IOPS、P99 TTFT、request rate、cost | **P0** | **Tutti: Making SSD-Backed KV Cache Practical for Long-Context LLM Serving** — **arXiv:2605.03375**，GPU io_uring / GPU-native object I/O 是最直接案例。([arXiv](https://arxiv.org/abs/2605.03375?utm_source=chatgpt.com))；**No Buffer, No Bottleneck: Efficient Zero-Copy KV Cache Offloading for Long-Context LLMs** — **OSDI 2026**，GPU 直接消费 CPU-resident KV，取消 HBM staging。([USENIX](https://www.usenix.org/conference/osdi26/presentation/luo?utm_source=chatgpt.com)) |
| **6. KV Restore Transaction** | 业务语义是“一次 prefix 在 deadline 前 ready”，底层却看到几千次孤立 I/O | Compound restore descriptor；priority/deadline/destination/partial-ready/group completion | generic I/O 缺 semantic grouping、useful-ready、recompute-OK 等概念 | Agentic prefix restore、高 QPS | useful-restore latency、HoL、P99 TTFT、goodput | **P0** | **ObjectCache: Layerwise Object-Storage Retrieval for KV Cache Reuse** — **arXiv:2605.22850, 2026 preprint**，将 chunk storage 重新聚合为 GPU 消费顺序的 layerwise delivery transaction。([arXiv](https://arxiv.org/abs/2605.22850?utm_source=chatgpt.com))；**Unified KV Pooling to Accelerate Long-Context LLM Serving** — **arXiv:2606.14779, 2026 preprint**，GLT + 并行 restore + SPDK passthrough。([alphaXiv](https://www.alphaxiv.org/abs/2606.14779v1?utm_source=chatgpt.com)) |
| **7. Bandwidth-aware Physical Striping** | 单 DDR/CXL/SSD/NIC 成为 laggard，aggregate BW 无法兑现 | 根据实时 BW 将 KV stripe 到 NUMA/channel/device/SSD/NIC | 现有 interleave 多为静态/capacity-oriented，不理解 GPU-perspective BW 和 deadline | 多 CXL/SSD/DDR offload | aggregate BW、tail completion、TTFT、throughput | **P0** | **AdaptiveKV: Accelerating KV Cache Offloading with a Bandwidth-Adaptive Memory Allocation Mechanism** — **ACM Transactions on Architecture and Code Optimization, 2026**，根据 GPU 视角带宽动态调整 DDR/CXL allocation ratio。([ResearchGate](https://www.researchgate.net/publication/408725878_AdaptiveKV_Accelerating_KV_Cache_Offloading_with_a_Bandwidth-Adaptive_Memory_Allocation_Mechanism?utm_source=chatgpt.com))；**Unified KV Pooling to Accelerate Long-Context LLM Serving** — **arXiv:2606.14779**，根据各 memory/SSD bandwidth 比例分片 KV。([alphaXiv](https://www.alphaxiv.org/abs/2606.14779v1?utm_source=chatgpt.com)) |
| **8. Topology-aware KV Data Path** | SSD→DRAM→GPU 可能经过错误 NUMA/RC/NIC，产生额外 hop 和 contention | 暴露 GPU/NIC/CXL/SSD/NUMA cost graph；route/affinity hint；multi-path | 各子系统知道局部 topology，但 runtime 缺统一 costed topology graph | PD、multi-NIC、remote KV | PCIe/UPI traffic、link util、TTFT、throughput | **P0/P1** | **DualPath: Accelerating Agentic LLM Inference by Harvesting Disaggregated KV-Cache Storage I/O** — **ACM SIGCOMM 2026**，同时利用 PE/DE storage NIC 和 compute network。([SIGCOMM Conferences](https://conferences.sigcomm.org/sigcomm/2026/program/papers/?utm_source=chatgpt.com))；**Mooncake: A KVCache-centric Disaggregated Architecture for LLM Serving** — **ACM Transactions on Storage**，生产系统里的 topology-aware multi-NIC Transfer Engine。([MADSys](https://madsys.cs.tsinghua.edu.cn/publication/mooncake-a-kvcache-centric-disaggregated-architecture-for-llm-serving/?utm_source=chatgpt.com)) |
| **9. Execution-affinity KV Format** | 同一份 CPU KV 可能传 GPU 算，也可能 CPU/PIM inplace compute；要求不同 layout | KV 写入时标记 GPU-/CPU-/PIM-friendly physical region；execution owner 可动态调整 | allocator 只知道地址/容量，不知道未来 compute affinity | CPU-GPU co-attention、PIM attention | migration bytes、TPOT、batch、energy | **P1** | **Physically Partitioned KVCache Format for CPU--GPU Load Balancing in MoE Inference** — **arXiv:2609.14507, 2026 preprint**，最直接提出 physical format 编码 device affinity。([arXiv](https://arxiv.org/abs/2609.14507?utm_source=chatgpt.com))；**CHIME: A Case for Efficient Long-Context Attention-FC Disaggregated Inference with DIMM-PIM** — **ISCA 2026**，KV 固定在 DIMM-PIM，Attention/FC 分配到不同 execution substrate。([IPADS](https://ipads.sjtu.edu.cn/pub/publication?utm_source=chatgpt.com)) |
| **10. Device-side Staging / Prefetch Window** | SSD/CXL-HM latency 无法直接进入 critical path，但用大 DRAM cache 又太贵 | CXL/SSD/HBF controller 内 DRAM/SRAM staging，支持显式 prefetch intent | Generic LRU 看不到 prefix 顺序和未来消费信息 | multi-turn Agent、CXL-HM、SSD prefix store | staging/total-KV ratio、stall、TTFT、$/session | **P1** | **HyMCache: A KV Cache Framework for Multi-Turn LLM Serving with CXL-Hybrid Memory** — **arXiv:2607.18141, 2026 preprint**，明确提出 bounded DRAM staging window 而非普通 capacity cache。([arXiv](https://arxiv.org/abs/2607.18141?utm_source=chatgpt.com))；**ITME: Inference Tiered Memory Expansion with Disaggregated CXL-Hybrid Memories** — **arXiv:2606.12556, 2026 preprint**，device-side DMA prefetch + SSD-backed CXL-HM。([arXiv](https://arxiv.org/abs/2606.12556?utm_source=chatgpt.com)) |
| **11. KV-specific QoS / Deadline Scheduling** | foreground restore、decode recall、spill、prefetch 争用同一 PCIe/CXL/NVMe | per-transaction priority、bandwidth reservation、deadline、read-over-write | NIC/NVMe/memory QoS 各自独立，无法从 inference SLO 映射为 E2E data-path QoS | 高 QPS restore + background spill | P99 TTFT/TPOT、SLO violation、goodput | **P1** | **Bidaw: Enhancing Key-Value Caching for Interactive LLM Serving via Bidirectional Computation–Storage Awareness** — **FAST 2026**，compute/storage 双向感知并把 KV loading latency 纳入调度。([USENIX](https://www.usenix.org/conference/fast26/presentation/hu-shipeng?utm_source=chatgpt.com))；**ObjectCache: Layerwise Object-Storage Retrieval for KV Cache Reuse** — **arXiv:2605.22850**，按 per-layer stall target 分配共享网络 bandwidth。([arXiv](https://arxiv.org/abs/2605.22850?utm_source=chatgpt.com)) |
| **12. KV Physicalization Telemetry** | placement 不知道实时 BW、queue、NUMA contention、read amplification，只能静态猜 | per-path bytes/queue/stall/service-time/useful-bytes telemetry，并能关联 KV/request | PMU/NIC/NVMe counters 丰富，但不是 KV transaction/request aware | adaptive placement/runtime feedback | predictor error、tail stability、adaptive gain | **P1** | **AdaptiveKV: Accelerating KV Cache Offloading with a Bandwidth-Adaptive Memory Allocation Mechanism** — **ACM TACO 2026**，runtime bandwidth characterization + predictor 是非常直接的参考。([ResearchGate](https://www.researchgate.net/publication/408725878_AdaptiveKV_Accelerating_KV_Cache_Offloading_with_a_Bandwidth-Adaptive_Memory_Allocation_Mechanism?utm_source=chatgpt.com))；**Mooncake: A KVCache-centric Disaggregated Architecture for LLM Serving** — **ACM Transactions on Storage**，global scheduler 以 KV location/load/transfer state 作为一等调度信息。([MADSys](https://madsys.cs.tsinghua.edu.cn/publication/mooncake-a-kvcache-centric-disaggregated-architecture-for-llm-serving/?utm_source=chatgpt.com)) |
| **13. Near-memory KV Selection** | Top-k/index scan 如果在 GPU，需要先访问大量远端 metadata/KV，省下的 payload 又被 selection traffic 吃掉 | CXL-PNM/HBF base-die 做 digest scan/filter/Top-k/page selection | 可编程性、多模型共享、一致性、收益边界尚不成熟 | 1M-token sparse attention | CXL/PCIe bytes、TBT、energy/token、GPU batch | **P2** | **Scalable Processing-Near-Memory for 1M-Token LLM Inference: CXL-Enabled KV-Cache Management Beyond GPU Limits** — **PACT 2025**，直接把 token-page selection 下沉到 CXL-PNM。([arXiv](https://arxiv.org/abs/2511.00321?utm_source=chatgpt.com))；**STARC: Selective Token Access with Remapping and Clustering for Efficient LLM Decoding on PIM Systems** — **ASPLOS 2026**，通过 clustering/remapping 将 sparse logical access 映射成 PIM row/bank-friendly access。([DOI](https://doi.org/10.1145/3779212.3790226?utm_source=chatgpt.com)) |
| **14. Near-memory Attention / KV Compute** | KV 很远时，搬 KV 往往比把 Q/结果搬过去更贵 | DIMM-PIM/CXL-PIM/HBF-NDP 执行 QK/SV/reduction | production PagedAttention lifecycle、dynamic batching、sharing 兼容困难 | decode-heavy long-context | TPOT、batch、energy、GPU count、$/token | **P2** | **CHIME: A Case for Efficient Long-Context Attention-FC Disaggregated Inference with DIMM-PIM** — **ISCA 2026**，GPU 做 FC、DIMM-PIM 存 KV 并做 decoding attention。([IPADS](https://ipads.sjtu.edu.cn/pub/publication?utm_source=chatgpt.com))；**PATTON: Enabling Commodity PIM for Production LLM Serving** — **arXiv:2609.11392, 2026 preprint**，解决 PagedAttention block lifecycle 与 PIM physical layout 的冲突。([arXiv](https://arxiv.org/abs/2609.11392?utm_source=chatgpt.com)) |
| **15. HBF/Flash-aware Physical Lifecycle** | NAND 有 page/plane/program/erase/endurance/thermal；不能当作“大 HBM” | plane striping、page construction、write coalescing、lifetime grouping、GC/thermal hints | FTL 不理解 KV session lifetime；runtime 不理解 flash physical geometry | HBM+HBF / full-HBF KV | useful BW、write amplification、GC pause、thermal、tokens/$ | **P2 strategic** | **SPLASH: Co-Designing Sparse Attention with High-Bandwidth Flash for Efficient Long-Context Inference** — **arXiv:2609.23816, 2026 preprint**，page granularity + plane parallelism co-design。([ArcXiv](https://arcxiv.org/abs/2609.23816?utm_source=chatgpt.com))；**HBFlex: A Flexible Memory System for Bridging Fine-Grained LLM States and Coarse-Grained HBF Parallel Execution** — **arXiv:2609.18675, 2026 preprint**，进一步同时覆盖 read/write/GC/lifetime。([arXiv](https://arxiv.org/abs/2609.18675?utm_source=chatgpt.com)) |
| **16. Memory-to-Memory Direct Movement** | DDR↔CXL↔SSD/HBF↔peer memory 经 CPU/GPU bounce 会吃 Host BW 和增加 latency | device/switch/DPU P2P DMA，并在移动过程中完成 address generation/relayout | 有 PCIe/CXL/DMA 基础，但缺跨设备 semantic descriptor、security/failure/layout conversion | multi-tier promotion/demotion、PD handoff | host BW、CPU cycles、migration latency | **P1/P2** | **ITME: Inference Tiered Memory Expansion with Disaggregated CXL-Hybrid Memories** — **arXiv:2606.12556**，硬件 DMA pipeline 将 SSD-backed CXL-HM 向 Host/GPU 主动 staging。([arXiv](https://arxiv.org/abs/2606.12556?utm_source=chatgpt.com))；**PAM: Processing Across Memory Hierarchy for Efficient KV-centric LLM Serving System** — **arXiv:2602.11521**，PAM interface 用 sender command reorder + receiver address generation + relayout buffer 完成异构 memory 间直接 format-aware migration。([arXiv](https://arxiv.org/abs/2602.11521?utm_source=chatgpt.com)) |

### 从参考论文反推，P0 其实进一步收敛成了 4 个硬件课题

我会特别关注表中论文的**交叉重合**。如果同一种底层能力被不同论文、不同介质、不同 workload 独立“重新发明”，往往意味着这里存在真正的通用硬件抽象缺口。

**第一，`KV-aware Relayout + Scatter/Gather Data Mover`。** Strata、SolidAttention、PAM、Tutti 都在以不同形式解决同一个问题：

$$
\text{Logical KV Layout}
\neq
\text{Transfer Layout}
\neq
\text{Consumer Layout}
$$

这已经不是 SSD 或 CXL 专属问题，而是通用 HOST/SoC primitive。

**第二，`KV Restore Transaction + Stable Descriptor`。** ObjectCache、Unified KV Pooling、Tutti、Beluga 的共同趋势，是不能再让底层只看到独立的 `(addr,len)`：

$$
\boxed{
KVHandle +
Layout +
Segments +
Destination +
Deadline +
ReadySemantics
}
$$

可能才是更合理的硬件/OS contract。

**第三，`Topology/Bandwidth/QoS-aware Multi-path Engine`。** AdaptiveKV、DualPath、Mooncake、Beluga 都在软件层做 topology discovery、bandwidth balance、route selection。这很像过去 CPU 从软件 memcpy/IO 调度逐渐演进出 DMA、IOMMU、RSS、多队列一样——目前 KV 领域还缺少统一的“movement resource scheduler”。

**第四，`Near-memory Selection` 可能比 `Near-memory Attention` 更值得先做。** STARC 和 CXL-PNM 的结果让我更倾向于先把：

$$
\text{scan/filter/index/TopK}
$$

下沉到 memory side，而不是一开始就在 CXL DIMM 上实现完整 attention。因为前者计算简单、接口稳定，而且能直接减少：

$$
Bytes_{CXL/PCIe}
$$

；后者则要面对 softmax、precision、kernel evolution、模型结构变化等更大的硬件固化风险。([DOI](https://doi.org/10.1145/3779212.3790226?utm_source=chatgpt.com))

所以，这张表现在已经可以作为下一步 HOST 体系结构探索的 **literature-backed feature map**：每个 feature 后面都有至少一条已经证明业务问题存在的证据链，而不是从 CPU ISA 或 SoC feature 倒推“哪里能用”。这点对后续筛选真正值得投入的研究课题非常重要。