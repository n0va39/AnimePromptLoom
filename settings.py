"""Validated per-conversation options; credentials never belong here."""
import math
import re

DEFAULTS = dict(mode="local", model="", ollama_model="", history_limit=12, input_budget=32000,
                output_tokens=8192, temperature=0.7, thinking="default",
                include_responses=True, elaboration="restrained", memory="", instruction="")


def validate_settings(value=None):
    if value is None:
        return DEFAULTS.copy()
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("생성 설정에 지원하지 않는 항목이 있습니다.")
    result = DEFAULTS | value
    for key, choices in {"mode": ("local", "frontier", "ollama_frontier"), "thinking": ("default", "low", "high"),
                         "elaboration": ("restrained", "expanded")}.items():
        if result[key] not in choices:
            raise ValueError(f"{key} 설정이 올바르지 않습니다.")
    for key, low, high in (("history_limit", 0, 100), ("input_budget", 4096, 200000), ("output_tokens", 512, 32768)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f"{key}는 {low}~{high} 범위의 정수여야 합니다.")
    if type(result["temperature"]) not in (int, float) or not math.isfinite(result["temperature"]) or not 0 <= result["temperature"] <= 2:
        raise ValueError("생성 강도는 0~2 사이여야 합니다.")
    if type(result["include_responses"]) is not bool:
        raise ValueError("이전 응답 포함 설정이 올바르지 않습니다.")
    for key, limit in (("model", 150), ("ollama_model", 150), ("memory", 24000), ("instruction", 12000)):
        if not isinstance(result[key], str) or len(result[key]) > limit:
            raise ValueError(f"{key}는 {limit}자 이하의 문자열이어야 합니다.")
    result["model"] = result["model"].strip()
    result["ollama_model"] = result["ollama_model"].strip()
    if result["ollama_model"] and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", result["ollama_model"]) or "://" in result["ollama_model"]):
        raise ValueError("Ollama 모델 이름 형식이 올바르지 않습니다.")
    if result["model"] and not re.fullmatch(r"gemini-[A-Za-z0-9._-]+", result["model"]):
        raise ValueError("Gemini 모델 ID 형식이 올바르지 않습니다.")
    return result
