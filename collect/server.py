"""collect/screen.html과 call-agent.py를 이어주는 로컬 서버.

screen.html을 정적 파일로 내주고, POST /call-agent 요청을 받으면
call-agent.py의 call_agent()를 그대로 호출해 결과를 돌려준다.
"""

import http.server
import importlib.util
import json
import socketserver
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PORT = 8787

_spec = importlib.util.spec_from_file_location("call_agent_module", BASE_DIR / "call-agent.py")
call_agent_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(call_agent_module)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def do_POST(self):
        if self.path != "/call-agent":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        text = body.get("text", "")

        try:
            result = call_agent_module.call_agent(text)
            payload = {"result": result}
            status = 200
        except Exception as e:
            payload = {"error": str(e)}
            status = 500

        response = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


if __name__ == "__main__":
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"http://127.0.0.1:{PORT}/screen.html 에서 확인하세요")
        httpd.serve_forever()
