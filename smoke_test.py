"""Optional real-Qwen context/lifecycle check, not a prompt-quality test."""
import json
import tempfile
from pathlib import Path

from compiler import Catalog, Compiler, DEFAULT_CSV, Ollama
from sessions import Sessions


def main():
    ollama = Ollama()
    compiler = Compiler(Catalog(DEFAULT_CSV), ollama)
    evidence = []
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "sessions.json"
        store = Sessions(path)
        session = store.save({"name": "컨텍스트 검증", "characters": "유나: 성인 여성. 검은 단발머리, 녹색 눈, 흰 셔츠.",
                              "background": "작은 도서관. 창가의 나무 책상."})
        try:
            for index, request in enumerate(("유나가 책을 읽는 모습.", "같은 인물이 고개를 들어 옆을 바라보는 모습.")):
                snapshot = store.get(session["id"])
                result = compiler.generate(request, session=snapshot, unload=index == 1)
                assert result["context"]["pinned_context"]
                assert result["context"]["included_turns"] == index
                assert not result["unload_error"]
                assert all(tag["name"].replace("_", " ") in compiler.catalog.tags for tag in result["verified"])
                store.append(session["id"], request, result, snapshot)
                loaded = ollama.request("/api/ps", timeout=10).get("models", [])
                model_loaded = any(model["name"] == "qwen3:8b" for model in loaded)
                assert model_loaded == (index == 0)
                evidence.append({"turn": index + 1, "result": result, "model_loaded_after": model_loaded})
                print(f"PASS turn {index + 1}: {result['seconds']}s, history={index}, loaded={model_loaded}", flush=True)
            restored = Sessions(path).get(session["id"])
            assert len(restored["turns"]) == 2
            assert restored["characters"] == session["characters"]
        finally:
            ollama.unload("qwen3:8b")
    Path("runtime-validation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
