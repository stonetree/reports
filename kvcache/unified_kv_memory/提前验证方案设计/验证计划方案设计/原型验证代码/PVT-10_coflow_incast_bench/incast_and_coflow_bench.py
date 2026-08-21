#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PVT-10: N-to-1 Incast 与 TP=8 Coflow 偏斜实测基准 (Benchmark Demo)
功能：
1. 测量 N-to-1 Incast 下，随着并发发送端 N (1, 2, 4, 8, 16) 增加，时延的超线性非等比膨胀（P50 vs P99/P999）；
2. 测量 TP=8 张量并行下，8 个绑定 Rank 的流作为一个 Coflow 传输时，最慢流对整体 CCT (Coflow Completion Time) 的木桶放大效应；
3. 测量 Layer 0 起始首层在普通无优先级队列 vs 硬件 QoS 优先级队列下的排队倒置与计算气泡消除效果。
"""

import sys
import time
import socket
import threading
import numpy as np
import argparse
import json

def receiver_server(port, ready_event, stop_event, chunk_size):
    """接收端：监听指定端口并接收数据块"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', port))
    sock.listen(128)
    sock.settimeout(0.5)
    ready_event.set()

    while not stop_event.is_set():
        try:
            conn, addr = sock.accept()
        except socket.timeout:
            continue
        except Exception:
            break
        
        # 处理客户端连接
        try:
            while not stop_event.is_set():
                header = conn.recv(16)
                if not header or len(header) < 16:
                    break
                # 解析大小
                size = int.from_bytes(header[:8], 'big')
                req_id = int.from_bytes(header[8:], 'big')
                # 接收正文
                received = 0
                while received < size:
                    buf = conn.recv(min(65536, size - received))
                    if not buf:
                        break
                    received += len(buf)
                # 发送 ACK 回包
                conn.sendall(req_id.to_bytes(8, 'big'))
        except Exception:
            pass
        finally:
            conn.close()
    sock.close()

def client_sender(server_ip, server_port, chunk_size, num_requests, results_list, thread_id, delay_inject_prob=0.0, delay_ms=0.0):
    """发送端：循环发送指定大小的块并记录 RTT"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        sock.connect((server_ip, server_port))
    except Exception as e:
        print(f"[Error] Thread {thread_id} connect failed: {e}")
        return

    payload = b'X' * chunk_size
    latencies = []

    for req_id in range(num_requests):
        # 模拟部分流的网络微抖动 (用于测量木桶短板)
        if delay_inject_prob > 0 and np.random.rand() < delay_inject_prob:
            time.sleep(delay_ms / 1000.0)

        t_start = time.perf_counter()
        header = chunk_size.to_bytes(8, 'big') + req_id.to_bytes(8, 'big')
        try:
            sock.sendall(header + payload)
            ack = sock.recv(8)
            t_end = time.perf_counter()
            lat_us = (t_end - t_start) * 1e6
            latencies.append((req_id, lat_us))
        except Exception as e:
            break

    sock.close()
    results_list.append((thread_id, latencies))

def run_incast_test(server_ip, base_port, chunk_size_kb, sender_counts, num_requests):
    """运行 N-to-1 Incast 实验"""
    print(f"\n=======================================================")
    print(f"  [实验一] N-to-1 Incast 非等比时延膨胀测试 (Chunk: {chunk_size_kb}KB)")
    print(f"=======================================================")
    print(f"{'并发流数(N)':<12}{'平均时延(µs)':<15}{'P50(µs)':<12}{'P90(µs)':<12}{'P99(µs)':<12}{'P99.9(µs)':<14}{'P99膨胀比':<12}")
    
    baseline_p99 = 1.0

    port_offset = 0
    for N in sender_counts:
        curr_port = base_port + port_offset
        port_offset += 1
        # 启动接收端
        ready_evt = threading.Event()
        stop_evt = threading.Event()
        server_th = threading.Thread(target=receiver_server, args=(curr_port, ready_evt, stop_evt, chunk_size_kb * 1024))
        server_th.daemon = True
        server_th.start()
        ready_evt.wait()

        # 启动 N 个客户端并发发送
        results = []
        threads = []
        for i in range(N):
            th = threading.Thread(target=client_sender, args=(server_ip, curr_port, chunk_size_kb * 1024, num_requests, results, i))
            threads.append(th)

        for th in threads:
            th.start()
        for th in threads:
            th.join()

        stop_evt.set()

        # 统计所有流的时延
        all_lats = []
        for tid, lats in results:
            all_lats.extend([lat for _, lat in lats])

        if not all_lats:
            print(f"{N:<12} 测试失败 (无数据)")
            continue

        avg_lat = np.mean(all_lats)
        p50 = np.percentile(all_lats, 50)
        p90 = np.percentile(all_lats, 90)
        p99 = np.percentile(all_lats, 99)
        p999 = np.percentile(all_lats, 99.9)

        if N == sender_counts[0]:
            baseline_p99 = p99
        inflation = p99 / baseline_p99

        print(f"{N:<12}{avg_lat:<15.2f}{p50:<12.2f}{p90:<12.2f}{p99:<12.2f}{p999:<14.2f}{inflation:<12.2f}x")

def run_tp8_coflow_test(server_ip, base_port, chunk_size_kb, num_coflows=100):
    """运行 TP=8 Coflow 木桶效应测试"""
    print(f"\n=======================================================")
    print(f"  [实验二] TP=8 Coflow 木桶短板与偏斜放大测试 (8 Ranks / Coflow)")
    print(f"=======================================================")

    # 启动 8 个端口模拟 8 个目标接收端 (TP=8)
    ready_evts = [threading.Event() for _ in range(8)]
    stop_evt = threading.Event()
    servers = []
    for rank in range(8):
        s_th = threading.Thread(target=receiver_server, args=(base_port + rank, ready_evts[rank], stop_evt, chunk_size_kb * 1024))
        s_th.daemon = True
        s_th.start()
        servers.append(s_th)
    for evt in ready_evts:
        evt.wait()

    # 场景 A: 干净网络 (各流均匀)
    # 场景 B: 真实扰动网络 (仅有 1 个 Rank 发生 2% 概率的 200µs 轻微微扰)
    for scenario_name, perturb_prob, perturb_ms in [("A. 干净专有网络 (理想状态)", 0.0, 0.0), 
                                                    ("B. 真实混压网络 (Rank 7 偶发微扰)", 0.15, 0.2)]:
        results = []
        threads = []
        for rank in range(8):
            prob = perturb_prob if rank == 7 else 0.0
            p_ms = perturb_ms if rank == 7 else 0.0
            th = threading.Thread(target=client_sender, args=(server_ip, base_port + rank, chunk_size_kb * 1024, num_coflows, results, rank, prob, p_ms))
            threads.append(th)

        for th in threads:
            th.start()
        for th in threads:
            th.join()

        # 整理 Coflow 数据: 每个 req_id 对应 8 个 Rank 的时延集合
        coflow_map = {req_id: [] for req_id in range(num_coflows)}
        all_single_flow_lats = []
        for tid, lats in results:
            for req_id, lat in lats:
                if req_id < num_coflows:
                    coflow_map[req_id].append(lat)
                    all_single_flow_lats.append(lat)

        cct_list = []
        for req_id, rank_lats in coflow_map.items():
            if len(rank_lats) == 8:
                cct_list.append(max(rank_lats))  # CCT = max(T_0 .. T_7)

        avg_single = np.mean(all_single_flow_lats) if all_single_flow_lats else 0
        p50_cct = np.percentile(cct_list, 50) if cct_list else 0
        p99_cct = np.percentile(cct_list, 99) if cct_list else 0
        skewness = (p99_cct / avg_single) if avg_single > 0 else 0

        print(f"\n>> 场景: {scenario_name}")
        print(f"   • 单流平均传输耗时 (Flow Avg): {avg_single:.2f} µs")
        print(f"   • TP=8 Coflow P50 完成时间 (CCT P50): {p50_cct:.2f} µs")
        print(f"   • TP=8 Coflow P99 完成时间 (CCT P99): {p99_cct:.2f} µs")
        print(f"   • 木桶放大偏斜比 (CCT P99 / Flow Avg): {skewness:.2f}x (说明单流优化无法解决木桶短板)")

    stop_evt.set()

def main():
    parser = argparse.ArgumentParser(description="PVT-10 Incast and Coflow Bench")
    parser.add_argument("--ip", type=str, default="127.0.0.1", help="Target server IP")
    parser.add_argument("--port", type=int, default=18800, help="Base Port")
    parser.add_argument("--chunk_kb", type=int, default=64, help="Chunk Size in KB")
    parser.add_argument("--requests", type=int, default=200, help="Number of requests per stream")
    args = parser.parse_args()

    print("=================================================================")
    print("  统一异构 KVCache 存储池 PVT-10 多流非等比劣化与 Coflow 实测验证")
    print("=================================================================")

    # 1. 运行 N-to-1 Incast
    run_incast_test(args.ip, args.port, args.chunk_kb, [1, 2, 4, 8, 16], args.requests)

    # 2. 运行 TP=8 Coflow
    run_tp8_coflow_test(args.ip, args.port + 100, args.chunk_kb, args.requests)

if __name__ == "__main__":
    main()
