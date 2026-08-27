# PVT-00：业务流量 Saved-Prefill 收益上限评估实施方案设计
## —— Mooncake 原生传输开销定位与国产通信协议加速上限评估

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个 `run_id` 必须冻结工作负载、代码包、模型布局、拓扑、并发、预热、样本量和证据等级；结果必须保留原始样本、失败请求、实际路径和结论状态。

> **验证范围声明**：当前受控工程中的 `proto_bench.cc` 只在本地分配内存并执行 `memcpy`，没有调用 URMA 或 UBMEM 驱动；`make_workload.py` 生成的是随机 Token ID 合成负载，不是完整真实模型推理；`traffic_generator.py` 可以向实际 HTTP 端点发送 R1/R2 请求，但不会自动证明缓存已经可消费，也不会生成多请求分布统计。因此，当前代码默认只能形成 `DEMO / W0` 工作流证据，不能单独关闭 E0 的真实协议或业务收益结论。

> **术语速查**：KVCache（大模型注意力键值缓存，即自回归生成过程中保存历史 Key 和 Value 激活状态、避免后续 Token 重复计算注意力）；Saved-Prefill（首字生成预计算节省，即利用已缓存的 KVCache 跳过已复用 Prompt 前缀的重复预计算）；TTFT（Time To First Token，首字生成延迟）；MHA（Multi-Head Attention，多头注意力，通常产生较大的逐 Token KV 数据）；MLA（Multi-head Latent Attention，多头潜在注意力，通过潜变量压缩 KV 状态）；URMA（通用远程直接内存访问，即用户态高性能远程内存访问接口）；UBMEM（统一总线内存直通共享协议，即支持跨节点与异构设备直接共享内存地址空间的底层通信协议）。

> **验证 ID**：PVT-00
> **验证名称**：业务流量 Saved-Prefill 收益上限与通信协议加速评估
> **验证优先级**：**🟡 P1 级（底座支撑项）**
> **对应验证阶段**：**E0（业务收益前提确认）**
> **证伪标记**：否（业务收益前提确认）
> **建议周期**：3~5 人日
> **主关联 IR**：`IR-02-11`, `IR-02-12`
> **核心 SRS / SR23 锚点**：
> - SRS：`L3-OB-PerPathTelemetry-047`, `L1-OB-SemanticMetrics-016`, `SE-MONITOR-001`, `SE-PERF-001`
> - SR23：`SR23-02-11-01`, `SR23-02-11-02`, `SR23-02-12-01`, `SR23-02-12-05`
> **配套源码**：[`提前验证方案设计/验证计划方案设计/原型验证代码/PVT-00/`](file:///d:/codes/reports/kvcache/unified_kv_memory/提前验证方案设计/验证计划方案设计/原型验证代码/PVT-00)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`；vLLM `842dd8fd96650063e1ad32e6075742d457d39773`；vLLM-Ascend `424e27e1fd2b1c6e0d7fe659b489b87c1223a33c`。正式结果必须以 `package_id`、`baseline_commit` 和配置哈希重新核对，不能只引用这里的默认值。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统软件视角：缓存只有在拉取成本低于重算成本时才有价值

在数据库、RPC 网关或 Web 后端中，缓存的收益并不由“命中”两个字决定，而由一次缓存命中的完整路径决定：目录查询、数据读取、反序列化、校验、挂接和后续同步都要计入。如果这条路径比本地重新执行计算更慢，缓存反而会产生负收益。

可以把一次请求拆成两条可比较的路径：

```text
本地重算：        请求进入 → Prefill 计算 → 首字生成
复用 KVCache：    请求进入 → 查询目录 → 加载 KV → 校验/挂接/同步 → 未命中部分 Prefill → 首字生成
```

PVT-00 不先假设“缓存必然更快”，而是先把两条路径放在同一场次、同一工作负载和同一硬件条件下测量。只有当复用路径的端到端 TTFT 低于同场次本地重算，且差值足以覆盖查询、加载、挂接和同步成本，Saved-Prefill 才具备进入后续调度设计的工程依据。

### 0.2 大模型推理中的对应物理问题

大模型的 Prefill（首字生成预计算，即对完整 Prompt 做输入理解并生成首个输出 Token 前的计算阶段）通常要处理较长输入。Prefill 期间产生的 KVCache 会被 Decode（逐 Token 生成阶段，即基于历史 KVCache 反复生成后续 Token）重复读取。

当后续请求复用了前一个请求的 Prompt 前缀时，系统有两种选择：

1. 重新执行这段前缀的 Prefill，消耗 NPU/GPU 计算时间；
2. 从外接 KV 存储池加载已保存的 KVCache，绕过这段前缀的重复计算，但要承担目录查询、数据传输、布局转换、校验、挂接和同步成本。

Qwen MHA 与 DeepSeek MLA 代表两种不同的 KV 数据密度。MHA 的单 Token KV 数据量通常更大，传输带宽更容易成为瓶颈；MLA 的 KV 状态更紧凑，目录查询、协议固定开销和挂接成本可能占比更高。文档中的约 `320KB/tok` 与 `35KB/tok` 只是输入构造的初始布局估算，正式结果必须以运行时 `model_layout_manifest` 为准。

### 0.3 当前配套工程能够证明什么，不能证明什么

公共契约把证据分成 DEMO、LAB 和 MEASURED 三档，并把环境标记为 W0、W1、W2。本项必须把“流程和公式跑通”与“目标驱动和真实业务收益被测到”分开归档。

| 子实验 | 当前源码能够完成的动作 | 当前源码不能直接证明的内容 | 当前默认证据状态 |
|---|---|---|---|
| 协议微基准 | 对本地 `malloc` 缓冲区做并发 `memcpy`，输出带宽、平均时延、P50、P99 和 CSV | URMA/UBMEM 设备带宽、DMA 完成时延、远端访问、协议差异、网卡/总线拥塞 | `DEMO / W0` |
| 工作负载生成 | 生成 R1 前缀预热请求与 R2 前缀复用请求，写出 `pvt00.workload.v1` JSON | 真实 Tokenizer、真实模型布局、真实 Prefill 算子和业务请求分布 | `DEMO`；提供运行时布局后才具备 LAB 输入条件 |
| 在线端点闭环 | 调用 `/v1/completions`，测量 R2 首个非空流式响应的 TTFT，写出一行 CSV | 目录命中、实际 KV 加载路径、逐请求传输分段、完整分位数和真实协议归因 | 由运行参数决定；无硬件路径时为 `DEMO` |
| W0 集群脚本 | 在 localhost 上启动 Mooncake Master、Prefill、Decode 和代理的流程示范 | 双节点、真实 RDMA/URMA/UBMEM、真实 NPU 模型和跨节点收益 | `DEMO / W0` |
| 通用 benchmark 解析 | 解析 `benchmark_serving` JSON 的吞吐、TTFT、TPOT 等字段 | 不能替代 PVT-00 的 R1/R2 同场次重算配对，也不能证明协议已切换 | `DEMO` 或绑定实际路径后的 `LAB` |

因此，当前任何 `proto_bench` 输出都只能描述“本地内存复制工作流”；任何 `traffic_generator.py` 输出都必须同时给出 `mode`、`actual_path`、代码包、配置哈希、就绪事件和同场次重算基线。缺少这些字段时，结果必须标为 `NOT-SUPPORTED` 或 `INVALID-EVIDENCE`，不能通过改变标签变成协议性能结论。

---

## 1. 验证目标与交付结论定义

### 1.1 现实前因痛点与待验证核心命题

1. **命题一：Saved-Prefill 是否存在净收益**。针对 Qwen MHA 与 DeepSeek MLA，在 30%、50%、70%、90%、98% 五档前缀复用率下，对比同场次本地重算和 KV 复用路径的端到端 TTFT，找出可形成净收益的上下文长度与复用率边界。
2. **命题二：传输协议是否存在可测的加速差异**。在相同 payload、并发、方向、设备和拓扑下，对比 URMA 与 UBMEM 的目录/数据路径时延、带宽和 P99；当前本地 `proto_bench.cc` 不具备该能力，必须先完成真实 SDK 接入或把该命题标为 `NOT-SUPPORTED`。
3. **命题三：上层收益能否由底层路径拆解解释**。通过请求进入、目录查询、数据加载、校验/挂接、未命中部分 Prefill 和首字事件，判断收益来自何处；如果只有 TTFT 而没有路径事件，最多形成业务现象记录，不能关闭底层归因。

### 1.2 交付物与结论边界

每个正式 `run_id` 至少交付：

1. 《URMA vs UBMEM 协议传输性能基准表》：覆盖冻结的 payload、并发、方向和重复轮次，保留原始样本、失败样本和设备计数器；
2. 《本地重算、原生 Mooncake、单项增强和完整增强四模式对账表》：每一条 R2 记录均能回指同场次 R1、就绪事件、实际路径和代码包；
3. 《MLA vs MHA 五档复用率 TTFT 收益交叉对账表》：记录运行时 KV 字节数、前缀 Token 数、未命中 Token 数、TTFT 分位数和净收益；
4. `manifest.json`、`environment.json`、原始事件、原始 stdout、汇总 CSV、汇总 JSON 和日志；
5. 按协议微基准和业务复用两条命题分别输出 `GO`、`CONDITIONAL`、`NO-GO`、`NOT-SUPPORTED` 或 `INVALID-EVIDENCE`，不把不同证据等级合并成一个总成绩。

---

## 2. 实验方案与测试矩阵设计

### 2.1 四种被测模式与公平 A/B

| 模式 | 目标被测对象 | 运行要求 | 当前脚本状态 |
|---|---|---|---|
| `recompute` | 禁用远端 KV 复用的完整代码包或配置 | R2 在同一端点、同一模型和同一工作负载下直接重算 | `traffic_generator.py` 支持；实际端点仍需工程师提供 |
| `mooncake_native` | 固定 Commit 的原生 Mooncake 完整代码包或配置 | 只能切换到真实原生端点，不能仅修改 `--mode` 字符串 | CLI 支持标签；当前仓未随脚本提供可运行服务 |
| `unified_single` | 只开启一项目标增强的完整代码包或配置 | 明确启用的增强项、代码包和配置哈希 | CLI 支持标签；目标服务需现场提供 |
| `unified_full` | 开启待验收增强集合的完整代码包或配置 | 记录所有开关和实际路径，不能把计划路径当完成路径 | CLI 支持标签；目标服务需现场提供 |

同一次 A/B 必须保持模型权重、TP、Tokenizer、输入工作负载、请求顺序、并发、请求率、设备、拓扑、资源配额、编译参数、预热轮数、测量轮数和统计口径一致。只改变待验证代码包或配置；如果实际路径没有改变，或者 `planned_path` 与 `actual_path` 无法闭环，结果无效。

### 2.2 协议微基准参数矩阵

| 维度 | 正式计划取值 | 当前源码支持情况 | 备注 |
|---|---|---|---|
| payload | 4KB、64KB、256KB、1MB、4MB、16MB、64MB；必要时扩展 128MB | 支持 `--payload-bytes` 单值运行；默认数组覆盖到 64MB | 每个 payload 单独生成一组结果，不能把不同运行混为一组 |
| 并发 | 1、4、16、32、64 | 支持 `--concurrency` 单值运行；默认数组覆盖五档 | 当前并发单位是本地 C++ 线程，不是 RDMA QP 或真实请求流 |
| 方向 | Write、Read、双向混流 | 当前代码固定执行本地 `memcpy` 并标为 `write` | 真实方向必须由驱动完成事件确认 |
| 协议 | URMA、UBMEM | 当前 `--protocol` 只改变代码内分支与忙等循环 | 不能据此形成协议差异结论 |
| 预热与测量 | 运行前冻结；建议至少 1 轮预热、3 次独立重复 | 当前 `proto_bench` 只按 `--duration-sec` 或 `--iters` 循环 | 正式脚本需额外保存重复轮次和环境快照 |

### 2.3 业务流量与模型参数矩阵

| 维度 | 正式计划取值 | 当前生成器支持情况 | 证据要求 |
|---|---|---|---|
| 模型架构 | Qwen MHA、DeepSeek MLA | `--model-type mha` / `mla` / `gqa`；只生成 metadata 和 Token ID | 正式结果必须提供真实模型和 `model_layout_manifest` |
| 前缀复用率 | 30%、50%、70%、90%、98% | 通过 `prefix_tokens` 与 `unique_tokens` 计算 | 以输出 JSON 的 `reuse_ratio` 为准，不以文件名推断 |
| 上下文总长 | 8K、32K、64K、128K、256K 或现场可用等价档位 | 通过 Token 数参数化；当前默认示例为 100K Token | 每个总长都要记录显存、布局和实际可消费状态 |
| 请求并发 | 1、4、16、32、64 | `traffic_generator.py` 一次只发送 R1 和 R2 | 多请求分布需使用真实压测器或扩展脚本，不能把单次结果当 P99 |
| 代码模式 | recompute、mooncake_native、unified_single、unified_full | CLI 支持四个字符串 | 每个模式使用独立 `package_id` 或配置哈希 |

五档复用率的等长构造示例（总 R2 Prompt 为 100K Token）：

| 复用率 | `prefix_tokens` | `unique_tokens` | 实际计算 |
|---:|---:|---:|---:|
| 30% | 30,000 | 70,000 | `30,000 / 100,000` |
| 50% | 50,000 | 50,000 | `50,000 / 100,000` |
| 70% | 70,000 | 30,000 | `70,000 / 100,000` |
| 90% | 90,000 | 10,000 | `90,000 / 100,000` |
| 98% | 98,000 | 2,000 | `98,000 / 100,000` |

这张表是输入构造方案，不是预置收益结论。若真实业务 Token 数或前缀分布不同，应按现场 workload manifest 扩展，而不是强行套用 100K Token。

### 2.4 环境与证据矩阵

| 环境 | 目的 | 最低条件 | 允许形成的结论 |
|---|---|---|---|
| W0 单机/localhost/Mock | 验证参数、命令、字段、R1/R2 关系和解析流程 | Python、C++ 编译器、localhost 端点或 Mock | 仅形成 `DEMO` 工作流结论 |
| W1 局部设备实测 | 观察绑定设备上的协议或端点行为 | 可用 NPU、网卡、SSD、驱动 SDK、模型布局和路径凭证 | 形成绑定版本与拓扑的 `LAB` 局部结论 |
| W2 代表性跨节点实测 | 关闭 E0 业务收益与协议对照结论 | 真实跨节点数据路径、真实代码包、设备完成量、同场次重算、重复实验 | 证据闭环后形成 `MEASURED` 结论 |

---

## 3. 实验动力学模型与统计口径

### 3.1 R1/R2 工作负载与复用率

`make_workload.py` 的当前模型是：R1 只包含前缀，R2 由同一段前缀和一段新 Token 拼接而成。

```text
R1: [---------------- Prefix A ----------------] → Prefill & Store
R2: [---------------- Prefix A ----------------][---------------- New B ----------------]
                    ^------ 可复用 ------^       ^------ 必须计算 ------^
```

当前脚本实际计算：

$$
reuse\_ratio=\frac{prefix\_tokens}{prefix\_tokens+unique\_tokens}
$$

脚本用随机数生成 Token ID，并通过 `model_layout_manifest` 读取 `kv_bytes_per_token`。因此：

- R1/R2 的 Token ID 关系可复现，但不等于真实业务 Tokenizer 的语言分布；
- `kv_bytes_per_token` 缺失时可以运行 DEMO，`traffic_generator.py` 在 LAB/MEASURED 下会拒绝该输入；
- `320KB/tok`、`35KB/tok` 和 TP=8 单卡分摊值只能作为初始估算，不能覆盖运行时布局 manifest；
- 只有真实端点产生 `ready_url` 或 `ready_file` 等价事件，R2 才能作为已具备消费条件的复用请求进行对账。

### 3.2 协议微基准的物理口径

理想情况下，协议单次传输时间可拆成固定提交、排队、设备 DMA、远端完成和同步开销：

$$
T_{protocol}=T_{submit}+T_{queue}+T_{DMA}+T_{remote\_complete}+T_{fence}
$$

但当前 `proto_bench.cc` 实际执行的是：

```text
本地 malloc → memset → memcpy(src_buf, dst_buf) → 本地高精度时钟 → 释放内存
```

`--protocol ubmem` 与 `--protocol urma` 目前都走本地 `memcpy`，URMA 分支额外执行一段忙等循环。这个分支差异只是测试桩代码路径，不是 URMA 与 UBMEM 的物理实现。因此，当前 CSV 中的 `bandwidth_gbps`、`latency_*_us` 只描述本地内存复制工作流，不能作为两种协议的加速比。

### 3.3 Saved-Prefill 端到端成本分解

对同一条 R2 请求，复用路径的 TTFT 可以按事件拆分为：

$$
TTFT_{reuse}=T_{dir\_query}+T_{data\_load}+T_{validate\_attach\_sync}+T_{uncached\_prefill}+T_{service}
$$

本地重算路径为：

$$
TTFT_{recompute}=T_{full\_prefill}+T_{service}
$$

Saved-Prefill 净收益写成直接可对账的文字关系：

$$
T_{net\_saved}=TTFT_{recompute}-TTFT_{reuse}
$$

只有 `T_net_saved > 0` 才说明该场景没有负收益。进一步的候选门槛为：

$$
T_{net\_saved}\ge 2.0\times(T_{dir\_query}+T_{data\_load}+T_{validate\_attach\_sync})
$$

该门槛必须在运行前写入 manifest，不能根据结果倒推；如果事件未采集齐全，不能用端到端差值冒充底层分项开销。

### 3.4 分位数与重复实验

单次 `traffic_generator.py` 调用只输出一条 R2 记录，不能直接提供 P50/P95/P99。正式业务结果必须在每个模式、复用率、模型和请求率条件下保留逐请求原始 TTFT，再从原始样本计算分位数。

- 默认至少 1 轮预热、3 轮独立重复；
- 失败请求必须保留，不能只统计成功请求；
- P99/P99.9 只能从足够数量的原始样本计算；样本不足时写 `null`，并填 `invalid_reason`；
- 同场次本地重算基线必须和 R2 使用相同工作负载、模型、请求顺序、设备、资源配额和统计口径；
- 协议微基准的平均带宽不能替代端到端 TTFT，端到端 TTFT 也不能反推协议已经使用 UBMEM。

---

## 4. 测试工具、当前实现边界与正式扩展要求

### 4.1 当前受控工程目录与运行方式

```text
原型验证代码/PVT-00/
├── Makefile
├── proto_bench.cc          # 当前只执行本地 memcpy 的协议流程脚手架
├── make_workload.py        # 生成 pvt00.workload.v1 合成 Token 负载
└── traffic_generator.py    # 调用 HTTP 端点，采集 R1/R2 的单次 TTFT

原型验证代码/deploy_and_bench_e2e/
├── deploy_cluster.sh       # 当前只允许 W0 localhost 工作流
├── run_online_benchmark.sh  # benchmark_serving 预热、测量和解析入口
└── parse_benchmark_metrics.py
```

当前确认可用的 PVT-00 协议微基准命令：

```bash
cd ./原型验证代码/PVT-00
make
./proto_bench --protocol urma --payload-bytes 1048576 --concurrency 4 --iters 1000 --out proto_urma_demo.csv
./proto_bench --protocol ubmem --payload-bytes 1048576 --concurrency 4 --iters 1000 --out proto_ubmem_demo.csv
```

当前 CLI 只包含：

```text
--protocol       代码分支标签，当前可填 urma 或 ubmem
--payload-bytes  单次运行的 payload 字节数
--concurrency    本地 C++ 线程数
--duration-sec   iterations 未指定时的运行秒数
--iters          每个线程的循环次数；指定后优先于 duration
--out            输出 CSV 路径
```

不要把不存在的 `--direction`、`--device`、`--qp-count`、`--remote` 或真实 SDK 参数写进当前命令。若要测真实 URMA/UBMEM，先完成第 4.3 节的工程扩展并重新锁定代码包。

### 4.2 源码实际行为审计

| 代码路径 | 实际行为 | 对证据的影响 |
|---|---|---|
| `proto_bench.cc::worker_transfer` | `malloc` 两块本地缓冲区、`memset` 后执行 `memcpy`；`ubmem` 与 `urma` 都不包含 SDK 头文件或驱动调用；URMA 分支只多一段忙等循环 | 输出不能证明 URMA/UBMEM 带宽、DMA、远端完成或协议差异；默认为 `DEMO / W0` |
| `proto_bench.cc::run_benchmark_case` | 汇总本地线程的总字节数和时延，计算平均值、P50、P99，输出 `DEMO,LOCAL_MEMCPY_ONLY` | `bandwidth_gbps` 是本地复制吞吐；空样本时输出 0 只是统计占位，不是 0 时延证明 |
| `Makefile` | 仅链接 `-pthread`，不链接 `liburma.so` 或 `libubmem.so` | 当前工程即使没有原厂驱动也能编译，但这不代表支持真实协议 |
| `make_workload.py` | 随机生成 Prefix A 和 New B Token ID，写出 workload schema、布局字节数和复用率 | 是可复现合成输入，不是实际模型推理或真实业务流量 |
| `traffic_generator.py::send_prompt` | 向 `<endpoint>/v1/completions` 发送 JSON prompt，按第一个非空流式响应行记录 TTFT | 没有读取模型内部目录命中、DMA 完成或 NPU 事件；一次调用只有一条 R2 样本 |
| `traffic_generator.py::wait_until_ready` | LAB/MEASURED 必须提供 `--ready-url` 或 `--ready-file`；DEMO 无事件时执行固定 sleep | 固定 sleep 只能作 DEMO；没有真实就绪事件时不能宣称 KV 已可消费 |
| `traffic_generator.py::main` | 非 recompute 模式先发送 R1，再等待就绪，再发送 R2；`--recompute-baseline-json` 缺失时 `net_saved_ms` 为 `null` | R1/R2 顺序可示范；没有同场次基线时不能关闭收益结论 |
| `deploy_cluster.sh` | `EVIDENCE_ENVIRONMENT` 非 W0 直接退出；W0 通过 localhost 启动 Master、Prefill、Decode 和代理 | 不能由该脚本形成双节点或真实 RDMA/UBMEM 结论 |
| `parse_benchmark_metrics.py` | 解析 `benchmark_serving` JSON 的 QPS、TTFT、TPOT；缺字段时输出 `INVALID_EVIDENCE` | 是通用 benchmark 摘要解析器，不包含 PVT-00 R1/R2 同场次配对；正式报告需统一状态枚举 |

特别注意：`traffic_generator.py` 的 `--protocol` 和 `--mode` 是结果字段和分支选择，不会自动替换服务端代码包。只有 `package_id`、配置哈希和 `actual_path` 能证明待测模式真的切换。

### 4.3 面向 LAB/MEASURED 的最小工程扩展

进入真实协议或业务收益结论前，至少补齐：

1. **真实驱动路径**：让 URMA/UBMEM SDK 完成真实注册、提交、远端访问和完成事件采集；分别支持方向、队列/并发和错误码，不能继续以本地 `memcpy` 代替；
2. **完整路径凭证**：为每条请求记录 `planned_path`、`actual_path`、代码包、配置哈希、设备序列或等价硬件标识；
3. **运行时布局与语义校验**：记录 `model_layout_manifest`、Tokenizer 哈希、模型架构、TP 切分、对齐和实际 `kv_bytes_per_token`；
4. **真实就绪事件**：提供 `ready_event`、`visibility_epoch` 或等价消费凭证，替换 LAB/MEASURED 中的固定 sleep；
5. **逐请求结构化事件**：至少保留请求进入、目录查询、数据加载、校验/挂接/同步、Prefill 开始和首字事件，不能只输出一条总 TTFT；
6. **同场次重算配对**：本地重算与每条 R2 使用同一 workload、同一请求序列或可复核配对键；不能用另一轮运行的单个平均值替代；
7. **多请求统计**：实现请求率、并发、预热、重复轮次和失败请求归档，从原始事件计算 P50/P95/P99；
8. **协议与设备计数器**：记录实际带宽、DMA 完成、重试、错误、队列深度和设备利用率；采集不到的字段使用 `null` 并填写 `invalid_reason`；
9. **统一状态输出**：把脚本内部的 `OK` 或 `INVALID_EVIDENCE` 转换为公共契约的 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE`，不能把 `OK` 直接当作准入通过。

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结实验身份、证据等级和公平 A/B 条件

- **操作意图**：先确定本轮是 W0/DEMO、W1/LAB 还是 W2/MEASURED，防止把本地复制或单次 HTTP 结果写成真实协议结论。
- **执行动作**：填写 `run_id`、`workload_schema_version`、`workload_id`、`package_id`、`baseline_commit`、`config_hash`、`model_layout_manifest`、`tokenizer_hash`、`hardware_profile`、`topology_profile`、`evidence_level`、模式、复用率、预热轮数、测量轮数和门槛。
- **应观察现象**：能明确列出本轮实际路径、未覆盖字段和输出目录；如果没有真实驱动、模型布局或就绪事件，提前记录 `NOT-SUPPORTED`，不要等到结果阶段再补写。

### 步骤 1：审计并运行协议微基准 W0 基线

- **操作意图**：先确认编译、参数、CSV 输出和本地并发统计闭环，建立后续扩展的流程基线；本步骤不验证 URMA/UBMEM 性能。
- **执行命令**：

```bash
cd ./原型验证代码/PVT-00
make
./proto_bench --protocol urma --payload-bytes 1048576 --concurrency 4 --iters 1000 --out proto_urma_demo.csv > proto_urma_stdout.txt 2>&1
./proto_bench --protocol ubmem --payload-bytes 1048576 --concurrency 4 --iters 1000 --out proto_ubmem_demo.csv > proto_ubmem_stdout.txt 2>&1
```

- **应观察现象**：终端出现 payload、线程数、带宽和 P99；CSV 的 `evidence_level` 为 `DEMO`，状态包含 `LOCAL_MEMCPY_ONLY`。
- **判定边界**：本步骤只能证明本地工作流可运行。不能将两个 CSV 的比值写成协议加速比，也不能用 `--protocol` 参数替代实际驱动切换。

### 步骤 2：生成运行时布局绑定的五档复用率负载

- **操作意图**：把复用率变成可审计的 Token ID 关系和 workload manifest，避免只用文件名或纸面百分比描述输入。
- **执行动作**：先准备包含 `kv_bytes_per_token` 的运行时布局清单，再按表 2.3 的五档比例分别运行；下面以 50% 为例：

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

- **应观察现象**：输出 JSON 中 `schema_version` 为 `pvt00.workload.v1`，R1 的 Token 序列等于 R2 的前缀，`reuse_ratio` 与请求参数一致，`kv_bytes_per_token` 来自 manifest。
- **证据边界**：没有真实模型布局时可以保存 DEMO 负载，但不能把初始 `35KB/tok` 或 `320KB/tok` 写成实测值；LAB/MEASURED 缺布局时应停止该轮。

### 步骤 3：先执行同场次本地重算并固化基线

- **操作意图**：给每个复用场景建立可回指的 R2 本地重算基线，避免用另一轮平均值掩盖设备、负载或服务状态变化。
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

- **应观察现象**：`mode` 为 `recompute`，输出含 `ttft_ms`，该 JSON 被后续模式通过 `--recompute-baseline-json` 引用。
- **判定边界**：本地端点、模型、请求负载和设备必须与后续模式一致；如果只得到单次样本，不能直接声称已获得稳定 P99 基线。

### 步骤 4：运行原生 Mooncake 复用路径

- **操作意图**：在固定的原生代码包和实际端点上，验证 R1 写入/就绪后 R2 复用路径的 TTFT，并与步骤 3 的同场次基线对账。
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

- **应观察现象**：R1 完成后出现真实就绪事件，R2 产生首字事件；输出含 `actual_path`、`ttft_ms`、`recompute_ttft_ms` 和 `net_saved_ms`。
- **证据边界**：`--protocol urma` 只是命令行字段，只有服务端代码包和设备完成事件证明实际路径时才可形成 URMA 结论。

### 步骤 5：在相同条件下切换单项增强与完整增强

- **操作意图**：把原生 Mooncake、单项增强和完整增强放在同一模型、同一 workload、同一请求顺序和同一硬件拓扑下对照，避免把多项变量变化误判为 UBMEM 收益。
- **执行动作**：分别以 `unified_single` 和 `unified_full` 运行步骤 4 的命令模板，只替换真实端点、`package_id`、`config_hash`、`actual_path` 和结果目录；不得只修改 `--mode`。
- **应观察现象**：四种模式都能回指独立代码包或配置；R1/R2 的 `workload_id`、模型布局、就绪事件和 `run_id` 关系完整。
- **停止条件**：如果目标端点、真实 UBMEM 驱动或实际完成路径不存在，把该模式记为 `NOT-SUPPORTED`；不得用 W0 的本地 `memcpy` 结果填充 UBMEM 行。

### 步骤 6：可选运行 W0 端到端部署和通用压测解析

- **操作意图**：验证项目已有 W0 部署脚本、benchmark_serving 输出和公共汇总解析器可以工作，但把它与 PVT-00 的 R1/R2 配对证据分开保存。
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

- **应观察现象**：部署脚本只在 W0 localhost 继续，结果目录下有 warmup、测量 JSON 和 `summary.csv`；缺少字段时解析器返回无效证据状态。
- **判定边界**：`run_online_benchmark.sh` 使用 `benchmark_serving` 的 ShareGPT 输入，不能替代 PVT-00 的 R1/R2 同场次重算配对；W0 结果只能标 `DEMO`。

### 步骤 7：保存原始数据、重复实验和证据包

- **操作意图**：把终端演示转成可复核结果，保留失败请求和未采集字段，防止汇总表掩盖证据缺口。
- **执行动作**：按 `results/PVT-00/<mode>/<run_id>/` 建立目录，保存 `manifest.json`、`environment.json`、原始 stdout、原始 CSV/JSON、逐请求事件、汇总 CSV/JSON 和日志；每个条件至少完成 3 次独立重复。
- **应观察现象**：汇总中的每个 TTFT、净收益和协议指标都能回指原始样本；`planned_path`、`actual_path`、`evidence_level`、`status` 和 `invalid_reason` 齐全。
- **字段规则**：没有采集到的字段写 `null` 并说明原因；不能用 `0` 表示没有丢包、没有同步开销或零时延。

---

## 6. 数据采集清单与记录格式

### 6.1 PVT-00 原始事件字段

公共事件字段之外，本项至少记录：

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

字段约束：

- `ready_event_ns` 只有真实就绪事件或等价消费凭证存在时填写；DEMO 固定 sleep 不得伪装成真实事件；
- `actual_path` 必须来自端点、设备完成事件或可审计日志，不能根据 `mode` 推断；
- `T_net_saved` 只有同场次重算基线和复用路径 TTFT 都有效时填写；
- 协议完成、DMA、目录查询、挂接和 NPU 事件当前脚本不能采集时使用 `null`，并写 `invalid_reason`；
- `status` 使用 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE`；脚本内部的 `OK` 只表示命令返回，不表示验证通过。

### 6.2 协议微基准汇总 CSV 模板

以下是字段模板，不是性能成绩：

```csv
validation_id,run_id,protocol,payload_bytes,concurrency,direction,sample_count,bandwidth_gbps,latency_avg_us,latency_p50_us,latency_p99_us,actual_path,package_id,config_hash,hardware_profile,topology_profile,evidence_environment,evidence_level,status,invalid_reason
<PVT-00>,<run_id>,<urma_or_ubmem>,<bytes>,<threads>,<write_or_read>,<count>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<actual_path_or_null>,<package_id>,<config_hash>,<hardware_profile>,<topology_profile>,<W0_OR_W1_OR_W2>,<DEMO_OR_LAB_OR_MEASURED>,<status>,<null_or_reason>
```

当前 `proto_bench.cc` 产生的行必须额外保留 `status=NOT-SUPPORTED` 或 `CONDITIONAL` 的说明，不能只因 CSV 有数值就写成 URMA/UBMEM `MEASURED`。

### 6.3 端到端对账 CSV 模板

```csv
validation_id,run_id,workload_id,model_id,model_type,kv_bytes_per_token,prefix_tokens,unique_tokens,total_r2_tokens,reuse_ratio,mode,protocol,planned_path,actual_path,package_id,baseline_commit,config_hash,hardware_profile,topology_profile,evidence_environment,evidence_level,sample_count,ttft_p50_ms,ttft_p95_ms,ttft_p99_ms,recompute_ttft_p50_ms,recompute_ttft_p95_ms,recompute_ttft_p99_ms,dir_query_p99_ms,data_load_p99_ms,attach_sync_p99_ms,net_saved_p50_ms,net_saved_p99_ms,ubmem_speedup,status,invalid_reason
<PVT-00>,<run_id>,<workload_id>,<model_id>,<mha_or_mla>,<runtime_value>,<prefix>,<unique>,<total>,<ratio>,<recompute_or_native_or_unified_single_or_unified_full>,<protocol>,<planned_path>,<actual_path>,<package_id>,<baseline_commit>,<config_hash>,<hardware_profile>,<topology_profile>,<W0_OR_W1_OR_W2>,<DEMO_OR_LAB_OR_MEASURED>,<count>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<status>,<null_or_reason>
```

`traffic_generator.py` 当前只写单条记录，正式多请求汇总需要保留逐请求源数据后再聚合；不得把一条 `ttft_ms` 复制到 P50/P95/P99 三列。

### 6.4 证据包目录

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

`manifest.json` 至少记录代码包、基线 Commit、配置哈希、输入 Schema、模型布局、Token 数、协议和模式、设备与拓扑、执行命令、原始文件哈希、证据等级、支持范围、未支持项、门槛和结论状态。

---

## 7. GO / CONDITIONAL / NO-GO / NOT-SUPPORTED / INVALID-EVIDENCE 判定规则

### 7.1 命题一：协议加速对照

- **GO（真实协议路径满足候选门槛）**：URMA 与 UBMEM 都有真实驱动完成事件、相同 payload/并发/方向/设备/拓扑和至少 3 次独立重复；在声明的测试区间内，候选 UBMEM 总路径相对 URMA 的加速比达到运行前冻结的 `>= 1.30x`，且错误、失败和尾部时延没有不可接受退化。该结论只对记录的代码包、设备和拓扑负责。
- **CONDITIONAL（局部支持）**：只形成 W1/LAB 的局部协议观测，或只有部分方向/并发/设备完成量；结论限定为已测条件，不外推到全部协议路径。
- **NOT-SUPPORTED**：当前仍是本地 `memcpy` 桩、目标驱动不可用或无法证明 `actual_path`；该状态不等于协议性能失败。
- **NO-GO（真实路径没有净收益）**：在有效 A/B 中 UBMEM 未达到门槛，或引入加速路径后错误率、失败率或 P99 明显恶化。

### 7.2 命题二：Saved-Prefill 业务净收益

- **GO（同场次收益闭环）**：在至少 50% 复用率和声明的模型/上下文条件下，至少 3 次独立重复；R1 就绪、R2 实际路径、同场次本地重算和关键版本字段齐全；`T_net_saved > 0` 且达到运行前冻结的 `T_net_saved >= 2.0 × (目录查询 + 数据加载 + 校验/挂接/同步)` 候选门槛。
- **CONDITIONAL（场景受限）**：只有 70% 以上高复用率、较长上下文或特定模型架构满足净收益，结论限定为该场景白名单；如果只有单次 TTFT 或 W0/DEMO 结果，也只能保留局部流程结论。
- **NO-GO（业务收益为负）**：在有效的声明测试范围内，加载、校验和挂接总开销持续大于本地直接重算耗时，且通过合理的路径和负载复核仍无法得到净收益。
- **NOT-SUPPORTED**：没有可消费的就绪事件、运行时布局、真实端点或同场次重算配对，无法形成对应业务结论。

### 7.3 统一无效证据规则

以下任一情况将对应子实验标为 `INVALID-EVIDENCE`，不得输出 `GO`：

- 用 `--protocol` 或 `--mode` 标签代替真实驱动、代码包或实际路径切换；
- 把本地 `memcpy`、固定 sleep、随机 Token ID 或单次 HTTP TTFT 写成真实协议、缓存就绪或业务分位数；
- 缺少同场次重算基线，或用另一场次平均值替代当前 R2；
- `planned_path` 与 `actual_path` 无法证明，或缺少模型布局、Tokenizer、设备、拓扑、配置哈希；
- 原始样本、失败请求、重复轮次、版本清单或 manifest 缺失；
- 字段缺失却用 0 填充，或把脚本返回 `OK` 当成性能通过；
- A/B 改变了设备、负载、资源配额、请求顺序、预热或统计口径；
- 把 `INVALID_EVIDENCE`、`BASELINE_INVALID` 等脚本内部状态未经解释直接写成 `GO`。

---

## 8. 执行阶段与交付闭环

版本一中的“Day 1~Day 3”信息保留为三个实施阶段；阶段名称用于组织工作，不把日期当作性能承诺。

| 阶段 | 工作内容 | 必须交付 | 退出条件 |
|---|---|---|---|
| 阶段 A：工具审计与 W0 基线 | 审计四个脚本的真实行为，完成本地协议桩、workload JSON、R1/R2 HTTP 闭环和字段校验 | 源码审计记录、命令日志、workload manifest、`DEMO` 证据包 | 命令可复现，已列明本地桩、固定 sleep 和未支持项 |
| 阶段 B：协议与业务局部实测 | 接入真实驱动或真实模型端点，扫描 payload/并发、五档复用率、MLA/MHA 和四种模式 | 原始事件、失败请求、TTFT 分位数、净收益、协议计数器、重复实验汇总 | 每个指标可回指原始样本，证据等级和实际路径明确 |
| 阶段 C：标准证据包与决策 | 对账同场次重算、原生 Mooncake、单项增强和完整增强，输出分项判定 | `manifest.json`、`environment.json`、协议表、业务表、摘要和未支持说明 | 通过公共契约校验，无模拟数值冒充真实结果 |

本项的最终作用是确认 Saved-Prefill 在什么复用率、模型布局和链路条件下有净收益，并量化协议路径是否值得进入后续 QueryPlan（查询与放置计划决策引擎，即按链路状态、算力和上下文长度选择加载或重算路径）设计。它不能用一张本地复制 CSV 代替真实协议结论，也不能把一次命中直接等同于生产收益。

---

## 9. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师负责**：
  1. 确认被测模型、Tokenizer、设备、节点角色、协议驱动、代码包、拓扑和证据等级；
  2. 冻结复用率、Token 数、并发、预热、重复轮次、A/B 门槛和结果目录；
  3. 执行 R1/R2 和本地重算，保存原始请求、失败请求、就绪事件和设备凭证；
  4. 判断某字段是否确实由设备、协议或推理引擎观测到，决定填写数值、`null`、`NOT-SUPPORTED` 还是 `INVALID-EVIDENCE`；
  5. 对现场模型和硬件结论进行复核。
- **AI Agent 负责**：
  1. 先阅读当前方案、公共契约和实际源码，列出真实 CLI、依赖、输出字段和未实现功能；
  2. 编写日志解析、逐请求统计、同场次基线配对、原始文件哈希和证据包生成工具；
  3. 检查复用率公式、TTFT 分位数、净收益和四模式 A/B 的字段完整性；
  4. 不凭空创建 URMA/UBMEM 驱动调用、真实模型成绩或未存在的端点，不把脚本返回 `OK` 改写成准入通过。

### 9.2 可直接复制给 Coding Agent 的 Prompt 模板

```text
我正在执行 PVT-00：业务流量 Saved-Prefill 收益上限与通信协议加速评估。

请先阅读：
1. ./提前验证方案设计/验证计划方案设计/01_PVT-00_业务流量Saved-Prefill收益上限评估实施方案设计.md
2. ./提前验证方案设计/验证计划方案设计/Benchmark公共契约与证据分级规范.md
3. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-00/Makefile
4. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-00/proto_bench.cc
5. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-00/make_workload.py
6. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-00/traffic_generator.py

约束：
- 先列出源码实际支持的 CLI、依赖、输出字段和路径；确认 proto_bench.cc 当前只做本地 memcpy，不能把 --protocol urma/ubmem 当成真实驱动切换。
- 确认 make_workload.py 只生成随机 Token ID 合成输入；正式 LAB/MEASURED 必须检查 model_layout_manifest 和 kv_bytes_per_token。
- 将没有真实驱动、真实就绪事件、同场次重算基线或实际路径凭证的结果标为 DEMO、NOT-SUPPORTED 或 INVALID-EVIDENCE；不要用固定值补齐。
- 为 30%、50%、70%、90%、98% 五档复用率生成 workload，并复算 prefix_tokens/(prefix_tokens+unique_tokens)。
- 对每条 R2 保留同场次 recompute、mooncake_native、unified_single、unified_full 的 workload_id、package_id、config_hash、planned_path、actual_path 和 TTFT。
- 缺失字段使用 null 并填写 invalid_reason；保留失败请求；不要把一条 TTFT 复制成 P50/P95/P99。
- 最后输出：源码能力矩阵、实际运行命令、字段字典、统计复算结果、证据等级、未支持项、无效证据项和下一步最小代码改动建议。
```

### 9.3 常见排错指南

- **找不到 `liburma.so` 或 `libubmem.so`**：当前 `Makefile` 本来就没有链接这两个库，`proto_bench` 能编译不代表驱动可用；先把本地桩结果标为 `DEMO`，不要通过改标签形成协议成绩。
- **协议加速比看起来很大或很小**：检查两次运行是否只是 URMA 分支忙等循环差异；在真实 SDK 接入前，任何比值都只能作为测试桩现象。
- **LAB/MEASURED 运行提示缺少 `kv_bytes_per_token`**：为 `make_workload.py` 提供包含该字段的运行时布局 manifest；不能把 `35KB/tok` 或 `320KB/tok` 纸面估算写入正式字段。
- **`ready-url` 或 `ready-file` 超时**：检查 R1 是否真的写入目标服务、就绪事件是否对应当前 `workload_id` 和 `run_id`；不能用固定 sleep 替代 LAB/MEASURED 事件。
- **`net_saved_ms` 为 `null`**：检查是否提供了 `--recompute-baseline-json`，以及该文件是否包含同场次的 `ttft_ms`；没有基线时只能保留时延记录，不能关闭收益命题。
- **`Connection refused` 或首字事件为空**：检查端点、`/v1/completions` 路由、模型服务健康状态和 SSE 输出；服务返回成功但没有首个有效流式事件时，应记录失败原因。
- **通用 `parse_benchmark_metrics.py` 返回 `INVALID_EVIDENCE`**：先检查 benchmark JSON 是否包含所需 QPS、TTFT、TPOT 和背景带宽字段；该解析器不负责 R1/R2 配对，不能用它绕过 PVT-00 的同场次基线要求。
- **复用率与文件名不一致**：以 workload JSON 中的 `metadata.reuse_ratio`、Token 数和随机种子为准；重新生成时更新 `workload_id`、配置哈希和 manifest。
- **想把单次 HTTP 结果写成 P99**：增加真实请求样本和独立重复，从原始事件计算分位数；样本不足时填写 `null` 和 `invalid_reason`。
