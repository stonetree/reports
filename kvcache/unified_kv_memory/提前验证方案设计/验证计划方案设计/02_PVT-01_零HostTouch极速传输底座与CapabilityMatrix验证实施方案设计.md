# PVT-01：Host CPU 零数据拷贝传输底座与硬件能力矩阵验证实施方案设计
## —— Mooncake TransferEngine 数据路径核对与国产硬件直达传输能力验证

> **公共执行契约**：本项严格遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个测试 `run_id` 必须在运行前完整固化数据块尺寸 (Payload)、传输路径、代码包版本、物理拓扑、队列深度、预热轮次、测量采样数及目标证据等级；实测产出必须完整保留底层硬件实际完成字节、异常失败事件、Host CPU 探针原始输出及规范的判定状态枚举。在 DEMO 演示模式下，严禁依据名义 Payload 理论值直接计算 Direct 路径的实测传输带宽。

> **验证范围声明**：在当前受控的原型验证工程中，`raw_trans_bench.cc` 仅在 `host_memcpy` 分支中实际执行了本地 `memcpy` 内存搬运；`urma_direct`、`ubmem_direct`、`nvme_direct` 与 `socket_tcp` 等分支目前尚未提交底层的真实硬件设备、物理网络或磁盘 I/O 操作，统一输出包含 `DEMO / DEMO_ONLY` 标记或实际完成字节为 0 的演示记录。同时，`host_touch_monitor.py` 当前仅挂载了 `kprobe:memcpy` 与 `kprobe:memmove` 内核探针，尚未覆盖完整的用户态、驱动层及内核空间多锚点，亦不会自动产出 `zero_touch_verified=true` 的最终判定。因此，现有受控源码仅用于验证参数传递、数据结构格式与异常拦截流程，不可直接作为证明 Host CPU 零数据拷贝、真实 DMA 直达或硬件能力矩阵已经成立的生产级依据。

> **术语速查**：
> - **Host CPU 零数据拷贝**：CPU 仅负责下发控制指令，不参与正文数据搬运，Host Payload Touch Bytes 严格为 0；
> - **DMA**：Direct Memory Access（直接内存访问：由硬件设备控制器直接读写目标内存，不经过 CPU 中转）；
> - **P2P DMA**：Peer-to-Peer DMA（对等直接内存访问：硬件设备之间直接跨总线读写显存或设备内存）；
> - **HBM**：High Bandwidth Memory（高带宽显存）；
> - **eBPF**：Extended Berkeley Packet Filter（扩展伯克利数据包过滤器：Linux 内核中的高性能可编程动态观测机制）；
> - **kprobe**：内核动态函数入口探针；
> - **uprobe**：用户态动态函数入口探针；
> - **URMA**：通用远程直接内存访问（用户态的高性能 RDMA 驱动接口与通信协议）；
> - **UBMEM**：统一总线内存直通共享协议（支持跨节点与异构设备间直接共享内存地址空间的底层通信协议）；
> - **NVMe**：Non-Volatile Memory Express（非易失性高速内存接口协议）；
> - **io_uring**：Linux 异步高性能 I/O 框架，支持批量提交与完成事件通知。

> **验证 ID**：PVT-01
> **验证名称**：Host CPU 零数据拷贝传输底座与硬件能力矩阵验证
> **验证优先级**：**🟡 P1 级（底座支撑项）**
> **对应验证阶段**：**E1（核心数据路径打通与多卡状态同步）**
> **证伪标记**：否（底层传输能力确认）
> **主关联 IR**：`IR-01-06`, `IR-01-08`, `IR-01-09`, `IR-01-12`
> **核心 SRS / SR23 锚点**：
> - SRS：`L4-MC-HIER-STORE-001`, `L3-MS-Tiering-038`, `L4-HW-HostPayloadTouchBudget-076`, `L4-FT-PathIntegrityPolicy-077`
> - SR23：`SR23-01-06-01`, `SR23-01-07-01`, `SR23-01-08-01`, `SR23-01-09-01`, `SR23-01-12-01`
> **配套源码**：[`./原型验证代码/PVT-01/`](./原型验证代码/PVT-01/)
> **开源基线版本**：Mooncake TransferEngine `f90ae691f109e49a60920e0c8abbf7e572826d8c`。正式测试结果必须独立记录现场驱动、NPU、网卡、NVMe SSD、内核及观测工具版本，严禁仅引用本文默认 Commit。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统系统视角：零数据拷贝约束解决的是什么问题

在高性能网络与存储系统（如 Kafka、Netty、DPDK 及分布式存储）的经典设计中，系统通常严格划分为控制面与数据面：

```text
控制面：应用层构建并下发描述符、更新队列门铃 (Doorbell)、指定源/目的物理地址与传输长度
数据面：网卡/SSD/DMA 控制器直接在硬件内存间搬运正文数据，完全不经过 CPU 寄存器与主机内存中转
```

一旦数据面发生路径退化，演变为“设备将数据先读入主机内存 (Host DDR)，再由 Host CPU 执行 `memcpy` 搬运至目标显存”，CPU 的参与度与耗时将随着传输 Payload 字节数呈线性增长。这种退化不仅会直接导致 CPU 利用率飙升，更会剧烈争用 Host DDR 内存总线带宽、L3 共享缓存及内存控制器通道，进而严重干扰同机部署的微秒级分布式调度、RPC 通信以及在线推理的控制面稳定性。

因此，“Host CPU 零数据拷贝”并非抽象的性能口号，而是一组必须在物理层面同时满足的硬核工程约束：

1. 底层硬件设备的物理完成量必须确切证明正文数据由目标 DMA 链路直接搬运；
2. 在被测采样时间窗口内，Host CPU 严禁执行任何正文数据的内存拷贝指令；
3. 动态探针的观测覆盖范围、被测进程 PID、采样时间窗与硬件完成中断必须形成严格的一一对应；
4. 测得的有效带宽、时延分布及 CPU 占用率，必须在相同 Payload 尺寸、队列深度及物理设备条件下与软件中转基准展开严密对照。

### 0.2 大模型推理中的对应物理场景

KVCache（大模型注意力键值缓存，即自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免后续 Token 生成时重复计算注意力）在分布式推理生命周期中，需要在本地 NPU HBM（高带宽显存）、远端节点显存、本地 NVMe SSD 大容量介质以及高速网络之间频繁流转。若每次数据移动都依赖 Host DDR 进行中转拷贝，随着上下文长度与并发请求的攀升，CPU 计算能力与主机内存总线将迅速沦为系统的隐性瓶颈。

本项旨在系统验证的核心目标物理路径包括：

- **跨节点 URMA/UBMEM 直达路径**：网卡 DMA 直接跨网络访问可注册的 NPU HBM 或加速器显存；
- **本地 NVMe Direct 直达路径**：NVMe 控制器通过 `io_uring` 固定缓冲区或现场等价接口，直接在 SSD 物理扇区与加速器显存间读写数据；
- **软件中转对比基线**：显式经由 Host DDR 与 Host CPU `memcpy` 执行数据中转，用于量化传统软件搬运的额外开销；
- **TCP/IP 传统网络基线**：经由标准 Linux Socket 协议栈与内核网络协议中转，作为传统分布式传输的基准对照。

上述路径代表系统架构设计的物理目标通路，并不代表当前原型测试桩 `raw_trans_bench.cc` 已全部完成硬件驱动对接。在开展实测前必须先执行严密的源码审计，依据实际支持能力准确判定本轮实验的证据等级（DEMO、LAB 或 MEASURED）。

### 0.3 什么是硬件能力矩阵

硬件能力矩阵（CapabilityMatrix，即在运行时自动探测各通信链路的带宽、时延、CPU 开销、物理完成量及协议支持状态的参数表，供调度算法使用）是上层调度引擎在微秒级时间内决策采用 Direct-View（远端直读，即直接通过高速链路读取远端显存中的 KV 数据，不产生本地显存拷贝）、Copy-to-HBM（拷贝到本地显存，即通过 DMA 将远端 KV 数据完整搬运至本地高带宽显存 HBM）还是回退至本地重算的关键输入依据。

硬件能力矩阵至少需要回答以下关键物理问题：

| 核心物理问题 | 能力矩阵必须记录的字段 |
|---|---|
| 该路径是否真实完成了物理数据搬运 | `actual_completed_bytes`（实际完成字节数）、设备完成事件凭证、`actual_path`（实测物理路径） |
| 硬件底层的数据搬运速率表现如何 | 有效物理带宽 (`bandwidth_gbps`)、P50/P99 时延分布、Payload 尺寸、队列深度、传输方向 |
| Host CPU 是否参与了正文数据搬运 | 探针覆盖符号列表、探针拦截次数、搬运字节数 (`host_touch_bytes`)、CPU 占用率 |
| 该实测结论适用于何种硬件环境 | 硬件设备型号、驱动版本、节点拓扑、代码包版本及配置哈希 |
| 该路径当前是否可安全供调度器使用 | `evidence_level`（证据等级）、`status`（状态枚举）、`invalid_reason`（无效原因）、时间戳与有效期 |

对于缺乏实际物理完成量或未取得完整探针证据的能力条目，矩阵中对应字段必须显式保留为 `null` 并注明原因；严禁使用默认理论带宽或以“未观测到”为由随意填报为 0。

### 0.4 eBPF 探针为什么不能只看应用日志

应用层日志仅能反映程序“计划提交”了某项传输任务，无法证明底层第三方运行库、驱动程序或隐式回退路径中是否偷偷执行了主机内存的二次复制。基于 Linux 内核的 eBPF 机制能够在内核函数入口与用户态动态库符号处精准捕获内存操作，但运用该工具时必须明晰其技术边界：

- 当前受控脚本仅挂载了 `kprobe:memcpy` 与 `kprobe:memmove`，尚未覆盖 glibc 用户态 `uprobe`、AVX 向量化指令、内核 `copy_to_user` 以及硬件加速卡专有的拷贝符号；
- 即使在采样窗口内未捕获到任何拷贝事件，也必须首先核实探针是否成功加载挂载、目标 PID 是否精确匹配、采样周期是否完整覆盖传输全过程以及输出解析是否正常；
- “探针未捕获到事件”绝不直接等同于“Host Payload Touch Bytes 严格为 0”，必须同时结合底层硬件的物理完成量与执行路径凭证共同交叉验证；
- 当前脚本在输出 JSON 中仅机械记录 `status=OK` 与 `host_touch_bytes` 数值，并不会根据搬运字节数自动做出零拷贝判定，亦未导出完整的覆盖率证明。

---

## 1. 验证目标与交付结论定义

### 1.1 现实前因痛点与待验证核心命题

1. **命题一：目标 Direct 路径是否真正绕过 Host CPU 正文数据搬运**。在 URMA、UBMEM 及 NVMe Direct 路径下，底层硬件实际完成搬运字节数大于 0，且在全量探针覆盖的采样窗口内，Host CPU 正文数据拷贝字节数严格为 0；
2. **命题二：目标路径的物理带宽与尾部时延是否显著优于软件中转**。在相同 Payload 尺寸、队列深度、硬件设备及拓扑条件下，严密对比 Host Memcpy、Socket TCP 与 Direct 路径的有效物理带宽、P50/P99 时延及 Host CPU 占用率；
3. **命题三：硬件能力矩阵是否具备作为生产级调度输入的完整性与可靠性**。每条传输链路必须完整记录支持状态、物理参数、版本凭证、物理拓扑、实测路径及有效期限；关键字段缺失的链路严禁被调度器误判为可用路径；
4. **命题四：目标路径是否达到预先固化的候选工程准入门槛**。候选准入门槛包括：网络有效线速达成率 $\ge 80\%$、NVMe 直达顺序读写带宽不低于设备标称峰值的 80%、Host CPU 正文拷贝字节数严格为 0 且 Host CPU 占用率 $< 5\%$。上述门槛为测试启动前冻结的判定标准，非现有测试桩代码的预设输出。

### 1.2 交付物与结论边界

每个正式的 `run_id` 必须至少交付以下结构化资产：

1. **《Host CPU 正文拷贝探针证据表》**：详细记录探针类型、覆盖符号列表、目标 PID、采样时间窗、拦截调用次数、搬运字节数、解析状态及未覆盖范围清单；
2. **《五类传输路径性能对照表》**：完整覆盖 URMA Direct、UBMEM Direct、NVMe Direct、Host Memcpy、Socket TCP 的 Payload 尺寸、队列深度、传输方向、实测带宽、P50/P99 时延、CPU 占用率及实际完成字节数；
3. **`capability_matrix.json`**：输出包含版本凭证、拓扑结构与有效期绑定的标准化硬件能力矩阵条目；
4. **标准证据包**：包含 `manifest.json`、`environment.json`、原始数据 CSV、探针原始 stdout/stderr 输出、系统日志及分析摘要；
5. **分项技术判定结论**：针对各传输链路独立输出 `GO`、`CONDITIONAL`、`NO-GO`、`NOT-SUPPORTED` 或 `INVALID-EVIDENCE` 结论，严禁将某单项通路的测试成绩混淆外推覆盖全量路径。

---

## 2. 实验方案与测试矩阵设计

### 2.1 五类路径与公平对照逻辑

| 传输路径模式 | 目标物理机制与路径含义 | 当前 `raw_trans_bench.cc` 实际执行行为 | 当前默认证据等级与状态 |
|---|---|---|---|
| `urma_direct` | URMA 网卡通过 RDMA DMA 直接读写远端设备显存或 NPU HBM | 未执行内存复制，未向网卡提交操作，仅执行空汇编屏障占位 | `DEMO / NOT-SUPPORTED` |
| `ubmem_direct` | UBMEM 统一总线协议直通访问跨节点与异构设备内存 | 未执行内存复制，未调用 UBMEM 驱动，仅执行空汇编屏障占位 | `DEMO / NOT-SUPPORTED` |
| `nvme_direct` | NVMe 控制器通过 Direct I/O 直接在 SSD 与显存间读写数据 | 未打开 NVMe 设备，未调用 `io_uring`，实际完成字节数为 0 | `DEMO / NOT-SUPPORTED` |
| `host_memcpy` | Host DDR / CPU `memcpy` 软件内存中转基准对比路径 | 真实调用 `std::memcpy` 执行数据搬运，输出完成字节与 CPU 耗时 | `DEMO`（接入真实源/目的设备后可升级为实测） |
| `socket_tcp` | 基于 Linux 标准 TCP/IP Socket 协议栈的传统网络传输路径 | 同样仅执行空汇编屏障占位，未创建真实网络套接字 | `DEMO / NOT-SUPPORTED` |

开展公平 A/B 对照测试时，必须严格保持硬件设备、Payload 尺寸、队列深度、测量循环次数、内存布局、CPU 绑核、资源配额、预热策略及统计口径的高度一致，测试中仅允许变更目标传输路径或被测代码包版本。仅在命令行中将 `--mode` 参数从 `urma_direct` 修改为 `ubmem_direct`，并不会自动切换底层物理驱动。

### 2.2 数据块与队列深度矩阵

| 测试维度 | 正式计划取值 | 当前源码支持情况 | 物理测试目的与机制分析 |
|---|---|---|---|
| Payload 尺寸 | 64KB、256KB、1MB、4MB、16MB、64MB、256MB、1GB | `--payload-bytes` 支持传入任意单值；默认示例为 64MB | 小数据块重点观察链路固定时延与开销，大数据块重点观测总线带宽爬坡与饱和吞吐 |
| 队列深度 (QD) | 1、2、4、8、16、32、64 | `--qd` 支持传入任意单值 | 在真实硬件路径中对应底层并发提交深度；当前测试桩仅为循环执行 |
| 测量循环轮数 | 运行前统一冻结；建议预热与测量轮次严格隔离 | `--loops` 支持传入单值；默认示例为 10 轮 | 构造足量的原始事件样本集，支撑权威统计分位数计算与重复性验证 |
| 传输方向 | Read（读）、Write（写）、双向混流 | 当前源码未提供方向参数，仅在本地分配 source/target 内存 | 正式实测必须由真实驱动扩展支持，并记录物理完成事件的方向 |
| 传输模式 | URMA、UBMEM、NVMe Direct、Host Memcpy、Socket TCP | `--mode` 支持接收上述模式字符串，但仅 `host_memcpy` 实际搬运数据 | 严禁将字符串参数支持直接等同于底层物理硬件已具备直达能力 |

### 2.3 Host CPU 与设备能力门槛

| 核心评估指标 | 候选准入门槛 | 必须具备的客观物理证据 | 当前代码支持状态 |
|---|---:|---|---|
| 网络有效线速达成率 | $\ge 80\%$ | 物理网络标称线速、底层实际完成字节数、稳态测量时长、实测物理路径凭证 | 当前未接入真实网络驱动 |
| NVMe 直达顺序带宽 | $\ge 80\%$ 设备标称峰值 | 磁盘设备型号、读写方向、实际完成字节数、`io_uring` 或硬件完成事件 | 当前未对接 NVMe 驱动 |
| Host CPU 正文拷贝字节 | 严格 $= 0$ | 完整的 kprobe/uprobe 或等价动态探针、目标 PID、采样时间窗、设备完成量 | 当前探针覆盖不全，无法关闭判定 |
| Host CPU 占用率 | 候选门槛 $< 5\%$ | 同场次进程 CPU 时间、墙上时间、专业采样工具数据及 CPU 绑核记录 | 当前仅记录单进程 CPU 时间，且非 memcpy 路径均为占位 |

### 2.4 环境与证据矩阵

| 运行环境级别 | 验证核心目的 | 最低前置条件 | 允许产出的证据结论 |
|---|---|---|---|
| W0 单机/本地桩 | 验证参数解析、CSV 导出、探针异常分支及能力矩阵结构格式的完整闭环 | Python 环境、C++ 编译器、可选 bpftrace 工具 | 仅可产出 `DEMO` 级别的工作流有效性结论 |
| W1 局部设备实测 | 绑定单台硬件设备或单条物理链路，验证局部传输路径与探针行为 | 具备可用 NPU、高性能网卡、NVMe SSD、原厂驱动 SDK、完成事件及探针环境 | 可产出绑定特定设备与拓扑的 `LAB` 局部结论 |
| W2 跨节点完整路径 | 正式关闭 E1 阶段 Host CPU 零数据拷贝底座与硬件能力矩阵的准入命题 | 2 节点真实集群、真实硬件链路、拓扑记录、高精度时钟同步及多轮重复实测 | 满足全量证据闭环后，可产出 `MEASURED` 生产级结论 |

---

## 3. 物理模型与统计口径

### 3.1 数据路径分解

在理想的 Host CPU 零数据拷贝 Direct 直达路径中，控制面与数据面的协同交互时序为：

```text
Host CPU (控制面)   : 内存分配与注册 → 描述符编译与聚合 → 提交硬件工作队列 (SQ) → 监听完成事件 (CQ)
硬件设备 (数据面)   : 源端设备显存 ──────────── PCIe / 网络 DMA 直达 ────────────► 目的端设备显存
```

而在传统的软件中转路径中，数据流转必须跨越主机内存：

```text
源端设备 / 外部网卡 ──PCIe DMA──► 主机内存 Host DDR ──Host CPU memcpy──► 目的端显存 HBM
```

单次传输的端到端耗时可按底层物理阶段分解为：

$$
T_{path}=T_{submit}+T_{queue}+T_{DMA}+T_{completion}+T_{fence}
$$

若存在 Host CPU 的介入中转，则必须额外显式记录主机内存拷贝的开销：

$$
T_{host\_copy}=T_{read\_DDR}+T_{memcpy}+T_{write\_DDR/HBM}
$$

上述物理模型用于规范底层细粒度事件的采集口径，严禁在缺乏硬件物理完成事件的前提下仅凭数学公式主观推断真实 DMA 已经发生。

### 3.2 有效带宽与线速达成率

有效传输带宽必须基于底层硬件实际完成搬运的物理字节数计算，严禁直接采用命令行传入的名义 Payload 参数替代：

$$
BW_{effective}=rac{actual\_completed\_bytes	imes 8}{T_{measure}	imes 10^9}\;\mathrm{Gbps}
$$

网络物理线速达成率的计算公式为：

$$
\eta_{wire}=rac{BW_{effective}}{BW_{line\_rate}}	imes 100\%
$$

在当前 `raw_trans_bench.cc` 实现中，Direct 模式输出的 `actual_completed_bytes` 固定为 0；因此，严禁使用 `payload_bytes × queue_depth × loops` 进行人工虚构补齐来计算带宽。

### 3.3 Host CPU 零数据拷贝的证据条件

要得出“Host CPU 正文数据拷贝严格为 0”的判定结论，必须同时满足以下五项硬核证据：

1. 动态观测探针成功加载并挂载，完整覆盖被测进程在传输过程中可能调用的全部内核及用户态内存复制入口；
2. 探针的目标监控 PID、采样时间窗口与实际数据传输时间窗严格对应；
3. `host_touch_bytes` 的原始采样值确切为 0，且该数值并非字段缺失或解析失败后的默认填充值；
4. 底层硬件记录的 `actual_completed_bytes > 0`，并具备设备物理完成事件或可审计日志证明数据已发生真实搬运；
5. 不存在未被探针监控的第三方隐式运行库、驱动私有拷贝或同步内存回退路径。

因此，若出现 `host_touch_bytes=null`、探针执行报错、输出缺失字节统计字段或 Direct 模式下实际完成字节数为 0 等任一情形，测试结果一律判定为 `INVALID-EVIDENCE` 或 `NOT-SUPPORTED`，严禁给出零数据拷贝通过的结论。

### 3.4 CPU 占用和尾部时延

进程维度的 CPU 占用率可按如下公式进行统计：

$$
CPU\% = rac{T_{process\_cpu}}{T_{wall}}	imes 100\%
$$

该指标仅描述被测进程在采样窗口内消耗的 CPU 时间比例，无法涵盖系统级的内存总线压力、特定 CPU 核心绑定位、后台干扰进程以及硬件中断开销。正式评测报告必须完整给出 CPU 统计方法、绑核策略、采样窗口、Payload 尺寸、队列深度以及 P50/P99 时延指标；仅凭单一的平均 CPU 占用率不足以关闭零数据拷贝验证命题。

---

## 4. 测试工具、当前实现边界与正式扩展要求

### 4.1 当前受控工程目录与运行方式

```text
原型验证代码/PVT-01/
├── Makefile
├── raw_trans_bench.cc           # 当前包含 host_memcpy 真实拷贝与其余占位分支的微基准程序
├── host_touch_monitor.py        # 基于 bpftrace 挂载内核 kprobe 的 Host 内存拷贝监控工具
└── export_capability_matrix.py  # 聚合基准 CSV 与探针输出并导出能力矩阵的解析工具
```

当前 `Makefile` 仅采用 `g++ -O3 -std=c++17 -pthread -Wall` 进行编译，未链接 URMA、UBMEM、CANN 驱动、`liburing` 或专用 Socket 库。当前可复现的 W0 构建与运行命令为：

```bash
cd ./原型验证代码/PVT-01
make clean
make
./raw_trans_bench --mode host_memcpy --payload-bytes 67108864 --qd 16 --loops 10 --out res_memcpy_demo.csv
./raw_trans_bench --mode urma_direct --payload-bytes 67108864 --qd 16 --loops 10 --out res_urma_demo.csv
```

当前测试桩 CLI 支持的参数清单：

```text
--mode           传输模式标签字符串；当前仅 host_memcpy 分支真实执行 memcpy 搬运
--payload-bytes  单次传输分配的本地内存缓冲区字节数
--qd             内部循环迭代深度；当前为同步循环，非异步硬件队列
--loops          外部测量循环轮次
--out            输出 CSV 文件的目标路径
```

### 4.2 源码实际行为审计

| 源码文件与核心函数 | 源码实际执行行为 | 对实测证据等级的影响分析 |
|---|---|---|
| `raw_trans_bench.cc::main` | 解析 `mode`、payload、`qd`、loops 及输出路径等参数；未进行物理设备存在性、传输方向或网络拓扑校验 | CLI 传入的模式字符串不代表底层已完成硬件设备绑定 |
| `raw_trans_bench.cc::worker` 主循环 | `host_memcpy` 分支真实调用 `std::memcpy`；其他模式仅执行空汇编内存屏障 (`asm volatile("" ::: "memory")`) | Direct、TCP 与 NVMe 模式在当前代码下未发生真实数据搬运 |
| `actual_completed_bytes` 字段输出 | 仅 `host_memcpy` 分支正常写入 `payload × qd × loops`；其余模式固定填报为 0 | Direct 占位模式输出的实际完成量为 0，不可据此推导传输带宽 |
| 进程 CPU 统计逻辑 | 调用 `CLOCK_PROCESS_CPUTIME_ID` 与墙上时钟计算单进程 CPU 占用百分比 | 仅能反映该测试进程自身开销，未覆盖系统级 CPU、硬件中断及内存总线争用 |
| `host_touch_monitor.py::BPF_PROGRAM` | 仅挂载 `kprobe:memcpy` 与 `kprobe:memmove`，按目标 PID 过滤并累加 `arg2` 参数字节数 | 未覆盖用户态 uprobe、AVX 向量化指令、内核用户空间拷贝及完整覆盖率证明 |
| `host_touch_monitor.py` 输出逻辑 | 当 bpftrace 工具缺失或输出未包含 `@memcpy_bytes` 时输出 `INVALID_EVIDENCE`；解析成功即输出 `OK`，但不校验字节是否为 0 | 脚本返回 `status=OK` 仅代表解析成功，不代表零拷贝成立，必须核验 `host_touch_bytes` |
| `export_capability_matrix.py` | 仅当运行模式非 DEMO、行状态为 `OK`、探针状态为 `OK` 且完成字节大于 0 时填写真实数值 | 当前测试桩输出多为 DEMO 或完成量为 0，生成的矩阵条目按规则大多保持为 `null` 或无效状态 |
| 硬件能力矩阵输出格式 | 输出符合 `schema_version=capability_matrix.v2` 规范的探针对象与链路条目 | 工具未自动填报设备型号、物理拓扑、配置哈希及有效期限，需由外部 manifest 补齐 |

特别说明：`host_touch_monitor.py` 内置的 bpftrace 脚本仅为按目标 PID 过滤的内核层级观测工具。它能够作为局部观测手段，但不能因命令执行成功即宣称实现了“全量五锚点覆盖”。正式实验必须在证据包中详细记录实际挂载的探针符号列表及未覆盖范围。

### 4.3 面向 LAB/MEASURED 的最小工程扩展

在正式进入真实硬件性能与零拷贝准入评估前，必须补齐以下工程支撑能力：

1. **真实硬件驱动对接**：分别实现 URMA、UBMEM、NVMe Direct 及 Socket TCP 的物理设备初始化、内存地址注册、队列提交、完成中断监听、错误码处理与资源释放，现场 API 必须以原厂 SDK 官方头文件为准；
2. **设备显存通路打通**：明确 NPU HBM 或设备显存的分配机制、物理句柄/虚拟地址映射、MR（Memory Region，内存区域注册）权限及对齐约束；驱动报错时必须显式抛出异常，严禁静默回退至 Host DDR 中转；
3. **真实异步提交模型**：将当前同步的 `qd` 循环改造为真实的硬件异步队列深度，逐描述符记录提交时间、硬件完成中断时间、完成字节数、错误码及队列类型；
4. **Direct I/O 生产级实现**：基于 `io_uring` 固定缓冲区或现场等价 NVMe 接口，完整记录块设备裸盘路径、LBA 物理扇区、4KB 严格对齐、读写方向、完成事件及设备性能计数器；
5. **探针覆盖范围扩展**：在当前内核 kprobe 基础上，依据实际调用链补齐 glibc 用户态 `uprobe`、内核拷贝入口、第三方运行库或设备驱动回退分支，并完整留存探针挂载加载日志；
6. **全链路事件与物理凭证**：逐条记录 `planned_path`（规划路径）、`actual_path`（实测物理路径）、硬件完成中断、Host touch 字节、请求上下文、代码包版本、配置哈希及网络拓扑；
7. **能力矩阵 Schema 补全**：完整填报设备型号、驱动版本、节点标识、物理链路、标称线速、测量时间窗、样本量、P50/P99 时延、有效期、证据等级、适用范围及 `invalid_reason`；
8. **规范化状态枚举输出**：将内部脚本返回的 `OK`、`DEMO_ONLY`、`INVALID_EVIDENCE` 映射为公共契约规定的 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE` 标准状态，严禁将脚本正常运行退出直接误判为测试准入通过。

### 4.4 适配接口示意与边界

以下代码仅展示适配扩展的接口逻辑结构，现场开发必须替换为原厂 SDK 的真实函数：

```cpp
// 接口示意伪代码：正式实测必须替换为现场 NPU/URMA/UBMEM SDK 的真实接口
void* device_buffer = allocate_p2p_device_memory(payload_bytes);
auto memory_region = register_device_memory(device_buffer, payload_bytes);
auto descriptor = build_dma_descriptor(memory_region, remote_address, payload_bytes);
submit_descriptor(descriptor);
auto completion = wait_for_device_completion();
record(completion.actual_bytes, completion.status, completion.timestamp_ns);
```

若现场硬件环境不支持设备显存直接注册或 NVMe Direct I/O，测试结果必须规范记录为 `NOT-SUPPORTED`；严禁在底层静默退化为 Host DDR 中转后，依然在日志中虚标为 `actual_path=urma_direct` 或 `nvme_direct`。

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结实验身份、路径、证据等级和采样窗口

- **操作意图**：明确本轮评测的执行级别（W0/DEMO、W1/LAB 或 W2/MEASURED），杜绝将测试桩占位路径的演示数值误判为真实硬件性能结论。
- **执行动作**：在配置清单中完整填报 `run_id`、`package_id`、`baseline_commit`、`config_hash`、`hardware_profile`、`topology_profile`、`evidence_environment`、`evidence_level`、测试路径模式、Payload 尺寸、队列深度、循环轮次、传输方向、预热策略及准入门槛，并列明探针实际覆盖范围。
- **应观察现象**：配置能够清晰界定本轮实测完成字节的物理来源、Host touch 探针覆盖的符号列表以及当前未支持特性；若缺乏真实硬件驱动或完成事件，应提前登记为 `NOT-SUPPORTED`。

### 步骤 1：编译并审计当前 W0 工具

- **操作意图**：核实 Makefile 编译配置、CLI 参数解析、CSV 数据格式及状态输出与源码实现的一致性，建立基准工作流基线。
- **执行命令**：

```bash
cd ./原型验证代码/PVT-01
make clean
make
./raw_trans_bench --mode host_memcpy --payload-bytes 67108864 --qd 16 --loops 10 --out res_memcpy_demo.csv > raw_memcpy_stdout.txt 2>&1
```

- **应观察现象**：CSV 完整包含 `path_mode`、`payload_bytes`、`queue_depth`、`actual_completed_bytes`、实测带宽、P50/P99 时延、CPU 占用率、`evidence_level` 及状态字段；`host_memcpy` 模式下完成字节大于 0，Direct 占位路径输出的实际完成量为 0。
- **判定边界**：本步骤仅证实本地测试桩工程能够正常构建与运行，不可据此关闭任何 Direct 直达或零数据拷贝的验证命题。

### 步骤 2：运行 Host Memcpy 对照基线

- **操作意图**：获取包含 Host CPU 与主机内存搬运的软件中转参考基准，验证 CPU 统计逻辑与 eBPF 探针是否能准确捕获已知的内存拷贝动作。
- **执行命令**：在保持与步骤 1 相同 Payload、`qd` 及 loops 条件下运行，并将真实的被测进程 PID 传递给探针：

```bash
./raw_trans_bench --mode host_memcpy --payload-bytes 67108864 --qd 16 --loops 10 --out res_memcpy.csv > raw_memcpy_stdout.txt 2>&1 &
BENCH_PID=$!
python3 ./host_touch_monitor.py ${BENCH_PID} 30 --out host_touch_memcpy.json --evidence-level LAB
wait ${BENCH_PID}
```

- **应观察现象**：探针成功加载并解析输出，针对 `host_memcpy` 的已知内存搬运操作形成大于 0 的字节统计；若进程在探针挂载前已结束、bpftrace 缺失或输出缺少字节字段，系统应正确记录为 `INVALID-EVIDENCE`。
- **判定边界**：本组实验属于软件中转基准组，非目标 Direct 直达组；探针捕获到非零拷贝属于符合预期的物理现象，严禁用此基准数据反向修正目标组的评测结果。

### 步骤 3：运行当前 Direct/TCP/NVMe 占位路径并确认停止条件

- **操作意图**：完整记录现有代码在五类模式下的执行表现，严格区分“命令行可正常执行”与“硬件设备已完成数据搬运”两者的本质差异。
- **执行命令**：

```bash
./raw_trans_bench --mode urma_direct --payload-bytes 67108864 --qd 16 --loops 10 --out res_urma_demo.csv > raw_urma_stdout.txt 2>&1
./raw_trans_bench --mode ubmem_direct --payload-bytes 67108864 --qd 16 --loops 10 --out res_ubmem_demo.csv > raw_ubmem_stdout.txt 2>&1
./raw_trans_bench --mode nvme_direct --payload-bytes 67108864 --qd 32 --loops 10 --out res_nvme_demo.csv > raw_nvme_stdout.txt 2>&1
./raw_trans_bench --mode socket_tcp --payload-bytes 67108864 --qd 16 --loops 10 --out res_tcp_demo.csv > raw_tcp_stdout.txt 2>&1
```

- **应观察现象**：Direct、NVMe 与 TCP 占位模式输出的 `actual_completed_bytes` 均为 0，状态标注为 `DEMO_ONLY`；上述产出仅可用于校验数据结构与异常拦截边界。
- **停止条件**：至此步骤严禁直接导出生产级硬件能力矩阵条目。若未完成第 4.3 节的驱动扩展，必须将对应目标路径标记为 `NOT-SUPPORTED`，严禁继续执行“80% 线速达成率”的准入判定。

### 步骤 4：启动探针并执行真实路径 A/B（条件步骤）

- **前置条件**：已成功接入真实的 URMA/UBMEM/NVMe/TCP 驱动代码包；底层硬件完成事件可被正常监听采集；探针覆盖范围已通过现场审查；`actual_path` 具备可审计凭证；硬件拓扑与配置哈希已严格冻结。
- **操作意图**：在相同 Payload 尺寸、队列深度、硬件设备及负载压力下，严密对比 Host Memcpy 与 Direct 直达路径，系统验证物理完成量、Host CPU 占用率、Host touch 字节及尾部时延稳定性。
- **执行动作**：沿用标准 CLI 参数规范执行实测；每条路径分配独立的 `run_id`，严禁仅修改客户端 mode 标签。动态探针必须在数据传输开始前完成挂载，并在传输窗口彻底关闭后导出原始日志。
- **应观察现象**：目标 Direct 路径的 `actual_completed_bytes > 0`；实测有效带宽可由完成字节数与测量时间严密复算；探针覆盖完整且 `host_touch_bytes=0`；异常失败、传输重试、设备错误码及 CPU 统计数据均清晰可溯。

### 步骤 5：停止探针并生成硬件能力矩阵

- **操作意图**：将各路径测试产出的 CSV 与探针输出 JSON 进行标准化聚合，生成供上层 QueryPlan 决策使用的能力参数表，仅将证据闭环的条目标记为有效数据。
- **执行命令**：

```bash
python3 ./host_touch_monitor.py <target_pid> 30 --out host_touch_evidence.json --evidence-level LAB

python3 ./export_capability_matrix.py   res_memcpy.csv res_tcp.csv res_urma.csv res_ubmem.csv res_nvme.csv   --host-touch-evidence host_touch_evidence.json   --out capability_matrix.json
```

- **应观察现象**：导出的 JSON 文件中 `schema_version` 标注为 `capability_matrix.v2`，每个路径条目均能精确追溯至源 CSV 与探针 JSON；未通过验证路径的有效带宽、时延及 Host touch 字段显式保留为 `null`，严禁用 0 填充。
- **判定边界**：当前导出工具未自动填充设备型号、物理拓扑、配置哈希及有效期限，必须由外部 `manifest.json` 补充关联；脚本输出的 `entries` 数量不代表实际通过准入测试的路径数量。

### 步骤 6：核对字段、状态和能力矩阵可消费性

- **操作意图**：严格审查生成的硬件能力矩阵，确保系统绝不会将带有 `DEMO_ONLY` 标记、探针覆盖不全或物理完成量为 0 的未经验证路径下发给上层调度引擎。
- **执行命令**：

```bash
python3 -m json.tool capability_matrix.json > capability_matrix.pretty.json
```

- **应观察现象**：JSON 文件结构合法可解析；每个有效条目完整包含 Payload、队列深度、实际完成量、代码版本、网络拓扑、证据等级及状态枚举；字段缺失时规范标记为 `INVALID-EVIDENCE`，严禁使用预设的默认带宽或时延进行失真填充。

### 步骤 7：归档原始数据并完成重复实验对账

- **操作意图**：完整留存动态探针原始输出、异常请求日志及物理运行环境快照，避免仅凭能力矩阵汇总摘要做出无法复核的技术结论。
- **执行动作**：在 `results/PVT-01/<mode>/<run_id>/` 目录下归档 CSV 数据、探针 JSON、控制台 stdout/stderr 输出、`capability_matrix.json`、`manifest.json`、`environment.json` 及分析摘要；每个测试条件确保至少完成 3 轮独立重复测量。
- **应观察现象**：报告中的每个有效带宽、P50/P99 时延、CPU 占用率、Host touch 字节及状态均能精准索引至底层原始事件；未采集到的字段显式置为 `null` 并注明 `invalid_reason`。

---

## 6. 数据采集清单与记录格式

### 6.1 传输原始事件字段

在统一公共事件字段的基础上，本验证项至少采集并持久化以下字段：

```text
run_id, validation_id, trace_id, event_name,
path_mode, planned_path, actual_path, direction,
payload_bytes, queue_depth, loop_id, descriptor_id,
submit_start_ns, submit_end_ns, device_complete_ns,
actual_completed_bytes, latency_ns, bandwidth_gbps,
host_touch_calls, host_touch_bytes, probe_scope, probe_pid,
cpu_process_us, wall_duration_us, cpu_pct,
device_id, driver_version, kernel_version,
package_id, baseline_commit, config_hash,
hardware_profile, topology_profile, evidence_environment, evidence_level,
status, error_code, invalid_reason
```

字段填写约束：

- `actual_completed_bytes` 必须严格基于底层硬件设备或通信协议的真实完成事件填报；当前占位 Direct 路径填报 0 仅代表“代码未执行真实搬运”，严禁将其误判为 0 字节正常完成；
- `host_touch_bytes=null` 代表尚未获取到合法的探针观测凭证，绝不代表零数据拷贝成立；
- `probe_scope` 必须完整列出实际挂载的探针符号清单以及当前未覆盖的调用范围；
- `cpu_pct` 仅在进程 CPU 耗时、墙上时钟时长、采样时间窗及 CPU 绑核信息完整齐备时方可作为准入门槛判定依据；
- `status` 严格限定为 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE`；脚本内部返回的 `OK` 必须经过证据规则校验后完成归一化映射。

### 6.2 传输结果 CSV 模板

以下为微基准输出字段格式规范（非预置实测成绩）：

```csv
validation_id,run_id,path_mode,payload_bytes,queue_depth,direction,actual_completed_bytes,bandwidth_gbps,latency_p50_us,latency_p99_us,cpu_process_us,wall_duration_us,cpu_pct,host_touch_calls,host_touch_bytes,probe_scope,actual_path,package_id,baseline_commit,config_hash,hardware_profile,topology_profile,evidence_environment,evidence_level,status,invalid_reason
<PVT-01>,<run_id>,<urma_direct_or_ubmem_direct_or_nvme_direct_or_host_memcpy_or_socket_tcp>,<bytes>,<qd>,<read_or_write>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<count_or_null>,<bytes_or_null>,<scope_or_null>,<actual_path_or_null>,<package_id>,<baseline_commit>,<config_hash>,<hardware_profile>,<topology_profile>,<W0_OR_W1_OR_W2>,<DEMO_OR_LAB_OR_MEASURED>,<status>,<null_or_reason>
```

### 6.3 硬件能力矩阵 JSON 约束

目标输出 JSON 结构规范：

```json
{
  "schema_version": "capability_matrix.v2",
  "run_id": "<run_id>",
  "hardware_profile": "<hardware_profile>",
  "topology_profile": "<topology_profile>",
  "probe": {
    "scope": ["<actual_probe_symbol>"],
    "target_pid": "<pid>",
    "host_touch_bytes": null,
    "status": "<status>",
    "invalid_reason": "<null_or_reason>"
  },
  "paths": [
    {
      "mode": "<path_mode>",
      "payload_bytes": "<bytes>",
      "queue_depth": "<qd>",
      "actual_completed_bytes": "<measured_or_null>",
      "effective_bw_gbps": "<measured_or_null>",
      "latency_p50_us": "<measured_or_null>",
      "latency_p99_us": "<measured_or_null>",
      "host_cpu_pct": "<measured_or_null>",
      "actual_path": "<actual_path_or_null>",
      "evidence_level": "<DEMO_OR_LAB_OR_MEASURED>",
      "status": "<status>",
      "invalid_reason": "<null_or_reason>"
    }
  ]
}
```

当前 `export_capability_matrix.py` 仅输出基础形态的 `capability_matrix.v2` 结构，不能替代代码版本、物理拓扑及有效期限等公共字段；正式产出必须在证据包的 `manifest.json` 中完整补齐。

### 6.4 证据包目录结构

```text
results/PVT-01/<mode>/<run_id>/
├── manifest.json
├── environment.json
├── raw_events.jsonl
├── raw_metrics.*
├── raw_stdout/
├── host_touch_evidence.json
├── res_*.csv
├── capability_matrix.json
├── summary.json
├── summary.csv
└── logs/
```

`manifest.json` 至少完整固化代码包版本、基线 Git Commit、配置哈希、设备型号/驱动/内核版本、探针实际覆盖范围、执行 CLI 命令、原始数据哈希、证据等级、功能支持范围、未支持特性、候选准入门槛及最终判定状态。

---

## 7. GO / CONDITIONAL / NO-GO / NOT-SUPPORTED / INVALID-EVIDENCE 判定规则

### 7.1 Host CPU 零数据拷贝判定

- **GO（零数据拷贝证据形成完整闭环）**：目标 Direct 直达路径的实际物理完成字节数大于 0；动态探针成功加载并完整覆盖传输全生命周期及实际调用入口；`host_touch_bytes=0` 确切成立；底层硬件完成事件、实测物理路径、版本凭证与网络拓扑完整齐备；Host CPU 占用率满足预先冻结的准入门槛。该结论仅对记录的物理路径与硬件设备负责。
- **CONDITIONAL（探针或路径覆盖有限）**：仅覆盖了部分内核或用户态调用入口，或仅在特定硬件组合上获得实测证据；结论严格限定为“在已覆盖范围内未观测到正文数据拷贝”，严禁外推为全链路零数据拷贝。
- **NOT-SUPPORTED（功能未支持）**：当前代码仍处于空汇编测试桩阶段、硬件设备不支持显存注册、动态探针无法正常加载或缺乏硬件完成事件凭证；该状态代表当前工程尚未具备验证条件，不代表目标系统架构失败。
- **NO-GO（确证发生正文数据拷贝）**：在 Direct 路径实测中，动态探针与硬件事件共同证实存在 Host CPU 正文数据搬运，或 Host CPU 占用率超出准入门槛且未取得合理的物理归因解释。

### 7.2 传输性能与能力矩阵判定

- **GO（传输路径达到候选准入门槛）**：网络 Direct 直达有效线速达成率 $\ge 80\%$，NVMe Direct 顺序读写带宽达到设备标称峰值的 $\ge 80\%$；P50/P99 时延分布、异常失败率、传输重试率、CPU 开销及 Host touch 证据链完整闭环；硬件能力矩阵数据可由底层原始文件严密复算验证。
- **CONDITIONAL（局部硬件或参数支持）**：仅在 W1/LAB 环境下、部分 Payload 尺寸/队列深度/传输方向或特定单机设备上满足准入门槛；结论严格绑定已测条件，严禁外推至未测路径。
- **NO-GO（物理路径未达准入门槛）**：在具备真实硬件完成事件的公平 A/B 实测中，物理带宽未达准入门槛，或性能提升伴随不可接受的传输失败、高重试率、CPU 占用飙升或尾部时延恶化。
- **NOT-SUPPORTED（物理环境未支持）**：现场缺乏目标硬件设备、原厂驱动 SDK、真实 DMA 直达通路或可用物理链路，严禁用测试桩输出替代真实硬件能力。

### 7.3 统一无效证据规则

凡出现以下任一情形，对应路径一律判定为 `INVALID-EVIDENCE`，严禁给出 `GO` 结论：

- 仅凭 CLI 传入的 `--mode` 字符串参数代替底层的真实驱动或硬件数据面切换；
- Direct、NVMe 或 TCP 路径的实际完成字节数为 0，却擅自按名义 Payload 理论值计算传输带宽；
- 动态探针未加载成功、监控 PID 不符、采样时间窗未完整覆盖传输过程、输出缺失字节统计或符号覆盖范围模糊不清；
- `host_touch_bytes=null` 或探针解析失败，却违规将其填报为 0；
- 缺失底层硬件完成事件凭证、实测物理路径、版本清单、网络拓扑、原始采样记录或失败事件日志；
- A/B 对照测试中擅自变更了 Payload 尺寸、队列深度、硬件设备、CPU 绑核、资源配额、循环轮次或统计口径；
- 硬件能力矩阵缺失关键字段，却采用预设的默认带宽、时延或 CPU 数值进行失真填充；
- 将测试脚本内部返回的 `OK`、`DEMO_ONLY` 或 `INVALID_EVIDENCE` 状态在未经技术归因与复核的情况下直接修改为 `GO`。

---

## 8. 执行阶段与交付闭环

测试实施划分为三个严密的演进阶段：

| 实施阶段 | 核心攻坚内容 | 阶段必须交付物 | 准出判定条件 |
|---|---|---|---|
| 阶段 A：工具审计与 W0 流程 | 严密核对当前 C++ 与 Python 工具的实际行为，完成 Host Memcpy 基础搬运、测试桩占位路径、探针异常分支及矩阵格式的验证闭环 | 源码审计报告、W0 基准 CSV、探针输出 JSON、`DEMO` 级 manifest | 明确当前底层完成量、探针覆盖范围及当前未支持特性 |
| 阶段 B：设备路径与探针闭环 | 对接原厂真实 URMA/UBMEM/NVMe/TCP 驱动，全流程采集底层硬件完成量、Host touch 字节、CPU 占用率及尾部时延稳定性 | 原始事件日志、硬件性能计数器、探针执行日志、重复实测汇总表 | 每个有效数据指标均能精准追溯至底层原始事件凭证 |
| 阶段 C：能力矩阵准入 | 导出绑定代码版本、网络拓扑与有效期限的标准化硬件能力矩阵，按路径独立输出 GO/CONDITIONAL/NO-GO 判定 | `capability_matrix.json`、性能对照表、技术总结及未支持说明 | 全量数据通过公共契约规范核验，杜绝将测试桩结果包装为硬件能力 |

本验证项的核心价值在于为后续多介质分层存储（Tiering）与微秒级动态选路（QueryPlan）提供高精度的真实物理参数表，并在工程层面证实“Host CPU 仅负责轻量控制面调度、正文数据完全由底层硬件数据面高速搬运”的技术边界。在缺乏真实物理完成事件与完整探针覆盖的前提下，硬件能力矩阵仅能作为未经验证的参考输入，严禁直接用于驱动生产环境的调度决策。

---

## 9. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师核心职责**：
  1. 确认现场部署的 NPU、高性能网卡、NVMe SSD、操作系统内核、原厂驱动 SDK、集群节点、网络拓扑及系统执行权限；
  2. 固化测试路径模式、Payload 尺寸、硬件队列深度、测量循环轮次、CPU 绑核策略、采样时间窗、探针覆盖范围及准入门槛；
  3. 实际启动/停止被测进程与动态探针，完整保存原始 stdout/stderr 输出、硬件完成中断凭证及异常失败事件；
  4. 严格审定 `host_touch_bytes=0` 是否确由完整探针覆盖与底层硬件完成量共同支撑，杜绝假阳性误判；
  5. 对硬件能力矩阵是否具备下发给上层调度器消费的生产条件承担最终技术把关责任。
- **AI Agent 协同职责**：
  1. 深入研读本方案设计、公共测试契约及四个原型验证源码文件，精准梳理实际支持的 CLI 参数、探针监控范围及当前未实现路径；
  2. 编写原始 CSV/JSON 数据解析、统计分位数计算、硬件完成量对账核实、能力矩阵字段补全及标准证据包生成工具；
  3. 严格核验 `actual_completed_bytes`、Host touch 字节、CPU 占用率、线速达成率及状态枚举的逻辑自洽性；
  4. 严守学术与技术诚信红线，严禁虚构驱动 SDK 接口、伪造探针覆盖范围或编造 Direct 性能数据，严禁将测试桩输出改写为零拷贝通过。

### 9.2 可直接复制给 Coding Agent 的 Prompt 模板

```text
我正在执行 PVT-01：Host CPU 零数据拷贝传输底座与硬件能力矩阵验证。

请先研读以下核心文件：
1. ./提前验证方案设计/验证计划方案设计/02_PVT-01_零HostTouch极速传输底座与CapabilityMatrix验证实施方案设计.md
2. ./提前验证方案设计/验证计划方案设计/Benchmark公共契约与证据分级规范.md
3. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-01/Makefile
4. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-01/raw_trans_bench.cc
5. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-01/host_touch_monitor.py
6. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-01/export_capability_matrix.py

执行约束与任务要求：
- 首先梳理源码实际支持的 CLI 参数与底层执行行为；确认 raw_trans_bench.cc 当前仅在 host_memcpy 分支执行真实的 memcpy，其余路径未向硬件设备提交操作，严禁将 mode 模式字符串直接当作物理路径切换。
- 确认 host_touch_monitor.py 当前仅挂载了 kprobe:memcpy 与 kprobe:memmove，严禁声称已覆盖完整的用户态、内核及 AVX 向量化拷贝入口。
- 将当前测试桩输出规范标记为 DEMO / NOT-SUPPORTED 或 INVALID-EVIDENCE；当 Direct/NVMe/TCP 模式下的实际完成字节数为 0 时，严禁依据名义 Payload 计算传输带宽。
- 设计真实硬件路径扩展时，必须严格要求记录设备实际完成量、actual_path、探针监控范围、目标 PID、采样时间窗、代码包版本、配置哈希及网络拓扑。
- Host CPU 零数据拷贝判定必须在“全量探针成功挂载 + host_touch_bytes=0 + actual_completed_bytes>0 + 实测物理路径凭证完整”同时满足时方可给出。
- 未采集到的字段显式置为 null 并详细注明 invalid_reason；完整留存失败异常事件；将脚本内部返回的 OK / INVALID_EVIDENCE 规范归一化为公共契约状态。
- 最终输出：源码能力核验矩阵、实际执行命令清单、五类路径性能对照表、证据字段数据字典、未支持特性清单、无效证据归因分析以及下一步最小代码重构建议。
```

### 9.3 常见排错指南

- **执行报错提示 `bpftrace` 命令不存在或权限不足**：优先记录为 `INVALID-EVIDENCE`，严禁用空 JSON 或手动填报 `host_touch_bytes=0` 代替；现场需排查 Linux 内核 eBPF 支持、root 权限配置或容器 Capabilities 特权参数。
- **探针输出日志中未找到 `@memcpy_bytes` 统计项**：当前脚本会自动记录 `INVALID_EVIDENCE`；需核实目标 PID 是否有效、采样时间窗是否覆盖进程运行周期，并明确记录当前探针仅挂载了两个 kprobe 的事实。
- **探针运行状态返回 `OK` 但记录的字节数大于 0**：解析工具当前仅校验字段是否成功提取，不会自动拒绝非零值；必须由证据审查层将该路径标记为存在 Host touch 开销，严禁判定为零拷贝。
- **Direct 模式输出 CSV 中测得带宽显示为 0**：此为当前测试桩占位分支的实际预期行为，并非硬件物理上的“零带宽”表现；需先核验 `actual_completed_bytes` 字段并确认原厂真实驱动是否已完成对接。
- **`export_capability_matrix.py` 将路径条目标记为 `INVALID_EVIDENCE`**：排查数据行是否带有 DEMO 标记、行状态是否为 `OK`、探针状态是否为 `OK` 以及实际完成字节是否大于 0；同时在外部 `manifest.json` 中补齐设备型号与网络拓扑。
- **NVMe Direct I/O 调用返回 `EINVAL` 错误码**：严格核对 `O_DIRECT` / 固定缓冲区的内存地址边界、磁盘 LBA 扇区起始位、传输长度及 4KB 严格对齐约束；若现场硬件无法支持 NVMe 直接访问设备显存，应规范记录为 `NOT-SUPPORTED`，严禁静默回退至 Host DDR 后继续冒用 Direct 标签。
- **URMA/UBMEM 注册设备显存失败**：排查现场原厂驱动的 P2P 直达支持、内存注册安全权限、设备物理句柄及显存生命周期管理；失败路径必须显式报错归档，严禁静默退化为 Host DDR 内存拷贝。
- **进程 CPU 占用率极低但缺乏 Host touch 探针证据**：低 CPU 占用率不能直接证明零数据拷贝成立；必须先修复探针挂载覆盖范围并取得底层硬件物理完成事件后，方可重新开展准入判定。
- **跨节点传输时间戳计算出现负数或异常偏差**：确保同一链路的两端采用单调硬件时钟进行计时，或记录 PTP（精确时间协议）纳秒级时钟同步状态及误差上限；在缺乏时钟对齐凭证时，严禁直接计算跨节点的单向绝对时延。
