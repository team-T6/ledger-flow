"""merge 화면(screen.html)을 call-agent.py·build_result.py와 이어주는 가벼운 로컬 서버.

표준 라이브러리(http.server)만 쓴다 — 추가 설치 없이 동작.
- screen.html의 "부르기" 버튼 → POST /call-agent → call-agent.py의 call_merge_agent()
- screen.html의 "결과물 만들기" 버튼 → POST /build-result → build_result.py의 run()
  (엑셀·PDF를 실제로 만들고, 미리보기에 쓸 표·집계를 JSON으로 돌려준다)
- GET /result.pdf → 방금 만든 PDF 파일 자체를 내려줘 화면에서 바로 미리보기한다
- GET /download/result.pdf, GET /download/result.xlsx → 같은 파일을 다운로드용으로 내려준다
API 키는 이 서버가 아니라 call-agent.py 쪽에서 .env를 읽어 쓴다.

사용법: python3 merge/server.py  (그다음 merge/screen.html을 브라우저로 연다)
"""

import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CALL_AGENT_PATH = os.path.join(BASE_DIR, "call-agent.py")
BUILD_RESULT_PATH = os.path.join(BASE_DIR, "build_result.py")
PDF_PATH = os.path.join(BASE_DIR, "result.pdf")
XLSX_PATH = os.path.join(BASE_DIR, "result.xlsx")
PORT = 8787

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# 경로 → (파일 경로, Content-Type, 다운로드 파일명 or None=미리보기)
FILE_ROUTES = {
    "/result.pdf": (PDF_PATH, "application/pdf", None),
    "/download/result.pdf": (PDF_PATH, "application/pdf", "merge_result.pdf"),
    "/download/result.xlsx": (XLSX_PATH, XLSX_CONTENT_TYPE, "merge_result.xlsx"),
}


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


call_agent = _load_module("call_agent", CALL_AGENT_PATH)
build_result = _load_module("build_result", BUILD_RESULT_PATH)


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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        route = FILE_ROUTES.get(urlsplit(self.path).path)
        if route is None:
            self._send_json(404, {"error": "not found"})
            return
        file_path, content_type, download_name = route
        if not os.path.exists(file_path):
            self._send_json(404, {"error": f"{os.path.basename(file_path)}가 아직 없습니다 — 먼저 결과물을 만드세요"})
            return
        with open(file_path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/call-agent":
            self._handle_call_agent()
        elif self.path == "/build-result":
            self._handle_build_result()
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_call_agent(self):
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
            result = call_agent.call_merge_agent(input_text)
            self._send_json(200, {"result": result})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _handle_build_result(self):
        try:
            result = build_result.run()
            self._send_json(200, {
                "ok_rows": result["ok_rows"],
                "flagged_rows": result["flagged_rows"],
                "summary": {
                    "total_expense": result["summary"]["total_expense"],
                    "total_income": result["summary"]["total_income"],
                    "by_category": result["summary"]["by_category"],
                    "by_method": result["summary"]["by_method"],
                    "by_payer": result["summary"]["by_payer"],
                },
                "envelope": result["envelope"],
            })
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    print(f"merge 담당자 호출 서버 실행 중 — http://localhost:{PORT}")
    print("merge/screen.html을 브라우저로 열어 사용하세요. 끄려면 Ctrl+C.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
