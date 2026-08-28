# PVT-09：DPU 硬件安全处理、Raw Direct 主路径与故障回退验证实施方案设计
## —— DPU 硬件安全处理与 Raw Direct 主路径验证：在途处理、软硬双轨协同与故障回退

> **公共执行契约**：本项严格遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个测试 `run_id` 必须在运行前完整固化 DPU/网卡/NPU 硬件型号、驱动固件版本、PCIe/NUMA 物理拓扑、传输通道模式、Payload 数据量、加密算法与完整性校验策略、硬件队列深度、并发请求数、故障注入参数、代码包版本、配置哈希及目标证据等级；实测产出必须完整保留描述符提交/硬件完成中断/CQE 事件、超时监测日志、故障隔离动作、通道切换确认凭证、端到端请求结果、实测带宽/时延、Host CPU 占用率、Host Payload Touch 探针采样、网络丢包统计、数据完整性校验及底层硬件错误码。在现场环境缺乏特定硬件能力时，必须严格标记为 `NOT-SUPPORTED`，严禁使用静态模拟数据替代。

> **验证范围声明**：在当前受控的原型验证工程中，`offload_fallback_bench.cc` 仅解析 `--hardware-supported` 与 `--out` 参数，并在输出 CSV 中固定写入 Raw Direct、DPU 及故障回退的演示数据行；`inject_fault.py` 仅在等待固定时间后生成独立的故障描述 JSON，并未实际启动被测系统 (SUT)、未控制物理设备驱动，亦未精确测量故障检测与切换时延。现有受控源码尚未打开 DPU、网卡或 NPU 设备句柄，未执行真实的 AES-GCM 加密、CRC 校验、硬件 DMA 传输、CQE 完成事件监听、看门狗心跳或生产级故障回退。因此，现有受控源码仅用于检验字段结构与故障流转流程预演，不可直接作为证明吞吐性能、Host CPU 卸载收益、数据完整性、500µs 极速切换或请求连续性保障的依据。在缺乏物理设备 I/O、真实故障时间线及数据一致性校验的前提下，测试结论统一限定标记为 `DEMO` 或 `LAB`；若业务安全策略强制要求正文加密，而 Raw Direct 缺乏等价密码学硬件支持，该路径必须规范标记为 `NOT-SUPPORTED` 或显式安全拒绝。

> **术语速查**：
> - **KVCache**：大模型注意力键值缓存（大模型自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免后续 Token 生成时重复计算注意力）；
> - **DPU**：Data Processing Unit（数据处理单元：在网络与存储数据面执行极速传输、数据校验或硬件加密等任务的专用协处理器）；
> - **Raw Direct**：原始直达路径（不依赖 DPU 协处理器与专用硬件压缩，仅依靠底层网卡/NPU DMA 直达的最小基础数据通路）；
> - **NPU**：Neural Processing Unit（神经网络处理器 / AI 加速芯片）；
> - **DMA**：Direct Memory Access（直接内存访问）；
> - **HBM**：High Bandwidth Memory（高带宽显存）；
> - **AES-256-GCM**：带 Galois 认证标签的 256 位高级加密标准（同时提供正文机密性与防篡改完整性认证）；
> - **CRC64**：64 位循环冗余校验（用于检测传输中的随机比特翻转错误，但无法替代密码学抗篡改认证）；
> - **T10-DIF**：存储设备数据完整性字段规范（用于端到端数据块保护）；
> - **SM4**：商用密码分组加密算法；
> - **Fly-in-line**：在途流式处理（在数据网络传输或 DMA 搬运过程中，由 DPU/NPU 流式完成动态量化、压缩解压或校验的软硬件协同技术）；
> - **Host Payload Touch Bytes**：Host CPU 或 Host DDR 触碰正文数据的字节数（目标零拷贝路径严格要求为 0，且必须由探针或硬件计数器证明）；
> - **CQE**：Completion Queue Entry（硬件完成队列条目）；
> - **Watchdog**：硬件/软件看门狗（用于实时监测心跳或请求超时并触发自动隔离与回退的保护组件）；
> - **Circuit Breaker**：熔断器（在检测到特定通道连续故障或超时后，暂时阻断新请求提交的自愈保护机制）；
> - **硬件能力矩阵**：CapabilityMatrix（即在运行时自动探测各通信链路的带宽、时延、数据转换与安全加密等物理参数表，供调度算法使用）；
> - **P99**：99 分位值（数据集中 99% 样本均优于该阈值的统计指标）；
> - **TCO**：Total Cost of Ownership（总体拥有成本：综合考量硬件采购、算力开销、能耗与运维的全生命周期综合成本）。

> **验证 ID**：PVT-09
> **验证名称**：DPU 硬件安全与算力卸载加速 vs Raw Direct 软硬双轨协同验证
> **验证优先级**：**🟡 P1 级（底座支撑项）**
> **对应验证阶段**：**E1（核心数据路径与硬件安全加速打通）**
> **证伪标记**：否（双轨加速效能与安全回退确认）
> **主关联 IR**：`IR-01-08`, `IR-01-09`
> **核心 SRS / SR23 锚点**：
> - SRS：`L4-MC-HIER-STORE-002`, `L4-MC-HIER-STORE-003`, `L4-NET-OFFLOAD-DPU-001`, `L4-FT-PathIntegrityPolicy-077`
> - SR23：`SR23-01-08-01`, `SR23-01-08-02`, `SR23-01-09-01`, `SR23-01-12-01`
> **配套源码**：[`./原型验证代码/PVT-09/`](./原型验证代码/PVT-09/)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`。正式测试结果必须绑定实际 DPU/网卡/NPU 硬件设备、驱动固件版本、加密算法库及配置哈希。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 协处理器卸载与安全回退的第一性原理

在分布式大模型推理架构中，KVCache 正文数据的网络传输与多级存储沉淀面临严苛的安全与合规约束。加密计算（如 AES-256-GCM / SM4）、消息认证码（MAC/Tag）生成以及数据完整性校验（CRC64/T10-DIF）会消耗巨大的算力开销。将此类安全与数据转换任务卸载至 DPU 专用协处理器，能够释放宝贵的主机 CPU 与 NPU 算力专注于高吞吐在线推理；然而，DPU 卸载同时引入了专有硬件固件、复杂驱动栈、专用硬件队列及硬件单点故障等多重外部强依赖。

反之，Raw Direct 原始直达路径依赖最基础的网卡与 NPU DMA 控制器，具备架构极简、零外部硬件依赖与极高鲁棒性的天然优势。因此，原厂软硬件协同架构确立了“**以 Raw Direct 纯软直达为主路径底座，以 DPU 硬件卸载为条件加速轨**”的软硬双轨协同体系。

在双轨协同架构中，故障回退绝非简单修改路由配置，而是一条严密的物理控制链：

```text
DPU 通道请求提交
        │
        ├─► 硬件看门狗 (Watchdog) 监听 CQE 完成中断与心跳
        │
(检测到 CQE 超时 / 心跳丢失)
        │
        ▼
触发熔断器 (Circuit Breaker) ──► 瞬间阻断新请求提交至故障队列
        │
        ▼
在途 (In-flight) 请求安全排空与取消 ──► 杜绝双写冲突与半写脏块
        │
        ▼
安全隔离故障 DPU 通道 ──► 依据安全策略平稳回退至 Raw Direct、本地重算或显式报错
        │
        ▼
执行端到端数据完整性校验 ──► 记录切换确认并放行安全消费
```

若在途请求已部分写入目标显存缓冲区，回退机制必须严格识别有效写入边界，严禁将未完成的半写脏块直接标记为 `READY`。同时，AES-GCM 的认证 Tag 校验与 Generation 代际版本必须深度参与判决，杜绝因硬件故障引发脏数据被错误消费。

### 0.2 Host CPU 零数据拷贝与数据面物理边界

系统严格执行微秒级控制面与零拷贝数据面的双流分离架构：

```text
控制面 (Host CPU) : 硬件能力探测 / 描述符构造与提交 / CQE 事件轮询 / 看门狗健康监测 / 故障熔断仲裁
数据面 (硬件 DMA) : 网卡 / DPU <====== PCIe P2P DMA 直达 ======> NPU HBM 显存
条件回退路径       : 仅在 DPU 故障且策略允许时 ──► 切换至 Raw Direct 原始直达或本地算子直接重算
```

Host CPU 负责管理会话密钥句柄、安全策略、通道状态及错误码，但绝对不将 KVCache 正文数据搬运至 Host DDR 执行 `memcpy`、软件 CRC 或软件加密。Host Payload Touch 探针必须完整覆盖正常提交、硬件完成、异常重试及故障回退的全生命周期。

### 0.3 当前受控源码能力矩阵审计

| 源码文件与路径 | 当前受控源码实际行为 | 现阶段尚不能声称的能力 |
|---|---|---|
| `原型验证代码/PVT-09/offload_fallback_bench.cc` | 仅解析 `--hardware-supported` 与 `--out` 参数；固定输出 Raw Direct、DPU（条件分支）及故障回退的演示数据行；全部输出标记为 `DEMO,DEMO_ONLY` | 未真正打开 DPU、网卡或 NPU 设备句柄；未执行真实的 AES/SM4 加密、CRC 校验、DMA 传输、CQE 轮询或看门狗故障自愈 |
| `offload_fallback_bench.cc` | 默认输出 Raw Direct 的 685 Gbps、0.75ms、0.5% CPU 等固定值；开启开关后输出 DPU 的 660 Gbps、0.78ms、1.2% CPU 及 80/120µs 切换时延 | 输出数值均为静态预设参数，非真实物理吞吐或时延采样；`--hardware-supported` 仅为命令行布尔开关，不能作为硬件支持依据 |
| `原型验证代码/PVT-09/inject_fault.py` | 解析 `--target`、`--fault`、`--timeout-us`、`--out` 参数；等待约 50ms 后生成包含 `DPU_FAULT_INJECTED` 与期望确认事件的描述 JSON | 未启动真实的被测系统进程；未控制物理 DPU 驱动；未精确测量故障检测与切换时延；未验证 Raw Direct 实际回退数据 |
| `原型验证代码/PVT-09/Makefile` | 采用 `g++ -O3 -std=c++17 -pthread -Wall` 构建单机 C++ 测试桩 | 未链接 DPU SDK、网卡 SDK、昇腾 CANN 运行时、OpenSSL/国密库或硬件监控驱动 |
| PVT-09 源码目录 | 当前尚未受控提供 `eval_dpu_fallback.py` | 早期文档中提及的汇总评估脚本在当前仓库中尚未受控提供 |

### 0.4 当前输出格式与命令行参数边界

当前受控 C++ 基准程序输出 CSV 包含以下字段：

```csv
mode,payload_mb,bandwidth_gbps,latency_ms,host_cpu_pct,fault_detect_us,fallback_switch_us,request_success_rate,packet_loss_count,integrity_ok,actual_path,evidence_level,status
```

特别说明：当前 C++ 程序未实现 `--mode`、`--payload-mb`、`--fault` 或 `--sut-cmd` 等参数。Python 故障脚本亦未提供 `--sut-cmd`。在工程报告中，C++ 输出的 `NOT_SUPPORTED` 统一规范映射为标准状态 `NOT-SUPPORTED`，`DEMO_ONLY` 与 `INJECTED` 属于内部状态，严禁直接映射为 `GO`。

---

## 1. 验证目标、交付物与候选准入门槛

### 1.1 核心验证目标

| 验证核心维度 | 必须回答的物理与工程问题 | 必须具备的最低客观证据 |
|---|---|---|
| DPU 硬件能力实测 | DPU 硬件是否真正支持目标 AES-GCM/SM4 加密、Tag 认证、CRC64 校验或格式转换 | 运行时硬件能力矩阵、固件驱动版本、最小真实 I/O 及数据校验记录 |
| Raw Direct 独立性 | 在无 DPU 协处理器的纯软环境下，系统能否独立稳定完成端到端数据传输 | 真实的 NPU HBM 显存目标地址、DMA/CQE 完成事件、数据校验及零拷贝证明 |
| 算力卸载真实收益 | 在相同安全策略与负载下，DPU 卸载能否显著降低 Host CPU 占用并提升有效吞吐 | 逐 I/O 采样样本、CPU/设备性能计数器、严格同安全策略下的 A/B 对照 |
| 密码学与数据正确性 | 加密、MAC Tag 认证、CRC64 校验及 T10-DIF 是否按安全策略严格执行无误 | 明文/密文比对、Nonce/Tag 认证凭证、篡改样本精准拒绝及一致性校验 |
| 毫秒级故障安全回退 | DPU 发生超时、崩溃或失联后，系统能否在极短时间内安全隔离并回退至替代路径 | 故障触发、看门狗检测、熔断隔离、路径切换及最终完成的全链路时间线 |
| 在途流式协同处理 | 动态量化、压缩解压及校验能否在数据搬运流中高效完成且不引入精度损失 | 转换标志位、输入输出张量比对、可逆性验证及硬件完成中断 |

### 1.2 候选工程准入门槛

| 评估维度 | 候选工程准入门槛 | 权威取证要求 |
|---|---:|---|
| DPU 有效传输吞吐 | 稳态 DPU 加密传输带宽达到物理链路标称可用峰值的 $\ge 80\%$ | 严禁使用静态预设常数替代物理实测 |
| Host CPU 占用率 | DPU 正常卸载路径下 Host CPU 占用率 $< 5\%$ | 明确采样时间窗口与全核 CPU 统计口径 |
| Raw Direct 独立基准 | 无 DPU 环境下 Raw Direct 独立完成传输，吞吐达标 | 与同安全策略下的测试基线公平比对 |
| 故障检测与切换时延 | 从 DPU 故障触发到 Raw Direct 切换确认完成时延 $< 500\mu	ext{s}$ | 单调高精度硬件时钟记录，逐事件对账 |
| 密码学与数据正确性 | 全量测试请求 **0 错误消费、0 脏数据放行、0 密文解密损坏** | 逐请求 Tag/CRC 校验与独立 Oracle 对账 |
| Host CPU 正文拷贝 | 正常直达路径下 Host Payload Touch 严格 $= 0$ | 必须由动态探针或硬件性能计数器全程证明 |

### 1.3 阶段交付资产

每个正式的 `run_id` 必须至少交付以下结构化资产：

1. **《DPU 卸载 vs Raw Direct 同策略性能 A/B 对照报告》**：涵盖两种路径在各 Payload 及并发下的吞吐、时延与 CPU 开销；
2. **《硬件能力矩阵与底层设备拓扑配置快照》**：完整记录 DPU/NIC/NPU 固件版本、PCIe 拓扑及密码学算法支持清单；
3. **《数据完整性与密码学安全认证审计日志》**：包含 AES-GCM Tag 校验、CRC64/T10-DIF 检验及篡改注水拒绝记录；
4. **《DPU 故障检测、熔断与回退全流程时间线表》**：逐微秒记录故障触发、看门狗告警、熔断隔离、切换确认及请求完成时间戳；
5. **标准证据包与判定报告**：包含 `manifest.json`、原始事件日志及 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE` 最终判定。

---

## 2. 目标通道模型、能力矩阵与回退状态机

### 2.1 动态通道路由决策模型

系统在提交数据传输任务前，按以下物理决策漏斗选择最优传输通道：

```text
业务安全合规策略校验 (是否强制要求 AES-256-GCM / SM4 加密)
        │
        ├─► 强制加密 ──► 检查 DPU 硬件能力矩阵 (算法/固件/DMA 支持)
        │                 ├─► DPU 健康就绪 ──► 选择 [DPU_HARDWARE_OFFLOAD]
        │                 └─► DPU 故障/缺失 ──► 若可信网络允许降级 ──► [RAW_DIRECT]
        │                                      若安全绝对强制 ──► [FAILED 显式拒绝]
        │
        └─► 可信内网 ──► 评估物理链路成本与时延
                          ├─► 默认主路径 ────► 选择 [RAW_DIRECT] (极简零依赖)
                          └─► 在途转换需求 ──► 选择 [DPU_HARDWARE_OFFLOAD] 或 [NPU_FUSED]
```

### 2.2 目标通道状态与安全上下文结构

```cpp
enum class ChannelMode : uint8_t {
    RAW_DIRECT = 0,             // 基础 Raw Direct 原始直达路径
    DPU_HARDWARE_OFFLOAD = 1,   // DPU 硬件加速与在途安全卸载路径
    LOCAL_RECOMPUTE = 2,        // 本地算子直接重算路径 (安全兜底)
    FAILED = 3                  // 显式安全拒绝状态
};

struct alignas(64) ChannelState {
    uint64_t    channel_epoch;          // 通道配置代际版本号
    ChannelMode mode;                   // 当前激活的工作模式
    bool        healthy;                // 通道健康状态标志
    uint64_t    last_heartbeat_ns;      // 最后一次心跳时间戳
    uint64_t    timeout_count;          // 连续超时计数器
    uint64_t    in_flight_requests;     // 在途并发请求计数
};

struct alignas(64) TransferSecurityContext {
    uint64_t request_id;                // 推理请求全局唯一 ID
    uint64_t object_id;                 // KVCache 对象全局唯一 ID
    uint64_t generation;                // 显存代际版本号
    uint32_t transform_flags;           // 在途流式处理标志位
    uint32_t payload_bytes;             // 正文物理字节大小
    uint64_t nonce_id;                  // AES-GCM 唯一 Nonce 序号
    uint64_t authentication_tag;        // 128-bit 认证标签哈希引用
    uint64_t checksum;                  // xxHash64 / CRC64 校验码
};
```

### 2.3 硬件看门狗与熔断器状态机流转

```text
[HEALTHY 健康态] ──(提交请求)──► [SUBMITTED] ──(硬件 CQE 正常返回)──► [HEALTHY 健康态]
     │                                │
     │                                ├─► (CQE 超时未返回 / 心跳丢失)
     ▼                                ▼
[SUSPECTED 疑似故障] ───────────────► [CIRCUIT_OPEN 熔断器打开，阻断新请求]
                                      │
                                      ▼
                             [DRAIN_INFLIGHT 在途请求安全排空与取消]
                                      │
                                      ▼
                             [EXECUTE_FALLBACK 执行合规回退: Raw Direct / 本地重算]
                                      │
                                      ▼
                             [RECOVERY_PROBE 探针探测恢复]
                                      │
             ┌────────────────────────┴────────────────────────┐
             ▼                                                 ▼
[恢复成功 ──► HEALTHY]                                [探测失败 ──► 保持 CIRCUIT_OPEN]
```

各状态迁移必须在日志中逐次记录 `channel_epoch`、请求 ID、错误码、时间戳及回退动作。DPU 超时后必须严格排空在途请求，严禁同一请求在 DPU 与 Raw Direct 两条路径并发双写导致数据错乱。

---

## 3. 实验方案与测试矩阵设计

### 3.1 五大核心测试条件设计

| 实验条件标识 | DPU 协处理器状态 | Raw Direct 状态 | 密码学安全与校验策略 | 核心对账定位与验证目的 |
|---|---|---|---|---|
| **条件 A** | 完全关闭 / 无设备 | **完全开启** | 基础无加密 / CRC64 校验 | **Raw Direct 基础性能与零依赖基线** |
| **条件 B** | **完全开启** | 关闭 | AES-256-GCM / SM4 + Tag 认证 | **DPU 硬件加密卸载性能基线** |
| **条件 C** | **完全开启** | 关闭 | CRC64 / T10-DIF 传输完整性 | **DPU 传输完整性卸载对照** |
| **条件 D** | **注入硬件超时故障** | **自动触发回退** | 严格维持合规安全策略 | **毫秒级故障熔断与安全回退实测** |
| **条件 E** | 完全关闭 / 无设备 | 尝试强制回退 | 强制 AES-256-GCM 加密 | **验证无硬件加密时的安全合规拦截** |

严禁将“DPU 加密路径”与“Raw Direct 无加密路径”直接横向比对并声称纯硬件加速比；所有比较必须严格绑定相同的安全合规策略。

### 3.2 负载与故障压力矩阵

| 测试维度 | 正式实施计划标准 | 工程说明与约束 |
|---|---|---|
| Payload 数据量 | 4MB、16MB、64MB、256MB | 覆盖不同上下文长度与 Batch 维度的张量大小 |
| 分片颗粒度 (Chunk) | 64KB、1MB、4MB | 严格遵循底层硬件 DMA 与网络 MTU 对齐约束 |
| 硬件队列深度 (QD) | 1、4、16、32 | 评估不同并发队列下的设备吞吐与排队时延 |
| 数据流向维度 | 网卡到 HBM、HBM 到网卡 | 全量覆盖换入与换出双向数据通路 |
| 故障注入类型 | CQE 硬件超时、心跳丢失、设备 PCIe 掉卡、数据篡改 | 验证全场景故障下的自愈机制与安全边界 |

---

## 4. 工具审计与最小实现增量

### 4.1 当前基准测试程序实际命令

当前受控 C++ 基准程序执行命令为：

```bash
cd ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-09
make clean && make -j2
./offload_fallback_bench --out offload_formula_demo.csv
./offload_fallback_bench --hardware-supported --out dpu_capability_flag_demo.csv
```

第一条命令输出 Raw Direct 固定演示行并标注 DPU 为 `NOT_SUPPORTED`；第二条命令输出 DPU 与回退的固定演示数据，状态明确标记为 `DEMO,DEMO_ONLY`。

### 4.2 当前 Python 故障注入脚本命令

当前受控 Python 故障脚本执行命令为：

```bash
python3 ./inject_fault.py   --target dpu --fault timeout --timeout-us 500   --out fault_event_demo.json
```

该脚本仅生成包含故障注入描述与期望确认事件的静态 JSON，不实际启动被测系统进程。

### 4.3 面向生产级实测的最小工程增量

在正式开展 DPU 卸载与回退实测前，必须补齐以下工程增量：

1. **硬件能力矩阵运行时探测**：编写自动读取 DPU 固件特性、密码学硬件支持及 DMA 显存映射能力的探针工具；
2. **DPU 驱动与硬件队列对接**：实现向真实 DPU 提交加密/校验描述符并监听底层 CQE 完成中断；
3. **Raw Direct 真实显存 DMA**：打通网卡控制器直接向 NPU 物理显存地址提交 DMA 读写并采集中断的链路；
4. **独立看门狗与熔断器实现**：开发支持微秒级心跳检测、超时熔断及在途请求安全取消的控制模块；
5. **Host CPU 零拷贝探针接入**：挂载 eBPF 动态探针，全程采集正常传输与故障回退过程中的 Host Payload Touch 字节；
6. **全链路密码学与数据校验**：集成端到端 AES-GCM Tag 认证、CRC64 检验及篡改样本恶意注入测试。

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结拓扑配置、安全策略与代码版本

- **操作意图**：确保所有对比测试均可精准追溯至唯一的源码版本、硬件拓扑及安全合规策略。
- **执行命令**：

```bash
run_id="PVT-09-$(date +%Y%m%d-%H%M%S)-offload"
result_dir="results/pvt09/${run_id}"
mkdir -p "${result_dir}"
git rev-parse HEAD > "${result_dir}/git_commit.txt"
date --iso-8601=ns > "${result_dir}/timestamp.txt"
git status --short > "${result_dir}/git_status.txt"
```

- **应观察现象**：结果目录创建成功，Commit 哈希、系统时间戳及环境快照完整落盘。
- **判定边界**：若安全策略或 DPU 硬件型号未锁定，测试结论直接判定为 `INVALID-EVIDENCE`。

### 步骤 1：运行当前 C++ DEMO 校验格式与流程

- **操作意图**：验证输出 CSV 的 Schema 结构与参数解析逻辑，同时明确记录当前程序尚未产生真实硬件 I/O 的事实。
- **执行命令**：

```bash
cd ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-09
make clean && make -j2
./offload_fallback_bench --out "../../../../results/pvt09/${run_id}/formula_demo.csv"
./offload_fallback_bench --hardware-supported --out "../../../../results/pvt09/${run_id}/hardware_flag_demo.csv"
```

- **应观察现象**：输出 CSV 包含演示数据行，明确标记 `DEMO,DEMO_ONLY`；硬件未支持时状态标注为 `NOT_SUPPORTED`。
- **判定边界**：本步骤仅证实基准程序的输出格式合法，不可据此断言吞吐性能或回退耗时。

### 步骤 2：运行当前故障注入脚本确认事件格式

- **操作意图**：验证故障事件的 JSON 结构，同时确认该脚本不包含真实的 SUT 进程控制。
- **执行命令**：

```bash
python3 ./inject_fault.py   --target dpu --fault timeout --timeout-us 500   --out "../../../../results/pvt09/${run_id}/fault_event_demo.json"
```

- **应观察现象**：导出的 JSON 中记录了故障注入参数与期望确认事件名称。
- **判定边界**：`expected_confirmation_event` 仅为预期事件名称，不可直接当作 500µs 回退已达标的实测依据。

### 步骤 3：执行现场硬件能力探测与安全策略核验（条件步骤）

- **操作意图**：客观探测 DPU、网卡及 NPU 的硬件能力边界，核对密码学算法支持状态。
- **执行动作**：导出 PCIe 拓扑与设备固件版本；探测 DPU 支持的 AES-GCM/SM4/CRC64 算法及 HBM 地址映射能力；生成带哈希指纹的 `hardware_capability_matrix.json`。
- **应观察现象**：每项硬件能力均具备明确的固件来源与探测结果。
- **判定边界**：若现场缺乏 DPU 硬件，相关测试项规范标记为 `NOT-SUPPORTED`。

### 步骤 4：运行 Raw Direct 原始直达基线实测（条件步骤）

- **操作意图**：证实无 DPU 协处理器时真实数据可安全直达 NPU 显存，并验证 Host CPU 零数据拷贝。
- **执行动作**：向真实 NPU 显存物理地址提交 DMA 读写；采集硬件完成中断 CQE、实际完成字节数及数据一致性校验；挂载 eBPF 探针采集 Host Payload Touch。
- **应观察现象**：完成字节数与请求严格一致；数据校验 100% 正确；Host Payload Touch 严格为 0。
- **判定边界**：若缺乏显存目标物理地址凭证或探针数据为空，该测试点判定为 `INVALID-EVIDENCE`。

### 步骤 5：运行 DPU 硬件安全卸载基线实测（条件步骤）

- **前置条件**：DPU 硬件驱动已就绪且能力探测确认支持。
- **操作意图**：在合规安全策略下，评估 DPU 加密与完整性卸载的吞吐性能及 Host CPU 释放效果。
- **执行动作**：提交真实 DPU 加密描述符；采集 DPU 硬件吞吐、CQE 完成中断、AES-GCM Tag 校验结果及 Host CPU 占用率。
- **应观察现象**：数据加密传输平稳完成；Tag 认证 100% 通过；Host CPU 占用率稳定保持在低位。
- **判定边界**：若缺乏 MAC Tag 认证失败拦截测试，不可断言密码学安全成立。

### 步骤 6：全场景 DPU 故障注入与毫秒级回退实测（条件步骤）

- **操作意图**：确证 DPU 发生硬件超时或崩溃时，系统能在微秒级时间内平稳熔断并安全回退至 Raw Direct。
- **执行动作**：主动注入 DPU CQE 超时或硬件心跳丢失；采集看门狗告警、熔断隔离、在途请求排空、Raw Direct 提交确认及最终请求完成的时间戳；验证数据完整性与代际版本。
- **应观察现象**：新请求瞬间停止进入故障队列；系统平稳回退至 Raw Direct；全流程时延精准收敛在 500µs 安全包络内；数据 0 损坏。
- **判定边界**：若回退后发生重复写入或脏数据消费，直接一票否决判定为 `NO-GO`。

### 步骤 7：验证在途流式处理功能（条件步骤）

- **操作意图**：严格区分数据传输与在途动态转换，验证动态量化、压缩解压及校验的正确性。
- **执行动作**：针对 FP16 到 INT4 动态量化验证精度损失指标；针对数据压缩验证无损可逆性；记录各转换步骤的实际执行设备与完成中断。
- **应观察现象**：数据转换与声明标志位严格相符；错误输入被精准拦截。
- **判定边界**：若仅在描述符中置位而无底层设备转换凭证，证据等级限定为 `DEMO`。

### 步骤 8：全链路数据统计、独立复核与证据归档

- **操作意图**：形成涵盖通道能力、密码学安全、零拷贝及故障自愈的完整证据链。
- **执行动作**：在 `results/pvt09/<run_id>/` 目录下完整归档 `hardware_capability_matrix.json`、`raw_direct_events.jsonl`、`dpu_events.jsonl`、`watchdog_events.jsonl`、`fallback_events.jsonl`、`pvt09_summary.csv`、`errors.log` 及 `summary.md`。

---

## 6. 数据采集清单、核心公式与记录格式

### 6.1 通道测试标准汇总记录字段

```csv
run_id,channel_mode,payload_bytes,security_policy,transform_flags,bandwidth_gbps,latency_p50_ms,latency_p99_ms,host_cpu_pct,host_payload_touch_bytes,fault,fault_detect_us,circuit_open_us,fallback_switch_us,request_success_rate,packet_loss_count,integrity_ok,planned_path,actual_path,evidence_level,status,invalid_reason
```

约束说明：`host_payload_touch_bytes` 必须基于 eBPF 探针或硬件性能计数器填报；空值代表未接入探针，严禁直接填为 0。

### 6.2 故障流转原始事件字段

```csv
run_id,request_id,channel_epoch,event,channel_mode,object_id,generation,ts_ns,error_code,inflight_bytes,security_context_id,actual_path,action,status
```

### 6.3 核心故障时延计算公式

$$
	ext{故障检测时延 (Fault Detect Latency)} = T_{	ext{detect}}(	ext{Watchdog}) - T_{	ext{trigger}}(	ext{Fault})
$$

$$
	ext{通道熔断隔离时延 (Circuit Open Latency)} = T_{	ext{open}}(	ext{CircuitBreaker}) - T_{	ext{detect}}(	ext{Watchdog})
$$

$$
	ext{回退切换确认时延 (Fallback Switch Latency)} = T_{	ext{confirm}}(	ext{RawDirect}) - T_{	ext{open}}(	ext{CircuitBreaker})
$$

$$
	ext{端到端故障自愈时延 (E2E Recovery Latency)} = T_{	ext{done}}(	ext{Request}) - T_{	ext{trigger}}(	ext{Fault})
$$

---

## 7. 候选准入门槛、判定规则与立即止损机制

### 7.1 候选工程准入门槛一览表

| 核心评估指标 | 候选工程准入门槛 | 权威取证要求 |
|---|---:|---|
| DPU 硬件加密吞吐 | 稳态加密传输带宽达到物理链路峰值的 $\ge 80\%$ | 真实硬件 CQE 完成中断与实际完成字节数对账 |
| Host CPU 算力释放 | DPU 正常卸载路径下 Host CPU 占用率 $< 5\%$ | 明确采样时间窗口与全核 CPU 统计口径 |
| Raw Direct 独立基准 | 纯软 Raw Direct 独立完成传输，吞吐达标 | 真实 HBM 显存物理地址映射与 DMA 完成中断 |
| 故障检测与切换时延 | 从故障触发到 Raw Direct 切换确认时延 $< 500\mu	ext{s}$ | 单调高精度硬件时钟记录，逐事件对账 |
| 密码学与数据正确性 | 全量测试请求 **0 错误消费、0 脏数据放行** | AES-GCM Tag 认证、CRC64 校验与 Oracle 对账 |
| Host CPU 正文拷贝 | 正常直达路径下 Host Payload Touch 严格 $= 0$ | 必须由动态探针或硬件性能计数器全程证明 |

### 7.2 状态判定枚举与规则

- **GO（全链路证据形成完整闭环）**：通道能力、数据正确性、零拷贝验证、吞吐性能及故障回退均具备完整 `MEASURED` 证据链；
- **CONDITIONAL（局部场景或特定硬件达标）**：核心门限在特定硬件配置或特定安全策略下达标，但特定异常下存在波动；结论严格限定于已测条件；
- **NO-GO（发生数据损坏或回退失败）**：发生密文解密损坏、DPU 故障未能熔断隔离、回退路径违反安全策略或出现不可控超时；
- **NOT-SUPPORTED（物理环境未支持）**：现场缺乏 DPU 协处理器、Raw Direct 不具备硬件 DMA 能力或驱动不支持目标密码学算法；
- **INVALID-EVIDENCE（无效证据）**：使用静态演示数据、故障脚本未实际控制 SUT、关键时间线缺失或空字段被当作 0。

### 7.3 立即安全止损条件

在测试过程中凡触发以下任一异常，必须立即终止测试并保存现场：

- AES-GCM 认证 Tag 校验失败、CRC64 错误或 Generation 代际不一致的数据被错误标记为 `READY`；
- DPU 发生超时告警后，熔断器未能生效，仍有新请求持续进入故障队列；
- 同一请求在 DPU 与 Raw Direct 两条路径发生并发双写冲突；
- 声明直达的正常路径测得 Host Payload Touch 大于 0 且缺乏合理的物理归因；
- Raw Direct 回退绕过了业务强制要求的密码学机密性与完整性安全策略；
- 硬件看门狗或驱动发生死锁导致系统无法平稳恢复。

---

## 8. 执行阶段划分与交付闭环

测试实施划分为四个严密的演进阶段：

| 实施阶段 | 核心攻坚内容 | 阶段必须交付物 | 准出判定条件 |
|---|---|---|---|
| E0（契约与策略规范确认） | 严密固化安全策略、能力矩阵 Schema、通道枚举、回退逻辑及证据等级规范 | 源码审计报告、DEMO 输出 CSV/JSON、规范化 Manifest | 确认当前代码边界，杜绝将测试桩冒充实测 |
| E1（Raw Direct 基础数据面） | 打通纯软 Raw Direct 单请求数据通路，验证真实 HBM DMA 目标与零数据拷贝 | Raw Direct 基线表、DMA 完成日志、eBPF 探针报告 | 证实 Raw Direct 路径成立且 Host Payload Touch=0 |
| E2（DPU 安全卸载与在途处理） | 接入真实 DPU 驱动，完成 AES-GCM/SM4 加密、Tag 认证、CRC64 校验及动态量化 | DPU 实测报告、密码学校验日志、CPU 卸载对比表 | 证实 DPU 吞吐达标且 Host CPU 占用率 $< 5\%$ |
| E3（故障回退与双轨混压总门禁） | 注入 CQE 超时与心跳丢失故障，全量验证看门狗检测、熔断隔离与 500µs 极速切换 | 故障时间线分析表、切换确认日志、最终判定结论 | 证实切换时延 $< 500\mu	ext{s}$ 且 0 错误消费 |
| 条件证伪（DPU 依赖与安全边界） | 分别关闭 DPU、关闭在途转换，严格证伪软硬双轨协同的必要性与技术边界 | 双轨依赖评估表、降级安全合规报告 | 确立纯软主路径，若硬件不支持则规范标记未支持 |

---

## 9. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师核心职责**：
  1. 确认现场部署的 DPU/网卡/NPU 硬件设备、PCIe 拓扑、驱动固件及特权系统权限；
  2. 固化安全合规策略、密码学算法、超时门限、测试数据量及安全止损红线；
  3. 实际启动/停止被测进程、故障注入工具及内核追踪器，完整留存系统 dmesg 日志与驱动告警；
  4. 严格审定 `host_payload_touch_bytes=0` 是否确由底层 DMA 硬件直接搬运且探针全程覆盖；
  5. 对软硬双轨协同机制是否达到生产准入标准承担最终技术把关责任。
- **AI Agent 协同职责**：
  1. 深入研读本方案设计、公共测试契约及原型源码，精准梳理实际支持的 CLI 参数、依赖库及当前未实现特性；
  2. 编写硬件能力矩阵自动探测脚本、逐请求 CQE 事件解析器、故障时间线审计及标准证据包生成工具；
  3. 严格核验各通道的实际执行状态、`planned_path` 与 `actual_path` 路径一致性及状态枚举归一化；
  4. 严守学术与技术诚信红线，严禁虚构 DPU 吞吐、伪造切换时延或将静态演示数据篡改为生产级实测。

### 9.2 可直接复制给 Coding Agent 的 Prompt 模板

```text
我正在执行 PVT-09：DPU 硬件安全处理、Raw Direct 主路径与故障回退验证。

请先研读以下核心文件：
1. ./提前验证方案设计/验证计划方案设计/10_PVT-09_DPU硬件安全与算力卸载加速与双轨协同验证实施方案设计.md
2. ./提前验证方案设计/验证计划方案设计/Benchmark公共契约与证据分级规范.md
3. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-09/Makefile
4. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-09/offload_fallback_bench.cc
5. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-09/inject_fault.py

执行约束与任务要求：
- 首先梳理源码实际支持的 CLI 参数与底层执行行为；确认 offload_fallback_bench.cc 当前仅输出静态演示 DEMO，inject_fault.py 仅生成静态故障事件且不控制 SUT，当前目录中不存在 eval_dpu_fallback.py。
- 正式实验必须基于统一的硬件能力矩阵与安全合规策略，对比 Raw Direct 原始直达、DPU 硬件安全卸载与故障回退。
- 逐请求采集 CQE 完成中断、AES-GCM Tag 认证结果、CRC64 校验码、Generation 代际、Host CPU 占用率及全生命周期的 Host Payload Touch 探针数据。
- 故障注入测试必须精确记录 fault_trigger、watchdog_detect、circuit_open、fallback_confirm 及 request_done 的微秒级时间戳。
- 区分 planned_path 与 actual_path；未采集到的字段显式置为 null 并详细注明 invalid_reason；将脚本内部状态规范归一化为公共契约枚举。
- 最终输出：源码能力核验矩阵、实际执行命令清单、双轨通道性能对照表、故障回退时间线审计表、未支持特性清单以及下一步最小代码重构建议。
```

### 9.3 常见排错指南

- **输出 CSV 中吞吐与时延显示为固定的 685/660 样例数值**：当前受控 C++ 代码仅为演示 DEMO；必须接入真实的底层驱动与硬件性能计数器，未接入前测试结果严格保持为 `DEMO`。
- **命令行传入 `--mode` 或 `--payload-mb` 报错未知参数**：当前受控基准程序未集成此类 CLI 参数；应按照实际支持的 CLI 参数执行，或在重构增量中补齐接口。
- **运行故障注入脚本传入 `--sut-cmd` 报错未知参数**：当前脚本不包含被测进程编排逻辑；需由外部测试脚手架统一编排并按请求 ID 关联事件日志。
- **故障 JSON 显示有 `expected_confirmation_event` 字段**：该字段仅为预期事件名称；必须等待并严格校验真实的 `fallback_confirm` 物理事件。
- **传入 `--hardware-supported` 后仍无任何物理设备事件**：该参数仅为代码内部的演示分支开关；必须执行真实的硬件能力探测与驱动调用。
- **Raw Direct 传输成功但业务安全策略报警拦截**：说明当前网络环境强制要求密码学加密而 Raw Direct 未配备硬件加密引擎；应规范判定为 `NOT-SUPPORTED` 或显式安全拒绝。
- **触发故障回退后数据 Checksum 校验失败**：排查在途请求排空逻辑与显存双写冲突；必须丢弃受损目标块，执行严格的代际隔离与幂等重试。
- **导出的 `host_payload_touch_bytes` 字段为空**：说明未接入完整的动态探针；空值代表未取证，绝不能直接写为 0。
- **注入异常后发生请求重复提交双写**：排查通道 Epoch 与请求 ID 幂等控制逻辑；必须在硬件提交端增加原子版本校验。
