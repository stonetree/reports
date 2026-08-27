# PVT-10：多流非等比性能劣化、N-to-1 Incast 与 TP=8 Coflow 偏斜实验实施方案设计
## —— 网络排队动力学与多卡协同短板实测验证：Incast 尾部爆炸、Coflow 偏斜与 Layer 0 气泡消除

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。实验必须同时记录平均耗时、P99/P99.9 尾部时延、各流实际到达时序与丢包重传统计。

> **验证 ID**：PVT-10  
> **验证名称**：多流非等比性能劣化、N-to-1 Incast 突发与 TP=8 Coflow 偏斜实测验证  
> **验证优先级**：**🔴 P0 级（评审攻坚专项）**  
> **对应验证阶段**：**E1/E3 网络排队动力学与多卡协同**  
> **证伪标记**：否（网络物理规律与协同调度收益确认）  
> **建议周期**：3~4 人日  
> **主关联 IR**：`IR-01-04`, `IR-01-11`, `IR-02-06`  
> **核心 SRS / SR23 锚点**：  
> - SRS: `L3-QO-SemanticQoS-045`, `L1-PD-RankConsensus-013`, `L3-TRANS-TOPO-SENSE-004`  
> - SR23: `SR23-01-04-01`, `SR23-01-11-02`, `SR23-02-06-01`  
> **配套源码**：[`提前验证方案设计/验证计划方案设计/原型验证代码/PVT-10_coflow_incast_bench/`](file:///d:/codes/reports/kvcache/unified_kv_memory/提前验证方案设计/验证计划方案设计/原型验证代码/PVT-10_coflow_incast_bench)  

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统系统开发视角：网络 Incast 拥塞与分布式木桶短板效应

在分布式网络通信与数据中心网络（如 HDFS / MapReduce Shuffle 阶段的 Straggler 慢节点、高并发 RPC 网关）中：

#### 1. N-to-1 Incast 缓冲区排队爆炸（Buffer Bloat）
- 当 $N$ 个发送端在同一个微秒时刻并发向单一接收端发送大批数据包时，交换机出端口的物理缓冲区（Switch Output Buffer）会被瞬间填满；
- 后续数据包只能排在长长的硬件队列后方，导致**单包排队时延从 $10\mu s$ 飙升到数毫秒（长尾延迟呈现非等比急剧恶化，恶化倍数远超 $N$ 倍）**，甚至触发交换机丢包与 TCP/RDMA 重传风暴！

#### 2. Coflow 协同完成时间（极值分布与木桶效应）
- 在分布式多机多卡计算中，一个计算步骤往往需要属于同一个作业的全部 $K$ 条数据流都传输完毕后才能启动（$\text{CCT} = \max(T_1, T_2, \dots, T_K)$）；
- **数学统计规律**：哪怕单条流的平均延迟很低（如 1ms），但随着并发流数量 $K$ 的增加，极值 $\max(\cdot)$ 会不可避免地捕获到最慢的那条流（Long Tail Straggler），导致整体作业完成时间被最慢的一条流死死拖垮！

---

### 0.2 大模型张量并行 (TP=8) 中的三大物理挑战

在大模型张量并行（TP=8）分布式推理集群中：

1. **TP=8 木桶短板效应放大**：
   - 每一步 Transformer 注意力计算，必须等 8 张 NPU 卡的 KVCache 全部到位后才能执行 AllReduce 集合通信。单卡的微小网络抖动会被 8 卡集合通信无限放大，导致整个集群的算力严重空转；
2. **Layer 0 排队倒置引发计算气泡（Pipeline Bubble）**：
   - 大模型的神经网络计算是**严格按层顺序（Layer 0 $\to$ Layer 1 $\dots \to$ Layer 79）**执行的；
   - 如果网络缺乏优先级控制，由于微突发扰动导致 Layer 0 的数据包被堵在后面、而 Layer 31 的数据包先到了，NPU 无法开始计算，只能原地干等，产生巨大的**计算流水气泡（Bubble）**；
3. **原厂重构方案**：
   - 通过 **SemanticQoS 硬件优先级队列（将 Layer 0 设为最高优先级）** 与 **Coflow 协同调度**，确保首层数据毫秒级直达，彻底消除计算气泡！

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                   Layer 0 优先级调度消除 NPU 计算流水气泡示意图                        │
├────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                        │
│  [ 无优先级调度 (传统方案) ]:                                                          │
│  网络到达时序: [ Layer 31 ] ──► [ Layer 15 ] ──► [ Layer 0 (被堵在最后!) ]              │
│  NPU 状态:     [ ----------- 算力原地干等 380us (计算气泡 Bubble) ----------- ] ──► 计算 │
│                                                                                        │
│  [ 开启 SemanticQoS 优先级调度 (本项目方案) ]:                                         │
│  网络到达时序: [ Layer 0 (高优直达) ] ──► [ Layer 1 ] ──► [ Layer 2 ] ...               │
│  NPU 状态:     [ 立即开始 Layer 0 计算 ] ──► [ 边算边等 Layer 1 ] ──► 气泡耗时归零!     │
│                                                                                        │
│ 收益：首层计算零等待，端到端 TTFT 显著缩短！                                           │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. 验证目标与交付结论定义

### 1.1 现实前因痛点与待验证核心命题
1. **命题一（N-to-1 Incast 非等比时延爆炸）**：当并发发送端从 $N=1$ 增至 $N=2, 4, 8, 16$ 时，P99 尾部时延呈现非等比长尾激增（膨胀比 $\gg N$）；
2. **命题二（TP=8 Coflow 木桶短板放大）**：在 TP=8 下，Coflow 完成时间 $\text{CCT} = \max(T_0..T_7)$ 相比单流平均耗时大幅偏斜；
3. **命题三（Layer 0 排队倒置与气泡消除）**：通过硬件 QoS 队列保证 Layer 0 优先到达，彻底消除 NPU 计算流水气泡（Pipeline Bubble）。

### 1.2 最终交付数据与结论产出
1. **《N-to-1 Incast 缓冲区排队与 P99 尾部时延膨胀表》**；
2. **《TP=8 Coflow 偏斜系数与完成时间 (CCT) 实测表》**；
3. **《Layer 0 优先级调度前后计算气泡对比表》**；
4. **《Go / Conditional / No-Go 判定结论》**。

---

## 2. 实验动力学模型与数学公式

### 2.1 N-to-1 Incast 队列时延非等比膨胀模型
设瓶颈链路服务率为 $\mu$，单个流的包到达率为 $\lambda$。当 $N$ 个流并发打满时，总到达率 $\Lambda = N \cdot \lambda \to \mu$。
在有限缓冲（Buffer Size $= B$）下，尾部时延 $P_{99}$ 不仅包含传输时延 $T_{\text{tx}}$，还叠加了突发排队时延 $T_{\text{queue}}$：
\[
T_{P99}(N) = T_{\text{tx}} + \frac{B}{\mu} \cdot f_{\text{burst}}(N) + T_{\text{retransmit}} \cdot P_{\text{loss}}(N)
\]

### 2.2 TP=8 Coflow 完成时间 (CCT) 木桶效应模型
一个 TP=8 Coflow 包含 8 个子流 $\{f_0, f_1, \dots, f_7\}$。其端到端耗时为：
\[
\text{CCT} = \max \{ T_0, T_1, \dots, T_7 \}
\]

---

## 3. 测试工具与工程构建规范

测试工程存放在 `./原型验证代码/PVT-10_coflow_incast_bench/` 目录下：

```
原型验证代码/PVT-10_coflow_incast_bench/
├── incast_bench.cc            # N-to-1 Incast 压测工具
├── coflow_bench.cc            # TP=8 Coflow 偏斜模拟工具
├── layer_priority_bench.cc    # Layer 0 优先级与计算气泡消融工具
├── plot_dynamics.py           # 绘制 Incast 膨胀与 Coflow 偏斜曲线脚本
└── Makefile                   # 编译构建工程 (make -j16)
```

编译方法：
```bash
cd ./原型验证代码/PVT-10_coflow_incast_bench && make clean && make -j16
```

---

## 4. 分步执行测试操作规程 (SOP)

开发人员在执行 PVT-10 时，请严格按照以下 4 个步骤逐步执行，并理解每一步的操作意图：

### 步骤 1：扫描 $N=1..16$ 发送端，测量 Incast 排队时延爆炸
- **操作意图**：将并发 Sender 数量从 1 逐渐增加到 16，向同一目的端口打入 128KB 数据包，记录平均时延与 P99/P99.9 尾部时延，证实排队时延膨胀比远大于 $N$。
- **执行命令**：
```bash
./incast_bench --senders 1 --block-kb 128 --loops 1000 --out res_incast_1.csv
./incast_bench --senders 4 --block-kb 128 --loops 1000 --out res_incast_4.csv
./incast_bench --senders 16 --block-kb 128 --loops 1000 --out res_incast_16.csv
```

### 步骤 2：测量 TP=8 Coflow 木桶偏斜效应
- **操作意图**：启动 8 个并行子流模拟 TP=8 张量并行传输，计算单流平均时延与整体 Coflow 完成时间 $\text{CCT} = \max(T_0..T_7)$，验证偏斜系数 $\text{Skew} > 1.5$。
- **执行命令**：
```bash
./coflow_bench --groups 4 --tp 8 --out res_coflow_noqos.csv
```

### 步骤 3：消融 Layer 0 QoS 优先级对计算气泡的消除
- **操作意图**：在注入微突发网络背景流下，分别测试无优先级与开启 QoS（Layer 0 置为高优先）时的 Layer 0 到达位次，验证计算流水气泡耗时是否归零。
- **执行命令**：
```bash
./layer_priority_bench --disable-qos --out res_bubble_noqos.csv
./layer_priority_bench --enable-qos --out res_bubble_qos.csv
```

### 步骤 4：生成动力学拟合曲线与汇总报告
- **操作意图**：汇总 Incast 爆炸倍数、Coflow 偏斜系数与气泡消除数据，输出完整的评审答辩证据包。
- **执行命令**：
```bash
python3 ./plot_dynamics.py --incast-dir ./ --coflow res_coflow_noqos.csv --bubble-qos res_bubble_qos.csv --out-summary summary_pvt10.csv
```

---

## 5. 数据采集清单与记录格式

### 5.1 Incast 与 Coflow 性能数据表 (`res_coflow.csv`)
```csv
test_item,concurrency_n,block_size_kb,avg_lat_us,p99_lat_us,cct_ms,skew_factor,bubble_time_us
incast_n1,1,128,15.2,18.4,0.0,1.0,0.0
incast_n8,8,128,45.8,182.0,0.0,3.9,0.0
incast_n16,16,128,92.1,840.5,0.0,9.1,0.0
coflow_tp8_no_qos,8,128,48.2,195.0,2.45,1.52,380.0
coflow_tp8_with_qos,8,128,32.1,45.0,0.85,1.08,0.0
```

---

## 6. Go / Conditional / No-Go 判定规则

- **Go (准入通过)**：
  - 成功测定 Incast 排队爆炸动力学曲线；
  - 开启 QoS 优先级调度后，Layer 0 传输延迟降低 $\ge 50\%$，计算流水气泡耗时归零；
  - TP=8 Coflow 偏斜系数降至 $< 1.15$；
- **Conditional (条件准入)**：气泡耗时降低但未完全归零；
- **No-Go (否决关闭)**：QoS 无法缓解多流拥塞或引发网络丢包风暴。

---

## 7. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 7.1 研发任务拆解与分工
- **工程师职责**：
  1. 编译测试工程；
  2. 按照第 4 节 SOP 步骤执行多并发压测；
  3. 观察不同 $N$ 并发下的 P99 时延与丢包率；
- **AI Agent 职责**：
  1. 负责 `incast_bench.cc` 中高精度纳秒级时间戳打点与 $\max(\cdot)$ 极值统计；
  2. 编写 Python 脚本自动拟合 Incast 排队膨胀曲线并输出评审证据图。

### 7.2 专属 Prompt 模板（可直接复制给 AI Agent）
```text
我是一名网络/系统级工程师，正在进行 PVT-10 多流 Incast 与 TP=8 Coflow 偏斜实验：
1. 请阅读 ./原型验证代码/PVT-10_coflow_incast_bench/ 中的全部源码与 Makefile；
2. 检查 incast_bench.cc，确保支持多线程并发向同一目的端口发送数据包，并记录 P50、P90、P99 与 P99.9 尾部延迟；
3. 按照第 4 节 SOP 步骤执行压测，扫描 N=1, 2, 4, 8, 16 并发，计算时延膨胀比 P99(N)/P99(1)；
4. 运行 layer_priority_bench 对比无优先级与开启 QoS 下 Layer 0 的到达位次与计算气泡时间；
5. 输出汇总 CSV 并绘制时延爆炸对比图。
```

### 7.3 常见排错指南
- **多线程测试时网卡瞬时丢包严重**：检查网卡 ring buffer 大小（`ethtool -g eth0`），可适当增大 rx/tx buffer；
- **高并发下计时器精度失真**：确保使用 `clock_gettime(CLOCK_MONOTONIC_RAW)` 进行纳秒计时，避免 NTP 时钟同步跳跃。
