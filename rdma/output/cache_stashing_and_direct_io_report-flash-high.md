# 缓存 Stashing 与 Direct I/O 技术深度解析与厂商布局报告

> **摘要**：在现代高性能计算、大模型推理（LLM Inference）及超大规模数据中心中，“内存墙（Memory Wall）”与 CPU 流水线停顿（Pipeline Stall）已成为系统吞吐与时延的最核心瓶颈。为了突破这一物理限制，系统架构沿着**控制面与数据面解耦（Control/Data Plane Separation）**的方向演化出了两条核心技术路径：一条是面向控制面与事件通知的 **Cache Stashing（硬件定向缓存注入）** 技术；另一条是面向数据面大块存储落盘与换出的 **DPU 与 SSD 端到端直通（PCIe P2P DMA）** 技术。
>
> 本报告基于对底层总线协议（PCIe TPH、AMBA CHI）、CPU 存储微架构（Intel DDIO/Extended DDIO、AMD SDCI、Arm CHI Stash）及硬件加速器（NVIDIA BlueField、AMD Pensando、Intel IPU/QAT、AWS Nitro）的深度物理拆解，全面总结 Cache Stashing 的发展演进、双向微观流程、KV-Cache 场景下的 DDR 访存放大倍数定量演算、全栈软硬件依赖、各大厂商布局矩阵及对比优劣势。

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
|  [阶段 2: 共享 LLC Stashing 时代] (2012 年, Intel DDIO 问世)                       |
|   PCIe DMA ──► 拦截写入共享 L3/LLC ──► 消除了 DRAM 写 ──► 限制: 仅限 LLC, 易跨 Slice|
|                                                                                   |
|  [阶段 3: 协议层标准化与 Steering Tag 时代] (2016~2020 年, PCIe TPH & ARM CHI)     |
|   PCIe TPH TLP 包含 Steering Tag / AMBA CHI 带 StashLPID ──► 定向路由至目标 CPU    |
|                                                                                   |
|  [阶段 4: 近核私有 L2 定向注入时代] (2022~2024 年, AMD SDCI & Intel Ext-DDIO)       |
|   网卡 DMA ──(PCIe TPH Tag)──► 绕过 LLC 直接注入目标 CCX/Core 私有 L2 (10~15ns Hit)  |
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
* **局限**：
  1. **LLC 容量污染与 Way 锁死**：为了防止高速网卡流量冲垮 CPU 计算，Intel 默认将 DDIO 的写配额限制在 LLC 的 10% ~ 20%（Ways of LLC）。在 200G+ 高吞吐下，几 MB 的配额会在几微秒内爆仓，强行引发 LRU 逐出（Eviction）写回 DRAM。
  2. **跨 CCX/Tile 访问开销**：DDIO 仅将数据存入“某个”LLC Slice。在现代多 Chiplet 架构（如 NUMA Tile 或多 CCX）中，若数据落在 Die A 的 LLC，而处理该队列的 CPU Core 在 Die B 上，跨网格总线（Mesh/Fabric）抓取数据依然产生 **30 ~ 50 ns** 延迟。

### 1.3 标准化 PCIe TPH (TLP Processing Hints) 与 Steering Tag (PCIe 3.0/4.0/5.0)
为了让外设能够“精准”通知 CPU，PCIe 规范（PCIe 3.0 及后续 4.0/5.0）引入了 **TPH (TLP Processing Hints)** 标准扩展。
* **机制**：外设在发出的 PCIe Read/Write TLP 报文头中，附带 **Steering Tag (ST)** 以及 Processing Hint 字段（如 Access Target: L2 / L3 / System Memory）。
* **意义**：统一了跨厂商的控制协议，使得不同网卡（如 Broadcom、Mellanox、Intel E810）能够将目标 CPU 逻辑核的 ID 告知 CPU 片上总线。

### 1.4 片上总线级定向 Stashing：Arm AMBA 5 CHI Stash 协议 (2016~2018 年)
Arm 在 AMBA 5 CHI (Coherent Hub Interface) 总线协议中原生定义了 **Cache Stashing** 事务规范。
* **机制**：定义了 `ReadCleanStash`、`StashOnceUnique` 等微架构事务类型。I/O 单元（或 PCIe 桥）在总线上发起 request 时带上 `StashLPID`（Logical Processor ID）。系统互联总线（DSU, DynamIQ Shared Unit）据此直接将数据装载至指定 Core 的私有 L2 缓存或 Cluster L3 中。

### 1.5 近核私有 L2 定向注入：AMD SDCI 与 Intel Extended DDIO (2022~2023 年)
随着 Chiplet 架构的全面演进，Cache Stashing 下沉到了 CPU 核心最近的私有缓存层：
* **AMD SDCI (Smart Data Cache Injection, 2022 年)**：在 EPYC 9004 (Genoa/Bergamo) 处理器中引入。结合 PCIe TPH，网卡 DMA 数据直接绕过 DRAM 和公共 LLC，**精准注入到负责该队列的 CCX (Core Complex) 内部的 Core L2 Cache（1MB~2MB）中**，CPU 读取延迟直接降至 **10 ~ 15 ns** 的物理极限。
* **Intel Extended DDIO (2023 年)**：在 Sapphire Rapids/Emerald Rapids Xeon 处理器中推出，扩展了传统 DDIO，支持配合 PCIe TPH 将数据定向推送到指定的 LLC Slice 甚至 Core L2 Cache。

### 1.6 芯片间一致性总线 Stashing：NVIDIA NVLink-C2C CHI Stash (2023~2024 年)
在 NVIDIA GH200 / GB200 等 Grace Hopper / Blackwell 异构系统架构中，Grace CPU 与 ConnectX-7 / BlueField-3 及 Hopper/Blackwell GPU 通过 **NVLink-C2C（Chip-to-Chip）** 链路互联。该总线原生支持 AMBA CHI 协议的 Cache Stashing，允许网卡或 GPU 将任务完成信号（Completion Tag）直接注入 Grace CPU 核心的 L2 Cache 中。

### 1.7 演进总结：从“数据推送到 DRAM”到“控制元数据精准推送至私有 L2”
Cache Stashing 的十年演进历史表明：**它的核心使命不是在 Cache 中保存海量数据 Payload，而是通过“控制元数据（Descriptors/Headers/Flags）精准推送至私有 L2”，消除物理 DRAM 延迟在 CPU 关键处理路径上的惩罚，实现零停顿（Zero-Stall）响应。**

---

## 二、 Cache Stashing 双向处理流程微观拆解

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
                                    ├────────────────────────┐
                                    │ (2. CPU Load Hit)      │ (3. CPU Store Result)
                                    ▼                        ▼
                              [ CPU Core ] ─────────────> [ L3/L2 Cache ]
                           (计算 CRC / 解压)              (更新后数据为 Dirty)
                                                             │
                                                             │ (4. NVMe DMA Read Hit)
                                                             ▼
                                                    [ NVMe SSD Controller ]
                                                             │
                                                             │ (5. 内存释放/延迟写回)
                                                             ▼
                                                    [ Host DDR DRAM ]
```

#### 微观步骤拆解：
1. **RNIC 入站与 TLP 构建**：RNIC A 接收到 RoCEv2 网络报文，硬件解析 RETH 头的 `rkey` 和目标虚拟地址（VA），转化为系统 IOVA。RNIC 内部 DMA Engine 构建 PCIe **Memory Write TLP**，并在包头中嵌入 **PCIe TPH Steering Tag**（指定负责该 Rx 队列的 CPU Core A 编号）。
2. **片上路由与 Cache 拦截**：TLP 抵达 CPU Root Complex，片上网络（Mesh/Fabric）路由器解码 Steering Tag，直接将 Payload 数据包头及描述符路由到 Core A 对应的 **L3 Cache Slice 甚至私有 L2 Cache**。数据被写入 SRAM，该 Cache Line 状态被标记为 **`Modified (Dirty)`**。物理 DDR DRAM 保持不变，**DDR 物理写次 = 0**。
3. **CPU 极速命中与运算**：Core A 上的轮询线程（如 DPDK PMD）读取描述符，触发 `L2/L3 Cache Hit`（耗时仅 10~12ns）。CPU 执行 CRC32 校验指令或解压缩算法。由于 CRC 计算仅生成 4 字节 Checksum 放在寄存器中，不修改 Payload 自身，因此不产生额外的 Dirty Line 写回。若是解压缩，CPU 将解压后的 Payload $P_u$ 写入 L3 Cache，同样标记为 Dirty。
4. **NVMe 零 DRAM 读出**：CPU 组装 NVMe SQ 描述符，敲响 NVMe 控制器的 Tail Doorbell。NVMe SSD 控制器发起 PCIe **Memory Read TLP**。CPU 的 Snoop Controller 探测到物理地址在 L3 Cache 中命中（Outbound Read Hit），**直接将 L3 Cache 中的数据通过 PCIe 总线吐给 NVMe 控制器**。数据完全不从 DRAM 读取，**DDR 物理读次 = 0**。
5. **内存释放与延迟写回 (Eviction)**：当 OS 释放该 KV-Cache 内存 Buffer 或被后续新数据挤出时，由于 Cache Line 是 `Modified` 状态，硬件 Cache Controller 自动执行一次 **Writeback（写回）** 落地到物理 DRAM。**DDR 物理写次 = 1**。

* **本方向物理小结**：在 Hot L3 命中流水线下，数据在 L3 充当“双端口 SRAM 缓存池”，**消除了 2 次 DDR 物理读取，仅产生 1 次延迟写回，物理带宽开销降至最低。**

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

#### 微观步骤拆解：
1. **SSD 驱动入站 Stash**：CPU 下发 NVMe Read 命令，指定 Host 侧 Buffer 地址。NVMe SSD 控制器读取 Flash 后，发起 PCIe Memory Write TLP。由于开启了 Stash/DDIO，数据直接被推送至 **L3 Cache** 中（标记为 Dirty），**DDR 物理写次 = 0**。
2. **RNIC 即刻读出命中**：网卡收到 CPU 的发送指令（Post Send WQE），发起 PCIe Memory Read TLP。由于读取间隔在微秒级内，Snoop Controller 捕捉到 **Outbound DDIO Read Hit**，直接将 **L3 Cache Line 数据通过 PCIe 总线拉走并封装为 RoCEv2 报文发往网络**，**DDR 物理读次 = 0**。
3. **延迟淘汰写回**：当该 Buffer 被回收淘汰时，Dirty Line 写回 DRAM，**DDR 物理写次 = 1**（若为 Clean 状态或被覆盖则为 0）。

#### 生效的三个硬性条件：
1. **Outbound Read Hit 硬件支持**：CPU 必须支持外设 DMA Read 直接命中 L3 Cache（Intel DDIO Outbound Read Hit / ARM CHI Snoop 机制）。
2. **微块流水线 (Micro-chunking Pipelining)**：由于 L3 留给 I/O 的 DDIO 配额有限（如 32MB~512MB），软件必须采用 **16MB~32MB 的流动窗口（Micro-Chunking）**——SSD 每写完 16MB，RNIC 立刻读走 16MB，保证数据始终在 L3 配额内循环，绝不溢出到 DRAM。
3. **时域局部性 (Temporal Locality)**：SSD 写入与 RNIC 读出的时间差必须控制在微秒级，防止被 CPU 其他线程强行 Evict。

---

## 三、 KV-Cache 落盘场景：Cache Stashing 访存放大倍数定量对比

### 3.1 理论推导模型与参数定义

为精准量化，基于第一性原理定义物理模型参数：
* **网络传输数据量**：设网络接收到的原始数据量为 $P_c$（若开启压缩，则为压缩数据量 $P_c$；若未压缩，则 $P_c = P_u$）。
* **解压后数据量**：设解压后的实际 KV-Cache 体积为 $P_u$。定义解压倍率为 $r = P_u / P_c$（典型 $r = 2.0$，即 2:1 压缩比）。
* **CRC 计算物理特性**：CRC 校验为**只读操作（Read-Only）**，生成 4 字节 Checksum 存在寄存器中，**不会写改动 Payload 自身，不产生额外的 Dirty Line 写回**。
* **DDR 访存放大倍数 ($M_{\text{DDR}}$)**：
  $$M_{\text{DDR\_Net}} = \frac{\text{Total DDR Physical Bytes Transferred}}{P_c} \quad (\text{相对于网络传输量 } P_c)$$
  $$M_{\text{DDR\_Uncompressed}} = \frac{\text{Total DDR Physical Bytes Transferred}}{P_u} \quad (\text{相对于解压后有效量 } P_u)$$

---

### 3.2 组合场景物理拆解

#### 场景 1：无 CRC + 无解压缩 (纯数据落盘, Payload = $P$)
* **关闭 Cache Stashing (Direct-to-DRAM)**：
  1. RNIC DMA Write 到 DRAM：$1 \times P$ (写)
  2. NVMe DMA Read 从 DRAM：$1 \times P$ (读)
  * **总 DDR 流量 = $2P$ (1 写 + 1 读)**。放大倍数 = **$2.0\times$**。
* **开启 Cache Stashing (Hot L3 命中)**：
  1. RNIC DMA Write 进 L3：$0$ (DRAM 写)
  2. NVMe DMA Read 命中 L3：$0$ (DRAM 读)
  3. L3 释放后 Dirty Line 延迟写回 DRAM：$1 \times P$ (写)
  * **总 DDR 流量 = $1P$ (1 写 + 0 读)**。放大倍数 = **$1.0\times$** (节省 **50%** 带宽)。
* **开启 Cache Stashing (Cold L3 / 爆仓溢出)**：
  1. L3 爆仓提前 Evict 到 DRAM：$1 \times P$ (写)
  2. NVMe DMA Read 打到 DRAM：$1 \times P$ (读)
  * **总 DDR 流量 = $2P$ (退化回 $2.0\times$)**。

#### 场景 2：有 CRC + 无解压缩 (校验只读, Payload = $P$)
* **关闭 Cache Stashing (Direct-to-DRAM)**：
  1. RNIC DMA Write 到 DRAM：$1 \times P$ (写)
  2. CPU Load 读 DRAM 算 CRC：$1 \times P$ (读)
  3. NVMe DMA Read 从 DRAM：$1 \times P$ (读)
  * **总 DDR 流量 = $3P$ (1 写 + 2 读)**。放大倍数 = **$3.0\times$**。
* **开启 Cache Stashing (Hot L3 命中)**：
  1. RNIC DMA Write 进 L3：$0$ (DRAM 写)
  2. CPU Load 算 CRC 命中 L3：$0$ (DRAM 读)
  3. NVMe DMA Read 命中 L3：$0$ (DRAM 读)
  4. L3 释放后 Dirty Line 延迟写回 DRAM：$1 \times P$ (写)
  * **总 DDR 流量 = $1P$ (1 写 + 0 读)**。放大倍数 = **$1.0\times$** (节省 **66.7%** 带宽)。
* **开启 Cache Stashing (Cold L3 / 爆仓溢出)**：
  1. L3 爆仓提前 Evict 到 DRAM：$1 \times P$ (写)
  2. CPU 读 DRAM 算 CRC：$1 \times P$ (读)
  3. NVMe 读 DRAM：$1 \times P$ (读)
  * **总 DDR 流量 = $3P$ (退化回 $3.0\times$)**。

#### 场景 3：无 CRC + 有解压缩 (网络收 $P_c$, 解压出 $P_u$, 设 $r = P_u/P_c = 2$)
* **关闭 Cache Stashing (Direct-to-DRAM)**：
  1. RNIC DMA Write $P_c$ 到 DRAM：$1 \times P_c$ (写)
  2. CPU Read $P_c$ 从 DRAM 进行解压：$1 \times P_c$ (读)
  3. CPU Write 解压后的 $P_u$ 到 DRAM：$1 \times P_u$ (写)
  4. NVMe DMA Read $P_u$ 从 DRAM 落盘：$1 \times P_u$ (读)
  * **总 DDR 流量 = $2P_c + 2P_u = 2P_c + 4P_c = 6P_c$** (若 $r=2$)。
  * **相对网络 $P_c$ 放大倍数 = $6.0\times$**；相对有效量 $P_u$ 放大倍数 = **$3.0\times$**。
* **开启 Cache Stashing (Hot L3 命中)**：
  1. RNIC DMA Write $P_c$ 进 L3：$0$ (DRAM 写)
  2. CPU Read $P_c$ 命中 L3：$0$ (DRAM 读)
  3. CPU Write $P_u$ 进 L3：$0$ (DRAM 写)
  4. NVMe DMA Read $P_u$ 命中 L3：$0$ (DRAM 读)
  5. 最终 $P_u$ 被释放时 Dirty Line 延迟写回 DRAM：$1 \times P_u$ (写)
  * **总 DDR 流量 = $1 \times P_u = 2P_c$** (若 $r=2$)。
  * **相对网络 $P_c$ 放大倍数 = $2.0\times$** (节省 **66.7%** 带宽)；相对有效量 $P_u$ 放大倍数 = **$1.0\times$**。

#### 场景 4：有 CRC + 有解压缩 (完整流水线, 设解压在 CPU L1/L2 寄存器单次 Pass 完成)
* **关闭 Cache Stashing (Direct-to-DRAM)**：
  1. RNIC DMA Write $P_c$ 到 DRAM：$1 \times P_c$ (写)
  2. CPU Read $P_c$ 从 DRAM (算 CRC + 解压)：$1 \times P_c$ (读)
  3. CPU Write $P_u$ 到 DRAM：$1 \times P_u$ (写)
  4. NVMe DMA Read $P_u$ 从 DRAM：$1 \times P_u$ (读)
  * **总 DDR 流量 = $2P_c + 2P_u = 6P_c$** (若 $r=2$)。放大倍数相对 $P_c$ = **$6.0\times$**。
* **开启 Cache Stashing (Hot L3 命中)**：
  1. 所有人均在 L3 完成读写：$0$ (DRAM 读写)
  2. 最终 $P_u$ 延迟写回 DRAM：$1 \times P_u = 2P_c$ (写)
  * **总 DDR 流量 = $1 \times P_u = 2P_c$** (若 $r=2$)。放大倍数相对 $P_c$ = **$2.0\times$** (相对 $P_u$ 为 **$1.0\times$**)。

---

### 3.6 详细计算数据对比汇总表

假设网络传入物理 Payload $P_c = 1.0\text{ GB}$，解压倍率 $r = 2.0$（即解压后 $P_u = 2.0\text{ GB}$）：

| 功能组合场景 | Cache Stashing 状态 | DDR 物理写 (GB) | DDR 物理读 (GB) | DDR 总流量 (GB) | 相对网络 $P_c$ 放大倍数 | 相对解压后 $P_u$ 放大倍数 | DDR 带宽节省率 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **场景 1：无 CRC / 无解压** | 关闭 Stash (Direct-DRAM) | $1.0$ | $1.0$ | **$2.0\text{ GB}$** | **$2.0\times$** | $2.0\times$ | 基准 |
| | **开启 Stash (Hot L3 命中)** | **$1.0$** | **$0.0$** | **$1.0\text{ GB}$** | **$1.0\times$** | **$1.0\times$** | **节省 50.0%** |
| | 开启 Stash (Cold L3 溢出) | $1.0$ | $1.0$ | $2.0\text{ GB}$ | $2.0\times$ | $2.0\times$ | 0% (退化) |
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
4. **Cache 级联与一致性控制器**：CPU 必须支持 **AMBA 5 CHI Stash 事务**（Arm 阵列）或 **Extended DDIO / SDCI 逻辑**（x86 阵列），能够在硬件层维护 `Modified/Exclusive` 状态转换，并在空间紧张时提供平滑的 Eviction 退化机制。

### 4.2 固件与操作系统层依赖 (Firmware & OS Layer)
1. **ACPI 表格与固件支持**：BIOS/固件必须向 OS 暴露 **ACPI `_DSM` (Device Specific Method)** 表格，声明 PCIe 槽位与 CPU 核心/NUMA 节点的 Steering Tag 物理映射关系。
2. **Linux 内核驱动**：操作系统内核必须开启 `CONFIG_PCIE_TPH` 内核编译选项。内核的 PCIe 子系统解析 ACPI `_DSM`，向设备驱动程序暴露 `pci_enable_tph()` API。
3. **中断向量绑定 (IRQ Affinity)**：网卡的 MSI-X 中断向量必须与 CPU Core 建立硬绑定（如 `set_irq_affinity`），确保网卡填写的 Steering Tag 与实际响应中断/轮询的 CPU Core 编号一致。

### 4.3 应用与框架层依赖 (Application & Framework Layer)
1. **轻量级事件循环 (Lean Event Loop)**：用户态驱动（如 **DPDK PMD** 或 **SPDK Polling**）必须保持极简的指令脚印（Instruction Footprint），避免加载庞大的代码段而将 Stash 进来的描述符冲刷掉。
2. **头尾分离策略 (Header-Data Split)**：驱动程序或硬件 RSS 引擎必须将数据包拆分为 **描述符/报文头（64~256 B，注入 L2/L3）** 与 **大块 Payload（直接入 DRAM/HBM）**，防止 MB 级的 Payload 导致 L2 Cache 发生惨烈的 Cache Thrashing。

---

## 五、 各大厂商 Cache Stashing 特性与软硬件布局矩阵

以下汇总 Intel、AMD、Arm 生态（AWS/Ampere/阿里）及 NVIDIA 在 Cache Stashing 方向上的技术布局与发布年份：

| 厂商 | 代表性 CPU / 芯片产品 | 硬件特性与协议名称 | 目标 Cache 层级 | 软件生态与驱动支持 | 发布年份 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Intel** | **CPU**: Xeon Scalable 1st~3rd Gen (Skylake/Cascade/Ice Lake)<br>**新 CPU**: Xeon 4th~6th Gen (Sapphire/Emerald/Granite Rapids, Sierra Forest)<br>**NIC/IPU**: Intel E810, IPU E2000 | **Intel DDIO** (早期)<br><br>**Extended DDIO**<br>+ PCIe TPH Cache Steering | 早期: 共享 **LLC 20% Way**<br><br>近期: 定向 **LLC Slice** 甚至 **Core L2 Cache** | • **Kernel**: `CONFIG_PCIE_TPH` 驱动、ACPI `_DSM` 映射<br>• **框架**: DPDK PMD 驱动、SPDK NVMe 队列轮询<br>• **SDK**: Intel IPU SDK | 2012 (DDIO)<br><br>2023 (Ext-DDIO) |
| **AMD** | **CPU**: EPYC 9004 (Genoa/Bergamo), EPYC 9005 (Turin)<br>**DPU**: Pensando Salina / Pollara | **AMD SDCI**<br>(Smart Data Cache Injection)<br>+ PCIe TPH Steering Tag | 目标 CCX 的 **私有 L2 Cache** (1MB~2MB) | • **Kernel**: 原生 ACPI TPH Steering Tag 解析<br>• **网卡绑定**: Broadcom BNXT / Mellanox NIC + DPDK/XDP<br>• **存储**: SPDK NVMe-oF CQE 直接注入 | 2022 |
| **Arm 生态**<br>*(AWS / Ampere / 阿里)* | **IP/CPU**: Neoverse N1/N2/V1/V2/V3, DSU-110/120<br>**芯片**: AWS Graviton3/4, AmpereOne, 阿里倚天 710 | **AMBA 5 CHI** Cache Stashing 协议 (`StashLPID`, `StashOnceUnique`) | 核心 **私有 L2 Cache** 或 **Cluster L3** | • **总线**: AMBA CHI 内核架构驱动<br>• **云端**: AWS Nitro 硬件调度栈与 Guest OS 映射<br>• **数据面**: DPDK / SPDK 适配 ARM64 AMBA CHI 报文 | 2016 (协议)<br>2021 (Graviton3)<br>2023 (Graviton4) |
| **NVIDIA** | **CPU**: Grace CPU (GH200 / GB200)<br>**DPU/NIC**: BlueField-3 DPU, ConnectX-7 / ConnectX-8 | **NVLink-C2C CHI Stash**<br>+ PCIe TPH Cache Steering | Grace 核心 **私有 L2 Cache** (1MB) 与 System Cache | • **驱动**: NVIDIA Grace SoC 驱动栈<br>• **软件栈**: DOCA SDK 异步事件通知<br>• **AI 框架**: CUDA / Triton / vLLM CPU-GPU 任务队列极速握手 | 2023 (GH200)<br>2024 (GB200) |

---

## 六、 各大厂商 DPU 与 SSD 直通（P2P DMA）特性与软硬件布局矩阵

以下汇总各大厂商在面向大块存储落盘与换出的 **DPU-to-SSD 直通（Peer-to-Peer DMA）** 方案上的布局与发布年份：

| 厂商 | 代表性 DPU / 加速器与存储硬件 | 直通协议与传输架构 | 硬件硬件硬化引擎 (Inline Offload Engines) | 软件生态与存储框架 | 发布年份 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **NVIDIA** | **DPU**: BlueField-2 / BlueField-3<br>**NIC**: ConnectX-6 / CX-7 / CX-8<br>**平台**: Grace Hopper / Blackwell | **GPUDirect Storage (GDS)**<br>+ PCIe P2PDMA<br>+ NVMe CMB / PMR | • **解压缩**: 硬件线速 LZ4 / Deflate Engine<br>• **校验**: 硬件 CRC32 / CRC64 Engine<br>• **安全**: AES-XTS 硬件加解密 | • **存储 SDK**: NVIDIA DOCA Storage Stack<br>• **存储框架**: SPDK GDS Plugin, NVMe-oF Target<br>• **AI 集成**: TensorRT-LLM / vLLM KV Cache Swap 插件 | 2020 (BF-2)<br>2023 (BF-3) |
| **AMD** | **DPU**: Pensando Salina / Pollara DPU<br>**SSD**: Alveo SmartSSD (FPGA CSD)<br>**平台**: EPYC + Pensando Helios | **PCIe P2PDMA**<br>+ NVMe CMB/PMR<br>+ CXL 2.0 / 3.0 Direct | • **压缩**: 硬件流式 LZ4 / ZSTD Engine<br>• **完整性**: Pipeline 级 CRC64 计算逻辑<br>• **安全**: 硬化 Crypto Engine | • **软件套件**: AMD Pensando Software Suite<br>• **内核**: Linux Kernel `p2pdma` / `p2pmem` 模块<br>• **存储框架**: 开源 SPDK P2P DMA Driver | 2022 (收购)<br>2024 (Pollara) |
| **Intel** | **IPU**: Mount Evans (IPU E2000)<br>**加速器**: QAT (QuickAssist) / DSA<br>**平台**: Xeon Scalable Platform | **PCIe P2PDMA**<br>+ NVMe CMB/PMR<br>+ CXL 内存/存储直通 | • **QAT Engine**: 硬件 Deflate / LZ4 压缩解压<br>• **DSA Engine**: 高速数据搬运与 CRC32C 校验<br>• **IPU**: 流式硬件数据包过滤与校验 | • **驱动栈**: Intel IPU SDK, QAT Engine Driver<br>• **存储框架**: SPDK P2PDMA Plugin<br>• **生态**: Linux Kernel NVMe target 硬件卸载 | 2021 (Mount Evans)<br>2023 (DSA/QAT) |
| **Arm 生态**<br>*(AWS / Marvell)* | **AWS**: Nitro V4 / V5 / V6 Card + Nitro SSD<br>**Marvell**: OCTEON 10 DPU (Neoverse N2)<br>**Fungible**: F1 DPU (已被微软收购) | **定制 ASIC PCIe P2P 管道**<br>+ AMBA CHI P2P<br>+ NVMe 接口 | • **Nitro ASIC**: 硬件流式解压/CRC/EBS 加密<br>• **OCTEON 10**: Inline Zip / Crypto Co-processors<br>• **Fungible**: TrueFabric 处理引擎 | • **AWS 平台**: Nitro Hypervisor / EBS 存储栈 (云端闭环)<br>• **Marvell**: OCTEON SDK, DPDK/SPDK 扩展<br>• **开源**: Linux Kernel Arm64 P2PDMA | 2017 (Nitro V1)<br>2022 (Nitro V5)<br>2022 (OCTEON 10) |

---

## 七、 KV-Cache 场景技术对比：DPU-SSD 直通 vs. Cache Stashing (L2/L3)

在 LLM 推理架构（如 Disaggregated Prefill-Decode 或 Tiered KV-Cache）中，针对 KV-Cache 的传输与持久化，DPU 直通 SSD 与 Cache Stashing 代表了两种截然不同的架构设计哲学：

```
[ 架构方案 A: DPU 直通 SSD (Data Plane Bypass) ]
RNIC ──(RoCEv2)──► DPU (硬化解压+CRC) ──(PCIe P2P DMA)──► NVMe SSD
                   ▲ (全过程 Zero-Host-CPU, Zero-Host-DRAM)

[ 架构方案 B: CPU Cache Stashing (Control/Compute Plane) ]
RNIC ──(PCIe TPH)──► Core L2/L3 Cache ──► CPU 处理控制头/调度 ──► DRAM ──► SSD
                     ▲ (极速通知 CPU, 消除 Pipeline Stall)
```

### 多维对比矩阵：

| 评估维度 | DPU 与 SSD 直通 (P2P DMA) | Cache Stashing 到 L2/L3 |
| :--- | :--- | :--- |
| **物理定位** | **数据面（Data Plane）大块存储换出与落盘** | **控制面（Control Plane）事件通知与描述符处理** |
| **Host CPU 算力消耗** | **0%** (数据流完全旁路 Host CPU 核心) | **非零** (仅在 CPU 需计算 CRC/解压时占用；纯转发时 0) |
| **Host DDR 带宽占用** | **0 GB** (数据在 PCIe Switch 局部转向，不经过内存控制器) | **0.5x ~ 1.0x** (Hot L3 命中) / **2x ~ 6x** (Cold L3 溢出) |
| **端到端处理时延** | 受限于 **PCIe 总线与 DPU 硬件 Engine 线速** ($100\text{G}\sim 400\text{G}$) | 受限于 **CPU 读写 Cache 速度与软件线程处理效率** |
| **缓存污染风险 (Cache Pollution)**| **零风险** (完全不进入 CPU L2/L3 Cache) | **存在风险** (若 KV-Cache Payload 超过 LLC 配额会强行冲刷 Cache) |
| **硬件部署成本与门槛** | 较高 (需部署昂贵的 DPU/IPU 卡及支持 P2P 的 PCIe Topology) | 较低 (主流 x86/ARM CPU 自带，仅需软件和驱动开启) |
| **适用 KV-Cache 具体场景** | **KV-Cache Swap-Out / Offload 到 SSD 持久化保存** | **KV-Cache 命中判断、RPC 控制头解析、GPU 任务完成通知** |

### 第一性原理对比结论：
1. **DPU 直通 SSD 是 KV-Cache 落盘/换出的终极形态**：在 Swap-Out 场景下，CPU 不需要处理数据内容，DPU 直通 SSD 做到 **Zero-Host-CPU** 和 **Zero-Host-DRAM**，彻底解开了内存墙限制。
2. **Cache Stashing 是 CPU 实时调度的终极形态**：在 KV-Cache 查找、路由决策及 CPU-GPU 任务握手时，数据必须进入 CPU 决策，此时 DPU 无法代劳，Cache Stashing 是消除 CPU 停顿的物理极限手段。

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

### 8.1 可计算存储盘 (Computational Storage Drives, CSD / SmartSSD)
* **原理**：将 FPGA 或专用 SoC 直接嵌入到 NVMe SSD 控制器内部（代表厂商：Samsung SmartSSD、Solidigm / Xilinx CSD）。
* **优势**：RNIC 进来的数据通过 PCIe P2P 直接写入 SmartSSD，**CRC 校验与解压缩动作完全在 SSD 内部完成**，连外部 DPU 芯片都不需要经过，真正实现“存储即计算（In-Storage Computing）”。

### 8.2 CXL (Compute Express Link 2.0/3.0) 共享内存池 (CXL.mem / CXL.cache)
* **原理**：通过 CXL 协议将远端 DRAM 抽象为本地物理地址空间。
* **优势**：当 Decode 节点的 GPU 显存或 DDR 不足时，CPU/GPU 发起标准的 Memory Load/Store 指令，CXL 控制器自动将事务封装为 CXL.mem 包在 CXL Switch 中传输。**完全绕过网络 TCP/RDMA 协议栈，实现硬件级的跨节点内存借用**。

### 8.3 GPUDirect Storage (GDS) 跨节点 / 跨设备直连
* **原理**：NVIDIA GDS 技术允许 NVMe SSD 控制器与 GPU HBM 显存之间建立 **PCIe P2P DMA** 路径。
* **优势**：在 PD 分离架构中，Prefill 节点从 SSD 读取 Prefix KV-Cache 到 GPU HBM 时，数据**既不经过 Host CPU，也不经过 Host DDR**，直接在 PCIe Switch 转向，传输延迟降低 60% 以上。

### 8.4 微块流水线 (Micro-chunking) 与在线量化 (Streaming Quantization)
* **原理**：在软件层将大块 KV-Cache 细粒度切分为 **16MB ~ 32MB 的 Micro-chunks**，并结合在线 FP16 -> FP8 / INT4 动态量化。
* **优势**：
  1. 微块尺寸精准拟合 CPU L3 Cache 配额，实现 100% L3 命中，避免 Cache 抖动；
  2. 在线量化将数据体积减半，网络与 DDR 物理流量直接**降低 50%**。

---

## 九、 总结与架构演进洞察

### 9.1 Cache Stashing 的“黄金三角”约束网络

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

Cache Stashing 要想发挥极致效果，必须满足物理上的“黄金三角”约束：
1. **数据极小**：仅 Stash 描述符与 Header，坚决不 Stash 大块 Payload；
2. **逻辑极精**：代码段与查找表脚印极小，避免 I-Cache 冲刷；
3. **消费极快**：CPU 线程（如 DPDK/SPDK 绑核线程）随时待命，在几百纳秒内消费数据，防止被 LRU 淘汰。

### 9.2 缓存隔离机制 (Intel CAT / RDT) 避免 LLC 污染
为防止高频 I/O 冲垮 CPU 的计算 Cache，现代服务器必须开启 **Intel RDT/CAT (Cache Allocation Technology)** 或 **AMD Cache Quality of Service (QoS)**。
* 通过硬件 Class of Service (CLOS) 重新划定 L3 Cache：为 Stashing 划分 20% 的专用 Way，为 CPU 计算线程隔离出 80% 的 Way，在硬件层面阻断 I/O 流量对计算 Cache 的侵蚀。

### 9.3 终极收敛趋势：控制面 Lean Control + 数据面 Bypass Data
计算体系结构的演进最终收敛于一个核心哲学：
* **让 CPU 彻底退居控制平面（Lean Control Plane）**：CPU 核心不再承担大块拷贝、加解密与解压缩，其唯一使命是**利用 Cache Stashing 以纳秒级速度响应事件、解析控制头、调度任务**。
* **让硬件加速器与总线接管数据平面（Bypass Data Plane）**：海量数据 Payload 交由 **DPU、PCIe P2P DMA、CXL 及 GDS** 在底层硬件总线上穿梭，彻底打通“内存墙”，实现数据中心级的高吞吐与确定性低延迟。
