# PVT-05：HBM-SSD 直达容量主路径与 DDR 条件角色 Tiering 验证实施方案设计
## —— 分层存储容量扩展与数据面路径验证：NVMe SSD 直达、Host DDR 条件回退与在线请求影响

> **公共执行契约**：本项严格遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个测试 `run_id` 必须在运行前严格固化物理设备、文件系统或裸盘路径、数据块尺寸 (Block Size)、数据传输路径、硬件队列深度、并发请求数、显存超配比例 (Overcommit Ratio)、预热轮次、样本总量及目标证据等级；实测产出必须完整保留底层原始 I/O 事件、硬件实际完成字节数、异常失败归因、实测物理路径 (`actual_path`)、Host CPU/DDR 动态观测数据及规范的状态判定枚举。凡出现关键字段缺失、依赖脚本静态预置数值、无法与底层设备性能计数器对账，或 `actual_path` 与 `planned_path` 发生非预期偏离等情况，一律严禁纳入 `MEASURED` 生产级汇总。

> **验证范围声明**：在当前受控的原型验证工程中，`tier_storage_bench.cc` 仅解析设备参数并固定输出 `DEMO / DEMO_ONLY` 标记的演示行，尚未真正打开 NVMe 块设备、未调用 `io_uring`/SPDK/GDS 等底层存储引擎，亦未分配真实的 NPU HBM 显存；`benchmark_tiering.py` 仅生成四种模式下的固定请求数、完成量、OOM、抢占、Token 统计及 CPU/DDR 数值，未执行真实的分层换入换出、在线推理打流或容量超配混压。因此，现有受控源码仅用于检验数据结构字段完整性与状态归档流程，不可直接作为证明 NVMe SSD 与 HBM 硬件直达、正文数据完全绕过主机内存 (Payload Bypass DDR)、显存容量有效扩展或在线推理 SLO 收益的依据。在缺乏底层物理设备 I/O、真实显存物理地址映射、硬件完成中断、Host Payload Touch 动态探针及原始系统日志的前提下，测试结论统一限定标记为 `DEMO` 或 `LAB`。

> **术语速查**：
> - **KVCache**：大模型注意力键值缓存（大模型自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免后续 Token 生成时重复计算注意力）；
> - **Tiering**：分层存储技术（将高频访问的热 KV 保留在高带宽显存 HBM 中、将低频冷 KV 异步沉淀至大容量 NVMe SSD 中，并在命中时按需异步换入的多级存储管理机制）；
> - **HBM**：High Bandwidth Memory（高带宽显存）；
> - **NVMe SSD**：基于 PCIe 总线与 NVMe 协议的高速固态硬盘，承担冷 KVCache 的大容量分层存储介质角色；
> - **GDS**：GPUDirect Storage（显存直读存储技术：数据直接在 NVMe SSD 与显存之间通过 PCIe DMA 传输，完全绕过主机 CPU 与系统内存）；
> - **io_uring**：Linux 内核提供的高性能异步 I/O 框架，支持批量提交与完成事件通知；
> - **Payload Bypass DDR**：绕过主机内存（数据直接在 NVMe SSD 与 NPU HBM 之间流转，严格绕过主机内存 Host DDR，消除 CPU 内存拷贝与总线争用）；
> - **Host Payload Touch Bytes**：Host CPU 或 Host DDR 触碰正文数据的字节数，目标直达路径严格要求为 0，且必须由动态探针或硬件性能计数器严密证明；
> - **O_DIRECT**：绕过操作系统页缓存 Direct I/O 机制（不能单独等同于绕过 Host DDR）；
> - **LBA**：Logical Block Address（磁盘逻辑块扇区物理地址）；
> - **Watermark**：显存高低水位线（用于在后台自动触发与停止冷数据块换出的容量阈值）；
> - **O_QD**：Outstanding Queue Depth（设备在途并发 I/O 硬件队列深度）。

> **验证 ID**：PVT-05
> **验证名称**：HBM-SSD 直达容量主路径、DDR 条件角色与 Tiering 分层存储验证
> **验证优先级**：**🔴 P0 级（核心关键项）**
> **对应验证阶段**：**E2（动态调度决策与分层扩容）**
> **证伪标记**：否（分层存储路径与容量扩展能力确认）
> **主关联 IR**：`IR-01-01`, `IR-02-08`, `IR-02-09`
> **核心 SRS / SR23 锚点**：
> - SRS：`L3-MS-Tiering-038`, `L3-MC-HIER-STORE-002`, `L3-MS-DDRRolePolicy-092`, `L3-SE-TierBypassPolicy-091`
> - SR23：`SR23-01-01-01`, `SR23-01-01-02`, `SR23-01-01-03`, `SR23-01-08-01`, `SR23-02-08-01`, `SR23-02-09-01`
> **配套源码**：[`./原型验证代码/PVT-05/`](./原型验证代码/PVT-05/)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`。正式测试结果必须绑定实际 I/O 后端引擎、底层驱动/运行时版本、NVMe 设备型号、加速器芯片及配置哈希，严禁仅引用默认 Commit。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统分层路径的性能瓶颈与目标架构

在传统大模型推理系统的显存分层换出方案中，受限于存储与计算硬件的物理互联拓扑，冷 KVCache 数据通常先通过 PCIe DMA 拷贝至主机内存 (Host DDR)，再由 Host CPU 或操作系统内核块设备层二次中转转发至 NVMe SSD；当缓存命中发生换入时，数据则沿着相反方向再次经历两次内存搬运。该传统中转路径的正文数据在流转过程中必须经历以下繁复环节：

```text
传统换出中转路径 : HBM ──PCIe DMA──► Host DDR ──Host CPU/内核块层中转──► NVMe SSD
传统换入中转路径 : NVMe SSD ──内核块层中转──► Host DDR ──Host CPU/DMA──► HBM
```

这条传统路径的核心系统风险不仅在于消耗 Host CPU 算力，更在于海量的正文数据频繁吞吐剧烈争用 Host DDR 内存带宽、L3 共享缓存及内存控制器通道，进而与在线推理的微秒级控制面调度、分布式 RPC 通信及网络页表更新产生严重资源争用。

因此，原厂软硬件协同系统确立了清晰的控制面与数据面双流分离架构：

```text
控制面 (Host CPU) : 负责块描述符管理、I/O 完成中断监听、视图租约维护与异常故障安全处理
数据面 (硬件 DMA) : NVMe SSD <====== PCIe P2P DMA / GDS 直达路径 ======> NPU HBM
条件回退路径       : 仅在底层硬件不支持直达、租约失效或突发故障时 ──► 降级经由 Host DDR 中转恢复
```

系统中的“直达 (Bypass DDR)”属性必须由底层硬件真实的 DMA 目标物理显存地址、硬件完成中断记录及 Host CPU 零数据拷贝探针共同闭环证明；严禁仅仅将测试模式字符串命名为 `hbm_ssd_direct` 就宣称已实现物理直达。

### 0.2 当前代码仓的文件清单与实际能力审计

| 源码文件与路径 | 当前受控源码实际行为 | 现阶段尚不能声称的能力 |
|---|---|---|
| `原型验证代码/PVT-05/tier_storage_bench.cc` | 解析 `--device`、`--block-size`、`--qd`、`--out` 等参数；在输出 CSV 中固定写入 direct 与 staging 两条演示数据行并标记 `DEMO,DEMO_ONLY` | 未真正打开 NVMe 块设备；未调用 `io_uring`、SPDK、GDS 驱动或分配真实 NPU HBM；无底层硬件完成队列 |
| `原型验证代码/PVT-05/benchmark_tiering.py` | 依据内置的静态参数生成四种模式下的固定请求数、完成量、OOM 次数、抢占数、Token 统计及 CPU/DDR 内存占用 | 未执行真实的显存分配、NVMe 磁盘换入换出、分层块动态管理或在线推理服务打流 |
| `原型验证代码/PVT-05/Makefile` | 仅提供当前 C++ 测试桩的编译规则，使用标准 g++ 编译 | 未链接 `liburing`、SPDK、GDS 或昇腾 CANN 驱动运行时库 |
| PVT-05 源码目录 | 当前尚未包含 `tier_allocator.h` 或 `eval_tiering.py` | 早期文档中提及的分层分配器头文件与评估脚本在当前仓库中尚未受控提供 |

特别说明：`tier_storage_bench.cc` 的源码注释已明确注明其属于演示数据结构与输出 Schema 的 DEMO 工具，后续需逐步替换为对接真实 SPDK/io_uring 存储通路与 Host Touch 动态探针。因此，当前仓库状态必须准确判定为“接口与证据格式规范预演”，不可直接作为“HBM-SSD 直达链路已经打通”的实测结论。

### 0.3 当前 DEMO 模式的输出语义与证据边界

当前受控 C++ 基准程序在运行后固定生成 `nvme_direct_bypass_ddr` 与 `hbm_ddr_ssd_staging` 两个演示数据行。其中，direct 模式下输出的 190.4 Gbps 吞吐、时延公式推导值及 1.2% Host CPU 占用率仅为静态预设的演示数值，并非底层物理设备的实测测量值；staging 模式下输出的 85.2 Gbps 吞吐与 65.4% CPU 占用率同样为模拟参数，并非 Host 侧的真实采样值。配套的 Python 评估脚本在各模式下同样写入了固定的请求完成数、OOM 发生率、抢占次数及 DDR 内存占用数值。

上述固定演示数据仅可用于验证输出 CSV 字段格式的完整性、状态机解析流程以及多模式报表分类逻辑，严禁将其作为宣称显存容量扩展比例、SSD 实际读写带宽或在线服务 SLO 收益的技术依据。

---

## 1. 验证目标与交付物定义

### 1.1 核心验证目标

| 验证核心维度 | 必须回答的物理与工程问题 | 必须具备的最低客观证据 |
|---|---|---|
| 直达数据面验证 | NVMe SSD 与 NPU HBM 之间的数据流转是否真正完全绕过 Host DDR | 真实块设备 I/O、真实显存物理地址、DMA 硬件完成中断、Host Payload Touch 动态探针 |
| 分层控制面流转 | 热块异步换出、冷块按需换入、高低水位线触发及租约生命周期是否清晰可溯 | 连续块区间描述符、分层状态迁移全量日志、硬件队列深度、容量回收触发归因 |
| 显存容量扩展收益 | 在显存超配 (Overcommit) 逐步提升时，HBM 可服务上下文容量、OOM 发生率及请求抢占率如何变化 | 相同请求流量、相同模型权重、相同 HBM 物理配额下的公平 A/B 原始采样记录 |
| 前台在线业务影响 | 后台高吞吐 SSD 换入换出 I/O 是否显著恶化前台在线推理的 TTFT/TPOT 及 SLO 达标率 | 前台在线时延分位数、后台 I/O 吞吐分位数、全链路混压时间线、硬件 QoS 优先级队列配置 |
| 条件角色退化验证 | Host DDR 是否仅在底层不支持硬件直达、租约失效或异常故障时作为条件兜底介质使用 | 按路径细分记录的 Host Payload Touch 字节数以及异常回退触发的根因日志 |

### 1.2 阶段交付资产

每个正式的 `run_id` 必须至少交付以下结构化资产：

1. **《存储与计算硬件拓扑配置快照》**：完整记录 PCIe 拓扑关系、NUMA 绑定、驱动版本、文件系统挂载参数或裸盘声明；
2. **《NVMe SSD 直达 I/O 性能与探针审计表》**：包含 `tier_storage_bench` 原始输出、硬件提交/完成计数及 Host Touch 探针原始日志；
3. **《四模式同场景容量与性能 A/B 对照报告》**：涵盖 `pure_hbm`、`mooncake_native_ssd`、`hbm_ddr_tier` 与 `hbm_ssd_direct` 的全量对账数据；
4. **《分层块状态机流转与水位线触发日志》**：详细记录块描述符状态迁移、Watermark 触发时间戳、在途 I/O 及异常回退记录；
5. **标准证据包与判定报告**：包含 `manifest.json`、原始数据 CSV/JSON、系统日志以及明确的 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE` 结论。

---

## 2. 目标数据结构、分层状态机与物理约束

### 2.1 目标分层块描述符设计

面向生产级多介质分层存储管理，底层分层块描述符的标准化定义如下（具体字段以受控实现为准）：

```cpp
struct alignas(64) TierBlockDescriptor {
    uint64_t block_id;
    uint64_t kv_virtual_addr;   // 设备可访问的物理/虚拟显存基地址 (IOVA)
    uint64_t kv_bytes;          // 实际有效 KV 字节数
    uint64_t nvme_lba;          // NVMe 磁盘 4KB 对齐的逻辑扇区物理起始地址
    uint32_t block_bytes;       // 物理块总字节大小 (4KB 整数倍)
    uint16_t tier;              // 介质层级: 0=HBM, 1=NVMe SSD, 2=Host DDR (条件回退)
    uint16_t state;             // 状态: HOT, EVICTING, COLD, LOADING, READY, INVALID
    uint64_t generation;        // 代际版本号，防止旧 I/O 完成覆盖新分配映射
    uint64_t lease_expire_ns;   // 视图租约到期时间戳 (纳秒)
};
static_assert(sizeof(TierBlockDescriptor) <= 64);
```

在工程实现中必须严格遵循以下约束：

- `kv_virtual_addr` 必须为设备控制器与 DMA 引擎均可直接物理访问的 IOVA（I/O 虚拟地址）或设备显存句柄，严禁仅使用普通 Host 用户态虚拟地址；
- `nvme_lba` 必须严格按照磁盘设备的物理扇区边界进行对齐；
- `generation` 与 `lease_expire_ns` 必须形成双重防护，坚决杜绝陈旧失效的后台 I/O 完成事件错误覆盖新分配的显存块；
- 发生 `INVALID` 状态、I/O 超时或硬件故障时，必须安全触发本地算子重算或向 Host DDR 条件回退；
- 描述符自身在 Host 内存中占用的控制元数据开销，严禁计入正文数据的 Host Payload Touch。

### 2.2 分层块分配器状态迁移设计

统一管理 HBM、NVMe SSD 及条件回退 DDR 的分层块分配器（TierBlockAllocator）必须在物理层面严格遵循以下状态迁移图谱：

```text
[HBM / HOT] ─────── 高水位 (Watermark High) 触发 ───────► [EVICTING 换出中]
     │                                                            │
     │ (正常在线消费)                                           DMA 写入完成中断
     ▼                                                            ▼
[HBM / HOT]                                                [NVMe / COLD 沉淀]
     ▲                                                            │
     │ 显存挂接就绪                                             缓存命中发起换入
     │                                                            ▼
[HBM / READY 就绪] ◄────── DMA 读取完成中断 ────────────── [LOADING 换入中]

(任何状态) ─── 发生超时 / 硬件错误 / 租约失效 ───► [INVALID] ──► 回退本地重算 / DDR 条件兜底
```

分层状态机流转日志中，必须逐次完整记录 `block_id`、前置状态、后置状态、代际 Generation、提交时间戳、完成时间戳、硬件错误码及回退动作。仅输出最终容量数字而缺乏状态迁移日志，无法证明分层生命周期的正确性。

### 2.3 4KB 严格对齐与容量计算准则

NVMe 设备物理扇区、文件偏移、单次 I/O 长度及 KVCache 块大小必须在测试启动前严格固化。设单块大小为 $B$、待存储的 KV 数据总量为 $S$，则分配块数与对齐字节数计算如下：

$$
	ext{blocks} = \left\lceil rac{S}{B} ightceil, \quad 	ext{aligned\_bytes} = 	ext{blocks} 	imes B
$$

当且仅当 $B$ 为 4KB 的整数倍，且 `nvme_lba * sector_bytes`、文件偏移 `offset` 及传输长度 `length` 均严格满足底层块设备的物理对齐约束时，系统方可进入真实 Direct I/O 阶段。物理对齐虽是 Direct I/O 的必要前提，但不能单独作为绕过 Host DDR 的充分证据。

### 2.4 显存高低水位线策略

显存池推荐配置高水位线 $H_{	ext{high}}$（如已用显存 85%）与低水位线 $H_{	ext{low}}$（如已用显存 70%），并全程捕获其动态触发事件：

```text
当 used_hbm >= H_high : 自动挑选 LRU 冷数据块，启动后台异步批量换出，直至 used_hbm <= H_low
当 used_hbm <= H_low  : 停止后台换出，将 PCIe 与存储 I/O 带宽完全释放保留给前台在线推理
```

水位线触发本身不是加速收益指标。必须同步监控换出批量、I/O 排队时延、前台在线 TPOT 波动、OOM 发生率及请求抢占次数，坚决杜绝通过过度激进的后台换出换取表面虚高的显存容量而严重破坏在线服务质量。

---

## 3. 实验方案与测试矩阵设计

### 3.1 四种运行模式对照定义

| 模式标识 | 目标物理数据路径 | 当前工程代码状态 | 真实生产级实验要求 |
|---|---|---|---|
| `pure_hbm` | 所有 KVCache 严格保留在本地 HBM 显存池中，作为容量与时延的黄金基线 | Python 脚本仅输出固定演示统计 | 真实显存分配与相同的在线请求流量 |
| `mooncake_native_ssd` | Mooncake 开源参考实现的 SSD 分层存储路径 | Python 脚本仅输出固定演示统计 | 明确其数据路径是否经过 Host DDR，并采集底层真实 I/O 证据 |
| `hbm_ddr_tier` | HBM 先经由 Host DDR 中转缓冲，再与 NVMe SSD 进行数据交互 | C++ 与 Python 均为 DEMO 模拟 | 真实的 Host DDR 缓冲池、异步 SSD I/O 及 Host Touch 字节统计 |
| `hbm_ssd_direct` | NVMe SSD 与 NPU HBM 之间直接进行 P2P DMA / GDS 直达数据流转 | 当前仅输出单行 DEMO 数据 | 真实的显存目标物理地址、DMA 硬件完成中断及 Host CPU 零拷贝证明 |

上述四种模式必须在完全相同的模型权重、Prompt 长度分布、KV 块大小、并发请求数、硬件工作温度及请求到达时序下开展公平 A/B 对照。若对比基线无法在相同物理硬件上运行，必须规范标记为 `CONDITIONAL`，严禁直接跨环境横向比对。

### 3.2 容量扩展与负载压力矩阵

| 测试维度 | 正式实施计划标准 | 工程说明与约束 |
|---|---|---|
| HBM 物理配额 | 设备物理显存可用容量的 50%、70%、90% | 用于模拟不同显存水位下的容量压力边界 |
| 超配比例 (Overcommit) | 1.0×、1.25×、1.5×、2.0× | 检验显存池超出物理容量时的分层支撑能力 |
| 并发请求数 | 1、4、16、32 并发 | 观测不同并发负载下的 I/O 队列与显存争用 |
| KV 块尺寸 (Block Size) | 1MB、4MB、16MB、64MB | 严格保持 4KB 对齐，分析不同块大小下的总线吞吐与碎片率 |
| I/O 硬件队列深度 (QD) | 1、4、16、32 深度 | 观测底层存储控制器的并发吞吐与排队时延 |
| 测试请求样本量 | 每测试点至少完成 500 个有效请求 | 预热阶段请求必须与稳态评测请求严格隔离记录 |
| 访问局部性分布 | 热点复用率 70%、90%；冷块换入比例 10%、30% | 构造真实大模型多轮对话与知识库检索的访存分布 |
| 后台存储压力 | 无后台 I/O、半饱和带宽、目标设备满载可用带宽 | 检验前后台混压下的抗干扰能力 |

### 3.3 物理运行环境严格冻结

在每个测试点正式执行前，必须自动采集并持久化以下环境快照：

- 执行 `lspci -vv` 导出 NPU 加速卡、NVMe 控制器、PCIe Root Complex 拓扑及 NUMA 亲和性关系；
- NVMe 固态硬盘型号、固件版本、Namespace 划分、物理扇区大小、文件系统挂载参数或裸盘标识；
- 加速器驱动版本、运行时环境、GDS 模块或原厂内核驱动加载状态；
- CPU 核心亲和性绑定、频率调节策略、操作系统 I/O 调度器及透明大页 (THP) 配置；
- 前台在线推理服务引擎版本与网络通信配置；
- 测试前后硬件设备的温度记录、SMART 健康计数器及总线错误统计。

严禁将生产真实业务数据直接用于换出测试。必须采用可生成、可追溯、严格脱敏且支持自动清理的合成张量样本，并完整记录随机种子与张量数据布局。

---

## 4. 工具审计与最小实现增量

### 4.1 当前命令行参数与边界

当前受控 C++ 基准程序仅支持以下 CLI 参数：

```text
--device <path-or-label>   NVMe 块设备或文件路径标签
--block-size <K|M|G>       单次 I/O 块字节尺寸
--qd <integer>             内部循环队列深度
--out <csv-path>           输出结果 CSV 目标路径
```

当前受控 Python 评估脚本仅支持以下 CLI 参数：

```text
--mode <pure_hbm|mooncake_native_ssd|hbm_ddr_tier|hbm_ssd_direct>
--overcommit <float>       显存超配系数
--concurrency <integer>    并发请求数
--out <json-path>          输出结果 JSON 目标路径
```

特别说明：当前 Python 脚本尚未集成 `--evidence-level` 或 `--loops` 等参数；执行时严禁传入未支持的选项。

### 4.2 当前 Makefile 与依赖边界分析

当前受控工程 `Makefile` 仅编译 `tier_storage_bench.cc` 的单机 DEMO 程序，未链接 `liburing`、SPDK、GDS 或昇腾 CANN 驱动运行时。因此，在正式开展生产级评测前，必须补齐以下最小工程增量：

1. **底层存储 I/O 引擎选型固化**：明确并固化基于 `io_uring` 固定缓冲区或 SPDK NVMe 驱动的用户态直达实现；
2. **真实 NVMe 读写与中断采集**：实现向真实磁盘提交 Direct I/O 操作，逐描述符采集提交时间戳、完成中断、实际字节数及硬件错误码；
3. **真实显存缓冲区映射**：将 I/O 目标地址严格绑定至 NPU 真实分配的 HBM 显存物理地址，杜绝使用普通的 Host `malloc` 内存；
4. **Host CPU 零拷贝探针接入**：针对 `hbm_ssd_direct` 直达路径与 `hbm_ddr_tier` 中转路径分别挂载 eBPF 动态探针，精准采集 Host Payload Touch 字节数；
5. **真实推理打流与时间线对齐**：对接在线推理流量，将后台分层换入换出事件与前台推理请求的 TTFT/TPOT 时间线实现精准关联；
6. **分层状态机与水位线控制**：开发 `TierBlockAllocator` 分层分配器，实现代际管理、Watermark 自动换出及异常安全回退。

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结实验身份、环境快照与代码版本

- **操作意图**：确保所有实测数据均可精准复现并追溯至唯一的源码版本、硬件拓扑及配置参数。
- **执行命令**：

```powershell
$runId = "PVT-05-$(Get-Date -Format yyyyMMdd-HHmmss)-direct"
$resultDir = "results/pvt05/$runId"
New-Item -ItemType Directory -Force $resultDir | Out-Null
git rev-parse HEAD | Out-File "$resultDir/git_commit.txt" -Encoding utf8
Get-Date -Format o | Out-File "$resultDir/timestamp.txt" -Encoding utf8
```

- **应观察现象**：结果目录创建成功，Git Commit 哈希与 ISO 时间戳完整非空。
- **判定边界**：若工作区存在未提交的修改，必须将 `git diff -- 原型验证代码/PVT-05` 完整归档；无法锁定源码版本时，该轮测试直接判定为 `INVALID-EVIDENCE`。

### 步骤 1：运行当前 C++ DEMO 校验格式与流程

- **操作意图**：验证输出 CSV 的 Schema 结构与参数解析逻辑，同时明确记录当前程序尚未产生真实硬件 I/O 的事实。
- **执行命令**：

```powershell
cd ./原型验证代码/PVT-05
make clean
make
./tier_storage_bench --device /dev/nvme0n1 --block-size 16M --qd 32 --out "$resultDir/tier_storage_demo.csv"
```

- **应观察现象**：输出 CSV 包含 direct 与 staging 两类固定演示行，状态明确标记为 `DEMO,DEMO_ONLY`；由于未实际打开块设备，程序不会因设备不存在而报错。
- **判定边界**：本步骤仅证实基准测试程序的输出格式合法，不可据此断言硬件带宽、时延或 Host DDR 是否被绕过。

### 步骤 2：运行当前 Python 四模式 DEMO

- **操作意图**：验证四种分层模式的 JSON 结果落盘格式，核对输入参数与模式枚举的自洽性。
- **执行命令**：

```powershell
python ./benchmark_tiering.py --mode pure_hbm --overcommit 1.0 --concurrency 1 --out "$resultDir/pure_hbm.json"
python ./benchmark_tiering.py --mode mooncake_native_ssd --overcommit 1.25 --concurrency 4 --out "$resultDir/mooncake_native_ssd.json"
python ./benchmark_tiering.py --mode hbm_ddr_tier --overcommit 1.5 --concurrency 16 --out "$resultDir/hbm_ddr_tier.json"
python ./benchmark_tiering.py --mode hbm_ssd_direct --overcommit 2.0 --concurrency 32 --out "$resultDir/hbm_ssd_direct.json"
```

- **应观察现象**：导出的 JSON 文件中 `evidence_level` 标注为 `DEMO`，`status` 标注为 `DEMO_ONLY`，`host_payload_touch_bytes` 为 null。
- **判定边界**：严禁依据上述输出计算容量扩展收益、OOM 降低比例或 SSD 实际带宽；若分析报告将 `DEMO_ONLY` 篡改为 `GO`，报告本身判定为 `INVALID-EVIDENCE`。

### 步骤 3：核对并显式登记证据缺口

- **操作意图**：在投入真实硬件测试前，地毯式梳理当前原型代码与生产级要求之间的技术差距，明确当前未支持特性。
- **执行命令**：

```powershell
rg -n "DEMO|DEMO_ONLY|host_payload_touch_bytes|direct_bw|ddr_bw|device|qd" 原型验证代码/PVT-05
Get-ChildItem 原型验证代码/PVT-05 -Force
```

- **应观察现象**：C++ 源码中存在固定预设数值与 DEMO 注释；目录下未包含 `tier_allocator.h`；当前输出缺乏硬件 I/O 完成中断、显存物理基地址及 Host Touch 探针计数。
- **判定边界**：未补齐上述底层驱动与探针前，PVT-05 结论严格限定为接口规范预演，不可进入 `MEASURED` 生产级判定。

### 步骤 4：接入单块真实 NVMe I/O 实测（条件步骤）

- **前置条件**：真实 NVMe 块设备已挂载就绪；`io_uring` 或 SPDK 驱动环境已搭建；数据安全校验方案已冻结。
- **操作意图**：在单数据块级别证实 NVMe 控制器到 NPU 显存物理地址的直达数据通路，验证 4KB 对齐与数据完整性。
- **执行动作**：针对 4KB、1MB、4MB 三种对齐长度分别执行单读、单写及读后数据校验；逐次记录 `submit_ts`、`complete_ts`、`nvme_lba`、目标显存物理地址及实际完成字节数；同步采集 Host Touch 探针数据。
- **应观察现象**：硬件完成中断返回的字节数与请求长度严格一致；数据校验 100% 正确；`hbm_ssd_direct` 路径的 Host Payload Touch 严格为 0，而 `hbm_ddr_tier` 路径能观测到明确的正文内存中转。
- **判定边界**：若完成字节数不符、目标缓冲区非显存物理地址或探针未完整覆盖 I/O 周期，该测试点判定为 `INVALID-EVIDENCE`，严禁扩大队列深度。

### 步骤 5：运行四模式同场景 A/B 压力实测（条件步骤）

- **操作意图**：在真实大模型请求流量下，严密对比四种模式的显存容量利用率、换入换出吞吐、前台在线推理时延及 Host DDR 的角色分工。
- **执行动作**：在固定模型权重、Prompt 随机种子、KV 块尺寸、HBM 物理配额及请求序列下，依次运行 `pure_hbm` 基线与三种分层模式；全流程采集前台 TTFT、TPOT、SLO 达标率、OOM 次数、抢占次数、后台 I/O 吞吐及 Host Touch 计数。
- **应观察现象**：四种模式均具备完整的物理时间线；直达模式的正文 Host Touch 严格为 0；DDR 中转模式的触碰字节数与实际换入换出量严格吻合；分层状态迁移次数与硬件完成中断数完全对齐。
- **判定边界**：若某模式仅生成静态数据、请求样本量不足或关键字段缺失，必须从横向对比中剔除并规范标记为 `CONDITIONAL` 或 `INVALID-EVIDENCE`。

### 步骤 6：全链路前后台混压与异常回退（条件步骤）

- **操作意图**：确证后台高负荷分层 I/O 在存储总线满载时不会无条件破坏前台在线推理的 TPOT 尾部稳定性，并验证 Host DDR 仅在异常回退时被条件性启用。
- **执行动作**：以前台无后台 I/O 作为基准；逐步增加后台冷块换出与换入压力直至设备饱和；主动注入 I/O 超时、租约失效及短 I/O 异常；检验系统是否平稳触发安全回退，确认是否存在陈旧代际 (Old Generation) 数据的错误消费。
- **应观察现象**：正常直达路径完全绕过主机内存；异常时具备清晰的错误归因与状态迁移记录；回退期间绝不会将未完成或已过期的脏数据块标记为 `READY`。
- **判定边界**：若无法提供状态迁移与数据校验凭证，不能判定容错成立；若异常后仍错误消费陈旧代际数据，该项直接一票否决判定为 `NO-GO`。

### 步骤 7：标准证据包归档与复核对账

- **操作意图**：将全量原始数据、硬件拓扑快照、状态迁移日志及异常记录标准化归档，支持跨团队独立复核。
- **执行动作**：在 `results/pvt05/<run_id>/` 目录下完整归档 `tier_storage.csv`、`mode_results.jsonl`、`block_state_transitions.jsonl`、`io_completion.jsonl`、`host_payload_touch.jsonl`、`foreground_latency.csv`、`errors.log` 及 `summary.md`。
- **应观察现象**：摘要报告中的每一个吞吐指标、时延分位数及容量扩展数值均能精准回溯至底层原始事件日志。

---

## 6. 数据采集清单与记录格式

### 6.1 底层存储设备 I/O 原始事件字段

```csv
run_id,request_id,block_id,mode,nvme_lba,offset_bytes,requested_bytes,completed_bytes,submit_ts_ns,complete_ts_ns,queue_depth,target_kind,target_addr,host_payload_touch_bytes,evidence_environment,evidence_level,status,error_code
```

字段填写约束：`target_kind` 必须严格区分 `HBM`、`HOST_DDR` 与 `FILE_BUFFER`；`actual_path` 必须基于底层实际观测结果填报，严禁直接从 `planned_path` 复制。

### 6.2 分层状态机流转原始事件字段

```csv
run_id,request_id,block_id,generation,old_state,new_state,reason,submit_ts_ns,complete_ts_ns,lease_expire_ns,bytes,actual_path,status
```

约束说明：状态迁移记录若缺失 Generation 代际、触发原因或硬件完成时间戳，无法证明时序与旧数据保护逻辑正确。

### 6.3 四模式汇总 CSV 模板

以下为微基准汇总输出字段格式规范（非预置实测成绩）：

```csv
run_id,mode,hbm_quota_bytes,overcommit,concurrency,block_size,queue_depth,request_count,completed,oom,preempt,throughput_gbps,io_p50_ms,io_p95_ms,io_p99_ms,ttft_p50_ms,tpot_p99_ms,cpu_util,ddr_bytes,host_payload_touch_bytes,planned_path,actual_path,evidence_environment,evidence_level,status,invalid_reason
<PVT-05>,<pure_hbm_or_native_ssd_or_ddr_tier_or_ssd_direct>,<bytes>,<ratio>,<conc>,<bytes>,<qd>,<count>,<completed>,<oom>,<preempt>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<bytes_or_null>,<bytes_or_null>,<planned>,<actual>,<W0_OR_W1_OR_W2>,<DEMO_OR_LAB_OR_MEASURED>,<status>,<null_or_reason>
```

---

## 7. 候选准入门槛、判定规则与立即止损机制

### 7.1 候选工程准入门槛

以下为评测验收的候选准入门槛（非当前测试桩已达到的结论）：

| 评估维度 | 候选工程准入门槛 | 客观物理证据与说明 |
|---|---:|---|
| 可服务上下文容量 | 同等 HBM 配额下，分层系统可服务 KV 容量相对纯 HBM 基线提升 $\ge 30\%$ | 必须基于真实完成的推理请求与相同请求序列计算 |
| OOM 与请求抢占抑制 | 在 1.5× 显存超配压力下，OOM 次数与抢占率相对基线下降 $\ge 50\%$ | 严禁通过减少请求量或篡改负载分布人为降低 |
| NVMe 直达有效带宽 | 稳态 Direct 路径顺序读写带宽达到物理设备标称峰值的 $\ge 80\%$ | 需明确标注读写比例、块尺寸及硬件队列深度 |
| Host CPU 正文拷贝 | 正常 `hbm_ssd_direct` 直达路径下 Host Payload Touch 严格 $= 0$ | 必须由动态探针或硬件计数器完整覆盖数据传输全生命周期 |
| 前台 TPOT 干扰率 | 满载混压下前台在线推理 TPOT P99 恶化率 $< 3\%$ | 需在统一的硬件 QoS 与统计时间窗口下评估 |
| 语义与代际一致性 | 所有就绪块 100% 通过语义与代际校验，陈旧失效数据 0 错误消费 | 发生任何一次陈旧脏数据读取即直接一票否决 |

### 7.2 状态判定枚举与规则

- **GO（全链路证据形成完整闭环）**：关键硬件路径打通，全量候选准入门槛达标，数据一致性与零拷贝校验 100% 通过；
- **CONDITIONAL（局部硬件或场景达标）**：局部路径成立，但特定拓扑、QoS 流控或某项次要门限受限；结论严格绑定已测条件；
- **NO-GO（关键路径未达标或发生数据损坏）**：直达路径未打通、发生陈旧数据错误消费，或前台 TPOT 干扰超出止损门限；
- **NOT-SUPPORTED（物理环境未支持）**：现场硬件、底层驱动或运行时环境缺乏目标直达能力；
- **INVALID-EVIDENCE（无效证据）**：原始证据缺失、关键字段为空、静态预置数值冒充实测、计划路径与实际路径不一致或统计不可复核。

### 7.3 立即安全止损条件

在测试过程中凡触发以下任一异常，必须立即终止测试并保存现场：

- NVMe 磁盘返回短 I/O 错误、介质坏块错误或硬件完成数与提交数不一致；
- NPU 显存物理目标地址校验失败，或发生 DMA 跨越显存边界非法越界写入；
- `hbm_ssd_direct` 直达路径测得的 Host Payload Touch 大于 0 且缺乏合理的物理归因；
- 已过期的陈旧 Generation 代际块或未完成写入的半写块被错误标记为 `READY` 并被下游算子消费；
- 前台在线推理 TPOT 尾部时延持续飙升超出预设止损红线，且后台 I/O 未能实现自适应退避降速；
- 存储设备温度、SMART 健康计数器或文件系统错误超出硬件安全运行范围。

---

## 8. 执行阶段划分与交付闭环

测试实施划分为四个严密的演进阶段：

| 实施阶段 | 核心攻坚内容 | 阶段必须交付物 | 准出判定条件 |
|---|---|---|---|
| E0（接口与证据规范确认） | 严密核对四模式 Schema、CLI 参数、结果目录结构及无效证据拦截逻辑 | 源码审计报告、DEMO 输出 CSV/JSON、规范化 manifest | 确认当前代码边界，杜绝将测试桩冒充实测 |
| E1（单块真实 I/O 与零拷贝） | 打通 4KB 对齐、真实 DMA 完成中断、显存物理基地址映射及 Host CPU 零拷贝对账 | 单块 I/O 日志、真实完成计数器、eBPF 探针原始日志 | 证实单块直达路径成立且 Host Payload Touch=0 |
| E2（分层状态机与容量扩容） | 开发分层块分配器，实现代际管理、Watermark 自动换出及多参数矩阵压力测试 | 状态机流转日志、容量超配实测表、OOM/抢占下降曲线 | 证实可服务容量提升 $\ge 30\%$ 且 OOM 下降 $\ge 50\%$ |
| E3（全链路前后台混压总门禁） | 在前台在线推理与后台分层存储满载 I/O 混压下，全量验证容量、TTFT、TPOT 及一致性 | 混压时间线分析报告、TPOT 干扰率评估表、最终判定结论 | 证实全链路 TPOT 干扰率 $< 3\%$ 且 0 错误消费 |
| 条件证伪（DDR 角色与依赖） | 针对 GDS 直达、io_uring/SPDK、NUMA 及 PCIe 拓扑开展严格证伪，摸清技术边界 | 软硬件依赖对照矩阵、降级回退边界报告 | 若硬件不支持直达则规范降级为 DDR 中转，严禁虚标 |

---

## 9. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师核心职责**：
  1. 确认现场部署的 NVMe 固态硬盘型号、PCIe 拓扑、NPU 显存物理地址、`io_uring`/SPDK 驱动及系统特权；
  2. 固化 HBM 物理配额、超配系数、并发度、KV 块尺寸、I/O 队列深度及停止测试的安全红线；
  3. 实际启动/停止被测进程与存储监控工具，完整留存 SMART 计数器、系统 dmesg 日志及异常错误；
  4. 严格审定 `host_payload_touch_bytes=0` 是否确由底层硬件直接完成数据搬运且探针全程覆盖；
  5. 对分层存储机制是否达到生产准入标准承担最终技术复核责任。
- **AI Agent 协同职责**：
  1. 深入研读本方案设计、公共测试契约及原型源码，精准梳理实际支持的 CLI 参数、依赖库及当前未实现特性；
  2. 编写原始 I/O 事件解析、分层状态机日志审计、容量超配统计、时延分位数计算及标准证据包生成工具；
  3. 严格核验 `actual_path` 与 `planned_path` 的一致性、4KB 对齐合法性、状态迁移时序及状态枚举归一化；
  4. 严守学术与技术诚信红线，严禁虚构驱动接口、伪造磁盘吞吐或编造零拷贝证据。

### 9.2 可直接复制给 Coding Agent 的 Prompt 模板

```text
我正在执行 PVT-05：HBM-SSD 直达容量主路径与 DDR 条件角色 Tiering 验证。

请先研读以下核心文件：
1. ./提前验证方案设计/验证计划方案设计/06_PVT-05_HBM-SSD直达容量主路径与DDR条件角色Tiering验证实施方案设计.md
2. ./提前验证方案设计/验证计划方案设计/Benchmark公共契约与证据分级规范.md
3. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-05/Makefile
4. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-05/tier_storage_bench.cc
5. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-05/benchmark_tiering.py

执行约束与任务要求：
- 首先梳理源码实际支持的 CLI 参数与底层执行行为；确认 tier_storage_bench.cc 与 benchmark_tiering.py 当前仅输出固定数值的 DEMO 数据，源码中未打开 NVMe 设备、未调用 io_uring/SPDK/GDS，且不存在 tier_allocator.h 或 eval_tiering.py。
- 明确区分 planned_path 与 actual_path；在缺乏真实 NVMe I/O、显存物理基地址及 eBPF 探针证据的前提下，测试结果统一限定标记为 DEMO 或 INVALID-EVIDENCE，严禁依据静态预设数据计算容量扩展比例或 SSD 带宽。
- 设计真实硬件扩展时，必须严格要求记录真实 I/O 完成中断、4KB 严格对齐、LBA 扇区地址、显存物理基地址、Generation 代际版本号、租约到期时间戳及全生命周期的 Host Payload Touch 探针数据。
- 状态机流转日志必须完整记录每个物理块的状态迁移轨迹；严禁将未完成写入的半写块或过期代际数据标记为 READY。
- 未采集到的字段显式置为 null 并详细注明 invalid_reason；完整留存硬件异常与失败请求；将 DEMO_ONLY 规范归一化为公共契约状态。
- 最终输出：源码能力核验矩阵、实际执行命令清单、四模式性能对照表、状态机流转审计表、未支持特性清单以及下一步最小代码重构建议。
```

### 9.3 常见排错指南

- **direct 与 staging 模式输出的数值完全固定且无任何设备错误**：此为当前受控源码处于 DEMO 占位阶段的预期表现；测试结果应规范标记为 `DEMO`，切勿据此对比带宽优劣。
- **导出的 `host_payload_touch_bytes` 字段为空**：空值代表尚未接入完整的动态探针，绝不代表零数据拷贝成立；该测试点必须记录为 `INVALID-EVIDENCE`。
- **执行命令传入 `--evidence-level` 参数报错**：当前受控 Python 脚本 CLI 尚未实现该参数；应按照实际支持的 CLI 参数执行，证据等级在外部元数据清单中补齐。
- **工程中找不到 `tier_allocator.h` 头文件**：当前仓库中尚未包含分层分配器实现；在未完成代码开发前，严禁宣称已完成 Watermark 水位线自动换出验证。
- **提升硬件队列深度 QD 但设备吞吐与完成数毫无变化**：排查 `qd` 是否仅为打印字段，确认底层后端驱动是否真正启用了异步并发硬件队列。
- **测试模式标记为 direct 但探针测得非零的正文内存触碰**：说明底层驱动发生了静默回退，实际执行了 Host DDR 中转；必须将 `actual_path` 修正为中转模式并判定为 `NO-GO` 或 `INVALID-EVIDENCE`。
- **测试中 OOM 发生率大幅下降但实际完成请求数显著减少**：说明 A/B 对照的工作负载不一致；必须废弃该测试点，重新冻结请求流量后重新测试。
- **注入异常后陈旧代际 (Old Generation) 数据块依然被算子消费**：说明代际校验与租约安全守卫存在严重缺陷；必须立即终止测试，判定为一票否决的 `NO-GO` 并紧急修复状态机。
