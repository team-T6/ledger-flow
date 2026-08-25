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

MODEL = "claude-sonnet-5"
MAX_TOKENS = 4096

# refine이 넘기는 거래 표 스키마(docs/interface-spec.md 확정 v1) — 입력 자체가
# 완전히 비어(헤더도 없어) "확인 대상 없음" 자리표시 행을 만들어야 할 때만 쓴다.
DEFAULT_COLUMNS = [
    "transaction_id", "날짜", "금액", "결제처", "카테고리", "비고",
    "결제수단", "결제구분", "원거래통화", "원거래금액",
    "source_type", "collect_status", "구매항목",
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


def load_valid_categories() -> list:
    """docs/categories.md "지출"·"수익" 표의 카테고리 열(첫 컬럼)을 그대로 뽑는다 — 판정 기준의 정본."""
    with open(CATEGORIES_PATH, encoding="utf-8") as f:
        content = f.read()
    names = []
    seen = set()
    for line in content.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or not cells[0] or cells[0] == "카테고리":
            continue
        if set(cells[0]) <= {"-"}:  # 표 구분선(|---|---|) 걸러내기
            continue
        name = cells[0].strip("*").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def classify_rows(rows, valid_categories):
    """일반 코드로 통과/반려를 가른다. 명칭 불일치 건은 사유를 비워 두고 반환한다(추정은 AI 몫)."""
    valid_set = set(valid_categories)
    results = []
    needs_guess = []
    for row in rows:
        category = (row.get("카테고리") or "").strip()
        tx_id = row.get("transaction_id", "")
        if not category:
            results.append({"transaction_id": tx_id, "result": "반려", "reason": "카테고리가 비어 있어 판정 불가"})
        elif category in valid_set:
            results.append({"transaction_id": tx_id, "result": "통과", "reason": ""})
        else:
            results.append({"transaction_id": tx_id, "result": "반려", "reason": None})
            needs_guess.append(row)
    return results, needs_guess


def guess_intended_categories(rows_needing_guess, valid_categories):
    """체계에 없는 명칭이 어느 카테고리를 의도했는지, AI 판단이 필요한 부분만 Claude에게 묻는다."""
    if not rows_needing_guess:
        return {}

    client = Anthropic(api_key=load_api_key())
    lines = []
    for row in rows_needing_guess:
        lines.append(
            f"- transaction_id={row.get('transaction_id', '')} "
            f"카테고리=\"{row.get('카테고리', '')}\" "
            f"결제처={row.get('결제처', '')} "
            f"구매항목={row.get('구매항목', '')} "
            f"비고={row.get('비고', '')}"
        )
    instruction = (
        "아래 거래들의 '카테고리' 값은 체계에 정의된 명칭과 일치하지 않는다. "
        "각 건마다 체계 명칭 목록 중 의도로 보이는 것을 하나씩 골라 transaction_id와 짝지어 돌려줘.\n\n"
        f"체계 명칭 목록: {', '.join(valid_categories)}\n\n"
        + "\n".join(lines)
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=load_role_instruction(),
        messages=[{"role": "user", "content": instruction}],
        output_config={"format": {"type": "json_schema", "schema": GUESS_SCHEMA}},
    )
    text = next(block.text for block in response.content if block.type == "text")
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


def run_verify1(input_text: str) -> dict:
    """verify1을 실행해 result.csv를 쓰고, 지휘에게 보낼 결과 보고를 돌려준다."""
    input_text = (input_text or "").strip()
    if not input_text:
        return _write_empty_result()

    reader = csv.DictReader(io.StringIO(input_text))
    fieldnames = reader.fieldnames or []
    rows = list(reader)

    if "카테고리" not in fieldnames or "transaction_id" not in fieldnames:
        return _failed_result("입력에 'transaction_id' 또는 '카테고리' 컬럼이 없습니다 — 거래 표 스키마를 확인하세요")

    if not rows:
        return _write_empty_result(fieldnames)

    valid_categories = load_valid_categories()
    results, needs_guess = classify_rows(rows, valid_categories)
    guesses = guess_intended_categories(needs_guess, valid_categories)

    for row, result in zip(rows, results):
        if result["reason"] is None:
            guess = guesses.get(result["transaction_id"])
            original = row.get("카테고리", "")
            if guess:
                result["reason"] = f'카테고리 "{original}"는 체계에 없음 — "{guess}"로 추정'
            else:
                result["reason"] = f'카테고리 "{original}"는 체계에 없음'

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
