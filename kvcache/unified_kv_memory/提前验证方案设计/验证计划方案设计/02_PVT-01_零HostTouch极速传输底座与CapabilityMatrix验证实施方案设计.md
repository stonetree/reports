# PVT-01：Host CPU 零数据拷贝传输底座与硬件能力矩阵验证实施方案设计
## —— Mooncake TransferEngine 深度重构与国产硬件零拷贝底座打通

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。DEMO 模式不得按名义 Payload 计算 Direct 路径实测带宽；LAB/MEASURED 必须使用设备实际完成字节和正式探针结果。缺少 bpftrace 或等价探针时结果为 `INVALID_EVIDENCE`。

> **验证 ID**：PVT-01  
> **验证名称**：Host CPU 零数据拷贝极速传输底座与硬件能力矩阵 (CapabilityMatrix) 验证  
> **验证优先级**：**🟡 P1 级（底座支撑项）**  
> **对应验证阶段**：**E1 核心数据路径打通**  
> **证伪标记**：否（底层传输能力确认）  
> **建议周期**：6~8 人日  
> **主关联 IR**：`IR-01-06`, `IR-01-08`, `IR-01-09`, `IR-01-12`  
> **核心 SRS / SR23 锚点**：  
> - SRS: `L4-MC-HIER-STORE-001`, `L3-MS-Tiering-038`, `L4-HW-HostPayloadTouchBudget-076`, `L4-FT-PathIntegrityPolicy-077`  
> - SR23: `SR23-01-06-01`, `SR23-01-07-01`, `SR23-01-08-01`, `SR23-01-09-01`, `SR23-01-12-01`  
> **开源基线版本与代码仓库**：  
> - **Mooncake TransferEngine**：[`https://github.com/kvcache-ai/Mooncake.git`](https://github.com/kvcache-ai/Mooncake.git) (Commit: `f90ae691f109e49a60920e0c8abbf7e572826d8c`，子模块路径: `mooncake-transfer-engine/`)  
> **研发对齐状态**：已闭环研发评估报告 1, 2, 3 项及 eBPF 探针防漏报规范（明确 P2P Pinning 接口、io_uring 双模式与全量 uprobe）  

---

## 0. 架构导读与传统软件系统视角切入

### 0.1 传统系统开发视角：为什么“零拷贝”至关重要？
在高性能网络编程与存储系统（如 Kafka、Netty、DPDK、SPDK）中，我们都非常清楚一个铁律：**千万不能让 CPU 当数据搬运工**。
- 如果每秒有数十 GB 的数据流经 CPU 进行 `memcpy`，CPU 会把全部算力消耗在从内存读取字节再写入内存的死循环中；
- 更致命的是，这会把主板的内存总线（DDR 带宽）和 CPU L3 Cache 彻底打爆，导致同一台机器上运行的其他关键业务（如控制面调度、API 服务）发生严重排队卡死（即所谓的“CPU 墙 / Memory Wall”）。

### 0.2 大模型推理中的对应物理场景
在大模型分布式推理中，节点之间需要以 100Gbps~400Gbps 的极高带宽搬运海量 KVCache（大模型注意力键值缓存：大模型自回归生成过程中缓存的历史 Key 与 Value 激活状态张量，用于避免后续 Token 生成时重复计算注意力）：
- **开源 Mooncake 的缺陷**：开源方案中，数据传输经常在 Host DDR、CPU 与 NPU 显存之间来回中转拷贝，导致数据吞吐被 CPU 锁死；
- **我们的原厂软硬件协同方案**：必须实现 **Host CPU 零数据拷贝（CPU 仅负责下发控制指令，不参与正文数据搬运，Host Payload Touch Bytes 严格为 0）**，让数据通过硬件 DMA（直接内存访问）直接在 NPU 显存（HBM）与网卡/SSD 之间直达流转（Peer-to-Peer DMA，P2P）。

### 0.3 什么是硬件能力矩阵 (CapabilityMatrix)？
在分布式系统中，调度器在决定把数据放到哪里、从哪里拉取之前，必须清楚知道当前服务器底层的各项物理指标（例如：从 NPU 显存写本地 NVMe SSD 的读写带宽是多少？跨节点通过高速网络读远端显存的时延是多少？）。
我们在运行时通过底层探针自动探测并生成的这张物理参数表，就叫做 **硬件能力矩阵（CapabilityMatrix，即在运行时自动探测各通信链路的带宽、时延等物理参数表，供调度算法使用）**。

### 0.4 关键技术手段解惑：什么是 eBPF？为什么必须用它来当“中立司法裁判”？
很多新进入本领域的工程师会问：“我们既然在代码里写了 DMA 传输，怎么证明 CPU 真的没有悄悄去调用 `memcpy` 拷贝数据呢？为什么需要 eBPF？”
- **什么是 eBPF？**：eBPF（Extended Berkeley Packet Filter）是 Linux 内核中的一项革命性技术。它允许开发者在不修改内核源码、不重启系统的情况下，在内核的关键函数入口（kprobe）或用户态 C 库函数（uprobe）挂载安全的高性能探针，实时拦截并统计特定函数被谁调用了、调用了多少次、传入了多少字节。
- **为什么必须用 eBPF？**：应用层代码可能会说谎或存在未察觉的第三方库隐式内存拷贝（如底层驱动偷偷把数据拷贝到了临时缓冲）。我们使用基于 eBPF 的监控脚本（`host_touch_monitor.py`）挂载到系统的 `kprobe:memcpy` 与 glibc 的 `uprobe:memcpy` 上，就像在操作系统内核安插了一个**不可伪造的中立“司法裁判”**：一旦传输过程中 CPU 发生了哪怕 1 个字节的内存拷贝，eBPF 探针会立刻抓现行并精准记录；如果 eBPF 记录的 `memcpy_bytes` 严格为 0，就给出了不可辩驳的“Host CPU 零数据拷贝”硬核技术证据！

---

## 1. 验证目标与交付结论定义

### 1.1 现实前因痛点与待验证核心命题
1. **现实痛点**：开源方案依赖 Host CPU 中转数据，并发拉取 KVCache 时 CPU 100% 满载，严重挤占控制面调度与在线推理；
2. **核心命题**：
   - 证明跨节点 **URMA（通用远程直接内存访问：用户态的高性能 RDMA 驱动接口与通信协议） / UBMEM（统一总线内存直通共享协议：支持跨节点与异构设备间直接共享内存地址空间的底层通信协议）** 传输与本地 **NVMe SSD 直达读写** 过程中，Host CPU 零数据拷贝（Host Payload Touch Bytes 严格为 0）；
   - 跨节点有效传输带宽达到物理网络线速的 **$\ge 80\%$**，本地 NVMe SSD 直达顺序读带宽达到物理峰值的 **$\ge 80\%$**；
   - 自动生成并导出标准的《硬件能力矩阵文件 (CapabilityMatrix)》，为上层微秒决策引擎（QueryPlan）提供精准参数。

### 1.2 最终交付数据与结论产出
1. **《eBPF CPU 数据拷贝检测日志表》**（证明 memcpy 调用次数与搬运字节数严格为 0）；
2. **《各介质路径裸传输带宽与时延曲线表》**（涵盖 URMA, UBMEM, NVMe Direct, Host Memcpy, TCP/IP 对照）；
3. **《硬件能力矩阵探针文件》**（`capability_matrix.json`）；
4. **《Go / No-Go 判定结论》**。

---

## 2. 基础/对照 Micro-Benchmark 构建方法

### 2.1 底层驱动 SDK 与 P2P 显存锁定规范

为了让网卡或 NVMe 控制器的 DMA 引擎能够直接访问 NPU 的显存（HBM），必须完成两个关键步骤：
1. **分配支持硬件 P2P 的显存**：调用 CANN 驱动接口分配允许跨设备 DMA 寻址的连续显存页；
2. **向网络/存储驱动注册内存区域（Memory Region, MR / Pinning）**：将显存的物理地址锁定并提交给网卡/存储控制器，获取本地与远端访问密钥（rkey/lkey），使后续数据传输完全由硬件 DMA 引擎自主完成，彻底绕过 CPU。

```cpp
#include <urma.h>
#include <ubmem.h>
#include <acl/acl.h>
#include <acl/acl_rt.h>
#include <liburing.h>

// 步骤 1：分配支持跨设备 P2P DMA 的 NPU 显存
void* npu_hbm_ptr = nullptr;
aclrtMalloc(&npu_hbm_ptr, payload_size, ACL_MEM_MALLOC_HUGE_FIRST_P2P);

// 步骤 2：将 NPU 显存物理地址直接注册至 URMA 网卡控制器 (Pin Memory)
struct urma_mr* mr = urma_register_dev_mr(
    qp->dev, npu_hbm_ptr, payload_size, 
    URMA_ACCESS_LOCAL_WRITE | URMA_ACCESS_REMOTE_READ | URMA_ACCESS_REMOTE_WRITE
);

// 步骤 3：NVMe Direct 存储访问（Linux 6.6+ io_uring + O_DIRECT 裸盘直达）
int fd = open("/dev/nvme0n1", O_RDWR | O_DIRECT);
io_uring_prep_read_fixed(sqe, fd, npu_hbm_ptr, size, lba_offset, buf_index);
```

### 2.2 测试工具与源码结构
源码存放在 `./原型验证代码/PVT-01/` 目录下：

```
原型验证代码/PVT-01/
├── raw_trans_bench.cc         # 测量 URMA/UBMEM/NVMe Direct 零拷贝 vs CPU memcpy 性能的 C++ 压测工具
├── Makefile                   # 编译 raw_trans_bench 的工程构建文件 (make -j16)
├── host_touch_monitor.py      # 基于 Linux eBPF (bpftrace) 监控 CPU 触碰的全量内核与用户态探针脚本
└── export_capability_matrix.py# 自动解析实测吞吐并导出 capability_matrix.json 的工具脚本
```

编译方法：
```bash
cd ./原型验证代码/PVT-01 && make clean && make -j16
```

### 2.3 四组实验路径设计意图与对照逻辑
为了形成严密的对比证据链，本验证设计了 2 组目标路径和 2 组对照基线：
- **路径 A（URMA / UBMEM 零拷贝 Direct 模式）**：【目标路径】NPU HBM ↔ 网卡 DMA 裸直达，验证跨节点零 CPU 拷贝下的线速传输；
- **路径 B（NVMe Direct SSD 直达模式）**：【目标路径】`io_uring` + `O_DIRECT` + P2P DMA，验证本地存储直达 NPU 显存时的零 CPU 拷贝与大吞吐；
- **对照组 C（CPU Memcpy 软中转基准）**：【反面基准】数据先从硬件读入 Host DDR 内存，再由 Host CPU 执行 `memcpy` 拷入 NPU 显存。用于量化“CPU 当搬运工”时的极端算力开销与总线瓶颈；
- **对照组 D（标准 TCP/IP Socket 基准）**：【传统基准】标准 Linux Socket 协议栈中转，用于量化传统网络协议在面对大模型传输时的性能劣势。

---

## 3. 业务 Benchmark 构造与流量特征编排

### 3.1 数据块尺寸梯度设计意图
- **小数据块（64KB, 256KB, 1MB）**：模拟极短的 System Prompt 或 Manifest 描述符。测试目标是测量链路的固有时延与小包调度开销；
- **中数据块（4MB, 16MB, 32MB）**：模拟 1K ~ 8K Token 的标准大模型对话前缀。测试目标是观察带宽爬坡与并发吞吐；
- **大数据块（64MB, 256MB, 1GB）**：模拟 32K ~ 128K 超长上下文（如长文档分析、代码库检索）的超大 KVCache。测试目标是验证硬件总线在饱和打满时的物理吞吐极限。

### 3.2 队列深度 (Queue Depth) 设计意图
- **单流时延压测（Queue Depth = 1）**：前一个包完成再发下一个，测量单次 DMA 往返的微秒级物理延迟；
- **并发吞吐压测（Queue Depth = 4, 16, 32, 64）**：同时向硬件提交多个并发描述符，测试硬件多通道并行时的峰值吞吐（衡量是否能喂饱 100G/400G 网卡）。

---

## 4. 软硬件环境与 eBPF 全量探针插桩方案

### 4.1 硬件拓扑与驱动要求
- **系统要求**：Linux Kernel 6.6+，开启 `CONFIG_BPF_SYSCALL=y` 与 `io_uring`，安装 `bpftrace` 工具；
- **设备要求**：已挂载 UBMEM / URMA 驱动模块与 CANN NPU 驱动，至少一块 NVMe 固态硬盘（如 `/dev/nvme0n1`）。

### 4.2 eBPF 全量防漏报探针设计与工作原理 (`host_touch_monitor.py`)

`host_touch_monitor.py` 在后台启动时，会自动向系统的 5 个关键锚点注入探针：

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        eBPF 内核态 + 用户态防漏报探针矩阵                              │
├───────────────────┬───────────────────────────────────┬────────────────────────────────┤
│ 探针类型          │ 挂载符号与探针点                  │ 监测防护目标（防漏报机制）     │
├───────────────────┼───────────────────────────────────┼────────────────────────────────┤
│ 内核 kprobe       │ kprobe:memcpy / kprobe:memmove    │ 捕获内核空间数据拷贝调用       │
│ 内核 kprobe       │ kprobe:copy_user_generic_string   │ 捕获内核态与用户态间跨空间拷贝 │
│ 内核 Tracepoint   │ tracepoint:exceptions:page_fault  │ 监测用户显存访问触发的缺页中断 │
│ glibc uprobe      │ uprobe:/lib64/libc.so.6:memcpy    │ 捕获通用 C 库 memcpy 入口      │
│ glibc AVX uprobe  │ uprobe:/lib64/libc.so.6:__memcpy_*│ 捕获 AVX512/AVX2 向量加速拷贝  │
└───────────────────┴───────────────────────────────────┴────────────────────────────────┘
```
**探针运行机制**：脚本接收被测进程的 PID 作为参数，仅对该进程发起的数据拷贝进行精确统计。若被测进程在传输期间调用了上述任何函数，探针会累加记录拷贝次数与字节数；若全程为 0，则在生成的 JSON 报告中打上 `zero_touch_verified: true` 标记。

---

## 5. 分步执行测试操作规程 (SOP)

开发人员在执行验证时，请严格按照以下 6 个步骤逐步执行，并理解每一步的操作意图：

### 步骤 1：启动 eBPF 内核与用户态中立监控器
- **操作意图**：在压测开始前预先将 eBPF 探针注入操作系统内核与 C 库，作为中立的“司法裁判”全天候监视 CPU 内存操作。
- **执行命令**：
```bash
# 启动监控脚本，监听目标进程（传入 PID 或设为全系统监听），采样时长设为 30 秒，输出为证据 JSON
python3 ./原型验证代码/PVT-01/host_touch_monitor.py $$ 30 --out host_touch_evidence.json --evidence-level LAB &
MONITOR_PID=$!
```

### 步骤 2：运行对照组 C（CPU Memcpy 软中转基准测试）
- **操作意图**：测量当采用传统“CPU 内存拷贝”搬运 64MB 数据时的耗时、吞吐与 CPU 占用率。开发人员应观察到 CPU 占用率飙升到接近 100%，eBPF 记录下大量 `memcpy_bytes`。该组数据作为后续证明 Direct 模式优势的基线对照。
- **执行命令**：
```bash
./raw_trans_bench --mode host_memcpy --payload-bytes 67108864 --qd 16 --loops 10 --out res_memcpy.csv
```

### 步骤 3：运行对照组 D（标准 TCP/IP Socket 网络基准测试）
- **操作意图**：测量采用标准 Linux Socket 传输时的网络时延与吞吐。用于展示传统网络栈在微秒级高并发场景下的性能瓶颈。
- **执行命令**：
```bash
./raw_trans_bench --mode socket_tcp --payload-bytes 67108864 --qd 16 --loops 10 --out res_tcp.csv
```

### 步骤 4：运行路径 A（URMA / UBMEM 跨节点 Direct RDMA 零拷贝模式）
- **操作意图**：启动 NPU HBM 与网卡 DMA 裸直达传输。开发人员应观察到传输带宽迅速达到物理网卡线速的 80% 以上，CPU 占用率低于 5%，且 eBPF 捕获的 `memcpy_bytes` 严格为 0。
- **执行命令**：
```bash
./raw_trans_bench --mode urma_direct --payload-bytes 67108864 --qd 16 --loops 10 --out res_urma.csv
./raw_trans_bench --mode ubmem_direct --payload-bytes 67108864 --qd 16 --loops 10 --out res_ubmem.csv
```

### 步骤 5：运行路径 B（NVMe Direct SSD 裸盘直达模式）
- **操作意图**：通过 `io_uring` FIXED Direct I/O 从本地 NVMe 裸盘直接将数据读取到 NPU 显存，验证绕过 Host DDR 时本地存储层的大吞吐直达能力。
- **执行命令**：
```bash
./raw_trans_bench --mode nvme_direct --payload-bytes 67108864 --qd 32 --loops 10 --out res_nvme.csv
```

### 步骤 6：停止 eBPF 监控并导出标准化《硬件能力矩阵》
- **操作意图**：停止监控脚本，调用解析工具将上述 4 组实测 CSV 与 eBPF 零拷贝证据聚合，自动生成标准的 `capability_matrix.json`。该文件将直接作为 PVT-04（QueryPlan 决策引擎）的物理输入配置！
- **执行命令**：
```bash
wait $MONITOR_PID
python3 ./原型验证代码/PVT-01/export_capability_matrix.py \
    res_memcpy.csv res_urma.csv res_ubmem.csv res_nvme.csv \
    --host-touch-evidence host_touch_evidence.json \
    --out capability_matrix.json
```

---

## 6. 数据采集清单与记录格式

### 6.1 传输性能数据表 (`pvt01_trans_results.csv`)
每个测试路径运行完成后，会输出包含以下字段的标准 CSV 表格：

```csv
path_id,mode,payload_size,queue_depth,bandwidth_gbps,latency_p50_us,latency_p99_us,cpu_usage_pct,memcpy_calls,memcpy_bytes
PATH-A1,urma_direct,67108864,16,84.2,760.5,820.1,2.1,0,0
PATH-A2,ubmem_direct,67108864,16,92.5,690.2,740.3,1.8,0,0
PATH-B,nvme_direct,67108864,8,26.4,2420.0,2650.0,3.2,0,0
PATH-C,host_memcpy,67108864,16,38.1,1680.0,2150.0,98.5,1024,67108864
PATH-D,socket_tcp,67108864,16,22.4,2890.0,3950.0,85.2,4096,67108864
```

---

## 7. 数据交叉组合与运算推导逻辑

### 7.1 线速达成率计算公式
$$\eta_{\text{wire\_speed}} = \frac{\text{实测吞吐带宽 (Gbps)}}{\text{物理标称线速 (Gbps)}} \times 100\%$$
- **物理意义**：衡量传输底座是否完全消除了软件协议栈开销，是否充分发挥了网卡与总线的硬件极限。

### 7.2 Host Payload Touch Bytes 判定
$$\text{Host Payload Touch Bytes} = \text{eBPF 捕获的 memcpy 搬运总字节数} \equiv 0$$
- **物理意义**：证明在传输全过程中，Host CPU 仅负责下发几条微秒级控制描述符，没有参与任何正文字节的数据搬移。

---

## 8. 多维扩展与扫参矩阵

| 参数维度 | 扫描范围 | 测试目标与意图 |
|---|---|---|
| **数据包尺寸** | 64KB, 256KB, 1MB, 4MB, 16MB, 64MB, 256MB, 1GB | 绘制各介质带宽饱和曲线，找出带宽拐点与小包时延惩罚 |
| **队列深度** | 1, 2, 4, 8, 16, 32, 64 | 探明物理网卡与 NVMe 控制器的最佳并发深度 |
| **介质链路** | UBMEM, URMA, NVMe Direct, Memcpy, TCP | 建立完备的硬件能力矩阵，为上层调度提供全景物理画像 |

---

## 9. Go / Conditional / No-Go 判定规则与交付报告模板

- **Go (准入通过)**：
  - 网络有效线速达成率 $\ge 80\%$；
  - NVMe SSD 直达顺序读带宽达到设备峰值 $\ge 80\%$；
  - 传输期间 Host CPU 零数据拷贝（`Host Payload Touch Bytes` 严格为 0，CPU 占用率 $< 5\%$）。
- **Conditional (条件准入)**：线速达成率在 $70\% \sim 80\%$ 之间，且零拷贝成立；
- **No-Go (否决关闭)**：存在不可消除的 Host CPU 内存拷贝，或有效带宽 $< 70\%$ 物理线速。

---

## 10. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 10.1 研发任务拆解与分工
- **工程师职责**：
  1. 检查测试机环境（Linux 6.6+、网卡驱动、NVMe 裸盘权限）；
  2. 按照第 5 节的 SOP 顺序执行 6 个测试步骤；
  3. 观察 `top` 中的 CPU 占用率与 eBPF 输出的 `host_touch_evidence.json`；
- **AI Agent 职责**：
  1. 负责 `raw_trans_bench.cc` 中 DMA 与 `io_uring` 固定缓冲区调用逻辑核对；
  2. 调试 `host_touch_monitor.py` 中的 bpftrace 脚本，确保探针无报错；
  3. 自动解析 CSV 表格并生成美观的性能对比折线图。

### 10.2 专属 Prompt 模板（可直接复制给 AI Agent）
```text
我是一名系统底层开发工程师，正在开展 PVT-01 验证（验证 Host CPU 零数据拷贝传输底座与导出硬件能力矩阵）：
1. 请阅读 ./原型验证代码/PVT-01/raw_trans_bench.cc 与 host_touch_monitor.py；
2. 检查 raw_trans_bench.cc 中对 urma_register_dev_mr 与 io_uring_prep_read_fixed 的调用，确保显存物理地址被正确锁定且避开 Host DDR 拷贝；
3. 为我生成一个自动化测试脚本 run_trans_pvt01.sh，按顺序执行 6 个 SOP 步骤（启动 eBPF 探针、跑 Memcpy/TCP 对照组、跑 URMA/UBMEM/NVMe Direct 组、导出能力矩阵）；
4. 运行 export_capability_matrix.py，自动解析生成的 CSV 并输出 capability_matrix.json；
5. 输出结果汇总表格，并验证 Host Payload Touch Bytes 是否严格为 0。
```

### 10.3 常见排错指南
- **eBPF 报错无法加载内核探针**：确保当前内核已开启 `CONFIG_BPF_SYSCALL=y`，或使用 `sudo` 权限运行；若在容器中运行，需以 `--privileged` 启动并挂载 `/sys/kernel/debug`；
- **NVMe Direct 报错 `EINVAL`**：`O_DIRECT` 要求读写缓冲区与磁盘偏移必须严格按 4096 字节（4KB）对齐，请检查内存是否由 `posix_memalign` 分配；
- **URMA 注册显存报错权限不足**：检查 NPU 驱动是否支持 P2P 模式，需确保调用了 `ACL_MEM_MALLOC_HUGE_FIRST_P2P` 标志。
