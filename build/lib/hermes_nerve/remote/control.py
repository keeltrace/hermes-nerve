from __future__ import annotations
import json,os,subprocess,time
from pathlib import Path,PurePosixPath
from typing import Any
from .config import RemoteHost
from .ssh import build_control_ssh_argv
from .util import sanitized_subprocess_env

def default_remote_control_path(host:RemoteHost,job_id:str)->str:
 root=host.control_root or ((host.workspace_root.rstrip("/")+"/.hermes-nerve-control") if host.workspace_root else "/tmp/hermes-nerve-control")
 return str(PurePosixPath(root)/f"{job_id}.json")
def write_remote_control(host:RemoteHost,remote_path:str,payload:dict[str,Any],*,timeout:float=20.0)->None:
 body=(json.dumps(payload,sort_keys=True,ensure_ascii=False,default=str)+"\n").encode();proc=subprocess.run(build_control_ssh_argv(host,remote_path),input=body,capture_output=True,timeout=timeout,env=sanitized_subprocess_env())
 if proc.returncode!=0:raise RuntimeError(f"remote control write failed: {proc.stderr.decode('utf-8','replace')[:1000]}")
def read_local_control()->dict[str,Any]|None:
 path=str(os.getenv("HERMES_NERVE_REMOTE_CONTROL_FILE") or "").strip()
 if not path:return None
 p=Path(path).expanduser()
 if not p.exists():return None
 try:data=json.loads(p.read_text(encoding="utf-8"))
 except Exception:return None
 return data if isinstance(data,dict) else None
def pre_tool_call(tool_name:str,args:dict,**kwargs):
 control=read_local_control()
 if not control:return None
 value=str(control.get("control") or "").upper()
 if value not in {"WATCH","REPLAN","BLOCK","STOP_REQUESTED"}:return None
 if tool_name in {"nerve_work_event","nerve_work_status","read_file","search_files"}:return None
 command=str((args or {}).get("command") or "")
 if tool_name in {"terminal","terminal.exec","shell"} and command.strip().startswith("git ") and any(command.strip().split()[1:2]==[x] for x in ("status","diff","add","commit","stash","rev-parse")):return None
 return {"action":"block","message":f"Nerve remote control {value} is active. Preserve useful work, emit nerve_work_event CHECKPOINT_READY with plan/tests/artifacts, and stop new implementation work.","rule_key":"jev:remote-work-control"}
