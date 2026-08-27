# PVT-10：多流非等比性能劣化、N-to-1 Incast 与 TP=8 Coflow 偏斜实验实施方案设计
## —— 网络排队动力学与多卡协同短板的可复现实测：Incast 尾部放大、Coflow 偏斜与 Layer 0 优先级验证

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个 `run_id` 必须冻结工作负载、代码包、拓扑、并发、预热、样本量与证据等级；结果必须保留原始样本、失败请求、实际路径和结论状态。

> **验证范围声明**：当前受控工程只有 `README.md` 与 `incast_and_coflow_bench.py`，是基于 TCP（Transmission Control Protocol，面向连接的可靠传输协议）RTT（Round-Trip Time，往返时间）的 W0/DEMO 测试桩。它可以示范并发统计和 TP=8 Coflow（关联子流共同完成的传输任务）计算，但不能单独证明交换机输出缓冲区、RDMA（Remote Direct Memory Access，远程直接内存访问）/RoCE（RDMA over Converged Ethernet，基于以太网的远程直接内存访问）硬件队列或真实 NPU（神经网络处理器）计算气泡。当前脚本输出只能按 DEMO 归档，不能直接关闭 E1/E3 性能结论。

> **术语速查**：TP=8（张量并行度为 8，即一个模型计算步骤由 8 个 Rank 协同完成）；P99/P99.9（样本中 99%/99.9% 请求不超过的尾部时延分位数）；CCT（Coflow Completion Time，关联子流全部完成时间）；ACK（确认应答，接收端向发送端返回的应用层完成信号）。

> **验证 ID**：PVT-10  
> **验证名称**：多流非等比性能劣化、N-to-1 Incast（多对一突发网络拥塞，即多个发送流在短时间内汇聚到同一接收端口）与 TP=8 Coflow 偏斜实测验证
> **验证优先级**：**🔴 P0 级（评审攻坚专项）**  
> **对应验证阶段**：**E1 核心数据路径与网络观测 / E3 全链路前后台混压总门禁**
> **证伪标记**：否（网络排队动力学与多卡协同调度收益确认）
> **建议周期**：3~4 人日  
> **主关联 IR**：`IR-01-04`, `IR-01-11`, `IR-02-06`  
> **核心 SRS / SR23 锚点**：  
> - SRS：`L3-QO-SemanticQoS-045`, `L1-PD-RankConsensus-013`, `L3-TRANS-TOPO-SENSE-004`
> - SR23：`SR23-01-04-01`, `SR23-01-11-02`, `SR23-02-06-01`
> **配套源码**：[`提前验证方案设计/验证计划方案设计/原型验证代码/PVT-10_coflow_incast_bench/`](file:///d:/codes/reports/kvcache/unified_kv_memory/提前验证方案设计/验证计划方案设计/原型验证代码/PVT-10_coflow_incast_bench)

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统分布式系统视角：为什么“多流平均值正常”仍可能掩盖长尾问题

在分布式存储的 Shuffle 阶段、远程过程调用（RPC）网关或高并发文件分发系统中，多个发送端共享一个接收端口时，瓶颈通常不在单条流的平均带宽，而在多个流的到达时间是否集中、队列是否溢出以及最慢流是否拖住了上层任务。

#### 1. N-to-1 Incast 与有限缓冲区排队

- 当 $N$ 个发送端在短时间内向同一接收端口发送数据时，交换机或接收网卡的出端口队列会积累排队数据；
- **Buffer Bloat（缓冲区膨胀，即队列为吸收突发而积累过多数据，导致排队时延明显上升）**会让 P99/P99.9 尾部时延先于平均值恶化；
- 若有效到达率接近或超过瓶颈链路服务率，可能出现丢包、协议重传或接收端处理排队。是否实际发生，必须通过网卡、协议栈或交换机计数器确认，不能由一条应用层 RTT 推断。

这里的关键不是预设“并发越高必然越慢”，而是测量在给定拓扑、负载和队列配置下，尾部时延相对于单流基线的变化是否超过线性增长。

#### 2. Coflow 与木桶短板

**Coflow（关联子流共同完成的传输任务）**用于描述一个上层计算步骤所依赖的一组数据流。若一个 TP=8 计算步骤需要 8 个 Rank 的数据全部就绪，其完成时间由最慢子流决定，而不是由单流平均值决定：

$$
\mathrm{CCT}=\max(T_0,T_1,\ldots,T_7)
$$

其中 CCT（Coflow Completion Time，关联子流全部完成时间）会捕获最慢流（Straggler，即拖慢整体任务的慢流或慢节点）。因此，单流平均时延改善并不等价于多卡协同任务完成时间改善。

---

### 0.2 大模型推理中的对应物理问题

KVCache（大模型注意力键值缓存，即大模型自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免后续 Token 生成时重复计算注意力）把大模型推理中的计算与数据搬运联系起来：

1. **Prefill（首字生成预计算，即对完整 Prompt 做输入理解并生成首个输出 Token 前的计算阶段）**通常需要一次性处理较长输入，关注 TTFT（Time To First Token，首字生成延迟）；
2. **Decode（逐 Token 生成阶段，即基于历史 KVCache 反复生成后续 Token）**需要持续访问历史 KVCache，关注 TPOT（Time Per Output Token，每个输出 Token 的生成耗时）；
3. 在跨节点 KVCache 加载、PD 分离或多副本回源时，多个网络流可能汇聚到同一接收端口，形成 N-to-1 Incast；
4. 如果 8 张卡采用 TP=8（张量并行度为 8，即一个模型计算步骤由 8 个 Rank 协同完成），任一 Rank 的尾部抖动都可能进入 CCT 的最大值，导致其他 Rank 等待。

#### Layer 0 到达顺序与计算气泡

Layer 0（模型的第一个网络层）是验证“数据到达顺序是否影响计算启动”的最小语义单元。若 Layer 0 数据与后续层数据共用无优先级队列，Layer 0 不一定先于后续层到达；但“是否倒置、倒置概率和倒置造成的等待”都必须通过实际到达时间戳测量，不能直接从网络拥塞假定。

目标路径可以使用硬件 QoS（Quality of Service，服务质量，即通过优先级队列、带宽保留和流量控制保护高优先级流量）或应用层协同调度。SemanticQoS（前后台服务质量保障策略，即按 metadata-critical、TTFT prefix load、decode-critical copy、prefetch、writeback 等语义分类，映射到硬件队列并配合应用层退避）是项目拟验证的调度方式；它的收益必须由无优先级基线与目标队列配置的公平 A/B 实测确认。

```text
无语义优先级：       Layer 31 ─────┐
                                   ├─► Layer 0 到达后才能启动首层计算
                     Layer 0 ─────┘       （等待量由实测时间戳确定）

SemanticQoS 目标：    Layer 0 ──► Layer 1 ──► Layer 2 ...
                     首层更早具备计算条件；是否形成可测的气泡降低由实验确认
```

---

### 0.3 当前配套工程能够证明什么，不能证明什么

公共契约将证据分为 DEMO、LAB 和 MEASURED 三档，并将环境标记为 W0、W1、W2。本项必须把“统计公式跑通”和“真实硬件物理结论”分开归档。

| 子实验 | 当前源码能够完成的动作 | 当前源码不能直接证明的内容 | 当前默认证据状态 |
|---|---|---|---|
| N-to-1 Incast | 在一个进程中启动多个客户端线程，自动扫描 $N=1,2,4,8,16$，计算平均值、P50、P90、P99、P99.9 | 交换机输出队列深度、真实包丢失、TCP/RDMA 重传归因、跨节点链路拥塞 | `DEMO / W0` |
| TP=8 Coflow | 启动 8 个本地 TCP 接收端口，按 8 个 Rank 的 RTT 计算 CCT，并对 Rank 7 注入代码内延迟 | 真实 TP=8 NPU 计算、集合通信等待、真实 QoS 队列和真实网络抖动 | `DEMO / W0` |
| Layer 0 优先级 | 当前没有独立 Layer 0 发送、优先级配置或 NPU 事件采集接口 | Layer 0 到达位次、计算流水气泡、硬件 QoS 前后差异 | `NOT-SUPPORTED` |
| 双节点/真实 RoCE | 当前脚本没有远端 Server 单独启动模式，也没有 RDMA/RoCE 数据路径 | 代表性跨节点 Incast、交换机计数器和硬件队列行为 | `NOT-SUPPORTED`，除非先完成工程扩展 |

因此，本方案保留版本一的 Incast、Coflow 和 Layer 0 三组验证目标，但明确当前 Python 测试桩只能作为流程示范。任何 W0 输出都不得写成“交换机发生丢包”“QoS 消除了气泡”或“TP=8 已达到生产级结论”。

---

## 1. 验证目标与交付结论定义

### 1.1 待验证核心命题

1. **命题一：N-to-1 Incast 可能呈现非线性尾部放大**。在相同拓扑、数据块大小和请求节奏下，扫描 $N=1,2,4,8,16$，比较 P50、P90、P99、P99.9 与单流基线；若高并发下尾部相对于线性参考继续放大，则说明该配置存在需要流控或拓扑治理的排队风险。
2. **命题二：TP=8 Coflow 的 CCT 由最慢 Rank 决定**。在每组 8 个子流的条件下，计算各流平均时延、CCT P50/P99 和偏斜系数，判断单流指标是否低估多卡协同等待。
3. **命题三：Layer 0 优先级可能降低首层等待**。在真实可配置 QoS 队列和 NPU 计算事件可用时，对比无优先级与 Layer 0 高优先级两条路径，记录到达倒置、首层就绪时间和计算气泡；当前源码不具备该验证条件，应先标记 `NOT-SUPPORTED`。

### 1.2 交付物与结论边界

每一轮正式结果至少交付：

1. 《N-to-1 Incast 缓冲区排队与尾部时延膨胀表》：包含并发度、块大小、负载、各分位数、失败请求和路径证据；
2. 《TP=8 Coflow 偏斜系数与 CCT 实测表》：包含 8 个 Rank 的独立完成事件、CCT、最慢 Rank、扰动参数和重复实验离散度；
3. 《Layer 0 优先级前后到达顺序与计算气泡对比表》：仅在真实硬件队列与 NPU 事件可采集时形成；
4. `manifest.json`、`environment.json`、原始事件、汇总 CSV、日志和结论状态；
5. 按子实验分别输出 `GO`、`CONDITIONAL`、`NO-GO`、`NOT-SUPPORTED` 或 `INVALID-EVIDENCE`，不把不同证据等级的结果合并为一个总成绩。

---

## 2. 实验方案与测试矩阵设计

### 2.1 三组子实验矩阵

| 序号 | 实验子项 | 正式拓扑与参数 | 核心观测指标 | 当前源码覆盖 |
|---:|---|---|---|---|
| 1 | **N-to-1 Incast：缓冲区排队与尾部时延放大** | 1 个 Receiver；$N=1,2,4,8,16$；块大小 64KB、128KB、1MB；无背景流与受控背景流分别测量 | 平均时延、P50/P90/P99/P99.9、$R_{99}(N)=P99(N)/P99(1)$、失败请求、丢包/重传计数、队列深度 | 支持 $N=1,2,4,8,16$ 和单一 `--chunk_kb`；不支持真实队列/丢包计数和结果文件输出 |
| 2 | **TP=8 Coflow：木桶短板与多组并发偏斜** | 每组 8 个绑定 Rank；TP=8；并发 Coflow 组数 $M=1,2,4$；块大小 64KB、128KB、1MB；干净、合成扰动、真实混压三种场景 | 单流平均、CCT P50/P99、最慢 Rank、$\mathrm{Skew}=CCT/T_{avg}$、组间干扰、失败/重试 | 支持 8 个本地端口和两种内置场景；`--requests` 表示每个 Rank 的请求数，不等于 $M$；不支持真实 TP/NPU 和 QoS A/B |
| 3 | **Layer 0：起始首层排队倒置与计算气泡** | Layer 0~Layer 31；背景大流量；无优先级 FIFO vs 硬件 QoS 高优先级；至少 3 次独立重复 | Layer 0 到达延迟与到达位次、倒置比例、首层计算开始时间、气泡 P50/P99、前台 TPOT 影响 | 当前没有实现；无硬件 QoS 或 NPU 事件时记录 `NOT-SUPPORTED` |

### 2.2 环境与对照矩阵

| 环境 | 目的 | 最低条件 | 可形成的结论 |
|---|---|---|---|
| W0 单机回环 | 验证线程并发、统计口径、CCT 计算和失败归档流程 | Python 3、NumPy、localhost TCP | 仅能形成 `DEMO` 工作流结论 |
| W1 两节点或多网卡局部实测 | 观察有限设备组合下的 RTT、带宽和局部队列行为 | 明确的 Receiver/Client 角色、节点地址、网卡计数器和拓扑记录 | 形成绑定设备与拓扑的 `LAB` 局部结论 |
| W2 代表性跨节点系统验证 | 支撑 E3 前后台混压和真实多卡结论 | 真实 NPU、网卡/RDMA、交换机 QoS、NPU 事件和完整路径证据 | 满足证据完整性后才可形成 `MEASURED` 结论 |

同一组 A/B 必须冻结设备、拓扑、模型布局、请求序列、并发、背景流、资源配额、编译参数、预热轮数和统计口径。仅把输出标签从“无 QoS”改成“有 QoS”而不改变实际队列或代码配置，结果无效。

### 2.3 运行前冻结的参数

```text
run_id, workload_schema_version, workload_id, seed,
package_id, baseline_commit, config_hash, feature_state,
hardware_profile, topology_profile, evidence_environment,
evidence_level, sender_counts, coflow_groups, tp_degree,
chunk_sizes, warmup_rounds, measure_rounds, background_load,
queue_class, planned_path
```

参数约束：

- `sender_counts` 至少包含 `1,2,4,8,16`；
- `chunk_sizes` 至少覆盖 `64KB,128KB,1MB`。当前脚本一次只能接收一个 `--chunk_kb`，需要分多轮运行；
- `coflow_groups` 与 `requests` 分开记录。当前脚本没有 `--groups` 参数，不能把 `--requests 4` 解释为 4 组 Coflow；
- P99.9 需要足够的原始样本。若某条件有效样本少于 1000，不能把插值结果当作稳定 P99.9，建议改为 `null` 并说明 `invalid_reason`；
- 默认至少 1 轮预热、3 轮独立重复。跨节点时间差必须记录同步方式和误差上限。

---

## 3. 实验动力学模型与统计口径

### 3.1 N-to-1 Incast 队列时延模型

设瓶颈链路服务率为 $\mu$，单个流的有效到达率为 $\lambda$，并发流数为 $N$，则总到达率近似为：

$$
\Lambda=N\cdot\lambda, \qquad \rho=\frac{\Lambda}{\mu}
$$

当利用率 $\rho$ 接近 1 时，有限队列中的等待时间对突发和服务抖动更敏感。设瓶颈处一次有效排队数据量为 $Q(N)$ 字节、服务率为 $\mu$ 字节/秒，则排队时间可用下式作解释性近似：

$$
T_{queue}(N)\approx\frac{Q(N)}{\mu}
$$

为保留版本一中对突发与重传的拆解，可使用以下解释模型，但不能用它替代原始样本：

$$
T_{P99}(N)=T_{tx}+\frac{B}{\mu}\cdot f_{burst}(N)+T_{retransmit}\cdot P_{loss}(N)
$$

其中：

- $B$ 是瓶颈队列中可观测或估算的有效排队字节数；
- $f_{burst}(N)$ 是待由实验拟合的突发放大因子；
- $P_{loss}(N)$ 和 $T_{retransmit}$ 只有在协议栈、网卡或交换机提供相应计数时才可填写；
- 应用层 TCP RTT 只能给出端到端完成时间，不能自动区分发送、排队、接收处理和重传原因。

每个并发度计算尾部膨胀比：

$$
R_{99}(N)=\frac{P_{99}(N)}{P_{99}(1)}, \qquad A_{99}(N)=\frac{R_{99}(N)}{N}
$$

当 $A_{99}(N)>1$ 时，表示该测量点相对于简单线性参考出现了超线性趋势；若要声称“远大于线性增长”，必须给出重复实验离散度、置信区间、样本量和对应硬件计数器，不能只看一条曲线。

### 3.2 TP=8 Coflow 完成时间与极值放大

第 $j$ 个 Coflow 的 8 个 Rank 完成时间为 $T_{j,0},\ldots,T_{j,7}$：

$$
CCT_j=\max_{k\in[0,7]}T_{j,k}
$$

单个 Coflow 的平均流耗时为：

$$
T_{avg,j}=\frac{1}{8}\sum_{k=0}^{7}T_{j,k}
$$

偏斜系数定义为：

$$
Skew_j=\frac{CCT_j}{T_{avg,j}}, \qquad Skew_{P99}=\frac{P99(CCT)}{mean(T_{j,k})}
$$

在子流独立同分布的简化假设下，CCT 的分布满足 $F_{CCT}(t)=F_T(t)^8$；真实网络中的共享队列、流量整形和相关抖动会破坏独立性，所以正式报告要同时给出 8 个 Rank 的原始时间序列，不能只引用极值统计公式。多个 Coflow 组 $M=1,2,4$ 时，还需比较组间交织带来的尾部变化。

### 3.3 Layer 0 到达倒置与计算气泡

设 Layer $i$ 的实际到达时间为 $t_i$，Layer 0 的到达位次为：

$$
rank_0=1+\left|\{i\in[1,31]:t_i<t_0\}\right|
$$

若需要计算气泡，必须同时采集 Layer 0 可计算事件 $t_{layer0\_ready}$ 与 NPU 首层计算开始事件 $t_{compute\_start}$：

$$
T_{bubble}=\max\left(0,\;t_{compute\_start}-t_{layer0\_ready}\right)
$$

`T_bubble=0` 只有在实际事件时间戳和测量分辨率支持时才可记录；“QoS 后气泡归零”是待验证结果，不是输入数据或预置结论。

---

## 4. 测试工具、当前实现边界与正式扩展要求

### 4.1 当前受控工程目录与运行方式

当前受控目录为：

```text
原型验证代码/PVT-10_coflow_incast_bench/
├── README.md
└── incast_and_coflow_bench.py
```

当前脚本没有 C++ 文件、Makefile、`--out` 参数、独立 `coflow_bench`、`layer_priority_bench` 或 `plot_dynamics.py`。因此，版本二中下列命令不是当前工程可执行命令，不应继续写入正式 SOP：

```text
./incast_bench ...
./coflow_bench ...
./layer_priority_bench ...
python3 ./plot_dynamics.py ...
```

当前 W0 运行命令为：

```bash
cd ./原型验证代码/PVT-10_coflow_incast_bench
python3 ./incast_and_coflow_bench.py --requests 100 --chunk_kb 64
```

命令行参数（CLI，即命令行接口）只有：

```text
--ip       客户端连接目标地址，默认 127.0.0.1
--port     本地接收端口基值，默认 18800
--chunk_kb 每个请求的数据块大小，默认 64KB
--requests 每条流的请求数，默认 200
```

### 4.2 源码实际行为审计

| 代码路径 | 实际行为 | 证据影响 |
|---|---|---|
| `run_incast_test` | 内部固定扫描 `[1, 2, 4, 8, 16]`；每个并发度使用一个新的本地端口；客户端线程并发建立 TCP 连接 | 可验证并发统计流程，但不能单凭结果推断交换机队列行为 |
| `receiver_server` | 每个接收端口的服务线程按连接顺序处理请求，没有为每个连接建立独立接收 Worker | 多个客户端虽同时发起，应用层 Receiver 仍可能串行处理；正式 Incast 需补充并发接收和硬件计数器 |
| `client_sender` | 使用 `time.perf_counter()` 测量从 `sendall` 到 ACK 返回的本地 RTT；失败时退出当前流，未单独输出失败事件 | 当前 P99 是应用层往返完成时间，不是纯网络排队时间；失败请求不能被无声丢弃 |
| `run_tp8_coflow_test` | 使用 `base_port+0` 到 `base_port+7` 模拟 8 个 Rank，计算 CCT P50/P99 和 `CCT P99 / 单流平均` | 可示范最大值统计；不等于真实 TP=8 NPU 或集合通信 |
| Rank 7 扰动场景 | 当前代码实际使用 `delay_inject_prob=0.15`、`delay_ms=0.2`，即 15% 概率注入 200µs 延迟；源码注释中的 2% 与此不一致 | 这是合成扰动，不是网络丢包或 QoS 证据；运行前必须以源码实际值为准并记录版本 |
| 输出 | 仅打印文本，没有 `raw_events.jsonl`、CSV、`manifest.json` 或绘图脚本 | 需要人工或 Agent 依据公共契约归一化结果；未归一化前只能保留 DEMO 日志 |

`--ip` 只改变客户端的连接目标。脚本仍在当前进程本地绑定接收端，并没有通过 SSH 或其他机制在远端自动启动 Server。因此，仅在 Node A 执行 `--ip <Node B>` 不能构成有效的双节点测试；若 Node B 没有独立 Receiver，结果应记为连接失败或 `NOT-SUPPORTED`，不能写成跨节点实测。

### 4.3 面向 LAB/MEASURED 的最小工程扩展

在进入真实硬件结论前，至少补齐以下能力：

1. **Server/Client 角色分离**：提供独立 Receiver 启动参数和健康检查，明确监听地址、端口、节点角色和拓扑；
2. **并发接收模型**：为每条连接建立独立 Worker 或等价并发模型，避免把 Receiver 单线程排队误当作交换机 Incast；
3. **原始事件输出**：每条请求保留 `send_start_ns`、`payload_bytes`、`ack_end_ns`、`status`、`flow_id`、`rank_id`、`coflow_id` 和错误码；
4. **协议与硬件计数器**：记录 TCP/RDMA 重传、网卡丢包、队列深度、带宽、交换机端口计数器和 QoS 队列类别；采集不到的字段使用 `null` 与 `invalid_reason`；
5. **多组 Coflow 与真实 A/B**：显式实现 $M=1,2,4$，并把 Fair-share（各流只按通用公平策略共享带宽）与 SemanticQoS 作为实际不同配置运行；
6. **Layer 0 事件链**：在数据描述符中带上 `layer_id`，在接收端记录到达顺序，并与 NPU Layer 0 ready/compute 事件关联；
7. **跨节点计时**：优先使用同一节点单调时钟测量单向事件；必须跨节点相减时，记录 PTP（Precision Time Protocol，精密时间协议）或其他同步方式及误差上限；
8. **统计与失败保留**：输出原始样本、失败样本、重复实验汇总、置信区间或等价离散度，禁止只打印成功请求的平均值。

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结实验身份、证据等级和当前工程范围

- **操作意图**：先确定本轮是 W0/DEMO、W1/LAB 还是 W2/MEASURED，防止把本地回环输出误写成跨节点硬件结论。
- **执行动作**：填写 `run_id`、`baseline_commit`、`hardware_profile`、`topology_profile`、`evidence_level`、`sender_counts`、`coflow_groups`、`chunk_sizes`、`warmup_rounds` 和 `measure_rounds`；检查源码树是否仍只有 Python 测试桩。
- **应观察现象**：能够明确列出本轮实际测试的路径、未覆盖字段和预期输出文件；若 `Layer 0`、硬件 QoS 或远端 Receiver 缺失，提前标为 `NOT-SUPPORTED`。

### 步骤 1：运行 W0 单机回环基线

- **操作意图**：验证端口监听、客户端线程、请求编号、ACK 闭环和 P50/P99 统计是否能够稳定运行，建立后续排错基线。
- **执行命令**：

```bash
cd ./原型验证代码/PVT-10_coflow_incast_bench
python3 ./incast_and_coflow_bench.py --requests 100 --chunk_kb 64
```

- **应观察现象**：输出 Incast 的 $N=1,2,4,8,16$ 行，并随后输出干净场景和 Rank 7 合成扰动场景的 TP=8 CCT 统计。当前输出是标准输出文本，不是 CSV；运行日志必须原样保存。
- **判定边界**：本步骤只能证明 W0/DEMO 流程可运行，不能证明非线性网络拥塞、真实丢包或 QoS 收益。

### 步骤 2：按块大小扫描 N-to-1 Incast

- **操作意图**：恢复版本一中的完整参数网格，观察块大小和并发度变化对尾部指标的影响，避免只用单一 64KB 或 128KB 样本得出泛化结论。
- **当前脚本执行方式**：脚本会在一次运行中自动扫描 $N=1,2,4,8,16$，不能通过 CLI 只选择某一个 N。对每个块大小单独运行，并为每轮使用独立 `run_id`：

```bash
python3 ./incast_and_coflow_bench.py --requests 1000 --chunk_kb 64  > raw_stdout_64k.txt  2>&1
python3 ./incast_and_coflow_bench.py --requests 1000 --chunk_kb 128 > raw_stdout_128k.txt 2>&1
python3 ./incast_and_coflow_bench.py --requests 1000 --chunk_kb 1024 > raw_stdout_1m.txt   2>&1
```

- **应观察现象**：每个 N 至少有平均值、P50、P90、P99 和 P99.9；同时记录是否有连接失败、流提前退出或结果缺失。`--requests 1000` 只是满足 P99.9 最低样本量的起点，正式报告仍应按有效样本数和重复轮次判断稳定性。
- **证据边界**：当前脚本的 RTT 包含 TCP 栈、Receiver 处理和 ACK 等开销，且没有交换机计数器；因此本步骤输出只能作为 W0/DEMO，不能直接填充 `packet_loss_count` 或 `retransmit_count`。

### 步骤 3：测量 TP=8 Coflow 最大值效应

- **操作意图**：验证 8 条关联子流的完成时间必须取最大值，并观察合成慢流如何抬高 CCT，而不是只看单流平均值。
- **执行方式**：步骤 2 的脚本运行结束后会自动执行 TP=8 Coflow；当前脚本使用 `--requests` 作为每个 Rank 的 Coflow 请求数，并自动运行两个场景：干净场景，以及只对 Rank 7 注入 15% 概率、200µs 延迟的合成扰动场景。
- **应观察现象**：记录 8 个 Rank 的有效样本数量、单流平均、CCT P50、CCT P99 和 `CCT P99 / 单流平均`。若某个 Rank 样本缺失，不能用 0 补齐 CCT，必须记录 `null` 并填写 `invalid_reason`。
- **正式扩展**：$M=1,2,4$ 组并发、Fair-share vs SemanticQoS 和真实背景流量不在当前 CLI 中，需完成第 4.3 节扩展后再执行；不能用 `--requests 4` 代替 4 组 Coflow。

### 步骤 4：跨节点或真实 RoCE 验证前的角色检查

- **操作意图**：避免把 `--ip` 参数误解为“自动在远端启动接收端”。
- **执行动作**：确认远端 Receiver 已通过独立进程或扩展后的 Server 模式启动，确认监听端口、节点地址、防火墙、网卡、交换机端口和计数器均已记录；确认 Client 端的 `planned_path` 与 `actual_path` 一致。
- **应观察现象**：能够从 Receiver 日志和 Client 原始事件中对应同一 `run_id`、`request_id`、`flow_id`；跨节点时间差有同步误差说明。
- **无法满足时**：停止该子实验并记录 `NOT-SUPPORTED` 或 `INVALID-EVIDENCE`，不要用本地回环数据替代跨节点结果。

### 步骤 5：执行 Layer 0 与硬件 QoS A/B（条件步骤）

- **前置条件**：有可配置的无优先级与高优先级队列；数据事件带 `layer_id`；有 NPU Layer 0 ready/compute 时间戳；能够读取网卡/交换机 QoS 队列计数器。
- **操作意图**：在同一背景流和同一 payload 下，比较 Layer 0 到达位次、首层就绪和计算气泡，判断 SemanticQoS 是否提供净收益。
- **A/B 要求**：只改变队列类别或调度策略，保持设备、拓扑、请求序列、并发、背景带宽、预热和统计口径一致；不能只修改输出标签。
- **应观察现象**：记录 Layer 0 是否被后续层超前、倒置比例、`T_bubble` P50/P99、前台 TPOT 变化、丢包/重传和实际队列计数。若硬件能力或 NPU 事件缺失，标记 `NOT-SUPPORTED`，不记录“气泡为 0”。

### 步骤 6：生成标准证据包并进行重复实验对账

- **操作意图**：把标准输出从一次性终端演示转化为可复核证据，保留失败样本和环境信息。
- **执行动作**：按 `results/PVT-10/<mode>/<run_id>/` 建立目录，归档 `manifest.json`、`environment.json`、原始日志或 `raw_events.jsonl`、`raw_metrics.*`、`summary.csv`、`summary.json` 和 `logs/`；运行至少 3 轮独立重复，计算 P50/P95/P99、P99.9、置信区间或等价离散度。
- **应观察现象**：所有摘要字段都能回指原始样本，`planned_path`、`actual_path`、`evidence_level`、`status` 和 `invalid_reason` 齐全；任何缺失字段使用 `null`，不使用 0 伪造“无时延”或“无错误”。

---

## 6. 数据采集清单与记录格式

### 6.1 PVT-10 专用原始事件字段

公共事件字段之外，本项至少记录：

```text
run_id, validation_id, subtest, mode,
request_id, coflow_id, flow_id, rank_id, layer_id,
sender_count_n, coflow_groups_m, tp_degree,
chunk_size_bytes, payload_bytes,
send_start_ns, send_end_ns, ack_end_ns,
latency_ns, cct_ns, arrival_seq,
queue_class, background_load,
packet_loss_count, retransmit_count,
planned_path, actual_path, status, error_code,
evidence_environment, evidence_level, package_id,
config_hash, hardware_profile, topology_profile,
invalid_reason
```

字段约束：

- `cct_ns` 只有在同一 `coflow_id` 的 8 个 Rank 完成事件齐全时填写；
- `layer_id`、`packet_loss_count`、`retransmit_count`、`queue_class` 等当前脚本不能采集的字段填写 `null`，并说明原因；
- `status` 使用 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE`；
- 应用层 ACK 成功不等于硬件网络无丢包。没有协议栈/网卡/交换机计数器时，不能把 `packet_loss_count` 写成 0；
- 合成 Rank 7 延迟必须单独记录 `fault_model=synthetic_delay`，不能标成 `packet_loss` 或真实网络故障。

### 6.2 汇总 CSV 模板

以下是字段模板，不是性能成绩：

```csv
validation_id,run_id,subtest,mode,sender_count_n,coflow_groups_m,tp_degree,chunk_size_bytes,sample_count,avg_us,p50_us,p90_us,p99_us,p99_9_us,cct_p50_us,cct_p99_us,skew_p99,layer0_arrival_rank,bubble_p50_us,bubble_p99_us,packet_loss_count,retransmit_count,evidence_level,status,invalid_reason
<PVT-10>,<run_id>,<incast>,<tcp_loopback_or_cross_node>,<N>,<null>,<null>,<bytes>,<count>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<null>,<null>,<null>,<null>,<null>,<null>,<null>,<null>,<DEMO_OR_LAB_OR_MEASURED>,<status>,<null_or_reason>
<PVT-10>,<run_id>,<coflow>,<fair_share_or_semantic_qos>,<null>,<M>,<8>,<bytes>,<count>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<LAB_OR_MEASURED>,<status>,<null_or_reason>
<PVT-10>,<run_id>,<layer0>,<fifo_or_hardware_qos>,<null>,<null>,<8>,<bytes>,<count>,<null>,<null>,<null>,<null>,<null>,<null>,<null>,<null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<LAB_OR_MEASURED_OR_NOT-SUPPORTED>,<status>,<null_or_reason>
```

### 6.3 证据包目录

```text
results/PVT-10/<mode>/<run_id>/
├── manifest.json
├── environment.json
├── raw_events.jsonl          # 当前 W0 可用原始 stdout，正式扩展应改为结构化事件
├── raw_metrics.*
├── summary.json
├── summary.csv
└── logs/
```

`manifest.json` 至少记录代码包、基线 Commit、配置哈希、输入 Schema、设备与拓扑、执行命令、原始文件哈希、证据等级、支持范围、未支持项和结论状态。

---

## 7. GO / CONDITIONAL / NO-GO / NOT-SUPPORTED / INVALID-EVIDENCE 判定规则

### 7.1 命题一：Incast 尾部放大

- **GO（命题得到当前环境支持）**：至少 3 次独立重复；$N=1,2,4,8,16$ 和至少两种块大小均有完整原始样本；在声明的高并发区间观察到 $R_{99}(N)$ 相对线性参考的超线性趋势，且实际路径、失败请求和硬件/协议计数器能够闭环。该结论只适用于记录的硬件和拓扑。
- **CONDITIONAL（局部支持）**：仅 W0/LAB 观察到尾部变化，或缺少交换机/协议计数器，结论限定为“该测试桩/局部设备上的 RTT 尾部趋势”，不外推为生产网络结论。
- **NO-GO（当前假设未被支持）**：在有效的正式对照中没有观察到预期尾部变化，或新增流控后 P99/P99.9、失败率或重传显著恶化；应回到队列、负载和流量模型检查，不预先保留原结论。

### 7.2 命题二：TP=8 Coflow 偏斜

- **GO（Coflow 统计和协同路径达到门限）**：8 个 Rank 的完成事件完整，CCT 与 `Skew` 可复算；在正式 Fair-share vs SemanticQoS A/B 中，候选目标为 `Skew_P99 < 1.15`，且前台失败率、丢包/重传和 TPOT 不出现不可接受退化。`1.15` 是运行前必须写入 manifest 的候选门限，不是当前脚本的预置结果。
- **CONDITIONAL（统计闭环但路径受限）**：W0 合成扰动能够展示最大值放大，或 LAB 仅覆盖部分 Rank/部分背景流；结论仅限于已测场景，不得写成真实 TP=8 生产收益。
- **NO-GO（协同调度没有净收益）**：真实 A/B 中 CCT 尾部未改善，或为压低 CCT 引入了更高的丢包、重传、前台 TPOT 退化或一致性问题。

### 7.3 命题三：Layer 0 优先级与计算气泡

- **GO（真实事件链闭环）**：存在实际硬件队列和 NPU Layer 0 事件；Layer 0 到达位次、首层计算开始时间、气泡分布、前台 TPOT 和队列计数器全部可回溯；QoS 路径相对无优先级基线达到运行前冻结的改善门限。版本一中的“Layer 0 传输延迟降低 ≥50%”可作为候选门限，但气泡是否为 0 必须由实测决定。
- **CONDITIONAL（只有局部证据）**：只测到网络到达时间，未测到 NPU 计算事件，或只在 W0 中进行顺序模拟；只能说明到达顺序趋势，不能关闭计算气泡结论。
- **NOT-SUPPORTED**：当前 Python 测试桩没有 Layer 0/QoS/NPU 事件接口，或现场没有相应硬件能力。
- **NO-GO（QoS 代价超过收益）**：高优先级策略没有降低首层等待，或挤压前台/其他关键流导致 P99、丢包、重传或业务 TPOT 变差。

### 7.4 统一无效证据规则

以下任一情况将对应子实验标为 `INVALID-EVIDENCE`，不得输出 `GO`：

- 使用版本二中的不存在命令、固定示例数据或未执行的 QoS 标签作为结果；
- `planned_path` 与 `actual_path` 无法证明，或把 TCP RTT 当成 RDMA/RoCE 硬件排队时间；
- 缺少原始样本、失败请求、设备/拓扑、版本清单、统计参数或重复实验信息；
- 8 个 Rank 不完整却计算 CCT，或用 0 填充缺失字段；
- 合成延迟被标成真实丢包、真实拥塞或硬件故障；
- A/B 没有实际切换队列、代码包或配置，或者设备、负载、资源配额不等价。

---

## 8. 执行阶段与交付闭环

版本一中的“Day 1~Day 3”信息保留为以下三个实施阶段；阶段名称用于组织工作，不把日期当作性能承诺：

| 阶段 | 工作内容 | 必须交付 | 退出条件 |
|---|---|---|---|
| 阶段 A：工具与基线 | 审计源码、确认实际 CLI、完成 W0 回环和失败归档 | 源码审计记录、运行日志、参数清单、`DEMO` manifest | 命令可复现，已列明不支持项 |
| 阶段 B：参数与硬件实测 | 扫描 $N=1..16$、64KB/128KB/1MB，完成 TP=8 CCT；具备条件时扩展 $M=1,2,4$ 与 Layer 0 A/B | 原始事件、P50/P90/P99/P99.9、CCT、Skew、计数器和重复实验汇总 | 每个结论字段可回指原始样本，证据等级明确 |
| 阶段 C：标准证据包 | 形成 Incast、Coflow、Layer 0 三张对账表和分项判定 | `manifest.json`、`environment.json`、原始数据、摘要、图表或明确的未支持说明 | 通过公共契约校验，无无效字段冒充实测 |

本项的最终作用是为 SemanticQoS 队列配置、TP 多卡协同下发和前后台混压门禁提供实测依据；它不能用 W0 回环数据代替 E3 全链路混压，也不能把“观察到 CCT 最大值”直接等同于某一硬件方案已经准入。

---

## 9. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师负责**：
  1. 确认被测设备、节点角色、网络拓扑、队列配置和证据等级；
  2. 冻结实验参数并执行命令，保存原始 stdout、失败请求和环境信息；
  3. 判断某一字段是否真的被设备或协议计数器观测到，决定 `null`、`NOT-SUPPORTED` 或 `INVALID-EVIDENCE`；
  4. 对正式硬件结论承担现场复核责任。
- **AI Agent 负责**：
  1. 先阅读当前目录中的 README 和实际源码，列出真实 CLI、端口、统计字段和未实现功能；
  2. 编写结构化日志解析、统计复算、重复实验对账和证据包生成工具；
  3. 检查 `CCT=max(T0..T7)`、$R_{99}(N)$、`Skew` 和缺失字段处理是否符合契约；
  4. 不凭空创建 `incast_bench.cc`、`coflow_bench`、`layer_priority_bench` 或固定“实测”数据，不替工程师宣布硬件路径已经通过。

### 9.2 可直接复制给 Coding Agent 的 Prompt 模板

```text
我正在执行 PVT-10：多流 N-to-1 Incast 与 TP=8 Coflow 偏斜验证。

请先阅读：
1. ./提前验证方案设计/验证计划方案设计/12_PVT-10_多流非等比劣化与TP8_Coflow偏斜实验实施方案设计.md
2. ./提前验证方案设计/验证计划方案设计/Benchmark公共契约与证据分级规范.md
3. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-10_coflow_incast_bench/README.md
4. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-10_coflow_incast_bench/incast_and_coflow_bench.py

约束：
- 先列出源码实际支持的命令、端口、参数和输出字段；不要假设存在 C++、Makefile、--out、Layer 0 独立工具或远端自动启动 Server。
- 将当前脚本结果标为 DEMO/W0；不能把 TCP RTT、合成 Rank 7 延迟或固定示例写成真实交换机丢包、RoCE QoS 或 NPU 气泡实测。
- 设计一个解析器，把 stdout 和后续结构化事件归一为公共契约字段；缺失字段使用 null 并填写 invalid_reason，禁止用 0 补齐。
- 对 Incast 复算 P50/P90/P99/P99.9、R99(N)=P99(N)/P99(1)；对 Coflow 复算 CCT=max(T0..T7) 和 Skew；保留失败请求。
- 把 N=1,2,4,8,16、块大小 64KB/128KB/1MB、TP=8、M=1/2/4 的“当前支持”和“需要扩展”分开列出。
- 最后输出：源码能力矩阵、运行命令、字段字典、统计复算结果、证据等级、未支持项和下一步最小代码改动建议。
```

### 9.3 常见排错指南

- **启动即提示 `No module named 'numpy'`**：当前脚本在解析命令行前就导入 NumPy；先在目标 Linux 测试环境准备与项目 Python 版本匹配的 NumPy，并在 manifest 中记录版本。依赖未满足时不要把“未运行”记成性能失败。
- **`Connection refused` 或端口被占用**：检查当前进程是否仍持有 18800~18804 或 Coflow 使用的 18900~18907 端口；确认 Receiver 是否在本机启动。不要仅修改 `--ip` 就声称已经完成双节点联调。
- **P99.9 波动很大**：检查每个条件的有效样本数。少于 1000 个样本时，P99.9 不稳定；增加 `--requests`、独立重复轮次，并保留原始样本。
- **高并发时某个流提前结束**：当前 `client_sender` 在异常后退出该流，必须记录失败原因和有效样本数；不能只合并成功 RTT。
- **无法解释“丢包/重传”**：TCP ACK 只说明应用层收到 ACK。使用协议栈、网卡和交换机可用计数器核对；采集不到时填 `null`，不要填 0。
- **合成扰动与代码注释不一致**：以当前源码参数 `0.15` 和 `0.2ms` 为准，运行前在 manifest 中记录；若要使用 2% 参数，先修改源码并更新 `baseline_commit/config_hash`，不能在报告中凭文字改写。
- **脚本输出没有 CSV**：这是当前工程现状。先保存 stdout，再由 Agent 编写解析器；不要引用版本二中不存在的 `plot_dynamics.py` 或 `--out` 参数。
- **跨节点时间无法直接相减**：记录 PTP 或其他同步方式及误差上限；无法证明时间基准时，只使用同一节点 RTT 或将跨节点字段标为无效。
- **想验证 Layer 0 气泡但没有 NPU 事件**：把该子实验标为 `NOT-SUPPORTED`，先补事件链和实际 QoS A/B；不要以应用层打印顺序代替计算开始时间。
