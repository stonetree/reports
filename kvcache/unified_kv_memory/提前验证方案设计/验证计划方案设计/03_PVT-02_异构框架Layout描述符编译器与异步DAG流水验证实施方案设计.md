# PVT-02：异构框架 Layout 描述符编译器与异步 DAG 流水验证实施方案设计
## —— Mooncake 离散 Block 传输协议重构：连续块贪心合并与异步 Stream 重叠

> **验证 ID**：PVT-02  
> **验证名称**：异构框架内存布局 (Layout) 描述符编译器与异步有向无环图 (DAG) 流水验证  
> **穿刺优先级**：**🔴 P0 级（核心决胜项）**  
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

## 1. 验证目标与交付结论定义

### 1.1 待验证核心命题
不同开源推理框架在显存布局管理上存在结构差异：
- **vLLM** 采用固定槽位大小的分页显存块（Paged Block，如 16 或 32 Tokens 组成一个固定物理 Block）；
- **SGLang** 采用基于 Radix Tree 动态扩展的物理连续前缀区间（长度从数十到数千 Tokens 不等）。

Mooncake 原生面向此类框架时缺乏对离散 Block Table 的批量编译合并能力，逐块提交导致 Host CPU 产生严重瓶颈。本验证旨在通过在 Mooncake 传输前置流水中注入 `DescriptorCompiler` 与异步 DAG 调度引擎，证明：
1. **Layout 描述符编译器（DescriptorCompiler）**能够将跨框架离散物理 Block 零拷贝编译为硬件 Scatter-Gather 描述符，使**描述符提交数量下降 $\ge 50\%$**，**Host CPU 提交耗时下降 $\ge 40\%$**；
2. **异步 DAG 流水调度引擎**能够实现 NPU 算力计算流（Compute Stream）与 DMA 传输流（Transfer Stream）的高效重叠，**计算-传输重叠率（Overlap Ratio）达到 $\ge 60\%$**。

### 1.2 最终交付数据与结论产出
开发人员执行完本方案后，必须输出以下交付件：
1. **《Descriptor 编译器耗时与 Scatter-Gather 压缩率实测表》**（覆盖 16~1024 离散段）；
2. **《CPU 提交时延基线 vs 批量编译优化对比表》**；
3. **《NPU Compute 与 DMA Transfer 异步流水 Timeline 重叠率分析表》**（附 Profiler Trace）；
4. **《Go / No-Go 判定结论》**：依据重叠率 $\ge 60\%$ 与 CPU 提交降幅 $\ge 40\%$ 门槛判定。

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
    uint32_t block_bytes;         // 本块总字节数 = 2 * N_layer * N_head * Head_dim * token_count * 2B
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

// 4. 批量描述符包头 (Batch Descriptor Header)
struct alignas(64) BatchDescriptorHeader {
    uint32_t batch_id;
    uint32_t total_entries;       // 编译合并后的 SG Entry 数量
    uint64_t total_payload_bytes; // 本批次总正文字节数
    uint64_t completion_fence_id;// 完成屏障 Fence ID
    std::vector<HardwareSGEntry> entries;
};
```

### 2.2 跨框架内存布局向 ExtentManifest 的极速转换适配器
原生支持 vLLM `v0.26.1+ (main)`（兼容传统 BlockTable 与新一代 V1 `KVCacheManager`）以及 SGLang Radix Tree：

```cpp
// 1. vLLM V0/V1 BlockTable 适配器
void adapt_vllm_blocks(const std::vector<uint64_t>& block_ids, uint32_t tokens_per_block, 
                       uint32_t bytes_per_block, std::vector<LogicalBlockExtent>& out) {
    out.reserve(block_ids.size());
    for (size_t i = 0; i < block_ids.size(); ++i) {
        LogicalBlockExtent ext;
        ext.logical_token_start = i * tokens_per_block;
        ext.token_count = tokens_per_block;
        ext.phys_base_addr = block_ids[i] * bytes_per_block;
        ext.stride_bytes = 0;
        ext.block_bytes = bytes_per_block;
        out.push_back(ext);
    }
}

// 2. SGLang RadixTree 动态连续 Span 适配器
struct SGLangSpan { uint64_t token_start; uint32_t len; uint64_t phys_addr; uint32_t bytes; };
void adapt_sglang_spans(const std::vector<SGLangSpan>& spans, std::vector<LogicalBlockExtent>& out) {
    out.reserve(spans.size());
    for (const auto& sp : spans) {
        LogicalBlockExtent ext{sp.token_start, sp.len, 0, sp.phys_addr, sp.bytes, 0};
        out.push_back(ext);
    }
}
```

### 2.3 物理连续块贪心合并算法 (Greedy SG Extent Merger)
编译器核心算法在 $O(N)$ 时间复杂度下，一次性完成离散 Block 的连续性探测与贪心合并：

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
- **Stream 0（Compute Stream）**：负责 NPU 算子计算；
- **Stream 1（Transfer Stream）**：负责底层 UBMEM/URMA DMA 传输；
- 通过 `aclrtRecordEvent` 与 `aclrtStreamWaitEvent` 构建微秒级无锁同步屏障。

---

## 3. 测试工具与工程构建规范 (对标 vLLM-Ascend 开源基线)

测试工程存放在 `./原型验证代码/PVT-02/` 目录下：

```
原型验证代码/PVT-02/
├── descriptor_compiler.h      # 描述符编译器头文件 (64B POD C++ 结构)
├── descriptor_compiler.cc     # 描述符贪心合并与硬件描述符生成算法实现
├── async_dag_bench.cc         # 跨节点 Layerwise 边算边传异步 DAG 重叠流水压测工具
├── Makefile                   # 编译构建工程 (make -j16)
└── make_manifests.py          # 离散碎片 Manifest 生成脚本
```

### 3.1 单元基准压测命令
```bash
cd ./原型验证代码/PVT-02 && make clean && make -j16
./async_dag_bench --chunks 16 --chunk_tokens 2048 --discrete_ratio 0.5
```

### 3.2 对标 vLLM-Ascend MooncakeLayerwiseConnector 在线消融
```bash
# 1. 启动官方原生 Python-ZMQ 序列化基线 (vLLM-Ascend 默认)
export VLLM_ASCEND_ENABLE_LAYERWISE=1
python3 -m vllm.entrypoints.openai.api_server \
    --model /models/Qwen/Qwen2.5-72B-Instruct \
    --tensor-parallel-size 8 \
    --kv-transfer-config '{"kv_connector": "MooncakeLayerwiseConnector", "kv_role": "kv_producer"}' \
    --port 8100 &

# 2. 发起在线打流并采集 C++ 描述符加速前后 CPU 提交开销与 TTFT
python3 -m vllm.benchmarks.benchmark_serving \
    --backend vllm \
    --model /models/Qwen/Qwen2.5-72B-Instruct \
    --dataset-name sharegpt \
    --num-prompts 100 \
    --request-rate 10 \
    --port 8100 \
    --save-result --result-filename ./res_dag_native.json
```

---

## 4. 数据采集清单与记录格式

### 4.1 描述符编译与流水重叠数据表 (`pvt02_dag_results.csv`)
```csv
workload_id,total_tokens,discrete_ratio,raw_block_count,merged_sg_entries,compression_ratio,compile_latency_us,compute_time_ms,transfer_time_ms,total_pipeline_time_ms,overlap_ratio_pct
DAG-01,16384,0.10,1024,103,0.899,1.8,12.5,14.2,16.1,74.5
DAG-02,16384,0.50,1024,512,0.500,2.4,12.5,14.2,18.4,62.8
DAG-03,16384,1.00,1024,1024,0.000,3.1,12.5,14.2,26.7,0.0
```

---

## 5. 数据交叉组合与运算推导逻辑

### 5.1 描述符压缩率计算公式
$$\text{压缩率} = 1.0 - \frac{\text{合并后描述符条目数 (merged\_entries)}}{\text{原始离散 Block 数量 (raw\_blocks)}}$$

### 5.2 计算-传输重叠率 (Overlap Ratio) 计算公式
$$\text{Overlap Ratio} = \frac{(T_{\text{compute}} + T_{\text{transfer}}) - T_{\text{total\_pipeline}}}{\min(T_{\text{compute}}, T_{\text{transfer}})} \times 100\%$$

---

## 6. Go / Conditional / No-Go 判定规则

- **Go (准入通过)**：
  - 描述符连续块合并压缩率 $\ge 50\%$，单次编译耗时 $< 5\mu s$；
  - 异步 DAG 流水计算-传输重叠率 $\ge 60\%$；
- **Conditional (条件准入)**：
  - 重叠率在 $45\% \sim 60\%$ 之间，需优化 Chunked Prefill 切块粒度；
- **No-Go (否决关闭)**：
  - 描述符编译开销超过传输收益，或异步流水导致严重的 NPU 计算空转。
