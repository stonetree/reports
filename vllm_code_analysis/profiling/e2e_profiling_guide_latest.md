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
