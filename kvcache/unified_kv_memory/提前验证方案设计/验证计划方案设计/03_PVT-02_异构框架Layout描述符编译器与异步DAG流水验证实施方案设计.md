# PVT-02：异构框架 Layout 描述符编译器与异步 DAG 流水验证实施方案设计
## —— Mooncake 离散 Block 传输协议重构：连续块贪心合并与异步 Stream 重叠

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。描述符正确性/编译时延与真实计算—传输重叠分成两组测试；只有固定字段的 wire header 和 SG entry 可以声明为 POD，含 `std::vector` 的宿主容器不作 POD 承诺。

> **验证 ID**：PVT-02  
> **验证名称**：异构框架内存布局 (Layout) 描述符编译器与异步有向无环图 (DAG) 流水验证  
> **验证优先级**：**🔴 P0 级（核心关键项）**  
> **对应验证阶段**：**E1 核心数据路径打通**  
> **证伪标记**：否（关键执行链确认）  
> **主关联 IR**：`IR-01-02`, `IR-01-04`  
> **核心 SRS / SR23 锚点**：  
> - SRS: `L3-SE-DescriptorFromManifest-079`, `L3-MC-LayoutTransformPlan-078`, `L2-OL-BulkDescriptor-025`, `L2-OL-LayoutNegotiation-024`  
> - SR23: `SR23-01-02-01`, `SR23-01-04-01`, `SR23-02-06-01`  
> **开源基线版本与代码仓库**：  
> - **Mooncake**：[`https://github.com/kvcache-ai/Mooncake.git`](https://github.com/kvcache-ai/Mooncake.git) (Commit: `f90ae691f109e49a60920e0c8abbf7e572826d8c`，子模块: `mooncake-transfer-engine/`, `mooncake-integration/`)  
> - **vLLM**：[`https://github.com/vllm-project/vllm.git`](https://github.com/vllm-project/vllm.git) (Commit: `842dd8fd96650063e1ad32e6075742d457d39773`，模块: `vllm/core/block_manager_v1.py`)  
> **研发对齐状态**：本方案将复核研发评估报告涉及的共享内存 64B POD 协议、vLLM/SGLang 适配器与 CANN Stream 驱动，并以实际编译和时间线为准。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 到底什么是“描述符 (Descriptor)”？（通俗类比与系统底层本质）

对于刚接触 AI 推理框架与硬件加速的工程师来说，“描述符”这个词往往令人感到抽象。下面从生活中的物流系统和计算机体系结构两个层次建立直观理解：

#### 1. 生活中的通俗类比：快递货运的“发货提单 (Shipping Manifest)”
- 假设你要搬迁一个巨大的图书馆，里面有 2,000 本书（对应 2,000 个 KVCache 数据块）。这些书分散在仓库各个不同的货架上。
- **低效的做法**：你每找到一本书，就单独叫一辆货车跑一趟。结果货车在路上来回跑了 2,000 趟，光是司机登记进出大门（CPU 门铃与中断开销）就把整个物流中心搞瘫痪了。
- **高效的做法**：调度员站在货架前，写下一张极简的**“发货清单（描述符清单）”**。清单上只记录纯粹的地址指引：“请从 1 号货架的 0x1000 位置起，连续搬运 50 本书，放到目的地 5 号货架的 0x8000 位置；再从 2 号货架 0x3000 位置起，搬运 30 本书……”。这张**不包含书本正文、仅包含数据源物理地址、目的物理地址、搬运长度和控制标志的几十字节元数据小卡片**，就是**描述符（Descriptor）**！
- 货车司机（硬件 DMA 控制器）拿到这张清单后，无需任何人工干预，直接按照清单上的物理地址一次性把所有包裹自动搬运完毕。

#### 2. 操作系统与硬件体系结构视角：Scatter-Gather DMA 描述符
- 在现代操作系统（如 Linux 网络协议栈 `sk_buff`）和硬件 DMA（Direct Memory Access）引擎中，硬件无法直接理解高级语言的对象、指针树或哈希表。
- 硬件 DMA 控制器本质上是一个只认物理地址的专用协处理器。当需要从不连续的多段内存中读取数据并写入到另一组不连续的内存时，驱动程序会在内存中构建一个数组，数组的每个元素被称为一个 **Scatter-Gather Entry（分散-聚集描述符条目）**：

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│              硬件 Scatter-Gather 描述符条目 (HardwareSGEntry, 64 字节)          │
├──────────────────┬──────────────────┬─────────────────┬───────────┬─────────────┤
│ src_phys_addr    │ dst_phys_addr    │ len_bytes       │ stream_id │ flags       │
│ (源物理显存基址) │ (目的物理基址)   │ (连续传输字节数)│ (流通道ID)│ (控制/栅栏) │
│ 8 Bytes          │ 8 Bytes          │ 4 Bytes         │ 2 Bytes   │ 2 Bytes     │
└──────────────────┴──────────────────┴─────────────────┴───────────┴─────────────┘
```

- 这个结构体就是硬件直接执行搬运的最小指令单元。

---

### 0.2 为什么需要“描述符编译器 (DescriptorCompiler)”？

在大模型分布式推理中，需要重点处理一个常见的工程痛点：

#### 1. 显存分页碎片问题（PagedAttention 带来的离散块）
- 现代大模型推理框架（如 vLLM / SGLang）为降低显存碎片，采用了类似于操作系统虚拟内存分页的机制（PagedAttention），将连续的 Token 序列切分为较小的物理块（Block，每个 Block 的 Token 数量以实际配置为准）。
- 当一个用户的输入提示词（Prompt）达到 32K Token 时，在显存中会被打散成多达 **2,000 个相互独立的物理 Block**。
- 这 2,000 个 Block 在物理显存（HBM）中，可能一部分在物理地址 `0x1000`，一部分在 `0x5000`，还有一部分在 `0x9000`。

#### 2. 开源 Mooncake 的性能瓶颈（Python 序列化与逐块提交）
- 在开源 Mooncake 方案中，当 Prefill 节点要把这 32K 提示词的 KVCache 传输给 Decode 节点时：
  1. Python 代码遍历这 2,000 个 Block 的内存指针，将它们组装成一个巨大的 Python 字典；
  2. 使用 JSON / Pickle / ZMQ 将 Python 字典序列化为字符串并通过网络发送；
  3. 接收端反序列化字符串，然后再一次性向底层的传输引擎发起 2,000 次独立的 DMA 传输请求。
- **需要实测确认的开销风险**：
  - Python 序列化与反序列化会引入额外时延，具体量级必须按冻结代码包、消息大小和现场环境记录；文中的毫秒级数值仅用于说明量纲，不是本项实测结果；
  - 对离散物理块逐项提交描述符会增加 Doorbell Register（网卡/设备提交队列的通知寄存器）次数，可能抬高 Host 控制面开销，CPU 占用与提交时延必须由 PVT-02 的原始样本确认。

#### 3. 软硬件协同方案：C++ 描述符编译器
- 我们在 C++ 底层实现高性能 **描述符编译器（DescriptorCompiler）**，专门解决上述瓶颈：
  - **职责一：跨进程固定字段协议**：摒弃 Python 字典与 JSON，采用 64 字节内存对齐的固定字段结构体（`ExtentManifest`），通过共享内存（`/dev/shm`）传递地址和长度等元数据；避免完整对象序列化，但固定字段处理开销仍需实测；
  - **职责二：贪心连续物理块合并（Greedy Extent Merge）**：编译器在 $O(N)$ 时间内单遍扫描，将物理地址相连的 Block 合并为一个大区间描述符。合并数量、描述符压缩率和 DMA 提交次数必须按真实布局测量，不能预先假定“2,000 个变成几十个”或某个压缩比例；
  - **职责三：编译为硬件执行描述符**：将合并后的区间直接转换为硬件 DMA 网卡能直接读取的 Scatter-Gather 描述符数组，实现微秒级提交。

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        描述符编译器贪心合并示意图 (Greedy Merge)                       │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ 原始 2,000 个离散 Block (每个 16 Token, 5KB):                                          │
│ [Block 0 (0x1000)] [Block 1 (0x2400)] [Block 2 (0x3800)] ... [Block 1999]             │
│   └── 物理地址首尾相连 (0x1000 + 5KB == 0x2400 ...) ──┘                                │
│                                                                                        │
│ 经过 DescriptorCompiler 编译合并后:                                                    │
│ ┌──────────────────────────────────────┬─────────────────────────────────────────────┐ │
│ │ 描述符 1 (Descriptor 1):             │ 描述符 2 (Descriptor 2):                    │ │
│ │ 源: 0x1000, 目的: 0x8000, 长度: 15KB │ 源: 0x6000, 目的: 0xB000, 长度: 10KB        │ │
│ └──────────────────────────────────────┴─────────────────────────────────────────────┘ │
│ 结果：提交条数与 CPU 提交耗时由编译前后同条件实测对账，不预置收益比例。             │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

### 0.3 什么是“异步 DAG 流水 (Async DAG Pipeline)”？（逐层边算边传）

- **传统串行死等模式**：
  - Transformer 大模型通常有 64 ~ 80 层神经网络（Layer 0 ~ Layer 79）。
  - 传统方案中，Prefill 节点必须等全部 80 层的 KVCache 算完并全量传输到 Decode 节点显存后，Decode 节点才开始计算第 0 层。此时网络和算力是完全交替串行等待的，总耗时为 $T_{\text{compute}} + T_{\text{transfer}}$。
- **异步 DAG 逐层边算边传流水**：
  - 利用国产 NPU 提供的**双 Stream 硬件多流机制**（计算流 Compute Stream 与 DMA 传输流 Transfer Stream 独立并发运行）：
  - 当 Prefill 节点算完 Layer 0 时，立刻向 Transfer Stream 派发 Layer 0 描述符开始跨节点网络传输；
  - 与此同时，Compute Stream 毫不等待，立即并发启动 Layer 1 的矩阵乘法计算！
  - **待验证的物理收益**：当计算流与传输流满足依赖条件时，前面的 Layer 数据可以在网络上并行传输；计算-传输重叠率和端到端首字延迟（TTFT）必须由时间线与同场次基线实测确认。

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                   Layerwise 边算边传 (计算与传输异步流水示意)                          │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ 计算流 (Compute Stream):  [ 计算 Layer 0 ] ──► [ 计算 Layer 1 ] ──► [ 计算 Layer 2 ]   │
│                                  │                   │                   │             │
│                                  ▼ 触发 DMA          ▼ 触发 DMA          ▼ 触发 DMA    │
│ 传输流 (DMA Copy Stream):         [ 传输 Layer 0 ] ──► [ 传输 Layer 1 ] ──► [ 传输 L2 ]│
│                                                                                        │
│ 待验证关系：若依赖条件满足，计算与传输可重叠；是否完全掩盖由时间线实测确认。       │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. 验证目标与交付结论定义

### 1.1 现实前因痛点与待验证核心命题
1. **现实痛点**：跨节点传输长上下文时，Python 序列化与离散 Block 逐块提交导致 CPU 产生严重开销，且计算与传输串行等待导致时延翻倍；
2. **核心命题**：
   - **Layout 描述符编译器（DescriptorCompiler）**将跨框架离散物理 Block 编译为硬件 Scatter-Gather 描述符，使**描述符提交数量下降 $\ge 50\%$**，**Host CPU 提交耗时下降 $\ge 40\%$**；
   - **异步 DAG 流水调度引擎**实现 NPU 算力计算流（Compute Stream）与 DMA 传输流（Transfer Stream）的高效重叠，**计算-传输重叠率（Overlap Ratio）达到 $\ge 60\%$**。

### 1.2 最终交付数据与结论产出
1. **《Descriptor 编译器耗时与 Scatter-Gather 压缩率实测表》**（覆盖 16～1024 个离散段）；
2. **《CPU 提交时延基线 vs 批量编译优化对比表》**；
3. **《NPU Compute 与 DMA Transfer 异步流水 Timeline 重叠率分析表》**（附 Profiler Trace）；
4. **《GO / CONDITIONAL / NO-GO / NOT-SUPPORTED / INVALID-EVIDENCE 判定结论》**。

---

## 2. 核心数据结构与跨框架 ExtentManifest 序列化协议

### 2.1 跨进程零拷贝 ExtentManifest 协议标准 (POD 结构体)
为了杜绝 Protobuf/FlatBuffers 序列化引发的微秒级 CPU 开销，跨框架与控制面传递统一采用 **64B 对齐的纯 C POD 共享内存结构体**（挂载于 `/dev/shm/kv_manifest_<req_id>` 或 UBMEM 共享环形队列）：

```cpp
#include <stdint.h>
#include <stddef.h>
#include <vector>
#include <string>

// 1. 单个连续物理 Block Extent 描述 (POD 32 字节)
struct alignas(32) LogicalBlockExtent {
    uint64_t logical_token_start; // 逻辑起始 Token 偏移 (如 0, 16, 32...)
    uint32_t token_count;         // 本段 Token 数量 (如 16 或 128)
    uint32_t stride_bytes;        // 层间或 Head 间跨步 (Stride)
    uint64_t phys_base_addr;      // NPU HBM 或 UBMEM 物理起始地址 (需 64B 对齐)
    uint32_t block_bytes;         // 本块总字节数
    uint32_t reserved;            // 8B 对齐填充
};

// 2. 统一框架清单 Header (固定 64 字节)
struct alignas(64) ExtentManifestHeader {
    uint64_t request_id;
    uint32_t framework_type_id;   // 0: vLLM Paged, 1: SGLang Radix, 2: Standard Extent
    uint32_t total_tokens;
    uint32_t layer_count;
    uint32_t extent_count;        // block_extents 数组长度
    uint64_t total_payload_bytes;
    uint8_t  padding[24];         // 填满 64 字节
};

// 3. 硬件 Scatter-Gather 描述符条目 (直接映射至 URMA / DMA 硬件队列)
struct alignas(64) HardwareSGEntry {
    uint64_t src_phys_addr;       // 源物理地址 (NPU HBM / UBMEM)
    uint64_t dst_phys_addr;       // 目的物理地址 (NPU HBM)
    uint32_t len_bytes;           // 传输连续字节长度
    uint16_t stream_id;           // 绑定 DMA Stream 标识
    uint16_t flags;               // 控制位: 0x01=Notify, 0x02=Fence Barrier, 0x04=LastSegment
};
```

### 2.1.1 批量描述符包头与跨进程边界

`HardwareSGEntry` 是硬件执行条目；批量编译结果还需要一个控制面包头记录批次标识、条目数量、总字节数和完成屏障。包头中的 `entries` 是 Host 侧容器，不属于跨进程固定长度的 wire header，序列化时只传递固定字段和紧随其后的条目数组。

```cpp
struct alignas(64) BatchDescriptorHeader {
    uint32_t batch_id;
    uint32_t total_entries;        // 编译合并后的 SG Entry 数量
    uint64_t total_payload_bytes;  // 本批次总正文字节数
    uint64_t completion_fence_id;  // 完成屏障 Fence ID
    std::vector<HardwareSGEntry> entries; // Host 侧动态容器，不直接作为 wire layout
};
```

### 2.1.2 跨框架内存布局向 ExtentManifest 的转换适配器

不同推理框架的显存布局不能直接把指针或 Python 对象交给 DMA。适配器负责把 vLLM 的 BlockTable、vLLM V1 `KVCacheManager` 以及 SGLang Radix Tree 的连续 Span，转换为统一的 `LogicalBlockExtent` 数组；只传递地址、长度和布局元数据，不复制 KVCache 正文。

```cpp
// vLLM V0/V1 BlockTable 适配器
void adapt_vllm_blocks(const std::vector<uint64_t>& block_ids,
                       uint32_t tokens_per_block,
                       uint32_t bytes_per_block,
                       std::vector<LogicalBlockExtent>& out) {
    out.reserve(block_ids.size());
    for (size_t i = 0; i < block_ids.size(); ++i) {
        LogicalBlockExtent ext{};
        ext.logical_token_start = i * tokens_per_block;
        ext.token_count = tokens_per_block;
        ext.phys_base_addr = block_ids[i] * bytes_per_block;
        ext.stride_bytes = 0;
        ext.block_bytes = bytes_per_block;
        out.push_back(ext);
    }
}

// SGLang Radix Tree 动态连续 Span 适配器
struct SGLangSpan {
    uint64_t token_start;
    uint32_t len;
    uint64_t phys_addr;
    uint32_t bytes;
};

void adapt_sglang_spans(const std::vector<SGLangSpan>& spans,
                        std::vector<LogicalBlockExtent>& out) {
    out.reserve(spans.size());
    for (const auto& sp : spans) {
        LogicalBlockExtent ext{};
        ext.logical_token_start = sp.token_start;
        ext.token_count = sp.len;
        ext.phys_base_addr = sp.phys_addr;
        ext.block_bytes = sp.bytes;
        out.push_back(ext);
    }
}
```

> 适配器只定义转换契约；`block_ids` 到物理地址的映射必须由现场框架分配器提供，不能把逻辑 Block ID 直接当成真实物理地址写入生产路径。

### 2.2 物理连续块贪心合并算法 (Greedy SG Extent Merger)
编译器核心算法在 $O(N)$ 时间复杂度下，一次性遍历输入数组，若发现相邻两个物理块在物理显存地址上是连续的（`src[i].addr == cur.addr + cur.len` 且 `dst[i].addr == cur.dst + cur.len`），则直接合并累加 `len_bytes`，仅在物理断开时才生成新的描述符条目：

```cpp
BatchDescriptorHeader DescriptorCompiler::compile_and_merge(
    const std::vector<LogicalBlockExtent>& src, 
    const std::vector<LogicalBlockExtent>& dst, 
    uint64_t req_id) {
    
    BatchDescriptorHeader batch;
    batch.batch_id = req_id;
    if (src.empty() || src.size() != dst.size()) return batch;

    HardwareSGEntry cur;
    cur.src_phys_addr = src[0].phys_base_addr;
    cur.dst_phys_addr = dst[0].phys_base_addr;
    cur.len_bytes = src[0].block_bytes;
    cur.stream_id = 0;
    cur.flags = 0;

    for (size_t i = 1; i < src.size(); ++i) {
        bool src_contig = (src[i].phys_base_addr == cur.src_phys_addr + cur.len_bytes);
        bool dst_contig = (dst[i].phys_base_addr == cur.dst_phys_addr + cur.len_bytes);

        if (src_contig && dst_contig) {
            cur.len_bytes += src[i].block_bytes; // 连续合并，压缩描述符
        } else {
            batch.entries.push_back(cur);
            cur.src_phys_addr = src[i].phys_base_addr;
            cur.dst_phys_addr = dst[i].phys_base_addr;
            cur.len_bytes = src[i].block_bytes;
            cur.flags = 0;
        }
    }
    cur.flags |= 0x02 | 0x04; // 置位 Fence Barrier 与 LastSegment
    batch.entries.push_back(cur);
    batch.total_entries = batch.entries.size();
    return batch;
}
```

### 2.3 NPU Stream 与 Event 异步 DAG 运行时流水驱动实现
采用双 Stream 解耦流水架构：
- **Stream 0（Compute Stream）**：负责 NPU 算子矩阵乘法计算；
- **Stream 1（Transfer Stream）**：负责底层 UBMEM/URMA DMA 传输；
- 通过 CANN 驱动的 `aclrtRecordEvent` 与 `aclrtStreamWaitEvent` 构建微秒级无锁同步屏障，实现 NPU 算子计算当前层时、DMA 硬件在后台并行传输下一层数据。

---

## 3. 测试工具与工程构建规范

测试工程存放在 `./原型验证代码/PVT-02/` 目录下：

```
原型验证代码/PVT-02/
├── descriptor_compiler.h      # 64B 固定字段 wire header 与动态 SG 宿主容器
├── descriptor_compiler.cc     # 贪心合并与描述符编译核心实现
├── make_manifests.py          # 生成不同显存碎片率 (10%, 50%, 100%) 的测试用例集
├── async_dag_bench.cc         # NPU 双 Stream 异步流水压测 Harness
├── Makefile                   # 编译构建工程 (make -j16)
└── eval_descriptor_pipeline.py# 自动化统计压缩率与重叠率分析脚本
```

编译方法：
```bash
cd ./原型验证代码/PVT-02 && make clean && make -j16
```

### 3.1 单元基准与在线消融对照

先在脱离推理框架的 Harness 中确认编译器的条目数量、编译耗时和地址正确性，再进入 vLLM-Ascend `MooncakeLayerwiseConnector` 在线消融。两种模式必须使用同一模型、同一输入数据集、同一请求率和同一设备拓扑。

```bash
# 单元基准：确认离散块合并与异步 DAG 的基本字段。
./async_dag_bench --block-count 1024 --fragmentation 0.5 \
    --chunks 16 --compute-ms <compute_ms> --dma-ms <dma_ms> \
    --loops 1000 --evidence-level LAB --out res_compiler_dag.csv

# 原生 vLLM-Ascend LayerwiseConnector 对照；端点与模型路径按现场环境替换。
export VLLM_ASCEND_ENABLE_LAYERWISE=1
python3 -m vllm.entrypoints.openai.api_server \
    --model <model_path> --tensor-parallel-size 8 \
    --kv-transfer-config '{"kv_connector": "MooncakeLayerwiseConnector", "kv_role": "kv_producer"}' \
    --port <native_port> &

python3 -m vllm.benchmarks.benchmark_serving \
    --backend vllm --model <model_path> --dataset-name sharegpt \
    --num-prompts 100 --request-rate 10 --port <native_port> \
    --save-result --result-filename ./res_dag_native.json
```

> `<compute_ms>`、端口、模型路径和结果值均为输入占位符；W0/DEMO 可以验证命令与 Schema，不能替代真实在线消融证据。

---

## 4. 分步执行测试操作规程 (SOP)

开发人员在执行 PVT-02 时，请严格按照以下 4 个步骤逐步执行，并理解每一步的操作意图：

### 步骤 1：生成不同显存碎片率的测试 Manifest 集
- **操作意图**：模拟生产环境中不同的显存碎片状态。当显存刚启动时物理地址极度连续（碎片率 10%）；当长时间运行后显存被反复分配释放变得高度离散（碎片率 50% 与 100%）。我们需要验证编译器在各种碎片率下的鲁棒性与压缩率。
- **物理原理**：脚本会生成 32K Token 对应的 2,000 个 Block 地址列表，分别构造“90% 物理相连”、“50% 物理相连”与“完全随机打散”三种测试数据集。
- **执行命令**：
```bash
python3 ./make_manifests.py --tokens 32768 --block-size 16 --fragmentation-ratios 0.1,0.5,1.0 --out-dir ./manifest_data
```

### 步骤 2：测试描述符编译耗时与压缩比
- **操作意图**：对比传统逐块遍历提交与经过 C++ 描述符编译器贪心合并后的提交耗时与描述符条目数，验证描述符数量是否减少 $\ge 50\%$、CPU 耗时是否降低 $\ge 40\%$。
- **执行命令**：
```bash
./descriptor_bench --manifest-dir ./manifest_data --loops 10000 --evidence-level LAB --out res_descriptor_compile.csv
```

### 步骤 3：启动 NPU 双 Stream 异步 DAG 边算边传流水压测
- **操作意图**：在真实 NPU 上拉起 Compute Stream（执行 GEMM 矩阵乘计算）与 Transfer Stream（执行 URMA DMA 传输），验证两者的重叠率（Overlap Ratio）是否达到 $\ge 60\%$。
- **执行命令**：
```bash
./async_dag_bench --layers 80 --tokens 32768 --evidence-level LAB --out res_async_dag.csv
```

### 步骤 4：生成综合对比表与 Timeline 证据图
- **操作意图**：汇总编译耗时、压缩比与 Stream Timeline 重叠率，输出 E1 底座准入证明报告。
- **执行命令**：
```bash
python3 ./eval_descriptor_pipeline.py --compile-csv res_descriptor_compile.csv --dag-csv res_async_dag.csv --out-png timeline_overlap.png --out-summary summary_pvt02.csv
```

---

## 5. 数据采集清单与记录格式

### 5.1 描述符编译与压缩性能表 (`pvt02_compile_results.csv`)
```csv
workload_id,tokens,fragmentation_pct,raw_blocks_count,merged_sg_entries,compression_ratio_pct,python_ser_us,cpp_compile_us,cpu_reduction_pct,evidence_level,status,invalid_reason
<workload_id>,<tokens>,<fragmentation_pct>,<raw_blocks_count>,<merged_sg_entries>,<calculated_compression_ratio_pct>,<measured_python_ser_us>,<measured_cpp_compile_us>,<calculated_cpu_reduction_pct>,<LAB_OR_DEMO>,<status>,<null_or_reason>
```

### 5.2 NPU 异步流水重叠率实测表 (`pvt02_dag_results.csv`)
```csv
layer_count,tokens,t_pure_compute_ms,t_pure_transfer_ms,t_serial_total_ms,t_pipelined_total_ms,overlap_ratio_pct,is_pass,evidence_level,status,invalid_reason
<layer_count>,<tokens>,<measured_compute_ms>,<measured_transfer_ms>,<calculated_serial_total_ms>,<measured_pipeline_total_ms>,<calculated_overlap_ratio_pct>,<TRUE_OR_FALSE>,<LAB_OR_DEMO>,<status>,<null_or_reason>
```

### 5.3 数据交叉组合与运算推导逻辑

描述符和流水两类证据必须从同一 `run_id` 的原始样本计算：

$$\text{描述符压缩率} = 1.0 - \frac{\text{合并后描述符条目数}}{\text{原始离散 Block 数量}}$$

$$\text{计算-传输重叠率} = \frac{(T_{\text{compute}} + T_{\text{transfer}}) - T_{\text{total\_pipeline}}}{\min(T_{\text{compute}}, T_{\text{transfer}})} \times 100\%$$

若 `actual_path`、Profiler Timeline、原始样本或代码包标识缺失，不能仅凭公式推导出 LAB/MEASURED 结论，应标记 `INVALID-EVIDENCE`。

---

## 6. GO / CONDITIONAL / NO-GO / NOT-SUPPORTED / INVALID-EVIDENCE 判定规则

- **GO（准入通过）**：
  - 在典型碎片率（50%）下，描述符数量压缩率 $\ge 50\%$，C++ 编译耗时 $< 50\mu s$；
  - 异步 DAG 流水计算-传输重叠率 $\ge 60\%$；
- **CONDITIONAL（条件准入）**：重叠率在 $45\% \sim 60\%$ 之间，需优化 Stream 事件同步粒度；
- **NO-GO（暂不准入）**：现场证据确认描述符编译产生内存泄露或段错误，或者重叠率 $< 45\%$；
- **NOT-SUPPORTED（当前不支持）**：现场框架适配器、NPU Event 或目标异步接口不可用，无法执行对应路径；
- **INVALID-EVIDENCE（证据无效）**：缺少跨框架输入、编译输出、NPU Timeline、资源释放记录或完整重复场次。缺失字段必须使用 `null` 并填写 `invalid_reason`。

---

## 7. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 7.1 研发任务拆解与分工
- **工程师职责**：
  1. 编译 `./原型验证代码/PVT-02/` 工程；
  2. 按照第 4 节 SOP 执行测试，观察终端输出的重叠率数值；
  3. 检查生成的 CSV 表格与 Timeline 图像；
- **AI Agent 职责**：
  1. 负责 `descriptor_compiler.cc` 中边界地址对齐与贪心合并分支审查；
  2. 负责 `async_dag_bench.cc` 中 CANN `aclrtEvent` 同步时序调优。

### 7.2 专属 Prompt 模板（可直接复制给 AI Agent）
```text
我是一名 C++ 系统级工程师，正在进行 PVT-02 异构框架 Layout 描述符编译器与异步流水验证：
1. 请阅读 ./原型验证代码/PVT-02/descriptor_compiler.h 与 descriptor_compiler.cc；
2. 检查贪心合并算法 compile_and_merge，确保能正确处理物理地址连续的 Extent 并合并，同时设置硬件 Fence 标志；
3. 按照第 4 节 SOP 步骤执行 make_manifests.py 生成碎片化测试集，并运行 descriptor_bench 测量编译耗时；
4. 运行 async_dag_bench，在 NPU 上启动 80 层计算与传输的双 Stream 异步流水，计算重叠率 Overlap Ratio；
5. 输出最终数据对账表 summary_pvt02.csv 并验证是否满足准入要求。
```

### 7.3 常见排错指南
- **贪心合并后数据传输错位**：检查 `cur.src_phys_addr` 和 `cur.dst_phys_addr` 是否同时满足连续递增条件，不能只判断源地址连续；
- **CANN Event 报 `ACL_ERROR_INVALID_PARAM`**：确保 `aclrtRecordEvent` 和 `aclrtStreamWaitEvent` 所传入的 Stream 句柄属于同一 NPU Device 上下文。
