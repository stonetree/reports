# PVT-04：QueryPlan 微秒级动态决策引擎与 CostEvaluator 验证实施方案设计
## —— 以实时链路状态拦截负收益：加载、重算与后续多介质路径的动态选择

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个样本必须同时记录输入遥测、模型布局、预测路径、实际执行或反事实来源、预测耗时、实际耗时、最优路径、后悔值和证据状态；只有预测逻辑没有实际路径证据时，只能形成 `DEMO` 决策流程结论。

> **验证范围声明**：当前受控工程中的 `QueryPlanFastPath` 只实现 `Remote_Load` 与 `Recompute` 两个动作；`CostEvaluator` 只使用输入的远端 EWMA 带宽、队列时延、KV 字节数、元数据时延和重算吞吐计算两个预测值；`query_plan_bench.cc` 单线程逐条读取场景 CSV，并把输入的 `actual_load_ms`/`actual_recompute_ms` 转换成 oracle 最优路径，没有真实执行加载或重算。工程中不存在 `make_scenarios.py`、`verify_negative_profit.py`、`eval_accuracy.py`，也没有 `--threads` 或 100K QPS 压测实现。因此，当前代码只能验证规则和计算字段，不能单独证明微秒级生产决策、MAPE、在线准确率或负收益发生率。

> **术语速查**：QueryPlan（查询与放置计划决策引擎，即在请求进入时根据链路、算力、缓存和 Deadline 选择加载或重算路径）；CostEvaluator（成本预估模型，即把 payload、带宽、队列和算力转换为候选路径耗时）；Telemetry（运行时遥测，即设备、网络和队列的实时状态）；EWMA（Exponentially Weighted Moving Average，指数加权移动平均，用于平滑带宽/时延观测）；FastPath（快速决策路径，即不做动态内存分配和阻塞 I/O 的短代码路径）；Regret（决策后悔值，即选中路径耗时与可选路径最短实测耗时的差值）；MAPE（Mean Absolute Percentage Error，平均绝对百分比误差）；Deadline（请求可接受的最晚完成时间）。

> **验证 ID**：PVT-04
> **验证名称**：QueryPlan 微秒级动态决策引擎与 CostEvaluator 成本预估模型验证
> **验证优先级**：**🔴 P0 级（核心关键项）**
> **对应验证阶段**：**E2（动态调度决策与分层扩容）**
> **证伪标记**：否（动态选路能力确认）
> **建议周期**：4~6 人日
> **主关联 IR**：`IR-01-03`, `IR-01-05`
> **核心 SRS / SR23 锚点**：
> - SRS：`L1-RT-Admission-007`, `L3-SE-QueryPlanFastPath-072`, `L3-TRANS-TOPO-SENSE-004`, `L4-FABRIC-ROUTER-001`
> - SR23：`SR23-01-03-01`, `SR23-01-05-01`, `SR23-01-09-01`, `SR23-02-02-01`, `SR23-02-02-02`
> **配套源码**：[`提前验证方案设计/验证计划方案设计/原型验证代码/PVT-04/`](file:///d:/codes/reports/kvcache/unified_kv_memory/提前验证方案设计/验证计划方案设计/原型验证代码/PVT-04)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`；vLLM `842dd8fd96650063e1ad32e6075742d457d39773`。正式结果必须绑定实际代码包、遥测采集版本、硬件和配置哈希。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统系统视角：数据库 CBO 为什么会选择全表扫描

关系型数据库的 CBO（Cost-Based Optimizer，基于代价的优化器）在执行查询前，会比较索引查找与全表扫描的预计成本。数据量小、命中行比例高或随机 I/O 较慢时，走索引不一定更快；系统需要使用当前统计信息，而不是看到“有索引”就固定选择索引。

KVCache 路径选择也是同一类问题：

```text
远端加载：目录查询 + 排队 + 数据加载 + 校验/挂接 + 同步
本地重算：未命中/完整 Prefill 的计算时间
本地 SSD：目录查询 + SSD 读取 + 设备写入 + 校验/挂接
```

QueryPlan 的职责是把这些候选路径转换成可比较的预计完成时间，并主动放弃没有净收益的加载。它不是“命中缓存就加载”的开关。

### 0.2 大模型推理中的负收益陷阱

KVCache（大模型注意力键值缓存，即自回归生成过程中保存历史 Key 和 Value 激活状态、避免后续 Token 重复计算注意力）命中后，仍然可能因为以下因素变慢：

- 前缀很短，目录查询和固定提交开销占主导；
- 链路拥塞，EWMA 带宽下降、排队时延上升；
- 远端数据量大，网络加载和本地挂接超过 NPU 重算；
- Deadline 很紧，加载虽然平均更快但无法按时完成；
- 能力矩阵过期或实际路径与计划路径不一致。

所以系统需要比较：

```text
如果加载与挂接总开销 < 本地直接重算耗时，且预计完成时间不超过 Deadline：执行加载
否则：主动回退至本地重算
```

上面的逻辑必须用实际路径和反事实对账验证，不能用一组纸面参数宣布决策准确。

### 0.3 为什么决策路径要短且无阻塞

QueryPlan 位于请求准入和数据路径选择的前面。若每次决策都执行动态分配、锁竞争、文件读取、RPC 或复杂序列化，决策本身会成为前台请求的额外排队。

工程上通常将遥测采样与在线决策分离：后台线程周期性更新快照，前台 FastPath 只读取冻结快照并做算术比较。`alignas(64)`（按 64 字节缓存行对齐）可以作为降低并发读写伪共享的实现手段，但是否带来收益必须有基线、并发和 CPU 时间线证据。

### 0.4 当前配套工程能够证明什么，不能证明什么

| 子实验 | 当前源码能够完成的动作 | 当前源码不能直接证明的内容 | 当前默认证据 |
|---|---|---|---|
| CostEvaluator | 根据 CSV 的带宽、队列、KV 字节数和算力计算加载/重算预测 | 真实链路遥测、目录/设备事件、预测误差和在线成本 | `DEMO / W0` |
| QueryPlan | 在无远端缓存、加载不划算或 Deadline 分支时选择 `RECOMPUTE`，否则可能选择 `REMOTE_LOAD` | 本地 HBM、SSD、Direct-View、Copy-to-HBM 等完整候选路径 | `DEMO / W0` |
| 决策耗时 | 用单调时钟包住一次 `generate_plan`，输出 `decision_ns` | 稳定 P99、并发吞吐、时钟开销扣除和真实 100K QPS | `DEMO` |
| 准确率 | 将输入的 `actual_load_ms`/`actual_recompute_ms` 计算为 oracle 最优路径 | 实际执行路径、同场次反事实实验和业务负收益率 | `DEMO`；实际成本来源完善后才可 LAB |

---

## 1. 验证目标与交付结论定义

### 1.1 待验证核心命题

1. **命题一：决策器能否拦截加载负收益**。在网络拥塞、短前缀、队列延迟和紧迫 Deadline 条件下，QueryPlan 是否选择预计更快且能按时完成的路径；
2. **命题二：成本模型是否足够准确**。基于 `model_layout_manifest`、实时带宽/队列和重算吞吐的预测值，与同场次真实或明确现场回放值的 MAPE 是否在运行前冻结的门槛内；
3. **命题三：决策是否足够快**。在真实快照读取、并发请求和编译优化条件下，单次 FastPath 的 P50/P95/P99 是否满足候选微秒门槛；
4. **命题四：决策选择是否接近实际最优路径**。在所有具备资格的候选路径中，选择路径的 Regret 是否受控，且不能因追求平均时延而恶化前台尾部、失败率或一致性。

### 1.2 交付物与结论边界

每个正式 `run_id` 至少交付：

1. 《QueryPlan 决策时延分位数表》：包含计时方法、时钟开销、并发、绑核、编译参数和原始样本；
2. 《CostEvaluator 预测与实际/反事实成本对账表》：每个字段均能回指同场次路径事件或明确的现场回放来源；
3. 《拥塞、短前缀和 Deadline 条件下负收益拦截表》：保留失败请求和实际加载/重算成本；
4. 《路径选择准确率与 Regret 表》：区分“oracle 最优路径”和“实际执行路径”；
5. `manifest.json`、`environment.json`、遥测快照、场景输入、原始输出、日志、摘要及分项状态。

---

## 2. 当前数据结构、成本模型与决策边界

### 2.1 当前 AccessIntent 与 ExecutionPlan

当前 `query_plan_fastpath.h` 的输入和输出为：

```cpp
enum class PlanAction { Remote_Load, Recompute };

struct AccessIntent {
    uint64_t request_id;
    uint32_t prefix_tokens;
    uint32_t deadline_ms;
    double current_ewma_bw_gbps;
    double queue_delay_ms;
    bool is_remote_cached;
    uint64_t kv_bytes_per_token;
    double compute_tokens_per_second;
    double metadata_overhead_ms;
};

struct ExecutionPlan {
    PlanAction action;
    double predicted_load_ms;
    double predicted_recompute_ms;
    double predicted_selected_ms;
    std::string reason;
};
```

当前没有 `is_cached_locally`、`is_cached_ssd`、优先级、租约、实际路径、能力矩阵版本或前台/后台类别字段。因此，不能把现有二分决策器描述成已经覆盖 Local HBM、Local SSD、Direct-View、Copy-to-HBM 的全局选择器。

### 2.2 当前成本模型

当前加载预测为：

$$
S=prefix\_tokens\times kv\_bytes\_per\_token
$$

$$
T_{load}=metadata\_overhead+queue\_delay+\frac{S}{BW_{ewma}}
$$

当前重算预测为：

$$
T_{recompute}=\frac{prefix\_tokens}{compute\_tokens\_per\_second}\times 1000
$$

实现中的带宽单位换算是 `Gbps → bytes/ms`，并用 `max(bytes_per_ms, 1.0)` 防止除零。正式扩展仍需补充：目录查询分项、DMA/设备完成、校验/挂接/同步、路径固定开销、尾部置信区间、能力矩阵有效期和不同数据介质成本。

### 2.3 当前决策逻辑与已知边界

当前 `generate_plan` 的实际顺序是：

1. `is_remote_cached=false` 时选择 `RECOMPUTE`；
2. 远端加载预测大于或等于重算预测时选择 `RECOMPUTE`，理由为 `NEGATIVE_BENEFIT_OR_DEADLINE`；
3. 当加载预测超过 Deadline 且重算预测不超过 Deadline 时选择 `RECOMPUTE`；
4. 其他情况选择 `REMOTE_LOAD`，理由为 `LOAD_PREDICTED_FASTER`。

这段实现没有实际加载/重算动作，也没有对 `deadline_ms` 同时超过的两条路径做明确“均超时”状态；当两条路径都超过 Deadline 时，可能仍按加载较快分支返回 `REMOTE_LOAD`。正式实现应增加 `DEADLINE_MISSED`、`NO_ELIGIBLE_PATH` 或等价状态，不能把过期请求继续当作按时完成。

### 2.4 目标多路径决策模型

后续完整方案可扩展为：

```text
本地 HBM 已有可消费副本 → Local_HBM_Attach
远端缓存可用且加载总成本低于重算 → Remote_Load / Direct-View / Copy-to-HBM
本地 SSD 可用且成本低于重算 → Local_SSD_Restore
所有加载路径无净收益或不满足 Deadline → Recompute
```

每个候选路径必须带 `eligible`、预计完成时间、实际路径、能力矩阵版本、租约/一致性状态和 `decision_reason`。若路径不可用，不应把无限大预测值、缺失字段或默认带宽当作真实成本。

---

## 3. 实验方案与测试矩阵设计

### 3.1 决策场景矩阵

| 场景 | 输入变化 | 重点观测 | 当前覆盖 |
|---|---|---|---|
| 正常网络/中等前缀 | 带宽稳定、队列低、前缀中等 | 加载与重算预测接近时的路径选择 | 支持规则演示 |
| 拥塞网络 | EWMA 带宽下降、队列延迟上升 | 是否主动回退重算、Regret 是否降低 | 可通过 CSV 输入演示，无真实遥测 |
| 短前缀 | 128、256、512 Token 等 | 固定目录/队列开销是否超过重算 | 可通过 CSV 输入演示 |
| 长上下文 | 32K、64K、128K Token 等 | KV 字节数和带宽对加载预测的影响 | `prefix_tokens` 可输入 |
| Deadline 紧迫 | 10、20、50ms 等 | 是否选择可按时完成路径 | 支持基础分支，但无实际完成时间 |
| 无远端缓存 | `is_remote_cached=0` | 是否强制本地重算 | 支持 |
| 多介质候选 | 本地 HBM、远端、SSD、重算 | 全局路径最优和资格过滤 | 当前不支持，需扩展数据结构 |

### 3.2 参数与统计矩阵

| 维度 | 正式计划 | 当前源码支持情况 |
|---|---|---|
| 样本数 | 10K~100K 场景，运行前冻结 | `query_plan_bench` 读取输入 CSV 行数，无自动生成器 |
| 并发 | 1、4、16、32 线程或现场请求并发 | 当前单线程顺序处理，无 `--threads` |
| 决策计时 | P50/P95/P99/P99.9，扣除或估算时钟开销 | 每行输出一次 `decision_ns`，没有分位数汇总 |
| 远端带宽 | 低、中、高和突发变化 | 由 CSV `bw_gbps` 提供，不采集真实遥测 |
| 队列时延 | 0、1、5、15ms 等冻结档位 | 由 CSV `queue_ms` 提供 |
| KV 字节数 | 来自运行时布局 manifest | CSV 可输入 `kv_bytes_per_token`，不校验 manifest |
| 重算吞吐 | 来自同场次 NPU/模型事件 | CSV 可输入 `compute_tps`，不执行重算 |

### 3.3 环境与证据矩阵

| 环境 | 目的 | 最低条件 | 允许形成的结论 |
|---|---|---|---|
| W0 输入回放 | 验证成本公式、决策分支、CSV 和字段 | C++ 编译器、手工/外部生成 CSV | `DEMO` 逻辑结论 |
| W1 遥测回放/局部设备 | 用现场快照和实际成本校准预测 | 能力矩阵、模型布局、部分设备事件 | 绑定条件的 `LAB` 结论 |
| W2 在线端到端 | 验证实际路径、准确率、Regret 和负收益拦截 | 真实请求、真实加载/重算、遥测、重复实验 | 证据闭环后形成 `MEASURED` 结论 |

### 3.4 公平对照和 oracle 边界

`predicted_*` 是决策器输入快照计算的预测值；`actual_*` 必须来自实际执行或明确的现场回放；`oracle_path` 是在所有候选实际成本已知时的反事实最优，不等于被测系统实际执行的路径。报告必须分开写：

```text
决策路径：QueryPlan 实际返回什么
执行路径：请求实际加载/重算了什么
oracle 路径：在实际成本已知时哪条路径最短
```

如果只有 oracle 输入，没有执行事件，只能计算“规则对给定标签的选择一致性”，不能称为在线决策准确率。

---

## 4. 测试工具、当前实现边界与正式扩展要求

### 4.1 当前受控工程目录与运行方式

```text
原型验证代码/PVT-04/
├── Makefile
├── query_plan_fastpath.h
├── query_plan_fastpath.cc
└── query_plan_bench.cc
```

当前 `Makefile` 只编译上述两个 C++ 源文件。可复现的 W0 命令为：

```bash
cd ./原型验证代码/PVT-04
make clean
make
./query_plan_bench --scenario-csv scenarios_demo.csv \
    --evidence-level DEMO --out query_plan_results_demo.csv
```

当前场景 CSV 必须包含 11 列：

```csv
request_id,prefix_tokens,deadline_ms,bw_gbps,queue_ms,is_cached,kv_bytes_per_token,compute_tps,metadata_ms,actual_load_ms,actual_recompute_ms
1,50000,100,80,1,1,327680,8500,0.08,120,400
```

示例行仅说明输入格式，不能作为测试成绩。当前 `--evidence-level` 接受任意字符串，外部证据审查层必须重新校验。

### 4.2 源码实际行为审计

| 代码路径 | 实际行为 | 对证据的影响 |
|---|---|---|
| `query_plan_fastpath.cc::estimate_load_cost` | `prefix_tokens × kv_bytes_per_token`，再加元数据和队列时延，并按当前 EWMA 带宽换算 | 只验证算术模型；没有目录、DMA、挂接和设备事件 |
| `estimate_recompute_cost` | `prefix_tokens / compute_tokens_per_second × 1000` | 只使用输入吞吐，不执行 NPU 重算 |
| `generate_plan` | 只在 `REMOTE_LOAD` 与 `RECOMPUTE` 间选择；无 HBM/SSD/租约/优先级路径 | 不能声明完整全局选路 |
| Deadline 分支 | 只特别处理“加载超时、重算未超时”；两者都超时没有独立状态 | 需要增加无可行路径或超时状态 |
| `query_plan_bench.cc::load_scenarios` | 要求恰好 11 列，直接 `stod/stoull`，不校验 header、字段范围或文件是否成功打开 | 非法输入可能异常退出；结果不含输入 schema 校验 |
| `query_plan_bench.cc` oracle | 用 `actual_load_ms < actual_recompute_ms` 计算 `optimal`；没有把 `is_remote_cached=0` 纳入 oracle 资格过滤 | 输出的 `actual_path` 实际是 oracle 选择，不是服务实际执行路径 |
| `query_plan_bench.cc` 计时 | 单线程包住一次 `generate_plan`，写出 `decision_ns` | 没有并发、批量、P99 汇总或时钟开销校准 |
| 输出 | CSV 只有 `evidence_level`，没有 `status`、`invalid_reason`、`package_id`、`actual_path` 语义字段 | 需要外部归一化和 manifest 才能进入正式证据包 |

### 4.3 面向 LAB/MEASURED 的最小工程扩展

1. **真实遥测快照**：后台采集链路带宽、队列、设备利用率、SSD 和显存水位，记录采样时间、有效期和来源；
2. **完整候选路径**：扩展 HBM、远端 Direct/Copy、SSD 和重算的资格、成本、实际路径和失败状态；
3. **模型布局绑定**：读取 `model_layout_manifest`、Tokenizer 哈希、TP 切分和 `kv_bytes_per_token`，禁止按模型名称硬编码；
4. **真实成本校准**：以同场次实际加载、挂接、同步、重算和失败事件校准预测模型，保留反事实或配对关系；
5. **多线程 FastPath**：在真实并发下验证无锁/低锁快照读取、缓存行布局、分配和日志行为，测量 P50/P95/P99；
6. **超时与无可行路径**：增加 `DEADLINE_MISSED`、`NO_ELIGIBLE_PATH`、租约失效、能力矩阵过期等明确状态；
7. **决策/执行/Oracle 分离**：结果字段分别记录 `planned_path`、`actual_path`、`oracle_path`、`actual_cost` 和 `regret`；
8. **状态与证据包**：把内部 `OK`、解析异常和 DEMO 回放归一化为公共契约状态，缺失字段使用 `null` 和 `invalid_reason`。

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结实验身份、输入来源和判定门槛

- **操作意图**：先区分 W0 输入回放、W1 局部实测和 W2 在线实测，防止把 oracle CSV 当作真实执行证据。
- **执行动作**：填写 `run_id`、`workload_schema_version`、`workload_id`、`package_id`、`baseline_commit`、`config_hash`、`model_layout_manifest`、`hardware_profile`、`topology_profile`、`evidence_level`、并发、样本量、遥测有效期、候选门槛和路径资格。
- **应观察现象**：能明确列出 `predicted`、`actual` 和 `oracle` 三类字段的来源；没有实际加载/重算事件时提前标为 `DEMO`。

### 步骤 1：准备最小场景 CSV

- **操作意图**：验证当前 benchmark 的 11 列输入格式和规则分支，构造正常、拥塞、短前缀、Deadline 和无缓存场景；当前工程没有 `make_scenarios.py`，需要人工或外部工具生成。
- **执行动作**：准备 `scenarios_demo.csv`，包含严格 11 列和真实/回放来源说明；每个场景记录 `actual_load_ms` 与 `actual_recompute_ms` 的来源，不复制预测值。
- **应观察现象**：场景能被解析；`is_cached=0`、加载较慢、加载超过 Deadline 等分支均有覆盖；输入字段和单位在 manifest 中固定。

### 步骤 2：编译并运行当前单线程决策 benchmark

- **操作意图**：验证 QueryPlan 的当前二分规则、CostEvaluator 算式和 `decision_ns` 字段输出。
- **执行命令**：

```bash
cd ./原型验证代码/PVT-04
make clean
make
./query_plan_bench --scenario-csv scenarios_demo.csv \
    --evidence-level DEMO --out query_plan_results_demo.csv \
    > query_plan_stdout.txt 2>&1
```

- **应观察现象**：输出每个请求的预测加载/重算成本、oracle 路径、Regret、选择一致性和单次 `decision_ns`。
- **判定边界**：这是输入回放/规则 DEMO；输出的 `actual_path` 不是实际服务执行路径，不能直接计算在线准确率或负收益发生率。

### 步骤 3：复算预测成本、oracle 和 Regret

- **操作意图**：独立检查 C++ 输出是否遵循单位换算、缓存资格和最优路径定义，避免 benchmark 自己生成的字段无人复核。
- **执行动作**：由外部解析器读取原始场景和输出，复算 `T_load`、`T_recompute`、候选资格、oracle、selected cost 和 Regret；发现缺字段、负数或单位不一致时标 `INVALID-EVIDENCE`。
- **应观察现象**：预测值能回指输入字段；oracle 路径明确标为 `oracle_path`，不再沿用含义不清的 `actual_path`。

### 步骤 4：校准真实/回放成本（条件步骤）

- **前置条件**：具备 PVT-00/PVT-01/PVT-03/PVT-05 的真实路径事件、模型布局、能力矩阵和同场次重算数据。
- **操作意图**：把纸面 `bw_gbps`、`queue_ms` 和 `compute_tps` 换成有时间戳、版本和有效期的现场输入，测量预测误差和选择后悔值。
- **执行动作**：记录请求快照、能力矩阵版本、实际加载/重算路径、完成时间、失败、超时、租约和资源水位；为每个预测样本保留实际或反事实配对键。
- **应观察现象**：`predicted_cost_ms` 与 `actual_cost_ms` 能逐样本对账；若只有预测无实际事件，状态仍为 `DEMO`。

### 步骤 5：验证拥塞和短前缀下的负收益拦截

- **操作意图**：确认链路变差或固定成本占比上升时，系统会主动回退重算，而不是因为缓存命中继续加载。
- **执行动作**：在相同模型、请求和代码包下改变可观测网络/队列条件或使用明确现场回放；分别记录 QueryPlan 选择、实际执行路径、实际成本、Deadline 状态和失败请求。
- **应观察现象**：当实际加载成本高于重算时，`planned_path` 与 `actual_path` 均应记录回退；若只改变 CSV 标签而没有真实路径变化，结果无效。

### 步骤 6：验证决策时延与并发稳定性（条件步骤）

- **前置条件**：实现多线程/批量压测、快照读取、绑核和原始计时输出；明确是否扣除时钟调用开销。
- **操作意图**：在目标请求并发下验证决策自身不会抬高前台尾部时延。
- **执行动作**：运行 1、4、16、32 线程或现场等价并发，保留每次 `decision_start_ns`/`decision_end_ns`、线程、CPU、快照版本和异常；从原始样本计算分位数。
- **应观察现象**：P99/P99.9 可回指原始计时；当前单线程 benchmark 不具备本步骤能力，未扩展前标 `NOT-SUPPORTED`。

### 步骤 7：生成证据包并做重复实验对账

- **操作意图**：把公式回放、成本校准、负收益拦截和决策时延分开归档。
- **执行动作**：按 `results/PVT-04/<subtest>/<run_id>/` 建目录，保存输入 CSV、遥测快照、能力矩阵、原始输出、逐请求实际事件、摘要、版本和日志；每个条件至少 3 次独立重复。
- **应观察现象**：`planned_path`、`actual_path`、`oracle_path`、`evidence_level`、`status` 和 `invalid_reason` 齐全，缺失字段为 `null`。

---

## 6. 数据采集清单与记录格式

### 6.1 决策原始字段

```text
run_id, validation_id, trace_id, request_id, event_name,
workload_schema_version, workload_id, model_id, model_layout_manifest,
tokenizer_hash, prefix_tokens, deadline_ms, kv_bytes_per_token,
is_remote_cached, candidate_paths, current_ewma_bw_gbps, queue_delay_ms,
compute_tokens_per_second, metadata_overhead_ms,
planned_path, actual_path, oracle_path, decision_reason,
predicted_load_ms, predicted_recompute_ms, predicted_selected_ms,
actual_load_ms, actual_recompute_ms, actual_selected_ms, regret_ms,
decision_start_ns, decision_end_ns, decision_ns,
telemetry_sample_ns, telemetry_age_ms, capability_matrix_version,
package_id, baseline_commit, config_hash, hardware_profile, topology_profile,
evidence_environment, evidence_level, status, error_code, invalid_reason
```

字段约束：

- `actual_path` 必须来自真实执行或明确回放，`oracle_path` 才是反事实最优；
- `actual_load_ms`/`actual_recompute_ms` 缺失时，不能计算在线准确率、MAPE 或 Regret；
- `telemetry_age_ms` 超过运行前冻结的有效期时，路径资格需要重新判定；
- 当前源码没有的候选路径和事件字段使用 `null`，并填写 `invalid_reason`；
- `status` 使用 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE`。

### 6.2 场景输入与结果 CSV 模板

```csv
validation_id,run_id,request_id,prefix_tokens,deadline_ms,bw_gbps,queue_ms,is_remote_cached,kv_bytes_per_token,compute_tps,metadata_ms,planned_path,actual_path,oracle_path,predicted_load_ms,predicted_recompute_ms,actual_load_ms,actual_recompute_ms,regret_ms,decision_ns,evidence_level,status,invalid_reason
<PVT-04>,<run_id>,<request_id>,<prefix>,<deadline>,<bw>,<queue>,<0_or_1>,<runtime_value>,<tps>,<metadata>,<planned_or_null>,<actual_or_null>,<oracle_or_null>,<predicted_or_null>,<predicted_or_null>,<actual_or_null>,<actual_or_null>,<calculated_or_null>,<measured_or_null>,<DEMO_OR_LAB_OR_MEASURED>,<status>,<null_or_reason>
```

### 6.3 统计口径

```text
MAPE = mean(abs(predicted_cost - actual_cost) / actual_cost) × 100%
决策准确率 = planned_path 与 oracle_path 一致的有效样本数 / 有效样本数
Regret = actual_cost(planned_path) - min(actual_cost(eligible_paths))
负收益发生率 = actual_load_ms > actual_recompute_ms 的有效远端缓存样本数 / 有效远端缓存样本数
```

`actual_cost` 必须来自同一设备、拓扑、负载、代码包和工作负载的实际执行或明确现场回放。预测值不能复制到实际值字段；分母为 0、缺少资格字段或路径不完整时，统计值写 `null`。

### 6.4 证据包目录

```text
results/PVT-04/<subtest>/<run_id>/
├── manifest.json
├── environment.json
├── scenarios.csv
├── telemetry_snapshots.jsonl
├── capability_matrix.json
├── raw_decision_events.jsonl
├── raw_execution_events.jsonl
├── summary.json
├── summary.csv
└── logs/
```

`manifest.json` 至少记录代码包、基线 Commit、配置哈希、模型布局、遥测和能力矩阵版本/有效期、执行命令、原始文件哈希、证据等级、候选门槛和状态。

---

## 7. GO / CONDITIONAL / NO-GO / NOT-SUPPORTED / INVALID-EVIDENCE 判定规则

### 7.1 成本模型与路径选择

- **GO（真实成本和路径闭环）**：模型布局、遥测、实际加载/重算事件、预测值、oracle、Regret 和重复实验完整；在运行前冻结的范围内，MAPE、选择准确率和负收益拦截率达到候选门槛，且没有前台尾部或失败率不可接受退化。
- **CONDITIONAL（场景受限）**：只在特定前缀长度、带宽区间、模型或缓存状态下满足门槛；结论限定为这些条件。
- **NO-GO（动态选择没有净收益）**：有效 A/B 中加载负收益没有被拦截，Regret 持续偏高，或选路带来更高的 TTFT、TPOT、失败率或资源占用。
- **NOT-SUPPORTED**：当前只有输入回放/公式，缺少真实候选路径、实际成本或有效遥测。

### 7.2 决策 FastPath 时延

- **GO（微秒级时延闭环）**：在真实目标并发、真实快照读取和明确计时口径下，P99 达到运行前冻结的 `<5µs` 候选门槛，且不依赖阻塞 I/O、动态分配或未记录的后台工作。
- **CONDITIONAL（局部性能）**：单线程或局部并发达到门槛，但尚未覆盖生产并发、CPU 绑核或遥测更新争用。
- **NO-GO（决策成为瓶颈）**：有效测量 P99 超出门槛，或决策抖动明显干扰前台请求。
- **NOT-SUPPORTED**：当前 benchmark 无并发和分位数汇总，只输出单次 `decision_ns`。

### 7.3 统一无效证据规则

以下任一情况将对应子实验标为 `INVALID-EVIDENCE`，不得输出 `GO`：

- 把 `query_plan_bench` 输入的 `actual_*` 字段当作真实执行，而没有来源和现场回放说明；
- 将 oracle `actual_path` 误写成服务真实路径，或缺少 `planned_path`/`actual_path` 区分；
- 用预测值复制填充实际成本、MAPE、Regret 或负收益分母；
- 缺少模型布局、遥测采样时间/有效期、能力矩阵、设备、拓扑、版本或代码包；
- 场景 CSV 列数、单位、范围或缓存资格未校验；
- 使用不存在的 100K QPS、`--threads`、场景生成器或分析脚本产出结果；
- 缺失字段用 0 或默认带宽补齐，或把 `DEMO` 回放改写为 LAB/MEASURED。

---

## 8. 执行阶段与交付闭环

| 阶段 | 工作内容 | 必须交付 | 退出条件 |
|---|---|---|---|
| 阶段 A：规则与输入回放 | 审计当前二分决策、成本公式、11 列 CSV 和 oracle 边界 | 源码审计、场景 CSV、规则输出、`DEMO` manifest | 预测/实际/oracle 语义分开 |
| 阶段 B：遥测与实际成本校准 | 接入能力矩阵、模型布局、真实加载/重算事件和失败状态 | 逐请求成本、遥测快照、MAPE、Regret、重复汇总 | 每个成本字段可回指事件 |
| 阶段 C：并发 FastPath 与在线门禁 | 验证并发决策时延、负收益拦截和前台影响 | P99、准确率、拦截率、前台尾部和分项结论 | 公共契约通过，无 oracle 冒充实际执行 |

本项的最终作用是让系统在微秒级控制面内选择有净收益且能按时完成的路径，并在链路或算力条件变差时主动回退重算。它不能用单线程公式回放替代真实在线选路，也不能把“预测正确”当作“请求实际按该路径完成”。

---

## 9. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师负责**：
  1. 确认模型布局、能力矩阵、遥测来源、候选路径、设备、拓扑和有效期；
  2. 冻结场景、并发、计时口径、A/B、门槛和实际成本来源；
  3. 执行或授权真实加载/重算回放，保存失败、超时、路径和资源事件；
  4. 复核 `planned_path`、`actual_path`、`oracle_path` 的语义和证据等级；
  5. 对是否允许 QueryPlan 影响前台请求承担现场复核责任。
- **AI Agent 负责**：
  1. 先阅读方案和四个实际源码文件，列出真实 CLI、结构体、分支和输出字段；
  2. 编写场景校验、成本复算、分位数、MAPE、Regret、状态归一化和证据包工具；
  3. 检查单位换算、Deadline 分支、缓存资格、oracle 与实际执行区分；
  4. 不凭空创建 100K QPS、并发脚本、遥测数据、实际成本或准入结论。

### 9.2 可直接复制给 Coding Agent 的 Prompt 模板

```text
我正在执行 PVT-04：QueryPlan 微秒级动态决策引擎与 CostEvaluator 验证。

请先阅读：
1. ./提前验证方案设计/验证计划方案设计/05_PVT-04_QueryPlan微秒级动态决策引擎与CostEvaluator验证实施方案设计.md
2. ./提前验证方案设计/验证计划方案设计/Benchmark公共契约与证据分级规范.md
3. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-04/Makefile
4. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-04/query_plan_fastpath.h
5. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-04/query_plan_fastpath.cc
6. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-04/query_plan_bench.cc

约束：
- 先列出真实结构体、CLI、输入 11 列和输出字段；确认当前只有 Remote_Load/Recompute，query_plan_bench 单线程逐条回放，不存在 --threads、make_scenarios.py、verify_negative_profit.py、eval_accuracy.py。
- 区分 predicted_path、planned_path、actual_path 和 oracle_path；query_plan_bench 当前的 actual_path 实际是由输入 actual_load_ms/actual_recompute_ms 算出的 oracle 标签，不是实际执行路径。
- 校验 CostEvaluator 的单位换算、缓存资格、Deadline 分支和 kv_bytes_per_token 来源；不要把模型名称或纸面带宽写成真实遥测。
- 没有真实加载/重算事件、能力矩阵、遥测时间戳和重复样本时，只能输出 DEMO/NOT-SUPPORTED/INVALID-EVIDENCE，不能计算在线准确率、MAPE 或负收益发生率。
- 缺失字段使用 null 并填写 invalid_reason；保留非法输入和失败请求；不要用预测值复制到 actual_*。
- 最后输出：源码能力矩阵、实际命令、场景校验结果、预测/实际/oracle 对账、未支持项和下一步最小代码改动建议。
```

### 9.3 常见排错指南

- **场景 CSV 报 11 列错误**：按源码要求保留 `request_id` 到 `actual_recompute_ms` 的 11 列；不要把方案中扩展字段直接塞进当前 benchmark，扩展后同步 schema。
- **决策器选择 `REMOTE_LOAD` 但实际加载更慢**：检查输入 `actual_*` 是否真实、是否考虑路径资格和遥测有效期；当前代码没有实际执行闭环，不能靠修改 reason 掩盖问题。
- **两条路径都超过 Deadline 仍选择加载**：这是当前分支边界；增加无可行路径/超时状态并写入 `invalid_reason`，不要把结果直接判为按时完成。
- **`decision_ns` 很小却无法达到生产 P99**：当前是单次包围时钟测量，未扣除时钟调用、未覆盖并发和遥测更新；先补原始计时和多线程压测。
- **MAPE 或准确率异常高**：检查 `actual_*` 是否被复制自预测值、分母是否接近 0、单位是否混用、oracle 是否过滤了不可用缓存路径。
- **想运行 `make_scenarios.py` 或 `eval_accuracy.py`**：当前目录没有这些文件；先用严格 11 列 CSV 和外部解析器完成 DEMO，不能引用不存在的执行结果。
- **模型 KV 字节数不可信**：从运行时 `model_layout_manifest` 读取并记录 Tokenizer、TP、数据类型和布局；不能按模型名称硬编码。
- **遥测更新导致尾部抖动**：记录快照版本、采样时间和线程/CPU；检查前台是否读到部分更新，必要时使用不可变快照或版本校验。
- **能力矩阵过期**：将路径标为不可用或重新采集；不能继续使用旧带宽和队列时延关闭当前运行结论。
