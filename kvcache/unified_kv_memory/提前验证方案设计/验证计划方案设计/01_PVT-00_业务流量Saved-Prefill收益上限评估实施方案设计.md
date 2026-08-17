# PVT-00：业务流量 Saved-Prefill 收益上限评估实施方案设计

> **验证 ID**：PVT-00  
> **验证名称**：业务流量 Saved-Prefill 收益上限与通信协议加速评估  
> **对应证据门**：**E0 立项充分性**  
> **证伪标记**：否（收益前提确认）  
> **建议周期**：4~6 人日  
> **主关联 IR**：`IR-02-11`, `IR-02-12`  
> **核心 SRS / SR23 锚点**：  
> - SRS: `L3-OB-PerPathTelemetry-047`, `L1-OB-SemanticMetrics-016`, `SE-MONITOR-001`, `SE-PERF-001`  
> - SR23: `SR23-02-11-01`, `SR23-02-11-02`, `SR23-02-12-01`, `SR23-02-12-05`  

---

## 1. 验证目标与交付结论定义

### 1.1 待验证核心命题
大模型推理中，输入预计算阶段（Prefill）计算量大、耗时长。本验证旨在通过受控 Benchmark 与底层网络 Micro-Benchmark 交叉比对，探明在不同复用率特征（30%、50%、70%、90%、98%）及不同长文本场景下：
1. 外接 KVCache 存储池相比纯算力重算（Recompute），能否取得明确的端到端 TTFT 净时间节省（Saved-Prefill）；
2. 采用 UBMEM 共享内存协议相比 URMA / 标准 RDMA 通信协议，在元数据处理与数据传输阶段带来的性能增益上限。

### 1.2 最终交付数据与结论产出
开发人员执行完本方案后，必须输出以下交付件：
1. **《URMA vs UBMEM 协议传输性能基准表》**（覆盖 4KB~64MB 包大小、1~64 并发）；
2. **《vLLM + Mooncake 前缀复用实测打点时延表》**（各阶段打点耗时）；
3. **《不同复用率与上下文长度下的 TTFT 收益交叉对账表与对比图》**；
4. **《Go / No-Go 判定结论》**：依据净收益公式计算是否满足 $\text{Saved-Prefill 净收益} \ge 2.0\times \text{总开销}$ 门槛。

---

## 2. 底层协议 Micro-Benchmark 构建方法

### 2.1 测试目标与工具
在物理裸机环境上，脱离推理框架，测量 **URMA** 与 **UBMEM** 两种通信协议在不同数据包大小与线程并发数下的单向/双向传输带宽与延迟基线。

### 2.2 压测工具构建与源码结构
本项验证涉及的全部底层测试与数据生成源码均存放在 `./原型验证代码/PVT-00/` 目录下：

```
原型验证代码/PVT-00/
├── proto_bench.cc        # 测量 URMA 与 UBMEM 底层协议带宽与时延的 C++ 微基准测试工具
├── Makefile             # 编译 proto_bench 的工程构建文件 (make -j16)
├── make_workload.py     # 构造具备 50%~98% 前缀复用率的 R1/R2 请求数据集生成脚本
└── traffic_generator.py # 受控发包与 TTFT/首 Token 时延采集的客户端驱动脚本
```

编译方法：
```bash
# 编译协议压测工具
cd ./原型验证代码/PVT-00 && make clean && make
```

### 2.3 测试参数网格
- **数据包大小 (Payload Size)**：4KB, 64KB, 256KB, 1MB, 4MB, 16MB, 64MB, 128MB；
- **并发线程数 (Thread Concurrency)**：1, 4, 16, 32, 64；
- **传输模式**：单向 Write、单向 Read、双向混流；
- **测试轮次**：每个组合预热 10 秒，正式测量 30 秒，重复 3 次取中位数。

### 2.4 数据输出格式 (`proto_benchmark_results.csv`)
```csv
protocol,payload_bytes,concurrency,direction,bandwidth_gbps,latency_avg_us,latency_p99_us
URMA,4096,1,write,3.2,12.5,18.2
UBMEM,4096,1,write,18.6,2.1,3.4
...
```

---

## 3. 业务 Benchmark 构造与流量特征编排

### 3.1 流量特征设计（以 50% 复用率为例）
构造由两批受控请求组成的请求序列：
- **请求 R1（前置预热请求）**：Prompt 长度 $L_1 = 50\text{K tokens}$，用于向 KVCache 存储池填充前缀；
- **请求 R2（复用测试请求）**：Prompt 长度 $L_2 = 100\text{K tokens}$，其中前 $50\text{K tokens}$ 与 R1 完全相同（Token ID 序列逐字一致），后 $50\text{K tokens}$ 为全新输入。

```
R1: [---------------- 50K Prefix A ----------------] -> Prefill & Store KV
R2: [---------------- 50K Prefix A ----------------][---------------- 50K New B ----------------]
                     ^--- 复用命中 (50%) ---^                       ^--- 本地重算 (50%) ---^
```

数据集生成命令：
```bash
python3 ./原型验证代码/PVT-00/make_workload.py --prefix-tokens 50000 --unique-tokens 50000 --out workload_50pct.json
```

### 3.2 发包器时序编排逻辑
驱动发包命令：
```bash
python3 ./原型验证代码/PVT-00/traffic_generator.py --workload workload_50pct.json --out run_50pct.log
```

时序编排逻辑：
1. **$T_0$ 时刻**：发包器向 vLLM 发送请求 R1；
2. **$T_0 \sim T_1$**：等待 R1 完成 Prefill 并将生成的 KVCache 异步写回外接存储池（Mooncake）；
3. **$T_1 \sim T_2$**：休眠 $T_{sleep} = 2.0\text{s}$（确保存储池元数据与前缀树完全建立完成）；
4. **$T_2$ 时刻**：发包器向 vLLM 发送请求 R2；
5. **$T_2 \sim T_3$**：记录 R2 的首 Token 输出时间（TTFT），并捕获底层传输统计。

---

## 4. 系统环境配置与插桩打点方案

### 4.1 隔离与控制变量配置
为了精确测量外接存储池的收益，**必须在 vLLM 上屏蔽本地 HBM KVCache 复用**，强制请求只能从外接 Mooncake 存储池中获取：
```bash
# vLLM 启动参数配置（禁用本地 Prefix Caching，启用外接存储池扩展插件）
python3 -m vllm.entrypoints.openai.api_server \
    --model /models/Qwen2.5-72B \
    --tensor-parallel-size 8 \
    --no-enable-prefix-caching \
    --kv-transfer-config '{"kv_connector": "MooncakeConnector", "kv_role": "kv_both"}' \
    --port 8000
```

### 4.2 关键路径插桩打点位置
在 vLLM 与 Mooncake 源码中植入高精度时钟打点（微秒级 `clock_gettime(CLOCK_MONOTONIC)`）：

| 打点标识 | 插桩组件与代码位置 | 测量含义 |
|---|---|---|
| `T_req_in` | vLLM API Server 入口 | 请求到达时间戳 |
| `T_lookup_start` | Mooncake Client 发起前缀查询 | 开始前缀元数据检索 |
| `T_lookup_end` | Mooncake Client 收到前缀匹配结果 | 前缀匹配建表与元数据返回 |
| `T_xfer_start` | 发起底层 KV 数据传输 | 开始拉取远程 KVCache |
| `T_xfer_end` | 远程 KV 数据拉取完成并落入显存 | 传输完成并 Ready |
| `T_prefill_start` | NPU Prefill Kernel 启动 | 开始计算未命中部分 (后 50K) |
| `T_first_token` | 生成第一个输出 Token | 首 Token 产出时间戳 |

---

## 5. 分步执行测试操作规程

开发人员请按以下 13 个具体步骤依次执行：

### 步骤 1：底层通信协议基准压测
运行底层 Benchmark，测量 URMA 与 UBMEM 在各包大小与并发下的性能：
```bash
./proto_bench --protocol urma --sizes 4K,64K,256K,1M,4M,16M,64M --threads 1,4,16,64 --out urma_bench.csv
./proto_bench --protocol ubmem --sizes 4K,64K,256K,1M,4M,16M,64M --threads 1,4,16,64 --out ubmem_bench.csv
```

### 步骤 2：构造 50% 复用率数据集
生成符合 R1 (50K), R2 (100K) 规范的 JSON 请求体：
```bash
python3 make_workload.py --prefix-tokens 50000 --unique-tokens 50000 --out workload_50pct.json
```

### 步骤 3：配置并启动 vLLM + Mooncake 服务
启动 vLLM 实例，屏蔽本地 HBM Cache，指定 Mooncake 为后端 KV 存储池。

### 步骤 4：启动发包器执行 R1 与 R2 测试
```bash
python3 traffic_generator.py --workload workload_50pct.json --sleep-sec 2.0 --log-file run_50pct.log
```

### 步骤 5：统计 R1 和 R2 的 TTFT 时延
从日志中提取 R1 与 R2 的端到端首 Token 时延：
- $TTFT_{R1} = T_{first\_token}(R1) - T_{req\_in}(R1)$（全量 50K Prefill + 存池）；
- $TTFT_{R2} = T_{first\_token}(R2) - T_{req\_in}(R2)$（前 50K 命中复用 + 后 50K 重算）。
- **判定标准**：若 $TTFT_{R2} < TTFT_{R1} \times 2$，说明复用生效。

### 步骤 6：统计 R2 KVCache 的实际传输大小
从 Mooncake 传输日志中读取实际传输的 KV Cache 字节数 $S_{kv\_bytes}$：
$$S_{kv\_bytes} = 2 \times N_{layers} \times H_{kv\_heads} \times D_{head} \times 50000 \times 2\text{ Bytes}$$
例如 Qwen2.5-72B ($N_L=80, H_{kv}=8, D_h=128, \text{FP16}$)，50K tokens 对应的总 KV 大小约为 $16.384\text{ GB}$（TP=8 单卡约 $2.048\text{ GB}$）。

### 步骤 7：比对底层协议传输时延，计算传输性能提升量
根据步骤 6 测得的 $S_{kv\_bytes}$，在步骤 1 的 `urma_bench.csv` 和 `ubmem_bench.csv` 中查找对应数据块大小的传输时延：
- $T_{xfer\_urma}(S_{kv\_bytes})$
- $T_{xfer\_ubmem}(S_{kv\_bytes})$
- 纯传输时延提升量：$\Delta T_{xfer} = T_{xfer\_urma} - T_{xfer\_ubmem}$

### 步骤 8：打点采集元数据与前缀匹配建表时延
从 `run_50pct.log` 中提取各打点项耗时：
- 元数据查询耗时：$T_{meta\_lookup} = T_{lookup\_end} - T_{lookup\_start}$
- 存储池建立前缀匹配表耗时：$T_{trie\_build}$
- 框架内部元数据准备与 Attach 耗时：$T_{attach}$

### 步骤 9：对 Mooncake 前缀元数据进行 UBMEM 共享内存改造并复测
将 Mooncake 默认基于 RPC/TCP 的元数据交互改造为基于 UBMEM 的共享内存/单边 Direct 映射：
1. 替换元数据通道为 `UBMEM_Trie_Client`；
2. 重新运行步骤 4，测得改造后的元数据查询耗时 $T_{meta\_ubmem}$。
3. 计算元数据优化提升量：$\Delta T_{meta} = T_{meta\_lookup} - T_{meta\_ubmem}$。

### 步骤 10：测量完全不复用 KVCache 的纯算力重算 TTFT 基线
关闭外接存储池，以纯算力重算模式向 vLLM 发送完整的 100K token 请求，记录完全不复用时的重算 TTFT 时延 $TTFT_{recompute\_100K}$。

### 步骤 11：交叉比对与端到端预期性能提升合成运算
根据前述步骤数据进行交叉合成（详见第 7 节运算公式）。

### 步骤 12：扩展不同复用率特征场景（30%、70%、90%、98%）
保持总 Prompt 长度 100K 不变，仿照步骤 2~11 分别构造：
- 30% 复用率：R1 30K, R2 100K（共享前 30K）；
- 70% 复用率：R1 70K, R2 100K（共享前 70K）；
- 90% 复用率：R1 90K, R2 100K（共享前 90K）；
- 98% 复用率：R1 98K, R2 100K（共享前 98K）。

### 步骤 13：扩展不同模型架构
切换不同模型配置重复上述流程：
- 模型 1：**Qwen2.5-72B** (Grouped-Query Attention, GQA)；
- 模型 2：**Llama-3.1-70B** (GQA)；
- 模型 3：**DeepSeek-V3** (Multi-Head Latent Attention, MLA，KV 压缩态)。

---

## 6. 数据采集清单与记录格式

### 6.1 原始测试数据记录表 (`pvt00_raw_measurements.csv`)
| 字段名称 | 含义 | 单位 | 示例值 |
|---|---|---|---|
| `model_name` | 测试模型名称 | 字符串 | Qwen2.5-72B |
| `reuse_ratio` | 前缀复用率 | 百分比 | 50% |
| `prefix_tokens` | 复用前缀 Token 数 | 个 | 50000 |
| `total_tokens` | R2 总 Prompt 长度 | 个 | 100000 |
| `kv_bytes_per_rank` | 单卡实际传输 KV 大小 | MB | 2048 |
| `ttft_recompute` | 100K 纯重算耗时 | ms | 12500.0 |
| `t_meta_raw` | 改造前元数据处理时延 | ms | 35.0 |
| `t_meta_ubmem` | UBMEM 改造后元数据时延 | ms | 1.8 |
| `t_xfer_urma` | URMA 传输 KV 实际耗时 | ms | 420.0 |
| `t_xfer_ubmem` | UBMEM 传输 KV 实际耗时 | ms | 185.0 |
| `t_prefill_remaining`| 剩余 50K Token Prefill 重算耗时 | ms | 6200.0 |
| `ttft_actual_r2` | R2 真实测量端到端 TTFT | ms | 6410.0 |

---

## 7. 数据交叉组合与运算推导逻辑

### 7.1 理论 Saved-Prefill 节省时间
$$T_{saved\_theoretical} = TTFT_{recompute\_100K} - \left( T_{meta} + T_{transfer} + T_{prefill\_remaining} \right)$$

### 7.2 通信协议增益对比
1. **URMA 协议下的端到端 TTFT 预期**：
   $$TTFT_{urma\_est} = T_{meta\_raw} + T_{xfer\_urma} + T_{prefill\_remaining}$$
2. **UBMEM 协议下的端到端 TTFT 预期**：
   $$TTFT_{ubmem\_est} = T_{meta\_ubmem} + T_{xfer\_ubmem} + T_{prefill\_remaining}$$
3. **UBMEM 带来的综合加速增益**：
   $$\Delta TTFT_{boost} = TTFT_{urma\_est} - TTFT_{ubmem\_est} = (T_{meta\_raw} - T_{meta\_ubmem}) + (T_{xfer\_urma} - T_{xfer\_ubmem})$$

### 7.3 收益开销比 (Benefit-to-Overhead Ratio)
$$\text{Benefit Ratio} = \frac{T_{recompute}(S_{prefix})}{T_{meta} + T_{transfer} + T_{attach}}$$
- 当 $\text{Benefit Ratio} > 2.0\times$ 时，判定该复用率具有显著立项正价值。

---

## 8. 多维扩展与扫参矩阵

| 维度 | 参数网格 |
|---|---|
| **前缀复用率** | 30%, 50%, 70%, 90%, 98% |
| **总上下文长度** | 8K, 32K, 64K, 128K, 256K |
| **模型规格** | Qwen2.5-72B (GQA), Llama-3.1-70B (GQA), DeepSeek-V3 (MLA) |
| **并发度** | 1 并发（单流时延极限）, 16 并发（高负载吞吐） |

---

## 9. Go / No-Go 判定规则与交付报告模板

### 9.1 判定规则
- **Go 门槛**：
  1. 在 $\ge 50\%$ 复用率下，UBMEM 改造后的 $TTFT_{actual}$ 相比纯重算下降 $\ge 35\%$；
  2. $\text{Benefit Ratio} \ge 2.0\times$；
  3. UBMEM 相比 URMA 在元数据 + 数据传输总时延上下降 $\ge 40\%$。
- **No-Go 门槛**：
  - 复用后的端到端 TTFT 反而慢于纯重算（发生负收益命中）；
  - 控制/元数据/传输总开销吞噬了 $\ge 70\%$ 的算力节省收益。

### 9.2 开发者交付报告格式模板
```markdown
# PVT-00 提前验证交付报告

## 1. 核心实测数据汇总表 (以 Qwen2.5-72B 为例)
| 复用率 | 纯重算TTFT(ms) | URMA端到端(ms) | UBMEM端到端(ms) | 净收益(ms) | 收益开销比 | 判定 |
|---|---|---|---|---|---|---|
| 30% | 12500 | 9200 | 8850 | 3650 | 2.4x | PASS |
| 50% | 12500 | 6655 | 6386 | 6114 | 3.5x | PASS |
| 70% | 12500 | 4120 | 3820 | 8680 | 5.2x | PASS |
| 90% | 12500 | 1680 | 1310 | 11190 | 9.8x | PASS |
| 98% | 12500 | 720 | 315 | 12185 | 18.5x | PASS |

## 2. 协议加速贡献拆解
- 元数据时延优化：从 35.0ms 降至 1.8ms (下降 94.8%)
- 数据传输时延优化：单卡 2GB KV 传输从 420ms 降至 185ms (加速 2.27x)

## 3. 最终结论
【Go / Conditional / No-Go】: GO
```
