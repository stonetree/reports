"""Poll raw Prometheus exposition and configured JSON collectors; keep missing data."""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import re
import time

import aiohttp

from .common import dump


SAMPLE = re.compile(r'^([a-zA-Z_:][\w:]*)(\{[^\n]*\})?\s+([^\s]+)(?:\s+[^\s]+)?$')
LABEL = re.compile(r'(\w+)="((?:\\.|[^"\\])*)"')


def parse_prometheus(text: str, name: str, labels: dict | None = None) -> list[float]:
    values = []
    for line in text.splitlines():
        match = SAMPLE.match(line)
        if not match or match[1] != name:
            continue
        present = {k: json.loads('"' + v + '"') for k, v in LABEL.findall(match[2] or "")}
        if any(present.get(k) != str(v) for k, v in (labels or {}).items()):
            continue
        try:
            value = float(match[3])
            if math.isfinite(value):
                values.append(value)
        except ValueError:
            continue
    return values


def quantile(values, p):
    if not values:
        return None
    values = sorted(values)
    pos = (len(values) - 1) * p
    lo = int(pos)
    return values[lo] + (values[min(lo+1, len(values)-1)] - values[lo]) * (pos-lo)


class Collector:
    def __init__(self, config: dict, out: Path, variant: dict):
        self.config, self.out, self.variant = config, out, variant
        self.stop = asyncio.Event()
        self.samples = []
        self.errors = []

    async def run(self):
        self.out.mkdir(parents=True, exist_ok=True)
        spec = self.config.get("telemetry", {})
        interval = float(spec.get("interval_s", 1))
        if interval <= 0:
            raise ValueError("telemetry.interval_s must be positive")
        maps = spec.get("metrics", [])
        urls = set(self.variant.get("metrics_urls", [])) | {m["url"] for m in maps if m.get("url")}
        start = time.perf_counter()
        with (self.out / "telemetry.jsonl").open("w", encoding="utf-8") as stream:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=spec.get("timeout_s", 5))) as session:
                i = 0
                while not self.stop.is_set():
                    at = time.perf_counter() - start
                    snapshot = {"elapsed_s": at, "wall_time_ns": time.time_ns(), "values": {}}
                    for url in sorted(urls):
                        try:
                            async with session.get(url) as response:
                                response.raise_for_status()
                                raw = await response.text()
                            index = sorted(urls).index(url)
                            (self.out / f"prometheus-{i:06d}-{index}.txt").write_text(raw, encoding="utf-8")
                            for m in maps:
                                if m.get("url") != url:
                                    continue
                                values = parse_prometheus(raw, m["metric"], m.get("labels"))
                                if not values:
                                    continue
                                reducer = m.get("reducer", "sum")
                                value = sum(values) if reducer == "sum" else max(values) if reducer == "max" else sum(values)/len(values)
                                snapshot["values"][m["name"]] = value * m.get("scale", 1)
                        except Exception as exc:
                            self.errors.append({"url": url, "elapsed_s": at, "error": str(exc)})
                    for job in spec.get("commands", []):
                        proc = None
                        try:
                            proc = await asyncio.create_subprocess_exec(*job["argv"], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                            stdout, stderr = await asyncio.wait_for(proc.communicate(), job.get("timeout_s", 5))
                            (self.out / f'command-{job["name"]}-{i:06d}.txt').write_bytes(stdout + b"\nSTDERR\n" + stderr)
                            if proc.returncode:
                                raise ValueError(f"collector exit {proc.returncode}")
                            if job.get("format") == "json":
                                values = json.loads(stdout)
                                for k, v in values.items():
                                    if isinstance(v, (int,float)) and not isinstance(v,bool) and math.isfinite(v):
                                        snapshot["values"][k] = v
                        except Exception as exc:
                            if proc is not None and proc.returncode is None:
                                proc.kill()
                                await proc.wait()
                            self.errors.append({"command": job["name"], "elapsed_s": at, "error": str(exc)})
                    self.samples.append(snapshot)
                    stream.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
                    stream.flush()
                    i += 1
                    try:
                        await asyncio.wait_for(self.stop.wait(), interval)
                    except asyncio.TimeoutError:
                        pass

    def summarize(self) -> dict:
        measured = [s for s in self.samples if s["elapsed_s"] < self.config["run"]["duration_s"]]
        names = set().union(*(s["values"].keys() for s in measured)) if measured else set()
        stats = {}
        for name in names:
            values = [s["values"][name] for s in measured if name in s["values"]]
            stats[name] = {"samples": len(values), "mean": sum(values)/len(values), "p95": quantile(values,.95),
                           "min": min(values), "max": max(values)}
        required = self.config.get("telemetry", {}).get("required_metrics", [])
        result = {"metrics": stats, "missing": [m for m in required if m not in stats], "errors": self.errors,
                  "scope": "summary samples exclude drain; raw snapshots include drain; polling origin slightly precedes replay origin"}
        dump(self.out / "telemetry_summary.json", result)
        return result


def derive_axes(telemetry: dict, calibration: dict, qps: float) -> dict:
    """Only named demand/ready measurements produce surface coordinates."""
    stats = telemetry.get("metrics", {})
    def get(name, stat="mean"):
        return stats.get(name, {}).get(stat)
    live, budget = get("live_kv_demand_bytes", "p95"), get("kv_budget_bytes")
    recompute, fetch = get("recompute_state_ms"), get("fetch_state_ready_ms")
    reference = calibration.get("reference_qps")
    return {"P_HBM": live / budget if live is not None and budget and budget > 0 else None,
            "E_reuse": recompute / fetch if recompute is not None and fetch and fetch > 0 else None,
            "L": qps / reference if reference and calibration.get("evidence_class") == "HARDWARE_MEASURED" else None}
