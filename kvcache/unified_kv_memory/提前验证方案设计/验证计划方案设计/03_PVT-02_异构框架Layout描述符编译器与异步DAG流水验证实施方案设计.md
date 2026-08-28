# PVT-02：异构框架 Layout 描述符编译器与异步 DAG 流水验证实施方案设计
## —— Mooncake 离散 Block 传输协议重构：连续块合并、跨框架适配与计算传输重叠

> **公共执行契约**：本项严格遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。描述符编译正确性、微秒级编译耗时以及计算—传输重叠收益必须独立记录并分别对账；跨进程通信协议中仅允许将包含固定字段的协议头 (Wire Header) 与硬件 Scatter-Gather 描述符条目 (SG Entry) 声明为标准网络/总线二进制布局，宿主进程内部包含 `std::vector` 动态容器的数据结构严禁声称具备固定长度或 POD (Plain Old Data) 内存布局。在包含固定模拟数据、虚拟连续地址或纯数学公式推导的测试场景下，评测结果统一限定标记为 `DEMO`。

> **验证范围声明**：在当前受控的原型验证工程中，`descriptor_compiler.cc` 能够对调用方传入的 `LogicalBlock` 内存数组执行基于源/目的物理地址连续性的基础合并；`async_dag_bench.cc` 则采用内部生成的模拟测试块，并通过 `compute_ms` 与 `dma_ms` 参数进行纯公式推导来计算异步流水耗时，尚未真实调用 CANN 驱动、URMA/UBMEM 协议栈或 NPU 硬件底层异步 Stream；`make_manifests.py` 当前仅用于生成离散的 JSON 负载描述文件，尚未被现有 C++ 基准程序读取消费；工程中目前尚未包含独立的 `descriptor_bench` 或 `eval_descriptor_pipeline.py` 自动化评估脚本。因此，现有受控源码主要用于验证地址合并核心算法与数据结构流程，不可直接作为证明已完成 vLLM/SGLang 生产框架适配、硬件 Scatter-Gather 实际执行或真实异步流水重叠收益的依据。

> **术语速查**：
> - **Descriptor**：描述符（向底层 DMA 控制器或网卡硬件提供源物理地址、目的物理地址、连续传输长度及控制标志的轻量元数据指令）；
> - **Layout**：内存布局（大模型注意力键值缓存 KVCache 在硬件显存中按模型层数、Token 序列、注意力头 Head 及数据类型组织排列的物理结构）；
> - **Block**：物理块（PagedAttention 分页显存池管理的基本物理单位，其字节大小与容纳 Token 数量由运行时参数决定）；
> - **ExtentManifest**：连续数据块区间清单描述符（用固定字段描述逻辑位置、物理起始基地址、连续长度及内存布局信息的结构化元数据数组）；
> - **Scatter-Gather**：分散-聚集 DMA（支持在单次硬件队列提交中同时描述并搬运多段物理离散内存块的高性能 DMA 传输机制）；
> - **DAG**：Directed Acyclic Graph（有向无环图：用于严格描述大模型分层计算、数据搬运与依赖同步顺序的无环执行图）；
> - **Stream**：设备异步执行队列（加速器硬件上按序执行指令任务的异步流通道）；
> - **Event**：硬件时间线同步事件（用于在不同 Stream 之间建立细粒度依赖与精确时间戳采样的同步原语）；
> - **POD**：Plain Old Data（平凡旧数据结构：具备确定性内存字节布局、支持直接进行跨进程内存复制与网络传输的紧凑结构体）。

> **验证 ID**：PVT-02
> **验证名称**：异构框架 Layout 描述符编译器与异步 DAG 流水验证
> **验证优先级**：**🔴 P0 级（核心关键项）**
> **对应验证阶段**：**E1（核心数据路径打通与多卡状态同步）**
> **证伪标记**：否（关键执行链确认）
> **主关联 IR**：`IR-01-02`, `IR-01-04`
> **核心 SRS / SR23 锚点**：
> - SRS：`L3-SE-DescriptorFromManifest-079`, `L3-MC-LayoutTransformPlan-078`, `L2-OL-BulkDescriptor-025`, `L2-OL-LayoutNegotiation-024`
> - SR23：`SR23-01-02-01`, `SR23-01-04-01`, `SR23-02-06-01`
> **配套源码**：[`./原型验证代码/PVT-02/`](./原型验证代码/PVT-02/)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`；vLLM `842dd8fd96650063e1ad32e6075742d457d39773`。正式测试结果必须以现场代码包、框架版本、硬件设备及配置哈希为准。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 描述符是什么：从物流清单到硬件 DMA 指令

在大模型推理系统中，一段超长 Prompt 所对应的 KVCache（大模型注意力键值缓存，即自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免后续 Token 生成时重复计算注意力）往往离散分布在显存池的众多物理页块中。若每个 Block 都由 CPU 单独发起一次传输请求，Host CPU 将陷入极其繁重的物理地址解析、长度计算及硬件队列门铃敲击 (Doorbell) 操作中，导致控制面提交耗时随 Block 数量呈线性暴涨，极易堵塞底层的硬件指令提交队列。

描述符的本质就是一张高度精炼、仅记录正文数据搬运控制信息的轻量元数据清单：

```text
源物理地址 (Src Phys Addr) → 目的物理地址 (Dst Phys Addr) → 连续传输字节数 (Length) → 绑定 Stream/队列 → 硬件控制标志 (Flags)
```

描述符自身不包含任何 KVCache 的正文张量数据，不负责承载复杂的 Python 高级对象，更不等同于宿主进程内部的动态 `std::vector` 容器。真正供底层硬件执行的条目必须采用定长固定字段、具备明确的内存对齐与地址边界校验；宿主侧的动态容器仅用于在编译阶段暂存中间结果，严禁将其直接当作跨进程或跨节点的网络传输协议头。

### 0.2 为什么需要描述符编译器

在大模型推理引擎中，PagedAttention（分页注意力：将超长序列的 KVCache 切分为若干固定尺寸物理块以消除显存碎片的技术）机制虽然大幅提升了显存利用率，但逻辑上连续的 Token 序列在物理显存中被映射为离散分布的物理块。一段长达数万 Token 的 Prompt 可能对应几十至数千个物理 Block。

若将这些离散 Block 逐个直接提交给通信层进行网络搬运，系统将承受巨大的额外开销：

1. 推理框架侧需反复遍历 BlockTable、Radix 树或 Span 区间，并频繁执行跨语言跨进程的数据序列化；
2. 跨进程传递大量的离散物理地址、块长度及内存布局元数据，剧烈争用 IPC 通道；
3. 为每个微小的离散块频繁构造并提交硬件 Scatter-Gather (SG) 描述符，引发严重的控制面提交开销；
4. 产生海量的细粒度硬件完成中断，增加事件同步排队与异常边界处理成本。

描述符编译器的核心工程使命，是在微秒级时间内将推理框架上报的“离散逻辑块数组”编译贪心合并为“硬件可高效批量执行的连续区间描述符数组”，将源地址与目的地址在物理上同时连续的相邻物理块合并为单条大块描述符：

```text
离散输入： [S0→D0, 长度 L][S1→D1, 长度 L][S2→D2, 长度 L][物理断开][S3→D3, 长度 L]
连续合并： [S0→D0, 连续长度 3L] ──────────────────────────────► [S3→D3, 连续长度 L]
```

合并后的描述符条目数、微秒级编译耗时、地址边界合法性及硬件实际提交次数必须基于真实输入与底层硬件事件进行严密测量，严禁预设固定的合并比例。

### 0.3 异构 Layout 与跨框架适配

不同的大模型推理框架（如 vLLM、SGLang 等）在 KVCache 的显存组织形式上存在显著差异：例如逻辑页表索引结构、物理 Block 块大小（如 16 或 64 Token）、模型层优先还是 Token 优先排列、注意力头 Head 切分方式以及物理显存句柄管理等均不尽相同。

跨框架适配器的核心职责是将各框架专有的逻辑显存结构标准化转换为统一的 `ExtentManifest`（连续数据块区间清单描述符），彻底杜绝直接将框架内部的逻辑 Block ID 误当成物理地址进行搬运：

```text
vLLM BlockTable ─────┐
SGLang Radix / Span ─┼─► 统一 ExtentManifest ─► 描述符编译器 ─► 硬件 Scatter-Gather 条目
现场显存分配器句柄 ────┘
```

适配器必须在运行时严格校验：逻辑 Token 连续区间、设备物理基地址、块字节大小、数据精度类型、模型层数、张量并行 (TP) 切分维度、硬件地址对齐以及内存读写权限。一旦布局元数据发生缺失或歧义，后续所有的描述符合并与数据搬运均无法保证语义正确。

### 0.4 异步 DAG 的物理原理

在传统的同步串行执行模式下，若必须等待整段 Prompt 的全量 KVCache 彻底搬运完毕后才启动前向 Prefill 计算，计算与通信之间将呈现完全串行的等待关系：

```text
传统串行：    [计算全量模型层] ──────────────► [网络传输全量层 KV] ──────────────► [启动下一阶段]
```

基于有向无环图 (DAG) 的分层异步流水机制，旨在通过加速器硬件事件 (Event) 建立细粒度的跨流依赖，使得已计算完成的特定 Layer 数据在满足消费资格后立即提交异步 DMA 传输，同时计算 Stream 无需等待并无缝启动下一层的 Prefill 计算：

```text
计算 Stream：  [Compute Layer 0] ─► [Compute Layer 1] ─► [Compute Layer 2] ─► ...
                     │                     │                     │
传输 Stream：        └─[DMA Layer 0]───────┴─[DMA Layer 1]───────┴─[DMA Layer 2]──► ...
```

在系统评测中，“代码能够同时下发两个任务”绝不等于“硬件物理上已经实现重叠”。必须通过加速器硬件 Stream、Event 依赖、完成中断时间线以及端到端数据依赖进行严格交叉取证，方可精确量化计算与传输的物理重叠率；仅将浮点参数代入数学公式只能验证理论上限，无法证明物理重叠收益已经达成。

### 0.5 当前配套工程能够证明什么，不能证明什么

| 验证子项 | 当前受控源码能够完成的执行动作 | 当前受控源码尚不能证明的内容 | 默认证据等级 |
|---|---|---|---|
| 连续性合并逻辑 | 对内部构造的 `LogicalBlock` 数组基于源/目的物理地址连续性执行 O(N) 贪心扫描与合并 | vLLM/SGLang 真实生产布局、真实物理地址合法性、目标端数据一致性 | `DEMO / W0` |
| Manifest 生成工具 | 生成包含指定块数、碎片率及十六进制物理地址的结构化 JSON 负载描述文件 | 当前 C++ benchmark 尚未读取该 JSON，无法证明框架 BlockTable/Span 生产适配 | `DEMO` |
| 描述符编译耗时 | 对内部模拟内存数组执行高频重复的 `compile_manifest` 调用，输出 P50/P95/P99 耗时 | 跨进程序列化开销、底层硬件描述符批量下发提交开销、CPU 控制面卸载收益 | `DEMO / W0` |
| 异步 DAG 耗时 | 基于输入的 `compute_ms`、`dma_ms` 及 `chunks` 参数执行纯数学公式推导，输出理论流水重叠率 | CANN/NPU 双 Stream 硬件执行、Event 事件依赖、真实 DMA 完成中断及端到端 TTFT 收益 | `DEMO / W0` |

---

## 1. 验证目标与交付结论定义

### 1.1 待验证核心命题

1. **命题一：源/目的物理布局的连续性能够被安全且无损地合并**。针对跨框架输入的离散物理块，编译器仅在源物理地址与目的物理地址同时保持严格连续、长度一致且权限合法的前提下执行合并；对于物理断开、长度不匹配或源/目的数量异常的输入，必须严格保留物理边界或显式抛出错误码，严禁发生地址越界或长度失真。
2. **命题二：批量描述符编译能够显著削减控制面提交开销**。在相同逻辑 Block 数量与显存碎片率输入下，严密对比逐块提交与批量合并提交的描述符条目数、微秒级编译耗时、硬件提交次数及 CPU 控制面开销；候选准入门槛为描述符条目数减少 $\ge 50\%$、控制面提交耗时降低 $\ge 40\%$（具体门槛需在测试运行前严格固化）。
3. **命题三：定长二进制协议能够安全高效地实现跨进程元数据传递**。协议头 (Wire Header)、硬件 SG 条目及数组长度必须具备严格的版本标识、定长字节边界、确定性大小端序、内存对齐及边界校验机制；宿主进程的动态容器严禁被误当成可直接跨进程共享的 POD 结构。
4. **命题四：计算与数据传输能够形成物理级的真实异步流水重叠**。在接入真实的加速器 NPU Stream、Event 同步事件及物理 DMA 完成中断的前提下，异步 DAG 流水方案的端到端执行耗时相对同步串行基线实现确定性的可复现降低；候选物理重叠率准入门槛为 $\ge 60\%$（非纯数学公式推导值）。

### 1.2 交付物与结论边界

每个正式的 `run_id` 必须至少交付以下结构化资产：

1. **《描述符输入与合并条目映射校验表》**：完整记录输入 Block 序列、输出 SG 条目、合并断点边界、字节长度守恒及物理地址映射关系的逐样本核验数据；
2. **《描述符编译器性能与压缩率对照表》**：详细记录编译耗时 P50/P95/P99 分位数、描述符条目压缩率、硬件提交次数及 Host CPU 控制面开销对比；
3. **《ExtentManifest 二进制协议规范与跨进程测试记录》**：明确 Wire Header 与 Extent 条目的固定字段定义、字节大小、端序规范及跨进程共享内存读取校验记录；
4. **《异步 DAG 物理执行时间线与重叠率分析报告》**：基于 Profiler 导出的 Compute/Transfer 硬件时间线、Event 依赖图谱、同步串行基线与流水实测耗时；
5. **标准证据包**：包含 `manifest.json`、`environment.json`、原始输入/输出数据、控制台日志、分析摘要及分项技术判定结论 (`GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE`)。

---

## 2. 核心数据结构与算法设计

### 2.1 当前工程实际数据结构

当前受控工程 `descriptor_compiler.h` 中定义的结构体如下：

```cpp
struct LogicalBlock {
    uint64_t block_id;
    uint64_t phys_addr;
    uint32_t size_bytes;
};

struct HardwareSGEntry {
    uint64_t src_phys_addr;
    uint64_t dst_phys_addr;
    uint32_t len_bytes;
    uint32_t flags;
};

struct WireBatchDescriptorHeader {
    uint32_t version;
    uint32_t flags;
    uint32_t entry_count;
    uint32_t reserved0;
    uint64_t total_bytes;
    uint64_t manifest_id;
    uint8_t reserved1[32];
};
static_assert(sizeof(WireBatchDescriptorHeader) == 64);

struct CompiledBatch {
    WireBatchDescriptorHeader header;
    std::vector<HardwareSGEntry> entries;
};
```

技术边界必须严格明确：当前受控源码中仅 `WireBatchDescriptorHeader` 结构体通过静态断言严格限定为 64B POD；`HardwareSGEntry` 当前尚未显式声明 `alignas(64)` 与填充字段断言；`CompiledBatch::entries` 属于宿主进程的动态容器。严禁将包含 `std::vector` 的 `CompiledBatch` 误标为 64B POD 结构体或直接进行跨进程内存复制。

### 2.2 目标 ExtentManifest 二进制协议设计

面向跨框架生产级互通的标准化 ExtentManifest 二进制协议目标定义如下（具体字段与对齐需以最终受控代码为准）：

```cpp
struct alignas(64) ExtentManifestHeader {
    uint64_t request_id;
    uint32_t version;
    uint32_t framework_type;
    uint32_t total_tokens;
    uint32_t extent_count;
    uint64_t total_payload_bytes;
    uint64_t layout_hash;
    uint8_t reserved[24];
};

struct alignas(64) LogicalBlockExtent {
    uint64_t logical_token_start;
    uint32_t token_count;
    uint32_t stride_bytes;
    uint64_t phys_base_addr;
    uint32_t block_bytes;
    uint32_t flags;
    uint64_t layout_hash;
    uint64_t reserved;
};
```

上述结构体为生产级接口设计目标。在正式实现中必须配备 `static_assert(sizeof(...) == 64)`、明确的大小端序处理、版本向下兼容机制、地址权限校验、数组越界防护及数据完整性校验码。当通过 `/dev/shm`（Linux 共享内存文件系统）或其他共享队列传递元数据时，必须完整记录跨进程的生命周期管理、内存可见性屏障及访问权限控制。

### 2.3 跨框架 Layout 适配器

适配器仅负责将各推理框架内部的逻辑显存元数据进行标准化解析与转换，绝不涉及 KVCache 正文数据的物理拷贝。核心接口逻辑示意如下：

```cpp
// 接口示意伪代码：Block ID 至物理显存基地址的映射必须由现场显存分配器提供
void adapt_vllm_block_table(const VllmBlockTable& table,
                            const RuntimeLayout& layout,
                            std::vector<LogicalBlockExtent>& out);

void adapt_sglang_radix_spans(const SglangRadixSpans& spans,
                              const RuntimeLayout& layout,
                              std::vector<LogicalBlockExtent>& out);
```

适配器必须在运行时执行以下最小边界校验：

- 校验 `token_count > 0`，且 `block_bytes` 与模型数据精度、Head 维度及层数布局严格吻合；
- `phys_base_addr` 必须为现场分配器分配的合法物理地址或设备句柄，严禁用 `block_id × bytes_per_block` 简单线性推算；
- 源端与目的端描述符条目数量与逻辑序列必须严格一致；
- 物理地址、连续长度、跨步步长 (Stride) 与内存权限必须严格满足硬件对齐约束；
- `layout_hash`、模型架构、Tokenizer 词表哈希、张量并行 (TP) 切分维度及代码包版本必须精准追溯至同一次测试请求。

### 2.4 当前贪心合并算法与边界分析

当前受控代码 `DescriptorCompiler::compile_manifest` 采用单次顺序扫描算法：

```cpp
const bool contiguous =
    source[i].phys_addr == current.src_phys_addr + current.len_bytes &&
    target[i].phys_addr == current.dst_phys_addr + current.len_bytes;
```

当且仅当源物理地址与目的物理地址同时满足空间连续时，算法累加当前 SG 条目的传输长度；一旦出现任何一侧物理地址断开，则将当前条目压入输出队列，并以当前物理块为起点开启新的 SG 条目。算法的时间复杂度为严格的 $O(N)$，并将所有源块的累加字节数固化写入 `header.total_bytes`。

当前实现存在的工程边界与审计事实包括：

1. 当输入为空数组或源/目的块数量不一致时，当前函数直接返回空 batch，尚未提供标准错误码或 `invalid_reason` 错误归因；
2. 尚未校验源块与目的块的 `size_bytes` 是否严格相等，亦未进行 64 位物理地址整数溢出、零长度攻击及显存读写权限检查；
3. `header.manifest_id` 尚未通过输入参数显式绑定，`flags` 与条目控制标志尚未完全对应底层硬件协议语义；
4. 合并逻辑严格依赖输入数组的原始物理相邻顺序，不执行跨区间的全局排序重组，亦不跨越模型层或逻辑安全边界进行合并；
5. 当前尚未导出逐个输入物理块与输出 SG 条目的映射对照表，亦未集成硬件实际执行后的完成事件校验。

因此，当前算法实现属于合并核心逻辑的 DEMO 阶段，不可直接作为证明已具备跨框架数据完整性与硬件传输安全保障的最终依据。

### 2.5 DAG 时间模型与真实事件要求

当前受控工程 `async_dag_bench.cc` 采用如下理论推导公式：

$$
T_{serial}=(T_{compute}+T_{DMA})	imes chunks
$$

$$
T_{pipeline}=T_{DMA}+\max(T_{compute},T_{DMA})	imes(chunks-1)+T_{compute}
$$

并将由上述公式推算得出的理论重叠率 `overlap_pct` 写入 CSV 文件。该计算过程并未实际调用加速器硬件 Stream 或 Event 机制，`compute_ms` 与 `dma_ms` 仅为命令行传入的固定参数。

因此，正式的生产级重叠率评测必须基于底层硬件 Profiler 采集的真实细粒度事件时间戳进行严密计算：

```text
compute_start / compute_end (Layer i)       : 计算流针对第 i 层的 Prefill 启动与完成时间戳
transfer_submit / transfer_complete (Layer i): 传输流针对第 i 层 KV 数据的异步 DMA 提交与物理完成时间戳
event_record / event_wait (Layer i)          : 硬件同步事件的记录与阻塞等待时间戳
consumer_ready (Layer i)                     : 下游算子捕获到第 i 层数据已完全安全就绪的时间戳
```

只有上述物理时间线数据才能真实证明计算与数据搬运的实际时空交叠、等待开销及硬件流水依赖关系。

---

## 3. 实验方案与测试矩阵设计

### 3.1 两组子实验矩阵

| 验证子项 | 正式测试输入参数 | 核心观测指标与输出 | 当前源码支持状态分析 |
|---|---|---|---|
| 描述符编译与正确性 | 物理 Block 数量：16、64、256、1024；碎片率：0%、10%、50%、90%、100%；源/目的物理地址、块字节大小及布局哈希 | 输出 SG 描述符条目数、条目压缩率、字节长度守恒性、源/目的连续性判定、微秒级编译耗时 P50/P95/P99、异常错误码拦截 | 采用内部构造的模拟 `LogicalBlock` 数组；尚未支持外部 JSON 读取、框架真实适配与硬件执行校验 |
| 异步 DAG 流水 | 模型层数/分块 Chunk 数、真实计算耗时、真实 DMA 传输耗时、硬件 Stream/Event 依赖图谱及同场次串行基线 | 同步串行总耗时、异步流水总耗时、物理重叠率、跨流等待耗时、首字延迟 TTFT 改善、硬件错误与超时记录 | 仅支持纯数学公式推导与演示输出；尚未调用 CANN、NPU 底层 Stream 或 URMA/UBMEM 硬件驱动 |

### 3.2 参数矩阵与当前 CLI

| 测试维度 | 正式实施计划标准 | 当前工程实际支持情况 |
|---|---|---|
| 物理 Block 数量 | 16、64、256、1024、4096 | `async_dag_bench --block-count` 支持传入单值；`make_manifests.py --block-count` 支持传入单值 |
| 显存碎片率 | 0、0.1、0.5、0.9、1.0 | 两个工具均支持传入单个浮点数值；当前不支持在单次运行中传入批量 ratios 数组 |
| 分块 Chunk 数量 | 1、2、4、8、16、32 | `async_dag_bench --chunks` 支持传入单值 |
| 编译测量循环轮次 | 运行前严格固化；建议至少执行 1000 轮并保留逐次原始耗时 | `async_dag_bench --loops` 支持传入单值；默认示例为 1000 轮 |
| 计算与 DMA 耗时 | 来源于底层硬件 Profiler 实测时间线 | 当前 `--compute-ms` 与 `--dma-ms` 仅作为公式推导的输入参数 |
| 推理框架适配 | vLLM、SGLang 及现场生产框架 | 当前工程尚未内置框架适配器与真实框架 BlockTable 转换逻辑 |

### 3.3 环境与证据矩阵

| 运行环境级别 | 验证核心目的 | 最低前置条件 | 允许产出的证据结论 |
|---|---|---|---|
| W0 单机模拟 | 验证描述符编译器的连续性合并算法、定长结构体及公式计算逻辑的执行闭环 | C++ 编译器、Python 环境 | 仅可产出 `DEMO` 级别的工作流有效性结论 |
| W1 局部设备实验 | 验证真实框架布局适配器转换或单卡 NPU 双 Stream/Event 硬件异步流水 | 具备现场推理框架、真实硬件设备、原厂驱动 SDK 及 Profiler 工具 | 可产出绑定特定框架版本与硬件拓扑的 `LAB` 局部结论 |
| W2 端到端实验 | 验证跨节点批量描述符硬件下发执行与端到端分层异步流水的业务净收益 | 具备真实 NPU 集群、物理网络 DMA 直达、在线推理端点、完整 Profiler 时间线及多轮重复实测 | 满足全量证据闭环后，可产出 `MEASURED` 生产级结论 |

### 3.4 公平 A/B 对照要求

在开展编译器 A/B 对照测试时，必须严格保持输入 Block 物理序列、显存地址分布、块字节大小、编译测试轮次、CPU 绑核策略及内存分配机制的高度一致，测试中仅允许变更逐块提交与批量合并提交策略。在开展异步 DAG A/B 对照测试时，必须严格保持模型权重、模型层数、Token 总数、计算负载、DMA Payload 尺寸、硬件设备、Stream 优先级及系统资源配额的高度一致，测试中仅允许变更同步串行与异步流水调度策略；严禁通过传入不同 `compute_ms` 或 `dma_ms` 的公式参数人为编造重叠收益。

---

## 4. 测试工具、当前实现边界与正式扩展要求

### 4.1 当前受控工程目录与运行方式

```text
原型验证代码/PVT-02/
├── Makefile
├── descriptor_compiler.h     # 描述符结构体定义与编译器类声明
├── descriptor_compiler.cc    # 连续物理块 O(N) 贪心合并算法实现
├── async_dag_bench.cc        # 包含内部模拟块编译与公式推导 DAG 耗时的测试基准程序
└── make_manifests.py         # 生成符合 JSON 规范的合成 Block 碎片描述文件
```

当前可复现的 W0 构建与运行命令为：

```bash
cd ./原型验证代码/PVT-02
make clean
make
python3 ./make_manifests.py --block-count 1024 --frag 0.5 --out manifest_1024_frag0.5.json
./async_dag_bench --block-count 1024 --fragmentation 0.5     --chunks 16 --compute-ms 300 --dma-ms 160 --loops 1000     --out res_compiler_dag_demo.csv
```

特别说明：`make_manifests.py` 生成的 JSON 文件当前尚未被 `async_dag_bench` 直接读取消费；基准程序在运行时会在内存中重新生成内部 `mock_blocks` 数组。当前工程 CLI 尚未集成 `--tokens`、`--block-size`、`--fragmentation-ratios`、`--out-dir`、`--layers`、`--evidence-level` 等扩展参数，亦不包含独立的 `descriptor_bench` 或 `eval_descriptor_pipeline.py` 脚本，执行时切勿传入未支持的选项。

### 4.2 源码实际行为审计

| 源码文件与核心逻辑 | 源码实际执行行为 | 对实测证据等级的影响分析 |
|---|---|---|
| `make_manifests.py` | 基于固定随机种子生成 `block_count` 个 64KB 块，按 `--frag` 参数跳跃生成地址，输出包含碎片率的 JSON 结构体 | 可用于生成标准化合成负载；尚未对接到 vLLM/SGLang 真实布局，亦未被现有 C++ 基准程序读取 |
| `async_dag_bench.cc::mock_blocks` | 运行时在内存中自动构造 source 与 target 模拟块数组，默认配置为 1024 块与 0.5 碎片率 | 与外部 manifest 文件脱钩，无法证明外部 JSON 描述符已实际参与编译 |
| `descriptor_compiler.cc` | 针对相邻块执行严格的源/目的物理地址空间连续性合并；未校验长度一致性、整数溢出、内存权限及全量映射 | 可证明连续性合并算法自身逻辑正确，但不能证明已解决跨框架适配与生产数据安全性 |
| `async_dag_bench.cc` 编译循环 | 真实高频调用 `compile_manifest`，统计并输出编译耗时 P50/P95/P99、输出 SG 条目数及压缩率 | 测得的编译时延反映的是本地模拟数组的处理性能，输出 CSV 明确标注为 `DEMO,DEMO_ONLY` |
| `async_dag_bench.cc` DAG 计算 | 基于输入的 `compute_ms`、`dma_ms` 及 `chunks` 纯数值推导 `serial_ms`、`dag_ms` 及 `overlap_pct` | 未调用任何真实计算、硬件 DMA、底层 Stream/Event 或 Profiler 时间线；仅属于数学公式演示 |
| `HardwareSGEntry` 结构体 | 当前字段仅包含源地址、目的地址、传输长度及 flags，未显式声明 `alignas(64)` | 尚未满足定长 64B 硬件网络/总线二进制传输协议标准 |
| `CompiledBatch` 结构体 | 内部包含固定字段的 header 以及 `std::vector` 动态条目容器 | 仅可作为 Host 侧局部数据容器使用，严禁直接将其用于跨进程或跨节点的定长内存复制 |
| 基准程序输出标记 | 输出 CSV 明确标记 `status=DEMO,DEMO_ONLY`，控制台 stdout 明确提示 DAG overlap 为公式推导值 | 输出的任何数值均不可直接用于关闭 E1 阶段真实硬件路径的准入判定 |

### 4.3 面向 LAB/MEASURED 的最小工程扩展

在正式进入真实框架适配、硬件描述符提交与异步流水收益评估前，必须补齐以下工程支撑能力：

1. **Manifest 生产级接入**：扩展 C++ 基准程序以支持读取 `make_manifests.py` 导出的标准化 JSON 或真实推理框架导出的布局文件，严格校验 Schema 版本、块数、地址合法性、连续长度、布局哈希及源/目的对应关系；
2. **跨框架适配器开发**：分别实现面向 vLLM BlockTable 与 SGLang Radix/Span 的专用适配器，标准化转换为统一的 `ExtentManifest`，彻底杜绝将逻辑 Block ID 当作物理地址；
3. **安全健壮型编译器实现**：在编译器中补齐针对零长度、整数溢出、源/目的块长度不匹配、地址物理对齐、内存访问权限及模型层逻辑边界的严格校验，输出规范的错误码并生成逐块输入至输出 SG 条目的映射表；
4. **定长二进制协议固化**：为 Wire Header、Extent 条目及硬件 SG 描述符增加显式的 64B 定长对齐、端序转换及 `static_assert` 静态断言，将 Host 进程的动态容器与跨进程/跨节点的二进制数据流彻底分离；
5. **真实硬件描述符批量提交**：将合并后生成的硬件 SG 条目批量提交给原厂 URMA/UBMEM 或 DMA 驱动，完整记录硬件提交次数、物理完成中断、传输错误码及内存屏障 (Fence) 行为；
6. **真实异步 DAG 流水引擎对接**：基于现场加速器 NPU 的 Compute Stream、Transfer Stream 及 Event 硬件同步机制，采集各层计算、传输、等待及下游消费就绪事件，彻底淘汰公式推导逻辑；
7. **同场次同步串行基线严格对照**：在完全相同的工作负载与硬件环境下运行全量计算后全量传输的同步串行基线，采集同场次的真实端到端 TTFT 与资源利用率；
8. **全链路原始事件与证据包归档**：完整留存原始物理地址、SG 描述符条目、细粒度时间戳、失败异常日志、版本清单、网络拓扑、Profiler Trace 时间线及多轮重复实验数据；
9. **规范化状态枚举输出**：将内部的 `DEMO_ONLY` 及错误状态映射为公共契约规定的 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE` 标准枚举，未采集到的字段显式置为 `null` 并注明 `invalid_reason`。

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结实验身份、输入和证据等级

- **操作意图**：明确本轮评测的执行级别（W0/DEMO、W1/LAB 或 W2/MEASURED），杜绝将模拟数据或纯公式推导结果误判为真实硬件性能结论。
- **执行动作**：在配置清单中完整填报 `run_id`、`workload_schema_version`、`workload_id`、`package_id`、`baseline_commit`、`config_hash`、目标框架类型、模型物理布局、Block 数量、碎片率档位、块字节大小、Chunk 分块数、计算/DMA 负载参数、硬件设备、网络拓扑、预热策略、重复轮次及准入门槛。
- **应观察现象**：配置能够清晰界定测试输入来自外部 manifest 文件还是内部模拟生成，DAG 耗时来自底层设备 Profiler 时间线还是数学公式推导；若现场缺乏真实框架或硬件事件支持，应提前登记为 `NOT-SUPPORTED`。

### 步骤 1：生成合成 Manifest 并记录其解耦边界

- **操作意图**：生成可复现的显存地址碎片输入描述文件，验证 JSON 数据结构与参数构造流程，同时明确记录其当前尚未直接接入 C++ 编译器的技术现状。
- **执行命令**：

```bash
cd ./原型验证代码/PVT-02
python3 ./make_manifests.py --block-count 1024 --frag 0.5 --out manifest_1024_frag0.5.json
```

- **应观察现象**：导出的 JSON 文件完整包含 1024 个物理块、`fragmentation_ratio=0.5` 及十六进制物理地址；文件结构合法可解析，块总数与 header 声明严格一致。
- **证据边界**：当前 `async_dag_bench` 尚未读取该 JSON 文件。若需声称“该 manifest 已被编译器实际消费”，必须先实现第 4.3 节的接入扩展并在日志中记录输入文件的哈希校验码。

### 步骤 2：编译并运行当前描述符合并 W0 基线

- **操作意图**：验证现有 `compile_manifest` 算法的连续性扫描逻辑、输出条目数量、微秒级编译耗时及 CSV 导出格式，建立后续安全合并器的回归对比基线。
- **执行命令**：

```bash
make clean
make
./async_dag_bench --block-count 1024 --fragmentation 0.5     --chunks 16 --compute-ms 300 --dma-ms 160 --loops 1000     --out res_compiler_dag_demo.csv > compiler_dag_stdout.txt 2>&1
```

- **应观察现象**：CSV 完整包含 `sg_entries`、编译分位数、`compression_pct`、串行/流水耗时及 `DEMO,DEMO_ONLY` 标记；控制台 stdout 明确提示 DAG overlap 为公式推导值。
- **判定边界**：当前基准程序尚未包含逐块提交基线、字节长度守恒校验、地址映射核实及输出数据校验，不可仅凭 `compression_pct` 单一数值直接判定编译器验证通过。

### 步骤 3：执行描述符正确性和边界测试（条件扩展）

- **前置条件**：已将标准化 JSON 或真实推理框架布局接入编译器，并补齐源/目的长度一致性、整数溢出、地址对齐、权限控制、空数组及数量不匹配等边界校验逻辑。
- **操作意图**：确认每一个输入的物理 Block 均精准且唯一地映射至输出的某个 SG 描述符条目中，输出总字节数严格守恒，且连续合并绝不越过模型层或逻辑安全边界。
- **执行动作**：分别针对碎片率 0%、10%、50%、90%、100% 以及非法输入样本执行测试；完整留存输入 manifest、输出 SG 条目、错误码及逐块映射对照表。
- **应观察现象**：合法输入全部通过长度守恒、地址连续性及逻辑边界校验；非法输入被显式拦截并记录明确的 `invalid_reason`；严禁以返回空 batch 伪装为测试成功。

### 步骤 4：运行当前 DAG 公式基线

- **操作意图**：验证 `serial_ms`、`dag_ms` 及 CSV 结果字段的格式生成逻辑，为后续真实硬件 Profiler 时间线解析工具提供结构参考。
- **执行命令**：

```bash
./async_dag_bench --block-count 1024 --fragmentation 0.5     --chunks 16 --compute-ms 300 --dma-ms 160 --loops 1000     --out res_dag_formula_demo.csv
```

- **应观察现象**：输出的 `dag_ms` 严格由命令行输入参数推导计算所得；执行过程中未产生 NPU 硬件事件、Stream 句柄、DMA 完成中断或真实层级时间戳。
- **判定边界**：本步骤产出严格限定标记为 `DEMO`，严禁将公式推导的 `overlap_pct` 包装为物理计算—传输重叠率的实测依据。

### 步骤 5：接入真实框架 Layout 和硬件 Stream/Event（条件步骤）

- **前置条件**：现场部署的 vLLM/SGLang 框架布局、真实物理显存句柄/地址、底层硬件 DMA、NPU Compute/Transfer Stream、Event 硬件同步原语及 Profiler 观测工具均已就绪。
- **操作意图**：在同一测试负载下完整建立“推理框架布局 → ExtentManifest → 批量 SG 描述符下发 → 底层硬件完成 → 下游消费就绪”的全链路物理事件关系。
- **执行动作**：完整记录框架适配输入哈希、manifest 版本、源/目的物理地址、逐条 SG 描述符、提交/完成时间戳、内存屏障、Stream/Event 依赖图谱及错误码；同时严格运行同场次的同步串行基线。
- **应观察现象**：每一模型层的 KVCache 数据均具备可精准追溯的计算与传输硬件事件；若框架适配器或底层硬件能力缺失，必须规范标记为 `NOT-SUPPORTED`，严禁回退至模拟数值后继续冒用 `MEASURED` 标签。

### 步骤 6：计算压缩率、控制开销和真实重叠率

- **操作意图**：基于底层原始描述符条目与硬件 Profiler 时间线严密计算三项核心指标，彻底杜绝以公式推导值或粗略平均值替代真实物理事件。
- **计算口径**：

```text
描述符条目压缩率 = 1 - (合并后硬件 SG 描述符条目数 / 原始离散物理 Block 总数)
实际异步流水净收益 = 同场次同步串行基线总耗时 - 异步 DAG 流水实际总耗时
物理重叠率         = 基于 Profiler 采集的真实 Compute/Transfer 时间区间交叠与等待时间严格计算，并在 manifest 中固化定义
```

- **应观察现象**：每个计算结果均能精确追溯至输入负载、底层硬件事件及代码包版本；在缺乏 Profiler 时间线凭证时，重叠率字段统一填报为 `null` 并注明 `invalid_reason`。

### 步骤 7：生成标准证据包并重复实验对账

- **操作意图**：完整保存合并正确性校验、微秒级编译耗时、底层 Profiler 时间线、异常错误及物理环境快照，为后续技术决策与架构演进提供扎实的技术依据。
- **执行动作**：在 `results/PVT-02/<subtest>/<run_id>/` 目录下归档 `manifest.json`、`environment.json`、原始输入 manifest、输出 SG 条目、结果 CSV、Profiler Trace、系统日志、分析摘要及错误记录；每个测试条件确保至少完成 3 轮独立重复测量。
- **应观察现象**：报告中的 `planned_path`、`actual_path`、`evidence_level`、`status` 及 `invalid_reason` 完整齐备；严禁将模拟地址或公式推导结果填入 `MEASURED` 生产级字段。

---

## 6. 数据采集清单与记录格式

### 6.1 描述符编译原始字段

```text
run_id, validation_id, trace_id, event_name,
workload_id, framework, layout_hash, manifest_hash,
block_id, logical_token_start, token_count,
src_phys_addr, dst_phys_addr, block_bytes,
output_entry_id, merged_len_bytes, merge_boundary_reason,
raw_block_count, merged_sg_entries, total_input_bytes, total_output_bytes,
compile_start_ns, compile_end_ns, compile_duration_ns,
planned_path, actual_path, package_id, baseline_commit, config_hash,
device_id, hardware_profile, topology_profile,
evidence_environment, evidence_level, status, error_code, invalid_reason
```

字段填写约束：

- `src_phys_addr` 与 `dst_phys_addr` 必须来源于现场真实的显存分配器或明确标注的 DEMO 生成器；
- `total_input_bytes == total_output_bytes` 仅在逐项长度守恒校验通过时方可填报确认；
- `merged_sg_entries` 必须为编译器实际输出的条目计数，严禁由预设压缩率公式反向倒推；
- 针对空输入、数量不匹配、整数溢出及非法地址等异常，统一记录为 `null` 或错误状态，严禁用 0 伪装为成功。

### 6.2 异步 DAG 原始字段

```text
run_id, validation_id, trace_id, workload_id,
layer_id, chunk_id, stream_id, event_id, descriptor_id,
compute_start_ns, compute_end_ns,
transfer_submit_ns, transfer_complete_ns,
event_record_ns, event_wait_ns, consumer_ready_ns,
payload_bytes, actual_completed_bytes, wait_ns,
serial_total_ns, pipeline_total_ns, overlap_ratio,
planned_path, actual_path, package_id, baseline_commit, config_hash,
device_id, profiler_trace, evidence_environment, evidence_level,
status, error_code, invalid_reason
```

在当前的纯公式 DEMO 测试中，因未产生真实硬件事件，上述细粒度硬件事件字段统一显式填写为 `null` 并注明 `formula_only_demo`。

### 6.3 汇总 CSV 模板

以下为微基准汇总输出字段格式规范（非预置实测成绩）：

```csv
validation_id,run_id,subtest,framework,workload_id,layout_hash,block_count,fragmentation_ratio,raw_blocks,merged_sg_entries,compression_pct,compile_p50_us,compile_p95_us,compile_p99_us,chunks,serial_ms,pipeline_ms,overlap_pct,actual_completed_bytes,evidence_level,status,invalid_reason
<PVT-02>,<run_id>,<compile_or_dag>,<vllm_or_sglang_or_simulation>,<workload_id>,<layout_hash_or_null>,<count>,<ratio>,<count_or_null>,<count_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<chunks>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<DEMO_OR_LAB_OR_MEASURED>,<status>,<null_or_reason>
```

### 6.4 证据包目录结构

```text
results/PVT-02/<subtest>/<run_id>/
├── manifest.json
├── environment.json
├── input_manifest.json
├── raw_descriptor_events.jsonl
├── raw_dag_events.jsonl
├── raw_metrics.*
├── profiler_trace.*
├── summary.json
├── summary.csv
└── logs/
```

`manifest.json` 至少完整固化输入 Schema、推理框架/模型物理布局、代码包版本、基线 Git Commit、配置哈希、硬件设备、网络拓扑、执行 CLI 命令、原始数据哈希、证据等级、功能支持范围、未支持特性、准入门槛及最终判定状态。

---

## 7. GO / CONDITIONAL / NO-GO / NOT-SUPPORTED / INVALID-EVIDENCE 判定规则

### 7.1 描述符合并与跨框架适配判定

- **GO（正确性与控制面收益形成完整闭环）**：真实跨框架输入、定长二进制 Schema、源/目的物理地址、字节长度守恒、边界错误拦截及底层硬件完成事件完整齐备；在运行前冻结的典型显存碎片率下，描述符条目压缩率达到 $\ge 50\%$，控制面提交耗时降低达到候选门槛 $\ge 40\%$，且未发生任何数据错位、显存越界或资源泄漏。
- **CONDITIONAL（局部框架或模拟支持）**：仅在模拟输入或单一推理框架/单卡设备上通过验证，或仅证实了条目压缩率但未完成底层硬件执行核验；结论严格限定于已测条件。
- **NOT-SUPPORTED（功能未支持）**：现场缺乏真实推理框架布局适配器、硬件物理地址/句柄支持、目标 DMA 通路或跨进程二进制协议验证；当前 W0 测试桩产出即属于此类边界。
- **NO-GO（确证错误或收益未达标）**：在合法的测试中证实存在物理长度/地址计算错误、数据错位、程序崩溃、显存泄漏，或压缩率与控制面耗时优化未能达到运行前冻结的准入门槛。

### 7.2 异步 DAG 流水判定

- **GO（真实硬件时间线形成完整闭环）**：具备真实的加速器 Compute/Transfer Stream、Event 硬件同步原语、物理完成中断及同场次同步串行基线；异步流水总耗时相对同步串行基线呈现确定性降低，实测物理重叠率达到 $\ge 60\%$ 候选门槛，且首字生成延迟 (TTFT)、硬件错误率、超时率及显存资源开销未出现不可接受的恶化。
- **CONDITIONAL（公式推导或局部时间线支持）**：仅取得 W0 纯公式推导趋势，或仅采集到局部算子的 Stream 事件；结论仅能说明流程与理论可行性，不可直接关闭生产级物理重叠判定。
- **NOT-SUPPORTED（物理环境未支持）**：现场缺乏加速器 NPU Event 机制、真实 DMA 传输通路或 Profiler 硬件时间线。
- **NO-GO（流水未产生净收益）**：在真实的 A/B 对照实测中，异步流水总耗时未出现降低，或跨流事件同步、超时重试、硬件错误及显存开销抵消了计算—传输重叠带来的加速收益。

### 7.3 统一无效证据规则

凡出现以下任一情形，对应子实验一律判定为 `INVALID-EVIDENCE`，严禁给出 `GO` 结论：

- 虽使用 `make_manifests.py` 生成了 JSON 文件，但无法证明 C++ 基准程序在运行时实际读取并消费了该文件；
- 将当前 `compute_ms` 与 `dma_ms` 纯公式推导值或 `DEMO_ONLY` 输出包装为真实的 NPU/DMA 硬件时间线；
- 将宿主进程内部的 `std::vector` 动态容器或未显式断言大小的 SG 条目声称为定长跨进程二进制 POD 协议；
- 推理框架内部的逻辑 Block ID 被直接当作物理显存地址使用，或源/目的块长度、访问权限、地址对齐及逻辑边界未通过严格校验；
- 缺失原始输入负载、输出 SG 条目、长度守恒校验、硬件完成凭证、Profiler Trace 时间线、版本凭证、网络拓扑或重复实验数据；
- A/B 对照测试中擅自变更了输入布局、计算/DMA 负载参数、硬件设备、Stream 队列配置或统计口径；
- 关键指标缺失却采用 0 进行失真填充，或将模拟压缩率与公式重叠率直接填报为 `MEASURED` 生产级数据。

---

## 8. 执行阶段与交付闭环

测试实施划分为三个严密的演进阶段：

| 实施阶段 | 核心攻坚内容 | 阶段必须交付物 | 准出判定条件 |
|---|---|---|---|
| 阶段 A：结构与 W0 回归 | 严密审计当前定长 Header、合并算法、模拟 manifest 生成器及公式 DAG 的实际行为 | 源码审计报告、编译基准 CSV、公式推导 CSV、边界缺陷清单、`DEMO` 级 manifest | 明确当前已支持功能与未支持特性的技术边界 |
| 阶段 B：框架适配与设备执行 | 接入 vLLM/SGLang 真实框架布局、真实物理地址/句柄、硬件 DMA 及加速器 Stream/Event 机制 | 原始布局映射表、硬件 SG 条目、底层完成中断日志、Profiler Trace 时间线、重复实测数据 | 数据映射与硬件时间线均能精准追溯至底层原始事件 |
| 阶段 C：对账与准入 | 与逐块提交及同步串行基线展开严密 A/B 对照，量化描述符压缩率、控制面耗时优化及异步流水净收益 | 核心能力总结、A/B 严密对账表、技术决策判定报告及未支持说明 | 全量数据通过公共契约规范核验，彻底杜绝将模拟推导结果冒充硬件实测结论 |

本验证项的核心价值在于为后续异构大模型推理框架的无缝接入、跨节点异步高速传输及大规模描述符批量下发提供扎实的技术依据；本项严禁用单一的“条目数减少”推断数据必然正确，亦不可用纯数学公式推导的理论重叠率替代真实的硬件 Profiler 时间线。

---

## 9. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师核心职责**：
  1. 确认现场部署的推理框架版本、模型 Layout 结构、加速卡显存句柄/物理基地址、DMA 驱动、NPU Stream/Event 机制及 Profiler 采集权限；
  2. 固化物理 Block 数量、碎片率档位、块字节大小、Chunk 分块数、计算/DMA 负载参数、A/B 对照策略及目标证据等级；
  3. 实际执行测试并完整保存原始 manifest 文件、地址映射表、硬件完成中断、Profiler 时间线及异常错误日志；
  4. 严格审定各项物理地址、传输长度、硬件完成量及时间戳是否确由底层真实硬件设备采集所得，而非来源于模拟生成器或数学公式；
  5. 对跨框架适配正确性与生产级执行路径结论进行最终技术复核与签字把关。
- **AI Agent 协同职责**：
  1. 深入研读本方案设计、公共测试契约及五个原型验证源码文件，精准梳理实际支持的 CLI 参数、结构体定长字节边界、算法执行逻辑及当前未实现特性；
  2. 编写 manifest 数据解析、字节长度守恒校验、地址映射核验、合并算法回归测试、统计分位数计算及标准证据包生成工具；
  3. 严格核验二进制协议布局、源/目的物理连续性判定、描述符压缩率、同步/流水耗时及状态枚举的逻辑自洽性；
  4. 严守学术与技术诚信红线，严禁虚构 `descriptor_bench`、`eval_descriptor_pipeline.py`、CANN Event 硬件事件或编造固定压缩率。

### 9.2 可直接复制给 Coding Agent 的 Prompt 模板

```text
我正在执行 PVT-02：异构框架 Layout 描述符编译器与异步 DAG 流水验证。

请先研读以下核心文件：
1. ./提前验证方案设计/验证计划方案设计/03_PVT-02_异构框架Layout描述符编译器与异步DAG流水验证实施方案设计.md
2. ./提前验证方案设计/验证计划方案设计/Benchmark公共契约与证据分级规范.md
3. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-02/Makefile
4. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-02/descriptor_compiler.h
5. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-02/descriptor_compiler.cc
6. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-02/async_dag_bench.cc
7. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-02/make_manifests.py

执行约束与任务要求：
- 首先梳理源码实际支持的 CLI 参数与底层执行行为；确认 make_manifests.py 生成的 JSON 当前未被 async_dag_bench 读取消费，且工程中不存在 descriptor_bench 或 eval_descriptor_pipeline.py。
- 确认当前仅 WireBatchDescriptorHeader 具备 64B static_assert 静态断言，HardwareSGEntry 尚未声明定长 64B 对齐，CompiledBatch 包含 std::vector 严禁当作定长二进制 POD 结构。
- 针对 compile_manifest 算法，严格检查空输入、源/目的数量不匹配、源/目的物理连续性、字节长度守恒、地址溢出及错误状态处理；严禁以返回空 batch 伪装为成功。
- 确认当前 DAG 流水仅包含基于 compute_ms 与 dma_ms 的纯公式推导，输出必须严格标记为 DEMO；真实物理重叠率必须来源于真实的加速器 Compute/Transfer Stream、Event 硬件同步与 Profiler 时间线。
- 在方案设计与实现中，严格将 vLLM/SGLang 的 BlockTable/Span 逻辑映射、ExtentManifest 结构、硬件 SG 描述符及底层物理传输路径解耦隔离；严禁将框架内部的逻辑 Block ID 直接当作物理地址。
- 未采集到的字段显式置为 null 并详细注明 invalid_reason；完整留存原始 manifest、映射关系、异常失败日志及 Profiler Trace；严禁填充预设的固定示例数据。
- 最终输出：源码能力核验矩阵、实际执行命令清单、核心数据结构审计表、合并算法回归测试结果、DAG 证据边界分析、未支持特性清单以及下一步最小代码重构建议。
```

### 9.3 常见排错指南

- **执行 `make_manifests.py` 生成 JSON 后，C++ benchmark 运行结果毫无变化**：此为当前受控工程的代码现状，C++ 程序在内部独立构造了 `mock_blocks` 数组；需先实现文件读取解析并记录 manifest 哈希校验码，严禁声称外部 JSON 输入已在基准测试中生效。
- **描述符合并后的条目数出现异常极少值**：检查源物理地址与目的物理地址是否同时保持严格连续、物理块字节大小是否一致、是否误将模拟地址或人工排序后的数组当作真实显存布局；完整留存逐块映射对照与长度守恒校验日志。
- **源物理地址连续但目的物理地址不连续**：算法严禁执行合并；合并判定条件必须同时满足源端与目的端空间连续，严禁仅凭单一端点的连续性错误合并。
- **传入空输入数组或源/目的块数量不一致**：当前受控源码直接返回空 batch 且未附带错误码；正式工程必须显式返回 `INVALID-EVIDENCE` 或输入参数错误状态，并记录详细原因。
- **出现显存物理地址溢出或连续长度异常**：在编译器中补齐 64 位整数溢出、零长度、地址对齐及权限范围校验；严禁将未经合法性校验的物理地址直接下发给底层硬件队列。
- **尝试运行 `descriptor_bench` 或 `eval_descriptor_pipeline.py`**：当前受控工程目录中不存在上述文件；应基于现有的 `async_dag_bench.cc` 进行功能扩展，严禁将不存在的脚本或命令写入测试报告。
- **测得的 DAG 流水重叠率极高但缺乏 NPU Profiler 时间线**：该高数值仅为纯数学公式代入固定参数推导的理论结果；测试结果必须规范标记为 `DEMO`，等待接入真实 Stream/Event/Profiler 硬件时间线证据。
- **跨进程读取描述符时出现结构体大小或字段错位**：检查跨编译器的 ABI 兼容性、`alignas(64)` 内存对齐、大小端序及 `static_assert` 断言；宿主进程的 `std::vector` 动态容器绝不可直接写入跨进程共享内存。
- **推理框架内部 Block ID 转换为物理显存地址失败**：必须通过现场显存分配器接口获取真实的设备物理句柄/物理地址及访问权限，严禁使用 `block_id × block_size` 进行简单线性推算。
- **异步流水执行出现硬件超时或死锁卡死**：绘制各硬件 Event 的 `record` 与 `wait` 依赖拓扑图，确认 Compute Stream 与 Transfer Stream 属于同一物理加速器设备上下文；在缺乏硬件事件闭环时，严禁通过手动调整公式参数来掩盖真实的跨流依赖缺陷。
