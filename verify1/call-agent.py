"""verify1 담당자(Claude)를 실제로 불러 쓰는 자리.

.claude/agents/verify1.md의 판단 규칙("AI 판단 / 일반 코드 구분")을 그대로 따른다 —
docs/categories.md에 정의된 카테고리 명칭과 일치하는지는 일반 코드로 문자열 대조하고,
반려 건에 대해 "어느 카테고리를 의도했는지" 추정할 때만 Claude를 부른다.
결과는 verify1/result.csv로 쓰고, 지휘에게 보낼 결과 보고(JSON)를 함께 돌려준다.
API 키는 팀 폴더 .env의 ANTHROPIC_API_KEY를 읽어 쓴다 (키 값은 이 파일에 적지 않는다).

사용법: echo "<검증할 CSV>" | python3 verify1/call-agent.py
"""

import csv
import io
import json
import os
import re
import sys

from anthropic import Anthropic

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)
ENV_PATH = os.path.join(REPO_ROOT, ".env")
AGENT_SPEC_PATH = os.path.join(REPO_ROOT, ".claude", "agents", "verify1.md")
CATEGORIES_PATH = os.path.join(REPO_ROOT, "docs", "categories.md")
RESULT_PATH = os.path.join(BASE_DIR, "result.csv")

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 4096

# refine이 넘기는 거래 표 스키마(docs/interface-spec.md 확정 v1) — 입력 자체가
# 완전히 비어(헤더도 없어) "확인 대상 없음" 자리표시 행을 만들어야 할 때만 쓴다.
DEFAULT_COLUMNS = [
    "transaction_id", "날짜", "금액", "결제처", "카테고리", "비고",
    "결제수단", "결제구분", "원거래통화", "원거래금액",
    "source_type", "source_file", "collect_status", "구매항목",
]

GUESS_SCHEMA = {
    "type": "object",
    "properties": {
        "guesses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "transaction_id": {"type": "string"},
                    "guess": {
                        "type": "string",
                        "description": "체계 명칭 목록 중 이 거래가 의도했을 카테고리 하나",
                    },
                },
                "required": ["transaction_id", "guess"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["guesses"],
    "additionalProperties": False,
}


def load_api_key() -> str:
    if not os.path.exists(ENV_PATH):
        raise RuntimeError(f".env 파일이 없습니다: {ENV_PATH}")
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                value = line.split("=", 1)[1].strip()
                if not value:
                    raise RuntimeError("ANTHROPIC_API_KEY 값이 비어 있습니다 (.env 확인)")
                return value
    raise RuntimeError("ANTHROPIC_API_KEY를 .env에서 찾을 수 없습니다")


def load_role_instruction() -> str:
    with open(AGENT_SPEC_PATH, encoding="utf-8") as f:
        content = f.read()
    # frontmatter(--- ... ---)를 걷어내고 본문(역할 지시문)만 그대로 쓴다
    match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
    return content[match.end():].strip() if match else content.strip()


def _append_unique(names, seen, name):
    if name and name not in seen:
        seen.add(name)
        names.append(name)


def load_category_sets():
    """docs/categories.md "지출 — 개인카드"·"지출 — 법인카드"·"수익" 표를 읽어
    결제구분별로 대조할 카테고리 명칭 목록을 만든다 — 판정 기준의 정본.

    지출은 개인/법인 카드가 각자 별도 2열 표(카테고리 | 포함 범위)로 분리돼 있다
    (2026-08-31 확정 — 설정 화면의 독립 편집에 맞춘 개편). 결제구분에 따라 대조할
    표가 다르다(개인결제 → 지출 — 개인카드, 법인결제 → 지출 — 법인카드,
    .claude/agents/verify1.md 판단 규칙). 수익은 표가 하나뿐이라 양쪽 결제구분에
    공통으로 쓴다.
    """
    with open(CATEGORIES_PATH, encoding="utf-8") as f:
        content = f.read()

    personal, corporate, income = [], [], []
    personal_seen, corporate_seen, income_seen = set(), set(), set()
    section = None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## 지출 — 개인카드"):
            section = "개인"
            continue
        if stripped.startswith("## 지출 — 법인카드"):
            section = "법인"
            continue
        if stripped.startswith("## 수익"):
            section = "수익"
            continue
        if stripped.startswith("##"):
            section = None
            continue
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not cells or not cells[0] or cells[0] == "카테고리":
            continue
        if set(cells[0]) <= {"-"}:  # 표 구분선(|---|---|) 걸러내기
            continue
        name = cells[0].strip("*").strip()
        if section == "개인":
            _append_unique(personal, personal_seen, name)
        elif section == "법인":
            _append_unique(corporate, corporate_seen, name)
        elif section == "수익":
            _append_unique(income, income_seen, name)

    personal_categories = personal + income
    corporate_categories = corporate + income
    return personal_categories, corporate_categories


def classify_rows(rows, personal_categories, corporate_categories):
    """일반 코드로 통과/반려를 가른다. 명칭 불일치 건은 사유를 비워 두고 반환한다(추정은 AI 몫).

    결제구분에 따라 대조할 카테고리 열이 다르다(verify1.md 판단 규칙) — 개인결제는
    개인카드 카테고리, 법인결제는 법인카드 카테고리와 대조한다. 다른 열의 명칭과만
    일치하는 경우(열이 어긋난 경우)는 추정 없이 바로 반려 사유를 만든다.
    """
    personal_set = set(personal_categories)
    corporate_set = set(corporate_categories)
    results = []
    needs_guess = []
    for row in rows:
        category = (row.get("카테고리") or "").strip()
        payment_type = (row.get("결제구분") or "").strip()
        tx_id = row.get("transaction_id", "")

        if not category:
            results.append({"transaction_id": tx_id, "result": "반려", "reason": "카테고리가 비어 있어 판정 불가"})
            continue

        if payment_type == "개인결제":
            target_set, other_set = personal_set, corporate_set
            target_label, other_label = "개인카드 카테고리", "법인카드 카테고리"
            candidates = personal_categories
        elif payment_type == "법인결제":
            target_set, other_set = corporate_set, personal_set
            target_label, other_label = "법인카드 카테고리", "개인카드 카테고리"
            candidates = corporate_categories
        else:
            results.append({
                "transaction_id": tx_id,
                "result": "반려",
                "reason": f'결제구분 값이 올바르지 않아("{payment_type}") 대조할 카테고리 열을 정할 수 없음',
            })
            continue

        if category in target_set:
            results.append({"transaction_id": tx_id, "result": "통과", "reason": ""})
        elif category in other_set:
            results.append({
                "transaction_id": tx_id,
                "result": "반려",
                "reason": f'카테고리 "{category}"는 {other_label} 열의 명칭 — {payment_type} 건은 {target_label} 열과 대조해야 함',
            })
        else:
            results.append({"transaction_id": tx_id, "result": "반려", "reason": None, "target_label": target_label})
            needs_guess.append((row, candidates))
    return results, needs_guess


def _default_call_model(system_prompt, user_message, schema):
    """독립 실행(CLI) 기본 호출 — API 키로 직접 Anthropic을 부른다.

    run-pipeline.py 등 호출자가 있는 환경에서는 그쪽의 client/CLI-폴백 호출을
    call_model로 주입해 쓴다 (API 키 없이 claude CLI 세션으로도 동작해야 하므로).
    """
    client = Anthropic(api_key=load_api_key())
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    return next(block.text for block in response.content if block.type == "text")


def guess_intended_categories(rows_needing_guess, call_model=None):
    """체계에 없는 명칭이 어느 카테고리를 의도했는지, AI 판단이 필요한 부분만 Claude에게 묻는다.

    call_model(system_prompt, user_message, schema) -> 응답 텍스트(JSON 문자열)를 주입받는다
    (호출자의 API/CLI-폴백 방식을 그대로 쓰기 위함) — 없으면 이 파일 단독 실행용 기본 호출을 쓴다.
    """
    if not rows_needing_guess:
        return {}

    call_model = call_model or _default_call_model
    lines = []
    for row, candidates in rows_needing_guess:
        lines.append(
            f"- transaction_id={row.get('transaction_id', '')} "
            f"카테고리=\"{row.get('카테고리', '')}\" "
            f"결제구분={row.get('결제구분', '')} "
            f"결제처={row.get('결제처', '')} "
            f"구매항목={row.get('구매항목', '')} "
            f"비고={row.get('비고', '')} "
            f"대조할 명칭 목록=[{', '.join(candidates)}]"
        )
    instruction = (
        "아래 거래들의 '카테고리' 값은 각자의 결제구분에 해당하는 체계 명칭과 일치하지 않는다. "
        "각 건마다 그 건의 '대조할 명칭 목록' 중 의도로 보이는 것을 하나씩 골라 transaction_id와 짝지어 돌려줘.\n\n"
        + "\n".join(lines)
    )
    text = call_model(load_role_instruction(), instruction, GUESS_SCHEMA)
    guesses = json.loads(text)["guesses"]
    return {g["transaction_id"]: g["guess"] for g in guesses}


def _write_csv(fieldnames, data_rows) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in data_rows:
        writer.writerow(row)
    csv_text = buf.getvalue()
    with open(RESULT_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(csv_text)
    return csv_text


def _empty_report(message="확인 대상 없음"):
    return {
        "stage": "verify1",
        "status": "empty",
        "output": "verify1/result.csv",
        "counts": {"total": 0, "ok": 0, "flagged": 0},
        "flags": [],
        "message": message,
    }


def _write_empty_result(fieldnames=None):
    header = list(fieldnames) if fieldnames else list(DEFAULT_COLUMNS)
    out_fieldnames = header + ["verify1_result", "verify1_reason"]
    blank_row = {col: "" for col in header}
    blank_row["verify1_result"] = "확인 대상 없음"
    blank_row["verify1_reason"] = "검증할 항목이 없음"
    csv_text = _write_csv(out_fieldnames, [blank_row])
    return {"csv": csv_text, "report": _empty_report()}


def _failed_result(message):
    report = {
        "stage": "verify1",
        "status": "failed",
        "output": "",
        "counts": {"total": 0, "ok": 0, "flagged": 0},
        "flags": [],
        "message": message,
    }
    return {"csv": "", "report": report}


def run_verify1(input_text: str, call_model=None) -> dict:
    """verify1을 실행해 result.csv를 쓰고, 지휘에게 보낼 결과 보고를 돌려준다.

    call_model은 guess_intended_categories로 그대로 전달한다 (호출자의 API/CLI-폴백
    방식 주입용 — 없으면 이 파일 단독 실행용 기본 호출을 쓴다).
    """
    input_text = (input_text or "").strip()
    if not input_text:
        return _write_empty_result()

    reader = csv.DictReader(io.StringIO(input_text))
    fieldnames = reader.fieldnames or []
    rows = list(reader)

    required_columns = ("transaction_id", "카테고리", "결제구분")
    missing = [c for c in required_columns if c not in fieldnames]
    if missing:
        return _failed_result(f"입력에 {', '.join(missing)} 컬럼이 없습니다 — 거래 표 스키마를 확인하세요")

    if not rows:
        return _write_empty_result(fieldnames)

    personal_categories, corporate_categories = load_category_sets()
    results, needs_guess = classify_rows(rows, personal_categories, corporate_categories)
    guesses = guess_intended_categories(needs_guess, call_model)

    for row, result in zip(rows, results):
        if result["reason"] is None:
            guess = guesses.get(result["transaction_id"])
            original = row.get("카테고리", "")
            target_label = result.get("target_label", "카테고리")
            if guess:
                result["reason"] = f'카테고리 "{original}"는 {target_label} 목록에 없음 — "{guess}"로 추정'
            else:
                result["reason"] = f'카테고리 "{original}"는 {target_label} 목록에 없음'

    out_fieldnames = fieldnames + ["verify1_result", "verify1_reason"]
    out_rows = []
    flags = []
    ok_count = 0
    for i, (row, result) in enumerate(zip(rows, results), start=1):
        out_row = dict(row)
        out_row["verify1_result"] = result["result"]
        out_row["verify1_reason"] = result["reason"]
        out_rows.append(out_row)
        if result["result"] == "통과":
            ok_count += 1
        else:
            flags.append({"row": i, "type": "반려", "reason": result["reason"]})

    csv_text = _write_csv(out_fieldnames, out_rows)

    report = {
        "stage": "verify1",
        "status": "ok" if not flags else "partial",
        "output": "verify1/result.csv",
        "counts": {"total": len(rows), "ok": ok_count, "flagged": len(flags)},
        "flags": flags,
        "message": "",
    }
    return {"csv": csv_text, "report": report}


def call_verify1_agent(input_text: str) -> str:
    """화면·CLI가 부르는 진입점. result.csv를 쓰고, 사람이 읽을 요약 + 지휘용 보고(JSON)를 텍스트로 합쳐 돌려준다."""
    result = run_verify1(input_text)
    report = result["report"]
    counts = report["counts"]

    summary_lines = [f"총 {counts['total']}건 · 통과 {counts['ok']}건 · 반려 {counts['flagged']}건"]
    if report["message"]:
        summary_lines.append(report["message"])
    if report["output"]:
        summary_lines.append(f"산출물: {report['output']}")
    summary = "\n".join(summary_lines)

    report_json = json.dumps(report, ensure_ascii=False, indent=2)
    return f"{summary}\n\n```json\n{report_json}\n```"


if __name__ == "__main__":
    input_text = sys.stdin.read()
    print(call_verify1_agent(input_text))
