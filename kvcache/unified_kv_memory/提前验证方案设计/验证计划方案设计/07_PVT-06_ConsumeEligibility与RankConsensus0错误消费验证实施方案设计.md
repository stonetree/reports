# PVT-06：ConsumeEligibility 与 RankConsensus 语义一致性与 TP=8 多卡状态同步验证实施方案设计
## —— 6 维语义资格校验与多卡协同验证：TP=8 共识、统一回退与集合通信安全

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个 `run_id` 必须冻结模型、Tokenizer、Prompt 模板、适配器、租约、Ready 状态、Rank 数、用例集合、超时、代码包和证据等级；结果必须保留逐用例 Oracle 对账、每个 Rank 的事件时间线、集合通信完成状态、错误消费、超时、退出码、`planned_path`/`actual_path` 和结论状态。脚本内部的 `PASS`、`FAIL`、`NOT_EXECUTED` 不得直接当作工程准入结论。

> **验证范围声明**：当前受控工程中的 PVT-06 原型只实现 6 个字段的基础比较和单进程位图演示；`rank_consensus_bench.cc` 没有共享内存、多进程 Rank、真实集合通信或真实共识计时，`test_correctness.py` 只有提供 `--sut-command` 才会调用被测接口。因此当前代码只能示范接口、用例和失败关闭流程，不能单独证明 TP=8 多卡一致性、部分前缀挂载、集合通信安全或错误消费已被消除；缺少真实 SUT、独立 Oracle、完整 Rank 时间线和原始日志的结果只能标记为 `DEMO`/`LAB`。

> **术语速查**：KVCache（大模型注意力键值缓存，即自回归生成过程中保存历史 Key 和 Value 激活状态、避免后续 Token 重复计算注意力）；ConsumeEligibility（消费资格校验引擎，即在读取命中 KV 前检查模型、Tokenizer、模板、适配器、Ready 和租约等条件）；RankConsensus（张量并行多卡状态同步机制，即让多个 Rank 对加载、回退和集合通信采用一致决策）；Tokenizer（分词器，把 Prompt 转成 Token ID 序列）；Prompt 模板（组织 system/user/assistant 消息的固定格式）；Ready Bit（写入就绪位，表示 KV 已完成写入和可见性处理）；Lease（租约，表示对象或视图在有效期内仍由提供方维护）；Generation（对象代次号，用于防止旧完成事件覆盖新映射）；Oracle（独立期望结果判定器，不复用被测判断逻辑）；SUT（System Under Test，被测系统）；POSIX 共享内存（跨进程共享内存机制，`/dev/shm` 是 Linux 常见挂载位置）；集合通信（多个 Rank 共同参与的 AllReduce/AllGather 等通信操作）；Coordinated Fallback（协同回退，即任一 Rank 失败时所有 Rank 统一进入本地重算）；Partial Attach（部分挂载，即只消费已确认完整且语义匹配的前缀 KV）；TPOT（Time Per Output Token，每个输出 Token 的生成耗时）；P99（延迟分布中 99% 请求不超过的分位值）。

> **验证 ID**：PVT-06
> **验证名称**：ConsumeEligibility 消费资格校验与 RankConsensus 多卡状态同步验证
> **验证优先级**：**🔴 P0 级（核心关键项）**
> **对应验证阶段**：**E1（核心数据路径打通与多卡状态同步）**
> **证伪标记**：否（可消费性与多卡协同正确性确认）
> **建议周期**：5~6 人日
> **主关联 IR**：`IR-01-10`, `IR-01-11`, `IR-02-01`, `IR-02-05`
> **核心 SRS / SR23 锚点**：
> - SRS：`L2-KV-AttachHandle-034`, `L2-KV-PartialAttachPlan-038`, `L3-MS-ConsumeEligibility-060`, `L3-CO-VisibilityReadyBitmap-064`, `L1-PD-RankConsensus-013`
> - SR23：`SR23-01-10-01`, `SR23-01-11-01`, `SR23-02-01-01`, `SR23-02-05-01`, `SR23-02-05-02`, `SR23-02-10-01`
> **配套源码**：[`提前验证方案设计/验证计划方案设计/原型验证代码/PVT-06/`](file:///d:/codes/reports/kvcache/unified_kv_memory/提前验证方案设计/验证计划方案设计/原型验证代码/PVT-06)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`；vLLM `842dd8fd96650063e1ad32e6075742d457d39773`。正式结果必须绑定实际 SUT、通信栈、驱动/运行时和配置哈希。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 为什么语义校验和多卡共识必须同时验证

KVCache 不是只由 Prompt 文本决定的字节数组。模型架构、Tokenizer、Prompt 模板、适配器和写入生命周期都会影响它是否可消费。只校验一个缓存键或只比较对象 ID，可能把内容相似但语义不兼容的 KV 接入当前计算。

多卡场景还多一个控制面约束：8 个 Rank 必须在进入集合通信前对“加载缓存”还是“本地重算”作出同一个决定。一个 Rank 进入加载分支、另一个 Rank 进入重算分支时，后续集合通信的参与顺序和消息数量可能不同。最终是否挂起、是否超时以及是否能安全回退，必须通过多进程时间线和集合通信完成记录确认，不能由位图示意图直接推出。

### 0.2 六维语义与生命周期检查

目标检查顺序可冻结为：

```text
模型版本 -> Tokenizer 指纹 -> Prompt 模板指纹 -> LoRA 适配器
-> Ready 就绪位 -> Lease 租约有效性
```

每个失败原因必须有独立枚举，便于审计、告警和后续回退。Ready 与 Lease 不是语义字段的替代品；六项全部通过也不代表正文 DMA 或多卡共识已经完成。

### 0.3 Rank 共识决策模型

目标协议可以使用每个 Rank 一个位的状态集合，但需要额外冻结 epoch、world size、超时和协议版本：

```text
每个 Rank 独立完成六维检查
    -> 在当前 consensus_epoch 写入本 Rank 结果
    -> 等待全部 Rank 或超时
    -> 读取同一 epoch 的全局状态
    -> 全部通过：统一加载
       任一失败/超时/epoch 不一致：统一本地重算
    -> 以相同顺序进入后续集合通信
```

单个 `uint8_t` 位图不足以区分“上一轮残留位”“本轮尚未写入”和“某 Rank 进程退出”。生产实现至少需要：

- `protocol_version`、`world_size`、`consensus_epoch`；
- 每个 Rank 的结果位、写入时间、进程存活或心跳信息；
- 超时后的唯一决策和可观测原因；
- 清理上一轮状态的所有权和异常退出清理策略。

### 0.4 当前受控源码能力矩阵

| 文件 | 当前可确认行为 | 当前不能声称的能力 |
|---|---|---|
| `原型验证代码/PVT-06/consume_eligibility.h` | `SemanticMetadata` 只有 6 个字段：4 个字符串、租约时间戳、`bool ready_bit`；`CheckResult` 有 `PASS` 和 6 个拒绝枚举 | 没有 Token ID 序列、generation、部分挂载计划、原子 Ready 或哈希计算 |
| `原型验证代码/PVT-06/consume_eligibility.cc` | 按固定顺序比较模型、Tokenizer、模板、适配器，之后检查 Ready 和 `now_ms > lease_expire_ms` | 没有 xxHash64、原子内存序、租约边界专项处理或多卡协议 |
| `原型验证代码/PVT-06/rank_consensus_bench.cc` | 单进程循环执行校验；在栈上写 `volatile uint8_t rank_bitmap`；每 100 次改成 `0x7F`；固定加 `18.5`；写死输出文件名 | 没有 `/dev/shm`、多进程、Rank 参数解析、真实集合通信、故障进程或真实共识耗时 |
| `原型验证代码/PVT-06/test_correctness.py` | 定义 C01-C08；只有提供 `--sut-command` 时才通过 stdin/stdout 调用 SUT 并由脚本比较返回状态 | 默认运行不执行被测接口；当前目录没有默认 SUT；C07/C08 所需返回状态也未在 C++ 原型实现 |
| `原型验证代码/PVT-06/Makefile` | 使用 `g++ -O3 -std=c++17 -pthread -Wall` 构建 `rank_consensus_bench` | 没有 POSIX 共享内存、xxHash、NCCL/HCCL 或其他集合通信依赖 |

### 0.5 当前原型的证据边界

`rank_consensus_bench.cc` 中的 `p50/p99` 是对单进程局部代码段计时后再加固定常数的结果，不是 8 个进程同步的分位数。它的 `rank_bitmap` 每次循环都在当前进程创建，不能代表 Rank 之间共享。命令行传入的 `--ranks`、`--loops`、`--inject-divergence` 和 `--out` 当前不会改变程序行为，因为源码没有参数解析。

`test_correctness.py` 在未提供 `--sut-command` 时会把用例写入结果，但 `actual` 为 `null`、`executed_cases` 为 0，并返回退出码 2。即使提供了 SUT，它也只比较返回 JSON 中的 `status`，不会自行验证真实 KV 内容、集合通信完成或错误消费字节。因此正确性脚本是可复用的 Harness，不是完整的 TP=8 端到端测试。

## 1. 验证目标与交付物

### 1.1 验证目标

| 目标 | 需回答的问题 | 最低证据 |
|---|---|---|
| 六维校验正确性 | 8 类语义、生命周期和状态冲突是否逐项被拒绝或按协议回退 | 每个用例实际调用 SUT、独立 Oracle 结果、错误消费计数 |
| 正常消费 | 六项匹配且数据完整时是否允许消费 | 成功用例、版本/租约/Ready 快照、数据校验 |
| 部分挂载 | 前缀匹配而尾部不匹配时是否只挂载合法前缀并重算尾部 | Partial Attach 计划、前缀长度、尾部计算和输出校验 |
| 多卡决策一致 | TP=8 的 Rank 是否采用同一加载/回退动作 | 每个 Rank 的决策、epoch、超时、进程退出码 |
| 集合通信安全 | 分歧、进程慢、进程退出时，后续集合通信是否按时完成 | 通信开始/完成时间、错误码、死锁监控和超时日志 |
| 性能边界 | 六维检查和共识路径是否达到目标延迟 | 真实逐次样本、CPU 亲和性、P50/P95/P99 计算脚本 |

### 1.2 交付物

1. 8 类冲突用例与独立 Oracle 结果表；
2. 正常、边界、过期和异常退出场景的原始事件日志；
3. TP=8 每个 Rank 的共识时间线和集合通信完成报告；
4. 错误消费、分支不一致和死锁监控汇总；
5. DEMO/LAB/MEASURED 证据分级及 `GO/CONDITIONAL/NO-GO/NOT-SUPPORTED/INVALID-EVIDENCE` 判定。

## 2. 目标接口、状态机与正确性模型

### 2.1 目标语义元数据

当前 `SemanticMetadata` 可以作为最小原型输入，但生产实验需要扩展字段并固定序列化规则：

```cpp
struct SemanticMetadata {
    std::string model_version;
    std::string tokenizer_hash;
    std::string template_hash;
    std::string adapter_id;
    uint64_t lease_expire_ms;
    bool ready_bit;
    uint64_t generation;
    uint64_t object_id;
};
```

如果采用 xxHash64，必须记录哈希算法、seed、输入字节规范化方式和碰撞处理策略。哈希字段不能掩盖 Tokenizer 版本或词表来源；相同哈希字符串的比较只能说明元数据指纹相同，不代表正文数据已经完成校验。

### 2.2 目标返回结果

```text
PASS
REJECT_MODEL_MISMATCH
REJECT_TOKENIZER_MISMATCH
REJECT_TEMPLATE_MISMATCH
REJECT_ADAPTER_MISMATCH
REJECT_NOT_READY
REJECT_LEASE_EXPIRED
PARTIAL_ATTACH_PLAN
COORDINATED_FALLBACK_RECOMPUTE
REJECT_GENERATION_MISMATCH
REJECT_PROTOCOL_MISMATCH
REJECT_TIMEOUT
```

当前 C++ 只实现前 7 个结果中的 6 个拒绝原因和 `PASS`，不实现后 5 个目标结果。C07、C08 在 Harness 中定义了预期字符串，但当前被测库没有对应接口；在补齐前运行这些用例应标记为 `NOT-SUPPORTED` 或 `INVALID-EVIDENCE`，不能假定通过。

### 2.3 六维检查边界

至少冻结以下边界：

- 模型、Tokenizer、模板和适配器比较采用字节级还是规范化后比较；
- Ready 为 false 时是否优先于 Lease 过期返回；
- `now_ms == lease_expire_ms` 是否已经过期。当前代码使用 `now_ms > lease_expire_ms`，等值时会放行，需由协议明确是保留行为还是待修正边界；
- `ready_bit` 的写入和读取是否具备 acquire/release 或等价可见性保证；
- 元数据匹配但正文 checksum 失败时返回何种状态；
- generation 不一致时是否直接拒绝消费并进入重算。

### 2.4 Rank 状态机

```text
INIT
  -> ELIGIBILITY_CHECKING
  -> RANK_READY / RANK_REJECTED
  -> CONSENSUS_WAIT
  -> ALL_RANKS_LOAD
     或 ALL_RANKS_FALLBACK_RECOMPUTE
  -> COLLECTIVE_ENTER
  -> COLLECTIVE_DONE
  -> CLEANUP
```

任何 Rank 进入 `COLLECTIVE_ENTER` 前，必须存在同一 `consensus_epoch` 的全局决策。超时、epoch 不一致、world size 不一致或其他 Rank 退出，默认动作应为全体回退；但该动作是否真正执行，需要由每个 Rank 的事件日志和后续通信完成记录证明。

## 3. 用例矩阵与冻结参数

### 3.1 8 类用例

| 用例 | 注入差异 | 目标结果 | 当前仓库状态 |
|---|---|---|---|
| C01 | `model_version` 不同 | `REJECT_MODEL_MISMATCH` | Harness 有定义；C++ 函数支持 |
| C02 | `tokenizer_hash` 不同 | `REJECT_TOKENIZER_MISMATCH` | Harness 有定义；C++ 函数支持字符串比较 |
| C03 | `template_hash` 不同 | `REJECT_TEMPLATE_MISMATCH` | Harness 有定义；C++ 函数支持字符串比较 |
| C04 | `adapter_id` 不同 | `REJECT_ADAPTER_MISMATCH` | Harness 有定义；C++ 函数支持 |
| C05 | `ready_bit=false` | `REJECT_NOT_READY` | Harness 有定义；C++ 函数支持 |
| C06 | `now_ms` 超过租约 | `REJECT_LEASE_EXPIRED` | Harness 有定义；C++ 函数支持 |
| C07 | 前缀匹配、尾部不匹配 | `PARTIAL_ATTACH_PLAN` | Harness 有定义；当前 C++ 无 Partial Attach 接口 |
| C08 | 单 Rank 状态分歧 | `COORDINATED_FALLBACK_RECOMPUTE` | Harness 有定义；当前没有多 Rank 协议 |

每个用例都要包含一条正向配对样本，证明仅改变目标字段时结果发生预期变化。C01-C06 至少覆盖“一个差异”和“多个差异”的优先级；如果多个字段同时冲突，必须记录协议规定的返回优先级，不能把任意首个返回当作通用语义。

### 3.2 TP=8 共识矩阵

| 场景 | Rank 状态 | 预期统一动作 | 必须观测 |
|---|---|---|---|
| 全部匹配 | 8/8 eligible | 统一加载 | 8 个 Rank 同一 epoch、同一决策、集合通信完成 |
| 单 Rank 未命中 | 7 eligible/1 reject | 全体本地重算 | 分歧被发现，不能出现部分加载、部分重算 |
| Rank 慢到超时 | 7 timely/1 timeout | 全体本地重算 | 超时原因、决策收敛和进程退出状态 |
| Rank 进程退出 | 7 alive/1 exited | 全体安全回退或请求失败 | 监控、超时、资源清理、无永久等待 |
| epoch 混用 | 新旧轮次同时出现 | 拒绝旧轮次并统一处理 | epoch、generation、共享内存清理 |
| 协议版本不一致 | 版本 A/B | 拒绝消费并显式失败 | 协议版本、失败码和集合通信行为 |

### 3.3 冻结参数

| 参数 | 建议值 | 说明 |
|---|---:|---|
| `world_size` | 8 | 真实 TP=8 结论必须使用 8 个独立 Rank；缩小规模只能是 LAB |
| 正确性重复次数 | 每个用例至少 100 次 | 统计拦截一致性并覆盖进程复用 |
| 共识重复次数 | 10000 起步，正式目标 100000 | 由真实多进程实现解析参数并记录 |
| 超时 | 先 500µs，再按设备实际调整 | 超时值必须进入元数据，不能只写在代码注释中 |
| 运行方式 | 单请求、批量请求、混合压力 | 所有模式保留原始时间线 |
| CPU 亲和性 | 固定 Rank 到 CPU/NUMA | 记录绑定策略和实际线程 |
| 目标门限 | 校验 P99 < 5µs；共识 P99 < 100µs | 目标是门限，不是预置结果 |

## 4. 工具审计、实际命令与最小实现增量

### 4.1 当前构建入口

当前受控目录 `提前验证方案设计/验证计划方案设计/原型验证代码/PVT-06/` 的 Makefile 仅构建 `rank_consensus_bench`：

```bash
cd ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-06
make clean && make -j2
```

`make clean` 会删除该目录下的 `rank_consensus_bench` 和 `*.csv`，执行前应确认目录中没有需要保留的结果文件，并将结果写到独立目录。当前 Makefile 未链接 xxHash、共享内存封装或集合通信库。

### 4.2 当前 rank_consensus_bench 的真实行为

当前程序不解析命令行参数，所有运行参数均使用源码常量：

- Eligibility 循环固定为 `100000`；
- 共识循环固定为 `10000`；
- 每 100 次在当前进程内把局部变量从 `0xFF` 改成 `0x7F`；
- 局部耗时上固定加 `18.5`；
- 输出固定写入当前工作目录的 `res_consensus_summary.csv`；
- CSV 状态写为 `DEMO,DEMO_ONLY`。

因此，以下命令可以运行程序，但参数不会产生声明中的行为：

```bash
./rank_consensus_bench --ranks 8 --loops 100000 --out res_consensus_latency.csv
./rank_consensus_bench --ranks 8 --inject-divergence rank7 --out res_fallback_test.csv
```

这两条命令只能作为“参数未被解析”的审计样例，不得作为 TP=8 或故障注入证据。真实实验前需补齐参数解析、输出路径、进程编排、共享内存和故障注入实现。

### 4.3 当前 test_correctness.py 的真实接口

当前脚本只有两个参数：

```text
--sut-command <command>
--out <path>
```

不提供 `--sut-command` 时，脚本不调用任何被测程序。提供后，脚本会把每个用例 JSON 写入 SUT 的 stdin，读取 stdout JSON 的 `status` 字段，并与 `expected` 比较。当前脚本不负责把自然语言错误、空 stdout、错误 JSON 或真实数据校验转换成完整工程证据；这些情况应在结果中记录为 SUT 错误或无效证据。

当前目录没有 `eval_consensus.py`。如果需要汇总脚本，应在实现后明确新增文件、输入 schema、统计公式和单元测试，不应继续在命令中引用不存在的脚本。

### 4.4 最小实现增量

进入 LAB/MEASURED 前至少需要完成：

1. 为 `ConsumeEligibility` 增加明确的部分挂载、generation 和协议版本接口，或者删去 C07/C08 的“已支持”表述；
2. 将 Ready、共享 epoch 和 Rank 状态放入真实跨进程共享区域，使用明确的原子内存序和清理策略；
3. 实现多进程 Rank 启动器，确保 8 个进程使用独立 rank ID、独立日志和统一配置；
4. 让共识决策真正影响后续模拟加载/重算分支，并接入可控的集合通信或等价同步栅栏；
5. 增加超时、进程退出、epoch 混用、协议版本不一致和单 Rank 分歧注入；
6. 增加独立评估工具，按原始逐次样本计算 P50/P95/P99、错误消费计数和死锁/超时计数；
7. 在真实 TP=8 环境中记录设备、驱动、运行时、CPU/NUMA、通信库和版本。

## 5. 逐步执行 SOP

### Step 0：冻结源码、用例和结果目录

操作意图：确保每次结果都能回到同一份实现和同一组用例。

执行动作：

```bash
run_id="PVT-06-$(date +%Y%m%d-%H%M%S)-eligibility"
result_dir="results/pvt06/${run_id}"
mkdir -p "${result_dir}"
git rev-parse HEAD > "${result_dir}/git_commit.txt"
date --iso-8601=ns > "${result_dir}/timestamp.txt"
git status --short > "${result_dir}/git_status.txt"
```

应观察现象：commit、时间戳和工作树状态均已落盘。

判定边界：如果 PVT-06 源码存在未记录的未提交修改，结果最多作为 `LAB`；无法确认源码版本时标记 `INVALID-EVIDENCE`。

### Step 1：编译并运行当前 DEMO，确认它的证据边界

操作意图：验证当前 C++ 工程能编译，同时把单进程 DEMO 的输出保存下来，避免误把它当作多卡结果。

执行动作：

```bash
cd ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-06
make clean && make -j2
./rank_consensus_bench > "../../../../results/pvt06/${run_id}/demo_stdout.txt" 2> "../../../../results/pvt06/${run_id}/demo_stderr.txt"
cp res_consensus_summary.csv "../../../../results/pvt06/${run_id}/res_consensus_summary_demo.csv"
```

路径应按实际当前工作目录调整，并把最终命令原样保存。若没有 `cp` 或结果目录不在相对路径上，使用等价的文件复制命令，不要改变程序输入输出语义。

应观察现象：终端输出 Eligibility 平均耗时、局部共识 P50/P99；CSV 标记 `DEMO,DEMO_ONLY`，文件名固定为 `res_consensus_summary.csv`。

判定边界：只能判定编译和 schema 演示成功。该步骤不能证明 8 个 Rank、共享内存、集合通信、共识 P99 或分歧回退成立。

### Step 2：运行未接入 SUT 的正确性 Harness，确认不会伪造通过

操作意图：验证脚本在没有被测适配器时明确报告未执行，而不是把空结果计为错误消费数为零。

执行动作：

```bash
python3 ./test_correctness.py --out "../../../../results/pvt06/${run_id}/correctness_not_executed.json"
```

应观察现象：JSON 中 `evidence_level` 为 `DEMO`，`status` 为 `NOT_EXECUTED`，`executed_cases` 为 0，`wrong_consume_count` 为空；脚本退出码为 2。

判定边界：如果上层报告把该结果当作 8 类用例通过，报告逻辑为 `INVALID-EVIDENCE`。

### Step 3：接入最小 SUT 适配器并执行 C01-C06

操作意图：让每个基础语义冲突真正调用被测接口，并由 Harness 独立比较返回状态。

执行动作：

1. 编写或接入一个 stdin/stdout JSON 适配器，明确输入字段到 `SemanticMetadata` 的映射；
2. 为 C01-C06 各生成一个只改变目标字段的输入；
3. 记录适配器版本、编译命令和输出 schema；
4. 使用实际 shell 命令传入 `--sut-command`。例如：

```bash
python3 ./test_correctness.py \
  --sut-command "./consume_eligibility_adapter" \
  --out "../../../../results/pvt06/${run_id}/correctness_c01_c06.json"
```

应观察现象：每个用例的 `actual.status` 与冻结的 `expected` 独立对账；适配器失败、stdout 非 JSON、超时或退出码非零都被记录，不得静默转成拒绝通过。

判定边界：当前仓库没有该适配器，直接执行上述示例会失败；这不是当前源码已经支持的命令。没有适配器时保持 `NOT-SUPPORTED` 或 `INVALID-EVIDENCE`。

### Step 4：补齐并执行 C07/C08

操作意图：验证部分挂载和单 Rank 分歧的协议动作，而不是只验证 6 个字符串字段。

执行动作：

1. C07 必须提供前缀 Token 序列、缓存 Token 序列、前缀 KV 字节范围、尾部 Token 数量和数据完整性结果；
2. C08 必须提供 world size、rank 状态、epoch、超时和统一决策输出；
3. 更新 SUT 适配器使其返回 `PARTIAL_ATTACH_PLAN` 或 `COORDINATED_FALLBACK_RECOMPUTE`，并把行动参数写入结果；
4. 用同一 Oracle 验证状态、前缀长度、尾部重算标志和统一 Rank 动作。

应观察现象：C07 不会把部分匹配错误地当成完整命中；C08 所有 Rank 得到同一回退动作，并带有同一 epoch。

判定边界：当前 C++ 库和单进程 DEMO 均没有这些能力；没有补齐接口和真实事件记录时，不能把 C07/C08 标记为通过。

### Step 5：运行真实多进程 Rank 共识

操作意图：把单进程位图演示替换为 8 个独立进程，观察每个 Rank 的写入、读取、决策和后续同步。

执行动作：

1. 冻结 `world_size=8`、协议版本、epoch、共享内存名称和超时；
2. 由启动器拉起 8 个进程，每个进程写独立日志并绑定 rank ID；
3. 使用真实 POSIX 共享内存或等价跨进程机制完成状态交换；
4. 采集共识开始/完成时间、状态决策、超时、进程退出码和共享内存清理结果；
5. 确认 `planned_path` 与 `actual_path` 均为跨进程路径，不能继续使用“局部变量位图”。

应观察现象：全命中时 8 个 Rank 同步进入加载；任一 Rank 拒绝或超时时，8 个 Rank 得到同一回退决策；测试结束后共享内存无残留。

判定边界：只有一个进程、只有线程内局部变量或没有 Rank 独立日志时，最多为 `DEMO`/`LAB`，不能进入 TP=8 `MEASURED` 汇总。

### Step 6：注入分歧、慢 Rank 和进程退出

操作意图：验证协同回退和集合通信不会依赖“所有 Rank 永远正常”的理想条件。

执行动作：

1. 让 Rank 7 返回不匹配；
2. 让 Rank 7 延迟超过超时；
3. 在共识等待阶段终止 Rank 7；
4. 注入旧 epoch 位图或协议版本不一致；
5. 观察剩余 Rank 是否统一回退、是否完成后续同步、是否在明确超时后退出；
6. 记录进程树、系统级死锁监控、通信库日志和共享内存清理。

应观察现象：分歧或超时时不出现一部分加载、一部分重算；集合通信要么按统一回退路径完成，要么按明确错误返回，不允许无限等待。

判定边界：如果没有后续集合通信或等价同步动作，不能声称“避免死锁”；只能说明位图决策本身发生了分支。

### Step 7：统计、复核与归档

操作意图：用原始逐次样本计算延迟和正确性，防止固定值、平均值或缺失样本掩盖长尾。

执行动作：

```text
results/pvt06/<run_id>/
  metadata.yaml
  command.txt
  git_commit.txt
  git_status.txt
  environment.txt
  topology.txt
  demo_stdout.txt
  demo_stderr.txt
  correctness_c01_c08.json
  rank_events.jsonl
  consensus_samples.csv
  collective_events.jsonl
  process_exit.csv
  timeout_and_faults.jsonl
  shm_cleanup.log
  summary.md
```

应观察现象：summary 中每个结论都能回指原始行；P99 使用逐次样本计算；正确性、死锁和性能结论分开列示。

判定边界：缺少 SUT 输出、Rank 时间线或集合通信完成证据时，不能输出 `GO`。

## 6. 证据字段、结果格式与统计

### 6.1 正确性结果

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

`wrong_consume=false` 只有在被测动作真正执行、消费资格被拒绝且没有后续正文接入时才可填写；如果 SUT 未执行，应使用 `null`，不能用 false 代替未知。

### 6.2 Rank 事件

```csv
run_id,consensus_epoch,protocol_version,world_size,rank,event,local_result,bitmap_or_state,decision,ts_ns,timeout_us,peer_count,collective_enter_ts_ns,collective_done_ts_ns,exit_code,error
```

事件至少包括 `eligibility_done`、`state_publish`、`consensus_read`、`decision`、`collective_enter`、`collective_done`、`cleanup` 和 `process_exit`。只保存最终 P99 不足以检查某个 Rank 是否提前进入集合通信。

### 6.3 汇总指标

```csv
run_id,case_set,world_size,total_cases,executed_cases,oracle_pass_count,wrong_consume_count,timeout_count,deadlock_count,decision_disagreement_count,eligibility_p50_us,eligibility_p95_us,eligibility_p99_us,consensus_p50_us,consensus_p95_us,consensus_p99_us,planned_path,actual_path,evidence_level,status,invalid_reason
```

统计规则：

- `oracle_pass_count` 只统计实际调用 SUT 的用例；
- `wrong_consume_count` 统计真实发生了正文消费但 Oracle 判定不允许的事件；
- `consensus_p99_us` 按一次共识轮次计算，不得将进程启动时间和多轮样本混为一个字段；
- `decision_disagreement_count` 按同一 `consensus_epoch` 的 Rank 决策比较；
- 超时和死锁分别计数，不能把“程序未输出”直接当成没有死锁。

### 6.4 证据分级

| 级别 | 允许内容 | 不允许内容 |
|---|---|---|
| `DEMO` | 单进程函数调用、固定 schema、位图逻辑示意 | 8 Rank 共识、集合通信安全、真实 P99 结论 |
| `LAB` | 多进程共享内存或模拟 SUT 的可复核实验 | 直接外推到真实 TP=8 设备和生产通信库 |
| `MEASURED` | 真实 TP=8、真实 SUT、完整时间线、故障注入和原始日志 | 关键 Rank 缺失、固定延时、未执行的用例或空值当零 |

## 7. 判定标准、无效证据与止损条件

### 7.1 候选门限

| 维度 | 候选要求 | 取证要求 |
|---|---:|---|
| 基础冲突拦截 | C01-C06 每个用例正确率 100% | 每个用例实际调用 SUT，Oracle 独立判定 |
| 部分挂载 | C07 只接入匹配前缀，尾部明确重算 | 有 Token 范围、KV 范围、动作日志和数据校验 |
| 多卡统一决策 | C08 及故障场景决策不一致数为 0 | 同一 epoch 下 8 个 Rank 均有日志 |
| 六维检查时延 | P99 < 5µs | 逐次测量，固定 CPU/编译/输入长度 |
| 共识时延 | TP=8 P99 < 100µs | 真实跨进程或通信栈路径，不能使用固定偏移 |
| 集合通信安全 | 死锁数为 0，超时有明确上界 | 进程监控、通信完成或显式错误返回 |
| 错误消费 | 错误消费数为 0 | 需要正文消费动作和 Oracle 对账 |

### 7.2 状态枚举

- `GO`：基础正确性、TP=8 共识、故障回退和延迟门限均有完整 `MEASURED` 证据；
- `CONDITIONAL`：正确性成立，但仅在限定拓扑、规模、通信库或超时配置下成立，必须写明条件；
- `NO-GO`：出现错误消费、Rank 决策分歧、集合通信死锁、旧代次消费或不可控超时；
- `NOT-SUPPORTED`：当前环境没有 TP=8 设备、跨进程共享内存、SUT 适配器或集合通信能力；
- `INVALID-EVIDENCE`：用例未执行、Oracle 缺失、固定值冒充实测、时间线不完整、退出码和日志缺失或空值被写成零/false。

### 7.3 无效证据规则

以下情况不能进入 `MEASURED` 汇总：

- 只运行 `test_correctness.py` 而未提供 `--sut-command`；
- 使用当前 `rank_consensus_bench` 的 `18.5` 固定偏移或 `0xFF/0x7F` 局部变量作为多卡结果；
- 命令中写了 `--ranks`、`--out` 或 `--inject-divergence`，但程序实际不解析这些参数；
- 只有一个汇总 P99，没有逐次样本和每个 Rank 事件；
- `actual_path` 只是复制 `planned_path`，没有真实路径观测；
- C07/C08 仍没有被测接口，却把 Harness 的 expected 字符串当作实际返回；
- `wrong_consume_count` 因未执行而被填为 0；
- 发生超时但没有进程状态、集合通信和共享内存清理日志。

### 7.4 立即止损条件

出现以下任一情况，应停止扩大重复次数并保留现场：

- 任何不匹配 KV 被实际接入当前推理；
- 8 个 Rank 在同一 epoch 产生不同加载/回退决策；
- 任一 Rank 无限等待、无法退出或共享内存残留不断累积；
- 旧 epoch、旧 generation 或未 Ready 数据进入消费；
- 测试工具报告 SUT 错误却继续统计为拦截成功；
- 集合通信库出现未恢复的通信错误或设备上下文异常。

## 8. 阶段推进与闭环

### E0：契约和用例前提确认

确认 C01-C08、结果枚举、Oracle、字段和无效证据规则。当前源码可以完成函数比较和 Harness 未执行路径的接口预演。

### E1：消费资格校验与基础正确性

接入 SUT 适配器，完成 C01-C06 的真实逐条校验，明确 Ready/Lease 边界和错误消费定义。

### E2：部分挂载与多卡协议

实现 C07 Partial Attach、C08 Coordinated Fallback、epoch/generation、共享内存和 8 个独立 Rank，完成全命中与单 Rank 分歧场景。

### E3：集合通信与故障混压

在真实 TP=8 运行时接入后续集合通信，注入慢 Rank、进程退出、版本不一致和超时，确认统一回退、资源清理和通信完成。

### 条件证伪：共享内存协议的边界

对单进程位图、POSIX 共享内存、设备侧通信、NCCL/HCCL 或其他集合通信栈逐项做替代实验。若某环境不支持所需跨进程或设备能力，应把结论限定为 `NOT-SUPPORTED`，不能用缩小规模的脚手架结果替代 TP=8 结论。

## 9. 研发人员与 AI Agent 执行约束

### 9.1 研发人员检查清单

- [ ] 先确认 `PROJECT_INDEX.md`、公共 Benchmark 契约和本文档版本；
- [ ] 记录 PVT-06 源码 commit、工作树状态、编译器和通信运行时；
- [ ] 区分函数比较、Harness、跨进程共识和集合通信四种证据层级；
- [ ] C01-C08 都有实际 SUT 输出或明确标为未支持；
- [ ] Ready、Lease、epoch、generation 和协议版本的边界已冻结；
- [ ] 每个 Rank 有独立日志，所有决策能按 epoch 对账；
- [ ] 错误消费、死锁、超时和进程退出状态没有用默认值掩盖；
- [ ] 统计使用逐次样本，P99 不使用固定常数或预置目标值；
- [ ] 结果包包含原始日志、命令、环境、汇总和无效证据原因。

### 9.2 AI Agent 执行提示词

```text
你负责执行 PVT-06 ConsumeEligibility 与 RankConsensus 验证。

先读取项目索引、公共 Benchmark 契约、本方案和原型目录。确认当前源码真实存在的文件、命令行和依赖。注意：consume_eligibility.cc 目前只做 6 个字段的顺序比较；rank_consensus_bench.cc 是单进程 DEMO，局部位图和固定 18.5µs 偏移不代表 TP=8；test_correctness.py 只有提供 --sut-command 才会调用被测接口；当前目录没有默认 SUT 和 eval_consensus.py。

执行 C01-C08 时，必须让每个用例调用真实 SUT，并由独立 Oracle 比较 expected 与 actual。C07 需要部分挂载计划，C08 需要真实多 Rank、epoch、超时和统一回退；如果能力不存在，标记 NOT-SUPPORTED 或 INVALID-EVIDENCE，不要伪造通过。

TP=8 实验必须记录 8 个 Rank 的事件时间线、决策、进程退出码、共享内存清理、集合通信完成状态和故障注入。区分 planned_path 与 actual_path。所有 P99、错误消费数和死锁数都必须从原始样本计算。最终只输出 GO、CONDITIONAL、NO-GO、NOT-SUPPORTED 或 INVALID-EVIDENCE，并解释证据缺口。
```

### 9.3 常见问题定位

| 现象 | 原因定位 | 处理方式 |
|---|---|---|
| 正确性结果 `NOT_EXECUTED` | 未提供 `--sut-command` | 接入可审计的 SUT；未接入时保持无效/未支持 |
| C07/C08 永远无法通过 | 当前 C++ 接口没有 Partial Attach 或多 Rank 协议 | 先补接口和事件模型，不修改 expected 伪造通过 |
| `--ranks`/`--out` 不改变输出 | 当前 bench 没有命令行解析 | 记录为 DEMO，补参数解析和独立输出路径 |
| 共识 P99 约等于固定值 | 使用了 `+18.5` 模拟同步成本 | 删除固定偏移，采集真实跨进程事件 |
| `/dev/shm` 无法复用上一轮 | 共享内存清理或 epoch 生命周期不完整 | 记录所有权、超时清理和异常退出清理 |
| 单 Rank 失败后其他 Rank 继续加载 | 共识决策没有真正控制后续路径 | 把统一回退作为唯一分支门，增加动作对账 |
| Lease 等于当前时间仍被放行 | 当前实现使用 `>` 而非 `>=` | 先冻结协议边界，再修改代码和回归用例 |
| 脚本把未执行计为零错误消费 | `wrong_consume_count` 的空值处理不严谨 | 未执行保持 `null`，汇总判为 `INVALID-EVIDENCE` |
| 没有死锁但也没有集合通信 | 测试只做到位图决策 | 只能报告共识演示，不能报告集合通信安全 |

本方案的完成标准不是“程序返回 0”，而是形成可复核证据链：六维元数据校验 → Oracle 对账 → 多 Rank 同一 epoch 共识 → 统一加载或回退 → 集合通信完成/显式失败 → 错误消费和长尾统计。
