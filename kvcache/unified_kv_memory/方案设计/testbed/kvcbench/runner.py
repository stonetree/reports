from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
import itertools
import json
import math
from pathlib import Path
import random
import time
import uuid

import aiohttp

from .analysis import summarize, generate_report
from .common import dump, read, digest
from .control import Controls, command, hook_evidence
from .replay import replay
from .sources import inventory
from .telemetry import Collector, derive_axes
from .trace import prepare


def make_plan(c: dict) -> list[dict]:
    cases = []
    for context, load, gap, profile in itertools.product(*(c["sweep"][k] for k in ("context_tokens", "load", "gap_s", "control_profiles"))):
        knobs = {"context_tokens": context, "load": load, "gap_s": gap, "control_profile": profile}
        cell = digest(knobs)[:12]
        for repeat in range(int(c["experiment"]["repeats"])):
            variants = list(c["experiment"]["variants"])
            if c["experiment"].get("randomize_order", True):
                random.Random(int(c["trace"].get("seed", 1)) + repeat + int(cell[:6],16)).shuffle(variants)
            for variant in variants:
                cases.append({"case_id": f"{cell}-r{repeat}-{variant}", "cell_id": cell,
                              "repeat": repeat, "variant": variant, "knobs": knobs.copy(), "measured_axes": {}})
    return cases


def schedule(base: list[dict], qps: float, duration: float, c: dict, namespace: str = "") -> list[dict]:
    """Morphed synthetic session arrivals preserve each session's configured gap.

    Normalized trace timestamps use explicit trace scaling instead. This does not
    model an agent's tool completion semantics; session turns use a FIFO lock.
    """
    if not base:
        raise ValueError("Empty workload")
    result = []
    if c["trace"].get("normalized_path"):
        times = sorted(float(r["arrival_s"]) for r in base)
        gaps = sorted(b-a for a,b in zip(times,times[1:]) if b>a)
        tail = gaps[len(gaps)//2] if gaps else 1.0
        original_window = float(c["trace"].get("normalized_window_s") or (times[-1]+tail))
        original_qps = len(base) / original_window
        factor = float(c["trace"].get("time_scale", 1)) * original_qps / qps
        if factor <= 0:
            raise ValueError("trace.time_scale must be positive")
        for row in base:
            row = copy.deepcopy(row)
            row["arrival_s"] = float(row["arrival_s"]) * factor
            row.setdefault("session_id", row["request_id"])
            row.setdefault("max_tokens", c["trace"]["output_tokens"])
            row["phase"] = "measure"
            if row["arrival_s"] < duration:
                result.append(row)
    else:
        sessions = {}
        for row in base:
            sessions.setdefault(row["session_id"], []).append(row)
        groups = list(sessions.values())
        average_turns = len(base) / len(groups)
        interval = average_turns / qps
        rng = random.Random(c["trace"].get("seed", 1))
        at, i = 0.0, 0
        while at < duration:
            group = sorted(groups[i % len(groups)], key=lambda x: (x.get("turn",0), x["arrival_s"]))
            first = min(r["arrival_s"] for r in group)
            for original in group:
                row = copy.deepcopy(original)
                row["arrival_s"] = at + original["arrival_s"] - first
                row["session_id"] = f'{original["session_id"]}:stream{i}'
                row["request_id"] = f'{original["request_id"]}:stream{i}'
                # New synthetic sessions must not all hit the same source-task prefix.
                if c["trace"].get("salt_mode", "session") in ("session", "request"):
                    tag = f"[KVCBENCH-SESSION:{i}]\n"
                    if isinstance(row["prompt"], str):
                        row["prompt"] = tag + row["prompt"]
                    elif "cache_salt" in row:
                        row["cache_salt"] += f":stream{i}"
                    else:
                        raise ValueError("Token-ID synthetic session isolation requires cache_salt or text prompts")
                if row["arrival_s"] < duration:
                    result.append(row)
            if c["trace"].get("arrival_process", "constant") == "poisson":
                at += rng.expovariate(1/interval)
            else:
                at += interval
            i += 1
            if len(result) > int(c["run"].get("max_requests", 1000000)):
                raise ValueError("Planned workload exceeds run.max_requests; reduce load/duration")
    for row in result:
        if namespace:
            # Isolation is identical for all requests in a case, preserving intended prefix sharing.
            if isinstance(row["prompt"], str):
                row["prompt"] = f"[KVCBENCH-RUN:{namespace}]\n" + row["prompt"]
            elif c["trace"].get("cache_salt_verified", False):
                row["cache_salt"] = namespace + ":" + row.get("cache_salt", "")
            else:
                raise ValueError("Token-ID replay with namespace isolation needs verified cache_salt support; use cache_policy=reset with commands instead")
        row["phase"] = "measure"
        # Token counts from the prepared corpus exclude run/session namespace.
        # Do not promote them into measured counts; server usage is authoritative.
        row["input_tokens_before_run_namespace"] = row.get("input_tokens")
    return sorted(result, key=lambda r: (r["arrival_s"],r["request_id"]))


def calibration_identity(c: dict) -> str:
    baseline = c["variants"][c.get("calibration",{}).get("variant","B0")]
    trace_keys = ("root","datasets","normalized_path","time_scale","normalized_window_s","turns","max_tasks","replicas",
                  "output_tokens","append_tokens","salt_mode","salt_transport","seed","tokenizer_path")
    return digest({"model":baseline["model"], "hardware_id":baseline.get("hardware_id"),
                   "accelerator_count":baseline["accelerator_count"], "slo":c["slo"],
                   "trace_contract":{k:c["trace"].get(k) for k in trace_keys}})


async def health(spec: dict, timeout=30):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        headers = {}
        if spec.get("api_key_env"):
            import os
            value = os.environ.get(spec["api_key_env"], "")
            if value:
                headers["Authorization"] = "Bearer " + value
        async with session.get(spec["base_url"].rstrip("/") + "/v1/models", headers=headers) as response:
            response.raise_for_status()
            data = await response.json()
        if spec["model"] not in [m.get("id") for m in data.get("data",[])]:
            raise ValueError(f"Configured model {spec['model']} not in /v1/models")
        return data


def new_run(c: dict, kind="experiment") -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    root = Path(c["run"]["output_root"]) / f"{stamp}-{kind}-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True)
    dump(root / "config.resolved.json", c)
    return root


async def run(c: dict, mock=False, calibration: dict | None = None) -> Path:
    root = new_run(c)
    source_info = inventory(c.get("sources", {})) if not mock else {}
    if not mock and c.get("sources", {}).get("strict", True):
        for key in ("vllm_ascend", "mooncake"):
            if not source_info.get(key,{}).get("exists"):
                raise ValueError(f"sources.{key} must point to an existing source checkout")
    evidence = "MOCK_SOFTWARE_ONLY" if mock else "HARDWARE_MEASURED"
    manifest = {"schema_version":1,"evidence_class":evidence,"created_utc": datetime.now(timezone.utc).isoformat(),
                "source_inventory":source_info,"status":"running", "trace_kind": "normalized_unverified" if c["trace"].get("normalized_path") else "synthetic_from_task_corpus",
                "limitations": ["Request timings measure the client-observed service including network and queueing.",
                                "Configuration descriptions are declared baseline semantics unless verified by a state command.",
                                "State-stall, physical bandwidth, external cache capacity and fetch/recompute economics require dedicated observations."]}
    dump(root / "manifest.json", manifest)
    if calibration is None:
        path = c.get("calibration",{}).get("evidence_path")
        calibration = read(Path(path)) if path else {}
    if calibration:
        if calibration.get("identity") != calibration_identity(c):
            raise ValueError("Calibration identity mismatch (model/hardware/SLO/trace contract); recalibrate")
        if not calibration.get("bracketed"):
            raise ValueError("Calibration has no observed passing/failing load bracket")
        if not mock and calibration.get("evidence_class") != "HARDWARE_MEASURED":
            raise ValueError("Mock or unclassified calibration cannot normalize a hardware run")
    reference = float(calibration.get("reference_qps") or c["experiment"].get("reference_qps",0))
    if reference <= 0:
        raise ValueError("Calibrate first, or configure experiment.reference_qps (declared; L is not measured without evidence)")
    plan = make_plan(c)
    dump(root / "plan.json", plan)
    dump(root / "calibration.json", calibration)
    print(f"Run: {root}; {len(plan)} cases", flush=True)
    try:
        for index, case in enumerate(plan,1):
            variant = c["variants"][case["variant"]]
            out = root / "cases" / case["case_id"]
            out.mkdir(parents=True)
            local = copy.deepcopy(c)
            local["trace"].update({k:case["knobs"][k] for k in ("context_tokens","gap_s")})
            local["_load"] = case["knobs"]["load"]
            base = prepare(local, out / "trace")
            if calibration.get("source_files") != read(out/"trace"/"trace_manifest.json").get("source_files") and calibration.get("source_files") is not None:
                raise ValueError("Trace source content changed since calibration; recalibrate")
            qps = reference * case["knobs"]["load"]
            canonical = schedule(base, qps, c["run"]["duration_s"], local)
            case["workload_id"] = digest(canonical)
            policy = variant.get("cache_policy", "namespace")
            requests = schedule(base, qps, c["run"]["duration_s"], local, root.name+":"+case["case_id"] if policy == "namespace" else "")
            offered = len(requests) / c["run"]["duration_s"]
            case["knobs"].update(qps=qps, offered_qps=offered)
            case["pairing_signature"] = digest({"model":variant["model"],"hardware_id":variant.get("hardware_id"),
                                                 "accelerator_count":variant["accelerator_count"], "slo": c["slo"],
                                                 "duration_s":c["run"]["duration_s"],"cache_policy":policy})
            case["baseline_declaration"] = variant
            case["cache_initial_state_verified"] = False
            case["cache_initial_state_note"] = "Namespace isolates hit keys; external capacity occupancy is not reset" if policy=="namespace" else "pending reset + verification"
            case["measurement_valid"] = False
            dump(out / "case.json", case)
            print(f"[{index}/{len(plan)}] {case['case_id']} offered={offered:.3f} rps", flush=True)
            values = {"case_id":case["case_id"],"case_dir":out,"variant":case["variant"],
                      "base_url":variant["base_url"],"context_tokens":case["knobs"]["context_tokens"],
                      "hook_control_file":c.get("hooks",{}).get("control_file", "")}
            if variant.get("prepare_argv"):
                await command(variant["prepare_argv"],values,out/"prepare_variant.json")
            if not mock:
                await health(variant)
            profile = c["controls"][case["knobs"]["control_profile"]]
            if case["variant"] not in profile.get("variants", c["experiment"]["variants"]):
                case["control_note"] = "Profile does not apply to this variant; retained as negative control"
                profile = c["controls"]["none"]
            async with Controls(c, profile, case["case_id"], out, values):
                if policy == "reset":
                    if not variant.get("cache_reset_argv") or not variant.get("cache_verify_argv"):
                        raise ValueError("cache_policy=reset requires reset and verify commands covering device AND external pools")
                    await command(variant["cache_reset_argv"],values,out/"cache_reset.json")
                    await command(variant["cache_verify_argv"],values,out/"cache_verify.json")
                    case["cache_initial_state_verified"] = True
                    case["cache_initial_state_note"] = "Configured reset + verify commands exited successfully; inspect command evidence for coverage"
                if variant.get("state_argv"):
                    await command(variant["state_argv"],values,out/"server_state.json")
                warm = int(c["run"].get("warmup_requests",0))
                if warm:
                    warm_requests = [dict(r, arrival_s=i*.02) for i,r in enumerate(requests[:warm])]
                    warm_spec = dict(c["run"], duration_s=max(.1,warm*.02),drain_s=c["run"]["request_timeout_s"])
                    warm_rows = await replay(warm_requests,variant,warm_spec,out/"warmup.jsonl",phase="warmup")
                    if any(r["status"] != "ok" for r in warm_rows):
                        raise RuntimeError("Warmup failed; do not proceed to measurement")
                collector = Collector(c,out,variant)
                job = asyncio.create_task(collector.run())
                try:
                    rows = await replay(requests,variant,c["run"],out/"requests.jsonl")
                finally:
                    collector.stop.set()
                    await job
                metrics = collector.summarize()
                summary = summarize(rows,c["run"]["duration_s"],variant["accelerator_count"],c["slo"])
                summary["evidence_class"] = evidence
                dump(out / "summary.json", summary)
                case["measured_axes"] = derive_axes(metrics,calibration,offered)
                hooks = hook_evidence(c,case["case_id"],out)
                def rule_observed(rule):
                    events=[json.loads(x) for x in (out/"hook_events.jsonl").read_text(encoding="utf-8").splitlines() if x]
                    return any(e.get("event")=="call" and all(e.get(k)==rule[k] for k in ("module","class","method")) and
                               (rule.get("action")=="observe" or e.get("applied") is True) for e in events)
                case["injection_verified"] = all(rule_observed(rule) for rule in profile.get("rules",[]))
                case["telemetry_complete"] = not metrics["missing"] and not metrics["errors"]
                case["measurement_valid"] = case["injection_verified"] and len(rows)==len(requests)
                dump(out / "case.json", case)
                if not case["injection_verified"]:
                    raise RuntimeError("Requested hook injection had zero observed applications; invalid case (see hook_summary.json)")
            await asyncio.sleep(float(c["run"].get("cooldown_s",0)))
        manifest["status"] = "completed"
    except BaseException as exc:
        manifest["status"] = "interrupted" if isinstance(exc,(KeyboardInterrupt,asyncio.CancelledError)) else "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        dump(root / "manifest.json", manifest)
        generate_report(root)
    return root
