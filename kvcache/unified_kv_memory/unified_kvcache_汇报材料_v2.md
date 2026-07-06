# 统一异构 KVCache 存储池技术汇报材料

本汇报材料专为技术主管及业务决策层审阅设计。报告将 [srs_v2.3.xlsx](file:///d:/codes/reports/kvcache/unified_kv_memory/srs_v2.3.xlsx) 中 140+ 项碎片化的 SRS 需求条目，提炼并归纳为面向系统底座的 **6 大技术子系统**。整个材料紧扣一个**轴心核心冲突**，全面回答了技术主管关心的“价值、痛点、亮点与指标、护城河”四大宏观问题，并提供了系统级的控制与数据流协同设计和完整的需求追溯矩阵（Traceability Matrix）。

---


## 零、 异构统一 KVCache 存储池最终设计目标与核心技术手段

本软件的终局架构（End-State Architecture）远非一个简单的软件级缓存字典，而是一个**深入底层硬件、横跨整个推理集群拓扑的“分布式异构虚拟内存操作系统”**。其旨在彻底打破单机物理显存（HBM Wall）的硬性限制，通过软硬协同的极端时延榨取，将分散在全集群的显存、DDR、SSD 甚至远端节点的存储介质统一纳管，构建无感的“算力与存储解耦”底座。

为实现这一宏大目标，系统从 140+ 项严苛的底层工程需求（SRS）出发，提炼并攻克了以下六大核心设计目标。每一个目标背后，都由极其厚重且具备极高技术壁垒的子系统（详见后续六大子系统）与硬核技术手段作为支撑：

### 1. 算力成本红利化（消除最高 40% 冗余计算，大幅压降 TCO）
* **系统挑战与避免“伪收益”**：盲目共享 KVCache 往往会引发网络风暴，若网络拉取时延（数十毫秒）超过本地重算耗时，不仅无法降低成本，反而会导致昂贵的 NPU 算力挂起空转（Stall）。
* **硬核技术手段（联动 TM1、TM3、TM4 子系统）**：
  * **[1.2 软硬件协同路由决策] 与 [1.3 级联水位强反压准入]**：依托 **L2-CONN-CostAwareReturn-039** 等需求，构建了动态代价感知（Cost-Aware）准入机制。网关实时测算链路 P99 带宽延迟，一旦评估搬运时延超过 `recompute_saved_time`，立即触发“算力代偿拦截”执行就地重算，绝不允许低质命中拖垮整个集群。
  * **[3.1 精细化容量预估]**：构建全局视角的分层注册池（HBM/DDR/SSD），实现集群规模的冗余算力与存储的高效置换。

### 2. 长文本推理使能（突破物理显存硬限制，使能百万 Token 无感并发）
* **系统挑战**：长 Context 推理会瞬间击穿 NPU HBM 上限，传统框架被迫极度压低 Batch Size，或引发进程 OOM 崩溃。
* **硬核技术手段（联动 TM3 存储分层子系统）**：
  * **[3.2 冷热状态机流转] 与 [3.4 智能主动搬移决策]**：基于 **L3-MC-IntelligentMigration-057** 落地了极其严密的冷热状态机流转协议（ALLOCATED→ACTIVE→EVICTING→RELEASED）。在 HBM 触及 High-85% 水位时，智能引擎自动计算未来复用概率与驱逐代价（Cost-based Eviction），将 Cold KV 异步下沉至本地 Host DDR 或 PCIe SSD，彻底解除物理显存对 Context 长度的枷锁，释放极其宝贵的显存用于计算。

### 3. NPU 极致饱和输出与空间碎片平抑（算力与存储绝对解耦）
* **系统挑战**：高并发长文本下，显存碎片率迅速飙升。传统的碎片整理（Defragmentation）需要暂停算子（Stop-the-world），导致吞吐暴跌。
* **硬核技术手段（联动 TM3、TM5 隔离子系统）**：
  * **[3.3 异步内存紧凑整理] 与 [5.2 租约级无感原子迁移]**：首创基于 **L3-CO-MigrationRCULock-090 (RCU无锁机制)** 的后台物理内存紧凑（Compaction）。搬移数据的同时，前台 Attention 计算算子无锁直读旧地址，在后台校验 CRC 无误的瞬间，通过原子指令一键更替元数据物理指针，实现 **100% 隐藏空间整理时延**。
  * **[1.4 框架协议标准化]**：推行 Pull-to-Pointer 契约，彻底将存储集群的复杂拓扑对推理框架（vLLM/SGLang）黑盒化。

### 4. 超长前缀微秒级匹配（TTFT 首字延迟极简压缩，跨网零损耗）
* **系统挑战**：分布式超大规模节点间的状态查询会带来高昂的 TCP RTT 风暴，导致 TTFT（首字延迟）急剧劣化。
* **硬核技术手段（联动 TM2 元数据检索网络）**：
  * **[2.1 微秒级快判] 与 [2.3 一致性哈希路由]**：在 L2 网关注入本地 BloomFilter 第一级快判（**L2-CONN-PFX-IDX-005**），确保绝大多数 Miss 判定在 5μs 内拦截。
  * 对于可能命中的前缀，直接通过 **L3-MC-PFX-IDX-003 单边 RDMA** 读取常驻于非交换内存（Non-Swappable DDR）中的 C++ SIMD 硬件加速版 RadixTree，将百万节点的分布式一致性哈希前缀碰撞开销压低到几十微秒内，彻底消除首字延迟的长尾波动。

### 5. 数据极速物理流转与多播组网（消除 CPU 控制面开销）
* **系统挑战**：百 GB 级别的 KVCache 穿透 PCIe 总线时，如果依赖 Host CPU 的套接字拷贝，将不可避免造成严重的内核态上下文切换。
* **硬核技术手段（联动 TM4 硬件数据流编排子系统）**：
  * **[4.2 散射收束描述符转换] 与 [4.3 底层零拷贝与 DPU 写回]**：框架引擎产出的碎片化 KV Block 经过 **L2-OL-BulkDescriptor-025** 聚合为 Scatter-Gather 连续描述符。
  * 在传输底层，直接调用 **L4-RDMA-P2P-NPU-001 (NPUDirect RDMA)**，打通 GPU 到网卡的内存映射，实现 100% 绕过 Host CPU 的 Zero-Copy 传输。
  * 在多节点复用相同 System Prompt 场景下，激发底层 **L3-TRANS-MUL-ENGINE-003 (1→N 硬件多播)**，确保源端带宽开销恒定为 O(1)。

### 6. 严苛环境下的工业级稳态、多租户隔离与容错自愈
* **系统挑战**：在万卡规模下，硬件故障是常态，且背景数据搬运往往会挤占 PCIe 物理带宽，导致前台在跑 Batch 突然卡顿，引发严重的多租户干扰。
* **硬核技术手段（联动 TM5、TM6 可靠性子系统）**：
  * **[5.1 多租户安全隔离] 与 [5.3 搬移流量的硬件级 QoS 隔离]**：基于 **L4-QO-MigrationQoS-064**，系统在支持 SR-IOV 的网卡固件级划分特定 QoS QP 队列（限制硬件发包速率）。即使后台发生海量冷热数据下沉，也绝对无法抢占前台推理算子的物理带宽。
  * 依托引用计数（Refcount）锁定活动块；通过 **L1-PD-RankConsensus-013** 强制执行 TP 并发卡间可用前缀长度的 Rank 级共识对齐。
  * 辅以 **[6.1-6.3 遥测与容错自愈]**，将底层硬件级 RAS 错误映射为标准架构异常并隔离受损副本，确保推理全链路的白盒化自愈。

---
## 一、 领导致理会：四大宏观核心问答 (Macro Executive Q&A)

### Q1：这个软件的价值是什么？
* **算力成本红利化（TCO 降低）**：通过池化技术，把全集群的 HBM（显存）、物理内存（DDR）、SSD 以及远端节点空闲显存整合为按需调度的“虚拟资源池”。这能消除高达 **40% 的冗余 Prefill（重算）** 算力浪费，使单卡推理并发吞吐量（Throughput）提升 **1.5x 至 3.0x**，大幅降低大模型在线服务的总拥有成本（TCO）。
* **长文本推理使能（Extend Context Window）**：彻底解除物理显存（HBM）对推理上下文长度的硬性限制。通过异构多级流动，单推理节点可在不修改推理框架的前提下支持 **百万 Token 级别** 的超长上下文推理，而不会因显存耗尽引发 OOM 崩溃。
* **集群资源统一虚拟化（Resource Pooling）**：将传统单机内、显存内的碎片化缓存，升格为跨节点、跨介质的分布式存储服务，使全集群闲置内存/SSD 能够跨界流动，提升整网物理资源利用率至 **90% 以上**。

### Q2：这些软件解决了当下推理场景中的什么业务痛点？
* **痛点 1：长前缀请求下的“重算算力黑洞”**。在 RAG（检索增强生成）、长文本 Agent 模板、多轮长对话场景中，System Prompt 和参考文档长达数万 Token。每次请求都重新计算 KVCache 导致首字延迟（TTFT）极高，且吞噬了推理集群大部分珍贵的计算算力。
* **痛点 2：物理显存墙（HBM Wall）限制并发瓶颈**。KVCache 尺寸与 Context Length 呈线性正相关。随着对话轮数增加，KVCache 迅速占满 NPU 显存，迫使 Batch Size 只能维持在极低水位，导致昂贵的 GPU/NPU 算力在等待输入中闲置。
* **痛点 3：集群级资源闲置与碎片化**。由于负载不均，部分 GPU 节点因长文本显存耗尽而频繁 OOM 拒绝请求，而相邻节点的 CPU DDR、本地 NVMe SSD 及闲置显存却完全空闲。缺乏一种跨介质、零拷贝的分布式拉取与共享手段来实现“算力与存储解耦”。

### Q3：这个软件在技术上有哪些亮点？这些亮点如何反映到最终的业务指标上？

本系统的核心技术亮点，可系统性映射并反映到以下三个维度的业务与财务指标上：

#### 1. TTFT、TPOT、KVCache 命中率等关键业务指标的系统性提升
* **首字延迟（TTFT）大幅压缩**：基于非交换内存的 SIMD（AVX-512/Neon）硬件加速 Radix Tree 检索 `L3-MS-TTFTIndexLayout-088` 与两级前缀快判 `L2-CONN-PFX-IDX-005`，将前缀检索开销压到微秒级。前缀命中时，TTFT 绝对时延降低 **2x - 5x**。
* **每 Token 延迟（TPOT）无感化**：通过 NPUDirect RDMA（NPU HBM 直传 `L4-RDMA-P2P-NPU-001`）与 C2C 内存语义直接指针直读 `L4-UB-C2C-UNIFY-002`，使得跨节点/介质 KVCache 传输时延缩短 50% 以上，TPOT 的长尾波动（P99 Jitter）控制在 **5% 以内**。
* **KVCache 命中率“高质化”**：区别于传统只统计命中率的做法，引入“可消费性准入判定”与“算力代偿感知自适应返回协议” `L2-CONN-CostAwareReturn-039`。剔除低带宽链路下的“无效低质命中”，确保返回的命中都是能转化为 TTFT 收益的 **“高质 Usable Hit”**。

#### 2. NPU 有效利用率（Duty Cycle）的结构性提升
* **消除 GPU/NPU 空转等待（Stall）**：传统推理框架中，NPU 经常需要同步等待 Host CPU 进行显存碎片整理、网络套接字传输或权重调度，导致 NPU 有效计算率偏低。
* **算力与存储控制流异步解耦**：系统通过异步内存紧凑引擎 `L3-MC-CompactionEngine-056`、无锁 RCU 页迁移机制 `L3-CO-MigrationRCULock-090` 以及 DPU 传输控制卸载 `L4-NET-OFFLOAD-DPU-001`，将空间整理与数据搬运完全在背景线程中执行。**NPU 算力资源从“计算+等待”结构性地转变为“100% 饱和 Attention 计算”**，算力利用效率大幅拉升。

#### 3. 每百万 Token 成本模型（Cost per Million Tokens）的颠覆性重构
* **计算密集型向存储/传输密集型的财务转换**：传统推理成本模型下，长 context 首轮请求的成本为 $Cost_{compute} \times Token_{prefill\_len}$。Prefill 阶段算力消耗随长度指数级增加。
* **重构成本模型**：引入存储池后，百万 Token 成本模型被重构为：
  $$\text{Cost} = \text{Cost}_{\text{compute}} \times (1 - \text{Hit Rate}) \times \text{Length} + \text{Cost}_{\text{transfer}} \times \text{Hit Rate} \times \text{Length} + \text{Cost}_{\text{decode}}$$
  由于底层高速零拷贝网络传输 1M Token 的功耗与时延成本（$\text{Cost}_{\text{transfer}}$）比 NPU 重新计算（$\text{Cost}_{\text{compute}}$）便宜 **1 到 2 个数量级**，在大规模长文本/多轮对话应用中，**每百万 Token 的综合运行成本结构性下降 40% - 70%**。

---

### Q4：这个软件最终的竞争力和护城河是什么？

系统通过 **“技术复杂度、硬件协同、数据飞轮、生态标准化”** 四维一体，构筑起极高的竞争壁垒与护城河：

```
       ┌─────────────────────────────────────────────────────────┐
       │                 4D 核心竞争力护城河                     │
       └────────────────────────────┬────────────────────────────┘
                                    │
         ┌──────────────────────────┼──────────────────────────┐
         ▼                          ▼                          ▼
┌──────────────────┐       ┌──────────────────┐       ┌──────────────────┐
│  一、技术复杂度  │       │  二、硬件协同底座  │       │   三、数据飞轮   │
│  - 极速元数据网络│       │  - NPUDirect RDMA│       │ - 并发高 ➔ 共享多│
│  - RCU并发无锁   │       │  - UBLINK GAP    │       │ - 成本降 ➔ 用户增│
│  - 分片目录/状态机│      │  - DPU/网卡QoS   │       │ - 飞轮良性循环   │
└────────┬─────────┘       └────────┬─────────┘       └────────┬─────────┘
         │                          │                          │
         └──────────────────────────┼──────────────────────────┘
                                    │
                                    ▼
                          ┌──────────────────┐
                          │  四、生态标准化  │
                          │  - 统一Connector  │
                          │  - 屏蔽异构介质  │
                          │  - 产业上下游粘性│
                          └──────────────────┘
```

* **维度一：技术复杂度（Technical Complexity）**
  * 构建了高并发、高弹性的分布式元数据检索网络。支持一致性哈希全局目录分片与原子可见发布机制 `L3-MS-AtomicPublishVisibility-087`。在数据层，实现了基于 RCU 理念的无锁高并发迁移与内存紧凑机制，解决了多副本全局一致性对齐、防悬空地址引用的复杂状态机治理问题。这种极高的技术复杂度使得竞品难以在短期内实现稳定且零抖动的工业级交付。
* **维度二：软硬协同的硬件协同壁垒（Hardware Co-design）**
  * 系统紧密绑定了特定高性能硬件特性。打通了 NPU 机内 UBLINK 的内存语义直读通道（GAP 寻址保护 `L4-UB-DirectViewGuard-072`）、跨节点 GPUDirect RDMA 零拷贝直传、网卡固件级硬件 QoS 流量隔离限制 `L4-QO-MigrationQoS-064` 以及 DPU 在线硬件解压缩。**这种全栈垂直硬件协同，使得纯软件层的缓存框架在性能极限上无法与本系统竞争。**
* **维度三：基于大模型共享前缀的“数据飞轮”（Data Flywheel）**
  * 推理服务并发用户数（并发量）越多，公共 Prompt、常用 Agent 模板、热点 RAG 知识库以及 System Prompt 的**共享重合度越高**。
  * 重合度越高 $\rightarrow$ 存储池的 **KVCache 命中率越高** $\rightarrow$ 每百万 Token 的 **计算成本越低、首字延迟越小** $\rightarrow$ 服务竞争力增强吸引 **更多用户接入** $\rightarrow$ 产生更多可复用的前缀。**数据飞轮的良性闭环，使得先发运行的集群具有无可比拟的成本与体验优势。**
* **维度四：承上启下的生态标准化（Ecosystem Standardization）**
  * **统一南向与北向适配标准**。北向定义了标准的 `KVConnector Protocol` 接口，无缝融合并统一了 vLLM 和 SGLang 等主流推理框架，避免推理引擎的碎片化；南向通过 `HardwareCapabilityAPI` 提供硬件接口标准，允许华为鲲鹏、NVIDIA 等异构算力与存储介质无缝接入。**通过定义产业标准，确立了强大的生态粘性与话语权。**

---

## 二、 核心轴心目标：解决“异构介质延迟”与“推理时延 SLA”的根本性冲突

本软件系统的全部设计，都围绕着解决一个**核心技术冲突（轴心冲突）**：

> [!IMPORTANT]
> **异构存储介质的物理延迟**（显存 HBM 约数 ns $\rightarrow$ 本地内存 DDR 约数十 ns $\rightarrow$ 远端 RDMA 网络约数 μs $\rightarrow$ 本地 SSD 约数十 μs 至 ms）呈现出**数个数量级的阶梯式暴涨**。
>
> 与之相对，在线 LLM 推理的 **时延 SLA 极其严苛**（TTFT 通常要求在 100-200ms 以内，TPOT 要求在 20-30ms 以内）。如果元数据检索在网络中引发 RTT 风暴，或者 KVCache 数据物理搬运时 Host CPU 软件栈开销过重导致 NPU 处于等待（Stall）状态，那么存储池所带来的“容量收益”将被其引入的“时延开销”彻底抵消，甚至慢于重新计算，导致系统整体性能受损。

因此，**统一异构 KVCache 存储池的轴心牵引使命**是：
通过**推理网关准入、极速元数据快判、零拷贝硬件通道、并发无锁迁移以及前后台流量的硬件 QoS 物理隔离，将所有元数据检索、数据物理搬运、空间降级整理的开销完全隐藏在推理计算与调度流水线的后台，实现“零额外开销、无感吞吐”的异构 KVCache 虚拟化流动，确保 GPU/NPU 算力始终处于 100% 饱和输出状态**。

---

## 三、 六大技术子系统划分及其必要性论证

为了攻克上述系统级的架构瓶颈，我们明确划分并定义了以下 6 大核心技术子系统，其具体技术实现与必要性论证如下：

### 一、 推理网关与标准适配子系统 (Inference Gateway & Adapter Subsystem)
* **架构定位与必要性**：解决**算力计算面与物理存储池之间的拓扑耦合、接口碎片化，以及在网络拥塞时发生“盲目拉取导致 GPU 算力饥饿”的系统级架构瓶颈**。如果推理引擎直接操作物理内存或网卡，将导致框架与特定加速卡深度绑定；同时，若不加控制地在网络拥塞时搬运数据，拉取时延会远超重算时间，导致推理流水线挂起。
* **具体技术实现（SRS 对齐）**：
  * **统一协议与接口标准化**：通过标准 gRPC IDL 与 Python Protocol 双重定义，封装统一的 `put/get/prefetch/evict/get_status` 操作接口，将数据结构标准化为包含模型特征的 `KVMeta` 与指示存储属性的 `KVPage` 描述符 `L2-CONN-API-STD-001`。
  * **算力与存储内存解耦契约**：推行 **Pull-to-Pointer（拉取至指定指针）契约** `L2-CONN-BufferContract-040`。存储池不校验 GPU 显存水位与算力调度状态，仅负责将 KV 块准确写入推理框架传入的 `device_ptr`。寻址错误时通过标准状态错误码上报 `L2-KV-StateErrorCode-037`。
  * **算力代偿自适应返回控制**：设计 **时延评估反馈逻辑** `L2-CONN-CostAwareReturn-039`。网关实时通过 100ms 周期的探测器获取路径带宽与时延 `L2-CONN-TOPO-ROUTE-002`，当计算出 KVCache 搬运耗时大于推理框架的 `recompute_saved_time` 时，存储池内部判定为逻辑 Miss，直接返回标准 `RECOMPUTE` 信号，强制推理引擎本地重算，保护 TTFT SLA。
  * **水位路由动态自适应规避**：收集底层节点的水位告警（Watermark Hints），对高水位（High/Critical）节点的访问路径增加启发式 Cost 权重评分 `L2-CONN-WatermarkQuery-032`，实现自适应流量规避。

### 二、 极速分布式元数据与检索网络子系统 (Distributed Metadata & Sharded Routing Subsystem)
* **架构定位与必要性**：解决**大规模分布式场景下“元数据分布式查询 RTT 放大”与“匹配检索路径串行化阻塞”的神经枢纽瓶颈**。在数千节点的推理集群中，高频的 KVCache 前缀检索如果必须走分布式强一致性查询（如 TCP-based Redis/etcd），会导致 TTFT 增加数十毫秒。本子系统必须在微秒级完成前缀冲突匹配判定。
* **具体技术实现（SRS 对齐）**：
  * **本地两级快速过滤与 Cache**：在 Connector SDK 进程内维护两级过滤结构：第一级为基于 **极低内存占用（<50MB）的本地 Bloom Filter**（误判率 FPR < 0.1%），实现确定 Miss 在 5μs 内本地快判 `L2-CONN-PFX-IDX-005`；第二级为 **HashMap 结构的进程内元数据缓存**（默认 100K 规格，`L2-CONN-META-CACHE-004`），规避网络 RTT 风暴。
  * **TTFT 匹配路径硬件级加速**：为彻底消除 CPU 上的最长前缀匹配（LPM）判定长尾时延，将 SGLang/vLLM Radix Tree 迁移至 C++/Cython 实现，利用 SIMD（AVX-512/ARM Neon）指令集对 Token 数组和哈希键进行硬件级比对；且核心 Radix Tree 的前两级拓扑节点常驻于 **非交换（Non-Swappable）的 DDR / 专用内存** 中 `L3-MS-TTFTIndexLayout-088`。
  * **GPU 端分层哈希并行化**：利用 GPU 端并行 xxHash-128 CUDA Kernel，引入每 256 tokens 的分层哈希 Checkpoint 进行增量哈希计算 `L1-VLLM-PFX-IDX-005`，将 10-20ms 的 Python 前缀计算压缩至 0.5ms。
  * **分布式目录哈希分片与单边 RDMA 访问**：全局前缀目录通过一致性哈希环进行分片，每个物理节点承载 100 个虚拟节点，设计双副本（N=2）高可用保障 `L3-MC-PFX-REPL-004`；查询链路从 TCP 改造为 **基于 RDMA One-sided READ 的直接内存直读** `L3-MC-PFX-IDX-003`，将分布式前缀查询 P50 时延压低至 100μs 内。
  * **前缀索引原子化可见性发布流水线**：Prefill 生成新 KV 写入物理 Extent 并校验 Checksum 期间，在全局目录中为节点设置掩码（Masked）不可见状态，校验无误且触发 Memory Fence 后再原子化去除掩码，杜绝高并发多节点读写下的空地址引用或误匹配 `L3-MS-AtomicPublishVisibility-087`。

### 三、 异构多级分层存储池与生命周期空间子系统 (Hierarchical Memory Pool Subsystem)
* **架构定位与必要性**：解决**长文本、高并发推理下“物理显存墙空间硬限制”与“高碎片率高并发分配导致算子停顿”的空间矛盾瓶颈**。该子系统在底层对多级异构介质进行统一扁平化池化，提供状态机生命周期控制，是整个存储池的空间物理基石。
* **具体技术实现（SRS 对齐）**：
  * **统一全局存储池注册与 NUMA 亲和分配**：在集群级维护统一的内存物理注册表，支持节点动态插拔；分配时采用 NUMA 亲和的专用分配器，通过 `numa_alloc_onnode` 将 KVCache Block 优先分配到与当前 NPU NUMA 亲和的 CPU 物理内存节点上，减少跨 NUMA 访问 `L3-MC-POOL-ALLOC-005`。
  * **统一地址空间访问空间管理**：在 Kunpeng 平台实现 CPU-DDR + NPU-HBM + SSD 统一虚拟内存空间（GAP 寻址），基于 Unified Memory 访问统计（NPUMemRangeGetAttribute）进行热度调度 `L4-C2C-UNIFY-POOL-001`。
  * **强一致性生命周期状态机**：严格规范 KVCache Block 的流转状态机：`ALLOCATED → LOADING → READY → ACTIVE → OFFLOADING → EVICTABLE → RELEASED/FAILED` `L1-MM-Lifecycle-009`，支持异构冷热下沉和 GC 自动回收。
  * **异步物理内存紧凑（Defragmentation）**：外部物理内存碎片率超过 30% 时，启动 **后台内存紧凑引擎（Compaction Engine）** `L3-MC-CompactionEngine-056`。使用内存屏障（Memory Barrier）技术，在不暂停或锁死推理线程的情况下，异步迁移物理页以合并空闲物理块。
  * **三级水位智能迁移与成本淘汰算法**：在 HBM 设立三级水位监控（Low-70%, High-85%, Critical-95%）。当越过 High 水位时，通过淘汰评分算法（综合Saved Prefill Time、未来复用概率、租户优先级、搬移成本）动态挑选 Warm/Cold KV 异步换出至本地 Host 内存或 SSD `L3-MC-IntelligentMigration-057`；Critical 水位时强制限制换入以防发生 HBM OOM。

### 四、 零拷贝硬件通道与多播数据流编排子系统 (Zero-Copy Transfer & Multicast Subsystem)
* **架构定位与必要性**：解决**海量数据物理移动下的“Host CPU 软件栈开销过大”与“数据通道 PCIe 抢占”的吞吐物理极限瓶颈**。若数据搬运仍需 Host CPU 频繁干预和内核态拷贝，系统吞吐将大幅缩水。该子系统是实现数据高速流动的物理大动脉。
* **具体技术实现（SRS 对齐）**：
  * **聚合不连续物理块（Bulk Descriptor）**：Connector 层将推理引擎生成的多个非连续物理 Block 合并为 **Scatter-Gather 聚合描述符**（Coalescing 机制）`L2-OL-BulkDescriptor-025`，批量提交以规避 PCIe 单次小块 Setup 耗时。
  * **流水线层级（Layer-by-Layer）计算传输重叠**：打通流式 KVCache 恢复 Pipeline，将“全层就绪再计算”改造成“每 K 层就绪即计算” `L1-SGL-SWP-XFER-002`，利用多 CUDA Stream 与 Event 同步，隐藏传输耗时。
  * **批量 DMA 提交与 CUDA Graph 捕获**：使用 CUDA Graph 捕获并重放非连续 Block 物理 memcpy `L1-VLLM-SWP-XFER-003`，用单次批量 DMA 代替循环。
  * **硬件级零拷贝直传（NPUDirect RDMA & DPU）**：底层传输层创建长生命周期预注册内存池 `L4-OL-RegisteredPool-053`；跨节点时采用 **NPUDirect RDMA（GPU-Direct）** 机制，将 NPU 显存物理注册为 `v_reg_mr`，实现端到端零 CPU 拷贝直传 `L4-RDMA-P2P-NPU-001`；通过 **DPU 硬件卡** 卸载 RDMA 完成队列（CQ）轮询和 Mooncake 路由表，由网卡/DPU 在线完成压缩与解压缩。
  * **多副本自适应解析与组播编排**：利用多副本路径解析器（Multi-Replica Path Resolver）在 5μs 内算得拓扑开销最低的最优物理源副本 `L3-SE-MultiReplicaResolver-089`；在共享 Prompt 跨节点分发场景下，启动 **1 → N 硬件组播（UD 组播 + 软 P2P Relay 混合模式）** `L3-TRANS-MUL-ENGINE-003`，使源端发送带宽开销始终为 O(1)。

### 五、 多卡协同共识、租约与 QoS 隔离子系统 (Consensus, Lease & QoS Isolation Subsystem)
* **架构定位与必要性**：解决**多租户/多卡场景下的“跨卡数据不一致导致对齐错乱”、“多租户越权访问”以及“背景数据流挤兑前台推理算子”的安全与隔离瓶颈**。在实际生产集群中，背景的水位迁移、页表紧凑会引发大量的并发数据竞争，如果缺乏硬件级 QoS 限制与无锁控制，前台推理的 TPOT 性能将严重抖动。
* **具体技术实现（SRS 对齐）**：
  * **多卡前缀一致性共识**：在 TP/PP 多卡并行推理组间执行 Rank 共识协议 `L1-PD-RankConsensus-013`，对可用前缀长度（usable length）、Layout 版本达成一致，防止卡间对齐偏差，不一致时自动回滚至 min-safe 安全前缀或重算。
  * **共享安全租约（ViewLease）控制**：近端共享直接访问（Direct-View）必须绑定携带 Epoch、Version、Refcount、Expiry 和 Revocation 撤销回调的 **租约句柄** `L2-MM-ViewLease-028`。当租约过期或撤销时，GPU 硬件层（SMMU/IOMMU 保护域，`L4-SC-ViewProtection-062`）将立即切断物理地址映射。
  * **活动推理块引用计数锁（Active Refcount Guard）**：对于在跑推理 Batch 正在 attach 消费的 Block，系统校验其活跃引用计数 `refcount > 0` `L3-CO-RefCountLifecycle-086`。一旦 refcount 大于 0，即使收到水位迁移或强制驱逐信号，系统也会将该物理块锁死在 `EVICTING` 状态，强行禁止物理覆盖、释放或下沉。
  * **并发无锁 RCU 迁移机制**：当进行多级水位换出或内存碎片紧凑页拷贝时，系统开启 **基于 RCU 理念的并发控制** `L3-CO-MigrationRCULock-090`：拷贝时前台读取流依然路由到旧物理 Extent 地址，计算完全不阻塞；拷贝及 CRC 校验完毕的瞬间，通过原子操作（Atomic Page Remap）一次性原子更替元数据指针，并经历一轮 Batch 推理的安全退出期（Safe Retirement Epoch）后，安全物理回收旧地址空间。
  * **物理层硬件 QoS 限速与 QP 隔离**：要求高性能网卡（RNIC）支持 SR-IOV，为控制信令、前台加载通道、背景迁移流量以及 SSD 预热通道，**物理划分 4 个以上独立的硬件队列对（QPs）**，并满足板载 SRAM 上下文缓存（Context Cache >= 128K QPs）`L4-HW-NICSpecConstraint-075`，规避多租户并发下的固件上下文频繁调出（Thrashing）；在物理层面为水位迁移流量限制硬件带宽 `L4-QO-MigrationQoS-064`，彻底阻断背景流挤兑前台推理算子的 PCIe 物理带宽。

### 六、 全栈遥测与故障容错自愈子系统 (Telemetry & Fault Tolerance Subsystem)
* **架构定位与必要性**：解决**超大规模异构计算集群高频物理故障导致推理整组雪崩起”以及“灰度/降级长尾延迟难追踪（命中了为什么变慢）”的可靠性与白盒化治理瓶颈**。在由数万张卡和上千台宿主机组成的集群中，硬件故障发生频率极高。系统必须将底层异构物理故障收拢翻译，并对全链路决策路径实施诊断 Trace，确保高可用。
* **具体技术实现（SRS 对齐）**：
  * **细粒度语义命中率遥测**：摒弃粗粒度统计，框架层向外持续导出包含 raw hit、usable hit、local usable hit、view hit、bulk-load hit、stream-restore hit、abandoned hit 在内的语义化命中指标，并输出请求关键路径时延拆解 `L1-OB-SemanticMetrics-016`。
  * **全决策决策路径 Trace**：为每次 KVCache 挂载与传输请求生成 **Path Decision Trace** `L2-OB-PathTrace-030`，详细记录所选后端、时延分解、副本所在介质、QoS 等级及 Fallback 原因，确保“命中了但首字延迟高”的异常情况可追溯。
  * **降级链路因果追溯（Fallback Causality Trace）**：对发生的降级行为（如从 UBLINK memory view 降级为 RDMA bulk 传输，或从 RDMA 传输降级为 TCP/重算，`L3-FT-FallbackTrace-046`）进行全路径因果关系捕获。
  * **RAS 硬件错误收拢与自愈转换**：底层网卡、UBLINK、IOMMU 发生物理报错时，由 **RAS 错误映射引擎** `L4-FT-RASErrorMap-061` 实时捕获并统一转换映射为存储池标准状态错误码（如 `REPLICA_DEGRADED`、`VISIBILITY_TIMEOUT` 等），进而触发 Fallback Contract 协议 `L2-FT-FallbackContract-029`，将受损副本隔离（Replica Quarantine）并无感调度至备用路径或触发就地重算，防止系统崩溃挂死。

---

## 四、 子系统间协同与双轨互锁架构 (Interactions & Dual-Track Interlocking)

系统的 6 个子系统，在运行时绝非各自孤立，而是通过**“控制轨”与“数据轨”双轨协同互锁**的方式编排在一起。在梳理双轨交互前，必须首先明确各个子系统的核心功能职责以及系统整体的控制面与数据面分工。

### 子系统核心职责定义与编号映射

本系统六大子系统的核心职责已与需求基线 (SRS) 严格对齐，其编号与具体职责定义如下：

* **TM1: 推理网关与标准适配子系统**
  * **[1.1]** 北向意图解析：提供上层可见的统一意图 API，精准解析推理引擎的 KVCache 挂载、预取与释放需求，隔离底层复杂拓扑。
  * **[1.2]** 软硬件协同路由决策：基于全局视角测算多副本网络开销与代偿阈值，选择最优物理路径或触发就地重算拦截，避免远端拥塞拖累整体吞吐。
  * **[1.3]** 级联水位强反压准入：实时感知底层存储节点的容量告警状态，在资源濒临枯竭时向推理调度层施加反压，防止系统因过载而发生 OOM 崩溃。
  * **[1.4]** 框架协议标准化：推行 Pull-to-Pointer 指针交付契约，无缝对接 vLLM、SGLang 等主流框架，使得算力节点的内存管理与全局存储池的责任彻底解耦。
* **TM2: 元数据与检索网络子系统**
  * **[2.1]** 微秒级前缀安全碰撞判定：利用极低内存占用的本地 Bloom Filter 结合缓存，实现 5μs 级的本地快判拦截，彻底规避必然 Miss 请求引发的网络 RTT 风暴。
  * **[2.2]** 多级目录镜像同步：维护全局目录的高可用多副本，实施基于原子掩码的无缝版本更替与可见性发布，杜绝并发环境下的元数据不一致或悬空指针。
  * **[2.3]** 全局一致性哈希路由：依托一致性哈希算法对海量 KV 前缀进行分布式分片，通过单边 RDMA 内存直读技术实现百微秒级的跨节点精确寻址。
* **TM3: 分层存储池与空间子系统**
  * **[3.1]** 精细化容量预估：汇总异构集群中 HBM、DDR 与 SSD 的碎片化空间，向网关层提供高精度的容量预测与准入水位判定基准。
  * **[3.2]** 冷热状态机流转：定义并强制执行从加载、就绪到驱逐（READY/ACTIVE/EVICTING）的严格一致性状态流转协议，确保系统在复杂调度下的数据生命周期安全。
  * **[3.3]** 异步内存紧凑整理：当外部物理碎片率飙升时，启动背景内存碎片搬移（Compaction）引擎，将离散页合并不连续物理块，消除分配器导致的卡顿。
  * **[3.4]** 基于多级水位的智能主动搬移决策：在 HBM 触碰高水位警戒线时，结合成本淘汰算法（Cost-based Eviction）主动将低频复用的 Cold KV 降级换出至 Host DDR 或 SSD。
* **TM4: 硬件加速与数据流编排子系统**
  * **[4.1]** 多层级流水线重叠：打破传统的串行挂载等待机制，将网络传输与计算流水线分层交织重叠，深度掩盖物理时延。
  * **[4.2]** 投机预取与散射收束描述符转换：将离散的物理内存块映射聚合为连续的 Scatter-Gather 描述符，大幅降低 PCIe 总线 Setup 频次与碎片损耗。
  * **[4.3]** 底层零拷贝与 DPU 硬件写回加速：打通 GPU 到网卡的显存直访通道 (GPUDirect RDMA)，利用硬件多播 (1→N) 与 DPU 在线卸载，实现全流程 0 CPU 开销的数据搬运。
* **TM5: 共享协同与 QoS 隔离子系统**
  * **[5.1]** 多租户安全隔离与多卡共识：颁发携带生命周期的安全租约，并通过严格的引用计数锁定机制阻止非法覆盖；协同 TP 组内多卡的可用前缀长度对齐。
  * **[5.2]** 租约级无感原子迁移：依托 RCU 理念，在冷热数据下沉或碎片整理时允许前台算子无锁并发直读，并在搬移完成后执行原子化的底层元数据指针更替。
  * **[5.3]** 搬移流量的硬件级 QoS 隔离：为高并发的背景迁移数据流在网卡固件级硬分配独立的限速队列（QoS QP），彻底杜绝背景流对前台高优推理网络带宽的物理挤兑。
* **TM6: 遥测观测与容错自愈子系统**
  * **[6.1]** 全链路性能观测追踪（命中了为什么变慢）：提供极其细粒度的时延拆解与语义指标，记录每一次降级、拦截或 Fallback 的根本原因。
  * **[6.2]** 路径降级因果追溯：针对 UBLINK 直读降级到 RDMA，或降级到 TCP/重算的全因果链进行捕获溯源，支撑系统级的白盒化诊断。
  * **[6.3]** 底层 RAS 错误标准映射：实时捕获底层物理级故障，并统一映射为标准的架构级错误状态码，触发自动剔除与调度自愈。

### 全局维度的控制面与数据面事务拆解

从整个软件系统的宏观视角出发，功能拆解遵循“控制与数据完全分离”的原则：

* **控制面（Control Plane）事务**：负责调度、决策、元数据同步与系统保护。具体包括：
  * **准入与路由决策**：解析框架查询意图 **[1.1]**、执行本地 BloomFilter 快判拦截 **[2.1]**、进行哈希路由解析与目录查询 **[2.3]**，并执行算力代偿测算与拦截 **[1.2]**。
  * **空间与生命周期控制**：分配安全租约句柄与引用计数锁定 **[5.1]**、检测多级水位告警并下发容量分配策略 **[3.1, 3.4]**。
  * **一致性与状态防护**：驱动冷热状态机流转 **[3.2]**、执行 RCU 并发无锁页迁移元数据原子更替 **[5.2]**，以及下发 QoS 限速配额设定 **[5.3]**。

* **数据面（Data Plane）事务**：负责 KVCache 物理载荷的高速搬运、聚合与硬件计算。具体包括：
  * **聚合与编排**：收集分散的小块物理内存，生成 Scatter-Gather 聚合描述符 **[4.2]**，编排软硬件协同传输流水线 **[4.1]**。
  * **物理搬运动作**：触发底层的 GPUDirect RDMA 物理传输 **[4.3]**、执行 1→N 的组播网络分发，以及驱动本地物理内存间的紧凑页拷贝 **[3.3]**。
  * **硬件协处理**：交由 DPU 等硬件执行数据在线压缩、解压与检验计算 **[4.3]**。

* **互锁机制（Interlocking）**：控制面通过引用计数、硬件 Fence、RCU 屏障以及水位强反压，防止控制路径的状态更新与数据面的数据物理搬运产生空指针冲突或总线带宽挤兑。

### 4.1 控制面与数据面双轨分离交互流图

```mermaid
flowchart TB
    %% Styling
    classDef control fill:#E1F5FE,stroke:#0288D1,stroke-width:2px,color:#01579B;
    classDef data fill:#E8F5E9,stroke:#388E3C,stroke-width:2px,color:#1B5E20;
    classDef hw fill:#FFEBEE,stroke:#D32F2F,stroke-width:2px,color:#C62828;

    subgraph Control_Track ["双轨之【控制面】（无阻碍极速决策）"]
        Intent["① 提交意图 (L1/L2)<br/>事务: 意图拦截<br/>[职责 1.1]"]
        FastCheck["② 本地 Filter/Cache 快判<br/>事务: 意图拦截<br/>[职责 2.1]"]
        DirLookup["③ 分布式目录单跳检索<br/>事务: 哈希路由解析<br/>[职责 2.3]"]
        PathResolve["④ 多副本最优路径选择<br/>事务: 软硬件协同路由<br/>[职责 1.2]"]
        LeaseLock["⑤ 安全租约分配<br/>事务: 租约控制<br/>[职责 5.1]"]
        
        Intent --> FastCheck
        FastCheck -->|Miss/Hit 判定| DirLookup
        DirLookup --> PathResolve
        PathResolve --> LeaseLock
    end
    class Intent,FastCheck,DirLookup,PathResolve,LeaseLock control;

    subgraph Interlocks ["双轨【互锁栅栏】（一致性与隔离保障）"]
        direction TB
        BudgetCheck["时延预算自适应代偿拦截<br/>L2-CONN-CostAwareReturn-039"]
        AtomicVis["元数据未发布掩码不可见保护<br/>L3-MS-AtomicPublishVisibility-087"]
        RCUMigrate["RCU并发读物理页映射原子替换<br/>L3-CO-MigrationRCULock-090"]
        QoS_QP["硬件限速/QoS队列物理通道隔离<br/>L4-QO-MigrationQoS-064"]
    end
    class BudgetCheck,AtomicVis,RCUMigrate,QoS_QP hw;

    subgraph Data_Track ["双轨之【数据面】（物理零拷贝高带宽搬运）"]
        Descriptor["⑥ 生成 Bulk 描述符<br/>事务: 聚合描述符转换<br/>[职责 4.2]"]
        MulticastXfer["⑦ 1→N 组播分发<br/>事务: 网络分发<br/>[职责 4.1]"]
        NPUDirect["⑧ RDMA零拷贝直传<br/>事务: 物理载荷搬运<br/>[职责 4.3]"]
        DPUQuant["⑨ DPU 控制卸载<br/>事务: 硬件协处理<br/>[职责 4.3]"]
        
        Descriptor --> MulticastXfer
        MulticastXfer --> NPUDirect
        NPUDirect --> DPUQuant
    end
    class Descriptor,MulticastXfer,NPUDirect,DPUQuant data;

    %% Interlocking wires
    DirLookup -.->|命中收益计算| BudgetCheck
    BudgetCheck -.->|判定通过| PathResolve
    LeaseLock -.->|锁定 HBM 物理块| Descriptor
    RCUMigrate -.->|原子更替指针防止悬空| MulticastXfer
    QoS_QP -.->|对传输网卡限速, 保护前台| NPUDirect
    AtomicVis -.->|校验通过后曝光| DirLookup
```

### 4.2 场景 1：TTFT 首字延迟极速快判与加载时序

首字延迟（TTFT）主路径的核心，是利用本地快判和单边 RDMA 检索避开 TCP 长尾延迟，并辅以时延预算拦截。具体 SRS 需求互锁机制如下：

```mermaid
sequenceDiagram
    autonumber
    participant Sch as 推理调度层 (L1)
    participant Conn as 对接适配层 (L2)
    participant Dir as 全局分片目录 (L3)
    participant Fab as 底层 Fabric (L4)
    participant RT as Attention 计算 Runtime

    Sch->>Conn: 1. 提交前缀查询 [职责 1.1: 北向意图解析]
    Note over Conn: [职责 2.1: 微秒级快判] 本地Bloom Filter判定 Miss?
    alt 本地 Bloom Filter 确定 Miss
        Conn-->>Sch: 2. 返回逻辑 MISS 信号
    else 可能命中
        Conn->>Dir: 3. 单边 RDMA 读全局哈希目录 [职责 2.3: 一致性哈希路由]
        Dir-->>Conn: 4. 返回物理 Extent 地址
    end
    Note over Conn: [职责 1.2: 路由决策] 评估带宽, 计算搬移耗时与代偿成本
    alt 搬移耗时 > 重算代偿阈值
        Conn-->>Sch: 5. 触发代偿拦截，直接返回逻辑 MISS [职责 1.3: 强反压]
    else 搬移划算 (高质命中)
        Conn->>Dir: 6. 申请安全租约，锁死引用计数 [职责 5.1: 多租户隔离]
        Dir->>Fab: 7. 生成聚合描述符下发 [职责 4.2: 散射收束描述符转换]
        Note over Fab: [职责 4.3: 零拷贝硬件通道] 触发 GPUDirect RDMA<br/>独占高优先级流队列
        Fab-->>Conn: 8. DPU 卸载完成校验 [职责 4.3] 返回完成事件
        Conn-->>Sch: 9. 交付挂载句柄并标准化反馈 [职责 1.4: 协议标准化]
        Sch->>RT: 10. 执行 TP 卡间前缀共识后 attach [职责 5.1: 多卡共识]
    end
```

### 4.3 场景 2：高水位无锁 RCU 页迁移与整理时序

背景的水位迁移与碎片整理必须绝对无感，不阻塞前台推理 Attention 算子。其具体互锁流转如下：

```mermaid
sequenceDiagram
    autonumber
    participant Sched as 推理引擎 / 算子计算
    participant Pool as 分层存储池 (L3)
    participant QoS as 协同与 QoS 隔离 (L3/L5)
    participant Fab as 底层 Fabric (L4)

    Note over Pool: 监控物理空间，越过水位线 [职责 3.4: 智能主动搬移决策]
    Pool->>QoS: 1. 扫描冷数据，发起引用计数安全检查 [职责 5.1: 安全隔离]
    alt 活跃引用计数 refcount > 0
        QoS-->>Pool: 2. 拒绝释放，锁定状态以保护在跑 Batch
    else 引用计数归 0 且锁定成功
        Pool->>QoS: 3. 启动并发无锁页迁移机制 [职责 5.2: 无感原子迁移]
        QoS->>Fab: 4. 发起本地页内存紧凑与拷贝 [职责 3.3: 内存紧凑整理]
        Note over Sched, Fab: 【互锁】前台算子无锁直读旧地址，保障 [职责 4.1: 流水线重叠]
        Note over Fab: 底层分配限速 QP 进行带宽压制 [职责 5.3: 硬件级 QoS 隔离]
        Fab-->>QoS: 5. 拷贝完毕返回完成事件
        QoS->>Pool: 6. 物理指针原子重组，流转至下沉状态 [职责 3.2: 状态机流转]
        Pool->>Pool: 7. 容量预估更新 [职责 3.1: 容量预估] 并回收旧资源
    end
```

### 4.4 需求互锁的充分必要性理论推导

为什么说这 140+ 条需求是最终打造高性能 **异构统一 KVCache 存储池** 的**充分必要条件**？

* **必要性论证（Necessity）**：
  * **若抽离控制面准入需求**（如删除 `L2-CONN-CostAwareReturn-039`）：拥塞或网络丢包时，系统会强制拉取远端 KVCache，导致单次请求的 TTFT 突破秒级，推理服务时延 SLA 崩溃。
  * **若抽离数据面零拷贝与硬件协作需求**（如删除 `L4-RDMA-P2P-NPU-001` 或 `L4-NET-OFFLOAD-DPU-001`）：KVCache 搬运将退化为 Host CPU 套接字拷贝。单次搬运耗时从 1ms 暴增至数十 ms，TPOT 劣化，导致 NPU 因严重算力饥饿而空转（Stall）。
  * **若抽离 QoS 隔离与无锁一致性需求**（如删除 `L3-CO-MigrationRCULock-090` 或 `L4-HW-NICSpecConstraint-075`）：背景碎片整理（Compaction）和冷热下沉产生的大流量会直接霸占 PCIe 物理带宽和网卡 SRAM 上下文缓存，挤兑前台推理算子的计算流。推理服务的 P99 TPOT Jitter 将恶化 1.5 倍以上，并引发卡间对齐崩溃。
* **充分性论证（Sufficiency）**：
  * 这 100 多条需求在 **“推理调度（L1）- 协议适配（L2）- 控制大脑（L3）- 硬件通道（L4）”** 每一层都建立了上下游互锁关系。它闭环了容量估算、微秒级快判、零拷贝直传、无锁迁移防悬空、租户域隔离以及硬件错误映射自愈的全生命周期。没有遗留任何技术和安全死角，足以在任何复杂物理拓扑下达成 SLA 时延约束下的 KVCache 虚拟化流动目标。

---

## 五、 系统级需求图谱与技术互锁网络 (System-Level Requirement Atlas)

以下**需求图谱与技术图谱**清晰展现了 SRS 中的离散需求如何相互咬合、逐层收拢服务于中间技术模块，并最终构筑成 **异构统一 KVCache 存储池** 这一轴心目标：

### 5.1 整体技术图谱树状映射图

```mermaid
flowchart TB
    classDef root fill:#D81B60,stroke:#880E4F,stroke-width:3px,color:#FFFFFF,font-weight:bold;
    classDef sys fill:#1E88E5,stroke:#0D47A1,stroke-width:2px,color:#FFFFFF,font-weight:bold;
    classDef srs fill:#ECEFF1,stroke:#607D8B,stroke-width:1px,color:#263238;

    Root["【轴心目标】<br/>SLA时延约束下隐藏介质延迟，NPU饱和输出<br/>(构建异构统一 KVCache 存储池)"]:::root

    %% Grouping into 3 main pillars vertically
    subgraph Pillar1 ["TTFT 保障面"]
        direction TB
        Sys1["TM1: 推理网关与标准适配<br/>(动态收益评估与算力解耦)"]:::sys
        SRS1["L2-CONN-API-STD-001 (标准化IDL)<br/>L2-CONN-BufferContract-040 (指针契约)<br/>L2-CONN-CostAwareReturn-039 (自适应代偿)<br/>L1-RT-Admission-007 (收益准入)<br/>L1-RT-WatermarkAdmission-024 (水位反压)"]:::srs
        Sys1 --> SRS1
        
        Sys2["TM2: 元数据与检索网络<br/>(微秒前缀匹配与原子发布)"]:::sys
        SRS2["L2-CONN-PFX-IDX-005 (5μs两级快判)<br/>L3-MS-TTFTIndexLayout-088 (SIMD RadixTree)<br/>L1-VLLM-PFX-IDX-005 (GPU hash)<br/>L3-MS-AtomicPublishVisibility-087 (原子发布)<br/>L3-MC-PFX-IDX-003 (单边RDMA目录)"]:::srs
        Sys2 --> SRS2
    end

    subgraph Pillar2 ["数据流动面"]
        direction TB
        Sys3["TM3: 多级分层存储池<br/>(空间虚拟池化与碎片平抑)"]:::sys
        SRS3["L3-MC-POOL-ALLOC-005 (NUMA亲和)<br/>L3-MC-CompactionEngine-056 (异步紧凑)<br/>L3-MC-IntelligentMigration-057 (水位迁移)<br/>L4-C2C-UNIFY-POOL-001 (统一地址池)<br/>L1-MM-Lifecycle-009 (生命状态机)"]:::srs
        Sys3 --> SRS3

        Sys4["TM4: 零拷贝与组播编排<br/>(零拷贝直传与多播分发)"]:::sys
        SRS4["L4-RDMA-P2P-NPU-001 (NPU零拷贝直传)<br/>L4-NET-OFFLOAD-DPU-001 (DPU控制卸载)<br/>L2-OL-BulkDescriptor-025 (聚合描述符)<br/>L3-TRANS-MUL-ENGINE-003 (1→N高效组播)<br/>L3-SE-MultiReplicaResolver-089 (最优路径解析)"]:::srs
        Sys4 --> SRS4
    end

    subgraph Pillar3 ["安全、QoS与可靠性面"]
        direction TB
        Sys5["TM5: 协同共识与 QoS 隔离<br/>(前后台硬流量隔离与一致性)"]:::sys
        SRS5["L3-CO-MigrationRCULock-090 (RCU无锁页迁移)<br/>L4-HW-NICSpecConstraint-075 (网卡QP规格)<br/>L4-QO-MigrationQoS-064 (迁移物理隔离)<br/>L3-CO-RefCountLifecycle-086 (引用锁定)<br/>L1-PD-RankConsensus-013 (卡间前缀共识)"]:::srs
        Sys5 --> SRS5

        Sys6["TM6: 全栈遥测与容错自愈<br/>(白盒性能诊断与RAS自愈)"]:::sys
        SRS6["L2-OB-PathTrace-030 (自适应决策Trace)<br/>L3-FT-FallbackTrace-046 (降级因果追溯)<br/>L4-FT-RASErrorMap-061 (RAS错误映射)<br/>L1-OB-SemanticMetrics-016 (命中收益分解)"]:::srs
        Sys6 --> SRS6
    end

    Root --> Pillar1
    Root --> Pillar2
    Root --> Pillar3
```

### 5.2 核心业务链互锁图谱分析

SRS 表格中的离散需求在逻辑上被编译为三条闭环的**核心技术互锁链条**，以此保证了系统的充分性：

* **链条 1：TTFT SLA 保障与自适应成本绕行链 (SLA Guarantee Chain)**
  * *互锁关系*：在请求进入时，前缀预算 `L1-PM-PrefixBudget-001` 与异步预检测 `L1-VLLM-PFX-SCHED-006` 并发执行哈希校验；若是 Miss，`L2-CONN-PFX-IDX-005` Bloom Filter 立即本地短路返回，绝不产生网络 RTT 开销。若是物理 Hit，元数据检索在 `L3-MS-TTFTIndexLayout-088` 固定的 Host DDR 内存中由 SIMD 硬件级快速判定，并在 `L2-CONN-CostAwareReturn-039` 处引入代价计算。一旦判定加载时延高过本地重算代偿，立刻返回 `RECOMPUTE`。
  * *结论*：该链条完全闭环了控制路径，保证前缀共享的开启在任何拓扑和网络状况下**绝对不带来 TTFT 负收益**。
* **链条 2：数据面零拷贝与高速组播数据流动链 (Zero-Copy Data Chain)**
  * *互锁关系*：一旦网关放行，`L2-OL-BulkDescriptor-025` 自动将离散块合并，`L3-SE-MultiReplicaResolver-089` 计算出网络拓扑最佳的 NPU 源节点，通过 `L4-OL-RegisteredPool-053` 长周期物理注册区绕过 CPU 中断。NPU 直接执行 `L4-RDMA-P2P-NPU-001`（GPUDirect RDMA），数据面完全在硬件中完成 zero-copy 流动，辅以 DPU `L4-NET-OFFLOAD-DPU-001` 硬件卡量化编解码。
  * *结论*：该链条消除了 CPU 软件栈开销，将数据搬运延迟逼近物理硬件极限，**实现了“远端访问近端化”**。
* **链条 3：背景 QoS 限速与无锁 RCU 隔离链 (QoS & Isolation Chain)**
  * *互锁关系*：当背景水位需要执行迁移时，`L3-CO-RefCountLifecycle-086` 检查活跃引用计数以防物理块被脏覆盖。拷贝启动后，前台算子通过 `L3-CO-MigrationRCULock-090` 指针指向继续直读旧 HBM，后台迁移通过 `L4-QO-MigrationQoS-064` 和 `L4-HW-NICSpecConstraint-075`（板载 SRAM Context Cache > 128K QPs）在 NPU 网卡固件级进行硬件限速，绝对不挤兑前台推理算子的 PCIe 通路。
  * *结论*：该链条从底层硬件到上层状态机锁死了背景流的带宽，**保障了多租户、多轮对话下推理 P99 延迟（TPOT Jitter）不发生任何长尾波动**。

---

## 六、 软件业务需求覆盖列表 (Traceability Matrix)

为了向技术主管清晰展现系统技术实现与 SRS 原始需求的百分之百对齐，以下表格建立了**宏观子系统到具体需求 ID 之间的映射关系**（重点标注了 v2.3 评审新增需求）：

| 序号 | 宏观技术子系统 | 主要技术职责 | 解决的核心系统瓶颈与第一性原理 | 覆盖的 SRS 需求 ID |
|:---|:---|:---|:---|:---|
| **1** | **推理网关与标准适配子系统** | 1. 统一接口 API 协议规范<br>2. 拓扑与拥塞感知路径路由<br>3. 强反压与时延预算准入<br>4. 算力执行内存与存储解耦 | **解决“算力调度与存储物理拓扑解耦及拥塞拉取下的算力饥饿”系统级瓶颈**<br>自适应算力代偿防前台挂起， Pull-to-Pointer 契约解耦框架内存，实现平台无关。 | **L1 推理层**：L1-PM-PrefixBudget-001, L1-RT-Admission-007, L1-RT-CacheAwareRouting-006, L1-VLLM-HIER-SCHED-002, L1-SE-IntentAPI-014, L1-SE-ExpertOverride-015, L1-RT-WatermarkAdmission-024<br>**L2 适配层**：L2-CONN-API-STD-001, L2-CONN-TOPO-ROUTE-002, L2-CONN-WatermarkQuery-032, <font color="red">**L2-CONN-CostAwareReturn-039** (自适应代偿返回)</font>, <font color="red">**L2-CONN-BufferContract-040** (内存权责解耦)</font><br>**L3 管理层**：L3-SE-PolicyEngine-032, L3-SE-UnifiedDescriptor-018, L3-TRANS-CAP-API-002, L3-TRANS-SEM-ENGINE-001, L3-SE-GranularityDispatch-033, L3-SE-ViewCopyCostModel-034, L3-TRANS-TOPO-SENSE-004, <font color="red">**L3-SE-MultiReplicaResolver-089** (最优多副本解析)</font><br>**L4 传输层**：L4-FABRIC-ROUTER-001, L4-HW-SemCapTable-050, L4-HW-AddrTrans-051, <font color="red">**L4-HW-NICSpecConstraint-075** (RNIC QP规格)</font> |
| **2** | **极速分布式元数据与检索网络** | 1. 本地两级快速过滤 (FPR)<br>2. 一致性哈希分片全局目录<br>3. 前缀索引原子可见发布<br>4. 非交换内存极速 Radix Tree | **解决“分布式超大规模下的元数据 RTT 风暴及检索路径串行化阻塞”系统级瓶颈**<br>非交换内存常驻与 SIMD 比对大幅压缩检索耗时，本地 Filter 与 RCU 目录原子发布保障多节点读写无错配。 | **L1 推理层**：L1-PM-SecureHash-005, L1-SGL-PFX-IDX-003 (SIMD加速), L1-VLLM-PFX-SCHED-006, L1-VLLM-PFX-IDX-005 (GPU hash)<br>**L2 适配层**：L2-CONN-PFX-IDX-005, L2-CONN-META-CACHE-004, L2-PM-BatchLookup-021 (批量查询)<br>**L3 管理层**：L3-MS-DirectoryMirror-036, L3-MS-MetadataDataPlane-035, L3-MC-PFX-IDX-003, L3-MC-PFX-REPL-004, L3-MS-MultiReplicaDirectory-037, L3-PM-HitQuality-023, <font color="red">**L3-MS-AtomicPublishVisibility-087** (原子发布流)</font>, <font color="red">**L3-MS-TTFTIndexLayout-088** (DDR检索RadixTree)</font> |
| **3** | **异构多级存储池与生命周期空间** | 1. HBM/DDR/SSD/Object 全局内存注册<br>2. 规格自适应估算 API<br>3. 一致性状态机生命周期管理<br>4. 异步物理内存紧凑整理 (Compaction)<br>5. 三级水位智能主动迁移 | **解决“大规模长文本推理下物理显存墙硬性限制与空间高碎片化”系统级瓶颈**<br>建立全局异构介质虚拟化平面与 READY/LOADING/EVICTING 强一致状态机，后台异步 Compaction 彻底平抑碎片率至 10% 以下。 | **L1 推理层**：L1-MM-Lifecycle-009, L1-MM-KVSizing-008, L1-OL-ActiveWarmClass-010, L1-OL-PartialBoundary-012, L1-OL-ViewVsCopy-011, L1-MM-DefragAware-023<br>**L3 管理层**：L3-MC-POOL-GLOBAL-001, L3-MS-Tiering-038, L3-MC-HIER-STORE-002, L3-MS-CostEviction-039, L3-MC-POOL-ALLOC-005, L3-MC-UBAllocator-049, L3-MC-CompactionEngine-056, L3-MC-IntelligentMigration-057<br>**L4 传输层**：L4-C2C-UNIFY-POOL-001, L4-UB-C2C-UNIFY-002 (C2C直访), L4-UB-P2P-FABRIC-003 (GAP访问) |
| **4** | **零拷贝硬件通道与多播数据流编排** | 1. 批量传输聚合描述符<br>2. NPUDirect RDMA 零拷贝通路<br>3. 1→N 高效组播多播分发<br>4. DPU 卸载与在线硬件压缩/解压 | **解决“海量数据物理移动下的 Host CPU 软件栈开销过大及 PCIe 抢占”系统级瓶颈**<br>利用长周期预注册池和 GPU-Direct RDMA 消除 CPU 控制栈，在线解压缩与 1→N 硬件组播消除发送端网络瓶颈。 | **L1 推理层**：L1-SGL-PRFCH-SCHED-004, L1-SGL-SWP-XFER-002, L1-VLLM-PRFCH-SCHED-004, L1-VLLM-SWP-XFER-003<br>**L2 适配层**：L2-CONN-PRFCH-ENGINE-007, L2-OL-BulkDescriptor-025, L2-OL-LayoutNegotiation-024<br>**L3 管理层**：L3-MS-HotReplication-040, L3-TRANS-MUL-ENGINE-003, L3-C2C-POOL-SCHED-002, L3-LMC-PRFCH-ENGINE-001, L3-UB-TOPO-ROUTE-001<br>**L4 传输层**：L4-OL-RegisteredPool-053, L4-RDMA-P2P-NPU-001 (GPUDirect), L4-RDMA-MUL-FABRIC-002, L4-MC-HIER-STORE-001, L4-MC-HIER-STORE-002, L4-MC-HIER-STORE-003, L4-NET-OFFLOAD-DPU-001 |
| **5** | **多卡协同共识、租约与 QoS 隔离** | 1. 卡间 usable prefix 长度对齐<br>2. ViewLease 安全租约与多消费者共读<br>3. 并发无锁 RCU 迁移指针原子替换<br>4. 引用计数驱逐锁定机制<br>5. 迁移带宽限制与物理通道硬隔离 | **解决“并发迁移/紧凑整理流量与推理算子争抢 PCIe 带宽的物理隔离”系统级瓶颈**<br>Rank Consensus 对齐 Usable 长度；Active Refcount Lock 锁住活动块；RCU 无锁迁移配合固件级 QoS QP 物理隔离确保 TPOT 稳定。 | **L1 推理层**：L1-SC-TenantIsolation-017, L1-PD-RankConsensus-013<br>**L2 适配层**：L2-MM-ViewLease-028, L2-MM-MultiConsumer-027, L2-CONN-DefragPause-031<br>**L3 管理层**：L3-CO-VersionPublish-043, L3-QO-SemanticQoS-045, L3-SC-PoolIsolation-048, <font color="red">**L3-CO-RefCountLifecycle-086** (引用计数驱逐保护)</font>, <font color="red">**L3-CO-MigrationRCULock-090** (RCU页迁移无锁读)</font><br>**L4 传输层**：L4-CO-FencePrimitive-052, L4-QO-TrafficClass-056, L4-SC-ViewProtection-062, L4-CO-PageMigration-063, <font color="red">**L4-QO-MigrationQoS-064** (迁移物理隔离)</font> |
| **6** | **全栈遥测与故障容错自愈** | 1. 全路径决策 Trace (命中了为什么慢)<br>2. 降级链路因果追溯 (Fallback Trace)<br>3. RAS 物理错误翻译映射<br>4. 统一接口状态/异常错误码 | **解决“灰度/降级长尾延迟难定位与硬件物理故障挂起”系统级瓶颈**<br>路径决策与 Fallback causality trace 使降级显性化；物理层 RAS 故障收拢翻译至软件标准错误码，提供自愈高可用。 | **L1 推理层**：L1-OB-SemanticMetrics-016 (可观测收益)<br>**L2 适配层**：L2-FT-FallbackContract-029, L2-OB-PathTrace-030<br>**L3 管理层**：L3-OB-PerPathTelemetry-047, L3-FT-FallbackTrace-046, L3-MS-UBC2CTier-055, L3-OB-KVStateTrace-083, L3-CTRL-KVInspectAPI-084<br>**L4 传输层**：L4-FT-RASErrorMap-061, L4-OB-RemoteAccessCounter-057 |

---
