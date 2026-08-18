# 통합 (merge) — 이 칸의 약속

> 담당: 박인혜 · 엑셀 회계장부 + PDF 결산 리포트 작성
> 계약의 정본은 [인터페이스 정의서](../docs/interface-spec.md), 단계 내부 설계는 [설계서](../docs/agents/merge.md), 에이전트에게 시키는 말은 [지시서](../.claude/agents/merge.md).

## 하는 일

가공(refine)이 만든 거래 표와 검증 1([verify1/](../verify1/))·검증 2([verify2/](../verify2/))의 항목별 통과/반려 결과를 모아, 엑셀 회계장부와 PDF 결산 리포트를 한 묶음으로 만든다. 파이프라인의 마지막 산출 단계다.

## 받는 것 / 내놓는 것

| | 내용 | 모양 |
|---|---|---|
| 받는 것 | 가공된 거래 표 + 검증1·검증2 결과 | [input-sample.md](input-sample.md) |
| 내놓는 것 | 엑셀 회계장부 + PDF 결산 리포트 | [stub.md](stub.md) |

검증 반려 건은 재시도하지 않고 곧장 "확인 필요" 목록으로 옮긴다 (반려 루프 없음). 앞 단계 결과가 비어 있거나 반려가 남아 있으면 그 자리에 "미완" 표시와 사유를 함께 적는다.

## 이 칸의 파일

| 파일 | 무엇 |
|---|---|
| `README.md` | 이 문서 — 칸의 약속 |
| `input-sample.md` | 받는 재료 견본 (가짜 데이터) |
| `stub.md` | 산출물 모양 견본 (가짜 데이터) |
| `call-agent.py` | merge 담당자(Claude)를 실제로 부르는 자리. `.claude/agents/merge.md`의 역할 지시문을 시스템 프롬프트로 쓴다 |
| `server.py` | `screen.html`과 `call-agent.py`를 잇는 로컬 서버 (표준 라이브러리만 사용) |
| `screen.html` | 실제 호출 화면 — `server.py`가 떠 있어야 동작한다 |
| `result.xlsx` / `result.pdf` | 진짜 산출물 — 실행하면 생김. **커밋하지 않는다** |

결과를 만드는 데 코드가 필요하면 그 코드도 이 칸 폴더 안에 둔다. 칸 폴더를 옮기지 않고, 기존 파일을 지우지 않는다. 새 산출물은 견본 옆에 만든다.

## 지금 상태

`result.xlsx`·`result.pdf`를 실제로 만드는 코드는 아직 없다. `call-agent.py`는 지금은 입력 글을 Claude에게 보내 처리 결과를 텍스트로 받아오기만 하고, 엑셀·PDF 파일을 쓰지는 않는다 ([docs/agents/merge.md](../docs/agents/merge.md) §6·§8 "막힌 점" 참고).

## 지켜야 할 것

- **API 키를 커밋하지 않는다.** `ANTHROPIC_API_KEY`는 팀 폴더 맨 위 `.env`(gitignore 대상)에서만 읽고, `call-agent.py`를 포함한 이 칸의 어떤 파일에도 값을 적지 않는다
- **실데이터를 커밋하지 않는다.** 견본은 [sample_data/](../sample_data/) 기반 가짜 데이터로만 만든다 (개인 금융정보)
- **명세 먼저, 구현 나중.** 동작을 바꾸려면 [설계서](../docs/agents/merge.md)를 먼저 고친다. 단계 간 약속이 걸리면 [인터페이스 정의서](../docs/interface-spec.md)를 담당자 합의로 고친다
