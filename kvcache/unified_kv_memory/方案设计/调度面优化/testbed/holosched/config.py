#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
模块名称: holosched.config
功能描述: 调度面软硬件协同测试平台核心配置、硬件常数与执行模式枚举
================================================================================
"""

from enum import IntEnum
from dataclasses import dataclass


class ExecRouteMode(IntEnum):
    """调度器路由与执行模式枚举"""
    FULL_PULL_UMR = 0         # 全量通过 CX6 静态 UMR 跨步零拷贝拉取
    UBMEM_DIRECT_READ = 1     # 远端 UBMEM 零拷贝按层直读 (带租约保护)
    HYBRID_LAYER_SPLIT = 2    # 按层混合执行：前 K* 层本地重算，后 80-K* 层流式拉取
    FALLBACK_RECOMPUTE = 3    # 拥塞或低复用度，100% 本地重算回退 (杜绝负收益倒挂)
    RELAY_TREE_BROADCAST = 4  # 1-to-8 热点前缀二叉树中继分发


@dataclass(frozen=True)
class HardwareTopologyConfig:
    """鲲鹏 920 + CX6/7 物理拓扑规格常数"""
    TOTAL_SOCKETS: int = 2
    TOTAL_SCCLS: int = 8
    SCCLS_PER_SOCKET: int = 4
    DDRC_CHANNELS_PER_SCCL: int = 2
    TOTAL_DDRC_CHANNELS: int = 16
    NOMINAL_SCCL_BW_GBPS: float = 42.0          # 单 SCCL (2 DDRC 通道) 标称可用读带宽 (GB/s)
    DUAL_RAIL_MAX_BW_GBPS: float = 80.0        # 双轨条带化峰值带宽 (GB/s)
    CONGESTION_MIN_BW_GBPS: float = 3.0         # 极端拥塞注入带宽 (GB/s)
    
    # 物理光纤环回端口映射
    COMPUTE_NS_NAME: str = "ns_compute"
    STORAGE_NS_NAME: str = "ns_storage"
    COMPUTE_DEV_PORT: str = "enp125s0f0"
    STORAGE_DEV_PORT: str = "enp125s0f1"


@dataclass(frozen=True)
class InferenceModelConfig:
    """大模型结构常数 (以 Llama-3-70B FP8/INT8 为基准)"""
    MODEL_NAME: str = "Llama-3-70B-Instruct-FP8"
    TOTAL_LAYERS: int = 80
    GQA_GROUPS: int = 8
    HIDDEN_DIM: int = 8192
    BLOCK_SIZE: int = 16
    
    # KVCache 尺寸折算 (TP=8 等效切片)
    TOTAL_BYTES_PER_TOKEN: float = 320.0 * 1024.0      # 320 KiB/Token (完整模型跨 80 层)
    BYTES_PER_TOKEN_PER_CARD: float = 40.0 * 1024.0   # 40 KiB/Token/卡 (等效 TP=8 切分)
    EQUIV_TP_RANKS: int = 8                           # 4 张物理卡 + 4 个影子拉取线程 = 8 Rank
    PHYSICAL_CARDS: int = 4                           # 单机实际配备物理计算卡数


@dataclass(frozen=True)
class LatencyBudgetConfig:
    """时延与 SLO 刚性预算门限"""
    FIXED_PATH_TF_MS: float = 5.25                    # 固定框架路径基础开销 Tf = 5.25ms
    FULL_RECOMPUTE_8K_MS: float = 240.0               # 8K 序列全量本地重算基准耗时 = 240.0ms
    TPOT_BASELINE_MS: float = 18.20                   # 前台无干扰在线 Decode 基线 TPOT = 18.20ms
    TPOT_MAX_INTERFERENCE_PCT: float = 3.0            # TPOT 干扰率刚性门限 (< 3.0%)
    TPOT_REDLINE_MS: float = 18.75                    # 3% 干扰绝对红线 (18.20 * 1.03 = 18.75ms)
    TPOT_TOLERANCE_BUDGET_MS: float = 0.55            # 全系统允许的绝对抖动预算 (550µs)
    
    # 目标阶段验收门限
    TARGET_TTFT_REDUCTION_PCT: float = 20.0           # E3 阶段 TTFT 降幅目标 >= 20%
    TARGET_GOODPUT_IMPROVEMENT_PCT: float = 10.0      # E3 阶段有效 QPS 提升目标 >= 10%
    TARGET_BG_THROUGHPUT_GBPS: float = 70.0           # E3 后台可持续换出速率 >= 70 Gbps
