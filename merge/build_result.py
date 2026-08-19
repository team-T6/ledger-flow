"""merge 최종 산출물(result.xlsx, result.pdf)을 만드는 스크립트.

입력은 앞 단계의 실제 산출물 — 가공 거래 표 refine/result.csv(거래 표 스키마 v1)와
두 검증 결과 verify1/result.csv·verify2/result.csv(입력 행 + verify{n}_result·verify{n}_reason,
interface-spec.md 검증 1·2 행 확정). 행 대조 키는 transaction_id.
가공 산출물이 없으면 취합 불가라 failed로 보고하고, 검증 결과가 없으면 그 자리는
"미완" 표시 + 사유를 남기고 나머지를 정상 진행한다 (단계 문서 "못 할 때").

한글 PDF 폰트는 merge/fonts/NanumGothic-Regular.ttf(OFL 라이선스, 리포에 포함)를 쓴다 —
운영체제마다 다른 시스템 폰트 경로에 기대지 않기 위함이다.

사용법: python3 merge/build_result.py
"""

import csv
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

REPO_ROOT = os.path.dirname(BASE_DIR)
REFINE_CSV = os.path.join(REPO_ROOT, "refine", "result.csv")
VERIFY_CSVS = {
    "verify1": os.path.join(REPO_ROOT, "verify1", "result.csv"),
    "verify2": os.path.join(REPO_ROOT, "verify2", "result.csv"),
}

# 엑셀 회계장부 컬럼 — 거래 표 스키마 (확정 v1)와 동일 (interface-spec.md "산출물 양식")
LEDGER_COLUMNS = ["transaction_id", "날짜", "금액", "결제처", "카테고리", "비고",
                  "결제수단", "결제구분", "원거래통화", "원거래금액",
                  "source_type", "collect_status", "구매항목"]

# 데모(web/index.html) "장부 에디토리얼" 테마와 톤을 맞춘 팔레트
PAGE_BG = colors.HexColor("#FAF7F2")
INK = colors.HexColor("#1C1917")
MUTED = colors.HexColor("#7A7263")
ACCENT = colors.HexColor("#1E5B45")  # 제목·섹션 띠·표 헤더 — 구조적 요소
ACCENT_CONTRAST = colors.HexColor("#FAF7F2")
ACCENT_LIGHT = colors.HexColor("#EFE9DD")
CHART_COLOR = ACCENT  # 데모 미리보기 막대그래프도 accent 한 가지 색만 쓴다
FLAG_COLOR = colors.HexColor("#A63A2E")
FLAG_LIGHT = colors.HexColor("#F3E3DF")
GRID_COLOR = colors.HexColor("#E6DFD2")

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

def _parse_amount(value):
    """금액 문자열을 정수로 바꾼다 (콤마 허용). 해석 불가·빈 값은 None."""
    s = str(value or "").replace(",", "").strip()
    try:
        return int(s)
    except ValueError:
        return None


def load_transactions():
    """가공 거래 표(스키마 v1)에 두 검증 판정을 transaction_id로 붙여 돌려준다.

    반환: (rows, incomplete) — incomplete는 검증 결과가 통째로 없는 쪽의 "미완" 사유 목록.
    화면(screen.html)이 아직 쓰는 구 필드(지출/수익/결제자)는 금액·결제구분에서 파생해 채운다.
    """
    with open(REFINE_CSV, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    incomplete = []
    verdicts = {}  # stage -> {transaction_id: (result, reason)}
    for stage, path in VERIFY_CSVS.items():
        if not os.path.exists(path):
            incomplete.append(f"{stage} 결과 없음 ({os.path.relpath(path, REPO_ROOT)}) — 미완 처리")
            verdicts[stage] = None
            continue
        with open(path, encoding="utf-8-sig", newline="") as f:
            verdicts[stage] = {
                v["transaction_id"]: (v.get(f"{stage}_result", ""), v.get(f"{stage}_reason", ""))
                for v in csv.DictReader(f)
            }

    for row in rows:
        amount = _parse_amount(row.get("금액"))
        # 화면 호환용 파생 필드 — 스키마 v1의 금액(부호)·결제구분에서 계산
        row["지출"] = -amount if (amount is not None and amount < 0) else None
        row["수익"] = amount if (amount is not None and amount > 0) else None
        row["결제자"] = row.get("결제구분", "")
        for stage in VERIFY_CSVS:
            if verdicts[stage] is None:
                row[f"{stage}_result"], row[f"{stage}_reason"] = "미완", f"{stage} 결과 없음"
            else:
                result, reason = verdicts[stage].get(row.get("transaction_id"), ("미완", f"{stage} 결과에 해당 행 없음"))
                row[f"{stage}_result"], row[f"{stage}_reason"] = result, reason
    return rows, incomplete


def split_transactions(transactions):
    """verify1·verify2 중 하나라도 반려면 확인 필요 목록으로 뺀다 (재시도 없음).
    flagged_rows에는 지휘 보고의 flags[].row로 쓸 1-기준 행 번호(row)를 함께 담는다."""
    ok_rows, flagged_rows = [], []
    for idx, row in enumerate(transactions, start=1):
        if row["verify1_result"] == "반려":
            flagged_rows.append({**row, "row": idx, "reason": row.get("verify1_reason", "")})
        elif row["verify2_result"] == "반려":
            flagged_rows.append({**row, "row": idx, "reason": row.get("verify2_reason", "")})
        else:
            ok_rows.append(row)
    return ok_rows, flagged_rows


def build_envelope(total, ok_rows, flagged_rows, failed=False, message="", incomplete=()):
    """지휘(orchestrator)에게 돌려주는 단계 결과 보고 — interface-spec.md "단계 결과 보고" 규격.
    status 어휘 4개 고정: ok(정상) · empty(대상 없음) · partial(확인 필요 건 있음) · failed(단계 실패).
    output은 empty·failed일 때 빈 값(interface-spec.md "단계 결과 보고" 필드 설명).
    incomplete는 검증 결과 부재 등 자리 단위 "미완" 사유 목록 — flags에 type 미완으로 싣는다."""
    if failed:
        status, output = "failed", []
    elif total == 0:
        status, output = "empty", []
    else:
        status = "ok" if not (flagged_rows or incomplete) else "partial"
        output = OUTPUT_PATHS

    flags = [{"row": 0, "type": "미완", "reason": reason} for reason in incomplete]
    flags += [{"row": r["row"], "type": "확인 필요", "reason": r["reason"]} for r in flagged_rows]
    return {
        "stage": "merge",
        "status": status,
        "output": output,
        "counts": {"total": total, "ok": len(ok_rows), "flagged": len(flagged_rows)},
        "flags": flags,
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
        "title": ParagraphStyle("title", fontName=FONT_NAME, fontSize=24, leading=28, textColor=ACCENT),
        "meta": ParagraphStyle("meta", fontName=FONT_NAME, fontSize=10, leading=14, textColor=MUTED),
        "body": ParagraphStyle("body", fontName=FONT_NAME, fontSize=11, leading=16, textColor=INK),
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
        ("TEXTCOLOR", (0, 0), (-1, -1), ACCENT_CONTRAST),
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
        ("TEXTCOLOR", (0, 0), (-1, 0), ACCENT_CONTRAST),
        ("GRID", (0, 0), (-1, -1), 0.5, GRID_COLOR),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [PAGE_BG, ACCENT_LIGHT]),
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
    chart.bars[0].strokeColor = PAGE_BG
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


def _paint_page_background(canvas, doc):
    """데모 랜딩 페이지와 같은 크림색(PAGE_BG) 바탕지를 매 페이지에 깐다."""
    canvas.saveState()
    canvas.setFillColor(PAGE_BG)
    canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
    canvas.restoreState()


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

    story.append(_section_heading("4. 결제구분별 합계 (개인/법인)"))
    story.append(Spacer(1, 2 * mm))
    story.append(_table(
        ["결제구분", "금액"],
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
    # 해외결제 여부는 원거래통화가 채워져 있는지로 판별한다 (스키마 v1)
    foreign_rows = [r for r in ok_rows if (r.get("원거래통화") or "").strip()]
    if foreign_rows:
        story.append(_table(
            ["날짜", "결제처", "원거래", "원화 환산"],
            [[r["날짜"], r["결제처"], f"{r['원거래금액']} {r['원거래통화']}", f"{(r['지출'] or r['수익'] or 0):,}원"]
             for r in foreign_rows],
            col_widths=_col_widths(0.18, 0.34, 0.24, 0.24), num_cols=(3,),
        ))
    else:
        story.append(Paragraph("해당 없음 (해외결제 건 없음)", s["body"]))
    story.append(Spacer(1, 7 * mm))

    story.append(_section_heading("7. 확인 필요 항목"))
    story.append(Spacer(1, 2 * mm))
    if flagged_rows:
        table = _table(
            ["날짜", "결제처", "사유"],
            [[r["날짜"], r["결제처"], Paragraph(r["reason"], s["flag"])] for r in flagged_rows],
            col_widths=_col_widths(0.18, 0.26, 0.56),
        )
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), FLAG_COLOR),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [PAGE_BG, FLAG_LIGHT]),
        ]))
        story.append(table)
    else:
        story.append(Paragraph("확인 필요 항목 없음", s["body"]))

    doc.build(story, onFirstPage=_paint_page_background, onLaterPages=_paint_page_background)


def run():
    """xlsx·pdf를 실제로 만들고, 화면 미리보기·지휘 보고에 쓸 결과를 함께 돌려준다."""
    if not os.path.exists(REFINE_CSV):
        failed_envelope = build_envelope(
            0, [], [], failed=True,
            message=f"가공 산출물 없음: {os.path.relpath(REFINE_CSV, REPO_ROOT)} — 취합 불가",
        )
        return {"ok_rows": [], "flagged_rows": [], "summary": summarize([]), "envelope": failed_envelope}

    transactions, incomplete = load_transactions()
    total = len(transactions)
    if total == 0:
        empty_envelope = build_envelope(0, [], [], message="확인 대상 없음")
        return {"ok_rows": [], "flagged_rows": [], "summary": summarize([]), "envelope": empty_envelope}

    ok_rows, flagged_rows = split_transactions(transactions)
    summary = summarize(ok_rows)
    try:
        build_ledger(ok_rows, XLSX_PATH)
        build_report(ok_rows, flagged_rows, summary, PDF_PATH)
    except Exception as e:
        envelope = build_envelope(total, ok_rows, flagged_rows, failed=True, message=f"산출물 생성 실패: {e}")
        return {"ok_rows": ok_rows, "flagged_rows": flagged_rows, "summary": summary, "envelope": envelope}

    envelope = build_envelope(total, ok_rows, flagged_rows,
                              message=" / ".join(incomplete), incomplete=incomplete)
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
