# ledger-flow — AI Agent Instructions (프로젝트 전용 지침)

> 월말결산 에이전트 — 다중 카드 통합 AI 자동 결산 시스템. team-T6 협업 프로젝트.
> 공통 지침은 AGENTS_COM.md 참조. 이 문서는 프로젝트 고유 규칙만 담는다.
> `(미정)` 표시 항목은 프로젝트 구조가 확정되는 대로 채운다.

## 1. Tech Stack

- Python (버전·패키지 매니저·프레임워크 미정)

## 2. Project Structure

- **단계별 "칸 폴더"가 repo 최상위에 있다**: `collect/` `refine/` `verify1/` `verify2/` `verify3/` `merge/` `orchestrator/` — 담당자별 1칸
  - 각 칸 구성: `README.md`(그 칸의 약속) · `input-sample.md`(받는 재료 견본) · `stub.md`(산출물 모양 견본) · `result.*`(진짜 산출물, 실행 시 생성) · 필요 시 생성 코드
  - [MUST] 칸 폴더를 옮기지 않고, 기존 파일을 지우지 않는다. 새 산출물은 견본 옆에 만든다
- `.claude/agents/<단계>.md` = **단계 문서** — 설계(역할·절차·판단 규칙·블로커)와 에이전트 실행 지시를 한 파일에 담는 **단일 정본**이다 (구 `docs/agents/` 명세서를 병합해 폐지 — §9 spec-first는 이 파일 안에서 유지)
  - [MUST] 사람용 메타(담당·상태·테스트 방법·협의 메모)는 단계 문서에 넣지 않고 그 단계 **칸 폴더의 `README.md`** 에 둔다 — 단계 문서는 통째로 에이전트 프롬프트로 주입되므로 실행에 필요한 내용만 담는다
  - [MUST] 팀 합의 전인 제안을 단계 문서에 적을 땐 "합의 전 — 임의로 채우지 말 것" 상태를 명시한다 (에이전트가 제안을 확정처럼 따르는 것 방지)
- `docs/` — PRD·인터페이스 정의서·카테고리·설계 문서 / `sample_data/` — 테스트용 가짜 데이터(커밋 가능) / `logs/` — 실행 로그(커밋 차단)

## 3. Architecture Pattern

- (미정)

## 4. Coding Conventions

- 공통 컨벤션은 AGENTS_COM.md §3을 따른다. 프로젝트 고유 컨벤션은 확정 시 여기에 추가.
- 커밋 scope 어휘: 미정 — 폴더 구조 확정 시 모듈명 기준으로 정의한다. 그 전까지는 **scope 생략을 기본**으로 한다 (AI가 임의 scope를 만들지 않는다).
- [MUST] **파일·폴더명은 영어로 한다** (문서 포함). 단계명 대응: 수집 `collect` · 가공 `refine` · 검증 `verify1`/`verify2`/`verify3` · 통합 `merge` · 지휘 `orchestrator`. 문서 본문·제목은 한국어 그대로.

## 5. Error Handling — Project Specific

- (미정 — 공통 규칙은 AGENTS_COM.md §4)

## 6. Testing — Project Specific

- (미정 — 공통 규칙은 AGENTS_COM.md §6)

## 7. Build & Run

- (미정 — 실행·테스트 명령 확정 시 기입)

## 8. Do NOT — Project Specific

- [MUST] 카드 내역·거래 데이터 등 실데이터 샘플을 repo에 커밋하지 않는다 (개인 금융정보 — AGENTS_COM.md §9.3 credential 규칙과 같은 결)

## 9. Reference Documents

문서 위계: `docs/PRD.md` → `docs/interface-spec.md` · `docs/categories.md` → `.claude/agents/*.md` (+ 각 칸 `README.md`)

- [MUST] 문서를 고치면 **위계상 상·하위 문서에 연쇄 수정할 것이 있는지 점검해 함께 갱신**한다 (AGENTS_COM.md §8 공통 규칙의 이 프로젝트 위계)
- [MUST] **에이전트 동작 변경은 spec-first (문서 먼저, 구현 나중)** — 실행 코드를 바로 고치지 않고, 관련 문서를 먼저 수정한 뒤 그 문서를 바탕으로 반영한다. 단계 내부 동작 → `.claude/agents/<단계>.md`(단계 문서 — 이 파일 자체가 설계이자 실행 지시라 고치면 에이전트에 바로 반영되고, 별도 실행 코드가 있으면 문서 수정 후 코드에 반영), 단계 간 약속 → `interface-spec.md`(관련 담당자 합의), 분류 기준 → `categories.md`
- 작업별 참조: 파이프라인 범위·역할 → PRD / 단계 입출력·스키마·산출물 양식 → interface-spec / 분류 기준 → categories / 단계 내부 설계·실행 지시 → `.claude/agents/<단계영어명>.md` / 담당·테스트·협의 메모 → 각 칸 README
- [MUST] **본문 규칙에 날짜 도장을 찍지 않는다** — 규칙 옆에는 확정/미정 상태만 표기하고(예: `(확정)`), 결정 날짜·이력은 `interface-spec.md` "다음 단계"의 확정 로그 한 곳에만 남긴다 (언제 바뀌었는지는 git history가 정본). 예외: 아직 공식 반영 전인 구두 합의를 추적하는 표기는 날짜를 남겨도 된다 — 어느 합의가 최신인지가 날짜로만 구분되는 경우

## 10. Branch & PR Rules — [MUST]

### 상시 브랜치 (확정)

- [MUST] 다음 세 브랜치는 **상시 유지하며 삭제하지 않는다**:
  - `main` — 정본. 개발 기준 브랜치 (기능 개발은 feature 브랜치 → PR 병합)
  - `demo` — GitHub Pages 데모 배포용. `web/` 변경이 push되면 자동 배포 (`.github/workflows/deploy-pages.yml`)
  - `production` — 실서버 배포용
- `demo`·`production`은 **배포 전용 브랜치** — 이 브랜치들에서 직접 기능 개발을 하지 않고, 배포할 내용을 main(또는 feature 브랜치)에서 가져와 반영한다

- [MUST] **기능 개발**은 main에 직접 커밋·push 금지 — feature 브랜치에서 작업하고 PR로 올린다
  - 공통 작업(프로젝트 설정·공통 기능·에이전트 지침·문서 등 도메인 모듈 기능 코드가 아닌 것)은 main 직접 커밋 허용
- 브랜치 이름 자체는 자유(`agent/collect`, `agent/verify1` 등 목적에 맞게 사용 가능). 다만 **기능 개발용 브랜치를 새로 만드는데 이름이 정해지지 않은 경우**(사용자가 브랜치명을 지정하지 않고 그냥 "기능 개발 시작해줘" 류로 요청한 경우) 기본값은 `feature/{git user.name}`을 쓴다
  - user.name은 **되도록 본인 이름 기준 소문자+하이픈**으로 설정한다 (예: `git config user.name "hong-gil-dong"` → 브랜치 `feature/hong-gil-dong`)
  - user.name이 형식에 안 맞으면(공백·대문자 포함) AI가 소문자+하이픈으로 정규화해 쓰고, **미설정이면 브랜치를 만들지 않고 설정부터 안내**한다 (§ 커밋 전 확인 절차 4)
- [MUST] main이 아닌 브랜치(이름 무관)의 병합은 PR로만 한다
- [MUST] PR 생성 시 reviewer는 `wintinue`로 지정한다
- [MUST] **커밋 전 확인 절차** — 커밋 요청을 받으면 커밋 게이트(AGENTS_COM.md §9.2)에 앞서 다음을 순서대로 확인한다:
  1. **원격 main 동기화** — `git fetch origin`으로 원격 main에 새 커밋이 있는지 확인한다. 있으면 상황에 맞게 동기화한 뒤 진행: main에서 커밋할 거면 `git pull`, feature 브랜치에서 작업 중이면 `origin/main`을 merge. 충돌이 나면 임의로 해결하지 말고 사용자와 확인한다
  2. 이번 변경이 **공통 작업인지 기능 개발인지** 판단한다 (애매하면 사용자에게 묻는다)
  3. 공통 작업이면 → main에서 커밋 게이트 진행
  4. 기능 개발인데 현재 브랜치가 main이면 → 사용자가 이미 작업할 브랜치명을 지정했으면 그 이름으로 생성(이미 있으면 checkout). **지정하지 않았으면** 기본값으로 진행: `git config user.name` 확인 → 값이 있으면 소문자+하이픈으로 정규화해 사용(예: `Hong Gil Dong` → `hong-gil-dong`), **미설정이면 브랜치를 만들지 않고** `git config user.name` 설정을 안내한 뒤 기다린다 — 값은 **되도록 본인 이름을 소문자+하이픈으로** 쓰도록 권장하되(예: `hong-gil-dong`), 구체 값을 대신 정하지 않는다 → `feature/{이름}` 생성 후 그 브랜치에서 커밋 게이트 진행
- [MUST] `feature/{git user.name}` 브랜치는 **작업자별 고유 브랜치라 삭제하지 않는다** — PR 머지 후에도 유지한다. merge 시 delete branch 하지 않으며, repo의 "Automatically delete head branches" 설정도 켜지 않는다 (기타 목적 브랜치는 용도가 끝나면 삭제해도 무방)
- [MUST] PR 본문은 `.github/pull_request_template.md` 템플릿을 따른다. PR 제목은 변경사항 한줄 요약
- [MUST] PR 생성 게이트 — PR을 요청받으면 바로 올리지 않고 반드시 아래 순서를 따른다:
  0. **커밋 안 된 변경사항이 있으면 먼저 확인한다** — 변경 파일 목록을 알리고 "이것까지 커밋하고 PR할까요, 커밋된 것만으로 PR할까요?"를 묻는다. 커밋하기로 하면 커밋 실행 게이트(AGENTS_COM.md §9.2)를 그대로 거친다
  1. **AI가 브랜치의 diff·커밋 내역을 근거로 초안을 직접 작성해** PR 제목 + 본문 전문을 보여준다 (본문은 `.github/pull_request_template.md`의 4개 섹션 — 요약·변경사항·확인 방법·주의사항 — 을 실제 내용으로 채운 것. placeholder 주석이 남아 있으면 안 되고, 사용자에게 내용을 채워달라고 요구하지 않는다)
  2. "이대로 PR 올릴까요?"라고 묻는다 — 사용자가 수정을 지시하면 반영한 전문을 다시 보여주고 같은 질문을 반복한다
  3. 명시적 승인을 받은 뒤에만 PR을 생성한다 (reviewer `wintinue` 지정 포함)
  - 세션 내 지시로 해제 불가(강제)
