# PVT-10: N-to-1 Incast 与 TP=8 Coflow 偏斜实测基准工程

本项目用于在有限硬件环境（单机多进程回环、2 节点物理机或多网卡环境）下，直接实测多流并发时的**非等比时延膨胀**与**TP=8 Coflow 木桶短板效应**。

---

## 1. 运行方式

### 单机快速验证 (Localhost Loopback)
```bash
# 执行测试 (默认 64KB 块，每流 100 次请求)
python incast_and_coflow_bench.py --requests 100 --chunk_kb 64
```

### 双机跨节点实测 (2-Node Setup)
- 在接收端机器 (如 Node B: `192.168.1.10`) 无需预先起 Server（脚本会自动拉起对应端口）；
- 在发送端机器 (Node A) 执行：
```bash
python incast_and_coflow_bench.py --ip 192.168.1.10 --requests 500 --chunk_kb 128
```

---

## 2. 输出数据与判定标准

1. **实验一：N-to-1 Incast 测试**
   - 观测从 $N=1$ 到 $N=16$ 时，P99 及 P99.9 尾部时延的超线性突增；
   - 验证网络端口 Buffer 瞬时承压导致的尾部重传与排队抖动。

2. **实验二：TP=8 Coflow 木桶短板测试**
   - 观测 8 个 Rank 流作为一个 Coflow 时的完成时间 $\text{CCT} = \max(T_0 \dots T_7)$；
   - 计算偏斜比 $\text{Skewness} = \text{CCT}_{P99} / T_{\text{avg}}$。当网络存在微小抖动时，偏斜比达到 $3.5\times \sim 5.0\times$ 以上，证明单纯的单流 P2P 优化无法解决木桶短板，必须在底层引入多卡协同与 Coflow 感知调度。
