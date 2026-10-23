from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import sys

from .analysis import generate_report
from .calibrate import calibrate
from .common import dump
from .config import load
from .control import Controls
from .runner import run, make_plan, new_run, health
from .sources import inventory
from .trace import prepare


def hook_environment(c):
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    paths = [str(Path(__file__).resolve().parent/"bootstrap"),str(root)]
    paths.extend(c.get("sources",{}).get(k,"") for k in ("vllm_ascend","vllm","mooncake"))
    paths.append(env.get("PYTHONPATH",""))
    env["PYTHONPATH"] = os.pathsep.join(p for p in paths if p)
    if c.get("hooks",{}).get("enabled"):
        hooks=c["hooks"]
        dump(Path(hooks["control_file"]),{"enabled":False,"case_id":"startup","targets":hooks["targets"],"rules":[]})
        env["KVCBENCH_HOOK_CONFIG"]=hooks["control_file"]
        env["KVCBENCH_EVENT_DIR"]=hooks["event_dir"]
    return env


async def mock(c):
    from .mock_server import start
    server,port = await start()
    local=copy.deepcopy(c)
    local["sources"]["strict"]=False
    local["hooks"]["enabled"]=False
    local["experiment"].update(reference_qps=8,variants=["B0","B1"],repeats=1)
    local["run"].update(duration_s=.5,drain_s=.5,request_timeout_s=2,warmup_requests=0,cooldown_s=0)
    local["sweep"].update(context_tokens=[0],load=[.5,1],gap_s=[0],control_profiles=["none"])
    # Exercise the supplied real corpus, while keeping timing evidence software-only.
    local["trace"].update(datasets=["deep-swe","swe-marathon"],max_tasks=2,turns=2,output_tokens=4)
    local["calibration"]["evidence_path"]=""
    for name in ("B0","B1"):
        local["variants"][name].update(base_url=f"http://127.0.0.1:{port}/{name}",model="mock-model",
                                        accelerator_count=1,hardware_id="mock-protocol-fixture",metrics_urls=[f"http://127.0.0.1:{port}/{name}/metrics"],
                                        cache_policy="namespace")
        for k in ("state_argv","cache_reset_argv","cache_verify_argv","prepare_argv"):
            local["variants"][name].pop(k,None)
    try:
        return await run(local,mock=True)
    finally:
        await server.cleanup()


def main(argv=None):
    parser=argparse.ArgumentParser(description="vLLM Ascend + Mooncake HOST KVC testbed")
    parser.add_argument("action",choices=["doctor","prepare","plan","run","calibrate","report","mock","server","reset-controls","refine"])
    parser.add_argument("--config",default="config.toml")
    parser.add_argument("--run-dir")
    parser.add_argument("--online",action="store_true")
    parser.add_argument("--variant",default="B1")
    args=parser.parse_args(argv)
    if args.action=="report":
        if not args.run_dir:
            parser.error("report requires --run-dir")
        print(generate_report(Path(args.run_dir)))
        return
    c=load(args.config)
    if args.action=="plan":
        root=new_run(c,"plan")
        cases=make_plan(c)
        dump(root/"plan.json",cases)
        seconds=len(cases)*(c["run"]["duration_s"]+c["run"]["drain_s"]+c["run"].get("cooldown_s",0))
        print(f"{len(cases)} cases; measure + maximum drain + cooldown = {seconds/3600:.2f} h (warmup/control time additional)\n{root}")
    elif args.action=="prepare":
        root=new_run(c,"prepare")
        rows=prepare(c,root)
        print(f"Prepared {len(rows)} requests: {root}")
    elif args.action=="doctor":
        root=new_run(c,"doctor")
        info=inventory(c.get("sources",{}))
        dump(root/"source_inventory.json",info)
        issues=[]
        for key in ("vllm_ascend","mooncake"):
            if not info.get(key,{}).get("exists"):
                issues.append(f"Missing sources.{key}")
        targets=c.get("hooks",{}).get("targets",[])
        discovered=[t for s in info.values() for t in s.get("connectors",[])]
        if c.get("hooks",{}).get("enabled"):
            for t in targets:
                if not any(x["module"]==t["module"] and x["class"]==t["class"] and set(t["methods"]).issubset(x["methods"]) for x in discovered):
                    issues.append(f"Unresolved hook target: {t}")
        if args.online:
            async def probes():
                for name in c["experiment"]["variants"]:
                    dump(root/f"health-{name}.json",await health(c["variants"][name]))
            asyncio.run(probes())
        print(json.dumps({"source_inventory":str(root/"source_inventory.json"),"issues":issues,"online_checked":args.online},ensure_ascii=False,indent=2))
        if issues:
            raise SystemExit(2)
    elif args.action=="server":
        spec=c["variants"][args.variant]
        cmd=spec.get("server_argv")
        if not cmd:
            parser.error("Set variants.<name>.server_argv for the existing server launch command")
        env=hook_environment(c)
        env.update({k:str(v) for k,v in spec.get("server_env",{}).items()})
        os.execvpe(cmd[0],cmd,env)
    elif args.action=="reset-controls":
        root=new_run(c,"reset")
        async def reset():
            for name in c["sweep"]["control_profiles"]:
                ctl=Controls(c,c["controls"][name],"manual-reset",root,{"case_dir":root,"case_id":"manual-reset"})
                ctl.applied=True
                await ctl.__aexit__(None,None,None)
        asyncio.run(reset())
        print(root)
    else:
        try:
            if args.action=="refine":
                if not args.run_dir:
                    parser.error("refine requires --run-dir of a completed coarse sweep")
                previous=Path(args.run_dir)
                result=json.loads((previous/"reports"/"refinement.json").read_text(encoding="utf-8"))
                if not result.get("next_loads"):
                    parser.error("No supported knee candidate; no automatic refinement loads available")
                c["sweep"]["load"]=result["next_loads"]
                c["_refinement_of"]=str(previous.resolve())
            root=asyncio.run(mock(c) if args.action=="mock" else calibrate(c) if args.action=="calibrate" else run(c))
            print(root)
        except KeyboardInterrupt:
            print("Interrupted; control cleanup executed where the process remained alive. Inspect manifest/reset logs.",file=sys.stderr)
            raise SystemExit(130)


if __name__=="__main__":
    main()
