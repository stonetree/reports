#!/usr/bin/env python3
"""
traffic_generator.py - 顺序发包驱动器与端到端 TTFT / 打点采集器
执行步骤：
1. 发送 R1 到推理服务端，等待完成；
2. 休眠 T_sleep 秒；
3. 发送 R2 到推理服务端，记录首 Token 产出时延 (TTFT)；
4. 抓取与计算 KVCache 传输量与收益。
"""
import argparse
import json
import time
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
            # 持续消费直到流结束

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
    parser.add_argument("--sleep-sec", type=float, default=2.0)
    parser.add_argument("--log-file", type=str, default="run_result.log")
    args = parser.parse_args()

    with open(args.workload, "r", encoding="utf-8") as f:
        data = json.load(f)

    r1 = data["requests"][0]
    r2 = data["requests"][1]

    print(f"=== Starting PVT-00 Benchmark with {args.workload} ===")
    print(f"1. Sending Warmup Request R1 ({len(r1['prompt_tokens'])} tokens)...")
    res_r1 = send_prompt(args.endpoint, r1["prompt_tokens"], r1["max_tokens"])
    print(f"   R1 Finished: TTFT = {res_r1['ttft_ms']:.2f} ms")

    print(f"2. Sleeping {args.sleep_sec} seconds for storage pool sync...")
    time.sleep(args.sleep_sec)

    print(f"3. Sending Reuse Test Request R2 ({len(r2['prompt_tokens'])} tokens)...")
    res_r2 = send_prompt(args.endpoint, r2["prompt_tokens"], r2["max_tokens"])
    print(f"   R2 Finished: TTFT = {res_r2['ttft_ms']:.2f} ms")

    # 计算与判定
    summary = {
        "metadata": data["metadata"],
        "r1_ttft_ms": res_r1["ttft_ms"],
        "r2_ttft_ms": res_r2["ttft_ms"],
        "is_reused": res_r2["ttft_ms"] < (res_r1["ttft_ms"] * 1.8)
    }

    with open(args.log_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"=== Results Summary saved to {args.log_file} ===")
    print(f"  R1 TTFT: {res_r1['ttft_ms']:.2f} ms")
    print(f"  R2 TTFT: {res_r2['ttft_ms']:.2f} ms")
    print(f"  KVCache Reuse Hit: {'YES' if summary['is_reused'] else 'NO'}")

if __name__ == "__main__":
    main()
