"""merge 최종 산출물(result.xlsx, result.pdf)을 만드는 스크립트.

refine·verify1의 실제 산출물이 아직 없어, merge/input-sample.md·stub.md와 같은
예시 값을 그대로 써서 만든다 (collect/collect.py가 sample_data를 쓰는 것과 같은 방식).
refine/result.*·verify1/result.*가 실제로 생기면 TRANSACTIONS를 그쪽을 읽어
채우도록 바꾸면 된다.

한글 PDF 폰트는 merge/fonts/NanumGothic-Regular.ttf(OFL 라이선스, 리포에 포함)를 쓴다 —
운영체제마다 다른 시스템 폰트 경로에 기대지 않기 위함이다.

사용법: python3 merge/build_result.py
"""

import os

from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
XLSX_PATH = os.path.join(BASE_DIR, "result.xlsx")
PDF_PATH = os.path.join(BASE_DIR, "result.pdf")
FONT_PATH = os.path.join(BASE_DIR, "fonts", "NanumGothic-Regular.ttf")
FONT_NAME = "NanumGothic"

LEDGER_COLUMNS = ["날짜", "지출", "수익", "결제처", "카테고리", "비고", "결제수단", "결제자"]

# merge/input-sample.md와 동일한 예시 입력 — 가공 거래 표 + 검증1·검증2 결과를 합친 것
TRANSACTIONS = [
    {"날짜": "2026-02-09", "지출": 23787, "수익": None, "결제처": "교촌치킨",
     "카테고리": "식비", "비고": "포인트 적립 476원", "결제수단": "하나카드", "결제자": "개인카드",
     "verify1": "통과", "verify2": "통과", "verify2_reason": ""},
    {"날짜": "2026-02-13", "지출": 85589, "수익": None, "결제처": "이마트 성수점",
     "카테고리": "소모품·비품", "비고": "", "결제수단": "하나카드", "결제자": "개인카드",
     "verify1": "통과", "verify2": "통과", "verify2_reason": ""},
    {"날짜": "2026-02-17", "지출": 17000, "수익": None, "결제처": "넷플릭스",
     "카테고리": "구독·소프트웨어", "비고": "", "결제수단": "하나카드", "결제자": "개인카드",
     "verify1": "통과", "verify2": "통과", "verify2_reason": ""},
    {"날짜": "2026-03-01", "지출": 29432, "수익": None, "결제처": "올리브영",
     "카테고리": "소모품·비품", "비고": "포인트 적립 589원", "결제수단": "하나카드", "결제자": "개인카드",
     "verify1": "통과", "verify2": "반려", "verify2_reason": "대상 월 아님 (날짜: 2026-03-01)"},
    {"날짜": "2026-02-15", "지출": 22805, "수익": 22805, "결제처": "교촌치킨",
     "카테고리": "식비", "비고": "", "결제수단": "하나카드", "결제자": "개인카드",
     "verify1": "통과", "verify2": "반려", "verify2_reason": "지출·수익 동시 기입 (지출 22,805 / 수익 22,805)"},
]


def split_transactions(transactions):
    """verify1·verify2 중 하나라도 반려면 확인 필요 목록으로 뺀다 (재시도 없음)."""
    ok_rows, flagged_rows = [], []
    for row in transactions:
        if row["verify1"] == "반려":
            flagged_rows.append({**row, "reason": row.get("verify1_reason", "")})
        elif row["verify2"] == "반려":
            flagged_rows.append({**row, "reason": row["verify2_reason"]})
        else:
            ok_rows.append(row)
    return ok_rows, flagged_rows


def build_ledger(ok_rows, xlsx_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "회계장부"
    ws.append(LEDGER_COLUMNS)
    for row in ok_rows:
        ws.append([row.get(col) for col in LEDGER_COLUMNS])
    wb.save(xlsx_path)


def _draw_heading(c, text, y):
    c.setFont(FONT_NAME, 14)
    c.drawString(20 * mm, y, text)
    return y - 9 * mm


def _draw_line(c, text, y, size=11):
    c.setFont(FONT_NAME, size)
    c.drawString(24 * mm, y, text)
    return y - 7 * mm


def summarize(ok_rows):
    """PDF 작성과 화면 미리보기가 함께 쓰는 집계 — 카테고리·결제수단·결제자별 합계, Top 지출."""
    total_expense = sum(r["지출"] or 0 for r in ok_rows)
    total_income = sum(r["수익"] or 0 for r in ok_rows)

    by_category = {}
    for r in ok_rows:
        by_category[r["카테고리"]] = by_category.get(r["카테고리"], 0) + (r["지출"] or 0)

    by_method = {}
    for r in ok_rows:
        by_method[r["결제수단"]] = by_method.get(r["결제수단"], 0) + (r["지출"] or 0)

    by_payer = {}
    for r in ok_rows:
        by_payer[r["결제자"]] = by_payer.get(r["결제자"], 0) + (r["지출"] or 0)

    top_spenders = sorted(ok_rows, key=lambda r: r["지출"] or 0, reverse=True)[:10]

    return {
        "total_expense": total_expense,
        "total_income": total_income,
        "by_category": sorted(by_category.items(), key=lambda kv: kv[1], reverse=True),
        "by_method": list(by_method.items()),
        "by_payer": list(by_payer.items()),
        "top_spenders": top_spenders,
    }


def build_report(ok_rows, flagged_rows, summary, pdf_path):
    pdfmetrics.registerFont(TTFont(FONT_NAME, FONT_PATH))

    total_expense = summary["total_expense"]
    total_income = summary["total_income"]

    c = canvas.Canvas(pdf_path, pagesize=A4)
    y = 280 * mm

    y = _draw_heading(c, "1. 결산 개요", y)
    y = _draw_line(c, f"총지출 {total_expense:,}원 · 총수익 {total_income:,}원 · 순액 {total_income - total_expense:,}원", y)
    y -= 4 * mm

    y = _draw_heading(c, "2. 카테고리별 지출", y)
    for category, amount in summary["by_category"]:
        share = amount / total_expense * 100 if total_expense else 0
        y = _draw_line(c, f"{category}: {amount:,}원 ({share:.1f}%)", y)
    y -= 4 * mm

    y = _draw_heading(c, "3. 결제수단별 합계", y)
    for method, amount in summary["by_method"]:
        y = _draw_line(c, f"{method}: {amount:,}원", y)
    y -= 4 * mm

    y = _draw_heading(c, "4. 결제자별 합계", y)
    for payer, amount in summary["by_payer"]:
        y = _draw_line(c, f"{payer}: {amount:,}원", y)
    y -= 4 * mm

    y = _draw_heading(c, "5. 주요 지출 Top 10", y)
    for r in summary["top_spenders"]:
        y = _draw_line(c, f"{r['날짜']} {r['결제처']}: {r['지출']:,}원", y)
    y -= 4 * mm

    y = _draw_heading(c, "6. 해외결제 명세", y)
    y = _draw_line(c, "해당 없음 (해외결제 건 없음)", y)
    y -= 4 * mm

    y = _draw_heading(c, "7. 확인 필요 항목", y)
    if flagged_rows:
        for r in flagged_rows:
            y = _draw_line(c, f"{r['날짜']} {r['결제처']}: {r['reason']}", y)
    else:
        y = _draw_line(c, "확인 필요 항목 없음", y)

    c.save()


def run():
    """xlsx·pdf를 실제로 만들고, 화면 미리보기에 쓸 결과를 함께 돌려준다."""
    ok_rows, flagged_rows = split_transactions(TRANSACTIONS)
    summary = summarize(ok_rows)
    build_ledger(ok_rows, XLSX_PATH)
    build_report(ok_rows, flagged_rows, summary, PDF_PATH)
    return {"ok_rows": ok_rows, "flagged_rows": flagged_rows, "summary": summary}


def main():
    if not TRANSACTIONS:
        print("확인 대상 없음")
        return

    result = run()
    print(f"엑셀 회계장부: {XLSX_PATH}")
    print(f"PDF 결산 리포트: {PDF_PATH}")
    print(f"장부 반영 {len(result['ok_rows'])}건 · 확인 필요 {len(result['flagged_rows'])}건")


if __name__ == "__main__":
    main()
