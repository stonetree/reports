# Cache Stashing 与 DPU/SSD 直通：机制、DDR 放大及 KVCache 选型

> 技术总结与架构建议（截至 2026-07-31）  
> 输入范围仅限本目录指定的 5 篇讨论稿；厂商能力与年份另以公开的官方资料、标准组织和上游项目文档校核。

## 技术摘要

- **Cache stashing 的本质不是“大容量缓存”，而是把即将被 CPU 或另一相干 I/O 设备消费的 cache line，提前放到合适的 CPU cache 中。**它主要消除 DMA 落 DDR 后的再次取数、降低流水线停顿和 DDR 流量；其成败取决于数据是否在逐出前被消费。
- **L2 与 L3/LLC stashing 的定位不同。**L2 更适合几十字节到几十 KiB、绑核且立即消费的描述符/包头/状态；L3/LLC 容量和共享性更适合微分片 payload 流水线。Intel 公开保证的是 DDIO 到 LLC；AMD SDCI 公开材料描述为定向到处理器/CCD cache；Arm CHI 可指定不同层级，但 stash 是提示，目标节点可以拒绝。
- **对于 RNIC/DPU→SSD 的大块 KVCache 落盘，P2P 直通是数据面最优解；cache stashing 是 CPU 必须处理数据时的折中或控制面优化。**二者并非前后代替关系：直通解决“大块搬运与固定功能卸载”，stashing 解决“CPU 必须立即介入的低时延消费”。
- 在本文的 16 GiB 输入、解压后 2:1 膨胀示例中，传统 DDR 生命周期流量为 **2×、3×、6×或 7×**；理想 L3 微分片流水线可降为 **1×或 3×**。这里按完整 buffer 生命周期计入 dirty line 最终写回，避免把“SSD 读取前暂时 0 DDR”误写成永久 0×。
- **不要把网络 FCS/RoCE ICRC、应用端到端 CRC、NVMe/T10 DIF 视为同一层完整性。**同样，RDMA completion 只证明相应 RDMA 语义完成，不等价于 KVCache 已经持久化到 NAND；持久化还要等待 NVMe completion，并按需要使用 FUA/Flush。

## 1. 范围、术语与讨论稿校正

### 1.1 输入材料

本文综合了以下讨论稿：

1. [IO stash技术分析.md](IO%20stash技术分析.md)
2. [IO.md](IO.md)
3. [stash crc on off ddr write and read.md](stash%20crc%20on%20off%20ddr%20write%20and%20read.md)
4. [RDMA 机制讨论.md](RDMA%20机制讨论.md)
5. [RDMA 机制讨论 补充稿.md](RDMA%20机制讨论%20补充稿.md)

这些文件是讨论稿而非产品规格；其中的数值示例和厂商能力不能直接视为已验证事实。本文保留其有价值的机制主线，但做以下校正：

| 讨论稿中的说法 | 本文采用的校正口径 |
|---|---|
| IOStash、cache stashing 混用 | IBM `iostash` 块缓存、IoT 产品名与本文的 CPU cache stashing 无关。本文统一用 **cache stashing / cache injection**。 |
| Intel 新平台可定向进入私有 L2 | 公开资料明确的是 **Intel DDIO 进入 LLC**；不把未经产品手册证明的 L2 定向作为通用能力。 |
| AMD SDCI 固定进入私有 L2 | AMD 官方资料写的是 processor cache，并强调按 CCD 定向；本文不把它扩展成固定私有 L2。 |
| 所有 Neoverse/Grace/Graviton 天然具备相同 stash 能力 | Arm CHI 定义了协议能力，但具体 SoC、I/O requester、target cache 和固件必须实现；不能仅凭采用 Arm 核就推定支持。 |
| GPUDirect Storage 等于 DPU→SSD 直通 | GDS 是 **GPU memory↔storage** 的直接 DMA 软件栈。BlueField/DPU→NVMe P2P 应引用 DOCA Storage/STA、SPDK 或 Linux P2PDMA。 |
| 开启 L3 stash 后总 DDR 一定为 0× | 热窗口内可能暂时 0；完整生命周期中 DDIO 写入的 dirty line 通常仍会逐出到 DRAM。本文分别给出热窗口与生命周期口径。 |
| DPU 普遍内置 CRC、LZ4、Zstd 并可串成 SSD 线速通路 | 必须逐产品核验算法、吞吐、数据路径和 SDK；“有 crypto/compress 单元”不等于能在目标 NVMe 路径中 inline 使用。 |

### 1.2 关键术语

- **Cache stashing / cache injection**：I/O requester 或一致性互联发出提示/事务，使数据分配到未来消费者附近的 cache。
- **Intel DDIO**：把 I/O 的主要目的地/来源从 DRAM 提升到 LLC 的平台能力；2012 年随 Xeon E5 产品化。
- **PCIe TPH**：TLP Processing Hints。Requester 在 TLP 中携带 Processing Hint 和 Steering Tag，让平台选择更合适的处理/缓存资源；它是提示机制，不单独保证目标 cache 层级。
- **P2PDMA**：两个 PCIe endpoint 之间直接 DMA，不用 host RAM 作为 bounce buffer。
- **“直通”**：本文特指 payload 数据面不进入 host DDR；host CPU 仍可运行控制面、驱动和异常处理。
- **DDR 放大倍数**：一次业务输入字节数为分母，host DDR 的读字节加写字节为分子。除非特别说明，按完整 buffer 生命周期统计。

## 2. Cache stashing 的发展过程

Cache stashing 的演进不是单一厂商的一条产品线，而是“DMA 卸载—标准化提示—平台 cache 注入—定向与软件可控”的逐步发展。

| 年份 | 阶段 | 关键变化 | 技术意义与边界 |
|---:|---|---|---|
| 2006 | I/O 卸载前奏 | Intel I/OAT/QuickData 将大块内存搬运从 CPU 指令流卸载到 DMA engine。[Intel 2006 发布稿](https://www.intel.com/pressroom/archive/releases/2006/20061017comp.htm) | 解决“谁搬数据”，但不等于把 I/O 数据定向注入目标 cache。 |
| 2008 | PCIe 标准化提示 | PCI-SIG 发布 TPH ECN，定义 per-transaction Processing Hint 与 Steering Tag。[PCI-SIG TPH ECN](https://pcisig.com/PCIExpress/ECN/Base/TLPProcessingHints) | Endpoint 可以表达未来消费位置/资源提示；平台是否采纳由实现决定。 |
| 2012 | LLC 成为 I/O 数据首站 | Intel DDIO 随 Xeon E5 推出，使 I/O 可直接读写 LLC。[Intel DDIO](https://www.intel.com/content/www/us/en/io/data-direct-i-o-technology.html) | 首次大规模把 cache 作为通用 I/O 数据源/目的地，减少入站写 DRAM+CPU 再读。 |
| 2013–2017 | 一致性互联原生 stash | AMBA 5 CHI 于 2013 年公布；CHI-B 后加入 cache stashing，Arm 2017 年公开介绍其对网络/存储的价值。[Arm CHI 概览](https://documentation-service.arm.com/static/68590853961937560be90eb2) | 支持带数据或无数据 stash，可面向不同 cache 层级；请求是建议，target 可忽略。 |
| 2018 | 设备间直通进入上游内核 | Linux 4.20 引入 PCI P2PDMA 框架，早期重点就是 NVMe-oF target 的 RNIC↔NVMe CMB 路径。[Linux P2PDMA 文档](https://docs.kernel.org/driver-api/pci/p2pdma.html) | 与 stashing 形成互补：数据不需 CPU 时直接绕过 CPU cache/DDR。 |
| 2024 | TPH 软件闭环与 AMD 产品化 | Linux 建立通用 TPH API；AMD EPYC 9005 平台公开 SDCI 配置与使用方式。[Linux TPH](https://docs.kernel.org/PCI/tph.html)、[AMD EPYC 9005 DPDK 调优](https://docs.amd.com/api/khub/documents/TPtxZn7Ajbl4RMxb9StmzA/content) | cache 定向从平台默认行为走向 queue/core-aware 的驱动控制。 |
| 2025–2026 | 端点与低时延软件栈完善 | AMD Solarflare Onload/TCPDirect/EF_VI 支持 SDCI；NVIDIA DOCA 为 ConnectX-6+ 暴露 TPH 测试/配置参数。[AMD SDCI](https://docs.amd.com/r/en-US/ug1586-onload-user/SDCI)、[NVIDIA DOCA TPH](https://docs.nvidia.com/doca/sdk/doca-perftest/) | 硬件能力必须与 IRQ/RSS、队列绑核、驱动更新和应用消费窗口共同工作。 |

这条历史线说明：技术目标由“避免 CPU 搬运”演进为“让数据在未来消费者附近出现”。与此同时，P2PDMA 把“不需要 CPU 消费”的数据彻底移出 host memory hierarchy。现代系统应先决定消费者是谁，再选择 stashing、直通或二者的组合。

## 3. 两个方向的处理流程

### 3.1 RNIC/DPU → CPU cache → SSD

#### 控制面准备

1. 应用申请并 pin 输入/输出缓冲区，建立 IOVA/物理页映射；RDMA 路径注册 MR，获得 lkey/rkey。
2. 建立 QP/CQ，配置 RSS、RX queue、MSI-X vector 与处理线程的 CPU affinity。
3. 支持 TPH 时，驱动通过 ACPI `_DSM` 取得目标 CPU/内存类型对应的 Steering Tag，写入设备 ST 表；Intel DDIO 的基础 LLC 分配可对软件透明。
4. 创建 NVMe SQ/CQ，注册 DMA-able buffer；确定 CRC 是校验压缩输入还是解压后输出，确定 NVMe FUA/Flush 策略。
5. 为 L3 payload pipeline 分配有界 ring，设置高/低水位和 chunk ownership 状态；chunk 必须小于可用 I/O cache budget，而不是小于总 LLC 容量就算安全。

#### 数据面

```text
网络 → RNIC MAC/RDMA 校验 → 地址/权限翻译 → PCIe DMA Write
     → [DDIO/TPH/CHI：分配至 LLC/目标 cache]
     → CPU 对热 chunk 做应用 CRC、解压/格式转换（若需要）
     → 写入输出 cache line → 提交 NVMe Write
     → SSD DMA Read（相干命中 cache，未命中则读 DDR）
     → SSD controller/FTL/NAND → NVMe completion → 可选 Flush/FUA 完成
```

关键点：

- RNIC 已做的 Ethernet FCS/RoCE ICRC 只覆盖相应网络层；应用 CRC 或 T10 DIF 仍可能需要。
- CPU 必须在 chunk 被逐出前消费；NVMe 必须在输出 chunk 被逐出前发起相干 DMA read，才会得到 DDR 减负。
- `RDMA Write` 完成、CPU CRC 完成、NVMe controller 接收数据、数据持久化到非易失介质是四个不同完成点。
- Intel DDIO 对 partial-line inbound write 可能需要读取旧 line 做 merge；应使用 64 B 对齐、整 cache line 写，减少隐含 read-for-ownership/merge。

### 3.2 SSD → CPU cache → RNIC/DPU

```text
应用命中 prefix/KV 索引 → 提交 NVMe Read
→ SSD 从 NAND/内部 DRAM 取数 → PCIe DMA Write
→ [DDIO/TPH/CHI：进入 LLC/目标 cache]
→ NVMe CQE 表示 chunk 可用，内存屏障后发布给发送队列
→ RNIC DMA Read（相干命中 cache）→ RDMA/RoCE 封包 → 远端
```

处理顺序如下：

1. 软件把大 KVCache 拆成固定 chunk，维护 `FREE → SSD_FILLING → READY → RNIC_READING → FREE` 状态机。
2. SSD 完成 DMA 后，CPU/轮询线程读取 CQE；必要的 DMA read barrier 之后才把同一 chunk 的 SGE 发布给 RNIC。
3. RNIC 的相干 DMA read 若命中 LLC，可直接取得最新 dirty line；若平台不支持相干 I/O read、chunk 已被逐出或跨 NUMA，便回退到 DDR。
4. RNIC CQE 到达后才能复用该 chunk。仅看到 SSD CQE 不能提前覆盖 buffer。

对于大块 prefix cache，L3 只是短暂 bounce window，不是容量层。稳态工作集近似为：

```text
W_cache ≈ chunk_size × (SSD 并发填充数 + RNIC 并发发送数 + 安全余量)
```

必须满足 `W_cache < I/O 可用 cache budget`，并用 LLC occupancy、DDIO hit/miss、IMC read/write、RNIC/SSD queue depth 实测，而不能仅靠理论容量判断。

### 3.3 SSD ↔ RNIC 真正 P2P

Linux 上游文档给出的已支持布局是：NVMe PCI driver 暴露 CMB 作为 P2P memory；RDMA driver 让 RNIC 直接 DMA 到该 memory；NVMe target 负责调度两端。拓扑路径在到达 host bridge 前是规范明确的；一旦需要穿过 host bridge/不同 hierarchy domain，内核默认阻止，除非硬件进入 allow list。[Linux P2PDMA](https://docs.kernel.org/driver-api/pci/p2pdma.html)

```text
远端 RDMA → RNIC ──PCIe P2P──> NVMe CMB/PMR → NVMe controller → NAND
NAND → NVMe CMB/PMR ──PCIe P2P──> RNIC → 远端 RDMA
                 （host DDR = 0；host CPU 仅控制面）
```

“同一 PCIe switch”不是协议层绝对必要条件，但它是最容易被 Linux 判定为安全、可路由且性能稳定的部署。仅处于同一 socket/Root Complex 并不自动意味着 P2P 可用：必须检查 root port、host bridge、ACS redirect、IOMMU、驱动和设备 DMA addressing。

## 4. KVCache 落盘的 DDR 放大量化

### 4.1 模型与假设

定义：

- `P`：RNIC 收到的 payload 字节数；也是放大倍数分母。
- `r`：解压膨胀比；解压后 `U = rP`。示例取 `P = 16 GiB`、`r = 2`，所以 `U = 32 GiB`。
- CRC 为只读计算，不修改 payload；网络层 ICRC 不替代这里的应用端到端 CRC。
- 解压输出使用独立 buffer；采用整 cache line 写或等价优化，**不计冷目标普通 store 的 RFO**。若发生 RFO，所有含解压场景再增加 `U` 的 DDR read，即示例再加 32 GiB / 2×。
- “传统”路径为 RNIC 写 DDR、CPU 读写、NVMe 从 DDR 读；“L3 stash”要求输入和输出都以微分片方式在逐出前被 CPU/NVMe 消费。
- L3 stash 的主表按**完整生命周期**统计 dirty line 最终写回。热窗口内，在 eviction 发生前可观测到 0 DDR，但不能把它当成生命周期的永久 0×。
- CRC+解压优先采用单次扫描：在解压读取输入时同时累计 CRC。若分成两个冷扫描，另列敏感性行。

### 4.2 通用公式

| 场景 | 传统路径 DDR 写 | 传统路径 DDR 读 | 传统总量/放大 | 理想 L3 stash 生命周期 DDR 写 | stash DDR 读 | stash 总量/放大 |
|---|---:|---:|---:|---:|---:|---:|
| CRC 关，解压关 | `P` | `P` | `2P` / **2×** | `P` | `0` | `P` / **1×** |
| CRC 开，解压关 | `P` | `2P`（CPU+SSD） | `3P` / **3×** | `P` | `0` | `P` / **1×** |
| CRC 关，解压开 | `P+U` | `P+U` | `2(P+U)` / **2(1+r)×** | `P+U` | `0` | `P+U` / **(1+r)×** |
| CRC 开，解压开（融合扫描） | `P+U` | `P+U` | `2(P+U)` / **2(1+r)×** | `P+U` | `0` | `P+U` / **(1+r)×** |
| CRC 开，解压开（两个冷扫描） | `P+U` | `2P+U` | `3P+2U` / **(3+2r)×** | `P+U` | `0` | `P+U` / **(1+r)×** |

为什么 stash 仍有 `P` 或 `P+U` 的写：RNIC 注入和 CPU 产生的最新 cache line 相对 DRAM 是 dirty 的，通用 write-back cache 在逐出时会写回。Intel 的 DDIO 监控文档也明确描述了 dirty victim 会触发 memory write。[Intel DDIO 事务说明](https://www.intel.com/content/www/us/en/developer/articles/technical/ddio-analysis-performance-monitoring.html)

### 4.3 16 GiB、2:1 解压的详细计算

| CRC | 解压 | 实现方式 | 传统 DDR 写 | 传统 DDR 读 | 传统总量 | 传统放大 | L3 stash DDR 写 | stash DDR 读 | stash 总量 | stash 放大 | 生命周期流量下降 |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 关 | 关 | 纯转存 | 16 GiB | 16 GiB | 32 GiB | **2×** | 16 GiB | 0 | 16 GiB | **1×** | 50.0% |
| 开 | 关 | CPU 单次 CRC | 16 GiB | 32 GiB | 48 GiB | **3×** | 16 GiB | 0 | 16 GiB | **1×** | 66.7% |
| 关 | 开 | CPU 解压，输出 32 GiB | 48 GiB | 48 GiB | 96 GiB | **6×** | 48 GiB | 0 | 48 GiB | **3×** | 50.0% |
| 开 | 开 | CRC 融入解压扫描 | 48 GiB | 48 GiB | 96 GiB | **6×** | 48 GiB | 0 | 48 GiB | **3×** | 50.0% |
| 开 | 开 | CRC 与解压分离且输入冷失效 | 48 GiB | 64 GiB | 112 GiB | **7×** | 48 GiB | 0 | 48 GiB | **3×** | 57.1% |

逐项解释：

- **纯转存 2×**：RNIC 写 `P`，SSD 读 `P`。
- **CRC 3×**：在纯转存基础上，CPU 额外读 `P`。
- **解压 6×（r=2）**：RNIC 写 `P` + CPU 读 `P` + CPU 写 `U` + SSD 读 `U` = `2P+2U=6P`。
- **CRC+解压融合仍为 6×**：CRC 在解压消费输入的同一遍循环完成，不新增整份 DDR read。
- **两个冷扫描为 7×**：CRC 先读 `P`，输入超过 cache 后解压又读 `P`，增加 1×。

### 4.4 热窗口、退化与直通三种边界

| 路径状态 | Host DDR 流量 | 含义 |
|---|---:|---|
| L3 stash 热窗口、尚未逐出 | 0×（瞬时观测） | CPU 和 NVMe 均从 cache 命中；dirty line 还没有写回。 |
| L3 stash 完整生命周期 | 上表的 1×/3× | dirty line 最终逐出；仍显著减少 DDR read。 |
| L3 stash 失效/提前逐出 | 回退到 2×/3×/6×/7× | cache 太小、消费者太慢、跨 NUMA、非相干 DMA 或队列拥塞。还可能额外污染 CPU 工作集。 |
| DPU/SmartNIC + NVMe P2P，且所需 CRC/解压均在直通数据路径内 | **0× host DDR** | 可能仍使用 DPU-local DDR/SRAM、NVMe CMB/PMR；“0×”只指 host DDR。 |

## 5. Cache stashing 的完整软硬件依赖

| 层级 | 必需能力 | 关键检查项 | 缺失时的表现 |
|---|---|---|---|
| I/O endpoint | DDIO 可透明受益，或 endpoint 具备 TPH extended capability、ST table/processing hint；设备 firmware 能按 queue 设置 | `lspci` capability、设备手册、firmware 版本、整 line DMA | 普通 DMA 或非定向 LLC 分配 |
| CPU/SoC Root Complex | DDIO、SDCI 或 CHI stash；I/O coherent read/write；本地 cache home agent | CPU SKU/stepping、socket/CCD/NUMA、相干属性 | 直接 DRAM、跨 socket 或 cache miss |
| Cache/QoS | 足够的 LLC ways/容量与可观测 PMU；必要时 cache allocation/隔离 | DDIO occupancy/hit/miss、CPU LLC MPKI、CAT/RDT 策略 | cache thrash、dirty eviction、CPU 热点被冲掉 |
| BIOS/firmware | 开启 DDIO/SDCI/TPH；ACPI `_DSM` 提供 Steering Tag；正确 IOMMU/ACS 策略 | BIOS 选项、ACPI 表、平台 errata | 特性静默关闭或 tag 无法取得 |
| OS 内核 | Linux `CONFIG_PCIE_TPH`；PCIe TPH API；DMA/IOMMU 与 memory type 支持 | `pcie_enable_tph()`、`pcie_tph_get_cpu_st()`、内核版本 | 驱动无法启用/更新 ST |
| 设备驱动 | 在 queue/IRQ affinity 变化时更新 ST；正确内存屏障和 DMA sync | RX/TX queue→CPU 映射、MSI-X notifier、MR/IOVA | 数据进错误 CCD/cache，跨核 snoop |
| 数据面软件 | DPDK/Onload/SPDK/RDMA verbs 等支持 pinned hugepage、异步队列、批处理 | queue depth、chunk ring、CQ polling、背压 | 控制面开销或 buffer 生命周期失控 |
| 应用调度 | 线程绑核；生产者与消费者时间接近；小工作集；CRC/解压融合 | chunk age、消费延迟、P99/P999、迁核 | 数据在消费前逐出，收益消失 |
| 数据布局 | 64 B 对齐、整 line DMA、避免 false sharing；输出避免 RFO | buffer alignment、SGE 边界、padding | partial write merge/RFO 带来隐藏 DDR read |
| 可验证性 | 同时测 IMC、LLC、PCIe、RNIC、NVMe 和端到端延迟 | 基线、stash on/off、direct、冷/热/拥塞测试 | 只能看到吞吐，无法确认数据实际走哪条路 |

Linux TPH 的软件闭环是：内核发现 capability，driver 显式启用，按目标 CPU/内存类型取得 ST，再把 ST 写入设备表；IRQ affinity 改变时驱动应同步更新。[Linux TPH API](https://docs.kernel.org/PCI/tph.html)

## 6. 厂商 Cache stashing 布局

| 厂商/生态 | 首次公开/产品年份 | 硬件/协议布局 | 软件布局 | 可确认的目标层级 | 证据强度与限制 |
|---|---:|---|---|---|---|
| Intel | **2012** | Xeon E5/E7 v2 起的 DDIO；当前 Xeon Scalable/Xeon 6 延续。I/O read/write 可直接访问 LLC；近代平台可用 BIOS/TPH 控制 allocating/non-allocating write。 | 默认对 OS/driver 透明；VTune/uncore PerfMon 监控；RDT/CAT 可做 LLC 资源治理。 | **LLC** | [Intel DDIO 产品说明](https://www.intel.com/content/www/us/en/io/data-direct-i-o-technology.html)、[事务与监控](https://www.intel.com/content/www/us/en/developer/articles/technical/ddio-analysis-performance-monitoring.html)。不把通用能力扩写成私有 L2。 |
| AMD | **2024**（EPYC 9005） | Zen 5 EPYC 的 SDCI；I/O 根据 ST 把入站数据、event、metadata 预装到处理器 cache，并按 CCD 定向。Solarflare X2/X4 可作为支持 endpoint。 | BIOS SDCI；Linux TPH；AMD `sfc` driver、Onload 9.1、TCPDirect 9.1、EF_VI。 | 官方公开为 processor/CCD cache | [AMD EPYC 9005 调优](https://docs.amd.com/api/khub/documents/TPtxZn7Ajbl4RMxb9StmzA/content)、[AMD Onload SDCI](https://docs.amd.com/r/en-US/ug1586-onload-user/SDCI)。不能据此断言固定私有 L2。 |
| Arm IP 生态 | **2017**（CHI-B cache stashing 公开） | AMBA 5 CHI 的带数据/无数据 stash transaction，支持 target LPID/节点并可面向不同 cache 层。SoC 必须集成相应 RN/HN/target。 | SoC firmware、interconnect driver、设备 driver 与应用 affinity 均为实现相关；无单一通用发行版开关。 | **协议允许多层级** | [Arm 2017 CHI 增强说明](https://developer.arm.com/community/arm-community-blogs/b/soc-design-and-simulation-blog/posts/introducing-new-amba-5-chi-protocol-enhancements)、[Arm CHI cache stashing](https://documentation-service.arm.com/static/68590853961937560be90eb2)。请求是 hint，target 可忽略；不能仅凭“Arm CPU”推定支持。 |
| NVIDIA 网络端点 | ConnectX-6 Dx **2019**；公开 DOCA TPH 工具链见 **2026** 文档 | ConnectX-6+ 可发 PCIe TPH，指定 target CPU core 和 volatile/persistent memory type；需搭配支持 TPH/cache injection 的 host。 | DOCA perftest/SDK、mlx5 firmware/driver、TPH-enabled Linux。 | 由 host 平台决定 | [ConnectX-6 Dx 2019 发布](https://nvidianews.nvidia.com/news/releases-20210113-6829469)、[NVIDIA DOCA TPH](https://docs.nvidia.com/doca/sdk/doca-perftest/)。这是 requester 侧能力，不等价于 BlueField 可把数据直塞任意 host L2。 |
| 跨厂商标准/内核 | TPH **2008**；Linux 通用支持 **2024** | PCIe TPH + ST；ACPI `_DSM` 提供 platform-specific tag。 | `CONFIG_PCIE_TPH` 及通用 API，设备 driver 自行决定 queue/IRQ 策略。 | 平台实现决定 | [PCI-SIG TPH](https://pcisig.com/PCIExpress/ECN/Base/TLPProcessingHints)、[Linux TPH](https://docs.kernel.org/PCI/tph.html)。 |

## 7. 厂商 DPU/IPU 与 SSD 直通/存储卸载布局

本表严格区分三种证据：**明确 P2P 到本地 NVMe**、**已产品化的 NVMe/NVMe-oF 存储卸载**、**只有通用 DPU/PCIe 能力但未公开直通细节**。

| 厂商/方案 | 发布年份 | 硬件与数据路径 | 软件栈 | CRC/压缩/保护能力的公开边界 | 成熟度判断 |
|---|---:|---|---|---|---|
| Linux RNIC↔NVMe P2PDMA 基线 | **2018**（Linux 4.20） | NVMe driver 暴露 CMB；RNIC DMA 到 CMB；`nvmet` 调度 RNIC 与 NVMe，host RAM 不作 bounce buffer。 | Kernel `pci_p2pdma`、NVMe target、RDMA driver；拓扑/ACS/IOMMU 检查。 | 不规定 transform engine；完整性由 RNIC/NVMe/应用另行组合。 | **明确、上游支持，但硬件/拓扑约束强。**[内核文档](https://docs.kernel.org/driver-api/pci/p2pdma.html) |
| NVIDIA BlueField-3 + DOCA Storage/STA | **2021** | BlueField-3 具备 400G、PCIe 5.0、DPA；DOCA NVMe-oF target 文档明确 DPA 通过 **PCIe P2P topology** 访问 NVMe drive。 | DOCA、DOCA STA、SPDK、SNAP/NVMe emulation。 | BlueField 软件公开 T10 DIF signature、crypto 等；DOCA Compress 可用，但是否能在目标 KV 路径 inline 串接必须按版本验证。 | **明确的 DPU→NVMe P2P 产品路径。**[BlueField-3 发布](https://nvidianews.nvidia.com/news/nvidia-extends-data-center-infrastructure-processing-roadmap-with-bluefield-3)、[DOCA NVMe-oF target](https://docs.nvidia.com/doca/archive/2-9-0-cx8/doca%2Bnvme-of%2Brdma%2Btarget%2Breference%2Bapplication%2Bguide/index.html) |
| Intel IPU E2000 / F2000X-PL | **2022** | E2000 提供 RDMA、NVMe offload、NVMe interface；F2000X-PL 面向 NVMe-oF、RoCE、compression、crypto。可连接 storage disk/front storage target。 | IPDK（扩展 DPDK/SPDK）、SPDK、伙伴 Link-Storage。 | E2000/F2000X 的公开资料确认 compression/crypto 与 NVMe offload；具体 CRC/算法/本地 SSD P2P 要看方案商 bitstream/软件。 | **产品化存储卸载；公开资料未把所有 SKU 定义成统一 RNIC↔本地 SSD P2P。**[E2000](https://community.intel.com/t5/Blogs/Tech-Innovation/Data-Center/Intel-Details-IPU-and-New-Circuit-Innovation-at-ISSCC-2023/post/1453900)、[F2000X-PL](https://www.intel.com/content/www/us/en/products/details/fpga/platforms/ipu/f2000x-pl-platform.html) |
| AMD Pensando Salina | **2024** | 400G、PCIe 5.0；可向 host 模拟 NVMe VF，把 NVMe/PCIe 命令转成加密的 NVMe/TCP，并用于 DPU-managed NVMe/KV-cache。 | Pensando SSDK、P4 pipeline、NVMe virtualization/storage service。 | 公开确认 AES-XTS、Header/Data Digest、NVMe/TCP；不据此推断 LZ4/Zstd 或本地 SSD P2P 已普遍 inline。 | **成熟的网络存储卸载；本地 DPU↔SSD P2P 需具体平台证明。**[2024 发布](https://ir.amd.com/news-events/press-releases/detail/1220/amd-delivers-leadership-ai-performance-with-amd-instinct-mi325x-accelerators)、[Salina 产品简介](https://www.amd.com/content/dam/amd/en/documents/pensando-technical-docs/product-briefs/pensando-salina-product-brief.pdf) |
| AWS Nitro + Nitro SSD | **2021**（Nitro SSD） | Nitro Card 分别承担 VPC、EBS、Local NVMe；Nitro SSD 面向本地高性能存储，I/O 与加密由专用卡/SoC 卸载。 | AWS 封闭固件、Nitro Hypervisor、EC2/EBS/instance store。 | 官方确认网络和存储 hardware encryption；内部 P2P/压缩细节不对用户开放。 | **大规模商用但封闭，不是可移植的用户可编程直通方案。**[Nitro SSD](https://aws.amazon.com/blogs/aws/aws-nitro-ssd-high-performance-storage-for-your-i-o-intensive-applications/)、[Nitro 组件](https://docs.aws.amazon.com/whitepapers/latest/security-design-of-aws-nitro-system/the-components-of-the-nitro-system.html) |
| Marvell OCTEON 10 | **2021** | Arm Neoverse N2、PCIe 5.0、DDR5、400G+ datapath、inline security/packet accelerators，定位网络/安全/存储 workload。 | Marvell SDK/DAO、DPDK/VPP 与伙伴存储软件。 | 公开确认 crypto/packet/ML 能力；未找到统一的 OCTEON 10 RNIC↔本地 SSD P2P 产品路径说明。 | **具备构建基础，不应写成已验证的通用 SSD 直通。**[Marvell 2021 发布](https://www.marvell.com/company/newsroom/marvell-extends-octeon-leadership-industry-first-5nm-dpu.html) |

补充说明：SPDK 的普通 NVMe-oF RDMA “zero-copy”是 **RNIC→一块 host memory→SSD**，不在 host memory 内二次复制，但仍产生 1 次 DDR 写+1次 DDR 读；不能与 PCIe P2PDMA 的 zero-host-DDR 混用。[SPDK NVMe-oF Target](https://spdk.io/doc/nvmf_tgt_pg.html)

## 8. KVCache 场景：DPU/SSD 直通与 L2/L3 stashing 对比

| 维度 | DPU/IPU/SmartNIC→SSD P2P | L2 stashing | L3/LLC stashing |
|---|---|---|---|
| 最合适的数据 | MB–GB 级 payload、固定格式流 | 64 B–几 KiB 的 descriptor/header/flag | 几 KiB–几十 MiB 的有界 micro-chunk |
| Host DDR | 理想为 0 | payload 通常仍在 DDR；主要省控制元数据 read | 热窗口省 read；完整生命周期通常仍有 dirty writeback |
| Host CPU | 仅控制/异常；transform 若硬件支持可全卸载 | CPU 必须立即消费，适合极短控制流 | CPU 可做 CRC/轻量 transform，但会消耗核心 |
| 延迟/吞吐决定因素 | PCIe topology、P2P memory、DPU engine、SSD | core affinity、stash target、轮询/IRQ | LLC budget、consumer gap、chunk pipeline、coherent DMA |
| 可编程性 | 受 DPU ISA/P4/SDK/engine 算法限制 | CPU 最灵活 | CPU 最灵活 |
| 部署复杂度 | 最高：硬件拓扑、ACS/IOMMU、双端驱动、DPU 软件、NVMe | 高：endpoint/SoC/firmware/driver/绑核 | 中高：平台 cache 行为、chunking 和观测 |
| 失败/降级 | P2P 不可达时回退 host buffer；某些 transform 需 DPU DDR | tag 错配、迁核、target 拒绝，回退 LLC/DDR | 提前逐出，回退 DDR 并污染共享 LLC |
| 数据保护 | 可在 DPU/SSD inline，但必须验证具体算法和覆盖范围 | CPU 软件最容易实现自定义 end-to-end checksum | 同左；适合融合 CRC+transform |
| KVCache 结论 | **落盘/换出首选**，尤其纯搬运或固定 transform | **仅控制面首选**，不适合大 payload | **无 P2P 或 CPU 必须处理 payload 时的备选/混合方案** |

推荐的混合架构是：

```text
控制元数据、索引命中、错误与策略决策 ──> L2/L3 stashing ──> 绑核 CPU
大块 KV payload（无需 CPU）          ──> DPU/NVMe P2P ──> SSD
需 CPU 自定义变换的少数流            ──> L3 micro-chunk pipeline ──> SSD
```

## 9. 除直通和 stashing 外的可选加速方案

| 方案 | 价值 | Host DDR 影响 | 适用条件/限制 |
|---|---|---:|---|
| GPU Direct Storage + GPU 侧压缩/量化 | KVCache 本就在 GPU HBM 时，直接 GPU↔本地/远端存储，避免 CPU bounce buffer；transform 可与 CUDA stream 编排。 | 可到 0 | 需要支持 GDS 的 GPU、文件系统/块层、`cuFile`、O_DIRECT/对齐和合适 PCIe topology。[GDS Overview](https://docs.nvidia.com/gpudirect-storage/overview-guide/index.html) |
| GPUDirect RDMA + GDS 两段流水 | 远端 KV 先进入 GPU HBM，GPU 做量化/压缩，再 GDS 落盘；适合本来就需要 GPU 处理的数据。 | 可到 0 | 多占 GPU HBM/SM/Copy Engine；若只是纯落盘，绕 GPU 不一定划算。 |
| Intel IAA/QAT/DSA 或独立 accelerator | 把 CRC、compress/decompress、copy 从通用 CPU 核卸载；算法比 DPU P4 更适合 bulk transform。 | 通常仍经过 DDR，除非系统把 accelerator 串入真正 P2P path | IAA/QPL 公开支持 CRC、压缩/解压等，但要测内存带宽和 NUMA；“offload CPU”不等于“绕过 DDR”。[Intel QPL/IAA](https://www.intel.com/content/www/us/en/developer/articles/technical/optimize-data-manipulation-with-qpl-and-dml.html) |
| Computational Storage / SmartSSD | 在 SSD controller/FPGA 附近做压缩、校验、过滤、去重，减少 PCIe 字节和 host 计算。 | 可显著降低 | 可编程模型、算法生态、SSD 成本与故障域；KV 格式要与设备协同设计。 |
| CXL.mem 容量层/内存池 | 热 KV 留在可字节寻址的扩展内存，避免频繁 SSD 读写；适合容量大但仍有复用的数据。 | 使用 CXL memory bandwidth，不是 host DDR | 延迟高于本地 DRAM、容量/带宽成本高、软件 tiering 与 NUMA 策略复杂。 |
| SPDK 用户态零拷贝、hugepage、polling | 即使必须经过 host memory，也避免内核栈拷贝、锁和 syscall，改善 p99。 | 仍约 2×（纯 RNIC→DDR→SSD） | 最成熟的保底路径；同 NUMA、1 GiB hugepage、queue/core 一一映射很重要。[SPDK 概览](https://spdk.io/doc/about.html) |
| 源端量化/压缩 + 内容寻址/去重 | 在上网前减少 `P`；prefix/KV 的块级 hash 与去重可同时降低网络、PCIe、DDR、SSD 写量。 | 按压缩/去重率同比下降 | 会引入计算与索引元数据；需要稳定 block format、版本和校验语义。 |
| 分层选择性写入 | 热 prefix 留 HBM/CXL/DDR，温数据 SSD，低复用数据直接丢弃/重算；减少无收益落盘。 | 从根源减少流量 | 需要命中率、重算成本、SSD endurance 和 QoS 联合策略。 |

## 10. 额外值得关注的重点

### 10.1 完成、可见性与持久化必须分层

建议为每个 chunk 维护至少四个状态：

1. `RDMA_RECEIVED`：RNIC 已满足 RDMA transport completion 语义。
2. `CPU_TRANSFORMED`：CRC/解压/量化完成，输出对 NVMe DMA 可见；发布状态前执行正确的 DMA memory barrier。
3. `NVME_COMPLETED`：controller 已完成命令。
4. `DURABLE`：若业务要求掉电后可恢复，FUA/Flush 或设备声明的持久化边界已满足。

不能用一个 flag 同时代表这四层完成。CRC 元数据也应包含版本、压缩算法、原始长度、压缩长度、模型/层/序列位置和 generation，避免“字节正确但语义错配”。

### 10.2 应同时优化三种放大

- **Host DDR 放大**：本文重点，受 stashing/P2P/transform 流程影响。
- **PCIe 放大**：host hairpin、DPU-local bounce、重复扫描或输出膨胀都会增加 PCIe 字节。
- **SSD 写放大**：FTL garbage collection、对齐不佳、小随机写、重复 KV block 会让 NAND 写量高于 host write。仅把 host DDR 降到 0 并不保证 SSD endurance 或落盘延迟最优。

### 10.3 评估不能只跑“热且无拥塞”的平均吞吐

最少覆盖以下实验矩阵：

| 维度 | 测试点 |
|---|---|
| 路径 | DDR baseline、L3 stash、P2P direct、fallback |
| 数据 | 4 KiB/64 KiB/1 MiB/16 MiB chunk；压缩比 1.0/1.5/2/4 |
| 处理 | 无 transform、CRC、解压、融合 CRC+解压、两个独立 pass |
| 压力 | 单流/多流；SSD queue depth 1/8/32/128；RNIC 满速；CPU 背景 LLC 压力 |
| 拓扑 | 同 switch、同 root port、跨 root port、跨 socket/NUMA |
| 指标 | host IMC R/W、LLC occupancy/hit/miss、PCIe TLP、DPU-local DRAM、SSD bandwidth/latency/endurance、CPU cycles/byte、P50/P99/P999 |

判定 stash 成功的证据不是“功能正确”，而是同一业务字节下 **IMC read 明显下降、LLC hit 上升、CPU MPKI 未恶化、P99 未因逐出抖动上升**。判定 P2P 成功则要同时看到 host IMC payload 流量接近零，并确认没有静默回退到 host bounce buffer。

## 11. 最终选型建议

1. **纯 KVCache 落盘或读盘转发、CPU 不看 payload**：优先 RNIC/DPU↔NVMe P2PDMA；不具备 P2P 时使用 SPDK host-memory zero-copy。不要为了“用上 cache”而把大 payload 全灌入 LLC。
2. **只做 CRC/标准解压/加密**：先验证 DPU/IAA/QAT/SmartSSD 是否能在实际数据路径 inline；若只能离线调用并仍需 DDR bounce，收益要按完整路径重新计算。
3. **CPU 必须执行自定义策略或格式变换**：使用 L3 micro-chunk pipeline，融合 CRC 与解压扫描；输入/输出 ring 与消费者绑在同 NUMA/CCD，严格控制 working set。
4. **包头、CQE、doorbell、索引命中通知**：优先 L2/定向 cache injection；payload 留 DDR、DPU 或 P2P path。
5. **建立可降级架构**：P2P 不可达→L3 pipeline；stash hit 下降→DDR/SPDK；硬件 transform 不支持算法→CPU/GPU fallback。每条 fallback 都应有独立性能 SLO 和可观测计数器。
6. **在采购前做 feature proof，而不是只看芯片框图**：要求厂商展示相同 firmware/driver/BIOS 组合下的 P2P topology、实际算法、线速、host IMC 计数器、数据保护覆盖范围和异常恢复流程。

## 12. 仍需通过目标平台实测回答的问题

- 目标 CPU SKU 的 DDIO/SDCI cache budget、替换策略和 non-allocating 控制粒度是多少？
- 目标 RNIC/NVMe 是否都支持所需的 TPH、相干 read、CMB/PMR 和 P2PDMA mapping？
- IOMMU 开启、SR-IOV/VM 隔离与 ACS 安全策略下，P2P 是否仍被允许？
- CRC 保护范围是压缩输入、解压输出还是端到端对象；是否需要 T10 DIF/DIX？
- 目标 DPU 的 compress/decompress 是否支持实际 codec、字典模式、block size 和错误恢复，并能否与 NVMe path 真正 inline？
- 最优 chunk 大小和 queue depth 是多少；在 SSD p99 抖动和 CPU LLC 背景压力下，stash hit 能否保持？
- buffer 生命周期能否安全避免无意义 dirty writeback，还是必须按本文保守的 1×/3×生命周期流量规划？

只有这些问题在真实平台上有 PMU/trace/故障注入证据后，才能把本文的“理想路径”转化为容量规划和性能承诺。
