#!/usr/bin/env python3
"""
parse_benchmark_metrics.py - 解析 vLLM benchmark_serving 输出 JSON 并输出对账汇总 CSV
提取 QPS, TTFT (P50, P90, P99), TPOT (P50, P90, P99) 与缓存复用命中率
"""
import argparse
import glob
import json
import os
import csv

def parse_metrics(results_dir: str, output_csv: str):
    json_files = glob.glob(os.path.join(results_dir, "bench_serving_rate_*.json"))
    if not json_files:
        print(f"No result JSON files found in {results_dir}")
        return

    rows = []
    for jf in sorted(json_files):
        with open(jf, "r", encoding="utf-8") as f:
            data = json.load(f)

        request_rate = data.get("request_rate", "N/A")
        actual_qps = data.get("request_throughput", data.get("qps", 0.0))
        
        # TTFT (ms)
        ttft_p50 = data.get("p50_ttft_ms", data.get("ttft_p50", 0.0))
        ttft_p90 = data.get("p90_ttft_ms", data.get("ttft_p90", 0.0))
        ttft_p99 = data.get("p99_ttft_ms", data.get("ttft_p99", 0.0))
        
        # TPOT (ms) / ITL
        tpot_p50 = data.get("p50_tpot_ms", data.get("tpot_p50", 0.0))
        tpot_p90 = data.get("p90_tpot_ms", data.get("tpot_p90", 0.0))
        tpot_p99 = data.get("p99_tpot_ms", data.get("tpot_p99", 0.0))

        rows.append({
            "target_rate_rps": request_rate,
            "actual_qps": f"{actual_qps:.2f}",
            "ttft_p50_ms": f"{ttft_p50:.2f}",
            "ttft_p90_ms": f"{ttft_p90:.2f}",
            "ttft_p99_ms": f"{ttft_p99:.2f}",
            "tpot_p50_ms": f"{tpot_p50:.2f}",
            "tpot_p90_ms": f"{tpot_p90:.2f}",
            "tpot_p99_ms": f"{tpot_p99:.2f}"
        })

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "target_rate_rps", "actual_qps",
            "ttft_p50_ms", "ttft_p90_ms", "ttft_p99_ms",
            "tpot_p50_ms", "tpot_p90_ms", "tpot_p99_ms"
        ])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Successfully generated summary report: {output_csv}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", type=str, default="./results", help="Path to benchmark JSON results")
    parser.add_argument("--output", type=str, default="./results/summary.csv", help="Output CSV path")
    args = parser.parse_args()

    parse_metrics(args.results_dir, args.output)
