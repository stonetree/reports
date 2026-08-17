# PVT-01：零 Host Touch 极速传输底座与 CapabilityMatrix 验证实施方案设计

> **验证 ID**：PVT-01  
> **验证名称**：零 Host Touch 极速传输底座与 CapabilityMatrix 探针验证  
> **对应证据门**：**E1 能力路径**  
> **证伪标记**：否（底座能力确认）  
> **建议周期**：6~8 人日  
> **主关联 IR**：`IR-01-06`, `IR-01-08`, `IR-01-09`, `IR-01-12`  
> **核心 SRS / SR23 锚点**：  
> - SRS: `L4-MC-HIER-STORE-001`, `L3-MS-Tiering-038`, `L4-HW-HostPayloadTouchBudget-076`, `L4-FT-PathIntegrityPolicy-077`  
> - SR23: `SR23-01-06-01`, `SR23-01-07-01`, `SR23-01-08-01`, `SR23-01-09-01`, `SR23-01-12-01`  

---

## 1. 验证目标与交付结论定义

### 1.1 待验证核心命题
统一异构存储池的数据平面必须建立在零/极低 Host CPU 参与的高性能硬件通路上。本验证旨在通过受控传输 Benchmark 与内核 eBPF 插桩，证明：
1. **跨节点 URMA / UBMEM 传输**与**本地 NVMe SSD 直达读写**过程中，Host CPU 正文触碰字节数（Host Payload Touch Bytes）**严格为 0**（Host CPU 仅负责提交与轮询 CQ 描述符，严禁参与正文 `memcpy`、编解码或全量 CRC）；
2. 跨节点有效传输带宽达到物理网络线速的 **$\ge 80\%$**，本地 NVMe SSD 直达顺序读带宽达到设备物理峰值的 **$\ge 80\%$**；
3. 输出系统级的**《硬件 CapabilityMatrix 探针能力表》**，为上层 QueryPlan 决策引擎提供真实的硬件拓扑时延与带宽参数。

### 1.2 最终交付数据与结论产出
开发人员执行完本方案后，必须输出以下交付件：
1. **《eBPF CPU 触碰与内存拷贝检测日志表》**（证明 memcpy 调用次数与字节数为 0）；
2. **《各介质路径裸传输带宽与时延曲线表》**（涵盖 URMA, UBMEM, NVMe Direct, Host Memcpy, TCP/IP 对照）；
3. **《硬件 CapabilityMatrix 探针矩阵文件》**（JSON/CSV 格式，包含介质对、带宽、时延、CPU 触碰开销）；
4. **《Go / No-Go 判定结论》**：依据线速达成率 $\ge 80\%$ 与 Host Touch = 0 门槛判定。

---

## 2. 基础/对照 Micro-Benchmark 构建方法

### 2.1 测试工具与源码结构
本项验证涉及的全部底层测试与监控源码均存放在 `./原型验证代码/PVT-01/` 目录下：

```
原型验证代码/PVT-01/
├── raw_trans_bench.cc         # 测量 URMA/UBMEM/NVMe Direct 零拷贝 vs CPU memcpy 性能的 C++ 压测工具
├── Makefile                   # 编译 raw_trans_bench 的工程构建文件 (make -j16)
├── host_touch_monitor.py      # 基于 Linux eBPF (bpftrace) 监控 Host CPU 内存拷贝事件的内核探针脚本
└── export_capability_matrix.py# 自动解析实测吞吐并导出 capability_matrix.json 的工具脚本
```

编译方法：
```bash
# 编译底层传输 Harness
cd ./原型验证代码/PVT-01 && make clean && make
```

### 2.2 四组实验路径构建与隔离设置
- **路径 A（URMA / UBMEM 零拷贝 Direct 模式）**：
  - 发送端通过 `liburma` / `libubmem` 注册 NPU HBM 物理内存区域；
  - 接收端发起 RDMA Direct Read/Write，网卡 DMA 直接搬运数据至远端 NPU HBM；
  - Host CPU 仅执行描述符提交与 CQ Polling。
- **路径 B（NVMe Direct SSD 直达模式）**：
  - 采用 SPDK / `io_uring` + `O_DIRECT` + Peer-to-Peer DMA（或内核 Direct I/O Bypass）；
  - 数据直接在 NVMe SSD 与 NPU HBM 之间流转，禁止 Host DDR Staging。
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

## 4. 软硬件环境与打点插桩方案

### 4.1 硬件拓扑与驱动要求
- **节点配置**：Node-0 与 Node-1，每节点配置 8× NPU (96GB HBM), 800G URMA 网卡, 4× NVMe PCIe Gen5 SSD；
- **系统支持**：Linux Kernel 6.6+, 开启 `io_uring`, 挂载 UBMEM / URMA 驱动模块。

### 4.2 eBPF 插桩与矩阵导出执行
内核监控启动：
```bash
python3 ./原型验证代码/PVT-01/host_touch_monitor.py <target_pid>
```

矩阵导出执行：
```bash
python3 ./原型验证代码/PVT-01/export_capability_matrix.py --in res_direct.csv,res_memcpy.csv --out capability_matrix.json
```

---

## 5. 分步执行测试操作规程

开发人员请按以下 12 个步骤依次执行：

### 步骤 1：启动内核 eBPF 监控
在被测节点上启动 `host_touch_monitor.py`，传入即将运行的测试进程 PID，确保实时捕获系统调用。

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
从 eBPF 输出中读取路径 A 执行期间的内核统计：
- 检查 `@memcpy_calls` 是否严格等于 0；
- 检查 `@memcpy_bytes` 是否严格等于 0；
- 统计 Host CPU 占用率（从 `/proc/[pid]/stat` 获取）。

### 步骤 6：运行路径 B（NVMe Direct SSD 直达模式）
测试 HBM ↔ NVMe SSD 直达顺序读写性能：
```bash
./raw_trans_bench --mode nvme_direct --sizes 1M,16M,64M,256M,1G --qd 1,8,32 --out res_nvme.csv
```

### 步骤 7：读取路径 B 的 eBPF 统计，验证 Host Touch
检查 NVMe 直达传输期间，Host CPU `@memcpy_calls` 与 `@memcpy_bytes` 是否为 0。

### 步骤 8：测量 Completion & Fence 异步通知延迟
在 `raw_trans_bench` 中打点测量硬件传输完成（CQE 就绪）到内存 Fence 同步屏障生效的微秒级耗时：
$$T_{fence\_sync} = T_{ready\_visible} - T_{dma\_complete}$$

### 步骤 9：比对路径 A/B 与对照组 C/D，计算吞吐与 CPU 节省
比对四组实验在相同包大小下的传输耗时与 CPU 周期开销（详见第 7 节推导公式）。

### 步骤 10：注入异常与网络拥塞扰动
使用 `tc` 限速或注入丢包，测试 URMA/UBMEM 重传机制与超时保护：
```bash
# 模拟 5% 丢包
sudo tc qdisc add dev eth0 root netem loss 5%
./raw_trans_bench --mode urma_direct --sizes 16M --qd 16 --out res_urma_loss.csv
sudo tc qdisc del dev eth0 root
```

### 步骤 11：生成并导出《硬件 CapabilityMatrix 探针能力表》
将实测数据汇总并导出为标准 JSON 探针配置文件 `capability_matrix.json`。

### 步骤 12：执行 Go / No-Go 判定与交付物打包。

---

## 6. 数据采集清单与记录格式

### 6.1 原始传输性能记录表 (`pvt01_raw_bandwidth.csv`)
| 字段名称 | 含义 | 单位 | 示例值 |
|---|---|---|---|
| `path_mode` | 传输路径模式 | 字符串 | URMA_Direct / NVMe_Direct / Host_Memcpy |
| `payload_size_kb` | 数据块大小 | KB | 16384 (16MB) |
| `queue_depth` | 队列深度 | 整数 | 16 |
| `bandwidth_gbps` | 测得有效带宽 | Gbps | 642.5 |
| `latency_p50_us` | 传输中位数时延 | 微秒 ($\mu s$) | 198.5 |
| `latency_p99_us` | 传输 P99 时延 | 微秒 ($\mu s$) | 235.0 |
| `host_cpu_pct` | Host CPU 占用率 | 百分比 | 0.8% |
| `host_memcpy_bytes`| Host CPU 拷贝正文字节数 | 字节 | 0 |
| `fence_delay_us` | 异步 Fence 同步耗时 | 微秒 ($\mu s$) | 2.1 |

---

## 7. 数据交叉组合与运算推导逻辑

### 7.1 线速达成率 (Line-Rate Efficiency)
$$\text{LineRate Efficiency} = \frac{\text{Measured Bandwidth (Gbps)}}{\text{Hardware Physical Peak Bandwidth (Gbps)}} \times 100\%$$
- 目标：800G URMA 网卡实测有效带宽 $\ge 640\text{ Gbps}$（达成率 $\ge 80\%$）；
- 目标：NVMe SSD 阵列实测顺序读带宽 $\ge 80\%$ 物理标称值。

### 7.2 Host Payload Touch 判定
$$\text{Host Touch Ratio} = \frac{\text{Host Copied Bytes}}{\text{Total Transferred Payload Bytes}} \times 100\%$$
- **判定硬指标**：对于 URMA_Direct 与 NVMe_Direct，$\text{Host Touch Ratio}$ **必须严格为 $0\%$**。

### 7.3 Host CPU 开销节约比
$$\text{CPU Saving Ratio} = \frac{\text{Host\_CPU\%}_{\text{memcpy}} - \text{Host\_CPU\%}_{\text{direct}}}{\text{Host\_CPU\%}_{\text{memcpy}}} \times 100\%$$
- 预期直接降低 CPU 负载 $\ge 90\%$，彻底消除 Host CPU 拷贝瓶颈。

---

## 8. 多维扩展与扫参矩阵

| 维度 | 参数网格 |
|---|---|
| **介质通路** | NPU HBM ↔ NPU HBM (Remote URMA), NPU HBM ↔ NPU HBM (Remote UBMEM), NPU HBM ↔ NVMe SSD |
| **数据包大小** | 64KB, 256KB, 1MB, 4MB, 16MB, 64MB, 256MB, 1GB |
| **并发深度 (QD)** | 1 (Latency bound), 4, 16, 32, 64 (Throughput bound) |
| **内存对齐** | 4KB Page 严格对齐、64B Cacheline 对齐、非对齐跨页 |

---

## 9. Go / No-Go 判定规则与交付报告模板

### 9.1 判定规则
- **Go 门槛**：
  1. URMA / UBMEM 跨节点大包传输有效带宽 $\ge 80\%$ 物理线速；
  2. NVMe SSD 直达读取带宽 $\ge 80\%$ 物理峰值；
  3. 主路径 Host Payload Touch 字节数严格为 0；
  4. CPU 节约比 $\ge 85\%$。
- **No-Go 门槛**：
  - 跨节点传输或 SSD 直达仍需 Host CPU 深度参与 memcpy；
  - 传输有效带宽低于线速的 50%。

### 9.2 开发者交付报告格式模板
```markdown
# PVT-01 提前验证交付报告

## 1. 核心传输与 CPU 触碰指标汇总
| 传输路径 | 数据块大小 | 实测带宽(Gbps) | 线速达成率 | Host Touch字节数 | CPU占用率 | 判定 |
|---|---|---|---|---|---|---|
| URMA Direct | 64MB | 682.0 | 85.25% | 0 Bytes | 0.6% | PASS |
| UBMEM Direct | 64MB | 745.0 | 93.12% | 0 Bytes | 0.4% | PASS |
| NVMe Direct | 64MB | 112.5 (14GB/s)| 87.50% | 0 Bytes | 0.8% | PASS |
| Host Memcpy (对照)| 64MB| 145.0 | 18.12% | 67108864 Bytes | 88.5% | BASELINE |

## 2. 硬件 CapabilityMatrix 探针导出片段 (`capability_matrix.json`)
```json
{
  "paths": {
    "hbm_to_remote_hbm_ubmem": {
      "bandwidth_gbps": 745.0,
      "base_latency_us": 3.2,
      "host_touch_bytes": 0
    },
    "hbm_to_local_ssd_direct": {
      "bandwidth_gbps": 112.5,
      "base_latency_us": 18.5,
      "host_touch_bytes": 0
    }
  }
}
```

## 3. 最终结论
【Go / Conditional / No-Go】: GO
```
