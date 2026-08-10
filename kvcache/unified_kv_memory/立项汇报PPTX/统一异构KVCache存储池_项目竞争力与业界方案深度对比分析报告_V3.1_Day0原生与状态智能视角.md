# 统一异构 KVCache 存储池项目竞争力与业界方案深度对比分析报告

## Executive Summary

- **本项目不应被定义为“更大的 KV 缓存”，而应被定义为“AI 推理状态基础设施”。** KVCache 不是普通字节，而是已经支付过 Prefill 计算成本、能够兑换未来时延和算力的物化执行状态。项目的目标，是让这种状态可以被识别、估值、调度、迁移、验证和安全消费。
- **“支持 UBMEM/URMA”不是独家能力，Day-0 第一方闭环才可能形成首发优势。** Mooncake 已有 URMA、UB、UBShmem 和 Ascend Direct 路径，LMCache 也可通过 Mooncake/NIXL/GDS 等后端快速吸收硬件能力。只有在目标芯片能力第一时间进入默认高性能路径、Core/Cache/NoC/IODie/DDRC/PCIe/UBMEM/URMA/网络/SSD/NPU/功耗/RAS 信息能够进入决策，并且项目具备修改芯片/固件/驱动与校准 QueryPlan 的共同设计权时，这种差异才成立。
- **长期壁垒不是首发本身，而是“Day-0 首发—标准认证—状态运营数据—决策知识—硬件反哺”的飞轮。** KVCache 本体是物化执行状态，前缀目录、热度、路径、队列和硬件遥测是状态运营元数据，成本模型、调度策略和异常模式是决策知识资产。项目应围绕这三层资产建设 Inference State Intelligence，而不是泛化成收集所有日志的通用运维平台。
- **架构价值只有被客户感知为“更多合格业务产出、更低单位成本、更稳定体验和更小经营风险”，竞争力才真正闭环。** StateValueEstimate 只是内部资源决策的中间变量；项目还必须把状态决策逐层映射到服务 SLO、客户业务事务和财务结果，并交付可对账的 Customer Value Receipt。当前 8 个强制 PVT 和 3 个条件 CVT 尚无通过结果，所有客户回报仍必须通过真实客户 Trace、同条件基线和业务价值测试验证。

---

## 一、文档定位与结论边界

### 1.1 文档信息

| 项目 | 内容 |
|---|---|
| 版本 | V3.1 |
| 评估基准日 | 2026-08-06 |
| 面向对象 | 技术 CTO、部门主管、首席架构师、开发团队主管、市场部门主管 |
| 主要用途 | 项目评审、立项论证、竞争定位、技术路线和市场叙事 |
| 项目事实基线 | 总体架构与 SRS 评审导读 V2.2、全量需求树 V2.3.1、SRS V2.2、关键技术原型验证清单 V1.5 |
| Mooncake 本地基线 | `aa9ec9113d29f43957440174363c3fe23592b8b7` |
| LMCache 本地基线 | `3b8093cf8860a39d05937af915adfb5db493a047` |

### 1.2 四类证据必须分开

| 标记 | 定义 | 本报告中的使用方式 |
|---|---|---|
| **事实** | 已由项目文档、源码或官方资料确认 | 可以描述当前设计或竞品现状 |
| **项目规划** | 已进入 SRS、需求树或 PVT，但尚未验收 | 只能描述为目标、要求或计划能力 |
| **分析判断** | 基于事实形成的架构和战略推论 | 必须说明成立条件 |
| **待验证主张** | 必须由 PVT/CVT 或生产数据证明 | 不得转写成已实现性能或市场承诺 |

### 1.3 本报告提出的概念升级

本报告在现有 `KVAccessIntent → QueryPlan → AttachHandle → ExtentManifest → Descriptor → Completion/Fence/Integrity → Ready` 契约链基础上，提出六个便于评审和后续设计讨论的概念：

- **Tier R**：把 Recompute（重算）视为资源图中的虚拟层级；
- **StateValueEstimate**：对一份候选 KV 状态的预期净价值进行估计；
- **ActualPathReceipt**：记录实际数据路径、主机触碰、完成与回退证据的路径收据；
- **Day-0 Native Enablement**：目标芯片/固件能力在平台发布窗口内进入默认可验证路径，而非后续可选 Adapter；
- **Conformance Profile**：把 UBMEM/URMA、Descriptor、Fence、错误、Telemetry 和回退要求版本化为可测试的兼容性画像；
- **Inference State Intelligence**：以状态运营元数据校准成本模型、策略和硬件设计的决策知识层。

它们是本报告建议的概念升级，**不是现有 SRS 已经冻结的正式对象名**。是否进入后续需求和接口，应经过架构评审。

---

## 二、项目必须从“缓存池”升级为“推理状态基础设施”

### 2.1 KVCache 的本质不是数据副本，而是物化计算结果

一份 KVCache 至少同时承载四种价值和约束：

1. **内含计算价值**：它代表已经完成的 Prefill 计算；丢弃后可能需要重新消耗 NPU/GPU 算力。
2. **未来时延价值**：在后续请求中及时复用，可以缩短 TTFT 并释放 Prefill 资源。
3. **资源占用成本**：它持续占用 HBM、UBMEM、DDR、SSD、网络带宽、队列和介质寿命。
4. **消费责任**：只有模型、权重、Tokenizer、Prompt Template、Adapter、租户、布局、精度、版本和状态一致时，才允许消费。

因此，KVCache 不应只被描述为“推理产生的大块临时数据”。更准确的定义是：

> **KVCache 是带语义身份、内含计算成本、时效、位置、风险和消费资格的物化执行状态。**

这个定义改变了系统的优化目标。缓存系统常以容量、命中率或带宽为中心；推理状态基础设施应以“在业务截止时间内安全消费并产生净收益”为中心。

### 2.2 “可识别、可定价、可调度、可验证消费”的完整含义

| 能力 | 不是指什么 | 真正要解决的问题 |
|---|---|---|
| 可识别 | 只有一个 Token Hash 或对象 Key | 能证明模型语义、布局、版本、租户和状态是同一份可复用执行结果 |
| 可定价 | 简单按 GB 收费 | 对节省的重算、访问时限、搬运成本、资源干扰、可靠性风险形成逐请求内部影子价格 |
| 可调度 | 只决定放入 HBM、DDR 或 SSD | 在多个副本、路径、介质、直接访问、分段加载和重算之间联合决策 |
| 可验证消费 | 收到 DMA/RDMA 完成通知 | 完成、可见性、版本、完整性、租约、授权和 Rank 共识全部满足后才发布 Ready |

### 2.3 存储只是落点之一，计算也是获取状态的路径

传统缓存层级通常写成：

```text
HBM → DDR → SSD → Remote Storage
```

本项目更合理的资源图应当是：

```text
HBM / UBMEM / DDR / Local SSD / Remote Memory / Remote SSD / Tier R
                                                                    │
                                                                    └─ Recompute
```

当已有 KV 的查询、搬运、挂接、Rank 同步和干扰成本高于重新 Prefill 时，“命中后加载”不是优化，而是负优化。Tier R 使系统承认一个关键事实：

> **最便宜的状态来源有时不是任何存储介质，而是重新计算。**

这也解释了为什么本项目不应追求最大命中率。真正应该最大化的是净复用价值和 SLO 达成率。

---

## 三、状态价值模型：让 KVCache 真正“可定价”

### 3.1 内部影子价格，而非先做商业计费

“可定价”首先是一种资源决策能力，不等同于向客户按 KV 字节收费。建议将候选状态的预期价值抽象为：

```text
ExpectedStateValue
  = P(semantic_valid)
  × P(ready_before_deadline)
  × SavedRecomputeCost
  - LookupCost
  - TransferCost
  - AttachCost
  - RankSyncCost
  - InterferenceCost
  - MediaCost
  - RiskReserve
```

其中：

- `SavedRecomputeCost` 不仅是算力时间，还包括释放的 Prefill 容量、排队时间和功耗；
- `TransferCost` 包含所有 Direct、Peer、Staged 路径和布局转换；
- `InterferenceCost` 包含对 Decode、CPU、NUMA、内存带宽、NIC、SSD 队列和其他租户的影响；
- `RiskReserve` 用于吸收超时、路径抖动、版本不确定性和回退失败风险。

只有在语义门禁通过、能够在截止时间前 Ready 且 `ExpectedStateValue > 0` 时，加载和复用才有充分理由。

### 3.2 价值模型应驱动完整生命周期

| 生命周期动作 | 状态价值驱动的问题 |
|---|---|
| Admission | 这份状态是否值得进入外层缓存，而不是生成后立即丢弃？ |
| Retention | 它应保存多久，过期后重算是否更便宜？ |
| Placement | 放在 HBM、UBMEM、DDR、SSD 还是远端，哪种位置具有最高边际价值？ |
| Replication | 多一个副本节省的尾延迟和故障风险是否覆盖容量成本？ |
| Promotion | 是否值得提前预取到更快层，还是保持原位？ |
| Eviction | 应逐出最低热度对象，还是逐出最低未来净价值对象？ |
| Consumption | 命中后加载、直接访问、部分加载还是重算？ |
| Settlement | 预测的节省是否真实发生，成本模型是否需要校准？ |

最后一项“Settlement”非常重要：QueryPlan 不能只输出估计，还应从实际路径、完成时延、回退和业务 SLO 中学习。这样，价值模型才能从静态规则变成软硬件共同校准的运行时能力。

### 3.3 建议的北极星指标

```text
usable_hit_ratio
  = 在截止时间内通过全部消费门禁的命中状态
  / 所有状态查询

positive_value_reuse_ratio
  = 实际产生正净收益的复用请求
  / 所有执行了加载的请求

state_value_realization
  = 实际节省的重算与 SLO 收益
  / QueryPlan 预测收益
```

它们应与 TTFT、TPOT、吞吐、NPU 利用率、Host Payload Touch、SSD 写放大和单位有效 Token 成本联合观察。单独提升 usable hit 仍可能造成资源干扰；单独降低 TTFT 也可能以 TPOT 或介质寿命为代价。

---

## 四、行业会收敛，但不会自动形成同一种产品

### 4.1 能力形态正在收敛

**事实**：Mooncake TENT 已能依据请求意图、优先级、内存类型、大小和位置选择传输，并提供多传输回退；文件路径默认可在 GDS、io_uring 和 RDMA 间选择。[Mooncake TENT Transport Selector](https://kvcache-ai.github.io/Mooncake/design/tent/transport-selector.html)

**事实**：LMCache 已从进程内缓存扩展到独立 MP 服务、多种 L1/L2 后端、异步 Store/Prefetch/Eviction、NIXL/P2P 和多硬件适配，并继续增强高级 L2 策略和插件体系。[LMCache 官方仓库](https://github.com/LMCache/LMCache)；[LMCache 官方文档](https://docs.lmcache.ai/)

**事实**：NVIDIA Dynamo/KVBM/NIXL 已把路由、SLA Planner、GPU/Host/SSD/远端层、Block 生命周期和异构传输组合为完整栈。[Dynamo Introduction](https://docs.nvidia.com/dynamo/getting-started/introduction)；[KVBM Guide](https://docs.nvidia.com/dynamo/latest/user-guides/kv-cache-offloading)

**事实**：vLLM 正在推进原生多层 KV Offloading，提出 Primary Tier、Secondary Tier、TieringManager、异步 Promotion 和规范化 CPU Layout。[vLLM Multi-tier KV Offloading RFC](https://github.com/vllm-project/vllm/issues/38260)

由此可以合理判断，以下能力会逐步成为行业标配：

- 多层容量和异步加载；
- 动态传输选择与故障回退；
- KV 感知路由与 P/D 分离；
- 目录、生命周期、事件和可观测性；
- 多运行时 Connector 和多存储 Provider；
- 基于 SLO、负载和成本的策略决策。

### 4.2 收敛的是能力，不一定是系统的不变量

| 方案 | 当前设计中心 | 首要回答的问题 |
|---|---|---|
| Mooncake | 高性能传输与分布式对象 Store | 如何可靠、高效地移动和保存 KV 对象？ |
| LMCache | 跨引擎、跨后端复用 | 如何让一次 Prefill 在更多引擎和介质中复用？ |
| NVIDIA Dynamo/KVBM/NIXL | NVIDIA 推理集群效率 | 如何组合路由、卸载、P/D 和 NVIDIA 硬件以优化 SLO/TCO？ |
| vLLM/SGLang HiCache | 推理运行时内调度和层级缓存 | 如何在引擎调度路径中扩大可复用状态容量？ |
| 本项目建议定位 | AI 执行状态的语义、价值和消费治理 | 什么状态值得保存、何时可以消费、加载还是重算、硬件应如何围绕状态价值协同？ |

这张表描述的是各方案的“重心”，不是说其他项目永远不会覆盖相邻能力。独特性必须来自长期坚持的设计取舍，而不是对竞品当前缺口的静态截图。

### 4.3 如果只做功能集合，本项目会被收敛掉

以下能力重要，但不能再作为长期独有卖点：

- HBM/DDR/SSD 多级缓存；
- RDMA/URMA/UB 传输；
- GDS 或设备直达 SSD；
- 异步预取与计算通信重叠；
- Prefix Cache 和缓存感知路由；
- P/D 分离；
- QoS、指标和插件化后端。

Mooncake 已包含 UMDK/URMA、UB/UBShmem 相关实现；LMCache 已覆盖 GDS、hipFile、NIXL、P2P 和多后端；NVIDIA 已有 KVBM/NIXL/GDS。协议接入的领先窗口通常短于产品和生态建设周期。

---

## 五、本项目应坚持的六条技术品味

### 5.1 语义先于位置：存在不是命中，命中不是可用

目录中存在候选 KV，只能称为 raw hit。只有模型语义、布局、精度、版本、租约、授权、完整性、路径、Rank 状态和时限均满足，才是 usable hit。

**系统不变量 1：任何物理位置和高性能路径都不能绕过语义身份与消费资格。**

这使本项目首先是状态系统，其次才是存储系统。

### 5.2 价值先于容量：缓存更多不等于服务更好

如果保存和加载一份 KV 带来的网络、SSD、CPU、内存带宽和 Decode 干扰超过重算收益，就不应存或不应加载。

**系统不变量 2：任何复用决策必须能解释预期净收益；命中率不能替代收益。**

### 5.3 重算是一等路径：Tier R 不是失败兜底

重算不应只在所有副本加载失败后被动触发，而应与 HBM、DDR、SSD 和远端路径同时参与计划比较。对于短前缀、拥塞路径、低复用概率、布局转换昂贵或截止时间不足的请求，重算可能是主路径。

**系统不变量 3：QueryPlan 必须允许在“有命中”时主动选择 Tier R。**

### 5.4 Ready 先于消费：Completion 只是证据之一

CQE、DMA 完成、RDMA 完成或文件读取完成，只证明某个后端动作结束。只有完成、Fence、版本、完整性、租约、授权和 Rank 共识闭合后，才能发布可消费引用。

```text
Completion + Visibility + Version + Integrity
+ Lease + Authorization + Rank Consensus
= Ready
```

**系统不变量 4：推理算子只能消费 Ready 状态。**

### 5.5 实际路径先于宣传路径：允许降级，但禁止隐性降级

Direct Path 可能因对齐、注册、文件系统、拓扑、驱动或队列条件回落到 Host staged path。系统可以为了可用性回退，但必须记录 planned path、actual path、Host Payload Touch、原因码、附加成本和 SLO 影响。

**系统不变量 5：任何路径退化都必须可观测、可预算、可限流和可禁用。**

### 5.6 开放数据平面、自主语义控制平面

Mooncake、LMCache、NIXL、3FS 或厂商存储并非天然竞争对象，它们可以作为 Provider。项目需要掌握的是状态身份、价值模型、消费门禁、QueryPlan、硬件能力图和证据标准。

**系统不变量 6：更换数据平面不能改变状态语义，也不能绕过消费和证据协议。**

这条原则兼顾自主性与生态速度：自主的是架构控制权，不是重复实现所有协议。

---

## 六、从现有架构演化为 Inference State Fabric

### 6.1 五个逻辑平面

| 平面 | 核心对象和职责 | 与现有架构的关系 |
|---|---|---|
| 状态语义平面 | KV 语义身份、版本、布局、租户、生命周期、租约 | 强化 TM2 和 Connector 合约 |
| 状态经济平面 | StateValueEstimate、Admission、Retention、load-vs-recompute、SLO | 强化 TM1/QueryPlan |
| 状态编排平面 | 副本、层级、预取、逐出、迁移、Tier R | 对应 TM1+TM3 |
| 状态数据平面 | UBMEM、URMA、DMA、RDMA、IO、SSD、第三方 Provider | 对应 TM4/L4 |
| 状态证据平面 | Completion、Fence、Integrity、Ready、ActualPathReceipt、Trace | 强化 TM6 并贯穿四层 |

这不是推翻现有四层架构，而是用“状态价值”重新解释四层和六个横向模块之间的关系。

### 6.2 建议的端到端状态生命周期

```text
Intent
  → Identify
  → Discover
  → Qualify
  → Price
  → Plan
  → Move / View / Recompute
  → Verify
  → Attach
  → Consume
  → Settle & Learn
```

- `Identify`：建立语义身份；
- `Discover`：发现候选副本；
- `Qualify`：排除不兼容或不可授权状态；
- `Price`：估计各路径和 Tier R 的净价值；
- `Plan`：确定路径、时限、回退和资源预算；
- `Verify`：形成 Ready 所需证据；
- `Settle & Learn`：比较预测与实际结果，校准能力图和成本模型。

### 6.3 硬件融合应从“Backend 接入”升级为“能力编译”

UBMEM、URMA、芯片、NIC/DPU、PCIe/互联、SSD 和固件的深度融合，应至少形成以下闭环：

1. 发现内存域、设备、队列、拓扑、注册和一致性能力；
2. 生成实测 `CapabilityGraph`，记录可达性、对齐、粒度、有效带宽、尾延迟、Fence、失败域和 Host Touch；
3. 将 KV 布局和访问意图编译成合法 Descriptor；
4. 运行时记录实际路径、拥塞和回退；
5. 校准 StateValueEstimate 和 QueryPlan；
6. 将反复出现的瓶颈反馈给芯片、驱动、固件、IO 和 SSD 团队。

如果只完成第 1—3 步，本项目只是硬件 Adapter；完成第 1—6 步，才形成软硬件协同飞轮。

---

## 七、与 Mooncake 的竞争与合作边界

### 7.1 Mooncake 的优势不能低估

Mooncake 已具有成熟的数据移动与 Store 能力：拓扑感知、多 NIC、DRAM/VRAM/SSD、对象生命周期、副本、租约、QoS、TENT 动态选择与故障切换，并广泛对接推理框架。其 Store 还在加强租户配额、SSD Offload 和持久化能力。[Mooncake Store](https://kvcache-ai.github.io/Mooncake/design/mooncake-store.html)

Mooncake 的工程成熟度、协议广度、生态和生产背景均明显领先于本项目当前阶段。

### 7.2 本项目不应把 Mooncake 的边界当成永久缺口

TENT 已出现 `intent_type`、统一优先级和策略绑定，说明 Mooncake 正从通用传输进一步接近意图感知。未来它完全可能增加更复杂的成本、状态和调度机制。

因此，本项目相对 Mooncake 的长期差异不应表述为“Mooncake 只会传输”，而应表述为：

- 本项目把状态价值和消费资格置于架构中心；
- QueryPlan 比较加载、直接访问、部分加载和 Tier R；
- actual path 和 Host Touch 是强治理对象；
- 目标硬件能力和消费语义由同一套证据闭环连接。

### 7.3 推荐策略

- 允许 Mooncake Transfer Engine/Store 作为 Provider；
- Mooncake 返回的对象命中仅视为候选 Extent，不直接等价于 Ready；
- 本项目为 Mooncake 路径增加状态身份、价值、时限、ActualPathReceipt 和消费门禁；
- 只有目标硬件路径无法由 Mooncake 满足时，才自研对应数据平面。

---

## 八、与 LMCache 的竞争与合作边界

### 8.1 LMCache 的优势不能低估

LMCache 的中心价值是跨引擎、跨后端复用。其 MP Daemon、vLLM 集成、CPU/Device-DAX/GDS、NIXL、P2P、异步预取、OpenTelemetry、CacheBlend 和丰富后端，使其具有很强的生态扩张能力。

对于需要快速获得 vLLM 集成、非前缀复用、对象存储和通用后端的团队，LMCache 的短期交付优势明显。

### 8.2 本项目的独立重心

LMCache 的通用可移植性决定了它需要兼容多种设备和后端；在部分非 CUDA 平台上，缺少专用 Handle 能力时可能回落为更通用的 gather/scatter 路径。本项目若与目标 NPU、UBMEM、URMA、驱动和 SSD 团队共同设计，可以更早形成原生路径。

但“平台原生”只有在以下条件同时成立时才是优势：

- 路径不是隐藏 staged；
- 端到端 p99 和单位有效 Token 成本优于通用路径；
- 不以 TPOT、正确性、CPU 或介质寿命为代价；
- 优势能够通过能力图和 Provider ABI 稳定复现。

### 8.3 推荐策略

- 将 LMCache Adapter 作为生态入口；
- 复用其通用后端、非前缀能力和引擎集成；
- 由本项目重新执行状态资格和 StateValueEstimate；
- 不重复建设已经成熟的通用存储插件。

---

## 九、NVIDIA 是最接近目标形态的战略对手

### 9.1 NVIDIA 已经覆盖大部分功能外形

| 层次 | 组件 | 能力 |
|---|---|---|
| 集群控制 | Dynamo Router/Planner | KV/负载感知路由、P/D、SLA/TCO 规划 |
| 状态资源管理 | KVBM | GPU、Host、SSD、远端存储、Block 生命周期和事件 |
| 异构传输 | NIXL | CPU/GPU/文件/块/对象的插件化数据移动 |
| 设备存储路径 | GDS | GPU 与存储之间的直接 I/O |
| 运行时 | TensorRT-LLM/NIM | KV 复用、Host Offload 和运行时管理 |
| 网络与卸载 | NVLink/UCX/BlueField/DOCA | 高速互联和基础设施卸载 |

[KVBM 官方文档](https://docs.nvidia.com/dynamo/latest/user-guides/kv-cache-offloading) 已明确覆盖 GPU、Pinned Host、RDMA 内存、本地/分布式 SSD、文件、对象和云存储；[KVBM Design](https://docs.nvidia.com/dynamo/v-0-9-0/design-docs/kvbm-design) 还描述了 Disk→Device 直接 Onboard、布局和 Block Pool 编排。

### 9.2 本项目不能用哪些能力对 NVIDIA 做差异化

- 多层存储；
- 动态传输；
- KV 感知路由；
- P/D 分离；
- SLO/TCO Planner；
- SSD 直达；
- 多运行时 Connector；
- 生命周期事件和插件化存储。

### 9.3 仍可能形成的独特价值

1. **目标硬件平台的状态管理主权**：围绕 NPU、UBMEM、URMA 和自有 IO/SSD 形成与 CUDA/NVLink/GDS 对等的第一方优化能力。
2. **更强的消费资格契约**：把语义身份、版本、租约、授权、Fence、完整性和 Rank 共识统一到 Ready 门禁。
3. **把重算作为一等资源路径**：不是只做 Offload/Onboard，而是对已有状态与重新生成状态做逐请求经济比较。
4. **路径真实性治理**：为 Direct、Peer、Staged 和 Tier R 输出统一实际路径收据。
5. **反向驱动硬件产品**：将状态工作负载证据转化为芯片、固件、网络和 SSD 的需求输入。

这些价值仍需 PVT 证明；NVIDIA 也可能继续吸收类似机制，因此必须尽早形成生产数据、兼容认证和生态标准。

---

## 十、其他方案与硬件厂商图谱

| 方案 | 定位与现状 | 对本项目的意义 |
|---|---|---|
| [SGLang HiCache](https://docs.sglang.io/docs/advanced_features/hicache_design) | GPU L1、Host L2、分布式 L3；RadixAttention、多 Rank 共识、I/O 友好布局、多 L3 后端 | 强运行时 Connector 对象，证明分层和布局优化已非独有 |
| [vLLM](https://github.com/vllm-project/vllm) | APC、KV Connector、NIXL/Mooncake/LMCache 与原生多层 Offloading 演进 | 事实接口标准，必须兼容而非替换 |
| [FlexKV](https://github.com/taco-project/FlexKV) | CPU/SSD/云存储、多节点复用、io_uring/GDS | L3 管理直接竞品，也可作为 Provider |
| [AIBrix](https://github.com/vllm-project/aibrix) | 云原生推理控制面、KV/负载路由、分布式 KV 和 SLO 路由 | 集群控制与云原生场景竞品 |
| [InfiniStore](https://bytedance.github.io/InfiniStore/design.html) | 推理集群远端内存池、RDMA、DRAM/SSD 和跨节点复用 | 轻量远端池 Provider |
| [MemServe](https://arxiv.org/abs/2406.17565) | 弹性 MemPool、全局 Prompt Tree 和缓存局部性调度 | 证明“全局池+调度”已有研究先例 |
| [3FS](https://github.com/deepseek-ai/3FS) | 高性能分布式文件系统 | L3 存储底座，不是完整状态语义层 |
| [vLLM Ascend KV Pool](https://docs.vllm.ai/projects/ascend/en/main/user_guide/feature_guide/kv_pool.html) | AscendStore、Memcache/Memfabric、Mooncake Connector、Layerwise 等能力 | 目标生态中的直接竞合对象 |
| MindIE/LLM DataDist | 昇腾推理、Prefix Cache 与 P/D KV 传输 | 需要整合边界，避免内部重复建设 |
| [AMD hipFile](https://rocm.docs.amd.com/projects/hipFile/en/develop/index.html) | Direct-to-GPU I/O 和 POSIX fallback | 说明设备直达会成为平台底座，回退治理更重要 |
| [Google TPU + Managed Lustre](https://cloud.google.com/tpu/docs/storage-best-practices) | Host RAM 与低时延存储的 KV Offload 基础设施建议 | 说明云厂商已把 KV 状态视为基础设施工作负载 |

---

## 十一、竞争力矩阵：从功能比较升级为设计中心比较

| 维度 | 本项目建议目标 | Mooncake | LMCache | NVIDIA Dynamo/KVBM/NIXL |
|---|---|---|---|---|
| 第一性对象 | 带语义、价值和证据的执行状态 | Segment/Object | Cache Chunk/Engine Object | KV Block |
| 优化目标 | SLO 内的安全正价值消费 | 高性能数据移动与对象服务 | 跨引擎复用与容量扩展 | NVIDIA 集群 SLO/TCO |
| 原始命中到可消费命中 | 架构中心 | Store/集成层共同处理 | 引擎/目录/后端共同处理 | Runtime Adapter/KVBM 处理 |
| 重算角色 | Tier R，一等候选路径 | 主要由上层决定 | 主要由引擎/策略决定 | Router/Planner/运行时共同决定 |
| 状态估值 | 逐请求净价值与截止时间 | 传输策略和位置成本持续增强 | Prefetch/Store 策略持续增强 | SLA/TCO/缓存负载能力较强 |
| 实际路径收据 | 规划为强契约 | 有路径和遥测，staged 可能自动出现 | Direct/copy 取决于后端和设备 | 有多路径和指标，统一消费语义仍在运行时层 |
| Host Payload Touch | 规划为一等 KPI 和预算 | 非统一产品北极星 | 各后端表现不同 | 具备直达能力，非等价统一指标 |
| 硬件反馈闭环 | 芯片/IO/SSD 联合设计目标 | 广泛适配，多平台通用 | 厂商中立、插件化 | NVIDIA 第一方闭环最强 |
| 数据平面开放性 | 计划兼容第三方 Provider | 自有 Store/TE，也被多方集成 | 后端生态广 | NIXL/KVBM 模块化 |
| 当前成熟度 | 原型待验证 | 明显领先 | 明显领先 | 生态、投入和硬件闭环最强 |

### 11.1 短期差异与长期壁垒必须分开

| 层级 | 示例 | 被竞品复制难度 | 建议投入 |
|---|---|---:|---|
| 功能差异 | 新介质、新协议、新 Connector | 低 | 只做目标场景必要能力 |
| 集成差异 | 目标芯片原生 UBMEM/URMA、算子和 Descriptor | 中 | 形成首发性能优势 |
| 决策与证据差异 | 状态价值模型、Tier R、Ready 门禁、实际路径收据 | 中高 | 作为架构核心 |
| 数据飞轮 | 真实工作负载、成本校准、故障与路径证据 | 高 | 持续积累，形成运营壁垒 |
| 标准与生态 | Connector/Provider ABI、硬件认证、跨框架身份协议 | 高 | 形成长期平台壁垒 |

真正可持续的竞争力主要位于后三层，而不是第一层。

---

## 十二、项目的独特价值应如何表述

### 12.1 面向评委的一句话

> **我们不是建设一个更大的 KV 缓存，而是把 KVCache 从推理框架内部的内存对象，提升为 AI 数据中心可识别、可估值、可调度、可验证消费的执行状态；存储只是状态的落点之一，重算也是状态的获取路径，最终让计算、内存、网络和存储围绕状态价值协同工作。**

这里建议用“可估值”解释技术机制，用“可定价”解释平台和商业延伸，避免评委误解为项目首先要建设计费系统。

### 12.2 四个重新定义

| 传统理解 | 本项目重新定义 | 价值 |
|---|---|---|
| KVCache 是大块临时内存 | KVCache 是物化执行状态 | 把计算与存储统一到同一个资源对象 |
| 命中就是存在 | 命中必须可消费且有正收益 | 避免错误命中和负优化 |
| 层级是 HBM/DDR/SSD | 层级是异构资源图 + Tier R | 不被固定三级缓存限制 |
| 性能是命中率和峰值带宽 | 性能是 SLO、净价值、路径真实性和资源护栏 | 从局部速度转向生产收益 |

### 12.3 面向不同角色的价值

#### 技术 CTO

项目争夺的是目标算力平台在“推理状态和数据移动”领域的架构控制权，而不是一个 Cache Backend。成功后，可降低对 CUDA/NIXL/GDS 单一路线的结构性依赖，并形成芯片—IO—SSD—推理软件联合优化入口。

#### 首席架构师

独特价值是跨层不变量：语义身份、价值、Tier R、Ready 和 ActualPathReceipt。需要防止框架、存储和传输层分别做局部决策，导致跨层信息丢失和责任不清。

#### 开发团队主管

交付不应按“完成多少 Backend”统计，而应按端到端状态闭环统计：一个状态能否被识别、估值、计划、获得、验证、消费和结算。

#### 部门主管

项目需要跨芯片、驱动、网络、SSD、推理框架和平台团队共同 Owner。其价值来自组织和技术闭环，单一软件团队难以独立兑现。

#### 市场部门主管

可传播的不是“我们也支持多级缓存”，而是“我们让每一份推理状态都能被识别、估值和可靠消费”。但所有性能数字必须绑定模型、硬件、工作负载和 PVT 证据。

---

## 十三、客户价值闭环：让“状态价值”变成可感知的业务回报

### 13.1 客户不购买“状态价值”，客户购买业务结果

“状态价值”回答的是系统内部问题：一份 KV 状态是否值得保存、搬运、直接访问或重算。客户真正关心的是另一组问题：

- 同样的算力预算，能否按时完成更多对话、检索回答、API 请求或 Agent 任务？
- 同样的业务量，能否减少加速卡、能耗、峰值冗余和运维成本？
- 高峰、长上下文和故障条件下，用户体验是否仍然稳定？
- 原来因成本或时延不可行的长上下文、多轮 Agent 和个性化服务，是否变得可以商业化？
- 每一项收益是否能够与基线对账，而不是依赖缓存命中率或峰值带宽来间接解释？

因此，必须明确两个不同层次：

> **State Value 是控制平面的微观经济学；Customer Value 是客户业务的经营结果。前者只有汇聚并兑现为后者，才构成竞争力闭环。**

本项目需要把优化单位从 KV Block 和 Token 上移到客户的**业务事务**。业务事务可以是一次满足质量和时延要求的对话轮次、一次 RAG 回答、一次完成的 Agent 任务、一次代码生成作业或一个满足 SLA 的 API 请求。客户价值的核心分母不是“缓存了多少 GB”，而是“完成了多少个满足质量、时延、正确性和可用性要求的业务事务”。

### 13.2 客户价值方程：从技术收益转成经营收益

建议用下面的框架定义客户价值，而不是预设一个未经验证的 ROI 百分比：

```text
CustomerValueCreated
  = ΔSLOQualifiedBusinessOutcomes × UnitBusinessContribution
  + AvoidedAcceleratorCapacity
  + AvoidedEnergyAndOperationsCost
  + AvoidedSLAAndFailureLoss
  + ArchitectureOptionValue
  - IncrementalPlatformTCO
```

其中：

- `SLOQualifiedBusinessOutcomes` 是在质量、TTFT/端到端时限、可用性和正确性约束内完成的业务事务；
- `UnitBusinessContribution` 由客户业务定义，可以是单次 API 毛利、单会话收入、单任务价值或内部生产率价值；
- `AvoidedAcceleratorCapacity` 是在相同业务量和 SLO 下，相对基线不再需要采购或租用的加速卡容量；
- `AvoidedEnergyAndOperationsCost` 包括能耗、CPU/内存/网络/SSD、值守和故障处理成本；
- `AvoidedSLAAndFailureLoss` 包括超时、降级、错误状态消费、越权和服务中断造成的损失；
- `ArchitectureOptionValue` 是客户能够跨芯片、框架和数据平面演进，减少锁定并缩短新产品上线时间的选择权价值；
- `IncrementalPlatformTCO` 必须包含统一池软件、元数据服务、额外介质、网络、运维和集成成本，不能只计算节省项。

这不是一个可以脱离客户数据直接填数的公式。没有客户 Trace、单位业务贡献、硬件价格、电价、SLA 合同和运维口径时，只能建立测量方法，不能宣称收入或 ROI 提升。

### 13.3 五条客户回报路径

| 客户回报 | 状态基础设施如何产生作用 | 客户应看到的指标 | 最终经营含义 |
|---|---|---|---|
| **更多收入或业务承载** | 正价值复用释放 Prefill 算力，价值调度减少排队和无效搬运 | 每加速卡小时的 SLO 合格请求、会话或任务数；高峰拒绝率 | 同等集群服务更多付费需求，或减少高峰需求流失 |
| **更低单位成本** | 在 HBM、UBMEM、DDR、SSD、远端和 Tier R 之间选择全局成本最低路径 | 每个合格业务事务的全生命周期成本；每百万有效 Token 成本 | 改善 API 毛利或降低企业内部 AI 单位服务成本 |
| **更稳定的客户体验** | Deadline、QoS、Migration Interlock 和负收益重算保护前台服务 | TTFT/端到端完成时延 p95/p99、TPOT/ITL、SLO 达成率 | 降低等待和超时，减少 SLA 赔付、投诉和用户流失风险 |
| **原来不可行的产品变得可行** | 长上下文、多轮状态、共享前缀和 Agent 分支获得经济可行的状态复用 | 在价格与 SLO 约束内可支持的上下文长度、并发会话和 Agent 步数 | 支持高级套餐、复杂 Agent 和差异化功能，缩短产品上市时间 |
| **更低风险与更高选择权** | 语义门禁、租户隔离、Ready、ActualPathReceipt 和开放 Provider | 错误/陈旧/越权消费率、可审计覆盖率、迁移周期和替换成本 | 降低事故、合规和供应商锁定风险，保护长期投资 |

客户不一定同时购买全部五类价值。首个客户项目应只选择一个主价值和一至两个护栏，例如“提高高峰期 SLO 合格 Agent 任务数，同时确保错误消费为零、单位任务成本不恶化”。目标过多会让 POC 无法归因。

### 13.4 技术必须通过一条完整价值传导链

客户价值不能从“支持 URMA”或“HBM↔SSD 路径更短”直接跳到“业务 ROI 更高”。必须逐层证明：

```text
状态决策
  → 资源结果
  → 服务结果
  → 业务结果
  → 财务结果
  → 客户可审计证据
```

| 层次 | 要回答的问题 | 示例证据 |
|---|---|---|
| 状态决策 | 为什么 Load、View 或 Recompute？ | StateValueEstimate、QueryPlan、Decision Regret |
| 资源结果 | 实际节省或消耗了什么？ | Prefill 计算、NPU/GPU 时间、HBM、网络、CPU、SSD、能耗 |
| 服务结果 | 请求体验是否改善且护栏未倒退？ | TTFT、TPOT/ITL、端到端时延、吞吐、可用性、正确性 |
| 业务结果 | 客户多完成了什么？ | 合格会话、RAG 回答、API 请求、Agent 任务或代码作业 |
| 财务结果 | 这些业务结果值多少钱？ | 增量毛利、避免容量、单位成本、避免损失、上线周期 |
| 审计证据 | 收益是否可复现、可归因？ | 基线版本、工作负载、planned/actual path、状态消费记录、成本口径 |

只有这六层能够使用同一 `request_id / session_id / tenant_id / query_plan_id` 追溯，客户才会把项目理解为业务基础设施，而不是一个难以归因的底层优化。

### 13.5 面向不同客户，业务价值单位必须不同

| 目标客户或场景 | 推荐的业务价值单位 | 主 KPI | 关键护栏 |
|---|---|---|---|
| 大模型 API/推理云 | 满足套餐 SLA 的请求或有效 Token | SLO 合格吞吐/加速卡小时、单位请求成本 | TTFT/ITL p99、质量、可用性 |
| 企业 RAG/智能助手 | 满足质量与时限的回答或会话 | 单集群并发会话、单合格回答成本 | 引用质量、租户隔离、错误状态消费 |
| 多轮 Agent 平台 | 成功完成的端到端任务 | 单加速卡小时完成任务数、单任务成本 | 任务成功率、端到端时限、恢复能力 |
| 长上下文/代码服务 | 按时完成的长上下文作业 | 经济可行上下文长度、作业吞吐 | 生成质量、TPOT、峰值内存和失败率 |
| 私有化或受监管行业 | 可审计且合规完成的业务事务 | 合规事务成本、连续服务能力 | 越权消费为零、审计覆盖、数据主权 |

这张表也决定了市场语言。面对 Agent 客户应讲“每小时完成更多可靠任务”，面对 API 云客户应讲“提高 SLA 合格产能和单位毛利”，面对私有化客户应讲“可审计、可迁移和可控风险”；不应向所有客户统一讲“命中率提升”。

### 13.6 客户应看到一张 Customer Value Receipt

本项目已有 ActualPathReceipt 的技术方向，但要让客户感知价值，还需要在其上汇聚形成 **Customer Value Receipt（客户价值凭证）**。这是本报告建议增加的产品概念，不是现有冻结 SRS 对象。

客户价值凭证至少应按租户、业务类型和时间窗口回答：

- 本周期处理了多少业务事务，其中多少满足约定 SLO？
- 哪些状态被复用、直接访问或重算，为什么这样选择？
- 相对双方确认的基线，实际节省了多少 Prefill 计算、加速卡时间和等待时间？
- 实际经过了什么数据路径，是否发生 staged、fallback 或资源干扰？
- 产生了多少额外合格产能或避免容量，单位业务成本如何变化？
- 是否出现错误、陈旧、越权消费、SLO 违约或介质寿命异常？
- 预测价值与兑现价值的偏差是多少，下一周期如何校准？

Customer Value Receipt 把“可验证消费”从技术正确性延伸到商业可验证性：客户不需要相信我们的架构叙事，只需要核对同一业务基线下是否获得更多合格产出或更低成本。

### 13.7 产品不能只交付能力，还要交付价值合同

建议把客户方案包装为四类可选择的价值合同，而不是销售“KVCache 容量”：

1. **SLO 合格产能合同**：在约定模型、硬件、流量分布和时延门槛下，承诺每加速卡小时的合格业务产出。
2. **单位业务成本合同**：在同等质量和 SLO 下，以单个合格请求、会话或任务的全生命周期成本对账。
3. **高峰稳定性合同**：围绕 p99、拒绝率、回退成功率和前后台干扰设置门槛。
4. **状态可信与可迁移合同**：围绕错误消费为零、租户隔离、路径可审计和 Provider 可替换性设置门槛。

合同指标必须从客户业务目标反推，技术指标只能作为驱动项。客户可以接受某个阶段没有最高命中率，但不会接受“命中率提高、业务 SLO 和成本却没有改善”。

### 13.8 用 Business Value Test 完成客户验证

建议在现有 PVT 之上增加 **BVT（Business Value Test，业务价值测试）**，不改变现有 PVT 编号和技术门禁：

1. **价值发现**：读取匿名客户 Trace，确定复用结构、业务事务、峰谷、SLO、单位成本和主要损失来源；没有正价值场景则 No-Go。
2. **冻结基线**：固定模型质量、框架、硬件、并发、上下文分布和业务 SLO，至少比较无外部复用、成熟开源路径和本项目路径。
3. **同条件实验**：分别测量客户结果、服务 SLO、资源成本和正确性护栏，禁止只展示命中率或峰值带宽。
4. **财务换算**：由客户确认硬件、租赁、能耗、运维、SLA 和单位业务贡献口径；未知项保留区间，不使用单点假精度。
5. **生产对账**：灰度期持续生成 Customer Value Receipt，验证收益能否跨高峰、故障和版本变化保持稳定。

[NVIDIA Dynamo Planner](https://docs.nvidia.com/dynamo/components/planner) 已明确指出 TTFT/ITL 等用户 SLA 不能简单由 CPU 利用率或请求吞吐代理；[Dynamo KVBM Guide](https://docs.nvidia.com/dynamo/latest/user-guides/kv-cache-offloading) 也把“复用不足时没有 TTFT 收益甚至发生性能退化”列为需要通过真实指标排查的情形。这恰好说明：行业正在走向 SLO 和代表性工作负载验证，但本项目还应再向前一步，把 SLO 继续映射为客户业务事务和财务回报。

### 13.9 面向客户的一句话

> **我们不是帮助客户保存更多 KV，而是把客户已经支付过的 Prefill 计算转化为可以反复兑现的 SLO 合格产能：在不降低模型质量和服务可靠性的前提下，让同一套算力完成更多有价值的对话、检索和 Agent 任务，或者让相同业务量消耗更少的加速卡、能耗和运维成本；每一笔收益都能由状态决策、实际路径和业务结果共同对账。**

这才是“推理状态基础设施”最终应被客户感知的产品品味：**不销售抽象先进性，而交付可以验收的业务结果；不要求客户相信命中率，而让客户核对价值凭证。**

---

## 十四、Day-0 原生、标准生态与状态智能：项目第二条竞争力主线

### 14.1 两点思考都有价值，但需要组合成三阶段壁垒

“目标芯片 UBMEM/URMA 第一优先级和第一时间支持”与“标准生态、微架构融合、运行数据资产化”不是两个平行卖点，而是一条连续的竞争力成长路径：

| 阶段 | 竞争作用 | 应沉淀的资产 | 必须证明的证据 | 如果停在本阶段 |
|---|---|---|---|---|
| **Day-0 首发** | 在目标芯片发布和客户首批部署窗口领先通用开源方案 | 原生 Provider、Descriptor/Fence、默认策略、性能包络 | 相同硬件和工作负载下的端到端 PVT | 只是短期时间窗口，容易被追平 |
| **标准与认证** | 把一次性优化转化为跨版本、跨厂商、可复制能力 | Conformance Profile、测试套件、兼容矩阵、认证流程 | 多代芯片/驱动/固件与多框架一致通过 | 只形成内部接口，无法成为生态 |
| **状态智能飞轮** | 用生产数据持续改进策略、容量规划和下一代硬件 | 工作负载画像、成本模型、异常模式、决策模型、硬件反馈 | 生产 Decision Regret 下降、客户价值稳定兑现、硬件改进闭环 | 没有长期学习，标准和首发仍会商品化 |

因此，Day-0 是竞争入口，标准生态是规模化机制，状态智能与硬件反哺才是长期壁垒。三者缺一不可。

### 14.2 软件“功能清单”不是壁垒，但软件控制权不可替代

“软件功能上的差异不是关键竞争力”这个判断基本正确，但不能进一步推导为“软件不重要”。需要区分：

- **易收敛的软件功能**：多级缓存、某种传输协议、LRU、Prefetch、Dashboard、某个 Connector。这些可以被 Mooncake、LMCache 或上游框架快速吸收。
- **难替代的软件控制权**：谁定义状态身份、谁解释硬件能力、谁决定 Load/View/Recompute、谁发布 Ready、谁记录实际路径、谁把遥测校准为成本模型、谁把客户问题反馈到芯片设计。

硬件原语本身不会自动产生客户价值。Core、NoC、IODie、DDRC、PCIe、UBMEM、URMA、NIC 和 SSD 的能力，必须经过软件的 `CapabilityGraph → QueryPlan → Descriptor → ActualPathReceipt → Customer Value Receipt` 才能转化为 SLO 合格产能。因此，项目要弱化的是功能数量竞争，强化的是**软硬件联合决策权、验证权和反馈权**。

### 14.3 Day-0 原生支持为何仍然成立，但“支持协议”已经不成立

必须正视开源现状：[Mooncake Build Guide](https://kvcache-ai.github.io/Mooncake/getting_started/build.html) 已列出 Ascend Direct 与 UBShmem 构建路径；[Ascend Direct Transport](https://kvcache-ai.github.io/Mooncake/design/transfer-engine/ascend_direct_transport.html) 已支持 HCCS/RDMA、异步传输和 Fabric Memory；[Kunpeng UB Transport](https://kvcache-ai.github.io/Mooncake/design/transfer-engine/kunpeng_ub_transport.html) 已使用 URMA；[TENT Transport Selector](https://kvcache-ai.github.io/Mooncake/design/tent/transport-selector.html) 也已支持 intent、priority、memory type、location、size、传输偏好和 fallback。

所以不能再说“Mooncake 不支持 URMA/UB，而我们支持”。真正的 Day-0 必须同时满足：

1. **时间优先**：在芯片/固件正式发布窗口内完成适配、验证和客户可用发布，而不是上游成熟后的跟随接入。
2. **默认优先**：UBMEM/URMA 是目标平台上经 PVT 证明后的默认候选路径，而不是编译期开关或边缘 Adapter。
3. **信息优先**：QueryPlan 可获得通用开源项目通常不可见的 Core/Cache/NoC/IODie/DDRC/IO/功耗/RAS 能力和遥测。
4. **共同修改权**：当路径瓶颈位于芯片、固件、驱动、队列、门铃、IOMMU 或 SSD 控制器时，项目能够推动底层修改，而不只是绕开问题。
5. **证据优先**：每条首发路径都有性能包络、失败边界、回退策略、版本矩阵和客户业务收益证据。
6. **生态优先**：把可公开的 Provider、Connector、测试用例和 Trace Schema 贡献到 Mooncake、LMCache、vLLM/SGLang 等生态，让目标硬件不依赖单一私有入口。

如果缺少第三、第四项，本项目与开源社区中的硬件适配团队没有本质区别；如果缺少第五、第六项，首发只能是一次性 Demo。

### 14.4 “完全兼容 UBMEM/URMA”必须变成可认证的标准资产

openEuler 已公开 [URMA API Guide](https://docs.openeuler.org/zh/docs/24.03_LTS_SP3/unifiedbus/unifiedbus/urma/URMA%20API%20Guide.ch.html) 和 [UB OS Component](https://www.openeuler.org/zh/projects/ub-os-component/)；URMA 涉及 segment、import/mapping、远程读写/原子、访问权限、完成与错误等明确语义。因此，“完全兼容”不能只是源代码中调用某个 API，而应形成版本化的 **KV State over UBMEM/URMA Conformance Profile**。

建议标准与生态至少交付六类资产：

| 标准资产 | 内容 | 解决的问题 |
|---|---|---|
| **Semantic ABI** | KV 身份、版本、租户、布局、状态、Ready 和消费资格 | 不同框架和硬件对“同一状态”含义一致 |
| **UBMEM/URMA Profile** | segment、mapping、registration、atomic、descriptor、fence、错误和安全要求 | 原生能力如何承载 KV 状态 |
| **Capability Schema** | 可达性、粒度、对齐、SG、带宽、尾延迟、失败域、功耗和 RAS | 微架构能力可被统一编译和查询 |
| **Conformance Suite** | 正确性、互操作、性能包络、故障、回退和安全测试 | “兼容”可重复验证而非自我声明 |
| **Reference Implementation** | 原生 Provider、框架 Connector、模拟器和最小样例 | 降低芯片、框架和存储厂商接入成本 |
| **Certification & Registry** | 芯片/固件/驱动/协议版本矩阵、认证结果和已知边界 | 客户能够选择可用组合并控制升级风险 |

标准化和微架构优化并不矛盾，但必须采用双层结构：**稳定的状态语义与证据契约向上保持可移植，芯片专属能力通过版本化 Hardware Profile 向下深穿透。** Core、NoC 或 IODie 细节不应泄漏为上层框架的硬编码条件，而应被编译成能力、成本和限制。

### 14.5 KVCache 是三类资产中的第一类，不应统称为“运维数据”

互联网运维元数据的类比是有启发性的，但需要更精确。KVCache 本体不是网页点击日志，而是一份可能包含客户语义、已经支付计算成本且可被推理算子直接消费的物化状态。建议把资产分为三层：

| 资产层 | 典型内容 | 核心价值 | 治理重点 |
|---|---|---|---|
| **A. 执行状态资产** | KVCache、可复用 Prefix/Layer Block、Encoder/多模态中间表示等 | 直接替代重算、缩短时延或扩展可服务能力 | 语义正确、租户隔离、生命周期、加密和安全销毁 |
| **B. 状态运营数据资产** | 请求状态、PrefixDirectory、位置/版本/租约、访问热度、路径热度、队列、带宽、失败和功耗 | 解释状态应该放哪、何时移动、是否重算以及为何失败 | 最小化采集、时间对齐、血缘、匿名化、留存和访问控制 |
| **C. 状态决策知识资产** | 工作负载画像、性能包络、成本模型、异常模式、放置/淘汰/预取/重算策略 | 持续提高决策质量，并反向影响容量规划和硬件设计 | 模型版本、漂移、可解释性、回滚和跨客户隔离 |

三层资产的价值强度和安全属性不同。A 层通常最敏感、生命周期最短；B 层适合在匿名化和聚合后形成运营洞察；C 层最有可能形成长期 IP 和数据飞轮。项目的长期壁垒不应依赖长期保存客户 KV 正文，而应更多来自 B、C 两层。

### 14.6 哪些推理中间数据值得进入项目范围

项目不应收集“所有 AI Infra 中间数据”，而应只纳管能够改变状态决策、客户 SLO 或硬件设计的数据：

| 数据域 | 代表字段或对象 | 可驱动的决策 |
|---|---|---|
| 请求与会话状态 | tenant、model、SLA、deadline、ISL/OSL、session/agent branch | Admission、优先级、路由、保留期限 |
| 全局前缀与对象目录 | prefix hash、semantic identity、location、version、lease、Ready | 可消费匹配、副本选择、错误状态拦截 |
| 访问与复用热度 | 频率、间隔、共享度、生命周期、未来复用概率 | Retention、Placement、Replication、Eviction |
| 路径与传输热度 | planned/actual path、bytes、p99、queue、fallback、Host Touch | 选路、限流、批量、Direct/Staged 治理 |
| 计算与内存压力 | Prefill/Decode 队列、Core/NPU 利用率、Cache/NoC/DDRC 带宽和争用 | Load vs Recompute、迁移时机、算搬均衡 |
| IO 与介质状态 | IODie/PCIe/NIC/SSD 队列、带宽、放大、寿命、故障域 | SSD Admission、写回、复制和寿命预算 |
| 能源与可靠性 | 功耗、温度、降频、ECC/RAS、链路健康 | 能效调度、故障隔离、性能包络和容量规划 |

与状态决策无关的通用应用日志、用户行为分析、模型质量数据和企业数据湖不应被纳入首期范围。边界越清楚，项目越像基础设施控制系统，而不是无边界的数据平台。

### 14.7 从 Telemetry 到 Inference State Intelligence 的闭环

建议在现有五个逻辑平面之上，明确一个不进入同步关键路径的 **State Intelligence Plane**：

```text
芯片/互联/内存/IO/SSD/功耗/RAS Telemetry
  → State Operations Data Model
  → Workload Profile / Performance Envelope / Cost Model
  → QueryPlan、Admission、Placement、Eviction、Load-vs-Recompute
  → ActualPathReceipt + Customer Value Receipt
  → 决策后悔、SLO、成本与异常对账
  → 策略校准 + 下一代芯片/固件/驱动需求
```

它应输出四类长期资产：

- **平台数字画像**：每代芯片和系统拓扑在不同 KV 粒度、布局、并发和功耗下的真实性能包络；
- **工作负载画像**：不同客户场景的复用结构、状态生命周期、峰谷和 SLO 敏感性；
- **决策知识库**：哪些条件下 Direct、Copy、Remote、SSD 或 Tier R 最优，以及策略失误的反例；
- **硬件联合设计清单**：Core、Cache、NoC、IODie、DDRC、PCIe、UB/URMA、SSD 和功耗机制的量化改进需求。

这使项目从“使用芯片能力”升级为“用真实推理状态负载定义和验证芯片能力”。这也是通用开源仓库最难单独完成、但第一方平台团队最有理由承担的工作。

### 14.8 与 Mooncake、LMCache 的区别应改写为职责中心不同

| 维度 | Mooncake | LMCache | 本项目应承担的独立责任 |
|---|---|---|---|
| 主要设计中心 | 高性能传输、分布式对象缓存和 TENT 数据移动策略 | 框架侧 KV 复用、Offload、Connector/Backend 与多进程存储管理 | 目标硬件第一方推理状态控制、标准认证和软硬件反馈 |
| 国产硬件支持 | 已有 URMA/UBShmem/Ascend Direct，仍以通用项目节奏演进 | 可通过 Mooncake/NIXL/插件等扩展 | Day-0 默认路径、未公开微架构信息和底层共同修改权 |
| 运行数据 | 传输、对象、QoS 和故障等数据持续增强 | 已有 lookup/hit/retrieve/store/remote latency/usage 等指标 | 跨请求—状态—路径—微架构—客户价值的统一血缘与模型 |
| 标准角色 | 提供广泛传输/存储 API 和社区实现 | 提供 KV Connector、Backend 和运维接口 | 定义目标生态 Conformance Profile、认证套件和兼容矩阵 |
| 数据飞轮 | 优化 Mooncake 的传输与 Store | 优化 LMCache 的复用与存储管理 | 同时校准 QueryPlan、客户容量规划与下一代国产硬件 |

本项目不应排斥二者。Mooncake 可以作为高质量数据平面和上游生态入口，LMCache 可以作为框架 Connector/复用层之一；项目的独立性来自**谁掌握目标平台的 Day-0、标准认证、跨层状态血缘、决策知识和硬件反哺责任**。

### 14.9 对标 NVIDIA，应对标系统工程方法而不是产品目录

[NVIDIA Dynamo Overall Architecture](https://docs.nvidia.com/dynamo/v-0-9-0/design-docs/overall-architecture) 已把请求路径、控制路径、状态/事件路径、Router、Planner、KVBM、NIXL 和故障闭环组织为完整推理系统；[DGX GB Rack Scale Software](https://docs.nvidia.com/dgx/dgxgb200-user-guide/software.html) 则把 CUDA 应用、NVLink/NVSwitch、InfiniBand/Ethernet、BlueField/DOCA 与整机软件协同起来。NVIDIA 的壁垒并不是某一个 KVCache 功能，而是 GPU、互联、网络、存储、运行时、调度、遥测和工程验证能够共同演进。

本项目合理的对标方式是：

1. **对标方法**：跨硬件—固件—驱动—运行时—框架—平台—客户 SLO 的共同 Owner 和闭环，而不是复制每个 NVIDIA 产品。
2. **对标资产**：标准契约、参考实现、性能包络、认证矩阵、工作负载与故障套件、生产遥测、容量模型和设计反馈。
3. **对标节奏**：目标芯片 Day-0 使能、首批客户灰度、上游社区接入和跨代兼容同时规划。
4. **国产生态差异**：面向多种国产芯片、UB/URMA、不同 IO/SSD 和供应链，建立开放语义契约，避免复制单厂商封闭绑定。

更准确的项目定位不是“国产 NVIDIA Dynamo”，而是：

> **面向国产 AI 硬件生态的推理状态系统工程参考项目：以 KVCache 为第一类状态工作负载，验证芯片到客户业务的全链路协同，并沉淀可复用标准、数据和决策知识。**

### 14.10 这条主线如何重新回答项目必要性

即使 Mooncake、LMCache 已经能够完成大量缓存、存储和传输功能，本项目仍可能必要，因为它承担了开源项目通常不会为单一目标平台持续承担的五项责任：

- 对目标芯片未公开路线、固件和微架构能力的 Day-0 产品化；
- 对 UBMEM/URMA 与 KV 状态语义的版本化标准、认证和客户升级责任；
- 对跨 Core/NoC/内存/IO/网络/SSD/功耗的端到端性能与故障归因；
- 对客户状态工作负载、决策模型和容量经济性的长期运营；
- 对下一代国产芯片和系统设计的量化反馈。

但必要性是有条件的。如果项目拿不到早期硬件信息和底层共同修改权、不能成为标准或认证 Owner、没有生产 Trace 和持续运营团队，也不能推动 Mooncake/LMCache/vLLM/SGLang 等上游接入，那么合理边界应收缩为 UBMEM/URMA Provider、认证工具或专项加速库，而不是完整平台。

### 14.11 面向高层的一句话

> **项目的必要性不在于再造一套 Mooncake 或 LMCache，而在于建立国产 AI 硬件的第一方推理状态系统工程：让新芯片能力 Day-0 进入可验证的客户路径，把一次性首发沉淀为 UBMEM/URMA 标准与认证，再把 KV 状态、运行元数据和决策知识形成生产飞轮，持续反哺 Core、NoC、IODie、内存、网络、SSD、NPU 和功耗设计。**

---

## 十五、产品边界：哪些必须自主，哪些应开放复用

### 15.1 必须自主掌握

1. KV 状态身份、版本、布局、租约和消费资格规范；
2. KVAccessIntent、QueryPlan 和 load/view/recompute 决策；
3. StateValueEstimate 及其在线校准机制；
4. Completion/Fence/Integrity/Ready 状态机；
5. CapabilityGraph 与目标硬件原生 Provider；
6. planned/actual path、Host Touch 和回退证据；
7. 跨框架 Connector 兼容和认证套件；
8. UBMEM/URMA KV State Conformance Profile、版本矩阵和认证责任；
9. State Operations Data Model、工作负载/性能/成本/故障证据库；
10. State Intelligence 的模型版本、决策校准、回滚和硬件反馈闭环。

### 15.2 优先开放和复用

1. Mooncake/NIXL 的通用传输；
2. LMCache 的通用后端、非前缀复用和 vLLM 集成；
3. 3FS、对象存储和通用分布式存储；
4. vLLM/SGLang 的运行时内 Paged KV 管理；
5. Kubernetes、Prometheus、OpenTelemetry 等通用基础设施；
6. 成熟 RDMA、TCP、NVMe-oF、io_uring 和文件系统组件；
7. 向 openEuler、Mooncake、LMCache、vLLM/SGLang 等上游公开的协议适配、参考实现和通用测试。

### 15.3 明确不做或暂不扩张

- 不在首阶段自建通用分布式文件系统；
- 不重写所有推理框架内部 KV 分配器；
- 不以“全栈自主”为由重复实现成熟协议；
- 不把 direct-view 作为所有 Decode 场景默认答案；
- 不把 KVCache 项目直接扩张为管理所有 AI 中间状态的通用平台；
- 不建设通用日志平台、用户行为分析系统或企业数据湖；
- 不以长期保存客户 KV 正文作为数据壁垒；
- 不在没有收益证据时追求最大容量和最大命中率。

---

## 十六、从 KVCache 到 AI State Fabric：长期机会与范围纪律

### 16.1 为什么 KVCache 可能是更大状态基础设施的第一站

未来 AI Infra 中会持续出现可复用、可迁移的执行状态：

- Encoder 和多模态中间特征；
- Agent 长会话和工具执行上下文；
- 投机解码草稿状态；
- 稀疏注意力索引与长期上下文状态；
- 推理迁移、容错和弹性伸缩状态；
- 某些模型权重、Adapter 或 Expert 的热状态。

它们与 KVCache 共享部分问题：语义身份、时效、内含计算、位置、生命周期、消费资格和重算替代。

### 16.2 不能过早泛化

不同状态的正确性、粒度、更新频率、消费方式和成本结构差异很大。本项目当前应坚持：

> **KVCache 是 Inference State Fabric 的验证锚点，不是用一个统一抽象立刻吞并所有 AI 状态。**

只有当 KVCache 的身份、估值、Tier R、Ready 和实际路径协议通过生产验证后，才考虑抽取更通用的 State ABI。

---

## 十七、四类实验决定“项目品味”是否真实存在

### 17.1 实验一：有 raw hit，但拒绝消费

构造模型版本、Adapter、布局、租约、完整性或 Rank 不一致场景。项目必须识别候选存在但不可消费，阻止错误状态进入算子，并自动选择其他副本或 Tier R。

**证明的主张**：语义先于位置，Ready 是硬门槛。

### 17.2 实验二：有 usable hit，但主动选择 Tier R

构造短前缀、路径拥塞、布局转换昂贵或截止时间不足场景。QueryPlan 应证明加载净收益为负，并在有命中的情况下主动重算。

**证明的主张**：项目不是命中率驱动，而是状态价值驱动。

### 17.3 实验三：标称 Direct，实际进入 Staged

注入对齐、注册、文件系统、队列或设备能力限制，使路径回退。系统必须输出 actual path、Host Payload Touch、原因码、成本和 SLO 影响，并根据策略限流或禁用。

**证明的主张**：允许降级，但禁止隐性降级。

### 17.4 实验四：更换数据平面，语义和决策保持稳定

在 Mooncake、LMCache 或自研 Provider 间切换，保持同一 KV 身份、QueryPlan 输入、Ready 门禁、Trace 和业务指标口径。

**证明的主张**：项目掌握的是状态控制面，而不是某个传输 Backend。

---

## 十八、现有 PVT 应如何承接新的核心思想

| PVT | 原有问题 | 状态基础设施视角的追加问题 | 必须输出的证据 |
|---|---|---|---|
| PVT-00 工作负载画像 | 是否存在复用机会 | 哪些状态具有正预期价值？ | 生命周期、复用概率、SavedRecomputeCost、SLO 和租户分布 |
| PVT-01 路径能力 | 哪条路径可用、够快 | 各路径真实价格和回退概率是多少？ | effective BW、p99、Host Touch、CPU/NUMA、actual path |
| PVT-02 布局与流水 | 能否批量异步搬运 | 布局如何影响状态价值和消费时限？ | 转换成本、批量度、队列、重叠和 Ready 时间 |
| PVT-03 UBMEM Direct-view | 是否可直接访问 | Direct-view 何时优于 copy-to-HBM 和 Tier R？ | TPOT、访存、算子成本、一致性和负收益边界 |
| PVT-04 QueryPlan | 是否会选路径 | 能否正确估值并选择 Load/View/Recompute？ | 预测误差、选择后悔率、负收益率、SLO 违约率 |
| PVT-05 DDR 角色 | DDR 是否有价值 | DDR 的边际状态价值是否覆盖干扰？ | 容量、带宽干扰、成本和分场景收益曲线 |
| PVT-06 raw→usable | 命中为何不可用 | 状态资格漏斗在哪里损失？ | 语义/布局/版本/租约/完整性/时限分类 |
| PVT-07 混合负载 | 端到端是否有收益 | 价值模型在多租、突发和故障下是否稳定？ | TTFT/TPOT p99、正价值复用、NPU、QoS、介质寿命 |

项目材料中的 TTFT p99、TPOT p99、usable hit、HBM 有效容量、NPU 利用率和有效带宽等建议目标，仍然是 **待验证门槛**。PVT 通过前，不得写成已实现性能。

### 18.1 建议新增四个核心指标

| 指标 | 定义 | 价值 |
|---|---|---|
| Decision Regret Rate | 事后看，所选路径比最佳可行路径成本更高的请求比例 | 衡量 QueryPlan 是否真正聪明 |
| Actual Path Fidelity | 实际路径与计划路径一致，或差异被完整记录和治理的请求比例 | 衡量软硬件路径是否可信 |
| Day-0 Enablement Lead Time | 从可用芯片/固件基线到原生路径通过 PVT 并可供客户灰度的时间 | 衡量首发优势是否真实存在 |
| Conformance Coverage | 已通过 Semantic ABI、UBMEM/URMA Profile、故障与回退测试的目标组合占比 | 衡量兼容性是否形成可复制标准 |

---

## 十九、12—18 个月实施路线

### 阶段 0：冻结世界观和对照基线（0—1 个月）

- 将“执行状态、Tier R、Ready、actual path、净价值”写入架构决策记录；
- 冻结 Mooncake、LMCache、Dynamo/KVBM、HiCache 和 Ascend KV Pool 对照版本；
- 统一模型、布局、并行配置、复用分布、SLO 和硬件拓扑；
- 冻结 Day-0 RACI、早期硬件/固件获取窗口与底层共同修改机制；
- 形成 UBMEM/URMA KV State Conformance Profile 草案；
- 选择一个灯塔客户场景，冻结“合格业务事务”、单位成本和财务换算口径；
- 建立 raw hit、usable hit、positive-value reuse 和 Host Touch 指标。

**退出门槛**：评审人员能够清楚区分“缓存命中”“可消费命中”“正价值消费”和“客户业务价值兑现”。

### 阶段 1：最小状态闭环（1—4 个月）

- 单框架、单模型、两种真实路径；
- 完成 Intent→Identify→Qualify→Plan→Verify→Attach→Consume；
- 建立版本、布局、完整性、租约、授权和 Rank 门禁；
- 实现 Tier R 主动选择；
- 建立 State Operations Data Model，串联请求、状态、路径、硬件遥测和策略版本；
- 打通请求、状态决策、服务 SLO 与业务事务的最小追踪链。

**退出门槛**：错误/陈旧/越权消费为 0；有命中选择重算的决策可解释；能够生成最小 Customer Value Receipt。

### 阶段 2：目标硬件能力编译与实际路径收据（4—8 个月）

- UBMEM/URMA 原生 Provider；
- CapabilityGraph 和 Descriptor 编译；
- 接入 Core/Cache/NoC/IODie/DDRC/IO/功耗/RAS 中与状态决策相关的可用遥测；
- 完成 Conformance Profile 正确性、互操作、性能包络、故障和回退测试；
- planned/actual path、Host Touch 和回退原因；
- StateValueEstimate 使用真实遥测校准；
- 故障、拥塞、SSD 抖动和中转注入。

**退出门槛**：至少一个代表场景相对成熟通用路径获得稳定端到端净收益，并能映射为 SLO 合格业务产出或单位业务成本改善，且护栏不倒退。

### 阶段 3：开放 Provider 与多租生产化（8—12 个月）

- 接入至少一个 Mooncake/NIXL/LMCache Provider；
- 增加第二个推理运行时 Connector；
- 完成租户、QoS、配额、热升级、灰度和回滚；
- 建立硬件/驱动/固件/SSD 兼容矩阵；
- 向至少一个关键上游提交通用 Provider/Connector、Trace Schema 或测试能力；
- 与灯塔客户完成冻结基线、同条件 BVT 和生产灰度对账。

**退出门槛**：更换 Provider 不改变状态语义和消费门禁；客户价值凭证能够跨版本和高峰周期稳定对账。

### 阶段 4：数据飞轮与生态标准（12—18 个月）

- 发布 Connector/Provider ABI、Conformance Profile、测试套件和参考实现；
- 形成芯片、网络、SSD 联合认证与公开兼容注册表；
- 建设非同步关键路径的 State Intelligence Plane，持续校准性能包络、成本模型和策略；
- 建立客户工作负载画像、容量、状态价值与业务价值规划工具；
- 将生产证据反向输入下一代硬件设计，并至少关闭一项可量化微架构/固件改进需求。

**退出门槛**：至少两个生产级工作负载、两个推理运行时和两种数据平面完成可复现验证；认证结果可复用，生产数据至少驱动一项策略改进和一项硬件/固件改进。

---

## 二十、评委汇报建议：用客户价值与国产系统工程兑现世界观

### 20.1 三分钟叙事结构

#### 第一问：为什么 KVCache 值得成为基础设施对象？

因为它不是普通缓存，而是已经付费生成、能够节省未来算力和时延的执行状态。随着长上下文、多轮对话、Agent、P/D 分离和集群化推理发展，其生命周期已经跨越单个请求、单个进程和单台机器。

#### 第二问：为什么现有缓存和传输方案还不够？

Mooncake、LMCache、KVBM/NIXL 已经很强，Mooncake 甚至已经支持 URMA、UBShmem 和 Ascend Direct。问题不是开源能不能接协议，而是谁对目标国产芯片 Day-0、未公开微架构能力、跨层标准认证、生产状态数据和下一代硬件反馈负责。通用社区可以提供高质量能力，但不会天然替单一目标平台长期承担这五项责任。

#### 第三问：本项目到底交付什么？

交付的不是另一个 Store，而是一套国产推理状态系统工程参考实现：以稳定状态契约连接框架，以 UBMEM/URMA Hardware Profile 深穿透芯片，以 Conformance Suite 保证兼容，以 State Intelligence 持续校准决策并反哺硬件。

#### 第四问：客户为什么会为它买单？

因为客户可以在同样硬件预算下获得更多 SLO 合格的对话、检索和 Agent 任务，或者在同等业务量下降低加速卡、能耗、峰值冗余和故障损失。项目不要求客户相信命中率，而是用 Customer Value Receipt 对账每周期的合格产出、单位成本和风险护栏。

#### 第五问：如何证明不是概念？

先用四个决定性技术实验证明状态控制正确，再用 Day-0 Lead Time、Conformance Coverage、客户 Trace、同条件基线和 BVT，证明首发、兼容、智能决策与客户业务回报都能稳定兑现。

### 20.2 建议的开场陈述

> 大模型推理正在从无状态计算走向状态化基础设施。KVCache 是第一类规模最大、价值最明确的推理执行状态。我们的目标不是建设一个更大的缓存，而是建设国产硬件生态的第一方推理状态系统工程：让新芯片能力 Day-0 进入可验证客户路径，把一次性首发沉淀为 UBMEM/URMA 标准与认证，把 KV 状态、运行元数据和决策知识形成生产飞轮，最终将已经支付过的 Prefill 计算转化为客户可重复兑现的 SLO 合格产能，并持续反哺下一代 Core、NoC、IODie、内存、网络、SSD、NPU 和功耗设计。

---

## 二十一、市场表述边界

### 21.1 当前可以使用

- “面向目标算力平台的 KV 推理状态基础设施架构。”
- “将 KVCache 从内存对象提升为可识别、可估值、可调度和可验证消费的执行状态。”
- “把重算纳入异构资源图，基于 SLO 和净价值选择 Load、View 或 Recompute。”
- “采用自主状态控制平面和开放数据平面，可集成 Mooncake、LMCache、NIXL 等 Provider。”
- “通过 planned/actual path 和 Host Payload Touch 治理隐藏中转和异常路径。”
- “以 SLO 合格业务产出和单位业务成本作为客户价值验收目标，并计划通过 Customer Value Receipt 对账。”
- “规划在目标芯片发布窗口内完成 UBMEM/URMA Day-0 原生使能，并以版本化 Conformance Profile 验证。”
- “以 KVCache 为首个状态工作负载，建设与状态决策直接相关的运行数据和硬件反馈闭环。”
- “对标 NVIDIA 的系统工程方法，建设面向国产硬件生态的推理状态参考实现、标准和验证体系。”

### 21.2 PVT 通过前禁止使用

- “全面领先 Mooncake、LMCache 或 NVIDIA”；
- “已实现 AI State Fabric”；
- “性能提升 20%/30%”；
- “已实现 HBM↔SSD 零拷贝”；
- “CPU 完全不参与”；
- “支持 URMA/UB 即拥有独家壁垒”；
- “适用于所有模型、请求和推理框架”；
- “已经为客户降低总体成本或提升收入”；
- “技术指标改善可以自动等价为客户 ROI”；
- “我们是唯一支持 UBMEM/URMA 的 KVCache 项目”；
- “已经完全兼容所有 UBMEM/URMA 版本和设备”；
- “已经形成行业标准或国产 NVIDIA 全栈”；
- “收集的运行数据天然就是高价值资产”。

### 21.3 PVT 通过后的合格表述

> 在指定客户工作负载、模型质量、布局、并发、SLO 和目标硬件条件下，本项目将可消费状态加载、直接访问和重算纳入同一 QueryPlan；相对双方冻结的基线，TTFT p99 改善 X%，TPOT p99 变化不超过 Y%，每加速卡小时的 SLO 合格业务事务提升 A%，单位合格业务事务成本下降 B%，错误状态消费为 0，Host Payload Touch 降至 Z。

> 针对目标芯片/固件版本 X，本项目在硬件可用后 Y 天完成 UBMEM/URMA 原生 Provider 的 PVT 和客户灰度；Conformance Profile R 覆盖语义、互操作、性能包络、故障、安全与回退共 N 项测试，已在 M 种框架/数据平面组合中通过。

所有数字必须能够追溯到 PVT/BVT 版本、硬件配置、工作负载、客户业务口径和原始数据。

---

## 二十二、风险、反例与必要克制

| 风险或反例 | 对结论的影响 | 应对原则 |
|---|---|---|
| 开源项目吸收状态和成本机制 | 功能差异进一步缩小 | 用生产数据、硬件反馈和标准生态构建壁垒 |
| StateValueEstimate 预测不准 | QueryPlan 可能比静态规则更差 | 从规则/查表起步，记录 Decision Regret，允许快速绕过 |
| 门禁和估值开销过大 | 语义正确但性能无收益 | 本地摘要、批量验证、缓存资格结果、快慢路径分离 |
| 重算消耗 Prefill 峰值资源 | Tier R 局部便宜但全局昂贵 | 纳入队列、集群负载、功耗和机会成本 |
| 过度强调单一芯片原生能力 | 生态狭窄、客户担忧锁定 | 语义 ABI 稳定，数据平面和非目标硬件可插拔 |
| Direct 路径只在理想条件成立 | 生产 p99 恶化 | ActualPathReceipt、回退率和 Host Touch 成为硬指标 |
| SSD 写放大和寿命 | TCO 反而上升 | Admission、热度/价值写入门槛、选择性写回和寿命预算 |
| 从 KVCache 过早泛化到所有 AI 状态 | 范围失控、抽象失真 | 先完成 KVCache 闭环，再抽取通用 State ABI |
| 缺少生产工作负载 | 所有价值模型只是理论 | PVT-00 和客户 Trace 是继续投入前提 |
| 技术收益无法映射为客户业务结果 | 架构成立但客户不买单 | 冻结业务事务和财务口径，以 BVT 和 Customer Value Receipt 对账 |
| 只计算节省、不计算平台增量成本 | ROI 被系统性高估 | 纳入软件、元数据、介质、网络、集成和运维的全生命周期 TCO |
| 拿不到早期芯片/固件信息或底层修改权 | Day-0 退化为普通 Adapter 跟随 | 冻结跨团队 RACI、早期访问窗口和硬件问题闭环 SLA |
| Conformance Profile 只有内部实现采用 | 所谓标准没有生态约束力 | 上游参考实现、第三方互操作、公开测试和认证注册表 |
| State Intelligence 扩张成通用数据湖 | 范围、成本和隐私风险失控 | 只采集能改变状态决策、客户 SLO 或硬件设计的数据 |
| 状态运营数据跨租户泄漏 | 数据资产反而成为安全负债 | 最小化、聚合/匿名化、租户隔离、血缘、留存和安全销毁 |

### 22.1 一个必须接受的 No-Go 条件

如果代表工作负载中：

- 复用概率不足；
- usable hit 转化率低；
- 加载大多慢于重算；
- 目标硬件原生路径无法形成稳定收益；
- 状态门禁和控制开销抵消收益；
- 无法获得目标硬件 Day-0 信息、共同修改权或跨代认证责任；
- Conformance Profile 无第三方互操作或上游采纳；
- 生产状态数据不能稳定降低 Decision Regret，也不能形成硬件改进闭环；
- 或技术净收益无法稳定转换为更多 SLO 合格业务事务、更低单位业务成本或可量化风险降低；

那么项目应缩小为 Connector、硬件 Provider 或专项加速能力，而不是继续建设完整状态平台。敢于接受这一 No-Go 条件，本身也是项目“证据优先”品味的一部分。

---

## 二十三、建议评审冻结的进一步问题

1. **对象边界**：首发版本的 KV 状态粒度是 Token Block、Layer Group、完整 Prefix 还是可组合 Extent？
2. **价值单位**：StateValueEstimate 以时间、NPU 周期、功耗、货币成本还是多目标向量表达？
3. **Tier R 权限**：谁有权决定重算——框架 Scheduler、统一池 QueryPlan，还是二者共同仲裁？
4. **Ready 责任边界**：哪些证据由 Provider 提供，哪些必须由 Connector 或统一池验证？
5. **路径真实性**：ActualPathReceipt 需要达到何种粒度，是否进入租户 SLO 和计费审计？
6. **第三方 Provider 合规**：Mooncake/LMCache/NIXL 接入时，最低必须提供哪些能力和证据？
7. **硬件反馈机制**：PVT 中发现的能力缺口如何进入芯片、驱动、固件和 SSD 产品路线？
8. **泛化门槛**：满足什么生产证据后，项目才允许扩展到 Encoder Cache 或其他 AI 状态？
9. **客户价值单位**：首个灯塔场景以请求、会话、回答、Agent 任务还是作业作为合格业务事务？
10. **财务口径**：硬件折旧/租赁、能耗、运维、SLA 损失和单位业务贡献由谁提供并签字确认？
11. **价值合同边界**：项目承诺服务 SLO、单位业务成本、业务产出还是仅提供可审计测量？
12. **价值凭证责任**：Customer Value Receipt 的基线、数据归属、对账周期和争议处理由谁负责？
13. **Day-0 组织权**：谁保证早期芯片/固件访问、底层修改、PVT 和客户首发窗口，跨团队 SLA 是什么？
14. **标准 Owner**：UBMEM/URMA KV State Profile 由谁版本化，谁批准不兼容变更，如何进入 openEuler 和上游社区？
15. **数据资产边界**：哪些状态运营字段允许跨客户聚合，保存多久，如何匿名化、审计和删除？
16. **硬件反馈门槛**：什么级别的生产证据可以进入 Core、NoC、IODie、DDRC、IO、SSD 和功耗设计决策？

---

## 二十四、最终判断

### 24.1 本项目是否具有独立于 Mooncake、LMCache 的独特价值

**有条件地具有。** 独特价值不来自竞品暂时缺少某个 Backend，而来自以下组合能否被固化并验证：

- 把 KVCache 定义为物化执行状态；
- 把语义身份和消费资格放在物理位置之前；
- 把重算作为 Tier R 纳入统一计划；
- 用 StateValueEstimate 管理 Admission、Placement、Consumption 和 Eviction；
- 用 Ready 连接硬件完成语义与推理正确性；
- 用 ActualPathReceipt 治理隐藏中转与回退；
- 用 Day-0 Native Enablement 把目标芯片能力第一时间转化为默认可验证路径；
- 用 Conformance Profile 和认证注册表把一次性适配沉淀为生态资产；
- 用 State Operations Data 与 Inference State Intelligence 持续降低 Decision Regret 并反哺硬件；
- 用 Customer Value Contract 把技术目标绑定到客户业务事务；
- 用 Customer Value Receipt 把状态决策、服务 SLO 和财务结果形成可审计对账；
- 用目标硬件和生产证据形成软硬件反馈飞轮；
- 用开放 Provider 保持生态速度和自主控制权。

### 24.2 竞争壁垒现在是否已经存在

**尚未。** 目前存在的是一套比“多级缓存”更有辨识度的架构主张。Day-0 Lead Time、UBMEM/URMA 性能包络、Conformance Coverage、上游采纳、State Intelligence 决策收益和硬件反馈都尚无完成证据。只有这些证据与四类决定性实验、PVT、客户 BVT 和生产价值凭证共同形成后，项目才会成为客户愿意购买且竞品难以复制的壁垒。

### 24.3 最终建议

项目应继续推进，但必须将立项和研发的中心问题从：

> “我们能支持多少层、多少协议、多少容量？”

改为：

> “我们能否在目标芯片发布窗口内，把 Core、NoC、IODie、内存、IO、UBMEM/URMA、网络、SSD、NPU、功耗与 RAS 能力编译成可认证的推理状态路径；再用生产状态数据持续改进决策，让客户在相同模型质量、业务 SLO 和硬件预算下完成更多有价值的业务事务，并以实际路径和业务结果完成对账？”

如果能够回答这一问题，本项目就形成“Day-0 首发—标准认证—状态智能—硬件反哺—客户价值”的完整竞争力闭环。Mooncake、LMCache、Dynamo/KVBM/NIXL 可以继续作为数据平面、Connector 或重要对照，本项目仍凭借目标平台第一方责任、跨层标准、生产决策知识和国产硬件共同设计形成独立价值。如果最终只能证明多级缓存、URMA Adapter、SSD Offload 或技术 KPI 改善，而不能证明跨代认证、决策学习、硬件反馈和客户业务结果，则项目必要性不足。

---

## 附录 A：主要一手资料

### A.1 项目内材料

- `统一异构KVCache存储池总体架构与SRS评审导读_V2.2评审稿.md`
- `统一异构KVCache存储池_全量需求树_V2.3.1_专属术语中文释义修订版_Excel兼容性修复版.xlsx`
- `KVCache SRS需求列表 V2.2_传输底座视角_建议修订版.xlsx`
- `统一异构KVCache存储池_关键技术原型验证清单_V1.5_SRS-V2.2对齐修订版.xlsx`

### A.2 Mooncake 与 LMCache

- [Mooncake 官方仓库](https://github.com/kvcache-ai/Mooncake)
- [Mooncake TENT Transport Selector](https://kvcache-ai.github.io/Mooncake/design/tent/transport-selector.html)
- [Mooncake Store](https://kvcache-ai.github.io/Mooncake/design/mooncake-store.html)
- [Mooncake Build Guide：Ascend Direct 与 UBShmem](https://kvcache-ai.github.io/Mooncake/getting_started/build.html)
- [Mooncake Ascend Direct Transport](https://kvcache-ai.github.io/Mooncake/design/transfer-engine/ascend_direct_transport.html)
- [Mooncake Kunpeng UB/URMA Transport](https://kvcache-ai.github.io/Mooncake/design/transfer-engine/kunpeng_ub_transport.html)
- [LMCache 官方仓库](https://github.com/LMCache/LMCache)
- [LMCache 官方文档](https://docs.lmcache.ai/)
- [LMCache MP Architecture](https://docs.lmcache.ai/mp/architecture.html)
- [LMCache Metrics Reference](https://docs.lmcache.ai/production/observability/metrics.html)

### A.3 NVIDIA

- [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo)
- [Dynamo Introduction](https://docs.nvidia.com/dynamo/getting-started/introduction)
- [Dynamo Overall Architecture](https://docs.nvidia.com/dynamo/v-0-9-0/design-docs/overall-architecture)
- [Dynamo Planner](https://docs.nvidia.com/dynamo/components/planner)
- [Dynamo KVBM Guide](https://docs.nvidia.com/dynamo/latest/user-guides/kv-cache-offloading)
- [KVBM Design](https://docs.nvidia.com/dynamo/v-0-9-0/design-docs/kvbm-design)
- [NVIDIA NIXL](https://github.com/ai-dynamo/nixl/blob/main/docs/nixl.md)
- [NVIDIA GPUDirect Storage](https://docs.nvidia.com/gpudirect-storage/overview-guide/)
- [DGX GB Rack Scale Software](https://docs.nvidia.com/dgx/dgxgb200-user-guide/software.html)

### A.4 其他方案与厂商

- [SGLang HiCache](https://docs.sglang.io/docs/advanced_features/hicache_design)
- [vLLM 官方仓库](https://github.com/vllm-project/vllm)
- [vLLM Multi-tier KV Offloading RFC](https://github.com/vllm-project/vllm/issues/38260)
- [FlexKV](https://github.com/taco-project/FlexKV)
- [AIBrix](https://github.com/vllm-project/aibrix)
- [InfiniStore Architecture](https://bytedance.github.io/InfiniStore/design.html)
- [MemServe](https://arxiv.org/abs/2406.17565)
- [DeepSeek 3FS](https://github.com/deepseek-ai/3FS)
- [vLLM Ascend KV Pool](https://docs.vllm.ai/projects/ascend/en/main/user_guide/feature_guide/kv_pool.html)
- [AMD hipFile](https://rocm.docs.amd.com/projects/hipFile/en/develop/index.html)

### A.5 UnifiedBus、UBMEM 与 URMA

- [openEuler URMA API Guide](https://docs.openeuler.org/zh/docs/24.03_LTS_SP3/unifiedbus/unifiedbus/urma/URMA%20API%20Guide.ch.html)
- [openEuler UB OS Component](https://www.openeuler.org/zh/projects/ub-os-component/)
- [openEuler UB Service Core](https://www.openeuler.org/en/projects/ub-service-core/)

---

## 附录 B：建议的竞争性验收看板

| 类别 | 指标 | 判断的问题 |
|---|---|---|
| 客户业务结果 | SLO 合格请求/会话/回答/Agent 任务、业务成功率 | 客户实际上多完成了什么？ |
| 客户财务价值 | 单位合格业务事务成本、避免容量、增量毛利、避免损失、平台增量 TCO | 技术收益是否形成可对账的经营回报？ |
| 状态资格 | raw hit、usable hit、资格失败分类 | 状态是否真的可用？ |
| 状态价值 | Expected/Realized State Value、正价值复用率 | 复用是否真的比重算划算？ |
| 决策质量 | Decision Regret Rate、预测误差 | QueryPlan 是否优于静态策略？ |
| Day-0 首发 | Enablement Lead Time、首批可灰度版本、首发缺陷关闭时间 | 新芯片能力是否第一时间成为可用产品路径？ |
| 标准与生态 | Conformance Coverage、跨代通过率、第三方互操作、上游采纳 | 一次性适配是否沉淀为生态资产？ |
| 状态智能 | 模型版本、漂移、策略收益、反例库、回滚成功率 | 生产数据是否持续改善决策而非只增加指标？ |
| 硬件反哺 | 已关闭的芯片/固件/驱动需求、性能包络改善 | 项目是否真正影响下一代国产硬件？ |
| 业务 SLO | TTFT/TPOT p50/p95/p99、吞吐 | 用户是否获得稳定收益？ |
| 路径真实性 | planned/actual path、Actual Path Fidelity、fallback | 宣称的路径是否真实？ |
| 主机参与 | Host Payload Touch、CPU、NUMA、内存带宽 | 是否把代价转移到主机？ |
| 硬件效率 | effective BW、NPU 利用率、队列、SSD IOPS/BW | 软硬件能力是否兑现？ |
| 正确性 | wrong/stale/unauthorized consume、Rank 不一致 | 错误状态是否在消费前被拦截？ |
| 可靠性 | 回退成功率、恢复时间、故障 SLO | 故障时是否仍能受控服务？ |
| 基础设施成本 | 单位有效 Token 成本、功耗、SSD 写放大与寿命 | 底层资源经济性是否改善？ |

看板必须同时包含重算基线、至少一种成熟开源基线和目标硬件通用路径基线，并明确客户业务事务、单位成本和财务换算口径；否则只能支持技术结论，不能支持客户竞争力结论。
