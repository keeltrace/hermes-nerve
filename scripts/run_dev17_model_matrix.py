#!/usr/bin/env python3
"""Nerve backend matrix: hosted Jev vs Laya vs OpenJev vs Liquid D1.

The worker model, fixture, DoD, Nerve thresholds, and Hermes profile baseline are
held constant. Only ``reflex_backend`` changes. Results are flushed after every
arm so an interrupted run remains analyzable.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "benchmarks/dev6_event_delivery/fixture"
HIDDEN = ROOT / "benchmarks/dev6_event_delivery/hidden_acceptance.py"
TASK_BODY_FILE = ROOT / "benchmarks/dev6_event_delivery/task-body.md"
FIELDS = [
    "pair","arm","profile","board","task","status","run_status","outcome","classification",
    "wall_s","calls","input","output","primary","cache_read","supervisor_tokens","combined",
    "nerve_worker_accounted","token_target","nerve_level","nerve_kill","nerve_orch_review","forecast_value","extension_tokens","watch_count","replan_count",
    "post_pass_calls","native_completion","external_stop","tests_rc","hidden_rc","git_clean","valid","invalid_reason",
]
CURRENT: dict[str, Any] = {"pid": 0, "board": "", "task": ""}


def run(cmd, *, check=True, capture=True, input_text=None, cwd=None, timeout=None):
    kwargs={"text":True,"check":check,"cwd":cwd,"timeout":timeout}
    if capture: kwargs["stdout"]=subprocess.PIPE; kwargs["stderr"]=subprocess.STDOUT
    if input_text is not None: kwargs["input"]=input_text
    p=subprocess.run(cmd,**kwargs)
    return p.stdout if capture else ""


def hermes(*args, check=True, input_text=None):
    return run(["hermes",*map(str,args)],check=check,input_text=input_text)


def parse_usage(path: Path) -> tuple[int,int,int,int,int]:
    text=path.read_text(errors="replace") if path.exists() else ""
    rows=[]
    for line in text.splitlines():
        if "API call #" not in line: continue
        mi=re.search(r"\bin=(\d+)",line); mo=re.search(r"\bout=(\d+)",line); mc=re.search(r"\bcache=(\d+)(?:/(\d+))?",line)
        if mi and mo: rows.append((int(mi.group(1)),int(mo.group(1)),int(mc.group(1)) if mc else 0))
    return len(rows),sum(x[0] for x in rows),sum(x[1] for x in rows),sum(x[0]+x[1] for x in rows),sum(x[2] for x in rows)


def metrics(profile: str, task: str, run_id: int) -> dict[str, Any]:
    db=Path.home()/".hermes/profiles"/profile/"jev/work-supervision.sqlite3"
    out={"worker":0,"supervisor":0,"target":0,"level":"CONTINUE","kill":0,"orch_review":0,"forecast":"","extension_tokens":0,"watch":0,"replan":0,"post":0,"native":0}
    if not db.exists(): return out
    con=sqlite3.connect(db)
    def one(sql,args=()):
        try:
            row=con.execute(sql,args).fetchone(); return row[0] if row and row[0] is not None else 0
        except Exception: return 0
    out["worker"]=int(one("SELECT COALESCE(SUM(accounted_tokens),0) FROM api_usage WHERE task_id=? AND run_id=?",(task,run_id)))
    out["supervisor"]=int(one("SELECT COALESCE(SUM(total_tokens),0) FROM supervisor_usage WHERE task_id=? AND run_id=?",(task,run_id)))
    try:
        row=con.execute("""SELECT c.payload_json,c.reserve_tokens FROM contracts c JOIN run_bindings r ON r.contract_hash=c.contract_hash WHERE r.task_id=? AND r.run_id=?""",(task,run_id)).fetchone()
        if row:
            p=json.loads(row[0]); out["target"]=int(row[1] or 0)+sum(max(0,int(x.get("estimated_tokens") or 0)) for x in p.get("criteria",[]))
    except Exception: pass
    levels=[]
    try:
        for (payload,) in con.execute("SELECT payload_json FROM supervision_diagnostics WHERE task_id=? AND run_id=? AND kind='nerve_observer' ORDER BY seq",(task,run_id)):
            try: levels.append(str(json.loads(payload).get("level") or ""))
            except Exception: pass
    except Exception: pass
    rank={"CONTINUE":0,"WATCH":1,"FORECAST":2,"REPLAN":3,"ORCH_REVIEW":4,"KILL":5}
    if levels: out["level"]=max(levels,key=lambda x:rank.get(x,-1))
    out["kill"]=int("KILL" in levels); out["watch"]=sum(x=="WATCH" for x in levels); out["replan"]=sum(x=="REPLAN" for x in levels)
    out["orch_review"]=int(one("SELECT COUNT(*) FROM supervision_diagnostics WHERE task_id=? AND run_id=? AND kind='nerve_orchestrator_handoff'",(task,run_id)) > 0)
    try:
        row=con.execute("SELECT payload_json FROM supervision_diagnostics WHERE task_id=? AND run_id=? AND kind='nerve_budget_forecast' ORDER BY rowid DESC LIMIT 1",(task,run_id)).fetchone()
        if row: out["forecast"]=str(json.loads(row[0]).get("value") or "")
        row=con.execute("SELECT payload_json FROM supervision_diagnostics WHERE task_id=? AND run_id=? AND kind='nerve_budget_extension' ORDER BY rowid DESC LIMIT 1",(task,run_id)).fetchone()
        if row:
            ext=json.loads(row[0]); out["extension_tokens"]=int(ext.get("extension_tokens") or 0); out["target"]=int(ext.get("effective_token_target") or out["target"])
    except Exception: pass
    out["post"]=int(one("SELECT COUNT(*) FROM supervision_diagnostics WHERE task_id=? AND run_id=? AND kind='completion_worker_call_after_verified'",(task,run_id)))
    out["native"]=int(one("SELECT COUNT(*) FROM supervision_diagnostics WHERE task_id=? AND run_id=? AND kind='completion_native_dispatch'",(task,run_id)))
    con.close(); return out


def patch_profile(profile: str, backend: str, args) -> None:
    py=Path.home()/".hermes/hermes-agent/venv/bin/python"
    cmd=[str(py if py.exists() else Path(sys.executable)),str(ROOT/"scripts/configure_reflex_profile.py"),profile,"--backend",backend,
         "--laya-base-url",args.laya_url,"--openjev-base-url",args.openjev_url,
         "--d1-base-url",args.d1_url,"--d1-model",args.d1_model,
         "--orchestrator-reviewer",args.source_profile]
    if args.openjev_expected_identity: cmd += ["--openjev-expected-identity",args.openjev_expected_identity]
    run(cmd,check=True)


def sidecar_preflight(backend: str, args) -> None:
    if backend=="laya":
        run([sys.executable,str(ROOT/"scripts/check_laya_sidecar.py"),"--base-url",args.laya_url],check=True,timeout=30)
    elif backend=="openjev":
        cmd=[sys.executable,str(ROOT/"scripts/check_openjev_sidecar.py"),"--base-url",args.openjev_url]
        if args.openjev_expected_identity: cmd += ["--expected-identity",args.openjev_expected_identity]
        run(cmd,check=True,timeout=30)
    elif backend=="d1":
        cmd=[sys.executable,str(ROOT/"scripts/check_systemone_sidecar.py"),"--d1","--base-url",args.d1_url,"--model",args.d1_model]
        if args.d1_expected_identity: cmd += ["--expected-identity",args.d1_expected_identity]
        run(cmd,check=True,timeout=30)


def install_profile(profile: str, backend: str, args) -> None:
    hermes("profile","create",profile,"--clone-from",args.source_profile,"--no-alias")
    shared=Path.home()/".hermes/shared/nous_auth.json"
    if shared.exists(): hermes("-p",profile,"auth","add","nous","--type","oauth",check=False,input_text="\n")
    dest=Path.home()/".hermes/profiles"/profile/"plugins/hermes-jev"
    if dest.exists(): shutil.rmtree(dest)
    shutil.copytree(ROOT,dest,ignore=shutil.ignore_patterns(".pytest_cache","__pycache__","*.pyc","PACKAGE_SHA256SUMS.txt"))
    hermes("-p",profile,"plugins","enable","hermes-jev")
    patch_profile(profile,backend,args)


def make_repo(path: Path) -> None:
    shutil.copytree(FIXTURE,path)
    extra=path/"tests/test_delivery_acceptance_visible.py"
    extra.write_text('''from delivery.dispatcher import Dispatcher\nfrom delivery.model import Event\nfrom delivery.store import DeliveryStore\n\ndef test_dead_event_redispatch_does_not_mutate_attempts():\n    s=DeliveryStore(); s.put(Event("d","payload"))\n    def h(e): raise RuntimeError("nope")\n    d=Dispatcher(s,h,max_attempts=2); assert d.dispatch("d").startswith("retry:"); assert d.dispatch("d")=="dead"\n    before=s.attempts("d"); assert d.dispatch("d")=="dead"; assert s.attempts("d")==before\n\ndef test_delivered_event_redispatch_is_duplicate_without_reinvoking_handler():\n    s=DeliveryStore(); s.put(Event("ok","payload")); calls=[]\n    def h(e): calls.append(e.event_id)\n    d=Dispatcher(s,h); assert d.dispatch("ok")=="delivered"; assert d.dispatch("ok")=="duplicate"; assert calls==["ok"]\n''')
    run(["git","init","-q"],cwd=path); run(["git","add","."],cwd=path); run(["git","-c","user.name=Jev Matrix","-c","user.email=jev-matrix@localhost","commit","-qm","freeze dev17 matrix baseline"],cwd=path)


def task_body() -> str:
    return TASK_BODY_FILE.read_text()+'''\n\n## Dev17 open-source backend matrix requirements\nAll Definition-of-Done items remain mandatory. Add tests/test_delivery_regression.py with at least five focused regression tests. Run the full suite, commit intended changes, leave git clean, and finish with the directly listed kanban_complete tool. Do not use the Hermes Kanban CLI, direct SQLite lifecycle mutation, or completion helper scripts from the worker.\n'''


def card(board: str, task: str) -> dict[str, Any]:
    try: return json.loads(hermes("kanban","--board",board,"show",task,"--json"))
    except Exception: return {}


def stop_current(reason="interrupted"):
    pid=int(CURRENT.get("pid") or 0)
    if pid:
        try: os.kill(pid,signal.SIGTERM)
        except (ProcessLookupError,PermissionError): pass
    b,t=CURRENT.get("board"),CURRENT.get("task")
    if b and t:
        hermes("kanban","--board",b,"block",t,f"dev17 matrix {reason}",check=False)


def lifecycle_terminal(status: str, run_status: str) -> bool:
    """Return True only when the whole same-card lifecycle is terminal.

    Review is deliberately non-terminal: Nerve hands authority to Hermes with
    kanban_request_review and the matrix must keep dispatching until the reviewer
    makes the final COMPLETE / CHANGES / BLOCK decision.
    """
    if status in {"done", "completed", "blocked", "archived"}:
        return True
    return run_status in {"blocked", "crashed", "timed_out", "gave_up", "rate_limited"}


def classification(status,run_status,tests_rc,hidden_rc,git_clean,external,nerve_kill,orch_review=0):
    if external: return "HARNESS_KILL"
    if tests_rc or hidden_rc: return "VERIFICATION_FAIL"
    if not git_clean: return "IMPLEMENTATION_FAIL"
    if status in {"done","completed"} and run_status in {"done","completed"}: return "SUCCESS"
    if orch_review or status == "review" or run_status == "review": return "ORCH_REVIEW"
    if nerve_kill: return "NERVE_KILL"  # legacy dev16/dev17-pre-RC evidence only
    return "LIFECYCLE_FAIL"


def run_arm(pair: int, backend: str, args, out: Path) -> dict[str, Any]:
    stamp=args.stamp; profile=f"ab17-{backend}-{stamp}-p{pair}"; board=f"jev-dev17-{stamp}-p{pair}-{backend}"; repo=out/f"pair{pair}-{backend}-repo"
    sidecar_preflight(backend,args); install_profile(profile,backend,args); make_repo(repo)
    hermes("kanban","boards","create",board,"--name",f"Dev17 matrix {stamp} p{pair} {backend}","--description","Hermes-Jev dev17 open-source Reflex matrix")
    task_json=json.loads(hermes("kanban","--board",board,"create",f"DEV17 matrix pair {pair} {backend}: repair frozen event delivery fixture","--body",task_body(),"--assignee",profile,"--workspace",f"dir:{repo}","--max-runtime",str(args.max_runtime),"--max-retries","1","--json"))
    task=task_json["id"]; CURRENT.update(pid=0,board=board,task=task)
    deadline=time.time()+args.spawn_wait; run_id=None; pid=0
    while time.time()<deadline and run_id is None:
        hermes("kanban","--board",board,"dispatch","--max","1","--json",check=False); time.sleep(2)
        d=card(board,task); runs=d.get("runs",[])
        if runs:
            rr=max(runs,key=lambda x:x["id"]); run_id=int(rr["id"]); pid=int(rr.get("worker_pid") or 0)
        else: time.sleep(3)
    if run_id is None: raise RuntimeError(f"worker did not spawn for {backend}")
    CURRENT["pid"]=pid; print(f"WORKER_STARTED pair={pair} arm={backend} run={run_id} pid={pid}",flush=True)
    implementation_run_id=run_id
    current_run_id=run_id
    start=time.time(); external=0; status=run_status=outcome="-"
    while time.time()-start < args.max_runtime:
        d=card(board,task); t=d.get("task",{}); runs=d.get("runs",[]); rr=max(runs,key=lambda x:x["id"]) if runs else {}
        status=str(t.get("status") or "missing"); run_status=str(rr.get("status") or "-"); outcome=str(rr.get("outcome") or "-")
        latest_run_id=int(rr.get("id") or 0)
        if latest_run_id and latest_run_id != current_run_id:
            current_run_id=latest_run_id
            pid=int(rr.get("worker_pid") or 0)
            CURRENT["pid"]=pid
            print(f"FOLLOWUP_WORKER_STARTED pair={pair} arm={backend} run={current_run_id} pid={pid} task_status={status}",flush=True)

        if lifecycle_terminal(status,run_status):
            break

        # kanban_request_review ends the implementation run and moves the card
        # to review. Keep dispatching through reviewer/orchestrator resolution.
        if status in {"review","ready"} and run_status not in {"running","in_progress"}:
            hermes("kanban","--board",board,"dispatch","--max","1","--json",check=False)
            time.sleep(2)
            continue

        calls,inp,outp,primary,cache=parse_usage(Path.home()/".hermes/profiles"/profile/"logs/agent.log")
        if primary >= args.emergency_primary_cap:
            external=1; print(f"EXTERNAL_EMERGENCY_STOP pair={pair} arm={backend} primary={primary} cap={args.emergency_primary_cap}",flush=True)
            try: os.kill(pid,signal.SIGTERM)
            except (ProcessLookupError,PermissionError): pass
            hermes("kanban","--board",board,"block",task,f"dev17 benchmark emergency primary-token cap {args.emergency_primary_cap} exceeded",check=False); break
        if pid:
            try: os.kill(pid,0)
            except ProcessLookupError:
                hermes("kanban","--board",board,"dispatch","--max","1","--json",check=False)
                time.sleep(2)
                continue
            except PermissionError: pass
        time.sleep(5)
    else:
        external=1; stop_current("max-runtime")
    wall=int(time.time()-start)
    if pid:
        for _ in range(20):
            try: os.kill(pid,0); time.sleep(1)
            except ProcessLookupError: break
            except PermissionError: break
        else:
            print(f"LINGERING_WORKER_TERM pair={pair} arm={backend} pid={pid}",flush=True)
            try: os.kill(pid,signal.SIGTERM)
            except (ProcessLookupError,PermissionError): pass
    time.sleep(1); d=card(board,task); t=d.get("task",{}); runs=d.get("runs",[]); rr=max(runs,key=lambda x:x["id"]) if runs else {}
    status=str(t.get("status") or "missing"); run_status=str(rr.get("status") or "-"); outcome=str(rr.get("outcome") or "-")
    tests=subprocess.run([sys.executable,"-m","pytest","tests/","-q"],cwd=repo,stdout=(out/f"pair{pair}-{backend}-tests.txt").open("w"),stderr=subprocess.STDOUT).returncode
    hidden=subprocess.run([sys.executable,str(HIDDEN),str(repo)],stdout=(out/f"pair{pair}-{backend}-hidden.txt").open("w"),stderr=subprocess.STDOUT).returncode
    git_clean=not bool(run(["git","status","--short"],cwd=repo).strip())
    calls,inp,outp,primary,cache=parse_usage(Path.home()/".hermes/profiles"/profile/"logs/agent.log"); m=metrics(profile,task,implementation_run_id); combined=primary+m["supervisor"]
    cls=classification(status,run_status,tests,hidden,git_clean,external,m["kill"],m["orch_review"])
    valid=int(cls=="SUCCESS"); reasons=[]
    if status not in {"done","completed"}: reasons.append("task_"+status)
    if run_status not in {"done","completed"}: reasons.append("run_"+run_status)
    if tests: reasons.append("tests");
    if hidden: reasons.append("hidden")
    if not git_clean: reasons.append("dirty_git")
    if external: reasons.append("external_stop")
    row={"pair":pair,"arm":backend,"profile":profile,"board":board,"task":task,"status":status,"run_status":run_status,"outcome":outcome,"classification":cls,"wall_s":wall,"calls":calls,"input":inp,"output":outp,"primary":primary,"cache_read":cache,"supervisor_tokens":m["supervisor"],"combined":combined,"nerve_worker_accounted":m["worker"],"token_target":m["target"],"nerve_level":m["level"],"nerve_kill":m["kill"],"nerve_orch_review":m["orch_review"],"forecast_value":m["forecast"],"extension_tokens":m["extension_tokens"],"watch_count":m["watch"],"replan_count":m["replan"],"post_pass_calls":m["post"],"native_completion":m["native"],"external_stop":external,"tests_rc":tests,"hidden_rc":hidden,"git_clean":int(git_clean),"valid":valid,"invalid_reason":",".join(reasons) if reasons else "-"}
    print(f"PAIR={pair} ARM={backend} valid={valid} class={cls} wall={wall}s calls={calls} primary={primary} supervisor={m['supervisor']} combined={combined} nerve={m['level']} kill={m['kill']} orch_review={m['orch_review']} forecast={m['forecast'] or '-'} extension={m['extension_tokens']} target={m['target']} reason={row['invalid_reason']}",flush=True)
    CURRENT.update(pid=0,board="",task=""); return row


def write_summary(rows: list[dict[str, Any]], out: Path):
    lines=["=== DEV17 OPEN-SOURCE REFLEX MATRIX SUMMARY ==="]
    for r in rows: lines.append(f"pair {r['pair']} {r['arm']}: valid={r['valid']} class={r['classification']} calls={r['calls']} combined={r['combined']} wall={r['wall_s']}s nerve={r['nerve_level']} kill={r['nerve_kill']} orch_review={r['nerve_orch_review']} forecast={r['forecast_value'] or '-'} extension={r['extension_tokens']} external={r['external_stop']}")
    for arm in sorted({r['arm'] for r in rows}):
        rr=[r for r in rows if r['arm']==arm]; valid=[r for r in rr if int(r['valid'])==1]
        lines.append(f"{arm}_valid={len(valid)}/{len(rr)}")
        if rr:
            vals=[int(r['combined']) for r in rr]; lines.append(f"{arm}_combined_median={int(statistics.median(vals))} {arm}_combined_max={max(vals)}")
    base={str(r['pair']):r for r in rows if r['arm']=='jev' and int(r['valid'])==1}
    for arm in ('laya','openjev','d1'):
        comps=[]
        for r in rows:
            if r['arm']==arm and int(r['valid'])==1 and str(r['pair']) in base:
                b=base[str(r['pair'])]; bt=int(b['combined']); at=int(r['combined']); comps.append((str(r['pair']),bt,at,(bt-at)/bt*100 if bt else 0.0))
        lines.append(f"{arm}_valid_matched_vs_jev={len(comps)}")
        for p,bt,at,pct in comps: lines.append(f"pair {p} jev={bt} {arm}={at} delta_vs_jev={pct:+.2f}%")
    text="\n".join(lines)+"\n"; (out/"summary.txt").write_text(text); print("\n"+text,flush=True)


def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument('--pairs',type=int,default=int(os.getenv('PAIRS','2')))
    p.add_argument('--arms',default=os.getenv('ARMS','jev,laya,openjev,d1'))
    p.add_argument('--source-profile',default=os.getenv('SOURCE_PROFILE','abtest-jev-dev17'))
    p.add_argument('--laya-url',default=os.getenv('HERMES_REFLEX_LAYA_BASE_URL','http://127.0.0.1:8765'))
    p.add_argument('--openjev-url',default=os.getenv('HERMES_REFLEX_OPENJEV_BASE_URL','http://127.0.0.1:3000'))
    p.add_argument('--openjev-expected-identity',default=os.getenv('HERMES_REFLEX_OPENJEV_EXPECTED_IDENTITY',''))
    p.add_argument('--d1-url',default=os.getenv('HERMES_REFLEX_D1_BASE_URL','http://127.0.0.1:8080'))
    p.add_argument('--d1-model',default=os.getenv('HERMES_REFLEX_D1_MODEL','LiquidAI/d1-3B'))
    p.add_argument('--d1-expected-identity',default=os.getenv('HERMES_REFLEX_D1_EXPECTED_IDENTITY',''))
    p.add_argument('--emergency-primary-cap',type=int,default=int(os.getenv('EMERGENCY_PRIMARY_CAP','2400000')))
    p.add_argument('--max-runtime',type=int,default=3600); p.add_argument('--spawn-wait',type=int,default=180)
    p.add_argument('--out',default='')
    a=p.parse_args(); a.stamp=time.strftime('%Y%m%d-%H%M%S'); arms=[x.strip() for x in a.arms.split(',') if x.strip()]
    unknown=set(arms)-{'jev','laya','openjev','d1'}
    if unknown: raise SystemExit(f"unsupported arms: {sorted(unknown)}")
    if not (Path.home()/'.hermes/profiles'/a.source_profile).is_dir(): raise SystemExit(f"source profile missing: {a.source_profile}")
    out=Path(a.out).expanduser() if a.out else Path.home()/f"jev-dev17-matrix-{a.stamp}"; out.mkdir(parents=True,exist_ok=True)
    results=out/'results.tsv'; rows=[]
    def on_signal(signum,frame): stop_current('signal'); write_summary(rows,out); raise SystemExit(128+signum)
    signal.signal(signal.SIGINT,on_signal); signal.signal(signal.SIGTERM,on_signal)
    try:
        for pair in range(1,a.pairs+1):
            order=arms[(pair-1)%len(arms):]+arms[:(pair-1)%len(arms)]
            for arm in order:
                print(f"=== PAIR {pair}/{a.pairs}: {arm} ===",flush=True)
                row=run_arm(pair,arm,a,out); rows.append(row)
                with results.open('w',newline='') as f:
                    w=csv.DictWriter(f,fieldnames=FIELDS,delimiter='\t'); w.writeheader(); w.writerows(rows); f.flush(); os.fsync(f.fileno())
    finally:
        write_summary(rows,out)
    print(f"DEV17_MATRIX=COMPLETE\nOUT={out}")
    return 0

if __name__=='__main__': raise SystemExit(main())
