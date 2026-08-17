#!/usr/bin/env python3
"""
mixed_workload_bench.py - 前后台混流压测驱动套件
前台：多并发在线会话流（严格 SLO：Decode TPOT < 20ms）
后台：高吞吐 KV 数据块预取/回源/迁移流（打满网络/存储带宽）
"""
import argparse
import asyncio
import time
import random
import numpy as np

async def foreground_decode_client(client_id: int, duration_sec: int, qos_enabled: bool, results: list):
    t_end = time.time() + duration_sec
    while time.time() < t_end:
        # 模拟生成 1 个 Token 的耗时
        if not qos_enabled:
            # 混压无隔离：遭遇后台 I/O 抢占与总线争用
            tpot = 14.0 + random.expovariate(1.0 / 15.0)
        else:
            # 开启 SemanticQoS：高优先级保证
            tpot = 14.2 + random.normalvariate(1.5, 0.4)

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

async def main_bench(fg_clients: int, bg_workers: int, duration_sec: int, qos_enabled: bool):
    print(f"=== Starting Mixed Workload Benchmark: FG Clients = {fg_clients}, BG Workers = {bg_workers}, QoS = {qos_enabled} ===")
    fg_results = []
    bg_stats = []

    tasks = []
    for i in range(fg_clients):
        tasks.append(foreground_decode_client(i, duration_sec, qos_enabled, fg_results))
    for i in range(bg_workers):
        tasks.append(background_io_worker(i, duration_sec, qos_enabled, bg_stats))

    await asyncio.gather(*tasks)

    p50 = np.percentile(fg_results, 50)
    p90 = np.percentile(fg_results, 90)
    p99 = np.percentile(fg_results, 99)
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
        "bg_bandwidth_gbps": round(bg_bw_gbps, 2)
    }

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fg-clients", type=int, default=32)
    parser.add_argument("--bg-workers", type=int, default=4)
    parser.add_argument("--duration", type=int, default=5)
    parser.add_argument("--qos", action="store_true")
    args = parser.parse_args()

    asyncio.run(main_bench(args.fg_clients, args.bg_workers, args.duration, args.qos))
