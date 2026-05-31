# vLLM KVCache Preemption 流程深度分析

> 本文档分析 vLLM (GPU) 和 vLLM-Ascend (NPU) 两个仓库中 KVCache 抢占（preemption）的完整代码流程。
> Preemption 是 HBM 不足时的**同步释放机制**，与 Offloading（异步备份）是两套独立的系统。

---

## 目录

1. [Preemption 与 Offloading 的关系](#1-preemption-与-offloading-的关系)
2. [核心类派生关系](#2-核心类派生关系)
3. [关键类实例化与生命周期](#3-关键类实例化与生命周期)
4. [GPU 路径 (vllm) 完整流程](#4-gpu-路径-vllm-完整流程)
5. [NPU 路径 (vllm-ascend) 完整流程](#5-npu-路径-vllm-ascend-完整流程)
6. [模块间消息传递](#6-模块间消息传递)
7. [时序图](#7-时序图)
8. [Preemption 与 Offloading 的交互](#8-preemption-与-offloading-的交互)
9. [关键函数签名与参数规格](#9-关键函数签名与参数规格)
10. [GPU vs NPU 差异对比](#10-gpu-vs-npu-差异对比)
11. [代码文件索引](#11-代码文件索引)

---

## 1. Preemption 与 Offloading 的关系

### 1.1 核心区别

| 维度 | Preemption (抢占) | Offloading (卸载) |
|------|-------------------|-------------------|
| **目的** | 释放 HBM 给高优先级请求 | 为未来请求建立 CPU 前缀缓存 |
| **GPU 数据** | 直接释放（丢弃） | 保留不动（拷贝一份到 CPU） |
| **是否阻塞调度** | 是（同步完成才能释放块） | 否（完全异步后台） |
| **请求状态变化** | RUNNING → PREEMPTED → WAITING | 不变，继续推理 |
| **触发条件** | HBM 不够分配时 | 请求推理过程中自动备份 |
| **恢复方式** | 重新 prefill（可能命中 prefix cache） | 从 CPU 加载回 GPU |
| **v1 架构** | Free-and-Recompute（无 Swap） | 异步拷贝到 CPU |

### 1.2 vLLM v1 的关键设计决策

**vLLM v1 消除了 v0 中的 Swap 机制**。v0 维护独立的 CPU swap space 和 `SWAPPED`/`WAITING`/`RUNNING` 三态，有显式的 swap_in/swap_out 操作。v1 简化为：

```
v0: RUNNING → SWAPPED (KV 拷贝到 CPU swap space) → WAITING → RUNNING (swap in)
v1: RUNNING → PREEMPTED (KV 直接丢弃) → WAITING → RUNNING (重新 prefill，可能命中 prefix cache)
```

这个简化的理由：
1. 消除了 CPU swap space 的管理复杂度
2. Prefix cache 可以部分恢复已计算的 KV（如果块还没被覆写）
3. 减少了 GPU↔CPU 同步拷贝的延迟

---

## 2. 核心类派生关系

### 2.1 Scheduler 体系

```
Scheduler                                       # vllm/v1/core/sched/scheduler.py
│   主调度器，包含 preemption 决策逻辑
│   schedule() → SchedulerOutput
│   _preempt_request() → 释放 KV 块 + 重置请求
│   update_from_output() → 处理模型输出
│
├── AsyncScheduler                              # vllm/v1/core/sched/scheduler.py
│   异步调度器 (pipeline parallelism)
│
├── RecomputeScheduler                          # vllm-ascend/core/recompute_scheduler.py
│   │   PD 解聚合模式专用
│   │   KV Consumer: 丢弃请求 + 通知客户端重发
│   │   非 KV Consumer: 回退到 upstream _preempt_request()
│   │
│   └── AsyncRecomputeScheduler(AsyncScheduler, RecomputeScheduler)
│
├── SchedulerDynamicBatch                       # vllm-ascend/core/scheduler_dynamic_batch.py
│   │   SLO 感知动态 token budget
│   │   内联 preemption (不走 _preempt_request)
│   │
│   └── (直接继承 Scheduler)
│
├── SchedulerProfilingChunk                     # vllm-ascend/core/scheduler_profiling_chunk.py
│   │   NPU profiling 动态 chunk sizing
│   │   使用 upstream _preempt_request()
│   │
│   └── (直接继承 Scheduler)
│
└── BalanceScheduler (monkey-patch)             # vllm-ascend/patch/platform/patch_balance_schedule.py
    │   跨 DP rank 负载均衡
    │   完整覆盖 schedule()
    │   使用 upstream _preempt_request()
    │
    └── (通过 patch 注入 EngineCoreProc)
```

### 2.2 Request 状态机

```
RequestStatus                                   # vllm/v1/request.py

  WAITING ──────────────────────────────────────┐
     │                                          │
     │ schedule() 分配成功                       │ _preempt_request()
     ▼                                          │
  RUNNING ──────────────────────────────────> PREEMPTED
     │                                          │
     │ 生成完成                                   │ 重新 schedule()
     ▼                                          │
  FINISHED_STOPPED                              │
  FINISHED_LENGTH_CAPPED                        │
  FINISHED_ABORTED                              │
  FINISHED_ERROR                                │
                                                │
  is_finished(status) = status > PREEMPTED      │
  (PREEMPTED 不算 finished)                      │
                                                │
  PREEMPTED 重新调度时:                           │
    status → RUNNING                            │
    num_computed_tokens 从 0 开始                 │
    但可能命中 prefix cache                       │
```

### 2.3 KV Cache 管理链

```
KVCacheManager                                  # vllm/v1/core/kv_cache_manager.py
│   allocate_slots() → KVCacheBlocks | None
│   get_computed_blocks() → (blocks, num_tokens)
│   free(request) → 释放所有块
│
└── KVCacheCoordinator                          # vllm/v1/core/kv_cache_coordinator.py
    │   多组 KV Cache 协调 (混合模型)
    │
    ├── AscendHybridKVCacheCoordinator          # vllm-ascend/patch/platform/patch_kv_cache_coordinator.py
    │   支持 full + sliding window + compressed MLA
    │
    └── SingleTypeKVCacheManager (per group)    # vllm/v1/core/single_type_kv_cache_manager.py
        ├── FullAttentionManager
        ├── SlidingWindowManager
        ├── MambaManager
        ├── CrossAttentionManager
        └── CompressAttentionManager            # vllm-ascend/core/single_type_kv_cache_manager.py
            (DeepSeekV4 DSA, compress_ratio=4/128)

BlockPool                                       # vllm/v1/core/block_pool.py
│   get_new_blocks() → 从空闲池分配
│   free_blocks() → ref_cnt--, 回收到空闲池
│   cache_full_blocks() → 注册到 prefix cache
│
└── free_block_queue: deque[KVCacheBlock]       # LRU 排序的空闲块队列
    cached_block_hash_to_block: dict             # prefix cache 哈希映射
```

---

## 3. 关键类实例化与生命周期

Preemption 系统的所有关键类均运行在 **Scheduler/EngineCore 进程**中。与 Offloading 不同，Preemption 不涉及 Worker 侧的独立组件——抢占决策、块释放、请求重排队全部在 Scheduler 内部完成。

### 3.1 初始化总链

```
EngineCore.__init__()                              [engine/core.py:97]
  │
  ├─ Scheduler.__init__()                           [core/sched/scheduler.py:65]
  │     │
  │     ├─ KVConnectorFactory.create_connector(role=SCHEDULER)  [scheduler.py:125]
  │     │     └─ OffloadingConnector(SCHEDULER)
  │     │           └─ OffloadingConnectorScheduler
  │     │                 └─ CPUOffloadingManager  (Offloading 侧，非 Preemption)
  │     │
  │     ├─ KVCacheManager.__init__()                [scheduler.py:230]
  │     │     │
  │     │     └─ get_kv_cache_coordinator()         [kv_cache_manager.py:141]
  │     │           │
  │     │           └─ UnitaryKVCacheCoordinator / HybridKVCacheCoordinator
  │     │                 │
  │     │                 ├─ BlockPool(num_gpu_blocks, enable_caching, ...)
  │     │                 │     ├─ KVCacheBlock × num_gpu_blocks
  │     │                 │     ├─ FreeKVCacheBlockQueue
  │     │                 │     └─ BlockHashToBlockMap (prefix cache)
  │     │                 │
  │     │                 └─ SingleTypeKVCacheManager[] (per KV group)
  │     │
  │     ├─ FCFSRequestQueue / PriorityRequestQueue  [scheduler.py:195-200]
  │     │     self.waiting = request_queue (等待队列)
  │     │     self.running: list[Request] = [] (运行队列)
  │     │
  │     └─ connector.bind_gpu_block_pool(kv_cache_manager.block_pool)
  │
  └─ (NPU 变体: 根据配置选择不同 Scheduler 子类)
        ├─ RecomputeScheduler                      [recompute_scheduler.py]
        │     └─ AsyncRecomputeScheduler (异步版)
        ├─ SchedulerDynamicBatch                   [scheduler_dynamic_batch.py]
        ├─ SchedulerProfilingChunk                 [scheduler_profiling_chunk.py]
        └─ BalanceScheduler (monkey-patch)         [patch_balance_schedule.py]
```

### 3.2 各关键类详解

#### Scheduler

| 属性 | 说明 |
|------|------|
| **实例化位置** | `EngineCore.__init__()` (`engine/core.py:148`) |
| **运行进程** | Scheduler/EngineCore 进程 |
| **构造参数** | `vllm_config`, `kv_cache_config`, `structured_output_manager`, `block_size`, `hash_block_size`, `log_stats` |
| **构造时创建** | `KVConnectorFactory.create_connector(role=SCHEDULER)` → `OffloadingConnector`；`KVCacheManager` → `KVCacheCoordinator` → `BlockPool`；`waiting` 请求队列；`running` 请求列表 |
| **引用持有者** | `EngineCore.scheduler` |
| **核心方法** | `schedule()` → 每个 engine step 调用一次，包含 preemption 决策；`_preempt_request()` → 执行抢占；`update_from_output()` → 处理模型输出 |
| **Preemption 相关状态** | `self.running: list[Request]` — 运行中请求列表，preemption 从中选牺牲者；`self.waiting: RequestQueue` — 等待队列，被抢占请求放回此处 |

#### KVCacheManager

| 属性 | 说明 |
|------|------|
| **实例化位置** | `Scheduler.__init__()` (`scheduler.py:230`) |
| **运行进程** | Scheduler/EngineCore 进程 |
| **构造时创建** | `KVCacheCoordinator` (通过 `get_kv_cache_coordinator()`)；引用 `block_pool` |
| **引用持有者** | `Scheduler.kv_cache_manager` |
| **Preemption 中的角色** | `allocate_slots()` 返回 None 时触发 preemption；`free(request)` 释放被抢占请求的所有块；`get_computed_blocks()` 在请求恢复时检查 prefix cache 命中 |

#### KVCacheCoordinator (及子类)

| 属性 | 说明 |
|------|------|
| **实例化位置** | `get_kv_cache_coordinator()` (`kv_cache_coordinator.py:635/647`) |
| **选择逻辑** | `enable_caching=False` → `KVCacheCoordinatorNoPrefixCache`；单 KV 组 → `UnitaryKVCacheCoordinator`；多 KV 组 → `HybridKVCacheCoordinator` |
| **NPU 变体** | `AscendHybridKVCacheCoordinator` (通过 `patch_kv_cache_coordinator.py` 注入) |
| **构造时创建** | `BlockPool`；`SingleTypeKVCacheManager[]` (每个 KV cache group 一个) |
| **引用持有者** | `KVCacheManager.coordinator` |
| **Preemption 中的角色** | `free(request_id)` → 委托给各 `SingleTypeKVCacheManager.free()` |

#### BlockPool

| 属性 | 说明 |
|------|------|
| **实例化位置** | `KVCacheCoordinator.__init__()` (`kv_cache_coordinator.py:50`) |
| **运行进程** | Scheduler/EngineCore 进程 |
| **构造参数** | `num_gpu_blocks`, `enable_caching`, `hash_block_size`, `enable_kv_cache_events` |
| **构造时创建** | `KVCacheBlock` 对象数组 (每个 GPU 块一个)；`FreeKVCacheBlockQueue` (空闲块队列)；`BlockHashToBlockMap` (prefix cache 哈希映射) |
| **引用持有者** | `KVCacheCoordinator.block_pool` → `KVCacheManager.block_pool` → 通过 `bind_gpu_block_pool()` 传给 `OffloadingConnector` |
| **Preemption 中的角色** | `free_blocks(ordered_blocks)` → `ref_cnt--`，`ref_cnt==0` 时加入空闲队列；prefix caching 启用时块保留 hash 供未来命中 |

#### SingleTypeKVCacheManager (及子类)

| 属性 | 说明 |
|------|------|
| **实例化位置** | `KVCacheCoordinator.__init__()` (`kv_cache_coordinator.py:66-78`) |
| **子类** | `FullAttentionManager`, `SlidingWindowManager`, `MambaManager`, `CrossAttentionManager`, `CompressAttentionManager` (NPU) |
| **Preemption 中的角色** | `free(request_id)` → `req_to_blocks.pop(request_id)` → **反序**释放块 (尾部先释放，头部后释放，保护 prefix cache) |

#### RequestQueue (FCFS / Priority)

| 属性 | 说明 |
|------|------|
| **实例化位置** | `Scheduler.__init__()` (`scheduler.py:195-200`) |
| **选择逻辑** | `SchedulingPolicy.FCFS` → `FCFSRequestQueue` (deque)；`SchedulingPolicy.PRIORITY` → `PriorityRequestQueue` (heapq) |
| **引用持有者** | `Scheduler.waiting` |
| **Preemption 中的角色** | `prepend_request(request)` → 被抢占请求放到队列头部，比新请求优先调度 |

#### Request (状态机)

| 属性 | 说明 |
|------|------|
| **实例化位置** | 由 API Server / Engine 前端创建，通过 `add_request()` 加入 Scheduler |
| **运行进程** | 跨进程传递：API Server → EngineCore (Scheduler 进程) |
| **Preemption 相关字段** | `status: RequestStatus` (RUNNING/PREEMPTED/WAITING)；`num_computed_tokens: int` (抢占时重置为 0)；`num_preemptions: int` (抢占计数)；`spec_token_ids: list` (抢占时清空) |
| **引用持有者** | `Scheduler.running` (list) 或 `Scheduler.waiting` (queue) |

### 3.3 NPU Scheduler 变体实例化

| 变体 | 实例化位置 | 激活条件 |
|------|-----------|---------|
| `RecomputeScheduler` | `RecomputeSchedulerConfig.initialize_from_config()` | `additional_config.recompute_scheduler_enable = True` 且 PD 解聚合模式 |
| `SchedulerDynamicBatch` | `platform.py` 中的 Scheduler 选择逻辑 | `SLO_limits_for_dynamic_batch` 配置存在 |
| `SchedulerProfilingChunk` | `platform.py` | NPU profiling chunk 配置 |
| `BalanceScheduler` | `patch_balance_schedule.py` monkey-patch | `enable_balance_scheduling = True` |

所有变体都继承 upstream `Scheduler`，共享 `_preempt_request()` 方法（除 `RecomputeScheduler` 在 KV Consumer 模式下覆盖为直接丢弃）。

### 3.4 实例流转总图

```
EngineCore (Scheduler 进程)
  │
  ├─ .scheduler ──→ Scheduler (或 NPU 变体)
  │                    │
  │                    ├─ .connector ──→ OffloadingConnector (SCHEDULER)
  │                    │                    │
  │                    │                    └─ .connector_scheduler
  │                    │                         └─ .manager ──→ CPUOffloadingManager
  │                    │                              (Offloading 侧，非 Preemption 核心)
  │                    │
  │                    ├─ .kv_cache_manager ──→ KVCacheManager
  │                    │                           │
  │                    │                           └─ .coordinator ──→ KVCacheCoordinator
  │                    │                                                 │
  │                    │                                                 ├─ .block_pool ──→ BlockPool
  │                    │                                                 │     │
  │                    │                                                 │     ├─ KVCacheBlock[]
  │                    │                                                 │     ├─ free_block_queue
  │                    │                                                 │     └─ cached_block_hash_to_block
  │                    │                                                 │
  │                    │                                                 └─ .single_type_managers[]
  │                    │                                                      └─ FullAttentionManager 等
  │                    │                                                           └─ req_to_blocks: dict
  │                    │
  │                    ├─ .waiting ──→ RequestQueue (FCFS/Priority)
  │                    │                    └─ deque[Request] / heapq[Request]
  │                    │
  │                    └─ .running ──→ list[Request]
  │                                         └─ 每个 Request 有:
  │                                              .status (RUNNING/PREEMPTED/...)
  │                                              .num_computed_tokens
  │                                              .num_preemptions
  │                                              .block_hashes
```

---

## 4. GPU 路径 (vllm) 完整流程

### 4.1 Preemption 触发

```
Scheduler.schedule()
  │
  ├─> 遍历 running 请求，为每个请求分配新 KV 块
  │
  ├─> kv_cache_manager.allocate_slots(request, num_new_tokens)
  │     │
  │     ├─> 成功: 返回 KVCacheBlocks，继续调度
  │     │
  │     └─> 失败 (返回 None): HBM 不够
  │           │
  │           └─> 进入 preemption 循环:
  │
  │   while True:
  │     new_blocks = allocate_slots(request, num_new_tokens)
  │     if new_blocks is not None:
  │         break  # 分配成功
  │
  │     # 选择牺牲者
  │     if policy == PRIORITY:
  │         victim = max(running, key=lambda r: (r.priority, r.arrival_time))
  │         # 最高 (priority, arrival_time) = 最低优先级 + 最晚到达
  │         running.remove(victim)
  │     else:  # FCFS
  │         victim = running.pop()
  │         # 最后一个 = 最近加入的 = 最年轻的
  │
  │     _preempt_request(victim, timestamp)
  │
  │     if victim == request:
  │         break  # 自己就是最低优先级，无法调度
  │
  │     # 重试分配 (victim 的块已释放)
```

### 4.2 `_preempt_request` 详解

```python
# scheduler.py:935-955

def _preempt_request(self, request: Request, timestamp: float) -> None:
    assert request.status == RequestStatus.RUNNING

    # ① 释放所有 GPU KV Cache 块
    self.kv_cache_manager.free(request)
    #   → KVCacheCoordinator.free(request_id)
    #     → SingleTypeKVCacheManager.free(request_id)
    #       → req_blocks = self.req_to_blocks.pop(request_id)
    #       → BlockPool.free_blocks(reversed(req_blocks))
    #         → 反序释放: 尾部块先进空闲池 (优先被覆写)
    #         → 头部块 (prefix) 后进空闲池 (更可能命中 prefix cache)
    #         → block.ref_cnt -= 1
    #         → if ref_cnt == 0: free_block_queue.append(block)

    # ② 释放编码器缓存 (多模态输入)
    self.encoder_cache_manager.free(request)

    # ③ 设置状态
    request.status = RequestStatus.PREEMPTED
    request.num_computed_tokens = 0          # 重置! 需要重新计算
    if request.spec_token_ids:
        request.spec_token_ids = []          # 清除投机解码 token
    request.num_preemptions += 1             # 计数

    # ④ 记录事件 (统计用)
    if self.log_stats:
        request.record_event(EngineCoreEventType.PREEMPTED, timestamp)

    # ⑤ 放回等待队列头部
    self.waiting.prepend_request(request)
    #   FCFS: deque.appendleft() → 比新请求优先调度
    #   Priority: 按优先级重新插入
```

### 4.3 块释放链详解

```
_preempt_request(request)
  │
  └─> KVCacheManager.free(request)
        │
        └─> KVCacheCoordinator.free(request_id)
              │
              └─> for manager in single_type_managers:
                    manager.free(request_id)
                      │
                      ├─> req_blocks = self.req_to_blocks.pop(request_id)
                      │     例: [block_0, block_1, block_2, ..., block_99]
                      │
                      ├─> ordered_blocks = reversed(req_blocks)
                      │     → [block_99, block_98, ..., block_1, block_0]
                      │     反序释放的意义:
                      │       尾部块 (block_99) 先进空闲池 → 优先被新请求覆写
                      │       头部块 (block_0) 后进空闲池 → 更久保留在 prefix cache 中
                      │
                      └─> BlockPool.free_blocks(ordered_blocks)
                            │
                            ├─> for block in blocks:
                            │     block.ref_cnt -= 1
                            │
                            └─> free_block_queue.append_n(
                                  [b for b in blocks if b.ref_cnt == 0]
                                )
                                │
                                ├─> 如果 prefix caching 启用:
                                │     block 保留 block_hash
                                │     保留在 cached_block_hash_to_block 中
                                │     直到被新请求重新分配时才真正清除
                                │
                                └─> 如果 prefix caching 未启用:
                                      block_hash = None
                                      直接进入空闲池
```

### 4.4 重新调度 (Recompute) 流程

```
下一个 schedule() step:
  │
  ├─> 条件: not preempted_reqs (本步没有新抢占)
  │         and token_budget > 0
  │
  ├─> 从 waiting 队列取请求
  │     request = waiting.peek_request()
  │     (preempted 请求在队列头部，优先处理)
  │
  ├─> 检查 prefix cache 命中
  │     new_computed_blocks, num_new_local_computed_tokens =
  │         kv_cache_manager.get_computed_blocks(request)
  │     │
  │     └─> BlockPool.find_longest_cache_hit(block_hashes)
  │           │
  │           ├─> 如果被释放的块还没被覆写 → 命中!
  │           │     返回已缓存的块 + 命中 token 数
  │           │
  │           └─> 如果块已被覆写 → 未命中
  │                 num_new_local_computed_tokens = 0
  │
  ├─> 分配新块 (仅对未命中的 token)
  │     new_blocks = allocate_slots(
  │         request, num_new_tokens,
  │         num_new_computed_tokens=num_new_local_computed_tokens,
  │         new_computed_blocks=new_computed_blocks,
  │     )
  │
  ├─> 请求状态转换
  │     if request.status == PREEMPTED:
  │         scheduled_resumed_reqs.append(request)
  │     request.status = RUNNING
  │     request.num_computed_tokens = num_computed_tokens
  │
  └─> Model Runner 处理
        │
        ├─> 从 persistent batch 中移除旧条目
        │     input_batch.remove_request(req_id)
        │
        ├─> 重新添加 (block_ids 替换而非追加)
        │     req_state.block_ids = new_block_ids  # 替换!
        │
        └─> Prefill 执行
              仅计算 prefix cache 未覆盖的 token
              如果全部命中 → 只需计算最后 1 个 token
```

### 4.5 SchedulerOutput 中的 Preemption 字段

```python
@dataclass
class SchedulerOutput:                          # vllm/v1/core/sched/output.py
    scheduled_new_reqs: list[NewRequestData]
    scheduled_cached_reqs: CachedRequestData
    num_scheduled_tokens: dict[str, int]
    total_num_scheduled_tokens: int
    finished_req_ids: set[str]
    preempted_req_ids: set[str] | None = None   # ← 本步被抢占的请求 ID

@dataclass
class CachedRequestData:
    req_ids: list[str]
    resumed_req_ids: set[str]                    # ← 从抢占恢复的请求 ID
    new_token_ids: list[list[int]]
    new_block_ids: list[tuple[list[int], ...] | None]
    num_computed_tokens: list[int]
```

---

## 5. NPU 路径 (vllm-ascend) 完整流程

### 5.1 RecomputeScheduler (PD 解聚合模式)

```
RecomputeScheduler.schedule()
  │
  ├─> KV Consumer 模式 (解码节点):
  │     │
  │     ├─> allocate_slots() 失败
  │     │
  │     ├─> 不走 _preempt_request()!
  │     │
  │     ├─> 直接丢弃请求:
  │     │     recomputed_req = running.pop()
  │     │     kv_cache_manager.free(recomputed_req)
  │     │     recomputed_reqs.append(RecomputeReqInfo(
  │     │         request_id, output_token_ids, client_index
  │     │     ))
  │     │
  │     └─> update_from_output() 中:
  │           发出 EngineCoreOutput:
  │             finish_reason = STOP
  │             stop_reason = "recomputed"
  │           → PD proxy 收到后重新发送请求到 prefill 节点
  │
  └─> 非 KV Consumer 模式:
        └─> 回退到 upstream _preempt_request()
```

**为什么 KV Consumer 要直接丢弃？**

```
PD 解聚合架构:
  Prefill 节点 ──── KV Transfer ────> Decode 节点 (KV Consumer)

Decode 节点 HBM 不够时:
  方案 A (upstream): 抢占 → 重新 prefill → 但 decode 节点没有 prompt，无法 prefill
  方案 B (RecomputeScheduler): 通知 PD proxy 重新发送请求到 prefill 节点

方案 B 更合理: decode 节点不负责 prefill，让 prefill 节点重新计算
```

### 5.2 SchedulerDynamicBatch (SLO 感知)

```python
# scheduler_dynamic_batch.py:237-259
# 内联 preemption，不走 _preempt_request()

self.kv_cache_manager.free(preempted_req)
self.encoder_cache_manager.free(preempted_req)
preempted_req.status = RequestStatus.PREEMPTED
preempted_req.num_computed_tokens = 0
self.waiting.prepend_request(preempted_req)

# 差异: 使用 break 而非 continue
# 即: 抢占一个请求后就停止，不再尝试调度更多
# 原因: 动态 batch 需要严格控制 token budget
```

### 5.3 BalanceScheduler (跨 DP 负载均衡)

```python
# patch_balance_schedule.py:275-307
# 在等待远程 KV 传输时，区分新请求和抢占恢复:

if request.num_preemptions:
    request.status = RequestStatus.PREEMPTED    # 抢占恢复
else:
    request.status = RequestStatus.WAITING      # 新请求

# 准入控制: 当任何 DP rank 满载时，阻止新 waiting 请求进入
if not balance_flag:
    break  # 不调度更多 waiting 请求
```

### 5.4 CompressAttentionManager (防止抢占风暴)

```python
# single_type_kv_cache_manager.py:237-244
# DeepSeekV4 压缩 MLA: 限制单个请求的最大块分配

if isinstance(kv_cache_spec, MLAAttentionSpec) and kv_cache_spec.compress_ratio > 1:
    max_compressed_tokens = max_model_len // compress_ratio
    kwargs["max_admission_blocks_per_request"] = cdiv(max_compressed_tokens, block_size) + 1

# 防止: 一个超长输入请求消耗所有块 → 触发级联抢占
```

### 5.5 NPU 专用 swap_blocks

```python
# attention_v1.py:111-122
# NPU attention backend 的 swap_blocks (用于 v0 兼容或特殊场景)

@staticmethod
def swap_blocks(
    src_kv_cache: list[torch.Tensor],    # [key_cache, value_cache]
    dst_kv_cache: list[torch.Tensor],
    src_to_dst: torch.Tensor,            # [N, 2] int64, (src_idx, dst_idx) 对
) -> None:
    src_key_cache, src_value_cache = src_kv_cache
    dst_key_cache, dst_value_cache = dst_kv_cache
    src_indices = src_to_dst[:, 0]
    dst_indices = src_to_dst[:, 1]
    # 使用 tensor indexing + .to(device) 处理跨设备拷贝
    dst_key_cache[dst_indices] = src_key_cache[src_indices].to(dst_key_cache.device)
    dst_value_cache[dst_indices] = src_value_cache[src_indices].to(dst_key_cache.device)
```

### 5.6 NPU Preemption 与 Offloading 的交互

NPU 侧的 preemption 与 offloading 交互比 GPU 侧更复杂，因为 NPU 有多种 KV Connector 实现：

```
NPU Scheduler 变体                    NPU KV Connector
━━━━━━━━━━━━━━━━━                    ━━━━━━━━━━━━━━━━

RecomputeScheduler                   CPUOffloadingConnector (Ascend 专用)
  │                                    ├─ ZMQ RPC → MetadataServer
  ├─ KV Consumer 模式:                  │    └─ CPUKVCacheManager (CPU 块管理)
  │   直接丢弃请求                       └─ SharedMemory 跨 DP rank 共享
  │   → 不触发 offloading flush
  │                                   AscendStoreConnector
  ├─ 非 KV Consumer 模式:               ├─ KVPoolScheduler (外部 KV 池)
  │   _preempt_request()               └─ 跟踪 _preempted_req_ids
  │   → build_connector_meta()             → 清理被抢占请求的 tracker
  │     → jobs_to_flush
  │                                   MooncakeConnectorV1
  │                                     └─ TransferEngine (P2P 传输)
  └─ 抢占后的请求恢复:
      get_num_new_matched_tokens()
      → 检查 CPU/外部 KV 池是否有缓存
      → 如果有 → 从外部加载 (而非重新 prefill)
```

**AscendStoreConnector 对 preemption 的处理** (`pool_scheduler.py`):

```python
# AscendStorePoolScheduler 跟踪被抢占的请求 ID
class AscendStorePoolScheduler:
    def build_connector_meta(self, scheduler_output):
        # 记录 preempted_req_ids
        self._preempted_req_ids = scheduler_output.preempted_req_ids

        # 清理被抢占请求的 tracker
        for req_id in self._preempted_req_ids:
            self._request_trackers.pop(req_id, None)
```

**NPU Block Table 的 swap_row 操作** (`block_table.py`):

```python
# 用于 batch compaction/reordering 时交换请求的块表行
class AscendBlockTable:
    def swap_row(self, src: int, tgt: int) -> None:
        num_blocks_src = self.num_blocks_per_row[src]
        num_blocks_tgt = self.num_blocks_per_row[tgt]
        self.num_blocks_per_row[src] = num_blocks_tgt
        self.num_blocks_per_row[tgt] = num_blocks_src
        self.block_table.np[[src, tgt]] = self.block_table.np[[tgt, src]]

# MultiGroupBlockTable 对所有 KV cache group 执行 swap_row
class MultiGroupBlockTable:
    def swap_row(self, src: int, tgt: int) -> None:
        for bt in self.block_tables:
            bt.swap_row(src, tgt)
```

### 5.7 NPU Scheduler 变体对比总结

| 维度 | Upstream Scheduler | RecomputeScheduler | SchedulerDynamicBatch | BalanceScheduler |
|------|-------------------|-------------------|----------------------|-----------------|
| **preemption 方式** | `_preempt_request()` | KV Consumer: 丢弃; 其他: `_preempt_request()` | 内联 (不走 `_preempt_request`) | `_preempt_request()` |
| **抢占后恢复** | 重新 prefill + prefix cache | KV Consumer: PD proxy 重发; 其他: 同 upstream | 同 upstream | 同 upstream |
| **抢占选择** | PRIORITY: max(priority, arrival); FCFS: pop() | 同 upstream | 同 upstream | 同 upstream |
| **循环抢占** | 支持 (while 循环直到分配成功) | KV Consumer: 不支持 (直接丢弃); 其他: 支持 | 不支持 (break) | 支持 |
| **与 offloading 交互** | jobs_to_flush | KV Consumer: 无交互; 其他: 同 upstream | 同 upstream | 同 upstream |
| **特殊机制** | 无 | `recomputed_reqs` + `stop_reason="recomputed"` | 动态 token budget | `balance_flag` 准入控制 |
| **激活条件** | 默认 | `recompute_scheduler_enable` + PD 模式 | `SLO_limits_for_dynamic_batch` | `enable_balance_scheduling` |

---

## 6. 模块间消息传递

### 6.1 消息总览

```
生产者                          消息类型                              消费者
━━━━━━━━                        ━━━━━━━━                              ━━━━━━━━

Scheduler                       SchedulerOutput                       ModelRunner
  ├─ preempted_req_ids            (集合 of request_id)                 ├─ 从 persistent batch 移除
  └─ scheduled_cached_reqs                                             └─ KV Connector 处理
       .resumed_req_ids           (集合 of request_id)
                                                                    OffloadingConnectorScheduler
                                                                      └─ flush pending store jobs

Scheduler                       Request 状态变更                       RequestQueue
  └─ _preempt_request()           status: RUNNING → PREEMPTED          └─ prepend_request()
                                  num_computed_tokens: → 0                 (放到队列头部)

KVCacheManager                  BlockPool 操作                         BlockPool
  └─ free(request)                req_to_blocks.pop()                   └─ free_blocks()
                                      → ref_cnt--                          → free_block_queue

ModelRunner                     CachedRequestData                      Attention Backend
  └─ resumed_req_ids              block_ids 替换 (非追加)                └─ 重新初始化请求状态

OffloadingConnectorScheduler    OffloadingConnectorMetadata            OffloadingConnectorWorker
  └─ jobs_to_flush                (set of job_id)                       └─ handle_preemptions()
                                                                            → worker.wait()
```

### 6.2 关键消息格式

#### SchedulerOutput (Preemption 相关字段)

```python
@dataclass
class SchedulerOutput:
    # ... 其他字段 ...
    preempted_req_ids: set[str] | None = None
    # 本步被抢占的请求 ID 集合
    # 生产者: Scheduler.schedule()
    # 消费者:
    #   ModelRunner → 从 persistent batch 移除
    #   OffloadingConnectorScheduler → flush pending stores

@dataclass
class CachedRequestData:
    resumed_req_ids: set[str]
    # 从抢占恢复的请求 ID 集合
    # 生产者: Scheduler.schedule() (WAITING 调度阶段)
    # 消费者:
    #   ModelRunner → block_ids 替换 (而非追加)
```

#### RecomputeSchedulerOutput (NPU 专用)

```python
@dataclass
class RecomputeReqInfo:
    request_id: str
    output_token_ids: ConstantList
    client_index: int = 0

@dataclass
class RecomputeSchedulerOutput(SchedulerOutput):
    recomputed_reqs: list[RecomputeReqInfo] | None = None
    # 被丢弃的请求 (KV Consumer 模式)
    # 生产者: RecomputeScheduler.schedule()
    # 消费者: update_from_output() → EngineCoreOutput(stop_reason="recomputed")
```

---

## 7. 时序图

### 7.1 Preemption 完整时序 (GPU)

```
Scheduler              KVCacheManager           BlockPool              ModelRunner
   │                       │                       │                       │
   │ schedule()            │                       │                       │
   │                       │                       │                       │
   │ 遍历 running 请求:    │                       │                       │
   │                       │                       │                       │
   │──allocate_slots(A)───>│                       │                       │
   │<────── None ──────────│  (HBM 不够)           │                       │
   │                       │                       │                       │
   │ 选择牺牲者 C:          │                       │                       │
   │ (最低优先级)           │                       │                       │
   │                       │                       │                       │
   │ _preempt_request(C)   │                       │                       │
   │                       │                       │                       │
   │──free(C)─────────────>│                       │                       │
   │                       │──free_blocks()───────>│                       │
   │                       │                       │ ref_cnt--             │
   │                       │                       │ free_queue.append     │
   │                       │<──────────────────────│                       │
   │                       │                       │                       │
   │ C.status = PREEMPTED  │                       │                       │
   │ C.num_computed = 0    │                       │                       │
   │ waiting.prepend(C)    │                       │                       │
   │                       │                       │                       │
   │──allocate_slots(A)───>│                       │                       │
   │<── KVCacheBlocks ─────│  (成功! C 的块可用了)  │                       │
   │                       │                       │                       │
   │ 构建 SchedulerOutput: │                       │                       │
   │  preempted_req_ids    │                       │                       │
   │   = {C.request_id}    │                       │                       │
   │                       │                       │                       │
   │──SchedulerOutput──────────────────────────────>                       │
   │                       │                       │                       │
   │                       │                       │  remove_request(C)    │
   │                       │                       │  (从 persistent batch) │
   │                       │                       │                       │
   │  ... 下一个 step ...  │                       │                       │
   │                       │                       │                       │
   │ 调度 WAITING 请求:    │                       │                       │
   │ C 在队列头部           │                       │                       │
   │                       │                       │                       │
   │──get_computed_blocks(C)>                      │                       │
   │                       │──find_cache_hit()────>│                       │
   │                       │                       │ 检查 prefix cache     │
   │                       │<── (blocks, tokens) ──│                       │
   │<── (hit_blocks, N) ───│                       │                       │
   │                       │                       │                       │
   │──allocate_slots(C)───>│                       │                       │
   │<── KVCacheBlocks ─────│                       │                       │
   │                       │                       │                       │
   │ C.status = RUNNING    │                       │                       │
   │ resumed_req_ids       │                       │                       │
   │  .add(C.request_id)   │                       │                       │
   │                       │                       │                       │
   │──SchedulerOutput──────────────────────────────>                       │
   │                       │                       │                       │
   │                       │                       │  C 重新加入 batch     │
   │                       │                       │  block_ids 替换       │
   │                       │                       │  prefill 未命中部分   │
```

### 7.2 Preemption 循环时序 (多次抢占)

```
Scheduler
   │
   │ schedule():
   │
   │ allocate_slots(A) → None (不够)
   │   preempt(C) → 释放 10 个块
   │
   │ allocate_slots(A) → None (还不够)
   │   preempt(D) → 释放 8 个块
   │
   │ allocate_slots(A) → None (还不够)
   │   preempt(E) → 释放 12 个块
   │
   │ allocate_slots(A) → 成功! (共释放 30 个块)
   │
   │ SchedulerOutput:
   │   preempted_req_ids = {C, D, E}
   │
   │ 注意: 如果 A 自己就是最低优先级:
   │   preempt(A) → A 被抢占
   │   break → A 无法被调度，放入 waiting 队列
```

### 7.3 NPU RecomputeScheduler 时序 (KV Consumer)

```
RecomputeScheduler     KVCacheManager           PD Proxy               Prefill Node
   │                       │                       │                       │
   │ schedule()            │                       │                       │
   │                       │                       │                       │
   │ allocate_slots(A) → None                      │                       │
   │                       │                       │                       │
   │ victim = running.pop()│                       │                       │
   │──free(victim)────────>│                       │                       │
   │                       │ 释放 GPU 块            │                       │
   │                       │                       │                       │
   │ recomputed_reqs       │                       │                       │
   │  .append(ReqInfo)     │                       │                       │
   │                       │                       │                       │
   │ update_from_output()  │                       │                       │
   │                       │                       │                       │
   │──EngineCoreOutput────────────────────────────>│                       │
   │  finish_reason=STOP   │                       │                       │
   │  stop_reason="recomputed"                     │                       │
   │                       │                       │                       │
   │                       │                       │ 识别 "recomputed"     │
   │                       │                       │ 重新发送请求 ─────────>│
   │                       │                       │                       │
   │                       │                       │                       │ 重新 prefill
   │                       │                       │                       │ 重新 KV transfer
```

---

## 8. Preemption 与 Offloading 的交互

### 8.1 交互场景

当 OffloadingConnector 正在异步备份某个请求的 KV Cache，而该请求被 preempt 时：

```
问题: 请求 C 有 pending store job (GPU→CPU 拷贝中)
      请求 C 被 preempt → GPU 块被释放 → 可能分配给新请求
      如果 DMA 还在读这些 GPU 块 → 读到被覆写的数据 → CPU 备份损坏

解决: flush 机制
      preempt 时标记 jobs_to_flush
      Worker 在执行模型前阻塞等待 flush 完成
      确保 DMA 读完后再让新请求覆写
```

### 8.2 交互流程

```
Scheduler 侧:
━━━━━━━━━━━━

build_connector_meta(scheduler_output):
  │
  ├─> for req_id in scheduler_output.preempted_req_ids:
  │     req_status = self._req_status[req_id]
  │     if req_status has pending store jobs:
  │         jobs_to_flush.update(req_status.transfer_jobs)
  │
  └─> OffloadingConnectorMetadata(
        store_jobs={...},
        load_jobs={...},
        jobs_to_flush={99, 100}    # ← 抢占触发的 flush
      )

_build_store_jobs() 中:
  │
  └─> for req_id, new_block_id_groups, preempted in yield_req_data():
        if preempted:
            for group_state in req_status.group_states:
                group_state.block_ids.clear()    # 清除旧块 ID 追踪

Worker 侧:
━━━━━━━━

handle_preemptions(metadata):
  │
  ├─> 提交上轮积压的 store jobs
  │     for job_id, spec in _unsubmitted_store_jobs:
  │         worker.transfer_async(job_id, spec)
  │
  └─> 阻塞等待 flush jobs
        if metadata.jobs_to_flush:
            worker.wait(metadata.jobs_to_flush)
            # → end_event.synchronize()
            # → CPU 线程挂起，直到 DMA 完成
```

### 8.3 _block_id_to_pending_jobs 防护网

即使没有 OffloadingConnector，Scheduler 也通过 `_block_id_to_pending_jobs` 追踪 pending store：

```
请求 C 完成 → 有 pending store job 102
  → _block_id_to_pending_jobs = {50: {102}, 51: {102}, 52: {102}, 53: {102}}

请求 C 被 preempt → GPU 块 [50,51,52,53] 释放

新请求 B 分配到块 [50, 51]:
  → 发现 50, 51 在 _block_id_to_pending_jobs 中
  → jobs_to_flush.update({102})
  → Worker 收到后 wait({102}) → 阻塞等 store 完成
  → store 完成后，GPU 块 [50,51] 上的旧数据安全备份到 CPU
  → 请求 B 可以安全覆写
```

---

## 9. 关键函数签名与参数规格

### 9.1 Scheduler

```python
# Scheduler._preempt_request
def _preempt_request(self, request: Request, timestamp: float) -> None:
    """
    抢占一个运行中的请求。
    1. 释放所有 GPU KV Cache 块
    2. 释放编码器缓存
    3. 重置 num_computed_tokens = 0
    4. 状态 → PREEMPTED
    5. 放回 waiting 队列头部
    """

# Scheduler.schedule (preemption 相关片段)
def schedule(self) -> SchedulerOutput:
    """
    主调度循环。
    当 allocate_slots() 返回 None 时触发 preemption。
    牺牲者选择:
      PRIORITY: max(priority, arrival_time) = 最低优先级
      FCFS: running.pop() = 最年轻的请求
    循环抢占直到分配成功或自身被抢占。
    """
```

### 9.2 KVCacheManager

```python
# KVCacheManager.free
def free(self, request: Request) -> None:
    """
    释放请求的所有 GPU KV Cache 块。
    块按反序释放 (尾部先释放，头部后释放)。
    如果 prefix caching 启用，块保留 hash 用于未来命中。
    """

# KVCacheManager.get_computed_blocks
def get_computed_blocks(
    self, request: Request
) -> tuple[KVCacheBlocks, int]:
    """
    检查 prefix cache 中是否有匹配的块。
    返回 (已缓存的块, 已计算的 token 数)。
    被抢占的请求重新调度时调用，可能命中之前释放的块。
    """

# BlockPool.free_blocks
def free_blocks(self, ordered_blocks: Iterable[KVCacheBlock]) -> None:
    """
    释放块列表。块应按驱逐优先级排序 (第一个最先被驱逐)。
    ref_cnt -= 1，当 ref_cnt == 0 时加入空闲队列。
    """
```

### 9.3 RecomputeScheduler (NPU)

```python
# RecomputeScheduler.schedule
def schedule(self) -> RecomputeSchedulerOutput:
    """
    PD 解聚合模式调度。
    KV Consumer: 抢占 = 丢弃请求 + 通知客户端重发
    非 KV Consumer: 回退到 upstream _preempt_request()
    """

# RecomputeScheduler.update_from_output
def update_from_output(
    self,
    scheduler_output: SchedulerOutput,
    model_runner_output: ModelRunnerOutput,
) -> dict[int, EngineCoreOutputs]:
    """
    处理 recomputed 请求:
    发出 EngineCoreOutput(finish_reason=STOP, stop_reason="recomputed")
    """
```

---

## 10. GPU vs NPU 差异对比

| 维度 | GPU (vllm) | NPU (vllm-ascend) |
|------|-----------|-------------------|
| **Preemption 策略** | Free-and-Recompute | Free-and-Recompute (默认) / Drop-and-Reissue (KV Consumer) |
| **牺牲者选择** | PRIORITY: max(priority, arrival) / FCFS: pop() | 同 upstream |
| **块释放** | `kv_cache_manager.free()` → 反序释放 | 同 upstream |
| **恢复方式** | 重新 prefill + prefix cache 命中 | 同 upstream / PD proxy 重发 |
| **Prefix cache 利用** | 释放的块保留 hash，可能被重新命中 | 同 upstream |
| **请求状态** | RUNNING → PREEMPTED → RUNNING | 同 upstream / 直接终止 |
| **Scheduler 变体** | 单一 Scheduler | 4 种: Recompute / DynamicBatch / ProfilingChunk / Balance |
| **防抢占风暴** | 无特殊机制 | CompressAttentionManager 限制单请求最大块数 |
| **跨 rank 协调** | 无 | BalanceScheduler 阻止满载 rank 接收新请求 |
| **swap_blocks** | `torch.ops._C_cache_ops.swap_blocks` | `tensor indexing + .to(device)` |

### NPU 独有特性

1. **RecomputeScheduler (KV Consumer 模式)**:
   - 抢占 = 丢弃请求 + 通知 PD proxy 重发
   - 避免 decode 节点做 prefill（decode 节点没有 prompt）

2. **SchedulerDynamicBatch**:
   - 内联 preemption（不走 `_preempt_request`）
   - 抢占一个就停止（严格控制 token budget）

3. **BalanceScheduler**:
   - 跨 DP rank 负载均衡
   - 用 `num_preemptions` 区分新请求和抢占恢复
   - 满载 rank 不接收新 waiting 请求

4. **CompressAttentionManager**:
   - `max_admission_blocks_per_request` 限制
   - 防止 DeepSeekV4 超长输入消耗所有块

5. **NPU swap_blocks**:
   - 使用 tensor indexing + `.to(device)` 而非 CUDA `swap_blocks`
   - 支持 NPU↔CPU 跨设备拷贝

---

## 11. 代码文件索引

### vllm (GPU) 关键文件

| 文件路径 | 作用 |
|---------|------|
| `vllm/v1/core/sched/scheduler.py` | 主调度器，`_preempt_request()` 和 preemption 决策 |
| `vllm/v1/core/sched/output.py` | `SchedulerOutput` (preempted_req_ids, resumed_req_ids) |
| `vllm/v1/request.py` | `Request` 类，`RequestStatus` 枚举 (PREEMPTED 状态) |
| `vllm/v1/core/kv_cache_manager.py` | `KVCacheManager.free()` 和 `get_computed_blocks()` |
| `vllm/v1/core/block_pool.py` | `BlockPool.free_blocks()` — ref_cnt 和空闲队列 |
| `vllm/v1/core/single_type_kv_cache_manager.py` | 反序释放块逻辑 |
| `vllm/v1/core/kv_cache_coordinator.py` | 多组 KV Cache 协调 free |
| `vllm/v1/core/sched/request_queue.py` | `prepend_request()` — 抢占请求放到队列头部 |
| `vllm/v1/worker/gpu_model_runner.py` | Model Runner: 移除 preempted 请求，恢复时替换 block_ids |

### vllm-ascend (NPU) 关键文件

| 文件路径 | 作用 |
|---------|------|
| `vllm_ascend/core/recompute_scheduler.py` | **RecomputeScheduler** — PD 解聚合模式，KV Consumer 丢弃请求 |
| `vllm_ascend/core/scheduler_dynamic_batch.py` | **SchedulerDynamicBatch** — SLO 感知，内联 preemption |
| `vllm_ascend/core/scheduler_profiling_chunk.py` | **SchedulerProfilingChunk** — NPU profiling 动态 chunk |
| `vllm_ascend/patch/platform/patch_balance_schedule.py` | **BalanceScheduler** — 跨 DP rank 负载均衡 |
| `vllm_ascend/core/single_type_kv_cache_manager.py` | **CompressAttentionManager** — 防抢占风暴 |
| `vllm_ascend/attention/attention_v1.py` | NPU `swap_blocks()` — tensor indexing 实现 |
| `vllm_ascend/platform.py` | Scheduler 选择逻辑 |
| `vllm_ascend/ascend_config.py` | `recompute_scheduler_enable` 配置 |
