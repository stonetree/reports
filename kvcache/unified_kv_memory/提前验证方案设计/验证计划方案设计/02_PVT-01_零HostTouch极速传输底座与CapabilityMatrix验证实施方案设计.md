# PVT-01：Host CPU 零数据拷贝传输底座与硬件能力矩阵验证实施方案设计
## —— Mooncake TransferEngine 数据路径核对与国产硬件直达传输能力验证

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个 `run_id` 必须冻结数据块、路径、代码包、拓扑、队列深度、预热、测量轮数和证据等级；结果必须保留实际完成字节、失败事件、Host CPU 探针原始输出和结论状态。DEMO 模式不得按名义 Payload 计算 Direct 路径实测带宽。

> **验证范围声明**：当前受控工程中的 `raw_trans_bench.cc` 只有 `host_memcpy` 分支真正执行本地 `memcpy`；`urma_direct`、`ubmem_direct`、`nvme_direct` 和 `socket_tcp` 分支目前不提交设备、网络或磁盘操作，统一输出 `DEMO / DEMO_ONLY` 或零实际完成字节。`host_touch_monitor.py` 当前只挂载 `kprobe:memcpy` 与 `kprobe:memmove`，没有代码中描述的完整用户态/内核多锚点覆盖，也不会自动生成 `zero_touch_verified=true`。因此，现有源码只能示范字段和失败关闭流程，不能单独证明 Host CPU 零数据拷贝、真实 DMA 或硬件能力矩阵已经成立。

> **术语速查**：Host CPU 零数据拷贝（Host CPU 仅负责下发控制指令，不参与正文数据搬运）；DMA（Direct Memory Access，直接内存访问，即由设备控制器直接读写目标内存）；P2P DMA（Peer-to-Peer DMA，设备之间直接读写显存或设备内存）；HBM（High Bandwidth Memory，高带宽显存）；eBPF（Extended Berkeley Packet Filter，Linux 内核中的可编程观测机制）；kprobe（内核函数入口探针）；uprobe（用户态函数入口探针）；URMA（通用远程直接内存访问，即用户态高性能远程内存访问接口）；UBMEM（统一总线内存直通共享协议，即支持跨节点与异构设备直接共享内存地址空间的底层通信协议）；NVMe（Non-Volatile Memory Express，面向高速固态存储的设备协议）；io_uring（Linux 异步 I/O 接口，支持批量提交和完成事件）。

> **验证 ID**：PVT-01
> **验证名称**：Host CPU 零数据拷贝传输底座与硬件能力矩阵验证
> **验证优先级**：**🟡 P1 级（底座支撑项）**
> **对应验证阶段**：**E1（核心数据路径打通与多卡状态同步）**
> **证伪标记**：否（底层传输能力确认）
> **建议周期**：3~5 人日
> **主关联 IR**：`IR-01-06`, `IR-01-08`, `IR-01-09`, `IR-01-12`
> **核心 SRS / SR23 锚点**：
> - SRS：`L4-MC-HIER-STORE-001`, `L3-MS-Tiering-038`, `L4-HW-HostPayloadTouchBudget-076`, `L4-FT-PathIntegrityPolicy-077`
> - SR23：`SR23-01-06-01`, `SR23-01-07-01`, `SR23-01-08-01`, `SR23-01-09-01`, `SR23-01-12-01`
> **配套源码**：[`提前验证方案设计/验证计划方案设计/原型验证代码/PVT-01/`](file:///d:/codes/reports/kvcache/unified_kv_memory/提前验证方案设计/验证计划方案设计/原型验证代码/PVT-01)
> **开源基线版本**：Mooncake TransferEngine `f90ae691f109e49a60920e0c8abbf7e572826d8c`。正式结果必须另外记录现场驱动、NPU、网卡、NVMe、内核和工具版本，不能只引用本文默认 Commit。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统系统视角：零数据拷贝约束解决的是什么问题

在 Kafka、Netty、DPDK 或高速存储系统中，数据路径通常包含控制面和数据面：

```text
控制面：应用下发描述符、队列门铃、地址和长度
数据面：网卡/SSD/DMA 控制器直接搬运正文数据
```

如果数据面退化为“设备读入 Host DDR，再由 Host CPU `memcpy` 到目标显存”，CPU 会随着 payload 字节数线性参与搬运。结果不仅是 CPU 利用率升高，还会占用 Host DDR 带宽、共享缓存和内存控制器，影响同机调度、RPC 和在线推理控制面。

所以，“零数据拷贝”不是一句性能口号，而是一个需要同时满足的路径约束：

1. 设备完成量必须证明正文确实由目标 DMA 路径搬运；
2. Host CPU 不能在被测窗口内执行正文复制；
3. 探针覆盖范围、被测 PID、采样时间窗和完成事件必须相互对应；
4. 带宽、时延和 CPU 占用要和同一 payload、队列深度及设备条件下的对照组比较。

### 0.2 大模型推理中的对应物理场景

KVCache（大模型注意力键值缓存，即自回归生成过程中保存历史 Key 和 Value 激活状态、避免后续 Token 重复计算注意力）可能在 NPU HBM、远端显存、NVMe SSD 和网络设备之间移动。若每次移动都经过 Host DDR，数据量越大，CPU 和内存总线越容易成为隐性瓶颈。

本项要验证的目标路径包括：

- 跨节点 URMA/UBMEM：网卡 DMA 直接访问可注册的 NPU HBM 或设备内存；
- 本地 NVMe Direct：NVMe 控制器通过 `io_uring`/固定缓冲或现场等价接口直接读写设备内存；
- 软中转基线：Host DDR 与 CPU `memcpy` 明确参与，量化软件中转成本；
- TCP/IP 基线：标准 Socket 协议栈作为传统网络路径对照。

这些是目标物理路径，不代表当前 `raw_trans_bench.cc` 已经实现。当前代码必须先完成源码审计，再决定本轮是 DEMO、LAB 还是 MEASURED。

### 0.3 什么是硬件能力矩阵

硬件能力矩阵（CapabilityMatrix，即在运行时记录各通信链路带宽、时延、CPU 负载、完成量和支持状态的参数表）是调度器选择 Direct-View（远端直读，即直接通过高速链路读取远端 KV 数据，不先产生本地显存完整副本）、Copy-to-HBM（拷贝到本地显存，即通过 DMA 将 KV 数据完整搬运到本地 HBM）或本地重算路径的输入。

它至少要回答：

| 物理问题 | 能力矩阵需要的字段 |
|---|---|
| 这条路径是否真的完成了数据搬运 | `actual_completed_bytes`、设备完成事件、`actual_path` |
| 设备搬运速度如何 | 有效带宽、P50/P99、payload、队列深度、方向 |
| Host CPU 是否参与正文搬运 | 探针覆盖范围、调用次数、搬运字节数、CPU 占用 |
| 结论适用于什么设备 | 设备型号、驱动、节点、拓扑、代码包和配置哈希 |
| 这条路径是否可供调度器使用 | `evidence_level`、`status`、`invalid_reason`、有效期 |

没有实际完成量或完整探针证据的能力条目，必须保留 `null` 并标记原因；不能用默认带宽或“未观测到”直接填 0。

### 0.4 eBPF 探针为什么不能只看应用日志

应用层日志只能说明某个函数“计划提交”了传输，不能证明第三方库、驱动或回退路径没有执行隐式复制。eBPF 可以在内核函数入口或用户态库函数入口观察调用，但它也有边界：

- 当前脚本仅有 `kprobe:memcpy`、`kprobe:memmove`，没有挂载 glibc `uprobe`、`copy_to_user` 或设备特定拷贝符号；
- 即使某个探针窗口内没有事件，也要确认探针加载成功、目标 PID 正确、采样覆盖整个传输、输出解析成功；
- “探针没有捕获到”不等于“Host Payload Touch Bytes 为 0”；还必须有设备完成量和路径凭证；
- 当前脚本只在输出中写 `status=OK` 和 `host_touch_bytes`，没有根据字节数自动判断零拷贝，也没有写入完整覆盖证明。

---

## 1. 验证目标与交付结论定义

### 1.1 现实前因痛点与待验证核心命题

1. **命题一：目标 Direct 路径是否真的绕过 Host CPU 正文搬运**。在 URMA、UBMEM 和 NVMe Direct 路径上，设备完成字节大于 0，Host CPU 正文拷贝字节数在完整探针覆盖窗口内为 0；
2. **命题二：目标路径的带宽和尾部时延是否优于软件中转**。在相同 payload、队列深度、设备和拓扑下，对比 Host Memcpy、Socket TCP 与 Direct 路径的有效带宽、P50/P99 和 CPU 占用；
3. **命题三：能力矩阵能否作为调度输入**。每条路径必须同时记录支持状态、物理参数、版本、拓扑、实际路径和有效期；缺少字段的路径不能被调度器当作已验证路径；
4. **命题四：目标路径是否满足候选工程门槛**。候选门槛为网络有效线速达成率不低于 80%、NVMe 直达顺序带宽不低于设备标称峰值的 80%、Host CPU 正文拷贝字节为 0 且 CPU 占用低于 5%；这些是运行前冻结的判定条件，不是现有代码的预置结果。

### 1.2 交付物与结论边界

每个正式 `run_id` 至少交付：

1. 《Host CPU 正文拷贝探针证据表》：包括探针类型、覆盖符号、目标 PID、采样窗口、调用次数、字节数、解析状态和未覆盖范围；
2. 《五类传输路径性能对照表》：覆盖 URMA Direct、UBMEM Direct、NVMe Direct、Host Memcpy、Socket TCP 的 payload、队列深度、方向、带宽、P50/P99、CPU 和完成量；
3. `capability_matrix.json`：每个路径一条或多条带有版本与拓扑绑定的能力条目；
4. `manifest.json`、`environment.json`、原始 CSV、探针原始 stdout/stderr、原始日志和摘要；
5. 按路径分别输出 `GO`、`CONDITIONAL`、`NO-GO`、`NOT-SUPPORTED` 或 `INVALID-EVIDENCE`。没有把握关闭的路径不能被其他路径的成绩覆盖。

---

## 2. 实验方案与测试矩阵设计

### 2.1 五类路径与公平对照逻辑

| 路径 | 目标物理含义 | 当前 `raw_trans_bench.cc` 实际行为 | 当前默认证据 |
|---|---|---|---|
| `urma_direct` | URMA 网卡直接访问设备内存或 NPU HBM | 不复制、不提交网卡，只执行空汇编占位 | `DEMO / NOT-SUPPORTED` |
| `ubmem_direct` | UBMEM 统一总线直通访问设备内存 | 不复制、不提交 UBMEM，只执行空汇编占位 | `DEMO / NOT-SUPPORTED` |
| `nvme_direct` | NVMe 通过 Direct I/O 直接读写设备内存 | 不打开 NVMe、不调用 `io_uring`，实际完成字节为 0 | `DEMO / NOT-SUPPORTED` |
| `host_memcpy` | Host DDR/CPU `memcpy` 软件中转基线 | 真正执行本地 `memcpy`，输出完成字节和 CPU 时间 | `DEMO`；接入真实源/目标后才可形成对照实测 |
| `socket_tcp` | 标准 TCP/IP Socket 传统网络基线 | 当前同样走空汇编占位，不创建 Socket | `DEMO / NOT-SUPPORTED` |

公平 A/B 只允许改变目标路径或被测代码包；设备、payload、队列深度、循环次数、内存布局、线程绑核、资源配额、预热和统计口径必须一致。仅把 `--mode` 从 `urma_direct` 改为 `ubmem_direct`，并不会切换底层驱动。

### 2.2 数据块与队列深度矩阵

| 维度 | 正式计划取值 | 当前源码支持情况 | 物理目的 |
|---|---|---|---|
| payload | 64KB、256KB、1MB、4MB、16MB、64MB、256MB、1GB | `--payload-bytes` 支持任意单值；默认 64MB | 小块观察固定时延，大块观察带宽爬坡和饱和 |
| queue depth | 1、2、4、8、16、32、64 | `--qd` 支持任意单值 | 正式设备路径中对应并发提交深度；当前程序只是循环执行 |
| loops | 运行前冻结，建议预热与测量分开 | `--loops` 支持；默认 10 | 形成足够的原始样本和重复轮次 |
| 方向 | Read、Write、双向 | 当前没有方向参数，代码只构造本地 source/target | 需由真实驱动扩展，并记录完成方向 |
| 路径 | URMA、UBMEM、NVMe Direct、Host Memcpy、Socket TCP | `--mode` 可接受字符串，但只有 `host_memcpy` 有实际复制 | 不能把字符串支持当成物理能力支持 |

### 2.3 Host CPU 与设备能力门槛

| 指标 | 候选门槛 | 必须具备的证据 | 当前代码状态 |
|---|---:|---|---|
| 网络有效线速达成率 | `>= 80%` | 物理标称线速、实际完成字节、稳定测量时长、实际路径 | 不支持真实网络 |
| NVMe 直达顺序带宽 | `>= 80%` 设备标称峰值 | 设备型号、读写方向、完成字节、`io_uring`/设备完成事件 | 不支持 NVMe |
| Host CPU 正文拷贝字节 | `= 0` | 完整 kprobe/uprobe 或等价探针、目标 PID、采样窗口、设备完成量 | 当前探针覆盖不足，不能关闭 |
| Host CPU 占用 | `< 5%` 候选门槛 | 同场次 CPU 时间、墙上时间、采样工具和绑核信息 | 当前仅进程 CPU 时间，且路径多为占位 |

### 2.4 环境与证据矩阵

| 环境 | 目的 | 最低条件 | 允许形成的结论 |
|---|---|---|---|
| W0 单机/本地桩 | 验证参数解析、CSV、探针失败分支和能力矩阵格式 | C++ 编译器、Python、可选 bpftrace | 仅形成 `DEMO` 流程结论 |
| W1 局部设备实测 | 绑定一台设备或一条链路验证局部路径 | 真实 NPU/网卡/NVMe、驱动、完成事件和探针 | 形成设备绑定的 `LAB` 结论 |
| W2 跨节点完整路径 | 验证真实网络 Direct 和业务可用能力 | 两节点、真实设备路径、拓扑记录、时钟/事件对齐和重复实验 | 证据闭环后形成 `MEASURED` 结论 |

---

## 3. 物理模型与统计口径

### 3.1 数据路径分解

目标 Direct 路径的期望时序是：

```text
Host CPU：分配/注册 → 编译描述符 → 提交队列 → 等待完成
设备数据面：源设备内存 ──DMA──► 目标设备内存
```

软件中转路径则是：

```text
源设备/网卡 → Host DDR → Host CPU memcpy → 目标 HBM/设备内存
```

可用以下解释模型拆分一次传输时间：

$$
T_{path}=T_{submit}+T_{queue}+T_{DMA}+T_{completion}+T_{fence}
$$

若存在 Host CPU 中转，还要显式记录：

$$
T_{host\_copy}=T_{read\_DDR}+T_{memcpy}+T_{write\_DDR/HBM}
$$

模型用于组织观测字段，不允许在没有设备完成事件时用公式推断实际 DMA 已发生。

### 3.2 有效带宽与线速达成率

有效完成字节必须来自设备或协议完成事件，而不是命令行 payload：

$$
BW_{effective}=\frac{actual\_completed\_bytes\times 8}{T_{measure}\times 10^9}\;\mathrm{Gbps}
$$

网络线速达成率为：

$$
\eta_{wire}=\frac{BW_{effective}}{BW_{line\_rate}}\times 100\%
$$

当前 `raw_trans_bench.cc` 对 Direct 模式输出 `actual_completed_bytes=0`，所以不能用 `payload_bytes × queue_depth × loops` 人工补出带宽。

### 3.3 Host CPU 零数据拷贝的证据条件

Host CPU 正文拷贝为 0 的结论必须同时满足：

1. 探针成功加载并覆盖被测进程实际使用的内核/用户态复制入口；
2. 目标 PID、采样窗口和传输窗口完全对应；
3. `host_touch_bytes` 的原始值确实为 0，且不是字段缺失或解析失败后的默认值；
4. `actual_completed_bytes > 0`，并有设备完成事件或路径凭证证明数据已经实际搬运；
5. 没有未覆盖的回退路径、第三方库或设备同步复制未被记录。

因此，`host_touch_bytes=null`、探针命令失败、输出缺少字节统计或 Direct 模式实际完成字节为 0，都只能标为 `INVALID-EVIDENCE` 或 `NOT-SUPPORTED`，不能写成零数据拷贝通过。

### 3.4 CPU 占用和尾部时延

进程 CPU 占用可以作如下解释性统计：

$$
CPU\% = \frac{T_{process\_cpu}}{T_{wall}}\times 100\%
$$

它只能描述采集窗口内进程使用 CPU 的比例，不能替代系统级内存带宽、核绑定位、后台进程和中断开销。正式报告至少给出 CPU 统计方式、绑核、采样窗口、payload、队列深度及 P50/P99；只给一个平均 CPU 百分比不足以关闭 Host CPU 零拷贝命题。

---

## 4. 测试工具、当前实现边界与正式扩展要求

### 4.1 当前受控工程目录与运行方式

```text
原型验证代码/PVT-01/
├── Makefile
├── raw_trans_bench.cc
├── host_touch_monitor.py
└── export_capability_matrix.py
```

当前 `Makefile` 只使用 `g++ -O3 -std=c++17 -pthread -Wall`，不链接 URMA、UBMEM、CANN、`liburing` 或 Socket 专用库。可复现的 W0 构建命令为：

```bash
cd ./原型验证代码/PVT-01
make clean
make
./raw_trans_bench --mode host_memcpy --payload-bytes 67108864 --qd 16 --loops 10 --out res_memcpy_demo.csv
./raw_trans_bench --mode urma_direct --payload-bytes 67108864 --qd 16 --loops 10 --out res_urma_demo.csv
```

当前 CLI 只有：

```text
--mode           传输路径字符串；只有 host_memcpy 分支执行 memcpy
--payload-bytes  单次本地缓冲区大小
--qd             内层循环次数，当前不是异步设备队列
--loops          外层测量循环次数
--out            输出 CSV 路径
```

### 4.2 源码实际行为审计

| 代码路径 | 实际行为 | 对证据的影响 |
|---|---|---|
| `raw_trans_bench.cc::main` | 解析 `mode`、payload、`qd`、loops 和输出路径；不校验设备、方向或拓扑 | CLI 字符串不等于真实设备绑定 |
| `raw_trans_bench.cc::worker` 等价主循环 | `host_memcpy` 执行 `std::memcpy`；其他模式只执行空汇编屏障 | Direct/TCP/NVMe 模式没有真实数据搬运 |
| `actual_completed_bytes` | 只有 `host_memcpy` 写入 `payload × qd × loops`；其他模式固定为 0 | Direct 行不能按名义 payload 计算带宽 |
| CPU 统计 | 使用 `CLOCK_PROCESS_CPUTIME_ID` 与墙上时间计算进程 CPU 百分比 | 只描述该进程，不覆盖系统级 CPU/中断/内存总线 |
| `host_touch_monitor.py::BPF_PROGRAM` | 仅挂载 `kprobe:memcpy`、`kprobe:memmove`，按 PID 累加 `arg2` | 没有 uprobe、AVX 符号、内核用户空间拷贝和完整覆盖证明 |
| `host_touch_monitor.py` 输出 | bpftrace 不存在或缺少 `@memcpy_bytes` 时写 `INVALID_EVIDENCE`；解析成功即写 `OK`，不检查字节是否为 0 | `status=OK` 不等于零拷贝，必须人工核对 `host_touch_bytes` |
| `export_capability_matrix.py` | 仅当非 DEMO、行状态为 `OK`、探针状态为 `OK` 且完成字节大于 0 时写入有效数值 | 当前行多为 DEMO 或完成量 0，矩阵通常只能保留 `null`/无效状态 |
| 能力矩阵输出 | 写入 `schema_version=capability_matrix.v2`、探针对象和路径条目 | 没有自动补充设备型号、拓扑、配置哈希和有效期，需要外部 manifest 补齐 |

`host_touch_monitor.py` 的 bpftrace 脚本是按目标 PID 过滤的内核探针。它能够作为一个局部观测工具，但不能因为命令返回成功就宣称“全量五锚点覆盖”。正式实验必须在证据包中记录实际加载的探针列表和未覆盖范围。

### 4.3 面向 LAB/MEASURED 的最小工程扩展

进入真实硬件结论前，至少补齐：

1. **真实设备适配**：分别实现 URMA、UBMEM、NVMe Direct 和 Socket TCP 的真实初始化、地址注册、提交、完成、错误处理和资源释放；现场 SDK 函数名必须以实际头文件为准；
2. **设备内存路径**：明确 NPU HBM 或设备内存的分配方式、物理句柄/虚拟地址导出、MR（Memory Region，注册给 DMA 设备使用的内存区域）注册、权限和对齐要求；失败时显式报错，不能静默回退 Host DDR；
3. **异步提交模型**：把当前 `qd` 循环改成真实队列深度，记录每个描述符的提交和完成时间、完成字节、错误码和队列类别；
4. **Direct I/O 实现**：若采用 `io_uring` 固定缓冲或现场等价 NVMe 接口，记录文件/裸盘、LBA、4KB 对齐、读写方向、完成事件和设备计数器；
5. **探针覆盖扩展**：在当前 kprobe 基础上，按实际路径补充用户态 `uprobe`、内核拷贝入口、第三方库或设备回退路径，并保留探针加载 stdout/stderr；
6. **事件与路径凭证**：逐条记录 `planned_path`、`actual_path`、设备完成、Host touch、请求关系、代码包、配置哈希和拓扑；
7. **能力矩阵 schema**：补齐设备型号、驱动、节点、链路、标称线速、测量窗口、样本量、P50/P99、有效期、证据等级、支持范围和 `invalid_reason`；
8. **状态归一化**：把脚本内部 `OK`、`DEMO_ONLY`、`INVALID_EVIDENCE` 映射为公共契约的 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE`，不能把工具成功当作验证通过。

### 4.4 适配接口示意与边界

下面的接口只表示适配位置，不代表现场 SDK 提供同名 API：

```cpp
// 伪代码：必须替换成现场 NPU/URMA/UBMEM SDK 的真实接口。
void* device_buffer = allocate_p2p_device_memory(payload_bytes);
auto memory_region = register_device_memory(device_buffer, payload_bytes);
auto descriptor = build_dma_descriptor(memory_region, remote_address, payload_bytes);
submit_descriptor(descriptor);
auto completion = wait_for_device_completion();
record(completion.actual_bytes, completion.status, completion.timestamp_ns);
```

如果现场不支持设备内存注册或 Direct I/O，结果应记录为 `NOT-SUPPORTED`，不能改用 Host DDR 后继续沿用 `actual_path=urma_direct` 或 `nvme_direct`。

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结实验身份、路径、证据等级和采样窗口

- **操作意图**：先区分 W0/DEMO、W1/LAB 和 W2/MEASURED，防止把当前占位路径的数值写成真实设备结论。
- **执行动作**：填写 `run_id`、`package_id`、`baseline_commit`、`config_hash`、`hardware_profile`、`topology_profile`、`evidence_environment`、`evidence_level`、路径、payload、`qd`、loops、方向、预热和门槛；列出探针覆盖范围。
- **应观察现象**：能明确知道本轮实际完成字节的来源、Host touch 探针覆盖的函数和未支持项；没有真实设备或完成事件时提前标记 `NOT-SUPPORTED`。

### 步骤 1：编译并审计当前 W0 工具

- **操作意图**：确认 Makefile、CLI、CSV schema 和状态输出与源码一致，避免使用版本二中不存在的 SDK/参数。
- **执行命令**：

```bash
cd ./原型验证代码/PVT-01
make clean
make
./raw_trans_bench --mode host_memcpy --payload-bytes 67108864 --qd 16 --loops 10 --out res_memcpy_demo.csv > raw_memcpy_stdout.txt 2>&1
```

- **应观察现象**：CSV 有 `path_mode`、`payload_bytes`、`queue_depth`、`actual_completed_bytes`、带宽、P50/P99、CPU、`evidence_level` 和状态；`host_memcpy` 的完成字节大于 0，Direct 占位路径不应被误认为已完成设备搬运。
- **判定边界**：本步骤只证明工具流程可以运行，不关闭任何 Direct 或零数据拷贝结论。

### 步骤 2：运行 Host Memcpy 对照基线

- **操作意图**：得到明确参与 Host CPU 的软件中转参考，检查 CPU 统计和 eBPF 观测链路是否能捕获已知的复制动作。
- **执行命令**：在步骤 1 的同一 payload、`qd` 和 loops 下运行，并把真实被测 PID 传给探针：

```bash
./raw_trans_bench --mode host_memcpy --payload-bytes 67108864 --qd 16 --loops 10 --out res_memcpy.csv > raw_memcpy_stdout.txt 2>&1 &
BENCH_PID=$!
python3 ./host_touch_monitor.py ${BENCH_PID} 30 --out host_touch_memcpy.json --evidence-level LAB
wait ${BENCH_PID}
```

- **应观察现象**：若探针加载并解析成功，`host_memcpy` 的已知复制动作应能形成非零观测；若进程在探针启动前已结束、bpftrace 不存在或输出缺少字节字段，记录 `INVALID-EVIDENCE`。
- **判定边界**：本组是软件中转对照，不是目标 Direct 路径；探针测到非零是预期现象，不能用它反向修改目标组的结果。

### 步骤 3：运行当前 Direct/TCP/NVMe 占位路径并确认停止条件

- **操作意图**：完整记录当前代码对五类 `mode` 的行为，把“命令可执行”和“设备数据已经搬运”明确分开。
- **执行命令**：

```bash
./raw_trans_bench --mode urma_direct --payload-bytes 67108864 --qd 16 --loops 10 --out res_urma_demo.csv > raw_urma_stdout.txt 2>&1
./raw_trans_bench --mode ubmem_direct --payload-bytes 67108864 --qd 16 --loops 10 --out res_ubmem_demo.csv > raw_ubmem_stdout.txt 2>&1
./raw_trans_bench --mode nvme_direct --payload-bytes 67108864 --qd 32 --loops 10 --out res_nvme_demo.csv > raw_nvme_stdout.txt 2>&1
./raw_trans_bench --mode socket_tcp --payload-bytes 67108864 --qd 16 --loops 10 --out res_tcp_demo.csv > raw_tcp_stdout.txt 2>&1
```

- **应观察现象**：Direct、NVMe 和 TCP 占位路径的 `actual_completed_bytes` 为 0，状态为 `DEMO_ONLY`；这些输出只能用于检查 schema 和失败边界。
- **停止条件**：到此为止不能导出真实能力矩阵性能条目。若没有完成第 4.3 节扩展，应把目标路径标为 `NOT-SUPPORTED`，不要继续执行“80% 线速”判定。

### 步骤 4：启动探针并执行真实路径 A/B（条件步骤）

- **前置条件**：真实 URMA/UBMEM/NVMe/TCP 代码包已就绪；设备完成事件可采集；探针覆盖范围已由现场验证；`actual_path` 可审计；硬件和拓扑已冻结。
- **操作意图**：在相同 payload、队列深度、设备和负载下，对比 Host Memcpy 与 Direct 路径，验证完成量、CPU、Host touch 和尾部时延。
- **执行动作**：沿用当前工具的 CLI 形状或现场扩展后的真实 CLI；每条路径独立 `run_id`，不能只改 mode 标签。探针必须在被测传输窗口前启动，并在窗口结束后保留原始输出。
- **应观察现象**：目标路径 `actual_completed_bytes > 0`；有效带宽可由完成量和测量时间复算；探针覆盖完整且 `host_touch_bytes=0`；失败、重试、设备错误和 CPU 统计均可回溯。

### 步骤 5：停止探针并生成硬件能力矩阵

- **操作意图**：把各路径 CSV 与探针 JSON 聚合为可供 QueryPlan 使用的能力参数，但只把证据完整的条目写成有效数据。
- **执行命令**：

```bash
python3 ./host_touch_monitor.py <target_pid> 30 --out host_touch_evidence.json --evidence-level LAB

python3 ./export_capability_matrix.py \
  res_memcpy.csv res_tcp.csv res_urma.csv res_ubmem.csv res_nvme.csv \
  --host-touch-evidence host_touch_evidence.json \
  --out capability_matrix.json
```

- **应观察现象**：JSON 的 `schema_version` 为 `capability_matrix.v2`，每个路径条目能回指源 CSV 和探针 JSON；无效路径的有效带宽、时延和 Host touch 字段为 `null`，而不是 0。
- **判定边界**：当前导出脚本不会自动补设备、拓扑、配置哈希和有效期，必须由外部 `manifest.json` 关联；脚本输出 `entries` 不等于有多少条路径通过。

### 步骤 6：核对字段、状态和能力矩阵可消费性

- **操作意图**：确认能力矩阵不会把 `DEMO_ONLY`、探针不完整或完成量为 0 的路径交给上层调度器。
- **执行命令**：

```bash
python3 -m json.tool capability_matrix.json > capability_matrix.pretty.json
```

- **应观察现象**：JSON 可解析；每个有效条目含 payload、队列深度、实际完成量、版本、拓扑、证据等级和状态；字段缺失时写 `INVALID-EVIDENCE`，不使用默认带宽或时延。

### 步骤 7：归档原始数据并完成重复实验对账

- **操作意图**：保留探针原始输出、失败请求和运行环境，避免只根据能力矩阵摘要做不可复核的性能结论。
- **执行动作**：按 `results/PVT-01/<mode>/<run_id>/` 建立目录，保存 CSV、探针 JSON、stdout/stderr、`capability_matrix.json`、`manifest.json`、`environment.json` 和摘要；每个条件至少完成 3 次独立重复。
- **应观察现象**：每个有效带宽、P50/P99、CPU、Host touch 和状态都能回指原始事件；无法采集的字段使用 `null` 与 `invalid_reason`。

---

## 6. 数据采集清单与记录格式

### 6.1 传输原始事件字段

公共事件字段之外，本项至少记录：

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

字段约束：

- `actual_completed_bytes` 必须来自设备/协议完成事件；当前占位 Direct 路径填写 0 只说明“代码没有完成搬运”，不能把它当作 0 字节正常成功；
- `host_touch_bytes=null` 表示未获得有效探针证据，不表示零数据拷贝；
- `probe_scope` 必须列出实际挂载的符号和未覆盖范围；
- `cpu_pct` 只有 CPU 时间和墙上时间、采样窗口与绑核信息齐全时才可用于门槛判定；
- `status` 使用 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE`；脚本内部 `OK` 需要经过证据规则归一化。

### 6.2 传输结果 CSV 模板

以下是字段模板，不是性能成绩：

```csv
validation_id,run_id,path_mode,payload_bytes,queue_depth,direction,actual_completed_bytes,bandwidth_gbps,latency_p50_us,latency_p99_us,cpu_process_us,wall_duration_us,cpu_pct,host_touch_calls,host_touch_bytes,probe_scope,actual_path,package_id,baseline_commit,config_hash,hardware_profile,topology_profile,evidence_environment,evidence_level,status,invalid_reason
<PVT-01>,<run_id>,<urma_direct_or_ubmem_direct_or_nvme_direct_or_host_memcpy_or_socket_tcp>,<bytes>,<qd>,<read_or_write>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<count_or_null>,<bytes_or_null>,<scope_or_null>,<actual_path_or_null>,<package_id>,<baseline_commit>,<config_hash>,<hardware_profile>,<topology_profile>,<W0_OR_W1_OR_W2>,<DEMO_OR_LAB_OR_MEASURED>,<status>,<null_or_reason>
```

### 6.3 硬件能力矩阵 JSON 约束

目标输出至少包含：

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

当前 `export_capability_matrix.py` 输出的是最小 `capability_matrix.v2`，不能替代上述版本、拓扑和有效期等公共字段；正式结果需要在证据包中补齐。

### 6.4 证据包目录

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

`manifest.json` 至少记录代码包、基线 Commit、配置哈希、设备/驱动/内核、探针范围、执行命令、原始文件哈希、证据等级、支持范围、未支持项、候选门槛和结论状态。

---

## 7. GO / CONDITIONAL / NO-GO / NOT-SUPPORTED / INVALID-EVIDENCE 判定规则

### 7.1 Host CPU 零数据拷贝

- **GO（零数据拷贝证据闭环）**：目标 Direct 路径实际完成字节大于 0；探针成功加载并覆盖完整传输窗口及实际复制入口；`host_touch_bytes=0`；设备完成事件、实际路径、版本和拓扑齐全；CPU 占用满足运行前冻结门槛。该结论只适用于记录的路径和设备。
- **CONDITIONAL（探针或路径覆盖有限）**：只覆盖了部分内核/用户态入口，或只在局部设备组合上形成证据；结论限定为“已覆盖范围内未观测到正文复制”，不能写成全路径零拷贝。
- **NOT-SUPPORTED**：当前仍是空汇编占位、设备不支持内存注册、探针无法加载或没有设备完成事件；该状态不等于目标架构失败。
- **NO-GO（真实复制已确认）**：在有效 Direct 路径中探针和设备事件共同确认 Host CPU 正文拷贝，或 CPU 负载超过运行前冻结门槛且无可接受解释。

### 7.2 传输性能与能力矩阵

- **GO（路径达到候选门槛）**：网络 Direct 有效线速达成率 `>=80%`，NVMe Direct 顺序带宽达到设备标称峰值的 `>=80%`；P50/P99、失败、重试、CPU 和 Host touch 证据完整；能力矩阵可由原始文件复算。
- **CONDITIONAL（局部支持）**：只有 W1/LAB、部分 payload/队列/方向或局部设备满足门槛；结论绑定这些条件，不外推到未测路径。
- **NO-GO（真实路径未达门槛）**：在有效的设备完成事件和公平 A/B 中，带宽低于门槛，或性能提升伴随不可接受的失败、重试、CPU 或尾部时延退化。
- **NOT-SUPPORTED**：现场没有目标设备、驱动、真实 DMA 或有效路径，不能用占位程序输出替代。

### 7.3 统一无效证据规则

以下任一情况将对应路径标为 `INVALID-EVIDENCE`，不得输出 `GO`：

- 用 `--mode` 字符串替代真实驱动或设备数据面切换；
- Direct/NVMe/TCP 实际完成字节为 0，却按名义 payload 计算带宽；
- 探针未加载、目标 PID 不符、采样窗口未覆盖、输出缺少字节统计或符号覆盖范围不明；
- `host_touch_bytes=null` 或解析失败，却写成 0；
- 缺少设备完成事件、实际路径、版本、拓扑、原始样本或失败事件；
- A/B 改变了 payload、队列深度、设备、绑核、资源配额、循环次数或统计口径；
- 能力矩阵缺字段却用默认带宽、时延或 CPU 值补齐；
- 把脚本内部 `OK`、`DEMO_ONLY` 或 `INVALID_EVIDENCE` 未经归一化直接写成 `GO`。

---

## 8. 执行阶段与交付闭环

| 阶段 | 工作内容 | 必须交付 | 退出条件 |
|---|---|---|---|
| 阶段 A：工具审计与 W0 流程 | 核对当前 C++/Python 实际行为，完成 Host Memcpy、占位 Direct、探针失败分支和矩阵格式验证 | 源码审计记录、W0 CSV、探针 JSON、`DEMO` manifest | 明确当前完成量、探针覆盖和未支持项 |
| 阶段 B：设备路径与探针闭环 | 接入真实 URMA/UBMEM/NVMe/TCP，完成设备完成量、Host touch、CPU 和尾部时延采集 | 原始事件、设备计数器、探针日志、重复实验汇总 | 每个有效数据字段能回指原始证据 |
| 阶段 C：能力矩阵准入 | 生成带版本、拓扑和有效期的矩阵，按路径做 GO/CONDITIONAL/NO-GO 判定 | `capability_matrix.json`、对照表、摘要和未支持说明 | 公共契约通过，未把占位结果写成硬件能力 |

本项的最终作用是为后续多介质分层和动态选路提供真实物理参数，并验证 Host CPU 只负责控制面、正文数据由设备数据面搬运的工程边界。没有真实完成事件和完整探针覆盖时，能力矩阵只能作为未验证输入，不能驱动生产决策。

---

## 9. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师负责**：
  1. 确认现场 NPU、网卡、NVMe、内核、驱动、节点、拓扑和权限；
  2. 冻结路径、payload、队列深度、循环次数、绑核、采样窗口、探针覆盖和判定门槛；
  3. 启动/停止被测进程和探针，保留原始 stdout/stderr、完成事件和失败事件；
  4. 判断 `host_touch_bytes=0` 是否真的具备完整探针和设备完成量支撑；
  5. 对能力矩阵是否可交给上层调度器承担现场复核责任。
- **AI Agent 负责**：
  1. 先阅读方案、公共契约和四个实际源码文件，列出真实 CLI、探针范围和未实现路径；
  2. 编写原始 CSV/JSON 解析、分位数计算、设备完成量核对、能力矩阵补充和证据包工具；
  3. 检查 `actual_completed_bytes`、Host touch、CPU、线速达成率和状态枚举的逻辑一致性；
  4. 不凭空增加 SDK 函数、探针覆盖或 Direct 性能数据，不把占位路径输出改写成零拷贝通过。

### 9.2 可直接复制给 Coding Agent 的 Prompt 模板

```text
我正在执行 PVT-01：Host CPU 零数据拷贝传输底座与硬件能力矩阵验证。

请先阅读：
1. ./提前验证方案设计/验证计划方案设计/02_PVT-01_零HostTouch极速传输底座与CapabilityMatrix验证实施方案设计.md
2. ./提前验证方案设计/验证计划方案设计/Benchmark公共契约与证据分级规范.md
3. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-01/Makefile
4. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-01/raw_trans_bench.cc
5. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-01/host_touch_monitor.py
6. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-01/export_capability_matrix.py

约束：
- 先列出当前真实 CLI 和源码行为；确认 raw_trans_bench.cc 只有 host_memcpy 执行 memcpy，其余路径不提交设备操作，不能把 mode 字符串当成真实路径。
- 确认 host_touch_monitor.py 当前只覆盖 kprobe:memcpy/memmove，不能声称已经覆盖完整用户态、内核和 AVX 拷贝入口。
- 将当前输出标为 DEMO/NOT-SUPPORTED 或 INVALID-EVIDENCE；Direct/NVMe/TCP 的实际完成字节为 0 时不得按名义 payload 计算带宽。
- 设计真实路径扩展时，要求记录设备完成量、actual_path、探针范围、目标 PID、采样窗口、代码包、配置哈希和拓扑。
- Host CPU 零数据拷贝只有在“完整探针成功 + host_touch_bytes=0 + actual_completed_bytes>0 + 路径凭证完整”同时满足时才允许判定。
- 缺失字段使用 null 并填写 invalid_reason；保留失败事件；把脚本内部 OK/INVALID_EVIDENCE 归一化为公共契约状态。
- 最后输出：源码能力矩阵、实际运行命令、五类路径对照表、证据字段、未支持项、无效证据项和下一步最小代码改动建议。
```

### 9.3 常见排错指南

- **`bpftrace` 不存在或权限不足**：先记录 `INVALID-EVIDENCE`，不要用空 JSON 或 `host_touch_bytes=0` 代替；现场需核对内核 BPF、权限和容器能力。
- **探针输出没有 `@memcpy_bytes`**：当前脚本会写 `INVALID_EVIDENCE`；检查 PID、采样窗口和 bpftrace 输出，同时记录当前仅覆盖两个 kprobe 的事实。
- **探针状态为 `OK` 但字节数大于 0**：工具当前只检查是否解析到字段，不会自动拒绝非零值；由证据审查层将该路径标为非零 Host touch，不能写成零拷贝。
- **Direct CSV 的带宽为 0**：这是当前占位分支的实际行为，不是设备“零带宽”成绩；先检查 `actual_completed_bytes` 和真实驱动是否已接入。
- **`export_capability_matrix.py` 把条目写成 `INVALID_EVIDENCE`**：检查行是否为 DEMO、状态是否为 `OK`、探针是否为 `OK`、完成字节是否大于 0；同时补齐外部 manifest 的设备和拓扑字段。
- **NVMe Direct 返回 `EINVAL`**：核对 O_DIRECT/固定缓冲的内存地址、LBA、长度和 4KB 对齐；若现场不能让 NVMe 直接访问设备内存，改记 `NOT-SUPPORTED`，不要回退 Host DDR 后沿用 Direct 标签。
- **URMA/UBMEM 注册设备内存失败**：核对现场驱动的 P2P 支持、内存注册权限、设备句柄和地址生命周期；失败路径必须显式记录，不能静默复制到 Host DDR。
- **CPU 占用低但 Host touch 没有证据**：低 CPU 不能证明零数据拷贝；先修复探针覆盖和设备完成事件，再重新运行。
- **跨节点时间无法直接相减**：使用同一节点单调时钟或记录 PTP/其他同步方式及误差上限；无法对齐时，不计算跨节点单向时延。
