#!/usr/bin/env python3
"""汇总 PVT-07 三组实测 JSON，并按规定基线计算 TTFT、QPS 与 TPOT。"""
import argparse
import json
from pathlib import Path

REQUIRED = {
    "run_id", "package_id", "config_hash", "evidence_level", "hardware_profile", "topology_profile",
    "workload_id", "model_id", "target_rate_rps", "p99_ttft_ms", "p99_tpot_ms", "qps", "bg_bw_gbps"
}


def load(path: str, target_rate_rps: float | None) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "rows" in data:
        rows = data["rows"]
        if target_rate_rps is not None:
            rows = [row for row in rows if float(row.get("target_rate_rps", -1)) == target_rate_rps]
        if len(rows) != 1:
            raise ValueError(f"{path} must resolve to exactly one row; got {len(rows)}")
        data = rows[0]
    aliases = {
        "p99_ttft_ms": "ttft_p99_ms",
        "p99_tpot_ms": "tpot_p99_ms",
        "qps": "actual_qps",
    }
    for target, source in aliases.items():
        if data.get(target) is None and data.get(source) is not None:
            data[target] = data[source]
    missing = REQUIRED - set(data)
    if missing:
        raise ValueError(f"{path} missing fields: {sorted(missing)}")
    if data["evidence_level"] == "DEMO":
        raise ValueError(f"{path} is DEMO and cannot close E3")
    return data


def percent_change(new: float, baseline: float) -> float:
    if baseline <= 0:
        raise ValueError("baseline must be positive")
    return (new - baseline) / baseline * 100.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unified-foreground", required=True)
    parser.add_argument("--mooncake-native-mixed", required=True)
    parser.add_argument("--unified-mixed", required=True)
    parser.add_argument("--background-tolerance-pct", type=float, default=5.0)
    parser.add_argument("--target-rate-rps", type=float, help="summary.json 含多个压测速率时选择同一目标速率")
    parser.add_argument("--out", default="pvt07_summary.json")
    args = parser.parse_args()
    try:
        foreground = load(args.unified_foreground, args.target_rate_rps)
        native = load(args.mooncake_native_mixed, args.target_rate_rps)
        enhanced = load(args.unified_mixed, args.target_rate_rps)
        if foreground["package_id"] != enhanced["package_id"]:
            raise ValueError("TPOT baseline must use the same enhanced package_id")
        for field in ["hardware_profile", "topology_profile", "workload_id", "model_id", "target_rate_rps"]:
            values = {foreground[field], native[field], enhanced[field]}
            if len(values) != 1:
                raise ValueError(f"A/B fairness field differs: {field}={sorted(values, key=str)}")
        bg_delta_pct = abs(percent_change(enhanced["bg_bw_gbps"], native["bg_bw_gbps"]))
        if bg_delta_pct > args.background_tolerance_pct:
            raise ValueError(f"mixed background bandwidth differs by {bg_delta_pct:.2f}%")
        ttft_reduction_pct = -percent_change(enhanced["p99_ttft_ms"], native["p99_ttft_ms"])
        qps_gain_pct = percent_change(enhanced["qps"], native["qps"])
        tpot_interference_pct = percent_change(enhanced["p99_tpot_ms"], foreground["p99_tpot_ms"])
        metrics = {
            "ttft_reduction_pct_vs_mooncake_native_mixed": ttft_reduction_pct,
            "qps_gain_pct_vs_mooncake_native_mixed": qps_gain_pct,
            "tpot_interference_pct_vs_same_package_foreground": tpot_interference_pct,
            "background_bandwidth_delta_pct": bg_delta_pct,
        }
        gates = {
            "ttft_reduction_ge_20": ttft_reduction_pct >= 20.0,
            "qps_gain_ge_10": qps_gain_pct >= 10.0,
            "tpot_interference_lt_3": tpot_interference_pct < 3.0,
            "background_load_comparable": True,
        }
        result = {"status": "PASS" if all(gates.values()) else "FAIL", "metrics": metrics, "gates": gates,
                  "inputs": {"unified_foreground": foreground, "mooncake_native_mixed": native, "unified_mixed": enhanced}}
        code = 0 if result["status"] == "PASS" else 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"status": "INVALID_EVIDENCE", "invalid_reason": str(exc)}
        code = 2
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": args.out, "status": result["status"]}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
