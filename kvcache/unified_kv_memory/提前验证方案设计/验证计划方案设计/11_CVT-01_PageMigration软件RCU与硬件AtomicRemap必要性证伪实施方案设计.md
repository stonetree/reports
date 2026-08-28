# CVT-01：PageMigration 软件 RCU 与硬件 Atomic Remap 必要性证伪实施方案设计
## —— 显存页迁移安全与硬件依赖证伪：软件 RCU、NPU 异步引用与 Atomic Remap 对照

> **公共执行契约**：本项严格遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个测试 `run_id` 必须在运行前完整固化内存分配器类型、底层加速硬件、CPU/NUMA 绑核拓扑、页迁移调度策略、并发 Reader 读取线程数、迁移物理块尺寸 (Block Size)、迁移循环轮次、读写并发比例、故障注入点、代码包版本、配置哈希及目标证据等级；实测产出必须完整保留 Reader 进入/退出临界区日志、指针原子读取与 CAS 翻转时间戳、NPU Stream/DMA 事件、双层宽限期判定、旧内存块释放事件、xxHash64 校验码、Generation 代际版本、异常安全回滚轨迹、前台在线停顿样本及 TPOT 尾部时延。现场缺乏硬件 Atomic Remap 能力时必须严格标记为 `NOT-SUPPORTED`，严禁填入静态模拟数据；证伪结论必须严格限定在已验证的访问契约、底层硬件拓扑及显存迁移场景之内。

> **验证范围声明**：在当前受控的原型验证工程中，`rcu_migration_bench.cc` 仅在输出 CSV 中固定生成三类迁移方案的演示数据行，未创建真实的并发 Reader 线程、未分配物理显存 Extent、未执行数据跨页搬运、未调用昇腾 CANN/NPU 驱动运行时，亦未产生真实的数据一致性 Checksum 校验、在线停顿或异常回滚事件；`--hardware-supported` 仅用于切换内部演示输出分支，不可作为证明底层硬件具备重映射能力的依据。因此，现有受控源码仅用于检验输出格式与状态流转逻辑，不可直接作为证明生产级软件 RCU 安全可用、NPU 异步引用已闭环纳入宽限期、脏读/错误消费已归零或硬件 Atomic Remap 属于非必要依赖的依据；输出的 `0.08ms`、`3.8µs` 等静态预设数值统一限定标记为 `DEMO` 或 `LAB`。

> **术语速查**：
> - **PageMigration**：显存页迁移 / 显存碎片整理（将冷热 KVCache 数据从离散或低效的旧物理显存块复制搬运至连续的新块，并原子更新上层寻址映射的内存整理技术）；
> - **KVCache**：大模型注意力键值缓存（大模型自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免后续 Token 生成时重复计算注意力）；
> - **RCU**：Read-Copy-Update（读-拷贝-更新：一种无锁读与宽限期延迟释放机制，前台读者持续无锁访问旧视图，写者在后台拷贝新数据、原子切换指针并在确认所有读者均退出旧视图的宽限期后才安全释放旧内存，实现前台读请求零停顿）；
> - **Atomic Remap**：硬件原子重映射（由硬件 MMU/IOMMU 或加速卡固件提供的一步式原子地址映射翻转原语）；
> - **Reader**：在页迁移期间可能持有旧显存块引用的前台推理线程或 NPU 异步算子执行单元；
> - **Grace Period**：双层宽限期（所有可能持有旧显存块引用的 Host 线程与 NPU 异步硬件算子完全执行完毕前的安全等待窗口）；
> - **Extent**：连续物理显存数据区间；
> - **Generation**：代际版本号（用于防止陈旧失效的映射覆盖新分配显存的版本标识）；
> - **CAS**：Compare-And-Swap（比较并交换原子操作）；
> - **NPU**：Neural Processing Unit（神经网络处理器 / AI 加速芯片）；
> - **NPU Stream Event**：用于在硬件层面精确确认 NPU 异步算子计算或 DMA 搬运已彻底完成的硬件流事件同步原语；
> - **CANN/ACL**：华为异构计算架构及其驱动运行时接口；
> - **Checksum**：用于检测数据迁移前后绝对一致性的密码学/哈希校验码；
> - **Use-After-Free**：UAF，内存释放后非法使用（即算子或线程访问了已被后台回收释放的旧物理显存块引发的严重内存越界错误）；
> - **Stop-the-World**：STW，全局暂停（在迁移期间强制阻塞所有前台读者访问的传统强同步方案）；
> - **TPOT**：Time Per Output Token（单字生成延迟）；
> - **P99**：99 分位值（数据集中 99% 样本均优于该阈值的统计指标）；
> - **NOT-SUPPORTED**：物理环境未支持（测试环境或硬件不具备对应能力，严禁伪造实测）。

> **验证 ID**：CVT-01
> **验证名称**：页迁移/Defrag 软件 RCU 与硬件 Atomic Remap 原语必要性证伪
> **验证优先级**：**🟢 P2 级（拓展证伪项）**
> **对应验证阶段**：**条件证伪阶段（架构简化与去依赖）**
> **证伪标记**：**是（优先证伪“硬件 Atomic Remap 是显存碎片整理与页迁移的必需强依赖”）**
> **主关联 IR**：`IR-01-01`, `IR-01-11`, `IR-01-12`
> **核心 SRS / SR23 锚点**：
> - SRS：`L4-CO-AtomicRemapPrimitive-065`, `L3-CO-MigrationRCULock-090`
> - SR23：`SR23-01-01-03`, `SR23-01-11-01`, `SR23-01-12-02`
> **配套源码**：[`./原型验证代码/CVT-01/`](./原型验证代码/CVT-01/)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`。正式测试结果必须绑定实际显存分配器、NPU 运行时驱动、硬件事件日志及配置哈希。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 显存页迁移安全性的物理约束与第一性原理

在长时间高并发的大模型推理服务中，显存频繁的分配与释放会导致严重的显存碎片化，进而降低长上下文请求的并发承载能力。显存页迁移（PageMigration / Defrag）的核心目标是在线整理显存碎片，其物理安全性必须在底层严格闭环以下三个关键操作：

```text
1. 异步复制与校验 : 将旧物理区间 (OldExtent) 数据完整复制至新分配的物理区间 (NewExtent)，并严格校验新数据的一致性；
2. 原子指针翻转   : 通过微秒级原子操作 (CAS) 将活跃指针切换至 NewExtent，使后续新进入的 Reader 能够立即无锁访问新显存块；
3. 延迟安全释放   : 必须且只能在确认全量持有 OldExtent 引用的 Host 线程与 NPU 异步硬件算子全部退出后，方可真正回收释放 OldExtent。
```

在系统底层，仅仅证明指针翻转耗时达到微秒级，并不代表旧显存块已可安全释放；仅仅统计 Host 侧的 CPU 线程引用，也绝不代表 NPU 硬件流水线中的异步 Attention 算子已停止读取旧显存物理地址。若在 NPU 异步算子尚未执行完毕前过早释放旧显存块，将瞬间引发灾难性的 **Use-After-Free (UAF)** 内存越界破坏或推理计算输出乱码。

软件 RCU 机制的第一性原理，就是建立显式的“**Host CPU 临界区追踪 + NPU Stream Event 硬件流事件**”双层宽限期仲裁协议，将旧块释放安全推迟至所有硬件引用彻底清空之后，从而在纯软层面彻底消除前台停顿与内存安全隐患。

### 0.2 双层宽限期（Host + NPU）协同模型

```text
后台迁移线程在显存中分配 NewExtent
        │
        ├─► 异步将 OldExtent 数据搬运至 NewExtent 并执行 xxHash64 数据校验
        ├─► 记录当前 NPU Stream 硬件流事件标记 (Stream Event A)
        │
执行原子 CAS 操作: active_ptr (OldExtent ──► NewExtent)
        │
        ├─► 新发起的推理请求与算子立即读取 NewExtent (前台 0 阻塞、0 停顿)
        │
启动双层宽限期 (Grace Period) 等待
        │
        ├─► 第 1 层：等待指针翻转前已进入临界区的 Host Reader 全部主动退出
        └─► 第 2 层：等待底层 NPU Stream Event A 硬件完成中断上报 (确认旧地址算子已彻底算完)
        │
(双层宽限期全部满足)
        │
        ▼
将 OldExtent 标记为 RETIRED ──► 安全回收释放 OldExtent 显存块
```

该机制要求上层内存分配器、DMA 提交引擎与底层 NPU 运行时驱动共享统一的对象代次 (Generation) 与引用生命周期协议。

### 0.3 严密的科学证伪逻辑与结论适用边界

| 真实实验现象 | 科学严密支持的技术结论 | 严禁过度外推的结论边界 |
|---|---|---|
| 软件 RCU 真实实测全量通过 | 在已验证的硬件拓扑、驱动版本及双层引用协议下，纯软 RCU 方案完全满足微秒级停顿与数据安全门限，证明在此场景下**无需额外依赖专有硬件 Atomic Remap 原语** | 严禁无边界外推为“对所有异构芯片、所有黑盒 DMA 设备及任意超大规模集群均不需要硬件 Atomic Remap” |
| 软件 RCU 实测发生数据越界或长尾 | 说明当前软件双层宽限期或 NPU 事件监听实现存在工程缺陷，需紧急修复或评估硬件原语引入 | 不能草率断言“硬件 Atomic Remap 在任何场景下都属于绝对必需” |
| 硬件 Atomic Remap 现场不支持 | 确认现场硬件与驱动环境暂不具备对应硬件原语，规范标记为 `NOT-SUPPORTED` | 严禁在缺乏硬件对比的前提下空口宣称软件方案在性能上绝对碾压硬件方案 |

### 0.4 当前受控源码能力矩阵审计

| 源码文件与路径 | 当前受控源码实际行为 | 现阶段尚不能声称的能力 |
|---|---|---|
| `原型验证代码/CVT-01/rcu_migration_bench.cc` | 解析 `--hardware-supported` 与 `--out` 参数；固定输出 `STOP_THE_WORLD`、`SOFTWARE_RCU_COPY_ON_MIGRATE` 及硬件重映射的演示数据行；全部输出标记为 `DEMO,DEMO_ONLY` | 未创建并发 Reader 线程；未分配物理 Extent；未搬运数据；未调用昇腾 CANN 运行时；未采集真实停顿分位数或回滚事件 |
| `rcu_migration_bench.cc` | 源码中固定写入 `0.08ms` 停顿、`3.8µs` 读取耗时、`1.8%` TPOT 干扰率、`0` 错误读取及 `TRUE` 回滚安全等样例字段 | 输出数值均为静态常量，非实测采样值；`--hardware-supported` 仅为代码分支开关，不能替代底层硬件能力探测 |
| `原型验证代码/CVT-01/Makefile` | 采用 `g++ -O3 -std=c++17 -pthread -Wall` 构建单机演示程序 | 未链接昇腾 CANN/ACL 库、NPU 运行时驱动、xxHash 库或多线程压力测试框架 |
| CVT-01 源码目录 | 当前尚未受控提供 `verify_checksum.py` 或 `eval_cvt01.py` | 早期文档中提及的校验与评估脚本在当前仓库中尚未受控提供 |

### 0.5 当前输出格式与命令行边界

当前受控 C++ 程序未实现 `--mode`、`--readers`、`--migrated-mb` 或 `--loops` 等参数。CSV 输出中的停顿时间字段为 `p99_pause_ms`，读取延迟字段为 `p99_read_us`。

程序默认将 `HARDWARE_ATOMIC_REMAP` 输出为内部状态 `NOT_SUPPORTED`；传入 `--hardware-supported` 仅在内部输出静态演示数据，其证据等级仍为 `DEMO`。因此，当前原型仅用于规范输出格式，不可直接用于关闭 CVT-01 证伪门禁。

---

## 1. 验证目标、交付物与候选准入门槛

### 1.1 核心验证目标

| 验证核心维度 | 必须回答的物理与工程问题 | 必须具备的最低客观证据 |
|---|---|---|
| 读路径零停顿安全 | 在显存页迁移全生命周期内，并发 Reader 是否仅能读到完整一致的旧视图或新视图 | 逐 Reader、逐轮次的 xxHash64 校验码、Generation 代际及视图 ID 对账 |
| 显存延迟释放安全 | 旧 Extent 显存块是否在全量 Host Reader 与 NPU 异步算子彻底退出前绝对保持有效 | 原子引用计数、NPU Stream Event 硬件中断及显存释放时间戳对账 |
| 在线停顿削减收益 | 相比传统的全局暂停 (Stop-the-World)，软件 RCU 方案能否消除前台读者的阻塞停顿 | 全量逐次停顿时间采样样本、P50/P95/P99 权威统计分布 |
| 前台在线业务影响 | 后台异步显存页迁移对前台在线推理的 TPOT 尾部时延是否存在显著扰动 | 真实前台推理请求采样、混压全流程时间线及 TPOT 干扰率计算 |
| 全场景异常安全回滚 | 在数据拷贝失败、指针翻转异常或宽限期超时时，系统能否平稳回滚至一致状态 | 故障注入日志、旧/新状态流转记录、回滚确认及数据完整性校验 |
| 硬件依赖必要性证伪 | 在已打通的双层宽限期契约下，纯软方案是否已充分满足性能与安全门限 | 软件 `MEASURED` 证据链、硬件探测日志及客观技术边界声明 |

### 1.2 候选工程准入门槛

| 核心评估指标 | 候选工程准入门槛 | 权威取证要求 |
|---|---:|---|
| 前台 Reader 停顿时间 | 软件 RCU 模式下前台 Reader 读取停顿 **P99 $< 1	ext{ms}$** | 全量逐次采样样本计算，严禁使用单次最大值替代 |
| 前台在线 TPOT 干扰率 | 显存页迁移期间前台推理 **P99 TPOT 干扰恶化率 $< 3\%$** | 严格基于同一增强代码包在无迁移干扰下的纯前台基线对账 |
| 数据正确性与内存安全 | 全量迁移过程中 **0 脏读、0 UAF 内存越界、0 错误消费** | 覆盖 Host CPU 线程与 NPU 异步算子的全量一致性校验 |
| 异常安全回滚可靠性 | 全场景故障注水下 **0 内存泄漏、0 脏块放行、回滚后 Checksum 100% 正确** | 逐故障点注入测试与 Oracle 对账 |
| 硬件能力客观状态 | 客观探测底层硬件 Atomic Remap 支持状态，不支持时标记 `NOT-SUPPORTED` | 严禁使用模拟数据伪造硬件实测结论 |

### 1.3 阶段交付资产

每个正式的 `run_id` 必须至少交付以下结构化资产：

1. **《Stop-the-World vs 软件 RCU vs 硬件重映射同场景性能对比报告》**：涵盖停顿耗时、读取时延及前台 TPOT 干扰率；
2. **《Host 线程与 NPU 异步算子引用全量事件时间线表》**：详细记录进入/退出临界区、CAS 翻转、Stream Event 中断及显存释放时间戳；
3. **《数据完整性与内存安全审计日志》**：包含 xxHash64 校验码、Generation 版本比对、UAF 监测及防脏读审计；
4. **《全阶段故障注水与安全回滚报告》**：记录拷贝阶段、翻转阶段及宽限期阶段注入异常后的安全回滚轨迹；
5. **标准证据包与判定报告**：包含 `manifest.json`、原始数据日志及 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE` 最终判定。

---

## 2. 目标数据结构、读临界区与释放协议设计

### 2.1 目标 Extent 描述符设计

面向跨设备内存整理与安全管理，底层 Extent 描述符标准化定义如下：

```cpp
struct alignas(64) ExtentDescriptor {
    uint64_t extent_id;                 // 显存连续物理区间全局唯一 ID
    uint64_t device_addr;               // 设备可访问的物理显存基地址 (IOVA)
    uint64_t bytes;                     // 区间物理总字节大小
    uint64_t generation;                // 显存代际版本号
    uint64_t checksum;                  // 原始数据 xxHash64 校验指纹
    uint32_t state;                     // 状态: ACTIVE, COPYING, RETIRED, RELEASED, INVALID
    uint32_t padding;
};
static_assert(sizeof(ExtentDescriptor) == 64);
```

### 2.2 目标活动指针与读临界区结构

```cpp
struct alignas(64) RCUPageEntry {
    std::atomic<ExtentDescriptor*> active{nullptr};      // 当前活跃显存指针 (原子操作)
    std::atomic<uint64_t>          epoch{0};             // RCU 轮次代际
    std::atomic<uint32_t>          host_readers{0};      // 当前活跃 Host 读者计数器
    std::atomic<uint64_t>          active_generation{0}; // 当前活跃代际版本
};

struct ReaderToken {
    uint64_t          reader_id;        // 读者线程/算子唯一 ID
    uint64_t          enter_epoch;      // 进入临界区时的 RCU Epoch
    ExtentDescriptor* extent;           // 持有的 Extent 句柄
    uint64_t          generation;       // 获取时的代际版本号
};
```

前台 Reader 线程在进入临界区时必须以 `std::memory_order_acquire` 读取 `active` 指针，退出临界区时原子递减计数。针对 NPU 异步算子，必须通过捕获底层 Stream Event 将硬件生命周期与释放协议严格绑定。

### 2.3 标准显存页迁移执行时序

```text
1. 锁定旧块元数据 : 读取 OldExtent 描述符并固化当前 generation
2. 分配新显存空间 : 在 HBM 显存池中分配物理连续的 NewExtent
3. 异步数据拷贝   : 启动 NPU DMA 引擎将 OldExtent 正文数据搬运至 NewExtent
4. 数据一致性校验 : 计算并核验 NewExtent 的 xxHash64 校验码与原始数据严格一致
5. 挂载硬件事件   : 在 NPU 驱动中插入 Stream Event A，标记旧地址算子提交边界
6. 执行原子 CAS   : 原子切换 active 指针 (OldExtent ──► NewExtent)
7. 新读者无锁导流 : 后续新发起的 Reader 立即读取 NewExtent
8. 等待双层宽限期 : 等待旧 Host 读者全部退出 (host_readers == 0) 并等待 Stream Event A 完成中断
9. 标记并安全释放 : 将 OldExtent 状态置为 RETIRED 并真正回收物理显存块
```

### 2.4 释放旧块的双重安全仲裁准则

$$
	ext{SafeToRelease}(	ext{OldExtent}) = 	ext{HostGracePeriodDone} \land 	ext{NPUEventCompleted} \land (	ext{ActiveGeneration} > 	ext{OldGeneration}) \land 	ext{NewExtentChecksumOK}
$$

上述四个条件中凡有任意一项未得到客观证实，系统严禁释放旧显存块，坚决杜绝任何潜在的 UAF 风险。

---

## 3. 实验方案与测试矩阵设计

### 3.1 三类迁移方案对照定义

| 迁移方案标识 | 读者访问与同步策略 | 核心对账定位与验证目的 | 当前工程代码状态 |
|---|---|---|---|
| `STOP_THE_WORLD` | 页迁移期间强制加写锁，全局阻塞所有前台读者 | 建立强同步安全与在线停顿的基线对比 | 当前仅生成固定演示 CSV |
| `SOFTWARE_RCU_COPY_ON_MIGRATE` | 异步拷贝、原子 CAS 翻转、双层宽限期后延迟释放 | **证伪“硬件 Atomic Remap 属于必需强依赖”的核心被测方案** | 当前仅生成固定演示 CSV |
| `HARDWARE_ATOMIC_REMAP` | 由硬件 MMU/IOMMU 或加速卡固件提供一步式映射更新 | 可选的硬件原语对照组 | 当前按开关生成 DEMO 或标记不支持 |

### 3.2 负载压力与故障注水矩阵

| 测试维度 | 正式实施计划标准 | 工程说明与约束 |
|---|---|---|
| 前台并发 Reader 数 | 1、8、32、64 并发线程 | 评估高并发读取下的临界区争用与停顿耗时 |
| 单次迁移物理块尺寸 | 4MB、64MB、256MB、1GB | 检验不同数据规模下的 DMA 搬运时延与宽限期等待 |
| 迁移测试重复轮次 | 100 轮起步，正式目标 1000 轮 | 确保充分捕获 P99 尾部时延与极其罕见的内存时序竞争 |
| 读/写并发比例 | 纯读并发、读写 10:1、读写 1:1 | 模拟真实多轮对话下的显存分配与访问交织压力 |
| 故障点注入场景 | 拷贝后翻转前故障、翻转后宽限期内故障、释放前故障、Checksum 校验失败、NPU Event 超时 | 验证系统在各种时序断点下的自愈、状态保留与安全回滚能力 |

---

## 4. 工具审计与最小实现增量

### 4.1 当前基准测试程序实际命令

当前受控 C++ 基准程序执行命令为：

```bash
cd ./提前验证方案设计/验证计划方案设计/原型验证代码/CVT-01
make clean && make -j2
./rcu_migration_bench --out rcu_schema_demo.csv
./rcu_migration_bench --hardware-supported --out rcu_hardware_flag_demo.csv
```

执行后生成的 CSV 文件明确标记 `evidence_level=DEMO` 与 `status=DEMO_ONLY`；硬件行在未传入开关时标记为 `NOT_SUPPORTED`。

### 4.2 面向生产级实测的最小工程增量

在正式开展软件 RCU 验证与硬件依赖证伪前，必须补齐以下工程增量：

1. **真实并发 Reader 线程池**：实现多线程持续发起显存读取并核验 xxHash64 与代际版本的测试脚手架；
2. **底层 Extent 显存分配与 DMA 搬运**：对接真实的 HBM 显存物理块分配与异步 DMA 拷贝；
3. **原子指针翻转与双层宽限期管理**：实现基于 `std::atomic` 的 CAS 翻转、Host 读者计数器及延迟释放队列；
4. **昇腾 CANN/NPU 硬件流事件对接**：调用 `aclrtCreateEvent`/`aclrtRecordEvent`/`aclrtStreamWaitEvent`，将底层算子硬件完成状态纳入宽限期判定；
5. **全时序断点故障注入组件**：实现针对数据损坏、CAS 竞争失败及 NPU 事件超时的自动化注水与回滚测试；
6. **高精度微秒级停顿采样器**：逐次采集每个 Reader 的读取耗时与停顿时间，生成精确的 P50/P95/P99 统计。

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结环境快照、代码版本与测试目录

- **操作意图**：确保所有对比测试均可精准追溯至唯一的源码版本、分配器配置及硬件驱动环境。
- **执行命令**：

```bash
run_id="CVT-01-$(date +%Y%m%d-%H%M%S)-migration"
result_dir="results/cvt01/${run_id}"
mkdir -p "${result_dir}"
git rev-parse HEAD > "${result_dir}/git_commit.txt"
date --iso-8601=ns > "${result_dir}/timestamp.txt"
git status --short > "${result_dir}/git_status.txt"
```

- **应观察现象**：结果目录创建成功，Commit 哈希、系统时间戳及工作区状态完整落盘。
- **判定边界**：若显存分配器或驱动版本发生漂移，测试结论直接判定为 `INVALID-EVIDENCE`。

### 步骤 1：运行当前 C++ DEMO 校验格式与流程

- **操作意图**：验证输出 CSV 的 Schema 结构与参数解析逻辑，同时明确记录当前程序尚未产生真实显存迁移的事实。
- **执行命令**：

```bash
cd ./提前验证方案设计/验证计划方案设计/原型验证代码/CVT-01
make clean && make -j2
./rcu_migration_bench --out "../../../../results/cvt01/${run_id}/rcu_schema_demo.csv"
./rcu_migration_bench --hardware-supported --out "../../../../results/cvt01/${run_id}/rcu_hardware_flag_demo.csv"
```

- **应观察现象**：导出的 CSV 文件包含三类方案的演示数据行，明确标记 `DEMO,DEMO_ONLY`。
- **判定边界**：本步骤仅证实基准测试程序的输出格式合法，不可据此断言软件 RCU 安全性或停顿指标已经达标。

### 步骤 2：运行真实并发 Reader 与软件 RCU 最小闭环（条件步骤）

- **操作意图**：在受控的 Host 多线程并发环境下，证实软件 RCU 读临界区、异步拷贝、原子 CAS 翻转及延迟释放的逻辑正确性。
- **执行动作**：拉起并发 Reader 线程持续读取目标 Extent 并校验 Checksum；后台迁移线程执行数据搬运、校验、CAS 翻转及 Host 宽限期等待；记录各 Reader 的读取耗时与数据状态。
- **应观察现象**：每个 Reader 仅能读取到完整一致的旧数据或新数据；旧显存块释放时间戳严格晚于所有活跃 Reader 退出时间戳；0 脏读发生。
- **判定边界**：若缺乏真实数据一致性校验或发生脏读，直接一票否决判定为 `NO-GO`。

### 步骤 3：接入 NPU 异步硬件流事件与双层宽限期（条件步骤）

- **前置条件**：已集成昇腾 CANN 驱动运行时，NPU Stream Event 接口已打通。
- **操作意图**：将 NPU 异步算子与底层 DMA 硬件生命周期严格纳入宽限期仲裁，消除 UAF 越界隐患。
- **执行动作**：在 NPU 上提交引用旧 Extent 的异步算子；在指针翻转前后插入硬件流事件标记；后台迁移线程等待 Host 读者清零并等待 NPU Stream Event 完成中断后，方可释放旧显存块。
- **应观察现象**：NPU 算子计算输出 100% 正确；旧显存块在 NPU 事件完成前绝不被释放；系统 0 UAF 崩溃。
- **判定边界**：在未接入真实 NPU 硬件流事件前，测试结论严格限定为 `LAB`，不可作为生产级准入依据。

### 步骤 4：运行 Stop-the-World vs 软件 RCU 同场景 A/B 实测（条件步骤）

- **操作意图**：在完全相同的并发负载与显存压力下，严密对比两种方案的停顿时间、前台 TPOT 干扰率及数据正确性。
- **执行动作**：依次运行 `STOP_THE_WORLD` 强同步方案与 `SOFTWARE_RCU_COPY_ON_MIGRATE` 软件 RCU 方案；全量采集前台 Reader 停顿时间样本及在线推理 TPOT 序列；计算 P50/P95/P99 时延分位数。
- **应观察现象**：相比全局暂停方案，软件 RCU 方案的前台读取停顿 P99 从数十毫秒大幅骤降至 $< 1	ext{ms}$，TPOT 干扰率稳定收敛在 $< 3\%$。
- **判定边界**：两组测试的负载、块尺寸及统计窗口必须绝对一致，否则 A/B 结论判定为 `INVALID-EVIDENCE`。

### 步骤 5：全时序断点故障注入与安全回滚实测（条件步骤）

- **操作意图**：确证软件 RCU 机制在各种异常扰动下绝不会发生提前释放、内存泄漏或脏块放行。
- **执行动作**：在拷贝完成、CAS 翻转后、宽限期等待中及校验失败等断点分别注入异常；观测系统是否平稳拦截并触发安全回滚；持续运行 Reader 检验是否存在野指针访问。
- **应观察现象**：异常发生时系统平稳回滚至一致状态；未完成的新块被安全销毁；旧显存块保持合法可用；数据 Checksum 100% 正确。
- **判定边界**：若注入异常后发生脏数据进入 `READY` 或内存泄漏，直接判定为 `NO-GO`。

### 步骤 6：现场硬件 Atomic Remap 能力探测与对照（条件步骤）

- **操作意图**：客观探测现场硬件与固件是否支持原子重映射原语；若支持则开展同场景对照，不支持则规范记录 `NOT-SUPPORTED`。
- **执行动作**：执行底层能力探针命令；若支持则采集硬件重映射路径的实测数据，若不支持则输出探测失败日志并标记 `NOT-SUPPORTED`。
- **应观察现象**：硬件能力状态与实际物理配置严格吻合。
- **判定边界**：严禁使用代码内部的布尔开关伪造硬件实测。

### 步骤 7：证伪结论严密总结与证据归档

- **操作意图**：形成涵盖读路径安全、双层宽限期对账、停顿对比及科学证伪边界的完整证据链。
- **执行动作**：在 `results/cvt01/<run_id>/` 目录下完整归档 `reader_events.jsonl`、`npu_stream_events.jsonl`、`pointer_flip_events.jsonl`、`grace_period_events.jsonl`、`release_events.jsonl`、`pause_samples.csv`、`tpot_samples.csv`、`rollback_events.jsonl`、`summary.csv` 及 `summary.md`。

---

## 6. 数据采集清单、核心公式与记录格式

### 6.1 迁移测试标准汇总记录字段

```csv
run_id,migration_scheme,reader_count,migrated_bytes,chunk_bytes,migration_rounds,p99_pause_ms,p99_read_us,tpot_interference_pct,corrupted_reads,use_after_free_count,rollback_safe,planned_path,actual_path,evidence_level,status,invalid_reason
```

约束说明：`corrupted_reads` 与 `use_after_free_count` 必须基于全量并发校验事件统计，严禁在未接入校验器时填为 0。

### 6.2 Reader 与 NPU 硬件流事件原始字段

```csv
run_id,request_id,reader_id,stream_id,object_id,extent_id,generation,event,device_addr,bytes,checksum,ts_ns,error_code
```

### 6.3 核心指标计算公式

$$
	ext{前台读取停顿分位数} : \quad T_{	ext{pause}}(	ext{P99}) = 	ext{percentile}(	ext{reader\_pause\_samples}, 99)
$$

$$
	ext{前台在线 TPOT 干扰率} : \quad \Delta 	ext{TPOT}_{	ext{P99}} = rac{	ext{TPOT}_{	ext{P99}}(	ext{Migrating}) - 	ext{TPOT}_{	ext{P99}}(	ext{Baseline})}{	ext{TPOT}_{	ext{P99}}(	ext{Baseline})} 	imes 100\%
$$

$$
	ext{显存安全释放延迟裕量} : \quad \Delta T_{	ext{safe}} = T_{	ext{release}}(	ext{OldExtent}) - \max\left( T_{	ext{exit}}(	ext{LastHostReader}), T_{	ext{done}}(	ext{NPUStreamEvent}) ight) \ge 0
$$

---

## 7. 候选准入门槛、判定规则与立即止损机制

### 7.1 候选工程准入门槛一览表

| 核心评估指标 | 候选工程准入门槛 | 权威取证要求 |
|---|---:|---|
| 前台 Reader 停顿耗时 | 软件 RCU 模式下前台 Reader 读取停顿 **P99 $< 1	ext{ms}$** | 全量逐次采样样本计算，严禁使用单次最大值替代 |
| 前台在线 TPOT 干扰率 | 显存页迁移期间前台推理 **P99 TPOT 干扰恶化率 $< 3\%$** | 基于同一代码构建在无迁移干扰下的纯前台基线对账 |
| 数据正确性与内存安全 | 全量迁移测试中 **0 脏读、0 UAF 内存越界、0 错误消费** | 覆盖 Host CPU 线程与 NPU 异步算子的全量一致性校验 |
| 异常安全回滚可靠性 | 全场景故障注水下 **0 内存泄漏、回滚后 Checksum 100% 正确** | 逐时序断点注入测试与 Oracle 对账 |
| 硬件依赖必要性证伪 | 在已验证条件下得出“无需硬件 Atomic Remap”的科学结论 | 完整的软件 `MEASURED` 证据链与严格的适用边界声明 |

### 7.2 状态判定枚举与规则

- **GO（科学证伪证据形成完整闭环）**：在明确的设备引用、分配器及拓扑范围内，软件 RCU 的停顿、TPOT 干扰率、数据正确性及回滚安全性均具备完整 `MEASURED` 证据链；可严密表述为“在已验证条件下不需要硬件 Atomic Remap 强依赖”；
- **CONDITIONAL（局部场景或特定规模达标）**：软件方案在特定并发或特定块大小下达标，但扩展至超大块或极端并发时存在长尾波动；结论严格限定于已测条件；
- **NO-GO（发生内存越界或数据损坏）**：出现提前释放、UAF 越界访问、脏读、旧代次消费或回滚失败；表明当前软件实现未达到准入标准；
- **NOT-SUPPORTED（物理环境未支持）**：现场缺乏硬件 Atomic Remap 原语或缺少 NPU 硬件流事件接口；对应硬件轨道规范记录为不支持，不影响软件方案的独立证伪；
- **INVALID-EVIDENCE（无效证据）**：使用静态演示数据、缺乏真实 Reader 线程/NPU 事件、校验样本缺失或实际路径未证实。

### 7.3 立即安全止损条件

在测试过程中凡触发以下任一异常，必须立即终止测试并保存现场：

- Reader 线程或 NPU 算子读取到 Checksum 损坏的数据块、半写脏块或陈旧代际数据；
- 旧 Extent 显存块在 Host 读者未完全退出或 NPU Stream Event 未完成前被过早释放；
- 发生 UAF 越界崩溃、野指针非法解引用或 NPU 显存物理地址非法访问；
- CAS 指针翻转后新显存块校验失败，但脏数据依然被下游算子消费；
- 注入故障后软件 RCU 状态机发生死锁，无法平稳执行安全回滚或报错退出；
- 前台推理 TPOT 尾部时延持续恶化超出预设止损红线，且后台迁移器无法自适应降速暂停。

---

## 8. 执行阶段划分与交付闭环

测试实施划分为四个严密的演进阶段：

| 实施阶段 | 核心攻坚内容 | 阶段必须交付物 | 准出判定条件 |
|---|---|---|---|
| E0（命题与契约规范确认） | 严密固化证伪命题边界、Manifest Schema、双层宽限期定义及证据等级规范 | 源码审计报告、DEMO 输出 CSV/JSON、规范化 Manifest | 确认当前代码边界，杜绝将测试桩冒充实测 |
| E1（Host Reader 软件 RCU） | 打通并发 Reader 线程、真实 Extent 分配、异步拷贝、CAS 翻转及 Host 宽限期 | Host RCU 基线表、CAS 翻转日志、停顿采样分布 | 证实 Host 读路径无锁成立且停顿 P99 $< 1	ext{ms}$ |
| E2（NPU 硬件流事件与回滚） | 接入真实 NPU Stream Event，验证双层宽限期、代际隔离及全时序断点故障回滚 | 双层宽限期日志、硬件流事件表、故障回滚报告 | 证实 0 UAF 越界且回滚后 Checksum 100% 正确 |
| E3（前台混压与硬件对照） | 在前台真实推理与高显存碎片混压下，全量对比 Stop-the-World 与软件 RCU | 混压时间线分析表、TPOT 干扰率报告、最终判定结论 | 证实全链路 TPOT 干扰率 $< 3\%$ 且完成科学证伪 |
| 条件证伪（引用协议覆盖边界） | 变换 Reader 临界区长度、NPU 异步时长及 DMA 访问模式，摸清纯软方案技术边界 | 协议边界评估表、硬件依赖证伪报告 | 确立纯软主路径，明确软硬件协同的适用包络 |

---

## 9. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师核心职责**：
  1. 确认现场部署的 NPU 加速卡、显存分配器、CANN 运行时驱动版本及系统特权；
  2. 固化显存页迁移块大小、并发 Reader 数、测试重复轮次及安全止损红线；
  3. 实际启动/停止并发测试脚手架与内核跟踪工具，完整留存系统 dmesg 日志与驱动告警；
  4. 严格审定 `corrupted_reads=0` 与 `use_after_free_count=0` 是否确由 Host+NPU 双层校验闭环证明；
  5. 对“硬件 Atomic Remap 必要性证伪结论”的科学严密性与适用边界承担最终技术把关责任。
- **AI Agent 协同职责**：
  1. 深入研读本方案设计、公共测试契约及原型源码，精准梳理实际支持的 CLI 参数、依赖库及当前未实现特性；
  2. 编写并发 Reader 压力测试脚手架、NPU Stream Event 硬件流事件解析器、停顿分位数统计及标准证据包生成工具；
  3. 严格核验各测试项的实际执行状态、`planned_path` 与 `actual_path` 路径一致性及状态枚举归一化；
  4. 严守学术与技术诚信红线，严禁虚构停顿指标、伪造硬件重映射支持或将静态演示数据篡改为生产级实测。

### 9.2 可直接复制给 Coding Agent 的 Prompt 模板

```text
我正在执行 CVT-01：PageMigration 软件 RCU 与硬件 Atomic Remap 必要性证伪验证。

请先研读以下核心文件：
1. ./提前验证方案设计/验证计划方案设计/11_CVT-01_PageMigration软件RCU与硬件AtomicRemap必要性证伪实施方案设计.md
2. ./提前验证方案设计/验证计划方案设计/Benchmark公共契约与证据分级规范.md
3. ./提前验证方案设计/验证计划方案设计/原型验证代码/CVT-01/Makefile
4. ./提前验证方案设计/验证计划方案设计/原型验证代码/CVT-01/rcu_migration_bench.cc

执行约束与任务要求：
- 首先梳理源码实际支持的 CLI 参数与底层执行行为；确认 rcu_migration_bench.cc 当前仅输出静态演示 DEMO，未创建 Reader、未迁移数据、未调用 CANN/NPU 驱动，当前目录中不存在 verify_checksum.py 或 eval_cvt01.py。
- 正式实验必须首先实现真实并发 Reader 线程、真实 Extent 显存分配、异步 DMA 拷贝、原子 CAS 指针翻转、xxHash64 校验及延迟释放；随后接入真实 NPU Stream Event 硬件流事件，严格证明旧显存块的设备引用何时彻底结束。
- 同场景对比 Stop-the-World 与软件 RCU 方案，固定并发度、块尺寸、迁移轮次及统计窗口。
- 故障注入测试必须全量覆盖拷贝后翻转前、翻转后宽限期内、释放前及 NPU Event 超时等断点。
- 逐次采样计算停顿 P99 与 TPOT 干扰率；区分 planned_path 与 actual_path；未采集到的字段显式置为 null 并详细注明 invalid_reason。
- 最终输出：源码能力核验矩阵、实际执行命令清单、三方案性能对照表、双层宽限期审计表、未支持特性清单以及下一步最小代码重构建议。
```

### 9.3 常见排错指南

- **输出 CSV 中停顿与时延显示为固定的 0.08ms/3.8µs 样例数值**：当前受控 C++ 代码仅为演示 DEMO；必须接入真实的并发 Reader 线程与显存迁移引擎，未接入前测试结果严格保持为 `DEMO`。
- **命令行传入 `--mode`、`--readers` 或 `--loops` 毫无效果**：当前受控基准程序未集成此类 CLI 参数；应按照实际支持的 CLI 参数执行，或在重构增量中补齐接口。
- **执行命令提示找不到 `verify_checksum.py` 脚本报错**：当前受控工程中尚未包含该脚本；需补齐校验器实现或人工核验，严禁引用不存在的命令。
- **Host 读者全部退出但 NPU 异步算子依然发生显存越界崩溃**：说明仅等待了 Host 宽限期而未将 NPU Stream Event 纳入仲裁；必须接入底层的硬件流事件监听，在硬件完成中断上报前严禁释放旧显存块。
- **CAS 指针翻转成功后数据 Checksum 校验失败**：排查数据拷贝完成与指针翻转的时序依赖；必须在 Checksum 100% 校验成功后方可执行指针翻转。
- **注入故障后进程未崩溃但数据输出异常**：进程存活不代表内存安全；必须严格核验 xxHash64 校验码、Generation 代际及显存释放时间戳。
- **传入 `--hardware-supported` 后输出仍为 `DEMO_ONLY`**：该参数仅为代码内部的演示分支开关；必须执行现场客观能力探测与驱动调用。
- **现场硬件环境不支持 Atomic Remap 原语**：规范记录为 `NOT-SUPPORTED`，并在已验证的软件 RCU 适用范围内闭合证伪结论。
- **停顿耗时 P99 统计被单次最大异常值干扰失真**：排查分位数计算逻辑；必须保存全量逐次采样样本，基于标准分位算法计算。
- **旧显存块释放时间戳早于 NPU Stream Event 完成时间戳**：说明双层宽限期逻辑存在严重漏洞；必须立即终止测试，判定为一票否决的 `NO-GO` 并紧急修复释放协议。
