"""Runtime-only observability hooks for KV connector experiments.

The module deliberately imports neither vLLM nor Ascend at import time.  It is
loaded through ``sitecustomize`` before the serving process imports them.
"""
from __future__ import annotations

import asyncio
import ast
import functools
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import random
import sys
import threading
import time
from typing import Any, Callable

_LOCK = threading.Lock()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class RuntimeHooks:
    def __init__(self, config_path: Path, event_dir: Path):
        self.config_path = config_path
        self.event_dir = event_dir
        self._mtime_ns = (-1, -1, -1, -1)
        self._config: dict[str, Any] = {}
        self._targets: dict[tuple[str, str], set[str]] = {}
        self._rng = random.Random(0)
        self.reload(force=True)

    def reload(self, force: bool = False) -> None:
        stat = self.config_path.stat()
        stamp = (stat.st_mtime_ns, stat.st_ctime_ns, getattr(stat, "st_ino", -1), stat.st_size)
        if not force and stamp == self._mtime_ns:
            return
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("KVCBENCH_HOOK_CONFIG must contain an object")
        targets = payload.get("targets") or []
        # A target list is fixed at startup. Rules may be replaced atomically.
        if force:
            for target in targets:
                key = (str(target["module"]), str(target["class"]))
                self._targets[key] = set(map(str, target.get("methods", [])))
            for rule in payload.get("rules", []):
                key = (str(rule["module"]), str(rule["class"]))
                self._targets.setdefault(key, set()).add(str(rule["method"]))
        self._config = payload
        self._mtime_ns = stamp

    def targets_for(self, module: str) -> dict[str, set[str]]:
        return {cls: methods for (mod, cls), methods in self._targets.items() if mod == module}

    def _rule(self, module: str, cls: str, method: str) -> dict[str, Any]:
        self.reload()
        if not self._config.get("enabled", False):
            return {"action": "observe", "delay_ms": 0.0, "probability": 0.0}
        matches = [x for x in self._config.get("rules", []) if x.get("module") == module and x.get("class") == cls and x.get("method") == method]
        rule = dict(matches[-1]) if matches else {"action": "observe"}
        action = rule.get("action", "observe")
        if action not in {"observe", "delay", "miss"}:
            raise ValueError(f"unsupported hook action: {action}")
        if action == "miss" and method != "get_num_new_matched_tokens":
            raise ValueError("miss is permitted only for get_num_new_matched_tokens")
        rule["probability"] = float(rule.get("probability", 1.0))
        rule["delay_ms"] = float(rule.get("delay_ms", 0.0))
        if not 0 <= rule["probability"] <= 1 or rule["delay_ms"] < 0:
            raise ValueError("invalid hook probability or delay")
        return rule

    def _event(self, payload: dict[str, Any]) -> None:
        """Best-effort evidence must never replace the connector's exception."""
        try:
            self.event_dir.mkdir(parents=True, exist_ok=True)
            file = self.event_dir / f"events-{os.getpid()}.jsonl"
            with _LOCK, file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception:
            # A caller's data-path error has priority. A later doctor can detect
            # missing event files instead of misclassifying this as a hit.
            pass

    @staticmethod
    def _request_id(args: tuple, kwargs: dict) -> str | None:
        candidate = kwargs.get("request") or kwargs.get("req")
        if candidate is None:
            for item in args:
                if hasattr(item, "request_id") or hasattr(item, "req_id"):
                    candidate = item
                    break
        return getattr(candidate, "request_id", None) or getattr(candidate, "req_id", None)

    def _log(self, module: str, cls: str, method: str, start: int, injected_ns: int, requested_ns: int, action: str, applied: bool, case_id: Any, args: tuple, kwargs: dict, result: Any = None, error: BaseException | None = None) -> None:
        payload = {"wall_time_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns(), "pid": os.getpid(),
                   "thread_id": threading.get_ident(), "event": "call",
                   "module": module, "class": cls, "method": method,
                   "duration_ns": time.perf_counter_ns() - start, "injected_delay_ns": injected_ns,
                   "action": action, "applied": applied, "case_id": case_id, "request_id": self._request_id(args, kwargs),
                   "injected_delay_ns": injected_ns, "requested_delay_ns": requested_ns}
        if method == "get_num_new_matched_tokens" and isinstance(result, tuple) and len(result) >= 2 and isinstance(result[0], int) and isinstance(result[1], bool):
            payload["matched_tokens"] = result[0]
            payload["lookup_async"] = result[1]
        if error is not None:
            payload["raised"] = type(error).__name__
            payload["error"] = str(error)[:512]
        self._event(payload)

    def wrap(self, module: str, clsname: str, method: str, original: Callable) -> Callable:
        if getattr(original, "__kvcbench_wrapped__", False):
            return original
        if asyncio.iscoroutinefunction(original):
            @functools.wraps(original)
            async def async_wrapper(*args, **kwargs):
                started = time.perf_counter_ns(); injected = requested = 0; action = "observe"; applied = False; case_id = None; result = None; error = None
                try:
                    rule = self._rule(module, clsname, method); action = rule["action"]; case_id = self._config.get("case_id")
                    active = self._rng.random() < rule["probability"]; applied = active and action != "observe"
                    if active and action == "miss":
                        result = (0, False); return result
                    if active and action == "delay":
                        requested = int(rule["delay_ms"] * 1_000_000); before = time.perf_counter_ns(); await asyncio.sleep(requested / 1e9); injected = time.perf_counter_ns() - before
                    result = await original(*args, **kwargs); return result
                except BaseException as exc:
                    error = exc; raise
                finally:
                    self._log(module, clsname, method, started, injected, requested, action, applied, case_id, args, kwargs, result, error)
            async_wrapper.__kvcbench_wrapped__ = True
            return async_wrapper
        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            started = time.perf_counter_ns(); injected = requested = 0; action = "observe"; applied = False; case_id = None; result = None; error = None
            try:
                rule = self._rule(module, clsname, method); action = rule["action"]; case_id = self._config.get("case_id")
                active = self._rng.random() < rule["probability"]; applied = active and action != "observe"
                if active and action == "miss":
                    result = (0, False); return result
                if active and action == "delay":
                    requested = int(rule["delay_ms"] * 1_000_000); before = time.perf_counter_ns(); time.sleep(requested / 1e9); injected = time.perf_counter_ns() - before
                result = original(*args, **kwargs); return result
            except BaseException as exc:
                error = exc; raise
            finally:
                self._log(module, clsname, method, started, injected, requested, action, applied, case_id, args, kwargs, result, error)
        wrapper.__kvcbench_wrapped__ = True
        return wrapper

    def install_module(self, module_name: str, module: Any) -> None:
        for clsname, methods in self.targets_for(module_name).items():
            cls = getattr(module, clsname, None)
            if cls is None:
                self._event({"event": "startup_error", "pid": os.getpid(), "module": module_name, "class": clsname, "error": "class_not_found"})
                continue
            for name in methods:
                descriptor = vars(cls).get(name)
                if isinstance(descriptor, (staticmethod, classmethod)):
                    self._event({"event": "startup_error", "pid": os.getpid(), "module": module_name, "class": clsname, "method": name, "error": "unsupported_descriptor"})
                    continue
                original = getattr(cls, name, None)
                if not callable(original):
                    self._event({"event": "startup_error", "pid": os.getpid(), "module": module_name, "class": clsname, "method": name, "error": "method_not_found"})
                    continue
                setattr(cls, name, self.wrap(module_name, clsname, name, original))
                installed = {"event": "installed", "pid": os.getpid(), "module": module_name, "class": clsname, "method": name,
                             "case_id": self._config.get("case_id"), "wall_time_ns": time.time_ns()}
                _write_json(self.event_dir / f"installed-{os.getpid()}-{module_name.replace('.', '_')}-{clsname}-{name}.json", installed)
                self._event(installed)


class _Loader(importlib.abc.Loader):
    def __init__(self, wrapped: importlib.abc.Loader, hooks: RuntimeHooks, module: str):
        self.wrapped, self.hooks, self.module = wrapped, hooks, module
    def create_module(self, spec):
        return self.wrapped.create_module(spec) if hasattr(self.wrapped, "create_module") else None
    def exec_module(self, module):
        self.wrapped.exec_module(module)
        self.hooks.install_module(self.module, module)


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, hooks: RuntimeHooks): self.hooks = hooks
    def find_spec(self, fullname, path=None, target=None):
        if not self.hooks.targets_for(fullname): return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and spec.loader and not isinstance(spec.loader, _Loader): spec.loader = _Loader(spec.loader, self.hooks, fullname)
        return spec


def install_from_env() -> RuntimeHooks | None:
    raw, events = os.getenv("KVCBENCH_HOOK_CONFIG"), os.getenv("KVCBENCH_EVENT_DIR")
    if not raw and not events: return None
    if not raw or not events: raise RuntimeError("KVCBENCH_HOOK_CONFIG and KVCBENCH_EVENT_DIR must both be set")
    hooks = RuntimeHooks(Path(raw).resolve(), Path(events).resolve())
    sys.meta_path.insert(0, _Finder(hooks))
    # Handle a module imported before this sitecustomize hook, without importing any new target.
    for module in list(sys.modules.values()):
        name = getattr(module, "__name__", "")
        if hooks.targets_for(name): hooks.install_module(name, module)
    return hooks


def doctor_sources(config: dict[str, Any]) -> dict[str, Any]:
    """AST-only source compatibility check; never imports a serving stack."""
    results = []
    for target in config.get("targets", []):
        path = Path(target.get("source_path", ""))
        item = {"module": target.get("module"), "class": target.get("class"), "source_path": str(path), "ok": False, "missing": []}
        if not path.is_file(): item["error"] = "source_not_found"; results.append(item); continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        classes = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
        cls = classes.get(target.get("class"))
        if cls is None: item["error"] = "class_not_found"; results.append(item); continue
        found = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        item["missing"] = sorted(set(target.get("methods", [])) - found); item["ok"] = not item["missing"]
        results.append(item)
    return {"ok": all(x["ok"] for x in results), "targets": results}
