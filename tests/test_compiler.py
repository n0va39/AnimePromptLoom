import copy
import csv
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock
from http.server import ThreadingHTTPServer

from app import Application, handler_for
from compiler import Catalog, Compiler, finalize, normalize


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tags.csv"
        with self.path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerows([
                ["name", "category", "post_count", "description"],
                ["1girl", 0, 1000, "여성 한 명"], ["1boy", 0, 500, "남성 한 명"],
                ["2girls", 0, 400, "여성 두 명"], ["2boys", 0, 300, "남성 두 명"],
                ["solo", 0, 900, "단 한 명"], ["reading", 0, 800, "책 읽기"],
                ["red_coat", 0, 70, "빨간 코트"], ["blue_jacket", 0, 60, "파란 재킷"],
                ["hair_over_eyes", 0, 600, "머리카락으로 눈을 가린 상태"],
                ["book", 0, 300, "책"], ["rain", 0, 200, "비"],
                ["character_(series)", 4, 10, "괄호 이름", "alias"],
                [":3", 0, 10, "입 모양"], ["1/2", 0, 2, "슬래시"]])
        self.catalog = Catalog(self.path)
        self.doc = {"description": "A woman reads a book.", "translation": "한 여성이 책을 읽는다.",
                    "subject_count": 1, "global_tags": ["1girl", "solo"],
                    "people": {"female": 1, "male": 0, "other": 0, "unspecified": 0},
                    "characters": [{"description": "A woman reads.", "tags": ["reading"]}],
                    "environment_tags": ["book"]}

    def test_full_membership_and_no_fuzzy_acceptance(self):
        self.doc["characters"][0]["tags"] = ["Hair_Over_Eyes", "hair over eyes", "beautiful girl", "readng"]
        result = finalize(self.doc, self.catalog, "눈을 가린 여성")
        self.assertEqual([t["rendered"] for t in result["verified"] if t["section"] == "Character 1"], ["hair over eyes"])
        self.assertEqual([t["tag"] for t in result["excluded"]], ["beautiful girl", "readng"])

    def test_special_characters_and_weight_format(self):
        self.doc["characters"][0]["tags"] = [r"character_\(series\)", ":3", "1/2", "(hair_over_eyes:0.8)"]
        result = finalize(self.doc, self.catalog, "가중치 사용")
        self.assertIn(r"character \(series\)", result["text"])
        self.assertIn(":3, 1/2, (hair over eyes:0.80)", result["text"])

    def test_automatic_weight_is_rejected(self):
        self.doc["characters"][0]["tags"] = ["(reading:1.2)"]
        result = finalize(self.doc, self.catalog, "책 읽는 여성")
        self.assertNotIn("(reading:1.20)", result["text"])
        self.assertEqual(len(result["excluded"]), 1)

    def test_multi_character_scope_and_solo(self):
        self.doc["subject_count"] = 2
        self.doc["people"]["male"] = 1
        self.doc["global_tags"] = ["solo", "2girls", "2boys"]
        self.doc["characters"] = [{"description": "Woman", "tags": ["1girl", "red_coat", "hair_over_eyes"]},
                                  {"description": "Man", "tags": ["1boy", "blue_jacket", "hair_over_eyes"]}]
        result = finalize(self.doc, self.catalog, "두 사람")
        self.assertIn("# Global / Composition\n1girl, 1boy", result["text"])
        self.assertNotIn("\nsolo", result["text"])
        self.assertNotIn("2girls", result["text"])
        self.assertNotIn("2boys", result["text"])
        self.assertEqual(result["text"].count("hair over eyes"), 2)
        self.assertIn("# Character 2\nblue jacket, hair over eyes", result["text"])

    def test_empty_sections_omitted_and_translation_not_in_positive(self):
        self.doc["characters"] = []
        self.doc["global_tags"] = []
        self.doc["environment_tags"] = []
        self.doc["subject_count"] = 0
        self.doc["people"]["female"] = 0
        result = finalize(self.doc, self.catalog, "장면")
        self.assertNotIn("# 2. English Tags", result["text"])
        self.assertNotIn("Korean Translation", result["positive"])
        self.assertTrue(result["markdown"].startswith("```text\n"))
        self.assertEqual(result["markdown"].count("```"), 2)

    def test_search_uses_korean_and_fifth_column_is_not_description(self):
        self.assertEqual(self.catalog.search("머리카락")[0].name, "hair_over_eyes")
        self.assertEqual(self.catalog.tags["character (series)"].description, "괄호 이름")
        self.assertEqual(normalize(r"Hair_Over_\(Eyes\)"), "hair over (eyes)")

    def test_unknown_count_gets_real_count_references(self):
        refs = self.catalog.references(["2people"], "두 명", "They are reading.", {"female": 1, "male": 1})
        names = {tag["name"] for tag in refs}
        self.assertTrue({"1girl", "1boy", "reading"}.issubset(names))
        self.assertNotIn("2people", names)

    def test_wrong_csv_rejected(self):
        self.path.write_text('1girl,0,100,"aliases"', encoding="utf-8")
        with self.assertRaises(ValueError):
            Catalog(self.path)

    def test_two_pass_generation_and_unload_on_success(self):
        ollama = Mock()
        ollama.models.return_value = ["qwen3:8b"]
        ollama.chat.side_effect = [copy.deepcopy(self.doc), copy.deepcopy(self.doc)]
        result = Compiler(self.catalog, ollama).generate("책 읽는 여성", refine=True)
        self.assertEqual(ollama.chat.call_count, 2)
        self.assertIn("csv_reference_data", ollama.chat.call_args.args[1][-1]["content"])
        enums = ollama.chat.call_args.kwargs["schema"]["properties"]["global_tags"]["items"]["enum"]
        self.assertIn("1girl", enums)
        self.assertNotIn("2people", enums)
        ollama.unload.assert_called_once_with("qwen3:8b")
        self.assertIsNone(result["unload_error"])

    def test_unload_on_model_failure(self):
        ollama = Mock()
        ollama.models.return_value = ["qwen3:8b"]
        ollama.chat.side_effect = RuntimeError("model failed")
        with self.assertRaisesRegex(RuntimeError, "model failed"):
            Compiler(self.catalog, ollama).generate("장면")
        ollama.unload.assert_called_once()

    def test_unload_failure_is_not_claimed_as_success(self):
        ollama = Mock()
        ollama.models.return_value = ["qwen3:8b"]
        ollama.chat.return_value = self.doc
        ollama.unload.side_effect = RuntimeError("unload failed")
        result = Compiler(self.catalog, ollama).generate("장면")
        self.assertEqual(result["unload_error"], "unload failed")

    def test_keep_model_option(self):
        ollama = Mock()
        ollama.models.return_value = ["qwen3:8b"]
        ollama.chat.return_value = self.doc
        Compiler(self.catalog, ollama).generate("장면", unload=False)
        ollama.unload.assert_not_called()
        self.assertEqual(ollama.chat.call_args.args[2], "5m")

    def test_http_boundary_and_invalid_requests(self):
        application = Application(self.path, "http://127.0.0.1:11434", Path(self.temp.name) / "sessions.json")
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(application))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(url + "/api/search?q=reading") as response:
            self.assertEqual(json.load(response)[0]["name"], "reading")
        for path, headers, expected in [
            ("/api/sessions", {"Host": "attacker.example"}, 403),
            ("/api/sessions", {"Origin": "https://example.com"}, 403),
            ("/state/sessions.json", {}, 404),
        ]:
            req = urllib.request.Request(url + path, headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(req)
            self.assertEqual(context.exception.code, expected)
            context.exception.close()
        for payload, origin, expected in [({}, None, 400), ([], None, 400), ({}, "https://example.com", 403)]:
            headers = {"Content-Type": "application/json"}
            if origin:
                headers["Origin"] = origin
            req = urllib.request.Request(url + "/api/generate", data=json.dumps(payload).encode(), headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(req)
            self.assertEqual(context.exception.code, expected)
            context.exception.close()

    def test_missing_user_csv_reports_setup_instructions(self):
        application = Application(Path(self.temp.name) / "missing.csv", "http://127.0.0.1:11434",
                                  Path(self.temp.name) / "sessions.json")
        self.assertIsNone(application.catalog)
        self.assertIn("data/SOURCE.md", application.catalog_error)
        self.assertIn("--csv", application.catalog_error)


if __name__ == "__main__":
    unittest.main()
