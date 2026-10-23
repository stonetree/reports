from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import time

from .common import dump


def expand_argv(argv: list[str], values: dict) -> list[str]:
    # Token replacement within argv elements; never evaluate shell syntax.
    for key, value in values.items():
        argv = [x.replace("{" + key + "}", str(value)) for x in argv]
    return [os.path.expandvars(x) for x in argv]


async def command(argv: list[str], values: dict, out: Path, timeout=120) -> dict:
    argv = expand_argv(argv, values)
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    result = {"argv": argv, "returncode": proc.returncode, "stdout": stdout.decode(errors="replace"),
              "stderr": stderr.decode(errors="replace"), "wall_time_ns": time.time_ns()}
    dump(out, result)
    if proc.returncode:
        raise RuntimeError(f"Control command failed: {argv[0]}; see {out}")
    return result


class Controls:
    def __init__(self, config: dict, profile: dict, case_id: str, out: Path, values: dict):
        self.config, self.profile, self.case_id, self.out, self.values = config, profile, case_id, out, values
        self.applied = False

    def write_hooks(self, enabled):
        hooks = self.config.get("hooks", {})
        if not hooks.get("enabled"):
            if enabled and self.profile.get("rules"):
                raise ValueError("Server hook rules requested but hooks.enabled=false")
            return
        dump(Path(hooks["control_file"]), {"enabled": enabled, "case_id": self.case_id,
             "targets": hooks["targets"], "rules": self.profile.get("rules", []) if enabled else []})

    async def __aenter__(self):
        self.out.mkdir(parents=True, exist_ok=True)
        self.applied = True  # reset even if apply partially fails
        try:
            self.write_hooks(True)
            if self.profile.get("apply_argv"):
                await command(self.profile["apply_argv"], self.values, self.out / "control_apply.json")
                await command(self.profile["verify_argv"], self.values, self.out / "control_verify.json")
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *_):
        try:
            if self.applied and self.profile.get("reset_argv"):
                await command(self.profile["reset_argv"], self.values, self.out / "control_reset.json")
        finally:
            self.write_hooks(False)


def hook_evidence(config: dict, case_id: str, out: Path) -> dict:
    hooks = config.get("hooks", {})
    matched, installed = [], []
    directory = Path(hooks.get("event_dir", out / "unused"))
    if hooks.get("enabled") and directory.is_dir():
        for path in sorted(directory.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("case_id") == case_id:
                    matched.append(event)
                if event.get("event") == "installed":
                    installed.append(event)
    with (out / "hook_events.jsonl").open("w", encoding="utf-8") as stream:
        for event in matched:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    result = {"case_id": case_id, "calls": len([x for x in matched if x.get("event") == "call"]),
              "injected_calls": sum(x.get("injected_delay_ns",0) > 0 or x.get("action") == "miss" for x in matched),
              "installed": installed, "warning": "Connector hook duration is not accelerator state-stall duration; batched calls may not have a request ID."}
    dump(out / "hook_summary.json", result)
    return result
