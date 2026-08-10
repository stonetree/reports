# srs_v2.4.1.xlsx 需求全面修订计划和修订方案

基于评审专家A给出的**第一版**和**补充版**评审意见，当前 v2.4.1 版本的 SRS 存在范围过大、层级边界不清、指标过度承诺等问题。更关键的是，基于补充版评审意见的纠正，我们必须摒弃“纯软件先行、硬件能力后置”的错误思路，转而采用**“软硬联合、双闭环验证”**的架构演进路线。

本次修订的核心目标是将 SRS 重构并收敛为一份**硬件感知、边界清晰、分阶段可落地**的软件需求规格说明。当两版评审意见冲突时，本方案严格以**补充版（A.02）**为准。

以下为具体修订计划与方案：

---

## 一、 核心修订思想与阶段规划重构

**核心思想修正：硬件能力验证与软件语义定义必须前置；但高风险硬件功能的业务化收益兑现可以分阶段。**

不再试图一次性建设全异构、全硬件、全路径最优的 KVCache 系统，也不做脱离硬件真相的“纯软件 MVP”。修订后的 SRS 将明确按以下四个阶段划分目标：

*   **Phase 0：软硬协同基线验证阶段（前置必做）**
    *   **目标**：确定系统硬件边界，防止软件闭门造车。
    *   **重点**：硬件能力探测（Capability Matrix）、注册内存池、可见性 Fence、SG 描述符约束、每条路径的 telemetry 与 microbenchmark。
*   **Phase 1：硬件感知的最小业务闭环（MVP主线）**
    *   **目标**：在真实硬件路径下验证 Prefix hit 能否稳定转化为 usable hit，且不拖垮 TTFT/TPOT。
    *   **重点**：至少打通本地 HBM↔DDR 或 Remote DDR (RDMA) 一条真实数据路径；基于实测代价的 Cost-aware QueryPlan 与 load-vs-recompute admission；KV object 状态机；Pull-to-provided-device-pointer 内存解耦契约。
*   **Phase 2：多 Tier KVCache 存储池扩展**
    *   **重点**：引入 SSD/Object tier、Tiering Manager、分布式目录镜像、状态感知驱逐（eviction）、Watermark 准入控制。
*   **Phase 3：平台增强硬件高级特性的业务化**
    *   **重点**：将 Phase 0/1 旁路验证过的高风险能力正式业务化。包括 UB/C2C direct view 活跃态直读、DPU 卸载、硬件多播 (Multicast)、硬件页迁移、On-the-fly 压缩/解压。

---

## 二、 模块架构与层级边界重构

针对原文档中 L1~L4 层级穿透、ID 归属混乱的问题，重新明确各层边界，并在 Excel 中对错位的需求进行重新归类或删改。

*   **L1 推理调度层 (TM1)**：**只做**准入 (admission)、路由 (routing)、批处理决策、load-vs-recompute 判定。**绝不解释或操作物理底层状态**。
*   **L2 KVConnector层 (TM2)**：**只暴露**稳定协议（QueryPlan、AttachHandle、Descriptor、ErrorCode、Intent）。它是协议映射层，不是物理状态真相源。
*   **L3 Transfer / KV Store Manager层 (TM3)**：**核心状态源**。维护 KV object 状态、placement、manifest、目录索引、策略引擎。
*   **L4 Transport / HAL层 (TM4/5)**：暴露硬件能力抽象、句柄、Fence、Registered Pool、Telemetry。**不决定业务语义**。

---

## 三、 具体需求的优先级调整矩阵

在 Excel 中，我们将对需求列表的优先级（P0/P1/P2/P3）进行大规模调整，严格对齐“软硬双闭环”战略。

### 1. 必须提升/保留为 P0 的“硬件基建”需求
_这些是 Phase 0/1 必须前置落地的底座，没有它们，软件 QueryPlan 就是空中楼阁。_

| 需求ID与功能点 | 修订后优先级 | 修订原因 |
| :--- | :--- | :--- |
| **L4-HW-SemCapTable-050** (语义能力表) | **P0 (保留)** | 软硬协同的入口，必须前置。 |
| **L4-HW-StorageLayoutCapability-067** (布局约束) | **提升至 P0** | 直接约束 manifest / SG descriptor 的生成，必须前置。 |
| **L4-OL-RegisteredPool-053** (注册内存池) | **P0 (保留)** | 高频 KV 传输必须池化注册内存，否则注册开销掩盖收益。 |
| **L4-RDMA-RemoteExtentHandle-071** (远端句柄) | **P0 (保留)** | Remote DDR / RDMA 主路径的必备抽象。 |
| **L4-CO-ExtentVisibilityFence-066** (可见性 Fence)| **P0 (保留)** | Ready bitmap 和 attach 正确性的物理底座。 |
| **L4-FT-RASErrorMap-061** (硬件RAS错误映射) | **P0 (保留)** | 硬件错误必须能及时映射到 recompute 降级。 |
| **L3-OB-PerPathTelemetry-047** (单路径遥测) | **P0 (保留)** | 没有动态观测，无法做实时的硬件联合调度和 Cost Model 决策。|
| **L4-QO-TrafficClass-056** (流量分类与QoS) | **P0/P1** | 至少软件队列与基础硬件隔离要前置。 |

### 2. 必须保留为 P0 的“软件核心语义”需求
_这是 Phase 1 业务闭环的骨架，确保“可正确消费”。_

| 核心能力方向 | 涉及的核心需求条目 |
| :--- | :--- |
| **租户与身份隔离** | L1-SC-TenantIsolation-017, L2-KV-SemanticIdentity-036 |
| **执行与存储边界** | L2-CONN-BufferContract-040 (Pull-to-provided-device-pointer) |
| **生命周期与状态** | L3-MS-KVObjectStateMachine-058, L3-MS-ReplicaPlacementState-059 |
| **可消费判断与路由**| L3-MS-ConsumeEligibility-060, L3-SE-QueryPlanFastPath-072 |
| **经济性准入计算** | L1-PM-PrefixBudget-001, L1-RT-Admission-007 |
| **快速索引与查找** | L3-MS-HotLocalIndex-070, L3-MS-RangeBatchLookup-071 |
| **并发与一致性保护**| L3-CO-VisibilityReadyBitmap-064, L3-CO-AttachDetachLease-063, L3-CO-RefCountLifecycle-086 |

### 3. 必须降级或后置业务化的“高风险/探索性”需求
_这些能力在 Phase 0 进行旁路探索，但不应成为 Phase 1 的交付承诺，避免阻塞主线。_

| 原需求 / 功能点 | 原优先级 | 修订后优先级 | 修订原因与处理方案 |
| :--- | :--- | :--- | :--- |
| **L3-MC-CompactionEngine-056** (在线 Compaction) | P0 | **P1/P2** | 极复杂，MVP先做 allocator / fragmentation telemetry。 |
| **L3-MC-IntelligentMigration-057** (主动迁移) | P0 | **P1** | MVP 先做 safe eviction 与 watermark 控制。 |
| **L4-UB-C2C-UNIFY-002** (UB Direct View) | P2 | **Phase 2/3 (旁路验证)**| Active decode 下直接 view 需要实测 TPOT stall，不可通用化。 |
| **L4-NET-OFFLOAD-DPU-001** (DPU 卸载) | - | **Phase 3** | DPU 调试极复杂，严重依赖硬件生态。 |
| **L3-TRANS-MUL-ENGINE-003** (1→N O(1) 多播) | P1 | **Phase 3** | 先做 tree relay / batched unicast，硬件多播不承诺主线。 |
| **L4-RDMA-MUL-FABRIC-002** (RDMA 组播) | P2 | **Phase 3** | 同上。 |
| **L4-CO-PageMigration-063** (硬件页迁移/原子TLB) | P1 | **Phase 3** | 软件层无法普遍保证底层硬件页迁移的原子性。 |
| **L4-CO-AtomicRemapPrimitive-065** (硬件原子 remap)| P0 | **Phase 2/3** | 降级为 L3 逻辑 extent remap + RCU 实现。 |

---

## 四、 需求规范性与描述文本修订原则

在刷新 `srs_v2.4.1.xlsx` 的具体条目时，需严格执行以下文本修订操作：

1.  **剥离具体实现手段**：将需求描述中绑定的“Cython/SIMD/xxHash/100 Virtual Nodes/128K Cache/100ms心跳”等硬性实现细节移除，改为“性能目标约束”，将具体选型放入“参考设计建议”列。
2.  **纠正过度泛化的“Direct View”**：
    *   明确 Decode-active KV 默认 HBM 驻留或 staging/copy-to-HBM。
    *   Direct View 必须仅限于 metadata、小对象、warm preview，或在 Cost Model 明确证明不恶化 TPOT 的场景下使用。
3.  **修正过度承诺的绝对指标**：
    *   取消通用的“<500ns”、“5μs以内远端查询”等不切实际的统一承诺。
    *   改为依赖 Capability Matrix 和实测指标：本地确定 miss/hot hit 可追求 5-50μs 级；远端查询必须使用 P50/P99、RTT 分布进行概率约束。
    *   将“路由正确率100%”改为“Fallback 成功率”与“SLO 违约率限制”。
4.  **统一对象发布一致性模型**：
    *   取消控制面 3PC 与分布式 WAL 的复杂承诺。
    *   明确采用：单写者对象发布流水线 (object publication pipeline)，配合 visibility fence 实现最终一致性。
5.  **Cost Model 从静态转为动态实测驱动**：
    *   `CostAwareReturn` 和 QueryPlan 必须基于 `measured_p50_latency`, `measured_bw`, `queue_depth`, `registration_cost` 等实时硬件 Telemetry 输出判定，而不能是静态阈值判断。

---

## 五、 待补充的关键需求模块 (需在 Excel 中新增)

为确保整个架构闭环可验证、有依据，将在 Excel 需求列表中补充以下横向体系约束：

1.  **新增 Workload Model 定义**：明确前缀复用率、上下文长度分布、Tenant 数量、Batch size 等边界，因为“消除冗余计算”、“压降 TCO”等商业指标严重依赖 Workload 假设。
2.  **新增正确性验收契约 (Correctness Contract)**：
    *   Semantic identity 穿透测试（防止串组户/模型/Tokenizer版本）。
    *   Template version / Adapter ID 不匹配防护。
    *   Stale hit / Tombstone resurrection 测试。
    *   滑动窗口 (Sliding-window) / MLA / GQA 的张量 Layout 兼容性约束。
3.  **明确端到端观测与验收指标体系**：
    *   废弃单一“命中率”，拆解为：`raw_hit`, `usable_hit`, `abandoned_hit`, `recompute_after_hit` 等。
    *   定义 **TTFT benefit**：Hit 路径与 Recompute 路径在 P50/P99 下的时延差值。
    *   定义 **TPOT interference**：后台迁移/读写流量对前台 Decode TPOT 造成的劣化比例限制。
4.  **新增“硬件联合验证矩阵”表格**：强制要求每一个业务目标（如“降低TTFT”、“提高HBM利用率”）必须映射到至少一个硬件能力探测指标（如“RDMA copy latency”、“Copy engine interference”），消除软硬脱节风险。

---

## 六、 修订实施步骤

1.  **备份原件**：基于当前的 `srs_v2.4.1.xlsx` 创建工作副本。
2.  **大纲对齐**：首先清理 L1-L4 的分类，将错位的需求 ID（如 L1 表中的 L3 需求、L2 表中的路由控制）调整到正确的层级。
3.  **优先级的逐行清洗**：依据“三、具体需求的优先级调整矩阵”，重置所有硬件卸载、协议加速、高级特性的优先级，并标注对应的 Phase 阶段。
4.  **术语与指标校准**：逐行审阅需求“验收方法”和“技术痛点”列，去除“100%正确”、“纳秒级延迟”、“SIMD汇编”等不合理描述，替换为基于 Telemetry 的统计指标约束。
5.  **内容增补**：增加“正确性验收”与“硬件联合验证矩阵”等新的需求类目。
6.  **交付定稿**：输出最终版的 `srs_v2.4.1.xlsx` 供再次审阅。
