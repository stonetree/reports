# you asked

message time: 2026-10-13 12:03:20

再进一步整理一张上面说的表格

---

# chatgpt response

我会把上一张“论文证据矩阵”进一步翻译成一张 **HOST/SoC 设计需求表**：不再按论文，而按可落地的 `physicalization primitive` 分解，明确 **上层要表达什么语义、底层需要什么硬件机制、现有平台是否已有基础、缺口在哪里、用什么业务场景验证、最终看什么 KPI**。

可以。把上一张“论文 → KPI”矩阵进一步抽象以后，我认为真正应该形成的不是一张“CPU feature wish list”，而是一张 **KV Physicalization 在 HOST/SoC 上的能力地图**。

先说明一个判断：现有平台其实已经有不少“积木”——CXL 3.0 已定义 memory pooling/sharing、fabric 和 peer-to-peer memory access；Intel DSA 已能做异步 data movement、scatter-gather，并支持 SVM；NVIDIA GDS 已支持 storage↔GPU 直接 DMA；GH200 则已经展示 coherent CPU-GPU memory + 高带宽 C2C 的形态。([Compute Express Link](https://computeexpresslink.org/wp-content/uploads/2023/12/CXL_3.0_white-paper_FINAL-1.pdf?utm_source=chatgpt.com))

**真正缺失的并不是 DMA、CXL、IOMMU 这些基础机制，而是把这些机制组合成“KV-aware physicalization contract”。**

## KV Physicalization → HOST/SoC 硬件能力矩阵

| Physicalization Primitive | 上层真正要表达的语义 / 当前瓶颈 | HOST/SoC 需要的底层能力 | 当前已有基础 | 关键缺口 | 最适合的验证场景 | 最终 KPI | 优先级 |
|---|---|---|---|---|---|---|---|
| **1. Compute-layout / Transfer-layout 解耦** | GPU attention 希望 `layer-first/compute-friendly`；跨 DDR/SSD/NIC 搬运希望 `page/object-first/contiguous`；同一 layout 无法同时满足 | **在线 relayout engine**：DMA 搬运过程中完成 stride/gather/scatter/transpose/address remap，不经 CPU core | DMA/DSA 已支持 copy、scatter-gather 等基础操作；Strata/PAM 已证明软件/专用硬件 relayout 有价值。Intel DSA 本身已有数据搬运/变换基础。([Intel](https://www.intel.com/content/www/us/en/products/details/processors/xeon/features/data-streaming-accelerator.html?utm_source=chatgpt.com)) | 现有 DMA 不理解 `layer/head/token/K/V`；缺少 KV descriptor → physical descriptor 的硬件执行模型 | HBM↔DDR、DDR↔CXL、DDR↔SSD 的长 prefix restore | effective BW、CPU cycles/GB、TTFT、SLO goodput | **P0** |
| **2. KV-aware Scatter/Gather DMA** | KV block 在 GPU 视角是大量离散 page，而存储/NIC 需要大 transaction；tiny I/O 放大 | 一个 descriptor 描述数百/数千 KV fragment；硬件进行 **coalesce + gather/scatter + bulk DMA** | SG DMA、IOMMU、DSA、NIC SG list 都已有 | SG list 是 generic byte ranges，不知道哪些 range 属于同一 prefix/layer/deadline；descriptor 提交/完成粒度仍过细 | Strata/Tutti/SolidAttention 类型 restore | IOPS↓、descriptor/request↓、BW utilization↑、TTFT↓ | **P0** |
| **3. Fine-grained Global KV Addressability** | Sparse Attention 只需要 Top-k，但 RDMA/object path 往往要搬整个 block/prefix | CPU/GPU 对 remote/CXL KV 提供 **load/store / cache-line or vector granularity** addressability | CXL 3.0 已支持 pooling/sharing、fabric、P2P；GH200 展示 coherent heterogeneous memory。([Compute Express Link](https://computeexpresslink.org/wp-content/uploads/2023/12/CXL_3.0_white-paper_FINAL-1.pdf?utm_source=chatgpt.com)) | 缺少面向 GPU sparse gather 的 KV layout、访问权限、QoS、multi-tenant isolation；“能寻址”≠“能高效随机访问” | SAC、HiSparse 类 native sparse attention，64K–1M context | physical bytes/logical bytes、TBT、TTFT、concurrency | **P0** |
| **4. Persistent KV Mapping / Stable Descriptor** | 每次 restore 都重复做 address translation、pin/register、MR setup、file→LBA lookup | 长生命周期的 `KVHandle → physical segments` 映射；支持 SVA/ATS/PASID/IOMMU，设备可以持久使用 virtual descriptor | PASID/ATS/SVM 已有；DSA descriptor 可直接接受 virtual address；GH200 ATS 支持 CPU/GPU 共用进程页表。([PCI-SIG](https://pcisig.com/PCIExpress/ECN/Base/PASIDTranslation?utm_source=chatgpt.com)) | 没有标准的 **KV object identity + address + layout + capability** descriptor；不同 NIC/SSD/GPU 重复维护元数据 | 多轮 Agent，同一 prefix 反复 restore | setup latency、registration churn、CPU cycles/request、P99 TTFT | **P0/P1** |
| **5. GPU/NPU-initiated I/O Fast Path** | SSD/NIC 数据可以直接 DMA 到 GPU，但 CPU 仍常在每个 I/O 的 submission/completion path | accelerator-visible SQ/CQ、doorbell、completion；GPU/NPU 可批量提交 NVMe/RDMA restore | GDS 已支持 storage↔GPU direct DMA，GPUDirect RDMA 支持 GPU↔NIC direct data path。([NVIDIA Docs](https://docs.nvidia.com/gpudirect-storage/index.html?utm_source=chatgpt.com)) | **data path 已能绕 CPU，但 control path 未必绕 CPU**；缺少 KV transaction-level submission、deadline/completion grouping | Tutti、SSD-backed LMCache/HiCache | CPU%↓、IOPS↑、TTFT P99↓、request rate↑、cost↓ | **P0** |
| **6. KV Restore Transaction** | 上层不是“读 1000 个 block”，而是“在 deadline 前让 request X 的 layer 0…N 可消费” | SoC/OS 接受一个 **compound restore descriptor**：多个 segment、优先级、deadline、destination、partial-ready semantics | io_uring、DMA queues、NIC work queues 已有基本异步机制 | generic I/O 缺少 **semantic grouping / useful-ready / partial completion / recompute-acceptable** 概念 | Agentic prefix restore、高 QPS、多请求并发 | useful-restore latency、P99 TTFT、HoL blocking、goodput | **P0** |
| **7. Bandwidth-aware Physical Striping** | 单 DDR channel、单 CXL device、单 SSD、单 NIC 很容易成为 laggard；总 BW 很高但实际只用一部分 | 按实时可用 BW 将 KV stripe 到 NUMA/channel/CXL device/SSD/NIC；硬件支持细粒度 interleave | NUMA/interleaving、CXL device pooling 已有；CXL 3.0 fabric 为共享/池化奠定基础。([Compute Express Link](https://computeexpresslink.org/wp-content/uploads/2023/12/CXL_3.0_white-paper_FINAL-1.pdf?utm_source=chatgpt.com)) | 传统 interleave 静态、capacity-oriented；KV 需要 **GPU-perspective BW + topology + request deadline** aware | Unified KV Pooling、AdaptiveKV、Beluga | aggregate BW、tail completion、TTFT、throughput | **P0** |
| **8. Topology-aware KV Data Path** | KV 从 SSD→DRAM→GPU 可能经过错误 NUMA、Root Complex、NIC，导致额外 hop 和 PCIe contention | SoC 暴露 GPU/NIC/CXL/SSD/NUMA 拓扑；支持 route hint / destination affinity / peer path | PCIe/CXL topology、GPUDirect P2P、NUMA affinity 都已有 | runtime 看不到统一的 **costed topology graph**；硬件也不能根据 KV transaction 动态选 path | DualPath、Mooncake multi-NIC、Beluga | PCIe/UPI traffic↓、link utilization↑、TTFT/throughput | **P0/P1** |
| **9. Execution-affinity KV Format** | CPU-resident KV 可能“搬 GPU 算”也可能“CPU/PIM 就地算”；两种计算要求不同 physical layout | KV 写入时可指定 `GPU-friendly / CPU-friendly / PIM-friendly` physical region；支持 execution owner 快速切换 | NUMA placement、HBM/DDR pinning、heterogeneous address spaces 已有 | 当前 allocator 主要理解地址/容量，不理解 **future compute affinity** | InplaceKVCache、ScoutAttention、CPU/GPU co-attention | KV migration bytes、TPOT、batch、energy | **P1** |
| **10. Device-side Staging / Prefetch Window** | SSD/CXL-HM/HBF 延迟不能直接进入关键路径；但完整 DRAM cache 又太贵 | CXL/SSD/HBF controller 内小 DRAM/SRAM staging；支持 runtime 发 `prefetch(KVHandle, deadline)` | SSD DRAM cache、CXL-HM prototype、DMA engine 都有类似 primitive | generic cache 的 LRU 无法利用 prefix 顺序和未来消费信息；需要 explicit intent API | HyMCache、ITME、ObjectCache | cache DRAM/total KV ratio、stall time、TTFT、$/session | **P1** |
| **11. KV-specific QoS / Deadline Scheduling** | foreground restore、decode recall、background spill、prefetch 都走同一 DDR/PCIe/CXL/NVMe，互相干扰 | DMA/NIC/CXL/SSD queues 支持 priority、bandwidth reservation、deadline、read-over-write policy | PCIe/NVMe queues、memory QoS、NIC QoS 都有局部机制 | QoS domain 相互独立；缺少从 inference request SLO 到 **端到端 data-path QoS** 的映射 | 高 QPS + SSD/HBF writeback + foreground restore | P99 TTFT/TPOT、SLO violations、goodput | **P1** |
| **12. KV Physicalization Telemetry** | Placement policy 如果不知道实际 BW、queue depth、NUMA congestion、read amplification，就只能静态猜 | per-device/per-path：bytes、queue、stall、retry、read amplification、useful bytes、service time counters | PMU、PCIe/CXL/NVMe/NIC counters 已很多 | counters 是设备导向而不是 **KV transaction/request/layer** 导向；无法闭环 PlacementResolver | 所有 heterogeneous KV pool | prediction error、tail stability、adaptive placement gain | **P1，基础设施** |
| **13. Near-memory KV Selection** | Sparse attention Top-k 选择本身若仍在 GPU，需要先访问大量远端 metadata/KV | CXL-PNM/HBF base-die 执行 digest scan、Top-k/filter/index | PIM/PNM prototypes 已经大量出现，但通用产品能力有限 | 可编程性、precision、metadata consistency、多模型共享、成本收益边界尚未成熟 | 1M-token sparse attention | bytes crossing CXL/PCIe、TBT、energy/token、GPU batch | **P2** |
| **14. Near-memory Attention / KV Compute** | KV 离 GPU 很远时，与其拉数据不如计算靠近数据 | DIMM-PIM/CXL-PIM/HBF-NDP 执行 QK/SV/reduction，GPU 只交换 Q/output | 主要为研究/专用硬件阶段 | 与 production PagedAttention lifecycle、动态 batching、prefix sharing 的兼容性仍是难点 | CHIME/L3/PAM/PATTON | TPOT、batch、energy、GPU count、$/token | **P2** |
| **15. HBF/Flash-aware Physical Lifecycle** | NAND 有 page/plane/program/erase/endurance/thermal；简单当“大容量 HBM”会失败 | plane-aware striping、page construction、write coalescing、lifetime grouping、GC hint、thermal/endurance budget | SSD controller 具备类似 FTL 基础，但 HBF 尚处早期 | FTL 不理解 KV session lifetime / attention access；runtime 不理解 flash physical geometry | SPLASH/HBFlex | useful BW、write amplification、GC pause、thermal throttle、tokens/$ | **P2 strategic** |
| **16. Memory-to-Memory Direct Movement** | DDR↔CXL↔SSD/HBF↔peer memory 经 CPU/GPU 中转浪费带宽和容量 | memory device / CXL switch / DPU 之间 P2P DMA，目标设备直接完成 relayout/write | CXL 3.0 已包含 peer-to-peer direct memory access；PCIe P2P 也有基础。([Compute Express Link](https://computeexpresslink.org/wp-content/uploads/2023/12/CXL_3.0_white-paper_FINAL-1.pdf?utm_source=chatgpt.com)) | 缺少跨厂商可组合的 semantic descriptor、security、failure semantics、layout conversion | multi-tier demotion/promotion、PD handoff | host BW↓、CPU cycles↓、migration latency↓ | **P1/P2** |

---

## 这张表里，我认为真正应该优先研究的是前六项

如果从“**能不能做出 HOST/SoC 独有价值**”而不是论文 novelty 来排序，我会把重点收敛成：

$$
\boxed{
\text{KV-aware Data Movement Engine}
}
$$

它不是一个更快的 memcpy engine，而应该具备：

$$
\begin{aligned}
&\text{Semantic Descriptor}\\
+&\text{Address Translation}\\
+&\text{Scatter/Gather}\\
+&\text{Relayout}\\
+&\text{Topology Routing}\\
+&\text{QoS/Deadline}\\
+&\text{Completion Semantics}
\end{aligned}
$$

这是我认为目前最有可能从已有 CPU/SoC 架构继续向前推进的一块。

为什么？

因为今天硬件其实已经分别存在：

- DSA/DMA：会搬数据；
- IOMMU/SVM/ATS/PASID：会翻译地址；
- CXL：会提供异构/共享内存语义；
- GDS/GPUDirect：会绕过 CPU 数据路径；
- NIC/NVMe：会异步 queue；
- NUMA/MPAM/QoS：会做部分资源控制。

但是它们看到的都是：

```text
src_addr
dst_addr
length
```

而 KV runtime 真正知道的是：

```text
KV object = {
    request/session,
    prefix,
    layer,
    head,
    token_range,
    representation,
    current_tier,
    target_tier,
    consumer,
    reuse_probability,
    deadline,
    priority
}
```

中间存在一个非常明显的 **semantic gap**。

---

# 我认为真正值得定义的是一个“KV Move Descriptor”

这可能比先想一条新 CPU 指令更有意义。

概念上可以是：

```text
KVMoveDesc {
    kv_object_id

    src {
        tier
        device
        address
        layout
    }

    dst {
        tier
        device
        address
        layout
    }

    logical_range {
        layer
        head
        token_begin
        token_end
    }

    transform {
        gather
        scatter
        relayout
        precision_convert
    }

    access_intent {
        consumer
        deadline
        priority
        prefetch
        foreground_restore
        background_spill
    }

    completion {
        whole_object
        per_layer
        partial_ready
    }
}
```

然后：

$$
KVMoveDesc
\rightarrow
HOST/SoC\ Data\ Movement\ Engine
$$

而不是：

$$
KVMoveDesc
\rightarrow CPU\ for\ loop
\rightarrow memcpy/RDMA/io\_uring...
$$

这里就出现了真正的软硬协同切入点。

---

# 进一步看，HOST CPU 的角色也因此可以重新划界

我不会再把未来 HOST 的定位简单写成：

> CPU 负责 KV control plane。

这个说法太粗。

更准确应该分成三层：

| 层 | 谁负责 | 内容 |
|---|---|---|
| **Semantic Decision** | CPU/runtime | 哪份 KV 值得保留、放哪、什么时候取、SLO/tenant/policy |
| **Physicalization Planning** | CPU + specialized runtime | representation、layout、target path、transfer unit、execution affinity |
| **Physical Execution** | DMA/CXL/NIC/SSD/HBF controller | address translation、SG、relayout、copy、striping、queue、completion |

也就是说：

$$
\boxed{
CPU\ decides\ intent,\quad
SoC\ executes\ physicalization
}
$$

这比“CPU 自己搬 KV”合理得多。

---

# 其中我最看好的三个 HOST/SoC 研究课题

**第一是 KV-aware relayout DMA。**  
Strata、PAM、SolidAttention、KVDrive 已经反复证明 `compute layout != transfer/storage layout`。现在大部分系统仍然用 GPU kernel、CPU loop 或专用软件解决。这是最容易形成通用硬件 primitive 的地方。

**第二是 KV Restore Transaction。**  
现在 OS/NVMe/RDMA 看到的是成千上万个 I/O，但业务看到的是“一次 prefix restore”。如果 CPU/SoC 能让这个 transaction 带着 `deadline + locality + destination + partial-ready` 下沉，可能直接影响 P99，而不只是平均带宽。

**第三是 topology/bandwidth-aware multi-path data mover。**  
DDR channel、CXL、SSD、NIC、GPU Root Complex 已经越来越复杂。DualPath、Beluga、AdaptiveKV、Unified KV Pooling 都在软件层重复做同一件事：找空闲带宽、striping、绕拥塞、选择路径。这非常像一个正在形成、但硬件还没有正式抽象出来的资源。

---

因此，如果下一步要把这张表真正转成你们的 **HOST CPU / SoC 技术探索路线图**，我会建议不要从“HBF、CXL、PIM 各做一个方向”来分。

更好的划分是：

$$
\boxed{
\text{Address}
\rightarrow
\text{Layout}
\rightarrow
\text{Movement}
\rightarrow
\text{Path}
\rightarrow
\text{QoS}
\rightarrow
\text{Completion}
}
$$

**介质只是执行这些 physicalization primitive 的不同 substrate。**

这个抽象层次，我认为已经开始比单纯的 KVCache tiering 更接近一个真正能落到 CPU/SoC 架构设计上的长期方向。