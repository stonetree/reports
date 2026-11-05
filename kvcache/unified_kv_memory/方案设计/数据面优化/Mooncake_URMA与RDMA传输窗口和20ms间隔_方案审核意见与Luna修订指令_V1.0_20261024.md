# Mooncake URMA/RDMA 传输窗口与约 20 ms 间隔：方案审核意见与 GPT-6 Luna 修订指令

**版本：** V1.0  
**审核日期：** 2026-10-24  
**被审文件：** [代码修改、测试验证与打点方案 V1.1](Mooncake_URMA与RDMA传输窗口和20ms间隔_代码修改测试与打点方案.md)  
**审核结论：** **技术排查方向合理，实施方案需修订后复审。当前完整替换代码不宜直接执行，现有打点不足以可靠定位约 20 ms 停顿。**  
**本次工作范围：** 阅读关联讨论最后两轮、核对本地源码及讨论附件、静态审核实现与实验设计，并独立复核并发及生命周期。本次没有修改 Mooncake 源码，没有运行编译、单测、性能或硬件测试。

## 1. 审核结论与依据

### 1.1 应保留的方向

原方案已经正确区分三个问题：单轮可提交窗口、小切片持续吞吐、约 20 ms 停顿。应继续保持这三个问题各自的假设、实验和结论，不能用“增加 Jetty 后吞吐提高”同时证明三者都已解决。

URMA（统一远程内存访问编程接口，用于向 UB（统一总线）设备提交异步数据传输）中的 Jetty 是收发队列对象，JFS 是发送队列，JFC 是完成队列；RDMA（远程直接内存访问，设备直接执行远端内存读写）中的 QP 是收发队列对，CQ 是完成队列。WR 是设备工作请求，slice 是 Mooncake 内部切片。本文的窗口指软件允许尚未回收的 WR 数量及其对应字节量，不等同于物理链路吞吐。

以下内容可保留：

- 默认 64 KiB slice、每队列 256 WR、URMA 单 Jetty 与 RDMA 双 QP，支持 16 MiB/32 MiB **软件容量上限**的推导。
- URMA 从随机选一个 Jetty 改为跳过满队列、轮转使用可用 Jetty，是值得验证的局部优化。
- 共享 JFC 需要有界原子预留，不能以多个 endpoint 各自读取余额代替全局额度保护。
- 使用单调时钟、诊断默认关闭、固定负载、参数分层扫描，以及将 mock（软件模拟）结果与硬件性能分开，均应保留。
- 3 ms 的 PMU（硬件性能计数器）采样不足以解析理论约 0.34 ms 的 UB 发送过程，不能据此判定 burst（端口计数器观察到的一段集中传输期）内链路速率偏低。

但当前方案存在生命周期错误、观测关联错误、硬件边界落实缺口和实验供给不足。这些问题会使实现出错或使结果无法回答原始问题，不能只补一段“实施前注意”后继续照抄代码。

### 1.2 审核证据和版本边界

本次通过 `read_thread` 读取了关联会话最近 10 轮范围内的最后两轮，采用的是用户确认“全部描述符已一次性提交”的后续讨论。更早的“每 20 ms 上层生产一批”“每轮 10 个大 WR”等推测，已经被后续讨论修正，不应重新引入。

关联会话附带的 `urma_endpoint.cpp`、`rdma_endpoint.cpp`、`ub_context.cpp` 均可读取，三个文件与本地对应文件的 SHA-256 完全相同。因此这三个文件的审核与讨论附件保持一致。RDMA 的 `worker_pool.cpp` 则以本地可读文件为依据。

本地 `.git/refs/heads/main` 记录为 `f90ae691f109e49a60920e0c8abbf7e572826d8c`。该值只标识本地引用，**不证明工作区干净，也不证明实验机器使用该版本**。本次 Git 状态命令未成功读取，故另记录关键文件哈希；Luna 后续实施必须重新保存实际工作区差异与构建产物信息。

| 证据 | 本次核对的位置与用途 |
|---|---|
| 被审方案 | V1.1；SHA-256：`9bd39e3611e5bc4b4232d89c0f2c65dd570eb8454487f0538843e486dd4d22ce`；下文行号均指这一快照 |
| URMA endpoint/context | [urma_endpoint.cpp](D:/codes/vllm/Mooncake/mooncake-transfer-engine/src/transport/kunpeng_transport/urma/urma_endpoint.cpp:521)：poll 内部发布成功、队列创建、随机单 Jetty 提交及销毁回收 |
| UB worker | [ub_context.cpp](D:/codes/vllm/Mooncake/mooncake-transfer-engine/src/transport/kunpeng_transport/ub_context.cpp:401)：JFC 与 Jetty 分阶段回收、worker 分工 |
| RDMA worker | [worker_pool.cpp](D:/codes/vllm/Mooncake/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp:98)：多 worker 时 post/poll 已分工；不能假设两者在同一线程 |
| Slice 生命周期 | [transport.h](D:/codes/vllm/Mooncake/mooncake-transfer-engine/include/transport/transport.h:203)、[transport.cpp](D:/codes/vllm/Mooncake/mooncake-transfer-engine/src/transport/transport.cpp:42)：终态发布和 batch 释放 |
| 配置与能力处理 | [urma_endpoint.h](D:/codes/vllm/Mooncake/mooncake-transfer-engine/include/transport/kunpeng_transport/urma/urma_endpoint.h:102)、[ub_context.h](D:/codes/vllm/Mooncake/mooncake-transfer-engine/include/transport/kunpeng_transport/ub_context.h:135)：全局配置、context 容量快照及 endpoint store |
| benchmark | [te_backend.cpp](D:/codes/vllm/Mooncake/mooncake-transfer-engine/benchmark/te_backend.cpp:379)、[main.cpp](D:/codes/vllm/Mooncake/mooncake-transfer-engine/benchmark/main.cpp:137)、[utils.cpp](D:/codes/vllm/Mooncake/mooncake-transfer-engine/benchmark/utils.cpp:20)：单批等待完成、计时范围、预热与参数 |
| 官方接口参考 | [openEuler URMA API Guide](https://docs.openeuler.org/zh/docs/24.03_LTS_SP4/unifiedbus/unifiedbus/urma/URMA%20API%20Guide.ch.html#2413-urma_post_jetty_send_wr)：确认 `bad_wr` 表示首个失败请求，以及设备能力区分对象数量和队列深度；该文档版本不能代替部署端 SDK/provider 的逐版本核对 |

关键源码哈希：

| 文件 | SHA-256 |
|---|---|
| `urma_endpoint.cpp` | `2474b3cd1b22d0dbbce6221ceba7983736558e6d0c31739b5e8f1d519e5497a3` |
| `ub_context.cpp` | `adfaced06bea6d20fbf88727d010cfa365244be3cd66ad0ae9794fefa34351e7` |
| `rdma_endpoint.cpp` | `33a6afc50f610853a1360dcb6d8a6a6753acb5d9ec8cb8619e30e013cdc98809` |
| `worker_pool.cpp` | `c889f1d13d2162e29d4c3ec36fbd3982c261fa523b52f854d3c1be6826a91fa1` |

## 2. 必须先澄清的技术判断

### 2.1 16 MiB/32 MiB 是上限推导，尚不是 burst 的已确认成因

`256 × 65536 B = 16 MiB`，`2 × 256 × 65536 B = 32 MiB`，这是配置允许的容量。实际可提交量还取决于当前深度、JFC/CQ 余额、pending slice 数、各 slice 的实际长度、分片分布及 provider 接受数量。

连续 completion 可以使软件持续补充 WR，一个 PMU 观察区间也可能跨越多次 post。反过来，一次 post 也可能被计数采样拆开。因此不能把“一个 burst”直接等同于“一次 post”或“一个完整窗口”。新增 Jetty 后流量若变为持续流，也可能说明优化有效，不应因 burst 不再线性增长而判为失败。

17.285 MB/35.463 MB 与窗口大小的接近程度是支持假设的线索。要建立因果关系，必须记录真实 accepted WR、payload bytes、额度耗尽与回收，再做受控变化。不同协议计数器多出来的字节不能未经核对就解释成固定协议头。

### 2.2 扩窗口不能代替修复 20 ms 服务间隔

在“持续有待发送数据、窗口受限、有效额度循环约 20 ms”这一简化模型下，16 MiB 和 32 MiB 对应约 0.839 GB/s、1.678 GB/s。它们只是模型估算，不是本次测量结果。

若希望以 50 GB/s 持续发送，而每份额度要等 20 ms 才能循环，需要约 1 GB 在途数据，即约 15,259 个 64 KiB WR。单个默认 4096 项 JFC 对应的 256 MiB，在同一简化条件下也仅约 13.42 GB/s。可见仅增加到 2/4 Jetty，并不必然解决根本限制。

这里的 20 ms 是**待测的软件额度循环/服务间隔**，不能直接叫作网络往返时延。应同时验证“窗口是否不足”和“额度为何长期不能复用”。不得将 50 GB/s 原始速率作为本轮代码验收的必达 payload 目标。

### 2.3 “描述符已提交”仍需按层记录，但不能重回上层供数不足的既定结论

用户确认的应用提交、进入 worker 队列、进入 endpoint pending、provider 接受 WR，是不同事件。用同一批次标识记录这些边界，有助于检查处理链中的实际等待；这不等于否定用户确认，也不意味着应重新优先开发应用批处理。

约 7 MB 工作量、页面参数 16→64、约 30 GB/s 属于用户报告的观察。具体 benchmark、页面参数单位、block/slice 的关系应由原脚本复现，不能把“页面变大”直接写成“底层每个 WR 变成 7 MB”。

## 3. 审核问题清单

P1 表示影响实现正确性或核心归因结论、必须在实施前修复的问题；P2 表示必须补齐的性能、复现或交付条件。本次未把静态风险写成已经发生的硬件故障。

| 编号 | 级别 | 问题 | 要求 |
|---|---|---|---|
| R01 | P1 | post 返回后重新读取已可能回收的 Slice | post 前快照长度，post 后只读本地快照 |
| R02 | P1 | thread-local completion 时间不能跨线程/队列关联 | 建立 context、队列、代次与事件关联 |
| R03 | P1 | worker/provider/两级额度的时间边界不完整 | 补齐阶段与真实额度可用时间 |
| R04 | P1 | 100 ms 共用限频器删除因果链，汇总无最终刷新 | 采集与打印分离，记录完整关联链及丢弃信息 |
| R05 | P1 | 硬件数量、深度、初始化快照与全局配置不一致 | 使用实际生效的 context/queue 容量 |
| R06 | P1 | 部分提交、异常 bad_wr、失效导入对象缺少封闭处理 | 明确成功边界、所有权及额度账本 |
| R07 | P1 | 默认实验只有 8 MiB，无法验证更大窗口 | 加入饱和供给及等总窗口对照 |
| R08 | P1 | 单测未真正制造超额竞争，缺少集成故障注入 | 增加并发争用、早完成、重试及重建用例 |
| R09 | P2 | 默认关闭诊断仍新增较多分配与原子操作 | 降低热路径成本并独立测开销 |
| R10 | P2 | 指标、启动、预热、回退与版本闭环不完整 | 给出可复现命令、结果分类与逐项闭环 |

### R01：诊断代码引入完成后的 Slice 解引用

**位置：** 被审方案第 3.3 节 L567–574、第 3.5 节 L1108–1115；本地 `UrmaContext::poll()` L539–551，以及 RDMA 完成处理。

方案在 `urma_post_jetty_send_wr()`/`ibv_post_send()` 返回后，遍历 `slice_list[...] ->length` 计算字节数。WR 被设备接受后，另一 worker 可以已经 poll 到完成并执行 `markSuccess()`；应用线程随后可以释放 batch 或复用相关对象。endpoint 写锁不保护完成发布和应用回收。URMA 源码已明确要求所有必要 Slice 读取必须发生在 `markSuccess()` 之前。

**修改要求：**

1. 构造 WR 时，在 provider 调用前建立当前提交链的长度前缀和或长度快照。
2. post 返回后，以经过验证的成功前缀数量计算 accepted bytes，不能再读取成功 Slice。可以在所有权仍明确的失败后缀上执行既有失败处理。
3. completion 侧同样在终态发布前提取 endpoint/queue/generation、长度等诊断字段；不能在外层为了补日志而重新读取已成功 Slice。
4. 同时修复 UB 和 RDMA，不以单 worker 的偶然安全掩盖多 worker 路径。

**验证要求：** mock provider 接受后、post 返回前，允许另一个线程完成全部 WR；应用立即释放/复用 batch，再让 post 返回。开启诊断运行该用例，检查字节统计和对象访问；可用时运行 ASan（地址错误检测工具）及并发压力测试。普通吞吐跑通不能替代这个竞态用例。

### R02：completion-to-post 的线程和队列关联错误

**位置：** 第 3.4 节 `State::last_completion_ns`、`recordPoll()`、`recordPost()`，约 L707–709、L838–840、L890。

该时间是线程私有的单个变量。UB 的 endpoint 由 shard 分工 post，JFC 由 `jfc_index` 分工 poll；二者不保证在同一 worker。当前 RDMA 多 worker 模式更已明确分为 post worker 和 poll worker。post 线程可能根本看不到相关 completion。即使碰巧同线程，其他 endpoint/CQ 的 completion 也会覆盖该时间。

另外，post 事件的 `queue` 表示 Jetty/QP 下标，poll 事件的 `queue` 表示 JFC/CQ 下标，不能按同名整数连接。当前 `post_progress` 甚至没有输出传入的 `endpoint_id`，仅在秒级汇总输出，不能恢复逐次链路。

**修改要求：**

- 为运行、context、endpoint、Jetty/QP、JFC/CQ 分配稳定标识，并包含对象重建代次；指针复用后不得连接到旧事件。
- 输出真实 OS TID、worker 角色、提交链序号；发布 Jetty/QP 到 JFC/CQ 的映射。
- 优先采用带标识的事件离线关联；若使用共享进展时间，明确同步语义与对应对象，不能仅将一个变量改成全局 atomic。
- 明确定义测量的是“相关队列额度释放→下一次使用该额度的 post”，不能把某个无关完成到任意 post 的间隔命名为 refill 延迟。
- 无法关联时输出 `unavailable` 或缺失原因。诊断数据不要以 0 伪装成有效结果。

**验证要求：** 两个 endpoint 分别共享/不共享完成队列，交换 post/poll worker，对其中一个 endpoint 注入延迟。该延迟必须只反映在对应关联链上，不被另一条流持续的 completion 覆盖。

### R03：现有时间边界会漏掉或错误归类停顿

**位置：** 第 3.4、3.5 节 worker/poll/release 打点，以及第 5 节归因表。

有三项不同缺口：

1. `worker_gap` 测的是上一轮 `workerLoopEnd()` 到下一轮 begin。线程若在队列搬运、endpoint 连接、锁等待、post/poll 函数内部暂停 20 ms，恢复后执行 end 会重置时间，下一轮 gap 接近零。因此“gap 小”不能排除调度或软件停顿。
2. UB 外层 `context_.poll()` 包含 provider poll、逐 completion 解析、失败日志和 `markSuccess()`。外层耗时不能标成纯 provider poll 耗时。
3. UB 每个 JFC 后递减 JFC outstanding，但所有 JFC 处理完才统一递减 Jetty depth。RDMA 也存在 CQ 与 QP 分阶段回收。方案只记录前一种额度，可能显示“已归还”，但 endpoint 仍受 Jetty/QP 深度限制。

**修改要求：** 至少补齐下列时间边界，并保留现有生命周期保护：

| 阶段 | 必须记录的边界 |
|---|---|
| worker | loop begin/end、begin-to-begin、post phase begin/end、poll phase begin/end；正常 park 单独标识 |
| 队列和 endpoint | 入队/出队，endpoint lock 等待，连接与 redispatch 耗时；只对有 pending 的路径判断 refill |
| provider post | 真正 provider API 进入/返回；WR 构造耗时另计 |
| provider poll | `urma_poll_jfc()`/底层 CQ poll 进入/返回及原始返回数 |
| completion 处理 | provider 返回到解析/终态发布的阶段边界；元数据在发布前快照 |
| 额度 | JFC/CQ 释放与 Jetty/QP 释放分别记录，写明对象与释放数量 |
| 下一轮 post | 相关额度已可用且 pending 非空后，实际 provider post 的时间 |

“连续空 poll”应仅在**该队列有在途请求**时建立区间；空闲无业务的时间不能累计成完成等待。并发计数 before/after 只能视为快照，不能强制要求 `after-before=本次post数`，因为另一个线程可能同时归还额度。

**验证要求：** 在队列处理、锁等待、provider 内、provider 返回后、JFC 释放后但 Jetty 释放前分别注入 20 ms 延迟，逐项核对分类。调度结论还须有 `perf sched`/off-CPU（线程未在 CPU 上运行的时间）证据；软件阶段耗时本身不能证明 CPU 被抢占。

### R04：日志限频破坏因果链，无法满足汇总验收

**位置：** 第 3.4 节 `shouldLogEvent()`、`recordCreditRelease()`、`recordPost()`。

一个线程所有慢事件共享 100 ms 限频器。一次约 20 ms 停顿恢复后，若先输出 worker gap，紧随其后的 poll、release、post 事件均可能被抑制。每周期都由同一种先发生的事件抢占额度，会产生类别偏置。因此“没有 credit_release 日志”不能说明额度及时回收。

此外，post 汇总仅在后续一次 post 跨过 1 s 时输出：短请求、末尾不足 1 s 的数据、post 停止后尚有在途请求的区间均可能没有最终汇总。现有 helper 没有在限频前保存所有慢事件计数和最大值，却要求最终报告提供这些值。

**修改要求：**

1. 采集与日志打印分离。默认诊断关闭；诊断开启时，用预分配有界缓冲或完整异常区间快照保存关联事件，再异步/离线导出。
2. 限频前更新完整计数、最大值和统计分母；缓冲溢出必须记录丢弃量、时间范围和事件类型。丢失关键链的数据标为不能归因。
3. 支持显式结束/worker 退出时刷新，区分正常退出、异常中止和日志不完整。
4. 延迟分布来自完整原始事件或明确的无偏采样；不能从阈值筛选后的日志计算总体 P99，也不能用调度 trace 代替缺失的 completion 时间。
5. 保留原方案低开销目标，但不要把“日志每秒少于数十行”作为观测完整性的替代标准。

**验证要求：** 连续产生多个 20 ms 周期，每个周期均含 gap、poll、两级 release 和 post。核对完整链、计数、最大值、丢弃标识；再覆盖运行不足 1 s 和末尾不足 1 s 的汇总。

### R05：硬件边界与初始化的实际生效值尚未闭环

**位置：** 第 3.2 节 L195–249 的 `updateUrmaGlobalConfig()`；源码 `UrmaContext::construct()` L64–74、L97–140；`UrmaEndpoint::construct()` L620 起；`UbContext` 初始化路径。

把常量 2048 称为“硬件上限受控”不成立。2048 是源码请求创建的 JFS 深度，不是本设备查询结果。官方设备能力区分队列数量与深度；当前方案只检查 `max_jetty/max_jfc`，没有处理 `max_jfs_depth/max_jfc_depth` 等限制。[URMA 官方能力定义](https://docs.openeuler.org/zh/docs/24.03_LTS_SP4/unifiedbus/unifiedbus/urma/URMA%20API%20Guide.ch.html)

还有一个确定的落地问题：当前 `construct()` 在调用 `openDevice()` **之前**已经复制 `num_jfc_list`、`num_jfces`、`max_jfc_e`。在 `openDevice()` 中改全局配置，不能修正这些局部快照；后面仍可能按旧值创建。`max_endpoints_` 和 endpoint store 也有独立容量，不能假定写全局字段就已生效。

**修改要求：**

- 区分 requested、device capability、created/effective 三类数值。发送深度至少受软件请求、当前创建策略、设备支持深度共同限制；若 provider 创建时调整了值，以 API 能确认的实际值为准。
- 查询能力后再派生创建参数。核对 JFS/JFC/JFR 深度、SGE（散列表项，用于描述内存区间）数量及本轮 slice 长度边界；具体字段以部署 SDK 为准。
- 队列创建和共享额度预留必须使用同一 context/queue 的 effective capacity，不能一处按实际创建容量、一处按可变 `globalConfig().max_jfc_e`。
- 当前代码同时创建 send JFC、recv JFC 等对象，要按该 SDK 对设备资源数量的定义核算总数，保留其他 context/进程竞争的可能；设备 max 值不是当前可用余额，也不保证创建必然成功。
- 处理多个设备能力不一致的初始化顺序。推荐将环境配置视为请求值，再派生各 context 不可变有效配置，避免较弱设备改写全局后污染其他设备或 RDMA。
- 能力不足时明确采用拒绝还是钳制。若两端钳制后 Jetty 数不同，握手须明确失败，不得静默改变映射。
- 补充第 N 个对象创建失败的清理与再次初始化验证。原实现已有的清理/回收缺口应标记为既有风险，不能宣称已由本次配置代码解决。

**验证要求：** 注入设备 JFS depth 小于 2048、JFC depth 小于 4096、对象数量小于请求、两设备能力不同、第 N 次创建失败等情况；断言传入 provider 的参数、最终对象数量、额度上限与日志完全一致。

### R06：部分提交与错误路径必须形成可核验的所有权账本

**位置：** 第 3.3 节 `imported_jetty_map_` 查询、JFC 预留、`bad_wr` 遍历；第 3.5 节 RDMA 失败边界处理。

URMA 方案在导入 Jetty 不存在时只报错，再将 `wr.tjetty=nullptr` 交给 provider。这不是完整的错误处理。非零返回且 `bad_wr==nullptr` 时，当前 URMA 代码还会把所有 WR 计为成功提交并从 pending 删除，额度可能永远不归还。此类返回是否能由实际 provider 产生尚待核实，但方案不能把异常边界自动解释为成功。

官方文档说明 `bad_wr` 指首个失败请求；具体版本对成功前缀、失败后缀、参数校验错误及异常输出的保证，仍应核对部署 provider。不能仅靠 `bad_wr - wr_list.data()` 假定任意返回指针都合法。[URMA post 接口](https://docs.openeuler.org/zh/docs/24.03_LTS_SP4/unifiedbus/unifiedbus/urma/URMA%20API%20Guide.ch.html#2413-urma_post_jetty_send_wr)

**修改要求：**

1. 在预留前验证导入映射和连接对象；失败时进入已定义的连接恢复/失败路径，禁止向 provider 提交空目标。
2. 明确返回码与失败指针合约。解析指针前，可与本次链中合法 WR 地址逐项匹配；不能先对不可信指针做减法再检查范围。
3. 在已确认前缀成功、后缀未提交的合约下，设尝试数 A、接受数 K：仅回退 A−K 的预留与 Jetty 深度，K 由 completion 归还；只有失败后缀进入重试。未尝试的后续 Jetty 工作保留 pending。
4. 非零返回且边界未知时，进入显式异常状态，记录并隔离该 endpoint 的不确定在途工作，按 provider 合约恢复、排空或结束请求；不能盲目重试全部请求，也不能把全部视为成功。
5. 定义连接 reset、flush、teardown 与晚到 completion 的回收责任，防止同一份额度被 teardown 和 poll 各归还一次。保留既有恢复路径，必要修改须有专门用例。
6. endpoint 返回错误不能被 worker 静默忽略；要么初始化保证不可达，要么调用方消费错误并使相关 slice 最终退出。`ERR_ENDPOINT` 返回本身并不构成闭环。

**验证要求：** 首 WR 失败、中间失败、末 WR 失败、全成功、异常空/非法失败指针、导入映射缺失、连接重建与晚到 completion。分别核对 accepted、pending、retry、terminal 与额度；不通过普通失败次数汇总替代逐请求所有权检查。

### R07：实验供给不足，会把窗口假设错误证伪

**位置：** 第 4.3、4.4、7.1 节，尤其固定命令的 `64 KiB × batch 128`。

该负载每批只有 8 MiB。在 classic backend 中，一个线程提交一批、等待整批完成、释放后才进入下一批；它最多只产生约 128 个完整 64 KiB slice，无法填满单 Jetty 默认 256 WR，更无法检验 512/1024/2048 WR 或多个 Jetty 的容量。原方案 1 MiB 等字节对照更小，适合分析请求固定成本，不适合验证窗口。

**修改要求：** 保留 8 MiB 小负载作为用户症状的对照，另建下列实验族：

| 实验族 | 固定内容 | 变化项与判定对象 |
|---|---|---|
| 小批复现 | 原始单线程脚本、8 MiB 或实际业务批次 | 验证原症状，不能用其不增长来否定窗口 |
| 单 endpoint 饱和 | 同一目标、同一方向、64 KiB slice、足够 pending | 扫 WR 深度和 Jetty 数，观察真实额度是否打满 |
| 等总窗口 | 如 1×512、2×256、4×128 WR，JFC 不成为更小上限 | 区分总在途容量与多队列并行收益 |
| 等 pending bytes | 64 KiB×4096、256 KiB×1024、1 MiB×256，均为 256 MiB，内部 slice 固定 64 KiB | 区分应用 request 数与批次固定成本；此处不是保证实际 inflight 字节相同 |
| slice 扫描 | 应用 block 足够大且固定总字节 | 扫内部 slice；若 block 只有 64 KiB，增大 slice 配置不会自动产生更大的 WR |
| 多 endpoint 竞争 | 共享一个 JFC，逐步增加 endpoint 数 | 验证总额度、公平性与饥饿，不把总吞吐掩盖的单流停滞写成通过 |
| 原业务回归 | 同一模型、同一 KVCache（注意力键值缓存，用于复用历史注意力计算状态）数据与提交轨迹 | 验证实际 20 ms 现象及业务传输耗时 |

饱和族应有足够数据覆盖有效聚合窗口及后续补充。例如 4×1024 WR、64 KiB、单 endpoint、JFC 允许时，窗口为 256 MiB，可先以 512 MiB pending 作为候选；若扩到 4×2048，须重新按实际 JFC/Jetty 限额计算。总 buffer、请求地址、注册区域和两端内存必须足够，不能机械扩大 batch 后越界。

实验中应直接确认该 endpoint 的 pending 非空、相关队列到达限额，再解释结果。若窗口没打满，记录原因，不把整批 submit bytes 误作实际在途窗口。

**验证要求：** 窗口实验出现无增长时，先证明供给充分、参数确实生效、目标 transport 已被使用、共享完成队列未先成为上限，才能讨论该假设是否被证伪。增长关系是条件性预测；不要求 PMU burst 必须严格按配置线性增长。

### R08：并发预留单测没有超额请求，集成风险缺少可执行覆盖

**位置：** 第 3.1、3.2 节 `SharedJfcReservationIsBoundedAcrossThreads`，第 4.1、4.2 节。

8 个线程各申请 512 次、每次 1，总请求恰好 4096；即使错误实现只是无条件原子加，也可能通过此用例。它没有验证容量已满后的拒绝、最后一段部分授予、释放和再预留竞争，更没有验证真实 post/poll 账本。

配置测试只覆盖环境变量，没有验证“设备能力钳制后创建参数”。方案的功能检查列表也没有给出故障注入实现与测试入口，不能用“运行 ub_transport_test”一句话宣称全覆盖。

**修改要求：**

- 竞争申请量显著超过容量，例如总请求至少 8192；用屏障同步起跑，检查累计授予不超过容量、满后授予为零。
- 预置 outstanding=4090，请求 16，断言仅授予 6；释放一部分后可以继续申请。增加多线程不同申请量、申请/释放交错与高水位监测。
- 无需设备的 planner/额度 helper 测试尽量只依赖必要库；不要因 `USE_UB` 或整个 transfer_engine 链接要求，让纯逻辑测试也必须依赖真实 URMA 环境。
- 为实际 submit/poll 路径加入 provider 注入层或复用现有可控 mock，明确测试二进制、gtest filter、构建条件和 mock 限制。
- 增加 R01–R06 的集成用例；能力测试验证 provider 实收参数，trace 测试验证关联和缺失检测。
- 保留 SDK/真实设备功能测试作为独立层。mock 只证明软件状态和故障路径，不证明真实完成时序、线速或固件行为。

**最低新增覆盖：** 超额竞争、部分授予、释放再申请、快速跨线程完成、部分 post、异常失败边界、某 Jetty 满而其他可用、共享 JFC 多 endpoint、两级额度延迟、重建代次、对象创建中途失败、最终汇总刷新。

### R09：热路径额外成本可能抵消小 slice 优化

**位置：** 第 3.1、3.3 节 planner 与每次 submit 的容器创建，第 3.4 节队列汇总扫描。

URMA 新路径每次 submit 都创建深度快照与 plan；可提交时还按 `max_wr_depth_` 创建三组 WR/SGE 向量。默认关闭诊断仍执行这些工作。配置 2048 WR 但仅需提交少数 slice 时，也可能做较大的初始化。相较原有路径，这是需要测量的新成本，不能以“诊断开关默认关闭”概括为无影响。

**修改要求：**

- planner 和缓冲管理按实际可提交数量工作，避免每轮按最大深度全量清零；可用 endpoint 锁保护的复用缓冲或适当预留容量。
- 不把独立测试 helper 与生产路径重复实现成两套不同分配逻辑；若保留 helper，验证其与真实提交决策一致。
- 诊断关闭时不新增诊断时钟、日志、字节快照、全队列汇总扫描；R01 的安全快照在诊断开启时实现，生产必要元数据另按其用途设计。
- 观察 CAS（比较并交换原子操作）争用和多 endpoint 公平性。容量有界不等于公平，多个 Jetty 共用一个 JFC 也不能突破 JFC 容量。
- 不在本轮顺带修改 completion 策略或拆分 UB post/poll worker。若未来采用选择性完成，必须另行证明计数和错误回收语义。

**验证要求：** 用小 slice、单/多 endpoint，对比旧二进制、仅观测改动、额度修复、轮转优化四组；测诊断关闭时的吞吐、延迟、worker CPU、分配次数/耗时。仅证明日志没有输出不构成开销验证。

### R10：启动、计时、预热、回退和状态报告需要补全

**位置：** 第 4.1、4.4、5、6、7 节。

`tebench --start_num_threads=1` 是 benchmark 发起线程数，不等于 transport worker 数。当前 RDMA 多 worker 已有 post/poll 分工；只写“单线程 UB/RDMA”会产生错误比较。`--backend=classic` 也不自动证明实际走 UB，必须验证运行时 transport 与端口流量。

classic `runSingleTransfer()` 的 timer 在请求构造后启动，`freeBatchID()` 前停止；其单批时间不包括全部 batch 控制成本。benchmark 主循环的总体时间包含更多间隙。两种数值回答不同问题，不能混用。当前源码内建预热约 1 s，方案写“预热至少 10 s”，却未给出实现这 10 s 的方式。

**修改要求：**

1. 为硬件和 mock 分别给出经过目标环境核对的 configure、build、CTest、手动测试命令。`<build-dir>` 模板可以保留，但要列明目标所需构建选项；未验证的选项不得当成确定可用命令。
2. 提供双端完整脚本、实际启用 UB/RDMA 的选择机制、进程名、设备与路由、方向、内存类型、端口及必要服务；保留启动日志证明选择生效。
3. 分别记录应用线程、transport post/poll worker、CPU 核与 NUMA（非一致内存访问域，用于说明 CPU 与内存/设备位置）绑定。独占核对照优先限定目标 worker，不把整个进程所有线程挤到一个核。
4. 明确 `request_interval_us=0` 及其他 pacing/限速配置，核查业务侧等待。此项是排除混杂因素，不是断言存在 20 ms 定时器。
5. 定义并同时报告：完整观测期的成功 payload bytes/时间、单批 submit→完成时延、整批循环时延、硬件计数器吞吐。重试尝试 bytes、accepted bytes、唯一成功 bytes分别统计。
6. 明确 10 s 预热在同一进程如何实现并剔除；可使用诊断/采集阶段划分或专门支持。不能用另起进程跑一次代替当前进程热身。
7. burst 起止、间隔、idle 时长分别定义。采样间隔不足时给出可观测范围，不能强求输出 active bandwidth 和精确 P99。
8. 同机校准 `steady_clock` 与采集时钟；跨主机只有校时误差可量化且满足精度要求时才相减。仅写 PTP/NTP 已启用不足以支持微秒级比较。
9. `MC_NUM_JETTY_PER_EP=1` 只恢复队列数量，不恢复原随机策略、原额度路径或原性能。完整回退应有旧二进制/独立提交可恢复，并保存对照结果。
10. 原方案 3.1、3.2 重复给出了相同 CMake 目标，应只保留一处；删除“config.h 增加常量”等与正文位置不一致的描述。配置解析补 `errno/ERANGE`、空串和边界测试，并明确是否允许空格和加号。

## 4. Luna 应采用的修订与实施顺序

### 4.1 先修订文档，再实施代码

本次用户授权的是方案审核与审核文档交付。以下是后续实施任务说明，不代表本次已授权、已执行或已通过源码变更。

**文档修订：** 在被审文件旁另存 V1.2 或新的日期版本，保留 V1.1。逐条回应 R01–R10，更新开头总览、代码块、测试步骤和验收表，避免正文修复而摘要仍保留旧承诺。

不建议在尚未确认 SDK 和对象所有权时继续提供“完整函数直接替换”的大段代码。可以先以精确改动点、接口、状态不变量、故障用例和最小 diff 说明实现；代码落实后再同步为可核验补丁。

### 4.2 代码提交建议拆分

| 顺序 | 内容 | 进入下一步的条件 |
|---|---|---|
| C0 | 保存旧二进制/源码快照，复现原始业务和小批基线 | 确认实际 transport、窗口配置、worker 分工和原始计数 |
| C1 | 仅观测改动，修复 R01–R04，不改变 Jetty 分配策略 | 早完成用例和事件关联通过；关闭诊断回归可接受 |
| C2 | 有效能力配置与共享 JFC 额度修复，保持单 Jetty 默认 | R05、R06、R08 的边界及故障用例通过 |
| C3 | 可配置 Jetty 数与轮转提交，默认仍为 1 | 满队列跳过、多 endpoint、部分失败、重建用例通过 |
| C4 | 饱和窗口、等总窗口、slice/block/batch 与业务回归 | 数据满足供给和观测条件，再决定是否采用新默认值 |

这些步骤不要求保留已知不安全代码用于性能比较。若 C1 在压力测试中暴露既有竞态，先给出单独修复和说明，再建立新的可比较基线。不能同时调整 UB worker 数、完成策略、slice、业务页面参数和 Jetty 默认数后只报告总收益。

### 4.3 必须写进方案的不变量

- 每份工作在任一时刻有明确归属：软件 pending、已预留待 post、provider 已接受待完成、失败待重试或最终完成；不能遗漏或在两个路径重复拥有。
- 某次尝试 A 个 WR、确认接受 K 个后，仅未接受部分立即回退；已接受部分只由其完成/已证明安全的故障清理归还一次。
- 单个 JFC 的计数包含正在预留尚未交给 provider 的部分，以及已接受但尚未归还的部分；上限采用该队列的有效容量。
- Jetty/QP 深度、JFC/CQ 计数分别有确定的回收责任；不能假定二者每个时刻相等。并发运行中可能处于不同回收阶段。
- 全部请求终止并完成清理后，相关在途与预留计数归零，无重复终态、无负数、无未解释的 pending；单纯读写校验通过不代替这些条件。
- 终态发布后不能再读取 Slice 诊断字段。对象重建后使用新代次，旧完成事件不得污染新队列。
- 诊断开关不改变传输语义。缓冲溢出、缺失事件或无法对齐时钟时，报告定位证据不足。

## 5. 修订后的验证矩阵与判定

### 5.1 最低验证矩阵

| 用例 | 方法 | 必须观察的结果 |
|---|---|---|
| V01 早完成 | post 返回前允许另一线程完成并释放 batch | 无完成后对象解引用，字节数准确 |
| V02 超额争用 | 多线程总申请超容量，混合申请大小 | 高水位不超有效容量；满后拒绝；释放后可复用 |
| V03 部分 post | 首/中/末 WR 失败及异常边界 | 成功前缀不重试，未接受部分不泄漏额度；异常不伪装成功 |
| V04 能力不足 | 设备深度/数量不足与创建中途失败 | 实际创建、有效配置和额度统一；清理后可再初始化 |
| V05 两级额度 | 在 JFC/CQ 释放后、Jetty/QP 释放前延迟 | 能分别看到两次释放，下一次 post 与真实可用额度对应 |
| V06 跨线程关联 | 多 endpoint、不同 post/poll worker、重建 | 不串流、不串代次，相关链可恢复 |
| V07 调度与锁等待 | 在不同 phase 注入延迟，并采集调度数据 | 区分阶段执行、锁等待、off-CPU 与 provider 内停顿 |
| V08 trace 完整性 | 短运行、多个 20 ms 周期、缓冲溢出 | 最终汇总完整；丢弃明确；缺失时拒绝强归因 |
| V09 队列轮转 | 1/2/4 Jetty；某 Jetty 满、其他空 | 使用可用队列，pending 不丢失，轮转可重复验证 |
| V10 饱和窗口 | 足量 pending；WR/Jetty 扫描 | 真实 accepted/outstanding 到达预定边界，或能解释更早上限 |
| V11 等窗口/粒度 | 等总 WR、等 pending bytes、slice 单独变化 | 可区分队列数、容量、request 固定成本与 WR 处理成本 |
| V12 原业务与回退 | 原始页面 16/64、同一提交轨迹、旧/新二进制 | 结果可复现；功能、性能、20 ms 归因分别报告 |

上述为**待实施验证要求**，没有一项在本次审核中被标记为已通过。

### 5.2 硬件实验纪律

每种配置至少保持原方案的 5 次重复，但采用交错或随机顺序运行旧/新配置，避免温度、对端负载和 CPU 状态随时间变化造成假收益。先看分布与误差范围，再决定是否增加重复次数。正确性校验可与纯性能运行分开，二者命令和开关要保留。

每次保存完整命令、两端环境、实际生效配置、源码差异、二进制哈希、SDK/驱动/固件版本、内存类型、CPU/NUMA/设备位置、应用与 worker 数、原始计数和采集时间范围。原始业务、tebench、mock 结果分开组织。

性能改善的项目阈值应在测试前写入。诊断开销也应预先给出可接受范围；若开销大到改变原有 20 ms 波形，应减少记录、缩短捕获或更换采集方式，不能用受扰动运行宣布原问题消失。

### 5.3 20 ms 的修订归因规则

| 已验证观测 | 可以支持的结论 | 仍不能直接推出的结论 |
|---|---|---|
| 有 pending，相关 worker 某阶段停顿，并有 sched trace 的 off-CPU 证据 | 该样本存在调度服务延迟 | 所有 20 ms 都由调度造成 |
| 有在途、worker 持续运行、原始 provider poll 持续返回 0 | 完成尚未在该 poll 路径可见，应继续查 provider/设备/对端与操作进度 | 设备已在 0.34 ms 完成、只是 CQ/JFC 延迟上报 |
| provider 已返回完成，JFC/CQ 与 Jetty/QP 回收阶段出现长延迟 | completion 处理或软件额度回收延迟 | 网络带宽不足 |
| 相关额度已可用、pending 持续非空，下一次 post 很晚 | 队列、锁、连接或其他提交控制路径存在等待 | 无关 endpoint 最近一次 completion 到 post 的时间就是本队列等待 |
| 饱和条件下 accepted 字节窗口随配置变化，平均吞吐随之变化 | 窗口对当前瓶颈有贡献 | 窗口是唯一根因，或 20 ms 已修复 |
| post/completion 与端口数据不一致 | 需要核对 READ/WRITE 方向、采样误差、计数语义、传输选路及操作完成定义 | 单凭这一点判定设备背压或固件故障 |

provider post 返回时间、硬件真实发完时间、completion 被软件看到的时间是三个边界。缺少硬件时间证据时，不能把 post→completion 全部解释成链路传输，也不能把 poll 为空全部解释为完成通知延迟。

### 5.4 最终报告必须独立给出四种状态

1. **代码功能：** PASS / FAIL / NOT RUN / BLOCKED，附用例与原始结果。
2. **窗口假设：** SUPPORTED / NOT SUPPORTED / INCONCLUSIVE；供给或采样不满足时采用 INCONCLUSIVE。
3. **性能目标：** IMPROVED / NO DEMONSTRATED IMPROVEMENT / REGRESSED / NOT MEASURED，引用关闭诊断的重复实验。
4. **20 ms 定位：** LOCALIZED / PARTIALLY LOCALIZED / UNRESOLVED；只定位到“provider 进度路径”时不能写成已经确认某硬件根因。

假设不成立不等于代码失败；代码通过不等于性能改善；扩大窗口后吞吐改善也不等于已经定位 20 ms。没有执行的硬件测试必须保持 NOT RUN/NOT MEASURED。

## 6. 可直接交给 GPT-6 Luna 的完整任务指令

> 请修订《Mooncake URMA/RDMA 传输窗口和约 20 ms 间隔：代码修改、测试验证与打点方案》V1.1，按本审核文档 R01–R10、V01–V12 逐项闭环。先产出 V1.2 或新的日期版本，不覆盖 V1.1；本轮如仅获文档修订授权，不修改 Mooncake 源码。
>
> 先核对本地源码、讨论附件和实际 SDK/provider 版本。保留“窗口容量、小 slice 持续吞吐、20 ms 停顿”三个独立问题，禁止把 16 MiB/32 MiB 推导写成已经确认的 burst 根因。
>
> 必须修复两类 post 后 Slice 读取：所有长度和关联元数据在 provider 调用前或 completion 终态发布前快照。不得依赖 endpoint 写锁保护 Slice 生命周期。
>
> 重做 trace 关联：记录 run/context/endpoint/Jetty 或 QP/JFC 或 CQ/对象代次/提交链序号/TID/worker 角色。删除 thread-local 单时间值支撑跨线程 completion-to-post 的做法。增加 loop、队列、锁、provider、completion 处理、两级额度回收和下一次 post 的阶段边界。采集与输出限频分离，支持最终刷新和丢弃标识；无法关联时明确 unavailable。
>
> 重做能力配置：先查询设备能力，再确定 context/queue 的有效创建值和额度上限。修正 openDevice 之前已经复制配置所造成的旧快照问题，核对 endpoint store、多个设备和双端配置；2048 只能称为当前创建策略的上限，不能未经查询称为设备上限。
>
> 完成错误路径设计：导入对象缺失不得 post 空目标；按部署合约解析部分提交；异常 bad_wr 不自动视为成功；成功、失败、未尝试和重试请求的所有权及额度回收必须闭环。核对 reset/flush/teardown 与晚到完成，避免重复回收。worker 必须消费必要错误。
>
> 重做实验：保留原 8 MiB 小批复现，增加单 endpoint 饱和供给、等总 WR 窗口、多 endpoint 共享 JFC、等 pending bytes 和有效 slice 扫描。不能用未填满窗口的负载证伪窗口假设。把应用线程与 transport worker 数分开，核对实际走 UB/RDMA 的证据。
>
> 补齐超额竞争、部分授予、释放再申请、早完成、部分失败、能力不足、创建中途失败、跨线程关联、两级额度延迟、重建代次和最终汇总的可执行用例。纯逻辑测试不应无必要依赖真实设备；mock 结论不能升级为硬件性能结论。
>
> 给出双端运行脚本、构建条件、同进程 10 s 预热的方法、指标计时范围、端口计数定义、重复与配对规则、性能/观测开销判定阈值、旧二进制完整回退方法。只写已核对过的命令；尚未执行的命令和结果明确标为待验证。
>
> 文档完成后，按 R01–R10 提供“原问题、修订位置、验证用例、执行状态、剩余限制”闭环表。对整份文档做一致性复核，保证执行总览、代码块、测试矩阵和验收标准一致。同步 PROJECT_INDEX.md 与 CHANGELOG.md，临时文件全部放项目 tmp。禁止虚构性能、测试通过、PR 编号或根因结论。

## 7. 修订交付与复审准入

Luna 的交付应包含修订版 Markdown、R01–R10 闭环表、V01–V12 用例映射、实际核对的版本与 SDK 信息。若实施代码，还应附最小补丁、构建与测试日志、原始数据路径及回退方式。文档修改不能用“后续实施再处理”关闭 R01–R08，至少必须将安全设计、精确改动点和验证入口写完整。

当前审核意见是：**保留优化方向，先修复实现与观测设计，再进入代码实施和硬件验证。** 窗口假设和 20 ms 的最终根因，仍以修订后的受控实验为准。
