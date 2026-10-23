"""Evidence-preserving metrics and reports for the HOST-KVC testbed.

This module deliberately does not collect host metrics.  It consumes the
per-request measurements emitted by the runner and keeps planned knobs apart
from measured axes, so that a report cannot silently promote a target into a
measurement.
"""
from __future__ import annotations

import csv
import html
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


TERMINAL_OK = {"ok"}
COHORT_STATUSES = {"ok", "error", "dropped", "timeout", "cancelled"}


def _num(value: Any) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def percentile(values: Iterable[float], p: float) -> float | None:
    """Linear-interpolated percentile; returns None rather than inventing data."""
    ordered = sorted(float(v) for v in values if _num(v) is not None)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, p)) * (len(ordered) - 1)
    lo, hi = int(math.floor(position)), int(math.ceil(position))
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def _ms(seconds: float | None) -> float | None:
    return None if seconds is None else seconds * 1000.0


def _completion_count(row: dict[str, Any]) -> tuple[int | None, bool]:
    count = row.get("completion_tokens")
    if isinstance(count, int) and count >= 0:
        return count, True
    usage = row.get("usage") or {}
    for key in ("completion_tokens", "output_tokens"):
        val = usage.get(key) if isinstance(usage, dict) else None
        if isinstance(val, int) and val >= 0:
            return val, True
    # SSE chunks are transport chunks, not tokens; never use them as a proxy.
    return None, False


def _request_metrics(row: dict[str, Any], slo: dict[str, Any]) -> dict[str, Any]:
    arrival = _num(row.get("arrival_s"))
    first = _num(row.get("first_token_s"))
    finish = _num(row.get("finish_s"))
    tokens, token_count_reliable = _completion_count(row)
    times = [_num(t) for t in (row.get("token_times_s") or [])]
    times = [t for t in times if t is not None]
    ttft = first - arrival if arrival is not None and first is not None and first >= arrival else None
    e2e = finish - arrival if arrival is not None and finish is not None and finish >= arrival else None
    quality = row.get("timing_quality")
    chunks = row.get("chunk_token_counts")
    strict_timing = quality == "server_token_timestamps" or (
        quality == "one_token_per_chunk" and isinstance(chunks, list) and token_count_reliable and
        all(isinstance(x, int) and x == 1 for x in chunks) and sum(chunks) == tokens and len(times) == tokens
    )
    tpot = None
    if strict_timing and token_count_reliable and tokens is not None and tokens > 1 and first is not None and times:
        last = max(times)
        if last >= first:
            tpot = (last - first) / (tokens - 1)
    chunk_gaps = [b - a for a, b in zip(times, times[1:]) if b >= a]
    delivery_proxy = None
    if token_count_reliable and tokens and tokens > 1 and first is not None and times:
        delivery_proxy = (max(times) - first) / (tokens - 1)
    single_token = token_count_reliable and tokens == 1
    min_output = slo.get("min_output_tokens")
    # An empty successful transport response is never a useful inference result.
    enough_output = tokens is not None and tokens > 0 and (min_output is None or tokens >= int(min_output))
    # A one-token answer has no inter-token interval, which is not a failure.
    tpot_ok = (single_token or slo.get("tpot_ms") is None or
               (tpot is not None and tpot * 1000 <= float(slo["tpot_ms"])))
    slo_ok = (
        row.get("status") in TERMINAL_OK and enough_output and
        (slo.get("ttft_ms") is None or (ttft is not None and ttft * 1000 <= float(slo["ttft_ms"]))) and
        tpot_ok and
        (slo.get("e2e_ms") is None or (e2e is not None and e2e * 1000 <= float(slo["e2e_ms"])))
    )
    return {"ttft_s": ttft, "e2e_s": e2e, "tpot_s": tpot,
            "tokens": tokens, "tokens_reliable": token_count_reliable,
            "single_token": single_token, "chunk_gaps_s": chunk_gaps, "delivery_tpot_proxy_s": delivery_proxy,
            "timing_quality": quality, "strict_timing": strict_timing,
            "slo_ok": bool(slo_ok)}


def summarize(rows: list[dict[str, Any]], duration_s: float,
              accelerator_count: int, slo: dict[str, Any]) -> dict[str, Any]:
    """Summarize one measure phase.

    Arrival cohort is fixed to ``[0, duration_s)``.  Every request in that
    cohort contributes to the SLO denominator, including errors and drops.
    A request finishing after the window is not goodput in the window, even if
    it eventually meets the SLO after drain.
    """
    if duration_s <= 0 or accelerator_count <= 0:
        raise ValueError("duration_s and accelerator_count must be positive")
    measured = [{**r, "_m": _request_metrics(r, slo)} for r in rows
                if r.get("phase", "measure") == "measure"]
    cohort = [r for r in measured if (_num(r.get("arrival_s")) is not None and
                                      0 <= _num(r.get("arrival_s")) < duration_s)]
    finished_window = [r for r in cohort if _num(r.get("finish_s")) is not None and
                       _num(r["finish_s"]) <= duration_s]
    good = [r for r in finished_window if r["_m"]["slo_ok"]]
    eventual_good = [r for r in cohort if r["_m"]["slo_ok"]]
    drained = [r for r in measured if r.get("status") == "ok" and _num(r.get("finish_s")) is not None and
               _num(r["finish_s"]) >= duration_s]
    all_finished = [r for r in measured if _num(r.get("finish_s")) is not None]
    end = max([duration_s] + [_num(r["finish_s"]) for r in all_finished])
    drain_s = max(0.0, end - duration_s)
    useful_tokens = sum(r["_m"]["tokens"] or 0 for r in good)
    drain_tokens = sum(r["_m"]["tokens"] or 0 for r in drained)
    # Latency percentiles describe completed successful requests. Failures and
    # drops are represented in the fixed-cohort SLO denominator/status table.
    metric_rows = [r["_m"] for r in measured if r.get("status") == "ok"]
    def q(name: str, p: float) -> float | None:
        return _ms(percentile((m[name] for m in metric_rows if m[name] is not None), p))
    status_counts: dict[str, int] = defaultdict(int)
    for r in cohort:
        status_counts[str(r.get("status", "unknown"))] += 1
    return {
        "schema_version": 1, "duration_s": duration_s,
        "accelerator_count": accelerator_count, "slo": slo,
        "cohort_requests": len(cohort), "completed_in_window": len(finished_window),
        "slo_pass_in_window": len(good), "eventual_slo_pass": len(eventual_good),
        "cohort_compliance": len(good) / len(cohort) if cohort else None,
        "eventual_cohort_compliance": len(eventual_good) / len(cohort) if cohort else None,
        "slo_goodput_rps": len(good) / duration_s,
        "slo_goodput_per_accelerator_rps": len(good) / duration_s / accelerator_count,
        "useful_output_tokens": useful_tokens,
        "useful_output_tokens_per_s": useful_tokens / duration_s,
        "status_counts": dict(status_counts), "drain_s": drain_s,
        "drain_completed_requests": len(drained),
        "drain_throughput_rps": len(drained) / drain_s if drain_s else None,
        "drain_output_tokens_per_s": drain_tokens / drain_s if drain_s else None,
        "latency_ms": {"ttft_p50": q("ttft_s", .50), "ttft_p90": q("ttft_s", .90),
                       "ttft_p99": q("ttft_s", .99), "e2e_p50": q("e2e_s", .50),
                       "e2e_p99": q("e2e_s", .99), "tpot_p50": q("tpot_s", .50),
                       "tpot_p99": q("tpot_s", .99),
                       "delivery_tpot_proxy_p99": q("delivery_tpot_proxy_s", .99),
                       "stream_chunk_gap_p99": _ms(percentile(
                           (gap for m in metric_rows for gap in m["chunk_gaps_s"]), .99))},
        "client_dispatch_delay_ms": percentile(((_num(r.get("dispatch_s")) - _num(r.get("arrival_s"))) * 1000 for r in measured if _num(r.get("dispatch_s")) is not None and _num(r.get("arrival_s")) is not None and _num(r.get("dispatch_s")) >= _num(r.get("arrival_s"))), .99),
        "missing": {"ttft": sum(m["ttft_s"] is None for m in metric_rows),
                    "e2e": sum(m["e2e_s"] is None for m in metric_rows),
                    "tpot": sum(m["tpot_s"] is None and not m["single_token"] for m in metric_rows),
                    "unreliable_token_count": sum(not m["tokens_reliable"] for m in metric_rows)},
        "definitions": {"ttft": "arrival to first token; includes client queueing",
                        "tpot": "strict server token timestamps or verified one-token-per-chunk only; unknown/batched SSE fails TPOT SLO conservatively",
                        "chunk_gap": "SSE transport delivery gap, not TPOT"},
        "latency_population": "successful requests only; errors/drops remain in SLO-goodput denominator",
    }


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists(): return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ci(values: list[float], repeats: int = 2000) -> list[float | None]:
    if len(values) < 3: return [None, None]
    rng = random.Random(20261023)
    means = [sum(rng.choice(values) for _ in values) / len(values) for _ in range(repeats)]
    return [percentile(means, .025), percentile(means, .975)]


def _svg_surface(cells: list[dict[str, Any]], out: Path) -> None:
    # SVG remains inspectable without a plotting dependency.
    w, h, margin = 880, 460, 65
    pts = [c for c in cells if _num(c.get("x")) is not None and _num(c.get("y")) is not None and _num(c.get("z")) is not None]
    title = "SLO goodput / accelerator on measured P_HBM / L axes"
    if not pts:
        out.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"><text x="20" y="40">No surface: measured P_HBM and L are absent.</text></svg>', encoding="utf-8"); return
    xs, ys = [float(p["x"]) for p in pts], [float(p["y"]) for p in pts]
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    def scale(v, lo, hi, size): return margin + (v - lo) / (hi - lo or 1) * size
    shapes = []
    for p in pts:
        z = max(-1.0, min(1.0, float(p["z"])))
        color = f"rgb({int(220*(1-z)/2)}, {int(90+140*(z+1)/2)}, {int(220*(z+1)/2)})"
        x, y = scale(float(p["x"]), xmin, xmax, w-2*margin), h-scale(float(p["y"]), ymin, ymax, h-2*margin)
        shapes.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="15" fill="{color}"/><text x="{x+18:.1f}" y="{y+4:.1f}" font-size="11">{float(p["z"]):+.3f}</text>')
    text = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"><style>text{{font-family:Arial}}</style>'
            f'<text x="20" y="26" font-size="16">{title}</text><line x1="{margin}" y1="{h-margin}" x2="{w-margin}" y2="{h-margin}" stroke="black"/><line x1="{margin}" y1="{margin}" x2="{margin}" y2="{h-margin}" stroke="black"/>'
            f'<text x="{w/2:.0f}" y="{h-12}" text-anchor="middle">P_HBM (measured)</text><text x="16" y="{h/2:.0f}" transform="rotate(-90 16 {h/2:.0f})">L (measured)</text>{"".join(shapes)}</svg>')
    out.write_text(text, encoding="utf-8")


def _line_fit(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Return intercept, slope, SSE for a least-squares line."""
    xs, ys = zip(*points)
    mx, my = statistics.mean(xs), statistics.mean(ys)
    slope = sum((x - mx) * (y - my) for x, y in points) / (sum((x - mx) ** 2 for x in xs) or 1.0)
    intercept = my - slope * mx
    return intercept, slope, sum((y - (intercept + slope*x))**2 for x, y in points)


def _knee(cells: list[dict[str, Any]]) -> dict[str, Any]:
    """Find a *descriptive* knee within one comparable experimental slice.

    The caller is responsible for grouping by variant, context, gap, profile,
    E slice, model/hardware and workload signature.  Cross-slice aggregation
    is prohibited because it can manufacture a knee from unrelated workloads.
    """
    buckets: dict[float, list[float]] = defaultdict(list)
    for c in cells:
        load, p99 = _num(c.get("load")), _num(c.get("ttft_p99"))
        if load is not None and p99 is not None:
            buckets[load].append(p99)
    points = sorted((x, statistics.mean(v)) for x, v in buckets.items())
    if len(points) < 6:
        return {"status":"insufficient_unique_load_points", "observed":len(points), "minimum":6}
    _, _, linear_sse = _line_fit(points)
    candidates = []
    for split in range(2, len(points) - 1):
        _, before, sse_before = _line_fit(points[:split])
        _, after, sse_after = _line_fit(points[split:])
        candidates.append((sse_before + sse_after, before, after, split))
    piece_sse, before, after, split = min(candidates)
    # Require a material positive steepening and material improvement over one line.
    if not (after > max(before * 1.5, before + 1e-9) and piece_sse < linear_sse * 0.80):
        return {"status":"no_supported_knee", "linear_sse":linear_sse, "piecewise_sse":piece_sse,
                "slope_before_ms_per_load":before, "slope_after_ms_per_load":after,
                "caution":"No material positive slope change with adequate fit improvement."}
    # Bootstrap only exists if every load has >=3 independent repetitions.
    repetitions = min(len(v) for v in buckets.values())
    ci = [None, None]
    if repetitions >= 3:
        rng = random.Random(20261023); estimates=[]
        for _ in range(500):
            sample=sorted((x, statistics.mean(rng.choice(v) for _ in v)) for x,v in buckets.items())
            allc=[]
            for s in range(2, len(sample)-1):
                _, a, ea=_line_fit(sample[:s]); _, b, eb=_line_fit(sample[s:]); allc.append((ea+eb,s,a,b))
            _, s, a, b=min(allc)
            if b > max(a*1.5, a+1e-9): estimates.append((sample[s-1][0]+sample[s][0])/2)
        if estimates: ci=[percentile(estimates,.025), percentile(estimates,.975)]
    return {"status":"candidate", "load_interval":[points[split-1][0],points[split][0]],
            "slope_before_ms_per_load":before,"slope_after_ms_per_load":after,
            "linear_sse":linear_sse,"piecewise_sse":piece_sse,
            "bootstrap_ci95_knee_load":ci,"min_repetitions_per_load":repetitions,
            "caution":"Descriptive candidate only; CI is null unless each load has >=3 repeats."}


def _safe_key(record: dict[str, Any]) -> tuple[Any, ...] | None:
    sig = record.get("pairing_signature")
    workload = record.get("workload_id")
    # Both identities are mandatory. A signature describes environment/SLO/cache
    # policy; it cannot prove that two runs replayed the same workload.
    if sig in (None, "") or workload in (None, ""):
        return None
    return ("workload", workload, "signature", json.dumps(sig, sort_keys=True, ensure_ascii=False))


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer=csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)


def _telemetry_value(data: Any, key: str) -> float | None:
    """Accept a standard telemetry metric only; never derive it from load."""
    if isinstance(data, dict):
        if key in data:
            value=data[key]
            if isinstance(value,dict): return _num(value.get("mean"))
            return _num(value)
        for value in data.values():
            found=_telemetry_value(value,key)
            if found is not None: return found
    return None


def _e_bin(value: Any) -> str:
    value=_num(value)
    if value is None: return "unobserved"
    if value < 1: return "<1"
    if value < 2: return "1-2"
    if value < 4: return "2-4"
    return ">=4"


def _svg_surface(cells: list[dict[str, Any]], out: Path, title: str, x_label: str, y_label: str) -> None:
    w,h,m=960,540,82
    pts=[p for p in cells if all(_num(p.get(k)) is not None for k in ("x","y","z"))]
    if not pts:
        out.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"><text x="20" y="40">无可绘制的可比数据点；请检查实测轴或配对条件。</text></svg>',encoding="utf-8"); return
    xs,ys,zs=([float(p[k]) for p in pts] for k in ("x","y","z")); xmin,xmax=min(xs),max(xs); ymin,ymax=min(ys),max(ys); zmin,zmax=min(zs),max(zs)
    def sx(v): return m+(v-xmin)/(xmax-xmin or 1)*(w-2*m)
    def sy(v): return h-m-(v-ymin)/(ymax-ymin or 1)*(h-2*m)
    def color(z):
        u=(z-zmin)/(zmax-zmin or 1); return f"rgb({int(40+190*(1-u))},{int(70+120*u)},{int(220*u)})"
    circles="".join(f'<circle cx="{sx(p["x"]):.1f}" cy="{sy(p["y"]):.1f}" r="15" fill="{color(float(p["z"]))}"/><text x="{sx(p["x"])+18:.1f}" y="{sy(p["y"])+4:.1f}" font-size="11">{float(p["z"]):+.3f}</text>' for p in pts)
    legend="".join(f'<rect x="{w-55}" y="{70+i*3}" width="16" height="3" fill="{color(zmin+(zmax-zmin)*i/100)}"/>' for i in range(101))
    svg=(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"><style>text{{font-family:Arial,"Microsoft YaHei"}}</style><text x="20" y="28" font-size="16">{html.escape(title)}</text><line x1="{m}" y1="{h-m}" x2="{w-m}" y2="{h-m}" stroke="black"/><line x1="{m}" y1="{m}" x2="{m}" y2="{h-m}" stroke="black"/><text x="{w/2}" y="{h-14}" text-anchor="middle">{html.escape(x_label)}</text><text x="20" y="{h/2}" transform="rotate(-90 20 {h/2})">{html.escape(y_label)}</text><text x="{w-34}" y="68" font-size="10">{zmax:.3f}</text><text x="{w-34}" y="390" font-size="10">{zmin:.3f}</text>{legend}{circles}</svg>')
    out.write_text(svg,encoding="utf-8")


def generate_report(run_dir: Path) -> dict[str, Any]:
    """Make an idempotent, Chinese evidence-bounded test report."""
    run_dir=Path(run_dir); reports=run_dir/"reports"; reports.mkdir(parents=True,exist_ok=True)
    mf=run_dir/"manifest.json"; manifest=json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
    records=[]
    for d in sorted((run_dir/"cases").glob("*")) if (run_dir/"cases").exists() else []:
        if not (d/"case.json").exists() or not (d/"summary.json").exists(): continue
        case=json.loads((d/"case.json").read_text(encoding="utf-8")); summary=json.loads((d/"summary.json").read_text(encoding="utf-8")); axes=case.get("measured_axes") or {}; knobs=case.get("knobs") or {}
        declared=case.get("baseline_declaration") or {}; telemetry_path=d/"telemetry_summary.json"
        telemetry=json.loads(telemetry_path.read_text(encoding="utf-8")) if telemetry_path.exists() else {}
        r={"case_id":case.get("case_id",d.name),"variant":case.get("variant"),"repeat":case.get("repeat"),"cell_id":case.get("cell_id"),"workload_id":case.get("workload_id"),"pairing_signature":case.get("pairing_signature"),"model_fingerprint":declared.get("model"),"hardware_fingerprint":declared.get("hardware_id"),"P_HBM":_num(axes.get("P_HBM")),"E_reuse":_num(axes.get("E_reuse")),"L":_num(axes.get("L")),"planned_load":_num(knobs.get("load",knobs.get("qps"))),"context_tokens":knobs.get("context_tokens"),"gap_s":knobs.get("gap_s"),"control_profile":knobs.get("control_profile"),"goodput":_num(summary.get("slo_goodput_per_accelerator_rps")),"ttft_p99":_num((summary.get("latency_ms") or {}).get("ttft_p99")),"cohort":summary.get("cohort_requests"),"missing":summary.get("missing",{}),"injection_verified":case.get("injection_verified",False),"measurement_valid":case.get("measurement_valid",False),"cache_initial_state_verified":case.get("cache_initial_state_verified",False),"baseline_verified":case.get("baseline_verified",False),"telemetry_complete":case.get("telemetry_complete",False),"case_status":case.get("status","unknown"),"raw_case":"../cases/"+d.name}
        for metric in ("state_stall_ratio","live_kv_demand_bytes","kv_budget_bytes","cpu_cores","ddr_bytes_per_s","nic_bytes_per_s","effective_external_kv_capacity_bytes"):
            r[metric]=_telemetry_value(telemetry,metric)
        r["telemetry_missing_detail"]=telemetry.get("missing",[]) if isinstance(telemetry,dict) else []
        r["telemetry_errors_detail"]=telemetry.get("errors",[]) if isinstance(telemetry,dict) else []
        r["cost_per_useful_token"]=_num((summary.get("cost") or {}).get("cost_per_useful_token"))
        r["pair_key"]=_safe_key(r); r["summary"]=summary; records.append(r)
    fields=["case_id","variant","repeat","cell_id","workload_id","pairing_signature","model_fingerprint","hardware_fingerprint","P_HBM","E_reuse","L","planned_load","context_tokens","gap_s","control_profile","goodput","ttft_p99","cohort","injection_verified","measurement_valid","cache_initial_state_verified","baseline_verified","telemetry_complete","case_status","state_stall_ratio","live_kv_demand_bytes","kv_budget_bytes","cpu_cores","ddr_bytes_per_s","nic_bytes_per_s","effective_external_kv_capacity_bytes","cost_per_useful_token","raw_case"]
    _write_csv(reports/"cells.csv",fields,records)
    groups=defaultdict(lambda:defaultdict(list)); unpairable_cells=set()
    for r in records:
        # Missing comparison identity is a failed pairing, never a shared None key.
        if r.get("pair_key") is not None:
            groups[(r.get("cell_id"),r.get("pair_key"))][r.get("variant")].append(r)
        else:
            unpairable_cells.add(r.get("cell_id"))
    attrs=[]
    for cell in sorted(str(x) for x in unpairable_cells):
        attrs.append({"cell_id":cell,"pairing_key":"", "contrast":"未配对：comparison identity 缺失", "paired_repeats":0,
                      "delta_goodput_per_accelerator_rps":None,"ci95":json.dumps([None,None]),"P_HBM":None,"E_reuse":None,"L":None,"planned_load":None,"context_tokens":None,"gap_s":None,"control_profile":None,"evidence_grade":"UNPAIRED","interpretation":"缺少 pairing_signature 或 workload_id，未计算差值"})
    for (cell,key),v in groups.items():
        for lo,hi,label in (("B0","B1","外部容量增量 B1-B0"),("B1","B2","控制策略增量 B2-B1"),("B2","B3","优化系统增量 B3-B2")):
            left={str(x.get("repeat")):x for x in v.get(lo,[])}; right={str(x.get("repeat")):x for x in v.get(hi,[])}; delta=[]; sample=None
            strict=[]
            for rep in left.keys() & right.keys():
                a,b=left[rep],right[rep]
                if a["goodput"] is not None and b["goodput"] is not None:
                    delta.append(b["goodput"]-a["goodput"]); sample=b
                    strict.append(manifest.get("evidence_class") == "HARDWARE_MEASURED" and all(x.get(flag) is True for x in (a,b) for flag in ("injection_verified","measurement_valid","cache_initial_state_verified","baseline_verified")) and bool(a.get("hardware_fingerprint")) and bool(b.get("hardware_fingerprint")))
            grade="STRICT_ATTRIBUTION_READY" if delta and all(strict) else ("EXPLORATORY_INITIAL_STATE_UNCONTROLLED" if delta else "UNPAIRED")
            attrs.append({"cell_id":cell,"pairing_key":json.dumps(key,ensure_ascii=False),"contrast":label,"paired_repeats":len(delta),"delta_goodput_per_accelerator_rps":statistics.mean(delta) if delta else None,"ci95":json.dumps(_ci(delta),ensure_ascii=False),"P_HBM":sample and sample["P_HBM"],"E_reuse":sample and sample["E_reuse"],"L":sample and sample["L"],"planned_load":sample and sample["planned_load"],"context_tokens":sample and sample["context_tokens"],"gap_s":sample and sample["gap_s"],"control_profile":sample and sample["control_profile"],"evidence_grade":grade,"interpretation":"系统级反事实差值；仅 STRICT_ATTRIBUTION_READY 可进入严格归因，且仍不等于 CPU 单独因果贡献"})
    attr_fields=list(attrs[0]) if attrs else ["cell_id","pairing_key","contrast","paired_repeats","delta_goodput_per_accelerator_rps","ci95"]
    _write_csv(reports/"attribution.csv",attr_fields,attrs)
    # Value surface is B1-B0 only. Prefer measured axes; a separately labelled
    # planned-proxy surface is produced if measured P_HBM is absent.
    cap=[a for a in attrs if a["contrast"].startswith("外部容量") and a["delta_goodput_per_accelerator_rps"] is not None]
    measured=[{"x":a["P_HBM"],"y":a["L"],"z":a["delta_goodput_per_accelerator_rps"]} for a in cap]
    _svg_surface(measured,reports/"surface.svg","价值曲面概览：B1-B0 每加速器 SLO goodput 增量（实测轴；切片见 surface_facets）","P_HBM（实测）","L（实测）")
    proxy=[{"x":_num(a["context_tokens"]),"y":a["planned_load"],"z":a["delta_goodput_per_accelerator_rps"]} for a in cap]
    _svg_surface(proxy,reports/"surface_planned_proxy.svg","价值曲面概览：B1-B0 增量（计划代理，非实测 P_HBM；切片见 surface_facets）","上下文 token（计划输入）","负载（计划输入）")
    facet_dir=reports/"surface_facets"; facet_dir.mkdir(exist_ok=True)
    facets=defaultdict(list)
    for a in cap:
        facets[(a["gap_s"],a["control_profile"],a["P_HBM"] is not None and a["L"] is not None,_e_bin(a["E_reuse"]))].append(a)
    facet_index=[]
    for i,(facet,values) in enumerate(sorted(facets.items(),key=lambda x:str(x[0]))):
        measured_axis=facet[2]; points=[{"x":a["P_HBM"] if measured_axis else _num(a["context_tokens"]),"y":a["L"] if measured_axis else a["planned_load"],"z":a["delta_goodput_per_accelerator_rps"]} for a in values]
        filename=f"facet_{i:03d}.svg"; kind="实测轴" if measured_axis else "计划代理轴"
        _svg_surface(points,facet_dir/filename,f"B1-B0 value surface / {kind} / gap={facet[0]}, control={facet[1]}, E_reuse={facet[3]}","P_HBM（实测）" if measured_axis else "上下文 token（计划）","L（实测）" if measured_axis else "负载（计划）")
        facet_index.append({"file":"surface_facets/"+filename,"gap_s":facet[0],"control_profile":facet[1],"axis_kind":kind,"E_reuse_bin":facet[3]})
    _write_csv(reports/"surface_facets.csv",["file","gap_s","control_profile","axis_kind","E_reuse_bin"],facet_index)
    slices=defaultdict(list)
    for r in records:
        # Variant is mandatory: never pool B0/B1/... on the same knee curve.
        slices[(r["variant"],r["context_tokens"],r["gap_s"],r["control_profile"],_e_bin(r["E_reuse"]),r["pairing_signature"])].append({"load":r["L"] if r["L"] is not None else r["planned_load"],"ttft_p99":r["ttft_p99"]})
    knees=[{"slice":str(k),"result":_knee(v)} for k,v in slices.items()]
    next_loads=[]
    for k in knees:
        r=k["result"]
        if r.get("status")=="candidate":
            a,b=r["load_interval"]; next_loads += [round(a+(b-a)*i/4,6) for i in (1,2,3)]
    refinement={"knees":knees,"next_loads":sorted(set(next_loads)),"axis_note":"Knee 曲线按 variant/context_tokens/gap_s/control_profile/E_reuse/配对签名隔离；L 缺失时仅使用计划负载作探索代理。"}
    (reports/"refinement.json").write_text(json.dumps(refinement,indent=2,ensure_ascii=False),encoding="utf-8")
    knee_rows=[]
    for item in knees:
        result=item["result"]
        knee_rows.append({"slice":item["slice"],"status":result.get("status"),"load_interval":json.dumps(result.get("load_interval"),ensure_ascii=False),"slope_before":result.get("slope_before_ms_per_load"),"slope_after":result.get("slope_after_ms_per_load"),"bootstrap_ci95":json.dumps(result.get("bootstrap_ci95_knee_load"),ensure_ascii=False),"caution":result.get("caution")})
    _write_csv(reports/"knees.csv",["slice","status","load_interval","slope_before","slope_after","bootstrap_ci95","caution"],knee_rows)
    regions=[]
    for r in records:
        # R1 is only a candidate when fetch/recompute economics is actually measured.
        label="unknown"; evidence="unobserved"
        if r["E_reuse"] is not None:
            evidence="measured_E_reuse"
            if r["E_reuse"] < 1: label="R1_candidate_fetch_not_economic"
        regions.append({"case_id":r["case_id"],"region":label,"evidence":evidence,"E_reuse":r["E_reuse"],"P_HBM":r["P_HBM"],"L":r["L"],"rule_version":"v1: R1 iff measured E_reuse<1; R0/R2-R5 unclassified"})
    _write_csv(reports/"regions.csv",["case_id","region","evidence","E_reuse","P_HBM","L","rule_version"],regions)
    diag=[]
    for r in records:
        stall,cap=r["state_stall_ratio"],r["effective_external_kv_capacity_bytes"]
        if stall is not None and cap is not None:
            diag.append({"x":stall,"y":cap,"z":r["goodput"]})
    _svg_surface(diag,reports/"diagnostic_surfaces.svg","诊断曲面：实测 State Stall 与有效外部 KV 容量（仅完整 telemetry）","State stall ratio（实测）","Effective external KV capacity bytes（实测）")
    (reports/"region_readme.md").write_text("# Region candidate rules\n\n`unknown` 表示没有足够实测轴，不能从高利用率或低 goodput 推断 R3/R4 等因果区域。`R1_candidate_fetch_not_economic` 的唯一规则是实测 `E_reuse = T_recompute/T_fetch < 1`；它仅提示复用经济性待复核，不自动宣称负净价值。`diagnostic_surfaces.svg` 只绘制同时具备实测 `state_stall_ratio` 与 `effective_external_kv_capacity_bytes` 的 case；缺失 telemetry 一律为 `unobserved`。\n",encoding="utf-8")
    missing={k:sum(int((r["missing"] or {}).get(k,0)) for r in records) for k in ("ttft","e2e","tpot","unreliable_token_count")}
    telemetry_metric_counts={k:sum(r.get(k) is not None for r in records) for k in ("state_stall_ratio","live_kv_demand_bytes","kv_budget_bytes","cpu_cores","ddr_bytes_per_s","nic_bytes_per_s","effective_external_kv_capacity_bytes")}
    telemetry_faults=sorted({str(x) for r in records for x in (r.get("telemetry_missing_detail") or [])+(r.get("telemetry_errors_detail") or [])})
    telemetry=[]
    for d in sorted((run_dir/"cases").glob("*")) if (run_dir/"cases").exists() else []:
        p=d/"telemetry_summary.json"
        if p.exists(): telemetry.append(d.name)
    cost=manifest.get("config",{}).get("hourly_cost") if isinstance(manifest.get("config"),dict) else None
    lines=["# HOST-KVC Value Surface 测试报告","",f"证据等级：`{manifest.get('evidence_class','UNSPECIFIED')}`。本报告只陈述该运行目录的测量结果；若 workload 由任务语料构造、加盐或压力变形而来，它是实验输入，不是生产 trace。","",f"共读取 {len(records)} 个 case。原始 case 文件可通过 `cells.csv` 的 `raw_case` 相对路径追溯。","", "## 指标口径","", "SLO goodput 只计固定到达窗口内、满足全部 SLO 且窗口结束前完成的请求；错误、超时和丢弃请求仍留在分母。TTFT 从请求到达开始，包含客户端排队。TPOT 仅接受服务器逐 token 时间戳或已验证的一 token 一 chunk；未知或批量 SSE 会保守地不通过 TPOT SLO。Drain 吞吐量单列，且只统计成功请求。","", "## 证据边界与缺口","",f"缺失指标计数：`{json.dumps(missing,ensure_ascii=False)}`。Telemetry 各标准字段有效 case 数：`{json.dumps(telemetry_metric_counts,ensure_ascii=False)}`；collector 声明缺失/错误：`{telemetry_faults or ['无']}`。",f"成本：{cost if cost is not None else '未提供'}；若配置提供，该值仅为 SCENARIO INPUT，不是测得 TCO。","", "## Value Surface","", "`surface.svg` 是 B1-B0 的系统级外部容量增量，并仅采用实测 P_HBM/L。`surface_planned_proxy.svg` 采用 context/负载计划输入作为代理，不能写作实测压力。不同 gap、控制档位和 E_reuse 切片见 `surface_facets/`。","", "## 归因对账","", "配对要求 workload_id 与 pairing_signature 均非空且一致。`STRICT_ATTRIBUTION_READY` 还要求硬件实测运行、两侧均验证基线、注入、测量有效、缓存初始状态以及硬件标识；其余差值仅为 `EXPLORATORY_INITIAL_STATE_UNCONTROLLED`，不可作严格因果归因。","", "| Cell | 对比 | 证据等级 | 成对重复 | Δ SLO goodput/加速器 | 95% CI |","|---|---|---|---:|---:|---|"]
    lines += [f"| {a['cell_id']} | {a['contrast']} | {a.get('evidence_grade','UNPAIRED')} | {a['paired_repeats']} | {a['delta_goodput_per_accelerator_rps']} | {a['ci95']} |" for a in attrs]
    lines += ["","## Case 指标索引","", "所有 case 的 variant、实测轴、计划输入、SLO goodput、TTFT P99、原始文件链接均在 `cells.csv`；完整请求明细在各 `raw_case/requests.jsonl`。","", "## Knee 候选","", "Knee 仅在同一实验切片中检查；要求分段模型比线性模型的 SSE 至少降低 20%，且后段斜率显著升高。每个负载点少于 3 个独立重复时不报告 Knee 置信区间。详细结果见 `refinement.json`。"]
    md="\n".join(lines)+"\n"; (reports/"report.md").write_text(md,encoding="utf-8"); (reports/"report.html").write_text("<!doctype html><meta charset='utf-8'><title>HOST-KVC 报告</title><pre>"+html.escape(md)+"</pre><p><img src='surface.svg'></p><p><img src='surface_planned_proxy.svg'></p>",encoding="utf-8")
    return {"reports_dir":str(reports),"cases":len(records),"attribution":attrs,"refinement":refinement,"missing":missing,"surface_facets":facet_index}
