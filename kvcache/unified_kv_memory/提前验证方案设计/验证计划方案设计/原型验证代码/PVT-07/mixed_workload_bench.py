#!/usr/bin/env python3
"""
mixed_workload_bench.py - 前后台混流压测驱动套件
前台：多并发在线会话流（严格 SLO：Decode TPOT < 20ms）
后台：高吞吐 KV 数据块预取/回源/迁移流（打满网络/存储带宽）
"""
import argparse
import asyncio
import json
import math
from pathlib import Path
import time
import random


def percentile(values: list[float], pct: float) -> float:
    """线性插值分位数，供无第三方依赖的 DEMO 工作流使用。"""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one sample")
    rank = (len(ordered) - 1) * pct / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)

async def foreground_decode_client(client_id: int, duration_sec: int, qos_enabled: bool, results: list, seed: int):
    rng = random.Random(seed + client_id)
    t_end = time.time() + duration_sec
    while time.time() < t_end:
        # 模拟生成 1 个 Token 的耗时
        if not qos_enabled:
            # 混压无隔离：遭遇后台 I/O 抢占与总线争用
            tpot = 14.0 + rng.expovariate(1.0 / 15.0)
        else:
            # 开启 SemanticQoS：高优先级保证
            tpot = 14.2 + rng.normalvariate(1.5, 0.4)

        results.append(tpot)
        await asyncio.sleep(0.015) # 模拟每 15ms 一次 Decode Step

async def background_io_worker(worker_id: int, duration_sec: int, qos_enabled: bool, throughput_stats: list):
    t_end = time.time() + duration_sec
    while time.time() < t_end:
        chunk_mb = 64
        # 模拟 64MB 传输耗时
        xfer_sec = 0.0016 # 约 320 Gbps
        if qos_enabled:
            # 前台优先时的微秒级避让
            xfer_sec *= 1.15

        throughput_stats.append(chunk_mb)
        await asyncio.sleep(xfer_sec)

async def main_bench(fg_clients: int, bg_workers: int, duration_sec: int, qos_enabled: bool, seed: int):
    print(f"=== Starting Mixed Workload Benchmark: FG Clients = {fg_clients}, BG Workers = {bg_workers}, QoS = {qos_enabled} ===")
    fg_results = []
    bg_stats = []

    tasks = []
    for i in range(fg_clients):
        tasks.append(foreground_decode_client(i, duration_sec, qos_enabled, fg_results, seed))
    for i in range(bg_workers):
        tasks.append(background_io_worker(i, duration_sec, qos_enabled, bg_stats))

    await asyncio.gather(*tasks)

    p50 = percentile(fg_results, 50)
    p90 = percentile(fg_results, 90)
    p99 = percentile(fg_results, 99)
    bg_total_gb = sum(bg_stats) / 1024.0
    bg_bw_gbps = (bg_total_gb * 8.0) / duration_sec

    print(f"Results -> Foreground Decode Count: {len(fg_results)}")
    print(f"  TPOT P50: {p50:.2f} ms, P90: {p90:.2f} ms, P99: {p99:.2f} ms")
    print(f"  Background Total I/O: {bg_total_gb:.2f} GB ({bg_bw_gbps:.2f} Gbps)")

    return {
        "qos_enabled": qos_enabled,
        "fg_samples": len(fg_results),
        "tpot_p50_ms": round(p50, 2),
        "tpot_p90_ms": round(p90, 2),
        "tpot_p99_ms": round(p99, 2),
        "bg_bandwidth_gbps": round(bg_bw_gbps, 2),
        "evidence_level": "DEMO",
        "status": "DEMO_ONLY",
        "seed": seed
    }

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fg-clients", type=int, default=32)
    parser.add_argument("--bg-workers", type=int, default=4)
    parser.add_argument("--duration", type=int, default=5)
    parser.add_argument("--qos", action="store_true")
    parser.add_argument("--out", default="mixed_workload_demo.json")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    result = asyncio.run(main_bench(args.fg_clients, args.bg_workers, args.duration, args.qos, args.seed))
    Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
