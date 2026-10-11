import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import test_compiler
from characters import validate_profiles, mask_labels, generation_schema, render_references
from compiler import Compiler, SCHEMA, finalize
from sessions import Sessions, build_messages


def profiles():
    return [{"id": "a", "label": "유나", "name": "Yuna", "use_name": False,
             "designation": "첫 번째 여성", "description": "검은 단발머리. 빨간 코트."},
            {"id": "b", "label": "민수", "name": "MyHero_v2", "use_name": True,
             "designation": "오른쪽 남성", "description": "파란 재킷."}]


class CharacterSettingsTests(unittest.TestCase):
    def test_roundtrip_defaults_and_legacy_preservation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sessions.json"
            original = {"version": 1, "sessions": {"legacy": {"id": "legacy", "name": "기존 대화", "characters": "유나: 검은 단발머리", "background": "도서관", "turns": [], "updated_at": "2026"}}}
            path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            store = Sessions(path)
            migrated = store.get("legacy")
            self.assertFalse(migrated["character_profiles"][0]["use_name"])
            self.assertEqual(migrated["character_profiles"][0]["label"], "유나")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
            saved = store.save({"id": "legacy", "name": "기존 대화", "background": "도서관", "character_profiles": profiles()})
            self.assertEqual(saved["characters"], "유나: 검은 단발머리")
            self.assertEqual(Sessions(path).get("legacy")["character_profiles"], profiles())
            store.save({"id": "legacy", "character_profiles": []})
            self.assertEqual(store.get("legacy")["character_profiles"], [])

    def test_private_names_never_enter_model_context_or_history(self):
        session = {"character_profiles": profiles(), "background": "유나와 민수의 도서관", "turns": [
            {"prompt": "유나가 책을 읽어", "result": {"text": "Yuna reads; an invented old name."}}]}
        messages, info = build_messages("base", "유나와 민수가 대화해", session)
        text = json.dumps(messages, ensure_ascii=False)
        for private in ("유나", "민수", "Yuna", "invented old name"):
            self.assertNotIn(private, text)
        self.assertIn("MyHero_v2", text)
        self.assertIn("첫 번째 여성", text)
        self.assertTrue(messages[-1]["content"].startswith("Character 1와 Character 2가 대화해\n"))
        self.assertEqual(info["included_turns"], 1)

    def test_free_korean_designator_and_default_ordinal(self):
        items = profiles()
        items[0]["designation"] = ""
        items[1].update(use_name=False, designation="왼쪽의 사람")
        messages, _ = build_messages("base", "첫 번째 여성이 왼쪽의 사람을 바라봐", {"character_profiles": items})
        context = json.loads(messages[1]["content"].split("\n", 1)[1])
        self.assertEqual(context["characters"][0]["designation"], "첫 번째 캐릭터")
        self.assertEqual(context["characters"][1]["designation"], "왼쪽의 사람")
        self.assertIn("좌우 위치나 구도를 뜻하지 않는다", messages[0]["content"])

    def test_latin_labels_are_not_substring_replaced(self):
        self.assertEqual(mask_labels("Ann celebrates an anniversary.", [{"label":"Ann", "name":"", "use_name":False}]),
                         "Character 1 celebrates an anniversary.")

    def test_enabled_name_requires_explicit_value(self):
        with self.assertRaises(ValueError):
            validate_profiles([{"use_name": True}])
        with self.assertRaises(ValueError):
            validate_profiles([{"label": "유나"}, {"label": "유나"}])
        self.assertFalse(validate_profiles([{}])[0]["use_name"])

    def test_schema_tracks_profile_ids_without_mutating_base(self):
        schema = generation_schema(SCHEMA, profiles())
        self.assertEqual(schema["properties"]["characters"]["items"]["properties"]["profile_id"]["enum"], ["", "a", "b"])
        self.assertNotIn("profile_id", SCHEMA["properties"]["characters"]["items"]["properties"])


class CharacterOutputTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_compiler.CompilerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def bound_doc(self):
        doc = copy.deepcopy(self.fixture.doc)
        doc.update(description="[[Character 1]] reads a book.", translation="[[Character 1]]이 책을 읽는다.")
        doc["characters"][0].update(profile_id="a", designation_en="the first woman", designation_ko="첫 번째 여성")
        return doc

    def test_lora_token_kept_exactly_and_scoped_by_id_not_position(self):
        doc = self.bound_doc()
        doc.update(description="[[Character 1]] looks at [[Character 2]].", translation="[[Character 1]]이 [[Character 2]]를 바라본다.")
        doc["characters"] = [{"profile_id": "b", "description": "A man.", "tags": ["blue_jacket", "myhero_v2"]},
                             {"profile_id": "a", "description": "The first woman.", "designation_en": "the first woman", "designation_ko": "첫 번째 여성", "tags": ["red_coat", "character_(series)"]}]
        result = finalize(doc, self.fixture.catalog, "두 사람", profiles())
        self.assertIn("# Character 1\nMyHero_v2, blue jacket", result["text"])
        self.assertIn("The first woman looks at MyHero_v2.", result["text"])
        self.assertNotIn("character ", result["text"])
        self.assertEqual(result["custom_names"][0]["name"], "MyHero_v2")
        self.assertFalse(any(tag["name"] == "MyHero_v2" for tag in result["verified"]))

    def test_csv_name_explicit_use_is_verified(self):
        items = profiles()[:1]
        items[0].update(use_name=True, name="character_(series)")
        doc = self.bound_doc()
        result = finalize(doc, self.fixture.catalog, "인물", items)
        self.assertIn("character_(series)", result["text"])
        self.assertEqual(result["custom_names"], [])
        self.assertTrue(any(tag.get("source") == "user_name" for tag in result["verified"]))

    def test_disabled_private_name_leak_is_rejected(self):
        doc = copy.deepcopy(self.fixture.doc)
        doc["description"] = "Yuna reads a book."
        with self.assertRaisesRegex(ValueError, "내부 이름"):
            finalize(doc, self.fixture.catalog, "인물", profiles())

    def test_generate_passes_profile_schema_and_name_policy(self):
        doc = self.bound_doc()
        ollama = Mock()
        ollama.models.return_value = ["qwen3:8b"]
        ollama.chat.return_value = doc
        result = Compiler(self.fixture.catalog, ollama).generate("유나가 책을 읽어", session={"character_profiles": profiles()})
        self.assertIn("profile_id", ollama.chat.call_args.kwargs["schema"]["properties"]["characters"]["items"]["required"])
        self.assertNotIn("유나", json.dumps(ollama.chat.call_args.args[1], ensure_ascii=False))
        self.assertNotIn("MyHero_v2", result["positive"])

    def test_llm_translated_designation_is_bound_in_both_languages(self):
        doc = self.bound_doc()
        doc["characters"][0].update(designation_en="the woman on the left", designation_ko="왼쪽의 여성")
        result = finalize(doc, self.fixture.catalog, "책을 읽어", profiles())
        self.assertIn("The woman on the left reads a book.", result["positive"])
        self.assertIn("왼쪽의 여성이 책을 읽는다.", result["text"])
        self.assertNotIn("[[", result["text"])
        self.assertFalse(any("left" in t["rendered"] for t in result["verified"]))

    def test_missing_reference_or_duplicate_identity_is_not_published(self):
        doc = self.bound_doc()
        doc["description"] = "A woman reads."
        with self.assertRaisesRegex(ValueError, "지칭을 묘사에서 누락"):
            render_references(doc, profiles())
        doc = self.bound_doc()
        doc["characters"].append(copy.deepcopy(doc["characters"][0]))
        with self.assertRaisesRegex(ValueError, "설정 연결"):
            render_references(doc, profiles())

    def test_already_expanded_designation_and_exact_name_are_accepted(self):
        doc = self.bound_doc()
        doc["description"] = "The first woman looks at MyHero_v2."
        doc["translation"] = "[[Character 1]]이 [[Character 2]]를 바라본다."
        doc["characters"].append({"profile_id":"b", "description":"MyHero_v2 looks at her.", "tags":[]})
        result = finalize(doc, self.fixture.catalog, "대화", profiles())
        self.assertEqual(result["document"]["description"], doc["description"])
        self.assertIn("첫 번째 여성이 MyHero_v2를 바라본다.", result["text"])


if __name__ == "__main__":
    unittest.main()
