# L1：推理框架层 SRS

| 需求唯一标识 | 归属层级与组件 | 核心攻克的技术痛点 | 重点针对的场景 | 研发实施技术要点（2026 技术栈） | 预期可量化核心指标收益和验收方法 |
|---|---|---|---|---|---|
| **L1-PM-PrefixBudget-001** | **L1 推理框架层 / Scheduler + Prefix Admission** | prefix 判断处于 TTFT 关键路径，远端 lookup 过慢会让命中变负收益 | 前缀匹配、TTFT-critical prefix lookup | 在 vLLM/SGLang Scheduler 中引入 `prefix_decision_deadline_ns`、`max_remote_lookup_us`、`fallback_policy`；超预算立即 partial hit 或 recompute | `prefix_decision_p99` 降低 ≥30%；`abandoned_hit_rate` 降低 ≥50%；验收：统计 raw hit、usable hit、abandoned hit |
| **L1-PM-PromptCanon-002** | **L1 推理框架层 / Gateway + Prompt Builder** | prompt 模板、tool schema、RAG chunk 顺序微小变化导致 hash miss | 多轮 chat、agent、RAG、tool calling | Gateway/Prompt Builder 执行 canonical JSON、chat template version 固化、RAG chunk 稳定排序、system prompt ID 化 | raw prefix hit rate 提升 ≥20%；验收：同一业务 trace canonical 前后 hash miss 对比 |
| **L1-PM-TokenHash-003** | **L1 推理框架层 / Tokenizer + Prefix Hash Engine** | 多轮请求重复 tokenization 与 block hash 计算，CPU 开销进入 TTFT | 多轮 agent session、长上下文续写 | 建立 session rolling token/hash state；仅对新增 suffix 增量 tokenization/hash；兼容 vLLM hash block 与 SGLang radix span | `prefix_index_compute_p99` 降低 ≥40%；验收：采集 tokenization/hash CPU 时间 |
| **L1-PM-HybridIndex-004** | **L1 推理框架层 / Prefix Cache Index** | 单纯 hash block 粒度粗，radix tree 精确但 metadata 复杂 | prefix 最长匹配、partial prefix hit | Hybrid Radix-Hash：radix 做 longest prefix span，hash 做 block/page exact lookup；兼容 vLLM APC 与 SGLang HiRadixTree | usable hit tokens 提升 ≥15%；验收：matched tokens / raw matched tokens 比值 |
| **L1-PM-SecureHash-005** | **L1 推理框架层 / Prefix Hash + Security Domain** | fast hash 性能好但有碰撞/跨租户风险，secure hash 成本高 | 多租户 prefix cache | 两级 hash：fast fingerprint 定位候选，secure hash/token span 校验；多租户默认 secure path | hash latency 降低 ≥30%，collision error 为 0；验收：压测 + 人工构造碰撞样本 |
| **L1-RT-CacheAwareRouting-006** | **L1 推理框架层 / Router + Placement Client** | 请求先路由到无 KV 节点，再远端拉取，local hit rate 低 | 集群级 agent/RAG serving | Router 查询 placement summary，按 HBM/local DDR/同机架 remote DDR hit score 路由；对接 Mooncake Store/LMCache directory | local usable hit rate 提升 ≥25%；跨节点 KV bytes/request 降低 ≥30%；验收：round-robin vs cache-aware A/B |
| **L1-RT-Admission-007** | **L1 推理框架层 / Scheduler + Load/Recompute Admission** | prefix 命中后仍可能加载成本大于 recompute | TTFT-critical load admission | Scheduler 计算 `lookup + transfer + attach + rank_sync < recompute_saved_time`；低价值 hit 放弃加载 | p99 TTFT 降低 ≥20%；abandoned remote load 降低 ≥50%；验收：记录 load-vs-recompute 决策与真实耗时 |
| **L1-MM-KVSizing-008** | **L1 推理框架层 / Model Metadata + KV Block Manager** | MHA/GQA/MLA/sliding-window KV 大小不同，粗估导致 HBM/DDR 预留错误 | HBM allocator、offload planning | 提供 `get_kv_block_bytes(model,dtype,tp,layout)`；纳入 GQA/MLA、TP rank、dtype、block/page size | OOM/preemption 次数降低 ≥50%；验收：实际 allocated bytes 与估算误差 <5% |
| **L1-MM-Lifecycle-009** | **L1 推理框架层 / KV Block Manager + Request Lifecycle** | async send/free、remote load 失败、request finished 交错导致 HBM 泄漏或 stale KV | KV block 生命周期管理 | 统一状态机：`ALLOCATED/LOADING/READY/ACTIVE/OFFLOADING/EVICTABLE/RELEASED/FAILED`；transfer completion 后再释放 | HBM leaked block = 0；stale attach = 0；验收：故障注入 + 长稳压测 |
| **L1-OL-ActiveWarmClass-010** | **L1 推理框架层 / KV Residency Classifier** | CXL/C2C/DDR 容量大但带宽低于 HBM，不能直接承载 decode-active KV | decode-active KV、warm prefix KV | 区分 `DECODE_ACTIVE` 与 `WARM_PREFIX`；active KV 默认进入 HBM，warm KV 可留 DDR/CXL/C2C | TPOT 不回退；HBM effective capacity 提升 ≥20%；验收：direct-view vs copy-to-HBM A/B |
| **L1-OL-ViewVsCopy-011** | **L1 推理框架层 / Access Mode Planner** | CXL/C2C direct access 可能省 copy 但拖慢 attention | CXL/C2C warm KV、短 prefix | direct-view eligibility check：短 span/metadata 可 direct view，长 KV 自动 copy to HBM | TTFT 不劣化，TPOT p99 回退 <3%；验收：按 span 长度 sweep |
| **L1-OL-PartialBoundary-012** | **L1 推理框架层 / Prefix Boundary Planner** | partial block/page 命中无法充分利用，全部 recompute 浪费 | prefix 部分命中 | 根据 block/page 边界选择 “load prefix + recompute suffix” 最优切点 | prefill FLOPs/request 降低 ≥15%；验收：matched tokens 与 recompute tokens 统计 |
| **L1-PD-RankConsensus-013** | **L1 推理框架层 / Distributed Rank Coordinator** | TP/PP 多 rank prefix 命中长度不一致会导致错误 attach | 多 GPU/NPU TP、PD 分离 | 多 rank 对 usable prefix length、KV version、ready bitmap 做 consensus，取 min-safe prefix | correctness fault = 0；rank wait p99 <100us；验收：多 rank 故障注入 |
| **L1-SE-IntentAPI-014** | **L1 推理框架层 / KVAccessIntent API** | 上层直接选择 CXL/RDMA/TCP 会污染 Scheduler，也容易误选路径 | 统一 KVCache 存储池北向接口 | 默认使用 `KVAccessIntent`：purpose、deadline、size、reuse value、fallback、visibility、isolation domain | 上层后端无关；新增后端无需改 Scheduler；验收：同一 intent 在 RDMA/CXL/TCP 环境下运行 |
| **L1-SE-ExpertOverride-015** | **L1 推理框架层 / Expert Policy Override** | 纯统一抽象隐藏性能语义，专家无法做极致调优 | 性能调优、A/B 实验、故障绕行 | 支持 `preferred_semantics`、`forbidden_semantics`、`force_copy_to_hbm`、`allow_direct_view`、`forbid_tcp` | path selection 可控；验收：固定 path 实验结果与配置一致 |
| **L1-OB-SemanticMetrics-016** | **L1 推理框架层 / KV Observability SDK** | 只统计 hit rate 无法解释 TTFT 收益 | 可观测性、容量规划 | 统计 raw hit、usable hit、local usable hit、view hit、bulk load hit、stream restore hit、abandoned hit | 指标覆盖率 100%；验收：每次请求输出 prefix critical path breakdown |
| **L1-SC-TenantIsolation-017** | **L1 推理框架层 / Tenant-Aware KV Key Builder** | 公共 KV 与租户私有 KV 混用可能造成数据泄漏 | 多租户共享 KVCache | KV key 包含 tenant/security domain/cache salt；公共 KV 需显式声明 shareable | 跨租户误命中 = 0；验收：多租户 hash collision 与访问控制测试 |

---

# L2：Connector / 语义适配层 SRS

| 需求唯一标识 | 归属层级与组件 | 核心攻克的技术痛点 | 重点针对的场景 | 研发实施技术要点（2026 技术栈） | 预期可量化核心指标收益和验收方法 |
|---|---|---|---|---|---|
| **L2-SE-UnifiedDescriptor-018** | **L2 Connector 层 / UnifiedKVAccessDescriptor** | Mooncake/NIXL/LMCache/HiCache descriptor 不统一，框架适配成本高 | Connector 统一抽象 | 定义 `UnifiedKVAccessDescriptor`，同时表达 intent、view、bulk、stream、layout、visibility、fallback | Connector 适配代码减少 ≥30%；验收：同一 descriptor 接入 Mooncake/NIXL/LMCache |
| **L2-SE-AccessSemantics-019** | **L2 Connector 层 / Access Semantic Adapter** | memory-view、bulk-transfer、stream-object 的语义差异被隐藏 | CXL/C2C/RDMA/TCP 融合 | Descriptor 显式支持 `MEMORY_VIEW`、`BULK_TRANSFER`、`STREAM_OBJECT`、`MANAGED_INTENT` | 错误路径选择率下降；验收：不同 size/tier 下 path selection 符合矩阵 |
| **L2-CO-VisibilityContract-020** | **L2 Connector 层 / Completion & Visibility Manager** | RDMA completion、CXL/C2C visibility、TCP ACK 语义不同，容易过早 attach | 跨 fabric KV ready 判定 | Connector 返回 `LOCAL_DONE`、`REMOTE_VISIBLE`、`GPU_VISIBLE`、`DURABLE_VISIBLE` | stale KV attach = 0；验收：RDMA→CXL→GPU 可见性故障注入 |
| **L2-PM-BatchLookup-021** | **L2 Connector 层 / Prefix Lookup Client** | 每个 block/page 单独查目录造成 metadata QPS 风暴 | prefix lookup | 支持 batch prefix lookup，输入 hash vector/radix span，返回连续命中 range | remote lookup QPS 降低 ≥50%；lookup p99 降低 ≥30%；验收：长 prompt block 数 sweep |
| **L2-PM-NegativeCache-022** | **L2 Connector 层 / Negative Lookup Cache** | miss 也很贵，大量无效远端目录查询拖慢 TTFT | remote prefix lookup | Connector 维护短 TTL negative cache；按 tenant/model/salt/prefix range 隔离 | remote negative lookup 降低 ≥60%；验收：低复用 workload 压测 |
| **L2-PM-HitQuality-023** | **L2 Connector 层 / Hit Quality Estimator** | 命中后不知道 tier、queue、预计传输时间，无法判断是否加载 | usable hit admission | Connector 返回 tier、bytes、layout、estimated_load_us、queue_depth、replica_count、visibility state | load/recompute 决策准确率 ≥90%；验收：预测耗时 vs 实测耗时误差 <20% |
| **L2-OL-LayoutNegotiation-024** | **L2 Connector 层 / Layout Negotiation Engine** | 不同后端最优布局不同，强行转换会抵消 offload 收益 | block/page transfer、SSD/GDS、RDMA | 协商 `cross_layer_block`、`page_first`、`layer_first`、`page_first_direct`；优先 MB 级连续 bulk layout | descriptor 数降低 ≥50%；effective BW 提升 ≥30%；验收：layout A/B microbench |
| **L2-OL-BulkDescriptor-025** | **L2 Connector 层 / Bulk Transfer Descriptor Builder** | 多 block 不连续传输导致 descriptor 风暴 | HBM↔DDR、RDMA、SSD prefetch | 支持 scatter-gather、coalescing、max descriptor count、min granularity；MB 级 KV block 批量提交 | CPU submit time 降低 ≥40%；验收：perf/eBPF 采集 descriptor 提交开销 |
| **L2-OL-RankSlice-026** | **L2 Connector 层 / Rank-Local Slice Planner** | TP 场景传全量 KV 浪费带宽 | 多卡 TP、PD disaggregation | Descriptor 包含 tp_rank、head range、layer range、offset、length，只传 rank-local KV shard | cross-node bytes 降低接近 `1/TP`；验收：TP=2/4/8 传输量对比 |
| **L2-MM-MultiConsumer-027** | **L2 Connector 层 / Multi-Consumer Handle Manager** | 同一 KV 被多卡/多请求重复加载，形成流量风暴 | 多 decode replica、agent 热 prefix | Multi-consumer KV handle：refcount、consumer bitmap、shared visibility fence | 重复远端拉取次数降低 ≥50%；验收：热门 prefix fanout 压测 |
| **L2-MM-ViewLease-028** | **L2 Connector 层 / Memory View Lease Manager** | memory view 生命周期复杂，易 use-after-free | CXL/C2C shared view | View handle 带 lease、epoch、refcount、revocation callback；过期不可 attach | stale view fault = 0；验收：lease expire/revoke 故障注入 |
| **L2-FT-FallbackContract-029** | **L2 Connector 层 / Fallback Guardrail** | connector 内部偷偷 fallback 到 TCP/SSD 会拉高 TTFT | TTFT-critical KV load | 强制执行 forbidden path；deadline 到期返回 partial hit/miss，不允许无限等待 | TTFT p99 outlier 降低 ≥30%；验收：禁用 RDMA 后检查 fallback 行为 |
| **L2-OB-PathTrace-030** | **L2 Connector 层 / Path Decision Trace** | 上层不知道实际用了 CXL、RDMA 还是 TCP，难以运维 | 可观测性、问题定位 | 每次 KV access 返回 selected_semantic、backend、reason、fallback_reason、latency breakdown | 线上问题定位时间降低；验收：trace 覆盖率 100% |
| **L2-HW-CapabilityDiscovery-031** | **L2 Connector 层 / Backend Capability Discovery** | 后端能力差异大，调用失败后才发现不支持 | 异构后端接入 | Connector 暴露 coherent、atomic、GDR、GDS、RDMA、TCP、QoS、fence、NPU DMA 能力表 | 部署配置错误减少；验收：启动自检与 capability matrix |

---

# L3：传输管理 / KV Store 层 SRS

| 需求唯一标识 | 归属层级与组件 | 核心攻克的技术痛点 | 重点针对的场景 | 研发实施技术要点（2026 技术栈） | 预期可量化核心指标收益和验收方法 |
|---|---|---|---|---|---|
| **L3-SE-PolicyEngine-032** | **L3 传输管理层 / Semantic Policy Engine** | intent 与底层路径之间缺少统一决策层 | semantic-aware transfer management | Transfer Manager 基于 intent、size、deadline、topology、telemetry 选择 view/bulk/stream/recompute | usable hit rate 提升 ≥20%；验收：策略选择与最优路径 oracle 对比 |
| **L3-SE-GranularityDispatch-033** | **L3 传输管理层 / Granularity-Aware Dispatcher** | 小 metadata 用 RDMA/TCP，大 KV 用 memory view，均会错配 | metadata/data 分离 | 规则：64B–4KB metadata 走 memory-view；0.5–8MB KV block/page 走 bulk；cold segment 走 stream/object | lookup p99 降低 ≥30%，bulk BW 提升 ≥20%；验收：粒度 sweep |
| **L3-SE-ViewCopyCostModel-034** | **L3 传输管理层 / View-vs-Copy Cost Model** | direct view 不一定比 copy 快 | CXL/C2C warm KV、NVLink-C2C DDR | 建模 direct read stall、copy cost、reuse count、deadline；动态选择 direct view 或 copy-to-HBM | TPOT 回退 <3%，TTFT 降低 ≥10%；验收：remote-tier counter + A/B |
| **L3-MS-MetadataDataPlane-035** | **L3 KV Store 层 / Metadata Plane + Data Plane** | metadata 查询与 KV data 搬运互相干扰 | 全局 directory、KV data path | metadata directory 放 CXL/C2C/local DDR；KV data 通过 RDMA/DMA/GDS；物理队列隔离 | prefix lookup p99 降低 ≥30%；验收：metadata QPS 与 data BW 混压 |
| **L3-MS-DirectoryMirror-036** | **L3 KV Store 层 / Node-Local Directory Mirror** | 全局目录同步查询放大 TTFT | remote directory lookup | 每节点维护 hot prefix directory mirror、Bloom/Cuckoo filter、replica summary | remote directory RTT 降低 ≥50%；验收：同 rack/cross rack lookup 对比 |
| **L3-MS-MultiReplicaDirectory-037** | **L3 KV Store 层 / Multi-Replica Semantic Directory** | 同一 KV 可在 CXL view、RDMA DDR、SSD object 多副本存在 | 多级 KV directory | KV object directory 记录 view location、bulk source、stream segment、version、tier hotness | path selection 命中最优副本比例 ≥90%；验收：副本故障与拥塞切换 |
| **L3-MS-Tiering-038** | **L3 KV Store 层 / Multi-Tier KV Placement Manager** | HBM/DDR/CXL/SSD/remote DDR 没有统一 promotion/demotion | 多级存储池 | tiering manager：HBM active、DDR warm、CXL pooled、remote DDR hot shared、SSD cold | HBM effective capacity 提升 ≥30%；GPU OOM/preemption 降低 ≥50%；验收：长上下文并发压测 |
| **L3-MS-CostEviction-039** | **L3 KV Store 层 / Cost-Aware Eviction Engine** | LRU 无法表达 KV 的未来复用价值和传输成本 | eviction / placement | 以 `saved_prefill_time - load_cost - memory_rent - interference_cost` 做 eviction score | 每 GB KV 带来的 saved prefill tokens 提升 ≥25%；验收：cache value curve |
| **L3-MS-HotReplication-040** | **L3 KV Store 层 / Hot Prefix Replication Manager** | 热 prefix 单副本打爆远端 DDR/RNIC/CXL device | agent/RAG 热模板、公共 system prompt | 自动 hot prefix replication；按 QPS、p99、queue depth 复制到 remote DDR/CXL/local DDR | hot shard p99 降低 ≥40%；验收：Zipf prefix workload |
| **L3-PD-NodeStagingFanout-041** | **L3 传输管理层 / Node Staging + Fanout Engine** | 多 GPU 同时远端拉同一 KV，形成 cross-node 流量风暴 | TP/DP 多卡、P/D 分离 | remote 只传一次到 node staging DDR/CXL，再通过 NVLink/PCIe P2P/rank slice fanout | cross-node bytes 降低 ≥50%；验收：4/8 GPU node fanout A/B |
| **L3-PD-PushPullRendezvous-042** | **L3 传输管理层 / P-D Rendezvous Orchestrator** | P→D push、D→P pull、directory rendezvous 三种模式混乱 | PD disaggregation | 统一 push/pull/rendezvous orchestration；Mooncake/NIXL/LMCache backend 可选 | P/D handoff p99 降低 ≥20%；验收：PD 分离吞吐与 TTFT |
| **L3-CO-VersionPublish-043** | **L3 KV Store 层 / Versioned Publish Protocol** | data 写完前 metadata 提前可见，会读到半写 KV | KV object publish | data write → checksum/version → fence/flush → publish ready bitmap；失败不暴露 location | data corruption = 0；验收：中断/重启/partial write 故障注入 |
| **L3-CO-CrossFabricBridge-044** | **L3 传输管理层 / Cross-Fabric Visibility Bridge** | RDMA 写 CXL，GPU 通过 C2C/CXL 读，visibility 边界复杂 | RDMA+CXL+C2C 融合 | RDMA completion 后执行必要 flush/invalidate/fence，再更新 state | stale read = 0；`rdma_to_gpu_visible_p99` 可观测；验收：跨 fabric memory ordering 测试 |
| **L3-QO-SemanticQoS-045** | **L3 传输管理层 / Semantic QoS Scheduler** | TTFT load、decode copy、write-back、cold restore 互相抢资源 | 多租户 QoS、tail latency | 按 metadata-critical、TTFT-load、decode-critical、prefetch、writeback、cold-restore 分队列和 token bucket | p99 TTFT 降低 ≥25%；后台任务不影响 TPOT >3%；验收：混合流量压测 |
| **L3-FT-FallbackTrace-046** | **L3 传输管理层 / Fallback Causality Trace** | fallback 原因不可见，运维无法定位性能退化 | 运维、故障恢复 | 记录 view→bulk、bulk→stream、bulk→recompute 原因、耗时、影响请求 | MTTR 降低；验收：每次 fallback 均有 trace_id 与 root cause |
| **L3-OB-PerPathTelemetry-047** | **L3 传输管理层 / Per-Path Telemetry Exporter** | Scheduler 无实时带宽/延迟/拥塞信息，无法正确 admission | 闭环调度 | 暴露 per-path p50/p95/p99、BW、queue depth、retry、fault、throttle、fallback count | load cost 预测误差 <20%；验收：telemetry 与实测 transfer 对齐 |
| **L3-SC-PoolIsolation-048** | **L3 KV Store 层 / Tenant Pool Isolation Manager** | CXL/RDMA/SSD 多租户共享池存在容量和带宽争抢 | 多租户 KV pool | per-tenant capacity、bandwidth、metadata namespace、encryption domain、audit log | 租户间干扰 <5%；越权访问 = 0；验收：多租户 stress 与隔离测试 |
| **L3-HW-CXLAllocator-049** | **L3 KV Store 层 / CXL Shared Allocator** | CXL pooled memory 不是天然 KV store，碎片和 locality 难控 | CXL memory pool | offset-based allocator、hugepage/page coloring、NUMA/fabric locality、free list、defrag | CXL fragmentation <10%；allocation p99 <100us；验收：长稳分配释放测试 |

---

# L4：底层传输 / 硬件层 SRS

| 需求唯一标识 | 归属层级与组件 | 核心攻克的技术痛点 | 重点针对的场景 | 研发实施技术要点（2026 技术栈） | 预期可量化核心指标收益和验收方法 |
|---|---|---|---|---|---|
| **L4-HW-SemCapTable-050** | **L4 硬件抽象层 / Semantic Capability Table** | 硬件能力差异被隐藏，上层误认为所有路径都是 read/write | CXL/C2C/RDMA/TCP/NPU DMA | HAL 输出 semantic capability table：coherent、atomic、DMA、RDMA、GDS、QoS、fence、persistent | 无效 path selection = 0；验收：启动自检 + runtime revalidation |
| **L4-HW-AddrTrans-051** | **L4 硬件抽象层 / Address Translation Telemetry** | memory view 依赖 IOMMU/ATS/SMMU/TLB，尾延迟不可见 | CXL/C2C direct view、NPU/GPU mapping | 暴露 address translation telemetry：TLB miss、page fault、ATS latency、shootdown | direct view p99 可解释；验收：page size/first-touch sweep |
| **L4-CO-FencePrimitive-052** | **L4 硬件抽象层 / Cross-Semantic Fence Primitive** | 跨 CPU/GPU/CXL/RDMA 的可见性缺少统一 fence | memory semantic + transfer semantic 融合 | 提供 `cpu_to_gpu_visible`、`rdma_to_gpu_visible`、`gpu_to_cxl_visible`、`cxl_to_rdma_visible` | stale visibility fault = 0；验收：并发读写/fence 顺序测试 |
| **L4-OL-RegisteredPool-053** | **L4 底层传输层 / Registered Memory Pool** | RDMA/GDR 频繁注册小块内存成本高 | GPUDirect RDMA、NIXL、Mooncake | HBM/DDR/CXL 预注册大块 region，offset suballocation；descriptor 复用 | registration overhead 降低 ≥80%；验收：transfer setup time microbench |
| **L4-OL-GDRPath-054** | **L4 底层传输层 / GPUDirect RDMA Path** | CPU bounce buffer 与 SM copy 占用降低吞吐 | remote DDR ↔ GPU HBM | 优先 GPUDirect RDMA / RDMA READ/WRITE；Mooncake/NIXL background I/O thread；避免 CPU staging | host CPU overhead 降低 ≥30%；effective RDMA BW 提升 ≥20%；验收：CPU cycles/byte 与 BW |
| **L4-MS-CXLC2CTier-055** | **L4 硬件层 / CXL + NVLink-C2C Memory Tier** | CXL/C2C 被误当成 HBM 扩容，可能拖慢 decode | CXL pooled memory、NVLink-C2C coherent memory | CXL/C2C 首先承载 metadata 与 warm KV；active decode KV 默认 copy to HBM；作为近端 memory tier 输入策略 | prefix lookup p99 降低 ≥30%；TPOT 回退 <3%；验收：metadata/direct-KV 分离 A/B |
| **L4-QO-TrafficClass-056** | **L4 底层传输层 / Traffic Class Isolation** | GPU copy engine、RNIC、CXL switch、TCP cold path 互相干扰 | 多流量共存 | TTFT、decode、writeback、cold restore 分 traffic class；RNIC queue、copy stream、CXL switch QoS 分离 | background interference 降低 ≥50%；验收：混合流量下 p99 TTFT/TPOT |
| **L4-OB-RemoteAccessCounter-057** | **L4 硬件抽象层 / Remote-Tier Access Counters** | direct view 是否拖慢 GPU/NPU 不可观测 | CXL/C2C direct access | GPU/NPU runtime 暴露 remote-tier load count、stall cycles、remote BW、L2 miss source | view-vs-copy 决策准确率 ≥90%；验收：counter 与 TPOT 回归分析 |
| **L4-OL-SSDObjectIO-058** | **L4 底层传输层 / SSD + Object I/O Path** | SSD-backed KV 恢复存在 tiny random I/O 与 CPU-centric I/O 问题 | 本地/远端 SSD KV | 支持大 segment layout、GDS/SPDK、GPU-native object I/O、slack-aware I/O scheduling | SSD restore TTFT 降低 ≥30%；GPU stall 降低 ≥50%；验收：NVMe BW 饱和度与 TTFT |
| **L4-FT-TCPColdPath-059** | **L4 底层传输层 / TCP Cold Path Isolation** | TCP fallback 抖动大，但仍需兼容冷路径 | 冷 KV、跨 AZ、object restore | TCP/object 只用于 cold restore/control plane；独立线程池、core、NIC queue、rate limit；TTFT-critical 默认禁止 | TCP fallback 不影响前台 p99；验收：TCP 慢路径混压 |
| **L4-HW-NPUNeutralABI-060** | **L4 硬件抽象层 / Accelerator-Neutral Memory Fabric ABI** | 统一 KV pool 不能只绑定 CUDA/GPU，NPU 缺等价抽象会割裂生态 | GPU/NPU 异构推理集群 | 定义 accelerator-neutral ABI：HBM registration、DMA、remote memory view、completion fence、telemetry、QoS | 同一 Connector 支持 GPU/NPU；验收：CUDA 与 NPU backend API 等价性测试 |
| **L4-FT-RASErrorMap-061** | **L4 硬件抽象层 / RAS-to-KV Error Mapping** | CXL poison、RDMA flush error、GPU page fault、TCP timeout 映射不统一 | 故障恢复、正确性 | RAS-to-KV error mapping：`POISONED_KV`、`STALE_VIEW`、`REMOTE_INVALIDATION`、`FABRIC_TIMEOUT`、`TRANSLATION_FAULT` | 错误恢复路径覆盖率 100%；验收：硬件/软件故障注入 |
| **L4-SC-ViewProtection-062** | **L4 硬件抽象层 / Memory View Protection** | memory view 裸地址共享存在越权访问和悬挂映射风险 | CXL/C2C shared memory、多租户 | read-only mapping、IOMMU/SMMU domain、tenant key、revocation、poison isolation、audit | 越权访问 = 0；view revocation p99 <1ms；验收：安全 fuzz 与 revoke 测试 |

---

## 按研发 owner 的组件聚合视图

| 研发 owner / 组件 | 主要负责需求 |
|---|---|
| **Gateway / Prompt Builder** | L1-PM-PromptCanon-002 |
| **Tokenizer / Prefix Hash Engine** | L1-PM-TokenHash-003、L1-PM-SecureHash-005 |
| **Scheduler / Admission Controller** | L1-PM-PrefixBudget-001、L1-RT-Admission-007、L1-SE-IntentAPI-014 |
| **Router / Placement Client** | L1-RT-CacheAwareRouting-006 |
| **KV Block Manager** | L1-MM-KVSizing-008、L1-MM-Lifecycle-009、L1-OL-ActiveWarmClass-010 |
| **Access Mode Planner** | L1-OL-ViewVsCopy-011、L1-OL-PartialBoundary-012 |
| **Distributed Rank Coordinator** | L1-PD-RankConsensus-013 |
| **Connector Runtime** | L2-SE-UnifiedDescriptor-018、L2-SE-AccessSemantics-019、L2-CO-VisibilityContract-020 |
| **Prefix Lookup Connector** | L2-PM-BatchLookup-021、L2-PM-NegativeCache-022、L2-PM-HitQuality-023 |
| **Bulk Transfer Connector** | L2-OL-LayoutNegotiation-024、L2-OL-BulkDescriptor-025、L2-OL-RankSlice-026 |
| **Memory View Connector** | L2-MM-MultiConsumer-027、L2-MM-ViewLease-028 |
| **Connector Observability / Guardrail** | L2-FT-FallbackContract-029、L2-OB-PathTrace-030、L2-HW-CapabilityDiscovery-031 |
| **Semantic Policy Engine** | L3-SE-PolicyEngine-032、L3-SE-GranularityDispatch-033、L3-SE-ViewCopyCostModel-034 |
| **KV Store / Directory** | L3-MS-MetadataDataPlane-035、L3-MS-DirectoryMirror-036、L3-MS-MultiReplicaDirectory-037 |
| **Tiering / Eviction / Replication** | L3-MS-Tiering-038、L3-MS-CostEviction-039、L3-MS-HotReplication-040 |
| **P/D Transfer Orchestrator** | L3-PD-NodeStagingFanout-041、L3-PD-PushPullRendezvous-042 |
| **Consistency / Fault / QoS** | L3-CO-VersionPublish-043、L3-CO-CrossFabricBridge-044、L3-QO-SemanticQoS-045、L3-FT-FallbackTrace-046 |
| **Telemetry / Pool Isolation / CXL Allocator** | L3-OB-PerPathTelemetry-047、L3-SC-PoolIsolation-048、L3-HW-CXLAllocator-049 |
| **Hardware Abstraction Layer** | L4-HW-SemCapTable-050、L4-HW-AddrTrans-051、L4-HW-NPUNeutralABI-060 |
| **Fence / Visibility / RAS** | L4-CO-FencePrimitive-052、L4-FT-RASErrorMap-061 |
| **RDMA/GDR/DMA Path** | L4-OL-RegisteredPool-053、L4-OL-GDRPath-054 |
| **CXL/C2C Memory Tier** | L4-MS-CXLC2CTier-055、L4-OB-RemoteAccessCounter-057、L4-SC-ViewProtection-062 |
| **QoS / SSD / TCP Cold Path** | L4-QO-TrafficClass-056、L4-OL-SSDObjectIO-058、L4-FT-TCPColdPath-059 |