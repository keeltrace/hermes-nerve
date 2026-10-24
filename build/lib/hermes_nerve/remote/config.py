from __future__ import annotations
from dataclasses import dataclass,asdict
from pathlib import PurePosixPath
from typing import Any
from .errors import RemoteWorkerError
from .util import validate_alias,validate_profile
DEFAULT_MAX_TURNS=100;DEFAULT_TASK_TIMEOUT_SECONDS=3600;DEFAULT_CONNECT_TIMEOUT_SECONDS=15
@dataclass(frozen=True)
class RemoteHost:
 alias:str;ssh_host:str;workspace:str|None;workspace_root:str|None;profile:str|None;hermes_binary:str;max_turns:int;task_timeout_seconds:int;connect_timeout_seconds:int;model:str|None;provider:str|None;toolsets:tuple[str,...];ssh_binary:str="ssh";git_cache_root:str|None=None;control_root:str|None=None
 def as_dict(self):
  d=asdict(self);d["toolsets"]=list(self.toolsets);return d

def _token(value:Any,label:str,max_len:int=4096)->str:
 text=str(value or "").strip()
 if not text or len(text)>max_len or any(c in text for c in "\r\n\x00"):raise RemoteWorkerError("invalid_config",f"{label} is invalid")
 return text
def _ssh_host(value:Any)->str:
 text=_token(value,"ssh_host",512)
 if text.startswith("-") or any(c.isspace() for c in text):raise RemoteWorkerError("invalid_config","ssh_host must be one OpenSSH destination/Host alias, not SSH options")
 return text
def _posint(value:Any,default:int,low:int,high:int,label:str)->int:
 if value in (None,""):return default
 try:v=int(value)
 except Exception as exc:raise RemoteWorkerError("invalid_config",f"{label} must be an integer") from exc
 if v<low or v>high:raise RemoteWorkerError("invalid_config",f"{label} must be between {low} and {high}")
 return v
def _root(value:Any,label:str)->str|None:
 if value in (None,""):return None
 text=_token(value,label);path=PurePosixPath(text)
 if not path.is_absolute() or ".." in path.parts:raise RemoteWorkerError("invalid_config",f"{label} must be an absolute POSIX path without '..'")
 return str(path)
def _workspace_under(root:str,requested:Any)->str:
 raw=str(requested or "").strip()
 if any(c in raw for c in "\r\n\x00") or len(raw)>4096:raise RemoteWorkerError("invalid_workspace","workspace contains invalid characters")
 req=PurePosixPath(raw) if raw else PurePosixPath(root)
 if ".." in req.parts:raise RemoteWorkerError("invalid_workspace","workspace traversal is not allowed")
 rootp=PurePosixPath(root);candidate=req if req.is_absolute() else rootp/req
 try:candidate.relative_to(rootp)
 except ValueError as exc:raise RemoteWorkerError("invalid_workspace","workspace must be inside configured workspace_root") from exc
 return str(candidate)

def resolve_host(settings:dict[str,Any],*,requested_alias:Any,workspace:Any=None,profile:Any=None,max_turns:Any=None)->RemoteHost:
 hosts=settings.get("hosts") or {}
 if not isinstance(hosts,dict):raise RemoteWorkerError("invalid_config","remote hosts must be a mapping")
 alias_value=str(requested_alias or "").strip() or str(settings.get("default_host") or "").strip()
 if not alias_value:raise RemoteWorkerError("unknown_host","No host provided and no default remote host configured")
 try:alias=validate_alias(alias_value)
 except ValueError as exc:raise RemoteWorkerError("unknown_host",str(exc)) from exc
 raw=hosts.get(alias)
 if not isinstance(raw,dict):raise RemoteWorkerError("unknown_host",f"Unknown remote host alias: {alias}")
 if raw.get("enabled",True) is False:raise RemoteWorkerError("host_disabled",f"Remote host {alias!r} is disabled")
 ssh_host=_ssh_host(raw.get("ssh_host") or alias);root=_root(raw.get("workspace_root"),"workspace_root")
 default_workspace=raw.get("default_workspace")
 if workspace not in (None,""):
  if not root:raise RemoteWorkerError("invalid_workspace",f"Host {alias!r} has no workspace_root; caller-selected workspaces are disabled")
  resolved_workspace=_workspace_under(root,workspace)
 elif default_workspace not in (None,""):
  resolved_workspace=_workspace_under(root,default_workspace) if root else _root(default_workspace,"default_workspace")
 else:resolved_workspace=root
 configured=str(raw.get("profile") or "").strip() or None;allowed=raw.get("allowed_profiles") or []
 if not isinstance(allowed,list):raise RemoteWorkerError("invalid_config","allowed_profiles must be a list")
 allowed_set={validate_profile(str(x)) for x in allowed}
 if configured:configured=validate_profile(configured);allowed_set.add(configured)
 requested=str(profile or "").strip() or None
 if requested:
  requested=validate_profile(requested)
  if requested not in allowed_set:raise RemoteWorkerError("profile_not_allowed",f"Profile {requested!r} is not allowed for host {alias!r}")
 resolved_profile=requested or configured
 default_turns=_posint(settings.get("default_max_turns"),DEFAULT_MAX_TURNS,1,5000,"default_max_turns");host_turns=_posint(raw.get("max_turns"),default_turns,1,5000,"max_turns");turns=min(_posint(max_turns,host_turns,1,5000,"max_turns"),host_turns) if max_turns not in (None,"") else host_turns
 default_timeout=_posint(settings.get("default_task_timeout_seconds"),DEFAULT_TASK_TIMEOUT_SECONDS,30,604800,"default_task_timeout_seconds");task_timeout=_posint(raw.get("task_timeout_seconds"),default_timeout,30,604800,"task_timeout_seconds");connect_timeout=_posint(raw.get("connect_timeout_seconds"),DEFAULT_CONNECT_TIMEOUT_SECONDS,1,300,"connect_timeout_seconds")
 toolsets_raw=raw.get("toolsets") or [];toolsets=tuple(x.strip() for x in toolsets_raw.split(",") if x.strip()) if isinstance(toolsets_raw,str) else tuple(str(x).strip() for x in toolsets_raw if str(x).strip()) if isinstance(toolsets_raw,list) else ()
 if not isinstance(toolsets_raw,(str,list)):raise RemoteWorkerError("invalid_config","toolsets must be list or comma-separated string")
 return RemoteHost(alias=alias,ssh_host=ssh_host,workspace=resolved_workspace,workspace_root=root,profile=resolved_profile,hermes_binary=_token(raw.get("hermes_binary") or "hermes","hermes_binary"),max_turns=turns,task_timeout_seconds=task_timeout,connect_timeout_seconds=connect_timeout,model=str(raw.get("model") or "").strip() or None,provider=str(raw.get("provider") or "").strip() or None,toolsets=toolsets,ssh_binary=_token(raw.get("ssh_binary") or "ssh","ssh_binary"),git_cache_root=_root(raw.get("git_cache_root"),"git_cache_root"),control_root=_root(raw.get("control_root"),"control_root"))
def host_from_dict(data:dict[str,Any])->RemoteHost:
 d=dict(data);d["toolsets"]=tuple(d.get("toolsets") or ());return RemoteHost(**d)
