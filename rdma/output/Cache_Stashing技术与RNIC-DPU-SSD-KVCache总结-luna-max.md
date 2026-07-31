# Cache Stashing 技术与 RNIC/DPU—SSD KVCache 数据通路总结

**资料范围**：本目录根目录下的 5 篇讨论稿，以及截至 2026-07-31 可核验的厂商、内核、开源项目公开资料  
**交付对象**：系统架构、DPU/RNIC、SSD、内核与推理基础设施团队  
**核心判断**：Cache Stashing 是“让 I/O 数据更快到达即将消费它的 CPU 核心/缓存”的低时延技术；它不是大块 KVCache 持久化的通用替代方案。对大块 KVCache，优先考虑 RNIC/DPU—SSD 的 P2P 或存储卸载；对描述符、完成标记、元数据和很小的热数据块，L2/L3 Stashing 仍然有价值。最稳妥的工程组合通常是“控制面 Stashing + 数据面 P2P/硬件引擎”。

---

## 1. 结论先行

1. **Cache Stashing 的本质**：把 PCIe/片上 I/O 的 DMA 目标从“先落 DDR、CPU 再读”改变为“直接进入共享 LLC、目标 L2 或一致性缓存层”，减少一次或多次 DDR 往返。Intel DDIO、Arm AMBA CHI Stash、PCIe TPH/Steering Tag、AMD SDCI 属于同一问题空间，但实现层次、可见性和依赖并不相同。

2. **RNIC/DPU→SSD 的大块 KVCache 数据面**：如果 PCIe 拓扑、IOMMU/ACS、设备驱动和 SSD 控制器都允许，首选 RNIC/DPU→SSD 的 P2P/存储卸载路径；CRC、压缩/解压缩也应尽量在 RNIC/DPU、DSA/QAT、SSD 侧 FPGA/控制器内完成。这样可以把 Host DDR payload traffic 降到接近 0。

3. **L3 Stashing 的收益有条件**：只有在以下条件同时成立时，L3 理想模型才有意义：
   - I/O 写入确实被分配到目标 LLC/L3，而不是直接写 DDR；
   - CPU 或后继 I/O 很快消费同一批 cache line；
   - 热窗口小于平台为 I/O 分配的 cache 容量，并且不会大量驱逐；
   - 后继 SSD DMA 读路径能够通过一致性协议/平台实现命中该缓存；
   - NUMA、PCIe root complex、CPU 核绑定和缓存路由都正确。

   否则，L3 Stashing 可能只增加 LLC 污染，DDR 放大倍数退化到普通路径，甚至因为驱逐和额外一致性流量而更差。

4. **KVCache 的推荐分层**：
   - 4B～几十 KB 的描述符、block table、队列门铃、完成标记：可考虑 L2/L3 Stashing 或目标核定向缓存；
   - 以 MB/GB 计的 KV block：优先 DPU/SSD P2P、NVMe-oF RDMA zero-copy、DPU/SSD 硬件 CRC/压缩；
   - 仍需 CPU 参与的微批处理：按微块切分，让每个微块在 L3 热窗口内完成“接收—校验—变换—发往 SSD”；
   - GPU 仍是主要消费者时：优先 GPUDirect RDMA/Storage、NVLink-C2C、GPU-side metadata/signaling，而不是把 GPU payload 绕到 CPU L3。

5. **文档中的数字是流量模型，不是端到端性能承诺**。DDR 放大倍数按“Host DDR 实际读写字节数 / 最终写入 SSD 的逻辑未压缩 KVCache 字节数”计算；不包含 PCIe/NVLink 包头、RDMA 协议开销、SSD NAND 内部写放大、描述符流量，也不假定所有 DDR 读写串行。

---

## 2. 资料与口径

### 2.1 本地讨论稿

本总结只读取根目录下以下 5 个文件，忽略其它目录中的文档：

| 文件 | 在本总结中的用途 |
|---|---|
| IO stash技术分析.md | Cache Stashing、Intel DDIO、PCIe/Arm 方向、KVCache 与 DDR 流量推导 |
| IO.md | Cache Stashing 的概念辨析、厂商/接口讨论、DPU/SSD 对比草稿 |
| stash crc on off ddr write and read.md | CRC 开关下 DDR 读写计数的基础模型 |
| RDMA 机制讨论.md | RDMA、QP、MR、RNIC、GPU/DDR 数据路径与控制面基础 |
| RDMA 机制讨论 补充稿.md | 前一篇的扩展稿，包含 GPU 同步、block table、vAttention 等补充讨论 |

RDMA 机制讨论 补充稿.md 的前 1092 行与 RDMA 机制讨论.md 重复，后文将其视为同一讨论链路的扩展，不重复计算。

### 2.2 三类信息必须区分

| 标记 | 含义 |
|---|---|
| **公开资料已证实** | 有厂商、Linux 内核、SPDK 或正式论文/文档明确描述该能力 |
| **拓扑/型号依赖** | 原理成立，但需要具体 CPU、NIC/DPU、SSD、PCIe switch、BIOS 和驱动组合；不能从“支持 PCIe/RDMA”直接推出 |
| **本文模型假设** | 为了可计算而设定的比例、容量、带宽或 cache 命中条件，不代表所有产品的实测结果 |

本地讨论稿中的部分“默认会进入 L3”“SSD 一定可以从 L3 直接读”“所有 P2P 都已成熟”等表述，本文改写为带条件的工程判断。厂商内部或未公开的部署，不作为公开事实引用。

---

## 3. Cache Stashing 是什么

### 3.1 普通 DMA 与 Stashing 的差异

传统的 PCIe I/O 写入大致是：

RNIC/SSD DMA → Host DDR → CPU cache miss/load → CPU 执行

如果 CPU 随后要校验、解压、更新 block table 或处理完成事件，同一数据至少会经历一次 DDR 写和一次 CPU 侧 DDR 读。

Cache Stashing 把路径改成：

RNIC/SSD DMA → 一致性缓存层（L2/L3/LLC）→ 立即消费它的 CPU 核心或后继 I/O

其中：

- **L2 Stashing**：通常需要更明确的目标核、Steering Tag、片上互连和平台支持；
- **L3/LLC Stashing**：常见于服务器 I/O 一致性机制，设备 I/O 进入共享 LLC，CPU 读取时可命中；
- **协议级 Stashing**：如 Arm CHI 的 Stash 事务，由片上互连表达缓存目标和一致性语义；
- **PCIe TPH**：设备可以携带 Steering Tag，请求平台将事务导向特定处理器/缓存资源；它是能力和提示，不等于任何平台都会命中 L2/L3；
- **P2P DMA**：设备到设备的 DMA，目标可以是另一个设备的 BAR/CMB/可寻址内存，不是 CPU cache Stashing，二者不能混称。

### 3.2 Cache Stashing 不等于 Cache Bypass，也不等于 P2P

| 机制 | 主要目标 | Host DDR 是否必经 | CPU 是否必须消费 payload | 典型收益 |
|---|---|---:|---:|---|
| Cache Stashing | 让 I/O 数据靠近 CPU/共享缓存 | 不一定 | 通常是 | 降低 CPU 读取延迟和 DDR 往返 |
| Cache bypass / non-temporal DMA | 避免污染 CPU cache | 通常要落 DDR 或设备内存 | 不一定 | 大流量、不重用 payload 时避免污染 |
| PCIe P2P | 设备到设备 DMA | 可绕过 | 不必须 | RNIC/DPU 与 SSD/GPU 之间零 Host-DRAM payload copy |
| CXL memory pooling | 扩展/分层内存 | 可能 | 视应用而定 | 热/冷 KV 分层与容量扩展，不是简单低延迟 stash |

### 3.3 DDR 读写放大倍数的定义

本文统一定义：

**DDR amplification = Host DDR payload traffic / R**

其中 R 是最终要写入 SSD 的**逻辑未压缩 KVCache payload**。DDR payload traffic 包括：

- DMA 写入 DDR；
- CPU/加速器从 DDR 读取；
- CPU/加速器把结果写回 DDR；
- cache dirty line 最终回写 DDR。

在 L3 热路径中，本文额外报告“关键窗口内 DDR traffic”和“完整生命周期的保守 traffic”。因为某些 cache line 在 SSD DMA 完成后仍然是 dirty，最终是否回写、何时回写、是否能在安全回收时丢弃，取决于内存生命周期和平台操作，不能只报一个“0 DDR”而不说明测量窗口。

---

## 4. Cache Stashing 的发展过程

### 4.1 从“DMA 到内存”到“DMA 到缓存”

早期服务器 I/O 的主要优化目标是减少 CPU copy。随着网络速率从 10/40GbE 走向 100/200/400GbE、SSD 从 SATA/NVMe 走向多队列和高 IOPS，单纯减少软件 copy 已经不够：如果包头、完成事件或需要 CPU 转换的数据先写 DDR，再被 CPU 读回，DDR 和内存控制器会成为额外瓶颈。

因此出现了三条逐渐汇合的路线：

1. **服务器 I/O 一致性路线**：设备 DMA 直接分配到共享 LLC，CPU 读入时命中，例如 Intel DDIO；
2. **片上一致性互连路线**：通过 CHI 等互连协议把数据 stash 到指定处理器/缓存层；
3. **设备定向路线**：通过 PCIe TPH/Steering Tag、IRQ/RX queue 绑定、DPU/加速器协作，把数据靠近真正的消费者。

### 4.2 关键里程碑

| 时间 | 技术/事件 | 变化 | 工程含义 |
|---|---|---|---|
| 2012～2013 产品代 | Intel Data Direct I/O（DDIO），随 Xeon E5/E7 v2 代公开 | I/O 的主要目的地/来源从 DRAM 转向处理器 LLC | 软件通常无需显式改写，但 cache way、NUMA、I/O 设备和负载必须匹配 |
| 2013 | Arm AMBA 5 CHI 公开 | 片上互连具备更明确的缓存一致性与 Stash 事务表达 | 适合 SoC/服务器芯片内部的 CPU、NIC、存储控制器协作；不自动等于外部 PCIe 设备能力 |
| 2010s～至今 | PCIe TPH/Steering Tag 逐渐用于处理器定向 | 设备可携带目标处理器/缓存相关提示 | 依赖平台 ACPI、IOMMU、PCIe 设备和驱动；Linux 当前提供 TPH 支持框架，但不保证所有平台启用 |
| 2016 前后公开的 CHI Cache Stashing 用例 | Arm 展示网络/存储场景把关键包头靠近处理器缓存 | 从“协议能力”走向“应用场景” | 对小的控制/元数据特别有价值，对 GB 级 payload 仍受缓存容量约束 |
| 2024～2025 | AMD EPYC 9005/Zen 5 代与 Solarflare X4 的 SDCI 公开资料 | NIC ingress 可定向到相关核心的 L2；配合 Onload/驱动 | 公开白皮书给出低时延改善，但只适用于支持的 EPYC、NIC、BIOS 和软件组合 |
| 2019～2026 | GPUDirect Storage、DPU storage offload、KV 专用存储层持续发展 | 大块数据面转向设备间 DMA、DPU/SSD 硬件处理和分层存储 | 对 KVCache，数据面逐渐与 CPU cache 优化分工，而不是由 L3 承担全部 payload |

### 4.3 发展的方向性结论

Cache Stashing 的发展不是“缓存越大、所有 I/O 越应该 stash”，而是：

普通 DMA → 共享 LLC → 目标 L2/片上一致性 → DPU/设备协同 → 控制面与数据面分工

随着 payload 增大，最优点通常从“缓存整块数据”转为：

- stash 元数据、描述符和状态；
- 在 DPU/SSD 中完成 CRC、压缩、解压缩、加密和格式转换；
- 通过 P2P 让大 payload 绕过 Host DDR；
- 用 CXL、GPU HBM、SSD flash tier 或 KV 专用层做容量与热度分层。

---

## 5. RNIC/DPU 到 SSD：完整处理流程

以下分为“普通 Host DDR 路径”“Cache Stashing 路径”和“P2P/存储卸载路径”。三者可以在一个系统中同时存在。

### 5.1 普通 Host DDR 路径

1. **RDMA 接收**：远端 RNIC 把 RoCE/InfiniBand 数据包交给本端 RNIC；RNIC 完成包重组、QP 校验、序列处理、RNR/重传等协议动作。
2. **链路完整性检查**：RNIC 处理以太网 FCS、RoCE/IB 相关 ICRC 等链路/传输完整性字段。本文的 DDR 模型不把这一步算作 Host DDR 的 application CRC。
3. **DMA 落地**：RNIC 根据 WQE、MR、lkey/rkey、IOMMU/IOVA 把 payload DMA 写入 Host DDR。
4. **CPU 处理**：CPU 从 DDR 读取 payload，做 application CRC、解压缩、格式转换、KV block 排布或元数据更新。
5. **准备 NVMe 写入**：CPU/DPU 填写 NVMe SQE、提交 doorbell；SSD 控制器通过 DMA 从 Host DDR 读取待写数据。
6. **SSD 内部路径**：SSD controller 把数据放入内部 DRAM/SRAM，完成 FTL 映射、磨损均衡、ECC/LDPC、NAND program；这些 NAND 内部流量不是本文 Host DDR amplification。
7. **完成处理**：SSD CQE、MSI-X 或 polling 通知提交方；RDMA CQE 和 SSD CQE 由软件/DPU 做关联，更新 KV block 状态。

这种路径的主要问题是：对“接收一次、CPU 读一次、SSD 再读一次”的 KVCache，payload 至少发生一次 DDR 写和一至两次 DDR 读；如果还产生解压缩输出，则会再增加 DDR 写。

### 5.2 Cache Stashing 路径

理想的 RNIC/DPU→L3 路径是：

RNIC DMA → I/O 一致性代理/LLC → CPU 立即读取 → 结果仍在 LLC → SSD 一致性 DMA 读取

具体实现必须回答以下问题：

- RNIC 的 DMA write 是否会写分配到 LLC，还是 bypass 到 DDR；
- I/O LLC 使用多少 ways，是否被平台限制；
- RNIC、CPU consumer、SSD 是否位于相同或可一致性互通的 NUMA/root complex；
- SSD outbound DMA read 是否能通过 snoop/一致性协议命中 dirty LLC line；
- L3 热窗口是否足够小，能否在驱逐前消费；
- cache line 在 CPU/SSD 之间的 owner、dirty、completion 和回收语义是什么。

如果只有“入站 DMA 可能进入 LLC”，而 SSD 的读路径无法命中缓存，则不能把它写成“RNIC→L3→SSD 零 DDR”；这时最少仍可能出现 dirty line 回写和 SSD 从 DDR 读取。

### 5.3 P2P/存储卸载路径

更适合大 KV payload 的理想路径是：

RNIC/DPU → CRC/解压缩/加密引擎 → SSD controller 可寻址的 P2P buffer/CMB/PMR → SSD NAND

典型步骤：

1. **启动前建立拓扑与资源**：确认 RNIC/DPU 与 SSD 是否在同一 PCIe switch 或允许的 PCIe hierarchy；确认 ACS、IOMMU、P2PDMA allowlist、BAR/CMB、DMA mask 和地址映射。
2. **建立存储队列**：DPU 或 host orchestrator 创建 NVMe SQ/CQ、分配 submission buffer，建立 NVMe namespace、queue pair 与 RDMA QP 的映射。
3. **建立 RDMA 资源**：注册远端/本端 MR，完成 page pin、IOVA、lkey/rkey、QP 状态转换和 out-of-band buffer handshake。
4. **接收与校验**：RNIC 收包；DPU inline engine 或专用压缩/CRC/crypto engine 在数据靠近设备时处理。
5. **P2P 交付**：DPU/RNIC 把 payload DMA 到 SSD controller 可访问的 P2P 内存窗口、CMB、PMR 或由 SSD driver 暴露的 peer resource。这里的“直通”通常是绕过 Host DDR，并不意味着 RNIC 直接改写 NAND。
6. **SSD 介质写入**：SSD controller 完成 FTL、ECC、NAND program 和持久化语义。
7. **完成传播**：SSD CQE→DPU/host orchestrator→RDMA CQE/远端 ACK；错误路径要同时传播 CRC、解压缩、NVMe status、重试和超时信息。

Linux 内核的 [PCI P2PDMA 文档](https://www.kernel.org/doc/html/latest/driver-api/pci/p2pdma.html)明确说明：同一 PCIe switch 内的 P2P 路由较可定义；跨 hierarchy domain 的路由不由 PCIe 规范统一定义，内核默认会阻止部分不安全组合，并要求 provider/client/orchestrator 驱动协同。该文档还以 NVMe Target Copy Offload 为例描述 RNIC→NVMe CMB→NVMe 的协同形态。

### 5.4 RNIC/DPU→SSD 三条路径的对照

| 路径 | Host DDR payload | CPU 是否读 payload | 适合对象 | 主要风险 |
|---|---:|---:|---|---|
| RNIC→DDR→CPU→DDR→SSD | 高 | 是 | 通用、兼容性优先、CPU 必须变换 | DDR 带宽、CPU cycles、copy/读写往返 |
| RNIC→L3→CPU→SSD | 理想热窗口低；完整生命周期通常仍有回写 | 是 | 小块热数据、CRC/轻量变换、控制面 | cache 容量、驱逐、NUMA、SSD 读命中语义 |
| RNIC/DPU→P2P/DPU engine→SSD | 目标 payload 可为 0 | 否或仅处理控制面 | 大块 KV、硬件 CRC/压缩、数据面 | PCIe 拓扑、驱动、IOMMU/ACS、设备能力与错误处理 |

---

## 6. SSD 到 RNIC/DPU：完整处理流程

### 6.1 普通 Host DDR 路径

1. RNIC/DPU/CPU 提交 NVMe read command；
2. SSD 从 NAND 读出数据，经过 SSD 内部 ECC/FTL，把结果 DMA 写入 Host DDR；
3. CPU 或 RNIC 再从 Host DDR 读取 payload；
4. 如果需要解压缩/CRC，CPU/DPU 读取并变换；
5. RNIC 通过 RDMA send/read response 发出 payload；
6. NVMe CQE、RDMA CQE、重传和 buffer 回收完成。

最简单的未压缩路径就是：

SSD→DDR 写 R + RNIC←DDR 读 R = 2R

这也是 SSD→RNIC 方向普通路径至少 2× DDR traffic 的来源。

### 6.2 Cache Stashing 路径

理想的 SSD→L3→RNIC 路径是：

SSD DMA → I/O LLC/L3 → RNIC 一致性读 / CPU 读 → RDMA transmit

它需要比“RNIC 入站 stash”更多的确认：

- SSD 的 DMA write 是否使用与 RNIC 相同的 I/O 一致性代理；
- RNIC 的 outbound DMA read 是否会 snoop 并拿到 LLC dirty line；
- SSD、RNIC、CPU 是否共享同一一致性域；
- data ownership 是否允许 RNIC 在 CPU 尚未显式 memcpy 的情况下直接发送；
- CQE 是否只在数据可见、cache ownership 已正确转移后产生。

因此，SSD→L3→RNIC 是一种**平台相关的可选优化**，不能作为通用 SSD 直通方案。对大 payload，SSD/RNIC P2P 或 DPU storage engine 通常更容易形成可度量、可隔离的数据面。

### 6.3 P2P 读路径

SSD NAND → SSD controller buffer/CMB → DPU/压缩解压缩/CRC engine → RNIC → RDMA network

如果 SSD 存储的是压缩 KV：

- 发送压缩数据：SSD 读压缩块，DPU/RNIC 直接发出，Host DDR 可保持 0；
- 发送未压缩数据：SSD 读压缩块，DPU 内联解压，再把未压缩流送给 RNIC；
- 如果目标端可解压，也可以减少本端 DPU 工作，但会改变网络流量、接收端 CPU/DPU 消耗和端到端时延。

---

## 7. KVCache 落盘示例：CRC 与解压缩四种场景

### 7.1 建模假设

为使数据可以复算，先给出符号，再给出具体数字：

- R：最终写入 SSD 的逻辑未压缩 KVCache 大小；
- α：压缩后网络 payload 与 R 的比值；
- C = αR：网络上接收的压缩 payload；
- 本文示例取 R = 20 GiB、α = 0.5，因此 C = 10 GiB；
- “CRC 开启”指 application-level CRC，默认校验网络接收的压缩字节流；
- RNIC 处理的链路 FCS/ICRC 仍然存在，但不计入 Host DDR，因为它在 RNIC 内完成；
- 只统计 payload，不统计 WQE、CQE、NVMe SQE/CQE、doorbell、block table 和小型状态结构；
- 基线路径是 RNIC→Host DDR→CPU CRC/解压缩→Host DDR→NVMe SSD；
- L3 Stashing 采用“微块及时消费、入站和出站都能命中同一一致性缓存”的理想热窗口；
- L3 完整生命周期的保守模型把 dirty input/output 都计入最终 DDR writeback；关键窗口内则先报告为 0 DDR read/0 DDR write；
- DPU/P2P 列假设 CRC、解压缩和 NVMe 数据面都在 DPU/SSD 侧处理，Host DDR 不承担 payload staging。

本地讨论稿用 Llama-3-70B GQA FP16 做过同类规模估算：

每 token KV = 2（K、V）×80 层×8 KV heads×128 head dim×2 B = 327,680 B ≈ 320 KiB/token

16 batch×4096 token×327,680 B = 21,474,836,480 B ≈ 20 GiB

这正好可以作为本节的 R。实际模型的层数、KV heads、head dim、数据类型、分页策略和压缩比会改变 R 与 α，但不改变流量计算方法。

### 7.2 基线 DDR 读写计算

为避免与 payload 符号 R 混淆，下表中的 W/R 只表示 DDR Write/Read；表内单位是 GiB。

| 场景 | DDR 操作展开 | 总 DDR payload | 相对最终未压缩 R 的放大倍数 |
|---|---|---:|---:|
| CRC 关，解压缩关 | RNIC 写 20 + SSD 读 20 | 40 | 2.0× |
| CRC 开，解压缩关 | RNIC 写 20 + CPU CRC 读 20 + SSD 读 20 | 60 | 3.0× |
| CRC 关，解压缩开 | RNIC 写 10 + CPU 解压读 10 + 解压输出写 20 + SSD 读 20 | 60 | 3.0× |
| CRC 开，解压缩开，**单遍融合** | RNIC 写 10 + CPU/引擎单遍读 10（同时 CRC 与解压）+ 输出写 20 + SSD 读 20 | 60 | 3.0× |
| CRC 开，解压缩开，**两个独立 pass** | RNIC 写 10 + CRC 读 10 + 解压读 10 + 输出写 20 + SSD 读 20 | 70 | 3.5× |
| CRC 校验未压缩输出（变体） | RNIC 写 10 + 解压读 10 + 输出写 20 + CRC 读 20 + SSD 读 20 | 80 | 4.0× |

一般化公式如下：

| 场景 | 基线 DDR traffic / R |
|---|---:|
| CRC 关、解压缩关 | 2 |
| CRC 开、解压缩关 | 3 |
| CRC 关、解压缩开 | 2 + 2α |
| CRC 开、解压缩开，CRC 与解压单遍融合 | 2 + 2α |
| CRC 开、解压缩开，CRC 与解压分两遍 | 2 + 3α |
| CRC 在解压缩后的 R 上单独读取校验 | 3 + 2α |

### 7.3 L3 Stashing 前后对比

下表把“L3 hot”分为两个视角：

- **关键窗口**：RNIC 收到微块到 SSD DMA 消费微块之间；理想命中时不发生 payload DDR read/write；
- **完整生命周期保守值**：把 dirty cache line 最终回写 DDR 计算进去。实际系统如果有专门的 discardable buffer 回收、P2P destination 或非临时写策略，可能低于该保守值；如果微块在消费前被驱逐，则退化到基线。

| 场景 | 基线：DDR 写/读 | 基线总量 / 放大 | L3 hot：关键窗口 | L3 hot：完整生命周期保守 DDR 写/读 | L3 总量 / 放大 | DPU/P2P：Host DDR payload |
|---|---:|---:|---:|---:|---:|---:|
| CRC 关，解压缩关 | W20 / R20 | 40 GiB / 2.0× | W0 / R0 | W20 / R0（输入 dirty 回写） | 20 GiB / 1.0× | W0 / R0；0× |
| CRC 开，解压缩关 | W20 / R40 | 60 GiB / 3.0× | W0 / R0 | W20 / R0（CRC 读命中 L3） | 20 GiB / 1.0× | W0 / R0；0×，CRC inline |
| CRC 关，解压缩开 | W30 / R30 | 60 GiB / 3.0× | W0 / R0 | W30 / R0（压缩输入 10 + 未压缩输出 20 回写） | 30 GiB / 1.5× | W0 / R0；0×，解压 inline |
| CRC 开，解压缩开，单遍融合 | W30 / R30 | 60 GiB / 3.0× | W0 / R0 | W30 / R0（输入 10 + 输出 20 回写） | 30 GiB / 1.5× | W0 / R0；0× |
| CRC 开，解压缩开，两个 pass | W30 / R40 | 70 GiB / 3.5× | W0 / R0 | W30 / R0；两次 CPU 读都命中 L3 | 30 GiB / 1.5× | W0 / R0；0× |
| CRC 校验解压后的 R（变体） | W30 / R50 | 80 GiB / 4.0× | W0 / R0 | W30 / R0；CRC 读命中 L3 | 30 GiB / 1.5× | W0 / R0；0× |

**解读**：

1. “CRC 开启”本身不会改变 payload 大小，但如果 CRC 在 CPU 上对 DDR 中的输入再读一遍，就会增加一个 R 或 C 的 DDR read；如果 CRC 与解压缩合并为同一遍流式读取，则只增加计算，不增加第二遍 DDR read。
2. 对压缩输入，L3 完整生命周期的保守 DDR traffic 是 C + R = (1+α)R，因为压缩输入和解压缩输出都可能形成 dirty cache line。它不是无条件的 0；只有在测量窗口内或专门的 P2P/discard 设计下才可能接近 0。
3. 对“CRC 关、解压缩关”，如果 CPU 不需要读取 payload，单纯把一个大块数据 stash 到 L3 可能没有必要；表中 1.0×是“SSD 出站读能命中 L3、最终 dirty line 回写一次”的理想上限模型，工程上往往应直接选择 P2P。
4. 20 GiB 不能整体放入 L3。L3 数值代表把流分成微块，例如让单个 in-flight window 小于 I/O LLC 可用容量，并由消费者及时推进；不是申请 20 GiB 的 L3。
5. 如果微块在 CPU 消费前发生大量 LLC eviction，L3 hot 列应替换为基线列或两者之间的实测混合值。

### 7.4 用 DDR 带宽换算的下界示例

为了只说明流量量级，假设可供该数据面使用的有效 DDR 带宽为 400 GiB/s。下表是 DDR bytes / 400 GiB/s 的总线流量下界，不是端到端时延：

| 场景 | 基线 DDR 流量 | 基线等效 DDR 时间 | L3 hot 完整生命周期流量 | L3 等效 DDR 时间 | DPU/P2P |
|---|---:|---:|---:|---:|---:|
| CRC 关、解压缩关 | 40 GiB | 100 ms | 20 GiB | 50 ms | 0 ms payload |
| CRC 开、解压缩关 | 60 GiB | 150 ms | 20 GiB | 50 ms | 0 ms payload |
| CRC 关、解压缩开 | 60 GiB | 150 ms | 30 GiB | 75 ms | 0 ms payload |
| CRC 开、解压缩开，单遍融合 | 60 GiB | 150 ms | 30 GiB | 75 ms | 0 ms payload |
| CRC 开、解压缩开，两个 pass | 70 GiB | 175 ms | 30 GiB | 75 ms | 0 ms payload |

这组时间不能直接代替实际吞吐/时延。RDMA、CPU、解压缩、PCIe 和 SSD 可以重叠；SSD NAND latency、队列深度、写放大、后台 GC、DPU engine throughput 以及压缩比都会影响最终结果。

### 7.5 何时 L3 Stashing 反而不划算

- KV block 大于 I/O LLC working set，导致“写入即驱逐”；
- DPU/RNIC 把数据以非一致性或 cache-bypass 方式直接写 DDR；
- SSD 只能从 DDR 读取，不能 snoop/命中 LLC；
- CPU consumer 调度不及时，网络突发造成 cache thrash；
- 同一 LLC 同时服务多个 NIC、SSD、CPU socket，I/O way 预算被抢占；
- 对已经不需要 CPU 触碰的 payload 仍然强行 stash，导致 CPU cache pollution；
- 分页、压缩或 checksum 的输出比输入更大，dirty output 回写抵消了 input read 的收益。

---

## 8. Cache Stashing 的完整软硬件依赖

### 8.1 硬件依赖

| 层次 | 必要能力 | 需要核验的具体项 |
|---|---|---|
| CPU/cache/uncore | I/O 一致性、LLC/L3 分配、目标核/目标 cache 支持 | DDIO/SDCI/CHI 版本、I/O LLC ways、cache line size、snoop/ownership、NUMA socket |
| I/O 互连 | PCIe TPH/Steering Tag 或片上 CHI 等路由能力 | TPH capability、Steering Tag 表、ACPI 描述、设备到 CPU 的目标映射 |
| RNIC | RDMA protocol offload、PCIe DMA、CQ/WQE、可选 inline CRC/crypto/压缩 | ConnectX/专用 RNIC/DPU 型号是否公开支持相应路径；MR、ODP、DMA-BUF、P2P |
| DPU/加速器 | DMA、队列、doorbell、完成回传，最好有 CRC/compress/decompress/crypto | engine throughput、队列背压、错误码、内存窗口、跨 PCIe root complex 能力 |
| PCIe fabric | P2P 路由、同 switch 或允许的 hierarchy path | ACS 是否强制上送 root complex、IOMMU 保护、BAR/CMB 可寻址、PCIe switch firmware |
| SSD/controller | peer DMA、CMB/PMR 或外部可访问 staging buffer、NVMe 多队列 | CMB 是否可作为 P2P provider、controller DMA direction、namespace/flush/FUA 语义 |
| 内存系统 | DDR 带宽和一致性流量可观测 | IMC read/write counters、IIO/CHA counters、LLC occupancy/eviction、PCIe counters |
| 可靠性 | CRC/ECC、重试、顺序、持久化和 buffer ownership | CRC 覆盖范围、解压错误处理、NVMe status、RDMA CQE、断电/flush 语义 |

### 8.2 软件、固件与驱动依赖

| 层次 | 必要能力 | 典型实现/检查项 |
|---|---|---|
| BIOS/固件 | 打开平台 I/O cache、SDCI、IOMMU、PCIe ATS/TPH 相关选项 | 不能把某个平台的 BIOS 开关名称推广到所有服务器；记录实际 firmware 版本 |
| OS 内核 | PCIe TPH 框架、P2PDMA、IOMMU/ACS 策略、NUMA、IRQ affinity | Linux CONFIG_PCIE_TPH、P2PDMA provider/client、allowlist、DMA mapping |
| NIC/RNIC 驱动 | RX queue、CPU core、Steering Tag、MR/CQ 与 cache/NUMA 的绑定 | vendor driver、OFED/rdma-core、DPDK PMD、Onload 或等效栈 |
| DPU runtime | queue orchestration、RDMA/NVMe/crypto/compress API、错误与背压 | DOCA、SPDK/DPDK、厂商 SDK、DPU Arm/embedded CPU 控制面 |
| 存储软件 | NVMe driver/target、CMB/PMR provider、NVMe-oF、zero-copy/polling | Linux NVMe、SPDK、nvmet、io_uring；SPDK P2P 仍需按版本和硬件验证 |
| 内存注册 | pin、IOVA、lkey/rkey、page lifetime、DMA-BUF/peer memory | ibv_reg_mr、nvidia-peermem/DMA-BUF（GPU 场景）、DPU memory map |
| 应用/推理 runtime | KV block 生命周期、微块 pipeline、完成标记、重试和取消 | block table、generation/version、ownership、backpressure、超时、回收 |
| 观测与验证 | 能区分 cache 命中、DDR 流量与 P2P 流量 | Intel CHA/IIO/IMC、AMD IBS/uncore（以实际平台为准）、PCIe counters、SSD latency |

### 8.3 正确性与内存顺序

Cache Stashing 和 P2P 都不能只靠“写完 flag”来证明数据可见。必须明确：

1. payload 的 producer、consumer、owner 和 lifetime；
2. 数据写入、CRC/decompress 完成、descriptor/flag 写入之间的顺序；
3. CPU、RNIC、DPU、GPU、SSD 各自可观察到的 completion 定义；
4. cache line 是 clean、dirty、invalid 还是由设备直接取得；
5. 取消、超时、重试、链路重传和部分写入时如何回收 buffer；
6. 应使用平台支持的 DMA/RDMA/CUDA/设备同步 API，而不是把某一个 CPU 的 sfence 或普通编译器屏障当成跨设备全局可见性保证。

---

## 9. 各大厂商在 Cache Stashing 方向的软硬件布局

以下表格只列入公开资料能够明确归类的能力；“相邻能力”专门标出，避免把 GPU peer memory、DPU offload 或 P2P 误报成 Host CPU L2/L3 Stashing。

| 厂商/技术 | 首次公开/产品年份 | 硬件布局 | 软件/协议布局 | 公开资料能确认的范围与限制 |
|---|---:|---|---|---|
| Intel Data Direct I/O（DDIO） | 约 2013，Xeon E5/E7 v2 代 | Xeon I/O 一致性代理把 PCIe I/O 的主要目的地/来源放到 LLC；粒度以 cache line 为主 | 通常对应用透明；需要 Intel NIC/SSD、正确 NUMA/PCIe 拓扑和性能计数器 | [Intel DDIO 介绍](https://www.intel.com/content/www/us/en/io/data-direct-i-o-technology.html)称 DDIO 将 I/O 的主要目的地变为处理器 cache；[DDIO 分析](https://www.intel.com/content/www/us/en/developer/articles/technical/ddio-analysis-performance-monitoring.html)说明 I/O LLC ways 有限制。不是“所有 I/O 永远进入 L3” |
| Intel Xeon 当前支持 | 2020s～2026 | 新一代 Xeon 继续提供 DDIO，具体 I/O LLC、way 分配和平台开关依型号 | Intel VTune/uncore counter、BIOS/平台工具 | [Intel 支持页](https://www.intel.com/content/www/us/en/support/articles/000087975/processors/intel-xeon-processors.html)列出支持代际；应按 CPU SKU 和服务器平台实测 |
| Arm AMBA 5 CHI Cache Stashing | 2013 CHI；约 2016 公开场景化增强 | CHI 一致性互连、系统 cache、CPU cluster/处理器 cache 的 Stash 事务 | SoC interconnect、cache controller、设备/桥接 IP、固件和 OS 协同 | [Arm CHI enhancement](https://developer.arm.com/community/arm-community-blogs/b/soc-design-and-simulation-blog/posts/introducing-new-amba-5-chi-protocol-enhancements?pifragment-27083=2%3Fpifragment-27083%3D2)明确展示网络/存储关键数据靠近处理器 cache；它是 SoC/IP 能力，不等于任意 PCIe 卡都可 stash 到 CPU L2 |
| PCIe TPH/Steering Tag 生态 | 2010s 标准能力；Linux 当前持续完善 | PCIe device 产生目标处理器/Steering Tag，平台负责路由 | Linux CONFIG_PCIE_TPH、ACPI _DSM、设备驱动和 IRQ/queue affinity | [Linux TPH 文档](https://docs.kernel.org/7.1/PCI/tph.html)说明 TPH 是可选 capability，需平台和 driver enable；不是单一厂商产品，也不是命中 L3 的充分条件 |
| AMD SDCI + EPYC/Zen 5 | 2024 产品代；2025 白皮书 | 支持的 EPYC 把 Solarflare X4 ingress 定向到相关 CPU core 的 L2/CCD | AMD sfc/Onload、BIOS SDCI、NUMA/IRQ 绑定、DPDK/网络栈 | [AMD SDCI 文档](https://docs.amd.com/r/en-US/ug1586-onload-user/SDCI)和 [X4 白皮书](https://www.amd.com/content/dam/amd/en/documents/processor-tech-docs/white-papers/x4-sdci-whitepaper.pdf)给出公开的 L2 ingress 与时延改善；依赖特定 EPYC、NIC、driver 和 same-CCD 消费 |
| AMD SDCI 的系统调优 | 2024～2026 | BIOS、IOMMU、CCD/cache、NIC 队列和中断亲和性共同决定效果 | AMD DPDK tuning、Solarflare Onload、Linux TPH/平台配置 | [AMD DPDK 调优指南](https://docs.amd.com/api/khub/documents/TPtxZn7Ajbl4RMxb9StmzA/content)提到 SDCI BIOS 与 IOMMU/NUMA/IRQ 依赖；不能把 SDCI 结果外推到普通 AMD NIC |
| NVIDIA GPUDirect RDMA/Storage（相邻能力） | GDR 2010s；GDS 2019 | NIC↔GPU memory、storage↔GPU memory 的 peer DMA；不以 Host CPU L2/L3 为目标 | nvidia-peermem/DMA-BUF、CUDA、cuFile、GDS、PCIe P2P | [GPUDirect RDMA](https://docs.nvidia.com/cuda/gpudirect-rdma/?ncid=ref-inc-938520)和 [GDS](https://docs.nvidia.com/gpudirect-storage/overview-guide/index.html)是 peer memory/data path，不应列为 CPU Cache Stashing |
| NVIDIA/Arm/其它 SoC 厂商的私有实现 | 公开资料不统一 | 可能基于 CHI、片上 system cache 或 DPU fabric | 厂商固件、SDK、专用驱动 | 未找到足够公开资料把某一具体 NVIDIA/Arm SoC 的外部 RNIC→CPU L2/L3 作为通用产品保证；应向供应商索要 cache allocation、TPH、coherence 和 counter 证据 |

### 9.1 对厂商表的关键判断

- **Intel DDIO 是公开资料最完整的服务器 LLC I/O 方案**，但它仍受 I/O LLC way、NUMA、拓扑和设备读写方向影响。
- **AMD SDCI 更接近“面向目标 CPU 核心的 L2 ingress”**，但其公开性能数据来自特定 EPYC + Solarflare X4 + Onload/应用组合。
- **Arm CHI 是协议/IP 层能力**，实际要由 SoC 集成方把 cache controller、I/O bridge、NIC/存储控制器接通。
- **TPH 是使能机制，不是结果保证**。设备支持 TPH、Linux 支持 TPH，不意味着该平台已为目标 L2/L3 配好有效 Steering Tag。
- **NVIDIA 的公开强项是 GPU peer memory、GDS、DPU/存储卸载和 KV 存储层**，不能把这些能力包装成 Host CPU Cache Stashing。

---

## 10. 各大厂商在 DPU—SSD 直通方向的软硬件布局

### 10.1 先定义“直通”

本文把“DPU—SSD 直通”分成三类：

1. **强 P2P**：RNIC/DPU 与 SSD controller 之间的 payload DMA 绕过 Host DDR；
2. **存储卸载**：DPU 终止 NVMe/NVMe-oF、执行 CRC/压缩/加密/调度，数据路径主要在 DPU/SSD，但是否绕过 Host DDR 需要型号和拓扑确认；
3. **SSD 内计算**：SmartSSD/计算存储在 SSD 侧处理数据，减少 host CPU/DDR 参与；仍需解决 RNIC/DPU 到 SSD 的 ingress 路径。

| 厂商/技术 | 首次公开/产品年份 | 硬件布局 | 软件/协议布局 | 直通结论与限制 |
|---|---:|---|---|---|
| Linux PCI P2PDMA + NVMe CMB/Target Copy Offload | 内核持续演进；公开文档未给出单一首发年 | PCIe peer resource、NVMe CMB/PMR、设备间 DMA | Linux P2PDMA provider/client/orchestrator、NVMe target、IOMMU/ACS/topology 检查 | [Linux P2PDMA](https://www.kernel.org/doc/html/latest/driver-api/pci/p2pdma.html)给出 RNIC→NVMe CMB 的协同模型；跨 root complex、ACS、设备 allowlist 可能使路径不可用 |
| SPDK P2P/CMB | 2010s～持续演进；当前文档未固定单一首发年 | SPDK 访问 NVMe CMB/peer memory，设备间 DMA | SPDK NVMe、RDMA transport、P2P resource provider、polling | [SPDK P2P](https://spdk.io/doc/peer_2_peer.html)明确标注相关功能仍需按版本/硬件验证；不是所有 NVMe 都有可用 CMB |
| SPDK NVMe-oF RDMA zero-copy（非 P2P） | 2010s～持续演进 | RNIC↔host memory↔SSD，减少 CPU copy | SPDK NVMe-oF RDMA target/initiator | [SPDK NVMe-oF RDMA](https://spdk.io/doc/nvmf_tgt_pg.html)可减少中间软件 copy，但仍可能经过 Host memory；不能与 RNIC↔SSD P2P 混称 |
| NVIDIA GPUDirect Storage（GDS） | 2019 | NVMe/storage NIC↔GPU memory 的 DMA；CUDA/GDS 处理存储数据面 | cuFile、GDS、NVIDIA driver、P2PDMA；部分 Linux/Ubuntu/kernel/driver 版本有要求 | [GDS overview](https://docs.nvidia.com/gpudirect-storage/overview-guide/index.html)和 [troubleshooting](https://docs.nvidia.com/gpudirect-storage/troubleshooting-guide/)确认 GPU/storage peer path；这是 GPU↔SSD 重点方案，DPU↔本地 SSD 仍需具体平台验证 |
| NVIDIA BlueField-3/DOCA Storage | 2023～2025 | BlueField DPU 的 RDMA/NVMe、DMA、压缩/加密等 engine；可承担 storage target/initiator 控制面 | DOCA Storage、DOCA RDMA、DOCA Compress、SPDK/NVMe-oF、DPU firmware | [DOCA Storage](https://docs.nvidia.com/doca/sdk/DOCA-Storage-Applications/index.html)公开了 DPU storage offload 和压缩等能力；“DPU 到本地 SSD 必为 P2P”仍依赖具体 PCIe topology/产品 |
| NVIDIA BlueField-4 Inference Context Memory / CMX | 2026 公告，计划 2026 下半年供货 | BlueField-4 storage processor、flash tier、KV placement/management | DOCA、NIXL、Dynamo 及合作伙伴 KV runtime | [NVIDIA BlueField-4 公告](https://nvidianews.nvidia.com/news/nvidia-bluefield-4-powers-new-class-of-ai-native-storage-infrastructure-for-the-next-frontier-of-ai)和 [CMX](https://www.nvidia.com/en-au/data-center/ai-storage/cmx/)显示 NVIDIA 将 KVCache 数据面专门化；它是 KV 存储层/硬件卸载，不等于公开承诺的通用 host PCIe P2P |
| Intel DSA + QAT + IPU | DSA/QAT：2023 前后随第 4 代 Xeon；IPU 为 2021 时代产品线 | DSA 做 data movement/CRC/DIF/peer accelerator；QAT 做压缩/解压缩/crypto；IPU 做网络/存储基础设施卸载 | Linux/DPDK/SPDK、QAT/DSA driver、IPU SDK、NVMe-oF/存储控制面 | [Intel DSA](https://www.intel.com/content/www/us/en/products/details/processors/xeon/features/data-streaming-accelerator.html)和 [QAT](https://www.intel.com/content/www/us/en/products/docs/accelerator-engines/what-is-intel-qat.html)证明硬件 engine；[Intel IPU](https://www.intel.com/content/www/us/en/products/details/networking/ipu.html)证明存储 initiator/offload 方向，但不是单一公开的 RNIC→本地 SSD P2P 产品 |
| AMD Pensando Salina | 2024 公告；2025 起陆续供货 | DPU 模拟 NVMe VF、终止主机命令、以 NVMe/TCP 等方式访问后端；网络/安全/存储处理 | Salina firmware、NVMe virtualization、NVMe-oF、host inbox NVMe | [Salina 公告/产品资料](https://www.amd.com/en/newsroom/press-releases/2024-10-10-amd-delivers-leadership-ai-performance-with-amd-in.html)与 [Salina brief](https://www.amd.com/content/dam/amd/en/documents/pensando-technical-docs/product-briefs/pensando-salina-product-brief.pdf)确认 DPU-managed NVMe；公开资料不足以把它归为所有平台上的本地 SSD P2P |
| AMD Pensando Giglio | 2026 公开号/产品资料 | SDS/NVMe virtualization、AES-XTS、CRC32/CRC64/checksum、压缩/解压缩、SHA-3、dedup 等 | Pensando SDK、NVMe-oF RDMA/TCP、存储服务控制面 | [Giglio product brief](https://www.amd.com/content/dam/amd/en/documents/pensando-technical-docs/product-briefs/pensando-giglio-product-brief.pdf)显示了面向存储的数据处理 engine；本表把它记为 storage offload，P2P 需要按 SKU/拓扑确认 |
| Marvell OCTEON 10 DPU | 2021 | PCIe 5、DDR5、网络/安全/存储加速、可编程数据面 | DPDK、VPP、SPDK、厂商 SDK、NVMe-oF | [OCTEON 10 公告](https://investor.marvell.com/news-events/press-releases/detail/814/marvell-extends-octeon-leadership-with-industrys-first-5nm-dpus)确认 DPU/storage/security 布局；公开资料不足以证明统一的 RNIC→本地 SSD P2P+KV 解压路径 |
| Samsung/Xilinx SmartSSD CSD | 2018 首代；2020/2022 后续代 | FPGA/Arm 处理器在 SSD 侧执行压缩/解压缩和数据处理，减少 host CPU 参与 | FPGA/XRT、SmartSSD SDK、应用特定 kernel | [Samsung 2018 SmartSSD](https://news.samsung.com/global/samsung-debuts-semiconductor-innovations-at-samsung-tech-day-that-maximize-data-center-efficiencies-and-enable-ai-enterprise-and-emerging-technologies)、[2022 第二代](https://news.samsung.com/us/samsung-electronics-develops-second-generation-smartssd-computational-storage-drive-upgraded)确认 SSD 内计算；[2025 生命周期通知](https://docs.amd.com/v/u/en-US/XCN24008)提示具体 SmartSSD 文档/产品可能停止维护 |
| AMD/Xilinx PCIe P2P 生态 | 持续演进 | FPGA/PCIe endpoint 与其它设备之间无 Host RAM peer transfer | Vitis/driver、P2P routing、IOMMU/topology | [AMD/Xilinx P2P 文档](https://docs.amd.com/r/en-US/ug1315-vitis-guidance/Peer-To-Peer-Transfer)证明通用 P2P 方向；与某个 DPU、某个 NVMe SSD 的端到端支持仍需验证 |

### 10.2 直通布局的现实边界

- **“DPU 有 NVMe offload”不等于“RNIC payload 已经绕过 Host DDR 进入本地 SSD”**。前者可以只是 DPU 终止 NVMe-oF、再由 DPU/host memory 与 SSD 交互。
- **“GPU Direct Storage”不等于“DPU Direct SSD”**。GDS 公开证明的是 storage↔GPU memory 数据面；DPU↔SSD 需要另外的 P2P、NVMe provider 和拓扑证据。
- **SSD 直通的目标通常是 controller buffer/CMB/PMR，而非 NAND**。NAND 仍由 SSD controller 进行 FTL、ECC、磨损均衡和持久化。
- **跨 PCIe hierarchy domain 是最大落差之一**。同一 switch 上的 P2P 比跨 root complex 更容易；ACS 可能把 P2P 强制送回 root complex。
- **DPU-managed NVMe-oF 可以是很好的工程替代**，即使不满足严格的本地 P2P 定义；需要在指标中区分“Host DDR copy 次数”“Host CPU cycles”“PCIe P2P bytes”和“端到端持久化时延”。

---

## 11. KVCache 场景：DPU—SSD 直通 vs L2/L3 Cache Stashing

| 维度 | DPU/SSD P2P 或 storage offload | L2/L3 Cache Stashing |
|---|---|---|
| 目标 | 大 payload 数据面绕过 Host DDR/CPU | 让 CPU 很快读到新到的数据或完成标记 |
| DDR payload | 理想为 0；小控制结构除外 | 关键窗口可为 0，完整生命周期通常有 dirty writeback；容量不足时退化 |
| CPU 消耗 | 主要做队列、控制、异常处理 | CPU 直接参与 CRC、解压缩、格式化或 metadata 更新 |
| 大块 KV scalability | 好；容量由 SSD/DPU/网络决定 | 差；受 I/O LLC ways、cache thrash 和微块窗口限制 |
| CRC | DPU/RNIC/DSA/QAT/SSD engine 可 inline | CPU 读取 cache line；需要专用 engine 才能降低 CPU cycles |
| 压缩/解压缩 | 可在 DPU/SSD/FPGA/QAT 内完成，适合流水线 | CPU cache 只解决数据位置，不自动提供压缩能力 |
| CPU cache pollution | 低；payload 不必进 CPU cache | 高；大块或突发数据会驱逐业务工作集 |
| 时延 | 设备到设备路径短，适合异步持久化 | CPU 需要立即消费的小块可能非常低；SSD NAND latency 仍然存在 |
| 拓扑依赖 | PCIe switch/root complex、ACS、IOMMU、CMB/peer resource、驱动协作 | CPU cache、I/O coherence、TPH/SDCI/DDIO、NUMA、cache allocation、出站命中 |
| 软件复杂度 | P2PDMA provider/client/orchestrator、NVMe/DPU/RDMA 资源管理 | BIOS/driver/cache hint、队列核绑定、内存顺序、dirty/ownership、eviction 监控 |
| 错误语义 | DPU/SSD 可统一 CRC、decompress、NVMe、RDMA status | 必须在 CPU cache ownership、DMA completion、回收和 retry 间建立一致语义 |
| 多租户隔离 | 可在 DPU/SSD queue、namespace、QoS 层隔离 | LLC ways、CPU core、NUMA 和 I/O QoS 容易互相影响 |
| 适合的 KV 对象 | 256 KiB～GB 级 KV block、冷/温数据落盘 | block metadata、descriptor、signal、热小块、CPU 必须转换的微块 |
| 最佳组合 | DPU P2P 承载 payload，缓存只放控制面 | L2/L3 stash descriptor/状态，小窗口处理 payload |

### 11.1 推荐的混合流水线

~~~text
远端 RNIC
   │ RoCE/IB
   ▼
本端 RNIC/DPU ── CRC/解压缩/加密/格式化 engine ── P2P ── SSD controller ── NAND
   │
   └── descriptor、block id、版本、完成标记、错误码 → CPU L2/L3/LLC
~~~

该架构让：

- 大块 KV payload 不污染 CPU cache；
- CPU 可以快速读取 block metadata、状态和完成标记；
- CRC/解压缩不必增加多次 DDR 往返；
- 遇到不支持 P2P 的拓扑时，可以把 payload fallback 到 DDR；
- 可把 L3 Stashing 当作低时延 fallback 或小块优化，而不是系统唯一数据面。

---

## 12. 除直通与 Cache Stashing 外，值得考虑的加速方案

### 12.1 硬件数据处理

1. **DPU inline CRC、压缩/解压缩、加密**：将校验和格式处理放在数据进入 Host DDR 之前；适合固定压缩格式、可流式处理的 KV block。
2. **Intel DSA/QAT**：DSA 负责数据搬运、CRC/DIF、peer accelerator movement；QAT 负责压缩/解压缩和 crypto；即使没有完整 DPU→SSD P2P，也可减少 CPU copy/cycles。
3. **SSD 内计算**：SmartSSD/FPGA/计算存储在 SSD 侧完成解压缩、过滤、重排；适合存储格式稳定、查询/加载模式可预测的场景。
4. **FPGA/可编程 NIC**：把 KV block framing、checksum、compression header、流量整形放入靠近 RNIC 的 pipeline。

### 12.2 数据通路与内存层次

1. **NVMe-oF RDMA zero-copy**：不一定是 endpoint P2P，但可消除 host software copy；将“是否绕过 Host DDR”和“是否绕过 CPU copy”分开评估。
2. **GPUDirect RDMA/Storage**：GPU 直接与 RNIC/SSD 交换 KV payload，适合 GPU-side prefetch、KV restore 和异步 offload。
3. **CXL memory pooling/扩展**：把温 KV 放到可扩展内存池，减少 SSD round-trip；这是容量/分层方案，不应误报为低时延 Cache Stashing。
4. **NVMe CMB/PMR 与 P2P DMA**：适合作为设备间中转资源，但要以具体 SSD controller 的 CMB、DMA direction 和内核 allowlist 为准。
5. **微块流水线**：把 block 分成 1～几十 MiB 的 chunk，分别做接收、CRC、解压、落盘和 completion；让缓存、DPU engine、PCIe queue 和 SSD queue 可重叠。

### 12.3 KVCache 算法与 runtime

1. **KV 量化/压缩/分层**：FP16→FP8/INT8/INT4、按层/按 head 选择精度、冷热分层，先降低需要搬运和落盘的 R。
2. **Prefix sharing / dedup / content-addressed block**：相同前缀或相同 KV block 只落盘一次，减少网络、DDR、SSD 写入和 NAND 写放大。
3. **GPU-resident metadata**：把 block table、version、ownership 和 ready flag 放在 GPU 可见内存，避免频繁 CPU round trip；需要严格使用 CUDA stream wait/signal、NVSHMEM 或等效同步语义。
4. **异步预取与持久化**：将 KV 传输、解压、GPU compute、SSD flush 通过 credit/backpressure 连接，而不是在每个 block 上同步等待。
5. **vAttention/虚拟内存管理类方案**：通过更灵活的 GPU memory mapping 和 kernel gather 机制改善 KV 管理；它解决的是 GPU 侧布局/管理问题，与 RNIC→SSD P2P 或 L3 Stashing 是正交的。
6. **队列与亲和性优化**：固定 RNIC RX queue、DPU core、SSD queue、CPU consumer 和 NUMA node；对 Stashing 和 P2P 都常常比微调单个 API 更重要。

---

## 13. 验证方案与建议的实验矩阵

### 13.1 两条最值得先做的原型

**原型 A：DPU/RNIC→SSD P2P**

- 选同一 PCIe switch 下的 RNIC/DPU 与支持 CMB/peer resource 的 NVMe；
- 先关闭压缩，仅测 payload P2P；
- 再加入 CRC inline；
- 再加入解压缩，比较“压缩后落盘”和“解压后落盘”；
- 记录 Host DDR bytes、Host CPU cycles、DPU engine utilization、SSD latency、RDMA CQE/NVMe CQE；
- 用故意打断 P2P 的拓扑作为 fallback 对照。

**原型 B：RNIC→L3→CPU→SSD 微块**

- 选支持 DDIO 或 SDCI 的特定平台；
- 调整微块大小，逐步超过 I/O LLC 热窗口；
- 比较 CPU 立即消费、延迟消费、突发消费三种情况；
- 分别测 CRC-only、decompress-only、CRC+decompress fused/separate；
- 再验证 SSD 出站 DMA 是否真的能够命中 cache，而不是只观测到入站写分配；
- 对比 L3 hit、LLC eviction、IMC read/write 和端到端时延。

### 13.2 关键指标

| 类别 | 指标 |
|---|---|
| DDR | IMC read/write bytes、读写带宽、读写队列、峰值/平均/尾延迟 |
| Cache | LLC occupancy、I/O way 使用、hit/miss、eviction、snoop/ownership |
| PCIe | P2P bytes、root complex hairpin、ACS、switch port throughput、completion latency |
| RNIC/RDMA | packet loss/retry、QP throughput、CQE latency、MR/ODP faults、CPU cycles |
| DPU | CRC/compress/decompress engine throughput、queue depth、backpressure、错误率 |
| SSD | read/write throughput、queue depth、CMB usage、controller latency、NAND write amplification、GC |
| 应用 | KV block end-to-end p50/p99/p999、GPU stall、tokens/s、恢复时间、功耗 |

### 13.3 决策门

1. 若同一平台有稳定的 RNIC/DPU→SSD P2P 且 DPU/SSD 能处理 CRC/解压缩：**数据面采用 P2P**。
2. 若没有 P2P，但 CPU 必须处理且微块可以稳定留在 LLC 热窗口：**采用 L3 Stashing + 微块流水线**。
3. 若 payload 大、CPU 不消费、L3 会驱逐：**关闭 payload Stashing，采用 DDR/zero-copy/硬件搬运的可预测路径**。
4. 无论选择哪条路径：**descriptor、completion、block metadata 和错误状态可以单独使用 L2/L3 Stashing**。

---

## 14. 风险、限制与容易误判的地方

1. **L3 的“0 DDR”是窗口性结论**：它描述的是 cache line 从 I/O 到消费者之间没有发生 DDR payload transaction；不代表 dirty line 永远不会回写。
2. **Cache 命中不是由入站路径单独决定的**：必须同时证明出站 SSD DMA/RNIC DMA 的一致性读能够取得该 line。
3. **大块 KV 不能整体 stash**：20 GiB 的工作集远超 LLC；必须微块化，否则结果通常接近或差于 DDR baseline。
4. **application CRC 与链路 CRC 不同**：RNIC 的 FCS/ICRC 由设备处理；CPU application CRC 是否读取 payload、校验压缩前还是解压后，直接决定 DDR read 计数。
5. **解压缩是否融合非常关键**：CRC 与解压缩对压缩输入做单遍流式读取时，基线为 2+2α；两遍独立读取则为 2+3α。
6. **“直通”不等于“写 NAND”**：绝大多数 P2P 是把数据交给 SSD controller/CMB/内部 buffer，由 SSD controller 完成 NAND 事务。
7. **P2P 不是必然可用**：同一 PCIe switch、ACS、IOMMU、root complex、BAR/CMB、设备驱动、Linux allowlist 任何一项不满足，都可能导致 fallback。
8. **Cache Stashing 不是透明的 QoS 隔离**：I/O ways、LLC occupancy、CPU NUMA、NIC/SSD 队列之间会互相干扰；多租户场景要测尾延迟。
9. **性能数字必须回到实机**：本地讨论稿中的 DDR/LLC/RDMA 带宽和 12 ns/80 ns 等延迟是讨论用估计值，本文不把它们当作通用硬件常数。
10. **公开发布年份不等于大规模可用年份**：公告、sample、GA、驱动支持和云上可用时间可能不同；表中已尽量分别标记。

---

## 15. 最终建议

对于 RNIC/DPU 到 SSD 的 KVCache 落盘，建议按以下优先级设计：

1. **先做大 payload 的 DPU/SSD 数据面**：P2P 或 storage offload；CRC、解压缩、加密和 framing 尽量在 DPU/SSD/专用 accelerator 完成。
2. **再做控制面 Cache Stashing**：把 descriptor、block id、version、完成标记、错误码和小型 metadata 定向到消费核 L2/L3。
3. **只有在 CPU 必须处理 payload 时才试验 L3 Stashing**：严格采用微块、same-NUMA/same-CCD、及时消费，并用 IMC/CHA/IIO counter 验证，而不是仅看软件吞吐。
4. **建立三种 fallback**：P2P 主路径、L3 微块路径、Host DDR 兼容路径；每种路径都要保留同样的 CRC、错误、持久化和 completion 语义。
5. **把“低 DDR”与“低端到端时延”分开优化**：P2P 可能降低 DDR 和 CPU，但 SSD NAND、DPU engine 和队列排队仍会主导时延；L3 可能降低 CPU load-to-use 延迟，却不一定改善大块持久化吞吐。

---

## 16. 参考资料

### 本地讨论稿

- IO stash技术分析.md
- IO.md
- stash crc on off ddr write and read.md
- RDMA 机制讨论.md
- RDMA 机制讨论 补充稿.md

### Cache Stashing、TPH 与 CPU I/O

- [Intel Data Direct I/O Technology](https://www.intel.com/content/www/us/en/io/data-direct-i-o-technology.html)
- [Intel DDIO Analysis and Performance Monitoring](https://www.intel.com/content/www/us/en/developer/articles/technical/ddio-analysis-performance-monitoring.html)
- [Intel VTune: Effective Utilization of Intel DDIO](https://www.intel.com/content/www/us/en/docs/vtune-profiler/cookbook/2024-2/effective-utilization-of-intel-ddio-technology.html)
- [Intel Xeon DDIO support matrix](https://www.intel.com/content/www/us/en/support/articles/000087975/processors/intel-xeon-processors.html)
- [Linux PCI TPH Support](https://docs.kernel.org/7.1/PCI/tph.html)
- [Arm AMBA 5 CHI protocol enhancements](https://developer.arm.com/community/arm-community-blogs/b/soc-design-and-simulation-blog/posts/introducing-new-amba-5-chi-protocol-enhancements?pifragment-27083=2%3Fpifragment-27083%3D2)
- [AMD SDCI for Onload](https://docs.amd.com/r/en-US/ug1586-onload-user/SDCI)
- [AMD Solarflare X4 SDCI white paper](https://www.amd.com/content/dam/amd/en/documents/processor-tech-docs/white-papers/x4-sdci-whitepaper.pdf)
- [AMD DPDK performance tuning guide](https://docs.amd.com/api/khub/documents/TPtxZn7Ajbl4RMxb9StmzA/content)

### P2P、RDMA、DPU 与 SSD

- [Linux PCI Peer-to-Peer DMA](https://www.kernel.org/doc/html/latest/driver-api/pci/p2pdma.html)
- [SPDK Peer-to-Peer DMA](https://spdk.io/doc/peer_2_peer.html)
- [SPDK NVMe-oF RDMA target](https://spdk.io/doc/nvmf_tgt_pg.html)
- [NVIDIA GPUDirect RDMA](https://docs.nvidia.com/cuda/gpudirect-rdma/?ncid=ref-inc-938520)
- [NVIDIA GPUDirect Storage overview](https://docs.nvidia.com/gpudirect-storage/overview-guide/index.html)
- [NVIDIA GPUDirect Storage troubleshooting](https://docs.nvidia.com/gpudirect-storage/troubleshooting-guide/)
- [NVIDIA DOCA Storage Applications](https://docs.nvidia.com/doca/sdk/DOCA-Storage-Applications/index.html)
- [NVIDIA BlueField-4 Inference Context Memory Storage announcement](https://nvidianews.nvidia.com/news/nvidia-bluefield-4-powers-new-class-of-ai-native-storage-infrastructure-for-the-next-frontier-of-ai)
- [NVIDIA CMX](https://www.nvidia.com/en-au/data-center/ai-storage/cmx/)
- [Intel Data Streaming Accelerator](https://www.intel.com/content/www/us/en/products/details/processors/xeon/features/data-streaming-accelerator.html)
- [Intel QuickAssist Technology](https://www.intel.com/content/www/us/en/products/docs/accelerator-engines/what-is-intel-qat.html)
- [Intel IPU](https://www.intel.com/content/www/us/en/products/details/networking/ipu.html)
- [AMD Pensando Salina announcement](https://www.amd.com/en/newsroom/press-releases/2024-10-10-amd-delivers-leadership-ai-performance-with-amd-in.html)
- [AMD Pensando Salina product brief](https://www.amd.com/content/dam/amd/en/documents/pensando-technical-docs/product-briefs/pensando-salina-product-brief.pdf)
- [AMD Pensando Giglio product brief](https://www.amd.com/content/dam/amd/en/documents/pensando-technical-docs/product-briefs/pensando-giglio-product-brief.pdf)
- [Marvell OCTEON 10 announcement](https://investor.marvell.com/news-events/press-releases/detail/814/marvell-extends-octeon-leadership-with-industrys-first-5nm-dpus)
- [AMD/Xilinx PCIe Peer-to-Peer Transfer](https://docs.amd.com/r/en-US/ug1315-vitis-guidance/Peer-To-Peer-Transfer)
- [Samsung first SmartSSD announcement](https://news.samsung.com/global/samsung-debuts-semiconductor-innovations-at-samsung-tech-day-that-maximize-data-center-efficiencies-and-enable-ai-enterprise-and-emerging-technologies)
- [Samsung second-generation SmartSSD](https://news.samsung.com/us/samsung-electronics-develops-second-generation-smartssd-computational-storage-drive-upgraded)
- [SmartSSD documentation discontinuation notice](https://docs.amd.com/v/u/en-US/XCN24008)

### GPU/KV runtime 与同步

- [CUDA GPUDirect RDMA memory ordering](https://docs.nvidia.com/cuda/archive/13.0.1/gpudirect-rdma/index.html)
- [CUDA stream memory wait/write operations](https://docs.nvidia.com/cuda/cuda-driver-api/group__CUDA__MEMOP.html)
- [NVSHMEM signal operations](https://docs.nvidia.com/nvshmem/api/gen/api/signal.html)
- [vAttention: Dynamic Memory Management for Serving LLMs](https://dl.acm.org/doi/10.1145/3676641.3711989)

---

## 附录：一页式架构决策表

| 问题 | 若答案为“是” | 推荐 |
|---|---|---|
| payload 是否达到 MB/GB，并且 CPU 不需要逐字节处理？ | 是 | RNIC/DPU→SSD P2P 或 storage offload |
| CRC/压缩/解压缩是否有 DPU/DSA/QAT/SSD engine？ | 是 | 在设备侧 inline，避免 Host DDR pass |
| CPU 是否必须立即读取 payload，并且微块能留在 LLC 热窗口？ | 是 | L2/L3 Cache Stashing + 微块流水线 |
| 仅需 CPU 读取 descriptor/状态/完成标记？ | 是 | 只 stash 控制面，不 stash 大 payload |
| RNIC 与 SSD 不在兼容的 P2P 拓扑，且无 DPU storage engine？ | 是 | Host DDR fallback + zero-copy/轮询/大页/队列亲和性优化 |
| 数据主要在 GPU HBM，CPU 不应成为中转？ | 是 | GPUDirect RDMA/Storage、NVLink-C2C、GPU-side signaling |
| KV 的主要瓶颈是容量/重复数据而非搬运？ | 是 | 量化、压缩、prefix sharing、dedup、CXL/分层存储 |
