"""collect/screen.html과 call-agent.py를 이어주는 로컬 서버.

screen.html을 정적 파일로 내주고, POST /call-agent·POST /read-receipt 요청을 받으면
call-agent.py의 call_agent()·call_agent_with_image()를 그대로 호출해 결과를 돌려준다.
"""

import base64
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
        if self.path == "/call-agent":
            self._handle_call_agent()
        elif self.path == "/read-receipt":
            self._handle_read_receipt()
        else:
            self.send_error(404)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _handle_call_agent(self):
        body = self._read_json_body()
        text = body.get("text", "")

        try:
            result = call_agent_module.call_agent(text)
            payload = {"result": result}
            status = 200
        except Exception as e:
            payload = {"error": str(e)}
            status = 500

        self._send_json(status, payload)

    def _handle_read_receipt(self):
        body = self._read_json_body()
        image_b64 = body.get("image", "")
        media_type = body.get("media_type", "")

        try:
            image_bytes = base64.b64decode(image_b64) if image_b64 else b""
            result = call_agent_module.call_agent_with_image(image_bytes, media_type)
            payload = {"result": result}
            status = 200
        except Exception as e:
            payload = {"error": str(e)}
            status = 500

        self._send_json(status, payload)

    def _send_json(self, status, payload):
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
