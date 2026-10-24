"""Regression tests: sensitive-home precedence over tmp-root classification.

A Hermes home can legitimately live under a temporary directory (isolated CI
runners, profile sandboxes). A credential path inside such a home must classify
as sensitive-home — never as routine "tmp" — or it would bypass approval gates
(review finding, PR #75).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve import risk


class TmpResidentHomePrecedenceTests(unittest.TestCase):
    def test_sensitive_file_under_tmp_resident_home_is_sensitive_home(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "runner" / ".hermes-home"
            cred = fake_home / ".ssh" / "id_ed25519"
            with patch.object(risk, "_HOME", fake_home), \
                 patch.dict("os.environ", {"HOME": str(fake_home)}):
                self.assertEqual(risk.classify_scope(cred.resolve(strict=False)), "sensitive-home")
                self.assertEqual(
                    risk.classify_path(cred, Path(td) / "workdir"), "sensitive-home")
                self.assertTrue(risk.sensitive_read_target(cred))

    def test_sensitive_dir_under_tmp_resident_home_is_sensitive_home(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "h"
            aws_dir = fake_home / ".aws"
            with patch.object(risk, "_HOME", fake_home), \
                 patch.dict("os.environ", {"HOME": str(fake_home)}):
                self.assertEqual(risk.classify_scope(aws_dir), "sensitive-home")
                self.assertEqual(risk.classify_path(aws_dir, Path(td) / "w"), "sensitive-home")

    def test_plain_tmp_path_outside_any_home_still_tmp(self):
        with tempfile.TemporaryDirectory() as td:
            plain = Path(td) / "scratch" / "notes.txt"
            with patch.object(risk, "_HOME", Path("/home/somebody-else")), \
                 patch.dict("os.environ", {"HOME": "/home/somebody-else"}):
                self.assertEqual(risk.classify_scope(plain), "tmp")
                self.assertEqual(risk.classify_path(plain, Path(td) / "w"), "tmp")

    def test_dotenv_under_tmp_resident_home_still_dotenv_not_tmp(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "h"
            env_file = fake_home / ".env"
            with patch.object(risk, "_HOME", fake_home), \
                 patch.dict("os.environ", {"HOME": str(fake_home)}):
                self.assertEqual(risk.classify_scope(env_file), "dotenv")
                self.assertTrue(risk.sensitive_read_target(env_file))


if __name__ == "__main__":
    unittest.main()
