"""collect 단계 정규화 스크립트.

sample_data/hana_card/*.csv(카드사 원본, 월별 1파일)를 읽어 표준 거래 표로 정규화하고,
collect/result.csv 하나로 합쳐 만든다 (칸 폴더 공용 관례 result.*, AGENTS.md §2).
할부 거래는 최초 구매일 기준으로 transaction_id를 한 번만 부여하고, 이후 회차가
다른 월 파일에 다시 나와도 같은 id를 재사용한다 (지시서 "하는 단계 4" 규칙).

구매항목: 가공(refine) 담당 요청으로 추가된 필드 — 영수증으로 구매 물품까지 확인된
거래(collect_status=확인됨)만 채우고, 나머지(확인 필요)는 빈칸으로 둔다. 실제 영수증
OCR이 아직 없어 이 더미 데이터의 구매항목은 사람이 채운 가짜 값이다(명세서 8장 참고).
"""

import csv
import glob
import os
import random

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(BASE_DIR, "..", "sample_data", "hana_card")

OUT_FIELDS = ["transaction_id", "날짜", "지출", "수익", "결제처", "구매항목",
              "비고", "결제수단", "결제자", "source_type", "collect_status"]

# stub.md 예시가 참조하는 거래는 확인됨으로 고정한다 — 견본 문서와 값이 어긋나지 않게.
PINNED_VERIFIED = {"tx_260202_01", "tx_260224_01", "tx_260210_01"}

ITEM_POOL = {
    "스타벅스 강남점": ["아메리카노", "카페라떼", "아메리카노, 크루아상", "카페라떼, 베이글"],
    "GS25 학동역점": ["삼각김밥, 커피", "생수, 과자", "도시락, 음료수", "라면, 김밥"],
    "쿠팡": ["생수, 휴지", "샴푸, 바디워시", "무선 이어폰", "책상 정리함, 문구류", "운동화"],
    "CGV": ["영화 티켓, 팝콘", "영화 티켓, 콜라"],
    "무신사": ["티셔츠", "니트, 바지", "운동화", "자켓"],
    "이마트 성수점": ["생수, 과일, 세제", "쌀, 계란, 우유", "고기, 채소"],
    "넷플릭스": ["OTT 구독료"],
    "올리브영": ["스킨케어 세트", "마스크팩, 클렌징폼", "선크림"],
    "SK주유소": ["휘발유"],
    "김밥천국": ["김밥, 라면", "돈까스, 김밥"],
    "교촌치킨": ["허니콤보 치킨"],
    "파리바게뜨": ["식빵, 케이크", "샌드위치, 커피"],
    "네이버페이": ["생활용품", "도서", "전자기기 액세서리"],
    "다이소": ["수납정리함, 문구류", "주방용품", "청소용품"],
    "배달의민족": ["치킨, 콜라", "떡볶이, 순대", "짜장면, 탕수육"],
    "카카오T": ["택시비"],
    "하이마트 강남점": ["냉장고"],
}


def normalize(files):
    id_registry = {}  # (이용일자, 이용가맹점, 이용금액, 할부기간) -> transaction_id
    day_seq = {}       # YYMMDD -> 다음 순번
    by_month = {}       # YYYYMM -> rows

    for path in files:
        fname = os.path.basename(path)
        file_month = fname[:7].replace("-", "")  # YYYYMM
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                usage_date = row["이용일자"]
                merchant = row["이용가맹점"]
                amount = int(row["이용금액"])
                term = row["할부기간"]
                round_no = row["회차"]
                principal = int(row["원금"])
                fee = int(row["수수료"])
                benefit_label = row["이용혜택"]
                benefit_amt = int(row["혜택금액"])

                key = (usage_date, merchant, amount, term)
                if key not in id_registry:
                    yymmdd = usage_date[2:].replace("-", "")
                    day_seq.setdefault(yymmdd, 0)
                    day_seq[yymmdd] += 1
                    id_registry[key] = f"tx_{yymmdd}_{day_seq[yymmdd]:02d}"
                tx_id = id_registry[key]

                expense = principal + fee

                if term == "일시불":
                    date = usage_date
                    memo = f"{benefit_label} {benefit_amt:,}원" if benefit_label != "없음" else ""
                else:
                    date = f"{file_month[:4]}-{file_month[4:]}-25"
                    num, denom = round_no.split("/")
                    paid_off = " (완납)" if num == denom else ""
                    memo = f"할부 {round_no} (원금 {principal:,} + 수수료 {fee:,}), 총액 {amount:,}{paid_off}"

                by_month.setdefault(file_month, []).append({
                    "transaction_id": tx_id,
                    "날짜": date,
                    "지출": expense,
                    "수익": 0,
                    "결제처": merchant,
                    "구매항목": "",
                    "비고": memo,
                    "결제수단": "하나카드",
                    "결제자": "개인카드",
                    "source_type": "card_excel",
                    "collect_status": "확인됨",
                })

    return by_month


def assign_status_and_items(rows, seed=7):
    """collect_status 절반을 확인 필요로 바꾸고, 확인됨 건에만 구매항목을 채운다.

    PINNED_VERIFIED(stub.md 예시가 참조하는 거래)는 확인됨으로 유지한다.
    """
    rng = random.Random(seed)
    ids = sorted({r["transaction_id"] for r in rows} - PINNED_VERIFIED)
    rng.shuffle(ids)
    review_ids = set(ids[: len(ids) // 2])

    item_rng = random.Random(seed)
    for r in rows:
        if r["transaction_id"] in review_ids:
            r["collect_status"] = "확인 필요"
            r["구매항목"] = ""
        else:
            r["collect_status"] = "확인됨"
            pool = ITEM_POOL.get(r["결제처"])
            r["구매항목"] = item_rng.choice(pool) if pool else ""


def write_output(by_month):
    rows = [r for month_rows in by_month.values() for r in month_rows]
    assign_status_and_items(rows)
    rows.sort(key=lambda r: (r["날짜"], r["transaction_id"]))
    out_path = os.path.join(BASE_DIR, "result.csv")
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return out_path, len(rows)


if __name__ == "__main__":
    files = sorted(glob.glob(os.path.join(SRC_DIR, "*.csv")))
    if not files:
        print("수집 대상 없음")
    else:
        by_month = normalize(files)
        out_path, count = write_output(by_month)
        print(f"{os.path.basename(out_path)}: {count}건")
