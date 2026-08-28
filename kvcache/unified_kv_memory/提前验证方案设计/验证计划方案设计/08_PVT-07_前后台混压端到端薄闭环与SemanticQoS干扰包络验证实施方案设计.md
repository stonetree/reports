# PVT-07：前后台混压端到端最小闭环（Vertical Slice）与 SemanticQoS 干扰控制验证实施方案设计
## —— 全链路前后台混压验证：原生参考实现、统一 KV 方案与 SemanticQoS 服务质量对照

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个测试 `run_id` 需在运行前完整固化物理服务器硬件、网络拓扑、模型架构、工作负载特征、前台目标请求速率、后台 I/O 压力、代码包版本、配置哈希、QoS 队列策略、预热周期、稳态采样窗口及目标证据等级；实测产出必须完整保留前台逐请求 TTFT/TPOT 原始采样、成功与失败请求对账、实际达成 QPS、后台目标/实测读写带宽、网卡硬件队列计数、Host CPU/DDR/PCIe 性能观测、`planned_path`/`actual_path` 路径对账及前后台混压物理时间线。TTFT 与 QPS 的收益对账基线需设定为同等物理条件下的原生参考实现混压测试，而 TPOT 干扰率的评估基线需设定为同一增强代码包的纯前台无干扰运行。

> **验证范围声明**：在当前受控的原型验证工程中，`mixed_workload_bench.py` 仅在本地单机运行 Python asyncio 协程，前台在线时延与后台搬运吞吐均基于数学概率分布与 sleep 延时进行纯软件模拟；`semantic_qos_controller.py` 仅提供了单进程内的布尔变量暂停控制，尚未对接 RoCE 网卡硬件队列、应用层动态带宽预算调节或 `Worker.step()` 算子级事件回调；`run_mixed_bench.py` 亦无法直接将当前的 DEMO 演示输出作为三组生产级输入进行闭环门禁评估。因此，现有受控源码仅用于验证接口定义、统计公式及门禁判决逻辑，不可直接作为证明真实在线大模型推理服务已打通、RoCE 优先级队列已生效、NVMe SSD 物理 I/O 已受控、SemanticQoS 干扰抑制已达标或双节点集群混压测试已通过的依据。在缺乏真实分布式服务调用、硬件性能计数器及原始系统日志的前提下，测试结论统一限定标记为 `DEMO` 或 `LAB`。

> **术语速查**：
> - **KVCache**：大模型注意力键值缓存（大模型自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免后续 Token 生成时重复计算注意力）；
> - **Decode**：逐字生成阶段（大模型逐个输出 Token 的计算阶段，前台 TPOT 尾部延迟主要在该阶段形成）；
> - **TTFT**：Time To First Token（首字生成延迟 / 首 Token 响应时间）；
> - **TPOT**：Time Per Output Token（每个输出 Token 的生成耗时 / 单字生成延迟）；
> - **QoS**：Quality of Service（服务质量：通过硬件优先级队列映射与带宽保留，确保高优先级实时前台请求不受后台大流量搬运的干扰）；
> - **SemanticQoS**：前后台服务质量保障策略（通过网络 RoCE 优先级队列映射与应用层微秒级自适应退避，确保后台数据换出与拉取不干扰前台在线推理的 TPOT 尾部延迟）；
> - **RoCE**：RDMA over Converged Ethernet（基于以太网的通用远程直接内存访问协议）；
> - **DSCP**：Differentiated Services Code Point（IP 报文差分服务标记，用于向交换机声明网络优先级）；
> - **CoS**：Class of Service（以太网数据链路层服务类别标记）；
> - **TC0/TC1**：Traffic Class 0/1（网络硬件流量类别优先级队列，约定 TC0 承载前台高优先级在线流，TC1 承载后台大流量搬运流）；
> - **Incast**：多对一突发网络拥塞（多个发送节点在极短时间内同时向单一接收节点回传数据包，导致交换机出端口缓冲区瞬间耗尽而引发的排队丢包风暴）；
> - **Worker.step()**：大模型推理引擎单步调度与执行生命周期（SemanticQoS 控制器在该物理边界通知后台 I/O 动态退避降速）；
> - **EWMA**：Exponentially Weighted Moving Average（指数加权移动平均：用于平滑短周期噪声的统计算法）；
> - **CDF**：Cumulative Distribution Function（累积分布函数：用于直观展示时延分位数分布）；
> - **P99**：99 分位值（数据集中 99% 样本均优于该阈值的统计指标）；
> - **后台目标带宽**：实验前配置的预期发送速率（不等于底层网络或设备的实际吞吐）。

> **验证 ID**：PVT-07
> **验证名称**：前后台混压端到端最小闭环（Vertical Slice）与 SemanticQoS 服务质量保障验证
> **验证优先级**：**🔴 P0 级（核心关键项）**
> **对应验证阶段**：**E3（全链路前后台混压总门禁）**
> **证伪标记**：否（全链路系统与服务质量保障能力确认）
> **主关联 IR**：`IR-01-04`, `IR-01-11`, `IR-02-06`
> **核心 SRS / SR23 锚点**：
> - SRS：`L3-QO-SemanticQoS-045`, `L3-MS-StateAwarePrefetch-081`, `L3-OB-PerPathTelemetry-047`, `L4-FT-PathIntegrityPolicy-077`
> - SR23：`SR23-01-04-01`, `SR23-01-11-02`, `SR23-02-02-01`, `SR23-02-02-02`, `SR23-02-06-01`, `SR23-02-11-01`, `SR23-02-12-03`, `SR23-02-12-05`
> **配套源码**：[`./原型验证代码/PVT-07/`](./原型验证代码/PVT-07/)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`；vLLM `842dd8fd96650063e1ad32e6075742d457d39773`。正式测试结果需绑定实际推理服务引擎、网卡与交换机 QoS 配置、底层驱动及配置哈希。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 前后台混压的物理资源争用与干扰链剖析

在大模型分布式推理集群中，前台在线推理的 Decode 阶段对时延高度敏感，每个 Token 的自回归生成均依赖微秒级的显存访问与节点间集合通信。而在同一物理节点与网络拓扑上，后台数据面同时承载着分层存储换入换出、跨节点 Prefetch 预取以及热点数据广播任务。在物理硬件层面，前后台双流将共同争用以下关键路径：

```text
前台在线流 : 在线请求 ──► 批处理调度 ──► NPU Decode 计算 ──► 节点间集合通信 AllReduce ──► 输出 Token
                                                   │                       │
                                           [PCIe 总线争用]          [网卡出端口 Incast 争用]
                                                   │                       │
后台搬运流 : 冷热 KV 换出 ──► DMA 读写 ──► PCIe P2P 传输 ──────────► RoCE 高吞吐网络传输 ──► 目标介质
```

若缺乏系统级的流控隔离，后台突发大流量搬运将占满交换机出端口缓冲区与 PCIe 总线带宽，导致前台 AllReduce 集合通信数据包在网卡队列中排队甚至遭遇丢包重传，引发前台 TPOT 出现长尾抖动，破坏业务在线 SLO。

SemanticQoS 服务质量保障策略的核心目标，是在前台执行在线 Decode 的微秒级关键时间窗口内，利用软硬件协同流控主动压制后台流量，确保前台集合通信获得足够的总线与网络带宽保障。

### 0.2 SemanticQoS 软硬件协同双层保障机制

本方案设计了“硬件流分类队列 + 应用层微秒级自适应退避”的双层服务质量保障机制：
1. **硬件层（网络与总线流控）**：通过在 IP 报文头部设置标准 DSCP/CoS 标记，在网卡硬件与交换机出端口将流量划分为不同的 Traffic Class 队列。将前台在线交互流映射至高优先级的 **TC0** 队列（保障优先调度与专属带宽隔离），后台搬运流映射至较低优先级的 **TC1** 队列，从物理层消除 Incast 队列头阻塞；
2. **应用层（微秒级自适应动态退避）**：在前台推理引擎的单步算子执行物理边界 (`Worker.step()`) 动态捕获时延指标。在 Step 启动瞬间自动缩减后台 I/O 提交预算；在 Step 结束且 TPOT 稳定达标后，基于指数加权移动平均 (EWMA) 按受控步长平滑恢复后台带宽，并设置上限保护。

```text
前台推理 Worker.step() 开始
        │
        ├─► 发送 StepBegin 事件，记录物理时间戳
        ├─► 压低后台 I/O 提交带宽预算 (foreground_guard_budget)
        └─► 前台高优先级执行 Decode 计算与 AllReduce 集合通信 (独占 TC0 硬件队列)
        │
前台推理 Worker.step() 结束
        │
        ├─► 采样并记录当前 Step 的实际 TPOT 耗时
        ├─► 更新 EWMA 移动平均线，监测长尾抖动偏离度
        └─► 若 TPOT 稳定在安全范围内 ──► 按步长受控恢复后台带宽预算 (不超过 max_budget)
            若 TPOT 发生超限扰动   ──► 触发指数退避，进一步收紧后台 I/O 吞吐
```

### 0.3 当前受控源码能力矩阵审计

| 源码文件与路径 | 当前受控源码实际行为 | 现阶段尚不能声称的能力 |
|---|---|---|
| `原型验证代码/PVT-07/mixed_workload_bench.py` | 采用 Python asyncio 并发运行前台与后台协程；前台通过正态/指数随机分布生成模拟 TPOT；后台按 64MB 固定间隔累加模拟字节数；输出 `tpot_*`、`bg_bandwidth_gbps` 并标记 `DEMO,DEMO_ONLY` | 无真实推理服务打流；无实际 TTFT 与 QPS 测量；无底层 RoCE 网卡与 NVMe SSD 物理 I/O；未对接 `Worker.step()`；无真实双节点集群通信 |
| `原型验证代码/PVT-07/semantic_qos_controller.py` | `on_foreground_step_begin()` 将 `bg_throttled` 设为 `True`；`on_foreground_step_end()` 直接将其重置为 `False`；超限分支为空操作 (`pass`)；`allow_background_transfer()` 仅返回布尔值 | 未实现 TC0/TC1 硬件队列映射；无 DSCP/CoS 标记；无动态带宽预算管理；无 EWMA 时延平滑；无后台真实 I/O 提交控制 |
| `原型验证代码/PVT-07/run_mixed_bench.py` | 读取三组 JSON 报告；执行字段完整性校验、A/B 对照公平性核验及后台带宽偏差计算；依据公式计算 TTFT 降幅、QPS 提升及 TPOT 干扰率 | 本身不产生真实性能指标；未对输入数据是否来自生产级 `MEASURED` 建立自动核验；对输入为 DEMO 的数据做拒绝拦截 |

### 0.4 当前脚本之间的数据格式边界

`mixed_workload_bench.py` 生成的演示 JSON 仅包含 `qos_enabled`、样本量、TPOT 分位数及模拟后台带宽；而 `run_mixed_bench.py` 严格要求输入 JSON 包含 `run_id`、`package_id`、`config_hash`、`hardware_profile`、`topology_profile`、`workload_id`、`model_id`、`target_rate_rps`、`p99_ttft_ms`、`p99_tpot_ms`、`qps` 及 `bg_bw_gbps` 等全量对账字段。

---

## 1. 验证目标、交付物与候选准入门槛

### 1.1 核心验证目标

| 验证核心维度 | 核心物理与工程问题 | 所需客观证据 |
|---|---|---|
| 纯前台黄金基线 | 同一增强代码包在无后台 I/O 干扰时的 TTFT、TPOT 及实际 QPS 物理基线是多少 | 真实在线推理服务打流、逐请求全量样本及成功/失败对账表 |
| 原生参考实现混压 | 原生参考实现在承受相同后台满载 I/O 压力时的 P99 TTFT、P99 TPOT 及 QPS 表现如何 | 真实后台 I/O 提交/完成对账日志、前台逐请求时间线及物理网络拥塞监控 |
| 统一 KV 方案 QoS 混压 | 开启 SemanticQoS 双层保障后，在同等后台负载下系统能否降低前台时延并提升吞吐 | 硬件队列统计、QoS 动态退避事件流、后台实测吞吐及前台逐请求时延样本 |
| 控制面闭环有效性 | 前台 `Worker.step()` 事件是否能在微秒级时间内切实驱动后台 I/O 提交预算退避与平稳恢复 | Step 事件物理时间戳、带宽预算动态调节日志、底层 I/O 提交端实测对账 |
| 异常与拥塞鲁棒性 | 在突发网络 Incast 拥塞、后台短 I/O 异常及前台突发流量冲击下，系统能否平稳收敛 | 交换机 PFC 丢包监控、异常错误日志、QoS 控制器恢复耗时及请求成功率 |

### 1.2 候选工程准入门槛 (E3 阶段)

在真实的 2 节点集群、完全相同的模型权重、请求序列及后台物理负载条件下，E3 全链路混压总门禁的候选准入门槛如下：
- **首字生成延迟改善**：相对同等负载下的原生参考实现混压测试，统一 KV 方案的 **首字生成延迟 P99 TTFT 降低 $\ge 20\%$**；
- **在线服务吞吐提升**：相对同等负载下的原生参考实现混压测试，统一 KV 方案的 **在线请求达标吞吐量 (QPS) 提升 $\ge 10\%$**；
- **前台长尾干扰抑制**：相对同一增强代码包的纯前台黄金基线，开启 SemanticQoS 后的 **单字生成延迟 P99 TPOT 干扰恶化率 $< 3\%$**。

### 1.3 阶段交付资产

每个正式 `run_id` 交付：
1. **《三组核心实验全量原始数据与公平性核验证明》**：涵盖纯前台基线、原生参考实现混压、统一 KV QoS 混压的三份独立 JSON 报告及 A/B 对齐清单；
2. **《前台逐请求时延全量采样表》**：记录每个请求的到达时间、首 Token 时间戳、逐 Token 生成时延、完成状态及错误归因；
3. **《后台 I/O 物理吞吐与网络队列审计日志》**：包含后台提交/完成字节数、目标/实测带宽对比、TC0/TC1 网卡队列计数器及 DSCP 抓包凭证；
4. **《SemanticQoS 控制流转与退避时间线分析报告》**：记录 `Worker.step()` 事件与后台带宽预算的微秒级关联轨迹；
5. **标准证据包与判定报告**：包含 `manifest.json`、原始数据、CDF 绘图及 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE` 最终判定。

---

## 2. 目标控制接口与调度模型设计

### 2.1 目标 QoS 流量描述符设计

面向软硬件协同流控，底层 QoS 流量描述符标准化定义如下：

```cpp
enum class TrafficClass : uint8_t {
    FOREGROUND_ONLINE = 0,      // 前台在线交互流 (高优先级，映射至 TC0)
    BACKGROUND_TIERING = 1,     // 后台分层搬运流 (低优先级，映射至 TC1)
};

struct alignas(64) QoSFlowDescriptor {
    uint64_t     run_id_hash;
    TrafficClass traffic_class;
    uint32_t     dscp;                          // IP 报文 DSCP 标记 (如 CS6 或 AF41)
    uint8_t      cos;                           // 以太网 802.1p CoS 优先级标记
    uint32_t     queue_id;                      // 底层硬件队列 ID (TC0 / TC1)
    uint64_t     target_bandwidth_bytes_per_sec;// 目标发送带宽
    uint64_t     current_budget_bytes_per_sec;  // 当前动态分配的传输预算
    uint64_t     in_flight_bytes;               // 在途未完成 I/O 字节数
    uint64_t     step_seq;                      // 关联的前台 Step 序号
};
static_assert(sizeof(QoSFlowDescriptor) <= 64);
```

### 2.2 SemanticQoS 控制器状态机流转

```text
[NORMAL 常态运行]
     │
     ▼
[FOREGROUND_STEP_ACTIVE 前台 Step 启动] ──► 压低后台预算至 [BACKGROUND_BUDGET_REDUCED]
     │                                                    │
     │ 前台 Decode 计算与 AllReduce 通信                  │ 后台按安全预算低速传输
     ▼                                                    ▼
[FOREGROUND_STEP_DONE 前台 Step 结束] ◄────────────────────┘
     │
     ▼
[RECOVERING_WITH_CAP 平滑受控恢复] ──(未超限且达到步长)──► [NORMAL 常态运行]
     │
     └─► (检测到 TPOT 超限扰动) ──► 触发指数退避 ──► [BACKGROUND_BUDGET_REDUCED]
```

### 2.3 动态退避与平滑恢复数学模型

$$
\text{Step 启动阶段} : \quad B_{\text{bg}}(t) = \min\left( B_{\text{guard}}, B_{\text{bg}}(t-1) \right)
$$

$$
\text{Step 结束阶段（发生时延超限扰动）} : \quad B_{\text{bg}}(t) = \max\left( B_{\text{min}}, B_{\text{bg}}(t-1) \times \alpha \right), \quad \alpha \in (0, 1)
$$

$$
\text{Step 结束阶段（时延稳定达标）} : \quad B_{\text{bg}}(t) = \min\left( B_{\text{max}}, B_{\text{bg}}(t-1) + \Delta B \right)
$$

---

## 3. 实验矩阵与 A/B 公平性校验规则

### 3.1 三组核心对比条件定义

| 实验条件标识 | 代码包版本与配置 | 后台物理负载 | 核心对账用途与定位 |
|---|---|---|---|
| `unified_foreground` | 软硬件协同方案代码包 (Unified KV) | **完全关闭** (0 后台 I/O) | **TPOT 干扰率计算的黄金基线** |
| `mooncake_native_mixed` | 开源原生参考实现代码包 (Mooncake Native) | **完全开启** (满载真实 I/O) | **TTFT 降幅与 QPS 提升的同场景收益基线** |
| `unified_mixed_qos` | 软硬件协同方案代码包 (Unified KV) | **完全开启** (开启 SemanticQoS) | **被测核心目标条件（全链路验证）** |

### 3.2 负载压力测试矩阵

| 测试维度 | 正式实施计划标准 | 工程说明与约束 |
|---|---|---|
| 前台目标请求速率 | 5、15、30 req/s | 评估不同并发负载下的时延与吞吐边界 |
| 前台并发客户端数 | 1、8、32、64 并发 | 构造阶梯式的推理请求并发压力 |
| 后台物理实测带宽 | 物理设备可用带宽的 25%、50%、80% | 检验不同后台压力级别下的总线与网络争用 |
| 后台并发 Worker 数 | 1、4、8 并发任务 | 评估多流并发下的 I/O 竞争与调度开销 |
| 稳态评测时长 | 预热 30s，稳态采样 $\ge 5\text{min}$ | 确保捕获长周期稳态表现 |
| 负载上下文分布 | 严格固化 Prompt 长度与生成 Token 数分布 | 记录随机种子与测试数据集指纹 |
| QoS 策略消融对比 | 关闭 QoS、仅硬件队列、仅应用退避、双层完整开启 | 拆解各层机制的独立贡献 |

### 3.3 A/B 对照公平性校验规则

1. 三组数据的 `hardware_profile`、`topology_profile`、`workload_id`、`model_id` 及 `target_rate_rps` 必须完全一致；
2. `unified_foreground` 纯前台组与 `unified_mixed_qos` 混压组的 `package_id` 必须严格一致；
3. 两组混压测试（原生参考实现 vs 统一 KV 方案）的后台实测物理带宽 `bg_bw_gbps` 偏差必须在预设容差范围（默认 $\le 5\%$）之内；
4. 输入数据的证据等级必须为 `MEASURED`。

---

## 4. 工具审计与最小实现增量

### 4.1 当前基准测试脚本行为审计

当前受控 `mixed_workload_bench.py` 的执行命令：

```bash
cd ./原型验证代码/PVT-07
python3 mixed_workload_bench.py \
  --fg-clients 32 --bg-workers 4 --duration 5 \
  --out mixed_workload_no_qos_demo.json --seed 42

python3 mixed_workload_bench.py \
  --fg-clients 32 --bg-workers 4 --duration 5 --qos \
  --out mixed_workload_qos_demo.json --seed 42
```

### 4.2 当前门禁汇总脚本行为审计

当前受控 `run_mixed_bench.py` 的 CLI 执行命令：

```bash
python3 run_mixed_bench.py \
  --unified-foreground <foreground-json> \
  --mooncake-native-mixed <native-mixed-json> \
  --unified-mixed <unified-mixed-json> \
  --background-tolerance-pct 5 \
  --target-rate-rps 30 \
  --out pvt07_summary.json
```

### 4.3 面向生产级闭环的最小工程增量

正式进入 E3 阶段验收前需补齐：
1. **真实推理服务适配器**：对接真实推理引擎，逐请求捕获到达时间戳、首字时间戳、完成状态、实际 QPS 及 TTFT/TPOT 样本；
2. **真实后台 I/O 驱动适配器**：对接 RoCE 网络传输与 NVMe SSD 存储引擎，采集物理完成字节数与目标/实测带宽；
3. **元数据全量对齐**：为三组实验生成标准化的 `run_id`、`package_id`、`config_hash` 及硬件拓扑指纹；
4. **QoS 控制器与 I/O 引擎对接**：将 `SemanticQoSController` 嵌入后台 I/O 引擎的提交函数，实现带宽预算限流；
5. **算子级事件挂载**：在前台推理框架的 `Worker.step()` 物理边界插入微秒级事件通知；
6. **网络流分类配置**：在操作系统与交换机侧完成 DSCP/CoS 到 TC0/TC1 的物理映射；
7. **全场景故障注入**：支持注入网络 Incast 拥塞、PFC 死锁及前台突发流量。

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结实验配置、环境快照与代码版本

- **操作意图**：确保三组对比测试均可追溯至唯一的源码版本、网络拓扑、硬件配置及工作负载。
- **执行命令**：

```bash
run_id="PVT-07-$(date +%Y%m%d-%H%M%S)-mixed"
result_dir="results/pvt07/${run_id}"
mkdir -p "${result_dir}"
git rev-parse HEAD > "${result_dir}/git_commit.txt"
date --iso-8601=ns > "${result_dir}/timestamp.txt"
git status --short > "${result_dir}/git_status.txt"
```

### 步骤 1：运行当前本地 DEMO 确认接口与边界

- **操作意图**：验证当前脚本的参数解析与输出落盘流程。
- **执行命令**：

```bash
cd ./原型验证代码/PVT-07
python3 mixed_workload_bench.py \
  --fg-clients 32 --bg-workers 4 --duration 5 \
  --out "../../../../results/pvt07/${run_id}/demo_no_qos.json" --seed 42
python3 mixed_workload_bench.py \
  --fg-clients 32 --bg-workers 4 --duration 5 --qos \
  --out "../../../../results/pvt07/${run_id}/demo_qos.json" --seed 42
```

### 步骤 2：审计当前控制器确认控制面真实边界

- **操作意图**：将控制器的可用接口与内部空操作进行显式取证。
- **执行命令**：

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

### 步骤 3：准备标准化三组输入 Schema（条件步骤）

- **操作意图**：确保提供给汇总评估工具的三组 JSON 均具备完整的物理来源与公平性对账字段。

### 步骤 4：运行真实纯前台黄金基线实测（条件步骤）

- **操作意图**：在无后台 I/O 干扰环境下测得同一增强代码包的物理性能基线，作为 TPOT 干扰率计算的分母。

### 步骤 5：运行原生参考实现混压实测（条件步骤）

- **操作意图**：测定开源原生参考实现在承受满载后台物理 I/O 压力时的性能表现，作为 TTFT 降幅与 QPS 提升的同场景基线。

### 步骤 6：运行统一 KV 方案 SemanticQoS 混压实测（条件步骤）

- **操作意图**：在完全相同的后台实际物理负载下，验证硬件流分类队列与应用层自适应退避对前台在线推理的保护效果。

### 步骤 7：执行三组数据汇总与门禁判决（条件步骤）

- **操作意图**：调用标准化汇总工具，基于三组真实的 `MEASURED` 数据计算三大核心指标并执行门禁判决。
- **执行命令**：

```bash
python3 run_mixed_bench.py \
  --unified-foreground ./results/unified_foreground.json \
  --mooncake-native-mixed ./results/mooncake_native_mixed.json \
  --unified-mixed ./results/unified_mixed_qos.json \
  --background-tolerance-pct 5 \
  --target-rate-rps 30 \
  --out ./results/pvt07_summary.json
```

### 步骤 8：全量证据包标准化归档

- **操作意图**：将三组测试的原始数据、硬件队列计数、QoS 事件流、CDF 曲线及环境快照统一归档。
- **执行动作**：在 `results/pvt07/<run_id>/` 目录下完整归档 `foreground_request_samples.jsonl`、`background_io_samples.jsonl`、`qos_events.jsonl`、`nic_queue_counters.jsonl`、`pvt07_summary.json`、`cdf_data.csv`、`errors.log` 及 `summary.md`。

---

## 6. 数据采集清单、核心公式与记录格式

### 6.1 混压测试标准汇总记录字段

```csv
run_id,condition,package_id,config_hash,evidence_level,hardware_profile,topology_profile,workload_id,model_id,target_rate_rps,actual_qps,completed_requests,failed_requests,p99_ttft_ms,p99_tpot_ms,bg_target_gbps,bg_bw_gbps,bg_read_ratio,planned_path,actual_path,qos_enabled,status,invalid_reason
```

### 6.2 前台逐请求原始采样字段

```csv
run_id,request_id,arrival_ts_ns,first_token_ts_ns,finish_ts_ns,output_tokens,ttft_ms,tpot_p50_ms,tpot_p99_ms,request_status,error_code,foreground_step_count
```

### 6.3 QoS 控制流转与后台 I/O 原始事件字段

```csv
run_id,step_seq,event,request_id,budget_before_bps,budget_after_bps,foreground_tpot_ms,bg_submitted_bytes,bg_completed_bytes,nic_tc0_bytes,nic_tc1_bytes,ts_ns,reason
```

### 6.4 核心对账指标计算公式

$$
\text{首字延迟降低比例 (TTFT Reduction)} = \frac{\text{TTFT}_{\text{P99}}(\text{NativeMixed}) - \text{TTFT}_{\text{P99}}(\text{UnifiedMixedQoS})}{\text{TTFT}_{\text{P99}}(\text{NativeMixed})} \times 100\%
$$

$$
\text{在线服务吞吐提升比例 (QPS Gain)} = \frac{\text{QPS}(\text{UnifiedMixedQoS}) - \text{QPS}(\text{NativeMixed})}{\text{QPS}(\text{NativeMixed})} \times 100\%
$$

$$
\text{前台长尾干扰恶化率 (TPOT Degradation)} = \frac{\text{TPOT}_{\text{P99}}(\text{UnifiedMixedQoS}) - \text{TPOT}_{\text{P99}}(\text{UnifiedForeground})}{\text{TPOT}_{\text{P99}}(\text{UnifiedForeground})} \times 100\%
$$

$$
\text{后台实测负载偏差率 (Background Load Deviation)} = \frac{\left| \text{BW}_{\text{bg}}(\text{UnifiedMixedQoS}) - \text{BW}_{\text{bg}}(\text{NativeMixed}) \right|}{\text{BW}_{\text{bg}}(\text{NativeMixed})} \times 100\%
$$

---

## 7. 候选准入门槛、判定规则与立即止损机制

### 7.1 候选工程准入门槛一览表

| 核心评估指标 | 候选工程准入门槛 | 权威对账基线与计算口径 |
|---|---:|---|
| 首字生成延迟改善 | 相对原生参考实现混压基线 **P99 TTFT 降低 $\ge 20\%$** | 相同硬件、模型、请求序列及后台负载下的 A/B 对照 |
| 在线服务吞吐提升 | 相对原生参考实现混压基线 **实际达标 QPS 提升 $\ge 10\%$** | 稳态统计窗口内实际成功完成的请求总数对账 |
| 前台长尾干扰抑制 | 相对同一增强代码包纯前台 **P99 TPOT 干扰恶化率 $< 3\%$** | 同一代码构建、同一硬件拓扑下的纯前台基线对账 |
| 后台负载对齐容差 | 两组混压测试的 **后台实测物理带宽偏差 $\le 5\%$** | 物理设备与网卡实际完成吞吐对账，确保 A/B 公平 |
| 全链路数据正确性 | 全量在线推理请求 **0 错误消费、0 内容截断损坏** | 逐请求张量 checksum 校验与独立 Oracle 对账 |

### 7.2 状态判定枚举与规则

- **GO（全链路证据闭环）**：三组真实 `MEASURED` 数据完整，后台实际负载偏差 $\le 5\%$，三大核心门限全量达标，QoS 控制事件与网卡/设备硬件计数物理吻合；
- **CONDITIONAL（局部场景或特定负载达标）**：核心门限在特定请求速率或受限后台压力下达标，但极端混压下存在波动；
- **NO-GO（性能未达标或前台严重恶化）**：前台 TPOT 干扰恶化率超出止损门限、TTFT 降幅未达标，或出现不可控的推理错误；
- **NOT-SUPPORTED（物理环境未支持）**：现场缺乏两节点集群、RoCE 网卡硬件流分类队列或可观测计数器；
- **INVALID-EVIDENCE（无效证据）**：使用模拟 DEMO 数据、三组测试条件不公平、后台实际带宽偏差超出容差或原始逐请求样本缺失。

### 7.3 立即安全止损条件

在测试过程中凡触发以下任一异常，立即终止测试并保存现场：
- 前台在线请求错误率持续攀升、推理服务端发生无响应挂起或 TPOT 尾部时延持续恶化超出预设止损红线；
- 网络发生剧烈 Incast 拥塞，导致大面积丢包重传、PFC 持续死锁暂停或网卡硬件队列非正常堆积；
- SemanticQoS 控制器在压低后台预算后发生死锁无法平稳恢复；
- 开启 QoS 保护后，系统出现比未开启 QoS 时更多且无法解释的底层硬件/通信异常；
- 流量统计显示前台数据包未正确进入 TC0 队列；
- 两组混压测试的后台实测带宽严重失配（偏差 $> 10\%$）。

---

## 8. 执行阶段划分与交付闭环

测试实施划分为四个严密的演进阶段：

| 实施阶段 | 核心攻坚内容 | 阶段必须交付物 | 准出判定条件 |
|---|---|---|---|
| E0（契约与基线规范确认） | 固化三组对比条件、Schema 结构、基线对账公式、采样窗口及证据等级 | 源码审计报告、DEMO 输出 JSON、规范化 manifest | 确认当前代码边界，杜绝将测试桩冒充实测 |
| E1（真实服务与后台 I/O 打通） | 分别接入真实大模型推理服务与后台真实 KV 搬运引擎，打通逐请求时延与带宽采样 | 逐请求时延表、后台实测吞吐表、系统资源监控 | 证实前台服务与后台 I/O 均能独立稳定运行 |
| E2（SemanticQoS 控制闭环） | 打通 `Worker.step()` 事件、控制器动态预算、后台提交队列及 TC0/TC1 网卡队列 | QoS 流转日志、网卡队列计数表、消融实验报告 | 证实软硬件双层 QoS 机制能够精准压制后台干扰 |
| E3（全链路前后台混压总门禁） | 在真实 2 节点集群下完成三组 A/B 混压测试，全量验证 TTFT、QPS、TPOT 及一致性 | 混压时间线报告、门禁判决汇总表、最终判定结论 | 证实 TTFT 降幅 $\ge 20\%$、QPS 提升 $\ge 10\%$ 且 TPOT 干扰率 $< 3\%$ |
| 条件证伪（软硬 QoS 独立贡献） | 分别单独关闭硬件队列与应用退避，证伪性能收益的技术源头与边界 | QoS 消融对照矩阵、技术边界评估报告 | 确立软硬件协同必要性，若硬件不支持则规范降级 |

---

## 9. 工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师核心职责**：
  1. 确认现场部署的 2 节点物理服务器、RoCE 网卡、交换机 QoS 队列配置及推理引擎特权；
  2. 固化模型架构、Prompt 序列分布、前台目标请求速率、后台 I/O 压力及安全止损红线；
  3. 实际启动/停止分布式推理服务、后台搬运任务及网络监控工具，完整留存交换机统计与系统 dmesg 日志；
  4. 严格审定三组测试的后台实际物理负载是否在 5% 容差内，确保 A/B 对照严密公平；
  5. 对全链路前后台混压是否达到生产准入标准承担最终技术把关责任。
- **AI Agent 协同职责**：
  1. 研读方案设计、公共测试契约及原型源码，梳理实际支持的 CLI 参数、依赖库及当前未实现特性；
  2. 编写推理服务适配层、逐请求时延解析器、QoS 动态退避流转审计、CDF 绘图及标准证据包生成工具；
  3. 严格核验三组输入的公平性对齐状态、`planned_path` 与 `actual_path` 路径一致性及状态枚举归一化；
  4. 严守技术诚信红线，严禁虚构推理请求、伪造吞吐降幅或将演示 DEMO 篡改为生产级实测。

### 9.2 可直接复制给 Coding Agent 的 Prompt 模板

```text
我正在执行 PVT-07：前后台混压端到端最小闭环（Vertical Slice）与 SemanticQoS 服务质量保障验证。

请先研读以下核心文件：
1. ./提前验证方案设计/验证计划方案设计/08_PVT-07_前后台混压端到端薄闭环与SemanticQoS干扰包络验证实施方案设计.md
2. ./提前验证方案设计/验证计划方案设计/Benchmark公共契约与证据分级规范.md
3. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-07/mixed_workload_bench.py
4. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-07/semantic_qos_controller.py
5. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-07/run_mixed_bench.py

执行约束与任务要求：
- 首先梳理源码实际支持的 CLI 参数与底层执行行为；确认 mixed_workload_bench.py 当前仅为基于 asyncio 的随机 TPOT/模拟后台吞吐 DEMO，semantic_qos_controller.py 仅实现布尔变量切换且超限分支为空，而 run_mixed_bench.py 需要完整的三组 JSON 输入并会显式拒绝 evidence_level=DEMO。
- 正式实验必须建立三组严格对齐的物理测试：同一增强代码包的纯前台黄金基线、原生参考实现混压基线、统一 KV 方案 SemanticQoS 混压组。
- 逐请求采集 TTFT、TPOT 全量样本、实际达成 QPS、后台实际物理完成字节数、网卡 TC0/TC1 硬件队列计数及控制器动态预算轨迹。
- 严格校验 A/B 公平性，两组混压的后台实测物理带宽偏差必须在 5% 容差内；区分 planned_path 与 actual_path。
- 未采集到的字段显式置为 null 并详细注明 invalid_reason；完整留存硬件异常与失败日志；将脚本状态规范归一化为公共契约枚举。
- 最终输出：源码能力核验矩阵、实际执行命令清单、三组实验性能对照表、QoS 控制流转审计表、未支持特性清单以及下一步最小代码重构建议。
```

### 9.3 常见排错指南

- **运行当前 DEMO 发现缺乏 TTFT 与 QPS 统计**：受控 DEMO 脚本仅模拟了 TPOT 与后台吞吐；需接入真实的推理服务适配器。
- **命令行传入 `--mode` 或 `--bg-gbps` 报错未知参数**：受控混流脚本尚未集成此类 CLI 参数；应按照实际支持的 CLI 参数执行。
- **运行汇总脚本提示缺少必需字段报错**：DEMO 生成的演示 JSON 与汇总脚本的 Schema 规范不匹配；需由真实的测试适配器生成全量标准 JSON。
- **汇总脚本拦截报错提示输入数据 `is DEMO`**：`run_mixed_bench.py` 内部设置了证据门禁，明确拒绝使用 DEMO 数据闭环 E3。
- **开启 QoS 后仅观测到随机分布参数发生微调**：当前 `--qos` 仅在 Python 协程内部切换了模拟分支；必须将控制器动态预算接入底层传输引擎的提交队列。
- **注入 TPOT 超限扰动后系统未发生动态退避**：排查控制器源码中超限分支是否仍为 `pass` 空操作；补齐指数退避与步长恢复的数学模型实现。
- **两组混压测试的目标带宽设定相同但实测带宽偏差巨大**：底层硬件队列调度或拥塞流控对不同数据流的处理差异导致实际吞吐失配；必须以物理完成字节数重新校准测试负载。
- **网卡硬件队列 TC0/TC1 计数器始终为 0**：排查操作系统 DSCP 标记与交换机 Priority Mapping 映射配置是否生效。
- **前台推理 TPOT 尾部时延突发剧烈恶化**：排查网络侧是否触发了 Incast 丢包、PFC 死锁暂停或 PCIe 总线竞争。
- **脚本输出的 `INVALID_EVIDENCE` 与文档状态规范不符**：在最终汇总评估层统一规范映射为标准状态 `INVALID-EVIDENCE`。
