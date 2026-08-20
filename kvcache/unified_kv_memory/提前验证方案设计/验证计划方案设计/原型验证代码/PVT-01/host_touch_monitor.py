#!/usr/bin/env python3
"""采集 Host Payload Touch。缺少正式探针时失败关闭，不生成 0 或 PASS。"""
import argparse
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

BPF_PROGRAM = r"""
kprobe:memcpy, kprobe:memmove /pid == %d/ {
    @memcpy_calls = count();
    @memcpy_bytes = sum(arg2);
}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target_pid", type=int)
    parser.add_argument("duration_sec", nargs="?", type=int, default=10)
    parser.add_argument("--out", default="host_touch_evidence.json")
    parser.add_argument("--evidence-level", choices=["LAB", "MEASURED"], default="LAB")
    args = parser.parse_args()
    evidence = {
        "target_pid": args.target_pid,
        "duration_sec": args.duration_sec,
        "evidence_level": args.evidence_level,
        "probe": "bpftrace:kprobe_memcpy_memmove",
    }
    if not shutil.which("bpftrace"):
        evidence.update({"status": "INVALID_EVIDENCE", "host_touch_bytes": None, "invalid_reason": "bpftrace_not_found"})
        Path(args.out).write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps(evidence))
        return 2

    process = subprocess.Popen(
        ["bpftrace", "-e", BPF_PROGRAM % args.target_pid],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(args.duration_sec)
    process.terminate()
    stdout, stderr = process.communicate(timeout=10)
    match = re.search(r"@memcpy_bytes:\s*(\d+)", stdout)
    if match is None:
        evidence.update({"status": "INVALID_EVIDENCE", "host_touch_bytes": None, "invalid_reason": "probe_output_missing_memcpy_bytes", "stderr": stderr})
        code = 3
    else:
        evidence.update({"status": "OK", "host_touch_bytes": int(match.group(1)), "invalid_reason": None})
        code = 0
    Path(args.out).write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
