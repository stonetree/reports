# Mooncake URMA/RDMA 传输窗口和约 20 ms 间隔：代码修改、测试验证与打点方案

- **版本：** V1.2
- **日期：** 2026-10-24
- **源码基线：** D:/codes/vllm/Mooncake，commit f90ae691f109e49a60920e0c8abbf7e572826d8c
- **版本标签：** v0.3.12.post1-224-gf90ae691
- **授权范围：** 本轮仅修订方案文档；没有修改 Mooncake 源码，没有执行构建、单测、性能或硬件实验。

## 0. 执行总览

本方案分别调查三个问题。每项问题都要使用能回答它的负载、指标和判据，不能用一个总吞吐数字同时代替窗口容量、小切片持续吞吐和约 20 ms 停顿的结论。

| 问题 | 本轮方案动作 | 可以支持的结论 | 不能据此推出 |
|---|---|---|---|
| 窗口容量 | 查询实际设备/队列能力；记录有效 WR 数和每个已接受请求的真实长度；用足量 pending 扫描有效窗口 | 当前配置下，软件在途请求或字节上限是否限制当前负载 | 16 MiB/32 MiB 是已确认的端口 burst 根因 |
| 小 slice 持续吞吐 | 固定总字节量，分别比较小/大 slice、应用 block/batch、单/多 endpoint；计量构造、提交、完成及 worker CPU | 小请求的固定软件成本、队列并行或数据通路供给是否限制该配置 | 仅凭大 slice 吞吐较高就证明窗口容量是主因 |
| 约 20 ms 停顿 | 按 run、context、endpoint、队列、代次和提交链关联完整阶段事件；对齐端口原始计数与采集时钟 | 在 pending 非空且证据完整时，停顿落在哪个软件/provider 可观测阶段 | provider 返回、完成可见和硬件实际传输完成是同一时刻；未关联时间可以归因 |

**现状与执行门槛：**

| 项目 | 当前状态 |
|---|---|
| 源码基线 | 静态核对完成；checkout clean，commit 与版本标签见本页标题 |
| 讨论附件 | 对关联会话中的 URMA endpoint、RDMA endpoint、UB context 源文件做 SHA-256 比对，与当前 checkout 对应文件一致 |
| 实际部署 SDK/provider/驱动/固件 | 未知。当前文档环境没有可用的目标设备 SDK/provider 安装证据；部署节点版本和 API 错误返回合约必须在硬件实验前补齐 |
| 修改 Mooncake 源码 | 未修改 |
| 构建、测试、硬件实验 | 均未执行；所有验证用例状态为 NOT RUN |
| 20 ms 根因、窗口根因和性能收益 | 均未确认 |

### 0.1 本轮改动总览

| 编号 | 方案变化 | 主要代码改动点（后续实施） | 验证入口 |
|---|---|---|---|
| C1 | post/completion 生命周期安全 | URMA、RDMA endpoint 的提交记录、completion 处理；以稳定请求 token 代替完成后解引用 Slice | V01、V03 |
| C2 | 可关联的阶段 trace | UB worker、URMA endpoint、RDMA worker pool、两类 endpoint 和有界 trace sink | V05–V08 |
| C3 | 每 context 有效能力配置 | URMA context open/query/create 顺序、URMA endpoint/JFC 创建、endpoint store 和握手参数 | V04、V09 |
| C4 | 额度与错误所有权闭环 | submit reservation、provider 返回解析、completion、retry、reset/flush/teardown | V02–V06 |
| C5 | 低扰动诊断 | 默认关闭；预分配缓冲、采集与导出分离、汇总刷新和丢弃计数 | V07、V08 |
| C6 | 实验供给与脚本边界 | 保留 8 MiB；新增窗口饱和、等总 WR、多 endpoint 共享完成队列、等总字节和 slice 扫描 | V10–V12 |

## 1. 范围、术语与证据分类

本文中的 KVCache 是大模型注意力键值缓存，即自回归生成期间保存的历史 Key/Value 激活张量，避免后续 token 重复计算注意力。URMA 是用户态远程内存访问接口；RDMA 是远程直接内存访问。WR 是设备工作请求。URMA 的 Jetty 是收发队列对象，JFS 是 Jetty 发送队列，JFC 是 Jetty 完成队列，JFCE 是完成事件队列，JFR 是 Jetty 接收队列；RDMA 的 QP 是收发队列对，CQ 是完成队列。Slice 是 Mooncake 内部传输切片；SGE 是描述传输内存区间的散列表项。本文中的 provider 指实现设备传输 API 并连接用户态库与驱动/设备的供应商软件层；HCA 是主机通道适配器；EID 是 URMA 设备端点标识；TID 是操作系统线程 ID；P99 指样本延迟的第 99 百分位；ASan 是地址错误检测工具；CI 在统计判据中指置信区间。

本文采用以下证据标记：

- **源码事实**：仅说明上述 Mooncake checkout 中可读到的实现，不代表目标机器部署版本。
- **讨论报告**：来自用户及关联讨论的观测描述，本轮没有重测。
- **推导**：按明确列出的假设计算所得，不是设备测量。
- **建议/门槛**：后续实施与实验前需要满足的条件，不是已通过结果。
- **未知/待验证**：当前证据不足，不得补成事实。

保留用户确认的提交路径：应用将一批描述符一次性提交至 UB worker 路径。较早“应用每 20 ms 才产一批”的解释已被后续讨论修正，不再作为默认假设。全部描述符已提交仍需按 endpoint、队列和阶段记录实际 pending、provider 接受数与完成数。

### 1.1 16 MiB/32 MiB 只是条件推导

当前 checkout 的配置默认值包括：MC_SLICE_SIZE 为 65536 B，MC_MAX_WR 为 256，RDMA 每 endpoint 两个 QP，URMA 每 endpoint 一个 Jetty。若每个 WR 恰好承载一个完整 64 KiB slice、所有队列都可用、JFC/CQ 额度不更小、provider 创建值未被调整，则：

- URMA 单个 Jetty 的名义软件在途容量：256 × 65536 B = 16 MiB；
- RDMA 两个 QP 的名义软件在途容量：2 × 256 × 65536 B = 32 MiB。

这是以名义队列数、配置深度和默认 slice size 相乘所得的容量上限推导，不是实测 burst 长度，也不等价于端口计数器读数。未观察到超过该数字的 burst，不能确认因果；观察到容量随配置变化也只能说明窗口对该负载有贡献。V1.1 中 17.285 MB/35.463 MB 与约 20 ms 的数字属于讨论报告，本轮未复测，不能作为性能验收结果。约 3 ms 的计数器采样也不能解析约 0.34 ms 量级的假设发送区间。

交付按安全、额度/能力、队列优化、饱和与业务回归分层，不将修改 worker 并发、完成策略、切片大小和业务请求大小混在同一个对照组中。

## 2. 本地源码、附件和 SDK/provider 核对

### 2.1 源码与附件

源码 checkout 为 D:/codes/vllm/Mooncake，分支状态为 main...origin/main，commit 为 f90ae691f109e49a60920e0c8abbf7e572826d8c，git describe 为 v0.3.12.post1-224-gf90ae691，工作区 clean。

关联讨论附件与 checkout 中同名源文件 SHA-256 一致：

| 文件 | SHA-256 |
|---|---|
| mooncake-transfer-engine/src/transport/kunpeng_transport/urma/urma_endpoint.cpp | 2474B3CD1B22D0DBBCE6221CEBA7983736558E6D0C31739B5E8F1D519E5497A3 |
| mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp | 33A6AFC50F610853A1360DCB6D8A6A6753ACB5D9EC8CB8619E30E013CDC98809 |
| mooncake-transfer-engine/src/transport/kunpeng_transport/ub_context.cpp | ADFACED06BEA6D20FBF88727D010CFA365244BE3CD66AD0AE9794FEFA34351E7 |

这只确认讨论附件与本地 checkout 同源，不代表目标机器正在运行该 commit，也不代表文件已在该 checkout 中修改。

### 2.2 当前源码可确认的实现边界

| 主题 | 本 checkout 静态事实 | 修订后要求 |
|---|---|---|
| Slice 生命周期 | URMA completion 在 markSuccess 前仍读取 Slice 字段；markSuccess 可发布批次终态，调用方随后可回收批次。RDMA 的 provider post/completion 也可能并行 | provider 调用前建立不可变提交记录；completion 终态发布前快照所有必要字段；发布后不再读 Slice |
| URMA 额度 | submit 随机挑一个 Jetty；JFS 创建深度字段写死为 2048；JFC outstanding 为共享额度；轮转并不保证多个 Jetty 均被使用 | 配置每队列容量必须来自已查询并成功创建的有效值；2048 仅称当前源码创建策略请求值 |
| URMA 初始化 | UrmaContext::construct 在 openDevice/query 前复制 JFC 数量和深度等全局值；openDevice 查询设备并更新部分全局配置，先前局部快照不会随之变化 | openDevice/query 之后派生不可变 context 有效配置，再创建 JFCE/JFC/JFR/Jetty 和 endpoint store |
| 队列能力 | 源码路径查询的设备能力不构成目标 SDK 所有队列深度、SGE、资源可用余额与创建后生效值的完整证据 | 记录 requested、device capability、create requested、create result/effective 四类数值；字段按目标 SDK/provider 核实 |
| URMA 错误边界 | 导入 Jetty 缺失时当前路径可继续构造空目标；bad_wr 异常边界需按 provider 合约处理 | 预验证导入对象；禁止向 provider post 空目标；未知边界隔离，不默认成功或全量重试 |
| RDMA 错误边界 | ibv_post_send 错误路径使用 bad_wr 计算失败后缀；异常空指针不能自动视为全链成功 | 只有落在本次合法 WR 链内且部署合约确认的 bad_wr 才可解释成功前缀 |
| completion 与 worker | UB 的活跃路径执行 post 后 poll；RDMA post/poll 有多 worker 分工。provider poll 外层还包含 completion 解析及终态发布 | 分开打点 provider 本身、completion 处理和两级额度回收；不得以 worker gap 单独解释调度 |
| transport 选择 | tebench 默认 backend 为 tent。classic 使用 Transfer Engine 自动发现路径；源码日志输出 installTransport。tebench 的 xport_type 属于 tent 路径，列表中没有 ub | 不以 USE_UB 构建选项、--backend=classic 或 --xport_type=rdma 当成实际 URMA 选路证明；运行时日志和设备端口计数须一致 |

### 2.3 SDK/provider 实际版本状态

本轮能确认的是 Mooncake 源码版本，不是部署设备 SDK/provider 版本。当前 checkout 没有 CMakeCache，也没有随仓库提供的 URMA SDK 头文件/库；FindUrma.cmake 使用外部系统安装路径或源码获取逻辑。即使该文件中的源码获取 tag 存在，也不能据此声称目标系统安装了该版本。当前本机可用证据不足以读取两端设备 SDK、URMA provider、驱动和固件版本，因此这些版本统一记为 **UNKNOWN / 硬件实验前必须补录**。

[openEuler URMA API Guide](https://docs.openeuler.org/zh/docs/24.03_LTS_SP4/unifiedbus/unifiedbus/urma/URMA%20API%20Guide.ch.html) 页面描述的是 24.03 LTS SP4 文档版本，不是已安装 SDK/provider 版本。该指南说明 bad_wr 指向第一个失败 WR，URMA post 非零返回代表错误；具体部署版本的成功前缀保证、异常输出和 reset/flush 行为仍须以部署 SDK/provider 文档或可重复实测确认。

硬件实验每端记录：操作系统与内核、Mooncake commit 与二进制 SHA-256、SDK 包/头文件版本、provider 库版本与路径、驱动和固件版本、设备型号/编号/EID、NIC/URMA 设备与端口、队列创建返回参数。缺任一关键版本字段时保留 UNKNOWN；不得用文档版本或 FetchContent fallback 版本补空。

## 3. 代码修改方案与安全不变量

本章是后续实现要求，不是源码已修改说明。V1.1 中涉及完整函数替换的代码块不应直接复制执行；先按目标 SDK/provider 接口完成验证，再形成最小补丁。

### 3.1 post 后不再读 Slice（R01）

URMA 与 RDMA 共用同一生命周期原则。

1. 在 provider API 调用之前，将本次 WR 链每项所需的 length、slice/task 归属、队列 ID、对象代次、提交序号和统计分类复制到稳定的不可变提交记录。该记录不引用可回收 batch 中的可变 Slice 字段。
2. provider 返回后的 accepted bytes、attempt bytes、失败后缀、trace 字段均由本次记录和经过验证的 accepted 数计算，不从原 Slice 列表回读长度。
3. completion 进入处理函数后，先通过稳定请求 token 查找不可变记录，并将本次 completion 所需元数据复制到本地变量；随后才允许调用 markSuccess/markFailed 等终态发布。
4. completion 终态发布后，不再读取 Slice、task、batch 或可能已释放的关联内存。
5. 请求 token 的存活覆盖 provider 调用期和所有已接受 WR 的 completion 期。至少持有两类引用：提交调用尚未返回的调用方引用，以及每个 provider 已确认接受的 completion 引用。两者都结束后 token 才可复用。对象槽位复用必须增加 generation。
6. endpoint 写锁只保护 endpoint 内部队列结构，不提供 Slice/batch 的生命周期保证。并发完成可以早于 post API 返回；此情形必须安全。

不要把长寿命 Slice 指针塞入诊断事件，也不要为了 trace 在终态发布后重新读取业务对象。

### 3.2 trace 事件关联和阶段边界（R02–R04）

每个事件必须包含下列关联键；不适用的字段以明确的 null 标记，不以 0 冒充有效 ID：

| 字段 | 定义 |
|---|---|
| run_id | 一次进程内实验运行的随机/单调唯一 ID |
| context_id、endpoint_id | 稳定逻辑 ID，不以可复用指针作为 ID |
| queue_id、queue_kind | Jetty/QP 的队列 ID 和类型；独立记录关联 JFC/CQ ID、类型及映射 |
| object_generation | context、endpoint、Jetty/QP、JFC/CQ 每次重建递增的代次 |
| submit_chain_id、wr_index | 每次 provider 调用的提交链 ID 和链内序号 |
| request_token_id | 稳定提交记录/请求 token ID |
| tid、worker_role | OS TID 与角色，例如 initiator、UB post/poll worker、RDMA post worker、RDMA poll worker |
| monotonic_ns、event_type、result | 本机 steady clock 时间、事件类型和原始结果/计数 |

移除以 thread-local 单一 last-completion 时间支持跨线程 completion-to-post 关联的做法。不能仅将该变量改成全局 atomic。离线用上述稳定对象键和额度因果关系恢复链路；无法唯一匹配时记录 unavailable 和原因。

必须记录的阶段如下：

| 阶段 | 边界与附加条件 |
|---|---|
| worker loop | loop begin/end、begin-to-begin、post phase begin/end、poll phase begin/end；正常 park/wakeup 单独事件 |
| 软件队列 | pending enqueue/dequeue；提交链/重试队列长度和等待时长 |
| endpoint 锁 | 锁等待 begin/acquired；锁内工作不与等待时间混合 |
| 连接和分发 | connect、endpoint 查找、redispatch begin/end |
| WR 构造 | 组装、长度快照、额度预留各自计时 |
| provider post | 实际 API 进入/返回、返回码、bad_wr 原始值/分类、attempted/accepted/unattempted 数 |
| provider poll | URMA JFC 或 RDMA CQ provider API 进入/返回、原始返回值；completion 解析另计 |
| completion | provider 返回到处理开始、处理开始到终态发布；终态所用元数据来自稳定 token |
| 额度一级 | JFC/CQ 额度释放时刻、对象 ID、释放数、释放前后快照 |
| 额度二级 | Jetty/QP 深度释放时刻、对象 ID、释放数、释放前后快照 |
| 后续提交 | 仅当同一队列 pending 非空且对应额度真实可用时，记录下一次 provider post；标明触发的额度类型和关联队列 |

连续空 poll 区间只针对该 JFC/CQ 有在途 accepted WR 时统计。空闲队列没有完成等待。并发环境的 before/after 计数是观测快照；不能要求两次快照之差等于单次 post 数，因为其他线程可能同时预留或释放。provider poll 外层总耗时不能标记为“provider poll 耗时”；completion 解析、失败日志和终态发布必须拆开。

线程阶段事件只能说明线程当时位于哪个软件阶段。若结论称线程因调度未运行，另采 perf sched 或等价 off-CPU 证据；若停顿在 provider API 内，只能先报告“provider 调用区间”，不能直接指为设备或固件故障。

### 3.3 采集与输出分离（R04）

- 默认关闭 trace。关闭时不增加诊断时钟读取、诊断日志、逐 slice trace 快照和全队列汇总扫描；R01 生命周期所必需的安全提交记录按生产逻辑实现，不受诊断开关影响。
- 开启时使用启动阶段预分配的有界事件环或等价固定容量存储；热路径只写入固定宽度事件，不格式化字符串、不逐事件同步写日志。
- 输出限频只控制导出/打印，不过滤事件采集。采集总数、最大值、分母在限频之前更新。
- 对缓冲满、事件截断和序号缺口记录 dropped_count、首末丢弃时间和丢弃事件类型。关键链缺事件时，该链结论为 incomplete/unavailable，不能用相邻日志补出因果关系。
- 正常结束或异常中止时，由统一 shutdown coordinator 在事件写入 worker 停止并确认最终序号后执行幂等 final flush；worker teardown 负责封存本地分段，不与其他仍在写入的线程并发导出同一环。短于 1 s 的运行和最后不足 1 s 的区间也必须有汇总。输出退出状态、最后处理序号、未刷写数和丢弃标记。
- P99 只能从完整原始分布或预先定义的无偏采样中计算。慢事件筛选日志不能用于总体 P99；缓冲溢出期间的延迟分布不得宣称完整。

### 3.4 设备能力与有效配置（R05）

配置分为 requested、device capability、create requested 和 created/effective 四层。每个 context 在查询对应设备后独立派生不可变有效配置；不能让一个设备对全局 GlobalConfig 的钳制污染后续设备、RDMA 配置或另一个 endpoint。

初始化顺序必须重排为：

1. 解析用户请求值，保留原始 requested 值；
2. openDevice 并查询该具体设备能力；
3. 按设备能力、软件策略及本进程资源预算计算 context effective 配置；
4. 使用该 immutable config 创建 JFCE/JFC/JFR/Jetty 或 QP/CQ；
5. 从创建返回对象/SDK 可查询字段记录 effective created 值；额度账本直接使用同一有效值；
6. 检查 endpoint store 实际容量、context endpoint 上限和已创建对象数量；
7. 握手发布本端队列数量/映射/代次；双端无法兼容则明确失败，不暗中调整队列映射。

现有 UrmaContext::construct 在 openDevice 之前复制 num_jfc_list、num_jfces、eid_index、max_jfc_e。更新全局 config 不会改变这些局部副本。修订应先取得设备能力并生成 config，再创建对象；创建过程中某个对象失败时，按成功创建顺序反向清理，测试清理后重新初始化。

能力审计至少覆盖 SDK 暴露的 JFS/JFC/JFR 深度、Jetty/JFC/JFCE/JFR 数量、SGE 数量、单 WR 最大长度、max message size 和 endpoint 总量。字段名和单位在目标 SDK/provider 未确认前写为“待按部署接口核验”，不照抄其他版本结构体名称。设备 max capability 不代表当前可用资源，不代表对象一定能创建；其他 context 和进程可能占用资源。

当前源码在 Jetty 创建时请求 JFS depth=2048。因此 2048 只能称为当前创建策略中的请求上限；只有查询设备能力并核对 create result 后，才能报告该设备对本次 context 的有效值。当前源码的 JFR depth=2048 和 JFC depth 默认请求值同样不得直接表述为硬件能力上限。send/recv JFC 的对象数量和共享资源核算按部署 SDK 语义分别核对。

### 3.5 提交所有权、失败和回收（R06）

每个 WR 在任意时刻必须且只能处于一个状态：

**pending → reserved → provider accepted → completion observed → terminal**

未被接受的失败项进入 **retry pending** 或终态失败；未尝试项继续留在 pending。provider 已接受、但 completion 报失败的请求由 worker 的错误消费路径取得稳定请求记录；只有在 completion/provider 合约证明该 WR 不再访问相关内存后，才释放本次队列额度，再按重试策略进入 retry pending 或终态失败。重试使用新的 submit_chain_id/attempt_id，并重新预留额度，不能复用旧 completion 所有权。状态转移和额度操作具备唯一 token/epoch，所有回收操作幂等且恰好一次。

#### URMA/RDMA post 边界处理

1. 额度预留前验证 endpoint 状态、导入 Jetty/目标 QP 映射和远端地址描述。导入对象缺失立即返回明确连接错误；禁止向 provider 提交 null tjetty/null qp。
2. 形成当前 provider 链和其每项稳定 token；保存合法 WR 地址集合或用安全的范围/对齐校验，不对未知 bad_wr 指针先做指针减法。
3. 仅当部署版本 contract 明确“非零返回时 bad_wr 是本次提交链中第一个失败 WR，之前的 WR 已被接受”时，才将其索引 K 视为 accepted prefix。A 为本次尝试数，则仅回滚 A−K 个未接受项的预留和 Jetty/QP 深度；K 项由 completion 归还。后续未尝试项仍在 pending。
4. 成功返回时按该 API 成功语义确认接受数，并仍允许 completion 先于 post 返回。
5. 非零返回但 bad_wr 为 null、非法地址、对齐异常、位于链外，或 provider contract 未确认时，结果为 ambiguous。不得自动当作全部成功，也不得把整链盲目重试。冻结该 endpoint 新提交，保存原链和额度账本，按已核对的 provider drain/reset 合约隔离、排空或失败退出。
6. worker 必须消费 submit 返回错误及 failed Slice；不得只写日志后丢弃。错误必须使关联请求进入明确 retry/final 状态，或使测试/服务以错误退出。

#### 两级额度与 reset/flush/teardown

- JFC/CQ 额度与 Jetty/QP 深度是两本独立账。为每项 WR 记录 reserve_generation、queue_generation 和唯一回收位。
- completion 到达时，按 SDK 语义分别登记 JFC/CQ credit 与 Jetty/QP depth 的实际可用时刻。若实现分两阶段释放，要在事件中保留两个时间；任何一方不得因另一方先释放而提前假定可用。
- reset/flush/teardown 先将对象状态从 active 切为 draining 并推进 generation；停止新 post，按 provider 合约收集完成或确认取消，再收敛 reservation。晚到 completion 先匹配原 generation；旧代请求不能改写新对象计数。
- teardown 与 completion 竞争时仅一个路径可完成 token 的回收 CAS/状态迁移；重复完成、旧代完成和 duplicate flush 记录为异常，不重复释放额度。
- 若 provider 无法确认某个 accepted WR 是否仍可能产生 completion，保留隔离额度直至 endpoint/context 销毁完成；不能为了计数归零而提前复用对象。

### 3.6 热路径成本控制（R09）

- planner、提交快照和暂存 WR 缓冲按实际待提交量或实际有效容量复用，不因一次只提交少量 slice 就清零/分配最大队列深度的多组容器。
- 额度 helper 必须与生产路径使用同一实现；测试中注入额度状态和 provider 行为，避免另写一个行为不同的“参考 planner”。
- 默认关闭 trace 时不做新增诊断工作。启用 trace 后复用预分配缓冲；溢出有标识，不动态扩容。
- 不在本轮混合修改 UB worker 数、post/poll worker 分工、completion policy、slice 粒度和默认 Jetty 数。新增轮转/并行度变更单独与仅修生命周期/额度版本对照。

## 4. 可执行验证矩阵

本轮仅将验证设计写入文档，V01–V12 均未执行。用例分三层：

1. **纯逻辑单测**：状态机、额度预留、generation、trace 关联和 flush，不依赖真实设备。尽量仅链接必要代码和 gtest；不能因 USE_UB 或整个 transfer_engine 初始化而要求设备。
2. **provider mock 集成测试**：由可控 provider test double 注入完成早于 post 返回、部分接受、无效 bad_wr、能力值和创建失败。它只证明软件状态及故障路径，不证明设备性能或 provider 真机行为。
3. **真实设备验证**：目标 SDK/provider 版本已登记，双端选路证据有效，所有传输/队列参数从运行时日志和设备接口确认。mock 结果不能升级为硬件性能。

| 用例 | 可执行场景 | 必查断言 | 执行状态 |
|---|---|---|---|
| V01 早完成与回收 | mock provider 在 post API 返回前将全链完成送至另一 poll 线程；completion 终态后立即释放/复用 batch，再让 post 返回 | 无 post 后/终态后 Slice 读取；成功字节与 token 代次准确；ASan 可用时再运行 | NOT RUN |
| V02 超额竞争与部分授予 | 屏障同步多线程申请，累计请求至少为容量 2 倍且混合申请大小；outstanding=4090 时申请 16 | 总授予及高水位不超过 4096；剩余额度仅授予 6；满后授予 0；释放后可申请；无负数/重复释放 | NOT RUN |
| V03 部分提交与失败指针 | first/middle/last WR 失败、全成功、空 bad_wr、非法/链外 bad_wr、导入映射缺失 | 只对 contract 确认的前缀计 accepted；失败后缀归还/重试一次；异常边界为 ambiguous；空目标从不 post | NOT RUN |
| V04 能力不足和创建失败 | mock 设备深度小于请求、JFC 数量不足、两设备能力不同、第 N 个 JFCE/JFC/JFR/Jetty 创建失败 | provider 实收创建参数=effective 配置；失败反向清理；再次初始化成功；endpoint store 和额度上限一致 | NOT RUN |
| V05 两级额度延迟 | 延迟 mock 在 JFC/CQ 额度释放后、Jetty/QP depth 释放前制造间隔 | 两级释放有各自事件和对象 ID；未真正可用的队列不被下一次 post 当作有额度 | NOT RUN |
| V06 跨线程关联与重建 | 两个 endpoint 共享/不共享完成队列，交换 post/poll worker；销毁并重建对象后注入旧完成 | 关联键不串 endpoint/队列/代次；旧完成不改写新代账本；无法匹配时为 unavailable | NOT RUN |
| V07 阶段与调度区分 | 分别在 queue、endpoint lock、connect/redispatch、provider post、provider poll、completion、两级释放点注入延迟 | 延迟落在正确阶段；调度原因另由 perf sched/off-CPU 证据支持；软件计时不冒充调度证据 | NOT RUN |
| V08 trace 丢弃与最终汇总 | 跑多个 20 ms 周期；覆盖小于 1 s、最后不足 1 s、buffer 溢出、worker 异常退出 | 所有写入线程停写后由统一协调者执行幂等 final flush；汇总分母/最大值完整；dropped_count、时间范围、事件类型准确；缺链标 incomplete | NOT RUN |
| V09 Jetty 轮转 | 1/2/4 Jetty；令首选 Jetty 满而其他有空间；共享 JFC 额度竞争 | 使用实际可用队列；pending 不丢失；实际 create 数与配置及握手映射一致 | NOT RUN |
| V10 窗口饱和 | pending 至少覆盖实际有效聚合容量两倍；扫描允许的 MC_MAX_WR/Jetty 组合 | 直接看到 pending 非空、accepted/outstanding 到边界或更早瓶颈；未填满窗口的结果不能证伪窗口假设 | NOT RUN |
| V11 等窗口/等字节/slice 对照 | 等总 WR 如 1×512、2×256、4×128；等 pending bytes；单、多 endpoint 共享 JFC；固定应用总字节扫描有效 slice size | 检查队列总容量、队列并行、共享额度、slice 固定成本，分开报告窗口上限和吞吐 | NOT RUN |
| V12 原业务、传输选路和回退 | 原始业务提交轨迹及 8 MiB 小批；记录双端新旧二进制哈希、实际 transport、端口原始计数；执行完整回退演练 | 业务症状可复现；选路有运行证据；成功 payload 与功能结果一致；回退恢复旧二进制及启动配置 | NOT RUN |

### 4.1 V02/V04 额度与创建故障参数

V02 除 4090+16 的边界值外，还要加入不同申请量并发（例如每次申请 1、3、17 个）与申请/释放交错。总申请量应显著大于容量，确保错误的无界原子加实现无法通过。记录每线程授予数、拒绝数、最大 outstanding 和最终账本。

V04 记录以下多设备/双端差异：每设备 requested 值、capability 值、创建 API requested 值、返回对象/effective 值、已分配对象数量、endpoint store 容量和握手发布值。两端配置不一致的测试必须核实协商结果：兼容映射则明确映射，不能协商则在 post 前失败。

### 4.2 V03 所有权账本断言

每个 request_token_id 的每个 WR 在运行中只能归属于 pending、reserved、provider accepted、completion observed、retry pending、terminal 之一。测试结束应满足：

- 成功数 + 失败终态数与输入 WR 数相符；
- 未完成请求数为零，或明确留在隔离对象并计入报告；
- reserved、JFC/CQ outstanding、Jetty/QP depth 均无负值；
- 同一 accepted WR 只由 completion 或已证明完成的故障清理路径释放一次；
- 未接受及未尝试请求均未伪装成 success；
- reset、flush、teardown 与晚到 completion 竞争不重复回收。

## 5. 硬件实验设计

硬件实验只有在 V01–V09 纯逻辑/provider mock 用例通过、部署版本与队列参数记录完整、实际 transport 有运行证据后才开始。若这些条件不满足，结果保持 BLOCKED 或 INCONCLUSIVE，不能将 mock 结果称为硬件性能。

### 5.1 负载族

| 负载族 | 固定/变化内容 | 用途与限制 |
|---|---|---|
| 原 8 MiB 小批复现 | 保留原单 endpoint、64 KiB × 128 个应用请求（总 payload 8 MiB），原提交轨迹和方向 | 与用户症状对照。128 个 64 KiB WR 少于默认单队列 256 WR，不能填满窗口；其不随窗口扩张不构成窗口假设的反证 |
| 单 endpoint 饱和供给 | 单目标、单 endpoint、同方向，持续提供足量 pending；先测当前有效 queue/JFC 总容量 | 候选 pending bytes 至少是实测有效 aggregate window 的 2 倍。按实际 created capacity 计算，不硬编码 512 MiB。若目标配置为 4×1024×64 KiB，则理论软件容量为 256 MiB；只有该参数能被创建且共享 JFC 不更小，才可将 512 MiB 作为待核实供给 |
| 等总 WR window | 在设备和 provider 允许范围内比较 1×512、2×256、4×128 等总 WR 相等配置 | 分别记录每队列 created depth 和共享 JFC/CQ 实际上限；如果 JFC 更小或某队列无法创建，该组不构成等窗口 |
| 多 endpoint 共享 JFC | 固定共享 JFC/context，增加 endpoint 数并对单流分别计量 | 查看全局额度、公平性、单流饥饿和总吞吐；总吞吐上升不等于单流无停顿 |
| 等 pending bytes | 示例：64 KiB×4096、256 KiB×1024、1 MiB×256，均为 256 MiB | 默认内部 slice 仍可能将较大应用 block 切成 64 KiB；此组比较应用请求数量/批处理成本，不能直接假定实际 inflight bytes 相同 |
| 有效 slice 扫描 | 固定应用总字节、目标范围、方向和 endpoint 数；分别读取并记录实际生效的 slice size | 要确认请求确实按该粒度切分。仅改变 block size 不等于改变内部 slice |
| 原业务回归 | 相同模型/数据、双端配置、请求提交轨迹和实际传输方向 | 单独回答真实业务是否仍有约 20 ms 停顿；不得用 tebench 结果替代应用结论 |

所有饱和组都必须记录提交前 pending 数/bytes、每条队列有效上限、provider 实际接受 WR、accepted payload bytes、在途高水位和 provider 返回。若队列从未到达预设边界或 pending 曾为空，本组不能用于否定窗口容量假设。

### 5.2 线程、内存与选路控制

- 应用发起线程数单列；transport 的 post worker 和 poll worker 数另列。tebench 的 start_num_threads 是 initiator 发起线程数，不是 Transfer Engine 的 transport worker 数。
- 记录 CPU 亲和性、NUMA node、内存类型、注册范围、设备/端口、读写方向、provider 参数和目标 buffer 范围。先让两端 buffer、注册区间和地址范围覆盖最大负载。
- 运行时证明 transport：保存两端 transport 初始化日志中的实际 installTransport/type、设备/端口选择和连接信息；将其与对应端口计数增量、单 endpoint/queue 事件关联。只有 CMake 开启 USE_UB 不是选路证明。
- classic tebench 的自动发现路径在当前 checkout 默认并不能用 CLI 显式选择 URMA；--xport_type 参数用于 tent backend，当前枚举不含 ub。不得把 --xport_type=ub 写成可执行命令。若要用 tebench 单独驱动 URMA，先增加并核实明确选择路径，再以日志和端口计数证明实际走 URMA。
- 当前源码可用 MC_FORCE_HCA 环境变量请求走 HCA/RDMA 路径；仍须检查运行时实际 transport 日志和 HCA 端口计数。仅写环境变量不够。
- pacing 必须记录为 request_interval_us=0，且确认被测应用/脚本没有额外 sleep、限速、回压或固定批间等待。该配置排除显式 benchmark pacing，不代表服务中不存在 20 ms 定时器。

### 5.3 双端命令与同进程预热

以下 tebench 命令的可执行文件名、classic backend、segment 参数、DRAM 类型、block/batch、线程、duration 和 request_interval_us 均与当前 checkout 的 benchmark/main.cpp、utils.cpp、te_backend.cpp 及源码输出提示核对过。**命令未在目标机器执行，启动结果和实际 transport 均待验证。** 命令可用于 classic RDMA 基线；不能用于证明 URMA 已选中。两端必须来自同一待测二进制，且服务端先启动。metadata_type 使用当前 p2p 默认值。

服务端（目标端）：

~~~bash
export MC_FORCE_HCA=1
./tebench --backend=classic --seg_name=kvwin_run01 --seg_type=DRAM --total_buffer_size=1073741824
~~~

发起端（客户端，8 MiB 小批复现，单个应用请求）：

~~~bash
export MC_FORCE_HCA=1
./tebench --backend=classic --target_seg_name=kvwin_run01 --seg_type=DRAM --total_buffer_size=1073741824 --op_type=write --start_block_size=65536 --max_block_size=65536 --start_batch_size=128 --max_batch_size=128 --start_num_threads=1 --max_num_threads=1 --duration=10 --request_interval_us=0
~~~

此命令的单次应用请求为 64 KiB×128=8 MiB。当前 tebench 在测量前有约 1 s 的内建预热，不能配置为同进程 10 s。为满足 10 s 同进程预热，后续 benchmark 代码需要增加明确的 warmup duration 控制，或在同一个 runner/process 中先运行 10 s 未计量 phase，再清零软件统计/端口计数基线并进入正式测量。参数/代码尚不存在，因此本版不伪造 warmup_duration_s 之类的启动参数，也不把另起进程的预热算作同进程预热。实施后必须将两个端点的命令、phase 起止事件和剔除规则补入复审版。

脚本使用限制：

1. 服务端、客户端分别保存 stdout/stderr、环境快照、启动命令、PID、退出码、二进制 SHA-256 和实际 transport 初始化行。
2. RDMA 基线只有在双端日志确认 RDMA，且目标端口原始计数随流量增加时才可记为有效。
3. URMA 实验当前没有已核实的 classic tebench CLI 选择命令；V12 不能仅凭上述脚本标记为 UB/URMA。待实现显式选择器后再给出相应脚本。
4. 运行前确认 p2p metadata service 的连接方式、端口、防火墙和 segment 名在两端一致；目标设备/驱动需在两端已就绪。

### 5.4 指标口径、端口计数和重复配对

每个 run 同时保存下列互不替代的数据：

| 指标 | 起止口径 | 分类 |
|---|---|---|
| 完整观测期成功 payload throughput | 预热后统一观测窗口内唯一成功 payload bytes ÷ steady_clock 窗口长度 | 主要软件吞吐；去重重试 |
| 单批 submit-to-completion latency | 方案新增统一计时：当前批开始调用 submit 前至所有该批请求终态完成 | 批次端到端传输时延 |
| 批次循环 latency | 批次请求构造开始至完成、回收和清理结束 | 应用/benchmark 控制成本 |
| provider submit latency | 实际 provider post API 进入至返回 | provider 调用时长，不含完成 |
| completion visibility/processing | provider poll 原始返回边界、解析完成、终态发布分段记录 | 软件观察/处理边界 |
| 硬件端口计数 | 采集器实际读到的原始累计值和时间；差分前处理重置/回绕 | 硬件报告值，不代替唯一成功 payload |

当前 tebench 的内建单批计时在请求向量构造完成后、submit 前启动，在全部 Slice 完成后、freeBatchID 前停止；它不包括请求向量构造和 batch 释放。主 benchmark 的测量循环计时覆盖更外层的请求循环，并受 pacing 配置影响。以上两种内建结果不能替代本方案新增的完整批次循环时延。若需要独立报告构造至清理完成的循环时延，须增加明确计时点并在各比较组保持一致。

每次 request 分开统计 **attempted WR**、**provider accepted WR**、**failed/unattempted WR**、**attempt payload bytes**、**accepted payload bytes** 和 **unique successful payload bytes**。retry bytes 不能累计进唯一成功 bytes。读/写方向、传输层 overhead 和端口采样计数分别报告。

若目标设备确认 UB 端口指标 tx_busi_flit_num 的每计数单位为 20 B，再报告原始 delta 和按 20 B 换算值；目前这只是关联讨论中的 counter unit 报告，未用目标硬件文档确认，必须先保存原始单位并核对计数器说明、计数器位宽、清零/回绕行为。不得直接将端口增量等同为 Mooncake payload。

burst 起点定义为预先选定端口计数器首次超过空闲噪声阈值的采样点；终点为恢复到同一噪声阈值并连续保持至少 2 个采样周期的首个区间边界。burst byte 仅为该区间计数差。burst 间隔定义为连续 burst 起点之间的时间；idle 定义为终点至下一起点。若采样周期大于目标发送过程，active bandwidth 和精确 burst 起止都报告 unavailable，不用粗粒度采样外推微秒级值。

节点内软件事件统一使用 steady_clock。端口采集时间与软件时钟需在每端同机校准；跨主机相减前必须量化时钟偏差和不确定度，其误差需小于待比较最短时间区间的 10%，否则只做各自时钟的局部分析，不做跨主机微秒级关联。

每配置至少 5 组配对重复，旧/新或诊断关/开按随机或交错顺序运行；每对使用相同输入、方向、拓扑、worker 绑定和环境。报告所有单次值、配对差、样本量、置信区间方法和原始数据位置。样本不足以让区间稳定时继续重复，不得只报均值或最好一次。

### 5.5 预先注册的性能与观测开销阈值

下列是**拟议项目判定阈值**，不是 Mooncake 官方承诺，也不是已测结果；目标团队须在运行前确认并冻结。

| 比较 | 建议通过条件 | 解释 |
|---|---|---|
| 诊断关闭的新版本 vs 旧二进制 | 配对吞吐比 95% CI 下界 ≥0.97；P99 时延比 95% CI 上界 ≤1.05；功能与唯一成功 bytes 完全一致 | 允许不超过 3% 的统计吞吐回退和 5% 的尾延迟回退 |
| 完整 trace 开启 vs 诊断关闭的新版本 | 配对吞吐比 95% CI 下界 ≥0.98；P99 比 95% CI 上界 ≤1.05；post/poll worker CPU 增幅 ≤5% | 观测扰动上限；trace 丢弃或缺事件时不通过完整性门槛 |
| 性能收益声明 | 吞吐配对提升 ≥5%，配对差 95% CI 下界 >0；错误数、唯一成功 bytes 无回退，P99 未恶化超过 5% | 达到此条件才记为当前配置有可重复收益；否则记 NO DEMONSTRATED IMPROVEMENT |

P99 的样本定义和估计方法必须预先固定，必须使用原始完整样本。窗口变化、诊断开启或关闭、Jetty 轮转各自单独比较；不能只报告混合了多个改动的最终组。

## 6. 构建、执行和回退计划

### 6.1 构建与测试条件

以下 configure/build 命令形式根据当前 checkout 的 CMake 选项与 target 条件核对过，但**未在当前环境执行**。它们还要求目标 Linux 工具链、项目依赖和实际 URMA SDK/provider 均可用。逻辑/mock 测试目标应在后续 patch 中从 USE_UB/transfer_engine 真设备依赖解耦；目标名在实际加入 CMake 前保持计划状态。

~~~bash
cmake -S "$MOONCAKE_SRC" -B "$BUILD_DIR" -DUSE_UB=ON -DBUILD_UNIT_TESTS=ON -DBUILD_BENCHMARK=ON -DWITH_STORE=OFF -DWITH_STORE_RUST=OFF
cmake --build "$BUILD_DIR" --target tebench config_test ub_transport_test
ctest --test-dir "$BUILD_DIR" --output-on-failure -R 'config_test|post_lifetime_mock_test|credit_ledger_test|trace_link_test|device_config_mock_test'
~~~

第一、二行是目标环境的命令模板，不表示可在本 Windows 文档环境执行成功。tebench 在当前 CMake 中由 BUILD_BENCHMARK 控制，ub_transport_test 由 USE_UB 相关条件生成；当前 CMake 明确没有将 ub_transport_test 注册到 CTest。若实际构建树中对应目标不存在，应先核对 CMake configure 摘要和目标列表，不能删掉失败日志后报告通过。

建议构建/验证顺序：

1. 无真实设备构建纯逻辑状态机、额度、能力配置和 trace 关联测试；保存编译器版本、CMakeCache 和日志。
2. 运行 gtest/CTest 纯逻辑和 mock 集；V01–V09 全通过后才进入 provider 集成。
3. 有目标 SDK 时构建 USE_UB 版本，手动执行 ub_transport_test；保留完整原始输出。该工具需要实际设备，不用它替代独立 mock 用例。
4. 真实设备功能 smoke test 后再执行窗口族和业务回归；任何失败、超时、错误边界 ambiguous 或 trace 丢弃都保留为未通过/不完整。

### 6.2 测试矩阵分阶段比较

至少建立下列构建/运行组，所有组使用同一编译器、优化选项、依赖版本和输入：

| 组 | 内容 | 目的 |
|---|---|---|
| B0 | 原始 V1.1 对应源码状态的旧二进制 | 复现基线；先确认 transport 与观测 |
| B1 | 仅生命周期修复和观测，诊断关闭 | 验证安全修复的生产路径成本，不改变队列分配策略 |
| B2 | B1 加有效能力配置、额度账本修复 | 验证有效上限和错误回收 |
| B3 | B2 加 Jetty 轮转/目标优化 | 单独评估轮转是否有性能收益 |
| B4 | B2/B3 分别诊断开启 | 评估 trace 观测开销和完整性 |

分阶段结果不可合并为“新版本总体优化”而不说明具体组间差异。

### 6.3 完整回退

回退基准在执行前生成并保存：旧源码提交或源码快照 SHA、旧二进制 SHA-256、构建参数/依赖清单、两端启动命令、环境变量、设备/队列配置、数据路径、端口原始计数和旧配置结果。

回退动作：

1. 停止两端新进程，确认没有该 run 的 accepted WR 或在途 completion；如需 reset/flush，先按部署 provider 合约执行并确认队列 drained。
2. 两端都恢复旧二进制及其依赖库/启动配置，再同时重启；不允许一端旧版本、一端新握手映射继续跑性能结果。
3. 核对两端 SHA-256、transport 初始化日志、Jetty/QP 数、有效深度及回退后 smoke test。
4. 将失败新版本日志、run id、端口原始计数和新旧配置差异归档。

只将 MC_NUM_JETTY_PER_EP 设为 1 不是完整回退，因为它不能恢复随机选队列策略、额度账本、生命周期、provider 错误处理和 trace 行为。当前本轮未生成新二进制，也未执行回退。

## 7. 约 20 ms 归因规则和最终状态

| 观测证据 | 可报告的结论 | 仍不可推出 |
|---|---|---|
| 同一 endpoint/queue 有 pending，worker 阶段出现长区间，perf sched 显示线程 off-CPU | 该样本存在调度服务延迟 | 所有约 20 ms 间隔均为调度导致 |
| 有 accepted WR 在途，worker 持续 poll，provider 原始 poll 多次返回 0 | completion 在这些 provider poll 样本中不可见 | 设备已在某个推算时刻完成但 provider 延迟通知 |
| provider 已返回 completion，completion 处理或两级额度释放阶段明显变长 | 已定位到对应软件处理/回收阶段 | 网络带宽不足或硬件根因 |
| 同一队列 pending 非空、额度已实际可用、下一次 post 延迟 | 后续查找锁、连接、分发、worker 阶段及提交路径 | 无关队列的 completion 到任意 post 的时间代表该队列等待 |
| 饱和供给且窗口被填满时，accepted bytes/window 随有效配置变化 | 窗口对该负载有贡献 | 窗口是唯一根因，或约 20 ms 问题已解决 |
| trace 丢弃、对象代次缺失、两端时钟精度不足或传输选路不确定 | 证据不完整，归因 unavailable/inconclusive | 用推测补成确定结论 |

每个 run 最后独立报告四类状态：

1. **代码功能：** PASS / FAIL / NOT RUN / BLOCKED，引用 V 用例和原始日志。
2. **窗口假设：** SUPPORTED / NOT SUPPORTED / INCONCLUSIVE；供给不足、窗口未填满或选路不明时必须 INCONCLUSIVE。
3. **性能目标：** IMPROVED / NO DEMONSTRATED IMPROVEMENT / REGRESSED / NOT MEASURED；只使用诊断关闭的配对运行。
4. **20 ms 定位：** LOCALIZED / PARTIALLY LOCALIZED / UNRESOLVED；只到 provider poll 可见性时不得写成设备根因。

功能通过不等于性能改善；窗口被支持不等于停顿被定位；mock 通过不等于硬件通过。

## 8. R01–R10 审核意见闭环表

下表是**文档修订闭环**。执行状态列中的“静态核对”仅代表源码/文档已读；验证用例没有执行，因此不作为代码问题已关闭或测试通过的证据。

| 编号 | 原问题 | 修订位置 | 验证用例 | 执行状态 | 剩余限制 |
|---|---|---|---|---|---|
| R01 | provider post 返回或 completion 终态发布后仍可能读取已回收 Slice；锁不保护 batch 生命周期 | §3.1；§4 V01、V03 | 早 completion、释放/复用 batch、ASan | 文档已修订；V01/V03 NOT RUN。源码风险已静态核对 | 尚无实现补丁；真实 provider 对 user_ctx 的持有语义待核实 |
| R02 | thread-local 单时间值不能关联跨线程、不同 endpoint/完成队列事件；queue 编号语义混用 | §3.2；§4 V06、V08 | 多 endpoint/worker 交换/重建 generation；无法匹配应 unavailable | 文档已修订；NOT RUN | 需实现稳定事件 ID 和实际 worker TID 采集 |
| R03 | worker gap、provider poll、completion 处理及两级额度释放边界不完整 | §3.2；§7；§4 V05、V07 | 分阶段注入延迟、两级额度延迟、off-CPU 核验 | 文档已修订；源码阶段事实静态核对；NOT RUN | 无硬件 trace；provider 内部阶段仍需适用 SDK 观测 |
| R04 | 共用限频器丢因果事件，短运行和结尾没有最终汇总 | §3.3；§4 V08 | 多周期、<1 s、末尾不满 1 s、buffer 溢出、异常退出 | 文档已修订；NOT RUN | 尚无 trace ring/final flush 实现和丢弃数据 |
| R05 | 设备能力、创建策略与 effective 值混淆；openDevice 前已有过期配置快照 | §2.2–2.3；§3.4；§4 V04、V09 | 能力不足、两设备、两端差异、第 N 个资源创建失败 | 源码初始化顺序静态核对；目标 SDK/provider UNKNOWN；V04/V09 NOT RUN | 无目标设备能力与创建返回记录；2048 仅源码 create request |
| R06 | 空导入对象可能 post；部分提交、空/非法 bad_wr、retry 和 reset/late completion 回收不闭环 | §3.5；§4.1–4.2 V02–V06 | first/middle/last 失败、非法指针、缺映射、flush/重建晚到完成 | 文档已修订；错误路径静态核对；NOT RUN | 部署 provider 对部分提交/异常返回/flush 的合约未知 |
| R07 | 单次 8 MiB 负载不足以填满默认 256 WR，错误地用于否定大窗口 | §5.1；§4 V10–V12 | 保留原小批；足量 pending；等总 WR、等 bytes、共享 JFC | 推导及源码默认值静态核对；NOT RUN | 真实有效容量、buffer/register 范围和性能未测 |
| R08 | 原单测未超额竞争，缺少部分授予、早完成、创建失败、跨线程/代次等可执行用例 | §4 全节；§6.1 | V01–V09，逻辑/mock 与设备验证分层 | 文档用例已补；全部 NOT RUN | 新测试目标/故障注入代码未实现 |
| R09 | 诊断关闭仍可能新增分配、容器初始化、原子争用与汇总扫描成本 | §3.3、§3.6、§5.5、§6.2 | B0–B4 配对测试；观测开销阈值 | 文档阈值与拆组方案已补；NOT RUN | 阈值待项目确认，当前无开销测量 |
| R10 | 实际选路、应用线程/worker、预热、计时口径、端口 counter、重复规则与完整回退缺失 | §2.3；§5.2–5.5；§6；§7 | V10–V12，双端运行日志、端口原始值及回退演练 | 当前源码 CLI/route 和计时范围静态核对；命令未执行；目标 SDK/provider UNKNOWN | tebench 当前无可核实 URMA 显式选择参数；10 s 同进程预热需后续 benchmark 支持 |

## 9. V01–V12 与 R 项一致性映射

| 验证项 | 覆盖审核项 | 本方案位置 | 当前状态 |
|---|---|---|---|
| V01 早完成 | R01 | §3.1、§4 | NOT RUN |
| V02 超额争用/部分授予 | R06、R08 | §3.5、§4.1–4.2 | NOT RUN |
| V03 部分 post/失败边界 | R01、R06 | §3.1、§3.5、§4.2 | NOT RUN |
| V04 能力不足/创建中途失败 | R05、R08 | §3.4、§4.1 | NOT RUN |
| V05 两级额度延迟 | R03、R06 | §3.2、§3.5、§4 | NOT RUN |
| V06 跨线程关联/重建 | R02、R06 | §3.2、§3.5、§4 | NOT RUN |
| V07 阶段/锁/调度边界 | R03、R09 | §3.2、§5.5 | NOT RUN |
| V08 trace 完整性/最终刷新 | R04 | §3.3、§7 | NOT RUN |
| V09 队列轮转/可用能力 | R05、R08 | §3.4、§4 | NOT RUN |
| V10 饱和窗口 | R07 | §5.1、§5.4 | NOT RUN |
| V11 等窗口/等字节/slice | R07、R09 | §5.1、§5.5 | NOT RUN |
| V12 原业务/选路/完整回退 | R07、R10 | §5.2–5.3、§6.3 | NOT RUN |

## 10. 一致性复核与本轮交付状态

复核以下承诺在执行总览、源码修改要求、测试矩阵、脚本、验收和闭环表中的口径一致：

- 三个问题始终分开，16 MiB/32 MiB 只作为假设条件下的软件窗口推导。
- 两类 post 后 Slice 读取均改为稳定快照/token 设计，completion 终态发布后无 Slice 回读；endpoint 写锁不再被当作生命周期屏障。
- trace 关联键、阶段边界、采集/输出限频、末尾刷新、buffer 丢弃和 unavailable 在设计、测试及归因限制中一致。
- 能力值分 requested、capability、create requested、created/effective；2048 仅是当前源码创建请求深度，不写成设备最大值。
- 所有权状态机覆盖成功、失败、未尝试、重试、两级额度、reset/flush/teardown 和晚到 completion。
- 原 8 MiB 负载保留但不用于证伪；窗口饱和供给、等总 WR、多 endpoint 共享 JFC、等 pending bytes 和 slice 扫描在负载表、V10/V11 与判据中相互对应。
- tebench 应用发起线程数不再与 transport worker 数混用；UB 选路和同进程 10 s 预热缺少当前实现时明确为未解决执行门槛。
- 性能阈值标为待预注册工程判据；所有构建、测试、硬件结果均标为未运行。
- R01–R10 与 V01–V12 均保留明确的执行状态和剩余限制。

**交付状态：** V1.2 文档已形成；V1.1 保留未覆盖。只读核对完成；未修改 Mooncake 源码；未运行任何构建、单测、benchmark 或设备测试；没有性能改善、测试通过、PR 或根因结论。目标 SDK/provider/驱动/固件版本、显式 URMA benchmark 选路和同进程 10 s 预热实现均为后续执行前置项。
