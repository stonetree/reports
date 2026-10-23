#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
模块名称: holosched.cost_evaluator
功能描述: 调度面闭式成本预估模型 (CostEvaluator) 与按层混合切分寻优引擎
================================================================================
"""

from typing import Tuple
from holosched.config import InferenceModelConfig, LatencyBudgetConfig


class CostEvaluator:
    """
    闭式成本预估模型与收益边界决策器。
    严密计算拉取与加载总开销 vs 本地直接重算耗时，确保仅在获取明确净加速收益时执行加载。
    """
    def __init__(
        self,
        model_cfg: InferenceModelConfig = InferenceModelConfig(),
        budget_cfg: LatencyBudgetConfig = LatencyBudgetConfig(),
    ):
        self.m = model_cfg
        self.b = budget_cfg

    def compute_workload_bytes_and_suffix(
        self,
        seq_len: int,
        nominal_hit_ratio: float,
        custom_recompute_full_ms: float = -1.0,
    ) -> Tuple[int, float, float, float]:
        """
        根据序列长度与前缀命中率计算 16-Token 对齐后的拉取字节数与计算耗时。
        
        返回:
            aligned_prefix: 对齐后的前缀 Token 数
            h: 实际对齐命中率
            pull_bytes_total: 8 卡等效拉取总字节数 (Bytes)
            t_recompute_full: 全量本地重算耗时 (ms)
            t_suffix: 未命中的后缀计算耗时 Ts (ms)
        """
        aligned_prefix = (int(seq_len * nominal_hit_ratio) // self.m.BLOCK_SIZE) * self.m.BLOCK_SIZE
        h = aligned_prefix / float(seq_len)
        pull_bytes_total = int(aligned_prefix * self.m.BYTES_PER_TOKEN_PER_CARD * self.m.EQUIV_TP_RANKS)

        if custom_recompute_full_ms > 0:
            t_recompute_full = custom_recompute_full_ms
            t_suffix = max(15.0, t_recompute_full * (1.0 - h * h))
        else:
            scale_len = seq_len / 8192.0
            t_recompute_full = self.b.FULL_RECOMPUTE_8K_MS * (scale_len ** 2)
            t_suffix = t_recompute_full * (1.0 - h * h)

        return aligned_prefix, h, pull_bytes_total, t_recompute_full, t_suffix

    def evaluate_full_pull_ttft(
        self,
        pull_bytes_total: int,
        prev_inflight_bytes: int,
        avail_bw_gbps: float,
        t_suffix: float,
        t_ctrl_ms: float,
    ) -> Tuple[float, float, float]:
        """
        评估全量拉取下的端到端预估 TTFT。
        
        返回:
            ttft_pull: 全量拉取端到端 TTFT (ms)
            tau_ms: 物理信道总传输耗时 (含在途排队) (ms)
            hidden_ms: 异步 UMR 跨步流水与计算掩盖省下的耗时 (ms)
        """
        effective_bytes = prev_inflight_bytes + pull_bytes_total
        tau_ms = (effective_bytes / (avail_bw_gbps * 1e9)) * 1000.0
        
        # 80 层流水重叠掩盖：重叠比例上限 (79/80)
        layer_overlap_limit = (float(self.m.TOTAL_LAYERS - 1) / float(self.m.TOTAL_LAYERS)) * min(tau_ms, t_suffix)
        hidden_ms = min(0.60 * tau_ms, layer_overlap_limit)
        
        ttft_pull = self.b.FIXED_PATH_TF_MS + t_ctrl_ms + t_suffix + tau_ms - hidden_ms
        return ttft_pull, tau_ms, hidden_ms

    def find_optimal_layer_split(
        self,
        pull_bytes_total: int,
        prev_inflight_bytes: int,
        avail_bw_gbps: float,
        t_recompute_full: float,
        t_suffix: float,
        t_ctrl_ms: float,
        step_layers: int = 8,
    ) -> Tuple[int, float]:
        """
        当全量拉取因信道拥塞跌入负收益区时，以第一性原理寻优按层混合切分最佳分界点 K*。
        
        决策逻辑：
        前 K* 层在本地计算卡重算，后 (80 - K*) 层通过网络流式拉取。
        返回:
            best_k: 最优重算层数 K* (0~80)
            best_ttft: 混合执行下的端到端预估 TTFT (ms)
        """
        best_ttft = self.b.FIXED_PATH_TF_MS + t_ctrl_ms + t_recompute_full
        best_k = self.m.TOTAL_LAYERS

        for k in range(step_layers, self.m.TOTAL_LAYERS, step_layers):
            frac_pull = (self.m.TOTAL_LAYERS - k) / float(self.m.TOTAL_LAYERS)
            frac_recomp = k / float(self.m.TOTAL_LAYERS)

            # 仅拉取后半段层，所需带宽与在途传输按比例缩减
            tau_partial = ((prev_inflight_bytes + pull_bytes_total * frac_pull) / (avail_bw_gbps * 1e9)) * 1000.0
            t_comp_hybrid = frac_recomp * t_recompute_full + frac_pull * t_suffix
            
            overlap_ratio = (self.m.TOTAL_LAYERS - k - 1) / float(self.m.TOTAL_LAYERS - k)
            hidden_partial = min(0.60 * tau_partial, overlap_ratio * min(tau_partial, t_comp_hybrid))
            
            candidate_ttft = self.b.FIXED_PATH_TF_MS + t_ctrl_ms + t_comp_hybrid + tau_partial - hidden_partial
            if candidate_ttft < best_ttft:
                best_ttft = candidate_ttft
                best_k = k

        return best_k, best_ttft
