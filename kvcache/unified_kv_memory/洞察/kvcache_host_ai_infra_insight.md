# KVCache 正在重塑 LLM 推理 AI Infra：从 GPU Local Cache 到数据中心级 State/Data-Movement Resource

> **用途**：本文件是后续 Agent 生成“一页技术洞察 PPTX”的证据底稿，而不是 PPT 文稿的一比一复刻。  
> **版本日期**：2026-09-15  
> **范围**：2026 年 OSDI / NSDI / SIGCOMM / SIGMOD / FAST 相关论文 + 2026 年 AMD / Intel / NVIDIA / Arm 官方产品与技术发布。  
> **核心问题**：随着 Agentic AI、超长上下文、高并发和高 KV 复用成为重要推理负载，KVCache 在 AI Infra 中的角色如何变化；这一变化如何重塑硬件布局、软件架构、业务调度和部署软件栈；又如何形成 HOST 侧、特别是 CPU 架构演进的外部驱动力。

---

## 0. 证据使用规则：后续 PPT 必须遵守

本报告把结论分成三类，后续 Agent 不应混用。

1. **[论文事实]**：论文原文中明确给出的 workload 特征、架构设计、实验数字或 Figure。可以直接用于 PPT，但必须保留论文名、会议、Figure 编号和基线条件。
2. **[厂商事实]**：AMD / Intel / NVIDIA / Arm 官方产品页、新闻稿或技术博客中的公开规格、产品定位或厂商自行给出的 benchmark 结论。厂商 benchmark 必须写成“AMD/NVIDIA/Intel 官方称/官方测试”，不能表述成第三方独立结论。
3. **[综合推断]**：根据多篇论文与厂商路线共同推导出的系统趋势，例如“LLM Scheduler 正从 GPU Scheduler 向 Compute + State + Data-Movement Scheduler 演进”。这类句子不是任何单篇论文的原话，必须明确标注为“综合判断/洞察”。

**重要边界**：本文并不主张“所有 LLM 推理都会由 KV 搬运主导”。论文证据显示，该趋势最强地出现在 **长上下文、高 KV reuse/offload、高并发、PD/存储解耦或多级缓存** 场景；短上下文、低缓存命中、低并发场景仍可能由 GPU compute 主导。Strata 也明确指出其设计面向 long-context，而 short-context 下与其他系统表现相当。[P2]

---

# 1. Executive Insight：本轮技术洞察的核心结论

## 1.1 从负载形态看：Agentic AI 正把“计算请求”变成“长期状态 + 小增量计算”

**[论文事实]** SIGCOMM'26 DualPath 对代表性 Agentic workload 的分析显示，agent 会在几十乃至上百轮中持续继承历史上下文；其一组 trace 的平均轮数为 **157**，平均上下文约 **32.7K token**，每轮平均只追加 **429 token**，由此得到约 **98.7% KV-Cache hit rate**。论文还指出，多轮、短 append 模式下 KV hit rate 典型可达到 95% 以上。[P1]

这意味着在线推理请求越来越不能只被理解成：

`Prompt -> GPU Compute -> Output`

而更接近：

`Existing State(KV) + Small Delta -> Compute -> New State`

**[综合推断]** 当历史状态远大于每轮新增输入时，请求的“数据状态位置”开始影响端到端性能；调度器不能只知道 GPU 是否空闲，还需要知道对应 KV 是否存在、在哪里、何时能够到达执行节点。[P1][P4][P5]

---

## 1.2 从硬件趋势看：GPU Compute 的增长速度显著快于传统 Host-GPU I/O 与 HBM Capacity

**[论文事实]** DualPath Figure 3 汇总 NVIDIA Ampere 到 Blackwell 的硬件趋势：论文给出的归一化结果为 **GPU Compute 28.8×、PCIe bandwidth 2.0×、GPU memory capacity 2.4×**，并据此指出 I/O-to-compute ratio 下降约 **14.4×**。[P1-F3]

**[综合推断]** 当 GPU compute 被更快架构、FlashAttention、量化、并行调度持续压缩时，同一批 KV bytes 的搬运时间不会按相同比例下降；因此，在高复用/长上下文场景，数据搬运更容易暴露为 critical path。这不是说 GPU compute 不再重要，而是说明 **Compute 与 Data Movement 的增长速率不匹配** 正成为新的系统约束。[P1][P2][P6]

---

## 1.3 从 KVCache 角色看：它正在经历四阶段演变

### Stage 1 — GPU-local execution cache

KVCache 主要常驻 GPU HBM，用于避免 attention 对历史 K/V 的重复计算。此阶段的关键问题是 HBM capacity 与 GPU-local block management。[P3]

### Stage 2 — Hierarchical KV cache

KV 开始在 **HBM / Host DRAM / SSD** 间分层。Strata、Bidaw、KVDrive 均证明，仅靠“把 KV 放到更大但更慢的 tier”并不能自动提升性能；KV loading latency、I/O stall、placement 与 request scheduling 必须协同。[P2][P5][P10]

### Stage 3 — Disaggregated / shared state

KV 逐渐脱离某一 GPU 或某一 serving instance 的生命周期，通过远端 memory、CXL memory pool、shared KV tier 等方式成为跨实例可复用状态。SYMPHONY 通过 compute-memory disaggregation 缓解 stateful session 的负载不均；Beluga 使用 CXL memory pool；2026-09 的 CXL/Kubernetes 预印本进一步把 composable CXL memory 暴露为 Kubernetes 可调度资源。[P4][P8][E1]

### Stage 4 — Schedulable state and data movement

DualPath 让 storage I/O path 可以动态选择；TurboBus 把 PCIe 带宽池化；KVServe 根据 workload/bandwidth/SLO 动态决定 KV compression；Libra 利用 chunked KV transfer 让 request 可按 micro-request 跨实例执行；MemChannel 则表明 switched CXL memory 也需要 admission/rate-control/QoS。[P1][P6][P7][P9][P11]

**[综合判断]** KVCache 的角色正在从“GPU memory 中的中间状态”升级为 **跨 Memory Tier、Network、Storage 和 Serving Instance 流动的数据中心级 State Resource**；与之对应，KV 的 location、ready time、transfer path、bandwidth 和 lifecycle 都开始成为可调度变量。[P1][P2][P4][P6][P7][P8][P9]

---

# 2. 为什么超长上下文 + 高并发会改变推理优化对象

## 2.1 HBM 容量墙首先迫使 KV 外溢，随后把瓶颈转化为 Data Movement

**[论文事实]** Strata 的 long-context 实验显示，在 SGLang 将 KV offload 到 CPU memory 的基线中，服务 LooGLE 时 **74% 的 prefill time 被 KV transfer 阻塞**，并导致最高约 **4× throughput reduction**。Strata 最终通过 GPU-assisted I/O + cache-aware scheduling，在不同模型/数据集上相对 hierarchical caching baselines 得到最高 **5× throughput at the same TTFT**。[P2]

**[论文事实]** KVDrive 同样指出，随着 context length 和 batch size 增长，KV transfer volume 会急剧上升并成为 decode latency 的主导来源；其系统跨 GPU memory、Host DRAM 与 SSD 联合编排 placement、pipeline scheduling 和 cross-tier coordination，报告最高 **1.74× throughput** 改善。[P10]

**[综合判断]** 因此，“扩大 KV capacity”只是第一步；当 working set 被扩展到 Host/SSD/remote memory 后，系统问题从 `capacity wall` 转化为 `movement + queueing + scheduling wall`。[P2][P5][P10]

---

## 2.2 高并发使 nominal bandwidth 不再等于 effective bandwidth

低并发下可以粗略写成：

`T_restore ≈ KV_size / effective_bandwidth`

高并发下则必须考虑：

`T_restore = lookup + queueing + transfer + attach/decompress - overlap`

其中 queueing 来自 DRAM、PCIe/CXL、NIC/RDMA、SSD 等共享资源上的并发流。

> **注意**：上式是本报告的分析框架，不是任何单篇论文中的原始公式。

**[论文事实]** MemChannel 在 switched CXL memory pooling 中实测并归纳了三类问题：**intra-host contention、in-fabric congestion、unmanaged host-to-remote-DIMM interaction**，因此引入 host-side admission、rate control、cross-host bookkeeping 和 congestion feedback。[P9]

**[论文事实]** TurboBus 观察到 GPU memory offload 场景中，单独 PCIe link 已成为瓶颈时，系统 aggregate PCIe bandwidth 仍可能有 **超过 60% idle**；其通过 scale-up fabric 借用其他 GPU 的空闲 PCIe path，KV-cache-offloaded inference throughput 最高 **1.6×**。[P6]

**[综合判断]** 高并发下真正需要调度的不是“总带宽”这一单值，而是 **path-specific bandwidth、queue depth、contention 和 priority**。[P6][P9]

---

# 3. KVCache 正在重塑 AI Infra 的四个层面

## 3.1 硬件布局：从 GPU + Host DRAM 走向多级 AI Memory/Data Fabric

传统结构近似为：

`GPU HBM <-> PCIe <-> Host DRAM`

论文与厂商路线共同显示，新的结构正在扩展为：

`GPU HBM <-> coherent/PCIe fabric <-> Host DRAM <-> CXL/Remote Memory/SSD`

并且 NIC/RDMA、scale-up fabric 也会参与 KV movement。[P1][P3][P6][P8]

### DirectKV：Host DRAM 从“backing store”变为 GPU 可直接访问的 KV tier

**[论文事实]** OSDI'26 DirectKV 利用 GH200 的 NVLink-C2C，使 GPU kernel 直接访问 CPU-resident KV，而不是先 copy 到 GPU staging buffer。论文在 GH200 上报告 **CPU-GPU transfer volume 最高降低 50%、GPU memory usage 降低 43%、E2E performance 最高提升 1.2×**。[P3]

**[洞察]** DirectKV 的意义不是简单“zero-copy 更快”，而是表明在足够高带宽/一致性的 CPU-GPU interconnect 下，**Host DRAM 可以进入 GPU execution-visible memory hierarchy**。[P3]

### Beluga：从 Host DRAM 进一步走向 CXL shared KV pool

**[论文事实]** SIGMOD'26 Beluga 基于 commercial CXL 2.0 switch 构建 shared memory pool，并将 Beluga-KVCache 集成到 vLLM。当前可访问论文版本报告，相对 MoonCake 的 RDMA-based solution，LLM inference throughput 最高提升 **4.79×**；在 dense KV transfer 实验中，直接 CXL path 相对 MoonCake 的 host bounce-buffer path 将 write/read latency 分别降低 **36.2% / 38.7%**。[P8]

**[重要修正]** 本报告不采用此前讨论过的“7.35×”数字，因为当前可公开核验版本明确给出的端到端最高吞吐结论是 **4.79× over MoonCake**。[P8]

### 新近趋势：CXL memory 从硬件池走向 Kubernetes schedulable resource

**[预印本事实，非顶会同等证据等级]** 2026-09-09 发布的 *Composable CXL Memory as a Kubernetes-Native Shared Memory for LLM Serving* 使用 Kubernetes DRA 把 composable CXL memory 暴露为 schedulable cluster resource，并由 vLLM/llm-d connector 把共享 CXL region 用作 KV tier。在两节点、512GiB CXL appliance、Qwen2.5-7B-Instruct 的 feasibility study 中，论文报告 cross-node prefix reuse 相对 recompute 将 TTFT 降低 **5.5×–36.6×**，external hit rate 95.4–99.5%，且作者明确说明该工作是 **feasibility study rather than performance evaluation**。[E1]

**[综合判断]** 硬件演进并非简单“给 GPU 增加更大内存”，而是在形成 **HBM—Host DRAM—CXL/Remote—SSD** 的 AI state hierarchy，并逐步被操作系统/容器编排器/serving runtime 共同管理。[P3][P8][E1]

---

## 3.2 软件架构：KV location / ready time 必须成为 runtime 的一等状态

**[论文事实]** Bidaw 指出，compute engine 与 two-tier storage “mutually unaware” 会导致 I/O-induced request blocking；其 Figure 9 明确设计了双向信息流：compute scheduler 感知 `KV location & size`，storage 侧感知 `future access timing`。Bidaw 最终报告 response latency 最高降低 **3.58×**、throughput 最高提升 **1.83×**。[P5]

**[论文事实]** Strata Figure 9 的 ablation 显示，I/O optimization 与 scheduling optimization 分别可以改进 hierarchical-cache baseline，而完整 Strata 同时利用两者。论文正文给出：`Strata-Schedule-Only` 与 `Strata-IO` 分别最高达到约 **1.8×** 与 **2.3×** peak-throughput improvement（相对其构建的 baseline）。[P2-F9]

**[综合判断]** 一个只维护 `free GPU KV blocks` 的 scheduler 已不足以表达多级 KV 系统。未来 serving runtime 至少需要感知：`prefix/hash、KV size、location、layout、ready-time、expected reuse、target GPU、path congestion、SLO`。[P2][P4][P5][P7]

---

## 3.3 业务调度：从 Load-aware Routing 走向 State-aware / SLO-aware Routing

**[论文事实]** NSDI'26 SYMPHONY 研究 multi-turn stateful LLM serving：其 Figure 1 在 8 GPU、1024 concurrent users 的实验中显示最忙与最空 GPU 的 outstanding request 差异可达到 **300 requests**。SYMPHONY 通过 compute-memory disaggregation、advisory prefetch、priority-based KV management 和 cooperative memory management，将平均 E2E latency 相对 vLLM 降低 **2.4×**，并可在平均 latency 仅增加 1.3% 的情况下服务 **4× more requests**。[P4]

**[论文事实]** NSDI'26 Libra 把 request 拆成 token-boundary micro-requests，由 global/local scheduler 做 SLO-aware placement，并用 chunked KV transfer 支持跨实例执行。论文在 real-world traces 上报告 goodput 最高 **1.91× / 1.61×**（分别相对论文定义的 colocated/disaggregated baselines），serving capacity 提升 **1.15×–3.07×**。[P11]

**[综合判断]** 调度对象正在从：

`Request + GPU load`

向：

`Request + KV State + Compute + Data Path + SLO`

演进。这里的核心不是盲目追求 cache hit，而是判断 cache hit 是否能在 SLO 内转化为有效收益。[P2][P4][P5][P11]

可以用下式作为内部分析框架：

`UsableHit = Hit AND (T_restore < T_recompute) AND SLO_feasible`

> 该公式是本报告的分析抽象，不是论文原式。

---

## 3.4 数据通路：从固定 I/O 拓扑走向 path/bandwidth/compression 可调度

### DualPath：I/O path 成为调度资源

**[论文事实]** 在典型 PD-disaggregated serving 中，Prefill Engine 从 storage 加载命中的 KV，而 Decode Engine 的 storage NIC 可能闲置。DualPath 增加 `storage -> decode -> RDMA compute network -> prefill` 第二条路径，并由 global scheduler 动态分配两条路径流量。论文在代表性 Agentic workloads 上报告 offline throughput 最高 **1.87×**、online serving throughput 平均 **1.96×**，同时满足其设定 SLO。[P1]

### TurboBus：PCIe link 的静态所有权被打破

**[论文事实]** TurboBus Figure 1 展示：offload workload 可经 scale-up fabric 使用邻居 GPU 的空闲 PCIe links，而不再被自身单一 PCIe link 限制。论文报告 model loading TTFT 最高降低 **40%**，KV-cache-offloaded inference throughput 最高 **1.6×**，同时被借用 link 的 colocated workload overhead <1%。[P6]

### KVServe：连“搬多少 bytes”也开始成为在线调度变量

**[论文事实]** SIGCOMM'26 KVServe Figure 1 显示，其 PD-separated 实验中 KV communication 可占总 latency 的 **60%**。论文指出 Llama-3.1-70B 在 128K tokens 可产生约 **39.06GB KV**（该数值来自论文引用的模型分析）。KVServe 根据 workload mix、bandwidth 和 SLO/quality budget 动态选择 compression profile；论文报告在其测试条件下 PD-separated serving JCT 最高改善 **9.13×**，KV-disaggregated prefix caching TTFT 最高改善 **32.8×**。[P7]

**[限定条件]** KVServe 的 9.13× / 32.8× 是特定 hardware/network bandwidth、workload、accuracy/SLO constraint 与 baseline 下的结果，不应与 Strata、DualPath 等论文的倍数直接横向比较。[P7]

**[综合判断]** 未来 data-movement scheduler 的决策空间会从 `Move / Don't Move` 扩展为：`which path / when / chunk size / priority / compression level / prefetch / recompute`。[P1][P6][P7][P9]

---

# 4. HOST 在 KV-centric AI Infra 中承担的角色变化

## 4.1 不是让 CPU 做更多 memcpy，而是让 HOST 成为控制面

**[综合设计原则]** 上述论文共同支持一种系统分工：大块 KV byte movement 应尽量由 GPU DMA、NVLink-C2C、PCIe DMA、RDMA、CXL load/store、storage DMA 等数据面完成；CPU 的高价值职责是 **decision、scheduling、coordination、metadata/control**。DirectKV、DualPath、TurboBus、Beluga 都在减少 CPU/Host bounce、借用硬件 fabric 或优化 path，而不是依赖 CPU core 做 bulk copy。[P1][P3][P6][P8]

因此可将未来 HOST 的职责拆成三类控制面。

### A. State Control Plane

负责全局或分层维护：`prefix/hash、location、size、layout、owner、ready-time、hotness、reuse probability、refcount/lease、TTL、tenant/consistency`。

- Strata：hierarchical cache + cache-aware scheduling。[P2]
- SYMPHONY：stateful session + prefetch/advisory requests。[P4]
- Bidaw：KV location/size 与 future access timing 双向传递。[P5]
- Beluga：shared CXL KV pool。[P8]

### B. Data-Movement Control Plane

负责 `path selection、traffic class、prefetch timing、compression、chunk/pipeline、bandwidth admission/reservation、backpressure`。

- DualPath：双路径 KV read + global scheduler。[P1]
- TurboBus：PCIe bandwidth pooling。[P6]
- KVServe：service-aware compression。[P7]
- MemChannel：CXL transport/QoS。[P9]

### C. SLO / Resource Control Plane

把 TTFT、TPOT、deadline、priority、tenant policy 与 GPU queue、KV ready-time、memory/fabric pressure 联合起来做 routing、batching、admission、restore/recompute 决策。[P2][P4][P5][P11]

**[综合判断]** 未来 HOST 的价值不只是“运行 vLLM Python runtime 并发 kernel”，而是作为 **State + Memory + Data-Movement Resource Orchestrator**，使 GPU compute、memory tier 与 I/O path 能够在同一个 SLO 闭环中被联合调度。[P1][P2][P4][P5][P6][P7][P9]

---

# 5. CPU 架构演进：KV/Agentic AI 带来的外部驱动力

> **重要限定**：以下不能表述成“KVCache 单独导致 AMD/Intel/NVIDIA/Arm CPU 改版”。更严谨的说法是：**Agentic AI、长上下文、state/data movement、tool execution、retrieval 与 accelerator orchestration 等工作负载共同形成新的 AI Host 需求；KVCache 是其中最典型、最数据密集且最能暴露 Memory/I/O 瓶颈的 workload 之一。** NVIDIA 已在 Vera 官方页面中直接把 “KV cache management” 列入 CPU workload；AMD/Intel/Arm 官方则从 AI Host/Agentic orchestration、memory bandwidth、accelerator I/O 等角度给出产业侧印证。[V1][V3][V5][V6]

## 5.1 驱动力一：Loaded single-thread / per-core latency 重新变得关键

**[厂商事实]** AMD EPYC 9006 官方把 CPU workload 分为 Agent Sandbox、**AI Host Node**、General-Purpose 三类，并称 AI Host Node 需要 **high-frequency、fast CPU-to-GPU communication、memory bandwidth**；AMD FAQ 明确表示 high-frequency cores 用于 latency-sensitive orchestration 与 AI host-node tasks，而高 core density 用于大规模 agent execution。[V1]

**[厂商事实]** NVIDIA Vera 技术博客公开 Olympus core 的 neural branch predictor、10-wide decode front end、deep out-of-order execution、Spatial Multithreading；NVIDIA 称这些设计面向 control-heavy / latency-sensitive agentic workload，并在其自有比较中称 peak loaded latency 相对 x86 CPU 降低 40%。该 40% 是 NVIDIA vendor benchmark，应按厂商数据引用。[V5]

**[综合推断]** KV state lookup、prefix/hash/index、scheduler queue、placement/path decision、runtime/tool control 等往往位于串行 critical path，难以仅靠堆 core 数降低单请求 latency，因此 `loaded per-core/ST performance` 会与 core density 同时重要。[P4][P5][V1][V5]

---

## 5.2 驱动力二：Memory bandwidth 与 bandwidth-per-core 成为 AI Host 核心指标

**[厂商事实]** AMD EPYC 9006 支持最多 **16 memory channels、12.8 GT/s MRDIMM**；9006X 官方博客给出最高 **1.6 TB/s memory bandwidth**，并提供最高 3× L3 cache per core（对比 comparable 9006 SP7 SKU）。[V1][V2]

**[厂商事实]** Intel Diamond Rapids 在 Hot Chips 2026 官方资料中给出 **16 memory channels @ 12800 MT/s**，并将新架构描述为 `adaptable compute building blocks + unified memory fabric + flexible I/O`。[V3]

**[厂商事实]** NVIDIA Vera 官方给出最高 **1.2 TB/s LPDDR5X bandwidth**、最高 1.5TB capacity，并明确称其服务于 efficient KV-cache management 和 data-intensive agentic workflows。[V4]

**[综合推断]** 当 Host DRAM 成为 KV warm tier、CXL/remote data 的 staging/coordination point，且 CPU 同时处理 metadata、retrieval、serialization、agent runtime 时，CPU memory subsystem 的 sustainable bandwidth 和 bandwidth-per-core 会影响 accelerator feeding 和 loaded latency。[P2][P3][P8][V1][V4]

---

## 5.3 驱动力三：LLC / unified cache / on-die fabric 更重要，但目标不是缓存整个 KV payload

**[厂商事实]** AMD 9006X 官方称 3D V-Cache 可提供最高 **3× L3 cache per core**；Intel Diamond Rapids 公布最多 **1.28 GB LLC**；NVIDIA Vera 使用 unified cache + SCF，并给出 **3.4 TB/s bisectional bandwidth**。[V2][V3][V4]

**[综合推断]** 大 LLC 对 KV-centric host 的主要意义不应表述成“把几十 GB KV 放进 CPU cache”，而是提高 **prefix/hash table、block metadata、routing/scheduler queues、descriptors、agent/runtime working set** 等控制面数据的局部性，并降低高并发下访问共享 metadata 的 latency。这一用途属于系统推导，并非厂商直接声明。[P2][P4][P5][V2][V3][V4]

---

## 5.4 驱动力四：CPU-GPU / PCIe / CXL / coherent fabric 从“外设连接”进入 CPU 产品定义中心

**[厂商事实]** AMD EPYC 9006 支持 PCIe Gen6；专门定位 AI Host Node 的 EPYC 9006 LP 官方博客给出 **112 Gb/s xGMI CPU-to-GPU connectivity**、最高 5GHz、LPDDR5X support。[V1][V2]

**[厂商事实]** Intel Diamond Rapids 提供 **128 lanes PCIe Gen6 + CXL 3.0**，并把 flexible I/O 与 unified memory fabric 列为架构组成。[V3]

**[厂商事实]** NVIDIA Vera 官方给出 CPU-GPU **1.8 TB/s coherent NVLink-C2C bandwidth**，并明确提到 unified memory architecture 与 KV-cache offload。[V4]

**[厂商事实]** Arm Neoverse CSS N4 官方发布给出最多 128 cores/die、LPDDR6、PCIe Gen7，并称 agentic AI 正增加 data movement、accelerator orchestration 与 concurrent compute 对高带宽 memory/I/O 的需求。[V6]

**[综合判断]** 这些路线与 DirectKV / TurboBus / Beluga / DualPath 的研究问题高度吻合：AI Host CPU 的 I/O die、coherent interconnect、root complex、memory fabric 正从“外围能力”转向影响 accelerator feeding、state movement 与 rack-scale memory 的系统性能要素。[P1][P3][P6][P8][V1][V3][V4][V6]

---

## 5.5 驱动力五：高 core/thread density 仍然重要，但使用场景从 VM density 扩展到 Agent/Infra concurrency

**[厂商事实]** AMD EPYC 9006 官方给出最高 **256 cores / 512 threads**，并明确把 high-density cores 与 large-scale parallel agent execution 关联；Intel Diamond Rapids 公布最多 **256 cores**。[V1][V3]

**[厂商事实]** Arm 官方称 Agentic AI 会增加 CPU-based computation，包括 reasoning、retrieval、tool calls、database 和 agent-to-agent interactions；同时指出不同工作负载需要不同取舍：scale-out/data-plane 更偏 throughput efficiency，而 agentic/performance-critical workload 更要求 responsive CPU。[V6]

**[综合判断]** AI Host 的 CPU 需求不是“单核性能 or 核心数”二选一，而是 `loaded per-core performance × concurrency`；前者服务 latency-sensitive orchestration/control path，后者服务 agent sandbox、runtime、retrieval、network/storage services 等并发工作。[V1][V5][V6]

---

## 5.6 驱动力六：Tail latency、QoS、resource isolation 将变成新的架构关注点

**[论文事实]** MemChannel 证明 shared CXL memory path 会出现 host/fabric contention，因此需要 end-to-end bandwidth control、host admission 与 congestion feedback。[P9]

**[厂商事实]** NVIDIA Vera 技术博客强调其 unified fabric / monolithic die 用于降低 loaded tail latency，并给出 vendor benchmark：peak loaded latency 相对所选 x86 baseline 低 40%。[V5]

**[综合判断]** 对在线 inference，峰值 bandwidth 之外还需要关注 `loaded P99/P99.9 latency、bandwidth isolation、priority traffic、NUMA/fabric contention`，因为 demand KV restore 与 background prefetch/publish/migration 共享 Memory/I/O resource。[P1][P9][V5]

---

# 6. 面向未来的 HOST 系统能力：Host-Centric KV Orchestrator（建议方案）

> 本节是基于上述论文证据提出的**系统方案建议**，不是某一篇论文已有架构。

## 6.1 目标

把当前分散在 inference engine、KV connector、storage layer、network path、cluster router 中的状态与策略统一到一个可观测、可闭环的 HOST orchestration layer，使调度对象从：

`Request + GPU`

升级为：

`Request + KV State + Compute + Memory Tier + Data Path + SLO`

## 6.2 六个模块

### 1. Global KV State Manager

维护 `prefix/hash -> location/size/layout/ready-time/hotness/refcount/tenant/lease`；支持本地快速 index + 全局 metadata/discovery。[P2][P4][P5][P8]

### 2. KV Cost Planner

对每次命中计算：`restore / prefetch / migrate / recompute / compress` 哪个方案的 SLO cost 最低。

分析模型可写为：

`T_restore = T_lookup + T_queue + T_transfer + T_attach/decompress - T_overlap`

仅当 `T_restore < T_recompute` 且 `SLO feasible` 时，把 hit 计为 “usable hit”。

> 该公式为本报告建议的决策模型，不是论文原公式。

### 3. Placement & Lifecycle Manager

在 HBM / Host DRAM / CXL / remote memory / SSD 间做 promotion、demotion、replication、eviction、prefetch；策略同时考虑 reuse probability、recompute cost、capacity cost、movement cost 与 topology affinity。[P2][P5][P8][P10]

### 4. Data Path Scheduler

在 C2C / PCIe / CXL / RDMA / storage / relay-GPU path 中选择路径，并决定 chunk size、pipeline、compression、priority、bandwidth reservation。[P1][P6][P7][P9]

### 5. HOST Resource Manager / QoS

对 CPU/NUMA/DRAM BW、PCIe、CXL、NIC、SSD、DMA 等资源实施 admission、priority、rate limit、backpressure、tenant isolation；区分 demand restore、decode fetch、prefetch、publish、replication、background migration 等 traffic class。[P9]

### 6. Telemetry & Feedback Loop

观测 `queue depth、actual BW、stall、cache hit、usable hit、restore ETA、GPU idle waiting for KV、P99 TTFT/TPOT`，为 routing/placement/path policy 提供在线反馈。[P1][P2][P4][P5]

---

# 7. 论文量化证据矩阵：后续 PPT 可用数字

> 不同论文模型、硬件、SLO 和 baseline 完全不同；下面的倍数只用于证明“某一瓶颈存在且可被系统优化”，**禁止在 PPT 中按倍数大小排序得出技术优劣结论**。

| 论文 | Venue | 论文直接观察到的问题 | 论文报告的关键效果 | 本次洞察中承担的证据角色 |
|---|---|---|---|---|
| DualPath | SIGCOMM'26 | Agentic short-append/high-reuse；Prefill storage NIC 饱和而 Decode storage NIC 闲置 | offline throughput up to **1.87×**；online avg **1.96×** under stated SLO | KV I/O path 变成全局可调度资源 [P1] |
| Strata | OSDI'26 | CPU-offload baseline 中 **74% prefill time** blocked on KV transfer；up to **4× throughput reduction** | same-TTFT throughput up to **5×** vs selected hierarchical baselines | I/O 与 scheduling 必须联合设计 [P2] |
| DirectKV | OSDI'26 | staging buffer + copy 放大 transfer/HBM 占用 | transfer **-50%**；GPU memory **-43%**；E2E up to **1.2×** | Host DRAM 进入 GPU-visible KV tier [P3] |
| SYMPHONY | NSDI'26 | stateful session stickiness 导致 GPU load imbalance | avg E2E latency **2.4× lower** vs vLLM；**4×** more requests with 1.3% avg latency increase | state-aware routing / compute-memory disaggregation [P4] |
| Bidaw | FAST'26 | compute scheduler 不知道 KV loading cost；storage 不知道 future access | latency reduction up to **3.58×**；throughput up to **1.83×** | compute-storage bidirectional awareness [P5] |
| TurboBus | SIGCOMM'26 | individual PCIe bottleneck but aggregate >**60% PCIe BW idle** | model-load TTFT **-40%**；KV-offload throughput up to **1.6×** | PCIe BW pooling / path ownership解耦 [P6] |
| KVServe | SIGCOMM'26 | disaggregation 把 KV 变成 network/storage payload；某实验 communication up to **60% latency** | JCT up to **9.13×**；prefix-caching TTFT up to **32.8×** under paper conditions | bytes-on-wire/compression 进入调度闭环 [P7] |
| Beluga | SIGMOD'26 | RDMA pool 的 bounce/control path；CXL shared memory alternative | throughput up to **4.79×** vs MoonCake；dense KV write/read latency -**36.2%/-38.7%** | Rack/shared memory semantic 改变 KV pool [P8] |
| MemChannel | NSDI'26 | shared CXL data path 的 host/fabric contention | 论文证明 isolation/scalability/multi-tenancy effectiveness；官方 abstract 未给单一倍数 | Memory fabric 需要 transport/QoS [P9] |
| KVDrive | SIGMOD'26 | context/batch 上升使 KV transfer dominate decode latency | throughput up to **1.74×** | multi-tier placement + scheduling + I/O co-design [P10] |
| Libra | NSDI'26 | prompt/response variability 造成 PD load/SLO conflict | goodput up to **1.91×/1.61×**；capacity **1.15×–3.07×** | KV transfer 支撑 micro-request mobility [P11] |
| AlignedServe | SIGMOD'26 | 同 batch token 因 KV length 不同产生 iteration bubble | decode throughput up to **1.98×**；latency up to **7.4× lower** | KV length 成为 batching dimension；CPU memory 做 in-flight pool [P12] |

---

# 8. 厂商路线矩阵：CPU 架构外部驱动力的产业侧印证

| 厂商 / 2026 路线 | 官方事实 | 与本洞察对应的 CPU 能力 | 证据等级 |
|---|---|---|---|
| AMD EPYC 9006 | 明确划分 **AI Host Node CPU**；最高 256C/512T；最多 16ch 12.8GT/s MRDIMM；PCIe Gen6；AI Host 强调 high-frequency + CPU-GPU communication + memory BW | concurrency、per-core latency、memory BW、accelerator I/O | AMD 官方 [V1] |
| AMD EPYC 9006X / LP | 9006X：up to 3× L3/core、>5GHz、up to 1.6TB/s memory BW；9006 LP：AI Host、112Gb/s xGMI、up to 5GHz、LPDDR5X | cache/locality、BW、CPU-GPU data path | AMD 官方 [V2] |
| Intel Diamond Rapids | enterprise-scale Agentic AI；`adaptable compute + unified memory fabric + flexible I/O`；up to 256 cores、1.28GB LLC、16ch 12800MT/s、128 lanes PCIe Gen6 + CXL3.0 | large LLC、memory fabric、CXL/PCIe I/O、concurrency | Intel 官方 Hot Chips 2026 [V3] |
| NVIDIA Vera | 官方明确列出 **KV cache management** 与 orchestration；1.2TB/s LPDDR5X；1.8TB/s coherent NVLink-C2C；3.4TB/s SCF；88 Olympus cores | KV Host memory、coherent CPU-GPU path、loaded latency、per-core performance | NVIDIA 官方 [V4][V5] |
| Arm AGI CPU / Neoverse CSS N4 | 官方称 agentic AI 增加 CPU-based compute/data movement/accelerator orchestration；CSS N4 up to 128C、LPDDR6、PCIe Gen7、up to 1.75× memory BW vs N3 | responsive CPU + throughput efficiency + high-speed memory/I/O | Arm 官方 [V6] |

**[综合判断]** AMD、Intel、NVIDIA、Arm 2026 路线并不能证明“KVCache 是唯一驱动力”，但共同表明 AI Host CPU 的产品定义正在从单纯 core/socket throughput，扩展到 **per-core/loaded latency、core density、large cache、memory bandwidth、unified/coherent fabric、PCIe/CXL/accelerator connectivity**。这与顶会中 KV state/data movement 暴露出的系统瓶颈高度一致。[P1][P2][P3][P6][P8][V1][V3][V4][V6]

---

# 9. 后续一页 PPT 的证据图索引：必须按 Figure 编号找原图

## 9.1 推荐主组合（最适合本次“HOST / CPU 驱动力”主题）

### 图 A — DualPath Figure 4：KV I/O path 从固定资源变成调度资源

- **论文**：DualPath: Breaking the Storage Bandwidth Bottleneck in Agentic LLM Inference  
- **会议**：SIGCOMM 2026（会议版本标题：DualPath: Accelerating Agentic LLM Inference by Harvesting Disaggregated KV-Cache Storage I/O）  
- **PDF**：https://arxiv.org/pdf/2602.21548  
- **Figure**：**Figure 4 — Dual-path loading illustration**  
- **页码**：PDF page 5/17（论文页码 5）  
- **截图内容**：只裁取 `(a) PE Read Path` 与 `(b) DE Read Path` 两张架构图；去掉 Figure caption 和正文。  
- **PPT 位置**：中间右侧，建议占右侧图片区约 45%–55% 高度，作为主架构图。  
- **支持结论**：Storage NIC / Compute NIC / Prefill / Decode 的逻辑 I/O ownership 可以被 global scheduler 打破；KV movement path 本身成为调度对象。[P1-F4]

### 图 B — Strata Figure 9：I/O optimization 与 scheduling optimization 必须联合

- **论文**：Strata: Hierarchical Context Caching for Long Context Language Model Serving  
- **会议**：OSDI 2026  
- **PDF**：https://www.usenix.org/system/files/osdi26-xie-zhiqiang.pdf  
- **Figure**：**Figure 9 — Breakdown of I/O and scheduling of Strata**  
- **页码**：PDF page 12/17；论文印刷页码 11  
- **截图内容**：裁取 Figure 9 左上 throughput-vs-TTFT plot；删除 caption 与 Figure 10/11。  
- **PPT 位置**：中间右侧下方左半。  
- **支持结论**：只优化 I/O 或只优化 scheduling 均不能达到完整系统的 performance envelope；KV hierarchy 与 request scheduling 需要 co-design。[P2-F9]

### 图 C — DualPath Figure 3（左半）：Compute 与 I/O/Memory 的代际增长失配

- **论文**：DualPath  
- **PDF**：https://arxiv.org/pdf/2602.21548  
- **Figure**：**Figure 3 — Left: Hardware trends of NVIDIA GPUs**  
- **页码**：PDF page 4/17（论文页码 4）  
- **截图内容**：仅裁取 Figure 3 左侧 hardware trend chart；保留 chart 内 `GPU Compute 28.8x / PCIe Bandwidth 2.0x / GPU Memory 2.4x` 标注；去掉右侧 batch-size chart 和 caption。  
- **PPT 位置**：中间右侧下方右半，或作为顶部背景数据的小证据图。  
- **支持结论**：GPU compute 的增长显著快于 host-GPU I/O 与 memory capacity，长期推动 data movement wall 暴露。[P1-F3]

## 9.2 可替换图：如果更强调 Host Memory / CXL

### DirectKV Figure 1

- **PDF**：https://www.usenix.org/system/files/osdi26-luo.pdf  
- **Figure**：**Figure 1 — KV cache offloading via swapping and zero copy**  
- **页码**：PDF page 4/17；USENIX proceedings page 41  
- **截图内容**：左侧 CPU Memory -> GPU L2/SM zero-copy 绿色 path + swap 红色 path；可连同 `PCIe 64GB/s / NVLink-C2C 900GB/s` 标注裁取。  
- **PPT 用途**：替换 DualPath Figure 4，强调“Host DRAM 进入 GPU execution-visible memory hierarchy”。[P3-F1]

### Beluga Figure 1

- **PDF**：https://arxiv.org/pdf/2511.20172  
- **Figure**：**Figure 1 — Overview of RDMA/CXL memory pools**  
- **页码**：PDF page 2/16  
- **截图内容**：只裁取 `(a) RDMA-based MemPool` 与 `(b) Beluga`。  
- **PPT 用途**：强调“KV pool 从 network semantic 进一步探索 memory semantic / CXL shared pool”。[P8-F1]

### Beluga Figure 11 / 12 / 14

- **PDF**：https://arxiv.org/pdf/2511.20172  
- **页码**：PDF page 12/16  
- **Figure 11**：Sensitivity to request arrival rates。  
- **Figure 12**：Sensitivity to input context lengths。  
- **Figure 14**：Data transfers for dense KVCache；论文文字给出 direct CXL path write/read latency 相对 MoonCake -36.2% / -38.7%。  
- **PPT 用途**：若主题要突出 CXL，选 Figure 12 或 14，避免同时堆三张。[P8-F11-14]

## 9.3 可替换图：如果更强调 Network / Bandwidth Pooling

### TurboBus Figure 1

- **PDF**：https://home.cse.ust.hk/~kaichen/papers/turbobus-sigcomm26.pdf  
- **Figure**：**Figure 1 — TurboBus pools PCIe bandwidth for GPU memory offloading**  
- **页码**：PDF page 1/14  
- **截图内容**：CPU/Host Memory + 多 PCIe links + scale-up fabric 前后对比。  
- **PPT 用途**：说明 physical topology 不变但 logical I/O ownership 可池化。[P6-F1]

### KVServe Figure 1 / Figure 12–14

- **PDF**：https://arxiv.org/pdf/2605.13734  
- **Figure 1**：PDF page 1/16，PD-separated serving 的 latency breakdown；communication 部分最高约 60%。  
- **Figure 12**：PDF page 10/16，End-to-End Performance across Hardware and Workloads。  
- **Figure 13**：PDF page 10/16，JCT in PD Separation。  
- **Figure 14**：PDF page 10/16，TTFT in Prefix Caching；图中标注最高 32.8×。  
- **PPT 用途**：Figure 1 用于证明 KV 已成为显式 communication payload；Figure 13/14 用于证明 adaptive compression 在 bandwidth-limited 条件下的收益。[P7-F1][P7-F12-14]

## 9.4 可替换图：如果更强调 Stateful Scheduling

### SYMPHONY Figure 1

- **PDF**：https://www.usenix.org/system/files/nsdi26-agarwal.pdf  
- **Figure**：**Figure 1 — Load imbalance in stateful LLM Serving**  
- **页码**：PDF page 3/16；USENIX proceedings page 2028  
- **截图内容**：outstanding requests 的 Min/Max/Median 时序图。  
- **PPT 用途**：证明 session/KV affinity 会造成跨 GPU load imbalance，State 必须进入 routing/scheduling。[P4-F1]

### Bidaw Figure 9

- **PDF**：https://www.usenix.org/system/files/fast26-hu-shipeng.pdf  
- **Figure**：**Figure 9 — System overview**  
- **页码**：PDF page 6/17；USENIX proceedings page 105  
- **截图内容**：Compute Engine / Two-tier Storage 及 `KV location & size`、`Future access timing` 双向箭头。  
- **PPT 用途**：证明 scheduler 与 storage manager 需要双向信息交换。[P5-F9]

---

# 10. 网页证据索引：后续 Agent 用于厂商动态/CPU 架构背书

## AMD

### [V1] AMD EPYC 9006 Series product page
- URL: https://www.amd.com/en/products/processors/server/epyc/9006-series.html
- 关键位置/内容：
  - `AI Host Node CPU`：high-frequency、fast CPU-to-GPU communication、memory bandwidth 用于提高 accelerator utilization/token generation。
  - up to 256 cores / 512 threads。
  - high memory bandwidth，PCIe Gen6。
  - FAQ：high-frequency 用于 latency-sensitive orchestration/AI host-node，high-density cores 用于 parallel agents。
- PPT 适用位置：中间左下“CPU 架构外部驱动力”或底部总结的厂商证据。

### [V2] Advancing Agentic Workflows With AMD EPYC 9006 Series Server CPUs
- URL: https://www.amd.com/en/blogs/2026/advancing-agentic-workflows-with-amd-epyc-9006-series.html
- 关键位置/内容：
  - EPYC 9006X：up to 3X L3 cache per core、frequencies above 5GHz、up to 1.6TB/s memory bandwidth。
  - EPYC 9006 LP：112Gb/s xGMI CPU-GPU connectivity、up to 5GHz、LPDDR5X；定位 rack-scale AI Host。
- PPT 适用位置：支持 cache / memory BW / accelerator connectivity 正进入 AI Host CPU 产品定义。

## Intel

### [V3] Intel Outlines Architectures for Agentic AI at Hot Chips 2026
- URL: https://www.intel.com/content/www/us/en/newsroom/news/client-computing/intel-outlines-architectures-for-agentic-ai-at-hot-chips-2026.html
- 关键位置/内容：
  - Diamond Rapids：enterprise-scale Agentic AI。
  - `adaptable compute building blocks + unified memory fabric + flexible I/O`。
  - up to 256 cores、1.28GB LLC、16 memory channels @ 12800MT/s、128 lanes PCIe Gen6 + CXL 3.0。
- PPT 适用位置：证明 CPU 架构目标已经同时覆盖 compute、memory fabric 与 I/O，而不是只增加 core count。

## NVIDIA

### [V4] NVIDIA Vera CPU product page
- URL: https://www.nvidia.com/en-us/data-center/vera-cpu/
- 关键位置/内容：
  - 官方明确称 Vera 作为 AI-factory host CPU 执行 ETL、**key-value (KV) cache management** 与 orchestration。
  - up to 1.2TB/s LPDDR5X memory bandwidth，up to 1.5TB capacity。
  - SCF 3.4TB/s bisectional bandwidth + unified cache architecture。
  - NVLink-C2C up to 1.8TB/s coherent CPU-GPU bandwidth，并明确关联 KV-cache offload。
- PPT 适用位置：这是“KVCache 已被 CPU 厂商直接列为 Host CPU workload”的最强官方证据。

### [V5] NVIDIA Vera CPU technical blog
- URL: https://developer.nvidia.com/blog/nvidia-vera-cpu-boosts-ai-factory-throughput-to-accelerate-agentic-workloads/
- 关键位置/内容：
  - neural branch predictor、10-wide decode、deep OoO、Spatial Multithreading。
  - NVIDIA vendor benchmark：peak loaded latency 比其 x86 comparison 低 40%；1.2TB/s memory、14GB/s per core（厂商比较）。
- PPT 适用位置：支持 Agentic/control-heavy workload 正推动 per-core/loaded latency 优化。
- 注意：40% 等比较必须注明 “NVIDIA 官方测试/对比”。

## Arm

### [V6] Arm expands AI infrastructure for the agentic era with AGI CPU and Neoverse CSS N4
- URL: https://newsroom.arm.com/news/arm-agi-cpu-neoverse-css-n4-agentic-ai
- 关键位置/内容：
  - 官方称 agents reason/retrieve/call tools/interact with DB/other agents，增加 CPU-based datacenter compute。
  - scale-out/data-plane 偏 throughput efficiency；agentic/performance-critical 偏 responsive CPU。
  - CSS N4：up to 128 cores/die、LPDDR6、PCIe Gen7、up to 1.75× memory bandwidth vs N3；官方直接关联 data movement、accelerator orchestration、concurrent compute。
- PPT 适用位置：作为 ARM 路线的补充背书。

---

# 11. 论文/网页完整来源索引

## 顶会论文

### [P1] DualPath — SIGCOMM 2026
- Paper: DualPath: Breaking the Storage Bandwidth Bottleneck in Agentic LLM Inference
- Conference version title: DualPath: Accelerating Agentic LLM Inference by Harvesting Disaggregated KV-Cache Storage I/O
- PDF: https://arxiv.org/pdf/2602.21548
- SIGCOMM program: https://conferences.sigcomm.org/sigcomm/2026/program/papers/
- Key figures: Figure 3, Figure 4; workload table/trace around paper p3–4.

### [P2] Strata — OSDI 2026
- Paper: Strata: Hierarchical Context Caching for Long Context Language Model Serving
- Official page: https://www.usenix.org/conference/osdi26/presentation/xie-zhiqiang
- PDF: https://www.usenix.org/system/files/osdi26-xie-zhiqiang.pdf
- Key figures: Figure 1, Figure 8, **Figure 9**.

### [P3] DirectKV — OSDI 2026
- Paper: No Buffer, No Bottleneck: Efficient Zero-Copy KV Cache Offloading for Long-Context LLMs
- Official page: https://www.usenix.org/conference/osdi26/presentation/luo
- PDF: https://www.usenix.org/system/files/osdi26-luo.pdf
- Key figures: **Figure 1**, Figure 8.

### [P4] SYMPHONY — NSDI 2026
- Paper: SYMPHONY: Enabling Compute-Memory Disaggregation in LLM Serving Systems
- Official page: https://www.usenix.org/conference/nsdi26/presentation/agarwal
- PDF: https://www.usenix.org/system/files/nsdi26-agarwal.pdf
- Key figures: **Figure 1**, Figure 2.

### [P5] Bidaw — FAST 2026
- Paper: Bidaw: Enhancing Key-Value Caching for Interactive LLM Serving via Bidirectional Computation–Storage Awareness
- Official page: https://www.usenix.org/conference/fast26/presentation/hu-shipeng
- PDF: https://www.usenix.org/system/files/fast26-hu-shipeng.pdf
- Key figure: **Figure 9**.

### [P6] TurboBus — SIGCOMM 2026
- Paper: TurboBus: Pooling PCIe Bandwidth for LLM Workloads via Scale-Up Fabrics
- PDF: https://home.cse.ust.hk/~kaichen/papers/turbobus-sigcomm26.pdf
- SIGCOMM program: https://conferences.sigcomm.org/sigcomm/2026/program/papers/
- Key figures: **Figure 1**, Figure 10.

### [P7] KVServe — SIGCOMM 2026
- Paper: KVServe: Service-Aware KV Cache Compression for Communication-Efficient Disaggregated LLM Serving
- PDF: https://arxiv.org/pdf/2605.13734
- SIGCOMM program: https://conferences.sigcomm.org/sigcomm/2026/program/papers/
- Key figures: **Figure 1**, Figure 12, Figure 13, Figure 14.

### [P8] Beluga — SIGMOD 2026
- Paper: Beluga: A CXL-Based Memory Architecture for Scalable and Efficient LLM KVCache Management
- PDF: https://arxiv.org/pdf/2511.20172
- SIGMOD papers: https://2026.sigmod.org/sigmod_papers.shtml
- Key figures: **Figure 1**, Figure 11, Figure 12, Figure 13, Figure 14.

### [P9] MemChannel — NSDI 2026
- Paper: Building A CSFQ-Inspired Transport for Switched CXL Memory Pooling
- Official page: https://www.usenix.org/conference/nsdi26/presentation/guo-zerui
- Key evidence: intra-host contention、in-fabric congestion、unmanaged host-remote DIMM interaction；host-side admission/rate control/congestion feedback。

### [P10] KVDrive — SIGMOD 2026
- Paper: KVDrive: A Holistic Multi-Tier KV Cache Management System for Long-Context LLM Inference
- ArXiv: https://arxiv.org/abs/2605.18071
- SIGMOD papers: https://2026.sigmod.org/sigmod_papers.shtml

### [P11] Libra — NSDI 2026
- Paper: Libra: Flexible Request Partitioning and Scheduling for Serving Unbalanced and Dynamic LLM Workloads
- Official page: https://www.usenix.org/conference/nsdi26/presentation/ruan-libra

### [P12] AlignedServe — SIGMOD 2026
- Paper: AlignedServe: Orchestrating Prefix-aware Batching to Build a High-throughput and Computing-efficient LLM Serving System
- ArXiv: https://arxiv.org/abs/2605.23389
- SIGMOD program: https://2026.sigmod.org/sigmod_program_detailed.shtml

## 新近但非同等级 peer-reviewed 证据

### [E1] Composable CXL Memory as a Kubernetes-Native Shared Memory for LLM Serving
- Date: 2026-09-09 preprint
- ArXiv: https://arxiv.org/abs/2609.10790
- 用途：说明 CXL shared memory 正开始通过 Kubernetes DRA/CDI 与 vLLM/llm-d connector 被软件栈资源化。
- 限定：作者明确称 feasibility study rather than performance evaluation。

---

# 12. 后续一页 PPTX 的内容编排约束（供生成 Agent 使用）

## 顶部贯通文本框：约整页高度 1/6

### 应讲什么

用 **Agentic workload + hardware trend** 建立问题：

- DualPath trace：157 turns / 32.7K context / 429-token append / 98.7% KV hit。[P1]
- Ampere→Blackwell：GPU compute 28.8×，PCIe BW 2×，GPU memory 2.4×。[P1-F3]
- 结论：长上下文、高复用把“重复计算问题”转化成“状态搬运问题”；compute 与 I/O 代际增长失配使 KV movement 更容易进入 critical path。[综合推断]

### 推荐文字方向

> Agentic AI 正将推理从“一次性 Prompt 计算”转变为“长生命周期 State Reuse”：代表性 coding-agent workload 中，单 session 可达百轮级交互，历史上下文远大于每轮新增 token，KV hit 接近 99%；与此同时 GPU Compute 的代际增长显著快于 PCIe 与 HBM Capacity。超长上下文与高并发正在把 KVCache 从 GPU 内部缓存推向跨 Host Memory、CXL、Network 与 Storage 流动的数据中心级状态资源。

以上文字的事实部分来自 DualPath；“数据中心级状态资源”属于综合判断。[P1]

---

## 中间左侧上框：现状 / 变化趋势

建议标题：**KVCache 的角色正在发生三级跨越**

1. `GPU-local Cache -> Hierarchical Memory`：Strata / DirectKV / KVDrive。[P2][P3][P10]
2. `Per-instance State -> Shared/Disaggregated State`：SYMPHONY / Beluga / E1。[P4][P8][E1]
3. `Passive Data -> Schedulable State & Data Path`：DualPath / TurboBus / KVServe / MemChannel。[P1][P6][P7][P9]

核心洞察：

> LLM serving 的调度对象正在从 `Request + GPU` 扩展为 `Request + KV State + Compute + Data Path + SLO`。[综合判断]

---

## 中间左侧下框：最新技术 + 定量效果

只选 4–6 个最能形成闭环的数字，不要堆满全部论文：

- Strata：same-TTFT throughput up to **5×**；证明 hierarchical cache 必须和 scheduler co-design。[P2]
- DualPath：online throughput avg **1.96×**；证明 I/O path 可以被全局调度。[P1]
- DirectKV：transfer **-50% / GPU memory -43% / E2E up to 1.2×**；证明 Host DRAM 可成为 direct-access tier。[P3]
- TurboBus：KV-offload throughput up to **1.6×**；证明 PCIe bandwidth 可 pooling。[P6]
- Beluga：throughput up to **4.79× vs MoonCake**；证明 CXL shared memory semantic 对 KV pool 的潜力。[P8]
- KVServe：bandwidth-limited paper conditions 下 JCT up to **9.13×** / prefix TTFT up to **32.8×**；证明 bytes-on-wire/compression 也应被在线控制。[P7]

页面脚注应注明：**不同论文 baseline、模型、硬件、SLO 不同，倍数不可横向比较。**

---

## 中间右侧 2/3：论文原始架构图 + 实验图

首选布局：

1. **主图（上）**：DualPath Figure 4 —— KV I/O dual-path architecture。
2. **下左**：Strata Figure 9 —— scheduling + I/O ablation。
3. **下右**：DualPath Figure 3 左半 —— GPU compute vs PCIe/memory trend。

如果整页更偏 HOST CPU / memory：把主图换成 DirectKV Figure 1 或 Beluga Figure 1。

截图必须：
- 保留坐标轴、legend、图内数字；
- 删除论文 Figure caption 和正文；
- 不重绘原始实验数据，避免视觉误差；
- 如需高亮，PPT 上覆盖半透明框/箭头，不改原图数据。

---

## 底部贯通文本框：约整页高度 1/6

建议最终总结：

> **随着 Agentic AI、多轮长上下文和高 KV 复用把 KVCache 从 GPU 私有缓存推向跨 HBM、Host Memory、CXL、Network 与 Storage 流动的数据中心级 State，LLM Serving 的优化对象正在从单一 GPU Compute 扩展为 Compute + State + Data Movement。2026 年 AMD、Intel、NVIDIA、Arm 的 CPU 路线同步强化 high-frequency/per-core performance、Memory BW/Cache、accelerator I/O/coherent fabric 与并发能力，与这一 AI Host 需求形成产业侧印证。HOST CPU 将从 Runtime/Kernel Launch 的辅助角色升级为 State、Memory 与 Data-Movement Control Plane。建议提前布局 State-aware Scheduling、Hierarchical Memory、Programmable Data Path、Bandwidth QoS 和 Host-Centric KV Orchestrator。**

其中：
- 前两句关于 KV 演变和 scheduler 变化为多论文综合判断。[P1][P2][P3][P4][P6][P8]
- 厂商特性来自 AMD/Intel/NVIDIA/Arm 官方资料。[V1][V2][V3][V4][V5][V6]
- `Host-Centric KV Orchestrator` 是本报告提出的建议方案，不应写成业界既有标准。

---

# 13. 最终业务洞察

顶会论文目前并不是在重复解决同一个“KVCache offload”问题，而是在不同层面同时打破旧 AI Infra 的隐含假设：

| 旧假设 | 2026 证据显示的变化 |
|---|---|
| KV 属于某块 GPU / 某个 serving instance | SYMPHONY、Beluga、CXL shared-memory work 开始把 State 与 Compute 解耦。[P4][P8][E1] |
| Host Memory 只是 GPU 缺容量时的慢速 backing store | DirectKV 让 CPU-resident KV 被 GPU 直接访问；Strata 把 Host/SSD 变成正式 cache hierarchy。[P2][P3] |
| PCIe/NIC/Storage path 是固定拓扑资源 | DualPath、TurboBus 证明 I/O path / PCIe bandwidth 可以动态选择、借用和池化。[P1][P6] |
| Cache hit 越高就一定越快 | Strata/Bidaw/KVServe 表明 transfer stall、ready time、compression overhead 与 scheduling 共同决定“命中是否可用”。[P2][P5][P7] |
| Scheduler 的核心对象是 GPU compute slot | Libra、SYMPHONY、DualPath 等开始把 KV movement、state locality 与 network/load 纳入 scheduling。[P1][P4][P11] |

因此本轮洞察可凝练为：

> **[综合判断] 当可复用 State 的规模超过 Compute-local Memory 能力，并且 State 的搬运时间进入 SLO critical path 时，State Movement 就不再是存储实现细节，而成为 Scheduling 本身的一部分。**

进一步可表达为：

`Future LLM Scheduler = Compute Scheduler + State Scheduler + Data-Movement Scheduler`

这不是某一篇论文给出的公式，而是对 OSDI/NSDI/SIGCOMM/SIGMOD/FAST 2026 多项工作的归纳。[P1][P2][P4][P5][P6][P7][P8][P9][P11]

从 CPU 产品侧看，更谨慎而有力的表述是：

> **[综合判断] KVCache 不是推动 CPU 架构演进的唯一因素，但它与 Agentic tool execution、retrieval、memory disaggregation 和 accelerator orchestration 一起，正在强化同一组 AI Host 需求：高 loaded per-core 性能、高并发、较大的 cache、高 memory bandwidth、高速 CPU-GPU/CXL I/O，以及可预测的 fabric latency/QoS。AMD EPYC 9006、Intel Diamond Rapids、NVIDIA Vera 与 Arm Neoverse CSS N4 的 2026 官方路线均对这些方向给出了直接产业证据；其中 NVIDIA 更明确把 KV-cache management 写入 Host CPU workload。**[V1][V2][V3][V4][V5][V6]

这也是后续一页 PPT 最值得传递的战略判断：

> **HOST 的未来价值不是“替 GPU 多算一点”，而是让分散在 HBM、Host Memory、CXL、Network 和 Storage 中的 AI State，在正确时间、通过正确路径、以满足 SLO 的代价到达正确的计算资源。CPU 因此从 Accelerator Host 演进为 AI Infra 的 State / Memory / Data-Movement Resource Orchestrator。**

---

