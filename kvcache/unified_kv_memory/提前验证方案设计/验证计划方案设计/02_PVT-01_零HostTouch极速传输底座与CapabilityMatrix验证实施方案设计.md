# PVT-01：Host CPU 零数据拷贝传输底座与硬件能力矩阵验证实施方案设计
## —— Mooncake TransferEngine 深度重构与国产硬件零拷贝底座打通

> **验证 ID**：PVT-01  
> **验证名称**：Host CPU 零数据拷贝极速传输底座与硬件能力矩阵 (CapabilityMatrix) 验证  
> **穿刺优先级**：**🟡 P1 级（底座支撑项）**  
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

## 1. 验证目标与交付结论定义

### 1.1 待验证核心命题
面向国产 AI 芯片构建统一异构 KVCache 存储与调度底座，必须将数据平面建立在 Host CPU 零数据拷贝的高性能硬件通路上。本验证旨在通过重构 Mooncake `TransferEngine`，并在受控传输基准与内核/用户态 eBPF 插桩下证明：
1. **跨节点 URMA / UBMEM 传输**与**本地 NVMe SSD 直达读写**过程中，Host CPU 零数据拷贝（CPU 仅负责下发控制指令与轮询完成队列 CQ，不参与正文搬运，Host Payload Touch Bytes 严格为 0）；
2. 跨节点有效传输带宽达到物理网络线速的 **$\ge 80\%$**（800G 网卡 $\ge 640\text{ Gbps}$ / $80\text{ GB/s}$），本地 NVMe SSD 直达顺序读带宽达到设备物理峰值的 **$\ge 80\%$**；
3. 输出系统级的**《硬件能力矩阵文件 (CapabilityMatrix)》**（即在运行时自动探测各通信链路的带宽、时延等物理参数表，供调度算法使用），为上层 QueryPlan 决策引擎提供真实的硬件拓扑时延与带宽参数。

### 1.2 最终交付数据与结论产出
开发人员执行完本方案后，必须输出以下交付件：
1. **《eBPF CPU 数据拷贝检测日志表》**（证明 memcpy 调用次数与搬运字节数严格为 0）；
2. **《各介质路径裸传输带宽与时延曲线表》**（涵盖 URMA, UBMEM, NVMe Direct, Host Memcpy, TCP/IP 对照）；
3. **《硬件能力矩阵探针文件》**（JSON/CSV 格式，包含介质对、带宽、时延、CPU 拷贝开销）；
4. **《Go / No-Go 判定结论》**：依据线速达成率 $\ge 80\%$ 与 Host CPU 零数据拷贝门槛判定。

---

## 2. 基础/对照 Micro-Benchmark 构建方法

### 2.1 底层驱动 SDK 与 P2P 显存锁定规范

#### 1. URMA / UBMEM 头文件与链接：
```cpp
#include <urma.h>
#include <ubmem.h>
#include <acl/acl.h>
#include <acl/acl_rt.h>
#include <liburing.h>
```
- **库文件链接**：`-lurma -lubmem -luring -lascendcl -lpthread`

#### 2. NPU 显存 P2P 注册与 Mooncake 扩展版驱动接口实现：
对齐 Mooncake 最新主线 `include/ascend_allocator.h` 与 `TransportType::AscendDirect` / `TransportType::UB` 体系：
```cpp
// 1. 调用 Mooncake 扩展版 VMM 接口分配支持跨设备 P2P DMA 的 NPU 显存 (1GB 对齐物理连续)
void* npu_hbm_ptr = mooncake::ascend_allocate_vmm_memory_direct(payload_size);
if (!npu_hbm_ptr) {
    aclrtMalloc(&npu_hbm_ptr, payload_size, ACL_MEM_MALLOC_HUGE_FIRST_P2P);
}

// 2. 将 NPU 显存物理 Handle 注册至 URMA 网卡控制器 (Pin Memory)
aclrtDrvMemHandle drv_handle = mooncake::ascend_get_physical_handle_from_va(npu_hbm_ptr);
struct urma_mr* mr = urma_register_dev_mr(
    qp->dev, npu_hbm_ptr, payload_size, 
    URMA_ACCESS_LOCAL_WRITE | URMA_ACCESS_REMOTE_READ | URMA_ACCESS_REMOTE_WRITE
);
```

#### 3. NVMe Direct 存储访问双模式设计：
- **主路径模式（Linux 6.6+ `io_uring` + `O_DIRECT`）**：
  - 打开裸盘块设备：`int fd = open("/dev/nvme0n1", O_RDWR | O_DIRECT);`
  - 使用预注册固定缓冲区执行 P2P DMA：`io_uring_prep_read_fixed(sqe, fd, npu_hbm_ptr, size, lba_offset, buf_index);`
- **高性能备选模式（SPDK NVMe 用户态驱动）**：
  - 通过 `spdk_nvme_ns_cmd_read_with_md()` 直接将 NVMe PRP/SGL 指向 NPU HBM 物理地址，绕过 Linux 内核页缓存。

### 2.2 测试工具与源码结构
本项验证涉及的全部底层测试与监控源码存放在 `./原型验证代码/PVT-01/` 目录下：

```
原型验证代码/PVT-01/
├── raw_trans_bench.cc         # 测量 URMA/UBMEM/NVMe Direct 零拷贝 vs CPU memcpy 性能的 C++ 压测工具
├── Makefile                   # 编译 raw_trans_bench 的工程构建文件 (make -j16)
├── host_touch_monitor.py      # 基于 Linux eBPF (bpftrace) 监控 CPU 触碰的全量内核与用户态探针脚本
└── export_capability_matrix.py# 自动解析实测吞吐并导出 capability_matrix.json 的工具脚本
```

编译方法：
```bash
# 编译底层传输 Harness
cd ./原型验证代码/PVT-01 && make clean && make
```

### 2.3 四组实验路径构建与隔离设置
- **路径 A（URMA / UBMEM 零拷贝 Direct 模式）**：
  - 发送端通过 `liburma` / `libubmem` 注册 NPU HBM 显存区域；
  - 接收端发起 RDMA Direct Read/Write，网卡 DMA 直接搬运数据至远端 NPU HBM；
  - Host CPU 仅执行描述符提交与 CQ Polling。
- **路径 B（NVMe Direct SSD 直达模式）**：
  - 采用 `io_uring` + `O_DIRECT` + Peer-to-Peer DMA；
  - 数据直接在 NVMe SSD 与 NPU HBM 之间流转，严格绕过 Host DDR。
- **对照组 C（CPU Memcpy 软中转基准）**：
  - 模拟传统两阶段中转：网卡/SSD $\to$ Host DDR $\to$ Host CPU `memcpy` $\to$ NPU HBM。
- **对照组 D（标准 TCP/IP Socket 基准）**：
  - 通过标准 Linux Socket 协议栈进行内核中转拷贝传输。

---

## 3. 业务 Benchmark 构造与流量特征编排

### 3.1 数据块尺寸梯度设计
构造覆盖 KVCache 全生命周期典型尺寸的内存 Extent：
- **小数据块（64KB, 256KB, 1MB）**：模拟前缀元数据、轻量 Manifest、极短系统提示词；
- **中数据块（4MB, 16MB, 32MB）**：模拟 1K ~ 8K Token 的标准对话上下文前缀；
- **大数据块（64MB, 256MB, 1GB）**：模拟 32K ~ 128K 超长上下文前缀及大批量并发 Block。

### 3.2 队列深度 (Queue Depth) 与并发流
- **单流时延压测**：Queue Depth = 1（测量单次微秒级延迟极限）；
- **并发吞吐压测**：Queue Depth = 4, 16, 32, 64（测试总线打满时的饱和带宽）。

---

## 4. 软硬件环境与 eBPF 全量探针插桩方案

### 4.1 硬件拓扑与驱动要求
- **节点配置**：Node-0 与 Node-1，每节点配置 8× NPU (96GB HBM3), 800G URMA 网卡, 4× NVMe PCIe Gen5 SSD；
- **系统支持**：Linux Kernel 6.6+, 开启 `io_uring`, 挂载 UBMEM / URMA 驱动模块。

### 4.2 eBPF 全量防漏报探针设计 (`host_touch_monitor.py`)

为了杜绝 glibc 内部优化版 SIMD（如 AVX-512/AVX2/NEON 汇编内联）绕过内核探测的问题，eBPF 监控脚本采用**多层立体探针**：

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        eBPF 内核态 + 用户态防漏报探针矩阵                              │
├───────────────────┬───────────────────────────────────┬────────────────────────────────┤
│ 探针类型          │ 挂载符号与探针点                  │ 监测防护目标                   │
├───────────────────┼───────────────────────────────────┼────────────────────────────────┤
│ 内核 kprobe       │ kprobe:memcpy / kprobe:memmove    │ 捕获内核空间数据拷贝调用       │
│ 内核 kprobe       │ kprobe:copy_user_generic_string   │ 捕获内核态与用户态间跨空间拷贝 │
│ 内核 Tracepoint   │ tracepoint:exceptions:page_fault  │ 监测用户显存访问触发的缺页中断 │
│ glibc uprobe      │ uprobe:/lib64/libc.so.6:memcpy    │ 捕获通用 C 库 memcpy 入口      │
│ glibc AVX uprobe  │ uprobe:/lib64/libc.so.6:__memcpy_*│ 捕获 AVX512/AVX2 向量加速拷贝  │
└───────────────────┴───────────────────────────────────┴────────────────────────────────┘
```

启动监控命令：
```bash
python3 ./原型验证代码/PVT-01/host_touch_monitor.py --target-pid <target_pid> --out ebpf_touch_log.txt
```

---

## 5. 分步执行测试操作规程

开发人员请按以下 12 个步骤依次执行：

### 步骤 1：启动内核与用户态 eBPF 监控
在被测节点上启动 `host_touch_monitor.py`，传入即将运行的测试进程 PID。

### 步骤 2：运行对照组 C（CPU Memcpy 软中转基准）
测试经过 Host CPU `memcpy` 中转的吞吐与 CPU 占用：
```bash
./raw_trans_bench --mode host_memcpy --sizes 64K,1M,16M,64M,256M,1G --qd 1,16,64 --out res_memcpy.csv
```

### 步骤 3：运行对照组 D（TCP/IP Socket 基准）
测试标准 Socket 传输在各包大小下的性能：
```bash
./raw_trans_bench --mode socket_tcp --sizes 64K,1M,16M,64M,256M,1G --qd 1,16,64 --out res_tcp.csv
```

### 步骤 4：运行路径 A（URMA / UBMEM 跨节点 Direct RDMA 模式）
测试零拷贝网络传输性能：
```bash
./raw_trans_bench --mode urma_direct --sizes 64K,1M,16M,64M,256M,1G --qd 1,16,64 --out res_urma.csv
./raw_trans_bench --mode ubmem_direct --sizes 64K,1M,16M,64M,256M,1G --qd 1,16,64 --out res_ubmem.csv
```

### 步骤 5：读取路径 A 的 eBPF 统计，验证 Host Touch
检查 `@memcpy_bytes` 是否严格等于 0，CPU 占用是否 $< 5\%$。

### 步骤 6：运行路径 B（NVMe Direct SSD 直达模式）
测试 HBM ↔ NVMe SSD 直达顺序读写性能（基于 `io_uring` FIXED buffer）：
```bash
./raw_trans_bench --mode nvme_direct --sizes 1M,16M,64M,256M,1G --qd 1,8,32 --out res_nvme.csv
```

### 步骤 7：读取路径 B 的 eBPF 统计，验证 Host Touch
检查 NVMe 直达传输下的 memcpy 字节数是否严格为 0。

### 步骤 8：汇总并生成吞吐对比表
将 4 条路径的 CSV 汇总生成带宽对比表。

### 步骤 9：运行硬件能力矩阵自动导出脚本
```bash
python3 ./原型验证代码/PVT-01/export_capability_matrix.py --input-dir . --out capability_matrix.json
```

### 步骤 10：验证 JSON 格式合法性与字段完备性
检查 `capability_matrix.json` 是否包含所有介质对的时延与带宽。

### 步骤 11：判定 Go / No-Go 结论
按判定公式计算线速达成率与零拷贝合规性。

### 步骤 12：归档数据并输出报告
将数据与日志归档至 `reports/evidence_pack_v1.0/`。

---

## 6. 数据采集清单与记录格式

### 6.1 传输性能数据表 (`pvt01_trans_results.csv`)
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

### 7.2 Host Payload Touch Bytes 判定
$$\text{Host Payload Touch Bytes} = \text{eBPF 捕获的 memcpy 搬运总字节数} \equiv 0$$

---

## 8. 多维扩展与扫参矩阵

| 参数维度 | 扫描范围 | 测试目标 |
|---|---|---|
| **数据包尺寸** | 64KB, 256KB, 1MB, 4MB, 16MB, 64MB, 256MB, 1GB | 绘制各介质带宽饱和曲线与小包时延惩罚 |
| **队列深度** | 1, 2, 4, 8, 16, 32, 64 | 探明物理网卡与 NVMe 控制器的最佳并发深度 |
| **介质链路** | UBMEM, URMA, NVMe Direct, Memcpy, TCP | 建立完备的硬件能力矩阵 |

---

## 9. Go / Conditional / No-Go 判定规则与交付报告模板

### 9.1 量化判定准则
- **Go (准入通过)**：
  - 800G 网络下有效线速达成率 $\ge 80\%$（带宽 $\ge 80\text{GB/s}$）；
  - NVMe SSD 直达顺序读带宽达到设备峰值 $\ge 80\%$；
  - 传输期间 Host CPU 零数据拷贝（`Host Payload Touch Bytes` 严格为 0，CPU 占用率 $< 5\%$）。
- **Conditional (条件准入)**：
  - 线速达成率在 $70\% \sim 80\%$ 之间，且零拷贝成立，需在驱动层进一步调优 QD 与轮询策略；
- **No-Go (否决关闭)**：
  - 存在不可消除的 Host CPU 内存拷贝，或有效带宽 $< 70\%$ 物理线速。
