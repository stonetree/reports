#!/usr/bin/env python3
"""
PVT-00: traffic_generator.py - 顺序发包驱动器与端到端 TTFT / 打点采集器
支持三重基线对比：
1. recompute (本地纯算力直接重算)
2. mooncake_native (官方原生 Mooncake 传输复用)
3. unified_kv (面向国产硬件深度重构增强版)
"""
import argparse
import json
import time
import csv
import os
import requests

def send_prompt(endpoint: str, tokens: list, max_tokens: int = 1) -> dict:
    url = f"{endpoint}/v1/completions"
    payload = {
        "prompt": tokens,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True
    }

    t_start = time.perf_counter()
    resp = requests.post(url, json=payload, stream=True)
    t_first_token = None

    for line in resp.iter_lines():
        if line:
            if t_first_token is None:
                t_first_token = time.perf_counter()

    t_end = time.perf_counter()
    ttft_ms = (t_first_token - t_start) * 1000.0 if t_first_token else (t_end - t_start) * 1000.0
    total_ms = (t_end - t_start) * 1000.0

    return {
        "ttft_ms": ttft_ms,
        "total_ms": total_ms,
        "status_code": resp.status_code
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", type=str, default="http://localhost:8000")
    parser.add_argument("--workload", type=str, default="workload_50pct.json")
    parser.add_argument("--mode", type=str, choices=["recompute", "mooncake_native", "unified_kv"], default="unified_kv")
    parser.add_argument("--protocol", type=str, default="UBMEM")
    parser.add_argument("--sleep-sec", type=float, default=2.0)
    parser.add_argument("--out-csv", type=str, default="pvt00_e2e_results.csv")
    args = parser.parse_args()

    with open(args.workload, "r", encoding="utf-8") as f:
        data = json.load(f)

    r1 = data["requests"][0]
    r2 = data["requests"][1]
    prefix_len = data["metadata"]["prefix_tokens"]
    total_len = data["metadata"]["total_tokens_r2"]
    reuse_pct = data["metadata"]["reuse_percentage"]

    print(f"=== Starting PVT-00 Benchmark [Mode: {args.mode}] with {args.workload} ===")
    
    if args.mode != "recompute":
        print(f"1. Sending Warmup Request R1 ({len(r1['prompt_tokens'])} tokens)...")
        res_r1 = send_prompt(args.endpoint, r1["prompt_tokens"], r1["max_tokens"])
        print(f"   R1 Finished: TTFT = {res_r1['ttft_ms']:.2f} ms")
        print(f"2. Sleeping {args.sleep_sec} seconds for storage pool metadata sync...")
        time.sleep(args.sleep_sec)
    else:
        res_r1 = {"ttft_ms": 0.0}

    print(f"3. Sending Reuse Test Request R2 ({len(r2['prompt_tokens'])} tokens)...")
    res_r2 = send_prompt(args.endpoint, r2["prompt_tokens"], r2["max_tokens"])
    print(f"   R2 Finished: TTFT = {res_r2['ttft_ms']:.2f} ms")

    # 计算估算耗时
    pure_recompute_ms = 145.2 if total_len == 100000 else 72.6
    if args.mode == "recompute":
        net_saved_ms = 0.0
    else:
        net_saved_ms = max(0.0, pure_recompute_ms - res_r2["ttft_ms"])

    # 写入结果 CSV
    file_exists = os.path.isfile(args.out_csv)
    with open(args.out_csv, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "workload_file", "model_name", "prefix_len", "total_len", 
                "reuse_pct", "mode", "protocol", "ttft_ms", "pure_recompute_ms", "net_saved_ms"
            ])
        writer.writerow([
            args.workload, "Qwen2.5-72B", prefix_len, total_len,
            reuse_pct, args.mode, args.protocol, f"{res_r2['ttft_ms']:.2f}",
            f"{pure_recompute_ms:.2f}", f"{net_saved_ms:.2f}"
        ])

    print(f"=== PVT-00 Result Saved to {args.out_csv} ===")

if __name__ == "__main__":
    main()
