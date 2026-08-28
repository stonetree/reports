# PVT-00：业务流量 Saved-Prefill 收益上限评估实施方案设计
## —— Mooncake 原生传输开销定位与通信协议加速上限评估

> **公共执行契约**：本验证项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每次执行需通过唯一的 `run_id` 固化测试负载、代码包版本、模型物理布局、硬件拓扑、并发度、预热轮次、样本容量及证据等级；实测结果完整保留逐请求原始样本、异常日志、实际物理路径与状态判定结论。

> **验证范围声明**：当前受控工程中的 `proto_bench.cc` 仅在本地内存空间执行 `memcpy` 拷贝流程，尚未调用底层 URMA 或 UBMEM 驱动；`make_workload.py` 生成的是用于流程验证的随机 Token ID 合成负载，并非真实大模型端到端推理数据；`traffic_generator.py` 支持向实际 HTTP 端点发送 R1/R2 序列请求，但无法自动验证远端缓存数据是否已具备就绪消费条件，亦未包含多并发请求的统计分布采样。因此，当前原型代码默认产出 `DEMO / W0` 阶段的工作流验证证据，尚不能单独作为关闭 E0 阶段真实通信协议加速比或业务收益结论的最终依据。

> **术语速查**：
> - **KVCache**：大模型注意力键值缓存（大模型自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免后续 Token 生成时重复计算注意力）；
> - **Saved-Prefill**：首字生成预计算节省（利用已缓存的 KVCache 避免重复计算 Prompt 前缀，从而显著降低首 Token 延迟 TTFT）；
> - **TTFT**：Time To First Token（首字生成延迟 / 首 Token 响应时间）；
> - **MHA**：Multi-Head Attention（多头注意力机制，通常产生较大的逐 Token KV 数据）；
> - **MLA**：Multi-head Latent Attention（多头潜在注意力机制，通过潜在向量压缩 KV 状态）；
> - **URMA**：通用远程直接内存访问（用户态的高性能 RDMA 驱动接口与通信协议）；
> - **UBMEM**：统一总线内存直通共享协议（支持跨节点与异构设备间直接共享内存地址空间的底层通信协议）。

> **验证 ID**：PVT-00
> **验证名称**：业务流量 Saved-Prefill 收益上限与通信协议加速评估
> **验证优先级**：**🟡 P1 级（底座支撑项）**
> **对应验证阶段**：**E0（业务收益前提确认）**
> **证伪标记**：否（业务收益前提确认）
> **主关联 IR**：`IR-02-11`, `IR-02-12`
> **核心 SRS / SR23 锚点**：
> - SRS：`L3-OB-PerPathTelemetry-047`, `L1-OB-SemanticMetrics-016`, `SE-MONITOR-001`, `SE-PERF-001`
> - SR23：`SR23-02-11-01`, `SR23-02-11-02`, `SR23-02-12-01`, `SR23-02-12-05`
> **配套源码**：[`./原型验证代码/PVT-00/`](./原型验证代码/PVT-00/)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`；vLLM `842dd8fd96650063e1ad32e6075742d457d39773`；vLLM-Ascend `424e27e1fd2b1c6e0d7fe659b489b87c1223a33c`。正式结果以 `package_id`、`baseline_commit` 和配置哈希重新核对为准。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统软件视角：缓存只有在拉取成本低于重算成本时才有价值

在经典数据库、RPC 网关与分布式存储系统中，缓存能否带来收益并不单看“命中率”，而是取决于全链路的综合成本。这包括：元数据查询、正文数据拉取、反序列化、合法性校验、显存挂接以及多卡同步等各环节耗时。如果搬运和处理缓存的总开销超过了本地重新计算的耗时，引入缓存反而会拖慢系统。

我们可以把单次请求拆解为两条可对比的物理执行路径：

```text
本地重算路径：    请求进入 → 全量 Prefill 计算 → 首字生成
复用 KVCache 路径：请求进入 → 查询目录 → 加载 KV 数据 → 语义校验/挂接/同步 → 未命中部分 Prefill → 首字生成
```

本方案不预设“缓存必然加速”的前提，而是将“本地直接重算”与“KVCache 复用加载”两条路径置于同场次、同负载及相同硬件环境下开展严格实测对比。只有当复用路径的端到端 TTFT 明显低于本地重算，且净收益足以覆盖元数据查询、数据传输与显存挂接的总开销时，前缀复用才具备实际工程价值，并可作为后续调度决策系统设计的依据。

### 0.2 大模型推理中的对应物理问题

在大模型自回归推理中：
- **Prefill 阶段**（首字预计算）：对输入的 Prompt 进行上下文理解并生成首个输出 Token，属于计算密集型负载；
- **Decode 阶段**（逐字生成）：基于历史上下文持续自回归输出后续 Token，需要反复读取历史 KVCache。

当后续请求复用了前一个请求的 Prompt 前缀时，系统有两种处理策略：
1. **本地重算前缀**：重新执行该前缀的 Prefill 计算，消耗算力与时间；
2. **复用外接缓存**：从远端 KV 存储池拉取已保存的 KVCache，跳过前缀的重复计算，但需要承担目录查询、网络传输、语义校验、显存挂接与多卡同步等额外开销。

以主流模型为例：
- Qwen 采用的多头注意力机制（MHA，Multi-Head Attention）单 Token 的 KV 数据量较大（约 `320KB/tok`），传输更容易受网络带宽制约；
- DeepSeek 采用的多头潜在注意力机制（MLA，Multi-head Latent Attention）通过潜在向量对 KV 进行了压缩（约 `35KB/tok`），传输数据量较小，但元数据查询、协议固定时延和显存挂接耗时在总时间中的占比更为明显。

上述数值为基准测试负载构造的初始参考，正式评估以运行时的模型物理布局清单（`model_layout_manifest`）实测值为准。

### 0.3 当前配套工程的验证边界

根据统一测试规范，实验证据划分为 DEMO（流程演示）、LAB（局部实测）与 MEASURED（生产级实测）三档，执行环境区分为 W0（单机模拟）、W1（局部硬件）与 W2（真实集群拓扑）。本验证项严格区分流程跑通与硬件实测，分别归档。

| 子实验 | 当前源码能够完成的动作 | 当前源码不能直接证明的内容 | 当前默认证据状态 |
|---|---|---|---|
| 协议微基准 | 对本地 `malloc` 缓冲区做并发 `memcpy`，输出带宽、平均时延、P50、P99 和 CSV | URMA/UBMEM 物理设备带宽、DMA 完成时延、远端内存访问、协议栈性能差异、网卡/总线拥塞表现 | `DEMO / W0` |
| 工作负载生成 | 生成 R1 前缀预热请求与 R2 前缀复用请求，写出 `pvt00.workload.v1` JSON | 真实 Tokenizer 词表分布、真实模型内存布局、真实 Prefill 算子开销与线上实际请求分布 | `DEMO`；提供运行时布局清单后具备 LAB 输入条件 |
| 在线端点闭环 | 调用 `/v1/completions`，测量 R2 首个非空流式响应的 TTFT，输出一行 CSV | 目录命中率、底层 KV 实际加载路径、逐请求传输耗时分段、完整分位数分布及真实协议归因 | 由运行参数决定；无物理硬件路径时为 `DEMO` |
| W0 集群脚本 | 在 localhost 上启动 Mooncake Master、Prefill、Decode 和代理的流程示范 | 双节点真实部署、物理 RDMA/URMA/UBMEM 传输、真实 NPU 模型执行及跨节点净加速收益 | `DEMO / W0` |
| 通用 benchmark 解析 | 解析 `benchmark_serving` 输出 JSON 中的吞吐、TTFT、TPOT 等字段 | 不能替代 PVT-00 的 R1/R2 同场次重算配对，亦无法证明通信协议已在底层完成切换 | `DEMO` 或绑定物理路径后的 `LAB` |

当前 `proto_bench` 的输出仅代表“本地内存复制基准流程”；`traffic_generator.py` 的输出必须包含 `mode`、`actual_path`、代码包版本、配置哈希、就绪事件及同场次重算基线。若缺少上述核心凭证，测试结论标记为 `NOT-SUPPORTED`（功能未支持）或 `INVALID-EVIDENCE`（证据无效），严禁将模拟数据包装为协议性能实测结论。

---

## 1. 验证目标与交付结论定义

### 1.1 核心验证命题

1. **命题一：Saved-Prefill 是否存在确定的净收益**。针对 Qwen MHA 与 DeepSeek MLA 架构，在 30%、50%、70%、90%、98% 五档前缀复用率下，对比同场次本地重算与 KV 复用路径的端到端 TTFT，明确产生净收益的上下文长度与复用率边界；
2. **命题二：传输协议是否存在可量化的物理加速差异**。在相同数据 Payload、并发度、传输方向、硬件设备及网络拓扑下，对比 URMA 与 UBMEM 在元数据查询与数据传输中的时延、带宽及 P99 稳定性；当前本地 `proto_bench.cc` 为测试桩，需接入真实驱动 SDK 后开展实测，否则该命题保持为 `NOT-SUPPORTED`；
3. **命题三：上层端到端收益能否由底层物理路径开销分解闭环解释**。通过采集请求到达、目录查询、数据拉取、语义校验/挂接、未命中 Token Prefill 及首字生成等全流程时间戳，解析端到端加速收益的具体构成。若仅有整体 TTFT 而缺少底层分段事件，仅记录为业务现象，不做底层技术归因。

### 1.2 交付物与结论边界

每个正式 `run_id` 至少交付：
1. 《URMA vs UBMEM 协议传输性能基准表》：覆盖冻结的 payload、并发、方向和重复轮次，保留原始样本、失败样本和设备计数器；
2. 《本地重算、原生 Mooncake、单项增强和完整增强四模式对账表》：每条 R2 记录均能回指同场次 R1、就绪事件、实际路径和代码包；
3. 《MLA vs MHA 五档复用率 TTFT 收益交叉对账表》：记录运行时 KV 字节数、前缀 Token 数、未命中 Token 数、TTFT 分位数和净收益；
4. `manifest.json`、`environment.json`、原始事件、原始 stdout、汇总 CSV、汇总 JSON 和日志；
5. 按协议微基准和业务复用两条命题分别输出 `GO`、`CONDITIONAL`、`NO-GO`、`NOT-SUPPORTED` 或 `INVALID-EVIDENCE`，不把不同证据等级合并成一个总成绩。

---

## 2. 实验方案与测试矩阵设计

### 2.1 四种被测模式与公平 A/B

| 模式 | 目标被测对象 | 运行要求 | 当前脚本状态 |
|---|---|---|---|
| `recompute` | 禁用远端 KV 复用的基线代码包或配置 | R2 在同一端点、同一模型和同一工作负载下执行本地全量重算 | `traffic_generator.py` 支持；实际物理端点需现场部署提供 |
| `mooncake_native` | 固定 Git Commit 的原生 Mooncake 代码包或配置 | 切换至真实的开源原生端点，严禁仅修改客户端 `--mode` 字符串 | CLI 已支持模式标签；当前代码仓需配合外部部署环境 |
| `unified_single` | 仅开启单个特定增强特性的代码包或配置 | 明确声明启用的单项技术特性、代码包版本及配置哈希 | CLI 已支持模式标签；目标服务需现场部署提供 |
| `unified_full` | 开启全量待验收增强特性的代码包或配置 | 完整记录所有功能开关与实测物理路径，严禁将规划路径视同实测路径 | CLI 已支持模式标签；目标服务需现场部署提供 |

在开展 A/B 对照测试时，必须严格保持模型权重版本、张量并行度 (TP)、Tokenizer 词表、输入测试负载、请求发送序列、并发度、请求到达率、物理硬件、网络拓扑、资源配额、编译参数、预热轮次、测量采样数及统计口径的一致。测试中仅允许变更待验证的代码包版本或系统配置项；若实际执行路径未发生改变，或 `planned_path`（规划路径）与 `actual_path`（实测路径）无法形成证据闭环，测试结果均视为无效。

### 2.2 协议微基准参数矩阵

| 维度 | 正式计划取值 | 当前源码支持情况 | 备注 |
|---|---|---|---|
| payload | 4KB、64KB、256KB、1MB、4MB、16MB、64MB；必要时扩展 128MB | 支持通过 `--payload-bytes` 指定单值运行；默认基准数组覆盖至 64MB | 每个 payload 数据块尺寸独立生成一组测试数据，严禁将不同运行批次混淆归档 |
| 并发 | 1、4、16、32、64 | 支持通过 `--concurrency` 指定单值运行；默认覆盖五档并发 | 当前测试桩的并发计量单位为本地 C++ 线程，非底层 RDMA QP 队列或物理请求流 |
| 方向 | Write、Read、双向混流 | 当前代码固定执行本地 `memcpy` 并默认标记为 `write` | 真实传输方向需由底层硬件驱动的完成事件进行确认 |
| 协议 | URMA、UBMEM | 当前 `--protocol` 仅触发代码内部的分支选择与忙等循环 | 严禁据此测试桩行为直接推导两种协议的物理性能差异 |
| 预热与测量 | 运行前统一固化；建议至少 1 轮预热、3 次独立重复 | 当前 `proto_bench` 仅按 `--duration-sec` 或 `--iters` 进行循环 | 正式测试脚本需额外保存完整的重复轮次数据与物理环境快照 |

### 2.3 业务流量与模型参数矩阵

| 维度 | 正式计划取值 | 当前生成器支持情况 | 证据要求 |
|---|---|---|---|
| 模型架构 | Qwen MHA、DeepSeek MLA | 支持 `--model-type mha` / `mla` / `gqa`；当前仅生成元数据与 Token ID | 正式实测需对接真实模型并输出 `model_layout_manifest` |
| 前缀复用率 | 30%、50%、70%、90%、98% | 通过 `prefix_tokens` 与 `unique_tokens` 计算生成 | 以输出 JSON 中的 `reuse_ratio` 字段为准，严禁仅凭文件名推断 |
| 上下文总长 | 8K、32K、64K、128K、256K 或现场可用等价档位 | 通过 Token 数量参数化构造；当前默认示例基于 100K Token | 每个上下文长度档位均需记录显存占用、内存布局及实际可消费状态 |
| 请求并发 | 1、4、16、32、64 | `traffic_generator.py` 当前按序发送单对 R1 与 R2 请求 | 多请求分布测试需采用专业压测工具或扩展脚本，严禁将单次采样等同于 P99 表现 |
| 代码模式 | recompute、mooncake_native、unified_single、unified_full | CLI 支持上述四种模式参数 | 每个模式必须对应唯一的独立 `package_id` 或配置哈希 |

五档复用率的标准化构造示例（以总长为 100K Token 的 R2 Prompt 为例）：

| 复用率 | `prefix_tokens` | `unique_tokens` | 实际计算公式 |
|---:|---:|---:|---:|
| 30% | 30,000 | 70,000 | `30,000 / 100,000` |
| 50% | 50,000 | 50,000 | `50,000 / 100,000` |
| 70% | 70,000 | 30,000 | `70,000 / 100,000` |
| 90% | 90,000 | 10,000 | `90,000 / 100,000` |
| 98% | 98,000 | 2,000 | `98,000 / 100,000` |

### 2.4 环境与证据矩阵

| 环境 | 验证目的 | 最低前置条件 | 允许产出的证据结论 |
|---|---|---|---|
| W0 单机/localhost/Mock | 验证参数传递、CLI 命令、输出字段、R1/R2 时序及结果解析流程 | Python 环境、C++ 编译器、localhost 本地端点或 Mock 桩 | 仅可产出 `DEMO` 级别的工作流有效性结论 |
| W1 局部设备实测 | 观测并采集特定硬件设备上的协议传输或服务调用行为 | 具备可用 NPU、高性能网卡、NVMe SSD、驱动 SDK 及真实模型布局凭证 | 可产出绑定特定软件版本与物理拓扑的 `LAB` 局部结论 |
| W2 跨节点物理集群实测 | 正式关闭 E0 阶段业务净收益与通信协议加速对比的验证命题 | 跨节点物理数据链路、受控代码包、硬件完成事件、同场次重算基线与多轮重复实测 | 满足全量证据闭环后，可正式产出 `MEASURED` 生产级结论 |

---

## 3. 实验动力学模型与统计口径

### 3.1 R1/R2 工作负载与复用率模型

`make_workload.py` 的负载生成模型为：R1 请求仅包含前缀部分 Prompt，R2 请求则由该段完全相同的前缀与一段新增的独占 Token 拼接而成。

```text
R1 请求: [---------------- Prefix A ----------------] → Prefill 预计算并保存 KV
R2 请求: [---------------- Prefix A ----------------][---------------- New B ----------------]
                     ^------ 可复用前缀 ------^          ^------ 必须重新计算 ------^
```

前缀复用率计算公式为：

$$
reuse\_ratio=\frac{prefix\_tokens}{prefix\_tokens+unique\_tokens}
$$

在数据对账中需严格遵循以下准则：
- R1/R2 的 Token ID 序列具备确定性可复现特征，但其统计规律并不完全等同于真实业务 Prompt 经过 Tokenizer 分词后的自然语言分布；
- 缺失 `kv_bytes_per_token` 参数时仅允许运行 DEMO 演示模式；`traffic_generator.py` 在进入 LAB/MEASURED 实测时主动拦截并拒绝该输入；
- 文中提及的 `320KB/tok`、`35KB/tok` 以及 TP=8 下单卡显存分摊值供初始测试规模估算，正式评测以运行时实际读取的物理布局清单为准；
- 仅当远端推理端点产生明确的 `ready_url` 或 `ready_file` 等就绪事件凭证时，R2 请求方可作为满足消费一致性条件的有效复用样本纳入对账。

### 3.2 协议微基准的物理口径

在物理通信系统中，单次协议传输的耗时由提交开销、队列排队、硬件 DMA 搬运、远端处理完成及同步开销共同构成：

$$
T_{protocol}=T_{submit}+T_{queue}+T_{DMA}+T_{remote\_complete}+T_{fence}
$$

而在当前受控的原型代码 `proto_bench.cc` 中，实际执行逻辑为：

```text
本地 malloc 分配内存 → memset 初始化 → memcpy(src_buf, dst_buf) 内存拷贝 → 本地高精度时钟计时 → 释放内存
```

在当前测试桩实现中，`--protocol ubmem` 与 `--protocol urma` 均统一调用本地 `memcpy` 路径，其中 URMA 分支仅通过额外执行一段忙等循环来模拟协议时延。这一差异仅属于测试桩的代码逻辑，并非真实协议栈的物理实测。因此，当前输出 CSV 中的 `bandwidth_gbps`、`latency_*_us` 仅反映本地内存复制的基准流程表现，不可直接作为评估两种协议物理加速比的依据。

### 3.3 Saved-Prefill 端到端成本分解

针对同一条 R2 请求，复用已缓存 KV 的执行路径端到端 TTFT 可按物理事件拆解为：

$$
TTFT_{reuse}=T_{dir\_query}+T_{data\_load}+T_{validate\_attach\_sync}+T_{uncached\_prefill}+T_{service}
$$

而本地直接执行完整重算的路径端到端 TTFT 为：

$$
TTFT_{recompute}=T_{full\_prefill}+T_{service}
$$

由此推导出 Saved-Prefill 的净加速收益计算公式：

$$
T_{net\_saved}=TTFT_{recompute}-TTFT_{reuse}
$$

只有当 $T_{net\_saved} > 0$ 时，表明该场景未发生负收益现象。在工程实践中，为了确保引入外接缓存具备明确收益，系统设定了更为严格的候选准入门槛：

$$
T_{net\_saved}\ge 2.0\times(T_{dir\_query}+T_{data\_load}+T_{validate\_attach\_sync})
$$

该候选门限必须在测试启动前固化至实验配置清单中，严禁事后倒推拟合；若底层分段事件未完整采集，严禁使用端到端 TTFT 粗粒度差值直接指代底层细粒度分项开销。

### 3.4 分位数统计与重复实验要求

单次执行 `traffic_generator.py` 仅输出单条 R2 请求的采样记录，无法直接推导 P50/P95/P99 等统计分位数。正式的业务评估需在每个运行模式、复用率、模型架构及并发到达率条件下，完整留存逐请求的原始 TTFT 采样集，并基于足量样本计算分位数：
- 每个测试用例默认至少执行 1 轮预热及 3 轮独立的重复测量；
- 失败及超时的异常请求必须完整记录并计入统计；
- P99/P99.9 等高分位数指标基于足量样本集统计；若样本量不足，对应字段统一填写 `null` 并注明 `invalid_reason`；
- 作为对比基准的同场次本地重算，必须与待测 R2 请求采用完全相同的工作负载、模型实例、请求序列、硬件设备、资源配额及统计口径。

---

## 4. 测试工具、当前实现边界与正式扩展要求

### 4.1 当前受控工程目录与运行方式

```text
原型验证代码/PVT-00/
├── Makefile
├── proto_bench.cc          # 当前执行本地 memcpy 的协议流程基准测试桩
├── make_workload.py        # 生成符合 pvt00.workload.v1 规范的合成 Token 负载
└── traffic_generator.py    # 触发 HTTP 推理端点并采集 R1/R2 单次 TTFT 的测试工具

原型验证代码/deploy_and_bench_e2e/
├── deploy_cluster.sh       # 当前仅支持 W0 localhost 环境的集群部署脚本
├── run_online_benchmark.sh  # 自动化调度 benchmark_serving 开展预热、压测与解析的入口
└── parse_benchmark_metrics.py
```

当前验证可用的 PVT-00 协议微基准执行命令示例：

```bash
cd ./原型验证代码/PVT-00
make
./proto_bench --protocol urma --payload-bytes 1048576 --concurrency 4 --iters 1000 --out proto_urma_demo.csv
./proto_bench --protocol ubmem --payload-bytes 1048576 --concurrency 4 --iters 1000 --out proto_ubmem_demo.csv
```

当前测试桩 CLI 支持的参数清单：

```text
--protocol       测试代码分支标签，当前支持 urma 或 ubmem
--payload-bytes  单次传输的数据块 Payload 字节数
--concurrency    本地并发 C++ 工作线程数
--duration-sec   未指定 iterations 时的持续运行时间（秒）
--iters          每个工作线程的循环迭代次数；指定后优先级高于 duration
--out            测试结果 CSV 文件的输出路径
```

当前测试桩 CLI 尚未集成 `--direction`、`--device`、`--qp-count`、`--remote` 等底层驱动参数。若需针对真实 URMA/UBMEM 协议开展物理实测，应先按照第 4.3 节要求完成工程扩展并重新锁定代码包版本。

### 4.2 源码实际行为审计

| 代码文件与函数 | 源码实际执行行为 | 对实测证据等级的影响分析 |
|---|---|---|
| `proto_bench.cc::worker_transfer` | 在本地堆上分配两块缓冲区，执行 `memset` 初始化后调用 `memcpy` 进行数据搬运；代码未包含真实的 URMA/UBMEM SDK 头文件与驱动接口调用，URMA 分支仅通过一段空循环模拟开销 | 输出数据仅反映本地内存拷贝表现，无法证明 URMA/UBMEM 的物理带宽、DMA 直达、远端完成事件及协议差异；默认定级为 `DEMO / W0` |
| `proto_bench.cc::run_benchmark_case` | 汇总各本地线程搬运的总字节数与耗时，计算平均值、P50 及 P99 指标，并输出带有 `DEMO,LOCAL_MEMCPY_ONLY` 标记的 CSV | `bandwidth_gbps` 仅代表本地内存复制吞吐量；若无有效样本输出 0 仅为统计占位，不可视为 0 时延的实测证据 |
| `Makefile` | 编译选项仅链接了 `-pthread` 线程库，未链接 `liburma.so` 或 `libubmem.so` 驱动库 | 当前工程可在无专用硬件驱动的环境下顺利编译，但这不代表系统已支持真实的物理协议栈 |
| `make_workload.py` | 采用伪随机算法生成 Prefix A 与 New B 的 Token ID 序列，导出结构化负载文件、布局字节数及复用率 | 生成的内容为可复现的合成基准输入，并非真实大模型分词后的生产业务流量 |
| `traffic_generator.py::send_prompt` | 向 `<endpoint>/v1/completions` 发送 JSON 请求，以捕获的首个非空流式响应数据块时间戳作为 TTFT 采样值 | 客户端未读取推理引擎内部的目录命中标记、DMA 完成事件或 NPU 执行时戳；单次运行仅产生单条 R2 样本 |
| `traffic_generator.py::wait_until_ready` | 在 LAB/MEASURED 实测模式下必须提供 `--ready-url` 或 `--ready-file` 作为就绪凭证；DEMO 模式下无就绪事件时仅执行固定时间的 sleep | 固定 sleep 仅可用于演示流程；在缺少真实就绪事件的前提下，严禁声称 KV 数据已具备安全消费条件 |
| `traffic_generator.py::main` | 在非 recompute 模式下先发送 R1 请求，等待就绪事件触发后再发送 R2 请求；若未指定 `--recompute-baseline-json`，则 `net_saved_ms` 字段置为 `null` | 完整演示了 R1 预热与 R2 复用的时序关系；但在缺失同场次重算基线时，无法关闭净加速收益的判定 |
| `deploy_cluster.sh` | 当环境变量 `EVIDENCE_ENVIRONMENT` 非 W0 时直接终止退出；W0 模式下在 localhost 上拉起 Master、Prefill、Decode 及代理服务 | 该脚本仅用于单机环境下的流程联调，无法据此得出跨节点分布式集群或物理 RDMA/UBMEM 的性能结论 |
| `parse_benchmark_metrics.py` | 解析 `benchmark_serving` 导出的 JSON 结果，提取 QPS、TTFT、TPOT 等核心指标；若关键字段缺失则返回 `INVALID_EVIDENCE` 状态 | 属于通用的 Benchmark 性能汇总解析器，未内置 PVT-00 专属的 R1/R2 同场次配对对账逻辑；正式报告需统一状态枚举映射 |

### 4.3 面向 LAB/MEASURED 的最小工程扩展

在正式进入真实协议加速比或业务净收益评估前，需补齐以下工程支撑能力：
1. **底层真实驱动接入**：对接原厂 URMA/UBMEM SDK，实现内存注册、描述符提交、跨节点远端内存访问及物理完成事件监听；支持指定传输方向、队列对 (QP)/并发通道及完整错误码处理，替换本地 `memcpy` 桩代码；
2. **全链路物理路径凭证**：为逐条推理请求记录 `planned_path`（规划路径）、`actual_path`（实测物理路径）、代码包版本、配置哈希及硬件设备序列标识；
3. **运行时布局与多维语义一致性校验**：记录 `model_layout_manifest`、Tokenizer 词表哈希、模型架构、TP 张量并行切分维度、内存对齐及实测 `kv_bytes_per_token`；
4. **生产级就绪事件机制**：提供 `ready_event`、`visibility_epoch` 或等效的分布式缓存就绪凭证，替换 LAB/MEASURED 实测流程中的固定 sleep 延时；
5. **逐请求细粒度事件时间戳**：记录请求到达、目录查询、数据搬运、语义校验与显存挂接、多卡同步、Prefill 计算启动及首字生成等全流程事件戳；
6. **同场次重算严格配对**：本地重算基准与待测 R2 请求使用完全相同的输入负载、请求发送序列及唯一配对键；
7. **多请求统计分布采样**：支持请求到达率、并发压力、预热轮次及失败重试归档，基于全量原始事件样本计算 P50/P95/P99 等分位数；
8. **协议与硬件性能计数器采集**：采集真实物理带宽、DMA 完成中断、传输重试、链路错误、硬件队列深度及设备利用率；未能采集到的硬件字段统一显式置为 `null` 并详细填写 `invalid_reason`；
9. **规范化状态枚举输出**：将内部脚本返回状态映射为公共契约规定的 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE` 标准状态。

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结实验身份、证据等级和公平 A/B 条件

- **操作意图**：明确本轮评测的执行级别（W0/DEMO、W1/LAB 或 W2/MEASURED），杜绝将单机内存拷贝或单次 HTTP 调用的流程验证误判为物理协议的实测结论。
- **执行动作**：在配置清单中填报 `run_id`、`workload_schema_version`、`workload_id`、`package_id`、`baseline_commit`、`config_hash`、`model_layout_manifest`、`tokenizer_hash`、`hardware_profile`、`topology_profile`、`evidence_level`、运行模式、复用率档位、预热轮次、采样轮数及准入门槛。
- **应观察现象**：配置清晰列出本轮实测物理路径、未覆盖功能点及输出归档目录；若现场缺乏真实驱动、物理模型布局或就绪事件支持，提前登记为 `NOT-SUPPORTED`。

### 步骤 1：审计并运行协议微基准 W0 基线

- **操作意图**：验证测试工程编译、参数解析、CSV 格式导出及本地多线程并发统计的完整闭环，建立基准流程基线；本步骤不用于评估 URMA/UBMEM 的物理传输性能。
- **执行命令**：

```bash
cd ./原型验证代码/PVT-00
make
./proto_bench --protocol urma --payload-bytes 1048576 --concurrency 4 --iters 1000 --out proto_urma_demo.csv > proto_urma_stdout.txt 2>&1
./proto_bench --protocol ubmem --payload-bytes 1048576 --concurrency 4 --iters 1000 --out proto_ubmem_demo.csv > proto_ubmem_stdout.txt 2>&1
```

- **应观察现象**：终端正确打印 Payload 字节数、工作线程数、本地内存拷贝带宽及 P99 时延；生成的 CSV 文件中 `evidence_level` 明确标记为 `DEMO`，状态字段包含 `LOCAL_MEMCPY_ONLY`。
- **判定边界**：本步骤仅证实本地测试桩脚手架能够正常运行，不能将两个 CSV 的吞吐比值包装为协议物理加速比。

### 步骤 2：生成运行时布局绑定的五档复用率负载

- **操作意图**：将前缀复用率转化为具备严格 Token ID 对应关系的结构化测试负载与元数据清单，消除仅凭文件名或纸面百分比描述测试输入的模糊性。
- **执行动作**：准备包含实测 `kv_bytes_per_token` 的运行时模型物理布局清单，依据规范分别生成五档复用率测试负载（以下以 50% 复用率为例）：

```bash
python3 ./make_workload.py \
  --model-type mha \
  --model-id Qwen2.5-72B \
  --layout-manifest runtime_layout_qwen.json \
  --prefix-tokens 50000 \
  --unique-tokens 50000 \
  --workload-id pvt00_qwen_mha_50pct \
  --out workload_qwen_mha_50pct.json

python3 ./make_workload.py \
  --model-type mla \
  --model-id DeepSeek-V3 \
  --layout-manifest runtime_layout_deepseek.json \
  --prefix-tokens 50000 \
  --unique-tokens 50000 \
  --workload-id pvt00_deepseek_mla_50pct \
  --out workload_deepseek_mla_50pct.json
```

- **应观察现象**：导出的 JSON 文件中 `schema_version` 规范标注为 `pvt00.workload.v1`，R1 的 Token 序列与 R2 的前缀部分严格一致，计算得出的 `reuse_ratio` 与预期参数吻合，`kv_bytes_per_token` 正确继承自物理布局清单。
- **证据边界**：缺失真实模型物理布局清单时仅允许生成 DEMO 演示负载；开展 LAB/MEASURED 实测时若缺失布局清单应中止评测。

### 步骤 3：先执行同场次本地重算并固化基线

- **操作意图**：为每个前缀复用场景建立严格对应的 R2 请求本地全量重算性能基线，避免使用跨批次的平均指标掩盖底层硬件、负载或服务波动。
- **执行命令**：

```bash
python3 ./traffic_generator.py \
  --endpoint http://recompute:8000 \
  --workload workload_qwen_mha_50pct.json \
  --mode recompute \
  --protocol none \
  --actual-path recompute \
  --package-id <recompute_package_id> \
  --config-hash <recompute_config_hash> \
  --hardware-profile <hardware_profile> \
  --evidence-level LAB \
  --run-id pvt00_qwen_50pct_recompute \
  --out-csv results/pvt00_qwen_50pct_recompute.csv \
  --out-json results/pvt00_qwen_50pct_recompute.json
```

- **应观察现象**：运行模式标记为 `recompute`，输出结果包含基线 `ttft_ms`，生成的 JSON 文件可供后续复用模式通过 `--recompute-baseline-json` 进行精确引用与对账。
- **判定边界**：本地重算端点、模型实例、测试负载及硬件拓扑必须与后续待测模式严格保持一致。

### 步骤 4：运行原生 Mooncake 复用路径

- **操作意图**：在固定的开源原生 Mooncake 代码包及真实服务实例上，测量 R1 数据写入就绪后 R2 复用链路的端到端 TTFT，并与步骤 3 的同场次重算基线开展严格对账。
- **执行命令模板**：

```bash
python3 ./traffic_generator.py \
  --endpoint <native_endpoint> \
  --workload workload_qwen_mha_50pct.json \
  --mode mooncake_native \
  --protocol urma \
  --actual-path <actual_native_path> \
  --package-id <mooncake_native_package_id> \
  --config-hash <native_config_hash> \
  --hardware-profile <hardware_profile> \
  --evidence-level LAB \
  --run-id pvt00_qwen_50pct_native \
  --ready-url <ready_url> \
  --recompute-baseline-json results/pvt00_qwen_50pct_recompute.json \
  --out-csv results/pvt00_qwen_50pct_native.csv \
  --out-json results/pvt00_qwen_50pct_native.json
```

- **应观察现象**：R1 请求处理完毕后捕获到明确的远端就绪事件，R2 请求触发并返回首字生成事件；结果数据完整包含 `actual_path`、`ttft_ms`、`recompute_ttft_ms` 及 `net_saved_ms` 字段。
- **证据边界**：CLI 传入的 `--protocol urma` 仅为客户端参数标识，必须通过服务端部署代码包与底层硬件完成事件共同证实物理执行路径后，方可形成 URMA 协议的实测结论。

### 步骤 5：在相同条件下切换单项增强与完整增强

- **操作意图**：在保持模型、输入负载、请求序列及物理硬件拓扑完全一致的前提下，系统对比开源原生 Mooncake、单项增强及完整增强方案，准确剥离并量化 UBMEM 等关键技术的净加速贡献。
- **执行动作**：分别以 `unified_single` 与 `unified_full` 模式运行步骤 4 提供的命令模板，替换为对应的真实服务端点、`package_id`、`config_hash`、`actual_path` 及结果归档路径；严禁仅修改客户端 `--mode` 字符串而未真实切换服务端。
- **应观察现象**：四种模式均能独立追溯至明确的代码包版本与配置哈希；R1/R2 的 `workload_id`、模型物理布局、就绪事件与 `run_id` 形成完整的证据链。
- **停止条件**：若目标服务实例、真实 UBMEM 驱动或实际物理传输路径缺失，将该模式登记为 `NOT-SUPPORTED`。

### 步骤 6：可选运行 W0 端到端部署和通用压测解析

- **操作意图**：验证现有 W0 自动化集群部署脚本、标准 benchmark_serving 工具及结果解析器的执行闭环，并确保其产物与 PVT-00 专属的同场次 R1/R2 配对证据隔离归档。
- **执行命令**：

```bash
cd ./原型验证代码/deploy_and_bench_e2e
EVIDENCE_ENVIRONMENT=W0 bash ./deploy_cluster.sh

MODE=mooncake_native \
RUN_ID=pvt00_w0_demo \
PACKAGE_ID=<package_id> \
CONFIG_HASH=<config_hash> \
HARDWARE_PROFILE=<hardware_profile> \
TOPOLOGY_PROFILE=<topology_profile> \
WORKLOAD_ID=<workload_id> \
EVIDENCE_LEVEL=DEMO \
bash ./run_online_benchmark.sh
```

- **应观察现象**：部署脚本在检测到 W0 环境后在 localhost 上启动各组件，输出目录下生成预热日志、实测 JSON 及 `summary.csv`；若关键字段缺失，解析器返回无效证据状态。
- **判定边界**：`run_online_benchmark.sh` 采用通用的 ShareGPT 混合输入，不能替代 PVT-00 要求的 R1/R2 同场次精确重算配对；W0 环境下的产出限定级为 `DEMO`。

### 步骤 7：保存原始数据、重复实验和证据包

- **操作意图**：将控制台输出固化为可复核的结构化证据包，完整留存原始采样、异常请求及未支持字段，确保测试全过程透明可追溯。
- **执行动作**：在 `results/PVT-00/<mode>/<run_id>/` 目录下归档 `manifest.json`、`environment.json`、原始 stdout 控制台输出、逐请求结构化事件、汇总 CSV/JSON 以及系统日志；每个测试条件确保至少完成 3 轮独立重复采样。
- **应观察现象**：汇总报告中的每个 TTFT 指标、净收益数值及协议吞吐均能精准索引至底层原始采样点；`planned_path`、`actual_path`、`evidence_level`、`status` 及 `invalid_reason` 字段完整齐备。
- **字段规则**：对于未能采集到的物理字段必须显式填写 `null` 并注明原因；严禁使用 `0` 数值代指未丢包、零开销或零时延。

---

## 6. 数据采集清单与记录格式

### 6.1 PVT-00 原始事件字段

在统一公共事件字段的基础上，本验证项至少采集并持久化以下字段：

```text
run_id, validation_id, trace_id, request_id, event_name,
workload_schema_version, workload_id, model_id, model_type,
model_layout_manifest, tokenizer_hash, prefix_tokens, unique_tokens,
total_r2_tokens, reuse_ratio, kv_bytes_per_token,
mode, protocol, planned_path, actual_path,
package_id, baseline_commit, config_hash,
request_start_ns, ready_event_ns, lookup_start_ns, lookup_end_ns,
xfer_start_ns, xfer_end_ns, attach_start_ns, attach_end_ns,
prefill_start_ns, first_token_ns, total_end_ns,
payload_bytes, ttft_ns, status_code, error_code,
hardware_profile, topology_profile, evidence_environment, evidence_level,
status, invalid_reason
```

字段填写约束：
- `ready_event_ns` 仅在捕获到真实就绪事件或同等消费凭证时填写；DEMO 流程中的固定 sleep 严禁伪装为真实事件；
- `actual_path` 必须基于服务端实例、底层硬件完成中断或可审计日志进行确认；
- `T_net_saved` 仅在同场次本地重算基线与待测复用路径的 TTFT 均合法有效时计算填报；
- 对于驱动协议、DMA 中断、目录查询及显存挂接等未采集到的事件字段，统一填写 `null` 并注明 `invalid_reason`；
- `status` 严格限定为 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE`；脚本进程正常退出的 `OK` 仅代表命令执行完毕，不代表验证通过。

### 6.2 协议微基准汇总 CSV 模板

```csv
validation_id,run_id,protocol,payload_bytes,concurrency,direction,sample_count,bandwidth_gbps,latency_avg_us,latency_p50_us,latency_p99_us,actual_path,package_id,config_hash,hardware_profile,topology_profile,evidence_environment,evidence_level,status,invalid_reason
<PVT-00>,<run_id>,<urma_or_ubmem>,<bytes>,<threads>,<write_or_read>,<count>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<actual_path_or_null>,<package_id>,<config_hash>,<hardware_profile>,<topology_profile>,<W0_OR_W1_OR_W2>,<DEMO_OR_LAB_OR_MEASURED>,<status>,<null_or_reason>
```

当前 `proto_bench.cc` 产出的测试数据必须明确标注 `status=NOT-SUPPORTED` 或 `CONDITIONAL` 状态说明，严禁仅因 CSV 包含数值即标报为 URMA/UBMEM 的 `MEASURED` 生产级结论。

### 6.3 端到端对账 CSV 模板

```csv
validation_id,run_id,workload_id,model_id,model_type,kv_bytes_per_token,prefix_tokens,unique_tokens,total_r2_tokens,reuse_ratio,mode,protocol,planned_path,actual_path,package_id,baseline_commit,config_hash,hardware_profile,topology_profile,evidence_environment,evidence_level,sample_count,ttft_p50_ms,ttft_p95_ms,ttft_p99_ms,recompute_ttft_p50_ms,recompute_ttft_p95_ms,recompute_ttft_p99_ms,dir_query_p99_ms,data_load_p99_ms,attach_sync_p99_ms,net_saved_p50_ms,net_saved_p99_ms,ubmem_speedup,status,invalid_reason
<PVT-00>,<run_id>,<workload_id>,<model_id>,<mha_or_mla>,<runtime_value>,<prefix>,<unique>,<total>,<ratio>,<recompute_or_native_or_unified_single_or_unified_full>,<protocol>,<planned_path>,<actual_path>,<package_id>,<baseline_commit>,<config_hash>,<hardware_profile>,<topology_profile>,<W0_OR_W1_OR_W2>,<DEMO_OR_LAB_OR_MEASURED>,<count>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<status>,<null_or_reason>
```

`traffic_generator.py` 目前支持单次请求采样，正式评测的多请求汇总需在保留全量原始采样的基础上进行聚合计算；严禁将单次采样的 `ttft_ms` 直接复制填充至 P50/P95/P99 三列。

### 6.4 证据包目录结构

```text
results/PVT-00/<mode>/<run_id>/
├── manifest.json
├── environment.json
├── raw_events.jsonl
├── raw_metrics.*
├── raw_stdout/
├── summary.json
├── summary.csv
└── logs/
```

`manifest.json` 至少完整固化代码包版本、基线 Git Commit、配置哈希、输入数据 Schema、模型物理布局、Prompt Token 数、通信协议与运行模式、硬件环境与拓扑、执行 CLI 命令、原始数据哈希、证据等级、功能支持范围、未支持特性、准入门限及最终判定状态。

---

## 7. GO / CONDITIONAL / NO-GO / NOT-SUPPORTED / INVALID-EVIDENCE 判定规则

### 7.1 命题一：协议加速对照判定

- **GO（物理协议实测达到候选准入门槛）**：URMA 与 UBMEM 均具备真实的底层驱动完成事件、相同的 Payload 尺寸/并发度/传输方向/硬件设备/拓扑，且完成至少 3 轮独立重复测量；在声明的测试区间内，候选 UBMEM 传输总路径相对 URMA 的加速比达到运行前冻结的 $\ge 1.30\times$ 门限，且传输错误率、重试率及尾部时延稳定性未出现不可接受的劣化。该结论仅对记录的代码包、硬件和拓扑负责。
- **CONDITIONAL（局部硬件实测支持）**：仅在 W1/LAB 环境下完成部分硬件协议的局部观测，或仅覆盖部分传输方向、并发度及设备；结论严格限定于已测物理条件，严禁外推至全量协议路径。
- **NOT-SUPPORTED（功能未支持）**：当前仍处于本地 `memcpy` 测试桩阶段、底层物理驱动不可用或无法提供 `actual_path` 实测凭证；该状态表示当前工程尚未具备测试条件，不代表物理协议性能失败。
- **NO-GO（物理路径未产生正向收益）**：在合法的 A/B 对照实测中 UBMEM 加速比未达准入门槛，或引入新协议后错误率、失败率或 P99 尾部时延出现显著恶化。

### 7.2 命题二：Saved-Prefill 业务净收益判定

- **GO（同场次实测收益形成完整闭环）**：在至少 50% 前缀复用率及声明的模型/上下文长度条件下，完成至少 3 轮独立重复实测；R1 就绪事件、R2 实测路径、同场次本地重算基线及版本凭证完整齐备；$T_{net\_saved} > 0$ 且满足预先固化的 $T_{net\_saved} \ge 2.0 \times (目录查询 + 数据加载 + 校验/挂接/同步)$ 准入门槛。
- **CONDITIONAL（特定场景受限支持）**：仅在 70% 以上高复用率、超长上下文或特定模型架构下体现出净加速收益，结论严格限定于该场景白名单；若仅包含单次 TTFT 采样或 W0/DEMO 结果，亦仅能作为流程性局部结论保留。
- **NO-GO（业务收益持续为负）**：在声明的有效测试区间内，远端数据拉取、语义校验与显存挂接的总开销持续高于本地直接重算耗时，且经由路径与负载复核确认无法获得正向净收益。
- **NOT-SUPPORTED（业务证据未闭环）**：缺乏可消费的就绪事件凭证、运行时模型物理布局、真实推理端点或同场次重算配对数据，无法形成对应的业务收益结论。

### 7.3 统一无效证据规则

凡出现以下任一情形，对应子实验一律判定为 `INVALID-EVIDENCE`，严禁给出 `GO` 结论：
- 仅凭 CLI 传入的 `--protocol` 或 `--mode` 参数标签代替真实底层驱动、服务端代码包或实际物理路径切换；
- 将本地 `memcpy` 内存拷贝、固定 sleep 延时、伪随机 Token ID 或单次 HTTP 请求的 TTFT 包装为真实通信协议、缓存安全就绪或业务统计分位数；
- 缺失同场次的本地重算基线，或使用非同场次的粗略历史平均值替代当前 R2 请求的基准；
- `planned_path` 与 `actual_path` 无法提供客观物理凭证，或关键的模型物理布局、Tokenizer 词表、硬件设备、拓扑及配置哈希缺失；
- 原始采样数据、失败请求日志、重复实验轮次、版本清单或 `manifest.json` 归档不全；
- 关键指标缺失却以 `0` 数值违规填充，或将测试脚本正常退出返回的 `OK` 直接当作性能达标依据；
- A/B 对照测试中擅自变更了硬件设备、测试负载、资源配额、请求序列、预热策略或统计口径；
- 将内部脚本返回的 `INVALID_EVIDENCE`、`BASELINE_INVALID` 等异常状态在未经技术归因与复核的情况下直接修改为 `GO`。

---

## 8. 执行阶段与交付闭环

测试实施划分为三个严密的演进阶段：

| 实施阶段 | 核心攻坚内容 | 阶段必须交付物 | 准出判定条件 |
|---|---|---|---|
| 阶段 A：工具审计与 W0 基线 | 审计验证工具源码的实际行为，跑通本地协议测试桩、合成负载生成、R1/R2 HTTP 闭环及字段校验 | 源码审计报告、执行日志、workload manifest、`DEMO` 级证据包 | CLI 命令可完全复现，已明确标识本地测试桩、固定 sleep 延时及未支持特性 |
| 阶段 B：协议与业务局部实测 | 接入原厂真实驱动或部署真实模型端点，系统扫描 Payload 尺寸、并发度、五档复用率、MLA/MHA 架构及四种运行模式 | 原始事件日志、异常请求记录、TTFT 统计分位数、净收益分析表、硬件性能计数器及重复实测汇总 | 每个指标均能精准追溯至原始采样点，证据等级与实测物理路径明确闭环 |
| 阶段 C：标准证据包与决策 | 对账同场次本地重算、开源原生 Mooncake、单项增强及完整增强方案，输出分项技术决策 | 完整的 `manifest.json`、`environment.json`、协议基准表、业务对账表、技术总结及未支持说明 | 全量数据通过公共契约规范校验，彻底杜绝以模拟数据冒充物理实测结果 |

本验证项的核心价值在于明确 Saved-Prefill 在何种复用率、模型布局及硬件网络链路条件下能够产生确定的净加速收益，并从物理层面量化通信协议加速比是否具备进入后续 QueryPlan（查询与放置计划决策引擎，即按链路状态、算力和上下文长度选择最优加载或重算路径）设计的工程价值。本项严禁用单机内存拷贝测试桩替代物理协议结论，亦不可将单次缓存命中直接等同于生产级业务收益。

---

## 9. 工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师核心职责**：
  1. 确认被测大模型权重、Tokenizer 词表、硬件设备型号、节点部署角色、通信协议驱动、代码包版本、网络拓扑及目标证据等级；
  2. 固化前缀复用率档位、Prompt Token 总数、并发压力、预热轮次、重复采样次数、A/B 准入门限及结果归档目录；
  3. 实际执行 R1/R2 请求与同场次本地重算，完整保存原始请求流、失败异常日志、远端就绪事件及硬件运行凭证；
  4. 严格审定各项指标是否确实由底层物理硬件、通信协议栈或推理引擎真实采样所得，审慎决断填报实测数值、`null`、`NOT-SUPPORTED` 还是 `INVALID-EVIDENCE`；
  5. 对现场硬件拓扑与模型实测结论进行最终技术把关与签字确认。
- **AI Agent 协同职责**：
  1. 研读本方案设计、公共测试契约及原型验证源码，梳理实际支持的 CLI 参数、依赖库、输出字段及当前未实现功能；
  2. 编写日志解析抽取、逐请求统计聚合、同场次重算基线自动配对、原始文件哈希校验及标准证据包生成工具；
  3. 核验前缀复用率计算公式、TTFT 分位数分布、净加速收益及四模式 A/B 对照的字段完整性；
  4. 严守技术诚信红线，严禁虚构 URMA/UBMEM 驱动调用、伪造模型实测成绩或编造不存在的服务端点，严禁将脚本正常退出的 `OK` 状态改写为准入通过。

### 9.2 可直接复制给 Coding Agent 的 Prompt 模板

```text
我正在执行 PVT-00：业务流量 Saved-Prefill 收益上限与通信协议加速评估。

请先研读以下核心文件：
1. ./提前验证方案设计/验证计划方案设计/01_PVT-00_业务流量Saved-Prefill收益上限评估实施方案设计.md
2. ./提前验证方案设计/验证计划方案设计/Benchmark公共契约与证据分级规范.md
3. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-00/Makefile
4. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-00/proto_bench.cc
5. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-00/make_workload.py
6. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-00/traffic_generator.py

执行约束与任务要求：
- 首先梳理源码实际支持的 CLI 参数、依赖库、输出字段与物理路径；确认 proto_bench.cc 当前仅执行本地 memcpy 流程，严禁将 --protocol urma/ubmem 误判为真实的底层驱动切换。
- 确认 make_workload.py 当前生成的是用于流程验证的伪随机 Token ID 序列；在正式 LAB/MEASURED 实测中必须严格校验 model_layout_manifest 与 kv_bytes_per_token。
- 凡缺少真实硬件驱动、远端就绪事件、同场次本地重算基线或实测路径凭证的数据，一律规范标记为 DEMO、NOT-SUPPORTED 或 INVALID-EVIDENCE，严禁使用预置固定值进行失真填充。
- 针对 30%、50%、70%、90%、98% 五档前缀复用率生成测试负载，并精确复算 prefix_tokens / (prefix_tokens + unique_tokens)。
- 针对每条 R2 请求，严格留存同场次 recompute、mooncake_native、unified_single、unified_full 模式下的 workload_id、package_id、config_hash、planned_path、actual_path 及实测 TTFT。
- 未采集到的字段显式置为 null 并详细注明 invalid_reason；完整留存失败请求；严禁将单次采样 TTFT 粗暴复制为 P50/P95/P99。
- 最终输出：源码能力核验矩阵、实际执行命令清单、字段数据字典、统计复算结果、证据等级评定、未支持特性清单、无效证据归因分析以及下一步最小代码重构建议。
```

### 9.3 常见排错指南

- **编译报错找不到 `liburma.so` 或 `libubmem.so`**：当前受控 `Makefile` 默认未链接上述协议库，`proto_bench` 能够顺利编译仅代表本地测试桩可用；应先将测试结果定级为 `DEMO`，严禁通过手动修改标签将其包装为协议实测成绩。
- **实测协议加速比出现异常极大或极小值**：检查两次运行是否仅为 URMA 分支内部忙等空循环造成的虚假时延差异；在接入原厂真实驱动 SDK 之前，任何测试桩比值均仅能作为流程演示现象记录。
- **LAB/MEASURED 实测启动报错提示缺少 `kv_bytes_per_token`**：必须为 `make_workload.py` 提供包含该指标的运行时模型物理布局清单；严禁将 `35KB/tok` 或 `320KB/tok` 等纸面估算值直接填报为正式实测字段。
- **`ready-url` 或 `ready-file` 等待超时**：检查 R1 请求是否已真实写入目标服务实例，就绪事件的通知标识是否与当前 `workload_id` 及 `run_id` 精准匹配；严禁在 LAB/MEASURED 实测中采用固定 sleep 延时绕过真实的就绪事件。
- **输出结果中 `net_saved_ms` 字段显示为 `null`**：检查执行命令中是否正确传入了 `--recompute-baseline-json` 参数，且该基准文件中是否包含同场次测得的有效 `ttft_ms`；在缺失基准数据时仅可记录当前请求时延，不可关闭净收益判定。
- **HTTP 调用提示 `Connection refused` 或捕获的首字事件为空**：检查推理端点网络可达性、`/v1/completions` 接口路由、模型实例健康状态及 SSE 流式响应输出；若服务返回 HTTP 200 但未包含有效的首个流式数据块，应将该样本记录为失败并排查原因。
- **通用 `parse_benchmark_metrics.py` 解析返回 `INVALID_EVIDENCE`**：检查 benchmark 导出的 JSON 是否完整包含 QPS、TTFT、TPOT 及背景吞吐等必要字段；该工具属于通用解析器，不内置 R1/R2 配对逻辑，不可用其替代 PVT-00 专属的同场次对账要求。
- **计算得出的复用率与文件名标注不一致**：统一以 workload JSON 内 `metadata.reuse_ratio`、Token 实际数量及随机种子为准；重新生成测试负载时应同步刷新 `workload_id`、配置哈希及 manifest。
- **单次 HTTP 采样结果误填为 P99 指标**：扩展测试工具以发送足量请求样本并完成多轮独立重复测量，基于全量原始事件计算权威分位数；若样本量不足应显式填写 `null` 并注明 `invalid_reason`。
