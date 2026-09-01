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
- GET  /uploads/manual      : 현금 등 직접 입력 목록 (collect.md "하는 단계 7")
- POST /uploads/manual      : 직접 입력 1건 추가 / POST /uploads/manual/delete : 1건 삭제
- POST /runs                : 파이프라인 실행 시작 (run-pipeline.py를 백그라운드 스레드로)
                              body.source가 "uploads"면 업로드 파일로 수집한다
                              body.fraud_check가 참이면 부정 사용 검증(부정 사용 감지)을 함께 실행한다
- GET  /categories          : 분류 기준(docs/categories.md) 원문 + 최종 업데이트 일시
- POST /categories          : 분류 기준 저장 — 원문을 통째로 받아 최종 업데이트 일시를 찍어 쓴다
- GET  /drive/browse?parent_id=: Drive 폴더 선택 UI용 — 그 폴더 바로 아래의 하위 폴더·가져올 수
                              있는 파일 목록 (Drive REST API 직접 호출, AI 미사용, parent_id
                              생략 시 "내 드라이브" 루트)
- POST /drive/import        : Google Drive 가져오기 — Drive REST API를 직접 호출해(AI 미사용)
                              대상 폴더(body.folder_id, 없으면 .env의 DRIVE_FOLDER 이름 매칭)의
                              파일을 uploads/inbox/에 원본 그대로 저장한다. body.file_ids를 주면
                              그 폴더 파일 중 고른 것만 가져온다 (사전 인증 필요)
- GET  /runs/current?since=N: 진행 이벤트 증분 조회 (화면이 1.5초 간격으로 폴링)
                              중간 확인 대기 중이면 confirm 필드(단계·행·수정 가능 필드)를 담고,
                              fraud_check로 이 실행의 부정 사용 감지 토글 값을 알린다 (화면 도중 접속 복원용)
- POST /runs/confirm        : 중간 확인 응답 — {stage, resolutions:[{transaction_id, fields}]}
                              (대기 상한 10분 — 초과하면 파이프라인이 전부 유지로 진행)
- GET  /refix/pending?month=YYYY-MM : 결산 완료 후 남은 확인 필요 건 목록 (재결산 화면용 —
                              중간 확인과 같은 payload 모양. 칸 산출물의 달이 아니면 보관본
                              archive/<월>/stages/에서 읽고, 그것도 없으면 409)
- POST /runs/refix          : 재결산 — {month, resolutions}를 받아 확인 반영 후 통합만 재실행
                              (run-pipeline.py의 run_refix — 보관된 월이면 stages/에서 단계
                              산출물을 복원해 진행. 진행은 /runs/current로 관찰)
- GET  /summary             : orchestrator/result-summary.md 원문 (?month=YYYY-MM이면 보관본)
- GET  /result-data         : 결산 결과 화면용 통계 JSON (merge 집계 함수 재사용, 읽기 전용)
- GET  /months              : 월별 보관함 목록 (archive/<월>/summary.json 배열)
- GET  /artifacts/...       : merge/result.xlsx · result.pdf 내려받기 (?month=YYYY-MM이면 보관본,
                              저장 파일명은 {월}_{report|ledger}_{생성일시}.{확장자}로 지어 준다)
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
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
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
MCP_CONFIG_PATH = os.path.join(REPO_ROOT, ".mcp.json")  # Drive MCP 등록 자리 (.mcp.json.example 참고) — 계정 인증 1회 수행에 계속 사용
GDRIVE_CREDS_DIR = os.environ.get("GDRIVE_CREDS_DIR") or os.path.join(REPO_ROOT, ".config")  # .mcp.json의 같은 값과 맞춘다
DRIVE_HTTP_TIMEOUT = 60  # Drive REST 호출 1건당 상한 (초)

ARTIFACTS = {  # 내려받기 허용 목록 — 경로 조작 방지를 위해 고정 매핑만 쓴다
    "/artifacts/result.xlsx": (os.path.join(REPO_ROOT, "merge", "result.xlsx"),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "/artifacts/result.pdf": (os.path.join(REPO_ROOT, "merge", "result.pdf"),
                              "application/pdf"),
}


def _download_name(file_path, month):
    """내려받기 파일명 — {대상월}_{report|ledger}_{생성일시}.{확장자}.

    디스크 경로(merge/result.* · archive/<월>/result.*)는 단계 간 약속(인터페이스 정의서)이라
    그대로 두고, 사용자에게 저장되는 이름만 알아보기 쉽게 짓는다. 생성일시는 파일 수정 시각.
    대상 월을 모르면(서버 재시작 직후의 최신본 등) 원래 파일명을 그대로 쓴다."""
    base = os.path.basename(file_path)
    if not month or not os.path.exists(file_path):
        return base
    ext = os.path.splitext(base)[1]
    doc = "ledger" if ext == ".xlsx" else "report"
    ts = datetime.fromtimestamp(os.path.getmtime(file_path)).strftime("%Y%m%d-%H%M")
    return f"{month}_{doc}_{ts}{ext}"


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


DRIVE_MANIFEST_PATH = os.path.join(REPO_ROOT, "uploads", "drive_manifest.json")  # uploads/inbox/ 밖에 둬서 collect가 데이터 파일로 오인해 처리하지 않게 한다


def load_drive_manifest():
    if not os.path.exists(DRIVE_MANIFEST_PATH):
        return set()
    try:
        with open(DRIVE_MANIFEST_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def add_to_drive_manifest(names):
    if not names:
        return
    names = load_drive_manifest() | set(names)
    os.makedirs(os.path.dirname(DRIVE_MANIFEST_PATH), exist_ok=True)
    with open(DRIVE_MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(names), f, ensure_ascii=False)


def list_uploads():
    if not os.path.isdir(UPLOAD_DIR):
        return []
    drive_names = load_drive_manifest()
    return [{"name": e.name, "size": e.stat().st_size,
             "source": "drive" if e.name in drive_names else "upload"}
            for e in sorted(os.scandir(UPLOAD_DIR), key=lambda e: e.name) if e.is_file()]


# 현금 등 자동 수집이 어려운 거래의 직접 입력 (collect.md "하는 단계 7") — uploads/inbox/
# 밖에 둬서 파일 기반 수집(카드사 엑셀·이미지·zip) 경로와 섞이지 않게 한다.
MANUAL_ENTRIES_PATH = os.path.join(REPO_ROOT, "uploads", "manual_entries.json")
MANUAL_REQUIRED_FIELDS = ("날짜", "결제처", "금액")


def load_manual_entries():
    if not os.path.exists(MANUAL_ENTRIES_PATH):
        return []
    try:
        with open(MANUAL_ENTRIES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_manual_entries(entries):
    os.makedirs(os.path.dirname(MANUAL_ENTRIES_PATH), exist_ok=True)
    with open(MANUAL_ENTRIES_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)


def add_manual_entry(payload):
    """직접 입력 한 건을 검증해 저장한다 — 실패하면 ValueError(사용자에게 보여줄 사유)."""
    missing = [k for k in MANUAL_REQUIRED_FIELDS if not str(payload.get(k, "")).strip()]
    if missing:
        raise ValueError(f"필수 항목 누락: {', '.join(missing)}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(payload.get("날짜", "")).strip()):
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다")
    try:
        amount = int(payload.get("금액"))
    except (TypeError, ValueError):
        raise ValueError("금액은 숫자여야 합니다")
    if amount <= 0:
        raise ValueError("금액은 양수로 입력해 주세요 — 수기 입력은 항상 지출로 기록됩니다")
    payment_type = payload.get("결제구분") or "개인결제"
    if payment_type not in ("개인결제", "법인결제"):
        raise ValueError("결제구분은 개인결제/법인결제 중 하나여야 합니다")

    entries = load_manual_entries()
    next_id = (max((e["id"] for e in entries), default=0)) + 1
    entry = {
        "id": next_id,
        "날짜": payload["날짜"].strip(),
        "결제처": payload["결제처"].strip(),
        "금액": -amount,  # 거래 표 스키마 — 지출은 음수 (수기 입력은 항상 지출)
        "결제구분": payment_type,
        "결제수단": (payload.get("결제수단") or "현금").strip(),
        "비고": (payload.get("비고") or "").strip(),
    }
    entries.append(entry)
    save_manual_entries(entries)
    return entries


def delete_manual_entry(entry_id):
    entries = [e for e in load_manual_entries() if e.get("id") != entry_id]
    save_manual_entries(entries)
    return entries


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

    가공·분류 검증은 실행 시점의 이 파일을 읽으므로(확정) 저장 즉시 다음 실행부터 반영된다.
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


def _drive_request(method, url, token, params=None, data=None, raw=False):
    """Drive/OAuth REST 호출 — AI(LLM) 없이 직접 호출한다 (바이너리 파일이 MCP 도구를 거치면

    base64 텍스트로 인코딩돼 LLM 컨텍스트를 통과해야 해 토큰 비용이 급증하는 문제를 피하기
    위한 확정 방식 — interface-spec.md 2026-09-01 확정 로그).
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=DRIVE_HTTP_TIMEOUT) as resp:
        raw_body = resp.read()
    return raw_body if raw else json.loads(raw_body)


def _drive_access_token():
    """저장된 OAuth 자격증명으로 유효한 access token을 돌려준다 — 만료 임박이면 갱신해 같이 저장한다.

    키 파일(gcp-oauth.keys.json)·토큰 파일(.gdrive-server-credentials.json)은 Drive MCP
    계정 인증 1회 수행 시 GDRIVE_CREDS_DIR에 생성된 것과 같은 파일이다(포맷도 동일하게 맞춤).
    """
    keyfile_path = os.path.join(GDRIVE_CREDS_DIR, "gcp-oauth.keys.json")
    token_path = os.path.join(GDRIVE_CREDS_DIR, ".gdrive-server-credentials.json")
    if not os.path.exists(keyfile_path) or not os.path.exists(token_path):
        raise RuntimeError(
            "Drive 계정 인증이 안 돼 있습니다 — .mcp.json.example을 참고해 Drive MCP를 등록하고 "
            "계정 인증을 먼저 1회 완료해 주세요")
    with open(keyfile_path, encoding="utf-8") as f:
        installed = json.load(f)["installed"]
    with open(token_path, encoding="utf-8") as f:
        token = json.load(f)
    expiry = datetime.fromtimestamp(token["expiry_date"] / 1000)
    if datetime.now() < expiry:
        return token["access_token"]
    refreshed = _drive_request(
        "POST", "https://oauth2.googleapis.com/token", None, data={
            "client_id": installed["client_id"],
            "client_secret": installed["client_secret"],
            "refresh_token": token["refresh_token"],
            "grant_type": "refresh_token",
        })
    token["access_token"] = refreshed["access_token"]
    token["expiry_date"] = int((datetime.now().timestamp() + refreshed["expires_in"]) * 1000)
    with open(token_path, "w", encoding="utf-8") as f:
        json.dump(token, f, indent=2)
    return token["access_token"]


def _drive_find_folder(token, name):
    escaped = name.replace("'", "\\'")
    result = _drive_request(
        "GET", "https://www.googleapis.com/drive/v3/files", token, params={
            "q": f"mimeType='application/vnd.google-apps.folder' and name contains '{escaped}' "
                 "and trashed=false",
            "fields": "files(id,name)",
            "pageSize": 1,
        })
    files = result.get("files") or []
    return files[0] if files else None


def _drive_list_folder_files(token, folder_id):
    files, page_token = [], None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "fields": "nextPageToken, files(id,name,mimeType,size)",
            "pageSize": 100,
        }
        if page_token:
            params["pageToken"] = page_token
        result = _drive_request("GET", "https://www.googleapis.com/drive/v3/files", token, params=params)
        files.extend(result.get("files") or [])
        page_token = result.get("nextPageToken")
        if not page_token or len(files) >= UPLOAD_MAX_COUNT:
            break
    return files


DRIVE_BROWSE_MAX_ENTRIES = 500  # 폴더 선택 UI 미리보기 상한 — 이 이상 큰 폴더는 이후 페이지를 안 불러온다


def list_drive_children(parent_id=None):
    """Drive 폴더 선택 UI용 — parent_id 바로 아래의 하위 폴더·가져올 수 있는 파일을 함께 돌려준다
    (AI 미사용) — {"folders": [{id,name}], "files": [{id,name,size}]}.

    계정 전체 폴더를 한 번에 나열하면 폴더가 많은 계정에서 평평한 목록이 되어 실제 구조를
    알아볼 수 없다. 웹 화면의 "Google Drive 연결" 모달이 이 함수를 매번 현재 보고 있는
    폴더로 다시 불러 계층을 눌러 들어가게 하고, 그 폴더 안의 파일을 체크박스로 여러 개
    골라 가져오게 한다(interface-spec.md 2026-09-01 "폴더 선택 UI 추가" 확정 로그).
    parent_id 생략 시 "내 드라이브" 루트("root"). 파일은 허용 확장자만 돌려준다 —
    Google Docs/Sheets 등 네이티브 파일과 그 외 확장자는 가져오기 대상이 아니라 뺀다.
    """
    token = _drive_access_token()
    parent = parent_id or "root"
    entries, page_token = [], None
    while True:
        params = {
            "q": f"'{parent}' in parents and trashed=false",
            "fields": "nextPageToken, files(id,name,mimeType,size)",
            "pageSize": 100,
            "orderBy": "folder,name",
        }
        if page_token:
            params["pageToken"] = page_token
        result = _drive_request("GET", "https://www.googleapis.com/drive/v3/files", token, params=params)
        entries.extend(result.get("files") or [])
        page_token = result.get("nextPageToken")
        if not page_token or len(entries) >= DRIVE_BROWSE_MAX_ENTRIES:
            break
    folders, files = [], []
    for e in entries:
        if e.get("mimeType") == "application/vnd.google-apps.folder":
            folders.append({"id": e["id"], "name": e["name"]})
        elif os.path.splitext(e["name"])[1].lower() in UPLOAD_EXTS:
            files.append({"id": e["id"], "name": e["name"], "size": e.get("size")})
    return {"folders": folders, "files": files}


def _drive_download_entries(token, entries):
    """entries([{id,name,mimeType,size}, ...])를 실제로 내려받아 uploads/inbox/에 저장한다.

    확장자·네이티브 파일·10MB 상한 판정을 여기 한 곳에 모아 drive_import의 두 경로
    (폴더 전체 가져오기 / 체크박스로 고른 파일만 가져오기)가 같은 규칙을 쓰게 한다.
    """
    saved, skipped = [], []
    for entry in entries:
        name = entry["name"]
        ext = os.path.splitext(name)[1].lower()
        if entry.get("mimeType", "").startswith("application/vnd.google-apps"):
            skipped.append(f"{name} (Google Docs/Sheets 등 네이티브 파일 — 내보내기 미지원)")
            continue
        if ext not in UPLOAD_EXTS:
            skipped.append(f"{name} (허용되지 않는 확장자)")
            continue
        size = entry.get("size")
        if size is not None and int(size) > UPLOAD_MAX_BYTES:
            skipped.append(f"{name} (10MB 초과)")
            continue
        content = _drive_request(
            "GET", f"https://www.googleapis.com/drive/v3/files/{entry['id']}", token,
            params={"alt": "media"}, raw=True)
        if len(content) > UPLOAD_MAX_BYTES:
            skipped.append(f"{name} (10MB 초과)")
            continue
        with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
            f.write(content)
        saved.append(name)
    return saved, skipped


def drive_import(month, folder_id=None, file_ids=None):
    """Google Drive 가져오기 — Drive REST API를 직접 호출하는 순수 다운로드 (확정 방식).

    사전 조건: .mcp.json에 Drive MCP 등록(.mcp.json.example 참고) + 그 계정 인증이 이 머신에서
    1회 완료돼 있어야 한다(GDRIVE_CREDS_DIR에 키·토큰 파일 존재). 조건이 빠지면 안내 오류를 돌려준다.
    AI(LLM) 호출 없이 이 함수만으로 다운로드가 끝난다 — 바이너리 파일이 MCP 도구를 거치며
    base64 텍스트로 LLM 컨텍스트를 통과하는 걸 피하기 위함(interface-spec.md 2026-09-01 확정 로그).

    folder_id가 주어지면(웹 화면의 폴더 선택 UI) 그 폴더를 그대로 쓴다. 없으면 DRIVE_FOLDER
    환경변수(또는 기본값 "ledger-flow") 이름 매칭으로 폴더를 찾는 폴백을 쓴다. file_ids가
    함께 주어지면(체크박스로 고른 파일) 그 폴더의 파일 중 해당 id만 골라 가져온다 — 파일명은
    항상 Drive 쪽 목록 조회 결과에서만 가져오므로(클라이언트가 보낸 이름을 신뢰하지 않음)
    경로 조작 위험이 없다(2026-09-01 "폴더 선택 UI 추가" 확정 로그).

    월로 거르지 않고 폴더의 파일을 그대로 가져온다 — 범위 밖 데이터를 거르는 책임은 수집이
    아니라 기간·금액 검증 몫이다(interface-spec.md §실행 파라미터: "수집은 대상 월 위주로
    모으되 걸러내는 책임은 지지 않는다"). month는 반환값(신규 저장 파일 목록)을 통해
    "Drive에서 가져옴" 표시에만 쓴다.
    """
    token = _drive_access_token()
    if folder_id:
        target_folder_id = folder_id
    else:
        folder_name = load_env_value("DRIVE_FOLDER") or "ledger-flow"
        folder = _drive_find_folder(token, folder_name)
        if not folder:
            return f"Drive에서 폴더 \"{folder_name}\"을(를) 찾지 못했습니다."
        target_folder_id = folder["id"]

    entries = _drive_list_folder_files(token, target_folder_id)
    if file_ids is not None:
        wanted = set(file_ids)
        entries = [e for e in entries if e["id"] in wanted]

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    before = {e.name for e in os.scandir(UPLOAD_DIR) if e.is_file()}
    saved, skipped = _drive_download_entries(token, entries)
    after = {e.name for e in os.scandir(UPLOAD_DIR) if e.is_file()}
    add_to_drive_manifest(after - before)  # 실제로 새로 생긴 파일만 "Drive에서 가져옴"으로 표시

    lines = [f"저장한 파일 ({len(saved)}개): " + (", ".join(saved) if saved else "없음")]
    if skipped:
        lines.append(f"건너뛴 파일 ({len(skipped)}개): " + ", ".join(skipped))
    return "\n".join(lines)


CONFIRM_WAIT_SECONDS = 600  # 중간 확인 응답 대기 상한 10분 — 초과 시 전부 유지로 진행 (단계 문서 확정)


class RunState:
    """실행 1회의 관찰 상태 — 메모리에만 유지한다 (서버 재시작 시 소멸)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.month = None
        self.events = []       # run-pipeline.py의 on_event가 쌓는 진행 이벤트
        self.error = None      # 실행기 자체가 예외로 죽은 경우의 사유
        self.fraud_check = False  # 이 실행의 부정 사용 감지 토글 — 화면이 도중에 붙을 때 검증 구성 복원용
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
            self.fraud_check = bool(fraud_check)
            self.confirm = None
            self.confirm_result = None
            self.confirm_ready.clear()
        thread = threading.Thread(target=self._work, args=(month, upload_dir, fraud_check),
                                  daemon=True)
        thread.start()
        return True

    def start_refix(self, month, resolutions):
        """재결산 시작 — 결산 완료 후 확인 반영 (통합만 재실행, run_refix). 일반 실행과 동시 불가."""
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.month = month
            self.events = []
            self.error = None
            self.fraud_check = False
            self.confirm = None
            self.confirm_result = None
            self.confirm_ready.clear()
        thread = threading.Thread(target=self._work_refix, args=(month, resolutions), daemon=True)
        thread.start()
        return True

    def _work_refix(self, month, resolutions):
        try:
            pipeline.run_refix(month, resolutions, on_event=self.on_event)
        except Exception as e:
            with self.lock:
                self.error = str(e)
        finally:
            with self.lock:
                self.running = False

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
                "fraud_check": self.fraud_check,
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

        if path == "/uploads/manual":
            self._send_json(200, {"entries": load_manual_entries()})
            return

        if path == "/drive/browse":
            parent_match = re.search(r"(?:^|&)parent_id=([\w-]+)(?:&|$)", query)
            try:
                listing = list_drive_children(parent_match.group(1) if parent_match else None)
            except Exception as e:
                self._send_json(502, {"error": str(e)})
                return
            self._send_json(200, listing)
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

        if path == "/refix/pending":
            # 결산 완료 후 남은 확인 필요 건 — 중간 확인(검증 시점)과 같은 payload 모양으로 준다
            month_q = re.search(r"(?:^|&)month=(\d{4}-\d{2})(?:&|$)", query)
            month = month_q.group(1) if month_q else ""
            if run_state.running:
                self._send_json(409, {"error": "결산이 실행 중입니다 — 끝난 뒤 다시 시도해 주세요"})
                return
            if not month or not pipeline.can_refix(month):
                self._send_json(409, {"error": f"재결산 대상({month})의 단계 산출물이 없습니다 "
                                               "(재결산 도입 전 보관본) — 자료를 올려 처음부터 결산해 주세요"})
                return
            # 칸 산출물이 그 달 것이면 작업 칸에서, 아니면 보관본(stages/)에서 읽기 전용으로 만든다
            rows = (pipeline.build_verify_pending()
                    if pipeline.current_workspace_month() == month
                    else pipeline.archive_pending(month))
            self._send_json(200, {"month": month, "stage": "verify",
                                  "editable": pipeline.CONFIRM_EDITABLE["verify"],
                                  "rows": rows})
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
            month = month_match.group(1) if month_match else run_state.month
            if month_match:
                file_path = os.path.join(ARCHIVE_DIR, month_match.group(1),
                                         os.path.basename(file_path))
            # inline=1 — 브라우저 안에서 바로 보여준다 (화면 PDF 미리보기용). 없으면 내려받기
            inline = re.search(r"(?:^|&)inline=1(?:&|$)", query) is not None
            self._send_file(file_path, content_type,
                            download_name=None if inline else _download_name(file_path, month))
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

        if self.path == "/runs/refix":
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {}
            month = body.get("month", "")
            resolutions = body.get("resolutions")
            if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
                self._send_json(400, {"error": "month는 YYYY-MM 형식이어야 합니다"})
                return
            if not isinstance(resolutions, list) or not resolutions:
                self._send_json(400, {"error": "반영할 확인 건이 없습니다 — 행을 체크해 주세요"})
                return
            # 대상 검증은 실행기(run_refix)가 한 번 더 한다 — 여기선 즉시 안내용 선검사만
            # (칸 산출물이 다른 달 것이면 실행기가 보관본 stages/에서 복원한 뒤 진행한다)
            if not pipeline.can_refix(month):
                self._send_json(409, {"error": f"재결산 대상({month})의 단계 산출물이 없습니다 "
                                               "(재결산 도입 전 보관본) — 자료를 올려 처음부터 결산해 주세요"})
                return
            if run_state.start_refix(month, resolutions):
                self._send_json(200, {"started": True, "month": month, "refix": True})
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
            folder_id = body.get("folder_id")
            if folder_id is not None and not isinstance(folder_id, str):
                self._send_json(400, {"error": "folder_id는 문자열이어야 합니다"})
                return
            file_ids = body.get("file_ids")
            if file_ids is not None and (not isinstance(file_ids, list)
                                          or not all(isinstance(x, str) for x in file_ids)):
                self._send_json(400, {"error": "file_ids는 문자열 배열이어야 합니다"})
                return
            try:
                report = drive_import(month, folder_id=folder_id, file_ids=file_ids)
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
            if os.path.exists(DRIVE_MANIFEST_PATH):
                os.remove(DRIVE_MANIFEST_PATH)
            if os.path.exists(MANUAL_ENTRIES_PATH):
                os.remove(MANUAL_ENTRIES_PATH)
            self._send_json(200, {"files": []})
            return

        if self.path == "/uploads/manual":
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                self._send_json(400, {"error": "요청 본문이 JSON이 아닙니다"})
                return
            try:
                entries = add_manual_entry(payload)
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
                return
            self._send_json(200, {"entries": entries})
            return

        if self.path == "/uploads/manual/delete":
            try:
                entry_id = int(json.loads(raw).get("id")) if raw else None
            except (json.JSONDecodeError, TypeError, ValueError):
                entry_id = None
            entries = delete_manual_entry(entry_id) if entry_id is not None else load_manual_entries()
            self._send_json(200, {"entries": entries})
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
