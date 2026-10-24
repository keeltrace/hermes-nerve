"""Actual risk-module regressions: policy values must not reach overrides."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

# Load only the real pure risk module, without plugin registration/activation.
_SPEC = importlib.util.spec_from_file_location(
    "_nerve_operand_risk", Path(__file__).parents[1] / "hermes_nerve" / "risk.py")
assert _SPEC is not None and _SPEC.loader is not None
risk = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = risk
_SPEC.loader.exec_module(risk)
GOLDEN = json.loads((Path(__file__).parent / 'fixtures' / 'policy-members-b486692.json').read_text())


class PolicyOperandTests(unittest.TestCase):
    def test_closed_protected_boundary_rejects_custom_operands(self):
        predicate = getattr(risk, '_protected_system_match', None)
        self.assertTrue(callable(predicate), 'Missing closed protected-path boundary')
        assert callable(predicate)
        class Unknown:
            def __eq__(self, other):
                raise AssertionError('Unknown equality invoked')
            def startswith(self, prefix):
                raise AssertionError('Unknown prefix receiver invoked')
            def __radd__(self, other):
                raise AssertionError('Unknown prefix suffix invoked')
        self.assertIs(predicate(Unknown(), '/'), False)
        self.assertIs(predicate('/etc/shadow', Unknown()), False)
        values = GOLDEN['_PROTECTED_SYSTEM_PREFIXES']
        for prefix in values:
            for text in (prefix, prefix + '/child', prefix + '.backup', '/x' + prefix, prefix.upper()):
                self.assertEqual(predicate(text, '/'), any(text == p or text.startswith(p + '/') for p in values))
                self.assertEqual(predicate(text, ''), any(text.startswith(p) for p in values))

    def test_closed_unbounded_boundary_preserves_all_original_members(self):
        predicate = getattr(risk, '_is_unbounded_executable', None)
        self.assertTrue(callable(predicate), 'Missing closed unbounded-command boundary')
        assert callable(predicate)
        class Unknown:
            def __hash__(self):
                raise AssertionError('Unknown hash invoked')
            def __eq__(self, other):
                raise AssertionError('Unknown equality invoked')
        self.assertIs(predicate(Unknown()), False)
        for value in GOLDEN['_UNBOUNDED_EXECUTABLES']:
            self.assertIs(predicate(value), True)
            self.assertIs(predicate(value + '_ordinary'), False)

    def test_protected_path_cannot_override_policy_comparison(self):
        calls = []
        class Text(str):
            def __str__(self):
                return self
            def __eq__(self, other):
                calls.append(other)
                return False
            def startswith(self, prefix):
                calls.append(prefix)
                return False
        class PathLike:
            def __str__(self):
                return Text('/etc/shadow')
        self.assertTrue(risk._protected_system_read(PathLike()))
        self.assertEqual(calls, [])

    def test_plain_protected_and_ordinary_paths_preserved(self):
        for value in GOLDEN['_PROTECTED_SYSTEM_PREFIXES']:
            self.assertTrue(risk._protected_system_read(Path(value)))
            self.assertTrue(risk._protected_system_read(Path(value + '/child')))
        self.assertFalse(risk._protected_system_read(Path('/opt/example/readme')))

    def test_tokenization_cannot_return_subclass_operands(self):
        class Token(str):
            def __radd__(self, other):
                return self
        # Exercise real shlex stream protocol, preserving a subclass in a token.
        class Stream:
            def __init__(self):
                self.done = False
            def read(self, size=1):
                if self.done:
                    return ''
                self.done = True
                return Token('x')
        result = risk.parse_argv(Stream())
        self.assertIsNotNone(result)
        self.assertTrue(all(type(token) is str for token in result))
        self.assertEqual(result, ['x'])

    def test_boundedness_does_not_dispatch_token_overrides(self):
        calls = []
        class Token(str):
            def __radd__(self, other):
                return self
            def lower(self):
                calls.append('lower')
                return self
        class Stream:
            def __init__(self):
                self.done = False
            def read(self, size=1):
                if self.done:
                    return ''
                self.done = True
                return Token('x')
        risk._segment_is_bounded(Stream())
        self.assertEqual(calls, [])

    def test_closed_disk_predicate_rejects_custom_operands_without_dispatch(self):
        predicate = getattr(risk, '_is_disk_executable', None)
        self.assertTrue(callable(predicate), 'Missing closed disk-policy boundary')
        assert callable(predicate)
        class Unknown:
            def __hash__(self):
                raise AssertionError('Policy comparison dispatched unknown hash')
            def __eq__(self, other):
                raise AssertionError('Policy literal escaped to unknown equality')
        self.assertIs(predicate(Unknown()), False)
        self.assertIs(predicate('mkfs'), True)
        self.assertIs(predicate('echo'), False)

    def test_disk_policy_still_blocks_every_declared_executable(self):
        # Frozen original membership, independent of the refactored predicate.
        for executable in {'dd', 'mkfs', 'mkfs.ext4', 'mkfs.xfs', 'mkfs.btrfs',
                           'newfs', 'fdisk', 'parted', 'gparted', 'sfdisk', 'cfdisk',
                           'blkdiscard', 'wipefs'}:
            with self.subTest(executable=executable):
                assessment = risk.classify_segment(executable, Path.cwd(), [])
                self.assertEqual(assessment.category, risk.PROHIBITED)

    def test_plain_command_tokens_preserved(self):
        self.assertEqual(risk.parse_argv('echo "hello world"'), ['echo', 'hello world'])
        self.assertIsNone(risk.parse_argv('"'))
        self.assertIsNone(risk.parse_argv(''))
        for value in GOLDEN['_UNBOUNDED_EXECUTABLES']:
            self.assertFalse(risk._segment_is_bounded(value))


if __name__ == '__main__':
    unittest.main()
