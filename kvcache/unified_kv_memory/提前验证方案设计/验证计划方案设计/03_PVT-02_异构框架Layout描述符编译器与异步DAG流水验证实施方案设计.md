# PVT-02：异构框架 Layout 描述符编译器与异步 DAG 流水验证实施方案设计
## —— Mooncake 离散 Block 传输协议重构：连续块合并、跨框架适配与计算传输重叠

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。描述符正确性、编译时延和计算—传输重叠必须分别记录；固定字段的 wire header 与硬件 SG entry 才能讨论跨进程布局，含 `std::vector` 的宿主容器不作固定长度或 POD 承诺。固定样例、模拟地址和公式推演只能标为 `DEMO`。

> **验证范围声明**：当前受控工程中的 `descriptor_compiler.cc` 能对调用方传入的 `LogicalBlock` 数组做源/目的物理地址连续性合并；`async_dag_bench.cc` 使用内部生成的模拟块，并把 DAG 时间写成 `compute_ms`/`dma_ms` 的公式结果，没有调用 CANN、URMA、UBMEM 或真实 NPU Stream；`make_manifests.py` 只生成 JSON，不被当前 C++ benchmark 读取；工程中不存在 `descriptor_bench` 或 `eval_descriptor_pipeline.py`。因此当前代码可以验证部分合并逻辑和字段流程，但不能单独证明 vLLM/SGLang 适配、硬件 Scatter-Gather 执行或真实异步重叠收益。

> **术语速查**：Descriptor（描述符，即给 DMA/网卡硬件提供源地址、目的地址、长度和控制标志的元数据指令）；Layout（内存布局，即 KV 数据按层、Token、Head 和数据类型在设备内存中的组织方式）；Block（分页 KVCache 的物理块，大小和 Token 数由运行时配置决定）；ExtentManifest（连续数据块区间清单描述符，即以固定字段描述逻辑位置、物理起始地址、长度和布局信息的元数据数组）；Scatter-Gather（分散-聚集 DMA，即一次硬件提交中描述多段不连续源/目的内存）；DAG（Directed Acyclic Graph，有向无环图，即用事件依赖描述计算、传输和同步顺序）；Stream（设备异步执行队列）；Event（设备时间线事件，用于记录完成并建立跨 Stream 依赖）；POD（Plain Old Data，固定字段数据结构，可直接按字节布局传输）。

> **验证 ID**：PVT-02
> **验证名称**：异构框架 Layout 描述符编译器与异步 DAG 流水验证
> **验证优先级**：**🔴 P0 级（核心关键项）**
> **对应验证阶段**：**E1（核心数据路径打通与多卡状态同步）**
> **证伪标记**：否（关键执行链确认）
> **建议周期**：4~6 人日
> **主关联 IR**：`IR-01-02`, `IR-01-04`
> **核心 SRS / SR23 锚点**：
> - SRS：`L3-SE-DescriptorFromManifest-079`, `L3-MC-LayoutTransformPlan-078`, `L2-OL-BulkDescriptor-025`, `L2-OL-LayoutNegotiation-024`
> - SR23：`SR23-01-02-01`, `SR23-01-04-01`, `SR23-02-06-01`
> **配套源码**：[`提前验证方案设计/验证计划方案设计/原型验证代码/PVT-02/`](file:///d:/codes/reports/kvcache/unified_kv_memory/提前验证方案设计/验证计划方案设计/原型验证代码/PVT-02)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`；vLLM `842dd8fd96650063e1ad32e6075742d457d39773`。正式结果必须以现场代码包、框架版本、设备和配置哈希为准。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 描述符是什么：从物流清单到硬件 DMA 指令

把一段长 Prompt 的 KVCache 想成分散在很多货架上的货物。若每个 Block 都单独发起一次搬运，CPU 需要反复准备地址、长度和提交通知；硬件队列会收到大量小任务，控制面开销随 Block 数量增长。

描述符就是一张只记录搬运所需元数据的清单：

```text
源物理地址 → 目的物理地址 → 连续字节长度 → Stream/队列 → 控制标志
```

它不包含 KVCache 正文，不负责保存 Python 对象，也不等同于一个动态 `std::vector`。真正的硬件执行条目必须是固定字段、可验证长度和地址范围的结构体；宿主侧的容器只负责暂存编译结果，不能直接当作跨进程 wire layout。

### 0.2 为什么需要描述符编译器

PagedAttention（分页注意力，即把长序列 KVCache 切成可独立管理的物理块）或其他显存池管理方式，会让逻辑连续的 Token 序列映射到离散的物理块。连续 Prompt 可能对应几十到数千个 Block。

如果直接把这些 Block 逐个提交，开销包括：

1. 框架侧把 BlockTable、Radix Tree 或 Span 转换成传输接口格式；
2. 跨进程传输地址、长度和布局元数据；
3. 为每个离散块创建或提交硬件 SG 条目；
4. 等待大量完成事件并处理边界错误。

描述符编译器的目标是把“逻辑块数组”转换为“硬件可以批量执行的区间数组”，并尽量合并源地址和目的地址同时连续的相邻块：

```text
离散输入： [S0→D0][S1→D1][S2→D2][断开][S3→D3]
连续合并： [S0→D0, 长度 L0+L1+L2] [S3→D3, 长度 L3]
```

合并后的条目数、编译时间、边界正确性和真实设备提交次数必须从原始输入和设备事件测量，不能预先写成“压缩到几十条”或固定比例。

### 0.3 异构 Layout 与跨框架适配

vLLM、SGLang 和其他推理框架可能使用不同的逻辑索引、Block 大小、层序、Head 布局和物理地址句柄。适配器的职责是把各自的逻辑结构转换成统一的 `ExtentManifest`，而不是把逻辑 Block ID 直接当成物理地址。

```text
vLLM BlockTable ─┐
SGLang Radix/Span ─┼─► 统一 ExtentManifest ─► 描述符编译器 ─► 硬件 SG 条目
现场分配器句柄 ───┘
```

适配器必须验证：逻辑 Token 区间、物理地址、块长度、数据类型、层数、TP 切分、对齐和权限之间的一致性。只要布局元数据缺失，后续的合并条数和传输正确性就无法解释。

### 0.4 异步 DAG 的物理原理

如果计算流必须等所有 KV 数据传输完成后才开始，计算和数据搬运是串行关系：

```text
串行：    [计算全部层] ─────────► [传输全部层] ─────────► [下一阶段]
```

分层流水的目标是建立真实事件依赖，使已完成的 Layer 数据在满足消费条件后立即提交传输，同时计算流继续处理下一层：

```text
计算流：  [Compute L0] ─► [Compute L1] ─► [Compute L2] ─► ...
              │               │               │
传输流：      └─[DMA L0]──────┴─[DMA L1]──────┴─[DMA L2]──► ...
```

“可以同时提交”不等于“已经重叠”。只有设备 Stream、Event、完成时间线和真实数据依赖都被采集，才能计算重叠率；使用两个浮点数代入公式只能验证数学关系。

### 0.5 当前配套工程能够证明什么，不能证明什么

| 子实验 | 当前源码能够完成的动作 | 当前源码不能直接证明的内容 | 当前默认证据 |
|---|---|---|---|
| 合并逻辑 | 对内部 `LogicalBlock` 数组按源/目的地址连续性做 O(N) 扫描和合并 | vLLM/SGLang 真实布局、物理地址有效性、目标数据一致性 | `DEMO / W0` |
| Manifest 生成 | 生成固定块数、碎片率和十六进制地址的 JSON | 当前 C++ benchmark 会读取该 JSON；真实框架 BlockTable/Span | `DEMO` |
| 编译时延 | 对内部模拟数组重复调用 `compile_manifest`，输出 P50/P95/P99 | 真实跨进程序列化成本、设备描述符提交成本、CPU 降低幅度 | `DEMO / W0` |
| DAG 时间 | 用 `compute_ms`、`dma_ms`、`chunks` 计算串行与理想流水公式 | CANN/NPU 双 Stream、Event 依赖、真实 DMA 完成和端到端 TTFT | `DEMO / W0` |

---

## 1. 验证目标与交付结论定义

### 1.1 待验证核心命题

1. **命题一：源/目的布局连续性可以被安全合并**。对跨框架输入，编译器只在源地址和目的地址同时连续、长度和权限合法时合并；不连续、长度不匹配或输入数量不一致时必须保留边界或返回明确错误。
2. **命题二：批量描述符可以减少控制面提交数量**。在相同逻辑 Block 输入下，对比逐块提交和合并后提交的条目数、编译时延、提交次数和 CPU 控制开销；候选目标是条目数减少 `>=50%`、提交控制耗时减少 `>=40%`，实际门槛必须运行前冻结。
3. **命题三：固定字段协议能跨进程传递元数据**。wire header、SG entry 和数组长度必须有明确版本、字节大小、端序、对齐和边界检查；宿主容器不能被误当成可直接共享的 POD。
4. **命题四：计算与传输能够形成真实异步流水**。在实际 NPU Stream/Event 和真实传输完成事件存在时，流水端到端耗时相对串行基线出现可重复降低；候选重叠率为 `>=60%`，不代表当前模拟公式已满足。

### 1.2 交付物与结论边界

每个正式 `run_id` 至少交付：

1. 描述符输入、输出条目、合并边界、长度守恒和地址映射的逐样本校验表；
2. 编译 P50/P95/P99、条目压缩率、提交次数和 CPU 控制开销对照表；
3. ExtentManifest wire schema、版本、固定字段大小、端序和跨进程读取测试记录；
4. 真实 Compute/Transfer Timeline、Event 依赖、串行基线和流水耗时；
5. `manifest.json`、`environment.json`、原始输入/输出、日志、摘要和分项 `GO`/`CONDITIONAL`/`NO-GO`/`NOT-SUPPORTED`/`INVALID-EVIDENCE` 结论。

---

## 2. 核心数据结构与算法设计

### 2.1 当前工程实际数据结构

当前 `descriptor_compiler.h` 中的结构体为：

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

边界必须明确：当前只有 `WireBatchDescriptorHeader` 通过静态断言固定为 64B；`HardwareSGEntry` 当前没有 `alignas(64)` 和补齐断言；`CompiledBatch::entries` 是宿主动态容器。不能把整个 `CompiledBatch` 宣称为 64B POD 或直接跨进程发送。

### 2.2 目标 ExtentManifest wire 协议

正式跨框架协议可采用如下目标结构，但字段和大小必须以最终实现为准：

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

这是设计目标，不是当前工程已有类型。进入生产实现前必须补充 `static_assert(sizeof(...))`、端序、版本兼容、地址权限、数组边界和校验字段。`/dev/shm`（Linux 共享内存文件系统）或其他共享队列只传元数据时，也要记录进程间生命周期、权限和可见性事件。

### 2.3 跨框架 Layout 适配器

适配器只转换逻辑元数据，不复制 KVCache 正文。示意接口如下：

```cpp
// 伪代码：block_id 到真实物理地址的映射必须由现场分配器提供。
void adapt_vllm_block_table(const VllmBlockTable& table,
                            const RuntimeLayout& layout,
                            std::vector<LogicalBlockExtent>& out);

void adapt_sglang_radix_spans(const SglangRadixSpans& spans,
                              const RuntimeLayout& layout,
                              std::vector<LogicalBlockExtent>& out);
```

适配器的最小校验包括：

- `token_count > 0`，`block_bytes` 与数据类型/Head/层布局一致；
- `phys_base_addr` 是现场分配器提供的有效设备地址或句柄，不能由 `block_id × bytes_per_block` 直接臆造；
- 源/目的条目数量与逻辑顺序一致；
- 地址、长度、stride 和权限满足硬件对齐要求；
- `layout_hash`、模型架构、Tokenizer、TP 切分和代码包能回指同一 workload。

### 2.4 当前贪心合并算法与边界

当前 `DescriptorCompiler::compile_manifest` 对 `source` 和 `target` 做单次扫描：

```cpp
const bool contiguous =
    source[i].phys_addr == current.src_phys_addr + current.len_bytes &&
    target[i].phys_addr == current.dst_phys_addr + current.len_bytes;
```

若同时连续则累加当前条目长度，否则先压入当前条目，再开始新条目。算法复杂度为 O(N)，并把源块总长度写入 `header.total_bytes`。

当前实现还存在以下必须记录的边界：

1. 空数组或源/目的数量不一致时返回空 batch，没有错误码或 `invalid_reason`；
2. 没有验证源块和目的块的 `size_bytes` 是否一致，也没有验证地址溢出、零长度或权限；
3. `header.manifest_id` 未由函数参数设置，`flags` 和 entry flags 不是完整的硬件协议语义；
4. 合并只依据输入数组相邻顺序，不会排序，也不会跨越逻辑边界合并；
5. 当前没有输出每个输入块对应的输出条目、长度守恒校验或设备执行结果。

所以，当前算法可以作为合并流程 DEMO，不能直接证明跨框架数据正确性或硬件传输安全。

### 2.5 DAG 时间模型与真实事件要求

当前 `async_dag_bench.cc` 使用：

$$
T_{serial}=(T_{compute}+T_{DMA})\times chunks
$$

$$
T_{pipeline}=T_{DMA}+\max(T_{compute},T_{DMA})\times(chunks-1)+T_{compute}
$$

并将脚本计算出的 `overlap_pct` 写入 CSV。该计算没有调用设备 Stream 或 Event，`compute_ms`、`dma_ms` 是命令行输入。因此正式重叠率必须基于真实时间戳：

```text
compute_start/end(layer_i)
transfer_submit/complete(layer_i)
event_record/wait(layer_i)
consumer_ready(layer_i)
```

只有这些事件能够证明计算和传输的实际交叠、等待和依赖关系。

---

## 3. 实验方案与测试矩阵设计

### 3.1 两组子实验矩阵

| 子实验 | 正式输入 | 核心观测 | 当前源码覆盖 |
|---|---|---|---|
| 描述符编译与正确性 | Block 数 16、64、256、1024；碎片率 0%、10%、50%、90%、100%；源/目的地址、块大小和布局哈希 | 输出 SG 条目数、压缩率、长度守恒、源/目的连续性、编译 P50/P95/P99、错误处理 | 内部模拟 `LogicalBlock`；无 JSON 读取、无框架适配和设备校验 |
| 异步 DAG 流水 | 层数/Chunk 数、真实计算时间、真实 DMA 时间、Stream/Event 依赖和同场次串行基线 | 串行/流水耗时、重叠率、等待时间、首字延迟、错误/超时 | 只有公式演示；没有 CANN/NPU/URMA/UBMEM |

### 3.2 参数矩阵与当前 CLI

| 参数 | 正式计划 | 当前工程实际支持 |
|---|---|---|
| Block 数 | 16、64、256、1024、4096 | `async_dag_bench --block-count` 单值；`make_manifests.py --block-count` 单值 |
| 碎片率 | 0、0.1、0.5、0.9、1.0 | 两个工具均支持一个浮点值；没有批量 ratios 参数 |
| Chunk 数 | 1、2、4、8、16、32 | `async_dag_bench --chunks` 支持 |
| 编译循环 | 运行前冻结，建议至少 1000 次并保留原始时间 | `--loops` 支持；默认 1000 |
| 计算/DMA 时间 | 来自设备 Timeline | 当前 `--compute-ms`、`--dma-ms` 只是公式输入 |
| 框架 | vLLM、SGLang、现场框架 | 当前没有适配器和真实框架输入 |

### 3.3 环境与证据矩阵

| 环境 | 目的 | 最低条件 | 允许形成的结论 |
|---|---|---|---|
| W0 单机模拟 | 验证编译器条目合并、字段和公式 | C++ 编译器、Python | `DEMO`：算法/格式流程可运行 |
| W1 局部设备实验 | 验证真实布局适配或单卡 Stream/Event | 现场框架、设备、驱动、Profiler | `LAB`：绑定版本和设备的局部结论 |
| W2 端到端实验 | 验证跨节点描述符执行与分层流水 | 真实 NPU、网络 DMA、框架端点、Timeline 和重复实验 | 证据闭环后形成 `MEASURED` 结论 |

### 3.4 公平 A/B 要求

编译器 A/B 要保持输入 Block 顺序、地址布局、块大小、编译循环、CPU 绑核和内存分配一致，只改变逐块提交与合并提交策略。DAG A/B 要保持模型、层数、Token 数、计算负载、DMA payload、设备、Stream 优先级和资源配额一致，只改变同步/流水策略。不能用不同 `compute_ms`/`dma_ms` 的公式输入制造重叠收益。

---

## 4. 测试工具、当前实现边界与正式扩展要求

### 4.1 当前受控工程目录与运行方式

```text
原型验证代码/PVT-02/
├── Makefile
├── descriptor_compiler.h
├── descriptor_compiler.cc
├── async_dag_bench.cc
└── make_manifests.py
```

当前可复现的 W0 命令：

```bash
cd ./原型验证代码/PVT-02
make clean
make
python3 ./make_manifests.py --block-count 1024 --frag 0.5 --out manifest_1024_frag0.5.json
./async_dag_bench --block-count 1024 --fragmentation 0.5 \
    --chunks 16 --compute-ms 300 --dma-ms 160 --loops 1000 \
    --out res_compiler_dag_demo.csv
```

`make_manifests.py` 的 JSON 当前没有被 `async_dag_bench` 读取；benchmark 内部重新生成 `mock_blocks`。当前工程没有 `--tokens`、`--block-size`、`--fragmentation-ratios`、`--out-dir`、`--layers`、`--evidence-level` 参数，也没有 `descriptor_bench` 或 `eval_descriptor_pipeline.py`。

### 4.2 源码实际行为审计

| 代码路径 | 实际行为 | 对证据的影响 |
|---|---|---|
| `make_manifests.py` | 用固定随机种子生成 `block_count` 个 64KB 块，按 `--frag` 概率跳跃地址，输出 `block_count`、`fragmentation_ratio` 和块数组 | 可生成合成输入；不是 vLLM/SGLang 布局，也没有被当前 C++ benchmark 读取 |
| `async_dag_bench.cc::mock_blocks` | 运行时内部生成 source/target 模拟数组，默认 1024 块、碎片率 0.5 | 与外部 manifest 脱钩，不能证明 JSON 输入已参与编译 |
| `descriptor_compiler.cc` | 对 source/target 相邻块做源/目的地址连续性合并；不校验长度、溢出、权限和完整映射 | 可验证基础合并逻辑，不能关闭框架适配和数据正确性 |
| `async_dag_bench.cc` 编译循环 | 真实调用 `compile_manifest`，输出编译 P50/P95/P99、SG 条目数和压缩率 | 编译时延是本地模拟数组时延，状态固定为 `DEMO,DEMO_ONLY` |
| `async_dag_bench.cc` DAG 计算 | 用输入的 `compute_ms`、`dma_ms` 和 `chunks` 计算 `serial_ms`、`dag_ms`、`overlap_pct` | 没有真实计算、DMA、Stream、Event 或 Timeline；只能作公式 DEMO |
| `HardwareSGEntry` | 当前字段为源地址、目的地址、长度和 flags，没有 `alignas(64)` | 不能宣称 SG entry 已满足 64B 硬件 wire layout |
| `CompiledBatch` | header 固定字段加 `std::vector` 动态条目 | 只能作 Host 容器，不可直接跨进程按固定字节发送 |
| 输出 | CSV 固定写 `DEMO,DEMO_ONLY`，stdout 明确说明 DAG overlap 是公式 | 任何输出数值都不能关闭 E1 真实路径结论 |

### 4.3 面向 LAB/MEASURED 的最小工程扩展

1. **Manifest 接入**：让 C++ benchmark 读取 `make_manifests.py` 或真实框架导出的 JSON，校验 schema、块数、地址、长度、布局哈希和源/目的对应关系；
2. **跨框架适配器**：分别实现 vLLM BlockTable、SGLang Radix/Span 与统一 ExtentManifest 的转换，禁止将逻辑 Block ID 当物理地址；
3. **安全合并器**：增加零长度、整数溢出、源/目的长度不等、地址对齐、权限和逻辑边界校验，输出错误码和每个输入块的映射；
4. **固定 wire layout**：为 header、Extent、SG entry 增加显式大小、对齐、端序和 `static_assert`，将宿主动态容器与跨进程数组分离；
5. **真实硬件提交**：把合并后的 SG entry 交给现场 URMA/UBMEM 或 DMA 驱动，记录提交次数、设备完成、失败和 Fence；
6. **真实 DAG**：使用现场 NPU 的 Compute/Transfer Stream 与 Event 接口，记录每层计算、传输、等待和消费事件，不能继续用浮点输入代替 Timeline；
7. **串行基线**：同一 workload 下运行全量计算/全量传输串行模式，记录同场次 TTFT 和资源占用；
8. **统计与证据包**：保留原始地址、条目、时间戳、失败、版本、拓扑、Profiler Trace、配置哈希和重复轮次；
9. **状态归一化**：把当前 `DEMO_ONLY` 和内部错误映射为公共契约状态，缺失字段使用 `null` 并填写 `invalid_reason`。

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结实验身份、输入和证据等级

- **操作意图**：明确本轮是 W0/DEMO、W1/LAB 还是 W2/MEASURED，避免把模拟块或公式结果写成真实硬件结论。
- **执行动作**：填写 `run_id`、`workload_schema_version`、`workload_id`、`package_id`、`baseline_commit`、`config_hash`、框架、模型布局、Block 数、碎片率、块大小、Chunk 数、计算/DMA 负载、设备、拓扑、预热、重复轮次和门槛。
- **应观察现象**：能明确列出输入来自外部 manifest 还是 benchmark 内部模拟，DAG 时间来自设备事件还是公式；若没有真实框架或设备事件，提前标记 `NOT-SUPPORTED`。

### 步骤 1：生成合成 Manifest 并记录它尚未接入 C++ benchmark

- **操作意图**：生成可复现的地址碎片输入，先验证 JSON 文件和参数构造流程，同时明确它不会自动进入当前 C++ 编译器。
- **执行命令**：

```bash
cd ./原型验证代码/PVT-02
python3 ./make_manifests.py --block-count 1024 --frag 0.5 --out manifest_1024_frag0.5.json
```

- **应观察现象**：JSON 中有 1024 个块、`fragmentation_ratio=0.5` 和十六进制 `phys_addr`；文件可解析、块数量与 header 一致。
- **证据边界**：当前 `async_dag_bench` 不读取该文件。若要声称“该 manifest 已被编译”，必须先实现第 4.3 节接入并在日志中记录输入文件哈希。

### 步骤 2：编译并运行当前描述符合并 W0 基线

- **操作意图**：验证当前 `compile_manifest` 的连续性扫描、条目数量、编译时延和 CSV 输出，建立后续安全合并器的回归基线。
- **执行命令**：

```bash
make clean
make
./async_dag_bench --block-count 1024 --fragmentation 0.5 \
    --chunks 16 --compute-ms 300 --dma-ms 160 --loops 1000 \
    --out res_compiler_dag_demo.csv > compiler_dag_stdout.txt 2>&1
```

- **应观察现象**：CSV 含 `sg_entries`、编译分位数、`compression_pct`、串行/公式 DAG 时间和 `DEMO,DEMO_ONLY`；stdout 明确说明 DAG overlap 是公式。
- **判定边界**：当前 benchmark 没有对逐块基线、长度守恒、地址映射和输出数据做完整校验，不能仅凭 `compression_pct` 判定编译器通过。

### 步骤 3：执行描述符正确性和边界测试（条件扩展）

- **前置条件**：已将 JSON/框架输入接入编译器，并补齐源/目的长度、地址溢出、对齐、权限、空数组和数量不一致校验。
- **操作意图**：确认每一个输入 Block 恰好归属于一个输出 SG 条目，输出总字节守恒，连续合并不会越过逻辑层/权限边界。
- **执行动作**：对碎片率 0%、10%、50%、90%、100% 和非法输入分别运行；保存输入 manifest、输出条目、错误码和逐条映射。
- **应观察现象**：合法输入通过长度/地址/边界校验；非法输入显式失败并写 `invalid_reason`；不能用空 batch 当作成功。

### 步骤 4：运行当前 DAG 公式基线

- **操作意图**：验证 `serial_ms`、`dag_ms` 和 CSV 字段计算流程，为后续真实设备 Timeline 的解析器提供格式参考。
- **执行命令**：

```bash
./async_dag_bench --block-count 1024 --fragmentation 0.5 \
    --chunks 16 --compute-ms 300 --dma-ms 160 --loops 1000 \
    --out res_dag_formula_demo.csv
```

- **应观察现象**：输出的 `dag_ms` 由命令行输入计算得到；不存在 NPU 事件、Stream 句柄、DMA 完成或真实层级时间戳。
- **判定边界**：本步骤只能标 `DEMO`。不能将 `overlap_pct` 写成真实计算—传输重叠率。

### 步骤 5：接入真实框架 Layout 和硬件 Stream/Event（条件步骤）

- **前置条件**：现场 vLLM/SGLang Layout、设备地址/句柄、硬件 DMA、NPU Compute/Transfer Stream、Event 和 Profiler 均可用。
- **操作意图**：在同一 workload 下建立“框架布局 → ExtentManifest → SG 提交 → 设备完成 → 消费就绪”的全链路事件关系。
- **执行动作**：记录框架适配输入哈希、manifest 版本、源/目的地址、每个 SG 条目、提交/完成时间、Fence、Stream/Event 依赖和错误码；同时运行串行基线。
- **应观察现象**：每层数据都有可回指的计算/传输事件；若适配器或设备能力缺失，标 `NOT-SUPPORTED`，不要回退模拟时间并沿用 `MEASURED`。

### 步骤 6：计算压缩率、控制开销和真实重叠率

- **操作意图**：从原始条目和设备 Timeline 计算三类指标，不让公式输入或平均值替代原始事件。
- **计算口径**：

```text
描述符压缩率 = 1 - 合并后 SG 条目数 / 原始离散 Block 数
实际流水收益 = 串行基线总时长 - 流水总时长
重叠率       = 由实际 Compute/Transfer 时间区间与等待区间定义并在 manifest 冻结
```

- **应观察现象**：每个结果能回指输入、设备事件和代码包；没有 Timeline 时，重叠率使用 `null` 并填 `invalid_reason`。

### 步骤 7：生成标准证据包并重复实验对账

- **操作意图**：保存合并正确性、编译开销、Timeline、失败和环境证据，支持后续复核和架构决策。
- **执行动作**：按 `results/PVT-02/<subtest>/<run_id>/` 建目录，保存 `manifest.json`、`environment.json`、原始 manifest、原始条目、CSV、Profiler Trace、日志、摘要和错误记录；每个条件至少 3 次独立重复。
- **应观察现象**：`planned_path`、`actual_path`、`evidence_level`、`status` 和 `invalid_reason` 齐全；不能把模拟地址或公式结果填入 MEASURED。

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

字段约束：

- `src_phys_addr`/`dst_phys_addr` 必须来自真实分配器或明确的 DEMO 生成器；
- `total_input_bytes == total_output_bytes` 只有在逐项长度校验通过时才可填写；
- `merged_sg_entries` 不能由压缩率公式倒推；
- 空输入、数量不一致、溢出和地址非法使用 `null`/错误状态，不用 0 伪装成功。

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

当前公式 DEMO 没有这些设备事件，真实字段必须填写 `null` 并说明 `formula_only_demo`。

### 6.3 汇总 CSV 模板

```csv
validation_id,run_id,subtest,framework,workload_id,layout_hash,block_count,fragmentation_ratio,raw_blocks,merged_sg_entries,compression_pct,compile_p50_us,compile_p95_us,compile_p99_us,chunks,serial_ms,pipeline_ms,overlap_pct,actual_completed_bytes,evidence_level,status,invalid_reason
<PVT-02>,<run_id>,<compile_or_dag>,<vllm_or_sglang_or_simulation>,<workload_id>,<layout_hash_or_null>,<count>,<ratio>,<count_or_null>,<count_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<chunks>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<DEMO_OR_LAB_OR_MEASURED>,<status>,<null_or_reason>
```

### 6.4 证据包目录

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

`manifest.json` 至少记录输入 Schema、框架/模型布局、代码包、基线 Commit、配置哈希、设备、拓扑、执行命令、原始文件哈希、证据等级、支持范围、未支持项和结论状态。

---

## 7. GO / CONDITIONAL / NO-GO / NOT-SUPPORTED / INVALID-EVIDENCE 判定规则

### 7.1 描述符合并与跨框架适配

- **GO（正确性与控制面收益闭环）**：跨框架输入、固定 wire schema、源/目的地址、长度守恒、边界错误和设备完成事件完整；在运行前冻结的典型碎片率下，描述符压缩率达到 `>=50%`，控制提交耗时达到候选降低 `>=40%`，且没有数据错位或资源泄漏。
- **CONDITIONAL（局部支持）**：只在模拟输入或单一框架/设备上通过，或只确认条目压缩未确认设备执行；结论绑定已测条件。
- **NOT-SUPPORTED**：没有真实框架 Layout、硬件地址/句柄、目标 DMA 或跨进程 wire 验证；当前 W0 输出属于此类边界。
- **NO-GO（真实错误或收益不足）**：有效测试确认长度/地址错误、数据错位、崩溃、泄漏，或达到不了运行前冻结的压缩/控制开销门槛。

### 7.2 异步 DAG 流水

- **GO（真实 Timeline 闭环）**：存在真实 Compute/Transfer Stream、Event、设备完成和串行基线；流水总时长相对串行基线稳定降低，候选重叠率达到 `>=60%`，并且首字延迟、错误、超时和资源占用没有不可接受退化。
- **CONDITIONAL（公式或局部 Timeline）**：只有 W0 公式趋势，或只有局部 Stream 事件；只能说明模型/流程可行，不能关闭生产级重叠结论。
- **NOT-SUPPORTED**：现场没有 NPU Event、真实 DMA 或 Profiler 时间线。
- **NO-GO（流水没有净收益）**：真实 A/B 中流水总时长未降低，或事件同步、超时、错误和资源占用抵消了计算—传输重叠收益。

### 7.3 统一无效证据规则

以下任一情况将对应子实验标为 `INVALID-EVIDENCE`，不得输出 `GO`：

- 使用 `make_manifests.py` 生成的文件，却没有证明 C++ benchmark 实际读取该文件；
- 把当前 `compute_ms`/`dma_ms` 公式或 `DEMO_ONLY` 输出写成真实 NPU/DMA Timeline；
- 把 `std::vector` 宿主容器或未断言大小的 SG entry 写成固定 wire POD；
- 逻辑 Block ID 被直接当作物理地址，或源/目的长度、权限、对齐和边界未校验；
- 缺少原始输入、输出条目、长度守恒、设备完成、Profiler Trace、版本、拓扑或重复实验；
- A/B 改变了输入布局、计算/DMA 负载、设备、Stream 配置或统计口径；
- 缺失字段用 0 填充，或将模拟压缩率、公式重叠率直接写成 MEASURED。

---

## 8. 执行阶段与交付闭环

| 阶段 | 工作内容 | 必须交付 | 退出条件 |
|---|---|---|---|
| 阶段 A：结构与 W0 回归 | 审计当前 header、合并算法、模拟 manifest 和公式 DAG | 源码审计、编译 CSV、公式 CSV、边界清单、`DEMO` manifest | 当前支持项与未支持项明确 |
| 阶段 B：框架适配与设备执行 | 接入 vLLM/SGLang Layout、真实地址/句柄、DMA 和 Stream/Event | 原始映射、SG 条目、设备完成、Profiler Trace、重复实验 | 数据和时间线均可回指原始事件 |
| 阶段 C：对账与准入 | 与逐块/串行基线比较，输出压缩、控制开销和流水收益 | 能力摘要、A/B 对账、结论状态和无效证据说明 | 公共契约通过，不把模拟结果冒充硬件结论 |

本项为后续异构框架接入、异步传输和描述符批量提交提供底层验证依据；它不能用“条目数变少”单独证明数据正确，也不能用公式重叠率替代真实设备时间线。

---

## 9. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师负责**：
  1. 确认框架版本、模型 Layout、设备地址/句柄、DMA 驱动、NPU Stream/Event 和 Profiler 权限；
  2. 冻结 Block 数、碎片率、块大小、Chunk 数、计算/DMA 负载、A/B 策略和证据等级；
  3. 保存原始 manifest、地址映射、设备完成、Timeline 和失败事件；
  4. 判断某个地址、长度、完成量和时间戳是否来自真实设备，而不是模拟器或公式；
  5. 对跨框架适配和生产路径结论进行现场复核。
- **AI Agent 负责**：
  1. 先阅读方案和五个实际源码文件，列出真实 CLI、结构体大小、算法边界和未实现功能；
  2. 编写 manifest 解析、长度/地址校验、合并回归、分位数计算和证据包工具；
  3. 检查 wire layout、源/目的连续性、压缩率、串行/流水时间和状态归一化；
  4. 不凭空创建 `descriptor_bench`、`eval_descriptor_pipeline.py`、CANN Event 结果或固定压缩率。

### 9.2 可直接复制给 Coding Agent 的 Prompt 模板

```text
我正在执行 PVT-02：异构框架 Layout 描述符编译器与异步 DAG 流水验证。

请先阅读：
1. ./提前验证方案设计/验证计划方案设计/03_PVT-02_异构框架Layout描述符编译器与异步DAG流水验证实施方案设计.md
2. ./提前验证方案设计/验证计划方案设计/Benchmark公共契约与证据分级规范.md
3. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-02/Makefile
4. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-02/descriptor_compiler.h
5. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-02/descriptor_compiler.cc
6. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-02/async_dag_bench.cc
7. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-02/make_manifests.py

约束：
- 先列出真实 CLI 和源码行为；确认 make_manifests.py 的 JSON 当前没有被 async_dag_bench 读取，且工程不存在 descriptor_bench/eval_descriptor_pipeline.py。
- 确认 WireBatchDescriptorHeader 才有 64B static_assert，HardwareSGEntry 没有固定 64B 断言，CompiledBatch 含 std::vector 不能当作 wire POD。
- 对 compile_manifest 检查空输入、数量不一致、源/目的连续性、长度守恒、地址溢出和错误状态；不要用空 batch 当成功。
- 当前 DAG 只有 compute_ms/dma_ms 公式，必须标 DEMO；真实重叠率只能来自 Compute/Transfer Stream、Event 和设备完成时间线。
- 设计时将 vLLM/SGLang BlockTable/Span、ExtentManifest、SG 条目和实际设备路径分开；逻辑 Block ID 不能直接当物理地址。
- 缺失字段使用 null 并填写 invalid_reason；保留原始 manifest、映射、失败和 Profiler Trace；不填固定示例数据。
- 最后输出：源码能力矩阵、实际命令、数据结构审计、合并回归结果、DAG 证据边界、未支持项和下一步最小代码改动建议。
```

### 9.3 常见排错指南

- **执行 `make_manifests.py` 后 benchmark 结果没有变化**：这是当前工程的真实边界，C++ 程序内部重新生成 `mock_blocks`；先实现文件读取并记录 manifest 哈希，不能声称外部输入生效。
- **合并条目数异常少**：检查源/目的地址是否同时连续、块大小是否一致、是否误把模拟地址或排序后的数组当成真实布局；保留逐块映射和长度守恒结果。
- **源地址连续但目的地址不连续**：不能合并；合并条件必须同时满足源、目的连续，不能只看一侧。
- **空输入或源/目的数量不同**：当前函数返回空 batch 且没有错误码；正式实现应显式返回 `INVALID-EVIDENCE`/输入错误并写原因。
- **出现地址溢出或长度异常**：补充整数溢出、零长度、对齐和权限校验；不要让硬件条目带着未经验证的地址提交。
- **想运行 `descriptor_bench` 或 `eval_descriptor_pipeline.py`**：当前受控目录没有这些文件；先基于现有 `async_dag_bench` 扩展，不能把不存在的命令写入结果。
- **DAG 重叠率很高但没有 NPU 时间线**：它只是公式输入带来的结果；标 `DEMO`，等待真实 Stream/Event/Profiler 证据。
- **跨进程读取出现结构体大小不一致**：检查编译器 ABI、`alignas`、端序和 `static_assert`；宿主 `std::vector` 不能直接写入共享内存协议。
- **框架 Block ID 到物理地址映射失败**：向现场分配器获取真实物理句柄/地址和权限，不能用 `block_id × block_size` 代替。
- **流水运行超时或死锁**：先画出每个 Event 的 record/wait 依赖，确认 Compute 和 Transfer Stream 属于同一设备上下文；缺少真实事件时不要通过调整公式掩盖依赖错误。
