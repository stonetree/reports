# Cache Stashing 与 DPU–SSD 直通：面向 KVCache 的技术总结

> 基于目录根部的 5 份讨论稿整理，并以截至 **2026-08-01** 可复核的公开资料校正了其中与具体产品相关的表述。本文未读取子目录中的文档。  
> 讨论稿中的 “IO stash” 容易与 IBM 的同名存储缓存产品混淆；本文的 **cache stashing** 专指把设备 DMA 的近期消费者数据定向留在 CPU 缓存层次（通常是 LLC/L3，少数平台可到 L2）的机制。

## 结论先行

1. **Cache stashing 不是“把大数据永久放进缓存”，也不是 DPU–SSD 直通的替代品。** 它解决的是“设备写入后，CPU 或另一台 DMA 设备很快要消费”的短暂交接问题。L2 最适合 CQE、描述符、页表项、校验头等小对象；L3/LLC 可在严格配额、分块和亲和性约束下承载短时的 KVCache 微批数据。
2. 对 CPU 参与的 RNIC/DPU → SSD 落盘，按本文 1 GiB 示例，**仅 CRC** 可从常规 DDR 路径的 **3.0×**（相对最终数据）降至理想 L3 stash 的 **1.0×**；输入经 **2:1 解压缩** 时，常规路径为 **3.0×**，理想 stash 为 **1.5×**。这里的 1.0×/1.5× 是正常可一致访问主机缓冲区最终回写 DDR 的下界，不能误称为“总 DDR 为 0”。
3. 真正的 RNIC/DPU ↔ SSD PCIe P2P 在**拓扑、IOMMU/ACS、驱动、DMA 映射和顺序语义**均满足时，可以把该数据路径的主机 DDR 流量降为 0；但数据仍会经过 PCIe、DPU 片上 SRAM/DDR 或设备缓冲区，且主机/DPU 仍需处理控制面。公开资料没有证明存在一个跨厂商、开箱即用、通用的“RNIC 收包 + CRC/解压 + 直写 NVMe”的标准成品管线。
4. 选型原则：**小而急、CPU 即刻消费的数据走 stash；大而直通、CPU 不看载荷的数据优先验证 P2P；大数据但 CPU 必须做转换时，采用 L3 微分块 + 硬件 CRC/压缩/复制卸载。**

---

## 1. 范围、术语与判定边界

### 1.1 输入材料与处理方式

本文以以下根目录文件为讨论基础：

- IO stash技术分析.md
- IO.md
- stash crc on off ddr write and read.md
- RDMA 机制讨论.md
- RDMA 机制讨论 补充稿.md

后三份 RDMA 讨论稿有大量重复段落。本文保留其共同的 RDMA 注册、QP/CQ、DMA、P2P 与 KVCache 场景分析，并对供应商能力、缓存层级和“零 DDR”等结论补上了平台条件。文中带链接的产品事实来自厂商、PCI-SIG、Linux 或 SPDK 一手资料；未有公开规格支撑的结论均标为“需实测”。

### 1.2 关键术语

| 术语 | 本文含义 | 不应误解为 |
|---|---|---|
| Cache stashing | 让设备 I/O 数据优先落入、或以提示方式导向某一缓存层，缩短设备—CPU/设备消费者交接 | 通用大容量缓存、持久化存储 |
| DDIO | Intel 平台使 I/O 入站写及设备读优先与 LLC 交互的机制 | 可直接、必然写入任意 CPU 核的 L2 |
| TPH | PCIe Transaction Processing Hints；设备携带 steering tag 的**提示** | 命令、协议保证或自动生效的 L2 写入 |
| SDCI | AMD EPYC 9005 相关平台的 Scalable Data Cache Injection；利用 TPH 将特定入站 I/O 导向目标核 L2 | 所有 AMD CPU、所有 NIC 的默认能力 |
| P2P / P2PDMA | 一个 PCIe Endpoint 的 DMA 直接访问另一 Endpoint 可映射内存/窗口 | 任意两设备、任意 PCIe 根复合体之间都可直通 |
| CRC | 本文主要指 KVCache/应用数据完整性校验 | Ethernet FCS、RoCE ICRC、NVMe 端到端保护的同义词 |
| DDR 放大 | 主机 DDR 控制器上由数据面导致的逻辑读写字节总量 ÷ 选定基准字节数 | PCIe、NAND、DPU 本地内存或缓存内部流量为零 |

**CRC 边界很重要。** RNIC 已会处理以太网/RoCE 的链路与传输完整性；本文“开启 CRC”表示应用还要对 KVCache 载荷做一次校验（例如对象校验、分块校验或写入前校验）。二者不能互相替代。

---

## 2. Cache stashing 的发展过程：从“缓存感知 DMA”到“定向、卸载与直通”

| 阶段 / 首次公开年份 | 代表技术 | 核心变化 | 对 KVCache 的现实意义 |
|---|---|---|---|
| 早期缓存感知 I/O（2000 年代） | Intel DCA/I/O 加速类机制 | 让 I/O 与 CPU cache coherence 更紧密，目标是降低设备到 CPU 的首访延迟 | 奠定“设备生产、CPU 很快消费”这一问题定义，但不等于现代通用定向 stash |
| **2008** | PCIe TPH ECN | 为 TLP 增加 processing hint 和 8-bit steering tag，标准化“可由平台解释的定向提示” | 为 RNIC/NIC 定向队列提供共同语言；它是可选能力、不是保证 |
| **2012** | Intel DDIO | Xeon E5 起将 I/O 的主要落点/来源扩展到 LLC | 最成熟的 L3 交接基础：NIC 写入、CPU 读和后续设备读可能在 LLC 命中 |
| 2010 年代中后期 | Arm AMBA 5 CHI cache stashing | 互连事务可携带 StashNID/StashLPID，目标由 SoC 实现定义 | 片上 SoC 可将网络/存储控制面更靠近目标 CPU；是否实现、落在哪级 cache 取决于芯片 |
| **2018** | Linux PCI P2PDMA | Linux 开始提供 peer-memory / P2PDMA 框架 | 发展方向从“借 LLC 交接”扩展到“完全绕过主机 DDR” |
| **2020** | NVIDIA GPUDirect Storage | 把 GPU 内存与本地 NVMe / 远端 NIC 的直接 DMA 形成软件栈 | 证明存储数据面可不经 CPU bounce buffer；其主体是 GPU–存储，不是通用 DPU–SSD |
| **2023** | Intel 4th Gen Xeon DSA/QAT/IAA | 把 copy、CRC、压缩/解压等转换更多交给片上加速器 | CPU 必须参与的路径可减少核开销，但不自动成为 Endpoint–Endpoint P2P |
| **2024–2026** | AMD EPYC 9005 SDCI 与软件支持 | 在特定 EPYC + NIC + 驱动组合中，TPH 用于将入站 I/O 引入目标核 L2 | 是“定向 L2”有公开产品证据的案例，特别适合包头、CQE、事件和小批量数据 |

这条演进线不是单一路线，而是三条互补路线：

1. **缓存路线**：DDR 旁路/LLC 暂存/定向 L2，降低近期消费者的 DDR 往返。
2. **转换路线**：用 CPU 向量指令、DSA、QAT、DPU 或 SmartSSD 做 CRC、复制、压缩/解压。
3. **拓扑路线**：用 PCIe P2P 让两个设备直接 DMA，取消主机 DDR 中转。

Intel 对 DDIO 的定义明确包含 I/O 入站读写与 LLC 的交互；其性能文档也明确指出，脏 LLC 行被逐出时仍会写回内存。[Intel DDIO Primer（2012）](https://www.intel.com/content/dam/www/public/us/en/documents/technology-briefs/data-direct-i-o-technology-brief.pdf) [Intel DDIO 性能分析](https://www.intel.com/content/www/us/en/developer/articles/technical/ddio-analysis-performance-monitoring.html)  
TPH 是 PCIe 可选能力，Linux 也要求驱动显式启用、获取 CPU tag 并将 tag 编程到设备。[PCI-SIG TPH ECN（2008）](https://pcisig.com/PCIExpress/ECN/Base/TLPProcessingHints) [Linux PCI TPH 文档](https://docs.kernel.org/PCI/tph.html)

---

## 3. 两个方向的完整处理流程

### 3.1 RNIC/DPU → SSD：接收 KVCache 后落盘

~~~mermaid
flowchart LR
    A["远端发送方"] --> B["RNIC / DPU 收到 RDMA 数据"]
    B --> C{"数据面落点"}
    C -->|常规| D["主机 DDR 接收缓冲"]
    C -->|Cache stash| E["目标 L3/LLC；小控制对象可到 L2"]
    C -->|真 P2P| F["DPU/RNIC 可映射 peer buffer"]
    D --> G["CPU 或加速器：CRC、解压、重排"]
    E --> G
    G --> H["NVMe SQ / doorbell；SSD 按 PRP/SGL DMA 取数"]
    F --> I["DPU 提交 NVMe；SSD peer DMA / 流式管线"]
    H --> J["NVMe 完成；写入已提交"]
    I --> J
~~~

建议把流程拆成**控制面**与**载荷数据面**，否则很容易把“RDMA 已完成”误认为“SSD 已持久化”：

1. **资源建立（控制面）**：主机/DPU 分配接收区域，注册 MR，获得 IOVA 与 rkey；建立 QP、CQ、RSS/IRQ/轮询核亲和性。存储侧准备 NVMe 命令、PRP/SGL 与队列空间。P2P 时还要建立 peer-memory 映射和 DMA 地址可达性。
2. **RDMA 接收与网络完整性**：RNIC 校验协议层完整性，并按远端 WQE 中的地址/rkey DMA。此时它写的是接收端已注册的目标，而不是“自动写 SSD”。
3. **选择落点**：
   - **常规路径**：RNIC DMA 写主机 DDR；CPU 后续从 DDR 读。
   - **L3 stash 路径**：入站 I/O 写分配进 LLC；CPU 紧接着读，可命中 LLC。若平台允许且行仍在 LLC，SSD 后续 DMA 读也可能命中 LLC。
   - **L2 stash 路径**：仅在平台、NIC、驱动和核亲和性共同支持时用作小对象快速交接；不可把 GiB 级 payload 当作 L2 常驻数据。
   - **P2P 路径**：DPU/RNIC 的 peer buffer、NVMe 控制器和 PCIe 路由均满足要求时，数据不进入主机 DDR。
4. **可选转换**：应用 CRC 可以和解压同一次流式读取融合；若拆成独立 CRC pass，就会多一次消费机会和更高的缓存失效风险。解压后的输出必须有一个对 SSD 可 DMA 的目标位置，可在 LLC 微分块中暂存，或在 DPU/加速器内流式产生。
5. **提交并取数**：写入 NVMe SQ 并按顺序 doorbell 后，SSD 控制器根据 PRP/SGL 发起 DMA 读取。常规路径读 DDR；stash 路径只有在 DMA coherent、目标行未被逐出且平台允许 I/O 读 LLC 时才可免去这次 DDR 读；P2P 路径则读 peer aperture。
6. **提交语义与完成**：NVMe CQ completion 才代表控制器已完成该命令定义的阶段（还需按 FUA、flush、断电保护策略定义“可恢复”）。**RDMA Write 的完成/ACK 只说明 RNIC 侧内存写已完成到相应语义，不等价于 SSD 已持久化。** 要向远端宣布“KVCache 可恢复”，必须把 NVMe completion/flush 纳入应用提交协议。

### 3.2 SSD → RNIC/DPU：从落盘 KVCache 回传

~~~mermaid
flowchart LR
    A["NVMe 命令 / PRP-SGL"] --> B["SSD 控制器从 NAND 取数"]
    B --> C{"SSD DMA 目标"}
    C -->|常规| D["主机 DDR 缓冲"]
    C -->|Cache stash| E["LLC 短时缓冲"]
    C -->|真 P2P| F["RNIC/DPU peer 可访问窗口"]
    D --> G["RNIC DMA 读本地源"]
    E --> G
    F --> H["RNIC/DPU 读取 peer 数据并发起 RDMA"]
    G --> I["网络发送"]
    H --> I
~~~

1. 应用先获得远端地址/rkey，准备本地 NVMe 读命令及目标 SGL。
2. SSD 从 NAND 取数并 DMA 到主机 DDR、LLC 暂存区或受支持的 peer-memory 区域。
3. **必须以 NVMe completion 和 DMA 可见性作为启动 RNIC 发送的前提**；仅看到 CPU 写了 doorbell 并不代表 payload 已对 RNIC 可读。DPU 流水线需要明确 fence、DMA completion 或硬件事件。
4. RNIC 以本地缓冲为源发起 RDMA Write，或响应远端 RDMA Read。若读源仍在 LLC 且平台支持 I/O 读 LLC，则可避免 DDR 读；未命中时仍要从 DDR 取数。
5. P2P 的反方向通常更难：SSD 需要可写的 peer aperture，RNIC/DPU 需要可读的 peer DMA 映射，驱动还要正确表达所有权和完成顺序。NVMe CMB/PMR 是控制器暴露的一类内存窗口，**不是“所有 NAND 数据天然可直连”的保证**。

Linux 和 SPDK 都把 P2P 看成拓扑敏感能力：同一 PCIe Switch 下通常最理想；跨 Root Port/层级可能被内核拒绝、被 ACS 改道，或性能显著下降。SPDK 目前也将其 peer-to-peer 方案标为实验性。[Linux PCI P2PDMA](https://www.kernel.org/doc/html/latest/driver-api/pci/p2pdma.html) [SPDK Peer-to-Peer](https://spdk.io/doc/peer_2_peer.html)

---

## 4. KVCache 落盘的 DDR 放大模型与详细计算

### 4.1 计算口径

为避免“cache hit 就等于没有 DDR”这种误读，使用以下保守、可审计的口径：

- **U**：最终写入 SSD 的未压缩 KVCache 大小；示例取 **1 GiB**。
- **P**：RNIC/DPU 收到的线上数据大小；r=P/U。未压缩时 r=1；本例“开启解压”假设 **2:1 压缩**，即 P=0.5 GiB、r=0.5。
- DDR 总流量 = 主机 DDR 写 + 主机 DDR 读；不包含 PCIe、NAND、CPU cache 内部、DPU 本地 DDR/SRAM 与控制描述符。
- “L3 stash 理想”要求：入站数据写入 LLC、所有即时 CPU/SSD 消费都命中 LLC，且正常一致性主机缓冲区最终只发生一次必要的脏数据回写。它不是无限容量缓存。
- 若路径采用 DPU/SSD 真 P2P 且无 host bounce buffer，则本节定义的**主机 DDR**数据流量可为 0；这不代表 DPU 内存、PCIe 或设备内部流量为 0。

### 4.2 场景逐项计算（U=1 GiB）

| 场景 | 线上 P | 常规 DDR 写入 | 常规 DDR 读取 | 常规总量 | 常规放大（/P） | 常规放大（/U） | 理想 L3 stash 写入 | 理想 L3 stash 读取 | 理想总量 | stash 放大（/P） | stash 放大（/U） | DDR 降幅 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CRC 关；解压关 | 1.0 | RNIC 写 1.0 | SSD 读 1.0 | **2.0** | 2.0× | 2.0× | 最终回写 1.0 | 0 | **1.0** | 1.0× | 1.0× | 50.0% |
| CRC 开；解压关 | 1.0 | RNIC 写 1.0 | CPU CRC 读 1.0 + SSD 读 1.0 | **3.0** | 3.0× | 3.0× | 最终回写 1.0 | 0 | **1.0** | 1.0× | 1.0× | 66.7% |
| CRC 关；解压开 | 0.5 | RNIC 写 0.5 + 解压输出写 1.0 | 解压输入读 0.5 + SSD 读 1.0 | **3.0** | 6.0× | 3.0× | 输入/输出最终回写 1.5 | 0 | **1.5** | 3.0× | 1.5× | 50.0% |
| CRC 开（与解压融合）；解压开 | 0.5 | 同上：0.5 + 1.0 | 解压流中同时 CRC 读 0.5 + SSD 读 1.0 | **3.0** | 6.0× | 3.0× | 输入/输出最终回写 1.5 | 0 | **1.5** | 3.0× | 1.5× | 50.0% |
| CRC 开（独立入站 pass）；解压开 | 0.5 | 0.5 + 1.0 | CRC 读 0.5 + 解压读 0.5 + SSD 读 1.0 | **3.5** | 7.0× | 3.5× | 输入/输出最终回写 1.5 | 0 | **1.5** | 3.0× | 1.5× | 57.1% |
| CRC 开（独立输出 pass）；解压开 | 0.5 | 0.5 + 1.0 | 解压读 0.5 + 输出 CRC 读 1.0 + SSD 读 1.0 | **4.0** | 8.0× | 4.0× | 输入/输出最终回写 1.5 | 0 | **1.5** | 3.0× | 1.5× | 62.5% |

表中“CRC 与解压融合”是关键工程点：解压器在读入压缩块、生成输出块的同一流中累计 CRC，就不应再增加独立的主机 DDR 读取 pass。若 CRC 覆盖的是解压后输出，也可在产生输出时累计；必须由协议明确 CRC 覆盖对象。

### 4.3 缓存命中并非二元：80% 命中时的回退

令 **h** 表示每一次“本可由 LLC 服务的后续消费者读取”仍命中 LLC 的比例。为显示敏感性，下面以所有此类读取取同一 h；实际项目应分别记录 CPU CRC、解压输入和 SSD DMA 读取的命中率。

| 场景 | h=100% 理想 stash | h=80% | h=0%（完全退化） | 常规基线 | 说明 |
|---|---:|---:|---:|---:|---|
| CRC 关；解压关 | 1.0 GiB | 1.2 GiB | 2.0 GiB | 2.0 GiB | 只有 SSD 读取会因逐出回到 DDR |
| CRC 开；解压关 | 1.0 GiB | 1.4 GiB | 3.0 GiB | 3.0 GiB | CRC 和 SSD 各有一次潜在回退读取 |
| 解压开；CRC 关或融合 | 1.5 GiB | 1.8 GiB | 3.0 GiB | 3.0 GiB | 输入供解压、输出供 SSD 两次交接 |
| 独立入站 CRC + 解压 | 1.5 GiB | 1.9 GiB | 3.5 GiB | 3.5 GiB | 多出一次压缩输入 CRC 的消费者 |
| 独立输出 CRC + 解压 | 1.5 GiB | 2.0 GiB | 4.0 GiB | 4.0 GiB | 解压输出被 CRC、SSD 两次消费 |

因此，stash 的收益来自**时间局部性**，不是开关本身。以“解压 + 融合 CRC”为例：

**DDR_stash = P + U + (1-h_in) × P + (1-h_out) × U**

其中 P+U 是正确性/一致性下最终必须落入内存的输入和输出脏数据量；后两项才是因缓存逐出产生的额外 DDR 读取。若数据只是在 LLC 中经过而从不需要主机可恢复内存副本，且平台允许特殊非一致性/设备私有策略，口径会不同；那是另一种内存所有权模型，不能与一般注册主机 MR 混为一谈。

### 4.4 L3 分块上限与 L2 使用边界

对解压路径，输入与输出需要同时短时存在。若给 I/O 数据的 LLC 配额为 C_IO，安全系数为 s（建议先取 0.5–0.75），使用 q 组 ping-pong 缓冲，则未压缩块上限可按下式起步：

**U_chunk ≤ (C_IO × s) / [q × (1+r)]**

例如 C_IO=64 MiB、s=0.75、q=2、r=0.5 时，U_chunk ≤ 16 MiB。这只是容量上界，还必须满足块在被替换前已被 CRC/解压/SSD DMA 消费。  
**L2 不适合放 16 MiB 的有效载荷。** L2 只应承载 RX/TX 描述符、CQE、块索引、长度、校验状态和门铃相关对象；大 payload 的目标是受控 LLC 窗口，或者 P2P。

---

## 5. Cache stashing 的完整软硬件依赖

| 层次 | L3/LLC stash 需要什么 | 目标 L2 stash 额外需要什么 | DPU–SSD 真 P2P 额外需要什么 | 验证重点 |
|---|---|---|---|---|
| CPU / Root Complex | 支持 coherent I/O 与可用 LLC；可配置 DDIO/IO LLC ways 的平台 | 目标核可接受定向 I/O 的架构实现 | 支持两 Endpoint 之间允许的 DMA 路径 | DDR 读写计数、LLC 占用/逐出、NUMA |
| NIC / RNIC | 正确 DMA 属性、队列与 CPU 亲和性 | NIC 支持 TPH，驱动可写入 steering tag | NIC/DPU 有可导出的 peer-memory 或相应 DMA 能力 | PCIe capability、TPH enable、队列/核绑定 |
| DPU / 加速器 | 可承接 CRC、解压、队列处理，避免 CPU 多 pass | 对控制对象可配合队列亲和性 | 支持 peer mapping、DMA fence、缓冲所有权与恢复 | DPU local DDR/SRAM 带宽、completion 链路 |
| SSD / NVMe | NVMe 控制器可 coherent DMA 读取主机缓冲 | 无特有要求 | 驱动/控制器能对 peer memory 建 SGL/映射；必要时 CMB/PMR | 是否真的走 peer 事务，而非隐式 bounce |
| PCIe 拓扑 | NIC、CPU、SSD 尽量同 NUMA 域 | 目标 CPU 与接收队列同 CCD/核心簇 | 同一 PCIe Switch 最佳；跨 Root Complex 通常高风险 | lspci 拓扑、Switch、ACS、链路宽度/代际 |
| BIOS / 固件 | DDIO、IOMMU、NUMA、LLC way 配置正确 | TPH/SDCI、ACPI _DSM 等平台支持 | ACS 策略与 IOMMU 允许相应 peer DMA，同时符合隔离要求 | BIOS 设置、IOMMU domain、ACS capability/control |
| Linux / 驱动 | NIC、NVMe、RDMA 驱动及中断/轮询亲和性 | CONFIG_PCIE_TPH；驱动启用 TPH、配置 tag | pci_p2pdma / peer-memory、NVMe/RDMA/DPU 驱动支持 | 内核是否拒绝 P2P、DMA map 是否 fallback |
| 数据面软件 | RDMA verbs、MR/rkey、NVMe PRP/SGL；DPDK/SPDK 可选 | RSS/flow steering、poller 与消费者核固定 | SPDK P2P 或厂商 SDK，O_DIRECT/绕过页缓存的缓冲策略 | 真实 DMA 地址、copy 次数、CQ 轮询延迟 |
| 转换与完整性 | CRC 与解压合并为单 pass；定义 CRC 覆盖范围 | 小对象在同核即时消费 | DPU/硬件引擎的流式 CRC/解压、错误上报与回退 | CRC 正确性、错误注入、吞吐和 P99 |
| 可观测性 | 监控 LLC hit/miss、IO LLC way、DDR MC read/write | per-core L2、同 CCD 命中和软中断迁移 | PCIe P2P 事务、DPU 内存和隐式 host copy | 对照实验：stash on/off、P2P on/off、强制回退 |

TPH 在 Linux 中不是“插上 NIC 即可用”：内核文档要求 PCIe capability、内核配置、平台 ACPI 信息、驱动启用和设备 tag 编程共同成立。[Linux PCI TPH 文档](https://docs.kernel.org/PCI/tph.html)  
AMD 对 SDCI 也明确限定于 EPYC 9005、支持的 NIC/驱动与快速同核消费场景。[AMD SDCI White Paper（2025）](https://www.amd.com/content/dam/amd/en/documents/epyc-technical-docs/white-papers/58725.pdf) [AMD Onload SDCI 指南](https://docs.amd.com/r/en-US/ug1586-onload-user/SDCI)

---

## 6. 各厂商的 cache stashing 软硬件布局（可公开核实部分）

> “发布年份”指该机制或该代平台首次公开的年份，不表示所有 SKU、主板、NIC 或软件组合均支持。下表刻意区分真正的 cache stashing 与相邻的缓存一致性能力，避免把品牌功能泛化成 L2/L3 stash。

| 厂商 / 生态 | 首次公开年份 | 硬件机制与落点 | 软件/配置布局 | 对 KVCache 的判断与限制 |
|---|---:|---|---|---|
| Intel | **2012**（DDIO） | I/O 入站写及设备读取优先通过 LLC；不是通用“直接写核私有 L2” | BIOS/平台的 DDIO 与 IO LLC ways；可用 Intel 性能工具观测；TPH/非分配写策略仍依赖平台 | 适合 CPU 紧接着处理的块或描述符。LLC 配额和逐出决定收益，脏行最终写 DDR |
| AMD | **2024**（EPYC 9005），SDCI 文档 **2025** | SDCI 使用 TPH 将特定入站 I/O 直接导向目标 core 的 L2 | 支持 SDCI 的 NIC、Solarflare sfc 驱动 / Onload；BIOS 及 DPDK 调优项 | 已有明确 L2 定向证据；应给 CQE/元数据优先，不应用于大 KV payload 常驻 |
| Arm AMBA / SoC 生态 | CHI stash 增强公开约 **2016** | CHI 可携带 StashNID/StashLPID；最终 cache 目标由 SoC 实现定义 | 依赖互连、固件、I/O 代理及具体 SoC 软件栈 | 可形成很强的片上 data plane，但不能从“使用 Arm CPU”推断已开启 PCIe 设备 stash |
| PCI-SIG + Linux | **2008**（TPH），Linux 文档/框架持续演进 | 定义 endpoint 向平台表达处理提示的标准能力 | Linux 驱动显式启用 TPH，申请 CPU tags 并写入 endpoint | 是跨厂商拼装的基础，不能单独保证命中层级、时延或吞吐 |
| NVIDIA Grace Hopper / Grace Blackwell 系统 | **2022**（GH200 架构公开） | NVLink-C2C 提供 CPU–GPU 的一致性内存访问 | CUDA 一致性内存、stream memory operations 等 | 这是高带宽异构一致性，不是公开的 PCIe TPH→CPU L2 cache stashing 实现；适合作为替代架构比较 |

参考资料：Intel 的 DDIO 机制与写回行为见 [DDIO Primer](https://www.intel.com/content/dam/www/public/us/en/documents/technology-briefs/data-direct-i-o-technology-brief.pdf)；Arm 明确把 CHI cache stashing 作为可选互连增强，目标实现由系统定义。[Arm AMBA CHI 介绍](https://developer.arm.com/community/arm-community-blogs/b/soc-design-and-simulation-blog/posts/introducing-new-amba-5-chi-protocol-enhancements)

---

## 7. DPU 与 SSD 直通：各厂商及软件生态的实际布局

> 这里的“直通”必须至少区分两件事：  
> **(a)** 有 DPU/压缩/存储卸载功能；**(b)** 有已公开、可部署的 DPU/RNIC 与 NVMe Endpoint–Endpoint P2P 数据路径。前者不自动推出后者。下表的“DPU–SSD 通用直通成熟度”以公开文档为准。

| 厂商 / 生态 | 首次公开年份 | 硬件布局 | 软件布局 | 已公开的直通范围 | DPU–SSD 通用直通成熟度 |
|---|---:|---|---|---|---|
| Linux PCI P2PDMA + SPDK | **2018** | 利用 PCIe Endpoint 及（可选）NVMe CMB 等 peer memory | Linux pci_p2pdma，SPDK P2P / CMB 路径 | 端点间 DMA；同一 Switch 最理想 | **实验性/拓扑依赖**；是搭建自研方案的基础，不是产品保证 |
| NVIDIA GPUDirect Storage | **2020** | GPU memory、NVMe、NIC 的直接 DMA 路径 | cuFile、nvidia-fs、O_DIRECT、CUDA GDS 栈 | **GPU ↔ 本地 NVMe**，以及 GPU↔远端 NIC | 成熟的相邻方案，但对象是 GPU，不可直接写成“DPU–SSD 已通用化” |
| NVIDIA BlueField-3 + DOCA | BF3 **2021**；DOCA 压缩栈持续发布 | 400 Gb/s DPU，DPU/host memory buffer；可用硬件 Deflate/LZ4 解压能力及 CRC 输出 | DOCA Core、DOCA Compress、RDMA/存储相关 SDK | DPU 上转换卸载有公开支持 | **需集成验证**；公开 DOCA 文档未承诺一条通用 BF3 RNIC→CRC/解压→NVMe P2P 成品管线 |
| Intel Xeon 加速器 | **2023**（4th Gen Xeon） | DSA 做数据搬运/CRC，QAT/IAA 做压缩/解压等 | idxd、accel-config、DML、QPL/DPDK 等 | CPU 平台内的转换和数据移动卸载 | 不是独立 DPU–SSD P2P；可与 Linux/SPDK P2P 组合但需逐项验证 |
| AMD Pensando | AMD 纳入 **2022** | 可编程 DPU，网络、存储、安全卸载 | Pensando SSDK / P4 可编程面 | DPU 侧网络/存储处理 | 公开产品资料未给出通用本地 NVMe P2P + CRC/解压 KVCache 流程 |
| Samsung SmartSSD | 首代 **2020**；二代 **2022** | 在 SSD 内放置 Arm/FPGA 计算，靠近 NAND 做计算存储 | SmartSSD 开发/应用软件栈 | “计算移入 SSD”，降低主机搬运 | 是替代性架构，不是 DPU 到 SSD PCIe peer DMA；适合筛选/转换下沉 |
| Marvell OCTEON 10 DPU | **2021** | DPU 集成 Arm Neoverse、网络/安全处理、PCIe/DDR | OCTEON SDK 与平台软件 | 网络/安全/存储基础能力 | 未见公开通用 DPU↔NVMe P2P KVCache 成品承诺，需与平台 P2PDMA 联合评估 |
| AWS Nitro | **2017** | Nitro Card / 控制器处理 VPC、EBS、实例本地 NVMe 等隔离与卸载 | AWS 托管服务接口 | 云内部网络/存储数据面卸载 | 架构有效但专有，客户没有通用可编程 DPU–SSD P2P 接口 |

这张表的最重要结论是：**市场已有很多“靠近数据的处理”组件，但“通用 DPU–SSD 直通”仍是一个由 PCIe 拓扑、DMA 映射、SSD 控制器、DPU SDK 与软件队列共同拼成的系统能力。** 不应因某厂商同时售卖 DPU、NVMe、压缩引擎，就推断该组合无需开发便可端到端 P2P。

公开依据包括：[NVIDIA GDS 概览](https://docs.nvidia.com/gpudirect-storage/overview-guide/index.html)、[NVIDIA DOCA Compress](https://docs.nvidia.com/doca/archive/2-5-3/DOCA%2BCompress/index.html)、[Intel 4th Gen Xeon 加速器事实表](https://download.intel.com/newsroom/2023/data-center-hpc/4th-Gen-Xeon-Accelerator-Fact-Sheet.pdf)、[AMD Pensando 产品页](https://www.amd.com/en/products/data-processing-units/pensando.html)、[Samsung SmartSSD 二代发布](https://news.samsung.com/us/samsung-electronics-develops-second-generation-smartssd-computational-storage-drive-upgraded)、[Marvell OCTEON 10 产品资料](https://www.marvell.com/content/dam/marvell/en/public-collateral/embedded-processors/marvell-octeon-10-dpu-platform-product-brief.pdf) 与 [AWS Nitro 架构说明](https://docs.aws.amazon.com/whitepapers/latest/security-design-of-aws-nitro-system/the-components-of-the-nitro-system.html)。

---

## 8. 面向 KVCache：DPU–SSD 直通 vs. cache stashing 到 L2/L3

| 维度 | DPU/RNIC–SSD 真直通 | Stash 到 L3/LLC | Stash 到 L2 |
|---|---|---|---|
| 最适合的数据 | 大块 KV payload、CPU 不检查载荷、可流式硬件转换 | CPU 必须很快做 CRC/解压/重排的中等微分块 | CQE、描述符、索引、状态字、短 header |
| 主机 DDR | 条件满足时数据面可为 0 | 有最终一致性回写；额外读取取决于 hit rate | 只适合小对象，payload 仍应去 LLC/DDR/P2P |
| CPU 核参与 | 控制面仍需；可把 payload 转换交给 DPU | 通常需要一个固定 poller/consumer 核 | 强依赖目标核、RSS/IRQ/队列亲和性 |
| 载荷容量 | 受 DPU/SSD buffer、DMA window、队列深度约束，不受 LLC 容量直接限制 | 受 LLC 配额、竞争业务、块生命周期严格约束 | 被核私有 L2 容量严限制 |
| 拓扑要求 | 极高：Switch/Root Complex、ACS、IOMMU、驱动均决定成败 | 要求同 NUMA 较好，但不要求 endpoint P2P 可达 | 还要求 cache 注入目标和消费者核匹配 |
| 转换能力 | 可在 DPU/硬件引擎流式 CRC、压缩/解压；需定义错误与回退 | 可融合 CPU CRC/解压，避免多次 DDR pass | 只用于触发/元数据，不做大块转换 |
| P99 风险 | P2P fallback、DMA map 失败、ACS 改道、队列死锁/顺序错误 | LLC 抖动、跨核迁移、I/O way 过小、被逐出 | L2 冲突与任务迁移最敏感 |
| 软硬件成熟度 | 某些专用组合成熟，通用 DPU–NVMe 仍需要逐平台验证 | Intel DDIO 已广泛部署；定向程度因平台不同 | 目前 AMD SDCI 等特定组合证据最清晰 |
| KVCache 推荐用法 | 规模大且读写模式稳定时的目标架构 | 作为 CPU 转换路径的低风险优化和 P2P 失败回退 | 仅优化控制面，带动 payload 路径但不承载它 |

**推荐的混合形态**通常优于二选一：

- 用 L2 stash 放收包 CQE、KV block index、CRC 结果和 NVMe 提交描述符；
- 用受控 L3 微分块完成必须由 CPU 处理的 CRC/解压；
- 对无需 CPU 查看的完整大块，走经验证的 DPU/SSD P2P；
- P2P 不可用或发生错误时，明确回退到 L3/DDR 路径，而不是让驱动隐式 bounce 后仍误报“直通”。

---

## 9. 除直通和 cache stashing 外，值得考虑的加速方案

| 方案 | 解决的主要瓶颈 | 与 stash / P2P 的关系 | 适用条件 |
|---|---|---|---|
| GPU Direct RDMA + GPUDirect Storage | GPU 参与推理/重算时的 GPU↔NIC↔NVMe 搬运 | 直接绕开 CPU bounce；与 DPU–SSD 不同但可组成端到端设备路径 | KVCache 的生产/消费主要在 GPU，且 GDS 拓扑和驱动受支持 |
| DSA / QAT / IAA / DPU 流式引擎 | CPU CRC、复制、压缩/解压的算力和多 pass | 不一定消除 DDR，但可减少 CPU 核与融合 pass | CPU 必须参与或 P2P 无法建立时尤其有价值 |
| Computational Storage / SmartSSD | 过滤、压缩、格式转换靠近 NAND | 从“绕过 DDR”转为“减少需要离开 SSD 的字节” | 工作负载允许将算子下沉，管理和调试可接受 |
| SPDK、O_DIRECT、io_uring fixed buffers | 页缓存复制、系统调用和动态 pin/unpin 开销 | 是 stash 与 P2P 的基础卫生，不替代任何硬件机制 | 低延迟、固定大队列、用户态数据面 |
| CXL.mem / CXL 共享内存 / HBM | 容量层级、跨加速器共享、CPU 内存带宽压力 | 不是 PCIe Endpoint P2P；可作为大 KVCache 的容量池或一致性域 | 允许更高访问时延，软件能处理 NUMA/分层放置 |
| KVCache 分页、块表与按层/按 token 调度 | 从算法与内存管理层减少无效搬运、碎片与元数据压力 | 可让 stash 专注元数据，让 P2P 只搬真正要用的页 | 推理引擎能支持 paged KV/虚拟地址/预取策略 |
| 数据格式与协议优化 | 减少 P、消除独立 CRC pass、减少重排 | 直接降低第 4 节的放大项 | 可变精度、分块压缩、CRC 与解压融合、零拷贝 SGL |

在很多 KVCache 系统中，**先减少传输字节和重复扫描，常常比先争取某一级缓存命中更稳健**。例如将 CRC 融入解压流、使用稳定大小的 KV block、按 token 预测预取、避免跨 NUMA 的队列迁移，都能降低最差路径。

---

## 10. 落地建议、基准测试与风险清单

### 10.1 分阶段实施

1. **先建立可信基线**：固定 NUMA、CPU poller、RNIC queue、NVMe queue，采用固定注册缓冲；分别记录吞吐、P50/P99、CPU 利用率、DDR read/write、LLC hit/miss、PCIe 计数和 SSD IOPS/带宽。
2. **先优化控制面**：把 CQE/描述符/状态对象定位在消费者核附近；验证 TPH/SDCI 后再谈 L2。若无法证明 tag 已生效，按普通 DMA 对待。
3. **再优化 CPU 转换数据面**：开启 LLC stash/DDIO，选择 4–16 MiB 起步的微分块；融合 CRC 与解压；对比 h=0%、80%、100% 的实测，而不是只看平均带宽。
4. **最后验证 P2P**：先用同一 PCIe Switch 拓扑；检查 ACS/IOMMU、P2PDMA mapping、NVMe SGL 和 DPU/RNIC peer aperture。用可观测的 PCIe/内存计数确认没有 host bounce。
5. **定义提交协议**：远端 ACK 需要区分“已写入接收 MR”“已提交 NVMe”“已可恢复持久化”三种状态；在故障恢复设计中保留 CRC、日志/元数据和重复提交处理。

### 10.2 必测矩阵

| 变量 | 至少应比较的档位 | 通过标准 |
|---|---|---|
| CRC | 关闭；融合 CRC；独立 CRC pass | 融合后不新增独立 DDR pass，错误注入可准确发现 |
| 压缩比 | 1:1、2:1、实际 P50/P99 | 不只按平均压缩比规划 LLC chunk 与队列 |
| stash | DDIO/stash 关；开；受限 IO-LLC way | DDR 读取和 LLC 指标与模型方向一致，P99 不恶化 |
| 命中窗口 | 小/中/大 chunk；不同队列深度 | 找到逐出拐点，而非只测单一吞吐峰值 |
| P2P | 同 Switch；同 Root 不同 Switch；跨 Root；强制禁用 | 可明确区分真 peer DMA 与 DDR fallback |
| CPU 亲和性 | 同核/同 CCD；跨 CCD；跨 NUMA | L2/L3 方案只在预期亲和性下宣称收益 |
| 提交语义 | RNIC 完成、NVMe 完成、FUA/flush | 故障注入后无“网络 ACK 早于持久化”数据丢失 |

### 10.3 需要在设计评审中明确的开放问题

- 目标服务器的 CPU 代际、NIC、NVMe 型号、PCIe Switch/Root Complex 拓扑和 BIOS 选项分别是什么？
- CRC 覆盖压缩前还是解压后数据？是否已有 RoCE ICRC/T10 DIF/应用对象校验，避免重复且无收益的扫描？
- KVCache 的实际压缩比分位数、块大小、并发 QP/NVMe 队列深度和持久化 SLA 是什么？
- DPU 本地 DDR 是否可承受最坏并发缓冲？P2P 失败时的可观察回退、限流和数据一致性策略是什么？
- “落盘完成”对上层是 SSD controller completion、FUA、flush，还是复制到第二副本之后？这决定 RDMA completion 之后还需要多少协议状态。

---

## 11. 参考资料（公开可复核）

1. [Intel: Data Direct I/O Technology Primer, 2012](https://www.intel.com/content/dam/www/public/us/en/documents/technology-briefs/data-direct-i-o-technology-brief.pdf)
2. [Intel: Analyzing and Monitoring Intel DDIO Performance](https://www.intel.com/content/www/us/en/developer/articles/technical/ddio-analysis-performance-monitoring.html)
3. [PCI-SIG: Transaction Processing Hints ECN, 2008](https://pcisig.com/PCIExpress/ECN/Base/TLPProcessingHints)
4. [Linux kernel: PCIe TPH Support](https://docs.kernel.org/PCI/tph.html)
5. [AMD: Scalable Data Cache Injection White Paper, 2025](https://www.amd.com/content/dam/amd/en/documents/epyc-technical-docs/white-papers/58725.pdf)
6. [Arm: AMBA 5 CHI protocol enhancements and cache stashing](https://developer.arm.com/community/arm-community-blogs/b/soc-design-and-simulation-blog/posts/introducing-new-amba-5-chi-protocol-enhancements)
7. [Linux kernel: PCI peer-to-peer DMA support](https://www.kernel.org/doc/html/latest/driver-api/pci/p2pdma.html)
8. [SPDK: Peer-to-peer memory](https://spdk.io/doc/peer_2_peer.html)
9. [NVIDIA: GPUDirect Storage overview](https://docs.nvidia.com/gpudirect-storage/overview-guide/index.html)
10. [NVIDIA: DOCA Compress](https://docs.nvidia.com/doca/archive/2-5-3/DOCA%2BCompress/index.html)
11. [Intel: 4th Gen Xeon built-in accelerators fact sheet](https://download.intel.com/newsroom/2023/data-center-hpc/4th-Gen-Xeon-Accelerator-Fact-Sheet.pdf)
12. [Samsung: Second-generation SmartSSD announcement](https://news.samsung.com/us/samsung-electronics-develops-second-generation-smartssd-computational-storage-drive-upgraded)

