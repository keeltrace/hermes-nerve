from __future__ import annotations
import json,os,sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any,Iterator
from .util import data_dir as default_data_dir,now_ts,truncate
ACTIVE={"starting","running","cancelling"}
_SCHEMA="""
CREATE TABLE IF NOT EXISTS remote_jobs (
 job_id TEXT PRIMARY KEY,status TEXT NOT NULL,host_alias TEXT NOT NULL,ssh_host TEXT NOT NULL,
 workspace TEXT,profile TEXT,task_id TEXT,run_id INTEGER,contract_hash TEXT,claim_identity TEXT,worker_id TEXT,
 supervision_db TEXT,control_path TEXT,git_json TEXT,started_at REAL NOT NULL,updated_at REAL NOT NULL,finished_at REAL,
 runner_pid INTEGER,ssh_pid INTEGER,remote_session_id TEXT,exit_code INTEGER,duration_ms INTEGER,result_text TEXT,
 tokens_json TEXT,last_activity_json TEXT,error_code TEXT,error_message TEXT,cancel_requested INTEGER NOT NULL DEFAULT 0,
 protocol_warnings INTEGER NOT NULL DEFAULT 0,protocol TEXT NOT NULL DEFAULT 'unknown',workspace_result_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_remote_jobs_status ON remote_jobs(status);
CREATE INDEX IF NOT EXISTS idx_remote_jobs_task_run ON remote_jobs(task_id,run_id);
"""
@contextmanager
def connect(root:Path|None=None)->Iterator[sqlite3.Connection]:
 root=root or default_data_dir();root.mkdir(parents=True,exist_ok=True);conn=sqlite3.connect(root/"jobs.sqlite3",timeout=30,isolation_level=None);conn.row_factory=sqlite3.Row;conn.execute("PRAGMA journal_mode=WAL");conn.execute("PRAGMA busy_timeout=30000");conn.executescript(_SCHEMA)
 try:yield conn
 finally:conn.close()
def init_storage(root:Path|None=None)->Path:
 root=root or default_data_dir();root.mkdir(parents=True,exist_ok=True)
 try:root.chmod(0o700)
 except OSError:pass
 with connect(root):pass
 return root
def job_dir(job_id:str,root:Path|None=None)->Path:
 p=(root or default_data_dir())/"jobs"/job_id;p.mkdir(parents=True,exist_ok=True)
 try:p.chmod(0o700)
 except OSError:pass
 return p
def create_job(job:dict[str,Any],root:Path|None=None)->None:
 with connect(root) as con:
  cols=list(job);vals=[job[c] if not isinstance(job[c],(dict,list)) else json.dumps(job[c],sort_keys=True) for c in cols];con.execute(f"INSERT INTO remote_jobs({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",vals)
def _decode(row):
 if row is None:return None
 d=dict(row)
 for key in ("tokens_json","last_activity_json","git_json","workspace_result_json"):
  raw=d.pop(key,None);d[key[:-5] if key.endswith('_json') else key]=json.loads(raw) if raw else None
 d["cancel_requested"]=bool(d.get("cancel_requested"));return d
def get_job(job_id:str,root:Path|None=None):
 with connect(root) as con:row=con.execute("SELECT * FROM remote_jobs WHERE job_id=?",(job_id,)).fetchone()
 return _decode(row)
def update_job(job_id:str,root:Path|None=None,**fields:Any)->None:
 if not fields:return
 allowed={"status","workspace","updated_at","finished_at","runner_pid","ssh_pid","remote_session_id","exit_code","duration_ms","result_text","tokens_json","last_activity_json","error_code","error_message","cancel_requested","protocol_warnings","protocol","workspace_result_json"};bad=set(fields)-allowed
 if bad:raise ValueError(f"unsupported remote job fields: {sorted(bad)}")
 fields.setdefault("updated_at",now_ts());pairs=[];vals=[]
 for key,value in fields.items():
  if key.endswith("_json") and value is not None and not isinstance(value,str):value=json.dumps(value,ensure_ascii=False,separators=(",",":"),default=str)
  if key=="error_message" and value is not None:value=truncate(value)
  pairs.append(f"{key}=?");vals.append(value)
 vals.append(job_id)
 with connect(root) as con:con.execute(f"UPDATE remote_jobs SET {', '.join(pairs)} WHERE job_id=?",vals)
def set_cancel(job_id:str,root:Path|None=None)->bool:
 with connect(root) as con:
  cur=con.execute("UPDATE remote_jobs SET cancel_requested=1,status=CASE WHEN status IN ('starting','running') THEN 'cancelling' ELSE status END,updated_at=? WHERE job_id=?",(now_ts(),job_id));return cur.rowcount>0
def write_private(path:Path,data:bytes|str)->None:
 path.parent.mkdir(parents=True,exist_ok=True);fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
 if isinstance(data,bytes):
  with os.fdopen(fd,"wb") as h:h.write(data)
 else:
  with os.fdopen(fd,"w",encoding="utf-8") as h:h.write(data)
def append_text(path:Path,text:str)->None:
 path.parent.mkdir(parents=True,exist_ok=True)
 with open(path,"a",encoding="utf-8") as h:h.write(text)
 try:path.chmod(0o600)
 except OSError:pass
def reconcile(job_id:str,pid_alive,root:Path|None=None):
 job=get_job(job_id,root)
 if not job or job["status"] not in ACTIVE:return job
 pid=job.get("runner_pid")
 if pid and pid_alive(int(pid)):return job
 if now_ts()-float(job.get("updated_at") or 0)<2:return job
 if job.get("cancel_requested"):update_job(job_id,root,status="cancelled",finished_at=now_ts(),error_code="cancelled",error_message="Cancellation requested and local runner is no longer alive.")
 else:update_job(job_id,root,status="lost",finished_at=now_ts(),error_code="connection_lost",error_message="Local runner died without a terminal result.")
 return get_job(job_id,root)
