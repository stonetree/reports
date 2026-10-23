#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
模块名称: workload.chaos_blaster
功能描述: 数据面混沌与后台大流量换出注入器 (DMA-Chaos-Blaster)
================================================================================
"""

import time
from typing import Optional
from holosched.telemetry import TelemetryManager
from holosched.config import HardwareTopologyConfig


class DMAChaosBlaster:
    """
    数据面混沌与后台满载换出流量注入器。
    
    核心功能：
    1. 在隔离核 (Core 60~63) 上注入 0 ~ 100 Gbps 的后台数据换出混压；
    2. 支持微切片粒度控制 (优化前 64MB 大块 vs 优化后 256KB 微切片)；
    3. 支持动态注入目标 SCCL 通道拥塞故障 (可用带宽从 42 GB/s 骤降至 3~6.5 GB/s)。
    """
    def __init__(
        self,
        telemetry_mgr: TelemetryManager,
        hw_cfg: HardwareTopologyConfig = HardwareTopologyConfig(),
    ):
        self.telemetry = telemetry_mgr
        self.hw = hw_cfg
        self.bg_active = False
        self.target_bg_rate_gbps = 0.0
        self.wqe_chunk_size_kb = 256
        self.injected_congested_sccl: Optional[int] = None

    def start_background_eviction_pressure(
        self,
        rate_gbps: float = 100.0,
        chunk_size_kb: int = 256,
    ) -> None:
        """
        开启后台换出压力注入。
        
        参数:
            rate_gbps: 目标换出速率 (0 ~ 100 Gbps)
            chunk_size_kb: 单个 WQE 发送切片粒度 (65536KB: 优化前基线; 256KB: 优化后)
        """
        self.bg_active = True
        self.target_bg_rate_gbps = rate_gbps
        self.wqe_chunk_size_kb = chunk_size_kb

        # 将换出压力均匀分散至 Socket 1 的 4 个存储域 SCCL 管道
        per_sccl_rate = rate_gbps / float(self.telemetry.num_sccls)
        for sccl_id in range(self.telemetry.num_sccls):
            pipe = self.telemetry.pipes[sccl_id]
            pipe.active_bg_evict_gbps = per_sccl_rate
            # 若使用 64MB 大块，引发严重队头阻塞与 DDRC 争用
            penalty_coeff = 0.55 if chunk_size_kb > 256 else 0.25
            pipe.avail_bw_gbps = max(1.0, 42.0 - (per_sccl_rate / 8.0) * penalty_coeff)

    def stop_background_eviction_pressure(self) -> None:
        """停止后台换出混压"""
        self.bg_active = False
        self.target_bg_rate_gbps = 0.0
        for sccl_id in range(self.telemetry.num_sccls):
            pipe = self.telemetry.pipes[sccl_id]
            pipe.active_bg_evict_gbps = 0.0
            if self.injected_congested_sccl != sccl_id:
                pipe.avail_bw_gbps = 42.0

    def inject_channel_congestion(
        self,
        target_sccl_id: int = 0,
        degraded_bw_gbps: float = 3.0,
    ) -> None:
        """
        向特定 SCCL 通道注入突发拥塞故障，检验调度器按层切分与回退重算自愈能力。
        """
        self.injected_congested_sccl = target_sccl_id
        if target_sccl_id in self.telemetry.pipes:
            self.telemetry.pipes[target_sccl_id].avail_bw_gbps = degraded_bw_gbps

    def clear_channel_congestion(self, target_sccl_id: int = 0) -> None:
        """恢复信道正常带宽"""
        if self.injected_congested_sccl == target_sccl_id:
            self.injected_congested_sccl = None
        if target_sccl_id in self.telemetry.pipes:
            base_bw = 42.0
            if self.bg_active:
                per_sccl_rate = self.target_bg_rate_gbps / float(self.telemetry.num_sccls)
                base_bw -= (per_sccl_rate / 8.0) * 0.25
            self.telemetry.pipes[target_sccl_id].avail_bw_gbps = max(1.0, base_bw)
