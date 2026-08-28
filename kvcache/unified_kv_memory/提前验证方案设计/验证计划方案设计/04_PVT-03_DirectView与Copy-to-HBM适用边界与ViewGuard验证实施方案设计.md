# PVT-03：Direct-View 与 Copy-to-HBM 适用边界与 ViewGuard 验证实施方案设计
## —— 远端直读与本地显存拷贝的成本交叉、Decode 适用边界和故障安全回退

> **公共执行契约**：本项严格遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。Direct-View（远端直读）与 Copy-to-HBM（拷贝到本地显存）的性能适用边界、ViewGuard（视图租约安全守卫机制）租约有效性以及远端节点故障安全回退机制必须独立记录并分别判定；严禁将纯公式推导输入、固定示意数值或单机本地模拟作为证明 SIGBUS 异常捕获、加速器硬件 Stream 安全重置或在线业务连续性的有效依据。

> **验证范围声明**：在当前受控的原型验证工程中，`view_vs_copy_bench.cc` 仅根据命令行传入的 `t_dma_copy_ms`、`t_local_hbm_read_ms` 及 `t_remote_view_read_ms` 参数进行理论公式累加，并未实际访问远端节点内存或本地 HBM；`benchmark_serving_view.py` 仅依据固定公式与随机扰动生成 DEMO 级别的 JSON 输出，未实际对接推理服务引擎；`view_guard.cc` 仅实现了本地租约的创建、有效性基础检查与主动撤销，其 `handle_remote_crash_fallback()` 接口固定返回 `false`，尚未安装真正的操作系统 SIGBUS 信号处理 Handler，亦未集成 `sigsetjmp/siglongjmp` 上下文恢复、NPU Stream 异常终止或本地自动重算回退机制。因此，现有受控源码仅用于演示成本模型计算、租约元数据结构与异常拦截边界，不可直接作为关闭 E1/E2 阶段性能边界或生产级容错结论的依据。

> **术语速查**：
> - **Direct-View**：远端直读（直接通过高速互联总线跨节点读取远端显存中的 KV 数据，不产生本地显存完整副本）；
> - **Copy-to-HBM**：拷贝到本地显存（通过 DMA 将远端 KV 数据完整搬运至本地高带宽显存 HBM 中）；
> - **SVM**：Shared Virtual Memory（共享虚拟内存：支持异构设备或分布式进程访问统一虚拟地址空间的内存机制）；
> - **SIGBUS**：总线错误信号（当进程访问物理总线未响应、非法映射或已失效的远端显存时由操作系统内核发送的同步硬件异常信号）；
> - **ViewGuard**：视图租约安全守卫机制（负责远端直读生命周期管理、访问租约有效性校验、SIGBUS 信号安全捕获及故障自动回退的系统保护机制）；
> - **Crossover Point**：成本交叉点（远端直读累加耗时与本地显存拷贝总耗时相等的临界读取次数）；
> - **TTFT**：Time To First Token（首字生成延迟 / 首 Token 响应时间）；
> - **TPOT**：Time Per Output Token（每个输出 Token 的生成耗时 / 单字生成延迟）；
> - **Lease**：租约（在严格限定时间窗口内允许特定请求读取特定远端对象的授权凭证）。

> **验证 ID**：PVT-03
> **验证名称**：Direct-View 与 Copy-to-HBM 适用边界及 ViewGuard 安全验证
> **验证优先级**：**🟡 P1 级（路径选择与安全支撑项）**
> **对应验证阶段**：**E1（核心数据路径打通）/ E2（动态调度决策与分层扩容）**
> **证伪标记**：**是（证伪“Decode 活跃阶段的 KVCache 默认适合 Direct-View 远端直读”）**
> **主关联 IR**：`IR-01-07`, `IR-02-04`, `IR-02-05`
> **核心 SRS / SR23 锚点**：
> - SRS：`L1-OL-ViewVsCopy-011`, `L2-MM-ViewLease-028`, `L3-SE-ViewCopyCostModel-034`, `L3-MS-UBC2CTier-055`
> - SR23：`SR23-01-07-01`, `SR23-01-10-01`, `SR23-02-04-01`, `SR23-02-05-02`
> **配套源码**：[`./原型验证代码/PVT-03/`](./原型验证代码/PVT-03/)
> **开源基线版本**：Mooncake `f90ae691f109e49a60920e0c8abbf7e572826d8c`；vLLM `842dd8fd96650063e1ad32e6075742d457d39773`。正式测试结果必须以现场硬件设备、驱动版本、推理框架及配置哈希为准。

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统系统视角：`mmap` 直读与本地缓存拷贝的取舍

在经典操作系统与高性能数据库系统中，远端 `mmap`/共享内存直读与一次性 `read()` 拷贝至本地缓存是两种经典的访问策略：

```text
远端直读 (Direct-View) : 建立映射与租约授权 ────────► 每次数据访问均直接跨越总线或物理网络
本地拷贝 (Copy-to-HBM) : 一次性通过 DMA 搬运至本地 ──► 后续所有重读操作均访问本地超高速内存
```

远端直读免去了一次性完整数据拷贝的开销与本地显存占用，非常适合只读取一次或重读频次极低的数据访问；而本地拷贝虽然引入了一次性搬运时延并占用本地显存，但能够将后续高频重读的访存带宽提升至本地高带宽显存 (HBM) 的物理极限。究竟哪一条路径具有更低的综合耗时，严格取决于传输数据量 (Payload)、远端总线访问带宽、固定建立时延、本地 HBM 访存带宽以及后续重读次数的联合函数，绝不能单凭“远端直读无需拷贝”的主观直觉盲目选用。

### 0.2 大模型推理中的对应物理问题

KVCache（大模型注意力键值缓存，即自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免后续 Token 生成时重复计算注意力）在 Prefill（首字生成预计算阶段）与 Decode（逐字生成阶段）两个阶段展现出截然不同的访存动力学特征：

- **Prefill 阶段**：通常对一段超长的历史 Prompt 上下文执行单次大范围注意力计算，采用 Direct-View 远端直读能够直接节省一次超长上下文的全量搬运耗时；
- **Decode 阶段**：每生成一个新的输出 Token，计算核心都必须对全量历史 KVCache 重新遍历执行一次注意力计算；若采用远端直读，跨节点总线的固定访问时延与有限网络带宽将被生成的输出 Token 数量成倍放大；
- **Copy-to-HBM 路径**：以一次性 DMA 搬运成本换取了 Decode 阶段本地 HBM 的极致读带宽，但会占用本地显存容量并略微增加初始的首字生成延迟 (TTFT)；
- **物理适用边界**：两条路径的优劣分界必须基于相同工作负载、相同硬件设备、相同数据规模及真实的底层硬件事件进行量化测算，文档中的示意数值仅用于交代计算量纲。

### 0.3 为什么需要 ViewGuard 视图守卫机制

Direct-View 远端直读的物理基地址与生命周期强依赖于远端物理节点的显存对象。一旦远端节点发生物理宕机、进程异常崩溃、显存映射被主动撤销、物理通信链路中断或租约过期，本地加速器在执行跨节点内存读取时将直接触发物理总线超时或操作系统同步硬件异常 (SIGBUS)。

生产级环境下的 ViewGuard 必须构筑以下完整的软硬件协同保护链路：

1. **前置微秒级校验**：在发起访存前严格校验租约有效位、对象唯一标识、物理地址空间范围及到期时间戳；
2. **请求与队列绑定**：在访问时间窗口内，将租约与发起线程、请求上下文及加速器执行 Stream 严格关联；
3. **故障主动隔离**：在捕获到远端故障时瞬间将租约标记为失效，坚决阻断后续请求继续消费；
4. **信号安全捕获**：在操作系统内核层精准捕获并隔离针对失效远端显存访问触发的 SIGBUS 异常；
5. **硬件队列重置**：安全中止并重置挂起的加速器硬件队列，将受影响的请求无缝回退至 Copy-to-HBM 或本地直接重算；
6. **故障审计追踪**：完整持久化记录故障发生时的物理地址、时间戳、硬件错误码、回退路径及进程健康状态。

这是一条跨越 CPU、加速卡驱动及远端物理节点的完整故障恢复链。仅仅在应用层返回 `false` 或输出一行日志，绝不能证明进程不会异常崩溃，亦无法保证推理业务的平稳连续。

### 0.4 当前配套工程能够证明什么，不能证明什么

| 验证子项 | 当前受控源码能够完成的执行动作 | 当前受控源码尚不能证明的内容 | 默认证据等级 |
|---|---|---|---|
| View-vs-Copy 成本模型 | 针对固定的读取次数数组，基于纯数学公式计算两条路径的累加耗时并输出较优路径 | 远端总线直读、本地 DMA 搬运、本地 HBM 实测读耗时以及实际业务 TTFT/TPOT | `DEMO / W0` |
| 租约创建与基础校验 | 记录对象 ID、远端地址、字节长度、到期时间并设置原子有效位；支持主动撤销租约 | 远端硬件地址映射、显存访问权限控制、高并发引用安全以及故障注水期间的拦截 | `DEMO / W0` |
| 故障回退接口 | 在控制台输出 `DEMO_ONLY` 提示信息，函数固定返回 `false` | SIGBUS 信号捕获、NPU Stream 安全重置、进程存活性保障、本地重算回退及业务连续性 | `NOT-SUPPORTED` |
| 服务模拟评测脚本 | 依据固定公式与随机分布生成 View 与 Copy 模式下的合成 TTFT 与 TPOT 分位数 | 真实大模型推理端点对接、物理远端直读、动态显存占用、真实 Decode 事件及故障安全 | `DEMO / W0` |

---

## 1. 验证目标与交付结论定义

### 1.1 待验证核心命题

1. **命题一：Direct-View 与 Copy-to-HBM 存在物理可测的成本交叉点 ($N_{crit}$)**。在传输 Payload、远端/本地访存带宽及固定建立时延严格冻结的前提下，量化实测不同重读次数下两条路径的端到端耗时，精确测定现场环境下的临界交叉点；
2. **命题二：Decode 活跃生成阶段的远端高频重读不具备净加速收益（证伪命题）**。在真实大模型 Decode 生成请求中，严密对比 View 与 Copy 模式下的单字生成延迟 (TPOT P50/P99)、尾部时延抖动及本地显存水位，明确得出 Decode 阶段是否应确立 Copy-to-HBM 优先策略的科学结论；
3. **命题三：ViewGuard 能够将失效的远端视图安全转化为可控的业务回退**。在租约自然过期、远端进程崩溃、物理节点宕机及通信链路断开等故障注入场景下，系统验证失效访问是否被秒级阻断、异常信号是否被安全捕获、硬件执行队列是否被正确重置、请求是否成功回退以及本地进程是否始终保持稳定存活；
4. **命题四：数据搬运路径必须基于前置动态成本量化决策而非单纯依赖缓存命中状态**。Direct-View、Copy-to-HBM 与本地直接重算的选择，必须由数据块规模、预估重读次数、业务超时 Deadline、链路物理参数、租约有效性及本地显存水位共同动态决断。

### 1.2 交付物与结论边界

每个正式的 `run_id` 必须至少交付以下结构化资产：

1. **《不同重读次数下 View-vs-Copy 成本交叉实测表》**：完整包含分项建立时延、有效带宽、总耗时、重读次数、Payload 尺寸及硬件能力矩阵来源凭证；
2. **《Decode 阶段 View-vs-Copy TTFT/TPOT 严密对照表》**：保留逐请求、逐 Token 的原始采样数据、失败请求统计、显存占用轨迹及多轮重复实测离散度；
3. **《ViewGuard 租约失效与远端故障安全回退测试报告》**：详细记录故障注入动作、硬件信号与设备错误事件、租约状态流转、回退执行路径、端到端恢复耗时及进程存活性状态；
4. **标准证据包**：包含 `manifest.json`、`environment.json`、执行 CLI 命令、代码包版本、配置哈希、原始日志、Profiler 硬件时间线及分析摘要；
5. **分项技术判定结论**：针对性能适用边界与容错安全机制分别独立输出 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE` 结论，严禁因性能达标而违规跳过容错安全判定。

---

## 2. 成本模型、决策树与 ViewGuard 目标设计

### 2.1 View-vs-Copy 成本交叉数学模型

设传输数据量为 $S$，生成阶段历史 KVCache 的重复读取次数为 $N$，远端直读的固定建立开销为 $t_{view\_setup}$，Copy-to-HBM 的一次性 DMA 搬运建立开销为 $t_{copy\_setup}$：

$$
T_{view}(N,S)=t_{view\_setup}+N	imes(t_{remote\_fixed}+rac{S}{BW_{remote}})
$$

$$
T_{copy}(N,S)=t_{copy\_setup}+rac{S}{BW_{copy}}+N	imes(rac{S}{BW_{local\_hbm}})
$$

当两条路径的端到端总耗时严格相等时，即可解得临界读取次数 $N_{crit}$：

$$
N_{crit} = rac{t_{copy\_setup} - t_{view\_setup} + rac{S}{BW_{copy}}}{t_{remote\_fixed} + rac{S}{BW_{remote}} - rac{S}{BW_{local\_hbm}}}
$$

正式计算必须严格采用 PVT-01 测得的硬件能力矩阵参数，并完整标注物理量纲、测量误差及有效期；文档中的任何示例数字仅用于展示逻辑关系，不可直接作为工程验收指标。

### 2.2 微秒级动态选路决策逻辑

Direct-View 远端直读绝非“只要缓存命中就无脑采用”。调度引擎应严格遵循以下前置动态决策逻辑：

```text
请求到达
   └─► 本地显存是否已有可消费副本？──是──► 走本地 HBM 直接复用（零拷贝快路径）
                │否
                ▼
        是否属于 Decode 阶段高频重读场景？──是──► 优先采用 Copy-to-HBM 或本地直接重算
                │否
                ▼
        读取次数、Payload 尺寸、业务 Deadline 与硬件能力矩阵是否支持 Direct-View？
                │否                                   │是
                ▼                                     ▼
        量化对比 Copy 与本地重算开销          量化对比 View 与 Copy/重算的综合成本
                │                                     │
                └──────────────────► 动态选择具有明确净加速收益且租约有效的最优路径
```

在执行决策判定时，必须同时满足以下物理约束：

- Direct-View 的租约有效剩余时间必须完全覆盖整个访问的时间窗口；
- 远端显存对象、物理地址范围、数据布局哈希及访问权限必须严格一致；
- 采用 Copy-to-HBM 路径时，本地显存池必须具备足够的连续空间并获取到真实的 DMA 完成事件；
- 当数据拉取与加载总开销超过本地直接重算耗时时，必须主动回退至本地重算；
- 任何选定路径均严禁超出业务请求的超时 Deadline，且不得破坏前台在线推理的 TPOT 尾部稳定性。

### 2.3 ViewLease 当前实现与目标扩展

当前受控源码 `view_guard.h` 中定义的结构体仅包含基础字段：

```cpp
struct ViewLease {
    uint64_t object_id;
    uint64_t remote_addr;
    uint64_t size_bytes;
    std::chrono::steady_clock::time_point expire_time;
    std::atomic<bool> is_valid;
};
```

面向跨节点生产级协同的目标协议结构设计如下：

```cpp
struct alignas(64) ViewLeaseDescriptor {
    uint64_t lease_id;
    uint64_t object_id;
    uint64_t remote_va_or_handle;
    uint32_t payload_bytes;
    uint32_t layout_version;
    uint64_t visibility_epoch;
    uint64_t expire_timestamp_ns;
    uint32_t owner_node_id;
    uint32_t permissions;
    uint32_t active_readers;
    uint32_t revoke_reason;
    uint8_t  reserved[16];
};
static_assert(sizeof(ViewLeaseDescriptor) == 64);
```

上述结构体为生产级协议的设计标准。在正式实现中必须补齐物理地址范围、布局版本校验、可见性 Epoch、并发原子引用计数及撤销原因枚举，并对跨进程共享内存访问提供定长 64B POD 保证。

### 2.4 ViewGuard 故障安全恢复目标链路

生产级 ViewGuard 必须完整跑通以下 7 步软硬件安全闭环：

```text
步骤 1：访问前微秒级校验租约有效性与过期时间戳
               ↓
步骤 2：进入 Direct-View 临界区，记录活跃请求与加速器 Stream 句柄
               ↓
步骤 3：模拟注入远端物理宕机、进程崩溃或租约主动撤销
               ↓
步骤 4：操作系统与驱动层捕获 SIGBUS 异常或硬件总线错误，精准提取故障地址
               ↓
步骤 5：安全中止 (Abort) 或物理隔离当前挂起的加速器硬件队列
               ↓
步骤 6：瞬间撤销本地租约，原子阻断后续任何失效对象的访存操作
               ↓
步骤 7：将受影响的请求安全回退至 Copy-to-HBM 或本地直接重算，输出完整故障凭证
```

在底层实现中，`sigaction`、`sigsetjmp/siglongjmp` 以及 CANN/NPU 的 `aclrtStreamAbort` 是核心的技术手段。在编写信号处理函数 (Signal Handler) 时，必须严格遵守异步信号安全 (Async-Signal-Safe) 规范，严禁在 Handler 中调用未经验证的动态内存分配、互斥锁或复杂 I/O 操作；硬件队列的安全重置能力亦必须由原厂驱动提供底层支持。

---

## 3. 实验方案与测试矩阵设计

### 3.1 三组子实验矩阵

| 验证子项 | 正式测试输入参数 | 核心观测指标与输出 | 当前源码支持状态分析 |
|---|---|---|---|
| View-vs-Copy 成本交叉 | Payload 尺寸：16MB、64MB 及扩展档位；重读次数：1、2、4、8、16、32、64、128、256 | View 总耗时、Copy 总耗时、分项建立成本、临界交叉点 $N_{crit}$、显存占用量 | 支持基于固定 `read_counts` 与命令行参数的公式推导；无真实硬件读写 |
| Decode 在线 A/B 对照 | View 模式与 Copy 模式、Prompt 上下文长度、输出 Token 数、请求并发度及重复轮次 | 首字延迟 TTFT、单字生成耗时 TPOT (P50/P95/P99)、失败请求率、尾部抖动、显存水位轨迹 | 仅提供合成 JSON 数据生成脚本；尚未实际对接在线推理服务引擎 |
| ViewGuard 故障注入 | 租约自然超时、远端进程异常崩溃、物理节点宕机、显存映射撤销；每类至少重复 3 轮 | 硬件信号/设备错误事件、租约状态流转、Stream 处理结果、回退执行路径、恢复耗时、进程存活性 | 尚未提供可执行的故障测试工具，回退接口固定返回失败 |

### 3.2 成本模型输入矩阵

| 测试维度 | 正式实施计划标准 | 当前工程实际支持情况 |
|---|---|---|
| Payload 尺寸 | 16MB、64MB 及现场可用等价档位 | `--payload-mb` 支持传入单值 |
| 重读次数序列 | 1、2、4、8、16、32、64、128、256 | `read_counts` 数组硬编码在 C++ 源码中，CLI 暂不支持动态配置 |
| 远端直读耗时 | 来源于真实的 Direct-View 完成事件或 PVT-01 硬件能力矩阵 | `--remote-read-ms` 仅作为公式推导的输入参数 |
| Copy 搬运耗时 | 来源于真实的 DMA 硬件完成事件 | `--dma-copy-ms` 仅作为公式推导的输入参数 |
| 本地 HBM 读耗时 | 来源于真实的加速器本地访存实测事件 | `--local-read-ms` 仅作为公式推导的输入参数 |

### 3.3 环境与证据矩阵

| 运行环境级别 | 验证核心目的 | 最低前置条件 | 允许产出的证据结论 |
|---|---|---|---|
| W0 公式/合成 | 验证 CLI 命令、成本模型计算逻辑、租约数据结构及状态流转的执行闭环 | C++ 编译器、Python 环境 | 仅可产出 `DEMO` 级别的工作流有效性结论 |
| W1 局部设备实验 | 在单机环境下验证 View 与 Copy 局部路径、内存租约及基础异常处理 | 具备可用显存、DMA 引擎、硬件事件及故障注入控制权限 | 可产出绑定特定硬件设备的 `LAB` 局部结论 |
| W2 真实跨节点实验 | 验证跨节点远端直读适用边界、Decode 性能劣化证伪及生产级 ViewGuard 容错 | 具备跨节点高速互联、真实在线推理服务、底层硬件事件及故障注水权限 | 满足全量证据闭环后，可产出 `MEASURED` 生产级结论 |

### 3.4 公平 A/B 对照与安全隔离要求

在开展性能 A/B 对照测试时，必须严格保持模型权重、Prompt 长度、输出 Token 数量、硬件设备、互联链路、请求并发率、资源配额、预热策略及统计口径的高度一致，测试中仅允许变更数据访问路径策略。故障注入测试必须在严格物理隔离的测试集群中开展，详细记录注入时间点与受影响对象，严禁使用生产在线流量进行未经授权的故障测试。性能适用边界与容错安全门限必须分别独立判定。

---

## 4. 测试工具、当前实现边界与正式扩展要求

### 4.1 当前受控工程目录与运行方式

```text
原型验证代码/PVT-03/
├── Makefile
├── view_vs_copy_bench.cc       # 基于命令行输入耗时推导交叉点与较优路径的微基准程序
├── view_guard.h                # 视图租约结构体与 ViewGuard 守卫类声明
├── view_guard.cc               # 租约创建、有效性校验与回退接口占位实现
└── benchmark_serving_view.py   # 生成包含随机扰动的 View 与 Copy 模式合成性能评估脚本
```

当前可复现的 W0 构建与运行命令为：

```bash
cd ./原型验证代码/PVT-03
make clean
make
./view_vs_copy_bench --payload-mb 64     --dma-copy-ms 3.20     --local-read-ms 0.06     --remote-read-ms 0.85     --evidence-level DEMO     --out res_view_vs_copy_demo.csv
python3 ./benchmark_serving_view.py     --mode view --prompt-len 32768 --decode-tokens 256     --seed 42 --output res_serving_view_demo.json
```

特别说明：上述命令中的耗时参数与合成服务数据均为 DEMO 输入。当前工程中尚未包含独立的 `view_guard_test` 故障注入测试程序或 `plot_crossover.py` 绘图脚本；服务脚本中 `--mode` 支持的参数为 `view` 与 `copy`，非内部全称。

### 4.2 源码实际行为审计

| 源码文件与核心逻辑 | 源码实际执行行为 | 对实测证据等级的影响分析 |
|---|---|---|
| `view_vs_copy_bench.cc` | 遍历固定的重读次数数组，基于输入的耗时参数计算累加时间并输出 `better_path` | 属于纯数学公式模型，未实际访问远端或本地硬件显存；输出未附带状态字段 |
| 代码内默认耗时参数 | 预置了 3.20ms、0.06ms、0.85ms 等默认输入值；支持通过 CLI 参数覆盖 | 默认结果不可作为现场硬件测量值，必须完整记录输入来源凭证与证据等级 |
| `--evidence-level` 参数 | 接受任意字符串传入，内部未对 DEMO、LAB、MEASURED 进行严格契约校验 | 必须由外部证据审查层进行归一化核验，不可轻信命令行传入的标签 |
| `view_guard.cc::create_lease` | 记录对象 ID、地址、大小、过期时间并将原子有效位 `is_valid` 置为 true | 尚未包含物理地址范围校验、访问权限、可见性 Epoch、引用计数或远端注册 |
| `view_guard.cc::validate_access` | 仅检查原子有效位状态以及当前单调时钟是否超过预设的过期时间 | 未进行对象/地址/长度的多维一致性校验，亦未关联并发撤销与设备完成事件 |
| `handle_remote_crash_fallback` | 在控制台输出 `DEMO_ONLY` 提示日志，函数固定返回 `false` | 尚未集成 SIGBUS 信号 Handler、NPU Stream Abort 或本地重算回退机制 |
| `benchmark_serving_view.py` | 依据固定 TTFT 公式与随机分布生成合成性能数据；输出 `status=DEMO_ONLY` | 属于模拟数据生成工具，无法证明真实环境下的 TTFT/TPOT 表现或远端直读收益 |
| Makefile 编译配置 | 仅采用 `-std=c++17 -pthread` 编译，未链接任何硬件设备或推理框架 SDK | 当前工程无法直接执行真实的底层硬件直达数据通路 |

### 4.3 面向 LAB/MEASURED 的最小工程扩展

在正式进入真实性能适用边界测定与 ViewGuard 容错验证前，必须补齐以下工程支撑能力：

1. **真实物理直读与 DMA 路径打通**：接入远端显存映射读取、Copy-to-HBM DMA 搬运、本地 HBM 访存及硬件完成事件；逐次记录实际 Payload、读取次数及物理路径凭证；
2. **硬件能力矩阵动态读取**：无缝读取由 PVT-01 实测产出的标准化硬件能力矩阵与有效期，严禁使用代码内置的默认时延；
3. **真实推理打流与采样**：将 View 与 Copy 模式作为推理服务引擎的底层配置，实时采集真实的 TTFT、TPOT、请求失败率、显存水位轨迹及逐 Token 访存事件；
4. **租约协议生产级补全**：在 `ViewLease` 中补齐全局 lease_id、对象/布局哈希、访问权限、可见性 Epoch、并发原子引用计数及显式撤销原因；
5. **异常恢复闭环实现**：在隔离环境中实现并严密验证 SIGBUS 信号安全捕获、NPU Stream 异常重置、租约秒级阻断、Copy/重算平稳回退及业务恢复能力；
6. **自动化故障注入工具开发**：实现针对租约自然超时、远端进程崩溃、物理节点宕机及显存映射撤销的可控故障注入工具，完整留存注入时间与硬件故障事件；
7. **全链路原始事件与证据包归档**：每组测试确保至少 3 轮独立重复，完整保存逐请求/逐 Token 原始样本、Profiler 时间线、进程存活性状态、Core Dump 记录、版本清单及网络拓扑；
8. **规范化状态枚举输出**：将脚本内部返回的 `DEMO_ONLY`、回退 `false` 及解析失败映射为公共契约规定的 `GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE` 标准状态，缺失字段规范填报为 `null` 并注明 `invalid_reason`。

---

## 5. 分步执行测试操作规程（SOP）

### 步骤 0：冻结实验身份、能力输入和安全边界

- **操作意图**：明确本轮评测的执行级别（W0/DEMO、W1/LAB 或 W2/MEASURED），确保性能适用测试与高危故障注入实验在物理环境上严格隔离。
- **执行动作**：在配置清单中完整填报 `run_id`、`workload_id`、`package_id`、`baseline_commit`、`config_hash`、`hardware_profile`、`topology_profile`、能力矩阵版本、Payload 尺寸、重读次数序列、输出 Token 数、预热策略、重复轮次及准入门槛；详细列明当前未实现的容错链路。
- **应观察现象**：配置能够清晰界定时延参数来源于现场硬件能力矩阵还是 CLI 手动输入；若现场缺乏真实跨节点远端路径或故障注入工具，应提前登记为 `NOT-SUPPORTED`。

### 步骤 1：构建并运行成本模型 W0

- **操作意图**：验证成本交叉计算工具的 CLI 参数解析、数据字段及数学公式流程，建立后续接入真实硬件能力参数的回归对比样本。
- **执行命令**：

```bash
cd ./原型验证代码/PVT-03
make clean
make
./view_vs_copy_bench --payload-mb 64     --dma-copy-ms 3.20 --local-read-ms 0.06 --remote-read-ms 0.85     --evidence-level DEMO --out res_view_vs_copy_demo.csv > crossover_stdout.txt 2>&1
```

- **应观察现象**：CSV 文件按预设的 `read_counts` 序列输出各点下的 View/Copy 总耗时与 `better_path` 推荐；`evidence_level` 仅反映 CLI 传入值，在证据清单中必须明确归类为 `DEMO`。
- **判定边界**：严禁将代码内置的默认参数或推导得出的 `better_path` 包装为硬件实测成绩；现场真实的 $N_{crit}$ 必须基于实测能力矩阵与底层原始事件重新计算。

### 步骤 2：运行合成 View/Copy 服务流程 DEMO

- **操作意图**：验证 TTFT 与 TPOT 的 JSON Schema 结构与分位数统计输出格式，杜绝将合成数据误判为真实推理服务的实测表现。
- **执行命令**：

```bash
python3 ./benchmark_serving_view.py     --mode view --prompt-len 32768 --decode-tokens 256     --seed 42 --output res_serving_view_demo.json
python3 ./benchmark_serving_view.py     --mode copy --prompt-len 32768 --decode-tokens 256     --seed 42 --output res_serving_copy_demo.json
```

- **应观察现象**：生成的 JSON 文件明确标注 `evidence_level=DEMO` 与 `status=DEMO_ONLY`；TPOT 数据由脚本内置的随机分布生成，随机种子仅用于保证合成流程的可复现性。
- **证据边界**：该脚本并未实际连接大模型推理服务的 `/v1/completions` 端点或真实 KV 路径，不可据此直接关闭 Decode 阶段的直读证伪命题。

### 步骤 3：核对真实 View/Copy A/B 实测（条件步骤）

- **前置条件**：真实远端直读与本地拷贝服务配置、底层硬件完成事件、PVT-01 硬件能力矩阵、大模型及评测数据集均已就绪；A/B 测试仅允许变更数据访问路径策略。
- **操作意图**：在真实 Prompt 上下文、输出长度及请求并发压力下严密对比 TTFT、TPOT、显存占用及请求失败率，从物理第一性原理确证 Decode 阶段是否存在远端高频读取放大。
- **执行动作**：在相同工作负载下分别运行 View 与 Copy 模式；逐次完整记录实际物理路径、租约信息、Payload 尺寸、重读次数、底层硬件事件、逐 Token 耗时统计以及同场次的本地重算基准。
- **应观察现象**：View 与 Copy 的性能差异能够精准与硬件能力矩阵及原始物理事件相互印证；若实际物理路径无法证明或仅有合成服务脚本，必须规范标记为 `NOT-SUPPORTED` 或 `INVALID-EVIDENCE`。

### 步骤 4：验证租约创建、过期和撤销

- **操作意图**：在不涉及硬件故障的前提下，首先验证租约创建、时间窗口过期及主动撤销等访问资格基础逻辑的闭环。
- **执行动作**：创建短周期租约，分别在有效期内、自然过期后以及显式主动撤销后调用 `validate_access`；完整记录时间戳、对象标识、物理地址、长度、原子有效位及返回值。
- **应观察现象**：有效租约正常允许访问，过期或已被撤销的租约被严格拒绝；当前代码仅能证明上述本地判定逻辑，不能证明远端显存映射或底层硬件消费的安全性。

### 步骤 5：执行远端故障与 SIGBUS 回退（条件步骤）

- **前置条件**：已在物理隔离环境中完成真实 View 路径对接、SIGBUS 信号与设备错误安全捕获、加速器 Stream 异常处理、租约秒级阻断及 Copy/重算回退机制的工程实现，并配备完善的恢复验证手段。
- **操作意图**：验证远端节点的突发故障绝不会导致本地大模型推理进程异常崩溃，且失效的远端显存对象绝不会被继续错误消费。
- **执行动作**：分别针对租约超时、远端进程崩溃、物理节点宕机及显存映射撤销执行可控注入；逐次完整记录注入时间、硬件故障信号/设备错误、故障物理地址、Stream 状态、租约状态、回退路径、恢复耗时、请求最终状态及本地进程存活性。
- **应观察现象**：异常信号被安全拦截捕获，失效租约被瞬间阻断，硬件执行队列按原厂驱动规则安全重置，请求平稳回退至有效路径；任一环节缺乏底层原始事件凭证时，一律不得判定为通过。
- **当前代码边界**：`handle_remote_crash_fallback()` 当前固定返回 `false`，且工程中不存在可直接执行的 `view_guard_test`；在未完成工程扩展前，本步骤统一规范记录为 `NOT-SUPPORTED`。

### 步骤 6：生成分项证据包和决策摘要

- **操作意图**：将性能成本交叉、在线业务影响与容错安全证据分开独立归档，彻底杜绝以单一的性能曲线掩盖系统的容错安全缺口。
- **执行动作**：在 `results/PVT-03/<subtest>/<run_id>/` 目录下归档成本输入、硬件能力矩阵、原始 TTFT/TPOT 采样数据、租约流转事件、故障注入日志、进程存活性记录、Profiler 时间线、`manifest.json`、`environment.json` 及分析摘要；每个测试条件确保至少完成 3 轮独立重复测量。
- **应观察现象**：报告中的每个 $N_{crit}$ 交叉点、TPOT 分位数及故障回退结果均能精准索引至底层原始样本；未采集到的字段显式置为 `null` 并注明 `invalid_reason`。

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

字段填写约束：在缺乏真实硬件事件时，`signal_name`、`device_error_code`、`process_alive` 或 `fallback_action` 等字段必须显式填写为 `null`，严禁用 `FALSE` 替代“未实际观测到”。

### 6.3 汇总 CSV 模板

以下为微基准汇总输出字段格式规范（非预置实测成绩）：

```csv
validation_id,run_id,subtest,mode,payload_bytes,read_count,view_total_ms,copy_total_ms,better_path,ttft_p50_ms,ttft_p99_ms,tpot_p50_ms,tpot_p99_ms,lease_valid,fault_type,signal_caught,stream_action,fallback_action,fallback_time_us,process_alive,evidence_level,status,invalid_reason
<PVT-03>,<run_id>,<crossover_or_decode_or_fault>,<view_or_copy>,<bytes>,<N>,<measured_or_null>,<measured_or_null>,<view_or_copy_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<measured_or_null>,<true_or_null>,<fault_or_null>,<true_or_null>,<abort_or_null>,<recompute_or_copy_or_error_or_null>,<measured_or_null>,<true_or_null>,<DEMO_OR_LAB_OR_MEASURED>,<status>,<null_or_reason>
```

### 6.4 证据包目录结构

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

`manifest.json` 至少完整固化硬件能力矩阵版本、代码包版本、基线 Git Commit、配置哈希、大模型结构与工作负载、故障注入授权范围、硬件设备、网络拓扑、执行 CLI 命令、原始文件哈希、证据等级、功能支持范围及分项判定状态。

---

## 7. GO / CONDITIONAL / NO-GO / NOT-SUPPORTED / INVALID-EVIDENCE 判定规则

### 7.1 View-vs-Copy 适用边界判定

- **GO（成本适用边界形成完整闭环且可复现）**：真实远端直读与本地拷贝硬件事件、能力矩阵参数、Payload 尺寸、重读次数及同场次基线完整齐备；临界交叉点 $N_{crit}$ 在至少 3 轮独立重复实测中保持稳定，路径决策推荐与实际测得的总耗时高度自洽，且未引发请求超时、显存耗尽或失败率恶化。
- **CONDITIONAL（场景条件受限）**：Direct-View 仅在极低重读次数、小 Payload 或 Prefill 阶段展现出确定性净收益；Decode 阶段结论严格限定于已测的输出 Token 长度、并发度及网络拓扑。
- **NO-GO（路径未产生净加速收益）**：在合法的实测中，View 或 Copy 模式在其声明的适用场景下性能持续劣于对比路径，或路径切换引入了不可接受的尾部时延抖动、显存开销或失败率上升。
- **NOT-SUPPORTED（功能未支持）**：当前仅具备纯数学公式模型、合成服务脚本，缺乏底层远端/本地硬件设备事件凭证或硬件能力矩阵不可用。

### 7.2 Decode 阶段直读证伪判定

- **GO（完成科学证伪并确立优先策略）**：在真实大模型在线 A/B 实测中，证实 Decode 阶段采用 Direct-View 在运行前冻结的输出长度与并发范围内，其 TPOT 均值与尾部时延显著劣于 Copy-to-HBM 或本地直接重算，且该性能劣化可由底层逐 Token 跨节点远端访存事件完整归因；系统随后在工程调度层面确立 Copy-to-HBM 为该场景的优先推荐路径。
- **CONDITIONAL（边界条件证伪）**：仅在特定 Payload 尺寸、超长输出、特定多租复用模式或特定硬件设备上观察到远端直读性能放大；调度策略严格限定于上述条件。
- **NO-GO（证伪不成立或目标路径表现更差）**：实测未观察到预期的性能劣化，或 Copy-to-HBM 的一次性搬运成本与显存压力导致了更差的端到端业务表现；严禁保留未经实测数据支撑的静态调度策略。

### 7.3 ViewGuard 故障安全判定

- **GO（故障容错恢复链形成完整闭环）**：每类运行前冻结的故障注入测试均产生了真实的底层故障事件；失效租约被瞬间阻断，SIGBUS 信号或设备硬件错误被安全捕获，加速器硬件队列按驱动规范平稳重置，请求成功回退至 Copy-to-HBM 或本地重算有效路径，本地进程保持稳定存活，恢复耗时与数据一致性均具备权威复核凭证。
- **CONDITIONAL（局部故障保护）**：仅在租约自然过期或应用层主动撤销场景下通过验证，尚未覆盖远端节点物理宕机、通信链路断开或底层硬件异常；结论严格限定于已覆盖的故障类型。
- **NO-GO（安全回退机制失效）**：在合法的故障注入下导致本地推理进程异常崩溃、失效的远端显存对象被继续错误消费、加速器硬件队列死锁无法恢复或业务请求未实现安全回退。
- **NOT-SUPPORTED（物理环境未支持）**：现场缺乏 SIGBUS 信号与硬件错误处理实现、故障注入程序或真实的远端直读物理路径。

### 7.4 统一无效证据规则

凡出现以下任一情形，对应子实验一律判定为 `INVALID-EVIDENCE`，严禁给出 `GO` 结论：

- 将 C++ 源码内预置的 0.02ms、0.85ms、3.20ms 等默认值或服务脚本生成的合成数值直接作为实测成绩填报；
- 将命令行传入的 `--evidence-level LAB` 字符串、脚本输出的 `DEMO_ONLY` 或函数返回的 `handle_remote_crash_fallback=false` 误判为真实硬件状态；
- 缺乏底层真实的远端访存、Copy DMA 搬运、本地 HBM 读取、硬件完成中断或逐 Token 采样事件；
- 租约对象 ID、物理地址、长度、数据布局、权限范围、可见性 Epoch 或实测物理路径无法提供权威凭证；
- 缺失故障注入时间戳、硬件信号/设备错误记录、本地进程存活性状态、回退执行结果或原始系统日志；
- 缺失同场次 A/B 对照数据、多轮重复实测记录、代码版本凭证、网络拓扑或硬件能力矩阵来源；
- 关键指标缺失却采用 0 或 `FALSE` 强行补齐，或将未实际观测解释为“未发生系统故障”。

---

## 8. 执行阶段与交付闭环

测试实施划分为三个严密的演进阶段：

| 实施阶段 | 核心攻坚内容 | 阶段必须交付物 | 准出判定条件 |
|---|---|---|---|
| 阶段 A：模型与 W0 流程 | 严密审计成本推导公式、合成服务脚本、租约基础校验逻辑及回退接口占位实现 | 成本公式 CSV、合成服务 JSON、源码审计报告、`DEMO` 级 manifest | 明确区分示意输入与当前未支持特性的技术边界 |
| 阶段 B：真实性能路径 | 接入远端直读与本地拷贝的底层硬件通路及真实在线推理 A/B 实测，确立临界交叉点 $N_{crit}$ 与 TPOT 证据链 | 原始读/拷贝事件、TTFT/TPOT 实测表、显存水位轨迹、多轮重复实测汇总 | 每个性能指标均能精准追溯至底层硬件事件 |
| 阶段 C：故障安全与策略固化 | 攻坚租约瞬间撤销、远端故障可控注入、加速器硬件队列安全重置及业务平稳回退 | 故障注入证据包、安全恢复对照表、动态选路策略报告及分项技术结论 | 性能适用边界与容错安全均通过公共契约核验，未覆盖项清晰单列 |

本验证项的核心价值在于为微秒级动态选路决策引擎（QueryPlan）提供高精度的成本预估模型与租约安全状态输入，并在工程层面证实“Decode 阶段远端直读将引发访存放大、必须优先采用 Copy-to-HBM”的技术边界，同时为分布式显存共享构筑坚不可摧的安全兜底防线。本项严禁用单一的“公式推导更快”代替真实硬件实测，亦不可用“仅捕获了一次信号”代替完整的端到端业务恢复。

---

## 9. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 9.1 工程师与 Agent 的职责边界

- **工程师核心职责**：
  1. 确认现场远端显存映射、加速卡 DMA 引擎、本地 HBM 通路、PVT-01 硬件能力矩阵、大模型推理端点及故障注入测试权限；
  2. 固化 Payload 尺寸、重读次数序列、输出 Token 长度、A/B 对照策略、故障注入类型、重复测试轮次及安全红线；
  3. 实际下发并完整留存 View/Copy 请求数据、租约全生命周期日志、故障注入时间戳、进程存活性记录及回退执行结果；
  4. 严格审定 SIGBUS 信号与底层设备错误事件是否确由真实物理路径触发并安全恢复，确保测试不会对生产环境产生任何非预期干扰；
  5. 对 Direct-View 路径是否具备向特定业务场景开放的生产条件承担最终技术复核与签字把关责任。
- **AI Agent 协同职责**：
  1. 深入研读本方案设计、公共测试契约及四个原型验证源码文件，精准梳理实际支持的 CLI 参数、代码内置默认值、输出状态及当前未实现功能；
  2. 编写硬件能力矩阵解析读取、成本模型复算、TTFT/TPOT 分位数计算、租约流转事件审计及故障注入日志解析工具；
  3. 严格核验 View/Copy 同场次 A/B 实测数据、临界交叉点 $N_{crit}$、缺失字段填报及证据等级规范性；
  4. 严守学术与技术诚信红线，严禁虚构 `view_guard_test`、`plot_crossover.py`、SIGBUS 捕获通过数据或 NPU Stream 恢复结论。

### 9.2 可直接复制给 Coding Agent 的 Prompt 模板

```text
我正在执行 PVT-03：Direct-View 与 Copy-to-HBM 适用边界及 ViewGuard 安全验证。

请先研读以下核心文件：
1. ./提前验证方案设计/验证计划方案设计/04_PVT-03_DirectView与Copy-to-HBM适用边界与ViewGuard验证实施方案设计.md
2. ./提前验证方案设计/验证计划方案设计/Benchmark公共契约与证据分级规范.md
3. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-03/view_vs_copy_bench.cc
4. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-03/view_guard.h
5. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-03/view_guard.cc
6. ./提前验证方案设计/验证计划方案设计/原型验证代码/PVT-03/benchmark_serving_view.py

执行约束与任务要求：
- 首先梳理源码实际支持的 CLI 参数与底层执行行为；确认 view_vs_copy_bench.cc 仅使用 CLI 传入的时延做纯公式计算，benchmark_serving_view.py 仅输出 DEMO 级别的合成 JSON 数据。
- 确认当前 ViewLease 仅包含对象 ID、地址、长度、过期时间与原子有效位；handle_remote_crash_fallback() 固定返回 false，尚未集成 SIGBUS Handler、NPU Stream Abort 或本地重算回退。
- 真实的性能适用结论必须读取 PVT-01 产出的硬件能力矩阵，并完整保留底层远端读、Copy DMA 搬运、本地 HBM 读取、硬件完成中断及逐 Token 采样事件；严禁将示意值填入 MEASURED 字段。
- ViewGuard 容错结论仅在底层故障事件真实发生、失效租约瞬间阻断、硬件队列平稳重置、Copy/重算回退成功、进程存活性及恢复耗时全部可追溯时方可给出。
- 未采集到的字段显式置为 null 并详细注明 invalid_reason；性能适用边界与容错安全门限分别独立输出结论；严禁虚构不存在的 view_guard_test 或 plot_crossover.py 脚本。
- 最终输出：源码能力核验矩阵、实际执行命令清单、View/Copy 成本对比表、Decode A/B 实测字段表、故障注入审计表、未支持特性清单以及下一步最小代码重构建议。
```

### 9.3 常见排错指南

- **测得的临界交叉点 $N_{crit}$ 随代码内置默认参数变动**：此为纯公式模型的预期现象；检查输入时延参数是否源自 PVT-01 硬件能力矩阵实测且对应相同的 Payload 尺寸，严禁将单一默认交叉点包装为固定技术结论。
- **运行服务评测脚本后未捕获到真实的大模型推理日志**：当前脚本并未实际连接推理服务，仅用于生成 DEMO JSON 数据；切勿将其误当成真实 TTFT/TPOT 的实测表现。
- **调用 `validate_access` 在租约过期后依然判定为允许访问**：核对单调时钟计时器、过期时间单位、并发撤销机制及对象/地址空间范围；当前源码实现尚未包含完整的跨节点并发安全协议。
- **调用故障回退接口固定返回 `false`**：此为当前受控工程 `handle_remote_crash_fallback()` 的占位行为，代表回退机制尚未在底层打通，绝不代表“故障已被安全处理”。
- **尝试直接运行 `view_guard_test` 脚本**：当前受控目录中不存在该测试文件；应首先在隔离环境中开发故障注入与原始事件采集工具，再开展安全判定。
- **安装 SIGBUS 信号 Handler 后导致进程产生二次崩溃**：检查 Handler 内部是否调用了非异步信号安全 (Non-Async-Signal-Safe) 的函数、是否存在信号重入问题、是否设置了合法的 `sigsetjmp` 跳转锚点；加速卡硬件执行队列的安全重置必须由原厂驱动提供底层支持。
- **执行 Copy-to-HBM 路径时出现显存不足 (OOM)**：完整记录当时的显存水位轨迹、显存分配失败事件及主动回退至本地重算的执行日志；严禁通过私自缩减 Payload 尺寸来掩盖大模型真实长上下文下的显存瓶颈。
- **跨节点传输时间戳计算出现负数或异常偏差**：优先采用单节点内的单调硬件时钟进行相对耗时计算；若必须进行跨节点时间对齐，需完整记录 PTP 等时钟同步状态及纳秒级误差上限。
- **Direct-View 虽然首字耗时更低但在线业务 TPOT 发生严重劣化**：在技术评审中必须严格区分 Prefill 阶段的一次性 TTFT 收益与 Decode 阶段高频重读的累积成本，严禁单纯依赖首字延迟指标盲目选择数据路径。
