# CVT-01：PageMigration 软件 RCU 与硬件 Atomic Remap 必要性证伪实施方案设计
## —— 显存页迁移安全与硬件依赖证伪：软件 RCU、NPU 异步引用与 Atomic Remap 对照

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个 `run_id` 必须冻结分配器、设备、CPU/NUMA 绑定、迁移方案、Reader 数、块大小、迁移轮次、读写比例、故障点、代码包、配置哈希和证据等级；结果必须保留 Reader 进入/退出、指针读取与翻转、NPU Stream 事件、宽限期、旧块释放、checksum、generation、回滚、停顿和前台 TPOT 样本。硬件 Atomic Remap 不可用时标记 `NOT-SUPPORTED`，不能填入模拟时延；证伪结论必须限定在实际访问契约、设备拓扑和迁移范围内。

> **验证范围声明**：当前受控工程中的 `rcu_migration_bench.cc` 只生成固定的三类迁移 schema，不创建 Reader、不分配 Extent、不迁移数据、不调用 NPU/CANN，也没有真实 checksum、停顿或回滚事件；`--hardware-supported` 只改变固定输出分支，不能作为硬件探测。因此当前源码只能检查结果格式和证伪状态流程，不能单独证明软件 RCU 安全、NPU 异步引用已纳入宽限期、错误读取为 0 或硬件 Atomic Remap 是否必要；固定的 `0.08ms`、`3.8µs` 等数值只能标记为 `DEMO`/`LAB`。

> **术语速查**：PageMigration（页迁移，即把 KVCache 数据从旧物理块复制到新块并更新后续访问映射）；KVCache（大模型注意力键值缓存，即自回归生成过程中保存历史 Key 和 Value 激活状态、避免后续 Token 重复计算注意力）；RCU（Read-Copy-Update，读-拷贝-更新，即读者继续使用旧视图、写者切换到新视图并在宽限期后释放旧视图）；Atomic Remap（原子重映射，即一次性改变地址映射，具体能力必须由现场硬件证明）；Reader（可能在迁移期间持有旧块引用的线程或设备执行单元）；Grace Period（宽限期，即所有仍可能使用旧块的 Host/NPU 操作完成前的等待区间）；Extent（连续物理数据区间）；Generation（对象代次号，用于防止旧完成事件覆盖新映射）；CAS（Compare-And-Swap，比较并交换）；NPU（Neural Processing Unit，神经网络处理器）；NPU Stream Event（用于确认异步算子或 DMA 已完成的 NPU 流事件）；CANN/ACL（华为异构计算架构及其运行时接口）；Checksum（用于检测迁移前后数据一致性的校验值）；Use-After-Free（释放后使用，即访问已回收的旧块）；Stop-the-World（全局暂停，即迁移期间阻塞所有读者的安全/停顿对照）；TPOT（Time Per Output Token，每个输出 Token 的生成耗时）；P99（样本分布中 99% 样本不超过的分位值）；NOT-SUPPORTED（测试环境或硬件不具备对应能力，不是模拟成功）。

> **验证 ID**：CVT-01
> **验证名称**：页迁移/Defrag 软件 RCU 与硬件 Atomic Remap 原语必要性证伪
> **验证优先级**：**🟢 P2 级（拓展证伪项）**
> **对应验证阶段**：**条件证伪阶段（架构简化与去依赖）**
> **证伪标记**：**是（优先证伪“硬件 Atomic Remap 是内存整理迁移的必需依赖”）**
> **建议周期**：2~3 人日
> **主关联 IR**：`IR-01-01`, `IR-01-11`, `IR-01-12`
> **核心 SRS / SR23 锚点**：
> - SRS：`L4-CO-AtomicRemapPrimitive-065`, `L3-CO-MigrationRCULock-090`
> - SR23：`SR23-01-01-03`, `SR23-01-11-01`, `SR23-01-12-02`
> **配套源码**：[`提前验证方案设计/验证计划方案设计/原型验证代码/CVT-01/`](file:///d:/codes/reports/kvcache/unified_kv_memory/提前验证方案设计/验证计划方案设计/原型验证代码/CVT-01)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`。正式结果必须绑定实际分配器、NPU 运行时、设备事件和配置哈希。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 迁移安全性的基本约束

迁移有三个必须分别证明的动作：

```text
复制：旧 Extent -> 新 Extent，并验证新内容
切换：新进入的 Reader 使用新 Extent
释放：所有仍可能使用旧 Extent 的 Host/NPU 操作完成后才回收旧 Extent
```

只证明指针切换快，不代表旧块可以释放；只统计 Host Reader，也不代表 NPU 异步算子已经停止引用旧地址。软件 RCU 的核心是把“谁可能持有旧引用”纳入可观察协议，并把旧块的释放延后到所有相关宽限期结束之后。

### 0.2 双层宽限期目标模型

目标软件路径应同时维护 Host 和 NPU 两类访问：

```text
后台迁移器分配 NewExtent
    -> 拷贝/校验 NewExtent
    -> 记录 NPU 事件并确认旧引用边界
    -> CAS active_ptr: OldExtent -> NewExtent
    -> 等待 Host Reader 宽限期
    -> 等待 NPU Stream Event 完成
    -> 再释放 OldExtent
```

这要求分配器、DMA 提交器和 NPU 运行时使用同一个对象代次与引用协议。若 NPU 算子只保存一个无法追踪的旧物理地址，软件 RCU 无法判断它何时结束；此时必须改为设备支持的重映射原语、全局暂停或其他可证明的安全机制。

### 0.3 证伪逻辑

| 结果 | 能支持的结论 | 不能支持的结论 |
|---|---|---|
| 软件 RCU 真实通过 | 在已验证拓扑、分配器和设备引用协议下，软件方法满足当前安全/停顿门限 | 对所有硬件、所有未追踪 DMA 和所有模型规模都不需要 Atomic Remap |
| 软件 RCU 失败 | 当前软件引用/宽限期实现不满足要求，需修正或引入更强机制 | 硬件 Atomic Remap 在任何环境都必需 |
| 硬件对照不支持 | 现场无法验证硬件路径 | 软件 RCU 一定优于或替代硬件路径 |
| 只有 Host Reader 模拟通过 | Host 层局部逻辑可演示 | NPU Stream、DMA 和设备内存安全已证明 |

### 0.4 当前受控源码能力矩阵

| 文件 | 当前可确认行为 | 当前不能声称的能力 |
|---|---|---|
| `原型验证代码/CVT-01/rcu_migration_bench.cc` | 解析 `--hardware-supported` 和 `--out`；固定写出 `STOP_THE_WORLD`、`SOFTWARE_RCU_COPY_ON_MIGRATE` 和硬件行；输出 `DEMO` 或内部 `NOT_SUPPORTED` | 不创建 Reader、不分配 Extent、不迁移数据、不调用 NPU/CANN、不记录停顿样本或回滚事件 |
| `rcu_migration_bench.cc` | 固定写入 `p99_pause_ms`、`p99_read_us`、`tpot_interference_pct`、`corrupted_reads` 和 `rollback_safe` 等样例字段 | 固定字段不等于真实 P99、读延迟、TPOT、错误读取或回滚结果；`--hardware-supported` 不是硬件探测 |
| `原型验证代码/CVT-01/Makefile` | 使用 `g++ -O3 -std=c++17 -pthread -Wall` 构建一个结果 schema DEMO | 没有 CANN/ACL、NPU Event、内存分配器、Checksum 或多线程测试依赖 |
| CVT-01 目录 | 当前没有 `verify_checksum.py` 或 `eval_cvt01.py` | 旧稿中引用的校验和汇总脚本不存在 |

### 0.5 当前输出和命令边界

当前程序没有 `--mode`、`--readers`、`--migrated-mb`、`--loops` 或故障点参数。传入这些参数不能切换迁移方案；程序一次运行会把三种方案写入同一个 CSV。当前 CSV 的停顿字段是 `p99_pause_ms`，不是旧稿中的 `p99_pause_time_us`。

程序默认把 `HARDWARE_ATOMIC_REMAP` 写成内部 `NOT_SUPPORTED`；传入 `--hardware-supported` 只让它写出固定硬件样例，仍然是 `DEMO`。因此当前原型只能检查结果格式和硬件状态分支，不能关闭 CVT-01 的证伪门禁。

## 1. 验证目标与交付物

### 1.1 验证目标

| 目标 | 需回答的问题 | 最低证据 |
|---|---|---|
| 读路径安全 | 迁移期间 Reader 是否只读到完整旧/新视图 | 逐 Reader/逐轮次 checksum、generation 和视图 ID |
| 释放安全 | 旧 Extent 是否在所有 Host/NPU 引用结束前保持有效 | 引用计数/epoch、NPU Event、释放时间线 |
| 停顿影响 | 软件 RCU 相对全局暂停是否减少 Reader 停顿 | 原始停顿样本和同场景 P99 |
| 在线影响 | 迁移期间前台 TPOT 是否满足当前门限 | 真实前台请求样本和迁移事件关联 |
| 异常回滚 | 拷贝、切换或释放阶段失败时能否回到一致视图 | 故障注入、旧/新状态、回滚动作和数据校验 |
| 硬件必要性 | 在已验证的引用契约下，软件 RCU 是否足以满足要求 | 软件 MEASURED 证据 + 硬件能力状态 + 适用边界 |

### 1.2 候选门限

以下门限是待测准入条件，不是当前固定 CSV 的结果：

| 维度 | 候选要求 | 说明 |
|---|---:|---|
| Reader 停顿 | 软件 RCU P99 `<1ms` | 按迁移轮次和 Reader 逐次采样 |
| 前台干扰 | 迁移期间 TPOT 干扰率 `<3%` | 需要同一增强代码包纯前台基线 |
| 数据正确性 | `corrupted_reads=0`，无 UAF、半写块和旧代次消费 | 需要 Host/NPU 两类访问都覆盖 |
| 回滚安全 | 所有故障点均无提前释放，恢复后 checksum 正确 | 任一不可恢复数据错误为 `NO-GO` |
| 硬件对照 | 若支持，记录 Atomic Remap 真实数据；不支持则标记 `NOT-SUPPORTED` | 不用模拟行填充硬件结论 |

### 1.3 交付物

1. 全局暂停、软件 RCU 和可用硬件重映射的同场景对照；
2. Reader/NPU 访问、迁移、指针切换、宽限期和释放时间线；
3. checksum、generation、UAF/野指针和半写数据检查结果；
4. 拷贝前后、切换前后、释放前后的异常回滚记录；
5. 软件 RCU 证伪结论的适用范围、硬件能力状态和最终状态判定。

## 2. 目标数据结构、读临界区与释放协议

### 2.1 目标 Extent 描述

当前仓库没有该结构。实现前需要冻结地址、长度、代次和校验字段：

```cpp
struct ExtentDescriptor {
    uint64_t extent_id;
    uint64_t device_addr;
    uint64_t bytes;
    uint64_t generation;
    uint64_t checksum;
    uint32_t state;       // ACTIVE, COPYING, RETIRED, RELEASED, INVALID
};
```

`device_addr` 必须表示 NPU/DMA 可访问的设备地址或经过明确映射的地址，不能默认是 Host 指针。`state`、`generation` 和释放事件必须能与每个 Reader/NPU 操作关联。

### 2.2 目标活动指针与读临界区

```cpp
struct RCUPageEntry {
    std::atomic<ExtentDescriptor*> active{nullptr};
    std::atomic<uint64_t> epoch{0};
    std::atomic<uint32_t> host_readers{0};
    std::atomic<uint64_t> active_generation{0};
};

struct ReaderToken {
    uint64_t reader_id;
    uint64_t enter_epoch;
    ExtentDescriptor* extent;
    uint64_t generation;
};
```

读者进入临界区后再以 acquire 方式读取活动指针，退出前释放对旧 Extent 的引用。真实 NPU 异步操作不能只依赖 Host `host_readers`；需要将 Stream Event、DMA completion 或等价设备事件写入同一个释放协议。

### 2.3 目标迁移序列

```text
1. 读取 OldExtent 元数据并锁定 generation
2. 分配 NewExtent
3. 异步拷贝 OldExtent -> NewExtent
4. 校验 NewExtent checksum/格式/长度
5. 发布 NPU/设备完成事件
6. CAS active: OldExtent -> NewExtent
7. 新 Reader 读取 NewExtent
8. 等待旧 Host Reader 和 NPU Event 完成
9. 标记 OldExtent RETIRED，再释放
```

如果 CAS 失败，必须释放或回收 NewExtent，不能留下不可达块；如果拷贝/校验失败，active 指针不得改变；如果切换后发现新块异常，只有在旧块仍处于可用宽限期内才能回滚。

### 2.4 释放前的双重判定

```text
safe_to_release(old_extent) =
    host_grace_period_complete
    AND npu/device_references_complete
    AND old_generation_not_visible_to_new_reader
    AND new_extent_checksum_ok
```

任何一项未知都不得释放旧块。尤其不能把 `active_ptr` 已经翻转解释为“旧块无人使用”。

## 3. 实验矩阵与冻结参数

### 3.1 迁移方案矩阵

| 方案 | 访问策略 | 用途 | 当前状态 |
|---|---|---|---|
| `STOP_THE_WORLD` | 迁移期间阻塞读者 | 建立安全和停顿对照 | 当前仅固定 CSV |
| `SOFTWARE_RCU_COPY_ON_MIGRATE` | Copy、CAS、双层宽限期后释放 | 证伪硬件必要性 | 当前仅固定 CSV |
| `HARDWARE_ATOMIC_REMAP` | 设备/硬件一次性更新映射 | 可选硬件对照 | 当前按开关写 DEMO/不支持 |

### 3.2 负载矩阵

| 维度 | 候选值 |
|---|---|
| Reader 数 | 1、8、32、64 |
| 迁移块大小 | 4MB、64MB、256MB、1GB |
| 迁移轮次 | 100、1000；正式结果按尾部稳定性增加 |
| 读/迁移比例 | 纯读、读写 10:1、读写 1:1 |
| Reader 行为 | 短临界区、长临界区、随机延迟、NPU 异步引用 |
| 迁移触发 | 定时、显存高水位、碎片阈值 |
| 统计窗口 | 预热、稳态、故障和恢复分别统计 |
| 前台服务 | 无服务局部测试、真实 Decode 服务；必须区分证据等级 |

### 3.3 故障矩阵

| 故障点 | 注入动作 | 预期检查 |
|---|---|---|
| `after_copy_before_flip` | 新块已拷贝但未切换时失败 | active 仍指向旧块，新块不被消费 |
| `after_flip_before_grace` | 指针已切换但旧 Reader/NPU 仍在使用时失败 | 旧块不释放，旧引用完成后可回滚/回收 |
| `before_release` | 双层宽限期判定前失败 | 不发生提前释放，状态可重试 |
| checksum failure | 新块校验失败 | 拒绝切换，记录错误并保留旧视图 |
| NPU event timeout | 设备引用完成事件超时 | 延迟释放或显式失败，不把超时当完成 |

## 4. 工具审计、实际命令与最小实现增量

### 4.1 当前构建入口

当前 Makefile 只构建 C++ schema DEMO：

```bash
cd ./提前验证方案设计/验证计划方案设计/原型验证代码/CVT-01
make clean && make -j2
```

构建参数没有 CANN/ACL、NPU 运行时、Checksum 库、线程测试或设备内存依赖。构建成功不能说明软件 RCU 已实现。

### 4.2 当前 C++ DEMO 的真实命令

```bash
./rcu_migration_bench --out rcu_schema_demo.csv
./rcu_migration_bench --hardware-supported --out rcu_hardware_flag_demo.csv
```

第一条输出硬件行的内部状态为 `NOT_SUPPORTED`；第二条输出固定硬件行，但两次运行的证据级别都为 `DEMO`。程序一次输出三种方案，不能用 `--mode`、`--readers` 或 `--loops` 控制实验。

### 4.3 当前缺失的校验和汇总入口

当前目录没有 `verify_checksum.py` 和 `eval_cvt01.py`。真实实验需先实现：

1. 逐 Reader/逐 NPU 事件的 checksum/generation 校验器；
2. 从原始停顿样本计算 P50/P95/P99 的统计器；
3. 迁移事件与前台 TPOT 的时间窗口关联器；
4. 故障点、旧块释放和回滚状态的独立判定器；
5. 统一输出 `GO/CONDITIONAL/NO-GO/NOT-SUPPORTED/INVALID-EVIDENCE`。

### 4.4 最小真实实现增量

进入 LAB/MEASURED 前至少需要：

1. 实现真实 Reader 线程和可控的长/短临界区；
2. 实现 Extent 分配、数据拷贝、checksum、generation 和状态迁移；
3. 实现 CAS 活动指针、Host epoch/Reader 追踪和延迟释放；
4. 接入真实 NPU Stream/DMA Event，证明设备引用何时结束；
5. 实现异常点注入、回滚、重复提交和半写块保护；
6. 接入真实显存碎片压力和前台 Decode 事件；
7. 对硬件 Atomic Remap 做能力探测，不能使用命令行布尔值代替；
8. 新增校验和、统计、评估和可复核结果目录。

## 5. 逐步执行 SOP

### Step 0：冻结版本和结果目录

操作意图：让软件方案、硬件对照和故障结果基于同一源码与配置。

执行动作：

```bash
run_id="CVT-01-$(date +%Y%m%d-%H%M%S)-migration"
result_dir="results/cvt01/${run_id}"
mkdir -p "${result_dir}"
git rev-parse HEAD > "${result_dir}/git_commit.txt"
date --iso-8601=ns > "${result_dir}/timestamp.txt"
git status --short > "${result_dir}/git_status.txt"
```

保存驱动、运行时、分配器、CPU/NUMA、显存配额和迁移配置；不要把当前未提交源码和历史 DEMO 输出混入同一结果目录而不记录。

应观察现象：源码版本、工作树状态和配置可回溯。

判定边界：无法确认读路径或分配器版本时，该轮最多为 `LAB`，不能进入正式证伪结论。

### Step 1：运行当前 schema DEMO

操作意图：确认当前程序的输出字段和硬件状态分支，明确它不是迁移测试。

执行动作：

```bash
cd ./提前验证方案设计/验证计划方案设计/原型验证代码/CVT-01
make clean && make -j2
./rcu_migration_bench \
  --out "../../../../results/cvt01/${run_id}/rcu_schema_demo.csv" \
  > "../../../../results/cvt01/${run_id}/rcu_schema_demo.stdout.txt"
./rcu_migration_bench \
  --hardware-supported \
  --out "../../../../results/cvt01/${run_id}/rcu_hardware_flag_demo.csv" \
  > "../../../../results/cvt01/${run_id}/rcu_hardware_flag_demo.stdout.txt"
```

应观察现象：输出包含三种 scheme，证据级别为 `DEMO`；硬件开关只影响硬件行内容和内部状态。

判定边界：不得使用固定的 `p99_pause_ms`、`p99_read_us`、TPOT、错误读取或回滚字段作为实测；该步骤只能闭合 schema DEMO。

### Step 2：实现并运行真实 Reader/迁移最小闭环

操作意图：先证明软件 RCU 的读临界区、拷贝、CAS 和释放顺序在可控 Host 并发下正确。

执行动作：

1. 固定 Reader 数、迁移块大小、轮次、随机 seed 和故障关闭状态；
2. 启动短临界区和长临界区 Reader，持续读取对象并校验 checksum/generation；
3. 启动迁移器，完成新块拷贝、校验、指针切换和双层（Host 层先行）宽限期；
4. 记录每个 Reader 进入/退出、看到的 extent/generation、暂停和错误；
5. 先完成无故障，再增加读/迁移比例和显存碎片压力。

应观察现象：每个 Reader 只读到完整旧/新视图；旧块释放时间晚于所有活跃引用退出；所有轮次和 Reader 都能对账。

判定边界：仅有一个线程、没有真实数据校验或只看最终进程退出码时，不能判定软件 RCU 安全。

### Step 3：接入 NPU 异步引用和双层宽限期

操作意图：把 Host Reader 之外的设备引用纳入释放安全判定。

执行动作：

1. 使用实际 NPU 分配和 Stream，提交明确引用旧 Extent 的异步算子或 DMA；
2. 在指针切换前后分别记录 Stream Event；
3. 确认运行时事件语义确实覆盖旧设备地址的读/写完成，而不是只表示 Host 提交完成；
4. 迁移器等待 Host Reader 宽限期和 NPU Event，之后才释放旧块；
5. 对比只等待 Host 与等待 Host+NPU 两种错误防护配置，验证缺失设备事件会被门禁阻止。

应观察现象：NPU 读取的数据完整；旧块在 NPU Event 完成前不会释放；设备事件、generation 和释放时间可相互关联。

判定边界：当前 Makefile 没有 CANN/ACL 依赖，无法直接执行本步骤；没有真实设备事件时，结论为 `NOT-SUPPORTED` 或 `INVALID-EVIDENCE`。

### Step 4：运行全局暂停和软件 RCU A/B

操作意图：在相同 Reader、迁移和前台负载下比较停顿、TPOT 和数据正确性。

执行动作：

1. 运行 `STOP_THE_WORLD`，记录读者停顿、前台 TPOT、checksum 和完成请求；
2. 运行 `SOFTWARE_RCU_COPY_ON_MIGRATE`，保持对象、轮次、Reader 和统计窗口不变；
3. 使用逐次样本计算 P50/P95/P99，不把一次最大值当 P99；
4. 对比迁移事件前后前台 TPOT 和请求完成率；
5. 保存完整性失败、异常回滚和未完成请求。

应观察现象：两组都有相同的迁移轮次和 Reader 样本；软件 RCU 的停顿变化可以回指指针切换和宽限期事件。

判定边界：如果两组请求流、迁移频率、显存配额或统计窗口不同，A/B 结果为 `INVALID-EVIDENCE`。

### Step 5：执行迁移故障注入和回滚

操作意图：确认软件 RCU 的安全性不只依赖正常路径。

执行动作：

1. 在拷贝完成、指针翻转后、宽限期结束前和释放前分别注入故障；
2. 注入新块 checksum 失败、NPU Event 超时和迁移线程异常退出；
3. 记录旧/新 Extent 状态、active 指针、generation、引用、释放和回滚；
4. 让持续 Reader 和 NPU 操作继续运行，检查是否出现 UAF、半写块、旧代次或野指针；
5. 只有在数据校验和资源状态恢复后，才允许进入下一轮。

应观察现象：拷贝失败不切换；切换后的故障不提前释放旧块；回滚或显式失败状态可追踪；没有错误数据进入 READY。

判定边界：进程没有崩溃不等于回滚正确；缺少 active 指针、释放时间和 checksum 证据时，故障结论为 `INVALID-EVIDENCE`。

### Step 6：探测硬件 Atomic Remap 对照

操作意图：确认硬件对照的真实可用性和它与软件 RCU 的适用边界。

执行动作：

1. 保存硬件、驱动、固件和运行时能力探测结果；
2. 确认 Atomic Remap 能否覆盖 NPU/DMA 持有的旧地址引用；
3. 在相同迁移对象、Reader、轮次和前台负载下执行真实硬件路径；
4. 记录映射更新、读路径、停顿、完整性和异常回滚事件；
5. 若能力不存在，保存探测失败原因并标记 `NOT-SUPPORTED`，继续闭合软件路径但不填充硬件性能。

应观察现象：硬件路径的 actual path、设备事件和能力矩阵一致。

判定边界：硬件命令行开关、厂商型号或理论时间不足以构成硬件对照。

### Step 7：统计证伪结论并归档

操作意图：把“软件满足当前契约”和“硬件普遍不需要”分开陈述。

执行动作：

```text
results/cvt01/<run_id>/
  metadata.yaml
  git_commit.txt
  git_status.txt
  environment.txt
  topology.txt
  allocator_config.json
  capability_matrix.json
  commands.txt
  reader_events.jsonl
  npu_stream_events.jsonl
  migration_events.jsonl
  pointer_flip_events.jsonl
  grace_period_events.jsonl
  release_events.jsonl
  checksum_events.jsonl
  pause_samples.csv
  tpot_samples.csv
  rollback_events.jsonl
  summary.csv
  invalid_evidence.md
  summary.md
```

应观察现象：每个 P99、错误计数和安全结论都能回指原始样本；证伪结论写明拓扑、设备引用协议和未覆盖范围。

判定边界：只有固定 CSV、Host 模拟或缺少 NPU 事件时，不能输出软件 RCU 的正式 `GO`，更不能输出普遍性硬件必要性结论。

## 6. 证据字段、统计公式与结果格式

### 6.1 迁移运行结果

```csv
run_id,migration_scheme,reader_count,migrated_bytes,chunk_bytes,migration_rounds,p99_pause_ms,p99_read_us,tpot_interference_pct,corrupted_reads,use_after_free_count,rollback_safe,planned_path,actual_path,evidence_level,status,invalid_reason
```

当前 C++ DEMO 的 header 使用 `p99_pause_ms`、`p99_read_us` 和 `corrupted_reads`；正式扩展字段应保持兼容或提供版本映射。空字段必须是 `null`/空并填写原因，不能用 0 代替未测。

### 6.2 Reader 和设备事件

```csv
run_id,request_id,reader_id,stream_id,object_id,extent_id,generation,event,device_addr,bytes,checksum,ts_ns,error_code
```

`event` 至少包括 `reader_enter`、`reader_pointer_load`、`npu_submit`、`copy_begin`、`copy_complete`、`checksum_ok`、`pointer_flip`、`reader_exit`、`npu_event_done`、`extent_retire`、`extent_release`、`rollback` 和 `invalid`。

### 6.3 统计公式

```text
Reader 停顿 P99 = percentile(reader_pause_samples, 99)

TPOT 干扰率 = (迁移期间前台 TPOT P99 - 无迁移前台 TPOT P99)
            / 无迁移前台 TPOT P99 × 100%

安全释放延迟 = old_extent_release_ts
              - max(last_host_reader_exit_ts, npu_event_done_ts)
```

安全释放延迟必须为非负，且其两个边界事件均已被实际观测；如果 NPU Event 未采集，不得把 Host Reader 退出时间单独当成安全释放条件。

### 6.4 证据分级

| 级别 | 允许内容 | 不允许内容 |
|---|---|---|
| `DEMO` | 固定 CSV、流程图、Host 伪实现和字段 schema | 软件 RCU 安全、P99、NPU 事件或硬件必要性结论 |
| `LAB` | 真实 Reader/测试设备/部分运行时的可复核结果 | 直接外推到所有 NPU、DMA 和生产分配器 |
| `MEASURED` | 真实迁移、Host+NPU 引用、完整性、释放和故障时间线 | 缺少设备事件、固定值、空值当零或只看进程不崩溃 |

## 7. 判定标准、无效证据与止损条件

### 7.1 证伪结论状态

- `GO`：在明确的设备引用、分配器、拓扑和迁移范围内，软件 RCU 的停顿、前台干扰、数据正确性和故障回滚均有完整 `MEASURED` 证据；可以表述为“在已验证条件下不需要硬件 Atomic Remap”；
- `CONDITIONAL`：软件路径在部分拓扑、Reader/NPU 访问模式或门限下成立，或硬件对照不可用；必须限定结论范围；
- `NO-GO`：出现提前释放、UAF、错误读取、旧代次消费、回滚失败或前台干扰超出止损线；表示当前软件实现不能准入；
- `NOT-SUPPORTED`：硬件 Atomic Remap、NPU Event、现场运行时或特定设备能力不可用；对应轨道不做性能结论；
- `INVALID-EVIDENCE`：固定样例、缺少真实 Reader/NPU、校验轮次不全、A/B 不公平、时间线无法对齐或实际路径不明。

### 7.2 无效证据规则

以下情况不能进入软件 RCU `MEASURED` 汇总：

- 直接使用 C++ DEMO 中的 `0.08ms`、`3.8µs`、`1.8%`、`0` 或 `TRUE`；
- 只运行 `rcu_migration_bench`，没有真实 Reader 和迁移动作；
- 只统计 Host Reader，未覆盖 NPU/DMA 异步引用；
- 只观察指针 CAS 成功，没有校验新块和释放旧块的顺序；
- 把“进程没有崩溃”当作无 UAF 或回滚成功；
- 运行不存在的 `verify_checksum.py`/`eval_cvt01.py`，或用手工 CSV 补齐结果；
- 用 `--hardware-supported` 作为硬件 Atomic Remap 能力证明；
- 用最大停顿值代替 P99，或把空字段填为 0/false；
- 故障注入没有记录触发点、旧/新块状态、generation 和释放事件。

### 7.3 立即止损条件

出现以下任一情况，应停止扩大迁移轮次和并发并保留现场：

- Reader 或 NPU 读取到 checksum 错误、半写数据或旧 generation；
- 旧 Extent 在 Host/NPU 宽限期完成前释放；
- 出现 UAF、野指针、设备地址越界或不可恢复的 NPU 上下文错误；
- 指针翻转后新块校验失败但仍进入 READY；
- 故障后软件 RCU 无法决定回滚、重试或显式失败；
- 前台 TPOT 长尾持续超过止损线且迁移器无法降速或暂停。

## 8. 阶段推进与闭环

### E0：命题、契约和证据边界确认

冻结“硬件 Atomic Remap 必要性”的适用范围、迁移对象、Reader/NPU 引用方式、门限和结果 schema。当前仓库只能完成固定结果 DEMO 审计。

### E1：Host Reader 软件 RCU

实现真实 Reader、Extent、Copy/CAS、checksum、epoch 和延迟释放，完成无故障 Host 并发正确性与停顿对照。

### E2：NPU 异步引用和故障回滚

接入真实 NPU Stream/DMA Event，验证双层宽限期、generation、半写块保护和四类迁移故障点。

### E3：前台混压和硬件对照

在真实前台 Decode/显存压力下完成 Stop-the-World、软件 RCU 和可用硬件路径 A/B；硬件不支持时明确记录 `NOT-SUPPORTED`。

### 条件证伪：引用协议的覆盖范围

改变 Reader 临界区、NPU 异步长度、DMA 访问方式、迁移块大小和故障点，检查软件 RCU 结论是否仍成立。若某类设备引用无法被软件协议观察，必须把该类场景从“已证伪硬件必要性”中剔除。

## 9. 研发人员与 AI Agent 执行约束

### 9.1 研发人员检查清单

- [ ] 已明确软件 RCU 结论的设备、分配器和引用协议边界；
- [ ] 真实 Reader、Extent、迁移轮次和 checksum 已接入；
- [ ] Host Reader 与 NPU/DMA 异步引用分别取证；
- [ ] 指针切换前完成拷贝和校验；
- [ ] 旧块释放晚于 Host/NPU 两类宽限期；
- [ ] generation、对象 ID、事件时间和 active 指针可对账；
- [ ] 故障点覆盖拷贝、切换、宽限期和释放前；
- [ ] Stop-the-World 与软件 RCU A/B 条件一致；
- [ ] 硬件能力来自探测，不是命令行布尔值；
- [ ] DEMO/LAB/MEASURED 未混入同一结论；
- [ ] 证伪结论没有超出已验证范围。

### 9.2 AI Agent 执行提示词

```text
你负责执行 CVT-01 PageMigration 软件 RCU 与硬件 Atomic Remap 必要性证伪。

先读取项目索引、公共 Benchmark 契约、本方案和原型目录。确认当前 rcu_migration_bench.cc 只解析 --hardware-supported/--out 并生成固定 DEMO CSV，不创建 Reader、不迁移数据、不调用 NPU；当前目录没有 verify_checksum.py 和 eval_cvt01.py。不要把固定 0.08ms、3.8us、1.8% 或硬件开关当作实测。

先实现真实 Host Reader、Extent 拷贝/CAS、checksum、generation 和延迟释放，再接入真实 NPU Stream/DMA Event，证明旧 Extent 的设备引用何时结束。比较 Stop-the-World 与软件 RCU 时固定 Reader、迁移轮次、块大小、前台负载和统计窗口。记录 reader_enter/exit、copy、checksum、pointer_flip、npu_event_done、extent_release、rollback 和错误事件。

故障注入覆盖拷贝后切换前、切换后宽限期内、释放前、checksum 失败和 NPU Event 超时。P99 从原始停顿样本计算，错误读取/UAF/旧代次消费不能用零或未崩溃替代。只有在明确的设备引用协议和完整 MEASURED 证据下，才可以输出“在已验证条件下不需要硬件 Atomic Remap”；否则输出 CONDITIONAL、NOT-SUPPORTED 或 INVALID-EVIDENCE。
```

### 9.3 常见问题定位

| 现象 | 原因定位 | 处理方式 |
|---|---|---|
| CSV 始终有固定 0.08ms/3.8µs | 当前程序是 schema DEMO | 保持 `DEMO`，接入真实 Reader 和迁移 |
| `--mode`/`--readers`/`--loops` 无效果 | 当前 C++ 没有这些参数解析 | 按真实 CLI 运行，或先补参数接口 |
| 找不到 `verify_checksum.py` | 当前目录没有该脚本 | 实现独立校验器，不手工补结果 |
| Host Reader 全部退出但仍有 NPU 访问 | Host 计数不能覆盖设备引用 | 接入 Stream/DMA Event，延迟释放旧块 |
| 指针 CAS 成功后 checksum 失败 | 新块校验与发布顺序错误 | 校验成功前禁止翻转，必要时回滚 |
| 迁移故障后进程没崩溃 | 进程存活不等于内存安全 | 检查 checksum、generation、释放时间和设备事件 |
| 硬件行显示 `DEMO_ONLY` | 传入了 `--hardware-supported` | 仍是 DEMO，执行能力探测和真实硬件路径 |
| 硬件 Atomic Remap 不可用 | 环境或驱动不支持 | 标记 `NOT-SUPPORTED`，限定软件证伪范围 |
| P99 被最大停顿替代 | 统计器没有逐次样本 | 保存每次 Reader 停顿并用统一分位算法 |
| 前台 TPOT 抖动但迁移数据正确 | 服务干扰未达门限 | 降低迁移并发/水位，按门限判 `CONDITIONAL` 或 `NO-GO` |
| 旧块释放时间早于 NPU Event | 双层宽限期不完整 | 立即停止压力，判定 `NO-GO` 并修复释放协议 |

本方案的完成标准不是“固定结果程序成功退出”，而是形成可复核链路：真实 Reader/NPU 引用 → 拷贝与校验 → 原子映射切换 → Host/NPU 双层宽限期 → 安全释放/回滚 → 前台停顿与 TPOT 统计 → 限定范围内的硬件必要性结论。
