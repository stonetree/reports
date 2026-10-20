# 测试日志与物理数据归档目录 (Logs)

本目录用于归档控制面性能放大效应评估过程中的全链路物理测量数据。包含 PMU 硬件计数器时序数据、影子压测实例访问日志、延迟阶梯扫描原始数据以及消融实验对比日志。

---

## 一、子目录功能划分

| 子目录 | 写入程序 | 文件类型与命名规范 | 归档内容说明 |
| :--- | :--- | :--- | :--- |
| `logs/pmu/` | `scripts/monitor_pmu.py` | `pmu_metrics_*.csv` | 鲲鹏 Uncore DDRC 读写带宽、HHA 跨片监听、SMMUv3 转换延迟与 OS `compact_stall` 内存压缩时序数据（100ms 采样粒度） |
| `logs/shadow/` | `scripts/shadow_worker.py` (Docker) | `shadow_worker_*.log` | 18 个影子压测容器的并发请求发送计数、RPC 往返延迟、P99 响应耗时与异常统计 |
| `logs/sweep/` | `scripts/run_delay_sweep.py` | `sweep_results.csv`, `raw_event_ring.bin` | 81 个阶梯测试点的端到端测量结果、在线计算的三大安全裕量 ($M_{HBM}, M_{publish}, M_{route}$) 以及 ARM CNTVCT 底层二进制事件环镜像 |
| `logs/ablation/` | `scripts/run_ablation.sh` | `exp0_baseline.csv` ~ `exp3_all_opt.csv` | Exp-0 (基线)、Exp-1 (仅压测平台)、Exp-2 (仅架构重构)、Exp-3 (软硬件协同完整方案) 四组消融实验对比测量数据 |

---

## 二、日志清洗与备份策略

1. **二进制事件环保存**：`raw_event_ring.bin` 为 4M 大小无锁环形缓冲区快照，单次完整扫描生成约 128MB 数据，建议归档为 `.tar.gz`；
2. **PMU 采样率**：默认 100ms 一次采样，持续 1 小时约生成 36,000 行 CSV 记录，单文件约 3.5MB；
3. **日志排查顺序**：若压测出现异常，先检查 `logs/shadow/` 确认影子容器连接状况，再查阅 `logs/sweep/` 确认测试点推进进度。
