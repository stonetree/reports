# Cache Stashing 与 DPU-SSD 直通技术总结

> 基于本目录根目录下的 5 篇讨论稿整理，并结合截至 **2026-07-31** 可公开核验的芯片、协议、驱动和产品资料复核。
>
> 本文只读取了以下 5 个根目录文件；其它目录中的文档未纳入分析：
>
> - `IO stash技术分析.md`
> - `IO.md`
> - `stash crc on off ddr write and read.md`
> - `RDMA 机制讨论.md`
> - `RDMA 机制讨论 补充稿.md`

## 1. 技术摘要：Cache Stashing 解决的是“即将被消费的数据离 CPU 有多远”

Cache Stashing 的本质不是把大容量数据永久存进缓存，而是让 I/O 设备把**短生命周期、即将被某个 CPU 核心消费的缓存行**直接放入缓存层级，减少“设备写 DDR → CPU 再从 DDR 读回”的往返。

对 KVCache 传输，结论可以先概括为：

1. **CPU 会马上读取 Payload 做 CRC、解压、量化、拼接或协议处理时，Stashing 可能有效。**理想的热缓存窗口内，DDR 读可以从 `P` 降到 `0`；但最终仍可能发生脏缓存写回，因此不能简单宣称“DDR 访问为 0”。
2. **CPU 不读取 Payload、只是把数据搬到 SSD 或另一个设备时，优先使用 PCIe P2P / DPU 内部数据通路。**单纯把大块 Payload Stash 到 L3 只会污染缓存，数据仍要被逐出到 DDR。
3. **Stashing 到 L3/LLC 与 Stashing 到指定 L2 不是同一个能力。**Intel DDIO 主要是设备与 LLC 的直接访问；AMD SDCI 使用 PCIe TPH/Steering Tag 把 I/O 数据定向到目标缓存域；Arm AMBA CHI 定义协议级 Stash 事务，具体缓存级别由 SoC 实现决定。
4. **SSD↔RNIC 直通能否成立，首先是拓扑和协议问题，其次才是软件问题。**同一 PCIe Switch 下通常最容易获得稳定 P2P；同一 Root Complex 下也可能支持，但必须逐平台确认 ACS、IOMMU、P2P 路由和驱动能力。
5. **对于 KVCache，最稳妥的优先级通常是：DPU/RNIC 内部 CRC/解压/加密卸载 → SSD/RNIC 或 SSD/GPU P2P → 小窗口 L3/L2 Stashing → DDR bounce buffer。**

## 2. Cache Stashing 的发展过程：从“设备直接访问 LLC”到“按消费者定向注入”

### 2.1 第一代：传统 DMA，设备把数据直接写入 DDR

传统网卡、存储控制器或 RDMA 网卡收到数据后，作为 PCIe Bus Master 发起 DMA Write：

```text
I/O 设备 / RNIC  ── PCIe Memory Write ──>  Root Complex / IOMMU  ──>  DDR
                                                │
                                                └── CPU 后续 Load，再进入 L1/L2/L3
```

对一个会被 CPU 读取的 Payload，典型数据流是：

```text
设备写 DDR 1 次 + CPU 从 DDR 读 1 次
```

如果之后还要由另一个 DMA 设备从 DDR 读出，例如 NVMe 控制器将数据写入 SSD，则又增加一次 DDR 读。DDR 带宽放大来自**中转**，而不是来自网络协议本身。

### 2.2 第二代：Intel DDIO，设备直接访问处理器 LLC

Intel 在 Xeon E5/E7 v2 家族引入 Data Direct I/O（DDIO），时间点约为 **2012 年**。DDIO 让 I/O 设备的 DMA 读写可以直接面向处理器的最后一级缓存（LLC），而不是每次都先终止于 DRAM。Intel 的公开资料明确说明，DDIO既支持 inbound write，也支持 outbound read；因此，在数据仍然留在 LLC 且平台支持一致性探测时，CPU 和另一 I/O 设备都可能直接命中缓存。

DDIO 不是一块独立的“网卡缓存”，也不是永久保留区：

- I/O 数据和 CPU 数据竞争同一缓存层级；
- 设备写入的数据会受到缓存容量、替换策略和平台配置限制；
- 数据被逐出时，脏缓存行可能写回 DDR；
- 纯 Payload 流量如果远大于缓存可用窗口，会形成 cache thrashing。

### 2.3 第三代：PCIe TPH，将“放到哪个缓存位置”标准化为提示

PCI-SIG 在 **2008 年**发布 TLP Processing Hints（TPH）相关 ECN。TPH 允许 PCIe Requester 在 TLP 中携带 Processing Hint 和 Steering Tag，让 Root Complex / 片上互联根据平台映射，把请求导向更合适的处理资源或缓存位置。

TPH 本身只是协议能力，不等于某个平台一定能把数据送入 L2。完整链路还需要：

```text
PCIe Endpoint 支持 TPH
  → 固件/ACPI 提供 Steering Tag
  → CPU/Root Complex/Uncore 支持解释 Tag
  → OS 打开 CONFIG_PCIE_TPH
  → 设备驱动为队列配置 Tag
  → 应用把接收队列绑到实际消费核
```

Linux 内核的 TPH 文档明确指出：内核负责发现 TPH，但是否启用以及如何配置 Steering Tag，仍由设备驱动决定。

### 2.4 第四代：AMD SDCI，把 I/O DMA 定向到目标 CPU 缓存域

AMD 的 Smart Data Cache Injection（SDCI）公开白皮书发布于 **2025 年 2 月**。SDCI 使用基于 PCIe TPH 的标准化方式，使端点设备可以把 inbound DMA write 定向到目标 CPU 的 L2 cache；AMD 同时提供 SDCI Allocation Enforcement（SDCIAE）来限制 I/O 可使用的缓存资源。

AMD 公开资料还显示，Solarflare Onload / sfc 驱动已经加入 SDCI 支持。效果依赖于：接收线程是否真正读取数据、线程是否位于对应 CCD、以及数据是否在被读取前已被逐出。

这里应避免把 SDCI 表述为“所有 EPYC 9004/9005 SKU 都默认支持的统一特性”。更稳妥的说法是：**近期 AMD EPYC 服务器平台提供 SDCI 能力，具体 SKU、BIOS、NIC 和驱动组合需要逐机验证。**

### 2.5 第五代：Arm AMBA CHI，把 Stash 定义为一致性互联事务

Arm 在 **2016 年**公开介绍 AMBA 5 CHI 的 Cache Stashing 能力。CHI 支持带数据的 Stash 事务和不同的 Stash 语义，允许系统设计者把即将被处理器消费的数据放到靠近消费点的位置。

但 CHI 是 SoC 互联协议，不是一个单独的服务器产品。Arm 文档也说明 Stash 目标由实现决定；在具体系统中可能是某个逻辑处理器对应的 L2，也可能是集群级缓存或其它一致性节点。因此，不能仅凭“某芯片采用 Arm Neoverse”就断言它已经实现了 PCIe SSD/RNIC 到指定 L2 的完整端到端路径。

## 3. Cache Stashing 的两个方向和完整处理流程

### 3.1 RNIC / DPU → SSD：网络入站后校验、变换、落盘

#### A. 传统 DDR bounce buffer 路径

```text
远端 GPU / CPU
   │  RDMA Write / RoCEv2 / InfiniBand
   ▼
RNIC / DPU 接收、解析、ICRC/链路校验
   │
   ├── DMA Write Payload → Host DDR
   │                          │
   │                          ├── CPU Load：CRC / 解压 / 量化 / 拼接
   │                          │       └── 结果写回 DDR
   │                          │
   │                          └── NVMe DMA Read → SSD Controller → NAND
   └── CQE / MSI-X / doorbell 通知完成
```

这条路径的优点是兼容性高、数据所有权清晰；缺点是大 Payload 至少经过一次 DDR 写和一次或多次 DDR 读。

#### B. L3/LLC Stashing 路径

```text
RNIC / DPU
   │  DMA Write，进入 LLC（平台支持 DDIO/一致性 I/O）
   ▼
LLC / L3 Dirty Cache Lines
   ├── CPU 核心读取：CRC / 解压 / 量化
   ├── NVMe 控制器发起 DMA Read：若平台支持 LLC snoop，则可直接取走
   └── 缓存行逐出或显式回收：可能写回 DDR
```

这条路径只有在以下窗口同时满足时才有意义：

1. 数据量被切成足够小的流式 chunk；
2. CPU 或下游 I/O 设备在 cache line 被逐出前立即消费；
3. PCIe Root Complex 和 I/O 一致性路径支持所需的 cache snoop / outbound read 行为；
4. 数据的生产者和消费者位于合适的 NUMA / CCD / LLC 域。

特别要注意：**“RNIC 写入 LLC”不自动等价于“SSD DMA 一定从 LLC 读到数据”。**Intel DDIO 的公开资料支持 LLC 方向上的 inbound/outbound 访问，但其它平台可能只公开支持设备写入缓存、CPU 读取命中，或者由实现决定是否允许外设读取脏 LLC。因此设计时必须做硬件实测和性能计数器验证。

#### C. DPU 内部处理 + SSD P2P 路径

```text
RNIC / DPU
   ├── 网卡内 CRC / checksum / 解压 / 加密 / 重排
   └── PCIe P2P DMA Write → NVMe SSD Controller → NAND
```

这是 KVCache 只需要校验、解压并落盘时更接近“物理最短路径”的方案。CPU 只负责配置队列、内存注册、权限和完成通知；数据不必进入 Host DDR 或 CPU L3。

### 3.2 SSD → RNIC / DPU：读出 PrefixCache 并发送到远端

#### A. SSD/RNIC P2P 直通

```text
NVMe SSD Controller
   │  PCIe DMA Read / P2P transaction
   ▼
PCIe Switch 或可验证的 Root Complex P2P 路径
   ▼
RNIC / DPU DMA buffer
   │  RDMA packetization / RoCEv2 / InfiniBand
   ▼
远端 RNIC → 远端 Host DDR / GPU HBM / SSD
```

这条路径不需要把大块 Payload 放进 CPU Cache。BlueField-3 文档公开列出了“DPU 作为 PCIe switch、NVMe SSD 作为 PCIe endpoint”的连接场景；SPDK 也公开支持 PCIe peer-to-peer DMA 的使用模型。

#### B. 没有 P2P 时的 DDR bounce buffer

```text
SSD → DDR（DMA Write）→ RNIC（DMA Read）→ 网络
```

每传输 `P` 字节，至少产生 `P` 字节 DDR 写和 `P` 字节 DDR 读，即 `2P` 的 DDR 流量。

#### C. 没有 P2P 时的 L3 bounce buffer

理论上可以是：

```text
SSD DMA Write → LLC Stash → RNIC DMA Read / snoop → 网络
```

但这不是通用替代方案，必须同时满足“SSD 能写入目标 LLC”“RNIC 的 DMA Read 能探测并取走脏 LLC”“缓存窗口不溢出”三个条件。若任何一个条件不成立，数据最终仍要写回 DDR，甚至会因为缓存污染而比 DDR bounce 更差。

## 4. KVCache 落盘的定量模型：CRC、解压和 Stashing 的 DDR 放大

### 4.1 示例输入和符号

以讨论稿中的 Llama-3-70B GQA 示例为基础：

```text
层数 Nlayer       = 80
KV 头数 Hkv       = 8
每头维度 Dhead    = 128
元素大小           = FP16 = 2 B
批量               = 16
上下文长度         = 4096 token
```

每 token 的 KVCache 大小为：

```text
S_token = 2(K/V) × 80 × 8 × 128 × 2 B
        = 327,680 B
        = 320 KiB
```

总原始 KVCache：

```text
V_raw = 16 × 4096 × 327,680 B
      = 20 GiB（约 21.47 GB）
```

为了把“开启解压缩”纳入同一张表，设网络传输的是压缩后的 `P_c = 5 GiB`，解压后落盘数据为 `P_u = 20 GiB`，即展开比：

```text
R = P_u / P_c = 4
```

表中“开启 CRC”指**Host/DPU 需要对 Payload 做的业务级 CRC 校验**，不等同于 RoCE ICRC 或 PCIe LCRC。若 CRC 已由 RNIC/DPU 硬件完成，需采用“CRC Offload”行，而不是把 CPU CRC 读流量算进去。

### 4.2 计数规则和假设

为便于审计，先采用一个保守且可复现的基线：

- RNIC 入站 Payload 先写入 DDR 或 LLC；
- CPU CRC 是一次完整读；
- CPU 解压是一次完整读 `P_c`，并向输出 buffer 写 `P_u`；
- NVMe 控制器从输出 buffer DMA Read `P_u` 写入 SSD；
- CRC 与解压默认是两次独立的 CPU pass；如果工程实现能融合为一次 pass，另列一行；
- “热 L3”假设输入和输出都在消费者访问前留在 LLC，且 NVMe DMA Read 能对脏 LLC 做一致性探测；
- 热 L3 下仍计入最终的脏缓存写回：输入 `P_c` 和输出 `P_u` 都可能各写回一次；所以不是简单的 `0 DDR`；
- “冷/溢出”表示数据在消费前已逐出，结果退化到 DDR bounce；
- 不计页表、描述符、CQE、元数据和 SSD FTL 内部写放大；这里只计算 Host DDR 数据面流量。

### 4.3 四种业务场景的详细 DDR 计算

下表以 `P_c = 5 GiB`、`P_u = 20 GiB` 为例；“放大倍数”统一以压缩入站 Payload `P_c` 为分母。

| 场景 | 路径 | DDR 写入构成 | DDR 读取构成 | DDR 总流量 | 相对 `P_c` 放大 | 说明 |
|---|---|---:|---:|---:|---:|---|
| 不开 CRC、不开解压 | 无 Stash | RNIC→DDR：5 GiB | SSD 从 DDR 读：5 GiB | **10 GiB** | **2.0×** | 典型 SSD 落盘 bounce buffer |
| 不开 CRC、不开解压 | 热 L3 Stash | 输入脏行最终写回：5 GiB | CPU无读；SSD LLC snoop：0 | **5 GiB** | **1.0×** | 只有在 SSD DMA 能读脏 LLC 且窗口不溢出时成立 |
| 不开 CRC、不开解压 | 冷/溢出 Stash | 逐出写回：5 GiB | SSD 从 DDR 读：5 GiB | **10 GiB** | **2.0×** | Stash 退化，另有缓存污染风险 |
| 开 CRC、不开解压 | 无 Stash | RNIC→DDR：5 GiB | CPU CRC：5 GiB；SSD：5 GiB | **15 GiB** | **3.0×** | 讨论稿中“1写+2读=3P”的对应场景 |
| 开 CRC、不开解压 | 热 L3 Stash | 输入脏行最终写回：5 GiB | CRC命中 LLC；SSD LLC snoop：0 | **5 GiB** | **1.0×** | 理想热窗口，DDR 读减少 100% |
| 开 CRC、不开解压 | 冷/溢出 Stash | 逐出写回：5 GiB | CPU CRC：5 GiB；SSD：5 GiB | **15 GiB** | **3.0×** | 退化到无 Stash，并承受 cache pollution |
| 不开 CRC、开解压 | 无 Stash | RNIC→DDR：5 GiB；解压输出：20 GiB | CPU读压缩输入：5 GiB；SSD读输出：20 GiB | **50 GiB** | **10.0×** | `5 + 20 + 5 + 20 = 50 GiB` |
| 不开 CRC、开解压 | 热 L3 Stash | 输入写回：5 GiB；输出写回：20 GiB | CPU读输入命中；SSD读输出命中：0 | **25 GiB** | **5.0×** | 仍有两个脏数据集的最终写回 |
| 不开 CRC、开解压 | 冷/溢出 Stash | RNIC写回：5 GiB；解压输出：20 GiB | CPU读：5 GiB；SSD读：20 GiB | **50 GiB** | **10.0×** | Stash 失去收益 |
| 开 CRC、开解压（独立两遍） | 无 Stash | RNIC→DDR：5 GiB；解压输出：20 GiB | CRC读：5 GiB；解压读：5 GiB；SSD读：20 GiB | **55 GiB** | **11.0×** | `5 + 20 + 5 + 5 + 20 = 55 GiB` |
| 开 CRC、开解压（独立两遍） | 热 L3 Stash | 输入写回：5 GiB；输出写回：20 GiB | CRC/解压/SSD 均命中 LLC：0 | **25 GiB** | **5.0×** | 最理想的 LLC bounce buffer |
| 开 CRC、开解压（独立两遍） | 冷/溢出 Stash | RNIC写回：5 GiB；解压输出：20 GiB | CRC读：5 GiB；解压读：5 GiB；SSD读：20 GiB | **55 GiB** | **11.0×** | 退化并污染 LLC |
| 开 CRC、开解压（融合一遍） | 无 Stash | RNIC→DDR：5 GiB；解压输出：20 GiB | 融合读：5 GiB；SSD读：20 GiB | **50 GiB** | **10.0×** | CRC 与解压共用一次输入扫描 |
| 开 CRC、开解压（融合一遍） | 热 L3 Stash | 输入写回：5 GiB；输出写回：20 GiB | 融合读/SSD读命中 LLC：0 | **25 GiB** | **5.0×** | 仍不能把最终脏写回自动消除 |

### 4.4 P2P 与 DPU Offload 的极限行

如果 CRC 和解压都在 DPU/RNIC 内部硬件引擎完成，且输出通过 PCIe P2P 直接写 NVMe SSD：

| 路径 | RNIC/DPU 到 SSD 的数据面 | Host DDR 数据面 | DDR 放大倍数 |
|---|---|---:|---:|
| DPU CRC Offload + DPU 解压 Offload + SSD P2P | DPU SRAM/packet buffer → NVMe controller → NAND | **0 GiB** | **0×** |
| DPU CRC Offload + SSD P2P，但解压在 CPU | 压缩输入仍需进入 CPU/内存处理 | 至少 `P_c + P_u`，具体取决于解压输入/输出缓冲区 | 通常 ≥5× |
| CPU CRC/解压 + L3 Stash | LLC 作为短窗口中转 | 25 GiB（热、可 snoop、R=4） | 5× |
| CPU CRC/解压 + DDR bounce | DDR 作为中转 | 55 GiB（CRC与解压两遍、R=4） | 11× |

因此，**L3 Stashing 的价值主要是减少 CPU 消费阶段的 DDR 读，而不是替代 DPU→SSD P2P。**若目标是“数据不进 Host DDR”，必须把 CRC/解压/重排也放到 DPU 或 SSD 侧，或者采用设备间 P2P。

### 4.5 Chunking 的工程约束

设实际可供 I/O 使用的 LLC 窗口为 `C_io`，同时存在 `k` 个并行流和安全系数 `α`，则单个 pipeline chunk 应满足：

```text
ChunkSize ≤ α × C_io / k
```

其中 `α` 不宜取 1，通常要为 CPU 热数据、描述符、预取和其它队列留出空间。讨论稿中使用 16–32 MiB 作为示例窗口可以作为起点，但**不是跨平台固定值**；Intel DDIO 配额、AMD SDCI allocation、Arm SoC 的缓存结构都可能不同。

## 5. Cache Stashing 的完整软硬件依赖

### 5.1 硬件依赖

| 层次 | 必需能力 | 作用 |
|---|---|---|
| CPU/Uncore | DDIO、SDCI 或 CHI Stash 等缓存注入能力 | 把 I/O 请求导向 LLC/L2/一致性节点 |
| PCIe Root Complex | 解释 TPH、处理 No-Snoop/一致性属性、正确路由 DMA | 将设备 TLP 映射到目标缓存或内存 |
| PCIe Endpoint | TPH Requester、Steering Tag 表或平台特定能力 | 让 NIC/NVMe 能声明目标缓存位置 |
| I/O 一致性 | outbound DMA read 能探测脏 LLC，或等价的写回/转发机制 | SSD/RNIC 作为第二消费者时尤其关键 |
| IOMMU | IOVA→PA 映射、权限隔离、DMA fault | 保护内存并支持虚拟化/容器 |
| NUMA/CCD 拓扑 | I/O 设备、消费核、目标内存尽量同域 | 减少跨 socket/CCD 一致性流量 |
| 设备 DMA 引擎 | 大包 DMA、scatter-gather、队列并行、完成队列 | 承载实际数据面 |
| 缓存资源控制 | Intel CAT/RDT、AMD SDCIAE 或 SoC 等价机制 | 限制 I/O 污染，保护 CPU 热数据 |

### 5.2 固件和平台依赖

- BIOS/UEFI 枚举 PCIe TPH、启用或暴露相应能力；
- ACPI `_DSM` 或等价固件表提供 CPU/内存到 Steering Tag 的映射；
- 配置 NUMA、I/O locality、MSI-X 中断亲和性；
- 对 P2P 方案，必须检查 ACS、ARI、ATS、PASID、IOMMU 和 PCIe Switch 的实际路径；
- 对安全环境，不能为了 P2P 或低延迟而无条件关闭 IOMMU/ACS，应按设备可信域配置。

### 5.3 OS、驱动和运行时依赖

| 软件层 | 依赖 |
|---|---|
| Linux 内核 | `CONFIG_PCIE_TPH`、DMA/IOMMU、PCI P2PDMA、resctrl/io_alloc（平台支持时） |
| NIC/RNIC 驱动 | TPH/Steering Tag 配置、RX queue→CPU/CCD 绑定、CQE 处理、RDMA 注册 |
| NVMe/SPDK | 用户态 NVMe 队列、P2P DMA、NVMe CMB/PMR（若设备支持）、NVMe-oF target/initiator |
| RDMA 软件栈 | rdma-core、libibverbs、OFED/DOCA 或厂商等价栈，MR/rkey/lkey、QP、CQ |
| DPU 软件 | Arm/Linux 控制面、DPU firmware、RoCE/RDMA offload、压缩/CRC/加密引擎 API |
| 应用运行时 | pinned memory、cache-line 对齐、流式 chunk、生产者/消费者状态机、内存屏障、完成通知 |
| 观测工具 | uncore/LLC/IIO/IMC PMU、PCIe AER、DMA fault、P2P bandwidth/latency、CQE drop 统计 |

### 5.4 正确性依赖

Cache Stashing 不改变生产者/消费者协议。仍需要：

1. 数据 buffer 和 descriptor 的所有权转移；
2. `payload → barrier → ready flag/CQE` 的发布顺序；
3. PCIe Posted Write、CPU store buffer、设备 DMA 完成之间的可见性验证；
4. 失败重试、CRC 错误、解压错误、SSD 写失败和 RDMA 重传处理；
5. 页面不能被 OS 移动或回收，必须正确 pin/register；
6. 对“消费后不需要持久化”的缓存行，使用经过平台验证的回收/失效策略，不能假设 dirty line 可以无条件丢弃。

## 6. 主流厂商的 Cache Stashing 软硬件布局

下表把“公开标准/产品能力”“厂商软件支持”和“尚未被公开资料证明的推断”分开。年份是公开发布或可核验的时间点，不一定等于首次芯片流片时间。

| 厂商/生态 | 技术与硬件位置 | 软件布局 | 公开年份 | 适用范围与证据边界 |
|---|---|---|---:|---|
| Intel | **DDIO**：I/O 设备与 Xeon LLC 直接访问；新平台还提供对 inbound I/O write 的 non-allocating/TPH 控制路径 | BIOS/平台配置、VTune/PerfMon 观测；通常对应用透明，驱动主要负责队列、NUMA 和访问模式 | **2012**（Xeon E5/E7 v2 家族） | Intel 官方资料明确说明 DDIO 面向 LLC，并支持 inbound write/outbound read；不是指定某个 CPU L2 的通用 API |
| PCI-SIG | **TPH**：在 PCIe TLP 中携带 Processing Hint/Steering Tag | Linux `CONFIG_PCIE_TPH`、设备驱动配置 ST 表 | **2008** | 协议标准，不保证任何 CPU 平台具备实际 cache injection |
| Arm | **AMBA 5 CHI Cache Stashing**：一致性互联中的 Stash 事务 | SoC 设计、CHI/DSU/Home Node 配置；由芯片厂商决定缓存目标和实现语义 | **2016**（公开介绍 CHI 增强能力） | 是 SoC/IP 协议能力；不能据此直接断言某个 Arm 服务器产品已实现 PCIe→指定 L2 的完整路径 |
| AMD | **SDCI**：基于 PCIe TPH，将适用的 inbound DMA write 导向目标 CPU 缓存域，公开白皮书描述到 L2；SDCIAE 可做 I/O cache allocation enforcement | Linux TPH API、resctrl/io_alloc、Solarflare Onload/sfc、CPU/CCD 亲和性配置 | **2025**（SDCI White Paper）；**2026**（Onload 文档） | AMD 官方资料明确给出 SDCI 和 Solarflare 软件支持；具体 EPYC SKU/NIC/BIOS 需逐机验证 |
| NVIDIA | BlueField/ConnectX 侧主要布局在 RDMA、P2P、DPU offload、GPUDirect；不是公开的通用 CPU L2 cache-stash 产品线 | DOCA、MLNX_OFED、GPUDirect RDMA/GDS、DPU firmware | **2020**（GDS公开介绍）；持续演进 | NVIDIA 公开资料支持“存储/NIC DMA 直接到 GPU memory”和 DPU P2P；不应把它等同于 CPU L3/L2 Stashing |
| AWS/其它 Arm SoC 厂商 | 可能基于 CHI/片上互联实现特定 Stash，但公开产品资料通常不披露完整的 PCIe endpoint→指定 L2 软件栈 | 取决于 SoC、固件、NIC/DPU 和操作系统 | 随产品实现 | 应标为“架构可选能力”，除非厂商公开给出产品级 end-to-end 证据 |

## 7. DPU 与 SSD 之间的直通方案：软硬件布局对比

这里的“直通”严格指**数据面不经过 Host CPU DDR**。它与“DPU 作为 NVMe-oF target/virtual storage controller”不同：后者可能仍在 DPU 本地内存或远端网络路径上经过一次软件/硬件缓冲。

| 厂商/方案 | 硬件布局 | 软件布局 | 公开年份 | 是否属于 DPU↔SSD 直通 | 适合 KVCache 的位置 |
|---|---|---|---:|---|---|
| 通用 PCIe P2P DMA | NVMe SSD 与 RNIC/DPU 同一 PCIe Switch，或同一 RC 且平台允许 endpoint-to-endpoint 路由 | Linux PCI P2PDMA、设备驱动、IOMMU/ACS/拓扑配置 | **2017 起进入 Linux P2P DMA 生态**；PCIe 本身更早 | **是，条件式** | 最直接的 SSD→DPU/RNIC 和 DPU→SSD 数据面 |
| SPDK P2P + NVMe CMB | SSD controller memory / CMB 与另一个 PCIe endpoint 之间直接 DMA；SPDK 文档以 NVMe controller 间复制为例 | SPDK userspace NVMe driver、DPDK hugepage、P2P DMA API | **2016**（SPDK 16.12 的 NVMe-oF initiator）；P2P 能力持续演进 | **是，取决于硬件与拓扑** | 做高吞吐、低 CPU 占用的本地 NVMe pipeline |
| NVIDIA BlueField-2/3 + NVMe | DPU 集成 PCIe switch/多路 PCIe 接口；BlueField-3 文档公开 NVMe SSD 作为 PCIe endpoint 的连接场景 | BlueField OS、DOCA、MLNX_OFED、SPDK、NVMe-oF target、SNAP、NVMe emulation | **2022–2023**（BF2/BF3 公开文档）；持续更新 | **可实现，取决于具体板卡和连接方式** | DPU 侧接盘、存储虚拟化、NVMe-oF target、SSD 直连数据面 |
| NVIDIA GPUDirect Storage | NVMe/NIC/RAID controller 的 DMA engine 直接访问 GPU memory；通常通过 PCIe Switch/同一 IO root complex | cuFile、`nvidia-fs`、O_DIRECT、NVIDIA GPU driver/OFED/DOCA | **2020**公开介绍；GDS 1.0 约 **2021** | **不是 DPU↔SSD；是 SSD/NIC↔GPU** | 若 KVCache 最终去 GPU HBM，通常优先于 CPU L3 bounce |
| Intel IPU E2100 | IPU SoC、200GbE、NVMe offload engine、虚拟存储/远端存储连接 | IPDK、DPDK、SPDK、NVMe transport offload | **2023**（E2100 product brief） | **是“存储卸载/虚拟化”能力；是否为同机 SSD P2P 需看板卡拓扑** | 远端 KVCache 存储访问、虚拟 NVMe、降低 host CPU 存储开销 |
| NVIDIA BlueField SNAP | DPU 将网络存储抽象成 host 可见的 NVMe 设备；数据处理可在 DPU 框架中执行 | SNAP、DOCA、NVMe-oF、PCIe NVMe emulation | **2020s**；2025 文档仍在维护 | **更准确地说是 DPU storage virtualization/offload，不必然是 DPU↔本地 SSD 的物理 P2P** | 对 host 透明暴露远端 PrefixCache / KVCache 块设备 |
| NVMe-oF RDMA | SSD 在远端 storage target，RNIC/DPU 处理 RDMA transport；数据可在 target 侧直接进入 NVMe 队列 | NVMe-oF RDMA、SPDK target、DOCA target、Linux NVMe RDMA | **2016**（NVMe-oF 1.0） | **是网络直通语义，但不是本机 SSD 与 DPU 的 PCIe P2P** | 适合 PrefixCache 分离存储和跨节点按块读写 |

工程上要把上表分成两类：

- **物理 P2P 类**：SSD 与 DPU/RNIC 同一 PCIe fabric，数据在 endpoint 间转发；
- **协议/存储卸载类**：DPU 终止 NVMe-oF、虚拟化 NVMe 或管理存储命令，数据可能经过 DPU 本地内存或远端 target，但不一定穿过 host DDR。

## 8. DPU-SSD 直通与 L2/L3 Stashing 在 KVCache 场景的对比

| 维度 | DPU/SSD PCIe P2P 直通 | L3/LLC Stashing | 指定 L2 Stashing |
|---|---|---|---|
| 数据是否进入 Host DDR | 通常不进入 | 理想热窗口不读 DDR，最终可能写回 | 同左，但容量更小、目标更局部 |
| 是否污染 CPU cache | 几乎不污染 | 可能污染 LLC；大 Payload 风险高 | 主要污染目标核/CCD 的 L2，误配会直接影响消费核 |
| 是否需要 CPU 消费 Payload | 不需要，适合 DPU/SSD 直接处理 | 通常需要 CPU 或另一个一致性 I/O 设备及时消费 | 最适合固定 CPU 核轮询、头部/描述符处理 |
| 对 CRC/解压的处理 | 可在 DPU/RNIC/SSD 侧硬件完成 | CPU 处理时可减少 DDR 读 | CPU 处理时延更低，但窗口更小 |
| 可扩展性 | 受 PCIe lane、switch、DMA engine 和 SSD 数量限制 | 受 LLC 容量和 DDIO/SDCI 配额限制 | 受每核 L2 容量和目标核负载限制 |
| 大块 KVCache | 优先方案 | 只有流式 chunk、消费紧邻时才适合 | 不适合直接灌入 GB 级 Payload |
| 小包/元数据/ready flag | 不一定有优势，MMIO/doorbell 足够 | 有利于快速被 CPU 消费 | 很有价值，尤其是 RX descriptor、CQE、block map |
| 软件复杂度 | 高：拓扑、P2P、IOMMU、设备驱动 | 中高：TPH/DDIO、cache policy、chunking、PMU | 高：Steering Tag、CPU affinity、驱动和平台协同 |
| 可靠性风险 | P2P 不可达、ACS/IOMMU 拒绝、设备兼容性 | eviction、dirty line、snoop/ordering 误判 | Tag/CCD 映射错误、缓存局部性不稳定 |
| 总体建议 | KVCache 数据面首选 | CPU 立即处理且 P2P 不可用时采用 | 主要承载 header、descriptor、CQE 和小控制块 |

### 8.1 典型决策

- **RNIC/DPU→SSD，CRC/解压也在 DPU 完成**：DPU→SSD P2P；不要把大 Payload Stash 到 CPU L3。
- **RNIC/DPU→SSD，CRC/解压必须由 CPU 完成**：用小窗口 L3/LLC Stash；chunk 以“从设备写入到 CPU 读完再到 SSD 读走”的闭环为边界。
- **SSD→RNIC，纯 PrefixCache 转发**：SSD/RNIC P2P 或 NVMe-oF RDMA；L3 bounce 只能作为平台受限时的 fallback。
- **SSD→CPU，CPU 做 metadata/block-table 查找**：大 Payload 走 DDR 或 GPU/DPU，只有 metadata 和 CQE 走 L2/L3 Stash。

## 9. 除直通和 Stashing 外，值得考虑的 KVCache 加速方案

1. **DPU/RNIC 硬件 CRC、压缩、解压、加密和校验卸载**：把重复、流式、规则固定的操作放到 DPU ASIC/FPGA/专用 engine；这是消除 Host DDR 访问最有效的方向。
2. **GPUDirect RDMA / GPUDirect Storage**：如果 KVCache 的最终消费者是 GPU，直接让 NIC 或 NVMe DMA 到 GPU memory，绕过 Host DDR；NVIDIA GDS 要求 O_DIRECT 和可达 GPU buffer 的 DMA engine。
3. **NVMe-oF RDMA + SPDK**：将 PrefixCache 作为远端 NVMe namespace，使用 RDMA transport 和用户态 NVMe queue，减少内核 block stack 和 CPU copy。
4. **DPU storage virtualization**：通过 BlueField SNAP、DOCA NVMe emulation、Intel IPU storage offload，让 host 看到标准 NVMe 设备，但由 DPU 终止协议、实施隔离和数据面卸载。
5. **NVMe CMB/PMR 与设备内 SRAM**：将短期队列、descriptor、metadata 或小块数据放到 SSD controller memory；不适合把整个 KVCache 当作稳定大容量缓存。
6. **分层 KVCache**：GPU HBM 放热 KV，CPU DDR/CXL 放温 KV，SSD/NVMe-oF 放冷 KV；按前缀命中率和 token 访问频率分层，而不是所有数据都追求 L3。
7. **量化、压缩和布局优化**：KVCache 使用 FP8/INT8、按 head/group 压缩、按 block 对齐，降低网络和 SSD 写入量；这类优化通常比缓存微架构优化更稳定。
8. **批量化和流水线**：把 RDMA WQE、NVMe SQE、CRC/decompress descriptor 和 completion 合并为大 batch，减少 doorbell、PCIe TLP、CQE 和中断次数。
9. **异步 IO 与轮询**：DPDK/SPDK、io_uring、busy-poll 和 completion batching，在尾延迟和 CPU 占用之间做可测量的折中。
10. **GPU 侧地址/映射优化**：减少 block-table 全量复制，只同步 delta；使用持久化 kernel、CUDA Graph、设备侧映射表或等价机制，避免每轮 decode 重复搬运元数据。
11. **CXL 内存扩展**：当主要矛盾是容量而不是端到端数据搬运时，可用 CXL.mem/CXL memory pooling 扩展容量；但 CXL 不是 SSD P2P，也不能替代 DPU offload。

## 10. 需要特别避免的误区

### 10.1 把“缓存访问延迟”当成“RNIC 端到端 DMA 延迟”

L2/L3 的 3–15 ns、DDR 的几十 ns，是存储层级内部的典型响应量级，不是 RNIC 发起 PCIe Read 到收到完整 Completion 的端到端时间。RNIC 端到端时间还包含 PCIe 链路、Root Complex/Uncore、IOMMU、缓存一致性探测、DMA engine 和 payload 分片开销。

### 10.2 把 Stash 当作无限容量的 SRAM bounce buffer

当输入速率大于消费者速率、或者 chunk 大于可用缓存窗口，数据会逐出。此时 Stashing 不仅不能降低 DDR 流量，还可能冲刷 CPU 热代码和 descriptor。

### 10.3 把“同一个 Root Complex”当作 P2P 可用的充分条件

P2P 能否工作必须结合 PCIe Switch/RC 型号、ACS routing、IOMMU、ATS/PASID、设备 BAR/peer memory 和驱动验证。工程上应以 P2P DMA 实测和 PCIe 拓扑检查为准，而不是只看 `lspci` 的层级关系。

### 10.4 把 RoCE ICRC、PCIe LCRC 和业务 CRC 混为一谈

- PCIe LCRC：链路层完整性；
- RoCE/InfiniBand ICRC：RDMA 传输包完整性；
- NVMe PI/T10 DIF：存储端到端保护；
- 业务 CRC：应用针对 KVCache 内容的校验。

如果前两类已经由硬件完成，不能再把它们当作 CPU Payload 扫描流量；若业务还要求第三类/第四类，则需单独计入。

## 11. 推荐落地路线和验证清单

### 11.1 推荐架构

```text
第一优先：DPU/RNIC 硬件 CRC/解压/加密
        ↓
第二优先：DPU↔SSD 或 NIC/SSD↔GPU 的 PCIe P2P / GDS
        ↓
第三优先：CPU 必须消费时，使用 L3/L2 Stash + 小窗口流水线
        ↓
兜底：DDR bounce buffer + SPDK/RDMA/批量化优化
```

### 11.2 最小验证矩阵

| 验证项 | 需要测什么 |
|---|---|
| 拓扑 | SSD、RNIC/DPU、GPU 是否同一 PCIe Switch/RC/NUMA；ACS 是否重定向 |
| 可达性 | P2P DMA 是否成功；是否出现 IOMMU fault、AER 或 peer memory error |
| Stash 命中 | IIO/LLC/IMC PMU：I/O LLC hit、DDR read/write、cache eviction |
| 热窗口 | chunk size、并发流数、生产者到消费者间隔、逐出比例 |
| 处理路径 | CPU CRC/解压是否被融合；DPU engine 是否真正消费 Payload |
| 端到端 | RDMA 吞吐、SSD 吞吐、KVCache 落盘延迟、恢复/重传、P99/P999 |
| 正确性 | CRC 错误注入、乱序、断电/重启、部分写、重复 block、cache line 可见性 |
| 隔离性 | 多租户/虚机下 IOMMU、ACS、SR-IOV、DPU 隔离是否仍成立 |

## 12. 最终判断

Cache Stashing 是一个**面向短生命周期、强时间局部性的缓存优化**；P2P 是一个**面向设备间大数据搬运的路径优化**。在 KVCache 系统中，两者不是互斥替代关系：

- 大 Payload 的物理搬运应尽量走 P2P/DPU/GPU direct path；
- CPU 必须立即处理的少量 Payload、header、descriptor、CQE 和 block map 才适合 L2/L3 Stashing；
- 如果业务必须让 CPU 对 GB 级 KVCache 做 CRC/解压，真正应该优先评估的是 DPU offload、压缩比例、chunk pipeline 和数据布局，而不是简单扩大 DDIO/L3 配额；
- 在没有 P2P 的平台上，热 L3 bounce 可以作为实验性优化，但必须用硬件 PMU 和端到端数据正确性验证证明它确实命中，不能只根据理论的“12 ns L3、80 ns DDR”做结论。

## 13. 进一步值得研究的问题

1. 目标平台的 DDIO/SDCI/CHI 具体支持的是“设备写入缓存”“CPU 读取命中”还是“另一个设备 DMA Read 也能 snoop 脏缓存”？
2. DPU 的 CRC/解压 engine 能否直接连接 NVMe SQ/数据 buffer，是否必须经过 DPU 本地 DDR？
3. KVCache 采用 FP8/INT8 后，网络、DDR、SSD 三者的瓶颈是否从内存带宽转移到 PCIe 或 NAND FTL？
4. 对 PrefixCache，按 prefix block 的命中概率、压缩比和重用距离，L3 cache window 是否仍然值得保留？
5. 是否可以用设备侧 block map、GPU 侧 persistent kernel 或 CUDA Graph，将控制面元数据更新从全量 H2D copy 降到 delta + flag？
6. 在虚拟化场景中，TPH Steering Tag、IOMMU、SR-IOV VF 和 DPU 多租户隔离能否同时保留？

## 14. 主要公开资料

以下链接用于核验文中“标准、产品能力和发布年份”类事实；讨论稿中的内部推导和本文的计算表以本文假设为准。

- [Intel Data Direct I/O Technology](https://www.intel.com/content/www/us/en/io/data-direct-i-o-technology.html)
- [Intel DDIO Performance Monitoring（含 inbound/outbound 和 non-allocating 讨论）](https://www.intel.com/content/www/us/en/developer/articles/technical/ddio-analysis-performance-monitoring.html)
- [Intel DDIO 支持的 Xeon 处理器列表](https://www.intel.com/content/www/us/en/support/articles/000087975/processors/intel-xeon-processors.html)
- [PCI-SIG TLP Processing Hints ECN](https://pcisig.com/PCIExpress/ECN/Base/TLPProcessingHints)
- [Linux PCIe TPH 支持文档](https://docs.kernel.org/6.14/PCI/tph.html)
- [AMD Smart Data Cache Injection White Paper（2025）](https://www.amd.com/content/dam/amd/en/documents/epyc-technical-docs/white-papers/58725.pdf)
- [AMD Onload SDCI 文档（2026）](https://docs.amd.com/r/en-US/ug1586-onload-user/SDCI)
- [AMD Onload TPH 模式](https://docs.amd.com/r/en-US/ug1586-onload-user/EF_TPH_MODE)
- [Arm AMBA CHI Cache Stashing 介绍](https://developer.arm.com/community/arm-community-blogs/b/soc-design-and-simulation-blog/posts/introducing-new-amba-5-chi-protocol-enhancements)
- [NVIDIA GPUDirect Storage Overview](https://docs.nvidia.com/gpudirect-storage/overview-guide/index.html)
- [NVIDIA GPUDirect Storage Design Guide](https://docs.nvidia.com/gpudirect-storage/design-guide/index.html)
- [NVIDIA GPUDirect Storage O_DIRECT Requirements](https://docs.nvidia.com/gpudirect-storage/o-direct-guide/)
- [NVIDIA BlueField-3 PCIe/NVMe 连接场景](https://docs.nvidia.com/nvidia-bluefield-3-networking-platform-user-guide.pdf)
- [NVIDIA BlueField Software Overview](https://docs.nvidia.com/networking/display/bluefieldbsp4100/bluefield%2Bsoftware%2Boverview)
- [NVIDIA BlueField SNAP](https://docs.nvidia.com/networking/display/bluefieldbsp454/snap%2Bon%2Bdpu)
- [NVIDIA DOCA NVMe Emulation + SPDK](https://docs.nvidia.com/doca/sdk/doca-nvme-emulation-application-guide/)
- [Intel IPU 产品页](https://www.intel.com/content/www/us/en/products/details/network-io/ipu.html)
- [Intel IPU E2100 产品简报](https://cdrdv2-public.intel.com/816692/Intel%20Infrastructure%20Processing%20Unit%20Adapter%20E2100-CCQDA2.pdf)
- [SPDK Peer-to-Peer DMA](https://spdk.io/doc/peer_2_peer.html)
- [SPDK 16.12：NVMe-oF initiator](https://spdk.io/release/2016/12/19/16.12_release/)
- [NVM Express：NVMe-oF 1.0 于 2016 年发布](https://nvmexpress.org/nvm-express-over-fabrics-specification-released/)

