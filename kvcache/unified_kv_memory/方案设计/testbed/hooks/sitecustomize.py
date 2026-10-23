"""Put this directory first in PYTHONPATH when starting vLLM-Ascend."""
try:
    from kvcbench.hooks_runtime import install_from_env
    install_from_env()
except Exception as exc:  # Python suppresses sitecustomize exceptions; make failure durable.
    import json, os, pathlib, time
    target = os.getenv("KVCBENCH_EVENT_DIR")
    if target:
        path = pathlib.Path(target); path.mkdir(parents=True, exist_ok=True)
        (path / f"startup-error-{os.getpid()}.json").write_text(json.dumps({"event":"startup_error", "error":repr(exc), "wall_time_ns":time.time_ns()}), encoding="utf-8")
    raise
