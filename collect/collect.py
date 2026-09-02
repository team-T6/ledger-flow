"""collect 단계 진입점 — 대상 월의 원본을 표준 거래 표(collect/result.csv)로 만든다.

카드사별 전용 매핑 규칙은 두지 않는다 (interface-spec.md 확정 로그 2026-09-02) — 어떤
카드사 서식이 와도 collect_uploads.py의 같은 변환 경로로 받는다. 이 파일에는 두 경로가
공유하는 거래 표 스키마(OUT_FIELDS)와, sample_data 원천으로 그 경로를 부르는 run()만 둔다.

컬럼은 interface-spec.md "거래 표 스키마 (확정 v1)" 순서 그대로 14개 —
금액은 부호로 지출/수익 구분(지출 음수·수익 양수), 결제자 대신 결제구분(개인결제/법인결제/빈 값),
해외결제 식별은 원거래통화·원거래금액(국내 결제는 빈칸), 카테고리는 가공 몫이라 빈칸.

지휘(orchestrator)가 파이프라인에서 부를 땐 run(month)을 쓴다 — 대상 월(YYYY-MM,
interface-spec.md §실행 파라미터)의 원본 폴더를 원천으로 수집하고, 단계 결과 보고
(envelope JSON)를 호출 응답으로 반환한다 (보고 파일은 남기지 않는다 — 전달 방식 확정).

사용법: python3 collect/collect.py [YYYY-MM]
"""

import importlib.util
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# sample_data/는 년월 폴더(YYYY-MM/)로 정리돼 있고, 그 안에 카드사 파일·영수증이 섞여 있다
SAMPLE_DIR = os.path.join(BASE_DIR, "..", "sample_data")

# interface-spec.md "거래 표 스키마 (확정 v1)" 순서 그대로
OUT_FIELDS = ["transaction_id", "날짜", "금액", "결제처", "카테고리", "비고",
              "결제수단", "결제구분", "원거래통화", "원거래금액",
              "source_type", "source_file", "collect_status", "구매항목"]


def run(month=None, on_progress=None):
    """파이프라인 진입점 — sample_data/<월>을 원천으로 수집기를 부른다.

    collect_uploads는 이 파일을 스키마 모듈로 읽으므로, 순환 import를 피하려 여기서 늦게 읽는다.
    """
    source_dir = os.path.join(SAMPLE_DIR, month) if month else SAMPLE_DIR
    spec = importlib.util.spec_from_file_location(
        "collect_uploads_from_sample", os.path.join(BASE_DIR, "collect_uploads.py"))
    uploads = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(uploads)
    return uploads.run(month, upload_dir=source_dir, on_progress=on_progress)


if __name__ == "__main__":
    month_arg = sys.argv[1] if len(sys.argv) > 1 else None
    result = run(month_arg)
    if result["out_path"] is None:
        print("수집 대상 없음")
    else:
        print(f"{os.path.basename(result['out_path'])}: {len(result['rows'])}건")
