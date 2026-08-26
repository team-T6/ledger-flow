"""지휘(orchestrator) 파이프라인 실행기.

수집 → 가공 → 검증 1·2(병렬) → 통합을 순서대로 진행시키고, 각 단계의 결과 보고
(envelope JSON)만 읽어 다음 진행을 판단한다 — 판단 규칙은 단계 문서
(.claude/agents/orchestrator.md) 확정 그대로. 산출물 내용은 열어보지 않는다.

단계 실행 방식 (단계 문서 "도구·코드" 확정):
- 수집·통합: 칸의 실행 코드(collect/collect.py · merge/build_result.py)를 직접 호출
  — 결과 보고도 그 코드가 만들어 반환한다
- 가공·검증 1·검증 2: 그 단계 문서를 시스템 프롬프트로 하는 API 단발 호출.
  응답을 구조화 출력(JSON 스키마)으로 강제해 산출물 CSV(artifact_csv)와 결과 보고
  (report)를 함께 받고, 산출물은 지휘가 그 칸의 result.csv로 써 준다 — 형식 위반은
  호출 실패 (임시 — 담당자 실행 코드가 생기면 직접 호출로 대체)
- 인증 폴백: .env에 ANTHROPIC_API_KEY가 없으면 같은 프롬프트를 claude CLI 헤드리스
  (claude -p, CLI 로그인 세션)로 실행한다. JSON-only 지시 + 코드 재검증으로 형식을 지킨다

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
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)
ENV_PATH = os.path.join(REPO_ROOT, ".env")
AGENTS_DIR = os.path.join(REPO_ROOT, ".claude", "agents")
LOGS_DIR = os.path.join(REPO_ROOT, "logs")
SUMMARY_PATH = os.path.join(BASE_DIR, "result-summary.md")

MODEL = "claude-sonnet-5"   # 판단형 단계(가공·검증) 공통 — effort high로 실행
MAX_TOKENS = 32000

STAGE_LABELS = {"collect": "수집", "refine": "가공", "verify1": "검증 1",
                "verify2": "검증 2", "merge": "통합", "summary": "최종 결과 요약"}

# 판단형 단계의 입력 산출물 (앞 단계 output 경로 — 거래 표는 CSV로 흐른다)
LLM_STAGE_INPUTS = {
    "refine": os.path.join(REPO_ROOT, "collect", "result.csv"),
    "verify1": os.path.join(REPO_ROOT, "refine", "result.csv"),
    "verify2": os.path.join(REPO_ROOT, "refine", "result.csv"),
}

# 판단형 단계가 참조해야 하는 기준 문서 — 단발 호출은 파일을 못 읽으므로 메시지에 동봉한다
# (동봉하지 않으면 모델이 체계를 지어내 분류·판정한다)
LLM_STAGE_REFERENCES = {
    "refine": [os.path.join(REPO_ROOT, "docs", "categories.md")],
    "verify1": [os.path.join(REPO_ROOT, "docs", "categories.md")],
    "verify2": [],
}

STATUS_VOCAB = {"ok", "empty", "partial", "failed"}

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
                               # interface-spec "단계 결과 보고" flags[].type 어휘 고정
                               "type": {"type": "string",
                                        "enum": ["확인 필요", "반려", "오류", "미완"]},
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


def call_claude_cli(system_prompt, user_message):
    """claude CLI 헤드리스 호출 — API 키 없이 CLI 로그인 세션으로 실행하는 인증 폴백.

    CLI에는 구조화 출력 강제가 없으므로 JSON-only 지시는 호출자가 메시지에 얹고,
    응답은 json 파싱 + validate_envelope로 재검증한다 (형식 위반은 재시도 1회 정책이 흡수).
    """
    result = subprocess.run(
        ["claude", "-p", "--model", MODEL, "--append-system-prompt", system_prompt],
        input=user_message, capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS)
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
    return problems


class Run:
    """실행 1회 — run 디렉터리·로그·보고 아카이브의 쓰기 주체는 지휘 하나다.

    on_event: 웹 서버 등 관찰자에게 진행을 알리는 훅 (없으면 CLI 그대로).
    로그 한 줄이 곧 이벤트 한 건 — 파일 기록과 통지가 어긋나지 않는다.
    """

    def __init__(self, month, on_event=None):
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
        self.log("run", "시작", memo=f"대상 월 {month}")

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

    def archive(self, stage, envelope, retry=False):
        name = f"report-{stage}-retry.json" if retry else f"report-{stage}.json"
        with open(os.path.join(self.run_dir, name), "w", encoding="utf-8") as f:
            json.dump(envelope, f, ensure_ascii=False, indent=2)

    def call_stage(self, stage, fn):
        """단계 1회 실행 + failed면 동일 입력 재호출 1회 (확정 정책). 확정 envelope를 돌려준다."""
        self.log(stage, "시작")
        envelope = self._safe_call(stage, fn)
        self.archive(stage, envelope)

        if envelope["status"] == "failed":
            self.log(stage, "실패", envelope["counts"], envelope["message"])
            retry_env = self._safe_call(stage, fn)
            self.archive(stage, retry_env, retry=True)
            self.retried[stage] = [envelope["message"], retry_env["message"]]
            envelope = retry_env

        if envelope["status"] == "failed" and not envelope["message"]:
            envelope["message"] = "사유 미기재 실패"
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

def run_collect(month, upload_dir=None):
    if upload_dir and any(e.is_file() for e in os.scandir(upload_dir)):
        mod = load_module("collect_uploads_stage",
                          os.path.join(REPO_ROOT, "collect", "collect_uploads.py"))
        return mod.run(month, upload_dir)["envelope"]
    mod = load_module("collect_stage", os.path.join(REPO_ROOT, "collect", "collect.py"))
    return mod.run(month)["envelope"]


def run_merge():
    mod = load_module("merge_stage", os.path.join(REPO_ROOT, "merge", "build_result.py"))
    return mod.run()["envelope"]


def make_llm_stage(client, stage, month):
    """판단형 단계 실행 함수 — 단계 문서를 시스템 프롬프트로, 입력 CSV를 메시지로 보낸다."""
    def call():
        input_path = LLM_STAGE_INPUTS[stage]
        if not os.path.exists(input_path):
            return failed_envelope(stage, f"입력 파일 없음: {os.path.relpath(input_path, REPO_ROOT)}")
        with open(input_path, encoding="utf-8-sig") as f:
            input_csv = f.read()

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
        if client is not None:
            # max_tokens가 커서 스트리밍 필수 (SDK 장시간 요청 가드) — 최종 메시지만 받는다
            with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=load_stage_doc(stage),
                messages=[{"role": "user", "content": user_message}],
                output_config={"effort": "high", "format": {"type": "json_schema", "schema": LLM_RESPONSE_SCHEMA}},
            ) as stream:
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
            text = extract_json_text(call_claude_cli(load_stage_doc(stage), cli_message))
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


# ---------- 최종 결과 요약 ----------

def stage_row(stage, envelope):
    if envelope is None:
        return f"| {STAGE_LABELS[stage]} | 미실행 | - | - |"
    c = envelope["counts"]
    output = envelope["output"]
    output_txt = ", ".join(output) if isinstance(output, list) else (output or "-")
    return f"| {STAGE_LABELS[stage]} | {envelope['status']} | {c['total']} / {c['ok']} / {c['flagged']} | {output_txt} |"


def write_summary(run):
    lines = [f"# 최종 결과 요약 ({run.month} 결산)", ""]
    for warning in run.warnings:
        lines += [f"> **경고: {warning}**", ""]

    lines += ["## 1. 단계별 진행 현황", "",
              "| 단계 | 상태 | 처리 건수 (전체/정상/확인필요) | 산출물 |", "|---|---|---|---|"]
    for stage in ("collect", "refine", "verify1", "verify2", "merge"):
        lines.append(stage_row(stage, run.envelopes.get(stage)))
    lines.append("")

    flags = [(stage, f) for stage in ("collect", "refine", "verify1", "verify2", "merge")
             for f in (run.envelopes.get(stage) or {}).get("flags", [])]
    lines += ["## 2. 확인 필요 목록", ""]
    if flags:
        lines += ["| 행 번호 | 단계 | 유형 | 사유 |", "|---|---|---|---|"]
        lines += [f"| {f['row'] or '-'} | {STAGE_LABELS[stage]} | {f['type']} | {f['reason']} |"
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


def run_pipeline(month, on_event=None, upload_dir=None):
    """파이프라인 1회 실행 — CLI(main)와 웹 서버가 같은 본체를 쓴다. Run을 돌려준다.

    upload_dir가 주어지고 파일이 있으면 수집을 그 업로드 파일들로 실행한다
    (없으면 기존 sample_data 경로 — CLI·대시보드 하위 호환).
    예외로 중단돼도 요약(run.log·result-summary.md)은 남기고, on_event에는
    반드시 종결 이벤트(state: 종료/오류)가 마지막으로 흐른다.
    """
    run = Run(month, on_event=on_event)
    try:
        _run_stages(run, month, upload_dir=upload_dir)
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


def _run_stages(run, month, upload_dir=None):
    run.notes.append(f"대상 월: {month}")
    if upload_dir:
        upload_count = sum(1 for e in os.scandir(upload_dir) if e.is_file())
        run.notes.append(f"수집 원천: 웹 업로드 파일 {upload_count}건")

    # 신선도 — 이번 실행이 만들 판단형 단계 산출물의 이전 실행 잔재를 지운다
    # (통합이 파일로 읽는 자리라, 옛 결과가 이번 실행 것으로 오인되지 않게)
    for stage in ("refine", "verify1", "verify2"):
        stale = os.path.join(REPO_ROOT, stage, "result.csv")
        if os.path.exists(stale):
            os.remove(stale)

    def finish():
        run.log("summary", "시작")
        write_summary(run)
        run.log("summary", "완료", memo=os.path.relpath(SUMMARY_PATH, REPO_ROOT))
        print(f"최종 결과 요약: {SUMMARY_PATH}")
        print(f"실행 기록: {run.run_dir}/")

    # 1. 수집
    collect_env = run.call_stage("collect", lambda: run_collect(month, upload_dir))
    if collect_env["status"] == "empty":
        run.notes.append("수집 empty — 특례로 이후 단계 호출 없이 종료 (결산 대상 없음)")
        finish()
        return
    if collect_env["status"] == "failed":
        run.notes.append(f"수집 실패로 중단: {collect_env['message']}")
        finish()
        return

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
        refine_env = run.call_stage("refine", make_llm_stage(client, "refine", month))
    if refine_env["status"] == "failed":
        run.notes.append(f"가공 실패로 중단: {refine_env['message']}")
        finish()
        return
    if refine_env["counts"]["total"] != collect_env["counts"]["total"]:
        run.warnings.append(f"거래 행 유실 의심 — 수집 {collect_env['counts']['total']}건 → 가공 {refine_env['counts']['total']}건")

    # 3. 검증 1·2 병렬
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(run.call_stage, "verify1", make_llm_stage(client, "verify1", month))
        f2 = pool.submit(run.call_stage, "verify2", make_llm_stage(client, "verify2", month))
        v1_env, v2_env = f1.result(), f2.result()

    v_failed = [env for env in (v1_env, v2_env) if env["status"] == "failed"]
    if len(v_failed) == 2:
        run.notes.append("검증 1·2 모두 실패 — 중단 (검증 없는 장부는 만들지 않는다)")
        if all(any(k in env["message"] for k in ENV_PROBLEM_KEYWORDS) for env in v_failed):
            run.warnings.append("검증 실패 원인이 환경 문제로 보임 — 환경 조치 후 재실행 권고")
        finish()
        return
    for env in v_failed:  # 편측 실패 — 그 자리는 통합이 "미완"으로 흡수한다
        run.notes.append(f"{STAGE_LABELS[env['stage']]} 실패 — 해당 검증 자리는 통합에 '미완'으로 전달: {env['message']}")
    for env in (v1_env, v2_env):
        if env["status"] != "failed" and env["counts"]["total"] != refine_env["counts"]["total"]:
            run.warnings.append(f"거래 행 유실 의심 — 가공 {refine_env['counts']['total']}건 → {STAGE_LABELS[env['stage']]} {env['counts']['total']}건")

    # 4. 통합
    merge_env = run.call_stage("merge", run_merge)
    if merge_env["status"] == "failed":
        run.notes.append(f"통합 실패 — 최종 산출물 없음: {merge_env['message']}")
    elif merge_env["counts"]["total"] != refine_env["counts"]["total"]:
        run.warnings.append(f"거래 행 유실 의심 — 가공 {refine_env['counts']['total']}건 → 통합 {merge_env['counts']['total']}건")

    # 대량 반려 경고 (partial 임계 50% 초과)
    for stage in ("collect", "refine", "verify1", "verify2", "merge"):
        env = run.envelopes.get(stage)
        if env and env["status"] == "partial" and env["counts"]["total"] > 0:
            if env["counts"]["flagged"] / env["counts"]["total"] > 0.5:
                run.warnings.append(f"대량 반려 — 원인 점검 필요 ({STAGE_LABELS[stage]} flagged {env['counts']['flagged']}/{env['counts']['total']})")

    run.notes.append("전 단계 counts.total 대조 완료" + (" — 유실 의심 있음 (경고 참고)" if any("유실" in w for w in run.warnings) else " — 유실 없음"))
    finish()


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r"\d{4}-\d{2}", sys.argv[1]):
        print("사용법: python3 orchestrator/run-pipeline.py <대상 월 YYYY-MM>")
        sys.exit(1)
    run_pipeline(sys.argv[1])


if __name__ == "__main__":
    main()
