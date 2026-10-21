import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_publication import audit, blocked_path, contains_secret, main, safe_location


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic test")
        self.git("config", "user.email", "test@example.invalid")
        (self.root / ".gitignore").write_text("state/\n.env\ndata/*.csv\n*.dpapi\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic fixture")
        self.fake_key = "AIza" + "A" * 35

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], stderr=subprocess.DEVNULL)

    def test_clean_repo_and_ignored_private_files(self):
        (self.root / "state").mkdir()
        (self.root / "state" / "sessions.json").write_text("private local fixture")
        self.assertTrue(audit(self.root)["passed"])
        self.assertTrue(blocked_path("state/secrets/gemini.dpapi"))
        self.assertFalse(blocked_path(".env.example"))

    def test_secret_in_working_index_and_deleted_history_is_detected_without_value(self):
        path = self.root / "example.txt"
        path.write_text(self.fake_key)
        report = audit(self.root)
        self.assertFalse(report["passed"])
        self.assertNotIn(self.fake_key, json.dumps(report))
        self.git("add", "example.txt")
        path.write_text("clean working file")
        self.assertTrue(any(f["location"] == "index:example.txt" for f in audit(self.root)["findings"]))
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic pattern fixture")
        self.git("add", "example.txt")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "Remove pattern")
        report = audit(self.root)
        self.assertTrue(any(f["location"].startswith("history:") for f in report["findings"]))
        self.assertNotIn(self.fake_key, json.dumps(report))

    def test_forced_private_file_and_redaction(self):
        (self.root / "state").mkdir()
        (self.root / "state" / "sessions.json").write_text("{}")
        self.git("add", "-f", "state/sessions.json")
        self.assertFalse(audit(self.root)["passed"])
        self.assertTrue(contains_secret(self.fake_key.encode()))
        self.assertNotIn(self.fake_key, safe_location(self.fake_key + ".txt"))

    def test_incomplete_audit_does_not_print_raw_error(self):
        capture = io.StringIO()
        with patch("scripts.check_publication.audit", side_effect=OSError(self.fake_key)), contextlib.redirect_stdout(capture):
            self.assertEqual(main(), 2)
        self.assertNotIn(self.fake_key, capture.getvalue())


if __name__ == "__main__":
    unittest.main()
