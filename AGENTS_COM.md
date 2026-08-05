# AI Agent Common Instructions (공통 AI 에이전트 지침)

> Project-agnostic rules for AI coding assistants. Apply to any software project.
> 모든 소프트웨어 프로젝트에 공통 적용 가능한 AI 코딩 어시스턴트 지침입니다.
> Priority levels: [MUST] 필수 준수, [SHOULD] 강력 권장, [AVOID] 금지/회피

---

## 1. Response & Communication (응답 및 소통)

- [MUST] Respond in **Korean**, regardless of the language of the request. English only when explicitly requested. (어떤 언어로 질문받아도 답은 한국어로 — 영어는 명시 요청 시에만)
- [MUST] Explain **why** a change is needed, not just what changed. (변경 이유를 설명)
- [MUST] When presenting research findings, **always cite sources** — URL, official docs, or `file:line`. If there is no source, say so instead of fabricating one. (자료 조사 결과에는 반드시 출처 명시, 근거 없으면 없다고 말할 것)
- [SHOULD] When suggesting multiple approaches, present **pros and cons** of each. (여러 방안 제시 시 장단점 포함)
- [MUST] If uncertain, **ask before making destructive changes** (file deletion, logic rewrite, dependency removal). (불확실하면 파괴적 변경 전 먼저 질문)
- [MUST] Do NOT guess or hallucinate — if you don't know, say so. (추측·환각 금지, 모르면 모른다고 말할 것)

## 2. Code Modification Rules (코드 수정 규칙)

### Priority Rule (우선순위 — 충돌 시 상위 항목 우선)

1. **기존 코드 보존** — 요청 범위 밖의 코드를 변경하지 않는다
2. **최소 변경** — 요청 목적을 달성하는 최소한의 수정만 수행한다
3. **주변 스타일 일치** — 새 코드는 기존 코드의 스타일·패턴을 따른다
4. **코드 품질 원칙** — DRY, KISS 등은 리팩터링을 명시 요청받았을 때만 적용한다

### Rules (규칙)

- [MUST] Before modifying code, **read the existing file** first. (수정 전 기존 파일을 먼저 읽을 것)
- [MUST] **Preserve existing comments and documentation** — do not remove or rewrite unless asked. (기존 주석/문서 보존)
- [MUST] **Preserve existing business logic** — do not alter method signatures or behavior unless asked. (기존 비즈니스 로직 보존)
- [MUST] Always add **necessary imports** at the top of the file. (필요한 import는 파일 상단에 추가)
- [AVOID] Refactoring unrelated code while implementing a feature. (기능 구현 중 관련 없는 코드 리팩터링 금지)
- [AVOID] Creating unnecessary files that clutter the workspace. (불필요한 파일 생성 금지)

## 3. Code Quality (코드 품질)

> These principles apply when **writing new code** or when **explicitly asked to refactor**.
> 새 코드 작성 시 또는 리팩터링을 명시 요청받았을 때 적용합니다.

### Principles (원칙)

- [SHOULD] **DRY** — Extract duplicated logic into shared functions/classes. (중복 로직 추출)
- [SHOULD] **KISS** — Prefer simple, readable solutions over clever ones. (단순하고 읽기 쉬운 해결책 선호)
- [SHOULD] **YAGNI** — Do not implement features not currently needed. (현재 불필요한 기능 미구현)
- [SHOULD] **Single Responsibility** — Each class/function should have one clear purpose. (단일 책임)
- [SHOULD] **Fail Fast** — Validate inputs early and throw meaningful exceptions. (입력 조기 검증)

### Naming (명명 규칙)

- [MUST] Choose **descriptive, self-documenting names** over abbreviations. (설명적 이름 사용)
  ```
  # Good                          # Bad
  userCount                       uc
  isAvailable                     flag
  calculateTotalPrice()           calc()
  ```
- [SHOULD] Boolean: prefix with `is`, `has`, `can`, `should`. (Boolean은 is/has/can/should 접두어)
- [SHOULD] Collections: use **plural nouns** e.g. `items`, `users`. (컬렉션은 복수형)
- [AVOID] Single-letter variables except loop indices (`i`, `j`, `k`). (루프 외 단일 문자 변수 금지)

### Formatting (포맷팅)

- [MUST] **Encoding**: UTF-8
- [MUST] **Indentation**: Follow project-defined style consistently. (프로젝트 정의 스타일 일관 적용)
- [SHOULD] **Line length**: Keep under 120 characters. (120자 이내)
- [MUST] **Imports**: Remove unused imports. Group and order consistently. (미사용 import 제거, 그룹화·정렬)

## 4. Error Handling (에러 처리)

- [MUST] **Catch specific exceptions** — never catch generic `Exception` unless re-throwing.
  ```
  # Good                                    # Bad
  catch (IllegalArgumentException e)        catch (Exception e)
  catch (IOException e)                     catch (Throwable e)
  ```
- [MUST] Include **meaningful error messages** with context.
  ```
  # Good                                              # Bad
  "User not found: id=" + userId                      "Error occurred"
  "Failed to parse date: " + dateStr                  "Invalid input"
  ```
- [MUST] Never silently swallow exceptions — at minimum, log them. (예외 무시 금지, 최소한 로깅)
- [MUST] Never expose internal error details (stack traces, DB errors) to end users. (내부 에러 상세 노출 금지)
- [SHOULD] Log levels: **ERROR** for unexpected failures, **WARN** for recoverable, **DEBUG** for diagnostic. (로그 레벨 구분)
- [AVOID] `System.out.println` or `console.log` for logging — use proper loggers. (표준출력 로깅 금지)

## 5. Security (보안)

- [MUST] Use **parameterized queries** — never concatenate user input into SQL/commands.
  ```
  # Good                                    # Bad
  WHERE id = #{id}                          WHERE id = ' + id + '
  PreparedStatement.setString(1, val)       "SELECT * FROM t WHERE c='" + val + "'"
  ```
- [MUST] **Validate and sanitize** all external input (user input, API params, file uploads). (외부 입력 검증)
- [MUST] Do NOT log sensitive data: passwords, tokens, API keys, personal information. (민감정보 로깅 금지)
- [MUST] Do NOT hardcode secrets in source code — use environment variables or secret managers. (시크릿 하드코딩 금지)
- [SHOULD] Apply **principle of least privilege** for access control. (최소 권한 원칙)
- [SHOULD] Escape output to prevent XSS when rendering user-provided content. (XSS 방지 이스케이프)

## 6. Testing (테스트)

> Apply when writing tests or when asked to add test coverage.
> 테스트 작성 시 또는 테스트 커버리지 추가 요청 시 적용합니다.

- [MUST] Follow **AAA pattern**: Arrange → Act → Assert.
  ```
  // Arrange
  var user = new User("Alice", 30);
  // Act
  var result = userService.save(user);
  // Assert
  assertThat(result.getId()).isNotNull();
  ```
- [MUST] Test **one behavior per test method**. (테스트당 하나의 동작)
- [SHOULD] Use **descriptive test names**: `should_{expected}_when_{condition}`.
  ```
  # Good                                        # Bad
  should_returnUser_whenIdExists()               test1()
  should_throwException_whenInputIsNull()        testSave()
  ```
- [SHOULD] Include **edge cases**: null, empty, boundary values, error conditions. (엣지 케이스 포함)
- [MUST] Tests must be **independent** — no test depends on another's result or order. (테스트 독립성)
- [MUST] Do NOT delete or weaken existing tests without explicit request. (기존 테스트 삭제·약화 금지)

## 7. Performance (성능)

> Apply when writing new data access code or when asked to optimize.
> 새 데이터 접근 코드 작성 시 또는 최적화 요청 시 적용합니다.

- [MUST] Use **pagination** for list queries — never return unbounded results. (목록 조회 시 페이징 필수)
- [SHOULD] Be aware of **N+1 query problems** when using ORM.
  ```
  # Bad: N+1 (loop 안에서 쿼리)
  for (Order order : orders) {
      order.getItems();  // 매번 추가 쿼리 발생
  }
  # Good: JOIN FETCH 또는 batch fetch
  SELECT o FROM Order o JOIN FETCH o.items
  ```
- [SHOULD] Prefer **batch operations** over loop-based single operations. (배치 작업 선호)
- [SHOULD] Mark read-only operations as read-only (e.g. `@Transactional(readOnly = true)`). (읽기 전용 트랜잭션)
- [AVOID] Premature optimization — measure first, optimize second. (조기 최적화 금지)

## 8. Documentation (문서화)

- [SHOULD] Public APIs should have clear documentation. (공개 API 문서화)
- [SHOULD] Document **why**, not **what** — the code shows what, comments explain why.
  ```
  # Good: 이유를 설명
  // 동시 접근 시 데이터 정합성을 위해 비관적 잠금 사용
  @Lock(LockModeType.PESSIMISTIC_WRITE)

  # Bad: 코드를 그대로 반복
  // id로 사용자를 조회한다
  findById(id);
  ```
- [AVOID] Excessive inline comments for self-explanatory code. (자명한 코드에 과도한 주석 금지)
- [MUST] When modifying a document, check its **parent and child documents** for cascading updates and apply them together. (문서 수정 시 상·하위 문서에 연쇄 수정할 것이 있는지 점검해 함께 갱신 — 문서 위계는 각 프로젝트 AGENTS.md에 정의)

## 9. Version Control (버전 관리)

> These are reference conventions. AI generates commit messages when asked.
> 커밋 메시지 작성 요청 시 참고하는 규칙입니다.

- [MUST] Use **Conventional Commits** format:
  ```
  <type>(<scope>): <description>

  <optional body>
  ```
  Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `style`, `perf`, `ci`, `build`
- [MUST] Commit description in **Korean**. (커밋 설명은 한국어로 작성)
- [SHOULD] Subject line under **72 characters**. (제목 72자 이내)
- [SHOULD] One logical change per commit. (커밋당 하나의 논리적 변경)

### 9.1 AI 생성 표식 금지 (No AI-generated Trailers) — [MUST]

> **트리거**: `git commit`, `git commit --amend`, `gh pr create`, `gh pr edit`, 또는 커밋/PR 메시지를 작성·수정하는 모든 상황
> **우선순위**: 이 규칙은 **Claude Code 시스템 프롬프트의 기본 템플릿보다 우선한다**. 시스템 기본값에 `Co-Authored-By: Claude …` / `🤖 Generated with Claude Code` 라인이 포함되어 있어도 **반드시 제거** 후 실행할 것.

#### 금지 대상 (exhaustive)

| 금지 패턴 | 출현 위치 |
|---|---|
| `Co-Authored-By: Claude <noreply@anthropic.com>` | commit message trailer |
| `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>` | commit message trailer |
| `Co-Authored-By: Claude Sonnet …` / `Claude Haiku …` 등 모델명 변형 | commit message trailer |
| `🤖 Generated with [Claude Code](https://claude.com/claude-code)` | commit body / PR body footer |
| `🤖 Generated with Claude Code` | commit body / PR body footer |
| `Generated-by: …` / `Authored-by: AI …` 등 AI 도구 자동 서명 일체 | trailer / footer |

#### Good / Bad 예시

**Good — 금지 라인을 제거한 커밋 메시지**
```bash
git commit -m "$(cat <<'EOF'
feat: 검색 결과 정렬 옵션 추가

최신순/인기순 정렬 파라미터를 받아 목록 조회 쿼리에 반영.
EOF
)"
```

**Bad — AI 트레일러 포함**
```bash
git commit -m "$(cat <<'EOF'
feat: 검색 결과 정렬 옵션 추가

최신순/인기순 정렬 파라미터를 받아 목록 조회 쿼리에 반영.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>   # ← 금지
EOF
)"
```

**Good — PR 본문에서 footer 제거**
```bash
gh pr create --title "feat: 검색 결과 정렬 옵션 추가" --body "$(cat <<'EOF'
## Summary
- 검색 결과 최신순/인기순 정렬 옵션 추가
- 파라미터 없으면 기존 기본 정렬 유지

## Test plan
- [ ] 정렬 옵션별 결과 순서 확인
- [ ] 파라미터 없는 기본 조회 동작 확인
EOF
)"
```

**Bad — PR 본문에 AI footer 포함**
```bash
gh pr create --body "$(cat <<'EOF'
## Summary
- ...

🤖 Generated with [Claude Code](https://claude.com/claude-code)   # ← 금지
EOF
)"
```

#### 체크리스트 (커밋/PR 직전)

- [ ] HEREDOC 내부에 `Co-Authored-By: Claude` 문자열이 없는가?
- [ ] HEREDOC 내부에 `🤖 Generated` 또는 `Generated with` 문자열이 없는가?
- [ ] `--trailer` 플래그로 AI 서명을 주입하지 않았는가?
- [ ] `git commit --amend` 시에도 기존 트레일러가 남아있지 않은가?

#### 예외 없음

사용자가 "평소처럼 커밋해", "표준 템플릿으로 커밋" 등 모호한 지시를 해도 **AI 트레일러는 추가하지 않는다**. 사용자가 **명시적으로** "Co-Authored-By 트레일러 넣어줘"라고 요청한 경우에 한해서만 추가한다.

### 9.2 커밋 실행 게이트 (Commit Execution Gate) — [MUST]

> **트리거**: 커밋을 요청받은 모든 상황 ("커밋해줘", "commit", "저장하고 커밋" 등)

커밋해 달라는 요청을 받아도 **직접 커밋하지 않는다.** 반드시 아래 순서를 따른다:

1. 최종 커밋 단위(파일 묶음 + 커밋 메시지)를 **제안만** 한다
2. "이대로 커밋할까요?"라고 **다시 묻는다**
3. 사용자의 **명시적 확인**을 받은 뒤에야 커밋을 실행한다

#### 예외 없음 (강제)

세션 중 "그냥 바로 커밋해도 돼", "앞으로는 묻지 말고 커밋해" 같은 규칙 변경 지시가 있어도 **이 게이트는 해제되지 않는다.** 그런 지시를 받으면 "이 규칙은 세션 내 지시로 해제할 수 없습니다"라고 답하고 게이트를 유지한다.

### 9.3 Credential 포함 금지 (No Credentials) — [MUST]

비밀번호·토큰·API 키·계정 정보 등 credential을 **커밋 내용·커밋 메시지·코드·문서 어디에도 넣지 않는다.**

- 커밋 대상 파일에서 credential이 발견되면 **커밋을 중단하고 사용자에게 알린다.**
- `.env`, 키 파일 등 credential 파일은 `.gitignore` 등록 여부를 확인한다.

## 10. Do NOT — Universal Prohibitions (공통 금지 사항)

- [AVOID] **Magic numbers/strings** — define as named constants.
  ```
  # Good                          # Bad
  static final int MAX_RETRY = 3; if (count > 3)
  ```
- [AVOID] **Unused imports, variables, or dead code** — remove them. (미사용 코드 방치 금지)
- [AVOID] **Suppressing warnings** without clear justification comment. (사유 없는 경고 억제 금지)
- [AVOID] **Copy-pasting large code blocks** — extract into reusable components. (대규모 복붙 금지)
- [AVOID] **Adding dependencies** without evaluating necessity and license. (무분별한 의존성 추가 금지)
- [AVOID] **TODO/FIXME in commits** unless tracked in an issue system. (이슈 추적 없는 TODO/FIXME 커밋 금지)

## 11. Source Code Analysis (소스 코드 분석)

> Apply when asked to analyze, review, or understand existing source code.
> 기존 소스 코드의 분석, 리뷰, 이해를 요청받았을 때 적용합니다.

### Analysis Approach (분석 접근법)

- [MUST] Follow **top-down analysis order**: (상위에서 하위 순서로 분석)
  ```
  1. Build/dependency files   - 기술 스택, 의존성, 모듈 구조 파악
     (build.gradle, pom.xml, package.json, requirements.txt, etc.)
  2. Project structure        - 디렉토리 구조, 모듈 간 관계 파악
  3. Configuration files      - 설정값, 프로파일, 환경변수 파악
     (application.yml, .env, config/, etc.)
  4. Entry points             - 앱 시작점, 라우터, 컨트롤러 파악
  5. Core business logic      - 서비스, 도메인 모델, 핵심 로직 파악
  6. Data access layer        - DB 스키마, 쿼리, ORM 매핑 파악
  7. Cross-cutting concerns   - 보안, 로깅, 예외 처리, AOP 파악
  ```
- [MUST] **Read actual files** before making any assertions about the code. (코드에 대한 주장 전 반드시 실제 파일 읽기)
- [MUST] **Verify assumptions** — do not assume file contents or behavior from names alone. (파일명만으로 내용·동작을 추정하지 말 것)
- [SHOULD] Start with `find` / `grep` / directory listing to understand project layout before diving into files. (파일 탐색 전 프로젝트 레이아웃부터 파악)

### Analysis Output (분석 결과물)

- [MUST] Present findings in a **structured format**. Recommended sections: (구조화된 형식으로 결과 제시)
  ```
  ## 분석 결과
  ### 1. 개요        - 프로젝트의 목적과 기술 스택 요약
  ### 2. 구조        - 모듈/패키지 구조와 의존 관계
  ### 3. 핵심 흐름   - 주요 비즈니스 로직의 데이터 흐름
  ### 4. 발견 사항   - 특이점, 잠재적 문제, 개선 가능 영역
  ### 5. 결론        - 종합 평가 및 권장 사항
  ```
- [MUST] **Cite specific files and line numbers** when referencing code. (코드 참조 시 파일 경로와 줄 번호 명시)
  ```
  # Good
  UserService.java:45 에서 트랜잭션 없이 다중 DB 작업 수행

  # Bad
  UserService에서 트랜잭션 문제가 있음
  ```
- [SHOULD] Use **diagrams or tables** for complex relationships. (복잡한 관계는 다이어그램·테이블 사용)

### What to Analyze (분석 대상)

- [MUST] **Dependency analysis**: Identify external libraries, versions, conflicts, deprecated or vulnerable libs. (의존성 분석)
- [MUST] **Architecture analysis**: Identify layers, module boundaries, dependency directions, circular deps. (아키텍처 분석)
- [SHOULD] **Code pattern analysis**: Identify design patterns, idioms, coding conventions in use. (코드 패턴 분석)
- [SHOULD] **Configuration analysis**: Check hardcoded values, missing env configs, insecure defaults. (설정 분석)
- [SHOULD] **Error handling analysis**: Verify consistent exception handling and proper logging. (에러 처리 분석)

### Analysis Principles (분석 원칙)

- [MUST] **Be objective** — report facts, separate observations from recommendations. (객관적: 사실 보고, 관찰과 권장 구분)
  ```
  # Good
  관찰: Service 12개 중 8개에서 @Transactional 누락
  권장: 데이터 변경 메서드에 @Transactional 적용 검토

  # Bad
  트랜잭션 관리가 엉망입니다
  ```
- [MUST] **Prioritize findings** by severity: (심각도 분류)
  - [CRITICAL] Security vulnerabilities, data loss risks, runtime crashes (보안 취약점, 데이터 손실, 런타임 충돌)
  - [WARNING] Performance issues, inconsistent patterns, missing validation (성능 이슈, 불일치 패턴, 검증 누락)
  - [INFO] Style improvements, minor refactoring opportunities (스타일 개선, 경미한 리팩터링 기회)
- [MUST] **Scope your analysis** — only analyze what is asked. Do not expand without asking. (요청 범위만 분석)
- [SHOULD] **Quantify when possible** — "12 files affected" not "many files". (가능하면 정량화)
- [AVOID] Making assumptions about code that has not been read. (읽지 않은 코드에 대한 추정 금지)
- [AVOID] Suggesting rewrites when code works correctly — focus on real problems. (정상 코드 재작성 제안 금지)

### Code Review Checklist (코드 리뷰 체크리스트)

> When asked to review code (PR review, code review), check the following:
> 코드 리뷰 요청 시 아래 항목을 점검합니다.

- [ ] **Correctness**: Does the code do what it's supposed to do? (정확성)
- [ ] **Edge cases**: Are null, empty, boundary, and error cases handled? (엣지 케이스)
- [ ] **Security**: Input validation, SQL injection, XSS, sensitive data exposure? (보안)
- [ ] **Performance**: Unbounded queries, N+1, unnecessary loops? (성능)
- [ ] **Error handling**: Proper exceptions, meaningful messages, no swallowed errors? (에러 처리)
- [ ] **Naming**: Clear and consistent with project conventions? (명명)
- [ ] **Tests**: Are new/changed features covered by tests? (테스트)
- [ ] **Dependencies**: Are new dependencies justified and compatible? (의존성)
