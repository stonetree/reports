#!/usr/bin/env python3
"""解析 benchmark_serving JSON；核心字段缺失时输出 INVALID_EVIDENCE，不回填 0。"""
import argparse
import csv
import glob
import json
from pathlib import Path

ALIASES = {
    "actual_qps": ["request_throughput", "qps"],
    "ttft_p50_ms": ["p50_ttft_ms", "ttft_p50"],
    "ttft_p90_ms": ["p90_ttft_ms", "ttft_p90"],
    "ttft_p99_ms": ["p99_ttft_ms", "ttft_p99"],
    "tpot_p50_ms": ["p50_tpot_ms", "tpot_p50"],
    "tpot_p90_ms": ["p90_tpot_ms", "tpot_p90"],
    "tpot_p99_ms": ["p99_tpot_ms", "tpot_p99"],
    "bg_bw_gbps": ["background_bandwidth_gbps", "bg_io_gbps", "bg_bw_gbps"],
}


def pick(data: dict, names: list[str]):
    for name in names:
        if data.get(name) is not None:
            return data[name]
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--output-json", help="同时输出供 PVT-07 对账器读取的结构化结果")
    parser.add_argument("--mode", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--package-id", required=True)
    parser.add_argument("--config-hash", required=True)
    parser.add_argument("--hardware-profile", required=True)
    parser.add_argument("--topology-profile", required=True)
    parser.add_argument("--workload-id", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--evidence-level", choices=["DEMO", "LAB", "MEASURED"], required=True)
    args = parser.parse_args()
    files = sorted(glob.glob(str(Path(args.results_dir) / "bench_serving_rate_*.json")))
    rows, invalid = [], False
    if not files:
        rows.append({"status": "INVALID_EVIDENCE", "invalid_reason": "no_result_json"})
        invalid = True
    for file_name in files:
        data = json.loads(Path(file_name).read_text(encoding="utf-8"))
        metrics = {key: pick(data, aliases) for key, aliases in ALIASES.items()}
        missing = sorted(key for key, value in metrics.items() if value is None)
        row = {
            "mode": args.mode, "run_id": args.run_id, "package_id": args.package_id,
            "config_hash": args.config_hash, "hardware_profile": args.hardware_profile,
            "topology_profile": args.topology_profile, "workload_id": args.workload_id, "model_id": args.model_id,
            "evidence_level": args.evidence_level, "source_file": Path(file_name).name,
            "target_rate_rps": data.get("request_rate"), **metrics,
            "cache_usable_hit_rate": pick(data, ["usable_hit_rate", "cache_hit_rate"]),
            "failed_requests": data.get("failed_requests"),
            "status": "INVALID_EVIDENCE" if missing else "OK",
            "invalid_reason": f"missing:{','.join(missing)}" if missing else None,
        }
        invalid = invalid or bool(missing)
        rows.append(row)
    fields = ["mode", "run_id", "package_id", "config_hash", "hardware_profile", "topology_profile",
              "workload_id", "model_id", "evidence_level", "source_file",
              "target_rate_rps", "actual_qps", "ttft_p50_ms", "ttft_p90_ms", "ttft_p99_ms",
              "tpot_p50_ms", "tpot_p90_ms", "tpot_p99_ms", "cache_usable_hit_rate", "failed_requests",
              "bg_bw_gbps", "status", "invalid_reason"]
    with Path(args.output).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    if args.output_json:
        Path(args.output_json).write_text(
            json.dumps({"schema_version": "benchmark_summary.v1", "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps({"output": args.output, "rows": len(rows), "status": "INVALID_EVIDENCE" if invalid else "OK"}))
    return 2 if invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
