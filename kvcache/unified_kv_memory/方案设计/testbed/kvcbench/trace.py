"""Trace corpus adaptation for KVCBench.

The bundled SWE datasets are task corpora, not production request traces.  This
module intentionally converts them into a labelled synthetic request schedule
and never evaluates code from the corpus.
"""
from __future__ import annotations

import glob
import hashlib
import json
import math
import random
from functools import lru_cache
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _salt(seed: int, scope: str) -> str:
    return hashlib.sha256(f"kvcbench:{seed}:{scope}".encode()).hexdigest()[:24]


def _approx_tokens(text: str) -> int:
    # Conservative, repeatable estimate for an unconfigured tokenizer.
    return max(1, (len(text) + 3) // 4)


def _load_parquet(root: Path, dataset: str) -> list[tuple[str, str, Path]]:
    files = sorted(root.glob(dataset + "/data/*.parquet"))
    if not files:
        return []
    try:
        import pyarrow.parquet as pq  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Reading parquet trace corpora requires pyarrow; use instruction.md fallback or install pyarrow.") from exc
    rows: list[tuple[str, str, Path]] = []
    for file in files:
        table = pq.read_table(file)
        for item in table.to_pylist():
            prompt = item.get("problem_statement") or item.get("instruction")
            identifier = item.get("instance_id") or item.get("task_id") or item.get("id")
            if isinstance(prompt, str) and prompt.strip():
                rows.append((str(identifier or f"{dataset}-{len(rows)}"), prompt.strip(), file))
    return rows


def _load_instruction_files(root: Path, dataset: str) -> list[tuple[str, str, Path]]:
    rows: list[tuple[str, str, Path]] = []
    pattern = str(root / dataset / "tasks" / "*" / "instruction.md")
    for name in sorted(glob.glob(pattern)):
        path = Path(name)
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            rows.append((path.parent.name, text, path))
    return rows


def _load_marathon(root: Path) -> list[tuple[str, str, Path]]:
    path = root / "swe-marathon" / "tasks.jsonl"
    if not path.is_file():
        return []
    rows: list[tuple[str, str, Path]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed JSON in {path}:{line_no}") from exc
        prompt = record.get("instruction") or record.get("description")
        if isinstance(prompt, str) and prompt.strip():
            rows.append((str(record.get("task_id") or record.get("name") or line_no), prompt.strip(), path))
    return rows


def _load_normalized(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed normalized JSON in {path}:{line_no}") from exc
        request_id = row.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError(f"normalized trace {path}:{line_no} has no request_id")
        if request_id in seen:
            raise ValueError(f"Duplicate request_id {request_id!r} in normalized trace")
        seen.add(request_id)
        arrival = row.get("arrival_s", 0)
        if isinstance(arrival, bool) or not isinstance(arrival, (int, float)) or not math.isfinite(arrival) or arrival < 0:
            raise ValueError(f"normalized trace {path}:{line_no} has invalid arrival_s")
        prompt = row.get("prompt", row.get("token_ids"))
        if not isinstance(prompt, (str, list)):
            raise ValueError(f"normalized trace {path}:{line_no} has neither prompt nor token_ids")
        if isinstance(prompt, list) and any(isinstance(token, bool) or not isinstance(token, int) or token < 0 for token in prompt):
            raise ValueError(f"normalized trace {path}:{line_no} has invalid prompt token IDs")
        if "prompt" not in row:
            row["prompt"] = prompt
            row.pop("token_ids", None)
        max_tokens = row.get("max_tokens")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError(f"normalized trace {path}:{line_no} has invalid max_tokens")
        rows.append(row)
    return rows


def _corpus_rows(root: Path, dataset: str) -> list[tuple[str, str, Path]]:
    if dataset == "swe-marathon":
        return _load_marathon(root)
    try:
        rows = _load_parquet(root, dataset)
    except RuntimeError as exc:
        # The distributed task directories carry readable instruction.md files.
        # Prefer those over failing a testbed deployment that deliberately has no
        # optional parquet dependency.
        if "requires pyarrow" not in str(exc):
            raise
        rows = []
    return rows or _load_instruction_files(root, dataset)


@lru_cache(maxsize=2)
def _get_tokenizer(tokenizer_path: str):
    try:
        from transformers import AutoTokenizer  # type: ignore
        return AutoTokenizer.from_pretrained(tokenizer_path, local_files_only=True)
    except Exception as exc:
        raise RuntimeError(f"Could not load tokenizer from {tokenizer_path!r}") from exc


def _count_tokens(text: str, tokenizer_path: str) -> tuple[int, str]:
    if not tokenizer_path:
        return _approx_tokens(text), "approx_chars_div_4"
    return len(_get_tokenizer(tokenizer_path).encode(text, add_special_tokens=False)), "tokenizer"


def _synthetic_padding(requested_tokens: int, tokenizer_path: str) -> tuple[str, int, str]:
    """Return labelled padding and its observed token count.

    Without a tokenizer this is explicitly an estimate.  We avoid trimming a
    tokenizer's output back into text, which could mutate or hide source text.
    """
    if requested_tokens <= 0:
        return "", 0, "none"
    padding = "[KVCBENCH SYNTHETIC CONTEXT] " + ("x " * requested_tokens)
    count, kind = _count_tokens(padding, tokenizer_path)
    return padding, count, kind


def prepare(config: dict[str, Any], out_dir: Path) -> list[dict[str, Any]]:
    """Prepare a deterministic workload and write its data/provenance manifest.

    The caller passes either the whole parsed configuration or its ``trace``
    mapping.  All returned records are safe JSON values for the HTTP runner.
    """
    spec = config.get("trace", config)
    root = Path(spec.get("root", "trace")).expanduser()
    datasets = spec.get("datasets", ["SWE-bench_Verified"])
    if isinstance(datasets, str):
        datasets = [datasets]
    seed = int(spec.get("seed", 1))
    turns = max(1, int(spec.get("turns", 1)))
    max_tasks = max(1, int(spec.get("max_tasks", 10)))
    replicas = max(1, int(spec.get("replicas", 1)))
    tenants = max(1, int(spec.get("tenants", 1)))
    gap_s = float(spec.get("gap_s", 0.0))
    interval_s = float(spec.get("arrival_interval_s", 0.0))
    if not math.isfinite(gap_s) or not math.isfinite(interval_s) or gap_s < 0 or interval_s < 0:
        raise ValueError("gap_s and arrival_interval_s must be finite and non-negative")
    salt_mode = str(spec.get("salt_mode", "session"))
    if salt_mode not in {"session", "request", "shared", "tenant", "none"}:
        raise ValueError("salt_mode must be session/request/shared/tenant/none")
    salt_transport = str(spec.get("salt_transport", "prefix"))
    if salt_transport not in {"prefix", "cache_salt"}:
        raise ValueError("salt_transport must be prefix/cache_salt")
    output_tokens = int(spec.get("output_tokens", 128))
    append_tokens = int(spec.get("append_tokens", 0))
    context_tokens = int(spec.get("context_tokens", 0))
    if output_tokens <= 0 or append_tokens < 0 or context_tokens < 0:
        raise ValueError("output_tokens must be positive; append_tokens and context_tokens must be non-negative")
    tokenizer_path = str(spec.get("tokenizer_path", ""))
    out_dir.mkdir(parents=True, exist_ok=True)

    normalized = spec.get("normalized_path")
    if normalized:
        path = Path(str(normalized)).expanduser()
        requests = _load_normalized(path)
        source_files = [path]
        # Preserve normalized records: only add provenance fields that are absent.
        for row in requests:
            row.setdefault("source", "normalized")
            row.setdefault("provenance", "user_supplied_normalized_workload")
        requests.sort(key=lambda row: (row.get("arrival_s", 0), row["request_id"]))
    else:
        corpus: list[tuple[str, str, Path, str]] = []
        source_counts: dict[str, int] = {}
        for dataset in datasets:
            loaded = _corpus_rows(root, str(dataset))
            source_counts[str(dataset)] = len(loaded)
            if not loaded:
                raise FileNotFoundError(f"Selected dataset {dataset!r} has no readable prompts. Install pyarrow for parquet-only datasets or provide normalized_path.")
            corpus.extend((identity, prompt, path, str(dataset)) for identity, prompt, path in loaded)
        random.Random(seed).shuffle(corpus)
        corpus = corpus[:max_tasks]
        source_files = sorted({item[2] for item in corpus})
        requests = []
        synthetic_prefix, padding_observed_tokens, padding_token_kind = _synthetic_padding(context_tokens, tokenizer_path)
        request_index = 0
        for task_id, base_prompt, _path, dataset in corpus:
            for replica in range(replicas):
                session_id = f"{dataset}:{task_id}:r{replica}"
                tenant_id = f"tenant-{replica % tenants}"
                session_salt = _salt(seed, session_id)
                history = synthetic_prefix + base_prompt
                previous_prompt: str | None = None
                for turn in range(turns):
                    request_id = f"{session_id}:t{turn}"
                    if salt_mode == "none":
                        salt = None
                    elif salt_mode == "shared":
                        salt = _salt(seed, "shared")
                    elif salt_mode == "tenant":
                        salt = _salt(seed, tenant_id)
                    elif salt_mode == "request":
                        salt = _salt(seed, request_id)
                    else:
                        salt = session_salt
                    prompt = history
                    if salt and salt_transport == "prefix":
                        prompt = f"[KVCBENCH-CACHE-SALT:{salt}]\n" + prompt
                    input_tokens, token_kind = _count_tokens(prompt, tokenizer_path)
                    # The previous serialized prompt is the maximal reusable
                    # prefix.  For prefix transport it includes the salt; for
                    # request salt no two turns intentionally share a key.
                    reusable = 0 if previous_prompt is None or salt_mode == "request" else _count_tokens(previous_prompt, tokenizer_path)[0]
                    requests.append({
                        "request_id": request_id, "session_id": session_id, "tenant_id": tenant_id,
                        "turn": turn, "arrival_s": request_index * interval_s + turn * gap_s,
                        "prompt": prompt, "max_tokens": output_tokens,
                        "input_tokens": input_tokens, "reusable_tokens": reusable,
                        "token_count_kind": token_kind, "source": dataset,
                        "provenance": "synthetic_multiturn_schedule_from_task_corpus",
                    })
                    if salt and salt_transport == "cache_salt":
                        requests[-1]["cache_salt"] = salt
                    previous_prompt = prompt
                    history += "\n[assistant turn omitted for deterministic replay] " + ("y " * append_tokens)
                request_index += 1

    for row in requests:
        arrival = row.get("arrival_s", 0)
        if isinstance(arrival, bool) or not isinstance(arrival, (int, float)) or not math.isfinite(arrival) or arrival < 0:
            raise ValueError(f"negative arrival_s for {row.get('request_id')}")
    ids = [row["request_id"] for row in requests]
    if len(ids) != len(set(ids)):
        raise ValueError("Generated workload has duplicate request_id values")
    with (out_dir / "workload.jsonl").open("w", encoding="utf-8") as handle:
        for request in requests:
            handle.write(json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n")
    manifest = {
        "schema_version": 1, "request_count": len(requests), "seed": seed,
        "classification": "user_supplied_normalized_workload" if normalized else "synthetic_schedule_from_task_corpus",
        "timing_provenance": str(spec.get("timing_provenance", "unknown")) if normalized else "synthetic_arrival_schedule",
        "source_files": [{"path": str(p), "sha256": _sha256(p)} for p in source_files],
        "salt_mode": salt_mode, "salt_transport": salt_transport,
        "loaded_prompt_count_by_dataset": {} if normalized else source_counts,
        "context_padding_requested_tokens": context_tokens if not normalized else 0,
        "context_padding_observed_tokens": padding_observed_tokens if not normalized else 0,
        "context_padding_token_count_kind": padding_token_kind if not normalized else "none",
        "warnings": (["cache_salt is passed as a client field; connector support and Mooncake external-key isolation must be verified on the target build."] if salt_transport == "cache_salt" else []) +
                    (["request salt intentionally prevents cross-turn prefix reuse."] if salt_mode == "request" else []) +
                    (["context_tokens requests labelled synthetic padding; observed count is recorded and source task text is retained verbatim."] if context_tokens else []),
    }
    (out_dir / "trace_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return requests
