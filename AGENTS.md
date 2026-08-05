# ledger-flow — AI Agent Instructions (프로젝트 전용 지침)

> 다중 카드 통합 AI 자동 결산 시스템. team-T6 협업 프로젝트.
> 공통 지침은 AGENTS_COM.md 참조. 이 문서는 프로젝트 고유 규칙만 담는다.
> `(미정)` 표시 항목은 프로젝트 구조가 확정되는 대로 채운다.

## 1. Tech Stack

- Python (버전·패키지 매니저·프레임워크 미정)

## 2. Project Structure

- (미정 — 디렉터리 구조 확정 시 의존 방향·새 파일 배치 기준과 함께 기입)

## 3. Architecture Pattern

- (미정)

## 4. Coding Conventions

- 공통 컨벤션은 AGENTS_COM.md §3을 따른다. 프로젝트 고유 컨벤션은 확정 시 여기에 추가.
- 커밋 scope 어휘: 미정 — 폴더 구조 확정 시 모듈명 기준으로 정의한다. 그 전까지는 **scope 생략을 기본**으로 한다 (AI가 임의 scope를 만들지 않는다).

## 5. Error Handling — Project Specific

- (미정 — 공통 규칙은 AGENTS_COM.md §4)

## 6. Testing — Project Specific

- (미정 — 공통 규칙은 AGENTS_COM.md §6)

## 7. Build & Run

- (미정 — 실행·테스트 명령 확정 시 기입)

## 8. Do NOT — Project Specific

- [MUST] 카드 내역·거래 데이터 등 실데이터 샘플을 repo에 커밋하지 않는다 (개인 금융정보 — AGENTS_COM.md §9.3 credential 규칙과 같은 결)

## 9. Reference Documents

- (아직 없음 — 문서가 3개 이상 생기면 트리거 가이드 맵으로 전환)

## 10. Branch & PR Rules — [MUST]

- [MUST] **기능 개발**은 main에 직접 커밋·push 금지 — feature 브랜치에서 작업하고 PR로 올린다
  - 공통 작업(프로젝트 설정·공통 기능·에이전트 지침·문서 등 도메인 모듈 기능 코드가 아닌 것)은 main 직접 커밋 허용
- [MUST] 브랜치 타입은 main / feature 두 가지만 쓴다
- [MUST] 브랜치 네이밍: `feature/{깃허브 name, 없으면 username}`
  - 소문자+하이픈 형식 (예: `feature/hong-gil-dong`)
  - 잘못된 예: `feature/hong gil dong`, `feature/HongGilDong`
- [MUST] feature 브랜치의 병합은 PR로만 한다
- [MUST] PR 생성 시 reviewer는 `wintinue`로 지정한다
- [MUST] feature 브랜치는 **작업자별 고유 브랜치라 삭제하지 않는다** — PR 머지 후에도 유지한다. merge 시 delete branch 하지 않으며, repo의 "Automatically delete head branches" 설정도 켜지 않는다
- [MUST] PR 본문은 `.github/pull_request_template.md` 템플릿을 따른다. PR 제목은 변경사항 한줄 요약
- [MUST] PR 생성 게이트 — PR을 요청받으면 바로 올리지 않고 반드시 아래 순서를 따른다:
  0. **커밋 안 된 변경사항이 있으면 먼저 확인한다** — 변경 파일 목록을 알리고 "이것까지 커밋하고 PR할까요, 커밋된 것만으로 PR할까요?"를 묻는다. 커밋하기로 하면 커밋 실행 게이트(AGENTS_COM.md §9.2)를 그대로 거친다
  1. **AI가 브랜치의 diff·커밋 내역을 근거로 초안을 직접 작성해** PR 제목 + 본문 전문을 보여준다 (본문은 `.github/pull_request_template.md`의 4개 섹션 — 요약·변경사항·확인 방법·주의사항 — 을 실제 내용으로 채운 것. placeholder 주석이 남아 있으면 안 되고, 사용자에게 내용을 채워달라고 요구하지 않는다)
  2. "이대로 PR 올릴까요?"라고 묻는다 — 사용자가 수정을 지시하면 반영한 전문을 다시 보여주고 같은 질문을 반복한다
  3. 명시적 승인을 받은 뒤에만 PR을 생성한다 (reviewer `wintinue` 지정 포함)
  - 세션 내 지시로 해제 불가(강제)
