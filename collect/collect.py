"""collect 단계 정규화 스크립트.

sample_data/hana_card/*.csv(카드사 원본, 월별 1파일)를 읽어 표준 거래 표로 정규화하고,
collect/result.csv 하나로 합쳐 만든다 (칸 폴더 공용 관례 result.*, AGENTS.md §2).
할부 거래는 최초 구매일 기준으로 transaction_id를 한 번만 부여하고, 이후 회차가
다른 월 파일에 다시 나와도 같은 id를 재사용한다 (지시서 "하는 단계 4" 규칙).
"""

import csv
import glob
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(BASE_DIR, "..", "sample_data", "hana_card")

OUT_FIELDS = ["transaction_id", "날짜", "지출", "수익", "결제처",
              "비고", "결제수단", "결제자", "source_type", "collect_status"]


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
                    "비고": memo,
                    "결제수단": "하나카드",
                    "결제자": "개인카드",
                    "source_type": "card_excel",
                    "collect_status": "확인됨",
                })

    return by_month


def write_output(by_month):
    rows = [r for month_rows in by_month.values() for r in month_rows]
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
