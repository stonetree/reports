审阅下面这份软件诉求分析和模块分析表，审阅是否有重要缺失的部分。

# KVCache SRS需求列表 V2.1

## 横向模块分类

| 顶层业务模块 (Top-Level Module) | 模块核心职责描述 (第一性原理定位) | L推理调度层需求 ID | LKVConnector 层需求 ID | L传输管理层需求 ID | L底层传输层需求 ID |
| --- | --- | --- | --- | --- | --- |
| TM1: 推理调度与标准接口控制 | 负责北向意图解析、软硬件协同路由决策、级联水位强反压准入与框架协议标准化。 | L1-PM-PrefixBudget-001, L1-RT-Admission-007, L1-RT-CacheAwareRouting-006, L1-VLLM-HIER-SCHED-002, L1-SE-IntentAPI-014, L1-SE-ExpertOverride-015, L1-RT-WatermarkAdmission-024 | L2-CONN-API-STD-001, L2-CONN-TOPO-ROUTE-002, L2-CONN-WatermarkQuery-032 | L3-SE-PolicyEngine-032, L3-SE-UnifiedDescriptor-018, L3-TRANS-CAP-API-002, L3-TRANS-SEM-ENGINE-001, L3-SE-GranularityDispatch-033, L3-SE-ViewCopyCostModel-034, L3-TRANS-TOPO-SENSE-004 | L4-FABRIC-ROUTER-001, L4-HW-SemCapTable-050, L4-HW-AddrTrans-051 |
| TM2: 分布式前缀索引与元数据平面 | 负责微妙级前缀安全碰撞判定、多级目录镜像同步与全局一致性哈希路由。 | L1-PM-SecureHash-005, L1-SGL-PFX-IDX-003, L1-VLLM-PFX-SCHED-006, L1-VLLM-PFX-IDX-005 | L2-CONN-PFX-IDX-005, L2-CONN-META-CACHE-004, L2-PM-BatchLookup-021 | L3-MS-DirectoryMirror-036, L3-MS-MetadataDataPlane-035, L3-MC-PFX-IDX-003, L3-MC-PFX-REPL-004, L3-MS-MultiReplicaDirectory-037, L3-PM-HitQuality-023 | - |
| TM3: 异构分层存储池与生命周期空间 | 负责精细化容量预估、冷热状态机流转、异步内存紧凑整理以及基于多级水位的智能主动搬移决策。 | L1-MM-Lifecycle-009, L1-MM-KVSizing-008, L1-OL-ActiveWarmClass-010, L1-OL-PartialBoundary-012, L1-OL-ViewVsCopy-011, L1-MM-DefragAware-023 | - | L3-MC-POOL-GLOBAL-001, L3-MS-Tiering-038, L3-MC-HIER-STORE-002, L3-MS-CostEviction-039, L3-MC-POOL-ALLOC-005, L3-MC-UBAllocator-049, L3-MC-CompactionEngine-056, L3-MC-IntelligentMigration-057 | L4-C2C-UNIFY-POOL-001, L4-UB-C2C-UNIFY-002, L4-UB-P2P-FABRIC-003 |
| TM4: 硬件加速传输与数据流编排 | 负责多层级流水线重叠、投机预取、散射收束描述符转换及底层零拷贝、DPU 硬件写回加速。 | L1-SGL-PRFCH-SCHED-004, L1-SGL-SWP-XFER-002, L1-VLLM-PRFCH-SCHED-004, L1-VLLM-SWP-XFER-003 | L2-CONN-PRFCH-ENGINE-007, L2-OL-BulkDescriptor-025, L2-OL-LayoutNegotiation-024 | L3-MS-HotReplication-040, L3-TRANS-MUL-ENGINE-003, L3-C2C-POOL-SCHED-002, L3-LMC-PRFCH-ENGINE-001, L3-UB-TOPO-ROUTE-001 | L4-OL-RegisteredPool-053, L4-RDMA-P2P-NPU-001, L4-RDMA-MUL-FABRIC-002, L4-MC-HIER-STORE-001, L4-MC-HIER-STORE-002, L4-MC-HIER-STORE-003, L4-NET-OFFLOAD-DPU-001 |
| TM5: 共享协同、安全隔离与 QoS 管控 | 负责多租户安全隔离、多卡共识、租约级内存无感原子迁移以及主动搬移流量的硬件级隔离。 | L1-SC-TenantIsolation-017, L1-PD-RankConsensus-013 | L2-MM-ViewLease-028, L2-MM-MultiConsumer-027, L2-CONN-DefragPause-031 | L3-CO-VersionPublish-043, L3-QO-SemanticQoS-045, L3-SC-PoolIsolation-048 | L4-CO-FencePrimitive-052, L4-QO-TrafficClass-056, L4-SC-ViewProtection-062, L4-CO-PageMigration-063, L4-QO-MigrationQoS-064 |
| TM6: 全路径全栈可观测性与容错保障 | 负责全链路性能观测追踪（命中了为什么变慢）、路径降级因果追溯与底层 RAS 错误向标准上层错误码的映射。 | L1-OB-SemanticMetrics-016 | L2-FT-FallbackContract-029, L2-OB-PathTrace-030 | L3-OB-PerPathTelemetry-047, L3-FT-FallbackTrace-046, L3-MS-UBC2CTier-055 | L4-FT-RASErrorMap-061, L4-OB-RemoteAccessCounter-057 |

## L1 推理调度层

| 需求唯一标识 | 优先级 | 核心模块 | 需求描述 | 重点针对场景 |
| --- | --- | --- | --- | --- |
| L1-PM-PrefixBudget-001 | P0 | 调度与路由控制 | 本需求要求在 Scheduler 中实现前缀判断预算控制机制：每个请求在进入 prefix lookup 前生成 prefix_decision_deadline_ns 与 max_remote_lookup_us，所有本地索引、远端目录查询、load/recompute 判断必须在预算内完成；若超过预算，系统必须立即降级为 partial hit 或 recompute，避免 raw hit 转化为 TTFT 负收益。 | 共享前缀命中快速判定 |
| L1-RT-Admission-007 | P0 | 调度与路由控制 | 本需求要求 Scheduler 在 prefix hit 后执行 load-vs-recompute admission 判断：只有当 lookup + transfer + attach + rank_sync 小于 recompute_saved_time 且满足 TTFT deadline 时才允许加载 KV，否则必须直接 recompute 或 partial recompute。 | 命中 KV 加载收益判定 |
| L1-RT-CacheAwareRouting-006 | P1 | 调度与路由控制 | 本需求要求 Router 在选择推理实例前查询 KV placement summary，优先将请求路由到已拥有对应 prefix KV 的 HBM、本地 DDR 或同机架远端 DDR 节点；在相同负载下，应优先提升 local usable hit rate，而不是先调度后远端搬运 KV。 | KVCache 亲和路由调度 |
| L1-VLLM-HIER-SCHED-002 | P1 | 调度与路由控制 | 本需求要求扩展vLLM BlockSpaceManager，支持GPU HBM/本地DDR/远端DDR/SSD四层存储状态的枚举、查询与调度决策，使调度器能感知并利用集群级分布式KVCache中的数据，在"加载远端KVCache"与"重新计算"之间做出最优决策，避免不必要的GPU重计算。 | 多层异构存储调度 |
| L1-PM-SecureHash-005 | P1 | 前缀索引与命中判定 | 本需求要求实现两级 hash 校验路径：第一级 fast fingerprint 用于快速候选定位，第二级 secure hash 或 token span 校验用于防止碰撞和跨租户误命中；多租户环境默认启用 secure verification，单租户可信环境允许配置 fast path。 | 多租户共享前缀安全命中 |
| L1-SGL-PFX-IDX-003 | P1 | 前缀索引与命中判定 | 本需求要求将SGLang RadixTree的最长前缀匹配（LPM）核心遍历函数从Python实现迁移至C++/Cython，通过SIMD（AVX2）加速节点键的字符串比较操作，并引入RCU无锁并发设计允许多个LPM查询同时执行，同时采用路径压缩减少RadixTree节点数量。 | 前缀匹配关键路径加速 |
| L1-VLLM-PFX-SCHED-006 | P1 | 前缀索引与命中判定 | 本需求要求将前缀匹配检测（哈希计算+本地查询+远端状态确认）从Scheduler决策的同步串行路径中解耦，通过独立前缀预检测Worker Pool异步执行，使Scheduler在决策时能直接读取已完成结果，远端RTT不再出现在调度关键路径上。 | 前缀匹配关键路径解耦 |
| L1-VLLM-PFX-IDX-005 | P2 | 前缀索引与命中判定 | 本需求要求将vLLM的前缀哈希计算从CPU Python层迁移至GPU端并行计算（xxHash-128 CUDA Kernel），引入每256 tokens的分层哈希Checkpoint实现增量计算，并将哈希计算与token embedding并行化执行，将前缀索引计算耗时从10-20ms压缩至0.5ms以内，消除前缀检测对TTFT的显著贡献。 | 前缀匹配关键路径 |
| L1-SGL-PRFCH-SCHED-004 | P2 | 卸载/加载与预取优化 | 本需求要求在SGLang中实现基于请求到达批次分析的投机性前缀预取机制，通过检测短时间窗口内同前缀请求的批量到达模式，主动触发OFFLOADED状态RadixNode从CPU DDR向GPU HBM的异步预取，将KVCache加载时延隐藏在请求排队等待阶段。 | 投机性前缀预取 |
| L1-SGL-SWP-XFER-002 | P2 | 卸载/加载与预取优化 | 本需求要求实现SGLang的Layer级流式KVCache加载Pipeline，将整体KVCache加载从"等待全部L层就绪才开始计算"改为"每K层就绪即触发对应层的Attention计算"，通过分级CUDA Stream与Event同步实现KVCache加载与prefill计算的深度流水线重叠。 | 流式KV加载Pipeline |
| L1-VLLM-PRFCH-SCHED-004 | P2 | 卸载/加载与预取优化 | 本需求要求在vLLM Scheduler中实现N步Lookahead预测机制，对请求队列中即将被调度的序列提前发起低优先级异步KVCache预取，使数据传输时延与请求排队等待时延充分重叠，从而消除Swap In等待对TTFT关键路径的影响。 | 请求调度预取优化 |
| L1-VLLM-SWP-XFER-003 | P2 | 卸载/加载与预取优化 | 本需求要求实现KVCache Block的批量聚合传输能力，通过CUDA Graph捕获并参数化重放多个非连续Block的批量memcpy，将当前for循环逐Block传输模式替换为单次批量DMA提交，显著提升PCIe带宽利用率并降低CPU launch overhead。 | Swap批量传输优化 |
| L1-MM-Lifecycle-009 | P0 | 容量估算与生命周期 | 本需求要求实现分布式 KV 生命周期状态机，将 KV block/page 明确建模为 ALLOCATED → LOADING → READY → ACTIVE → OFFLOADING → EVICTABLE → RELEASED/FAILED；异步 send/free、request finished、remote load failure 必须通过状态机完成资源回收与错误恢复。 | KVCache 生命周期一致性管理 |
| L1-MM-KVSizing-008 | P1 | 容量估算与生命周期 | 本需求要求提供模型结构感知的 KV block/page 容量估算 API，覆盖 MHA、GQA、MLA、sliding-window、hybrid/recurrent state、TP rank、dtype、layout version 等因素，确保 HBM/DDR/CXL/SSD 预留容量与实际 KV footprint 误差小于 5%。 | 模型 KVCache 容量规格化 |
| L1-OL-ActiveWarmClass-010 | P1 | 驻留策略与局部复用 | 本需求要求框架层显式区分 decode-active KV 与 warm prefix KV：decode-active KV 默认必须驻留 HBM，warm prefix KV 可驻留本地 DDR、本地DDR、本地SSD 或远端节点；系统不得把低带宽 memory tier 直接当作 HBM 扩容使用。 | 活跃 KV 与热 KV 分层驻留 |
| L1-OL-PartialBoundary-012 | P2 | 驻留策略与局部复用 | 本需求要求在 partial prefix hit 场景中自动选择最优 recompute boundary：系统应根据 block/page 边界、命中长度、load 时间和 recompute 时间决定 “加载已命中 prefix + recompute suffix” 的切点，而不是在 partial hit 时简单放弃全部命中。 | 部分前缀命中复用 |
| L1-OL-ViewVsCopy-011 | P2 | 驻留策略与局部复用 | 本需求要求实现 direct-view eligibility 判断：当 KV 对象为 metadata、短 prefix span 或 warm preview 时允许memory view；当 KV 将进入 decode attention active path 时，默认 copy-to-HBM，除非 cost model 证明 direct view 不影响 TPOT。 | 热 KV 近端访问与 HBM 回填选择 |
| L1-SC-TenantIsolation-017 | P0 | ⑥ 多卡协同与观测治理 | 本需求要求 KV key 构造必须包含 tenant、security domain、cache salt、model id、tokenizer hash、template version、layout version 等隔离字段；公共 KV 必须显式标记 shareable，私有 KV 不得跨租户命中或共享。 | 多租户 KVCache 隔离共享 |
| L1-SE-IntentAPI-014 | P0 | ⑥ 多卡协同与观测治理 | 本需求要求北向接口默认采用 KVAccessIntent 语义：上层只描述操作目的、deadline、KV 大小、reuse value、visibility、fallback policy、isolation domain，不直接绑定 UB.memory、RDMA、TCP 等具体路径，由下层 policy engine 决定执行语义。 | 统一 KVCache 访问意图编排 |
| L1-OB-SemanticMetrics-016 | P1 | ⑥ 多卡协同与观测治理 | 本需求要求框架层输出语义化 KV 命中指标，不得只统计 prefix hit rate；必须区分 raw hit、usable hit、local usable hit、view hit、bulk-load hit、stream-restore hit、abandoned hit，并记录每次请求 prefix critical path breakdown。 | KVCache 命中收益可观测 |
| L1-PD-RankConsensus-013 | P1 | ⑥ 多卡协同与观测治理 | 本需求要求在 TP/PP/P-D 分离场景中实现 rank-level prefix consensus：所有参与 rank 必须对 usable prefix length、KV version、ready bitmap、layout version 达成一致后才允许 attach；若 rank 间不一致，必须取 min-safe prefix 或降级 recompute。 | 多卡共享前缀一致复用 |
| L1-SE-ExpertOverride-015 | P3 | ⑥ 多卡协同与观测治理 | 本需求要求在默认 intent API 之外提供专家级 override：允许研发和运维在 A/B 实验、故障绕行、极致性能调优中显式指定 preferred semantics、forbidden semantics、force copy-to-HBM、allow direct view、forbid TCP fallback 等策略。 | 专家级 KV 传输策略控制 |
| L1-MM-DefragAware-023 | P1 | 容量估算与生命周期 | 本需求要求 L调度器支持内存紧凑（Compaction）感知：当底层触发碎片整理时，调度器需动态规避正在被迁移的物理 Block，或通过热备 Block 替换机制维持当前推理序列的连续写入，避免碎片整理导致推理线程死锁或停顿。 | 内存高碎片率推理不中断 |
| L1-RT-WatermarkAdmission-024 | P0 | 调度与路由控制 | 本需求要求调度器引入水位驱动的强反压准入策略。当接收到 L的 Critical 水位告警时，立即切断低优先级请求的 KV 加载意图，强制将新请求降级为 Recompute 或执行排队减速，保护系统不发生 HBM OOM。 | 级联水位高压反压准入 |

## L2 KVConnector层

| 需求唯一标识 | 优先级 | 核心模块 | 需求描述 | 重点针对场景 |
| --- | --- | --- | --- | --- |
| L2-CONN-API-STD-001 | P0 | 统一协议与标准接口 | 本需求要求定义并实施统一的KVConnector Protocol标准接口（Python Protocol + gRPC IDL双形式），覆盖put/get/prefetch/evict/get_status/get_transport_stats六类操作，制定KVPage/{ptr, mem_type, size, layer_idx}和KVMeta/{model_id, prefix_hash, dtype, kv_shape}标准数据结构，并制作conformance test suite，使vLLM和SGLang共用同一套Connector规范。 | 统一接口标准化 |
| L2-CONN-TOPO-ROUTE-002 | P1 | 路径路由与能力探测 | 本需求要求在Connector层实现传输路径质量动态监控与自适应路由能力，通过100ms周期的后台探测实时感知各传输路径的BW和时延，基于传输大小/SLO/路径质量综合评分自动选择最优传输后端，并支持路径故障时200ms内自动完成切换。 | 动态传输路径路由 |
| L2-CONN-PFX-IDX-005 | P0 | 本地元数据缓存与快速判定 | 本需求要求实现两级前缀索引结构：第一级本地 Filter（FPR<0.1%，内存<50MB）在5μs内快速排除确定Miss，避免不必要的分布式RTT；第二级分布式精确索引仅在第一级Filter未排除时才被访问，将整体前缀检测P50时延从5-20ms降至5μs以内（确定Miss场景）。 | 前缀命中快速判断 |
| L2-CONN-META-CACHE-004 | P1 | 本地元数据缓存与快速判定 | 本需求要求在Connector层维护本地in-process元数据缓存（HashMap，默认100K条目），缓存prefix_hash到存储位置的映射关系，通过TTL过期和L3层版本号失效通知保证缓存一致性，使高频复用场景下的元数据查询从5-20ms RTT降至10μs以内。 | 前缀命中快速决策 |
| L2-PM-BatchLookup-021 | P1 | 本地元数据缓存与快速判定 | 本需求要求 Connector 提供 batch prefix lookup API：一次提交 block hash vector、radix span 或 page hash range，返回连续命中区间、tier、location、version 和 estimated load cost，避免每个 block/page 单独访问远端目录造成 metadata RTT 风暴。 | 共享前缀 KVCache 批量查询 |
| L2-CONN-PRFCH-ENGINE-007 | P2 | 预取触发与命中评估 | 本需求要求在Request Arrival阶段实现主动KVCache预取触发机制，当新请求到达Waiting队列时立即查询前缀存储状态（利用L2本地缓存，μs级），对REMOTE_DDR状态的KVCache发起非阻塞式prefetch，并预分配目标NPU Buffer，使KVCache加载时延在请求排队期间完成，预取Buffer利用率目标≥80%。 | 前缀KV隐藏加载时延 |
| L2-OL-BulkDescriptor-025 | P1 | 传输描述与布局协商 | 本需求要求 Connector 将多个不连续 KV block/page 合并为 scatter-gather bulk descriptor，支持 coalescing、max descriptor count、min transfer granularity、batch submit，避免 for-loop 式小块传输放大 CPU submit 与 DMA/RDMA setup 开销。 | KVCache 批量卸载与批量加载 |
| L2-OL-LayoutNegotiation-024 | P1 | 传输描述与布局协商 | 本需求要求 Connector 与后端协商 KV layout：对 RDMA优先选择 MB 级连续 cross-layer block，对 SGLang HiCache/L存储支持 page-first/page-first-direct，对 SSD/object 支持 segment layout，减少 relayout 和小 descriptor 传输。 | KVCache 高效传输布局协商 |
| L2-FT-FallbackContract-029 | P0 | ⑥ 共享访问与故障控制 | 本需求要求 Connector 严格执行 fallback contract：若上层 intent 禁止 TCP、SSD 或 object fallback，则 Connector 不得内部静默降级；若 deadline 到期仍未 ready，必须返回 partial hit、miss 或 recompute signal。 | KV 加载失败快速降级恢复 |
| L2-MM-ViewLease-028 | P0 | ⑥ 共享访问与故障控制 | 本需求要求 memory-view 访问必须以 lease handle 形式暴露，handle 必须携带 epoch、version、refcount、expiry、revocation callback；lease 过期、被撤销或 version 不匹配时，memory view 不得继续 attach 或被 GPU/NPU 访问。 | 共享内存 KVCache 安全租约访问 |
| L2-OB-PathTrace-030 | P1 | ⑥ 共享访问与故障控制 | 本需求要求 Connector 为每次 KV access 生成 path decision trace，记录 selected semantic、selected backend、source/destination tier、reason、fallback reason、latency breakdown、bytes 和 descriptor count，支持运维定位“命中了但变慢”的根因。 | KVCache 传输路径可追踪运维 |
| L2-MM-MultiConsumer-027 | P2 | ⑥ 共享访问与故障控制 | 本需求要求 Connector 支持 multi-consumer KV handle：同一 KV object 被多个 decode replica、TP rank 或请求消费时，应通过 refcount、consumer bitmap 和 shared visibility fence 共享一次加载结果，避免多消费者重复远端拉取。 | 共享前缀 KVCache 批量广播 |
| L2-CONN-DefragPause-031 | P1 | ⑥ 共享访问与故障控制 | 本需求要求 Connector 层实现租约感知的碎片整理暂停与无感原子更替：当底层执行内存块迁移时，若涉及活跃的 Lease 句柄，必须通过 RCU（Read-Copy-Update）无锁机制延迟迁移，或在 50μs 内原子更替虚拟到物理的指针映射。 | 活跃租约下内存无感整理 |
| L2-CONN-WatermarkQuery-032 | P1 | 路径路由与能力探测 | 本需求要求 Connector 将节点内的容量水位提示（Watermark Hints）整合进路径质量探测流中。当目标节点处于 High 水位时，自动拉高该路径的 Cost 评分，触发路由自适应引流，规避过载节点。 | 动态拓扑水位路径规避 |

## L3 传输管理层

| 需求唯一标识 | 优先级 | 核心模块 | 需求描述 | 重点针对场景 |
| --- | --- | --- | --- | --- |
| L3-MC-POOL-GLOBAL-001 | P0 | 统一存储池与分层管理 | 本需求要求实现Mooncake集群级统一KVCache内存池，维护全集群内存注册表，支持节点动态加入/退出，提供基于RDMA的跨节点KVCache直接读取路由，以及两级索引（本地 + 全局），将集群KVCache内存利用率提升至≥90%。 | 集群KVCache统一管理 |
| L3-MS-Tiering-038 | P0 | 统一存储池与分层管理 | 本需求要求实现多级 KV tiering manager：根据 active/warm/cold 状态、reuse probability、load cost、memory rent 和 SLO，将 KV 在 HBM、本地 DDR、远端 DDR、本地 SSD、远端 SSD/object 之间 提级或降级。 | KV 热存储容量扩展 |
| L3-MC-HIER-STORE-002 | P1 | 统一存储池与分层管理 | 本需求要求实现KVCache三层分级调度器，按访问时间将KVCache分配到DDR（热，<1min）/本地SSD（温，1-60min）/远端SSD（冷，>1hr）三层存储，支持后台异步下沉（目标≥2GB/s）和异步预热，以及NPUDirect Storage（File）实现SSD到NPU HBM的零CPU拷贝直读路径。 | KVCache三层冷热分级 |
| L3-MS-CostEviction-039 | P1 | 统一存储池与分层管理 | 本需求要求实现KV提级/降级算法，使用多维因素决定保留、降级或驱逐 KV：score 至少包含 saved prefill time、future reuse probability、transfer cost、interference cost、memory rent 和 tenant priority。 | KVCache 成本感知淘汰与降级 |
| L3-MC-POOL-ALLOC-005 | P2 | 统一存储池与分层管理 | 本需求要求为Mooncake统一内存池实现NUMA感知的KVCache Block分配器，通过查询NPU与CPU NUMA节点的亲和关系，优先将KVCache Block分配到最近NUMA节点的DDR内存（numa_alloc_onnode），将Cross-NUMA KVCache访问比例从当前>50%降至<10%。 | NUMA亲和性KV分配 |
| L3-MC-UBAllocator-049 | P2 | 统一存储池与分层管理 | 本需求要求实现 UB pooled memory 专用 KV allocator：采用 offset-based addressing、hugepage/page coloring、NUMA/fabric locality、free-list、defrag 与 quota 控制，将 UB pool 从裸内存资源变成可管理 KV tier。 | UB 共享 KV 热存储池管理 |
| L3-MS-DirectoryMirror-036 | P0 | 前缀目录与元数据平面 | 本需求要求每个推理节点维护 节点级目录镜像，使确定 miss 和热命中尽量在本地完成，避免分布式 directory RTT 进入 TTFT 主路径。 | 节点级 KVCache 目录镜像加速 |
| L3-MS-MetadataDataPlane-035 | P0 | 前缀目录与元数据平面 | 本需求要求将 KV metadata plane 与 KV data plane 物理和逻辑隔离：prefix directory、manifest、ready bitmap、placement summary 优先放 UB/C2C/local DDR；大块 KV data 走 RDMA/DMA/GDS/SSD 路径。 | KVCache 元数据热路径加速 |
| L3-MC-PFX-IDX-003 | P1 | 前缀目录与元数据平面 | 本需求要求将Mooncake的前缀元数据查询路径从基于TCP的etcd/Redis改造为基于RDMA one-sided READ的直接内存访问，通过一致性哈希分片保证单跳路由并消除多跳开销，同时建立热点key本地副本实现μs级本地命中，将分布式前缀元数据查询P50时延从5-20ms降至100μs以内。 | 前缀命中判断加速 |
| L3-MC-PFX-REPL-004 | P1 | 前缀目录与元数据平面 | 本需求要求为Mooncake分布式前缀索引实现基于一致性哈希环（+100 Virtual Nodes/物理节点）的均衡分片机制，并为每个分片提供N=2副本，通过RDMA heartbeat（1s周期）检测故障并在5s内完成主备切换，消除单点故障导致的大规模KVCache查询不可用。 | 分布式索引高可用 |
| L3-MS-MultiReplicaDirectory-037 | P1 | 前缀目录与元数据平面 | 本需求要求全局 KV directory 支持同一 KV object 的多语义副本记录：包括 UB/C2C memory view、local DDR、remote DDR RDMA source、SSD segment、object key，并维护 version、visibility、hotness 与 cost metadata。 | 多副本 KVCache 全局目录管理 |
| L3-PM-HitQuality-023 | P1 | 前缀目录与元数据平面 | 本需求要求 Connector 在返回 prefix hit 时同时返回 hit quality metadata，包括所在 tier、bytes、layout、queue depth、replica count、estimated load latency、visibility state，使 Scheduler 能判断该 hit 是否能在 TTFT budget 内转化为 usable hit。 | 命中 KV 加载价值评估 |
| L3-SE-PolicyEngine-032 | P0 | 语义策略与路径管理 | 本需求要求 Transfer Manager 实现 semantic policy engine：根据 KVAccessIntent、KV size、deadline、reuse value、topology、telemetry、tier state 自动选择 memory view、bulk transfer、stream object 或 recompute 路径。 | 统一 KVCache 存储池智能路径选择 |
| L3-SE-UnifiedDescriptor-018 | P0 | 语义策略与路径管理 | 本需求要求定义统一 KV access descriptor，显式支持四类访问语义：MEMORY_VIEW、BULK_TRANSFER、STREAM_OBJECT、MANAGED_INTENT，覆盖 Mooncake、NIXL、LMCache、HiCache 后端所需的 key、layout、source/destination tier、semantic type、visibility、deadline、fallback、rank slice 等字段，使同一框架侧请求可在不同 connector 后端间无侵入切换。 | 跨框架 KVCache 传输任务统一编排 |
| L3-TRANS-CAP-API-002 | P0 | 语义策略与路径管理 | 本需求要求实现标准化的硬件传输能力查询API（HardwareCapabilityAPI），暴露 backend capability discovery：包括 coherent view、atomic、RDMA/GDR、GDS、CXL view、TCP stream、QoS、fence、NPU DMA、max segment size、registration requirement 等能力，提供get_transport_capabilities()接口返回List[TransportCapability]，每条能力记录包含{type, src_mem_types, dst_mem_types, max_bw_gbps, p50_latency_us, is_available}，覆盖C2C/UB_MEM/UBLINK/RDMA_NPUDIRECT等全部主流传输类型，使L1/L2层无需硬编码平台判断逻辑。 | 平台无关能力感知 |
| L3-TRANS-SEM-ENGINE-001 | P0 | 语义策略与路径管理 | 本需求要求实现统一传输语义引擎（TransferDecisionEngine），在节点启动时通过HardwareCapabilityRegistry探测并注册所有可用传输能力（C2C/UB/RDMA/UBLINK/PCIe等）的BW/latency特性，在每次KVCache传输请求时基于(src_mem, dst_mem, size, dst_count, latency_req)五元组自动选择最优传输原语，并维护动态路由表（100ms更新），支持完整Fallback Chain。 | 多平台统一传输管理 |
| L3-SE-GranularityDispatch-033 | P1 | 语义策略与路径管理 | 本需求要求 Transfer Manager 按数据粒度自动分发访问语义：64B–4KB metadata 优先 memory-view，0.5–8MB KV block/page 优先 bulk transfer，冷 segment/object 优先 stream/object，避免协议与数据粒度错配。 | KV 元数据与 KV 数据分流传输 |
| L3-SE-ViewCopyCostModel-034 | P1 | 语义策略与路径管理 | 本需求要求实现 view-vs-copy cost model：综合 direct view stall cycles、copy-to-HBM cost、expected reuse count、deadline 和 TPOT 敏感性，判断 UB/C2C warm KV 应直接访问还是先 copy 到 HBM。 | 近端热 KV 直访与回填优化 |
| L3-TRANS-TOPO-SENSE-004 | P1 | 语义策略与路径管理 | 本需求要求实现传输路径质量实时感知机制，以100ms为周期采样各路径P50/P99时延（RDMA pingpong +  Event + UB read test），当检测到路径P99超过基线2倍时自动标记degraded并引流至备用路径，通过EWMA预测下5秒路径质量以提前切换，使路径拥塞期间KVCache传输P99恶化比例控制在1.5x以内。 | 动态传输质量适应 |
| L3-MS-HotReplication-040 | P1 | 传输编排与广播 | 本需求要求对公共 system prompt、agent tool schema、RAG 热模板等 hot prefix 自动复制到多个本地 DDR、UB pool 或远端 DDR shard，复制决策由 QPS、p99 latency、queue depth 和 cross-node bytes 触发。 | 热点前缀 KVCache 多副本扩散 |
| L3-TRANS-MUL-ENGINE-003 | P1 | 传输编排与广播 | 本需求要求实现1→N KVCache多播传输优化引擎，通过路由树构建（最大化局部性最小化跳数）和硬件UD组播+软件P2P relay混合模式，使Disaggregated Prefill场景下同一前缀KVCache多节点分发时发送端BW消耗为O(1)。 | 一对多KVCache高效分发 |
| L3-C2C-POOL-SCHED-002 | P2 | 传输编排与广播 | 本需求要求基于NPU Unified Memory访问统计（NPUMemRangeGetAttrute）实现统一内存中KVCache Block的热度感知动态迁移调度，将访问频次超过阈值（默认10次/s）的Block异步预迁移至NPU HBM（NPUMemPrefetchAsync），将冷降温的Block通过madvise释放HBM页面保留在CPU DDR，维持HBM利用率>85%。 | C2C统一内存热度调度 |
| L3-LMC-PRFCH-ENGINE-001 | P2 | 传输编排与广播 | 本需求要求实现基于访问时序滑动窗口（1000条记录）的访问模式预测引擎，当检测到特定prefix_hash在时间窗口内满足频率阈值（5秒内>3次且最近1次<30s前）时，自动触发该KVCache从SSD到DDR的预加热（后台队列，占用NVMe BW≤30%），将SSD→NPU的端到端时延从>150ms降至<30ms（预热命中场景）。 | 冷KVCache预取加热 |
| L3-UB-TOPO-ROUTE-001 | P2 | 传输编排与广播 | 本需求要求集成拓扑发现，构建完整的UBLINK连通矩阵，在KVCache P2P传输决策时优先选择UBLINK路径（bandwidth优先），实现跨NPU直接内存映射和P2P ，在同节点8卡环境下实现KVCache P2P传输带宽≥700GB/s。 | 机内NPU KVCache P2P高速传输 |
| L3-CO-VersionPublish-043 | P0 | 一致性、隔离与QoS | 本需求要求 KV object 发布必须保证数据完整性：统一暴露 completion/visibility contract：区分 local completion、remote visibility、GPU visibility、durable visibility；只有达到请求要求的 visibility level 后，上层才允许 attach KV 或发布 metadata。KV data 写入完成并校验 checksum 后，执行必要 fence/flush，再更新 version 与 ready bitmap；未完成或失败写入不得暴露给任何 lookup 或 attach。 | KVCache 对象一致发布 |
| L3-QO-SemanticQoS-045 | P0 | 一致性、隔离与QoS | 本需求要求 Transfer Manager 建立语义化 QoS 队列：metadata-critical、TTFT prefix load、decode-critical copy、prefetch、background writeback、cold restore 必须分队列、限速和优先级调度，避免后台流量影响前台 TTFT/TPOT。 | 前台推理与后台 KV 任务流量隔离 |
| L3-SC-PoolIsolation-048 | P0 | 一致性、隔离与QoS | 本需求要求多租户 KV pool 实现容量、带宽、metadata namespace、KV key namespace、encryption domain 和 audit log 隔离；任一租户的 UB/RDMA/SSD KV 使用不得对其他租户造成不可控干扰或越权访问。 | 多租户统一 KV 存储池资源隔离 |
| L3-OB-PerPathTelemetry-047 | P0 | ⑥ 全路径观测与故障追踪 | 本需求要求 Transfer Manager 按路径持续导出 telemetry：包括 UB/C2C view latency、RDMA bandwidth、RDMA CQ latency、copy engine queue、SSD/GDS latency、TCP p99、retry、throttle、fault 和 fallback count。 | KVCache 全路径性能观测 |
| L3-FT-FallbackTrace-046 | P1 | ⑥ 全路径观测与故障追踪 | 本需求要求为每次路径降级生成 fallback causality trace：记录 view→bulk、bulk→stream、bulk→recompute 或 RDMA→TCP 的触发原因、耗时、丢失收益、影响请求和关联硬件 telemetry。 | KVCache 降级链路故障追踪 |
| L3-MS-UBC2CTier-055 | P1 | ⑥ 全路径观测与故障追踪 | 本需求要求 UB/C2C memory tier 首先承载KVCache相关的元数据，譬如 prefix metadata、directory mirror、manifest、ready bitmap、warm KV staging，而不是直接承载大规模 decode-active KV；若用于 direct KV view，必须由 view-vs-copy cost model 判定。 | UBlink-C2C 近端热 KV 存储扩展 |
| L3-MC-CompactionEngine-056 | P0 | 统一存储池与分层管理 | 本需求要求实现集群/节点双级 KVCache 内存紧凑引擎（Compaction Engine）。当 HBM/DDR 外部碎片率超过 30% 时自动触发，采用内存屏障（Memory Barrier）技术异步聚合非连续空闲 Block，将碎片率平抑至 10% 以内。 | 长周期运行内存紧凑整理 |
| L3-MC-IntelligentMigration-057 | P0 | 统一存储池与分层管理 | 本需求要求设计三级水位（Low-70%, High-85%, Critical-95%）监测与智能迁移决策引擎。达到 High 水位时，根据 KV 成本淘汰评分（Saved Prefill Time、QPS、租户优先级）异步批量将 Warm/Cold KVCache 换出至本地 DDR 或 SSD；达到 Critical 水位时，强制拓宽换出带宽并限制换入。 | 跨异构介质智能主动搬移 |

## L4 底层传输层

| 需求唯一标识 | 优先级 | 核心模块 | 需求描述 | 重点针对场景 |
| --- | --- | --- | --- | --- |
| L4-OL-RegisteredPool-053 | P0 | RDMA与零拷贝传输 | 本需求要求底层传输层建立长生命周期 registered memory pool：HBM、DDR、UB region 应预注册为大块可复用区域，通过 offset suballocation 供 RDMA/GDR/NIXL/Mooncake 使用，避免每次 KV block 传输临时注册。 | 高频 KV 传输注册内存池化 |
| L4-RDMA-P2P-NPU-001 | P1 | RDMA与零拷贝传输 | 本需求要求集成NVIDIA NPUDirect RDMA能力，将NPU HBM注册为RDMA可访问内存区域（v_reg_mr），实现NPU HBM→网卡→目标NPU HBM的零CPU拷贝直传路径，消除当前跨节点KVCache传输中2次CPU参与的内存拷贝（NPU→CPU DDR→网卡→远端网卡→CPU DDR→NPU），将端到端传输时延降低40-60%。 | 跨节点KVCache零拷贝传输 |
| L4-RDMA-MUL-FABRIC-002 | P2 | RDMA与零拷贝传输 | 本需求要求实现KVCache的RDMA层1→N广播传输封装，使发送端带宽消耗O(1)（与接收方数量无关）。 | 共享前缀KVCache批量广播 |
| L4-C2C-UNIFY-POOL-001 | P1 | 统一内存与内存语义访问 | 本需求要求在Kunpeng  NPU平台上，统一内存架构实现NPU+CPU DDR+SSD统一KVCache内存池，通过NPUMemPrefetchAsync和热度感知MemoryAdvisor自适应策略，将单节点KVCache有效访问容量从 HBM alone 扩展至HBM + DDR + SSD，使KVCache容量约束得到根本性缓解。 | CPU/NPU统一内存池 |
| L4-UB-C2C-UNIFY-002 | P2 | 统一内存与内存语义访问 | 本需求要求在支持UBLink C2C互联的平台上，为KVCache构建NPU-CPU统一地址空间访问路径，并通过NPUMemPrefetchAsync实现访问模式自适应预取，使NPU能通过指针直接访问CPU侧KVCache，消除显式NPUMemcpy传输开销，将CPU→NPU KVCache访问时延从2-5ms降至<500ns。 | 内存语义零拷贝KV访问 |
| L4-UB-P2P-FABRIC-003 | P3 | 统一内存与内存语义访问 | 本需求要求对接UBFabric Manager，将UB内存区域注册为全局可访问资源并分配Global Access Pointer（GAP），供远端节点直接通过GAP进行load/store操作访问KVCache，无需RDMA注册和显式传输，实现跨节点KVCache内存语义访问，目标访问时延显著低于RDMA传输语义。 | 跨节点内存语义KV访问 |
| L4-MC-HIER-STORE-001 | P2 | DPU卸载与分层存储I/O | 本需求要求实现KVCache三层分级直出，实现NPU直接访问本地SSD（NPUDirectSSD）、NPU直接出网卡（NPUDirectNIC），提升KVCache在不同介质、不同节点之间的流动效率。 | KVCache多层异构架构 |
| L4-MC-HIER-STORE-002 | P3 | DPU卸载与分层存储I/O | 本需求要求实现KVCache on-the-fly 压缩功能，在从NPU卸载到DDR或SSD时，提供on-the-fly无刷压缩功能。 | KVCache多层异构架构 |
| L4-MC-HIER-STORE-003 | P3 | DPU卸载与分层存储I/O | 本需求要求实现KVCache on-the-fly 解压缩功能，在从DDR或SSD加载到NPU时，提供on-the-fly的解压缩功能。 | KVCache多层异构架构 |
| L4-NET-OFFLOAD-DPU-001 | P3 | DPU卸载与分层存储I/O | 本需求要求将KVCache RDMA传输的控制路径（CQ轮询与完成中断通知Host）、数据路径（INT8量化卸载到DPU FPGA）和路由决策（Mooncake路由表本地化到DPU）卸载至 DPU，使Host CPU的KVCache传输相关开销降低≥70%。 | 传输数据路径CPU卸载 |
| L4-FABRIC-ROUTER-001 | P0 | Fabric能力与统一路由 | 本需求要求实现混合传输Fabric路由引擎（Fabric Router），以(src_mem, dst_mem, size, latency_req, dst_count)为输入，基于实时维护的硬件拓扑数据库（每10min刷新）和传输质量反馈，输出最优传输原语（内存语义或传输语义）和具体执行路径，路由决策时延<3μs，在多平台混合集群中正确路由率达到100%。 | 多语义底层传输统一路由 |
| L4-HW-SemCapTable-050 | P0 | Fabric能力与统一路由 | 本需求要求硬件抽象层输出 semantic capability table：对每条路径声明可支持的链路类型，支持 coherent memory view、atomic、DMA、RDMA、GDS、TCP stream、QoS、fence、persistent visibility、NPU DMA，使上层不会误把所有路径当作普通 read/write。 | 异构 KV 传输硬件能力建模 |
| L4-HW-AddrTrans-051 | P1 | Fabric能力与统一路由 | 本需求要求硬件层为 UB/C2C/NPU/NPU memory view 暴露地址翻译 telemetry，包括 IOMMU/ATS/SMMU/TLB miss、page fault、shootdown、page size 和 first-touch 代价，使 direct view 的尾延迟可观测、可优化。 | 共享内存 KV 访问地址转换优化 |
| L4-CO-FencePrimitive-052 | P0 | 一致性、安全与可靠性 | 本需求要求硬件抽象层提供跨语义 fence 原语，至少覆盖 cpu_to_NPU_visible、rdma_to_NPU_visible、NPU_to_UB_visible、UB_to_rdma_visible，确保不同 fabric 间数据完成与可见性边界明确。 | 跨设备 KVCache 可见性同步 |
| L4-FT-RASErrorMap-061 | P0 | 一致性、安全与可靠性 | 本需求要求硬件层将 UB error、RDMA flush error、NPU page fault、TCP timeout、SSD read error、translation fault 映射为统一 KV object fault code，并触发对应 fallback、recompute、replica invalidation 或 quarantine 策略。 | KVCache 硬件故障统一恢复 |
| L4-QO-TrafficClass-056 | P0 | 一致性、安全与可靠性 | 本需求要求底层为不同 KV 流量建立流量隔离：TTFT prefix load、decode-critical copy、metadata lookup、background writeback、SSD cold restore、TCP fallback 应分 RNIC queue、copy stream、UB switch queue 或线程池隔离。 | KVCache 多类业务流量硬件隔离 |
| L4-SC-ViewProtection-062 | P0 | 一致性、安全与可靠性 | 本需求要求 memory view 访问必须具备硬件级保护：read-only mapping、IOMMU/SMMU domain、tenant key、view revocation、poison isolation、audit log；裸地址不得跨租户或跨安全域直接共享。 | 共享内存 KVCache 安全访问保护 |
| L4-OB-RemoteAccessCounter-057 | P1 | 一致性、安全与可靠性 | 本需求要求 NPU/NPU runtime 暴露 remote-tier access counters：包括 UB/C2C load count、remote memory bandwidth、stall cycles、Lmiss source、copy engine utilization，使系统能判断 direct view 是否拖慢 TPOT。 | 近端共享内存 KV 访问性能观测 |
| L4-CO-PageMigration-063 | P1 | 一致性、安全与可靠性 | 本需求要求底层传输层提供硬件级页迁移（Page Migration）与大页（Hugepage）重组原语，允许在不更改上层虚拟地址（GAP/指针）的前提下，在硬件层原子级完成物理内存页复制与 IOMMU/TLB 刷新。 | 硬件级零拷贝内存重组 |
| L4-QO-MigrationQoS-064 | P0 | 一致性、安全与可靠性 | 本需求要求底层传输层为水位触发的主动迁移流量分配专用硬件通道（如特定的 RNIC Queue、PCIe DMA Ring 或独立拷贝引擎），与前台推理流量（TTFT/TPOT）实现物理隔离，确保大规模 KVCache 跨介质搬移时不挤兑推理带宽。 | 主动迁移流量硬件隔离保障 |

## 01_核心模块字典

| 层级 | 核心模块 | 模块说明 |
| --- | --- | --- |
| L1 | 调度与路由控制 | 调度、路由、前缀预算、加载收益判定与多层存储感知调度。 |
| L1 | 前缀索引与命中判定 | GPU/C++ 前缀哈希、RadixTree 加速、前缀检测异步解耦与安全哈希。 |
| L1 | 卸载/加载与预取优化 | Swap 批量传输、流式 KV 加载、Lookahead/投机性预取。 |
| L1 | 容量估算与生命周期 | KV 容量估算与分布式生命周期状态机。 |
| L1 | 驻留策略与局部复用 | Active/Warm 分层、View vs Copy、部分前缀复用。 |
| L1 | 多卡协同与观测治理 | 多卡一致、统一访问意图、专家策略、命中观测与多租户隔离。 |
| L2 | 统一协议与标准接口 | 统一 KVConnector Protocol 与标准数据结构。 |
| L2 | 路径路由与能力探测 | Connector 层动态路径路由与传输质量探测。 |
| L2 | 本地元数据缓存与快速判定 | 本地元数据缓存、两级前缀索引、批量前缀查询。 |
| L2 | 预取触发与命中评估 | 请求到达阶段主动预取与命中状态利用。 |
| L2 | 传输描述与布局协商 | KV 布局协商、批量传输 descriptor 与 coalescing。 |
| L2 | 共享访问与故障控制 | 多消费者共享、View Lease、fallback 契约、路径追踪。 |
| L3 | 统一存储池与分层管理 | 集群统一内存池、冷热分层、Tiering、成本淘汰、NUMA/UB 分配。 |
| L3 | 前缀目录与元数据平面 | 分布式前缀索引、目录镜像、多副本目录、命中质量元数据。 |
| L3 | 语义策略与路径管理 | 统一传输语义、策略引擎、粒度分发、View/Copy 模型、能力和质量感知。 |
| L3 | 传输编排与广播 | UBLink 拓扑路由、一对多分发、冷 KV 预热、统一内存热度调度、热点复制。 |
| L3 | 一致性、隔离与QoS | 一致发布、多租户池隔离、语义化 QoS。 |
| L3 | 全路径观测与故障追踪 | 全路径遥测、fallback 追踪、UB/C2C 热存储扩展观测。 |
| L4 | RDMA与零拷贝传输 | 跨节点零拷贝、1→N RDMA 广播、注册内存池。 |
| L4 | 统一内存与内存语义访问 | CPU/NPU 统一内存池、C2C/UB 内存语义访问、跨节点 memory view。 |
| L4 | DPU卸载与分层存储I/O | DPU 传输卸载、分层直出、卸载压缩与加载解压。 |
| L4 | Fabric能力与统一路由 | 硬件能力表、地址转换遥测、混合 Fabric 路由。 |
| L4 | 一致性、安全与可靠性 | 可见性同步、流量隔离、远端访问计数、RAS 错误映射与共享内存保护。 |
