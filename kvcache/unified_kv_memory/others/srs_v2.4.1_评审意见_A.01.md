我会按“目标是否成立 → 架构边界是否清晰 → 每层需求可实现性 → P0/P1优先级是否合理 → 关键风险与收敛建议”来评审，并会核对当前主流框架/传输栈的事实基础。

我已经核对了 vLLM、SGLang HiCache、Mooncake、NIXL/LMCache 的当前公开资料。初步判断是：方向成立，但 v2.4 不是“可直接开发的 SRS”，更像“技术路线全集 + 研究议程 + 部分硬件规格诉求”的混合体，需要收敛成分阶段、可验收、平台可落地的需求基线。

## 总体评审结论

这份 **KVCache SRS v2.4 的技术方向是成立的**：围绕长上下文、Prefix KV 复用、PD 分离、分布式 KV 存储池、异构传输路径、可消费状态判定、租约/引用计数/可见性控制等问题，抓住了当前在线推理系统的真实瓶颈。当前 vLLM 已有 hash-based Automatic Prefix Caching，核心思路就是按 KV block 哈希复用已处理请求的 KV cache；vLLM 也已有 NixlConnector、MooncakeStoreConnector 等 KV transfer / shared KV store 方向的公开能力。SGLang HiCache 也已经把 RadixAttention 扩展到 GPU HBM、Host memory、外部存储 backend 的多级 KV cache，并支持 tier-aware routing。Mooncake、NIXL、LMCache 也都在朝“KVCache 成为推理系统一等存储/传输对象”的方向演进。([vLLM](https://docs.vllm.ai/en/stable/design/prefix_caching/))

但从 **SRS 可落地性** 看，v2.4 还不能直接作为开发基线通过。它现在更像是：

> “统一异构 KVCache 存储池的技术全景蓝图 + 研究课题清单 + 部分硬件规格诉求 + 架构设计说明”的混合体，尚不是一份边界清晰、优先级收敛、可验收、可排期的软件需求规格说明。

我的评审意见是：**方向通过，SRS 需要重大收敛后再进入详细设计。**  
建议结论为：**有条件通过架构方向评审，不建议直接进入全量开发。**

---

# 1. 这份需求最有价值的部分

## 1.1 “raw hit ≠ usable hit”的判断是正确的

SRS 中最关键、最正确的判断是：**命中 KVCache 不等于一定应该加载 KVCache。**

第一性原理上，一个 prefix hit 的真实收益应满足：

```text
benefit_us =
  saved_prefill_compute_us
  - prefix_lookup_us
  - metadata_query_us
  - transfer_or_view_us
  - attach_us
  - rank_sync_us
  - interference_penalty_us
  - failure_risk_penalty_us
```

只有当 `benefit_us > 0`，并且满足 TTFT deadline 时，hit 才是 usable hit。否则，物理上命中了 KV，但系统层面应该把它当作 miss / recompute / partial recompute。

因此，下面这些需求是方向正确且应保留为核心 P0/P1：

| 需求 | 评审意见 |
|---|---|
| L1-PM-PrefixBudget-001 | 必须保留。prefix lookup 不能无限拖慢 TTFT。 |
| L1-RT-Admission-007 | 必须保留。load-vs-recompute 是整个系统的经济性核心。 |
| L2-CONN-CostAwareReturn-039 | 思路正确，但不应让“存储池内部”单独决定逻辑 Miss，应由 L3 返回 QueryPlan + cost，由 L1 Scheduler 结合实时 batch 状态最终裁决。 |
| L3-PM-HitQuality-023 / L3-SE-QueryPlanFastPath-072 | 非常关键。上层不应该拿到底层裸地址，而应该拿到“可执行计划”。 |

这部分是 v2.4 最大的技术亮点，也是区别于普通 KV offload 系统的核心。

---

## 1.2 “执行 HBM 与存储池解耦”的边界判断是正确的

L2-CONN-BufferContract-040 提出的 **Pull-to-Provided Device Pointer** 契约非常重要。它避免了统一存储池过度侵入推理框架的执行内存管理。

正确边界应该是：

```text
推理框架 / Scheduler / Block Manager：
  负责 batch admission、execution HBM 分配、attention runtime block table、算子可执行性。

统一 KV 存储池：
  负责 KV object 生命周期、placement、manifest、tiering、transfer、metadata、可见性、租约、故障恢复。

Connector：
  负责把二者通过 QueryPlan / AttachHandle / Descriptor 连接起来。
```

这条边界如果不划清，系统会变成“存储池既管数据，又管算力调度，又管 HBM 执行内存”，复杂度会失控。

因此 L2-CONN-BufferContract-040 应保留为 P0。

---

## 1.3 KV object 状态机、placement state、ready bitmap、lease/refcount 是必要的

v2.4 中大量状态一致性需求看起来繁琐，但方向是对的。因为一旦 KVCache 从单机 HBM 扩展到 HBM / Host DDR / remote DDR / SSD / object / UB / RDMA 多介质，单纯的 `prefix_hash -> address` 已经不够了。

至少要建模：

```text
KVObjectState:
  INIT / WRITING / COMMITTED / READY / ACTIVE_ATTACHED /
  PREFETCHING / LOADING / MIGRATING / COMPACTING /
  EVICTING / TOMBSTONE / FAILED / QUARANTINED

PlacementState:
  tier / node_id / device_id / extent / layout /
  visibility / version / replica_health / queue_state /
  migration_token / last_access

ConsumeEligibility:
  CONSUMABLE / WAIT_READY / NEED_LOAD / NEED_COPY_TO_HBM /
  MIGRATING_RETRY / STALE_RECOMPUTE / MISS
```

这部分应作为统一 KVCache 存储池的核心数据模型，而不是后期补丁。尤其是下面这些需求应保留为 P0：

| 需求 | 评审意见 |
|---|---|
| L3-MS-KVObjectStateMachine-058 | P0，必须有。 |
| L3-MS-ReplicaPlacementState-059 | P0，必须有。 |
| L3-MS-ConsumeEligibility-060 | P0，必须有。 |
| L3-CO-VisibilityReadyBitmap-064 | P0，必须有。 |
| L3-CO-AttachDetachLease-063 | P0，必须有。 |
| L3-CO-RefCountLifecycle-086 | P0，必须有。 |
| L3-MS-MigrationInterlock-062 | P0，必须有，但实现方式要降低承诺。 |

这一组需求是真正防止“命中但不能消费”“命中到旧副本”“命中到半迁移地址”“active decode 中 KV 被释放”的基础。

---

## 1.4 prefix metadata 快路径与 batch lookup 方向正确

分布式 KVCache 系统中，**metadata lookup 很容易比 data transfer 更先进入 TTFT 关键路径**。SRS 提出 hot local index、directory mirror、batch lookup、range lookup、metadata/data plane 分离，是正确的。

SGLang HiCache / Dynamo 文档已经明确体现了 tier-aware routing 和 shared-pool awareness 的思路：router 不仅看 worker 本地缓存，还会考虑共享外部池中的 KV block。([NVIDIA Docs](https://docs.nvidia.com/dynamo/integrations/kv-cache-integrations/hi-cache)) Mooncake Store 也明确支持多级 cache hierarchy、distributed KV cache、object placement、replication、eviction 等能力。([GitHub](https://github.com/kvcache-ai/Mooncake))

应保留为核心 P0/P1 的需求包括：

| 需求 | 评审意见 |
|---|---|
| L3-MS-HotLocalIndex-070 | P0，避免远端 RTT 进入 TTFT。 |
| L3-MS-RangeBatchLookup-071 | P0，避免逐 block 查询风暴。 |
| L3-MS-PrefixDirectorySchema-069 | P0，必须一次查询返回可消费判断所需字段。 |
| L3-MS-MetadataDataPlane-035 | P0，metadata/data plane 分离是必要架构原则。 |
| L2-PM-BatchLookup-021 | P1，可与 L3 range lookup 对齐。 |

---

# 2. 当前 SRS 的主要问题

## 2.1 需求层级混乱：L1/L2/L3/L4 的职责边界没有完全收敛

当前表中存在明显层级穿透和 ID 归属混乱。例如：

| 问题 | 示例 | 影响 |
|---|---|---|
| L1 表中出现 L3 需求 | TM3 中 L1 列包含 L3-MM-Lifecycle-009 | 会导致调度层与存储池层边界不清。 |
| L2 表中出现 L3 前缀索引 ID | TM2 的 L2 列有 L3-CONN-PFX-IDX-005 | ID 命名和层级职责不一致。 |
| 动态路径选择重复出现 | L2-CONN-TOPO-ROUTE-002、L3-TRANS-SEM-ENGINE-001、L4-FABRIC-ROUTER-001 | 三层都在“路由”，容易重复实现。 |
| 状态错误码、可消费判定重复 | L2-KV-StateErrorCode-037、L2-KV-ConsumeEligibility-035、L3-MS-ConsumeEligibility-060 | L2 应该是协议映射，L3 才是状态真相源。 |

建议重新定义层级职责：

```text
L1 推理调度层：
  只做 admission、routing、batch decision、load-vs-recompute、deadline 选择。

L2 KVConnector 层：
  只暴露稳定协议：QueryPlan、AttachHandle、Descriptor、ErrorCode、Intent。

L3 Transfer / KV Store Manager：
  维护 object state、placement、manifest、directory、policy engine、tiering、QoS。

L4 Transport / HAL：
  暴露能力、句柄、fence、registered pool、RDMA/GDS/UB/NVMe/TCP 插件。
```

**不要让 L1 解释物理状态，不要让 L4 决定业务语义，不要让 L2 拥有状态真相。**

---

## 2.2 很多需求不是“需求”，而是“指定实现手段”

SRS 中大量条目写成了具体技术方案，例如：

- “RadixTree 核心遍历函数从 Python 迁移至 C++/Cython，通过 SIMD AVX2……”
- “xxHash-128 Kernel，每 25 tokens checkpoint……”
- “100ms 周期后台探测……”
- “100 Virtual Nodes/物理节点……”
- “RDMA heartbeat 1s 周期，5s 主备切换……”
- “QP Context Cache 至少 128K……”

这些不应该直接作为 SRS 需求，而应该拆成：

```text
需求：
  prefix lookup P99 不超过 X μs；
  metadata local miss 判定 P99 不超过 Y μs；
  directory failover 不造成超过 Z 秒不可用；
  传输路径选择错误率低于 A；
  后台迁移对前台 TPOT P99 影响不超过 B%。

设计约束：
  可采用 C++/Cython/SIMD/RCU/RDMA/一致性哈希/虚拟节点等实现。

验收方法：
  microbenchmark + replay trace + fault injection + chaos test。
```

如果 SRS 直接绑定实现，后续架构设计和工程优化空间会被压死。

---

## 2.3 部分性能目标缺乏物理依据或过度承诺

下面这些目标需要降级、重写或改为探索项。

| 原需求/说法 | 评审意见 |
|---|---|
| “CPU→NPU KVCache 访问时延从 2-5ms 降至 <500ns” | 作为通用目标不成立。跨 CPU/NPU、跨 C2C/UB/PCIe 的 direct view 延迟取决于一致性协议、页表、TLB、链路、cacheability，不应承诺 <500ns。 |
| “1→N RDMA 广播发送端带宽 O(1)” | 商品 RDMA 可靠传输主要是点到点，硬件 multicast 通常有可靠性/交换机/协议限制。可以做 P2P relay、tree broadcast、in-network multicast 探索，但不应作为 P2/P1 硬需求。 |
| “路由正确率 100%” | 工程系统中不应写 100%。应定义错误分类、fallback 成功率、SLO 违约率。 |
| “100% 隐藏空间整理时延” | compaction 无法保证 100% 隐藏。应改为“后台 compaction 期间前台 TPOT P99 恶化不超过 X%”。 |
| “集群 KVCache 内存利用率 ≥90%” | 需要限定 workload、碎片率、冗余副本、tenant quota，否则不可验收。 |
| “prefix 查询从 5-20ms 降至 5μs 以内” | 对本地确定 miss / hot hit 可以；对远端 directory / RDMA / 共享池查询不应承诺 5μs。 |
| “远端 direct view 显著低于 RDMA 传输语义” | 不一定。小 metadata 可能成立，大 KV active decode 通常会被远端访问 stall 放大。 |

NVIDIA GPUDirect RDMA 的官方文档也明确指出，虽然它允许 GPU 与第三方 PCIe peer device 建立直接数据路径，但平台限制很重要，例如设备通常需要共享同一 PCIe root complex，并且不同平台可能存在性能或兼容性限制。([NVIDIA Docs](https://docs.nvidia.com/cuda/gpudirect-rdma/)) GPUDirect Storage 也只是减少 CPU copy、降低 CPU overhead，并不等价于“SSD 到 GPU/NPU 可无限低延迟”。([NVIDIA Docs](https://docs.nvidia.com/gpudirect-storage/))

---

## 2.4 “direct view” 被过度泛化了

SRS 中多处提到 memory view / direct view / UB/C2C view。这个方向对 **metadata、小对象、warm preview、短 prefix span** 是有价值的，但不能泛化为 decode-active KV 的默认路径。

第一性原理：

```text
decode 阶段每生成一个 token，都要读历史 KV。
历史越长，attention 对 KV 读取带宽越敏感。
如果 active decode KV 放在远端 DDR / SSD / remote memory view，
每一步 decode 都可能引入远端访问 stall。
```

因此正确原则应是：

```text
decode-active KV:
  默认 HBM 驻留。

warm prefix KV:
  可以在 DDR / remote DDR / SSD / object tier 中保留。

即将进入 decode attention active path 的 KV:
  默认 copy-to-HBM 或 staged-to-HBM。

direct view:
  只允许在 cost model 明确证明不会恶化 TPOT 时使用。
```

L1-OL-ActiveWarmClass-010 和 L1-OL-ViewVsCopy-011 的方向是对的，但 L4-UB-C2C-UNIFY-002、L4-UB-P2P-FABRIC-003 这类需求应降级为平台能力插件，而不是基础架构承诺。华为 CloudMatrix384 的公开论文确实描述了 UB 网络支持 NPU/CPU 间直接 all-to-all 数据交换和资源池化，但这类能力是特定平台架构特性，不应被写成所有平台通用能力。([arXiv](https://arxiv.org/html/2506.12708v3))

---

## 2.5 发布一致性模型存在冲突

SRS 同时出现了：

- L2-KV-PublishCommit-033：publish_prepare / publish_commit / publish_abort 三阶段发布接口；
- L3-MS-AtomicPublishVisibility-087：放弃控制面的 3PC 与分布式 WAL，采用单写者最终一致性，但要求前缀索引原子可见。

这两者不是不能共存，但当前表述容易冲突。

建议统一为：

```text
不做跨节点强一致 3PC。
采用单写者 object publication pipeline。

对象写入流程：
  allocate temp object
  write data extents
  checksum / visibility fence
  write manifest
  update ready bitmap
  publish version
  atomically unmask prefix directory entry

失败处理：
  abort temp object
  tombstone / quarantine
  background GC
```

也就是说，**可以保留 prepare/commit/abort 作为本地对象发布 API 语义，但不要承诺分布式 3PC。**

---

## 2.6 多播、DPU、硬件页迁移、原子 remap 应降级为研究项

以下需求不应作为当前 P0/P1 主线：

| 需求 | 建议 |
|---|---|
| L3-TRANS-MUL-ENGINE-003 | 改为 P3/R&D。先实现 tree relay / batched unicast，再评估硬件 multicast。 |
| L4-RDMA-MUL-FABRIC-002 | 改为 P3/R&D。不要承诺发送端 O(1)。 |
| L4-NET-OFFLOAD-DPU-001 | 改为 P3。DPU offload 需要硬件、DOCA/SDK、数据格式、调试体系共同成熟。 |
| L4-CO-PageMigration-063 | 改为 P3。通用硬件级页迁移 + 不改虚拟地址 + 原子 TLB 刷新不是软件层可普遍保证的能力。 |
| L4-CO-AtomicRemapPrimitive-065 | 改为 L3 逻辑 extent remap + RCU，而不是 L4 硬件原子 remap。 |
| L4-MC-HIER-STORE-002/003 on-the-fly 压缩/解压 | 改为 P3。KV 压缩涉及精度、layout、算子兼容和端到端收益验证。 |

可以保留这些作为 **平台增强能力**，但不要让它们阻塞 MVP。

---

# 3. 各 TM 模块评审

## TM1：推理调度与标准接口控制

**结论：高度必要，优先级应最高。**

TM1 是这份 SRS 中最应该先落地的部分。没有 TM1，后面的分布式 KV 池可能反而拖慢系统。

应保留的核心能力：

```text
KVAccessIntent
prefix lookup budget
load-vs-recompute admission
watermark admission
cache-aware routing
cost-aware QueryPlan
buffer ownership boundary
```

需要修改的点：

1. **Router 不应直接查询复杂 placement 细节**，应查询 L3 返回的 compact placement summary / QueryPlan。
2. **CostAwareReturn 不应在存储池内部单独判定最终 miss**，否则会脱离 batch 状态、算力队列、水位、deadline。正确方式是 L3 返回 cost，L1 最终裁决。
3. L1-VLLM-HIER-SCHED-002 不应要求 vLLM BlockSpaceManager 直接枚举全局 HBM/DDR/SSD 状态。vLLM 侧应只感知 abstract tier / attach plan / local execution HBM availability。

---

## TM2：分布式前缀索引与元数据平面

**结论：必要，但性能目标需要拆分。**

合理目标应拆成三类：

| 场景 | 合理目标 |
|---|---|
| 本地确定 miss | 5–20μs 级别可以追求。 |
| 本地 hot hit | 10–50μs 级别可以追求。 |
| 远端 directory 查询 | 不应承诺 5μs；应以 P50/P99、RTT、batch lookup、异步预检测来约束。 |

vLLM 的 prefix caching 是 hash-based block matching；SGLang 的 RadixAttention / HiCache 则更偏 radix/prefix tree 体系。v2.4 同时覆盖 vLLM 和 SGLang 是有意义的，但不应强制两者共享同一种索引结构。vLLM 侧更适合 block hash vector / page hash range；SGLang 侧更适合 radix span / page-first layout。([vLLM](https://docs.vllm.ai/en/stable/design/prefix_caching/))

建议把 L3-MS-TTFTIndexLayout-088 改成：

```text
提供统一 PrefixLookupRequest/PrefixLookupResult 协议；
底层允许 hash table、radix tree、range index、Bloom/Xor filter 多实现；
验收按 local miss、local hit、remote candidate、stale guard 四类路径分别测试。
```

---

## TM3：异构分层存储池与生命周期空间

**结论：架构正确，但实现复杂度最高，需要分阶段。**

必须保留：

```text
KV object state machine
placement state
extent manifest
page/extent allocator
tiering manager
state-aware eviction
migration interlock
attach lease / refcount
```

需要纠正：

1. **不要声称 HBM + DDR + SSD 可以“根本性解除 context 长度枷锁”。**  
   它能扩展 warm/cold KV 容量，但 active decode KV 仍受 HBM、attention 带宽、每步读放大的约束。

2. **Compaction 不应承诺完全无感。**  
   可以做到 batch boundary 安全切换、copy-on-migrate、RCU metadata pointer swap，但不能保证任何时刻完全无 stall。

3. **L3-MC-CompactionEngine-056 不应是第一阶段 P0。**  
   第一阶段更应先做 allocator + extent manifest + free list + coalescing + fragmentation metrics。真正在线 compaction 应该放到 P1/P2。

---

## TM4：硬件加速传输与数据流编排

**结论：bulk descriptor / async pipeline 可落地；DPU / multicast / deep hardware offload 应后置。**

应优先落地：

| 能力 | 优先级 |
|---|---|
| scatter-gather descriptor | P0/P1 |
| long-lived registered pool | P0 |
| async prefetch / transfer overlap | P1 |
| descriptor coalescing | P1 |
| backend capability discovery | P0 |
| RDMA/GDS/NIXL/Mooncake backend plugin | P1 |

NIXL 的公开说明已经把它定位为 AI inference 的点到点 data movement library，并提供 memory / storage 抽象、backend plugin、metadata exchange、descriptor、transfer 管理等能力；这与 v2.4 的 L3/L4 设计方向高度一致。([GitHub](https://github.com/ai-dynamo/nixl)) Mooncake Transfer Engine 也公开说明支持多 RDMA NIC 聚合、拓扑感知路径选择、失败切换，以及多种传输协议。([GitHub](https://github.com/kvcache-ai/Mooncake))

但下面这些目标需要收敛：

```text
1→N O(1) multicast
DPU INT8 量化卸载
硬件级原子 remap
SSD→NPU 完全绕 CPU 控制路径
跨节点 memory semantic direct load/store
```

这些都应作为平台专项或研究验证项，而不是通用 SRS 主路径。

---

## TM5：共享协同、安全隔离与 QoS 管控

**结论：必要，但 P0 范围过大。**

必须 P0：

```text
tenant / security domain / cache salt
semantic identity
lease handle
refcount lifecycle
attach/detach
view protection
fallback contract
state-aware traffic class at software level
```

可降级：

```text
硬件级 SR-IOV QP 强隔离
128K QP context cache
DPU/网卡固件级 QoS 兜底
secure extent zero-fill / crypto erase
```

原因是：多租户隔离是 P0，但“必须依赖特定硬件队列/QP/固件 SRAM 指标”不应作为软件 SRS 的第一阶段硬门槛。L4-HW-NICSpecConstraint-075 更像 **硬件选型规格**，应移到“平台部署约束/硬件采购规格”章节，而不是软件需求主表。

---

## TM6：全路径全栈可观测性与容错保障

**结论：应提升为贯穿所有阶段的 P0/P1。**

这部分写得比较正确。因为这类系统最常见的问题不是“没有命中”，而是：

```text
命中了，但变慢；
命中了，但不可消费；
命中了，但版本不兼容；
命中了，但正在迁移；
命中了，但 rank 不一致；
命中了，但 fallback 到 TCP；
命中了，但 copy-to-HBM 阻塞；
命中了，但后台 migration 抢了带宽。
```

因此必须把 raw hit rate 拆成：

```text
raw_hit
identity_compatible_hit
ready_hit
usable_hit
local_usable_hit
direct_view_hit
bulk_load_hit
stream_restore_hit
abandoned_hit
stale_hit_blocked
recompute_after_hit
```

L1-OB-SemanticMetrics-016、L2-OB-PathTrace-030、L3-OB-PerPathTelemetry-047、L3-OB-KVStateTrace-083 应保留，并且最好作为 MVP 观测基线。

---

# 4. 建议重排后的 P0 最小闭环

如果要把 v2.4 收敛成可开发版本，我建议先定义一个 **MVP P0 闭环**，不要一开始就做全量 HBM/DDR/SSD/Remote/UB/DPU/Multicast。

## P0-MVP 应聚焦这 10 件事

| 编号 | P0 能力 | 对应需求 |
|---|---|---|
| 1 | 统一 KVSemanticIdentity 与 tenant/cache salt 隔离 | L1-SC-TenantIsolation-017, L2-KV-SemanticIdentity-036 |
| 2 | Pull-to-provided-device-pointer 边界 | L2-CONN-BufferContract-040 |
| 3 | KV object state machine + placement state | L3-MS-KVObjectStateMachine-058, L3-MS-ReplicaPlacementState-059 |
| 4 | ready bitmap + visibility fence | L3-CO-VisibilityReadyBitmap-064, L4-CO-ExtentVisibilityFence-066 |
| 5 | consume eligibility + QueryPlan | L3-MS-ConsumeEligibility-060, L3-SE-QueryPlanFastPath-072 |
| 6 | prefix lookup budget + load-vs-recompute admission | L1-PM-PrefixBudget-001, L1-RT-Admission-007 |
| 7 | hot local index + batch/range lookup | L3-MS-HotLocalIndex-070, L3-MS-RangeBatchLookup-071 |
| 8 | attach/detach lease + refcount protection | L2-MM-ViewLease-028, L3-CO-AttachDetachLease-063, L3-CO-RefCountLifecycle-086 |
| 9 | extent manifest + SG descriptor generation | L3-MC-ExtentManifest-076, L3-SE-DescriptorFromManifest-079 |
| 10 | path trace + semantic metrics + fallback trace | L1-OB-SemanticMetrics-016, L2-OB-PathTrace-030, L3-FT-FallbackTrace-046 |

这个闭环先只覆盖：

```text
单机 / 小集群
HBM + local DDR
可选 remote DDR
不强依赖 SSD
不强依赖 DPU
不做硬件 multicast
不做跨节点 memory semantic direct view
```

这样才能先证明核心命题：

> KV 命中是否能稳定转化为 usable hit，并在 TTFT/TPOT/TCO 上产生正收益。

---

# 5. 建议降级或移出主线的需求

| 需求 | 当前优先级 | 建议优先级 | 原因 |
|---|---:|---:|---|
| L1-VLLM-PFX-IDX-005 GPU xxHash-128 Kernel | P2 | P3/实验 | vLLM 当前 hash-based prefix 机制成立，但 GPU hash 是否收益大于复杂度需要 profiling 证明。 |
| L2-MM-MultiConsumer-027 | P2 | P2 保留但限定 | 多消费者共享有价值，但不能默认广播；应按 request fanout / saved prefill / delay penalty 决策。 |
| L3-MC-CompactionEngine-056 | P0 | P1/P2 | 在线 compaction 极复杂，先做 allocator/fragmentation telemetry。 |
| L3-MC-IntelligentMigration-057 | P0 | P1 | 主动迁移有价值，但第一阶段先做 safe eviction / admission / watermark。 |
| L3-TRANS-MUL-ENGINE-003 | P1 | P3 | 1→N O(1) 多播不应成为主线承诺。 |
| L4-RDMA-MUL-FABRIC-002 | P2 | P3 | 同上。 |
| L4-UB-C2C-UNIFY-002 | P2 | 平台插件 P2/P3 | 特定平台能力，不应通用化。 |
| L4-UB-P2P-FABRIC-003 | P3 | 保持 P3 | 正确，继续作为远期探索。 |
| L4-CO-PageMigration-063 | P1 | P3 | 通用硬件页迁移/原子 TLB 刷新不可作为软件可交付承诺。 |
| L4-CO-AtomicRemapPrimitive-065 | P0 | P2/P3 | 应改为 L3 logical extent remap + RCU。 |
| L4-HW-NICSpecConstraint-075 | P1 | 移到硬件规格章节 | 这是采购/平台约束，不是软件功能需求。 |

---

# 6. 需要补充的关键缺失项

## 6.1 缺少明确 workload model

当前业务目标中写了“消除最高 40% 冗余计算”“大幅压降 TCO”，但没有定义 workload。

必须补充：

```text
prefix reuse ratio
shared prefix length distribution
request arrival burstiness
tenant count
model size
context length distribution
TP/PP/DP 配置
PD 分离比例
HBM/DDR/SSD/RDMA 拓扑
SLO: TTFT P50/P95/P99, TPOT P50/P95/P99
```

否则 40% 节省无法评审。对于完全随机 prompt，prefix cache 几乎没有收益；对于 agent tool schema、system prompt、RAG 模板、高复用会话，收益才可能显著。

---

## 6.2 缺少“正确性验收”要求

KVCache 复用不是普通缓存。它直接影响模型输出正确性。

应补充：

```text
semantic identity mismatch test
tokenizer hash mismatch test
template version mismatch test
adapter_id mismatch test
layout version mismatch test
rank partition mismatch test
position policy mismatch test
sliding-window / MLA / GQA 兼容性 test
stale hit / tombstone resurrection test
partial attach correctness test
```

尤其对于 GQA、MLA、sliding-window、hybrid/recurrent state，不同模型的 KV 结构和可复用条件不同。L1-MM-KVSizing-008 提到了这些因素，但 SRS 还需要把它们转化为 correctness contract。

---

## 6.3 缺少端到端验收指标

建议按下面方式定义验收：

| 指标 | 建议定义 |
|---|---|
| usable hit rate | usable_hit / total_requests，而不是 raw_hit。 |
| abandoned hit rate | raw_hit 后因 deadline / transfer / stale / migration 放弃的比例。 |
| TTFT benefit | hit path 与 recompute path 的 P50/P95/P99 差值。 |
| TPOT interference | 前台 decode TPOT 在 migration/prefetch/writeback 背景流量下的恶化比例。 |
| metadata latency | local miss、local hot hit、remote lookup、batch lookup 分别统计。 |
| attach safety | active lease 下 evict/migrate/compact fault injection 零悬空地址。 |
| stale protection | tokenizer/model/template/layout 更新后旧 KV 不得命中。 |
| fallback correctness | RDMA fail、SSD slow、TCP fallback forbidden、deadline expire 下动作正确。 |

---

# 7. 最关键的修改建议

## 建议 1：把“统一异构 KVCache 存储池”收敛成三条主路径

```text
主路径 A：Prefix hit 快速判定路径
  request -> semantic identity -> local hot index -> QueryPlan -> admission

主路径 B：KV load / attach 消费路径
  QueryPlan -> allocate execution HBM -> transfer/copy/view -> ready bitmap -> AttachHandle -> attention runtime

主路径 C：KV publish / lifecycle 路径
  prefill KV generated -> publish pipeline -> manifest -> placement -> directory visible -> eviction/tiering/gc
```

所有需求都应该能挂到这三条路径之一。挂不上的，暂时放到增强项。

---

## 建议 2：把 P0 定义为“正确性 + 可消费 + 正收益”，而不是“极致硬件性能”

P0 不应追求一开始就做到 DPU、多播、UB direct view、SSD zero-copy。P0 应先保证：

```text
不误命中；
不命中旧版本；
不返回悬空地址；
active KV 不被释放；
命中后能判断是否值得加载；
加载失败能快速 recompute；
观测上能解释为什么命中变慢。
```

这才是统一 KVCache 存储池的地基。

---

## 建议 3：把硬件能力改成 capability matrix，而不是固定实现承诺

L4 不应该写：

```text
必须支持 NPUDirect RDMA
必须支持 UB GAP
必须支持硬件页迁移
必须支持 DPU offload
```

而应该写：

```text
TransportCapability:
  supports_memory_view
  supports_bulk_transfer
  supports_rdma_read
  supports_rdma_write
  supports_gds
  supports_registered_hbm
  supports_registered_host_ddr
  supports_visibility_fence
  supports_qos_queue
  supports_remote_access_counter
  max_sg_entries
  max_segment_size
  registration_granularity
  p50_latency_us
  p99_latency_us
  measured_bw_gbps
```

NIXL 和 Mooncake 的公开架构都已经体现了通过统一 abstraction / backend plugin / 多传输协议来屏蔽底层差异的方向，这比在 SRS 中绑定单一硬件路径更稳妥。([NVIDIA Developer](https://developer.nvidia.com/blog/enhancing-distributed-inference-performance-with-the-nvidia-inference-transfer-library/))

---

# 8. 最终评审意见

## 可以通过的部分

这份 v2.4 在以下方面已经具备较高架构质量：

1. **抓住了 KVCache 系统的核心矛盾**：命中不等于收益，必须做 load-vs-recompute。
2. **认识到了前缀索引不是普通 metadata 查询**：它是 TTFT 快路径的一部分。
3. **把 KVCache 从“框架内部临时 buffer”提升成了“可发布、可寻址、可迁移、可租约保护的对象”**。
4. **引入了 semantic identity / tenant / layout / version / ready bitmap / lease/refcount 等必要正确性维度**。
5. **把 observability 纳入核心设计**，这对于排查“命中了但变慢”非常关键。
6. **方向与 vLLM / SGLang / Mooncake / NIXL / LMCache 当前生态演进一致**，不是脱离现实的架构空想。([vLLM](https://docs.vllm.ai/en/stable/features/nixl_connector_usage/))

## 不能直接通过的部分

但它目前还存在这些阻塞问题：

1. **范围过大**：P0/P1 中混入太多远期硬件能力和研究课题。
2. **层级边界不清**：L1/L2/L3/L4 多处职责重复或穿透。
3. **性能指标过度承诺**：若无平台实测，5μs、<500ns、100% 正确、O(1) 多播等目标不可验收。
4. **实现手段写进需求**：SIMD、虚拟节点数、heartbeat 周期、QP cache 等不应作为 SRS 主体。
5. **一致性模型需要统一**：三阶段发布、单写者最终一致、commit log、tombstone、visibility pipeline 需要重新整理。
6. **硬件平台泛化不足**：UB/C2C/NPUDirect/DPU/GDS/RDMA 能力应作为 capability plugin，而不是通用基线。

## 建议评审结论

**建议结论：有条件通过架构方向评审；SRS 需重构后进入下一阶段。**

重构目标不是删减技术深度，而是把它收敛成：

```text
第一阶段：可正确消费的统一 KV object + QueryPlan + AttachHandle + HBM/DDR 路径
第二阶段：分布式 directory + remote DDR/SSD + cost-aware routing + tiering
第三阶段：UB/C2C direct view、DPU offload、hardware multicast、compression、硬件 QoS
```

一句话总结：

> v2.4 的技术洞察是对的，但工程形态太“满”。真正可行的路线不是一次性建设全异构、全硬件、全路径最优的 KVCache 操作系统，而是先把“KV 是否可消费、是否值得消费、如何安全消费”这个最小闭环做扎实，再逐步引入远端存储、硬件直通、DPU、多播和跨平台高级能力。