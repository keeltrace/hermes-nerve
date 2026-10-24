"""Contract tests for the opt-in, local-only Nerve UI."""
import json
import os
import subprocess
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hermes_nerve.control_panel import ControlPanel, PanelError
from hermes_nerve.control_panel_server import make_handler, serve
from hermes_nerve.profiles import save_profile, load_profile


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.panel = ControlPanel(home=self.home)

    def test_new_setup_defaults_operator_without_writing(self):
        before = self.panel.status()
        self.assertTrue(before["fresh"])
        self.assertEqual(before["suggested_profile"], "operator")
        self.assertEqual(before["current"]["profile"], "legacy")
        self.assertFalse((self.home / "nerve" / "profile.json").exists())
        self.assertIn("verified_experience", {m["id"] for m in before["modules"]})

    def test_status_exposes_read_only_runtime_identity(self):
        identity = self.panel.status()["runtime_identity"]
        self.assertIn(identity["status"], {"MATCH", "DRIFT", "UNKNOWN"})
        self.assertRegex(identity["source_tree_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(identity["module_roots"])

    def test_explicit_legacy_stays_legacy(self):
        save_profile({"version":1,"nerve_profile":"legacy","nerve_modules":{},"advanced":{}},self.home)
        info = self.panel.status()
        self.assertEqual(info["suggested_profile"],"legacy")
        self.assertFalse(info["fresh"])
        self.assertEqual(info["current"]["profile"],"legacy")

    def test_preview_apply_and_restore_exact_previous_bytes(self):
        start = {"version":1,"nerve_profile":"lean","nerve_modules":{"action_gate":False},"advanced":{}}
        save_profile(start, self.home)
        path = self.home / "nerve" / "profile.json"
        old = path.read_bytes()
        req = {"profile":"operator","overrides":{"selective_context_skills":False},
               "advanced":{"reflex_backend":"laya"}}
        preview = self.panel.preview(req)
        self.assertFalse(preview["modules"]["selective_context_skills"])
        self.assertTrue(preview["modules"]["verified_experience"])
        changed = self.panel.apply({**req, "preview_id":preview["preview_id"]})
        self.assertTrue(changed["restart_required"])
        self.assertEqual(load_profile(self.home)["nerve_profile"],"operator")
        restored = self.panel.rollback({"id":changed["rollback_id"]})
        self.assertTrue(restored["ok"])
        self.assertEqual(path.read_bytes(),old)
        self.assertTrue(self.panel.snapshots())

    def test_disable_parent_prunes_children(self):
        req = {"profile":"operator","overrides":{"selective_context":False},
               "advanced":{}}
        data = self.panel.preview(req)
        for key in ("selective_context","selective_context_memory",
                    "selective_context_tools","selective_context_skills"):
            self.assertFalse(data["modules"][key])

    def test_preview_token_expires_on_change(self):
        req = {"profile":"operator","overrides":{},"advanced":{}}
        before = self.panel.preview(req)
        save_profile({"version":1,"nerve_profile":"lean","nerve_modules":{},"advanced":{}}, self.home)
        with self.assertRaisesRegex(PanelError,"changed since preview"):
            self.panel.apply({**req,"preview_id":before["preview_id"]})

    def test_rejects_unknown_modules_and_advanced_fields(self):
        with self.assertRaises(PanelError):
            self.panel.preview({"profile":"operator","overrides":{"bad_module":True},"advanced":{}})
        with self.assertRaises(PanelError):
            self.panel.preview({"profile":"operator","overrides":{},
                                "advanced":{"reflex_d1_token":"do-not-show"}})
        with self.assertRaises(PanelError):
            self.panel.preview({"profile":"operator","overrides":{},
                                "advanced":{"selective_context_tools_mode":17}})

    def test_external_profile_pin_is_surfaced_not_fatal(self):
        # 0.4.13 config-repair: a stale config.yaml nerve_profile no longer
        # blocks saving (the saved Nerve profile wins at runtime); the preview
        # proceeds and carries an explicit conflict note instead.
        with patch.object(self.panel,"host_settings",return_value=({"nerve_profile":"lean"},[])):
            preview = self.panel.preview({"profile":"operator","overrides":{},"advanced":{}})
            self.assertTrue(preview.get("conflict_note"))
            self.assertIn("lean", preview["conflict_note"])
            self.assertEqual(preview["profile"], "operator")

    def test_existing_advanced_secrets_do_not_leak_into_status(self):
        save_profile({"version":1,"nerve_profile":"operator","nerve_modules":{},
                      "advanced":{"reflex_d1_token":"private-value",
                                  "reflex_backend":"d1"}},self.home)
        status = json.dumps(self.panel.status())
        self.assertNotIn("private-value",status)
        self.assertNotIn("reflex_d1_token",status)
        self.assertEqual(self.panel.status()["current"]["backend"],"d1")

    def test_nested_credentials_are_not_sent_to_browser(self):
        save_profile({"version":1,"nerve_profile":"operator","nerve_modules":{},
                      "advanced":{"remote_hosts":{"local":{"host":"127.0.0.1",
                         "password":"never-in-browser","api_key":"never-in-browser"}}}},self.home)
        raw=json.dumps(self.panel.status())
        self.assertNotIn("never-in-browser",raw)
        self.assertNotIn("password",raw)

    def test_apply_preserves_unsubmitted_private_advanced_settings(self):
        private={"remote_hosts":{"gpu":{"host":"localhost","password":"sensitive"}},
                 "reflex_d1_token":"another-private-value"}
        save_profile({"version":1,"nerve_profile":"operator","nerve_modules":{},
                      "advanced":private},self.home)
        request={"profile":"operator","overrides":{"selective_context_tools":False},
                 "advanced":{}}
        preview=self.panel.preview(request)
        self.panel.apply({**request,"preview_id":preview["preview_id"]})
        stored=load_profile(self.home)
        self.assertEqual(stored["advanced"],private)

    def test_advanced_setting_can_be_cleared_without_resetting_secrets(self):
        save_profile({"version":1,"nerve_profile":"operator","nerve_modules":{},
                      "advanced":{"reflex_backend":"d1","reflex_d1_token":"private"}},self.home)
        request={"profile":"operator","overrides":{},"advanced":{},
                 "clear_advanced":["reflex_backend"]}
        preview=self.panel.preview(request)
        self.assertEqual(preview["backend"],"jev")
        self.panel.apply({**request,"preview_id":preview["preview_id"]})
        stored=load_profile(self.home)
        self.assertNotIn("reflex_backend",stored["advanced"])
        self.assertEqual(stored["advanced"]["reflex_d1_token"],"private")

    def test_connection_check_does_not_call_paid_provider(self):
        result = self.panel.test_backend({"backend":"jev","provider":"openrouter"})
        self.assertEqual(result["status"],"not_tested")
        with self.assertRaisesRegex(PanelError,"Only HTTP loopback"):
            self.panel.test_backend({"backend":"d1","base_url":"http://example.net:8080"})
        with self.assertRaisesRegex(PanelError,"Only HTTP loopback"):
            self.panel.test_backend({"backend":"d1","base_url":"http://127.0.0.1:8080@evil.net:80"})

    def test_install_requires_preview_and_preserves_old_copy(self):
        dest = self.home / "plugins" / "hermes-nerve"
        dest.mkdir(parents=True)
        (dest/"plugin.yaml").write_text("name: nerve\nversion: old\n")
        (dest/"keep.txt").write_text("previous version")
        with patch("hermes_nerve.control_panel.shutil.which",return_value="/fake/hermes"):
            with patch("hermes_nerve.control_panel.subprocess.run") as run:
                run.return_value.returncode=0
                proposal=self.panel.install_preview()
                with self.assertRaises(PanelError):
                    self.panel.install({"preview_id":"wrong"})
                done=self.panel.install({"preview_id":proposal["preview_id"]})
        self.assertTrue(done["ok"])
        self.assertTrue((dest/"hermes_nerve"/"cli.py").exists())
        self.assertEqual((Path(done["backup"])/"keep.txt").read_text(),"previous version")

    def test_installed_wheel_registration_assets_are_discovered(self):
        share = self.home / "fakeprefix" / "share" / "hermes-nerve"
        share.mkdir(parents=True)
        (share / "plugin.yaml").write_text("name: nerve\\n")
        (share / "__init__.py").write_text("x=1\\n")
        absent = self.home / "fakeprefix" / "site-packages"
        absent.mkdir(parents=True)
        panel = ControlPanel(home=self.home, source=absent)
        with patch("hermes_nerve.control_panel.sysconfig.get_path", return_value=str(self.home / "fakeprefix")):
            registration, package = panel.installation_sources()
        self.assertEqual(registration, share)
        self.assertTrue((package / "web" / "app.js").is_file())

    def test_first_install_initializes_operator(self):
        with patch("hermes_nerve.control_panel.shutil.which", return_value="/fake/hermes"):
            with patch("hermes_nerve.control_panel.subprocess.run") as run:
                run.return_value.returncode = 0
                before = self.panel.install_preview()
                self.assertTrue(before["initialize_operator"])
                result = self.panel.install({"preview_id": before["preview_id"]})
        self.assertTrue(result["profile_initialized"])
        self.assertEqual(load_profile(self.home)["nerve_profile"], "operator")

    def test_failed_fresh_install_preserves_unconfigured_legacy(self):
        with patch("hermes_nerve.control_panel.shutil.which", return_value="/fake/hermes"):
            with patch("hermes_nerve.control_panel.subprocess.run") as run:
                run.return_value.returncode = 1
                proposal = self.panel.install_preview()
                with self.assertRaises(PanelError):
                    self.panel.install({"preview_id": proposal["preview_id"]})
        self.assertIsNone(load_profile(self.home))
        self.assertFalse((self.home / "plugins" / "hermes-nerve").exists())

    def test_staging_failure_never_deletes_existing_installation(self):
        dest=self.home / "plugins" / "hermes-nerve"
        dest.mkdir(parents=True)
        (dest / "plugin.yaml").write_text("name: nerve" + chr(10))
        (dest / "keep.txt").write_text("must survive")
        with patch("hermes_nerve.control_panel.shutil.which",return_value="/fake/hermes"):
            with patch("hermes_nerve.control_panel.shutil.copytree",side_effect=OSError("copy failed")):
                proposal=self.panel.install_preview()
                with self.assertRaisesRegex(PanelError,"Install failed"):
                    self.panel.install({"preview_id":proposal["preview_id"]})
        self.assertEqual((dest / "keep.txt").read_text(),"must survive")

    def test_failed_plugin_enable_restores_previous_copy(self):
        dest = self.home / "plugins" / "hermes-nerve"
        dest.mkdir(parents=True)
        (dest/"plugin.yaml").write_text("name: nerve\nversion: old\n")
        (dest/"important.txt").write_text("original")
        with patch("hermes_nerve.control_panel.shutil.which",return_value="/fake/hermes"):
            with patch("hermes_nerve.control_panel.subprocess.run") as run:
                # Admission succeeds; this must still reach the enable-failure
                # recovery path, rather than accidentally testing rejection.
                run.side_effect = lambda args, **kwargs: subprocess.CompletedProcess(
                    args, 0 if "validate" in args else 1, stdout="", stderr="")
                proposal=self.panel.install_preview()
                with self.assertRaisesRegex(PanelError,"Install failed"):
                    self.panel.install({"preview_id":proposal["preview_id"]})
        native_calls = [call.args[0] for call in run.call_args_list if "plugins" in call.args[0]]
        self.assertEqual([args[-2] for args in native_calls], ["validate", "enable"])
        self.assertEqual((dest/"important.txt").read_text(),"original")
        self.assertEqual((dest/"plugin.yaml").read_text(),"name: nerve\nversion: old\n")

    def test_host_binding_cannot_be_public(self):
        with self.assertRaisesRegex(ValueError,"127.0.0.1"):
            serve(host="0.0.0.0",open_browser=False,panel=self.panel)


class HttpSecurityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.panel = ControlPanel(home=Path(temp.name))
        self.token = "test-session-token"
        self.server = ThreadingHTTPServer(("127.0.0.1",0),make_handler(self.panel,self.token))
        self.thread=Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, *, token=None, origin=None, body=None, host=None):
        address="http://127.0.0.1:"+str(self.server.server_port)+path
        headers={}
        if token is not None: headers["X-Nerve-Token"]=token
        if origin is not None: headers["Origin"]=origin
        if host is not None: headers["Host"]=host
        data=json.dumps(body).encode() if body is not None else None
        if data is not None: headers["Content-Type"]="application/json"
        return urlopen(Request(address,data=data,headers=headers,method="POST" if data else "GET"),timeout=3)

    def test_static_ui_and_authenticated_status(self):
        with self.request("/") as resp:
            self.assertEqual(resp.status,200)
            self.assertIn(b"NERVE",resp.read())
            self.assertIn("default-src",resp.headers["Content-Security-Policy"])
        with self.request("/api/status",token=self.token) as resp:
            self.assertEqual(json.load(resp)["suggested_profile"],"operator")

    def test_rejects_unauthorized_and_cross_origin_writes(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/api/status")
        self.assertEqual(error.exception.code,403)
        body={"profile":"operator","overrides":{},"advanced":{}}
        with self.assertRaises(HTTPError) as error:
            self.request("/api/preview",token=self.token,origin="http://evil.example",body=body)
        self.assertEqual(error.exception.code,403)

    def test_browser_can_apply_and_restore_after_preview(self):
        origin="http://127.0.0.1:"+str(self.server.server_port)
        body={"profile":"operator","overrides":{"selective_context_skills":False},"advanced":{}}
        with self.request("/api/preview",token=self.token,origin=origin,body=body) as resp:
            preview=json.load(resp)
        with self.request("/api/apply",token=self.token,origin=origin,
                          body={**body,"preview_id":preview["preview_id"]}) as resp:
            done=json.load(resp)
        self.assertTrue(done["ok"])
        with self.request("/api/status",token=self.token) as resp:
            self.assertEqual(json.load(resp)["current"]["profile"],"operator")
        with self.request("/api/rollback",token=self.token,origin=origin,
                          body={"id":done["rollback_id"]}) as resp:
            self.assertTrue(json.load(resp)["ok"])
        with self.request("/api/status",token=self.token) as resp:
            self.assertTrue(json.load(resp)["fresh"])

    def test_preview_endpoint_and_host_rebinding_guard(self):
        origin="http://127.0.0.1:"+str(self.server.server_port)
        body={"profile":"operator","overrides":{},"advanced":{}}
        with self.request("/api/preview",token=self.token,origin=origin,body=body) as resp:
            self.assertEqual(json.load(resp)["profile"],"operator")
        with self.assertRaises(HTTPError) as error:
            self.request("/api/status",token=self.token,host="evil.example")
        self.assertEqual(error.exception.code,403)


if __name__ == "__main__":
    unittest.main()
