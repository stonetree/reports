#!/usr/bin/env python3
"""
inject_fault.py - PVT-09 DPU 硬件通道与驱动故障注入脚本
用于模拟 DPU 硬件超时、PCIe 链路中断，触发 Raw Direct 路径无缝 Fallback 降级。
"""
import argparse
import json
import time
from pathlib import Path

def inject(target: str, fault: str, timeout_us: int, out_file: str):
    started_ns = time.monotonic_ns()
    print(f"=== Injecting Fault: Target = {target}, Fault = {fault} ===")
    print(f"1. Sending trigger signal to {target} driver mock...")
    time.sleep(0.05)
    print(f"2. Simulating hardware {fault} event...")
    print(f"3. Fallback watcher notified. SUT should downgrade to Raw Direct path immediately.")
    event = {
        "event_name": "DPU_FAULT_INJECTED",
        "target": target,
        "fault": fault,
        "configured_timeout_us": timeout_us,
        "monotonic_timestamp_ns": started_ns,
        "trigger_complete_ns": time.monotonic_ns(),
        "expected_confirmation_event": "RAW_DIRECT_FALLBACK_CONFIRMED",
        "evidence_level": "DEMO",
        "status": "INJECTED"
    }
    Path(out_file).write_text(json.dumps(event, indent=2), encoding="utf-8")
    print(f"Injection event saved to {out_file}; the SUT confirmation event must be joined by run_id in LAB/MEASURED.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=str, default="dpu")
    parser.add_argument("--fault", type=str, default="disconnect")
    parser.add_argument("--timeout-us", type=int, default=500)
    parser.add_argument("--out", default="fault_event.json")
    args = parser.parse_args()

    inject(args.target, args.fault, args.timeout_us, args.out)
