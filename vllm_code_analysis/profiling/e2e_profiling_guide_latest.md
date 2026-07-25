# vLLM & vLLM-Ascend (Latest Main) 全量 E2E 推理 CPU 侧打点计时与占比分析方案

> 本方案针对最新 `main` 分支版本的 vLLM 社区原生库以及 **vLLM-Ascend (华为昇腾 NPU)** 扩展库，构建了**全量无死角覆盖 6 大一级环节**（并融入 Mooncake Master RPC、Prefix Cache、BlockTable H2D 等关键子环节）的 CPU 打点 Profiling 体系。

---

## 一、 6 大一级环节与细分子环节树状图谱 (Sum = 100%)

```
CPU Total Processing Time (100%)
├── ❶ Stage_1: Tokenization_and_Request_Init
│   └── (HTTP Request JSON Parse + Tokenizer Encode + Sequence Init)
│
├── ❷ Stage_2: Scheduler_and_Memory_Management
│   ├── [Sub-2.1] Local_PrefixCache_Lookup (RadixTree / Block Hash 匹配)
│   └── [Sub-2.2] Mooncake_Master_RPC_RTT (vLLM <-> Mooncake Master 远程元数据查询)
│
├── ❸ Stage_3: Model_Input_Preparation_and_H2D
│   ├── [Sub-3.1] BlockTable_and_SlotMapping_CPU_Build (List->Tensor 组包与 Padding)
│   └── [Sub-3.2] BlockTable_H2D_Transfer (CPU -> NPU/GPU 显存异步下发)
│
├── ❹ Stage_4: Model_Forward_Launch_Overhead
│   └── (Python -> PyTorch API / Ascend CANN ACL 框架 Kernel Launch 下发)
│
├── ❺ Stage_5: Sampling_and_Logits_PostProcessing
│   └── (Logits D2H 提取 + Top-P/Top-K / Sample Token 计算)
│
└── ❻ Stage_6: Detokenization_and_Streaming_Response
    └── (Token Decode 解码 + Stop Words 检查 + HTTP SSE Streaming 吐出)
```

---

## 二、 关键代码文件与插桩位置对照

| 环节编号 | 环节名称 | vLLM (GPU) 文件路径 | vLLM-Ascend (NPU) 文件路径 | 对应核心函数/方法 |
| :---: | :--- | :--- | :--- | :--- |
| **❶** | **Tokenization & Request Init** | `vllm/entrypoints/openai/api_server.py` | `vllm/entrypoints/openai/api_server.py` | `create_chat_completion()` |
| **❷** | **Scheduler & Memory Mgmt** | `vllm/core/scheduler.py` | `vllm/core/scheduler.py` | `Scheduler.schedule()` |
| **[Sub-2.1]**| *Local Prefix Cache Lookup* | `vllm/core/block_manager.py` | `vllm/core/block_manager.py` | `BlockSpaceManager.allocate()` |
| **[Sub-2.2]**| *Mooncake Master RPC RTT* | - | `vllm_ascend/.../mooncake_backend.py` | `MooncakeBackend.exists()` / `get()` |
| **❸** | **Input Prep & H2D Transfer** | `vllm/worker/gpu_model_runner.py` | `vllm_ascend/worker/ascend_model_runner.py` | `prepare_input_tensors()` |
| **[Sub-3.1]**| *BlockTable CPU Build* | `vllm/worker/gpu_model_runner.py` | `vllm_ascend/worker/ascend_model_runner.py` | `_prepare_prompt()` / `_prepare_decode()` |
| **[Sub-3.2]**| *BlockTable H2D Transfer* | `vllm/worker/gpu_model_runner.py` | `vllm_ascend/worker/ascend_model_runner.py` | `tensor.to(device, non_blocking=True)` |
| **❹** | **Model Forward Launch** | `vllm/model_executor/models/` | `vllm_ascend/attention/ascend_attention.py` | `model.forward()` / CANN ACL Launch |
| **❺** | **Sampling & Post-Processing** | `vllm/model_executor/layers/sampler.py` | `vllm_ascend/sample/` | `Sampler.forward()` |
| **❻** | **Detokenization & Stream** | `vllm/entrypoints/openai/api_server.py` | `vllm/entrypoints/openai/api_server.py` | `tokenizer.decode()` |

---

## 三、 使用方式

配套的补丁文件为 `e2e_profiling_latest.patch`，可在根目录下通过以下命令直接应用补丁：

```bash
# 在 vllm 代码仓库应用补丁
cd d:/codes/vllm/vllm
git apply ../e2e_profiling_latest.patch

# 在 vllm-ascend 代码仓库应用补丁
cd d:/codes/vllm/vllm-ascend
git apply ../e2e_profiling_latest.patch
```

在测试基准运行完毕后，可在日志中看到如下精准归一化到 **100% Total CPU Latency** 的分析结果：

```text
================================================================================
          vLLM & vLLM-Ascend CPU E2E Performance Breakdown                      
================================================================================
Stage / Sub-Stage Name                        | Cost (ms)    | Ratio in CPU Total (%)
--------------------------------------------------------------------------------
► Stage_1:Tokenization_and_Req_Init           | 18.5210      | 14.82%              
► Stage_2:Scheduler_and_Memory_Management     | 48.6102      | 38.89%              
   └── Sub-2.1:Local_PrefixCache_Lookup       | 14.2105      | (11.37% of Total CPU)
   └── Sub-2.2:Mooncake_Master_RPC_RTT        | 27.8100      | (22.25% of Total CPU)
► Stage_3:Model_Input_Preparation_and_H2D    | 34.1200      | 27.30%              
   └── Sub-3.1:BlockTable_and_SlotMapping_CPU | 23.5102      | (18.81% of Total CPU)
   └── Sub-3.2:BlockTable_H2D_Transfer        | 8.9100       | (7.13% of Total CPU)
► Stage_4:Model_Forward_Launch_Overhead       | 6.2105       | 4.97%               
► Stage_5:Sampling_and_Logits_PostProcessing  | 4.8102       | 3.85%               
► Stage_6:Detokenization_and_HTTP_Stream      | 12.7100      | 10.17%              
--------------------------------------------------------------------------------
TOTAL CPU TRACKED LATENCY (100%)              | 124.9819     | 100.00%
================================================================================
```

---

## 四、 物理机器测试实操方案 (Physical Machine Testing Plan)

本方案专门针对包含 **昇腾 NPU (如 Atlas 800T A2 / Atlas 300I) 物理服务器** 的硬件环境，指导开展精准、可复现的端到端 CPU 侧 Profiling 性能评估。

### 4.1 物理机器环境预检与准备

在物理节点上运行前，需确保硬件驱动、环境变量与 NUMA 拓扑正确配置：

```bash
# 1. 检查物理机 NPU 芯片与驱动状态
npu-smi info

# 2. 检查 CPU NUMA 架构拓扑 (确定 NPU 与 CPU Socket 亲和性)
numactl -H
lscpu

# 3. 加载 CANN 驱动与 PyTorch NPU 环境变量
source /usr/local/Ascend/ascend-toolkit/set_env.sh
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3

# 4. (如启用 Mooncake) 设置 Mooncake 配置文件环境变量
export MOONCAKE_CONFIG_PATH=/etc/mooncake/mooncake.json
export ASCEND_ENABLE_USE_FABRIC_MEM=1
```

### 4.2 物理机进程 CPU 绑核 (NUMA Pinning)

为防止物理服务器多 CPU Socket 间的 Cross-NUMA 内存访存开销造成 CPU 调度延迟剧烈抖动，**强烈建议使用 `numactl` 进行 CPU 绑核**：

```bash
# 假设 NPU 0 绑定的本地 CPU 节点为 NUMA node 0 (对应 Core 0-31)
# 使用 numactl 将 vLLM/vLLM-Ascend 推理进程限定在 Node 0 的 CPU 和内存上：
numactl --cpunodebind=0 --membind=0 python3 -m vllm.entrypoints.openai.api_server \
    --model /path/to/DeepSeek-V3/ \
    --trust-remote-code \
    --port 8000
```

### 4.3 物理机器上的 4 大对比测试场景与命令

在物理服务器上，通过操控并发度、上下文长度以及分布式 Cache 配置，诱发不同 CPU 环节成为主要矛盾：

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
* **期望瓶颈**: `Stage_1: Tokenization` 与 `Stage_4: Model_Forward_Launch`。

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
* **期望瓶颈**: `Sub-2.1: Local_PrefixCache_Lookup` 耗时显著上升 (预期占比 >15%)。

#### 场景 3：Mooncake 分布式 KV Pool 元数据 RPC 场景
* **测试目的**: 测试 vLLM 与 Mooncake Master 服务间跨进程 RPC 往返时延 `Sub-2.2: Mooncake_Master_RPC_RTT`。
* **前置准备**: 启动 Mooncake Master 服务（在 Mooncake 源码目录下运行 Master Daemon）。
* **配置**: 环境变量 `MOONCAKE_CONFIG_PATH` 指向节点配置。
* **命令**:
  ```bash
  python3 benchmarks/benchmark_serving.py \
      --backend vllm \
      --dataset-name ShareGPT \
      --num-prompts 100 \
      --request-rate 5
  ```
* **期望瓶颈**: `Sub-2.2: Mooncake_Master_RPC_RTT` (预期占比 20%~35%)。

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
* **期望瓶颈**: `Sub-3.1: BlockTable_and_SlotMapping_CPU_Build` (预期占比 >25%)。

### 4.4 物理机自动化日志采集与分析脚本

在物理机执行压测后，可运行以下脚本抓取 vLLM 标准输出中的 `vLLM & vLLM-Ascend CPU E2E Performance Breakdown` 日志块，生成分析文件：

```bash
#!/bin/bash
# collect_cpu_profiling.sh

LOG_FILE="vllm_server_profiling.log"

echo "=== Extracting CPU Profiling Summary from Physical Server Log ==="
grep -A 20 "CPU E2E Performance Breakdown" $LOG_FILE

if [ $? -ne 0 ]; then
    echo "Warning: No Profiling Summary found in $LOG_FILE. Please check if the patch is applied."
fi
```
