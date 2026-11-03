# 基于鲲鹏 SIMD 与 IOStash 的 KVC 边界流式位平面压缩与紧凑池：优化与验证详细方案设计 (V1.0)

> **任务代号**：`M2-F02`（11月 KVC 前沿开拓航道二 · 空间体积维 $B_{\text{wire}}$）  
> **版本**：V1.0 ｜ **日期**：2026-11-03  
> **模块职责**：KVC 压缩与解压缩优化主责工程师 (`1.0 FTE`) ｜ **协同接口人**：底层传输数据面挂载点接口人 (CY)、边界描述符编译对齐接口人 (WZX)、按层计算通信掩盖接口人 (LDY)  
> **目标架构底座**：双路鲲鹏 920 处理器（ARMv8.2-A，8 个 SCCL / 16 通道 DDRC，支持 NEON/SVE 向量指令集、MPAM L3 分区与 SMMUv3 `IOStash` 缓存直注）+ ConnectX-6/7 或 UB 400G 网卡  
> **双重交付定位**：  
> 1. **开发人员 + AI Agent 协作施工蓝图**：面向系统底座研发人员与编码 Agent，提供剥离大模型数学细节、纯 C++ 系统与微架构层面的代码切入点、第三方开源库选型集成、双视图描述符设计及 `IOStash` 硬件加速实现；  
> 2. **M2 门禁性能验收规范**：提供从微基准（M08）到 vLLM + Mooncake 端到端推理（M01/M02）的四级正交测试场景构造方法与验收红线。

---

## 一、 系统定位与专有技术术语释义

按照项目工程规范，在深入技术方案前，对核心专有名词首次出现提供直白功能释义：

1. **KVCache**：大模型注意力键值缓存（大模型自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免后续 Token 生成时重复计算注意力）；
2. **Saved-Prefill**：首字生成预计算节省（利用已缓存的 KV Cache 避免重复计算 Prompt，从而显著降低首 Token 延迟 TTFT）；
3. **UBMEM**：统一总线内存直通共享协议（支持跨节点与异构设备间直接共享内存地址空间的底层通信协议）；
4. **URMA**：通用远程直接内存访问（用户态的高性能 RDMA 驱动接口与通信协议）；
5. **Direct-View**：远端直读（直接通过高速总线跨节点读取远端显存中的 KV 数据，不产生本地显存拷贝）；
6. **Copy-to-HBM**：拷贝到本地显存（通过 DMA 将远端 KV 数据完整搬运至本地高带宽显存 HBM 中）；
7. **IOStash**：输入输出缓存直注技术（PCIe 设备或网卡发起 DMA 写入时，携带 Steering Tag 缓存提示直接将数据写入指定的 CPU L3 Cache，避免先写入内存带来的内存控制器带宽消耗与缓存未命中延迟）；
8. **MPAM**：内存系统资源分区与监控（ARM 架构下的硬件级缓存与内存带宽分区隔离技术，可通过配置路掩码为特定进程锁定专属 L3 Cache 容量）；
9. **SMMUv3**：系统内存管理单元第三版（ARM 架构下负责 DMA 地址转换与缓存属性注入的硬件单元，支持通过流表项 STE 配置 `ALLOCHINT` 实现硬件靶向缓存直注）；
10. **Bit-Plane（位平面）**：位平面分离技术（将多字节数值类型按位或按字节拆解为多个同权重的平整数据流，使得同一位平面的数据呈现高度相似的局部相关性）；
11. **Non-Temporal Store（非临时流式存储）**：ARMv8 架构的流式写入指令（如 `stnp`，提示处理器该写入数据后续不会被 CPU 立即复用，从而绕过各级 Cache 直接经写合并缓冲区刷入内存，消除写分配带来的 Cache 污染）；
12. **TTFT**：Time To First Token（首字生成延迟 / 首 Token 响应时间）；
13. **TPOT**：Time Per Output Token（每个输出 Token 的生成耗时 / 单字生成延迟）；
14. **DDRC**：双倍速率同步动态随机存取内存控制器（Double Data Rate Controller，负责调度并执行主机内存读写请求的物理硬件单元）。

---

## 二、 第一性原理辩证审视：为什么要做 KVC 位平面压缩与 IOStash？有何物理陷阱？

在开展工程实现前，必须从计算机体系结构第一性原理出发，辩证审视 **“KVC 流式压缩”** 与 **“利用空闲 CPU + L3 Cache `IOStash` 加速编解码”** 的**正确性、完整性与物理边界**。

### 2.1 为什么通用压缩库（直接调用 LZ4 / Zstd）在 KVCache 上会失效？

* **物理事实（BF16/FP16 的双极化信息熵）**：
  在大模型推理生成的 `BF16`（1 bit 符号 + 8 bits 指数 + 7 bits 尾数）或 `FP16` KVCache 张量中，高低字节的统计特性截然相反：
  * **高字节（High Byte，含符号位与指数位 Exponent）**：经过 LayerNorm 与 RoPE 旋转位置编码后，同层激活值的数量级高度集中在狭窄区间（例如指数集中分布在 `120 ~ 127`），**信息熵极低（仅 `2.2 ~ 3.1 bits/byte`）**，具备极高的无损压缩潜力；
  * **低字节（Low Byte，尾数位 Mantissa）**：服从近似均匀分布，**信息熵接近理论极限（`7.6 ~ 7.9 bits/byte`）**，属于不可压缩的随机噪声。
* **直接调用通用压缩库的后果**：
  若直接将连续的 `BF16` 内存块递交给原生 `LZ4` 或 `Zstd`，高熵尾数位与低熵指数位每隔 1 字节交替出现（`[Exp0, Man0, Exp1, Man1, ...]`），彻底打断了字节级重复模式（LZ77 字典匹配率趋近于 0），导致**压缩比仅为 `1.05x ~ 1.08x`（几乎无压缩），且白白浪费 50% 的 CPU 算力去尝试压缩随机尾数位**。
* **第一性原理破局（SIMD 位平面解交织 + 仅对高字节熵编码）**：
  对标开源成果 **`ZipNN`**、**`ZipServ`** 与 **`C-Blosc2`**：
  1. 先用 **鲲鹏 ARM NEON/SVE 硬件解交织指令（`vuzp1q_u8` / `vuzp2q_u8`）**，在寄存器内将 `256 KB` 的 BF16 切片瞬间拆分为 **`128 KB` 连续指数平面（High-Byte Plane）** 与 **`128 KB` 连续尾数平面（Low-Byte Plane）**；
  2. **只对 `128 KB` 指数平面调用极速熵编码（Zstd 底层 `HUF` 霍夫曼 / FSE 或 `BloscLZ`）**，将其从 `128 KB` 压缩至 `32 ~ 45 KB`；
  3. 对 `128 KB` 尾数平面，在 **无损模式（Mode 0）** 下直接原样透传（零 CPU 编码开销），在 **紧凑模式（Mode 1：动态块缩放 FP8 / 尾数截断）** 下直接硬件移位截断。
  * **定量收益**：无损压缩比跃升至 **`1.55x ~ 1.75x`（体积缩减 `36% ~ 43%`，100% Bit-Exact 零精度损失）**；因为编码器只需处理一半的字节数，**CPU 编解码吞吐直接翻倍**。

---

### 2.2 辩证分析：利用鲲鹏空闲 CPU 与 L3 Cache IOStash 加速压缩/解压缩的巨大收益与三大物理陷阱

利用 HOST 上空闲的 CPU 和 L3 Cache 探索 `IOStash` 能力实现编解码提速，切中了主机侧流式压缩的核心瓶颈（**内存通道带宽放大墙**）。但若无配套的流控与指令改造，直接开启 `IOStash` 会引发严重的 **L3 缓存颠簸（Cache Thrashing）负收益**。以下给出严格的定量算账与辩证设计：

#### （1）必要性与正确性证明：为什么不用 IOStash，CPU 解压就会撞上“DDRC 3.20倍内存带宽放大墙”？

假设原始 KVC 体积为 $1.0\text{ GB}$，压缩后体积为 $0.60\text{ GB}$（压缩比 $1.67\times$）。在 `Get` 路径上（远端存储节点 $\to$ 本地 HOST 边界解压整备 $\to$ GPU/NPU 按层自取）：

* **传统路径（无 `IOStash`，网卡 DMA 写 DDR + CPU 从 DDR 读出解压再写回 DDR）**：
  1. 网卡 RNIC/UB DMA 将压缩流写入本地 Host DDR：产生 **$0.60\times$ DDRC 写流量**；
  2. 鲲鹏 CPU 核从 Host DDR 读取压缩流进 Cache 进行解压：产生 **$0.60\times$ DDRC 读流量**（遭遇 `~95ns` 的 DRAM 访存停顿）；
  3. CPU 将解压还原后的标准 BF16 KVC 写入 Host DDR 边界池：产生 **$1.00\times$ DDRC 写流量**；
  4. GPU/NPU 按边界描述符从 Host DDR 边界池 DMA 拉取 KVC：产生 **$1.00\times$ DDRC 读流量**。
  * **全流程 DDRC 内存流量放大比**：
    $$\text{Traffic}_{\text{DDRC}} = 0.60 + 0.60 + 1.00 + 1.00 = \mathbf{3.20\times}$$
  * 当逻辑 KVC 吞吐达到 `40 GB/s` 时，单 SCCL 的 DDRC 内存控制器必须承受 $40 \times 3.20 = \mathbf{128\text{ GB/s}}$ 的读写洪峰，直接使鲲鹏单 SCCL 的内存通道饱和（`local_ddrc_util -> 1.0`），导致解压吞吐卡死并严重干扰前台在线任务。

* **引入鲲鹏 `IOStash`（PCIe Steering Tag + SMMUv3 Direct L3 Cache Injection）+ 非临时存储（Non-Temporal Store）后**：
  1. 网卡收到压缩切片（约 `150 KB`）时，携带硬件 `Steering Tag` 经由鲲鹏 IO Die 的 SMMUv3 直接注入到目标 SCCL 的 **L3 Cache（Tag Allocate，产生 $0.00\times$ DDRC 写流量）**；
  2. 绑定在该 SCCL 上的空闲 CPU Worker 核收到完成事件后，直接从 **本地 L3 Cache 命中读取压缩切片（延迟仅 `~12ns`，带宽 `> 400 GB/s`，产生 $0.00\times$ DDRC 读流量）**；
  3. CPU 在 NEON/SVE 寄存器内完成位平面解压与拼装后，使用 **ARMv8 非临时成对存储指令（`stnp q0, q1, [dst]`，Non-Temporal Store Pair）**，绕过 L3 Cache 直接经写合并缓冲区（Write-Combining Buffer）流式写入 Host DDR 边界池（产生 **$1.00\times$ DDRC 写流量**）；
  4. GPU/NPU 从 Host DDR 边界池读取（产生 **$1.00\times$ DDRC 读流量**）。
  * **优化后 DDRC 内存流量放大比**：
    $$\text{Traffic}_{\text{DDRC, IOStash}} = 0.00 + 0.00 + 1.00 + 1.00 = \mathbf{2.00\times}$$
  * **带宽收益**：直接削减了 **`37.5%`** 的主机 DDRC 内存通道带宽消耗；消除了 CPU 读取压缩源的 DRAM 访存停顿，**单核解压吞吐提升 `1.45x ~ 1.80x`**。

```
+----------------------------------------------------------------------------------------------------------+
|               【传统 DDR 中转解压 (3.20x DDRC 流量)】 vs. 【鲲鹏 IOStash + stnp 流式解压 (2.00x)】          |
+----------------------------------------------------------------------------------------------------------+
| ❌ 传统无 IOStash 路径 (DDRC 流量 = 3.20 GB / 1GB KVC，内存控制器饱和瓶颈)：                                |
|   [网卡 RNIC] ──(1.写0.6G)──► [Host DDR] ──(2.读0.6G, 95ns停顿)──► [CPU解压] ──(3.写1.0G)──► [Host DDR]    |
|                                                                                               │          |
|                                                                      [GPU/NPU HBM] ◄─(4.读1.0G)┘          |
+----------------------------------------------------------------------------------------------------------+
| ✅ 鲲鹏 IOStash + NEON/SVE + stnp 路径 (DDRC 流量 = 2.00 GB / 1GB KVC，省 37.5% 内存带宽，单核提速 1.6x)：   |
|   [网卡 RNIC] ══(1.IOStash 直注 L3, 0 DDR流量!)══► [鲲鹏 SCCL L3 Cache 专属分区 (6MB MPAM 锁路)]            |
|                                                                 │                                        |
|                                                   (2.L3 Hit 直读, 12ns, 0 DDR流量!)                       |
|                                                                 ▼                                        |
|   [GPU/NPU HBM] ◄─(4.按边界描述符直拉 1.0G)── [Host DDR 边界池] ◄─(3.CPU stnp 绕过L3直写 1.0G)─ [CPU SIMD] |
+----------------------------------------------------------------------------------------------------------+
```

#### （2）完整性剖析：盲目开启 IOStash 必踩的三大物理陷阱及工程破解法则

若仅在驱动层开启 `IOStash` 开关而缺少配套流控与指令适配，系统将出现负收益。本方案实施以下三项工程防护：

1. **陷阱一：在途窗口（`64 MB`）远超 L3 容量（`8 MB`）引发的“L3 自我踩踏风暴（Stash Eviction Storm）”**
   * **物理机理**：单队列在途数据若达到 `64 MB`（如 `1024` 个切片），而鲲鹏 920 单个 SCCL 的 L3 Cache 扣除控制面锁定后，留给数据面 `IOStash` 的空间仅为 **`6 MB ~ 8 MB`**。若网卡将 `64 MB` 数据无节制 Stash 进 L3，后到达的切片会在 CPU 尚未解压前将先到的切片踢回 DDR，产生严重的 Cache 颠簸与总线冲突；
   * **工程破解（信用门控定长 Stash 环形窗 `Credit-Gated L3 Stash Ring`）**：开辟一个包含 **`32 个槽位 × 160 KB = 5.12 MB`** 的固定物理地址环形缓冲区（`L3_Stash_Ring`），并开启硬件 `IOStash`。网卡每次最多向该环形窗投递 `32` 个切片；CPU Worker 每解压完 1 个槽位，立即原地归还 1 个信用（复用同一段物理地址，使其稳定常驻 L3 Cache 热区）。
2. **陷阱二：解压后的输出写操作把待解压的输入从 L3 中踢走（Output-Evicts-Input Pollution）**
   * **物理机理**：CPU 解压出的 `256 KB` 原始 BF16 数据由 GPU/NPU 的 DMA 引擎直接读取，CPU 不再复用。若使用常规 `memcpy` 或普通 SIMD 存储指令写回，按默认 Write-Allocate 策略，输出数据会强行分配进 L3 Cache，挤占 Stash 输入空间；
   * **工程破解（非对称缓存策略：`L3-Keep Read` + `Non-Temporal Store Write`）**：读取 L3 Stash 输入使用常规缓存加载，而写入 Host DDR 边界池**强制使用 ARM64 非临时流式存储汇编 `stnp`（Store Pair Non-Temporal）**，确保解压输出绕过 L3 Cache 直达 DDR。
3. **陷阱三：跨 SCCL 伪共享与远核拉取（Cross-SCCL Snoop Penalty）**
   * **物理机理**：若网卡将数据包 Stash 进 `SCCL 1` 的 L3 Cache，而解压线程被调度在 `SCCL 2` 上，将触发跨 SCCL 的 HHA 缓存行迁移；
   * **工程破解（`SCCL-Jetty-Core` 三位一体硬亲和）**：严格绑定网卡队列（QP/Jetty）的靶向节点 ID（`StashNID`）与解压 Worker 线程到同一个 SCCL 的空闲物理核上（每个 SCCL 抽调 2 个空闲核，4 个 SCCL 共 8 核）。

---

### 2.3 8 核鲲鹏 SIMD + IOStash 吞吐与长上下文网络传输时延数学验证

#### （1）鲲鹏 920 算力与吞吐定量推导
* **硬件规格**：主频 $2.6\text{ GHz}$，双 128-bit NEON 执行单元每周期吞吐 $32\text{ 字节}$（`uzp1/uzp2` 或 `zip1/zip2`）；
* **L3 命中状态下的算子耗时**：
  * 位平面分离/重交织：约 $0.15\text{ cycles/byte}$，单核吞吐达到 $17.3\text{ GB/s}$；
  * 指数平面极速熵编码（仅对 50% 字节编码）：使得等效处理速度翻倍；
  * 单核无损模式下：编码吞吐约 $7.5\text{ GB/s}$，解码吞吐约 $13.5\text{ GB/s}$；
* **8 核跨 4 个 SCCL 并行吞吐**：
  * 编码聚合吞吐：$7.5\text{ GB/s} \times 8 = \mathbf{60.0\text{ GB/s}}$；
  * 解码聚合吞吐：$13.5\text{ GB/s} \times 8 = \mathbf{108.0\text{ GB/s}}$；
  * **结论**：8 核 SIMD 吞吐完全覆盖 400Gbps（$50\text{ GB/s}$）的物理网卡线速，不会成为流水线瓶颈。

#### （2）32K Qwen-72B 长上下文传输时延对比
以 32K 上下文（原始 KVC 体积 $10.0\text{ GB}$，95% 命中需传输 $9.5\text{ GB}$，物理网卡单口有效带宽 $38.0\text{ GB/s}$）为例：
* **原始未压缩传输耗时**：
  $$T_{\text{raw}} = \frac{9.5\text{ GB}}{38.0\text{ GB/s}} = 250.0\text{ ms}$$
* **Mode 0（无损位平面压缩 $1.65\times$，网上传输 $5.76\text{ GB}$，流水线尾部延迟 $4.2\text{ ms}$）**：
  $$T_{\text{Mode0}} = \frac{9.5 / 1.65}{38.0} \times 1000 + 4.2 = \mathbf{155.7\text{ ms}}\quad (\text{时延削减 } 94.3\text{ ms / } -37.7\%,\ \text{等效逻辑带宽 } \mathbf{61.0\text{ GB/s}})$$
* **Mode 1（紧凑 FP8 压缩 $2.05\times$，网上传输 $4.63\text{ GB}$，流水线尾部延迟 $3.5\text{ ms}$）**：
  $$T_{\text{Mode1}} = \frac{9.5 / 2.05}{38.0} \times 1000 + 3.5 = \mathbf{125.5\text{ ms}}\quad (\text{时延削减 } 124.5\text{ ms / } -49.8\%,\ \text{等效逻辑带宽 } \mathbf{75.7\text{ GB/s}})$$

---

## 三、 第三方开源库选型评估与融合架构

为了确保在四周内高质量交付，系统坚持“不重复造轮子”原则，采用 **“开源工业级内核 + 鲲鹏 NEON/SVE 与 IOStash 定制胶水层”** 融合架构：

| 开源库 / 方案名称 | 开源协议与代码形态 | 核心算法与优势分析 | 存在短板与适配改造点 | 本项目（`M2-F02`）定位与采用策略 |
| :--- | :--- | :--- | :--- | :--- |
| **1. `C-Blosc2`**<br>(`Blosc/c-blosc2`) | BSD-3 协议<br>纯 C 高性能库 | 原生内置多线程分块引擎、**ARM NEON 加速的 `BLOSC_SHUFFLE`（字节重排）与 `BLOSC_BITSHUFFLE`**，集成 `BloscLZ`、`LZ4`、`Zstd` 后端，接口精简。 | 默认对重排后的所有字节流统一压缩，未针对 BF16 跳过不可压缩的低 8 位尾数平面，存在计算冗余。 | **【W1 基线参照与快速脚手架】**<br>首周编译接入 `C-Blosc2`，快速跑通全链路数据流作为对比基线。 |
| **2. `ZipNN` + `Zstd HUF`**<br>(`zipnn/zipnn` + `facebook/zstd`) | Apache-2.0 / BSD<br>C 语言核心实现 | 专门针对大模型 BF16/FP16 张量设计：默认以 **`256 KB` 为切片（拆为两个 `128 KB` 高低字节组）**，跳过高熵尾数位，仅对低熵指数位调用 `libzstd` 底层 **`HUF_compress`（有限状态熵/霍夫曼）**，BF16 无损压缩率达 `33% ~ 45%`。 | 原生未针对鲲鹏 L3 `IOStash` 环形内存池与非临时写入指令（`stnp`）做零拷贝与微架构适配。 | **【Mode 0 无损压缩主力内核】**<br>抽取 `ZipNN` 的双平面分组逻辑与 `libzstd` 底层头文件，接入专属 `IOStash` + `stnp` 内核。 |
| **3. `ZipServ` Triple-Bitmap / 动态块 FP8**<br>(`arXiv:2603.17435`) | 开源算法原语<br>纯 SIMD 无分支实现 | 统计高频连续指数窗口（`BaseExp`）或按每 64 个元素共享 1 个 FP16 Scale 转为 **紧凑 `FP8 (E4M3)` / `BF12`（8位指数+高4位尾数）**，全程无分支（Branch-free），单核吞吐可达 `> 15 GB/s`。 | 属于低损量化模式（Perplexity 变化 `< 0.1%`），需提供独立配置开关与无损模式平滑切换。 | **【Mode 1 极速紧凑模式内核】**<br>用于长上下文（32K~100K）或网络拥塞场景，实现 **50% 体积缩减（`2.0x`）** 与最高线速。 |

---

## 四、 核心优化技术点与实现原理详解

### 4.1 技术点一：与描述符优化深度咬合的“紧凑连续装箱与双视图边界描述符”

* **产生背景**：
  未压缩前，每个 16-Token 的 KVC 切片大小严格固定（如 `256 KB`）。一旦经过无损位平面压缩（压缩比 `1.5x ~ 1.8x` 动态变化），切片在网络和存储池中的长度变为变长（如 `142 KB`、`161 KB`、`153 KB`）。若将变长压缩块单独作为 URMA/RDMA WQE 提交，将破坏 4KB/64KB 硬件页对齐并产生大量离散描述符。
* **双视图描述符架构实现**：
  1. **视图 A：网络与存储池紧凑描述符（`CompressedMegaFrameDescriptor`，面向跨网与落盘）**：
     将连续 $M=16$ 个原本各为 `256 KB`（共 `4.0 MB`）的 KVC 切片，经 SIMD 位平面压缩后紧凑拼接（Contiguous Packing）进同一个连续的 `2.4 MB` 预注册巨页帧中，帧头附加 64 字节（1 条 Cacheline）的微索引头 `CompactPackHeader`：
     ```cpp
     struct alignas(64) CompactPackHeader {
         uint32_t magic_and_mode;       // 0x4B564350 ('KVCP') | CodecMode (0:Lossless_BF16, 1:Compact_FP8, 2:BF12)
         uint32_t num_slices;           // 默认聚合 16 个 Slice (原始 4MB -> 压缩后 ~2.4MB)
         uint32_t total_compressed_bytes; // 整帧实际占用字节数 (用于单次 URMA/RDMA WQE 传输长度)
         uint32_t uncompressed_stride;  // 固定为 262144 (256 KB)，用于解压后定长写回边界池
         uint16_t slice_comp_sizes[16]; // 每个子切片压缩后的精确字节数 (单位: 4B 对齐)
         uint8_t  base_exponents[16];   // 每个子切片的基准指数 BaseExp (供 ZipServ/FP8 快速还原)
     };
     ```
     原本需要提交 16 个离散 WQE 的 `4.0 MB` 数据，收敛为 **1 个指向连续内存块的描述符**，网络传输字节数缩减至 `~2.4 MB`。
  2. **视图 B：HOST-GPU 边界定长描述符（`BoundaryKVDescriptor`）**：
     当巨页帧到达目标端并经 `IOStash` + SIMD 解压后，Worker 线程按照 `uncompressed_stride = 256 KB` 固定步长，将 16 个切片连续写入 **Host DDR 边界加速池（`4.0 MB` 连续巨页段）**，向 GPU/NPU 算子流递交标准的连续描述符，算子层保持标准连续对齐的张量视图。

---

### 4.2 技术点二：鲲鹏 ARM NEON/SVE 寄存器级位平面解交织与流式编解码内核

将 KVC 张量处理抽象为“输入一段 `256 KB` 的 `uint16_t` 数组，输出一段紧凑字节流”的高性能数据流算子。

#### （1）编码端（发送前）：NEON 位平面分离 + 高字节熵编码 + 紧凑拼接
利用 ARM64 NEON 的 `vuzp1q_u8`（抽偶数字节：低 8 位尾数）与 `vuzp2q_u8`（抽奇数字节：高 8 位符号与指数），在 1 个时钟周期内完成 16 个 BF16 元素的位平面分离：

```cpp
#include <arm_neon.h>
#include <zstd.h>
#include "huf.h"
#include "lz4.h"

// 鲲鹏 NEON 位平面分离 (256KB BF16 -> 128KB HighByte[Exp] + 128KB LowByte[Mantissa])
void kunpeng_bf16_bitplane_split_neon(const uint8_t* __restrict src_bf16,
                                      uint8_t* __restrict dst_exp_high,
                                      uint8_t* __restrict dst_man_low,
                                      size_t num_bf16_elements) {
    // 小端序下: src[2*i] 为低8位(尾数), src[2*i+1] 为高8位(符号+指数)
    size_t i = 0;
    for (; i + 64 <= num_bf16_elements; i += 64) {
        // 预取未来 512B 数据至 L1 Cache
        __builtin_prefetch(src_bf16 + (i + 256) * 2, 0, 3);

        uint8x16_t v0 = vld1q_u8(src_bf16 + (i + 0)  * 2);
        uint8x16_t v1 = vld1q_u8(src_bf16 + (i + 8)  * 2);
        uint8x16_t v2 = vld1q_u8(src_bf16 + (i + 16) * 2);
        uint8x16_t v3 = vld1q_u8(src_bf16 + (i + 24) * 2);
        uint8x16_t v4 = vld1q_u8(src_bf16 + (i + 32) * 2);
        uint8x16_t v5 = vld1q_u8(src_bf16 + (i + 40) * 2);
        uint8x16_t v6 = vld1q_u8(src_bf16 + (i + 48) * 2);
        uint8x16_t v7 = vld1q_u8(src_bf16 + (i + 56) * 2);

        // 硬件单周期解交织：分离低字节(尾数)与高字节(指数)
        uint8x16_t low0  = vuzp1q_u8(v0, v1);
        uint8x16_t high0 = vuzp2q_u8(v0, v1);
        uint8x16_t low1  = vuzp1q_u8(v2, v3);
        uint8x16_t high1 = vuzp2q_u8(v2, v3);
        uint8x16_t low2  = vuzp1q_u8(v4, v5);
        uint8x16_t high2 = vuzp2q_u8(v4, v5);
        uint8x16_t low3  = vuzp1q_u8(v6, v7);
        uint8x16_t high3 = vuzp2q_u8(v6, v7);

        // 写入 L1/L2 Cache 临时工作区
        vst1q_u8(dst_man_low  + i + 0,  low0);
        vst1q_u8(dst_man_low  + i + 16, low1);
        vst1q_u8(dst_man_low  + i + 32, low2);
        vst1q_u8(dst_man_low  + i + 48, low3);

        vst1q_u8(dst_exp_high + i + 0,  high0);
        vst1q_u8(dst_exp_high + i + 16, high1);
        vst1q_u8(dst_exp_high + i + 32, high2);
        vst1q_u8(dst_exp_high + i + 48, high3);
    }
}
```
分离完成后，仅对 `dst_exp_high`（`128 KB`）调用 `HUF_compress()` 或 `LZ4_compress_fast()`，并与 `dst_man_low` 拼接装入巨页帧中。

#### （2）解码端（接收后）：L3 IOStash 直读解压 + NEON zip 重交织 + stnp 流式写回
接收端从 L3 Cache 直接读取压缩指数流并解压，与 L3 中的尾数流通过 `vzip1q_u8 / vzip2q_u8` 重交织，使用 `stnp` 指令穿透写回 Host DDR 边界池：

```cpp
// 鲲鹏 NEON 位平面重交织 + stnp 非临时流式写回 (绕过 L3 直达 DDR)
void kunpeng_bf16_unshuffle_and_stream_store(const uint8_t* __restrict src_exp_high_l2,
                                             const uint8_t* __restrict src_man_low_l3,
                                             uint8_t* __restrict dst_ddr_boundary,
                                             size_t num_bf16_elements) {
    for (size_t i = 0; i + 32 <= num_bf16_elements; i += 32) {
        // 从 L2/L3 加载指数与尾数
        uint8x16_t low0  = vld1q_u8(src_man_low_l3  + i);
        uint8x16_t high0 = vld1q_u8(src_exp_high_l2 + i);
        uint8x16_t low1  = vld1q_u8(src_man_low_l3  + i + 16);
        uint8x16_t high1 = vld1q_u8(src_exp_high_l2 + i + 16);

        // 交织还原为标准小端 BF16
        uint8x16_t out0 = vzip1q_u8(low0, high0);
        uint8x16_t out1 = vzip2q_u8(low0, high0);
        uint8x16_t out2 = vzip1q_u8(low1, high1);
        uint8x16_t out3 = vzip2q_u8(low1, high1);

        // 使用 ARMv8 stnp 绕过 L3 Cache 直写 Host DDR 边界池
        uint8_t* out_ptr = dst_ddr_boundary + i * 2;
        asm volatile(
            "stnp %q0, %q1, [%4, #0]\n\t"
            "stnp %q2, %q3, [%4, #32]\n\t"
            :
            : "w"(out0), "w"(out1), "w"(out2), "w"(out3), "r"(out_ptr)
            : "memory"
        );
    }
    // 刷入内存通道，对随后的 GPU/NPU DMA 操作立即可见
    asm volatile("dmb ishst" ::: "memory");
}
```

---

### 4.3 技术点三：基于 ByteExpress 范式与 IOStash 的信用门控 L3 驻留环形流水线

参考大规模分布式训练与推理 RDMA 通信架构（`ByteExpress`）中的无锁环形队列设计，结合鲲鹏 920 硬件特性构建 **信用门控 L3 Stash 流水线**：

1. **硬件层配置（开启鲲鹏 SMMUv3 IOStash 与 MPAM L3 锁路）**：
   * **MPAM L3 分区保护**：为每个 SCCL 的编解码工作线程组划分专属的 **6 MB ~ 8 MB L3 Cache 掩码（`cpbm = 0x0fc0`）**，防止后台换出流量冲刷热区；
   * **靶向 IOStash 映射**：在注册环形内存池（每 SCCL 分配 `32 Slots × 160 KB = 5.12 MB` 的 2MB 巨页对齐内存）时，通过 SMMUv3 流表项（STE `ALLOCHINT=1` 与 PCIe Steering Tag）开启写分配缓存直注。
2. **软件层流控（信用门控防止 L3 溢出）**：
   * 每个 SCCL 绑定 2 个专职空闲物理核，维护深度为 32 的无锁环形槽位池；
   * 远端拉取时，仅将获取了槽位信用的请求投递至对应的环形槽位；
   * 完成事件到达后，本地 Worker 从 L3 读出数据，解压并通过 `stnp` 写回 DDR 边界池，随后立即原子归还信用，复用相同 L3 物理地址。

---

## 五、 性能测试场景构造方法（四级正交验证体系）

为保证方案演进具备客观实测数据支撑，测试场景按四级递进构造：

### 5.1 场景一（L0 算子与开源库微基准）：真实 KVC 张量语料下的压缩比、吞吐与精度验证
* **测试语料构造准则（杜绝全零与随机数失真）**：
  编写 `dump_real_kvcache_corpus.py`，在推理框架运行 `Qwen2.5-72B` / `Llama-3.1-8B` 处理 SWE-bench 代码 Prompt（8K / 32K 上下文）时，抓取不同网络层的真实 `BF16` 张量保存为 `4 GB` 标准测试集。
* **正交对比组构造（`bench_kv_codec_micro.cpp`）**：
  在鲲鹏 920 上分别配置 1、2、4、8 核，对比以下分支：
  1. `Baseline-Memcpy`：纯内存拷贝基线；
  2. `OpenSource-LZ4 / Zstd-L1`：未做位平面分离的原生通用压缩；
  3. `OpenSource-Blosc2`：调用 `C-Blosc2`（`BLOSC_SHUFFLE` + `BLOSC_BLOSCLZ/LZ4`）；
  4. `Ours-Mode0 (Lossless Bit-Plane)`：位平面分离 + 高字节熵编码 + `stnp` 流式写回（校验解压后与原张量完全一致，`memcmp == 0`）；
  5. `Ours-Mode1 (Compact Group-FP8)`：每 64 元素共享 Scale 的向量化 FP8 紧凑转换。

### 5.2 场景二（L1 微架构与 IOStash 专项验证）：基于鲲鹏 Uncore PMU 的缓存命中与内存放大比对账
* **测试方法**：
  在双命名空间环回环境中，通过底层遥测守护进程采集硬件计数器：
  * 指标包含：DDRC 读写流量（`hisi_scclX_ddrcY/flux_rd, flux_wr`）、L3 Cache 读命中率（`hisi_scclX_l3cY/rd_cpipe, rd_hit_cpipe`）以及 CPU 的 IPC 与访存停顿周期；
* **三组消融对照**：
  * **组 A（无 IOStash + 普通 memcpy 写回）**：基线对照，观察 DDRC 流量放大比与 L3 未命中情况；
  * **组 B（无流控全窗口 IOStash）**：观察在途数据过大导致的 L3 颠簸现象；
  * **组 C（开启 32-Slot 信用门控 IOStash + MPAM 锁路 + stnp 非临时写回）**：验证 L3 读命中率提升至 **`> 92%`**，DDRC 放大比降至 **`≤ 2.05x`**，单核解压吞吐提升 **`≥ 1.45x`**。

### 5.3 场景三（L2 传输底座与描述符优化验证）：逻辑有效带宽突破物理线速验证
* **测试方法**：
  在底层传输组件开启位平面编解码与聚合描述符：
  * 固定单张 400G 网卡（物理线速上限为 `38 ~ 42 GB/s`）；
  * 传输 `10 GB` 真实 KVC 语料，测量从发起拉取到边界池解压就绪的端到端耗时，计算等效逻辑带宽：
    $$\text{Logical KVC Bandwidth (GB/s)} = \frac{\text{Raw Uncompressed KVC Size (10.0 GB)}}{T_{\text{wall}} (\text{s})}$$
  * 统计描述符数量，验证通过 16 合 1 紧凑装箱后 WQE 提交量削减 12 倍以上。

### 5.4 场景四（L3 在线推理端到端业务验证）：SWE-bench 长上下文 TTFT、总吞吐与存储池容量倍增对账
* **测试方法**：
  在多机集群启动推理框架并加载分布式存储连接器，运行 SWE-bench 长上下文多轮对话负载：
  1. **实验 4A（跨节点拉取端到端 TTFT 对账）**：对比原始满速传输与开启流式位平面压缩后的 TTFT 降幅；
  2. **实验 4B（固定 Host 内存池配额下的容量与总吞吐对账）**：将 Host 存储池配额限制为 `64 GB`，注入并发 32K 请求（原始需求约 `100 GB`）：
     * 无压缩基线：触发内存溢出、换出与重算，TTFT 劣化；
     * 开启紧凑池：工作集压缩至 `50 ~ 60 GB`，完整保留在高速内存中，测试系统总吞吐（QPS）提升幅度。

---

## 六、 量化验收门限对账表与四周（W1~W4）实施路线图

### 6.1 四级验收标准与预期成果对账表

| 验收层级 | 考核核心维度 | 优化前 / 原生基线表现 | 预期达标门限 (PASS) | 挑战目标 (Challenge) |
| :---: | :--- | :--- | :---: | :---: |
| **L0 算子级** | 真实 KVC 压缩比与 8 核 SIMD 吞吐 | 原生 LZ4: 压缩比 `1.06x`, 吞吐 `18 GB/s` | **Mode 0 (无损): `≥ 1.55x`, 0 误差**<br>**Mode 1 (FP8): `≥ 1.95x`**<br>8 核解压吞吐 **`≥ 45 GB/s`** | Mode 0 达 `1.70x`<br>8 核解压 **`≥ 80 GB/s`** |
| **L1 微架构级** | `IOStash` L3 命中率与 DDRC 内存放大比 | 无 IOStash: L3 读命中 `< 15%`, DDRC 放大比 **`3.20x`** | 开启 `IOStash + stnp`:<br>L3 读命中 **`≥ 88%`**, DDRC 放大比 **`≤ 2.15x`**<br>单核解压提速 **`≥ 1.40x`** | DDRC 放大比 **`≤ 2.02x`**<br>单核提速 **`≥ 1.65x`** |
| **L2 底座级** | 单口 400G 下的**等效逻辑 KVC 带宽**与描述符数 | 物理线速 `38.0 GB/s`, 256KB 切片需 `40,960` 个 WQE/10GB | **等效逻辑带宽 `≥ 58.0 GB/s` (Mode 0)**<br>**等效逻辑带宽 `≥ 70.0 GB/s` (Mode 1)**<br>紧凑装箱后 WQE 描述符数削减 **`≥ 12x`** | 逻辑带宽 **`≥ 75.0 GB/s`**<br>描述符数削减 **`16x`** |
| **L3 业务 E2E 级** | 32K~100K 端到端 TTFT、Host 池有效容量与系统总吞吐 (QPS) | 32K 跨网拉取耗时 `~250ms`；`64GB` 池在 `100GB` 工作集下严重溢出 | • **32K+ 端到端 TTFT 独立降低 `15% ~ 25%`**<br>• **Host 内存池有效容量扩大 `1.6x ~ 2.0x`**<br>• **容量受限工况下系统总吞吐提升 `≥ 20%`** | E2E TTFT 降低 **`> 28%`**<br>总吞吐提升 **`> 35%`** |

---

### 6.2 开发人员与 AI Agent 协作的四周（W1~W4）实施路线图

1. **W1（11.02 ~ 11.08）：开源库接入与鲲鹏 NEON 位平面 + stnp 内核破局**
   * **Day 1~2**：编写 `dump_real_kvcache_corpus.py` 导出真实 BF16 KVC 测试集；编译接入开源 `C-Blosc2` 与 `ZipNN/libzstd`，测出 L0 基线数据；
   * **Day 3~5**：实现 `kunpeng_bf16_bitplane_split_neon`（`vuzp1q_u8/vuzp2q_u8`）与 `kunpeng_bf16_unshuffle_and_stream_store`（`vzip1q_u8/vzip2q_u8` + `stnp` 非临时存储），在 8 核鲲鹏上达成 `> 45 GB/s` 解压吞吐。
2. **W2（11.09 ~ 11.15）：CompactPackHeader 聚合描述符与 IOStash 环形流水线打通**
   * **Day 6~9**：实现 `CompactPackHeader`（16 个切片紧凑装箱为 1 个巨页帧描述符），嵌入传输数据面管道，与底层传输和描述符编译接口对齐；
   * **Day 10~12**：配置鲲鹏 MPAM L3 专属路掩码（`cpbm = 0x0fc0`）与 SMMUv3 `IOStash` 32-Slot 信用门控环形窗，利用底层遥测守护进程验证 DDRC 流量放大比降至 `~2.0x`。
3. **W3（11.16 ~ 11.22）：Mode 1 动态 FP8 紧凑池扩展与框架边界联调**
   * 实现 Mode 1（动态块缩放 FP8 模式），对接边界描述符与按层拉取流，跑通基准测试，实测等效逻辑带宽达到 `60 ~ 75 GB/s`。
4. **W4（11.23 ~ 11.29）：L3 业务级消融实验与成果归档**
   * 在集群上完成长上下文端到端 TTFT 消融与容量扩展总吞吐对账，整理对比数据并输出验证报告。

---

## 七、 附录：工程代码初始化脚手架与自动化提示词规范

可将以下工程脚手架规范递交编码 Agent，用于快速生成基础代码目录与测试桩：

```text
请基于《基于鲲鹏 SIMD 与 IOStash 的 KVC 边界流式位平面压缩与紧凑池：优化与验证详细方案设计 (V1.0)》，在工作区 `mooncake-transfer-engine/src/codec/` 与 `benchmarks/kv_codec/` 目录下生成以下工程骨架文件：
1. `include/kv_bitplane_codec.h`：定义 64 字节对齐的 `CompactPackHeader` 聚合描述符结构体、`CreditGatedStashRing` 32 槽位信用门控环形池接口，以及 Mode 0（BF16 无损位平面）与 Mode 1（动态块 FP8）编解码 API；
2. `src/kunpeng_neon_bitplane.cpp`：完整实现基于 ARM64 NEON `vuzp1q_u8/vuzp2q_u8` 的位平面分离函数、调用 `libzstd` (`HUF_compress`/`LZ4_compress_fast`) 的高字节编码逻辑、以及基于 `vzip1q_u8/vzip2q_u8` + 内联汇编 `stnp`（Non-Temporal Store Pair）的零 L3 污染流式解压写回函数；
3. `benchmarks/kv_codec/bench_kv_codec_micro.cpp` 与 `CMakeLists.txt`：构造符合真实 BF16 KVC 指数分布（高字节低熵 ~2.5 bits/byte、低字节高熵 ~7.8 bits/byte）的基准测试器，支持一键对比 `Memcpy`、`Raw-LZ4`、`C-Blosc2`、`Ours-Mode0-Lossless` 与 `Ours-Mode1-FP8` 在 1~8 核鲲鹏下的压缩比、吞吐（GB/s）与 Bit-Exact 一致性校验。
```
