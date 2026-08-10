# 统一异构 KVCache 存储池基础设施软件栈 —— 正式立项申请汇报文稿

> **汇报对象**：技术 CTO / 部门主管 / 首席架构师 / 开发团队主管 / 市场部门主管  
> **汇报阶段**：立项申请与前置验证决策汇报 (Formal Project Approval Presentation)  
> **配套 PPT 交付件**：[统一异构KVCache存储池_正式立项申请汇报文稿.pptx](file:///d:/codes/reports/kvcache/unified_kv_memory/%E7%BB%9F%E4%B8%80%E5%BC%82%E6%9E%84KVCache%E5%AD%98%E5%82%A8%E6%B1%A0_%E6%AD%A3%E5%BC%8F%E7%AB%8B%E9%A1%B9%E7%94%B3%E8%AF%B7%E6%B1%87%E6%8A%A5%E6%96%87%E7%A8%BF.pptx)  
> **排版风格**：正式 Executive Presentation 格式，统一采用“【技术手段/机制】+【功能协同/量化收益】”两段式标准陈述句式；严禁口水化与非正式表达；聚焦特性功能与可量化性能收益。

---

# 目录与文稿结构

- **Slide 1: 正式立项申请封面 (Title & Executive Metadata)**
- **Slide 2: 战略定位与核心痛点 (Strategic Vision & Core Pain Points)**
- **Slide 3: 核心竞争力与三大阵营对比 (Core Competitiveness & Baseline Matrix)**
- **Slide 4: 全栈系统架构沙盘 (5-Layer System Architecture Sandbox)**
- **Slide 5: 四大硬核技术方向与关键机制拆解 (Four Technical Pillars Breakdown)**
- **Slide 6: 分阶段实施路线图与 E0~E5 证据门里程碑 (Execution Roadmap & Evidence Gates)**
- **Slide 7: 投资决策逻辑、一级 KPI 树与资产兜底机制 (Investment Decision & Risk Management)**
- **附录: 评审高频考问与回应策略 (Executive Q&A Strategy)**

---

## Slide 1: 正式立项申请封面 (Title Slide)

### 幻灯片标题
**统一异构 KVCache 存储池基础设施软件栈**

### 幻灯片副标题
**面向国产 AI 硬件生态的第一方推理状态系统工程立项申请**  
*—— 深度整合 UBMEM/URMA 硬件特性，重构 AI Infra 执行状态价值底座*

### 汇报元数据
* **汇报对象**：技术 CTO / 部门主管 / 首席架构师 / 开发团队主管
* **汇报阶段**：立项申请与前置验证决策

---

## Slide 2: 战略定位与核心痛点 (Strategic Vision & Core Pain Points)

### 执行摘要 (Executive Summary)
> **有机融合 UBMEM/URMA 国产硬件原语与状态智能决策编译，协同控制面超低时延访问与数据面 CPU 零触碰搬运，构建面向国产 AI 硬件的第一方推理状态基础设施系统工程。**

### 核心痛点与第一方系统工程破局对比

#### 1. 行业 AI 推理基础设施核心痛点
* **物理 Raw Hit 不等于逻辑可消费**：传统缓存方案缺少统一语义约束，频发版本错位或布局不匹配，导致推理错误或隐性系统 Stall。
* **盲目数据搬运耗时慢于 Prefill 重算**：无差别追求 Cache Hit Rate 导致跨节点搬运耗时超过 NPU 本地计算，拖累 TTFT 尾部延迟。
* **通用开源无法深度使能国产微架构**：Mooncake / LMCache 适配通用硬件，无法接入国产芯片非公开计数器与微架构控制权。

#### 2. 第一方系统工程破局路径
* **状态语义身份与资格前置校验**：基于 `KVSemanticIdentity` 与 `Ready` 协议治理，确保错误或半写状态绝对隔离，实现消费零错误。
* **基于 SLO 的 QueryPlan 全路径编译**：将 View、Load、Move 与 Recompute 统一编译择优，确保持续选择延迟最低与算力最优路径。
* **第一方软硬协同与 Day-0 责任闭环**：深度融合 UBMEM/URMA 特性，建立“生产瓶颈 ➔ 软件优化 ➔ 驱动修改 ➔ 硬件反哺”演进飞轮。

---

## Slide 3: 核心竞争力与三大阵营对比 (Core Competitiveness & Baseline Matrix)

### 1. 双视角价值矩阵 (Dual-Perspective Value Matrix)

#### 外部客户业务价值视角 (Customer Value)
* **TTFT 交互体验**：正价值复用与流式加载，实现 **TTFT p99 时延降低 ≥20%**。
* **并发服务能力**：SSD 经济容量池分层预取，实现 **HBM 有效并发容量提升 ≥30%**。
* **服务质量保障**：控制面超低时延与 QoS 隔离，平抑前后台混流抢占抖动控制在 **5% 以内**。

#### HOST 侧 / 集群侧 / 硬件价值视角 (Host & Hardware Value)
* **CPU 正文解绑**：URMA 零拷贝与 UBMEM 预注册结合，实现 **CPU 正文触碰完全归零 (0-Touch)**。
* **总线有效带宽**：SG 批量描述符与 Fence 屏障优化，实现 **总线/网络有效带宽利用率 ≥80%**。
* **异构介质池化**：原生打通 HBM/DDR/SSD 及 L3 芯片内存储，摆脱 Host DDR 必经瓶颈。

---

### 2. 三大阵营对比分析表 (4-Way Baseline Comparison Table)

| 对比维度 | 通用开源传输 (Mooncake) | 框架侧复用 (LMCache) | NVIDIA 全栈 (Dynamo/NIXL) | 本项目第一方系统工程 |
|---|---|---|---|---|
| **定位中心** | 高性能传输 Engine 与对象存储 | 推理框架侧 Connector 与 Chunk 缓存 | NVIDIA 平台的完整推理系统工程 | **国产目标硬件的第一方推理状态系统工程** |
| **介质形态** | DRAM / NVMe 静态扩展 | DRAM / Disk / Remote 通用层级 | HBM / DRAM / NIXL 传输 | **原生支持 HBM / DDR / SSD 及 L3 芯片内存储** |
| **决策要素** | 静态传输 Intent 与 QoS 选择 | Prefix 匹配与生命周期策略 | Event 驱动 Router 与计算重叠 | **以 KVCache 价值/SLO/微架构代价为决策编译要素** |
| **硬件关系** | 广泛适配通用网卡与传输驱动 | 依赖标准 OS / PCIe 设备接口 | 与 NVIDIA 硬件及 CUDA 深度绑定 | **深度使能 UBMEM/URMA，第一方联合修改与 Day-0 认证** |

---

## Slide 4: 全栈系统架构沙盘 (5-Layer System Architecture Sandbox)

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│ 1. 业务场景层 (Business Application Layer)                                                         │
│    [ 长上下文 RAG 检索 ]    [ 多轮对话 Agent 交互 ]    [ Prefill-Decode 独立分离 ]    [ 多租户共享上下文 ]│
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 2. 推理框架与复用接入层 (Framework & Reuse Layer)                                                   │
│    [ vLLM / SGLang Connector ]       [ 统一契约: KVAccessIntent ]       [ 句柄/生命周期: AttachHandle ]│
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 3. 控制与语义管理平面 (Control & Management Plane) ★ 核心攻关                                       │
│    [ SemanticIdentity 校验 ]  [ PrefixDirectory 目录 ]  [ ConsumeEligibility ]  [ Placement 副本放置 ] │
│    [ QueryPlan 物理代价编译 ]                          [ Rank Consensus 保持 ]                            │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 4. 传输与数据面 (Transport & Data Fabric Layer) ★ 核心攻关                                          │
│    [ URMA 零拷贝 Engine ]    [ UBMEM 预注册内存池 ]    [ SG 描述符编译器 ]    [ Fence 可见性屏障 ] │
│    [ 硬件 CRC 完整性校验 ]                             [ ActualPathReceipt 审计凭证 ]                     │
├──────────────────────────────────────────────────────────────────────────────────────────────────┤
│ 5. 异构介质与硬件底座 (Heterogeneous Hardware Fabric Layer)                                         │
│    [ NPU HBM (片上/片外) ]    [ Host DDR (条件中转) ]    [ NVMe SSD (经济容量) ]    [ L3/NoC 芯片内结构 ] │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Slide 5: 四大硬核技术方向与关键机制拆解 (Four Technical Pillars Breakdown)

### 方向一：状态语义身份与可消费资格治理 (KVSemanticIdentity & Eligibility)
**大标题陈述：有机融合 `KVSemanticIdentity` 编码与 `Ready` 协议校验，实现物理 Raw Hit 向正价值可消费命中的精准转换，构建零错误消费的状态安全防线。**
* **1.1 `KVSemanticIdentity` 机制**：深度整合 Token 序列、模型 Hash、Layout 布局与租户隔离标识，实现物理 Cache 命中与推理语义一致性的毫秒级精准校验。
* **1.2 `Ready` 协议与 Lease 租约**：协同部署异步 Write Fence 与分布式租约，实现半写状态、跨 Rank 不一致状态的物理隔离与安全拒绝。
* **1.3 消费资格准入控制**：严格执行价值评估与安全鉴权，实现负收益加载请求的实时拦截与正价值状态的精准准入。

---

### 方向二：基于 QueryPlan 的全路径代价决策编译 (SLO-driven QueryPlan)
**大标题陈述：有机整合 NPU 微架构计数与拓扑代价模型，实现 View、Load、Move 与 Recompute 的统一编译择优，构建面向 SLO 目标的高性能决策引擎。**
* **2.1 多路径物理代价编译**：基于目标硬件总线带宽、PCIe 饱和度及 NPU 负载，实现 Prefill 重算与远程 Fetch 耗时的实时量化对比。
* **2.2 Direct View / Load 切换**：联合开发只读 Shared Memory 映射与 HBM Direct Copy 模式，实现元数据 View 与正文 Load 的最优路径选择。
* **2.3 前后台混流 QoS 平抑**：协同部署 Prefetch 预取优先级队列与带宽限制器，实现背景数据搬运对在线 Decode 阶段 TPOT 干扰的有效平抑。

---

### 方向三：国产硬件 UBMEM/URMA 原生传输与异构池化 (Zero-Touch Transport)
**大标题陈述：有机链接 UBMEM 预注册内存池与 URMA 零拷贝总线，实现大块数据传输与高频元数据同步的无缝协同，构建 HBM↔SSD 高性能传输底座。**
* **3.1 UBMEM 预注册池化管理**：实时建立 HBM、Host DDR 与 SSD 统一地址空间映射，实现跨介质内存分配延迟小于 10 微秒。
* **3.2 SG 批量描述符编译搬运**：开放 Scatter-Gather 描述符离线编译契约，实现物理不连续 Block 数据在 URMA 管道中的单次 I/O 零触碰传输。
* **3.3 硬件 CRC 与 Fence 屏障**：协同实施传输端到端硬件校验与 Visibility Barrier，保障数据传输完整性与 DMA 可见性。

---

### 方向四：第一方硬件能力 Day-0 认证与闭环反哺 (Hardware Co-design)
**大标题陈述：有机统筹微架构遥测数据与第一方驱动修改通道，实现生产瓶颈向硬件演进的需求转化，构建软硬协同代际演进闭环。**
* **4.1 微架构遥测与反例库建设**：确立 NoC 争用、TLB Stall 及 PCIe 写放大遥测指标，实现决策后悔 (Decision Regret) 的自动归因与策略演进。
* **4.2 Day-0 驱动固件联合认证**：联合开发硬件能力 Conformance 准入测试集，实现芯片新特性在推理系统中的 Day-0 快速使能。
* **4.3 生产瓶颈反哺硬件设计**：构建基于真实场景的 KV 状态访问 Profile，实现对下一代国产芯片 Interconnect 与 Cache 架构的量化反向定义。

---

## Slide 6: 分阶段实施路线图与 E0~E5 证据门里程碑 (Execution Roadmap & Evidence Gates)

### 1. P0~P3 分阶段实施路线图
* **阶段 0: 立项预研 (4~5 周)**：交付 PVT-00~07 原型验证包 ➔ Trigger: Go / Conditional / No-Go
* **阶段 1 (P0): 最小闭环 (3 个月)**：实现 SemanticIdentity 与 URMA 底座 ➔ 达成 E1 证据门
* **阶段 2 (P1): 框架集成 (4 个月)**：对接 vLLM/SGLang 与 QueryPlan ➔ 达成 E2 证据门
* **阶段 3 (P2): 生产调优 (4 个月)**：长上下文 QoS 与多介质池化 ➔ 达成 E3/E4 证据门
* **阶段 4 (P3): 商业交付 (3 个月)**：第一方认证体系与开放规范 ➔ 达成 E5 证据门

---

### 2. E0~E5 证据门解锁标准

| 证据门等级 | 对应项目阶段 | 解锁条件与硬核验证标准 | 不通过 / 失败处置动作 |
|---|---|---|---|
| **E0 (立项充分性)** | 立项评审阶段 | SRS 需求闭包完成，PVT-00~07 原型验证设计冻结，硬件合作 RACI 明确。 | 补充验证材料或取消平台立项。 |
| **E1 (能力成立)** | P0 阶段结束 | URMA/UBMEM 主路径 Host Touch 为 0，wrong/stale 错误状态消费率严格为 0。 | 禁用受影响硬件路径，退化为通用 Provider。 |
| **E2 (决策更优)** | P1 阶段结束 | Oracle Replay 显示 QueryPlan 决策后悔率 <5%，显著优于静态规则。 | 限定白名单场景，退化为静态分层策略。 |
| **E3 (系统增益)** | P2 阶段中期 | 相比最佳开源同等调优基线，TTFT p99 降低 ≥20%，HBM 容量提升 ≥30%。 | 缩小适用场景范围，停止完整平台扩张。 |
| **E4 (客户价值)** | P2 阶段结束 | 客户联创环境中单位 Token 综合 TCO 降低，SLA 违约率大幅下降。 | 转为专项硬件加速工具箱交付。 |
| **E5 (壁垒持续)** | P3 阶段交付 | 新芯片代际 Day-0 适配周期缩短 50%，形成开放规范与生产反例库。 | 定位为一次性集成项目，不宣称持久壁垒。 |

---

## Slide 7: 投资决策逻辑、一级 KPI 树与资产兜底机制 (Investment Decision & Risk Management)

### 一、“分阶段授权”投资决策逻辑
* **批准“验证权”，非无条件买单**：立项评审回答的是“投资期望值是否为正”，而非“终局产品是否已验收”。
* **4~5 周有限投入购买关键事实**：先投入 4~5 周资源完成 PVT-00~07 原型验证，验证 Saved-Prefill 收益与 URMA 零触碰可行性。
* **根据证据逐级释放资源**：若 PVT 达标则进入 P0 建设；若部分达标则限定白名单；若不达标则启动 No-Go 止损。

### 二、项目一级 KPI 树与防误导护栏
* **★ 一级核心指标**：SLO 合格业务事务产出 (单位：合格请求/机架/天)
* **效率与成本指标**：单位 Token 综合 TCO 成本 (元/百万 Token) | TTFT p99 降低 ≥20%
* **质量与防误导护栏**：错误状态消费率严格为 0 | 严禁空泛 Hit Rate，必须按正价值 Usable Hit 对账

### 三、No-Go 止损与沉淀资产路径 (Asset Recovery)
* **UBMEM/URMA Provider**：作为独立高性能传输插件沉淀，贡献至通用开源社区。
* **Descriptor Compiler**：作为物理不连续 I/O 描述符离线编译工具箱复用。
* **Conformance Suite**：转为国产硬件 AI 状态传输的标准测试与性能认证工具包。

---

## 附录：评审高频考问与回应策略 (Executive Q&A Strategy)

### Q1: 开源 Mooncake 和 LMCache 演进迅速，为何不直接等待开源演进？
> **回应策略**：开源社区非常优秀，但通用开源不会自动为特定国产芯片承担第一方责任。通用开源追求通用适配，无法接入国产芯片非公开的微架构计数器，也无法获得驱动/固件联合修改与 Day-0 交付窗口。本项目定位为特定国产硬件的第一方系统工程，与 Mooncake/LMCache 不是简单竞争关系，本项目可将其作为 Provider 接入，重点攻关底层硬件使能与状态价值决策。

### Q2: 为何当前不能直接提供实测胜出 Mooncake 20% 的终局数据？
> **回应策略**：项目尚处于立项论证阶段，要求当前提供终局产品实测会陷入“未立项没有证据、没有证据不能立项”的死循环。我们提请高层批准的是“以有限投入购买关键事实”的 4~5 周 PVT 验证权。我们已经设计了 8 项可证伪的原型验证 (PVT-00~07) 和严格的 No-Go 退出机制，将用数据和事实说话，逐级放行后续投入。

### Q3: 若项目中途遇到硬件合作不到位或性能不达预期，如何控制风险？
> **回应策略**：我们在架构上设计了严格的退化与隔离机制。在控制面，QueryPlan 若不胜出可退化为静态规则；在数据面，URMA 若受限可退化为标准 Socket/DDR 通道；在项目管理上，若触发 No-Go 规则，项目将自动收缩为 Provider 或底层加速工具箱，前期研发沉淀的描述符编译器与测试集将全量保留，确保投资安全。
