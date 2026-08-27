# PVT-09：DPU 硬件安全处理、Raw Direct 主路径与故障回退验证实施方案设计
## —— DPU 硬件安全处理与 Raw Direct 主路径验证：在途处理、双轨协同与故障回退

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。每个 `run_id` 必须冻结 DPU/网卡/NPU、驱动固件、PCIe/NUMA 拓扑、通道、payload、加密与完整性策略、队列深度、并发、故障注入、代码包、配置哈希和证据等级；结果必须保留提交/完成/CQE、超时、隔离、切换确认、请求结果、带宽/时延、Host CPU、Host Payload Touch、丢包、校验和错误码。能力不具备时标记 `NOT-SUPPORTED`，不能用模拟数据替代。

> **验证范围声明**：当前受控工程中的 `offload_fallback_bench.cc` 只解析 `--hardware-supported` 和 `--out`，写入固定 Raw Direct、DPU 和故障回退 schema；`inject_fault.py` 只等待固定时间并生成独立故障事件，不启动 SUT、不控制设备、不测量检测/切换时间。现有代码未打开 DPU、网卡或 NPU，也未执行加密、CRC、DMA、CQE、看门狗或真实回退。因此当前源码只能验证字段和故障流程预演，不能单独证明吞吐、CPU 占用、完整性、500µs 切换或请求连续性；缺少真实设备事件、故障时间线和数据校验的结果只能标记为 `DEMO`/`LAB`。若强制安全策略要求正文加密，而 Raw Direct 没有等价保护，只能标记为 `NOT-SUPPORTED` 或显式失败。

> **术语速查**：KVCache（大模型注意力键值缓存，即自回归生成过程中保存历史 Key 和 Value 激活状态、避免后续 Token 重复计算注意力）；DPU（Data Processing Unit，数据处理单元，即在网络/存储数据面执行传输、校验或加密等工作的专用处理器）；Raw Direct（原始直达路径，即不依赖 DPU 协处理器和专用硬件压缩、依靠网卡/NPU DMA 形成的最小数据通路）；NPU（Neural Processing Unit，神经网络处理器）；DMA（Direct Memory Access，直接内存访问）；HBM（High Bandwidth Memory，高带宽显存）；AES-256-GCM（带认证标签的 256 位加密模式，同时提供机密性和篡改检测）；CRC64（64 位循环冗余校验，用于发现随机错误但不等价于密码学认证）；T10-DIF（存储设备数据完整性字段规范）；SM4（商用密码分组算法）；Fly-in-line（在途流式处理，即数据传输过程中由 DPU/NPU 执行量化、压缩、解压或校验）；Host Payload Touch Bytes（Host CPU 或 Host DDR 触碰正文数据的字节数，目标零拷贝路径要求为 0 但必须由探针/设备计数证明）；CQE（Completion Queue Entry，完成队列条目）；Watchdog（看门狗，用于监测心跳或请求超时并触发隔离/回退）；Circuit Breaker（熔断器，在通道连续失败或超时后暂时禁止新请求）；硬件能力矩阵（运行时探测网卡、DPU、NPU 和链路的带宽、时延、转换与安全能力）；P99（延迟分布中 99% 样本不超过的分位值）；TCO（Total Cost of Ownership，总体拥有成本，本文只讨论 CPU、设备依赖和故障维护带来的工程影响）。

> **验证 ID**：PVT-09
> **验证名称**：DPU 硬件安全与算力卸载加速 vs Raw Direct 软硬双轨协同验证
> **验证优先级**：**🟡 P1 级（底座支撑项）**
> **对应验证阶段**：**E1（核心数据路径与硬件安全加速打通）**
> **证伪标记**：否（双轨加速效能与安全回退确认）
> **建议周期**：4~5 人日
> **主关联 IR**：`IR-01-08`, `IR-01-09`
> **核心 SRS / SR23 锚点**：
> - SRS：`L4-MC-HIER-STORE-002`, `L4-MC-HIER-STORE-003`, `L4-NET-OFFLOAD-DPU-001`, `L4-FT-PathIntegrityPolicy-077`
> - SR23：`SR23-01-08-01`, `SR23-01-08-02`, `SR23-01-09-01`, `SR23-01-12-01`
> **配套源码**：[`提前验证方案设计/验证计划方案设计/原型验证代码/PVT-09/`](file:///d:/codes/reports/kvcache/unified_kv_memory/提前验证方案设计/验证计划方案设计/原型验证代码/PVT-09)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`。正式结果必须绑定实际 DPU/网卡/NPU、驱动固件、加密库和配置哈希。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 协处理器卸载与安全回退的第一性原理

加密、认证和完整性计算会增加数据面处理工作。DPU 卸载可能降低 Host CPU 参与，但会引入固件、驱动、队列和设备健康状态等依赖。Raw Direct 减少外部专用硬件依赖，但如果没有等价的加密和认证能力，就不能自动满足所有业务安全策略。

故障回退也不是“换一个枚举值”这么简单。完整回退链至少包括：

```text
DPU 请求提交
  -> 心跳/CQE/超时监测
  -> 停止新请求进入故障队列
  -> 处理在途请求和重复提交边界
  -> 隔离 DPU 通道
  -> 选择 Raw Direct、重新计算或显式失败
  -> 校验数据完整性和请求幂等性
  -> 记录切换确认和最终路径
```

若在途请求已经部分写入目标缓冲区，回退路径必须知道哪些字节有效，不能直接把目标块标记为 READY。AES-GCM 的认证标签、CRC64/T10-DIF 和 generation/请求 ID 应共同参与完成判定；CRC 发现错误但不提供密码学认证，不能单独作为安全防篡改证明。

### 0.2 Host CPU 与正文数据的边界

目标零拷贝数据面应明确区分：

```text
控制面：Host CPU -> 能力探测 / 描述符提交 / CQE 轮询 / 看门狗 / 遥测
数据面：网卡/DPU <==== DMA ====> NPU HBM
条件回退：故障或策略不满足 -> 安全重试 / 本地重算 / 显式失败
```

Host CPU 可以处理描述符、密钥句柄、策略、状态和错误码，但不应把 KV 正文读入 Host DDR 后再执行 `memcpy`、CRC 或加密。Host Payload Touch 必须覆盖正常提交、完成、重试和回退全过程；只探测某一个函数调用不能证明正文零触碰。

### 0.3 当前受控源码能力矩阵

| 文件 | 当前可确认行为 | 当前不能声称的能力 |
|---|---|---|
| `原型验证代码/PVT-09/offload_fallback_bench.cc` | 只解析 `--hardware-supported` 和 `--out`；固定写入 Raw Direct、DPU（条件开启）和 DPU 故障回退行；所有输出 `evidence_level=DEMO` | 不打开 DPU/网卡/NPU；不执行加密、CRC、DMA、CQE、看门狗或故障回退 |
| `offload_fallback_bench.cc` | 默认输出 Raw Direct `685`、`0.75ms`、`0.5%` 等固定样例；开启布尔开关后写入 DPU `660`、`0.78ms`、`1.2%` 和回退 `80/120µs` 等固定值 | 这些不是吞吐、时延、CPU 或回退实测；`--hardware-supported` 不是能力探测 |
| `原型验证代码/PVT-09/inject_fault.py` | 解析 `--target`、`--fault`、`--timeout-us`、`--out`；等待固定 50ms，写入故障注入事件和“期望确认事件”字段 | 不启动 SUT、不控制 DPU、不测量检测/切换时间、不验证 Raw Direct 结果 |
| `原型验证代码/PVT-09/Makefile` | 使用 `g++ -O3 -std=c++17 -pthread -Wall` 构建一个 C++ DEMO | 没有 DPU SDK、网卡 SDK、NPU 运行时、加密库、CRC 库或设备监控依赖 |
| PVT-09 目录 | 当前没有 `eval_dpu_fallback.py` | 旧稿引用的汇总脚本不存在 |

### 0.4 当前输出和命令边界

当前 C++ 输出字段为：

```csv
mode,payload_mb,bandwidth_gbps,latency_ms,host_cpu_pct,fault_detect_us,fallback_switch_us,request_success_rate,packet_loss_count,integrity_ok,actual_path,evidence_level,status
```

当前程序没有 `--mode`、`--payload-mb`、`--fault`、`--timeout-us` 或 `--sut-cmd` 参数。未知参数不会驱动对应行为，旧稿按通道分别执行的命令不对应当前源码。故障注入脚本也没有 `--sut-cmd`，只能生成独立事件文件。

当前 C++ 内部状态使用 `NOT_SUPPORTED`，正式文档统一写 `NOT-SUPPORTED`；`DEMO_ONLY`、`INJECTED` 等内部状态也不能直接映射为 `GO`。

## 1. 验证目标与交付物

### 1.1 验证目标

| 目标 | 需回答的问题 | 最低证据 |
|---|---|---|
| DPU 能力 | DPU 是否真正支持目标加密、认证、CRC 或格式转换 | 运行时能力矩阵、设备版本、最小真实 I/O 和校验结果 |
| Raw Direct | 无 DPU 时是否能独立完成允许的数据路径 | 真实 NPU HBM 目标、DMA/CQE、数据校验和 Host Touch |
| 卸载收益 | DPU 路径在相同安全策略和负载下是否降低 Host CPU 或提高有效吞吐 | 逐 I/O 样本、CPU/设备计数、同场景 A/B |
| 安全正确性 | 加密/认证/CRC/T10-DIF 是否按策略正确执行 | 明文/密文、nonce/tag、校验失败和数据比对 |
| 故障回退 | DPU 超时或失联后能否隔离并进入批准的替代路径 | 触发、检测、隔离、切换、完成和请求结果时间线 |
| 在途处理 | 量化/压缩/解压/校验是否能在数据传输中完成且不引入错误 | 转换标志、输入输出格式、精度/可逆性和设备事件 |

### 1.2 候选门限

以下是进入评审前的候选门限，必须用真实现场数据验证：

| 维度 | 候选要求 | 备注 |
|---|---:|---|
| DPU 有效吞吐 | 在冻结 payload、并发和安全策略下达到现场链路/设备可用带宽的 80% 以上 | 不能用代码中的固定 `660` 代替 |
| Host CPU | DPU 正常路径 Host CPU 占用低于 5% 的候选门限 | 必须说明采样窗口和 CPU 统计范围 |
| Raw Direct | 无 DPU 环境下可独立完成，吞吐达到其能力矩阵基线的候选比例 | 不与未加密 DPU 路径直接混比 |
| 检测/切换 | 从真实故障触发到 Raw Direct 切换确认小于 500µs | 500µs 是门限，不是当前脚本的结果 |
| 正确性 | 加密认证、CRC/Tag、generation 和请求结果全部通过 | 任一错误数据消费为 `NO-GO` |
| Host Payload Touch | 目标直达路径正常阶段为 0 | 空值、未覆盖重试或只采集控制面不算证明 |

如果业务要求故障后继续传输，但 Raw Direct 不满足安全策略，应输出“安全策略不支持下的回退失败”，而不是为了保持请求成功率而绕过加密要求。

### 1.3 交付物

1. DPU 卸载、Raw Direct 和合规安全策略下的对照数据；
2. 硬件能力矩阵、设备拓扑、驱动/固件和配置快照；
3. 加密、认证、CRC/T10-DIF、DMA 目标和 Host Touch 原始证据；
4. DPU 超时、失联、CQE 错误和回退时间线；
5. 在途量化/压缩接口的能力表和正确性结果；
6. `GO/CONDITIONAL/NO-GO/NOT-SUPPORTED/INVALID-EVIDENCE` 判定。

## 2. 目标通道模型、能力矩阵与回退状态机

### 2.1 通道选择模型

当前仓库没有通道路由器。目标选择逻辑应至少检查：

```text
安全策略是否满足
  -> DPU 能力矩阵是否匹配
  -> DPU 心跳/队列是否健康
  -> NPU HBM 和网卡 DMA 目标是否可用
  -> 当前路径的成本与时延是否满足
  -> 选择 DPU Offload / Raw Direct / 本地重算 / 显式失败
```

禁止仅根据设备是否存在选择 DPU。设备存在但固件不支持目标算法、队列不支持 HBM 地址、认证标签无法验证或安全策略不允许时，都必须拒绝该通道。

### 2.2 目标通道状态结构

以下结构是目标接口示意，当前源码没有实现：

```cpp
enum class ChannelMode : uint8_t {
    RAW_DIRECT = 0,
    DPU_HARDWARE_OFFLOAD = 1,
    LOCAL_RECOMPUTE = 2,
    FAILED = 3,
};

struct ChannelState {
    uint64_t channel_epoch;
    ChannelMode mode;
    bool healthy;
    uint64_t last_heartbeat_ns;
    uint64_t timeout_count;
    uint64_t in_flight_requests;
};

struct TransferSecurityContext {
    uint64_t request_id;
    uint64_t object_id;
    uint64_t generation;
    uint32_t transform_flags;
    uint32_t payload_bytes;
    uint64_t nonce_or_nonce_id;
    uint64_t authentication_tag;
    uint64_t checksum;
};
```

真实实现还需明确密钥句柄、nonce 唯一性、认证标签长度、校验顺序、失败清理和请求幂等性。不能把密钥明文、认证标签或敏感数据写入普通结果日志。

### 2.3 看门狗和熔断状态机

```text
HEALTHY
  -> SUBMITTED
  -> COMPLETED
  -> HEALTHY

HEALTHY/SUBMITTED
  -> TIMEOUT_OR_CQE_ERROR
  -> SUSPECTED
  -> CIRCUIT_OPEN
  -> DRAIN_OR_CANCEL_INFLIGHT
  -> RAW_DIRECT / LOCAL_RECOMPUTE / FAILED
  -> RECOVERY_PROBE
  -> HEALTHY 或 CIRCUIT_OPEN
```

每个状态迁移必须记录 `channel_epoch`、请求 ID、对象代次、错误码、时间戳和最终动作。DPU 超时后不能让同一请求同时在 DPU 和 Raw Direct 两条路径写入同一目标块，除非使用明确的幂等写入和 generation 校验。

### 2.4 正常路径和故障路径

```text
正常：请求 -> 能力校验 -> DPU/Raw Direct 提交 -> CQE -> Tag/CRC/数据校验 -> READY

故障：请求 -> DPU 提交 -> 超时/CQE 错误
      -> 禁止新 DPU 请求 -> 标记在途状态
      -> Raw Direct（策略允许时）/本地重算/显式失败
      -> 完整性校验 -> READY 或 INVALID
```

如果回退路径不具备正文认证能力，则即使传输完成，也只能进入“传输完成但业务策略不允许消费”的状态，不能直接标记为 READY。

## 3. 实验矩阵与冻结参数

### 3.1 通道与安全策略矩阵

| 条件 | DPU | Raw Direct | 加密/认证 | 目的 |
|---|---|---|---|---|
| A | 关闭/无 | 开启 | 现场允许的最低策略 | Raw Direct 能力基线 |
| B | 开启 | 关闭 | AES-256-GCM/SM4 + Tag 或现场策略 | DPU 卸载基线 |
| C | 开启 | 关闭 | CRC64/T10-DIF 与传输校验 | 完整性处理对照 |
| D | DPU 正常后注入故障 | 允许的回退路径 | 不得降低强制安全策略 | 故障切换 |
| E | 无 DPU | Raw Direct | 强制加密策略 | 判断是否 `NOT-SUPPORTED` |

不能比较“DPU 加密路径”和“Raw Direct 不加密路径”来得出纯硬件收益；安全策略必须是可比条件，或把差异明确写入结论。

### 3.2 负载矩阵

| 维度 | 候选值 |
|---|---|
| Payload | 4MB、16MB、64MB、256MB |
| 分片大小 | 64KB、1MB、4MB；按 DMA 和协议要求对齐 |
| 并发 | 1、4、16、32 |
| Queue Depth | 1、4、16、32 |
| 访问方向 | 网卡到 HBM、HBM 到网卡；按实际业务双向测量 |
| 安全策略 | 无、CRC64、AES-256-GCM/SM4、GCM Tag + CRC（按现场合规要求） |
| 运行阶段 | 预热、稳态、故障注入、恢复探测 |
| 重复次数 | 每点至少 30 次；故障点另记录每次触发时间线 |

设备资源不足时可以减少点位，但必须记录删减原因、保留未测字段为 `null`，不能把未测通道复制成已测通道。

### 3.3 故障矩阵

| 故障 | 注入位置 | 预期动作 | 必须观测 |
|---|---|---|---|
| DPU CQE 超时 | 请求完成前 | 停止新请求、超时检测、按策略回退 | submit/detect/isolate/fallback/confirm |
| DPU 心跳丢失 | 控制通道 | 熔断 DPU、探测替代路径 | heartbeat、熔断时间、在途请求 |
| DPU 设备断开 | 设备/PCIe | 显式错误或安全回退 | 设备错误、请求结果、资源清理 |
| 完整性失败 | 接收/写入校验 | 丢弃目标块并重试或重算 | tag/CRC、generation、retry |
| Raw Direct 不支持 | 路由阶段 | 本地重算或 NOT-SUPPORTED | 策略原因和最终请求状态 |

## 4. 工具审计、实际命令与最小实现增量

### 4.1 当前构建入口

当前 Makefile 只构建 C++ DEMO：

```bash
cd ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-09
make clean && make -j2
```

它未链接 DPU SDK、网卡 SDK、NPU 运行时、加密库或设备监控接口。当前构建成功只说明固定结果程序可以编译。

### 4.2 当前 C++ DEMO 的真实命令

当前程序支持的命令为：

```bash
./offload_fallback_bench --out offload_formula_demo.csv
./offload_fallback_bench --hardware-supported --out dpu_capability_flag_demo.csv
```

第一条输出 Raw Direct 固定样例，并输出 DPU 行为的 `NOT_SUPPORTED`；第二条只把内部布尔值改为“有硬件支持”样式，仍然输出 `DEMO`，不执行任何 DPU 操作。程序一次写入多个模式，不能用 `--mode` 选择单独路径。

### 4.3 当前故障脚本的真实命令

当前 `inject_fault.py` 支持：

```bash
python3 ./inject_fault.py \
  --target dpu \
  --fault timeout \
  --timeout-us 500 \
  --out fault_event_demo.json
```

脚本会等待固定约 50ms，然后写出 `DPU_FAULT_INJECTED` 和 `expected_confirmation_event` 字段。它没有启动 SUT，也没有验证 `RAW_DIRECT_FALLBACK_CONFIRMED` 是否出现，因此输出只能标记 `DEMO`/`INJECTED`，不能作为 500µs 回退结果。旧稿中的 `--sut-cmd` 参数当前不支持。

### 4.4 当前缺失的评估入口

PVT-09 目录没有 `eval_dpu_fallback.py`。正式评估器需要读取：

- 真实 DPU/Raw Direct 逐请求完成事件；
- 看门狗、CQE、隔离和回退确认事件；
- 加密/认证/CRC/T10-DIF 结果和错误样本；
- Host CPU 与 Host Payload Touch 采样；
- 真实路径、能力矩阵、配置哈希和证据等级。

### 4.5 最小真实实现增量

进入 LAB/MEASURED 前至少需要：

1. 接入硬件能力矩阵和真实 DPU/NIC/NPU 设备探测；
2. 实现最小 DPU 提交、CQE 回收、认证/校验和数据比对；
3. 实现 Raw Direct 的真实 HBM DMA 目标和完成事件；
4. 实现独立看门狗、熔断、在途请求处理、通道 epoch 和回退路由；
5. 接入 Host Payload Touch 探针和 CPU/设备计数器；
6. 增加安全策略校验，禁止无认证路径绕过强制加密要求；
7. 接入真实故障注入，不仅生成期望事件；
8. 新增评估器、逐请求事件 schema 和重复运行统计。

## 5. 逐步执行 SOP

### Step 0：冻结版本、拓扑和安全策略

操作意图：确保所有通道比较都基于同一源码、设备和安全口径。

执行动作：

```bash
run_id="PVT-09-$(date +%Y%m%d-%H%M%S)-offload"
result_dir="results/pvt09/${run_id}"
mkdir -p "${result_dir}"
git rev-parse HEAD > "${result_dir}/git_commit.txt"
date --iso-8601=ns > "${result_dir}/timestamp.txt"
git status --short > "${result_dir}/git_status.txt"
```

同时保存 DPU/NIC/NPU、驱动、固件、PCIe/NUMA、加密策略、密钥句柄配置和能力矩阵。密钥原文不能写入结果目录。

应观察现象：版本、拓扑、策略和结果目录均可追溯。

判定边界：安全策略、DPU 型号或实际目标地址未冻结时，结果只能是 `INVALID-EVIDENCE`。

### Step 1：运行当前 C++ DEMO，确认其不构成设备证据

操作意图：验证当前程序的 CSV schema 和硬件标志边界，防止固定样例进入正式结论。

执行动作：

```bash
cd ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-09
make clean && make -j2
./offload_fallback_bench \
  --out "../../../../results/pvt09/${run_id}/formula_demo.csv" \
  > "../../../../results/pvt09/${run_id}/formula_demo.stdout.txt"
./offload_fallback_bench \
  --hardware-supported \
  --out "../../../../results/pvt09/${run_id}/hardware_flag_demo.csv" \
  > "../../../../results/pvt09/${run_id}/hardware_flag_demo.stdout.txt"
```

应观察现象：两次均输出 `evidence_level=DEMO`；第一条内部 DPU 状态为 `NOT_SUPPORTED`，第二条产生 DPU 和回退固定行，但仍是 DEMO。

判定边界：不得使用 `685`、`660`、`80/120µs`、`0.5%/1.2%` 或 `1.0` 成功率作为实测结果；`--hardware-supported` 不能替代设备探测。

### Step 2：运行当前故障注入 DEMO

操作意图：确认故障事件 schema，同时确认该脚本不验证 SUT 回退。

执行动作：

```bash
python3 ./inject_fault.py \
  --target dpu --fault timeout --timeout-us 500 \
  --out "../../../../results/pvt09/${run_id}/fault_event_demo.json"
```

应观察现象：脚本输出故障注入文本，JSON 中包含 `DPU_FAULT_INJECTED`、配置超时值和 `expected_confirmation_event`；脚本本身没有 SUT 进程、实际检测时间或回退确认。

判定边界：`expected_confirmation_event` 只是待观察事件名称，不是实际事件；该步骤不能判定 500µs、请求成功率或丢包为零。

### Step 3：完成真实能力探测与安全策略检查

操作意图：确定 DPU、网卡、NPU 和 DMA 目标是否支持待测路径，并判断 Raw Direct 是否满足安全策略。

执行动作：

1. 保存 PCIe/NUMA 拓扑、设备型号、固件、驱动和运行时；
2. 探测 DPU 支持的加密/认证/CRC/转换算法、最大消息、队列和 HBM 地址能力；
3. 探测网卡到 NPU HBM 的 DMA/IOMMU 路径；
4. 验证 nonce、Tag、密钥句柄和 CRC/T10-DIF 配置；
5. 生成带能力版本和配置哈希的 `hardware_capability_matrix.json`；
6. 对不支持的能力写 `supported=false` 和原因，不用命令行开关代替。

应观察现象：每条待测能力都有设备来源、版本、探测命令和结果；安全策略与通道候选能逐项对照。

判定边界：能力矩阵缺失、只依据设备型号推测或 Raw Direct 不满足强制安全策略时，该路径为 `NOT-SUPPORTED` 或 `INVALID-EVIDENCE`。

### Step 4：运行单请求 Raw Direct 基线

操作意图：先证明无 DPU 时真实数据可以进入 NPU HBM，并完成校验和 Host Touch 对账。

执行动作：

1. 使用小 payload 和单队列，分配可由网卡访问的 HBM 目标；
2. 写入已知 pattern 或受控合成 KV，记录 object ID/generation；
3. 提交真实 DMA，采集提交时间、CQE、完成字节和错误码；
4. 在接收端校验数据、Tag/CRC 和 generation；
5. 采集完整生命周期的 Host Payload Touch 和 CPU 采样；
6. 再增加 payload、并发和队列深度。

应观察现象：请求完成字节等于提交字节；数据校验通过；目标确实是 HBM；Host Payload Touch 结果有探针来源而不是空值。

判定边界：没有真实 HBM 目标、CQE 或正文探针时，Raw Direct 只能是 `LAB`/`DEMO`，不能判为零拷贝或性能通过。

### Step 5：运行 DPU 硬件卸载基线

操作意图：在能力矩阵已确认的安全策略下，比较 DPU 与 Raw Direct 的有效吞吐、CPU 和完整性。

执行动作：

1. 固定 payload、分片、并发、队列深度、加密/认证策略和请求顺序；
2. 提交真实 DPU 描述符，记录算法、密钥句柄 ID、nonce/Tag ID 和 CQE；
3. 对明文输入、密文输出、解密结果和认证失败样本分别校验；
4. 采集 DPU 设备计数、Host CPU、Host Payload Touch 和前后台竞争；
5. 与同安全策略的 Raw Direct/软件基线对比。

应观察现象：DPU 能力、实际路径、认证/校验结果和设备完成事件相互一致；Host CPU 只承担控制面职责。

判定边界：DPU 行为只由固定 CSV 或设备型号推测、没有 Tag/CRC 失败用例、或安全策略不同的 A/B，不得用于卸载收益结论。

### Step 6：注入 DPU 故障并验证回退

操作意图：观察真实故障从触发到检测、隔离、切换和最终请求状态的完整链路。

执行动作：

1. 先运行少量请求并确认正常 DPU 路径；
2. 注入 CQE 超时、心跳丢失或设备断开中的一种故障；
3. 记录 `fault_trigger`、`watchdog_detect`、`circuit_open`、`inflight_drained`、`fallback_submit`、`fallback_confirm` 和 `request_done`；
4. 检查在途请求是否重复写入、目标块是否被错误标记 READY；
5. 核对 Raw Direct 是否符合安全策略；若不符合，必须进入本地重算或显式失败；
6. 重复不同并发和队列深度，观察是否存在故障扩散。

应观察现象：新请求不再进入故障 DPU 队列；回退动作与安全策略一致；每个请求有唯一最终路径；完整性和 generation 校验通过。

判定边界：只有注入脚本输出而没有 SUT 事件时，不是故障回退证据；如果回退后仍有重复/半写数据，立即判 `NO-GO`。

### Step 7：验证在途流式处理

操作意图：区分“设备做了传输”与“设备在途完成了量化/压缩/校验”，避免仅凭描述符标志宣称卸载。

执行动作：

1. 对 FP16/FP8 到 INT4/FP4 的转换，使用已冻结张量和误差指标校验输入输出；
2. 对可逆压缩/解压，校验解压数据与原始数据一致，并统计压缩比和额外时延；
3. 对 AES-GCM/SM4，校验 Tag、nonce、密钥句柄和错误输入；
4. 对 CRC64/T10-DIF，注入随机错误和篡改样本，区分错误检测与认证能力；
5. 记录每个 `inline_transform_flags`、实际执行设备、完成事件和 Host Touch。

应观察现象：请求声明的转换与实际设备事件一致；错误输入不会进入 READY；NPU 融合转换的算力开销与 Host CPU 开销分开报告。

判定边界：只在描述符中置位而没有设备完成事件、输出数据或错误样本时，不能判为在途卸载。

### Step 8：统计、复核与归档

操作意图：让性能、安全和故障结论分别可回溯，避免固定样例污染正式汇总。

执行动作：

```text
results/pvt09/<run_id>/
  metadata.yaml
  git_commit.txt
  git_status.txt
  environment.txt
  topology.txt
  hardware_capability_matrix.json
  security_policy.json
  command.txt
  raw_direct_events.jsonl
  dpu_events.jsonl
  cqe_events.jsonl
  watchdog_events.jsonl
  fallback_events.jsonl
  integrity_events.jsonl
  host_payload_touch.jsonl
  cpu_device_counters.jsonl
  request_results.csv
  pvt09_summary.csv
  errors.log
  summary.md
```

应观察现象：每个请求都能关联到唯一通道、epoch、对象 generation 和最终状态；性能数据和安全失败数据均保留。

判定边界：缺少设备完成事件、故障时间线、完整性校验或 Host Touch 探针时，不能输出 `GO`。

## 6. 目标在途处理双轨设计

### 6.1 三类在途处理场景

1. **在途量化/反量化**：在传输路径中将 FP16/FP8 转成 INT4/FP4，或由 NPU 在落入 HBM 后融合反量化；精度误差和转换位置必须实测；
2. **在途压缩/解压**：针对结构化稀疏 KV 使用硬件/软件 Codec 或只传输有效块；压缩比、可逆性和额外时延分开记录；
3. **在途加密/完整性校验**：DPU 或其他受信任设备执行 AES-GCM/SM4、CRC64/T10-DIF；CRC 只能用于错误检测，认证必须由 GCM Tag 或等价机制完成。

这些是目标能力，不是当前 PVT-09 代码已经实现的功能。只有能力矩阵、设备事件和数据校验三者同时存在，才可将某个转换标记为 `MEASURED`。

### 6.2 有 DPU 与无 DPU 的执行边界

| 处理维度 | DPU 硬件卸载 | 无 DPU 的 Raw Direct + NPU 协同 |
|---|---|---|
| 数据通路 | DPU/网卡按能力矩阵执行安全或格式处理，再 DMA 到 NPU HBM | 网卡 DMA 直接写 NPU HBM；NPU 在消费时执行允许的融合转换 |
| Host CPU 职责 | 配置策略、提交描述符、轮询 CQE、看门狗和遥测 | 同样只做控制面，不读取 KV 正文到 Host DDR |
| 量化/反量化 | 只有 DPU 真实支持并完成时才使用；记录误差和格式 | 可由 NPU 融合算子处理；NPU 算力和 Host CPU 占用分开统计 |
| 压缩/解压 | 只有 Codec 能力和输出校验通过时才启用 | 可采用 NPU/软件结构化处理；不能将“少传字节”直接称为可逆压缩 |
| 加密/认证 | DPU 处理 AES-GCM/SM4，并返回可验证 Tag | 若 Raw Direct 没有等价认证，则只适用于策略允许的可信网络 |
| CRC/块校验 | DPU 或设备执行并在完成事件中返回结果 | 可校验描述符/元数据或由 NPU 处理；CRC 不替代密码学认证 |
| 故障 | 看门狗隔离 DPU，按安全策略回退 | 网卡/NPU 失败时本地重算或显式失败，不得声称 DPU 回退 |

### 6.3 目标扩展描述符

PVT-02 描述符层和 PVT-09 传输层可预留在途处理标志。下面只是目标 schema，当前仓库没有该结构：

```cpp
struct alignas(64) HardwareSGEntryExtended {
    uint64_t src_device_addr;
    uint64_t dst_device_addr;
    uint32_t len_bytes;
    uint16_t stream_id;
    uint16_t transform_flags;
    uint32_t security_context_id;
    uint64_t generation;
    uint64_t checksum_or_tag_ref;
};

enum TransformFlags : uint16_t {
    DPU_AES_GCM_DECRYPT = 1u << 0,
    DPU_SM4_DECRYPT = 1u << 1,
    DPU_CRC64_VERIFY = 1u << 2,
    DPU_DEQUANTIZE = 1u << 3,
    NPU_FUSED_DEQUANTIZE = 1u << 4,
};
```

实现时必须验证：设备地址而不是普通 Host 地址、长度和边界、security context 生命周期、Tag/CRC 引用、generation、权限和失败后的资源释放。只检查 flags 不检查实际设备执行，证据等级仍为 `DEMO`。

### 6.4 两条目标执行流

```text
DPU 路径：能力检查 -> DPU 描述符 -> 加密/校验/转换 -> DMA -> HBM -> CQE/Tag 校验 -> READY

Raw Direct：策略检查 -> 网卡 DMA -> HBM -> NPU 融合转换或消费
           -> 若安全策略不满足：本地重算或显式失败
```

两条路径必须分别记录 `planned_path`、`actual_path`、`transform_flags`、设备完成事件、数据校验、Host Touch、切换原因和最终业务状态。

## 7. 证据字段、公式与判定规则

### 7.1 运行结果字段

```csv
run_id,channel_mode,payload_bytes,security_policy,transform_flags,bandwidth_gbps,latency_p50_ms,latency_p99_ms,host_cpu_pct,host_payload_touch_bytes,fault,fault_detect_us,circuit_open_us,fallback_switch_us,request_success_rate,packet_loss_count,integrity_ok,planned_path,actual_path,evidence_level,status,invalid_reason
```

当前 C++ 的 CSV 只有部分字段；新增字段必须由真实采集器填写。缺失字段使用 `null`，不能把空字段或固定 0 解读为零错误、零丢包或零触碰。

### 7.2 故障事件字段

```csv
run_id,request_id,channel_epoch,event,channel_mode,object_id,generation,ts_ns,error_code,inflight_bytes,security_context_id,actual_path,action,status
```

`event` 至少包括 `request_submit`、`cqe_complete`、`heartbeat_missed`、`fault_trigger`、`watchdog_detect`、`circuit_open`、`inflight_drained`、`fallback_submit`、`fallback_confirm`、`integrity_verify`、`request_done` 和 `cleanup`。

### 7.3 计时公式

```text
故障检测时延 = watchdog_detect - fault_trigger
隔离时延     = circuit_open - watchdog_detect
回退切换时延 = fallback_confirm - circuit_open
端到端故障恢复 = request_done - fault_trigger
```

这些差值必须来自同一请求、同一 `channel_epoch` 和单调时钟。注入脚本的配置超时值不是检测时延，期望确认事件也不是回退确认事件。

### 7.4 证据分级

| 级别 | 允许内容 | 不允许内容 |
|---|---|---|
| `DEMO` | 固定 CSV、能力字段 schema、故障事件生成和理论流程 | DPU/Raw Direct 性能、安全、回退或 Host Touch 结论 |
| `LAB` | 测试设备、模拟 DPU 或局部真实 DMA 的可复核结果 | 直接外推到现场生产 DPU/NPU/网卡 |
| `MEASURED` | 真实设备、真实安全策略、完整 CQE/Tag/CRC/故障/探针证据 | 固定数值、能力开关、缺失在途事件或空字段当零 |

### 7.5 状态枚举

- `GO`：通道能力、数据正确性、Host Touch、性能和故障回退均有完整 `MEASURED` 证据，且满足冻结门限；
- `CONDITIONAL`：某通道只在指定硬件、拓扑、安全策略、payload 或故障类型下成立，必须写明限制；
- `NO-GO`：出现错误数据消费、Tag/CRC/generation 失败、DPU 故障未隔离、回退路径违反安全策略或请求状态不可追踪；
- `NOT-SUPPORTED`：现场没有 DPU、Raw Direct 不具备所需 DMA/安全能力、或驱动/固件不支持对应能力；
- `INVALID-EVIDENCE`：固定样例冒充实测、故障脚本未接 SUT、设备能力未探测、关键时间线缺失、空字段被当作零或实际路径不明。

### 7.6 立即止损条件

出现以下任一情况，应停止扩大 payload、并发或故障频率并保留现场：

- 认证失败、CRC/T10-DIF 错误或 generation 不一致的数据进入 READY；
- DPU 超时后仍有新请求进入故障队列；
- 同一请求在 DPU 和 Raw Direct 同时写入且没有幂等/版本保护；
- Host Payload Touch 在声明直达的正常路径出现非零且无法解释；
- Raw Direct 回退绕过了业务强制加密或认证策略；
- 看门狗、设备错误或故障注入导致进程/设备无法恢复；
- 事件时间线无法区分故障触发、检测、隔离、切换和完成。

## 8. 阶段推进与闭环

### E0：能力、策略与证据契约确认

确认 DPU/NPU/网卡型号、驱动、拓扑、安全策略、通道枚举、结果字段和无效证据处理。当前仓库可完成固定 CSV 和故障事件 DEMO 审计。

### E1：Raw Direct 单请求数据路径

打通真实 HBM 目标、DMA/CQE、数据校验和 Host Touch，先在无故障小 payload 下确认基本路径。

### E2：DPU 卸载与在途处理

按能力矩阵接入加密、认证、CRC/T10-DIF、量化/压缩等实际支持的功能，完成同安全策略 A/B 和错误输入校验。

### E3：故障回退与混合压力

注入 DPU 超时、心跳丢失、CQE 错误和设备断开，在前后台并发下验证熔断、在途请求、Raw Direct/重算选择、数据正确性和 500µs 候选门限。

### 条件证伪：DPU 依赖与安全路径

分别关闭 DPU、关闭在途转换、改变安全策略和切换拓扑，确认性能变化来自真实硬件能力而不是固定数据或负载差异。若 Raw Direct 不满足加密策略，应保留 `NOT-SUPPORTED`，不能为了维持吞吐而弱化安全结论。

## 9. 研发人员与 AI Agent 执行约束

### 9.1 研发人员检查清单

- [ ] 已冻结 DPU/NPU/网卡、驱动、固件、PCIe/NUMA 和安全策略；
- [ ] 能力矩阵来自运行时探测，不是 `--hardware-supported` 手工开关；
- [ ] Raw Direct 的真实 HBM 目标、DMA/CQE 和 Host Touch 可回溯；
- [ ] DPU 加密/认证/CRC/转换结果有设备事件和数据校验；
- [ ] DPU 故障有触发、检测、隔离、在途处理、回退、确认和完成时间线；
- [ ] 回退路径没有绕过强制安全策略；
- [ ] generation、请求 ID、Tag/CRC 和重复提交边界已验证；
- [ ] 在途处理 flags 与实际设备执行相符；
- [ ] DEMO/LAB/MEASURED 未混入同一汇总；
- [ ] 空字段未被当作 0、FALSE 或成功；
- [ ] 正式状态统一使用 `GO/CONDITIONAL/NO-GO/NOT-SUPPORTED/INVALID-EVIDENCE`。

### 9.2 AI Agent 执行提示词

```text
你负责执行 PVT-09 DPU 硬件安全处理、Raw Direct 主路径与故障回退验证。

先读取项目索引、公共 Benchmark 契约、本方案和原型目录。确认当前 offload_fallback_bench.cc 只解析 --hardware-supported/--out 并生成固定 DEMO 行，inject_fault.py 只等待约 50ms后写故障事件，不启动 SUT、不测量回退；当前目录没有 eval_dpu_fallback.py。不得把固定吞吐、CPU、80/120µs、成功率或命令行能力开关当作实测。

正式实验先探测 DPU/NIC/NPU 能力、DMA 到 HBM、加密/认证/CRC/T10-DIF 和安全策略。然后分别打通 Raw Direct 单请求、DPU 卸载、在途量化/压缩/校验和真实 DPU 故障回退。记录 CQE、Tag/CRC、generation、Host Payload Touch、CPU/设备计数，以及 fault_trigger/watchdog_detect/circuit_open/fallback_confirm/request_done 事件。

只有在回退路径符合安全策略、数据完整性通过、实际路径可确认且原始时间线完整时，才评估 GO/CONDITIONAL/NO-GO。没有 DPU 或能力不支持时标记 NOT-SUPPORTED；证据缺失时标记 INVALID-EVIDENCE。不得杜撰设备数据，也不得为了维持请求成功而绕过强制加密策略。
```

### 9.3 常见问题定位

| 现象 | 原因定位 | 处理方式 |
|---|---|---|
| C++ 输出固定 685/660 等数值 | 当前程序是结果 schema DEMO | 保持 `DEMO`，接入真实设备和计数器 |
| `--mode`/`--payload-mb` 报错或无效果 | 当前 C++ 没有这些参数 | 按真实 CLI 运行，或先补参数解析 |
| `--sut-cmd` 报未知参数 | 当前故障脚本不启动 SUT | 由外部编排器启动 SUT，并按 request/run ID 关联事件 |
| 故障 JSON 有 expected confirmation | 这是期望事件名称，不是实际确认 | 等待并校验真实 `fallback_confirm` 事件 |
| `--hardware-supported` 后仍无设备事件 | 命令行只改变固定输出分支 | 执行能力探测和真实 CQE/Tag 校验 |
| Raw Direct 传输成功但安全策略不允许 | 没有等价加密/认证 | 判 `NOT-SUPPORTED` 或本地重算/显式失败 |
| 回退后数据 checksum 失败 | 在途请求或半写块处理错误 | 丢弃目标块，检查 generation、幂等和排空逻辑 |
| Host Touch 字段为空 | 没有覆盖数据面全生命周期的探针 | 空值保持无效，不写成 0 |
| 设备错误后请求重复写入 | DPU/Raw Direct 双重提交无版本保护 | 使用 channel epoch、请求 ID 和幂等提交 |
| 只有加密吞吐没有认证失败样本 | 只测了正常路径 | 注入错误 Tag/nonce/密文并核对拒绝行为 |
| 找不到 `eval_dpu_fallback.py` | 当前目录没有评估脚本 | 先实现评估器或按原始事件手工复核，不能引用不存在文件 |

本方案的完成标准不是“固定结果程序返回 0”，而是形成可复核链路：能力矩阵与安全策略 → 真实 DMA/CQE → 加密/认证/完整性校验 → Host 控制面边界 → DPU 故障监测与隔离 → 合规回退/重算 → 请求和数据最终状态。
