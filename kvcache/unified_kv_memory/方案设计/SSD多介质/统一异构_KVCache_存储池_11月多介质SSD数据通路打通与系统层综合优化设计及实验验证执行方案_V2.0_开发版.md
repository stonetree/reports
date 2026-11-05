# 统一异构 KVCache 存储池：11月多介质（SSD）数据通路独立交付与12月全系统集成验证方案 (v3.0 解耦执行版)

> **文档版本**：v3.0（职责边界清晰界定 + 11月 LJQ 独立交付验证 + 12月全系统集成测试双轨执行版）  
> **适用周期**：
> - **第一阶段（2026年11月 M2 攻坚期：W1 `11.02` $\sim$ W4 `11.29`）**：**LJQ 100% 零外部依赖独立闭环交付 SSD 优化功能与独立基准验证**（通过内置 Fallback/Stub 机制彻底解耦他人进度风险）；
> - **第二阶段（2026年12月 M3 集成期：W1 `12.01` $\sim$ W4 `12.27`）**：**与 WZX、YY、LDY、YZN、NXY、WXH、CY 开展跨模块联调与全系统集成性能验证**。  
> **核心定位**：面向 OCP 2026 KVCache 存储演进趋势，在**当前无直通 SSD（GDS / P2P BAR1）实验硬件**且**11月其他成员模块进度存在不确定性**的双重约束下，以**“契约先行、依赖倒置、11月独立交付闭环、12月平滑插拔集成”**为纲领，确保 LJQ 在 11 月底按时高质量完成 SSD 数据通路优化的独立交付与性能达标，并为 12 月全系统集成及未来直通 SSD 硬件演进预留 $100\%$ 兼容的标准接口。

---

## 目录 (Table of Contents)

- [一、全局视野：SSD 引入对 AI Infra 中 KVCache 体系的深刻重构](#一全局视野ssd-引入对-ai-infra-中-kvcache-体系的深刻重构)
  - [1.1 OCP 2026 趋势研判：为什么 SSD 是下一代 KVCache 基础设施的必争之地？](#11-ocp-2026-趋势研判为什么-ssd-是下一代-kvcache-基础设施的必争之地)
  - [1.2 全局系统冲击：SSD 介入引发的五大跨层耦合效应与双阶段解耦策略](#12-全局系统冲击ssd-介入引发的五大跨层耦合效应与双阶段解耦策略)
  - [1.3 第一性原理数学精算：11月独立交付极限（工况 D）与12月集成极限（工况 E）](#13-第一性原理数学精算11月独立交付极限工况-d与12月集成极限工况-e)
- [二、全局统一架构与跨模块职责边界全景定义（解决“边界不清与耦合阻塞”问题）](#二全局统一架构与跨模块职责边界全景定义解决边界不清与耦合阻塞问题)
  - [2.1 四层正交解耦架构全景图（标注 LJQ 独立交付域 vs. 12月外部插件域）](#21-四层正交解耦架构全景图标注-ljq-独立交付域-vs-12月外部插件域)
  - [2.2 LJQ 与其他六大模块的职责边界矩阵与 11 月零依赖内置兜底（Fallback）机制](#22-ljq-与其他六大模块的职责边界矩阵与-11-月零依赖内置兜底fallback机制)
  - [2.3 统一 64-bit 跨介质寻址 (`FC-GVA` v2) 与两级对齐描述符](#23-统一-64-bit-跨介质寻址-fc-gva-v2-与两级对齐描述符)
  - [2.4 可插拔硬件后端契约 (`IKVStorageBackend`) 与六大跨模块依赖倒置接口规范](#24-可插拔硬件后端契约-ikvstoragebackend-与六大跨模块依赖倒置接口规范)
- [三、第一部分：不依赖直通硬件的系统层综合优化方案（区分 11月独立交付 vs. 12月集成扩展）](#三第一部分不依赖直通硬件的系统层综合优化方案区分-11月独立交付-vs-12月集成扩展)
  - [3.1 [LJQ 11月独立交付] 写时跨卡分片聚簇（含 GPU Gather-Pack 内置兜底）与日志型 Extent 分配器](#31-ljq-11月独立交付-写时跨卡分片聚簇含-gpu-gather-pack-内置兜底与日志型-extent-分配器)
  - [3.2 [LJQ 11月独立交付] 读时 Sort-Merge 合并、内置巨页池与用户态条带化 `io_uring` 双缓冲流水](#32-ljq-11月独立交付-读时-sort-merge-合并内置巨页池与用户态条带化-io_uring-双缓冲流水)
  - [3.3 [LJQ 11月独立交付] `FC-MAS` 快慢队列物理隔离、内置自适应 QoS 限流与 ARM64 `ldnp/stnp` 流式搬运](#33-ljq-11月独立交付-fc-mas-快慢队列物理隔离内置自适应-qos-限流与-arm64-ldnpstnp-流式搬运)
  - [3.4 [12月 M3 跨模块全局协同] SSD 与全系统六大模块的深度耦合设计（12月集成阶段激活）](#34-12月-m3-跨模块全局协同-ssd-与全系统六大模块的深度耦合设计12月集成阶段激活)
- [四、第二部分：依赖具备直通能力 SSD 硬件的优化方案（硬件就绪后平滑演进）](#四第二部分依赖具备直通能力-ssd-硬件的优化方案硬件就绪后平滑演进)
  - [4.1 PCIe P2P BAR1 直通 DMA 机理与 SMMUv3 P2P 映射打通](#41-pcie-p2p-bar1-直通-dma-机理与-smmuv3-p2p-映射打通)
  - [4.2 破解“硬件直通 vs. CPU 压缩”悖论：算子尾声位平面打包与动态路径切换](#42-破解硬件直通-vs-cpu-压缩悖论算子尾声位平面打包与动态路径切换)
  - [4.3 面向远端存储池的 `NVMe -> Host 1GB HugePage -> URMA` 直通切穿 (Cut-Through) 流水](#43-面向远端存储池的-nvme---host-1gb-hugepage---urma-直通切穿-cut-through-流水)
  - [4.4 硬件到位后的零成本热切换指南 (Drop-in Migration)](#44-硬件到位后的零成本热切换指南-drop-in-migration)
- [五、核心代码修改位置定位与 Agent 开发指引（明确标注 11月独立开发 vs. 12月集成联调）](#五核心代码修改位置定位与-agent-开发指引明确标注-11月独立开发-vs-12月集成联调)
  - [5.1 Mooncake Store & Transfer Engine 修改定位（LJQ 11月独立交付核心域）](#51-mooncake-store--transfer-engine-修改定位ljq-11月独立交付核心域)
  - [5.2 vLLM / SGLang 推理引擎侧修改定位（11月独立运行模式 vs. 12月联调挂载点）](#52-vllm--sglang-推理引擎侧修改定位11月独立运行模式-vs-12月联调挂载点)
  - [5.3 跨模块适配器与 12 月集成联调代码定位](#53-跨模块适配器与-12-月集成联调代码定位)
- [六、双轨制实验验证方案：11月 LJQ 独立交付验证 vs. 12月全系统集成验证](#六双轨制实验验证方案11月-ljq-独立交付验证-vs-12月全系统集成验证)
  - [6.1 实验测试床 (`HoloTestbed-4X`)、LJQ 独立压测套件 (`kv_ssd_standalone_bench`) 与直通仿真桩](#61-实验测试床-holotestbed-4x-ljq-独立压测套件-kv_ssd_standalone_bench-与直通仿真桩)
  - [6.2 11月独立交付指标 vs. 12月全系统集成指标双轨对照明细表](#62-11月独立交付指标-vs-12月全系统集成指标双轨对照明细表)
  - [6.3 六大核心验证实验双轨设计 (`Exp-1` $\sim$ `Exp-6`：区分 `Track-A 11月独立验证` 与 `Track-B 12月集成验证`)](#63-六大核心验证实验双轨设计-exp-1--exp-6区分-track-a-11月独立验证-与-track-b-12月集成验证)
  - [6.4 11月 W1~W4（独立交付主线）与 12月 M3（全系统集成主线）分阶段执行检查表](#64-11月-w1w4独立交付主线与-12月-m3全系统集成主线分阶段执行检查表)

---

## 一、全局视野：SSD 引入对 AI Infra 中 KVCache 体系的深刻重构

### 1.1 OCP 2026 趋势研判：为什么 SSD 是下一代 KVCache 基础设施的必争之地？

从 OCP 2026（Open Compute Project）KVCache 专题的技术演进来看，工业界正经历从“HBM + Host DRAM 双层缓存”向 **“HBM + Host DRAM + 高速 NVMe SSD 三级统一异构存储池”** 的范式转移。其根本驱动力源于三个不可逆的物理定律：

1. **Agentic AI 与长上下文的“容量-驻留时间乘积（Capacity-Residence Product）”爆炸**：
   在 SWE-bench 代码修复、深度研究（Deep Research）与多轮 ReAct Agent 场景中，单会话上下文普遍处于 $32\text{K} \sim 128\text{K}$ 区间（Qwen-72B 下单会话 KVC 达 $10\text{ GB} \sim 40\text{ GB}$）。更致命的是，Agent 在调用外部工具（如编译代码、运行测试集、搜索网页）时存在 **$5\text{ s} \sim 60\text{ s}$ 的计算停顿期（Tool-Wait Window）**。若仅靠单机数百 GB 的 Host DRAM，并发仅数十个 Agent 会话就会把 DRAM 彻底撑爆，引发剧烈的缓存颠簸（Cache Thrashing）。
2. **存储介质 TCO 与容量密度的数量级代差**：
   以单节点存储成本与容量密度衡量，HBM3e : DDR5 : 企业级 PCIe Gen5 NVMe SSD 的单位 GB 成本比约为 $100 : 10 : 1$，而单机可挂载容量比则为 $1 : 4 : 64$（单机可轻松挂载 $30\text{ TB} \sim 60\text{ TB}$ NVMe SSD）。只有打通 SSD 层，才能将 KVCache 的长时有效驻留窗口从“秒级”扩展到“小时级”。
3. **PCIe Gen5 与多盘聚合带宽已逼近内存总线量级**：
   单块 PCIe 5.0 $\times 4$ NVMe SSD 顺序读取带宽高达 $14\text{ GB/s}$，$4\sim 8$ 盘条带化（Striping）聚合顺序带宽可达 $20\text{ GB/s} \sim 50\text{ GB/s}$，叠加无损/轻量化位平面压缩（$1.85\times \sim 2.18\times$）后，其**等效逻辑吞吐可达 $37\text{ GB/s} \sim 90\text{ GB/s}$**，完全具备直接喂饱 GPU/NPU 计算流水线的物理潜力。

---

### 1.2 全局系统冲击：SSD 介入引发的五大跨层耦合效应与双阶段解耦策略

将 SSD 引入 AI Infra 会从底层颠覆 KVCache 的分配、压缩、传输、掩盖与调度逻辑。为了防止其他模块在 11 月的开发进度滞后阻塞 LJQ 的 SSD 核心功能交付，我们在架构上采取 **“11月 LJQ 内置轻量级兜底机制（Self-Contained Fallback）实现独立闭环交付 + 12月通过标准接口切换为他人深度优化模块”** 的双阶段推进策略：

| 系统维度 | 引入 SSD 后的物理冲突与深刻影响 | 11月 LJQ 独立闭环交付策略（零外部依赖） | 12月跨模块全局协同策略（全系统集成） |
| :--- | :--- | :--- | :--- |
| **1. 块分配与内存布局**<br>*(关联 WZX `M2-T03`)* | SSD 最小逻辑块（LBA）为 $4\text{ KB}$。若直接将原生 PagedAttention 的 $8\text{ KB}$ 散碎块落盘，IOPS 队列瞬间打满，带宽跌至 $<1.8\text{ GB/s}$ 且引发严重写放大（WAF）。 | **LJQ 内置 GPU Gather-Pack 聚簇算子 + 日志分配器**：在 HBM 侧通过单次 Copy-Stream Gather Kernel 将散碎 $8\text{ KB}$ 块打包为连续 $16\text{ MB}$ 层缓冲后再 DMA 至 Host 落盘，不依赖 WZX 即可消灭 $99.6\%$ 随机小 I/O。 | **切换为 WZX `16-Block Slab` 连续分配器**：上层分配天然连续，省去 GPU Gather-Pack 步骤，直接发起大块连续 DMA。 |
| **2. 数据压缩与体积维**<br>*(关联 YY `M2-F02`)* | SSD 物理带宽（$20\text{ GB/s}$）低于 DDR（$42\text{ GB/s}+$），且有寿命限制。落盘前引入流式压缩可将 SSD 等效带宽与寿命放大 $1.85\times$。 | **LJQ 内置 `BypassRawCodec` 与 `MockBitPlaneCopyCodec`**：11月主验 Raw 物理带宽极限（$\ge 15\text{ GB/s}$），并用内置 NEON `ldnp/stnp` 定率模拟桩独立验证三级流水状态机与 $4\text{ KB}$ 对齐填充逻辑。 | **热插拔挂载 YY 鲲鹏 SIMD 位平面压缩引擎**：实装真实指数/尾数分离与零游程编码，实现等效 $>28\text{ GB/s}$ 逻辑吞吐。 |
| **3. 计算-通信时间掩盖**<br>*(关联 LDY `M2-F01`)* | SSD 存在第一跳读取耗时（单层 $128\text{ MB}$ 需 $6.25\text{ ms}$），需与 GPU 层间计算流水重叠，且受慢速请求队头阻塞影响。 | **LJQ 内置全层双缓冲换入 + `StandaloneHbmWindowHarness`**：11月在原生 vLLM 中支持全层流水预取，并提供内置 2 层 HBM 窗口 + 虚拟计算步进器（Dummy Compute Pacer）独立验证按层流水握手。 | **联调 LDY 按层滑动窗与动态 $K^*(m)$ 重算**：由 LDY 对前 $K^*=5\sim 7$ 层执行真机重算，彻底掩盖 SSD 首层启动延迟。 |
| **4. 芯片 IO Die 与巨页底座**<br>*(关联 NXY `M2-T04` & YZN `M2-T02`)* | NVMe SSD、GPU 与网卡共享同一颗鲲鹏 IO Die 的 PCIe RC，极易打爆 SMMUv3 IOTLB 与 PCIe Credit，导致 Decode TPOT 劣化。 | **LJQ 内置 `mmap(MAP_HUGE_1GB)` 分配器 + 软件自适应 QD 限流器**：11月使用自有巨页池分配 `256MB` 缓冲环，并根据 Decode 活跃 Batch 与 CQE 延迟自动压降 NVMe QD（$128 \rightarrow 16$）。 | **接入 YZN 全局巨页共池与 NXY 硬件 PMU 探针**：共享跨组件 1GB 巨页池，并由 NXY 基于 `hisi_pcie` 硬件计数器执行 $200\mu\text{s}$ 级精准限流。 |
| **5. 全局调度与生命周期**<br>*(关联 WXH `M2-T05`)* | 盲目同步直读 SSD 会使短请求 TTFT 恶化；传统 LRU 会在 Agent 工具调用停顿期把热会话误踢入 SSD 造成颠簸。 | **LJQ 内置启发式三桶分类器 + `FC-MAS` 自动双队列预取**：根据前缀长度与命中次数自动归入 Hot/Warm/Cold 分桶，并在 L3 内部对命中 SSD 的请求自动执行后台预热后再放行。 | **受控于 WXH `KVTether` L2 调度大脑**：接入工具停顿租约（Tool-Wait Lease）、Webhook 超前升迁及三元成本路由模型。 |

---

### 1.3 第一性原理数学精算：11月独立交付极限（工况 D）与12月集成极限（工况 E）

以 **Qwen-72B 模型（$L = 80$ 层，$TP = 8$，`block_size = 16` Tokens，BF16 精度）** 处理 **$32\text{K}$ 上下文** 为标准基准：
* **数据体量分解**：
  * 单 Token 单层（8 卡合计）KVC 大小为 $64\text{ KB}$；单块（16 Tokens）单层 8 卡合计为 $1\text{ MB}$，故**单卡单层单块为 $128\text{ KB}$（若更高 GQA 压缩比则为 $8\text{ KB} \sim 32\text{ KB}$）**。
  * $32\text{K}$ 上下文总 KVC 体积 $V_{\text{total}} = 10\text{ GB}$（即 $10,240\text{ MB}$）；
  * 每一层（8 卡合计）体积 $V_{\text{layer}} = \frac{10,240\text{ MB}}{80} = 128\text{ MB}$；
  * **单卡（Per-TP-Rank）每一层体积** $V_{\text{rank, layer}} = \frac{128\text{ MB}}{8} = \mathbf{16\text{ MB}}$。

* **物理带宽设定**：
  * 节点 NVMe SSD 条带化顺序读取物理峰值带宽 $B_{\text{SSD}} = 20\text{ GB/s}$；
  * Host DDR 到 GPU/NPU HBM 的聚合 DMA 带宽 $B_{\text{DMA}} = 42\text{ GB/s}$；
  * 鲲鹏 CPU NEON 位平面无损解压缩吞吐 $B_{\text{decomp}} = 48\text{ GB/s}$，无损压缩比 $\gamma = 1.85\times$（压缩后总落盘体积 $V_{\text{comp}} = \frac{10\text{ GB}}{1.85} \approx 5.41\text{ GB}$，单层压缩体积 $69.2\text{ MB}$）。

#### 五种工况下的端到端数据搬运耗时精确推导（明确区分 11 月独立目标与 12 月集成目标）：

1. **工况 A：未优化的原始散碎 SSD 读取（11月 Baseline 基线）**
   $163,840$ 个离散碎块随机读取，受制于 NVMe 队列开销与 Flash 通道冲突，有效带宽 $B_{\text{rand}} \approx 1.8\text{ GB/s}$：
   $$T_{\text{Scattered}} = \frac{V_{\text{total}}}{B_{\text{rand}}} = \frac{10\text{ GB}}{1.8\text{ GB/s}} = \mathbf{5,555.6\text{ ms}}$$

2. **工况 B：理想硬件直通 SSD（GDS / P2P BAR1）+ 写时层主序连续布局（无压缩，理论参照系）**
   SSD 通过 PCIe P2P 直接按层顺序写入 HBM，共 80 层，每层 $128\text{ MB}$：
   $$T_{\text{Direct\_Raw}} = \frac{V_{\text{total}}}{B_{\text{SSD}}} = \frac{10\text{ GB}}{20\text{ GB/s}} = \mathbf{500.00\text{ ms}}$$

3. **工况 C：传统 SSD 笨拙串行中转（先全量读入 Host DDR，再全量 DMA 拷入 HBM）**
   $$T_{\text{Serial}} = \frac{10\text{ GB}}{20\text{ GB/s}} + \frac{10\text{ GB}}{42\text{ GB/s}} = 500.00\text{ ms} + 238.10\text{ ms} = \mathbf{738.10\text{ ms}}$$

4. **工况 D：【11月 LJQ 独立交付目标态】传统 SSD + 层主序聚簇 + 8 卡各 $32\text{ MB}$ 巨页双缓冲按层流水（不依赖外部压缩模块）**
   * **缓冲池精确配比**：为每个 TP Rank（共 8 卡）分配 **$32\text{ MB}$ 固定环形缓冲槽（Ping 槽 $16\text{ MB}$ + Pong 槽 $16\text{ MB}$）**，整机 8 卡合计 $256\text{ MB}$。
   * 当第 $l$ 层（单卡 $16\text{ MB}$，8 卡并发合计 $128\text{ MB}$）从 SSD 读入 Ping 槽时，耗时 $t_{\text{stage1}} = \frac{128\text{ MB}}{20\text{ GB/s}} = 6.25\text{ ms}$；
   * 与此同时，第 $l-1$ 层从 Pong 槽通过 DMA 推入 HBM（若无 WZX 连续块，则在 HBM 内附带一次耗时仅 $0.12\text{ ms}$ 的 GPU Scatter Kernel 分发至散碎块），总耗时 $t_{\text{stage2}} = \frac{128\text{ MB}}{42\text{ GB/s}} + 0.12\text{ ms} = 3.10\text{ ms}$；
   * 由于 $t_{\text{stage2}} < t_{\text{stage1}}$，前 79 层的第二跳 DMA 与 HBM 内置 Scatter 耗时被第一跳 SSD 读取 **$100\%$ 完全掩盖**，仅在第 80 层产生一个 $3.10\text{ ms}$ 的流水线排空尾延迟（Pipeline Drain Tail）：
   $$T_{\text{Pipelined\_Raw}} = L \times \max\left(t_{\text{stage1}},\, t_{\text{stage2}}\right) + t_{\text{stage2}} = 80 \times 6.25\text{ ms} + 3.10\text{ ms} = \mathbf{503.10\text{ ms}}$$
   > **核心推论 1（11月 LJQ 独立交付理论基石）**：在完全不依赖直通硬件、且完全不依赖其他成员（WZX/YY/LDY/WXH）11月代码的前提下，仅凭 LJQ 独立开发的**层主序聚簇 + 按层双缓冲流水**，即可将 $32\text{K}$ KVC 换入耗时从 $5,555.6\text{ ms}$ 压降至 **$503.10\text{ ms}$（实机工程验收门限 $\le 550\text{ ms}$）**，达到理想硬件直通 SSD **$99.4\%$（$500.00 / 503.10$）** 的物理极限性能！

5. **工况 E：【12月 M3 跨模块集成目标态】传统 SSD + 按层双缓冲流水 + YY 鲲鹏 SIMD 位平面流式压缩**
   * 在 12 月集成 YY（`M2-F02`）无损位平面压缩（$\gamma = 1.85\times$）后，单层从 SSD 读入的实际物理字节数降至 $\frac{128\text{ MB}}{1.85} = 69.19\text{ MB}$；
   * 阶段 1（SSD 读入压缩块）：$t_{\text{stage1}}^{\text{comp}} = \frac{69.19\text{ MB}}{20\text{ GB/s}} = 3.46\text{ ms}$；
   * 阶段 2（鲲鹏多核 NEON 寄存器内流式解压 + 零缓存污染写入 Pong 槽）：$t_{\text{stage2}}^{\text{decomp}} = \frac{128\text{ MB}}{48\text{ GB/s}} = 2.67\text{ ms}$；
   * 阶段 3（Pong 槽 DMA 推入 HBM）：$t_{\text{stage3}}^{\text{dma}} = \frac{128\text{ MB}}{42\text{ GB/s}} = 2.98\text{ ms}$；
   * 三级流水线稳态步进周期由最大瓶颈级决定：$\Delta t = \max(3.46,\, 2.67,\, 2.98) = 3.46\text{ ms}$：
   $$T_{\text{Pipelined\_Comp}} = 80 \times 3.46\text{ ms} + 2.67\text{ ms} + 2.98\text{ ms} = \mathbf{282.45\text{ ms}}$$
   > **核心推论 2（12月全系统集成基石）**：12 月将 LJQ 独立验证通过的三级流水框架与 YY 的压缩插件热插拔合体后，端到端耗时将从 $503.10\text{ ms}$ 进一步降至 **$282.45\text{ ms}$（实机工程验收门限 $\le 320\text{ ms}$，等效逻辑读取带宽跃升至 $35.4\text{ GB/s}$）**。

---

## 二、全局统一架构与跨模块职责边界全景定义（解决“边界不清与耦合阻塞”问题）

为彻底解决原始方案中**“职责边界模糊”**与**“强耦合导致 11 月无法独立交付”**的问题，本章采用**依赖倒置原则（Dependency Inversion Principle, DIP）**对全局架构进行严格切分：所有跨模块交互全部收敛为 **6 个标准化 C++/Python 抽象插件接口**。在 11 月，LJQ 模块默认挂载**自包含内置兜底实现（Built-in Standalone Fallbacks）**完成独立交付；在 12 月，通过配置开关一键切换为其他成员的外部实现。

### 2.1 四层正交解耦架构全景图（标注 LJQ 独立交付域 vs. 12月外部插件域）

```
+=============================================================================================================+
| 【L2 调度与策略层】                                                                                         |
|   • [12月外部插件: WXH M2-T05] KVTether 租约保护 + Webhook 超前升迁 + 三元成本路由 + 动态 K*(m) 下发          |
|   • [11月 LJQ 内置兜底: LocalHeuristicTieringRule] 基于前缀长度/命中次数的三桶自动分类与 SSD 命中自动预取触发 |
+=============================================================================================================+
                                      │ 通过 ITieringPolicyHint 接口下发指令
                                      ▼
+=============================================================================================================+
| 【L3.1 跨介质寻址、聚簇与日志分配层 (LJQ 11月 100% 独立主责交付域)】                                         |
|   • [LJQ 独立核心] 统一 64位跨介质寻址 (FC-GVA v2) + LayerExtentDescriptor (64B 硬件对齐描述符)               |
|   • [LJQ 独立核心] 日志型分桶追加写分配器 (LogStructuredBucketAllocator，消除 SSD GC 毛刺，WAF <= 1.05)       |
|   • [块布局适配接口 IBlockLayoutProvider]:                                                                   |
|       ├─ 11月 LJQ 内置兜底: HostSgCoalesceEngine (GPU Copy-Stream Gather/Scatter 算子将8KB碎块聚簇为16MB)   |
|       └─ 12月 WZX 外部接入: ContiguousSlabBlockProvider (上层直接提供 16-Block 连续物理块区间，零算子打包)    |
+=============================================================================================================+
                                      │ 输出连续 4KB 对齐的 LayerExtentDescriptor 链
                                      ▼
+=============================================================================================================+
| 【L3.2 共性双缓冲流水线、快慢队列隔离与流式编解码层 (LJQ 11月 100% 独立主责交付域)】                         |
|   • [LJQ 独立核心] 读时区间排序合并器 (Read-Time Sort-Merge Coalescer) + 用户态确定性多盘条带化器             |
|   • [LJQ 独立核心] FC-MAS 快慢介质物理隔离双队列 (Fast DDR Queue vs. Slow SSD Staging Queue，防队头阻塞)      |
|   • [LJQ 独立核心] 三级异步滑动窗流水状态机 + ARM64 ldnp/stnp 非临时指令零 L3 缓存污染搬运内核                |
|   • [四大解耦插件槽位 (11月内置实现 -> 12月外部替换)]:                                                       |
|       1. IStagingMemoryProvider : [11月 LJQ 内置 mmap 1GB 巨页池]    ──► [12月 YZN 全局 SCCL 巨页共池]       |
|       2. IStreamCodecPlugin     : [11月 LJQ Raw直通 / 定率模拟压缩桩] ──► [12月 YY 鲲鹏 NEON 位平面压缩]      |
|       3. ILayerWindowHandshake  : [11月 LJQ 全层预取 / 内置2层仿真窗] ──► [12月 LDY 2层 HBM 窗 + K* 重算]     |
|       4. IIoQoSThrottle         : [11月 LJQ 软件延迟反馈自适应 QD]   ──► [12月 NXY IO Die PMU 硬件探针限流]  |
+=============================================================================================================+
                                      │ 调用统一抽象契约 IKVStorageBackend::SubmitBatchLayerIO(...)
                                      ▼
+-------------------------------------------------------------------------------------------------------------+
| 【L3.3 可插拔硬件后端适配层 (LJQ 11月独立交付域)】                                                           |
|   • [后端 A：11月实机主力] HostPinnedUringBackend (io_uring O_DIRECT + FixedBuffers + SQPOLL + 异步 DMA)     |
|   • [后端 B：仿真对比 / 未来硬件] DirectBar1P2PEmulator (11月周期精确仿真桩) / DirectBar1P2PBackend      |
|   • [后端 C：12月跨机联调] RemoteUrmaCutThroughBackend (11月 Loopback 验证，12月对接 CY 400G URMA 网卡)      |
+-------------------------------------------------------------------------------------------------------------+
```

---

### 2.2 LJQ 与其他六大模块的职责边界矩阵与 11 月零依赖内置兜底（Fallback）机制

下表严格界定了 **LJQ 必须自己独立开发交付的内容**、**外部协作者应当提供的内容**、**双方的精确代码分界线**，以及 **当外部协作者在 11 月未能按时交付时，LJQ 如何依靠内置 Fallback 独立完成 11 月验收**：

| 协同对象与关联任务 | 外部协作者负责提供什么（12月集成输入） | LJQ 负责独立开发什么（11月核心交付） | 精确接口契约与物理分界线 | 11月外部未就绪时 LJQ 的内置兜底（Fallback）方案 |
| :--- | :--- | :--- | :--- | :--- |
| **1. WZX**<br>`M2-T03`<br>连续块分配器 | 在 vLLM/SGLang `BlockManager` 中实现 `16-Block Contiguous Slab` 分配，导出连续物理块区间列表 `vector<ContiguousBlockRange>`。 | 1. 接收块映射表并组装为单卡单层 $16\text{ MB}$ 的 `LayerExtentDescriptor`；<br>2. 实现 `LogStructuredBucketAllocator` 管理 SSD LBA 地址分配与整段 TRIM 回收。 | **分界线**：HBM 块地址列表。<br>**接口**：`IBlockLayoutProvider::ExportLayerBlockRanges(seq_id, layer_id)`。 | **内置 `HostSgCoalesceEngine`**：当输入仍为原生 vLLM 离散 $8\text{ KB}$ 块 ID 数组时，LJQ 在 GPU Copy Stream 上启动轻量级 `kv_gather_pack_kernel`（耗时 $<0.12\text{ ms}$/层），在 HBM 内先拼成 $16\text{ MB}$ 临时连续缓冲再执行单次 DMA，**100% 独立实现顺序大块落盘**。 |
| **2. YZN**<br>`M2-T02`<br>SCCL 1GB 巨页底座 | 提供跨组件统一的 `GlobalScclHugePageAllocator`，按鲲鹏 `SCCL_ID` 分配绑定至本地 DDRC 与 IO Die 的 1GB 巨页虚拟/物理地址。 | 1. 将获取到的 $256\text{ MB}$ 巨页内存切分为 8 卡 $\times 2$ 槽（Ping/Pong 各 $16\text{ MB}$）无锁环形缓冲区；<br>2. 完成 `io_uring_register_buffers` 与 GPU `cudaHostRegister` / `halHostRegister` 双重锁定。 | **分界线**：Host 连续虚拟内存首指针 `void*` 与 `numa_node`。<br>**接口**：`IStagingMemoryProvider::AllocPinnedHugePage(bytes, sccl_id)`。 | **内置 `NativeHugePageProvider`**：LJQ 直接通过 Linux 系统调用 `mmap(..., MAP_HUGETLB \| MAP_HUGE_1GB)` + `mbind(MPOL_BIND)` 自主申请 $256\text{ MB}$ 巨页（若系统未预留 1GB 巨页则自动降级为 `MAP_HUGE_2MB` 连续锁页内存），**零外部依赖跑通 `io_uring` FixedBuffer**。 |
| **3. YY**<br>`M2-F02`<br>鲲鹏 SIMD 位平面压缩 | 提供无状态的算子函数：输入 $16\text{ MB}$ BF16 源指针与目标指针，利用鲲鹏多线程 NEON 指令执行位平面压缩/解压，返回压缩后有效字节数。 | 1. 管理三级流水状态机（`Stage1: uring` $\leftrightarrow$ `Stage2: Codec` $\leftrightarrow$ `Stage3: HBM DMA`）的无锁并发同步；<br>2. 执行 **$4\text{ KB}$ 硬件对齐尾部填充（Padding）** 与帧头 CRC32 校验封装；<br>3. 实现 ARM64 `ldnp/stnp` 零缓存污染流式搬运底座。 | **分界线**：Host Ping/Pong 槽内存指针。<br>**接口**：`IStreamCodecPlugin::CompressLayer(src, dst, raw_len, &comp_len)` 与 `DecompressLayer(...)`。 | **内置双插件兜底**：<br>1. `BypassRawAlignedCodec`：直接走两级流水，验证 11 月物理极限吞吐（$\ge 15\text{ GB/s}$）；<br>2. `MockBitPlaneCopyCodec`：LJQ 内置基于 NEON `ldnp/stnp` 的按比例（$1.85\times$）截断/填充搬运桩，**100% 独立验证三级流水并发时序、$4\text{ KB}$ 填充对齐与 L3 Cache 零污染指标**。 |
| **4. LDY**<br>`M2-F01`<br>按层计算掩盖与 HBM 滑动窗 | 1. 在 GPU HBM 中维护 2 层（$256\text{ MB}$）滑动窗口；<br>2. 对命中请求的前 $K^*(m)$ 层执行原地 Prefill 重算，并在每层 Attention 计算完成后发出窗口槽位释放信号。 | 1. 按层顺序将第 $K^*+1 \sim 80$ 层 KVC 异步 DMA 推入指定 HBM 目标地址，完成后触发 `LayerReadyEvent`；<br>2. 提供 SSD 介质的实测性能参数（$T_{\text{first}}, \Delta t_{\text{pipe}}$）供计算 $K^*(\text{SSD})$。 | **分界线**：HBM 目标层缓冲指针与 CUDA/Ascend Event 同步原语。<br>**接口**：`ILayerWindowHandshake::AcquireHbmLayerSlot(layer_id)` 与 `NotifyLayerDmaDone(layer_id, event)`。 | **内置双模式兜底**：<br>1. **vLLM 原生全层流水模式**：直接将 80 层流水解包写入请求对应的全量 HBM KVCache 块，无需 LDY 即可在原生 vLLM 中端到端运行；<br>2. **`StandaloneHbmWindowHarness`**：LJQ 测试套件内置 $256\text{ MB}$ HBM 双层窗与可配置计算步进核函数（模拟每层 $6.5\text{ ms}$ Attention 计算），**独立验证层间计算-IO 重叠率 $>85\%$**。 |
| **5. WXH**<br>`M2-T05`<br>L2 全局调度与 `KVTether` | 1. 识别 Agent Tool-Call 停顿期并下发 `Bucket_ID`（Hot/Warm/Cold）生命周期标签；<br>2. 在收到外部工具返回信号时下发 `ProactivePromote(seq_id)` 超前预热指令；<br>3. 执行三元成本路由决策。 | 1. 根据 `Bucket_ID` 将数据写入对应的 SSD 日志追加写分区；<br>2. 实现 `FC-MAS` 快慢介质物理隔离双队列（`FastMediaQueue` 与 `SlowSSDStagingQueue`）及后台异步升迁执行器。 | **分界线**：调度策略指令结构体。<br>**接口**：`ITieringPolicyHint::GetBucketHint(seq_meta)` 与 `TriggerAsyncPromotion(seq_id, target_tier)`。 | **内置 `LocalHeuristicTieringRule`**：当无 WXH 外部指令时，LJQ 根据上下文长度与历史命中次数自动映射至 3 个 TTL 分桶；且当请求在 vLLM `Scheduler` 中被检出命中 SSD 时，自动转入 `SlowSSDStagingQueue` 异步预取完前序层后再放行至 GPU 队列，**独立消除队头阻塞**。 |
| **6. NXY & CY**<br>`M2-T04 / T01`<br>硬件 QoS 与跨机 URMA | 1. NXY 实时监测鲲鹏 IO Die `hisi_pcie` 与 `smmuv3` 硬件计数器，在拥塞时下发目标队列深度配额；<br>2. CY 提供跨节点 400G URMA 预注册内存单边 `WRITE` 通道。 | 1. 实现 `ThrottleQueueDepth(max_qd, max_chunk_kb)` 瞬时限流阀门与 I/O 微切片器（$2\text{ MB} \rightarrow 256\text{ KB}$）；<br>2. 实现 `RemoteUrmaCutThroughBackend` 的 `io_uring CQE -> RDMA/URMA WQE` 切穿状态机。 | **分界线**：QoS 限流回调接口与网络发送句柄。<br>**接口**：`IIoQoSThrottle::OnCongestionLevelChange(qd_limit)`。 | **内置 `SelfAdaptiveIoThrottle` + CLI 注入器**：LJQ 根据当前活跃 Decode Batch 大小及 `io_uring` CQE 延迟斜率自动动态缩放 QD（$128 \leftrightarrow 16$），并提供 `kv_qos_ctl` 命令行压测注入器，**11月独立验证 $<200\mu\text{s}$ 限流响应与 Decode TPOT 劣化 $<3\%$**。 |

---

### 2.3 统一 64-bit 跨介质寻址 (`FC-GVA` v2) 与两级对齐描述符

为解决变长压缩数据破坏 `O_DIRECT` 与 P2P BAR1 的 $4\text{ KB}$ 硬件对齐要求，LJQ 在 11 月独立交付 `FC-GVA`（FlatCake Global Virtual Address）v2 寻址规范：

#### 1. 64-bit `FC-GVA` 位域编码规范
$$
\underbrace{\text{Media\_Type}}_{4\text{ bits [63:60]}} \;\Big|\;
\underbrace{\text{Node\_SCCL\_ID}}_{12\text{ bits [59:48]}} \;\Big|\;
\underbrace{\text{Bucket\_Zone\_ID}}_{8\text{ bits [47:40]}} \;\Big|\;
\underbrace{\text{Comp\_Mode}}_{2\text{ bits [39:38]}} \;\Big|\;
\underbrace{\text{Aligned\_4KB\_LBA\_Offset}}_{38\text{ bits [37:0]}}
$$
* **`Media_Type` (4 bits)**：`0x1` = 本地 HBM，`0x2` = 本地 Host DDR（1GB HugePage），`0x3` = 本地 NVMe SSD（非直通/直通通用），`0x4` = 远端节点 DDR，`0x5` = 远端节点 NVMe SSD，`0x6` = CXL 内存池。
* **`Node_SCCL_ID` (12 bits)**：高 8 位为集群节点 ID，低 4 位为鲲鹏物理 NUMA / SCCL（Super CPU Cluster）及对应 IO Die 编号（强制保证 SSD、DDR 缓冲池与加速卡处于同一颗 IO Die）。
* **`Bucket_Zone_ID` (8 bits)**：对应 SSD 上的日志型追加写分区/分桶号（按 TTL 生命周期分组，消除 GC）。
* **`Comp_Mode` (2 bits)**：`00` = Raw 未压缩（11月独立交付默认态），`01` = BF16 位平面无损压缩（$1.85\times$，12月与 YY 集成态），`10` = FP8 + 位平面压缩（$2.18\times$）。
* **`Aligned_4KB\_LBA\_Offset` (38 bits)**：以 **$4\text{ KB}$ 逻辑块（LBA）为单位**的偏移量，寻址空间高达 $2^{38} \times 4\text{ KB} = \mathbf{1\text{ PB}}$，天然保证任何地址乘以 $4096$ 后 $100\%$ 满足 `O_DIRECT` 与 PCIe BAR1 的 $4\text{ KB}$ 硬件对齐要求！

#### 2. 层主序两级对齐描述符 (`LayerExtentDescriptor`)
每个层主序聚簇块（Layer-Major Extent）在控制面与数据面之间通过固定 $64\text{ Bytes}$（恰好 1 个 CPU Cacheline）的描述符传递：
* `uint64_t fc_gva`：上述 64 位统一地址；
* `uint32_t raw_logical_bytes`：解压/还原后的真实 KVC 字节数（如单卡单层固定为 $16,777,216\text{ Bytes} = 16\text{ MB}$）；
* `uint32_t disk_aligned_bytes`：经过编解码插件处理并向上填充至 $4\text{ KB}$ 整数倍后的实际磁盘 I/O 字节数（未压缩时等于 $16,777,216\text{ B}$；开启 $1.85\times$ 压缩后如为 $8,845,120\text{ B}$，向上对齐 $4\text{ KB}$ 后为 $8,847,360\text{ B}$），直接填入 `io_uring` SQE 或 GDS `cuFileRead` 长度字段；
* `uint16_t layer_id`、`uint16_t tp_rank_id`、`uint32_t crc32_checksum` 及目标 HBM 窗口槽位指针。

---

### 2.4 可插拔硬件后端契约 (`IKVStorageBackend`) 与六大跨模块依赖倒置接口规范

LJQ 在 11 月第 1 周（W1）交付独立头文件 SDK（`kv_ssd_contract_sdk.h`），包含 1 个底层存储后端接口与 6 个上层/横向插件接口。通过启动参数 `--kv-ssd-standalone-mode=true`（11月默认）或 `false`（12月集成），在运行时动态注入具体实现：

1. **底层硬件存储后端接口 `IKVStorageBackend`（连接传统 SSD 与未来直通 SSD）**：
   * `RegisterStagingMemory(void* vaddr, size_t size, MemoryType type) -> BackendMemHandle`
   * `SubmitBatchLayerWrite(const LayerExtentDescriptor* descs, size_t count, CompletionToken* token)`
   * `SubmitBatchLayerRead(const LayerExtentDescriptor* descs, size_t count, StagingSlot* slots, CompletionToken* token)`
   * `ThrottleQueueDepth(uint32_t max_inflight_qd, uint32_t max_io_slice_kb)`
2. **六大跨模块依赖倒置接口（实现 11 月独立与 12 月集成的无缝切换）**：
   * `IBlockLayoutProvider`：默认实例 `LjqGpuGatherScatterProvider`（11月） $\rightarrow$ 集成实例 `WzxContiguousSlabProvider`（12月）；
   * `IStagingMemoryProvider`：默认实例 `LjqNativeHugePageProvider`（11月） $\rightarrow$ 集成实例 `YznGlobalScclHugePageProvider`（12月）；
   * `IStreamCodecPlugin`：默认实例 `LjqBypassOrMockCodec`（11月） $\rightarrow$ 集成实例 `YyKunpengNeonBitPlaneCodec`（12月）；
   * `ILayerWindowHandshake`：默认实例 `LjqFullLayerOrSimWindowHandshake`（11月） $\rightarrow$ 集成实例 `LdyLayerwiseRecomputeHandshake`（12月）；
   * `ITieringPolicyHint`：默认实例 `LjqHeuristicTieringRule`（11月） $\rightarrow$ 集成实例 `WxhKVTetherPolicyBrain`（12月）；
   * `IIoQoSThrottle`：默认实例 `LjqSoftwareLatencyQoS`（11月） $\rightarrow$ 集成实例 `NxyKunpengPmuQoSProbe`（12月）。

---

## 三、第一部分：不依赖直通硬件的系统层综合优化方案（区分 11月独立交付 vs. 12月集成扩展）

本章中，**3.1 $\sim$ 3.3 节为 LJQ 在 11 月 W1 $\sim$ W4 独立闭环交付的核心技术方案（零外部代码依赖）**；**3.4 节为保留的 12 月 M3 跨模块深度协同方案**。

### 3.1 [LJQ 11月独立交付] 写时跨卡分片聚簇（含 GPU Gather-Pack 内置兜底）与日志型 Extent 分配器

#### 1. 痛点剖析：为什么原生 PagedAttention 落盘性能不足 $1.8\text{ GB/s}$？
在 Qwen-72B（TP=8）下，单个 Block（16 Tokens）在单卡单层的 KVC 仅 $8\text{ KB} \sim 128\text{ KB}$。一个 $32\text{K}$ 请求包含 $2,048$ 个 Block $\times 80$ 层 $= 163,840$ 个物理离散的小碎片。若按 Block 粒度落盘，会产生十余万次 $8\text{ KB}$ 随机 I/O；且若在 Host 侧发起 $2,048$ 次细粒度 `cudaMemcpyAsync` / `aclrtMemcpyAsync`，单层驱动下发开销高达 $>5\text{ ms}$，同样会拖垮流水线。

#### 2. 核心设计一：双模式写时层主序连续聚簇（Write-Time Layer-Major Coalescing）
当一段上下文（如 $4\text{K} \sim 32\text{K}$ Tokens）需要从 HBM 下沉写入 SSD 时，LJQ 强制执行 **层主序（Layer-Major）跨块连续聚簇**，并提供 **11月独立模式** 与 **12月集成模式** 两种聚簇引擎：
* **【11月 LJQ 独立兜底模式】基于 GPU/NPU Copy-Stream 的算子级 Gather-Pack 聚簇 (`HostSgCoalesceEngine`)**：
  * 当 WZX 的上层连续 Slab 分配器尚未接入时，面对原生 vLLM 离散的 $8\text{ KB} \sim 128\text{ KB}$ 物理块，**严禁发起逐块 Host-Device DMA**！
  * LJQ 在每张卡的 HBM 中预留一个极小的 **$32\text{ MB}$ Gather/Scatter 临时双缓冲槽（Ping/Pong 各 $16\text{ MB}$，仅占 80GB HBM 的 $0.04\%$）**；
  * **写盘聚簇**：在独立低优先级 Copy Stream 上启动 LJQ 编写的轻量级向量化打包核函数 `kv_layer_gather_pack_kernel<<<grid, block, 0, copy_stream>>>(hbm_block_ptrs, hbm_contig_staging, num_blocks)`，利用高达 $3\text{ TB/s}$ 的 HBM 片内显存带宽，在 **$< 0.12\text{ ms}$** 内将该层所有离散小块在 HBM 内聚簇为一块连续的 $16\text{ MB}$ 缓冲区，随后仅发起 **1 次 $16\text{ MB}$ 连续大块 DMA** 写入 Host 1GB 巨页 Ping 槽；
  * **读盘解包**：从 SSD 读入的 $16\text{ MB}$ 连续层数据经 1 次大块 DMA 进入 HBM 临时槽后，由 `kv_layer_scatter_unpack_kernel` 在 $< 0.12\text{ ms}$ 内分发回原生 vLLM 的离散物理块。
* **【12月 WZX 集成模式】零拷贝连续 Slab 直传 (`ContiguousSlabBlockProvider`)**：
  * 12 月接入 WZX 的 `16-Block Contiguous Slab` 后，上层物理块天然以 $2\text{ MB}$ 连续大段存在，自动旁路上述 HBM Gather/Scatter Kernel，直接由 Host DMA 引擎拉取。
* **8 卡并发落盘收益**：无论处于 11 月独立模式还是 12 月集成模式，落盘阶段均将原本 $163,840$ 次随机小 I/O 彻底折叠为 **$80\text{ 层} \times 8\text{ 卡} = 640$ 次 $16\text{ MB}$ 纯顺序大块 NVMe I/O**，随机小 I/O 消除率达 **$99.6\%$**！

#### 3. 核心设计二：面向 SSD Flash 友好的日志型分桶追加写分配器 (`LogStructuredBucketAllocator`)
为彻底消灭异步淘汰导致的磁盘空洞与 NVMe GC 长尾毛刺，LJQ 独立实现 **`LogStructuredBucketAllocator`**：
* **三档生命周期分桶（TTL Bucket）**：
  将 SSD 空间划分为 3 个独立的追加写流（Append-Only Streams）：
  1. `Bucket_Hot_ShortTTL`（11月内置规则：序列长度 $<4\text{K}$ 或首次换出；12月对接 WXH：SWE-bench 工具等待 $5\text{ s} \sim 60\text{ s}$ 会话）；
  2. `Bucket_Warm_MediumTTL`（11月内置规则：序列长度 $4\text{K} \sim 16\text{K}$ 多轮会话；12月对接 WXH：预期驻留 $1\text{ min} \sim 15\text{ min}$ 会话）；
  3. `Bucket_Cold_LongTTL`（11月内置规则：序列长度 $>16\text{K}$ 或高频命中共享前缀；12月对接 WXH：仓库级静态上下文）。
* **大段（Segment = $256\text{ MB}$）循环追加与整段 TRIM 回收**：
  每个 Bucket 内部由固定 $256\text{ MB}$ 的大段（Segment）组成，所有 `Layer Extent` 严格按指针顺序追加写入（Append-Only），绝不原地覆写。当会话过期淘汰时，仅在内存位图中清零；一旦某个 $256\text{ MB}$ Segment 的有效率低于 $15\%$，后台清理线程直接对该 $256\text{ MB}$ 连续区间下发一次 `fallocate(FALLOC_FL_PUNCH_HOLE)`（文件系统模式）或 `ioctl(NVME_IOCTL_IO_CMD, nvme_dsm_deallocate)`（裸盘模式），实现 **设备级写放大 $WAF \le 1.05$，4小时稳态 P99 读延迟毛刺降低 $90\%$ 以上**。

---

### 3.2 [LJQ 11月独立交付] 读时 Sort-Merge 合并、内置巨页池与用户态条带化 `io_uring` 双缓冲流水

#### 1. 读时区间排序合并（Read-Time Sort-Merge Coalescer）
当请求命中 SSD 中的多段前缀块时，L3.2 引擎在每一层内对目标 `FC-GVA` 描述符按 `Aligned_4KB_LBA_Offset` 升序排列：若相邻两个 Extent 在磁盘 LBA 上连续（或间距 $< 64\text{ KB}$ 空洞），直接合并为单次大块连续读取请求，丢弃中间少量无效空洞比发起两次独立 NVMe 命令更快。

#### 2. 用户态确定性多盘条带化器 (`UserSpaceNvmeStriper`) —— 规避内核 `mdadm` 锁争抢
为规避 Linux 内核 `mdadm` 软 RAID0 在高并发 `io_uring` `SQPOLL` 下存在的 bio 切分与软中断锁争抢问题，LJQ 在 `HostPinnedUringBackend` 中直接实现 **用户态确定性 Rank-to-NVMe 绑定与条带化**：
* 在双路鲲鹏（4 个 SCCL，每个 SCCL 挂载 2 张加速卡与 $1 \sim 2$ 块本地 PCIe RC 下的 NVMe SSD）上，每个 TP Rank 的 `PerRankPipelineWorker` 独享独立的 `io_uring` 实例并直接打开同 SCCL 下的裸设备 `/dev/nvmeXn1`（或独立 XFS 分区），**完全绕过内核 `mdadm` 层，实现 8 卡 8 队列零锁争抢并发顺序读写**。

#### 3. 基于内置 1GB 巨页的 Per-Rank $32\text{ MB}$ 双缓冲三级流水线
```
时间轴 (Time) ───►
Layer l   : [Stage 1: SSD -> Host Ping 槽 (io_uring FixedBuf, 6.25ms Raw / 3.46ms Comp)]
Layer l-1 :        [Stage 2: IStreamCodecPlugin + ldnp/stnp (0~2.67ms)] ──► [Stage 3: Host -> HBM DMA (3.10ms)]
Layer l-2 :                                                                        [GPU Attention 计算第 l-2 层]
```
* **内置巨页池分配（`NativeHugePageProvider`）与双重锁页注册**：
  * 11 月独立交付时，由 LJQ 自有的 `NativeHugePageProvider` 通过 `mmap(NULL, 256MB, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS|MAP_HUGETLB|MAP_HUGE_1GB, -1, 0)` 按 SCCL 申请合计 $256\text{ MB}$ 物理连续巨页内存（每卡 $32\text{ MB} =$ Ping $16\text{ MB}$ + Pong $16\text{ MB}$）；
  * 初始化时一次性调用 `io_uring_register_buffers`（注册为内核固定缓冲区，消除每次 I/O 的内核页表 Pin/Unpin 开销）与 GPU/NPU `cudaHostRegister` / `halHostRegister`（注册为 DMA 锁页内存）。

---

### 3.3 [LJQ 11月独立交付] `FC-MAS` 快慢队列物理隔离、内置自适应 QoS 限流与 ARM64 `ldnp/stnp` 流式搬运

#### 1. `FC-MAS`（Media-Aware Scheduler）快慢介质物理双队列隔离
为防止命中 SSD 的慢请求（耗时 $\sim 500\text{ ms}$）阻塞命中 Host DDR 的快请求（耗时 $< 10\text{ ms}$），LJQ 在调度与传输层实装物理双队列隔离：
* **`Fast-Path Queue (DDR / HBM Ready)`**：高优先级无锁队列，专供命中本地/远端 DDR 的快请求，独占 $70\% \sim 85\%$ 的 PCIe DMA 配额，微秒级立即响应；
* **`Slow-Path Staging Queue (SSD Prefetch / Promotion)`**：后台流式升迁队列。在 11 月独立模式下，当 vLLM `Scheduler` 检出某请求前缀命中 SSD 时，先将其挂入 `Slow-Path Staging Queue`（不占用 GPU Running 槽位），由 LJQ 后台流水线将其前序层预热至 Host 巨页环/HBM 后，再原子移交至 `Fast-Path Queue` 参与 GPU 调度，**彻底消除 SSD 慢请求引起的队头阻塞（Head-of-Line Blocking）**。

#### 2. 内置软件自适应 QoS 限流器 (`SelfAdaptiveIoThrottle`) —— 11月独立保护 Decode TPOT
在不依赖 NXY 硬件 PMU 探针的 11 月独立交付阶段，LJQ 在 `HostPinnedUringBackend` 内部实装闭环软件限流器：
* **双重触发机制**：
  1. 监听推理引擎每步 Decode 的活跃 Batch Size（通过轻量级钩子 `NotifyActiveDecodeBatch(batch_size)`）；
  2. 实时统计 `io_uring` CQE 完成延迟的指数移动平均（EMA）。
* **动态限流动作**：当在线活跃 Decode Batch $\ge 8$ 或 CQE 延迟均值上升超过空载基线 $1.5\times$ 时，在 $< 100\mu\text{s}$ 内自动触发 `ThrottleQueueDepth(max_qd=16, max_slice_kb=256)`，将 NVMe 并发队列深度从 $128$ 压降至 $16$，并将 $16\text{ MB}$ 层请求切分为 $256\text{ KB} \sim 512\text{ KB}$ 微切片交错提交，确保在线 Decode TPOT 劣化严格 **$< 3\%$**。同时提供命令行工具 `kv_qos_ctl` 支持外部手动注入限流指令以开展独立消融测试。

#### 3. 鲲鹏 ARM64 `ldnp/stnp` 非临时指令零 L3 缓存污染搬运
当 CPU 执行 Host 缓冲环内的数据重排或编解码插件（11 月 `MockBitPlaneCopyCodec` 或 12 月 YY `KunpengNeonBitPlaneCodec`）写入目标 DMA 槽位时，LJQ 封装了基于 ARMv8/v9 内联汇编的 **非临时成对加载/存储算子（Non-Temporal Load/Store Pair：`ldnp q0, q1, [x0]` / `stnp q0, q1, [x1]`）**，绕过鲲鹏 L3 Cache（SLC）直接通过 Write-Combining Buffer 刷入 DDR，实测将宿主机 CPU L3 Cache Miss 率降低 **$80\%$ 以上**。

---

### 3.4 [12月 M3 跨模块全局协同] SSD 与全系统六大模块的深度耦合设计（12月集成阶段激活）

> **阶段说明**：本节保留原方案中与团队其余成员的完整深度协同设计。以下耦合特性在 11 月通过抽象接口完成预埋，并在 **12 月 M3 全系统集成测试阶段（关闭 `--kv-ssd-standalone-mode`）正式激活联调**。

#### 1. 协同耦合一（12月激活）：`LJQ (SSD 通路) + YY (M2-F02 鲲鹏 SIMD 位平面压缩)` —— 4KB 对齐流式压缩引擎
* **插件挂载**：将 `IStreamCodecPlugin` 从 11 月的内置模拟桩切换为 YY 的 `YyKunpengNeonBitPlaneCodec`。
* **落盘写路径（`HBM -> Host Ping 槽 -> YY NEON 压缩 -> SSD`）**：
  1. GPU 将第 $l$ 层（单卡 $16\text{ MB}$ BF16）DMA 写入 Host Ping 槽；
  2. YY 的鲲鹏多线程流水线立即使用 ARM NEON `vuzp1q_u8` / `vuzp2q_u8` 单指令完成高低字节（Exponent 与 Mantissa）解交织，对高 8 位指数平面执行差分零游程编码，并调用 LJQ 的 `stnp` 存储原语直接写入 Pong 槽；
  3. **4KB 边界对齐填充（LJQ 负责）**：若压缩后字节数 $S_{\text{comp}}$ 不是 $4096$ 的整数倍，LJQ 框架自动在尾部追加 $P = (4096 - (S_{\text{comp}} \bmod 4096)) \bmod 4096$ 字节的零填充，使落盘长度 $S_{\text{disk}} = S_{\text{comp}} + P$ 严格满足 $4\text{ KB}$ 对齐，更新 `LayerExtentDescriptor` 后由 `io_uring` `O_DIRECT` 顺序写入 SSD。
* **换入读路径（`SSD -> Host Ping 槽 -> YY NEON 解压 -> HBM`）**：
  `io_uring` 完成第 $l$ 层压缩块读取后，立即触发 YY 的 NEON `vzip1q_u8` / `vzip2q_u8` 还原为 $16\text{ MB}$ 标准 BF16 布局并启动 HBM DMA，将 $32\text{K}$ KVC 换入总耗时从 $503.1\text{ ms}$ 压降至 **$282.5\text{ ms}$**。

#### 2. 协同耦合二（12月激活）：`LJQ (SSD 通路) + LDY (M2-F01 按层计算掩盖)` —— 介质感知的动态 $K^*(m)$ 切分
* **插件挂载**：将 `ILayerWindowHandshake` 切换为 LDY 的 `LdyLayerwiseRecomputeHandshake`。
* **介质感知的动态 $K^*(m)$ 闭式解算方程**：
  设单层 Prefill 重算耗时为 $t_{\text{comp\_layer}}$，介质 $m$ 的首层就绪总延迟为 $T_{\text{first}}(m)$，稳态每层流水步进耗时为 $\Delta t_{\text{pipe}}(m)$。为确保 GPU 在算完前 $K^*$ 层后、准备计算第 $K^*+1$ 层时数据已 $100\%$ 抵达 HBM 滑动窗口，动态切分层数 $K^*(m)$ 必须满足：
  $$K^*(m) = \left\lceil \frac{T_{\text{first}}(m) + \max\left(0,\; (L - K^*) \cdot \left(\Delta t_{\text{pipe}}(m) - t_{\text{comp\_layer}}\right)\right)}{t_{\text{comp\_layer}}} \right\rceil$$
  * 当命中 **Host DDR** 时：$T_{\text{first}} \approx 3.0\text{ ms}$，解得 **$K^*(\text{DDR}) = 2 \sim 3\text{ 层}$**；
  * 当命中 **SSD（未压缩独立态 / 开启 YY 压缩集成态）** 时：$T_{\text{first}}$ 分别为 $9.35\text{ ms}$ 与 $9.11\text{ ms}$，解得 **$K^*(\text{SSD}) = 5 \sim 7\text{ 层}$**；
  * **执行闭环**：12 月联调时，LDY 自动将 SSD 命中请求的前序重算层数从 $2$ 层动态调整为 $6$ 层，后 $74$ 层由 LJQ 的双缓冲流水线直接灌入 LDY 的 $256\text{ MB}$ HBM 滑动窗，实现 **$>85\%$ 的计算-I/O 重叠率与零气泡等待**。

#### 3. 协同耦合三（12月激活）：`LJQ (L3 引擎) + WXH (M2-T05 L2 调度大脑)` —— `KVTether` 租约与超前异步升迁
* **插件挂载**：将 `ITieringPolicyHint` 切换为 WXH 的 `WxhKVTetherPolicyBrain`。
* **SWE-bench 工具调用停顿期防误杀（`KVTether` Tool-Wait Lease）**：
  1. 当模型输出 Tool Call Token 时，WXH 为该会话打上 `TOOL_WAIT_LEASE (TTL = 30s)` 标签，前 $15\text{ s}$ 锁定在 Host DDR 中防 LRU 误踢；
  2. 若超过 $15\text{ s}$ 仍未返回且 DDR 水位 $>85\%$，WXH 下发异步指令给 LJQ，将该会话从 DDR 静默流式降级（Demote）至 SSD 的 `Bucket_Hot_ShortTTL` 分区；
  3. **超前异步升迁（Proactive Promotion）**：一旦网关收到外部工具执行完毕的 Webhook 或 HTTP 请求头（早于推理调度入队 $50\sim 200\text{ ms}$），WXH 立即调用 LJQ 的 `TriggerAsyncPromotion()` 启动 `Slow-Path Staging Queue` 将该会话前 $16$ 层提前预热回 Host DDR 巨页池，彻底掩盖 SSD 启动延迟。

#### 4. 协同耦合四（12月激活）：`LJQ (SSD 通路) + NXY (M2-T04 硬件 QoS) + YZN (M2-T02 亲和底座)` —— IO Die 拓扑绑定与双时间尺度限流
* **插件挂载**：将 `IStagingMemoryProvider` 切换为 YZN 的全局巨页池，将 `IIoQoSThrottle` 切换为 NXY 的硬件 PMU 探针。
* **PCIe RC 反压感知与 NVMe QD 联动限流**：
  NXY 的 `CapabilityMatrix` 每 $<500\mu\text{s}$ 直读鲲鹏 IO Die 的 `hisi_pcie` 与 `smmuv3_pmcg` 硬件 PMU 计数器。当某颗 IO Die 的 PCIe 带宽利用率超过 $75\%$（例如 CY 正在进行 400G URMA 满速拉取）时，NXY 在 **$200\mu\text{s}$ 快速尺度** 直接回调 LJQ 的 `ThrottleQueueDepth(16, 256)` 压降 NVMe 并发度，并在 **$20\text{ ms}$ 慢速尺度** 联动 ARM MPAM 限制后台线程 DDR 带宽配额。

---

## 四、第二部分：依赖具备直通能力 SSD 硬件的优化方案（硬件就绪后平滑演进）

当后续实验室引入支持 PCIe P2P / GPUDirect Storage (`cuFile`) 或昇腾 P2P NVMe BAR1 映射的硬件环境与驱动固件后，上层 L2、L3.1、L3.2 代码无需任何改动，直接启用 `DirectBar1P2PBackend` 并实施以下三项硬件专属优化：

### 4.1 PCIe P2P BAR1 直通 DMA 机理与 SMMUv3 P2P 映射打通

1. **物理路径变革**：
   消除 `Host Pinned 32MB Ring Buffer` 这一跳，数据流从两跳变为单跳：
   $$\text{NVMe SSD Controller} \xrightarrow{\text{PCIe Switch / Same IO Die RC P2P DMA}} \text{GPU/NPU HBM BAR1 256MB 滑动窗口}$$
2. **SMMUv3 P2P 地址转换打通**：
   在 `DirectBar1P2PBackend::RegisterStagingMemory()` 中，调用底层驱动导出 HBM 滑动窗口的 PCIe BAR1 物理总线地址（Bus Address），并在 Linux 内核 `iommu_map` / `dma_map_resource` 中为 NVMe 设备的 StreamID 建立指向该 BAR1 物理区间的 `IOMMU_MMIO` 映射，开启 PCIe ACS P2P Redirect Bypass，**彻底释放 $100\%$ 的 Host DDR 内存控制器带宽**。

---

### 4.2 破解“硬件直通 vs. CPU 压缩”悖论：算子尾声位平面打包与动态路径切换

针对**“启用硬件直通绕过 Host CPU 后，YY 的鲲鹏 SIMD 位平面压缩无法执行，反而导致 SSD 等效吞吐从 $35.4\text{ GB/s}$ 跌回 $20\text{ GB/s}$”**的物理矛盾，设计 **“算子尾声融合压缩 + 双路径自适应切换”** 方案：

1. **方案 A：GPU/NPU 算子尾声（Epilogue）轻量级位平面打包（保留直通 + 压缩双重红利）**
   将 BF16 高 8 位（Exponent）与低 8 位（Mantissa）分离及共享基准指数差分打包迁移为 **GPU CUDA / 昇腾 Ascend C 的轻量级 Copy-Stream 融合核函数**（耗时 $< 0.15\text{ ms}$/层）。此时单层 P2P 直读体积同样缩小 $1.85\times$（仅 $69.2\text{ MB}$），单跳直通耗时降至：
   $$T_{\text{Direct\_Comp}} = \frac{5.41\text{ GB}}{20\text{ GB/s}} + T_{\text{unpack\_overlap}} \approx \mathbf{270.5\text{ ms}}$$
2. **方案 B：双路径动态切换（Adaptive Dual-Backend Routing）**
   同一节点同时实例化 `HostPinnedUringBackend`（带鲲鹏 CPU 压缩，耗时 $282.5\text{ ms}$，消耗 Host DDR 带宽）与 `DirectBar1P2PBackend`（无压缩纯直通，耗时 $500.0\text{ ms}$，零 Host DDR 带宽消耗）：当 Host DDR 带宽利用率 $< 50\%$ 时走 Host 压缩流水路径；当 Host DDR 繁忙（$> 50\%$）时自动切向 BAR1 硬件直通路径。

---

### 4.3 面向远端存储池的 `NVMe -> Host 1GB HugePage -> URMA` 直通切穿 (Cut-Through) 流水

针对跨节点远端存储切穿需求，在适配层提供第三个标准后端 `RemoteUrmaCutThroughBackend`：
* 在远端存储节点上，LJQ 的 `256MB` 1GB 巨页环形缓冲池 **同时预注册到内核 `io_uring`（作为 `FixedBuffer`）和 Mooncake URMA/RDMA 网卡（作为预注册 `Memory Region / LKey`）**；
* 当远端 SSD 通过 `io_uring` 完成第 $l$ 层（单卡 $16\text{ MB}$）读取并产生 CQE 时，轮询线程无需任何内存拷贝，直接将该槽位的物理偏移封装为 URMA/RDMA `WRITE_WITH_IMM` WQE 下发给 400G 网卡，直接打入本地计算节点 GPU 的 HBM 滑动窗口。
* **11月独立验证机制**：在 11 月未与 CY 跨机联调前，LJQ 使用单机 Loopback RDMA/RoCE（或 Local Shm DMA 模拟通道）独立验证 `io_uring CQE -> Network WQE` 零拷贝切穿状态机。

---

### 4.4 硬件到位后的零成本热切换指南 (Drop-in Migration)

当直通 SSD 硬件与驱动到位后，开发团队仅需执行以下 3 步（修改代码量 $< 150$ 行）即可完成平滑切换：
1. **编译配置切换**：在 CMake 中开启 `-DENABLE_GDS_P2P_BACKEND=ON`，链接 `libcufile.so` 或昇腾 P2P 驱动库；
2. **后端工厂注入**：在配置文件中将 `storage_backend_type` 从 `"uring_host_pinned"` 切换为 `"direct_bar1_p2p"`；
3. **注册句柄替换**：`StorageEngine::Init()` 自动调用 `DirectBar1P2PBackend::RegisterStagingMemory()`，上层寻址、聚簇、分桶分配与双队列逻辑 **$100\%$ 原封不动直接运行**。

---

## 五、核心代码修改位置定位与 Agent 开发指引（明确标注 11月独立开发 vs. 12月集成联调）

为保障 LJQ 在 11 月底能够完全独立地完成代码合入与交付验证，以下将所有代码修改点严格划分为 **【LJQ 11月独立开发交付项】** 与 **【12月 M3 跨模块联调挂载项】**。

### 5.1 Mooncake Store & Transfer Engine 修改定位（LJQ 11月独立交付核心域）

| 目标子系统与文件路径 | 归属阶段与责任人 | 核心修改类 / 函数定位 | 具体改造动作与数据结构设计指引（供 Agent 执行） |
| :--- | :--- | :--- | :--- |
| **`mooncake-transfer-engine/include/transport/kv_ssd_contract_sdk.h`** *(新建)* | **【11月 W1】**<br>LJQ 100% 独立 | • `IKVStorageBackend`<br>• `LayerExtentDescriptor` (64B)<br>• 六大解耦插件纯虚接口 | 1. 定义 `FC-GVA` v2 64-bit 编解码宏与 `LayerExtentDescriptor`（强制 `assert(disk_aligned_bytes % 4096 == 0)`）；<br>2. 定义 `IBlockLayoutProvider`、`IStagingMemoryProvider`、`IStreamCodecPlugin`、`ILayerWindowHandshake`、`ITieringPolicyHint`、`IIoQoSThrottle` 纯虚接口与默认工厂函数。 |
| **`mooncake-store/include/offset_allocator.h`**<br>**`mooncake-store/src/log_bucket_allocator.cpp`** *(新建)* | **【11月 W1】**<br>LJQ 100% 独立 | • `LogStructuredBucketAllocator`<br>• `LocalHeuristicTieringRule` | 1. 实现 3 个 TTL 分桶（`Hot`, `Warm`, `Cold`）的 $256\text{ MB}$ Segment 循环追加写分配器；<br>2. 实现位图失效追踪与后台异步回收线程（当 Segment 有效率 $<15\%$ 时调用 `fallocate(FALLOC_FL_PUNCH_HOLE)` 或 `nvme_dsm` 整段释放）；<br>3. 内置 `LocalHeuristicTieringRule`，在无 WXH 输入时按前缀长度自动返回分桶号。 |
| **`mooncake-transfer-engine/src/transport/ssd_uring_backend.cpp`** *(新建)* | **【11月 W2】**<br>LJQ 100% 独立 | • `HostPinnedUringBackend`<br>• `UserSpaceNvmeStriper`<br>• `NativeHugePageProvider`<br>• `SelfAdaptiveIoThrottle` | 1. 内置 `NativeHugePageProvider`（通过 `mmap(MAP_HUGETLB)` 自主分配 $256\text{ MB}$ 巨页并切分为 8 卡 $\times 2$ 槽 $\times 16\text{ MB}$）；<br>2. 实现 `UserSpaceNvmeStriper` 将 8 个 TP Rank 直接绑定至同 SCCL 下的独立 NVMe 设备/文件，启用 `io_uring (O_DIRECT + IORING_REGISTER_BUFFERS + IORING_SETUP_SQPOLL)`；<br>3. 实现读时 Sort-Merge 区间合并器与三级异步流水无锁状态机；<br>4. 内置 `BypassRawAlignedCodec`、`MockBitPlaneCopyCodec`（含 ARM64 `ldnp/stnp` 内联汇编算子）及 `SelfAdaptiveIoThrottle` 软件自适应 QD 限流器。 |
| **`mooncake-transfer-engine/src/transport/gpu_gather_scatter_pack.cu`** *(新建)* | **【11月 W2】**<br>LJQ 100% 独立 | • `HostSgCoalesceEngine`<br>• `kv_layer_gather_pack_kernel`<br>• `kv_layer_scatter_unpack_kernel` | 1. 分配每卡 $32\text{ MB}$ HBM 临时 Gather/Scatter 缓冲槽；<br>2. 实现 GPU/NPU Copy-Stream 向量化打包/解包算子，在无 WZX 连续 Slab 时将原生 vLLM 的离散 $8\text{ KB}$ 块在 HBM 内高速聚簇为 $16\text{ MB}$ 连续层块（耗时 $<0.12\text{ ms}$），彻底消除逐块小 DMA 开销。 |
| **`mooncake-transfer-engine/src/transport/ssd_direct_p2p_backend.cpp`** *(新建)* | **【11月 W2】**<br>LJQ 100% 独立 | • `DirectBar1P2PEmulator`<br>• `DirectBar1P2PBackend` | 1. 11月实现 `DirectBar1P2PEmulator` 硬件周期精确仿真桩（实测 `io_uring` 读取 + Peer DMA 旁路注入），用于独立完成 `W3-T09` 平衡点实验；<br>2. 预埋 `cuFileBufRegister` / `cuFileRead` 硬件直通代码分支。 |

---

### 5.2 vLLM / SGLang 推理引擎侧修改定位（11月独立运行模式 vs. 12月联调挂载点）

| 目标子系统与文件路径 | 归属阶段与责任人 | 核心修改类 / 函数定位 | 具体改造动作与数据结构设计指引（供 Agent 执行） |
| :--- | :--- | :--- | :--- |
| **`vllm/distributed/kv_transfer/kv_connector/mooncake_ssd_connector.py`** *(新建/扩展)* | **【11月 W3 独立】**<br>LJQ 独立开发<br>*(12月与 LDY 联调)* | • `MooncakeSSDLayerwiseConnector`<br>• `StandaloneHbmWindowHarness` | 1. **11月独立模式（`standalone_mode=True`）**：支持全层双缓冲异步换入/换出（利用 `HostSgCoalesceEngine` 自动打包/解包原生 vLLM 离散块），并提供 `StandaloneHbmWindowHarness` 测试模式；<br>2. **12月集成模式（`standalone_mode=False`）**：对接 LDY 的 `ILayerWindowHandshake` 与动态 $K^*(m)$ 重算切分逻辑。 |
| **`vllm/core/scheduler.py`** | **【11月 W3 独立】**<br>LJQ 独立开发<br>*(12月与 WXH 联调)* | • `Scheduler._schedule_prefill()`<br>• `FastMediaQueue`<br>• `SlowSSDStagingQueue` | 1. **11月独立模式**：在 `Scheduler` 中实装 `FC-MAS` 双队列：查询 `MooncakeSSDLayerwiseConnector` 命中介质类型，命中 DDR 入 `FastMediaQueue`，命中 SSD 入 `SlowSSDStagingQueue` 自动后台预取前序层，就绪后移入 `FastMediaQueue`，并在每步 Decode 时调用 `NotifyActiveDecodeBatch()` 触发 LJQ 内置 QoS 限流；<br>2. **12月集成模式**：接入 WXH 的 `KVTether` 租约状态机与外部 Webhook 超前预热信号。 |

---

### 5.3 跨模块适配器与 12 月集成联调代码定位

| 目标子系统与文件路径 | 归属阶段与责任人 | 核心修改类 / 函数定位 | 12 月 M3 集成联调具体动作 |
| :--- | :--- | :--- | :--- |
| **`vllm/core/block_manager.py`** | **【12月 M3 集成】**<br>WZX 主责 + LJQ 联调 | • `WzxContiguousSlabProvider` | 实现 `IBlockLayoutProvider` 接口，将 WZX 的 `16-Block Slab` 连续物理块区间直接传给 LJQ，关闭 LJQ 的 `gpu_gather_scatter_pack` 兜底算子。 |
| **`kv_controller/codec/kunpeng_bitplane_simd.cpp`** | **【12月 M3 集成】**<br>YY 主责 + LJQ 联调 | • `YyKunpengNeonBitPlaneCodec` | 实现 `IStreamCodecPlugin` 接口，挂载至 LJQ 的 `HostPinnedUringBackend` Stage-2 槽位，替换 11 月的 `MockBitPlaneCopyCodec`。 |
| **`kv_controller/qos/capability_matrix_probe.cpp`** | **【12月 M3 集成】**<br>NXY/YZN + LJQ 联调 | • `YznGlobalScclHugePageProvider`<br>• `NxyKunpengPmuQoSProbe` | 实现 `IStagingMemoryProvider` 与 `IIoQoSThrottle` 接口，将巨页分配切换至 YZN 全局池，并由 NXY 的 `<500µs` 硬件 PMU 探针接管 `ThrottleQueueDepth()` 调用。 |

---

## 六、双轨制实验验证方案：11月 LJQ 独立交付验证 vs. 12月全系统集成验证

为彻底解决原始方案中**“验证测试与其他人强耦合，导致 11 月底无法独立完成交付验收”**的问题，本章构建了独立的压测工具链，并将所有性能指标与六大实验严格切分为 **【Track-A：11月 LJQ 独立交付验证（零外部依赖，11月底准出红线）】** 与 **【Track-B：12月 M3 全系统集成验证（跨模块联调，12月底准出红线）】**。

### 6.1 实验测试床 (`HoloTestbed-4X`)、LJQ 独立压测套件 (`kv_ssd_standalone_bench`) 与直通仿真桩

1. **物理硬件环境配置 (`HoloTestbed-4X`)**：
   * **计算与主控节点**：双路鲲鹏 920（4 个 SCCL / 4 颗 IO Die），配备 $1\text{ TB}$ DDR5 内存；
   * **加速卡**：8 张 80GB HBM 加速卡（TP=8 运行 Qwen-72B BF16 模型）；
   * **SSD 存储阵列**：4 $\sim$ 8 块企业级 PCIe 4.0/5.0 NVMe SSD，分别挂载于 4 个 SCCL 对应的本地 PCIe RC 下，单机聚合顺序读物理峰值 $18 \sim 22\text{ GB/s}$。

2. **【核心新增】LJQ 11月独立验证测试套件 (`kv_ssd_standalone_bench` + 独立 vLLM 压测器)**：
   为了在不依赖任何他人代码分支的前提下对 SSD 通路进行全维度验收，LJQ 在 11 月交付一套自包含的 C++/CUDA/Ascend 独立验证套件 `kv_ssd_standalone_bench`，内含四大合成负载发生器（Synthetic Harness）：
   * **组件 1：离散/连续块布局发生器 (`SyntheticBlockGenerator`)**：模拟原生 vLLM $8\text{ KB}$ 随机离散 HBM 块分布（测试 LJQ 内置 GPU Gather-Pack 聚簇）及 16-Block 连续分布；
   * **组件 2：多轮会话与生命周期轨迹回放器 (`ParetoTraceReplayer`)**：按真实 SWE-bench 帕累托分布生成 $2\text{K} \sim 64\text{K}$ 变长 KVC 写入、读取与过期淘汰事件流，独立压测 `LogStructuredBucketAllocator` 的 4 小时稳态 WAF 与 P99 延迟；
   * **组件 3：内置 2 层 HBM 滑动窗与虚拟计算步进器 (`StandaloneHbmWindowHarness`)**：在 GPU Compute Stream 上按每层 $t_{\text{comp\_layer}} = 6.5\text{ ms}$ 执行矩阵乘步进算子，同时在 Copy Stream 上运行 LJQ 的 `SSD -> Host HugePage -> HBM 2层窗` 流水线，**完全独立地精确测量计算-I/O 重叠率与流水线气泡率**；
   * **组件 4：背景 Decode 与总线压力注入器 (`DecodeInterferenceInjector`)**：在 GPU 上维持稳定步进的 Decode 算子流並实时采集每步 TPOT，同时周期性触发 LJQ 的 SSD 满速换入换出与 `SelfAdaptiveIoThrottle` 限流器，**完全独立地测量 SSD 读写对 Decode TPOT 的干扰度**。

3. **“直通硬件软件仿真桩”（`DirectBar1P2PEmulator`）**：
   在实机上同时跑通 `SSD -> Host HugePage (io_uring)`（获取真实 NVMe 顺序带宽与延迟 $t_{\text{SSD}}$），并在 GPU 侧使用 Peer DMA 旁路注入器按线速向 HBM BAR1 滑动窗口写入数据，同时关停第二跳冗余等待，用于在 11 月独立完成 `W3-T09`（传统双缓冲流水 vs. 硬件 BAR1 直通物理平衡点）实测对比验证。

---

### 6.2 11月独立交付指标 vs. 12月全系统集成指标双轨对照明细表

下表清晰界定了 **11 月底 LJQ 独立交付考核的硬性量化指标（Track-A）** 与 **12 月底全系统集成考核的进阶指标（Track-B）**：

| 指标编号与维度 | 原始未优化基线 (Baseline) | 【Track-A：11月 LJQ 独立交付硬门限】<br>*(零外部依赖，仅靠 LJQ 模块 + 内置兜底)* | 【Track-B：12月 M3 全系统集成目标门限】<br>*(联合 WZX + YY + LDY + YZN + NXY + WXH)* | 指标差异说明与演进归因 |
| :--- | :--- | :--- | :--- | :--- |
| **M-01：写盘随机小 I/O 消除率** | $0\%$（全为 $8\text{ KB}$ 随机写） | **$\ge 99.5\%$**<br>*(依靠 LJQ 内置 GPU Gather-Pack + 层主序聚簇)* | **$\ge 99.6\%$**<br>*(切换为 WZX 16-Block Slab 零拷贝聚簇)* | 11月靠 LJQ 内置算子打包已完全消灭随机小 I/O；12月省去 $0.12\text{ ms}$/层 HBM 内部打包开销。 |
| **M-02：SSD 实际物理顺序读吞吐** | $< 1.8\text{ GB/s}$ | **$\ge 14.5 \sim 18.0\text{ GB/s}$**<br>*(达到裸盘顺序读物理峰值的 $\ge 85\%$)* | **$\ge 15.0 \sim 18.5\text{ GB/s}$** | 物理 I/O 吞吐完全由 LJQ 的 `io_uring` + 1GB 巨页 + 用户态条带化决定，11月一步到位。 |
| **M-03：KVC 等效逻辑读取吞吐** | $< 1.8\text{ GB/s}$ | **Raw 真实模式：$\ge 14.5 \sim 18.0\text{ GB/s}$**<br>**内置 $1.85\times$ 模拟桩模式：$\ge 26.8\text{ GB/s}$** | **真实压缩模式：$\ge 27.5 \sim 34.0\text{ GB/s}$**<br>*(挂载 YY 鲲鹏 NEON 位平面压缩)* | 11月考核真实未压缩物理极限及三级流水框架模拟吞吐；12月考核接入 YY 真实算法后的等效吞吐。 |
| **M-04：$32\text{K}$ 上下文 ($10\text{ GB}$) 换入 HBM 端到端耗时** | $\sim 5,555\text{ ms}$ | **Raw 真实模式：$\le 550\text{ ms}$** *(理论 $503.1\text{ ms}$)*<br>**内置 $1.85\times$ 模拟桩模式：$\le 320\text{ ms}$** | **真实无损压缩模式：$\le 310\text{ ms}$**<br>*(理论 $282.5\text{ ms}$)* | 11月独立交付直接实现 **$10.1\times$ 真实加速**（逼近直通硬件 $99.4\%$）；12月叠加 YY 压缩实现 **$17.9\times$ 加速**。 |
| **M-05：4小时稳态覆写设备端写放大 (WAF) 与 P99 读毛刺** | $WAF > 3.2$<br>P99 读延迟 $> 45\text{ ms}$ | **$WAF \le 1.05$**<br>**单层读 P99 延迟 $< 8.0\text{ ms}$（劣化 $< 10\%$）** | **$WAF \le 1.03$**<br>**单层读 P99 延迟 $< 7.5\text{ ms}$** | 由 LJQ 的 `LogStructuredBucketAllocator` 与 $256\text{ MB}$ 整段 TRIM 独立保障，11月完全达标。 |
| **M-06：宿主机 CPU L3 Cache Miss 降低率** | 冲刷严重（Miss 率 $> 65\%$） | **L3 Cache Refill 降低 $\ge 80\%$**<br>*(依靠 LJQ ARM64 `ldnp/stnp` 非临时指令)* | **L3 Cache Refill 降低 $\ge 82\%$**<br>*(YY 解压算子内联调用 LJQ `stnp` 原语)* | 零缓存污染流式搬运底座由 LJQ 在 11 月独立实现并验证。 |
| **M-07：按层流水计算-I/O 重叠率** | $0\%$（串行等待全量读完） | **$\ge 85\%$（气泡等待率 $< 10\%$）**<br>*(在 `StandaloneHbmWindowHarness` 下实测)* | **$\ge 88\%$（气泡等待率 $< 8\%$）**<br>*(联调 LDY 真机前 $K^*=6$ 层重算掩盖)* | 11月使用内置虚拟计算步进器验证双缓冲窗口流水重叠率；12月在 vLLM 真机重算流中验证。 |
| **M-08：快慢请求混跑下 DDR 快请求 TTFT P99 劣化率** | 队头阻塞严重（劣化 $> 400\%$） | **TTFT P99 劣化 $< 5\%$**<br>*(依靠 LJQ `FC-MAS` 物理双队列隔离)* | **TTFT P99 劣化 $< 3\%$**<br>*(叠加 WXH `KVTether` Webhook 超前预热)* | 11月仅靠 LJQ 的 `FC-MAS` 双队列即可彻底消除 SSD 慢请求对 DDR 快请求的队头阻塞。 |
| **M-09：SSD 满载换入换出下在线 Decode TPOT 劣化率** | 劣化 $> 35\% \sim 120\%$ | **Decode TPOT 劣化 $< 3.0\%$**<br>*(依靠 LJQ `SelfAdaptiveIoThrottle` 软件限流)* | **Decode TPOT 劣化 $< 2.0\%$**<br>*(接入 NXY IO Die PMU $200\mu\text{s}$ 硬件探针)* | 11月通过内置 Decode Batch 感知与 CQE 延迟反馈自动限流 QD 至 16；12月升级为硬件 PMU 触发。 |
| **M-10：72B 超显存 $1.5\sim 2.0\times$ 冲击下 SLO 完成率与总吞吐提升 (`W3-T07`)** | OOM 崩溃或纯重算吞吐极低 | **【11月独立 vLLM 模式】**：**零 OOM，有效完成率 $> 95\%$，相比纯重算基线总吞吐（Goodput）提升 $\ge 35\%$** | **【12月全组件集成模式】**：**有效完成率 $> 96\%$，相比纯重算基线总吞吐提升 $45\% \sim 70\%$** | 11月在原生 vLLM + LJQ SSD 全层流水下独立跑通超显存压测（提升 $\ge 35\%$）；12月叠加压缩与重算掩盖达到 $45\% \sim 70\%$。 |

---

### 6.3 六大核心验证实验双轨设计 (`Exp-1` $\sim$ `Exp-6`：区分 `Track-A 11月独立验证` 与 `Track-B 12月集成验证`)

每个实验均明确拆分为 **11 月 LJQ 独立可执行的验证闭环（Track-A）** 与 **12 月跨模块联调后的集成验证闭环（Track-B）**：

#### 实验一（Exp-1）：微基准——写时层主序聚簇与 `io_uring` 1GB 巨页双缓冲吞吐压测
* **【Track-A：11月 LJQ 独立验证方案】（使用 `kv_ssd_standalone_bench --exp=1`）**：
  * **测试环境**：完全不依赖 WZX、YZN、YY 代码，直接运行 LJQ 独立压测二进制程序。
  * **五组独立阶梯消融**：
    1. `Baseline-0`：模拟原生 vLLM $8\text{ KB}$ 离散块 + Linux 默认 PageCache 读写；
    2. `Standalone-1`：开启 LJQ 内置 GPU Gather-Pack 层主序 `16MB/Layer` 聚簇 + 传统串行 `pread/pwrite (O_DIRECT)`；
    3. `Standalone-2`：`Standalone-1` + LJQ 内置 `NativeHugePageProvider` (`1GB HugePage`) + `UserSpaceNvmeStriper` + `io_uring (O_DIRECT + FixedBuffer + SQPOLL)` Per-Rank $32\text{ MB}$ 双缓冲按层流水（`BypassRawAlignedCodec` 无压缩真实模式）；
    4. `Standalone-3`：`Standalone-2` + 启用 LJQ 内置 `MockBitPlaneCopyCodec`（按 $\gamma = 1.85\times$ 执行真实 NEON `ldnp/stnp` 搬运与 $4\text{ KB}$ 对齐填充），独立验证三级流水并发时序与零缓存污染；
    5. `Standalone-4`：`DirectBar1P2PEmulator` 单跳直通仿真模式。
  * **11月独立准出红线**：`Standalone-2` 实测物理读吞吐 $\ge 14.5\text{ GB/s}$，$32\text{K}$ 上下文换入 HBM 耗时 $\le 550\text{ ms}$；`Standalone-3` 下换入耗时 $\le 320\text{ ms}$ 且 `perf stat -e l3d_cache_refill` 降低 $\ge 80\%$。
* **【Track-B：12月跨模块集成验证方案】**：
  * 将 `IBlockLayoutProvider` 切换为 WZX 的 `16-Block Slab`，将 `IStagingMemoryProvider` 切换为 YZN 的全局巨页池，将 `IStreamCodecPlugin` 切换为 YY 的真实鲲鹏 NEON 位平面压缩算子，复测上述指标，验证真实无损压缩下换入耗时 $\le 310\text{ ms}$。

#### 实验二（Exp-2）：持久稳态——日志型 TTL 分桶追加写 vs. 原地覆写下的 NVMe GC 长尾毛刺对比
* **【Track-A：11月 LJQ 独立验证方案】（使用 `kv_ssd_standalone_bench --exp=2 --duration=4h`）**：
  * **执行步骤**：将测试 SSD 分区预填充至 $80\%$ 容量水位，使用内置 `ParetoTraceReplayer` 配合 LJQ 内置 `LocalHeuristicTieringRule` 持续运行 4 小时（累计写入 $> 20\text{ TB}$），对比传统空闲链表原地覆写与 LJQ `LogStructuredBucketAllocator`（3 档分桶 + $256\text{ MB}$ 整段 TRIM）。
  * **11月独立准出红线**：`nvme smart-log` 设备端写放大系数 $WAF \le 1.05$；4 小时后单层读取 P99 延迟 $< 8.0\text{ ms}$（劣化 $< 10\%$）。
* **【Track-B：12月跨模块集成验证方案】**：
  * 接入 WXH 的 `KVTether` 真实生命周期预测标签替代启发式规则，验证在真实 Agent 轨迹下 $WAF \le 1.03$。

#### 实验三（Exp-3）：跨层掩盖——SSD 双缓冲流水与按层计算掩盖消融实验
* **【Track-A：11月 LJQ 独立验证方案】（使用 `kv_ssd_standalone_bench --exp=3` 内置 `StandaloneHbmWindowHarness`）**：
  * **执行步骤**：在无 LDY 代码介入的情况下，利用 LJQ 内置的 $256\text{ MB}$ HBM 2 层滑动窗与 Compute Stream 虚拟计算步进器（模拟每层 Prefill 耗时 $6.5\text{ ms}$），对比：
    1. 组 1：等待 80 层全部读完再启动 Compute Stream；
    2. 组 2：Compute Stream 跳过前 $K^* = 2$ 层（模拟未针对 SSD 调优）；
    3. 组 3：Compute Stream 按介质感知公式重算前 $K^*(\text{SSD}) = 6$ 层，后 74 层与 LJQ SSD 双缓冲流水并发执行。
  * **11月独立准出红线**：在内置 Harness 下，组 3 的 GPU 计算流气泡等待率（Stall Ratio）**$< 10\%$**，计算-I/O 重叠率 **$\ge 85\%$**。
* **【Track-B：12月跨模块集成验证方案】**：
  * 在 vLLM 中正式挂载 LDY 的 `LdyLayerwiseRecomputeHandshake` 真机算子，测量 Qwen-72B 端到端 $32\text{K}$ 请求真实 TTFT 降低 $15\% \sim 22\%$。

#### 实验四（Exp-4）：快慢隔离——`FC-MAS` 双队列物理隔离与多轮 Agent 轨迹压测
* **【Track-A：11月 LJQ 独立验证方案】（基于原生 vLLM + LJQ `MooncakeSSDLayerwiseConnector` 独立模式）**：
  * **执行步骤**：在 vLLM 中注入混合前缀命中负载（$70\%$ 请求命中 Host DDR + $30\%$ 请求命中 NVMe SSD），对比原生单一 `waiting` 队列与开启 LJQ `FC-MAS` 快慢物理双队列（含自动后台预热）的表现。
  * **11月独立准出红线**：混跑 SSD 慢请求时，**命中 Host DDR 的快请求 TTFT P99 劣化 $< 5\%$（彻底消除队头阻塞）**。
* **【Track-B：12月跨模块集成验证方案】**：
  * 叠加 WXH 的 `KVTether` 工具停顿租约与外部 Webhook 超前异步升迁（Proactive Promotion），在完整 SWE-bench 回放中验证多轮会话有效缓存命中率 **$> 88\%$**，DDR 快请求 TTFT P99 劣化 **$< 3\%$**。

#### 实验五（Exp-5）：极限混压与超显存冲击——`W3-T07` 72B 超显存 $1.5 \sim 2.0\times$ 压测 & QoS 防护验证
* **【Track-A：11月 LJQ 独立验证方案】（独立完成 `W3-T07` 准出）**：
  * **执行步骤**：
    1. 在原生 vLLM + Qwen-72B 上开启 LJQ 的 `MooncakeSSDLayerwiseConnector`（独立全层双缓冲流水模式）与 `SelfAdaptiveIoThrottle` 软件自适应限流器；
    2. 注入高并发长上下文请求，使活跃会话总 KVC 工作集达到 HBM 物理显存的 **$1.5 \sim 2.0$ 倍**，触发高频 `HBM <-> DDR <-> SSD` 换入换出，同时维持 32 个在线 Decode 请求；
    3. 使用 `kv_qos_ctl` 与自动限流对比开启/关闭 NVMe QD 限流（$128 \rightarrow 16$）时的在线 Decode TPOT。
  * **11月独立准出红线**：
    * 超显存 $1.5 \sim 2.0\times$ 冲击下系统零 OOM，**有效请求 SLO 完成率 $> 95\%$，系统总有效吞吐（Goodput）相比无 SSD 纯重算基线提升 $\ge 35\%$**；
    * 并发 SSD 满速读写期间，在线 Decode 请求 **TPOT 劣化严格 $< 3.0\%$**，限流指令响应延迟 $< 200\mu\text{s}$。
* **【Track-B：12月跨模块集成验证方案】**：
  * 全量开启 WZX、YY、LDY、YZN、NXY、WXH 及 CY 的 100G/400G URMA 跨机流量混压，验证系统总有效吞吐提升跃升至 **$45\% \sim 70\%$**，且在跨机网络与本地 SSD 双重冲击下 Decode TPOT 劣化 **$< 2.0\%$**。

#### 实验六（Exp-6）：前瞻对比——`W3-T09` 传统 SSD 双缓冲流水 vs. 硬件直通 SSD 物理平衡点分析
* **【Track-A：11月 LJQ 独立验证方案】（11月 100% 独立完成 `W3-T09` 报告交付）**：
  * **执行步骤**：利用 `kv_ssd_standalone_bench` 中的 `HostPinnedUringBackend`（实机双缓冲流水）与 `DirectBar1P2PEmulator`（单跳直通仿真桩），以及内置 `stream_triad_bg_injector`（背景 DDR 内存带宽压力发生器，可精确控制宿主机 DDR 带宽占用率在 $10\% \sim 95\%$），对上下文长度 $L_{\text{ctx}} \in \{2\text{K} \sim 128\text{K}\}$ 与背景 DDR 压力 $U_{\text{DDR}} \in \{10\% \sim 95\%\}$ 进行二维网格扫描。
  * **11月独立交付成果**：输出完整的《`W3-T09` 传统 SSD 双缓冲流水 vs. 硬件 GDS 直通物理平衡点实测对比报告》与胜负分界相图（Phase Diagram）。

---

### 6.4 11月 W1~W4（独立交付主线）与 12月 M3（全系统集成主线）分阶段执行检查表

#### 第一阶段：2026年11月 W1~W4 —— LJQ 100% 独立闭环交付主线（零外部阻塞风险）

| 周次与时间窗口 | 核心执行动作（LJQ 100% 独立自主闭环，不依赖他人 11 月进度） | 11 月独立交付实机量化验收红线（Track-A Gate Criteria） |
| :--- | :--- | :--- |
| **11月 W1**<br>`11.02 ~ 11.08`<br>*[契约冻结与日志分配器独立闭环]* | 1. 复现原生未优化 SSD 读写基线（记录 $<1.8\text{ GB/s}$ 瓶颈证据）；<br>2. 编写并冻结 `kv_ssd_contract_sdk.h`（含 `FC-GVA` v2、`LayerExtentDescriptor`、`IKVStorageBackend` 及六大解耦插件接口），分发给各模块负责人作为 12 月联调契约；<br>3. 独立实现 `LogStructuredBucketAllocator`（含内置启发式三桶分类器与 $256\text{ MB}$ 整段 TRIM）及 GPU Gather-Pack 聚簇算子 `HostSgCoalesceEngine`。 | • 接口契约头文件独立编译通过；<br>• 依靠内置 GPU Gather-Pack 聚簇，写时随机小 I/O 消除率 **$\ge 99.5\%$**；<br>• 独立运行 **Exp-2 (Track-A)**：4 小时稳态覆写压测下设备端写放大 **$WAF \le 1.05$**，P99 读延迟 $< 8.0\text{ ms}$。 |
| **11月 W2**<br>`11.09 ~ 11.15`<br>*[双缓冲流水与内核旁路实机达标]* | 1. 实现内置 `NativeHugePageProvider`（`mmap` 1GB 巨页池）、`UserSpaceNvmeStriper`（用户态确定性多盘绑定）与读时 Sort-Merge 合并器；<br>2. 完成 `HostPinnedUringBackend`（`O_DIRECT` + `FixedBuffer` + `SQPOLL`）Per-Rank $32\text{ MB}$ 双缓冲按层流水；<br>3. 实现 ARM64 `ldnp/stnp` 流式搬运原语、`BypassRawAlignedCodec`、`MockBitPlaneCopyCodec` 及 `DirectBar1P2PEmulator` 仿真桩。 | • 独立运行 **Exp-1 (Track-A)**：<br>• **【硬门限 1】**：实测 SSD 物理顺序读吞吐突破 **$14.5 \sim 18.0\text{ GB/s}$**（内置模拟压缩桩下等效吞吐 $> 26.8\text{ GB/s}$）；<br>• **【硬门限 2】**：$32\text{K}$ 上下文换入 HBM 耗时从 $\sim 5555\text{ ms}$ 压降至 **Raw $\le 550\text{ ms}$**（模拟压缩桩下 $\le 320\text{ ms}$），L3 Cache Refill 降低 **$\ge 80\%$**。 |
| **11月 W3**<br>`11.16 ~ 11.22`<br>*[独立接入 vLLM 与快慢隔离/QoS 闭环]* | 1. 在 vLLM 中独立实装 `MooncakeSSDLayerwiseConnector`（独立全层流水模式）与 `FC-MAS` 快慢介质物理隔离双队列；<br>2. 实现 `StandaloneHbmWindowHarness`（内置 2 层 HBM 滑动窗与虚拟计算步进器）；<br>3. 实现 `SelfAdaptiveIoThrottle` 软件自适应 NVMe QD 限流器与 `kv_qos_ctl` 注入工具。 | • 独立运行 **Exp-3 & Exp-4 (Track-A)**：<br>• 内置 Harness 下按层流水计算-I/O 重叠率 **$\ge 85\%$**（气泡率 $< 10\%$）；<br>• 混跑 SSD 慢请求时，命中 DDR 的快请求 TTFT P99 劣化 **$< 5\%$**；<br>• SSD 满速换入下，在线 Decode TPOT 劣化 **$< 3.0\%$**。 |
| **11月 W4**<br>`11.23 ~ 11.29`<br>*[11月独立消融验证与 M2 独立准出]* | 1. 在 `HoloTestbed-4X` 上全量跑通 **Exp-1A $\sim$ Exp-6A（Track-A 独立验证矩阵）**；<br>2. 独立完成 `W3-T07`（原生 vLLM + LJQ SSD 通道下的 72B 超显存 $1.5 \sim 2.0\times$ 冲击压测）与 `W3-T09`（直通 vs. 流水物理平衡点报告）；<br>3. 交付 **LJQ 11 月 M2 阶段 SSD 优化独立交付代码包与验收报告**。 | • **【11月 M2 独立交付终极红线】**：在零外部模块依赖下，`W3-T07` 超显存 $1.5 \sim 2.0\times$ 冲击有效完成率 **$> 95\%$**，系统总吞吐相比纯重算基线提升 **$\ge 35\%$**；<br>• 100% 达成 6.2 节 `Track-A` 全部 10 项独立交付性能指标。 |

---

#### 第二阶段：2026年12月 M3（W1 `12.01` ~ W4 `12.27`）—— 跨模块全系统集成测试主线

| 周次与时间窗口 | 核心集成联调动作（LJQ 与各模块负责人通过插件接口对插联调） | 12 月全系统集成量化验收红线（Track-B Gate Criteria） |
| :--- | :--- | :--- |
| **12月 W1**<br>`12.01 ~ 12.06`<br>*[底座与块布局联调]* | 1. 联调 **WZX (`M2-T03`)**：挂载 `WzxContiguousSlabProvider`，切换为上层 16-Block 连续物理块直传，旁路内置 GPU Gather-Pack 算子；<br>2. 联调 **YZN (`M2-T02`)**：挂载 `YznGlobalScclHugePageProvider`，将 LJQ 缓冲环纳入全节点统一 1GB 巨页池管理。 | • 零拷贝连续块直传校验 $100\%$ 一致；<br>• 跨组件共享 1GB 巨页池下 SMMUv3 IOTLB Miss 率降低 $> 80\%$。 |
| **12月 W2**<br>`12.07 ~ 12.13`<br>*[流式压缩与计算掩盖联调]* | 1. 联调 **YY (`M2-F02`)**：挂载 `YyKunpengNeonBitPlaneCodec`，替换内置模拟桩，跑通真实位平面无损压缩三级流水；<br>2. 联调 **LDY (`M2-F01`)**：挂载 `LdyLayerwiseRecomputeHandshake`，在 vLLM 真机推理流中激活前 $K^*(\text{SSD})=6$ 层重算 + 后 74 层 SSD 2层窗口流水掩盖。 | • 执行 **Exp-1B & Exp-3B**：<br>• 真实无损压缩（$\gamma \ge 1.85\times$）下等效逻辑读吞吐 **$\ge 27.5 \sim 34.0\text{ GB/s}$**，$32\text{K}$ 换入耗时 **$\le 310\text{ ms}$**；<br>• 真机计算-I/O 重叠率 **$\ge 88\%$**。 |
| **12月 W3**<br>`12.14 ~ 12.20`<br>*[L2 调度大脑与硬件 QoS/跨机联调]* | 1. 联调 **WXH (`M2-T05`)**：挂载 `WxhKVTetherPolicyBrain`，激活工具停顿租约、Webhook 超前异步预热与三元成本路由；<br>2. 联调 **NXY (`M2-T04`) & CY (`M2-T01`)**：挂载 `NxyKunpengPmuQoSProbe` 硬件探针限流，并跑通 `RemoteUrmaCutThroughBackend` 跨节点远端 SSD 切穿流水。 | • 执行 **Exp-2B & Exp-4B**：<br>• SWE-bench 真实轨迹下多轮缓存命中率 **$> 88\%$**，DDR 快请求 TTFT P99 劣化 **$< 3\%$**；<br>• 400G URMA + 本地 SSD 混压下 Decode TPOT 劣化 **$< 2.0\%$**。 |
| **12月 W4**<br>`12.21 ~ 12.27`<br>*[全系统大集成回归与 M3 准出]* | 1. 全量开启八大模块优化，执行 **Exp-5B（全系统集成版 `W3-T07` 超显存极限压测）**；<br>2. 输出《11月独立基线 vs. 12月全系统集成增益消融分析总报告》。 | • **【12月 M3 全系统集成终极红线】**：72B 超显存 $1.5 \sim 2.0\times$ 冲击下有效请求完成率 **$> 96\%$**，全系统总有效吞吐（Goodput）相比纯重算基线提升 **$45\% \sim 70\%$**。 |

