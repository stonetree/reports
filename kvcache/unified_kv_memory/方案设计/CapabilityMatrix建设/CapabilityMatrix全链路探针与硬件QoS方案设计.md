# CapabilityMatrix 全链路探针（含 IO Die/RC <500µs）与硬件 QoS 方案设计 (V2.0)

> **版本**：V2.0（工程落地与性能验收执行版）｜**日期**：2026-11-03  
> **任务代号与名称**：`M2-T04 CapabilityMatrix 全链路探针 (含 IO Die/RC <500µs) 与硬件 QoS`（对应阶段任务 `W3-T05` / `CP-09`）  
> **主责团队**：遥测探针与物理能力矩阵研发团队（0.5 FTE）｜**核心协同接口**：调度引擎与选路研发团队（负责调度下沉与 `MountedSegment` 选路）、描述符编译与边界协同研发团队（负责边界描述符内嵌）  
> **适用架构底座**：面向国产 AI 硬件的统一异构 KVCache 存储与调度底座，覆盖双路鲲鹏 920（计算 Die `SCCL` + IO Die `NimBus`）+ ConnectX-6/7（RoCE v2）或 灵衢 UB（`UBMEM` / `URMA`）+ Mooncake 增强主干 + `HoloTestbed-4X` 验证平台  
> **双重交付目标**：
> 1. **研发与自动化协作编码就绪**：提供零歧义的寄存器事件名、64B 结构体升级定义、C++20 零系统调用采集补丁、单边内联写上报逻辑与双时间尺度硬件 QoS 控制代码；
> 2. **测试场景构造与门禁验收就绪**：提供可精确复现 **IO Die / PCIe RC 争抢、3ms 采集阻塞、20 实例羊群踩踏、100Gbps 后台混压** 的测试场景构造方法与量化判定门限。

---

## 零、专有名词与技术术语释义

按照项目规范，对核心专有名词首次出现提供直白功能释义：

1. **硬件能力矩阵（CapabilityMatrix，即在运行时自动探测各通信链路的带宽、时延等物理参数表，供调度算法使用）**；
2. **KVCache**：大模型注意力键值缓存（大模型自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免后续 Token 生成时重复计算注意力）；
3. **Saved-Prefill**：首字生成预计算节省（利用已缓存的 KV Cache 避免重复计算 Prompt，从而显著降低首 Token 延迟 TTFT）；
4. **TTFT**：Time To First Token（首字生成延迟 / 首 Token 响应时间）；
5. **TPOT**：Time Per Output Token（每个输出 Token 的生成耗时 / 单字生成延迟）；
6. **UBMEM**：统一总线内存直通共享协议（支持跨节点与异构设备间直接共享内存地址空间的底层通信协议）；
7. **URMA**：通用远程直接内存访问（用户态的高性能 RDMA 驱动接口与通信协议）；
8. **QoS**：Quality of Service（服务质量，通过硬件优先级队列映射与带宽保留，确保高优先级实时前台请求不受后台大流量搬运的干扰）；
9. **Incast**：多对一突发网络拥塞（多个发送节点在极短时间内同时向单一接收节点回传数据包，导致交换机出端口缓冲区瞬间耗尽而引发的排队丢包风暴）；
10. **Host CPU 零数据拷贝（CPU 仅负责下发控制指令，不参与正文数据搬运，Host Payload Touch Bytes 严格为 0）**；
11. **MountedSegment**：挂载存储段（由存储引擎在节点本地或远端内存池中预先划分并注册的大颗粒连续物理内存段，作为 KVCache 放置与调度的基本载体）。

---

## 1. 进展复盘与核心攻关问题定义（第一性原理审视）

### 1.1 前期进展复盘与三大遗留瓶颈

经过前期原型攻关，`CapabilityMatrix` 已经完成了 `64B`（8 个 Metric）的底层采集与原型打通，其中 6 个指标的采集耗时已压至几微秒甚至纳秒级。但对照阶段验收红线与端到端业务收益要求，当前仍存在三个必须彻底攻克的工程与体系结构问题：

1. **【采集时延卡点】2 个慢指标采集耗时高达 `3ms`（`3000µs`），阻塞 `< 500µs` 端到端上报构建目标**：
   当前仍有 2 个硬件指标（网卡底层拥塞/诊断计数器与同步 RTT/sysfs 状态读取）耗时在 `3ms` 量级，比 `500µs` 的端到端采集+上报+构建总目标超标 **6 倍**，比 `< 2µs` 的本地无阻塞采集目标超标 **1500 倍**。
2. **【模型完整性盲区】缺乏对鲲鹏 IO Die 资源争抢与 PCIe Root Complex（RC）性能瓶颈的感知**：
   初版木桶模型仅考虑了“计算 Die 上的 `DDRC` 内存通道带宽”与“网卡 `Port` 物理带宽”两端。然而在双路鲲鹏 920 的多 Die 封装（MCP）微架构中，**计算卡（GPU/NPU）与 400G 网卡共同挂载在独立的 IO Die（NimBus）上**。当计算卡发起高强度 H2D/D2H 或 P2P 传输、或者小页 DMA 击穿 IO Die 上的共享 SMMUv3 页表时，哪怕 DDRC 内存通道与网线均处于空闲状态，**IO Die 内部的 PCIe RC 信用队列、SMMUv3 IOTLB 与跨 Die HHA（缓存一致性家代理）也会发生严重拥塞**，导致初版模型出现“预测可用带宽 `40 GB/s`、实测传输仅 `4.76 GB/s`”的漏判。
3. **【端到端闭环缺口】64B 指标尚未与中心 `MountedSegment` 描述符、微秒选路及双时间尺度硬件 QoS 形成业务收益闭环**：
   64B 指标采集输出后，必须证明其对调度选路（消除 `3 GB/s` 劣质网络下高达数百毫秒的负收益等待）和前后台流量隔离（100Gbps 后台换出混压下保住前台 Decode **TPOT 干扰率 `< 3%`**）的充分必要性。

---

### 1.2 第一性原理剖析：鲲鹏多 Die 微架构下的“三段式全链路瓶颈拓扑”

在双路鲲鹏 920 服务器中，单次 KVCache 从主机存储池（或经网卡从远端）跨越 HOST 域与 GPU/NPU 域边界的物理数据流，必须依次穿过三个物理隔离的硬件子系统：

```
+-------------------------------------------------------------------------------------------------------------+
|                       鲲鹏 920 多 Die (MCP) 体系结构下的 KVCache 传输三段式物理瓶颈拓扑                       |
+-------------------------------------------------------------------------------------------------------------+
|                                                                                                             |
|  【第一段：计算 Die (SCCL 0~7)】         【第二段：IO Die (NimBus 共享枢纽)】       【第三段：外设与网络】    |
|  +----------------------------+         +----------------------------------+       +---------------------+  |
|  | • 4x DDRC 内存控制器/SCCL  |  HCCS/  | • HHA (跨Die一致性目录与监听过滤) | PCIe  | • CX6/7 或 UB 网卡   |  |
|  |   (flux_rd / flux_wr)      | ──────► | • SMMUv3 (共享IO页表转换 PMCG)   | ────► |   (Port Rx/Tx,      |  |
|  | • L3 Cache (MPAM CPOR/MBW) |  Hydra  | • PCIe Root Complex (RC x16)     |  5.0  |    ECN/CNP/Credit)  |  |
|  +----------------------------+  环网   |   (TLP 吞吐 & Outbound Stalled)  |       +----------+----------+  |
|                                         +-----------------+----------------+                  │             |
|                                                           │ PCIe P2P / H2D 争抢               │ 400G 光纤   |
|                                                           ▼                                   ▼             |
|                                         +----------------------------------+       +---------------------+  |
|                                         | • 同 IO Die 挂载的 GPU / NPU 卡   |       | 远端存储节点 / 交换网|  |
|                                         +----------------------------------+       +---------------------+  |
+-------------------------------------------------------------------------------------------------------------+
```

从计算机体系结构第一性原理出发，任何一段发生阻塞都会通过硬件信用（Credit）反压机制锁死整条管道：

* **第一段（计算 Die 瓶颈）**：受限于单个 SCCL 下属 4 条 DDRC 通道的物理饱和带宽（实测 STREAM 极限约 `75 ~ 105 GB/s`）；
* **第二段（IO Die 共享枢纽瓶颈 —— 核心新增拓扑）**：
  1. **PCIe Root Complex (RC) 吞吐与信用瓶颈**：单根 PCIe 5.0 x16 RC 的有效载荷上限约为 `52 ~ 56 GB/s`（PCIe 4.0 x16 仅 `26 ~ 28 GB/s`）。当挂载在同一 IO Die RC 树下的 GPU/NPU 正在执行张量并行通信或换出时，RC 的 Posted/Non-Posted 事务信用耗尽（硬件计数器 `outbound_stalled` / `rc_credit_stall` 激增）；
  2. **IO Die SMMUv3 地址转换争抢**：网卡与加速卡共享 IO Die 上的 SMMUv3 转换单元。当未对齐 1GB 巨页或发生多流并发散碎访问时，`smmuv3_pmcg/tlb_miss` 触发高昂的跨 Die 页表遍历（Page Table Walk），单次 Miss 停顿高达 `15 ~ 30` 个事务周期；
  3. **跨 Die HHA 目录监听冲突**：当网卡（位于 IO Die）访问远端 SCCL（跨 NUMA）内存或未开启 `No-Snoop` 时，IO Die 必须向目标计算 Die 的 **HHA（Home Agent）** 发送缓存一致性监听请求（`snoop`），引发环网拥塞。
* **第三段（网络端口与交换 Fabric 瓶颈）**：受限于 400Gbps 端口物理速率（有效载荷 `45 GB/s`）以及交换网 ECN/PFC 或 UB Flit Credit 拥塞折损。

---

## 2. 64B `CapabilityMatrix` V2.0 结构升级与三段式数学模型

### 2.1 充分必要性证明：为什么严格保持 `64 Bytes` 并采用重构后的 8 大字段？

在前期方案的基础上，在 **严格保持 `64 Bytes = 1 条鲲鹏 L3 Cacheline`** 的物理前提下，通过紧凑字段重排，将 **PCIe RC 带宽利用率** 与 **IO Die 争抢惩罚系数** 纳入 `ChannelCapabilityEntryV2`。

这样设计的充分必要性在于：
1. **单指令无锁原子读（Zero-Tearing Read）**：64B 严格对齐单条 CPU 缓存行，调度器热路径读取仅需 `< 50ns`（一次 L1/L3 Cache 命中），且单边 `RDMA_WRITE_INLINE` 仅占 1 个 WQE 内联载荷；
2. **覆盖“容量-带宽-排队-片上争抢-QoS配额”全维度**：直接支撑调度引擎的 `< 2µs` 闭式选路（判断全量拉取、按层 $K^*$ 切分、或主动回退至本地重算）与边界描述符编译模块的 `MountedSegment` 描述符签发。

```cpp
#include <cstdint>

// 严格 64 字节对齐，独占 1 条鲲鹏 920 L3 Cacheline (64B)
struct alignas(64) ChannelCapabilityEntryV2 {
    // [字段 1: 4B] 全链路木桶合成后的瞬时有效可用带宽 (GB/s)，供调度器直接除算传输耗时
    float avail_bw_gbps;            // Offset 0..3

    // [字段 2: 4B] 基础硬件 RTT + 结合在途字节推算的排队等待时延 (us)
    float est_queue_latency_us;     // Offset 4..7

    // [字段 3: 4B] 归一化网络与链路层拥塞压力指数 [0.0=空闲, 1.0=严重拥塞/触发PFC或Credit耗尽]
    float net_congestion_index;     // Offset 8..11

    // [字段 4: 4B] 片上双瓶颈饱和度（拆分为 2 个 uint16 定点数，精度 0.01%，范围 [0, 10000]）
    //   - local_ddrc_util_bp : 计算 Die 本地 SCCL 4通道 DDRC 带宽饱和度 (基点 Basis Points)
    //   - pcie_rc_util_bp    : 【新增】IO Die 本地 PCIe Root Complex 带宽与信用饱和度 (基点)
    uint16_t local_ddrc_util_bp;    // Offset 12..13
    uint16_t pcie_rc_util_bp;       // Offset 14..15

    // [字段 5: 4B] 【升级】IO Die 综合争抢与地址转换惩罚系数 (>= 1.0)
    //   融合 SMMUv3 IOTLB Miss、网卡 MTT Miss 以及跨 Die HHA 监听冲突惩罚
    float iodie_penalty_factor;     // Offset 16..19

    // [字段 6: 4B] 当前推荐给后台换出任务的硬件带宽配额百分比 [5% ~ 85%] 及通道健康标志位
    uint16_t bg_quota_percent;      // Offset 20..21
    uint16_t health_flags;          // Offset 22..23 (BIT0: LINK_UP, BIT1: RC_STALLED, BIT2: IODIE_HOT, BIT3: STALE)

    // [字段 7: 8B] 当前管道已签发但未完成的在途 DMA 字节数 (In-flight Bytes，含中心预扣与本地队列)
    uint64_t inflight_bytes;        // Offset 24..31

    // [字段 8: 8B] ARMv8 cntvct_el0 硬件物理时钟戳 (用于纳米级保鲜期校验 Staleness Check)
    uint64_t last_update_tsc;       // Offset 32..39

    // [扩展槽位: 24B] 内嵌绑定 Segment 容量水位与双向捎带元数据 (严守 64B 总长)
    uint64_t free_capacity_bytes;   // Offset 40..47: 当前 SCCL/Segment 剩余可用巨页容量 (供存储管理与调度模块容量调度)
    uint64_t active_lease_epoch;    // Offset 48..55: 租约纪元号与僵尸预扣回收版本计数器
    uint32_t optimal_slice_kb;      // Offset 56..59: 200us 快路径推荐的后台微切片粒度 (64 / 128 / 256 KB)
    uint32_t reserved_pad;          // Offset 60..63: 64B 严格对齐填充
};
static_assert(sizeof(ChannelCapabilityEntryV2) == 64, "ChannelCapabilityEntryV2 MUST be exactly 64 bytes!");
```

---

### 2.2 三段式全链路木桶定律与 IO Die 争抢融合数学模型

设采样窗口为 $\Delta t = 500\ \mu\text{s} = 5 \times 10^{-4}\text{ s}$。对于任意传输管道 $p = (\text{SCCL } s,\; \text{IO\_Die\_RC } r,\; \text{Port } k)$，其**瞬时有效可用带宽 $BW_{\text{avail}}^{(p)}(t)$** 由升级后的 **三段式全链路木桶方程** 唯一确定：

$$BW_{\text{avail}}^{(p)}(t) = \frac{1}{\alpha_{\text{iodie}}^{(r,s)}(t)} \cdot \max\left(\epsilon,\;\; \min \Big( \underbrace{BW_{\text{DDRC\_rem}}^{(s)}(t)}_{\text{① 计算Die内存余量}},\;\; \underbrace{BW_{\text{PCIe\_RC\_rem}}^{(r)}(t)}_{\text{② IO Die RC根复合体余量}},\;\; \underbrace{BW_{\text{Port\_rem}}^{(k)}(t)}_{\text{③ 网络端口有效余量}} \Big)\right)$$

各项物理分量的精确计算公式如下：

1. **① 计算 Die 本地 SCCL 内存通道剩余带宽 $BW_{\text{DDRC\_rem}}^{(s)}(t)$**：
   $$BW_{\text{DDRC\_used}}^{(s)} = \frac{\sum_{c=0}^{3} \left(\Delta \text{flux\_rd}_{s,c} + \Delta \text{flux\_wr}_{s,c}\right) \times 32\text{ B}}{\Delta t \times 10^9}, \quad BW_{\text{DDRC\_rem}}^{(s)} = BW_{\text{DDRC\_max}}^{(s)} - BW_{\text{DDRC\_used}}^{(s)}$$
2. **② IO Die PCIe Root Complex 剩余有效带宽 $BW_{\text{PCIe\_RC\_rem}}^{(r)}(t)$**：
   采集鲲鹏 IO Die 上的 PCIe RC PMU（`hisi_pcie<sccl>_<core>`） 的读写 TLP 载荷计数（`rx_mwr_tlp`、`tx_mrd_tlp`）以及发送停顿周期比 $S_{\text{rc\_stall}} = \frac{\Delta \text{outbound\_stalled\_cycles}}{\text{Total\_Cycles}(\Delta t)}$：
   $$BW_{\text{RC\_used}}^{(r)} = \frac{\left(\Delta \text{rx\_mwr\_tlp}_r + \Delta \text{tx\_mrd\_tlp}_r\right) \times B_{\text{tlp\_avg}}}{\Delta t \times 10^9}$$
   $$BW_{\text{PCIe\_RC\_rem}}^{(r)}(t) = BW_{\text{RC\_max}}^{(r)} \cdot \left(1 - S_{\text{rc\_stall}}^{(r)}(t)\right) - BW_{\text{RC\_used}}^{(r)}(t)$$
   *(注：当同一 IO Die 下的 GPU/NPU 占满 PCIe P2P/H2D 通道导致 RC 信用耗尽时，$S_{\text{rc\_stall}} \to 0.8$，$BW_{\text{PCIe\_RC\_rem}}^{(r)}$ 跌破 `10 GB/s`，第一时间触发木桶短板告警。)*
3. **③ 网络端口扣除拥塞折损后的剩余带宽 $BW_{\text{Port\_rem}}^{(k)}(t)$**：
   $$BW_{\text{Port\_rem}}^{(k)}(t) = BW_{\text{Port\_max}}^{(k)} \cdot \left(1 - \Psi_{\text{cong}}^{(k)}(t)\right) - BW_{\text{Port\_used}}^{(k)}(t)$$
4. **④ IO Die 综合争抢与地址转换惩罚因子 $\alpha_{\text{iodie}}^{(r,s)}(t) \ge 1.0$**：
   融合 **SMMUv3/UMMU 页表缺失率 $r_{\text{tlb\_miss}}$**、**网卡 MTT 缓存缺失率 $r_{\text{mtt\_miss}}$** 以及 **跨 Die HHA 一致性监听冲突率 $r_{\text{hha\_snoop}} = \frac{\Delta \text{hha\_rx\_outer} + \Delta \text{snoop\_conflict}}{\Delta \text{hha\_total\_ops}}$**：
   $$\alpha_{\text{iodie}}^{(r,s)}(t) = 1.0 + K_{\text{tlb}} \cdot \left(r_{\text{tlb\_miss}} + r_{\text{mtt\_miss}}\right) + K_{\text{hha}} \cdot r_{\text{hha\_snoop}}$$
   *(标定常数：$K_{\text{tlb}} = 20.0$，$K_{\text{hha}} = 2.0$。当开启 1GB 巨页 + `iommu.passthrough=1` + 本地 SCCL 强亲和绑定时，$r_{\text{tlb\_miss}} = 0, r_{\text{hha\_snoop}} = 0 \implies \alpha_{\text{iodie}} = 1.00$；若发生 4% 页表缺失与 15% 跨 Die 监听冲突，$\alpha_{\text{iodie}} = 1 + 20 \times 0.04 + 2 \times 0.15 = \mathbf{2.10}$，前置将评估带宽折半，防止选路器踩入慢速陷阱。)*

#### 实测工况算力与传输对账对比

设鲲鹏 920 单 SCCL DDRC 最大带宽为 $85.0\text{ GB/s}$，PCIe 5.0 x16 RC 最大载荷带宽为 $56.0\text{ GB/s}$，400GbE 网卡端口最大载荷带宽为 $45.0\text{ GB/s}$。

当工况为：DDRC 空闲（利用率 20%，余量 $68.0\text{ GB/s}$），网络端口空闲（已用 $5.0\text{ GB/s}$，无拥塞，余量 $40.0\text{ GB/s}$）；但同 IO Die 挂载的计算卡执行高强度通信占用 RC 达 $46.0\text{ GB/s}$（利用率 82.1%，RC 余量仅 $10.0\text{ GB/s}$），且伴随 4% 的 SMMUv3 IOTLB 缺失与 15% 的 HHA 跨 Die 监听冲突（$\alpha_{\text{iodie}} = 2.10$）：
* **旧两段式模型（忽略 IO Die RC 与一致性冲突）**：预测可用带宽为 $\min(68.0, 40.0) = \mathbf{40.0\text{ GB/s}}$，造成严重高估；
* **升级三段式模型**：准确预测可用带宽为 $\frac{1}{2.10} \times \min(68.0, 10.0, 40.0) = \mathbf{4.76\text{ GB/s}}$，可直接触发微秒级路径切换或本地重算回退。

---

## 3. 五大核心优化技术点与代码级实现规范

### 3.1 优化点 C1：消灭 `3ms` 慢指标——基于 `mmap` 用户态直读与硬件 DMA Auto-Dump 的 `< 1.5µs` 采集引擎

#### 3.1.1 慢指标根因定位
经代码与内核调用栈剖析，前期原型中耗时 `3ms` 的两个指标根因如下：
1. **通过 VFS 文件系统读取网卡 `hw_counters`（如 `rp_cnp_handled` / `out_of_buffer`）**：
   每次调用 `open("/sys/class/infiniband/mlx5_0/ports/1/hw_counters/rp_cnp_handled") + read()`，不仅触发内核上下文切换，而且 `mlx5` 内核驱动在响应 sysfs 读取时，会同步向网卡固件发起一条 PCIe Mailbox 命令（`MLX5_CMD_OP_QUERY_VNIC_ENV` / `QUERY_Q_COUNTER`）并睡眠等待固件中断返回。单次固件 Mailbox 往返叠加 OS 调度延迟高达 `1.2ms ~ 2.8ms`；
2. **通过同步探测包（Active Ping-Pong Probe）或 `ethtool` `ioctl` 获取 RTT 与链路速率**：
   在采集线程中同步下发探测包并等待完成，一旦遇到微突发排队，耗时直接飙升至 `1.5ms ~ 3.5ms`。

#### 3.1.2 优化原理与零系统调用实现
彻底废除运行期 `sysfs` `read()`、`ioctl()` 和同步发包探测，改用两项零系统调用硬件直通技术：

* **技术 1（针对鲲鹏 DDRC / PCIe RC / SMMUv3 / HHA 计数器）：`perf_event_open` 结合物理 MMIO 页只读 `mmap` 直读**
  在守护进程启动时调用一次 `perf_event_open` 获取 fd，随后将其映射为用户态 `perf_event_mmap_page` 共享页（或直接 `/dev/mem` `mmap` 鲲鹏 Uncore PMU 物理寄存器页）。运行期每 `500µs` 采样时，仅需在用户态执行内存指针解引用指令（耗时约 `40ns`，0 系统调用）。
* **技术 2（针对网卡拥塞计数器与硬件 RTT）：启用 `mlx5dv_devx` 固件计数器页直接映射 + 带内 CQE 硬件时间戳被动测距**
  1. 在初始化阶段通过 `mlx5dv_devx_obj_create` 创建硬件 Bulk Counter 对象并绑定到 Host Pinned 内存页，配置网卡硬件每隔 `100µs` 自动通过 DMA 刷写计数器镜像（或直接 `mmap` 网卡 UAR/诊断寄存器页）；
  2. 硬件 RTT 不再单独发包探测，而是直接在正常数据面收割完成事件时，读取 CQE 硬件时间戳差值（`completion_ts - doorbell_ts`）并通过原子操作写回共享内存。

```cpp
// 文件位置：src/telemetry_fast_collector.hpp
#pragma once
#include <cstdint>
#include <atomic>
#include <sys/mman.h>
#include <linux/perf_event.h>

class ZeroSyscallPmuReader {
public:
    // 初始化阶段调用一次：映射 perf_event_mmap_page 到用户态虚拟地址
    bool init_mmap_counter(int perf_fd) {
        void* base = mmap(nullptr, 4096, PROT_READ, MAP_SHARED, perf_fd, 0);
        if (base == MAP_FAILED) return false;
        mmap_page_ = reinterpret_cast<const struct perf_event_mmap_page*>(base);
        return true;
    }

    // 运行期热路径（每 500us 调用）：纯用户态 Seqlock 内存读取，耗时 < 45ns，0 系统调用
    inline uint64_t read_counter_ns() const noexcept {
        uint32_t seq;
        uint64_t count = 0;
        do {
            seq = __atomic_load_n(&mmap_page_->lock, __ATOMIC_ACQUIRE);
            __asm__ __volatile__("dmb ishld" ::: "memory");
            // 直接从映射页读取硬件 PMU 累加值 (Uncore PMU offset)
            count = __atomic_load_n(&mmap_page_->offset, __ATOMIC_RELAXED);
            __asm__ __volatile__("dmb ishld" ::: "memory");
        } while (__atomic_load_n(&mmap_page_->lock, __ATOMIC_ACQUIRE) != seq || (seq & 1));
        return count;
    }

private:
    const struct perf_event_mmap_page* mmap_page_{nullptr};
};

// ARMv8 架构级虚拟时钟直读（耗时 ~1.5ns，跨 Die 严格同步）
static inline uint64_t read_cntvct_el0() noexcept {
    uint64_t tsc;
    __asm__ __volatile__("isb; mrs %0, cntvct_el0" : "=r"(tsc) :: "memory");
    return tsc;
}
```

---

### 3.2 优化点 C2：补齐 IO Die 与 PCIe RC 探针——鲲鹏 Uncore PMU 精确事件绑定

在守护进程中，按每个鲲鹏 Socket 下的计算 Die（`sccl_id`）与 IO Die（`pcie_rc_id`、`smmu_id`）绑定硬件 PMU 事件（遵循 openEuler 内核规范）：

| 物理子系统 | 鲲鹏 Uncore PMU 设备路径 (`/sys/bus/event_source/devices/`) | 采集的硬件事件名称 | 物理意义与单位换算 |
| :--- | :--- | :--- | :--- |
| **1. 计算 Die DDRC** | `hisi_sccl<s_id>_ddrc<0..3>` | `flux_rd`, `flux_wr` | 每次计数 = **32 Bytes** DDR 内存读写突发 |
| **2. 计算 Die HHA** | `hisi_sccl<s_id>_hha<0..1>` | `rx_ops_num`, `rx_outer`, `snoop_lookup` | 统计跨 Die/跨 Socket 远端访存占比与一致性监听冲突率 |
| **3. IO Die PCIe RC** | `hisi_pcie<sccl>_<rc_core>` | `rx_mwr_tlp`, `tx_mrd_tlp`, `outbound_stalled` | 统计经由该 PCIe Root Complex 的 DMA 读写载荷与信用反压停顿周期 |
| **4. IO Die SMMUv3** | `smmuv3_pmcg_<io_die_id>` | `transaction`, `tlb_miss` | 统计 IO 页表地址转换总事务数与 IOTLB 穿透缺失次数 |

---

### 3.3 优化点 C3：`< 500µs` 单边内联写上报与中心 `MountedSegment` 描述符一体化构建

为确保从“底层硬件计数器变化”到“中心/远端调度器感知并构建完 `MountedSegment` 能力视图”的全流程严格受控在 **`< 500µs`**：
1. **本地守护线程周期（`200µs ~ 250µs`）**：绑定在隔离管理核上的守护线程每 `200µs` 执行一次本地全指标采集（耗时 `< 1.5µs`）；
2. **单边硬件内联写直推（耗时约 `2µs`，远端 CPU 零拷贝与零中断参与）**：
   每个存储节点仅需上报本机 4 个 SCCL 的 `4 × 64B = 256 Bytes` 端点能力向量。守护线程直接调用 `IBV_WR_RDMA_WRITE` + `IBV_SEND_INLINE`（RoCE）或 `UBMEM` 全局共享窗直写（`stlr` 指令），将 256 字节直接 DMA 覆写进中心 Master 内存中对应 `MountedSegment` 的 `capability_slot` 偏移处；
3. **突变立即触发（Event-Driven Early Push）**：
   若在任意 `200µs` 采样点检测到 `avail_bw_gbps` 跌幅 `> 15%` 或 `pcie_rc_util_bp > 8000`（80%），无需等待周期结束，立即触发单边内联写推送，将端到端感知延迟压至约 `202µs`（远低于 `500µs` 门限）。

---

### 3.4 优化点 C4：防羊群踩踏与防死锁——两级原子在途预扣减与 50ms 僵尸租约自愈（`AtomicReservationSlab`）

在高并发微突发场景下，若多个 Worker 在 `200µs` 采样间隙内感知到同一个空闲 `Segment` 并同时发起请求，会引发多对一突发网络拥塞（Incast）与羊群踩踏。同时，若某个客户端预扣后异常中断，会导致 `inflight_bytes` 永久泄漏产生假拥塞死锁。

为此，实装 **“签发即预扣 + 50ms 租约超时自动冲正”** 闭环机制：
1. **中心签发描述符时原子预扣**：当中心 Master 为请求签发指向 `Segment s`、数据量为 $B_{\text{req}}$ 的边界描述符时，立即执行原子预扣：
   ```cpp
   segment_cap_table[s].inflight_bytes.fetch_add(B_req, std::memory_order_relaxed);
   ```
   并在 `AtomicReservationSlab` 中记录 `{req_id, sccl_id, B_req, expire_tsc = now_tsc + 50ms}`；
2. **纳秒级联动更新排队时延**：同一微秒内到达的后续并发请求在读取 `est_queue_latency_us` 时，直接按更新后的 `inflight_bytes` 算出排队膨胀：
   $$t_{\text{wait\_now}} = \text{est\_queue\_latency\_us} + \frac{\text{inflight\_bytes}}{\text{avail\_bw\_gbps} \times 10^3}$$
   一旦超限立即自动切向备选通道或下发按层 $K^*$ 混合描述符；
3. **带外 10ms 僵尸租约巡检自愈**：守护进程每 `10ms` 扫描一次 `AtomicReservationSlab`，对超过 `50ms` 未收到完成确认的残留预扣条目执行原子 `fetch_sub` 自动冲销，彻底杜绝僵尸泄漏死锁。

---

### 3.5 优化点 C5：严格保障 TPOT `< 3%` 干扰红线——双时间尺度硬件 QoS 闭环控制器（`HierarchicalQoSController`）

针对 100Gbps 后台换出流量冲击前台 Decode TPOT 的问题，为了避免高频修改 Linux `/sys/fs/resctrl` 引发内核锁争用与采样混叠震荡，实施 **快慢双时间尺度解耦控制**：

1. **微观快路径（Fast Path，周期 `200µs`，纯用户态零系统调用）**：
   * 守护线程根据实时测得的 `pcie_rc_util_bp`、`local_ddrc_util_bp` 与 `net_congestion_index`，动态计算峰值压力 $U_{\text{peak}} = \max(\text{ddrc\_util},\; \text{rc\_util},\; \Psi_{\text{cong}})$，直接更新共享内存 `ChannelCapabilityEntryV2.optimal_slice_kb`：
     * 当 $U_{\text{peak}} < 0.40$（空闲）：`optimal_slice_kb = 512`（大块高吞吐换出）；
     * 当 $0.40 \le U_{\text{peak}} < 0.65$（温负载）：`optimal_slice_kb = 256`（标准微切片）；
     * 当 $U_{\text{peak}} \ge 0.65$（前台 Prefill/Decode 密集期）：`optimal_slice_kb = 64`（极细粒度微切片，允许前台高优 TC0 流量在微秒级间隙插队抢占 PCIe RC 与网卡队列，消除队头阻塞）；
2. **宏观慢路径（Slow Path，周期 `20ms` 对齐 Decode Token 步长，硬件寄存器限流）**：
   * 每隔 `20ms`（40 个采样周期），基于滑动指数加权均值（EWMA, $\alpha = 0.3$）与迟滞死区（`[18.35ms, 18.60ms]`），联动下发两级硬件限流执行器：
     * **执行器 1（鲲鹏片上内存与 HHA 带宽阀门）**：向 `/sys/fs/resctrl/bg_kv_group/schemata` 写入 `MB:0=<bg_quota>;1=<bg_quota>...`（动态将后台换出线程组的硬件 `PARTID=2` 的 `MBW_MAX` 配额在 `10%` 至 `85%` 间平滑调节）；
     * **执行器 2（网卡/UB 硬件队列限速器）**：调用 `ibv_modify_qp_rate_limit`（RoCE）或 `urma_modify_jetty`（UB）动态压降后台低优队列（TC1 / VL1）的硬件发包速率上限。

---

## 4. 五大标准化测试场景构造方法与端到端验收方案

基于 `HoloTestbed-4X` 验证平台，定义 5 个正交测试场景的构造方法、执行流程与量化判定门限：

```
+---------------------------------------------------------------------------------------------------------------+
|                        CapabilityMatrix 与硬件 QoS 五大标准验收场景与量化门限总览                             |
+------------------+----------------------------------------+------------------------------+--------------------+
| 场景代号与名称   | 场景构造与瓶颈注入方法                 | 核心观测与度量指标           | 阶段验收 PASS 红线 |
+------------------+----------------------------------------+------------------------------+--------------------+
| S1: 探针极速微基准| 替换 sysfs 为 mmap+DMA Dump，绑核压测  | 8指标本地采集 / 跨机上报耗时 | 本地 <2µs, 上报<500µs|
| S2: IO Die/RC争抢| 同IO Die启动GPU P2P满载 + 4KB碎页冲击  | 有效带宽预测误差 & 选路切换  | 误差 <5%, 零漏判   |
| S3: 20实例微突发 | 4卡真机+16影子协程同微秒发起400并发拉取| 羊群踩踏抑制 & P99 TTFT      | P99 TTFT 降 >=60%  |
| S4: 3GB/s劣质链路| TC/硬件限速注入3~10GB/s拥塞 + 边界掩码 | 决策耗时 & 拥塞 TTFT 净收益  | 决策<2µs, 0%负收益 |
| S5: 100G后台混压 | chaos_blaster注入100Gbps换出流冲击前台 | 前台 Decode TPOT & 有效 QPS  | TPOT干扰<3%, QPS+15%|
+------------------+----------------------------------------+------------------------------+--------------------+
```

---

### 4.1 场景 S1：【微基准验证】`3ms -> <500µs` 全指标采集与中心 `MountedSegment` 上报延迟压测

* **测试目的**：验证优化点 C1 与 C3 是否彻底消灭 2 个 `3ms` 慢指标，确保本地采集 `< 2µs`、跨机单边上报与元数据构建 `< 500µs`。
* **场景构造方法**：
  1. 在存储节点（Socket 1，Core 64）启动升级后的 `holo_pmu_daemon --interval-us 200 --enable-iodie-probe`；
  2. 在计算节点（Socket 0）启动中心 Master，开启 `SegmentCapabilityTable` 接收槽位；
  3. 连续运行 1,000,000 次（200 秒）采集与单边 `RDMA_WRITE_INLINE` 同步循环，利用 `cntvct_el0` 硬件时钟记录每次采集耗时 $\Delta t_{\text{collect}}$ 与中心表时间戳差值 $\Delta t_{\text{e2e}} = \text{now\_tsc}_{\text{master}} - \text{entry.last\_update\_tsc}$。
* **执行指令**：
  ```bash
  sudo ./build/test_m2_t04_probe_latency --iterations 1000000 --check-staleness
  ```
* **验收判定门限（PASS 标准）**：
  * 全部 8 个指标单次本地采集耗时：**Mean `< 1.0µs`，P99.9 `< 2.0µs`**（较前期基线 `3000µs` 提速超 1500 倍），运行期系统调用次数严格为 0；
  * 端到端数据采集 + 单边上报 + 中心 `MountedSegment` 元数据构建刷新延迟：**P99 `< 500µs`**（突发事件触发下 `< 220µs`）。

---

### 4.2 场景 S2：【IO Die 与 PCIe RC 争抢构造实验】验证三段式木桶模型对片上瓶颈的识别精度

* **测试目的**：构造 **“DDRC 内存通道空闲 + 网络光纤空闲，但 IO Die 内部 PCIe RC 或 SMMUv3 发生严重争抢”** 的工况，验证升级后的 `CapabilityMatrix V2.0` 能否精准捕获瓶颈并指导调度器切换通道，对比旧模型的漏判缺陷。
* **场景构造方法（两步注入法）**：
  * **子工况 S2-A（注入同 IO Die PCIe RC 带宽与信用争抢）**：
    在 Socket 0 的 `SCCL 0`（挂载计算卡 0、1 与网卡共享同一 IO Die RC）上，启动后台干扰程序 `pcie_rc_stressor`，让计算卡 0 与计算卡 1 持续通过 PCIe Root Complex 执行高强度 P2P / Pin-Memory DMA 读写（占用该 PCIe RC 约 `45 GB/s` 带宽，但不占用存储池 DDRC 通道，也不发送外部网络包）。
  * **子工况 S2-B（注入 IO Die SMMUv3 IOTLB 页表击穿）**：
    关闭 1GB 巨页聚合，使用 65,536 个离散随机排列的 `4KB` 小页注册 MR 并发起高频散碎 DMA，人为制造 `smmuv3_pmcg/tlb_miss` 与网卡 `mtt_cache_miss`。
* **执行指令**：
  ```bash
  python3 benchmarks/test_m2_t04_iodie_rc_contention.py \
      --stress-mode pcie_rc_and_smmu --target-sccl 0 --backup-sccl 2
  ```
* **验收判定门限（PASS 标准）**：
  1. **瓶颈捕获灵敏度**：在 S2-A 注入后 `500µs` 内，矩阵中 `SCCL 0` 管道的 `pcie_rc_util_bp` 准确升至 `> 8000`（80%），评估可用带宽 `avail_bw_gbps` 从 `40.0 GB/s` 准确降至 **`< 6.0 GB/s`**（与真实 DMA 拉取实测带宽误差 **`< 5%`**；而旧模型仍漏判为 `40.0 GB/s`）；
  2. **S2-B 惩罚系数响应**：在 4KB 碎页冲击下，`iodie_penalty_factor` 自动从 `1.00` 升至 **`> 2.00`**；
  3. **端到端选路收益**：调度器根据矩阵立即将原本发往 `SCCL 0` 的 KVC 拉取请求自动切向处于空闲 IO Die 的 `SCCL 2` 通道（或触发按层切分），使该工况下的 **端到端 TTFT 从旧模型漏判时的 `580ms` 降至 `172ms`（净降幅 `70.3%`）**。

---

### 4.3 场景 S3：【高并发微突发】防羊群踩踏与 50ms 僵尸租约自愈实验

* **测试目的**：验证在高并发微突发下，`MountedSegment` 内嵌 `64B` 矩阵与 `inflight_bytes` 原子预扣减能否消除羊群踩踏，以及客户端异常中断时 `AtomicReservationSlab` 能否在 50ms 内自愈。
* **场景构造方法**：
  1. 利用双轨孪生发生器，由 4 张真机卡 + 16 个绑核影子协程在同一微秒向中心 Master 发起 20 个并发实例（共 400 笔并发查表与描述符申请），目标前缀均命中存储池中的两个副本（`Segment_0` 与 `Segment_1`）；
  2. 在第 10 秒人为终止其中 4 个正在拉取的影子协程（模拟客户端崩溃未归还配额），观察后续请求的选路行为。
* **验收判定门限（PASS 标准）**：
  * **防羊群分流**：20 个并发请求被中心原子预扣机制均匀分流至多个空闲 `Segment`/通道，未发生单通道拥塞死锁，**微突发端到端 P99 TTFT 从无预扣时的 `145.2ms` 降至 `≤ 28.5ms`（降幅达 `80.3%`）**；
  * **僵尸租约自愈**：注入客户端崩溃后，`inflight_bytes` 泄漏在 **`≤ 60ms`（50ms 租约 + 10ms 巡检）** 内被自动冲销清零，系统未发生持续误判回退。

---

### 4.4 场景 S4：【劣质链路动态拥塞】`< 2µs` 边界描述符掩码裁决与负收益熔断实验

* **测试目的**：验证硬件能力矩阵与调度选路引擎、边界按层描述符编译器协同后，在中度拥塞（`8~10 GB/s`）与重度拥塞（`3 GB/s`）下能否彻底消除负收益等待。
* **场景构造方法**：
  使用混沌注入器动态调节链路可用带宽（从 `42 GB/s` 阶梯压降至 `10 GB/s`、`6.5 GB/s`、`3.0 GB/s`），发送 8K~32K（70%~95% 命中）在线推理请求，对比盲目全量拉取与开启矩阵动态裁决后的端到端 TTFT。
* **验收判定门限（PASS 标准）**：
  * **选路决策耗时**：**P99 `< 2.0µs`**；
  * **中度拥塞（`8~10 GB/s`）**：自动下发按层混合边界描述符（重算前 $K^*=25$ 层 + 并发拉取后 55 层），**TTFT 控制在约 `165ms`**（较全量拉取 `320ms` 降低 **`48.4%`**，较全量重算 `240.8ms` 降低 **`31.5%`**）；
  * **重度拥塞（`3.0 GB/s`）**：`< 2µs` 内立即裁决回退至本地重算，**TTFT 从盲目拉取的 `629.9ms ~ 767.0ms` 锁定在 `240.8ms ~ 245.7ms`（净降幅 `61.8% ~ 68.0%`），负收益请求比例 `< 1%`**。

---

### 4.5 场景 S5：【100 Gbps 后台满载换出混压】双时间尺度硬件 QoS 保障前台 Decode TPOT

* **测试目的**：验证优化点 C5（`200µs` 用户态微切片 + `20ms` EWMA 鲲鹏 MPAM `MBW_MAX` 与网卡硬件限流）能否在 100Gbps 后台持续换出压力下，保障前台 Decode **TPOT 干扰率 `< 3%`（`≤ 18.75ms`）** 的红线指标。
* **场景构造方法**：
  1. 启动在线推理前台流（持续运行 8K 上下文 Prefill + 512 Tokens Decode 逐字生成，空载基线 $\text{TPOT}_{\text{base}} = 18.20\text{ ms}$）；
  2. 启动混压注入工具，在同一 SCCL 与同一网卡端口上持续注入 **100 Gbps（`12.5 GB/s`）** 的后台 KVC 换出大流量；
  3. 分别在“无 QoS 控制”、“仅静态限速 15Gbps”、“开启双时间尺度硬件 QoS 闭环”三种模式下各稳态运行 20 分钟，采集前台 TPOT 分布、TTFT P99 与后台实际换出吞吐。
* **验收判定门限（PASS 标准）**：
  1. **前台 Decode TPOT 干扰率**：从无控时的 `21.29ms`（劣化 `+16.98%`）压降至 **`≤ 18.56ms`（干扰率 `1.98% < 3.0%`）**；
  2. **后台换出吞吐**：在前台计算间隙动态放宽 MPAM 配额至 `85%`，使后台平均排出速率维持在 **`≥ 75 Gbps`**，避免静态限速导致的显存换出积压；
  3. **综合服务质量收益**：混压工况下单次有效推理请求的综合计算成本降低，业务按时达标合格率提升，系统在满足延时约束下的有效 QPS 较基线 **提升 `≥ 15%`**。

---

## 5. 四周（W1~W4）攻坚里程碑与工程协作指引

### 5.1 四周倒排攻坚与交付里程碑

| 周次 | 核心攻坚目标 | 关键修改文件 / 模块 | 可验证的量化准出指标 |
| :--- | :--- | :--- | :--- |
| **W1 (11.02~11.08)** | **消灭 `3ms` 慢指标 & 接入 IO Die/RC 探针** | `include/holo_telemetry_probe.hpp`<br>`src/telemetry_daemon.cpp` | 完成 `mmap` 直读与网卡 DMA Auto-Dump 改造；接入 `hisi_pcie`、`smmuv3`、`hisi_hha`；**通过场景 S1 测试（本地采集 `< 2µs`）** |
| **W2 (11.09~11.15)** | **`<500µs` 单边上报 & 中心 `MountedSegment` 闭环** | `mooncake-store/include/segment.h`<br>`include/atomic_reservation_slab.hpp` | 打通 `256B` 节点向量单边内联写上报与三段式木桶合成；合入 50ms 僵尸租约自愈器；**通过场景 S2 & S3 测试（上报 `< 500µs`，预测误差 `< 5%`）** |
| **W3 (11.16~11.22)** | **双时间尺度硬件 QoS 闭环 & 100G 混压调优** | `holosched/scheduler.py`<br>`scripts/02_init_kunpeng_mpam.sh` | 实装 200µs 微切片与 20ms EWMA MPAM `MBW_MAX` / Jetty 硬件限速；**通过场景 S4 & S5 实机混压测试（TPOT 干扰 `< 2.5%`）** |
| **W4 (11.23~11.29)** | **全链路消融对账 & 阶段交付证据包归档** | `benchmarks/run_all_experiments.py`<br>`reconciler/flight_reconciler.py` | 配合调度与系统验证团队完成消融实验，导出 S1~S5 全套置信区间数据表、波形图与回退阈值配置包 |

---

### 5.2 面向工程落地与自动化协作的执行任务模板

工程研发团队在推进代码实现与自测验证时，可依据以下标准化工作项开展落地：

1. **【数据结构升级】**：在 `include/holo_telemetry_probe.hpp` 与 Mooncake `MountedSegment` 头文件中更新 `ChannelCapabilityEntryV2` 结构体（严格校验 `static_assert(sizeof(...) == 64)`），加入 `pcie_rc_util_bp`、`iodie_penalty_factor` 与 `optimal_slice_kb` 字段；
2. **【消灭 3ms 慢采集】**：在 `src/telemetry_daemon.cpp` 中使用第 3.1 节的 `ZeroSyscallPmuReader`（`perf_event_open` + `mmap` Seqlock 直读）替换所有 `/sys/class` VFS 文件读取，并绑定鲲鹏 `hisi_ddrc`、`hisi_pcie`、`smmuv3_pmcg` 与 `hisi_hha` 四个 Uncore PMU 事件源，实现第 2.2 节的三段式全链路木桶公式；
3. **【实现双时间尺度 QoS】**：在调度与控制守护进程中实装第 3.5 节的 200µs 用户态微切片调节与 20ms EWMA MPAM `/sys/fs/resctrl` `MBW_MAX` 硬件配额控制器；
4. **【构造并运行 S1~S5 验收套件】**：编写并执行第 4 节定义的 S1（探针微基准）、S2（IO Die PCIe RC 与 SMMU 争抢注入）、S3（20 实例微突发防羊群与僵尸租约自愈）、S4（3GB/s 拥塞回退与按层掩码）、S5（100Gbps 后台混压保 TPOT < 3%）五个自动化测试脚本，输出实测对账表。
