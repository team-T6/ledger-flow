# ledger-flow

**월말결산 에이전트 — 다중 카드 통합 AI 자동 결산 시스템** · team-T6

개인·법인 명의가 섞인 여러 카드의 결제 내역(카드사 엑셀, 영수증 사진, 결제 문자 캡처)을 모아,
멀티 에이전트 파이프라인이 월 결산 산출물을 자동 생성합니다. 사람의 몫은 "확인 필요" 건 처리만 남깁니다.

🔗 **상시 데모(쇼케이스)**: https://team-t6.github.io/ledger-flow/

## 무엇을 해결하나

여러 카드를 쓰는 순간 지출 데이터는 원천별로 파편화되고, 월 결산은 매달 같은 순서의 수작업으로 반복됩니다.
ledger-flow는 이 반복 결산을 아래 파이프라인으로 자동화합니다.

```
수집(collect) → 가공(refine) → 검증 1·2(verify1 ∥ verify2, 병렬) → 통합(merge)
                        ↑ 오케스트레이터(orchestrator)가 순서 지휘
```

| 단계 | 역할 |
| --- | --- |
| 수집 `collect` | 카드사 엑셀 파싱, 영수증·문자 이미지 OCR → 표준 거래 표(CSV) |
| 가공 `refine` | 결제처 정규화(PG사 → 실가맹점명), 카테고리 분류 |
| 검증 1 `verify1` | 카테고리 분류가 정의된 체계([categories.md](docs/categories.md))에 맞는지 검증 |
| 검증 2 `verify2` | 대상 월 포함 여부, 금액 부호 규칙, 해외결제 원화 환산 검증 |
| 통합 `merge` | 검증 통과 데이터로 최종 산출물 생성, 반려 건은 "확인 필요" 목록으로 보고 |
| 지휘 `orchestrator` | 단계 순차 호출, 실패 처리, 최종 마무리 |

검증 1·2는 직렬 2단계가 아니라 **하나의 검증 스테이지 안에서 병렬 실행**되며,
반려 건은 재시도 없이 리포트의 "확인 필요" 목록으로 넘어갑니다.

### 최종 산출물

- **엑셀 회계장부** — 정제·분류 완료된 전체 거래 내역 (카드/명의별 구분, 카테고리 부여)
- **PDF 결산 리포트** — 월 단위 요약 (카테고리별 지출 금액·비중 등)

## 저장소 구조

```
collect/ refine/ verify1/ verify2/ merge/ orchestrator/   # 단계별 "칸 폴더" (담당자별 1칸)
├── README.md        # 그 칸의 약속 + 담당·테스트·협의 메모
├── input-sample.md  # 받는 재료 견본
├── stub.md          # 산출물 모양 견본
└── result.*         # 진짜 산출물 (실행 시 생성)

.claude/agents/<단계>.md   # 단계 문서 — 설계 + 에이전트 실행 지시의 단일 정본
docs/                      # PRD · 인터페이스 정의서 · 카테고리 · 온보딩
sample_data/               # 테스트용 가짜 데이터 (커밋 가능)
web/                       # 데모 쇼케이스 (GitHub Pages, demo 브랜치에서 관리)
logs/                      # 실행 로그 (커밋 차단)
```

## 빠른 시작 — 로컬에서 실제 서비스 띄우기

`web/index.html`은 여는 방법에 따라 두 모드로 동작합니다.
GitHub Pages·정적 서버로 열면 **데모(연출) 모드**, `orchestrator/server.py`로 서빙하면 **실제 실행 모드**입니다.

### 1. 의존성 설치

Python 3 환경에서 다음 패키지를 설치합니다 (별도 requirements 파일 없음 — 현재 코드 기준):

macOS / Linux:

```bash
pip install anthropic openpyxl reportlab
```

Windows:

```bash
py -m pip install anthropic openpyxl reportlab
```

### 2. `.env` 작성

repo 루트에 `.env` 파일을 만들고 두 값을 넣습니다 (`.gitignore`로 커밋 차단됨):

```
ANTHROPIC_API_KEY=sk-ant-...
INVITE_CODE=원하는초대코드
```

### 3. 서버 실행

macOS / Linux:

```bash
python3 orchestrator/server.py
```

Windows:

```bash
py orchestrator\server.py
```

http://localhost:8788 접속 → 초대코드 입력(`.env`의 `INVITE_CODE`와 대조) → 월 선택 →
카드사 CSV·영수증 이미지 업로드(`uploads/inbox/`에 저장) → 결산 시작.
진행 로그가 실시간으로 흐르고, 완료되면 결과 화면에서 **PDF 리포트·엑셀 장부**를 내려받습니다.

같은 서버의 http://localhost:8788/screen.html 에서 개발자용 지휘 대시보드(연동/가공 현황/리포트)를 쓸 수 있습니다 — 이쪽은 `sample_data/`를 수집 원천으로 실행합니다.

### 다른 실행 방법

- **CLI로 파이프라인만**: `python3 orchestrator/run-pipeline.py <대상 월 YYYY-MM>` (Windows: `py orchestrator\run-pipeline.py <대상 월 YYYY-MM>`) — 실행 기록은 `logs/run_*/`, 최종 요약은 `orchestrator/result-summary.md`
- **데모(연출)만 로컬 확인**: `python3 -m http.server 8090 --directory web` (Windows: `py -m http.server 8090 --directory web`) → http://localhost:8090 (데모 초대코드 `LEDGER`, 실제 실행 없음)
- **외부 시연**: 로컬 서버 + `cloudflared tunnel --url http://localhost:8788` — 절차는 [web/README.md](web/README.md) 참고

개발·시연은 `sample_data/`의 가짜 데이터로만 합니다.

## 문서

처음 오신 분은 **[온보딩 가이드](docs/onboarding.md)** 부터 읽어주세요.

| 문서 | 내용 |
| --- | --- |
| [PRD](docs/PRD.md) | 문제 정의 · 범위 · 역할 분담 |
| [인터페이스 정의서](docs/interface-spec.md) | 단계 간 계약 · 거래 표 스키마 · 산출물 양식 |
| [카테고리 체계](docs/categories.md) | 회계 계정과목 기반 분류 기준 (확정) |
| `.claude/agents/<단계>.md` | 단계별 설계 + 실행 지시 (단일 정본) |
| [AGENTS.md](AGENTS.md) · [AGENTS_COM.md](AGENTS_COM.md) | AI 에이전트 작업 지침 (프로젝트 전용 · 공통) |

## 협업 규칙 요약

- `main`(정본) · `demo`(Pages 배포) · `production`(실서버) 세 브랜치는 상시 유지
- 기능 개발은 feature 브랜치에서 작업 후 PR로 병합 (main 직접 커밋 금지)
- **카드 실거래 데이터·credential은 절대 커밋하지 않습니다** — 샘플은 `sample_data/`만 허용

## License

[LICENSE](LICENSE) 참고
