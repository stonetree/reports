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
