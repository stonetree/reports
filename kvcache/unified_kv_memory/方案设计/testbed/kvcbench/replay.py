"""Bounded open-loop HTTP streaming replay, with a fixed arrival cohort."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import aiohttp


async def completion(session: aiohttp.ClientSession, spec: dict, request: dict,
                     origin: float, phase: str = "measure", partial: dict | None = None) -> dict:
    now = lambda: time.perf_counter() - origin
    row = partial if partial is not None else {}
    row.update({k: request.get(k) for k in ("request_id", "session_id", "tenant_id", "arrival_s", "turn",
           "input_tokens", "reusable_tokens", "token_count_kind", "source", "provenance")})
    row.update(phase=phase, scheduled_s=request["arrival_s"], dispatch_s=now(),
               first_token_s=None, token_times_s=[], chunk_token_counts=[], completion_tokens=None,
               status="error", usage={}, finish_reason=None, emission_kind="sse_delivery")
    headers = {"X-Request-Id": request["request_id"]}
    if spec.get("api_key_env"):
        key = os.environ.get(spec["api_key_env"], "")
        if key:
            headers["Authorization"] = "Bearer " + key
    body = {"model": spec["model"], "prompt": request["prompt"], "max_tokens": request["max_tokens"],
            "temperature": 0, "stream": True, "stream_options": {"include_usage": True}}
    body.update(spec.get("request_options", {}))
    if "cache_salt" in request:
        body["cache_salt"] = request["cache_salt"]
    token_ids_count = 0
    saw_done = False
    try:
        async with session.post(spec["base_url"].rstrip("/") + "/v1/completions", json=body, headers=headers) as response:
            row["http_status"] = response.status
            if response.status != 200:
                row["error"] = (await response.text())[:2048]
                return row
            # StreamReader iteration splits at newline, independently of TCP frame boundaries.
            data_lines = []
            async def consume(lines):
                nonlocal token_ids_count, saw_done
                payload = "\n".join(lines)
                if payload == "[DONE]":
                    saw_done = True
                    return
                obj = json.loads(payload)
                if obj.get("error"):
                    raise ValueError(f"stream error: {obj['error']}")
                usage = obj.get("usage")
                if isinstance(usage, dict):
                    row["usage"] = usage
                    count = usage.get("completion_tokens")
                    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                        row["completion_tokens"] = count
                        row["token_count_source"] = "server_usage"
                for choice in obj.get("choices", []):
                    ids = choice.get("token_ids") or []
                    logs = choice.get("logprobs") or {}
                    counted = len(ids) if ids else len(logs["tokens"]) if isinstance(logs,dict) and isinstance(logs.get("tokens"),list) else None
                    text = choice.get("text") or ""
                    if text or ids:
                        t = now()
                        if row["first_token_s"] is None:
                            row["first_token_s"] = t
                        row["token_times_s"].append(t)
                        row["chunk_token_counts"].append(counted)
                        token_ids_count += len(ids)
                    if choice.get("finish_reason"):
                        row["finish_reason"] = choice["finish_reason"]
            async for raw in response.content:
                line = raw.decode("utf-8").rstrip("\r\n")
                if not line:
                    if data_lines:
                        await consume(data_lines)
                        data_lines = []
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip(" "))
            if data_lines:
                await consume(data_lines)
            if row["completion_tokens"] is None and token_ids_count:
                row["completion_tokens"] = token_ids_count
                row["token_count_source"] = "server_token_ids"
            row["stream_done"] = saw_done
            counts=row["chunk_token_counts"]
            row["timing_quality"] = "one_token_per_chunk" if counts and all(x==1 for x in counts) and len(counts)==row["completion_tokens"] else "batched_or_unknown"
            # HTTP 200 alone does not prove a complete generation.
            if row["finish_reason"] and saw_done and row["first_token_s"] is not None:
                row["status"] = "ok"
            else:
                row["error"] = "incomplete_or_empty_stream"
    except asyncio.CancelledError:
        row["status"] = "cancelled"
        row["error"] = "phase drain deadline"
        raise
    except (asyncio.TimeoutError, TimeoutError):
        row["status"] = "timeout"
        row["error"] = "request deadline"
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        row["finish_s"] = now()
    return row


async def replay(requests: list[dict], spec: dict, run: dict, out: Path,
                 phase: str = "measure") -> list[dict]:
    """Never wait for capacity in the arrival producer or silently reduce offered load."""
    duration = float(run["duration_s"])
    cap = int(run["max_inflight"])
    pending_cap = int(run["max_pending"])
    timeout = float(run["request_timeout_s"])
    requests = sorted((r for r in requests if 0 <= r["arrival_s"] < duration), key=lambda r: (r["arrival_s"], r["request_id"]))
    origin = time.perf_counter()
    gate = asyncio.Semaphore(cap)
    locks = {}
    rows, active = [], set()
    out.parent.mkdir(parents=True, exist_ok=True)
    stream = out.open("w", encoding="utf-8")
    def record(row):
        rows.append(row)
        stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
    def terminal(req, status, reason):
        return {**{k: v for k, v in req.items() if k != "prompt"}, "status": status, "error": reason,
                "phase": phase, "scheduled_s": req["arrival_s"], "dispatch_s": None,
                "first_token_s": None, "token_times_s": [], "completion_tokens": None,
                "finish_s": time.perf_counter() - origin}
    connector = aiohttp.TCPConnector(limit=cap, limit_per_host=cap)
    client_timeout = aiohttp.ClientTimeout(total=None, sock_connect=min(timeout, 30), sock_read=timeout)
    async with aiohttp.ClientSession(timeout=client_timeout, connector=connector, read_bufsize=1024*1024) as session:
        async def task(req):
            partial = {}
            try:
                async def execute():
                    # A session cannot consume its next turn before the preceding request has completed.
                    async with locks.setdefault(req["session_id"], asyncio.Lock()):
                        async with gate:
                            return await completion(session, spec, req, origin, phase, partial)
                budget = req["arrival_s"] + timeout - (time.perf_counter() - origin)
                if budget <= 0:
                    record(terminal(req, "timeout", "deadline before dispatch"))
                else:
                    record(await asyncio.wait_for(execute(), budget))
            except asyncio.TimeoutError:
                record({**terminal(req, "timeout", "arrival-to-completion deadline including pending queue"),
                        **partial, "status":"timeout", "error":"arrival-to-completion deadline including pending queue",
                        "finish_s":time.perf_counter()-origin})
            except asyncio.CancelledError:
                record({**terminal(req, "cancelled", "drain deadline"), **partial,"status":"cancelled",
                        "error":"drain deadline", "finish_s":time.perf_counter()-origin})
                raise
        try:
            for req in requests:
                delay = origin + req["arrival_s"] - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
                if len(active) >= cap + pending_cap:
                    record(terminal(req, "dropped", "client bounded queue full"))
                else:
                    handle = asyncio.create_task(task(req))
                    active.add(handle)
                    handle.add_done_callback(active.discard)
            # Keep the measurement window open even if this cohort is sparse.
            await asyncio.sleep(max(0, origin + duration - time.perf_counter()))
            if active:
                _, left = await asyncio.wait(list(active), timeout=float(run["drain_s"]))
                for handle in left:
                    handle.cancel()
                if left:
                    await asyncio.gather(*left, return_exceptions=True)
        finally:
            for handle in list(active):
                handle.cancel()
            if active:
                await asyncio.gather(*list(active), return_exceptions=True)
            stream.close()
    return sorted(rows, key=lambda r: (r["arrival_s"], r["request_id"]))
