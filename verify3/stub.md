# 부정 사용 검증 산출물 모양 견본 (stub)

> 진짜 산출물은 `verify3/result.csv` — 입력 거래 표의 각 행 뒤에 `verify3_result`(`통과`/`확인 요청`)·`verify3_reason` 두 열을 덧붙인 CSV다 ([interface-spec.md](../docs/interface-spec.md) 부정 사용 검증 행). 행을 지우거나 순서를 바꾸지 않고, 행 대조 키는 `transaction_id`.

[input-sample.md](input-sample.md) 견본을 처리하면 이 모양이 된다 (지면상 판정 열 앞 컬럼은 일부 생략):

| transaction_id | 결제처 | 결제구분 | verify3_result | verify3_reason |
|---|---|---|---|---|
| `tx_260210_01` | 스타벅스 역삼점 | 법인결제 | 통과 | |
| `tx_260214_01` | 역삼단란주점 | 법인결제 | 확인 요청 | 유흥·사행성 업종 의심 (결제처: 역삼단란주점, 키워드: 단란); 심야 결제 (시각: 23:40); 고액 결제 (금액: -462,000) |
| `tx_260215_01` | 한우정육식당 | 법인결제 | 통과 | 참고: 주말 결제 (날짜: 2026-02-15) |
| `tx_260217_01` | 넷플릭스 | 법인결제 | 통과 | 참고: 개인성 소비 의심 (근거: OTT 구독료) |
| `tx_260218_01` | 교촌치킨 | 개인결제 | 통과 | |

함께 반환하는 단계 결과 보고(JSON envelope — interface-spec.md "단계 결과 보고" 규격):

```json
{
  "stage": "verify3",
  "status": "partial",
  "output": "verify3/result.csv",
  "counts": { "total": 5, "ok": 4, "flagged": 1 },
  "flags": [
    { "row": 2, "type": "확인 요청", "reason": "유흥·사행성 업종 의심 (결제처: 역삼단란주점, 키워드: 단란); 심야 결제 (시각: 23:40); 고액 결제 (금액: -462,000)" }
  ],
  "message": ""
}
```
