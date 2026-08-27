# PVT-08：1-to-N 组播式 KV 分发拓扑与效能验证实施方案设计
## —— 真实业务广播场景验证：硬件网络多播与软件分层中继 (Staging Fanout) 效能及拓扑对比

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。输入必须覆盖节点数、对象大小、拓扑和故障；输出必须包含源端 Egress 字节/带宽、各接收端完成时间、重试和慢节点影响。无硬件多播时标记 `NOT-SUPPORTED/N/A`，不记 PASS。

> **验证 ID**：PVT-08  
> **验证名称**：1-to-N 组播式 KV 分发：硬件多播 vs 软件分层中继 (Staging Fanout) 拓扑与效能验证  
> **验证优先级**：**🟢 P2 级（拓展验证项）**  
> **对应验证阶段**：**E1 核心数据路径与分发拓扑打通**  
> **证伪标记**：否（分发效能与拓扑选型确认）  
> **建议周期**：3~4 人日  
> **主关联 IR**：`IR-01-04`, `IR-01-10`  
> **核心 SRS / SR23 锚点**：  
> - SRS: `L4-RDMA-MUL-FABRIC-002`  
> - SR23: `SR23-01-04-02`, `SR23-01-10-01`  
> **开源基线版本与代码仓库**：  
> - **Mooncake**：[`https://github.com/kvcache-ai/Mooncake.git`](https://github.com/kvcache-ai/Mooncake.git) (Commit: `f90ae691f109e49a60920e0c8abbf7e572826d8c`，子模块: `mooncake-transfer-engine/`)  
> **研发对齐状态**：已闭环研发评估报告 14 项与硬件多播双轨制规范（明确真实硬件多播与软件 Staging 树状分层双轨评测规程）  

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统系统开发视角：单播 (Unicast) vs 树状中继广播 (Tree Broadcast)

在分布式网络（如 BitTorrent P2P 文件分发、CDN 视频流中继、分布式集群配置同步 Gossip）中：
- **单播（Unicast）瓶颈**：如果源节点要向 64 个目标节点发送同一份 100MB 的文件，源节点必须在网卡上连续调用 64 次发送（总传输量 $64 \times 100\text{MB} = 6.4\text{GB}$）。源节点的上行出向带宽（Egress Bandwidth）瞬间被打满，排在后面的第 60~64 个节点要苦等十几秒才能收到数据！
- **树状分层中继（Staging Fanout / Tree Broadcast）**：
  - 源节点仅把数据发送给 2 个一级中继节点（出向流量从 $O(N)$ 骤降为 $O(1)$）；
  - 2 个中继节点收到后，各自并发转发给下一级的子节点；
  - 整网的广播完成时间从线性时间 $O(N)$ 降至对数时间 $O(\log_2 N)$，极大降低了对源节点网卡的带宽压榨！

---

### 0.2 大模型集群中的三大高频 1-to-N 组播场景

在大模型生产推理集群中，存在三大不可忽视的高频广播场景：

1. **热点系统提示词广播（System Prompt Broadcast）**：
   - 比如一份 64K 的公司规章或法律知识库作为通用 System Prompt，计算完 KVCache 后需要瞬间同步给集群中的 16 ~ 64 个 Decode 副本；
2. **Multi-Agent 多智能体协同分发**：
   - 主调度 Agent 分析了包含 100K 复杂上下文的环境状态后，需要并发将该上下文推送给 8 个不同的专业工作子 Agent（Code Agent、Review Agent、Test Agent 等）；
3. **PD 分离架构下的 1-to-N 副本同步**：
   - 单个 Prefill 节点计算完巨型 KV 后，向多个 Decode 实例并发分发。

#### 为什么选择“纯软树状分发 (Staging Fanout)”而非“硬件网络多播”？
- **硬件网络多播的局限**：依赖交换机开启 IGMP/PIM 组播路由协议，网络配置极其复杂脆弱，跨机房/公有云 VPC 通常直接封禁硬件多播；
- **软件分层中继的巨大优势**：仅依靠标准的点对点 RDMA/TCP 即可在应用层组建转发树，不仅能**节省 $\ge 60\%$ 的源端带宽**，而且在遇到慢节点（Straggler）时具有天然的**异步解耦能力**（慢节点不会拖慢正常节点），免除了对交换机硬件特性的强依赖。

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                   Staging Fanout 软件树状分发拓扑与慢节点解耦时序                      │
├────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                        │
│                      [ 源节点 (Source Root) ]                                          │
│                             /        \                                                 │
│             (DMA 传输 1)   /          \   (DMA 传输 2)                                 │
│                           ▼            ▼                                               │
│                 [ 中继节点 C1 ]    [ 中继节点 C2 ]                                     │
│                   /       \            /       \                                       │
│                  ▼         ▼          ▼         ▼                                      │
│                [ C3 ]    [ C4 ]     [ C5 ]    [ C6 (慢节点: +10ms 延迟) ]              │
│                                                                                        │
│ 收益 1：源端网卡流量减少 75% (仅需发送 2 次)；                                         │
│ 收益 2：正常节点 C1~C5 在 2.1ms 准时就绪并启动推理，慢节点 C6 零阻塞整网！             │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. 验证目标与交付结论定义

### 1.1 现实前因痛点与待验证核心命题
1. **现实痛点**：源节点以单播方式向多节点分发热点 KVCache 时遭遇严重的出向带宽瓶颈与网络 Incast 排队拥塞；
2. **核心命题**：
   - 证明软件分层树状组播（Staging Fanout）在免除复杂交换机组播配置依赖的同时，相比单播能节省 **$\ge 60\%$** 的源节点网卡出向带宽；
   - 证明在慢节点扰动与丢包下，软件分层组播具有天然的异步解耦能力，不会因单个慢节点阻塞整网。

### 1.2 最终交付数据与结论产出
1. **《三大场景下 N 次单播 vs 软件 Staging 组播 vs 硬件多播完成时延对比表》**；
2. **《慢节点/丢包扰动下各分发方案抗抖动与恢复时延实测表》**；
3. **《源节点出向带宽与网卡吞吐占用对比图》**；
4. **《Go / Conditional / No-Go 判定结论》**。

---

## 2. 核心数据结构与树状软件分层组播设计

### 2.1 软件 Staging 树状拓扑定义

```cpp
#include <stdint.h>
#include <vector>
#include <string>
#include <atomic>

struct FanoutTreeNode {
    uint32_t node_id;             // 节点 ID
    std::string ip_port;          // 通信端点
    std::vector<uint32_t> children_ids; // 下游中继转发目标子节点集合
    bool is_root = false;
    bool is_relay = false;
};

struct BroadcastTask {
    uint64_t broadcast_id;
    uint64_t object_id;
    uint32_t total_bytes;
    std::vector<FanoutTreeNode> topology_tree;
    std::atomic<uint32_t> ack_count{0};
};
```

---

## 3. 测试工具与工程构建规范

测试工程存放在 `./原型验证代码/PVT-08/` 目录下：

```
原型验证代码/PVT-08/
├── multicast_fanout_bench.cc # 1-to-N 单播 vs 软件 Staging 组播 vs 硬件多播对比压测工具
├── test_fanout_scenarios.py  # 驱动三大业务场景的测试脚本
├── eval_fanout.py            # 统计源端带宽与广播时延分析脚本
└── Makefile                  # 编译构建工程 (make -j16)
```

编译方法：
```bash
cd ./原型验证代码/PVT-08 && make clean && make -j16
```

---

## 4. 分步执行测试操作规程 (SOP)

开发人员在执行 PVT-08 时，请严格按照以下 4 个步骤逐步执行，并理解每一步的操作意图：

### 步骤 1：运行 N 次单播基线测试
- **操作意图**：源节点以单播方式向 8~16 个目标节点依次发送 64MB 数据，测量源端网卡总带宽占用与最后一个节点接收完成的时间，作为对照基准。
- **执行命令**：
```bash
./multicast_fanout_bench --mode unicast_n_times --nodes 8 --payload-mb 64 --out res_unicast.csv
```

### 步骤 2：运行软件 Staging 树状分层组播测试
- **操作意图**：构建 2 叉树分层拓扑，源节点仅向下游 2 个中继节点发送，由中继节点并行转发，测量源端出向带宽节省比例（验证是否 $\ge 60\%$）与整网广播时间。
- **执行命令**：
```bash
./multicast_fanout_bench --mode software_staging_fanout --nodes 8 --payload-mb 64 --out res_staging.csv
```

### 步骤 3：注入慢节点扰动，验证异步解耦特性
- **操作意图**：在叶子节点 C6 人为注入 10ms 网络延迟，观察正常节点 C1~C5 的就绪时间是否保持在 2.1ms 准时启动推理，验证软件树状分发不会因单节点抖动拖慢整网。
- **执行命令**：
```bash
python3 ./test_fanout_scenarios.py --scenario prompt_broadcast --nodes 8 --payload-mb 64 --topology staging_fanout --fault slow_node_c6 --out res_slow_node.json
```

### 步骤 4：生成三大业务场景汇总对比表
- **操作意图**：汇总热点 Prompt 广播、Multi-Agent 上下文分发与 PD 副本同步三大场景的测试数据，输出完整对账表。
- **执行命令**：
```bash
python3 ./eval_fanout.py --unicast res_unicast.csv --staging res_staging.csv --slow-node res_slow_node.json --out summary_pvt08.csv
```

---

## 5. 数据采集清单与记录格式

### 5.1 组播分发性能对比表 (`res_fanout.csv`)
```csv
scenario,payload_mb,target_nodes,scheme,total_broadcast_time_ms,source_egress_gbps,p99_node_ready_ms,tail_spread_ms
prompt_broadcast,64,8,unicast_n_times,18.4,180.2,18.4,4.2
prompt_broadcast,64,8,software_fanout,4.8,45.1,5.1,0.6
prompt_broadcast,64,8,hardware_mcast,4.3,22.5,4.3,0.0
multi_agent_fanout,128,8,software_fanout,9.2,46.0,9.6,0.8
```

---

## 6. Go / Conditional / No-Go 判定规则

- **Go (准入通过)**：在 $N \ge 8$ 节点下，软件 Staging Fanout 相比单播节省 $\ge 60\%$ 源端带宽，且广播完成时延相比硬件多播差距 $< 10\%$；
- **Conditional (条件准入)**：在小规模节点（$N \le 4$）下收益不明显，仅在 $\ge 8$ 节点大集群启用；
- **No-Go (否决关闭)**：软件分层组播时延显著劣于单播，且实现复杂度过高。

---

## 7. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 7.1 研发任务拆解与分工
- **工程师职责**：
  1. 编译测试工程；
  2. 按照第 4 节 SOP 步骤执行单播与树状组播测试；
  3. 观察源端网卡流量与各节点就绪时间；
- **AI Agent 职责**：
  1. 负责 `multicast_fanout_bench.cc` 中二叉树/多叉树中继转发与异步 ACK 聚合逻辑编写；
  2. 自动生成带宽对比柱状图与广播时延分布曲线。

### 7.2 专属 Prompt 模板（可直接复制给 AI Agent）
```text
我是一名分布式网络工程师，正在进行 PVT-08 验证（1-to-N 组播式 KV 分发拓扑与效能）：
1. 请阅读 ./原型验证代码/PVT-08/multicast_fanout_bench.cc 与 test_fanout_scenarios.py；
2. 检查基于计算节点的树状中继转发算法，确保源节点仅需向下游中继发送数据，由中继节点异步接力广播；
3. 按照第 4 节 SOP 步骤执行压测，模拟 1 节点向 8、16 节点广播 64MB 数据包，对比单播与软件树状分发的源端带宽与完成时延；
4. 运行 test_fanout_scenarios.py 注入单节点 10ms 延迟，验证软件分层组播是否具备异步解耦特性（不阻塞其他健康节点）。
```

### 7.3 常见排错指南
- **中继节点转发出现丢包**：检查中继节点的显存与网络并发接收缓冲区，确保中继转发时采用流式流水线（边收边发）；
- **慢节点阻塞了整棵树**：检查 ACK 收集逻辑，确保上层调度器只要收到法定节点数（Quorum）就准许启动推理，不要做全量同步等待。
