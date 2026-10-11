"""Standalone localhost web application. Python 3.10+, no third-party packages."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from compiler import Catalog, Compiler, DEFAULT_CSV, Ollama, ROOT
from sessions import Sessions


class Application:
    def __init__(self, csv_path: Path, ollama_url: str, state_path: Path = ROOT / "state" / "sessions.json"):
        self.catalog = None
        self.catalog_error = None
        try:
            self.catalog = Catalog(csv_path)
        except FileNotFoundError:
            self.catalog_error = "태그 CSV가 없습니다. data/SOURCE.md를 참고해 data/danbooru_tags_classified.csv를 준비하거나 --csv로 경로를 지정하세요."
        except (OSError, ValueError) as exc:
            self.catalog_error = str(exc)
        self.ollama = Ollama(ollama_url)
        self.jobs = {}
        self.lock = threading.Lock()
        self.active = None
        self.stopping = False
        self.sessions = Sessions(state_path)

    def status(self):
        models, connection_error = [], None
        try:
            models = self.ollama.models()
        except RuntimeError as exc:
            connection_error = str(exc)
        return {"models": models, "connection_error": connection_error,
                "catalog": self.catalog.info() if self.catalog else None,
                "catalog_error": self.catalog_error, "active_job": self.active}

    def create_job(self, payload):
        prompt, model = payload.get("prompt"), payload.get("model")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 6000:
            raise ValueError("장면 요청을 1~6,000자로 입력해 주세요.")
        if not isinstance(model, str) or not model or len(model) > 150:
            raise ValueError("설치된 Qwen 모델을 선택해 주세요.")
        for key, default in (("unload", True), ("thinking", False), ("refine", False)):
            if type(payload.get(key, default)) is not bool:
                raise ValueError(f"{key} 설정이 올바르지 않습니다.")
        session = self.sessions.get(payload.get("session_id"))
        if self.catalog is None:
            raise ValueError(f"CSV를 읽을 수 없습니다: {self.catalog_error}")
        with self.lock:
            if self.stopping:
                raise BlockingIOError("앱이 종료 중입니다.")
            if self.active:
                raise BlockingIOError("이미 생성 중입니다. 현재 작업이 끝날 때까지 기다려 주세요.")
            job_id = uuid.uuid4().hex
            self.active = job_id
            self.jobs[job_id] = {"status": "running", "stage": "생성을 준비하고 있습니다."}
            while len(self.jobs) > 10:
                del self.jobs[next(iter(self.jobs))]

        def progress(stage):
            with self.lock:
                self.jobs[job_id]["stage"] = stage

        def run():
            try:
                result = Compiler(self.catalog, self.ollama).generate(prompt, model, payload.get("unload", True), progress,
                            session=session, thinking=payload.get("thinking", False), refine=payload.get("refine", False))
                try:
                    self.sessions.append(session["id"], prompt, result, session)
                except OSError as exc:
                    result["save_error"] = f"대화 저장 실패: {exc}. 결과를 복사해 보관해 주세요."
                result["session_id"] = session["id"]
                final = {"status": "done", "result": result}
            except Exception as exc:
                final = {"status": "error", "error": str(exc)}
            with self.lock:
                self.jobs[job_id] = final
                self.active = None

        threading.Thread(target=run, daemon=True).start()
        return job_id


def handler_for(application):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Do not write user prompt text to logs.
            pass

        def send_data(self, status, body, content_type="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; connect-src 'self'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def local_request(self):
            allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            origin = self.headers.get("Origin")
            return self.headers.get("Host") in allowed_hosts and (not origin or origin in {f"http://{host}" for host in allowed_hosts})

        def do_GET(self):
            if not self.local_request():
                return self.send_data(403, {"error": "로컬 앱에서만 접근할 수 있습니다."})
            url = urlsplit(self.path)
            static = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if url.path in static:
                name, content_type = static[url.path]
                return self.send_data(200, (ROOT / "web" / name).read_bytes(), content_type)
            if url.path == "/api/status":
                return self.send_data(200, application.status())
            if url.path == "/api/sessions":
                return self.send_data(200, application.sessions.list())
            if url.path.startswith("/api/sessions/"):
                try:
                    session = application.sessions.get(url.path.rsplit("/", 1)[-1])
                    session["total_turns"] = len(session["turns"])
                    session["turns"] = session["turns"][-20:]
                    return self.send_data(200, session)
                except ValueError as exc:
                    return self.send_data(404, {"error": str(exc)})
            if url.path == "/api/search":
                query = parse_qs(url.query).get("q", [""])[0][:100]
                results = application.catalog.search(query) if application.catalog else []
                return self.send_data(200, [asdict(tag) for tag in results])
            if url.path.startswith("/api/jobs/"):
                with application.lock:
                    job = application.jobs.get(url.path.rsplit("/", 1)[-1])
                    return self.send_data(200 if job else 404, job or {"error": "작업을 찾을 수 없습니다."})
            return self.send_data(404, {"error": "경로를 찾을 수 없습니다."})

        def do_POST(self):
            if not self.local_request() or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send_data(403, {"error": "로컬 앱의 JSON 요청만 허용됩니다."})
            if self.path == "/api/shutdown":
                with application.lock:
                    if application.active:
                        return self.send_data(409, {"error": "생성 완료 후 앱을 종료해 주세요."})
                    application.stopping = True
                self.send_data(200, {"stopped": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path not in {"/api/generate", "/api/sessions"}:
                return self.send_data(404, {"error": "경로를 찾을 수 없습니다."})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 256000:
                    raise ValueError("요청 크기가 올바르지 않습니다.")
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError("JSON 객체가 필요합니다.")
                if self.path == "/api/sessions":
                    with application.lock:
                        if application.active or application.stopping:
                            raise BlockingIOError("생성 완료 후 대화 설정을 변경해 주세요.")
                        return self.send_data(200, application.sessions.save(payload))
                job_id = application.create_job(payload)
                return self.send_data(202, {"job_id": job_id})
            except BlockingIOError as exc:
                return self.send_data(409, {"error": str(exc)})
            except (ValueError, UnicodeDecodeError) as exc:
                return self.send_data(400, {"error": str(exc)})

    return Handler


def main():
    parser = argparse.ArgumentParser(description="AnimePromptLoom — local prompt workspace (development)")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--state", type=Path, default=ROOT / "state" / "sessions.json")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--start-ollama", action="store_true", help="Start installed Ollama if its local server is stopped")
    args = parser.parse_args()
    endpoint = urlsplit(args.ollama_url)
    if endpoint.scheme != "http" or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("Ollama 주소는 로컬 HTTP 주소여야 합니다.")
    application = Application(args.csv, args.ollama_url, args.state)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(application))
    owned_ollama = None
    try:
        if args.start_ollama and args.ollama_url.rstrip("/") == "http://127.0.0.1:11434":
            try:
                application.ollama.models()
            except RuntimeError:
                executable = shutil.which("ollama")
                if executable:
                    owned_ollama = subprocess.Popen([executable, "serve"], stdout=subprocess.DEVNULL,
                                                    stderr=subprocess.DEVNULL,
                                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    for _ in range(20):
                        if owned_ollama.poll() is not None:
                            break
                        time.sleep(0.5)
                        try:
                            application.ollama.models()
                            break
                        except RuntimeError:
                            pass
        url = f"http://127.0.0.1:{args.port}"
        print(f"AnimePromptLoom (development): {url}\nStop: Ctrl+C", flush=True)
        if args.open:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if owned_ollama is not None and owned_ollama.poll() is None:
            owned_ollama.terminate()
            owned_ollama.wait(timeout=10)


if __name__ == "__main__":
    main()
