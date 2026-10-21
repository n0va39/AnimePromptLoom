"""Synthetic fixtures only: no real API keys, cloud calls, CSV data, or GPU."""
import copy
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from app import Application, handler_for
from compiler import Catalog, SCHEMA, finalize
from credentials import KeyStore
from frontier import FrontierCompiler
from gemini import Gemini, NoRedirect
from sessions import Sessions, build_messages
from settings import validate_settings

FAKE_KEY = "synthetic-test-only-not-a-valid-api-key"
DOC = {"description":"A woman reads a book.", "translation":"한 여성이 책을 읽는다.",
       "subject_count":1, "people":{"female":1,"male":0,"other":0,"unspecified":0},
       "global_tags":["1girl"], "characters":[{"description":"A woman reads.","tags":["reading","invented_tag"]}],
       "environment_tags":["book"]}


class TemporaryStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.keys = KeyStore(self.root / "secrets")


class KeyTests(TemporaryStore):
    def test_memory_only_delete_and_status_no_secret(self):
        self.keys.save(FAKE_KEY, False)
        self.assertEqual(self.keys.get(), FAKE_KEY)
        self.assertFalse(self.keys.path.exists())
        self.assertNotIn(FAKE_KEY, json.dumps(self.keys.status()))
        self.assertEqual(self.keys.status()["storage"], "session")
        self.assertFalse(KeyStore(self.root / "secrets").status()["configured"])
        self.keys.delete()
        with self.assertRaises(ValueError):
            self.keys.get()

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_native_dpapi_disk_roundtrip_and_corruption(self):
        self.keys.save(FAKE_KEY)
        self.assertNotIn(FAKE_KEY.encode(), self.keys.path.read_bytes())
        restored = KeyStore(self.root / "secrets")
        self.assertEqual(restored.get(), FAKE_KEY)
        self.keys.path.write_bytes(b"corrupt ciphertext")
        with self.assertRaisesRegex(ValueError, "다시 저장"):
            restored.get()
        self.assertEqual(self.keys.path.read_bytes(), b"corrupt ciphertext")
        self.keys.save(FAKE_KEY, False)
        self.assertFalse(self.keys.path.exists())

    def test_failed_encryption_preserves_previous_key_no_plaintext_fallback(self):
        self.keys.save(FAKE_KEY, False)
        with patch("credentials.protect", side_effect=ValueError("encryption unavailable")):
            with self.assertRaises(ValueError):
                self.keys.save("replacement-synthetic-key-only")
        self.assertEqual(self.keys.get(), FAKE_KEY)
        self.assertFalse(self.keys.path.exists())

    def test_invalid_key_errors_do_not_echo_input(self):
        for value in (None, "short", FAKE_KEY + " contains spaces", "\x00" + FAKE_KEY):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ValueError) as error:
                    self.keys.save(value, False)
                self.assertNotIn(FAKE_KEY, str(error.exception))


class AdapterTests(TemporaryStore):
    def setUp(self):
        super().setUp()
        self.keys.save(FAKE_KEY, False)
        self.client = Gemini(self.keys)
        self.client.opener = Mock()
        self.settings = validate_settings({"mode":"frontier","model":"gemini-2.5-flash"})

    def response(self, document):
        self.client.opener.open.return_value = io.BytesIO(json.dumps(document).encode())

    def test_rest_payload_history_schema_and_cached_usage(self):
        self.response({"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"hidden","thought":True},{"text":json.dumps(DOC)}]}}],
                       "usageMetadata":{"promptTokenCount":3000,"cachedContentTokenCount":2000,"totalTokenCount":3400,"sensitive":"ignore"}})
        result = self.client.chat(self.settings["model"], [{"role":"system","content":"rules"},
            {"role":"user","content":"fixed"},{"role":"user","content":"before"},
            {"role":"assistant","content":"previous result"},{"role":"user","content":"modify"}], SCHEMA, self.settings)
        self.assertEqual(result, DOC)
        req = self.client.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent")
        self.assertNotIn(FAKE_KEY, req.full_url)
        self.assertEqual(req.get_header("X-goog-api-key"), FAKE_KEY)
        payload = json.loads(req.data)
        self.assertNotIn(FAKE_KEY, json.dumps(payload))
        self.assertEqual([c["role"] for c in payload["contents"]], ["user","model","user"])
        self.assertEqual(payload["systemInstruction"]["parts"][0]["text"], "rules")
        self.assertEqual(payload["generationConfig"]["responseJsonSchema"], SCHEMA)
        self.assertNotIn("thinkingConfig", payload["generationConfig"])
        self.assertEqual(self.client.usage, [{"promptTokenCount":3000,"cachedContentTokenCount":2000,"totalTokenCount":3400}])

    def test_http_errors_sanitized_without_retry_or_redirect(self):
        for code in (400, 403, 429, 500):
            self.client.opener.open.reset_mock()
            self.client.opener.open.side_effect = urllib.error.HTTPError("https://example.invalid/" + FAKE_KEY, code, FAKE_KEY, {}, io.BytesIO(FAKE_KEY.encode()))
            with self.assertRaises(RuntimeError) as caught:
                self.client.info("gemini-2.5-flash")
            self.assertNotIn(FAKE_KEY, str(caught.exception))
            self.assertIn(str(code), str(caught.exception))
            self.client.opener.open.assert_called_once()
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,"",{},"https://example.invalid"))

    def test_model_pagination_and_filter(self):
        self.client.request = Mock(side_effect=[{"models":[{"name":"models/gemini-example","supportedGenerationMethods":["generateContent"],"inputTokenLimit":32000},
                                               {"name":"models/embedding","supportedGenerationMethods":["embedContent"]}],"nextPageToken":"a/b"}, {"models":[]}])
        self.assertEqual(self.client.models(), [{"id":"gemini-example","input_limit":32000,"output_limit":None}])
        self.assertIn("a%2Fb", self.client.request.call_args.args[0])
        for value in ("../secret", "gemini-x?key=oops", "https://example.invalid"):
            with self.assertRaises(ValueError):
                self.client.info(value)

    def test_incomplete_blocked_or_invalid_output_fails(self):
        for response in ({"promptFeedback":{"blockReason":"SAFETY"}}, {"candidates":[{"finishReason":"MAX_TOKENS"}]},
                         {"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"not json"}]}}]}):
            self.response(response)
            with self.assertRaises(ValueError):
                self.client.chat(self.settings["model"], [{"role":"user","content":"scene"}], SCHEMA, self.settings)

    def test_thinking_config_model_family(self):
        for model, expected in (("gemini-2.5-flash", {"thinkingBudget":1024}), ("gemini-3-pro-preview", {"thinkingLevel":"LOW"})):
            self.response({"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":json.dumps(DOC)}]}}]})
            self.client.chat(model, [{"role":"user","content":"scene"}], SCHEMA, self.settings | {"thinking":"low"})
            payload = json.loads(self.client.opener.open.call_args.args[0].data)
            self.assertEqual(payload["generationConfig"]["thinkingConfig"], expected)


class ContextTests(TemporaryStore):
    def test_settings_validation_and_old_session_migration(self):
        for value in ({"api_key":FAKE_KEY}, {"history_limit":True}, {"history_limit":-1}, {"temperature":float('nan')}, {"model":"https://example.invalid"}):
            with self.assertRaises(ValueError):
                validate_settings(value)
        store = Sessions(self.root / "sessions.json")
        old = store.save({"name":"old"})
        del store.data["sessions"][old["id"]]["generation"]
        store._write()
        before = store.path.read_bytes()
        self.assertEqual(store.get(old["id"])["generation"]["mode"], "local")
        self.assertEqual(before, store.path.read_bytes())
        updated = store.save({"id":old["id"],"generation":{"mode":"frontier","memory":"window seat","history_limit":0}})
        self.assertEqual(Sessions(store.path).get(old["id"])["generation"], updated["generation"])

    def test_history_zero_extended_range_and_response_toggle(self):
        session = {"turns":[{"prompt":f"request {n}","result":{"text":f"answer {n}"}} for n in range(15)]}
        messages, report = build_messages("rules","now",session,history_limit=0,memory="permanent",include_responses=True)
        self.assertEqual(report["included_turns"],0)
        self.assertIn("permanent",messages[1]["content"])
        for include in (False, True):
            messages, report = build_messages("rules","now",session,history_limit=12,include_responses=include)
            self.assertEqual(report["included_turns"],12)
            self.assertEqual("answer 14" in json.dumps(messages),include)

    def test_renamed_character_is_masked_in_previous_response(self):
        profile = {"id":"one","label":"NewAlias","name":"","use_name":False,"designation":"첫 번째 여성","description":"coat"}
        session = {"character_profiles":[profile],"turns":[{"prompt":"OldAlias reads", "result":{"text":"OldAlias reads a book."},
            "context":{"character_profiles":[profile | {"label":"OldAlias"}]}}]}
        messages, _ = build_messages("rules","NewAlias sits",session,reference_contract=False,include_responses=True)
        text = json.dumps(messages)
        self.assertNotIn("OldAlias",text)
        self.assertNotIn("NewAlias",text)
        self.assertNotIn("[[Character",text)
        self.assertIn("Character 1 reads a book",text)


class IntegrationTests(TemporaryStore):
    def setUp(self):
        super().setUp()
        self.csv = self.root / "tags.csv"
        self.csv.write_text("name,category,post_count,description\n1girl,0,100,woman\nreading,0,80,reading\nbook,0,70,book\n",encoding="utf-8")
        self.app = Application(self.csv,"http://127.0.0.1:11434",self.root / "sessions.json")
        self.app.ollama = Mock()
        self.session = self.app.sessions.save({"generation":{"mode":"frontier","model":"gemini-2.5-flash"}})
        self.client = Mock(provider="gemini", usage=[{"promptTokenCount":2000,"cachedContentTokenCount":1000}])
        self.client.info.return_value = {"inputTokenLimit":100000,"outputTokenLimit":32768}
        self.client.chat.side_effect = lambda *args: copy.deepcopy(DOC)

    def test_frontier_generation_validates_locally_and_preserves_context(self):
        result = FrontierCompiler(Catalog(self.csv),self.client).generate("책 읽는 여성",self.session)
        self.assertEqual(result["provider"],"gemini")
        self.assertEqual(result["excluded"][0]["tag"],"invented_tag")
        self.assertEqual(result["usage"][0]["cachedContentTokenCount"],1000)
        self.assertFalse(result["unload_requested"])
        self.assertEqual(self.client.chat.call_count,1)
        self.client.info.return_value["inputTokenLimit"] = 2000
        with self.assertRaisesRegex(ValueError,"한도"):
            FrontierCompiler(Catalog(self.csv),self.client).generate("scene",self.session)
        self.assertEqual(self.client.chat.call_count,1)

    def test_cloud_dispatch_requires_selection_and_never_calls_ollama(self):
        payload = {"session_id":self.session["id"],"prompt":"책 읽는 여성"}
        with self.assertRaises(ValueError):
            self.app.create_job(payload)
        payload["allow_cloud"] = True
        with self.assertRaises(ValueError):
            self.app.create_job(payload)
        self.app.keys.save(FAKE_KEY,False)
        with patch("app.Gemini", return_value=self.client):
            job_id = self.app.create_job(payload)
            deadline = time.monotonic() + 3
            while self.app.active and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertEqual(self.app.jobs[job_id]["status"],"done")
        self.assertEqual(len(self.app.sessions.get(self.session["id"])["turns"]),1)
        self.assertNotIn(FAKE_KEY,self.app.sessions.path.read_text(encoding="utf-8"))
        self.app.status("frontier")
        self.assertFalse(self.app.ollama.mock_calls)

    def test_ollama_extended_dispatch_does_not_require_gemini_key(self):
        session = self.app.sessions.save({"id":self.session["id"],"generation":{"mode":"ollama_frontier","ollama_model":"glm-5.2:cloud"}})
        self.client.provider, self.client.cloud, self.client.unload = "ollama", True, True
        with patch("app.OllamaFrontier", return_value=self.client), patch("app.Gemini") as gemini:
            job_id = self.app.create_job({"session_id":session["id"],"prompt":"장면","allow_cloud":True})
            deadline = time.monotonic() + 3
            while self.app.active and time.monotonic() < deadline:
                time.sleep(.01)
            gemini.assert_not_called()
        self.assertEqual(self.app.jobs[job_id]["status"],"done")
        self.assertEqual(self.app.jobs[job_id]["result"]["model"],"glm-5.2:cloud")

    def test_frontier_character_ids_names_and_csv_stay_separate(self):
        profiles = [{"id":"a","label":"PrivateAlias","name":"","use_name":False,"designation":"첫 번째 여성","description":"reads"},
                    {"id":"b","label":"SecondAlias","name":"MyHero_v2","use_name":True,"designation":"","description":"stands"}]
        doc = copy.deepcopy(DOC)
        doc.update(subject_count=2,people={"female":2,"male":0,"other":0,"unspecified":0},
                   description="The first woman reads beside MyHero_v2.",translation="첫 번째 여성이 MyHero_v2 옆에서 책을 읽는다.")
        doc["characters"] = [{"profile_id":"b","description":"MyHero_v2 stands.","tags":[]},
                             {"profile_id":"a","description":"The first woman reads.","tags":["reading"]}]
        result = finalize(doc,self.app.catalog,"여성 두 명",profiles,reference_contract=False)
        self.assertIn("# Character 1\nMyHero_v2",result["text"])
        self.assertIn("# Character 2\nreading",result["text"])
        self.assertNotIn("PrivateAlias",result["text"])
        self.assertEqual(len(result["custom_names"]),1)
        doc["description"] = "PrivateAlias reads."
        with self.assertRaisesRegex(ValueError,"내부 이름"):
            finalize(doc,self.app.catalog,"scene",profiles,reference_contract=False)
        doc["description"] = "Two women read."
        doc["characters"][1]["profile_id"] = "b"
        with self.assertRaisesRegex(ValueError,"캐릭터 연결"):
            finalize(doc,self.app.catalog,"scene",profiles,reference_contract=False)

    def test_http_credentials_write_only_origin_guard_and_no_leak(self):
        server = ThreadingHTTPServer(("127.0.0.1",0),handler_for(self.app))
        worker = threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        def cleanup():
            server.shutdown(); server.server_close(); worker.join()
        self.addCleanup(cleanup)
        base = f"http://127.0.0.1:{server.server_port}"
        def request(path, data=None, extra=None):
            req = urllib.request.Request(base+path,data=None if data is None else json.dumps(data).encode(),
                headers={"Content-Type":"application/json","X-AnimePromptLoom":"1"} | (extra or {}))
            with urllib.request.urlopen(req) as reply:
                return json.load(reply)
        payload = {"action":"save","api_key":FAKE_KEY,"persistent":False}
        for headers in ({"Origin":"https://example.invalid"},{"X-AnimePromptLoom":""}):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                request("/api/credentials/gemini",payload,headers)
            self.assertEqual(caught.exception.code,403)
            caught.exception.close()
        saved = request("/api/credentials/gemini",payload)
        self.assertTrue(saved["configured"])
        self.assertNotIn(FAKE_KEY,json.dumps(saved))
        self.assertNotIn(FAKE_KEY,json.dumps(request("/api/status?mode=frontier")))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            request("/api/credentials/gemini")
        self.assertEqual(caught.exception.code,404)
        caught.exception.close()
        self.app.active = "synthetic-job"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            request("/api/credentials/gemini",{"action":"delete"})
        self.assertEqual(caught.exception.code,409)
        caught.exception.close()
        self.app.active = None
        self.assertFalse(request("/api/credentials/gemini",{"action":"delete"})["configured"])


if __name__ == "__main__":
    unittest.main()
