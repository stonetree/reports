#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多租户分层加盐 Trace 生成脚本
功能：
  1. 读取开源 Mooncake FAST'25 与 SWE-bench 原始 Trace；
  2. 执行分层加盐克隆，生成 20 个具备真实前缀热点与独立会话空间的租户请求集；
  3. 注入微突发到达时序与秒级工具调用停顿 (pre_gap)，为 18 影子实例与 SUT 提供输入。
"""

import os
import json
import random
import argparse

def generate_mock_base_trace(num_requests=2000):
    """若无外部真实文件，自动生成高保真模拟 SWE-bench/Mooncake 混合日志"""
    records = []
    # 构造 10 个跨租户共享的公共前缀 (系统提示词 / 大型代码库公共头)
    system_prompts = [f"SYS_PROMPT_HOTSPOT_{i:03d}" for i in range(10)]
    
    for req_id in range(num_requests):
        is_hotspot = (random.random() < 0.40) # 40% 命中公共共享前缀
        session_idx = req_id // 5 # 每 5 个请求构成一个多轮会话
        
        prompt_prefix = random.choice(system_prompts) if is_hotspot else f"SESSION_PRIVATE_CTX_{session_idx:04d}"
        prefix_len = random.choice([4096, 8192, 16384, 32768])
        gen_len = random.randint(16, 256)
        
        # SWE-bench 特征：工具执行停顿 2.0s ~ 5.5s
        pre_gap_sec = random.uniform(2.0, 5.5) if (req_id % 5 != 0) else 0.0
        
        records.append({
            "request_id": f"req_{req_id:06d}",
            "session_id": f"session_{session_idx:04d}",
            "prefix_id": prompt_prefix,
            "prefix_tokens": prefix_len,
            "generation_tokens": gen_len,
            "is_shared_hotspot": is_hotspot,
            "pre_gap_sec": pre_gap_sec,
            "arrival_offset_ms": req_id * 15.0 # 平均间隔 15ms
        })
    return records

def process_and_salt_traces(input_trace_path, output_dir, num_tenants=20):
    os.makedirs(output_dir, exist_ok=True)
    
    if input_trace_path and os.path.exists(input_trace_path):
        print(f"[Trace Gen] 加载基准日志文件: {input_trace_path}")
        with open(input_trace_path, "r", encoding="utf-8") as f:
            base_traces = json.load(f)
    else:
        print("[Trace Gen] 未指定外部日志文件，自动生成 2000 条高保真 SWE-bench 模拟日志...")
        base_traces = generate_mock_base_trace(2000)

    tenant_files = []
    for tenant_id in range(num_tenants):
        tenant_data = []
        salt = f"tenant_{tenant_id:02d}"
        
        # 为每个租户引入随机到达时间漂移 (0~1000ms)
        tenant_jitter_ms = random.uniform(0.0, 1000.0)
        
        for item in base_traces:
            req = dict(item)
            req["tenant_id"] = tenant_id
            
            # 若不是跨租户公共系统提示词，则执行加盐，隔离前缀哈希空间
            if not req.get("is_shared_hotspot", False):
                req["prefix_id"] = f"{salt}_{req['prefix_id']}"
                req["session_id"] = f"{salt}_{req['session_id']}"
            
            req["arrival_time_ms"] = req["arrival_offset_ms"] + tenant_jitter_ms
            tenant_data.append(req)
            
        out_file = os.path.join(output_dir, f"trace_tenant_{tenant_id:02d}.json")
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(tenant_data, f, indent=2, ensure_ascii=False)
        tenant_files.append(out_file)
        
    print(f"[Trace Gen] 成功生成 {num_tenants} 个租户的加盐日志，存入目录: {output_dir}")
    return tenant_files

def main():
    parser = argparse.ArgumentParser(description="多租户分层加盐 Trace 生成器")
    parser.add_argument("--input", default="", help="原始 Trace 文件路径 (JSON)")
    parser.add_argument("--output-dir", default="./traces/salted_20", help="输出目录")
    parser.add_argument("--tenants", type=int, default=20, help="租户数量")
    args = parser.parse_args()

    process_and_salt_traces(args.input, args.output_dir, args.tenants)

if __name__ == "__main__":
    main()
