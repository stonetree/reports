你的担心是成立的：如果前两阶段完全不碰硬件能力抽象，只做纯软件 KV object / directory / QueryPlan，后期很容易出现“软件语义已经定型，但硬件能力接不进去”的返工。我会把评审意见调整为：硬件联合不能后置，但硬件功能本身可以分层进入。

你的判断是对的，而且这是 v2.4 评审里需要修正的关键点。

我前一版建议“前期先做 KV object / QueryPlan / AttachHandle / HBM+DDR 最小闭环”，容易被理解成：**前期先做纯软件语义，硬件能力后面再接入**。如果按这个方式实施，确实会把最大风险后移。对于这类“软硬协同 KVCache 异构统一内存池”，硬件能力不是后期优化项，而是架构语义的一部分。

更准确的修正意见应该是：

> **硬件能力验证与软件语义定义必须前置；但高风险硬件功能的业务化收益兑现可以分阶段。**  
> 换句话说，不能晚做硬件，但也不能一开始把所有硬件路径都做成业务主路径。

---

# 1. 为什么硬件联合不能后置

这类系统的核心不是普通 KVCache offload，而是：

```text id="bm76va"
KVCache 业务语义
  ×
硬件 memory / fabric / DMA / RDMA / SSD / C2C / QoS 能力
  →
TTFT / TPOT / HBM 利用率 / TCO 改善
```

如果前期只做软件对象模型、目录、状态机，而不同时验证硬件能力，会出现几个严重后果。

## 1.1 数据结构可能从一开始就设计错

KV block/page 的 layout、extent manifest、scatter-gather descriptor、alignment、page size、max segment size、registration granularity、direct-IO granularity 都受硬件约束影响。

例如：

```text id="tuwgvf"
如果底层 RDMA / DMA backend 的 max_sg_entries 很小，
L3 manifest 允许过度碎片化就会导致 descriptor 爆炸。

如果 GDS / SSD direct I/O 要求 4KB / 512B / page-aligned，
上层 KV segment schema 就不能随意切分。

如果 HBM registered pool 临时注册成本很高，
L4-OL-RegisteredPool-053 就必须成为前置 P0，而不是后期优化。
```

NIXL 的公开定位就是给 AI inference 提供 point-to-point data movement，并通过 memory/storage abstraction、backend plugin、descriptor/transfer management 来屏蔽底层差异；这说明数据移动抽象本身就是推理架构的一等对象，而不是最后再接的驱动层。([NVIDIA Developer](https://developer.nvidia.com/blog/enhancing-distributed-inference-performance-with-the-nvidia-inference-transfer-library/?utm_source=chatgpt.com))

## 1.2 Cost model 没有硬件实测就是空的

load-vs-recompute admission 的核心公式是：

```text id="2r4lrz"
是否加载 KV =
  saved_prefill_time
  >
  lookup_time + transfer_time + attach_time + sync_time + interference_penalty
```

其中 `transfer_time / attach_time / interference_penalty` 完全依赖硬件路径：

```text id="r5dzmp"
HBM -> HBM copy
Host DDR -> HBM DMA
Remote DDR -> local HBM RDMA
SSD -> HBM GDS / host staging
UB/C2C direct view
TCP fallback
```

如果前期没有硬件 microbenchmark，L2-CONN-CostAwareReturn-039、L3-SE-ViewCopyCostModel-034、L3-SE-PlacementResolver-061 都只能写成逻辑判断，无法形成真实决策。

Mooncake 的技术路线本身就是 KVCache-centric disaggregated architecture，利用 GPU 集群中未充分使用的 CPU、DRAM、SSD 资源构建 KVCache disaggregated cache，并通过 scheduler 在吞吐和 SLO 之间做平衡；这说明它的收益不是纯软件目录带来的，而是调度策略与底层资源/链路共同作用的结果。([arXiv](https://arxiv.org/abs/2407.00079?utm_source=chatgpt.com))

## 1.3 后期接硬件会倒逼 L2/L3 返工

如果 L4 能力后置，很可能后期才发现：

```text id="145kqb"
某些路径不支持 read-after-write 可见性 fence；
某些 memory view 不能被安全 revoke；
某些 RDMA handle 在 pool re-registration 后无法稳定失效；
某些设备没有 remote access counter；
某些 QoS queue 不能做到硬隔离；
某些路径不支持所需的 max descriptor count；
某些平台只能 bulk transfer，不能 memory view。
```

这会反过来修改：

- KVAttachHandle 字段；
- QueryPlan 字段；
- fallback contract；
- lease 语义；
- ready bitmap 粒度；
- extent manifest；
- traffic class；
- observable metrics。

所以，硬件 capability matrix 必须从第一阶段就存在。

---

# 2. 修正后的总体判断

我建议把前一版评审结论从：

> “第一阶段先做软件最小闭环，硬件增强后置”

修正为：

> **第一阶段必须做软硬联合最小闭环，但只选择少数确定性高的硬件路径作为业务主路径；高风险硬件特性作为旁路验证，不进入关键业务 SLO。**

也就是：

```text id="rksk9g"
硬件抽象前置；
硬件实测前置；
硬件约束前置；
硬件 telemetry 前置；

但：
  DPU offload、跨节点 memory semantic direct view、硬件 multicast、
  原子页迁移、on-the-fly 压缩/解压
  不直接作为第一阶段业务主路径。
```

这比“纯软件先行”更符合你的目标。

---

# 3. 建议采用“双闭环”实施路线

不要把阶段划成“先软件、后硬件”，而要划成两个同步闭环：

```text id="uxq0jh"
闭环 A：语义正确性闭环
  KV object -> state machine -> placement -> QueryPlan -> AttachHandle -> lease/refcount -> fallback

闭环 B：硬件数据路径闭环
  capability discovery -> registered pool -> extent handle -> SG descriptor -> fence -> QoS -> telemetry -> microbenchmark
```

第一阶段就要让 A 和 B 打通。区别只是：第一阶段不要覆盖所有硬件路径。

---

# 4. 修正后的阶段规划

## Phase 0：软硬协同基线验证阶段

这个阶段应该放在真正业务开发之前，时间不宜太长，但必须存在。目标不是做功能，而是确定系统边界。

### 必做内容

| 类别 | 必做项 | 对应需求 |
|---|---|---|
| 硬件能力探测 | semantic capability table | L4-HW-SemCapTable-050 |
| 路径能力建模 | Fabric Router 最小版 | L4-FABRIC-ROUTER-001 |
| 注册内存池 | HBM/DDR registered pool | L4-OL-RegisteredPool-053 |
| 远端句柄 | RemoteExtentHandle | L4-RDMA-RemoteExtentHandle-071 |
| 可见性边界 | extent visibility fence | L4-CO-ExtentVisibilityFence-066 |
| SG 约束 | max_sg_entries / alignment / segment size | L4-HW-StorageLayoutCapability-067 |
| 路径 telemetry | RDMA BW、CQ latency、DMA queue、copy stream | L3-OB-PerPathTelemetry-047 |
| microbenchmark | HBM↔DDR、DDR↔HBM、RDMA、SSD、fallback path | 新增 |

### 输出物

```text id="asow8w"
1. TransportCapability Matrix
2. 每条路径的 p50/p99 latency、BW、registration cost、setup overhead
3. max descriptor count / min granularity / alignment 约束
4. fence / visibility / consistency 语义说明
5. 哪些路径可作为 Phase 1 主路径，哪些只能作为实验路径
```

这一步的价值是：**防止 L1/L2/L3 在没有硬件真相的情况下闭门造车。**

---

## Phase 1：硬件感知的最小业务闭环

这个阶段不能只是 HBM+DDR 纯软件 offload，而应该至少打通一个真实硬件数据路径。

建议主路径选择：

```text id="v06ysu"
Path 1: local HBM -> local DDR -> local HBM
Path 2: local HBM -> remote DDR via RDMA -> local HBM
Path 3: fallback recompute
```

暂时不把 SSD、DPU、跨节点 direct view、硬件 multicast 放入主路径。

### Phase 1 必须落地的能力

| 能力 | 为什么必须前置 |
|---|---|
| Pull-to-provided-device-pointer | 明确执行 HBM 与存储池边界。 |
| registered pool | 否则每次注册成本会污染传输收益判断。 |
| SG descriptor | KV block 天然碎片化，不做 descriptor 就无法验证真实吞吐。 |
| visibility fence | 没有 fence，ready bitmap 没有正确性基础。 |
| QueryPlan | 让 L1 看到硬件路径代价，而不是裸地址。 |
| path telemetry | 没有 telemetry，无法解释“命中了但变慢”。 |
| load-vs-recompute admission | 这是业务收益闭环。 |
| lease/refcount | 防止 active KV 被迁移/释放。 |

这时 Phase 1 的业务目标不是“全量统一异构存储池”，而是验证：

```text id="hz7ykq"
在真实硬件路径下，
一个 prefix hit 能否稳定转化为 usable hit，
并且 TTFT/TPOT 不被 transfer / attach / sync 拖垮。
```

---

## Phase 2：扩展为多 tier KVCache 存储池

Phase 2 才进入更完整的异构分层：

```text id="zq7vun"
HBM
local DDR
remote DDR
local SSD
remote SSD / object
```

此时引入：

- tiering manager；
- hot/cold/warm 状态；
- SSD segment；
- PersistentExtentHandle；
- prefetch；
- state-aware eviction；
- directory mirror；
- multi-replica resolver；
- watermark admission；
- semantic QoS。

vLLM 的 MooncakeStoreConnector 已经明确把 MooncakeDistributedStore 作为 shared KV cache pool，用于外部分布式 KV store/offloading；SGLang HiCache 也已经体现了 hierarchical KV caching 和 tier-aware router 与外部共享池协同的方向。([vLLM](https://docs.vllm.ai/en/stable/features/mooncake_store_connector_usage/?utm_source=chatgpt.com)) 所以 Phase 2 的方向是有现实生态基础的。

---

## Phase 3：平台增强硬件路径

这个阶段再把高风险能力逐步业务化：

```text id="8hlbmi"
UB/C2C direct view
跨节点 memory semantic access
DPU offload
硬件 multicast / relay tree
GDS / NPUDirect Storage
on-the-fly compression
硬件 QoS 强隔离
原子 remap / page migration
```

注意：这些能力不是 Phase 3 才“开始研究”，而是 **Phase 0 就开始能力验证**，Phase 1/2 做旁路实验，Phase 3 才进入主路径。

---

# 5. 需要调整的 P0/P1 优先级

基于你的提醒，我建议将部分 L4/L3 硬件抽象需求提前为 P0，而不是后置。

## 5.1 应前置为 P0 的硬件相关需求

| 需求 | 建议 | 理由 |
|---|---|---|
| L4-HW-SemCapTable-050 | P0 保留 | 这是软硬协同的入口。 |
| L4-HW-StorageLayoutCapability-067 | 提升到 P0 | 直接约束 manifest / descriptor / layout。 |
| L4-OL-RegisteredPool-053 | P0 保留 | 高频 KV 传输必须池化注册内存。 |
| L4-RDMA-RemoteExtentHandle-071 | P0 保留 | remote DDR / RDMA 主路径需要。 |
| L4-CO-ExtentVisibilityFence-066 | P0 保留 | ready bitmap 和 attach 正确性的底座。 |
| L4-FT-RASErrorMap-061 | P0 保留 | 硬件错误必须能映射到 recompute / quarantine。 |
| L4-QO-TrafficClass-056 | P0/P1 | 至少软件队列和部分硬件队列隔离要前置。 |
| L4-OB-RemoteAccessCounter-057 | P1，可前置验证 | direct view 是否可用依赖这个。 |
| L3-TRANS-CAP-API-002 | P0 保留 | L1/L2/L3 不能硬编码平台能力。 |
| L3-TRANS-SEM-ENGINE-001 | P0 保留 | 但第一阶段只支持少数路径。 |
| L3-SE-DescriptorFromManifest-079 | P0 保留 | manifest 必须能生成真实可执行 descriptor。 |
| L3-OB-PerPathTelemetry-047 | P0 保留 | 没有观测就无法做硬件联合调优。 |

这组需求应该作为 **Phase 0/Phase 1 的硬件地基**。

---

## 5.2 仍建议后置业务化的硬件需求

这些不是不重要，而是风险和平台依赖太强，不能作为第一阶段业务主路径。

| 需求 | 建议 | 说明 |
|---|---|---|
| L4-UB-C2C-UNIFY-002 | Phase 0 验证，Phase 2/3 业务化 | direct view 需要实测 TPOT stall。 |
| L4-UB-P2P-FABRIC-003 | Phase 0 研究，Phase 3 业务化 | 跨节点 memory semantic access 平台依赖极强。 |
| L4-NET-OFFLOAD-DPU-001 | Phase 0 预研，Phase 3 业务化 | DPU 控制面/数据面 offload 调试和稳定性复杂。 |
| L3-TRANS-MUL-ENGINE-003 | Phase 0 验证，Phase 3 业务化 | 先做 tree relay，不承诺 O(1)。 |
| L4-RDMA-MUL-FABRIC-002 | Phase 3 | 硬件 multicast 的可用性、可靠性、交换机支持不确定。 |
| L4-CO-AtomicRemapPrimitive-065 | Phase 2/3 | 第一阶段用 logical extent remap + RCU 更现实。 |
| L4-CO-PageMigration-063 | Phase 3 | 硬件页迁移不是通用软件可控能力。 |
| L4-MC-HIER-STORE-002/003 | Phase 3 | 压缩/解压涉及精度、layout、算子兼容。 |

---

# 6. 对原 SRS 的关键修改建议

## 6.1 把“硬件联合”从 L4 孤岛提升为贯穿式设计目标

当前 v2.4 的问题不是没有硬件需求，而是硬件需求主要堆在 L4，容易被理解成底层实现细节。应该在顶层 TM 中明确：

```text id="jqnzlg"
本系统不是普通 KVCache 存储池，
而是软硬协同的 KVCache 异构统一内存池。

所有 QueryPlan、AttachHandle、PlacementResolver、CostModel、QoS、Telemetry
都必须由真实硬件 capability 和动态 telemetry 驱动。
```

建议新增一个顶层横向原则：

| 原则 | 内容 |
|---|---|
| HW-Aware by Design | 任何 KV 访问计划必须绑定硬件 capability、路径实测代价、可见性语义和故障语义。 |
| Capability First | 不允许 L1/L2/L3 假设某路径支持 memory view、atomic、fence、QoS、RDMA、GDS。 |
| Telemetry Closed Loop | 所有路径选择必须被 per-path telemetry 校正。 |
| Safe Fallback | 任何硬件路径不可用时，必须能回退 copy / load / recompute。 |

---

## 6.2 把 Phase 1 MVP 改成“硬件感知 MVP”

前一版的 MVP 需要修改为：

```text id="nn39vy"
Phase 1 不做：
  全量 SSD/object tier
  DPU offload
  硬件 multicast
  跨节点 memory semantic direct view
  on-the-fly compression
  硬件页迁移

Phase 1 必须做：
  capability matrix
  registered pool
  RDMA / DMA 至少一条真实数据路径
  SG descriptor
  visibility fence
  RemoteExtentHandle / LocalExtentHandle
  per-path telemetry
  cost-aware QueryPlan
  load-vs-recompute admission
```

这就避免了“后期硬件风险集中爆发”。

---

## 6.3 Cost model 必须从第一阶段使用真实测量值

不要把 cost model 写成静态配置。建议改成：

```text id="f1h9l6"
CostModel 输入：
  measured_p50_latency_us
  measured_p99_latency_us
  measured_bw_gbps
  descriptor_setup_us
  registration_cost_us
  queue_depth
  copy_engine_util
  RNIC CQ latency
  HBM pressure
  DDR NUMA locality
  active decode interference
  fallback probability
```

输出：

```text id="5bhnyf"
QueryPlan:
  consume_action
  source_placement
  expected_load_us
  expected_attach_us
  expected_interference_us
  confidence
  fallback_action
```

这样 L2-CONN-CostAwareReturn-039 就不是拍脑袋返回 MISS，而是基于硬件测量和调度状态做收益判断。

---

# 7. 建议新增一个“硬件联合验证矩阵”

为了防止风险后移，SRS 里应该新增一张表：每个业务目标必须绑定至少一个硬件验证项。

| 业务目标 | 必须验证的硬件能力 | 不验证的风险 |
|---|---|---|
| 降低 TTFT | RDMA/DDR/HBM copy latency、descriptor setup、fence cost | 命中后加载比重算慢。 |
| 提高 HBM 利用率 | watermark、eviction bandwidth、copy engine interference | 后台迁移挤占前台 decode。 |
| 支持长上下文 | HBM↔DDR/SSD tiering、prefetch latency、active/warm 分层 | 只扩容量，不改善可用延迟。 |
| 降低 TPOT 干扰 | remote access counter、stall cycles、copy stream queue | direct view 拖慢 decode。 |
| 多租户隔离 | SR-IOV/QoS queue/namespace/isolation domain | 背景流量或越权访问不可控。 |
| 安全 attach | visibility fence、lease revocation、handle invalidation | attach 到半写入或过期副本。 |
| 快速 fallback | RAS error map、transport timeout、fallback latency | 硬件异常导致请求长时间挂起。 |
| 高效批量传输 | max_sg_entries、alignment、registration granularity | SG descriptor 过碎导致带宽上不去。 |

这张表比单纯列 L4 需求更有价值，因为它把硬件能力和业务指标绑定起来。

---

# 8. 对“风险后移”的具体控制建议

## 8.1 每个阶段必须有硬件 Exit Criteria

例如 Phase 1 不能只说“QueryPlan 开发完成”，而要有硬件验收门槛：

```text id="7amnm1"
Phase 1 Exit Criteria:
1. HBM->DDR->HBM 路径完成端到端 KV load/attach。
2. remote DDR RDMA->HBM 路径完成端到端 KV load/attach。
3. registered pool 下传输性能优于临时注册路径。
4. SG descriptor 合并后 CPU submit overhead 明显下降。
5. visibility fence 完成前，ready bitmap 不可见。
6. active lease 下 evict/migrate 不会释放物理 extent。
7. cost model 能在“加载比重算慢”场景返回 recompute。
8. path trace 能解释每次 hit 被放弃的原因。
```

这样就不会出现“软件都写完了，硬件一接全返工”。

---

## 8.2 硬件路径要分成“主路径”和“旁路验证路径”

建议每阶段定义：

```text id="6ox85x"
Production Path:
  进入业务闭环，影响 TTFT/TPOT SLO。

Experimental Path:
  只跑 replay / shadow / benchmark，不影响线上决策。

Forbidden Path:
  当前平台能力不满足，不允许 QueryPlan 选择。
```

例如：

| 路径 | Phase 1 状态 | Phase 2 状态 | Phase 3 状态 |
|---|---|---|---|
| HBM ↔ local DDR bulk copy | Production | Production | Production |
| remote DDR RDMA load | Production/灰度 | Production | Production |
| SSD staged restore | Experimental | Production | Production |
| UB/C2C direct view metadata | Experimental | Production | Production |
| UB/C2C direct view active KV | Forbidden/Experimental | Experimental | 条件 Production |
| DPU offload | Experimental | Experimental | 条件 Production |
| hardware multicast | Experimental | Experimental | 条件 Production |

---

# 9. 修正后的最终评审意见

你的补充后，我会把评审结论改成下面这样：

> v2.4 的核心目标不是普通软件 KVCache 管理，而是软硬协同的 KVCache 异构统一内存池。因此，不能采用“先软件抽象、后硬件接入”的阶段策略。硬件 capability、registered pool、extent handle、visibility fence、SG descriptor、per-path telemetry、QoS 隔离、RAS error map 必须在第一阶段前置验证，并参与 QueryPlan、AttachHandle、CostModel 的语义定义。  
>   
> 但同时，硬件能力要分层进入：Phase 1 只选择 HBM/DDR/RDMA 等确定性较高路径进入业务主链路；UB/C2C direct view、DPU offload、硬件 multicast、on-the-fly compression、硬件页迁移等高风险路径应从 Phase 0 开始旁路验证，但不应阻塞 Phase 1 的业务闭环。  
>   
> 因此，正确路线不是“软件先行”或“硬件后置”，而是“硬件能力抽象与验证左移，硬件高级特性业务化分阶段”。

一句话概括：

> **硬件不能后置，后置的是高风险硬件特性的主路径承诺；前置的是硬件能力表、真实路径测量、可见性语义、句柄模型、descriptor 约束和 QoS/RAS telemetry。**