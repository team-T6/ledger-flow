# -*- coding: utf-8 -*-
"""가공(refine) 단계 실행 스크립트.

입력: collect/result.csv (없으면 refine/input-sample.md의 ```csv 블록 — 견본 실행)
출력: refine/result.csv — 거래 표 스키마 v1 그대로, 결제처 정규화 + 카테고리 채움
      (컬럼 추가·삭제 없음 — interface-spec.md "거래 표 스키마", 단계 문서 .claude/agents/refine.md)

이 스크립트는 단계 문서의 "AI 판단 / 일반 코드 구분"에서 **일반 코드** 몫만 맡는다:
CSV 읽기/쓰기·스키마 유지·이미 알려진 가맹점/PG 매핑 규칙 적용. 매핑에 없는
신규·애매 건의 추론은 AI 판단 몫이라 임의 배정하지 않고 '확인 필요'로 남긴다.

사용법: python3 refine/refine.py [입력경로] [출력경로]
"""

import csv
import io
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)
DEFAULT_INPUT = os.path.join(REPO_ROOT, "collect", "result.csv")
SAMPLE_INPUT = os.path.join(BASE_DIR, "input-sample.md")
DEFAULT_OUTPUT = os.path.join(BASE_DIR, "result.csv")

CONFIRM_NEEDED = "확인 필요"

# "PG사명/가맹점" 꼴로 붙는 알려진 PG사 표기 — 뒤의 실가맹점명으로 정규화한다
PG_PREFIXES = ("NHN KCP", "KG이니시스", "토스페이먼츠", "나이스페이먼츠", "다날")

# PG/플랫폼 단독 표기 — 실가맹점을 특정할 수 없어 결제처 원문 유지 + 확인 필요
PG_ONLY = ("네이버페이", "카카오페이", "페이코")

# 업종이 하나로 특정되는 가맹점 → docs/categories.md 확정 체계의 카테고리
MERCHANT_CATEGORY = {
    "교촌치킨": "식대",
    "배달의민족": "식대",
    "김밥천국": "식대",
    "스타벅스 강남점": "식대",      # 커피·디저트는 식대 포함 범위
    "파리바게뜨": "식대",
    "GS25 학동역점": "식대",        # 편의점 간식은 식대 포함 범위
    "카카오T": "교통비",
    "SK주유소": "교통비",
    "다이소": "기타물품",
    "올리브영": "기타물품",
    "하이마트 강남점": "기타물품",  # 가전 → 기타물품(비품)
    "넷플릭스": "구독료",
    "CGV": "기타",                  # 영화관람 — 전용 카테고리 없음
    "무신사": "기타",               # 의류 — 전용 카테고리 없음
}

# 종합몰·대형마트 — 결제처만으론 품목을 알 수 없어 구매항목을 근거로 분류한다
GENERAL_MERCHANTS = ("쿠팡", "이마트 성수점")

# 구매항목 키워드 → 카테고리 (앞에서부터 첫 매칭 채택)
ITEM_KEYWORDS = (
    ("식대", ("김밥", "커피", "치킨", "콜라", "떡볶이", "순대", "고기", "채소",
              "과일", "라면", "빵", "도시락", "음료", "팝콘")),
    ("기타물품", ("이어폰", "마우스", "정리함", "문구", "냉장고", "가전", "세제",
                  "물티슈", "종이컵", "수납", "샴푸", "화장품")),
    ("교육비", ("책", "서적")),
)


def normalize_merchant(merchant):
    """결제처 정규화. (정규화된 결제처, 실가맹점 식별 여부)를 돌려준다."""
    merchant = (merchant or "").strip()
    for pg in PG_PREFIXES:
        if merchant.startswith(pg + "/"):
            real = merchant[len(pg) + 1:].strip()
            if real:
                return real, True
            return merchant, False
    if merchant in PG_ONLY:
        return merchant, False
    return merchant, True


def classify(merchant, items, memo):
    """결제처·구매항목·비고를 근거로 카테고리를 정한다. 매핑 밖이면 확인 필요."""
    if merchant in MERCHANT_CATEGORY:
        return MERCHANT_CATEGORY[merchant]
    text = " ".join(v for v in (items, memo) if v)
    if merchant in GENERAL_MERCHANTS or text:
        for category, keywords in ITEM_KEYWORDS:
            if any(k in text for k in keywords):
                return category
    return CONFIRM_NEEDED


def load_csv_text(input_path):
    if input_path.endswith(".md"):
        with io.open(input_path, "r", encoding="utf-8") as f:
            match = re.search(r"```csv\n(.*?)```", f.read(), re.DOTALL)
        if not match:
            raise ValueError(f"csv 블록을 찾을 수 없음: {input_path}")
        return match.group(1)
    with io.open(input_path, "r", encoding="utf-8-sig") as f:
        return f.read()


def main():
    input_path = sys.argv[1] if len(sys.argv) > 1 else (
        DEFAULT_INPUT if os.path.exists(DEFAULT_INPUT) else SAMPLE_INPUT)
    output_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUTPUT

    if not os.path.exists(input_path):
        raise FileNotFoundError(f"입력 파일 없음: {input_path}")

    reader = csv.DictReader(io.StringIO(load_csv_text(input_path)))
    rows = list(reader)
    fieldnames = reader.fieldnames or []
    for required in ("결제처", "카테고리", "구매항목", "비고"):
        if required not in fieldnames:
            raise ValueError(f"거래 표 스키마 컬럼 누락: {required} (입력: {input_path})")

    flagged = 0
    for row in rows:
        merchant, resolved = normalize_merchant(row["결제처"])
        if resolved:
            row["결제처"] = merchant
            category = classify(merchant, row.get("구매항목", ""), row.get("비고", ""))
        else:
            category = CONFIRM_NEEDED  # 결제처는 원문 유지
        row["카테고리"] = category
        if category == CONFIRM_NEEDED:
            flagged += 1

    with io.open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    total = len(rows)
    if total == 0:
        print(f"empty: 처리 대상 없음 (입력 거래 표가 비어 있음) -> {output_path}")
        return
    print(f"done: total={total} ok={total - flagged} flagged={flagged} -> {output_path}")


if __name__ == "__main__":
    main()
