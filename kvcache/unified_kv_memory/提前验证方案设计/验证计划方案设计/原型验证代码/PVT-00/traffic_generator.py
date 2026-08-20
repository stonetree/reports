#!/usr/bin/env python3
"""PVT-00 在线发包器：实际切换端点/代码包，采集 R2 TTFT，不生成固定性能值。"""
import argparse
import csv
import json
import os
import time
import uuid
from pathlib import Path

import requests


def send_prompt(endpoint: str, tokens: list[int], max_tokens: int) -> dict:
    started_ns = time.monotonic_ns()
    response = requests.post(
        f"{endpoint.rstrip('/')}/v1/completions",
        json={"prompt": tokens, "max_tokens": max_tokens, "temperature": 0.0, "stream": True},
        stream=True,
        timeout=600,
    )
    response.raise_for_status()
    first_token_ns = None
    for raw_line in response.iter_lines():
        line = raw_line.decode("utf-8", errors="replace").strip()
        if line and line not in {"data: [DONE]", "[DONE]"} and first_token_ns is None:
            first_token_ns = time.monotonic_ns()
    ended_ns = time.monotonic_ns()
    if first_token_ns is None:
        raise RuntimeError("response completed without a first-token event")
    return {
        "ttft_ms": (first_token_ns - started_ns) / 1_000_000,
        "total_ms": (ended_ns - started_ns) / 1_000_000,
        "status_code": response.status_code,
    }


def wait_until_ready(args: argparse.Namespace) -> None:
    if args.ready_url:
        deadline = time.monotonic() + args.ready_timeout_sec
        while time.monotonic() < deadline:
            try:
                if requests.get(args.ready_url, timeout=2).ok:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.2)
        raise TimeoutError(f"cache readiness event timed out: {args.ready_url}")
    if args.ready_file:
        deadline = time.monotonic() + args.ready_timeout_sec
        while time.monotonic() < deadline:
            if Path(args.ready_file).exists():
                return
            time.sleep(0.2)
        raise TimeoutError(f"cache readiness file timed out: {args.ready_file}")
    if args.evidence_level != "DEMO":
        raise RuntimeError("LAB/MEASURED requires --ready-url or --ready-file")
    time.sleep(args.demo_sleep_sec)


def load_recompute_baseline(path: str | None) -> float | None:
    if not path:
        return None
    with Path(path).open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    value = data.get("ttft_ms")
    if value is None:
        raise ValueError("recompute baseline JSON must contain ttft_ms")
    return float(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True, help="与 mode 对应的实际代码包服务端点")
    parser.add_argument("--workload", required=True)
    parser.add_argument("--mode", choices=["recompute", "mooncake_native", "unified_single", "unified_full"], required=True)
    parser.add_argument("--protocol", default="unknown")
    parser.add_argument("--actual-path", required=True)
    parser.add_argument("--package-id", required=True)
    parser.add_argument("--config-hash", required=True)
    parser.add_argument("--hardware-profile", required=True)
    parser.add_argument("--evidence-level", choices=["DEMO", "LAB", "MEASURED"], default="DEMO")
    parser.add_argument("--run-id", default=str(uuid.uuid4()))
    parser.add_argument("--ready-url")
    parser.add_argument("--ready-file")
    parser.add_argument("--ready-timeout-sec", type=float, default=60.0)
    parser.add_argument("--demo-sleep-sec", type=float, default=2.0)
    parser.add_argument("--recompute-baseline-json")
    parser.add_argument("--out-csv", default="pvt00_e2e_results.csv")
    parser.add_argument("--out-json", help="将本次记录同时写为单条 JSON，便于后续模式直接引用重算基线")
    args = parser.parse_args()

    with Path(args.workload).open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    if data.get("schema_version") != "pvt00.workload.v1":
        raise ValueError("unsupported workload schema")
    metadata = data["metadata"]
    if args.evidence_level != "DEMO" and metadata.get("kv_bytes_per_token") is None:
        raise ValueError("LAB/MEASURED requires kv_bytes_per_token from runtime layout manifest")

    requests_by_id = {item["id"]: item for item in data["requests"]}
    if args.mode != "recompute":
        warmup = requests_by_id["R1_warmup"]
        send_prompt(args.endpoint, warmup["prompt_tokens"], warmup["max_tokens"])
        wait_until_ready(args)
    reuse = requests_by_id["R2_reuse_test"]
    result = send_prompt(args.endpoint, reuse["prompt_tokens"], reuse["max_tokens"])
    recompute_ttft_ms = result["ttft_ms"] if args.mode == "recompute" else load_recompute_baseline(args.recompute_baseline_json)
    net_saved_ms = None if recompute_ttft_ms is None else recompute_ttft_ms - result["ttft_ms"]
    row = {
        "run_id": args.run_id,
        "workload_id": data["workload_id"],
        "model_id": metadata["model_id"],
        "model_type": metadata["model_type"],
        "kv_bytes_per_token": metadata.get("kv_bytes_per_token"),
        "prefix_tokens": metadata["prefix_tokens"],
        "total_r2_tokens": metadata["total_r2_tokens"],
        "reuse_ratio": metadata["reuse_ratio"],
        "mode": args.mode,
        "protocol": args.protocol,
        "actual_path": args.actual_path,
        "package_id": args.package_id,
        "config_hash": args.config_hash,
        "hardware_profile": args.hardware_profile,
        "evidence_level": args.evidence_level,
        "ttft_ms": round(result["ttft_ms"], 4),
        "recompute_ttft_ms": None if recompute_ttft_ms is None else round(recompute_ttft_ms, 4),
        "net_saved_ms": None if net_saved_ms is None else round(net_saved_ms, 4),
        "status": "OK",
    }
    exists = os.path.isfile(args.out_csv)
    with Path(args.out_csv).open("a", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        if not exists:
            writer.writeheader()
        writer.writerow(row)
    if args.out_json:
        with Path(args.out_json).open("w", encoding="utf-8") as stream:
            json.dump(row, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
