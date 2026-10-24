from __future__ import annotations
import shlex,shutil
from pathlib import Path
from typing import Any
from .config import RemoteHost
from .errors import RemoteWorkerError

def resolve_ssh_binary(value:str)->str:
 raw=str(value or "ssh").strip()
 if Path(raw).is_absolute():
  if not Path(raw).is_file():raise RemoteWorkerError("ssh_not_found",f"Configured SSH binary does not exist: {raw}")
  return raw
 found=shutil.which(raw)
 if not found:raise RemoteWorkerError("ssh_not_found",f"OpenSSH client {raw!r} was not found on PATH")
 return found
def q(v:str)->str:return shlex.quote(str(v))
def _remote_hermes_argv(host:RemoteHost,*,stream_json:bool)->list[str]:
 argv=[host.hermes_binary]
 if host.profile:argv += ["--profile",host.profile]
 if host.workspace:argv += ["--in","$JEV_WORKSPACE_REAL"]
 argv.append("chat")
 if stream_json:argv += ["--oneshot","--query-file","-","--format","stream-json","--source","tool"]
 else:argv += ["-Q","--query-file","-","--source","tool"]
 if host.model:argv += ["--model",host.model]
 if host.provider:argv += ["--provider",host.provider]
 if host.toolsets:argv += ["--toolsets",",".join(host.toolsets)]
 argv += ["--max-turns",str(host.max_turns)]
 return argv
def _render(argv:list[str])->str:
 out=[]
 for arg in argv:
  out.append('"$JEV_WORKSPACE_REAL"' if arg=="$JEV_WORKSPACE_REAL" else q(arg))
 return " ".join(out)
def build_remote_command(host:RemoteHost,*,env:dict[str,str]|None=None)->str:
 parts=["set -eu"]
 for key,value in sorted((env or {}).items()):
  if not key.replace("_","").isalnum() or not key[0].isalpha():raise RemoteWorkerError("invalid_config",f"invalid remote env key {key!r}")
  parts.append(f"export {key}={q(value)}")
 if host.workspace:
  parts.append(f"JEV_WORKSPACE={q(host.workspace)}")
  if host.workspace_root:
   parts.append(f"JEV_ROOT={q(host.workspace_root)}")
   parts.append('if ! cd -- "$JEV_ROOT"; then printf "%s\\n" "__JEV_REMOTE_ERROR__:workspace_root_missing" >&2; exit 72; fi')
   parts.append('JEV_ROOT_REAL=$(pwd -P)')
  parts.append('if ! cd -- "$JEV_WORKSPACE"; then printf "%s\\n" "__JEV_REMOTE_ERROR__:remote_workspace_missing" >&2; exit 74; fi')
  parts.append('JEV_WORKSPACE_REAL=$(pwd -P)')
  if host.workspace_root:
   parts.append('if [ "$JEV_ROOT_REAL" != "/" ]; then case "$JEV_WORKSPACE_REAL" in "$JEV_ROOT_REAL"|"$JEV_ROOT_REAL"/*) ;; *) printf "%s\\n" "__JEV_REMOTE_ERROR__:workspace_outside_root" >&2; exit 73 ;; esac; fi')
 help_cmd=_render([host.hermes_binary,"chat","--help"]);stream_cmd=_render(_remote_hermes_argv(host,stream_json=True));legacy_cmd=_render(_remote_hermes_argv(host,stream_json=False))
 parts.append(f"if {help_cmd} 2>/dev/null | grep -q -- '--format'; then exec {stream_cmd}; else printf '%s\\n' '__JEV_REMOTE_PROTOCOL__:legacy-text' >&2; exec {legacy_cmd}; fi")
 return "; ".join(parts)
def build_ssh_argv(host:RemoteHost,*,env:dict[str,str]|None=None)->list[str]:
 return [resolve_ssh_binary(host.ssh_binary),"-T","-o","BatchMode=yes","-o",f"ConnectTimeout={host.connect_timeout_seconds}","-o","ServerAliveInterval=15","-o","ServerAliveCountMax=3",host.ssh_host,build_remote_command(host,env=env)]
def build_control_ssh_argv(host:RemoteHost,remote_path:str)->list[str]:
 command=("set -eu; umask 077; mkdir -p -- " + q(str(Path(remote_path).parent)) + "; tmp=" + q(remote_path+".tmp") + "; cat > \"$tmp\"; mv -f -- \"$tmp\" " + q(remote_path))
 return [resolve_ssh_binary(host.ssh_binary),"-T","-o","BatchMode=yes","-o",f"ConnectTimeout={host.connect_timeout_seconds}",host.ssh_host,command]
def classify_failure(*,process_exit:int|None,result_exit:int|None,stderr_text:str,had_result:bool)->tuple[str,str]:
 lower=stderr_text.lower()
 if "__jev_remote_error__:workspace_root_missing" in lower:return "remote_workspace_missing","Configured remote workspace_root does not exist."
 if "__jev_remote_error__:remote_workspace_missing" in lower:return "remote_workspace_missing","Requested remote workspace does not exist."
 if "__jev_remote_error__:workspace_outside_root" in lower:return "invalid_workspace","Remote workspace resolved outside configured workspace_root."
 if "host key verification failed" in lower or "remote host identification has changed" in lower:return "host_key_failed","OpenSSH rejected the remote host key."
 if "permission denied" in lower or "authentication failed" in lower:return "ssh_auth_failed","OpenSSH authentication failed."
 if any(x in lower for x in ("connection refused","connection timed out","operation timed out","no route to host","could not resolve hostname","name or service not known")):return "ssh_connect_failed","OpenSSH could not establish a usable connection."
 if (process_exit==127 or result_exit==127) and "hermes" in lower and ("not found" in lower or "command not found" in lower):return "remote_hermes_missing","Hermes is not installed or not on PATH on remote host."
 if not had_result and process_exit==0:return "protocol_error","Remote Hermes exited without a terminal result event."
 if not had_result:return "remote_start_failed",f"Remote worker exited before a Hermes result event (ssh exit {process_exit})."
 return "remote_agent_failed",f"Remote Hermes reported non-zero result (exit {result_exit if result_exit is not None else process_exit})."
