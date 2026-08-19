"""orchestrator 화면(screen.html)과 call-agent.py를 이어주는 가벼운 로컬 서버.

표준 라이브러리(http.server)만 쓴다 — 추가 설치 없이 동작.
screen.html의 "확인하기" 버튼이 POST /call-agent 로 입력 글을 보내면,
call-agent.py의 call_orchestrator_agent()를 그대로 불러 처리 결과를 돌려준다.
API 키는 이 서버가 아니라 call-agent.py 쪽에서 .env를 읽어 쓴다.

사용법: python3 orchestrator/server.py  (그다음 orchestrator/screen.html을 브라우저로 연다)
"""

import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CALL_AGENT_PATH = os.path.join(BASE_DIR, "call-agent.py")
SCREEN_PATH = os.path.join(BASE_DIR, "screen.html")
PORT = 8788  # merge/server.py(8787)와 동시에 띄울 수 있게 포트를 달리 둔다


def _load_call_agent():
    spec = importlib.util.spec_from_file_location("call_agent", CALL_AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


call_agent = _load_call_agent()


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        # 브라우저로 http://localhost:8788 을 바로 열어도 화면이 나오게 한다
        if self.path not in ("/", "/screen.html"):
            self._send_json(404, {"error": "not found"})
            return
        with open(SCREEN_PATH, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/call-agent":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
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

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    print(f"orchestrator 담당자 호출 서버 실행 중 — http://localhost:{PORT}")
    print("orchestrator/screen.html을 브라우저로 열어 사용하세요. 끄려면 Ctrl+C.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
