"""Baseline load calibration and external measured service-curve jobs."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .analysis import summarize
from .common import dump, digest
from .control import command
from .replay import replay
from .runner import new_run, schedule, health, calibration_identity
from .trace import prepare


async def calibrate(c: dict, mock=False) -> Path:
    root = new_run(c,"calibration")
    spec = c.get("calibration", {})
    name = spec.get("variant", "B0")
    variant = c["variants"][name]
    if not mock:
        await health(variant)
    local = copy.deepcopy(c)
    local["trace"]["gap_s"] = spec.get("gap_s",c["trace"].get("gap_s",0))
    base = prepare(local,root/"trace")
    trials = []
    reference = None
    bracketed = False
    for qps in sorted(spec.get("qps",[.5,1,2,4,8])):
        if qps <= 0:
            raise ValueError("calibration.qps must be positive")
        out = root/f"qps-{qps}"
        out.mkdir()
        run_spec = dict(c["run"], duration_s=spec.get("duration_s",c["run"]["duration_s"]))
        requests = schedule(base,qps,run_spec["duration_s"],local,f"{root.name}:cal-{qps}")
        warm=int(c["run"].get("warmup_requests",0))
        if warm:
            warm_requests=[dict(r,arrival_s=i*.02) for i,r in enumerate(requests[:warm])]
            warm_spec=dict(run_spec,duration_s=max(.1,warm*.02),drain_s=c["run"]["request_timeout_s"])
            warmed=await replay(warm_requests,variant,warm_spec,out/"warmup.jsonl",phase="warmup")
            if any(r["status"]!="ok" for r in warmed):
                raise RuntimeError("Calibration warmup failed")
        rows = await replay(requests,variant,run_spec,out/"requests.jsonl")
        s = summarize(rows,run_spec["duration_s"],variant["accelerator_count"],c["slo"])
        passed = bool(s["cohort_requests"] >= spec.get("min_requests",30) and
                      (s["cohort_compliance"] or 0) >= spec.get("required_compliance",.95) and
                      not s["status_counts"].get("dropped",0) and not s["status_counts"].get("error",0))
        trial = {"offered_qps":len(requests)/run_spec["duration_s"],"requested_qps":qps,"pass":passed,"summary":s}
        trials.append(trial)
        dump(out/"summary.json",trial)
        print(f"Calibration {qps} rps: pass={passed}, compliance={s['cohort_compliance']}",flush=True)
        if passed:
            reference = trial["offered_qps"]
        elif reference is not None:
            bracketed = True
            break
    service_curves = []
    for job in spec.get("jobs",[]):
        out = root/"service-curves"/job["name"]
        out.mkdir(parents=True)
        # Every tool provides its own raw data. No peak/specification values are invented.
        result = await command(job["argv"],{"output_dir":out},out/"command.json",job.get("timeout_s",300))
        try:
            observed = json.loads(result["stdout"])
        except json.JSONDecodeError as exc:
            raise ValueError(f"Service curve job {job['name']} must return a JSON object on stdout") from exc
        if not isinstance(observed,dict) or not observed.get("observations"):
            raise ValueError("service-curve output must contain nonempty observations plus benchmark conditions")
        dump(out/"observed.json",observed)
        service_curves.append({"name":job["name"],"path":str(out/"observed.json")})
    result = {"schema_version":1,"evidence_class":"MOCK_SOFTWARE_ONLY" if mock else "HARDWARE_MEASURED",
              "identity":calibration_identity(c),"source_files":json.loads((root/"trace"/"trace_manifest.json").read_text(encoding="utf-8")).get("source_files"),
              "baseline_variant":name,"model":variant["model"],"hardware_id":variant.get("hardware_id"),
              "accelerator_count":variant["accelerator_count"],"slo":c["slo"],"bracketed":bracketed,
              "reference_qps":reference if bracketed else None,"trials":trials,"service_curves":service_curves,
              "definition":"largest tested arrival rate meeting the configured SLO before the first failing higher rate; not a theoretical throughput maximum",
              "workload_id":digest(base),"trace_config":local["trace"]}
    dump(root/"calibration.json",result)
    if not bracketed:
        raise RuntimeError(f"Calibration unbracketed or insufficient samples; extend calibration.qps/duration. Raw evidence: {root}")
    return root
