"""ledger-flow 로컬 웹 서버 — 사용자 페이지(web/index.html 실제 모드) + 파이프라인 실행.

표준 라이브러리(http.server)만 쓴다 — 추가 설치 없이 동작.
- GET  /                    : 사용자 페이지 (web/index.html — 이 서버에서 열리면 실제 모드로 동작)
- GET  /screen.html         : 개발자용 지휘 대시보드 (기존 화면)
- GET  /favicon.svg         : 파비콘 (web/favicon.svg)
- GET  /health              : 실제 모드 감지용 식별 응답 (프런트 프로브 전용)
- POST /auth                : 초대코드 검증 (.env의 INVITE_CODE와 대조)
- GET  /uploads             : 업로드된 파일 목록
- POST /uploads             : 파일 업로드 (JSON base64) → uploads/inbox/ 저장
- POST /uploads/delete      : 업로드 파일 1건 삭제 / POST /uploads/clear : 전체 삭제
- POST /runs                : 파이프라인 실행 시작 (run-pipeline.py를 백그라운드 스레드로)
                              body.source가 "uploads"면 업로드 파일로 수집한다
                              body.fraud_check가 참이면 검증 3(부정 사용 감지)을 함께 실행한다
- GET  /categories          : 분류 기준(docs/categories.md) 원문 + 최종 업데이트 일시
- POST /categories          : 분류 기준 저장 — 원문을 통째로 받아 최종 업데이트 일시를 찍어 쓴다
- POST /drive/import        : Google Drive 가져오기 — claude CLI headless + .mcp.json의 Drive MCP로
                              대상 폴더 파일을 uploads/inbox/에 저장한다 (사전 인증 필요)
- GET  /runs/current?since=N: 진행 이벤트 증분 조회 (화면이 1.5초 간격으로 폴링)
                              중간 확인 대기 중이면 confirm 필드(단계·행·수정 가능 필드)를 담는다
- POST /runs/confirm        : 중간 확인 응답 — {stage, resolutions:[{transaction_id, fields}]}
                              (대기 상한 10분 — 초과하면 파이프라인이 전부 유지로 진행)
- GET  /summary             : orchestrator/result-summary.md 원문 (?month=YYYY-MM이면 보관본)
- GET  /result-data         : 결산 결과 화면용 통계 JSON (merge 집계 함수 재사용, 읽기 전용)
- GET  /months              : 월별 보관함 목록 (archive/<월>/summary.json 배열)
- GET  /artifacts/...       : merge/result.xlsx · result.pdf 내려받기 (?month=YYYY-MM이면 보관본)
- POST /call-agent          : (기존) 결과 보고 확인 — call-agent.py 호출
실행 상태·이벤트는 메모리에만 둔다. 영구 기록은 logs/run_*/ 규약과, 결산 정상 종료 시
파이프라인이 archive/<YYYY-MM>/에 남기는 월별 산출물 보관본(웹 보관함용 — gitignore 차단)뿐.
API 키는 이 서버가 아니라 call-agent.py / run-pipeline.py 쪽에서 .env를 읽어 쓴다.

사용법: python3 orchestrator/server.py  (그다음 http://localhost:8788 을 브라우저로 연다)
"""

import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime
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
ARCHIVE_DIR = os.path.join(REPO_ROOT, "archive")  # 월별 산출물 보관 — .gitignore가 archive/를 차단한다
UPLOAD_EXTS = {".csv", ".txt", ".png", ".jpg", ".jpeg", ".xlsx", ".pdf", ".zip"}
UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # 파일당 10MB
UPLOAD_MAX_COUNT = 30

CATEGORIES_PATH = os.path.join(REPO_ROOT, "docs", "categories.md")  # 분류 기준 단일 정본
CATEGORIES_MAX_BYTES = 64 * 1024  # 설정 화면 저장 상한 — 정본이 문서 파일이라 넉넉히 잡는다
MCP_CONFIG_PATH = os.path.join(REPO_ROOT, ".mcp.json")  # Drive MCP 등록 자리 (.mcp.json.example 참고)
DRIVE_IMPORT_TIMEOUT = 300  # Drive 가져오기 CLI 호출 상한 (초)

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


def build_result_data(month=None):
    """결산 결과 화면용 통계 — merge의 집계 함수를 읽기 전용으로 호출한다 (파일 재생성 없음)."""
    refine_csv = os.path.join(REPO_ROOT, "refine", "result.csv")
    if not os.path.exists(refine_csv):
        return None
    merge_mod = _get_merge_module()
    transactions, incomplete = merge_mod.load_transactions()
    ok_rows, flagged_rows = merge_mod.split_transactions(transactions)
    summary = merge_mod.summarize(ok_rows)
    return {
        "month": month or run_state.month,
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
        "rows": merge_mod.build_row_list(transactions, flagged_rows),
    }


def list_months():
    """월별 보관함 목록 — archive/<YYYY-MM>/summary.json을 모아 월 오름차순으로 돌려준다."""
    months = []
    if not os.path.isdir(ARCHIVE_DIR):
        return months
    for name in sorted(os.listdir(ARCHIVE_DIR)):
        if not re.fullmatch(r"\d{4}-\d{2}", name):
            continue
        try:
            with open(os.path.join(ARCHIVE_DIR, name, "summary.json"), encoding="utf-8") as f:
                entry = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue  # summary 없는(깨진) 보관 폴더는 목록에서 제외
        entry["month"] = name
        entry["files"] = {"pdf": os.path.exists(os.path.join(ARCHIVE_DIR, name, "result.pdf")),
                          "xlsx": os.path.exists(os.path.join(ARCHIVE_DIR, name, "result.xlsx"))}
        months.append(entry)
    return months


UPDATED_LINE_RE = re.compile(r"^> 최종 업데이트: .*$", re.MULTILINE)


def read_categories():
    """분류 기준 원문 + 최종 업데이트 일시 — 정본은 docs/categories.md 하나다."""
    with open(CATEGORIES_PATH, encoding="utf-8") as f:
        content = f.read()
    match = UPDATED_LINE_RE.search(content)
    updated = match.group(0).split(":", 1)[1].strip() if match else ""
    return {"content": content, "updated": updated}


def write_categories(content):
    """분류 기준 저장 — 최종 업데이트 일시를 지금 시각으로 찍어 쓴다 (설정 화면 저장 경로).

    가공·검증 1은 실행 시점의 이 파일을 읽으므로(확정) 저장 즉시 다음 실행부터 반영된다.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    stamp = f"> 최종 업데이트: {now}"
    if UPDATED_LINE_RE.search(content):
        content = UPDATED_LINE_RE.sub(stamp, content, count=1)
    else:  # 스탬프 줄이 지워진 채 저장돼도 제목 다음 줄에 복원한다
        content = re.sub(r"^(# .*\n)", rf"\1\n{stamp}\n", content, count=1)
    with open(CATEGORIES_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    return now


def drive_import(month):
    """Google Drive 가져오기 — claude CLI headless + .mcp.json의 Drive MCP (확정 방식).

    사전 조건: claude CLI 설치 + .mcp.json에 Drive MCP 등록(.mcp.json.example 참고) +
    해당 MCP의 계정 인증이 이 머신에서 완료돼 있어야 한다. 조건이 빠지면 안내 오류를 돌려준다.
    CLI는 repo 밖 임시 디렉터리에서 실행한다 (프로젝트 지침 주입 방지 — run-pipeline.py와 같은 관례).
    """
    if not shutil.which("claude"):
        raise RuntimeError("claude CLI가 설치되어 있지 않습니다 — Drive 가져오기는 CLI 경유로 동작합니다")
    if not os.path.exists(MCP_CONFIG_PATH):
        raise RuntimeError(".mcp.json이 없습니다 — .mcp.json.example을 복사해 Drive MCP를 등록하고 "
                           "계정 인증을 먼저 완료해 주세요")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    folder = load_env_value("DRIVE_FOLDER") or "ledger-flow"
    allowed = ", ".join(sorted(UPLOAD_EXTS))
    prompt = (
        f"연결된 Google Drive MCP 도구로 Drive에서 폴더 이름 \"{folder}\"(없으면 그 이름이 포함된 폴더)를 찾아, "
        f"그 안의 파일 중 결산 대상 월 {month}에 해당하는 카드 내역·영수증 파일만 골라 "
        f"로컬 디렉터리 {UPLOAD_DIR} 에 원본 파일명 그대로 저장하라. "
        f"허용 확장자: {allowed} — 그 외 파일은 건너뛴다. 파일당 10MB를 넘으면 건너뛴다. "
        "작업이 끝나면 저장한 파일명 목록과 건너뛴 파일·사유를 한국어로 짧게 보고하라. "
        "Drive에 접근할 수 없으면 그 사실만 보고하라."
    )
    result = subprocess.run(
        ["claude", "-p", "--mcp-config", MCP_CONFIG_PATH, "--strict-mcp-config",
         "--permission-mode", "acceptEdits", "--allowed-tools",
         "Write,mcp__gdrive__*", prompt],
        capture_output=True, text=True, timeout=DRIVE_IMPORT_TIMEOUT,
        cwd=tempfile.gettempdir())
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise RuntimeError(f"Drive 가져오기 실패 (claude CLI exit {result.returncode}): {detail}")
    return (result.stdout or "").strip()


CONFIRM_WAIT_SECONDS = 600  # 중간 확인 응답 대기 상한 10분 — 초과 시 전부 유지로 진행 (단계 문서 확정)


class RunState:
    """실행 1회의 관찰 상태 — 메모리에만 유지한다 (서버 재시작 시 소멸)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.month = None
        self.events = []       # run-pipeline.py의 on_event가 쌓는 진행 이벤트
        self.error = None      # 실행기 자체가 예외로 죽은 경우의 사유
        self.confirm = None    # 대기 중인 중간 확인 요청 (없으면 None)
        self.confirm_result = None
        self.confirm_seq = 0
        self.confirm_ready = threading.Event()

    def start(self, month, upload_dir=None, fraud_check=False):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.month = month
            self.events = []
            self.error = None
            self.confirm = None
            self.confirm_result = None
            self.confirm_ready.clear()
        thread = threading.Thread(target=self._work, args=(month, upload_dir, fraud_check),
                                  daemon=True)
        thread.start()
        return True

    def _work(self, month, upload_dir, fraud_check):
        # 월별 보관(archive/<월>/)은 파이프라인이 종료 직전에 직접 수행한다 — 실행 경로 무관
        try:
            pipeline.run_pipeline(month, on_event=self.on_event, upload_dir=upload_dir,
                                  on_confirm=self.on_confirm, fraud_check=fraud_check)
        except Exception as e:
            with self.lock:
                self.error = str(e)
        finally:
            with self.lock:
                self.running = False
                self.confirm = None

    def on_event(self, event):
        with self.lock:
            self.events.append(event)

    def on_confirm(self, payload):
        """파이프라인 스레드가 부르는 중간 확인 훅 — 화면 응답까지 블록한다.

        반환: 사용자 입력(resolutions 목록, 빈 목록이면 전부 유지) / 대기 상한 초과면 None.
        """
        with self.lock:
            self.confirm_seq += 1
            self.confirm = dict(payload, seq=self.confirm_seq)
            self.confirm_result = None
            self.confirm_ready.clear()
        answered = self.confirm_ready.wait(CONFIRM_WAIT_SECONDS)
        with self.lock:
            result = self.confirm_result if answered else None
            self.confirm = None
            self.confirm_result = None
        return result

    def resolve_confirm(self, stage, resolutions):
        """POST /runs/confirm 처리 — 대기 중인 요청과 단계가 맞아야 반영한다."""
        with self.lock:
            if not self.confirm or self.confirm.get("stage") != stage:
                return False
            self.confirm_result = resolutions
            self.confirm_ready.set()
            return True

    def snapshot(self, since):
        with self.lock:
            return {
                "running": self.running,
                "month": self.month,
                "error": self.error,
                "confirm": self.confirm,
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

        if path == "/favicon.svg":
            self._send_file(os.path.join(REPO_ROOT, "web", "favicon.svg"), "image/svg+xml")
            return

        if path == "/health":
            self._send_json(200, {"service": "ledger-flow", "role": "orchestrator", "version": 1})
            return

        if path == "/uploads":
            self._send_json(200, {"files": list_uploads()})
            return

        if path == "/categories":
            try:
                self._send_json(200, read_categories())
            except OSError as e:
                self._send_json(500, {"error": f"분류 기준 파일을 읽지 못했습니다: {e}"})
            return

        if path == "/months":
            self._send_json(200, {"months": list_months()})
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

        # month=YYYY-MM — 보관본을 서빙한다 (형식 강제라 경로 조작 불가). 없으면 최신본
        month_match = re.search(r"(?:^|&)month=(\d{4}-\d{2})(?:&|$)", query)

        if path == "/summary":
            summary_path = SUMMARY_PATH
            if month_match:
                summary_path = os.path.join(ARCHIVE_DIR, month_match.group(1), "result-summary.md")
            self._send_file(summary_path, "text/markdown; charset=utf-8")
            return

        if path in ARTIFACTS:
            file_path, content_type = ARTIFACTS[path]
            if month_match:
                file_path = os.path.join(ARCHIVE_DIR, month_match.group(1),
                                         os.path.basename(file_path))
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
            fraud_check = bool(body.get("fraud_check"))  # 부정 사용 감지 토글 (기본 꺼짐)
            if run_state.start(month, upload_dir=upload_dir, fraud_check=fraud_check):
                self._send_json(200, {"started": True, "month": month, "source": source,
                                      "fraud_check": fraud_check})
            else:
                self._send_json(409, {"error": f"이미 실행 중입니다 (대상 월 {run_state.month})"})
            return

        if self.path == "/categories":
            try:
                content = json.loads(raw).get("content", "") if raw else ""
            except json.JSONDecodeError:
                content = ""
            if not isinstance(content, str) or not content.strip():
                self._send_json(400, {"error": "저장할 내용이 비어 있습니다"})
                return
            if len(content.encode("utf-8")) > CATEGORIES_MAX_BYTES:
                self._send_json(400, {"error": "내용이 너무 큽니다 (64KB 초과)"})
                return
            if "## 지출" not in content:
                self._send_json(400, {"error": "지출 카테고리 표가 없습니다 — 형식을 확인해 주세요"})
                return
            try:
                updated = write_categories(content)
            except OSError as e:
                self._send_json(500, {"error": f"저장 실패: {e}"})
                return
            self._send_json(200, {"ok": True, "updated": updated})
            return

        if self.path == "/drive/import":
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {}
            month = body.get("month", "")
            if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
                self._send_json(400, {"error": "month는 YYYY-MM 형식이어야 합니다"})
                return
            try:
                report = drive_import(month)
            except Exception as e:
                self._send_json(502, {"error": str(e)})
                return
            self._send_json(200, {"ok": True, "report": report, "files": list_uploads()})
            return

        if self.path == "/runs/confirm":
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {}
            stage = str(body.get("stage") or "")
            resolutions = body.get("resolutions")
            if not isinstance(resolutions, list):
                self._send_json(400, {"error": "resolutions는 배열이어야 합니다"})
                return
            # 형식 방어는 파이프라인의 clean_resolutions가 한 번 더 한다 — 여기선 목록만 강제
            if run_state.resolve_confirm(stage, resolutions):
                self._send_json(200, {"ok": True})
            else:
                self._send_json(409, {"error": "대기 중인 확인 요청이 없거나 단계가 다릅니다"})
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
