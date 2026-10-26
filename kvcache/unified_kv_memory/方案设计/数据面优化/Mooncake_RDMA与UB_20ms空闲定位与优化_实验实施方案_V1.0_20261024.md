# Mooncake RDMA/UB 两次 burst 之间约 20 ms 空闲：定位与优化实验方案

**版本：** V1.0，2026-10-24  
**用途：** 交给 AI agent，先把约 20 ms 分解到具体阶段，再用对照实验缩短它。  
**源码参考：** `D:/codes/vllm/Mooncake` 的 classic Transfer Engine，参考提交 `f90ae691f109e49a60920e0c8abbf7e572826d8c`。  
**配套方案：** [UB 传输性能优化实验](Mooncake_UB传输性能优化_实验实施方案_V1.0_20261024.md)。本方案可独立执行，不要求先完成 UB 优化。  
**当前状态：** 方案完成，本次未修改源码或执行硬件实验。

## 1. 本轮直接要得到什么

实验要回答三个明确问题：

1. 描述符已经一次性提交以后，下一个 burst 为何还要等待约 20 ms？
2. RDMA 与 UB 的停顿是否落在同一阶段，能否用同一项干预缩短？
3. 缩短停顿后，平均传输带宽能提高多少；若暂时不能缩短，扩大窗口能改善多少？

采用“复现→最少打点→分类→只改一个因素→复测”的流程。首轮不建设通用 trace 平台、请求 token、对象代次、故障恢复和大批 mock 用例。实验使用稳定连接、固定 endpoint、短时间捕获；保留现有完成处理。发生错误的 run 不拿来算正常吞吐，保存错误后重新跑即可。

## 2. 对 V1.2 的审核结论

[V1.2](Mooncake_URMA与RDMA传输窗口和20ms间隔_代码修改测试与打点方案_V1.2_20261024.md)已经纠正了“拿任意 completion 时间减任意 post 时间”的问题，也正确区分 provider poll、完成处理和两级额度回收。应保留这些阶段划分。

但它对当前实验过重：从状态机和完整测试矩阵开始，仍缺少“看到哪类 20 ms 后直接尝试哪个补丁”的操作顺序。本方案把前置工作压缩到明确选路、小量读写确认和短时 trace，随后直接开展性能干预。

还应纠正两处启动细节：classic P2P 模式使用目标主机名作为 segment，客户端应读取目标端真实输出；`MC_FORCE_HCA` 在 `USE_UB` 构建中不能保证排除 UB。独立复现需明确安装 RDMA 或 UB，不能只改变一个环境变量后假定 transport 已经切换。

## 3. 当前最有价值的判断

RDMA 是远程直接内存访问；UB 是统一总线，URMA 是其统一远程内存访问编程接口。WR 是设备工作请求，slice 是引擎内部切片。RDMA 的 QP/CQ 是收发队列对/完成队列，URMA 的 Jetty/JFC 是收发队列对象/完成队列。

本次读到的代码中：

- 活跃 UB worker 执行 `performPostSend()`→`performPoll()`，没有显式 20 ms sleep。
- RDMA 默认 `MC_WORKERS_PER_CTX=2` 时，worker 0 负责 poll，其他 worker 负责 post；UB 每个 worker 都执行 post/poll。两者的线程模型不同。
- 两种 worker 都只有在提交已经处理完、进入空闲判断后，才可能因 100 ms 条件进入 1 s 条件变量等待；它不是尚有 backlog 时的固定 20 ms 发送节拍。
- UB/RDMA 的单次 poll 上限都为 64。JFC/CQ 额度先释放，Jetty/QP 深度又在后面释放；这两个阶段之间的工作可能影响下一轮提交。
- 默认只绑定 NUMA socket，即一组 CPU；没有为 worker 分配独立物理核。

因此，**第一轮优先查 CPU 服务间隔、completion 回收和 post 控制路径，直接做绑核、worker 数与 poll/credit 补丁对照。** 这些动作成本低、判别力强。只有原始 provider poll 仍持续看不到完成时，再下探 provider/设备和内存路径。

用户已经确认全部描述符一次提交。新增入队打点用于识别中间处理时间，不将“上层每 20 ms 才产生一批”重新作为默认解释。

## 4. 先定义两个时间，不把间隔和空闲混为一谈

burst 指端口出现的一段集中数据传输。

- **起点间隔 P：** 下一次 burst 起点减本次 burst 起点。
- **空闲时间 I：** 本次 burst 结束至下次 burst 开始。
- **活跃时间 A：** 本次 burst 持续发送的时间。

若是稳定周期，`P≈A+I`。原始讨论中“20 ms”有时指相邻流量事件间隔，因此报告同时列 P 和 I。否则 RDMA 发送 3 ms 后等 17 ms，与发送后再等 20 ms，会被错误地混为同一种情况。

约 17.285 MB/35.463 MB 是既有讨论的端口观察，先复现，不把它们当作应用 payload。软件名义窗口为 UB 16 MiB、RDMA 32 MiB；它支持窗口解释，但本实验直接记录真实 posted bytes 与在途数量。

主结果用成功 payload/总耗时；端口计数用于看流量形态。3 ms 采样足以帮助观察约 20 ms 级空档，分不开理论约 0.34 ms 的 UB burst 内部速率。首轮不用为了分辨 0.34 ms 而停下整个实验。

## 5. 建立三层复现，快速确定调查范围

| 层次 | 跑什么 | 用来判断什么 |
|---|---|---|
| L1 原始业务 | 原 vLLM/Mooncake 传输，保留全部描述符一次提交及原内存类型 | 复现用户真正关心的 20 ms |
| L2 Transfer Engine 独立 benchmark | 单 endpoint、一次提交足量数据、steady-state 连续运行，分别选择 UB/RDMA | 判断脱离业务以后间隔是否仍存在 |
| L3 provider 原始 benchmark | 已部署的 URMA/RDMA 原始传输脚本，或最小直接 post/poll 循环 | 把 Mooncake 队列/worker 路径与 provider/设备路径分开 |

**先跑 L1+L2。** L2 没有 20 ms，就直接比对 L1 的 CPU、元数据、连接、切片入队和调用轨迹，不先改固件；L2 也有，则按下面阶段时间线定位。L3 用在 provider 方向仍有疑问时，不是所有实验的前置门槛。

L2 必须同时保留 8 MiB 小批和大批。64 KiB×128 只有 8 MiB，填不满默认 UB 16 MiB/RDMA 32 MiB 窗口，不能独立检验回收循环。推荐 64 KiB×8192=512 MiB，内存不足可用 128 MiB，并把本轮总窗口限制在 64 MiB 以内。

应用线程固定为 1，先保留原 transport worker 数，再做 E2 扫描。首轮 WRITE，随后 READ；实际业务内存类型另做同方向对照。

## 6. 独立启动 UB/RDMA 的最小方法

已有明确选路的原始脚本可先直接使用。若统一采用当前 tebench，给 benchmark 增加以下两个**实验参数**，不是当前已有参数：

```text
--classic_transport=ub|rdma
--classic_topology=/path/to/local-device-topology.json
```

在 `benchmark/utils.cpp`、`utils.h`、`te_backend.cpp` 中实现：指定 transport 时创建 `TransferEngine(false)`；按现有方式 init；读取拓扑 JSON；在注册 buffer 前调用 `installTransport(transport, args)`，其中 `args[0]` 是该 JSON 字符串。这样一端只安装一个 transport。该接口与拓扑参数用法已有源码，不需要改引擎的全局自动选路规则。

各端 JSON 示例：

```json
{"cpu:0":[["LOCAL_DEVICE"],[]],"cpu:1":[["LOCAL_DEVICE"],[]]}
```

将 `LOCAL_DEVICE` 换成本端所测设备，CPU 节点覆盖本机实际 NUMA 节点。NUMA 指 CPU 到内存和设备的访问位置关系。agent 自动从节点配置和原脚本读取设备信息，两端使用各自本地名称。

在 Linux 实验节点构建 classic、`USE_UB=ON`、`USE_TENT=OFF`、`BUILD_BENCHMARK=ON`，沿用已安装的传输依赖；主机内存微基准可关闭 Ascend/Ascend Direct/UBShmem 自动传输。TENT 是另一套引擎，本轮不研究它。构建模板与配套 UB 方案第 4 节一致，可独立使用。

目标端，`TRANSPORT=ub` 或 `rdma`：

```bash
"$TEBENCH" --backend=classic --classic_transport="$TRANSPORT" \
  --classic_topology="$LOCAL_TOPOLOGY" \
  --seg_type=DRAM --total_buffer_size=1073741824
```

发起端读取目标端输出的 `TARGET_SEGMENT`，然后：

```bash
export MC_MAX_WR=256 MC_SLICE_SIZE=65536 MC_WORKERS_PER_CTX=2
"$TEBENCH" --backend=classic --classic_transport="$TRANSPORT" \
  --classic_topology="$LOCAL_TOPOLOGY" \
  --target_seg_name="$TARGET_SEGMENT" \
  --seg_type=DRAM --total_buffer_size=1073741824 --op_type=write \
  --start_block_size=65536 --max_block_size=65536 \
  --start_batch_size=8192 --max_batch_size=8192 \
  --start_num_threads=1 --max_num_threads=1 \
  --duration=20 --request_interval_us=0
```

目标端也显式设置本轮相同队列参数。8 MiB 对照将 batch 改为 128。两端启动日志各保存一行实际 transport、设备和队列配置。一次简单读写确认后立即开始测量，不等完整 SDK 能力审计和异常注入测试。

## 7. 七组最小打点，足够分解 20 ms

### 7.1 打哪里

全程用同机 `CLOCK_MONOTONIC` 或对应单调时钟；端口采集也记录该时钟。首轮分析发起节点即可，不需要先做跨主机纳秒校时。

| 点位 | 源码位置，行号为本次快照参考 | 记录内容 |
|---|---|---|
| A 提交和入队 | `UbTransport::submitTransferTask()`；UB `ub_context.cpp::submitPostSend()` 约 L211–297；RDMA `worker_pool.cpp::submitPostSend()` 约 L182–334 | TE submit 开始/结束、context 入队的数量/字节、worker 出队时间 |
| B endpoint 尝试提交 | URMA `urma_endpoint.cpp::submitPostSend()` 约 L871；RDMA `rdma_endpoint.cpp` 约 L915 | pending 数、Jetty/QP 深度、JFC/CQ 余额；未提交原因：无 pending、WR 满、完成队列额度满 |
| C 真正 provider post | URMA `urma_post_jetty_send_wr()` 约 L930–947；RDMA `ibv_post_send()` 约 L984–1014 | 进入/返回时间，WR 数，事先累计的 payload bytes，返回码 |
| D 原始 provider poll | URMA `UrmaContext::poll()` 内 `urma_poll_jfc()` 约 L527；RDMA context 原始 CQ poll 调用 | 进入/返回时间、原始返回数；连续空 poll 的开始/结束 |
| E 完成处理和两次额度释放 | UB worker 约 L442–447；RDMA worker 约 L716–722 | provider 返回后处理耗时；JFC/CQ 额度释放；Jetty/QP 深度释放 |
| F 下一次 post | 与 C 相同位置，离线按 endpoint/队列连接 | 额度可用且 pending 非空后，到下一次真正 post 的时间 |
| G worker 阶段 | UB `transferWorker()` 约 L517–536；RDMA 约 L957–994 | loop begin、post/poll phase 起止、TID、角色；长锁等待或连接处理单独记录 |

UB 的 `context_.poll()` 包含完成解析，不能只包围它就叫“provider poll 耗时”。应在 `urma_poll_jfc()` 那一行测原始调用；RDMA 同样定位到底层 CQ poll。原始返回后到额度释放的时间归为软件完成处理。

### 7.2 怎样低成本实现

实验只保持固定连接，不做 endpoint 重建。每条记录包含：

```text
time_ns, tid, event, endpoint_ptr, queue_kind, queue_index,
completion_queue_ptr, pending, depth, outstanding, count, bytes, rc
```

`tid` 在线程启动时取一次。初始化时额外输出 Jetty/QP、depth 地址、JFC/CQ 的映射；同一次稳定运行里，指针足够作关联键。post/poll 在不同线程也按队列映射连接，**不用 thread-local 的 last_completion 来推断另一个线程何时完成**。

- 每线程预分配一个简单数组或 `reserve()` 后不再扩容的 vector，捕获 2–3 s，结束后统一写 CSV/二进制。热路径不调用逐条 `LOG(INFO)`。
- 记录每次 post；完成 poll 可以按 100 μs 小区间聚合 count，保留“长空 poll 后第一次完成”的精确时间与慢 API 事件。空 poll 也按约 100 μs 汇总，不逐调用写文件。
- worker phase 保留时间边界；不再只测“循环 end 到下一次 begin”，否则会漏掉函数内部的长停顿。完整时钟采样过重时，先用阶段进度快照加调度 trace 捕获毫秒级空档。
- 只在对应队列 `outstanding>0` 时累计空 poll；没有在途请求的 idle 不算 completion 等待。
- 统计 bytes 在 post 前构造 WR 时累加；completion 侧需要的元数据在 `markSuccess()` 前读完。无需为观测再引入长寿命请求对象。
- 数组满时标记本次捕获截断，扩大数组或缩短捕获后重跑。首轮无需实现复杂滚动导出和异常退出协调。

先关打点跑吞吐，再开打点看原来的约 20 ms 是否还存在。若打开后波形明显改变，减少记录字段/频率；性能排序以关闭打点的 run 为准。

### 7.3 离线实际计算什么

以**仍有 pending、相关队列窗口已满、等待下一次有效提交**的一轮为分析对象：

| 量 | 含义 |
|---|---|
| `post_call` | provider post 调用本身多久 |
| `post_to_first_completion` | 本轮提交后，该队列首次观测完成的时间；包含设备运行和完成可见过程 |
| `completion_process` | 原始 poll 返回到本批完成处理结束 |
| `completion_to_jfc/cq_credit` | 完成可见到完成队列额度释放 |
| `completion_to_jetty/qp_credit` | 完成可见到发送队列额度可用 |
| `credit_to_next_post` | 真正解除本次阻塞的相关额度可用后，pending 非空时到下一次 post |
| `worker_phase_gap/off_cpu` | 哪段代码长，线程是否在该段未获 CPU |

不用等待整批全部完成才定义“额度回来”：只要释放的数量已经足够让相关队列继续发送，就开始观察下一次 post。不同队列的阶段可能交叠，不把这些时长无条件相加成总时间；先画同一时间轴看实际阻塞区间。

## 8. E1：业务 vs 独立 TE，先确定 20 ms 在哪一层

按 L1 原业务和 L2 大批 benchmark，各跑 UB、RDMA。保留原队列/worker 参数；短时捕获 2–3 s，至少覆盖数十次发送事件。

| 结果 | 直接行动 |
|---|---|
| 只有业务有 20 ms | 继续记录业务进程里相同 post/poll worker 的 CPU、调用次数和 pending；比较资源竞争、内存类型、其他 endpoint 和同步调用 |
| TE 独立运行也有 20 ms | 进入 E2–E5，优先改 worker 调度和额度回收 |
| 改成大批以后间隔消失，小批仍有 | 重点测批次构造、状态查询、完成后再 submit 的空档；执行 E6 |
| UB 与 RDMA 的长区间出现在不同阶段 | 分别处理，不强求同一根因 |

业务侧也补一个“应用调用 submit 前后”的时间即可，不全面铺开上层 trace。用户已一次提交的这一批若全过程 pending 持续存在，后续排查就锁定在 transport 层。

## 9. E2：独立核、worker 数和 CPU 配额，第一优先级干预

### 9.1 先看线程有没有运行

AI agent 自动读取进程 PID/TID，并区分发起线程、UB worker、RDMA post worker、RDMA poll worker。可在 worker 启动时用 `pthread_setname_np()` 加短名称，便于识别。

```bash
ps -T -p "$PID" -o pid,tid,psr,pcpu,stat,comm
lscpu -e=CPU,NODE,SOCKET,CORE,ONLINE
perf stat -p "$PID" \
  -e task-clock,cycles,instructions,context-switches,cpu-migrations \
  -o "$RUN/perf-stat.txt" -- sleep 5
perf sched record -o "$RUN/perf-sched.data" -p "$PID" -- sleep 3
perf sched timehist -i "$RUN/perf-sched.data" -p "$PID" \
  > "$RUN/perf-sched.txt"
```

`perf` 调度采集是短时定位 run，和无采集吞吐 run 分开。若该节点未启用 sched trace，就先读每线程 `/proc/$PID/task/$TID/schedstat` 的运行时间、排队时间和调度次数增量，再用可用的 ftrace/eBPF 调度工具细化。粗粒度 `pidstat` 可补 CPU 趋势，不能单凭它确定 20 ms 的具体调度边界。

### 9.2 四个直接对照

1. **原绑定方式，workers=2。** 保留默认现象。
2. **同 NUMA 独立核，workers=2。** 发起线程、post、poll 分配不同物理核。通过 `taskset -pc "$CORE" "$TID"` 设置，或新增实验用 worker 核列表，在线程启动时绑定。
3. **独立核，workers=1。** RDMA 此时同线程 post/poll；UB 也同线程。比较线程交接是否是关键。
4. **独立核，workers=4。** 检查更多 worker 是否提升或反而消耗 CPU；同一 endpoint 不会自动获得四路 post。

固定应用线程=1、slice、总窗口和传输方向。观察 20 ms 是否收缩，以及哪类 `off_cpu/credit_to_next_post` 减少。

### 9.3 同时查 cgroup CPU 限额

读取 `/proc/$PID/cgroup`，定位对应 cgroup 的 `cpu.max`、`cpu.stat`，在实验前后取差值；cgroup 是 Linux 对进程分组实施资源限制的机制。关注 `nr_throttled` 和 `throttled_usec` 是否在 burst 空档中持续增加。

若确认 quota throttling，直接做一次将实验进程放到无该配额限制的现有实验运行环境/宿主进程的对照，而不是继续调传输队列。只绑核不解除配额，可能完全不改变问题。变更实验启动资源配置即可，不需要改 Mooncake 资源管理架构。

**有价值结论：** 绑核或解除 CPU 配额后，空闲明显缩短、post/poll 间隔同步下降且吞吐提高，就先保留这个实验配置，继续验证原业务。只有运行时间线支持时才归因到调度；源码没有 sleep 并不意味着线程一直在 CPU 上。

## 10. E3：固定总窗口，对照“发得少”与“等得久”

首轮使用现成参数，不依赖多 Jetty 补丁：

| transport | 配置 | 名义总窗口，64 KiB slice |
|---|---|---:|
| UB | 1 Jetty × WR 256 | 16 MiB |
| RDMA | `MC_NUM_QP_PER_EP=1`，WR 256 | 16 MiB |
| UB | 1 Jetty × WR 512 | 32 MiB |
| RDMA | 2 QP × WR 256 | 32 MiB |
| 两者追加 | 在成功范围内继续扫 WR 1024/2048 | 检查容量变化趋势 |

保持 pending 足量，记录真实在途/posted bytes。初始 UB 是 1 Jetty，`MC_NUM_JETTY_PER_EP` 尚不是现有环境参数；本方案不要求先实现它。

| 结果 | 解释与动作 |
|---|---|
| 相同窗口下流量大小接近，但都仍隔约 20 ms | 软件窗口能解释发送量差异，继续 E2/E4/E5 处理等待 |
| 扩窗口平均带宽提升，20 ms 不变 | 扩窗口是有效补偿；单列其收益，同时继续找间隔来源 |
| 相同窗口下 UB 空档更长或 poll 行为不同 | 传输实现/worker/provider 差异需单独分析 |
| 实际窗口未打满 | 先解决 pending、完成队列额度或分布问题，再解释窗口结果 |

该对照避免将 UB 小窗口与 RDMA 大窗口直接比较后，把所有差别归因于物理带宽。

## 11. E4：poll 与额度回收的三个小补丁

当 trace 显示 completion 已可见，但窗口补得慢，优先执行本节；也可把它作为低成本筛查，与原实现直接 A/B。

### 11.1 每个完成队列处理完就释放相关 Jetty/QP 深度

当前 UB/RDMA 在每个 JFC/CQ 后先减少该完成队列 outstanding，发送队列 depth 则放在整个队列扫描结束后统一减少。

实验修改：把本批完成对应的 `jetty_depth_set/qp_depth_set` 改为**每个完成队列局部累计**，处理完该 JFC/CQ 立即减少对应发送队列 depth，然后继续下一完成队列；已经释放的条目不再在函数末重复释放。保留成功 Slice 的现有发布顺序与必要的先读字段，不为此更换 completion 数据结构。

对照看 `completion_to_jetty/qp_credit` 和下一次 post 是否前移。如果原来只有一个很快的完成队列，该项可能收益小；这是有信息的实验结果，不必强行扩大改动。

### 11.2 单次 poll 容量和连续回收预算

将硬编码 64 参数化为实验配置，UB/RDMA 分别测：

```text
单次 poll 容量：64 → 256
每队列每轮总预算：64 → 256 → 1024
```

每轮对该 JFC/CQ 连续 poll，直到返回不足本次容量、返回零或达到预算；然后交回提交路径。UB 和 RDMA 两套完成缓冲大小同步修改。每个候选记录吞吐、正/空 poll 数、两级额度延迟、CPU。

这项回答“窗口已经完成，但每次只捞一小批、补充不及时”是否影响性能。它也可能增加空 poll 成本，所以用数据选预算，不无限 drain。

### 11.3 活跃循环先 poll，再 post

UB 当前 post→poll，尝试 poll→post；再根据效果比较 poll→post→poll。先固定窗口与 slice，保持每轮预算一致。

RDMA workers=1 时同样可测顺序；默认 workers=2 已是专职 post/poll，不用套用“换顺序”这个动作，而应测独立核和提前释放额度。只在数据证明必要后，才对 UB 加专职 poll 的实验分支。

**选择标准：** gap 收缩、补充更及时且有效吞吐提高，保留补丁；只有 CPU 更高、吞吐没有提高的版本不保留。无需先补全产品级调度器。

## 12. E5：worker 一直 poll 但没有完成时，直接下探 provider

进入条件：有在途 WR、pending 非空、worker 确实在 CPU 上持续执行，原始 poll 近 20 ms 返回零；不是把外层线程停顿误叫成 provider 无完成。

依次做以下实验：

1. **原始 provider 对照。** 使用环境里已能跑通的 URMA/RDMA benchmark；固定 64 KiB、单发送线程、尽量相同的在途深度，持续 post/poll。不要拿其默认大消息结果代替同粒度对照。
2. **最小 post/poll 循环。** 若现有 benchmark 无法控制窗口，复用当前 endpoint/context 初始化做一个只发固定 WR 链、立即 poll、回收后立刻补充的实验程序。保留设备、内存注册、方向和完成策略，去掉 Mooncake 的软件队列、状态查询和连接查找热路径。
3. **READ/WRITE 对照。** 同样字节和窗口换方向，观察等待是否只在某种操作出现。READ 计数要采对应收发端口，不能只看发起端 TX。
4. **内存类型对照。** 主机内存与原业务加速器内存分别测。若只有后者长，继续检查该内存路径的注册、访问和同步处理，不改动已证明正常的 H2H 路径。
5. **provider CPU 分析。** 若 post 或 poll API 自身有长调用，短时采样 `perf record -o "$RUN/perf-provider.data" -g -p "$PID" -- sleep 3`，看时间消耗在调用内部还是线程被调出。若调用都很短而长期返回零，整理 WR 大小、在途量、方向、错误计数和时间线，再针对设备/provider 的完成产生与传输进度做下一轮实验。

原始 provider 同粒度运行没有 20 ms，研究焦点就留在 Mooncake。原始循环也出现相同等待，说明值得在 provider/设备/内存路径继续深挖。这里不强行把“poll 没看到完成”命名为固件合并通知；polling 场景也不能只调中断参数就当作定位完成。

## 13. E6：如果空档在应用查询或切片/入队阶段

### 13.1 批次完成到下一批 submit

classic benchmark 逐 task 忙轮询 `getTransferStatus()`，一批全部完成后才 free/重建/提交下一批。若 timeline 显示窗口早已空了，而下一批 submit 很晚：

- 给 `build/submit/status/free` 四段计时，测 64 KiB×8192 与 1 MiB×512，同为 512 MiB、内部 slice=64 KiB，区分任务数量成本。
- 用两个非重叠地址区做 2 个并行批次，保持总 pending bytes 一样；仍有收益再测 4 批次。
- 把发起线程放到独立核；若它与 poll 线程争 CPU/配额，再试 status 查询每完整轮做一次 `yield` 或短暂退避的实验。该退避只为降低竞争，单列结果，不能把人为等待混到传输完成时间里。

### 13.2 描述符构造、shard 队列和锁

若应用 submit 已开始，但 context/endpoint pending 很晚才出现，或 worker 在搬队列时长时间不 poll：

- `ub_transport.cpp::submitTransferTask()` 的水位目前引用 `max_wr*num_qp_per_ep`，并受新分配 slice 的计数影响。先统计实际入队次数和每次数量，再试独立 pending 数的 128/512/2048 slice 分批提交。
- 对锁内逐条搬运全部 slice 的部分，试 `swap/move` 出队列后再在锁外合并，或每轮限制搬运量，使 poll 有机会运行。
- 若 endpoint 锁等待长，先把 WR 缓冲复用、导入目标查找移出逐 WR 循环，缩短锁内工作；用单 post worker 对照确认锁竞争贡献。
- 若出现连接、redispatch、重试，先单列这些样本。初始化握手和错误路径不能代表稳定连接的每个 20 ms 周期；本轮稳定运行不需要额外开发重连机制。

用户已确认描述符一次提交，预期重点仍是后面的服务过程；只有入队时间线确实显示这里长，才做本节代码实验。

## 14. 端口数据怎样采、怎样与软件时间线一起看

沿用已有 UB PMU/RDMA 端口计数采集器，保存累计原值。PMU 指硬件性能计数器。每次读取额外记录本机单调时钟的读前、读后时间，用实际间隔差分；不把 `sleep(3ms)` 当成精确采样间隔。

- 先以现有约 3 ms 周期复现流量空档；如果读取成本允许，再降到 1 ms 或更细。
- 用同机时间绘图：软件 post、正 poll、两级 credit 释放、worker 运行区间，下面叠加端口字节差分。
- 以连续零增量区间估算 I，以非零流量段起点估算 P；使用真实采样周期给出时间分辨率。不用粗采样计算 UB burst 内瞬时线速。
- UB `tx_busi_flit_num` 先保留原计数；已有资料确认单位后再换算。软件 payload 和端口字节分别画，不强求二者逐字节相等。
- READ/WRITE 分别选择正确流量方向。首轮只做同节点时钟关联，避免跨主机时钟问题分散精力。

如果软件和端口时间线都持续前进，只有采集器显示固定节拍，缩短读取周期或更换同端口另一计数来源复核。无需为了排除采样假象先安装一套完整遥测系统。

## 15. 判定表：看到什么就做什么

| 主导现象 | 最有判别力的干预 | 下一步方向 |
|---|---|---|
| post/poll worker 有约 20 ms off-CPU | 独立核、workers=1/2、解除实验 CPU 配额对照 | 运行资源、线程交接和业务竞争 |
| worker 运行，但队列搬运/锁/连接阶段长 | 分段计时、缩短锁内工作、限制单轮队列搬运 | 软件提交控制路径 |
| 正 poll 早到，Jetty/QP 额度很晚释放 | 每个 JFC/CQ 就地释放 depth、增加回收预算 | completion 处理/回收策略 |
| 额度已可用，pending 非空，下次 post 很晚 | post worker 独立核、单 worker、poll→post | worker 服务间隔或队列调度 |
| 原始 poll 短而连续返回零约 20 ms | 同粒度 provider 直连、READ/WRITE、内存类型对照 | provider/设备/内存访问进度 |
| TE 大批无间隔，业务/小批有 | 等字节 block/batch、双批次重叠、上层四段计时 | 批次固定成本或业务资源竞争 |
| 扩窗口能增吞吐但间隔不变 | 保留该收益，同时继续以上对应实验 | 容量补偿与等待优化分别推进 |

## 16. 结果如何说明性能空间

每个实验记录：有效 GB/s、批次传输时间、P/I 的中位数与 P90/最大值、每 burst posted bytes、主要阶段时长、worker CPU、post/poll 调用数和错误/重试数。

每种候选至少 3 次交错重复，20 s 吞吐 run 与 2–3 s trace run 分开。优先报告中位变化与所有单次值，不为首轮性能探索构造繁重的置信区间准入流程。

例如在“每轮 16 MiB、原补充周期 20 ms”的简化条件下，缩到 2 ms，模型吞吐由约 0.839 GB/s 提升到约 8.39 GB/s，仍受链路和其他开销限制。这说明缩短空闲可能远比继续扩大 WR 深度更有价值。该数字是实验动机，不是本方案已经实现的结果。

干预后若流量变连续，原来孤立 burst 已消失，就报告连续区间吞吐、长空档次数和占比；不强求继续产生一个“20 ms 间隔分布”。

最终将结论写成：**哪一阶段占了多少时间→哪项改动使它缩短多少→平均带宽提高多少→原业务能否复现。** 暂时只定位到 provider 层，也要交付已排除的路径、最小复现程序和下一项具体干预，不停留在“还需进一步分析”。

## 17. AI agent 可直接执行的任务指令

> 请以性能实验方式研究 Mooncake classic RDMA/UB 两次 burst 中间约 20 ms 的空闲。先保存原始业务现象，再用单 endpoint 足量 pending 的独立 TE benchmark 对照，应用线程固定 1；UB/RDMA 显式选路。P2P 目标名称取目标端实际输出，不套用 V1.2 的自定义 segment 名，也不将 MC_FORCE_HCA 当成排除 UB 的开关。
>
> 按第 7 节加七组最小阶段打点：提交/入队、endpoint 阻塞原因、原始 post、原始 poll、完成处理与两级额度、下一次 post、worker phase/TID。固定连接的短时实验中使用指针+队列映射即可，不建设 token/generation/通用 trace 平台。post 前累计 bytes；完成发布后不再读取 Slice。缓存事件，结束后导出，不在热路径逐 WR 打日志。
>
> 第一轮直接执行 E1、E2、E3：业务/TE 对照，独立核与 workers=1/2/4，CPU 配额核对，等总窗口对照。根据时间线选择 E4 的就地释放额度、poll batch/budget、循环顺序补丁。若仍在原始 provider poll 阶段等待，执行 E5 的原始 provider、方向和内存类型对照；若空档发生在下一批或入队前，执行 E6。每一步明确它支持或排除了什么，并继续最有信息的下一项实验。
>
> 你负责诊断打点、小型 benchmark/脚本、上述针对性干预与结果分析。你不是独自在代码库工作，不回滚他人修改；如果另一 agent 同时优化 UB，使用独立实验 checkout 或分别应用补丁，避免把两人的变量混在一个对照中。不要要求先完成故障注入、完整单测和产品回退系统才开始性能测试。
>
> 每配置 20 s、至少 3 次交错重复，短 trace 单独捕获 2–3 s。交付实际补丁、双端脚本、时间线图、gap_results.csv、诊断结论和最有效的优化方法。报告真实成功 bytes 与时间；把模型预测和测量分开。无法在当前节点做某一项，就保留明确的命令和待测点，继续能运行的实验；不把未测的20 ms根因写成已确认。项目临时文件放 tmp，同步索引与变更记录。

## 18. 最小结果文件

```text
gap_results.csv:
run,transport,workload,op,workers,cpu_binding,cpu_quota,
max_wr,queue_count,slice_bytes,batch_bytes,GBps,
period_p50_ms,idle_p50_ms,idle_p90_ms,idle_max_ms,
post_to_completion_ms,completion_to_credit_ms,credit_to_post_ms,
offcpu_ms,post_calls,poll_calls,errors,retries,variant,result_path
```

每个候选至少附一张“原始→干预后”的同尺度时间线。报告按性能贡献排序，最后给出可重复启动的最佳方案及下一步最值得做的实验。
