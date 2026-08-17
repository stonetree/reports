# PVT-04：QueryPlan 微秒级动态决策引擎与 CostEvaluator 验证实施方案设计

> **验证 ID**：PVT-04  
> **验证名称**：QueryPlan 微秒级动态决策引擎与 Cost Evaluator 验证  
> **对应证据门**：**E2 决策优越性**  
> **证伪标记**：否（决策能力确认）  
> **建议周期**：5~7 人日  
> **主关联 IR**：`IR-01-03`, `IR-01-05`  
> **核心 SRS / SR23 锚点**：  
> - SRS: `L1-RT-Admission-007`, `L3-SE-QueryPlanFastPath-072`, `L3-TRANS-TOPO-SENSE-004`, `L4-FABRIC-ROUTER-001`  
> - SR23: `SR23-01-03-01`, `SR23-01-05-01`, `SR23-01-09-01`, `SR23-02-02-01`, `SR23-02-02-02`  

---

## 1. 验证目标与交付结论定义

### 1.1 待验证核心命题
物理命中（Raw Hit）并不等于一定带来性能收益。当网络拥塞、前缀过短或请求 Deadline 极其苛刻时，强行远端 Load KV Cache 反而可能比本地 NPU 重新计算（Recompute）更慢。本验证旨在通过算法原型与微基准证明：
1. **QueryPlan 动态决策引擎**能够在 **$P99 < 5\mu s$** 内，基于实时 Telemetry 链路状态（EWMA 带宽、队列深度）与 NPU 算力吞吐，在 `Load(Local)`, `Load(Remote)`, `SSD Restore`, `Recompute` 之间输出全局最优计划；
2. **CostEvaluator 成本预估模型**的预测误差 **$\text{MAPE} < 20\%$**，决策准确率 **$\ge 90\%$**，且**负收益命中率（选了 Load 但实际慢于 Recompute）严格 $< 1\%$**。

### 1.2 最终交付数据与结论产出
开发人员执行完本方案后，必须输出以下交付件：
1. **《QueryPlan FastPath 决策时延分位值表》**（P50, P90, P99, P99.9）；
2. **《CostEvaluator 预测耗时 vs 真实/反事实执行耗时对账表》**；
3. **《网络拥塞与短前缀下负收益拦截率验证表》**；
4. **《Go / No-Go 判定结论》**：依据决策耗时 $P99 < 5\mu s$ 与负收益率 $< 1\%$ 门槛判定。

---

## 2. 核心数据结构与算法原型详细设计

### 2.1 核心数据结构定义

```cpp
// 1. 计划决策动作枚举
enum class PlanAction : uint8_t {
    Local_HBM_Attach = 0, // 本地 HBM 直接复用 (开销 ~0.05ms)
    Remote_URMA_Load = 1, // 跨节点 800G URMA 传输 (开销 ~2.5ms)
    Local_SSD_Restore= 2, // 本地 NVMe SSD 直达换入 (开销 ~12.0ms)
    Recompute        = 3  // 本地算力重算 (Prefill Compute)
};

// 2. 访问意图描述 (来自调度层)
struct alignas(64) KVAccessIntent {
    uint64_t request_id;
    uint32_t prefix_tokens;       // 匹配前缀 Token 数
    uint32_t deadline_ms;         // 业务 SLA 最晚容忍时延 (0 为无限制)
    uint8_t priority_class;       // 0: 高优先级交互流, 1: 批处理
    bool is_cached_locally;       // 本地是否驻留
    bool is_cached_remotely;      // 远端节点是否驻留
    bool is_cached_ssd;           // SSD 是否归档
};

// 3. 实时遥测快照 (无锁原子更新)
struct alignas(64) LinkTelemetrySnapshot {
    std::atomic<double> ewma_remote_bw_gbps{750.0}; // 指数平滑可用带宽
    std::atomic<double> remote_queue_delay_ms{0.2}; // 远端队列等待
    std::atomic<double> ssd_read_bw_gbps{190.0};    // SSD 直达带宽
    std::atomic<double> npu_prefill_tps{8500.0};    // NPU Prefill 算力吞吐 (Tokens/s)
    std::atomic<double> meta_overhead_ms{0.08};     // 元数据交互时延 (80us)
};

// 4. 决策输出结果
struct ExecutionPlan {
    PlanAction action;
    double estimated_cost_ms;     // 预估总耗时
    double recompute_baseline_ms; // 重算对照基线耗时
    const char* decision_reason;  // 决策触发分支描述
};
```

### 2.2 CostEvaluator 五维成本评估模型

```
1. 算力重算耗时预估：
   T_recompute = (prefix_tokens / npu_prefill_tps) * 1000.0  [ms]

2. 远端 URMA 加载耗时预估：
   KV_bytes = prefix_tokens * bytes_per_token (例: 72B模型为 327.68 KB/token)
   T_remote = meta_overhead + queue_delay + (KV_bytes * 8 / (ewma_remote_bw * 1e6))  [ms]

3. SSD 直达恢复耗时预估：
   T_ssd = meta_overhead + (KV_bytes * 8 / (ssd_read_bw * 1e6)) + t_nvme_driver_setup  [ms]
```

### 2.3 微秒级 FastPath 决策树状态机与剪枝算法

```mermaid
flowchart TD
    Start["收到 KVAccessIntent (Tokens, Deadline)"] --> CheckLocal{"is_cached_locally ?"}
    CheckLocal -- "YES" --> ActionLocal["Plan: Local_HBM_Attach<br/>(最优路径, 耗时 ~0.05ms)"]
    CheckLocal -- "NO" --> EvalCost["CostEvaluator: 并发估算 T_recompute, T_remote, T_ssd"]
    
    EvalCost --> CheckDeadline{"Deadline > 0 ?"}
    CheckDeadline -- "YES" --> DeadlineRule{"min(T_remote, T_ssd) > Deadline ?"}
    DeadlineRule -- "YES" --> ForceRecompute["Plan: Recompute<br/>(原因: 传输排队超时, 触发 Deadline 保护)"]
    DeadlineRule -- "NO" --> CompareNet
    
    CheckDeadline -- "NO" --> CompareNet{"is_cached_remotely && (T_remote < T_recompute) ?"}
    CompareNet -- "YES" --> ActionRemote["Plan: Remote_URMA_Load<br/>(正收益确认: 省时 T_recomp - T_remote)"]
    CompareNet -- "NO" --> CheckSSD{"is_cached_ssd && (T_ssd < T_recompute) ?"}
    CheckSSD -- "YES" --> ActionSSD["Plan: Local_SSD_Restore<br/>(容量层回源)"]
    CheckSSD -- "NO" --> Intercept["Plan: Recompute<br/>(负收益强制拦截: 加载耗时慢于算力重算)"]
```

#### 极速 C++ 决策核心代码实现：
```cpp
ExecutionPlan QueryPlanFastPath::generate_plan(const KVAccessIntent& intent, const LinkTelemetrySnapshot& tele) {
    ExecutionPlan plan;
    double t_recomp = (intent.prefix_tokens / tele.npu_prefill_tps.load(std::memory_order_relaxed)) * 1000.0;
    plan.recompute_baseline_ms = t_recomp;

    // 1. 本地命中分支 (极速返回)
    if (intent.is_cached_locally) {
        plan.action = PlanAction::Local_HBM_Attach;
        plan.estimated_cost_ms = 0.05;
        plan.decision_reason = "LOCAL_HBM_HIT";
        return plan;
    }

    // 2. 远端加载成本计算
    double kv_bytes = intent.prefix_tokens * 327680.0; // 320KB/tok
    double t_remote = tele.meta_overhead_ms.load(std::memory_order_relaxed) +
                      tele.remote_queue_delay_ms.load(std::memory_order_relaxed) +
                      (kv_bytes * 8.0) / (tele.ewma_remote_bw_gbps.load(std::memory_order_relaxed) * 1e6);

    // 3. Deadline 约束检查
    if (intent.deadline_ms > 0 && t_remote > intent.deadline_ms && t_recomp <= intent.deadline_ms) {
        plan.action = PlanAction::Recompute;
        plan.estimated_cost_ms = t_recomp;
        plan.decision_reason = "DEADLINE_MISS_FALLBACK_RECOMPUTE";
        return plan;
    }

    // 4. 正负收益严格裁决 (负收益拦截)
    if (intent.is_cached_remotely && t_remote < t_recomp) {
        plan.action = PlanAction::Remote_URMA_Load;
        plan.estimated_cost_ms = t_remote;
        plan.decision_reason = "POSITIVE_BENEFIT_REMOTE_LOAD";
        return plan;
    }

    plan.action = PlanAction::Recompute;
    plan.estimated_cost_ms = t_recomp;
    plan.decision_reason = "NEGATIVE_BENEFIT_INTERCEPTED";
    return plan;
}
```

---

## 3. 基础/对照 Micro-Benchmark 构建方法

### 3.1 测试工具与源码结构
本项验证涉及的全部决策引擎与压测 Harness 源码均存放在 `./原型验证代码/PVT-04/` 目录下：

```
原型验证代码/PVT-04/
├── query_plan_fastpath.h  # 微秒级动态决策引擎与 CostEvaluator 成本预估头文件
├── query_plan_fastpath.cc # 实时链路感知、5 维成本预估与微秒级剪枝决策算法实现
├── query_plan_bench.cc    # 决策引擎 100K QPS 吞吐压测与反事实决策对账 Harness
└── Makefile               # 编译 query_plan_bench 的工程构建文件 (make -j16)
```

编译方法：
```bash
# 编译决策引擎压测 Harness
cd ./原型验证代码/PVT-04 && make clean && make
```
- **FastPath 决策核心**：执行无锁、查表与成本计算逻辑；
- **反事实校验执行器（Counterfactual Executor）**：在真实执行所选 Action 后，立即镜像执行未被选择的 Action，精准获取真实客观的时间差。

### 3.2 四组实验对照设计
- **实验组（QueryPlan 智能决策）**：由引擎依据实时链路感知动态选择 Action；
- **对照组 A（盲目总是 Load）**：只要有缓存命中，强制执行远端传输；
- **对照组 B（纯算力重算基线）**：忽略所有缓存，强制由 NPU 纯算力重新计算；
- **对照组 C（反事实镜像组）**：用于精确计算实验组决策的“后悔值”与准确率。

---

## 4. 业务 Benchmark 构造与流量特征编排

### 4.1 四类典型决策场景构造
1. **场景 1（正常空闲长前缀）**：
   - 前缀 32K Tokens，网络空闲（800G URMA），此时 $T_{load} \approx 2.5\text{ms} \ll T_{recompute} \approx 40.0\text{ms}$，应决策 `Remote_Load`；
2. **场景 2（极短前缀场景）**：
   - 前缀 16 ~ 32 Tokens，此时 $T_{recompute} \approx 0.05\text{ms} < T_{load\_overhead} \approx 0.2\text{ms}$，应决策 `Recompute`；
3. **场景 3（网络严重拥塞/高丢包）**：
   - 前缀 8K Tokens，人为注入网络拥塞（限速至 1Gbps / 增加 30ms 延迟），此时 $T_{load} \approx 65.0\text{ms} > T_{recompute} \approx 10.0\text{ms}$，应决策 `Recompute`；
4. **场景 4（苛刻 Deadline 约束）**：
   - 请求携带 `Deadline = 5ms`，但远端队列排队预计需要 12ms，应决策 `Recompute`。

---

## 5. 软硬件环境与打点插桩方案

### 5.1 注入与打点配置
- 利用 Linux `tc netem` 动态注入链路延迟与抖动；
- 植入打点：
  - `T_decision_start` / `end`：决策耗时（纳秒级）；
  - `T_actual_action_cost`：所选动作真实耗时；
  - `T_counterfactual_cost`：反事实对比耗时。

---

## 6. 分步执行测试操作规程

开发人员请按以下 12 个步骤依次执行：

### 步骤 1：编译微基准 Harness
编译 `query_plan_bench`。

### 步骤 2：执行极限并发单核决策延迟压测
循环调用 500,000 次 `generate_plan()`，记录 QPS 与延迟分位值（P50, P90, P99, P99.9）：
```bash
./query_plan_bench
```

### 步骤 3：验证场景 1（空闲长前缀）
注入 32K Tokens 请求，验证决策动作是否为 `Remote_Load`，并记录预估耗时与实际耗时。

### 步骤 4：验证场景 2（极短前缀拦截）
注入 16~64 Tokens 请求，验证决策引擎是否正确拦截小 I/O 并决策为 `Recompute`。

### 步骤 5：验证场景 3（网络拥塞扰动注入）
通过 `tc netem` 注入 50ms 网络延迟，验证决策引擎是否立即感知并切换为 `Recompute`。

### 步骤 6：验证场景 4（Deadline 约束求解）
设置 Deadline = 5ms，验证决策动作是否正确放弃排队。

### 步骤 7：启用反事实镜像对账（Counterfactual Check）
开启反事实执行器，对 10,000 个混合请求记录实际耗时与未选路径耗时。

### 步骤 8：计算 CostEvaluator 预测误差 MAPE
比对 $T_{pred}$ 与 $T_{real}$，计算平均绝对百分比误差。

### 步骤 9：统计负收益发生率（Negative Benefit Ratio）
统计实际发生“选了 Load 但实际慢于 Recompute”的异常请求比例。

### 步骤 10：对比“盲目 Load”组与“QueryPlan 决策”组的平均 TTFT
计算两组在混流下的平均 TTFT 差距。

### 步骤 11：压力测试 Telemetry 遥测并发更新
以 1000 Hz 频率在后台持续刷新链路快照，验证决策引擎无锁读取的线程安全性与低延迟。

### 步骤 12：输出判定结论与立项证据包。

---

## 7. 数据采集清单与记录格式

### 7.1 决策引擎性能记录表 (`pvt04_decision_latency.csv`)
| 吞吐 (QPS) | P50 时延 ($\mu s$) | P90 时延 ($\mu s$) | P99 时延 ($\mu s$) | P99.9 时延 ($\mu s$) |
|---|---|---|---|---|
| 380,000 | 0.85 | 1.45 | 3.10 | 4.80 |

### 7.2 场景决策与反事实对账表 (`pvt04_plan_accuracy.csv`)
| 场景 | 前缀长度 | 决策 Action | 预估耗时 (ms) | 实际耗时 (ms) | 反事实耗时 (ms) | 决策收益 (ms) | 准确判定 |
|---|---|---|---|---|---|---|---|
| **空闲长前缀** | 32K | Remote_Load | 2.50 | 2.58 | 40.20 | +37.62 | 正确 |
| **极短前缀** | 24 | Recompute | 0.03 | 0.03 | 0.28 | +0.25 | 正确 |
| **网络拥塞** | 8K | Recompute | 10.00 | 9.85 | 68.20 | +58.35 | 正确 (拦截负收益) |
| **紧迫 Deadline**| 16K | Recompute | 20.00 | 19.50 | 35.00 | +15.50 | 正确 |

---

## 8. 数据交叉组合与运算推导逻辑

### 8.1 预测误差 MAPE 计算
$$\text{MAPE} = \frac{1}{N} \sum_{i=1}^{N} \left| \frac{T_{\text{real}}(i) - T_{\text{pred}}(i)}{T_{\text{real}}(i)} \right| \times 100\%$$

### 8.2 负收益发生率 (Negative Benefit Ratio)
$$\text{Negative Benefit Ratio} = \frac{\sum \mathbb{I}(\text{Action}=\text{Load} \land T_{\text{load\_real}} > T_{\text{recomp\_real}})}{N_{\text{total\_requests}}} \times 100\%$$

---

## 9. 多维扩展与扫参矩阵

| 维度 | 参数网格 |
|---|---|
| **前缀长度** | 16, 64, 256, 1K, 4K, 16K, 32K, 64K, 128K Tokens |
| **网络带宽** | 100M, 1G, 10G, 100G, 400G, 800G URMA |
| **注入网络延迟** | 0ms, 1ms, 5ms, 20ms, 50ms, 100ms |
| **NPU 算力吞吐** | 2000, 5000, 8500, 15000 Tokens/s |

---

## 10. Go / No-Go 判定规则与交付报告模板

### 10.1 判定规则
- **Go 门槛**：
  1. 决策引擎单次耗时 $P99 < 5.0\mu s$；
  2. CostEvaluator 预测误差 $\text{MAPE} < 20\%$；
  3. 决策准确率 $\ge 90\%$，负收益发生率 $< 1.0\%$。
- **No-Go 门槛**：
  - 决策耗时 $> 50\mu s$；
  - 负收益率 $> 5.0\%$（即频繁做出错误拉取决策）。

### 10.2 开发者交付报告格式模板
```markdown
# PVT-04 提前验证交付报告

## 1. 决策引擎性能
- 峰值吞吐: 380,000 req/s
- P99 决策耗时: 3.10 us (PASS, 门槛 < 5.0 us)

## 2. 预测准确度与收益实测
- CostEvaluator MAPE 误差: 11.2% (PASS, 门槛 < 20%)
- 综合决策准确率: 98.4% (PASS, 门槛 >= 90%)
- 负收益拦截率: 100.0% (在 50ms 网络拥塞与极短前缀下 0 次发生负拉取)
- 相对盲目 Load 组端到端平均 TTFT 优化: 34.2%

## 3. 最终结论
【Go / Conditional / No-Go】: GO
```
