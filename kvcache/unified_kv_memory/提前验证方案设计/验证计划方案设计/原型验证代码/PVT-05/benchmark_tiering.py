#!/usr/bin/env python3
"""
benchmark_tiering.py - 150% ~ 200% HBM 超载长上下文分层测试驱动脚本
输出 DEMO 级固定样例，展示四组对照和结果 Schema；不作为容量或性能实测证据。
"""
import argparse
import json
import time

def run_tiering_workload(mode: str, overcommit: float, concurrency: int, out_file: str):
    print(f"=== Running Tiering Workflow: Mode = {mode}, Overcommit = {overcommit*100:.0f}%, Concurrency = {concurrency} ===")
    total_requests = 500
    base_hbm_capacity_requests = int(total_requests / overcommit) # 333 requests

    if mode == "pure_hbm":
        # 纯 HBM 模式：无外挂存储，超出水位直接 OOM 与抢占
        completed = base_hbm_capacity_requests
        oom_count = int((total_requests - completed) * 0.7)
        preempt_count = total_requests - completed - oom_count
        tokens_served_m = 20.4
        cpu_pct = 1.2
        ddr_gb = 4.2
    elif mode == "mooncake_native_ssd":
        completed, oom_count, preempt_count = 460, 8, 32
        tokens_served_m, cpu_pct, ddr_gb = 29.8, 8.0, 12.0
    elif mode == "hbm_ddr_tier":
        # DDR 中转模式：由 CPU 负责换入换出，极耗 CPU
        completed = 475
        oom_count = 0
        preempt_count = 25
        tokens_served_m = 31.8
        cpu_pct = 65.4
        ddr_gb = 128.0
    else: # hbm_ssd_direct
        # SSD 直达模式：Direct I/O，Payload Bypass DDR
        completed = 492
        oom_count = 0
        preempt_count = 8
        tokens_served_m = 33.2
        cpu_pct = 1.8
        ddr_gb = 5.1

    summary = {
        "mode": mode,
        "overcommit_ratio": overcommit,
        "concurrency": concurrency,
        "total_requests": total_requests,
        "completed_requests": completed,
        "oom_count": oom_count,
        "preempt_count": preempt_count,
        "tokens_served_million": tokens_served_m,
        "host_cpu_pct": cpu_pct,
        "host_ddr_usage_gb": ddr_gb,
        "host_payload_touch_bytes": None,
        "evidence_level": "DEMO",
        "status": "DEMO_ONLY"
    }

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"Summary -> Completed: {completed}/{total_requests}, OOM: {oom_count}, Preempts: {preempt_count}")
    print(f"  Tokens Served: {tokens_served_m}M, CPU: {cpu_pct}%, DDR: {ddr_gb}GB")
    print(f"Saved to {out_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", type=str, choices=["pure_hbm", "mooncake_native_ssd", "hbm_ddr_tier", "hbm_ssd_direct"], default="hbm_ssd_direct")
    parser.add_argument("--overcommit", type=float, default=1.5)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--out", type=str, default="res_tier_summary.json")
    args = parser.parse_args()

    run_tiering_workload(args.mode, args.overcommit, args.concurrency, args.out)
