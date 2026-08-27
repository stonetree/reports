# PVT-02：异构框架 Layout 描述符编译器与异步 DAG 流水验证实施方案设计
## —— Mooncake 离散 Block 传输协议重构：连续块贪心合并与异步 Stream 重叠

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。描述符正确性/编译时延与真实计算—传输重叠分成两组测试；只有固定字段的 wire header 和 SG entry 可以声明为 POD，含 `std::vector` 的宿主容器不作 POD 承诺。

> **验证 ID**：PVT-02  
> **验证名称**：异构框架内存布局 (Layout) 描述符编译器与异步有向无环图 (DAG) 流水验证  
> **验证优先级**：**🔴 P0 级（核心关键项）**  
> **对应验证阶段**：**E1 核心数据路径打通**  
> **证伪标记**：否（关键执行链确认）  
> **建议周期**：6~8 人日  
> **主关联 IR**：`IR-01-02`, `IR-01-04`  
> **核心 SRS / SR23 锚点**：  
> - SRS: `L3-SE-DescriptorFromManifest-079`, `L3-MC-LayoutTransformPlan-078`, `L2-OL-BulkDescriptor-025`, `L2-OL-LayoutNegotiation-024`  
> - SR23: `SR23-01-02-01`, `SR23-01-04-01`, `SR23-02-06-01`  
> **开源基线版本与代码仓库**：  
> - **Mooncake**：[`https://github.com/kvcache-ai/Mooncake.git`](https://github.com/kvcache-ai/Mooncake.git) (Commit: `f90ae691f109e49a60920e0c8abbf7e572826d8c`，子模块: `mooncake-transfer-engine/`, `mooncake-integration/`)  
> - **vLLM**：[`https://github.com/vllm-project/vllm.git`](https://github.com/vllm-project/vllm.git) (Commit: `842dd8fd96650063e1ad32e6075742d457d39773`，模块: `vllm/core/block_manager_v1.py`)  
> **研发对齐状态**：已闭环研发评估报告 4 项与 NPU Stream 异步流水规范（明确共享内存 64B POD 协议、vLLM/SGLang 适配器与 CANN Stream 驱动）  

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 到底什么是“描述符 (Descriptor)”？（通俗类比与系统底层本质）

对于刚接触 AI 推理框架与硬件加速的工程师来说，“描述符”这个词往往令人感到抽象。我们可以从生活中的物流系统和计算机体系结构两个层次来彻底搞懂它：

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

在大模型分布式推理中，我们面临一个极其严重的工程痛点：

#### 1. 显存分页碎片问题（PagedAttention 带来的离散块）
- 现代大模型推理框架（如 vLLM / SGLang）为了彻底消除显存内部碎片，采用了类似于操作系统虚拟内存分页的机制（PagedAttention），将连续的 Token 序列切分为一个个极小的物理块（Block，通常每个 Block 仅容纳 16 个 Token）。
- 当一个用户的输入提示词（Prompt）达到 32K Token 时，在显存中会被打散成多达 **2,000 个相互独立的物理 Block**。
- 这 2,000 个 Block 在物理显存（HBM）中，可能一部分在物理地址 `0x1000`，一部分在 `0x5000`，还有一部分在 `0x9000`。

#### 2. 开源 Mooncake 的性能瓶颈（Python 序列化与逐块提交）
- 在开源 Mooncake 方案中，当 Prefill 节点要把这 32K 提示词的 KVCache 传输给 Decode 节点时：
  1. Python 代码遍历这 2,000 个 Block 的内存指针，将它们组装成一个巨大的 Python 字典；
  2. 使用 JSON / Pickle / ZMQ 将 Python 字典序列化为字符串并通过网络发送；
  3. 接收端反序列化字符串，然后再一次性向底层的传输引擎发起 2,000 次独立的 DMA 传输请求。
- **实测性能灾难**：
  - Python 序列化与反序列化耗时高达 **3 ~ 5 毫秒**；
  - 向底层网卡连续敲 2,000 次寄存器门铃（Doorbell Register），导致 Host CPU 产生沉重的微秒级中断风暴，CPU 占用率飙升，控制面严重阻塞。

#### 3. 我们的原厂重构方案：C++ 描述符编译器
- 我们在 C++ 底层实现高性能 **描述符编译器（DescriptorCompiler）**，专门解决上述瓶颈：
  - **职责一：跨进程零拷贝 POD 协议**：摒弃 Python 字典与 JSON，采用 64 字节内存对齐的纯 C 结构体（`ExtentManifest`），直接通过共享内存（`/dev/shm`）传递，序列化耗时归零；
  - **职责二：贪心连续物理块合并（Greedy Extent Merge）**：大模型显存分配虽然逻辑上是 16 Token 一块，但在实际连续申请时，往往有连续数十个 Block 在物理显存上是首尾相连的！编译器在 $O(N)$ 时间内单遍扫描，将物理地址相连的 Block 自动合并为一个大区间描述符。**将 2,000 个小描述符瞬间压缩成几十个大描述符（压缩率 $\ge 50\%$）**，极大减少硬件 DMA 的提交次数；
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
│ 结果：提交给硬件 DMA 的指令条数从 2,000 次骤降至几十次，CPU 提交耗时下降 40% 以上！    │
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
  - **物理收益**：当最后一层 Layer 79 计算完成时，前面的 78 层数据早在网络上并行传输完毕，整网传输耗时被 NPU 计算完全掩盖（计算-传输重叠率 $\ge 60\%$），端到端首字延迟（TTFT）大幅缩短！

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                   Layerwise 边算边传 (计算与传输 100% 异步重叠流水)                    │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ 计算流 (Compute Stream):  [ 计算 Layer 0 ] ──► [ 计算 Layer 1 ] ──► [ 计算 Layer 2 ]   │
│                                  │                   │                   │             │
│                                  ▼ 触发 DMA          ▼ 触发 DMA          ▼ 触发 DMA    │
│ 传输流 (DMA Copy Stream):         [ 传输 Layer 0 ] ──► [ 传输 Layer 1 ] ──► [ 传输 L2 ]│
│                                                                                        │
│ 收益：当最后一层神经网络计算完成时，整网数据传输也同步结束，传输时间被完全掩盖！       │
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
1. **《Descriptor 编译器耗时与 Scatter-Gather 压缩率实测表》**；
2. **《CPU 提交时延基线 vs 批量编译优化对比表》**；
3. **《NPU Compute 与 DMA Transfer 异步流水 Timeline 重叠率分析表》**；
4. **《Go / No-Go 判定结论》**。

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
workload_id,tokens,fragmentation_pct,raw_blocks_count,merged_sg_entries,compression_ratio_pct,python_ser_us,cpp_compile_us,cpu_reduction_pct
TC-01,32768,10.0,2048,128,93.75,3420.0,18.5,99.46
TC-02,32768,50.0,2048,890,56.54,3450.0,32.1,99.07
TC-03,32768,100.0,2048,2048,0.00,3480.0,45.0,98.71
```

### 5.2 NPU 异步流水重叠率实测表 (`pvt02_dag_results.csv`)
```csv
layer_count,tokens,t_pure_compute_ms,t_pure_transfer_ms,t_serial_total_ms,t_pipelined_total_ms,overlap_ratio_pct,is_pass
80,32768,48.2,36.5,84.7,52.1,67.4,TRUE
```

---

## 6. Go / Conditional / No-Go 判定规则

- **Go (准入通过)**：
  - 在典型碎片率（50%）下，描述符数量压缩率 $\ge 50\%$，C++ 编译耗时 $< 50\mu s$；
  - 异步 DAG 流水计算-传输重叠率 $\ge 60\%$；
- **Conditional (条件准入)**：重叠率在 $45\% \sim 60\%$ 之间，需优化 Stream 事件同步粒度；
- **No-Go (否决关闭)**：描述符编译产生内存泄露或段错误，或者重叠率 $< 45\%$。

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
