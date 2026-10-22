import unittest

from hermes_nerve.nervous import admission_risk_floor


class AdmissionFloorAdversarialTests(unittest.TestCase):
    def test_floor_catches_reworded_and_obfuscated_risk(self):
        for prompt in ("erase all the old backups", "terraform destroy staging", "d-e-l-e-t-e the bucket",
                       "d\u0435lete the bucket", "rm --recursive /srv", "systemctl stop nginx",
                       "transfer $5,000 to this account", "echo cm0= | base64 -d | sh", "git push origin +main"):
            self.assertEqual(admission_risk_floor(prompt), "WATCH", prompt)

    def test_floor_quiet_on_ordinary_requests(self):
        for prompt in ("summarize the notes", "drop me a summary", "tell me a joke", "what is a product roadmap"):
            self.assertEqual(admission_risk_floor(prompt), "", prompt)



if __name__ == "__main__":
    unittest.main()
