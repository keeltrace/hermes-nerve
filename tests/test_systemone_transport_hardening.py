"""Regression tests for local System One transport egress hardening.

The local System One/D1 transport can carry decision state and inlined local
image bytes. It must never be proxied through environment proxies and must
never follow a redirect off the loopback endpoint. These tests use only
127.0.0.1 loopback servers and synthetic responses.
"""

from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hermes_nerve.reflex import systemone
from hermes_nerve.reflex.systemone import SystemOneHTTPClient, SystemOneHTTPError


class _OKHandler(BaseHTTPRequestHandler):
    hits = 0

    def do_POST(self):
        type(self).hits += 1
        body = json.dumps({"answers": {"q": {"type": "noul", "noul": 0.5}}, "usage": {}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class _RedirectHandler(BaseHTTPRequestHandler):
    redirect_to = ""

    def do_POST(self):
        self.send_response(302)
        self.send_header("Location", self.redirect_to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        return


def _serve(handler) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class SystemOneTransportHardeningTests(unittest.TestCase):
    def test_opener_disables_proxies_and_redirects(self) -> None:
        handlers = getattr(systemone._OPENER, "handlers", [])
        # ProxyHandler({}) registers no proxy_open methods, so no environment
        # proxy handler is active on the local transport.
        active_proxies = [
            h
            for h in handlers
            if isinstance(h, urllib.request.ProxyHandler) and getattr(h, "proxies", None)
        ]
        self.assertEqual(active_proxies, [])
        redirects = [h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertTrue(redirects)
        self.assertIsInstance(redirects[0], systemone._NoRedirectHandler)

    def test_redirect_is_refused_and_target_never_contacted(self) -> None:
        _OKHandler.hits = 0
        target = _serve(_OKHandler)
        _RedirectHandler.redirect_to = f"http://127.0.0.1:{target.server_address[1]}/evil"
        redirector = _serve(_RedirectHandler)
        try:
            client = SystemOneHTTPClient(base_url=f"http://127.0.0.1:{redirector.server_address[1]}")
            with self.assertRaises(SystemOneHTTPError) as ctx:
                client.system_one(state="x", questions={"q": {"type": "noul", "instructions": "y?"}})
            self.assertIn("redirect", str(ctx.exception).lower())
            self.assertEqual(_OKHandler.hits, 0)
        finally:
            redirector.shutdown()
            redirector.server_close()
            target.shutdown()
            target.server_close()

    def test_environment_proxy_is_ignored_for_loopback(self) -> None:
        _OKHandler.hits = 0
        server = _serve(_OKHandler)
        keys = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")
        old = {key: os.environ.get(key) for key in keys}
        try:
            os.environ["http_proxy"] = "http://127.0.0.1:1"
            os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
            client = SystemOneHTTPClient(base_url=f"http://127.0.0.1:{server.server_address[1]}")
            response = client.system_one(state="x", questions={"q": {"type": "noul", "instructions": "y?"}})
            self.assertEqual(_OKHandler.hits, 1)
            self.assertEqual(response.answers["q"]["noul"], 0.5)
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
