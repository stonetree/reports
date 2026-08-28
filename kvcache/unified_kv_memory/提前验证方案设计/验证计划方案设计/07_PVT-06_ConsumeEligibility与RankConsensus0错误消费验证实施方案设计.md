# PVT-06：ConsumeEligibility 与 RankConsensus 语义一致性与 TP=8 多卡状态同步验证实施方案设计
## —— 6 维语义资格校验与多卡协同验证：TP=8 状态同步、统一协同回退与集合通信安全

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个测试 `run_id` 需在运行前完整固化模型架构、Tokenizer 词表哈希、Prompt 模板、LoRA 适配器、视图租约、Ready 就绪状态、张量并行 Rank 总数、测试用例集、超时时限、代码包版本及目标证据等级；实测产出必须完整保留逐用例的 Oracle 独立对账记录、各 Rank 的硬件事件时间线、集合通信完成状态、错误消费计数、超时日志、进程退出码、`planned_path`/`actual_path` 路径对账及规范的状态判定枚举。测试脚本内部返回的 `PASS`、`FAIL` 或 `NOT_EXECUTED` 属于中间过程状态，不可直接等同于工程准入结论。

> **验证范围声明**：在当前受控的原型验证工程中，`consume_eligibility.cc` 仅实现了 6 个基础字符串与时间戳字段的顺序比较；`rank_consensus_bench.cc` 仅为单进程内部局部变量位图的逻辑演示，未集成真实的跨进程 POSIX 共享内存 (`/dev/shm`)、多进程 Rank 编排、底层硬件集合通信调用或物理跨卡同步计时；`test_correctness.py` 仅在显式传入 `--sut-command` 时才会通过标准输入输出调用外部被测程序。因此，现有受控源码仅用于验证接口定义、用例构造逻辑与异常拦截流程，不可直接作为证明已实现生产级 TP=8 张量并行多卡状态同步、部分前缀安全挂载 (Partial Attach)、集合通信安全或已完全消除错误消费的依据。在缺乏真实被测系统 (SUT)、独立 Oracle 校验器、全量 Rank 物理时间线及底层系统日志的前提下，测试结论统一限定标记为 `DEMO` 或 `LAB`。

> **术语速查**：
> - **KVCache**：大模型注意力键值缓存（大模型自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免后续 Token 生成时重复计算注意力）；
> - **ConsumeEligibility**：消费资格校验引擎（在读取命中 KVCache 之前，对模型架构、Tokenizer 词表哈希、Prompt 模板哈希、LoRA 适配器、Ready 就绪位与视图租约有效性等 6 维语义进行微秒级合法性检查的校验引擎）；
> - **RankConsensus**：张量并行多卡状态同步机制（在 TP=8 多卡并行时，通过低时延共享内存同步各卡命中状态与一致性动作，确保多卡协同，杜绝集合通信死锁）；
> - **Tokenizer**：分词器（将自然语言文本编码为 Token ID 整数序列的词表映射组件）；
> - **Prompt 模板**：组织系统提示词、用户输入与上下文结构的标准化消息模板；
> - **Ready Bit**：写入就绪位（用于标识 KVCache 物理块已完成 DMA 写入且内存可见性已完全生效的原子标志位）；
> - **Lease**：视图租约（用于标识显存对象在限定时间窗口内合法有效且未被释放的授权凭证）；
> - **Generation**：代际版本号（用于防止陈旧失效的后台 I/O 完成事件错误覆盖新显存映射的版本标识）；
> - **Oracle**：独立判决器（独立于被测系统算法、用于提供绝对正确基准结果的权威验证器）；
> - **SUT**：System Under Test（被测系统）；
> - **POSIX 共享内存**：跨进程共享内存机制（Linux 系统下通常挂载于 `/dev/shm` 内存文件系统）；
> - **集合通信**：多张加速卡之间共同参与的 AllReduce、AllGather 等底层分布式通信原语；
> - **Coordinated Fallback**：协同回退（当张量并行中任意一张卡发生校验失败或超时未响应时，驱动所有卡统一平稳回退至本地直接重算的安全机制）；
> - **Partial Attach**：部分前缀安全挂载（当缓存数据仅有部分前缀匹配而尾部发生语义冲突时，安全挂载匹配前缀并仅对冲突尾部执行增量重算的精细调度策略）；
> - **TTFT**：Time To First Token（首字生成延迟）；
> - **TPOT**：Time Per Output Token（单字生成延迟）。

> **验证 ID**：PVT-06
> **验证名称**：ConsumeEligibility 消费资格校验与 RankConsensus 多卡状态同步验证
> **验证优先级**：**🔴 P0 级（核心关键项）**
> **对应验证阶段**：**E1（核心数据路径打通与多卡状态同步）**
> **证伪标记**：否（可消费性与多卡协同正确性确认）
> **主关联 IR**：`IR-01-10`, `IR-01-11`, `IR-02-01`, `IR-02-05`
> **核心 SRS / SR23 锚点**：
> - SRS：`L2-KV-AttachHandle-034`, `L2-KV-PartialAttachPlan-038`, `L3-MS-ConsumeEligibility-060`, `L3-CO-VisibilityReadyBitmap-064`, `L1-PD-RankConsensus-013`
> - SR23：`SR23-01-10-01`, `SR23-01-11-01`, `SR23-02-01-01`, `SR23-02-05-01`, `SR23-02-05-02`, `SR23-02-10-01`
> **配套源码**：[`./原型验证代码/PVT-06/`](./原型验证代码/PVT-06/)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`；vLLM `842dd8fd96650063e1ad32e6075742d457d39773`。正式测试结果需绑定实际被测系统、通信库版本、底层驱动及配置哈希。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 为什么语义校验和多卡共识必须同时验证

在大模型分布式推理架构中，KVCache 不仅取决于 Prompt 原始文本。模型结构微调、Tokenizer 词表变更、Prompt 模板差异、LoRA 适配器切换以及显存块的写入生命周期，都会直接决定一段缓存张量在数学语义上是否允许被当前推理上下文安全消费。若仅在应用层比对缓存键哈希或对象 ID，可能将表面相似但实际数学权重不兼容的数据接入计算流，导致模型输出逻辑错乱或产生非预期的生成结果。

在张量并行 (TP=8) 多卡协同场景下，控制面面临分布式同步约束：8 张物理加速卡必须在进入下一次集合通信 (AllReduce/AllGather) 前，对“是复用加载缓存数据”还是“全体执行本地直接重算”达成一致决断。若部分 Rank 判定命中并进入加载分支，而其余 Rank 因局部校验失败进入重算分支，多卡之间在集合通信算子上的调用时序与张量尺寸将发生错位，引发集群死锁挂起。因此，多卡状态同步机制需与 6 维语义校验深度协同，确保所有卡在每次推理迭代中步调完全一致。

### 0.2 六维语义与全生命周期检查体系

生产级 ConsumeEligibility 校验引擎对命中的 KVCache 执行以下 6 维微秒级合法性检查：

```text
第 1 维：模型版本 (Model Version) ──────────────► 验证基础模型架构与权重版本是否完全匹配
第 2 维：Tokenizer 词表哈希 (Tokenizer Hash) ────► 验证分词算法与编码词表指纹是否一致
第 3 维：Prompt 模板哈希 (Template Hash) ───────► 验证 System/User/Assistant 模板组织结构是否匹配
第 4 维：LoRA 适配器标识 (Adapter ID) ──────────► 验证微调权重与插槽标识是否吻合
第 5 维：Ready 写入就绪位 (Ready Bit) ──────────► 验证后台 DMA 搬运已完成且内存屏障已生效
第 6 维：ViewLease 租约有效性 (Lease Validity) ──► 验证远端显存对象租约未过期且未被主动撤销
```

每一个维度的校验失败对应独立的错误枚举码，以便精准审计、故障归因及触发针对性的回退策略。仅有 6 维全部通过方可放行消费；任一维度异常均阻断消费。

### 0.3 Rank 共识决策模型与 POSIX 共享内存架构

为了在张量并行多卡之间实现微秒级低开销状态同步，系统采用基于 POSIX 共享内存 (`/dev/shm`) 的原子位图共识协议：

```text
各 Rank 独立完成 6 维校验
        │
        ▼
各 Rank 将本卡结果写入当前 consensus_epoch 的共享原子位图 (Shared Atomic Bitmap)
        │
        ▼
等待全量 8 卡写入完成或触发超时时限 (Timeout)
        │
        ▼
读取同一 epoch 的全局位图：
  ├─► 8 卡全量通过 (0xFF) : 8 卡同步进入缓存加载与显存挂接
  └─► 任一卡失败/超时/版本错位 : 8 卡统一触发 Coordinated Fallback，步调一致回退至本地重算
        │
        ▼
全量 Rank 以完全相同的算子调用顺序安全进入后续分布式集合通信
```

单字节位图绑定严格的 `consensus_epoch`（共识轮次）、`protocol_version`（协议版本）及各 Rank 的心跳时间戳，防止因上一轮残留脏位或异常退出进程引发误判。

### 0.4 当前受控源码能力矩阵审计

| 源码文件与路径 | 当前受控源码实际行为 | 现阶段尚不能声称的能力 |
|---|---|---|
| `原型验证代码/PVT-06/consume_eligibility.h` | `SemanticMetadata` 包含 4 个字符串、租约时间戳及 `bool ready_bit`；`CheckResult` 定义了 `PASS` 与 6 个拒绝枚举 | 未包含 Token ID 序列对比、Generation 代际版本号、部分挂载执行计划、原子内存序及 xxHash64 哈希计算 |
| `原型验证代码/PVT-06/consume_eligibility.cc` | 按固定顺序执行模型、Tokenizer、模板、适配器字符串比较，随后比对 Ready 位及 `now_ms > lease_expire_ms` | 缺乏 xxHash64 指纹校验、原子内存屏障 (Acquire/Release)、租约临界边界处理及跨卡通信协议 |
| `原型验证代码/PVT-06/rank_consensus_bench.cc` | 单进程循环执行校验；在栈局部变量中操作 `volatile uint8_t rank_bitmap`；每 100 轮模拟注入 `0x7F`；耗时计算固定累加常数 `18.5`；输出硬编码至固定 CSV 文件 | 未调用 `/dev/shm` 共享内存；无多进程 Rank 编排；无真实集合通信调用；未模拟真实进程宕机；测得的共识耗时为模拟值 |
| `原型验证代码/PVT-06/test_correctness.py` | 定义了 C01~C08 共 8 类冲突用例；仅在外部提供 `--sut-command` 时通过 stdin/stdout 与被测程序交互并比对返回状态 | 默认运行不调用任何被测接口；当前目录下未包含默认 SUT 可执行文件；C07 与 C08 所需的部分挂载与协同回退状态在 C++ 原型中尚未实现 |
| `原型验证代码/PVT-06/Makefile` | 采用 `g++ -O3 -std=c++17 -pthread -Wall` 编译单机版 `rank_consensus_bench` | 未链接 POSIX 共享内存库、xxHash 库、HCCL/NCCL 集合通信库或分布式运行时 |

### 0.5 当前原型的证据边界与审计事实

`rank_consensus_bench.cc` 输出的 `P50/P99` 仅是对单进程内部局部代码段计时后累加静态常数所得的演示数据，并非 8 个独立物理进程在真实总线与网络上的同步分位数。

`test_correctness.py` 在未显式传入 `--sut-command` 时，仅将测试用例静态写入结果文件，此时 `actual` 字段为 `null`，`executed_cases` 为 0，且脚本以状态码 2 退出。因此，该脚本属于标准化测试 Harness 脚手架，不直接作为证明系统已消除错误消费的生产级依据。

---

## 1. 验证目标与交付物定义

### 1.1 核心验证目标

| 验证核心维度 | 核心物理与工程问题 | 所需客观证据 |
|---|---|---|
| 六维语义校验正确性 | 8 类语义冲突、生命周期过期及状态未就绪用例是否均被精准拦截或安全回退 | 每一个测试用例实际调用 SUT 的输入输出日志、独立 Oracle 判决结果、错误消费绝对计数 |
| 正常语义消费放行 | 在 6 维语义完全匹配且数据就绪的前提下，系统是否平稳放行缓存消费 | 正向用例执行日志、模型/租约/Ready 快照凭证、数据一致性校验记录 |
| 部分前缀安全挂载 | 当长 Prompt 前缀匹配而尾部语义冲突时，系统能否安全挂载匹配前缀并增量重算尾部 | Partial Attach 拆分执行计划、匹配前缀长度、尾部重算任务日志及最终张量输出校验 |
| 多卡状态同步一致性 | 在张量并行 TP=8 环境下，8 个独立 Rank 是否在同一 epoch 内达成一致决断 | 8 个 Rank 独立记录的决策日志、Epoch 轮次标识、超时拦截日志、进程退出状态码 |
| 集合通信死锁防护 | 在单卡慢、卡分歧或特定 Rank 异常崩溃注水场景下，后续集合通信算子是否能平稳退出或安全完成 | 集合通信调用开始/完成时间戳、底层错误码、系统死锁监控日志、看门狗超时记录 |
| 微秒级极速控制面 | 6 维语义校验与多卡状态同步耗时是否稳定达到微秒级工程准入门限 | 逐样本原始计时日志、CPU 核心亲和性绑定记录、P50/P95/P99 统计分布 |

### 1.2 阶段交付资产

每个正式 `run_id` 交付：
1. **《8 类语义冲突用例与独立 Oracle 对账报告》**：逐用例记录输入 Payload、预期结果、SUT 实际返回、Oracle 判定及错误消费计数；
2. **《TP=8 多卡状态同步全量事件时间线日志》**：包含 8 个 Rank 的独立决策日志、Epoch 流转轨迹、共享内存状态及进程退出码；
3. **《集合通信死锁防护与故障注水测试表》**：记录慢卡注水、进程异常终止、分歧回退及通信算子完成时间戳；
4. **《微秒级校验与同步时延分位数对照表》**：基于全量原始样本精确计算的 P50/P95/P99 时延指标；
5. **标准证据包与判定报告**：包含 `manifest.json`、原始事件日志、环境快照及 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE` 最终判定。

---

## 2. 目标接口、状态机与正确性模型

### 2.1 目标语义元数据结构扩展

面向跨框架生产级互通的目标语义元数据结构设计：

```cpp
struct alignas(64) SemanticMetadata {
    char     model_version[32];     // 模型架构与版本标识
    uint64_t tokenizer_hash;        // xxHash64 词表指纹
    uint64_t template_hash;         // xxHash64 Prompt 模板指纹
    char     adapter_id[32];        // LoRA 适配器标识
    uint64_t lease_expire_ms;       // 视图租约到期绝对时间戳 (毫秒)
    uint64_t generation;            // 显存块代际版本号
    uint64_t object_id;             // 显存对象全局唯一 ID
    uint32_t ready_bit;             // 原子写入就绪标志位 (Acquire/Release)
    uint32_t protocol_version;      // 协议版本标识
};
static_assert(sizeof(SemanticMetadata) == 128);
```

### 2.2 标准返回结果枚举定义

校验引擎与共识模块对外输出的标准状态枚举定义：

```cpp
enum class EligibilityResult {
    PASS = 0,
    REJECT_MODEL_MISMATCH = 1,
    REJECT_TOKENIZER_MISMATCH = 2,
    REJECT_TEMPLATE_MISMATCH = 3,
    REJECT_ADAPTER_MISMATCH = 4,
    REJECT_NOT_READY = 5,
    REJECT_LEASE_EXPIRED = 6,
    PARTIAL_ATTACH_PLAN = 7,
    COORDINATED_FALLBACK_RECOMPUTE = 8,
    REJECT_GENERATION_MISMATCH = 9,
    REJECT_PROTOCOL_MISMATCH = 10,
    REJECT_TIMEOUT = 11
};
```

### 2.3 六维校验物理边界与仲裁准则

在执行 6 维语义校验时遵循以下准则：
- 模型、Tokenizer、模板及适配器的比对严格遵循规范化字节流的一致性；
- `ready_bit` 未就绪（`false`）时，优先于租约过期抛出 `REJECT_NOT_READY`，确保半写块在第一时间被拦截；
- 时间戳校验采用 `now_ms >= lease_expire_ms`，达到或超过到期时间戳的租约视为已失效；
- `ready_bit` 的读写具备原子内存序（Release 写入、Acquire 读取），确保在多核与异构设备间的内存可见性；
- 发生代际 `generation` 不匹配时，拒绝消费并回退至本地重算。

### 2.4 张量并行 Rank 状态机设计

张量并行 TP=8 各 Rank 遵循以下状态机流转：

```text
[INIT 初始化]
     │
     ▼
[ELIGIBILITY_CHECKING 6 维校验中]
     │
     ├─► 校验通过 ──► [RANK_READY] ─────┐
     └─► 校验失败 ──► [RANK_REJECTED] ──┤
                                        ▼
                             [CONSENSUS_WAIT 等待全局共识]
                                        │
             ┌──────────────────────────┴──────────────────────────┐
             ▼                                                     ▼
[ALL_RANKS_LOAD 全卡加载]                             [ALL_RANKS_FALLBACK 全卡协同重算]
             │                                                     │
             └──────────────────────────┬──────────────────────────┘
                                        ▼
                             [COLLECTIVE_ENTER 进入集合通信]
                                        │
                                        ▼
                             [COLLECTIVE_DONE 通信完成]
                                        │
                                        ▼
                             [CLEANUP 共享内存与上下文清理]
```

---

## 3. 测试用例矩阵与冻结参数

### 3.1 八类核心测试用例集

| 用例 ID | 注入冲突或异常场景 | 预期标准返回结果 | 当前仓库支持状态审计 |
|---|---|---|---|
| **C01** | 注入 `model_version` 不匹配 | `REJECT_MODEL_MISMATCH` | Harness 已定义；C++ 函数完整支持 |
| **C02** | 注入 `tokenizer_hash` 词表指纹不匹配 | `REJECT_TOKENIZER_MISMATCH` | Harness 已定义；C++ 函数支持字符串比较 |
| **C03** | 注入 `template_hash` 模板指纹不匹配 | `REJECT_TEMPLATE_MISMATCH` | Harness 已定义；C++ 函数支持字符串比较 |
| **C04** | 注入 `adapter_id` LoRA 适配器不匹配 | `REJECT_ADAPTER_MISMATCH` | Harness 已定义；C++ 函数完整支持 |
| **C05** | 注入 `ready_bit = false` 半写脏块未就绪 | `REJECT_NOT_READY` | Harness 已定义；C++ 函数完整支持 |
| **C06** | 注入 `now_ms` 超过租约到期时间戳 | `REJECT_LEASE_EXPIRED` | Harness 已定义；C++ 函数完整支持 |
| **C07** | 前缀 Token 匹配但尾部语义冲突 | `PARTIAL_ATTACH_PLAN` | Harness 已定义；当前 C++ 原型尚未提供部分挂载拆分接口 |
| **C08** | TP=8 注入单卡状态分歧 (7 命中 / 1 拒绝) | `COORDINATED_FALLBACK_RECOMPUTE` | Harness 已定义；当前 C++ 原型尚未集成跨进程多卡协议 |

### 3.2 TP=8 状态同步场景矩阵

| 评测场景分类 | 各 Rank 状态分布特征 | 预期全卡统一步调 | 所需底层客观证据 |
|---|---|---|---|
| 全量命中场景 | 8/8 Rank 全部通过校验 | 8 卡同步执行缓存加载 | 8 卡同一 Epoch 记录、统一下发加载指令、集合通信平稳完成 |
| 单卡分歧场景 | 7 卡通过 / 1 卡发生语义拒绝 | 8 卡步调一致回退重算 | 状态分歧被瞬间捕获、全量放弃加载、无单卡擅自进入孤立路径 |
| 慢卡超时场景 | 7 卡准时 / 1 卡执行严重超时 | 8 卡在超时截断后统一重算 | 看门狗超时中断、超时原因归因、各卡在超时后平稳收敛 |
| 进程崩溃场景 | 7 卡存活 / 1 卡发生突发终止 | 7 卡安全捕获并报错退出 | 进程心跳监测日志、死锁破除、共享内存资源被安全释放 |
| Epoch 错位场景 | 新旧不同轮次的位图混用 | 拒绝旧轮次并重新同步 | Epoch 冲突拦截日志、代际隔离验证、共享内存原子重置 |
| 协议版本冲突 | 节点间协议版本 A/B 冲突 | 阻断消费并显式报错 | 协议版本握手日志、安全拒绝凭证 |

### 3.3 物理测试参数严格冻结

| 测试维度 | 正式实施计划标准 | 工程说明与约束 |
|---|---:|---|
| 张量并行规模 (`world_size`) | 严格 $= 8$ | TP=8 结论基于 8 个独立物理 Rank 采集 |
| 正确性测试轮次 | 每个用例至少重复 100 轮 | 检验边界拦截的稳定性与进程复用安全性 |
| 状态同步测试轮次 | 10000 轮起步，正式目标 100000 轮 | 获取高精度的微秒级 P99 尾部时延分布 |
| 同步超时时限 (Timeout) | 基准设定 $500\mu\text{s}$ | 超时门限写入测试元数据清单 |
| CPU 核心亲和性绑定 | 8 个 Rank 绑定至独立 CPU/NUMA 核心 | 消除操作系统调度争用对时延的干扰 |
| 候选准入门槛 | 校验 P99 $< 5\mu\text{s}$；同步 P99 $< 100\mu\text{s}$ | 候选工程门限 |

---

## 4. 工具审计、实际命令与最小实现增量

### 4.1 当前构建入口与文件清单

当前受控工程目录构建命令：

```bash
cd ./原型验证代码/PVT-06
make clean && make -j2
```

### 4.2 当前基准程序行为审计

当前受控 `rank_consensus_bench.cc` 尚未实现命令行参数解析，内部采用固定常量运行：
- 校验循环轮次硬编码为 `100000`；
- 状态同步循环轮次硬编码为 `10000`；
- 每 100 轮在单进程栈内存中将局部变量从 `0xFF` 修改为 `0x7F`；
- 统计耗时在局部执行时间基础上固定累加常数 `18.5`；
- 输出结果写入当前目录的 `res_consensus_summary.csv`；
- 输出 CSV 状态标记为 `DEMO,DEMO_ONLY`。

### 4.3 当前测试脚手架行为审计

当前受控 `test_correctness.py` 支持 `--sut-command <command>` 与 `--out <path>` 参数。在未提供被测命令时，脚本不实际调用被测程序。传入被测命令后，脚本通过标准输入输出交互比对返回状态。

### 4.4 面向 LAB/MEASURED 的最小工程增量

正式评估前需补齐以下工程增量：
1. **语义校验接口补齐**：为 `ConsumeEligibility` 补齐部分挂载计划、Generation 代际校验及协议版本接口；
2. **跨进程共享内存实现**：将 Ready 标志位、Consensus Epoch 及 8-bit 原子位图挂载至 `/dev/shm` POSIX 共享内存；
3. **多进程 Rank 编排启动器**：开发支持拉起 8 个独立物理进程的启动器，实现独立日志记录与 Rank ID 绑定；
4. **协同决策与通信对接**：使共识决策真正驱动下游的缓存加载与重算分支，并对接底层集合通信同步屏障；
5. **全场景故障注入工具**：实现针对慢卡超时、进程崩溃、Epoch 错位及协议版本冲突的自动化注水工具；
6. **全链路原始事件归档**：实现基于全量逐次原始样本的 P50/P95/P99 统计工具。

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结源码版本、测试用例集与结果目录

- **操作意图**：确保评测数据可追溯至唯一的源码版本、用例集与测试环境。
- **执行命令**：

```bash
run_id="PVT-06-$(date +%Y%m%d-%H%M%S)-eligibility"
result_dir="results/pvt06/${run_id}"
mkdir -p "${result_dir}"
git rev-parse HEAD > "${result_dir}/git_commit.txt"
date --iso-8601=ns > "${result_dir}/timestamp.txt"
git status --short > "${result_dir}/git_status.txt"
```

### 步骤 1：编译并运行当前 DEMO 确认边界

- **操作意图**：验证当前 C++ 代码可编译构建，并将单进程 DEMO 的输出规范归档。
- **执行命令**：

```bash
cd ./原型验证代码/PVT-06
make clean && make -j2
./rank_consensus_bench > "../../../../results/pvt06/${run_id}/demo_stdout.txt" 2> "../../../../results/pvt06/${run_id}/demo_stderr.txt"
cp res_consensus_summary.csv "../../../../results/pvt06/${run_id}/res_consensus_summary_demo.csv"
```

- **应观察现象**：导出的 CSV 标记 `DEMO,DEMO_ONLY`。

### 步骤 2：运行未接入 SUT 的 Harness 确认防伪造边界

- **操作意图**：验证测试脚手架在未接入被测适配器时能够显式报告未执行。
- **执行命令**：

```bash
python3 ./test_correctness.py --out "../../../../results/pvt06/${run_id}/correctness_not_executed.json"
```

- **应观察现象**：导出的 JSON 中 `evidence_level` 标注为 `DEMO`，`status` 标注为 `NOT_EXECUTED`，`executed_cases` 为 0，脚本以状态码 2 退出。

### 步骤 3：接入最小 SUT 适配器执行 C01~C06 正确性实测（条件步骤）

- **前置条件**：已开发并接入基于 stdin/stdout JSON 协议的被测适配器。
- **操作意图**：使每一个基础语义冲突用例均真实调用底层校验引擎。
- **执行命令**：

```bash
python3 ./test_correctness.py \
  --sut-command "./consume_eligibility_adapter" \
  --out "../../../../results/pvt06/${run_id}/correctness_c01_c06.json"
```

### 步骤 4：补齐并执行 C07/C08 协议扩展用例（条件步骤）

- **操作意图**：验证长 Prompt 部分前缀挂载与单卡分歧协同回退的高阶协议动作。
- **执行动作**：针对 C07 验证匹配前缀与冲突尾部的拆分计划；针对 C08 验证单卡分歧下 8 个 Rank 是否均获得同一协同回退决策。

### 步骤 5：运行真实多进程 TP=8 状态同步（条件步骤）

- **前置条件**：`world_size=8` 多进程编排器就绪；`/dev/shm` POSIX 共享内存已打通。
- **操作意图**：在真实 8 个独立物理进程中验证原子位图状态发布、并发读取、决策收敛及后续集合通信。
- **执行动作**：拉起 8 个独立 Rank 进程，采集各卡共识时间戳、状态决策、超时事件、进程退出码及共享内存释放记录。

### 步骤 6：全场景故障注水与集合通信死锁防护（条件步骤）

- **操作意图**：确证协同回退与集合通信在发生单卡抖动、进程崩溃或版本错位时不会引发集群死锁。
- **执行动作**：分别向 Rank 7 注入语义拒绝、超时延迟、终止进程、旧 Epoch 错位及协议版本冲突；观测其余存活 Rank 是否能在明确超时后安全收敛并退出。

### 步骤 7：全链路统计、独立复核与证据归档

- **操作意图**：基于全量原始逐次样本精确计算时延分位数与正确性指标。
- **执行动作**：在 `results/pvt06/<run_id>/` 目录下完整归档 `correctness_c01_c08.json`、`rank_events.jsonl`、`consensus_samples.csv`、`collective_events.jsonl`、`process_exit.csv`、`timeout_and_faults.jsonl`、`shm_cleanup.log` 及 `summary.md`。

---

## 6. 数据采集清单与记录格式

### 6.1 语义校验正确性原始记录

```json
{
  "run_id": "<run-id>",
  "case_id": "C01",
  "conflict": "model_version",
  "injected_fields": {"model_version": "<value>"},
  "expected_status": "REJECT_MODEL_MISMATCH",
  "actual_status": "<sut-status-or-null>",
  "oracle_pass": true,
  "wrong_consume": false,
  "sut_executed": true,
  "evidence_level": "LAB",
  "invalid_reason": null
}
```

### 6.2 多卡 Rank 原始事件字段

```csv
run_id,consensus_epoch,protocol_version,world_size,rank,event,local_result,bitmap_or_state,decision,ts_ns,timeout_us,peer_count,collective_enter_ts_ns,collective_done_ts_ns,exit_code,error
```

### 6.3 汇总指标 CSV 模板

```csv
run_id,case_set,world_size,total_cases,executed_cases,oracle_pass_count,wrong_consume_count,timeout_count,deadlock_count,decision_disagreement_count,eligibility_p50_us,eligibility_p95_us,eligibility_p99_us,consensus_p50_us,consensus_p95_us,consensus_p99_us,planned_path,actual_path,evidence_environment,evidence_level,status,invalid_reason
<PVT-06>,<case_set>,8,<total>,<executed>,<pass_count>,<wrong_count>,<timeout_count>,0,0,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<planned>,<actual>,<W0_OR_W1_OR_W2>,<DEMO_OR_LAB_OR_MEASURED>,<status>,<null_or_reason>
```

---

## 7. 候选准入门槛、判定规则与立即止损机制

### 7.1 候选工程准入门槛

| 评估维度 | 候选工程准入门槛 | 权威取证要求 |
|---|---:|---|
| 基础语义冲突拦截率 | C01~C06 每个用例正确拦截率严格 $= 100\%$ | 每一个用例均实际调用 SUT，由独立 Oracle 严格对账 |
| 部分前缀安全挂载 | C07 仅安全挂载匹配前缀，尾部明确增量重算 | 具备 Token 范围、显存物理基地址、动作日志及输出校验 |
| 多卡决策一致性 | C08 及全场景故障注水下全卡决策分歧数严格 $= 0$ | 同一 Epoch 下全量 8 个 Rank 均具备自洽日志凭证 |
| 6 维语义校验耗时 | 单次 6 维语义校验耗时 P99 $< 5\mu\text{s}$ | 全量逐次原始采样，固定 CPU 绑核与编译优化 |
| 多卡状态同步耗时 | TP=8 多卡状态同步耗时 P99 $< 100\mu\text{s}$ (RankConsensus) | 真实跨进程共享内存或底层通信栈实测 |
| 集合通信死锁发生率 | 全场景故障注水下集合通信死锁数严格 $= 0$ | 进程树监控、通信完成中断日志或显式异常返回 |
| 陈旧脏数据错误消费 | 全量测试场景下陈旧失效数据错误消费数严格 $= 0$ | 具备正文数据实际消费动作与 Oracle 的逐条对账 |

### 7.2 状态判定枚举与规则

- **GO（全链路证据闭环）**：基础正确性、TP=8 多卡共识、故障协同回退及微秒时延门限均具备完整 `MEASURED` 证据链；
- **CONDITIONAL（局部拓扑或参数达标）**：正确性校验通过，但仅在特定拓扑、受限规模或特定超时配置下成立；
- **NO-GO（发生错误消费或死锁）**：发生陈旧数据错误消费、Rank 决策出现分歧、集合通信发生死锁或出现未受控超时；
- **NOT-SUPPORTED（物理环境未支持）**：现场缺乏 TP=8 硬件加速卡、跨进程共享内存或 SUT 适配器；
- **INVALID-EVIDENCE（无效证据）**：用例未实际执行、Oracle 缺失、静态常数冒充实测或时间线不完整。

### 7.3 立即安全止损条件

在测试过程中凡触发以下任一异常，立即终止测试并保存现场：
- 任何语义不兼容、未就绪或已过期的 KVCache 数据被实际接入在线推理计算；
- 8 个 Rank 在同一 Epoch 内产生不同的加载与重算决策，引发多卡状态分裂；
- 任意 Rank 发生无限等待、进程无法正常退出或共享内存残留资源泄漏；
- 旧 Epoch、旧代际 Generation 或未 Ready 的半写数据被下游算子读取消费；
- 测试脚手架捕获到 SUT 异常报错却依然将其统计为拦截成功；
- 底层集合通信库抛出未恢复的严重通信错误或设备上下文崩溃。

---

## 8. 执行阶段划分与交付闭环

测试实施划分为四个演进阶段：

| 实施阶段 | 核心攻坚内容 | 阶段必须交付物 | 准出判定条件 |
|---|---|---|---|
| E0（契约与用例规范确认） | 核对 C01~C08 用例定义、结果枚举、Oracle 判定逻辑及无效证据规则 | 源码审计报告、Harness 架构图、DEMO 输出 JSON | 确认当前代码边界，杜绝将测试桩冒充实测 |
| E1（语义校验与基础正确性） | 接入真实 SUT 适配器，全量完成 C01~C06 真实逐条校验，明确 Ready/Lease 边界 | 逐用例测试日志、Oracle 对账表、错误消费审计报告 | 证实 6 维语义校验 100% 正确拦截且 0 错误消费 |
| E2（部分挂载与多卡协议） | 实现 C07 部分挂载、C08 协同回退、Epoch 代际隔离、共享内存及 8 个独立 Rank | 状态机流转日志、共享内存通信报告、多卡分歧对账表 | 证实 TP=8 多卡在分歧场景下统一平稳回退 |
| E3（集合通信与故障混压总门禁） | 在真实 TP=8 运行时接入集合通信，注入慢卡、进程崩溃、版本冲突及超时异常 | 死锁监控日志、全场景故障注水报告、最终判定结论 | 证实死锁数严格为 0，同步时延 P99 $< 100\mu\text{s}$ |
| 条件证伪（共享内存协议边界） | 针对单进程位图、POSIX 共享内存及设备侧直接通信开展严格对比证伪 | 跨卡通信机制对照表、边界评估报告 | 确立纯软主路径，若硬件不支持则规范标记未支持 |

---

## 9. 工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师核心职责**：
  1. 确认现场部署的 8 卡加速器硬件环境、驱动版本、POSIX 共享内存权限及集合通信运行时；
  2. 固化模型结构、Tokenizer 词表、Prompt 模板、LoRA 适配器、超时门限及安全止损红线；
  3. 实际启动/停止多进程启动器与测试脚手架，完整保存系统 dmesg 日志、死锁监测数据及进程退出码；
  4. 严格审定 `wrong_consume_count=0` 是否确由真实数据消费与独立 Oracle 共同闭环证明；
  5. 对语义一致性与多卡状态同步是否达到生产准入标准承担最终技术把关责任。
- **AI Agent 协同职责**：
  1. 研读方案设计、公共测试契约及原型源码，梳理实际支持的 CLI 参数、结构体定义及当前未实现特性；
  2. 编写 SUT 适配器封装、独立 Oracle 对账脚本、多进程时间线解析、分位数统计及标准证据包生成工具；
  3. 严格核验各用例的实际执行状态、`planned_path` 与 `actual_path` 路径一致性及状态枚举归一化；
  4. 严守技术诚信红线，严禁虚构 SUT 输出、伪造多卡同步耗时或将未执行用例篡改为测试通过。

### 9.2 可直接复制给 Coding Agent 的 Prompt 模板

```text
我正在执行 PVT-06：ConsumeEligibility 与 RankConsensus 语义一致性与 TP=8 多卡状态同步验证。

请先研读以下核心文件：
1. ./提前验证方案设计/验证计划方案设计/07_PVT-06_ConsumeEligibility与RankConsensus0错误消费验证实施方案设计.md
2. ./提前验证方案设计/验证计划方案设计/Benchmark公共契约与证据分级规范.md
3. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-06/Makefile
4. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-06/consume_eligibility.h
5. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-06/consume_eligibility.cc
6. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-06/rank_consensus_bench.cc
7. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-06/test_correctness.py

执行约束与任务要求：
- 首先梳理源码实际支持的 CLI 参数与底层执行行为；确认 consume_eligibility.cc 当前仅做 6 个字段的基础比较，rank_consensus_bench.cc 是单进程 DEMO（局部变量位图与固定 18.5µs 偏移不代表真实 TP=8），且 test_correctness.py 仅在传入 --sut-command 时才调用被测程序。
- 执行 C01~C08 用例时，必须确保每个用例均实际调用真实的 SUT 适配器，并由独立 Oracle 比对 expected 与 actual；若 C07 部分挂载或 C08 多卡协同回退接口尚未实现，必须规范标记为 NOT-SUPPORTED 或 INVALID-EVIDENCE，严禁伪造通过。
- TP=8 多卡实验必须真实记录 8 个独立物理 Rank 的事件时间线、决策状态、进程退出码、共享内存清理及集合通信完成中断。
- 区分 planned_path 与 actual_path；所有 P99 时延、错误消费数及死锁数必须从原始逐次采样样本中计算。
- 未采集到的字段显式置为 null 并详细注明 invalid_reason；完整留存硬件异常与失败日志；将脚本状态规范归一化为公共契约枚举。
- 最终输出：源码能力核验矩阵、实际执行命令清单、8 类用例对账表、多卡共识审计表、未支持特性清单以及下一步最小代码重构建议。
```

### 9.3 常见排错指南

- **正确性测试结果显示 `status=NOT_EXECUTED`**：未向脚本传入 `--sut-command` 参数；需接入可审计的被测系统适配器。
- **C07 或 C08 用例执行无法通过**：受控 C++ 源码中尚未实现 Partial Attach 拆分或多卡协同协议；需先补齐底层接口与状态机模型。
- **运行命令传入 `--ranks` 或 `--out` 未改变程序输出**：受控基准程序未集成 CLI 参数解析；应记录为 `DEMO` 并在重构中补齐参数解析。
- **测得的共识耗时 P99 等于某一固定常数**：代码中仍在使用 `+18.5` 静态偏移模拟同步开销；必须采集真实的跨进程共享内存同步事件。
- **执行测试提示 `/dev/shm` 无法复用或残留报错**：排查共享内存所有权管理、超时自动清理及进程退出清理 Handler。
- **注入单卡分歧后其余存活 Rank 依然执行缓存加载**：说明多卡共识决策未能真正驱动下游分支执行；必须将全卡一致决断作为唯一的准入分支门。
- **租约时间戳与当前时间完全相等时被放行**：核对代码中是否采用了 `>` 判断而非 `>=`。
- **测试报告将未执行用例统计为 0 错误消费**：未实际执行的用例该字段必须保持为 `null`，汇总判定为 `INVALID-EVIDENCE`。
- **测试中无死锁发生但未捕获到集合通信事件**：测试仅执行了上层位图决策，未实际调用底层通信算子；该测试点仅能证明共识逻辑。
