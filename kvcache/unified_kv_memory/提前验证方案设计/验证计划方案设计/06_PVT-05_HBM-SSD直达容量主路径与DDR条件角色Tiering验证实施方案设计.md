# PVT-05：HBM-SSD 直达容量主路径与 DDR 条件角色 Tiering 验证实施方案设计
## —— Mooncake LocalCache 分层存储重构：打通 io_uring HBM↔SSD 直达主路径

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。固定样例只用于展示流程；正式对照包含纯 HBM、原生 Mooncake SSD Offload、DDR 中转和 SSD 直达四组。容量按实际可服务 Token/请求计算，Host DDR 触碰来自探针。

> **验证 ID**：PVT-05  
> **验证名称**：HBM↔SSD 直达容量主路径与 DDR 条件角色 Tiering（分层存储）验证  
> **验证优先级**：**🔴 P0 级（核心关键项）**  
> **对应验证阶段**：**E2/E3 分层存储扩容收益**  
> **证伪标记**：否（容量扩展价值确认）  
> **建议周期**：6~8 人日  
> **主关联 IR**：`IR-01-01`, `IR-02-08`, `IR-02-09`  
> **核心 SRS / SR23 锚点**：  
> - SRS: `L3-MS-Tiering-038`, `L3-MC-HIER-STORE-002`, `L3-MS-DDRRolePolicy-092`, `L3-SE-TierBypassPolicy-091`  
> - SR23: `SR23-01-01-01`, `SR23-01-01-02`, `SR23-01-01-03`, `SR23-01-08-01`, `SR23-02-08-01`, `SR23-02-09-01`  
> **开源基线版本与代码仓库**：  
> - **Mooncake 存储引擎**：[`https://github.com/kvcache-ai/Mooncake.git`](https://github.com/kvcache-ai/Mooncake.git) (Commit: `f90ae691f109e49a60920e0c8abbf7e572826d8c`，子模块: `mooncake-store/`)  
> **研发对齐状态**：已闭环研发评估报告 3 项与 TierBlockAllocator 规范（明确 io_uring 裸盘直达、4KB LBA 扇区分配器与 DDR 严格 Bypass）  

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统系统开发视角：操作系统多级存储分层 (Tiering) 与异步 I/O 进化史

在操作系统内存管理、虚拟内存 Swap 以及数据库 Buffer Pool 的经典架构中，分层存储（Tiering）是解决“内存昂贵且容量有限”的标准解法：
- **物理分层金字塔**：
  - 一级（SRAM / HBM）：读写带宽高达数 TB/s，延迟纳秒级，但容量极小（单卡仅 32~64GB）且成本极高；
  - 二级（Host DDR）：带宽数十至上百 GB/s，容量数百 GB，成本中等；
  - 三级（NVMe SSD）：顺序读写带宽高达数 GB/s ~ 数十 GB/s，容量数 TB ~ 数十 TB，单 GB 成本极低（仅为 HBM 的 1%）。
- **异步淘汰机制（Watermark LRU）**：
  - 当一级内存使用率达到高水位线（High Watermark，如 85%）时，后台守护线程异步启动扫描，将最久未被访问的冷数据页（LRU Cold Pages）刷写到大容量 NVMe SSD 中并释放显存；
  - 当显存占用回落至低水位线（Low Watermark，如 65%）时，后台停止换出；
  - 当某个请求再次需要冷数据时，按需从 SSD 异步加载（Page Fault / Restore）。

---

### 0.2 为什么必须使用 Linux 6.6+ `io_uring` FIXED Direct I/O？

为了极致压榨 NVMe SSD 的硬件顺序读写带宽（PCIe 4.0/5.0 可达 7 ~ 28 GB/s），传统的文件 I/O 方式存在不可逾越的性能瓶颈：

#### 1. 传统 POSIX `read/write` 的瓶颈：
- 每次读写都要经历两次 CPU 用户态/内核态上下文切换（Context Switch）；
- 数据必须经过 Linux 内核的 Page Cache，导致 Host CPU 产生沉重的 `memcpy` 拷贝开销，并严重污染系统内存。

#### 2. Linux `io_uring` FIXED Direct I/O 的降维打击：
- **提交与完成环形队列（SQ / CQ）**：应用程序在用户态直接将 I/O 请求填入提交队列（Submission Queue），内核异步拉取并执行，完成后填入完成队列（Completion Queue）。单次系统调用即可批量提交成百上千个 I/O，**系统调用开销降为 0**；
- **固定缓冲区注册 (`IORING_REGISTER_BUFFERS`)**：提前将 NPU HBM 或内存地址 Pin 锁定在内核中，消除每次 I/O 时的页表锁定开销；
- **Direct I/O (`O_DIRECT`) 裸盘直达**：彻底绕过 Linux Page Cache 和 Host DDR，直接由 NVMe 驱动控制 DMA 控制器在 SSD 物理扇区（LBA）与 NPU HBM 之间流转（**Payload Bypass DDR，Host 触碰字节严格为 0**）！

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                   io_uring FIXED Direct I/O 裸盘直达数据流通路                         │
├────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                        │
│   [ NPU HBM 显存 (冷 KVCache) ]                                                        │
│                 │                                                                      │
│                 ▼ (PCIe P2P DMA 直达, 严格绕过 Host DDR)                               │
│   [ NVMe SSD 裸块设备 (/dev/nvme0n1, 4KB 对齐物理扇区 LBA) ]                            │
│                 ▲                                                                      │
│                 │ (用户态 SQ 批量提交, 零系统调用上下文切换)                            │
│   [ io_uring 用户态提交队列 (Submission Queue Ring) ]                                  │
│                                                                                        │
│ 收益：顺序读写吞吐达到 NVMe 硬件物理峰值的 80% 以上，Host CPU 与 DDR 占用归零！        │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

### 0.3 4KB 物理扇区对齐 (`posix_memalign`) 的硬核要求

- **硬件约束**：NVMe 固态硬盘底层的物理读写单元是 4KB（4096 字节）逻辑块地址（LBA, Logical Block Address）。
- **Direct I/O 铁律**：当使用 `O_DIRECT` 与 `io_uring` 直接操作裸盘块设备时，Linux 内核要求：
  1. 内存缓冲区的物理起始地址必须按 **4096 字节严格对齐**；
  2. 磁盘文件/设备的偏移量（Offset）必须是 **4096 的整数倍**；
  3. 单次读写的字节长度（Length）必须是 **4096 的整数倍**。
- 如果违反上述任意一条，内核会立即返回 `-EINVAL`（Invalid Argument 22）错误！因此在代码中必须使用 `posix_memalign` 分配对齐内存，并在 `TierBlockAllocator` 中按 4KB 扇区管理 LBA 空间。

---

## 1. 验证目标与交付结论定义

### 1.1 现实前因痛点与待验证核心命题
1. **现实痛点**：显存容量极度昂贵匮乏，开源文件系统 Offload 极慢且严重抢占 CPU 资源；
2. **核心命题**：
   - **HBM ↔ SSD 直达容量主路径** 有效读写带宽达到 NVMe 物理设备顺序峰值的 **$\ge 80\%$**；
   - 在 130% ~ 200% HBM 额定容量的超载压力下，通过分层存储换入换出，实现**可服务有效 Token 容量提升 $\ge 30\%$**，**OOM 内存溢出率下降 $\ge 50\%$**；
   - 验证 **Payload 路径严格 Bypass Host DDR**（数据直接在 SSD 与 NPU HBM 间流转，Host DDR 触碰字节严格为 0）。

### 1.2 最终交付数据与结论产出
1. **《HBM ↔ SSD 裸盘与直达读写带宽达成率实测表》**；
2. **《超载压力下 纯 HBM vs DDR 中转 vs SSD 直达扩容与 OOM 对比表》**；
3. **《Payload Bypass DDR vs DDR 软中转 CPU 开销与时延对账表》**；
4. **《Go / No-Go 判定结论》**。

---

## 2. 核心数据结构与 TierBlockAllocator 驱动设计

### 2.1 核心数据结构定义

```cpp
#include <stdint.h>
#include <atomic>
#include <vector>
#include <unordered_map>
#include <mutex>
#include <liburing.h>

enum class TierLocation : uint8_t {
    HBM_ACTIVE = 0,    // 驻留在一级 NPU HBM
    SSD_EVICTED = 1,   // 已换出至 NVMe SSD 阵列
    MIGRATING = 2      // 正在异步换入/换出中
};

struct alignas(64) TierBlockDescriptor {
    uint64_t block_id;
    uint32_t token_count;
    TierLocation location;
    uint64_t hbm_phys_addr;       // HBM 物理基址
    uint64_t ssd_lba_offset;      // NVMe 块设备物理 LBA 扇区偏移 (4KB 严格对齐)
    uint32_t size_bytes;          // 块字节大小 (如 2MB Extent)
    std::atomic<uint64_t> last_access_epoch; // LRU 访问热度时间戳
    std::atomic<uint16_t> pin_count;         // 活跃推理 Pin 计数 (禁止驱逐)
};

class TierBlockAllocator {
private:
    uint64_t total_lba_sectors_;
    std::atomic<uint64_t> free_sector_head_{0};
    const uint32_t sector_size_bytes_ = 4096; // 4KB 扇区

public:
    TierBlockAllocator(uint64_t disk_size_bytes) 
        : total_lba_sectors_(disk_size_bytes / sector_size_bytes_) {}

    uint64_t allocate_lba_extent(uint32_t bytes) {
        uint64_t sectors_needed = (bytes + sector_size_bytes_ - 1) / sector_size_bytes_;
        uint64_t start_sector = free_sector_head_.fetch_add(sectors_needed, std::memory_order_relaxed);
        return start_sector * sector_size_bytes_;
    }
};

struct WatermarkConfig {
    double high_watermark_pct = 0.85; // 85% 显存占用触发异步换出
    double low_watermark_pct = 0.65;  // 降至 65% 停止换出
    uint32_t max_concurrent_ios = 32; // io_uring 最大并发 QD
};
```

---

## 3. 测试工具与工程构建规范

测试工程存放在 `./原型验证代码/PVT-05/` 目录下：

```
原型验证代码/PVT-05/
├── tier_storage_bench.cc      # NVMe SSD 直达压测工具
├── tier_allocator.h           # 4KB 对齐 LBA 块分配器
├── Makefile                   # 编译构建工程 (make -j16)
├── benchmark_tiering.py       # 150%~200% HBM 显存超载下分层扩容压测脚本
└── eval_tiering.py            # 四组模式对账与扩容收益分析脚本
```

编译方法：
```bash
cd ./原型验证代码/PVT-05 && make clean && make -j16
```

---

## 4. 分步执行测试操作规程 (SOP)

开发人员在执行 PVT-05 时，请严格按照以下 4 个步骤逐步执行，并理解每一步的操作意图：

### 步骤 1：测试 NVMe 裸盘直接顺序读写带宽
- **操作意图**：通过 `io_uring` FIXED Direct I/O 对 NVMe 裸盘进行 16MB、64MB 大块读写压测，测量裸盘能够达到的最大硬件顺序吞吐，验证是否达到标称值的 80% 以上。
- **执行命令**：
```bash
./tier_storage_bench --device /dev/nvme0n1 --block-size 16M --qd 32 --loops 100 --out res_ssd_direct.csv
```

### 步骤 2：启动纯 HBM 基线在 150% 超载下的打流压测
- **操作意图**：在不开启分层存储的情况下，向集群打入 150% 额定显存容量的并发请求，记录 OOM 崩溃次数与被驱逐丢弃的请求数，作为对照基线。
- **执行命令**：
```bash
python3 ./benchmark_tiering.py --mode pure_hbm --concurrency 64 --overcommit 1.5 --out res_pure_hbm.json
```

### 步骤 3：启动 SSD 直达分层存储在 150%~200% 超载下的打流压测
- **操作意图**：开启 `TierBlockAllocator` 与 `io_uring` 换出换入，在相同超载压力下打流，验证 OOM 发生率是否下降 50% 以上、可服务有效 Token 是否提升 30% 以上。
- **执行命令**：
```bash
python3 ./benchmark_tiering.py --mode hbm_ssd_direct --concurrency 64 --overcommit 1.5 --out res_ssd_tiering.json
```

### 步骤 4：生成四组模式对账表与扩容图表
- **操作意图**：汇总纯 HBM、Mooncake 原生 SSD Offload、DDR 软中转与 SSD 直达四组数据，输出对比表格。
- **执行命令**：
```bash
python3 ./eval_tiering.py --pure-hbm res_pure_hbm.json --ssd-tiering res_ssd_tiering.json --out summary_tiering.csv
```

---

## 5. 数据采集清单与记录格式

### 5.1 分层存储超载压测数据表 (`pvt05_tiering_results.csv`)
```csv
test_case,overcommit_pct,mode,active_requests,served_tokens_total,oom_count,preempt_count,ssd_write_bw_gbps,ssd_read_bw_gbps,host_ddr_touch_bytes
TC-01,100,pure_hbm,32,1048576,0,0,0.0,0.0,0
TC-02,150,pure_hbm,48,1180000,12,18,0.0,0.0,0
TC-03,150,ddr_staging,48,1420000,2,4,0.0,0.0,34359738368
TC-04,150,ssd_direct_io,48,1572864,0,0,24.5,26.8,0
```

---

## 6. Go / Conditional / No-Go 判定规则

- **Go (准入通过)**：
  - 在 150% 显存超载下，系统支持的可服务 Token 容量提升 $\ge 30\%$；
  - OOM 错误与请求驱逐发生率降低 $\ge 50\%$；
  - SSD 直达主路径全程 Bypass Host DDR（`Host Payload Touch Bytes` 严格为 0）。
- **Conditional (条件准入)**：容量提升在 $20\% \sim 30\%$ 之间；
- **No-Go (否决关闭)**：SSD 换入换出导致前台严重长尾抖动，或无法绕过 Host DDR。

---

## 7. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 7.1 研发任务拆解与分工
- **工程师职责**：
  1. 准备 NVMe 测试盘或裸分区；
  2. 按照第 4 节 SOP 步骤执行超载打流；
  3. 观察 `dmesg` 是否有 I/O 错误，检查 OOM 记录；
- **AI Agent 职责**：
  1. 负责 `tier_storage_bench.cc` 中 `io_uring` FIXED 缓冲区注册逻辑；
  2. 编写 Python 脚本自动计算超载容量提升率与 OOM 降低率。

### 7.2 专属 Prompt 模板（可直接复制给 AI Agent）
```text
我是一名存储/Linux 系统工程师，正在进行 PVT-05 HBM-SSD 直达分层存储扩容验证：
1. 请阅读 ./原型验证代码/PVT-05/tier_storage_bench.cc 与 benchmark_tiering.py；
2. 检查 io_uring 提交逻辑，确保使用了 IORING_OP_READ_FIXED / IORING_OP_WRITE_FIXED，且缓冲区使用 posix_memalign 进行了 4096 字节对齐；
3. 按照第 4 节 SOP 步骤执行压测，测试 NVMe 裸盘在 16MB、64MB 块大小下的顺序读写带宽；
4. 运行 benchmark_tiering.py 模拟 150% 与 200% 的显存超载流量，统计纯 HBM vs SSD 直达下的 OOM 发生次数与可服务 Token 提升比例；
5. 输出对比 CSV 表格。
```

### 7.3 常见排错指南
- **`io_uring` 报 `EFAULT` 错误**：检查显存指针是否成功通过 `io_uring_register_buffers` 进行了内核固定缓冲注册；
- **磁盘写入吞吐远低于预期**：检查是否开启了文件系统日志（Ext4/XFS），直达测试必须使用裸块设备分区并设置 `O_DIRECT`。
