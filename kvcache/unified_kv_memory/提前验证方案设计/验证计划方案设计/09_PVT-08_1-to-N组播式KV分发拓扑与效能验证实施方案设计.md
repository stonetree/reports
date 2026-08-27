# PVT-08：1-to-N 组播式 KV 分发拓扑与效能验证实施方案设计
## —— 真实业务广播场景验证：硬件网络多播与软件分层中继的拓扑、成本与故障对比

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个 `run_id` 必须冻结节点集合、源节点、消费者集合、对象大小、分片、树结构、方案、重复次数、故障注入、代码包、拓扑版本和证据等级；结果必须保留源端实际 Egress 字节、各消费者接收/完成事件、完成时间、重试、校验、错误码、硬件多播探测、`planned_path`/`actual_path` 和原始拓扑计数器。理想公式不能替代真实完成事件。

> **验证范围声明**：当前受控工程中的 `multicast_fanout_bench.cc` 只按公式生成单播、软件分层中继和硬件多播三类 DEMO 行；`test_fanout_scenarios.py` 只生成场景清单，不执行网络发送、接收、ACK、重试或慢节点统计，当前目录也没有 `eval_fanout.py`。因此现有代码只能验证参数、字段和场景状态，不能单独证明网络带宽、源端 Egress、消费者完成时间、真实多播或故障收益；固定公式和场景清单只能标记为 `DEMO`/`LAB`。硬件多播没有现场能力探测和完成事件时，必须标记为 `NOT-SUPPORTED`。

> **术语速查**：KVCache（大模型注意力键值缓存，即自回归生成过程中保存历史 Key 和 Value 激活状态、避免后续 Token 重复计算注意力）；1-to-N 分发（一个源节点向 N 个消费者节点发送同一业务对象）；Unicast（单播，源节点为每个消费者建立独立发送路径）；Staging Fanout（软件分层中继转发，利用计算节点建立树状拓扑并由中继继续发送）；Hardware Multicast（硬件网络多播，由交换机和网卡复制数据或处理组播组）；RDMA（Remote Direct Memory Access，远程直接内存访问）；URMA（通用远程直接内存访问，用户态高性能远程内存访问接口）；ACK（接收确认，用于确认消费者收到并校验对象或分片）；Egress（源端出向流量，必须统计实际发送字节）；Straggler（慢节点，即接收或处理明显较慢的消费者）；Incast（多对一突发网络拥塞，即多个中继同时向同一节点发送）；PD 分离（Prefill 与 Decode 计算生成解耦架构，即输入理解与逐字生成分置不同节点）；Quorum（法定完成数，只有协议明确允许部分消费者先继续时才能使用）；P99（完成时间分布中 99% 样本不超过的分位值）。

> **验证 ID**：PVT-08
> **验证名称**：1-to-N 组播式 KV 分发：硬件多播与软件分层中继拓扑及效能验证
> **验证优先级**：**🟢 P2 级（拓展验证项）**
> **对应验证阶段**：**E1（核心数据路径打通与分发拓扑验证）**
> **证伪标记**：否（分发效能与拓扑选型确认）
> **建议周期**：3~4 人日
> **主关联 IR**：`IR-01-04`, `IR-01-10`
> **核心 SRS / SR23 锚点**：
> - SRS：`L4-RDMA-MUL-FABRIC-002`
> - SR23：`SR23-01-04-02`, `SR23-01-10-01`
> **配套源码**：[`提前验证方案设计/验证计划方案设计/原型验证代码/PVT-08/`](file:///d:/codes/reports/kvcache/unified_kv_memory/提前验证方案设计/验证计划方案设计/原型验证代码/PVT-08)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`。正式结果必须绑定实际通信栈、网卡/交换机配置、拓扑快照和配置哈希。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 三种方案的物理差异

对于同一对象大小 `S` 和消费者数 `N`，理论源端发送量可以写成：

```text
单播：             source_egress = S × N
二叉软件中继：     source_egress = S × min(N, 2)
理想硬件多播：     source_egress = S
```

这些只是拓扑模型的字节下界或理想估计。真实源端发送量还受到分片头、控制消息、重试、拥塞重传、连接建立、慢节点回退和中继重复发送影响。必须使用源端网卡/通信库实际计数器验证。

对于分支因子 `b`、树深度 `d=ceil(log_b(N))` 和每跳有效传输时间 `T_hop`，软件树的理想完成时间近似为 `d × T_hop`；但中继同时接收和发送、队列排队、节点 NUMA、Incast 和故障会改变实际结果。因此“对数级”只能作为拓扑预期，不是性能承诺。

### 0.2 三类业务场景

| 场景 | 源对象 | 消费者 | 主要风险 |
|---|---|---|---|
| `prompt_broadcast` | 热点系统 Prompt 对应的 KVCache | 多个 Decode 副本 | 源端重复发送和前台启动时延 |
| `multi_agent` | 主 Agent 形成的共享上下文 KV | 多个专业 Agent | 多消费者同时接收造成 Incast |
| `pd_1p_to_nd` | Prefill 节点产生的 KVCache | 多个 Decode 节点 | 生成与接收的时间窗口不一致 |

场景清单必须冻结对象大小、消费者集合、Token/张量布局、分片规则和校验方式。不能因为三个场景使用同一 payload 字节数，就把它们当作相同业务负载。

### 0.3 当前受控源码能力矩阵

| 文件 | 当前可确认行为 | 当前不能声称的能力 |
|---|---|---|
| `原型验证代码/PVT-08/multicast_fanout_bench.cc` | 解析 `--nodes`、`--payload-mb`、`--sizes`、`--slow-node`、`--hardware-supported`、`--out`；按公式写单播/软件中继/硬件多播三类行 | 不创建 socket/QP，不发送/接收数据，不统计 ACK、重试、真实 Egress 或消费者完成时间 |
| `原型验证代码/PVT-08/multicast_fanout_bench.cc` | 单播源端字节为 `payload_bytes * nodes`；软件行使用 `payload_bytes * min(nodes, 2)`；时间由 `payload_mb/8` 的固定公式生成；慢节点只给最终时间加 10ms | 不能用这些数值证明实际网络带宽、树状中继或慢节点解耦 |
| `原型验证代码/PVT-08/multicast_fanout_bench.cc` | 默认 `nodes=8`、`payload=16MB`；硬件支持由命令行布尔值决定；每行 `retries=0` | 没有能力探测，`--hardware-supported` 不是现场硬件证明，重试为固定值 |
| `原型验证代码/PVT-08/test_fanout_scenarios.py` | 生成 `pvt08.scenario.v1` 场景 JSON；场景、节点数、payload、拓扑、故障和 seed 可配置；输出 `DEMO,SCENARIO_ONLY` | 不运行分发，不生成完成时间、源端字节、重试或慢节点影响；`workload_id` 使用随机 UUID，不因 seed 固定 |
| `原型验证代码/PVT-08/Makefile` | 使用 `g++ -O3 -std=c++17 -pthread -Wall` 构建 C++ 程序 | 没有 RDMA/URMA/TCP、多播、网卡监控或通信库依赖 |
| PVT-08 目录 | 当前没有 `eval_fanout.py` | 旧稿中引用的汇总脚本不存在 |

### 0.4 当前命令和状态的边界

当前 C++ 程序没有 `--mode`、`--multicast-group` 或 `--evidence-level` 参数；它一次运行会把三种方案都写入同一个 CSV。旧稿中按模式分别调用的命令不对应当前源码。Python 脚本的 `--fault` 只接受 `none`、`slow_node`、`node_failure`，不能使用 `slow_node_c6`。

C++ 输出中的 `NOT_SUPPORTED` 和 Python 输出中的 `SCENARIO_ONLY` 是源码内部字符串；正式文档统一映射为 `NOT-SUPPORTED` 或 `DEMO`/`INVALID-EVIDENCE`，但不能把内部状态直接解释成真实性能结论。

## 1. 验证目标与交付物

### 1.1 验证目标

| 目标 | 需回答的问题 | 最低证据 |
|---|---|---|
| 源端带宽 | 软件中继是否减少源端真实出向字节 | 源端网卡/通信库计数、发送日志和对象 ID 对账 |
| 完成时延 | 各方案从源开始到所有协议要求的消费者完成需要多久 | 源端开始、每个消费者完成、最终完成事件 |
| 中继效率 | 中继是否发生接收-转发重叠，是否形成预期拓扑 | 每跳发送/接收事件、树拓扑和中继队列 |
| 慢节点影响 | 单个消费者慢或失败时，健康节点是否被不必要地阻塞 | 健康节点完成时间、慢节点延迟、ACK/重试时间线 |
| 数据正确性 | 消费者收到的对象是否与源对象一致且未重复消费旧代次 | checksum、generation、对象 ID、分片完整性 |
| 硬件多播可用性 | 现场网络是否真的支持该路径 | 网卡/交换机配置、能力探测和实际组播完成事件 |

### 1.2 候选门限

在 `N>=8`、同一 payload、同一消费者集合和相同重复次数下，软件分层中继相对单播的源端 Egress 节省比例候选门限为至少 60%。该门限只适用于真实源端发送字节可对账的条件。

慢节点场景不直接预置“健康节点不受影响”。应先冻结健康节点完成时间的同场次基线，再报告：

```text
健康节点影响 = 慢节点注入时健康节点 P99 完成时间
             / 无故障时健康节点 P99 完成时间 - 1
```

硬件多播和软件中继的完成时延不应强行设定为同一门限。硬件多播不可用时标记 `NOT-SUPPORTED`；软件中继是否准入仍由自己的带宽、时延、重试和正确性门限判定。

### 1.3 交付物

1. 三个业务场景的单播/软件中继/硬件多播对照结果；
2. 源端、中继和消费者逐事件发送/接收/完成日志；
3. 慢节点、节点退出和丢包/重试故障报告；
4. 拓扑、网卡、交换机、通信库和数据布局快照；
5. `GO/CONDITIONAL/NO-GO/NOT-SUPPORTED/INVALID-EVIDENCE` 判定和证据缺口。

## 2. 目标拓扑、消息和确认协议

### 2.1 目标树节点描述符

当前仓库尚未实现该结构。真实实现前需要冻结节点和边的协议字段：

```cpp
struct FanoutTreeNode {
    uint32_t node_id;
    std::string endpoint;
    std::vector<uint32_t> children;
    uint32_t parent_id;
    bool is_source;
    bool is_relay;
};

struct BroadcastObject {
    uint64_t broadcast_id;
    uint64_t object_id;
    uint64_t generation;
    uint64_t payload_bytes;
    uint32_t chunk_bytes;
    uint32_t expected_consumers;
    uint64_t checksum;
};
```

节点描述符必须区分源、转发中继和最终消费者。`expected_consumers` 不能只由收到 ACK 的数量反推；源端应从冻结 manifest 得到消费者集合，避免漏节点被误判为完成。

### 2.2 分片和消息生命周期

每个广播对象建议按固定 `chunk_bytes` 分片，并为每片携带对象 ID、代次、分片序号、总分片数和 checksum：

```text
SOURCE_READY
  -> CHUNK_SUBMITTED
  -> RELAY_RECEIVED / CONSUMER_RECEIVED
  -> CHUNK_VERIFIED
  -> ACK_SENT
  -> OBJECT_READY
```

中继不得在没有完成接收校验的情况下把分片标记为可转发，除非协议明确支持流式转发并能在消费者侧检测半片、乱序和重复。任何重试都必须带原始分片序号和重试次数，防止重复数据被计入有效 Egress。

### 2.3 三种路径

```text
单播： Source -> C1, Source -> C2, ... Source -> CN

软件中继： Source -> Relay-1/Relay-2 -> 下游消费者

硬件多播： Source -> Multicast Group -> 多个消费者
```

软件中继的树应根据链路带宽、NUMA、消费者位置和中继负载生成。源端只发送一级并不自动保证总带宽更低，因为每个中继仍要产生下游发送；必须分别报告源端 Egress、总网络字节和完成时延。

## 3. 实验矩阵与冻结参数

### 3.1 场景和拓扑矩阵

| 维度 | 候选值 |
|---|---|
| 场景 | `prompt_broadcast`、`multi_agent`、`pd_1p_to_nd` |
| 消费者数 | 4、8、16；正式门限至少包含 8 |
| 拓扑 | `unicast`、`staging_fanout`、`hardware_multicast` |
| 分支因子 | 2、4；树深度和实际节点映射写入 manifest |
| Payload | 16MB、64MB、256MB；最终以真实 KV 布局为准 |
| 分片大小 | 64KB、1MB、4MB；要求按协议和设备对齐 |
| 重复次数 | 预热后每点至少 30 次，正式点按尾部稳定性增加 |
| 故障 | 无故障、慢节点、节点退出、可控丢包/重试 |

当前 C++ DEMO 支持 `--sizes 16,64,256` 这类尺寸输入，但仍只按公式生成 CSV；当前 Python 场景脚本的 `payload-mb` 是单值输入，且场景清单不是执行结果。

### 3.2 统一 A/B 条件

三种方案必须冻结：

- 相同的源对象、对象 ID、generation、payload 字节和 checksum；
- 相同的消费者节点集合和节点健康状态；
- 相同的分片大小、发送重复次数和启动顺序；
- 相同的 CPU/NUMA 绑定、网卡、MTU、链路速率和拥塞配置；
- 相同的故障注入位置、延迟、丢包率和故障持续时间。

如果硬件多播需要不同网段或交换机配置，需单独记录拓扑差异并把比较结论标记为 `CONDITIONAL`，不能称为严格同场景 A/B。

## 4. 工具审计、实际命令与最小实现增量

### 4.1 当前 C++ DEMO 的真实命令

当前构建和运行入口为：

```bash
cd ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-08
make clean && make -j2
./multicast_fanout_bench \
  --nodes 8 \
  --sizes 16,64,256 \
  --slow-node \
  --out multicast_formula_demo.csv
```

若要让硬件多播行填入理论字段，可以额外传入 `--hardware-supported`，但该参数只是人为开关：

```bash
./multicast_fanout_bench \
  --nodes 8 --payload-mb 64 --hardware-supported \
  --out multicast_hardware_flag_demo.csv
```

该命令仍输出 `DEMO`，不能证明硬件多播已被设备支持。当前程序一次输出三种方案，不能使用 `--mode` 选择单一方案。

### 4.2 当前 Python 场景清单命令

当前脚本支持的真实参数为：

```bash
python3 ./test_fanout_scenarios.py \
  --scenario prompt_broadcast \
  --nodes 8 \
  --payload-mb 64 \
  --topology staging_fanout \
  --fault slow_node \
  --seed 42 \
  --out prompt_broadcast_staging_scenario.json
```

该命令只生成 `pvt08.scenario.v1` 场景 JSON，输出 `evidence_level=DEMO`、`status=SCENARIO_ONLY`，不会运行网络分发。`--fault slow_node` 也没有指定具体节点字段；真实实现应增加 `fault_node`，当前脚本不能表达“C6 慢节点”这一精确注入。

### 4.3 当前缺失的评估入口

当前 PVT-08 目录没有 `eval_fanout.py`。在新增评估脚本之前，不能继续使用旧稿的汇总命令。正式评估器至少需要读取：

- C++/真实后端的源端和中继计数；
- 每个消费者的完成事件和 checksum；
- 场景 manifest、拓扑、故障和实际路径；
- 重试、丢包、超时和节点退出；
- 证据等级和无效原因。

### 4.4 最小真实实现增量

进入 LAB/MEASURED 前至少需要：

1. 实现实际单播发送和消费者接收，记录每个对象/分片的发送与完成事件；
2. 实现软件中继树的构建、父子连接、流式/非流式转发和 ACK 聚合；
3. 为硬件多播增加能力探测和现场配置读取，不接受命令行布尔值作为能力证明；
4. 增加 checksum、generation、重复分片、短包和乱序校验；
5. 增加慢节点、节点退出、丢包、重试和中继故障注入；
6. 接入网卡/通信库计数器，区分源端 Egress、总网络字节和有效 Payload 字节；
7. 为 Python 场景清单增加稳定 `workload_id`、`fault_node`、实际执行状态和 manifest 哈希；
8. 新增评估器和汇总 schema，统一 `NOT_SUPPORTED` 到 `NOT-SUPPORTED`。

## 5. 逐步执行 SOP

### Step 0：冻结源码、拓扑和结果目录

操作意图：确保不同方案使用相同对象、节点集合和故障配置。

执行动作：

```bash
run_id="PVT-08-$(date +%Y%m%d-%H%M%S)-fanout"
result_dir="results/pvt08/${run_id}"
mkdir -p "${result_dir}"
git rev-parse HEAD > "${result_dir}/git_commit.txt"
date --iso-8601=ns > "${result_dir}/timestamp.txt"
git status --short > "${result_dir}/git_status.txt"
```

应观察现象：commit、时间戳、工作树状态、节点清单和 topology profile 均已保存。

判定边界：源节点、消费者集合或对象 checksum 未冻结时，不得开始性能比较。

### Step 1：运行当前 C++ 公式 DEMO

操作意图：确认当前程序的参数、CSV schema 和固定公式边界。

执行动作：

```bash
cd ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-08
make clean && make -j2
./multicast_fanout_bench \
  --nodes 8 --sizes 16,64,256 --slow-node \
  --out "../../../../results/pvt08/${run_id}/formula_demo.csv"
```

应观察现象：CSV 包含 `N_UNICAST`、`SOFTWARE_STAGING_FANOUT` 和 `HARDWARE_MULTICAST` 三类行；前两类和硬件支持条件下的行标记 `DEMO,DEMO_ONLY`，硬件未声明支持时内部状态为 `NOT_SUPPORTED`。

判定边界：该步骤只能验证理论 schema；不得使用其中的源端字节、完成时间、重试或慢节点时间作为网络实测。

### Step 2：生成三类场景清单

操作意图：为真实后端准备统一场景 manifest，并确认场景生成器不会被误解为执行器。

执行动作：

```bash
python3 ./test_fanout_scenarios.py \
  --scenario prompt_broadcast --nodes 8 --payload-mb 64 \
  --topology unicast --fault none --seed 42 \
  --out "../../../../results/pvt08/${run_id}/prompt_unicast_scenario.json"

python3 ./test_fanout_scenarios.py \
  --scenario prompt_broadcast --nodes 8 --payload-mb 64 \
  --topology staging_fanout --fault none --seed 42 \
  --out "../../../../results/pvt08/${run_id}/prompt_staging_scenario.json"

python3 ./test_fanout_scenarios.py \
  --scenario prompt_broadcast --nodes 8 --payload-mb 64 \
  --topology hardware_multicast --fault none --seed 42 \
  --out "../../../../results/pvt08/${run_id}/prompt_hardware_scenario.json"
```

应观察现象：每个 JSON 包含 `scenario`、`nodes`、`payload_bytes`、`topology`、`fault` 和 required outputs，状态为 `SCENARIO_ONLY`。

判定边界：`workload_id` 当前由 UUID 随机生成，三次运行不会自动相同；真实 A/B 必须由外部 manifest 固定同一个 workload ID，不能直接拿三份随机 ID 比较。

### Step 3：完成硬件多播能力探测

操作意图：在尝试硬件多播前确认网络、网卡和通信栈支持，避免把命令行标志当成能力。

执行动作：

1. 保存交换机 IGMP/PIM 或等价组播配置、VLAN/子网、MTU、PFC/QoS 和组播组范围；
2. 保存网卡驱动、固件、通信库版本和能力输出；
3. 在小 payload、少量消费者下执行真实组播 join、send、receive、checksum 和 leave；
4. 记录每个消费者的完成事件和源端实际 Egress；
5. 若任一条件不具备，写入 `hardware_multicast_supported=false` 并标记 `NOT-SUPPORTED`。

应观察现象：能力探测和真实小包验证结果一致；组播组成员、发送端和接收端可在拓扑快照中对账。

判定边界：只有配置文件、没有真实 join/receive/completion 时，硬件多播结论为 `INVALID-EVIDENCE`，不是 `GO` 或 `NOT-SUPPORTED`。

### Step 4：运行真实单播基线

操作意图：建立源端 Egress、消费者完成时间和重试行为的基线。

执行动作：

1. 使用统一 manifest 启动一个源节点和 N 个消费者；
2. 为每个消费者发送同一对象的每个分片；
3. 记录源端发送、消费者接收、checksum 通过、ACK、重试和最终完成；
4. 保存每次重复的源端总字节、有效 Payload 字节和完成时间；
5. 分离预热和稳态，不能用理论 `payload × nodes` 代替计数器。

应观察现象：所有预期消费者都能校验完成；源端 Egress 与每个对象/分片发送记录一致；重试可追踪。

判定边界：任一消费者缺失、checksum 失败或发送/完成数无法对账时，单播基线为 `INVALID-EVIDENCE`，不能用于节省比例计算。

### Step 5：运行软件分层中继

操作意图：验证源端只承担一级发送是否能在真实树状转发中降低源端 Egress，同时观察中继处理和尾部。

执行动作：

1. 固定分支因子和树结构，记录每个父子边；
2. 启动中继的接收、校验和下游转发；
3. 源端只向一级中继提交对象，并记录实际提交字节；
4. 记录中继每个分片的接收、转发、队列等待和 ACK；
5. 记录所有消费者完成时间、重试、checksum 和对象状态。

应观察现象：树结构与实际连接一致；源端 Egress、总网络字节和有效 Payload 字节可分别统计；中继没有把旧 generation 或未校验分片转发为 READY。

判定边界：只在源端软件变量中减少发送次数而没有中继网络事件时，仍是 DEMO；不能使用理论 `min(nodes,2)` 结果作为真实节省比例。

### Step 6：运行硬件多播或记录不支持

操作意图：在能力已确认时进行同场景硬件多播对照；不支持时保留清晰的环境结论。

执行动作：

1. 使用与单播/软件中继相同的 payload、消费者集合和重复次数；
2. 由所有消费者加入同一组播组；
3. 采集源端发送、各消费者接收/完成、丢包、重试和退出事件；
4. 对比源端 Egress、总网络字节和完成时间；
5. 在配置或协议不支持时停止该轨道，输出 `NOT-SUPPORTED` 原因。

应观察现象：硬件路径的 actual path 能被网卡/交换机/通信库观测；消费者完成事件不只是应用层“假 ACK”。

判定边界：不能以 `--hardware-supported` 命令行参数或理想 `source_egress=payload` 作为硬件实测。

### Step 7：注入慢节点、节点退出和重试

操作意图：验证健康消费者和整网完成策略是否受单点异常影响。

执行动作：

1. 在固定消费者上注入可控接收延迟；
2. 在接收或转发阶段终止一个叶子节点；
3. 在指定链路注入可控丢包或短暂连接错误；
4. 观察健康节点是否完成、是否发生不必要的全树等待、是否正确重试；
5. 记录慢节点影响、重试次数、最终状态和资源清理。

应观察现象：健康节点完成时间、慢节点完成时间和整网完成时间可以分开报告；重试不导致重复对象被错误消费；节点退出有明确超时和回退动作。

判定边界：如果协议要求所有消费者完成，则不能用 Quorum 结论替代全量完成；如果协议允许 Quorum，必须在 manifest 中显式冻结法定数和业务影响。

### Step 8：统计、复核与归档

操作意图：形成可复核的源端、网络、消费者和故障证据链。

执行动作：

```text
results/pvt08/<run_id>/
  metadata.yaml
  manifest.json
  command.txt
  git_commit.txt
  git_status.txt
  environment.txt
  topology.txt
  switch_multicast_config.txt
  nic_capability.txt
  source_events.jsonl
  relay_events.jsonl
  consumer_events.jsonl
  ack_retry_events.jsonl
  node_health_events.jsonl
  per_run_summary.csv
  cdf_data.csv
  errors.log
  summary.md
```

应观察现象：每条性能结论都能回指事件日志；方案、场景、故障和实际路径没有混用。

判定边界：缺少源端真实 Egress、消费者完成事件或 topology profile 时，不能输出性能 `GO`。

## 6. 证据字段、公式与汇总格式

### 6.1 运行汇总

```csv
run_id,scenario,workload_id,scheme,nodes,payload_bytes,chunk_bytes,source_egress_bytes,total_network_bytes,completed_consumers,expected_consumers,checksum_failures,retries,normal_nodes_p99_ms,all_nodes_done_ms,tail_spread_ms,planned_path,actual_path,evidence_level,status,invalid_reason
```

`source_egress_bytes` 必须来自源端发送计数；`total_network_bytes` 应包含中继下游发送但要说明是否包含控制包和重传；`completed_consumers` 由消费者完成事件计算，不由 ACK 数量直接代替。

### 6.2 事件字段

```csv
run_id,broadcast_id,object_id,generation,scheme,node_id,parent_id,event,chunk_id,bytes,ts_ns,retry_no,checksum_ok,error_code
```

`event` 至少包括 `source_submit`、`relay_receive`、`relay_forward`、`consumer_receive`、`checksum_done`、`ack_send`、`retry`、`object_ready`、`node_exit` 和 `cleanup`。

### 6.3 故障记录

```csv
run_id,scenario,scheme,fault,fault_node,fault_start_ns,fault_end_ns,healthy_nodes_completed,slow_node_completed,retries,timeouts,health_impact_pct,status,invalid_reason
```

### 6.4 统计公式

```text
源端 Egress 节省率 = (单播源端 Egress - 被测方案源端 Egress)
                   / 单播源端 Egress × 100%

尾部扩散 = 所有消费者完成时间 P99 - 所有消费者完成时间 P50

健康节点影响 = 故障场景健康节点完成时间 P99
             / 无故障场景健康节点完成时间 P99 - 1

有效完成率 = checksum 通过且 generation 正确的消费者数
           / manifest 规定的消费者数
```

只在对象、消费者集合、重复次数和统计窗口一致时计算节省率。理论公式可以作为 `DEMO` 辅助列，但正式汇总必须同时提供实测列和原始事件引用。

### 6.5 证据分级

| 级别 | 允许内容 | 不允许内容 |
|---|---|---|
| `DEMO` | 公式字节数、理论时间、场景清单和 schema | 网络带宽、完成时延、重试、慢节点解耦结论 |
| `LAB` | 测试文件、局部真实链路或模拟中继的可复核结果 | 直接外推到现场多节点网络和硬件多播 |
| `MEASURED` | 真实源/中继/消费者、完整事件、拓扑和故障注入 | 固定公式、缺少消费者时间线或能力未验证 |

## 7. 判定标准、无效证据与止损条件

### 7.1 状态枚举

- `GO`：软件中继在冻结节点规模下满足源端 Egress 节省候选门限，所有消费者数据正确，慢节点/重试行为满足协议，且证据完整；硬件多播只有在能力验证和真实事件齐全时才可单独判定；
- `CONDITIONAL`：仅在特定节点数、树结构、payload、链路或故障参数下满足，必须写明条件；
- `NO-GO`：数据损坏、generation 错误、健康节点被不必要阻塞、重试无法收敛或完成时延超过止损线；
- `NOT-SUPPORTED`：硬件/交换机/通信库不支持硬件多播，或当前环境不具备对应网络能力；该状态不自动否定软件中继；
- `INVALID-EVIDENCE`：只有理论公式、场景 JSON、固定值、缺失源端/消费者事件、计划路径与实际路径不一致或无法复核拓扑。

### 7.2 无效证据规则

以下情况不能进入 `MEASURED` 汇总：

- 运行 `multicast_fanout_bench` 产生的 `DEMO` 行被直接当作网络实测；
- 使用 `--hardware-supported` 作为硬件能力证明；
- Python 只生成 `SCENARIO_ONLY` 清单，却据此填写完成时间或重试；
- 将理论 `payload × min(nodes,2)` 当作软件中继实际源端 Egress；
- 所有消费者共用一个最终完成时间，没有逐节点完成事件；
- `retries=0` 来自固定初始化，没有通信库或网络日志支持；
- 慢节点参数未记录具体节点、注入时段和实际延迟；
- 三种方案使用不同对象、消费者集合、树深度或统计窗口；
- 以 Quorum 完成替代 manifest 要求的全量消费者完成；
- 硬件多播只保存配置，没有真实 join、receive、checksum 和 completion。

### 7.3 立即止损条件

出现以下任一情况，应停止扩大 payload 或节点规模并保留现场：

- checksum 失败、旧 generation 被消费或重复分片无法去重；
- 源端或中继发送字节持续增长但消费者完成不增长；
- Incast 导致丢包、重试风暴、交换机队列溢出或网络设备错误；
- 慢节点导致健康节点无限等待，或节点退出后资源不能清理；
- 计划使用硬件多播但实际流量落入单播/软件转发路径；
- 源端 Egress、总网络字节和消费者事件无法相互对账。

## 8. 阶段推进与闭环

### E0：场景、拓扑和字段确认

确认三类业务场景、三种方案、节点集合、payload、树结构、故障参数和结果 schema。当前仓库可完成公式 DEMO 和场景清单预演。

### E1：单播和软件中继真实数据面

打通真实单播、消费者校验、软件树状中继、ACK、重试和逐节点完成事件，先完成无故障小规模对照。

### E2：慢节点与节点故障

完成慢节点、节点退出、丢包/短连接和中继故障注入，验证健康节点影响、资源清理和数据正确性。

### E3：硬件多播和规模扩展

在现场能力探测通过后完成硬件多播对照，并扩展到 8/16 个消费者和三个业务场景。硬件不支持时，保留 `NOT-SUPPORTED` 证据并闭合软件中继结论。

### 条件证伪：源端节省与总网络成本

分别比较源端 Egress、总网络字节、CPU/中继资源和完成尾延迟，防止只优化源端计数却把成本转移到中继或交换机。若中继转发带来的总网络和尾部成本超过业务约束，应把方案标记为 `CONDITIONAL` 或 `NO-GO`。

## 9. 研发人员与 AI Agent 执行约束

### 9.1 研发人员检查清单

- [ ] 三个业务场景的对象、payload、消费者集合和 checksum 已冻结；
- [ ] 单播、软件中继和硬件多播使用同一 manifest；
- [ ] 源端 Egress、总网络字节、逐消费者完成和重试均有原始事件；
- [ ] 软件树父子关系与实际连接一致；
- [ ] 硬件多播能力有现场探测和真实接收验证；
- [ ] 慢节点、节点退出和丢包注入的节点、时段和参数已记录；
- [ ] generation、分片序号、checksum 和重复数据校验已启用；
- [ ] Quorum 或全量完成规则在 manifest 中明确；
- [ ] DEMO/LAB/MEASURED 未混入同一汇总；
- [ ] 结果状态统一使用 `GO/CONDITIONAL/NO-GO/NOT-SUPPORTED/INVALID-EVIDENCE`。

### 9.2 AI Agent 执行提示词

```text
你负责执行 PVT-08 1-to-N 组播式 KV 分发拓扑与效能验证。

先读取项目索引、公共 Benchmark 契约、本方案和原型目录。确认当前 multicast_fanout_bench.cc 只按公式生成 N_UNICAST、SOFTWARE_STAGING_FANOUT、HARDWARE_MULTICAST 三类 DEMO 行，不发送网络数据；test_fanout_scenarios.py 只生成 SCENARIO_ONLY 场景清单；当前目录没有 eval_fanout.py。不要把理论 source_egress、固定 10ms 慢节点偏移或命令行 --hardware-supported 当作实测。

正式实验必须使用同一 manifest、对象 ID/generation/checksum、payload、消费者集合、树结构、重复次数和故障参数，对比单播、软件分层中继和硬件多播。记录源端 Egress、总网络字节、中继事件、逐消费者 completion、checksum、ACK、重试、节点退出和 actual_path。硬件多播不支持时标记 NOT-SUPPORTED，不用理论结果替代。

统计源端 Egress 节省率、完成时间 P50/P99、尾部扩散、健康节点影响和有效完成率。若源端事件、消费者完成事件、拓扑或故障日志缺失，输出 INVALID-EVIDENCE；若数据错误或旧 generation 被消费，输出 NO-GO。不得杜撰网络带宽和完成时延。
```

### 9.3 常见问题定位

| 现象 | 原因定位 | 处理方式 |
|---|---|---|
| CSV 中所有方案都有固定时间 | 当前 C++ 只是公式 DEMO | 保持 `DEMO`，接入真实数据面 |
| 硬件多播行有数据但设备未配置 | 使用了 `--hardware-supported` 开关 | 删除能力假设，执行现场探测 |
| `eval_fanout.py` 找不到 | 当前目录没有该脚本 | 先实现评估器或人工按字段复核，不引用不存在的命令 |
| `slow_node_c6` 参数报错 | Python 只接受 `slow_node` | 增加 `fault_node` 字段，或按当前 CLI 生成场景后由后端注入 |
| 三份场景 `workload_id` 不同 | 当前脚本使用随机 UUID | 外部冻结统一 workload ID 和 manifest |
| 源端字节减少但总网络字节增加 | 中继承担了下游复制成本 | 分别报告源端和全网成本，重新评估树结构 |
| 健康节点被慢节点拖住 | ACK/完成策略是全量同步 | 先确认业务是否允许 Quorum；不允许则保留 NO-GO |
| 重试后消费者收到重复块 | 缺少分片序号/generation 去重 | 增加对象版本和幂等接收校验 |
| 硬件组播实际走了单播 | 组播成员、交换机或通信栈未生效 | 通过抓包/设备计数确认 actual_path |
| 节点退出后树无法结束 | 中继和超时清理不完整 | 增加节点健康、超时、重建和资源回收事件 |

本方案的完成标准不是“公式程序成功写出 CSV”，而是形成可复核链路：统一 manifest → 真实源端发送 → 中继/硬件路径 → 逐消费者校验完成 → ACK/重试与故障时间线 → 源端和全网成本统计 → 最终状态判定。
