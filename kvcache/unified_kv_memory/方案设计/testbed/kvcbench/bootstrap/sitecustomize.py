"""Activated only through the server launcher's explicit PYTHONPATH."""
try:
    from kvcbench.hooks_runtime import install_from_env
    install_from_env()
except Exception as exc:
    import json
    import os
    from pathlib import Path
    import time
    directory = os.getenv("KVCBENCH_EVENT_DIR")
    if directory:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"startup-error-{os.getpid()}.json").write_text(
            json.dumps({"event":"startup_error","error":repr(exc),"wall_time_ns":time.time_ns()}),encoding="utf-8")
    raise
