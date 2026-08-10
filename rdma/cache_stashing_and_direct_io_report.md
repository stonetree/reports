# 缓存 Stashing 与 Direct I/O 技术深度解析与厂商布局报告 (v2 完整修订版)

> **摘要**：在现代高性能计算、大模型推理（LLM Inference）及超大规模数据中心中，“内存墙（Memory Wall）”与 CPU 流水线停顿（Pipeline Stall）已成为系统吞吐与时延的最核心瓶颈。为了突破这一物理限制，系统架构沿着**控制面与数据面解耦（Control/Data Plane Separation）**的方向演化出了两条核心技术路径：一条是面向控制面与事件通知的 **Cache Stashing（硬件定向缓存注入）** 技术；另一条是面向数据面大块存储落盘与换出的 **DPU 与 SSD 端到端直通（PCIe P2P DMA）** 技术。
>
> 本报告基于对底层总线协议（PCIe TPH、AMBA CHI）、CPU 存储微架构（Intel DDIO/Extended DDIO、AMD SDCI、Arm CHI Stash）及硬件加速器（NVIDIA BlueField、AMD Pensando、Intel IPU/QAT、AWS Nitro）的深度物理拆解，全面总结 Cache Stashing 的发展演进、双向微观流程（含直通与写回双路径）、KV-Cache 场景下的 DDR 访存放大倍数定量演算、全栈软硬件依赖（含 128B/64B 粒度不对齐拆包逻辑）、各大厂商布局矩阵、真实生产落地案例、Read-and-Invalidate 零写回演进机制，以及片外 PCIe TPH 标准化与片内 Fabric 私有化的分层解耦范式。

---

## 一、 Cache Stashing 技术的发展演进历程

```
+-----------------------------------------------------------------------------------+
|                            Cache Stashing 架构演进历程                             |
+-----------------------------------------------------------------------------------+
|                                                                                   |
|  [阶段 1: 传统 DMA 时代] (2010 年以前)                                            |
|   PCIe DMA ──► 物理 DRAM (60~100ns) ──► CPU Cache Miss ──► 触发总线读 (Pipeline Stall)
|                                                                                   |
|  [阶段 2: 共享 LLC 盲目 Stashing 时代] (2012 年, Intel DDIO 问世 - 私有黑盒)        |
|   PCIe DMA ──► 拦截写入共享 L3/LLC ──► 消除了 DRAM 写 ──► 限制: 仅限 LLC, 易跨 Slice|
|                                                                                   |
|  [阶段 3: 协议层标准化与 Steering Tag 时代] (2016~2020 年, PCIe TPH & ARM CHI)     |
|   PCIe TPH TLP 包含 Steering Tag / AMBA CHI 带 StashLPID ──► 定向路由至目标 CPU    |
|                                                                                   |
|  [阶段 4: 近核私有 L2 定向注入与路线归一时代] (2022~2024 年, AMD SDCI & Intel Ext-DDIO)
|   网卡 DMA ──(PCIe TPH Tag)──► 归一至 PCIe TPH 标准, 直接注入目标 Core 私有 L2 (10~15ns)
|                                                                                   |
+-----------------------------------------------------------------------------------+
```

### 1.1 传统 DMA 时代的“内存墙”与 Pipeline Stall
在传统的 DMA（Direct Memory Access）架构中，外设（网卡、NVMe 控制器）通过 PCIe 总线直接读写 Host DRAM（物理内存）。
* **物理瓶颈**：随着网络速率迈向 100Gbps/400Gbps，单核处理一个网络数据包或描述符的时间预算被压缩至 **10 ~ 20 ns**（在 3.0GHz 主频下仅相当于 30 ~ 60 个时钟周期）。
* **痛点**：当 CPU 线程试图去读取位于 DRAM 中的网卡描述符或 Ring Buffer 状态时，会触发昂贵的 `Cache Miss`。CPU 必须跨片上总线去访问物理 DRAM，产生 **60 ~ 100 ns** 的物理等待，导致 CPU 执行流水线（Pipeline）陷入长达 **200+ 个时钟周期的死等（Pipeline Stall）**。

### 1.2 共享 L3 Cache Stashing：Intel DDIO 的诞生与局限 (2012 年)
为了解决 DRAM 访问延迟问题，Intel 在 2012 年（Ivy Bridge-EP 架构 Xeon E5 v2）推出了 **Intel DDIO (Data Direct I/O)** 技术。
* **突破**：CPU 的 System Agent（Root Complex）拦截发往 DRAM 地址的 PCIe Write TLP 报文，将其**直接写入 CPU 的共享 L3 Cache（LLC, Last Level Cache）**，将数据标记为 `Modified/Dirty` 状态。
* **局限与技术路线归一**：
  1. **早期 DDIO 的私有局限**：早期的 Intel DDIO 是一个 **Intel 私有、黑盒的 Root Complex 拦截机制**。发出的 PCIe TLP 报文并不带 Steering Tag，CPU 边缘盲目拦截并塞入共享 LLC，无法精准指定 CPU 核心。
  2. **LLC 容量污染与 Way 锁死**：为了防止高速网卡流量冲垮 CPU 计算，Intel 默认将 DDIO 的写配额限制在 LLC 的 10% ~ 20%（Ways of LLC）。在 200G+ 高吞吐下，几 MB 的配额会在几微秒内爆仓，强行引发 LRU 逐出（Eviction）写回 DRAM。
  3. **路线归一至 PCIe TPH**：从 2023 年 Xeon Sapphire Rapids（4th Gen Xeon）开始，Intel 推出了 **Extended DDIO**，正式**归一并全面拥抱了 PCIe TPH (TLP Processing Hints) 标准路线**，使得网卡/IPU 发出的标准 TPH Steering Tag 能够直接被片上 Mesh 路由器解码，定向注入指定 LLC Slice 甚至 Core L2 Cache。

### 1.3 标准化 PCIe TPH (TLP Processing Hints) 与 Steering Tag (PCIe 3.0/4.0/5.0)
为了让外设能够“精准”通知 CPU，PCIe 规范（PCIe 3.0 及后续 4.0/5.0/6.0）引入了 **TPH (TLP Processing Hints)** 标准扩展。
* **机制**：外设在发出的 PCIe Read/Write TLP 报文头中，附带 **Steering Tag (ST)** 以及 Processing Hint 字段（如 Access Target: L2 / L3 / System Memory）。
* **意义**：统一了跨厂商的控制协议，使得不同网卡（如 Broadcom、Mellanox、Intel E810）能够将目标 CPU 逻辑核的 ID 告知 CPU 片上总线。

### 1.4 片上总线级定向 Stashing：Arm AMBA 5 CHI Stash 协议 (2016~2018 年)
Arm 在 AMBA 5 CHI (Coherent Hub Interface) 总线协议中原生定义了 **Cache Stashing** 事务规范。
* **机制**：定义了 `ReadCleanStash`、`StashOnceUnique` 等微架构事务类型。I/O 单元（或 PCIe 桥）在总线上发起 request 时带上 `StashLPID`（Logical Processor ID）。系统互联总线（DSU, DynamIQ Shared Unit）据此直接将数据装载至指定 Core 的私有 L2 缓存或 Cluster L3 中。
* **ARM 的双层协同架构**：ARM 在 SoC 芯片内部（DSU / Core Cluster 内）走的是原生 **AMBA 5 CHI 片上总线 `Stash` 协议**；而当跨标准 PCIe 外设插在 ARM 服务器（如 AWS Graviton3/4、AmpereOne）上时，**片外依然走标准的 PCIe TPH (TLP Processing Hints) 报文**！ARM 服务器的 PCIe Root Complex (RC) 在收到 PCIe TPH 报文后，在硬件桥接层将其**转译（Translate）为片上 AMBA CHI 的 Stash 事务**并送入 NoC。因此 ARM 并没有背离 PCIe 标准，而是实现了“片外 PCIe TPH 标准 + 片内 AMBA CHI 协议”的无缝协同。

### 1.5 近核私有 L2 定向注入与路线归一：AMD SDCI 与 Intel Extended DDIO (2022~2023 年)
随着 Chiplet 架构的全面演进，Cache Stashing 下沉到了 CPU 核心最近的私有缓存层，且技术路线完成了向 PCIe TPH 标准的统一：
* **AMD SDCI (Smart Data Cache Injection, 2022 年)**：在 EPYC 9004 (Genoa/Bergamo) 处理器中引入。结合 PCIe TPH，网卡 DMA 数据直接绕过 DRAM 和公共 LLC，**精准注入到负责该队列的 CCX (Core Complex) 内部的 Core L2 Cache（1MB~2MB）中**，CPU 读取延迟直接降至 **10 ~ 15 ns** 的物理极限。
* **Intel Extended DDIO (2023 年, 路线归一)**：正式拥抱 PCIe TPH 标准，将 TLP Steering Tag 路由至指定 Core L2 Cache。

### 1.6 芯片间一致性总线 Stashing：NVIDIA NVLink-C2C CHI Stash (2023~2024 年)
在 NVIDIA GH200 / GB200 等 Grace Hopper / Blackwell 异构系统架构中，Grace CPU 与 ConnectX-7 / BlueField-3 及 Hopper/Blackwell GPU 通过 **NVLink-C2C（Chip-to-Chip）** 链路互联。该总线原生支持 AMBA CHI 协议的 Cache Stashing，允许网卡或 GPU 将任务完成信号（Completion Tag）直接注入 Grace CPU 核心的 L2 Cache 中。

### 1.7 演进总结：从“数据推送到 DRAM”到“控制元数据精准推送至私有 L2”
Cache Stashing 的十年演进历史表明：**它的核心使命不是在 Cache 中保存海量数据 Payload，而是通过“控制元数据（Descriptors/Headers/Flags）精准推送至私有 L2”，消除物理 DRAM 延迟在 CPU 关键处理路径上的惩罚，实现零停顿（Zero-Stall）响应。**

---

## 二、 Cache Stashing 双向微观数据通路拆解（含直通与写回双路径）

### 2.1 方向一：RNIC -> Host L2/L3 Cache -> SSD (入站写 / 接收与落盘流程)

此流程对应于远端节点将 KV-Cache 通过 RDMA 网络推送到本地，本地 CPU 进行在线解析/校验/解压后，再下发写盘命令至 NVMe SSD 的过程。

```
[ RNIC (Sender Remote) ] ──(RoCEv2 Network)──> [ RNIC A (Receiver Local) ]
                                                        │
                                      (1. PCIe DMA Write + TPH Tag)
                                                        ▼
                                       [ CPU System Agent / Mesh Router ]
                                                        │
                                    ┌───────────────────┴───────────────────┐
                                    ▼                                       ▼
                         【命中 L3/L2 Stash 配额】                    【未命中 / 溢出退化】
                         直接写入 L3/L2 Cache                       直接写入物理 DRAM
                         标记为 Modified/Dirty                      (退化为 3x 放大)
                                    │
       ┌────────────────────────────┴───────────────────────────┐
       ▼                                                        ▼
 【路径 1: Snoop Hit 直吐 (0 GB DRAM 读)】                【路径 2: 脏行淘汰写回 (1x DRAM 写)】
  NVMe SSD 发起 PCIe Read, Snoop 控制器                   当内存 Buffer 释放或 L3 空间
  直接将 L3 脏行通过 PCIe 吐给 NVMe                         紧张被 LRU Evict 时, 自动写回
 (完全不从物理 DRAM 读取!)                                (有且仅有 1 次延迟写回落地)
```

#### 微观步骤拆解：
1. **RNIC 入站与 TLP 构建**：RNIC A 接收到 RoCEv2 网络报文，硬件解析 RETH 头的 `rkey` 和目标虚拟地址（VA），转化为系统 IOVA。RNIC 内部 DMA Engine 构建 PCIe **Memory Write TLP**，并在包头中嵌入 **PCIe TPH Steering Tag**（指定负责该 Rx 队列的 CPU Core A 编号）。
2. **片上路由与 Cache 拦截**：TLP 抵达 CPU Root Complex，片上网络（Mesh/Fabric）路由器解码 Steering Tag，直接将 Payload 数据包头及描述符路由到 Core A 对应的 **L3 Cache Slice 甚至私有 L2 Cache**。数据被写入 SRAM，该 Cache Line 状态被标记为 **`Modified (Dirty)`**。物理 DDR DRAM 保持不变，**DDR 物理写次 = 0**。
3. **CPU 极速命中与运算**：Core A 上的轮询线程（如 DPDK PMD）读取描述符，触发 `L2/L3 Cache Hit`（耗时仅 10~12ns）。CPU 执行 CRC32 校验指令或解压缩算法。由于 CRC 计算仅生成 4 字节 Checksum 放在寄存器中，不修改 Payload 自身，因此不产生额外的 Dirty Line 写回。若是解压缩，CPU 将解压后的 Payload $P_u$ 写入 L3 Cache，同样标记为 Dirty。
4. **路径 1：NVMe 零 DRAM 读出 (Outbound Read Hit)**：CPU 组装 NVMe SQ 描述符，敲响 NVMe 控制器的 Tail Doorbell。NVMe SSD 控制器发起 PCIe **Memory Read TLP**。CPU 的 Snoop Controller 探测到物理地址在 L3 Cache 中命中（Outbound Read Hit），**直接将 L3 Cache 中的数据通过 PCIe 总线吐给 NVMe 控制器**。数据完全不从 DRAM 读取，**DDR 物理读次 = 0**。
5. **路径 2：内存释放与淘汰写回 (Eviction Writeback)**：当 OS 释放该 KV-Cache 内存 Buffer 或被后续新数据挤出时，由于 Cache Line 是 `Modified` 状态，硬件 Cache Controller 自动执行一次 **Writeback（写回）** 落地到物理 DRAM。**DDR 物理写次 = 1**。

---

### 2.2 方向二：SSD -> Host L2/L3 Cache -> RNIC (出站读 / 发送与透传流程)

此流程对应于本地 Prefix-Cache 命中，需要将 SSD 中持久化的 KV-Cache 读出，通过网络发送往远端 Decode 节点的场景。

```
[ NVMe SSD Controller ] ──(1. PCIe DMA Write + TPH Tag)──> [ L3 Cache (Dirty) ]
                                                                   │
                                                                   ├──(2. RNIC DMA Read Hit)──> [ RNIC ] ──> Network
                                                                   │
                                                                   └──(3. Buffer 回收/淘汰)───> [ Host DDR DRAM ]
```

---

### 2.3 “ Read-and-Invalidate（读完即失效）”与零写回演进机制拆解

针对“**为什么消费完 Cache 之后仍然有 1 次写回 DRAM？能否彻底省掉这 1 次写回？**”进行了微架构级剖析：

```
[ NVMe SSD ] ──(1. PCIe Read + ReadOnce Hints)──> [ Snoop Controller ]
                                                         │
                                        (2. Outbound Read Hit 直吐数据)
                                                         ▼
                                             [ L3 Cache Line (Dirty) ]
                                                         │
                                     (3. 状态直接跃迁为 Invalid, 强行丢弃!)
                                                         ▼
                                            [ 无 DRAM 物理写回发生! ]
```

1. **现役 MESI 状态机的约束**：在传统 PCIe / x86 MESI 状态机中，`Modified` 状态意味着“系统内唯一的最新有效副本”。硬件探针无法预知上层软件后续是否还会读该内存，为防止数据损坏（Data Corruption），在 Buffer 释放/逐出时，硬件强制要求发生 **1 次延迟写回（Writeback）到 DRAM**。
2. **下一代协议突破（Read-and-Invalidate / ReadOnce）**：
   * **ARM AMBA 5 CHI `ReadOnce` / `ReadOnceCleanInvalid`**：显式告知 Cache Controller 该数据只消费一次。消费完后，Cache Line 状态直接切为 `Invalid` 并清空，**彻底切断写回 DRAM 的物理动作**。
   * **ARM ISA `DC IVAC` 指令**：在 AArch64 指令集中，软件执行 `DC IVAC`（Data Cache Invalidate by VA）可显式强行将脏行切为 `Invalid`，**且物理上明确不触发写回 DRAM**！
   * **CXL 3.0 (CXL.cache) Transient Memory Buffer Invalidation**：针对暂态 Bounce Buffer，数据消费完毕后发起的 CXL Cache Line Invalidate 命令可将脏行直接抹除，实现真正的 **0 DDR Read + 0 DDR Write**。

---

## 三、 KV-Cache 落盘场景：Cache Stashing 访存放大倍数定量对比

### 3.1 理论推导模型与参数定义

为精准量化，基于第一性原理定义物理模型参数：
* **网络传输数据量**：设网络接收到的原始数据量为 $P_c$（若开启压缩，则为压缩数据量 $P_c$；若未压缩，则 $P_c = P_u$）。
* **解压后数据量**：设解压后的实际 KV-Cache 体积为 $P_u$。定义解压倍率为 $r = P_u / P_c$（典型 $r = 2.0$，即 2:1 压缩比）。
* **DDR 访存放大倍数 ($M_{\text{DDR}}$)**：
  $$M_{\text{DDR\_Net}} = \frac{\text{Total DDR Physical Bytes Transferred}}{P_c} \quad (\text{相对于网络传输量 } P_c)$$
  $$M_{\text{DDR\_Uncompressed}} = \frac{\text{Total DDR Physical Bytes Transferred}}{P_u} \quad (\text{相对于解压后有效量 } P_u)$$

---

### 3.6 详细计算数据对比汇总表（含下一代零写回演进对比）

假设网络传入物理 Payload $P_c = 1.0\text{ GB}$，解压倍率 $r = 2.0$（即解压后 $P_u = 2.0\text{ GB}$）：

| 功能组合场景 | Cache Stashing 状态 | DDR 物理写 (GB) | DDR 物理读 (GB) | DDR 总流量 (GB) | 相对网络 $P_c$ 放大倍数 | 相对解压后 $P_u$ 放大倍数 | DDR 带宽节省率 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **场景 1：无 CRC / 无解压** | 关闭 Stash (Direct-DRAM) | $1.0$ | $1.0$ | **$2.0\text{ GB}$** | **$2.0\times$** | $2.0\times$ | 基准 |
| | **开启 Stash (Hot L3 命中)** | **$1.0$ (写回)** | **$0.0$ (直吐)**| **$1.0\text{ GB}$** | **$1.0\times$** | **$1.0\times$** | **节省 50.0%** |
| | 开启 Stash (Cold L3 溢出) | $1.0$ | $1.0$ | $2.0\text{ GB}$ | $2.0\times$ | $2.0\times$ | 0% (退化) |
| | **下一代 ReadOnce Stashing**| **$0.0$** | **$0.0$** | **$0.0\text{ GB}$** | **$0.0\times$** | **$0.0\times$** | **节省 100%** |
| | **DPU 硬件直通 (P2P DMA)**| **$0.0$** | **$0.0$** | **$0.0\text{ GB}$** | **$0.0\times$** | **$0.0\times$** | **节省 100%** |
| **场景 2：有 CRC / 无解压** | 关闭 Stash (Direct-DRAM) | $1.0$ | $2.0$ | **$3.0\text{ GB}$** | **$3.0\times$** | $3.0\times$ | 基准 |
| | **开启 Stash (Hot L3 命中)** | **$1.0$** | **$0.0$** | **$1.0\text{ GB}$** | **$1.0\times$** | **$1.0\times$** | **节省 66.7%** |
| | 开启 Stash (Cold L3 溢出) | $1.0$ | $2.0$ | $3.0\text{ GB}$ | $3.0\times$ | $3.0\times$ | 0% (退化) |
| | **DPU 硬件直通 (P2P DMA)**| **$0.0$** | **$0.0$** | **$0.0\text{ GB}$** | **$0.0\times$** | **$0.0\times$** | **节省 100%** |
| **场景 3：无 CRC / 有解压** | 关闭 Stash (Direct-DRAM) | $1.0(P_c) + 2.0(P_u)$ | $1.0(P_c) + 2.0(P_u)$ | **$6.0\text{ GB}$** | **$6.0\times$** | **$3.0\times$** | 基准 |
| | **开启 Stash (Hot L3 命中)** | **$2.0(P_u)$** | **$0.0$** | **$2.0\text{ GB}$** | **$2.0\times$** | **$1.0\times$** | **节省 66.7%** |
| | 开启 Stash (Cold L3 溢出) | $3.0$ | $3.0$ | $6.0\text{ GB}$ | $6.0\times$ | $3.0\times$ | 0% (退化) |
| | **DPU 硬件直通 (P2P DMA)**| **$0.0$** | **$0.0$** | **$0.0\text{ GB}$** | **$0.0\times$** | **$0.0\times$** | **节省 100%** |
| **场景 4：有 CRC / 有解压** | 关闭 Stash (Direct-DRAM) | $3.0$ | $3.0$ | **$6.0\text{ GB}$** | **$6.0\times$** | **$3.0\times$** | 基准 |
| | **开启 Stash (Hot L3 命中)** | **$2.0(P_u)$** | **$0.0$** | **$2.0\text{ GB}$** | **$2.0\times$** | **$1.0\times$** | **节省 66.7%** |
| | 开启 Stash (Cold L3 溢出) | $3.0$ | $3.0$ | $6.0\text{ GB}$ | $6.0\times$ | $3.0\times$ | 0% (退化) |
| | **DPU 硬件直通 (P2P DMA)**| **$0.0$** | **$0.0$** | **$0.0\text{ GB}$** | **$0.0\times$** | **$0.0\times$** | **节省 100%** |

---

## 四、 Cache Stashing 技术的全栈软硬件依赖配套

Cache Stashing 并非简单的驱动开关，而是依赖从**PCIe 协议层、CPU Uncore 互联、Cache 一致性控制器到 OS 内核与用户态数据面框架**的全栈协同：

```
+-------------------------------------------------------------------------------+
|                       Cache Stashing 全栈技术依赖配套                           |
+-------------------------------------------------------------------------------+
|                                                                               |
| [应用/框架层]  DPDK PMD / SPDK CQE Polling / CUDA Event / vLLM Scheduler       |
|                 ↳ 绑定 Core 亲和性，极简 Command 循环 (低 I-Cache 占用)          |
+-------------------------------------------------------------------------------+
| [操作系统层]  Linux Kernel (CONFIG_PCIE_TPH) / ACPI _DSM 表 / IRQ Affinity     |
|                 ↳ 解析 ACPI TPH 表格，将 MSI-X Vector 映射给 Steering Tag (ST) |
+-------------------------------------------------------------------------------+
| [固件与 BIOS]  BIOS 开启 DDIO/SDCI / PCIe ACS 转发使能                         |
|                 ↳ 配置 Direct Cache Access 规则与 TPH 属性                       |
+-------------------------------------------------------------------------------+
| [芯片硬件层]  PCIe Endpoint (网卡/DPU 带 TPH) ──► PCIe Switch / RC              |
|                 ──► CPU Uncore Mesh (Steering Decoder) ──► L2/L3 Cache (CHI) |
+-------------------------------------------------------------------------------+
```

### 4.1 硬件层依赖 (Hardware Layer)
1. **PCIe Endpoint (外设端)**：网卡（RNIC）、DPU 或 NVMe 控制器硬件芯片必须支持 **PCIe TPH (TLP Processing Hints)** 规范，具备在发出的 Memory Read/Write TLP 中嵌入 8-bit/16-bit **Steering Tag (ST)** 的能力。
2. **PCIe Root Complex (RC) & Switch**：Root Complex 必须能够透传带有 TPH Hints 的 TLP 报文，不修改或丢弃 Processing Hint 字段。若开启虚拟化/IOMMU，PCIe **ACS (Access Control Services)** 必须配置正确的 P2P/Steering 规则。
3. **CPU Uncore & 片上网络 (Mesh/Fabric)**：芯片内部的 System Agent / Mesh 路由器必须内置 **Steering Tag Decoder** 硬件逻辑，能够将 ST Tag 解码为物理 Core ID / CCX 编号，并将 TLP Payload 导向正确的 Cache Block。
4. **Cache 级联与一致性控制器**：CPU 物理 Cache 控制器必须支持 **AMBA 5 CHI Stash 事务** 或 **Extended DDIO / SDCI 逻辑**。

### 4.2 粒度不对齐物理处理（PCIe 128B TLP vs. CPU 64B Cache Line）
* **物理事实**：PCIe 外设发起的 Read/Write TLP 通常包含 128B、256B 或更大的 Payload，或遵从 128B Read Completion Boundary (RCB)，而主流 CPU（Intel/AMD/ARM）物理 Cache Line 均为 64B。
* **硬件级 TLP 拆包与拼装 (Splitting & CplD Assembly)**：
  1. **入站写拆分**：Root Complex 接收到外设 128B Write TLP（带 TPH Steering Tag）后，片上硬件自动将其解包并拆分为 **2 个 64B 独立片内事务**（ARM CHI / Intel Mesh Packets），并发注入到 2 个 64B 物理 Cache Line Slot 中。
  2. **出站读探针拼装**：NVMe SSD 发起 128B PCIe Read 时，Snoop Controller 在 L3 中探针 2 个连续的 64B Lines，Outbound Hit 后由 Root Complex 拼装为 1 个 128B PCIe CplD 报文发往 SSD。
* **对齐惩罚与工程最佳实践**：
  1. **Partial Write (读-修改-写 RMW 惩罚)**：若 128B 数据物理首地址未按 64B 对齐（例如跨越 3 个 Cache Line），会导致首尾 Line 发生局部写入，触发 CPU **Read-Modify-Write**，产生额外的 30~50ns 延迟惩罚。
  2. **内存结构对齐规范**：高性能框架（DPDK/SPDK）的 Ring Buffer 描述符与 Header Buffer 在代码中必须强制添加 **`__attribute__((aligned(64)))`**，防止跨行产生 RMW 性能损失与配额浪费。

---

## 五、 各大厂商 Cache Stashing 特性与软硬件布局矩阵

以下汇总 Intel、AMD、Arm 生态（AWS/Ampere/阿里）及 NVIDIA 在 Cache Stashing 方向上的技术布局与发布年份：

| 厂商 | 代表性 CPU / 芯片产品 | 硬件特性与协议名称 | 目标 Cache 层级 | 软件生态与驱动支持 | 发布年份 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Intel** | **CPU**: Xeon Scalable 1st~3rd Gen (Skylake/Cascade/Ice Lake)<br>**新 CPU**: Xeon 4th~6th Gen (Sapphire/Emerald/Granite Rapids, Sierra Forest)<br>**NIC/IPU**: Intel E810, IPU E2000 | **Intel DDIO** (早期私有)<br><br>**Extended DDIO**<br>+ PCIe TPH (路线归一) | 早期: 共享 **LLC 20% Way**<br><br>近期: 定向 **LLC Slice** 甚至 **Core L2 Cache** | • **Kernel**: `CONFIG_PCIE_TPH` 驱动、ACPI `_DSM` 映射<br>• **框架**: DPDK PMD 驱动、SPDK NVMe 队列轮询<br>• **SDK**: Intel IPU SDK | 2012 (DDIO)<br><br>2023 (Ext-DDIO) |
| **AMD** | **CPU**: EPYC 9004 (Genoa/Bergamo), EPYC 9005 (Turin)<br>**DPU**: Pensando Salina / Pollara | **AMD SDCI**<br>(Smart Data Cache Injection)<br>+ PCIe TPH Steering Tag | 目标 CCX 的 **私有 L2 Cache** (1MB~2MB) | • **Kernel**: 原生 ACPI TPH Steering Tag 解析<br>• **网卡绑定**: Broadcom BNXT / Mellanox NIC + DPDK/XDP<br>• **存储**: SPDK NVMe-oF CQE 直接注入 | 2022 |
| **Arm 生态**<br>*(AWS / Ampere / 阿里)* | **IP/CPU**: Neoverse N1/N2/V1/V2/V3, DSU-110/120<br>**芯片**: AWS Graviton3/4, AmpereOne, 阿里倚天 710 | **AMBA 5 CHI** Cache Stashing 协议 (片外转译 PCIe TPH) | 核心 **私有 L2 Cache** 或 **Cluster L3** | • **总线**: AMBA CHI 内核架构驱动<br>• **云端**: AWS Nitro 硬件调度栈与 Guest OS 映射<br>• **数据面**: DPDK / SPDK 适配 ARM64 AMBA CHI 报文 | 2016 (协议)<br>2021 (Graviton3)<br>2023 (Graviton4) |
| **NVIDIA** | **CPU**: Grace CPU (GH200 / GB200)<br>**DPU/NIC**: BlueField-3 DPU, ConnectX-7 / ConnectX-8 | **NVLink-C2C CHI Stash**<br>+ PCIe TPH Cache Steering | Grace 核心 **私有 L2 Cache** (1MB) 与 System Cache | • **驱动**: NVIDIA Grace SoC 驱动栈<br>• **软件栈**: DOCA SDK 异步事件通知<br>• **AI 框架**: CUDA / Triton / vLLM CPU-GPU 任务队列极速握手 | 2023 (GH200)<br>2024 (GB200) |

### 5.1 Cache Stashing 厂商布局与生态洞察 (Vendor Insights)

* **特性支持完备程度与路线归一**：各大厂商在硬件层已全面收敛于 **PCIe TPH (TLP Processing Hints)** 与 **Arm AMBA 5 CHI** 协议双标准。Intel 从早期的私有黑盒 DDIO 盲目拦截**全面归一**至 Extended DDIO (PCIe TPH 标准路线)；ARM 实现了“片外 PCIe TPH 标准 $\rightarrow$ PCIe Root Complex 硬件转译 $\rightarrow$ 片内 AMBA CHI Stash”的软硬件协同；AMD (SDCI) 与 NVIDIA (NVLink-C2C CHI Stash) 均实现了将 DMA 直接注入 Core 级私有 L2 Cache 的精准控制。
* **软件生态建设现状**：软硬件闭环体系建设健全。操作系统层（Linux 内核 `CONFIG_PCIE_TPH` 及 ACPI `_DSM` 表）与用户态高性能数据面框架（DPDK PMD、SPDK CQE Polling、NVIDIA DOCA SDK）已完成深度适配，实现了无需更改上层业务逻辑即可零开销透传硬件 Steering Tag。
* **实际生产落地案例**：
  * **AWS Graviton3/4 + Nitro V5**: Nitro 卡在处理 EBS 块存储 descriptor 与 VirtIO 虚拟化网卡 Header 时，通过 AMBA CHI Stash 注入 Graviton 核心 L2 Cache。
  * **Broadcom NetXtreme-E (BNXT) / ConnectX-7 + AMD EPYC 9004/9005 (SDCI)**: 在高频交易 (HFT) 平台与 400G OVS-DPDK 中，网卡将行情 UDP 包头与 Rx Ring Descriptor 直塞目标 CPU Core L2，消除了 200+ 周期的 Pipeline Stall。
  * **NVIDIA GH200 / GB200 (Grace Hopper/Blackwell)**: CUDA / TensorRT-LLM 任务队列交互中，利用 NVLink-C2C CHI Stash 将 GPU Task Completion Flag 直塞 Grace CPU 核心 L2 Cache，极大压缩了 CPU-GPU 任务调度气泡。

---

## 六、 各大厂商 DPU 与 SSD 直通（P2P DMA）特性与软硬件布局矩阵

以下汇总各大厂商在面向大块存储落盘与换出的 **DPU-to-SSD 直通（Peer-to-Peer DMA）** 方案上的布局与发布年份：

| 厂商 | 代表性 DPU / 加速器与存储硬件 | 直通协议与传输架构 | 硬件硬件硬化引擎 (Inline Offload Engines) | 软件生态与存储框架 | 发布年份 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **NVIDIA** | **DPU**: BlueField-2 / BlueField-3<br>**NIC**: ConnectX-6 / CX-7 / CX-8<br>**平台**: Grace Hopper / Blackwell | **GPUDirect Storage (GDS)**<br>+ PCIe P2PDMA<br>+ NVMe CMB / PMR | • **解压缩**: 硬件线速 LZ4 / Deflate Engine<br>• **校验**: 硬件 CRC32 / CRC64 Engine<br>• **安全**: AES-XTS 硬件加解密 | • **存储 SDK**: NVIDIA DOCA Storage Stack<br>• **存储框架**: SPDK GDS Plugin, NVMe-oF Target<br>• **AI 集成**: TensorRT-LLM / vLLM KV Cache Swap 插件 | 2020 (BF-2)<br>2023 (BF-3) |
| **AMD** | **DPU**: Pensando Salina / Pollara DPU<br>**SSD**: Alveo SmartSSD (FPGA CSD)<br>**平台**: EPYC + Pensando Helios | **PCIe P2PDMA**<br>+ NVMe CMB/PMR<br>+ CXL 2.0 / 3.0 Direct | • **压缩**: 硬件流式 LZ4 / ZSTD Engine<br>• **完整性**: Pipeline 级 CRC64 计算逻辑<br>• **安全**: 硬化 Crypto Engine | • **软件套件**: AMD Pensando Software Suite<br>• **内核**: Linux Kernel `p2pdma` / `p2pmem` 模块<br>• **存储框架**: 开源 SPDK P2P DMA Driver | 2022 (收购)<br>2024 (Pollara) |
| **Intel** | **IPU**: Mount Evans (IPU E2000)<br>**加速器**: QAT (QuickAssist) / DSA<br>**平台**: Xeon Scalable Platform | **PCIe P2PDMA**<br>+ NVMe CMB/PMR<br>+ CXL 内存/存储直通 | • **QAT Engine**: 硬件 Deflate / LZ4 压缩解压<br>• **DSA Engine**: 高速数据搬运与 CRC32C 校验<br>• **IPU**: 流式硬件数据包过滤与校验 | • **驱动栈**: Intel IPU SDK, QAT Engine Driver<br>• **存储框架**: SPDK P2PDMA Plugin<br>• **生态**: Linux Kernel NVMe target 硬件卸载 | 2021 (Mount Evans)<br>2023 (DSA/QAT) |
| **Arm 生态**<br>*(AWS / Marvell)* | **AWS**: Nitro V4 / V5 / V6 Card + Nitro SSD<br>**Marvell**: OCTEON 10 DPU (Neoverse N2)<br>**Fungible**: F1 DPU (已被微软收购) | **定制 ASIC PCIe P2P 管道**<br>+ AMBA CHI P2P<br>+ NVMe 接口 | • **Nitro ASIC**: 硬件流式解压/CRC/EBS 加密<br>• **OCTEON 10**: Inline Zip / Crypto Co-processors<br>• **Fungible**: TrueFabric 处理引擎 | • **AWS 平台**: Nitro Hypervisor / EBS 存储栈 (云端闭环)<br>• **Marvell**: OCTEON SDK, DPDK/SPDK 扩展<br>• **开源**: Linux Kernel Arm64 P2PDMA | 2017 (Nitro V1)<br>2022 (Nitro V5)<br>2022 (OCTEON 10) |

### 6.1 DPU 与 SSD 直通 (P2P DMA) 厂商布局与生态洞察 (Vendor Insights)

* **特性支持完备程度**：硬件厂商（NVIDIA BlueField-3、AMD Pensando、Intel IPU/QAT、AWS Nitro）普遍集成了硬化的线速解压缩（LZ4/Deflate/ZSTD）、CRC32/64 校验和 AES 加解密引擎。配合 PCIe P2PDMA 与 NVMe CMB/PMR 规范，在硬件数据面上已完整实现 100% 旁路 Host CPU 与 Host DRAM 的直通管道。
* **软件生态建设现状**：公有云巨头与头部硬件厂商（如 AWS Nitro 存储栈、NVIDIA DOCA/GDS SPDK Plugin、AMD Pensando SDK）已构建起完备的底层驱动与 API 栈。然而在通用开源社区（如标准 Linux Kernel 内核 NVMe target 与通用 AI 框架）中，开箱即用的跨厂商 P2P 协作仍存在较强的厂商私有生态壁垒。
* **实际生产落地案例**：
  * **AWS EBS (Elastic Block Store) Nitro System**: Nitro DPU 卡在解密与 CRC 校验后，通过 PCIe P2PDMA 直接写入 NVMe Nitro SSD，Host Graviton CPU 完全 0% 占用。
  * **NVIDIA GPUDirect Storage (GDS) + BlueField-3 DPU**: 在大模型推理（vLLM / TensorRT-LLM）生产集群中，BlueField-3 硬件解压 KV-Cache，结合 GDS P2PDMA 直通 NVMe SSD，实现 KV-Cache Swap-Out 全过程 0% CPU 占用与 0 DRAM 流量。
  * **AMD Pensando Salina/Pollara + Helios AI 整柜**: Pensando DPU 配合 SPDK P2P 驱动将网存数据线速落盘至 PCIe SSD。
  * **Samsung SmartSSD / Solidigm CSD (可计算存储盘)**: 在 Spark/Hadoop 检索与数据库节点中，直接在 SSD 内置 FPGA 完成线速解压与 CRC 校验。

---

## 七、 KV-Cache 场景技术对比：DPU-SSD 直通 vs. Cache Stashing (L2/L3)

在 LLM 推理架构（如 Disaggregated Prefill-Decode 或 Tiered KV-Cache）中，针对 KV-Cache 的传输与持久化，DPU 直通 SSD 与 Cache Stashing 代表了两种截然不同的架构设计哲学：

```
[ 架构方案 A: DPU 直通 SSD (Data Plane Bypass) ]
RNIC ──(RoCEv2)──► DPU (硬件解压+CRC) ──(PCIe P2P DMA)──► NVMe SSD
                   ▲ (全过程 Zero-Host-CPU, Zero-Host-DRAM)

[ 架构方案 B: CPU Cache Stashing (Control/Compute Plane) ]
RNIC ──(PCIe TPH)──► Core L2/L3 Cache ──► CPU 处理控制头/调度 ──► DRAM ──► SSD
                     ▲ (极速通知 CPU, 消除 Pipeline Stall)
```

---

## 八、 KV-Cache 加速传输的其它可选前沿方案

除了 DPU 直通 SSD 与 Cache Stashing 之外，工业界和学术界还探索了以下几种高价值的 KV-Cache 加速传输与存储方案：

```
+-----------------------------------------------------------------------------------+
|                        其它 KV-Cache 加速传输与存储前沿方案                         |
+-----------------------------------------------------------------------------------+
|                                                                                   |
|  1. 可计算存储盘 (CSD / SmartSSD) ──► 校验与解压直接在 SSD 控制器/FPGA 内部完成      |
|  2. CXL (Compute Express Link) ──► CXL.mem 共享内存池, 硬件级跨节点内存借用       |
|  3. GPUDirect Storage (GDS) ──► GPU HBM 与 NVMe SSD / 远端 GPU 物理 P2P 直连       |
|  4. 流式微块量化 (Micro-chunking) ──► 16MB 窗口流式传输 + FP8/INT8 在线动态量化       |
|                                                                                   |
+-----------------------------------------------------------------------------------+
```

---

## 九、 总结与架构演进洞察

### 9.1 片外 PCIe TPH 标准化与片内 Fabric 私有化的分层解耦范式

```
+-----------------------------------------------------------------------------------+
| 1. 片外总线层 (Off-Chip) : 100% 统一走 PCIe TPH + Steering Tag 国际标准             |
|    [网卡 / DPU / NVMe SSD] ──(PCIe Memory Write TLP + TPH Tag)──► [PCIe Bus]      |
+-----------------------------------------------------------------------------------+
                                          │
                                          ▼
+-----------------------------------------------------------------------------------+
| 2. 边界转译层 (Boundary)  : PCIe Root Complex / I/O Die 提取 ST 翻译为片内 Core ID |
+-----------------------------------------------------------------------------------+
                                          │
                                          ▼
+-----------------------------------------------------------------------------------+
| 3. 片内互联层 (On-Chip)   : 各家走各自私有/生态内的极速 Fabric / NoC 总线协议       |
|                                                                                   |
|    • Intel : 转换为 Intel Mesh / UPI 片内报文  ──► 目标 LLC Slice / Core L2      |
|    • AMD   : 转换为 Infinity Fabric (IF) 报文 ──► 目标 CCX L2 Cache (SDCI)      |
|    • ARM   : 转换为 AMBA 5 CHI (StashLPID) ──► 目标 DSU / Core L2 Cache         |
|    • NVIDIA: 转换为 NVLink-C2C CHI 报文       ──► 目标 Grace Core L2 Cache      |
+-----------------------------------------------------------------------------------+
```

系统微架构在片外与片内的深层次物理分层逻辑如下：
1. **片外求“极大兼容性（Interoperability）”**：PCIe 是跨厂商通用标准。PCIe SIG 全盘统一了 **PCIe TPH (TLP Processing Hints) & Steering Tag** 规范。外设厂商只需向 PCIe TLP 头填入 ST Tag，即可无缝兼容任意 CPU 平台。
2. **片内求“极致性能与物理效率（Efficiency）”**：CPU 芯片内部的 Mesh、Infinity Fabric 或 AMBA CHI 总线运行在 2GHz~3.5GHz 极高主频下，对延迟要求是纳秒级。一跨过 Root Complex 芯片边界，硬件立刻将 PCIe 报文“拆包”，提取 ST Tag 并转译为 CPU 片内各自的极速总线协议。

### 9.2 Cache Stashing 的“黄金三角”约束网络

```
               [ 1. 数据极小 ]
               (64B - 256B 描述符/包头)
                    / \
                   /   \
                  /     \
                 /       \
[ 2. 逻辑极精 ] ◄─────────► [ 3. 消费极快 ]
(指令 footprint 小)         (CPU 线程随时待命 Polling)
```

### 9.3 终极收敛趋势：控制面 Lean Control + Data Bypass
计算体系结构的演进最终收敛于一个核心哲学：
* **让 CPU 彻底退居控制平面（Lean Control Plane）**：CPU 核心不再承担大块拷贝、加解密与解压缩，其唯一使命是**利用 Cache Stashing 以纳秒级速度响应事件、解析控制头、调度任务**。
* **让硬件加速器与总线接管数据平面（Bypass Data Plane）**：海量数据 Payload 交由 **DPU、PCIe P2P DMA、CXL 及 GDS** 在底层硬件总线上穿梭，彻底打通“内存墙”，实现数据中心级的高吞吐与确定性低延迟。
