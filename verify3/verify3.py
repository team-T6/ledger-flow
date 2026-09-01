"""부정 사용 검증(verify3) 실행 코드 — 하이브리드.

단계 문서(.claude/agents/verify3.md) "AI 판단 / 일반 코드 구분" 확정 그대로:
주말/시각 판정·키워드 포함 대조·동일 가맹점 건수 집계·금액 임계 비교·스코어링·사유
템플릿 채움은 일반 코드로 판정하고, 키워드 오탐 문맥 판단(F3)과 개인성 소비 의심(F4)
추론에만 call_model 콜백으로 AI를 쓴다. call_model이 없으면 F3는 키워드 대조 결과를
그대로 쓰고 F4는 건너뛴다 (판정을 지어내지 않는다).

기준 정본은 docs/fraud-rules.md — 실행 시점의 파일에서 업종 키워드 표와 F6 임계치를
읽어 적용한다 (파싱 실패 시 이 파일의 기본값 폴백).

결과는 verify3/result.csv로 쓰고, 지휘에게 보낼 결과 보고(JSON)를 함께 돌려준다.

사용법: python3 verify3/verify3.py  (입력: refine/result.csv)
"""

import csv
import io
import json
import os
import re
import sys
from datetime import date as date_cls

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)
RESULT_PATH = os.path.join(BASE_DIR, "result.csv")
RULES_PATH = os.path.join(REPO_ROOT, "docs", "fraud-rules.md")

REQUIRED_COLUMNS = ("transaction_id", "날짜", "금액", "결제처", "결제구분")

# fraud-rules.md 파싱 실패 시 폴백 (정본은 파일 — 실행 시점 기준)
FALLBACK_KEYWORDS = ["주점", "호프", "포차", "바", "클럽", "라운지", "노래방", "노래연습장",
                     "단란", "유흥", "룸", "가라오케", "마사지", "안마", "스파", "골프장",
                     "스크린골프", "카지노", "복권", "로또", "배팅", "성인"]
FALLBACK_F6_THRESHOLD = 300000

AI_SCHEMA = {
    "type": "object",
    "properties": {
        "f3": {"type": "array", "items": {
            "type": "object",
            "properties": {"transaction_id": {"type": "string"}, "is_match": {"type": "boolean"}},
            "required": ["transaction_id", "is_match"], "additionalProperties": False}},
        "f4": {"type": "array", "items": {
            "type": "object",
            "properties": {"transaction_id": {"type": "string"}, "basis": {"type": "string"}},
            "required": ["transaction_id", "basis"], "additionalProperties": False}},
    },
    "required": ["f3", "f4"], "additionalProperties": False,
}


def load_rules():
    """fraud-rules.md에서 F3 키워드 표와 F6 임계치를 읽는다 (실행 시점 파일 기준)."""
    keywords, threshold = FALLBACK_KEYWORDS, FALLBACK_F6_THRESHOLD
    try:
        with open(RULES_PATH, encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"### 업종 키워드 표(.*?)(?=\n#|\Z)", text, re.S)
        if m:
            # 섹션 안에서 "·" 구분자가 가장 많은 줄이 키워드 나열 줄이다 (설명 문장과 구분)
            line = max(m.group(1).splitlines(), key=lambda ln: ln.count("·"), default="")
            if line.count("·") >= 5:
                parsed = [re.sub(r"\(.*?\)", "", t).strip() for t in line.split("·")]
                parsed = [t for t in parsed if t]
                if parsed:
                    keywords = parsed
        m = re.search(r"기본 ([\d,]+)원", text)
        if m:
            threshold = int(m.group(1).replace(",", ""))
    except OSError:
        pass
    return keywords, threshold


def _detect_signals(row, same_day_counts, keywords, f6_threshold):
    """결정론 기준(F1·F2·F3 후보·F5·F6)을 적용해 (신호 목록, 판단 불가 여부)를 돌려준다.

    신호는 (기준, 강도, 사유) 튜플 — 강도는 fraud-rules.md 신호 강도 표기(약/중/강).
    F3는 키워드 대조 후보만 표시하고 문맥 확정은 호출자(AI)가 한다.
    """
    signals = []
    undecidable = False
    merchant = (row.get("결제처") or "").strip()
    memo = (row.get("비고") or "").strip()
    items = (row.get("구매항목") or "").strip()
    raw_date = (row.get("날짜") or "").strip()

    # F1 주말
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw_date)
    if m:
        if date_cls(int(m.group(1)), int(m.group(2)), int(m.group(3))).weekday() >= 5:
            signals.append(("F1", "약", f"주말 결제 (날짜: {raw_date})"))
    else:
        undecidable = True

    # F2 심야 — 비고·원문에 시각이 남은 행만 (없으면 건너뜀 — 블로커 확정)
    m = re.search(r"결제시각 (\d{1,2}):(\d{2})", memo) or re.search(r"\b(\d{1,2}):(\d{2})\b", memo)
    if m:
        hour = int(m.group(1))
        if hour >= 22 or hour < 6:
            signals.append(("F2", "중", f"심야 결제 (시각: {int(m.group(1)):02d}:{m.group(2)})"))

    # F3 업종 키워드 — 포함 대조는 코드, 경계·문맥 확정은 AI
    haystack = f"{merchant} {memo} {items}"
    for kw in keywords:
        if kw and kw in haystack:
            signals.append(("F3", "강", f"유흥·사행성 업종 의심 (결제처: {merchant}, 키워드: {kw})"))
            break

    # F5 동일 가맹점 단시간 연속 결제 — 같은 결제처·같은 날 2건 이상
    n = same_day_counts.get((raw_date, merchant), 0)
    if n >= 2:
        signals.append(("F5", "강", f"동일 가맹점 연속 결제 {n}건 (결제처: {merchant})"))

    # F6 단일 건 고액 — 지출(음수) 절대값 임계 이상
    try:
        amount = int((row.get("금액") or "").strip())
    except ValueError:
        amount = None
    if amount is not None and amount < 0 and abs(amount) >= f6_threshold:
        signals.append(("F6", "약", f"고액 결제 (금액: {abs(amount):,}원)"))

    return signals, undecidable


def _ask_ai(call_model, f3_candidates, corporate_rows, keywords):
    """F3 문맥 확정 + F4 개인성 소비 추론을 한 번의 호출로 묻는다."""
    system_prompt = (
        "너는 법인카드 부정 사용 검증(verify3)의 AI 판단 보조다. 부정 사용을 단정하지 말고, "
        "요청한 두 판단만 JSON으로 답한다.\n"
        "1) f3: 아래 키워드 대조 후보 각각이 실제 유흥·사행성 업종 문맥인지 판단한다 — "
        "가맹점명 안의 우연한 부분 문자열(예: \"바르다김선생\"의 \"바\")은 is_match=false.\n"
        "2) f4: 법인결제 행 중 개인성 소비(OTT·게임·개인 쇼핑 등)로 의심되는 행만 골라 "
        "transaction_id와 근거 한 줄(basis)을 적는다 — 결제처·카테고리·구매항목이 근거. "
        "업무 성격이 그럴듯하면 넣지 않는다.\n"
        f"유흥·사행성 키워드 표: {', '.join(keywords)}"
    )
    cand_lines = [f"- {c['transaction_id']}: 결제처 \"{c['merchant']}\", 걸린 키워드 \"{c['keyword']}\""
                  for c in f3_candidates] or ["(없음)"]
    row_lines = [
        f"- {r['transaction_id']}: 결제처 \"{r.get('결제처', '')}\", 카테고리 \"{r.get('카테고리', '')}\", "
        f"구매항목 \"{r.get('구매항목', '')}\", 비고 \"{r.get('비고', '')}\""
        for r in corporate_rows]
    user_message = ("F3 키워드 대조 후보:\n" + "\n".join(cand_lines)
                    + "\n\n법인결제 행 목록 (F4 판단 대상):\n" + "\n".join(row_lines))
    data = json.loads(call_model(system_prompt, user_message, AI_SCHEMA))
    f3_valid = {d["transaction_id"] for d in data.get("f3", []) if d.get("is_match")}
    f4_bases = {d["transaction_id"]: (d.get("basis") or "").strip()
                for d in data.get("f4", []) if d.get("transaction_id")}
    return f3_valid, f4_bases


def _write_csv(fieldnames, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    csv_text = buf.getvalue()
    with open(RESULT_PATH, "w", encoding="utf-8-sig", newline="") as f:
        f.write(csv_text)
    return csv_text


def _report(status, counts=None, flags=None, message=""):
    return {
        "stage": "verify3",
        "status": status,
        "output": "verify3/result.csv" if status in ("ok", "partial") else "",
        "counts": counts or {"total": 0, "ok": 0, "flagged": 0},
        "flags": flags or [],
        "message": message,
    }


def run_verify3(input_text: str, call_model=None) -> dict:
    """verify3를 실행해 result.csv를 쓰고, 지휘에게 보낼 결과 보고를 돌려준다."""
    input_text = (input_text or "").strip()
    if not input_text:
        return {"csv": "", "report": _report("empty", message="검증 대상 없음")}

    reader = csv.DictReader(io.StringIO(input_text))
    fieldnames = reader.fieldnames or []
    rows = list(reader)
    missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        return {"csv": "", "report": _report("failed", message=f"입력에 {', '.join(missing)} 컬럼이 없습니다 — 거래 표 스키마를 확인하세요")}
    if not rows:
        csv_text = _write_csv(fieldnames + ["verify3_result", "verify3_reason"], [])
        return {"csv": csv_text, "report": _report("empty", message="검증 대상 없음")}

    keywords, f6_threshold = load_rules()
    corporate_rows = [r for r in rows if (r.get("결제구분") or "").strip() == "법인결제"]
    same_day_counts = {}
    for r in corporate_rows:
        key = ((r.get("날짜") or "").strip(), (r.get("결제처") or "").strip())
        same_day_counts[key] = same_day_counts.get(key, 0) + 1

    # 1차: 결정론 신호 수집 (F3는 후보 상태)
    detected = {}   # transaction_id -> (signals, undecidable)
    f3_candidates = []
    for r in corporate_rows:
        tid = (r.get("transaction_id") or "").strip()
        signals, undecidable = _detect_signals(r, same_day_counts, keywords, f6_threshold)
        detected[tid] = (signals, undecidable)
        for crit, _, reason in signals:
            if crit == "F3":
                kw = reason.rsplit("키워드: ", 1)[-1].rstrip(")")
                f3_candidates.append({"transaction_id": tid, "merchant": (r.get("결제처") or "").strip(),
                                      "keyword": kw})

    # 2차: AI 판단 (F3 문맥 확정 + F4) — call_model 없으면 F3 후보 유지·F4 생략
    ai_note = ""
    f3_valid = {c["transaction_id"] for c in f3_candidates}
    f4_bases = {}
    if call_model is not None and corporate_rows:
        try:
            f3_valid, f4_bases = _ask_ai(call_model, f3_candidates, corporate_rows, keywords)
        except (ValueError, KeyError, TypeError) as e:
            ai_note = f"AI 판단(F3 문맥·F4) 실패 — 키워드 대조 결과로 판정, F4 생략: {e}"

    # 3차: 스코어링 (fraud-rules.md 판정 규칙) — 강 1개 또는 약·중 합 2개 이상 → 확인 요청
    out_fieldnames = fieldnames + ["verify3_result", "verify3_reason"]
    out_rows, flags, ok_count = [], [], 0
    for i, row in enumerate(rows, start=1):
        out_row = dict(row)
        tid = (row.get("transaction_id") or "").strip()
        if (row.get("결제구분") or "").strip() != "법인결제":
            out_row["verify3_result"], out_row["verify3_reason"] = "통과", ""
            ok_count += 1
            out_rows.append(out_row)
            continue

        signals, undecidable = detected[tid]
        signals = [s for s in signals if not (s[0] == "F3" and tid not in f3_valid)]
        if tid in f4_bases:
            signals.append(("F4", "중", f"개인성 소비 의심 (근거: {f4_bases[tid]})"))

        if undecidable:
            out_row["verify3_result"] = "확인 요청"
            out_row["verify3_reason"] = "판단 불가 (형식 오류)"
            flags.append({"row": i, "type": "확인 요청", "reason": out_row["verify3_reason"]})
        elif any(s[1] == "강" for s in signals) or len(signals) >= 2:
            out_row["verify3_result"] = "확인 요청"
            out_row["verify3_reason"] = "; ".join(s[2] for s in signals)
            flags.append({"row": i, "type": "확인 요청", "reason": out_row["verify3_reason"]})
        elif signals:  # 약·중 신호 1개만 — 통과시키되 참고 메모
            out_row["verify3_result"] = "통과"
            out_row["verify3_reason"] = f"참고: {signals[0][2]}"
            ok_count += 1
        else:
            out_row["verify3_result"], out_row["verify3_reason"] = "통과", ""
            ok_count += 1
        out_rows.append(out_row)

    csv_text = _write_csv(out_fieldnames, out_rows)
    message = (f"법인결제 {len(corporate_rows)}건 검증 완료: 확인 요청 {len(flags)}건, "
               f"통과 {len(corporate_rows) - len(flags)}건. "
               f"개인결제 {len(rows) - len(corporate_rows)}건은 판정 없이 통과 처리.")
    if not corporate_rows:
        message = "법인결제 행 없음 — 판정 대상 없음"
    if ai_note:
        message = f"{message} / {ai_note}"
    report = _report(
        "ok" if not flags else "partial",
        counts={"total": len(rows), "ok": ok_count, "flagged": len(flags)},
        flags=flags,
        message=message,
    )
    return {"csv": csv_text, "report": report}


if __name__ == "__main__":
    input_path = os.path.join(REPO_ROOT, "refine", "result.csv")
    with open(input_path, encoding="utf-8-sig") as f:
        result = run_verify3(f.read())  # 단독 실행은 AI 없이 (F3 대조 그대로·F4 생략)
    r = result["report"]
    print(f"{r['status']}: total={r['counts']['total']} ok={r['counts']['ok']} flagged={r['counts']['flagged']}")
    print(r["message"])
