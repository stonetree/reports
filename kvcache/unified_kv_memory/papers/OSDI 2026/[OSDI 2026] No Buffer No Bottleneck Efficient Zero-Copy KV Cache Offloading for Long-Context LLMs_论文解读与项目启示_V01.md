# DirectKV：面向长上下文推理的无暂存缓冲 KVCache 卸载

> 论文：*No Buffer, No Bottleneck: Efficient Zero-Copy KV Cache Offloading for Long-Context LLMs*  
> 作者：Shutian Luo、Haiying Shen（University of Virginia）  
> 会议：20th USENIX Symposium on Operating Systems Design and Implementation（OSDI 2026）  
> 解构版本：V01  
> 分析范围：论文正文、图表、算法与 Discussion；论文事实以原 PDF 为准。项目映射只使用本项目受控架构与验证基线。目标目录中的既有同名报告仅用于确定 V01 文件名，未读取，也未作为本报告的内容来源。

本文把证据分为四类：论文事实、论文实测、分析判断和项目建议。作者对系统能力的描述若没有对应实验，按“作者主张”处理，不等同于已经测量。

## 1. 一页读懂论文

### 1.1 论文解决什么问题？

长上下文大模型推理会产生很大的 KVCache（大模型注意力键值缓存，用于保存历史 Key/Value 激活状态，避免后续 Token 重复计算注意力）。把 KVCache 放在 GPU 高带宽显存（HBM）中，容量很快不够；把它卸载到 CPU 内存，现有系统通常又要先把数据搬回 HBM 暂存区，额外占用显存，并在 CPU 与 GPU 之间产生反复搬运。

DirectKV 试图在 NVIDIA GH200 这类 CPU-GPU 高带宽互联平台上，让 GPU 注意力内核直接读取 CPU 固定页内存中的 KVCache，不再为每次注意力计算准备 HBM 暂存副本，同时尽量把性能拉回接近 HBM 常驻路径的水平。

### 1.2 根因是什么？

表面问题是 CPU 内存带宽比 HBM 低。更深一层的根因是：现有矩阵乘法和注意力内核默认所有操作数都在 HBM，会按适合对称高速内存的顺序反复取数。一旦 KV 在 CPU 内存，相同 KV tile 会跨 CPU-GPU 链路重复读取，有限的 L2 命中率又放大了远端流量。朴素零拷贝因此省了显存，却把互联带宽暴露成新的主瓶颈。

论文的矩阵乘法动机实验很直白：在 PCIe 上，朴素零拷贝从 56 ms 增至 1122 ms；即使换成 NVLink-C2C（NVIDIA 芯片间高带宽一致性互联），也从 52 ms 增至 106 ms。与此同时，它确实减少了 HBM 占用。这说明“能直读”与“直读得快”是两件事。

### 1.3 核心洞察是什么？

KVCache 的远端访问是大块并发流量，性能主要受持续带宽影响，而不是单次 load 延迟。既然 HBM 带宽远高于 CPU-GPU 互联带宽，就应该让 CPU 内存中的 KV tile 成为片上共享内存里的驻留对象，把允许重复的访问转移到 HBM和片上存储，而不是反复跨互联读取 KV。

换句话说，DirectKV 没有消除 CPU-GPU 与 HBM 的带宽差；它改变访问顺序，让较慢链路上的数据尽可能只过一次。

### 1.4 核心抽象是什么？

核心抽象是“CPU 固定页内存中的设备可见 KVCache + CPU 内存感知注意力内核”。KVCache Manager 用 `cudaHostAlloc`（CUDA 固定页内存分配接口）分配固定页内存，向 GPU 提供设备可见指针；内核把 CPU 内存视为可直接取数的 KV 层级，而不是必须先复制到 HBM 的外部存储。

这个抽象把“KV 放在哪里”和“注意力如何消费 KV”连接起来。普通服务框架仍可负责连续批处理、前缀复用、淘汰与分层管理，DirectKV 只提供 CPU 驻留 KV 的执行路径。论文没有提出新的跨请求目录、租约协议或分布式对象语义。

![交换式卸载与零拷贝路径](<[OSDI 2026] No Buffer No Bottleneck Efficient Zero-Copy KV Cache Offloading for Long-Context LLMs_论文解读与项目启示_V01_assets/fig01_offloading_paths.png>)

*原文 Fig. 1，论文页 41。红色路径需要 HBM 暂存副本；绿色路径由 GPU 内核直接访问 CPU 内存。*

### 1.5 关键优化方法速览

#### 方法 1：固定页 KV 直接访问并取消 HBM 暂存

- **问题**：交换式卸载需要 HBM 暂存区，还会产生 KV swap-in/swap-out 流量。
- **方法**：把 KV 放在 CPU 固定页内存中，GPU 注意力内核通过设备可见指针直接读取，新增 KV 也直接写入 CPU 内存。
- **效果**：取消 HBM KV 暂存区，减少显存占用，并避免显式复制操作；但朴素实现会明显变慢，不能单独构成高性能方案。
- **关键证据**：Fig. 2 的矩阵乘法实验显示朴素零拷贝在 NVLink-C2C 上仍约慢 2 倍，在 PCIe 上慢 20 倍以上；其价值首先是容量，不是性能。

#### 方法 2：KV 驻留的 CPU 内存感知分块

- **问题**：普通分块顺序会让 CPU 驻留操作数随三重循环反复跨互联读取。
- **方法**：把 CPU 端 KV tile 固定在共享内存中，遍历 GPU 端的查询和中间输出；以更多 HBM 读写换取更少 CPU-GPU 读取。
- **效果**：将瓶颈从 CPU-GPU 互联转移到带宽更高的 HBM，恢复 L2 局部性。
- **关键证据**：矩阵实验中延迟从 106 ms 降至 54 ms，L2 命中率从 32.3% 回升至 75.1%；模型级实验报告 CPU-GPU 流量最多减少 50%，单 Token 延迟最多降低 70%。

#### 方法 3：Warp 级取数与计算流水

- **问题**：即使跨互联流量已经减少，串行的“取 tile、同步、计算”仍会让计算单元等待内存。
- **方法**：把 Warp Group（由多个 GPU Warp 组成、可分工执行取数或计算的线程组）分成 producer、consumer，预填充阶段另设 storer；取下一块、计算当前块和必要写回并行执行。
- **效果**：数据量基本不变，但取数等待被计算覆盖，内存吞吐率提高。
- **关键证据**：简化矩阵实验中数据量近似不变，HBM 吞吐率从 0.3 TB/s 提升到 1.3 TB/s，延迟从 54 ms 降至 48 ms。

#### 方法 4：KV 投影与注意力融合

- **问题**：分离内核先生成 K/V、写回 CPU 内存，随后的注意力内核又立刻把它们读回来。
- **方法**：在一个 CUDA 内核中完成 K/V 投影与注意力，生成的 K/V tile 留在共享内存中直接参与注意力，同时由写回 Warp 追加到 CPU 端 KVCache。
- **效果**：消除“刚写回、又读回”的往返，减少依赖等待和内核边界开销。
- **关键证据**：论文的简化实验将 85 ms 降至 57 ms，即约 1.49 倍加速；模型级 Fig. 13 报告 2.5～3.0 倍的延迟比和最高 3.5 倍 HBM 吞吐率，但缺少完整的联合消融来隔离所有变量。

### 1.6 最重要的实验结果是什么？

1. **设计不变量得到直接验证**：只把地址改为 CPU 内存并不可行。Fig. 2 显示朴素零拷贝在 NVLink-C2C 上仍从 52 ms 增至 106 ms；Fig. 3 进一步显示 L2 命中率从约 77% 降至 32.3%。这组结果支撑了“必须重排访问，而不是只改内存位置”的设计前提。
2. **CPU 内存感知分块确实改变了资源流向**：Fig. 4/5 的矩阵实验把 CPU 端 B tile 流量从 33.5 GB 降到 0.4 GB，代价是增加 HBM 侧中间结果流量；延迟基本恢复到 HBM 基线水平。
3. **端到端容量收益明显**：论文正文报告 DirectKV 平均使用 47 GB GPU 显存，比三个卸载基线平均少约 35 GB，对应 43% 降幅；在 32K 上下文下，Neo、Pie 和 SGLang 出现 OOM，DirectKV 与 FlexGen 仍可运行。
4. **端到端性能提升有边界**：作者报告相对卸载系统平均 1.2 倍加速，16K 上下文下约比 Neo/Pie 快 1.3 倍、比 FlexGen 快 1.7 倍。相较摘要中的“up to 1.2×”，正文同时给出更高的特定点位比值，两者口径并不相同。
5. **高带宽互联是必要条件**：512 Token 实验中，NVLink-C2C 相对 PCIe 最多降低 4.2 倍注意力延迟。PCIe 平台上，DirectKV 更接近容量扩展方案，性能收益受链路带宽限制。

### 1.7 最大局限是什么？

论文只在单机 GH200 上做主要评测，PCIe 对照使用另一台 H100 系统；没有跨节点 Direct-View、RDMA、远端故障或多租户混压实验。32K 是主要长上下文上限，未覆盖引言讨论的 128K。请求时间戳由 Poisson 过程合成，结果以每 Token 延迟和平均显存为主，缺少生产环境 P99、错误率与服务目标达标率。

更重要的是，论文把 KV 放在 Host DRAM 中。CPU 不执行正文拷贝，但正文仍经过并常驻 Host DDR。这能支持“Host CPU 不搬正文”的原则，不能证明本项目更严格的“绕过主机内存”路径已经成立。

### 1.8 为什么与本项目相关？

- **必要性证据**：论文独立确认 HBM 容量和跨层反复搬运会同时限制长上下文推理，单纯增加 CPU 容量并不能解决性能问题。
- **可行性证据**：在高带宽 C2C 链路上，通过片上缓存复用、访问顺序重排和内核流水，设备可以直接消费 Host 侧 KV，而不需要完整 HBM 副本。
- **差异化证据**：论文只解决单节点、固定页 CPU 内存和 CUDA 内核问题；本项目仍需处理国产 NPU、URMA/UBMEM、跨节点租约、语义一致性、多卡状态同步、NVMe 分层和前后台 QoS。
- **剩余举证责任**：论文不能证明本项目的 TTFT（Time To First Token，首字生成延迟）、QPS（Queries Per Second，每秒处理的请求数）和 TPOT（Time Per Output Token，每个输出 Token 的生成耗时）目标，也不能证明 Direct-View（远端直读，不生成完整本地显存副本）在 Decode 阶段必然优于或劣于 Copy-to-HBM（通过 DMA 将 KV 搬到本地显存）。它反而要求 PVT-03 加入“经过内核重排的 Direct-View”对照，避免用朴素远读提前判死刑。

## 2. 为什么这个问题值得解决

KVCache 容量随上下文长度线性增长。论文指出，十亿参数级模型在 128K Token 场景中，KVCache 可能达到数百 GB，已经超过单卡 HBM。把 KVCache 完全留在 HBM，会压缩批量规模并导致长上下文请求 OOM；把它搬到 CPU 或 SSD，又会引入带宽差和暂存区。

现有路径各自解决了一半问题：

- HBM 常驻性能最好，但受容量上限约束。
- Swap 路径用 CPU 内存扩容，仍需 HBM 暂存，并可能对同一层执行 swap-in 和 swap-out，链路流量和显存占用都存在。
- CPU 参与注意力可少搬数据，却放弃 GPU 的计算能力。
- 朴素零拷贝取消了暂存区，但内核访问模式没有变，跨互联的重复读取把性能打穿。

![朴素零拷贝的容量与延迟权衡](<[OSDI 2026] No Buffer No Bottleneck Efficient Zero-Copy KV Cache Offloading for Long-Context LLMs_论文解读与项目启示_V01_assets/fig02_naive_zero_copy_tradeoff.png>)

*原文 Fig. 2，论文页 41。矩阵规模为 10240×10240，每个矩阵约 400 MB。该图是动机微基准，不是 LLM 端到端结果。*

DirectKV 成立依赖两个观察。第一，长上下文注意力每个 SM 一次读取约 100 KB KV tile，100 多个 SM 并发后形成 MB 级聚合流量，因此持续带宽比单次访问延迟更重要。第二，GH200/GB200 一类平台提供高带宽 C2C 互联，链路虽慢于 HBM，却已经足以通过软件重排来隐藏部分差距。没有第二个条件，零拷贝主要只能换容量。

从反证角度看，Fig. 2/3 排除了一个简单解释：性能下降并不只是固定远端访问延迟。数据被重复取回、L2 命中率下降和互联带宽受限共同出现，说明问题是访问放大和存储层级错配。

## 3. 核心抽象与系统设计

DirectKV 把系统分成四个部件：离线 Kernel Generator、在线 Kernel Adaptor、Attention Fusion Engine 和 KV Cache Manager。生成器预编译不同数据类型、头维度、tile 大小及 Prefill/Decode 模式的内核；适配器在运行时选取候选；融合引擎执行投影与注意力；KV 管理器维护 CPU 固定页内存中的 KV。

![DirectKV 系统架构](<[OSDI 2026] No Buffer No Bottleneck Efficient Zero-Copy KV Cache Offloading for Long-Context LLMs_论文解读与项目启示_V01_assets/fig08_directkv_architecture.png>)

*原文 Fig. 8，论文页 44。图中的迭代 1 为 Prefill，后续迭代进入 Decode。*

### 3.1 控制面与数据面

Kernel Generator 和 Kernel Adaptor 属于执行选择控制面。它们决定运行哪个编译版本，但不管理跨请求复用、目录查询或跨节点放置。KV Cache Manager 与融合内核构成数据面：前者提供固定页地址和生命周期，后者直接发起读写。

这个边界很重要。论文在 Discussion 中明确表示，DirectKV 不替代 vLLM 或 SGLang 的调度、批处理和缓存管理，只为“KV 已经位于 CPU 内存”这一状态提供高效执行路径。因此，论文不能用来证明查询与放置计划决策引擎、缓存淘汰或多级介质策略有效。

### 3.2 关键设计不变量

DirectKV 依赖以下关系：

1. CPU-GPU 链路必须提供足够高的持续带宽，且并发大块访问能够接近该带宽。
2. 一个从 CPU 取回的 KV tile 必须在 SMEM（GPU 流式多处理器内由软件管理的片上共享内存）中被充分复用，否则访问放大会重新压垮链路。
3. 额外产生的 HBM 中间结果流量不能把新瓶颈推到 HBM；论文在 GH200 上观察到 HBM 有余量，但没有给出跨硬件的阈值模型。
4. KV 块不由 CPU 并发更新。作者据此说明方案不依赖统一页表的一致性机制，只需要固定页和设备可见指针。
5. SMEM 容量足以容纳双缓冲和投影/注意力 tile。论文默认把统一 L1/SMEM 池的 80% 留给 SMEM，并使用两级流水；这属于实现参数，不是跨平台常数。

### 3.3 Prefill 与 Decode 的不同循环顺序

Prefill 阶段的 Query 很长，CPU 端 KV 取回成本高。内核把 KV tile 驻留在 SMEM 中，遍历 HBM 中的 Q 和流式 Softmax 状态。Decode 阶段只有一个新 Query，内核改为遍历历史 K/V，让输出保存在寄存器中，避免反复读写中间结果。

同一个“让慢层级数据驻留”的原则，在两个阶段采取不同循环顺序。论文没有把 Prefill 和 Decode 粗暴合并成一套 tile 规则，这是其内核设计中最扎实的一点。

## 4. 关键技术实现与作用机理

### 4.1 固定页 KV 直接访问并取消 HBM 暂存

#### 解决的问题

交换式卸载把 CPU 内存当作后备存储。每层注意力开始前，KV 要复制进 HBM 暂存区；之后还可能换出。暂存区占用本来就紧张的显存，动态复制地址也可能迫使 CUDA Graph 更新或重新捕获。

#### 核心方法

KV Cache Manager 用 `cudaHostAlloc` 分配固定页内存，GPU 通过设备可见指针直接读取历史 KV，并把新生成 KV 直接追加到 CPU 内存。

#### 实现路径

1. Prefill 生成初始 K/V，融合内核通过零拷贝写入 CPU 固定页。
2. Decode 时，内核从 CPU 内存分块读取历史 K/V 到 SMEM。
3. 当前 Token 的新 K/V 写入同一 CPU 端缓存。
4. 输出注意力结果写回 HBM，供后续前馈层使用。

#### 资源 / 状态变化

`CPU KV → HBM 暂存 → SMEM` 变为 `CPU 固定页 KV → SMEM`。

HBM 中的完整 KV 暂存副本被取消，但 CPU 内存成为在线数据层级，Host DDR 容量、固定页配额和 C2C 带宽都进入运行时约束。

#### 为什么有效

它删除了显式 swap-in/swap-out 与 HBM 暂存区，直接释放容量，并减少同一 KV 在层级之间的完整复制。它本身不解决慢链路上的重复读取，所以必须与 4.2～4.4 共同使用。

#### 达到的优化效果

**直接效果**：论文正文报告 DirectKV 平均 GPU 显存占用 47 GB，三个卸载基线分别为 86、88 和 74 GB，平均减少约 35 GB。

**端到端效果**：32K 上下文下，DirectKV 仍可运行；Neo、Pie 和 SGLang OOM。该结果同时受到模型参数、批量、上下文和各系统缓冲策略影响，不能仅归因于固定页内存。

#### 论文证据

Fig. 2 证明零拷贝能省暂存区，但朴素实现很慢；Fig. 11 展示上下文长度与内存容量结果。两组证据共同说明“取消缓冲有容量价值，但高性能需要内核重构”。

![上下文长度、延迟与内存占用](<[OSDI 2026] No Buffer No Bottleneck Efficient Zero-Copy KV Cache Offloading for Long-Context LLMs_论文解读与项目启示_V01_assets/fig11_context_length_memory.png>)

*原文 Fig. 11，论文页 49。正文给出的绝对显存数字与图中柱高存在肉眼可见的不完全一致，复现时应以作者原始数据和脚本为准。*

#### 亮点

论文没有把零拷贝包装成免费收益。动机实验先展示其严重性能损失，再说明后续内核重构为什么必要，因果链完整。

#### 代价与局限

固定页会占用 Host DRAM，可能受到操作系统页锁定配额、NUMA 放置和内存压力影响。论文没有给出固定页池的回收、超卖、碎片或多租户隔离实验。数据仍在 Host DDR 中，不满足“正文绕过主机内存”。

#### 对项目的意义

**ADAPT**。可吸收“设备直接消费 Host 侧 KV、取消完整 HBM 暂存”的原则；不能照搬 `cudaHostAlloc`、CUDA 指针和 GH200 地址语义。项目必须在国产 NPU 上验证注册内存、设备可见性、DMA 完成语义和 Host Payload Touch Bytes。

### 4.2 KV 驻留的 CPU 内存感知分块

#### 解决的问题

普通 GEMM 分块保持输出 C 驻留，循环中重复加载 A/B。B 一旦位于 CPU 内存，这个原本合理的顺序会让 B 的远端流量随计算循环放大。论文测得朴素零拷贝 L2 命中率只有 32.3%，CPU 侧 B 流量达到 33.5 GB。

#### 核心方法

把 CPU 侧 KV tile 设为驻留对象，一次取入 SMEM 后跨多个 Query/输出 tile 复用；允许中间输出反复经过 HBM。

#### 实现路径

1. 从 CPU 内存取一个 K/V tile 到 SMEM。
2. 从 HBM 取对应 Q 或中间输出 tile。
3. 在 Tensor Core/寄存器上完成乘加与流式 Softmax 更新。
4. 把中间输出写回 HBM，再处理下一个 Q tile。
5. 当前 K/V tile 的所有复用完成后，再取下一块 CPU KV。

#### 资源 / 状态变化

`反复跨 C2C 读取 KV` → `KV 跨 C2C 读取一次 + 中间状态在 HBM 反复读写`。

较慢互联上的 33.5 GB B 流量降到 0.4 GB；代价是 HBM 侧读写增加到论文所列的 61.8 GB 与 33.9 GB。这里不是减少总字节，而是把字节搬到更快的层级。

#### 为什么有效

GH200 的 HBM 带宽约 4 TB/s，而 NVLink-C2C 双向标称 900 GB/s、单方向约 450 GB/s。只要 HBM 仍有余量，用更多 HBM 流量换更少 C2C 流量就是有利交易。

#### 达到的优化效果

**直接效果**：矩阵微基准延迟 106→54 ms，L2 命中率 32.3%→75.1%。

**端到端效果**：Fig. 12 报告三种模型的 CPU-GPU 流量最多减少 50%，每 Token 延迟最多降低 70%。论文没有说明 Fig. 12 是否完全固定了 Warp 流水和融合内核配置，因此“全部收益只来自分块”仍有混杂可能。

#### 论文证据

![CPU 内存感知分块恢复 L2 局部性](<[OSDI 2026] No Buffer No Bottleneck Efficient Zero-Copy KV Cache Offloading for Long-Context LLMs_论文解读与项目启示_V01_assets/fig05_cpu_aware_tiling_effect.png>)

*原文 Fig. 5，论文页 43。该图验证核心设计不变量：重排访问后，零拷贝延迟基本回到 HBM 基线，L2 命中率也接近基线。*

![CPU 内存感知零拷贝的模型级消融](<[OSDI 2026] No Buffer No Bottleneck Efficient Zero-Copy KV Cache Offloading for Long-Context LLMs_论文解读与项目启示_V01_assets/fig12_cpu_aware_ablation.png>)

*原文 Fig. 12，论文页 49。左图是 CPU-GPU 流量，右图是每 Token 延迟。*

#### 亮点

这项方法的价值不在“使用共享内存”，而在于把非对称层级的带宽价格写进循环顺序。资源转换能够被硬件计数器直接验证，也能迁移到其他异构加速器。

#### 代价与局限

收益依赖 HBM 有余量。如果模型权重、激活或其他任务已经把 HBM 带宽打满，额外的中间状态读写会形成新瓶颈。论文没有给出 HBM 饱和点、不同并发租户或不同 head dimension 下的阈值曲线。

#### 对项目的意义

**ADAPT**。应把“慢层级 KV 驻留，快层级中间状态流动”作为国产 NPU Direct-View 内核的候选模板。项目需要用 NPU 的片上 SRAM/L1、DMA 队列和矩阵计算单元重写，并把 HBM 带宽余量加入硬件能力矩阵和动态选路成本模型。

### 4.3 Warp 级取数与计算流水

#### 解决的问题

分块减少了远端字节，但每个 tile 若仍按“加载完成后再计算”的方式串行执行，计算单元会等待 C2C/HBM 取数。

#### 核心方法

使用 Warp Group 分工和双缓冲，让 producer 预取下一 tile，consumer 计算当前 tile，Prefill 的 storer 同时写回已完成 K/V。

#### 实现路径

1. Producer 通过 Hopper Tensor Memory Accelerator 异步搬入下一 tile。
2. Consumer 在当前 tile 上执行矩阵乘、RoPE 或流式 Softmax。
3. Prefill 中，Storer 把已生成 K/V 写入 CPU 内存。
4. 交换双缓冲区角色，推进到下一流水级。

#### 资源 / 状态变化

`取数等待 → 计算 → 写回` 变为 `取数(n+1) ∥ 计算(n) ∥ 写回(n-1)`。

#### 为什么有效

数据量没有下降，收益来自关键路径重叠。只要每一级耗时接近且缓冲区足够，互联和 HBM 等待可以被 Tensor Core 计算覆盖。

#### 达到的优化效果

**直接效果**：简化矩阵实验的数据量为 37.1 GB 与 37.3 GB，基本不变；HBM 吞吐率 0.3→1.3 TB/s，延迟 54→48 ms。

**端到端效果**：论文没有提供“关闭 Warp 流水”的完整 LLM 端到端消融，不能据此估算它对 1.2 倍整体加速的独立贡献。

#### 论文证据

原文 Fig. 6（论文页 43）是直接证据。它排除了“只是少搬了数据”这一替代解释，因为两组数据量几乎相同。

#### 亮点

作者用不变的数据量和变化的吞吐率做因果归因，比只报端到端加速更可信。

#### 代价与局限

流水依赖 Hopper Warp Group 和 TMA，默认两级流水还会增加 SMEM 缓冲需求。论文没有评估流水深度、寄存器压力、occupancy 或不同硬件代际的适配成本。

#### 对项目的意义

**VALIDATE**。原则可迁移，但能否复用取决于国产 NPU 是否提供独立 DMA/Load 单元、异步事件和可控片上双缓冲。先做算子级时间线，确认搬运与计算真的重叠，再进入系统路径。

### 4.4 KV 投影与注意力融合

#### 解决的问题

分离内核会把新生成 K/V 写入 CPU 内存，下一注意力内核随即再取回。数据往返和内核边界都位于当前 Token 的关键路径。

#### 核心方法

在一次 CUDA launch 中完成 K/V 投影、可选 RoPE、历史 KV 注意力以及新 KV 追加，当前 K/V tile 保留在 SMEM 中直接消费。

#### 实现路径

1. 从 HBM 读取输入 X 和投影权重，生成 K/V tile。
2. K tile 如有需要执行 RoPE（Rotary Position Embedding，旋转位置编码）。
3. K/V 一路写入 CPU 固定页，作为后续 Token 的历史缓存。
4. 同一份 K/V 留在 SMEM 中，立即参与当前注意力。
5. Prefill 对长 Q 逐块累计流式 Softmax；Decode 对历史 K/V 逐块扫描，当前输出留在寄存器中。

#### 资源 / 状态变化

`投影 → CPU 写回 → 注意力重新从 CPU 读取` 变为 `投影结果在 SMEM 内直接进入注意力 ∥ 后台追加 CPU KV`。

#### 为什么有效

当前 K/V 不再穿过 CPU-GPU 链路两次，投影和注意力之间也不需要全局内存发布后再读取。它同时减少数据依赖和 kernel launch 边界。

![分离内核与融合内核的数据路径](<[OSDI 2026] No Buffer No Bottleneck Efficient Zero-Copy KV Cache Offloading for Long-Context LLMs_论文解读与项目启示_V01_assets/fig09_projection_attention_fusion.png>)

*原文 Fig. 9，论文页 46。D2H 表示设备到 Host 的 KV 写回。融合路径仍写回 CPU，只是不再为当前注意力再次读取。*

#### 达到的优化效果

**直接效果**：简化实验的 HBM 吞吐率从 1.6 增至 1.9 TB/s，延迟从 85 ms 降至 57 ms。85/57≈1.49，因此论文所写“49% speedup”成立，但不能表述成“延迟降低 49%”；实际延迟降幅约 33%。

**端到端效果**：Fig. 13 报告模型级 HBM 吞吐率最高 3.5 倍、延迟比 2.5～3.0 倍。图中没有误差条和配置明细，不宜把这些比值外推到任意上下文或并发度。

#### 论文证据

![融合内核的吞吐与延迟对比](<[OSDI 2026] No Buffer No Bottleneck Efficient Zero-Copy KV Cache Offloading for Long-Context LLMs_论文解读与项目启示_V01_assets/fig13_fused_kernel_ablation.png>)

*原文 Fig. 13，论文页 50。比较 Separate Kernel 与 Fused Kernel，覆盖 Llama-8B、OPT-13B 和 OPT-30B。*

#### 亮点

融合不是单纯减少 launch 次数，而是删除了一次跨层级往返。这个差别决定了它能否在零拷贝路径中产生稳定收益。

#### 代价与局限

融合后内核更大，SMEM 和寄存器分配更紧，编译变体也更多。论文采用离线模板候选池和运行时选择，但没有给出候选数量、编译时长、二进制体积或选错内核的代价。数值等价性只以“保持完整注意力精度”陈述，没有单独的误差表。

#### 对项目的意义

**ADAPT**。项目可以把“生成 KV、发布到持久缓存、当前请求立即消费”合成同一设备侧流水，减少刚发布就回读。需要先明确框架算子边界、NPU 编译器支持、缓存发布完成语义和多卡可见性，不能只按 CUDA launch 结构照抄。

## 5. 实验到底证明了什么

| Claim | Experiment / Evidence | Conditions & Baseline | Result | What it proves | What it does not prove |
|---|---|---|---|---|---|
| 朴素零拷贝会暴露带宽与局部性问题 | Fig. 2/3，矩阵微基准 | 10240² 矩阵；H100 PCIe 与 GH200 NVLink-C2C；Baseline/Swap/Zero-Copy | PCIe 56→1122 ms；C2C 52→106 ms；L2 命中率约 77%→32.3% | 只改地址空间不能得到高性能零拷贝 | 不能代表完整注意力或端到端服务收益 |
| CPU 内存感知分块能减少慢链路访问放大 | Fig. 4/5 与 Algorithm 1 | GH200 矩阵微基准 | CPU 侧 B 流量 33.5→0.4 GB；延迟 106→54 ms | 访问重排把瓶颈从 C2C 转到 HBM | 不证明所有模型、维度和 HBM 压力下都成立 |
| CPU-aware 零拷贝在模型上仍有效 | Fig. 12 | Llama-8B、OPT-13B、OPT-30B；Naive vs CPU-aware | CPU-GPU 流量最多 -50%，延迟最多 -70% | 微基准机制能迁移到模型执行 | 未说明其他内核优化是否完全保持相同，归因不是完全隔离 |
| Warp 流水隐藏内存等待 | Fig. 6 | 简化矩阵；无流水 vs overlap | 数据量近似不变；0.3→1.3 TB/s；54→48 ms | 性能收益来自重叠，不是少搬数据 | 不证明端到端独立贡献或尾延迟收益 |
| 融合删除投影到注意力的往返 | Fig. 7/13 | 简化两核链及三种模型 | 85→57 ms；模型级报告 2.5～3.0 倍延迟比 | 融合路径显著改善吞吐和延迟 | 不证明收益全部来自 CPU 往返删除，也未给误差条 |
| DirectKV 在高负载下优于卸载基线 | Fig. 10 | ShareGPT/Alpaca；Poisson 到达；最高 30 req/s；GH200 | Llama-8B、OPT-13B 在 30 req/s 均约 0.75 s；卸载基线约 1.55～3.95 s | 在论文配置下，DirectKV 的负载扩展性更好 | 不证明 P99、生产到达过程或相同质量/压缩口径下的普适优势 |
| 长上下文下兼顾容量和延迟 | Fig. 11 | 1K～32K 上下文；96 GB HBM | 平均约 1.2 倍；16K 对 Neo/Pie 约 1.3 倍；32K 多个基线 OOM | 取消暂存区提高可服务上下文和容量 | OOM 结果不是同可行域性能对比；正文绝对显存数字与图柱高需核对 |
| 高带宽互联是性能前提 | Fig. 14 | 512 Token；H100 PCIe vs GH200 NVLink-C2C | 注意力延迟最多降低 4.2 倍 | 链路带宽对零拷贝性能有决定性影响 | 两台系统除互联外还有 CPU/内存差异；512 Token 不能代表全部长上下文 |

![不同请求率下的每 Token 延迟](<[OSDI 2026] No Buffer No Bottleneck Efficient Zero-Copy KV Cache Offloading for Long-Context LLMs_论文解读与项目启示_V01_assets/fig10_request_rate_latency.png>)

*原文 Fig. 10，论文页 48。SGLang 在可装入 HBM 时速度更快或接近，但在较大模型和高请求率下受容量限制。*

![PCIe 与 NVLink-C2C 敏感性](<[OSDI 2026] No Buffer No Bottleneck Efficient Zero-Copy KV Cache Offloading for Long-Context LLMs_论文解读与项目启示_V01_assets/fig14_interconnect_sensitivity.png>)

*原文 Fig. 14，论文页 50。序列长度为 512；该图更适合作为链路敏感性证据，不是长上下文主结果。*

### 5.1 对替代解释的检查

DirectKV 相对 Neo、Pie、FlexGen 的端到端优势不一定全部来自零拷贝。系统同时更换了注意力内核、分块方式、Warp 流水和投影融合，基线还采用不同的 CPU 计算、暂存或压缩策略。论文提供了若干组件实验，但没有完整的累加式端到端消融，因此各机制的独立贡献仍未闭合。

SGLang 的 OOM 说明 HBM 容量边界真实存在，但不能把 OOM 后的缺失点当作速度提升。FlexGen 使用 4-bit 压缩，质量和计算开销口径也与 DirectKV 不完全相同；论文没有给出这些配置的完整公平性表。

## 6. 论文的亮点与局限

### 6.1 零拷贝数据路径

**亮点**

把容量收益和性能代价分开验证。论文没有用“零拷贝”概念替代性能证据，而是先证明朴素实现失败，再给出修复路径。

**局限**

数据仍驻留 Host DDR，只是 CPU 核不执行复制。固定页压力、NUMA、页锁定配额和多租户内存隔离未评测。

### 6.2 非对称带宽分块

**亮点**

明确写出资源交换：减少 C2C 访问，增加 HBM 访问。Fig. 4/5 同时给字节、命中率和延迟，证据能对上机制。

**局限**

论文没有扫描 HBM 忙碌度、链路带宽、tile 大小和并发度的联合空间，也没有给出“何时 HBM 侧新增流量反噬”的临界点。

### 6.3 流水与融合

**亮点**

Warp 流水的实验保持数据量近似不变，融合实验展示删除往返后的吞吐变化，两者的因果解释比单一端到端速度数字更可靠。

**局限**

两项结果主要来自简化微基准或局部对比。缺少全系统逐项开关、尾延迟、occupancy、寄存器溢出和数值误差数据。

### 6.4 端到端容量—性能折中

**亮点**

评测覆盖三种模型、1K～32K 上下文和最高 30 req/s，能看到 HBM 常驻快但容易 OOM、CPU/SSD 卸载容量大但延迟高的实际边界。

**局限**

主要平台只有一台 GH200。请求时间由 Poisson 过程生成，未报告 P99、失败率、能耗、CPU 利用率或生产流量回放。正文的显存绝对值与 Fig. 11 柱高需要原始数据核对。

### 6.5 可集成性与硬件依赖

**亮点**

方案不要求 GH200 统一页表的一致性，依赖标准固定页和设备指针；与缓存管理策略的责任边界也写得较清楚。

**局限**

实际高性能依赖 NVLink-C2C、Hopper Warp Group、TMA、CUTLASS 和 FlashAttention-3。论文说可用于 GB200，但没有 GB200 实测；对非 CUDA 加速器只能迁移原则，不能直接复用实现。

## 7. 论文没有解决什么

1. **跨节点缓存池**：DirectKV 是节点内 CPU-GPU 内存层级优化。论文只说明 CPU 固定页可进一步通过 RDMA（Remote Direct Memory Access，远程直接内存访问）传输，没有测远端所有权、目录、路由、网络拥塞或接收端布局。
2. **生命周期与故障安全**：没有租约、撤销、远端崩溃、失效地址访问和加速器队列恢复协议。固定页对象何时可释放，也没有状态机分析。
3. **语义一致性**：没有模型、Tokenizer、提示词模板、LoRA、Ready 位和版本校验。论文假定传入的是正确 KV 地址。
4. **多卡一致动作**：Tensor Parallel 只在 Discussion 中说明每卡维护各自 head shard 和 CPU pool，没有测多卡命中差异、同步时延或故障分歧。
5. **分层存储与容量压力**：没有 NVMe SSD、可分页内存、冷数据回源、固定页池回收或 Host 内存耗尽实验。作者也承认回退到 pageable/disk tier 后，零拷贝收益会下降。
6. **生产 QoS**：没有前台推理与后台换入/换出混压，也没有 P99 TPOT 干扰、优先级队列或拥塞控制。
7. **Saved-Prefill（首字生成预计算节省，利用已缓存 KV 避免重复计算 Prompt）**：论文优化单请求长上下文的 KV 卸载与 Decode 执行，不验证跨请求前缀复用带来的 TTFT 收益。

### 7.1 内部一致性与复现问题

- §7.1 写 Python bindings 集成 PyTorch 2.2，§7.2 的评测环境又写 PyTorch 2.3。需要作者代码或环境清单确认正式版本。
- §7.3.3 正文给出 DirectKV/Neo/Pie/FlexGen 的 GPU 显存为 47/86/88/74 GB；Fig. 11(b) 的柱高肉眼并不完全对应这些数值。43% 降幅应在原始数据中重算。
- §2.4.3 的 85→57 ms 被写为“49% speedup”。按加速比计算为 1.49 倍，若按延迟降幅则约 33%；报告中应始终区分两种口径。
- Algorithm 1 对 NativeTiling 与 CpuAwareTiling 的渐进流量描述较粗，正文写到 C 流量从 `2·O(n³)` 增至 `3·O(n³)`，但没有逐项定义读、写和累加次数。该表达足以说明方向，不足以复算常数。

## 8. 对本项目的启发

### DIRECTLY ADOPT

1. **资源账要按层级拆开**：CPU-GPU/节点间链路字节、HBM 字节、片上缓存命中率和最终延迟必须同时采集。只看端到端 TTFT/TPOT 无法判断瓶颈是否真正转移。
2. **Direct-View 必须绑定消费内核**：远端可见地址只解决“能访问”，内核的 tile 驻留和循环顺序决定“是否值得访问”。QueryPlan（查询与放置计划决策引擎，用于选择加载、远端直读或重算路径）不能只读取链路带宽，还要读取内核是否支持慢层级感知模式。

### ADAPT

1. **NPU 版 CPU/远端内存感知分块**：把 CPU 或远端 KV tile 驻留在 NPU 片上 SRAM，允许 Q 与中间状态走 HBM。必须重新测片上容量、DMA 并发、HBM 余量和编译器调度。
2. **设备侧生产者—消费者流水**：用国产 NPU 的异步搬运队列和事件替代 Hopper Warp Group/TMA。验收依据是硬件时间线重叠率，不是接口名称相似。
3. **投影—注意力—缓存发布融合**：当前 Token 的 K/V 既要供本次注意力消费，又要发布为后续缓存。项目需把设备计算完成、缓存 Ready 和多卡可见性分开建模，避免把 kernel 完成等同于缓存已可安全消费。
4. **动态路径判断**：Direct-View（远端直读，不生成完整本地显存副本）与 Copy-to-HBM（通过 DMA 搬到本地显存）的成本模型应加入“内核访问放大系数”和“HBM 额外流量”，不能只比较一次网络读取与一次复制。

### DO NOT COPY

1. 不照搬 `cudaHostAlloc`、CUDA Graph、CUTLASS 模板池和 GH200 地址模型。这些是实现载体，不是项目接口契约。
2. 不把 CPU 固定页路径记成 Payload Bypass DDR（绕过主机内存的数据直达）。DirectKV 的正文数据明确驻留 Host DDR。
3. 不把单节点、无 CPU 并发写的假设扩展成跨节点视图一致性。项目仍需 ViewGuard（视图租约安全守卫机制，管理远端直读生命周期和故障回退）、多维语义校验和张量并行多卡状态同步。

### VALIDATE

1. 经过内核重排后，Decode Direct-View 是否仍然输给 Copy-to-HBM。DirectKV 说明这个结论依赖链路和内核，不能预设。
2. Host CPU 零数据拷贝是否成立：CPU 可负责元数据与下发，但 Host Payload Touch Bytes 必须为 0；同时单列 DDR DMA 字节，避免把“CPU 不复制”误写成“绕过 DDR”。
3. 片上缓存、HBM 与 UB/URMA 链路的真实带宽交叉点，以及 HBM 被前台算子占用后，慢层级感知分块是否仍有净收益。

## 9. 对本项目立项的背书

### 9.1 Problem Validation

论文以硬件实测确认两个项目问题是真实的：长上下文 KVCache 会把 HBM 容量推到 OOM；跨层卸载若沿用面向 HBM 的内核，会产生重复传输和局部性下降。这是单节点 GH200 上的实测证据，可支持本项目继续投入“统一异构 KVCache 存储与设备侧消费”方向，但不能直接扩展到跨节点 NPU 集群。

### 9.2 Mechanism Validation

本项目获得外部技术支持的不是某个 CUDA API，而是三条底层原则：

1. 设备可以直接消费 Host 侧 KV，而不要求完整 HBM 副本。
2. 非对称内存层级必须通过循环重排，让慢链路数据驻留并复用。
3. 传输、计算和缓存发布可以在设备侧流水或融合，减少关键路径等待。

这些原则为 Host CPU 零数据拷贝、Direct-View 候选路径和异步 DAG 流水提供可行性证据。它们尚未证明国产 NPU、UBMEM（统一总线内存直通共享协议）或 URMA（通用远程直接内存访问）具有相同的可见性和带宽。

### 9.3 Research-Gap Validation

论文没有解决而本项目明确要解决的部分包括：跨节点与多介质统一路径、Direct-View 租约故障安全、模型与词表等语义一致性、多卡状态同步、动态选路、NVMe SSD 扩容以及前后台混压 QoS。这些缺口与项目范围直接重合，构成差异化依据。

需要保持克制：论文没有做这些事，只能说明问题仍在，不能说明本项目方案已经正确。

### 9.4 Unsupported Project Claims

本项目仍需独立证明：

- 国产 NPU 能否从 Host/远端内存稳定直读，且 Host Payload Touch Bytes 为 0；
- Direct-View、Copy-to-HBM 与本地重算的真实交叉点；
- 视图失效时设备队列能否有界终止并安全回退；
- 张量并行多卡状态同步耗时及一致动作正确性；
- NVMe 到设备路径是否绕过 DDR；
- 两节点前后台混压下，TTFT 降低至少 20%、QPS 提升至少 10%，且 TPOT 干扰小于 3%。

### 9.5 Project Establishment Verdict

- **Necessity evidence**：长上下文 KVCache 的 HBM 容量压力和跨层重复搬运都有实测支持。
- **Feasibility evidence**：高带宽异构互联上，设备直接消费 Host KV 并通过内核重排恢复性能已经得到单机 GH200 验证。
- **Differentiation evidence**：跨节点、国产 NPU、动态选路、租约安全、语义一致性、多卡同步和多介质 QoS 仍未解决，且正是本项目范围。
- **Remaining burden of proof**：项目必须在目标硬件与 Mooncake 基线上完成四个核心验证阶段 E0～E3 的实测；DirectKV 的 1.2 倍、43% 和 4.2 倍结果不能替代这些门槛。

## 10. 建议验证

### Experiment 1: 优化内核下的 View-vs-Copy 交叉点

- **Hypothesis**：加入慢层级感知分块后，Direct-View 在高带宽链路和有限重读区间内可以优于 Copy-to-HBM；朴素远读会显著低估其适用范围。
- **Variable**：上下文长度、输出 Token 数、KV tile、链路限速、HBM 水位、并发度、Native/CPU-aware 内核。
- **Baseline**：本地 HBM、Copy-to-HBM、朴素 Direct-View、本地重算。
- **Metric**：TTFT、TPOT P50/P99、链路字节、HBM 字节、片上命中率、显存峰值、Host Payload Touch Bytes。
- **Controlled conditions**：相同模型、精度、批量、请求序列、算子版本和预热状态。
- **Pass condition**：至少一个稳定区间中，优化 Direct-View 的 TPOT P99 不劣于 Copy-to-HBM，显存占用更低，且 CPU 不读写正文。
- **Fail implication**：若优化后仍无收益，Decode 默认走 Copy-to-HBM 或重算，Direct-View 只保留给低重读/Prefill 场景。
- **Decision affected**：E2 路径选择策略与 PVT-03 证伪结论。

### Experiment 2: 层级流量归因与瓶颈转移

- **Hypothesis**：KV 驻留分块能显著减少 UB/URMA 或 C2C 字节，新增 HBM 流量不会在目标并发下形成更严重瓶颈。
- **Variable**：tile 大小、片上缓冲比例、HBM 背景带宽占用、head dimension、Prefill/Decode。
- **Baseline**：原生注意力分块与优化分块。
- **Metric**：远端字节、HBM 读写字节、有效带宽、片上命中率、算子延迟、计算单元利用率。
- **Controlled conditions**：固定输入张量和计算量，禁止同时打开融合与流水，先隔离分块贡献。
- **Pass condition**：远端字节至少下降 30%，算子延迟同步下降，且 HBM 带宽未进入持续饱和。
- **Fail implication**：若只发生流量迁移而延迟不降，停止把该内核列为 Direct-View 快路径。
- **Decision affected**：NPU 内核架构与硬件能力矩阵字段。

### Experiment 3: 异步流水与融合的正交消融

- **Hypothesis**：流水主要隐藏等待，融合主要删除投影—注意力往返，两者应有可区分的硬件证据。
- **Variable**：流水开/关、融合开/关，组成 2×2 对照；再扫描流水深度。
- **Baseline**：无流水、分离内核。
- **Metric**：算子时间线重叠率、链路/HBM 字节、kernel launch 数、occupancy、寄存器与片上内存占用、数值误差。
- **Controlled conditions**：相同分块、编译选项、频率与输入。
- **Pass condition**：流水在字节基本不变时降低等待；融合在计算相同时删除一次可观测往返；结果满足数值误差门限。
- **Fail implication**：若两者收益无法隔离或资源压力抵消收益，保留较简单的单项优化。
- **Decision affected**：异步 DAG、描述符批量提交和融合算子边界。

### Experiment 4: Host 内存压力与多租户尾延迟

- **Hypothesis**：固定页池扩大后，页锁定、NUMA 和并发租户会影响带宽与 TPOT P99，必须纳入容量水位控制。
- **Variable**：固定页池比例、NUMA 节点、前台实例数、后台换出强度、Host 内存水位。
- **Baseline**：单租户空载与普通可分页内存路径。
- **Metric**：TPOT P50/P99、有效带宽、CPU 利用率、页锁定失败、OOM、NUMA 跨节点字节、恢复时间。
- **Controlled conditions**：固定前台到达过程和模型批量，后台负载逐级增加。
- **Pass condition**：目标水位内 TPOT 干扰小于 3%，无页锁定失败或不可恢复 OOM。
- **Fail implication**：限制固定页池、引入水位回收，或把 CPU 内存路径降为条件能力。
- **Decision affected**：E3 混压门槛、容量管理与 QoS。

### Experiment 5: 跨节点视图失效与安全回退

- **Hypothesis**：在真实 Direct-View 访问期间，租约过期、远端进程退出或链路中断可以阻断新访问，并让在途设备操作有界结束或转入可审计失败状态。
- **Variable**：故障类型、注入时点、访问规模、租约剩余时间、多卡并发。
- **Baseline**：Copy-to-HBM 和本地重算回退。
- **Metric**：错误消费数、设备队列状态、请求成功率、恢复时间、旧视图访问次数、多卡分歧次数。
- **Controlled conditions**：同一对象版本、相同故障脚本、独立 Oracle 校验输出。
- **Pass condition**：错误消费为 0，故障范围可定位，请求按策略成功回退，进程与设备状态可恢复。
- **Fail implication**：关闭生产 Direct-View，保留 Copy-to-HBM/重算主路径。
- **Decision affected**：ViewGuard、E1 消费安全和 E2 视图开放范围。

## Appendix

### A. 评测条件速查

| 项目 | 论文配置 |
|---|---|
| 主平台 | NVIDIA GH200 Grace-Hopper Superchip，Hopper GPU，96 GB HBM3，LPDDR5X CPU 内存 |
| PCIe 对照 | H100 PCIe Gen5；作者以相同 H100 GPU 架构减少 GPU 差异，但主机侧仍不同 |
| 软件 | CUDA 12.4、CUTLASS 3.0+、FlashAttention-3 扩展；PyTorch 版本在 §7.1/§7.2 分别写 2.2/2.3 |
| 模型 | Llama-3.1-8B、OPT-13B、OPT-30B |
| 数据集 | ShareGPT、Alpaca |
| 到达过程 | 因数据集无请求时间戳，使用 Poisson 到达 |
| 主范围 | 最高 30 req/s，1K～32K 上下文；另有高负载压力场景 |
| 基线 | SGLang、Pie、Neo、FlexGen |

### B. 证据索引

| Claim | Source | Evidence class | Condition | Caveat |
|---|---|---|---|---|
| 朴素零拷贝性能差 | Fig. 2/3，§2.3 | 实测微基准 | 10240² GEMM，H100/GH200 | 非完整注意力 |
| 慢层级感知分块减少访问放大 | Fig. 4/5，Algorithm 1，§2.4.1/§5.1 | 实测微基准 + 算法 | GH200 | 未扫描 HBM 饱和区 |
| Warp 流水隐藏等待 | Fig. 6，§2.4.2 | 实测微基准 | 简化矩阵 | 无端到端独立消融 |
| 融合删除 KV 往返 | Fig. 7/9/13，§5.3/§5.4/§7.4.2 | 简化微基准 + 模型级实测 | 三种模型 | 联合变量与配置明细不足 |
| 高负载性能更好 | Fig. 10，§7.3.1 | 端到端实测 | GH200，Poisson 到达 | 未报告 P99/误差条 |
| 长上下文容量更大 | Fig. 11，§7.3.2/§7.3.3 | 端到端实测 | 1K～32K，96 GB HBM | 正文数值与柱高需核对 |
| 高带宽互联是前提 | Fig. 14，§7.4.3/§8.2 | 跨平台实测 | 512 Token | H100 PCIe 与 GH200 不只链路不同 |
| 可集成分布式服务 | §8.1～§8.3 | 作者主张 | 架构讨论 | 无跨节点实测 |

### C. 图像资产说明

本报告嵌入 10 张原论文图，均从权威 PDF 页面以 300 DPI 渲染后裁切。裁切采用“安全区域抓取、外部白边收紧、小安全边保留”的顺序，逐张对照原页检查图例、坐标轴、子图标签和边界；原论文图注改由 Markdown 正文单独说明。
