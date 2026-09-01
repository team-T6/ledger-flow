"""기간·금액 검증(verify2) 실행 코드.

단계 문서(.claude/agents/verify2.md)의 세 판단 기준 — 대상 월·금액 단일 기입·해외 환산
상식 범위 — 은 전부 결정론적 대조라 AI 호출 없이 일반 코드로 판정한다 (문서 "AI 판단 /
일반 코드 구분" 확정). 결과는 verify2/result.csv로 쓰고, 지휘에게 보낼 결과 보고(JSON)를
함께 돌려준다.

판단 불가 행(날짜 형식 깨짐·범위 미정 통화)은 문서 "오류·예외 처리"대로 임의 통과
처리하지 않고 반려 값에 "판단 불가" 사유를 남겨 envelope에는 "확인 필요"로 싣는다.
대상 월이 아닌 행(날짜는 읽혔으나 다른 달)은 `대상외`로 표시해 지휘가 확인 없이
제외한다 (2026-09-01 확정 로그). 행 결과 어휘: 통과 / 반려 / 대상외.

사용법: python3 verify2/verify2.py <YYYY-MM>  (입력: refine/result.csv)
"""

import csv
import io
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULT_PATH = os.path.join(BASE_DIR, "result.csv")

# 단계 문서 "판단 기준 3" 통화별 상식 범위 (원/1단위) — 초기값, 운영하며 조정
FX_SANITY_RANGES = {
    "USD": (1250, 1550),
    "EUR": (1350, 1700),
    "JPY": (8, 11),
    "CNY": (170, 215),
    "GBP": (1550, 1950),
}

REQUIRED_COLUMNS = ("transaction_id", "날짜", "금액", "결제수단")


def _check_row(row, month):
    """한 행에 세 기준을 순서대로 적용해 (반려 사유 목록, 판단 불가 여부, 대상외 여부)를 돌려준다.

    대상외 = 날짜가 읽혔고 대상 월이 아닌 경우 — 수집 범위 밖 데이터라 지휘가 확인 없이 제외한다
    (verify2.md 판단 기준 1). 날짜 형식 오류는 대상외가 아니라 반려(판단 불가).
    """
    reasons = []
    undecidable = False
    out_of_scope = False

    # 1. 대상 월
    date = (row.get("날짜") or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        if not date.startswith(month):
            reasons.append(f"대상 월 아님 (날짜: {date})")
            out_of_scope = True
    else:
        reasons.append("판단 불가 (형식 오류)")
        undecidable = True

    # 2. 금액 단일 기입
    amount_raw = (row.get("금액") or "").strip()
    amount = None
    if not amount_raw:
        reasons.append("금액 미기입")
    else:
        try:
            amount = int(amount_raw)
        except ValueError:
            reasons.append("금액 형식 불명확")

    # 3. 해외 환산 상식 범위 — 원거래통화가 채워진 행만 (국내 결제는 건너뜀)
    currency = (row.get("원거래통화") or "").strip()
    if currency:
        orig_raw = (row.get("원거래금액") or "").strip()
        try:
            orig_amount = float(orig_raw) if orig_raw else 0.0
        except ValueError:
            orig_amount = 0.0
        if orig_amount == 0.0:
            reasons.append("원거래금액 미기입 (해외결제인데 원거래 금액 없음)")
        elif currency not in FX_SANITY_RANGES:
            reasons.append(f"판단 불가 (범위 미정 통화: {currency})")
            undecidable = True
        elif amount is not None:
            rate = abs(amount) / orig_amount
            low, high = FX_SANITY_RANGES[currency]
            if not (low <= rate <= high):
                reasons.append(
                    f"환산 환율 이상 (약 {rate:,.0f}원/{currency} — 상식 범위 {low:,} - {high:,} 밖)")

    return reasons, undecidable, out_of_scope


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
        "stage": "verify2",
        "status": status,
        "output": "verify2/result.csv" if status in ("ok", "partial") else "",
        "counts": counts or {"total": 0, "ok": 0, "flagged": 0},
        "flags": flags or [],
        "message": message,
    }


def run_verify2(input_text: str, month: str) -> dict:
    """verify2를 실행해 result.csv를 쓰고, 지휘에게 보낼 결과 보고를 돌려준다."""
    input_text = (input_text or "").strip()
    if not input_text:
        return {"csv": "", "report": _report("empty", message="검증 대상 없음")}
    if not re.match(r"^\d{4}-\d{2}$", month or ""):
        # 단계 문서 "알려진 블로커" — 대상 월이 없으면 추정하지 않고 실패 보고
        return {"csv": "", "report": _report("failed", message=f"대상 월 파라미터 없음 또는 형식 오류: {month!r}")}

    reader = csv.DictReader(io.StringIO(input_text))
    fieldnames = reader.fieldnames or []
    rows = list(reader)
    missing = [c for c in REQUIRED_COLUMNS if c not in fieldnames]
    if missing:
        return {"csv": "", "report": _report("failed", message=f"입력에 {', '.join(missing)} 컬럼이 없습니다 — 거래 표 스키마를 확인하세요")}
    if not rows:
        csv_text = _write_csv(fieldnames + ["verify2_result", "verify2_reason"], [])
        return {"csv": csv_text, "report": _report("empty", message="검증 대상 없음")}

    out_fieldnames = fieldnames + ["verify2_result", "verify2_reason"]
    out_rows, flags, ok_count = [], [], 0
    for i, row in enumerate(rows, start=1):
        reasons, undecidable, out_of_scope = _check_row(row, month)
        out_row = dict(row)
        if out_of_scope:
            # 대상 월 아님 — 지휘가 확인 없이 제외 (verify2.md 판단 기준 1)
            out_row["verify2_result"] = "대상외"
            out_row["verify2_reason"] = "; ".join(reasons)
            flags.append({"row": i, "type": "대상외", "reason": out_row["verify2_reason"]})
        elif reasons:
            out_row["verify2_result"] = "반려"
            out_row["verify2_reason"] = "; ".join(reasons)
            flags.append({"row": i, "type": "확인 필요" if undecidable else "반려",
                          "reason": out_row["verify2_reason"]})
        else:
            out_row["verify2_result"] = "통과"
            out_row["verify2_reason"] = ""
            ok_count += 1
        out_rows.append(out_row)

    csv_text = _write_csv(out_fieldnames, out_rows)
    report = _report(
        "ok" if not flags else "partial",
        counts={"total": len(rows), "ok": ok_count, "flagged": len(flags)},
        flags=flags,
    )
    return {"csv": csv_text, "report": report}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python3 verify2/verify2.py <대상 월 YYYY-MM>")
        sys.exit(1)
    input_path = os.path.join(BASE_DIR, "..", "refine", "result.csv")
    with open(input_path, encoding="utf-8-sig") as f:
        result = run_verify2(f.read(), sys.argv[1])
    r = result["report"]
    print(f"{r['status']}: total={r['counts']['total']} ok={r['counts']['ok']} flagged={r['counts']['flagged']}")
