#!/usr/bin/env python3
"""将传输结果与 Host Touch 探针证据汇总为硬件能力矩阵。"""
import argparse
import csv
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_files", nargs="+")
    parser.add_argument("--host-touch-evidence", required=True)
    parser.add_argument("--out", default="capability_matrix.json")
    args = parser.parse_args()
    touch = json.loads(Path(args.host_touch_evidence).read_text(encoding="utf-8"))
    paths = {}
    for csv_file in args.csv_files:
        with Path(csv_file).open("r", encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                key = f"{row['path_mode']}_{row['payload_bytes']}B_QD{row['queue_depth']}"
                status = row["status"]
                valid = row["evidence_level"] != "DEMO" and status == "OK" and touch.get("status") == "OK" and int(row["actual_completed_bytes"]) > 0
                paths[key] = {
                    "mode": row["path_mode"],
                    "payload_bytes": int(row["payload_bytes"]),
                    "queue_depth": int(row["queue_depth"]),
                    "actual_completed_bytes": int(row["actual_completed_bytes"]),
                    "effective_bw_gbps": float(row["bandwidth_gbps"]) if valid else None,
                    "latency_p50_us": float(row["latency_p50_us"]) if valid else None,
                    "latency_p99_us": float(row["latency_p99_us"]) if valid else None,
                    "host_cpu_pct": float(row["host_cpu_pct"]),
                    "host_touch_bytes": touch.get("host_touch_bytes") if valid else None,
                    "evidence_level": row["evidence_level"],
                    "status": "OK" if valid else "INVALID_EVIDENCE",
                }
    matrix = {"schema_version": "capability_matrix.v2", "host_touch_evidence": touch, "paths": paths}
    Path(args.out).write_text(json.dumps(matrix, indent=2), encoding="utf-8")
    print(json.dumps({"output": args.out, "entries": len(paths)}))


if __name__ == "__main__":
    main()
