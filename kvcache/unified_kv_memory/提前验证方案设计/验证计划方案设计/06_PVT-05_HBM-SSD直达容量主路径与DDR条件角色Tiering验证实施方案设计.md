# PVT-05：HBM-SSD 直达容量主路径与 DDR 条件角色 Tiering 验证实施方案设计
## —— 分层存储容量扩展与数据面路径验证：NVMe SSD 直达、Host DDR 条件回退与在线请求影响

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个 `run_id` 必须冻结设备、文件系统或裸盘、数据块、路径、队列深度、并发、容量超配、预热、样本量和证据等级；结果必须保留原始 I/O 事件、实际完成字节、失败原因、实际路径、Host CPU/DDR 观测和结论状态。关键字段为空、为脚本固定值、无法与设备计数器核对，或 `actual_path` 与 `planned_path` 不一致时，不得进入 `MEASURED` 汇总。

> **验证范围声明**：当前受控工程中的 `tier_storage_bench.cc` 只解析设备参数并输出固定 `DEMO / DEMO_ONLY` 行，未打开 NVMe 设备、未调用 `io_uring`/SPDK/GDS、未分配真实 HBM；`benchmark_tiering.py` 只生成四种模式的固定请求、完成、OOM、抢占、Token 和 CPU/DDR 字段，未执行真实分层换入换出、在线推理或容量压力。因此现有源码只能检查字段和状态归档，不能单独证明 NVMe SSD 与 HBM 直达、正文绕过 Host DDR、容量扩展或在线 SLO 收益；缺少真实设备 I/O、目标显存地址、完成事件、Host Payload Touch 探针和原始日志时，结果只能标记为 `DEMO`/`LAB`。

> **术语速查**：KVCache（大模型注意力键值缓存，即自回归生成过程中保存历史 Key 和 Value 激活状态、避免后续 Token 重复计算注意力）；Tiering（分层存储技术，即将热 KV 保留在显存、冷 KV 异步沉淀到 NVMe SSD，并在命中时按需换入）；HBM（High Bandwidth Memory，高带宽显存）；NVMe SSD（通过 PCIe/NVMe 协议访问的固态盘，承担冷 KV 的大容量介质角色）；GDS（GPUDirect Storage，显存直读存储技术，即 NVMe SSD 与显存通过 DMA 传输并绕过主机内存）；io_uring（Linux 异步 I/O 接口，用于批量提交和回收存储请求）；Payload Bypass DDR（绕过主机内存，即正文 KV 数据不经 Host DDR 中转）；Host Payload Touch Bytes（Host CPU 或 Host DDR 触碰正文数据的字节数，目标直达路径要求为 0 但必须由探针或硬件计数器证明）；O_DIRECT（绕过页缓存的文件 I/O 方式，不能单独证明绕过 Host DDR）；LBA（Logical Block Address，逻辑块地址）；Watermark（显存水位线，用于触发和停止冷块换出）；O_QD（Outstanding Queue Depth，设备同时在途的 I/O 请求数）。

> **验证 ID**：PVT-05
> **验证名称**：HBM-SSD 直达容量主路径、DDR 条件角色与 Tiering 分层存储验证
> **验证优先级**：**🔴 P0 级（核心关键项）**
> **对应验证阶段**：**E2（动态调度决策与分层扩容）**
> **证伪标记**：否（分层存储路径与容量扩展确认）
> **建议周期**：5~6 人日
> **主关联 IR**：`IR-01-01`, `IR-02-08`, `IR-02-09`
> **核心 SRS / SR23 锚点**：
> - SRS：`L3-MS-Tiering-038`, `L3-MC-HIER-STORE-002`, `L3-MS-DDRRolePolicy-092`, `L3-SE-TierBypassPolicy-091`
> - SR23：`SR23-01-01-01`, `SR23-01-01-02`, `SR23-01-01-03`, `SR23-01-08-01`, `SR23-02-08-01`, `SR23-02-09-01`
> **配套源码**：[`提前验证方案设计/验证计划方案设计/原型验证代码/PVT-05/`](file:///d:/codes/reports/kvcache/unified_kv_memory/提前验证方案设计/验证计划方案设计/原型验证代码/PVT-05)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`；正式结果必须绑定实际 I/O 后端、驱动/运行时、NVMe、加速器和配置哈希，不能只引用本文默认 Commit。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统分层路径的性能问题

传统显存不足处理通常把冷 KV 先写入 Host DDR，再由 CPU 或内核块层转发到 SSD；换入时按相反方向搬运。该路径的正文数据至少经过以下环节：

```text
HBM -> Host DDR staging -> filesystem/page cache or kernel block layer -> NVMe SSD
NVMe SSD -> filesystem/page cache or kernel block layer -> Host DDR staging -> HBM
```

这条路径的风险不是“CPU 是否参与计算”，而是 Host DDR 既占用容量，又承受正文数据带宽，可能与在线推理的控制、网络和页表流量竞争。目标路径应明确区分：

```text
控制面：Host CPU -> descriptor / completion / lease / error handling
数据面：NVMe SSD <==== DMA / GDS-like path ====> HBM
条件回退：设备或租约异常 -> Host DDR staging -> 本地重算或安全恢复
```

这里的“直达”必须由真实 DMA 目标地址、I/O 完成记录和 Host Payload Touch 探针共同证明；仅仅把模式命名为 `hbm_ssd_direct` 不构成证据。

### 0.2 当前仓库的文件和实际能力

| 文件 | 当前可确认行为 | 不能据此声称的能力 |
|---|---|---|
| `原型验证代码/PVT-05/tier_storage_bench.cc` | 解析 `--device`、`--block-size`、`--qd`、`--out`；固定输出两类路径的吞吐、时延和 CPU 数值；输出行标记为 `DEMO,DEMO_ONLY` | 未打开 NVMe 设备；未调用 `io_uring`、SPDK、GDS 或真实 HBM；没有真实完成队列 |
| `原型验证代码/PVT-05/benchmark_tiering.py` | 生成四种模式的固定请求数、完成数、OOM、抢占、Token 和 CPU/DDR 字段 | 没有真实 HBM 分配、SSD 换入换出、分层块管理或在线推理服务 |
| `原型验证代码/PVT-05/Makefile` | 只提供当前 C++ DEMO 的编译入口 | 没有发现 `liburing`、SPDK、GDS 或加速器运行时链接 |
| PVT-05 目录 | 当前没有 `tier_allocator.h` 或 `eval_tiering.py` | 旧文档中引用的分层分配器和评估脚本目前不存在 |

`tier_storage_bench.cc` 的注释已明确说明它是固定结果 schema 的 DEMO，需要后续替换为 SPDK/io_uring 完成路径和 Host Touch 探针。因此当前仓库状态应先判为“接口与证据格式预演”，而不是“HBM-SSD 直达链路已验证”。

### 0.3 当前 DEMO 的输出语义

当前 C++ 程序固定生成 `nvme_direct_bypass_ddr` 和 `hbm_ddr_ssd_staging` 两行。所谓 direct 行的 `190.4 Gbps`、时延公式和 `1.2%` CPU 不是设备测量值；所谓 staging 行的 `85.2 Gbps` 和 `65.4%` CPU 也不是 Host 采样值。Python 脚本同样按模式写入固定的完成、OOM、抢占和 DDR 数值。

这些固定行可以用来检查 CSV 字段是否完整、状态机是否能解析、报告是否能区分四种模式，但不得用于宣称容量扩展比例、设备带宽或服务 SLO 收益。

## 1. 验证目标与交付物

### 1.1 目标

| 目标 | 必须回答的问题 | 最低证据 |
|---|---|---|
| 直达数据面 | NVMe SSD 到 HBM 的数据是否绕过 Host DDR | 真实设备 I/O、真实 HBM 地址、DMA/完成日志、Host Payload Touch 探针 |
| 分层控制面 | 热块换出、冷块换入、水位线和租约是否可追踪 | 块描述符、状态迁移日志、队列深度、回收原因 |
| 容量扩展 | overcommit 增大后，HBM 可服务容量和 OOM/抢占如何变化 | 相同请求流、相同模型、相同 HBM 配额下的 A/B 原始记录 |
| 前台影响 | 后台 SSD I/O 是否改变 TTFT/TPOT 和请求达标率 | 前台时延分位数、后台 I/O 分位数、混压时间线、QoS 配置 |
| 条件角色 | Host DDR 是否只在不支持直达、故障或回退时使用 | 按路径分层的 Host Payload Touch 计数和异常原因 |

### 1.2 交付物

1. 设备和加速器拓扑、驱动、文件系统或裸设备配置快照；
2. `tier_storage_bench` 的原始输出、真实提交/完成计数和 Host Touch 探针日志；
3. 四种模式的同场景请求结果及 A/B 对照表；
4. 分层块描述符、状态迁移、watermark 触发与异常回退日志；
5. `MEASURED`、`LAB`、`DEMO` 的证据分级表和最终 `GO/CONDITIONAL/NO-GO` 判定。

## 2. 目标数据结构、分层状态与物理约束

### 2.1 目标分层块描述符

当前仓库尚未提供该结构。下面是实现前需要冻结的最小字段，不应被误解为现有代码已经具备的接口：

```cpp
struct TierBlockDescriptor {
    uint64_t block_id;
    uint64_t kv_virtual_addr;
    uint64_t kv_bytes;
    uint64_t nvme_lba;
    uint32_t block_bytes;
    uint16_t tier;          // HBM=0, NVMe=1, DDR_FALLBACK=2
    uint16_t state;         // HOT, EVICTING, COLD, LOADING, READY, INVALID
    uint64_t generation;
    uint64_t lease_expire_ns;
};
static_assert(sizeof(TierBlockDescriptor) <= 64);
```

实际实现还必须明确：

- `kv_virtual_addr` 是否为设备可访问的 IOVA 或其他设备地址，而非仅 Host 虚拟地址；
- `nvme_lba` 是否按设备逻辑扇区对齐；
- `generation` 和 `lease_expire_ns` 如何防止旧块完成事件覆盖新映射；
- `INVALID`、超时和设备错误如何触发本地重算或安全回退；
- descriptor 本身是否在 Host DDR 中保存，以及这部分控制面内存不应计入正文 Payload Touch。

### 2.2 分层块分配器的目标行为

目标组件可命名为 `TierBlockAllocator`（分层块分配器，即统一管理 HBM、NVMe 和条件回退 DDR 块位置信息的组件），但当前仓库尚无对应头文件。实现前应先冻结以下状态迁移：

```text
HBM/HOT --watermark_high--> EVICTING --I/O complete--> NVMe/COLD
NVMe/COLD --cache hit--> LOADING --I/O complete--> HBM/READY
任一路径 --timeout/error/lease invalid--> INVALID -> recompute or DDR_FALLBACK
HBM/READY --lease refresh--> HBM/READY
```

状态迁移日志至少包含 `block_id`、旧状态、新状态、generation、提交时间、完成时间、错误码和回退动作。只输出最终容量统计，无法证明中途发生过何种换入换出，不足以支持状态机结论。

### 2.3 4KB 对齐与容量计算

NVMe 设备扇区、文件偏移、I/O 长度和 KV 块大小必须在实验前冻结。若块大小为 `B`、KV 元数据加数据总量为 `S`，则分配块数至少按：

```text
blocks = ceil(S / B)
aligned_bytes = blocks * B
```

当 `B` 为 4KB 的整数倍、且 `nvme_lba * sector_bytes`、`offset`、`length` 均满足设备要求时，才可进入真实 I/O 阶段。对齐不是直达证据；它只是避免因设备约束导致的隐式缓冲和短 I/O。

### 2.4 水位线策略

建议设置高水位 `H_high` 与低水位 `H_low`，并记录每次触发：

```text
used_hbm >= H_high: 选择冷块，异步换出，直到 used_hbm <= H_low
used_hbm <= H_low: 允许停止换出，保留 I/O 并发给前台请求
```

水位线本身不是收益结论。必须同时观察换出批量、I/O 排队、前台 TPOT、OOM 和抢占，避免通过过度换出换取表面容量而损伤在线服务。

## 3. 实验矩阵与冻结参数

### 3.1 四种运行模式

| 模式 | 目标路径 | 当前状态 | 真实实验要求 |
|---|---|---|---|
| `pure_hbm` | 所有 KV 只留在 HBM，作为容量与时延基线 | Python 仅输出固定统计 | 真实 HBM 分配和同一请求流 |
| `mooncake_native_ssd` | 参考实现的 SSD 分层路径 | Python 仅输出固定统计 | 明确参考实现是否经过 DDR，并提供其 I/O 证据 |
| `hbm_ddr_tier` | HBM 与 Host DDR staging，再与 SSD 交互 | C++/Python 均为 DEMO | 真实 DDR 缓冲、SSD I/O、Host Touch 计数 |
| `hbm_ssd_direct` | HBM 与 NVMe SSD 直接数据路径 | 当前只是一行 DEMO | 真实 DMA/GDS-like 目标地址、完成事件和拓扑支持 |

四种模式必须使用相同模型、相同 Prompt 分布、相同 KV 块大小、相同并发、相同设备温度和相同请求顺序。若参考实现不能在同一设备或同一请求流下运行，应标记为 `CONDITIONAL`，不能直接横向比较。

### 3.2 容量与负载矩阵

第一轮建议冻结以下矩阵；设备资源不足时可以减少点位，但必须在元数据中写明删减原因：

| 维度 | 建议值 |
|---|---|
| HBM 配额 | 设备可用容量的 50%、70%、90% |
| overcommit | 1.0、1.25、1.5、2.0 |
| 并发 | 1、4、16、32 |
| KV block size | 1MB、4MB、16MB、64MB；最终以 4KB 对齐 |
| I/O queue depth | 1、4、16、32 |
| 请求数 | 每点至少 500 个完成请求；预热请求单独记录 |
| 访问分布 | 热点复用 70%、90%；冷块换入比例 10%、30% |
| 后台压力 | 无后台、半带宽、目标设备可用带宽上限 |

当前 Python DEMO 固定 `total_requests=500`，但不执行真实请求。该数值只能作为结果 schema 的默认字段，不能替代真实工作负载。

### 3.3 环境冻结

每个实验点执行前保存：

- `lspci -vv` 中加速器、NVMe、PCIe Root Complex 和 NUMA 关系；
- NVMe 型号、固件、namespace、扇区大小、文件系统挂载参数或裸设备声明；
- 加速器驱动、运行时、GDS/内核模块状态；
- CPU 亲和性、NUMA 绑定、频率策略、I/O 调度器、透明大页设置；
- 网络和前台推理服务版本（若进行混压）；
- 实验前后设备温度、错误计数和 SMART 摘要。

严禁把生产数据直接作为换出内容。应使用可追踪、可清理、不可泄露的合成 KV 或脱敏样本，并记录其生成 seed 和张量布局。

## 4. 工具审计与最小实现增量

### 4.1 当前可执行命令

当前 C++ DEMO 的命令行只有以下参数：

```text
--device <path-or-label>
--block-size <K|M|G>
--qd <integer>
--out <csv-path>
```

当前 Python 脚本的命令行只有：

```text
--mode <pure_hbm|mooncake_native_ssd|hbm_ddr_tier|hbm_ssd_direct>
--overcommit <float>
--concurrency <integer>
--out <json-path>
```

当前 Python 脚本不接受 `--evidence-level`、`--loops` 或真实设备参数；将这些参数直接加入命令会失败，不能把预期接口写成“当前执行命令”。

### 4.2 当前 Makefile 与依赖边界

当前 `Makefile` 只编译 `tier_storage_bench.cc` 的 C++ DEMO。仓库没有发现 `liburing`、SPDK、GDS 或加速器运行时的链接配置。因此在真实实验前至少需要完成：

1. 选择并固定 `io_uring` 或 SPDK 的 I/O 后端；
2. 让后端真实提交 NVMe 读写并记录提交/完成/错误计数；
3. 让目标缓冲区来自设备可访问的 HBM 分配，而不是普通 Host `malloc`；
4. 为 staging 与 direct 两条路径分别接入 Host Payload Touch 观测；
5. 接入真实 workload，确保换入换出与前台请求的时间线可关联；
6. 增加 `tier_allocator`、状态机、watermark 和结果评估工具，或在文档中明确其尚未实现。

### 4.3 最小真实 I/O 验证路径

推荐先做小块、单队列、单请求的验证，再增加并发和容量压力：

```text
设备发现 -> 4KB 对齐读写 -> 真实 completion -> HBM 目标校验
-> Host Touch 观测 -> 多队列 -> 分层状态机 -> 前后台混压
```

任何一步失败，都应保留失败日志并停止向下一阶段扩展。尤其不能在没有单请求目标地址验证时直接运行全量 overcommit。

## 5. 逐步执行 SOP

### Step 0：冻结版本和实验目录

操作意图：让结果可以回到同一份源码、设备和参数。

执行动作：

```powershell
$runId = "PVT-05-$(Get-Date -Format yyyyMMdd-HHmmss)-direct"
$resultDir = "results/pvt05/$runId"
New-Item -ItemType Directory -Force $resultDir | Out-Null
git rev-parse HEAD | Out-File "$resultDir/git_commit.txt" -Encoding utf8
Get-Date -Format o | Out-File "$resultDir/timestamp.txt" -Encoding utf8
```

应观察现象：目录创建成功，commit 和时间戳非空。

判定边界：若工作树有与 PVT-05 相关的未提交修改，必须把 `git diff -- 原型验证代码/PVT-05` 保存到结果目录；无法确定源码版本时，该轮只能是 `INVALID-EVIDENCE`。

### Step 1：运行当前 C++ DEMO，确认接口而非设备能力

操作意图：确认结果 schema 和参数解析可用，同时显式记录当前程序没有真实设备 I/O。

执行动作：

```powershell
cmake --build 原型验证代码/PVT-05 --config Release
原型验证代码/PVT-05/tier_storage_bench.exe --device /dev/nvme0n1 --block-size 16M --qd 32 --out "$resultDir/tier_storage_demo.csv"
```

如果当前环境为 Linux，应使用实际生成的可执行文件路径；如果编译系统不是 CMake，应按该目录当前 Makefile 的真实入口执行，并把完整命令写入 `command.txt`。不要自行添加 `--loops` 或 `--evidence-level`。

应观察现象：输出包含 direct/staging 两类固定行，通常标记 `DEMO,DEMO_ONLY`；程序不会因为设备不可访问而证明设备路径失败，因为当前实现并未真正打开设备。

判定边界：该步骤只能判定 `DEMO` 的 schema 完整性；不能判定带宽、时延、CPU 占用或 Host DDR 是否被绕过。

### Step 2：运行当前 Python 四模式 DEMO

操作意图：确认四种模式的结果字段能落盘，并核对脚本输入参数和模式名。

执行动作：

```powershell
python 原型验证代码/PVT-05/benchmark_tiering.py --mode pure_hbm --overcommit 1.0 --concurrency 1 --out "$resultDir/pure_hbm.json"
python 原型验证代码/PVT-05/benchmark_tiering.py --mode mooncake_native_ssd --overcommit 1.25 --concurrency 4 --out "$resultDir/mooncake_native_ssd.json"
python 原型验证代码/PVT-05/benchmark_tiering.py --mode hbm_ddr_tier --overcommit 1.5 --concurrency 16 --out "$resultDir/hbm_ddr_tier.json"
python 原型验证代码/PVT-05/benchmark_tiering.py --mode hbm_ssd_direct --overcommit 2.0 --concurrency 32 --out "$resultDir/hbm_ssd_direct.json"
```

应观察现象：JSON 中的 `evidence_level` 为 `DEMO`，状态为 `DEMO_ONLY`，`host_payload_touch_bytes` 为空；`overcommit` 和 `concurrency` 进入元数据，但不驱动真实设备或服务行为。

判定边界：不得据此计算容量扩展收益、OOM 降幅、SSD 带宽或 DDR 绕过比例。若报告脚本把 `DEMO_ONLY` 当作 `GO`，报告逻辑本身应判为 `INVALID-EVIDENCE`。

### Step 3：检查证据缺口

操作意图：在投入真实设备前，把当前原型缺失的证据字段显式列出。

执行动作：

```powershell
rg -n "DEMO|DEMO_ONLY|host_payload_touch_bytes|direct_bw|ddr_bw|device|qd" 原型验证代码/PVT-05
Get-ChildItem 原型验证代码/PVT-05 -Force
```

应观察现象：C++ 源码存在固定数值和 DEMO 注释；目录中没有 `tier_allocator.h`、`eval_tiering.py`。当前输出没有真实 I/O completion、HBM 地址、LBA 映射或 Host Touch 计数。

判定边界：若没有补齐这些证据，PVT-05 只能停留在接口预演，不能进入 `MEASURED`。

### Step 4：接入单块真实 NVMe I/O

操作意图：先证明一个真实块从设备到设备可访问 HBM 的数据通路，再进入分层压力。

执行动作：

1. 冻结真实设备或测试文件，确认数据可清理且不会覆盖生产数据；
2. 选择 `io_uring` 或 SPDK，并把依赖、版本和编译参数写入环境快照；
3. 对 4KB、1MB、4MB 三种对齐长度分别执行单读、单写、读后校验；
4. 记录 `submit_ts`、`complete_ts`、`res`、`nvme_lba`、目标 HBM 地址和实际完成字节；
5. 对 direct 与 staging 路径分别采集 Host Payload Touch。

应观察现象：真实 completion 的 `res` 与请求长度一致；数据校验通过；direct 路径的 Host Payload Touch 为 0，staging 路径应能观测到非零正文触碰。

判定边界：只要完成字节不一致、目标缓冲区不是设备可访问 HBM、或者探针未覆盖整个 I/O 生命周期，该点标记 `INVALID-EVIDENCE`，不得继续扩大 QD。

### Step 5：运行四模式同场景 A/B

操作意图：在真实请求流中比较显存容量、换入换出、前台时延和 Host DDR 角色。

执行动作：

1. 固定模型、Tokenizer、Prompt seed、KV 布局、HBM 配额和请求顺序；
2. 先运行 `pure_hbm` 基线，再运行 `mooncake_native_ssd`、`hbm_ddr_tier`、`hbm_ssd_direct`；
3. 每个模式记录预热、稳态、回收和异常阶段，不能把预热阶段并入稳态均值；
4. 逐点改变 overcommit、并发、块大小和 QD；
5. 同时采集前台 TTFT、TPOT、达标率、OOM、抢占、后台 I/O 吞吐、设备时延、CPU/DDR 和 Host Touch。

应观察现象：四种模式均有完整运行时间线；直达模式的正文 Host Touch 仍为 0；DDR staging 模式的触碰量与实际换入换出字节可以对账；状态迁移数与 I/O completion 数量一致。

判定边界：若某模式只生成固定统计、请求数不一致、实际路径不明或关键字段为空，应从横向比较中剔除并标记 `CONDITIONAL` 或 `INVALID-EVIDENCE`。

### Step 6：前后台混压与异常回退

操作意图：确认后台分层 I/O 在设备和总线受压时不会无条件损伤前台在线推理，并验证 DDR 只在条件性回退时出现。

执行动作：

1. 以无后台 I/O 作为前台基线；
2. 增加后台冷块换出、冷块换入和混合读写压力；
3. 注入设备超时、租约过期、短 I/O 和目标缓冲区不可用事件；
4. 检查是否触发安全回退、是否发生错误消费、是否发生半写块进入 READY；
5. 将每个异常事件与前台请求 ID、块 generation 和最终动作关联。

应观察现象：正常 direct 路径不触碰 Host Payload；异常时有明确回退原因和可追踪状态迁移；回退期间不把未完成或过期块标记为 READY。

判定边界：若只能通过日志文字推测回退而没有状态和数据校验，不能判定容错成立；若异常后仍消费旧 generation 数据，该点为 `NO-GO`。

### Step 7：归档证据包

操作意图：让其他研发人员可以在同一版本环境复核结果。

执行动作：

```text
results/pvt05/<run_id>/
  metadata.yaml
  command.txt
  git_commit.txt
  environment.txt
  topology.txt
  tier_storage.csv
  mode_results.jsonl
  block_state_transitions.jsonl
  io_completion.jsonl
  host_payload_touch.jsonl
  foreground_latency.csv
  errors.log
  summary.md
```

应观察现象：所有摘要数值都能回指原始文件；所有路径和状态字段有统一枚举。

判定边界：缺少命令、源码版本或原始计数器时，只能保留为过程记录，不能作为评审结论。

## 6. 证据字段、结果格式与汇总模板

### 6.1 设备 I/O 记录

```csv
run_id,request_id,block_id,mode,nvme_lba,offset_bytes,requested_bytes,completed_bytes,submit_ts_ns,complete_ts_ns,queue_depth,target_kind,target_addr,host_payload_touch_bytes,evidence_level,status,error
```

`target_kind` 至少区分 `HBM`、`HOST_DDR`、`FILE_BUFFER`。`actual_path` 应由后端观测结果填写，不能直接复制 `planned_path`。

### 6.2 分层状态记录

```csv
run_id,request_id,block_id,generation,old_state,new_state,reason,submit_ts_ns,complete_ts_ns,lease_expire_ns,bytes,actual_path,status
```

状态迁移缺少 generation、原因或完成时间时，不能证明旧块保护和换入换出时序正确。

### 6.3 模式汇总

```csv
run_id,mode,hbm_quota_bytes,overcommit,concurrency,block_size,queue_depth,request_count,completed,oom,preempt,throughput,io_p50_ms,io_p95_ms,io_p99_ms,ttft_p50_ms,tpot_p99_ms,cpu_util,ddr_bytes,host_payload_touch_bytes,planned_path,actual_path,evidence_level,status
```

生成器和解析器需要在单元测试中检查字段名，避免把不可见字符带入结果。

### 6.4 证据分级

| 级别 | 允许内容 | 不允许内容 |
|---|---|---|
| `DEMO` | 固定输出、接口解析、状态机 schema 演示 | 任何设备吞吐、容量收益或 Host DDR 结论 |
| `LAB` | 测试文件、模拟设备或局部真实组件的可复核结果 | 直接外推到生产级 HBM/NVMe 拓扑 |
| `MEASURED` | 真实设备、真实 HBM、真实请求、完整探针和原始日志 | 关键字段缺失、路径未证实或只依赖脚本固定值 |

## 7. 判定标准、无效证据与风险止损

### 7.1 候选门限

以下是进入评审前的候选门限，不是当前代码已经达到的结论：

| 维度 | 候选要求 | 说明 |
|---|---:|---|
| 可服务容量 | 在同一 HBM 配额下，分层可服务 KV 容量相对纯 HBM 增加至少 30% | 必须用真实完成请求和相同请求流计算 |
| OOM/抢占 | overcommit 压力下 OOM 与抢占相对基线下降至少 50% | 不得通过减少请求数或改变请求分布获得 |
| SSD 有效带宽 | 稳态直达路径达到设备可用带宽的 80% 以上 | 需明确读写比例、块大小和 QD |
| Host Payload Touch | 正常 `hbm_ssd_direct` 路径为 0 | 必须由探针或硬件计数器覆盖完整运行区间 |
| 前台干扰 | 混压下 TPOT P99 相对无后台基线增加小于 3% | 需沿用统一 QoS 和统计窗口 |
| 数据正确性 | 所有 READY 块通过 checksum/版本校验，无旧 generation 消费 | 任一错误消费均为 `NO-GO` |

门限没有满足时，输出实际测量值、置信区间或重复运行范围和失败原因，不得通过改写模式名或删除异常点来满足门限。

### 7.2 状态枚举

- `GO`：关键路径和证据完整，候选门限满足，数据正确性通过；
- `CONDITIONAL`：局部路径成立，但设备拓扑、QoS、异常回退或某项门限仍有限制；
- `NO-GO`：关键路径不成立、出现错误消费、数据损坏或前台干扰超过止损线；
- `NOT-SUPPORTED`：当前硬件、驱动或运行时没有所需能力；
- `INVALID-EVIDENCE`：原始证据缺失、字段为空、固定值冒充实测、计划路径与实际路径不一致或统计不可复核。

当前脚本中的 `DEMO_ONLY` 是脚本内部状态，不得直接映射为 `GO`；汇总层应统一转换为 `DEMO` 证据级别并阻止生产级结论。

### 7.3 立即止损条件

出现以下任一情况，应停止扩大压力并保留现场：

- NVMe 返回短 I/O、介质错误或 completion 数与提交数不一致；
- HBM 目标地址验证失败，或设备 DMA 写入越界；
- Host Payload Touch 在 direct 路径出现非零且无法解释；
- 过期 generation 或未完成块进入 READY；
- 前台 TPOT 长尾持续超过预设止损线，且后台压力没有可控回退；
- 设备温度、健康计数器或文件系统错误超出实验安全范围。

## 8. 阶段推进与闭环

### E0：接口与证据前提确认

确认四模式 schema、命令行、结果目录、证据分级和无效证据处理正确。当前仓库只能完成这一层的 DEMO 预演。

### E1：单块真实 I/O 与 Host Touch

打通 4KB 对齐、真实 completion、设备可访问 HBM 目标和 direct/staging 两条路径的正文触碰对账。未完成前不进入容量压力。

### E2：分层状态机与容量扩展

实现块描述符、generation、watermark、换入换出、回收和错误回退，完成 overcommit、并发、块大小和 QD 矩阵。

### E3：全链路前后台混压

在真实在线推理和后台 SSD 分层 I/O 同时运行时，验证容量、OOM/抢占、TTFT、TPOT、设备带宽、Host Payload Touch 和数据一致性门限。

### 条件证伪：DDR 角色与外部依赖

对 GDS-like 直达、io_uring/SPDK、文件系统缓存、NUMA、PCIe 拓扑和异常回退逐项做 A/B。若硬件或驱动不能支持 HBM 目标 DMA，应明确把路径降级为 `hbm_ddr_tier`，不能继续使用 `hbm_ssd_direct` 名称包装结果。

## 9. 研发人员与 AI Agent 执行约束

### 9.1 研发人员检查清单

- [ ] 当前源码、命令行和文档中的文件名一致；
- [ ] 设备、HBM、驱动、GDS/io_uring/SPDK 依赖已冻结；
- [ ] direct/staging 的 planned path 和 actual path 分开记录；
- [ ] 真实 completion、LBA、目标地址和 Host Touch 可回溯；
- [ ] 四种模式使用相同请求流和统计窗口；
- [ ] 状态迁移包含 generation、租约、错误原因和最终动作；
- [ ] DEMO/LAB/MEASURED 没有混入同一汇总；
- [ ] 所有异常点保留，未通过删除数据来“修正”结论。

### 9.2 AI Agent 执行提示词

```text
你负责执行 PVT-05 HBM-SSD 直达容量主路径与 DDR 条件角色验证。

先读取项目索引、公共 Benchmark 契约和本方案，确认当前源码真实存在的文件、命令行和依赖。不得把设计目标写成现有能力。当前 PVT-05 原型中的 tier_storage_bench.cc 和 benchmark_tiering.py 是固定结果 DEMO；如果没有真实 NVMe/HBM、io_uring/SPDK/GDS 路径和 Host Payload Touch 证据，只能输出 DEMO 或 INVALID-EVIDENCE。

执行时固定 git commit、设备拓扑、模型、KV 布局、请求流、HBM 配额、块大小、QD 和并发。先做单块真实 I/O，再做分层状态机和四模式 A/B，最后做前后台混压与超时回退。记录 planned_path 与 actual_path，禁止用空的 host_payload_touch_bytes 代替零触碰证明。

输出必须包含原始 CSV/JSON、命令、环境快照、I/O completion、块状态迁移、Host Touch、时延分位数、异常日志和 GO/CONDITIONAL/NO-GO/NOT-SUPPORTED/INVALID-EVIDENCE 判定。若证据缺失，明确列出缺口和下一步实现，不得杜撰设备数据。
```

### 9.3 常见问题定位

| 现象 | 优先检查 | 处理方式 |
|---|---|---|
| direct 与 staging 输出完全固定且无设备错误 | 是否仍在运行当前 DEMO | 标记 `DEMO`，不要据此比较带宽 |
| `host_payload_touch_bytes` 为空 | 是否接入完整 Host Touch 探针 | 空值不是 0；标记 `INVALID-EVIDENCE` |
| `--evidence-level` 参数报错 | 当前脚本 CLI 未实现该参数 | 按实际 CLI 运行，证据等级写入元数据或先补接口 |
| 找不到 `tier_allocator.h` | 当前仓库尚未实现分层块分配器 | 不能声称 watermark/状态机已验证 |
| QD 提升但 completion 数不变 | `qd` 只是输出字段或后端没有真实队列 | 检查提交/完成计数和设备监控 |
| DDR Touch 非零但模式标记 direct | planned path 与 actual path 不一致 | 标记 `NO-GO` 或 `INVALID-EVIDENCE`，修正路径实现 |
| OOM 降低但请求数也减少 | A/B 工作负载不一致 | 丢弃该比较，重新冻结请求流 |
| 异常后旧块仍可被消费 | generation/lease 校验缺失 | 立即停止压力，判定 `NO-GO` |

本方案的完成标准不是“脚本成功退出”，而是建立一条可复核的证据链：真实设备 I/O → 真实 HBM 目标 → Host Payload Touch 对账 → 分层状态迁移 → 同场景容量与服务指标 → 异常回退和最终状态判定。
