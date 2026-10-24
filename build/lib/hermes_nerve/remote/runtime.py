from __future__ import annotations
import threading
from pathlib import Path
from typing import Any
from .util import data_dir as default_data_dir
_LOCK=threading.RLock();_settings={"hosts":{}};_data_dir=None
def configure(*,hosts:dict[str,Any]|None=None,default_host:str="",default_max_turns:int=100,default_task_timeout_seconds:int=3600,data_dir:str="")->None:
 global _settings,_data_dir
 with _LOCK:
  _settings={"hosts":dict(hosts or {}),"default_host":str(default_host or ""),"default_max_turns":int(default_max_turns),"default_task_timeout_seconds":int(default_task_timeout_seconds)};_data_dir=Path(data_dir).expanduser() if str(data_dir or "").strip() else None
def settings()->dict[str,Any]:
 with _LOCK:return {**_settings,"hosts":dict(_settings.get("hosts") or {})}
def data_dir()->Path:
 with _LOCK:return _data_dir or default_data_dir()
