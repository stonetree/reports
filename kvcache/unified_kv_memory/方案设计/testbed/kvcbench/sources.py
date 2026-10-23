"""Read source trees without importing accelerator libraries or modifying code."""
from __future__ import annotations

import ast
from pathlib import Path
import subprocess

from .common import file_hash


METHODS = {"get_num_new_matched_tokens", "start_load_kv", "wait_for_layer_load", "get_finished", "save_kv_layer", "wait_for_save"}


def inventory(paths: dict) -> dict:
    result = {}
    for name, raw in paths.items():
        if name not in ("vllm", "vllm_ascend", "mooncake"):
            continue
        if not raw:
            continue
        root = Path(raw)
        item = {"path": str(root), "exists": root.is_dir(), "git_commit": None, "connectors": []}
        if root.is_dir():
            for key, argv in (("git_commit", ["git", "-C", str(root), "rev-parse", "HEAD"]),
                              ("git_status", ["git", "-C", str(root), "status", "--porcelain"])):
                try:
                    proc = subprocess.run(argv, text=True, capture_output=True, timeout=15)
                    item[key] = proc.stdout.strip() if proc.returncode == 0 else None
                    if proc.returncode:
                        item[key + "_error"] = proc.stderr.strip()[:512]
                except (OSError, subprocess.TimeoutExpired) as exc:
                    item[key + "_error"] = str(exc)
            if name in ("vllm_ascend", "vllm"):
                subtree = root / ("vllm_ascend/distributed/kv_transfer" if name == "vllm_ascend" else "vllm/distributed/kv_transfer")
                for file in sorted(subtree.rglob("*.py")):
                    if "connector" not in file.name:
                        continue
                    try:
                        tree = ast.parse(file.read_text(encoding="utf-8"))
                    except (UnicodeError, SyntaxError):
                        continue
                    for cls in (x for x in tree.body if isinstance(x, ast.ClassDef)):
                        methods = [x.name for x in cls.body if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name in METHODS]
                        if len(methods) >= 2:
                            item["connectors"].append({"file": str(file), "module": ".".join(file.relative_to(root).with_suffix("").parts),
                                                       "class": cls.name, "methods": methods, "sha256": file_hash(file)})
        result[name] = item
    return result
