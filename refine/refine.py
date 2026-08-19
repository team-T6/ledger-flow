# -*- coding: utf-8 -*-
"""
가공(refine) 단계 실행 스크립트.

입력: refine/input-sample.md (수집 산출물 CSV를 그대로 담은 파일)
출력: refine/result.md (거래 표 스키마 유지 + 결제처 뒤에 '카테고리' 열 추가)

동작(설계: docs/agents/refine.md, 지시서: .claude/agents/refine.md):
1) 결제처의 PG사·플랫폼 표기 뒤 실제 가맹점을 판단해 정규화한다.
   - PG 표기라 실제 가맹점을 특정할 수 없으면 결제처는 원문 그대로 두고 카테고리는 '확인 필요'.
2) 정규화된 가맹점명 + 거래항목 + 비고를 근거로 docs/categories.md 체계의 카테고리를 채운다.
3) 식별·분류 불가 건은 임의 배정하지 않고 '확인 필요'로 남기고 버리지 않는다.
"""

import csv
import io
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_PATH = os.path.join(BASE_DIR, "input-sample.md")
OUTPUT_PATH = os.path.join(BASE_DIR, "result.md")

CONFIRM_NEEDED = "확인 필요"

# 가맹점(정규화된 결제처) -> categories.md 카테고리
# 근거는 결제처 + 거래항목 + 비고. 모든 입력 행은 지출(수익=0)이라 지출 카테고리만 사용.
MERCHANT_CATEGORY = {
    # 식비: 식사·배달
    "교촌치킨": "식비",
    "배달의민족": "식비",       # 배달 플랫폼이나 거래항목이 모두 음식 -> 식비(배달)
    "김밥천국": "식비",
    # 카페·간식: 커피·디저트·편의점 간식
    "스타벅스 강남점": "카페·간식",
    "파리바게뜨": "카페·간식",   # 베이커리(빵·케이크·샌드위치·커피)
    "GS25 학동역점": "카페·간식",  # 편의점 -> categories.md '편의점 간식'
    # 여비교통: 대중교통·택시·주유·주차
    "카카오T": "여비교통",        # 거래항목이 택시로 특정됨 -> 여비교통(택시)
    "SK주유소": "여비교통",       # 주유 -> 여비교통(주유)
    # 소모품·비품: 사무용품·가전·생활용품
    "쿠팡": "소모품·비품",        # 생수·무선마우스·세제·종이컵·물티슈 등 생활용품·가전
    "이마트 성수점": "소모품·비품",  # 장보기·생필품·식료품·정육/채소 등 생활 소모품
    "다이소": "소모품·비품",      # 생활용품·주방용품·문구류
    "올리브영": "소모품·비품",    # 스킨/로션·샴푸/바디워시 등 생활용품
    "하이마트 강남점": "소모품·비품",  # 냉장고(가전)
    # 구독·소프트웨어: SaaS·멤버십·콘텐츠 구독
    "넷플릭스": "구독·소프트웨어",
    # 기타 지출: 위 어느 카테고리에도 속하지 않는 지출(잡비)
    "CGV": "기타 지출",           # 영화관람 - 전용 카테고리 없음 -> 기타 지출
    "무신사": "기타 지출",        # 의류(티셔츠·니트/바지·운동화) - 전용 카테고리 없음
    # PG/플랫폼 표기라 실제 가맹점·품목 특정 불가 -> 확인 필요
    "네이버페이": CONFIRM_NEEDED,
}

OUTPUT_COLUMNS = [
    "transaction_id", "날짜", "지출", "수익", "결제처", "카테고리",
    "거래항목", "비고", "결제수단", "결제자", "source_type", "collect_status",
]


def load_rows(path):
    with io.open(path, "r", encoding="utf-8") as f:
        text = f.read()
    reader = csv.DictReader(io.StringIO(text))
    return list(reader), reader.fieldnames


def classify(merchant):
    """결제처(정규화)를 카테고리로. 매핑에 없으면 확인 필요."""
    return MERCHANT_CATEGORY.get(merchant, CONFIRM_NEEDED)


def md_escape(value):
    return (value or "").replace("|", "\\|").replace("\n", " ")


def main():
    rows, _ = load_rows(INPUT_PATH)

    if not rows:
        with io.open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            f.write("# 가공(refine) 결과\n\n처리 대상 없음 (입력 거래 표가 비어 있음).\n")
        print("empty: 처리 대상 없음")
        return

    out_rows = []
    flagged = 0
    for r in rows:
        merchant = r["결제처"]
        category = classify(merchant)
        if category == CONFIRM_NEEDED:
            flagged += 1
        out = dict(r)
        out["카테고리"] = category
        out_rows.append(out)

    total = len(out_rows)
    ok = total - flagged

    lines = []
    lines.append("# 가공(refine) 결과")
    lines.append("")
    lines.append("> 수집 산출물(거래 표 CSV)에 결제처 정규화와 카테고리를 채운 결과.")
    lines.append("> 스키마: 거래 표 스키마 유지 + 결제처 뒤에 '카테고리' 열 추가.")
    lines.append("> 근거: docs/interface-spec.md(거래 표 스키마) · docs/categories.md(분류 체계).")
    lines.append("")
    lines.append("- 총 %d건 · 정상 분류 %d건 · 확인 필요 %d건" % (total, ok, flagged))
    lines.append("")
    lines.append("| " + " | ".join(OUTPUT_COLUMNS) + " |")
    lines.append("|" + "|".join(["---"] * len(OUTPUT_COLUMNS)) + "|")
    for out in out_rows:
        cells = [md_escape(out.get(col, "")) for col in OUTPUT_COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    with io.open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("done: total=%d ok=%d flagged=%d -> %s" % (total, ok, flagged, OUTPUT_PATH))


if __name__ == "__main__":
    main()