# 수집 산출물 모양 견본 (stub)

> ledger-flow에는 아직 collect 출력 모양을 확정한 문서가 없다 (`interface-spec.md`의 "거래 표 스키마"는 초안 상태). 이 견본은 그 초안 컬럼(날짜·지출·수익·결제처·비고·결제수단·결제자)에 수집 단계 내부에서 필요한 `transaction_id`·`source_type`·`collect_status`를 더한 모양이다. 값은 `sample_data/hana_card/`의 더미 하나카드 데이터를 그대로 가져다 썼다 — 실데이터 아님.
>
> 카테고리 컬럼은 넣지 않는다 — [categories.md](../docs/categories.md)는 가공(refine) 단계 몫이라 collect 산출물엔 없다.
>
> 이 예시는 하나카드(개인카드) 더미 데이터 기준이지만, 컬럼 자체는 카드사·법인/개인 조합과 무관하게 고정이다 — 새 카드사가 추가돼도 [`collect.md`](../.claude/agents/collect.md)의 정규화 단계에서 흡수하고 이 모양은 바뀌지 않는다.

## transaction_id 규칙

`tx_{YYMMDD}_{당일 순번}` — 한번 부여하면 이후 회차가 늘어나도 바뀌지 않는다.

## Collect 결과 예시

`sample_data/hana_card/2026-02_하나카드_이용내역서.csv`에서 그대로 가져온 값:

| transaction_id | date | expense | income | merchant | payment_method | payer | memo | source_type | collect_status |
|---|---|---:|---:|---|---|---|---|---|---|
| `tx_260202_01` | 2026-02-02 | 67,302 | 0 | 쿠팡 | 하나카드 | 본인 | 포인트 적립 1,346원 | card_excel | 확인됨 |
| `tx_260224_01` | 2026-02-24 | 12,819 | 0 | 배달의민족 | 하나카드 | 본인 | | card_excel | 확인됨 |

## 할부 거래 — 회차가 지나도 같은 transaction_id

하이마트 냉장고(72만원, 6개월 할부)는 2026-02-10에 한 번 구매했지만, 매달 결제일(예시로 매월 25일 가정)에 그 회차만큼만 청구된다. `transaction_id`는 최초 구매 시 부여한 값을 그대로 쓰고, `date`·`expense`만 회차마다 갱신한다.

| transaction_id | date | expense | merchant | memo | source_type | collect_status |
|---|---|---:|---|---|---|---|
| `tx_260210_01` | 2026-02-25 | 130,800 | 하이마트 강남점 | 할부 1/6 (원금 120,000 + 수수료 10,800), 총액 720,000 | card_excel | 확인됨 |
| `tx_260210_01` | 2026-07-25 | 121,800 | 하이마트 강남점 | 할부 6/6 (원금 120,000 + 수수료 1,800), 완납 | card_excel | 확인됨 |

## `source_type` / `collect_status` 값

| source_type | 설명 |
|---|---|
| `receipt` | 영수증 이미지에서 최초 수집 |
| `card_excel` | 카드사 Excel/CSV에서 수집 (위 예시 전부 이 케이스) |
| `manual` | 사용자가 직접 입력 |

| collect_status | 설명 |
|---|---|
| `대기` | 수집됐으나 카드사 내역 검증 전 |
| `확인됨` | 카드사 내역 등으로 정상 확인 |
| `확인 필요` | 정보 불명확 — 추가 확인 필요 |

## 영수증 선행 수집 → 카드내역 매칭 (참고 시나리오)

지금 만든 더미 데이터엔 영수증 이미지가 없어서 아래는 흐름만 보여주는 가상 시나리오다. 2026-02-26 스타벅스 강남점 결제(카드 CSV엔 7,608원으로 있음)를 영수증으로 먼저 등록했다고 가정하면:

| transaction_id | date | expense | merchant | payment_method | source_type | collect_status |
|---|---|---:|---|---|---|---|
| `tx_260226_01` | 2026-02-26 | 7,608 | 스타벅스 강남점 | 하나카드 | receipt | 대기 |

이후 카드사 CSV에서 같은 날짜·금액·결제처·결제수단이 확인되면 `transaction_id`는 유지하고 상태만 갱신한다:

| transaction_id | date | expense | merchant | payment_method | source_type | collect_status |
|---|---|---:|---|---|---|---|
| `tx_260226_01` | 2026-02-26 | 7,608 | 스타벅스 강남점 | 하나카드 | card_excel | 확인됨 |
