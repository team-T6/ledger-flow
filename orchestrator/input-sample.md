# 지휘 받는 재료 견본 (input-sample)

> 지휘가 받는 것은 산출물이 아니라 **각 단계의 결과 보고 JSON**이다 (산출물 내용은 열어보지 않는다). 보고는 단계 호출의 **응답(반환값)** 으로 직접 받는다 (전달 방식 확정). 규격 정본은 [인터페이스 정의서](../docs/interface-spec.md) "단계 결과 보고" — 여기서는 필드를 재정의하지 않고, **status 케이스별 견본**만 둔다.
>
> 값은 분류 검증 칸의 견본([verify1/input-sample.md](../verify1/input-sample.md), `sample_data/2026-02/` 더미 5건)과 같은 시나리오 기준 — 실데이터 아님.

## 케이스 1 — `ok` (정상 산출: 다음 단계 진행)

```json
{
  "stage": "collect",
  "status": "ok",
  "output": "collect/result.csv",
  "counts": { "total": 5, "ok": 5, "flagged": 0 },
  "flags": [],
  "message": "카드사 CSV 1개 처리, 이미지 0건"
}
```

## 케이스 2 — `partial` (산출했지만 flagged 건 있음: 진행 + 요약 "확인 필요 목록"에 싣기)

```json
{
  "stage": "verify1",
  "status": "partial",
  "output": "verify1/result.csv",
  "counts": { "total": 5, "ok": 2, "flagged": 3 },
  "flags": [
    { "row": 3, "type": "반려", "reason": "카테고리 \"식비\"는 체계에 없음 — \"식대\"로 추정" },
    { "row": 4, "type": "반려", "reason": "카테고리 \"카페\"는 체계에 없음 — \"식대\"로 추정" },
    { "row": 5, "type": "반려", "reason": "카테고리가 비어 있어 판정 불가 (가공에서 \"확인 필요\" 처리된 건)" }
  ],
  "message": ""
}
```

## 케이스 3 — `empty` (처리할 대상 없음: 다음 단계 진행)

```json
{
  "stage": "collect",
  "status": "empty",
  "output": "",
  "counts": { "total": 0, "ok": 0, "flagged": 0 },
  "flags": [],
  "message": "수집 대상 없음"
}
```

## 케이스 4 — `failed` (단계 실패, 산출물 없음: 동일 입력 재호출 1회 후 실패 위치 기준 처리 — `message` 필수)

```json
{
  "stage": "refine",
  "status": "failed",
  "output": "",
  "counts": { "total": 0, "ok": 0, "flagged": 0 },
  "flags": [],
  "message": "입력 파일 collect/result.csv를 열 수 없음"
}
```

## 유실 감지 기준

단계 사이에서 `counts.total`을 대조한다 — 위 시나리오라면 수집 5건이 가공·검증·통합까지 5건으로 유지돼야 하고, 줄어들면 거래 행 유실로 본다.
