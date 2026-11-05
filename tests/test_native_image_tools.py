"""Regression tests for local-only image input on the native decision tools.

These exercise the Hermes-visible handler seam (``hermes_nerve.tools``) plus the
schema contract. All image fixtures are synthetic bytes generated at runtime; no
real user images, downloads, or remote URLs are involved.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve import privacy, receipts, schemas, tools
from hermes_nerve.engine import DecisionEngine
from hermes_nerve.jsonl import read_jsonl
from hermes_nerve.reflex.shadow import ShadowProvider
from hermes_nerve.reflex.systemone import D1Client, SystemOneHTTPError

PNG_SIG = b"\x89PNG\r\n\x1a\n"
JPEG_SIG = b"\xff\xd8\xff\xe0"
WEBP_SIG = b"RIFF\x00\x00\x00\x00WEBP"


def _answers(payload: dict) -> dict:
    answers: dict = {}
    for name, question in payload["questions"].items():
        qtype = question.get("type")
        if qtype == "choice":
            labels = list(question["criteria"])
            answers[name] = {
                "type": "choice",
                "choice": labels[0],
                "probabilities": {labels[0]: 0.9, labels[1]: 0.1},
                "confidence": 0.9,
            }
        elif qtype == "noul":
            answers[name] = {"type": "noul", "noul": 0.8}
        else:
            answers[name] = {"type": qtype}
    return {
        "id": "local-img-1",
        "model": "LiquidAI/d1-2b",
        "provider": "Liquid D1",
        "answers": answers,
        "usage": {"input_tokens": 5, "output_tokens": 0},
    }


def _capable_provider(captured: list) -> D1Client:
    def transport(url, headers, body, timeout):
        payload = json.loads(body)
        captured.append(payload)
        return 200, json.dumps(_answers(payload)).encode(), {}

    return D1Client(transport=transport)


class _UnsupportedProvider:
    """Provider without ``supports_images``; must never be reached with images."""

    def system_one(self, *, state, questions, model=None):
        raise AssertionError("unsupported provider must not be called")


def _write_image(path: Path, magic: bytes, total: int = 64) -> bytes:
    body = magic + b"\x00" * max(0, total - len(magic))
    path.write_bytes(body)
    return body


class NativeImageToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.receipts = self.home / "receipts.jsonl"
        self._old_env = {
            "HERMES_HOME": os.environ.get("HERMES_HOME"),
            "HERMES_NERVE_RECEIPTS": os.environ.get("HERMES_NERVE_RECEIPTS"),
        }
        os.environ["HERMES_HOME"] = str(self.home)
        os.environ["HERMES_NERVE_RECEIPTS"] = str(self.receipts)
        receipts.configure(enabled=True, detail="hash")
        self._old_factory = tools._engine_factory

    def tearDown(self) -> None:
        tools._engine_factory = self._old_factory
        receipts.configure(enabled=True, detail=None)
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _use_provider(self, provider) -> None:
        tools._engine_factory = lambda: DecisionEngine(provider)

    def _records(self) -> list:
        return read_jsonl(self.receipts) if self.receipts.exists() else []

    # -- schemas -----------------------------------------------------------

    def test_schemas_expose_local_only_image_paths(self) -> None:
        for schema in (schemas.NERVE_DECIDE, schemas.NERVE_RANK, schemas.NERVE_ASSESS):
            params = schema["parameters"]
            self.assertIn("image_paths", params["properties"], schema["name"])
            self.assertNotIn("image_paths", params.get("required", []), schema["name"])
            image = params["properties"]["image_paths"]
            self.assertEqual(image["type"], "array")
            self.assertEqual(image["items"]["type"], "string")
            self.assertGreaterEqual(image["maxItems"], 1)
            desc = image["description"].lower()
            for needle in ("local", "loopback", "systemone", "d1", "shadow", "hosted"):
                self.assertIn(needle, desc, (schema["name"], needle))

    # -- default (no image) behavior --------------------------------------

    def test_no_image_paths_leaves_text_behavior_unchanged(self) -> None:
        captured: list = []
        self._use_provider(_capable_provider(captured))
        payload = json.loads(
            tools.nerve_decide(
                {"state": {"x": 1}, "instructions": "Choose", "choices": ["A", "B"]}
            )
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(len(captured), 1)
        self.assertNotIn("images", captured[0])
        self.assertEqual(captured[0]["state"], {"x": 1})
        record = self._records()[-1]
        self.assertEqual(record["state_sha256"], privacy.canonical_hash({"x": 1}))

    # -- local images ------------------------------------------------------

    def test_local_png_loaded_into_inline_data_url(self) -> None:
        captured: list = []
        self._use_provider(_capable_provider(captured))
        image = self.home / "screen.png"
        raw = _write_image(image, PNG_SIG)
        payload = json.loads(
            tools.nerve_decide(
                {
                    "state": {"screen": "confirm"},
                    "instructions": "Choose",
                    "choices": ["ALLOW", "BLOCK"],
                    "image_paths": [str(image)],
                }
            )
        )
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(
            captured[0]["images"], ["data:image/png;base64," + base64.b64encode(raw).decode()]
        )
        # Tool result (which enters the model context) carries no bytes or path.
        text = json.dumps(payload)
        self.assertNotIn(base64.b64encode(raw).decode(), text)
        self.assertNotIn(str(image), text)

    def test_jpeg_and_webp_formats_are_accepted(self) -> None:
        captured: list = []
        self._use_provider(_capable_provider(captured))
        jpeg = self.home / "a.jpg"
        webp = self.home / "b.webp"
        _write_image(jpeg, JPEG_SIG)
        _write_image(webp, WEBP_SIG)
        payload = json.loads(
            tools.nerve_decide(
                {
                    "state": {},
                    "instructions": "Choose",
                    "choices": ["A", "B"],
                    "image_paths": [str(jpeg), str(webp)],
                }
            )
        )
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(captured[0]["images"][0].startswith("data:image/jpeg;base64,"))
        self.assertTrue(captured[0]["images"][1].startswith("data:image/webp;base64,"))

    def test_image_only_state_none_normalized_to_empty_string(self) -> None:
        captured: list = []
        self._use_provider(_capable_provider(captured))
        image = self.home / "only.png"
        _write_image(image, PNG_SIG)
        payload = json.loads(
            tools.nerve_decide(
                {
                    "state": None,
                    "instructions": "Choose",
                    "choices": ["A", "B"],
                    "image_paths": [str(image)],
                }
            )
        )
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(captured[0]["state"], "")
        self.assertIn("images", captured[0])

    def test_assess_and_rank_forward_images(self) -> None:
        captured: list = []
        self._use_provider(_capable_provider(captured))
        image = self.home / "q.png"
        _write_image(image, PNG_SIG)
        assessed = json.loads(
            tools.nerve_assess(
                {
                    "state": {},
                    "questions": {"q": {"type": "noul", "instructions": "true?"}},
                    "image_paths": [str(image)],
                }
            )
        )
        self.assertTrue(assessed["ok"], assessed)
        ranked = json.loads(
            tools.nerve_rank(
                {
                    "state": {},
                    "instructions": "rank",
                    "items": {"A": "first", "B": "second"},
                    "image_paths": [str(image)],
                }
            )
        )
        self.assertTrue(ranked["ok"], ranked)
        self.assertTrue(captured[0]["images"][0].startswith("data:image/png;base64,"))
        self.assertTrue(captured[-1]["images"][0].startswith("data:image/png;base64,"))

    # -- provider validation before filesystem access ---------------------

    def test_unsupported_provider_refused_before_reading_files(self) -> None:
        self._use_provider(_UnsupportedProvider())
        missing = self.home / "does-not-exist.png"
        payload = json.loads(
            tools.nerve_decide(
                {
                    "state": {},
                    "instructions": "Choose",
                    "choices": ["A", "B"],
                    "image_paths": [str(missing)],
                }
            )
        )
        self.assertFalse(payload["ok"])
        error = payload["error"]
        self.assertIn("LOCAL ONLY", error)
        self.assertIn("loopback", error)
        self.assertIn("shadow", error)
        # Provider check ran first: no filesystem error and provider untouched.
        self.assertNotIn("regular", error)
        self.assertNotIn("must not be called", error)
        self.assertNotIn(str(missing), error)

    def test_shadow_fallback_provider_refused(self) -> None:
        class _Dummy:
            def system_one(self, **kwargs):
                raise AssertionError("shadow providers must not be called")

        shadow = ShadowProvider(_Dummy(), _Dummy())
        self._use_provider(shadow)
        image = self.home / "s.png"
        _write_image(image, PNG_SIG)
        payload = json.loads(
            tools.nerve_decide(
                {
                    "state": {},
                    "instructions": "Choose",
                    "choices": ["A", "B"],
                    "image_paths": [str(image)],
                }
            )
        )
        self.assertFalse(payload["ok"])
        self.assertIn("shadow", payload["error"])
        self.assertNotIn(str(image), payload["error"])

    # -- malformed / oversized / too many ---------------------------------

    def test_remote_systemone_refused_before_filesystem_access(self) -> None:
        for url in ('https://example.invalid', 'http://127.0.0.1.example.invalid'):
            with self.subTest(url=url):
                provider = D1Client(base_url=url, transport=lambda *args: self.fail('must not contact provider'))
                self._use_provider(provider)
                with patch.object(tools.os, 'lstat', side_effect=AssertionError('filesystem accessed')) as read:
                    payload = json.loads(tools.nerve_decide({
                        'state': '', 'instructions': 'Choose', 'choices': ['A', 'B'],
                        'image_paths': ['private-image.png']}))
                self.assertFalse(payload['ok'])
                self.assertIn('LOCAL ONLY', payload['error'])
                read.assert_not_called()

    def test_backend_error_cannot_echo_image_payload_into_tool_result(self) -> None:
        image = self.home / 'private-error.png'
        encoded = base64.b64encode(_write_image(image, PNG_SIG)).decode()
        def transport(*args):
            raise SystemOneHTTPError(f'backend echoed {image} data:image/png;base64,{encoded}')
        self._use_provider(D1Client(transport=transport))
        result = json.loads(tools.nerve_decide({
            'state': '', 'instructions': 'Choose', 'choices': ['A', 'B'],
            'image_paths': [str(image)]}))
        self.assertFalse(result['ok'])
        self.assertNotIn(str(image), result['error'])
        self.assertNotIn(encoded, result['error'])

    def test_replaced_path_cannot_follow_a_symlink(self) -> None:
        self._use_provider(_capable_provider([]))
        image = self.home / 'original.png'
        target = self.home / 'target.png'
        _write_image(image, PNG_SIG)
        _write_image(target, PNG_SIG)
        original = image.lstat()
        image.unlink()
        image.symlink_to(target)
        with patch.object(tools.os, 'lstat', return_value=original):
            payload = json.loads(tools.nerve_decide({
                'state': '', 'instructions': 'Choose', 'choices': ['A', 'B'],
                'image_paths': [str(image)]}))
        self.assertFalse(payload['ok'])

    def test_rejects_non_regular_file(self) -> None:
        self._use_provider(_capable_provider([]))
        payload = json.loads(
            tools.nerve_decide(
                {
                    "state": {},
                    "instructions": "Choose",
                    "choices": ["A", "B"],
                    "image_paths": [str(self.home)],
                }
            )
        )
        self.assertFalse(payload["ok"])
        self.assertIn("regular", payload["error"])
        self.assertNotIn(str(self.home), payload["error"])

    def test_rejects_unsupported_format(self) -> None:
        self._use_provider(_capable_provider([]))
        bogus = self.home / "fake.png"
        bogus.write_bytes(b"not an image at all")
        payload = json.loads(
            tools.nerve_decide(
                {
                    "state": {},
                    "instructions": "Choose",
                    "choices": ["A", "B"],
                    "image_paths": [str(bogus)],
                }
            )
        )
        self.assertFalse(payload["ok"])
        self.assertIn("PNG", payload["error"])
        self.assertNotIn(str(bogus), payload["error"])

    def test_rejects_oversized_image(self) -> None:
        self._use_provider(_capable_provider([]))
        big = self.home / "big.png"
        big.write_bytes(PNG_SIG + b"\x00" * (tools._MAX_IMAGE_BYTES + 1))
        payload = json.loads(
            tools.nerve_decide(
                {
                    "state": {},
                    "instructions": "Choose",
                    "choices": ["A", "B"],
                    "image_paths": [str(big)],
                }
            )
        )
        self.assertFalse(payload["ok"])
        self.assertIn("limit", payload["error"].lower())
        self.assertNotIn(str(big), payload["error"])

    def test_rejects_too_many_images(self) -> None:
        self._use_provider(_capable_provider([]))
        paths = []
        for i in range(tools._MAX_IMAGE_COUNT + 1):
            p = self.home / f"img{i}.png"
            _write_image(p, PNG_SIG)
            paths.append(str(p))
        payload = json.loads(
            tools.nerve_decide(
                {
                    "state": {},
                    "instructions": "Choose",
                    "choices": ["A", "B"],
                    "image_paths": paths,
                }
            )
        )
        self.assertFalse(payload["ok"])
        self.assertIn("at most", payload["error"])

    # -- receipt binding / privacy ----------------------------------------

    def test_receipt_binds_image_hash_without_storing_bytes_or_paths(self) -> None:
        captured: list = []
        self._use_provider(_capable_provider(captured))
        receipts.configure(enabled=True, detail="sanitized")
        image = self.home / "private-screen.png"
        raw = _write_image(image, PNG_SIG)
        tools.nerve_decide(
            {
                "state": {"note": "visible"},
                "instructions": "Choose",
                "choices": ["A", "B"],
                "image_paths": [str(image)],
            }
        )
        record = self._records()[-1]
        text = json.dumps(record)
        self.assertNotIn(str(image), text)
        self.assertNotIn(base64.b64encode(raw).decode(), text)
        state = record.get("state")
        self.assertIsInstance(state, dict)
        self.assertEqual(len(state["image_sha256"]), 1)
        self.assertEqual(state["state"]["note"], "visible")
        self.assertEqual(record['state_sha256'], privacy.canonical_hash({
            'state': {'note': 'visible'},
            'image_sha256': [privacy.canonical_hash(captured[0]['images'][0])]}))

    def test_different_image_content_changes_state_hash(self) -> None:
        captured: list = []
        self._use_provider(_capable_provider(captured))
        one = self.home / "one.png"
        two = self.home / "two.png"
        _write_image(one, PNG_SIG, total=64)
        _write_image(two, PNG_SIG, total=96)
        tools.nerve_decide(
            {"state": {"x": 1}, "instructions": "Choose", "choices": ["A", "B"], "image_paths": [str(one)]}
        )
        first = self._records()[-1]["state_sha256"]
        tools.nerve_decide(
            {"state": {"x": 1}, "instructions": "Choose", "choices": ["A", "B"], "image_paths": [str(two)]}
        )
        second = self._records()[-1]["state_sha256"]
        self.assertNotEqual(first, second)
        # Neither matches the text-only binding for the same state.
        self.assertNotEqual(first, privacy.canonical_hash({"x": 1}))

    def test_caller_cannot_override_receipt_image_binding(self) -> None:
        self._use_provider(_capable_provider([]))
        image = self.home / 'binding.png'
        hashes = []
        for size in (64, 96):
            _write_image(image, PNG_SIG, total=size)
            result = json.loads(tools.nerve_decide({
                'state': {'image_sha256': ['caller-controlled']},
                'instructions': 'Choose', 'choices': ['A', 'B'],
                'image_paths': [str(image)]}))
            self.assertTrue(result['ok'], result)
            hashes.append(self._records()[-1]['state_sha256'])
        self.assertNotEqual(*hashes)


if __name__ == "__main__":
    unittest.main()
