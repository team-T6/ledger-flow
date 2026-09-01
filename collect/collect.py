"""collect 단계 정규화 스크립트.

sample_data/<YYYY-MM>/의 하나카드(개인카드)·신한법인카드(법인카드) CSV 원본을
(파일명의 카드사 이름으로 판별 — 같은 월 폴더에 다른 카드사·영수증 이미지가 섞여 있어도 이 둘만 골라 처리한다)
카드사별 컬럼 매핑 규칙으로 표준 거래 표로 정규화하고,
collect/result.csv 하나로 합쳐 만든다 (칸 폴더 공용 관례 result.*, AGENTS.md §2).
컬럼은 interface-spec.md "거래 표 스키마 (확정 v1)" 순서 그대로 13개 —
금액은 부호로 지출/수익 구분(지출 음수·수익 양수), 결제자 대신 결제구분(개인결제/법인결제),
해외결제 식별은 원거래통화·원거래금액(국내 결제는 빈칸), 카테고리는 가공 몫이라 빈칸.

할부 거래는 최초 구매일 기준으로 transaction_id를 한 번만 부여하고, 이후 회차가
다른 월 파일에 다시 나와도 같은 id를 재사용한다 (단계 문서 "하는 단계 4" 규칙).

구매항목: 가공(refine) 담당 요청으로 추가된 필드 — 영수증으로 구매 물품까지 확인된
거래(collect_status=확인됨)만 채우고, 나머지(확인 필요)는 빈칸으로 둔다. 실제 영수증
OCR이 아직 없어 이 더미 데이터의 구매항목은 사람이 채운 가짜 값이다.

지휘(orchestrator)가 파이프라인에서 부를 땐 run(month)을 쓴다 — 대상 월(YYYY-MM,
interface-spec.md §실행 파라미터)의 원본 파일 위주로 수집하고, 단계 결과 보고
(envelope JSON)를 호출 응답으로 반환한다 (보고 파일은 남기지 않는다 — 전달 방식 확정).

사용법: python3 collect/collect.py [YYYY-MM]
"""

import csv
import glob
import os
import random
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# sample_data/는 년월 폴더(YYYY-MM/)로 정리돼 있고, 그 안에 카드사·영수증이 섞여 있다 —
# 이 스크립트는 파일명으로 하나카드·신한법인카드 CSV만 골라 처리한다 (§ run() 참고)
SAMPLE_DIR = os.path.join(BASE_DIR, "..", "sample_data")
HANA_NAME_HINT = "하나카드"
SHINHAN_NAME_HINT = "신한법인카드"

# interface-spec.md "거래 표 스키마 (확정 v1)" 순서 그대로
OUT_FIELDS = ["transaction_id", "날짜", "금액", "결제처", "카테고리", "비고",
              "결제수단", "결제구분", "원거래통화", "원거래금액",
              "source_type", "source_file", "collect_status", "구매항목"]


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


def normalize(files, default_month=None):
    id_registry = {}  # (이용일자, 이용가맹점, 이용금액, 할부기간) -> transaction_id
    day_seq = {}       # YYMMDD -> 다음 순번
    by_month = {}       # YYYYMM -> rows

    for path in files:
        fname = os.path.basename(path)
        if re.match(r"^\d{4}-\d{2}", fname):
            file_month = fname[:7].replace("-", "")  # YYYYMM
        else:
            # 파일명에 월이 없는 원본(웹 업로드 등) — 호출자가 준 대상 월로 할부 대체일을 합성한다
            file_month = (default_month or "0000-00").replace("-", "")[:6]
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
                    "금액": -expense,  # 스키마 v1 — 지출은 음수
                    "결제처": merchant,
                    "카테고리": "",     # 가공(refine) 몫 — 자리만 만들어 둔다
                    "비고": memo,
                    "결제수단": "하나카드",
                    "결제구분": "개인결제",
                    "원거래통화": "",   # 국내 결제 — 해외결제 여부는 이 컬럼 유무로 판별
                    "원거래금액": "",
                    "source_type": "card_excel",
                    "source_file": fname,
                    "collect_status": "확인됨",
                    "구매항목": "",
                })

    return by_month


def parse_shinhan(files):
    """신한법인카드 승인내역 서식 매핑 (규칙 있는 서식 — 단계 문서 "AI 판단 / 일반 코드 구분").

    승인+취소 쌍은 걸러내지 않고 둘 다 넘긴다 — 취소 행은 환불(양수 금액)로 기입하고
    비고에 표기한다 (범위 밖 데이터의 반려는 검증 몫, run() docstring과 같은 원칙).
    통화가 KRW가 아니면 원거래통화·원거래금액으로 매핑한다 (해외결제 승격 규칙 확정).
    부정 사용 검증(F2 심야)이 쓸 수 있게 결제 시각을 비고에 남긴다.
    """
    rows = []
    for path in files:
        fname = os.path.basename(path)
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                date, time_part = row["승인일시"].split(" ")
                amount = int(row["승인금액"])
                cancelled = row["취소여부"].strip().upper() == "Y"
                currency = row["통화"].strip()
                is_foreign = bool(currency) and currency != "KRW"
                memo_bits = [f"결제시각 {time_part[:5]}", f"승인번호 {row['승인번호']}",
                             f"사용자 {row['사용자']}"]
                if cancelled:
                    memo_bits.append("승인취소")
                rows.append({
                    "transaction_id": "",  # run()에서 하나카드와 같은 체계로 이어 부여
                    "날짜": date,
                    "금액": amount if cancelled else -amount,
                    "결제처": row["가맹점명"],
                    "카테고리": "",
                    "비고": ", ".join(memo_bits),
                    "결제수단": "신한법인카드",
                    "결제구분": "법인결제",
                    "원거래통화": currency if is_foreign else "",
                    "원거래금액": row["해외이용금액"].strip() if is_foreign else "",
                    "source_type": "card_excel",
                    "source_file": fname,
                    "collect_status": "확인됨",
                    "구매항목": "",
                })
    return rows


def assign_status_and_items(rows, seed=7):
    """확인됨 건(핵심 값을 실제로 읽은 거래)에 구매항목을 채운다 (collect.md "하는 단계" 1).

    hana_card 표본은 날짜·금액·결제처를 전부 읽어내므로 collect_status는 파싱 시점에
    이미 확인됨으로 채워져 있다 — 여기서 상태를 임의로 뒤집지 않는다.
    """
    item_rng = random.Random(seed)
    for r in rows:
        if r["collect_status"] == "확인됨":
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
    return out_path, rows


def build_envelope(rows, message=""):
    """지휘에게 호출 응답으로 돌려주는 단계 결과 보고 — interface-spec.md "단계 결과 보고" 규격."""
    total = len(rows)
    if total == 0:
        return {
            "stage": "collect", "status": "empty", "output": "",
            "counts": {"total": 0, "ok": 0, "flagged": 0}, "flags": [],
            "message": message or "수집 대상 없음",
        }
    flags = []
    for idx, r in enumerate(rows, start=1):
        if r["collect_status"] != "확인됨":
            flags.append({
                "row": idx, "type": "확인 필요",
                "reason": f"{r['결제처']} 거래 — {r['collect_status']} 상태",
            })
    ok = total - len(flags)
    return {
        "stage": "collect",
        "status": "ok" if not flags else "partial",
        "output": "collect/result.csv",
        "counts": {"total": total, "ok": ok, "flagged": len(flags)},
        "flags": flags,
        "message": message,
    }


def run(month=None):
    """파이프라인 진입점 — 대상 월(YYYY-MM) 원본을 정규화해 result.csv를 만들고 envelope를 반환한다.

    month가 None이면 원본 전체를 수집한다 (수동 실행용). 대상 월 위주로 모으되
    걸러내는 책임은 지지 않는다 — 범위 밖 데이터의 반려는 기간·금액 검증 몫 (interface-spec.md §실행 파라미터).
    """
    month_dirs = sorted(glob.glob(os.path.join(SAMPLE_DIR, "[0-9][0-9][0-9][0-9]-[0-9][0-9]")))
    if month:
        month_dirs = [d for d in month_dirs if os.path.basename(d) == month]
    all_csvs = sorted(p for d in month_dirs for p in glob.glob(os.path.join(d, "*.csv")))
    files = [p for p in all_csvs if HANA_NAME_HINT in os.path.basename(p)]
    shinhan_files = [p for p in all_csvs if SHINHAN_NAME_HINT in os.path.basename(p)]
    if not files and not shinhan_files:
        return {"out_path": None, "rows": [], "envelope": build_envelope([])}

    by_month = normalize(files)
    shinhan_rows = parse_shinhan(shinhan_files)
    if shinhan_rows:
        # transaction_id는 하나카드 부여분과 같은 tx_YYMMDD_NN 체계를 이어 쓴다 (중복 방지)
        day_seq = {}
        for month_rows in by_month.values():
            for r in month_rows:
                m = re.match(r"tx_(\d{6})_(\d+)", r["transaction_id"])
                if m:
                    day_seq[m.group(1)] = max(day_seq.get(m.group(1), 0), int(m.group(2)))
        for r in shinhan_rows:
            yymmdd = r["날짜"][2:].replace("-", "")
            day_seq[yymmdd] = day_seq.get(yymmdd, 0) + 1
            r["transaction_id"] = f"tx_{yymmdd}_{day_seq[yymmdd]:02d}"
            by_month.setdefault(r["날짜"][:7].replace("-", ""), []).append(r)
    out_path, rows = write_output(by_month)
    return {"out_path": out_path, "rows": rows, "envelope": build_envelope(rows)}


if __name__ == "__main__":
    month_arg = sys.argv[1] if len(sys.argv) > 1 else None
    result = run(month_arg)
    if result["out_path"] is None:
        print("수집 대상 없음")
    else:
        print(f"{os.path.basename(result['out_path'])}: {len(result['rows'])}건")
