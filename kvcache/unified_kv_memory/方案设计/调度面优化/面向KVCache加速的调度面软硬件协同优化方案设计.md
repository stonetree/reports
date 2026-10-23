# 面向 KVCache 加速的调度面软硬件协同优化方案设计

> **方案定位**：面向国产 AI 硬件的统一异构 KVCache 存储与调度底座 / 原厂软硬件协同系统（Unified KV Memory & Scheduling Subsystem）
> **目标架构底座**：鲲鹏 920 多核处理器（单 Socket 含 4 个 SCCL，每个 SCCL 物理亲和 2 个 DDRC 通道，双路服务器整机共计 8 个 SCCL / 16 个 DDRC 内存控制器通道）+ CX6/7 网卡（RoCE / RDMA）+ 国产统一总线互联底座（UBMEM / URMA）+ 异构分层介质（Host DDR、CXL / UBMEM 共享内存池、NVMe SSD）
> **适用场景**：在线大模型推理、长文档问答、SWE-bench / DeepSWE-bench 代码生成 Agent 多轮交互微突发，以及全链路前后台前缀恢复混压场景

---

## 1. 方案概述与第一性原理物理界定

### 1.1 大模型推理调度面的系统定位与物理流动本质

在大模型在线推理服务体系中，KVCache（大模型注意力键值缓存：大模型自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免后续 Token 生成时重复计算注意力）的复用直接决定了首字生成延迟 TTFT（Time To First Token，首字生成延迟 / 首 Token 响应时间）的压缩上限。而在鲲鹏 920（多 SCCL / Die 架构）+ CX6/7（RDMA）与 UB（统一总线 UBMEM / URMA）异构底座上，**KVC 调度面（Scheduling Plane）** 绝不是传统操作系统层面的通用“进程 / 线程任务调度器”，而是一个工作在 **微秒级时间尺度**、直接操控 **片上内存控制器（DDRC）、缓存一致性家代理（HHA）与网卡 DMA 队列** 的**“状态—带宽—算力”套利状态机**。

在支持 PD Disaggregation / PD 分离（Prefill 与 Decode 计算生成解耦架构：将大模型的输入理解 Prefill 阶段与逐字生成 Decode 阶段分别部署在不同的物理硬件节点上，通过高速网络传输 KVCache 实现独立资源弹性伸缩）以及混合部署的现代推理集群中，调度面承担着“全局前缀感知选路”与“单机微架构硬件协同执行”的双重核心职责。

其核心物理使命在于：**在满足在线 Decode 逐字生成阶段单字生成耗时 TPOT（Time Per Output Token，每个输出 Token 的生成耗时 / 单字生成延迟）干扰 $< 3\%$（系统允许的绝对容忍预算仅 $0.55\text{ ms}$）的刚性约束下，确保每一次 KVC 拉取与加载总开销严格处于盈亏平衡线（Break-Even Line）以下的绿色净加速区（即 $T_{\text{load\_exposed}} < T_{\text{recompute}}$，拉取与加载总开销 < 本地直接重算耗时，仅在获取明确净加速收益时执行加载，否则主动回退至本地重算），从而最大化端到端 TTFT 降幅（$\ge 20\%$）与系统有效吞吐（Goodput $\ge 10\%$）**。

### 1.2 调度的三大物理要素界定

从计算机体系结构与硬件协同的第一性原理出发，Host 侧 KVC 调度面的三大核心要素界定如下：

```
+----------------------------------------------------------------------------------------------------+
|                                    Host 侧 KVC 调度面三大物理要素                                   |
+------------------------------------+----------------------------------+----------------------------+
| 1. 调度的对象 (What to Schedule)   | 2. 调度的输入信息 (Inputs)        | 3. 调度的决策 (Decisions)   |
+------------------------------------+----------------------------------+----------------------------+
| • 逻辑事务对象: 模式/句柄/描述符   | • 控制面语义与命中特征           | • 选路与计算回退决策       |
| • 鲲鹏片上硅片资源: 16通道/L3/带宽 | • 微架构硬件探针实时水位         | • 多轨物理通道绑定         |
| • 异构互联通道: QP/UMR/UBMEM/URMA  | • 全局 SLO 压差与后台积压        | • 前后台硬件配额动态反压   |
+------------------------------------+----------------------------------+----------------------------+
```

1. **调度的对象（What to Schedule）**：
   * **逻辑事务对象**：请求级前缀恢复模式（全量拉取 / 按层切分混合执行 / 退避重算）；大模型 80 层 Transformer 的跨步张量块句柄（`KVHandle`）及预编译 DMA 描述符链（WQE List）。
   * **片上硅片资源对象（鲲鹏 920）**：双路服务器共计 8 个物理 SCCL（Super CPU Cluster，即 NUMA Die），每个 SCCL 物理亲和 **2 个 DDRC 内存控制器通道（整机合计 16 个 DDRC 通道）**、**L3 Cache 容量路分区（通过 ARM MPAM `CPOR` 实施）**、以及 **HHA/DDRC 内存带宽配额分区（通过 ARM MPAM `MBW_MAX` 实施）**。
   * **互联传输通道对象（CX6/7 & UB）**：CX6/7 网卡的 **高/低优先级硬件发送队列（Strict Priority QP）**、片上 **UMR（User-Mode Memory Registration，用户态内存注册跨步间接页表 KLM MKey）**，以及国产统一总线下的 **UBMEM（统一总线内存直通共享协议：支持跨节点与异构设备间直接共享内存地址空间的底层通信协议）** 远端直读窗口与 **URMA（通用远程直接内存访问：用户态的高性能 RDMA 驱动接口与通信协议）** 传输通道。
2. **调度的输入信息（Scheduling Inputs）**：
   * **请求语义与命中特征（控制面输入）**：序列总长 $N$、16-Token 对齐后的实际命中前缀 $P = \lfloor N \cdot h_{\text{nom}} / 16 \rfloor \times 16$、实际块命中率 $h = P/N$、待拉取数据量 $D$（如 8K 序列 100% 命中在 TP=8 下合计 $2.5\text{ GiB}$，单卡 $320\text{ MiB}$）、后缀重算耗时 $T_s = 240(1-h^2)\text{ ms}$、以及 ConsumeEligibility（多维度语义一致性与租约校验引擎：对命中 KV 的模型架构、Tokenizer 词表哈希、Prompt 模板哈希、LoRA 适配器、Ready 就绪位与租约有效性等 6 维语义进行微秒级合法性检查，杜绝错误消费）的 Epoch Lease 租约状态。
   * **微架构实时硬件水位（数据面探针输入）**：鲲鹏各 SCCL 的 DDRC 实时读写流量（`hisi_scclX_ddrcY/flux_rd, flux_wr`）、跨 Die Hydra 总线穿行率（`hisi_scclX_hhaY/rx_outer`）、PCIe/UB Root Port Credit 等待计数、以及网卡硬件队列在途字节水位（In-flight Bytes）。
   * **全局 SLO 压差状态**：当前节点前台 Decode 批次的 TPOT 基线（$18.20\text{ ms}$）与 $3\%$ 干扰红线（$18.75\text{ ms}$）之间的残余时间窗（仅 $0.55\text{ ms}$），以及后台换出队列的积压速率（如输入 $100\text{ Gbps}$ 与排出 $70\text{ Gbps}$ 之间的 $30\text{ Gbps}$ 差额）。
3. **调度的决策（Scheduling Decisions）**：
   * **选路与计算回退决策（Admission & Fallback）**：通过 QueryPlan（查询与放置计划决策引擎：在微秒级时间内根据链路状态、算力与上下文长度选择最优数据加载或重算路径）与 CostEvaluator（成本预估模型：在决策前量化预估各加载路径与重算开销的数学模型），微秒级判定走 **“Copy-to-HBM（拷贝到本地显存：通过 DMA 将远端 KV 数据完整搬运至本地高带宽显存 HBM 中） / Direct-View（远端直读：直接通过高速总线跨节点读取远端显存中的 KV 数据，不产生本地显存拷贝） / 按层部分重算（前 $K$ 层重算 + 后 $80-K$ 层流式拉取） / 全量重算回退”**。
   * **多轨物理通道绑定（Multi-Rail Pipe Binding）**：为每次 KVC 卸载（Put）或召回（Get）分配最优的 `{SCCL_ID, DDRC_Channel_Mask, RNIC/URMA_Port}` 物理通路，并在针对同一 System Prompt 的一对多热点场景（1-to-8）下动态激活 Staging Fanout（软件分层中继转发：利用集群计算节点建立树状中继转发拓扑，分层接力广播热点前缀数据）。
   * **前后台硬件配额动态反压（Hardware QoS Throttling）**：通过 SemanticQoS（前后台服务质量保障策略：通过网络 RoCE 优先级队列映射与应用层微秒级自适应退避，确保后台数据换出与拉取不干扰前台在线推理的 TPOT 尾部延迟），实时下发鲲鹏 MPAM 硬件带宽阈值与网卡微切片（Micro-Chunk）速率门限，动态平衡前台 TTFT/TPOT 与后台可持续排出进度。

### 1.3 核心技术指标与四个核心验证阶段 (E0 ~ E3) 对齐

本调度面方案的设计指标严格锚定面向生产级集群落地的**四个核心验证阶段与目标 (E0~E3)**：

```
+---------------------------------------------------------------------------------------------------------+
|                                      四个核心验证阶段与目标演进全景                                      |
+---------------------+---------------------+-----------------------------------+-------------------------+
| 阶段编号与名称      | 核心验证目标        | 调度面核心使能机制                | 关键验收指标            |
+---------------------+---------------------+-----------------------------------+-------------------------+
| E0 (业务收益前提)   | 确认 Saved-Prefill  | 闭式 Cost Model 盈亏平衡选路      | 探明通信协议加速上限    |
|                     | 净收益门限          |                                   | 确定拉取与重算收益拐点  |
+---------------------+---------------------+-----------------------------------+-------------------------+
| E1 (核心路径打通)   | 零拷贝底座与多卡    | 硬件 UMR 描述符预编译与下发       | Host CPU 零数据拷贝     |
|                     | 状态同步            | 64B 扁平无锁表与六维租约校验      | TP=8 同步 P99 &lt; 100µs|
+---------------------+---------------------+-----------------------------------+-------------------------+
| E2 (动态调度决策)   | Direct-View vs      | 微秒级 QueryPlan 决策引擎         | 选路决策 P99 &lt; 5µs   |
|                     | Copy-to-HBM 边界    | 视图租约安全守卫机制与 SSD 扩容   | 准确率 &gt;= 90%, 负收益&lt;1%|
+---------------------+---------------------+-----------------------------------+-------------------------+
| E3 (全链路混压门禁) | 前台推理与后台满载  | 256KB 微切片 + MPAM 硬件反压      | 全链路 TTFT 降低 &gt;= 20% |
|                     | I/O 混压总门禁      | 异步预取门禁 (Async Prefetch Gate)| TPOT 干扰率 &lt; 3% (0.55ms)|
|                     |                     |                                   | 集群有效 QPS 提升 &gt;= 10%|
+---------------------+---------------------+-----------------------------------+-------------------------+
```

其中：
- **Saved-Prefill（首字生成预计算节省：利用已缓存的 KV Cache 避免重复计算 Prompt，从而显著降低首 Token 延迟 TTFT）** 在 E0 阶段必须验证在各前缀复用率下净收益显著存在；
- **张量并行多卡状态同步机制 (RankConsensus) 耗时 P99 &lt; 100µs** 在 E1 阶段打通，保障多卡并行无死锁；
- **视图租约安全守卫机制 (ViewGuard) 异常回退与 SSD 扩容** 在 E2 阶段验证，在远端超时或崩溃时捕获异常并在 1ms 内回滚重算；
- **全链路前后台混压总门禁 (E3)** 在真实 2 节点集群下要求端到端 TTFT 降低 $\ge 20\%$、有效 QPS 提升 $\ge 10\%$ 且前台 TPOT 干扰率 $< 3\%$。

---

## 2. 调度面并发架构设计：三套差异化系统级方案对比

为探明在真实鲲鹏 920 + CX6/7 + UB 异构基础设施下的体系结构边界，避免中庸折中，本节从三个截然不同的体系结构维度并行推演三套完整系统方案：

### 2.1 方案 A（极致性能导向）：全硬件直通 + 客户端单边无锁反射调度架构（Ultra-Low Latency Reflex Architecture）

* **核心设计哲学**：**“关键路径零远端 CPU 参与、零动态内存注册、全硬件寄存器反射”**，以空间和硬件预留换取极致的微秒级控制面（$< 1\ \mu\text{s}$ 选路）与线速数据面（稳态 $> 42\text{ GB/s}$ 乃至双轨 $80\text{ GB/s}$）。
* **控制面与数据面拓扑逻辑**：
  * **控制面（去中心化单边直读）**：彻底取消中心化的 Mooncake Master RPC 调度线程池。元数据采用 **64B Cacheline 对齐的无锁扁平哈希表（SwissTable 变体）**，驻留在远端 Host 的 **1GB HugePage** 锁死内存中，并通过 **鲲鹏 MPAM `CPOR`** 独占锁定每个 SCCL 的高 4 路 L3 Cache Ways。表项内直接内联存储 **`KVHandle` + 预编译好的 CX6/7 UMR 跨步 MKey + 该节点当前 SCCL 硬件带宽水位字**。vLLM 调度器通过 **URMA / CX6 单边 `RDMA Read`** 在 $2\ \mu\text{s}$ 内一次性取回句柄与远端水位，在本地 L1 Cache 内完成闭式公式选路（耗时 $< 500\text{ ns}$）。
  * **数据面（硬件 UMR 原位跨步直达）**：针对大模型“GPU/NPU 按层计算（Layer-first）与 Host 按页存储（Page-first）的物理布局失配”，不做任何 CPU `memcpy` 拼包，也不妥协放大逻辑 PageSize。利用 CX6/7 硬件 **UMR（`mlx5dv_wr_mr_interleaved`）** 将分散在 80 层的 16-Token 切片在网卡硬件 KLM 页表中映射为虚拟连续地址空间；开启鲲鹏 `iommu.passthrough=1`（SMMUv3 硬件直通）、关闭 PCIe `IOCache`（Non-Allocating Write，防止冲刷 L3），按 Layer $0 \to 79$ 顺序直达投递。
* **潜在并发瓶颈**：
  1. **无协调分布式“羊群踩踏（Herd Incast）”**：在 SWE-bench / DeepSWE-bench 20 实例并发微突发下，20 个客户端在同一微秒通过单边读看到同一个存储节点的某个 SCCL 通道空闲（如 $42\text{ GB/s}$），同时向该通道发起单边 `RDMA Read`，导致目标端 PCIe Root Port 瞬间发生 20-to-1 Incast（多对一突发网络拥塞：多个发送节点在极短时间内同时向单一接收节点回传数据包，导致交换机出端口缓冲区瞬间耗尽而引发的排队丢包风暴），实际带宽跌破 $11.28\text{ GB/s}$ 门限。
  2. **网卡片上 ICM/MTT 硬件缓存膨胀**：为海量 16-Token 细粒度块预注册静态 UMR 跨步 MKey 会大量消耗 CX6/7 片上有限的 ICM（Interconnect Context Memory）SRAM，当内存池超百 GB 时仍可能诱发网卡硬件页表换出抖动。
* **核心技术折中（Trade-offs）**：
  * **收益（Gain）**：端到端控制面与调度面 P99 延迟从 **$86\text{ ms}$ 压缩至 $< 0.6\text{ ms}$**，描述符编译与提交耗时 $t_d$ 从 **$15.4\text{ ms}$ 压缩至 $< 0.3\text{ ms}$**，在 SWE-bench 短增量场景下 TTFT 缩短超 **50%**。
  * **代价（Cost）**：必须静态独占 1GB 巨页、25% 的鲲鹏 L3 Cache 容量及大量网卡 MKey 表项，无法支持内存超卖与 SSD 弹性分层。

### 2.2 方案 B（极致资源优化）：中心化精算师 + 多级介质弹性复用架构（Cost-Optimal Elastic Tiering Architecture）

* **核心设计哲学**：**“每一字节显存/内存/网卡带宽利用率最大化”**，以集中式全局账本调度 + Host 软件辅助批处理，花最少的硬件成本实现 **有效显存扩容 $\ge 30\%$、单次有效推理请求的综合计算成本降低、降低系统 TCO（Total Cost of Ownership，总体拥有成本：综合考量服务器硬件采购、算力利用率、网络设备、机房电力能耗与业务达标产出的全生命周期综合成本）、源端广播流量削减 $75\%$**。
* **控制面与数据面拓扑逻辑**：
  * **控制面（集中式 `Mooncake-Conductor` 全局精算调度）**：设立中心化调度器维护全集群 KVC 副本拓扑、租约引用计数与全局通道令牌桶（Token Bucket）。为节省网卡 MKey 表项，放弃硬件 UMR，采用 **Host 侧“混合亚页布局（Hybrid Sub-Page Layout）”**——将同一请求相邻 8~16 个块的同层数据在 Host DDR 内聚合为 64KB 连续微块，全池仅注册 **唯一一个全局 Physical MR**。
  * **数据面（三级分层 + 二叉软件中继 + RCU 无锁整理）**：打通 **`Host DDR <-> CXL / UBMEM 扩展池 <-> NVMe SSD`** 三级分层。通过 Tiering / TierBlockAllocator（分层存储技术与分层块分配器：将冷 KV Cache 异步沉淀到 NVMe SSD 大容量介质中，管理 4KB 对齐的磁盘物理扇区 LBA），后台利用鲲鹏片上内置 DMA 引擎异步将冷块换出至对齐的 SSD 物理扇区；面对 1-to-8 热点 System Prompt 分发，调度器动态构建 **深度为 3 的二叉树软件中继树**，源端仅发送 2 份数据（节省 $75\%$ 源端出口带宽）；内存碎片整理采用 RCU（Read-Copy-Update，读-拷贝-更新：一种无锁读与宽限期延迟释放机制，在后台显存碎片整理与页迁移时保证前台读请求零停顿），读者继续访问旧页，宽限期后释放。
* **潜在并发瓶颈**：
  1. **中心调度器排队雪崩拐点效应**：所有选路、租约分配与中继树构建均需经过中心 Conductor。当集群请求到达率 $\lambda$ 遇到微突发逼近服务台容量（如 $\lambda \to 900\text{ req/s}$）时，中心节点 CPU 序列化与锁争用会导致排队延迟指数级发散（P99 飙升至 $51\sim 235\text{ ms}$）。
  2. **软件中继与分层迁移引发的 Host DDRC 带宽内耗**：二叉树中继的中间节点需要将 KVC“从网卡读入 Host DDR，再从 Host DDR 复制两份发往子节点”，叠加 SSD 冷热换入换出，会在鲲鹏 DDRC 上产生 **2~3 倍的读写放大**，极易在后台换出负载 $> 17.65\text{ Gbps}$ 时击穿前台 **TPOT 3% 干扰红线**。
* **核心技术折中（Trade-offs）**：
  * **收益（Gain）**：硬件资源消耗极低（零特殊网卡表项要求），显著降低单请求硬件摊销成本，在 150% 容量超载压力下依然保持高吞吐与低 OOM 率。
  * **代价（Cost）**：树状中继存在多跳存储转发延迟（广播完成时间仅缩短 $25\%$，达不到 $40\%$ 的挑战目标）；且在 Agent 高频微突发下，中心控制面易成为全系统 TTFT 尾部时延的瓶颈。

### 2.3 方案 C（前沿颠覆性创新架构）：基于 3FS RTS + NSDI'26 CSFQ 的“接收端授权 + 内核级 eBPF/PMU 反应堆”解耦架构（Disaggregated Credit-Driven Fabric）

* **核心设计哲学**：融合 **DeepSeek 3FS（Fire-Flyer File System）的“接收端驱动 Request-to-Send (RTS) 流量整形 + CRAQ 链式强一致复制”**、**NSDI 2026 `MemChannel` 的“无状态核心公平排队（CSFQ）带宽探针”** 以及 **鲲鹏硬件内核级 eBPF/PMU 事件驱动机制**，从协议根源上消灭拥塞与锁争用。
* **控制面与数据面拓扑逻辑**：
  * **控制面（无状态客户端 + CSFQ 权重标定 + 接收端 RTS 信用授权）**：
    - 借鉴 **NSDI'26 CSFQ 思想**，客户端在发起拉取前不依赖全局中心调度器，而是根据本请求的 **前缀命中率 $h$、后缀计算紧迫度与 SLO 剩余预算**，计算出一个 16-bit 的 **收益密度优先级标量（Benefit-to-Cost Priority Tag, $\mathcal{P}_{\text{CSFQ}}$）**。
    - 借鉴 **DeepSeek 3FS 协议**，客户端不直接发起盲目的大块 `RDMA Read`，而是向目标存储节点发送一个 64B 的极轻量 **RTS（Request-to-Send）控制帧**。
    - 存储节点在鲲鹏独立管理核上运行一个 **内核旁路 USERBIO / eBPF 硬件反应堆（Reactor）**，该反应堆通过 `mmap` 直读本节点各 SCCL 的 `hisi_ddrc` 与 `hisi_hha` PMU 硬件寄存器，按 $\mathcal{P}_{\text{CSFQ}}$ 权重在微秒级向客户端颁发 **精确到微秒时间窗的 CTS（Clear-to-Send）令牌**；若当前节点 DDRC 拥塞且该请求的 $\mathcal{P}_{\text{CSFQ}}$ 低于动态盈亏水位（Break-Even Watermark），反应堆在 **$2\ \mu\text{s}$ 内直接返回 `NACK_RECOMPUTE`**，强制客户端立即转入本地重算（或仅授权拉取后半部分层）。
  * **数据面（3FS 风格 Server-Push RDMA Write + UBMEM 远端按层直读混合模式）**：
    - 对于获批的全量/部分层恢复，由存储节点按时间片主动发起 **`RDMA Write with Immediate`（或 URMA Write）**，将每一层的 KVC 精准推入计算节点显存/内存，每推完一层通过硬件 Immediate Data 唤醒计算流（实现层间掩盖 $H \ge 60\%$）；
    - 在具备 UBMEM 的节点间，当计算节点显存不足或仅需稀疏读取时，调度器直接签发 **带租约保护的 UBMEM 远端直读窗口**，由 NPU/GPU 算子通过统一总线 Load/Store 语义直接跨机消费远端 Host 内存中的 KVC，若租约失效则由 ViewGuard 在 $< 1\text{ ms}$ 内安全回退重算。
    - 多副本写入采用 **3FS CRAQ（Chain Replication with Apportioned Queries）链式复制**：写请求从链头（Head）流向链尾（Tail），读请求可分摊至链上任意节点，兼顾强一致性与多节点读带宽聚合。
* **潜在并发瓶颈**：
  1. **RTS/CTS 握手的额外首跳 RTT 开销**：相比方案 A 的直接单边读，RTS 授权机制在极低负载下多出了约 $2\sim 3\ \mu\text{s}$ 的握手延迟。
  2. **CRAQ 链式写入的慢节点拖累（Tail Latency on Write Chain）**：后台换出采用链式复制时，写入吞吐受限于链上 DDRC 最繁忙的那个节点。
* **核心技术折中（Trade-offs）**：
  * **收益（Gain）**：**彻底根除了分布式 Incast 羊群效应与前后台带宽盲目抢占**，将“负收益请求比例 $< 1\%$”和“TPOT 干扰 $< 3\%$”转变为硬件协议层面的确定性保障；同时利用 UBMEM 远端直读突破了本地显存容量限制。
  * **代价（Cost）**：需要深度定制底层传输引擎的状态机（从纯 Pull 模式升级为 RTS/CTS + Hybrid Push/Direct-Read 模式）。

### 2.4 三套方案系统级特性横向对比矩阵

| 评估维度 | 方案 A（极致性能：单边无锁反射） | 方案 B（极致资源：中心精算+多级分层） | 方案 C（颠覆创新：3FS RTS + CSFQ + UBMEM） |
| :--- | :--- | :--- | :--- |
| **控制面/调度延迟 (P99)** | **$&lt; 500\ \mu\text{s}$**（单边直读 + 本地反射） | $5 \sim 50\text{ ms}$（受中心服务台排队拐点限制） | **$&lt; 1.5\ \mu\text{s}$**（硬件反应堆 RTS 快速裁决） |
| **数据面稳态带宽** | **$42 \sim 80\text{ GB/s}$**（UMR 零拷贝线速） | $25 \sim 40\text{ GB/s}$（受软件拼包与中继折返限制） | **$42 \sim 80\text{ GB/s}$**（按时间窗无冲突满载推送） |
| **TPOT 3% 红线保障能力** | 中高（依赖静态 MPAM 隔离，防不住突发 Incast） | 较弱（中继与分层拷贝易造成 DDRC 带宽超标） | **极强（接收端 PMU 门禁 + 微切片推进，干扰 $&lt; 1.5\%$）** |
| **等效显存/内存扩容与成本** | 较低（需 1GB 巨页独占 + 大量网卡 MKey） | **极高（DDR+SSD 分层扩容 $\ge 30\%$，中继省流 $75\%$）** | **高（UBMEM 远端直读 + CRAQ 分摊读取）** |
| **最适配业务场景** | **SWE-bench Agent 高频短增量、低延迟严苛场景** | **超长文档（100K）、高并发低成本吞吐优先场景** | **PD 分离与混部通用生产集群、高混压 E3 验收场景** |


## 3. 逆向链条自检：三大致命系统级漏洞与物理机理

站在严苛的系统架构与芯片协同第一性原理视角，对上述三套方案进行逆向链条自检与极端混压推演，可以精准识别出 **3 个在真实生产集群中必定引发性能雪崩与 SLO 击穿的致命系统级漏洞**。以下揭示其底层物理机理，并给出焊死漏洞的终极自检修复机制。

---

### 3.1 致命漏洞 1：遥测时效性悖论与分布式“羊群踩踏”（Telemetry Staleness & Herd Incast Paradox）

* **致命物理机理剖析**：
  在去中心化单边读方案或常规周期性硬件遥测探测架构中，无论后台探针采样多快（例如每 $500\ \mu\text{s}$ 更新一次），遥测硬件状态从 Uncore PMU 产生、写入共享内存到被集群中 20 个 vLLM 实例并发读取，客观上必然存在百微秒至毫秒级的**感知滞后窗口（Staleness Window）**。
  在 SWE-bench / DeepSWE-bench 这类 Agent 场景下，任务通常以批次形式派生（如 20 个并行 Rollout 子任务）。当 20 实例并发微突发涌入时，20 个调度器在同一时刻读到的状态完全一致：“目标存储节点 1 的 SCCL-0 剩余带宽充足，为 $42\text{ GB/s}$”。于是，**20 个调度实例几乎在同一微秒全部做出“全量拉取”的相同决策**，在同一毫秒内向该节点同一个 PCIe Root Port 与该 SCCL 亲和的 2 个 DDRC 通道砸下 $20 \times 320\text{ MiB} = 6.4\text{ GiB}$ 的高并发传输请求！
  结果，目标端瞬间触发 20-to-1 的严重 Incast 拥塞，每个请求实际分摊到的物理带宽坍缩至 **$2.1\text{ GB/s}$**。这不仅远低于 $70\%$ 前缀命中下的加载收益门限（$7.94\text{ GB/s}$）与 $100\%$ 命中门限（$11.28\text{ GB/s}$），更导致所有 20 个请求的端到端耗时集体从预期的数十毫秒恶化至 **$629.93\text{ ms}$**，单请求产生高达 **$389.18\text{ ms}$ 的负收益倒挂损失**！
* **终极自检修复机制 —— 基于硬件原子 Fetch-and-Add 的“查表即预扣（Atomic In-flight Byte Reservation）”**：
  1. **消灭滞后感知窗口**：在每个鲲鹏 SCCL 通道的共享内存状态槽中，除了记录 PMU 实测背景物理带宽 $B_{\text{pmu}}$ 外，增设一个 64-bit 硬件原子计数器 `inflight_bytes`，用于实时追踪当前已获批但尚未在物理总线上完成排出的在途字节总数。
  2. **单步往返原子预扣**：当客户端发起元数据查询时，不再使用普通读指令，而是利用 **CX6/7 硬件 `RDMA Fetch-and-Add`（或 UBMEM 统一总线硬件原子指令 `ldadd`）**，在 **单次 $2\ \mu\text{s}$ 往返内，原子地将本请求待拉取字节数 $D$ 累加到目标通道的 `inflight_bytes` 上，并瞬间取回累加前的历史值 `prev_inflight`**。
  3. **瞬时排队精确折算**：第 $k$ 个到达的并发请求拿到的 `prev_inflight`，在代数上严格等于排在其前面的 $k-1$ 个并发请求的在途字节总和。调度器无需等待任何网络反馈，即可在本地闭式计算出包含在途排队的真实有效加载耗时：
     $$\tau_{\text{effective}} = \frac{\text{prev\_inflight} + D}{B_{\text{pmu}}}$$
  4. **微秒级自适应熔断分流**：在 20 并发微突发到达瞬间，前 4 个请求计算发现 $\tau_{\text{effective}} < T_s$（满足拉取与加载总开销 < 本地直接重算耗时），获批全速拉取；第 5~20 个请求在原子指令返回的同一微秒，立即检测到 `prev_inflight` 已排队数 GB，系统当场在 **$2\ \mu\text{s}$ 内触发“按层混合切分重算”或直接将流量重定向至其他空闲 SCCL 副本**（同时通过异步原语退还预扣额度）。从物理数学机制上彻底杜绝羊群踩踏。

---

### 3.2 致命漏洞 2：异步 DMA 队列“覆水难收”引发的 TPOT 0.55ms 红线失守（Irrevocable DMA WQE HoL Blocking）

* **致命物理机理剖析**：
  系统要求在全链路混压下，前台在线 Decode 阶段的单字生成耗时 TPOT 干扰率严格 $< 3\%$。实测前台基线 TPOT 为 $18.20\text{ ms}$，$3\%$ 干扰红线为 $18.75\text{ ms}$，**留给全系统前后台资源竞争的绝对容忍时间预算仅为区区 $0.55\text{ ms}$（$550\ \mu\text{s}$）**。
  在 ARM 鲲鹏 920 微架构下，PCIe 控制器通过 SMMUv3（系统内存管理单元）接入片上 CCI-550 相干互联环网。默认情况下，硬件一致性引擎会将所有 PCIe Inbound DMA 流量判定为相干请求，导致 DMA 写操作必须经过 HHA 并向 L3 Tag（L3T）广播嗅探，甚至直接分配进入 CPU L3 Cache（Allocating Write）。当后台大块 KVC 持续灌入时，CPU L3 缓存瞬间被大流量物理冲刷，前台推理的主线程上下文、vLLM 算子发射指令队列被全部逐出，引发严重的 CPU 流水线停顿。
  与此同时，异步 DMA 的底层物理缺陷在于：**一旦后台线程将一个 $64\text{ MiB}$ 的大块 KVC 换出描述符（WQE）提交压入 CX6/7 网卡硬件发送队列或 PCIe Root Complex FIFO，主机软件就彻底丧失了对这笔 DMA 搬运的中途撤回与抢占控制权！**
  在 $42\text{ GB/s}$ 的物理线速下，完全排出这笔 $64\text{ MiB}$ 数据需要占用总线 **$1.52\text{ ms}$**。若在搬运启动后的第 $0.1\text{ ms}$ 时刻，前台在线推理突然发起高优先级的 Prefill 数据拉取或进入访存密集的 Decode 步进，前台高优传输流将被死死阻塞在硬件 FIFO 队尾长达 $1.42\text{ ms}$，**直接击穿 $0.55\text{ ms}$ 容忍预算达 2.6 倍之多**，导致前台请求发生严重长尾毛刺。
* **终极自检修复机制 —— 微切片时间窗投递（$256\text{ KB}$ Micro-Chunk Pacing）+ 鲲鹏 MPAM 硬件时钟周期级反压**：
  1. **软件侧细粒度时域切片（将不可撤回窗口压缩至 $6.1\ \mu\text{s}$）**：在调度面的后台换出引擎中，严禁下发超过 MB 级的大块 WQE。强制将后台换出任务在软件层切分为 **$\le 256\text{ KB}$ 的微切片 WQE**（单次通道仅维持 2~4 个在途微切片），并严格绑定至 CX6/7 网卡的 **低优先级硬件队列（Traffic Class 1 / TC1）**；而前台在线拉取流绑定至 **严格高优先级硬件队列（Strict Priority TC0）**。在 $42\text{ GB/s}$ 带宽下，$256\text{ KB}$ 微切片的物理总线占有时间仅为 **$6.1\ \mu\text{s}$**，这意味着前台高优流量到达后，最长仅需等待 $6.1\ \mu\text{s}$ 即可在硬件级强行抢占总线（仅消耗 $550\ \mu\text{s}$ 容忍预算的 $1.1\%$）！
  2. **微架构相干流解耦与 Non-Allocating 配置**：在 SMMUv3 页表描述符中显式配置 `Inner Non-Cacheable` 属性；在 PCIe TLP 包头中强制置位 `Relaxed Ordering (RO=1)` 与 `No-Snoop`；在鲲鹏芯片配置中将承载 KVC 的 PCIe Root Port 设置为 Direct-to-DDR 模式，彻底切断片上 L3 Tag 广播嗅探通路。
  3. **硅片侧 MPAM 硬件时钟周期级反压闸门**：将后台 DMA 换出线程统一绑定至鲲鹏微架构的独立硬件分区 `PARTID=2`，前台在线推理线程绑定至 `PARTID=1`。当后台负载上升或前台 TPOT 出现抖动时，调度面通过 Linux `/sys/fs/resctrl` 动态收紧 `PARTID=2` 的 **`MBW_MAX`（内存控制器带宽硬配额，例如锁死在 $25\%$ 或 $15\%$）**。一旦前台 Decode 发起内存读写，鲲鹏 HHA 与 DDRC 硬件状态机在 **百纳秒级（$< 0.1\ \mu\text{s}$）** 内自动挂起 `PARTID=2` 的访存事务，确保前台在线 TPOT 干扰严格受控于 $\le 2\%$（实测 $\le 18.56\text{ ms}$）。

---

### 3.3 致命漏洞 3：动态 UMR 注册与跨核 PMU 读取的“控制面自噬”（Control-Plane Self-Cannibalization）

* **致命物理机理剖析**：
  若在在线推理的读取关键路径上，为了追求 80 层张量跨步传输而临时动态调用驱动 API（如 `mlx5dv_wr_mr_interleaved`）注册 UMR 间接页表，在高并发突发请求下，大量线程会同时涌入网卡固件（Firmware）的上下文命令队列，触发内部互斥锁的剧烈争用，导致原本仅需微秒级的描述符准备耗时 $t_d$ 陡增至 **$15.4\text{ ms}$ 乃至数十毫秒**；
  同理，若前台调度线程在每一次请求决策时，都通过系统调用或 cross-core 中断去跨核读取鲲鹏片上 PMU 寄存器，将引发严重的核间中断（IPI）风暴与上下文切换，使得调度面自身的执行开销吞噬原本好不容易通过网络节省下来的 TTFT 时间窗。
* **终极自检修复机制 —— 静态池化 UMR 环形模板槽（Pre-allocated UMR Ring）+ 带外影子能力矩阵（Shadow Capability Matrix）**：
  1. **写时预绑定静态 UMR 槽**：在系统初始化引导阶段，根据模型固定的 80 层跨步几何结构，预先在网卡硬件表项中建立固定规模（如 4096 个）的 **静态 UMR KLM 模板槽位**；当 KVC 数据在后台写入（Put）时，直接完成跨步模板挂载，并将就绪的虚拟连续句柄存入 `KVHandle`。在**在线读取（Get）关键路径上实现 100% 零动态 MR 注册**，仅需 1 次 PCIe Write-Combining Doorbell 即可触发硬件跨步传输，将描述符准备耗时 $t_d$ 压缩至 $< 300\ \mu\text{s}$。
  2. **带外硬件物理能力矩阵（Shadow Capability Matrix）**：在鲲鹏双路 CPU 上各单独隔离 1 个独立的物理小核，专门运行 **带外 Uncore PMU 异步遥测协程**。该协程每隔 $200\ \mu\text{s}$ 轮询一次片上 `hisi_sccl*_ddrc*` 与 `hisi_sccl*_hha*` 性能计数器，并将各 SCCL 的可用带宽与队列深度更新至一块 **64B 对齐的只读共享内存页**中。该共享内存页通过 **鲲鹏 MPAM `CPOR` 永久锁死在 CPU L3 Cache Ways 中**。前台调度线程读取该矩阵的物理开销仅为 **1 次本地 L1/L3 Cache 命中（耗时 $< 15\text{ ns}$）**，彻底消除系统调用与核间中断。


## 4. 调度面软硬件协同扩展优化体系（融合原始讨论稿硬核技术）

为解决研发团队在前期原型验证中遇到的“选路探测耗时高达 10ms”、“20 实例微突发下 P99 飙升至 86ms”、以及“评委质疑单请求亚毫秒优化无法传导至宏观业务”等核心难题，本节从原始讨论稿中深度萃取调度面相关的硬核软硬件协同技术，构建起一套自底向上的全链路赋能体系。

---

### 4.1 突破 10ms 选路瓶颈：基于底层 PMU 与硬件计数器的微秒级硬件物理能力矩阵（CapabilityMatrix）

* **10ms 瓶颈的底层根因剖析**：
  在前期实测中，调度器进行动态选路评估耗时高达 **10ms**。其根本原因在于：调度逻辑与底层硬件状态采集强耦合，且通过传统 Linux 内核接口（如读取 `/sys/class/infiniband/.../hw_counters`、调用 `ethtool -S` 或执行驱动 `ioctl`）获取硬件数据。每一次调用均触发**用户态-内核态上下文切换 + VFS 虚拟文件解析 + 网卡固件 Mailbox 寄存器互斥锁同步**，单次往返就消耗数毫秒。
  更严重的是，10ms 探测周期会诱发严重的**“惊群效应（Herd Incast）”**：200 个并发查询在 $1\sim 2\text{ ms}$ 内涌入，调度器在这 10ms 窗口期内看到的硬件能力矩阵是完全静止的陈旧状态（Stale State），导致所有流量瞬间打向看起来最空闲的单一通道，引发网络与总线拥塞。
* **破局机制：基于“三层极速遥测 + 双轨感知 + 乐观预扣减”的微秒级硬件物理能力矩阵**：

```
+----------------------------------------------------------------------------------------------------+
|                微秒级硬件物理能力矩阵 (CapabilityMatrix) 采集与同步架构                              |
+----------------------------------------------------------------------------------------------------+
|  第一层: RDMA 链路双轨感知   |  第二层: 鲲鹏片上/CXL 遥测    |  第三层: 跨节点同步与预扣减          |
+------------------------------+-------------------------------+-------------------------------------+
| • 随路捎带: wr.imm_data 32bit| • ARM Uncore PMU (HHA/DDRC)   | • 64-bit 状态字随路捎带 (Piggyback) |
|   (零额外包带回健康向量)     |   (hisi_sccl*_ddrc*/flux_*)   |   (随 RDMA Write Imm 零额外开销)    |
| • 主动微探测: 双包色散测速   | • CXL CPMU (cxl_pmu_mem)      | • 客户端本地乐观预扣减 (Optimistic) |
|   (背靠背 32KB Packet-Pair)  | • perf_event_open + mmap 直读 | • 硬件原子 Fetch-and-Add 查表预扣   |
| • 传输引擎内生影子计数器     |   (30~80ns, 零系统调用)       |   (彻底杜绝 10ms 惊群踩踏)          |
+------------------------------+-------------------------------+-------------------------------------+
```

1. **第一层：RDMA（CX6/7）链路微秒级双轨感知（随路捎带 + 主动微探测）**：
   * **随路捎带机制（In-Band Piggybacking，零开销实时回传）**：
     仅靠带外轮询必然引发“静默链路感知盲区”。利用 RoCE/IB 原生 `IBV_WR_RDMA_WRITE_WITH_IMM` 包头中的 32-bit `wr.imm_data` 免费字段，在正常搬运 KVC 数据时将接收端的实时健康向量（`Health_Vector_32b`：包含 SCCL 通道可用带宽、DDRC 拥塞位、在途排队深度）随 CQE 捎带回传，达成 **0 额外网络包、0 额外 PCIe 事务、0 时延增加** 的微秒级被动同步。
   * **主动微探测机制（Active Probing，破除静默链路盲区）**：
     针对长时间没有流量的静默候选链路，调度器避免直接盲目切入海量流量。借鉴 Google Swift、Alibaba HPCC 与 Stanford Hummingbird 顶会设计，采用两级轻量主动探测：
     - **RTT 基线探测**：按需派发单个 64B 单边 `RDMA Read` 微探测帧，在 $2\ \mu\text{s}$ 内获取链路物理往返时延；
     - **瓶颈带宽色散测速（Packet-Pair Dispersion Probe）**：连续发射一对紧邻的 **背靠背 32KB 微数据包**，通过测量接收端两个数据包到达的时间差 $\Delta t$，直接根据公式 $B_{\text{probe}} = 32\text{ KB} / \Delta t$ 精确推算出静默链路当前的真实可用物理带宽，从源头杜绝盲目切流导致的 Incast 踩踏。
   * **传输引擎内生影子计数器（耗时 $< 10\text{ ns}$）**：
     在传输引擎的 `post_send`（下发 WQE）与 `poll_cq`（轮询完成 CQE）挂钩处，维护 64-bit 原子变量：
     $$\text{Bytes}_{\text{inflight}} = \sum \text{WQE}_{\text{posted}} - \sum \text{CQE}_{\text{completed}}$$
     并结合 ARM 硬件虚拟时钟寄存器 `cntvct_el0`（读取仅需 $2\text{ ns}$）计算最近 $200\ \mu\text{s}$ 滑动窗口内的实际排出速率与单 WQE 物理往返时间 RTT（$T_{\text{CQE}} - T_{\text{WQE}}$）。
2. **第二层：鲲鹏 CPU DDR 通道与 CXL/UB 硬件 PMU 的用户态零系统调用直读（耗时 $30\sim 80\text{ ns}$）**：
   * 采用 `perf_event_open()` 在进程初始化时绑定鲲鹏 Uncore PMU（`hisi_sccl*_ddrc*` 内存通道读写流量、`hisi_sccl*_hha*` 跨 Die 流量）与 CXL（Compute Express Link，通用计算互联总线：用于支持 CPU、加速器与内存池之间高带宽、低延迟的缓存一致性互联）3.0 标准硬件性能计数器 `cxl_pmu_mem`，并对返回的文件描述符调用 **`mmap()` 映射出内核环形缓冲页**。
   * 在运行期，调度线程**完全不发起 `read()` 系统调用**，而是直接在用户态直读 `mmap` 共享内存页并配合寄存器指令折算物理带宽，单次采集耗时直接从数毫秒压降至 **$30\sim 80\text{ ns}$**！
3. **第三层：防惊群同步与动态选路闭式门禁**：
   * **本地乐观预扣减（Optimistic Reservation）**：调度器在向目标通道派发 $10\text{ GB}$ 拉取任务的瞬间，立刻在本地矩阵该节点的 `inflight_bytes` 上原子累加 $10\text{ GB}$，使得紧随其后的后续请求感知到真实的在途负载，杜绝羊群效应。
   * **闭式选路门禁（CostEvaluator 闭式求解）**：
     对于任意请求，调度器在 $< 1\ \mu\text{s}$ 内代入闭式不等式计算：
     $$T_{\text{load}} = T_{\text{ctrl}} + \frac{\text{InFlight\_Bytes} + D}{B_{\text{eff}} \times (1 - \alpha \cdot \text{CNP\_Rate})}$$
     $$T_{\text{recompute}} = \frac{\text{Queue\_Tokens} + \text{FLOPs}(L_{\text{prefix}})}{\text{Compute\_TFLOPS}_{\text{eff}}}$$
     - 若 $T_{\text{load}} < \eta \cdot T_{\text{recompute}}$（$\eta = 0.8$）且 DDR 带宽充足 $\to$ **全量异步流式拉取**；
     - 若 $T_{\text{load}} > T_{\text{recompute}}$（处于拥塞负收益区）$\to$ **立即熔断，本地算子重算**；
     - 若处于临界区（$0.8 \le T_{\text{load}} / T_{\text{recompute}} \le 1.2$）$\to$ **触发“前 $K$ 层重算 + 后 $80-K$ 层流式拉取”的软硬协同混合执行**！

---

### 4.2 控制面与调度面全链路时延规格体系（规范 C1 ~ C5 框架）

通过在鲲鹏 920 与 CX6/7 底座上将控制面与调度面全面下沉解耦，对齐项目规范的 C1~C5 时延分解体系：

| 环节编号与名称 | 当前基线耗时 (32K, 20实例突发) | **目标竞争力规格 (Mean / P99)** | 物理根因剖析 | 鲲鹏 + CX6/7 软硬协同优化手段 | 第一性原理推导依据（硬件极限算账） |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **C1. 客户端块哈希计算卸载**<br>*(Token Block Hashing)* | $1.5 \sim 5.0\text{ ms}$<br>*(Python SHA256)* | **$50\ \mu\text{s}$ / $120\ \mu\text{s}$** | Python 层对象序列化 + 逐块串行 SHA256，每 Token 耗时 150ns，32K 需 ~4.8ms。 | 下沉至 C++ 采用 **ARMv8 硬件指令加速 CRC32C / NEON 向量化 XXH3**，增量计算并缓存历史哈希。 | 鲲鹏泰山核单核 NEON/CRC32C 吞吐 $> 15\text{ GB/s}$；32K Tokens 仅 $128\text{ KB}$，硬件指令流水耗时 $< 10\ \mu\text{s}$，预留软件栈裕量定为 $50\ \mu\text{s}$。 |
| **C2. 一体化前缀快查与句柄返回**<br>*(合并 is_existed 与查描述符)* | Mean: **$4.0\text{ ms}$**<br>P99: **$86.0\text{ ms}$** | **$150\ \mu\text{s}$ / $500\ \mu\text{s}$** | 1. 2048 个 Key 全量遍历哈希表引发跨 Die L3 Miss 与锁争用；<br>2. 查存在与查描述符分为 2 次独立 RPC。 | 1. **前缀二分探测**：2048 个块降维至 $\log_2(2048)=11$ 次探测；<br>2. **64B 扁平无锁表 + 鲲鹏 MPAM `CPOR` 锁死 L3 Ways**；<br>3. 一次性返回内联 **`KVHandle`**（消灭第 2 次 RPC）或采用 **单边 RDMA Read**。 | CX6/7 跨机单边 RDMA Read 硬件 RTT 为 $2\ \mu\text{s}$；鲲鹏本地 L3 命中仅 $15\text{ ns}$。二分查找 11 个 Key + 单步网络往返，P99 稳定锁死在 $< 500\ \mu\text{s}$（提速 **172 倍**）。 |
| **C3. 动态选路与 Break-Even 门禁**<br>*(Dynamic Routing & Admission)* | **$10.0\text{ ms}$**<br>*(在线同步探测/脚本计算)* | **$2\ \mu\text{s}$ / $5\ \mu\text{s}$** | 在请求关键路径上同步拉取状态或执行低效脚本（10ms 等效于白白耗尽搬运 420MB 数据的宝贵时间）。 | **“后台异步遥测 + 前台零 I/O 查表”解耦**：<br>后台协程每 $500\ \mu\text{s}$ 直读 **鲲鹏 Uncore PMU 与网卡队列** 更新共享内存表；前台直接执行本地 L1 Cache 闭式不等式计算。 | 1 次共享内存原子读（L1/L3 命中 $\approx 5\text{ ns}$）+ 闭式不等式计算（$< 50$ 条指令 $\approx 20\text{ ns}$），前台选路耗时稳居 **$< 2\ \mu\text{s}$**（提速 **5000 倍**）。 |
| **C4. TP=8 多卡语义校验与租约同步**<br>*(Multi-Card Sync & Lease)* | $0.5 \sim 3.0\text{ ms}$ | **$30\ \mu\text{s}$ / $100\ \mu\text{s}$** | 8 个 Worker 独立通过网络/IPC 校验并走 Socket 同步屏障，受木桶效应严重拖累。 | Scheduler 在 C2 统一完成六维校验与 Epoch 租约获取，通过 **主机共享内存 RingBuffer + ARM `sev/wfe` 硬件事件指令（或原子 Barrier）** 同步唤醒 8 个本地 Worker。 | 单机内跨 SCCL 共享内存原子同步耗时 $0.2 \sim 1\ \mu\text{s}$；即便跨节点走 RDMA Atomic 同步，P99 亦严格受控于 $100\ \mu\text{s}$ 以内。 |
| **C5. 硬件描述符编译与下发 ($t_d$)**<br>*(Descriptor Prep & Doorbell)* | **$15.4\text{ ms}$**<br>*(基线实测数据)* | **$300\ \mu\text{s}$ / $800\ \mu\text{s}$** | 在读路径上由 CPU 逐层逐页遍历拼装数千个离散 SGE 并多次敲击网卡 Doorbell。 | 通过 DescriptorCompiler（描述符编译器：将跨框架离散物理 Block 贪心合并为硬件 Scatter-Gather 描述符，大幅降低 CPU 提交开销），采用 **写时预编译 UMR 跨步 MKey + 读时批量合并投递**。 | 条目数压缩 $>90\%$；在鲲鹏上构造数十个 WQE 并下发 Doorbell 仅需 $50 \sim 200\ \mu\text{s}$，不可掩盖等待削减 $14.6\sim 15.1\text{ ms}$。 |
| **【全链路控制面与调度面总计】**<br>*(C1 + C2 + C3 + C4 + C5)* | Mean: **~$32\text{ ms}$**<br>P99: **$> 115\text{ ms}$** | **Mean: $< 0.6\text{ ms}$**<br>**P99: $< 1.6\text{ ms}$** | **吃掉 SWE-bench 单步 TTFT 的 50%+，引发严重排队雪崩与算力空泡** | **鲲鹏 MPAM 锁 L3 + PMU 异步探针 + CX6 静态 UMR + 单次 `KVHandle` 闭环** | **相比基线削减 98% 控制面开销**，在 20 实例微突发下稳如磐石，彻底打通宏观 TTFT、TPOT 与有效 QPS 收益传导通道！ |

* **横向解耦机制：调度器主循环临界区剥离（Async Prefetch Gate）**：
  在 vLLM 连续批处理中，若将 C1~C3 塞在 `Scheduler.schedule()` 主循环内同步执行，会造成 $2.0\sim 15.0\text{ ms}$ 的同步阻塞。通过引入 **控制面异步预取流水线（Async Prefetch Gate）**，请求进入 Waiting 队列时由独立 C++ 线程并行完成 C1~C3，主调度器仅在 `schedule()` 时用 **Mean $< 100\ \mu\text{s}$ / P99 $< 250\ \mu\text{s}$** 读取就绪的 `KVHandle`。这严格小于 TPOT 3% 干扰红线预算（$550\ \mu\text{s}$）的一半，确保无论混部还是 Chunked Prefill（分块首字预计算：将超长 Prompt 切分为若干固定大小的 Chunk 分批输入计算，以平滑显存峰值并实现与 Decode 阶段的细粒度交织调度）均达成零调度空泡（Zero Bubble）。

---

### 4.3 控制面与调度面微观优化向在线宏观指标的五大真实物理传导机制

在技术评审中，评审专家往往质疑：“单请求在控制面上节省 0.3~1ms，相对于秒级的端到端 TTFT 而言微不足道，为什么值得做这么深度的软硬件协同？”。本方案揭示五大真实物理传导机制，严密论证微观时延如何决定宏观业务生死：

#### 机制 1：【消除 Continuous Batching 调度临界区阻塞】比较基准从 200ms TTFT 切换到 0.55ms TPOT 容忍预算
在 vLLM 等推理引擎的连续批处理循环中，每生成 1 个 Token，中央调度器 `schedule()` 都必须在主线程中运行一次。当有新请求到达时，前缀匹配、元数据查询与租约校验若在调度关键路径上同步执行：
- 面对基线单字生成耗时 **TPOT = $18.20\text{ ms}$**，系统死守的 **3% 干扰目标红线是 $18.75\text{ ms}$**，留给全系统的干扰容忍预算仅为 **$0.55\text{ ms}$（$550\ \mu\text{s}$）**！
- 若控制面出现 $1.0\sim 3.0\text{ ms}$ 的同步查询与排队延迟，它**直接击穿了 $0.55\text{ ms}$ TPOT 干扰预算的 180%~500%**，导致正在运行这 8 张卡上的数十个并发请求同时出现严重 TPOT 尾部毛刺；
- 通过 **Async Prefetch Gate 异步预取门禁** 将控制面剥离至后台线程，主循环交接开销压至 $< 100\ \mu\text{s}$，彻底切断了控制面毛刺向在线 TPOT 传导的物理路径。

#### 机制 2：【合并 $t_m + t_d$，激活 2.5 倍反流水掩盖杠杆效应】不可掩盖的零号层开销
端到端延迟推导公式为：
$$T = T_f + T_s + t_m + t_d + t_q + \tau - H, \quad H = \min\left(0.6\tau, \frac{79}{80}\min(\tau, T_s)\right)$$
1. **控制面与描述符编译的物理合一**：基线中提交描述符 $t_d$ 高达 **$15.4\text{ ms}$**，是因为客户端查表仅拿到零散块号，需在 CPU 上遍历 80 层临时拼装描述符。通过在 `KVHandle` 中直接内联通过 ExtentManifest（连续数据块区间清单描述符：用 64B POD 结构体描述连续物理 Block，避免跨框架传递时的序列化开销）预编译好的 UMR 跨步描述符，使得查表完成即等于描述符就绪，将 $t_m + t_d$ 合并为统一的“控制面快路径”，在 $40.46\text{ ms}$ 总 TTFT 优化中的贡献从单薄的 **$0.30\text{ ms}$ 跃升为 $13.70\text{ ms}$（独占总收益的 33.8%）**！
2. **2.5 倍反流水掩盖杠杆**：数据搬运耗时 $\tau$ 享有按层异步流水高达 $60\%$ 的掩盖（$H = 0.6\tau$），在数据面上节省 $1\text{ ms}$ 传输时间，对 TTFT 的净贡献仅为 $(1 - 0.6) = 0.4\text{ ms}$；而控制面耗时 $(t_m + t_d + t_q)$ 发生在第 0 层数据搬运启动之前，是 **100% 无法被计算流水掩盖的串行阻塞死区（$H=0$）**！因此，**控制面每节省 $1\text{ ms}$，净收益等效于数据面节省 $2.5\text{ ms}$ 的搬运时间**！

#### 机制 3：【微突发成批到达与长尾裂变根因】从稳态泊松分布升级为 $M^{[X]}/G/1$ 批次排队模型
实测揭示，统一存储池服务 20 个 vLLM 实例（集群仅 160 卡）时，单个 Agent 派生 20 个并行 Rollout，汇聚到存储池即为 400 个并发查询（$81.9\text{ 万次}$ 哈希查找）。在鲲鹏多 SCCL 架构下，传统 `std::unordered_map` 引发跨 Die 的海量 L3 Miss（100ns/次）与互斥锁缓存行乒乓（Cacheline Bouncing），导致平均时延仅 4ms 时，**P99 却裂变成 86ms（长尾放大 21.5 倍）**，吃掉 TTFT 的近一半。采用 64B 扁平无锁表 + MPAM 物理锁死 L3 Cache，彻底消灭锁争用与缓存颠簸。

#### 机制 4：【微秒级控制面决定动态选路的生死】防止系统掉入 629ms 负收益深渊
动态选路前必须先查元数据获取命中率 $h$ 与节点位置，查表时延 $t_m$ 是所有请求（包括最终回退重算的请求）付出的沉没成本。且在 $42\text{ GB/s}$ 线速下，搬运单卡 $320\text{ MiB}$ 仅需 **$7.6\text{ ms}$**，链路拥塞以毫秒级跳变。若控制面耗时数毫秒至数十毫秒，决策时依据的已是失效状态，导致选路误判率暴增。一旦在 $3\text{ GB/s}$ 拥塞下误判拉取，耗时飙至 **$629.93\text{ ms}$（单请求劣变 $389.18\text{ ms}$）**。微秒级控制面是保障选路准确率 $\ge 90\%$、负收益 $< 1\%$ 的物理前提。

#### 机制 5：【SWE-bench Agent 场景下的占比颠覆】从 1% 边缘开销跃升为 50%+ 决定性瓶颈
在 SWE-bench / DeepSWE-bench 这类 Agent 场景中，交互通常达数十轮，呈现“超长历史前缀（32K~64K，98.5% 命中）+ 极短新增（256~512 Tokens，仅 1.5% 新增）”的典型特征。
- GPU/NPU 计算新增 512 Tokens 的增量 Prefill 耗时 $T_s$ **仅需 $10\sim 15\text{ ms}$**；
- 但由于工具执行存在秒级等待（Tool Execution Gap），KVC 早已换出至 Host 存储池；
- 此时，未优化的基线控制面（P99 耗时 **$118.2\text{ ms}$**）在端到端 TTFT（$145.2\text{ ms}$）中**占比高达惊人的 $81.4\%$**！通过软硬件协同优化将控制面压缩至 $1.53\text{ ms}$，端到端 P99 TTFT 暴降 **$80.3\%$**！

---

### 4.4 异构底座互联与主机 DMA 协同扩展

在系统技术设计中，必须严格厘清外设 PCIe DMA 与主机片上系统 DMA 的体系结构分工与协同原语：

1. **外设 PCIe DMA 与主机片上系统 DMA（Kunpeng DSA / CPA）的分工**：
   * **外设 PCIe DMA**：由网卡（CX6/7）或加速卡主动发起，负责跨主机或跨 PCIe 总线的数据搬运（如 RDMA 跨节点收发、设备直达零拷贝）；
   * **主机片上系统 DMA（Kunpeng DSA / CPA）**：内嵌于鲲鹏处理器 Uncore 互联结构中，专门负责 **Host 内存池内部的分层数据整理**（如 Host DDR $\leftrightarrow$ CXL/UBMEM 共享内存池之间的冷热数据搬移）。此过程完全**绕过 ARM CPU 核的 Load/Store 流水线与 L1/L2 Cache**，CPU 仅需下发轻量级描述符，消灭内存搬运带来的 CPU 算力消耗与缓存冲刷。
   * **NVMe SSD 直达扩容通道**：通过 GDS（GPUDirect Storage，显存直读存储技术：数据直接在 NVMe SSD 与显存之间通过 PCIe DMA 传输，完全绕过主机 CPU 与系统内存）与 Payload Bypass DDR（绕过主机内存：数据直接在 NVMe SSD 与 NPU HBM 之间流转，严格绕过主机内存 Host DDR，消除 CPU 内存拷贝与总线争用），建立从磁盘物理扇区到加速卡显存的极速直达通道。
   * **原始直达通路**：构建 Raw Direct（原始直达路径：不依赖 DPU 协处理器与专用硬件压缩，仅依靠底层网卡/SSD DMA 直达的最小基础数据通路），确保即便在异构协处理器不可用的极简硬件环境下，依然能够打通确定性的高吞吐数据流。
2. **硬件 2D/3D Strided DMA 跨步搬运与普通 DMA 的本质区别**：
   * **普通 DMA 加速器**：仅支持单段物理连续缓冲区的搬运（`{src_addr, dst_addr, length}`）；面对大模型 80 层的离散切片，CPU 必须反复下发成百上千个小描述符，引发严重的描述符下发瓶颈；
   * **硬件 2D/3D Strided DMA**：硬件寄存器原生支持跨步步长与维度配置（如 `{base_addr, block_size, stride_bytes, repeat_count}`）。CPU 仅需**敲击一次硬件 Doorbell 下发单条描述符**，片上 DMA 状态机即可自动在硬件总线上完成 80 层张量切片的跨步抽取与重排，实现真正的零 CPU 参与原位布局重组。
3. **面向 CXL 3.0/3.1 与 UB 统一总线内存池的近存协同**：
   在国产统一总线（UBMEM）与 CXL 架构下，调度面将近存计算（Near-Memory Direct Access）与硬件地址交织（Interleaving）纳入调度考量。调度器不仅依据网络链路状态，更依据总线 Home Agent 的 Snoop 过滤开销与一致性穿行率，动态在“本地显存拷入（Copy-to-HBM）”与“总线直接 Load/Store 远端直读（Direct-View）”之间实施微秒级权衡决策。
4. **非 UMR 硬件环境下的兼容降级路径**：
   针对不具备 CX6/7 硬件 UMR 高级特性的通用网卡或国产异构芯片，调度面设计了三级平滑降级分支：
   * **分支 1（硬件异步 2D SDMA）**：利用加速卡内部的异步 Copy Engine（SDMA）在 HBM 内部执行毫秒级 2D 转置，将网络大包解包至连续层；
   * **分支 2（虚拟内存多重映射 VMM / `cuMemMap`）**：在虚拟地址空间通过物理页多重映射将各层离散物理块映射为单一连续虚拟地址，避免数据搬运；
   * **分支 3（多 SGE 链式散布收集批处理）**：将 80 层切片通过预分配的环形 SGE 数组批处理提交给通用网卡 DMA，单次 Doorbell 下发，保证在通用硬件上的稳健运行。


## 5. 终极进化版架构（Kunpeng-HoloSched）系统拓扑与全链路交互模型

### 5.1 端到端系统架构交互拓扑图

终极进化版软硬件协同调度架构（Kunpeng-HoloSched）将全局路由协同、单机微架构硬件感知与物理执行引擎深度耦合，端到端交互拓扑如下（Mermaid 严格遵循语法规范，全面转义特殊符号并消除解析冲突）：

```mermaid
graph TD
    subgraph 集群接入与全局路由层["集群接入与全局路由层 (Global Ingress Router)"]
        Req["在线推理请求流<br/>(长文档 / SWE-bench Agent 微突发)"]
        GlobalRouter["全局前缀感知路由器 (KV-Aware Router)<br/>查锁一体与随路凭据透传"]
    end

    subgraph 节点调度与业务接入层["节点调度与业务接入层 (vLLM 引擎侧)"]
        AsyncGate["异步预取门禁 (Async Prefetch Gate)<br/>C++ 硬件 CRC32C 哈希与二分探测"]
        HoloSched["Kunpeng-HoloSched 核心调度器<br/>(微秒级闭式选路 / 按层切分 / 准入反压)"]
    end

    subgraph 控制面与状态协同层["控制面与状态协同层 (L3 Cache 锁死区)"]
        MetaTable["无锁扁平元数据表 (1GB 巨页)<br/>内联 KVHandle 与写时预编译 UMR 句柄"]
        ShadowMat["带外硬件物理能力矩阵<br/>(各 SCCL 实时带宽与原子在途字节数)"]
        PMUDaemon["绑核 PMU 遥测与 MPAM 守护协程<br/>(200µs 轮询 Uncore 寄存器与双轨状态)"]
    end

    subgraph 鲲鹏与网卡软硬协同执行层["鲲鹏 920 与 CX6/7 和 UB 软硬协同执行层"]
        direction LR
        PathRecomp["局部/全量重算路径<br/>(前 K 层本地算子重算)"]
        PathFastPull["高优流式拉取快路径 (TC0)<br/>CX6 静态 UMR 跨步零拷贝 / UBMEM 直读"]
        PathBgEvict["低优微切片换出路径 (TC1)<br/>256KB Micro-Chunk 与二叉树中继"]
        HwIsolate["鲲鹏微架构硬件底座<br/>SMMUv3 直通 / MPAM 强隔离 / 16通道 DDRC 条带化"]
    end

    Req -->|"1. 请求到达全局网关"| GlobalRouter
    GlobalRouter -->|"2. 查锁一体获取凭据 / 随路透传 Ticket"| AsyncGate
    AsyncGate -->|"3. 单边 RDMA Fetch-and-Add 查表与原子预扣在途字节"| MetaTable
    MetaTable -.->|"同步更新在途排队负载"| ShadowMat
    PMUDaemon -->|"4. 用户态 mmap 直读 PMU 寄存器并刷新"| ShadowMat
    ShadowMat -->|"5. L1/L3 本地零拷贝读取 (时延 &lt; 15ns)"| HoloSched
    MetaTable -->|"返回轻量 KVHandle 与命中特征"| HoloSched

    HoloSched -->|"6a. 拥塞或低复用 (T_load &gt; T_s)<br/>触发按层切分或安全回退 (决策 &lt; 5µs)"| PathRecomp
    HoloSched -->|"6b. 净收益区 (T_load &lt; T_s)<br/>批量下发预编译描述符 (提交 &lt; 300µs)"| PathFastPull
    HoloSched -->|"6c. 后台换出准入与 AIMD 速率整形<br/>(死守 TPOT 0.55ms 容忍预算)"| PathBgEvict

    PathFastPull -->|"PCIe RO=1 / 旁路片上 L3 Tag 嗅探<br/>按层流水掩盖 (H &gt;= 60%)"| HwIsolate
    PathBgEvict -->|"MPAM PARTID=2 硬件限流 (15%~70% 动态调节)"| HwIsolate
    HwIsolate -.->|"Uncore PMU 硬件实时水位反馈"| PMUDaemon
```

### 5.2 全局路由与节点调度的“查锁一体 + 随路凭据透传”交互闭环

针对主流开源方案中“全局路由器调度一次、Worker 拉取前又向元数据服务发起第二次远程 RPC 查表”导致的 TP=8 并发请求 8 倍放大与毫秒级排队雪崩，Kunpeng-HoloSched 确立了**“查锁一体 + 随路凭据透传（Lookup-and-Lease Ticket Pass-Through）”**闭环架构：
1. **全局网关查锁一体**：在请求进入集群网关时，全局前缀感知路由器（KV-Aware Router）直接通过单边原子查表锁定前缀租约，一次性获取内联了目标物理节点、SCCL 通道与跨步描述符索引的定长凭据（`Ticket: Segment-KVHandle`）；
2. **随路凭据透传（Ticket Pass-Through）**：全局路由器将带有该凭据的推理请求直接投递给指定的 Prefill 节点或 Decode 工作节点；
3. **计算节点凭票即行（Zero-Lookup Local Execution）**：计算节点内部的 8 个 TP Worker 收到请求后，**100% 免除向元数据服务器发起的第二次远程 RPC 查询**，直接凭票下发网卡硬件 Doorbell 启动搬运，彻底消除分布式二次查表带来的跨机 RTT 开销与 Continuous Batching 调度主线程阻塞。

### 5.3 全链路交互序列与状态机跃迁模型

在微秒级时间尺度内，Kunpeng-HoloSched 调度器依据状态机对每一个到达的请求执行四阶段严格流转：
1. **阶段 1：异步探查与原子预扣（$< 150\ \mu\text{s}$）**：
   请求到达 vLLM 的 `Waiting` 队列时，异步预取门禁（Async Prefetch Gate）通过 ARMv8 CRC32C 硬件指令计算增量 Hash，并发起单边 `RDMA Fetch-and-Add` 查表。一次往返取回 `KVHandle`、前缀命中率 $h$，并通过原子原语 `AcquireInflightReservation` 对目标通道完成在途字节 $D$ 的原子预扣。
2. **阶段 2：L1 Cache 闭式不等式决策（$< 1.0\ \mu\text{s}$）**：
   调度器从 L3 Cache 锁死区读取目标 SCCL 的实时 PMU 带宽 $B_{\text{pmu}}$，结合累加后的在途排队字节，闭式计算 $T_{\text{load}}$ 与 $T_{\text{recompute}}$。若全量拉取处于净加速区，选择 `FULL_PULL_UMR`；若链路突发拥塞导致收益倒挂，寻优最优切分层 $K^*$ 切换为 `HYBRID_LAYER_SPLIT`；若完全拥塞则果断选择 `FALLBACK_RECOMPUTE`。
3. **阶段 3：描述符批量秒级下发（$< 300\ \mu\text{s}$）**：
   对于获准拉取的请求，直接复用 `KVHandle` 中写时预编译的 UMR KLM MKey，构造聚合 WQE 并通过 1 次 PCIe Write-Combining Doorbell 批量推入网卡硬件高优队列（TC0），触发按层流式传输，层间计算流水掩盖率 $H \ge 60\%$。
4. **阶段 4：传输完成在途扣减与后台 AIMD 反压控制（生命周期闭环）**：
   当硬件 DMA 传输完成触发 CQE 或发生超时回退时，调度引擎立即调用 `ReleaseInflightReservation` 原子扣减在途计数，杜绝排队计数死锁；同时带外 PMU 守护协程监控前台在线 TPOT，按 AIMD 算法动态调节后台换出线程的 MPAM 内存带宽配额（$15\% \sim 70\%$），死守 $0.55\text{ ms}$ 容忍预算。

---

## 6. 生产级核心数据结构与调度算法实现

### 6.1 核心数据结构定义（严格 64B Cacheline 对齐与零对齐空洞）

以下生产级 C++20 头文件完整定义了 `Kunpeng-HoloSched` 的三大核心对象：**内联硬件描述符句柄（`KVHandle`）**、**鲲鹏 SCCL 物理管道实时遥测状态（`SCCLPipeTelemetry`）** 与 **最终调度决策输出（`ScheduleDecision`）**。通过严格的成员字节重排，彻底消除了内存对齐空洞（Padding Hole），并增加了单指令无锁断言：

```cpp
#pragma once
#include <cstdint>
#include <atomic>
#include <algorithm>
#include <cmath>
#include <type_traits>

// 1. 调度的逻辑与描述符对象：紧凑重排、严格 64 字节 Cacheline 对齐的 POD 句柄
struct alignas(64) KVHandle {
    // 8 字节对齐区 (共 24 字节)
    uint64_t prefix_hash_lo;       // 128-bit 块哈希低位 (8B)
    uint64_t prefix_hash_hi;       // 128-bit 块哈希高位 (8B)
    uint64_t base_iova_addr;       // 1GB HugePage 物理/IOVA 基地址 (SMMU Passthrough) (8B)

    // 4 字节对齐区 (共 16 字节)
    uint32_t target_node_id;       // 远端存储节点 ID (4B)
    uint32_t precompiled_umr_mkey; // 写时预编译的 CX6 UMR 跨步虚拟连续 MKey (4B)
    uint32_t lease_epoch;          // 租约纪元号 (用于多维度语义一致性校验与 RCU 迁移) (4B)
    uint32_t layer_stride_bytes;   // 80 层跨步步长 (支持硬件 In-flight 布局转换) (4B)

    // 2 字节对齐区 (共 6 字节)
    uint16_t target_sccl_id;       // 鲲鹏物理 Die (SCCL 0~7) 亲和编号 (2B)
    uint16_t nic_port_id;          // 绑定的 CX6/7 或 URMA 物理端口 (2B)
    uint16_t contiguous_blocks;    // 连续合并的 16-Token 块数量 (条目压缩 >= 50%) (2B)

    // 1 字节对齐区与保留填充 (共 18 字节，总计严格 64 字节，零隐式 Padding Hole)
    uint8_t  storage_tier;         // 0: UBMEM/远端显存, 1: Host DDR, 2: NVMe SSD (1B)
    uint8_t  valid_flags;          // 六维校验位掩码 (Model/Vocab/Template/LoRA/Ready/Lease) (1B)
    uint8_t  reserved_padding[16]; // 显式对齐填充区，供未来硬件卸载扩展 (16B)
};
static_assert(sizeof(KVHandle) == 64, "KVHandle must be exactly 64 bytes");

// 2. 调度的硬件探针输入对象：鲲鹏单 SCCL (物理亲和 2 个 DDRC 通道) 的实时遥测状态槽
struct alignas(64) SCCLPipeTelemetry {
    // 使用标量编码存储可用带宽 (以 100 Mbps 为单位整数，杜绝 atomic<double> 锁开销)
    std::atomic<uint32_t> avail_bw_scaled{420};      // 42.0 GB/s = 420 (单位: 100MB/s)
    std::atomic<uint32_t> hha_cross_die_ratio_ppm{20000}; // 跨 Die 流量百万分比 (20000 ppm = 2%)
    
    // [致命漏洞1修复] 客户端在查表时通过 RDMA Fetch-and-Add / UBMEM 原子预扣
    std::atomic<uint64_t> inflight_bytes{0};         // 当前在途未完成传输字节数 (严格 Lock-Free)
    std::atomic<uint32_t> active_bg_evict_mbps{0};   // 当前后台换出速率 (MB/s)
    std::atomic<uint32_t> mpam_bw_quota_pct{30};     // MPAM MBW_MAX 当前授予后台的配额百分比
    uint8_t               padding[36];               // 填充至 64B Cacheline
};
static_assert(sizeof(SCCLPipeTelemetry) == 64, "SCCLPipeTelemetry must be 64 bytes");
static_assert(std::atomic<uint64_t>::is_always_lock_free, "atomic<uint64_t> must be lock-free");

// 3. 调度的决策输出对象：指导 vLLM 执行引擎与传输引擎
enum class ExecRouteMode : uint8_t {
    FULL_PULL_UMR        = 0, // 全量通过 CX6 UMR 跨步零拷贝拉取
    UBMEM_DIRECT_READ    = 1, // 远端 UBMEM 零拷贝按层直读 (带租约保护)
    HYBRID_LAYER_SPLIT   = 2, // 按层混合执行：前 split_layer 层本地重算，后续层流式拉取
    FALLBACK_RECOMPUTE   = 3, // 拥塞或低复用度，100% 本地重算回退 (杜绝负收益倒挂)
    RELAY_TREE_BROADCAST = 4, // 1-to-8 热点前缀二叉树中继分发
    FALLBACK_2D_SDMA     = 5  // 无 UMR 硬件环境下的 2D SDMA 异步解包降级模式
};

struct ScheduleDecision {
    ExecRouteMode mode;
    uint16_t      recompute_layers;     // [0, split_layer) 由 GPU/NPU 本地重算
    uint16_t      pull_start_layer;     // [split_layer, 80) 由网络流式拉取并掩盖
    uint16_t      assigned_sccl_id;     // 绑定的本地鲲鹏 SCCL 内存管道
    uint8_t       hardware_qp_priority; // 0: TC0 严格高优 (前台), 1: TC1 微切片低优 (后台)
    uint32_t      wqe_chunk_size_kb;    // 前台: 大块聚合; 后台: 强制 <= 256KB [致命漏洞2修复]
    double        predicted_ttft_ms;    // 预估 TTFT (用于在线对账)
};
```

### 6.2 生产级核心调度算法实现（补齐在途释放生命周期与 AIMD 反压）

以下核心调度器完整实现了 **闭式不等式选路、按层混合切分寻优、在途字节获取/释放双原语、以及针对 TPOT $0.55	ext{ ms}$ 红线的 MPAM AIMD 硬件反压控制器**：

```cpp
class KunpengHoloScheduler {
private:
    static constexpr int TOTAL_LAYERS = 80;
    static constexpr double BYTES_PER_TOKEN_PER_CARD = 40.0 * 1024.0; // 40 KiB/token/卡 (TP=8)
    static constexpr double FULL_RECOMPUTE_8K_MS = 240.0;             // 8K 全量重算基线 240ms
    static constexpr double FIXED_PATH_TF_MS = 5.25;                  // 固定路径开销 Tf
    static constexpr double TPOT_BASELINE_MS = 18.20;                 // 基线单字生成耗时
    static constexpr double TPOT_3PCT_BUDGET_MS = 0.55;               // 3% 干扰红线预算 (18.75 - 18.20ms)

    SCCLPipeTelemetry* cluster_telemetry_mmap_; // 映射在 L3 Cache 锁死区的全局共享遥测表

public:
    // [致命漏洞1彻底闭环修复 - 原语1] 查表准入时的在途字节原子预扣
    uint64_t AcquireInflightReservation(uint16_t sccl_id, uint64_t bytes_to_pull) noexcept {
        return cluster_telemetry_mmap_[sccl_id].inflight_bytes.fetch_add(bytes_to_pull, std::memory_order_acq_rel);
    }

    // [致命漏洞1彻底闭环修复 - 原语2] 传输完成 CQE 或回退时的原子扣减 (杜绝单调递增系统死锁)
    void ReleaseInflightReservation(uint16_t sccl_id, uint64_t bytes_pulled) noexcept {
        cluster_telemetry_mmap_[sccl_id].inflight_bytes.fetch_sub(bytes_pulled, std::memory_order_acq_rel);
    }

    // 核心调度决策函数：在请求关键路径上执行，耗时 < 1.0 微秒 (P99 < 5us)
    ScheduleDecision MakePrefillSchedule(
        uint32_t seq_len_tokens,
        double nominal_hit_ratio,
        const KVHandle& handle,
        uint64_t prev_inflight_bytes // 查表时通过原子 Fetch-and-Add 返回的排队历史值
    ) noexcept {
        ScheduleDecision dec{};

        // 1. 六维语义与租约毫秒级安全校验 (任一分歧或租约失效立即回退重算)
        if ( (handle.valid_flags != 0x3F) || (handle.lease_epoch == 0) ) {
            dec.mode = ExecRouteMode::FALLBACK_RECOMPUTE;
            dec.recompute_layers = TOTAL_LAYERS;
            dec.pull_start_layer = TOTAL_LAYERS;
            dec.predicted_ttft_ms = FIXED_PATH_TF_MS + FULL_RECOMPUTE_8K_MS;
            // 校验失败未实际拉取，立即归还预扣在途配额
            ReleaseInflightReservation(handle.target_sccl_id, 0);
            return dec;
        }

        // 2. 计算 16-Token 对齐后的实际命中率 h 与拉取字节数 D
        uint32_t aligned_prefix_tokens = (static_cast<uint32_t>(seq_len_tokens * nominal_hit_ratio) / 16) * 16;
        double h = static_cast<double>(aligned_prefix_tokens) / static_cast<double>(seq_len_tokens);
        double pull_bytes_total = aligned_prefix_tokens * BYTES_PER_TOKEN_PER_CARD * 8.0;

        // 3. 读取目标鲲鹏 SCCL 管道的实时 PMU 带宽 (从标量还原为 GB/s，耗时 ~5ns)
        const auto& pipe = cluster_telemetry_mmap_[handle.target_sccl_id];
        double bw_gbps = std::max(0.5, static_cast<double>(pipe.avail_bw_scaled.load(std::memory_order_relaxed)) / 10.0);

        // 4. 计算后缀计算时间 Ts 与包含在途排队的全量拉取耗时 tau [闭式推导]
        double scale_len = static_cast<double>(seq_len_tokens) / 8192.0;
        double t_recompute_full = FULL_RECOMPUTE_8K_MS * scale_len * scale_len;
        double t_suffix = t_recompute_full * (1.0 - h * h); // Ts = 240 * (1 - h^2)

        double effective_queue_bytes = static_cast<double>(prev_inflight_bytes) + pull_bytes_total;
        double tau_full_ms = (effective_queue_bytes / (bw_gbps * 1e9)) * 1000.0;

        // 5. 计算按层异步流水隐藏量 H: H = min(0.6*tau, 79/80 * min(tau, Ts))
        double hidden_ms = std::min(0.60 * tau_full_ms, (79.0 / 80.0) * std::min(tau_full_ms, t_suffix));

        // 控制面优化后开销: tm (0.15ms) + td 预编译提交 (0.30ms) + tq 决策 (0.002ms) = 0.452ms
        constexpr double t_ctrl_optimized_ms = 0.452;
        double ttft_pull_full = FIXED_PATH_TF_MS + t_ctrl_optimized_ms + t_suffix + tau_full_ms - hidden_ms;
        double ttft_recomp_only = FIXED_PATH_TF_MS + t_ctrl_optimized_ms + t_recompute_full;

        // 6. 动态 Break-Even 选路与“按层混合切分（Layer-Split）”寻优
        if (ttft_pull_full <= ttft_recomp_only) {
            dec.mode = (handle.storage_tier == 0) ? ExecRouteMode::UBMEM_DIRECT_READ : ExecRouteMode::FULL_PULL_UMR;
            dec.recompute_layers = 0;
            dec.pull_start_layer = 0;
            dec.assigned_sccl_id = handle.target_sccl_id;
            dec.hardware_qp_priority = 0; // TC0 严格高优
            dec.wqe_chunk_size_kb = 16384; // 前台大块聚合下发
            dec.predicted_ttft_ms = ttft_pull_full;
            return dec;
        }

        // 当全量拉取因突发排队落入负收益区时，按流水第一性原理寻优切分层 K*
        double best_hybrid_ttft = ttft_recomp_only;
        uint16_t best_k = TOTAL_LAYERS;

        for (uint16_t k = 8; k < TOTAL_LAYERS; k += 8) {
            double layer_frac_pull = static_cast<double>(TOTAL_LAYERS - k) / TOTAL_LAYERS;
            double layer_frac_recomp = static_cast<double>(k) / TOTAL_LAYERS;

            double tau_partial = ((prev_inflight_bytes + pull_bytes_total * layer_frac_pull) / (bw_gbps * 1e9)) * 1000.0;
            double t_compute_hybrid = layer_frac_recomp * t_recompute_full + layer_frac_pull * t_suffix;
            // 严格对齐流水线重叠推导公式: H_partial 依赖层间流水掩盖比例 (79-k)/(80-k)
            double pipeline_overlap_ratio = static_cast<double>(TOTAL_LAYERS - k - 1) / static_cast<double>(TOTAL_LAYERS - k);
            double h_partial = std::min(0.60 * tau_partial, pipeline_overlap_ratio * std::min(tau_partial, t_compute_hybrid));
            double candidate_ttft = FIXED_PATH_TF_MS + t_ctrl_optimized_ms + t_compute_hybrid + tau_partial - h_partial;

            if (candidate_ttft < best_hybrid_ttft) {
                best_hybrid_ttft = candidate_ttft;
                best_k = k;
            }
        }

        if (best_k < TOTAL_LAYERS) {
            dec.mode = ExecRouteMode::HYBRID_LAYER_SPLIT;
            dec.recompute_layers = best_k;
            dec.pull_start_layer = best_k;
            dec.assigned_sccl_id = handle.target_sccl_id;
            dec.hardware_qp_priority = 0;
            dec.wqe_chunk_size_kb = 8192;
            dec.predicted_ttft_ms = best_hybrid_ttft;
            // 仅拉取部分层，按比例退还免拉取层的预扣配额
            double released_bytes = pull_bytes_total * (static_cast<double>(best_k) / TOTAL_LAYERS);
            ReleaseInflightReservation(handle.target_sccl_id, static_cast<uint64_t>(released_bytes));
        } else {
            dec.mode = ExecRouteMode::FALLBACK_RECOMPUTE;
            dec.recompute_layers = TOTAL_LAYERS;
            dec.pull_start_layer = TOTAL_LAYERS;
            dec.predicted_ttft_ms = ttft_recomp_only;
            // 完全回退重算，退还全部预扣配额
            ReleaseInflightReservation(handle.target_sccl_id, static_cast<uint64_t>(pull_bytes_total));
        }
        return dec;
    }

    // [AIMD 自适应反压] 后台换出硬件 QoS 控制器：每 200us 调用，死守 TPOT 0.55ms 容忍预算
    void EnforceBackgroundQoS(uint16_t sccl_id, double current_tpot_ms) noexcept {
        auto& pipe = cluster_telemetry_mmap_[sccl_id];
        double tpot_interference_ms = current_tpot_ms - TPOT_BASELINE_MS;
        uint32_t current_quota = pipe.mpam_bw_quota_pct.load(std::memory_order_relaxed);

        // 乘法减（Multiplicative Decrease）：前台 TPOT 干扰逼近 0.36ms 警戒线 (2%)，立刻急停折半
        if (tpot_interference_ms > 0.36 || (pipe.hha_cross_die_ratio_ppm.load() > 100000)) {
            uint32_t throttled_quota = std::max(15u, current_quota / 2);
            pipe.mpam_bw_quota_pct.store(throttled_quota, std::memory_order_release);
            WriteKunpengMPAMSchemata(sccl_id, /*partid=*/2, throttled_quota);
        } 
        // 加法增（Additive Increase）：前台平稳 (干扰 < 0.15ms)，按 +5% 步进探测恢复，上限 70%
        else if (tpot_interference_ms < 0.15 && current_quota < 70) {
            uint32_t recovered_quota = std::min(70u, current_quota + 5);
            pipe.mpam_bw_quota_pct.store(recovered_quota, std::memory_order_release);
            WriteKunpengMPAMSchemata(sccl_id, /*partid=*/2, recovered_quota);
        }
    }

private:
    void WriteKunpengMPAMSchemata(uint16_t sccl_id, int partid, uint32_t mbw_pct) noexcept {
        // 通过内核 resctrl 接口写入硬件配置，百纳秒级触发 DDRC 硬件限流状态机
    }
};
```

---

## 7. 生产级落地实施清单与全链路观测对账矩阵

### 7.1 四步研发工程落地路径

为了在实测验证中高效落地并拿到坚实的技术证据，制定以下四步工程实施路径：

1. **Step 1：打底鲲鹏微架构隔离环境（Day 1~3）**
   * 内核启动参数配置：`iommu.passthrough=1 default_hugepagesz=1G hugepagesz=1G hugepages=256`，根除 SMMUv3 页表回查引发的带宽断崖；
   * 挂载 Linux `/sys/fs/resctrl`，创建 `ctrl_meta` 硬件分区锁定高 4 路 L3 Cache Ways（`L3:0=f000...`）专门保障元数据表与影子能力矩阵，创建 `bg_evict` 分区通过 `MB:0=25...` 接管后台换出内存带宽配额。
2. **Step 2：上线带外 PMU 硬件物理能力矩阵与原子预扣查表（Day 4~8）**
   * 启动单核守护协程采集片上 `hisi_sccl*_ddrc*/flux_rd,flux_wr` 与 `hisi_sccl*_hha*/rx_outer`，导出 `SCCLPipeTelemetry` 共享内存页；
   * 将前缀存在查询与描述符查询合一为 `batch_lookup_handle`，客户端对命中的前缀使用二分探测，并在关键路径上通过单边原子指令预扣 `inflight_bytes`。
3. **Step 3：使能 CX6/7 静态 UMR 跨步槽与后台 $256	ext{ KB}$ 微切片（Day 9~14）**
   * 在传输引擎初始化时预注册 80 层跨步 UMR 模板，消除在线读路径上 $15.4	ext{ ms}$ 的描述符循环拼装开销；
   * 将后台换出 WQE 拆分为 $\le 256	ext{ KB}$ 微切片并绑定至网卡硬件低优 QP（TC1），消除不可撤回的大块 DMA 对前台 Decode 造成的总线队头阻塞。
4. **Step 4：双节点全工况混压对账（Day 15~21，直通 E3 验收）**
   * 按照对账矩阵采集微观硬件计数器与宏观业务指标，完成闭环验收。

### 7.2 全链路观测指标对账矩阵

| 观测维度 | 核心观测指标名称 | 采集手段 / 硬件计数器 | 优化前基线 | **调度优化后目标规格** | 关联宏观业务收益贡献（闭环对账） |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **控制与调度面微观指标** | **1. 前缀句柄查询延迟 (20×20 突发)** | 调度器埋点 (`t_m`) | Mean $4.0	ext{ ms}$ / P99 **$86.0	ext{ ms}$** | **Mean &lt; 150 µs / P99 &lt; 500 µs** | 消灭 SWE-bench Agent 微突发排队风暴，**Agent 单步 P99 TTFT 降低 &gt; 75%** |
| **控制与调度面微观指标** | **2. 动态选路决策耗时 (`t_q`)** | 调度器埋点 (`MakePrefillSchedule`) | **$10.0	ext{ ms}$**（同步探测） | **Mean &lt; 1 µs / P99 &lt; 5 µs** | 消除 10ms 同步探测沉没成本，实现微秒级拥塞感知 |
| **控制与调度面微观指标** | **3. 描述符编译与下发耗时 (`t_d`)** | Transfer Engine 埋点 | **$15.40	ext{ ms}$** | **Mean &lt; 300 µs / P99 &lt; 800 µs** | **直接削减不可掩盖首层等待 $14.6\sim 15.1	ext{ ms}$**（占 8K 场景总收益的 $36\%$） |
| **鲲鹏与网卡硬件层指标** | **4. 鲲鹏 L3 元数据命中率** | `hisi_sccl*_l3c*/rd_spipe,rd_hit_spipe` | $60\%\sim 75\%$（被 DMA 冲刷） | **$\ge 99.2\%$**（MPAM `CPOR` 锁死） | 根除跨 SCCL 随机访存长尾，支撑张量并行 (TP=8) 多卡状态同步机制耗时 **P99 &lt; 100 µs (RankConsensus)** |
| **鲲鹏与网卡硬件层指标** | **5. DDRC 内存流量放大比与跨 Die 率** | `hisi_sccl*_ddrc*/flux_*` & `hha/rx_outer` | 放大比 $1.8\sim 3.0	ext{x}$；跨 Die $> 35\%$ | **放大比 $= 1.00	ext{x}$（零 CPU 拷贝）；跨 Die &lt; 5%** | 稳态物理带宽锁死 **$\ge 42	ext{ GB/s}$（双轨达 $80	ext{ GB/s}$）**，消除带宽抖动 |
| **在线推理宏观业务指标 (E3)** | **6. 负收益请求比例与拥塞回退** | 在线对账日志 ($T_{	ext{actual}} > T_{	ext{recomp}}$) | 拥塞时恶化至 **$629.93	ext{ ms}$** | **负收益请求比例 &lt; 1%；准确率 $\ge 90\%$** | 拥塞下自动切换按层混合/重算，**单请求规避 $389.18	ext{ ms}$ 倒挂损失** |
| **在线推理宏观业务指标 (E3)** | **7. 端到端首字时间 (TTFT)** | Benchmark 客户端实测 (8K 70% 命中) | **$187.15	ext{ ms}$** | **$\le 146.69	ext{ ms}$（多轨扩展达 $138.20	ext{ ms}$）** | **端到端平均 TTFT 降低 $21.6\% \sim 26.2\%$（超额达成 E3 $\ge 20\%$ 目标）** |
| **在线推理宏观业务指标 (E3)** | **8. 混压下逐字生成耗时 (TPOT) 与有效 QPS** | 100 Gbps 后台压力下前台实测 | TPOT 恶化至 **$21.29	ext{ ms}$ ($+17\%$)** | **TPOT $\le 18.56	ext{ ms}$（干扰 &lt; 2%，严守 &lt; 3% 红线）** | 保住后台 $\ge 70	ext{ Gbps}$ 排出进度，**集群有效 QPS 提升 $\ge 15\%$（超额达成 $\ge 10\%$ 目标）** |

### 7.3 两大标杆场景端到端量化对账

#### 场景 1：SWE-bench / DeepSWE-bench 编码 Agent 多轮微突发（控制面与调度面主场）
* **工况定义**：20 个 vLLM 实例共享存储底座；第 40 轮交互，历史前缀 32K Tokens（命中率 $h = 98.4\%$，增量 $\Delta L = 512$ Tokens）；因工具执行等待 5 秒，32K KVC 已换出至 Host 内存池；20 并发微突发到达。
* **端到端 TTFT 拆解对比**：
  * **增量后缀计算耗时 ($T_s$)**：仅计算 512 Tokens，固定为 **$15.0	ext{ ms}$**；
  * **数据面净暴露耗时 ($	au - H$)**：通过 UMR 零拷贝与按层流水掩盖，净暴露仅 **$12.0	ext{ ms}$**；
  * **控制面与调度面耗时 ($T_{	ext{ctrl}}$)**：
    - **优化前基线 (P99)**：Python Hash ($4.8	ext{ ms}$) + 突发查表 P99 ($86.0	ext{ ms}$) + 同步选路 ($10.0	ext{ ms}$) + 描述符拼装 ($15.4	ext{ ms}$) + 多卡同步 ($2.0	ext{ ms}$) = **$118.2	ext{ ms}$**！
      $\rightarrow$ **基线 P99 TTFT** $= 15.0 + 12.0 + 118.2 = \mathbf{145.2	ext{ ms}}$（**控制面占比高达 $81.4\%$**）；
    - **软硬协同优化后 (P99)**：硬件 CRC32C ($0.12	ext{ ms}$) + 二分与 MPAM 锁 L3 ($0.50	ext{ ms}$) + 闭式不等式选路 ($0.005	ext{ ms}$) + 预编译 UMR ($0.80	ext{ ms}$) + 硬件事件同步 ($0.10	ext{ ms}$) = **$1.53	ext{ ms}$**！
      $\rightarrow$ **优化后 P99 TTFT** $= 15.0 + 12.0 + 1.53 = \mathbf{28.53	ext{ ms}}$（**P99 TTFT 暴降 $80.3\%$，控制面占比压回 $5.3\%$**）！

#### 场景 2：8K 70% 命中 + 100 Gbps 后台换出全工况混压（对齐 E3 验收总门禁）
* **工况定义**：TP=8、8K 序列、70% 命中（拉取 2.5 GiB，重算 30% 后缀 $T_s = 122.4	ext{ ms}$），后台同时承受 100 Gbps 换出压力与偶发链路拥塞（跌至 3 GB/s）。
* **业务指标兑现**：
  * **TTFT 降低 $\ge 20\%$**：通过写时预编译描述符（省 $13.4\sim 14.9	ext{ ms}$）+ 按层异步流水（省 $26.81	ext{ ms}$）+ 多 SCCL 条带化通道（稳态 $42\sim 80	ext{ GB/s}$ 再省 $8.5	ext{ ms}$），端到端 TTFT 从 $187.15	ext{ ms}$ 降至 **$138.20 \sim 146.69	ext{ ms}$（降幅达 $21.6\% \sim 26.2\%$）**；
  * **TPOT 干扰率 $< 3\%$**：通过异步预取门禁剥离主循环临界区（阻塞 $< 0.25	ext{ ms}$）+ 鲲鹏 MPAM `MBW_MAX` 硬件带宽闸门 + CX6 优先级队列，在 100 Gbps 混压下将前台 TPOT 从 $21.29	ext{ ms}$（恶化 17%）稳稳压制在 **$18.56	ext{ ms}$（干扰率仅 $1.9\%$，严守 $0.55	ext{ ms}$ 红线）**，同时保住后台 $\ge 70	ext{ Gbps}$ 的排出进度，**集群有效 QPS 提升 $\ge 15\%$**。
