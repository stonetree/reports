#!/usr/bin/env python3
"""PVT-06 正确性驱动器。通过 stdin/stdout JSON 调用被测适配器，Oracle 独立判定。"""
import argparse
import json
import shlex
import subprocess
from pathlib import Path

TEST_CASES = [
    {"id": "C01", "conflict": "model_version", "expected": "REJECT_MODEL_MISMATCH"},
    {"id": "C02", "conflict": "tokenizer_hash", "expected": "REJECT_TOKENIZER_MISMATCH"},
    {"id": "C03", "conflict": "template_hash", "expected": "REJECT_TEMPLATE_MISMATCH"},
    {"id": "C04", "conflict": "lora_adapter", "expected": "REJECT_ADAPTER_MISMATCH"},
    {"id": "C05", "conflict": "ready_bit", "expected": "REJECT_NOT_READY"},
    {"id": "C06", "conflict": "lease_expired", "expected": "REJECT_LEASE_EXPIRED"},
    {"id": "C07", "conflict": "partial_match", "expected": "PARTIAL_ATTACH_PLAN"},
    {"id": "C08", "conflict": "rank_state_mismatch", "expected": "COORDINATED_FALLBACK_RECOMPUTE"},
]


def invoke(command: str, case: dict) -> dict:
    completed = subprocess.run(
        shlex.split(command), input=json.dumps(case), capture_output=True, text=True, timeout=30, check=False
    )
    if completed.returncode != 0:
        return {"status": "SUT_ERROR", "stderr": completed.stderr, "returncode": completed.returncode}
    return json.loads(completed.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sut-command", help="被测适配器命令；stdin 读取用例 JSON，stdout 输出实际 status JSON")
    parser.add_argument("--out", default="res_correctness.json")
    args = parser.parse_args()
    results = []
    for case in TEST_CASES:
        if args.sut_command:
            actual = invoke(args.sut_command, case)
            actual_status = actual.get("status")
            oracle_pass = actual_status == case["expected"]
        else:
            actual, oracle_pass = None, None
        results.append({**case, "actual": actual, "oracle_pass": oracle_pass})
    executed = args.sut_command is not None
    passed = executed and all(item["oracle_pass"] for item in results)
    summary = {
        "evidence_level": "LAB" if executed else "DEMO",
        "status": "PASS" if passed else ("FAIL" if executed else "NOT_EXECUTED"),
        "total_cases": len(results),
        "executed_cases": len(results) if executed else 0,
        "wrong_consume_count": 0 if passed else None,
        "results": results,
    }
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": args.out, "status": summary["status"]}))
    if not executed:
        return 2
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
