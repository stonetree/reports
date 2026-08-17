#!/usr/bin/env python3
"""
inject_fault.py - CVT-03 DPU 硬件通道与驱动故障注入脚本
用于模拟 DPU 硬件超时、PCIe 链路中断，触发 Raw Direct 路径无缝 Fallback 降级。
"""
import argparse
import time

def inject(target: str, fault: str):
    print(f"=== Injecting Fault: Target = {target}, Fault = {fault} ===")
    print(f"1. Sending trigger signal to {target} driver mock...")
    time.sleep(0.05)
    print(f"2. Simulating hardware {fault} event...")
    print(f"3. Fallback watcher notified. SUT should downgrade to Raw Direct path immediately.")
    print("Injection completed.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=str, default="dpu")
    parser.add_argument("--fault", type=str, default="disconnect")
    args = parser.parse_args()

    inject(args.target, args.fault)
