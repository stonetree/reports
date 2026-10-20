#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
影子实例工作进程脚本 (Shadow Worker)
功能：
  1. 运行于轻量 Docker 容器中，代表一个独立的外部租户；
  2. 加载分配给当前租户的加盐 Trace 文件；
  3. 基于 aiohttp 高并发异步向真实 SUT 节点下发前缀元数据查询与分布式槽位预留请求；
  4. 周期性构造微突发脉冲，模拟 400 并发下的元数据锁争用与跨实例状态漂移。
"""

import os
import sys
import json
import time
import asyncio
import aiohttp
import signal

g_running = True

def handle_signal(sig, frame):
    global g_running
    g_running = False

async def worker_loop():
    worker_id = os.getenv("WORKER_ID", "1")
    tenant_id = os.getenv("TENANT_ID", "2")
    # 优先读取目标 SUT URL，其次读取全局 SUT1_URL，兜底 http://sut1:8000
    sut_url = os.getenv("TARGET_SUT_URL", os.getenv("SUT1_URL", "http://sut1:8000"))
    trace_file = os.getenv("TRACE_FILE", "")
    burst_concurrency = int(os.getenv("BURST_CONCURRENCY", "22"))
    
    print(f"[Shadow Worker {worker_id}] 启动: 租户 ID={tenant_id}, 目标 SUT={sut_url}")
    print(f"[Shadow Worker {worker_id}] 提示：若网络超时，请检查 configs/cluster_env.sh 或宿主机 /etc/hosts 配置")
    
    traces = []
    if trace_file and os.path.exists(trace_file):
        with open(trace_file, "r", encoding="utf-8") as f:
            traces = json.load(f)
        print(f"[Shadow Worker {worker_id}] 成功加载 {len(traces)} 条 Trace 请求")
    else:
        print(f"[Shadow Worker {worker_id}] 未发现 Trace 文件，生成动态模拟循环负载")
        traces = [{
            "request_id": f"dyn_req_{i}",
            "prefix_id": f"tenant_{tenant_id}_hotspot_{i % 10}",
            "prefix_tokens": 16384,
            "pre_gap_sec": 3.0
        } for i in range(1000)]

    timeout = aiohttp.ClientTimeout(total=10)
    connector = aiohttp.TCPConnector(limit=burst_concurrency * 2)
    
    stats_success = 0
    stats_failed = 0
    latencies = []

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        req_idx = 0
        total_reqs = len(traces)
        
        while g_running and req_idx < total_reqs:
            batch_tasks = []
            # 构造微突发并发请求块
            batch_size = min(burst_concurrency, total_reqs - req_idx)
            
            for b in range(batch_size):
                item = traces[req_idx + b]
                payload = {
                    "tenant_id": tenant_id,
                    "request_id": item["request_id"],
                    "prefix_id": item["prefix_id"],
                    "prefix_tokens": item["prefix_tokens"]
                }
                
                # 向 SUT 元数据快路径接口发送查询与预留
                task = asyncio.create_task(send_mock_query(session, sut_url, payload))
                batch_tasks.append(task)
                
            results = await asyncio.gather(*batch_tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, float):
                    stats_success += 1
                    latencies.append(res)
                else:
                    stats_failed += 1
                    
            req_idx += batch_size
            
            # 定期打印吞吐与 P99 状态
            if stats_success > 0 and stats_success % 200 == 0:
                p99 = sorted(latencies)[int(len(latencies) * 0.99)] if latencies else 0.0
                print(f"[Shadow Worker {worker_id}] 已发送 {stats_success} 次请求 | 失败 {stats_failed} | 最近 P99 响应={p99:.2f} ms")
                latencies = latencies[-1000:]
                
            # 引入细粒度微突发间隙 (5ms ~ 20ms)
            await asyncio.sleep(0.015)

    print(f"[Shadow Worker {worker_id}] 运行结束，总成功={stats_success}, 总失败={stats_failed}")

async def send_mock_query(session, sut_url, payload):
    t0 = time.time()
    try:
        url = f"{sut_url}/v1/metadata/check_prefix"
        async with session.post(url, json=payload) as resp:
            await resp.read()
            lat_ms = (time.time() - t0) * 1000.0
            return lat_ms
    except Exception as e:
        return e

def main():
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    asyncio.run(worker_loop())

if __name__ == "__main__":
    main()
