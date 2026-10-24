from __future__ import annotations
import os,re,time
from pathlib import Path
from typing import Any
from ..paths import hermes_home
_ALIAS=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PROFILE=re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
def now_ts()->float:return time.time()
def data_dir()->Path:
 explicit=str(os.getenv("HERMES_NERVE_REMOTE_DATA") or "").strip();return Path(explicit).expanduser() if explicit else hermes_home()/"plugin-data"/"hermes-nerve"/"remote"
def validate_alias(value:str)->str:
 text=str(value or "").strip()
 if not _ALIAS.fullmatch(text):raise ValueError("host alias must use only letters, digits, '.', '_' or '-'")
 return text
def validate_profile(value:str)->str:
 text=str(value or "").strip()
 if not _PROFILE.fullmatch(text):raise ValueError("profile must use only letters, digits, '.', '_' or '-'")
 return text
def truncate(value:Any,limit:int=4000)->str:
 text=str(value or "");return text if len(text)<=limit else text[:limit]+"..."
def sanitized_subprocess_env()->dict[str,str]:
 env=dict(os.environ)
 # Local runner may need Jev provider keys for controller-side judgments, but
 # SSH never forwards environment (-o SendEnv is not used). Strip common agent
 # credential forwarding knobs and SSH_ASKPASS to keep execution noninteractive.
 for key in list(env):
  if (
   key in {"SSH_ASKPASS","GIT_ASKPASS"}
   or key.startswith("HERMES_REMOTE_FORWARD_")
   or key in {
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_TASK_ID",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_IDENTITY",
    "HERMES_KANBAN_WORKER_ID",
    "HERMES_NERVE_DOD_HASH",
   }
  ):
   env.pop(key,None)
 return env
def pid_is_alive(pid:int)->bool:
 if pid<=0:return False
 try:os.kill(pid,0);return True
 except OSError:return False
