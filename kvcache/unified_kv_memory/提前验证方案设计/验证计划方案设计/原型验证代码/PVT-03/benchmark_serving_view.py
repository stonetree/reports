#!/usr/bin/env python3
"""
benchmark_serving_view.py - 在推理服务上对比 View vs Copy 模式对 Prefill TTFT 与 Decode TPOT 的影响
当前脚本输出 DEMO 级合成数据，用于展示结果 Schema；不能作为性能或故障安全实测证据。
"""
import argparse
import json
import math
import random


def percentile(values: list[float], pct: float) -> float:
    """线性插值分位数，避免 DEMO 工作流依赖额外数值库。"""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one sample")
    rank = (len(ordered) - 1) * pct / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)

def simulate_serving(mode: str, prompt_len: int, decode_tokens: int, out_file: str, seed: int):
    rng = random.Random(seed)
    print(f"=== Running Serving Benchmark: Mode = {mode} ===")
    # 模拟 Prefill TTFT
    if mode == "view":
        # Prefill View: 省去拉取拷贝时间
        ttft_ms = 8.5 + (prompt_len / 4096.0) * 2.0
    else:
        # Prefill Copy: 需先拉取到本地显存
        ttft_ms = 12.8 + (prompt_len / 4096.0) * 2.0

    # 模拟 Decode 阶段逐 Token 耗时
    tpot_list = []
    for step in range(decode_tokens):
        if mode == "view":
            # 每次 Attention 跨总线远端读取，合成较大的长尾开销
            base_tpot = 28.0 + rng.expovariate(1.0 / 15.0)
        else:
            # 本地高速 HBM 读取: 延迟低且平稳
            base_tpot = 12.5 + rng.normalvariate(0.5, 0.2)
        tpot_list.append(base_tpot)

    p50_tpot = percentile(tpot_list, 50)
    p90_tpot = percentile(tpot_list, 90)
    p99_tpot = percentile(tpot_list, 99)

    res = {
        "mode": mode,
        "prompt_len": prompt_len,
        "decode_tokens": decode_tokens,
        "ttft_ms": round(ttft_ms, 2),
        "tpot_p50_ms": round(p50_tpot, 2),
        "tpot_p90_ms": round(p90_tpot, 2),
        "tpot_p99_ms": round(p99_tpot, 2),
        "evidence_level": "DEMO",
        "status": "DEMO_ONLY",
        "seed": seed
    }

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)

    print(f"Result -> TTFT: {res['ttft_ms']} ms, TPOT P50: {res['tpot_p50_ms']} ms, P99: {res['tpot_p99_ms']} ms")
    print(f"Saved to {out_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", type=str, choices=["view", "copy"], default="copy")
    parser.add_argument("--prompt-len", type=int, default=32768)
    parser.add_argument("--decode-tokens", type=int, default=256)
    parser.add_argument("--output", type=str, default="res_serving.json")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    simulate_serving(args.mode, args.prompt_len, args.decode_tokens, args.output, args.seed)
