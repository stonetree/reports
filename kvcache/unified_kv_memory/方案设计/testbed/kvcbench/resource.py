"""Linux CPU affinity adapter with process-generation checks and rollback."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from .common import dump, read


def cpus(text: str) -> set[int]:
    result=set()
    for item in text.split(","):
        if "-" in item:
            a,b=map(int,item.split("-")); result.update(range(a,b+1))
        else:
            result.add(int(item))
    if not result or min(result)<0:
        raise ValueError("Empty or invalid CPU list")
    return result


def generation(pid):
    # comm can contain spaces and parentheses; remaining fields start after the final ')'.
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")",1)[1].split()[19]


def restore(state: dict):
    errors=[]
    for entry in state["threads"]:
        try:
            if generation(entry["tid"]) != entry["generation"]:
                errors.append(f"Thread {entry['tid']} reused; not touched")
                continue
            os.sched_setaffinity(entry["tid"],set(entry["previous"]))
        except ProcessLookupError:
            pass
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError("; ".join(errors))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["apply","verify","reset"])
    parser.add_argument("--state",required=True)
    parser.add_argument("--pids",default="")
    parser.add_argument("--cpus",default="")
    args=parser.parse_args(argv)
    if not hasattr(os,"sched_setaffinity"):
        parser.error("Linux sched_setaffinity required")
    path=Path(args.state)
    if args.action=="apply":
        if path.exists() and not read(path).get("restored"):
            raise RuntimeError("Active state exists; reset it first")
        desired=cpus(args.cpus)
        entries=[]
        for pid in args.pids.split(","):
            for task in Path(f"/proc/{int(pid)}/task").iterdir():
                tid=int(task.name)
                entries.append({"tid":tid,"generation":generation(tid),"previous":sorted(os.sched_getaffinity(tid))})
        state={"threads":entries,"desired":sorted(desired),"restored":False}
        dump(path,state)
        try:
            for entry in entries:
                os.sched_setaffinity(entry["tid"],desired)
        except BaseException:
            restore(state)
            state["restored"]=True
            dump(path,state)
            raise
    else:
        state=read(path)
        if args.action=="reset":
            restore(state)
            state["restored"]=True
            dump(path,state)
        else:
            for entry in state["threads"]:
                if generation(entry["tid"]) != entry["generation"] or os.sched_getaffinity(entry["tid"]) != set(state["desired"]):
                    raise RuntimeError(f"Affinity verification failed for {entry['tid']}")
    print("ok")


if __name__=="__main__":
    main()
