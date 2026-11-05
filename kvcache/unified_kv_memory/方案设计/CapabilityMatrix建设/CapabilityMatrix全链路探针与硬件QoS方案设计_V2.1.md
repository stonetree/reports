# CapabilityMatrix 全链路探针（含 IO Die/RC $<500\mu\text{s}$）与硬件 QoS 方案设计 (V2.1 基准对齐修订版)

> **版本**：V2.1（严格对齐《11月HOST侧KVC调度面优化与底层硬件遥测详细设计及全链路实验验证方案》修订定稿版）  
> **日期**：2026-11-05  
> **任务代号与名称**：`M2-T04 CapabilityMatrix 全链路探针 (含 IO Die/RC <500µs) 与双时间尺度硬件 QoS 闭环`（对应原 `W3-T05` / `CP-09`）  
> **唯一责任人**：NXY（0.5 FTE，独立统管全链路遥测矩阵与前后台硬件 QoS 闭环）  
> **正交协同接口**：  
> - **WXH（1.0 FTE，`M2-T05` 调度面总控）**：消费 $64\text{B}$ 矩阵执行 $<2\mu\text{s}$ 选路避险、$K^*$ 切分与 Step 1 双桶 Epoch 原子预扣；  
> - **CY（1.0 FTE，`M2-T01`）& YZN（0.5 FTE，`M2-T02`）**：4-SCCL `MemChannel` 物理通道映射与 $256\text{KB} / 2048\text{KB}$ 自适应 WQE 切片协同；  
> - **YY（1.0 FTE，`M2-F02`）& LJQ（0.5 FTE，`M2-F03`）**：SIMD 位平面压缩线程池与 SSD 异步刷盘线程统一纳入后台 MPAM `PARTID_BG` 内存带宽管控。  
> **工程规范说明**：鉴于开发人员均配置有编码 Agent，本文档不堆砌冗长样板代码，重点阐述物理机理、数学闭式方程、$64\text{B}$ 二进制协议布局、跨模块状态机契约以及精确到文件与函数的代码修改锚点。

---

## 零、 冲突审阅结论与对齐修订总表（Audit & Alignment Ledger）

经严格比对前期《CapabilityMatrix全链路探针与硬件QoS方案设计 (V2.0)》与总体基准方案《11月HOST侧KVC调度面优化与底层硬件遥测详细设计及全链路实验验证方案》，共发现 **6 项实质性冲突**。现已全部按总体基准方案完成修正，具体修订对账如下：

| 冲突编号与维度 | 原《CapabilityMatrix 方案 (V2.0)》中的冲突设计 | 基准方案《11月HOST侧调度面优化方案》的权威标准（冲突原因分析） | 本次 V2.1 修订版落位动作 |
| :--- | :--- | :--- | :--- |
| **冲突 1：<br>64B 矩阵粒度与 RDMA 覆写冲突** | 将 `ChannelCapabilityEntryV2` 定义为每个通道 $64\text{B}$（单节点 4 个 SCCL 需 $256\text{B}$），且将 Master 维护的 `inflight_bytes` 放在该结构体内部。 | **冲突根因**：1) $256\text{B}$ 跨 4 条缓存行，单次 RDMA Write Inline 无法保证单缓存行无撕裂读取；2) 若远端每 $500\mu\text{s}$ 用 RDMA 单边写覆盖该结构体，会**直接冲掉中心 Master 刚累加的 `inflight_bytes` 原子预扣值**！<br>**基准标准**：严格采用 **单节点/单 `MountedSegment` 聚合 $64\text{B}$**（内含 `uint16_t[4]` 四通道数组），且**将只读遥测矩阵与 Master 本地预扣计数器物理分离**。 | 已重构 **第 2.1 节** 的 $64\text{B}$ 字节布局表与 **第 3.3 节** 的上报机制，确保 $64\text{B}$ 涵括 4 通道且只写遥测区。 |
| **冲突 2：<br>原子预扣与硬件探针的“双重扣减”** | 第 3.4 节采用单一 `inflight_bytes` 原子累加，直到请求传输完成或 $50\text{ms}$ 超时后才扣减。 | **冲突根因**：请求开始传输 $500\mu\text{s}$ 后，NXY 的硬件 PMU 探针已将该流量反映在 $BW_{\text{avail}}$ 的下降中。若软件继续扣减同一笔 `inflight_bytes`，会导致**物理探针扣一次 + 软件预扣又扣一次（Double-Counting）**，引发大量误回退！<br>**基准标准**：改用 **双桶 Epoch 滑动窗口预扣器（`EpochAwareReservationSlab`）**，仅统计当前 $500\mu\text{s}$ 盲区内的新增突发，新遥测周期到达即清零旧桶。 | 已重写 **第 3.4 节**，实装按 `telemetry_epoch % 2` 翻转的双桶原子预扣与 $50\text{ms}$ 僵尸租约自愈机制。 |
| **冲突 3：<br>ARM Uncore PMU `mmap` 内核陷阱** | 第 3.1.2 节仅使用 `perf_event_open` + `mmap` 读取 `perf_event_mmap_page->offset`，假设其必然支持用户态零系统调用。 | **冲突根因**：在部分 openEuler/Linux 内核版本中，鲲鹏 Uncore PMU 驱动（如 `hisi_sccl_ddrc`）未置位 `cap_user_rdpmc = 1`，直接读 `mmap` 页会读出全 $0$ 或被迫退化为 `read()` 系统调用。<br>**基准标准**：必须增加 **`cap_user_rdpmc` 校验 + `/dev/mem` (`O_RDONLY`) 物理 MMIO 寄存器页直接映射兜底路径**。 | 已升级 **第 3.1.2 节**，确立 `perf_event mmap` 与 `/dev/mem` Uncore MMIO 页直读双保险架构。 |
| **冲突 4：<br>200µs 微切片粒度与 CY 单通道顶速冲突** | 第 3.5.1 节在重负载下将 WQE 切碎至 $64\text{KB}$，且空闲时最大仅放宽至 $512\text{KB}$。 | **冲突根因**：$64\text{KB}$ 过碎会导致网卡 WQE 元数据开销激增，且空闲上限 $512\text{KB}$ 会严重拖累 CY（`M2-T01`）冲击单通道 $>38\text{GB/s}$ 平顶线速！<br>**基准标准**：采用 **前台 Decode 状态感知的二档自适应切片**：前台活跃（`fg_decode_active_bitmap == 1`）时切为 **$256\text{KB}$ 黄金切片**（占 PCIe RC 仅 $6.4\mu\text{s}$，单次门铃批量下发 + $16:1$ 信号汇聚）；前台空闲时恢复 **$2048\text{KB}$ ($2\text{MB}$) 大包**。 | 已修订 **第 3.5.1 节**，对齐 $256\text{KB} / 2048\text{KB}$ 自适应切换律与 16:1 门铃汇聚。 |
| **冲突 5：<br>MPAM 硬件限流遗漏 YY 压缩与 LJQ 刷盘线程** | 第 3.5.2 节仅将 Mooncake 后台网络换出线程放入 MPAM `PARTID=2` 控制组。 | **冲突根因**：11 月 YY 引入了鲲鹏 NEON 位平面流式压缩（内存带宽 $>45\text{GB/s}$），LJQ 引入了 SSD 刷盘。若不将其纳入 `PARTID_BG`，压缩线程会绕过 MPAM 抢占前台 Decode 的 DDRC 带宽！<br>**基准标准**：将 **Mooncake 后台搬运线程 + YY SIMD 压缩线程池 + LJQ `io_uring` 线程** 统一绑定至 `PARTID_BG (ID=2)` 受 `MBW_MAX` 配额刚性约束。 | 已修订 **第 3.5.2 节** 与 **第 5 节** 脚本锚点，实现后台全线程统一 MPAM 围栏管控。 |
| **冲突 6：<br>代码修改路径锚点与源码展示风格不一致** | 散落大段 C++ 实现源码，且文件路径（如 `holosched/scheduler.py`）与主干工程目录不一致。 | **基准标准**：开发人员均配有 Agent，**无需输出详细实现代码**，重点讲清方案机理与数学模型，并将代码修改锚点统一对齐至 `方案设计/调度面优化/testbed/` 与 `mooncake-transfer-engine/` 标准路径。 | 已精简冗余 C++ 源码，并在 **第 3.6 节** 提供精确的文件与函数修改锚点总表。 |

---

## 一、 术语首次出现释义与背景问题定义

### 1.1 核心专有名词释义
1. **CapabilityMatrix（硬件能力矩阵）**：在运行时以微秒级周期自动探测内存控制器、片上 IO 总线、PCIe 根复合体与网卡端口物理状态的 $64\text{B}$ 紧凑结构体，供调度算法在 $<50\text{ns}$ 内无锁读取。
2. **KVCache（大模型注意力键值缓存）**：自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免重复计算。
3. **TTFT（Time To First Token，首字生成延迟）** 与 **TPOT（Time Per Output Token，单字生成延迟）**：衡量在线推理首字响应与逐字生成平滑度的核心业务 SLA 指标（本项目 Decode 基线 $\text{TPOT} = 18.20\text{ms}$，$3\%$ 干扰红线容忍预算仅 $+0.55\text{ms}$，即 $\le 18.75\text{ms}$）。
4. **Goodput（系统有效达标吞吐量 / 有效 QPS）**：在严格满足 TTFT 与 TPOT 服务等级目标（SLO）前提下，系统每秒完成的请求数。
5. **MPAM（Memory System Resource Partitioning and Monitoring）**：ARMv8.4+ / 鲲鹏 920 提供的硬件级 L3 缓存分区（`CPOR`）与内存控制器带宽上限配额（`MBW_MAX`）隔离技术。

---

### 1.2 前期进展复盘与 11 月三大攻坚目标

在 10 月前三周原型攻关中，`CapabilityMatrix` 已打通 $64\text{B}$ 8 个指标的采集链路，其中 6 个指标耗时在微秒/纳秒级，但暴露出三个阻碍端到端业务价值兑现的核心瓶颈：

1. **【采集时延卡点】2 个慢指标耗时高达 $3\text{ms}$（$3000\mu\text{s}$）**：
   读取 DDRC/网卡底层拥塞计数器（通过 `/sys/class` VFS 触发固件 Mailbox）与同步探测耗时达 $3000\mu\text{s}$，比 $<500\mu\text{s}$ 的全网同步目标超标 **6 倍**，比 $<2\mu\text{s}$ 的本地零阻塞采集目标超标 **1500 倍**。
2. **【微架构感知盲区】缺乏对鲲鹏 IO Die 资源争抢与 PCIe Root Complex（RC）瓶颈的建模**：
   在双路鲲鹏 920 多 Die 封装（MCP）架构下，计算 Die（`SCCL`）与挂载外设的 `IO Die`（NimBus）通过片上总线互联。当同 IO Die 下的计算卡发起高强度 DMA、或离散小页击穿共享 `SMMUv3` IOTLB、或跨 NUMA 访问引发 `HHA`（Home Agent）监听风暴时，即便 DDRC 与网线空闲，**IO Die 内部的 PCIe RC 信用队列也会发生严重队头阻塞（HoL Blocking）**，导致旧两段式模型将真实仅剩 $4.76\text{GB/s}$ 的劣质通道误判为 $40.0\text{GB/s}$。
3. **【前后台混压干扰】$100\text{Gbps}$ 后台流量冲垮前台 Decode TPOT**：
   无硬件流控时，$100\text{Gbps}$ 后台 KVC 换出与压缩流量会抢占 PCIe RC 与 DDRC，导致前台 Decode TPOT 从 $18.20\text{ms}$ 劣化至 $21.29\text{ms}$（$+16.98\%$）；而若简单静态限死后台带宽（需压至 $<17.65\text{Gbps}$ 才能保住 $3\%$ 红线），又会导致换出积压与容量崩盘。

```
+-------------------------------------------------------------------------------------------------------------+
|                       鲲鹏 920 多 Die (MCP) 体系结构下的 KVCache 传输三段式物理瓶颈拓扑                       |
+-------------------------------------------------------------------------------------------------------------+
|                                                                                                             |
|  【第一段：计算 Die (SCCL 0~3)】         【第二段：IO Die (NimBus 共享枢纽)】       【第三段：外设与网络】    |
|  +----------------------------+         +----------------------------------+       +---------------------+  |
|  | • 4x DDRC 内存控制器/SCCL  |  HCCS/  | • HHA (跨Die一致性目录与监听过滤) | PCIe  | • CX6/7 或 UB 网卡   |  |
|  |   (flux_rd / flux_wr)      | ──────► | • SMMUv3 (共享IO页表转换 PMCG)   | ────► |   (Port Rx/Tx,      |  |
|  | • L3 Cache (MPAM CPOR/MBW) |  Hydra  | • PCIe Root Complex (RC x16)     | 4.0/  |    ECN/CNP/Credit)  |  |
|  +----------------------------+  环网   |   (TLP 吞吐 & Credit Stall)      | 5.0   +----------+----------+  |
|                                         +-----------------+----------------+                  │             |
|                                                           │ PCIe P2P / H2D 争抢               │ 400G 光纤   |
|                                                           ▼                                   ▼             |
|                                         +----------------------------------+       +---------------------+  |
|                                         | • 同 IO Die 挂载的 GPU / NPU 卡   |       | 远端存储节点 / 交换网|  |
|                                         +----------------------------------+       +---------------------+  |
+-------------------------------------------------------------------------------------------------------------+
```

---

## 二、 $64\text{B}$ `CapabilityMatrix` 缓存行对齐协议与三段式木桶数学模型

### 2.1 严格对齐单条缓存行的 $64\text{B}$ 二进制协议定义（`CapabilityMatrixEntry`）

为了确保：① 单次 `IBV_WR_RDMA_WRITE_INLINE` 即可完成整个节点（全部 4 个 `MemChannel`）的能力状态原子同步；② 在中心 Master 的 `MountedSegment` 中严格占据 1 条 CPU L1/L3 Cache Line（$64\text{ Bytes}$），避免跨缓存行读取撕裂；③ **与中心 Master 的在途预扣计数器物理隔离，防止远端 RDMA 单边写覆盖 Master 本地预扣状态**，定义 `struct alignas(64) CapabilityMatrixEntry` 的精确二进制布局如下：

| 字节偏移 (Byte Offset) | 字段名称 (Field Name) | 数据类型 | 物理含义、量纲与设计目的说明 |
| :---: | :--- | :---: | :--- |
| `0x00 ~ 0x07` (8B) | `epoch_and_version` | `uint64_t` | 高 48 位为单调递增的 $500\mu\text{s}$ 遥测周期号 `telemetry_epoch`（驱动 WXH 双桶预扣翻转）；低 16 位为 Seqlock 奇偶校验版本号。 |
| `0x08 ~ 0x0F` (8B) | `timestamp_ns` | `uint64_t` | 采集时刻的 ARMv8 `cntvct_el0` 硬件纳秒时间戳（供调度侧执行 $>2\text{ms}$ 保鲜期校验）。 |
| `0x10 ~ 0x17` (8B) | `ch_avail_bw_mbps[4]` | `uint16_t[4]` | 节点内 4 个 `MemChannel`（对应 4 个 SCCL/Port）经三段式木桶方程合成后的瞬时可用带宽，单位：$10\text{ MB/s}$（量程 $0 \sim 655.35\text{ GB/s}$）。 |
| `0x18 ~ 0x1F` (8B) | `ddrc_util_permille[4]` | `uint16_t[4]` | 4 个 SCCL 的计算 Die DDRC 内存控制器带宽利用率千分比（$0 \sim 1000\text{‰}$）。 |
| `0x20 ~ 0x27` (8B) | `pcie_rc_stall_permille[4]` | `uint16_t[4]` | **【新增】** 4 个通道对应 IO Die PCIe RC 的带宽饱和度与信用停顿千分比（$0 \sim 1000\text{‰}$）。 |
| `0x28 ~ 0x2B` (4B) | `iodie_alpha_fp8[4]` | `uint8_t[4]` | **【新增】** 4 个通道的 IO Die 拥塞惩罚因子 $\alpha_{\text{iodie}} \ge 1.0$ 定点数编码：`val = round((alpha - 1.0) * 100)`（量程 $1.00 \sim 3.55$）。 |
| `0x2C ~ 0x2F` (4B) | `nic_queue_depth_kb[2]` | `uint16_t[2]` | 主备双物理网卡端口当前未完成 WQE 排队深度（单位：$64\text{ KB}$），用于计算基礎排队等待时延。 |
| `0x30 ~ 0x33` (4B) | `active_mpam_quota_pct[4]` | `uint8_t[4]` | 当前 4 个 SCCL 上后台任务控制组（`PARTID_BG`）的 MPAM `MBW_MAX` 配额百分比（$10\% \sim 85\%$）。 |
| `0x34 ~ 0x37` (4B) | `tier_free_cap_gb[2]` | `uint16_t[2]` | 本节点 Host DDR 池与 NVMe SSD 池的剩余可用容量（单位：$100\text{ MB}$），供 WXH 纵向升降级决策消费。 |
| `0x38 ~ 0x3B` (4B) | `fg_decode_active_bitmap` | `uint32_t` | 前台在线 Decode 活跃状态位图与实时 TPOT 裕度标志（触发 $200\mu\text{s}$ 快路径 $256\text{KB} / 2048\text{KB}$ 切片切换）。 |
| `0x3C ~ 0x3F` (4B) | `health_flags_and_crc` | `uint32_t` | 链路健康度标志位（`BIT0: LINK_UP`, `BIT1: RC_STALLED`, `BIT2: IODIE_HOT`, `BIT3: ECN_ALERT`）与 16-bit 校验和。 |

---

### 2.2 三段式全链路木桶定律与 IO Die 争抢融合数学模型

设遥测采样周期为 $\Delta t = 500\mu\text{s}$。对于节点内的任意物理通道 $p = (s, r, k)$（即绑定在计算 Die `SCCL` $s$、IO Die `PCIe RC` $r$、网卡物理端口 $k$ 上的 `MemChannel`），其全链路瞬时物理可用带宽 $BW_{\text{avail}}^{(p)}(t)$ 由 **三段式木桶短板方程** 唯一确定：

$$BW_{\text{avail}}^{(p)}(t) = \frac{1}{\alpha_{\text{iodie}}^{(p)}(t)} \cdot \max\left(BW_{\text{min}}, \;\; \min\Big(BW_{\text{DDRC\_rem}}^{(s)}(t), \;\; BW_{\text{PCIe\_RC\_rem}}^{(r)}(t), \;\; BW_{\text{Port\_rem}}^{(k)}(t)\Big)\right)$$

其中三大物理段余量与惩罚因子的闭式计算方程如下：

1. **第一段：计算 Die 本地 SCCL 内存控制器剩余带宽 $BW_{\text{DDRC\_rem}}^{(s)}(t)$**：
   读取 `hisi_sccl<s_id>_ddrc<0..3>` 的读写 Flux 硬件计数器增量（每次 burst 传输对应字节数 $B_{\text{burst}} = 64\text{B}$，或按芯片步进标定值）：
   $$BW_{\text{DDRC\_used}}^{(s)}(t) = \frac{\sum_{c=0}^{3} \left(\Delta \text{flux\_rd}_{s,c} + \Delta \text{flux\_wr}_{s,c}\right) \cdot B_{\text{burst}}}{\Delta t}, \quad BW_{\text{DDRC\_rem}}^{(s)}(t) = BW_{\text{DDRC\_peak}}^{(s)} - BW_{\text{DDRC\_used}}^{(s)}(t)$$

2. **第二段：IO Die PCIe Root Complex 剩余有效吞吐 $BW_{\text{PCIe\_RC\_rem}}^{(r)}(t)$**：
   读取 IO Die `hisi_pcie<sccl>_<rc_core>` 的读写 TLP 报文统计（`rx_mwr_tlp`, `tx_mrd_tlp`）以及出向信用耗尽停顿周期比 $r_{\text{rc\_stall}}^{(r)}(t) = \frac{\Delta \text{tx\_credit\_stall\_cycles}}{\Delta \text{total\_cycles}}$：
   $$BW_{\text{PCIe\_RC\_rem}}^{(r)}(t) = \left(1 - r_{\text{rc\_stall}}^{(r)}(t)\right) \cdot \left(BW_{\text{PCIe\_peak}}^{(r)} - BW_{\text{PCIe\_used}}^{(r)}(t)\right)$$

3. **第三段：网卡物理端口剩余可用带宽 $BW_{\text{Port\_rem}}^{(k)}(t)$**：
   结合网卡硬件 DMA Auto-Dump 刷写的收发字节数与 RoCE ECN/CNP（或 URMA Credit）拥塞折损因子 $\Psi_{\text{cong}}^{(k)}(t) \in [0, 1)$：
   $$BW_{\text{Port\_rem}}^{(k)}(t) = BW_{\text{Port\_peak}}^{(k)} \cdot \left(1 - \Psi_{\text{cong}}^{(k)}(t)\right) - BW_{\text{Port\_used}}^{(k)}(t)$$

4. **IO Die 综合争抢与地址转换惩罚因子 $\alpha_{\text{iodie}}^{(p)}(t) \ge 1.0$**：
   采集 IO Die 上 `smmuv3_pmcg` 的 IOTLB 缺失率 $r_{\text{tlb\_miss}}(t) = \frac{\Delta \text{tlb\_miss}}{\Delta \text{transaction}}$ 以及 `hisi_hha` 的跨 Die 远程访问与监听冲突率 $r_{\text{cross\_die}}(t) = \frac{\Delta \text{rx\_outer}}{\Delta \text{rx\_ops\_num}}$：
   $$\alpha_{\text{iodie}}^{(p)}(t) = 1.0 + \beta_1 \cdot \max\left(0, \; r_{\text{tlb\_miss}}(t) - \theta_{\text{tlb}}\right) + \beta_2 \cdot \max\left(0, \; r_{\text{cross\_die}}(t) - 0.05\right)$$
   *(标定参数：$\beta_1 = 20.0, \beta_2 = 2.0, \theta_{\text{tlb}} = 0.001$。当开启 $1\text{GB}$ 巨页 + `iommu.passthrough=1` 且严格同 SCCL 亲和绑定时，$r_{\text{tlb\_miss}} = 0, r_{\text{cross\_die}} < 0.05 \implies \alpha_{\text{iodie}} = 1.00$；当发生 $4\%$ 页表缺失与 $20\%$ 跨 Die 冲突时，$\alpha_{\text{iodie}} \approx 2.10$，前置将预测带宽折半。)*

---

## 三、 五大核心工程设计方案与代码修改锚点

### 3.1 优化点 C1：消灭 2 个 $3\text{ms}$ 慢指标——具备 `/dev/mem` MMIO 双保险的零系统调用采集引擎（$< 2\mu\text{s}$）

#### （1）$3\text{ms}$ 慢指标根因剖析
前期原型中耗时 $3000\mu\text{s}$ 的两个指标，一是每次调用 VFS `open/read` 读取 `/sys/bus/event_source/devices/hisi_sccl_ddrc*` 与 `/sys/class/infiniband/mlx5_*/hw_counters/*`，触发内核上下文切换及网卡驱动向固件下发阻塞式 PCIe Mailbox 命令（`MLX5_CMD_OP_QUERY_Q_COUNTER`）；二是同步 Ping-Pong 探测网络 RTT。

#### （2）零系统调用三级直读架构（含微架构专家级 MMIO 兜底路径）
1. **片上 Uncore PMU 采集（`perf_event mmap` + `/dev/mem` MMIO 只读映射双保险）**：
   * **路径 A（标准 `perf_event_mmap_page`）**：初始化时调用 `perf_event_open()` 并将 fd `mmap` 到用户空间。
   * **路径 B（针对 ARM Uncore PMU `cap_user_rdpmc == 0` 的 `/dev/mem` 物理 MMIO 映射兜底）**：在部分 Linux 内核版本下，ARM Uncore PMU 驱动未开启用户态指令直读（`pc->cap_user_rdpmc == 0`），直接读 `mmap` 页会返回 $0$ 或退化为系统调用。因此，在 `KunpengUncorePmuReader::init()` 中自动校验 `cap_user_rdpmc`；若为 `0`，立即通过 `/dev/mem`（以 `O_RDONLY` 只读安全权限）将鲲鹏 920 片上 DDRC、PCIe RC、SMMUv3、HHA 的物理 MMIO 计数寄存器页直接映射至守护进程虚拟地址空间。运行时仅执行一条 ARM64 `ldr` 内存加载指令（单指标耗时 $< 150\text{ns}$，$100\%$ 零系统调用）。
2. **网卡物理端口计数器采集（硬件 DMA Auto-Dump）**：
   * 初始化阶段调用 `mlx5dv_devx_obj_create`（或 URMA 计数器映射接口）创建硬件 Bulk Counter 并绑定至预分配的 Host Pinned Memory，配置网卡微码以 $250\mu\text{s}$ 周期自动通过 DMA 刷写计数器镜像页。CPU 采集仅需一次指针解引用（耗时 $< 20\text{ns}$）。
3. **带内被动 RTT 测距**：
   * 废弃主动发包探测，直接在正常 RDMA/URMA 数据面收割 CQE 时读取硬件完成时间戳差值（`cqe_timestamp - wqe_post_timestamp`），原子更新至共享内存。

---

### 3.2 优化点 C2：鲲鹏 920 片上四类 Uncore PMU 硬件事件精确映射表

守护进程启动时，按双路鲲鹏 920 的拓扑绑定以下 4 类硬件事件源：

| 物理子系统层级 | 鲲鹏 Uncore PMU 设备节点 / MMIO 基址组 | 绑定采集的硬件事件名 | 合成输出至 $64\text{B}$ 矩阵的目标字段 |
| :--- | :--- | :--- | :--- |
| **1. 计算 Die 内存层** | `hisi_sccl<0..3>_ddrc<0..3>` | `flux_rd`, `flux_wr` | `ddrc_util_permille[4]` & $BW_{\text{DDRC\_rem}}^{(s)}$ |
| **2. 计算 Die 一致性层** | `hisi_sccl<0..3>_hha<0..1>` | `rx_ops_num`, `rx_outer`, `snoop_lookup` | 计算跨 Die 比例 $r_{\text{cross\_die}} \to$ `iodie_alpha_fp8[4]` |
| **3. IO Die 根复合体层** | `hisi_pcie<sccl>_<rc_core>` | `rx_mwr_tlp`, `tx_mrd_tlp`, `tx_credit_stall` | `pcie_rc_stall_permille[4]` & $BW_{\text{PCIe\_RC\_rem}}^{(r)}$ |
| **4. IO Die 地址转换层** | `smmuv3_pmcg_<io_die_id>` | `transaction`, `tlb_miss` | 计算页表缺失率 $r_{\text{tlb\_miss}} \to$ `iodie_alpha_fp8[4]` |

---

### 3.3 优化点 C3：$<500\mu\text{s}$ 单边 RDMA Write Inline 上报与中心表无打扰同步

1. **双写同步机制**：
   绑核守护线程 `telemetry_daemon` 每 $500\mu\text{s}$（支持带宽突变 $>15\%$ 时在 $200\mu\text{s}$ 内提前触发）完成一次 8 指标采集与三段式方程合成（本地耗时 $<1.5\mu\text{s}$）：
   * **本地同步**：通过 Seqlock（递增 `epoch_and_version`）写入本机共享内存 `/dev/shm/kvc_cap_matrix`（耗时 $<50\text{ns}$），供本机 QoS 控制器与本地调度器读取；
   * **跨机中心同步**：调用 `IBV_WR_RDMA_WRITE_INLINE` 将 $64\text{B}$ `CapabilityMatrixEntry` 直接单边 DMA 写入中心 Master 节点的 `SegmentCapabilityTable[node_id]` 独立缓存行槽位。
2. **零 CPU 打扰与防完成队列溢出**：
   每个存储节点的 $64\text{B}$ 槽位严格按 $64\text{B}$ 边界对齐（消除多节点并发 RDMA 写入时的 L3 Cache False Sharing）；发送端每 $16$ 次内联写才置位一次 `IBV_SEND_SIGNALED` 清理发送 CQ，中心 Master 节点 CPU 全程零中断、零轮询开销。

---

### 3.4 优化点 C4：防羊群踩踏且根除“双重扣减”——双桶 Epoch 滑动窗口原子预扣器（`EpochAwareReservationSlab`）

#### （1）为什么必须摒弃传统的单一 `inflight_bytes` 预扣？
若在请求开始前累加 `inflight_bytes` 并在请求完成（例如 $15\text{ms}$ 后）才扣减，那么当请求传输了 $500\mu\text{s}$、NXY 的下一拍硬件遥测矩阵（周期 $E_{k+1}$）上报时，硬件 PMU 已经实测到了该请求占用的带宽并下调了 `ch_avail_bw_mbps`。如果此时调度器继续从更新后的 `ch_avail_bw_mbps` 中减去该请求的 `inflight_bytes`，就会把同一份流量 **在硬件探针和软件计数器里重复扣减两次（Double-Counting）**，造成严重的虚假拥塞与误回退！

#### （2）双桶 Epoch 滑动窗口预扣机制（协同 WXH `M2-T05`）
在中心 Master 的 `MountedSegment` 结构中（独立于远端 RDMA 写入的 $64\text{B}$ 遥测区之外），为每个通道 $p \in \{0,1,2,3\}$ 维护两个无锁原子预扣桶 `std::atomic<uint64_t> pending_bytes[4][2]`：
1. **Step 1 签发时按当前 Epoch 入桶**：
   读取 $64\text{B}$ 矩阵中的当前遥测周期号 $E_k = \text{cap\_matrix.telemetry\_epoch}$。当为请求签发通道 $p$、压缩后传输字节数为 $S_{\text{payload}} = L_{\text{match}} \cdot S_{\text{token}} \cdot \gamma_{\text{comp}}$ 时，原子累加至当前周期桶：
   $$\text{atomic\_fetch\_add}\left(\text{pending\_bytes}[p][E_k \bmod 2], \;\; S_{\text{payload}}\right)$$
2. **瞬时有效可用带宽折减计算**：
   同一 $500\mu\text{s}$ 盲区窗口内后续到达的并发请求，实时读取到的有效可用带宽 $BW_{\text{eff}}^{(p)}(t)$ 为：
   $$BW_{\text{eff}}^{(p)}(t) = \max\left(BW_{\text{min}}, \;\; BW_{\text{avail}}^{(p)}(E_k) - \frac{\text{pending\_bytes}[p][E_k \bmod 2]}{\tau_{\text{window}}}\right)$$
   当 $20 \times 20$（$400$ 并发）微突发在几十微秒内同时命中同一前缀时，后续请求立即感知到 $BW_{\text{eff}}^{(p)}$ 下降，自动分流至其他空闲 `MemChannel` 或切换为 $K^*$ 按层混合重算，**彻底粉碎网卡 Incast 羊群踩踏**。
3. **Epoch 翻转原子清零与 $50\text{ms}$ 僵尸租约自愈**：
   一旦检测到 NXY 单边写入了新的遥测周期 $E_{k+1}$（即 $E_k$ 周期内下发的请求已经进入物理网卡/DDRC 并被硬件 PMU 捕获），调度器立即将旧桶 `pending_bytes[p][E_k % 2]` 原子置零（`exchange(0)`），**$100\%$ 消除跨周期双重扣减**。对于已签发但客户端崩溃未能实际发起 DMA 的异常情况，由后台 $50\text{ms}$ 僵尸租约回收器自动兜底冲销。

---

### 3.5 优化点 C5：死守 TPOT $< 3\%$ 红线且不伤单通道峰值——双时间尺度硬件 QoS 控制器（`HierarchicalQoSController`）

```
           前台在线推理请求 (PARTID_FG: 锁4路L3, MBW=100%, 高优QP SL=5) ────┐
                                                                            ├──► 共享 PCIe RC / DDRC / 网卡端口
后台 KVC 换出 + YY SIMD 压缩 + LJQ SSD 刷盘 (PARTID_BG: 低优QP SL=1) ───┐    │
                                                                       ▼    ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│                 NXY 双时间尺度硬件 QoS 协同控制器 (HierarchicalQoSController)      │
│                                                                                   │
│  ┌─────────────────────────────────────────┐  ┌────────────────────────────────┐  │
│  │ 快路径 (Fast Path: 200µs, 用户态零系统调用)│  │ 慢路径 (Slow Path: 20ms, 硬件级)│  │
│  │ • 监测 fg_decode_active_bitmap          │  │ • EWMA 监测前台 TPOT 增量      │  │
│  │ • Decode 活跃: 256KB 黄金微切片 (6.4µs) │  │ • 写 MPAM PARTID_BG MBW_MAX    │  │
│  │ • Decode 空闲: 2048KB 大包保 CY 顶速平顶│  │   (10%~85%) + 网卡 QP 硬件限速 │  │
│  └─────────────────────────────────────────┘  └────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────────────────┘
```

#### （1）快路径控制（$200\mu\text{s}$ 尺度：前台状态感知的 $256\text{KB} / 2048\text{KB}$ 自适应微切片）
* **兼顾防阻塞与 CY 单通道顶速平顶（修复专家质询 3.2）**：
  后台 RDMA/URMA 传输引擎在提交任务时，直接检查共享内存 `CapabilityMatrixEntry` 中的 `fg_decode_active_bitmap`：
  $$S_{\text{slice}}(t) = \begin{cases} 
  256\text{ KB}, & \text{if } \text{fg\_decode\_active\_bitmap}^{(s)} \neq 0 \quad (\text{前台 Decode 活跃期：黄金微切片防 RC 阻塞}) \\
  2048\text{ KB}, & \text{if } \text{fg\_decode\_active\_bitmap}^{(s)} == 0 \quad (\text{前台 Decode 空闲期：恢复 2MB 大包跑满 >38GB/s})
  \end{cases}$$
* **物理算账与批量门铃优化**：在 PCIe 4.0/5.0 $\times 16$（$\sim 40\text{GB/s}$）总线上，单个 $256\text{KB}$ 切片占用的连续传输时间仅为 $\frac{256\text{KB}}{40\text{GB/s}} = 6.4\mu\text{s}$。前台高优先级队列（`SL = 5, DSCP = 46`）在网卡与 PCIe RC 硬件仲裁器中的最大排队等待时间被严格封顶在 **$6.4\mu\text{s}$**，彻底消除队头阻塞；同时，后台将 $2\text{MB}$ 数据拆成 $8$ 个 $256\text{KB}$ WQE 后，通过**单次 `ibv_post_send` 链表批量敲击一次 Doorbell，且每 $16$ 个 WQE 才开启一次 `IBV_SEND_SIGNALED`**，确保微切片下依然维持 $>95\%$ 的物理带宽效率。

#### （2）慢路径控制（$20\text{ms}$ 尺度：统一围栏管理后台搬运、SIMD 压缩与 SSD 刷盘的 MPAM 配额）
* **全后台线程统一入笼（修复专家质询 2.1）**：
  在鲲鹏 `/sys/fs/resctrl` 下建立两个硬件资源控制组：
  * `PARTID_FG (ID=1)`：绑定前台 vLLM/SGLang 推理线程与控制面快查线程，配置 L3 缓存独占 4 路（`CPOR = 0x000F`）且内存带宽无限制（`MBW_MAX = 100%`）；
  * `PARTID_BG (ID=2)`：**将 Mooncake 后台换出/拉取线程、YY 的鲲鹏 NEON 位平面流式压缩/解压线程池（`>45 GB/s` 访存大户）以及 LJQ 的 `io_uring` SSD 线程全部写入 `/sys/fs/resctrl/PARTID_BG/tasks`**。
* **PI 闭环硬件配额调节律**：
  每隔 $20\text{ms}$（对齐 Decode 单步周期），计算前台 TPOT 相对空载基线（$18.20\text{ms}$）的指数移动平均增量 $\Delta \text{TPOT}_{\text{ewma}}(t)$，动态调节 `PARTID_BG` 的 `MBW_MAX` 硬件寄存器配额 $Q_{\text{bg}}(t) \in [10\%, 85\%]$：
  $$Q_{\text{bg}}(t + \Delta t) = \text{clip}\left(Q_{\text{bg}}(t) - K_p \cdot \left(\Delta \text{TPOT}_{\text{ewma}}(t) - 0.30\text{ms}\right), \;\; 10\%, \;\; 85\%\right)$$
  同时同步调用 `ibv_modify_qp_rate_limit` / `urma_modify_jetty` 调整后台低优队列硬件速率上限。在将前台 TPOT 干扰死死压在 **$+0.36\text{ms}$（$\le 18.56\text{ms}$，干扰率 $<2.0\%$）** 的同时，使后台安全换出速率从无控越线点的 $17.65\text{Gbps}$ 大幅提升至 **$\ge 75\text{Gbps}$（放大 $4.25\times$）**。

---

### 3.6 `M2-T04` 核心代码修改位置与接口锚点清单（供开发人员与编码 Agent 执行）

| 所属模块 | 目标文件路径锚点 (Filepath Anchor) | 核心类 / 函数修改点与具体改造逻辑说明 |
| :--- | :--- | :--- |
| **1. 64B 矩阵与双桶预扣头文件** | `mooncake-store/include/metadata.h` | • 定义 `struct alignas(64) CapabilityMatrixEntry`（严格对齐第 2.1 节 $64\text{B}$ 字节表，内嵌于 `MountedSegment`）；<br>• 在 `MountedSegment` 中独立定义 `EpochAwareReservationSlab`（含 `std::atomic<uint64_t> pending_bytes_[4][2]` 与 `on_new_telemetry_epoch()` 翻转清零接口）。 |
| **2. 零系统调用遥测守护进程** | `方案设计/调度面优化/testbed/src/telemetry_daemon.cpp` | • 实现 `KunpengUncorePmuReader`：初始化 `hisi_sccl_ddrc`、`hisi_pcie`、`smmuv3_pmcg`、`hisi_hha`，校验 `cap_user_rdpmc`，若为 `0` 自动调用 `mmap_uncore_mmio_readonly()` 映射 `/dev/mem` 物理计数器页；<br>• 实现 `NicCounterDmaReader`：读取 `mlx5dv_devx` 自动刷写的 Host Pinned 内存页；<br>• 在 `run_telemetry_loop()` 中每 $500\mu\text{s}$ 解算第 2.2 节三段式方程，更新 `/dev/shm/kvc_cap_matrix` 并通过 `post_rdma_write_inline_64b()` 单边上报。 |
| **3. 硬件隔离与 MPAM 初始化** | `方案设计/调度面优化/testbed/02_hardware_isolation.sh` | • 挂载 `/sys/fs/resctrl`，创建 `PARTID_FG` (`L3:0=000f`, `MB:0=100`) 与 `PARTID_BG` (`MB:0=85`)；<br>• 提供线程注册钩子，将 Mooncake 后台搬运 TID、YY SIMD 压缩线程池 TID、LJQ `io_uring` 线程 TID 全部绑定至 `PARTID_BG/tasks`；<br>• 配置网卡硬件 QoS 优先级映射（前台 `SL=5/TC5`，后台 `SL=1/TC1`）。 |
| **4. 快路径自适应微切片** | `mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp` | • 在 `submitTransferRequest()` 中无锁读取本地共享内存 `cap_matrix->fg_decode_active_bitmap`；<br>• 动态选择 `slice_size = (fg_active ? 256KB : 2048KB)`，将切分后的 WQE 串成单向链表通过单次 `ibv_post_send()` 下发，每 $16$ 个 WQE 置位一次 `IBV_SEND_SIGNALED`。 |
| **5. 慢路径双时间尺度控制器** | `mooncake-transfer-engine/include/qos/hierarchical_qos_controller.h` *(新增)* | • 实现 `HierarchicalQoSController::step_slow_path_20ms()`：计算前台 TPOT 的 EWMA 增量，通过常驻打开的 `fd_schemata` 执行 `pwrite()` 动态更新 4 个 SCCL 的 `MBW_MAX` 配额（$10\% \sim 85\%$）及网卡 QP 硬件限速值。 |

---

## 四、 五大标准化测试场景构造方法与端到端门禁验收方案

依托单机 4 卡双命名空间光纤物理环回平台 **`HoloTestbed-4X`**（Socket 0 为存储池发送域，Socket 1 为推理接收域，环境变异系数 $\text{CV} < 4.8\%$，统一采用 ARM `cntvct_el0` 硬件寄存器执行 7 锚点纳秒打点），定义 `M2-T04` 的 5 个标准化验收场景：

```
+---------------------------------------------------------------------------------------------------------------+
|                      CapabilityMatrix 与硬件 QoS 五大标准验收场景与量化 PASS 红线总览                         |
+------------------+----------------------------------------+------------------------------+--------------------+
| 场景代号与名称   | 场景构造与瓶颈注入方法                 | 核心观测与度量指标           | 阶段验收 PASS 红线 |
+------------------+----------------------------------------+------------------------------+--------------------+
| S1: 探针极速微基准| 替换 sysfs 为 mmap/MMIO+DMA Dump 压测  | 8指标本地采集 / 单边上报耗时 | 本地<2µs, 上报<500µs|
| S2: IO Die/RC争抢| 同IO Die启动卡间P2P满载 + 4KB碎页冲击  | 有效带宽预测误差 & 选路切换  | 误差 <8%, 零漏判   |
| S3: 20实例微突发 | 20实例×20并发(400请求)同毫秒命中同前缀 | 双桶防重扣、防踩踏 & P99 TTFT| P99 TTFT 降 >60%   |
| S4: 3GB/s劣质链路| 硬件限速注入 3~15GB/s 拥塞 + K* 切分   | 决策耗时 & 拥塞 TTFT 净收益  | 决策<2µs, 0%负收益 |
| S5: 100G后台混压 | 注入 100Gbps 换出+SIMD压缩冲击前台解码 | 前台 Decode TPOT & 有效 QPS  | TPOT干扰<2%, QPS+15%|
+------------------+----------------------------------------+------------------------------+--------------------+
```

---

### 4.1 场景 S1：【微基准验证】`3ms -> <500µs` 零系统调用采集与单边内联写上报压测

* **测试目的**：验证优化点 C1 与 C3 是否彻底根除 2 个 $3\text{ms}$ 慢指标，确保无论内核是否开启 `cap_user_rdpmc`，本地 8 指标采集均 $< 2\mu\text{s}$、端到端单边上报与中心表刷新均 $< 500\mu\text{s}$。
* **执行步骤**：
  1. 在发送域（Socket 0）独立管理核启动 `telemetry_daemon`，连续执行 $1,000,000$ 次全量 8 指标采集（含 DDRC、PCIe RC、SMMUv3、HHA、NIC Auto-Dump）与三段式方程合成；
  2. 每 $500\mu\text{s}$ 通过 `IBV_WR_RDMA_WRITE_INLINE` 将 $64\text{B}$ 结构体单边写至接收域（Socket 1）的 `MountedSegment` 槽位；
  3. 对比双方 `cntvct_el0` 物理时钟戳（单机双域同源晶振，时钟偏移严格为 $0\text{ns}$），统计本地采集耗时与端到端可见性延迟。
* **验收 PASS 红线**：
  * 本地 8 指标采集 + 三段式合成总耗时：**$\text{Mean} < 1.0\mu\text{s}$，$\text{P99.9} < 2.0\mu\text{s}$**（运行期系统调用次数严格为 $0$）；
  * 采集 + 单边 RDMA 内联写 + 中心表构建全周期：**$\text{P99} < 500\mu\text{s}$**。

---

### 4.2 场景 S2：【IO Die 与 PCIe RC 争抢注入实验】验证三段式木桶模型精度

* **测试目的**：构造 **“DDRC 内存通道空闲 + 网络光纤空闲，但 IO Die 内部 PCIe RC 或 SMMUv3 发生严重争抢”** 的特定工况，验证三段式木桶模型能否精准识别瓶颈并指导 WXH 选路器避开劣化通道。
* **执行步骤**：
  1. **子工况 S2-A（注入同 IO Die PCIe RC 信用争抢）**：在 `SCCL 0` 所属的 IO Die 上，启动干扰负载让同挂载在该 PCIe RC 下的计算卡持续执行高强度本地 P2P / Host-Device DMA（占满该 PCIe RC 约 $85\%$ 带宽，但不走外部网线，也不占 `SCCL 1~3` 的 DDRC）；
  2. **子工况 S2-B（注入 SMMUv3 IOTLB 击穿与跨 Die HHA 冲突）**：构造未对齐的 $4\text{KB}$ 离散碎页跨 Die DMA 流，人为拉高 `tlb_miss` 与 `hha_rx_outer`；
  3. 观察 `CapabilityMatrixEntry` 中 `ch_avail_bw_mbps[0]`、`pcie_rc_stall_permille[0]` 与 `iodie_alpha_fp8[0]` 的响应波形及真实 KVC 传输吞吐。
* **验收 PASS 红线**：
  * 三段式模型预测可用带宽 $BW_{\text{avail}}^{(0)}(t)$ 与实测 DMA 吞吐的相对误差 **$< 8\%$**（准确从 $40\text{GB/s}$ 压降至 $<6\text{GB/s}$，彻底消除旧两段式模型的漏判）；
  * WXH 选路器在 $<2\mu\text{s}$ 内自动将请求切向空闲的 `SCCL 2/3` 通道，使该工况下的端到端 TTFT 较盲目走 `SCCL 0` 降低 **$\ge 65\%$**。

---

### 4.3 场景 S3：【SWE-bench 400 并发微突发】双桶 Epoch 防羊群踩踏与防双重扣减实测

* **测试目的**：验证优化点 C4（`EpochAwareReservationSlab` 双桶原子预扣）在 $20$ 实例 $\times 20$ 并发微突发下能否消除网卡 Incast 踩踏，且在跨 $500\mu\text{s}$ 周期时无双重扣减误判。
* **执行步骤**：
  1. 使用 SWE-bench 轨迹回放器在同一毫秒窗口内突发注入 $400$ 个并发请求（共享 $32\text{K}$ 前缀，$98\%$ 命中 + $512$ 新增 Token）；
  2. 分别测试三种模式：① 关闭预扣；② 旧版单一 `inflight_bytes` 预扣（直至传输结束才扣减）；③ V2.1 双桶 Epoch 滑动窗口预扣（新遥测周期到达即清零旧桶）；
  3. 统计网卡端口排队深度、误回退重算率、微突发 $\text{P99 TTFT}$ 以及稳态高负载下的系统有效吞吐（Goodput）。
* **验收 PASS 红线**：
  * 相比旧版单一预扣，双桶 Epoch 预扣将跨周期双重扣减导致的**误回退率从 $>18\%$ 压降至 $< 1\%$**；
  * 相比无预扣，微突发 **$\text{P99 TTFT}$ 降幅 $> 60\%$**（从 $145.2\text{ms}$ 压降至 $\le 28.5\text{ms}$），稳态高并发下系统 **有效吞吐（Goodput）提升 $12\% \sim 18\%$**；客户端崩溃注入后残留预扣在 $\le 60\text{ms}$ 内 $100\%$ 自愈清零。

---

### 4.4 场景 S4：【3~15 GB/s 拥塞链路】协同 WXH `<2µs` 选路避险与 LDY $K^*$ 混合切分实测

* **测试目的**：验证 `CapabilityMatrix` 驱动 WXH `CostEvaluator`（含压缩比 $\gamma_{\text{comp}}$ 与 Slab 描述符数 $N_{\text{slab\_desc}}$）及 LDY 按层双缓冲引擎在恶劣与中度拥塞下的业务收益。
* **执行步骤**：
  1. 将物理通道可用带宽分别限制在 $42\text{GB/s}$（健康）、$12\text{GB/s}$（中度拥塞）、$3\text{GB/s}$（严重拥塞）；
  2. 发送 $8\text{K}$ 上下文请求，对比盲目拉取与开启矩阵动态裁决后的端到端 TTFT。
* **验收 PASS 红线**：
  * 选路读取与闭式评估耗时 **$\text{P99} < 2.0\mu\text{s}$**；
  * 在 $3\text{GB/s}$ 恶劣拥塞下 $100\%$ 触发契约熔断回退本地重算，**端到端 TTFT 降低 $\ge 61.8\%$（$629.9\text{ms} \to 240.7\text{ms}$），负收益退化率为 $0\%$**；
  * 在 $12\text{GB/s}$ 中度拥塞下自动解算 $K^*$ 混合切分，端到端 TTFT 较纯拉取和纯重算均降低 **$15\% \sim 25\%$**。

---

### 4.5 场景 S5：【100 Gbps 后台换出 + SIMD 压缩混压】双时间尺度硬件 QoS 护航实测

* **测试目的**：验证优化点 C5（$200\mu\text{s}$ $256\text{KB}/2048\text{KB}$ 自适应微切片 + $20\text{ms}$ MPAM `PARTID_BG` 统一限流）能否在 $100\text{Gbps}$ 后台满载换出与 YY SIMD 压缩并发冲击下，死守前台 Decode **$\text{TPOT}$ 干扰率 $< 3\%$（且实际压入 $< 2.0\%$）**，同时在前台空闲时不影响 CY 单通道顶速。
* **执行步骤**：
  1. **前台空闲单通道顶速校验**：关闭前台 Decode，启动 CY 单通道大块传输，验证 `fg_decode_active_bitmap == 0` 时自动采用 $2048\text{KB}$ 大包，单通道平顶带宽维持 **$> 38\text{GB/s}$**；
  2. **100Gbps 满载混压测试**：启动前台在线 Decode 稳态流（基线 $\text{TPOT} = 18.20\text{ms}$），同时在同 SCCL 启动后台 $100\text{Gbps}$ KVC 换出流与 YY NEON 位平面压缩线程；
  3. 对比“无 QoS 控制”、“仅限制网络线程但漏管压缩线程”、“V2.1 全线程 `PARTID_BG` MPAM + $256\text{KB}$ 微切片”三种模式下的前台 TPOT、后台排出速率与系统有效达标 QPS。
* **验收 PASS 红线**：
  * $100\text{Gbps}$ 混压下，前台在线 Decode **$\text{TPOT}$ 干扰率从 $+16.98\%$（$21.29\text{ms}$）压降至 $< 2.0\%$（绝对值 $\le 18.56\text{ms}$，净增 $\le 0.36\text{ms}$，严守 $<3\%$ 红线）**；
  * 后台平均有效排出带宽维持 **$\ge 75\text{Gbps}$**（较无控越线阈值 $17.65\text{Gbps}$ 提升 $4.25\times$）；
  * 混压工况下系统 **有效达标吞吐（Goodput / 有效 QPS）提升 $\ge 15\%$**。

---

## 五、 四周（W1~W4）攻坚里程碑与跨模块联调计划

| 周次与时间窗口 | 核心攻坚目标 | 关键修改文件锚点 | 跨模块协同与可验证准出指标 |
| :--- | :--- | :--- | :--- |
| **W1**<br>*(11.02 ~ 11.08)* | **消灭 2 个 `3ms` 慢指标 & 接入 IO Die/RC 探针** | `方案设计/调度面优化/testbed/src/telemetry_daemon.cpp`<br>`mooncake-store/include/metadata.h` | 冻结对齐后的 $64\text{B}$ `CapabilityMatrixEntry` 头文件；完成 `perf_event mmap` + `/dev/mem` MMIO 兜底直读与网卡 DMA Auto-Dump；**通过场景 S1 测试（本地采集 $< 2\mu\text{s}$）**。 |
| **W2**<br>*(11.09 ~ 11.15)* | **`<500µs` 单边内联上报 & 双桶 Epoch 预扣闭环** | `mooncake-store/src/master_service.cpp`<br>`方案设计/调度面优化/testbed/src/telemetry_daemon.cpp` | 打通 $64\text{B}$ 单边 `RDMA_WRITE_INLINE` 500µs 周期上报；协同 WXH 联调 `EpochAwareReservationSlab` 双桶预扣与三段式木桶选路；**通过场景 S2 & S3 测试（上报 $<500\mu\text{s}$，预测误差 $<8\%$）**。 |
| **W3**<br>*(11.16 ~ 11.22)* | **双时间尺度硬件 QoS 闭环 & 100G 混压调优** | `方案设计/调度面优化/testbed/02_hardware_isolation.sh`<br>`mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp`<br>`mooncake-transfer-engine/include/qos/hierarchical_qos_controller.h` | 协同 CY 实装 $256\text{KB}/2048\text{KB}$ 自适应微切片；协同 YY/LJQ 将压缩与刷盘线程全部纳入 MPAM `PARTID_BG` 20ms 动态限流；**通过场景 S4 & S5 实机混压测试（TPOT 干扰 $< 2.0\%$）**。 |
| **W4**<br>*(11.23 ~ 11.29)* | **配合 `W3-T06` 八级消融 Level 5/6 复验与归档** | `方案设计/调度面优化/testbed/03_run_benchmark.sh` | 配合 WXH 完成 `W3-T06` 全链路八级递进消融实验（重点支撑 Level 5 混压 QoS 消融与 Level 6 选路防踩踏消融），归档 E3 门禁验收报告与 MPAM/QoS 生产调优指南。 |
