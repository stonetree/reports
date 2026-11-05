# 11月HOST侧KVC调度面优化与底层硬件遥测详细设计及全链路实验验证方案

> **文档版本**：V2.0（已完成AI系统调度、微架构与数据压缩、高性能网络与QoS三方专家严苛评审闭环修订）  
> **核心责任人**：WXH（1.0 FTE，调度面总控与全链路消融 Owner）、NXY（0.5 FTE，全链路遥测 `CapabilityMatrix` 与硬件 QoS Owner）  
> **正交协同接口**：LJQ（0.5 FTE，存储介质数据面执行）、LDY（1.0 FTE，按层计算通信掩盖与 টেস্ট床备援）、CY（1.0 FTE，URMA/RDMA 单通道平顶与多通道传输）、YZN（0.5 FTE，多通道内存注册底座）、WZX（1.0 FTE，上层双池 Slab 块分配与描述符编译）、YY（1.0 FTE，鲲鹏 SIMD 位平面流式压缩）  
> **架构仲裁与总体对齐**：LL（系统架构师）

---

## 术语首次出现释义与数学符号规范表

1. **KVCache（Key-Value Cache，大模型注意力键值缓存）**：自回归生成过程中缓存的历史 Token Key 与 Value 张量，用于避免重复计算。
2. **CapabilityMatrix（硬件能力矩阵）**：运行时以微秒级周期自动探测内存控制器、片上 IO 总线、PCIe 根复合体与网卡端口物理状态的 $64\text{B}$ 紧凑结构体。
3. **HoloTestbed-4X（全栈软硬件全息实验测试床）**：采用单机 $4$ 卡双命名空间光纤物理环回搭建的低变异性（$\text{CV} < 4.8\%$）高保真基准评测平台。
4. **TTFT（Time To First Token，首字生成延迟）**：从收到推理请求至吐出首个输出 Token 的端到端耗时。
5. **TPOT（Time Per Output Token，单字生成延迟）**：Decode 阶段平均生成每个输出 Token 的耗时（本项目在线基线为 $18.20\text{ms}$，$3\%$ 干扰容忍红线预算为 $+0.55\text{ms}$，即 $\le 18.75\text{ms}$）。
6. **Goodput（有效达标吞吐量 / 有效 QPS）**：在严格满足服务等级目标（SLO，如 $\text{P99 TTFT}$ 与 $\text{TPOT}$ 门限）前提下，系统每秒完成的请求数或 Token 数。
7. **MPAM（Memory System Resource Partitioning and Monitoring）**：ARMv8.4+ / 鲲鹏 920 提供的硬件级 L3 缓存分区（CPOR）与内存控制器带宽配额（`MBW_MAX`）隔离技术。
8. **SMMUv3 / HHA / PCIe RC**：鲲鹏 920 IO Die 上的系统内存管理单元（负责 IOMMU 页表转换）、跨 Die 缓存一致性主代理（Home Agent）与 PCIe 根复合体（Root Complex）。

---

## 第一部分：架构审阅与理论升华——融合 Slide 2 协同范式与 HOST 调度业务价值推理路径

### 1.1 对汇报稿 Slide 2「GPU/NPU 与 HOST/CPU 主从协同与时延契约范式」的深度审阅与落地映射

在《统一异构 KVCache 存储池关键技术汇报（V14 定稿融合版）》的 Slide 2 中，确立了极具前瞻性的 **“主从协同与时延契约范式（Master-Slave Synergy & Deadline Contract Paradigm）”**。经系统架构审阅，该范式精准破解了传统异构推理系统中“算力卡等待外围慢速 I/O”与“CPU 盲目搬运挤占总线”的根本矛盾。我们将 Slide 2 的核心思想全面转化为 11 月 HOST 侧调度面的工程设计准则：

#### （1）主权边界与“数据面零拷贝、控制/调度面强统筹”铁律
* **GPU/NPU（计算与消费中心）**：拥有 KVC 的完整所有权与消费时序主导权。只负责密集张量计算（Prefill/Decode）、本地 HBM 显存主权分配、按层双流流式消费以及超时安全回退（本地重算兜底）。
* **HOST/CPU（外围基础设施大管家）**：**数据面正文拷贝字节严格为 $0$**（由 DMA/RDMA/CXL/GDS 硬件引擎直达显存）。HOST 侧的核心存在价值在于 **“在满足 GPU 指定时延契约（Deadline）的前提下，统筹外围多级存储容量与并发传输带宽”**。

#### （2）时延契约（Deadline Contract）的数学化交互闭环
Slide 2 提出的双向控制信令在 11 月调度面中被实例化为微秒级状态机契约：
* **下行契约（GPU/Scheduler $\to$ HOST 集群控制器）**：下发请求不再是盲目的 `Fetch(Key)`，而是携带时延预算的契约三元组：
  $$\text{Contract}_{\text{down}} = \left(\text{Intent: Fetch/Offload}, \; T_{\text{deadline}}, \; \text{HBM\_Window\_Handle}\right)$$
  其中 $T_{\text{deadline}} = T_{\text{recompute}}(L_{\text{match}}) - \Delta T_{\text{safety}}$ 为本地重算盈亏平衡时延门限（或在按层流水下为第 $L$ 层的计算掩盖窗口 $T_{\text{layer\_compute}}$）。
* **上行履约反馈（HOST 集群控制器 $\to$ GPU/Execution Engine）**：HOST 调度面（WXH）读取底层硬件能力矩阵（NXY），在 $< 2\mu\text{s}$ 内完成履约判定：
  1. **全额履约（Full Fulfillment）**：若实时有效带宽充裕，满足 $T_{\text{transfer}} \le T_{\text{deadline}}$，立即原子预扣通道带宽，返回 `[就绪通知 + 最优多通道源地址句柄 KVHandle]`；
  2. **部分履约 / 混合掩盖（Partial Fulfillment via $K^*$ Split）**：若网络处于中度拥塞（如 $8 \sim 20\text{GB/s}$），全量拉取将违约，但部分拉取可被掩盖。HOST 控制器解算最优切分层 $K^*$，返回 `[前 K* 层本地重算 + 后 (80-K*) 层异步拉取句柄]`（对接 LDY 引擎）；
  3. **契约熔断 / 透明回退（Contract Breach $\to$ Transparent Fallback）**：若链路严重拥塞（如 $3\text{GB/s}$）或目标 KVC 位于慢速 SSD 且升迁排队超期，HOST 立即返回 `[契约拒绝 / 触发 GPU 本地透明重算]`，彻底斩断长尾等待。

#### （3）Slide 2 四大核心协同职责与三大价值杠杆在 11 月任务中的精确落位

| Slide 2 协同职责定义 | Slide 2 对应的系统价值杠杆 | 11 月调度面与协同模块的落地转化机制 | 主责与协同人 |
| :--- | :--- | :--- | :--- |
| **1. 冷热存储位置调度**<br>*(Tiered Placement)* | **【场景一 · 容量杠杆】**<br>突破显存墙，推迟容量相变拐点 | 捕捉 SWE-bench Agent 工具休眠窗（Tool-Wait TTL）与 $O(L^2)$ 重算代价，驱动 `HBM <-> DDR <-> SSD` 异步升降级与超前预取；通过快慢队列物理隔离消除 SSD 队头阻塞。 | **WXH** (决策)<br>**LJQ** (介质执行) |
| **2. 多维传输带宽统筹**<br>*(Bandwidth & Layout)* | **【场景二 · 带宽杠杆】**<br>消除木桶短板，推迟带宽饱和拐点 | 基于 `MountedSegment` 内嵌的 $64\text{B}$ 矩阵感知 4-SCCL 内存通道与多网卡负载，结合上层双池 Slab 连续分配与 SIMD 位平面压缩，动态路由至最空闲物理通道。 | **WXH** (选路)<br>**YZN/CY/WZX/YY** |
| **3. 高并发访问排队治理**<br>*(Concurrency & Queueing)* | **【场景三 · 排队与QoS杠杆】**<br>削平长尾延迟，推迟排队劣化拐点 | 在控制面 Step 1 引入带 Epoch 标记的 `inflight_bytes` 原子预扣，根除多实例并发命中同一前缀时的网卡 Incast 羊群踩踏；剥离调度器同步 I/O 临界区（$< 100\mu\text{s}$）。 | **WXH** (防踩踏)<br>**NXY** (矩阵同步) |
| **4. 前后台 QoS 隔离与兜底**<br>*(QoS & Safety Net)* | **【场景三 · 排队与QoS杠杆】**<br>死守 $0.55\text{ms}$ TPOT 预算与 $0\%$ 退化 | 采集鲲鹏 DDRC/IO Die/PCIe RC 硬件 PMU（$< 500\mu\text{s}$），实施 $200\mu\text{s}$ WQE 微切片与 $20\text{ms}$ MPAM `MBW_MAX` 硬件内存限流，并在劣质链路下 $100\%$ 触发 Break-Even 重算兜底。 | **NXY** (遥测+QoS)<br>**WXH** (熔断回退) |

---

### 1.2 使命破局：从“局部微秒级优化”到“端到端业务指标跃升”的四条业务价值推理路径

在工程实践中，单纯汇报“探针采集从 $3\text{ms}$ 降到 $2\mu\text{s}$”或“选路决策耗时 $< 2\mu\text{s}$”很容易被挑战：**“在几百毫秒的 TTFT 面前，省下几微秒有什么业务价值？”**

事实上，HOST 侧 KVC 调度的真正威力**绝非直接从分母中减去决策本身的微秒耗时**，而是通过微秒级的高频精准干预，**改变系统的排队论负载率 $\rho$、消除零号层前的串行反掩盖停顿、阻断跨请求队头阻塞（HoL Blocking），并将系统的容量/带宽/排队相变拐点大幅右移**。以下构建四条严密的业务价值推理路径：

---

#### 【价值推理路径一：高并发微突发下的防羊群踩踏与多通道均衡】
* **1. 真实物理/业务瓶颈问题**：
  在 SWE-bench 多 Agent 协同或共享池高并发场景（如 $20$ 个 vLLM 实例 $\times 20$ 并发 $= 400$ 并发请求微突发），多个请求在极短时间窗口（$< 10\text{ms}$）内同时命中远端节点 A 上的同一份热前缀 KVC，或同时打向同一个 SCCL 内存通道。传统的上层调度器只看“KVC 亲和性”或“静态队列长度”，会将数十个并发拉取请求瞬间全部路由至同一网卡端口/内存通道，触发严重的 **多对一 Incast 队列溢出与跨 Die HHA 总线冲突**，导致该链路实际有效带宽从 $40\text{GB/s}$ 断崖式暴跌至 $< 5\text{GB/s}$。
* **2. HOST 侧 KVC 调度解法**：
  WXH 在控制面 Step 1（查询命中返回 `MountedSegment` 瞬间）引入 **基于 $64\text{B}$ 实时能力矩阵的 `inflight_bytes` 原子预扣与多通道分流机制**。第 $1$ 个请求预扣后，后续并发请求在纳秒级内立即感知到该通道有效剩余带宽 $BW_{\text{eff}}$ 已折减，自动分流至次优副本通道，或在超出链路并发容量时直接切换为 $K^*$ 混合重算。
* **3. 局部技术指标改善**：
  单节点网卡出端口瞬时排队深度降低 $75\%$ 以上；跨 SCCL 流量放大比锁定为 $1.00\times$；微突发选路与预扣决策耗时 $\text{P99} < 2\mu\text{s}$。
* **4. 排队论数学传导机制**：
  根据成批到达排队论 $M^{[X]}/G/1$ 模型，当批次大小 $X=20$ 突发到达时，若无预扣分流，服务台有效服务率 $\mu$ 因 Incast 拥塞而下降，系统瞬时利用率 $\rho = \frac{\lambda \cdot \mathbb{E}[X]}{\mu} \to 1$，排队等待时间遵循：
  $$W_q = \frac{\lambda \cdot \mathbb{E}[X^2]}{2\mu(\mu - \lambda\mathbb{E}[X])} + \frac{\rho \cdot \text{Var}(S)}{2(1-\rho)\mathbb{E}[S]}$$
  通过原子预扣消除拥塞崩溃并消减服务时间方差 $\text{Var}(S)$，将有效服务容量拐点 $\mu$ 从 $1000\text{ req/s}$ 推高至 $1429\text{ req/s}$。
* **5. 真实在线业务指标跃升**：
  * **微突发场景（$20 \times 20$ 并发）$\text{P99 TTFT}$ 降低 $> 60\%$**（消除 Incast 尾部雪崩）；
  * **常规高负载场景（$\rho \in [0.75, 0.90]$）下，同一 SLO 门限内的系统有效吞吐（Goodput / QPS）提升 $12\% \sim 18\%$**。

---

#### 【价值推理路径二：拥塞/抖动链路下的 Deadline 契约熔断与 $K^*$ 混合切分】
* **1. 真实物理/业务瓶颈问题**：
  生产集群网络不可避免存在背景流量冲击或局部负载倾斜。如 `DolphinRouting` (arXiv:2610.40292) 实测所示，当可用带宽跌破盈亏平衡门限（如 $70\%$ 命中下门限为 $7.94\text{GB/s}$，恶劣时跌至 $3\text{GB/s}$），传统策略依然“无条件死等远端拉取”，导致单次 $8\text{K}/32\text{K}$ 请求拉取耗时高达 $629.93\text{ms}$，而本地直接重算仅需 $240.75\text{ms}$，产生 **$389\text{ms}$ 的严重负收益倒挂**，并且该慢请求长期占住 HBM 预分配块，拖垮整个 Batch 的并发度。此外，若采用传统的同步网络探针（耗时 $10\text{ms}$），探测本身就在第 $0$ 号层前引入了无法被流水线掩盖的純串行停顿。
* **2. HOST 侧 KVC 调度解法**：
  NXY 通过后台守护进程每 $500\mu\text{s}$ 异步刷新共享内存中的 `CapabilityMatrix`；WXH 的 `CostEvaluator` 在前台仅做一次 L1 Cache 原子读（$< 50\text{ns}$），在 $< 2\mu\text{s}$ 内代入感知压缩率与碎片度的闭式成本方程：
  * 严重拥塞（$< 7.94\text{GB/s}$）：$100\%$ 触发契约熔断，立即回退本地重算；
  * 中轻度拥塞（$8 \sim 20\text{GB/s}$）：动态解算最优重算层数 $K^*$，指挥 LDY 引擎用前 $K^*$ 层的本地零等待计算完全掩盖后 $(80-K^*)$ 层的慢速网络拉取。
* **3. 局部技术指标改善**：
  选路决策耗时从同步探测的 $10\text{ms}$ 压降至 $\text{Mean} < 1\mu\text{s} / \text{P99} < 2\mu\text{s}$（提速 $5000$ 倍）；劣质链路识别准确率 $\ge 95\%$；负收益请求比例从 $> 30\%$ 降至 $0\%$。
* **4. Amdahl 零号层反掩盖杠杆传导**：
  在按层流水掩盖模型中（见汇报稿 Slide 8），数据面加载耗时 $\tau$ 有 $H = 0.6\tau$ 被计算隐藏，数据面每节省 $1\text{ms}$ 仅兑现 $0.4\text{ms}$ 端到端收益；而调度选路耗时 $t_q$ 位于 **第 $0$ 号层计算启动前的纯串行区（隐藏率 $H=0$）**，具备 **$2.5\times$ 反掩盖杠杆**（即控制/调度面省 $1\text{ms}$ 等效于数据面省 $2.5\text{ms}$）。消除 $10\text{ms}$ 同步探测直接净省 $10\text{ms}$ TTFT，同时截断 $389\text{ms}$ 长尾。
* **5. 真实在线业务指标跃升**：
  * **$3\text{GB/s}$ 恶劣拥塞下，端到端 $\text{TTFT}$ 降低 $61.8\%$（$629.93\text{ms} \to 240.75\text{ms}$，性能退化率严格为 $0\%$）**；
  * **$8 \sim 20\text{GB/s}$ 波动网络下，通过 $K^*$ 混合掩盖使平均 $\text{TTFT}$ 降低 $15\% \sim 25\%$，SLO 达标率从 $42\%$ 恢复至 $98\%$（有效 Goodput 提升 $> 15\%$）**。

---

#### 【价值推理路径三：Agent 长程会话下的跨介质冷热升降级与快慢队列隔离】
* **1. 真实物理/业务瓶颈问题**：
  在 $72\text{B}$ 模型或 SWE-bench 多轮编码 Agent 场景下，活跃工作集达到 HBM 显存容量的 $1.5 \sim 2.0$ 倍。当 Agent 调用外部工具（编译代码、运行测试，耗时 $10\text{s} \sim 120\text{s}$）时，朴素 LRU 策略常在工具返回前わずか数秒将其長达 $32\text{K} \sim 64\text{K}$ 的 KVC 从 DDR 驱逐或丢弃，导致下一轮请求命中率从 $84\%$ 崩盘至 $36\%$（被迫全量重算，TTFT 暴增数秒）。反之，若保留在 SSD 中并在请求到达时同步直读 SSD（`O_DIRECT`），会阻塞推理主循环，使并发短请求的 TTFT 恶化 $3.4$ 倍（$27\text{ms} \to 93\text{ms}$，见 arXiv:2610.21249）。
* **2. HOST 侧 KVC 调度解法**：
  WXH 建立纵向跨介质生命周期控制器：
  1. **工具窗口感知降级（Tool-Wait Demotion）** 与 **$O(L^2)$ 代价加权留存**：优先将计算恢复代价呈平方级高昂的长序列保留在高速层；
  2. **超前异步升迁（Proactive Promotion）**：根据 Agent 工具执行历史耗时分布，在预计返回前 $2\text{s}$ 向 LJQ 存储引擎下发 `Promote(SSD -> DDR)` 指令；
  3. **快慢队列物理隔离（`FC-MAS`）**：若请求到达时 KVC 仍在 SSD 中，将其挂入异步预热慢队列（`WARMING_SLOW_QUEUE`），绝不阻塞 `READY_FAST_QUEUE` 中的 DRAM/HBM 命中请求与在线 Decode 批次。
* **3. 局部技术指标改善**：
  跨介质缓存全局有效命中率在 $1.5 \sim 2\times$ 超显存压力下从 $36\%$ 提升至 $> 85\%$；调度器主循环临界区阻塞时间严格 $< 100\mu\text{s}$。
* **4. 业务指标传导机制**：
  缓存命中率提升 $\Delta h = +49\%$ 直接消除了海量 $O(L^2)$ 冗余 Prefill 浮点运算，释放出的 GPU 算力周期全部转化为新请求的并发处理能力。
* **5. 真实在线业务指标跃升**：
  * **多轮 Agent 场景平均 $\text{TTFT}$ 降低 $35\% \sim 41\%$**；
  * **在 $150\% \sim 200\%$ 容量超载工况下，系统有效完成率维持 $> 95\%$，整体有效吞吐（Goodput）提升 $> 25\%$**。

---

#### 【价值推理路径四：前后台 100Gbps 混压下的微架构争抢感知与双时间尺度硬件 QoS】
* **1. 真实物理/业务瓶颈问题**：
  为了支撑高频 KVC 换入换出，HOST 侧后台搬运流量高达 $100\text{Gbps}$（$12.5\text{GB/s}$）。如 `PulseInfer` (arXiv:2609.34555) 及项目前期实测所示，大块 DMA/RDMA 传输会瞬间打满鲲鹏 IO Die 上的 **PCIe Root Complex（RC）信用队列** 与 **DDRC 内存控制器带宽**，造成严重的微架构级队头阻塞（HoL Blocking）。即使前台在线 Decode 请求完全不涉及网络 I/O，其访问本地内存/总线的延迟也会被后台大包拖慢，导致 **前台 Decode TPOT 从基线 $18.20\text{ms}$ 恶化至 $21.29\text{ms}$（劣化 $+16.98\%$，远超 $3\%$ 即 $18.75\text{ms}$ 的 SLA 红线）**。一旦关闭或大幅静态限死后台流量（如静态限流在 $17.65\text{Gbps}$ 以下），KVC 换出队列就会积压溢出，引发连锁重算。
* **2. HOST 侧 KVC 调度与 QoS 解法**：
  NXY 将鲲鹏 IO Die（`smmuv3_pmcg` / `hisi_hha`）与 PCIe RC（`hisi_pcie`）纳入 `< 500µs` `CapabilityMatrix` 探针，并联动 **双时间尺度硬件 QoS 控制器**：
  * **快路径（$200\mu\text{s}$ 尺度）**：当检测到前台 Decode 活跃时，将后台大块 WQE 动态微切片为 $256\text{KB}$，允许前台高优 I/O 在每 $6\mu\text{s}$ 的切片间隙硬件插队，根除 PCIe RC 队头阻塞；
  * **慢路径（$20\text{ms}$ 尺度）**：基于 EWMA 水位动态写鲲鹏 MPAM `MBW_MAX` 硬件寄存器（在 $10\% \sim 85\%$ 间实时调节后台内存带宽上限）与网卡 QP 硬件限速器。
* **3. 局部技术指标改善**：
  PCIe RC `outbound_stalled` 停顿率下降 $> 80\%$；后台可用安全带宽从无控时的 $17.65\text{Gbps}$（越过 $3\%$ 红线点）大幅推高至 $\ge 75\text{Gbps}$。
* **4. 业务指标传导机制**：
  在死守前台 $\text{TPOT} \le 18.56\text{ms}$ 的同时，后台安全换出带宽扩大了 $\frac{75\text{Gbps}}{17.65\text{Gbps}} = 4.25\times$，使更多请求的 KVC 得以保存并复用，直接提升系统在严苛 TPOT SLA 约束下的最大可承载并发请求数。
* **5. 真实在线业务指标跃升**：
  * **$100\text{Gbps}$ 满载混压下，前台在线 Decode $\text{TPOT}$ 干扰率从 $+16.98\%$ 压降至 $< 2.0\%$（控制在 $\le 18.56\text{ms}$，净增 $\le 0.36\text{ms}$，严守 $< 3\%$ 红线）**；
  * **在严守 TPOT 与 TTFT 双重 SLA 前提下，系统整体有效达标吞吐（Goodput / 有效 QPS）提升 $\ge 15\%$**。

---

### 1.3 四条业务价值推理路径全景汇总对账表

| 价值路径编号与场景 | 物理/微架构瓶颈根因 | HOST 侧调度/遥测核心机制 | 局部技术指标跨越 | 端到端业务指标确定性收益 |
| :--- | :--- | :--- | :--- | :--- |
| **路径一：高并发微突发防踩踏**<br>*(SWE-bench 400并发)* | 多实例并发命中同源引发网卡 Incast 与跨 Die 争用 | Step 1 原子在途预扣（含 Epoch 防重扣）+ 4-SCCL 分流 | 选路 $< 2\mu\text{s}$，端口排队降 $75\%$，跨 Die 放大比 $= 1.00\times$ | 微突发 **$\text{P99 TTFT}$ 降 $> 60\%$**<br>稳态 **有效吞吐 Goodput $+12\% \sim 18\%$** |
| **路径二：拥塞链路契约兜底**<br>*(3~20GB/s 抖动网络)* | 盲目拉取致 $389\text{ms}$ 负收益倒挂；$10\text{ms}$ 同步探测串行阻塞 | `<500µs` 异步矩阵 + `<2µs` 闭式评估 + $K^*$ 按层混合掩盖 | 决策提速 $5000\times$ ($10\text{ms} \to <2\mu\text{s}$)，负收益请求占比 $0\%$ | $3\text{GB/s}$ 下 **$\text{TTFT}$ 降 $61.8\%$（$0\%$ 退化）**<br>波动网 **有效 Goodput $+15\%+$** |
| **路径三：长程会话跨层升降级**<br>*(72B 超显存 1.5~2x)* | Tool-Wait 期间盲目 LRU 驱逐；同步直读 SSD 阻塞主循环 | Tool-Wait TTL + $O(L^2)$ 代价置换 + 超前升迁 + 快慢队列隔离 | 超显存下全局命中率 $36\% \to >85\%$，主循环阻塞 $< 100\mu\text{s}$ | 多轮会话 **平均 $\text{TTFT}$ 降 $35\%+$**<br>超容量 **有效 Goodput $+25\%+$** |
| **路径四：前后台 100G 混压隔离**<br>*(Decode + 100G 换出)* | 后台大包占满 PCIe RC 与 DDRC，致 Decode TPOT 劣化 $16.98\%$ | IO Die/RC 探针 + $200\mu\text{s}$ 微切片 + $20\text{ms}$ MPAM `MBW_MAX` 限流 | 安全后台排出速率 $17.65 \to \ge 75\text{Gbps}$ ($4.25\times$) | **$\text{TPOT}$ 干扰压至 $< 2.0\%$ ($\le 18.56\text{ms}$)**<br>混压下 **有效 Goodput $+15\%+$** |

---

## 第二部分：NXY (0.5 FTE) `M2-T04` 详细优化方案设计——全链路 `CapabilityMatrix` ($<500\mu\text{s}$) 与双时间尺度硬件 QoS 闭环

### 2.1 鲲鹏 920 片上微架构三段式木桶带宽数学模型

在双路鲲鹏 920（每路包含 $2$ 个 Super CPU Cluster `SCCL` + $1$ 个 `IO Die`）体系结构下，KVC 数据流必须穿越 **“DDRC 内存控制器 $\leftrightarrow$ 片上 Hydra/HHA 一致性总线与 SMMUv3 $\leftrightarrow$ PCIe Root Complex $\leftrightarrow$ RoCE/URMA 网卡物理端口”**。任何一环饱和都会成为全链路的木桶短板。

#### （1）片上三大硬件计数器采集源定义
1. **DDRC 内存通道层（`hisi_sccl_ddrc`）**：采集每个 SCCL 下属 4 个 DDRC 通道的读写 Flux 计数器（`flux_wr` / `flux_rd`），计算第 $s$ 个 SCCL 的实时剩余内存带宽：
   $$BW_{\text{DDRC\_rem}}^{(s)}(t) = BW_{\text{DDRC\_peak}}^{(s)} - \frac{\Delta (\text{flux\_rd}^{(s)} + \text{flux\_wr}^{(s)}) \cdot 64\text{B}}{\Delta t}$$
2. **PCIe Root Complex 层（`hisi_pcie`）**：采集目标网卡挂载的 PCIe RC 端口的 TLP 出入向字节数（`rx_mwr_tlp` / `tx_mrd_tlp`）及出向信用停顿周期比 $r_{\text{rc\_stall}} = \frac{\Delta \text{tx\_credit\_stall\_cycles}}{\Delta \text{total\_cycles}}$，计算剩余 RC 吞吐：
   $$BW_{\text{PCIe\_RC\_rem}}^{(r)}(t) = \left(1 - r_{\text{rc\_stall}}(t)\right) \cdot \left(BW_{\text{PCIe\_peak}}^{(r)} - BW_{\text{PCIe\_used}}^{(r)}(t)\right)$$
3. **IO Die 争抢惩罚因子（`smmuv3_pmcg` & `hisi_hha`）**：采集 SMMUv3 的地址转换缺失率 $r_{\text{tlb\_miss}} = \frac{\Delta \text{tlb\_miss}}{\Delta \text{tlb\_access}}$ 以及 HHA 跨 Die 访问比例 $r_{\text{cross\_die}} = \frac{\Delta \text{rx\_outer}}{\Delta \text{rx\_ops\_num}}$，构造动态 IO Die 拥塞惩罚因子 $\alpha_{\text{iodie}}(t) \ge 1.0$：
   $$\alpha_{\text{iodie}}(t) = 1.0 + \beta_1 \cdot \max\left(0, \; r_{\text{tlb\_miss}}(t) - \theta_{\text{tlb}}\right) + \beta_2 \cdot \max\left(0, \; r_{\text{cross\_die}}(t) - 0.05\right)$$
   *(注：当开启 YZN/CY 的 $1\text{GB}$ 巨页与 `iommu.passthrough=1` 并严格同 SCCL 亲和绑定后，$r_{\text{tlb\_miss}} \to 0$ 且 $r_{\text{cross\_die}} < 0.05$，正常态下 $\alpha_{\text{iodie}}(t) = 1.00$；一旦出现异常跨 NUMA 访问或巨页回退，$\alpha_{\text{iodie}}(t)$ 立即上升以惩罚该劣化路径)*。

#### （2）全链路三段式有效可用带宽合成方程
对于绑定在路径 $p = (s, r, k)$（即 SCCL $s$、PCIe RC $r$、网卡端口 $k$）上的 `MemChannel`，其瞬时物理可用带宽为：
$$BW_{\text{avail}}^{(p)}(t) = \frac{1}{\alpha_{\text{iodie}}(t)} \cdot \min\left(BW_{\text{DDRC\_rem}}^{(s)}(t), \; BW_{\text{PCIe\_RC\_rem}}^{(r)}(t), \; BW_{\text{Port\_rem}}^{(k)}(t)\right)$$

---

### 2.2 `64B CapabilityMatrix` 缓存行对齐二进制内存布局设计

为了确保单次 RDMA `IBV_WR_RDMA_WRITE_INLINE` 即可原子更新，且在 HOST CPU 侧严格对齐单个 L1/L2 Cache Line（$64\text{ Bytes}$）以实现无撕裂读取（Tear-free Read），定义 `struct alignas(64) CapabilityMatrixEntry` 的精确字节布局如下：

| 字节偏移 (Byte Offset) | 字段名称 (Field Name) | 数据类型 | 物理含义与单位说明 |
| :---: | :--- | :---: | :--- |
| `0x00 ~ 0x07` (8B) | `epoch_and_version` | `uint64_t` | 高 48 位为单调递增的 $500\mu\text{s}$ 遥测周期编号 `telemetry_epoch`；低 16 位为 Seqlock 奇偶校验版本号（偶数为稳定态）。 |
| `0x08 ~ 0x0F` (8B) | `timestamp_ns` | `uint64_t` | 采集时刻的 `cntvct_el0` 硬件纳秒时间戳（用于调度侧检测 >2ms 的陈旧探针并自动降级）。 |
| `0x10 ~ 0x17` (8B) | `ch_avail_bw_mbps[4]` | `uint16_t[4]` | 节点内 4 个 `MemChannel` (对应 4 个 SCCL/Port) 经三段式木桶方程合成后的实时可用带宽，单位：`10 MB/s`（量程 $0 \sim 655.35\text{ GB/s}$）。 |
| `0x18 ~ 0x1F` (8B) | `ddrc_util_permille[4]` | `uint16_t[4]` | 4 个 SCCL 的 DDRC 内存控制器带宽利用率千分比（$0 \sim 1000\text{‰}$）。 |
| `0x20 ~ 0x27` (8B) | `pcie_rc_stall_permille[4]` | `uint16_t[4]` | 4 个 PCIe RC 的 TLP 信用停顿率与队列拥塞千分比（$0 \sim 1000\text{‰}$）。 |
| `0x28 ~ 0x2B` (4B) | `iodie_alpha_fp8[4]` | `uint8_t[4]` | 4 个通道的 IO Die 惩罚因子 $\alpha_{\text{iodie}}$ 定点数编码：`val = round((alpha - 1.0) * 100)`。 |
| `0x2C ~ 0x2F` (4B) | `nic_queue_depth_kb[2]` | `uint16_t[2]` | 主备双物理网卡端口当前未完成 WQE 排队字节数（单位：`64 KB`）。 |
| `0x30 ~ 0x33` (4B) | `active_mpam_quota_pct` | `uint8_t[4]` | 当前 4 个 SCCL 上后台任务 MPAM `MBW_MAX` 配额百分比（$10\% \sim 85\%$）。 |
| `0x34 ~ 0x37` (4B) | `tier_free_cap_gb[2]` | `uint16_t[2]` | 本节点 Host DDR 池与 NVMe SSD 池的剩余可用容量（单位：`100 MB`）。 |
| `0x38 ~ 0x3B` (4B) | `fg_decode_active_bitmap` | `uint32_t` | 前台在线 Decode 活跃状态位图与实时 TPOT 裕度水位标志（供快路径 QoS 读取）。 |
| `0x3C ~ 0x3F` (4B) | `health_flags_and_crc` | `uint32_t` | 链路健康度标志位（含 RoCE ECN/PFC 告警位）与 16-bit 校验和。 |

---

### 2.3 消灭 2 个 3ms 慢指标：零系统调用采集与 $500\mu\text{s}$ 单边内联上报架构

在 10 月基线中，读取 DDRC 与网卡状态耗时高达 $3\text{ms}$（$3000\mu\text{s}$），根因在于每次采集均触发了内核态 `open/read/close` 遍历 `/sys/bus/event_source/devices/hisi_sccl_ddrc*` 文本节点与网卡 `ioctl` 锁竞争。11 月通过以下三级零系统调用架构将其压入 **本地采集 $< 2\mu\text{s}$、全网上报闭环 $< 500\mu\text{s}$**：

```
┌───────────────────────────────────────────────────────────────────────────────────┐
│                      节点本地硬件层 (Kunpeng 920 + CX6/URMA NIC)                  │
│  [DDRC / PCIe RC / SMMU / HHA PMU 硬件寄存器]       [NIC 物理端口计数器 DMA 引擎] │
└───────────────────────┬───────────────────────────────────────────┬───────────────┘
                        │ 1. 零系统调用直读 (<150ns/metric)         │ 2. 硬件自动刷写 (Auto-Dump)
                        ▼                                           ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│              NXY 本地遥测守护线程 (telemetry_daemon, 绑定独立管理核)              │
│  • 路径 A: perf_event_open + mmap (cap_user_rdpmc) 或 /dev/mem 只读映射 MMIO 页   │
│  • 路径 B: 直接解引用 Host Pinned Memory 中的网卡 DMA 计数器镜像页                │
│  • 耗时: 8 项指标采集 + 三段式方程合成总耗时 < 1.5µs (严守 <2µs 红线)             │
└───────────────────────┬───────────────────────────────────────────┬───────────────┘
                        │ 3a. 本地共享内存 Seqlock 写入 (<50ns)     │ 3b. IBV_WR_RDMA_WRITE_INLINE (500µs周期)
                        ▼                                           ▼
        [本机 L2/QoS 控制器直读]                   [远端调度中心 SegmentCapabilityTable]
```

1. **消灭慢指标一（Uncore PMU 零系统调用读取 + MMIO 双保险机制）**：
   * **首选路径（`perf_event_mmap_page`）**：守护进程初始化时调用 `perf_event_open()` 打开 `hisi_sccl_ddrc`、`hisi_pcie`、`smmuv3_pmcg`、`hisi_hha` 计数器，并通过 `mmap()` 映射环形缓冲区首页。
   * **微架构专家级兜底路径（`/dev/mem` 只读映射 Uncore MMIO 页）**：针对部分 Linux 内核版本下 ARM Uncore PMU 驱动未置位 `cap_user_rdpmc = 1`（导致 `mmap` 读取退化为 `read()` 系统调用）的隐患，在初始化阶段自动检测 `pc->cap_user_rdpmc`。若为 `0`，守护进程直接通过 `/dev/mem`（以 `O_RDONLY` 权限）将鲲鹏 920 片上 Uncore PMU 的物理 MMIO 计数寄存器页映射至用户态虚拟地址空间，运行时仅需执行一条 ARM64 `ldr` 内存加载指令（耗时 $< 150\text{ns}$），彻底绕过内核陷阱！
2. **消灭慢指标二（网卡状态计数器硬件 DMA Auto-Dump）**：
   * 废弃 `ethtool / sysfs / ibv_query_port` 慢速轮询。在驱动初始化阶段，调用 Mellanox `mlx5dv_devx_obj_create`（或 URMA 硬件计数器映射接口）配置网卡硬件微码，将物理端口吞吐、ECN 标记包数与队列水位以 $250\mu\text{s}$ 周期自动 DMA 刷写（Auto-Dump）至预分配的 Host `mmap` 锁定内存页。CPU 采集时仅做一次指针解引用（耗时 $< 20\text{ns}$）。
3. **$500\mu\text{s}$ 单边 RDMA Write Inline 无打扰同步**：
   * 守护线程每 $500\mu\text{s}$ 将组装好的 $64\text{B}$ `CapabilityMatrixEntry` 通过 `IBV_WR_RDMA_WRITE_INLINE` 直接单边写入中心调度节点的 `SegmentCapabilityTable[node_id]` 槽位。
   * **防 L3 Cache 抖动优化**：每个节点的 $64\text{B}$ 槽位严格独占一条 $64\text{B}$ 缓存行（消除 False Sharing），且每 $16$ 次上报才开启一次 `IBV_SEND_SIGNALED` 清理发送端 CQ，接收端 CPU 全程零中断、零轮询开销。

---

### 2.4 双时间尺度硬件 QoS 控制器（`HierarchicalQoSController`）

为了解决后台 $100\text{Gbps}$ 换出流量冲击前台 Decode TPOT（基线 $18.20\text{ms}$，容忍上限 $+0.55\text{ms}$）的难题，同时避免与 CY 的单通道顶速目标及 YY 的 CPU 压缩线程产生冲突，设计双时间尺度协同控制律：

#### （1）快路径控制（$200\mu\text{s}$ 时间尺度：自适应动态 WQE 微切片与批量门铃）
* **冲突调和机理（协同 CY `M2-T01`）**：若无条件将所有后台传输切碎为 $64\text{KB}$，会导致网卡 WQE 元数据开销增大，干扰 CY 冲击 $38\text{GB/s}+$ 平顶带宽。因此引入 **前台状态感知的自适应切片门限 $S_{\text{slice}}(t)$**：
  $$S_{\text{slice}}(t) = \begin{cases} 
  256\text{ KB}, & \text{if } \text{fg\_decode\_active\_bitmap}^{(s)} == 1 \quad (\text{前台 Decode 活跃，开启微切片抢占}) \\
  2048\text{ KB}, & \text{if } \text{fg\_decode\_active\_bitmap}^{(s)} == 0 \quad (\text{前台空闲，恢复大包顶速平顶})
  \end{cases}$$
* **为什么 $256\text{KB}$ 是黄金平衡点？** 在 PCIe 4.0/5.0 $\times 16$（约 $40\text{GB/s}$）链路上，单个 $256\text{KB}$ WQE 占用的 PCIe RC 连续传输时间仅为 $\frac{256\text{KB}}{40\text{GB/s}} = 6.4\mu\text{s}$。即使前台 Decode 触发高优先级访问，其在 PCIe RC 队列中的最大队头阻塞等待时间被严格封顶在 **$6.4\mu\text{s}$ 以内**（对 $18.2\text{ms}$ 的 TPOT 影响不足 $0.04\%$），而 $256\text{KB}$ 包长已足以让 RDMA/URMA 跑满 $> 95\%$ 的物理线速！
* **零额外门铃开销**：后台传输将 $2\text{MB}$ 数据拆分为 $8$ 个 $256\text{KB}$ 的 WQE 链表，通过一次 `ibv_post_send` 批量敲击一次 Doorbell，并在硬件队列上将后台 QP 映射至低优先级服务等级（`SL = 1, DSCP = 8`），前台通信映射至高优先级（`SL = 5, DSCP = 46`），由网卡硬件仲裁器在每 $6.4\mu\text{s}$ 的 WQE 边界执行无损抢占。

#### （2）慢路径控制（$20\text{ms}$ 时间尺度：MPAM 内存带宽硬件配额与压缩线程统一纳入）
* **跨模块微架构隔离（协同 YY `M2-F02` SIMD 压缩）**：YY 的鲲鹏 NEON 位平面流式压缩/解压线程以 $> 45\text{GB/s}$ 吞吐读写 DDR 内存。若不将其纳入 MPAM 管控，压缩线程将与前台推理争抢 DDRC 带宽。
* **MPAM 硬件分区映射**：
  * `PARTID_FG (ID=1)`：绑定前台 vLLM/SGLang 推理 Worker 与控制面快查线程，`MBW_MAX = 100%`，并锁死 4 路 L3 Cache（`CPOR = 0x000F`）；
  * `PARTID_BG (ID=2)`：**统一绑定后台 KVC 搬运线程、LJQ 的 SSD 刷盘线程以及 YY 的 SIMD 压缩/解压线程池**。
* **比例-积分（PI）动态配额控制律**：每 $20\text{ms}$ 读取一次经 EWMA 平滑后的前台 TPOT 增量 $\Delta \text{TPOT}_{\text{ewma}}$ 与 DDRC 利用率，动态调整 `/sys/fs/resctrl/PARTID_BG/schemata` 中的 `MBW_MAX` 配额 $Q_{\text{bg}}(t) \in [10\%, 85\%]$：
  $$Q_{\text{bg}}(t + \Delta t) = \text{clip}\left(Q_{\text{bg}}(t) - K_p \cdot \left(\Delta \text{TPOT}_{\text{ewma}}(t) - 0.30\text{ms}\right), \; 10\%, \; 85\%\right)$$
  当 $\Delta \text{TPOT}_{\text{ewma}} > 0.30\text{ms}$（逼近 $0.55\text{ms}$ 红线的安全水位）时，硬件状态机在微秒内锁死后台 DDRC 带宽配额；当 $\Delta \text{TPOT}_{\text{ewma}} > 0.45\text{ms}$ 时，触发紧急熔断，挂起后台低优换出队列。

---

### 2.5 `M2-T04` 重点代码修改位置与接口锚点清单

开发人员及编码 Agent 请严格按照以下文件与函数锚点实施改造：

| 代码库 / 模块 | 目标文件路径 (Filepath Anchor) | 核心类 / 函数修改点与具体改造逻辑说明 |
| :--- | :--- | :--- |
| **Testbed 遥测守护进程** | `方案设计/调度面优化/testbed/src/telemetry_daemon.cpp` | 1. 新增 `KunpengUncorePmuReader` 类：实现 `perf_event_open` + `mmap` 初始化，并加入 `cap_user_rdpmc` 检查与 `/dev/mem` Uncore MMIO 物理地址页只读映射兜底函数 `mmap_uncore_mmio_registers()`；<br>2. 新增 `NicCounterDmaReader` 类：替换原有的 `/sys/class/infiniband` 文件读取，直接读取预映射的硬件计数器页；<br>3. 在主循环 `run_telemetry_loop()` 中实现 $500\mu\text{s}$ 周期的 ثلاث段式木桶方程解算，更新本地共享内存 `/dev/shm/kvc_cap_matrix` 并调用 `post_rdma_write_inline_64b()`。 |
| **硬件隔离初始化脚本** | `方案设计/调度面优化/testbed/02_hardware_isolation.sh` | 1. 挂载 `/sys/fs/resctrl` 并创建 `PARTID_FG` 与 `PARTID_BG` 两个控制组；<br>2. 将 YY 的 SIMD 压缩线程 TID、LJQ 的 `io_uring` 线程 TID 与 Mooncake 后台传输线程 TID 统一写入 `/sys/fs/resctrl/PARTID_BG/tasks`；<br>3. 配置 CX6/URMA 网卡的 QoS 优先级队列（`mlx5_qos` / `ets`），将前台与后台 QP 分别绑定至不同 Traffic Class (TC)。 |
| **Mooncake 传输引擎** | `mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp` | 1. 在 `submitTransferRequest()` 中接入 NXY 的快路径微切片逻辑：读取共享内存中的 `fg_decode_active_bitmap`，动态选择 `slice_size = 256KB` 或 `2048KB`；<br>2. 将拆分后的子 WQE 串成单向链表，通过单次 `ibv_post_send()` 下发，设置每 $16$ 个 WQE 触发一次 `IBV_SEND_SIGNALED`。 |
| **双时间尺度 QoS 控制器** | `mooncake-transfer-engine/include/qos/hierarchical_qos_controller.h` *(新增)* | 实现 `HierarchicalQoSController::step_slow_path_20ms()`：计算前台 TPOT 的 EWMA 偏差，通过保持打开的文件描述符 `fd_schemata`（使用 `pwrite` 避免重复 `open` 开销）动态更新 MPAM `MBW_MAX` 百分比与网卡硬件 Rate Limiter。 |

---

## 第三部分：WXH (1.0 FTE) `M2-T05` 详细优化方案设计——下沉式 KVC 集群控制器（微秒选路 + 跨层升降级决策）

### 3.1 总体架构：L2 调度决策大脑与 L3 数据面手脚的正交解耦协议

为彻底根除 10 月攻关中出现的职责重叠，确立 **WXH 独占 L2 全局调度决策层（Policy Brain）**，通过标准化的异步指令契约驱动下层 L3 数据面执行引擎（Mechanism Hands）：

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│           vLLM / SGLang 推理引擎调度主循环 (临界区阻塞严格 < 100µs)                    │
│     [READY_FAST_QUEUE: HBM/DDR 就绪请求]       [WARMING_SLOW_QUEUE: SSD 异步升迁等待]   │
└───────────────────────────────┬────────────────────────────────────────┬───────────────┘
                                │ 1. 下发时延契约 (Fetch + Deadline)     ▲ 5. 升迁完成唤醒
                                ▼                                        │
┌────────────────────────────────────────────────────────────────────────┴───────────────┐
│        WXH 下沉式 KVC 集群控制器 (L2 调度面总控大脑: 横向选路 + 纵向升降级)            │
│                                                                                        │
│  ┌────────────────────────────────────────┐  ┌──────────────────────────────────────┐  │
│  │ A. 横向微秒级通道选路避险引擎 (<2µs)   │  │ B. 纵向跨介质冷热升降级状态机        │  │
│  │ • MountedSegment 内嵌 64B 能力矩阵     │  │ • SWE-bench Tool-Wait TTL 休眠窗感知 │  │
│  │ • Step 1 带 Epoch 原子在途预扣 (防重扣)│  │ • O(L^2) 重算代价 × 热度 效益密度淘汰│  │
│  │ • 感知压缩与碎片度的闭式 CostEvaluator │  │ • 提前 2s 触发 SSD->DDR 超前异步升迁 │  │
│  └──────┬──────────────────┬──────────┬───┘  └──────────────────┬───────────────────┘  │
└─────────┼──────────────────┼──────────┼─────────────────────────┼──────────────────────┘
          │ 指令1: 多通道拉取│ 指令2: K*│ 指令3: 契约熔断         │ 指令4: Promote/Demote
          ▼                  ▼          ▼                         ▼
   [CY/YZN 多通道引擎]  [LDY 按层双缓冲] [GPU 本地透明重算]  [LJQ 异构介质零拷贝引擎]
```

---

### 3.2 横向微秒级选路避险引擎详细设计

#### （1）`MountedSegment` 内嵌 $64\text{B}$ 能力矩阵与 Step 1 带 Epoch 原子预扣（根除双重扣减）
在 Mooncake 控制面查询命中返回 `MountedSegment` 描述符时，直接内嵌由 NXY 维护的 $64\text{B}$ `CapabilityMatrixEntry` 镜像。

* **专家级问题修复：为什么朴素的 `inflight_bytes` 预扣会导致“双重扣减（Double-Counting）”？**
  假设在时刻 $t_0$（遥测周期 $E_k$），请求 $R_1$ 预扣了 $500\text{MB}$ 数据并开始 RDMA 传输（耗时 $15\text{ms}$）。到了 $t_0 + 500\mu\text{s}$（遥测周期 $E_{k+1}$），NXY 的硬件 PMU 探针已经实测到了 $R_1$ 正在占用的物理带宽，并将更新后的 $BW_{\text{avail}}(E_{k+1})$ 上报给调度器。此时 $R_1$ 尚未传完，如果调度器在计算有效带宽时继续从 $BW_{\text{avail}}(E_{k+1})$ 中减去 $R_1$ 的 `inflight_bytes`，就会造成 **物理探针扣一次 + 软件预扣又扣一次** 的双重扣减，导致大量原本健康的请求被误判为拥塞而回退重算！
* **工程解法：双桶 Epoch 滑动窗口预扣器（`EpochAwareReservationSlab`）**：
  将每个通道 $p$ 的在途预扣拆分为两个无锁原子计数器：`pending_bytes[epoch % 2]`（仅统计**当前 $500\mu\text{s}$ 遥测周期 $E_k$ 内刚刚下发、尚未被 NXY 下一拍硬件探针捕获**的新增突发流量）。
  当请求在周期 $E_k$ 到达时，原子执行：
  $$\text{atomic\_fetch\_add}\left(\text{pending\_bytes}^{(p)}[E_k \bmod 2], \; S_{\text{payload}}\right)$$
  此时用于选路决策的瞬时有效可用带宽 $BW_{\text{eff}}^{(p)}(t)$ 为：
  $$BW_{\text{eff}}^{(p)}(t) = \max\left(BW_{\text{min}}, \; BW_{\text{avail}}^{(p)}(E_k) - \frac{\text{pending\_bytes}^{(p)}[E_k \bmod 2]}{\tau_{\text{window}}}\right)$$
  一旦 NXY 的新周期 $E_{k+1}$ 矩阵到达（意味着 $E_k$ 周期内发起的传输已进入物理 PMU 统计口径），调度器立即将旧桶 $\text{pending\_bytes}^{(p)}[E_k \bmod 2]$ 原子清零！这样既 **完美填补了 $500\mu\text{s}$ 的硬件遥测盲区（拦截微突发羊群踩踏）**，又 **$100\%$ 消除了跨周期的双重扣减**。同时保留 $50\text{ms}$ 僵尸租约自愈回收器，防止异常崩溃泄漏。

#### （2）感知压缩率（YY）与 Slab 碎片度（WZX）的闭式成本评估器（`CostEvaluator`）
为了与 11 月 YY 的 SIMD 位平面压缩（`M2-F02`）及 WZX 的双池 Slab 分配器（`M2-T03`）无缝联动，升级传输耗时方程与重算耗时方程：

1. **修正后的全链路传输耗时方程 $T_{\text{transfer}}^{(p)}$**：
   $$T_{\text{transfer}}^{(p)} = \frac{L_{\text{match}} \cdot S_{\text{token}} \cdot \gamma_{\text{comp}}}{BW_{\text{eff}}^{(p)}(t)} + \frac{L_{\text{match}} \cdot S_{\text{token}} \cdot \mathbb{I}(\gamma_{\text{comp}} < 1)}{BW_{\text{SIMD\_decomp}}} + N_{\text{slab\_desc}} \cdot t_{\text{sge\_post}} + T_{\text{RTT}}$$
   其中：
   * $\gamma_{\text{comp}} \in [0.46, 1.0]$ 为该 KVC Segment 写入时记录在元数据中的实际压缩比（未压缩为 $1.0$，BF16 无损位平面压缩约为 $0.54$，FP8 压缩约为 $0.46$）；
   * $BW_{\text{SIMD\_decomp}} \approx 45\text{ GB/s}$ 为 YY 鲲鹏 NEON 流式解压吞吐；
   * $N_{\text{slab\_desc}}$ 为 WZX `Prefill Slab Pool`（16-Block 连续簇）分配后的实际离散描述符段数（稳态下相比原生 FIFO 减少 $16\times$），$t_{\text{sge\_post}}$ 为单 SGE 硬件提交开销。
2. **考虑前缀平方复杂度的本地重算耗时方程 $T_{\text{recompute}}$**：
   对于总长度为 $L_{\text{total}}$、命中前缀长度为 $L_{\text{match}}$（命中率 $h = L_{\text{match}} / L_{\text{total}}$）的请求，若回退本地全量重算，相较于只计算后缀 $(L_{\text{total}} - L_{\text{match}})$ 所额外付出的重算时间成本为：
   $$T_{\text{recompute}}(L_{\text{match}}) = N_{\text{layer}} \cdot \left(\frac{4 \cdot L_{\text{match}} \cdot D^2 + 2 \cdot L_{\text{match}}^2 \cdot D}{P_{\text{NPU\_eff}}}\right)$$
3. **三态路由决策与 $K^*$ 动态切分方程（对接 LDY `M2-F01`）**：
   遍历候选副本所在的物理通道 $p \in \mathcal{P}$，取 $p^* = \arg\min_p T_{\text{transfer}}^{(p)}$：
   * **状态一（全量拉取 `FETCH_ALL`）**：若 $T_{\text{transfer}}^{(p^*)} \le T_{\text{recompute}}(L_{\text{match}}) - \Delta T_{\text{margin}}$ 且第 $0$ 层首包等待可控，下发全量拉取指令至通道 $p^*$；
   * **状态二（按层混合掩盖 `HYBRID_K_STAR`）**：若网络处于中度拥塞（如 $8 \sim 20\text{GB/s}$），全量拉取存在流水线断流气泡，则解算前 $K^*$ 层本地重算、后 $(N_{\text{layer}} - K^*)$ 层并发拉取的平衡方程：
     $$K^* \cdot t_{\text{layer\_recomp}} = \left(N_{\text{layer}} - K^*\right) \cdot t_{\text{layer\_fetch}}\left(BW_{\text{eff}}^{(p^*)}\right) + T_{\text{RTT}} \implies K^* = \left\lceil \frac{N_{\text{layer}} \cdot t_{\text{layer\_fetch}} + T_{\text{RTT}}}{t_{\text{layer\_recomp}} + t_{\text{layer\_fetch}}} \right\rceil$$
     若 $1 \le K^* \le 0.5 N_{\text{layer}}$，则向 LDY 双缓冲引擎下发 $K^*$ 切分指令，实现**零气泡计算通信完全重叠**；
   * **状态三（契约熔断回退 `FALLBACK_LOCAL_RECOMPUTE`）**：若 $T_{\text{transfer}}^{(p^*)} > T_{\text{recompute}}(L_{\text{match}})$ 或 $K^* > 0.5 N_{\text{layer}}$（如 $3\text{GB/s}$ 恶劣拥塞），立即在 $< 2\mu\text{s}$ 内阻断网络 I/O，回退本地重算，确保 **$0\%$ 性能退化**。

---

### 3.3 纵向跨介质（`HBM <-> DDR <-> SSD`）冷热升降级状态机详细设计

#### （1）融合 $O(L^2)$ 重算代价与 SWE-bench 工具休眠窗的效益密度函数
传统的 LRU 淘汰策略将所有 Block 视为同权，极易在容量受限时驱逐高价值的深层长前缀。WXH 在集群控制器中定义每个 KVC 对象 $i$ 的 **留存效益密度评分（Retention Utility Density, $\mathcal{U}_i$）**：
$$\mathcal{U}_i(t) = \frac{T_{\text{recompute}}(L_i)}{\text{Size}_{\text{bytes}}(L_i, \gamma_{\text{comp}})} \cdot P_{\text{hit}}\left(\Delta t_{\text{idle}}, \; \text{State}_{\text{agent}}\right) \propto \frac{L_i}{\gamma_{\text{comp}}} \cdot P_{\text{hit}}\left(\Delta t_{\text{idle}}, \; \text{State}_{\text{agent}}\right)$$
* **$O(L^2)$ 代价自动加权**：由于重算代价 $T_{\text{recompute}} \propto L_i^2$，而存储体积 $\text{Size} \propto L_i \cdot \gamma_{\text{comp}}$，二者相除后，单位字节的留存收益天然与序列长度 $L_i$ 成正比（并受压缩比 $\gamma_{\text{comp}}$ 放大）！这意味着 $32\text{K}$ 序列的每字节留存价值是 $2\text{K}$ 序列的 $16$ 倍。
* **SWE-bench Agent 状态机感知概率 $P_{\text{hit}}$**：
  通过解析上层请求 Header 或会话钩子中的 Agent 状态标签：
  1. **`STATE_TOOL_CALL_ENTER`（进入工具执行休眠窗，预期休眠 $\hat{\tau}_{\text{tool}} \in [10\text{s}, 120\text{s}]$）**：当 $\hat{\tau}_{\text{tool}} > 15\text{s}$ 且 Host DDR 水位 $> 80\%$ 时，控制器主动将 $P_{\text{hit}}$ 临时调低，向 LJQ 下发 `AsyncDemote(DDR -> SSD, LayerMajor_16MB)`，腾出 DDR 空间供当前活跃请求使用；
  2. **`STATE_PROACTIVE_WARMUP`（预计返回前超前预热）**：后台定时器在距离预计工具返回时刻 $\hat{\tau}_{\text{tool}} - 2.0\text{s}$ 时（或接收到 Agent 沙箱进程退出的轻量 Webhook 信令瞬间），立即将 $P_{\text{hit}}$ 拉升至 $1.0$，向 LJQ 下发 `AsyncPromote(SSD -> DDR)`，在推理请求真正到达前完成静默预热！

#### （2）临界区剥离与快慢双队列物理隔离（`READY_FAST_QUEUE` vs. `WARMING_SLOW_QUEUE`）
针对专家指出的“若 Agent 提前返回、KVC 仍在 SSD 中时阻塞 vLLM 调度主循环”的致命风险，借鉴 `FlatCake FC-MAS` (arXiv:2610.38355)，对推理调度主循环实施 **非阻塞两阶段队列改造**：
1. 在 `Scheduler.schedule()` 中（临界区耗时严格限制在 $< 100\mu\text{s}$）：
   * 若请求命中的 KVC 位于 HBM 或 Host DDR（`Tier == TIER_HBM || Tier == TIER_DDR`），直接进入 **`READY_FAST_QUEUE`**，参与本轮 Continuous Batching 调度；
   * 若请求命中的 KVC 位于 NVMe SSD（`Tier == TIER_SSD`），首先调用 `CostEvaluator` 校验 SSD 升迁排队预估耗时 $T_{\text{ssd\_promote}}$ 是否小于 $T_{\text{recompute}}$：
     * 若 $T_{\text{ssd\_promote}} \ge T_{\text{recompute}}$：直接放弃 SSD 读取，转入 `READY_FAST_QUEUE` 执行本地重算；
     * 若 $T_{\text{ssd\_promote}} < T_{\text{recompute}}$：向 LJQ 下发高优异步升迁指令，并将该请求移入 **`WARMING_SLOW_QUEUE`**（**此时绝不为其分配 HBM 物理块，也绝不阻塞当前 Batch 的 Decode 执行！**）。
2. 当 LJQ 的 `io_uring` 异步 DMA 完成 `SSD -> DDR` 搬运并通过原子回调置位 `KVHandle.status = DDR_READY` 后，下一轮调度循环（$< 1\text{ms}$ 内）自动将该请求从 `WARMING_SLOW_QUEUE` 弹出并推入 `READY_FAST_QUEUE`。

---

### 3.4 `M2-T05` 重点代码修改位置与接口锚点清单

| 代码库 / 模块 | 目标文件路径 (Filepath Anchor) | 核心类 / 函数修改点与具体改造逻辑说明 |
| :--- | :--- | :--- |
| **Mooncake 元数据与描述符** | `mooncake-store/include/metadata.h`<br>`mooncake-store/src/master_service.cpp` | 1. 在 `MountedSegment` 结构体尾部内嵌对齐的 `CapabilityMatrixEntry cap_matrix` ($64\text{B}$) 以及压缩率字段 `float comp_ratio`；<br>2. 新增 `EpochAwareReservationSlab` 类：维护双桶原子计数器 `std::atomic<uint64_t> pending_bytes_[4][2]`，在 Step 1 `GetReplicaList()` 返回前完成原子预扣，并在接收到新 `telemetry_epoch` 时翻转清零旧桶。 |
| **WXH 成本评估与选路引擎** | `mooncake-store/src/scheduler/cost_evaluator.cpp` *(新增)* | 1. 实现 `CostEvaluator::evaluate_route()`：使用无锁 Seqlock 读取 `MountedSegment.cap_matrix`，结合 `comp_ratio` 与 WZX 传入的 `slab_desc_count` 计算 $T_{\text{transfer}}$ 与 $T_{\text{recompute}}$，耗时控制在 $< 500\text{ns}$；<br>2. 输出枚举决策 `{FETCH_ALL, HYBRID_K_STAR(k_star), FALLBACK_RECOMPUTE}`。 |
| **纵向跨介质生命周期管理** | `mooncake-store/src/scheduler/tier_lifecycle_manager.cpp` *(新增)* | 1. 实现 `TierLifecycleManager::calc_utility_density()`：按 $\mathcal{U}_i \propto (L_i / \gamma_{\text{comp}}) \cdot P_{\text{hit}}$ 维护最小堆淘汰队列；<br>2. 实现 `on_agent_tool_event()` 接口：处理 `TOOL_ENTER` 延迟降级与 `TOOL_EXIT / TTL_EXPIRING` 超前升迁，调用 LJQ 的 `StorageBackend::async_promote_to_ddr()` 与 `async_demote_to_ssd()`。 |
| **vLLM / SGLang 调度器插件** | `vllm/core/scheduler.py` 或<br>`sglang/srt/managers/schedule_policy.py` | 1. 在调度队列中拆分 `ready_fast_queue` 与 `warming_slow_queue`，剥离所有同步等待 I/O 的调用（确保 `schedule()` 耗时 $< 100\mu\text{s}$）；<br>2. 根据 `CostEvaluator` 返回的 `k_star` 参数，设置 `seq_group.hybrid_recompute_layers = k_star`，交由 LDY 的 Attention Layer Hook 执行前 $K^*$ 层重算与后 $(80-K^*)$ 层 DMA 异步挂载。 |

---

## 第四部分：全栈实验平台 `HoloTestbed-4X` 建设与全链路验证执行步骤设计（含 `W3-T06` 八级递进消融）

### 4.1 `HoloTestbed-4X` 物理拓扑与 `cntvct_el0` 纳秒级 7 锚点黑匣子打点规范

#### （1）单机 4 卡双命名空间光纤物理环回拓扑
为了在 100% 激发真实鲲鹏 920 片上微架构（4-SCCL、SMMUv3、PCIe RC）与物理光模块 RDMA/URMA 协议栈的同时，消除跨机房交换机背景噪声并实现零时钟漂移（Zero Clock Skew），采用如下标准拓扑：
* **发送域（Namespace A / `ns_storage_pool`）**：绑定 Socket 0（SCCL 0 & SCCL 1）、物理网卡 Port 0、2 张计算卡及本地 NVMe SSD RAID0 阵列，模拟远端/分层 KVC 存储池节点；
* **接收与推理域（Namespace B / `ns_infer_engine`）**：绑定 Socket 1（SCCL 2 & SCCL 3）、物理网卡 Port 1、2 张计算卡，运行 vLLM/SGLang 在线推理引擎；
* **物理连接**：Port 0 与 Port 1 通过 $400\text{Gbps} / 200\text{Gbps}$ 物理光纤跳线直连环回（Optical Loopback），流量 $100\%$ 走真实 PCIe 总线、网卡 SerDes 与光模块，絕不走内核 loopback 软短路。

#### （2）基于 ARM `cntvct_el0` 寄存器的 7 锚点零开销黑匣子打点
废弃会产生数百纳秒开销的 `clock_gettime()`，统一使用内联汇编读取鲲鹏系统计数器 `mrs %0, cntvct_el0`（开销 $< 8\text{ns}$），并在每个请求的生命周期内记录定长 $64\text{B}$ 轨迹日志 `TraceRecord`：
1. `T1_lookup_start`：请求进入控制面发起前缀匹配时刻；
2. `T2_schedule_done`：控制面返回 `MountedSegment` 且 WXH `CostEvaluator` 完成 `<2µs` 选路与 Step 1 原子预扣时刻（$\Delta T_{\text{sched}} = T_2 - T_1$）；
3. `T3_desc_post`：WZX Slab 描述符编译完成并调用 `ibv_post_send` 敲击网卡 Doorbell 时刻；
4. `T4_dma_first_layer`：第 $0$ 层（或第 $K^*$ 层）KVC DMA 传输完成并通知计算流时刻；
5. `T5_dma_all_done`：全部 80 层 KVC 传输（及 YY SIMD 解压）完成时刻；
6. `T6_first_token`：Prefill 算子完成并吐出首个 Token 时刻（$\text{E2E TTFT} = T_6 - T_1$）；
7. `T7_decode_step_avg`：并发 Decode 阶段每步耗时均值（用于实时核算 $\text{TPOT}$ 干扰率）。

---

### 4.2 11月6日（W1 周五）外援交付门禁检查单与 Plan A / Plan B 接管预案

WXH 作为 `HoloTestbed-4X` 总统筹人，必须在 **11月6日（W1 周五）18:00** 对外援交付成果执行刚性门禁审查：

* **门禁通过硬性标准（4 项全过方为 Pass）**：
  1. `01_env_init.sh` 与 `02_hardware_isolation.sh` 无报错跑通，1GB 巨页、SMMU passthrough、MPAM `/sys/fs/resctrl` 分区全部生效；
  2. 双命名空间物理光纤环回 RDMA/URMA 带宽打通，大块基线带宽稳定 $\ge 40\text{GB/s}$；
  3. 跑通 `03_run_benchmark.sh`（vLLM + Mooncake 8B/32B 模型），成功导出包含 `T1~T7` 纳秒打点的首份 `E0` 基线 CSV 报表；
  4. 连续 10 次重复跑批的 TTFT 与带宽变异系数 **$\text{CV} < 5.0\%$**（优于 $< 10\%$ 红线）。
* **门禁切换预案**：
  * **Plan A（门禁 Pass）**：外援继续承担 W2~W4 的日常回归压测与数据收集脚本维护，WXH 100% 聚焦调度算法研发与消融分析；
  * **Plan B（任一指标未达标或进度滞后）**：11月6日当晚立即触发备援切换，由具备深厚 Linux 内核与硬件调优功底的 **LDY** 介入并用 1 天时间收口脚本环境，确保 W2 全员联调绝不延误。

---

### 4.3 分步实验验证执行手册（Step-by-Step Execution Playbook）

#### 实验一（`M2-T04-Exp1`）：`CapabilityMatrix` 零系统调用采集时延与三段式木桶精度验证
* **执行步骤**：
  1. **Step 1（基线采集对比）**：在 `HoloTestbed-4X` 上分别运行 10 月旧版探针（sysfs/ioctl）与 11 月新版 `telemetry_daemon`（`mmap` / MMIO + NIC Auto-Dump），连续采样 $100,000$ 次，统计单次采集（8 个指标）与单边 RDMA 内联写上报的 P50 / P99 / P999 耗时；
  2. **Step 2（三段式瓶颈注入拟合）**：使用流量发生器分别构造三种单一瓶颈工况：① 用 `stress-ng --vm` 压满 SCCL 0 的 DDRC 带宽；② 用跨 Die DMA 压满 IO Die HHA/PCIe RC；③ 用 `ib_write_bw` 压满网卡端口。对比 `CapabilityMatrix` 计算出的 $BW_{\text{avail}}^{(p)}(t)$ 与实测 KVC 传输吞吐的拟合误差。
* **验收通过标准**：本地 8 指标采集耗时 **$\text{P99} < 2.0\mu\text{s}$**（较 $3000\mu\text{s}$ 提速 $> 1500\times$）；含 RDMA 上报与中心表构建总周期 **$< 500\mu\text{s}$**；三段式带宽预测相对误差 **$< 8\%$**。

#### 实验二（`M2-T04-Exp2`）：100Gbps 后台满载混压下双时间尺度硬件 QoS 护航实测
* **执行步骤**：
  1. **Step 1（纯前台基线测定）**：在接收域启动在线 Decode 稳态负载（Batch Size = $32$，输入 $8\text{K}$），关闭后台搬运，测定纯前台基线 $\text{TPOT}_{\text{base}}$（预期约 $18.20\text{ms}$）；
  2. **Step 2（无控混压对照组）**：关闭 NXY QoS 控制器，在同 SCCL 后台持续注入 $100\text{Gbps}$ 的 KVC 换出与 YY SIMD 压缩流量，记录前台 $\text{TPOT}_{\text{uncontrolled}}$ 劣化幅度（预期劣化至 $\sim 21.29\text{ms}$，即 $+16.98\%$）；
  3. **Step 3（双时间尺度 QoS 开启组）**：开启 $200\mu\text{s}$ 自适应 WQE 微切片（$256\text{KB}$）与 $20\text{ms}$ MPAM `MBW_MAX` 动态限流，在同样 $100\text{Gbps}$ 后台压力下连续运行 $300\text{s}$，同步记录前台 TPOT、后台实际完成排出带宽以及固定 SLA（$\text{TPOT} \le 18.75\text{ms}$）下的最大有效 QPS。
* **验收通过标准**：前台 Decode TPOT 干扰率 **$< 2.0\%$（绝对值 $\le 18.56\text{ms}$，严守 $< 3\%$ 红线）**；后台实际有效排出带宽维持 **$\ge 75\text{Gbps}$**；混压工况下系统有效达标 QPS（Goodput）相比静态限流/无控基线提升 **$\ge 15\%$**。

#### 实验三（`M2-T05-Exp1`）：恶劣拥塞（$3\text{GB/s}$）与中度拥塞（$8 \sim 15\text{GB/s}$）下选路避险与 $K^*$ 掩盖实测
* **执行步骤**：
  1. **Step 1（网络沙箱限速注入）**：利用网卡硬件 Rate Limiter 或背景干扰流，将测试通道可用带宽分别设定在 $42\text{GB/s}$（健康）、$12\text{GB/s}$（中度拥塞）、$6\text{GB/s}$（轻度倒挂）、$3\text{GB/s}$（严重拥塞）四个档位；
  2. **Step 2（策略对比跑批）**：在 $8\text{K}$ 上下文（$70\%$ 与 $100\%$ 前缀命中）下，对比三种策略：① `Naive Always-Fetch`（盲目拉取）；② `Always-Recompute`（纯本地重算）；③ `WXH CostEvaluator + LDY K* Hybrid`。
* **验收通过标准**：选路决策耗时 **$\text{P99} < 2.0\mu\text{s}$**；在 $3\text{GB/s}$ 恶劣拥塞下 $100\%$ 自动回退重算，**$\text{E2E TTFT}$ 较盲目拉取降低 $\ge 61.8\%$（$629.9\text{ms} \to 240.7\text{ms}$），负收益请求占比为 $0\%$**；在 $12\text{GB/s}$ 中度拥塞下，启用 $K^*$ 混合掩盖比纯拉取和纯重算的 $\text{TTFT}$ 均降低 **$15\% \sim 25\%$**。

#### 实验四（`M2-T05-Exp2`）：SWE-bench 20 实例 $\times$ 20 并发微突发（400 并发）防羊群踩踏实测
* **执行步骤**：
  1. **Step 1（微突发流量回放）**：使用 SWE-bench 真实轨迹回放器，模拟 $20$ 个 Agent 实例在同一毫秒窗口内各自突发 $20$ 个并发请求（共 $400$ 并发，共享 $32\text{K}$ 前缀，$98\%$ 命中 + $512$ 新增 Token），且远端 KVC 分布在 4 个 `MemChannel` 上；
  2. **Step 2（开启/关闭原子预扣对比）**：对比关闭预扣（请求全部扎堆打向默认通道 0）与开启 `EpochAwareReservationSlab`（Step 1 原子预扣 + 多通道分流 + 溢出转 $K^*$ 重算），并扫描不同请求到达率 $\lambda$ 下的系统容量拐点。
* **验收通过标准**：$400$ 并发微突发下 **$\text{P99 TTFT}$ 降低 $> 60\%$**；无双重扣减误判（健康负载下缓存有效利用偏差 $< 2\%$）；稳态高负载下的 **系统有效吞吐（Goodput）提升 $12\% \sim 18\%$**。

#### 实验五（`M2-T05-Exp3`）：72B 超显存 $1.5 \sim 2\times$ 下跨介质智能升降级与快慢队列隔离实测
* **执行步骤**：
  1. **Step 1（超容量多轮负载构造）**：配置活跃会话总 KVC 工作集为 HBM+DDR 容量的 $1.8$ 倍，注入包含真实 Tool-Wait 休眠窗（$15\text{s} \sim 60\text{s}$）的多轮 Agent 会话流，同时混入短增量在线请求；
  2. **Step 2（三组消融对比）**：① 朴素 LRU + 同步直读 SSD；② WXH 效益密度淘汰 + Tool-Wait 超前升迁（无快慢队列隔离）；③ 完整方案（效益密度 + 超前升迁 + `FC-MAS` 快慢双队列隔离 + LJQ $16\text{MB}$ 层主序聚簇 I/O）。
* **验收通过标准**：调度器主循环临界区阻塞耗时 **$\text{P99} < 100\mu\text{s}$**；超显存下全局有效命中率恢复至 **$> 85\%$**；多轮会话 **系统总有效吞吐（Goodput）提升 $> 25\%$**，请求有效完成率 **$> 95\%$**。

---

### 4.4 `W3-T06` 全链路八级递进消融实验矩阵与奥卡姆剃刀剪枝准则

在第 4 周（11.23~11.29），由 WXH 主持全员协同开展 `W3-T06` 八级递进消融实验。严格执行 **“单项叠加、控制变量、量化边际贡献、负增益一票剪枝”** 的奥卡姆剃刀准则：

| 消融层级 | 递进叠加模块配置 (Cumulative Configuration) | 责任人 | 核心局部观测指标 | 端到端业务指标预期边际贡献 (TTFT / TPOT / Goodput) |
| :---: | :--- | :---: | :--- | :--- |
| **Level 0** | **原始基线**：Vanilla vLLM/SGLang + Mooncake 默认传输 | WXH | 基线带宽 $\sim 12\text{GB/s}$，描述符零合并 | 冻结基准分母（环境变异系数 $\text{CV} < 4.8\%$） |
| **Level 1** | `Level 0` + **上层双池 16-Block Slab 分配与硬件 SGE/UMR 合并** | + WZX | 稳态 VA 连续率 $<1\% \to >85\%$，提交耗时 $15.4\text{ms} \to <1.5\text{ms}$ | 单请求 $\text{TTFT}$ 净降 **$5\% \sim 12\%$** |
| **Level 2** | `Level 1` + **URMA/RDMA 单通道顶速平顶与 4-SCCL 多通道并发** | + CY/YZN | 单通道平顶 $>38\text{GB/s}$，多通道聚合 $>70\text{GB/s}$，跨 Die 放大 $=1.00\times$ | 长序列（$8\text{K} \sim 32\text{K}$）加载等待缩减 $>55\%$，$\text{TTFT}$ 再降 **$10\% \sim 15\%$** |
| **Level 3** | `Level 2` + **鲲鹏 NEON 位平面流式压缩** | + YY | 压缩吞吐 $>45\text{GB/s}$，KVC 传输体积缩减 $45\% \sim 54\%$ | 逻辑等效带宽翻倍，高并发带宽受限期 **Goodput 提升 $10\% \sim 15\%$** |
| **Level 4** | `Level 3` + **按层双缓冲滑动窗口计算-通信全掩盖** | + LDY | HBM 窗口仅占 $256\text{MB}$，计算通信重叠率 $>80\%$ | 端到端 $\text{TTFT}$ 独立再降 **$15\% \sim 22\%$** |
| **Level 5** | `Level 4` + **`<500µs` 能力矩阵与双时间尺度硬件 QoS** | + NXY | 采集 $<2\mu\text{s}$，后台维持 $\ge 75\text{Gbps}$ | $100\text{G}$ 混压下 **$\text{TPOT}$ 干扰从 $+16.98\%$ 压至 $<2.0\%$，Goodput $+15\%$** |
| **Level 6** | `Level 5` + **微秒选路避险、$K^*$ 切分与 Step 1 防踩踏预扣** | + WXH | 决策 $<2\mu\text{s}$，$3\text{GB/s}$ 下 $0\%$ 退化 | 恶劣链路 $\text{TTFT}$ 降 **$61.8\%$**；$400$ 并发微突发 **$\text{P99 TTFT}$ 降 $>60\%$，Goodput $+12\%+$** |
| **Level 7** | `Level 6` + **跨介质智能升降级、快慢队列隔离与 SSD 直通** | + WXH/LJQ | SSD 聚簇吞吐 $12 \sim 18\text{GB/s}$，超显存命中率 $>85\%$ | $1.5 \sim 2\times$ 超显存多轮会话 **Goodput 提升 $>25\%$**（全链路总验收） |

> **奥卡姆剃刀剪枝铁律**：在 Level $i-1 \to$ Level $i$ 的递进测试中，若某项特性在目标工况下的端到端业务指标（TTFT / TPOT / Goodput）净收益 $< 1.5\%$ 或引入长尾抖动，必须立即定位根因；若在 W4 周三前无法修复为确定性正收益，由架构师 LL 与 WXH 联合裁定将其从 E3 生产主干分支中剪枝剥离。

---

### 4.5 11月 W1~W4 周次推进里程碑与跨模块联调作战表

| 周次与时间窗口 | NXY (`M2-T04` 遥测与硬件 QoS) 核心动作 | WXH (`M2-T05` 调度面总控与消融) 核心动作 | 跨模块联调门禁与里程碑交付物 |
| :---: | :--- | :--- | :--- |
| **W1**<br>*(11.02 ~ 11.08)* | 完成 `perf_event_open mmap` / `/dev/mem` MMIO 直读与网卡 DMA Auto-Dump 改造，接入 IO Die (`smmuv3`/`hisi_hha`) 与 PCIe RC PMU，跑通单次采集 **$< 2\mu\text{s}$**。 | **【11.06 外援门禁】** 督导验收 `HoloTestbed-4X` 实机基线（导出首份 E0 报表，若未过立即切 Plan B）；完成 `MountedSegment` 内嵌 $64\text{B}$ 矩阵与双桶 Epoch 原子预扣。 | **M2-W1 门禁**：Testbed 实机跑通（$\text{CV} < 5\%$）；$64\text{B}$ 头文件契约冻结。 |
| **W2**<br>*(11.09 ~ 11.15)* | 打通 $500\mu\text{s}$ 单边 RDMA Write Inline 上报与中心表构建闭环；完成 MPAM `PARTID_FG/BG` 划分（纳入 YY 压缩线程）。 | 对接 NXY 实时矩阵，实装感知压缩率与 Slab 碎片度的 `<2µs` `CostEvaluator`（含 $K^*$ 解算）；完成纵向跨介质状态机与快慢队列隔离代码。 | **M2-W2 门禁**：`3GB/s` 沙箱跑通 $100\%$ 自动回退重算（$0\%$ 退化）；完成向 LJQ 下发升降级指令接口联调。 |
| **W3**<br>*(11.16 ~ 11.22)* | 联调 $200\mu\text{s}$ 自适应 WQE 微切片（协同 CY）与 $20\text{ms}$ MPAM 动态配额，在 Testbed 上完成 $100\text{Gbps}$ 后台满载混压测试。 | 作为调度面总集成者，联调 NXY、LJQ、LDY、WZX、YY，在 Testbed 上完成 SWE-bench $400$ 并发微突发防踩踏与 $1.8\times$ 超显存升降级压测。 | **M2-W3 门禁**：$100\text{G}$ 混压下前台 $\text{TPOT}$ 干扰 $< 2.0\%$；微突发 $\text{P99 TTFT}$ 降 $> 60\%$。 |
| **W4**<br>*(11.23 ~ 11.29)* | 冻结全链路 `CapabilityMatrix` 精度对账数据，输出 E3 阶段 $100\text{G}$ 混压 QoS 门禁验收报告与调优参数手册。 | 主持 **`W3-T06` 全链路八级递进消融复验（Level 0 ~ Level 7）**，执行奥卡姆剃刀剪枝，汇总全局业务指标提升证据包。 | **M2 终验交付**：统一调度底座主干代码、八级消融对账总表、生产级软硬协同调优指南。 |

---

## 第五部分：三方领域专家严苛评审意见与终稿闭环修订对账单 (Expert Audit & Resolution Ledger)

在本文档定稿交付前，已引入 **AI 系统与推理调度专家**、**微架构与数据压缩优化专家**、**高性能网络与硬件 QoS 专家** 对初稿进行了严苛审阅。专家提出的 $6$ 项核心质询已全部在本文档第 1~4 部分中完成数学建模与工程闭环修复，具体修订对账如下：

| 评审专家角色 | 专家严苛审核意见与质询痛点 (Harsh Critique) | 终稿闭环修复方案与正文落位章节 (Resolution & Section Anchor) |
| :--- | :--- | :--- |
| **专家一：<br>AI 系统与推理调度专家** | **【质询 1.1：常规健康网络下的业务价值断层】**<br>“如果 WXH 的 `<2µs` 选路仅仅在 $3\text{GB/s}$ 极端拥塞时做熔断回退，那么在生产环境 $95\%$ 的健康/微波动时间内，HOST 侧调度的业务吞吐增益从何而来？” | **已闭环修复（见 1.2 节路径一/二 & 3.2 节）**：<br>1. 在常规高并发下，通过 Step 1 原子预扣 + 4-SCCL 多通道负载均衡消除微突发 Incast，将 $M^{[X]}/G/1$ 排队拐点右移，使稳态有效吞吐（Goodput）提升 **$12\% \sim 18\%$**；<br>2. 在 $8 \sim 20\text{GB/s}$ 波动区间引入 $K^*$ 动态按层切分（前 $K^*$ 层重算掩盖后 $80-K^*$ 层拉取），使非极端拥塞下 TTFT 依然降低 **$15\% \sim 25\%$**。 |
| **专家一：<br>AI 系统与推理调度专家** | **【质询 1.2：SSD 慢速升迁阻塞推理主循环风险】**<br>“在 SWE-bench 中，若 Agent 提前结束工具调用返回，而其 KVC 仍在 SSD 中或正在升迁途中，vLLM `Scheduler.schedule()` 若同步等待 SSD I/O，会导致同 Batch 内所有正在 Decode 的请求 TPOT 暴增 $>50\text{ms}$！” | **已闭环修复（见 1.2 节路径三 & 3.3 节 (2) & 3.4 节）**：<br>引入 `READY_FAST_QUEUE` 与 `WARMING_SLOW_QUEUE` 快慢双队列物理隔离（借鉴 `FlatCake FC-MAS`）。命中 SSD 时仅下发非阻塞 `AsyncPromote` 并将请求挂入慢队列，不分配 HBM 亦不阻塞当前 Decode 批次，严格保证调度主循环临界区 **$< 100\mu\text{s}$**。 |
| **专家二：<br>微架构、数据压缩与软件优化专家** | **【质询 2.1：跨特性耦合失配（压缩/Slab 碎片 vs 选路/MPAM）】**<br>“11月 YY 引入了 SIMD 位平面压缩（体积减半但耗 CPU 与内存带宽），WZX 引入了双池 Slab 分配。若 WXH 的 `CostEvaluator` 仍用静态体积和静态描述符开销，盈亏门限将产生 $50\%$ 偏差！且 YY 的压缩线程会抢占前台 Decode 的 DDRC 带宽！” | **已闭环修复（见 2.4 节 (2) & 3.2 节 (2)）**：<br>1. 升级 `CostEvaluator` 闭式方程，显式引入 `MountedSegment` 中的动态压缩比 $\gamma_{\text{comp}}$、SIMD 解压耗时项与 WZX 实际 Slab 描述符数 $N_{\text{slab\_desc}}$；<br>2. 在 `02_hardware_isolation.sh` 中将 YY 的 SIMD 压缩线程池统一绑定至后台 MPAM `PARTID_BG` 控制组，受 `MBW_MAX` 硬件配额刚性约束，彻底隔离对前台 Decode 的干扰。 |
| **专家二：<br>微架构、数据压缩与软件优化专家** | **【质询 2.2：ARM Uncore PMU 用户态直读的内核陷阱隐患】**<br>“Linux 内核下的 ARM Uncore PMU（如 `hisi_sccl_ddrc`）在部分内核版本中并未实现用户态 `cap_user_rdpmc=1`，盲目用 `perf_event_open mmap` 会在运行时退化为系统调用或读出全 0！” | **已闭环修复（见 2.3 节 & 2.5 节）**：<br>在 `KunpengUncorePmuReader` 中设计双保险机制：初始化时检查 `cap_user_rdpmc`；若不支持用户态指令直读，立即自动切换为通过 `/dev/mem` (`O_RDONLY`) 直接映射鲲鹏 Uncore PMU 的物理 MMIO 计数寄存器页至用户空间，确保 $100\%$ 零系统调用、单次读取 **$< 150\text{ns}$**。 |
| **专家三：<br>高性能网络与硬件 QoS 专家** | **【质询 3.1：原子预扣与硬件探针的『双重扣减（Double-Counting）』陷阱】**<br>“当请求预扣了 `inflight_bytes` 并开始传输后，NXY 下一个 $500\mu\text{s}$ 周期的硬件探针已经将该流量体现为 $BW_{\text{avail}}$ 的下降。如果软件继续扣除该请求的 `inflight_bytes` 直到传输结束，就会把同一份拥塞扣减两次，导致严重的误回退！” | **已闭环修复（见 3.2 节 (1) & 3.4 节）**：<br>设计 **双桶 Epoch 滑动窗口预扣器（`EpochAwareReservationSlab`）**：按遥测周期 `telemetry_epoch % 2` 维护两个原子桶，`pending_bytes` 仅统计当前 $500\mu\text{s}$ 盲区内新下发的流量；一旦下一拍硬件遥测矩阵到达，立即将旧 Epoch 桶原子清零，彻底根除跨周期双重扣减。 |
| **专家三：<br>高性能网络与硬件 QoS 专家** | **【质询 3.2：$200\mu\text{s}$ WQE 微切片与 CY 单通道顶速平顶目标的冲突】**<br>“NXY 将后台 WQE 切碎以防 PCIe RC 阻塞，而 CY 需要大包 Bulk I/O 来消除传输锯齿并跑满 $38\text{GB/s}+$。若全天候切碎为 $64\text{KB}$，会拖累 CY 的多通道峰值带宽！” | **已闭环修复（见 2.4 节 (1) & 2.5 节）**：<br>实装 **前台状态感知的自适应切片与批量门铃**：当 `fg_decode_active_bitmap == 1` 时采用 **$256\text{KB}$ 黄金切片**（单包占 PCIe RC 仅 $6.4\mu\text{s}$，完全消除队头阻塞，且通过单次 `ibv_post_send` 链表批量下发 + $16:1$ 信号汇聚保证 $>95\%$ 线速）；当无前台 Decode 时自动恢复为 $2\text{MB}$ 大包。 |
