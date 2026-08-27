# PVT-00：业务流量 Saved-Prefill 收益上限评估实施方案设计
## —— Mooncake 原生传输开销定位与国产通信协议加速上限评估

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。三种模式必须绑定独立代码包或配置；缓存就绪以事件为准；本地重算基线必须来自实测请求。

> **验证 ID**：PVT-00  
> **验证名称**：业务流量 Saved-Prefill 收益上限与通信协议加速评估  
> **验证优先级**：**🟡 P1 级（底座支撑项）**  
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

## 0. 架构导读与传统软件系统视角切入

### 0.1 传统软件视角下的“计算 vs 缓存”权衡
在传统系统架构（如 Web 后端、数据库缓存层）中，当某个计算过程非常昂贵时，我们通常会引入缓存（例如 Redis / Memcached 缓存复杂 SQL 查询结果）。
但是，**引入缓存并不总是划算的**：如果从缓存拉取数据的网络时延和反序列化开销，比本地直接重新计算一次还要慢，那么缓存就带来了严重的“负收益”。

### 0.2 大模型推理中的对应物理场景
大模型在生成第一个字之前，需要对用户输入的 Prompt 提示词进行**首字预计算（Prefill，输入理解阶段）**。这个过程本质是海量矩阵乘法，极度消耗 NPU/GPU 算力，导致**首字生成延迟（TTFT, Time To First Token，首字响应时间）**极高（长文本下可达数秒）。
- **什么是 KVCache？** 大模型在 Prefill 阶段会为每个 Token 生成 Key 和 Value 特征张量（KVCache，大模型注意力键值缓存：大模型自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免后续 Token 生成时重复计算注意力）。
- **什么是 Saved-Prefill？** 如果后续请求带有相同的提示词前缀（如系统提示词、知识库文档、多轮历史对话），我们可以把之前算好的 KVCache 从外接存储池直接拉回显存，**跳过耗时的矩阵重算**，这就是 **Saved-Prefill（首字生成预计算节省）**。

### 0.3 本验证的核心使命
在编写复杂的分布式调度系统之前，必须通过**最精简的微基准测试（Micro-Benchmark）**，实测回答系统底线问题：
> **“在不同的前缀复用率（30%~98%）下，通过网络拉取 KVCache 到底能不能跑赢本地 NPU 重新计算？原厂 UBMEM 协议相比通用 URMA/RDMA 协议能带来多大的加速物理上限？”**

---

## 1. 验证目标与交付结论定义

### 1.1 现实前因痛点与待验证核心命题
1. **现实痛点**：开源 Mooncake 在拉取 KVCache 时存在元数据查询与传输开销。如果网络带宽不足或传输协议低效，拉取开销将超过算力重算耗时，导致业务变慢；
2. **核心命题**：
   - 针对 **DeepSeek MLA (~35KB/tok)** 与 **Qwen MHA (320KB/tok)** 两类典型模型，测定外接存储池相比纯算力本地重算（Recompute）的端到端 TTFT 净时间节省；
   - 采用 **UBMEM（统一总线内存直通共享协议）** 相比 **URMA（通用远程直接内存访问）** / 标准 RDMA 通信协议，在元数据处理与数据传输阶段带来的性能增益上限。

### 1.2 最终交付数据与结论产出
1. **《URMA vs UBMEM 协议传输性能基准表》**（覆盖 4KB~64MB 包大小、1~64 并发）；
2. **《vLLM + Mooncake 前缀复用实测打点时延表》**（包含源码插桩微秒级打点）；
3. **《MLA vs MHA 双模型不同复用率下的 TTFT 收益交叉对账表与对比图》**；
4. **《Go / No-Go 判定结论》**：依据净收益公式计算是否满足 $\text{Saved-Prefill 净收益} \ge 2.0\times \text{总开销}$ 门槛。

---

## 2. 底层协议 Micro-Benchmark 构建方法

### 2.1 测试目标与底层 SDK 依赖规范
在物理裸机环境上，脱离上层推理框架，使用 C++ 驱动直接测量 **URMA** 与 **UBMEM** 两种通信协议在不同数据包大小与线程并发数下的单向/双向传输带宽与延迟基线。

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
├── make_workload.py     # 构造具备 30%~98% 前缀复用率的请求数据集生成脚本 (支持 MLA/MHA)
└── traffic_generator.py # 受控发包与 TTFT/首 Token 时延采集的客户端驱动脚本 (支持 A/B 模式)
```

编译方法：
```bash
cd ./原型验证代码/PVT-00 && make clean && make -j16
```

---

## 3. 业务 Benchmark 构造与流量特征编排

### 3.1 流量特征设计原理（为什么需要合成多档复用率？）
在真实大模型业务中，用户请求的前缀复用率（Prefix Reuse Ratio）差异巨大：
- **低复用场景（30%）**：多轮对话早期，用户问题占主体，公共上下文较少；
- **标准复用场景（50%~70%）**：带长文档上下文的知识库问答（RAG），文档内容固定，用户提问不同；
- **高复用场景（90%~98%）**：超长系统角色设定或固定的法律/代码库分析。

为了精准测定不同业务场景下的加速效果，`make_workload.py` 通过以下时序构造合成数据集：
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
python3 ./原型验证代码/PVT-00/make_workload.py --model-type mha --model-id Qwen2.5-72B --layout-manifest runtime_layout_qwen.json --prefix-tokens 50000 --unique-tokens 50000 --out workload_mha_50pct.json

# 生成 DeepSeek MLA (~35KB/tok) 模型数据集
python3 ./原型验证代码/PVT-00/make_workload.py --model-type mla --model-id DeepSeek-V3 --layout-manifest runtime_layout_deepseek.json --prefix-tokens 50000 --unique-tokens 50000 --out workload_mla_50pct.json
```

---

## 4. 系统环境配置与源码行级插桩打点方案

### 4.1 实验环境、测试模型基线与源码版本锁定
- **模型权重基线路径**：
  - 主测 Dense 模型：`/models/Qwen/Qwen2.5-72B-Instruct`（FP16，80 层，GQA 分组查询注意力 $H_{kv}=8$, $D_{head}=128$，单 Token KV 大小为 $320\text{ KB/Token}$，张量并行 TP=8 单卡 $40\text{ KB/Token}$）；
  - 主测 MLA 压缩态模型：`/models/deepseek-ai/DeepSeek-V3`（FP8 MLA，61 层，潜变量 512 + RoPE 64，单 Token KV 显存占用约为 $35\text{ KB/Token}$，张量并行 TP=8 单卡约为 $4.38\text{ KB/Token}$）；
- **推理引擎与存储组件版本锁定**：
  - `vLLM`：Commit: `842dd8fd96650063e1ad32e6075742d457d39773`；
  - `vLLM-Ascend`：Commit: `424e27e1fd2b1c6e0d7fe659b489b87c1223a33c`；
  - `Mooncake`：Commit: `f90ae691f109e49a60920e0c8abbf7e572826d8c`。

### 4.2 关键路径行级插桩打点位置与测量意图

为了精确将请求时延拆解为“网络传输耗时”、“元数据查询耗时”与“算力计算耗时”，我们在推理引擎关键代码行注入了 6 个微秒级时钟打点：

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        源码行级时钟打点与时序拆解全景图                                │
├───────────────────┬───────────────────────────────────┬────────────────────────────────┤
│ 打点标识          │ 插桩文件与具体函数位置            │ 测量物理含义与意图             │
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
**时延拆解公式**：
- 元数据开销：$T_{\text{dir\_query}} = T_{\text{lookup\_end}} - T_{\text{lookup\_start}}$
- 数据搬运开销：$T_{\text{data\_load}} = T_{\text{xfer\_end}} - T_{\text{xfer\_start}}$
- 首字生成延迟：$\text{TTFT} = T_{\text{first\_token}} - T_{\text{req\_in}}$

---

## 5. 分步执行测试操作规程 (SOP)

开发人员在执行 PVT-00 时，请严格按照以下 6 个步骤逐步执行，并理解每一步的操作意图：

### Step 1：编译底层通信 Benchmark 并建立通信基线
- **操作意图**：脱离上层 Python 与推理框架，在纯 C++ 裸机环境下测定网卡在 1MB~64MB 大包传输时的真实物理线速与单包时延，作为后续分析的理论天花板。
- **执行命令**：
```bash
cd ./原型验证代码/PVT-00 && make clean && make -j16
./proto_bench --protocol urma --payload-bytes 1048576 --concurrency 4 --iters 1000 --out res_proto_baseline.csv
```

### Step 2：生成受控多模型测试数据集
- **操作意图**：使用 `make_workload.py` 分别针对 Qwen MHA (320KB/tok) 和 DeepSeek MLA (35KB/tok) 生成 30%~98% 五档复用率的请求，确保测试输入完全受控且可复现。
- **执行命令**：
```bash
# 生成 Qwen MHA (320KB/tok) 50% 复用率测试集
python3 ./make_workload.py --model-type mha --model-id Qwen2.5-72B --layout-manifest runtime_layout_qwen.json --prefix-tokens 50000 --unique-tokens 50000 --out workload_mha_50pct.json

# 生成 DeepSeek MLA (~35KB/tok) 50% 复用率测试集
python3 ./make_workload.py --model-type mla --model-id DeepSeek-V3 --layout-manifest runtime_layout_deepseek.json --prefix-tokens 50000 --unique-tokens 50000 --out workload_mla_50pct.json
```

### Step 3：一键拉起开源基线服务集群 (Mooncake + vLLM)
- **操作意图**：启动包含 2 个节点的官方原生 Mooncake + vLLM 推理服务集群，验证基线配置下的端点可用性。
- **执行命令**：
```bash
cd ../deploy_and_bench_e2e && EVIDENCE_ENVIRONMENT=W0 bash ./deploy_cluster.sh
```

### Step 4：发起在线打流并采集原生基线时延
- **操作意图**：向官方原生 Mooncake 发起打流压测，记录在各个复用率下的 TTFT 与源码插桩时延，作为对照基线。
- **执行命令**：
```bash
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
- **操作意图**：在相同硬件与数据集下，将通信驱动切换为 UBMEM 统一总线直通协议，验证原厂软硬件协同对拉取延迟的进一步压缩。
- **执行命令**：
```bash
MODE=unified_full RUN_ID=run_pvt00_01 PACKAGE_ID=pkg_pvt00 CONFIG_HASH=hash01 HARDWARE_PROFILE=hw_2node TOPOLOGY_PROFILE=topo_p2p WORKLOAD_ID=workload_mha_50pct MODEL_ID=Qwen2.5-72B EVIDENCE_LEVEL=LAB bash ../deploy_and_bench_e2e/run_online_benchmark.sh
```

### Step 6：自动解析指标并输出对账表
- **操作意图**：解析测试输出的原始 JSON 文件，计算各复用率下的 Saved-Prefill 净收益与 UBMEM 加速比，输出标准化汇总表。
- **执行命令**：
```bash
python3 ../deploy_and_bench_e2e/parse_benchmark_metrics.py \
    --results-dir ../deploy_and_bench_e2e/results/unified_full/run_pvt00_01 \
    --output ../deploy_and_bench_e2e/results/unified_full/run_pvt00_01/summary.csv \
    --output-json ../deploy_and_bench_e2e/results/unified_full/run_pvt00_01/summary.json \
    --mode unified_full --run-id run_pvt00_01 --package-id pkg_pvt00 --config-hash hash01 \
    --hardware-profile hw_2node --topology-profile topo_p2p --workload-id workload_mha_50pct \
    --model-id Qwen2.5-72B --evidence-level LAB
```

---

## 6. 数据采集清单与记录格式

### 6.1 端到端请求时延表 (`pvt00_e2e_results.csv`)
```csv
run_id,workload_id,model_id,model_type,kv_bytes_per_token,prefix_tokens,total_r2_tokens,reuse_ratio,mode,protocol,actual_path,package_id,config_hash,hardware_profile,evidence_level,ttft_ms,recompute_ttft_ms,net_saved_ms,status
run01,w01,Qwen2.5-72B,mha,327680,50000,100000,0.5,mooncake_native,URMA,remote_urma,pkg01,hash01,hw_2n,LAB,38.5,125.0,86.5,OK
run02,w02,DeepSeek-V3,mla,35840,50000,100000,0.5,unified_full,UBMEM,remote_ubmem,pkg02,hash02,hw_2n,LAB,14.2,65.0,50.8,OK
```

---

## 7. 数据交叉组合与运算推导逻辑

### 7.1 Saved-Prefill 净收益计算公式
$$\text{Saved-Prefill 净收益} = T_{\text{pure\_recompute}} - T_{\text{kv\_transfer\_and\_attach}}$$
$$\text{总传输与挂接开销} = T_{\text{dir\_query}} + T_{\text{data\_load}} + T_{\text{attach\_and\_sync}}$$
- **物理意义**：净收益必须显著大于 0。若净收益为负，说明从网络拉取比本地重算还慢，发生负加速！

### 7.2 判定通过条件
$$\text{Saved-Prefill 净收益} \ge 2.0 \times \text{总传输与挂接开销}$$
$$\text{UBMEM 相比 URMA 加速比} = \frac{T_{\text{URMA\_total}}}{T_{\text{UBMEM\_total}}} \ge 1.30\times$$

---

## 8. 多维扩展与扫参矩阵

| 维度 | 取值范围 | 测试目的与意图 |
|---|---|---|
| **前缀复用率** | 30%, 50%, 70%, 90%, 98% | 探明不同业务场景下的盈亏平衡临界点（临界复用率） |
| **上下文总长** | 8K, 32K, 64K, 128K, 256K | 评估超长文本下的传输带宽与显存扩展瓶颈 |
| **模型架构** | Dense (Qwen2.5-72B) vs MLA (DeepSeek-V3) | 验证不同 KV 尺寸密度下的协议加速特性与传输开销占比 |
| **并发度** | 1, 4, 16, 32, 64 并发请求 | 检验高并发下存储池吞吐与队列反压表现 |

---

## 9. Go / Conditional / No-Go 判定规则

- **Go (准入通过)**：在 $\ge 50\%$ 复用率下，Saved-Prefill 净收益 $\ge 2.0\times$ 总开销，且 UBMEM 协议加速比 $\ge 1.30\times$；
- **Conditional (条件准入)**：仅在 $\ge 70\%$ 高复用率或超长文本（$\ge 64\text{K}$）下满足收益要求，后续系统需设定场景白名单准入；
- **No-Go (否决关闭)**：在全部复用率下加载总开销均大于本地直接重算耗时（净收益为负），判定外接 KVCache 方案不成立。

---

## 10. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 10.1 研发任务拆解与分工
- **工程师职责**：
  1. 部署测试机环境并准备模型权重路径；
  2. 按照第 5 节的 6 个步骤依次执行打流；
  3. 观察客户端输出的 TTFT 数值并检查是否达成 2 倍净收益；
- **AI Agent 职责**：
  1. 检查 Makefile 编译依赖并配置正确的驱动路径；
  2. 自动生成多复用率数据集生成脚本 `make_workload.py`；
  3. 编写 Python 脚本自动计算 Saved-Prefill 净收益与绘制加速比曲线。

### 10.2 专属 Prompt 模板（可直接复制给 AI Agent）
```text
我是一名系统开发工程师，正在开展 PVT-00 原型验证（评估 KVCache 缓存拉取收益与 UBMEM 加速上限）：
1. 请阅读 ./原型验证代码/PVT-00/proto_bench.cc 与 Makefile；
2. 确保 proto_bench.cc 正确调用了 urma_write / ubmem_write 接口，并在 4KB 到 64MB 多种包大小下进行计时；
3. 为我生成一个自动化测试脚本 run_proto_bench.sh，依次测试 1、4、16、32 线程并发下的吞吐与延迟；
4. 运行 make_workload.py 生成包含 30%、50%、70%、90%、98% 五档复用率的 ShareGPT 测试数据集；
5. 按照第 5 节 SOP 步骤执行在线打流，测试完成后编写 Python 脚本解析原始日志，计算净收益公式并生成 pvt00_results.csv。
```

### 10.3 常见排错指南
- **找不到 liburma.so / libubmem.so**：若当前机器没有原厂驱动，可让 Agent 增加 `-DMOCK_DRIVER` 编译宏，在单机内存中模拟传输时延，优先打通数据流与上层逻辑；
- **权重文件缺失**：若缺少 Qwen-72B 本地权重，可使用轻量 Qwen2.5-7B 或通过 `--dummy-weights` 启动虚构权重进行吞吐流控测试。
