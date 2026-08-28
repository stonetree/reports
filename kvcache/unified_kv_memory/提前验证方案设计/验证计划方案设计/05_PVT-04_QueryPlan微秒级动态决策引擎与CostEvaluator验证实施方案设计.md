# PVT-04：QueryPlan 微秒级动态决策引擎与 CostEvaluator 验证实施方案设计
## —— 以实时链路状态拦截负收益：加载、重算与多介质分层存储路径的动态选择

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个测试样本需同时完整记录输入遥测快照、模型物理布局、预测路径、实际执行路径（或经审计的现场回放来源）、预测耗时、实际耗时、反事实 Oracle 最优路径、决策后悔值 (Regret) 及证据状态枚举；在仅具备算法预测逻辑而缺乏底层实际数据路径执行凭证的前提下，测试结论统一限定标记为 `DEMO` 决策流程验证。

> **验证范围声明**：在当前受控的原型验证工程中，`QueryPlanFastPath` 仅实现了 `Remote_Load` 与 `Recompute` 两个二分决策动作；`CostEvaluator` 仅使用外部输入的远端 EWMA 带宽、队列排队时延、KV 字节大小、元数据时延及本地重算吞吐进行纯数学公式预测；`query_plan_bench.cc` 采用单线程逐行回放场景 CSV 文件，并直接依据输入文件中的 `actual_load_ms` 与 `actual_recompute_ms` 字段计算 Oracle 反事实最优路径，程序本身并未实际执行任何底层的数据加载或 NPU 算子重算。工程中目前尚未包含 `make_scenarios.py`、`verify_negative_profit.py` 或 `eval_accuracy.py` 等外部脚本，亦未实现 `--threads` 多线程并发或 100K QPS 高吞吐压测能力。因此，现有受控源码仅用于验证选路分支规则与数学计算字段，不可直接作为证明微秒级生产决策耗时、预测模型 MAPE 误差、在线决策准确率或真实负收益拦截率的依据。

> **术语速查**：
> - **QueryPlan**：查询与放置计划决策引擎（在请求进入系统时，依据实时链路状态、算力负载、缓存分布及业务 Deadline，在微秒级时间内选择最优数据加载或重算路径的调度引擎）；
> - **CostEvaluator**：成本预估模型（将 Payload 尺寸、网络带宽、排队时延及算力吞吐量化转换为候选路径预期耗时的数学模型）；
> - **Telemetry**：运行时遥测（底层硬件设备利用率、网络拥塞状态及队列深度的实时监控数据流）；
> - **EWMA**：Exponentially Weighted Moving Average（指数加权移动平均：用于平滑网络带宽与时延短期波动的高灵敏度时序滤波算法）；
> - **FastPath**：快速决策路径（消除动态内存分配、互斥锁争用与阻塞 I/O 的高性能执行通路）；
> - **Regret**：决策后悔值（选定执行路径的实际耗时与全量候选路径中最优实测耗时的绝对差值）；
> - **MAPE**：Mean Absolute Percentage Error（平均绝对百分比误差：衡量成本预估模型预测值与物理实测值偏差程度的统计指标）；
> - **Deadline**：业务超时时限（单次推理请求为满足服务 SLO 所允许的最晚首字生成完成时间）。

> **验证 ID**：PVT-04
> **验证名称**：QueryPlan 微秒级动态决策引擎与 CostEvaluator 成本预估模型验证
> **验证优先级**：**🔴 P0 级（核心关键项）**
> **对应验证阶段**：**E2（动态调度决策与分层扩容）**
> **证伪标记**：否（动态选路与负收益拦截能力确认）
> **主关联 IR**：`IR-01-03`, `IR-01-05`
> **核心 SRS / SR23 锚点**：
> - SRS：`L1-RT-Admission-007`, `L3-SE-QueryPlanFastPath-072`, `L3-TRANS-TOPO-SENSE-004`, `L4-FABRIC-ROUTER-001`
> - SR23：`SR23-01-03-01`, `SR23-01-05-01`, `SR23-01-09-01`, `SR23-02-02-01`, `SR23-02-02-02`
> **配套源码**：[`./原型验证代码/PVT-04/`](./原型验证代码/PVT-04/)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`；vLLM `842dd8fd96650063e1ad32e6075742d457d39773`。正式测试结果需绑定实际代码包版本、遥测采集环境、硬件拓扑及配置哈希。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统系统视角：基于代价的查询优化

在现代关系型数据库的 CBO（基于代价的查询优化器）中，系统在执行 SQL 查询前会量化比较索引查找与全表扫描的预估代价。当查询涉及的数据量极大或返回行比例过高时，强制走索引不仅无法加速，反而会因大量离散随机读而慢于全表扫描。因此，系统需基于当前统计信息做出动态代价决断。

大模型 KVCache 的路径调度遵循相同的原理：

```text
远端加载路径 : 目录查询开销 + 硬件队列排队 + 网络数据拉取 + 6 维语义校验/显存挂接 + 跨流同步
本地重算路径 : 依据本地 NPU 算力对未命中/全量 Prompt 直接执行 Prefill 前向计算的时间
本地 SSD 恢复: 目录查询 + 磁盘 NVMe 读取 + NPU 显存写入 + 语义校验/显存挂接
```

QueryPlan 决策引擎的核心职责，是在微秒级时间内量化预估各候选路径的端到端耗时，仅在数据拉取与加载总开销小于本地直接重算耗时且能满足业务时限时才执行加载，否则主动回退至本地重算。

### 0.2 大模型推理中的动态选路考量

在大模型推理实践中，KVCache 在以下典型场景下容易出现数据加载耗时超过本地直接重算的情况：
- **极短 Prompt 前缀**：前缀仅有数百 Token，网络传输数据量虽小，但目录元数据查询、跨进程 IPC 及硬件队列提交的固定开销占据主导；
- **网络 Incast 突发拥塞**：跨节点物理链路拥塞，EWMA 测得的有效带宽骤降、排队时延上升；
- **长上下文传输瓶颈**：远端数据量巨大（如数万 Token），跨节点网络拉取与本地显存挂接的耗时超过了本地多卡张量并行 (TP) 下的 NPU 算力重算耗时；
- **业务 Deadline 紧迫**：加载的平均时延虽短，但尾部波动导致无法在业务规定的 Deadline 内按时完成；
- **硬件能力矩阵失效**：底层链路物理参数过期，或实际执行路径与预期规划路径发生退化。

因此，系统建立严格的前置动态决策准则：

```text
当且仅当：拉取与加载总开销 < 本地直接重算耗时，且预计完成时间不超过业务 Deadline：执行数据加载
否则：主动回退至本地直接重算
```

### 0.3 为什么决策路径要短且无阻塞

QueryPlan 位于大模型在线请求准入与数据路径分发的最前端。若每次决策都执行动态内存分配、互斥锁竞争、磁盘文件读取或跨网络 RPC，决策开销本身就会成为前台在线请求的排队瓶颈，导致首字生成延迟 (TTFT) 恶化。

在系统工程实现中，采用“控制面异步遥测采集与数据面无锁决策”解耦架构：
- **后台线程**：以毫秒级周期异步探测物理链路状态，维护无锁的运行时遥测快照；
- **前台 FastPath**：读取冻结的不可变快照，执行寄存器算术比较，在微秒级时间内完成路径决断；
- **内存对齐优化**：采用 `alignas(64)`（按 64 字节缓存行对齐）消除并发读写时的 CPU 缓存伪共享。

### 0.4 当前配套工程能够证明什么，不能证明什么

| 验证子项 | 当前受控源码能够完成的执行动作 | 当前受控源码尚不能证明的内容 | 默认证据等级 |
|---|---|---|---|
| CostEvaluator 成本预估 | 根据 CSV 传入的带宽、排队时延、KV 字节数及算力参数计算加载与重算的公式预测耗时 | 真实的物理链路遥测采样、目录查询/硬件完成事件、生产环境预测误差分布及在线动态成本 | `DEMO / W0` |
| QueryPlan 二分选路 | 在未命中远端缓存、加载耗时过长或预估超时分支时输出 `RECOMPUTE`，否则输出 `REMOTE_LOAD` | 本地 HBM 复用、NVMe SSD 恢复、Direct-View 远端直读、Copy-to-HBM 等全量多介质候选路径 | `DEMO / W0` |
| 单次决策耗时测量 | 调用单调时钟统计单次 `generate_plan` 的执行时间，输出 `decision_ns` | 生产级 P99 尾部时延、高并发吞吐能力、时钟调用固有开销扣除以及 100K QPS 饱和压测表现 | `DEMO` |
| 决策准确率与 Regret | 依据输入的 `actual_load_ms` 与 `actual_recompute_ms` 字段数值计算 Oracle 反事实最优路径 | 真实业务环境下的实际执行路径、同场次真实反事实对照实验以及端到端负收益实际拦截率 | `DEMO`（需完善实际成本来源后方可转为 LAB） |

---

## 1. 验证目标与交付结论定义

### 1.1 待验证核心命题

1. **命题一：决策引擎能够在恶劣工况下精准拦截数据加载的负收益**。在网络突发拥塞、极短前缀、高排队延迟及紧迫 Deadline 等严苛工况下，QueryPlan 能够准确识别风险并主动回退至更稳定、可按时达标的本地重算路径；
2. **命题二：CostEvaluator 成本预估模型具备高精度的预测能力**。基于 `model_layout_manifest`、实时 EWMA 带宽、队列深度及本地算力吞吐推导的预测耗时，与同场次物理实测耗时之间的平均绝对百分比误差 (MAPE) 满足准入门槛；
3. **命题三：FastPath 决策耗时达到微秒级性能**。在读取不可变快照与现代编译优化条件下，单次 FastPath 决策耗时的 P50/P95/P99 稳定满足候选门限（如 P99 $< 5\mu\text{s}$）；
4. **命题四：选路决策逼近全局最优路径**。在所有具备消费资格的候选介质路径中，QueryPlan 选定路径的决策后悔值 (Regret) 受到收敛，且选路优化不破坏前台 TPOT 尾部稳定性。

### 1.2 交付物与结论边界

每个正式 `run_id` 交付：
1. **《QueryPlan 决策耗时分位数与并发稳定性报告》**：记录计时采样方法、时钟开销扣除说明、线程并发度、CPU 绑核策略及原始样本数据；
2. **《CostEvaluator 预测与物理实测成本对账表》**：记录预测值、物理实测值、误差分布及 MAPE 统计；
3. **《恶劣工况下负收益拦截与 SLO 达标分析表》**：记录拥塞、短前缀及紧迫 Deadline 场景下的决策路径流转、失败拦截率及实际业务收益；
4. **《路径选择准确率与决策后悔值 (Regret) 统计表》**：区分“Oracle 反事实最优路径”与“服务实际执行路径”，输出量化对账结论；
5. **标准证据包**：包含 `manifest.json`、`environment.json`、遥测快照历史、场景输入集、原始输出 CSV、系统日志及分项技术判定结论 (`GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE`)。

---

## 2. 当前数据结构、成本模型与决策边界

### 2.1 当前 AccessIntent 与 ExecutionPlan 结构体

当前受控工程 `query_plan_fastpath.h` 中定义的输入与输出结构体如下：

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

说明：当前受控源码中现有的二分决策器覆盖了远端加载与本地重算的基础选择，后续扩展支持本地 HBM 复用与 NVMe SSD 恢复等多介质路径。

### 2.2 当前成本模型与计算公式

远端加载路径的预测模型：

$$
S = prefix\_tokens \times kv\_bytes\_per\_token
$$

$$
T_{load} = metadata\_overhead + queue\_delay + \frac{S}{BW_{ewma}}
$$

本地直接重算路径的预测模型：

$$
T_{recompute} = \frac{prefix\_tokens}{compute\_tokens\_per\_second} \times 1000
$$

源码实现中将网络带宽单位从 $\text{Gbps}$ 换算为 $\text{Bytes/ms}$，并采用 $\max(bytes\_per\_ms, 1.0)$ 进行除零保护。

### 2.3 当前决策逻辑与已知边界分析

当前受控工程 `generate_plan` 函数的实际执行决策树：
1. 当 `is_remote_cached == false` 时，选择 `RECOMPUTE` 路径；
2. 当远端加载预测耗时大于或等于本地重算预测耗时（$T_{load} \ge T_{recompute}$）时，主动选择 `RECOMPUTE` 路径，决策归因为 `NEGATIVE_BENEFIT_OR_DEADLINE`；
3. 当远端加载预测耗时超过业务 Deadline 且本地重算预测耗时未超过 Deadline 时，选择 `RECOMPUTE` 路径；
4. 其余正常情况下选择 `REMOTE_LOAD` 路径，决策归因为 `LOAD_PREDICTED_FASTER`。

工程边界与审计事实：
- 属于规则推导，未实际发起底层数据拉取或算子重算；
- 当远端加载与本地重算预测耗时同时超出业务 Deadline 时，需补充 `DEADLINE_MISSED` 或 `NO_ELIGIBLE_PATH` 显式异常枚举。

### 2.4 目标多路径决策扩展模型

面向多介质分层存储池的完整 QueryPlan 决策拓扑设计：

```text
1. 本地 HBM 显存池已存在可用且校验通过的副本 ──► 选择 Local_HBM_Attach（零拷贝快路径）
2. 远端节点存在有效缓存且综合拉取成本低于重算 ────► 动态选择 Direct-View 远端直读 或 Copy-to-HBM 本地拷贝
3. 本地 NVMe SSD 存在缓存且读取挂接成本低于重算 ──► 选择 Local_SSD_Restore（Bypass DDR 直达恢复）
4. 全量加载路径均无净收益或均无法满足 Deadline ──► 主动回退至 Recompute（本地算力直接重算）
```

每个候选路径条目携带 `eligible` 资格标志、预估完成时间戳、实际物理路径、能力矩阵版本及 `decision_reason`。

---

## 3. 实验方案与测试矩阵设计

### 3.1 决策场景矩阵

| 评测场景分类 | 关键输入参数扰动特征 | 重点观测指标与物理行为 | 当前源码覆盖状态 |
|---|---|---|---|
| 正常网络 / 中等前缀 | 网络带宽平稳、排队时延低、Prompt 前缀适中 | 观测加载与重算预测成本接近时的平滑选路行为 | 支持基础规则演示 |
| 突发拥塞网络 | EWMA 带宽骤降、硬件队列排队时延攀升 | 验证决策器是否感知拥塞并主动回退至本地重算，抑制后悔值 | 支持通过 CSV 输入进行离线演示 |
| 极短前缀场景 | Prompt 长度为 128、256、512 Token 等极小区间 | 验证固定元数据查询与提交开销是否触发负收益拦截 | 支持通过 CSV 场景输入演示 |
| 超长上下文场景 | Prompt 长度为 32K、64K、128K Token 等超长区间 | 检验 KV 数据体积与物理带宽对加载预测时延的放大效应 | `--prefix-tokens` 支持传入任意参数 |
| 紧迫 Deadline 约束 | 业务超时时限设定为 10ms、20ms、50ms 等档位 | 验证决策器是否过滤无法按时完成的高风险加载路径 | 支持基础分支 |
| 远端未命中场景 | `is_remote_cached = 0` | 验证无缓存状态下的强制重算兜底逻辑 | 完整支持 |

### 3.2 参数与统计矩阵

| 测试维度 | 正式实施计划标准 | 当前工程实际支持情况 |
|---|---|---|
| 测试样本规模 | 10K 至 100K 场景，运行前严格固化 | `query_plan_bench` 按输入 CSV 实际行数回放 |
| 线程并发度 | 1、4、16、32 线程或现场真实并发请求数 | 当前源码支持单线程顺序回放 |
| 决策耗时采样 | 统计 P50/P95/P99/P99.9 耗时，扣除时钟调用开销 | 逐行输出单次 `decision_ns` 原始值 |
| 远端网络带宽 | 低速、中速、高速及突发抖动多档位 | 由 CSV 中 `bw_gbps` 列传入 |
| 队列排队时延 | 0ms、1ms、5ms、15ms 等固化档位 | 由 CSV 中 `queue_ms` 列传入 |
| 单 Token KV 字节 | 依据运行时模型物理布局精准计算 | 由 CSV 中 `kv_bytes_per_token` 列传入 |
| 本地重算吞吐 | 依据同场次 NPU 实测算力填报 | 由 CSV 中 `compute_tps` 列传入 |

### 3.3 环境与证据矩阵

| 运行环境级别 | 验证核心目的 | 最低前置条件 | 允许产出的证据结论 |
|---|---|---|---|
| W0 场景回放 | 验证成本计算公式、选路分支逻辑、CSV 输入输出格式的执行闭环 | C++ 编译器、外部生成的标准化 CSV 场景集 | 仅可产出 `DEMO` 级别的逻辑有效性结论 |
| W1 遥测回放/局部设备 | 采用现场采集的遥测快照与局部硬件实测成本校准预估模型 | 具备硬件能力矩阵、模型布局文件及局部硬件实测事件 | 可产出绑定特定测试条件的 `LAB` 局部结论 |
| W2 在线端到端 | 验证真实推理流量下的实际选路路径、预测准确率、Regret 及负收益拦截效果 | 具备真实在线推理请求、真实数据加载/重算执行、实时遥测及多轮重复实验 | 满足全量证据闭环后，可产出 `MEASURED` 生产级结论 |

### 3.4 公平对照与 Oracle 边界定义

测试报告中清晰分开呈现：
1. **决策路径 (Planned Path)**：QueryPlan 决策引擎实际推荐下发的路径；
2. **执行路径 (Actual Path)**：底层运行时实际执行的数据拉取或算子重算路径；
3. **Oracle 最优路径 (Oracle Path)**：在事后物理成本已知时耗时最短的理想路径。

---

## 4. 测试工具、当前实现边界与正式扩展要求

### 4.1 当前受控工程目录与运行方式

```text
原型验证代码/PVT-04/
├── Makefile
├── query_plan_fastpath.h       # 决策结构体声明与 QueryPlanFastPath 类定义
├── query_plan_fastpath.cc      # 成本预估公式与二分决策核心逻辑实现
└── query_plan_bench.cc         # 单线程回放场景 CSV 并计算决策耗时与 Oracle 的基准程序
```

W0 构建与运行命令：

```bash
cd ./原型验证代码/PVT-04
make clean
make
./query_plan_bench --scenario-csv scenarios_demo.csv \
    --evidence-level DEMO --out query_plan_results_demo.csv
```

输入场景 CSV 11 列格式：

```csv
request_id,prefix_tokens,deadline_ms,bw_gbps,queue_ms,is_cached,kv_bytes_per_token,compute_tps,metadata_ms,actual_load_ms,actual_recompute_ms
1,50000,100,80,1,1,327680,8500,0.08,120,400
```

### 4.2 源码实际行为审计

| 源码文件与核心逻辑 | 源码实际执行行为 | 对实测证据等级的影响分析 |
|---|---|---|
| `estimate_load_cost` 函数 | 计算 `prefix_tokens × kv_bytes_per_token` 并累加元数据及排队时延，按当前 EWMA 带宽换算 | 验证了算术模型的计算正确性；缺乏底层目录查询、DMA 搬运及显存挂接事件 |
| `estimate_recompute_cost` 函数 | 计算 `prefix_tokens / compute_tokens_per_second × 1000` | 使用命令行传入的理论吞吐，未实际调用 NPU 执行 Prefill 重算 |
| `generate_plan` 函数 | 在 `REMOTE_LOAD` 与 `RECOMPUTE` 之间进行二分选择；无 HBM/SSD/租约判断 | 尚未覆盖多介质分层存储的全局选路需求 |
| `load_scenarios` 函数 | 严格要求 11 列并调用 `stod/stoull`，未对字段数值合法范围做防御检查 | 异常输入可能导致程序退出 |
| Oracle 路径计算逻辑 | 依据输入的 `actual_load_ms < actual_recompute_ms` 标记 `optimal` | 输出的 `actual_path` 字段实质为 Oracle 标签，非服务实际执行路径 |
| 决策耗时计时逻辑 | 单线程循环调用单调时钟包围单次 `generate_plan`，直接输出 `decision_ns` | 未覆盖多线程并发、批量处理及系统时钟调用开销校准 |

### 4.3 面向 LAB/MEASURED 的最小工程扩展

正式评估前需补齐以下工程能力：
1. **真实硬件遥测快照接入**：后台异步采集物理链路带宽、网络队列深度、设备利用率及显存水位；
2. **全介质候选路径支持**：扩展支持本地 HBM 复用、远端 Direct-View 直读、Copy-to-HBM 拷贝、本地 SSD 直达恢复及算力重算；
3. **模型物理布局深度绑定**：自动读取 `model_layout_manifest`、Tokenizer 词表哈希、张量并行 (TP) 维度及精确的 `kv_bytes_per_token`；
4. **真实物理成本校准**：以同场次实际发生的数据拉取、显存挂接、跨流同步及算子重算事件校准预估模型；
5. **多线程无锁 FastPath 实现**：在真实高并发请求下验证不可变快照读取、CPU 缓存行对齐 (`alignas(64)`) 及零锁争用表现；
6. **超时与无可行路径处理**：在枚举中增加 `DEADLINE_MISSED`、`NO_ELIGIBLE_PATH` 等状态；
7. **决策/执行/Oracle 严格分离**：在输出字段中分别独立记录 `planned_path`、`actual_path`、`oracle_path`、`actual_cost` 及 `regret`；
8. **规范化状态枚举输出**：统一映射为公共契约规定的标准状态。

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结实验身份、输入来源和判定门槛

- **操作意图**：明确本轮评测的执行级别（W0 场景回放、W1 局部实测或 W2 在线实测），避免将 Oracle 反事实标注误判为真实系统执行证据。
- **执行动作**：在配置清单中填报 `run_id`、`workload_schema_version`、`workload_id`、`package_id`、`baseline_commit`、`config_hash`、`model_layout_manifest`、`hardware_profile`、`topology_profile`、`evidence_level`、并发线程数、场景样本量、遥测有效期及候选准入门槛。

### 步骤 1：准备标准化场景 CSV

- **操作意图**：验证当前基准程序的 11 列输入格式解析与选路分支规则，构造涵盖正常网络、突发拥塞、极短前缀、紧迫 Deadline 及未命中缓存的全量场景集。
- **执行动作**：准备 `scenarios_demo.csv`，严格包含 11 列数据，并详细注明各行中 `actual_load_ms` 与 `actual_recompute_ms` 的物理来源。

### 步骤 2：编译并运行当前单线程决策基准

- **操作意图**：验证 QueryPlan 的二分决策逻辑、CostEvaluator 算式计算及单次 `decision_ns` 字段导出流程。
- **执行命令**：

```bash
cd ./原型验证代码/PVT-04
make clean
make
./query_plan_bench --scenario-csv scenarios_demo.csv \
    --evidence-level DEMO --out query_plan_results_demo.csv \
    > query_plan_stdout.txt 2>&1
```

- **应观察现象**：输出 CSV 记录每个请求的预测加载/重算成本、Oracle 最优路径、决策后悔值 (Regret) 及单次 `decision_ns` 耗时。

### 步骤 3：复算预测成本、Oracle 路径与后悔值

- **操作意图**：由外部独立工具核对 C++ 输出是否严格遵循单位换算、缓存准入资格及最优路径定义。
- **执行动作**：编写解析工具读取原始场景 CSV 与输出结果，独立复算 $T_{load}$、$T_{recompute}$、候选路径资格、Oracle 标签、选定成本及 Regret 数值。

### 步骤 4：校准真实与回放物理成本（条件步骤）

- **前置条件**：具备真实硬件路径事件、模型物理布局、硬件能力矩阵及同场次算子重算实测数据。
- **操作意图**：将纸面静态参数替换为现场实测输入，量化评测成本预估模型的 MAPE 误差分布与选路后悔值。
- **执行动作**：逐样本记录请求快照、硬件能力矩阵版本、实际执行路径、完成时间戳及超时状态。

### 步骤 5：验证恶劣工况下的负收益拦截效果

- **操作意图**：确证在物理链路恶化或固定开销占比过高时，决策引擎能够识别风险并主动回退至本地重算。
- **执行动作**：注入可观测的网络拥塞/排队延迟或采用现场回放，分别记录 QueryPlan 选路决策、实际执行路径、实际耗时及 Deadline 达标状态。

### 步骤 6：验证决策耗时与并发稳定性（条件步骤）

- **前置条件**：实现多线程并发压测工具、不可变遥测快照读取及高精度原始计时输出。
- **操作意图**：在高并发请求压力下验证 FastPath 决策本身不抬高前台在线推理的尾部时延。
- **执行动作**：运行多线程并发压测，基于全量原始样本精确计算 P50/P95/P99/P99.9 分位数。

### 步骤 7：生成标准证据包并完成重复实验对账

- **操作意图**：将公式回放、成本模型校准、负收益拦截及决策耗时测试数据独立归档。
- **执行动作**：在 `results/PVT-04/<subtest>/<run_id>/` 目录下归档输入场景集、遥测快照历史、硬件能力矩阵、原始输出 CSV、逐请求实际事件日志及分析摘要；每个条件至少完成 3 轮独立重复测量。

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

### 6.2 场景输入与结果 CSV 模板

```csv
validation_id,run_id,request_id,prefix_tokens,deadline_ms,bw_gbps,queue_ms,is_remote_cached,kv_bytes_per_token,compute_tps,metadata_ms,planned_path,actual_path,oracle_path,predicted_load_ms,predicted_recompute_ms,actual_load_ms,actual_recompute_ms,regret_ms,decision_ns,evidence_level,status,invalid_reason
<PVT-04>,<run_id>,<request_id>,<prefix>,<deadline>,<bw>,<queue>,<0_or_1>,<runtime_value>,<tps>,<metadata>,<planned_or_null>,<actual_or_null>,<oracle_or_null>,<predicted_or_null>,<predicted_or_null>,<actual_or_null>,<actual_or_null>,<calculated_or_null>,<measured_or_null>,<DEMO_OR_LAB_OR_MEASURED>,<status>,<null_or_reason>
```

### 6.3 核心统计口径与数学公式

$$
\text{MAPE} = \frac{1}{N} \sum_{i=1}^{N} \left| \frac{\text{predicted\_cost}_i - \text{actual\_cost}_i}{\text{actual\_cost}_i} \right| \times 100\%
$$

$$
\text{决策准确率} = \frac{\sum [planned\_path == oracle\_path]}{N_{\text{valid}}} \times 100\%
$$

$$
\text{Regret} = actual\_cost(planned\_path) - \min_{p \in eligible\_paths} \{ actual\_cost(p) \}
$$

$$
\text{负收益发生率} = \frac{\sum [actual\_load\_ms > actual\_recompute\_ms \land is\_remote\_cached == 1]}{N_{\text{valid\_cached}}} \times 100\%
$$

### 6.4 证据包目录结构

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

---

## 7. GO / CONDITIONAL / NO-GO / NOT-SUPPORTED / INVALID-EVIDENCE 判定规则

### 7.1 成本模型与选路决策判定

- **GO（真实物理成本与选路决策闭环）**：模型物理布局、实时遥测快照、真实加载/重算硬件事件、预测值、Oracle 标注及 Regret 完整齐备；MAPE 预测误差、选路准确率及负收益拦截率均达到准入门槛；
- **CONDITIONAL（场景条件受限）**：仅在特定 Prompt 前缀长度、特定网络带宽区间或特定缓存状态下满足门限要求；
- **NO-GO（动态选路未产生净收益）**：数据加载负收益未被有效拦截，选路后悔值 (Regret) 持续偏高，或调度机制引入更高开销；
- **NOT-SUPPORTED（功能未支持）**：缺乏真实候选路径支持、底层实际执行事件或硬件能力矩阵不可用。

### 7.2 FastPath 决策耗时判定

- **GO（微秒级性能闭环）**：在目标请求并发压力、不可变快照读取及明确时钟校准下，单次 FastPath 决策耗时 P99 达到准入门槛（如 P99 $< 5\mu\text{s}$）；
- **CONDITIONAL（局部并发性能达标）**：仅在单线程或低并发条件下满足时延门限；
- **NO-GO（决策耗时超出预期）**：单次决策耗时 P99 超出准入门限；
- **NOT-SUPPORTED（物理环境未支持）**：基准测试程序缺乏多线程并发与分位数统计能力。

### 7.3 统一无效证据规则

凡出现以下任一情形，对应子实验一律判定为 `INVALID-EVIDENCE`：
- 将输入 CSV 中的 `actual_*` 标注字段直接当成真实物理执行证据且缺乏权威来源说明；
- 将 Oracle 理论最优标注与实际执行路径混淆；
- 直接使用预测耗时数值复制填充实际成本、MAPE 误差或 Regret 计算；
- 缺失模型物理布局、遥测采样时间戳与有效期或硬件能力矩阵；
- 关键指标缺失却采用 0 填充。

---

## 8. 执行阶段与交付闭环

测试实施划分为三个演进阶段：

| 实施阶段 | 核心攻坚内容 | 阶段必须交付物 | 准出判定条件 |
|---|---|---|---|
| 阶段 A：规则与输入回放 | 审计二分选路决策逻辑、成本预估算式、11 列场景 CSV 解析及 Oracle 反事实边界 | 源码审计报告、标准化场景集、规则推导结果、`DEMO` 级 manifest | 明确区分预测、实际与 Oracle 三类语义边界 |
| 阶段 B：遥测与实际成本校准 | 接入硬件能力矩阵、模型物理布局、底层真实数据拉取/算子重算事件及失败状态 | 逐请求实测成本表、遥测快照历史、MAPE 误差报告、Regret 统计、多轮重复汇总 | 每个成本字段均能精准追溯至底层硬件事件 |
| 阶段 C：并发 FastPath 与在线门禁 | 攻坚多线程高并发决策时延、恶劣工况负收益拦截及前台在线推理影响评估 | P99 决策时延报告、选路准确率表、负收益拦截率、前台 TPOT 干扰率及分项技术结论 | 全量数据通过公共契约规范核验，杜绝以 Oracle 标注冒充实际执行 |

本验证项的核心价值在于确保大模型推理调度系统在微秒级控制面内，始终选择具备确定性净加速收益且能按时达标的最优路径，并在物理链路或算力条件恶化时主动回退至本地重算。

---

## 9. 工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师核心职责**：
  1. 确认模型物理布局、PVT-01 硬件能力矩阵、运行时遥测数据源、候选介质路径、硬件拓扑及数据有效期；
  2. 固化测试场景集、并发线程数、计时采样口径、A/B 对照策略、准入门槛及实际物理成本来源；
  3. 实际下发或授权执行真实的数据拉取与算子重算，完整留存请求失败、超时、执行路径及系统资源事件；
  4. 严格审定 `planned_path`、`actual_path` 与 `oracle_path` 的语义边界与证据等级真实性；
  5. 对是否允许 QueryPlan 正式介入并驱动前台在线推理流量承担最终技术把关责任。
- **AI Agent 协同职责**：
  1. 研读方案设计、公共测试契约及源码，梳理实际支持的 CLI 参数、结构体定义、决策分支及输出字段；
  2. 编写场景合法性校验、成本模型复算、分位数统计、MAPE 误差分布、Regret 计算、状态归一化及证据包生成工具；
  3. 严格核验量纲单位换算、Deadline 超时分支、缓存准入资格以及 Oracle 标注与实际执行路径的物理隔离；
  4. 严守技术诚信红线，严禁虚构并发压测脚本、物理遥测数据、实际执行成本或准入通过结论。

### 9.2 可直接复制给 Coding Agent 的 Prompt 模板

```text
我正在执行 PVT-04：QueryPlan 微秒级动态决策引擎与 CostEvaluator 验证。

请先研读以下核心文件：
1. ./提前验证方案设计/验证计划方案设计/05_PVT-04_QueryPlan微秒级动态决策引擎与CostEvaluator验证实施方案设计.md
2. ./提前验证方案设计/验证计划方案设计/Benchmark公共契约与证据分级规范.md
3. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-04/Makefile
4. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-04/query_plan_fastpath.h
5. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-04/query_plan_fastpath.cc
6. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-04/query_plan_bench.cc

执行约束与任务要求：
- 首先梳理源码实际支持的结构体、CLI 参数、输入 11 列格式及输出字段；确认当前仅支持 Remote_Load 与 Recompute 二分决策，query_plan_bench 仅为单线程逐行回放，工程中不存在 --threads、make_scenarios.py、verify_negative_profit.py 或 eval_accuracy.py。
- 严格区分 predicted_path、planned_path、actual_path 与 oracle_path；确认 query_plan_bench 当前输出的 actual_path 实质是由输入中 actual_load_ms 与 actual_recompute_ms 推导出的 Oracle 理论标签，非服务真实物理执行路径。
- 校验 CostEvaluator 的量纲单位换算、缓存准入资格过滤、Deadline 超时分支及 kv_bytes_per_token 来源；严禁将模型名称硬编码或纸面静态带宽当作真实遥测。
- 在缺乏真实数据拉取/算子重算事件、硬件能力矩阵、遥测时间戳及多轮重复样本的前提下，测试结果统一限定输出为 DEMO/NOT-SUPPORTED/INVALID-EVIDENCE，严禁计算在线准确率、MAPE 或真实负收益发生率。
- 未采集到的字段显式置为 null 并详细注明 invalid_reason；完整留存非法输入样本与失败请求；严禁用预测值复制填充 actual_* 字段。
- 最终输出：源码能力核验矩阵、实际执行命令清单、场景输入校验结果、预测/实际/Oracle 对账表、未支持特性清单以及下一步最小代码重构建议。
```

### 9.3 常见排错指南

- **场景 CSV 回放报错提示非 11 列格式**：严格按照受控源码要求保留自 `request_id` 至 `actual_recompute_ms` 的 11 列。
- **决策器判定选择 `REMOTE_LOAD` 但物理实测加载更慢**：检查输入的 `actual_*` 字段是否源自真实硬件实测、是否进行了候选路径资格过滤以及遥测数据是否已过有效期。
- **加载与重算两条路径均超出 Deadline 依然判定为加载**：受控源码的已知分支边界；需在重构中增加 `DEADLINE_MISSED` 或无可行路径状态并记录详细原因。
- **单次测得的 `decision_ns` 极小但无法在生产高并发下达标**：当前测试仅为单线程单次时钟包围测量，未扣除时钟调用的固有开销，且未覆盖多线程并发；需先补充全量原始计时与高并发压测。
- **计算得出的 MAPE 误差或选路准确率呈现异常极高值**：排查 `actual_*` 字段是否被错误复制自预测值、量纲单位是否混淆（如 ms 与 s 混用）以及 Oracle 标签是否排除了不可用的缓存路径。
- **尝试运行 `make_scenarios.py` 或 `eval_accuracy.py` 脚本**：当前受控工程目录中不存在上述文件；应采用严格的 11 列 CSV 文件与外部解析脚本完成 DEMO 验证。
- **模型单 Token KV 字节大小不可信**：从运行时的 `model_layout_manifest` 中读取并记录 Tokenizer、TP 维度、数据精度及显存布局；严禁在代码中硬编码固定数值。
- **遥测快照异步更新引发前台决策尾部抖动**：排查前台是否读取到了处于写入中间态的脏快照，必要时引入不可变快照或原子版本校验机制。
- **硬件能力矩阵超出有效期限**：将对应传输路径标记为不可用并触发重新采集；严禁继续使用过期的旧带宽与排队时延数据。
