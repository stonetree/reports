# Mooncake UB 传输性能优化：实验实施方案

**版本：** V1.0，2026-10-24  
**用途：** 交给 AI agent，在双节点实验环境中实现小补丁、测试吞吐、找出有效参数和改动。  
**源码参考：** `D:/codes/vllm/Mooncake`，本次读取的 classic Transfer Engine，参考提交 `f90ae691f109e49a60920e0c8abbf7e572826d8c`。  
**配套方案：** [RDMA/UB 约 20 ms 空闲定位与优化实验](Mooncake_RDMA与UB_20ms空闲定位与优化_实验实施方案_V1.0_20261024.md)。  
**当前状态：** 方案完成；本次没有修改传输源码或执行硬件测试。

## 1. 审核 V1.2 后的明确取舍

[Luna 的 V1.2](Mooncake_URMA与RDMA传输窗口和20ms间隔_代码修改测试与打点方案_V1.2_20261024.md)保留了正确的窗口、切片、批量和时间线思路，但还不是当前最合适的性能实验执行书。主要问题和本方案的处理如下。

| V1.2 的问题 | 本次处理 |
|---|---|
| 先完成 token、generation、回收状态机和 V01–V09，再做硬件实验 | 直接采用“编译→一次读写确认→性能基线→单项改动→复测”。故障恢复、对象重建、完整 mock 测试不作为性能实验前置工作 |
| 改动总览主要是观测和状态管理，缺少 Jetty 轮转及 poll 优化的实际算法 | 明确列出 U1–U7 的源码点位、算法、参数、负载与结果解释 |
| UB 的 benchmark 选路仍列为待解决门槛 | 给 benchmark 增加两个实验参数，显式安装 UB 或 RDMA；改动只放 benchmark，不重构引擎选路 |
| P2P 示例用 `--seg_name=kvwin_run01`，客户端也使用该名 | 当前 classic P2P 初始化使用 `getHostname()`，该自定义名字不会按示例生效；客户端使用目标端实际输出的 segment 名 |
| `MC_FORCE_HCA=1` 被用于 RDMA 对照 | `USE_UB` 下仍会安装 UB，不能用它保证 RDMA 单独选路；显式只安装一个 transport |
| 8 MiB 小批虽然被指出不足，但仍占据唯一启动示例 | 同时给出 8 MiB 症状复现与 512 MiB 足量供给命令，分开回答问题 |
| 固定 10 s 预热、完整分位数置信区间和大量状态报告使首轮工作变重 | 先采用现有约 1 s 内建预热、20 s 测量、3 次交错重复；差异明显就继续优化，差异小时再增加重复 |

本轮目标是找到性能空间。保留三项必要检查即可：实际走 UB、测的是成功传完的数据、前后负载一致。出现传输错误的配置记录为该点未跑通，换回上一点继续实验；不把错误恢复机制建设变成本轮主体。

## 2. 从什么地方获得性能

UB 指统一总线；URMA 指统一远程内存访问接口，是本方案调用 UB 传输的编程接口。Jetty 是 URMA 收发队列对象，JFS 是其发送队列，JFC 是完成队列。WR 是设备工作请求，slice 是 Mooncake 内部切片，endpoint 是到同一远端设备的连接对象。RDMA 指远程直接内存访问，其 QP/CQ 分别是收发队列对和完成队列。

当前源码有三个直接值得优化的地方：

1. **窗口偏小。** 默认一个 Jetty、每 Jetty 256 WR、64 KiB slice，名义在途容量为 16 MiB。先提高 `MC_MAX_WR`，可以立即验证容量对吞吐的影响。
2. **多 Jetty 使用方式不足。** URMA 每次随机选一个 Jetty；该 Jetty 满时，即使其他 Jetty 空闲也可能返回。改为一次调用遍历可用 Jetty，才能稳定使用多队列容量。
3. **小 WR 的软件服务速率可能不足。** 50 GB/s、64 KiB 对应约 76.3 万 WR/s；每 WR 一个完成，又要处理相同数量的完成记录。放大有效 slice、批量 poll 和减少每 WR 固定工作，都有明确的性能动机。

| 实际 slice | 50 GB/s 所需 WR/s，理论估算 |
|---|---:|
| 64 KiB | 762,939 |
| 128 KiB | 381,470 |
| 256 KiB | 190,735 |
| 512 KiB | 95,367 |
| 1 MiB | 47,684 |

64 KiB 对应平均每 1.31 μs 完成一个 WR 的处理量。这是**流水线平均处理能力**，不要求每个 WR 的端到端时延小于 1.31 μs。

用户报告的大工作量场景约 30 GB/s 是重要参考：说明这条 UB 路径存在更高吞吐的可能。首轮重点是让小切片负载也得到足量供给和更高的软件处理效率。最终以同一负载的新旧对照为准。

## 3. 实验范围和指标

首轮使用双节点、单方向、一个目标设备、一个 endpoint、主机内存到主机内存。应用发起线程固定为 1；transport worker 数另外记录。先做 WRITE，再用 READ 复核最好配置，随后回到原始业务的内存类型和方向。

每个配置记录以下数据，第一项是主要排序指标：

- **有效吞吐 GB/s：** 测量期成功完成的 payload bytes ÷ 测量秒数；同时保留每批传输时延。
- **实际工作量：** 应用 block、batch、内部 slice 长度、Jetty 数、WR 深度、成功 WR 数与失败数。
- **队列使用：** 每 Jetty posted WR、最大在途 WR、post 调用次数、poll 返回总数、poll 空返回次数。
- **CPU：** 各 post/poll worker 的 CPU 使用率；必要时用 `perf stat` 看 cycles、instructions 和 context switches。
- **约 20 ms 现象：** 仍在不在、每次发送量是否变大、停顿是否缩短。精确定位交给配套方案，不阻塞吞吐扫描。

每点测量 20 s，3 次交错运行基线/候选。连续三次都有明显提升，优先保留；中位吞吐提升约 10% 以上可直接作为有价值候选；3%–10% 再补两次；小于约 3% 先视作未见明确收益。该分档只是实验排序工具，不是产品验收门限。测试前后各做一次低频数据内容确认即可，不在吞吐热循环里逐字节校验。

## 4. 首先打通一套明确走 UB 的 benchmark

### 4.1 源码已存在和需要增加的参数

| 参数 | 当前源码状态 | 本轮用途 |
|---|---|---|
| `MC_MAX_WR` | 已有 | 每 Jetty/QP 软件在途深度 |
| `MC_SLICE_SIZE` | 已有 | 内部切片大小；必须由较大的应用 block 才能实际用到 |
| `MC_WORKERS_PER_CTX` | 已有，支持 1–8，默认 2 | 单 worker 与多 worker 的调度/回收对照 |
| `MC_NUM_CQ_PER_CTX` | 已有，也设置 UB 的 JFC/JFCE 数 | 首轮固定默认；后续测试完成队列分布 |
| `MC_NUM_QP_PER_EP` | 已有 | RDMA 对照；不代表 UB 的 Jetty 数 |
| `MC_NUM_JETTY_PER_EP` | 当前没有解析 | 新增一个整数解析，赋给已有 `num_jetty_per_ep` |
| `MC_UB_POLL_BATCH` | 本轮新增 | 单次 provider poll 容量，默认保持 64 |
| `MC_UB_POLL_BUDGET` | 本轮新增 | 每轮每 JFC 最多处理的完成数，默认保持 64 |
| `--classic_transport=ub\|rdma` | 本轮新增 benchmark 参数 | 禁用自动安装，显式选择一个传输 |
| `--classic_topology=<json路径>` | 本轮新增 benchmark 参数 | 两端各自指定一个本地设备的拓扑 |

新增实验参数在进程启动时读取，不在每次 post/poll 中调用 `getenv()`。保留现有默认行为，只有指定实验参数才走新路径。

### 4.2 最小 benchmark 改动

修改 `benchmark/utils.cpp`、`utils.h` 和 `te_backend.cpp`：

1. 增加上述两个 `classic_*` 参数并传入 `XferBenchConfig`。
2. 指定 `classic_transport` 时，创建 `TransferEngine(false)`，关闭自动拓扑发现与多传输安装。
3. 按现有方式初始化 P2P metadata；读取 `classic_topology` 文件为字符串。
4. 构造 `void* args[2] = {topology_json.data(), nullptr}`，在注册 buffer **之前**调用 `installTransport("ub", args)` 或 `installTransport("rdma", args)`。拓扑字符串在调用期间有效即可。
5. 保留后续 buffer 注册、请求构造与完成查询路径，启动时打印 transport、设备名、应用线程与 transport worker 数。

此方式直接复用 `TransferEngineImpl::installTransport()` 已有的 `args[0]` 拓扑 JSON 解析，不需要另写一个传输引擎，也不需要把 `--xport_type=ub` 塞进 TENT 的参数列表。TENT 是 Mooncake 另一套传输实现，本实验针对 classic 路径。

每端生成自己的拓扑 JSON，示例中 `LOCAL_DEVICE` 替换为本端实际 URMA 设备名或 RDMA HCA 名；CPU NUMA 节点按本机填写：

```json
{"cpu:0":[["LOCAL_DEVICE"],[]],"cpu:1":[["LOCAL_DEVICE"],[]]}
```

AI agent 用本机设备列表和已有 UB benchmark 脚本自动确定名称。两端设备名可以不同；每份 JSON 只填本端名称。若实验环境已有能够明确选择 URMA 的原始 benchmark，先直接复用它测基线；上述补丁用于统一后续 UB/RDMA 对照。

### 4.3 构建与启动

以下命令在**目标 Linux 实验节点**执行。`SRC` 指该节点的 Mooncake 源码目录，`TEBENCH` 指本次构建出的可执行文件。新增参数需先按 4.2 实现。

```bash
cmake -S "$SRC" -B "$SRC/build/ub-perf" \
  -DUSE_UB=ON -DUSE_TENT=OFF \
  -DUSE_ASCEND=OFF -DUSE_ASCEND_DIRECT=OFF \
  -DUSE_ASCEND_HETEROGENEOUS=OFF -DUSE_UBSHMEM=OFF \
  -DBUILD_BENCHMARK=ON -DWITH_STORE=OFF -DWITH_STORE_RUST=OFF
cmake --build "$SRC/build/ub-perf" --target tebench -j
```

沿用实验节点已有依赖安装。agent 保存实际构建命令和结果，构建通过后直接做双端小数据读写，不先建设大批 mock 测试。

目标端：

```bash
export MC_MAX_WR=256 MC_SLICE_SIZE=65536 MC_WORKERS_PER_CTX=2
"$TEBENCH" --backend=classic --classic_transport=ub \
  --classic_topology="$LOCAL_TOPOLOGY" \
  --seg_type=DRAM --total_buffer_size=1073741824
```

目标端输出 `To start initiators ... --target_seg_name=...` 后，agent 把**输出中的真实名称**写入发起端 `TARGET_SEGMENT`。当前 P2P 代码使用主机名，不能自行假定 `--seg_name` 改变了它。

发起端，8 MiB 小批：

```bash
export MC_MAX_WR=256 MC_SLICE_SIZE=65536 MC_WORKERS_PER_CTX=2
"$TEBENCH" --backend=classic --classic_transport=ub \
  --classic_topology="$LOCAL_TOPOLOGY" \
  --target_seg_name="$TARGET_SEGMENT" \
  --seg_type=DRAM --total_buffer_size=1073741824 --op_type=write \
  --start_block_size=65536 --max_block_size=65536 \
  --start_batch_size=128 --max_batch_size=128 \
  --start_num_threads=1 --max_num_threads=1 \
  --duration=20 --request_interval_us=0
```

512 MiB 足量供给：同一命令将 `start_batch_size/max_batch_size` 改为 `8192`。`64 KiB×8192=512 MiB`，可覆盖本轮最大 4096 个 64 KiB WR 的名义聚合窗口及下一轮补充。两端 1 GiB buffer 覆盖该范围。正式脚本由 agent 自动启动两端、读取目标端名称并收集结果，不要求用户手工逐点运行。

当前 benchmark 内建约 1 s 预热，首轮直接用它。只有观察到首轮/末轮明显不同或温度、连接初始化影响很大时，再延长同进程预热；不为了满足一个预热数字先改一轮 benchmark。

## 5. U1：先扫单 Jetty WR 深度，最快确认窗口收益

**代码改动：** 无；沿用当前单 Jetty 实现。

运行 `MC_MAX_WR=128/256/512/1024/2048`，slice 固定 65536，worker 固定 2；8 MiB 与 512 MiB 两种负载分别测。当前源码请求创建 JFS depth=2048，因此本轮先以 2048 为扫描顶点；设备若只成功支持更小值，就在成功范围内扫描，不展开通用设备能力框架改造。

| 现象 | 下一步 |
|---|---|
| 512 MiB 吞吐随 WR 深度明显上升，小批变化小 | 窗口和持续供给确有收益，保留最好深度，进入 U2 |
| burst 字节增加，平均吞吐增加，间隔仍约 20 ms | 扩窗口有效，但间隔是另一问题；两项成果分别记录 |
| 实际在途深度没有达到设置值 | 检查 pending、JFC 余额、分片分布和生效配置，先让实验真正施加该深度 |
| 深度已打满但吞吐不再上升 | 出现处理速率或链路上限，转 U3/U4，不继续放大窗口 |

16 MiB 在 50 GB/s 下理论约 0.336 ms 发完。若软件只能每 20 ms 才补充一次，16 MiB/20 ms 约 0.839 GB/s；这是解释数量级的简化模型，不能替代测量。扩窗口用于检验性能空间，配套方案用于把补充间隔缩短。

## 6. U2：多 Jetty 一次遍历提交

### 6.1 改动文件

- `src/config.cpp`：解析 `MC_NUM_JETTY_PER_EP` 并打印生效值。
- `include/transport/kunpeng_transport/urma/urma_endpoint.h`：新增轮转起点 `next_jetty_index_`。
- `src/transport/kunpeng_transport/urma/urma_endpoint.cpp`：修改 `UrmaEndpoint::submitPostSend()`。

不修改握手协议格式。当前握手已经交换 Jetty 列表，实验双端配置相同的 Jetty 数即可。

### 6.2 提交算法

继续使用现有 endpoint 写锁。一次 submit 按轮转顺序扫描全部 Jetty：

```text
offset = 0
for each Jetty starting at next_jetty_index:
    room = max_wr - current_jetty_depth
    n = min(room, pending_count - offset, shared_JFC_remaining)
    if n <= 0: continue
    reserve n shared_JFC credits
    build n WRs for pending[offset : offset+n]
    increment this Jetty depth; provider post this chain
    keep existing partial-failure processing
    offset += attempted_count
erase only attempted prefix from pending, once after the loop
advance next_jetty_index
```

共享 JFC 在多个 endpoint/worker 并发时，用一个简单 CAS 循环预留可用额度，成功 completion 继续沿用现有归还路径。本轮不引入 token/epoch/通用账本。若只跑单 endpoint，也保留这个局部原子预留，避免换成多流时测到超额提交带来的假收益。

计量 payload 时，在构造 WR 的循环里累加每项长度或前缀和；post 后不重新解引用已可能完成的 Slice。保留现有错误处理即可，失败配置报错结束该 run，不在本轮建设异常恢复系统。

先比较：

| 比较 | 配置 | 解释 |
|---|---|---|
| 相同每队列深度 | Jetty=1/2/4，WR=256 | 总容量与队列数同时增加，观察实际吞吐收益 |
| 相同总 WR | 1×512、2×256、4×128 | 区分多队列执行能力与单纯增加容量 |
| 组合候选 | 1/2/4 × WR 512/1024 | 找到收益拐点，不必把所有参数做全排列 |

同一 endpoint 的多个 Jetty 当前共享一个 JFC；`4×1024=4096` 已到默认单 JFC 的名义额度。增加 JFC 数不会自动扩大这个 endpoint 的容量。首轮不改 JFC depth，达到该边界就转处理速率实验。

### 6.3 顺手消除两个多余工作

导入 Jetty 的 `imported_jetty_map_.find()` 当前在每个 WR 内执行。把查找移到每个 Jetty 批次外，只查一次，再给本批 WR 使用同一个目标对象。

避免为了实现 planner 每次新建多组大 vector。WR/SGE 暂存可在 endpoint 内按实际需要扩容后复用，受现有写锁保护。单独保留“只轮转”和“轮转+缓冲复用”两个实验版本，就能看清收益来自哪里。

## 7. U3：改变有效 slice，拆开 WR 处理率和在途字节量

只把 `MC_SLICE_SIZE` 从 64 KiB 调到 1 MiB，而应用 block 仍为 64 KiB，没有意义：请求本身不够大，实际 WR 不会变大。

改用 **1 MiB block × 512 batch = 512 MiB**，然后扫描 slice=64/128/256/512 KiB/1 MiB。源码可能合并尾片，记录真实长度即可；整除的大 block 使首轮结果更直观。

做两条扫描线：

| 扫描线 | Jetty | WR/Jetty | 用途 |
|---|---:|---:|---|
| 固定 WR 数 | 固定 1 或 U2 最好值 | 固定 256 | 看大 slice 的综合收益，包含增加在途字节的效果 |
| 固定名义总窗口 32 MiB | 1 | slice 64/128/256/512/1024 KiB 对应 WR 512/256/128/64/32 | 主要看减少 WR 数和完成处理成本的收益 |

若固定窗口时，大 slice 仍显著更快，说明小 WR 的软件/provider 处理成本很重要；若只有固定 WR 数时变快，容量增加贡献更大。两条线一起测，避免把全部收益归因于某一个因素。

达到大 slice 后，记录同样 GB/s 对应的 WR/s 与 worker CPU。若吞吐已接近该设备/内存路径可提供的水平，停止扩 slice，选择最小的有效粒度。实际业务离散地址是否支持大 slice，留到业务回归判断；不把它当作微基准实验的前置障碍。

## 8. U4：批量回收完成，尽快把窗口补满

**位置：** `src/transport/kunpeng_transport/ub_context.cpp::UbWorkerPool::performPoll()`，以及 `urma_endpoint.cpp::UrmaContext::poll()`。当前每次 JFC poll 最多 64 条，worker 顺序 post→poll。

增加两个实验参数：

- `MC_UB_POLL_BATCH=64/256`：单次 `urma_poll_jfc()` 的容量，数组/缓冲大小同步调整。
- `MC_UB_POLL_BUDGET=64/256/1024`：每轮每 JFC 最多处理的完成数。

一次 worker poll 对每个所负责 JFC，连续 poll 到“返回不足本次容量、返回零、或达到 budget”即转下一个 JFC；下一轮继续 post。沿用现有 completion 处理和 slice 终态代码，不重写它们。

分别测试：

| 版本 | 改动 | 要回答的问题 |
|---|---|---|
| P0 | batch=64，budget=64，原顺序 | 基线 |
| P1 | batch=64，budget=256/1024 | 多轮排空是否减少补充周期 |
| P2 | batch=256，budget=256/1024 | 较大单次 poll 能否降低调用与循环成本 |
| P3 | 上一最好配置，每个 JFC 处理完立即归还相关 Jetty depth | 避免等待其他完成队列遍历，提前让 post worker 获得额度 |
| P4 | 固定回收预算，比较 post→poll、poll→post、poll→post→poll | 观察回收与提交顺序是否降低 refill 等待 |

P3 使用每 JFC 局部的 depth 累计，已经释放的条目不再在函数末释放。P4 每种顺序使用相同 poll 预算，避免同时改变顺序和工作量。观察高水位、空 poll 比例与吞吐，选择更有收益的策略；不假定“poll 越多越快”。

U4 建议使用 U2 最好窗口、64 KiB slice、512 MiB pending。窗口先固定，才能把收益归到回收策略。

## 9. U5：减少应用批次和状态查询的固定成本

classic benchmark 目前一批 submit 后逐 task 查询，全部完成后释放，再发下一批。因此增大应用 block/batch 本身也会减少每字节的控制成本。

先固定内部 slice=64 KiB，比较 512 MiB 总字节：

| 应用 block | batch | 总 payload |
|---:|---:|---:|
| 64 KiB | 8192 | 512 MiB |
| 256 KiB | 2048 | 512 MiB |
| 1 MiB | 512 | 512 MiB |
| 8 MiB | 64 | 512 MiB |

增加 benchmark 本地计时：`allocate/build`、`submitTransfer`、`getTransferStatus` 循环、`freeBatchID`、完整批次循环。不需要逐 task 日志，累计调用数和总耗时即可。

若 `getTransferStatus` 和 batch 空档占比高，再实现 **2 个批次重叠** 的实验：两个独立地址区域，一批正在完成时允许另一批提交；固定总 pending bytes 后对比单批/双批，避免只是多加了供给。最后有收益再测 4 个批次。

还应注意 `ub_transport.cpp::submitTransferTask()` 的提交水位使用 `max_wr*num_qp_per_ep`，并且 `nr_slices` 只在 slice 非缓存新分配时增加。这不是约 20 ms 定时器，但可能使首轮和热身后的“切片→入队”行为不同。先计量 `submitTransfer` 与 context 入队时间；若这里占比明显，再试用独立 pending 数计水位，扫描 128/512/2048 个 slice 分批入队。该项单独比较，不在改 Jetty 时顺手改掉。

## 10. U6：worker 和 CPU 布局

先扫 `MC_WORKERS_PER_CTX=1/2/4`，应用线程仍为 1。当前 UB 每个 worker 都 post→poll；同一 endpoint 的 post 队列按 shard 分配，增加 worker 不保证同一 endpoint 的 post 工作会并行。

对每种 worker 数做两组：默认 socket 绑定、每个活跃 worker 分配一个同 NUMA 的独立物理核。发起线程与 poll worker 分开。`bindToSocket()` 只是允许线程使用整个 socket 的 CPU 集合，不是独占核。

若 worker=1 更好，优先使用它，说明跨线程回收/调度成本值得减少。若 2/4 更好，再看是否由 poll 并行或多 endpoint 贡献。只有 CPU 数据显示 post/poll 串行是瓶颈时，再做 UB 专职 post/poll 的小分支；先不用它重构全部 worker 模型。

当端口负载持续而 CPU 占用还高时，可试将每 WR 的 `getCurrentTimeInNano()` 合并为每提交链一次时间，再给链内 WR 使用同一提交时间；其用途是传输超时记账，保留该含义。该项属于进一步减少热路径调用的实验，单独测量。

## 11. U7：完成通知数量的后续实验

如果 U1–U6 后仍显示完成处理占用主要 CPU，单独尝试选择性完成：同一 Jetty 中每 N=4/16 个 WR 仅请求一次成功完成，并据有序完成推进已完成前缀。

这项需要先确认实际 URMA provider 的有序完成和失败返回语义，否则软件无法正确计算成功 bytes。首轮保留每 WR 一个完成，已经可以验证窗口、并行与 poll 的大部分空间。选择性完成不是当前首轮的必做项，也不扩展成通用错误恢复项目。

## 12. 建议的执行顺序与收敛方式

1. 复用现有 UB benchmark 测一次 8 MiB 与大工作量；必要时完成 4.2 的明确选路小补丁。
2. **先做 U1。** 仅环境参数就能知道加深窗口有没有用。
3. **再做 U2。** 实现一次遍历多 Jetty，比较相同总窗口和增大总窗口。
4. **做 U3/U4。** slice 扫描与完成回收各固定其他参数，找到 WR 处理速率的拐点。
5. U5/U6 根据 CPU 与批次计时选择优先项；保留最有收益的两三项组合复测。
6. 最好配置分别跑 READ、WRITE 和原始业务；同时记录 20 ms 是否仍存在。

初始高价值候选是 **2 Jetty、每 Jetty 512/1024 WR、256 KiB slice、较大批量 poll**。它是待测候选，不是最终推荐配置；32 MiB 等窗口对照和原小批实验会告诉我们收益来自哪里。

只要某项改动在同负载下稳定提高吞吐，就保留它；即使 20 ms 还未解释，也已经得到有效的性能结果。若只是 burst 变大而空闲未缩短，转配套方案处理空闲时间。

## 13. 交给 AI agent 的执行指令

> 目标是在实验环境确认 Mooncake classic UB 传输的性能上限及优化空间。按本方案先建立 8 MiB 和 512 MiB 对照，随后依次执行 U1、U2、U3、U4；U5/U6 依据 CPU 与阶段耗时选择。直接做最小实验补丁和真机测试，不建设 token/generation/故障恢复框架，不把完整单测矩阵作为跑性能的前提。
>
> 你负责 benchmark 参数、明确 transport 安装、Jetty 轮转、poll 预算和性能计量所需文件。保留现有恢复路径；诊断在 post 前计算长度，完成发布后不再读取 Slice。你不是独自在代码库工作，不回滚他人改动；记录自己的差异。若另一 agent 同时研究 20 ms，用独立实验 checkout 或按补丁分别跑，先不要合并变量。
>
> 不要把新增参数当已有功能。先实现，再编译、双端小量读写确认，然后跑数据。自动定位实际设备、CPU 和目标 segment，保存运行命令。当前 P2P segment 使用目标端输出名称，不套用 V1.2 的自定义 segment 示例。
>
> 以实际有效吞吐排名；每点 20 s、3 次交错复测。保留配置、成功 bytes、运行时间、错误数、worker CPU、WR/s 与队列高水位。重要参数变化分别测，最后组合最好项。看到某项无收益时说明瓶颈转移到哪里，并继续下一个有证据的候选。
>
> 最终交付源码补丁、可重复双端运行脚本、results.csv、简明分析和最好配置。明确每项收益是扩窗口、减少 WR、减少批次成本还是加快回收；未跑到的硬件点直接列待测。不要把方案推导写成实测值。所有实验脚本和结果放指定实验目录；本报告项目的中间文件放 tmp。更新项目索引与变更记录。

## 14. 最终结果表

```text
run,variant,transport,op,block_bytes,batch,app_threads,workers,
jetty_count,max_wr,slice_bytes,poll_batch,poll_budget,payload_bytes,
elapsed_s,GBps,posted_wr,poll_calls,poll_empty,worker_cpu_pct,
max_outstanding,error_count,burst_gap_ms,command,result_path
```

最终说明四件事即可：原始吞吐、最好吞吐、贡献最大的改动、原业务能否获得同类收益。20 ms 还存在时，附对应配置和时间线，供配套实验继续使用。
