from __future__ import annotations

import math
import os
from pathlib import Path
import tomllib


def load(path: str | Path) -> dict:
    path = Path(path).resolve()
    with path.open("rb") as stream:
        c = tomllib.load(stream)
    c["_config_path"] = str(path)
    for section, keys in {"trace": ["root", "normalized_path", "tokenizer_path"],
                          "run": ["output_root"], "sources": ["vllm", "vllm_ascend", "mooncake"],
                          "hooks": ["control_file", "event_dir"],
                          "calibration": ["evidence_path"]}.items():
        for key in keys:
            value = c.get(section, {}).get(key)
            if value:
                value = Path(os.path.expandvars(os.path.expanduser(value)))
                c[section][key] = str((path.parent / value).resolve() if not value.is_absolute() else value)
    validate(c)
    return c


def positive(value, name, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (value == 0 and not zero):
        raise ValueError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}")


def validate(c: dict) -> None:
    for s in ("run", "trace", "experiment", "sweep", "variants", "slo"):
        if s not in c:
            raise ValueError(f"Missing [{s}]")
    r, e = c["run"], c["experiment"]
    for k in ("duration_s", "request_timeout_s", "max_inflight", "max_pending", "drain_s"):
        positive(r[k], "run." + k)
    for k in ("warmup_requests", "cooldown_s"):
        positive(r.get(k, 0), "run." + k, True)
    positive(e["repeats"], "experiment.repeats")
    positive(e.get("reference_qps", 0), "experiment.reference_qps", True)
    for k in ("ttft_ms", "tpot_ms", "e2e_ms"):
        positive(c["slo"][k], "slo." + k)
    for key in ("context_tokens", "load", "gap_s", "control_profiles"):
        if not c["sweep"].get(key):
            raise ValueError(f"sweep.{key} cannot be empty")
    for x in c["sweep"]["load"]:
        positive(x, "sweep.load")
    for k in ("context_tokens", "gap_s"):
        for x in c["sweep"][k]:
            positive(x, "sweep." + k, True)
    for v in e["variants"]:
        spec = c["variants"][v]
        if v not in ("B0", "B1", "B2", "B3"):
            raise ValueError(f"Unknown baseline {v}")
        if not spec.get("base_url", "").startswith(("http://", "https://")):
            raise ValueError(f"variants.{v}.base_url must be HTTP(S)")
        positive(spec["accelerator_count"], "accelerator_count")
        if not spec.get("model") or not spec.get("description"):
            raise ValueError(f"{v}: model and baseline description required")
        if spec.get("cache_policy", "namespace") not in ("namespace", "reset"):
            raise ValueError("cache_policy must be namespace or reset")
        for k in ("prompt","stream","stream_options","model","max_tokens","n","echo"):
            if k in spec.get("request_options",{}):
                raise ValueError(f"request_options cannot override protocol field {k}")
    if len({c["variants"][v]["base_url"] for v in e["variants"]}) < len(e["variants"]):
        if not all(c["variants"][v].get("prepare_argv") for v in e["variants"]):
            raise ValueError("Variants sharing an endpoint require explicit per-variant prepare_argv")
    for name in c["sweep"]["control_profiles"]:
        if name not in c.get("controls", {}):
            raise ValueError(f"Unknown control profile {name}")
    for profile in c.get("controls", {}).values():
        for action in ("apply_argv", "verify_argv", "reset_argv"):
            if action in profile and (not isinstance(profile[action], list) or not all(isinstance(x, str) for x in profile[action])):
                raise ValueError(f"{action} must be an argv string array, never shell code")
        if profile.get("apply_argv") and (not profile.get("reset_argv") or not profile.get("verify_argv")):
            raise ValueError("External control needs apply/verify/reset commands")
        for hook in profile.get("rules", []):
            positive(hook.get("delay_ms", 0), "hook delay_ms", True)
            rate = hook.get("probability", 1.0)
            if not 0 <= rate <= 1:
                raise ValueError("hook probability outside [0,1]")
            if hook.get("action", "delay") not in ("delay", "observe", "miss"):
                raise ValueError("Only delay, observe and lookup miss are supported")
            if hook.get("action") == "miss" and hook.get("method") != "get_num_new_matched_tokens":
                raise ValueError("Safe miss adapter applies only to get_num_new_matched_tokens")
