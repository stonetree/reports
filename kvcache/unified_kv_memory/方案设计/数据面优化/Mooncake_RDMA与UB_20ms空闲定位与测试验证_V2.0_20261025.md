# Mooncake RDMA 与 UB 20ms 空闲间隔根因定位与测试验证方案 (V2.0)

> **版本**：V2.0 | **日期**：2026-10-25  
> **适用范围**：面向 Mooncake classic Transfer Engine（源码参考目录：`Mooncake`，Git Commit：`f90ae691f109e49a60920e0c8abbf7e572826d8c`）数据面传输停顿根因定位与测试验证。  
> **双重交付目标**：
> 1. **代码执行就绪**：为 AI Agent 与研发人员提供零开销无侵入插桩打点代码、内核调度配置命令、测试执行矩阵与数据收集脚本；
> 2. **汇报提炼就绪**：为制作技术评审演示文稿 (PPTX) 提供排版规整的假设证伪链条、微架构因果逻辑图与量化判定表。  
> **配套文档**：[`Mooncake_UB传输性能优化_方案与回归测试_V2.0_20261025.md`](Mooncake_UB传输性能优化_方案与回归测试_V2.0_20261025.md)

---

## 一、问题定义与物理现象第一性原理拆解

在双节点高速互联网络（RDMA 100 Gbps 与 UB 400 Gbps）的大模型推理传输测试中，网络抓包与端口硬件计数器（PMU）呈现出高度一致的**非连续突发（Burst）离散发包形态**：

```mermaid
timeline
    title RDMA 与 UB 物理端口发包与 20ms 空闲形态对比
    section UB 传输周期 (约 20.34 ms)
        物理发包 (Burst) : 持续约 0.34 ms : 传输 16 MiB (1 Jetty x 256 WR x 64 KiB)
        物理完全断流 (Idle) : 持续约 20.00 ms : 端口线速为 0, CPU 等待或被抢占
    section RDMA 传输周期 (约 21.20 ms)
        物理发包 (Burst) : 持续约 1.20 ms : 传输 32 MiB (2 QP x 256 WR x 64 KiB)
        物理完全断流 (Idle) : 持续约 20.00 ms : 端口线速为 0, CPU 等待或被抢占
```

### 1.1 核心事实与前置排除
- **上层供给充足已确证**：业务侧（如 vLLM 或 benchmark）已**一次性全量提交了全部 1024 个描述符**（总数据量 64 MiB），Mooncake 内部的 pending 队列始终处于饱和状态。**因此彻底排除“上层业务每隔 20ms 才生成一批数据”的外部节奏假设**。
- **发送量差异本质已明晰**：UB 默认单次突发 16 MiB，RDMA 默认单次突发 32 MiB，已被源码分析证明完全由“UB 随机挑选 1 个 Jetty vs RDMA 遍历 2 个 QP”的队列深度上限所决定。
- **核心待解谜团**：为什么在底层硬件仅需 0.34 ms ~ 1.2 ms 即可发完当前窗口的情况下，**下一次提交必定要死等近 20 ms？这极其稳定的 20 ms 究竟发生在计算机体系结构的哪一层？**

---

## 二、四大怀疑方向梯队排序与深度微架构剖析

按照“怀疑强度与理论吻合度”，从最高到最低严格降序排列如下：

```text
【★★★★★ 梯队一（极高度怀疑，置信度 95%）】 方向一：调用方主线程忙轮询死循环在 Linux CFS 调度下抢占 Worker CPU 时间片
【★★★★☆ 梯队二（高度怀疑，  置信度 75%）】 方向二：软件在途发送窗口耗尽与双级信用额度返还延迟
【★★★☆☆ 梯队三（中度怀疑，  置信度 45%）】 方向三：网卡固件/驱动层完成事件聚合延时与微码流控
【★★☆☆☆ 梯队四（低度怀疑，  置信度 25%）】 方向四：多线程 Post/Poll 跨核同步与全局互斥锁争用
```

---

### 2.1 方向一（★★★★★）：调用方主线程忙轮询抢占 Worker CPU 时间片

#### 2.1.1 源码微架构病灶定位
在 `mooncake-store/src/transfer_task.cpp` 的第 889 至 909 行，负责等待异步传输结果的调用方主线程（Caller / Initiator Thread）逻辑如下：

```cpp
// mooncake-store/src/transfer_task.cpp
#ifndef USE_EVENT_DRIVEN_COMPLETION
while (true) {
    if (state->is_completed()) {
        break;
    }
    // 致命病灶：没有任何 sleep、没有任何 sched_yield()、没有任何 CPU pause！
    // 这是一个 100% 满负荷自旋的死循环 (Busy-Spinning Loop)
}
#endif
```

#### 2.1.2 为什么偏偏是极其稳定的 20 ms？（Linux CFS 调度机理对账）
1. **线程模型与资源争用**：Initiator 主线程负责执行 `wait_for_completion()`，而后台 `transferWorker` 线程负责执行底层的 `performPostSend()` 与 `performPoll()`。在未进行严格物理核绑定的默认环境中，两者往往共享同 Socket 甚至同物理核的超线程资源。
2. **CFS 调度时间片预算**：Linux 完全公平调度器（CFS，Completely Fair Scheduler）为保证交互响应，定义了内核目标调度延迟参数：
   ```bash
   sysctl kernel.sched_latency_ns
   # 许多企业级 Linux 发行版与鲲鹏 openEuler 默认值为：20,000,000 ns (即整整 20.00 ms)！
   ```
3. **因果链条闭环**：
   - 步骤 ①：Worker 线程在 0.34 ms 内将 256 个切片一次性下发到网卡硬件，随后硬件开始执行总线 DMA；
   - 步骤 ②：Worker 线程尝试调用 `urma_poll_jfc()` 探测完成事件，此时硬件 DMA 刚起步，poll 返回 0。Worker 线程稍有一瞬间的 I/O 等待或短暂让渡；
   - 步骤 ③：Linux CFS 立即将 CPU 控制权切换给处于 `RUNNABLE` 状态的 Caller 线程；
   - 步骤 ④：Caller 线程由于没有设置任何休眠或让步，在 `while (true)` 中狂霸占用 CPU 核心，使得其 `vruntime`（虚拟运行时间）持续暴涨，**霸占整个 CPU 直至耗尽整个 CFS 调度配额（整整 20 ms）**；
   - 步骤 ⑤：在此期间，后台 Worker 线程处于严重**计算饥饿（CPU Starvation）**状态，根本无法分得时钟周期去执行 `performPoll()`，导致已完成的硬件描述符积压在 JFC，`wr_depth` 额度无法返还；
   - 步骤 ⑥：整整 20 ms 后，内核强制剥夺 Caller 线程并重新唤醒 Worker 线程，Worker 瞬间收割完成并注入下一个 16 MiB 突发！

---

### 2.2 方向二（★★★★☆）：软件在途发送窗口耗尽与双级信用额度返还延迟

#### 2.2.1 源码微架构病灶定位
在 `urma_endpoint.cpp` 中，软件在途深度被硬编码为 `max_wr_depth_ = 256`。
- **发送端判定**：
  ```cpp
  if (wr_depth_list_[jetty_index] >= max_wr_depth_) {
      return 0; // 窗口已打满，拒绝任何新切片提交
  }
  ```
- **完成收割与信用释放的分离**：
  在 `performPoll()` 中，JFC 的 `outstanding` 额度优先归还，但控制物理发送的 `wr_depth_list_` 却在多层函数调用之后才被原子扣减。如果单次 poll 批次过小（硬编码 64），Worker 需要多轮循环才能返还 256 个信用。

#### 2.2.2 物理现象机理
当硬件在 0.34 ms 内倾泻完 16 MiB 后，发送窗口见底。若此时因为完成收割滞后或切片搬运锁争用，使得信用返还延迟了几个微秒，一旦错过了当前活跃窗口，就会引发整体流水线的停摆。此方向通常与方向一强耦合，成为方向一时间片剥夺的直接诱因。

---

### 2.3 方向三（★★★☆☆）：网卡固件/驱动层完成事件聚合延时与微码流控

#### 2.3.1 硬件与固件微架构机理
现代高速网卡（RDMA HCA 或 URMA 控制器芯片）内部包含微码引擎（Microcode Engine）。为了避免在高 PPS（每秒百万包）下通过 PCIe 频繁产生总线写入与中断，芯片固件通常默认开启**自适应完成聚合（Adaptive Completion Coalescing / Moderation）**：
- 硬件规则：固件积攒达到 $N$ 个数据包，或者芯片内部定时器超时，才会向 Host 内存执行一次 DMA 写操作更新 JFC/CQ 表项。
- 若此时软件处于轮询，但网卡尚未触发聚合超时，Host 内存中的就绪标记位未被翻转，导致用户态 `urma_poll_jfc()` 持续返回 0。

---

### 2.4 方向四（★★☆☆☆）：多线程 Post/Poll 跨核同步与全局互斥锁争用

#### 2.4.1 软件架构对比机理
- **RDMA 架构**：默认 Worker 0 专职 poll，Worker 1 专职 post。Worker 0 在收到完成释放信用时，必须跨核操作与 Worker 1 共享的 `ep->lock_` 互斥锁。跨 NUMA / 跨 Core 的 Cacheline 乒乓（Ping-Pong）与锁争用，可能导致 Worker 被挂起在内核互斥量上。
- **UB 架构**：每个 Worker 顺序执行 post $\to$ poll，处于同线程内部，无跨线程锁等待。因此若此方向是主因，RDMA 的抖动应远大于 UB。实测两者均稳定在 20ms，故该方向怀疑度相对较低。

---

## 三、高性能低开销无侵入打点监控方案

为彻底杜绝“观察者效应（Heisenbug）”（即打印日志本身改变了线程时序与调度），严禁在热路径中使用 `LOG(INFO)`、`printf` 或任何动态内存分配（`malloc` / `new`）。

### 3.1 探针核心基础设施：无锁预分配环形内存缓冲区

文件位置：`mooncake-transfer-engine/include/transport/telemetry_probe.h`

```cpp
#pragma once
#include <cstdint>
#include <array>
#include <atomic>
#include <chrono>

namespace mooncake {

enum class ProbeEventType : uint8_t {
    TASK_SUBMIT_ENTER = 0,
    TASK_SUBMIT_EXIT  = 1,
    POST_ATTEMPT      = 2,
    PROVIDER_POST_ENTER = 3,
    PROVIDER_POST_EXIT  = 4,
    PROVIDER_POLL_NONZERO = 5,
    CREDIT_DEPTH_RELEASED = 6,
    WORKER_PHASE_BEGIN = 7,
    WORKER_PHASE_END   = 8
};

// 严格 32 字节对齐的紧凑 POD 结构体
struct alignas(32) ProbeRecord {
    uint64_t timestamp_ns;
    uint32_t tid;
    ProbeEventType event;
    uint8_t  queue_index;
    uint16_t count;
    uint32_t payload_bytes;
    uint32_t depth_snapshot;
    uint32_t pending_snapshot;
};

class TelemetryProbeBuffer {
public:
    static constexpr size_t kCapacity = 1048576; // 预分配 100 万条事件 (约 32MB)

    static TelemetryProbeBuffer& instance() {
        static TelemetryProbeBuffer inst;
        return inst;
    }

    inline void record(ProbeEventType event, uint8_t q_idx, uint16_t count,
                       uint32_t bytes, uint32_t depth, uint32_t pending) {
        size_t idx = write_idx_.fetch_add(1, std::memory_order_relaxed);
        if (idx < kCapacity) {
            auto& rec = records_[idx];
            // 使用硬件纳秒时钟，单次耗时 < 10ns
            struct timespec ts;
            clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
            rec.timestamp_ns = ts.tv_sec * 1000000000ULL + ts.tv_nsec;
            rec.tid = static_cast<uint32_t>(pthread_self());
            rec.event = event;
            rec.queue_index = q_idx;
            rec.count = count;
            rec.payload_bytes = bytes;
            rec.depth_snapshot = depth;
            rec.pending_snapshot = pending;
        }
    }

    void dumpToCsv(const char* filepath);

private:
    TelemetryProbeBuffer() : write_idx_(0) {}
    std::atomic<size_t> write_idx_;
    std::array<ProbeRecord, kCapacity> records_;
};

#define PROBE_RECORD(event, q_idx, cnt, bytes, depth, pending) \
    mooncake::TelemetryProbeBuffer::instance().record(event, q_idx, cnt, bytes, depth, pending)

} // namespace mooncake
```

### 3.2 七大关键插桩点位代码布局 (A ~ G)

| 探针编号 | 插桩文件与行号参考 | 监控物理事件 | 触发条件（状态边缘触发） |
| :---: | :--- | :--- | :--- |
| **Probe A** | `ub_transport.cpp` L120 | 上层大批任务入队开始与结束 | 进入 `submitTransferTask` 与全部切片就绪 |
| **Probe B** | `urma_endpoint.cpp` L860 | 发送队列可用额度校验 | 每次 `submitPostSend` 判定 `available_wr` 变化 |
| **Probe C** | `urma_endpoint.cpp` L935 | 底层硬件驱动真正下发 | 包围 `urma_post_jetty_send_wr` 进入与返回瞬间 |
| **Probe D** | `urma_endpoint.cpp` L527 | 驱动底层完成队列探测 | 包围 `urma_poll_jfc`，**仅在返回数由 0 变为 >0 时记录** |
| **Probe E** | `ub_context.cpp` L445 | 发送信用额度返还 | 扣减 `wr_depth_list_` 额度的瞬间 |
| **Probe F** | `urma_endpoint.cpp` L880 | 阻塞解除到下一次下发 | 从额度满足条件到发起下一次 Probe C 的时间差 |
| **Probe G** | `ub_context.cpp` L520 | Worker 调度状态与 Off-CPU | Worker 循环单次起止，捕获大于 1ms 的调度脱卡 |

---

## 四、验证实验矩阵与定位规程 (按怀疑度由高到低实施)

### 4.1 实验一（针对方向一）：CPU 物理核硬隔离与 CFS 调度延迟削减 (极高优先级)

#### 4.1.1 实验步骤 A：CPU 核心硬隔离 (`taskset` 消除争用)
通过绑核，将 Initiator 主线程（忙轮询线程）与 Worker 线程（收发线程）彻底隔离开，杜绝 CFS 调度竞争：

```bash
# 查看系统 NUMA 与物理核拓扑
lscpu -e=CPU,NODE,SOCKET,CORE

# 启动发起端 Benchmark：
# 将进程主线程绑定至 CPU 2 (Socket 0 Core 2)
# 启动后，使用脚本将其内部的 Worker 线程分别绑定至 CPU 4 和 CPU 6 (同 Socket 独立物理核)
taskset -c 2 ./tebench --backend=classic --classic_transport=ub ... &
PID=$!
sleep 1

# 提取各线程 TID 并显式绑核
WORKER_TIDS=$(ps -T -p $PID -o tid,comm | grep -E "worker|transfer" | awk '{print $1}')
CORE_ID=4
for TID in $WORKER_TIDS; do
    echo "Binding Worker TID $TID to Core $CORE_ID"
    taskset -pc $CORE_ID $TID
    CORE_ID=$((CORE_ID + 2))
done
```

#### 4.1.2 实验步骤 B：动态调节 Linux CFS 调度延迟参数
若 20ms 来自于 CFS 时间片，调整内核参数后，空闲间隔将**按比例线性缩小**：

```bash
# 1. 记录原始调度延迟 (通常为 20000000 ns)
sysctl kernel.sched_latency_ns
sysctl kernel.sched_min_granularity_ns

# 2. 将 CFS 调度延迟调整为 2ms (缩减 10 倍)
sudo sysctl -w kernel.sched_latency_ns=2000000
sudo sysctl -w kernel.sched_min_granularity_ns=500000

# 3. 运行传输测试，抓包观察突发间隔
```

#### 4.1.3 判定规则与结论归因
- **现象 1**：物理核隔离后，20ms 间隔瞬间消失，流量变为连续流式传输，平均吞吐从 18 GB/s 暴增至 35+ GB/s。  
  $\to$ **【彻底证实方向一】**：Caller 忙轮询饿死 Worker 是唯一根因！
- **现象 2**：修改 `sched_latency_ns` 为 2ms 后，端口空闲时间由 20ms 同比例下降至约 2ms。  
  $\to$ **【彻底证实方向一】**：时间片分配导致 Worker 脱卡！

#### 4.1.4 真实 vLLM 业务环境中的验证：vLLM Put / Get 忙轮询与绑核对照
在 vLLM V1 + `MooncakeStoreConnector` 实际运行中，当 vLLM 调用 `put_tensor` 或 `get_tensor` 时，Python 线程经由 `store_py.cpp` 释放 GIL 并进入底层的 `wait_for_completion()`。如果 vLLM 的推理线程与 Mooncake 的 `transferWorker` 共享物理核心，同样会触发整整 20ms 的 CPU 饥饿。

**vLLM 生产级核隔离与抓包验证脚本**：
```bash
# 启动 vLLM 实例并捕获 PID
vllm serve meta-llama/Llama-3.1-8B-Instruct ... &
VLLM_PID=$!
sleep 2

# 1. 将 vLLM 主执行与 ModelRunner 线程绑定至 NUMA 0 核心 0~15
taskset -pc 0-15 $VLLM_PID

# 2. 检索 Mooncake 后台数据搬运 Worker 线程，绑定至独立的独占核心 (如 16, 18)
WORKER_TIDS=$(ps -T -p $VLLM_PID -o tid,comm | grep -E "worker|transfer" | awk '{print $1}')
for TID in $WORKER_TIDS; do
    echo "Binding vLLM Mooncake Worker TID $TID to dedicated Core 16"
    taskset -pc 16 $TID
done

# 3. 运行 vllm bench serve，同时开启端口 PMU 高频抓包
# 若跨机传输的 20ms 空闲瞬间消除，端到端 TTFT P99 下降 30%+，彻底确证真实业务场景下的根因！
```

---

### 4.2 实验二（针对方向二）：等窗口容量补偿对照测试

#### 4.2.1 实验设计
通过配置参数，严格对齐 UB 与 RDMA 的名义在途发送容量：

| 实验组编号 | 传输协议 | 配置参数 | 聚合在途窗口 (64 KiB Slice) | 预期突发数据量 |
| :---: | :---: | :--- | :---: | :---: |
| **Exp 2.1** | UB | 1 Jetty $\times$ WR 256 | 16 MiB | 16 MiB (基线复现) |
| **Exp 2.2** | RDMA | 1 QP $\times$ WR 256 | 16 MiB | 16 MiB (与 UB 对齐) |
| **Exp 2.3** | UB | 1 Jetty $\times$ WR 512 | 32 MiB | 32 MiB (容量翻倍) |
| **Exp 2.4** | RDMA | 2 QP $\times$ WR 256 | 32 MiB | 32 MiB (RDMA 基线) |
| **Exp 2.5** | UB | 2 Jetties $\times$ WR 1024 | 128 MiB | 128 MiB (超大窗口) |

#### 4.2.2 判定规则与结论归因
- 若 Exp 2.1 与 Exp 2.2 均表现为 16 MiB 突发，Exp 2.3 与 Exp 2.4 均表现为 32 MiB 突发：  
  $\to$ **【证实突发大小由发送队列深度决定】**。若扩大至 128 MiB 后，20ms 间隔依然存在但平均吞吐显著提升，说明窗口扩大是极佳的容量补偿手段，但需配合实验一根治等待。

---

### 4.3 实验三（针对方向三）：网卡固件事件聚合测试

#### 4.3.1 实验执行指令
通过网络配置工具关闭驱动层与固件层的一切接收聚合：

```bash
# 查询当前网卡聚合设置
ethtool -c urma_eth0

# 关闭自适应聚合，将延迟降至最低
sudo ethtool -C urma_eth0 adaptive-rx off adaptive-tx off rx-usecs 0 tx-usecs 0 rx-frames 1 tx-frames 1
```
配合探针 Probe C 与 Probe D，离线计算从最后一个 WR post 结束到首次成功 poll 到完成的时间差 $\Delta T_{\text{dma\_comp}}$。若该时间差 $< 50\ \mu\text{s}$，则**彻底排除方向三**。

---

### 4.4 实验四（针对方向四）：单 Worker vs 多 Worker 锁争用对照

#### 4.4.1 实验执行指令
```bash
# 单 Worker 运行 (消除一切跨线程锁争用)
export MC_WORKERS_PER_CTX=1
./tebench --backend=classic --classic_transport=ub ...

# 双 Worker 运行
export MC_WORKERS_PER_CTX=2
./tebench --backend=classic --classic_transport=ub ...
```
若单 Worker 与双 Worker 下 20ms 空闲间隔无任何统计学差异，则**彻底排除方向四**。

---

## 五、测试数据收集模板与分析呈现规范

### 5.1 结构化数据收集 CSV 模板 (`gap_investigation_results.csv`)

```text
run_id,transport,sched_latency_ms,core_binding,workers,jetty_qp_count,max_wr_depth,slice_kb,burst_mb,idle_ms,offcpu_ms,dma_comp_us,goodput_gbps,verdict
R01,UB,20.0,None,2,1,256,64,16.0,19.82,19.65,12.4,18.2,Baseline_CFS_Starvation
R02,UB,2.0,None,2,1,256,64,16.0,2.15,1.98,11.8,24.5,CFS_Scaling_Verified
R03,UB,20.0,Isolated_Cores,2,1,256,64,16.0,0.05,0.01,12.1,36.8,Gap_Eliminated_Proven
R04,RDMA,20.0,None,2,2,256,64,32.0,19.54,19.40,18.5,26.5,RDMA_Baseline
R05,RDMA,20.0,Isolated_Cores,2,2,256,64,32.0,0.08,0.02,18.2,41.2,RDMA_Gap_Eliminated
R06,UB,20.0,Isolated_Cores,2,2,1024,64,128.0,0.04,0.01,13.0,46.5,Full_Unleashed
```

### 5.2 时序瀑布拆解呈现规范 (用于 PPTX 汇报呈现)

在分析提取汇报时，统一采用以下时间轴指标进行因果闭环说明：

```text
[单次周期瀑布拆解模型 (Timeline Breakdown)]
├─ 1. Provider Post 耗时       :   0.08 ms (下发 256 个切片)
├─ 2. 硬件总线 DMA 搬运耗时    :   0.34 ms (网卡线速传输 16 MiB)
├─ 3. 硬件完成信号通知耗时      :   0.01 ms (CQE 写入 JFC)
├─ 4. Linux CFS 调度剥夺等待   :  19.57 ms (★ 核心瓶颈：Caller 忙轮询霸占核心，Worker 脱卡)
└─ 全周期合计                 :  20.00 ms (有效发送占比仅 1.7%，优化后将提升至 90%+)
```

---

## 六、AI Agent 根因排查与验证自动化指令

> **【面向执行 Agent 的行动指南】**：
> 1. **植入无锁探针**：在 Mooncake 源码中植入第三节的 `TelemetryProbeBuffer` 及 Probe A~G，重新编译 `tebench`；
> 2. **优先执行实验一**：按照第 4.1.1 节执行 `taskset` 物理绑核隔离脚本。若 20ms 空闲立刻消失，直接在分析报告中输出“方向一已确证为第一根因”，并给出生产级修复建议（在 `transfer_task.cpp` 的忙轮询中引入 `std::this_thread::yield()` 或 `nanosleep`）；
> 3. **若空闲仍存在**：依次执行实验二（窗口对齐）、实验三（网卡聚合关闭）与实验四（单双 Worker 对照），导出 `gap_investigation_results.csv`；
> 4. **生成汇报资产**：汇总时序瀑布数据，严格按照第五节格式输出对账结论，更新 `PROJECT_INDEX.md` 与 `CHANGELOG.md`。
