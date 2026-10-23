#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
模块名称: holosched.scheduler
功能描述: Kunpeng-HoloSched 核心软硬件协同调度器与双时间尺度 QoS 控制器
================================================================================
"""

from dataclasses import dataclass
from typing import Optional
from holosched.config import (
    ExecRouteMode,
    InferenceModelConfig,
    LatencyBudgetConfig,
    HardwareTopologyConfig,
)
from holosched.telemetry import KVHandle, TelemetryManager
from holosched.cost_evaluator import CostEvaluator


@dataclass
class ScheduleDecision:
    """调度面输出的微秒级执行决策"""
    mode: ExecRouteMode
    recompute_layers: int          # [0, K*) 本地计算卡执行重算的层数
    pull_start_layer: int          # [K*, 80) 网络流式拉取起始层
    assigned_sccl_id: int          # 绑定的远端存储物理 SCCL 管道
    hardware_qp_priority: int      # 0: TC0 严格高优(前台), 1: TC1 微切片低优(后台)
    wqe_chunk_size_kb: int         # 前台大块聚合 (16384KB); 后台强制 <= 256KB
    predicted_ttft_ms: float       # 闭式模型预估的端到端 TTFT (ms)
    ctrl_overhead_ms: float        # 控制面与调度决策总耗时 C1+C2+C3+C4+C5 (ms)


class HierarchicalQoSController:
    """
    双时间尺度分层服务质量保障控制器 (针对专家评审改进 3)
    
    1. 快路径 (200µs, 用户态网卡流控):
       在用户态实时跟踪 DDRC flux 与在途字节，按 256KB 微切片动态调控换出注入步长，
       单次调节耗时 < 50ns，零内核系统调用；
    2. 慢路径 (20ms, 内核态 MPAM 硬件限流):
       对齐在线 Decode 单步 Token 周期 (18.2ms)，通过滑动指数加权均值 (EWMA)
       与滞后死区控制调节 /sys/fs/resctrl/bg_evict/schemata 中的 MBW_MAX，
       根除 200µs 高频写入引发的内核互斥锁争用与超调震荡。
    """
    def __init__(self, target_tpot_ms: float = 18.20, redline_tpot_ms: float = 18.75):
        self.target_tpot_ms = target_tpot_ms
        self.redline_tpot_ms = redline_tpot_ms
        self.tolerance_window_ms = redline_tpot_ms - target_tpot_ms # 0.55ms
        
        # 慢路径状态 (EWMA 过滤)
        self.ewma_tpot = target_tpot_ms
        self.current_mpam_quota_pct = 70.0  # 初始 70% 内存带宽配额
        self.last_mpam_update_time_ms = 0.0
        
        # 快路径状态 (用户态微切片令牌桶)
        self.current_micro_chunk_kb = 256
        self.fast_path_throttle_ratio = 1.0

    def evaluate_fast_path(self, current_inflight_bytes: int, avail_bw_gbps: float) -> int:
        """快路径 (200µs): 根据实时信道压力调整单次 WQE 粒度 (32KB ~ 256KB)"""
        if avail_bw_gbps < 10.0 or current_inflight_bytes > 500 * 1024 * 1024:
            self.current_micro_chunk_kb = 64
        elif current_inflight_bytes > 200 * 1024 * 1024:
            self.current_micro_chunk_kb = 128
        else:
            self.current_micro_chunk_kb = 256
        return self.current_micro_chunk_kb

    def evaluate_slow_path(self, measured_tpot_ms: float, now_ms: float) -> Optional[int]:
        """
        慢路径 (20ms): 对齐 Token 步长，更新 MPAM MBW_MAX 硬件配额。
        返回需要下发至 resctrl 的新配额百分比 (若无需调整返回 None)。
        """
        # 采样周期保护：严禁高频写入，强制 >= 18ms
        if now_ms - self.last_mpam_update_time_ms < 18.0:
            return None

        # EWMA 平滑滤波 (alpha = 0.3)
        self.ewma_tpot = 0.3 * measured_tpot_ms + 0.7 * self.ewma_tpot
        self.last_mpam_update_time_ms = now_ms

        delta_tpot = self.ewma_tpot - self.target_tpot_ms

        # 死区滞后控制：[18.35ms, 18.60ms] 保持稳定，消除抖动
        if delta_tpot > 0.36:
            # 逼近 0.55ms 红线，乘法减半限流
            self.current_mpam_quota_pct = max(15.0, self.current_mpam_quota_pct * 0.5)
            return int(self.current_mpam_quota_pct)
        elif delta_tpot < 0.15:
            # 前台极为从容，加法恢复 (每次 +5%)
            self.current_mpam_quota_pct = min(70.0, self.current_mpam_quota_pct + 5.0)
            return int(self.current_mpam_quota_pct)
        
        return None


class KunpengHoloScheduler:
    """
    面向鲲鹏 920 + CX6/7 + UBMEM 的软硬协同核心调度器。
    支持在单机 4 卡 Testbed 上运行与对比 Baseline / 方案 A / 方案 B / HoloSched 四种架构策略。
    """
    def __init__(
        self,
        telemetry_mgr: Optional[TelemetryManager] = None,
        model_cfg: InferenceModelConfig = InferenceModelConfig(),
        budget_cfg: LatencyBudgetConfig = LatencyBudgetConfig(),
    ):
        self.m = model_cfg
        self.b = budget_cfg
        self.telemetry = telemetry_mgr or TelemetryManager(num_sccls=4)
        self.cost_evaluator = CostEvaluator(self.m, self.b)
        self.qos_controller = HierarchicalQoSController(
            target_tpot_ms=self.b.TPOT_BASELINE_MS,
            redline_tpot_ms=self.b.TPOT_REDLINE_MS
        )

    def schedule_request(
        self,
        req_id: int,
        seq_len: int,
        nominal_hit_ratio: float,
        handle: KVHandle,
        policy: str = "holosched",
        custom_recompute_full_ms: float = -1.0,
    ) -> ScheduleDecision:
        """
        核心微秒级调度决策入口。
        """
        # 1. 六维语义一致性与租约校验 (ConsumeEligibility)
        if handle.valid_flags != 0x3F or handle.lease_epoch == 0:
            return ScheduleDecision(
                mode=ExecRouteMode.FALLBACK_RECOMPUTE,
                recompute_layers=self.m.TOTAL_LAYERS,
                pull_start_layer=self.m.TOTAL_LAYERS,
                assigned_sccl_id=handle.target_sccl_id,
                hardware_qp_priority=0,
                wqe_chunk_size_kb=0,
                predicted_ttft_ms=self.b.FIXED_PATH_TF_MS + self.b.FULL_RECOMPUTE_8K_MS,
                ctrl_overhead_ms=0.452,
            )

        # 2. 算力与传输参数计算
        aligned_prefix, h, pull_bytes, t_recomp_full, t_suffix = (
            self.cost_evaluator.compute_workload_bytes_and_suffix(
                seq_len, nominal_hit_ratio, custom_recompute_full_ms
            )
        )

        sccl_id = handle.target_sccl_id
        pipe = self.telemetry.pipes[sccl_id]
        avail_bw = pipe.avail_bw_gbps

        # 3. 执行不同策略的分支路径
        if policy == "baseline":
            # 基线：无预编译 UMR + 同步阻塞查表 + 无动态熔断回退
            t_ctrl_ms = 31.80
            prev_inflight = pipe.inflight_bytes
            pipe.inflight_bytes += pull_bytes # 承受盲目排队
            
            tau_actual = ((prev_inflight + pull_bytes) / (avail_bw * 1e9)) * 1000.0
            hidden_ms = 0.15 * min(tau_actual, t_suffix)
            ttft_base = self.b.FIXED_PATH_TF_MS + t_ctrl_ms + t_suffix + tau_actual - hidden_ms
            
            return ScheduleDecision(
                mode=ExecRouteMode.FULL_PULL_UMR,
                recompute_layers=0,
                pull_start_layer=0,
                assigned_sccl_id=sccl_id,
                hardware_qp_priority=0,
                wqe_chunk_size_kb=65536,
                predicted_ttft_ms=ttft_base,
                ctrl_overhead_ms=t_ctrl_ms,
            )

        elif policy == "scheme_a":
            # 方案 A (纯单边反射)：控制面极速 (0.452ms)，但无原子预扣 (看不见在途排队)
            t_ctrl_ms = 0.452
            ttft_perceived, _, _ = self.cost_evaluator.evaluate_full_pull_ttft(
                pull_bytes, 0, avail_bw, t_suffix, t_ctrl_ms
            )
            ttft_recomp = self.b.FIXED_PATH_TF_MS + t_ctrl_ms + t_recomp_full

            if ttft_perceived <= ttft_recomp:
                # 盲目拉取，但在物理信道遭遇真实的羊群排队
                real_inflight = pipe.inflight_bytes
                pipe.inflight_bytes += pull_bytes
                ttft_real, _, _ = self.cost_evaluator.evaluate_full_pull_ttft(
                    pull_bytes, real_inflight, avail_bw, t_suffix, t_ctrl_ms
                )
                return ScheduleDecision(
                    mode=ExecRouteMode.FULL_PULL_UMR,
                    recompute_layers=0,
                    pull_start_layer=0,
                    assigned_sccl_id=sccl_id,
                    hardware_qp_priority=0,
                    wqe_chunk_size_kb=16384,
                    predicted_ttft_ms=ttft_real,
                    ctrl_overhead_ms=t_ctrl_ms,
                )
            else:
                return ScheduleDecision(
                    mode=ExecRouteMode.FALLBACK_RECOMPUTE,
                    recompute_layers=self.m.TOTAL_LAYERS,
                    pull_start_layer=self.m.TOTAL_LAYERS,
                    assigned_sccl_id=sccl_id,
                    hardware_qp_priority=0,
                    wqe_chunk_size_kb=0,
                    predicted_ttft_ms=ttft_recomp,
                    ctrl_overhead_ms=t_ctrl_ms,
                )

        # 终极版 Kunpeng-HoloSched (或方案 B 中心精算师)
        t_ctrl_ms = 0.452 if policy == "holosched" else 8.50

        # [漏洞 1 修复] 模拟单边 RDMA Fetch-and-Add 原子在途预扣
        prev_inflight = self.telemetry.acquire_inflight_reservation(sccl_id, req_id, pull_bytes)

        ttft_pull_full, tau_full, _ = self.cost_evaluator.evaluate_full_pull_ttft(
            pull_bytes, prev_inflight, avail_bw, t_suffix, t_ctrl_ms
        )
        ttft_recomp_full = self.b.FIXED_PATH_TF_MS + t_ctrl_ms + t_recomp_full

        # 判断是否严格处于净加速区
        if ttft_pull_full <= ttft_recomp_full:
            return ScheduleDecision(
                mode=ExecRouteMode.FULL_PULL_UMR,
                recompute_layers=0,
                pull_start_layer=0,
                assigned_sccl_id=sccl_id,
                hardware_qp_priority=0,
                wqe_chunk_size_kb=16384,
                predicted_ttft_ms=ttft_pull_full,
                ctrl_overhead_ms=t_ctrl_ms,
            )

        # 跌入负收益风险区：寻找最佳按层混合切分点 K*
        best_k, best_hybrid_ttft = self.cost_evaluator.find_optimal_layer_split(
            pull_bytes, prev_inflight, avail_bw, t_recomp_full, t_suffix, t_ctrl_ms
        )

        if best_k < self.m.TOTAL_LAYERS and policy == "holosched":
            # 部分层重算：原子返还免于拉取的预扣配额
            released_bytes = int(pull_bytes * (best_k / float(self.m.TOTAL_LAYERS)))
            self.telemetry.release_inflight_reservation(sccl_id, req_id, released_bytes)
            return ScheduleDecision(
                mode=ExecRouteMode.HYBRID_LAYER_SPLIT,
                recompute_layers=best_k,
                pull_start_layer=best_k,
                assigned_sccl_id=sccl_id,
                hardware_qp_priority=0,
                wqe_chunk_size_kb=8192,
                predicted_ttft_ms=best_hybrid_ttft,
                ctrl_overhead_ms=t_ctrl_ms,
            )
        else:
            # 严重拥塞：100% 本地重算回退，原子归还全部预扣配额
            self.telemetry.release_inflight_reservation(sccl_id, req_id, pull_bytes)
            return ScheduleDecision(
                mode=ExecRouteMode.FALLBACK_RECOMPUTE,
                recompute_layers=self.m.TOTAL_LAYERS,
                pull_start_layer=self.m.TOTAL_LAYERS,
                assigned_sccl_id=sccl_id,
                hardware_qp_priority=0,
                wqe_chunk_size_kb=0,
                predicted_ttft_ms=ttft_recomp_full,
                ctrl_overhead_ms=t_ctrl_ms,
            )
