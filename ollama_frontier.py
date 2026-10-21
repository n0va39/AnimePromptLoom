"""Advanced context mode for registered Ollama local and cloud chat models."""
import copy
import json
import re
from compiler import check_document


class OllamaFrontier:
    provider = "ollama"
    label = "Ollama"

    def __init__(self, ollama, unload=True, allow_cloud=True):
        self.ollama, self.unload = ollama, unload
        self.allow_cloud = allow_cloud
        self.usage = []
        self.cloud = False
        self.metadata = {}
        self.started = False

    def info(self, model):
        if model not in self.ollama.models(include_cloud=True):
            raise ValueError("Ollama에 등록된 모델을 선택하세요. 클라우드 모델은 Ollama 로그인과 등록이 필요합니다.")
        self.metadata = self.ollama.info(model)
        capabilities = self.metadata.get("capabilities", [])
        if capabilities and "completion" not in capabilities:
            raise ValueError("선택 모델은 텍스트 대화를 지원하지 않습니다.")
        self.cloud = self.ollama.is_cloud(model, self.metadata)
        if self.cloud and not self.allow_cloud:
            raise ValueError("이 모델은 클라우드에서 실행됩니다. 설정에서 API 방식을 선택하세요.")
        limits = [v for k, v in self.metadata.get("model_info", {}).items() if k.endswith(".context_length") and type(v) is int and v > 0]
        return {"contextTokenLimit": min(limits)} if limits else {}

    def chat(self, model, messages, schema, settings):
        think = self.ollama.thinking_value(self.metadata, settings["thinking"])
        messages = copy.deepcopy(messages)
        # Cloud currently lacks structured-output support. Give the schema as
        # instructions, then validate the returned document locally, without retry.
        messages[0]["content"] += "\nReturn only a JSON object matching this schema, no code fences:\n" + json.dumps(schema, ensure_ascii=False)
        payload = {"model":model,"messages":messages,"stream":False,
                   "options":{"temperature":settings["temperature"],"num_predict":settings["output_tokens"]}}
        if not self.cloud:
            payload.update(format=schema, keep_alive=0 if self.unload else "5m")
            payload["options"]["num_ctx"] = settings["input_budget"] + settings["output_tokens"]
        if think is not None:
            payload["think"] = think
        self.started = True
        response = self.ollama.request("/api/chat",payload)
        if response.get("done_reason") == "length":
            raise ValueError("Ollama 출력 길이 한도에 도달했습니다. 출력 예산을 늘려 주세요.")
        try:
            content = response["message"]["content"].strip()
            fence = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", content, re.S)
            doc = json.loads(fence[1] if fence else content)
        except (KeyError, TypeError, ValueError):
            raise ValueError("Ollama 모델이 유효한 JSON을 반환하지 않았습니다. 다른 모델이나 설정을 사용하세요. 자동 재시도하지 않았습니다.") from None
        self.usage.append({target:response[source] for source,target in (("prompt_eval_count","promptTokenCount"),("eval_count","candidatesTokenCount")) if type(response.get(source)) is int})
        return check_document(doc)

    def release(self, model):
        if self.started and self.unload and not self.cloud:
            self.ollama.unload(model)
