#!/usr/bin/env python3
"""
export_capability_matrix.py - 自动解析 raw_trans_bench.csv 并生成标准 capability_matrix.json
为上层 QueryPlan 与调度器提供实测硬件介质能力参数。
"""
import csv
import json
import os
import sys

def parse_csv_to_matrix(csv_files: list, output_json: str):
    matrix = {
        "version": "1.0",
        "description": "Hardware Capability Matrix from PVT-01 actual measurements",
        "paths": {}
    }

    for fpath in csv_files:
        if not os.path.exists(fpath):
            continue
        with open(fpath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                mode = row["path_mode"]
                size = int(row["payload_bytes"])
                qd = int(row["queue_depth"])
                bw = float(row["bandwidth_gbps"])
                p50 = float(row["latency_p50_us"])
                p99 = float(row["latency_p99_us"])
                cpu = float(row["host_cpu_pct"])

                key = f"{mode}_{size//1024}KB_QD{qd}"
                matrix["paths"][key] = {
                    "mode": mode,
                    "payload_bytes": size,
                    "queue_depth": qd,
                    "effective_bw_gbps": bw,
                    "latency_p50_us": p50,
                    "latency_p99_us": p99,
                    "host_cpu_pct": cpu,
                    "host_touch_bytes": 0 if "direct" in mode else size
                }

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(matrix, f, indent=2)

    print(f"Exported capability matrix with {len(matrix['paths'])} entries to {output_json}")

if __name__ == "__main__":
    files = sys.argv[1:] if len(sys.argv) > 1 else ["res_urma.csv", "res_ubmem.csv", "res_nvme.csv"]
    parse_csv_to_matrix(files, "capability_matrix.json")
