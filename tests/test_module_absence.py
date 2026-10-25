from __future__ import annotations
import importlib.util, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]

class Ctx:
    def __init__(self,home:Path,extra=None):
        self.home=home; self.extra=extra or {}; self.tools={}; self.hooks=[]; self.engine=None
    def get_config(self,key,default=None):
        values={"work_supervision_db":str(self.home/"work.db"),"remote_data_dir":str(self.home/"remote")}; values.update(self.extra); return values.get(key,default)
    def register_tool(self,*,name,schema=None,handler=None,**kwargs): self.tools[name]=(schema,handler)
    def register_hook(self,name,callback): self.hooks.append((name,callback))
    def register_context_engine(self,engine): self.engine=engine

def load_plugin():
    spec=importlib.util.spec_from_file_location("nerve_modular_test_root",ROOT/"__init__.py",submodule_search_locations=[str(ROOT)])
    mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod);return mod

class ModuleAbsenceTests(unittest.TestCase):
    def register(self, profile, extra=None):
        td=tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        env=patch.dict(os.environ,{"HERMES_HOME":td.name,"HERMES_KANBAN_TASK":"","HERMES_KANBAN_TASK_ID":""},clear=False); env.start(); self.addCleanup(env.stop)
        mod=load_plugin(); values={"nerve_profile":profile}; values.update(extra or {}); ctx=Ctx(Path(td.name),values); mod.register(ctx); return mod,ctx

    def test_lean_removes_qol_surfaces_and_hooks(self):
        mod,ctx=self.register("lean")
        self.assertIn("nerve_decide",ctx.tools); self.assertIn("nerve_supervise_card",ctx.tools); self.assertIn("nerve_nervous_event",ctx.tools)
        self.assertNotIn("nerve_rank",ctx.tools); self.assertNotIn("nerve_verify",ctx.tools); self.assertNotIn("nerve_assess",ctx.tools)
        self.assertNotIn("nerve_context_curate",ctx.tools); self.assertNotIn("nerve_context_explain",ctx.tools); self.assertNotIn("nerve_remote_delegate_task",ctx.tools); self.assertNotIn("nerve_assistant",ctx.tools)
        self.assertIsNone(ctx.engine)
        self.assertFalse(any(cb is mod.ledger.observe_tool_call for _,cb in ctx.hooks))
        self.assertFalse(any(cb is mod.gate.pre_tool_call for _,cb in ctx.hooks))

    def test_lean_runtime_defaults_cut_provider_admission_and_cap_calls(self):
        mod=load_plugin()
        td=tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        env=patch.dict(os.environ,{"HERMES_HOME":td.name,"HERMES_KANBAN_TASK":"","HERMES_KANBAN_TASK_ID":""},clear=False); env.start(); self.addCleanup(env.stop)
        ctx=Ctx(Path(td.name),{"nerve_profile":"lean"})
        with patch.object(mod.nervous,"configure",wraps=mod.nervous.configure) as conf:
            mod.register(ctx)
        kwargs=conf.call_args.kwargs
        self.assertFalse(kwargs["admission_enabled"])
        self.assertEqual(kwargs["max_provider_calls_per_turn"],12)
        self.assertEqual(kwargs["event_preview_chars"],800)
        self.assertEqual(kwargs["retain_recent_events"],32)

    def test_lean_runtime_defaults_allow_explicit_override(self):
        mod=load_plugin()
        td=tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        env=patch.dict(os.environ,{"HERMES_HOME":td.name,"HERMES_KANBAN_TASK":"","HERMES_KANBAN_TASK_ID":""},clear=False); env.start(); self.addCleanup(env.stop)
        ctx=Ctx(Path(td.name),{
            "nerve_profile":"lean",
            "nervous_turn_admission":True,
            "nervous_max_provider_calls_per_turn":21,
            "nervous_event_preview_chars":1000,
            "nervous_retain_recent_events":40,
        })
        with patch.object(mod.nervous,"configure",wraps=mod.nervous.configure) as conf:
            mod.register(ctx)
        kwargs=conf.call_args.kwargs
        self.assertTrue(kwargs["admission_enabled"])
        self.assertEqual(kwargs["max_provider_calls_per_turn"],21)
        self.assertEqual(kwargs["event_preview_chars"],1000)
        self.assertEqual(kwargs["retain_recent_events"],40)

    def test_lean_schema_surface_stays_token_bounded(self):
        _,ctx=self.register("lean")
        chars=sum(len(json.dumps(schema,separators=(",",":"),ensure_ascii=False)) for schema,_ in ctx.tools.values())
        self.assertLessEqual(chars,4500)

    def test_operator_removes_factory_and_remote_surfaces(self):
        _,ctx=self.register("operator")
        self.assertIn("nerve_context_curate",ctx.tools); self.assertIn("nerve_context_explain",ctx.tools); self.assertIn("nerve_nervous_event",ctx.tools)
        self.assertNotIn("nerve_supervise_card",ctx.tools); self.assertNotIn("nerve_remote_delegate_task",ctx.tools); self.assertNotIn("nerve_assistant",ctx.tools)
        names={n for n,_ in ctx.hooks}; self.assertNotIn("post_api_request",names); self.assertNotIn("api_request_error",names); self.assertIsNotNone(ctx.engine)

    def test_action_gate_profiles_are_actually_active_by_default(self):
        for profile in ("fat_cat","operator"):
            with self.subTest(profile=profile):
                mod,_=self.register(profile,{"remote_hosts":{}})
                self.assertEqual(mod.gate.gate_mode(),"advisory")

    def test_action_gate_profile_default_respects_explicit_off_override(self):
        mod,_=self.register("operator",{"gate_mode":"off"})
        self.assertEqual(mod.gate.gate_mode(),"off")

    def test_marie_kondo_is_minimal_and_has_no_nervous_or_context_hooks(self):
        mod,ctx=self.register("marie_kondo")
        self.assertIn("nerve_decide",ctx.tools); self.assertIn("nerve_supervise_card",ctx.tools); self.assertIn("nerve_stats",ctx.tools)
        self.assertNotIn("nerve_nervous_event",ctx.tools); self.assertNotIn("nerve_context_curate",ctx.tools); self.assertNotIn("nerve_remote_delegate_task",ctx.tools); self.assertNotIn("nerve_assistant",ctx.tools)
        self.assertIsNone(ctx.engine)
        self.assertFalse(any(cb is mod.nervous.post_tool_call for _,cb in ctx.hooks))
        self.assertFalse(any(cb is mod.ledger.observe_tool_call for _,cb in ctx.hooks))

    def test_fat_cat_uses_existing_event_tool_for_assistant_and_remote_is_conditional(self):
        _,ctx=self.register("fat_cat",{"remote_hosts":{}})
        self.assertIn("nerve_nervous_event",ctx.tools); self.assertIn("nerve_context_curate",ctx.tools)
        self.assertNotIn("nerve_assistant",ctx.tools); self.assertNotIn("nerve_remote_delegate_task",ctx.tools)
        self.assertIn("pre_llm_call",{n for n,_ in ctx.hooks})

    def test_explicit_module_override_wins(self):
        _,ctx=self.register("lean",{"nerve_modules":{"context_governor":True}})
        self.assertIn("nerve_context_curate",ctx.tools); self.assertIn("nerve_context_explain",ctx.tools); self.assertIsNotNone(ctx.engine)

    def test_assistant_loops_keep_shared_event_transport_when_nervous_is_off(self):
        _,ctx=self.register("lean",{"nerve_modules":{"nervous":False,"assistant_loops":True}})
        self.assertIn("nerve_nervous_event",ctx.tools)
        self.assertNotIn("nerve_context_curate",ctx.tools)

    def test_profile_shadow_backend_requires_shadow_testing_but_legacy_honors_existing_setting(self):
        # New profiles make shadow testing an explicit module cost. Legacy keeps the v0.2.3 setting verbatim.
        td=tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        env=patch.dict(os.environ,{"HERMES_HOME":td.name,"HERMES_KANBAN_TASK":"","HERMES_KANBAN_TASK_ID":""},clear=False); env.start(); self.addCleanup(env.stop)
        mod=load_plugin()
        legacy=Ctx(Path(td.name),{"reflex_backend":"shadow"})
        with patch.object(mod.reflex,"configure",wraps=mod.reflex.configure) as conf:
            mod.register(legacy)
        self.assertEqual(conf.call_args.kwargs["backend"],"shadow")
        lean=Ctx(Path(td.name),{"nerve_profile":"lean","reflex_backend":"shadow"})
        with patch.object(mod.reflex,"configure",wraps=mod.reflex.configure) as conf:
            mod.register(lean)
        self.assertEqual(conf.call_args.kwargs["backend"],"jev")

    def test_operator_work_supervision_off_creates_no_database(self):
        td=tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        env=patch.dict(os.environ,{"HERMES_HOME":td.name,"HERMES_KANBAN_TASK":"","HERMES_KANBAN_TASK_ID":""},clear=False); env.start(); self.addCleanup(env.stop)
        db=Path(td.name)/"work.db"
        mod=load_plugin(); ctx=Ctx(Path(td.name),{"nerve_profile":"operator","work_supervision_db":str(db)})
        mod.register(ctx)
        self.assertFalse(db.exists())

    def test_fat_cat_ignores_old_unmarked_profile_disable_but_respects_operator_disable(self):
        td=tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        env=patch.dict(os.environ,{"HERMES_HOME":td.name,"HERMES_KANBAN_TASK":"","HERMES_KANBAN_TASK_ID":""},clear=False); env.start(); self.addCleanup(env.stop)
        mod=load_plugin(); a=mod.assistant
        a._write(a._settings_path(),{"schema":1,"enabled":False})
        ctx=Ctx(Path(td.name),{"nerve_profile":"fat_cat","remote_hosts":{}}); mod.register(ctx)
        self.assertTrue(a.enabled())
        a.disable(); ctx2=Ctx(Path(td.name),{"nerve_profile":"fat_cat","remote_hosts":{}}); mod.register(ctx2)
        self.assertFalse(a.enabled())

    def test_disabled_work_runtime_supervisor_cannot_lazy_wake_store(self):
        from hermes_nerve.work import runtime
        with tempfile.TemporaryDirectory() as td:
            db=Path(td)/"work.db"
            runtime.configure(enabled=False,store_path=str(db))
            with self.assertRaisesRegex(RuntimeError,"disabled"):
                runtime.supervisor()
            self.assertFalse(db.exists())

    def test_profile_switch_changes_registered_features(self):
        """Phase 7 profile switching at activation level: re-registering the
        plugin after a profile change must produce a different feature
        surface, driven by the saved sidecar (the single authority)."""
        td=tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        env=patch.dict(os.environ,{"HERMES_HOME":td.name,"HERMES_KANBAN_TASK":"","HERMES_KANBAN_TASK_ID":""},clear=False); env.start(); self.addCleanup(env.stop)
        mod=load_plugin()
        ctx1=Ctx(Path(td.name),{"nerve_profile":"operator"}); mod.register(ctx1)
        self.assertIn("nerve_context_curate",ctx1.tools)
        self.assertNotIn("nerve_supervise_card",ctx1.tools)
        from hermes_nerve.profiles import save_profile
        save_profile({"version":1,"nerve_profile":"fat_cat","nerve_modules":{},"advanced":{}},Path(td.name))
        ctx2=Ctx(Path(td.name),{}); mod.register(ctx2)
        self.assertIn("nerve_supervise_card",ctx2.tools)
        self.assertNotIn("nerve_assistant",ctx2.tools)
        # And back: operator sidecar removes the supervision surface again.
        save_profile({"version":1,"nerve_profile":"operator","nerve_modules":{},"advanced":{}},Path(td.name))
        ctx3=Ctx(Path(td.name),{}); mod.register(ctx3)
        self.assertNotIn("nerve_supervise_card",ctx3.tools)
        self.assertIn("nerve_context_curate",ctx3.tools)
