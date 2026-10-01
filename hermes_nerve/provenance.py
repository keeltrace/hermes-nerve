from __future__ import annotations
from typing import Any
VERSION="0.3.2.dev0"; VERIFIED="VERIFIED"; LOCAL_ONLY="LOCAL_ONLY"; UNVERIFIED="UNVERIFIED"; ERROR="ERROR"
def classify_status(*,live_provider_call:bool,request_id:str="",receipt_id:str="",error:bool=False,stale:bool=False)->str:
 if error:return ERROR
 if stale:return UNVERIFIED
 if not live_provider_call:return LOCAL_ONLY
 return VERIFIED if str(request_id or "").strip() or str(receipt_id or "").strip() else UNVERIFIED
def execution_provenance(*,live_provider_call:bool,transport:str="openrouter-decisions",request_id:str="",receipt_id:str="",error:bool=False,stale:bool=False)->dict[str,Any]:
 return {"engine":"hermes-nerve","version":VERSION,"transport":transport,"live_provider_call":bool(live_provider_call),"provenance_status":classify_status(live_provider_call=live_provider_call,request_id=request_id,receipt_id=receipt_id,error=error,stale=stale)}
def result_provenance(*,live_provider_call:bool,request_id:str="",receipt_id:str="",provider:str="",transport:str="openrouter-decisions",model:str="",contract:str="",created_at:str="",subject_sha256:str="",result_sha256:str="",error:bool=False,stale:bool=False)->dict[str,Any]:
 return {"verified_by":"hermes-nerve","provenance_status":classify_status(live_provider_call=live_provider_call,request_id=request_id,receipt_id=receipt_id,error=error,stale=stale),"request_id":str(request_id or ""),"receipt_id":str(receipt_id or ""),"provider":str(provider or ""),"transport":str(transport or ""),"model":str(model or ""),"contract":str(contract or ""),"created_at":str(created_at or ""),"subject_sha256":str(subject_sha256 or ""),"result_sha256":str(result_sha256 or ""),"stale":bool(stale)}
def attach(payload:dict[str,Any],*,live_provider_call:bool,transport:str="openrouter-decisions")->dict[str,Any]:
 out=dict(payload);out.setdefault("execution",execution_provenance(live_provider_call=live_provider_call,transport=transport));out.setdefault("provenance_status",out["execution"].get("provenance_status",UNVERIFIED));return out
