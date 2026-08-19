"""orchestrator 화면(screen.html)을 여는 로컬 웹 서버 — 파이프라인 실행 + 담당자 호출.

표준 라이브러리(http.server)만 쓴다 — 추가 설치 없이 동작.
- POST /runs                : 파이프라인 실행 시작 (run-pipeline.py를 백그라운드 스레드로)
- GET  /runs/current?since=N: 진행 이벤트 증분 조회 (화면이 1.5초 간격으로 폴링)
- GET  /summary             : orchestrator/result-summary.md 원문
- GET  /artifacts/...       : merge/result.xlsx · result.pdf 내려받기
- POST /call-agent          : (기존) 결과 보고 확인 — call-agent.py 호출
실행 상태·이벤트는 메모리에만 둔다 (저장 안 함 원칙 — 영구 기록은 logs/run_*/ 규약뿐).
API 키는 이 서버가 아니라 call-agent.py / run-pipeline.py 쪽에서 .env를 읽어 쓴다.

사용법: python3 orchestrator/server.py  (그다음 http://localhost:8788 을 브라우저로 연다)
"""

import importlib.util
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)
CALL_AGENT_PATH = os.path.join(BASE_DIR, "call-agent.py")
PIPELINE_PATH = os.path.join(BASE_DIR, "run-pipeline.py")
SCREEN_PATH = os.path.join(BASE_DIR, "screen.html")
SUMMARY_PATH = os.path.join(BASE_DIR, "result-summary.md")
PORT = 8788  # merge/server.py(8787)와 동시에 띄울 수 있게 포트를 달리 둔다

ARTIFACTS = {  # 내려받기 허용 목록 — 경로 조작 방지를 위해 고정 매핑만 쓴다
    "/artifacts/result.xlsx": (os.path.join(REPO_ROOT, "merge", "result.xlsx"),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "/artifacts/result.pdf": (os.path.join(REPO_ROOT, "merge", "result.pdf"),
                              "application/pdf"),
}


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


call_agent = _load_module("call_agent", CALL_AGENT_PATH)
pipeline = _load_module("run_pipeline_module", PIPELINE_PATH)


class RunState:
    """실행 1회의 관찰 상태 — 메모리에만 유지한다 (서버 재시작 시 소멸)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.month = None
        self.events = []       # run-pipeline.py의 on_event가 쌓는 진행 이벤트
        self.error = None      # 실행기 자체가 예외로 죽은 경우의 사유

    def start(self, month):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.month = month
            self.events = []
            self.error = None
        thread = threading.Thread(target=self._work, args=(month,), daemon=True)
        thread.start()
        return True

    def _work(self, month):
        try:
            pipeline.run_pipeline(month, on_event=self.on_event)
        except Exception as e:
            with self.lock:
                self.error = str(e)
        finally:
            with self.lock:
                self.running = False

    def on_event(self, event):
        with self.lock:
            self.events.append(event)

    def snapshot(self, since):
        with self.lock:
            return {
                "running": self.running,
                "month": self.month,
                "error": self.error,
                "events": [e for e in self.events if e["seq"] > since],
                "summary_ready": (not self.running) and os.path.exists(SUMMARY_PATH),
            }


run_state = RunState()


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type, download_name=None):
        if not os.path.exists(path):
            self._send_json(404, {"error": f"파일 없음: {os.path.relpath(path, REPO_ROOT)}"})
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path, _, query = self.path.partition("?")

        if path in ("/", "/screen.html"):
            self._send_file(SCREEN_PATH, "text/html; charset=utf-8")
            return

        if path == "/runs/current":
            since = 0
            match = re.search(r"(?:^|&)since=(\d+)", query)
            if match:
                since = int(match.group(1))
            self._send_json(200, run_state.snapshot(since))
            return

        if path == "/summary":
            self._send_file(SUMMARY_PATH, "text/markdown; charset=utf-8")
            return

        if path in ARTIFACTS:
            file_path, content_type = ARTIFACTS[path]
            self._send_file(file_path, content_type, download_name=os.path.basename(file_path))
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""

        if self.path == "/runs":
            try:
                month = json.loads(raw).get("month", "") if raw else ""
            except json.JSONDecodeError:
                month = ""
            if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
                self._send_json(400, {"error": "month는 YYYY-MM 형식이어야 합니다"})
                return
            if run_state.start(month):
                self._send_json(200, {"started": True, "month": month})
            else:
                self._send_json(409, {"error": f"이미 실행 중입니다 (대상 월 {run_state.month})"})
            return

        if self.path == "/call-agent":
            try:
                input_text = json.loads(raw).get("input", "") if raw else ""
            except json.JSONDecodeError:
                input_text = raw
            input_text = input_text.strip()
            if not input_text:
                self._send_json(200, {"result": "확인 대상 없음"})
                return
            try:
                result = call_agent.call_orchestrator_agent(input_text)
                self._send_json(200, {"result": result})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return

        self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    print(f"지휘 대시보드 서버 실행 중 — http://localhost:{PORT}")
    print("브라우저로 열어 사용하세요. 끄려면 Ctrl+C.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
