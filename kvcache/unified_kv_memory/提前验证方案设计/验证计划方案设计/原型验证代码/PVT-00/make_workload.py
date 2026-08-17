#!/usr/bin/env python3
"""
make_workload.py - 构造 R1 (前置预热) 与 R2 (复用测试) 请求数据集
支持指定前缀 Token 数与独立 Token 数，构造 30%, 50%, 70%, 90%, 98% 复用率场景。
"""
import argparse
import json
import random

def generate_workload(prefix_tokens: int, unique_tokens: int, output_file: str):
    random.seed(42)
    # 构造确定性的公共前缀 Token 序列 (1000 ~ 50000)
    prefix_seq = [random.randint(100, 30000) for _ in range(prefix_tokens)]
    # 构造 R2 独有的后半段 Token 序列
    unique_seq = [random.randint(100, 30000) for _ in range(unique_tokens)]

    r1_prompt = prefix_seq
    r2_prompt = prefix_seq + unique_seq

    reuse_ratio = prefix_tokens / len(r2_prompt)

    payload = {
        "metadata": {
            "prefix_tokens": prefix_tokens,
            "unique_tokens": unique_tokens,
            "total_r2_tokens": len(r2_prompt),
            "reuse_ratio": f"{reuse_ratio * 100:.1f}%"
        },
        "requests": [
            {
                "id": "R1_warmup",
                "prompt_tokens": r1_prompt,
                "max_tokens": 1,
                "description": "Pre-warming prefix cache"
            },
            {
                "id": "R2_reuse_test",
                "prompt_tokens": r2_prompt,
                "max_tokens": 32,
                "description": "Evaluating TTFT with prefix cache hit"
            }
        ]
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"Generated workload saved to {output_file}")
    print(f"  R1 Tokens: {len(r1_prompt)}, R2 Tokens: {len(r2_prompt)}, Reuse Ratio: {reuse_ratio * 100:.1f}%")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix-tokens", type=int, default=50000, help="Prefix token length")
    parser.add_argument("--unique-tokens", type=int, default=50000, help="Unique token length for R2")
    parser.add_argument("--out", type=str, default="workload_50pct.json", help="Output JSON file")
    args = parser.parse_args()

    generate_workload(args.prefix_tokens, args.unique_tokens, args.out)
