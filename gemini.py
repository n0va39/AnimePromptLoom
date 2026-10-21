"""Gemini REST adapter. No keys in URLs, responses, logs, or session files."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Gemini:
    provider = "gemini"
    label = "Gemini"

    def __init__(self, keys):
        self.keys = keys
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.usage = []

    def request(self, route, payload=None):
        # Only internally constructed routes; the API host is not configurable.
        req = urllib.request.Request(BASE_URL + route,
            data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode(),
            headers={"x-goog-api-key": self.keys.get(), "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=180 if payload else 20) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError("invalid response")
            return result
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            hint = {400: "모델과 생성 설정을 확인하세요.", 401: "API 키를 확인하세요.",
                    403: "키의 API 권한을 확인하세요.", 404: "모델 ID를 확인하세요.",
                    429: "요청 한도 또는 사용량 한도를 확인하세요."}.get(code, "잠시 후 다시 시도하세요.")
            raise RuntimeError(f"Gemini API 오류 ({code}). {hint}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("Gemini 연결에 실패했거나 시간이 초과되었습니다. 자동 재시도하지 않았습니다.") from None
        except (ValueError, UnicodeError):
            raise RuntimeError("Gemini 응답 형식을 읽을 수 없습니다.") from None

    @staticmethod
    def model_id(model):
        if not isinstance(model, str) or not re.fullmatch(r"gemini-[A-Za-z0-9._-]{1,140}", model):
            raise ValueError("Gemini 모델 ID를 선택하세요.")
        return model

    def models(self):
        found, token = [], ""
        while True:
            page = self.request("models?pageSize=1000" + ("&pageToken=" + urllib.parse.quote(token, safe="") if token else ""))
            for model in page.get("models", []):
                name = model.get("name", "").removeprefix("models/")
                if name.startswith("gemini-") and "generateContent" in model.get("supportedGenerationMethods", []):
                    found.append({"id": name, "input_limit": model.get("inputTokenLimit"), "output_limit": model.get("outputTokenLimit")})
            token = page.get("nextPageToken", "")
            if not token:
                return found

    def info(self, model):
        return self.request("models/" + self.model_id(model))

    def chat(self, model, messages, schema, settings):
        self.model_id(model)
        systems = [m["content"] for m in messages if m["role"] == "system"]
        contents = []
        for message in messages:
            if message["role"] == "system":
                continue
            role = "model" if message["role"] == "assistant" else "user"
            part = {"text": message["content"]}
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": role, "parts": [part]})
        config = {"responseMimeType": "application/json", "responseJsonSchema": schema,
                  "maxOutputTokens": settings["output_tokens"], "temperature": settings["temperature"]}
        if settings["thinking"] != "default":
            if model.startswith("gemini-2.5-"):
                config["thinkingConfig"] = {"thinkingBudget": 1024 if settings["thinking"] == "low" else 8192}
            elif re.match(r"gemini-[3-9]", model):
                config["thinkingConfig"] = {"thinkingLevel": settings["thinking"].upper()}
            else:
                raise ValueError("이 모델은 추론 설정을 '모델 기본'으로 사용하세요.")
        response = self.request("models/" + model + ":generateContent", {
            "systemInstruction": {"parts": [{"text": "\n\n".join(systems)}]},
            "contents": contents, "generationConfig": config})
        if response.get("promptFeedback", {}).get("blockReason"):
            raise ValueError("Gemini에서 요청을 차단했습니다. 제공자 정책과 요청 내용을 확인하세요.")
        items = response.get("candidates", [])
        if not items or items[0].get("finishReason") != "STOP":
            reason = items[0].get("finishReason") if items else ""
            raise ValueError("Gemini 출력 길이 한도에 도달했습니다. 출력 예산을 늘려 주세요." if reason == "MAX_TOKENS"
                             else "Gemini가 완전한 응답을 반환하지 않았습니다.")
        try:
            content = "".join(p.get("text", "") for p in items[0]["content"]["parts"] if not p.get("thought"))
            doc = json.loads(content)
        except (KeyError, TypeError, ValueError):
            raise ValueError("Gemini의 JSON 응답을 해석하지 못했습니다.") from None
        usage = response.get("usageMetadata", {})
        self.usage.append({key: usage[key] for key in ("promptTokenCount", "cachedContentTokenCount", "candidatesTokenCount", "thoughtsTokenCount", "totalTokenCount")
                           if type(usage.get(key)) is int})
        return doc
