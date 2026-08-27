# CVT-01：PageMigration 软件 RCU 与硬件 AtomicRemap 必要性证伪实施方案设计
## —— Mooncake 显存整理纯软化：软件 RCU 机制证伪硬件 AtomicRemap 芯片依赖

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。统一判定量为 Reader 停顿 P99 `<1ms`、TPOT 干扰率 `<3%` 和错误读取为 0；最大值不能替代 P99。无硬件 Atomic Remap 时硬件组标记 `NOT-SUPPORTED/N/A`。

> **验证 ID**：CVT-01  
> **验证名称**：页迁移/Defrag 软件 RCU 与硬件 Atomic Remap 原语必要性证伪  
> **验证优先级**：**🟢 P2 级（拓展证伪项）**  
> **对应验证阶段**：**条件证伪阶段 (架构简化与去依赖)**  
> **证伪标记**：**是（优先证伪“硬件 Atomic Remap 是内存整理迁移的必需依赖”）**  
> **建议周期**：3~4 人日  
> **主关联 IR**：`IR-01-01`, `IR-01-11`, `IR-01-12`  
> **核心 SRS / SR23 锚点**：  
> - SRS: `L4-CO-AtomicRemapPrimitive-065`, `L3-CO-MigrationRCULock-090`  
> - SR23: `SR23-01-01-03`, `SR23-01-11-01`, `SR23-01-12-02`  
> **开源基线版本与代码仓库**：  
> - **Mooncake 存储引擎**：[`https://github.com/kvcache-ai/Mooncake.git`](https://github.com/kvcache-ai/Mooncake.git) (Commit: `f90ae691f109e49a60920e0c8abbf7e572826d8c`，子模块: `mooncake-store/`)  
> **研发对齐状态**：已闭环研发评估报告 10 项与 RCU 宽限期检测机制（明确 Host Epoch 计数与 NPU Stream 硬件事件双层同步屏障）  

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统系统开发视角：Linux 内核 RCU (Read-Copy-Update) 无锁并发艺术

在 Linux 内核（如核心路由表更新、文件描述符表扩展）与超高性能无锁数据结构中，RCU（Read-Copy-Update，读-拷贝-更新）是实现“极高并发读取零锁停顿”的顶尖设计：
- **传统互斥锁（Mutex / Spinlock）的死穴**：当有成百上千个线程在并发读取一份共享数据时，若后台写线程要修改其中一个节点，加互斥锁会导致所有读线程全部挂起（Stop-the-world），引发严重的并发雪崩；
- **RCU 的精妙机制**：
  1. **读端（Reader）完全无锁**：读线程直接读取当前的旧节点指针，零锁竞争、零原子开销；
  2. **写端（Writer）后台拷贝更新**：写线程在后台复制一份新节点、完成修改；
  3. **原子指针翻转（Atomic Pointer Flip）**：写线程通过一次极速的 CAS（Compare-And-Swap）原子操作，将全局指针指向新节点；
  4. **宽限期等待（Grace Period）**：写线程等待所有正在读取旧节点的 Reader 全部退出临界区（宽限期结束）后，再异步释放旧节点的内存。

---

### 0.2 大模型显存碎片整理 (Defrag) 与硬件 Remap 芯片依赖的硬核证伪

在大模型长时间在线运行中：
- **显存碎片危机**：随着不同长度会话请求的频繁创建与销毁，NPU 显存中会散落大量无法分配的空闲碎片。存储引擎必须在后台将离散的物理页迁移合并为连续的大内存区间（Defragmentation）；
- **业界硬件派的观点（争议痛点）**：有人主张显存迁移时必须依赖底层 ASIC 芯片提供“硬件原子重映射（Hardware Atomic Remap）”原语，否则在迁移途中无法保证前台读线程的数据一致性。这种硬件依赖导致系统架构极度复杂，且受制于特定硬件厂商；
- **我们的纯软证伪使命**：
  - 我们借鉴 Linux 内核 RCU 思想，设计 **软件 RCU 双层同步屏障**（Host Epoch 计数器 + CANN NPU Stream 事件栅栏）；
  - 实测证明：在 32 并发 Reader 持续满载读取下，软件 RCU 实现 **P99 停顿 $< 1\text{ms}$**、**TPOT 干扰率 $< 3\%$**、**数据读取 Checksum 错误严格为 0**；
  - **结论：彻底证伪“专用硬件 Atomic Remap 芯片是必需品”的假设，确立纯软主路径，实现 100% 架构自主可控**！

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                   软件 RCU 显存页迁移无锁同步与双层宽限期时序                          │
├────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                        │
│  [ 前台 32 并发 Reader ] ──► 无锁读取旧 Extent (0 锁等待)                              │
│                                                                                        │
│  [ 后台 Defrag 迁移器 ]                                                                 │
│         ├── 1. 异步分配新连续 Extent 并通过 DMA 拷贝数据                                │
│         ├── 2. 原子 CAS 翻转指针: active_ptr = NewExtent (耗时 < 1us)                  │
│         ├── 3. 新 Reader 立即自动读取 NewExtent                                        │
│         └── 4. 双层宽限期检测 (Host Epoch == 0 && aclrtEventSynchronize 硬件完成)       │
│                     │                                                                  │
│                     ▼                                                                  │
│         [ 安全释放旧 Extent 物理内存 ]: 0 读脏、0 段错误、0 显存泄漏！                 │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. 验证目标与交付结论定义

### 1.1 现实前因痛点与待验证核心命题
1. **现实痛点**：显存碎片整理易引发前台推理停顿，而强依赖硬件 Remap 芯片会带来极高的采购成本与硬件死锁风险；
2. **核心命题**：
   - 证明纯软件 **RCU（Read-Copy-Update）** 与 Copy-on-Migrate 在 32 并发 Reader 持续读取下，迁移停顿 **$P99 < 1\text{ms}$**、TPOT 干扰率 **$< 3\%$**、错误读取为 0；
   - 彻底证伪硬件专用 Atomic Remap 芯片的必要性，确立纯软主路径。

### 1.2 最终交付数据与结论产出
1. **《Stop-the-world 锁表 vs 软件 RCU vs 硬件 Remap 迁移停顿与 Jitter 对比表》**；
2. **《高并发 Reader 下软件 RCU 内存一致性与 Checksum 校验表》**；
3. **《Go / Conditional / No-Go 证伪判定结论》**。

---

## 2. 核心数据结构与 RCU 宽限期双层屏障设计

### 2.1 核心数据结构定义

```cpp
#include <stdint.h>
#include <atomic>
#include <vector>
#include <acl/acl.h>
#include <acl/acl_rt.h>

struct alignas(64) RCUExtentNode {
    uint64_t extent_id;
    uint64_t phys_base_addr;      // 物理显存基址
    uint32_t size_bytes;
    uint32_t checksum;            // 数据块内容校验哈希 (xxHash32)
};

struct alignas(64) AtomicPageTableEntry {
    std::atomic<RCUExtentNode*> active_ptr{nullptr}; // 当前活跃指针 (原子 CAS 翻转)
    std::atomic<uint64_t> current_epoch{0};          // RCU Epoch 宽限期轮次
    std::atomic<uint32_t> active_readers{0};         // 活跃 Host Reader 计数
    aclrtEvent npu_quiescent_event{nullptr};         // NPU 侧静默点硬件事件屏障
};
```

---

## 3. 测试工具与工程构建规范

测试工程存放在 `./原型验证代码/CVT-01/` 目录下：

```
原型验证代码/CVT-01/
├── rcu_migration_bench.cc    # 32 并发 Reader 下 Stop-the-world 锁表 vs 软件 RCU 迁移停顿对比工具
├── verify_checksum.py        # 验证 100 轮迁移下数据一致性与 Checksum 脚本
├── eval_cvt01.py             # 统计分析迁移停顿与证伪判定报告脚本
└── Makefile                  # 编译构建工程 (make -j16)
```

编译方法：
```bash
cd ./原型验证代码/CVT-01 && make clean && make -j16
```

---

## 4. 分步执行测试操作规程 (SOP)

开发人员在执行 CVT-01 时，请严格按照以下 4 个步骤逐步执行，并理解每一步的操作意图：

### 步骤 1：运行传统全局互斥锁迁移基线测试
- **操作意图**：在 32 线程并发读取下，采用传统 Mutex 锁住整个显存页表执行页迁移，记录读线程遭遇的严重停顿（通常 $> 15\text{ms}$）与 TPOT 恶化幅度，作为对照基线。
- **执行命令**：
```bash
./rcu_migration_bench --mode mutex_lock --readers 32 --migrated-mb 1024 --loops 100 --out res_mutex.csv
```

### 步骤 2：运行纯软件 RCU 无锁页迁移测试
- **操作意图**：在相同 32 线程并发读取下，开启软件 RCU 机制（原子 CAS 指针翻转 + 宽限期延迟释放），测量 Reader 的停顿是否降低至 $P99 < 1\text{ms}$，TPOT 干扰率是否 $< 3\%$。
- **执行命令**：
```bash
./rcu_migration_bench --mode software_rcu --readers 32 --migrated-mb 1024 --loops 100 --out res_rcu.csv
```

### 步骤 3：验证数据一致性与零读脏
- **操作意图**：在 100 轮页迁移全过程中，检查所有 32 个 Reader 线程读出的 KVCache Checksum（xxHash32）是否与源数据 100% 逐字吻合，验证是否有读脏、半写脏块或野指针。
- **执行命令**：
```bash
python3 ./verify_checksum.py --input-csv res_rcu.csv --out-report checksum_report.json
```

### 步骤 4：生成证伪对账表与判定结论
- **操作意图**：对比传统加锁与软件 RCU 的停顿数据，给出证伪硬件 Atomic Remap 芯片的判定报告。
- **执行命令**：
```bash
python3 ./eval_cvt01.py --mutex res_mutex.csv --rcu res_rcu.csv --checksum checksum_report.json --out summary_cvt01.csv
```

---

## 5. 数据采集清单与记录格式

### 5.1 迁移停顿与 Jitter 测试数据表 (`res_rcu.csv`)
```csv
migration_scheme,reader_threads,migrated_mb,p99_pause_time_us,tpot_jitter_pct,checksum_errors,rollback_success
stop_the_world_lock,32,1024,18500.0,42.5,0,TRUE
software_rcu_epoch,32,1024,420.0,2.1,0,TRUE
hardware_atomic_remap,32,1024,380.0,1.8,0,TRUE
```

---

## 6. Go / Conditional / No-Go 判定规则

- **Go (证伪成功/准入)**：软件 RCU 迁移停顿 $P99 < 1\text{ms}$，TPOT 干扰率 $< 3\%$，错误读取为 0；
- **Conditional (条件准入)**：停顿在 $1\text{ms} \sim 3\text{ms}$，需缩小单个迁移 Batch 的 Extent 粒度；
- **No-Go (证伪失败)**：高并发下软件 RCU 读脏或崩溃，仍需底层硬件 Remap 支持。

---

## 7. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 7.1 研发任务拆解与分工
- **工程师职责**：
  1. 编译测试工程；
  2. 按照第 4 节 SOP 步骤执行 32 并发压测；
  3. 检查校验报告中的 Checksum 错误数是否严格为 0；
- **AI Agent 职责**：
  1. 负责 `rcu_migration_bench.cc` 中 CAS 无锁指针替换与 NPU Event 同步屏障逻辑；
  2. 自动生成停顿时间对比图表。

### 7.2 专属 Prompt 模板（可直接复制给 AI Agent）
```text
我是一名 C++ 系统级工程师，正在进行 CVT-01 软件 RCU 机制证伪验证：
1. 请阅读 ./原型验证代码/CVT-01/rcu_migration_bench.cc 与 Makefile；
2. 检查 RCU 原子指针翻转（CAS）与 Host Epoch / NPU Stream 宽限期等待逻辑，确保在释放旧页面前所有活跃 Reader 已安全退出；
3. 按照第 4 节 SOP 步骤执行压测程序，在 32 并发 Reader 线程下施加持续读压力，统计页迁移期间 Reader 的 P99 停顿耗时；
4. 输出对比传统互斥锁（Mutex）与软件 RCU 的性能对账表，验证停顿是否严格 < 1ms 且 Checksum 校验 100% 正确。
```

### 7.3 常见排错指南
- **Reader 读取到野指针触发段错误（Segmentation Fault）**：说明旧 Extent 在活跃 Reader 退出前被提前释放了，检查 `active_readers` 原子计数器的递增与递减配对；
- **NPU 算子读取到未迁移完成的半写数据**：检查 `aclrtEventSynchronize` 是否在指针翻转前正确执行了数据写入 Fence。
