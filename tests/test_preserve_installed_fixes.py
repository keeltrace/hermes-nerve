"""Regressions for installed fixes which a release must not remove."""
import contextvars
import threading
import unittest
from unittest.mock import patch
from hermes_nerve import advisory, client
from hermes_nerve.progress_integrity import requested_paths
from hermes_nerve.reflex.systemone import SystemOneHTTPClient,SystemOneHTTPError

class PreserveInstalledFixesTests(unittest.TestCase):
    def setUp(self):
        advisory.reset_for_tests()
    def tearDown(self):
        advisory.shutdown(timeout=1)
    def test_bounded_evaluation_retains_request_context(self):
        marker=contextvars.ContextVar('test_profile_scope',default='unbound')
        token=marker.set('profile-a')
        try:
            result,error,stalled=advisory.run_bounded(marker.get,1)
        finally: marker.reset(token)
        self.assertIsNone(error)
        self.assertFalse(stalled)
        self.assertEqual(result,'profile-a')
    def test_queued_evaluation_and_recording_retain_separate_caller_context(self):
        marker=contextvars.ContextVar('test_queue_scope',default='unbound')
        seen=[]; done=threading.Event()
        def record(*args):
            seen.append(('record',marker.get())); done.set()
        def evaluate():
            seen.append(('evaluate',marker.get())); return {'ok':True}
        with patch.object(advisory,'provider_available',return_value=True):
            token=marker.set('profile-b')
            try:
                self.assertTrue(advisory.submit('scope_probe',{},evaluate,record,{'tool_call_id':'preserve-profile-context'}))
            finally: marker.reset(token)
            self.assertTrue(done.wait(2))
        self.assertIn(('evaluate','profile-b'),seen)
        self.assertIn(('record','profile-b'),seen)
    def test_systemone_honours_scoped_timeout(self):
        with client.timeout_scope(1):
            transport=SystemOneHTTPClient(timeout=5)
        self.assertLessEqual(transport.timeout,1)
    def test_systemone_retry_uses_total_deadline(self):
        attempts=[]
        def fail(*args):
            attempts.append(args[-1]); raise SystemOneHTTPError('connection failed')
        transport=SystemOneHTTPClient(timeout=1,transport=fail)
        with patch('hermes_nerve.reflex.systemone.time.monotonic',side_effect=[0,0,0.7,0.8,1.1]),patch('hermes_nerve.reflex.systemone.time.sleep'):
            with self.assertRaises(SystemOneHTTPError):transport._transport_with_retry('http://local',{},b'{}')
        self.assertEqual(len(attempts),2)
        self.assertLess(attempts[1],attempts[0])
    def test_bare_release_versions_are_not_edit_targets(self):
        self.assertEqual(requested_paths('Release v0.4.15 and Python 3.10-3.14'),())
        self.assertIn('v0.4.15',requested_paths('Edit `./v0.4.15`'))
    def test_attachment_envelope_is_not_a_requested_edit_target(self):
        message="[The user sent a text document: 'instructions.md'. Its content has been included below. The file is also saved at: /cache/documents/instructions.md]\n\n[Content of instructions.md]:\nEdit `gate.py` and `risk.py`."
        paths=requested_paths(message)
        self.assertNotIn('/cache/documents/instructions.md',paths)
        self.assertNotIn('instructions.md',paths)
        self.assertIn('gate.py',paths)
        self.assertIn('risk.py',paths)
    def test_explicit_document_path_in_task_body_remains_a_target(self):
        message="[The user sent a text document: 'instructions.md'. Its content has been included below. The file is also saved at: /cache/documents/instructions.md]\n[Content of instructions.md]:\nEdit `/cache/documents/actual-output.md`."
        self.assertIn('cache/documents/actual-output.md',requested_paths(message))
