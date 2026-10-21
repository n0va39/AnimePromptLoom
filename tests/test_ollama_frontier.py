import copy
import json
import unittest
from unittest.mock import Mock
from compiler import Ollama, SCHEMA
from frontier import FrontierCompiler
from ollama_frontier import OllamaFrontier
from settings import validate_settings
import test_gemini


class OllamaProviderTests(unittest.TestCase):
    def test_model_list_all_families_and_cloud_opt_in(self):
        client = Ollama()
        client.request = Mock(return_value={"models":[{"name":"qwen3:8b"},{"name":"glm-local:latest"},
            {"name":"gemma:latest"},{"name":"glm-5.2:cloud"},{"name":"custom-alias","remote_model":"remote-model"}]})
        self.assertEqual(client.models(),["qwen3:8b","glm-local:latest","gemma:latest"])
        self.assertEqual(len(client.models(include_cloud=True)),5)

    def test_cloud_alias_cannot_generate_in_local_mode(self):
        client = Ollama()
        client.request = Mock(return_value={"remote_host":"https://ollama.com","remote_model":"glm-5.2"})
        with self.assertRaisesRegex(ValueError,"API"):
            client.chat("custom-alias",[{"role":"user","content":"private scene"}],0)
        self.assertEqual(client.request.call_count,1)
        self.assertEqual(client.request.call_args.args[0],"/api/show")

    def test_thinking_uses_metadata_no_unsupported_flags(self):
        self.assertEqual(Ollama.thinking_value({"thinking":{"values":["low","high"]}},"low"),"low")
        self.assertIsNone(Ollama.thinking_value({},"default"))
        self.assertIsNone(Ollama.thinking_value({"capabilities":["completion"]},False))
        self.assertTrue(Ollama.thinking_value({"capabilities":["thinking"]},True))
        with self.assertRaises(ValueError):
            Ollama.thinking_value({"thinking":{"values":[True,False]}},"low")
        client = Ollama()
        client.request = Mock(side_effect=[{"capabilities":["completion"]}, {"message":{"content":json.dumps(test_gemini.DOC)}}])
        client.chat("glm-local",[{"role":"user","content":"scene"}],0)
        self.assertNotIn("think",client.request.call_args.args[1])


class AdvancedOllamaTests(test_gemini.TemporaryStore):
    def setUp(self):
        super().setUp()
        self.ollama = Ollama()
        self.client = OllamaFrontier(self.ollama)
        self.settings = validate_settings({"mode":"ollama_frontier","ollama_model":"glm-5.2:cloud"})

    def setup_model(self, cloud=True, content=None):
        model = self.settings["ollama_model"]
        info = {"capabilities":["completion","thinking"],"thinking":{"values":["low","high"]},
                "model_info":{"glm.context_length":100000}}
        if cloud:
            info.update(remote_host="https://ollama.com",remote_model="glm-5.2")
        calls = []
        def request(route,payload=None,**kwargs):
            calls.append((route,payload))
            if route == "/api/tags": return {"models":[{"name":model}]}
            if route == "/api/show": return info
            if route == "/api/chat": return {"message":{"content":content or json.dumps(test_gemini.DOC)},"prompt_eval_count":200,"eval_count":50}
            return {}
        self.ollama.request = Mock(side_effect=request)
        return calls

    def test_status_classifies_cloud_aliases_without_generating(self):
        from app import Application
        app = Application(self.root / "missing.csv", "http://127.0.0.1:11434", self.root / "sessions.json")
        app.ollama.request = Mock(return_value={"models": [
            {"name": "local-model"}, {"name": "remote-alias", "remote_model": "glm-5.2"}]})
        status = app.status("ollama_frontier")
        self.assertEqual(status["models"], ["local-model", "remote-alias"])
        self.assertEqual(status["cloud_models"], ["remote-alias"])
        app.ollama.request.assert_called_once_with("/api/tags", timeout=5)

    def test_local_execution_blocks_cloud_alias_before_prompt_submission(self):
        self.settings["ollama_model"] = "custom-alias"
        calls = self.setup_model(cloud=True)
        self.client.allow_cloud = False
        with self.assertRaisesRegex(ValueError, "API"):
            self.client.info("custom-alias")
        self.assertFalse(any(route == "/api/chat" for route, _ in calls))

    def test_local_execution_with_custom_context_remains_available(self):
        self.settings["ollama_model"] = "glm-local:latest"
        calls = self.setup_model(cloud=False)
        self.client.allow_cloud = False
        self.client.info(self.settings["ollama_model"])
        self.client.chat(self.settings["ollama_model"], [{"role": "system", "content": "rules"}], SCHEMA, self.settings)
        self.assertEqual(calls[-1][0], "/api/chat")
        self.assertFalse(self.client.cloud)

    def test_cloud_omits_structured_format_and_local_memory_options(self):
        calls = self.setup_model()
        self.client.info(self.settings["ollama_model"])
        doc = self.client.chat(self.settings["ollama_model"],[{"role":"system","content":"rules"},{"role":"user","content":"scene"}],SCHEMA,self.settings)
        self.assertEqual(doc,test_gemini.DOC)
        payload = calls[-1][1]
        self.assertNotIn("format",payload)
        self.assertNotIn("keep_alive",payload)
        self.assertNotIn("num_ctx",payload["options"])
        self.assertNotIn("think",payload)
        self.assertIn('"subject_count"',payload["messages"][0]["content"])
        self.client.release(self.settings["ollama_model"])
        self.assertEqual(calls[-1][0],"/api/chat")

    def test_local_options_context_and_release(self):
        self.settings["ollama_model"] = "glm-local:latest"
        calls = self.setup_model(cloud=False)
        self.client.info(self.settings["ollama_model"])
        self.client.chat(self.settings["ollama_model"],[{"role":"system","content":"rules"},{"role":"user","content":"scene"}],SCHEMA,self.settings | {"thinking":"high"})
        payload = calls[-1][1]
        self.assertEqual(payload["format"],SCHEMA)
        self.assertEqual(payload["think"],"high")
        self.assertEqual(payload["options"]["num_ctx"],self.settings["input_budget"]+self.settings["output_tokens"])
        self.client.release(self.settings["ollama_model"])
        self.assertEqual(calls[-1],("/api/generate",{"model":"glm-local:latest","keep_alive":0,"stream":False}))

    def test_malformed_output_no_silent_retry(self):
        calls = self.setup_model(content="prose instead of JSON")
        self.client.info(self.settings["ollama_model"])
        with self.assertRaisesRegex(ValueError,"자동 재시도"):
            self.client.chat(self.settings["ollama_model"],[{"role":"system","content":"rules"}],SCHEMA,self.settings)
        self.assertEqual(sum(route=="/api/chat" for route,_ in calls),1)

    def test_full_generation_cloud_and_local_failure_release(self):
        from compiler import Catalog
        csv = self.root / "tags.csv"
        csv.write_text("name,category,post_count,description\n1girl,0,100,woman\nreading,0,80,reading\nbook,0,70,book\n",encoding="utf-8")
        self.setup_model()
        session = {"generation":self.settings,"character_profiles":[],"turns":[]}
        result = FrontierCompiler(Catalog(csv),self.client).generate("여성이 책을 읽는다",session)
        self.assertEqual(result["provider"],"ollama")
        self.assertTrue(result["remote"])
        self.assertFalse(result["unload_requested"])
        self.assertEqual(result["usage"],[{"promptTokenCount":200,"candidatesTokenCount":50}])
        self.settings["ollama_model"] = "glm-local:latest"
        calls = self.setup_model(cloud=False,content="broken JSON")
        with self.assertRaises(ValueError):
            FrontierCompiler(Catalog(csv),self.client).generate("장면",session)
        self.assertEqual(calls[-1][0],"/api/generate")


if __name__ == "__main__":
    unittest.main()
