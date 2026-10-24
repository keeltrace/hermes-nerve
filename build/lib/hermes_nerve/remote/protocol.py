from __future__ import annotations
import json
from dataclasses import dataclass,field
from typing import Any,Callable

@dataclass
class ProtocolState:
 remote_session_id:str|None=None
 result_event:dict[str,Any]|None=None
 last_activity:dict[str,Any]|None=None
 warnings:int=0
 events:int=0
 protocol:str="stream-json"
 last_tool_use:dict[str,Any]|None=None
 on_event:Callable[[dict[str,Any]],None]|None=field(default=None,repr=False)
 def consume_line(self,line:str)->dict[str,Any]|None:
  stripped=line.strip()
  if not stripped:return None
  self.events+=1
  try:event=json.loads(stripped)
  except json.JSONDecodeError:self.warnings+=1;return None
  if not isinstance(event,dict):self.warnings+=1;return None
  et=str(event.get("type") or "")
  if et=="system" and event.get("subtype")=="init":
   if event.get("session_id"):self.remote_session_id=str(event["session_id"])
   self.last_activity={"type":"system","subtype":"init"}
  elif et=="tool_use":
   self.last_tool_use=event;self.last_activity={"type":"tool_use","name":str(event.get("name") or "unknown")}
  elif et=="tool_result":self.last_activity={"type":"tool_result","name":str(event.get("name") or "unknown"),"is_error":bool(event.get("is_error"))}
  elif et=="text":self.last_activity={"type":"text"}
  elif et=="result":
   self.result_event=event
   if event.get("session_id"):self.remote_session_id=str(event["session_id"])
   self.last_activity={"type":"result","exit_code":event.get("exit_code")}
  else:self.last_activity={"type":et or "unknown"}
  if self.on_event:
   try:self.on_event(event)
   except Exception:self.warnings+=1
  return event
