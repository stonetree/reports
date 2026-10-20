#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
控制面压测平台集群环境连通性与硬件配置前置检查工具 (Preflight Check)
功能：
  1. 验证 SUT1 / SUT2 节点的 IP/域名解析、网络连通性与 HTTP API 可达性；
  2. 检查操作系统 1GB 巨页内存预留 (HugePages) 与 MPAM resctrl 隔离挂载状态；
  3. 核验 C++ 探针动态库与压舱石内存分配器编译产物；
  4. 核验 20 租户分层加盐 Trace 数据集与日志存储目录完整性；
  5. 输出格式化体检报告与修复指引，杜绝真实测试盲目启动。
"""

import os
import sys
import json
import socket
import urllib.request
import urllib.error
import argparse
import platform

class PreflightChecker:
    def __init__(self, config_path=None, quick_mode=False):
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.base_dir = os.path.dirname(self.script_dir)
        if config_path is None:
            self.config_path = os.path.join(self.base_dir, "configs", "cluster_env.json")
        else:
            self.config_path = config_path if os.path.isabs(config_path) else os.path.join(self.base_dir, config_path)
            
        self.quick_mode = quick_mode
        self.results = []
        self.config = self._load_config()

    def _load_config(self):
        cfg = {
            "sut_nodes": {
                "sut1": {"ip": "192.168.10.101", "port": 8000, "hostname": "sut1", "url": "http://192.168.10.101:8000"},
                "sut2": {"ip": "192.168.10.102", "port": 8000, "hostname": "sut2", "url": "http://192.168.10.102:8000"}
            },
            "hardware_tuning": {
                "hugepages_1gb_required": 450,
                "mpam_resctrl_path": "/sys/fs/resctrl"
            },
            "paths": {
                "probe_library": "./csrc/libcontrol_probe.so",
                "ballast_binary": "./csrc/ballast_alloc",
                "traces_dir": "./traces/salted_20",
                "logs_dir": "./logs"
            }
        }
        # 优先读取配置文件
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    user_cfg = json.load(f)
                    cfg.update(user_cfg)
            except Exception as e:
                print(f"[Preflight] 读取配置文件失败: {e}，使用默认配置。")

        # 环境变量优先级最高
        if os.getenv("SUT1_URL"):
            cfg["sut_nodes"]["sut1"]["url"] = os.getenv("SUT1_URL")
        if os.getenv("SUT2_URL"):
            cfg["sut_nodes"]["sut2"]["url"] = os.getenv("SUT2_URL")
        return cfg

    def record(self, category, item, status, detail, remediation=""):
        self.results.append({
            "category": category,
            "item": item,
            "status": status, # PASS, WARN, FAIL
            "detail": detail,
            "remediation": remediation
        })

    def check_network_and_sut(self):
        suts = self.config.get("sut_nodes", {})
        for name, node in suts.items():
            url = node.get("url", "")
            hostname = node.get("hostname", name)
            ip = node.get("ip", "")
            port = node.get("port", 8000)

            # 1. 检查域名/主机名解析
            try:
                resolved_ip = socket.gethostbyname(hostname)
                self.record("网络与解析", f"{hostname} DNS/Hosts解析", "PASS", f"成功解析至: {resolved_ip}")
            except Exception as e:
                self.record("网络与解析", f"{hostname} DNS/Hosts解析", "WARN", f"无法解析主机名 '{hostname}': {e}",
                            f"请在 /etc/hosts 中添加: {ip} {hostname}")

            # 2. 检查 HTTP 连通性
            check_urls = [f"{url}/metrics", f"{url}/health", f"{url}/v1/models"]
            timeout_val = 0.5 if self.quick_mode else 1.5
            reachable = False
            last_err = ""
            for u in check_urls:
                try:
                    req = urllib.request.Request(u, headers={"User-Agent": "PreflightCheck/1.0"})
                    with urllib.request.urlopen(req, timeout=timeout_val) as resp:
                        if resp.status in [200, 404]: # 404 表示 HTTP 服务存活
                            self.record("SUT 连通性", f"{name} HTTP 探测 ({url})", "PASS", f"状态码: {resp.status} ({u})")
                            reachable = True
                            break
                except Exception as e:
                    last_err = str(e)

            if not reachable:
                self.record("SUT 连通性", f"{name} HTTP 探测 ({url})", "WARN",
                            f"无法连接目标服务: {last_err}",
                            f"请确认 {name} 节点 vLLM / Mooncake 实例已启动，或执行: source configs/cluster_env.sh 修正 IP")

    def check_os_and_hugepages(self):
        is_linux = (platform.system() == "Linux")
        if not is_linux:
            self.record("操作系统配置", "Linux 内核子系统检查", "WARN",
                        f"当前系统为 {platform.system()}，跳过 1GB 巨页与 MPAM 物理内核检查 (部署到测试集群时将自动生效)")
            return

        # 检查 1GB HugePages
        meminfo_path = "/proc/meminfo"
        if os.path.exists(meminfo_path):
            with open(meminfo_path, "r") as f:
                content = f.read()
            huge_total = 0
            huge_size_kb = 0
            for line in content.splitlines():
                if line.startswith("HugePages_Total:"):
                    huge_total = int(line.split()[1])
                elif line.startswith("Hugepagesize:"):
                    huge_size_kb = int(line.split()[1])

            req_pages = self.config.get("hardware_tuning", {}).get("hugepages_1gb_required", 450)
            if huge_size_kb == 1048576 and huge_total >= req_pages:
                self.record("操作系统配置", "1GB HugePages 巨页内存", "PASS", f"配置正常: {huge_total} 页 (1048576 kB)")
            else:
                self.record("操作系统配置", "1GB HugePages 巨页内存", "WARN",
                            f"当前仅配置 {huge_total} 页 (单页 {huge_size_kb} kB)，低于推荐的 {req_pages} 页",
                            f"执行: echo {req_pages} | sudo tee /sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages")

        # 检查 MPAM resctrl
        resctrl_path = self.config.get("hardware_tuning", {}).get("mpam_resctrl_path", "/sys/fs/resctrl")
        if os.path.exists(resctrl_path) and os.path.exists(os.path.join(resctrl_path, "schemata")):
            self.record("操作系统配置", "ARM MPAM resctrl 挂载", "PASS", f"路径存在且已挂载: {resctrl_path}")
        else:
            self.record("操作系统配置", "ARM MPAM resctrl 挂载", "WARN",
                        f"未检测到 MPAM resctrl 文件系统: {resctrl_path}",
                        "执行: sudo mount -t resctrl resctrl /sys/fs/resctrl")

    def _resolve_path(self, p):
        if os.path.isabs(p):
            return p
        # 移除前导 ./ 避免跨目录路径异常
        if p.startswith("./") or p.startswith(".\\"):
            p = p[2:]
        return os.path.join(self.base_dir, p)

    def check_probe_and_binaries(self):
        paths = self.config.get("paths", {})
        probe_path = self._resolve_path(paths.get("probe_library", "csrc/libcontrol_probe.so"))
        ballast_path = self._resolve_path(paths.get("ballast_binary", "csrc/ballast_alloc"))

        # 检查 C++ 探针动态库
        if os.path.exists(probe_path):
            self.record("核心组件", "C++ 纳米级探针动态库", "PASS", f"发现二进制库: {probe_path}")
        else:
            self.record("核心组件", "C++ 纳米级探针动态库", "WARN",
                        f"未找到共享库: {probe_path}",
                        "执行编译: g++ -O3 -shared -fPIC -pthread -std=c++17 csrc/control_plane_probe.cpp -o csrc/libcontrol_probe.so")

        # 检查 450GB 压舱石二进制
        if os.path.exists(ballast_path):
            self.record("核心组件", "450GB 压舱石预占程序", "PASS", f"发现执行文件: {ballast_path}")
        else:
            self.record("核心组件", "450GB 压舱石预占程序", "WARN",
                        f"未找到可执行文件: {ballast_path}",
                        "执行编译: gcc -O3 -D_GNU_SOURCE csrc/ballast_alloc.c -o csrc/ballast_alloc")

    def check_traces_and_logs(self):
        paths = self.config.get("paths", {})
        traces_dir = self._resolve_path(paths.get("traces_dir", "traces/salted_20"))
        logs_dir = self._resolve_path(paths.get("logs_dir", "logs"))

        # 检查 Trace 文件
        if os.path.exists(traces_dir):
            trace_files = [f for f in os.listdir(traces_dir) if f.startswith("trace_tenant_") and f.endswith(".json")]
            if len(trace_files) >= 20:
                self.record("数据资产", "20 租户分层加盐 Trace", "PASS", f"完整就绪: 发现 {len(trace_files)} 个租户日志文件")
            else:
                self.record("数据资产", "20 租户分层加盐 Trace", "WARN",
                            f"文件不完整 (仅发现 {len(trace_files)} 个)",
                            "执行生成: python scripts/generate_salted_traces.py --output-dir traces/salted_20 --tenants 20")
        else:
            self.record("数据资产", "20 租户分层加盐 Trace", "FAIL", f"目录不存在: {traces_dir}",
                        "执行生成: python scripts/generate_salted_traces.py --output-dir traces/salted_20 --tenants 20")

        # 检查日志子目录
        required_logs = ["pmu", "shadow", "sweep", "ablation"]
        missing_logs = []
        for sub in required_logs:
            p = os.path.join(logs_dir, sub)
            if not os.path.exists(p):
                missing_logs.append(sub)

        if not missing_logs:
            self.record("数据资产", "物理日志归档子目录", "PASS", f"4 类日志目录均已创建 ({', '.join(required_logs)})")
        else:
            self.record("数据资产", "物理日志归档子目录", "WARN",
                        f"缺失子目录: {missing_logs}",
                        f"执行创建: mkdir -p logs/{{{','.join(required_logs)}}}")

    def run_all(self):
        print("=" * 80)
        print(" 控制面性能放大效应测试平台 - 集群前置就绪体检工具 (Preflight Check)")
        print("=" * 80)
        
        self.check_network_and_sut()
        self.check_os_and_hugepages()
        self.check_probe_and_binaries()
        self.check_traces_and_logs()

        # 打印报告表格
        print(f"\n{'类别':<14} | {'检查项目':<24} | {'状态':<6} | {'详细说明'}")
        print("-" * 80)
        has_fail = False
        warn_count = 0
        for r in self.results:
            status_str = f"[{r['status']}]"
            print(f"{r['category']:<14} | {r['item']:<24} | {status_str:<6} | {r['detail']}")
            if r["remediation"] and r["status"] in ["WARN", "FAIL"]:
                print(f"{' ':<14} | {' -> 修复指引:':<24} | {' ':6} | {r['remediation']}")
            if r["status"] == "FAIL":
                has_fail = True
            elif r["status"] == "WARN":
                warn_count += 1

        print("=" * 80)
        if has_fail:
            print("❌ 体检未通过：存在致命阻塞项，请参照上述修复指引完成后重试！")
            return 1
        elif warn_count > 0:
            print(f"⚠️  体检完成：存在 {warn_count} 项告警 (非致命)。若是本地离线自测可继续；若是真实双节点集群，请根据指引配置。")
            return 0
        else:
            print("✅ 体检全部通过：集群网络、内核环境、探针库与测试数据均已 100% 就绪，可安全启动压测！")
            return 0

def main():
    parser = argparse.ArgumentParser(description="集群环境连通性与配置预检工具")
    parser.add_argument("--config", default="./configs/cluster_env.json", help="集群配置 JSON 文件")
    parser.add_argument("--quick", action="store_true", help="快速探测模式")
    args = parser.parse_args()

    checker = PreflightChecker(config_path=args.config, quick_mode=args.quick)
    code = checker.run_all()
    sys.exit(code)

if __name__ == "__main__":
    main()
