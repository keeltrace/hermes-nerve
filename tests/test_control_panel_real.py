"""Live-wire contract tests; no paid calls, downloads, or model installs in CI."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, build_opener, ProxyHandler
import json
import tempfile
import unittest

from hermes_nerve.control_panel import ControlPanel, PanelError
from hermes_nerve.local_backend_setup import preview, launch, LocalSetupError, status
from hermes_nerve.reflex.d1_service import Service


class SystemOneFixture(BaseHTTPRequestHandler):
    calls = 0
    fail_decision = False
    def log_message(self, *args):
        pass
    def do_GET(self):
        raw = json.dumps({"status":"ok", "model":"LiquidAI/d1-3B"}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)
    def do_POST(self):
        SystemOneFixture.calls += 1
        length = int(self.headers.get("Content-Length","0"))
        incoming = json.loads(self.rfile.read(length))
        if SystemOneFixture.fail_decision:
            self.send_response(404); self.end_headers(); return
        if not incoming.get("questions", {}).get("setup_probe"):
            self.send_response(422); self.end_headers(); return
        raw = json.dumps({
            "model": "LiquidAI/d1-3B",
            "answers": {"setup_probe": {"type":"choice", "choice":"billing",
                        "confidence":.97, "probabilities":{"billing":.985, "technical":.015}}},
            "usage": {"input_tokens":54, "output_tokens":0},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers(); self.wfile.write(raw)


class WireTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.panel=ControlPanel(home=Path(self.tmp.name))
        SystemOneFixture.calls=0; SystemOneFixture.fail_decision=False
        self.server=ThreadingHTTPServer(("127.0.0.1",0),SystemOneFixture)
        self.thread=Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url="http://127.0.0.1:"+str(self.server.server_port)

    def test_health_does_not_masquerade_as_model_inference(self):
        check=self.panel.test_backend({"backend":"d1","base_url":self.url,"mode":"health"})
        self.assertTrue(check["reachable"])
        self.assertFalse(check["verified"])
        self.assertFalse(check["ok"])
        self.assertEqual(check["status"],"reachable_only")
        self.assertEqual(SystemOneFixture.calls,0)

    def test_real_inference_uses_systemone_wire_and_validates_answer(self):
        real=self.panel.test_backend({"backend":"d1","base_url":self.url,"mode":"live"})
        self.assertTrue(real["ok"],real)
        self.assertTrue(real["verified"])
        self.assertEqual(real["status"],"inference_passed")
        self.assertEqual(real["result"]["choice"],"billing")
        self.assertEqual(real["usage"]["output_tokens"],0)
        self.assertEqual(SystemOneFixture.calls,1)

    def test_health_only_service_fails_real_inference(self):
        SystemOneFixture.fail_decision=True
        check=self.panel.test_backend({"backend":"d1","base_url":self.url,"mode":"health"})
        real=self.panel.test_backend({"backend":"d1","base_url":self.url,"mode":"live"})
        self.assertTrue(check["reachable"])
        self.assertFalse(real["verified"])
        self.assertEqual(real["status"],"inference_failed")
        self.assertEqual(SystemOneFixture.calls,1)

    def test_unexpected_identity_rejected(self):
        with patch.object(self.panel,"read", wraps=self.panel.read) as read:
            cfg=self.panel.read()
            def get(key,default=None):
                if key=="reflex_d1_expected_identity":return "Q4-required"
                return default
            cfg[3].get_config=get
            read.return_value=cfg
            outcome=self.panel.test_backend({"backend":"d1","base_url":self.url,"mode":"live"})
        self.assertFalse(outcome["verified"])

    def test_rejects_local_url_attacks(self):
        candidates=("http://evil.net:8080","http://127.0.0.1:8080@evil.net:80",
                    "http://127.0.0.1:1234/x","http://127.0.0.1:99999",
                    "file:///etc/passwd","http://127.0.0.1:8080?x=y")
        for url in candidates:
            with self.subTest(url=url), self.assertRaises(PanelError):
                self.panel.test_backend({"backend":"d1","base_url":url,"mode":"live"})

    def test_shadow_requires_both_real_results(self):
        def answer(b,**_):
            if b=="jev":return {"ok":True,"verified":True,"message":"hosted ready"}
            return {"ok":False,"verified":False,"message":"local unavailable"}
        with patch.object(self.panel,"_backend_probe",side_effect=answer) as call:
            check=self.panel.test_backend({"backend":"shadow","compare_backend":"d1","mode":"live"})
        self.assertEqual(call.call_count,2)
        self.assertFalse(check["verified"])
        self.assertEqual(check["status"],"comparison_not_verified")

    def test_hosted_inference_actually_calls_client_only_in_live_mode(self):
        resp=SimpleNamespace(answers={"setup_probe":{"choice":"billing","confidence":.9}},
                             model="jev-model",usage={"input_tokens":5},latency_ms=24.0)
        with patch("hermes_nerve.client.JevClient") as client:
            client.return_value.system_one.return_value=resp
            status_check=self.panel.test_backend({"backend":"jev","provider":"typesafe","mode":"health"})
            self.assertFalse(status_check["verified"])
            self.assertEqual(client.call_count,0)
            live=self.panel.test_backend({"backend":"jev","provider":"typesafe","mode":"live"})
            self.assertTrue(live["verified"])
            self.assertEqual(client.return_value.system_one.call_count,1)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)
        self.panel=ControlPanel(home=self.home)

    def test_up_to_date_install_does_not_offer_copy_onto_itself(self):
        existing=self.home/"plugins"/"nerve"
        existing.mkdir(parents=True)
        (existing/"plugin.yaml").write_text("name: nerve\n")
        (existing/"__init__.py").write_text("# loader\n")
        (existing/"hermes_nerve").mkdir()
        source=ControlPanel(home=self.home,source=existing)
        with patch("hermes_nerve.control_panel.shutil.which",return_value="/fake/hermes"):
            result=source.install_preview()
            self.assertTrue(result["already_installed"])
            self.assertEqual(result["destination"],str(existing))
            with self.assertRaises(PanelError):
                source.install(result)

    def test_install_uses_correct_existing_plugin_directory(self):
        for name in ("nerve","hermes-nerve"):
            target=self.home/"plugins"/name
            target.mkdir(parents=True)
            (target/"plugin.yaml").write_text("name: nerve\n")
        self.assertEqual(self.panel.installed_path().name,"nerve")

    def test_local_preview_discloses_packages_disksize_and_model_code(self):
        fake_disk=SimpleNamespace(free=36*(1024**3))
        with patch("hermes_nerve.local_backend_setup.shutil.disk_usage",return_value=fake_disk), \
             patch("hermes_nerve.local_backend_setup._python_path",return_value="/usr/bin/python3.12"), \
             patch("hermes_nerve.local_backend_setup._port_busy",return_value=False):
            plan=preview(self.home,"d1")
            self.assertTrue(plan["can_install"])
            self.assertTrue(plan["requires_remote_code_consent"])
            self.assertIn("6.25",plan["download_note"])
            self.assertFalse(preview(self.home,"openjev")["supported"])
            with self.assertRaises(LocalSetupError):
                launch(self.home,"d1",{"backend":"d1","plan_id":plan["plan_id"],
                                      "approve_downloads":True},Path(__file__).resolve().parents[1])
            with patch("hermes_nerve.local_backend_setup.subprocess.Popen",return_value=SimpleNamespace(pid=12345)) as popen:
                done=launch(self.home,"d1",{"backend":"d1","plan_id":plan["plan_id"],
                                    "approve_downloads":True,"approve_remote_code":True},
                                    Path(__file__).resolve().parents[1])
                self.assertEqual(done["phase"],"queued")
                self.assertTrue(popen.called)
                self.assertNotIn("shell",popen.call_args.kwargs)
                self.assertEqual(status(self.home,"d1")["phase"],"queued")

    def test_install_is_blocked_when_port_in_use(self):
        with patch("hermes_nerve.local_backend_setup._port_busy",return_value=True):
            p=preview(self.home,"laya")
            self.assertFalse(p["can_install"])
            self.assertTrue(p["port_busy"])


class FakeD1:
    def system_one(self,state,questions):
        return {"answers": {name: {"choice":"billing","confidence":.9} for name in questions},
                "usage":{"output_tokens":0}}


class D1ServiceTests(unittest.TestCase):
    def setUp(self):
        self.server=Service(("127.0.0.1",0),FakeD1())
        self.thread=Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url="http://127.0.0.1:"+str(self.server.server_port)
        self.opener=build_opener(ProxyHandler({}))

    def test_service_exposes_health_and_real_inference_with_no_model_download(self):
        with self.opener.open(self.url+"/healthz",timeout=3) as response:
            health=json.load(response)
        self.assertEqual(health["model"],"LiquidAI/d1-3B")
        query={"model":"LiquidAI/d1-3B","state":"refund","questions":
               {"probe":{"type":"choice","instructions":"Route","criteria":
                          {"billing":"Payments","technical":"Bugs"}}}}
        req=Request(self.url+"/v1/systemone",data=json.dumps(query).encode(),method="POST",
                    headers={"Content-Type":"application/json"})
        with self.opener.open(req,timeout=3) as response:out=json.load(response)
        self.assertEqual(out["answers"]["probe"]["choice"],"billing")
        self.assertEqual(out["usage"]["output_tokens"],0)


if __name__ == "__main__":
    unittest.main()
