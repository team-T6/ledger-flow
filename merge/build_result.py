"""merge 최종 산출물(result.xlsx, result.pdf)을 만드는 스크립트.

refine·verify1의 실제 산출물이 아직 없어, merge/input-sample.md·stub.md와 같은
예시 값을 그대로 써서 만든다 (collect/collect.py가 sample_data를 쓰는 것과 같은 방식).
refine/result.*·verify1/result.*가 실제로 생기면 TRANSACTIONS를 그쪽을 읽어
채우도록 바꾸면 된다.

한글 PDF 폰트는 merge/fonts/NanumGothic-Regular.ttf(OFL 라이선스, 리포에 포함)를 쓴다 —
운영체제마다 다른 시스템 폰트 경로에 기대지 않기 위함이다.

사용법: python3 merge/build_result.py
"""

import json
import os

from openpyxl import Workbook
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
XLSX_PATH = os.path.join(BASE_DIR, "result.xlsx")
PDF_PATH = os.path.join(BASE_DIR, "result.pdf")
FONT_PATH = os.path.join(BASE_DIR, "fonts", "NanumGothic-Regular.ttf")
FONT_NAME = "NanumGothic"

# 지휘에게 보내는 단계 결과 보고의 output 필드 — repo 기준 상대 경로 (interface-spec.md 예시와 동일 관례)
OUTPUT_PATHS = ["merge/result.xlsx", "merge/result.pdf"]

LEDGER_COLUMNS = ["날짜", "지출", "수익", "결제처", "카테고리", "비고", "결제수단", "결제자"]

ACCENT = colors.HexColor("#1F4E78")  # 제목·섹션 띠·표 헤더 — 구조적 요소
ACCENT_LIGHT = colors.HexColor("#DCE6F1")
CHART_COLOR = colors.HexColor("#1baf7a")  # 막대그래프 전용 — ACCENT(남색)와 겹치지 않는 색
FLAG_COLOR = colors.HexColor("#C00000")
FLAG_LIGHT = colors.HexColor("#FBE4E4")
GRID_COLOR = colors.HexColor("#BFBFBF")

# 표·그래프·섹션 배너가 전부 같은 좌우 여백을 쓰게 만드는 기준값 —
# 이 폭에서 벗어나는 요소가 없어야 열(오른쪽 끝)이 서로 어긋나지 않는다.
PAGE_MARGIN = 18 * mm
CONTENT_WIDTH = A4[0] - 2 * PAGE_MARGIN


def _col_widths(*fractions):
    """비율을 표 컬럼 폭(포인트)으로 바꾼다. 마지막 컬럼이 남은 폭을 다 가져가
    반올림 오차 없이 합이 정확히 CONTENT_WIDTH가 되게 한다."""
    widths = [CONTENT_WIDTH * f for f in fractions[:-1]]
    widths.append(CONTENT_WIDTH - sum(widths))
    return widths

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
    """verify1·verify2 중 하나라도 반려면 확인 필요 목록으로 뺀다 (재시도 없음).
    flagged_rows에는 지휘 보고의 flags[].row로 쓸 1-기준 행 번호(row)를 함께 담는다."""
    ok_rows, flagged_rows = [], []
    for idx, row in enumerate(transactions, start=1):
        if row["verify1"] == "반려":
            flagged_rows.append({**row, "row": idx, "reason": row.get("verify1_reason", "")})
        elif row["verify2"] == "반려":
            flagged_rows.append({**row, "row": idx, "reason": row["verify2_reason"]})
        else:
            ok_rows.append(row)
    return ok_rows, flagged_rows


def build_envelope(total, ok_rows, flagged_rows, failed=False, message=""):
    """지휘(orchestrator)에게 돌려주는 단계 결과 보고 — interface-spec.md "단계 결과 보고" 규격.
    status 어휘 4개 고정: ok(정상) · empty(대상 없음) · partial(확인 필요 건 있음) · failed(단계 실패).
    output은 empty·failed일 때 빈 값(interface-spec.md "단계 결과 보고" 필드 설명)."""
    if failed:
        status, output = "failed", []
    elif total == 0:
        status, output = "empty", []
    else:
        status = "ok" if not flagged_rows else "partial"
        output = OUTPUT_PATHS

    return {
        "stage": "merge",
        "status": status,
        "output": output,
        "counts": {"total": total, "ok": len(ok_rows), "flagged": len(flagged_rows)},
        "flags": [{"row": r["row"], "type": "확인 필요", "reason": r["reason"]} for r in flagged_rows],
        "message": message,
    }


def build_ledger(ok_rows, xlsx_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "회계장부"
    ws.append(LEDGER_COLUMNS)
    for row in ok_rows:
        ws.append([row.get(col) for col in LEDGER_COLUMNS])
    wb.save(xlsx_path)


def _styles():
    return {
        "title": ParagraphStyle("title", fontName=FONT_NAME, fontSize=22, leading=26, textColor=ACCENT),
        "meta": ParagraphStyle("meta", fontName=FONT_NAME, fontSize=10, leading=14, textColor=colors.grey),
        "body": ParagraphStyle("body", fontName=FONT_NAME, fontSize=11, leading=16),
        "flag": ParagraphStyle("flag", fontName=FONT_NAME, fontSize=10, leading=14, textColor=FLAG_COLOR),
    }


def _section_heading(text):
    """섹션 제목 띠 — 아래 표(_table)와 똑같이 colWidths=[CONTENT_WIDTH]인 표로 만든다.
    Paragraph의 backColor는 leftIndent/borderPadding에 따라 표와 다른 폭으로 그려질 수 있어,
    표와 좌우 끝을 정확히 맞추려고 같은 Table 메커니즘을 그대로 재사용한다."""
    table = Table([[text]], colWidths=[CONTENT_WIDTH])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
        ("FONTSIZE", (0, 0), (-1, -1), 13),
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return table


def _table(header, rows, col_widths, num_cols=()):
    """헤더 색·격자선이 있는 표 하나를 만든다. num_cols는 오른쪽 정렬할 열 인덱스."""
    data = [header] + rows
    style = [
        ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, GRID_COLOR),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ACCENT_LIGHT]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]
    for col in num_cols:
        style.append(("ALIGN", (col, 0), (col, -1), "RIGHT"))
    return Table(data, colWidths=col_widths, style=TableStyle(style))


def _category_chart(by_category):
    """카테고리별 지출 막대그래프 — 도형(Drawing)으로 그린다.

    Drawing 폭을 표·섹션 배너와 같은 CONTENT_WIDTH로 맞춰 오른쪽 끝이 서로
    어긋나지 않게 하고, 막대 사이 간격(barSpacing)을 고정값으로 줘서 항상
    일정하게 유지한다.
    """
    LEFT_AXIS_MARGIN = 16 * mm   # 금액 축 눈금 숫자가 들어갈 자리
    BOTTOM_AXIS_MARGIN = 14 * mm  # 카테고리 이름이 들어갈 자리
    RIGHT_PAD = 4 * mm
    TOP_PAD = 6 * mm
    height = 60 * mm

    drawing = Drawing(CONTENT_WIDTH, height)
    chart = VerticalBarChart()
    chart.x = LEFT_AXIS_MARGIN
    chart.y = BOTTOM_AXIS_MARGIN
    chart.width = CONTENT_WIDTH - LEFT_AXIS_MARGIN - RIGHT_PAD
    chart.height = height - BOTTOM_AXIS_MARGIN - TOP_PAD

    chart.data = [[amount for _, amount in by_category]]
    chart.categoryAxis.categoryNames = [name for name, _ in by_category]
    chart.categoryAxis.labels.fontName = FONT_NAME
    chart.categoryAxis.labels.fontSize = 8

    chart.valueAxis.labels.fontName = FONT_NAME
    chart.valueAxis.labels.fontSize = 8
    chart.valueAxis.valueMin = 0
    chart.valueAxis.labelTextFormat = lambda v: f"{int(v):,}"
    chart.valueAxis.visibleGrid = 1
    chart.valueAxis.gridStrokeColor = GRID_COLOR
    chart.valueAxis.gridStrokeWidth = 0.4

    # 막대 사이 간격을 고정값으로 둬서 카테고리 수와 무관하게 항상 일정하다
    chart.groupSpacing = 8
    chart.barWidth = 10 * mm
    chart.bars[0].fillColor = CHART_COLOR
    chart.bars[0].strokeColor = colors.white
    chart.bars[0].strokeWidth = 0.4

    drawing.add(chart)
    return drawing


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
    s = _styles()

    total_expense = summary["total_expense"]
    total_income = summary["total_income"]
    net = total_income - total_expense

    doc = SimpleDocTemplate(
        pdf_path, pagesize=A4,
        topMargin=PAGE_MARGIN, bottomMargin=PAGE_MARGIN, leftMargin=PAGE_MARGIN, rightMargin=PAGE_MARGIN,
    )
    story = []

    story.append(Paragraph("결산 리포트", s["title"]))
    story.append(Paragraph("통합(merge) 담당 — 엑셀 회계장부와 한 묶음으로 전달되는 리포트", s["meta"]))
    story.append(Spacer(1, 8 * mm))

    story.append(_section_heading("1. 결산 개요"))
    story.append(Spacer(1, 2 * mm))
    story.append(_table(
        ["총지출", "총수익", "순액"],
        [[f"{total_expense:,}원", f"{total_income:,}원", f"{net:,}원"]],
        col_widths=_col_widths(1 / 3, 1 / 3, 1 / 3),
    ))
    story.append(Spacer(1, 7 * mm))

    story.append(_section_heading("2. 카테고리별 지출"))
    story.append(Spacer(1, 2 * mm))
    if summary["by_category"]:
        rows = [
            [category, f"{amount:,}원", f"{(amount / total_expense * 100 if total_expense else 0):.1f}%"]
            for category, amount in summary["by_category"]
        ]
        story.append(_table(["카테고리", "금액", "비중"], rows, col_widths=_col_widths(0.40, 0.30, 0.30), num_cols=(1, 2)))
        story.append(Spacer(1, 4 * mm))
        story.append(_category_chart(summary["by_category"]))
    else:
        story.append(Paragraph("집계할 지출 없음", s["body"]))
    story.append(Spacer(1, 7 * mm))

    story.append(_section_heading("3. 결제수단별 합계"))
    story.append(Spacer(1, 2 * mm))
    story.append(_table(
        ["결제수단", "금액"],
        [[method, f"{amount:,}원"] for method, amount in summary["by_method"]],
        col_widths=_col_widths(0.5, 0.5), num_cols=(1,),
    ))
    story.append(Spacer(1, 7 * mm))

    story.append(_section_heading("4. 결제자별 합계"))
    story.append(Spacer(1, 2 * mm))
    story.append(_table(
        ["결제자", "금액"],
        [[payer, f"{amount:,}원"] for payer, amount in summary["by_payer"]],
        col_widths=_col_widths(0.5, 0.5), num_cols=(1,),
    ))
    story.append(Spacer(1, 7 * mm))

    story.append(_section_heading("5. 주요 지출 Top 10"))
    story.append(Spacer(1, 2 * mm))
    story.append(_table(
        ["날짜", "결제처", "금액"],
        [[r["날짜"], r["결제처"], f"{r['지출']:,}원"] for r in summary["top_spenders"]],
        col_widths=_col_widths(0.23, 0.54, 0.23), num_cols=(2,),
    ))
    story.append(Spacer(1, 7 * mm))

    story.append(_section_heading("6. 해외결제 명세"))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph("해당 없음 (해외결제 건 없음)", s["body"]))
    story.append(Spacer(1, 7 * mm))

    story.append(_section_heading("7. 확인 필요 항목"))
    story.append(Spacer(1, 2 * mm))
    if flagged_rows:
        table = _table(
            ["날짜", "결제처", "사유"],
            [[r["날짜"], r["결제처"], r["reason"]] for r in flagged_rows],
            col_widths=_col_widths(0.18, 0.26, 0.56),
        )
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), FLAG_COLOR),
            ("TEXTCOLOR", (2, 1), (2, -1), FLAG_COLOR),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, FLAG_LIGHT]),
        ]))
        story.append(table)
    else:
        story.append(Paragraph("확인 필요 항목 없음", s["body"]))

    doc.build(story)


def run():
    """xlsx·pdf를 실제로 만들고, 화면 미리보기·지휘 보고에 쓸 결과를 함께 돌려준다."""
    total = len(TRANSACTIONS)
    if total == 0:
        empty_envelope = build_envelope(0, [], [], message="확인 대상 없음")
        return {"ok_rows": [], "flagged_rows": [], "summary": summarize([]), "envelope": empty_envelope}

    ok_rows, flagged_rows = split_transactions(TRANSACTIONS)
    summary = summarize(ok_rows)
    try:
        build_ledger(ok_rows, XLSX_PATH)
        build_report(ok_rows, flagged_rows, summary, PDF_PATH)
    except Exception as e:
        envelope = build_envelope(total, ok_rows, flagged_rows, failed=True, message=f"산출물 생성 실패: {e}")
        return {"ok_rows": ok_rows, "flagged_rows": flagged_rows, "summary": summary, "envelope": envelope}

    envelope = build_envelope(total, ok_rows, flagged_rows)
    return {"ok_rows": ok_rows, "flagged_rows": flagged_rows, "summary": summary, "envelope": envelope}


def main():
    result = run()
    envelope = result["envelope"]
    if envelope["status"] == "empty":
        print("확인 대상 없음")
        return

    print(f"엑셀 회계장부: {XLSX_PATH}")
    print(f"PDF 결산 리포트: {PDF_PATH}")
    print(f"장부 반영 {len(result['ok_rows'])}건 · 확인 필요 {len(result['flagged_rows'])}건")
    print("지휘에게 보내는 단계 결과 보고:")
    print(json.dumps(envelope, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
