"""Configurable frontier mode, independent of Ollama's lifecycle and scaffolding."""
import time
import json
from characters import generation_schema
from compiler import ROOT, SCHEMA, finalize
from sessions import build_messages, estimate_tokens
from settings import validate_settings


class FrontierCompiler:
    def __init__(self, catalog, client):
        self.catalog, self.client = catalog, client

    def generate(self, request, session, progress=None):
        started = time.monotonic()
        settings = validate_settings(session.get("generation"))
        provider = getattr(self.client, "provider", "gemini")
        label = "Gemini" if provider == "gemini" else "Ollama"
        model = settings["model"] if provider == "gemini" else settings["ollama_model"]
        if not isinstance(request, str) or not request.strip() or len(request) > 6000:
            raise ValueError("장면 요청을 1~6,000자로 입력해 주세요.")
        report = progress or (lambda _: None)
        report(f"{label} 모델의 입력·출력 한도를 확인하고 있습니다.")
        info = self.client.info(model)
        for field, limit in (("input_budget", "inputTokenLimit"), ("output_tokens", "outputTokenLimit")):
            if type(info.get(limit)) is int and settings[field] > info[limit]:
                raise ValueError(f"{field} 설정이 선택 모델 한도({info[limit]:,})보다 큽니다.")
        if type(info.get("contextTokenLimit")) is int and settings["input_budget"] + settings["output_tokens"] > info["contextTokenLimit"]:
            raise ValueError("입력 예산과 최대 출력의 합이 Ollama 모델의 컨텍스트 한도를 초과합니다.")
        profiles = session.get("character_profiles", [])
        schema = generation_schema(SCHEMA, profiles, reference_contract=False)
        system = (ROOT / "frontier_prompt.txt").read_text(encoding="utf-8")
        system += ("\nYou may add contextually appropriate scene details when unspecified. Never override explicit features or constraints."
                   if settings["elaboration"] == "expanded" else
                   "\nPreserve the requested scene without adding unrequested objects, background, weather, lighting, or fixed character features.")
        # Reserve input space for the structured response schema and provider template.
        overhead = estimate_tokens([{"content": json.dumps(schema, ensure_ascii=False)}]) * (2 if provider == "ollama" else 1) + 512
        messages, context = build_messages(system, request, session,
            input_budget=settings["input_budget"] - overhead, history_limit=settings["history_limit"],
            reference_contract=False, include_responses=settings["include_responses"],
            memory=settings["memory"], instruction=settings["instruction"])
        context.update(input_budget=settings["input_budget"], schema_overhead=overhead,
                       estimated_input_tokens=context["estimated_input_tokens"] + overhead)
        report(f"선택한 설정과 대화 컨텍스트를 {label}로 보내고 있습니다.")
        unload_error = None
        try:
            doc = self.client.chat(model, messages, schema, settings)
            report("응답 태그를 로컬 CSV 전체와 대조하고 있습니다.")
            result = finalize(doc, self.catalog, request, profiles, reference_contract=False)
        finally:
            if provider == "ollama":
                try:
                    self.client.release(model)
                except RuntimeError as exc:
                    unload_error = str(exc)
        result.update(model=model, provider=provider, mode=settings["mode"], context=context,
                      generation=settings, usage=self.client.usage, thinking=settings["thinking"],
                      refined=False, draft_unverified=[], unload_requested=provider == "ollama" and self.client.unload and not self.client.cloud, unload_error=unload_error,
                      remote=provider == "gemini" or self.client.cloud,
                      seconds=round(time.monotonic() - started, 1))
        return result
