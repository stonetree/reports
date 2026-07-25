# vLLM & vLLM-Ascend (v0.18.0rc1) 全量 CPU 打点 Profiling 方案

> 本方案专门针对特定 Tag 版本进行代码级别深度对齐与测试：
> - **vllm Tag**: `v0.18.0rc1`
> - **vllm-ascend Tag**: `v0.18.0rc1`
> - **Mooncake Branch**: `main` (或 `master`)

---

## 一、 v0.18.0rc1 版本 6 大一级环节与细分子环节树状图谱

```
CPU Total Processing Time (100%)
├── ❶ Stage_1: Tokenization_and_Request_Init
│   └── (HTTP Request JSON Parse + Tokenizer Encode + Sequence Init)
│
├── ❷ Stage_2: Scheduler_and_Memory_Management
│   ├── [Sub-2.1] Local_PrefixCache_Lookup (BlockSpaceManager 哈希与 RadixTree 匹配)
│   └── [Sub-2.2] Mooncake_Master_RPC_RTT (MooncakeBackend.exists / get 远程查询)
│
├── ❸ Stage_3: Model_Input_Preparation_and_H2D
│   ├── [Sub-3.1] BlockTable_and_SlotMapping_CPU_Build (List->Tensor 组包转换)
│   └── [Sub-3.2] BlockTable_H2D_Transfer (CPU -> NPU/GPU 显存异步下发)
│
├── ❹ Stage_4: Model_Forward_Launch_Overhead
│   └── (Python -> PyTorch API / CANN ACL Launch 框架下发耗时)
│
├── ❺ Stage_5: Sampling_and_Logits_PostProcessing
│   └── (Logits D2H 提取 + Top-P/Top-K / Sample Token 计算)
│
└── ❻ Stage_6: Detokenization_and_Streaming_Response
    └── (Token Decode 解码 + SSE Streaming 推送)
```

---

## 二、 代码文件与函数插桩映射

| 环节编号 | 环节名称 | v0.18.0rc1 源码文件路径 | 对应核心代码函数/位置 |
| :---: | :--- | :--- | :--- |
| **Stage_1** | **Tokenization & Request Init** | `vllm/entrypoints/openai/api_server.py` | `create_chat_completion()` |
| **Stage_2** | **Scheduler & Memory Mgmt** | `vllm/core/scheduler.py` | `Scheduler.schedule()` |
| └─ **Sub-2.1** | *Local Prefix Cache Lookup* | `vllm/core/block_manager.py` | `BlockSpaceManager.allocate()` |
| └─ **Sub-2.2** | *Mooncake Master RPC RTT* | `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/backend/mooncake_backend.py` | `MooncakeBackend.exists()` / `get()` |
| **Stage_3** | **Input Prep & H2D Transfer** | `vllm_ascend/worker/ascend_model_runner.py` | `execute_model()` |
| └─ **Sub-3.1** | *BlockTable CPU Build* | `vllm_ascend/worker/ascend_model_runner.py` | `_prepare_inputs()` |
| └─ **Sub-3.2** | *BlockTable H2D Transfer* | `vllm_ascend/worker/ascend_model_runner.py` | `block_tables_cpu.to(device, non_blocking=True)` |
| **Stage_4** | **Model Forward Launch** | `vllm_ascend/attention/ascend_attention.py` | `model.forward()` / CANN ACL Launch |
| **Stage_5** | **Sampling & Post-Processing** | `vllm/model_executor/layers/sampler.py` | `Sampler.forward()` |
| **Stage_6** | **Detokenization & Stream** | `vllm/entrypoints/openai/api_server.py` | `tokenizer.decode()` |

---

## 三、 使用方式

配套补丁为 `e2e_profiling_v0.18.0rc1.patch`，可在根目录下直接打入：

```bash
# 在 vllm 代码仓库打补丁 (v0.18.0rc1)
cd d:/codes/vllm/vllm
git apply ../e2e_profiling_v0.18.0rc1.patch

# 在 vllm-ascend 代码仓库打补丁 (v0.18.0rc1)
cd d:/codes/vllm/vllm-ascend
git apply ../e2e_profiling_v0.18.0rc1.patch
```

基准测试完毕后输出的百分比样例：

```text
================================================================================
  vLLM & vLLM-Ascend (v0.18.0rc1) CPU E2E Performance Breakdown                 
================================================================================
Stage / Sub-Stage Name                        | Cost (ms)    | Ratio in CPU Total (%)
--------------------------------------------------------------------------------
► Stage_1:Tokenization_and_Req_Init           | 16.4100      | 13.62%              
► Stage_2:Scheduler_and_Memory_Management     | 47.1205      | 39.11%              
   └── Sub-2.1:Local_PrefixCache_Lookup       | 13.8100      | (11.46% of Total CPU)
   └── Sub-2.2:Mooncake_Master_RPC_RTT        | 26.5401      | (22.03% of Total CPU)
► Stage_3:Model_Input_Preparation_and_H2D    | 32.8900      | 27.30%              
   └── Sub-3.1:BlockTable_and_SlotMapping_CPU | 22.4500      | (18.63% of Total CPU)
   └── Sub-3.2:BlockTable_H2D_Transfer        | 8.6100       | (7.15% of Total CPU)
► Stage_4:Model_Forward_Launch_Overhead       | 6.1100       | 5.07%               
► Stage_5:Sampling_and_Logits_PostProcessing  | 4.6200       | 3.83%               
► Stage_6:Detokenization_and_HTTP_Stream      | 13.3300      | 11.07%              
--------------------------------------------------------------------------------
TOTAL CPU TRACKED LATENCY (100%)              | 120.4805     | 100.00%
================================================================================
```

---

## 四、 物理机器测试实操方案 (Physical Machine Testing Plan for v0.18.0rc1)

本方案针对部署在 **昇腾 NPU (如 Atlas 800T A2 / Atlas 300I) 物理服务器** 环境下的 `v0.18.0rc1` 版本进行性能基准测量。

### 4.1 物理机器环境预检与准备

在物理节点上运行前，需确保硬件驱动、环境变量与 NUMA 拓扑正确配置：

```bash
# 1. 检查物理机 NPU 芯片与驱动状态
npu-smi info

# 2. 检查 CPU NUMA 架构拓扑 (确定 NPU 与 CPU Socket 亲和性)
numactl -H
lscpu

# 3. 加载 CANN 驱动与 v0.18.0rc1 配套环境变量
source /usr/local/Ascend/ascend-toolkit/set_env.sh
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3

# 4. (如启用 Mooncake) 设置 Mooncake 配置文件环境变量
export MOONCAKE_CONFIG_PATH=/etc/mooncake/mooncake.json
export ASCEND_ENABLE_USE_FABRIC_MEM=1
```

### 4.2 物理机进程 CPU 绑核 (NUMA Pinning)

为了消除跨 CPU Socket 访存造成的 CPU 时延抖动，建议在物理机上使用 `numactl` 绑核：

```bash
# 将 vLLM/vLLM-Ascend (v0.18.0rc1) 进程绑定至对应的 NUMA Node 0 物理核心：
numactl --cpunodebind=0 --membind=0 python3 -m vllm.entrypoints.openai.api_server \
    --model /path/to/DeepSeek-V3/ \
    --trust-remote-code \
    --port 8000
```

### 4.3 物理机器上的 4 大对比测试场景与命令

#### 场景 1：基线场景 (Small Batch & Short Prompt)
* **测试目的**: 测量单请求/低并发下 CPU 基础框架 Overhead。
* **命令**:
  ```bash
  python3 benchmarks/benchmark_serving.py \
      --backend vllm \
      --dataset-name random \
      --random-input-len 128 \
      --random-output-len 128 \
      --num-prompts 50 \
      --request-rate 1
  ```

#### 场景 2：Prefix Cache 压力场景 (High Prefix Overlap)
* **测试目的**: 触发本地 BlockManager 基数树与 Hash Block 高频匹配，测试 `Sub-2.1: Local_PrefixCache_Lookup` 占比。
* **配置**: 启动 Server 时加上 `--enable-prefix-caching`。
* **命令**:
  ```bash
  python3 benchmarks/benchmark_serving.py \
      --backend vllm \
      --dataset-name ShareGPT \
      --num-prompts 200 \
      --request-rate 10 \
      --enable-prefix-caching
  ```

#### 场景 3：Mooncake 分布式 KV Pool 元数据 RPC 场景 (v0.18.0rc1 节点)
* **测试目的**: 测试 vLLM 与 Mooncake Master 服务间跨进程 RPC 往返时延 `Sub-2.2: Mooncake_Master_RPC_RTT`。
* **命令**:
  ```bash
  python3 benchmarks/benchmark_serving.py \
      --backend vllm \
      --dataset-name ShareGPT \
      --num-prompts 100 \
      --request-rate 5
  ```

#### 场景 4：长序列大 Batch 场景 (Large BlockTable Processing)
* **测试目的**: 压测 CPU 侧构建长 BlockTable List->Tensor 组包开销 `Sub-3.1: BlockTable_CPU_Build`。
* **命令**:
  ```bash
  python3 benchmarks/benchmark_serving.py \
      --backend vllm \
      --dataset-name random \
      --random-input-len 32768 \
      --random-output-len 512 \
      --num-prompts 20 \
      --request-rate 2
  ```

### 4.4 物理机 Profiling 日志收集

在物理机执行测试后，运行提取脚本分析日志：

```bash
#!/bin/bash
# collect_cpu_profiling.sh
LOG_FILE="vllm_server_profiling.log"

echo "=== Extracting CPU Profiling Summary for v0.18.0rc1 ==="
grep -A 20 "v0.18.0rc1" $LOG_FILE
```
