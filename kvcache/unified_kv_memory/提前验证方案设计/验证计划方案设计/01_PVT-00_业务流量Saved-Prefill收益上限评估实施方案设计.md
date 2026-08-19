# PVT-00：业务流量 Saved-Prefill 收益上限评估实施方案设计
## —— Mooncake 原生传输开销瓶颈穿刺与国产专属协议加速上限评估

> **验证 ID**：PVT-00  
> **验证名称**：业务流量 Saved-Prefill 收益上限与通信协议加速评估  
> **穿刺优先级**：**🟡 P1 级（底座支撑项）**  
> **对应验证阶段**：**E0 业务收益前提确认**  
> **证伪标记**：否（业务收益前提确认）  
> **建议周期**：4~6 人日  
> **主关联 IR**：`IR-02-11`, `IR-02-12`  
> **核心 SRS / SR23 锚点**：  
> - SRS: `L3-OB-PerPathTelemetry-047`, `L1-OB-SemanticMetrics-016`, `SE-MONITOR-001`, `SE-PERF-001`  
> - SR23: `SR23-02-11-01`, `SR23-02-11-02`, `SR23-02-12-01`, `SR23-02-12-05`  
> **开源基线版本与代码仓库**：  
> - **Mooncake**：[`https://github.com/kvcache-ai/Mooncake.git`](https://github.com/kvcache-ai/Mooncake.git) (Commit: `f90ae691f109e49a60920e0c8abbf7e572826d8c`，涵盖 `mooncake-transfer-engine`, `mooncake-integration`)  
> - **vLLM**：[`https://github.com/vllm-project/vllm.git`](https://github.com/vllm-project/vllm.git) (Commit: `842dd8fd96650063e1ad32e6075742d457d39773`)  
> - **vLLM-Ascend**：[`https://github.com/vllm-project/vllm-ascend.git`](https://github.com/vllm-project/vllm-ascend.git) (Commit: `424e27e1fd2b1c6e0d7fe659b489b87c1223a33c`)  
> **研发对齐状态**：已闭环研发评估报告 1, 12, 13 项（明确驱动 SDK、模型基准与行级源码插桩位置）  

---

## 1. 验证目标与交付结论定义

### 1.1 待验证核心命题
大模型推理中，输入预计算阶段（Prefill，即首字生成前的 Prompt 计算）计算量大、耗时长。本验证旨在通过受控基准测试与底层网络微基准测试交叉比对，探明在不同前缀复用率（30%、50%、70%、90%、98%）及不同长文本场景下：
1. 穿刺 Mooncake 原生数据传输与框架接入在端到端流程中的耗时占比与性能瓶颈；
2. 针对 **DeepSeek MLA (FP8, 512B/tok)** 与 **Qwen MHA (FP16, 320KB/tok)** 两种截然不同的 KV 密度模型，测定外接存储池相比纯算力本地重算（Recompute）的端到端 TTFT（Time To First Token，首字生成延迟）净时间节省（即 Saved-Prefill 收益）；
3. 采用 UBMEM（统一总线内存直通共享协议）相比 URMA（通用远程直接内存访问）/ 标准 RDMA 通信协议，在元数据处理与数据传输阶段带来的性能增益上限。

### 1.2 最终交付数据与结论产出
开发人员执行完本方案后，必须输出以下交付件：
1. **《URMA vs UBMEM 协议传输性能基准表》**（覆盖 4KB~64MB 包大小、1~64 并发）；
2. **《vLLM + Mooncake 前缀复用实测打点时延表》**（包含源码插桩微秒级打点）；
3. **《MLA vs MHA 双模型不同复用率下的 TTFT 收益交叉对账表与对比图》**；
4. **《Go / No-Go 判定结论》**：依据净收益公式计算是否满足 $\text{Saved-Prefill 净收益} \ge 2.0\times \text{总开销}$ 门槛。

---

## 2. 底层协议 Micro-Benchmark 构建方法

### 2.1 测试目标与底层 SDK 依赖规范
在物理裸机环境上，脱离推理框架，测量 **URMA** 与 **UBMEM** 两种通信协议在不同数据包大小与线程并发数下的单向/双向传输带宽与延迟基线。

#### 核心 C/C++ 驱动头文件与链接库规范：
```cpp
// 引入原厂标准通信驱动 SDK
#include <urma.h>        // URMA 用户态 Verbs API 头文件
#include <ubmem.h>       // UBMEM 统一总线内存直通 API 头文件
#include <infiniband/verbs.h>
#include <pthread.h>
#include <time.h>
```
- **库文件路径**：`/usr/lib64/liburma.so`, `/usr/lib64/libubmem.so`
- **GCC/Clang 编译链接参数**：`-lurma -lubmem -lpthread -O3 -march=native`

### 2.2 压测工具构建与源码结构
本项验证涉及的全部底层测试与数据生成源码存放在 `./原型验证代码/PVT-00/` 目录下：

```
原型验证代码/PVT-00/
├── proto_bench.cc        # 测量 URMA 与 UBMEM 底层协议带宽与时延的 C++ 微基准测试工具
├── Makefile             # 编译 proto_bench 的工程构建文件 (make -j16)
├── make_workload.py     # 构造具备 50%~98% 前缀复用率的 R1/R2 请求数据集生成脚本 (支持 MLA/MHA)
└── traffic_generator.py # 受控发包与 TTFT/首 Token 时延采集的客户端驱动脚本 (支持 A/B 模式)
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

数据集生成命令（支持指定模型类型）：
```bash
# 生成 Qwen MHA 模型数据集 (320KB/tok)
python3 ./原型验证代码/PVT-00/make_workload.py --model-type mha --prefix-tokens 50000 --unique-tokens 50000 --out workload_mha_50pct.json

# 生成 DeepSeek MLA 模型数据集 (512B/tok)
python3 ./原型验证代码/PVT-00/make_workload.py --model-type mla --prefix-tokens 50000 --unique-tokens 50000 --out workload_mla_50pct.json
```

### 3.2 发包器时序编排逻辑
驱动发包命令：
```bash
python3 ./原型验证代码/PVT-00/traffic_generator.py --workload workload_mha_50pct.json --out run_50pct.log
```

---

## 4. 系统环境配置与源码行级插桩打点方案

### 4.1 实验环境、测试模型基线与源码版本锁定
- **模型权重基线路径**：
  - 主测 Dense 模型：`/models/Qwen/Qwen2.5-72B-Instruct`（FP16，80 层，GQA 分组查询注意力 $H_{kv}=8$, $D_{head}=128$，单 Token KV 大小为 $320\text{ KB/Token}$，张量并行 TP=8 单卡 $40\text{ KB/Token}$）；
  - 主测 MLA 压缩态模型：`/models/deepseek-ai/DeepSeek-V3`（FP8 MLA 多头潜变量注意力，$D_{latent}=512$，单 Token 仅 $512\text{ Bytes}$）；
  - 备选长文模型：`/models/meta-llama/Llama-3.1-70B-Instruct`（FP16/FP8）。
- **推理引擎与存储组件版本锁定**：
  - `vLLM`：Commit: `842dd8fd96650063e1ad32e6075742d457d39773`；
  - `vLLM-Ascend`：Commit: `424e27e1fd2b1c6e0d7fe659b489b87c1223a33c`；
  - `Mooncake`：Commit: `f90ae691f109e49a60920e0c8abbf7e572826d8c`。

### 4.2 隔离与控制变量启动参数
```bash
# vLLM 启动参数配置（禁用本地 Prefix Caching，启用外接存储池扩展插件）
python3 -m vllm.entrypoints.openai.api_server \
    --model /models/Qwen/Qwen2.5-72B-Instruct \
    --tensor-parallel-size 8 \
    --no-enable-prefix-caching \
    --kv-transfer-config '{"kv_connector": "MooncakeConnector", "kv_role": "kv_both"}' \
    --port 8000
```

### 4.3 关键路径行级插桩打点位置 (C/C++ & Python)

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        源码行级时钟打点与时序拆解全景图                                │
├───────────────────┬───────────────────────────────────┬────────────────────────────────┤
│ 打点标识          │ 插桩文件与具体函数位置            │ 测量物理含义                   │
├───────────────────┼───────────────────────────────────┼────────────────────────────────┤
│ T_req_in          │ vllm/entrypoints/openai/api_server│ HTTP 接口层接收到请求时间戳    │
│ T_lookup_start    │ vllm/core/scheduler.py:schedule() │ 调度器发起前缀元数据查询       │
│ T_lookup_end      │ mooncake/src/connector.cc:get_kv()│ Mooncake 匹配返回命中 Block 链 │
│ T_xfer_start      │ mooncake/src/connector.cc:xfer()  │ 发起底层 URMA/UBMEM DMA 传输   │
│ T_xfer_end        │ mooncake/src/connector.cc:wait()  │ 数据到达 NPU HBM 并完成 Fence  │
│ T_prefill_start   │ vllm/worker/worker.py:execute()   │ NPU 启动未命中部分 Prefill 算子│
│ T_first_token     │ vllm/worker/worker.py:sample()    │ 产出第一个 Token 并准备流式回包│
└───────────────────┴───────────────────────────────────┴────────────────────────────────┘
```

---

## 5. 分步执行测试操作规程 (SOP)

开发人员请按以下完整开源软件部署与在线打流规程执行：

### Step 1：编译底层通信 Benchmark 并建立通信基线
```bash
cd ./原型验证代码/PVT-00 && make clean && make -j16
./proto_bench --iters 1000 --out res_proto_baseline.csv
```

### Step 2：生成受控多模型数据集
```bash
# 生成 Qwen MHA (320KB/tok) 50% 复用率测试集
python3 ./make_workload.py --model-type mha --prefix-tokens 50000 --unique-tokens 50000 --out workload_mha_50pct.json

# 生成 DeepSeek MLA (512B/tok) 50% 复用率测试集
python3 ./make_workload.py --model-type mla --prefix-tokens 50000 --unique-tokens 50000 --out workload_mla_50pct.json
```

### Step 3：一键拉起开源基线服务集群 (Mooncake + vLLM)
```bash
# 启动 Mooncake Master、Prefill 实例 (端口 8100)、Decode 实例 (端口 8200) 与 PD 代理 (端口 8000)
cd ../deploy_and_bench_e2e && bash ./deploy_cluster.sh
```

### Step 4：发起 vLLM benchmark_serving 在线打流
```bash
# 发送 ShareGPT / 合成流量，采集基线在线请求时延与 TTFT
python3 -m vllm.benchmarks.benchmark_serving \
    --backend vllm \
    --model /models/Qwen/Qwen2.5-72B-Instruct \
    --dataset-name sharegpt \
    --dataset-path ./ShareGPT_V3_unfiltered_cleaned_split.json \
    --num-prompts 200 \
    --request-rate 10 \
    --port 8000 \
    --save-result \
    --result-filename ./results/bench_serving_mha_native.json
```

### Step 5：切换为 Unified KV (UBMEM 零拷贝扩展版) 重复打流消融
```bash
# 切换为 UBMEM 驱动与 C++ 描述符扩展版，重新打流
bash ./run_online_benchmark.sh
```

### Step 6：自动解析指标并计算 Saved-Prefill 净收益
```bash
python3 ./parse_benchmark_metrics.py --results-dir ./results --output ./results/pvt00_e2e_results.csv
```

---

## 6. 数据采集清单与记录格式

### 6.1 端到端请求时延表 (`pvt00_e2e_results.csv`)
```csv
workload_id,model_name,model_arch,prefix_len,total_len,reuse_pct,mode,protocol,ttft_ms,pure_recompute_ms,dir_query_ms,data_load_ms,attach_ms,net_saved_ms
WK-01,Qwen2.5-72B,MHA,50000,100000,50,mooncake_native,URMA,82.4,145.2,3.1,42.8,4.5,62.8
WK-02,Qwen2.5-72B,MHA,50000,100000,50,unified_kv,UBMEM,56.2,145.2,0.8,21.3,1.2,89.0
WK-03,DeepSeek-V3,MLA,50000,100000,50,unified_kv,UBMEM,28.4,98.6,0.6,3.2,0.8,70.2
```

---

## 7. 数据交叉组合与运算推导逻辑

### 7.1 Saved-Prefill 净收益计算公式
$$\text{Saved-Prefill 净收益} = T_{\text{pure\_recompute}} - T_{\text{kv\_transfer\_and\_attach}}$$
$$\text{总传输与挂接开销} = T_{\text{dir\_query}} + T_{\text{data\_load}} + T_{\text{attach\_and\_sync}}$$

### 7.2 判定通过条件
$$\text{Saved-Prefill 净收益} \ge 2.0 \times \text{总传输与挂接开销}$$
$$\text{UBMEM 相比 URMA 加速比} = \frac{T_{\text{URMA\_total}}}{T_{\text{UBMEM\_total}}} \ge 1.30\times$$

---

## 8. 多维扩展与扫参矩阵

| 维度 | 取值范围 | 测试目的 |
|---|---|---|
| **前缀复用率** | 30%, 50%, 70%, 90%, 98% | 探明不同业务场景下的盈亏平衡临界点 |
| **上下文总长** | 8K, 32K, 64K, 128K, 256K | 评估超长文本下的传输带宽与显存扩展瓶颈 |
| **模型架构** | Dense (Qwen2.5-72B) vs MLA (DeepSeek-V3) | 验证不同 KV 尺寸密度下的协议加速特性 |
| **并发度** | 1, 4, 16, 32, 64 并发请求 | 检验高并发下存储池吞吐与队列反压表现 |

---

## 9. Go / Conditional / No-Go 判定规则

- **Go (准入通过)**：在 $\ge 50\%$ 复用率下，Saved-Prefill 净收益 $\ge 2.0\times$ 总开销，且 UBMEM 协议加速比 $\ge 1.30\times$；
- **Conditional (条件准入)**：仅在 $\ge 70\%$ 高复用率或超长文本（$\ge 64\text{K}$）下满足收益要求，后续系统需设定场景白名单准入；
- **No-Go (否决关闭)**：在全部复用率下加载总开销均大于本地直接重算耗时（净收益为负），判定外接 KVCache 方案不成立。
