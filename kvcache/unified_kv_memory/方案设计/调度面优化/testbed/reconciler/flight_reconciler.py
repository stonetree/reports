#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
模块名称: reconciler.flight_reconciler
功能描述: 黑匣子全链路对账解析器 (FlightReconciler) 与 SLO 判定门限引擎
================================================================================
"""

import numpy as np
from dataclasses import dataclass
from typing import List, Dict, Any, Tuple
from holosched.config import LatencyBudgetConfig


@dataclass
class ReconciledReport:
    """全链路对账报告聚合结构体"""
    total_requests: int
    mean_ttft_ms: float
    p90_ttft_ms: float
    p99_ttft_ms: float
    mean_tpot_ms: float
    p99_tpot_ms: float
    tpot_interference_pct: float
    negative_benefit_rate_pct: float
    c1_hash_mean_us: float
    c2_lookup_mean_us: float
    c3_route_mean_ns: float
    c4_sync_p99_us: float
    c5_doorbell_mean_us: float
    e3_ttft_reduction_passed: bool
    e3_tpot_interference_passed: bool


class FlightReconciler:
    """
    黑匣子全链路对账解析器。
    读取压测采集的数据点，对账微观指标 (C1~C5) 与宏观业务指标 (TTFT/TPOT/Goodput)。
    """
    def __init__(self, budget_cfg: LatencyBudgetConfig = LatencyBudgetConfig()):
        self.b = budget_cfg

    def reconcile_records(
        self,
        records: List[Dict[str, Any]],
        baseline_ttft_ms: float = 187.15,
        baseline_tpot_ms: float = 18.20,
    ) -> ReconciledReport:
        """
        解析并输出完整对账指标
        """
        if not records:
            return ReconciledReport(
                total_requests=0,
                mean_ttft_ms=0.0,
                p90_ttft_ms=0.0,
                p99_ttft_ms=0.0,
                mean_tpot_ms=0.0,
                p99_tpot_ms=0.0,
                tpot_interference_pct=0.0,
                negative_benefit_rate_pct=0.0,
                c1_hash_mean_us=0.0,
                c2_lookup_mean_us=0.0,
                c3_route_mean_ns=0.0,
                c4_sync_p99_us=0.0,
                c5_doorbell_mean_us=0.0,
                e3_ttft_reduction_passed=False,
                e3_tpot_interference_passed=False,
            )

        ttft_arr = np.array([r.get("actual_ttft_ms", 0.0) for r in records])
        tpot_arr = np.array([r.get("actual_tpot_ms", 18.20) for r in records])
        c1_arr = np.array([r.get("c1_hash_us", 0.0) for r in records])
        c2_arr = np.array([r.get("c2_lookup_us", 0.0) for r in records])
        c3_arr = np.array([r.get("c3_route_ns", 0.0) for r in records])
        c4_arr = np.array([r.get("c4_sync_us", 0.0) for r in records])
        c5_arr = np.array([r.get("c5_doorbell_us", 0.0) for r in records])

        mean_ttft = float(np.mean(ttft_arr))
        p90_ttft = float(np.percentile(ttft_arr, 90))
        p99_ttft = float(np.percentile(ttft_arr, 99))

        mean_tpot = float(np.mean(tpot_arr))
        p99_tpot = float(np.percentile(tpot_arr, 99))

        # TPOT 干扰率计算
        tpot_interf_pct = max(0.0, (mean_tpot - baseline_tpot_ms) / baseline_tpot_ms * 100.0)

        # 统计负收益请求比例 (实测延迟大于纯重算基线的比例)
        recomp_threshold = baseline_ttft_ms * 1.3
        neg_count = np.sum(ttft_arr > recomp_threshold)
        neg_rate_pct = (float(neg_count) / float(len(ttft_arr))) * 100.0

        # E3 门限判定
        ttft_reduction_pct = (baseline_ttft_ms - mean_ttft) / baseline_ttft_ms * 100.0
        e3_ttft_pass = (ttft_reduction_pct >= self.b.TARGET_TTFT_REDUCTION_PCT)
        e3_tpot_pass = (tpot_interf_pct < self.b.TPOT_MAX_INTERFERENCE_PCT) and (p99_tpot <= self.b.TPOT_REDLINE_MS)

        return ReconciledReport(
            total_requests=len(records),
            mean_ttft_ms=mean_ttft,
            p90_ttft_ms=p90_ttft,
            p99_ttft_ms=p99_ttft,
            mean_tpot_ms=mean_tpot,
            p99_tpot_ms=p99_tpot,
            tpot_interference_pct=tpot_interf_pct,
            negative_benefit_rate_pct=neg_rate_pct,
            c1_hash_mean_us=float(np.mean(c1_arr)),
            c2_lookup_mean_us=float(np.mean(c2_arr)),
            c3_route_mean_ns=float(np.mean(c3_arr)),
            c4_sync_p99_us=float(np.percentile(c4_arr, 99)),
            c5_doorbell_mean_us=float(np.mean(c5_arr)),
            e3_ttft_reduction_passed=e3_ttft_pass,
            e3_tpot_interference_passed=e3_tpot_pass,
        )
