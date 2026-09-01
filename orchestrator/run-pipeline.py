"""지휘(orchestrator) 파이프라인 실행기.

수집 → 가공 → 분류/기간·금액 검증(병렬) → 통합을 순서대로 진행시키고, 각 단계의 결과 보고
(envelope JSON)만 읽어 다음 진행을 판단한다 — 판단 규칙은 단계 문서
(.claude/agents/orchestrator.md) 확정 그대로. 산출물 내용은 열어보지 않는다.

단계 실행 방식 (단계 문서 "도구·코드" 확정):
- 수집·통합: 칸의 실행 코드(collect/collect.py · merge/build_result.py)를 직접 호출
  — 결과 보고도 그 코드가 만들어 반환한다
- 가공·분류 검증: 각 단계 문서의 "AI 판단 / 일반 코드 구분"을 그대로 따르는 하이브리드
  실행 — 명칭 문자열 대조·PG사 정규화 같은 결정론적 부분은 그 칸의 코드
  (refine/refine.py의 run_refine_hybrid · verify1/call-agent.py의 run_verify1)가 코드로
  처리하고, 애매한 것만 make_call_model()로 만든 콜백을 통해 Claude에 묻는다
  (run_refine_hybrid · run_verify1_hybrid)
- 기간·금액 검증: 세 기준 모두 결정론적 대조라 담당자 실행 코드(verify2/verify2.py의
  run_verify2)가 AI 없이 코드로 판정한다 (단계 문서 "AI 판단 / 일반 코드 구분")
- 부정 사용 검증: 하이브리드 — 주말/시각·키워드 대조·집계·임계·스코어링은 담당자 실행
  코드(verify3/verify3.py의 run_verify3)가 코드로 판정하고, F3 문맥·F4 개인성 소비
  추론만 make_call_model 콜백으로 Claude에 묻는다
- 인증 폴백: .env에 ANTHROPIC_API_KEY가 없으면 하이브리드·단발 호출 모두 같은 프롬프트를
  claude CLI 헤드리스(claude -p, CLI 로그인 세션)로 실행한다. JSON-only 지시 + 코드
  재검증으로 형식을 지킨다

중간 확인 (웹 실행 한정): on_confirm 훅이 있으면 수집·가공 직후와 통합 직전에
확인 필요·반려 행을 사용자에게 보여 수정·확인을 받아 반영한다 — run_confirmation 참고.

실행 기록: logs/run_YYYYMMDD_HHMMSS/ — run.log + report-<단계>.json(재시도는 -retry)
최종 결과 요약: orchestrator/result-summary.md (stub.md 형식) — 어떤 경우에도 작성

사용법: python3 orchestrator/run-pipeline.py 2026-02
"""

import csv
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)
ENV_PATH = os.path.join(REPO_ROOT, ".env")
AGENTS_DIR = os.path.join(REPO_ROOT, ".claude", "agents")
LOGS_DIR = os.path.join(REPO_ROOT, "logs")
SUMMARY_PATH = os.path.join(BASE_DIR, "result-summary.md")

# 판단형 단계 모델 (단계 문서 "도구·코드" 확정) — 가공은 sonnet(effort medium),
# 분류/기간·금액/부정 사용 검증은 기준 대조 위주의 기계적 판정이라 haiku. Haiku 4.5는 effort 파라미터
# 미지원이라 STAGE_EFFORT에 넣지 않는다 (넣으면 API가 400을 돌려준다)
STAGE_MODELS = {"refine": "claude-sonnet-5",
                "verify1": "claude-haiku-4-5", "verify2": "claude-haiku-4-5",
                "verify3": "claude-haiku-4-5"}
STAGE_EFFORT = {"refine": "medium"}
MAX_TOKENS = 32000

STAGE_LABELS = {"collect": "수집", "refine": "가공", "verify1": "분류 검증",
                "verify2": "기간·금액 검증", "verify3": "부정 사용 검증", "verify": "검증",
                "merge": "통합", "summary": "최종 결과 요약", "archive": "보관"}
ARCHIVE_DIR = os.path.join(REPO_ROOT, "archive")  # 월별 산출물 보관 — gitignore가 커밋 차단

# 판단형 단계의 입력 산출물 (앞 단계 output 경로 — 거래 표는 CSV로 흐른다)
LLM_STAGE_INPUTS = {
    "refine": os.path.join(REPO_ROOT, "collect", "result.csv"),
    "verify1": os.path.join(REPO_ROOT, "refine", "result.csv"),
    "verify2": os.path.join(REPO_ROOT, "refine", "result.csv"),
    "verify3": os.path.join(REPO_ROOT, "refine", "result.csv"),
}

# 판단형 단계가 참조해야 하는 기준 문서 — 단발 호출은 파일을 못 읽으므로 메시지에 동봉한다
# (동봉하지 않으면 모델이 체계를 지어내 분류·판정한다)
LLM_STAGE_REFERENCES = {
    "refine": [os.path.join(REPO_ROOT, "docs", "categories.md")],
    "verify1": [os.path.join(REPO_ROOT, "docs", "categories.md")],
    "verify2": [],
    "verify3": [os.path.join(REPO_ROOT, "docs", "fraud-rules.md")],
}

STATUS_VOCAB = {"ok", "empty", "partial", "failed"}
# interface-spec "단계 결과 보고" flags[].type 어휘 고정
FLAG_TYPE_VOCAB = {"확인 필요", "반려", "확인 요청", "오류", "미완"}

# interface-spec.md "단계 결과 보고" 규격 — 판단형 단계 응답에 구조화 출력으로 강제한다
ENVELOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "stage": {"type": "string"},
        "status": {"type": "string", "enum": sorted(STATUS_VOCAB)},
        "output": {"type": "string"},
        "counts": {
            "type": "object",
            "properties": {"total": {"type": "integer"}, "ok": {"type": "integer"},
                           "flagged": {"type": "integer"}},
            "required": ["total", "ok", "flagged"],
            "additionalProperties": False,
        },
        "flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"row": {"type": "integer"},
                               "type": {"type": "string", "enum": sorted(FLAG_TYPE_VOCAB)},
                               "reason": {"type": "string"}},
                "required": ["row", "type", "reason"],
                "additionalProperties": False,
            },
        },
        "message": {"type": "string"},
    },
    "required": ["stage", "status", "output", "counts", "flags", "message"],
    "additionalProperties": False,
}

LLM_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "artifact_csv": {"type": "string", "description": "산출물 CSV 전문 (헤더 포함)"},
        "report": ENVELOPE_SCHEMA,
    },
    "required": ["artifact_csv", "report"],
    "additionalProperties": False,
}


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------- claude CLI 폴백 (인증 폴백 — 단계 문서 "도구·코드" 확정) ----------

CLI_TIMEOUT_SECONDS = 1800  # 판단형 단계 1회 호출 상한 — CLI가 매달린 채 파이프라인이 멎는 것 방지

# CLI 폴백 작업 디렉터리 — repo 안에서 실행하면 프로젝트 지침 파일(CLAUDE.md·AGENTS*.md)이
# 호출마다 통째로 주입되어 사용량을 낭비한다 (단계 문서 "도구·코드" 확정)
CLI_WORKDIR = tempfile.gettempdir()


def extract_json_text(text):
    """CLI 출력에서 JSON 본문만 추린다 — 코드펜스나 앞뒤 설명이 섞여도 복원한다."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def is_usage_limit_error(message):
    """실패 사유가 사용량 한도 초과인지 — CLI/API의 한도 오류 문구로 판별한다."""
    lowered = (message or "").lower()
    return "hit your limit" in lowered or "usage limit" in lowered


class CLIStreamUnsupported(RuntimeError):
    """설치된 claude CLI가 스트리밍 출력 플래그를 지원하지 않을 때 — 일반 호출로 폴백한다."""


def _call_claude_cli_stream(model, system_prompt, user_message, on_text):
    """claude CLI 스트리밍 호출 — 텍스트 델타를 on_text로 흘려 진행률 계산에 쓴다.

    stream-json 이벤트 중 text_delta만 진행 관찰에 쓰고, 최종 본문은 result 이벤트에서 받는다.
    """
    proc = subprocess.Popen(
        ["claude", "-p", "--model", model, "--append-system-prompt", system_prompt,
         "--output-format", "stream-json", "--include-partial-messages", "--verbose"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=CLI_WORKDIR)

    def feed():
        try:
            proc.stdin.write(user_message)
            proc.stdin.close()
        except Exception:
            pass  # 프로세스가 먼저 죽은 경우 — 아래 종료 코드 처리에서 드러난다
    threading.Thread(target=feed, daemon=True).start()

    timed_out = []
    watchdog = threading.Timer(CLI_TIMEOUT_SECONDS, lambda: (timed_out.append(True), proc.kill()))
    watchdog.start()
    final_text = None
    partial = []
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "stream_event":
                delta = (event.get("event") or {}).get("delta") or {}
                if delta.get("type") == "text_delta":
                    chunk = delta.get("text", "")
                    partial.append(chunk)
                    try:
                        on_text(chunk)
                    except Exception:
                        pass  # 관찰자 오류가 호출을 멈추면 안 된다
            elif event.get("type") == "result" and isinstance(event.get("result"), str):
                final_text = event["result"]
        returncode = proc.wait()
    finally:
        watchdog.cancel()
    if timed_out:
        raise RuntimeError(f"claude CLI 시간 초과 ({CLI_TIMEOUT_SECONDS}초)")
    if returncode != 0:
        detail = (proc.stderr.read() or "").strip()[:300]
        if "output-format" in detail or "include-partial-messages" in detail \
                or "unknown option" in detail.lower() or "unknown argument" in detail.lower():
            raise CLIStreamUnsupported(detail)
        raise RuntimeError(f"claude CLI 실행 실패 (exit {returncode}): {detail}")
    return final_text if final_text is not None else "".join(partial)


def call_claude_cli(model, system_prompt, user_message, on_text=None):
    """claude CLI 헤드리스 호출 — API 키 없이 CLI 로그인 세션으로 실행하는 인증 폴백.

    CLI에는 구조화 출력 강제가 없으므로 JSON-only 지시는 호출자가 메시지에 얹고,
    응답은 json 파싱 + validate_envelope로 재검증한다 (형식 위반은 재시도 1회 정책이 흡수).
    on_text가 있으면 스트리밍 출력으로 실행해 진행 관찰을 지원한다
    (설치된 CLI가 스트리밍 플래그를 모르면 일반 호출로 자동 폴백 — 진행 관찰만 없어진다).
    """
    if on_text is not None:
        try:
            return _call_claude_cli_stream(model, system_prompt, user_message, on_text)
        except CLIStreamUnsupported:
            pass
    result = subprocess.run(
        ["claude", "-p", "--model", model, "--append-system-prompt", system_prompt],
        input=user_message, capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS,
        cwd=CLI_WORKDIR)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise RuntimeError(f"claude CLI 실행 실패 (exit {result.returncode}): {detail}")
    return result.stdout


def load_api_key():
    if not os.path.exists(ENV_PATH):
        raise RuntimeError(f".env 파일이 없습니다: {ENV_PATH}")
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                value = line.split("=", 1)[1].strip()
                if value:
                    return value
                raise RuntimeError("ANTHROPIC_API_KEY 값이 비어 있습니다 (.env 확인)")
    raise RuntimeError("ANTHROPIC_API_KEY를 .env에서 찾을 수 없습니다")


def load_stage_doc(stage):
    """단계 문서 본문을 시스템 프롬프트로 쓴다 — frontmatter(--- ... ---)는 걷어낸다."""
    with open(os.path.join(AGENTS_DIR, f"{stage}.md"), encoding="utf-8") as f:
        content = f.read()
    match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
    return content[match.end():].strip() if match else content.strip()


def failed_envelope(stage, message):
    return {"stage": stage, "status": "failed", "output": "",
            "counts": {"total": 0, "ok": 0, "flagged": 0}, "flags": [],
            "message": message}


def empty_envelope(stage, message="확인 대상 없음"):
    """입력 표가 없거나 비어 있을 때 — 재시도 없이 바로 확정한다 (verify2.md·verify3.md
    "오류·예외 처리": "입력 표가 없거나 비어 있으면 → status: empty ... 재시도 없음")."""
    return {"stage": stage, "status": "empty", "output": "",
            "counts": {"total": 0, "ok": 0, "flagged": 0}, "flags": [],
            "message": message}


def validate_envelope(env, stage):
    """받은 보고가 규격에 맞는지 코드로 재검증한다 — 어긋나면 사유 목록을 돌려준다."""
    problems = []
    if not isinstance(env, dict):
        return ["결과 보고가 JSON 객체가 아님"]
    for key in ("stage", "status", "output", "counts", "flags", "message"):
        if key not in env:
            problems.append(f"필드 누락: {key}")
    if problems:
        return problems
    if env["stage"] != stage:
        problems.append(f"stage 불일치: {env['stage']} (기대: {stage})")
    if env["status"] not in STATUS_VOCAB:
        problems.append(f"status 어휘 위반: {env['status']}")
    counts = env["counts"]
    if not (isinstance(counts, dict) and all(isinstance(counts.get(k), int) for k in ("total", "ok", "flagged"))):
        problems.append("counts 형식 위반 (total·ok·flagged 정수 필요)")
    if not isinstance(env["flags"], list):
        problems.append("flags가 배열이 아님")
    else:
        for i, f in enumerate(env["flags"]):
            if not (isinstance(f, dict) and isinstance(f.get("row"), int)
                    and f.get("type") in FLAG_TYPE_VOCAB and isinstance(f.get("reason"), str)):
                problems.append(f"flags[{i}] 형식 위반 (row 정수·type 어휘·reason 문자열 필요)")
    return problems


class Run:
    """실행 1회 — run 디렉터리·로그·보고 아카이브의 쓰기 주체는 지휘 하나다.

    on_event: 웹 서버 등 관찰자에게 진행을 알리는 훅 (없으면 CLI 그대로).
    로그 한 줄이 곧 이벤트 한 건 — 파일 기록과 통지가 어긋나지 않는다.
    """

    def __init__(self, month, on_event=None, fraud_check=False):
        self.month = month
        self.on_event = on_event
        self._seq = 0
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = os.path.join(LOGS_DIR, f"run_{stamp}")
        os.makedirs(self.run_dir, exist_ok=True)
        self.log_path = os.path.join(self.run_dir, "run.log")
        self.envelopes = {}        # stage -> 확정 envelope
        self.retried = {}          # stage -> [1차 message, 2차 message] (재시도 발생 시)
        self.warnings = []         # 요약 맨 앞에 실을 경고
        self.notes = []            # 요약 4장 메모
        self._lock = threading.Lock()  # 검증 병렬 구간의 로그·이벤트 직렬화용
        memo = f"대상 월 {month}"
        if fraud_check:  # 실행 파라미터를 실행 로그에 기록 (interface-spec §실행 파라미터)
            memo += " · 부정 사용 감지 토글 켬"
        self.log("run", "시작", memo=memo)

    def log(self, stage, state, counts=None, memo="", envelope=None):
        counts_txt = ""
        if counts:
            counts_txt = f"total={counts['total']}, ok={counts['ok']}, flagged={counts['flagged']}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{now} | {STAGE_LABELS.get(stage, stage)} | {state} | {counts_txt} | {memo}"
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if self.on_event:
                self._seq += 1
                event = {"seq": self._seq, "time": now, "stage": stage,
                         "stage_label": STAGE_LABELS.get(stage, stage),
                         "state": state, "counts": counts, "memo": memo}
                if envelope is not None:
                    event["envelope"] = {"status": envelope["status"],
                                         "output": envelope["output"],
                                         "counts": envelope["counts"],
                                         "flags": envelope["flags"],
                                         "message": envelope["message"]}
                try:
                    self.on_event(event)
                except Exception:
                    pass  # 관찰자 오류가 파이프라인을 멈추면 안 된다

    def progress(self, stage, done, total):
        """진행 이벤트 — 관찰자(웹)에게만 흘린다. run.log에는 쓰지 않는다
        (실행 로그 형식은 시작/완료/실패 확정 — 진행률은 휘발성 관찰 데이터)."""
        if not self.on_event:
            return
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq,
                     "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     "stage": stage, "stage_label": STAGE_LABELS.get(stage, stage),
                     "state": "진행", "counts": None, "memo": "",
                     "progress": {"done": done, "total": total}}
            try:
                self.on_event(event)
            except Exception:
                pass  # 관찰자 오류가 파이프라인을 멈추면 안 된다

    def archive(self, stage, envelope, retry=False):
        name = f"report-{stage}-retry.json" if retry else f"report-{stage}.json"
        with open(os.path.join(self.run_dir, name), "w", encoding="utf-8") as f:
            json.dump(envelope, f, ensure_ascii=False, indent=2)

    def call_stage(self, stage, fn):
        """단계 1회 실행 + failed면 동일 입력 재호출 1회 (확정 정책). 확정 envelope를 돌려준다.

        예외: 사용량 한도 초과 실패는 한도 리셋 전엔 재호출해도 같은 결과라 재시도 없이
        바로 확정한다 (단계 문서 "판단 규칙" failed 처리 확정).
        """
        self.log(stage, "시작")
        envelope = self._safe_call(stage, fn)
        self.archive(stage, envelope)

        if envelope["status"] == "failed" and is_usage_limit_error(envelope["message"]):
            self.warnings.append(f"{STAGE_LABELS[stage]} — 사용량 한도 초과로 재시도 생략, "
                                 "한도 리셋 후 재실행 권고")
        elif envelope["status"] == "failed":
            self.log(stage, "실패", envelope["counts"], envelope["message"])
            retry_env = self._safe_call(stage, fn)
            self.archive(stage, retry_env, retry=True)
            self.retried[stage] = [envelope["message"], retry_env["message"]]
            envelope = retry_env

        if envelope["status"] == "failed" and not envelope["message"]:
            envelope["message"] = "사유 미기재 실패"
        if envelope["status"] == "failed" and stage in ("refine", "verify1", "verify2", "verify3"):
            # 실패 확정 전 attempt에서 산출물 CSV를 먼저 쓴 경우가 있을 수 있다 — 남아 있으면
            # 통합이 "성공한 산출물"로 오인하니 지운다 (그 자리는 통합이 "미완"으로 처리)
            stale = os.path.join(REPO_ROOT, stage, "result.csv")
            if os.path.exists(stale):
                os.remove(stale)
        state = "실패" if envelope["status"] == "failed" else "완료"
        self.log(stage, state, envelope["counts"], envelope["message"], envelope=envelope)
        self.envelopes[stage] = envelope
        return envelope

    def _safe_call(self, stage, fn):
        try:
            envelope = fn()
        except Exception as e:
            return failed_envelope(stage, f"실행 오류: {e}")
        problems = validate_envelope(envelope, stage)
        if problems:
            return failed_envelope(stage, "결과 보고 형식 위반: " + " / ".join(problems))
        return envelope


# ---------- 단계 실행 함수 ----------

def run_collect(month, upload_dir=None, on_progress=None):
    if upload_dir and any(e.is_file() for e in os.scandir(upload_dir)):
        mod = load_module("collect_uploads_stage",
                          os.path.join(REPO_ROOT, "collect", "collect_uploads.py"))
        return mod.run(month, upload_dir, on_progress=on_progress)["envelope"]
    mod = load_module("collect_stage", os.path.join(REPO_ROOT, "collect", "collect.py"))
    return mod.run(month)["envelope"]


def run_merge(month=None, fraud_check=False):
    mod = load_module("merge_stage", os.path.join(REPO_ROOT, "merge", "build_result.py"))
    return mod.run(month, fraud_check=fraud_check)["envelope"]


def make_call_model(client, model):
    """단계 문서 "AI 판단 / 일반 코드 구분"을 따르는 하이브리드 단계(가공·분류 검증)가 쓰는
    call_model(system_prompt, user_message, schema) — 애매한 판단만 골라 물을 때 호출한다.

    client가 있으면 API로, 없으면 claude CLI 로그인 세션 폴백으로 부른다 (make_llm_stage와
    같은 인증 폴백 원칙).
    """
    def call_model(system_prompt, user_message, schema):
        if client is not None:
            # max_tokens가 커서 비스트리밍 호출은 SDK 10분 제한 가드에 걸린다 — 스트리밍 필수
            with client.messages.stream(
                model=model, max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            ) as stream:
                response = stream.get_final_message()
            return next(b.text for b in response.content if b.type == "text")
        cli_message = (
            f"{user_message}\n\n"
            "출력 형식(반드시 지켜라): 설명·인사·코드펜스 없이, 아래 JSON 스키마를 따르는 "
            "단일 JSON 객체만 출력한다.\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )
        return extract_json_text(call_claude_cli(model, system_prompt, cli_message))
    return call_model


def run_refine_hybrid(client, month, on_progress=None):
    """가공(refine) 하이브리드 실행 — PG사 정규화는 코드로, 카테고리 분류만 AI에 묻는다
    (refine.md "AI 판단 / 일반 코드 구분" 확정, refine/refine.py의 run_refine_hybrid)."""
    def call():
        input_path = LLM_STAGE_INPUTS["refine"]
        if not os.path.exists(input_path):
            # refine.md "못 할 때": 입력 거래 표가 비어 있으면 처리 대상 없음(empty) — 재시도 없음
            return empty_envelope("refine", f"입력 파일 없음: {os.path.relpath(input_path, REPO_ROOT)}")
        with open(input_path, encoding="utf-8-sig") as f:
            input_csv = f.read()
        with open(LLM_STAGE_REFERENCES["refine"][0], encoding="utf-8") as f:
            categories_text = f.read()
        mod = load_module("refine_stage_hybrid", os.path.join(REPO_ROOT, "refine", "refine.py"))
        call_model = make_call_model(client, STAGE_MODELS["refine"])
        result = mod.run_refine_hybrid(input_csv, categories_text, load_stage_doc("refine"), call_model)
        if result["csv"]:
            with open(os.path.join(REPO_ROOT, "refine", "result.csv"), "w", encoding="utf-8-sig", newline="") as f:
                f.write(result["csv"])
        return result["report"]
    return call


def run_verify1_hybrid(client, month, on_progress=None):
    """분류 검증(verify1) 하이브리드 실행 — 명칭 문자열 대조는 코드로, 체계에 없는 명칭이
    어느 카테고리를 의도했는지 추정만 AI에 묻는다 (verify1.md "AI 판단 / 일반 코드 구분"
    확정, verify1/call-agent.py의 run_verify1)."""
    def call():
        input_path = LLM_STAGE_INPUTS["verify1"]
        if not os.path.exists(input_path):
            return failed_envelope("verify1", f"입력 파일 없음: {os.path.relpath(input_path, REPO_ROOT)}")
        with open(input_path, encoding="utf-8-sig") as f:
            input_csv = f.read()
        mod = load_module("verify1_stage_hybrid", os.path.join(REPO_ROOT, "verify1", "call-agent.py"))
        call_model = make_call_model(client, STAGE_MODELS["verify1"])
        result = mod.run_verify1(input_csv, call_model=call_model)
        return result["report"]
    return call


def run_verify2_code(month):
    """기간·금액 검증(verify2) 실행 — 세 기준 모두 결정론적 대조라 AI 없이 담당자 실행
    코드로 판정한다 (verify2.md "AI 판단 / 일반 코드 구분" 확정, verify2/verify2.py)."""
    def call():
        input_path = LLM_STAGE_INPUTS["verify2"]
        if not os.path.exists(input_path):
            return empty_envelope("verify2", f"입력 파일 없음: {os.path.relpath(input_path, REPO_ROOT)}")
        with open(input_path, encoding="utf-8-sig") as f:
            input_csv = f.read()
        mod = load_module("verify2_stage", os.path.join(REPO_ROOT, "verify2", "verify2.py"))
        return mod.run_verify2(input_csv, month)["report"]
    return call


def run_verify3_hybrid(client, month):
    """부정 사용 검증(verify3) 하이브리드 실행 — 결정론 기준·스코어링은 코드로, F3 문맥·F4
    추론만 AI에 묻는다 (verify3.md "AI 판단 / 일반 코드 구분" 확정, verify3/verify3.py)."""
    def call():
        input_path = LLM_STAGE_INPUTS["verify3"]
        if not os.path.exists(input_path):
            return empty_envelope("verify3", f"입력 파일 없음: {os.path.relpath(input_path, REPO_ROOT)}")
        with open(input_path, encoding="utf-8-sig") as f:
            input_csv = f.read()
        mod = load_module("verify3_stage_hybrid", os.path.join(REPO_ROOT, "verify3", "verify3.py"))
        call_model = make_call_model(client, STAGE_MODELS["verify3"])
        return mod.run_verify3(input_csv, call_model=call_model)["report"]
    return call


def make_llm_stage(client, stage, month, on_progress=None):
    """판단형 단계 실행 함수 — 단계 문서를 시스템 프롬프트로, 입력 CSV를 메시지로 보낸다.

    on_progress(stage, done, total): 처리 건수 기반 진행 통지 (API 스트리밍 경로 한정 —
    응답에 흘러드는 artifact_csv의 행 구분(이스케이프 \\n)을 세어 추정한다.
    CLI 폴백은 스트림이 없어 진행 통지 없이 실행된다).
    """
    def call():
        input_path = LLM_STAGE_INPUTS[stage]
        if not os.path.exists(input_path):
            # verify2.md·verify3.md "오류·예외 처리": 입력 표가 없거나 비어 있으면
            # empty로 바로 확정 — 재시도 없음
            return empty_envelope(stage, f"입력 파일 없음: {os.path.relpath(input_path, REPO_ROOT)}")
        with open(input_path, encoding="utf-8-sig") as f:
            input_csv = f.read()
        total_rows = max(0, len(list(csv.reader(io.StringIO(input_csv)))) - 1)

        references = ""
        for ref_path in LLM_STAGE_REFERENCES[stage]:
            with open(ref_path, encoding="utf-8") as f:
                references += f"\n\n참고 기준 문서 ({os.path.relpath(ref_path, REPO_ROOT)}) 전문 — 판단은 반드시 이 문서에 실제로 적힌 내용만 근거로 한다:\n\n{f.read()}"

        user_message = (
            f"결산 대상 월: {month} (실행 파라미터 — interface-spec §실행 파라미터)\n\n"
            f"입력 거래 표 CSV ({os.path.relpath(input_path, REPO_ROOT)}) 전문:\n\n{input_csv}"
            f"{references}\n\n"
            "위 입력을 단계 문서의 지시대로 처리하라. artifact_csv에는 산출물 CSV 전문(헤더 포함, "
            "입력 행을 지우거나 순서를 바꾸지 않은 것)을, report에는 단계 결과 보고를 담아라. "
            f"report.stage는 \"{stage}\", report.output은 \"{stage}/result.csv\"로 한다."
        )
        output_config = {"format": {"type": "json_schema", "schema": LLM_RESPONSE_SCHEMA}}
        if stage in STAGE_EFFORT:
            output_config["effort"] = STAGE_EFFORT[stage]
        if client is not None:
            # max_tokens가 커서 스트리밍 필수 (SDK 장시간 요청 가드) — 최종 메시지만 받는다
            with client.messages.stream(
                model=STAGE_MODELS[stage],
                max_tokens=MAX_TOKENS,
                system=load_stage_doc(stage),
                messages=[{"role": "user", "content": user_message}],
                output_config=output_config,
            ) as stream:
                if on_progress and total_rows:
                    # 응답 JSON은 artifact_csv부터 흐른다 — 문자열 안의 행 구분은 \n 두 글자로
                    # 이스케이프되므로 그 수를 센다 (첫 행은 헤더, 상한은 입력 행수)
                    on_progress(stage, 0, total_rows)
                    newline_count, reported = 0, 0
                    for delta in stream.text_stream:
                        newline_count += delta.count("\\n")
                        done = min(max(newline_count - 1, 0), total_rows)
                        if done > reported:
                            reported = done
                            on_progress(stage, done, total_rows)
                response = stream.get_final_message()
            text = next(b.text for b in response.content if b.type == "text")
        else:
            # 인증 폴백 — API 키 없이 claude CLI 로그인 세션으로 실행 (단계 문서 "도구·코드")
            cli_message = (
                f"{user_message}\n\n"
                "출력 형식(반드시 지켜라): 설명·인사·코드펜스 없이, 아래 JSON 스키마를 따르는 "
                "단일 JSON 객체만 출력한다.\n"
                f"{json.dumps(LLM_RESPONSE_SCHEMA, ensure_ascii=False)}"
            )
            on_text = None
            if on_progress and total_rows:
                # API 경로와 같은 원리 — 응답 텍스트의 행 구분(이스케이프 \n)을 세어 진행 통지
                on_progress(stage, 0, total_rows)
                counted = {"newlines": 0, "reported": 0}

                def on_text(chunk):
                    counted["newlines"] += chunk.count("\\n")
                    done = min(max(counted["newlines"] - 1, 0), total_rows)
                    if done > counted["reported"]:
                        counted["reported"] = done
                        on_progress(stage, done, total_rows)
            text = extract_json_text(call_claude_cli(STAGE_MODELS[stage], load_stage_doc(stage),
                                                     cli_message, on_text=on_text))
        data = json.loads(text)

        artifact_path = os.path.join(REPO_ROOT, stage, "result.csv")
        with open(artifact_path, "w", encoding="utf-8-sig") as f:
            f.write(data["artifact_csv"])

        # 보고 건수 가드 — counts.total은 단계 간 유실 감지의 근거라, 모델이 잘못 세면
        # 가짜 유실 경고가 난다. 산출물 CSV의 실제 데이터 행수를 정본으로 정정한다
        # (내용 판단이 아니라 기계적 행수 확인 — 산출물 내용을 열어보지 않는다는 원칙과 구분)
        report = data["report"]
        actual_rows = max(0, len(list(csv.reader(io.StringIO(data["artifact_csv"])))) - 1)
        counts = report.get("counts")
        if (report.get("status") in ("ok", "partial") and isinstance(counts, dict)
                and counts.get("total") != actual_rows):
            note = f"보고 건수 정정: total {counts.get('total')} → {actual_rows} (산출물 행수 기준)"
            counts["total"] = actual_rows
            counts["ok"] = actual_rows - counts.get("flagged", 0)
            report["message"] = f"{report['message']} / {note}" if report.get("message") else note
        return report
    return call


# ---------- 중간 확인 (사용자 입력 — 웹 실행 경로 한정, 단계 문서 "중간 확인" 확정) ----------

# 확인 지점별 사용자 수정 허용 필드 — 그 시점 문제의 원인 필드만 연다
# (가공의 결제구분: 미확정이면 카테고리 체계를 못 정하므로 여기서 사용자가 확정한다 —
#  interface-spec.md 확정 로그 2026-09-01)
CONFIRM_EDITABLE = {
    "collect": ["날짜", "금액", "결제처", "결제구분", "구매항목"],
    "refine": ["결제처", "결제구분", "카테고리"],
    "verify": ["날짜", "금액", "카테고리"],
}
USER_CONFIRM_NOTE = "[사용자 확인]"


def verify_editable_for(data):
    """검증·재결산 확인 지점에서 이 행이 열어 줄 수정 필드 — 공통 필드에 더해,
    그 행의 `결제구분`이 미확정(빈 값)일 때만 `결제구분`을 연다 (카테고리 체계가
    구분에 따라 갈리므로). 이미 정해진 명의는 잠근다 (interface-spec.md 확정 로그
    2026-09-01 — 명의 판정은 가공 단계 책임, 검증 결과를 명의 변경으로 뒤집지 않는다).
    """
    fields = list(CONFIRM_EDITABLE["verify"])
    if not str((data or {}).get("결제구분", "")).strip():
        fields.append("결제구분")
    return fields


def _verify_fix_allows(row, key):
    """검증 확인 반영에서 이 키를 이 행에 쓸 수 있는가 — 공통 허용 필드이거나,
    `결제구분`이면서 그 행이 아직 미확정일 때만 (서버 최종 방어)."""
    if key not in row:
        return False
    if key in CONFIRM_EDITABLE["verify"]:
        return True
    return key == "결제구분" and not str(row.get("결제구분", "")).strip()


def read_csv_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return rows, reader.fieldnames or []


def write_csv_rows(path, rows, fieldnames):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _tag_user_confirmed(row):
    memo = (row.get("비고") or "").strip()
    if USER_CONFIRM_NOTE not in memo:
        row["비고"] = f"{memo} {USER_CONFIRM_NOTE}".strip()


def _tid(row):
    return (row.get("transaction_id") or "").strip()


def clean_resolutions(resolutions):
    """관찰자(웹)가 보낸 사용자 입력을 방어적으로 거른다 — transaction_id + 문자열 필드 +
    선택 필드 exclude(결산 제외, boolean)만 통과."""
    fixes = []
    for item in resolutions or []:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("transaction_id") or "").strip()
        if not tid:
            continue
        fields = item.get("fields")
        if not isinstance(fields, dict):
            fields = {}
        fixes.append({"transaction_id": tid,
                      "exclude": bool(item.get("exclude")),
                      "fields": {str(k): str(v).strip() for k, v in fields.items()}})
    return fixes


def build_stage_pending(stage, envelope):
    """수집·가공 확인 지점 — 단계 보고 flags의 행 데이터를 그 칸 result.csv에서 읽어 만든다."""
    path = os.path.join(REPO_ROOT, stage, "result.csv")
    if not os.path.exists(path):
        return []
    rows, _ = read_csv_rows(path)
    pending = []
    for flag in envelope.get("flags", []):
        idx = flag.get("row")
        if not (isinstance(idx, int) and 1 <= idx <= len(rows)):
            continue
        row = rows[idx - 1]
        if not _tid(row):
            continue
        pending.append({"transaction_id": _tid(row), "type": flag["type"],
                        "reason": flag["reason"], "data": dict(row)})
    return pending


def apply_stage_fixes(stage, fixes):
    """수집·가공 확인 반영 — 그 칸 result.csv를 사용자 입력으로 갱신한다 (이후 단계가 재처리).

    수정 없이 확인만 한 행도 반영으로 친다 (수집은 collect_status를 확인됨으로).
    exclude 행은 값 수정 대신 result.csv에서 지워 이후 단계가 처리하지 않는다
    (결산 제외 — interface-spec.md 확정 로그 2026-09-01).
    (반영된 tid 집합, 제외된 tid 집합)을 돌려준다."""
    path = os.path.join(REPO_ROOT, stage, "result.csv")
    if not os.path.exists(path):
        return set(), set()
    rows, fieldnames = read_csv_rows(path)
    by_tid = {_tid(r): r for r in rows}
    editable = CONFIRM_EDITABLE[stage]
    applied, excluded = set(), set()
    for fix in fixes:
        row = by_tid.get(fix["transaction_id"])
        if row is None:
            continue
        if fix.get("exclude"):
            excluded.add(fix["transaction_id"])
            continue
        for key, value in fix["fields"].items():
            if key in editable and key in row:
                row[key] = value
        if stage == "collect" and "collect_status" in row:
            row["collect_status"] = "확인됨"
        _tag_user_confirmed(row)
        applied.add(fix["transaction_id"])
    if excluded:
        rows = [r for r in rows if _tid(r) not in excluded]
    if applied or excluded:
        write_csv_rows(path, rows, fieldnames)
    return applied, excluded


# 검증기별 "사용자 확인 대상" 판정값 — 1·2는 반려, 3은 확인 요청(부정 단정 없음)
VERIFY_FLAG_VALUES = {"verify1": "반려", "verify2": "반려", "verify3": "확인 요청"}


def build_verify_pending(refine_path=None, verify_paths=None):
    """검증 확인 지점(통합 직전) — 검증 CSV들의 반려·확인 요청 행을 transaction_id로 합쳐 만든다.

    경로 인자를 주면 그 파일들로 만든다 — 보관본(archive/<월>/stages/)을 복원 없이
    읽기 전용으로 볼 때 쓴다 (기본은 작업 칸의 result.csv들)."""
    refine_path = refine_path or LLM_STAGE_INPUTS["verify1"]  # refine/result.csv — 장부 값의 정본
    refine_by_tid = {}
    if os.path.exists(refine_path):
        refine_by_tid = {_tid(r): r for r in read_csv_rows(refine_path)[0]}
    pending = {}
    for stage, flag_value in VERIFY_FLAG_VALUES.items():
        path = (verify_paths or {}).get(stage) or os.path.join(REPO_ROOT, stage, "result.csv")
        if not os.path.exists(path):
            continue
        for row in read_csv_rows(path)[0]:
            if row.get(f"{stage}_result") != flag_value or not _tid(row):
                continue
            entry = pending.setdefault(_tid(row), {
                "transaction_id": _tid(row), "type": flag_value, "reasons": [],
                "data": dict(refine_by_tid.get(_tid(row), row))})
            entry["reasons"].append(
                f"{STAGE_LABELS[stage]}: {row.get(f'{stage}_reason', '')}".strip(" :"))
            if flag_value == "반려":  # 반려·확인 요청이 겹치면 반려가 대표 유형
                entry["type"] = "반려"
    result = []
    for entry in pending.values():
        entry["reason"] = " / ".join(entry.pop("reasons"))
        # 미확정 결제구분 행만 행별 editable로 결제구분을 연다 (없으면 payload 공통값 사용)
        row_editable = verify_editable_for(entry["data"])
        if row_editable != CONFIRM_EDITABLE["verify"]:
            entry["editable"] = row_editable
        result.append(entry)
    return result


def apply_verify_fixes(fixes):
    """검증 확인 반영 — refine 값을 갱신하고, 반려했던 검증 판정을 통과(사용자 확인)로 갱신한다.

    통합은 장부 값을 refine/result.csv에서 읽으므로 값 수정은 거기에 반영하고,
    검증 CSV에는 같은 값 반영 + 판정 갱신으로 두 파일이 어긋나지 않게 한다.
    exclude 행은 refine·검증 CSV 모두에서 지워 통합이 장부에 싣지 않는다 (결산 제외).
    (반영된 tid 집합, 제외된 tid 집합)을 돌려준다."""
    applied, excluded = set(), set()
    refine_path = LLM_STAGE_INPUTS["verify1"]
    if not os.path.exists(refine_path):
        return applied, excluded
    rows, fieldnames = read_csv_rows(refine_path)
    by_tid = {_tid(r): r for r in rows}
    for fix in fixes:
        row = by_tid.get(fix["transaction_id"])
        if row is None:
            continue
        if fix.get("exclude"):
            excluded.add(fix["transaction_id"])
            continue
        for key, value in fix["fields"].items():
            if _verify_fix_allows(row, key):
                row[key] = value
        _tag_user_confirmed(row)
        applied.add(fix["transaction_id"])
    if not applied and not excluded:
        return applied, excluded
    if excluded:
        rows = [r for r in rows if _tid(r) not in excluded]
    write_csv_rows(refine_path, rows, fieldnames)

    fixes_by_tid = {f["transaction_id"]: f for f in fixes}
    for stage, flag_value in VERIFY_FLAG_VALUES.items():
        path = os.path.join(REPO_ROOT, stage, "result.csv")
        if not os.path.exists(path):
            continue
        vrows, vfields = read_csv_rows(path)
        changed = False
        if excluded:
            kept = [r for r in vrows if _tid(r) not in excluded]
            changed = len(kept) != len(vrows)
            vrows = kept
        for row in vrows:
            if _tid(row) not in applied:
                continue
            for key, value in fixes_by_tid[_tid(row)]["fields"].items():
                if _verify_fix_allows(row, key):
                    row[key] = value
            _tag_user_confirmed(row)
            # 부정 사용 검증의 확인 요청은 "업무 사용 확정" 입력이 같은 방식으로 통과 처리한다
            if row.get(f"{stage}_result") == flag_value:
                row[f"{stage}_result"] = "통과"
                row[f"{stage}_reason"] = "사용자 확인"
            changed = True
        if changed:
            write_csv_rows(path, vrows, vfields)
    return applied, excluded


def stage_tid_by_row(stage):
    """단계 result.csv의 행 번호(1부터) → transaction_id 맵 — flags의 row 대조용.

    제외(exclude) 반영은 행을 지워 번호를 밀기 때문에, 반영 전에 만들어 둬야 한다."""
    path = os.path.join(REPO_ROOT, stage, "result.csv")
    if not os.path.exists(path):
        return {}
    rows, _ = read_csv_rows(path)
    return {i + 1: _tid(r) for i, r in enumerate(rows)}


def drop_resolved_flags(envelope, resolved, excluded, tid_by_row):
    """반영·제외된 행의 flags를 단계 보고에서 걷어내고 counts를 갱신한다 (요약의 확인 필요
    목록 정합). tid_by_row는 반영 전 스냅숏(stage_tid_by_row) — 반영된 행은 ok로 옮기고,
    제외된 행은 total에서도 뺀다."""
    if not resolved and not excluded:
        return
    kept, n_resolved, n_excluded = [], 0, 0
    for f in envelope.get("flags", []):
        tid = tid_by_row.get(f.get("row"))
        if tid in resolved:
            n_resolved += 1
        elif tid in excluded:
            n_excluded += 1
        else:
            kept.append(f)
    if n_resolved or n_excluded:
        envelope["flags"] = kept
        counts = envelope["counts"]
        counts["flagged"] = max(0, counts["flagged"] - n_resolved - n_excluded)
        counts["ok"] += n_resolved
        counts["total"] = max(0, counts["total"] - n_excluded)
        if envelope["status"] == "partial" and not kept:
            envelope["status"] = "ok"


def run_confirmation(run, on_confirm, point):
    """확인 지점 1곳 실행 — 대기(확인 대기) → 사용자 입력 반영(확인 반영) → 보고 갱신.

    on_confirm(payload)는 관찰자(웹 서버)가 구현한다: 사용자 입력(resolutions 목록)을
    돌려주거나, 응답이 없으면(대기 상한 초과 등) None을 돌려준다. CLI 실행은 훅이 없어 통과.
    """
    if on_confirm is None:
        return
    if point == "verify":
        pending = build_verify_pending()
    else:
        envelope = run.envelopes.get(point)
        if not envelope or envelope["status"] == "failed":
            return
        pending = build_stage_pending(point, envelope)
    if not pending:
        return

    run.log(point, "확인 대기", memo=f"확인 필요 {len(pending)}건 — 사용자 입력 대기")
    payload = {"stage": point, "editable": CONFIRM_EDITABLE[point],
               "month": run.month, "rows": pending}
    try:
        resolutions = on_confirm(payload)
    except Exception as e:
        run.log(point, "확인 반영", memo=f"관찰자 오류 — 전부 그대로 진행: {e}")
        run.notes.append(f"중간 확인({STAGE_LABELS[point]}) 관찰자 오류 — 확인 필요 {len(pending)}건 유지")
        return
    if resolutions is None:
        run.log(point, "확인 반영", memo="응답 없음(대기 상한 초과) — 전부 그대로 진행")
        run.notes.append(f"중간 확인({STAGE_LABELS[point]}) 응답 없음 — 확인 필요 {len(pending)}건 유지")
        return

    fixes = clean_resolutions(resolutions)
    if point == "verify":
        snapshots = {s: stage_tid_by_row(s) for s in VERIFY_FLAG_VALUES}
        applied, excluded = apply_verify_fixes(fixes)
        for stage in ("verify1", "verify2", "verify3"):
            env = run.envelopes.get(stage)
            if env and env["status"] != "failed":
                drop_resolved_flags(env, applied, excluded, snapshots.get(stage, {}))
        counts = None
    else:
        snapshot = stage_tid_by_row(point)
        applied, excluded = apply_stage_fixes(point, fixes)
        drop_resolved_flags(run.envelopes[point], applied, excluded, snapshot)
        counts = run.envelopes[point]["counts"]
    memo = (f"사용자 확인 반영 {len(applied)}건"
            + (f" · 결산 제외 {len(excluded)}건" if excluded else "")
            + f" · 그대로 유지 {len(pending) - len(applied) - len(excluded)}건")
    run.log(point, "확인 반영", counts=counts, memo=memo)
    run.notes.append(f"중간 확인({STAGE_LABELS[point]}): {memo}")


# ---------- 월별 보관 ----------

# 재결산 복원 대상 단계 — 보관 시 이 단계들의 result.csv와 단계 보고를 stages/에 같이 남긴다
REFIX_STAGES = ("collect", "refine", "verify1", "verify2", "verify3")


def archive_stages_dir(month):
    return os.path.join(ARCHIVE_DIR, month, "stages")


def archive_outputs(month, run_dir=None):
    """최종 산출물을 archive/<월>/에 보관한다 — 웹 보관함(months API)의 데이터 원천.

    어떤 실행 경로(웹 서버·CLI·직접 호출)로 결산해도 정상 종료면 보관되도록 파이프라인이 맡는다.
    보관된 월의 재결산(확인 반영) 복원용으로 단계 산출물과 단계 보고(run_dir의
    report-*.json)를 stages/에 함께 남긴다 (단계 문서 "재결산" 확정).
    실거래 정보라 archive/는 커밋이 차단되어 있다.
    """
    dest = os.path.join(ARCHIVE_DIR, month)
    os.makedirs(dest, exist_ok=True)
    for src, name in ((os.path.join(REPO_ROOT, "merge", "result.xlsx"), "result.xlsx"),
                      (os.path.join(REPO_ROOT, "merge", "result.pdf"), "result.pdf"),
                      (SUMMARY_PATH, "result-summary.md")):
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, name))
    stages_dir = archive_stages_dir(month)
    os.makedirs(stages_dir, exist_ok=True)
    for stage in REFIX_STAGES:
        src = os.path.join(REPO_ROOT, stage, "result.csv")
        stage_dst = os.path.join(stages_dir, f"{stage}.csv")
        if os.path.exists(src):
            shutil.copy2(src, stage_dst)
        elif os.path.exists(stage_dst):
            os.remove(stage_dst)  # 이번 실행에 없는 단계(예: 토글 끈 verify3)의 옛 보관 잔재 제거
    if run_dir and os.path.isdir(run_dir):
        for name in os.listdir(stages_dir):
            if name.startswith("report-") and name.endswith(".json"):
                os.remove(os.path.join(stages_dir, name))  # 보고는 이번 실행 것으로 통째 교체
        for name in os.listdir(run_dir):
            if name.startswith("report-") and name.endswith(".json"):
                shutil.copy2(os.path.join(run_dir, name), os.path.join(stages_dir, name))
    # 보관함 화면용 집계 수치 — merge의 집계 함수를 읽기 전용으로 재사용한다
    merge_mod = load_module("merge_stage_summary",
                            os.path.join(REPO_ROOT, "merge", "build_result.py"))
    transactions, incomplete = merge_mod.load_transactions()
    ok_rows, flagged_rows = merge_mod.split_transactions(transactions)
    summary = merge_mod.summarize(ok_rows)
    data = {
        "month": month,
        "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tx_count": len(transactions),
        "ok_count": len(ok_rows),
        "flagged_count": len(flagged_rows),
        "total_expense": summary["total_expense"],
        "total_income": summary["total_income"],
        "net": summary["total_income"] - summary["total_expense"],
        "by_category": summary["by_category"],
        "by_payer": summary["by_payer"],
        # 보관월 상세 화면의 주요 지출 표 — 화면에 필요한 필드만 상위 5건
        "top_spenders": [{"날짜": r.get("날짜", ""), "결제처": r.get("결제처", ""),
                          "카테고리": r.get("카테고리", ""), "지출": r.get("지출", 0)}
                         for r in summary["top_spenders"][:5]],
        "flags": [{"row": r["row"], "결제처": r.get("결제처", ""),
                   "reason": r.get("reason", "")} for r in flagged_rows],
        "incomplete": incomplete,
        # 보관월 상세 화면의 "거래 내역" 표 — 전체 거래(확인 필요 표시 포함)
        "rows": merge_mod.build_row_list(transactions, flagged_rows),
    }
    with open(os.path.join(dest, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return os.path.relpath(dest, REPO_ROOT)


# ---------- 최종 결과 요약 ----------

def stage_row(stage, envelope):
    if envelope is None:
        return f"| {STAGE_LABELS[stage]} | 미실행 | - | - |"
    c = envelope["counts"]
    output = envelope["output"]
    output_txt = ", ".join(output) if isinstance(output, list) else (output or "-")
    return f"| {STAGE_LABELS[stage]} | {envelope['status']} | {c['total']} / {c['ok']} / {c['flagged']} | {output_txt} |"


def summary_stages(run):
    """요약·집계에 쓸 단계 목록 — 부정 사용 검증은 토글을 켠 실행(보고 존재)에만 싣는다."""
    stages = ["collect", "refine", "verify1", "verify2"]
    if "verify3" in run.envelopes:
        stages.append("verify3")
    stages.append("merge")
    return stages


def write_summary(run):
    lines = [f"# 최종 결과 요약 ({run.month} 결산)", ""]
    for warning in run.warnings:
        lines += [f"> **경고: {warning}**", ""]

    lines += ["## 1. 단계별 진행 현황", "",
              "| 단계 | 상태 | 처리 건수 (전체/정상/확인필요) | 산출물 |", "|---|---|---|---|"]
    for stage in summary_stages(run):
        lines.append(stage_row(stage, run.envelopes.get(stage)))
    lines.append("")

    flags = [(stage, f) for stage in summary_stages(run)
             for f in (run.envelopes.get(stage) or {}).get("flags", [])]
    lines += ["## 2. 확인 필요 목록", ""]
    if flags:
        lines += ["| 행 번호 | 단계 | 유형 | 사유 |", "|---|---|---|---|"]
        lines += [f"| {f.get('row') or '-'} | {STAGE_LABELS[stage]} | {f.get('type', '?')} | {f.get('reason', '')} |"
                  for stage, f in flags]
    else:
        lines.append("없음")
    lines.append("")

    lines += ["## 3. 산출물 위치", ""]
    merge_env = run.envelopes.get("merge")
    if merge_env and merge_env["status"] in ("ok", "partial"):
        outputs = merge_env["output"] if isinstance(merge_env["output"], list) else [merge_env["output"]]
        lines += [f"- `{p}`" for p in outputs]
    else:
        lines.append("최종 산출물 없음 (진행 중단 — 아래 메모 참고)")
    lines.append("")

    lines += ["## 4. 메모", ""]
    lines += [f"- {note}" for note in run.notes]
    for stage, messages in run.retried.items():
        lines.append(f"- {STAGE_LABELS[stage]} 재시도 1회 — 1차: {messages[0]} / 2차: {messages[1]}")
    lines.append(f"- 실행 기록: `{os.path.relpath(run.run_dir, REPO_ROOT)}/`")

    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


ENV_PROBLEM_KEYWORDS = ("없음", "없습니다", "찾을 수 없", "로드 실패", "누락", "권한", "열 수 없")


def run_pipeline(month, on_event=None, upload_dir=None, on_confirm=None, fraud_check=False):
    """파이프라인 1회 실행 — CLI(main)와 웹 서버가 같은 본체를 쓴다. Run을 돌려준다.

    upload_dir가 주어지고 파일이 있으면 수집을 그 업로드 파일들로 실행한다
    (없으면 기존 sample_data 경로 — CLI·대시보드 하위 호환).
    on_confirm이 있으면(웹 실행 경로) 확인 지점(수집·가공 직후, 통합 직전)에서
    파이프라인을 일시정지하고 사용자 입력을 받아 반영한다 — run_confirmation 참고.
    fraud_check(부정 사용 감지 토글, 기본 꺼짐)를 켜면 검증 스테이지에 부정 사용 검증을
    함께 병렬 실행한다 (interface-spec §실행 파라미터 확정).
    예외로 중단돼도 요약(run.log·result-summary.md)은 남기고, on_event에는
    반드시 종결 이벤트(state: 종료/오류)가 마지막으로 흐른다.
    """
    run = Run(month, on_event=on_event, fraud_check=fraud_check)
    try:
        _run_stages(run, month, upload_dir=upload_dir, on_confirm=on_confirm,
                    fraud_check=fraud_check)
        run.log("run", "종료", memo="정상 종결")
    except Exception as e:
        run.notes.append(f"실행기 오류로 중단: {e}")
        try:
            write_summary(run)
        except Exception:
            pass
        run.log("run", "오류", memo=str(e))
        raise
    return run


def _run_stages(run, month, upload_dir=None, on_confirm=None, fraud_check=False):
    run.notes.append(f"대상 월: {month}")
    if fraud_check:
        run.notes.append("부정 사용 감지 토글: 켬 (부정 사용 검증 실행)")
    if upload_dir:
        upload_count = sum(1 for e in os.scandir(upload_dir) if e.is_file())
        run.notes.append(f"수집 원천: 웹 업로드 파일 {upload_count}건")

    # 신선도 — 이번 실행이 만들 판단형 단계 산출물의 이전 실행 잔재를 지운다
    # (통합이 파일로 읽는 자리라, 옛 결과가 이번 실행 것으로 오인되지 않게.
    # verify3는 토글과 무관하게 지운다 — 토글 끈 실행에 옛 결과가 남으면 안 된다)
    for stage in ("refine", "verify1", "verify2", "verify3"):
        stale = os.path.join(REPO_ROOT, stage, "result.csv")
        if os.path.exists(stale):
            os.remove(stale)

    def finish():
        # 대량 반려 경고 (partial 임계 50% 초과) — 어느 단계에서 중단되든 그때까지 쌓인
        # envelope을 기준으로 매번 검사한다 (orchestrator.md "판단 규칙" — 조기 종료
        # 경로도 예외 없음)
        for stage in summary_stages(run):
            env = run.envelopes.get(stage)
            if env and env["status"] == "partial" and env["counts"]["total"] > 0:
                if env["counts"]["flagged"] / env["counts"]["total"] > 0.5:
                    run.warnings.append(f"대량 반려 — 원인 점검 필요 ({STAGE_LABELS[stage]} flagged {env['counts']['flagged']}/{env['counts']['total']})")
        run.log("summary", "시작")
        write_summary(run)
        run.log("summary", "완료", memo=os.path.relpath(SUMMARY_PATH, REPO_ROOT))
        print(f"최종 결과 요약: {SUMMARY_PATH}")
        print(f"실행 기록: {run.run_dir}/")

    # 1. 수집 — 업로드 수집은 처리한 파일 수 기준으로 진행을 알린다
    collect_env = run.call_stage("collect", lambda: run_collect(
        month, upload_dir,
        on_progress=lambda done, total: run.progress("collect", done, total)))
    if collect_env["status"] == "empty":
        run.notes.append("수집 empty — 특례로 이후 단계 호출 없이 종료 (결산 대상 없음)")
        finish()
        return
    if collect_env["status"] == "failed":
        run.notes.append(f"수집 실패로 중단: {collect_env['message']}")
        finish()
        return

    # 중간 확인 1 — 수집이 확인 필요로 남긴 행을 가공 전에 사용자에게 (웹 실행 한정)
    run_confirmation(run, on_confirm, "collect")

    client = None
    client_error = None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=load_api_key())
    except Exception as e:
        client_error = str(e)
    if client is None and shutil.which("claude"):
        # 인증 폴백 — API 키가 없어도 claude CLI 로그인 세션으로 판단형 단계를 실행한다
        run.notes.append(f"판단형 단계를 claude CLI(로그인 세션)로 실행 — API 클라이언트 없음: {client_error}")
        client_error = None

    # 2. 가공
    if client_error:
        refine_env = run.call_stage("refine", lambda: failed_envelope(
            "refine", f"API 클라이언트 준비 실패: {client_error} (claude CLI 폴백도 불가 — CLI 미설치)"))
    else:
        refine_env = run.call_stage("refine", run_refine_hybrid(client, month, on_progress=run.progress))
    if refine_env["status"] == "failed":
        run.notes.append(f"가공 실패로 중단: {refine_env['message']}")
        finish()
        return
    if refine_env["counts"]["total"] != collect_env["counts"]["total"]:
        run.warnings.append(f"거래 행 유실 의심 — 수집 {collect_env['counts']['total']}건 → 가공 {refine_env['counts']['total']}건")

    # 중간 확인 2 — 가공이 확인 필요로 남긴 행(분류 불가 등)을 검증 전에 사용자에게
    run_confirmation(run, on_confirm, "refine")

    # 3. 검증 스테이지 병렬 — 기본 1·2, 부정 사용 감지 토글을 켠 실행은 3도 함께
    # verify1·3은 하이브리드(결정론 판정은 코드, 애매한 것만 AI), verify2는 전부 코드
    # (각 단계 문서 "AI 판단 / 일반 코드 구분" 확정)
    verify_stages = ["verify1", "verify2"] + (["verify3"] if fraud_check else [])
    with ThreadPoolExecutor(max_workers=len(verify_stages)) as pool:
        futures = {}
        for stage in verify_stages:
            if stage == "verify1":
                fn = run_verify1_hybrid(client, month, on_progress=run.progress)
            elif stage == "verify2":
                fn = run_verify2_code(month)
            else:
                fn = run_verify3_hybrid(client, month)
            futures[stage] = pool.submit(run.call_stage, stage, fn)
        verify_envs = {stage: f.result() for stage, f in futures.items()}
    v1_env, v2_env = verify_envs["verify1"], verify_envs["verify2"]

    # 중단 판단은 분류/기간·금액 검증 기준 (확정 — 부정 사용 검증 편측 실패는 "미완"으로 진행)
    v_failed = [env for env in (v1_env, v2_env) if env["status"] == "failed"]
    if len(v_failed) == 2:
        run.notes.append("분류/기간·금액 검증 모두 실패 — 중단 (검증 없는 장부는 만들지 않는다)")
        if all(any(k in env["message"] for k in ENV_PROBLEM_KEYWORDS) for env in v_failed):
            run.warnings.append("검증 실패 원인이 환경 문제로 보임 — 환경 조치 후 재실행 권고")
        finish()
        return
    failed_envs = [env for env in verify_envs.values() if env["status"] == "failed"]
    for env in failed_envs:  # 편측 실패 — 그 자리는 통합이 "미완"으로 흡수한다
        run.notes.append(f"{STAGE_LABELS[env['stage']]} 실패 — 해당 검증 자리는 통합에 '미완'으로 전달: {env['message']}")
    for env in verify_envs.values():
        if env["status"] != "failed" and env["counts"]["total"] != refine_env["counts"]["total"]:
            run.warnings.append(f"거래 행 유실 의심 — 가공 {refine_env['counts']['total']}건 → {STAGE_LABELS[env['stage']]} {env['counts']['total']}건")

    # 중간 확인 3 — 검증 반려 행을 통합 직전에 사용자에게 (수정·확인하면 장부에 실린다)
    run_confirmation(run, on_confirm, "verify")

    # 4. 통합
    merge_env = run.call_stage("merge", lambda: run_merge(month, fraud_check=fraud_check))
    if merge_env["status"] == "failed":
        run.notes.append(f"통합 실패 — 최종 산출물 없음: {merge_env['message']}")
    elif merge_env["counts"]["total"] != refine_env["counts"]["total"]:
        run.warnings.append(f"거래 행 유실 의심 — 가공 {refine_env['counts']['total']}건 → 통합 {merge_env['counts']['total']}건")

    run.notes.append("전 단계 counts.total 대조 완료" + (" — 유실 의심 있음 (경고 참고)" if any("유실" in w for w in run.warnings) else " — 유실 없음"))
    finish()

    # 5. 보관 — 정상 산출된 결산만 월별 보관함에 남긴다 (요약까지 쓴 뒤라 finish() 다음)
    if merge_env["status"] in ("ok", "partial"):
        try:
            dest = archive_outputs(month, run_dir=run.run_dir)
            run.log("archive", "완료", memo=f"월별 보관함에 저장 — {dest}/")
        except Exception as e:
            run.log("archive", "실패", memo=f"보관 실패: {e}")


# ---------- 재결산 (결산 완료 후 확인 반영 — 웹 실행 경로 한정, 단계 문서 "재결산" 확정) ----------

SUMMARY_MONTH_RE = re.compile(r"^# 최종 결과 요약 \((\d{4}-\d{2}) 결산\)")


def current_workspace_month():
    """지금 칸 산출물이 어느 달 결산 것인지 — 최종 결과 요약 머리글에서 읽는다."""
    if not os.path.exists(SUMMARY_PATH):
        return None
    with open(SUMMARY_PATH, encoding="utf-8") as f:
        match = SUMMARY_MONTH_RE.match(f.readline().strip())
    return match.group(1) if match else None


def archive_pending(month):
    """보관본의 단계 산출물로 확인 필요 목록을 만든다 — 복원 없는 읽기 전용 (재결산 화면용)."""
    stages_dir = archive_stages_dir(month)
    return build_verify_pending(
        refine_path=os.path.join(stages_dir, "refine.csv"),
        verify_paths={s: os.path.join(stages_dir, f"{s}.csv") for s in VERIFY_FLAG_VALUES})


def can_refix(month):
    """이 달을 재결산할 수 있는지 — 칸 산출물이 그 달 것이거나, 복원 가능한 보관본이 있으면 참."""
    return current_workspace_month() == month or \
        os.path.exists(os.path.join(archive_stages_dir(month), "refine.csv"))


def restore_stage_outputs(month):
    """보관본의 단계 산출물을 작업 칸으로 복원한다 — 보관된 월 재결산의 진입 단계."""
    stages_dir = archive_stages_dir(month)
    if not os.path.exists(os.path.join(stages_dir, "refine.csv")):
        raise RuntimeError(f"{month} 보관본에 단계 산출물이 없어 재결산할 수 없습니다 "
                           "(재결산 도입 전 보관본) — 자료를 올려 처음부터 결산해 주세요")
    for stage in REFIX_STAGES:
        src = os.path.join(stages_dir, f"{stage}.csv")
        dst = os.path.join(REPO_ROOT, stage, "result.csv")
        if os.path.exists(src):
            shutil.copy2(src, dst)
        elif os.path.exists(dst):
            os.remove(dst)  # 그 달 실행에 없던 단계의 잔재가 통합에 섞이지 않게 지운다


def load_stage_envelopes(run, month):
    """그 달 결산의 단계 보고를 되읽어 run.envelopes에 채운다 (재결산 요약의 단계 표용).

    우선순위: 보관본(archive/<월>/stages/) → report-merge.json이 있는 가장 최근 logs/run_*.
    재시도 보고(-retry)가 있으면 그쪽이 확정 보고다. 되읽은 보고는 이번 run 디렉터리에도
    다시 아카이브해 자립시킨다.
    """
    source_dir = None
    stages_dir = archive_stages_dir(month)
    if os.path.exists(os.path.join(stages_dir, "report-merge.json")):
        source_dir = stages_dir
    elif os.path.isdir(LOGS_DIR):
        for name in sorted(os.listdir(LOGS_DIR), reverse=True):
            prev_dir = os.path.join(LOGS_DIR, name)
            if prev_dir != run.run_dir and name.startswith("run_") \
                    and os.path.exists(os.path.join(prev_dir, "report-merge.json")):
                source_dir = prev_dir
                break
    if source_dir is None:
        return None
    for stage in ("collect", "refine", "verify1", "verify2", "verify3", "merge"):
        retry_path = os.path.join(source_dir, f"report-{stage}-retry.json")
        base_path = os.path.join(source_dir, f"report-{stage}.json")
        path = retry_path if os.path.exists(retry_path) else base_path
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                envelope = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue  # 깨진 아카이브는 건너뛴다 — 그 단계만 요약에 미실행으로 남는다
        if not validate_envelope(envelope, stage):
            run.envelopes[stage] = envelope
            run.archive(stage, envelope)
    return os.path.relpath(source_dir, REPO_ROOT)


def run_refix(month, resolutions, on_event=None):
    """결산 완료 후 확인 반영 재결산 — 검증 시점 수정과 같은 효력으로 반영하고 통합만 재실행한다.

    칸 산출물이 그 달 것이 아니면 보관본(archive/<월>/stages/)에서 복원한 뒤 진행한다 —
    복원할 보관본도 없으면(재결산 도입 전 보관본 등) 처음부터 실행하도록 거절한다.
    """
    restored = current_workspace_month() != month
    if restored:
        restore_stage_outputs(month)  # 복원 불가면 여기서 거절된다
    if not os.path.exists(LLM_STAGE_INPUTS["verify1"]):
        raise RuntimeError("가공 산출물(refine/result.csv)이 없습니다 — 처음부터 결산해 주세요")

    run = Run(month, on_event=on_event)
    try:
        run.notes.append(f"대상 월: {month}")
        if restored:
            run.notes.append(f"보관본에서 단계 산출물 복원 — archive/{month}/stages/")
        prev_name = load_stage_envelopes(run, month)
        run.notes.append("재결산 — 결산 완료 후 확인 필요 건 사용자 확인 반영, 통합만 재실행"
                         + (f" (그 결산의 단계 보고: {prev_name}/)" if prev_name else ""))

        fixes = clean_resolutions(resolutions)
        snapshots = {s: stage_tid_by_row(s) for s in VERIFY_FLAG_VALUES}
        applied, excluded = apply_verify_fixes(fixes)
        for stage in VERIFY_FLAG_VALUES:
            env = run.envelopes.get(stage)
            if env and env["status"] != "failed":
                drop_resolved_flags(env, applied, excluded, snapshots.get(stage, {}))
                run.archive(stage, env)  # 갱신된 counts·flags로 아카이브도 맞춘다
        memo = (f"재결산 — 사용자 확인 반영 {len(applied)}건"
                + (f" · 결산 제외 {len(excluded)}건" if excluded else ""))
        run.log("verify", "확인 반영", memo=memo)
        run.notes.append(memo)

        # 재결산은 fraud_check를 인자로 받지 않는다 — 그 결산 때 verify3 보고가 있었는지로
        # 토글 여부를 되짚는다(load_stage_envelopes가 위에서 이미 채워둔 값)
        refix_fraud_check = "verify3" in run.envelopes
        merge_env = run.call_stage("merge", lambda: run_merge(month, fraud_check=refix_fraud_check))
        if merge_env["status"] == "failed":
            run.notes.append(f"통합 실패 — 최종 산출물 없음: {merge_env['message']}")

        run.log("summary", "시작")
        write_summary(run)
        run.log("summary", "완료", memo=os.path.relpath(SUMMARY_PATH, REPO_ROOT))

        if merge_env["status"] in ("ok", "partial"):
            try:
                dest = archive_outputs(month, run_dir=run.run_dir)
                run.log("archive", "완료", memo=f"월별 보관함에 저장 — {dest}/")
            except Exception as e:
                run.log("archive", "실패", memo=f"보관 실패: {e}")
        run.log("run", "종료", memo="정상 종결 (재결산)")
    except Exception as e:
        run.notes.append(f"실행기 오류로 중단: {e}")
        try:
            write_summary(run)
        except Exception:
            pass
        run.log("run", "오류", memo=str(e))
        raise
    return run


def main():
    args = [a for a in sys.argv[1:] if a != "--fraud-check"]
    fraud_check = "--fraud-check" in sys.argv[1:]
    if len(args) != 1 or not re.fullmatch(r"\d{4}-\d{2}", args[0]):
        print("사용법: python3 orchestrator/run-pipeline.py <대상 월 YYYY-MM> [--fraud-check]")
        sys.exit(1)
    run_pipeline(args[0], fraud_check=fraud_check)


if __name__ == "__main__":
    main()
