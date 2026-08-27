# PVT-07：前后台混压端到端最小闭环（Vertical Slice）与 SemanticQoS 干扰控制验证实施方案设计
## —— 全链路前后台混压验证：原生参考实现、统一 KV 方案与 SemanticQoS 对照

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个 `run_id` 必须冻结硬件、拓扑、模型、工作负载、请求速率、后台负载、代码包、配置哈希、QoS 配置、预热、测量窗口和证据等级；结果必须保留前台逐请求 TTFT/TPOT、完成与失败请求、实际 QPS、后台目标/实际带宽、网卡队列计数、CPU/DDR/PCIe 观测、`planned_path`/`actual_path` 和前后台时间线。TTFT/QPS 的收益基线是相同条件下的原生参考实现混压，TPOT 干扰基线是同一增强代码包的纯前台运行，两种基线不能互相替代。

> **验证范围声明**：当前受控工程中的 `mixed_workload_bench.py` 只运行本地 asyncio 协程，前台和后台都是数学/睡眠模拟；`semantic_qos_controller.py` 只提供布尔暂停控制，没有网卡队列、动态预算或 `Worker.step()` 行级回调；`run_mixed_bench.py` 也不能读取当前 DEMO 输出直接形成三组真实对照。因此当前代码只能验证字段、统计公式和控制流程，不能单独证明真实推理服务、RoCE 队列、SSD I/O、SemanticQoS 生效或两节点混压已成立；缺少真实服务事件、设备计数器和原始日志的结果只能标记为 `DEMO`/`LAB`。

> **术语速查**：KVCache（大模型注意力键值缓存，即自回归生成过程中保存历史 Key 和 Value 激活状态、避免后续 Token 重复计算注意力）；Decode（逐 Token 生成阶段，前台 TPOT 主要在此形成）；TTFT（Time To First Token，首字生成延迟）；TPOT（Time Per Output Token，每个输出 Token 的生成耗时）；QoS（Quality of Service，服务质量，即用优先级、带宽预算和退避控制保护前台请求）；SemanticQoS（前后台服务质量保障策略，即让在线 Decode 流和后台 KV 搬运流采用可区分、可观测的调度规则）；RoCE（RDMA over Converged Ethernet，基于以太网的远程直接内存访问）；DSCP（IP 包中的差分服务标记，用于映射网络优先级）；CoS（以太网帧的服务类别标记）；TC0/TC1（硬件流量类别队列，本文约定 TC0 承担前台高优先级流、TC1 承担后台流）；Incast（多对一突发网络拥塞，即多个发送节点同时向一个接收端发送）；Worker.step()（推理 Worker 的一次调度/执行步，目标实现可在此边界通知后台退避）；EWMA（指数加权移动平均，用于平滑观测）；CDF（累计分布函数，用于展示时延分布）；P99（延迟分布中 99% 样本不超过的分位值）；后台目标带宽（实验前设定的发送目标，不等于设备或网络实际带宽）。

> **验证 ID**：PVT-07
> **验证名称**：前后台混压端到端最小闭环（Vertical Slice）与 SemanticQoS 服务质量保障验证
> **验证优先级**：**🔴 P0 级（核心关键项）**
> **对应验证阶段**：**E3（全链路前后台混压总门禁）**
> **证伪标记**：否（全链路系统与服务质量保障确认）
> **建议周期**：7~8 人日
> **主关联 IR**：`IR-01-04`, `IR-01-11`, `IR-02-06`
> **核心 SRS / SR23 锚点**：
> - SRS：`L3-QO-SemanticQoS-045`, `L3-MS-StateAwarePrefetch-081`, `L3-OB-PerPathTelemetry-047`, `L4-FT-PathIntegrityPolicy-077`
> - SR23：`SR23-01-04-01`, `SR23-01-11-02`, `SR23-02-02-01`, `SR23-02-02-02`, `SR23-02-06-01`, `SR23-02-11-01`, `SR23-02-12-03`, `SR23-02-12-05`
> **配套源码**：[`提前验证方案设计/验证计划方案设计/原型验证代码/PVT-07/`](file:///d:/codes/reports/kvcache/unified_kv_memory/提前验证方案设计/验证计划方案设计/原型验证代码/PVT-07)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`；vLLM `842dd8fd96650063e1ad32e6075742d457d39773`。正式结果必须绑定实际服务、网卡/交换机 QoS 配置、驱动/运行时和配置哈希。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 前后台混压的物理干扰链

前台在线 Decode 与后台 KV 搬运可能共同竞争网络出端口、PCIe、设备 DMA 队列、Host DDR 和 CPU 调度。一个可验证的物理链路应写成：

```text
前台请求 -> 推理调度 -> Decode/集合通信 -> 输出 Token
后台 KV 任务 -> 设备/网络 I/O -> 队列排队 -> 设备完成
                              \-> 与前台共享网络、PCIe、显存或 Host DDR
```

QoS 的作用不是让后台任务消失，而是在前台 Decode 的关键窗口限制后台的占用，并让这种限制可以由队列计数、实际带宽和前台逐请求 TPOT 对账。仅在 Python 协程中延长 `sleep`，不能证明硬件队列确实发生了优先级调度。

### 0.2 目标 SemanticQoS 双层机制

目标实现可分为两层：

1. **硬件层**：将前台和后台标记映射到不同队列，例如前台使用高优先级 TC0、后台使用 TC1；具体 DSCP/CoS 数值和无损配置由现场设备确认；
2. **应用层**：在前台 Decode Step 开始时降低或暂停后台提交，在 Step 结束后依据实时观测按受控步长恢复，设置恢复上限和连续超时保护。

```text
前台 step begin
    -> 记录事件和 step_seq
    -> 降低后台提交预算
    -> 前台执行 Decode/集合通信
前台 step end
    -> 记录本步 TPOT
    -> 更新平滑观测和尾部计数
    -> 按规则恢复后台预算，但不超过上限
```

该机制必须与后台 I/O 的实际提交端相连。只改变一个控制器对象里的 `bool`，而后台任务不读取该对象，不能形成控制闭环。

### 0.3 当前受控源码能力矩阵

| 文件 | 当前可确认行为 | 当前不能声称的能力 |
|---|---|---|
| `原型验证代码/PVT-07/mixed_workload_bench.py` | asyncio 并发运行前台/后台协程；前台用指数分布或正态分布生成 TPOT；后台按 64MB 和固定睡眠间隔累计模拟字节；输出 `tpot_*`、`bg_bandwidth_gbps`、`DEMO`、`DEMO_ONLY` | 没有真实推理请求、TTFT、QPS、网络/SSD I/O、Worker.step、硬件队列或两节点通信 |
| `原型验证代码/PVT-07/semantic_qos_controller.py` | `on_foreground_step_begin()` 设置 `bg_throttled=True`；结束时直接恢复为 false；超限分支为 `pass`；`allow_background_transfer()` 只返回布尔值 | 没有 TC0/TC1、DSCP/CoS、带宽预算、EWMA、动态退避、后台提交端或微秒级事件 |
| `原型验证代码/PVT-07/run_mixed_bench.py` | 读取三个 JSON；校验必需字段、A/B 公平性、背景带宽差异；计算 TTFT 降幅、QPS 提升、TPOT 干扰率和门限 | 不生成真实指标；不检查输入状态是否为 `MEASURED`；只拒绝 `evidence_level == DEMO`；异常状态使用脚本内部 `INVALID_EVIDENCE` |
| `原型验证代码/PVT-07` 目录 | 没有独立的前台服务启动器、后台真实 I/O 适配器、网卡配置脚本或结果可视化脚本 | 不能声称已完成端到端最小闭环 |

### 0.4 当前脚本之间不能直接串联

`mixed_workload_bench.py` 的输出只有 `qos_enabled`、前台样本数量、TPOT 分位数、后台模拟带宽、证据级别和 seed；`run_mixed_bench.py` 要求 `run_id`、`package_id`、`config_hash`、硬件/拓扑/工作负载、模型、目标速率、P99 TTFT、P99 TPOT、QPS 和 `bg_bw_gbps` 等字段。当前输出缺少这些字段，因此不能直接作为汇总脚本的三组输入。

此外，`mixed_workload_bench.py` 只有 `--fg-clients`、`--bg-workers`、`--duration`、`--qos`、`--out`、`--seed` 参数，没有文档旧稿中使用的 `--mode`、`--rate`、`--prompts` 或 `--bg-gbps`。当前脚本只能分别运行 QoS 关闭和开启的本地 DEMO，不能表达“纯前台、原生混压、统一 KV 混压”三种工程模式。

## 1. 验证目标与交付物

### 1.1 验证目标

| 目标 | 需回答的问题 | 最低证据 |
|---|---|---|
| 前台基线 | 同一增强代码包无后台时的 TTFT、TPOT、实际 QPS 是多少 | 真实服务请求、逐请求样本和完成/失败统计 |
| 原生混压 | 原生参考实现承受相同后台负载时的 P99 TTFT、P99 TPOT 和 QPS 是多少 | 真实后台提交/完成计数和前台时间线 |
| QoS 混压 | 开启 SemanticQoS 后，后台负载可比时前台指标如何变化 | 队列映射、控制事件、实际后台带宽和前台样本 |
| 控制闭环 | `Worker.step()` 或等价事件是否真正改变后台提交预算 | step 事件、预算变化、后台提交端观测 |
| 可靠性 | 网络拥塞、后台短 I/O、前台突发和控制器恢复是否可处理 | 异常日志、前后台状态、恢复时间和请求完成情况 |

### 1.2 目标门限

在真实两节点、统一请求流和相同后台实际负载条件下，候选 E3 门限为：

- 相对原生参考实现混压，统一 KV 混压的 P99 TTFT 降低至少 20%；
- 相对原生参考实现混压，统一 KV 混压的实际 QPS 提升至少 10%；
- 相对同一增强代码包纯前台，QoS 混压的 P99 TPOT 干扰率小于 3%。

这些是待测门限，不是当前 DEMO 的结果。后台实际带宽偏离 A/B 对照容差时，应先判为无效或条件结果，不能继续套用门限。

### 1.3 交付物

1. 同一增强代码包的纯前台基线、原生参考实现混压、统一 KV QoS 混压三组结果；
2. 每个请求的 TTFT/TPOT、完成状态、错误原因和时间戳；
3. 后台目标/实际带宽、读写比例、队列计数、QoS 标记和控制事件；
4. 三组 A/B 公平性检查、P99/QPS 计算和 CDF；
5. `GO/CONDITIONAL/NO-GO/NOT-SUPPORTED/INVALID-EVIDENCE` 判定及证据缺口。

## 2. 目标控制接口与调度模型

### 2.1 目标流量描述符

当前仓库没有该结构。真实实现前需冻结最小描述符，且明确它只描述控制面，不代表硬件已经支持：

```cpp
enum class TrafficClass : uint8_t {
    FOREGROUND_ONLINE = 0,
    BACKGROUND_TIERING = 1,
};

struct QoSFlowDescriptor {
    uint64_t run_id_hash;
    TrafficClass traffic_class;
    uint32_t dscp;
    uint8_t cos;
    uint32_t queue_id;
    uint64_t target_bandwidth_bytes_per_sec;
    uint64_t current_budget_bytes_per_sec;
    uint64_t in_flight_bytes;
    uint64_t step_seq;
};
```

需要在设备侧确认 `dscp/cos -> queue_id` 的实际映射；在程序侧确认后台 I/O 提交是否读取 `current_budget_bytes_per_sec`。两者任一没有闭合，都只能报告“配置存在”，不能报告“QoS 生效”。

### 2.2 目标控制器状态

```text
NORMAL
  -> FOREGROUND_STEP_ACTIVE
  -> BACKGROUND_BUDGET_REDUCED
  -> FOREGROUND_STEP_DONE
  -> RECOVERING_WITH_CAP
  -> NORMAL
```

控制器需要记录状态进入和退出时间、step 序号、观测到的 TPOT、预算旧值/新值、恢复原因和后台实际提交量。后台任务应能感知预算变化，不能只更新控制器内部字段。

### 2.3 退避与恢复规则

建议先冻结可审计的规则，再通过现场数据调参：

```text
若本次或窗口内 TPOT 超过目标：
    background_budget = max(min_budget, old_budget * backoff_factor)
若连续窗口未超限：
    background_budget = min(max_budget, old_budget + restore_step)
前台 step 活跃期间：
    禁止后台预算超过 foreground_guard_budget
```

`backoff_factor`、`min_budget`、`max_budget`、`restore_step` 和窗口大小必须进入 `config_hash`。当前控制器没有这些字段，不能引用“后台按 10Gbps 线性恢复”作为已有行为。

## 3. 实验矩阵与 A/B 公平性

### 3.1 三组核心条件

| 条件 | 代码包 | 后台负载 | 用途 |
|---|---|---|---|
| `unified_foreground` | 统一 KV 代码包 | 关闭 | TPOT 干扰基线 |
| `mooncake_native_mixed` | 原生参考实现 | 开启 | TTFT/QPS 收益基线 |
| `unified_mixed_qos` | 统一 KV 代码包 | 开启，SemanticQoS | 被测条件 |

三组必须冻结硬件、拓扑、模型、精度、Tokenizer、请求数据、目标速率、运行时长和后台负载。纯前台组和统一混压组必须使用相同 `package_id`，这是 `run_mixed_bench.py` 已有的检查；原生参考实现可以使用不同代码包，但其他公平性字段必须一致。

### 3.2 负载矩阵

第一轮可采用以下候选点，现场资源不足时减少点位必须说明：

| 维度 | 候选值 |
|---|---|
| 前台目标速率 | 5、15、30 req/s；以实际达成速率另行记录 |
| 前台并发 | 1、8、32、64 |
| 后台实际带宽 | 设备可用带宽的 25%、50%、80% |
| 后台并发 worker | 1、4、8 |
| 运行时长 | 预热 30s，稳态 5min 起步 |
| 请求规模 | 固定 Prompt/输出 Token 分布；记录 seed 和数据版本 |
| 后台 I/O | KV 换出、换入、预取分别测量，再测混合比例 |
| QoS | 关闭、硬件队列、应用退避、双层开启 |

当前 DEMO 默认 `fg_clients=32`、`bg_workers=4`、`duration=5`、`seed=42`，这些只是脚本默认值，不是正式 E3 参数。

### 3.3 A/B 校验规则

`run_mixed_bench.py` 当前会检查：

- 三组 `hardware_profile`、`topology_profile`、`workload_id`、`model_id`、`target_rate_rps` 相同；
- 纯前台组与统一混压组 `package_id` 相同；
- 两组混压的 `bg_bw_gbps` 偏差不超过 `--background-tolerance-pct`，默认 5%；
- 输入证据级别不能是字面值 `DEMO`。

正式报告还应补充检查：请求数和输出 Token 分布、后台读写比例、QoS 配置、代码配置哈希、实际运行时长、失败请求比例、时间窗口和 `actual_path`。当前汇总脚本尚未检查这些字段。

## 4. 工具审计、实际命令与最小实现增量

### 4.1 当前 DEMO 的实际命令

当前混流脚本的真实命令为：

```bash
cd ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-07
python3 mixed_workload_bench.py \
  --fg-clients 32 --bg-workers 4 --duration 5 \
  --out mixed_workload_no_qos_demo.json --seed 42

python3 mixed_workload_bench.py \
  --fg-clients 32 --bg-workers 4 --duration 5 --qos \
  --out mixed_workload_qos_demo.json --seed 42
```

应观察两个 JSON 均为 `evidence_level=DEMO`、`status=DEMO_ONLY`，包含 TPOT 模拟样本和后台模拟带宽。第二次运行的 `--qos` 只改变随机样本生成分支，不会配置网卡或调用后台 I/O。

以下旧命令不是当前脚本支持的接口：

```bash
python3 mixed_workload_bench.py --mode unified_kv_mixed_qos --bg-gbps 400 --rate 30 --prompts 1000 --out result.json
```

若执行会因未知参数失败，不能把该命令写成当前可执行 SOP。

### 4.2 当前汇总脚本的实际命令

`run_mixed_bench.py` 的命令行参数为：

```bash
python3 run_mixed_bench.py \
  --unified-foreground <foreground-json> \
  --mooncake-native-mixed <native-mixed-json> \
  --unified-mixed <unified-mixed-json> \
  --background-tolerance-pct 5 \
  --target-rate-rps <optional-rate> \
  --out pvt07_summary.json
```

但当前混流 DEMO 输出缺少汇总脚本的必需字段，不能直接填入上述三个输入。只有在真实适配器产生完整 schema 后，才能使用该脚本做门限计算。

当前汇总脚本在异常时输出 `INVALID_EVIDENCE`，这是脚本内部下划线格式；项目文档和最终报告统一使用 `INVALID-EVIDENCE`，并在适配层完成映射。

### 4.3 最小真实闭环增量

进入 LAB/MEASURED 前至少需要：

1. 增加前台真实推理服务适配器，输出逐请求 TTFT、TPOT、完成状态和实际 QPS；
2. 增加后台真实 KV 搬运适配器，记录提交/完成字节、读写比例、目标/实际带宽和设备错误；
3. 为三组条件生成统一的 `run_id/package_id/config_hash/hardware_profile/topology_profile/workload_id/model_id`；
4. 把 QoS 控制器接到实际后台提交端，补齐预算、退避、恢复和事件记录；
5. 接入真实 `Worker.step()` 或等价调度事件，并记录事件时间与请求 ID；
6. 在网络侧配置并验证 DSCP/CoS 到队列的映射，保存网卡和交换机计数；
7. 扩展汇总脚本检查 `MEASURED`、实际路径、请求完整性、后台读写比和时间窗口；
8. 增加异常注入：Incast、后台短 I/O、前台突发、QoS 控制器暂停、设备错误和恢复。

## 5. 逐步执行 SOP

### Step 0：冻结版本、配置和结果目录

操作意图：让三组结果可在同一源码、配置、拓扑和工作负载下复核。

执行动作：

```bash
run_id="PVT-07-$(date +%Y%m%d-%H%M%S)-mixed"
result_dir="results/pvt07/${run_id}"
mkdir -p "${result_dir}"
git rev-parse HEAD > "${result_dir}/git_commit.txt"
date --iso-8601=ns > "${result_dir}/timestamp.txt"
git status --short > "${result_dir}/git_status.txt"
```

应观察现象：commit、时间戳、工作树状态和配置快照均已保存。

判定边界：三组代码包或配置哈希不可追溯时，整组结果标记 `INVALID-EVIDENCE`。

### Step 1：运行当前本地混流 DEMO

操作意图：确认当前脚本接口和 DEMO 输出字段，建立与真实闭环不同的明确基线。

执行动作：

```bash
cd ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-07
python3 mixed_workload_bench.py \
  --fg-clients 32 --bg-workers 4 --duration 5 \
  --out "../../../../results/pvt07/${run_id}/demo_no_qos.json" --seed 42
python3 mixed_workload_bench.py \
  --fg-clients 32 --bg-workers 4 --duration 5 --qos \
  --out "../../../../results/pvt07/${run_id}/demo_qos.json" --seed 42
```

应观察现象：脚本生成本地 JSON，输出 TPOT 分位数和模拟后台带宽，状态为 `DEMO_ONLY`。

判定边界：本步骤不能提供 TTFT/QPS、网卡队列、真实后台带宽或 SemanticQoS 生效证据；只能验证 DEMO 运行和字段落盘。

### Step 2：确认当前控制器没有形成真实闭环

操作意图：把控制器的可用接口和未实现行为单独取证，避免将方法名当作效果。

执行动作：

```bash
python3 - <<'PY'
from semantic_qos_controller import SemanticQoSController

controller = SemanticQoSController(target_tpot_p99_limit_ms=20.0)
controller.on_foreground_step_begin()
before_end = controller.allow_background_transfer()
controller.on_foreground_step_end(100.0)
after_end = controller.allow_background_transfer()
print({"during_step": before_end, "after_step": after_end})
PY
```

应观察现象：step 开始期间返回不允许后台传输，step 结束后立即恢复；即使传入 100ms，超限分支也没有调整任何预算字段。

判定边界：该步骤只能说明布尔控制器的局部行为；没有后台提交端、事件时间戳和网卡队列计数时，不能判定 QoS 控制成立。

### Step 3：准备真实三组输入 schema

操作意图：在运行汇总脚本前，确保每组 JSON 都具备真实来源和公平性字段。

执行动作：每组输入至少包含：

```json
{
  "run_id": "<run-id>",
  "package_id": "<package>",
  "config_hash": "<hash>",
  "evidence_level": "MEASURED",
  "hardware_profile": "<profile>",
  "topology_profile": "<topology>",
  "workload_id": "<workload>",
  "model_id": "<model>",
  "target_rate_rps": 30.0,
  "p99_ttft_ms": 0.0,
  "p99_tpot_ms": 0.0,
  "qps": 0.0,
  "bg_bw_gbps": 0.0,
  "actual_path": "<observed-path>",
  "request_samples_file": "<path>"
}
```

应观察现象：每个指标都能回指逐请求原始文件，三组公平性字段一致，两个混压组的实际后台带宽偏差在冻结容差内。

判定边界：当前 DEMO JSON 缺少必需字段，不能通过此步骤；禁止手工把模拟 TPOT 改名为 TTFT/QPS 或把 `DEMO` 改写为 `MEASURED`。

### Step 4：运行真实前台基线

操作意图：测量同一增强代码包无后台时的前台表现，作为 TPOT 干扰基线。

执行动作：

1. 固定模型、Tokenizer、Prompt/输出 Token 分布和目标速率；
2. 关闭后台 KV 搬运和 QoS 控制；
3. 运行预热窗口后开始稳态采样；
4. 保存每个请求的到达、首 Token、各输出 Token、完成或失败时间；
5. 记录实际 QPS、P99 TTFT/P99 TPOT、CPU/设备利用率和配置哈希。

应观察现象：前台请求完成数与失败数可对账，稳态窗口和预热窗口分离，结果证据级别由采集链路决定。

判定边界：只有服务端汇总均值而无逐请求样本时，不能计算可复核 P99；没有同一 `package_id` 时，不能作为统一混压的 TPOT 基线。

### Step 5：运行原生参考实现混压

操作意图：提供 TTFT/QPS 的同场景收益基线，并确保后台负载可与被测组比较。

执行动作：

1. 启动原生参考实现和相同的前台请求流；
2. 启动后台 KV 搬运，冻结读写比例、块大小、并发和目标带宽；
3. 记录网卡、设备和后台 I/O 的实际完成字节；
4. 保存前台逐请求样本和后台时间线；
5. 运行结束后核对两组混压实际后台带宽偏差。

应观察现象：原生参考实现混压结果包含 `p99_ttft_ms`、`p99_tpot_ms`、`qps`、`bg_bw_gbps` 和全部元数据。

判定边界：只记录目标带宽或只运行本地协程时，不能作为收益基线；后台实际带宽不匹配时，标记 `CONDITIONAL` 或 `INVALID-EVIDENCE`。

### Step 6：运行统一 KV SemanticQoS 混压

操作意图：在同一后台实际负载下观察硬件队列和应用层退避是否降低前台干扰。

执行动作：

1. 启动与 Step 4 相同的增强代码包；
2. 配置并保存 DSCP/CoS 到 TC0/TC1 的现场映射；
3. 接入 `Worker.step()` 或等价前台事件；
4. 运行后台真实 I/O，采集预算变化、提交/完成字节和队列计数；
5. 保存前台逐请求 TTFT/TPOT、实际 QPS、失败请求和所有异常；
6. 核对 `actual_path` 与 `planned_path`，避免把“开启 QoS”当作路径观测。

应观察现象：前台事件与后台预算变化在时间线上可关联；两个混压组后台实际带宽可比；QoS 组的 P99 指标由真实样本计算。

判定边界：如果只有控制器日志、没有后台提交量和网卡/设备计数，不能判定退避真正生效。

### Step 7：执行三组汇总与门禁

操作意图：用统一脚本计算三项门限，并保留脚本输入和异常信息。

执行动作：

```bash
python3 run_mixed_bench.py \
  --unified-foreground ./results/unified_foreground.json \
  --mooncake-native-mixed ./results/mooncake_native_mixed.json \
  --unified-mixed ./results/unified_mixed_qos.json \
  --background-tolerance-pct 5 \
  --target-rate-rps 30 \
  --out ./results/pvt07_summary.json
```

应观察现象：脚本输出三项百分比指标、四个 gate 和输入快照。当前脚本的 `PASS/FAIL/INVALID_EVIDENCE` 是内部结果，正式报告需要映射到本文档状态枚举。

判定边界：脚本通过字段校验不等于物理链路已验证；若输入证据级别不是 `MEASURED`、实际路径不明或原始样本缺失，报告不得输出 `GO`。

### Step 8：归档证据包

操作意图：让评审人员能够复核混压时序、公平性和门禁计算。

执行动作：

```text
results/pvt07/<run_id>/
  metadata.yaml
  command.txt
  git_commit.txt
  git_status.txt
  environment.txt
  topology.txt
  qos_mapping.txt
  foreground_request_samples.jsonl
  background_io_samples.jsonl
  foreground_summary.json
  native_mixed_summary.json
  unified_mixed_summary.json
  qos_events.jsonl
  nic_queue_counters.jsonl
  device_counters.jsonl
  pvt07_summary.json
  cdf_data.csv
  errors.log
  summary.md
```

应观察现象：每个汇总字段能回指原始样本、队列计数或配置快照。

判定边界：缺少两组混压实际后台带宽、前台请求样本或 QoS 映射证据时，不能闭合 E3。

## 6. 证据字段、公式与汇总格式

### 6.1 每运行一行的标准字段

```csv
run_id,condition,package_id,config_hash,evidence_level,hardware_profile,topology_profile,workload_id,model_id,target_rate_rps,actual_qps,completed_requests,failed_requests,p99_ttft_ms,p99_tpot_ms,bg_target_gbps,bg_bw_gbps,bg_read_ratio,planned_path,actual_path,qos_enabled,status,invalid_reason
```

`actual_qps` 应使用完成请求数除以稳态统计窗口；目标速率只用于公平性匹配，不能替代实际 QPS。`bg_bw_gbps` 必须由设备/网络实际完成字节和统计窗口计算，不能复制 `bg_target_gbps`。

### 6.2 逐请求样本

```csv
run_id,request_id,arrival_ts_ns,first_token_ts_ns,finish_ts_ns,output_tokens,ttft_ms,tpot_p50_ms,tpot_p99_ms,request_status,error_code,foreground_step_count
```

如果服务端只能给出每请求平均 TPOT，仍需说明其统计口径；P99 不能由多个请求平均值再次推导。

### 6.3 QoS 事件和后台 I/O

```csv
run_id,step_seq,event,request_id,budget_before_bps,budget_after_bps,foreground_tpot_ms,bg_submitted_bytes,bg_completed_bytes,nic_tc0_bytes,nic_tc1_bytes,ts_ns,reason
```

`event` 至少包括 `foreground_step_begin`、`foreground_step_end`、`budget_backoff`、`budget_restore`、`background_submit`、`background_complete` 和 `queue_counter_sample`。

### 6.4 汇总公式

沿用 `run_mixed_bench.py` 的基线定义：

```text
TTFT 降幅 = (原生参考实现混压 P99 TTFT - 统一 KV 混压 P99 TTFT)
          / 原生参考实现混压 P99 TTFT × 100%

QPS 提升 = (统一 KV 混压实际 QPS - 原生参考实现混压实际 QPS)
        / 原生参考实现混压实际 QPS × 100%

TPOT 干扰率 = (统一 KV 混压 P99 TPOT - 统一 KV 纯前台 P99 TPOT)
            / 统一 KV 纯前台 P99 TPOT × 100%

后台带宽偏差 = |统一 KV 混压实际带宽 - 原生参考实现混压实际带宽|
             / 原生参考实现混压实际带宽 × 100%
```

分母必须为正，统计窗口、请求流和后台负载要一致。若 P99 TTFT 或 QPS 的输入为零、空值或来自 DEMO，汇总结果为 `INVALID-EVIDENCE`。

### 6.5 证据分级

| 级别 | 允许内容 | 不允许内容 |
|---|---|---|
| `DEMO` | 本地随机 TPOT、控制器布尔状态、结果 schema 和公式演示 | TTFT/QPS 收益、真实 QoS、两节点混压结论 |
| `LAB` | 测试服务、模拟后台 I/O 或局部真实网络的可复核结果 | 直接外推到生产两节点和现场网卡队列 |
| `MEASURED` | 真实服务、真实后台 I/O、真实硬件计数、完整时间线和 A/B 字段 | 缺少逐请求样本、实际带宽或 QoS 映射证明 |

## 7. 判定标准、无效证据与止损条件

### 7.1 状态枚举

- `GO`：三组真实数据完整，后台实际负载可比，TTFT 降幅至少 20%、QPS 提升至少 10%、TPOT 干扰率小于 3%，且 QoS 控制事件与硬件/设备观测一致；
- `CONDITIONAL`：部分门限或部分拓扑满足，或只能在限定 QoS/后台负载下成立，必须写明条件；
- `NO-GO`：前台干扰超过止损线、门限明显不满足、后台控制无法恢复、请求错误或出现不可解释的队列/设备异常；
- `NOT-SUPPORTED`：现场没有两节点、真实后台 I/O、RoCE/等价网络 QoS 或可观测队列，无法执行对应验证；
- `INVALID-EVIDENCE`：使用 DEMO、三组字段不公平、实际后台带宽不可比、原始样本缺失、计划路径与实际路径不一致或只依赖脚本内部 PASS。

### 7.2 当前汇总脚本与正式状态的映射

| 当前脚本输出 | 正式解释 |
|---|---|
| `PASS` | 仅表示输入字段通过脚本门限；只有输入为完整 `MEASURED` 且路径证据齐全时才可进一步判为 `GO` |
| `FAIL` | 门限未同时满足；结合证据完整性判为 `CONDITIONAL` 或 `NO-GO` |
| `INVALID_EVIDENCE` | 统一映射为 `INVALID-EVIDENCE` |
| 输入为 `DEMO` 被拒绝 | 保持 `DEMO`/`INVALID-EVIDENCE`，不得改写为失败实测 |

### 7.3 无效证据规则

以下情况不能进入 E3 `MEASURED` 汇总：

- 用随机生成的 TPOT 样本冒充真实推理请求；
- 以 `bg_target_gbps` 代替 `bg_bw_gbps`；
- 三组运行时间、请求速率、输出 Token 分布或后台读写比例不同；
- 只改变 `--qos` 参数，却没有真实后台提交端读取预算；
- `semantic_qos_controller.py` 的 `pass` 分支被描述为“已完成动态退避”；
- 输入 JSON 手工补齐 `p99_ttft_ms`、`qps` 或 `package_id`，但没有原始来源；
- 汇总脚本输入虽非 `DEMO`，但没有完整 `MEASURED` 采集链路；
- 只有 P99 没有逐请求样本，或没有前后台实际完成计数；
- 网卡队列映射只存在配置文件，没有现场计数或抓包/设备状态对账。

### 7.4 立即止损条件

出现以下任一情况，应停止扩大后台压力并保留现场：

- 前台请求错误率持续上升、服务端失去响应或 TPOT 长尾超过预设止损线；
- Incast 导致丢包、重传、PFC 持续暂停或网卡队列异常增长；
- 后台限速后无法恢复，或恢复时再次造成前台长尾；
- QoS 组出现比无 QoS 组更高且无法解释的设备错误；
- 计划使用 TC0/TC1，但现场计数显示流量没有进入对应队列；
- 三组后台实际带宽不匹配，继续比较将改变实验问题。

## 8. 阶段推进与闭环

### E0：数据契约和基线定义

冻结三组条件、字段、基线、公式、运行窗口和证据等级。当前 PVT-07 可以完成随机 DEMO 与汇总脚本接口审计。

### E1：真实前台服务和后台 I/O

分别接入前台推理服务和后台 KV I/O，打通逐请求 TTFT/TPOT、实际 QPS、后台完成字节和完整元数据。

### E2：SemanticQoS 控制闭环

打通前台 step 事件、控制器预算、后台提交端、网卡队列和设备计数，完成关闭 QoS、硬件队列、应用退避、双层开启的消融。

### E3：两节点前后台混压门禁

在真实两节点和统一请求流下完成三组 A/B，核对后台实际带宽，计算 P99 TTFT、QPS 和 P99 TPOT 干扰率，输出正式状态。

### 条件证伪：硬件 QoS 与应用退避的独立贡献

分别关闭硬件队列和应用退避，验证收益是否来自真实机制而不是负载差异。若现场不支持 DSCP/CoS、TC 队列或 `Worker.step()` 事件，应把结论限定为软件模拟或 `NOT-SUPPORTED`。

## 9. 研发人员与 AI Agent 执行约束

### 9.1 研发人员检查清单

- [ ] 三组条件的硬件、拓扑、模型、请求流、目标速率和后台负载已冻结；
- [ ] 纯前台组与统一混压组使用同一 `package_id`；
- [ ] 原生混压和统一混压的实际后台带宽在容差内；
- [ ] TTFT、TPOT 和 QPS 来自真实请求样本；
- [ ] 后台带宽来自设备/网络实际完成字节；
- [ ] QoS 控制器接入实际后台提交端，而不是只修改局部布尔值；
- [ ] DSCP/CoS、TC0/TC1 映射有现场配置和计数证据；
- [ ] `planned_path` 与 `actual_path` 分开记录；
- [ ] DEMO/LAB/MEASURED 未混入同一门禁汇总；
- [ ] 所有异常、失败请求和止损事件均保留。

### 9.2 AI Agent 执行提示词

```text
你负责执行 PVT-07 前后台混压端到端最小闭环与 SemanticQoS 验证。

先读取项目索引、公共 Benchmark 契约、本方案和原型目录。确认当前 mixed_workload_bench.py 只是 asyncio 随机 TPOT/后台字节 DEMO，semantic_qos_controller.py 只有布尔暂停且超限分支为空，run_mixed_bench.py 需要完整三组 JSON 字段并会拒绝 evidence_level=DEMO。不要把当前脚本输出改名为真实 TTFT/QPS，也不要把目标带宽当作实际带宽。

正式实验必须使用同一硬件、拓扑、模型、请求流、目标速率和后台实际负载，建立统一 KV 纯前台、原生参考实现混压、统一 KV SemanticQoS 混压三组数据。记录逐请求 TTFT/TPOT、完成/失败、实际 QPS、后台提交/完成字节、网卡队列计数、控制器预算变化、planned_path、actual_path 和原始日志。

先做 DEMO 接口审计，再接入真实前台服务、后台 I/O、前台 step 事件和 DSCP/CoS 队列映射。P99 TTFT 降幅、QPS 提升和 TPOT 干扰率必须从原始样本计算。最终输出 GO、CONDITIONAL、NO-GO、NOT-SUPPORTED 或 INVALID-EVIDENCE；如果证据缺失，列出缺口，不要杜撰结果。
```

### 9.3 常见问题定位

| 现象 | 原因定位 | 处理方式 |
|---|---|---|
| 当前 DEMO 没有 TTFT/QPS | 脚本只生成 TPOT 和模拟后台字节 | 保持 DEMO，接入真实推理服务和请求统计 |
| `--mode/--rate/--bg-gbps` 报未知参数 | 当前混流脚本不支持这些参数 | 按真实 CLI 运行，或先补接口 |
| 汇总脚本提示缺字段 | DEMO 输出 schema 与汇总要求不一致 | 由真实采集器生成完整 JSON，禁止手工填数 |
| 汇总脚本提示 `is DEMO` | `run_mixed_bench.py` 明确阻止 DEMO 关闭 E3 | 提升证据链，不要改写 evidence_level |
| QoS 开启后只有随机分布变化 | 当前 `--qos` 只切换模拟样本生成分支 | 接入实际预算和后台提交端 |
| 超过 TPOT 限制后没有退避 | 控制器超限分支为 `pass` | 实现预算、事件和恢复测试 |
| 后台带宽目标相同但实际不同 | 设备/网络队列或限速配置改变了负载 | 以实际完成字节校准，重新做 A/B |
| TC0/TC1 没有计数 | DSCP/CoS 映射未生效或没有采集 | 先验证现场队列，再进行混压 |
| 前台长尾突然升高 | Incast、PFC、PCIe 或设备队列争用 | 降低后台压力，保留网卡/设备计数和时间线 |
| `INVALID_EVIDENCE` 与文档状态不一致 | 脚本内部使用下划线 | 汇总层统一映射为 `INVALID-EVIDENCE` |

本方案的完成标准不是“两个 Python 进程成功退出”，而是形成可复核链路：真实前台请求 → 真实后台搬运 → QoS 控制事件 → 网卡/设备队列观测 → 三组公平 A/B → P99 TTFT、实际 QPS 和 P99 TPOT 计算 → 最终状态判定。
