# PVT-01：Host CPU 零数据拷贝传输底座与硬件能力矩阵验证实施方案设计
## —— Mooncake TransferEngine 数据路径核对与硬件直达传输能力验证

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个测试 `run_id` 需在运行前完整固化数据块尺寸 (Payload)、传输路径、代码包版本、物理拓扑、队列深度、预热轮次、测量采样数及目标证据等级；实测产出必须完整保留底层硬件实际完成字节、异常失败事件、Host CPU 探针原始输出及规范的判定状态枚举。在 DEMO 演示模式下，严禁依据名义 Payload 理论值直接计算 Direct 路径的实测传输带宽。

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
> **开源基线版本**：Mooncake TransferEngine `f90ae691f109e49a60920e0c8abbf7e572826d8c`。正式测试结果需独立记录现场驱动、NPU、网卡、NVMe SSD、内核及观测工具版本。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统系统视角：控制面与数据面双流分离

在高性能网络与存储系统（如 Kafka、Netty、DPDK 及分布式存储）中，系统通常严格划分为控制面与数据面：

```text
控制面：应用层构建并下发描述符、更新队列门铃 (Doorbell)、指定源/目的物理地址与传输长度
数据面：网卡/SSD/DMA 控制器直接在硬件内存间搬运正文数据，完全不经过 CPU 寄存器与主机内存中转
```

如果数据面发生退化，设备将数据先读入主机内存 (Host DDR)，再由 CPU 执行 `memcpy` 拷贝至目标显存，CPU 搬运开销将随数据量线性增长。这不仅会推高 CPU 利用率，还会抢占主机内存带宽与 L3 缓存，进而干扰在线推理的控制面调度与微秒级通信。

因此，本方案将“Host CPU 零数据拷贝”定义为系统底座的核心设计约束：
1. 正文数据由硬件 DMA 控制器直接搬运，底层硬件需产生明确的物理完成事件；
2. 在数据传输采样窗口内，Host CPU 仅负责下发控制描述符，不执行正文数据的内存拷贝；
3. 动态探针监控覆盖传输生命周期，与硬件完成中断形成严格对应；
4. 实测有效带宽、时延分布及 CPU 占用率与软件中转基准展开严密对照。

### 0.2 大模型推理中的对应物理场景

KVCache 在分布式推理生命周期中，需要在本地 NPU HBM、远端显存、本地 NVMe SSD 及高速网络之间频繁流转。若每次搬运均需 Host DDR 中转，在长上下文和高并发场景下，CPU 与内存总线将成为性能瓶颈。

本方案在 PVT-01 中重点验证以下四条物理路径：
- **跨节点 URMA/UBMEM 直达路径**：网卡 DMA 直接跨网络访问 NPU HBM 或加速器显存；
- **本地 NVMe Direct 直达路径**：NVMe 控制器通过 `io_uring` 固定缓冲区，直接在 SSD 物理扇区与加速器显存间读写数据；
- **软件中转对比基线**：经由 Host DDR 与 CPU `memcpy` 执行数据中转，用于量化传统软件搬运的额外开销；
- **TCP/IP 传统网络基线**：经由标准 Linux Socket 协议栈与内核网络协议中转，作为基准对照。

### 0.3 什么是硬件能力矩阵

硬件能力矩阵（CapabilityMatrix，即在运行时自动探测各通信链路的带宽、时延、CPU 开销、物理完成量及协议支持状态的参数表，供调度算法使用）是上层调度引擎在微秒级时间内决策采用 Direct-View（远端直读）、Copy-to-HBM（拷贝到本地显存）还是回退至本地重算的关键输入依据。

硬件能力矩阵记录以下关键指标：

| 核心物理问题 | 能力矩阵记录字段 |
|---|---|
| 该路径是否真实完成物理数据搬运 | `actual_completed_bytes`（实际完成字节数）、设备完成事件凭证、`actual_path`（实测物理路径） |
| 硬件底层的数据搬运速率表现 | 有效物理带宽 (`bandwidth_gbps`)、P50/P99 时延分布、Payload 尺寸、队列深度、传输方向 |
| Host CPU 是否参与正文数据搬运 | 探针覆盖符号列表、探针拦截次数、搬运字节数 (`host_touch_bytes`)、CPU 占用率 |
| 实测结论适用的硬件环境 | 硬件设备型号、驱动版本、节点拓扑、代码包版本及配置哈希 |
| 该路径当前是否可安全供调度器使用 | `evidence_level`（证据等级）、`status`（状态枚举）、`invalid_reason`（无效原因）、时间戳与有效期 |

对于缺乏实际物理完成量或未取得完整探针证据的能力条目，矩阵中对应字段显式保留为 `null` 并注明原因。

### 0.4 eBPF 探针技术边界

应用层日志仅能反映程序下发了传输任务，无法证明底层运行库或驱动是否发生了主机内存拷贝。基于 Linux 内核的 eBPF 机制可在函数入口与动态库符号处捕获内存操作：
- 当前受控脚本挂载了 `kprobe:memcpy` 与 `kprobe:memmove`，后续需进一步扩展至用户态 `uprobe` 及内核 `copy_to_user` 等入口；
- 探针未捕获到拷贝事件时，需同时结合底层硬件的物理完成量与执行路径凭证共同交叉核验；
- 脚本输出需结合 `host_touch_bytes` 实际采样值与覆盖率证据完成零拷贝判定。

---

## 1. 验证目标与交付结论定义

### 1.1 核心验证命题

1. **命题一：目标 Direct 路径是否真正绕过 Host CPU 正文数据搬运**。在 URMA、UBMEM 及 NVMe Direct 路径下，底层硬件实际完成搬运字节数大于 0，且在探针覆盖的采样窗口内，Host CPU 正文数据拷贝字节数严格为 0；
2. **命题二：目标路径的物理带宽与尾部时延是否显著优于软件中转**。在相同 Payload 尺寸、队列深度、硬件设备及拓扑条件下，对比 Host Memcpy、Socket TCP 与 Direct 路径的有效物理带宽、P50/P99 时延及 CPU 占用率；
3. **命题三：硬件能力矩阵是否具备作为生产级调度输入的完整性与可靠性**。每条传输链路完整记录支持状态、物理参数、版本凭证、物理拓扑、实测路径及有效期限；
4. **命题四：目标路径是否达到预先固化的候选工程准入门槛**。候选准入门槛包括：网络有效线速达成率 $\ge 80\%$、NVMe 直达顺序读写带宽不低于设备标称峰值的 80%、Host CPU 正文拷贝字节数严格为 0 且 CPU 占用率 $< 5\%$。

### 1.2 交付物与结论边界

每个正式 `run_id` 交付：
1. **《Host CPU 正文拷贝探针证据表》**：记录探针类型、覆盖符号列表、目标 PID、采样时间窗、拦截调用次数、搬运字节数及未覆盖范围清单；
2. **《五类传输路径性能对照表》**：覆盖 URMA Direct、UBMEM Direct、NVMe Direct、Host Memcpy、Socket TCP 的 Payload 尺寸、队列深度、传输方向、实测带宽、P50/P99 时延、CPU 占用率及实际完成字节数；
3. **`capability_matrix.json`**：输出包含版本凭证、拓扑结构与有效期绑定的标准化硬件能力矩阵条目；
4. **标准证据包**：包含 `manifest.json`、`environment.json`、原始数据 CSV、探针原始输出、系统日志及分析摘要；
5. **分项技术判定结论**：针对各传输链路独立输出 `GO`、`CONDITIONAL`、`NO-GO`、`NOT-SUPPORTED` 或 `INVALID-EVIDENCE` 结论。

---

## 2. 实验方案与测试矩阵设计

### 2.1 五类路径与公平对照逻辑

| 传输路径模式 | 目标物理机制与路径含义 | 当前 `raw_trans_bench.cc` 实际执行行为 | 当前默认证据等级与状态 |
|---|---|---|---|
| `urma_direct` | URMA 网卡通过 RDMA DMA 直接读写远端设备显存或 NPU HBM | 未执行内存复制，未向网卡提交操作，仅执行空汇编屏障占位 | `DEMO / NOT-SUPPORTED` |
| `ubmem_direct` | UBMEM 统一总线协议直通访问跨节点与异构设备内存 | 未执行内存复制，未调用 UBMEM 驱动，仅执行空汇编屏障占位 | `DEMO / NOT-SUPPORTED` |
| `nvme_direct` | NVMe 控制器通过 Direct I/O 直接在 SSD 与显存间读写数据 | 未打开 NVMe 设备，未调用 `io_uring`，实际完成字节数为 0 | `DEMO / NOT-SUPPORTED` |
| `host_memcpy` | Host DDR / CPU `memcpy` 软件内存中转基准对比路径 | 真实调用 `std::memcpy` 执行数据搬运，输出完成字节与 CPU 耗时 | `DEMO`（接入真实源/目的设备后可升级为实测） |
| `socket_tcp` | 基于 Linux 标准 TCP/IP Socket 协议栈的传统网络传输路径 | 仅执行空汇编屏障占位，未创建真实网络套接字 | `DEMO / NOT-SUPPORTED` |

开展 A/B 对照测试时，需严格保持硬件设备、Payload 尺寸、队列深度、测量循环次数、内存布局、CPU 绑核、资源配额、预热策略及统计口径的一致，测试中仅变更目标传输路径或被测代码包版本。

### 2.2 数据块与队列深度矩阵

| 测试维度 | 正式计划取值 | 当前源码支持情况 | 物理测试目的与机制分析 |
|---|---|---|---|
| Payload 尺寸 | 64KB、256KB、1MB、4MB、16MB、64MB、256MB、1GB | `--payload-bytes` 支持传入任意单值；默认示例为 64MB | 小数据块重点观察链路固定时延与开销，大数据块重点观测总线带宽爬坡与饱和吞吐 |
| 队列深度 (QD) | 1、2、4、8、16、32、64 | `--qd` 支持传入任意单值 | 在真实硬件路径中对应底层并发提交深度；当前测试桩仅为循环执行 |
| 测量循环轮数 | 运行前统一冻结；建议预热与测量轮次严格隔离 | `--loops` 支持传入单值；默认示例为 10 轮 | 构造足量的原始事件样本集，支撑统计分位数计算与重复性验证 |
| 传输方向 | Read（读）、Write（写）、双向混流 | 当前源码未提供方向参数，仅在本地分配 source/target 内存 | 正式实测由真实驱动扩展支持，并记录物理完成事件的方向 |
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

在 Host CPU 零数据拷贝 Direct 直达路径中，控制面与数据面的时序为：

```text
Host CPU (控制面)   : 内存分配与注册 → 描述符编译与聚合 → 提交硬件工作队列 (SQ) → 监听完成事件 (CQ)
硬件设备 (数据面)   : 源端设备显存 ──────────── PCIe / 网络 DMA 直达 ────────────► 目的端设备显存
```

而在传统的软件中转路径中，数据流转需跨越主机内存：

```text
源端设备 / 外部网卡 ──PCIe DMA──► 主机内存 Host DDR ──Host CPU memcpy──► 目的端显存 HBM
```

单次传输的端到端耗时按底层物理阶段分解为：

$$
T_{path}=T_{submit}+T_{queue}+T_{DMA}+T_{completion}+T_{fence}
$$

若存在 Host CPU 介入中转，则需额外记录主机内存拷贝开销：

$$
T_{host\_copy}=T_{read\_DDR}+T_{memcpy}+T_{write\_DDR/HBM}
$$

### 3.2 有效带宽与线速达成率

有效传输带宽基于底层硬件实际完成搬运的物理字节数计算：

$$
BW_{effective}=\frac{actual\_completed\_bytes \times 8}{T_{measure} \times 10^9}\;\mathrm{Gbps}
$$

网络物理线速达成率计算公式为：

$$
\eta_{wire}=\frac{BW_{effective}}{BW_{line\_rate}} \times 100\%
$$

在当前测试桩中，Direct 模式输出的 `actual_completed_bytes` 固定为 0，严禁通过名义 Payload 理论计算虚构带宽。

### 3.3 Host CPU 零数据拷贝的判定依据

得出“Host CPU 正文数据拷贝严格为 0”的判定结论，需同时满足以下条件：
1. 动态探针成功挂载，覆盖被测进程在传输中可能调用的内核与用户态内存复制入口；
2. 探针监控 PID 与采样时间窗口与实际数据传输时间窗严格对应；
3. `host_touch_bytes` 原始采样值确切为 0，且非默认填充值；
4. 底层硬件记录的 `actual_completed_bytes > 0`，具备设备完成事件凭证；
5. 不存在未被探针监控的第三方隐式运行库或驱动私有拷贝路径。

若出现 `host_touch_bytes=null`、探针执行报错或实际完成字节数为 0 等情形，测试结果判定为 `INVALID-EVIDENCE` 或 `NOT-SUPPORTED`。

### 3.4 CPU 占用和尾部时延

进程维度 CPU 占用率统计公式：

$$
CPU\% = \frac{T_{process\_cpu}}{T_{wall}} \times 100\%
$$

评测报告需完整记录 CPU 统计方法、绑核策略、采样窗口、Payload 尺寸、队列深度以及 P50/P99 时延指标。

---

## 4. 测试工具、当前实现边界与正式扩展要求

### 4.1 当前受控工程目录与运行方式

```text
原型验证代码/PVT-01/
├── Makefile
├── raw_trans_bench.cc           # 包含 host_memcpy 真实拷贝与其余占位分支的微基准程序
├── host_touch_monitor.py        # 基于 bpftrace 挂载内核 kprobe 的 Host 内存拷贝监控工具
└── export_capability_matrix.py  # 聚合基准 CSV 与探针输出并导出能力矩阵的解析工具
```

W0 构建与运行命令：

```bash
cd ./原型验证代码/PVT-01
make clean
make
./raw_trans_bench --mode host_memcpy --payload-bytes 67108864 --qd 16 --loops 10 --out res_memcpy_demo.csv
./raw_trans_bench --mode urma_direct --payload-bytes 67108864 --qd 16 --loops 10 --out res_urma_demo.csv
```

当前测试桩 CLI 支持的参数：

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

### 4.3 面向 LAB/MEASURED 的最小工程扩展

正式评估前需补齐以下工程能力：
1. **真实硬件驱动对接**：实现 URMA、UBMEM、NVMe Direct 及 Socket TCP 的物理设备初始化、内存地址注册、队列提交、完成中断监听、错误码处理与资源释放；
2. **设备显存通路打通**：明确 NPU HBM 或设备显存的分配机制、物理句柄/虚拟地址映射与 MR 权限；
3. **真实异步提交模型**：将同步循环改造为真实的硬件异步队列，记录提交与完成时间戳；
4. **Direct I/O 实现**：基于 `io_uring` 固定缓冲区或 NVMe 接口，记录裸盘路径、LBA 扇区及 4KB 对齐；
5. **探针覆盖扩展**：补齐用户态 `uprobe` 与内核拷贝入口；
6. **全链路事件与物理凭证**：记录 `planned_path`、`actual_path`、硬件完成中断与 Host touch 字节；
7. **能力矩阵 Schema 补全**：填报设备型号、驱动版本、节点拓扑及有效期限；
8. **规范化状态枚举输出**：统一映射为公共契约规定的标准状态。

### 4.4 适配接口示意

```cpp
// 接口示意伪代码：正式实测需替换为现场 NPU/URMA/UBMEM SDK 真实接口
void* device_buffer = allocate_p2p_device_memory(payload_bytes);
auto memory_region = register_device_memory(device_buffer, payload_bytes);
auto descriptor = build_dma_descriptor(memory_region, remote_address, payload_bytes);
submit_descriptor(descriptor);
auto completion = wait_for_device_completion();
record(completion.actual_bytes, completion.status, completion.timestamp_ns);
```

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结实验身份、路径、证据等级和采样窗口

- **操作意图**：明确本轮评测的执行级别（W0/DEMO、W1/LAB 或 W2/MEASURED），避免将测试桩演示数值误判为真实硬件性能结论。
- **执行动作**：在配置清单中填报 `run_id`、`package_id`、`baseline_commit`、`config_hash`、`hardware_profile`、`topology_profile`、`evidence_environment`、`evidence_level`、测试路径模式、Payload 尺寸、队列深度、循环轮次、传输方向、预热策略及准入门槛。
- **应观察现象**：配置清晰界定本轮实测物理来源、探针覆盖范围及未支持特性。

### 步骤 1：编译并审计当前 W0 工具

- **操作意图**：核实 Makefile 编译配置、CLI 参数解析、CSV 数据格式及状态输出的一致性，建立基准流程基线。
- **执行命令**：

```bash
cd ./原型验证代码/PVT-01
make clean
make
./raw_trans_bench --mode host_memcpy --payload-bytes 67108864 --qd 16 --loops 10 --out res_memcpy_demo.csv > raw_memcpy_stdout.txt 2>&1
```

- **应观察现象**：CSV 包含各核心字段；`host_memcpy` 模式下完成字节大于 0，Direct 占位路径输出的实际完成量为 0。

### 步骤 2：运行 Host Memcpy 对照基线

- **操作意图**：获取包含 CPU 搬运的软件中转参考基准，验证 CPU 统计逻辑与 eBPF 探针能否准确捕获已知的内存拷贝动作。
- **执行命令**：

```bash
./raw_trans_bench --mode host_memcpy --payload-bytes 67108864 --qd 16 --loops 10 --out res_memcpy.csv > raw_memcpy_stdout.txt 2>&1 &
BENCH_PID=$!
python3 ./host_touch_monitor.py ${BENCH_PID} 30 --out host_touch_memcpy.json --evidence-level LAB
wait ${BENCH_PID}
```

- **应观察现象**：探针成功解析输出，对 `host_memcpy` 操作形成大于 0 的字节统计。

### 步骤 3：运行当前 Direct/TCP/NVMe 占位路径并确认停止条件

- **操作意图**：记录现有代码在五类模式下的执行表现，区分命令行执行与硬件实际搬运。
- **执行命令**：

```bash
./raw_trans_bench --mode urma_direct --payload-bytes 67108864 --qd 16 --loops 10 --out res_urma_demo.csv > raw_urma_stdout.txt 2>&1
./raw_trans_bench --mode ubmem_direct --payload-bytes 67108864 --qd 16 --loops 10 --out res_ubmem_demo.csv > raw_ubmem_stdout.txt 2>&1
./raw_trans_bench --mode nvme_direct --payload-bytes 67108864 --qd 32 --loops 10 --out res_nvme_demo.csv > raw_nvme_stdout.txt 2>&1
./raw_trans_bench --mode socket_tcp --payload-bytes 67108864 --qd 16 --loops 10 --out res_tcp_demo.csv > raw_tcp_stdout.txt 2>&1
```

- **应观察现象**：Direct、NVMe 与 TCP 占位模式输出的 `actual_completed_bytes` 均为 0，状态标注为 `DEMO_ONLY`。
- **停止条件**：若未接入真实驱动，对应目标路径标记为 `NOT-SUPPORTED`。

### 步骤 4：启动探针并执行真实路径 A/B（条件步骤）

- **前置条件**：已接入真实 URMA/UBMEM/NVMe/TCP 驱动；底层硬件完成事件可正常采集；探针覆盖已审查；`actual_path` 具备凭证。
- **操作意图**：对比 Host Memcpy 与 Direct 直达路径，验证物理完成量、CPU 占用率、Host touch 字节及尾部时延。
- **应观察现象**：目标 Direct 路径的 `actual_completed_bytes > 0`；实测有效带宽由完成字节数与测量时间严密复算；探针覆盖完整且 `host_touch_bytes=0`。

### 步骤 5：停止探针并生成硬件能力矩阵

- **操作意图**：聚合各路径 CSV 与探针输出 JSON，生成供上层 QueryPlan 决策使用的能力参数表。
- **执行命令**：

```bash
python3 ./host_touch_monitor.py <target_pid> 30 --out host_touch_evidence.json --evidence-level LAB

python3 ./export_capability_matrix.py \
  res_memcpy.csv res_tcp.csv res_urma.csv res_ubmem.csv res_nvme.csv \
  --host-touch-evidence host_touch_evidence.json \
  --out capability_matrix.json
```

- **应观察现象**：导出的 JSON 文件中 `schema_version` 标注为 `capability_matrix.v2`；未通过验证路径的字段显式保留为 `null`。

### 步骤 6：核对字段、状态和能力矩阵可消费性

- **操作意图**：审查生成的硬件能力矩阵，确保未经验证的路径不会下发给上层调度引擎。
- **执行命令**：

```bash
python3 -m json.tool capability_matrix.json > capability_matrix.pretty.json
```

- **应观察现象**：JSON 文件结构完整有效；有效条目包含 Payload、队列深度、完成量、版本、拓扑及状态。

### 步骤 7：归档原始数据并完成重复实验对账

- **操作意图**：留存探针原始输出、异常日志及环境快照，确保结论可复核。
- **执行动作**：在 `results/PVT-01/<mode>/<run_id>/` 目录下归档 CSV 数据、探针 JSON、控制台输出、`capability_matrix.json`、`manifest.json` 与 `environment.json`；每个条件至少完成 3 轮独立重复测量。

---

## 6. 数据采集清单与记录格式

### 6.1 传输原始事件字段

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

### 6.2 传输结果 CSV 模板

```csv
validation_id,run_id,path_mode,payload_bytes,queue_depth,direction,actual_completed_bytes,bandwidth_gbps,latency_p50_us,latency_p99_us,cpu_process_us,wall_duration_us,cpu_pct,host_touch_calls,host_touch_bytes,probe_scope,actual_path,package_id,baseline_commit,config_hash,hardware_profile,topology_profile,evidence_environment,evidence_level,status,invalid_reason
<PVT-01>,<run_id>,<urma_direct_or_ubmem_direct_or_nvme_direct_or_host_memcpy_or_socket_tcp>,<bytes>,<qd>,<read_or_write>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<count_or_null>,<bytes_or_null>,<scope_or_null>,<actual_path_or_null>,<package_id>,<baseline_commit>,<config_hash>,<hardware_profile>,<topology_profile>,<W0_OR_W1_OR_W2>,<DEMO_OR_LAB_OR_MEASURED>,<status>,<null_or_reason>
```

### 6.3 硬件能力矩阵 JSON 约束

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

---

## 7. GO / CONDITIONAL / NO-GO / NOT-SUPPORTED / INVALID-EVIDENCE 判定规则

### 7.1 Host CPU 零数据拷贝判定

- **GO（零数据拷贝证据闭环）**：目标 Direct 直达路径的实际物理完成字节数大于 0；动态探针成功加载并完整覆盖传输全周期；`host_touch_bytes=0` 确切成立；硬件完成事件、实测物理路径与拓扑完整齐备；CPU 占用率满足准入门槛；
- **CONDITIONAL（探针或路径覆盖有限）**：仅覆盖了部分内核或用户态调用入口，或仅在特定硬件组合上获得实测证据；结论限定在已测范围内；
- **NOT-SUPPORTED（功能未支持）**：当前代码处于测试桩阶段、硬件不支持显存注册、探针无法加载或缺乏硬件完成凭证；
- **NO-GO（发生正文数据拷贝）**：实测证实存在 Host CPU 正文数据搬运，或 CPU 占用率超出准入门槛且无合理解释。

### 7.2 传输性能与能力矩阵判定

- **GO（传输路径达到准入门槛）**：网络 Direct 直达有效线速达成率 $\ge 80\%$，NVMe Direct 顺序读写带宽达到设备标称峰值的 $\ge 80\%$；P50/P99 时延分布、异常失败率、重试率、CPU 开销及 Host touch 证据链闭环；
- **CONDITIONAL（局部硬件或参数支持）**：仅在 W1/LAB 环境、部分 Payload 尺寸/队列深度或特定设备上满足准入门槛；
- **NO-GO（物理路径未达准入门槛）**：在公平 A/B 实测中，物理带宽未达准入门槛，或伴随高重试率、CPU 占用飙升或尾部时延恶化；
- **NOT-SUPPORTED（物理环境未支持）**：缺乏目标硬件设备、驱动 SDK 或可用物理链路。

### 7.3 统一无效证据规则

凡出现以下任一情形，对应路径一律判定为 `INVALID-EVIDENCE`：
- 仅凭 CLI `--mode` 字符串代替底层驱动切换；
- Direct、NVMe 或 TCP 路径实际完成字节数为 0 时，擅自按名义 Payload 计算带宽；
- 探针未成功加载、监控 PID 不符、采样窗口未覆盖传输过程或符号覆盖范围不清；
- `host_touch_bytes=null` 却违规填报为 0；
- 缺失硬件完成事件凭证、实测物理路径或拓扑记录；
- A/B 对照测试中擅自变更硬件设备、Payload、队列深度、CPU 绑核或统计口径；
- 硬件能力矩阵缺失关键字段却使用预设默认值填充；
- 将内部脚本返回的 `OK` 状态未经核验直接修改为 `GO`。

---

## 8. 执行阶段与交付闭环

测试实施划分为三个演进阶段：

| 实施阶段 | 核心攻坚内容 | 阶段必须交付物 | 准出判定条件 |
|---|---|---|---|
| 阶段 A：工具审计与 W0 流程 | 核对 C++ 与 Python 工具行为，完成 Host Memcpy 基础搬运、测试桩占位路径、探针异常分支及矩阵格式闭环 | 源码审计报告、W0 基准 CSV、探针输出 JSON、`DEMO` 级 manifest | 明确当前底层完成量、探针覆盖范围及未支持特性 |
| 阶段 B：设备路径与探针闭环 | 对接原厂真实 URMA/UBMEM/NVMe/TCP 驱动，采集底层硬件完成量、Host touch 字节、CPU 占用率及时延稳定性 | 原始事件日志、硬件性能计数器、探针执行日志、重复实测汇总表 | 各数据指标精准追溯至底层原始事件凭证 |
| 阶段 C：能力矩阵准入 | 导出绑定代码版本、网络拓扑与有效期的硬件能力矩阵，按路径独立输出判定结论 | `capability_matrix.json`、性能对照表、技术总结及未支持说明 | 全量数据通过公共契约核验，杜绝以测试桩结果冒充硬件能力 |

本验证项的核心价值在于为多介质分层存储（Tiering）与微秒级动态选路（QueryPlan）提供高精度的物理参数表，并在工程层面证实“CPU 仅负责轻量控制面调度、正文数据由底层硬件高速搬运”的技术边界。在缺乏真实硬件完成事件与完整探针覆盖的前提下，硬件能力矩阵仅作为参考，不直接用于生产调度决策。

---

## 9. 工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师核心职责**：
  1. 确认部署的 NPU、高性能网卡、NVMe SSD、内核版本、驱动 SDK、拓扑及执行权限；
  2. 固化测试路径模式、Payload 尺寸、队列深度、循环轮次、绑核策略、采样窗口及准入门槛；
  3. 启动/停止被测进程与探针，保存原始输出、硬件完成凭证及异常事件；
  4. 审定 `host_touch_bytes=0` 是否由完整探针覆盖与硬件完成量共同支撑；
  5. 对硬件能力矩阵是否具备下发给上层调度器消费的条件承担最终把关责任。
- **AI Agent 协同职责**：
  1. 研读方案设计、公共测试契约及源码，梳理实际支持的 CLI 参数、探针监控范围及未实现路径；
  2. 编写数据解析、统计分位数计算、硬件完成量对账、能力矩阵字段补全及证据包生成工具；
  3. 核验 `actual_completed_bytes`、Host touch 字节、CPU 占用率、线速达成率及状态枚举的逻辑自洽性；
  4. 严守技术诚信红线，严禁虚构驱动 SDK 接口、伪造探针覆盖范围或编造 Direct 性能数据。

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

- **执行报错提示 `bpftrace` 命令不存在或权限不足**：优先记录为 `INVALID-EVIDENCE`，严禁用空 JSON 或手动填报 `host_touch_bytes=0` 代替；排查 Linux 内核 eBPF 支持、root 权限配置或容器 Capabilities 特权参数。
- **探针输出日志中未找到 `@memcpy_bytes` 统计项**：脚本自动记录 `INVALID_EVIDENCE`；核实目标 PID 是否有效、采样时间窗是否覆盖进程运行周期。
- **探针运行状态返回 `OK` 但记录的字节数大于 0**：解析工具仅校验字段是否成功提取，不会自动拒绝非零值；必须由证据审查层将该路径标记为存在 Host touch 开销，严禁判定为零拷贝。
- **Direct 模式输出 CSV 中测得带宽显示为 0**：此为当前测试桩占位分支的预期行为，并非硬件“零带宽”表现；需先核验 `actual_completed_bytes` 字段并确认真实驱动是否完成对接。
- **`export_capability_matrix.py` 将路径条目标记为 `INVALID_EVIDENCE`**：排查数据行是否带有 DEMO 标记、行状态是否为 `OK`、探针状态是否为 `OK` 以及实际完成字节是否大于 0；在外部 `manifest.json` 中补齐设备型号与网络拓扑。
- **NVMe Direct I/O 调用返回 `EINVAL` 错误码**：核对 `O_DIRECT` / 固定缓冲区的内存地址边界、磁盘 LBA 扇区起始位、传输长度及 4KB 对齐约束；若硬件不支持 NVMe 直接访问显存，记录为 `NOT-SUPPORTED`。
- **URMA/UBMEM 注册设备显存失败**：排查原厂驱动的 P2P 直达支持、内存注册权限、设备物理句柄及显存生命周期管理；失败路径显式报错归档，严禁静默退化为 Host DDR 内存拷贝。
- **进程 CPU 占用率极低但缺乏 Host touch 探针证据**：低 CPU 占用率不能直接证明零数据拷贝成立；需修复探针挂载覆盖范围并取得底层硬件物理完成事件后重新开展判定。
- **跨节点传输时间戳计算出现负数或偏差**：确保同一链路两端采用单调硬件时钟，或记录 PTP 纳秒级时钟同步状态及误差上限；在缺乏时钟对齐凭证时，不直接计算跨节点的单向绝对时延。
