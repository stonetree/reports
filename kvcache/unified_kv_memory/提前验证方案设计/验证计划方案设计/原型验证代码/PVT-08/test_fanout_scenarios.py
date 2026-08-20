#!/usr/bin/env python3
"""生成 PVT-08 可复现场景清单，不生成性能达标结论。"""
import argparse
import json
import uuid
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=["prompt_broadcast", "multi_agent", "pd_1p_to_nd"], required=True)
    parser.add_argument("--nodes", type=int, required=True)
    parser.add_argument("--payload-mb", type=int, default=64)
    parser.add_argument("--topology", choices=["unicast", "staging_fanout", "hardware_multicast"], required=True)
    parser.add_argument("--fault", choices=["none", "slow_node", "node_failure"], default="none")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="fanout_scenario.json")
    args = parser.parse_args()
    scenario = {
        "schema_version": "pvt08.scenario.v1",
        "workload_id": str(uuid.uuid4()),
        "scenario": args.scenario,
        "nodes": args.nodes,
        "payload_bytes": args.payload_mb * 1024 * 1024,
        "topology": args.topology,
        "fault": args.fault,
        "seed": args.seed,
        "required_outputs": ["source_egress_bytes", "consumer_complete_ns", "retries", "slow_node_impact"],
        "evidence_level": "DEMO",
        "status": "SCENARIO_ONLY",
    }
    Path(args.out).write_text(json.dumps(scenario, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": args.out, "workload_id": scenario["workload_id"]}))


if __name__ == "__main__":
    main()
