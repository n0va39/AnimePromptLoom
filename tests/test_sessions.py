import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from sessions import Sessions, build_messages, estimate_tokens
import test_compiler
from compiler import Compiler


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sessions.json"
        self.store = Sessions(self.path)

    def test_disk_roundtrip_and_session_isolation(self):
        first = self.store.save({"name": "첫 대화", "characters": "유나: 검은 단발", "background": "도서관"})
        second = self.store.save({"name": "다른 대화", "characters": "갈색 머리", "background": "숲"})
        self.store.append(first["id"], "책 읽기", {"text": "Reads a book."}, first)
        reloaded = Sessions(self.path)
        self.assertEqual(reloaded.get(first["id"])["characters"], "유나: 검은 단발")
        self.assertEqual(len(reloaded.get(first["id"])["turns"]), 1)
        self.assertEqual(reloaded.get(second["id"])["turns"], [])
        self.assertFalse(self.path.with_suffix(".tmp").exists())

    def test_update_preserves_history_and_original_context_snapshot(self):
        session = self.store.save({"characters": "검은 머리", "background": "도서관"})
        self.store.append(session["id"], "책 읽기", {"text": "Reads."}, session)
        self.store.save({"id": session["id"], "characters": "빨간 머리", "background": "숲"})
        saved = self.store.get(session["id"])
        self.assertEqual(saved["characters"], "빨간 머리")
        self.assertEqual(saved["turns"][0]["context"]["characters"], "검은 머리")

    def test_corrupt_data_is_not_overwritten(self):
        self.path.write_text("broken", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            Sessions(self.path)
        self.assertEqual(self.path.read_text(), "broken")

    def test_pinned_context_and_recent_history_are_sent_in_order(self):
        session = {"characters": "유나: 녹색 눈", "background": "도서관", "turns": [
            {"prompt": f"request {n}", "result": {"text": f"answer {n}"}} for n in range(7)]}
        messages, info = build_messages("system", "이번에는 창가로 이동", session)
        self.assertIn("녹색 눈", messages[1]["content"])
        self.assertEqual(messages[2]["content"], "request 3")
        self.assertEqual(messages[-1]["content"], "이번에는 창가로 이동")
        self.assertEqual(info["included_turns"], 4)
        self.assertEqual(info["omitted_turns"], 3)

    def test_budget_drops_whole_old_turns_not_pinned_context(self):
        session = {"characters": "Yuna", "background": "Library", "turns": [
            {"prompt": "older" * 100, "result": {"text": "older" * 100}},
            {"prompt": "latest", "result": {"text": "latest answer"}}]}
        fixed, _ = build_messages("system", "current", None)
        messages, info = build_messages("system", "current", session, input_budget=estimate_tokens(fixed) + 200)
        self.assertEqual(info["included_turns"], 1)
        self.assertIn("Yuna", messages[1]["content"])
        self.assertEqual(messages[2]["content"], "latest")

    def test_oversized_pinned_context_raises_instead_of_truncating(self):
        with self.assertRaisesRegex(ValueError, "자동으로 잘라내지"):
            build_messages("system", "current", {"characters": "가" * 2000}, input_budget=1000)

    def test_invalid_session_id(self):
        with self.assertRaises(ValueError):
            self.store.get([])


class ContextIntegrationTests(unittest.TestCase):
    def test_default_single_call_with_context_and_keep_alive(self):
        fixture = test_compiler.CompilerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        ollama = Mock()
        ollama.models.return_value = ["qwen3:8b"]
        ollama.chat.return_value = fixture.doc
        context = {"characters": "유나: 검은 단발", "background": "도서관", "turns": [
            {"prompt": "창가로 이동", "result": {"text": "She moves to the window."}}]}
        result = Compiler(fixture.catalog, ollama).generate("책을 읽어", unload=False, session=context)
        ollama.chat.assert_called_once()
        args = ollama.chat.call_args
        self.assertEqual(args.args[2], "5m")
        self.assertFalse(args.kwargs["thinking"])
        self.assertIn("검은 단발", args.args[1][1]["content"])
        self.assertEqual(args.args[1][-3]["content"], "창가로 이동")
        self.assertEqual(result["context"]["included_turns"], 1)
        ollama.unload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
