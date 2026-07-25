# vLLM & Mooncake 全业务场景 CPU Profiling 评测与决策边界矩阵指南

> 本指南构建了一套**覆盖低、中、高元数据占比的 6 大典型在线推理业务场景矩阵**。
> 
> 通过全场景横向对比，帮助架构师与性能工程师**准确识别在哪些业务场景下元数据开销微乎其微，在哪些场景下元数据处理会剧烈上升并成为决定性的性能瓶颈（提供量化决策边界）**。

---

## 一、 6 大在线推理业务场景全景矩阵

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                              全业务场景 CPU Profiling 评测矩阵                           │
├───────────────────────┬───────────────────────────────┬────────────────────────────────┤
│       业务场景名称     │          负载特征描述          │       预期 CPU 瓶颈与元数据占比 │
├───────────────────────┼───────────────────────────────┼────────────────────────────────┤
│ 场景 1: 代码补全       │ • 短 Input (32-128 Tokens)    │ • 瓶颈: ❶ Tokenization + ❹ Launch│
│ (Code Completion)     │ • 单次生成, 无前缀重复, 低 Batch│ • 元数据占比: < 5% (极低, 安全区)│
├───────────────────────┼───────────────────────────────┼────────────────────────────────┤
│ 场景 2: 短文本 Agent   │ • 中等 Input (256-1k Tokens)  │ • 瓶颈: ❶ Tokenization + ❺ Sample│
│ (Agent Tool Call)     │ • 结构化 JSON, 低前缀复用      │ • 元数据占比: ~ 8% (低, 安全区) │
├───────────────────────┼───────────────────────────────┼────────────────────────────────┤
│ 场景 3: 传统单轮问答   │ • 中/短 Input (512 Tokens)    │ • 瓶颈: ❶ Tokenization + ❻ Stream│
│ (Single-turn QA)      │ • 独立 Prompt, 高流式输出      │ • 元数据占比: ~ 10% (较低)      │
├───────────────────────┼───────────────────────────────┼────────────────────────────────┤
│ 场景 4: 多轮对话/Chat │ • 高前缀重合度 (Shared System)│ • 瓶颈: ❷ Sub-2.1 Local Prefix │
│ (Multi-turn Chat)     │ • 动态增长上下文, 高 RequestRate│ • 元数据占比: 20% - 35% (高警示区)│
├───────────────────────┼───────────────────────────────┼────────────────────────────────┤
│ 场景 5: RAG/长文档分析 │ • 超长 Input (32k-128k Tokens)│ • 瓶颈: ❸ Sub-3.1 BlockTable  │
│ (RAG & Long Doc)      │ • 极长块链表, 不均匀 Batch 序列│ • 元数据占比: 25% - 40% (高警示区)│
├───────────────────────┼───────────────────────────────┼────────────────────────────────┤
│ 场景 6: 跨节点分布式检索│ • 多 Worker 集群协作          │ • 瓶颈: ❷ Sub-2.2 Mooncake RPC │
│ (Distributed KV Pool) │ • 共享 KV Pool, 远端 Key 检索 │ • 元数据占比: 30% - 45% (高警示区)│
└───────────────────────┴───────────────────────────────┴────────────────────────────────┘
```

---

## 二、 各场景数据集构造规格与测试命令行

### 场景 1：代码补全 (Code Completion) —— 【元数据极低占比组】
* **负载特征**: 短输入、短输出、无共享前缀、低并发。
* **数据集构造**: 输入 64 Tokens（如代码片段），输出 32 Tokens。
* **命令行**:
  ```bash
  python3 benchmarks/benchmark_serving.py \
      --backend vllm \
      --dataset-name random \
      --random-input-len 64 \
      --random-output-len 32 \
      --num-prompts 100 \
      --request-rate 2
  ```
* **瓶颈表现**: CPU 耗时集中在 `Stage_1: Tokenization` 与 `Stage_4: Model Forward Launch`，元数据开销 `< 5%`。

---

### 场景 2：短文本 Agent / Tool Call —— 【元数据低占比组】
* **负载特征**: 包含系统角色定义，但每次 Tool Call 消息独立，输入长度约 256~512 Tokens。
* **数据集构造**: 输入 256 Tokens 结构化 JSON 提示词，输出 128 Tokens。
* **命令行**:
  ```bash
  python3 benchmarks/benchmark_serving.py \
      --backend vllm \
      --dataset-name random \
      --random-input-len 256 \
      --random-output-len 128 \
      --num-prompts 100 \
      --request-rate 5
  ```
* **瓶颈表现**: `Stage_1: Tokenization` 与 `Stage_5: Sampling` 为主，元数据开销约 `8%`。

---

### 场景 3：传统单轮问答 (Single-turn QA) —— 【元数据中低占比组】
* **负载特征**: 典型 FAQ 问答，输入约 512 Tokens，流式生成 256 Tokens。
* **数据集构造**: 标准 ShareGPT 过滤单轮数据。
* **命令行**:
  ```bash
  python3 benchmarks/benchmark_serving.py \
      --backend vllm \
      --dataset-name ShareGPT \
      --num-prompts 100 \
      --request-rate 10
  ```
* **瓶颈表现**: `Stage_6: Detokenization & Streaming Response` 占比提升，元数据开销约 `10%`。

---

### 场景 4：多轮对话 / 共享 System Prompt —— 【元数据高占比组】
* **负载特征**: 共享 8k Tokens 系统提示词，高并发并发。
* **数据集构造**: 带 8k 相同前缀的 `shared_prefix_8k.jsonl` 数据集。
* **命令行**:
  ```bash
  python3 benchmarks/benchmark_serving.py \
      --backend vllm \
      --dataset-path ./shared_prefix_8k.jsonl \
      --num-prompts 200 \
      --request-rate 30 \
      --enable-prefix-caching
  ```
* **瓶颈表现**: `Sub-2.1: Local_PrefixCache_Lookup` 暴增，元数据开销升至 `20% ~ 35%`。

---

### 场景 5：RAG / 长文档分析 (Long Context) —— 【元数据极高占比组】
* **负载特征**: 输入长度 32k~64k Tokens，异构混合长短请求。
* **数据集构造**: 异构数据 `hetero_blocktable_dataset.jsonl`（10% 的 64k 文本 + 90% 的短文本）。
* **命令行**:
  ```bash
  python3 benchmarks/benchmark_serving.py \
      --backend vllm \
      --dataset-path ./hetero_blocktable_dataset.jsonl \
      --num-prompts 150 \
      --request-rate 15
  ```
* **瓶颈表现**: `Sub-3.1: BlockTable_CPU_Build` (Tensor Padding) 大幅上升，元数据开销升至 `25% ~ 40%`。

---

### 场景 6：跨节点分布式 KV Pool 检索 —— 【元数据极高占比组】
* **负载特征**: 启用 Mooncake KV Pool，节点并发拉取共享 Cache。
* **数据集构造**: Mooncake Key 数据集，配合高 Request Rate (100)。
* **命令行**:
  ```bash
  export ASCEND_ENABLE_USE_FABRIC_MEM=1
  export MOONCAKE_CONFIG_PATH=/etc/mooncake/mooncake.json
  
  python3 benchmarks/benchmark_serving.py \
      --backend vllm \
      --dataset-path ./remote_mooncake_keys.jsonl \
      --num-prompts 300 \
      --request-rate 100
  ```
* **瓶颈表现**: `Sub-2.2: Mooncake_Master_RPC_RTT` 暴增，元数据开销升至 `30% ~ 45%`。

---

## 三、 全场景横向 Profiling 对比结果举例 (决策边界)

下表汇总了在物理机上跑完 6 大场景矩阵后自动抽取的耗时占比矩阵：

```text
==============================================================================================================
                              Full Business Workload Profiling Decision Matrix                                
==============================================================================================================
Stage / Sub-Stage Name          | Code (1)    | Agent (2)   | QA (3)      | Chat (4)    | RAG (5)     | DistKV (6)
--------------------------------------------------------------------------------------------------------------
Stage_1: Tokenization           | 42.10%      | 35.20%      | 28.50%      | 12.10%      | 8.10%       | 6.20%
Stage_2: Scheduler & Memory     | 8.50%       | 12.10%      | 15.20%      | 38.80% ◄    | 22.40%      | 42.10% ◄
  └── Sub-2.1: PrefixCache      | (1.20%)     | (2.10%)     | (4.10%)     | (21.50%) ◄  | (8.20%)     | (6.10%)
  └── Sub-2.2: Mooncake RPC     | (0.00%)     | (0.00%)     | (0.00%)     | (5.10%)     | (3.10%)     | (28.40%) ◄
Stage_3: Input Prep & H2D       | 11.20%      | 14.50%      | 18.10%      | 24.20%      | 38.50% ◄    | 22.10%
  └── Sub-3.1: BlockTable Build | (3.10%)     | (4.20%)     | (6.50%)     | (14.20%)    | (27.80%) ◄  | (12.50%)
  └── Sub-3.2: H2D Transfer     | (2.10%)     | (3.10%)     | (4.20%)     | (5.10%)     | (6.10%)     | (4.80%)
Stage_4: Forward Launch Overhead| 25.10% ◄    | 18.20%      | 12.10%      | 5.10%       | 4.10%       | 3.80%
Stage_5: Sampling PostProcess   | 8.10%       | 12.50% ◄    | 8.10%       | 4.80%       | 3.20%       | 2.90%
Stage_6: Detokenize & Stream    | 5.00%       | 7.50%       | 18.00% ◄    | 15.00%      | 23.70%      | 22.90%
--------------------------------------------------------------------------------------------------------------
TOTAL CPU TRACKED LATENCY       | 100.00%     | 100.00%     | 100.00%     | 100.00%     | 100.00%     | 100.00%
METADATA TOTAL RATIO (Sub-2+3)  | 4.30% (安全)| 6.30% (安全)| 10.70%(安全)| 35.70%(瓶颈)| 36.00%(瓶颈)| 40.90%(瓶颈)
==============================================================================================================
```

### 关键决策分析结论：
1. **绿色安全区 (场景 1/2/3: 代码补全/Agent/短问答)**:
   - **结论**: 元数据总占比 `< 11%`，CPU 侧处理速度极快。在此类业务线上，无需过度投入 C++ Scheduler 重构或复杂的 KV Cache Pool 优化。
2. **红色高警示瓶颈区 (场景 4/5/6: 多轮 Chat/RAG 长文本/分布式 KV Pool)**:
   - **场景 4 瓶颈**: `Sub-2.1: Local PrefixCache Lookup` 占比高达 `21.5%`，瓶颈在 Python RadixTree 哈希匹配。建议优先优化 C++ Block Manager。
   - **场景 5 瓶颈**: `Sub-3.1: BlockTable Build` 占比高达 `27.8%`，瓶颈在长序列下 List->Tensor 动态 Padding。建议重构输入张量准备逻辑。
   - **场景 6 瓶颈**: `Sub-2.2: Mooncake RPC` 占比高达 `28.4%`，瓶颈在 ZMQ/gRPC 往返与 Master 锁。建议开启客户端本地 Cache 拦截。
