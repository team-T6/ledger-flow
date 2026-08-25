"""ledger-flow 로컬 웹 서버 — 사용자 페이지(web/index.html 실제 모드) + 파이프라인 실행.

표준 라이브러리(http.server)만 쓴다 — 추가 설치 없이 동작.
- GET  /                    : 사용자 페이지 (web/index.html — 이 서버에서 열리면 실제 모드로 동작)
- GET  /screen.html         : 개발자용 지휘 대시보드 (기존 화면)
- GET  /health              : 실제 모드 감지용 식별 응답 (프런트 프로브 전용)
- POST /auth                : 초대코드 검증 (.env의 INVITE_CODE와 대조)
- GET  /uploads             : 업로드된 파일 목록
- POST /uploads             : 파일 업로드 (JSON base64) → uploads/inbox/ 저장
- POST /uploads/delete      : 업로드 파일 1건 삭제 / POST /uploads/clear : 전체 삭제
- POST /runs                : 파이프라인 실행 시작 (run-pipeline.py를 백그라운드 스레드로)
                              body.source가 "uploads"면 업로드 파일로 수집한다
- GET  /runs/current?since=N: 진행 이벤트 증분 조회 (화면이 1.5초 간격으로 폴링)
- GET  /summary             : orchestrator/result-summary.md 원문
- GET  /result-data         : 결산 결과 화면용 통계 JSON (merge 집계 함수 재사용, 읽기 전용)
- GET  /artifacts/...       : merge/result.xlsx · result.pdf 내려받기
- POST /call-agent          : (기존) 결과 보고 확인 — call-agent.py 호출
실행 상태·이벤트는 메모리에만 둔다 (저장 안 함 원칙 — 영구 기록은 logs/run_*/ 규약뿐).
API 키는 이 서버가 아니라 call-agent.py / run-pipeline.py 쪽에서 .env를 읽어 쓴다.

사용법: python3 orchestrator/server.py  (그다음 http://localhost:8788 을 브라우저로 연다)
"""

import base64
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

WEB_INDEX_PATH = os.path.join(REPO_ROOT, "web", "index.html")
ENV_PATH = os.path.join(REPO_ROOT, ".env")
UPLOAD_DIR = os.path.join(REPO_ROOT, "uploads", "inbox")  # .gitignore가 uploads/를 차단한다
UPLOAD_EXTS = {".csv", ".txt", ".png", ".jpg", ".jpeg", ".xlsx"}
UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # 파일당 10MB
UPLOAD_MAX_COUNT = 30

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

_merge_module = None


def _get_merge_module():
    """merge 집계 함수(load_transactions·split_transactions·summarize) 재사용용 지연 로드.

    build_result.py가 openpyxl·reportlab을 import하므로, /result-data를 실제로 쓸 때만 로드한다.
    """
    global _merge_module
    if _merge_module is None:
        _merge_module = _load_module(
            "merge_stage_readonly", os.path.join(REPO_ROOT, "merge", "build_result.py"))
    return _merge_module


def load_env_value(key):
    """리포 루트 .env에서 key= 값을 읽는다 (없으면 None) — run-pipeline.py의 키 파서와 같은 관례."""
    if not os.path.exists(ENV_PATH):
        return None
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith(f"{key}="):
                value = line.split("=", 1)[1].strip()
                return value or None
    return None


def list_uploads():
    if not os.path.isdir(UPLOAD_DIR):
        return []
    return [{"name": e.name, "size": e.stat().st_size}
            for e in sorted(os.scandir(UPLOAD_DIR), key=lambda e: e.name) if e.is_file()]


def build_result_data():
    """결산 결과 화면용 통계 — merge의 집계 함수를 읽기 전용으로 호출한다 (파일 재생성 없음)."""
    refine_csv = os.path.join(REPO_ROOT, "refine", "result.csv")
    if not os.path.exists(refine_csv):
        return None
    merge_mod = _get_merge_module()
    transactions, incomplete = merge_mod.load_transactions()
    ok_rows, flagged_rows = merge_mod.split_transactions(transactions)
    summary = merge_mod.summarize(ok_rows)
    return {
        "month": run_state.month,
        "tx_count": len(transactions),
        "ok_count": len(ok_rows),
        "flagged_count": len(flagged_rows),
        "total_expense": summary["total_expense"],
        "total_income": summary["total_income"],
        "net": summary["total_income"] - summary["total_expense"],
        "by_category": summary["by_category"],
        "by_method": summary["by_method"],
        "by_payer": summary["by_payer"],
        "top_spenders": [{"날짜": r.get("날짜", ""), "결제처": r.get("결제처", ""),
                          "카테고리": r.get("카테고리", ""), "지출": r.get("지출") or 0}
                         for r in summary["top_spenders"]],
        "flags": [{"row": r["row"], "날짜": r.get("날짜", ""), "결제처": r.get("결제처", ""),
                   "reason": r.get("reason", "")} for r in flagged_rows],
        "incomplete": incomplete,
        "upload_count": len(list_uploads()),
    }


class RunState:
    """실행 1회의 관찰 상태 — 메모리에만 유지한다 (서버 재시작 시 소멸)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.month = None
        self.events = []       # run-pipeline.py의 on_event가 쌓는 진행 이벤트
        self.error = None      # 실행기 자체가 예외로 죽은 경우의 사유

    def start(self, month, upload_dir=None):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.month = month
            self.events = []
            self.error = None
        thread = threading.Thread(target=self._work, args=(month, upload_dir), daemon=True)
        thread.start()
        return True

    def _work(self, month, upload_dir):
        try:
            pipeline.run_pipeline(month, on_event=self.on_event, upload_dir=upload_dir)
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

        if path == "/":
            self._send_file(WEB_INDEX_PATH, "text/html; charset=utf-8")
            return

        if path == "/screen.html":
            self._send_file(SCREEN_PATH, "text/html; charset=utf-8")
            return

        if path == "/health":
            self._send_json(200, {"service": "ledger-flow", "role": "orchestrator", "version": 1})
            return

        if path == "/uploads":
            self._send_json(200, {"files": list_uploads()})
            return

        if path == "/result-data":
            try:
                data = build_result_data()
            except Exception as e:
                self._send_json(500, {"error": f"결과 집계 실패: {e}"})
                return
            if data is None:
                self._send_json(404, {"error": "결산 결과가 아직 없습니다 — 먼저 결산을 실행하세요"})
            else:
                self._send_json(200, data)
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
            # inline=1 — 브라우저 안에서 바로 보여준다 (화면 PDF 미리보기용). 없으면 내려받기
            inline = re.search(r"(?:^|&)inline=1(?:&|$)", query) is not None
            self._send_file(file_path, content_type,
                            download_name=None if inline else os.path.basename(file_path))
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""

        if self.path == "/runs":
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {}
            month = body.get("month", "")
            source = body.get("source", "sample")  # 기존 대시보드(screen.html) 하위 호환 기본값
            if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
                self._send_json(400, {"error": "month는 YYYY-MM 형식이어야 합니다"})
                return
            if source not in ("sample", "uploads"):
                self._send_json(400, {"error": f"source 어휘 위반: {source} (sample | uploads)"})
                return
            upload_dir = None
            if source == "uploads":
                if not list_uploads():
                    self._send_json(400, {"error": "업로드된 파일이 없습니다 — 자료를 먼저 올려 주세요"})
                    return
                upload_dir = UPLOAD_DIR
            if run_state.start(month, upload_dir=upload_dir):
                self._send_json(200, {"started": True, "month": month, "source": source})
            else:
                self._send_json(409, {"error": f"이미 실행 중입니다 (대상 월 {run_state.month})"})
            return

        if self.path == "/auth":
            try:
                code = (json.loads(raw).get("code", "") if raw else "").strip()
            except json.JSONDecodeError:
                code = ""
            invite = load_env_value("INVITE_CODE")
            if not invite:
                self._send_json(500, {"error": ".env에 INVITE_CODE를 설정하세요 (예: INVITE_CODE=우리팀코드)"})
            elif code == invite:
                self._send_json(200, {"ok": True})
            else:
                self._send_json(403, {"error": "초대코드가 올바르지 않습니다"})
            return

        if self.path == "/uploads":
            try:
                files = json.loads(raw).get("files", []) if raw else []
            except json.JSONDecodeError:
                files = []
            if not isinstance(files, list) or not files:
                self._send_json(400, {"error": "올릴 파일이 없습니다"})
                return
            existing = {f["name"] for f in list_uploads()}
            new_names = {os.path.basename(str(f.get("name", ""))) for f in files}
            if len(existing | new_names) > UPLOAD_MAX_COUNT:
                self._send_json(400, {"error": f"파일은 최대 {UPLOAD_MAX_COUNT}개까지 올릴 수 있습니다"})
                return
            saved = []
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            for f in files:
                name = os.path.basename(str(f.get("name", "")).strip())
                ext = os.path.splitext(name)[1].lower()
                if not name or ext not in UPLOAD_EXTS:
                    self._send_json(400, {"error": f"허용되지 않는 파일 형식: {name or '(이름 없음)'} "
                                                   f"(가능: {', '.join(sorted(UPLOAD_EXTS))})"})
                    return
                try:
                    data = base64.b64decode(f.get("data_base64", ""), validate=True)
                except Exception:
                    self._send_json(400, {"error": f"파일 내용을 읽지 못했습니다: {name}"})
                    return
                if len(data) > UPLOAD_MAX_BYTES:
                    self._send_json(400, {"error": f"파일이 너무 큽니다 (10MB 초과): {name}"})
                    return
                with open(os.path.join(UPLOAD_DIR, name), "wb") as out:
                    out.write(data)
                saved.append(name)
            self._send_json(200, {"saved": saved, "files": list_uploads()})
            return

        if self.path == "/uploads/delete":
            try:
                name = os.path.basename(str(json.loads(raw).get("name", "")).strip()) if raw else ""
            except json.JSONDecodeError:
                name = ""
            target = os.path.join(UPLOAD_DIR, name)
            if name and os.path.isfile(target):
                os.remove(target)
            self._send_json(200, {"files": list_uploads()})
            return

        if self.path == "/uploads/clear":
            for entry in list_uploads():
                os.remove(os.path.join(UPLOAD_DIR, entry["name"]))
            self._send_json(200, {"files": []})
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
