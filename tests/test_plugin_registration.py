from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_nerve.provenance import VERSION

ROOT=Path(__file__).resolve().parents[1]
EXPECTED_TOOLS={
    "nerve_decide","nerve_rank","nerve_verify","nerve_assess","nerve_context_curate","nerve_context_rehydrate",
    "nerve_stats","nerve_nervous_event","nerve_supervise_card","nerve_work_event","nerve_work_status",
    "nerve_remote_delegate_task","nerve_remote_worker_status","nerve_remote_worker_result","nerve_remote_worker_cancel","nerve_remote_worker_control",
}
EXPECTED_HOOKS={"pre_tool_call","post_tool_call","pre_llm_call","transform_tool_result","pre_verify","post_api_request","api_request_error","post_llm_call","on_session_end"}

class FakeCtx:
    def __init__(self, home: Path, extra=None):
        self.tools={}; self.hooks=[]; self.engine=None; self.home=home; self.extra=extra or {}
    def get_config(self,key,default=None):
        overrides={"work_supervision_db":str(self.home/"work.db"),"remote_data_dir":str(self.home/"remote")}
        overrides.update(self.extra)
        return overrides.get(key,default)
    def register_tool(self,*,name,schema=None,handler=None,**kwargs): self.tools[name]=(schema,handler)
    def register_hook(self,name,callback): self.hooks.append((name,callback))
    def register_context_engine(self,engine): self.engine=engine

class RegistrationTests(unittest.TestCase):
    def load_plugin(self):
        spec=importlib.util.spec_from_file_location("hermes_nerve_plugin_root",ROOT/"__init__.py",submodule_search_locations=[str(ROOT)])
        mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod);return mod

    def test_legacy_registration_is_v023_contract(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ,{"HERMES_HOME":td,"HERMES_KANBAN_TASK":"","HERMES_KANBAN_TASK_ID":""},clear=False):
            mod=self.load_plugin();ctx=FakeCtx(Path(td))
            with patch.object(mod.work_runtime,"configure",wraps=mod.work_runtime.configure) as work_config:
                mod.register(ctx)
            self.assertEqual(VERSION,"0.3.0")
            self.assertEqual(set(ctx.tools),EXPECTED_TOOLS)
            self.assertNotIn("nerve_assistant",ctx.tools)
            self.assertEqual({n for n,_ in ctx.hooks},EXPECTED_HOOKS)
            self.assertIsNotNone(ctx.engine)
            self.assertIs(work_config.call_args.kwargs["nerve_auto_kill"],True)

            pre_llm=next(cb for name,cb in ctx.hooks if name=="pre_llm_call")
            with patch.object(mod.work_hooks,"pre_llm_call",return_value="work"), patch.object(mod.nervous,"pre_llm_call",return_value="nervous"):
                self.assertEqual(pre_llm(),"work")
            with patch.object(mod.work_hooks,"pre_llm_call",return_value=None), patch.object(mod.nervous,"pre_llm_call",return_value="nervous"):
                self.assertEqual(pre_llm(),"nervous")

            assess=ctx.tools["nerve_assess"][0]["parameters"]["properties"]["questions"]["additionalProperties"]["oneOf"]
            by_type={x["properties"]["type"]["enum"][0]:x for x in assess}
            self.assertEqual(by_type["choice"]["properties"]["criteria"]["minProperties"],2)
            self.assertEqual(by_type["score"]["properties"]["criteria"]["minItems"],2)

    def test_legacy_registration_honors_gate_mode_env_fallback(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ,{
            "HERMES_HOME":td,
            "HERMES_KANBAN_TASK":"",
            "HERMES_KANBAN_TASK_ID":"",
            "HERMES_NERVE_GATE_MODE":"ask-only",
        },clear=False):
            mod=self.load_plugin();ctx=FakeCtx(Path(td))
            mod.register(ctx)
            self.assertEqual(mod.gate.gate_mode(),"ask-only")

    def test_legacy_headless_zero_tools_and_success_log(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ,{
            "HERMES_HOME":td,"HERMES_KANBAN_TASK":"legacy-pin","HERMES_KANBAN_RUN_ID":"1","HERMES_KANBAN_CLAIM_LOCK":"claim"
        },clear=False):
            mod=self.load_plugin();ctx=FakeCtx(Path(td))
            identity=SimpleNamespace(task_id="legacy-pin",run_id=1,contract_hash="abcdef1234567890")
            with patch.object(mod.work_hooks,"bootstrap_kanban_worker",return_value=identity), self.assertLogs("hermes_nerve",level="INFO") as logs:
                mod.register(ctx)
            self.assertEqual(ctx.tools,{})
            self.assertEqual({n for n,_ in ctx.hooks},EXPECTED_HOOKS)
            self.assertIsNone(ctx.engine)
            self.assertTrue(any("headless supervision bound at startup" in line for line in logs.output))

if __name__=="__main__":unittest.main()