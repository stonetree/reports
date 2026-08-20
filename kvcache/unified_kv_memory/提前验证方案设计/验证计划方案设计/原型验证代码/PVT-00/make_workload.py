#!/usr/bin/env python3
"""构造 PVT-00 的 R1 预热请求和 R2 复用请求，输出统一 workload schema。"""
import argparse
import json
import random
import uuid
from pathlib import Path


def load_layout_manifest(path: str | None) -> dict | None:
    if not path:
        return None
    with Path(path).open("r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    if "kv_bytes_per_token" not in manifest:
        raise ValueError("layout manifest must contain kv_bytes_per_token")
    return manifest


def generate_workload(args: argparse.Namespace) -> None:
    random.seed(args.seed)
    prefix_seq = [random.randint(100, 30000) for _ in range(args.prefix_tokens)]
    unique_seq = [random.randint(100, 30000) for _ in range(args.unique_tokens)]
    r2_prompt = prefix_seq + unique_seq
    reuse_ratio = args.prefix_tokens / len(r2_prompt)
    layout = load_layout_manifest(args.layout_manifest)
    payload = {
        "schema_version": "pvt00.workload.v1",
        "workload_id": args.workload_id or str(uuid.uuid4()),
        "metadata": {
            "model_type": args.model_type,
            "model_id": args.model_id,
            "model_layout_manifest": args.layout_manifest,
            "kv_bytes_per_token": layout["kv_bytes_per_token"] if layout else None,
            "prefix_tokens": args.prefix_tokens,
            "unique_tokens": args.unique_tokens,
            "total_r2_tokens": len(r2_prompt),
            "reuse_ratio": reuse_ratio,
            "seed": args.seed,
        },
        "requests": [
            {"id": "R1_warmup", "prompt_tokens": prefix_seq, "max_tokens": 1},
            {"id": "R2_reuse_test", "prompt_tokens": r2_prompt, "max_tokens": 32},
        ],
    }
    with Path(args.out).open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    print(json.dumps({
        "status": "OK",
        "schema_version": payload["schema_version"],
        "workload_id": payload["workload_id"],
        "total_r2_tokens": len(r2_prompt),
        "reuse_ratio": reuse_ratio,
        "kv_bytes_per_token": payload["metadata"]["kv_bytes_per_token"],
        "output": args.out,
    }, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-type", choices=["mha", "mla", "gqa"], required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--layout-manifest", help="运行时 KV 布局清单；正式测试必须提供")
    parser.add_argument("--workload-id")
    parser.add_argument("--prefix-tokens", type=int, default=50000)
    parser.add_argument("--unique-tokens", type=int, default=50000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="workload_50pct.json")
    generate_workload(parser.parse_args())
