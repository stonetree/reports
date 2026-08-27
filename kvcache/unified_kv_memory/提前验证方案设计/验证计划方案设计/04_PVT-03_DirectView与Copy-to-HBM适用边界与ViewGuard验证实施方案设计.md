# PVT-03：Direct-View 与 Copy-to-HBM 适用边界与 ViewGuard 验证实施方案设计
## —— 远端直读与本地显存拷贝的成本交叉、Decode 适用边界和故障回退

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。Direct-View 与 Copy-to-HBM 的性能边界、ViewGuard 租约有效性和远端故障回退必须分别判定；公式输入、固定示意数值和本地模拟不能作为 SIGBUS 捕获、设备流重置或业务连续性的证据。

> **验证范围声明**：当前受控工程中的 `view_vs_copy_bench.cc` 只使用命令行提供的 `t_dma_copy_ms`、`t_local_hbm_read_ms` 和 `t_remote_view_read_ms` 计算两条路径的累计时间，没有访问远端内存或本地 HBM；`benchmark_serving_view.py` 生成固定公式和随机扰动的 DEMO JSON，不连接推理服务；`view_guard.cc` 只实现租约创建、有效性检查和撤销，`handle_remote_crash_fallback()` 明确返回 `false`，没有安装 SIGBUS handler，也没有 `sigsetjmp/siglongjmp`、NPU Stream 重置或本地重算回退。当前代码只能示范成本模型、租约字段和失败边界，不能单独关闭 E1/E2 的性能或容错结论。

> **术语速查**：Direct-View（远端直读，即直接通过高速总线读取远端 KV 数据，不先产生本地显存完整副本）；Copy-to-HBM（拷贝到本地显存，即通过 DMA 将远端 KV 数据完整搬运到本地 HBM）；SVM（Shared Virtual Memory，共享虚拟内存，使设备或进程访问统一虚拟地址空间）；SIGBUS（总线错误信号，访问失效映射或设备总线无响应时可能由操作系统发送）；ViewGuard（视图租约安全守卫机制，负责租约有效期、访问资格和故障回退）；Crossover Point（成本交叉点，即两条路径总耗时相等的重读次数）；TPOT（Time Per Output Token，每个输出 Token 的生成耗时）；TTFT（Time To First Token，首字生成延迟）；Lease（租约，即在限定时间内允许读取某个远端对象的授权记录）。

> **验证 ID**：PVT-03
> **验证名称**：Direct-View 与 Copy-to-HBM 适用边界及 ViewGuard 安全验证
> **验证优先级**：**🟡 P1 级（路径选择与安全支撑项）**
> **对应验证阶段**：**E1（核心数据路径）/ E2（动态调度决策与分层扩容）**
> **证伪标记**：**是（证伪“Decode 活跃 KV 默认适合 Direct-View 远端读取”）**
> **建议周期**：4~6 人日
> **主关联 IR**：`IR-01-07`, `IR-02-04`, `IR-02-05`
> **核心 SRS / SR23 锚点**：
> - SRS：`L1-OL-ViewVsCopy-011`, `L2-MM-ViewLease-028`, `L3-SE-ViewCopyCostModel-034`, `L3-MS-UBC2CTier-055`
> - SR23：`SR23-01-07-01`, `SR23-01-10-01`, `SR23-02-04-01`, `SR23-02-05-02`
> **配套源码**：[`提前验证方案设计/验证计划方案设计/原型验证代码/PVT-03/`](file:///d:/codes/reports/kvcache/unified_kv_memory/提前验证方案设计/验证计划方案设计/原型验证代码/PVT-03)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`；vLLM `842dd8fd96650063e1ad32e6075742d457d39773`。正式结果必须以现场设备、驱动、框架和配置哈希为准。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统系统视角：`mmap` 直读与本地缓存拷贝的取舍

在操作系统和数据库中，远端 `mmap`/共享内存直读与一次性 `read()` 到本地 Buffer 是两种典型策略：

```text
直读：       建立映射/授权 → 每次访问穿过总线或网络
本地拷贝：   一次性搬运到本地 → 后续访问走本地高速内存
```

直读省去一次完整拷贝，适合只读一次或重读次数很少的场景；本地拷贝需要额外显存和一次搬运，却把后续重读转为本地 HBM 访问。哪条路径更快取决于数据大小、远端访问带宽、固定建立开销、本地 HBM 带宽和重读次数，不能用“远端延迟低”单独决定。

### 0.2 大模型推理中的对应物理问题

KVCache（大模型注意力键值缓存，即自回归生成过程中保存历史 Key 和 Value 激活状态、避免后续 Token 重复计算注意力）在 Prefill（首字生成预计算，即对完整 Prompt 做输入理解并生成首个输出 Token 前的计算阶段）和 Decode（逐 Token 生成阶段，即基于历史 KVCache 反复生成后续 Token）中的访问模式不同：

- Prefill 通常对一段历史上下文进行一次大范围扫描，Direct-View 可能节省一次完整搬运；
- Decode 每生成一个 Token 都要再次访问历史 KVCache，远端每次访问的固定时延和带宽限制会被输出 Token 数放大；
- `Copy-to-HBM` 以一次性搬运换取后续本地带宽，但会占用本地显存并增加初始 TTFT；
- 适用边界必须由相同 workload、相同设备、相同数据和真实路径事件计算，示意数值只能用于说明量纲。

### 0.3 为什么需要 ViewGuard

Direct-View 的数据地址或租约依赖远端对象。如果远端节点故障、映射撤销、链路中断或租约过期，本地设备继续访问可能触发总线错误。生产级保护至少需要：

1. 访问前检查租约有效位、对象、地址范围和过期时间；
2. 访问窗口内关联请求、线程和设备 Stream；
3. 故障发生后撤销租约，阻止后续消费；
4. 捕获并记录可归因的 SIGBUS/设备错误；
5. 安全重置挂起的设备队列，并把请求回退到 Copy-to-HBM 或本地重算；
6. 保存故障地址、时间、错误码、回退结果和进程状态。

这是一条跨 CPU、设备和远端节点的故障链。单独返回 `false` 或打印一行日志，不能证明进程没有崩溃，也不能证明数据已经安全回退。

### 0.4 当前配套工程能够证明什么，不能证明什么

| 子实验 | 当前源码能够完成的动作 | 当前源码不能直接证明的内容 | 当前默认证据 |
|---|---|---|---|
| View-vs-Copy 成本模型 | 对固定 read count 计算两条公式路径总时间和较优路径 | 远端总线读、本地 DMA、HBM 读、实际 TTFT/TPOT | `DEMO / W0` |
| 租约创建/校验 | 记录对象、远端地址、长度、过期时间和原子有效位；可撤销租约 | 远端映射、地址权限、并发引用安全、故障期间访问 | `DEMO / W0` |
| 故障回退接口 | 打印 `DEMO_ONLY`，返回 `false` | SIGBUS 捕获、NPU Stream Abort、进程不崩溃、本地重算和请求连续性 | `NOT-SUPPORTED` |
| 服务对照脚本 | 生成 View/Copy 的合成 TTFT 和 TPOT 分位数 | 真实推理端点、远端读、显存占用、Decode 事件和故障安全 | `DEMO / W0` |

---

## 1. 验证目标与交付结论定义

### 1.1 待验证核心命题

1. **命题一：Direct-View 与 Copy-to-HBM 存在可测的成本交叉**。在 payload、远端/本地带宽和固定建立开销冻结后，测量不同重读次数下两条路径总时延，确定现场的 Crossover Point；
2. **命题二：Decode 活跃阶段的远端重复读取可能不具备净收益**。对 View 与 Copy 的真实 Decode 请求，比较 TPOT P50/P99、尾部抖动和本地显存占用，决定是否对 Decode 设置 Copy-to-HBM 优先路径；
3. **命题三：ViewGuard 能把失效远端视图转成可控回退**。在租约过期、远端进程/节点故障、链路断开等故障注入下，验证访问是否被阻止、异常是否被捕获、设备队列是否安全处理、请求是否回退以及进程是否保持可用；
4. **命题四：路径选择必须由动态成本而不是命中状态决定**。Direct-View、Copy-to-HBM 和本地重算应由数据规模、重读次数、Deadline、链路能力、租约状态和本地显存水位共同决定。

### 1.2 交付物与结论边界

每个正式 `run_id` 至少交付：

1. 《不同重读次数下 View-vs-Copy 成本交叉表》：包含原始分项时延、总时延、重读次数、payload、路径和能力矩阵来源；
2. 《Decode View-vs-Copy TTFT/TPOT 对照表》：保留逐请求/逐 Token 原始样本、失败请求、显存占用和重复实验离散度；
3. 《ViewGuard 租约失效与远端故障回退测试表》：包含故障注入动作、信号/设备事件、租约状态、回退路径、恢复时间和进程状态；
4. `manifest.json`、`environment.json`、运行命令、代码包、配置哈希、原始日志、Profiler/设备事件和摘要；
5. 性能边界与容错安全分别输出 `GO`、`CONDITIONAL`、`NO-GO`、`NOT-SUPPORTED` 或 `INVALID-EVIDENCE`，不因性能通过而跳过安全结论。

---

## 2. 成本模型、决策树与 ViewGuard 目标设计

### 2.1 View-vs-Copy 成本模型

设 payload 为 $S$，重读次数为 $N$，远端直读固定开销为 $t_{view\_setup}$，Copy-to-HBM 一次性搬运开销为 $t_{copy\_setup}$：

$$
T_{view}(N,S)=t_{view\_setup}+N\times(t_{remote\_fixed}+S/BW_{remote})
$$

$$
T_{copy}(N,S)=t_{copy\_setup}+S/BW_{copy}+N\times(S/BW_{local\_hbm})
$$

当两条路径总时延相等时得到 $N_{crit}$。正式计算必须使用 PVT-01 或现场能力矩阵的真实输入，并保留单位、测量误差和有效期；文档中的任何 `0.02ms`、`0.85ms`、`3.20ms` 等数字只能作为示意输入，不是本项成绩。

### 2.2 微秒级路径选择逻辑

Direct-View 不是“命中即使用”。建议按以下顺序执行：

```text
请求到达
  └─► 本地已有可消费副本？──是──► 本地 HBM 复用
               │否
               ▼
       是否为 Decode 高频重读？──是──► 优先 Copy-to-HBM 或本地重算
               │否
               ▼
       读取次数、payload、Deadline 和能力矩阵是否支持 View？
               │否                         │是
               ▼                           ▼
          比较 Copy/重算成本        比较 View 与 Copy/重算成本
               │                           │
               └────────► 选择有净收益且租约有效的路径
```

判定时必须同时考虑：

- Direct-View 的租约剩余时间是否覆盖整个访问窗口；
- 远端对象、地址范围、布局和权限是否一致；
- Copy-to-HBM 是否有显存空间和设备完成事件；
- 本地重算是否比加载和挂接更快；
- 任何候选路径都不能超过请求 Deadline 或破坏前台 TPOT。

### 2.3 ViewLease 当前实现与目标扩展

当前 `ViewLease` 字段为 `object_id`、`remote_addr`、`size_bytes`、过期时间和原子 `is_valid`。目标生产结构至少还需要：

```cpp
struct ViewLeaseDescriptor {
    uint64_t lease_id;
    uint64_t object_id;
    uint64_t remote_va_or_handle;
    uint32_t payload_bytes;
    uint32_t layout_version;
    uint64_t visibility_epoch;
    uint64_t expire_timestamp_ns;
    uint32_t owner_node;
    uint32_t permissions;
    // 并发引用、撤销状态、错误原因等固定字段
};
```

目标结构仍需由现场协议决定。必须加入地址范围、布局版本、可见性 epoch、权限、引用计数和撤销原因，且对并发访问和跨进程读取做固定大小断言。当前代码没有这些字段，不能直接当作生产 View 协议。

### 2.4 ViewGuard 故障恢复目标链

```text
访问前校验租约
      ↓
进入 Direct-View 临界区并记录请求/Stream
      ↓
远端故障或租约撤销
      ↓
捕获实际 SIGBUS/设备错误并记录故障地址
      ↓
停止或隔离挂起的设备队列
      ↓
撤销租约、阻断失效对象
      ↓
Copy-to-HBM 或本地重算
      ↓
恢复请求并输出完整故障证据
```

`sigaction`、`sigsetjmp/siglongjmp` 和 `aclrtStreamAbort` 等是可能的实现手段，不是当前代码已具备的能力。信号处理函数中还必须遵守异步信号安全约束，不能在 handler 中直接调用未经验证的复杂分配、锁或日志函数；设备队列能否安全重置也必须由现场驱动确认。

---

## 3. 实验方案与测试矩阵设计

### 3.1 三组子实验矩阵

| 子实验 | 正式参数 | 核心观测指标 | 当前源码覆盖 |
|---|---|---|---|
| View-vs-Copy 交叉 | payload 16MB、64MB 及现场扩展；重读次数 1、2、4、8、16、32、64、128、256 | View 总时延、Copy 总时延、分项成本、$N_{crit}$、显存占用 | 支持固定 read_counts 和公式输入；无真实读写 |
| Decode 在线 A/B | View/Copy、Prompt 长度、输出 Token 数、请求率、并发和重复轮次 | TTFT、TPOT P50/P95/P99、失败、尾部、显存和实际路径 | 只有合成 JSON 脚本；不连接服务 |
| ViewGuard 故障注入 | 租约超时、远端进程/节点故障、链路断开、地址撤销；每类至少 3 次独立重复 | 信号/设备事件、租约状态、Stream 处理、回退路径、恢复时间、进程存活 | 当前无故障测试可执行程序，回退接口固定失败 |

### 3.2 成本模型输入矩阵

| 维度 | 正式计划 | 当前工具状态 |
|---|---|---|
| payload | 16MB、64MB、现场可用等价档位 | `--payload-mb` 单值 |
| 重读次数 | 1、2、4、8、16、32、64、128、256 | `read_counts` 固定在源码内，无 CLI 选择 |
| 远端直读耗时 | 来自真实 Direct-View 完成事件或 PVT-01 能力矩阵 | `--remote-read-ms` 只是输入参数 |
| Copy 搬运耗时 | 来自真实 DMA 完成事件 | `--dma-copy-ms` 只是输入参数 |
| 本地 HBM 读取耗时 | 来自真实设备事件 | `--local-read-ms` 只是输入参数 |

### 3.3 环境与证据矩阵

| 环境 | 目的 | 最低条件 | 允许形成的结论 |
|---|---|---|---|
| W0 公式/合成 | 验证命令、成本模型、字段和状态 | C++/Python 运行时 | 仅形成 `DEMO` 结论 |
| W1 局部设备 | 验证同一设备上的 View/Copy 局部路径和租约逻辑 | 可用设备内存、DMA、事件和故障控制 | 形成绑定设备的 `LAB` 结论 |
| W2 真实跨节点 | 验证远端读取、Decode 影响和故障回退 | 跨节点数据路径、真实推理、设备事件、故障注入权限 | 证据闭环后形成 `MEASURED` 结论 |

### 3.4 公平 A/B 与安全隔离

性能 A/B 必须保持模型、Prompt、输出长度、设备、链路、请求率、资源配额、预热和统计口径一致，只改变 View/Copy 路径。故障注入必须在隔离环境进行，记录注入时间和影响对象，不允许以生产请求作为未经批准的故障试验对象。性能门槛和故障安全门槛分别判定。

---

## 4. 测试工具、当前实现边界与正式扩展要求

### 4.1 当前受控工程目录与运行方式

```text
原型验证代码/PVT-03/
├── Makefile
├── view_vs_copy_bench.cc
├── view_guard.h
├── view_guard.cc
└── benchmark_serving_view.py
```

当前可复现的 W0 命令：

```bash
cd ./原型验证代码/PVT-03
make clean
make
./view_vs_copy_bench --payload-mb 64 \
    --dma-copy-ms 3.20 \
    --local-read-ms 0.06 \
    --remote-read-ms 0.85 \
    --evidence-level DEMO \
    --out res_view_vs_copy_demo.csv
python3 ./benchmark_serving_view.py \
    --mode view --prompt-len 32768 --decode-tokens 256 \
    --seed 42 --output res_serving_view_demo.json
```

这些命令中的时间和合成服务结果都是 DEMO 输入。当前工程没有 `view_guard_test`、`plot_crossover.py`，服务脚本的模式名称是 `view`/`copy`，不是 `direct_view`/`copy_to_hbm`。

### 4.2 源码实际行为审计

| 代码路径 | 实际行为 | 对证据的影响 |
|---|---|---|
| `view_vs_copy_bench.cc` | 固定扫描 1、2、4、8、16、32、64、128、256 次，按输入耗时计算 View/Copy 总时间和较优路径 | 是成本模型，不访问远端或本地设备；输出没有状态字段 |
| `view_vs_copy_bench.cc` 默认值 | 使用 3.20ms、0.06ms、0.85ms 等代码内默认输入；可被 CLI 覆盖 | 默认结果不能当作现场测量，必须记录输入来源和证据等级 |
| `--evidence-level` | 接受任意字符串，不限制 DEMO/LAB/MEASURED | 只能由外部证据审查层校验，不能信任命令行标签 |
| `view_guard.cc::create_lease` | 记录地址、大小、过期时间并把 `is_valid` 置 true | 没有地址范围、权限、可见性 epoch、引用计数或远端注册 |
| `view_guard.cc::validate_access` | 检查原子有效位和当前时间是否超过过期时间 | 没有对象/地址/长度校验，也没有并发撤销与设备完成关联 |
| `handle_remote_crash_fallback` | 打印 `DEMO_ONLY`，返回 `false` | 没有 SIGBUS handler、Stream Abort 或本地重算回退 |
| `benchmark_serving_view.py` | 对 View/Copy 使用固定 TTFT 公式，并为 TPOT 生成随机分布；输出 `evidence_level=DEMO`、`status=DEMO_ONLY` | 不是推理服务，不能证明真实 TTFT/TPOT 或远端直读 |
| Makefile | 只链接 C++ 标准库和 pthread，无设备/框架 SDK | 当前工程不能执行目标硬件路径 |

### 4.3 面向 LAB/MEASURED 的最小工程扩展

1. **真实 View/Copy 路径**：接入远端映射/读取、Copy-to-HBM DMA、本地 HBM 访问和完成事件；记录实际 payload、读次数和路径凭证；
2. **动态能力输入**：读取 PVT-01 产生的硬件能力矩阵和有效期，不能使用代码内默认时延；
3. **真实推理打流**：将 View/Copy 作为实际端点或配置开关，记录 TTFT、TPOT、请求失败、显存水位、实际路径和逐 Token 事件；
4. **租约完整性**：增加 lease_id、对象/布局哈希、权限、可见性 epoch、引用计数、撤销原因和并发访问保护；
5. **异常处理闭环**：在隔离环境实现并验证 SIGBUS/设备错误捕获、Stream 安全处理、租约撤销、Copy/重算回退和恢复结果；
6. **故障注入工具**：实现租约超时、远端进程/节点故障、链路断开和地址撤销的可控注入，保留注入时间与实际故障事件；
7. **统计与证据包**：至少 3 次独立重复，保存逐请求/逐 Token 样本、Profiler/设备事件、进程存活、核心转储状态、版本和拓扑；
8. **状态归一化**：将脚本 `DEMO_ONLY`、回退 `false`、解析失败等内部状态映射为公共契约状态，缺失字段写 `null` 和 `invalid_reason`。

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结实验身份、能力输入和安全边界

- **操作意图**：先区分公式 DEMO、局部 LAB 和真实 MEASURED，并确保性能试验与故障注入隔离。
- **执行动作**：填写 `run_id`、`workload_id`、`package_id`、`baseline_commit`、`config_hash`、`hardware_profile`、`topology_profile`、能力矩阵版本、payload、重读次数、输出 Token 数、预热、重复轮次和门槛；列出当前未实现的故障链。
- **应观察现象**：能明确说明耗时来自真实设备还是 CLI 输入；没有远端路径、设备事件或故障注入工具时提前标 `NOT-SUPPORTED`。

### 步骤 1：构建并运行成本模型 W0

- **操作意图**：验证 Crossover 表的命令、字段和公式流程，建立后续真实能力数据接入的回归样本。
- **执行命令**：

```bash
cd ./原型验证代码/PVT-03
make clean
make
./view_vs_copy_bench --payload-mb 64 \
    --dma-copy-ms 3.20 --local-read-ms 0.06 --remote-read-ms 0.85 \
    --evidence-level DEMO --out res_view_vs_copy_demo.csv > crossover_stdout.txt 2>&1
```

- **应观察现象**：CSV 按固定 read_counts 输出 View/Copy 总时间和 `better_path`；`evidence_level` 只是命令行输入，必须在 manifest 中明确标 `DEMO`。
- **判定边界**：不能把代码内默认时延、交叉点或 `better_path` 写成硬件实测；真实 `N_crit` 必须由能力矩阵和原始事件重新计算。

### 步骤 2：运行合成 View/Copy 服务流程 DEMO

- **操作意图**：验证 TTFT/TPOT JSON schema 和分位数输出，不把合成数据误解为真实推理服务。
- **执行命令**：

```bash
python3 ./benchmark_serving_view.py \
    --mode view --prompt-len 32768 --decode-tokens 256 \
    --seed 42 --output res_serving_view_demo.json
python3 ./benchmark_serving_view.py \
    --mode copy --prompt-len 32768 --decode-tokens 256 \
    --seed 42 --output res_serving_copy_demo.json
```

- **应观察现象**：JSON 的 `evidence_level=DEMO`、`status=DEMO_ONLY`；TPOT 由脚本内随机分布生成，输入 seed 只保证该合成流程可复现。
- **证据边界**：脚本没有连接 `/v1/completions` 或真实 KV 路径，不能用它关闭 Decode 证伪命题。

### 步骤 3：核对真实 View/Copy A/B（条件步骤）

- **前置条件**：真实 View/Copy 端点、设备完成事件、能力矩阵、模型和数据集均已准备；A/B 只改变路径策略。
- **操作意图**：在真实 Prompt、输出长度和请求率下比较 TTFT、TPOT、显存和失败率，确认是否存在 Decode 重复远端读取放大。
- **执行动作**：按同一 workload 分别运行 View 与 Copy；每次记录实际路径、租约、payload、重读次数、设备事件、逐 Token 统计和同场次本地重算参考。
- **应观察现象**：View/Copy 的差异能回指能力矩阵和原始事件；若实际路径无法证明或只有合成服务脚本，应标 `NOT-SUPPORTED`/`INVALID-EVIDENCE`。

### 步骤 4：验证租约创建、过期和撤销

- **操作意图**：先验证不涉及硬件故障的访问资格边界，再进入危险的远端故障注入。
- **执行动作**：创建短租约，分别在有效期内、过期后和显式撤销后调用 `validate_access`；记录时间戳、对象、地址、长度、有效位和返回值。
- **应观察现象**：有效租约允许访问，过期/撤销租约拒绝访问；当前代码只能证明这两个本地判断，不能证明远端映射或设备消费安全。

### 步骤 5：执行远端故障与 SIGBUS 回退（条件步骤）

- **前置条件**：已在隔离环境完成真实 View 路径、SIGBUS/设备错误捕获、设备队列处理、租约撤销和 Copy/重算回退实现，并有恢复验证方案。
- **操作意图**：验证远端故障不会把本地推理进程直接带入不可恢复状态，并且失效对象不会继续被消费。
- **执行动作**：分别注入租约超时、远端进程/节点故障、链路断开和映射撤销；记录注入时间、故障信号/设备错误、故障地址、Stream 状态、租约状态、回退路径、恢复时间、请求结果和进程存活。
- **应观察现象**：异常能被实际捕获，失效租约被阻断，设备队列按现场驱动规则处理，请求按约定回退；任一环节没有原始事件时不得判定通过。
- **当前代码边界**：`handle_remote_crash_fallback()` 当前固定返回 `false`，不存在可直接执行的 `view_guard_test`；本步骤在未扩展前记录 `NOT-SUPPORTED`。

### 步骤 6：生成分项证据包和决策摘要

- **操作意图**：把性能交叉、在线影响和故障安全分开归档，防止性能曲线覆盖容错缺口。
- **执行动作**：按 `results/PVT-03/<subtest>/<run_id>/` 建目录，保存成本输入、能力矩阵、原始 TTFT/TPOT、租约事件、故障注入日志、进程状态、Profiler/设备事件、`manifest.json`、`environment.json` 和摘要；每个条件至少 3 次独立重复。
- **应观察现象**：每个 `N_crit`、TPOT 分位数和回退结果能回指原始样本；没有采集到的字段使用 `null` 并填写 `invalid_reason`。

---

## 6. 数据采集清单与记录格式

### 6.1 View-vs-Copy 原始字段

```text
run_id, validation_id, trace_id, request_id, event_name,
workload_id, model_id, model_layout_manifest, prompt_tokens, decode_tokens,
payload_bytes, read_count, mode, planned_path, actual_path,
t_view_setup_ns, t_remote_read_ns, t_copy_setup_ns, t_dma_copy_ns,
t_local_hbm_read_ns, total_time_ns, better_path, crossover_input_source,
lease_id, visibility_epoch, lease_valid, lease_expire_ns,
device_complete_ns, local_hbm_bytes, remote_bytes,
ttft_ns, tpot_ns, gpu_or_npu_memory_bytes,
package_id, baseline_commit, config_hash, hardware_profile, topology_profile,
evidence_environment, evidence_level, status, error_code, invalid_reason
```

### 6.2 ViewGuard 故障字段

```text
run_id, validation_id, fault_id, fault_type, injection_start_ns,
target_node, target_object_id, active_readers, lease_id,
signal_name, fault_address, device_error_code, stream_id,
stream_action, lease_state_before, lease_state_after,
fallback_action, fallback_start_ns, fallback_end_ns,
request_status, process_alive, core_dump_created,
package_id, config_hash, hardware_profile, topology_profile,
evidence_environment, evidence_level, status, invalid_reason
```

`signal_name`、`device_error_code`、`process_alive` 或 `fallback_action` 没有实际事件时写 `null`，不能用 `FALSE` 代替“没有观测到”。

### 6.3 汇总 CSV 模板

```csv
validation_id,run_id,subtest,mode,payload_bytes,read_count,view_total_ms,copy_total_ms,better_path,ttft_p50_ms,ttft_p99_ms,tpot_p50_ms,tpot_p99_ms,lease_valid,fault_type,signal_caught,stream_action,fallback_action,fallback_time_us,process_alive,evidence_level,status,invalid_reason
<PVT-03>,<run_id>,<crossover_or_decode_or_fault>,<view_or_copy>,<bytes>,<N>,<measured_or_null>,<measured_or_null>,<view_or_copy_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<true_or_null>,<fault_or_null>,<true_or_null>,<abort_or_null>,<recompute_or_copy_or_error_or_null>,<measured_or_null>,<true_or_null>,<DEMO_OR_LAB_OR_MEASURED>,<status>,<null_or_reason>
```

### 6.4 证据包目录

```text
results/PVT-03/<subtest>/<run_id>/
├── manifest.json
├── environment.json
├── capability_matrix.json
├── raw_cost_events.jsonl
├── raw_serving_events.jsonl
├── raw_fault_events.jsonl
├── raw_metrics.*
├── profiler_trace.*
├── process_state/
├── summary.json
├── summary.csv
└── logs/
```

`manifest.json` 至少记录能力矩阵版本、代码包、基线 Commit、配置哈希、模型和 workload、故障注入授权/范围、设备与拓扑、执行命令、原始文件哈希、证据等级、支持范围和分项状态。

---

## 7. GO / CONDITIONAL / NO-GO / NOT-SUPPORTED / INVALID-EVIDENCE 判定规则

### 7.1 View-vs-Copy 适用边界

- **GO（成本边界可复现）**：真实 View/Copy 事件、能力矩阵、payload、重读次数和同场次基线齐全；$N_{crit}$ 在至少 3 次独立重复中稳定，路径选择与实际总耗时一致，并且没有超 Deadline、显存或失败率退化。
- **CONDITIONAL（场景受限）**：Direct-View 只在低重读、较小 payload 或 Prefill 场景有净收益；Decode 结论限定为已测输出长度、并发和拓扑。
- **NO-GO（路径没有净收益）**：有效实测中 View 或 Copy 在声明场景持续不如另一条路径，或路径引入不可接受的尾部时延、显存或失败率退化。
- **NOT-SUPPORTED**：当前只有公式模型、合成服务脚本、没有远端/本地设备事件或能力矩阵不可用。

### 7.2 Decode 直读证伪

- **GO（完成证伪并形成策略）**：真实在线 A/B 显示 Decode 的 Direct-View 在运行前冻结的输出长度和并发范围内，TPOT/尾部成本显著高于 Copy-to-HBM 或本地重算，并能由逐 Token 远端读事件解释；随后把 Copy-to-HBM 设为相应场景的优先路径。
- **CONDITIONAL（边界而非全局结论）**：只有特定 payload、输出长度、复用模式或设备上观察到直读放大；策略限定在这些条件。
- **NO-GO（证伪不成立或目标路径更差）**：实测未观察到预期差异，或 Copy-to-HBM 的一次性成本和显存压力反而造成更差的在线结果；不得保留未经数据支持的固定策略。

### 7.3 ViewGuard 故障安全

- **GO（故障链闭环）**：每类运行前冻结的故障注入均有实际故障事件；失效租约被阻断，信号/设备异常被捕获，设备队列按驱动规则处理，请求回退到有效路径，进程保持可用，恢复时间和数据一致性均可回溯。
- **CONDITIONAL（局部保护）**：仅租约过期或软件撤销通过，尚未覆盖远端节点故障、链路断开或设备异常；只能声明已覆盖的故障类型。
- **NO-GO（安全回退失败）**：有效故障注入导致进程崩溃、失效视图继续被消费、设备队列无法恢复或请求没有安全回退。
- **NOT-SUPPORTED**：当前没有 SIGBUS/设备错误处理、故障注入程序或真实 View 路径。

### 7.4 统一无效证据规则

以下任一情况将对应子实验标为 `INVALID-EVIDENCE`，不得输出 `GO`：

- 把代码内 0.02ms、0.85ms、3.20ms 或服务脚本合成数值写成实测；
- 把 `--evidence-level LAB` 字符串、`DEMO_ONLY` 或 `handle_remote_crash_fallback=false` 当作真实状态；
- 没有实际远端读、Copy DMA、HBM 读取、设备完成或逐 Token 事件；
- 租约对象、地址、长度、布局、权限、可见性 epoch 或实际路径无法证明；
- 缺少故障注入时间、信号/设备事件、进程状态、回退结果或原始日志；
- 缺少同场次 A/B、重复实验、版本、拓扑或能力矩阵；
- 缺失字段用 0 或 `FALSE` 补齐，或将未观测解释为“没有故障”。

---

## 8. 执行阶段与交付闭环

| 阶段 | 工作内容 | 必须交付 | 退出条件 |
|---|---|---|---|
| 阶段 A：模型与 W0 流程 | 审计成本公式、合成服务、租约校验和回退占位 | 公式 CSV、合成 JSON、源码审计、`DEMO` manifest | 示意输入与未支持项明确 |
| 阶段 B：真实性能路径 | 接入 View/Copy 设备路径和在线推理 A/B，形成 $N_{crit}$ 与 TPOT 证据 | 原始读/拷贝事件、TTFT/TPOT、显存和重复汇总 | 每个指标能回指设备事件 |
| 阶段 C：故障安全与策略固化 | 完成租约撤销、远端故障注入、设备处理和回退对账 | 故障证据包、恢复表、路径策略和分项结论 | 性能与安全均通过公共契约，未覆盖项单独列出 |

本项的最终作用是让调度器根据真实成本和租约状态选择远端直读、本地拷贝或重算，并验证远端视图失效时的安全回退边界。它不能以“公式更快”代替设备实测，也不能以“捕获了一个信号”代替完整业务恢复。

---

## 9. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师负责**：
  1. 确认远端映射、设备 DMA、HBM、能力矩阵、推理端点和故障注入权限；
  2. 冻结 payload、重读次数、输出长度、A/B、故障类型、重复轮次和安全边界；
  3. 执行并记录 View/Copy 请求、租约生命周期、故障注入、进程状态和回退结果；
  4. 判断 SIGBUS/设备事件和恢复是否来自真实路径，确认测试不会影响生产请求；
  5. 对 Direct-View 是否开放给某一场景承担现场复核责任。
- **AI Agent 负责**：
  1. 先阅读方案和四个实际源码文件，列出真实 CLI、默认参数、输出状态和未实现功能；
  2. 编写能力矩阵读取、成本复算、TTFT/TPOT 分位数、租约事件和故障日志解析工具；
  3. 检查 View/Copy 同场次 A/B、$N_{crit}$、缺失字段和证据等级；
  4. 不凭空创建 `view_guard_test`、`plot_crossover.py`、SIGBUS 通过数据或 NPU Stream 恢复结论。

### 9.2 可直接复制给 Coding Agent 的 Prompt 模板

```text
我正在执行 PVT-03：Direct-View 与 Copy-to-HBM 适用边界及 ViewGuard 安全验证。

请先阅读：
1. ./提前验证方案设计/验证计划方案设计/04_PVT-03_DirectView与Copy-to-HBM适用边界与ViewGuard验证实施方案设计.md
2. ./提前验证方案设计/验证计划方案设计/Benchmark公共契约与证据分级规范.md
3. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-03/view_vs_copy_bench.cc
4. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-03/view_guard.h
5. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-03/view_guard.cc
6. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-03/benchmark_serving_view.py

约束：
- 先列出真实 CLI 和源码行为；确认 view_vs_copy_bench.cc 只用 CLI 时间做公式，benchmark_serving_view.py 只输出 DEMO 合成 JSON。
- 确认 ViewLease 只有对象、地址、长度、过期时间和有效位；handle_remote_crash_fallback 当前返回 false，没有 SIGBUS handler、Stream Abort 或回退实现。
- 真实性能结论必须读取能力矩阵并保留远端读、Copy DMA、HBM 读取、设备完成和逐 Token 事件；不能把示意值写成 MEASURED。
- ViewGuard 只有在故障事件实际发生、租约撤销、设备处理、Copy/重算回退、进程状态和恢复结果全部可回溯时才允许判定。
- 缺失字段使用 null 并填写 invalid_reason；性能边界与安全故障分开出结论；不创建不存在的 view_guard_test 或 plot_crossover.py。
- 最后输出：源码能力矩阵、实际命令、View/Copy 成本表、Decode A/B 字段、故障注入字段、未支持项和下一步最小代码改动建议。
```

### 9.3 常见排错指南

- **Crossover 点随默认参数变化**：这是公式模型预期现象；检查输入时延是否来自能力矩阵和同一 payload，不能把某个默认交叉点写成固定结论。
- **运行服务脚本后没有真实端点日志**：当前脚本不连接推理服务，只生成 DEMO JSON；不要把它当作 TTFT/TPOT 实测。
- **`validate_access` 在租约过期后仍允许访问**：核对单调时钟、过期时间单位、并发撤销和对象/地址范围；当前实现没有完整并发安全协议。
- **故障回退返回 `false`**：这是当前 `handle_remote_crash_fallback()` 的固定行为，表示回退未实现，不是“故障已安全处理”。
- **想直接运行 `view_guard_test`**：当前目录没有该文件；先实现隔离故障注入工具和原始事件记录，再执行安全判定。
- **SIGBUS handler 导致新的崩溃**：检查 handler 中是否调用了非异步信号安全函数、是否重复进入、是否有有效 `sigsetjmp` 锚点；设备队列处理必须由现场驱动验证。
- **Copy-to-HBM 显存不足**：记录显存水位、分配失败和回退到重算的实际事件；不能用降小 payload 后的结果替代原场景。
- **跨节点时间无法直接相减**：优先使用同一节点单调时钟；跨节点时记录 PTP/其他同步方式及误差上限。
- **View 更快但业务 TPOT 变差**：区分一次性 TTFT 和 Decode 高频重读成本，不能只按首字延迟选择路径。
