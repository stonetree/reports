# HoloTestbed-4X: 面向 KVCache 加速的调度面软硬件协同测试验证平台

> **项目定位**：面向国产 AI 硬件的统一异构 KVCache 存储与调度底座（Unified KV Memory & Scheduling Subsystem）单机 4 卡裂变式验证平台。
> **目标服务器硬件**：双路鲲鹏 920（128 核，8 个 SCCL，16 通道 DDRC）+ 4 张计算卡 + ConnectX-6/7 双口网卡 + NVMe SSD。
> **解决的核心痛点**：在实验室单机 4 卡硬件环境下，通过物理硬隔离、外部光纤环回与双轨孪生负载放大机制，高保真复现大规模集群 TP=8（2.5 GiB）拉取、20 实例微突发与 100 Gbps 前后台混压工况，验证闭式选路、按层切分与 MPAM 硬件反压调度策略的有效性。

---

## 1. 软件工程目录结构

```text
testbed/
├── CMakeLists.txt                       # C++20 遥测守护进程工程编译脚本
├── README.md                            # 本平台快速上手与工程说明文档
├── requirements.txt                     # Python 依赖清单 (numpy, matplotlib, seaborn)
├── scripts/                             # 自动化部署与环境配置脚本
│   ├── 01_setup_kernel_hugepages.sh     # SMMUv3 直通、1GB 巨页与核隔离配置
│   ├── 02_init_kunpeng_mpam.sh          # ARM MPAM resctrl 挂载与 L3/DDRC 硬件隔离
│   ├── 03_setup_dual_netns_loopback.sh  # 双 Network Namespace 与物理光纤环回及 QoS 队列配置
│   └── 04_run_e0_e3_benchmarks.sh       # 一键运行 E0~E3 自动化评测与门限判定脚本
├── include/                             # C++ 高性能头文件
│   ├── holo_telemetry_probe.hpp         # cntvct_el0 硬件纳秒时钟、32B 黑匣子与 PMU 共享内存布局
│   └── atomic_reservation_slab.hpp      # 带租约超时与自愈防死锁机制的原子在途配额条带 (评审改进 1)
├── src/                                 # C++ 守护进程源码
│   └── telemetry_daemon.cpp             # 鲲鹏 920 带外 Uncore PMU 守护进程 (holo_pmu_daemon)
├── holosched/                           # 核心调度器与模型模块
│   ├── __init__.py
│   ├── config.py                        # 硬件规格、模型参数与 SLO 预算定义
│   ├── telemetry.py                     # 硬件遥测状态机与原子配额仲裁器
│   ├── cost_evaluator.py                # 闭式成本预估与按层混合切分寻优引擎 (Layer-Split K*)
│   └── scheduler.py                     # 核心调度决策器与双时间尺度 QoS 控制器 (评审改进 3)
├── workload/                            # 双轨孪生混合负载与混沌注入模块
│   ├── __init__.py
│   ├── twin_track_generator.py          # 真实度量轨 + 影子协程簇 + 硬件对齐 Co-flow 屏障 (评审改进 2)
│   └── chaos_blaster.py                 # 后台满载换出 (0~100Gbps) 与 SCCL 拥塞注入器
├── reconciler/                          # 全链路对账与 SLO 门限引擎
│   ├── __init__.py
│   └── flight_reconciler.py             # 黑匣子轨迹解析、微观耗时分解 (C1~C5) 与 SLO 统计
└── benchmarks/                          # 自动化测试与可视化导出
    ├── __init__.py
    └── run_all_experiments.py           # E0~E3 完整实验评测与四合一可视化大盘导出脚本
```

---

## 2. 核心架构与三大专家评审修复改进

| 专家评审问题与物理漏洞 | 潜在破坏后果 | 落地修复机制与工程实现 |
| :--- | :--- | :--- |
| **问题 1：原子预扣缺乏租约超时释放机制** | 网络丢包或异常中断导致在途配额永久泄漏（Zombie Leak），通道虚假拥塞并锁死在重算模式 | 引入基于 `cntvct_el0` 纳秒时钟的租约生命周期（默认 50ms）与带外巡检自愈回收器（`AtomicReservationSlab`），微秒级自动清退并纠正在途配额 |
| **问题 2：TP=4 真机与影子协程非对称完成时延** | 影子协程写 Host DDR 与计算卡写 HBM 速度不一致，测出的 TP=8 跨卡同步时延失真 | 构建硬件对齐的多卡联合流屏障（`HardwareAlignedCoflowBarrier`），影子线程绑定 Socket 0 PCIe RC 争用缓冲并注入 RankConsensus（P99 < 100µs）屏障 |
| **问题 3：200µs 高频写入内核 resctrl 产生严重抖动** | 产生严重内核 `rdtgroup_mutex` 互斥锁争用与 CPU 抖动，且与 18.2ms 的 Decode 周期产生采样混叠超调 | 引入**双时间尺度分层控制架构**：快路径（200µs）用户态无锁令牌桶控 WQE 切片；慢路径（20ms）对齐 Token 步长，采用 EWMA 平滑与滞后死区控制调节 MPAM 硬件限流 |

---

## 3. 快速上手运行指南

### 3.1 环境依赖准备
```bash
# 安装 Python 科学计算与绘图依赖
pip install -r requirements.txt
```

### 3.2 运行端到端自动化验证与生成评估大盘
```bash
# 运行完整 E0~E3 四阶段评测
python benchmarks/run_all_experiments.py
```
执行完毕后，控制台将输出 E0~E3 各阶段量化对账表，并在根目录下输出出版级分析大盘图：`holotestbed_4x_evaluation.png`。

### 3.3 物理硬件环境部署与编译 (需鲲鹏服务器 root 权限)
```bash
# 1. 编译带外 PMU 守护进程
mkdir -p build && cd build
cmake .. && make -j
cd ..

# 2. 依次配置内核巨页、MPAM 隔离与物理光纤环回网络
sudo bash scripts/01_setup_kernel_hugepages.sh
sudo bash scripts/02_init_kunpeng_mpam.sh
sudo bash scripts/03_setup_dual_netns_loopback.sh enp125s0f0 enp125s0f1 mlx5_0 mlx5_1

# 3. 在后台启动带外 PMU 守护进程
sudo ./build/holo_pmu_daemon &

# 4. 执行全链路压测
sudo bash scripts/04_run_e0_e3_benchmarks.sh
```
