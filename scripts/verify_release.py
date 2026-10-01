#!/usr/bin/env python3
"""Offline structural verifier for the current stable Nerve release."""
from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
import sys
import tempfile
try:
 import tomllib
except ModuleNotFoundError:
 import tomli as tomllib
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
 sys.path.insert(0,str(ROOT))
EXPECTED="0.3.1rc2"
EXPECTED_TOOLS={
 "nerve_decide","nerve_rank","nerve_verify","nerve_assess","nerve_context_curate","nerve_context_rehydrate","nerve_stats","nerve_nervous_event",
 "nerve_supervise_card","nerve_work_event","nerve_work_status","nerve_remote_delegate_task","nerve_remote_worker_status","nerve_remote_worker_result","nerve_remote_worker_cancel","nerve_remote_worker_control",
}
EXPECTED_HOOKS={"pre_tool_call","post_tool_call","pre_llm_call","transform_tool_result","pre_verify","post_api_request","api_request_error","post_llm_call","on_session_end"}
PROFILE_FAT_CAT_TOOLS={"nerve_decide","nerve_rank","nerve_verify","nerve_assess","nerve_context_curate","nerve_context_rehydrate","nerve_stats","nerve_nervous_event","nerve_supervise_card","nerve_work_event","nerve_work_status"}
PROFILE_OPERATOR_TOOLS={"nerve_decide","nerve_rank","nerve_verify","nerve_assess","nerve_context_curate","nerve_context_rehydrate","nerve_stats","nerve_nervous_event"}
PROFILE_MARIE_KONDO_TOOLS={"nerve_decide","nerve_rank","nerve_verify","nerve_assess","nerve_stats","nerve_supervise_card","nerve_work_event","nerve_work_status"}
PROFILE_LEAN_TOOLS={"nerve_decide","nerve_stats","nerve_nervous_event","nerve_supervise_card","nerve_work_event","nerve_work_status"}

class Ctx:
 def __init__(self,td,extra=None):self.tools=[];self.hooks=[];self.engine=None;self.td=td;self.extra=extra or {}
 def get_config(self,key,default=None):
  values={"work_supervision_db":str(self.td/"work.db"),"remote_data_dir":str(self.td/"remote")};values.update(self.extra);return values.get(key,default)
 def register_tool(self,*,name,schema=None,handler=None,**kw):self.tools.append(name)
 def register_hook(self,name,callback):self.hooks.append(name)
 def register_context_engine(self,engine):self.engine=engine

def plugin_version():
 text=(ROOT/"plugin.yaml").read_text();m=re.search(r'^version:\s*["\']?([^"\'\s]+)',text,re.M);assert m;return m.group(1)
def catalog_version():
 text=(ROOT/"packaging/hermes-catalog/nerve.yaml").read_text();m=re.search(r'^version:\s*["\']?([^"\'\s]+)',text,re.M);assert m;return m.group(1)

def main():
 py=tomllib.loads((ROOT/"pyproject.toml").read_text())
 assert py["project"]["version"]==EXPECTED
 assert plugin_version()==EXPECTED
 from hermes_nerve.provenance import VERSION
 assert VERSION==EXPECTED
 assert catalog_version()==EXPECTED, (catalog_version(), EXPECTED)
 assert py["project"]["dependencies"]==[],"core plugin must remain dependency-free"
 assert py["project"]["optional-dependencies"]["laya"]==["laya==0.3.3"]
 from hermes_nerve.cli import _advanced_catalog
 init_text=(ROOT/"__init__.py").read_text()
 profile_block=init_text[init_text.index("def _register_profile(ctx):"):init_text.index("\ndef register(ctx):",init_text.index("def _register_profile(ctx):"))]
 registration_keys=set(re.findall(r'get\("([a-zA-Z0-9_]+)"',profile_block))
 catalog_keys=set(_advanced_catalog())
 assert registration_keys<=catalog_keys,("advanced catalog missing profile-registration keys",sorted(registration_keys-catalog_keys))
 for rel in ("hermes_nerve/reflex/laya.py","hermes_nerve/reflex/openjev.py","hermes_nerve/reflex/shadow.py","hermes_nerve/reflex/laya_service.py","tests/test_reflex_laya_dev15b.py","tests/test_reflex_openjev_dev17.py","tests/test_dev15_controller_completion.py","docs/DEV15B_LAYA_INTEGRATION.md","docs/DEV16_INSTALL_AND_RETEST.md","docs/DEV17_OPEN_SOURCE_VALIDATION.md","scripts/install_dev15_profile.sh","scripts/verify_dev15.sh","scripts/install_dev16_profile.sh","scripts/verify_dev16.sh","scripts/install_dev17_profile.sh","scripts/verify_dev17.sh","scripts/check_laya_sidecar.py","scripts/check_openjev_sidecar.py","scripts/configure_reflex_profile.py","scripts/setup_laya_dev17.sh","scripts/setup_openjev_dev17.sh","scripts/run_dev17_model_matrix.py","tests/test_dev17_matrix_qol.py","tests/test_dev16_nerve_budget.py","hermes_nerve/work/nerve.py","docs/DEV16_SESSION_FINDINGS.md","scripts/verify_dev6_benchmark.py","benchmarks/dev6_event_delivery/task-body.md","benchmarks/dev6_event_delivery/fixture/tests/test_delivery.py","benchmarks/dev6_event_delivery/hidden_acceptance.py"):
  assert (ROOT/rel).exists(),rel
 from hermes_nerve.reflex import config as reflex_config
 reflex_config.configure(backend="laya",laya_base_url="http://127.0.0.1:8765",laya_model="convaiinnovations/laya-typed-decisions")
 cfg=reflex_config.settings();assert cfg["backend"]=="laya";assert cfg["laya_model"]=="convaiinnovations/laya-typed-decisions";assert "laya_token" not in cfg
 reflex_config.configure(backend="openjev",openjev_base_url="http://127.0.0.1:3000",openjev_model="openjev")
 cfg=reflex_config.settings();assert cfg["backend"]=="openjev";assert cfg["openjev_model"]=="openjev";assert "openjev_token" not in cfg
 reflex_config.configure(backend="jev")
 with tempfile.TemporaryDirectory() as t:
  td=Path(t)
  inherited_kanban={k:os.environ.get(k) for k in ("HERMES_KANBAN_TASK","HERMES_KANBAN_TASK_ID","HERMES_KANBAN_RUN_ID","HERMES_KANBAN_CLAIM_LOCK")}
  os.environ["HERMES_KANBAN_TASK"]="";os.environ["HERMES_KANBAN_TASK_ID"]=""
  spec=importlib.util.spec_from_file_location("hermes_nerve_plugin_verify",ROOT/"__init__.py",submodule_search_locations=[str(ROOT)]);mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod);ctx=Ctx(td);mod.register(ctx)
  assert set(ctx.tools)==EXPECTED_TOOLS,(set(ctx.tools)^EXPECTED_TOOLS)
  assert set(ctx.hooks)==EXPECTED_HOOKS,(ctx.hooks,EXPECTED_HOOKS)
  assert ctx.engine is not None
  profiles={
   "fat_cat":(PROFILE_FAT_CAT_TOOLS,True),
   "operator":(PROFILE_OPERATOR_TOOLS,True),
   "lean":(PROFILE_LEAN_TOOLS,False),
   "marie_kondo":(PROFILE_MARIE_KONDO_TOOLS,False),
  }
  for profile,(expected_tools,expect_engine) in profiles.items():
   pctx=Ctx(td,{"nerve_profile":profile,"remote_hosts":{}});mod.register(pctx)
   assert set(pctx.tools)==expected_tools,(profile,set(pctx.tools)^expected_tools)
   assert (pctx.engine is not None)==expect_engine,(profile,pctx.engine)
   assert "nerve_assistant" not in pctx.tools,profile
  # Ordinary Kanban workers must expose zero Jev schemas while retaining the
  # headless work-supervision hook path.
  old_env={k:os.environ.get(k) for k in ("HERMES_KANBAN_TASK","HERMES_KANBAN_RUN_ID","HERMES_KANBAN_CLAIM_LOCK","HERMES_NERVE_OFFLINE_VERIFY")}
  try:
   os.environ["HERMES_KANBAN_TASK"]="verify-headless";os.environ["HERMES_KANBAN_RUN_ID"]="1";os.environ["HERMES_KANBAN_CLAIM_LOCK"]="claim";os.environ["HERMES_NERVE_OFFLINE_VERIFY"]="1"
   hctx=Ctx(td);mod.register(hctx);assert hctx.tools==[],hctx.tools;assert set(hctx.hooks)==EXPECTED_HOOKS
  finally:
   for k,v in inherited_kanban.items():
    if v is None: os.environ.pop(k,None)
    else: os.environ[k]=v
  from hermes_nerve.work.store import SupervisionStore
  store=SupervisionStore(td/"verify-work.db");tables=store.raw_table_names();forbidden={"tasks","task_runs","dependencies","queue","queues","scheduler"};assert not (tables&forbidden),(tables&forbidden)
 from hermes_nerve import schemas
 variants=schemas.NERVE_ASSESS["parameters"]["properties"]["questions"]["additionalProperties"]["oneOf"];by={v["properties"]["type"]["enum"][0]:v for v in variants};assert by["choice"]["properties"]["criteria"]["minProperties"]==2;assert by["score"]["properties"]["criteria"]["minItems"]==2;assert "criteria" not in by["noul"]["required"]
 from hermes_nerve.work import runtime as work_runtime
 work_runtime.configure(enabled=True, completion_controller_attempts=3)
 assert work_runtime.settings()["completion_controller_attempts"]==3
 print(f"PASS version={EXPECTED} legacy_tools={len(EXPECTED_TOOLS)} fat_cat_tools={len(PROFILE_FAT_CAT_TOOLS)} operator_tools={len(PROFILE_OPERATOR_TOOLS)} lean_tools={len(PROFILE_LEAN_TOOLS)} marie_kondo_tools={len(PROFILE_MARIE_KONDO_TOOLS)} hook_names={len(EXPECTED_HOOKS)} catalog_version={catalog_version()} profiles=REGISTERED_AND_ASSERTED single_authority=PASS controller_completion=PASS reflex_laya=PASS reflex_openjev=PASS")
 return 0
if __name__=="__main__":raise SystemExit(main())