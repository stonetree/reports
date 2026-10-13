> From: https://chatgpt.com/c/6ac856aa-d94c-83e8-a0dc-6db23cb47568

# you asked

message time: 2026-10-09 10:57:12

我在思考一个问题，研究围绕KVC的优化技术，这些技术确实能产生重要落地技术吗？我们当前研究的这些技术方向，真的有价值吗？特别是这些优化技术不能仅仅停留在上层软件，要能深入到计算机体系架构层面，以及芯片SOC层面。如何真正利用到这些底层技术做优化，现在我们在研究的这些优化技术能下层到这个层级吗？譬如之前讨论的IO stash能力，在部分场景可以解决DDR内存带宽瓶颈问题，这个就是深入到底层的技术。但我感觉当前计划验证的这些技术都还没有到这个层面。当然，也有可能我们才刚开始，随着技术探索的不断深入，对问题和技术的认识会也会不断深入，但从目前外界研究进展开，有这方面深入研究的论文或是案例吗？

---

# chatgpt response

我认为你的担心是成立的，而且看完你这版 V10 之后，我会把问题再收敛一步：

> **当前这批 KVCache 技术方向并不是“没有价值”，也不是“下沉不到体系结构”；真正的问题是：现在的验证框架主要在回答“软件优化能不能改善 TTFT/QPS/TPOT”，还没有把“能否改变芯片内部的数据运动、缓存、互连、I/O 和控制路径”作为一级研究目标。**
>
> 换句话说，**我们目前验证到的是 system optimization，下一步必须主动进入 architecture exploration。**

而从 2026 年外界的研究和产业进展看，已经出现了一批非常明确的案例：KVCache 研究正在从 `scheduler/cache policy`，快速向 **CXL、NVLink-C2C、GPU direct I/O、DPU、RNIC、CXL-SSD、memory controller、专用 KV accelerator** 下沉。这说明这条路不但存在，而且很可能正是下一阶段 KVC 技术真正拉开差距的地方。

---

## 一、先从第一性原理判断：KVC 为什么有资格成为“体系结构问题”

把 KVCache 抽象掉“LLM”这个名字，它本质上是一个非常特殊的数据对象：

**容量巨大、生命周期明确、访问高度可预测、读多写少、分层明显、消费顺序与 Transformer layer 强相关，而且它的价值不是数据本身，而是“避免重算”。**

因此它实际上同时撞上了五面硬件墙：

$$
T_{KV}\approx
T_{lookup}
+T_{translation}
+T_{queue}
+\frac{Bytes}{BW}
+T_{sync}
-T_{overlap}
$$

真正决定它性能的，并不只是 vLLM 或 Mooncake 写得好不好，而是：

**数据经过多少次 DDR、多少次 PCIe/C2C/NoC、是否经过 CPU cache、是否需要 staging buffer、DMA 能不能 scatter/gather、IOMMU/SMMU 是否成为瓶颈、NIC/DPU 能不能直接处理、内存控制器有没有 QoS、设备能否理解 KV block/layer 的语义。**

所以，只要 KVC 足够大，它迟早会从“软件数据结构问题”变成：

> **memory hierarchy + data movement + coherence + I/O + control offload 问题。**

这和数据库最终会逼出 PMEM/CXL/NDP、网络最终会逼出 SmartNIC/DPU，本质上是同一条演进路线。

---

# 二、你的直觉为什么会觉得“我们现在还没有做到这个层次”

这个感觉不是错觉。

你当前 V10 中其实已经有一些非常靠近体系结构的候选，例如：

CPU 零数据拷贝明确提出“注册设备内存，由设备 DMA，CPU 只下发控制”；描述符合并与异步流水；前后台资源控制；以及可选设备卸载。 

但真正进入三周主验证路径的六条，主要还是：

**元数据快路径、传输与多通道、描述符与流水、动态选路、前后台资源控制、组合验证。**

这里存在一个微妙但非常重要的问题：

**“涉及硬件” ≠ “研究硬件机制”。**

例如现在的“传输带宽提高”，如果最后只是：

> Mooncake 调整 batch size → 从 10 GB/s 提升到 42 GB/s → TTFT 降低

那么它仍然是系统软件优化。

但如果继续追：

> 为什么 10 GB/s？  
> 是单 WQE 太小？  
> doorbell 太频繁？  
> DMA outstanding 不足？  
> PCIe MPS/MRRS？  
> IOTLB miss？  
> NUMA 路径？  
> Host DDR 被读写两遍？  
> NIC→DDR→GPU，还是 NIC→GPU？  
> DMA descriptor fetch 是否打到 DDR？  
> 是否能 stash descriptor 到 LLC？  
> 是否能由 DPU 直接提交 GPU DMA？

这时候才真正进入体系结构研究。

所以我会把目前状态定义为：

> **技术方向里已经埋着 architecture hooks，但验证方法还没有把它们挖出来。**

---

# 三、而且你自己的模型其实已经给了一个很强的信号

你 V10 第 7 页的 70% 前缀场景贡献分解非常值得重新看：

| 优化 | 模型贡献 |
|---|---:|
| 元数据 | 0.30 ms |
| 描述符/提交 | 13.40 ms |
| 按层异步流水 | 26.81 ms |
| 动态选路 | -0.05 ms |



虽然这些数字现在还是场景模型，不是最终实测，但它揭示的方向很有意义：

**越靠近 data movement / execution pipeline 的优化，潜在收益越大；越靠近纯 control-plane policy 的优化，单请求直接收益越小。**

这和体系结构直觉是高度一致的。

所以我不会停止元数据优化，但会改变它的研究定位：

> 元数据研究的最终目标不应该只是“hash 快 30%”，  
> 而应该继续问：
>
> **metadata/control state 是否可以进入 cache / SLC / RNIC / DPU / memory-side engine？**

这才可能产生芯片价值。

---

# 四、外界已经出现了几类非常值得关注的“真正下沉”的 KVC 工作

这里我认为有几篇/几类工作，和你现在的问题高度相关。

## 1. DirectKV：这是非常典型的“吃透硬件之后重新设计 KV path”

OSDI 2026 的 **DirectKV** 基于 GH200/GB200 的 NVLink-C2C，不再遵循传统：

CPU DRAM → GPU staging buffer → attention

而是让 GPU kernel **直接访问 CPU-resident KV**，然后针对 CPU memory 的访问特征重新设计 CUDA kernel、tiling、warp pipeline，并将 KV generation / attention 做融合。

结果是在 GH200 上，CPU-GPU 数据传输最多减少 50%，GPU memory 使用减少 43%，端到端性能最高约 1.2×。([USENIX](https://www.usenix.org/conference/osdi26/presentation/luo))

这篇论文最重要的不是那 1.2×。

而是它体现了一个极其重要的方法论：

> **不是“在现有软件路径上优化 memcpy”，而是因为 NVLink-C2C 出现了，于是重新定义 KV 的存储位置和 attention kernel。**

这才是真正的软硬协同。

---

## 2. Beluga：直接改变 KVC memory architecture

Beluga 使用 CXL switch 构建 CPU/GPU 可共享的大容量内存池，让 GPU/CPU 通过 CXL 使用原生 load/store 语义访问共享 KV memory，而不是把所有远端内存都抽象成 RDMA object transfer。

论文报告相对于其 RDMA baseline，Beluga-KVCache TTFT 降低 89.6%、吞吐提升 7.35×。([arXiv](https://arxiv.org/abs/2511.20172))

这里研究的已经不是：

> KV 放在哪一台 server？

而变成：

> **GPU address space / CXL fabric / shared memory / coherence / switch topology 应该如何为 KV 服务？**

这就是典型的 architecture problem。

---

## 3. LM-CXD：这篇尤其值得你重点关注

这是 2026 年 9 月刚出来的工作 **LM-CXD**。

它发现普通 CXL-SSD 即使使用 CXL，依然没有把 KV workload 的语义利用起来；于是直接设计 **KV-aware CXL SSD**：

把 KV chunk 变成设备可见的 I/O unit；

让设备向上暴露 NAND→DRAM 的执行进度；

利用 SSD 自己的 DRAM 作为 GPU-accessible buffer；

并让 serving scheduler 和设备共同做 windowed/layerwise prefetch。

最终 TTFT 相对于普通 CXL-SSD 最多降低 2.6×/4.03×。([arXiv](https://arxiv.org/abs/2609.26828))

这篇工作我认为和你们现在的课题尤其重要。

它提出的其实就是一个核心思想：

> **上层知道“下一层 Transformer 什么时候需要哪个 KV”，设备知道“数据什么时候从 NAND 到 DRAM”。**
>
> 过去这两个信息彼此不可见。
>
> 真正的优化，是把这两个语义打通。

这就是我认为你们统一 KVC pool 最应该向下发展的方向。

---

# 五、产业界其实已经比论文走得更激进了

NVIDIA 2026 年的 **CMX Context Memory Storage** 已经明确把 KV cache 定义成一种新的 AI-native data class。

它用 BlueField-4 构建一个专门位于 host/GPU memory 和传统 shared storage 之间的 **G3.5 context tier**。BlueField-4 负责 KV I/O plane，终止 NVMe-oF / object-RDMA，执行 KV movement，并通过硬件引擎进行 CRC、encryption、integrity，减少 host CPU 和 host-memory copy。([NVIDIA Developer](https://developer.nvidia.com/blog/introducing-nvidia-bluefield-4-powered-inference-context-memory-storage-platform-for-the-next-frontier-of-ai/))

NVIDIA 官方甚至明确写道：

> BlueField-4 用于加速 **KV I/O 和 control-plane operations**，降低 host CPU involvement 和 serialization。([NVIDIA Developer](https://developer.nvidia.com/blog/introducing-nvidia-bluefield-4-powered-inference-context-memory-storage-platform-for-the-next-frontier-of-ai/))

这已经完全不是“一个 KVConnector”的问题了。

而是：

> **KVCache 正在推动一种新的 storage/DPU/SoC architecture。**

AMD 今年也已经推出 ROCm AMD Infinity Context。其 hipFile 路径可以让 network storage → GPU HBM，**绕过 host CPU memory bus**；AIC 将 LMCache、NIXL、hipFile、RDMA NIC 和 storage tier 连接起来。([ROCm 博客](https://rocm.blogs.amd.com/software-tools-optimization/amd-infinity-context/README.html))

AMD 最近甚至公开把 DPU 定位成 agentic inference 中负责 KV orchestration、storage protocol 和 data movement 的 infrastructure processor。([AMD](https://www.amd.com/en/blogs/2026/how-agentic-ai-is-expanding-the-role-of-the-dpu-.html?utm_source=chatgpt.com))

所以从 NVIDIA/AMD 的动作看，答案已经相当明确：

> **KVC 不再只是 inference framework 功能，它正在成为芯片和系统架构设计输入。**

---

# 六、你提到的 I/O stash，是一个非常好的参照物，但需要注意它最适合优化什么

Arm CHI 的 Cache Stashing 允许 I/O device/accelerator 把数据直接 stash 到 CPU cache，以便 CPU 消费时不必再从外部内存读回来。Arm 官方描述的典型用途就是 networking/storage packet header 等延迟敏感数据。([Arm 开发者](https://developer.arm.com/community/arm-community-blogs/b/soc-design-and-simulation-blog/posts/introducing-new-amba-5-chi-protocol-enhancements?utm_source=chatgpt.com))

Intel DSA 也有类似思想：DSA 可以选择把 copy 的 destination 放进 LLC，而不是只写 DRAM，可作为硬件 prefetch/data mover 使用。([英特尔](https://www.intel.com/content/www/us/en/developer/articles/technical/accelerate-memory-bandwidth-bound-kernels-with-dsa.html?utm_source=chatgpt.com))

但这里我要稍微修正一个容易形成的直觉：

**I/O stash 很可能不是 bulk KV payload 的最佳技术。**

因为数 GB KV 如果全部 stash：

cache capacity 根本不够，而且会污染 CPU LLC/SLC。

I/O stash 更适合：

**descriptor、metadata、completion、queue state、prefix-index node、control message。**

而大块 KV 正文真正应该做的是：

> **尽量不进入 DDR。**

也就是：

NIC / SSD / remote memory  
→ GPU/NPU HBM

而不是：

NIC  
→ DDR  
→ GPU/NPU。

这也是为什么 GPUDirect Storage、AMD hipFile、DirectKV、Beluga、CMX 最终都在解决 **data-path bypass**。

---

# 七、因此我会把你们现在的六个方向重新评价一次

| 当前方向 | 当前层次 | 下沉潜力 | 真正值得研究的 SoC/architecture 问题 |
|---|---|---|---|
| Metadata fast path | 目前主要软件 | **中高** | LLC/SLC residency、I/O stash、hash/index offload、RNIC/DPU lookup |
| 传输/多通道 | 已接近硬件 | **极高** | DDR bypass、P2P DMA、CXL/C2C、IOMMU、NIC/PCIe/NoC topology |
| Descriptor batching | 软件+DMA boundary | **极高** | DMA descriptor engine、SG capability、doorbell、queue depth、IOTLB |
| Layer pipeline | runtime | **极高** | layer-aware prefetch、device DMA、kernel/data-movement co-design |
| Dynamic routing | 主要软件 | **中** | hardware telemetry、congestion/path feedback、DPU steering |
| 前后台资源控制 | 软件 QoS | **高** | NIC queue、NoC/MC QoS、MPAM、BW partition、hardware credits |

所以我不会推翻当前六项。

但如果目标真的是：

> **最后给 CPU/SoC/NIC/DPU/NPU 芯片提出有价值的架构需求**

那这六项现在缺一个纵向维度：

### 从“功能优化项”升级成“硬件假设”。

---

# 八、我建议下一阶段增加一条非常重要的研究主线

不是再增加十个 KV 算法，而是针对当前技术做：

> **KVCache Hardware Opportunity Mining**

而且我建议只抓三个问题。

### ① KV 正文到底经过了哪些物理路径？

把：

GPU/NPU ↔ DDR ↔ NIC ↔ remote DDR/SSD

真正画成 **SoC dataflow**。

实际采集：

DDR read/write bytes、PCIe/C2C traffic、NoC traffic、DMA outstanding、IOMMU/TLB、NUMA hop、CPU cache traffic。

最后问一句：

> **这里有没有一份数据根本不应该进 DDR？**

这是最可能产生类似 GPUDirect / hipFile / C2C 的架构需求的地方。

---

### ② KV control path 里什么应该进入硬件？

例如：

prefix descriptor、metadata lookup、lease/refcount、completion、CRC、地址转换、allocation/free。

这类东西数据量小、访问频繁、语义稳定。

非常适合：

IO stash  
LLC placement  
DSA  
DPU/RNIC  
CXL-device accelerator  
far atomic。

NSDI 2026 的 OneSidedMW 就是一个很好的参考：它利用 RNIC offload + Memory Window，让 remote memory allocation/deallocation 不经过 memory-node CPU，KV-store workload最高获得 10.6× 改善。([USENIX](https://www.usenix.org/conference/nsdi26/presentation/wang-zixuan?utm_source=chatgpt.com))

这和你们未来做 KV `lease / attach / allocation / ownership` 很接近。

---

### ③ 让硬件“理解一点点 KV 语义”

不是把 vLLM scheduler 做进芯片。

这会很危险，因为算法变化太快。

真正适合进入硬件的是稳定语义：

`block/chunk`  
`layer`  
`priority`  
`deadline`  
`hot/cold`  
`read/write`  
`progress`  
`tenant`  
`consumer`

然后硬件提供：

DMA、prefetch、QoS、cache hint、telemetry、atomic、encryption、CRC、address translation。

而策略仍由软件决定。

LM-CXD、NEMO、CMX 本质上都是这个思路。NEMO 甚至直接在 memory controller 附近增加 programmable telemetry engine，让 OS 以更低开销看到 memory behavior。([USENIX](https://www.usenix.org/conference/osdi26/presentation/li-shihang))

这比“把缓存算法硬化”要成熟得多。

---

# 九、所以我现在对这个项目的判断其实比之前更积极，但研究目标需要再升一级

我不会得出：

> “目前这些 KVC 优化还不够底层，所以价值有限。”

我会得出一个稍微不同的结论：

> **当前项目正处在一个很合理的第一阶段——先确认 KV 的宏观收益链路；但如果项目最终定位是“统一异构 KVCache 存储池 + 软硬协同”，三周验证完成以后，必须立刻进入第二阶段：从 KVC workload 反推 hardware architecture requirements。**

否则最终很容易变成：

**一个功能很丰富的 KV middleware。**

这当然有价值，但它和：

> **“通过 KVCache workload 驱动 CPU/SoC/NIC/DPU/NPU 架构创新”**

完全是两个高度。

而现在外部证据已经足够充分地说明，第二条路是真实存在的：DirectKV 在利用 C2C，Beluga 在改变 memory fabric，LM-CXD 在重构 CXL-SSD，OneSidedMW 在利用 RNIC，NVIDIA CMX 和 AMD AIC 已经直接围绕 KVC 重构 I/O 和 DPU 架构。([USENIX](https://www.usenix.org/conference/osdi26/presentation/luo?utm_source=chatgpt.com))

**所以你现在这个疑问，其实很可能意味着项目应该进入下一层了：从“验证 KVC 优化”转向“寻找哪些 KVC 行为值得被硬件原生支持”。**

我认为下一步最值得做的，不是继续泛泛搜更多 KVCache 论文，而是基于你 V10 的 11 项候选能力，做一张 **“KVC 软件机制 → 微架构瓶颈 → SoC primitive → 可验证 counter → 潜在芯片需求”五层映射表**。  
那张表一旦做出来，我们基本就能看清：**哪些方向只是软件工程，哪些是真的可能形成芯片特性的。**

---

# you asked

message time: 2026-10-09 23:20:57

你说的没错，但我从今年顶会上发表的这几篇文章来看，我发现一个趋势，就是KVC的主动管理权似乎有从GPU上发起的趋势，也就是说KVC的产生、如何存放、如何拉取、在什么时候拉取，都是从上层调度侧在做决策，更多是从GPU的角度，你如何看这个观点？如果这个观点成立，后续HOST侧，特别是CPU，在KVC的整个生命周期中又能发挥什么作用？

---

# chatgpt response

我认为这个观察**大体成立，但需要把“GPU 发起”与“GPU 中心化”区分开**。今年 OSDI/NSDI 的几篇工作放在一起看，确实出现了明显的 **GPU-centric KVCache lifecycle**：KVCache 什么时候有价值、什么时候需要、需要哪一层、是否值得拉取，本质上越来越围绕 GPU 即将执行的 attention/prefill/decode 来定义；数据路径也越来越允许 GPU 直接触达 host/CXL/storage 资源。

但我不认为趋势是“KVCache 管理权从 CPU 转移给 GPU”。更准确的描述是：

> **KVCache 正从“Host 管数据、GPU 被动消费”的模式，演变成“GPU/推理执行语义提出需求，Host/global scheduler 做全局决策，DMA/NIC/DPU/存储设备执行数据运动”的分层自治架构。**

这个区别非常重要，因为它恰恰决定了未来 HOST CPU 的价值在哪里。

---

## 1. 今年的论文确实支持你观察到的趋势

几个例子放在一起非常明显。

**DirectKV** 已经不是 CPU 主动 `memcpy` KV 到 GPU，而是 GPU kernel 直接访问 CPU-resident KV，通过 NVLink-C2C，把 CPU memory 当作 GPU memory hierarchy 的扩展，并在 kernel 内 pipeline KV fetch / compute / write-back。([USENIX](https://www.usenix.org/conference/osdi26/presentation/luo))

**ECHO** 更进一步：它把动态 KV eviction/recall 管理放进 GPU graph，并通过 fused GPU kernel 把 recall 和 index computation 重叠。这个工作是目前“KV management execution 向 GPU 下沉”最明显的例子之一。([USENIX](https://www.usenix.org/conference/osdi26/presentation/liu-guangda))

**Strata** 的描述也很典型：一边是 **GPU-assisted I/O**，另一边仍然保留 **cache-aware scheduler**。也就是说，它并没有让 GPU 完全拥有缓存管理权，而是形成了“scheduler 决策 + GPU 加速执行”的组合。([USENIX](https://www.usenix.org/conference/osdi26/presentation/xie-zhiqiang))

**SYMPHONY** 则从另一侧说明问题：prefetch hint 来自用户交互和 workload structure，而真正的 placement 又需要 serving framework 与 GPU memory manager 协同。([USENIX](https://www.usenix.org/conference/nsdi26/presentation/agarwal))

甚至最近的 LM-CXD 都非常明确地把这个边界讲出来了：

> serving engine 知道“哪个 KV chunk 将被消费”，device 知道“数据现在在哪里、什么时候准备好”。

因此它做的是**语义和硬件状态之间的协同**，而不是由某一侧包办全部决策。([arXiv](https://arxiv.org/abs/2609.26828?utm_source=chatgpt.com))

所以我认为你看到的是一个真实趋势：

$$
\boxed{\text{KV lifecycle is becoming accelerator-demand-driven}}
$$

但还不是：

$$
\boxed{\text{KV lifecycle is becoming GPU-owned}}
$$

---

# 2. 一个容易混淆的地方：当前很多“GPU 决策”，其实还是 CPU 上的 scheduler 在做

以现在的 vLLM 为例，这一点非常清楚。

vLLM 当前 KVConnector 本身就拆成两部分：

- **Scheduler side**：查询 remote KV、决定 matched tokens、分配 block、管理状态、处理 request finished；
- **Worker side**：实际 load/save KV、按 layer 等待、执行数据传输。

其接口注释甚至明确写出了 scheduler-side 与 worker-side 的职责划分。([GitHub](https://github.com/vllm-project/vllm/blob/main/vllm/distributed/kv_transfer/kv_connector/v1/base.py?utm_source=chatgpt.com))

`KVCacheManager` 本身也是 scheduler 与 KV management system 之间的接口。([GitHub](https://github.com/vllm-project/vllm/blob/main/docs/design/hybrid_kv_cache_manager.md?utm_source=chatgpt.com))

SGLang 目前也还是类似架构：`UnifiedRadixCache` scheduler-facing controller 持有 scheduling policy，负责什么时候 evict / backup / load / prefetch，以及 host pool、async ACK 等资源。([GitHub](https://github.com/sgl-project/sglang/issues/30145?utm_source=chatgpt.com))

更有意思的是 SGLang 今年的 Programmatic KV Cache RFC，它实际上已经提出一个很清楚的三层模型：

> Router 表达 intent → SGLang scheduler/HiCache 负责 allocation、transfer completion 和 visibility → storage backend 管存储对象生命周期。([GitHub](https://github.com/sgl-project/sglang/issues/27574?utm_source=chatgpt.com))

这其实很接近我认为未来成熟架构应该长成的样子。

所以“GPU 角度”是对的：

**需求由 GPU execution timeline 定义。**

但执行这个需求的 global control plane，今天仍然主要运行在 CPU。

---

# 3. 这反而意味着：CPU 的角色不是消失，而是在发生一次非常大的角色迁移

我会用一句话概括：

> **CPU 正从 KVCache 的 Data Mover，变成 KVCache Fabric 的 Control Processor。**

过去的形态可能是：

```text
CPU scheduler
    ↓
CPU 查表
    ↓
CPU 分配 buffer
    ↓
NIC → DDR
    ↓
CPU memcpy / DMA orchestration
    ↓
DDR → GPU
```

CPU 既做控制又深度介入数据路径。

未来更可能变成：

```text
                    ┌──── GPU execution state
                    │   layer/token/deadline
                    ▼
Application → Global KV Scheduler
                  │
                  │ Intent / policy
                  ▼
          Host Control Plane
      placement / mapping / QoS
         / protection / lease
                  │
                  ▼
        NIC / DPU / DMA / CXL
                  │
          ┌───────┴───────┐
          ▼               ▼
       GPU HBM       CXL/DDR/SSD
```

**KV payload 尽量不经过 CPU core。**

但是 CPU 控制的：

- namespace
- address space
- topology
- ownership
- placement
- admission
- QoS
- security
- consistency
- failure recovery

反而更多了。

你 V10 里面“CPU 零数据拷贝，CPU 只下发控制指令”其实已经不经意间走到了这个方向。

---

# 4. 我认为未来 KV 生命周期可以明确拆成三种“权力”

这个拆分非常关键。

### 第一种：**Demand authority——谁知道什么时候需要 KV？**

这里 GPU/推理 runtime 的地位越来越高。

例如：

- 下一步执行哪个 layer；
- 哪个 request 即将进入 prefill；
- 当前 decode batch 是什么；
- sparse attention 下一步需要哪些 block；
- 哪一段 KV 即将被访问。

这些信息离 GPU execution 最近。

所以：

> **什么时候需要某块 KV，GPU/runtime 最清楚。**

ECHO、DirectKV、layerwise prefetch 都是这个方向。

---

### 第二种：**Policy authority——应该把 KV 放在哪里？**

这一层我认为 CPU/global scheduler 很难被 GPU 替代。

因为 GPU 通常只看到局部：

```text
我现在要 block X
```

而 host control plane 可以看到：

```text
GPU0 HBM 还有多少？
GPU1 是否也会使用这个 prefix？
CPU DDR 水位是多少？
CXL pool 是否拥塞？
另一节点是否已有副本？
SSD 是否已有持久副本？
哪个 tenant 优先级更高？
后台 demotion 是否已经积压？
当前网络拥塞程度？
是否应该 recompute？
```

这是一个**全系统优化问题**。

数学上其实已经不是：

$$
\min T_{GPU}
$$

而是：

$$
\min
\left(
T_{TTFT},
T_{TPOT},
Cost,
BW,
Energy
\right)
$$

subject to：

$$
HBM,\ DDR,\ CXL,\ NIC,\ SSD,\ SLA,\ Tenant
$$

这种 global resource optimization 天然需要 host/global control plane。

你的动态选路和前后台资源控制其实已经属于这个方向。

---

### 第三种：**Mechanism authority——数据到底怎么搬？**

这一层则越来越不会由 CPU core 来干。

会逐渐下沉给：

- GPU DMA
- NIC/RNIC
- DPU
- CXL device
- SSD controller
- SoC DMA engine
- memory controller

也就是说：

> CPU 决定“搬什么、为什么搬、优先级多少”；  
> hardware engine 决定“怎样高效搬过去”。

这是我认为最终最合理的分工。

---

# 5. 因此，对未来 HOST CPU 而言，有六类事情反而越来越重要

## ① Global KV namespace / metadata

如果 KV 从 HBM 扩展到：

```text
HBM
DDR
CXL memory
SSD
remote node
```

那问题已经不再是 GPU allocator。

而是：

```text
KV ID
 → location[]
 → state
 → version
 → owner
 → lease
 → refcount
 → residency
 → readiness
```

谁维护这个 global state？

很难让每块 GPU 自己维护。

否则很快就会出现分布式一致性问题。

所以这里 CPU 很可能类似：

> **KVCache 的 MMU/OS kernel。**

这也是你之前设计 KV 状态机、lease、multi-consumer、view 等能力真正有价值的地方。

---

# 6. ② Placement / lifecycle

GPU 最适合判断：

> “我马上要用。”

CPU/global scheduler 更适合判断：

> “这个东西未来一段时间应该放在哪里。”

例如 Agent workload：

```text
Turn N
   ↓
tool call
   ↓
等待 300 ms
   ↓
Turn N+1
```

GPU 在 tool call 阶段可能已经完全没有 execution context。

但 host scheduler 知道：

```text
这个 session 尚未结束；
这个 prefix 300 ms 后大概率再次访问；
不要 evict；
甚至应该 DDR→HBM prefetch。
```

这也是 SYMPHONY 所谓 advisory request 的价值。([USENIX](https://www.usenix.org/conference/nsdi26/presentation/agarwal))

而 SGLang 今年已经开始把 `workflow_id / step_id / cache_ttl / reuse_hint` 这类 Agent semantic metadata 往 scheduler 和 cache node 里传。([GitHub](https://github.com/sgl-project/sglang/issues/24656?utm_source=chatgpt.com))

这个趋势尤其值得关注。

因为：

> **Agentic AI 越发展，CPU 看到的时间尺度就会远远大于 GPU。**

GPU 看 µs–ms。

Scheduler 看 ms–seconds–minutes。

---

# 7. ③ 跨 GPU / 跨节点的资源仲裁

单 GPU 可以自己管理自己的 HBM。

但一旦变成：

```text
8 GPU
× N node
+ shared CXL
+ shared NIC
+ shared SSD
```

很多决策天然不能局部完成。

例如：

```text
GPU0 请求 20 GB/s KV preload
GPU1 正在做 latency-sensitive decode
GPU2 发生 all-reduce
后台正在 SSD→DDR warmup
NIC 已经 90% loaded
```

到底谁让？

这是一个系统 QoS 问题。

NSDI'26 MemChannel 正是在 switched CXL memory pooling 上做 host-side admission control、cross-host bookkeeping、congestion signal 和 rate control。([USENIX](https://www.usenix.org/conference/nsdi26/presentation/guo-zerui?utm_source=chatgpt.com))

所以未来 CPU/SoC 的一个非常大的角色实际上是：

> **traffic governor。**

这里就可以真正下沉到：

- MPAM/resctrl
- LLC partition
- memory bandwidth QoS
- NoC QoS
- PCIe/CXL traffic class
- NIC queue priority
- DMA credit
- memory controller priority

这已经非常接近真正的芯片架构课题了。

---

# 8. ④ Address-space 与 protection

如果 GPU 可以直接访问：

```text
CPU memory
CXL memory
remote memory
SSD buffer
```

那么问题马上变成：

> GPU 到底可以访问什么？

这里需要：

- IOMMU/SMMU
- PASID
- ATS
- address translation
- memory registration
- pinning
- page ownership
- isolation
- revocation

否则 GPU direct access 根本无法在多租户系统安全使用。

所以一个非常有意思的现象是：

> **GPU 越 autonomous，Host MMU/IOMMU/control plane 反而越重要。**

有点像用户态 RDMA。

RDMA 把 CPU 从 packet data path 移走了，但并没有让 OS 消失。

OS 负责：

```text
register MR
map memory
protection
queue setup
lifetime
failure recovery
```

KVCache 很可能走同样一条路。

---

# 9. ⑤ Reliability / consistency / recovery

假设 GPU 发起 remote KV read：

```text
GPU
 ↓
remote KV block
```

读到一半的时候：

- lease expired；
- block 正在 migration；
- remote node fail；
- version 已更新；
- TP=8 中有一张卡失败；
- partial transfer。

谁负责恢复？

不能把复杂分布式 recovery 全部塞进 GPU kernel。

因此典型分工很可能是：

```text
GPU fast path
        ↓ failure
Host slow path
        ↓
re-resolve / retry / recompute
```

这其实和 CPU page fault 非常相似：

GPU 走 fast path；

异常进入 host control plane。

所以你们之前设计的：

- lease
- state machine
- TP consensus
- fallback
- publish/commit

并不是边缘功能。

它们很可能恰恰属于 CPU 长期留下来的核心责任。

---

# 10. 最后一个，也是我认为 CPU 最容易被低估的：Observability

GPU 知道：

```text
我现在 stall 了。
```

但不一定知道：

```text
是 DDR congestion？
CXL switch congestion？
PCIe queue？
remote NIC？
IOTLB？
另一块 GPU？
后台 eviction？
NUMA？
```

这需要跨 SoC 的 telemetry。

OSDI'26 NEMO 的核心思想就是把 memory observability 下沉到 CXL memory device 附近，以较低 CPU overhead 提供更高保真度信号，再让系统软件做决策。([USENIX](https://www.usenix.org/conference/osdi26/presentation/li-shihang?utm_source=chatgpt.com))

这实际上暗示了一个很重要的未来架构：

```text
Hardware telemetry
       ↓
HOST control CPU
       ↓
global decision
       ↓
GPU / DMA / DPU
```

而不是：

```text
CPU自己搬所有数据
```

---

# 11. 所以我认为未来最合理的 KVC 架构不是 CPU-centric，也不是 GPU-centric

而是一个三层模型：

```text
┌─────────────────────────────────────┐
│        Semantic / Policy Plane      │
│     Router / Scheduler / HOST CPU   │
│                                     │
│  reuse probability                  │
│  placement                          │
│  lifecycle                          │
│  admission / QoS                    │
│  global consistency                 │
└────────────────┬────────────────────┘
                 │ Intent
                 ▼
┌─────────────────────────────────────┐
│         Execution Demand Plane      │
│              GPU/NPU                │
│                                     │
│  token/layer execution state        │
│  actual KV demand                   │
│  local HBM state                    │
│  compute/data overlap               │
└────────────────┬────────────────────┘
                 │ descriptors
                 ▼
┌─────────────────────────────────────┐
│          Data Movement Plane        │
│ DMA / NIC / DPU / CXL / SSD ctrl    │
│                                     │
│ zero-copy / prefetch / P2P          │
│ SG DMA / queue / priority           │
└─────────────────────────────────────┘
```

我认为今年论文正在逐渐形成的其实就是这个架构，只是大家还没有统一这么命名。

---

# 12. 这也会改变我们之前讨论“HOST 优化”的切入方式

之前我们容易问：

> **CPU 怎么把 KV 搬得更快？**

我现在认为这个问题应该逐渐废弃。

应该改成三个问题：

### CPU 不应该做什么？

首先找：

```text
CPU memcpy
CPU bounce buffer
CPU polling
CPU per-block descriptor build
CPU packet processing
```

凡是这些能移出 CPU critical path 的尽量移出去。

---

### CPU 必须掌握什么？

重点应该变成：

```text
global metadata
lifecycle
placement
QoS
admission
protection
failure recovery
telemetry aggregation
```

这些不能轻易完全交给 GPU。

---

### CPU/SoC 应该向 GPU 提供什么 primitive？

这才是你真正关心的芯片课题：

```text
GPU-visible host memory
CXL coherent access
ATS/PASID
fast page mapping
IO cache stash
LLC/SLC QoS
memory BW QoS
DMA queues
SG DMA
doorbell
hardware telemetry
fine-grained completion
atomic state transition
```

这时你们的研究自然就从软件走到了 SoC。

---

## 我甚至会进一步下一个判断

如果未来 GPU/NPU 越来越能够自己：

- 判断 layer demand；
- 发起 DMA；
- 直接读取 CPU/CXL memory；
- prefetch；
- 在 kernel 内 overlap；

那么 **CPU 核本身在 KV payload fast path 中的重要性确实会下降。**

但是：

> **HOST SoC 在整个 KV 系统中的重要性可能会上升。**

因为 CPU core 从 data mover 退出以后，原来隐藏在软件里的问题会全部暴露给：

- memory controller；
- LLC/SLC；
- home agent；
- CXL controller；
- IOMMU；
- PCIe root complex；
- DMA engine；
- NoC QoS。

这其实与你最开始提出的“研究最终必须深入 CPU/SoC 架构”并不矛盾。

恰恰相反：

> **真正值得研究的可能不是“怎样让 CPU core 更多参与 KVCache”，而是“当 GPU 成为 KV 的主动消费者之后，HOST SoC 怎样成为高效的 KV fabric substrate”。**

我认为这是一个比“HOST 侧 KVC 优化”更加准确、也更有潜力形成芯片架构特性的研究命题。

而且回头看你 V10，**动态选路、前后台资源控制、零数据拷贝、多通道、描述符流水**其实都可以沿着这条轴重新解释；现在缺的不是技术方向，而是把它们从“软件优化项”重新映射成 **GPU-driven KV lifecycle 下的 HOST/SoC architecture requirements**。

我会建议我们下一步直接做这件事：把完整 KV 生命周期拆成 **Create → Publish → Lookup → Place → Prefetch → Attach → Consume → Demote → Evict → Recover**，对每一个阶段分别标出 **GPU、CPU、DPU/NIC、Memory/CXL controller 谁拥有 Demand / Policy / Mechanism 三种权力**。这样会非常清楚地看出：**CPU 到底是在退出，还是从执行者升级成控制者；以及哪些位置值得做真正的 SoC 特性。**