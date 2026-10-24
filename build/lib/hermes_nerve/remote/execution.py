from __future__ import annotations
import json,os,subprocess,sys,time,uuid
from pathlib import Path
from typing import Any
from .config import RemoteHost,resolve_host
from .control import default_remote_control_path,write_remote_control
from .runtime import data_dir,settings
from .storage import create_job,get_job,init_storage,job_dir,reconcile,set_cancel,write_private
from .task import build_task_envelope
from .util import now_ts,pid_is_alive,sanitized_subprocess_env
from ..work.models import RunIdentity

TERMINAL={"completed","failed","cancelled","lost","fenced","rate_limited","handed_off"}
_LOCAL_RUNNERS:dict[str,subprocess.Popen]={}

def _reap_local_runner(job_id:str,*,wait_terminal:bool=False)->None:
 proc=_LOCAL_RUNNERS.get(job_id)
 if proc is None:return
 if proc.poll() is None and wait_terminal:
  try:proc.wait(timeout=2)
  except Exception:return
 if proc.poll() is None:return
 try:proc.wait(timeout=0)
 except Exception:pass
 _LOCAL_RUNNERS.pop(job_id,None)
class RemoteExecution:
 def __init__(self,job_id:str,root:Path|None=None):self.job_id=job_id;self.root=root or data_dir()
 def status(self):
  job=reconcile(self.job_id,pid_is_alive,self.root);_reap_local_runner(self.job_id,wait_terminal=bool(job and job.get("status") in TERMINAL));return job
 def wait(self,timeout:float|None=None,poll:float=.2):
  deadline=time.monotonic()+timeout if timeout is not None else None
  while True:
   job=self.status()
   if not job:return None
   if job.get("status") in TERMINAL:return job
   if deadline is not None and time.monotonic()>=deadline:raise TimeoutError(f"remote job {self.job_id} did not finish before timeout")
   time.sleep(poll)
 def result(self):
  job=self.status()
  if not job:return None
  return {k:job.get(k) for k in ("job_id","status","result_text","tokens","remote_session_id","error_code","error_message","duration_ms","workspace_result")}
 def cancel(self):set_cancel(self.job_id,self.root);return self.status()
 def request_control(self,control:dict[str,Any]):return RemoteManager(self.root).send_control(self.job_id,control)
class RemoteManager:
 def __init__(self,root:Path|None=None):self.root=init_storage(root or data_dir())
 def start(self,*,host_alias:str,goal:str,context:str="",workspace:str|None=None,profile:str|None=None,max_turns:int|None=None,identity:RunIdentity|None=None,supervision:dict[str,Any]|None=None,supervision_db:str="",supervision_mode:str="shadow",require_claim_fence:bool=False,git:dict[str,Any]|None=None)->RemoteExecution:
  host=resolve_host(settings(),requested_alias=host_alias,workspace=workspace,profile=profile,max_turns=max_turns);job_id="jevremote-"+uuid.uuid4().hex;path=job_dir(job_id,self.root);control_path=default_remote_control_path(host,job_id);task=build_task_envelope(goal,context,supervision=supervision);task_path=path/"task.txt";write_private(task_path,task)
  git_spec=dict(git or {})
  if identity and git_spec:
   safe_task="".join(c if c.isalnum() or c in "-_." else "-" for c in identity.task_id)[:70] or "task"
   git_spec.setdefault("result_key",f"{safe_task}-{identity.run_id}")
  started=now_ts();job={"job_id":job_id,"status":"starting","host_alias":host.alias,"ssh_host":host.ssh_host,"workspace":host.workspace,"profile":host.profile,"task_id":identity.task_id if identity else None,"run_id":identity.run_id if identity else None,"contract_hash":identity.contract_hash if identity else None,"claim_identity":identity.claim_identity if identity else None,"worker_id":identity.worker_id if identity else None,"supervision_db":supervision_db or None,"control_path":control_path,"git_json":git_spec or None,"started_at":started,"updated_at":started};create_job(job,self.root)
  spec={"host":host.as_dict(),"task_path":str(task_path),"identity":identity.as_dict() if identity else None,"supervision_db":supervision_db or "","supervision_mode":supervision_mode,"control_path":control_path,"require_claim_fence":bool(require_claim_fence),"reviewer":str((supervision or {}).get("reviewer") or ""),"git":git_spec or None};write_private(path/"runner-spec.json",json.dumps(spec,sort_keys=True,ensure_ascii=False,default=str))
  cmd=[sys.executable,"-m","hermes_nerve.remote.runner","--job-id",job_id,"--data-dir",str(self.root)];proc=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,env=sanitized_subprocess_env());_LOCAL_RUNNERS[job_id]=proc;from .storage import update_job;update_job(job_id,self.root,runner_pid=proc.pid);return RemoteExecution(job_id,self.root)
 def execution(self,job_id:str)->RemoteExecution:return RemoteExecution(job_id,self.root)
 def send_control(self,job_id:str,control:dict[str,Any]):
  path=job_dir(job_id,self.root);spec=json.loads((path/"runner-spec.json").read_text(encoding="utf-8"));host=RemoteHost(**{**spec["host"],"toolsets":tuple(spec["host"].get("toolsets") or ())});identity=spec.get("identity") or {};payload={"schema":"hermes-nerve-remote-control/v1","job_id":job_id,"task_id":identity.get("task_id"),"run_id":identity.get("run_id"),"contract_hash":identity.get("contract_hash"),"control":str(control.get("control") or "STOP_REQUESTED").upper(),"decision_id":str(control.get("decision_id") or ""),"reason":str(control.get("reason") or ""),"issued_at":time.time()};write_remote_control(host,str(spec["control_path"]),payload);return payload
