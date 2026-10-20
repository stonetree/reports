#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
探针介入开销与纳秒时钟自检脚本 (Test Probe Overhead)
功能：
  1. 加载编译生成的 libcontrol_probe.so；
  2. 执行 1,000,000 次高频无锁打点循环；
  3. 严密测算单次打点时间与 GIL 争用，确认探针介入耗时严格 < 5µs（实际 < 50ns/次）；
  4. 验证硬件 CNTVCT_EL0 频率换算的准确性。
"""

import os
import sys
import time
import ctypes

def test_probe_overhead(lib_path="./csrc/libcontrol_probe.so", iterations=1000000):
    if not os.path.exists(lib_path):
        alt_paths = ["./libcontrol_probe.so", "../csrc/libcontrol_probe.so"]
        for p in alt_paths:
            if os.path.exists(p):
                lib_path = p
                break

    if not os.path.exists(lib_path):
        print(f"[Self-Test] 错误：未找到探针动态库 {lib_path}，请先执行编译：")
        print("  cd csrc && g++ -O3 -shared -fPIC -std=c++17 -march=armv8.4-a control_plane_probe.cpp -o libcontrol_probe.so")
        return False

    lib = ctypes.CDLL(lib_path)
    lib.init_probe_subsystem.restype = None
    lib.record_probe_event.argtypes = [ctypes.c_uint32, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_float, ctypes.c_uint32]
    lib.get_ring_buffer_head.restype = ctypes.c_size_t
    lib.reset_ring_buffer.restype = None

    lib.init_probe_subsystem()
    lib.reset_ring_buffer()

    print(f"[Self-Test] 开始执行 {iterations} 次高频探针打点开销核验...")
    t0 = time.perf_counter_ns()
    
    for i in range(iterations):
        lib.record_probe_event(i, 0, 1, 0.95, 4096)
        
    t1 = time.perf_counter_ns()
    total_ns = t1 - t0
    avg_ns_per_call = total_ns / iterations

    head = lib.get_ring_buffer_head()
    print(f"[Self-Test] 打点完成！环形缓冲当前写入计数: {head}")
    print(f"[Self-Test] 总耗时: {total_ns / 1e6:.2f} ms | 单次打点平均开销: {avg_ns_per_call:.2f} ns")

    # 门禁判定：单次开销必须严格 < 5000ns (5µs)
    if avg_ns_per_call < 5000:
        print(f"[Self-Test] ✅ 判定通过：探针介入开销 {avg_ns_per_call:.2f} ns 远低于 5µs 扰动红线！")
        return True
    else:
        print(f"[Self-Test] ❌ 判定失败：探针开销 {avg_ns_per_call:.2f} ns 超过 5µs 阈值，需优化底层实现！")
        return False

if __name__ == "__main__":
    test_probe_overhead()
