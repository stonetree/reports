# vLLM KVCache Offloading 流程深度分析

> 本文档分析 vLLM (GPU) 和 vLLM-Ascend (NPU) 两个仓库中 KVCache 卸载（offload/store）和加载（load）的完整代码流程，
> 涵盖从调度器决策到底层数据传输的全链路，包括类派生关系、消息格式、时序图。
>
> 最后更新: 2026-05-31

---

## 目录

1. [整体架构概览](#1-整体架构概览)
2. [设计理念与组件职责详解](#2-设计理念与组件职责详解)
3. [核心类派生关系](#3-核心类派生关系)
4. [关键类实例化与生命周期](#4-关键类实例化与生命周期)
5. [GPU 路径 (vllm) 完整流程](#5-gpu-路径-vllm-完整流程)
6. [NPU 路径 (vllm-ascend) 完整流程](#6-npu-路径-vllm-ascend-完整流程)
7. [模块间消息传递](#7-模块间消息传递)
8. [时序图](#8-时序图)
9. [底层传输机制详解](#9-底层传输机制详解)
10. [关键函数签名与参数规格](#10-关键函数签名与参数规格)
11. [GPU vs NPU 差异对比](#11-gpu-vs-npu-差异对比)
12. [代码文件索引](#12-代码文件索引)

---

## 1. 整体架构概览

### 1.1 Offloading 的定位

Offloading 是**异步备份机制**，不是 HBM 释放机制：

| 维度 | Offloading (卸载) | Preemption (抢占) |
|------|-------------------|-------------------|
| **目的** | 为未来请求建立 CPU 前缀缓存 | 释放 HBM 给高优先级请求 |
| **GPU 数据** | 保留不动（拷贝一份到 CPU） | 直接释放（丢弃或重算） |
| **是否阻塞调度** | 否（完全异步后台） | 是（同步完成才能释放块） |
| **请求状态** | 不变，继续推理 | RUNNING → PREEMPTED → WAITING |
| **触发条件** | 请求推理过程中，已完成的块自动备份 | HBM 不够分配时 |

### 1.2 核心组件分层

```
┌─────────────────────────────────────────────────────────────────┐
│                     Scheduler 层 (调度决策)                       │
│  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────┐  │
│  │   Scheduler      │  │ KVCacheManager   │  │ KVConnector   │  │
│  │ (请求调度+抢占)   │  │ (GPU块分配/释放)  │  │ (卸载决策)     │  │
│  └──────────────────┘  └──────────────────┘  └───────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│                     Connector 层 (传输编排)                       │
│  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────┐  │
│  │ Offloading       │  │ CPU Offloading   │  │ KV Pool       │  │
│  │ Connector        │  │ Connector        │  │ Connector     │  │
│  │ (vllm upstream)  │  │ (vllm-ascend)    │  │ (分布式KV池)   │  │
│  └──────────────────┘  └──────────────────┘  └───────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│                     Handler 层 (数据传输)                         │
│  ┌──────────────────┐  ┌──────────────────┐                     │
│  │ SingleDirection  │  │ CpuNpu           │                     │
│  │ OffloadingHandler│  │ OffloadingHandler│                     │
│  │ (GPU, CUDA流)    │  │ (NPU, NPU流)     │                     │
│  └──────────────────┘  └──────────────────┘                     │
├─────────────────────────────────────────────────────────────────┤
│                     Kernel 层 (底层拷贝)                          │
│  ┌──────────────────┐  ┌──────────────────┐                     │
│  │ cuMemcpyBatch    │  │ aclrtMemcpy      │                     │
│  │ Async (CUDA)     │  │ BatchAsync(CANN) │                     │
│  └──────────────────┘  └──────────────────┘                     │
└─────────────────────────────────────────────────────────────────┘
```

### 1.3 两条并行的卸载路径

| 路径 | 仓库 | 入口类 | 特点 |
|------|------|--------|------|
| **OffloadingConnector** | vllm (upstream) | `OffloadingConnector` → `OffloadingSpec` | 通用框架，GPU/NPU 均可用，基于 `OffloadingHandler` 抽象 |
| **CPUOffloadingConnector** | vllm-ascend | `CPUOffloadingConnector` | Ascend 专用，支持 prefix caching 感知，使用 ZMQ RPC + 共享内存 |

---

## 2. 设计理念与组件职责详解

### 2.1 Connector 设计理念

vLLM 的 Connector 设计理念：**将"数据怎么搬"从"数据搬什么"中彻底解耦**。

#### 三层分离

```
┌─────────────────────────────────────────────────────┐
│  决策层: Scheduler + KVCacheManager                  │
│  职责: 决定"搬什么"、"什么时候搬"、"搬到哪个逻辑位置"  │
│  不关心: 底层用什么 API 搬数据                        │
├─────────────────────────────────────────────────────┤
│  编排层: Connector (KVConnectorBase_V1)              │
│  职责: 将决策层的意图翻译为传输层的操作                 │
│  接口: 统一的抽象接口，所有 Connector 实现相同协议      │
├─────────────────────────────────────────────────────┤
│  传输层: Handler / DMA / NCCL / RDMA                │
│  职责: 执行实际的字节搬运                             │
│  不关心: 为什么要搬、搬的是 KV Cache 还是别的什么       │
└─────────────────────────────────────────────────────┘
```

#### 四个设计约束

**1. 双角色分离**: 同一个 Connector 类在两个进程中各实例化一次，但行为完全不同：

```python
class OffloadingConnector(KVConnectorBase_V1):
    def __init__(self, config, role, kv_cache_config):
        if role == KVConnectorRole.SCHEDULER:
            self.connector_scheduler = OffloadingConnectorScheduler(...)
            # 只做元数据管理，不碰张量
        elif role == KVConnectorRole.WORKER:
            self.connector_worker = OffloadingConnectorWorker(...)
            # 持有张量，执行传输
```

**2. 统一接口协议**: 所有 Connector 实现相同的抽象接口，Scheduler 通过接口调用，不感知具体实现。换一种传输方式，只需换 Connector 实现，Scheduler 一行不改。

**3. 元数据驱动**: Scheduler 和 Worker 之间只传递元数据（块 ID、job ID、状态），不传递实际数据。DMA 引擎直接在 GPU/CPU 内存间搬运数据，不经过 Scheduler 进程。

**4. 插件化注册**: 通过 Factory + 注册表实现零修改扩展。新增 Connector 只需实现接口 + 注册到 Factory，用户通过 `--kv-connector` 配置启用。

#### 如果没有 Connector

```python
# 没有 Connector 的 Scheduler (耦合严重):
def schedule(self):
    for request in self.running:
        blocks = self.kv_cache_manager.allocate_slots(request)
        if blocks is None:
            # 硬编码: CUDA memcpy 到 CPU
            cudaMemcpyAsync(cpu_buf, gpu_buf, size, D2H, stream)
            # 硬编码: 如果是多节点，用 NCCL
            if self.is_distributed:
                ncclSend(gpu_buf, size, peer_rank)
            # 硬编码: 如果是 Ascend NPU
            if self.is_npu:
                aclrtMemcpyAsync(...)
```

有了 Connector 之后：

```python
# 有 Connector 的 Scheduler (完全解耦):
def schedule(self):
    for request in self.running:
        blocks = self.kv_cache_manager.allocate_slots(request)
        if blocks is None:
            self._preempt_request(victim)
    # 统一的 Connector 接口，不关心底层实现
    num_matched = self.connector.get_num_new_matched_tokens(request, computed)
    self.connector.update_state_after_alloc(request, blocks, num_matched)
    metadata = self.connector.build_connector_meta(scheduler_output)
```

### 2.2 Connector 注册表 (15 种)

`KVConnectorFactory` 在 `factory.py:148-236` 注册了 15 种 Connector：

| # | 名称 | 场景 | 用途 |
|---|------|------|------|
| 1 | **ExampleConnector** | 教学/开发参考 | 展示如何实现最基础的 KV connector |
| 2 | **ExampleHiddenStatesConnector** | 教学/开发参考 | 展示如何传输 hidden states |
| 3 | **P2pNcclConnector** | GPU 间直连 | 使用 NCCL P2P 在同节点多 GPU 间直传 KV cache |
| 4 | **LMCacheConnectorV1** | 外部系统集成 | 集成 LMCache，将 KV cache 存储到 Redis/磁盘等 |
| 5 | **LMCacheMPConnector** | LMCache 多进程 | LMCache 的多进程优化版本 |
| 6 | **NixlConnector** | 高性能网络 | 集成 NVIDIA NIXL，跨节点 RDMA/InfiniBand 传输 |
| 7 | **MultiConnector** | 组合多个 connector | 同时使用多个 connector，按优先级选择 |
| 8 | **MoRIIOConnector** | MoRIIO 网络 | 集成 MoRIIO 高性能网络库 |
| 9 | **OffloadingConnector** ⭐ | 异步 CPU offloading | 将已完成的 KV cache 异步备份到 CPU 内存 |
| 10 | **DecodeBenchConnector** | 性能测试 | decode 阶段的性能测试/benchmark |
| 11 | **MooncakeConnector** | 字节 Mooncake | 集成字节跳动 Mooncake KV cache 传输系统 |
| 12 | **MooncakeStoreConnector** | Mooncake 存储 | Mooncake 的存储优化版本 |
| 13 | **FlexKVConnectorV1** | 灵活传输 | 支持多种后端或动态路由的灵活 KV 传输 |
| 14 | **SimpleCPUOffloadConnector** ⭐ | 简单 CPU offloading | 直接操作 GPU BlockPool ref_cnt 的简单实现 |
| 15 | **HF3FSKVConnector** | 3FS 文件系统 | 集成 3FS 分布式文件系统持久化 KV cache |

按场景分类：

| 类别 | Connector |
|------|-----------|
| **教学/示例** | ExampleConnector, ExampleHiddenStatesConnector |
| **GPU 直连** | P2pNcclConnector |
| **CPU Offload** | OffloadingConnector, SimpleCPUOffloadConnector |
| **外部系统集成** | LMCacheConnectorV1, LMCacheMPConnector, MooncakeConnector, MooncakeStoreConnector, FlexKVConnectorV1, HF3FSKVConnector |
| **高性能网络** | NixlConnector, MoRIIOConnector |
| **组合/测试** | MultiConnector, DecodeBenchConnector |

### 2.3 OffloadingSpec 详解

#### Spec 的本质

`OffloadingSpecFactory.create_spec()` 生成的是一个**平台相关的卸载规格配置对象**，它是 `OffloadingSpec` 抽象基类的具体实现。Spec 是一个 **"配置 + 工厂"** 的组合体：

1. **平台特性**: GPU/CPU/NPU 的块大小、内存布局、传输方式
2. **工厂方法**: 如何创建适合该平台的 Manager 和 Handler

#### 继承关系

```
OffloadingSpec (ABC)                    # vllm/v1/kv_offload/base.py
  │
  ├── CPUOffloadingSpec                 # vllm/v1/kv_offload/cpu/spec.py
  │     GPU 平台默认实现
  │
  ├── TieringOffloadingSpec             # vllm/v1/kv_offload/tiering/spec.py
  │     分层存储（CPU + SSD/远程）
  │
  └── NPUOffloadingSpec                 # vllm-ascend/kv_offload/npu.py
        NPU 平台实现
```

#### 工厂模式

```python
# factory.py
class OffloadingSpecFactory:
    _registry = {
        "CPUOffloadingSpec": ("vllm.v1.kv_offload.cpu.spec", "CPUOffloadingSpec"),
        "TieringOffloadingSpec": ("vllm.v1.kv_offload.tiering.spec", "TieringOffloadingSpec"),
    }

    def create_spec(config, kv_cache_config) -> OffloadingSpec:
        spec_name = extra_config.get("spec_name", "CPUOffloadingSpec")
        spec_cls = cls._registry[spec_name]()
        return spec_cls(config, kv_cache_config)
```

#### Spec 的两个核心工厂方法

**`get_manager()` → 调度器侧 Manager**:
- 调用时机: `OffloadingConnectorScheduler.__init__()` 时
- 返回: `CPUOffloadingManager`（管理 CPU 块池、LRU 驱逐、ref_cnt）
- 运行进程: Scheduler 进程

**`get_handlers(kv_caches)` → Worker 侧 Handler**:
- 调用时机: `OffloadingConnectorWorker._register_handlers()` 时（需要 GPU 张量）
- 返回: Handler 实例（执行实际数据传输）
- 运行进程: Worker 进程

#### 不同平台的 Spec 差异

| 平台 | Spec 类 | Manager | Handler | 传输方式 |
|------|---------|---------|---------|----------|
| **GPU** | `CPUOffloadingSpec` | `CPUOffloadingManager` | `CpuGpuOffloadingHandlers` (2个) | `cuMemcpyBatchAsync` |
| **NPU** | `NPUOffloadingSpec` | `CPUOffloadingManager` (复用) | `CpuNpuOffloadingHandler` (1个双向) | `aclrtMemcpyBatchAsync` |
| **分层** | `TieringOffloadingSpec` | `TieringOffloadingManager` | `CpuGpuOffloadingHandlers` + 远程 Handler | CPU + SSD/网络 |

#### 两个进程中的 Spec 实例

`CPUOffloadingSpec` 在 Scheduler 进程和 Worker 进程中各有一个**独立实例**：

| 维度 | Scheduler 进程中的 Spec | Worker 进程中的 Spec |
|------|------------------------|---------------------|
| **调用的工厂方法** | `get_manager()` → 创建 Manager | `get_handlers()` → 创建 Handler |
| **不调用的方法** | `get_handlers()` (没有 GPU 张量) | `get_manager()` (Manager 是调度器侧) |
| **`_manager` 字段** | 首次调用后非 None | 始终为 None |
| **`_handlers` 字段** | 始终为 None | 首次调用后非 None |

### 2.4 核心组件职责对比

#### KVConnectorFactory vs KVCacheManager vs bind_gpu_block_pool

| 组件 | 职责 | 一句话 |
|------|------|--------|
| **KVConnectorFactory** | 根据配置决定创建哪种 Connector | "选谁来做" |
| **KVCacheManager** | GPU KV Cache 块的分配/释放/前缀缓存 | "GPU 块怎么分" |
| **bind_gpu_block_pool** | 将 GPU BlockPool 引用注入 Connector | "让 Connector 看到 GPU 块池" |

**为什么需要 bind？** 创建顺序依赖：

```python
# scheduler.py — 创建顺序
self.connector = KVConnectorFactory.create_connector(...)  # ① 先创建 Connector
self.kv_cache_manager = KVCacheManager(...)                 # ② 再创建 KVCacheManager
self.connector.bind_gpu_block_pool(self.kv_cache_manager.block_pool)  # ③ 最后绑定
```

Connector 创建时 `BlockPool` 还不存在，所以必须通过后续 `bind` 注入。

**谁真正用了 bind？**

| Connector | 是否 override | 用途 |
|-----------|-------------|------|
| **OffloadingConnector** | 否 (空操作) | 不需要直接操作 GPU BlockPool |
| **SimpleCPUOffloadConnector** | **是** | 需要直接操作 GPU BlockPool 的 `touch()`、`free_blocks()` |
| **MultiConnector** | **是** | 代理给所有子 Connector |

**SimpleCPUOffloadConnector 为什么需要 GPU BlockPool？**

它同时管理 GPU 和 CPU 两套 BlockPool，需要协调两者的 ref_cnt：

```python
# manager.py — SimpleCPUOffloadScheduler 对 GPU BlockPool 的操作

# 用途 1: load 时 touch GPU 块防止被抢占释放 (line 376-378)
self._gpu_block_pool.touch(
    [self._gpu_block_pool.blocks[bid] for bid in gpu_block_ids]
)

# 用途 2: store 完成后释放 GPU 块的 ref_cnt (line 722-724)
self._gpu_block_pool.free_blocks(
    self._gpu_block_pool.blocks[bid] for bid in gpu_block_ids
)

# 用途 3: 遍历 GPU 空闲队列找可卸载的块 (line 455-470)
gpu_pool = self._gpu_block_pool
free_queue = gpu_pool.free_block_queue
```

#### OffloadingConnectorScheduler vs CPUOffloadingManager

```
OffloadingConnectorScheduler = 物流调度中心
  "这批货(GPU块)什么时候发、发到哪、到了没"
  不关心仓库有多大、货架怎么摆

CPUOffloadingManager = 仓库管理员
  "仓库(CPU)还有多少空位、放哪个货架、满了扔谁的旧货"
  不关心货从哪来、什么时候来、谁来搬
```

| 维度 | OffloadingConnectorScheduler | CPUOffloadingManager |
|------|------------------------------|---------------------|
| **职责** | 卸载流程编排 | CPU 块池管理 |
| **知道什么** | 哪些 GPU 块需要卸载、传输任务状态 | CPU 块池状态、LRU 顺序 |
| **不知道什么** | CPU 块物理 ID、驱逐策略 | GPU 块 ID、请求调度状态 |
| **核心方法** | `_build_store_jobs()`, `build_connector_meta()` | `prepare_store()`, `lookup()`, `evict()` |
| **运行进程** | Scheduler 进程 | Scheduler 进程 |
| **持有关系** | `self.manager` → CPUOffloadingManager | 被 OffloadingConnectorScheduler 持有 |

两者的调用关系：

```
OffloadingConnectorScheduler 每个 step 中:
  ① get_num_new_matched_tokens()
     └─→ manager.lookup(key)          ← 查 CPU 缓存
     └─→ manager.touch(keys)          ← 更新 LRU

  ② update_state_after_alloc()
     └─→ manager.prepare_load(keys)   ← 锁定 CPU 块

  ③ _build_store_jobs()
     └─→ manager.prepare_store(keys)  ← 分配 CPU 块
     └─→ manager.touch(keys)          ← 更新 LRU

  ④ update_connector_output()
     └─→ manager.complete_store(keys) ← 标记 CPU 块就绪
     └─→ manager.complete_load(keys)  ← 解锁 CPU 块
```

#### KVCacheManager vs CPUOffloadingManager

两者**没有职责重叠**。它们管理两个完全独立的块池：

| 维度 | KVCacheManager | CPUOffloadingManager |
|------|---------------|---------------------|
| **管理的资源** | GPU HBM 块 | CPU DRAM 块 |
| **谁调用它** | Scheduler | OffloadingConnectorScheduler |
| **是否知道对方** | 不知道 | 不知道 |
| **分配块** | `allocate_slots()` | `prepare_store()` → `_allocate_blocks()` |
| **释放块** | `free()` | `complete_store(success=False)` |
| **缓存命中** | `get_computed_blocks()` | `lookup()` |
| **引用计数** | BlockPool 的 `ref_cnt` | BlockStatus 的 `ref_cnt` |
| **LRU 管理** | BlockPool 的 `free_block_queue` | `LRUCachePolicy` 的 `OrderedDict` |

两者操作模式相似但作用域完全独立。CPUOffloadingManager 没有复用 KVCacheManager 的 BlockPool，因为 CPU 侧不需要 prefix cache 哈希表、多 group 协调、sliding window 裁剪等复杂功能。

**对比: SimpleCPUOffloadConnector 的选择**

`SimpleCPUOffloadConnector` 做了不同选择——它**复用了 BlockPool**：

```python
# simple_kv_offload/manager.py
class SimpleCPUOffloadScheduler:
    def __init__(self, ...):
        # 为 CPU 创建一套完整的 KVCacheCoordinator + BlockPool
        self.cpu_coordinator = get_kv_cache_coordinator(self.cpu_kv_cache_config, ...)
        self.cpu_block_pool = self.cpu_coordinator.block_pool
        # 同时持有 GPU BlockPool 引用
        self._gpu_block_pool = None  # 后续通过 bind_gpu_block_pool 注入
```

这个方案更重，但 CPU 侧也能用 prefix cache 哈希表做命中检测。代价是需要同时操作 GPU 和 CPU 两个 BlockPool 的 ref_cnt。

### 2.5 OffloadingConnectorScheduler 的 4 个调用点

OffloadingConnectorScheduler 在一个 engine step 中被调用 **4 次**，每次的输入/输出不同：

#### 调用 1: `get_num_new_matched_tokens` — 查 CPU 缓存命中

```
调用位置: scheduler.py:605
调用时机: 调度 WAITING 请求时

输入:
  ├─ request: Request                    ← waiting 队列中取出的请求
  └─ num_computed_tokens: int            ← KVCacheManager.get_computed_blocks() 的返回值
                                          (GPU prefix cache 命中的 token 数)

内部调用:
  └─ manager.lookup(key) 逐个检查 CPU 缓存

输出:
  └─ (num_external_tokens, load_kv_async): tuple[int | None, bool]
       ├─ num_external_tokens → 返回给 Scheduler → 赋值给 num_external_computed_tokens
       └─ load_kv_async → 如果 True，请求状态设为 WAITING_FOR_REMOTE_KVS
```

#### 调用 2: `update_state_after_alloc` — 创建 Load TransferJob

```
调用位置: scheduler.py:753
调用时机: KVCacheManager 为请求分配完 GPU 块之后

输入:
  ├─ request: Request
  ├─ blocks: KVCacheBlocks               ← KVCacheManager.allocate_slots() 的返回值
  └─ num_external_computed_tokens: int   ← 调用 1 返回的 CPU 命中 token 数

内部调用:
  └─ manager.prepare_load(keys) → 锁定 CPU 源块 (ref_cnt++)

输出:
  └─ 无返回值 (副作用: 创建 load TransferJob，存入 _current_batch_load_jobs)
```

#### 调用 3: `build_connector_meta` — 打包所有传输任务

```
调用位置: scheduler.py:933
调用时机: schedule() 末尾，构建 SchedulerOutput 时

输入:
  └─ scheduler_output: SchedulerOutput   ← Scheduler 本轮的调度结果
       ├─ num_scheduled_tokens: dict[req_id, int]
       ├─ scheduled_cached_reqs.new_block_ids
       ├─ preempted_req_ids: set[str]
       └─ finished_req_ids: set[str]

内部调用:
  ├─ _build_store_jobs(scheduler_output)
  │    ├─ yield_req_data() → 提取 GPU 块 ID
  │    ├─ manager.prepare_store() → 分配 CPU 目标块
  │    └─ 创建 store TransferJob(s)
  └─ 处理 preempted_req_ids → 标记 flush jobs

输出:
  └─ OffloadingConnectorMetadata
       ├─ load_jobs: dict[int, TransferJob]
       ├─ store_jobs: dict[int, TransferJob]
       └─ jobs_to_flush: set[int]
     → 赋值到 scheduler_output.kv_connector_metadata → 传给 Worker
```

#### 调用 4: `update_connector_output` — 处理完成报告

```
调用位置: scheduler.py:2164
调用时机: update_from_output() 中，处理 Worker 返回的结果

输入:
  └─ kv_connector_output: KVConnectorOutput   ← Worker 进程返回
       └─ kv_connector_worker_meta: OffloadingWorkerMetadata
            └─ completed_jobs: dict[int, int]  (job_id → 完成该 job 的 Worker 数量)

内部处理:
  ├─ job_status.pending_count -= count
  ├─ 当 pending_count == 0:
  │    ├─ store job → manager.complete_store(keys) → block.ref_cnt: -1 → 0
  │    └─ load job → manager.complete_load(keys) → block.ref_cnt--
  └─ 清理 _jobs, _block_id_to_pending_jobs

输出:
  └─ 无返回值 (副作用: CPU 块状态更新)
```

#### 完整数据流

```
Scheduler.schedule()
  │
  │  ┌─ 调用 1 ──────────────────────────────────────────────────────┐
  │  │  get_num_new_matched_tokens(request, gpu_cache_hits)          │
  │  │    输入: Request + GPU prefix cache 命中数                     │
  │  │    来源: waiting 队列 + KVCacheManager.get_computed_blocks()   │
  │  │    输出: (cpu_cache_hits, async_flag)                         │
  │  │    去向: Scheduler 用于计算 total_computed_tokens              │
  │  └───────────────────────────────────────────────────────────────┘
  │
  │  ┌─ 调用 2 ──────────────────────────────────────────────────────┐
  │  │  update_state_after_alloc(request, gpu_blocks, cpu_hits)      │
  │  │    输入: Request + GPU 块分配结果 + CPU 命中数                  │
  │  │    来源: KVCacheManager.allocate_slots() + 调用 1 的输出       │
  │  │    输出: 无 (副作用: 创建 load TransferJob)                    │
  │  └───────────────────────────────────────────────────────────────┘
  │
  │  ┌─ 调用 3 ──────────────────────────────────────────────────────┐
  │  │  build_connector_meta(scheduler_output)                       │
  │  │    输入: SchedulerOutput (本轮调度结果)                        │
  │  │    输出: OffloadingConnectorMetadata                          │
  │  │    去向: scheduler_output.kv_connector_metadata → Worker      │
  │  └───────────────────────────────────────────────────────────────┘
  │
  │  ... Worker 执行传输 ...
  │
  │  ┌─ 调用 4 ──────────────────────────────────────────────────────┐
  │  │  update_connector_output(kv_connector_output)                 │
  │  │    输入: KVConnectorOutput (Worker 返回的完成报告)             │
  │  │    输出: 无 (副作用: 更新 CPU 块状态)                          │
  │  └───────────────────────────────────────────────────────────────┘
```

### 2.6 NPU 侧对应实现

#### NPUOffloadingSpec

```python
# vllm-ascend/kv_offload/npu.py
class NPUOffloadingSpec(OffloadingSpec):
    def get_manager(self) -> OffloadingManager:
        # 复用 upstream 的 CPUOffloadingManager
        return CPUOffloadingManager(block_size=offloaded_block_size, ...)

    def get_handlers(self, kv_caches, attn_backends):
        # 创建 NPU 专用 Handler (1 个，双向)
        self._handler = CpuNpuOffloadingHandler(
            gpu_block_size=gpu_block_size,
            cpu_block_size=gpu_block_size * block_size_factor,
            num_cpu_blocks=num_cpu_blocks,
            gpu_caches=kv_caches,
            attn_backends=attn_backends,
        )
        yield GPULoadStoreSpec, CPULoadStoreSpec, self._handler
        yield CPULoadStoreSpec, GPULoadStoreSpec, self._handler
```

与 GPU 版 `CPUOffloadingSpec` 的差异：
- GPU 版创建 2 个 Handler (每方向一个 `SingleDirectionOffloadingHandler`)
- NPU 版创建 1 个 Handler (`CpuNpuOffloadingHandler`，内部有 `d2h_stream` 和 `h2d_stream`)
- NPU 版复用 upstream 的 `CPUOffloadingManager`（CPU 块管理逻辑与平台无关）

#### CpuNpuOffloadingHandler vs SingleDirectionOffloadingHandler

| 维度 | GPU: SingleDirectionOffloadingHandler | NPU: CpuNpuOffloadingHandler |
|------|--------------------------------------|------------------------------|
| **实例数** | 2 个 (gpu_to_cpu + cpu_to_gpu) | 1 个 (双向) |
| **Stream** | `torch.cuda.Stream` (每个 handler 独立) | `torch.npu.Stream` (d2h + h2d 各一个) |
| **Event** | `torch.Event` | `torch.npu.Event` |
| **底层 API** | `ops.swap_blocks_batch()` → `cuMemcpyBatchAsync` | `torch.ops._C_ascend.swap_blocks_batch()` → `aclrtMemcpyBatchAsync` |
| **方向参数** | 隐式 (`cudaMemcpyDefault` 自动推断) | 显式 `direction` (0=H2D, 1=D2H) |
| **指针计算** | `compute_sub_block_ptrs()` (处理非连续 row_stride) | `expand_block_ids()` + 向量化广播 |
| **张量布局** | 统一 int8 view `(num_blocks, page_bytes)` | 原始 dtype `(blocks, size, heads, dim)` × 2 (key, value) |

#### CPUOffloadingConnector (Ascend 专用)

vllm-ascend 还实现了一个独立的 `CPUOffloadingConnector`，与 upstream 的 `OffloadingConnector` 是**两条并行路径**：

| 维度 | OffloadingConnector (upstream) | CPUOffloadingConnector (Ascend) |
|------|-------------------------------|--------------------------------|
| **prefix cache** | 通过 `CPUOffloadingManager` 的 LRU/ARC | 通过 `CPUKVCacheManager` + `BlockPool` |
| **CPU 内存管理** | 每个 Worker 独立分配 pinned memory | ZMQ RPC + SharedMemory 跨 DP rank 共享 |
| **数据传输** | `swap_blocks_batch()` 批量 DMA | `tensor.copy_()` 逐层拷贝 |
| **适用场景** | 通用，GPU/NPU 均可 | Ascend NPU 专用 |

#### NPU Scheduler 变体

vllm-ascend 提供了 4 种 Scheduler 变体，都继承 upstream `Scheduler`：

| 变体 | 激活条件 | Preemption 差异 |
|------|---------|----------------|
| **RecomputeScheduler** | `recompute_scheduler_enable` + PD 解聚合 | KV Consumer: 丢弃请求 + 通知客户端重发 |
| **SchedulerDynamicBatch** | `SLO_limits_for_dynamic_batch` | 内联 preemption，不走 `_preempt_request` |
| **SchedulerProfilingChunk** | NPU profiling 配置 | 使用 upstream `_preempt_request()` |
| **BalanceScheduler** | `enable_balance_scheduling` | 跨 DP rank 负载均衡，阻止满载 rank 接收新请求 |

---

## 3. 核心类派生关系

### 3.1 KVConnector 体系

```
KVConnectorBase_V1                              # vllm/.../kv_connector/v1/base.py
│   定义调度器侧和 Worker 侧的抽象接口
│   调度器侧: get_num_new_matched_tokens(), update_state_after_alloc(),
│             build_connector_meta()
│   Worker 侧: start_load_kv(), wait_for_layer_load(),
│              save_kv_layer(), get_finished()
│
├── OffloadingConnector (+ SupportsHMA)         # vllm/.../offloading_connector.py
│   │   通用卸载连接器入口
│   │   __init__() 根据 role 创建 Scheduler 或 Worker 实例
│   │   通过 OffloadingSpecFactory.create_spec() 创建平台专用 Spec
│   │
│   ├── OffloadingConnectorScheduler            # vllm/.../offloading/scheduler.py
│   │   │   调度器侧卸载逻辑
│   │   │   持有 OffloadingManager (CPUOffloadingManager)
│   │   │   管理 TransferJob 生命周期
│   │   │   处理抢占时的 flush
│   │   │
│   │   └── 内部状态:
│   │       _req_status: dict[ReqId, RequestOffloadState]
│   │       _jobs: dict[int, TransferJobStatus]
│   │       _current_batch_load_jobs: dict[int, TransferJob]
│   │       _block_id_to_pending_jobs: dict[int, set[int]]
│   │
│   └── OffloadingConnectorWorker               # vllm/.../offloading/worker.py
│       │   Worker 侧卸载逻辑
│       │   持有 OffloadingWorker
│       │   管理 store 延迟提交
│       │
│       └── 内部状态:
│           _load_jobs: dict[int, ReqId]
│           _unsubmitted_store_jobs: list[tuple[int, TransferSpec]]
│           _connector_worker_meta: OffloadingWorkerMetadata
│
└── CPUOffloadingConnector                      # vllm-ascend/.../cpu_offload_connector.py
    │   Ascend 专用，prefix-caching 感知
    │   使用 ZMQ RPC + SharedMemory
    │
    ├── CPUOffloadingConnectorScheduler
    │   └── ZMQ RPC → MetadataServer → CPUKVCacheManager
    │
    └── CPUOffloadingConnectorWorker
        ├── load_stream: torch.npu.Stream
        ├── save_stream: torch.npu.Stream
        └── tensor.copy_() 逐层拷贝
```

### 3.2 OffloadingSpec / Manager / Handler 体系

```
OffloadingSpec (ABC)                            # vllm/v1/kv_offload/base.py
│   抽象规格，由 OffloadingSpecFactory 创建
│   get_manager() → OffloadingManager
│   get_handlers() → Iterator[(src_type, dst_type, Handler)]
│
├── (GPU 默认 Spec，由 Factory 创建)
│   get_manager() → CPUOffloadingManager
│   get_handlers() → CpuGpuOffloadingHandlers
│
└── NPUOffloadingSpec                           # vllm-ascend/kv_offload/npu.py
    get_manager() → CPUOffloadingManager (复用 upstream)
    get_handlers() → CpuNpuOffloadingHandler

OffloadingManager (ABC)                         # vllm/v1/kv_offload/base.py
│   调度器侧元数据管理（不接触张量）
│   lookup(), prepare_store(), prepare_load(),
│   complete_store(), complete_load(), touch()
│
└── CPUOffloadingManager                        # vllm/v1/kv_offload/cpu/manager.py
    │   可插拔缓存策略 (LRU/ARC)
    │   管理 CPU 块池分配/回收
    │
    └── _policy: CachePolicy
        ├── LRUCachePolicy                      # vllm/v1/kv_offload/cpu/policies/lru.py
        │   OrderedDict 实现，O(1) 驱逐
        │
        └── ARCCachePolicy                      # vllm/v1/kv_offload/cpu/policies/arc.py
            自适应替换缓存

OffloadingHandler (ABC)                         # vllm/v1/kv_offload/worker/worker.py
│   Worker 侧数据传输（持有实际张量）
│   transfer_async(), get_finished(), wait()
│
├── SingleDirectionOffloadingHandler             # vllm/v1/kv_offload/cpu/gpu_worker.py
│   │   GPU 专用，单方向 (GPU→CPU 或 CPU→GPU)
│   │   使用 CUDA Stream + Event
│   │   调用 ops.swap_blocks_batch()
│   │
│   └── 内部状态:
│       src_tensors / dst_tensors: list[torch.Tensor]
│       _transfers: deque[Transfer]
│       _stream_pool: list[torch.cuda.Stream]
│       _event_pool: list[torch.Event]
│
├── CpuGpuOffloadingHandlers                    # vllm/v1/kv_offload/cpu/gpu_worker.py
│   │   组合两个 SingleDirectionOffloadingHandler
│   │   gpu_to_cpu_handler + cpu_to_gpu_handler
│   │
│   └── 分配 CPU pinned tensors (或 mmap)
│
└── CpuNpuOffloadingHandler                     # vllm-ascend/kv_offload/cpu_npu.py
    │   NPU 专用，双向 (NPU↔CPU)
    │   使用 NPU Stream + Event
    │   调用 torch.ops._C_ascend.swap_blocks_batch()
    │
    └── 内部状态:
        d2h_stream / h2d_stream: torch.npu.Stream
        _d2h_transfers / _h2d_transfers: deque[Transfer]
        _event_pool: list[torch.npu.Event]
        _npu_base_ptrs / _cpu_base_ptrs: np.ndarray
```

### 3.3 数据传输规格体系

```
LoadStoreSpec (ABC)                             # vllm/v1/kv_offload/base.py
│   抽象传输规格
│
└── BlockIDsLoadStoreSpec (ABC)                 # 基于块 ID 的规格
    │   block_ids: np.ndarray (int64)
    │
    ├── GPULoadStoreSpec                        # GPU 端规格
    │   block_ids: list[int]       # GPU 块 ID
    │   group_sizes: Sequence[int] # 每 KV 组的块数 (HMA 多组)
    │   block_indices: Sequence[int] # 每组起始块索引
    │
    └── CPULoadStoreSpec                        # CPU 端规格
        block_ids: list[int]       # CPU 块 ID

TransferSpec = tuple[LoadStoreSpec, LoadStoreSpec]  # (源端规格, 目标端规格)

TransferJob                                     # vllm/.../offloading/common.py
    req_id: str                    # 请求 ID
    transfer_spec: TransferSpec    # 传输规格对
```

---

## 4. 关键类实例化与生命周期

vLLM 的 Offloading 系统运行在**两个独立进程**中：Scheduler 进程（EngineCore）和 Worker 进程（GPUWorker，每个 TP rank 一个）。同一个类在两个进程中各有一个实例，但内部行为不同。

### 4.1 初始化总链

```
引擎启动
  │
  ├── Scheduler 进程 (EngineCore)
  │     │
  │     └─ EngineCore.__init__()                        [engine/core.py:97]
  │           │
  │           ├─ Scheduler.__init__()                    [core/sched/scheduler.py:65]
  │           │     │
  │           │     ├─ KVConnectorFactory.create_connector(role=SCHEDULER)  [scheduler.py:125]
  │           │     │     │
  │           │     │     └─ OffloadingConnector.__init__(role=SCHEDULER)   [offloading_connector.py:51]
  │           │     │           │
  │           │     │           ├─ OffloadingSpecFactory.create_spec()      [factory.py:52]
  │           │     │           │     └─ CPUOffloadingSpec.__init__()       [cpu/spec.py:22]
  │           │     │           │         读取 cpu_bytes_to_use, block_size_factor
  │           │     │           │         _manager = None (延迟创建)
  │           │     │           │         _handlers = None (不会在此进程创建)
  │           │     │           │
  │           │     │           └─ OffloadingConnectorScheduler(spec)       [offloading_connector.py:64]
  │           │     │                 │
  │           │     │                 ├─ SchedulerOffloadConfig.from_spec(spec)
  │           │     │                 └─ spec.get_manager()
  │           │     │                       └─ CPUOffloadingManager(num_blocks, cache_policy, ...)
  │           │     │                            创建 LRUCachePolicy 或 ARCCachePolicy
  │           │     │
  │           │     └─ KVCacheManager.__init__()            [scheduler.py:230]
  │           │           │
  │           │           └─ get_kv_cache_coordinator()     [kv_cache_manager.py:141]
  │           │                 │
  │           │                 └─ UnitaryKVCacheCoordinator / HybridKVCacheCoordinator
  │           │                       │
  │           │                       ├─ BlockPool(num_gpu_blocks, enable_caching, ...)
  │           │                       │     创建 KVCacheBlock 对象数组
  │           │                       │     创建 FreeKVCacheBlockQueue
  │           │                       │     创建 BlockHashToBlockMap (prefix cache)
  │           │                       │
  │           │                       └─ SingleTypeKVCacheManager(s) (per KV group)
  │           │
  │           └─ connector.bind_gpu_block_pool(kv_cache_manager.block_pool)
  │
  └── Worker 进程 (GPUWorker, 每个 TP rank)
        │
        └─ GPUWorker.initialize_from_config()             [gpu_worker.py:546]
              │
              ├─ ensure_kv_transfer_initialized()          [gpu_worker.py:558]
              │     │
              │     └─ KVConnectorFactory.create_connector(role=WORKER)  [kv_transfer_state.py:83]
              │           │
              │           └─ OffloadingConnector.__init__(role=WORKER)    [offloading_connector.py:51]
              │                 │
              │                 ├─ OffloadingSpecFactory.create_spec()     [factory.py:52]
              │                 │     └─ CPUOffloadingSpec.__init__()      [cpu/spec.py:22]
              │                 │         (独立实例，与 Scheduler 进程中的不同)
              │                 │
              │                 └─ OffloadingConnectorWorker(spec)         [offloading_connector.py:66]
              │                       │
              │                       └─ OffloadingWorker()                [offloading/worker.py:45]
              │                            handlers = set()
              │                            transfer_type_to_handler = {}
              │
              └─ model_runner.initialize_kv_cache()         [gpu_worker.py:561]
                    │
                    └─ ActiveKVConnector.__init__()          [gpu/kv_connector.py:48]
                          │
                          └─ kv_connector.register_kv_caches(kv_caches_dict)  [gpu/kv_connector.py:56]
                                │
                                └─ OffloadingConnectorWorker.register_kv_caches()  [offloading/worker.py:57]
                                      │
                                      ├─ 构建 CanonicalKVCaches (张量规范化)
                                      │
                                      └─ _register_handlers(canonical_kv_caches)  [offloading/worker.py:183]
                                            │
                                            └─ spec.get_handlers(kv_caches)  [cpu/spec.py:91]
                                                  │
                                                  └─ CpuGpuOffloadingHandlers(kv_caches, ...)
                                                        │
                                                        ├─ 分配 CPU pinned tensors
                                                        │
                                                        ├─ SingleDirectionOffloadingHandler(gpu_to_cpu=True)
                                                        │     持有 gpu_tensors, cpu_tensors
                                                        │     创建 CUDA stream pool, event pool
                                                        │
                                                        └─ SingleDirectionOffloadingHandler(gpu_to_cpu=False)
                                                              共享同一组 tensors
                                                              独立的 stream pool, event pool
                                            │
                                            └─ worker.register_handler():
                                                  ("GPU","CPU") → gpu_to_cpu_handler
                                                  ("CPU","GPU") → cpu_to_gpu_handler
```

### 4.2 各关键类详解

#### OffloadingConnector

| 属性 | 说明 |
|------|------|
| **实例化位置** | `KVConnectorFactory.create_connector()` (`kv_connector/factory.py:75`) |
| **实例化次数** | 2 次：Scheduler 进程 1 次 (role=SCHEDULER)，Worker 进程 1 次 (role=WORKER) |
| **调用者** | Scheduler 进程: `Scheduler.__init__()` (`scheduler.py:125`)；Worker 进程: `ensure_kv_transfer_initialized()` (`kv_transfer_state.py:83`) |
| **内部行为** | 根据 role 创建 `OffloadingConnectorScheduler` 或 `OffloadingConnectorWorker`，两者互斥 |
| **引用持有者** | Scheduler 进程: `Scheduler.connector`；Worker 进程: 全局 `_KV_CONNECTOR_AGENT` (`kv_transfer_state.py`)，通过 `get_kv_transfer_group()` 获取 |
| **生命周期** | 与进程同生命周期，进程启动时创建，进程退出时销毁 |

#### OffloadingConnectorScheduler

| 属性 | 说明 |
|------|------|
| **实例化位置** | `OffloadingConnector.__init__()` (`offloading_connector.py:64`)，仅当 `role==SCHEDULER` |
| **运行进程** | Scheduler/EngineCore 进程 |
| **构造时创建** | `SchedulerOffloadConfig.from_spec(spec)` + `spec.get_manager()` → `CPUOffloadingManager` |
| **引用持有者** | `OffloadingConnector.connector_scheduler` |
| **被谁调用** | `Scheduler.schedule()` → `connector.build_connector_meta()` → `connector_scheduler.build_connector_meta()` |
| **核心状态** | `_req_status`: 每个请求的卸载状态；`_jobs`: 所有 in-flight TransferJob；`_block_id_to_pending_jobs`: 块→job 映射 |

#### OffloadingConnectorWorker

| 属性 | 说明 |
|------|------|
| **实例化位置** | `OffloadingConnector.__init__()` (`offloading_connector.py:66`)，仅当 `role==WORKER` |
| **运行进程** | Worker/GPU 进程 (每个 TP rank) |
| **构造时创建** | `OffloadingWorker()` (handler 路由器) |
| **引用持有者** | `OffloadingConnector.connector_worker` |
| **延迟初始化** | Handler 在 `register_kv_caches()` 时才创建（需要 GPU 张量） |
| **被谁调用** | `ActiveKVConnector` 在每次 forward 时调用 `pre_forward()` / `post_forward()` |
| **核心状态** | `_load_jobs`: load job→req_id 映射；`_unsubmitted_store_jobs`: 延迟提交的 store 队列 |

#### CPUOffloadingSpec (OffloadingSpec)

| 属性 | 说明 |
|------|------|
| **实例化位置** | `OffloadingSpecFactory.create_spec()` (`kv_offload/factory.py:52`) |
| **实例化次数** | 2 次：Scheduler 进程和 Worker 进程各一个独立实例 |
| **工厂机制** | 注册表查找: `spec_name` → `CPUOffloadingSpec` (默认) 或 `TieringOffloadingSpec`；也可通过 `spec_module_path` 动态加载 |
| **延迟创建** | `_manager = None` (Scheduler 进程首次 `get_manager()` 时创建)；`_handlers = None` (Worker 进程首次 `get_handlers()` 时创建) |
| **两个工厂方法** | `get_manager()` → `CPUOffloadingManager` (仅 Scheduler 进程调用)；`get_handlers(kv_caches)` → `CpuGpuOffloadingHandlers` (仅 Worker 进程调用) |
| **引用持有者** | `OffloadingConnectorScheduler` 和 `OffloadingConnectorWorker` 各持有一份 spec 引用 |

#### CPUOffloadingManager

| 属性 | 说明 |
|------|------|
| **实例化位置** | `CPUOffloadingSpec.get_manager()` (`cpu/spec.py:75`) |
| **调用者** | `OffloadingConnectorScheduler.__init__()` (`offloading/scheduler.py:264`) |
| **运行进程** | Scheduler/EngineCore 进程 (仅在此进程创建) |
| **构造参数** | `num_blocks` (从 `cpu_bytes_to_use` 计算)、`cache_policy` ("lru"/"arc")、`enable_events`、`store_threshold`、`max_tracker_size` |
| **构造时创建** | `LRUCachePolicy` 或 `ARCCachePolicy`；空闲块列表 `_free_list`；引用计数 `counts` (OrderedDict) |
| **引用持有者** | `OffloadingConnectorScheduler.manager` |
| **被谁调用** | `_build_store_jobs()` → `prepare_store()`；`get_num_new_matched_tokens()` → `lookup()`；`update_state_after_alloc()` → `prepare_load()`；`update_connector_output()` → `complete_store/load()` |

#### OffloadingWorker

| 属性 | 说明 |
|------|------|
| **实例化位置** | `OffloadingConnectorWorker.__init__()` (`offloading/worker.py:45`) |
| **运行进程** | Worker/GPU 进程 |
| **构造时创建** | `handlers: set` (空)；`transfer_type_to_handler: dict` (空) |
| **引用持有者** | `OffloadingConnectorWorker.worker` |
| **Handler 注册** | `_register_handlers()` 调用 `spec.get_handlers()` 获取 handler，然后 `register_handler(src_cls, dst_cls, handler)` 注册到路由表 |
| **路由机制** | `transfer_async(job_id, spec)` → 从 spec 提取 `(src.medium(), dst.medium())` → 查 `transfer_type_to_handler` → 委托给对应 handler |
| **聚合完成** | `get_finished()` 遍历所有 handler 收集结果；`wait(job_ids)` 遍历所有 handler 等待 |

#### CpuGpuOffloadingHandlers

| 属性 | 说明 |
|------|------|
| **实例化位置** | `CPUOffloadingSpec.create_handlers()` (`cpu/spec.py:84-89`)，由 `get_handlers()` 调用 (`cpu/spec.py:99`) |
| **调用者** | `OffloadingConnectorWorker._register_handlers()` → `spec.get_handlers(kv_caches)` |
| **运行进程** | Worker/GPU 进程 |
| **构造时创建** | 1. 为每个 GPU KV cache tensor 分配对应的 CPU pinned tensor (或 mmap view)；2. 创建两个 `SingleDirectionOffloadingHandler` |
| **产出** | `gpu_to_cpu_handler` (GPU→CPU) + `cpu_to_gpu_handler` (CPU→GPU) |
| **引用持有者** | `CPUOffloadingSpec._handlers` (缓存，后续 `get_handlers()` 复用) |

#### SingleDirectionOffloadingHandler

| 属性 | 说明 |
|------|------|
| **实例化位置** | `CpuGpuOffloadingHandlers.__init__()` (`cpu/gpu_worker.py:437` 和 `446`) |
| **实例化次数** | 2 个: 一个 `gpu_to_cpu=True`，一个 `gpu_to_cpu=False` |
| **运行进程** | Worker/GPU 进程 |
| **构造参数** | `gpu_tensors`, `cpu_tensors`, `block_size_factor`, `kv_cache_groups_data_refs`, `gpu_to_cpu` |
| **构造时创建** | `_transfers: deque` (in-flight 传输队列)；`_stream_pool: list` (CUDA Stream 复用池)；`_event_pool: list` (CUDA Event 复用池) |
| **流转路径** | `CpuGpuOffloadingHandlers` → yield 给 `_register_handlers()` → `OffloadingWorker.register_handler()` → 存入 `transfer_type_to_handler` 路由表 |
| **被谁调用** | `OffloadingWorker.transfer_async()` 路由到 → `handler.transfer_async(job_id, spec)` |
| **被谁轮询** | `OffloadingWorker.get_finished()` → `handler.get_finished()` |

#### CpuNpuOffloadingHandler (NPU)

| 属性 | 说明 |
|------|------|
| **实例化位置** | `NPUOffloadingSpec.get_handlers()` (`vllm-ascend/kv_offload/npu.py:53`) |
| **实例化次数** | 1 个 (同时处理两个方向，内部有 `d2h_stream` 和 `h2d_stream`) |
| **运行进程** | Worker/NPU 进程 |
| **与 GPU 版的差异** | GPU 版创建 2 个 handler (每方向一个)；NPU 版创建 1 个 handler (双向) |
| **流转路径** | 同 GPU 版: yield → `_register_handlers()` → `OffloadingWorker.register_handler()` |

#### Scheduler

| 属性 | 说明 |
|------|------|
| **实例化位置** | `EngineCore.__init__()` (`engine/core.py:148`) |
| **运行进程** | Scheduler/EngineCore 进程 |
| **构造时创建** | `KVConnectorFactory.create_connector(role=SCHEDULER)` → `OffloadingConnector`；`KVCacheManager` → `KVCacheCoordinator` → `BlockPool` |
| **引用持有者** | `EngineCore.scheduler` |
| **核心调用** | 每个 engine step: `schedule()` → `SchedulerOutput` → 传给 Worker |

#### KVCacheManager / KVCacheCoordinator / BlockPool

| 类 | 实例化位置 | 调用者 | 运行进程 |
|----|-----------|--------|---------|
| `KVCacheManager` | `Scheduler.__init__()` (`scheduler.py:230`) | EngineCore | Scheduler |
| `KVCacheCoordinator` (子类) | `get_kv_cache_coordinator()` (`kv_cache_coordinator.py:635/647`) | `KVCacheManager.__init__()` | Scheduler |
| `BlockPool` | `KVCacheCoordinator.__init__()` (`kv_cache_coordinator.py:50`) | KVCacheCoordinator | Scheduler |
| `SingleTypeKVCacheManager` | `KVCacheCoordinator.__init__()` (`kv_cache_coordinator.py:66-78`) | KVCacheCoordinator | Scheduler |

引用链: `Scheduler.kv_cache_manager` → `KVCacheManager.coordinator` → `KVCacheCoordinator.block_pool` → `BlockPool`

`BlockPool` 还被传递给: `Scheduler.connector.bind_gpu_block_pool(kv_cache_manager.block_pool)` (`scheduler.py:246`)

### 4.3 实例流转总图

```
Scheduler 进程                                    Worker 进程 (每个 TP rank)
━━━━━━━━━━━━━━                                    ━━━━━━━━━━━━━━━━━━━━━━━━

EngineCore                                        GPUWorker
  │                                                  │
  ├─ Scheduler                                       ├─ 全局 _KV_CONNECTOR_AGENT
  │    │                                             │    │
  │    ├─ .connector ────────────────────────────────┼──→ OffloadingConnector (WORKER)
  │    │    OffloadingConnector (SCHEDULER)           │         │
  │    │    │                                        │         ├─ .spec ──→ CPUOffloadingSpec
  │    │    ├─ .spec ──→ CPUOffloadingSpec            │         │    (独立实例)
  │    │    │    (独立实例)                            │         │
  │    │    └─ .connector_scheduler                   │         └─ .connector_worker
  │    │         │                                    │              │
  │    │         ├─ .manager ──→ CPUOffloadingManager │              ├─ .spec (同上)
  │    │         │    │                               │              │
  │    │         │    └─ ._policy ──→ LRUCachePolicy  │              └─ .worker ──→ OffloadingWorker
  │    │         │                                    │                   │
  │    │         └─ ._req_status                      │                   ├─ .handlers
  │    │              ._jobs                          │                   │    ├─ gpu_to_cpu_handler
  │    │              ._block_id_to_pending_jobs      │                   │    └─ cpu_to_gpu_handler
  │    │                                             │                   │
  │    └─ .kv_cache_manager                          │                   └─ .transfer_type_to_handler
  │         │                                        │                        ("GPU","CPU") → handler_1
  │         └─ .coordinator                          │                        ("CPU","GPU") → handler_2
  │              │                                   │
  │              ├─ .block_pool ──→ BlockPool         │
  │              │    │                               │
  │              │    └─ .free_block_queue            │
  │              │         .cached_block_hash_to_block│
  │              │                                    │
  │              └─ .single_type_managers[]           │
  │                   └─ FullAttentionManager 等      │
```

### 4.4 两个进程中的 Spec 实例对比

`CPUOffloadingSpec` 在两个进程中各有一个**独立实例**，它们不共享状态：

| 维度 | Scheduler 进程中的 Spec | Worker 进程中的 Spec |
|------|------------------------|---------------------|
| **创建时机** | `Scheduler.__init__()` 时 | `ensure_kv_transfer_initialized()` 时 |
| **调用的工厂方法** | `get_manager()` → 创建 `CPUOffloadingManager` | `get_handlers()` → 创建 `CpuGpuOffloadingHandlers` |
| **不调用的工厂方法** | `get_handlers()` (不需要，没有 GPU 张量) | `get_manager()` (不需要，Manager 是调度器侧) |
| **`_manager` 字段** | 首次 `get_manager()` 后非 None | 始终为 None |
| **`_handlers` 字段** | 始终为 None | 首次 `get_handlers()` 后非 None |
| **共享的配置** | `block_size_factor`, `gpu_block_size`, `hash_block_size`, `offload_prompt_only` (从相同的 `VllmConfig` 读取，值相同) |

---

## 5. GPU 路径 (vllm) 完整流程

### 5.1 Store 流程 (GPU → CPU)

```
步骤 1: 调度器决策
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scheduler.schedule()
  │
  ├─> 请求完成一轮推理后，识别可卸载的 KV 块
  │
  └─> OffloadingConnectorScheduler._build_store_jobs(scheduler_output)
        │
        ├─> yield_req_data(scheduler_output) 提取本批次已计算的 GPU 块 ID
        │
        ├─> 计算 num_offloadable_tokens (受 offload_prompt_only 限制)
        │
        ├─> 筛选 new_offload_keys (基于 hash 的逻辑块标识)
        │     过滤: 已 store 的块、SWA 不可达块、block_id==0 的跳过块
        │
        ├─> CPUOffloadingManager.prepare_store(keys, req_context)
        │     ├─> 过滤: 访问次数 < store_threshold 的块
        │     ├─> 过滤: 已在 CPU 缓存中的块
        │     ├─> 检查 CPU 块池空闲: _get_num_free_blocks()
        │     ├─> 如空间不足:
        │     │     _policy.evict(n, protected) → LRU 驱逐
        │     │     驱逐条件: ref_cnt==0 且不在 protected 集合中
        │     │     驱逐后: CPU 块 ID 回收进 _free_list，数据丢弃
        │     ├─> _allocate_blocks(keys_to_store) → 分配 CPU 块 ID
        │     ├─> _policy.insert(key, block) → 注册到缓存策略
        │     └─> 返回 PrepareStoreOutput:
        │           keys_to_store: 实际需要卸载的块标识
        │           store_spec: CPULoadStoreSpec(cpu_block_ids)
        │           evicted_keys: 被驱逐的旧块标识
        │
        ├─> 组装 GPU 源端:
        │     src_spec = GPULoadStoreSpec(src_block_ids, group_sizes, block_indices)
        │
        └─> 创建 TransferJob:
              store_jobs[job_id] = TransferJob(req_id, (src_spec, dst_spec))

步骤 2: 元数据打包
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OffloadingConnectorScheduler.build_connector_meta(scheduler_output)
  │
  ├─> 处理抢占: preempted_req_ids → 标记 flush
  │
  └─> 打包为 OffloadingConnectorMetadata:
        load_jobs: dict[int, TransferJob]      # CPU→GPU 加载任务
        store_jobs: dict[int, TransferJob]     # GPU→CPU 卸载任务
        jobs_to_flush: set[int]                # 需要等待完成的旧 job
        ↓
        赋值到 scheduler_output.kv_connector_metadata

步骤 3: Worker 接收元数据
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OffloadingConnectorWorker 收到 metadata

  ├─> handle_preemptions(metadata):
  │     提交上轮积压的 store jobs
  │     如有 jobs_to_flush → worker.wait() 阻塞等待
  │
  ├─> prepare_store_kv(metadata):
  │     将本轮 store jobs 排入 _unsubmitted_store_jobs
  │     (延迟到下一步执行，避免影响 token 生成延迟)
  │
  └─> start_kv_transfers(metadata):
        提交上轮积压 store → worker.transfer_async()
        提交本轮 load jobs → worker.transfer_async()

步骤 4: 执行传输 (核心)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SingleDirectionOffloadingHandler.transfer_async(job_id, transfer_spec)
  │
  ├─> 1. 从 spec 提取 src_blocks / dst_blocks (numpy int64 数组)
  │
  ├─> 2. 按 KV cache group 分组处理:
  │     对每个 group:
  │       计算 skip_count (子块对齐偏移)
  │       对每个 data_ref (layer 的 key/value):
  │         compute_sub_block_ptrs() → 计算字节指针
  │
  ├─> 3. compute_sub_block_ptrs() 详解:
  │     输入: block_ids, block_size_factor, tensor
  │     计算: base_ptr + block_id * row_stride + sub_offset * sub_block_size
  │     输出: all_src[], all_dst[], all_sizes[] (int64 numpy 数组)
  │     当 factor==1 时走快速路径: output = base_ptr + block_ids * row_stride
  │
  ├─> 4. 转换为 torch.Tensor:
  │     batch_src = torch.from_numpy(all_src)    # int64 CPU tensor
  │     batch_dst = torch.from_numpy(all_dst)    # int64 CPU tensor
  │     batch_sizes = torch.from_numpy(all_sizes) # int64 CPU tensor
  │
  ├─> 5. 获取/创建 CUDA Stream 和 Event (从池中复用)
  │
  ├─> 6. GPU→CPU 方向: stream.wait_stream(torch.cuda.current_stream())
  │     确保模型计算完成后再读取 GPU 数据
  │
  ├─> 7. 如有前序传输: stream.wait_event(last_transfer.end_event)
  │     保证传输按提交顺序执行
  │
  ├─> 8. 在独立 CUDA Stream 中执行:
  │     with torch.cuda.stream(stream):
  │         start_event.record(stream)
  │         ops.swap_blocks_batch(
  │             batch_src, batch_dst, batch_sizes,
  │             is_src_access_order_any=(not gpu_to_cpu)
  │         )
  │         end_event.record(stream)
  │
  └─> 9. 记录 Transfer(job_id, stream, start_event, end_event, num_bytes)
        transfer_async() 返回 True (数据可能还没搬完!)

步骤 5: 完成检测
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SingleDirectionOffloadingHandler.get_finished()
  │
  └─> 轮询队首 transfer 的 end_event.query() (非阻塞!)
      ├─> 完成: 弹出 transfer, 计算 transfer_time
      │         回收 stream/event 到池中
      └─> 返回 list[TransferResult(job_id, success, size, time, type)]

步骤 6: 结果回传
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OffloadingConnectorWorker.get_finished(finished_req_ids)
  │
  ├─> 收集所有完成传输
  ├─> 记录到 OffloadingWorkerMetadata(completed_jobs={job_id: 1})
  └─> 返回 (finished_sending, finished_recving)

Scheduler 接收 KVConnectorOutput
  │
  └─> OffloadingConnectorScheduler.update_connector_output(output)
        ├─> 减少 job pending_count
        ├─> 当 pending_count == 0 (所有 Worker 都完成):
        │     store job → manager.complete_store(keys)
        │       └─> 标记块就绪 (ref_cnt: -1 → 0)
        │     load job → manager.complete_load(keys)
        │       └─> 减少 ref_cnt，块可被驱逐
        └─> 清理 _jobs, _block_id_to_pending_jobs
```

### 5.2 Load 流程 (CPU → GPU)

```
步骤 1: 查找已卸载的块
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
新请求到达 Scheduler

connector.get_num_new_matched_tokens(request, num_computed_tokens)
  │
  └─> OffloadingConnectorScheduler.get_num_new_matched_tokens()
        ├─> 创建 RequestOffloadState
        ├─> update_offload_keys(): 生成请求所有块的 OffloadKey
        ├─> _lookup(req_status):
        │     对每个块调用 manager.lookup(key, req_context)
        │     └─> _policy.get(key) → BlockStatus | None
        │     └─> 检查 block.is_ready (ref_cnt >= 0)
        │     └─> 返回 True/False/None (None=延迟重试)
        ├─> 返回 CPU 中可用的 token 数量
        └─> _touch(req_status) 更新 LRU 顺序

步骤 2: 分配 GPU 块
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KVCacheManager.allocate_slots(request, num_new_tokens, ...)
  └─> 为请求分配 GPU 块

步骤 3: 准备加载
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OffloadingConnectorScheduler.update_state_after_alloc(request, blocks, num_external_tokens)
  │
  ├─> manager.prepare_load(keys_to_load, req_context)
  │     ├─> _policy.get(key) → BlockStatus
  │     ├─> block.ref_cnt += 1 (防止被驱逐)
  │     └─> 返回 CPULoadStoreSpec(cpu_block_ids)
  │
  ├─> 创建 GPULoadStoreSpec(gpu_block_ids, group_sizes, block_indices)
  ├─> 创建 TransferJob(src=CPUSpec, dst=GPUSpec)
  └─> 加入 _current_batch_load_jobs

步骤 4-6: 与 Store 流程步骤 2-5 相同 (方向相反)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
关键差异:
  - is_src_access_order_any = True (CPU 源不会被 GPU 流并发写入)
  - 允许 DMA 预取优化

步骤 7: 请求恢复执行
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scheduler 收到 finished_recving
  │
  ├─> 请求状态: WAITING_FOR_REMOTE_KVS → WAITING
  ├─> manager.complete_load(keys) → ref_cnt--
  └─> 请求使用已加载的 KV Cache 继续推理
```

---

## 6. NPU 路径 (vllm-ascend) 完整流程

### 6.1 路径 A: OffloadingConnector + NPUOffloadingSpec

与 GPU 路径共享 Scheduler 侧逻辑（`OffloadingConnectorScheduler`），差异在 Worker 侧 Handler。

#### NPU Handler 传输详解

```python
# vllm-ascend/kv_offload/cpu_npu.py

CpuNpuOffloadingHandler.transfer_async(job_id, spec):
  │
  ├─> 1. 判断方向:
  │     src_spec 是 CPULoadStoreSpec → H2D (CPU→NPU), 使用 h2d_stream
  │     src_spec 是 GPULoadStoreSpec → D2H (NPU→CPU), 使用 d2h_stream
  │
  ├─> 2. expand_block_ids() — 展开块 ID
  │     CPU 块 = NPU 块 × block_size_factor
  │     例: block_ids=[0,1,3], factor=4 → [0,1,2,3, 4,5,6,7, 12,13,14,15]
  │     向量化 NumPy 实现
  │
  ├─> 3. 向量化构建指针数组 (无 Python 循环):
  │     num_pairs = src_sub_block_count
  │     num_sub_tensors = len(layers) × 2  (每层 key + value)
  │
  │     all_src = (base_ptrs[:, None] + block_ids[None, :] * block_size).ravel()
  │     all_dst = (base_ptrs[:, None] + block_ids[None, :] * block_size).ravel()
  │     all_sizes = broadcast(block_size, (num_sub_tensors, num_pairs)).ravel()
  │
  │     布局: [layer0_key_blk0, ..., layer0_val_blk0, ..., layer1_key_blk0, ...]
  │
  ├─> 4. torch.ops._C_ascend.swap_blocks_batch(src, dst, sizes, direction)
  │     direction: 0=H2D, 1=D2H
  │     CANN 8.5+: aclrtMemcpyBatchAsync (单次 API)
  │     回退: 循环 aclrtMemcpyAsync
  │
  └─> 5. 记录 Transfer, 通过 end_event.query() 轮询完成
```

### 6.2 路径 B: CPUOffloadingConnector (Ascend 专用)

```
Scheduler 侧:
  CPUOffloadingConnectorScheduler
    └─ ZMQ RPC → MetadataServer (独立进程)
        └─ CPUKVCacheManager
            ├─ BlockPool: CPU 块分配 + prefix cache
            └─ SharedMemory: 跨 DP rank 共享 CPU KV Cache

Worker 侧:
  CPUOffloadingConnectorWorker
    ├─ register_kv_caches(): 分配 CPU KV Cache (SharedMemory)
    ├─ start_load_kv(): 逐层 tensor.copy_() (CPU→NPU)
    │     for (cpu_id, gpu_id) in load_block_mapping:
    │         gpu_kv[layer][kv][gpu_id].copy_(cpu_kv[layer][kv][cpu_id])
    │     在 load_stream (torch.npu.Stream) 上
    ├─ wait_for_layer_load(): load_stream.synchronize()
    └─ _save_listener() (后台线程): NPU→CPU 逐层 copy
```

### 6.3 NPU 前向推理中的 KV Cache 写入/读取

```
写入 (Attention forward):
  BaseDeviceAdaptor.reshape_and_cache(key, value, key_cache, value_cache, slot_mapping)
    [910B] → torch_npu._npu_reshape_and_cache(...)
    [A5]   → torch_npu.npu_scatter_pa_kv_cache(..., cache_mode="Norm")

读取 (Attention compute):
  BaseDeviceAdaptor.kv_cache_load(cache_kv_c, cache_k_pe, block_table, ...)
    [910B] → torch_npu.atb.npu_paged_cache_load(...)
    [A5]   → torch_npu.npu_gather_pa_kv_cache(...)
```

---

## 7. 模块间消息传递

### 7.1 消息总览

```
生产者                          消息类型                              消费者
━━━━━━━━                        ━━━━━━━━                              ━━━━━━━━

Scheduler                       SchedulerOutput                       ModelRunner
  ├─ scheduled_new_reqs           ├─ num_scheduled_tokens              ├─ execute_model()
  ├─ scheduled_cached_reqs        ├─ preempted_req_ids                 └─ kv_connector.pre_forward()
  ├─ preempted_req_ids            └─ kv_connector_metadata
  └─ kv_connector_metadata              │
         │                              ▼
         │                    OffloadingConnectorMetadata
         │                      ├─ load_jobs: dict[int, TransferJob]
         │                      ├─ store_jobs: dict[int, TransferJob]
         │                      └─ jobs_to_flush: set[int]
         │                              │
         │                              ▼
OffloadingConnectorScheduler    OffloadingConnectorMetadata     OffloadingConnectorWorker
  ├─ _build_store_jobs()          (通过 scheduler_output 传递)     ├─ prepare_store_kv()
  └─ build_connector_meta()                                        ├─ start_kv_transfers()
                                                                   └─ handle_preemptions()

OffloadingConnectorWorker       OffloadingWorkerMetadata        OffloadingConnectorScheduler
  └─ get_finished()               └─ completed_jobs:               └─ update_connector_output()
                                      dict[int, int]                   └─ manager.complete_store/load()
                                      (job_id → count)

CPUOffloadingManager            PrepareStoreOutput              OffloadingConnectorScheduler
  └─ prepare_store()              ├─ keys_to_store                 └─ _build_store_jobs()
                                  ├─ store_spec: CPULoadStoreSpec
                                  └─ evicted_keys
```

### 7.2 关键消息格式详解

#### TransferJob — 传输任务

```python
@dataclass
class TransferJob:                              # common.py
    req_id: str                                 # 请求 ID
    transfer_spec: TransferSpec                 # = (src_spec, dst_spec)

# Store 示例:
TransferJob(
    req_id="req_42",
    transfer_spec=(
        GPULoadStoreSpec(                       # 源: GPU
            block_ids=[50, 51, 52, 53],         # GPU 块 ID
            group_sizes=[4],                    # 1 个 KV 组，4 个块
            block_indices=[12],                 # 起始索引
        ),
        CPULoadStoreSpec(                       # 目标: CPU
            block_ids=[3],                      # CPU 块 ID (factor=4, 1 个 CPU 块)
        ),
    )
)

# Load 示例:
TransferJob(
    req_id="req_99",
    transfer_spec=(
        CPULoadStoreSpec(block_ids=[3]),        # 源: CPU
        GPULoadStoreSpec(                       # 目标: GPU
            block_ids=[200, 201, 202, 203],
            group_sizes=[4],
            block_indices=[50],
        ),
    )
)
```

#### OffloadingConnectorMetadata — Scheduler→Worker

```python
@dataclass
class OffloadingConnectorMetadata:              # common.py
    load_jobs: dict[int, TransferJob]           # job_id → 加载任务
    store_jobs: dict[int, TransferJob]          # job_id → 卸载任务
    jobs_to_flush: set[int] | None = None       # 需要阻塞等待的旧 job

# 示例:
OffloadingConnectorMetadata(
    load_jobs={
        101: TransferJob("req_99", (CPUSpec[3], GPUSpec[200,201,202,203]))
    },
    store_jobs={
        102: TransferJob("req_42", (GPUSpec[50,51,52,53], CPUSpec[3]))
    },
    jobs_to_flush={99}                          # 抢占触发，必须等 job 99 完成
)
```

#### OffloadingWorkerMetadata — Worker→Scheduler

```python
@dataclass
class OffloadingWorkerMetadata:                 # common.py
    completed_jobs: dict[int, int]              # job_id → 完成计数

# 示例 (2 个 Worker 的 TP 场景):
# Worker 0 报告: OffloadingWorkerMetadata(completed_jobs={101: 1, 102: 1})
# Worker 1 报告: OffloadingWorkerMetadata(completed_jobs={101: 1, 102: 1})
# Scheduler 聚合: pending_count 从 2 减到 0 → 调用 complete_store/load
```

#### PrepareStoreOutput — Manager→Scheduler

```python
@dataclass
class PrepareStoreOutput:                      # base.py
    keys_to_store: list[OffloadKey]             # 需要卸载的块标识
    store_spec: LoadStoreSpec                    # CPU 目标块规格
    evicted_keys: list[OffloadKey]              # 被驱逐的旧块

# keys_to_store 决定 GPU 源端要拷贝哪些块
# store_spec 决定 CPU 目标端写到哪些块
# evicted_keys 仅用于事件通知，不参与传输
```

#### TransferResult — Handler→Worker

```python
@dataclass
class TransferResult:                           # worker/worker.py
    job_id: int                                 # 任务 ID
    success: bool                               # 是否成功
    transfer_size: int                          # 传输字节数
    transfer_time: float                        # 传输耗时 (秒)
    transfer_type: tuple[str, str]              # ("GPU","CPU") 或 ("CPU","GPU")
```

### 7.3 消息生命周期

```
TransferJob 的完整生命周期:
━━━━━━━━━━━━━━━━━━━━━━━━━━

创建: OffloadingConnectorScheduler
  ├─ _build_store_jobs() → store TransferJob
  └─ update_state_after_alloc() → load TransferJob

打包: build_connector_meta()
  └─ 放入 OffloadingConnectorMetadata

传递: scheduler_output.kv_connector_metadata
  └─ Scheduler → ModelRunner → KVConnectorWorker

消费: OffloadingConnectorWorker
  ├─ 提取 transfer_spec → worker.transfer_async()
  ├─ req_id → 记录到 _load_jobs (load 完成时回传)
  └─ TransferJob 本身不回传

完成报告: OffloadingWorkerMetadata
  └─ completed_jobs={job_id: 1} → 回传给 Scheduler

最终处理: update_connector_output()
  └─ pending_count 减到 0 → complete_store/load → 清理 _jobs
```

---

## 8. 时序图

### 8.1 Store (GPU→CPU) 完整时序

```
Scheduler              ModelRunner              Worker                  Handler              DMA Engine
   │                       │                       │                       │                      │
   │ schedule()            │                       │                       │                      │
   │ ├─ _build_store_jobs  │                       │                       │                      │
   │ │  prepare_store()    │                       │                       │                      │
   │ │  → CPUSpec[3]       │                       │                       │                      │
   │ │  → TransferJob 102  │                       │                       │                      │
   │ │                     │                       │                       │                      │
   │ ├─ build_connector_meta                       │                       │                      │
   │ │  → Metadata{store_jobs:{102:...}}           │                       │                      │
   │ │                     │                       │                       │                      │
   │──SchedulerOutput──────>                       │                       │                      │
   │  (kv_connector_metadata)                      │                       │                      │
   │                       │                       │                       │                      │
   │                       │ execute_model()       │                       │                      │
   │                       │ → Compute Stream      │                       │                      │
   │                       │                       │                       │                      │
   │                       │ post_forward()        │                       │                      │
   │                       │──kv_connector────────>│                       │                      │
   │                       │                       │                       │                      │
   │                       │                       │ prepare_store_kv()    │                      │
   │                       │                       │ → 排队 job 102        │                      │
   │                       │                       │                       │                      │
   │                       │                       │ start_kv_transfers()  │                      │
   │                       │                       │ → 提交上轮 store      │                      │
   │                       │                       │ → 提交本轮 load       │                      │
   │                       │                       │                       │                      │
   │                       │                       │──transfer_async(102)─>│                      │
   │                       │                       │                       │ compute_ptrs()       │
   │                       │                       │                       │ swap_blocks_batch()──>│
   │                       │                       │                       │  ← 立即返回           │ ← 异步拷贝
   │                       │                       │                       │ record end_event     │
   │                       │                       │<────── return True ───│                      │
   │                       │                       │                       │                      │
   │                       │                       │ get_finished()        │                      │
   │                       │                       │─── end_event.query()─>│                      │
   │                       │                       │<────── False ─────────│  (还没完成)           │
   │                       │                       │                       │                      │
   │                       │                       │                       │                      │
   │  ... 下一个 step ...  │                       │                       │                      │
   │                       │                       │                       │                      │
   │                       │                       │ get_finished()        │                      │
   │                       │                       │─── end_event.query()─>│                      │
   │                       │                       │<────── True ──────────│  (完成了!)            │
   │                       │                       │                       │                      │
   │                       │                       │ → WorkerMetadata      │                      │
   │                       │                       │   {completed:{102:1}} │                      │
   │                       │                       │                       │                      │
   │<──KVConnectorOutput───│<──────────────────────│                       │                      │
   │                       │                       │                       │                      │
   │ update_connector_output()                     │                       │                      │
   │ → pending_count -= 1  │                       │                       │                      │
   │ → complete_store()    │                       │                       │                      │
   │   → block.is_ready    │                       │                       │                      │
```

### 8.2 Load (CPU→GPU) 完整时序

```
Scheduler              ModelRunner              Worker                  Handler              DMA Engine
   │                       │                       │                       │                      │
   │ 新请求到达             │                       │                       │                      │
   │ get_num_new_matched_tokens()                  │                       │                      │
   │ → manager.lookup(key) │                       │                       │                      │
   │ → 命中 CPU 缓存       │                       │                       │                      │
   │ → 返回 num_hit_tokens │                       │                       │                      │
   │                       │                       │                       │                      │
   │ allocate_slots()      │                       │                       │                      │
   │ → 分配 GPU 块         │                       │                       │                      │
   │                       │                       │                       │                      │
   │ update_state_after_alloc()                    │                       │                      │
   │ → manager.prepare_load()                      │                       │                      │
   │   → ref_cnt++         │                       │                       │                      │
   │   → CPUSpec[3]        │                       │                       │                      │
   │ → TransferJob 101     │                       │                       │                      │
   │                       │                       │                       │                      │
   │ build_connector_meta  │                       │                       │                      │
   │ → Metadata{load_jobs:{101:...}}               │                       │                      │
   │                       │                       │                       │                      │
   │──SchedulerOutput──────>                       │                       │                      │
   │                       │                       │                       │                      │
   │                       │ start_kv_transfers()  │                       │                      │
   │                       │                       │──transfer_async(101)─>│                      │
   │                       │                       │                       │ swap_blocks_batch()──>│
   │                       │                       │                       │  (CPU→GPU, H2D)      │ ← 异步
   │                       │                       │                       │                      │
   │                       │                       │ get_finished()        │                      │
   │                       │                       │ → end_event.query()   │                      │
   │                       │                       │                       │                      │
   │  ... 等待完成 ...      │                       │                       │                      │
   │                       │                       │                       │                      │
   │                       │                       │ get_finished() → True │                      │
   │                       │                       │ → finished_recving    │                      │
   │                       │                       │   .add("req_99")      │                      │
   │                       │                       │                       │                      │
   │<──finished_recving────│<──────────────────────│                       │                      │
   │                       │                       │                       │                      │
   │ 请求状态:             │                       │                       │                      │
   │ WAITING_FOR_REMOTE_KVS → WAITING              │                       │                      │
   │ → complete_load()     │                       │                       │                      │
   │   → ref_cnt--         │                       │                       │                      │
   │ → 请求继续推理         │                       │                       │                      │
```

### 8.3 抢占与 Offloading 交互时序

```
Scheduler              Worker                  Handler
   │                       │                       │
   │ Step N:               │                       │
   │ 请求 C 被抢占          │                       │
   │ _preempt_request(C)   │                       │
   │ → free GPU blocks     │                       │
   │ → status=PREEMPTED    │                       │
   │                       │                       │
   │ build_connector_meta  │                       │
   │ → preempted_req_ids   │                       │
   │   包含 C 的 request_id │                       │
   │ → jobs_to_flush       │                       │
   │   包含 C 的 store job  │                       │
   │                       │                       │
   │──SchedulerOutput──────>                       │
   │                       │                       │
   │                       │ handle_preemptions()  │
   │                       │ → 提交积压 store      │
   │                       │──transfer_async──────>│
   │                       │                       │ → swap_blocks_batch()
   │                       │                       │
   │                       │ → wait(jobs_to_flush) │
   │                       │                       │ → end_event.synchronize()
   │                       │                       │   (阻塞等待 DMA 完成)
   │                       │                       │
   │                       │ ← 等待完成 ────────────│
   │                       │                       │
   │ Step N+1:             │                       │
   │ C 的 GPU 块已安全备份  │                       │
   │ 可分配给新请求         │                       │
```

---

## 9. 底层传输机制详解

### 9.1 块映射机制

GPU block 到 CPU sub-block 的映射由 Scheduler 在创建 TransferJob 时确定：

```
Scheduler 侧:
  GPU 块: [50, 51, 52, 53]  (来自 KVCacheManager)
  CPU 块: [12]              (来自 CPUOffloadingManager.prepare_store)
  block_size_factor = 4

Handler 侧 (transfer_async):
  GPU src_blocks = [50, 51, 52, 53]  (factor=1, 不展开)
  CPU dst_blocks = [12]              (factor=4, 展开为 sub_block)

  expand_block_ids([12], factor=4) → [48, 49, 50, 51]

  compute_sub_block_ptrs:
    GPU block 50 → CPU sub_block 48 (CPU block 12 的第 0 个 sub_block)
    GPU block 51 → CPU sub_block 49 (CPU block 12 的第 1 个 sub_block)
    GPU block 52 → CPU sub_block 50 (CPU block 12 的第 2 个 sub_block)
    GPU block 53 → CPU sub_block 51 (CPU block 12 的第 3 个 sub_block)

  CPU block 12 (4× GPU block size):
  ┌─────────┬─────────┬─────────┬─────────┐
  │ sub 48  │ sub 49  │ sub 50  │ sub 51  │
  │←GPU 50  │←GPU 51  │←GPU 52  │←GPU 53  │
  └─────────┴─────────┴─────────┴─────────┘
```

### 9.2 异步传输机制

整个传输是**全异步**的，分三层：

```
Python 层 (transfer_async)     ← 提交任务，立即返回
  │
  ▼
CUDA/NPU 驱动层                ← 提交 DMA 拷贝到 Stream，立即返回
  │
  ▼
硬件层 (DMA Engine)            ← 实际搬运数据，与 GPU 计算并行
```

**CUDA Stream 同步屏障**:

```
Compute Stream:  [attention forward][sampling]──┐
                                               │ wait_stream
Transfer Stream:                               └──[memcpy]──

wait_stream 是 GPU 硬件层面的依赖:
  CPU 线程调用时不阻塞
  只是告诉 CUDA 驱动: "Transfer Stream 的后续操作必须等 Compute Stream 到达这里"
```

**链式排序** (多个 transfer 共用一个 Stream):

```
Transfer Stream:  [job A]──end_event_A──┐
                                        │ wait_event
                             [job B]────┘──end_event_B──┐
                                                        │ wait_event
                                             [job C]────┘

CPU 线程提交完就走了，不等 DMA 完成
wait_event 保证的是 Stream 内部的执行顺序
```

### 9.3 完成检测

```python
# 非阻塞轮询 (get_finished):
end_event.query()
  True  → DMA 完成，数据已搬完
  False → DMA 还在工作

# 阻塞等待 (wait, 用于 jobs_to_flush):
end_event.synchronize()
  CPU 线程挂起，直到 DMA 完成
```

### 9.4 底层拷贝实现

#### GPU (CUDA): `swap_blocks_batch`

```cpp
// csrc/libtorch_stable/cache_kernels.cu

// 三层策略:
// 1. CUDA 12.8+: cuMemcpyBatchAsync (单次驱动调用)
//    - 通过 cuGetProcAddress 运行时解析
//    - 支持 srcAccessOrder 优化
// 2. ROCm 7.1+: hipMemcpyBatchAsync
// 3. 回退: 循环 cudaMemcpyAsync(..., cudaMemcpyDefault, stream)
//    - cudaMemcpyDefault 让驱动自动判断方向

// 方向自动推断:
//   src 是 GPU 地址 + dst 是 CPU 地址 → DeviceToHost
//   src 是 CPU 地址 + dst 是 GPU 地址 → HostToDevice
```

#### NPU (CANN): `swap_blocks_batch`

```cpp
// csrc/torch_binding.cpp

// 两层策略:
// 1. CANN 8.5+: aclrtMemcpyBatchAsync (单次 API)
//    - direction 参数显式指定: 0=H2D, 1=D2H, 2=D2D
//    - 使用 aclrtMemcpyBatchAttr 指定 src/dst location
// 2. 回退: 循环 aclrtMemcpyAsync
```

### 9.5 Store 与推理的关系

Store 是拷贝，不是移动。GPU 数据留在原地，请求继续用它推理：

```
Step N:   请求 A decode → attention 读 GPU block 50-53
          同时 store job 在 DMA 上把 block 50-53 拷贝到 CPU

Step N+1: 请求 A 继续 decode → 继续读 GPU block 50-53
          store 可能还没完成，无所谓

Step N+2: store 完成，CPU 有了备份
          GPU 数据不动，请求 A 继续用
```

GPU 块回收条件：
1. 请求完成 → `kv_cache_manager.free()`
2. 如有 pending store → 注册到 `_block_id_to_pending_jobs`
3. 新请求分到这些块 → 触发 flush → 等 store 完成后才能覆写

---

## 10. 关键函数签名与参数规格

### 10.1 调度器层

```python
# KVCacheManager.allocate_slots
def allocate_slots(
    self,
    request: Request,                    # 请求对象
    num_new_tokens: int,                 # 需要分配的新 token 数
    num_new_computed_tokens: int = 0,    # 前缀缓存命中的 token 数
    new_computed_blocks: KVCacheBlocks | None = None,
    num_lookahead_tokens: int = 0,       # 投机解码 lookahead
    num_external_computed_tokens: int = 0, # 外部 KV 源已计算
    delay_cache_blocks: bool = False,    # 延迟缓存 (异步传输时)
    num_encoder_tokens: int = 0,         # cross-attention
    full_sequence_must_fit: bool = False,
) -> KVCacheBlocks | None

# CPUOffloadingManager.prepare_store
def prepare_store(
    self,
    keys: Collection[OffloadKey],        # 要存储的块标识
    req_context: ReqContext,             # 请求上下文
) -> PrepareStoreOutput | None
# 返回 None 表示 CPU 空间不够且无法驱逐

# CPUOffloadingManager.prepare_load
def prepare_load(
    self,
    keys: Collection[OffloadKey],        # 要加载的块标识
    req_context: ReqContext,
) -> LoadStoreSpec
# 增加 ref_cnt 防止块被驱逐
```

### 10.2 Handler 层

```python
# SingleDirectionOffloadingHandler (GPU)
class SingleDirectionOffloadingHandler(OffloadingHandler):
    def __init__(
        self,
        gpu_tensors: list[torch.Tensor],     # GPU KV cache (int8 view)
        cpu_tensors: list[torch.Tensor],     # CPU KV cache (pinned)
        block_size_factor: int,              # CPU块 / GPU块
        kv_cache_groups_data_refs: list[list[CanonicalKVCacheRef]],
        gpu_to_cpu: bool,                    # True=D2H, False=H2D
        mmap_region: SharedOffloadRegion | None = None,
    )
    def transfer_async(self, job_id: int, transfer_spec: TransferSpec) -> bool
    def get_finished(self) -> list[TransferResult]
    def wait(self, job_ids: set[int]) -> None

# CpuNpuOffloadingHandler (NPU)
class CpuNpuOffloadingHandler(OffloadingHandler):
    def __init__(
        self,
        gpu_block_size: int,                 # NPU 块大小 (token 数)
        cpu_block_size: int,                 # CPU 块大小
        num_cpu_blocks: int,                 # CPU 块总数
        gpu_caches: dict[str, torch.Tensor], # layer_name → (key, value)
        attn_backends: dict[str, type[AttentionBackend]],
    )
    def transfer_async(self, job_id: int, spec: TransferSpec) -> bool
    def get_finished(self) -> list[TransferResult]
```

### 10.3 底层拷贝

```python
# GPU: ops.swap_blocks_batch
def swap_blocks_batch(
    src_ptrs: torch.Tensor,              # int64 CPU tensor, 源指针数组
    dst_ptrs: torch.Tensor,              # int64 CPU tensor, 目标指针数组
    sizes: torch.Tensor,                 # int64 CPU tensor, 每块字节数
    is_src_access_order_any: bool = False,
    # True: 允许 DMA 预取 (仅 CPU→GPU 安全)
    # False: 保持流顺序 (GPU→CPU 必须)
) -> None

# NPU: torch.ops._C_ascend.swap_blocks_batch
# C++ 签名:
void swap_blocks_batch(
    const torch::Tensor& src_ptrs,       # int64 CPU tensor
    const torch::Tensor& dst_ptrs,       # int64 CPU tensor
    const torch::Tensor& sizes,          # int64 CPU tensor
    int64_t direction                    # 0=H2D, 1=D2H, 2=D2D
)
```

---

## 11. GPU vs NPU 差异对比

| 维度 | GPU (vllm) | NPU (vllm-ascend) |
|------|-----------|-------------------|
| **Stream 类型** | `torch.cuda.Stream` | `torch.npu.Stream` |
| **Event 类型** | `torch.Event` | `torch.npu.Event` |
| **底层拷贝 API** | `cuMemcpyBatchAsync` (CUDA 12.8+) | `aclrtMemcpyBatchAsync` (CANN 8.5+) |
| **回退拷贝 API** | `cudaMemcpyAsync` 循环 | `aclrtMemcpyAsync` 循环 |
| **方向参数** | 隐式 (`cudaMemcpyDefault` 自动推断) | 显式 `direction` 参数 (0/1/2) |
| **DMA 预取** | `is_src_access_order_any` 标志 | 不支持 |
| **KV 写入算子** | `reshape_and_cache` (CUDA kernel) | `_npu_reshape_and_cache` / `npu_scatter_pa_kv_cache` |
| **KV 读取算子** | `paged_attention` 内核集成 | `npu_paged_cache_load` / `npu_gather_pa_kv_cache` |
| **CPU 张量分配** | pinned memory 或 mmap | pinned memory |
| **指针计算** | `compute_sub_block_ptrs` (处理非连续) | `expand_block_ids` + 向量化广播 |
| **张量布局** | 统一 int8 view `(num_blocks, page_bytes)` | 原始 dtype `(blocks, size, heads, dim)` × 2 |
| **Event 池** | Stream + Event 双池 | 仅 Event 池 |
| **专用 Connector** | 无 | `CPUOffloadingConnector` (ZMQ + SharedMemory) |

### NPU 独有特性

1. **aclrtMemcpyBatchAsync** (CANN 8.5+): 单次 API 提交所有块拷贝
2. **共享内存 CPU KV Cache**: `multiprocessing.shared_memory` 跨 DP rank 共享
3. **压缩 MLA (DSA)**: `CompressAttentionManager`, compress_ratio = 4 或 128
4. **混合块大小**: 物理块拆分为多个逻辑块
5. **上下文并行 (DCP/PCP)**: KV Cache 跨 NPU rank 交错存储
6. **2MB 对齐**: Prefill 解聚合场景下 KV Cache 按 2MB 边界对齐
7. **自定义 AICore 内核**: `transpose_kv_cache_by_block`, `load_index_kv_cache`

---

## 12. 代码文件索引

### vllm (GPU) 关键文件

| 文件路径 | 作用 |
|---------|------|
| `vllm/v1/core/sched/scheduler.py` | 主调度器，协调 KV 分配和卸载决策 |
| `vllm/v1/core/kv_cache_manager.py` | GPU KV Cache 块分配和前缀缓存 |
| `vllm/v1/core/kv_cache_coordinator.py` | 多组 KV Cache 协调 (混合模型) |
| `vllm/v1/core/block_pool.py` | 底层块池 + LRU 管理 |
| `vllm/v1/kv_offload/base.py` | 卸载基础抽象 (OffloadingManager, OffloadingSpec, LoadStoreSpec) |
| `vllm/v1/kv_offload/cpu/manager.py` | CPU 卸载管理器 (LRU/ARC 策略) |
| `vllm/v1/kv_offload/cpu/gpu_worker.py` | **GPU↔CPU 数据传输 Handler** |
| `vllm/v1/kv_offload/worker/worker.py` | 卸载 Worker (多 Handler 管理) |
| `vllm/distributed/kv_transfer/kv_connector/v1/offloading_connector.py` | 卸载连接器入口 |
| `vllm/distributed/kv_transfer/kv_connector/v1/offloading/scheduler.py` | 调度器侧卸载逻辑 |
| `vllm/distributed/kv_transfer/kv_connector/v1/offloading/worker.py` | Worker 侧卸载逻辑 |
| `vllm/distributed/kv_transfer/kv_connector/v1/offloading/common.py` | 消息类型定义 (TransferJob, Metadata) |
| `vllm/_custom_ops.py` | `swap_blocks_batch` / `swap_blocks` Python 封装 |
| `csrc/libtorch_stable/cache_kernels.cu` | CUDA `swap_blocks_batch` 实现 |

### vllm-ascend (NPU) 关键文件

| 文件路径 | 作用 |
|---------|------|
| `vllm_ascend/kv_offload/cpu_npu.py` | **NPU↔CPU 数据传输 Handler** |
| `vllm_ascend/kv_offload/npu.py` | NPU 卸载规格 (NPUOffloadingSpec) |
| `vllm_ascend/distributed/kv_transfer/kv_pool/cpu_offload/cpu_offload_connector.py` | Ascend 专用 CPU 卸载连接器 |
| `vllm_ascend/distributed/kv_transfer/kv_pool/cpu_offload/cpu_kv_cache_manager.py` | CPU 侧 KV Cache 块管理 |
| `vllm_ascend/distributed/kv_transfer/kv_pool/cpu_offload/metadata.py` | ZMQ RPC 元数据服务器 |
| `vllm_ascend/device/device_op.py` | NPU 设备操作 (reshape_and_cache, kv_cache_load) |
| `vllm_ascend/worker/model_runner_v1.py` | NPU 模型运行器 (KV Cache 初始化) |
| `vllm_ascend/worker/block_table.py` | NPU 块表管理 |
| `vllm_ascend/core/single_type_kv_cache_manager.py` | 自定义 KV Cache 管理器 (压缩 MLA) |
| `vllm_ascend/patch/platform/patch_kv_cache_coordinator.py` | KV Cache 协调器补丁 |
| `csrc/torch_binding.cpp` | C++ `swap_blocks_batch` / `swap_blocks` 实现 |

### 环境变量

| 变量名 | 仓库 | 作用 |
|--------|------|------|
| `VLLM_USE_PRECOMPILED` | vllm | 使用预编译二进制，跳过原生构建 |
| `VLLM_ASCEND_ENABLE_BATCH_MEMCPY` | vllm-ascend | 控制 `aclrtMemcpyBatchAsync`: "1"=启用, "0"=禁用, None=自动 |
| `VLLM_ASCEND_FUSION_OP_TRANSPOSE_KV_CACHE_BY_BLOCK` | vllm-ascend | 启用融合转置 KV Cache 算子 |
| `VLLM_ASCEND_APPLY_DSV4_PATCH` | vllm-ascend | 启用 DeepSeekV4 MultiBlockPool |
