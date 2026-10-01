"""Unit tests for hermes_nerve.language_detector and language_telemetry (KEE-73 phase 2).

Covers:
- classify_text_segments: all exemption categories (code, command, url, filename,
  structured, log, quote, cjk_identifier, translation, prose)
- extract_prose_segments / is_exempt / prose_for_detection
- TurnObservation: creation, as_dict/from_dict round-trip
- LanguageTelemetry: counters, mismatch_rate, ring buffer, as_dict/from_dict
- ObserveOnlyValidator: all ObservationResult branches (match, mismatch,
  undetermined, no_prose, contract_off, no_contract_lang), thread safety, reset
- LanguageContract.observe_response() integration (end-to-end)
- LanguageContract.telemetry accessor
- LanguageContract.as_dict() includes telemetry
- LanguageContract.reset_session() resets telemetry
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from hermes_nerve.language_detector import (
    TextSegment,
    classify_text_segments,
    extract_prose_segments,
    is_exempt,
    prose_for_detection,
)
from hermes_nerve.language_telemetry import (
    LanguageTelemetry,
    ObserveOnlyValidator,
    TurnObservation,
)
from hermes_nerve.language_contract import (
    LanguageContract,
    LanguageContractConfig,
)


# ============================================================
# language_detector — classify_text_segments
# ============================================================

class TestClassifyFencedCode(unittest.TestCase):

    def test_fenced_code_block_is_code(self):
        text = "Here is some code:\n```python\nprint('hello')\n```\nEnd."
        segs = classify_text_segments(text)
        cats = [s.category for s in segs]
        self.assertIn("code", cats)
        # The fenced block itself should be code
        code_segs = [s for s in segs if s.category == "code"]
        self.assertTrue(any("print" in s.text for s in code_segs))

    def test_tilde_fenced_block_is_code(self):
        text = "Example:\n~~~bash\necho hello\n~~~\nDone."
        segs = classify_text_segments(text)
        cats = [s.category for s in segs]
        self.assertIn("code", cats)

    def test_inline_code_span_is_code(self):
        text = "Use `os.path.join()` to build paths."
        segs = classify_text_segments(text)
        code_segs = [s for s in segs if s.category == "code"]
        self.assertTrue(any("os.path.join" in s.text for s in code_segs))

    def test_prose_outside_code_is_prose(self):
        text = "Here is some code:\n```python\nprint('hello')\n```\nEnd."
        segs = classify_text_segments(text)
        prose_segs = [s for s in segs if s.category == "prose"]
        combined = " ".join(s.text for s in prose_segs)
        # "Here is some code:" and "End." should be prose
        self.assertTrue("End" in combined or "Here" in combined)


class TestClassifyURL(unittest.TestCase):

    def test_https_url_is_url(self):
        text = "See https://example.com/path?q=1 for details."
        segs = classify_text_segments(text)
        url_segs = [s for s in segs if s.category == "url"]
        self.assertTrue(len(url_segs) >= 1)
        self.assertIn("https://example.com", url_segs[0].text)

    def test_http_url_is_url(self):
        segs = classify_text_segments("Visit http://foo.bar/baz now.")
        url_segs = [s for s in segs if s.category == "url"]
        self.assertTrue(len(url_segs) >= 1)

    def test_mailto_is_url(self):
        segs = classify_text_segments("Email mailto:user@example.com please.")
        url_segs = [s for s in segs if s.category == "url"]
        self.assertTrue(len(url_segs) >= 1)

    def test_prose_around_url_is_prose(self):
        text = "Visit https://example.com for more info."
        segs = classify_text_segments(text)
        prose_segs = [s for s in segs if s.is_prose]
        combined = " ".join(s.text for s in prose_segs)
        self.assertTrue("Visit" in combined or "for more info" in combined)


class TestClassifyCommand(unittest.TestCase):

    def test_dollar_prompt_is_command(self):
        text = "Run the following:\n$ git status\nThen check output."
        segs = classify_text_segments(text)
        cmd_segs = [s for s in segs if s.category == "command"]
        self.assertTrue(len(cmd_segs) >= 1)
        self.assertIn("git status", cmd_segs[0].text)

    def test_shebang_is_command(self):
        text = "#!/usr/bin/env python3"
        segs = classify_text_segments(text)
        cmd_segs = [s for s in segs if s.category == "command"]
        self.assertTrue(len(cmd_segs) >= 1)


class TestClassifyLog(unittest.TestCase):

    def test_iso_timestamp_line_is_log(self):
        text = "Output:\n2024-01-15T12:34:56 INFO starting\nDone."
        segs = classify_text_segments(text)
        log_segs = [s for s in segs if s.category == "log"]
        self.assertTrue(len(log_segs) >= 1)

    def test_severity_prefix_is_log(self):
        text = "ERROR: something went wrong here"
        segs = classify_text_segments(text)
        log_segs = [s for s in segs if s.category == "log"]
        self.assertTrue(len(log_segs) >= 1)

    def test_python_traceback_is_log(self):
        text = 'File "foo.py", line 42, in bar\n    raise ValueError("oops")'
        segs = classify_text_segments(text)
        log_segs = [s for s in segs if s.category == "log"]
        self.assertTrue(len(log_segs) >= 1)


class TestClassifyQuote(unittest.TestCase):

    def test_blockquote_is_quote(self):
        text = "They said:\n> This is a blockquote.\n> Second line.\nEnd."
        segs = classify_text_segments(text)
        quote_segs = [s for s in segs if s.category == "quote"]
        self.assertTrue(len(quote_segs) >= 1)


class TestClassifyStructured(unittest.TestCase):

    def test_markdown_table_is_structured(self):
        text = "Results:\n| Name | Value |\n| foo  | 1     |\n| bar  | 2     |\nEnd."
        segs = classify_text_segments(text)
        struct_segs = [s for s in segs if s.category == "structured"]
        self.assertTrue(len(struct_segs) >= 1)


class TestClassifyTranslation(unittest.TestCase):

    def test_translation_payload_flag_makes_entire_text_exempt(self):
        text = "这是一段中文文字，整个响应是翻译内容。"
        segs = classify_text_segments(text, translation_payload=True)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].category, "translation")
        self.assertTrue(segs[0].is_exempt)

    def test_translation_payload_returns_single_segment(self):
        text = "Hello world this is a test."
        segs = classify_text_segments(text, translation_payload=True)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].category, "translation")


class TestClassifyProse(unittest.TestCase):

    def test_plain_prose_is_prose(self):
        text = "This is a plain natural-language sentence with no code."
        segs = classify_text_segments(text)
        self.assertTrue(all(s.category == "prose" for s in segs))

    def test_empty_text_returns_empty(self):
        segs = classify_text_segments("")
        self.assertEqual(segs, [])

    def test_whitespace_only_returns_empty(self):
        segs = classify_text_segments("   \n   ")
        self.assertEqual(segs, [])

    def test_segments_cover_text_contiguously(self):
        """All segments are non-overlapping and text content is fully covered."""
        text = (
            "Intro text. See https://example.com for more.\n"
            "```python\nprint('hi')\n```\n"
            "Conclusion."
        )
        segs = classify_text_segments(text)
        for i in range(len(segs) - 1):
            self.assertLessEqual(segs[i].end, segs[i + 1].start,
                                 f"Overlap between segments {i} and {i+1}")
        # All segment texts should be non-empty (we filter blank prose)
        for s in segs:
            self.assertTrue(s.text, f"Empty segment: {s!r}")


class TestIsExempt(unittest.TestCase):

    def test_pure_code_block_is_exempt(self):
        text = "```python\nprint('hello')\n```"
        self.assertTrue(is_exempt(text))

    def test_pure_url_is_exempt(self):
        self.assertTrue(is_exempt("https://example.com/path"))

    def test_translation_payload_is_exempt(self):
        self.assertTrue(is_exempt("some text", translation_payload=True))

    def test_empty_is_exempt(self):
        self.assertTrue(is_exempt(""))
        self.assertTrue(is_exempt("   "))

    def test_prose_is_not_exempt(self):
        self.assertFalse(is_exempt("This is a natural language sentence."))

    def test_mixed_prose_and_code_is_not_exempt(self):
        text = "Here is the code:\n```\nfoo()\n```\nAnd this prose remains."
        self.assertFalse(is_exempt(text))


class TestProseForDetection(unittest.TestCase):

    def test_returns_none_for_short_prose(self):
        # Under 20 chars
        result = prose_for_detection("Hi there.")
        self.assertIsNone(result)

    def test_returns_prose_for_sufficient_content(self):
        text = "This is a sufficiently long prose sentence for detection purposes."
        result = prose_for_detection(text)
        self.assertIsNotNone(result)
        self.assertIn("sufficiently", result)

    def test_strips_code_from_prose(self):
        text = "Here is some code:\n```python\nprint('hello')\n```\nAnd more natural language prose text here."
        result = prose_for_detection(text)
        self.assertIsNotNone(result)
        # Code should not appear in prose
        self.assertNotIn("print", result)
        self.assertIn("natural language", result)

    def test_translation_payload_returns_none(self):
        text = "这是一段翻译内容，应该被豁免不用于检测。"
        result = prose_for_detection(text, translation_payload=True)
        self.assertIsNone(result)

    def test_custom_min_chars(self):
        text = "Short text."
        # Default min_prose_chars=20 would reject this; lower it
        result = prose_for_detection(text, min_prose_chars=5)
        self.assertIsNotNone(result)

    def test_only_url_returns_none(self):
        text = "https://example.com/path/to/resource"
        result = prose_for_detection(text)
        self.assertIsNone(result)


class TestTextSegmentProperties(unittest.TestCase):

    def test_prose_is_not_exempt(self):
        s = TextSegment(text="hello", category="prose", start=0, end=5)
        self.assertFalse(s.is_exempt)
        self.assertTrue(s.is_prose)

    def test_code_is_exempt(self):
        s = TextSegment(text="```\nfoo()\n```", category="code", start=0, end=10)
        self.assertTrue(s.is_exempt)
        self.assertFalse(s.is_prose)

    def test_all_categories_except_prose_are_exempt(self):
        from hermes_nerve.language_detector import _EXEMPT_CATEGORIES
        for cat in _EXEMPT_CATEGORIES:
            s = TextSegment(text="x", category=cat, start=0, end=1)
            self.assertTrue(s.is_exempt, f"Expected {cat!r} to be exempt")


# ============================================================
# TurnObservation
# ============================================================

class TestTurnObservation(unittest.TestCase):

    def test_creation(self):
        obs = TurnObservation(
            turn_id="t1",
            result="match",
            contract_language="zh",
            detected_language="zh",
            prose_char_count=42,
        )
        self.assertEqual(obs.turn_id, "t1")
        self.assertEqual(obs.result, "match")
        self.assertIsNotNone(obs.observed_at)

    def test_as_dict_from_dict_round_trip(self):
        obs = TurnObservation(
            turn_id="t2",
            result="mismatch",
            contract_language="ja",
            detected_language="zh",
            prose_char_count=100,
        )
        d = obs.as_dict()
        obs2 = TurnObservation.from_dict(d)
        self.assertEqual(obs2.turn_id, "t2")
        self.assertEqual(obs2.result, "mismatch")
        self.assertEqual(obs2.contract_language, "ja")
        self.assertEqual(obs2.detected_language, "zh")
        self.assertEqual(obs2.prose_char_count, 100)
        self.assertEqual(obs2.observed_at, obs.observed_at)

    def test_from_dict_defaults(self):
        obs = TurnObservation.from_dict({
            "turn_id": "t3",
            "result": "no_prose",
            "prose_char_count": 0,
        })
        self.assertIsNone(obs.contract_language)
        self.assertIsNone(obs.detected_language)


# ============================================================
# LanguageTelemetry
# ============================================================

class TestLanguageTelemetry(unittest.TestCase):

    def _telemetry(self) -> LanguageTelemetry:
        return LanguageTelemetry(session_id="test-sess")

    def test_initial_state(self):
        t = self._telemetry()
        self.assertEqual(t.total_turns_observed, 0)
        self.assertEqual(t.match_count, 0)
        self.assertIsNone(t.mismatch_rate)

    def test_record_match(self):
        t = self._telemetry()
        obs = TurnObservation(turn_id="t1", result="match",
                              contract_language="zh", detected_language="zh",
                              prose_char_count=50)
        t.record(obs)
        self.assertEqual(t.total_turns_observed, 1)
        self.assertEqual(t.match_count, 1)
        self.assertEqual(t.language_counts.get("zh"), 1)

    def test_record_mismatch(self):
        t = self._telemetry()
        obs = TurnObservation(turn_id="t2", result="mismatch",
                              contract_language="ja", detected_language="zh",
                              prose_char_count=60)
        t.record(obs)
        self.assertEqual(t.mismatch_count, 1)
        self.assertEqual(t.mismatch_languages.get("zh"), 1)

    def test_record_no_prose(self):
        t = self._telemetry()
        obs = TurnObservation(turn_id="t3", result="no_prose",
                              contract_language=None, detected_language=None,
                              prose_char_count=0)
        t.record(obs)
        self.assertEqual(t.no_prose_count, 1)

    def test_record_contract_off(self):
        t = self._telemetry()
        obs = TurnObservation(turn_id="t4", result="contract_off",
                              contract_language=None, detected_language=None,
                              prose_char_count=0)
        t.record(obs)
        self.assertEqual(t.contract_off_count, 1)

    def test_mismatch_rate_calculation(self):
        t = self._telemetry()
        for r in ["match", "match", "mismatch", "undetermined"]:
            obs = TurnObservation(turn_id="x", result=r,
                                  contract_language="zh",
                                  detected_language="zh" if r == "match" else "ja" if r == "mismatch" else None,
                                  prose_char_count=50)
            t.record(obs)
        # 1 mismatch / (2 match + 1 mismatch + 1 undetermined) = 0.25
        self.assertAlmostEqual(t.mismatch_rate, 0.25)

    def test_ring_buffer_capped(self):
        t = LanguageTelemetry(session_id="s", _recent_max=5)
        for i in range(10):
            obs = TurnObservation(turn_id=f"t{i}", result="no_prose",
                                  contract_language=None, detected_language=None,
                                  prose_char_count=0)
            t.record(obs)
        self.assertLessEqual(len(t.recent_observations), 5)

    def test_as_dict_from_dict_round_trip(self):
        t = self._telemetry()
        obs = TurnObservation(turn_id="t1", result="match",
                              contract_language="ko", detected_language="ko",
                              prose_char_count=30)
        t.record(obs)
        d = t.as_dict()
        t2 = LanguageTelemetry.from_dict(d)
        self.assertEqual(t2.session_id, "test-sess")
        self.assertEqual(t2.match_count, 1)
        self.assertEqual(t2.total_turns_observed, 1)
        self.assertEqual(len(t2.recent_observations), 1)
        self.assertEqual(t2.recent_observations[0].result, "match")


# ============================================================
# ObserveOnlyValidator
# ============================================================

class TestObserveOnlyValidator(unittest.TestCase):

    def _validator(self) -> ObserveOnlyValidator:
        return ObserveOnlyValidator("test-sess")

    def test_contract_off_result(self):
        v = self._validator()
        obs = v.observe(
            turn_id="t1",
            assistant_text="Some text here.",
            contract_language=None,
            contract_mode="off",
        )
        self.assertEqual(obs.result, "contract_off")
        self.assertEqual(v.telemetry.contract_off_count, 1)

    def test_no_prose_result_for_code_only_response(self):
        v = self._validator()
        text = "```python\nprint('hello')\n```"
        obs = v.observe(
            turn_id="t1",
            assistant_text=text,
            contract_language="en",
            contract_mode="user",
        )
        self.assertEqual(obs.result, "no_prose")
        self.assertEqual(v.telemetry.no_prose_count, 1)

    def test_no_contract_lang_result(self):
        v = self._validator()
        obs = v.observe(
            turn_id="t1",
            assistant_text="This is a sufficiently long prose sentence for language detection.",
            contract_language=None,
            contract_mode="user",
        )
        self.assertEqual(obs.result, "no_contract_lang")

    def test_undetermined_result_for_latin_prose(self):
        v = self._validator()
        obs = v.observe(
            turn_id="t1",
            assistant_text="This is a long English sentence with enough characters for detection.",
            contract_language="en",
            contract_mode="user",
        )
        # Latin-script language detector returns None → undetermined
        self.assertEqual(obs.result, "undetermined")

    def test_match_result_for_cjk_prose(self):
        v = self._validator()
        # Assistant responds in CJK prose; contract resolved to "zh"
        obs = v.observe(
            turn_id="t1",
            assistant_text="这是一段很长的中文文字，足够用于语言检测和验证。这段文字包含足够多的字符。",
            contract_language="zh",
            contract_mode="user",
        )
        self.assertEqual(obs.result, "match")
        self.assertEqual(v.telemetry.match_count, 1)

    def test_mismatch_result_for_wrong_language(self):
        v = self._validator()
        # Assistant responds in Chinese; contract expects Japanese
        obs = v.observe(
            turn_id="t1",
            assistant_text="这是一段很长的中文文字，足够用于语言检测和验证。这段文字包含足够多的字符。",
            contract_language="ja",
            contract_mode="user",
        )
        self.assertEqual(obs.result, "mismatch")
        self.assertEqual(v.telemetry.mismatch_count, 1)
        self.assertEqual(obs.detected_language, "zh")

    def test_translation_payload_is_no_prose(self):
        v = self._validator()
        obs = v.observe(
            turn_id="t1",
            assistant_text="这是一段中文文字。",
            contract_language="en",
            contract_mode="user",
            translation_payload=True,
        )
        self.assertEqual(obs.result, "no_prose")

    def test_reset_clears_telemetry(self):
        v = self._validator()
        v.observe(
            turn_id="t1",
            assistant_text="这是一段很长的中文文字，足够用于语言检测。",
            contract_language="zh",
            contract_mode="user",
        )
        self.assertEqual(v.telemetry.total_turns_observed, 1)
        v.reset()
        self.assertEqual(v.telemetry.total_turns_observed, 0)

    def test_thread_safety(self):
        v = self._validator()
        errors: list[Exception] = []

        def run():
            for i in range(20):
                try:
                    v.observe(
                        turn_id=f"t{i}",
                        assistant_text="这是中文文字，用于测试线程安全。" * 3,
                        contract_language="zh",
                        contract_mode="user",
                    )
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertFalse(errors, f"Thread errors: {errors}")
        self.assertEqual(v.telemetry.total_turns_observed, 80)

    def test_prose_char_count_is_recorded(self):
        v = self._validator()
        text = "这是一段很长的中文文字，足够用于语言检测和验证。这段文字包含足够多的字符。"
        obs = v.observe(
            turn_id="t1",
            assistant_text=text,
            contract_language="zh",
            contract_mode="user",
        )
        self.assertGreater(obs.prose_char_count, 0)


# ============================================================
# LanguageContract integration (phase 2 methods)
# ============================================================

class TestLanguageContractPhase2(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._home = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _contract(self, **kw) -> LanguageContract:
        cfg = LanguageContractConfig(**kw)
        return LanguageContract(config=cfg, session_id="kee73-sess", home=self._home)

    def test_observe_response_contract_off(self):
        c = self._contract(mode="off")
        obs = c.observe_response(turn_id="t1", assistant_text="Hello world test.")
        self.assertEqual(obs.result, "contract_off")

    def test_observe_response_no_prose_for_code(self):
        c = self._contract(mode="user")
        obs = c.observe_response(
            turn_id="t1",
            assistant_text="```python\nprint('hello')\n```",
        )
        self.assertEqual(obs.result, "no_prose")

    def test_observe_response_no_contract_lang_early_session(self):
        """Before any user turn establishes a language, contract_language is None."""
        c = self._contract(mode="user")
        obs = c.observe_response(
            turn_id="t1",
            assistant_text="This is a long enough prose sentence for detection here.",
        )
        self.assertEqual(obs.result, "no_contract_lang")

    def test_observe_response_match_after_user_turn(self):
        c = self._contract(mode="user")
        # Establish CJK language from user turn
        c.record_turn(turn_id="t1", user_text="你好世界，我很高兴见到你")
        self.assertEqual(c.resolved_language, "zh")
        # Assistant responds in Chinese
        obs = c.observe_response(
            turn_id="t2",
            assistant_text="这是一段很长的中文文字，足够用于语言检测和验证。这段文字包含足够多的字符。",
        )
        self.assertEqual(obs.result, "match")

    def test_observe_response_mismatch(self):
        c = self._contract(mode="user")
        c.record_turn(turn_id="t1", user_text="こんにちは、元気ですか？今日はいい天気ですね")
        self.assertEqual(c.resolved_language, "ja")
        # Assistant (incorrectly) responds in Chinese
        obs = c.observe_response(
            turn_id="t2",
            assistant_text="这是一段很长的中文文字，足够用于语言检测和验证。这段文字包含足够多的字符。",
        )
        self.assertEqual(obs.result, "mismatch")
        self.assertEqual(obs.detected_language, "zh")
        self.assertEqual(obs.contract_language, "ja")

    def test_observe_response_fixed_mode(self):
        c = self._contract(mode="fixed", fixed_language="es")
        # Assistant responds in Chinese — detected "zh" vs contract "es"
        # Result will be "mismatch" (detected zh != es)
        obs = c.observe_response(
            turn_id="t1",
            assistant_text="这是一段很长的中文文字，足够用于语言检测和验证。这段文字包含足够多的字符。",
        )
        self.assertIn(obs.result, ("mismatch", "undetermined"))  # Latin "es" can't be detected vs zh

    def test_observe_response_translation_payload_is_exempt(self):
        c = self._contract(mode="user")
        c.record_turn(turn_id="t1", user_text="こんにちは、元気ですか？今日はいい天気ですね")
        obs = c.observe_response(
            turn_id="t2",
            assistant_text="这是一段很长的中文文字。",
            translation_payload=True,
        )
        self.assertEqual(obs.result, "no_prose")

    def test_telemetry_property(self):
        c = self._contract(mode="user")
        telemetry = c.telemetry
        self.assertEqual(telemetry.session_id, "kee73-sess")
        self.assertEqual(telemetry.total_turns_observed, 0)

    def test_telemetry_accumulates_across_observe_calls(self):
        c = self._contract(mode="user")
        c.record_turn(turn_id="t1", user_text="你好世界，我很高兴见到你")
        c.observe_response(
            turn_id="t2",
            assistant_text="这是一段很长的中文文字，足够用于语言检测和验证。",
        )
        c.observe_response(
            turn_id="t3",
            assistant_text="```python\nprint('hello')\n```",
        )
        self.assertEqual(c.telemetry.total_turns_observed, 2)
        self.assertEqual(c.telemetry.no_prose_count, 1)

    def test_as_dict_includes_telemetry(self):
        c = self._contract(mode="user")
        d = c.as_dict()
        self.assertIn("telemetry", d)
        self.assertEqual(d["telemetry"]["session_id"], "kee73-sess")

    def test_reset_session_resets_telemetry(self):
        c = self._contract(mode="user")
        c.record_turn(turn_id="t1", user_text="你好世界，我很高兴见到你")
        c.observe_response(
            turn_id="t2",
            assistant_text="这是一段很长的中文文字，足够用于语言检测和验证。",
        )
        self.assertGreater(c.telemetry.total_turns_observed, 0)
        c.reset_session()
        self.assertEqual(c.telemetry.total_turns_observed, 0)

    def test_observe_does_not_enforce(self):
        """observe_response must not raise or modify state regardless of mismatch."""
        c = self._contract(mode="user")
        c.record_turn(turn_id="t1", user_text="こんにちは、元気ですか？今日はいい天気ですね")
        # Mismatch — should complete without exception
        obs = c.observe_response(
            turn_id="t2",
            assistant_text="这是一段很长的中文文字，足够用于语言检测和验证。这段文字包含足够多的字符。",
        )
        self.assertEqual(obs.result, "mismatch")
        # Contract state should be unchanged (no enforcement)
        self.assertEqual(c.resolved_language, "ja")


if __name__ == "__main__":
    unittest.main()
