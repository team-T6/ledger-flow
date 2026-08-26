# web — 쇼케이스 + 로컬 실행 페이지 (Ledger Flow · 월말결산 비서)

랜딩 + 결산 플로우(초대코드 → 월 선택 → 자료 넣기 → 결산 진행 → 결과)를 담은
정적 단일 페이지(`index.html`). 테마는 장부 에디토리얼 한 가지로 고정되어 있다.

같은 파일이 **여는 방법에 따라 두 모드로 동작**한다:

| 모드 | 언제 | 동작 |
|---|---|---|
| 데모 (기본값) | GitHub Pages·`file://`·일반 정적 서버 | 하드코딩 샘플 연출 — 실제 실행·업로드·다운로드 없음 |
| 실제 (로컬) | `python3 orchestrator/server.py`로 서빙된 `http://localhost:8788` | 업로드한 파일로 실제 결산 파이프라인(Claude 에이전트 호출) 실행 |

모드 감지는 페이지 로드 시 `GET /health` 프로브 하나 — 실패하면 무조건 데모라서,
서버 없는 환경에서 데모가 깨질 일이 없다.

## 실제 모드로 쓰기 (로컬)

1. 리포 루트 `.env`에 `INVITE_CODE=...`(초대코드, 자유 값)를 둔다. 인증은 둘 중 하나 — `.env`에 `ANTHROPIC_API_KEY=...`를 넣거나, **claude CLI에 로그인**되어 있으면 키 없이도 CLI 세션으로 자동 실행된다
2. `python3 orchestrator/server.py` 실행 → http://localhost:8788 접속
3. 초대코드 입력(서버가 `.env`와 대조) → 월 선택 → 카드사 CSV·영수증 이미지 업로드 → 결산 시작
4. 진행 로그가 실시간으로 흐르고, 끝나면 결과 화면에서 PDF·엑셀을 받는다

업로드 파일은 `uploads/inbox/`에 놓인다 (gitignore 차단 — 커밋되지 않는다).
개발자용 지휘 대시보드는 같은 서버의 `/screen.html`에 그대로 있다.

## 데모 운용 (확정)

- **상시 데모 = GitHub Pages**: `demo` 브랜치에 `web/` 변경이 push되면 자동 배포
  (`.github/workflows/deploy-pages.yml`) → https://team-t6.github.io/ledger-flow/
- **실행 시연 = 로컬 + Cloudflare quick tunnel** (진짜 파이프라인을 보여줄 때):
  1. `python3 orchestrator/server.py` 실행 (포트 8788)
  2. `cloudflared tunnel --url http://localhost:8788` — 출력되는 임시 URL을 참석자에게 공유
  3. 시연이 끝나면 터널 종료 (URL은 사라진다)

## 로컬에서 데모(연출)만 확인

```
python3 -m http.server 8090 --directory web
```

http://localhost:8090 접속. 데모 초대코드는 `LEDGER` (데모 모드 한정 — 실제 모드는 `.env`의 `INVITE_CODE`로 서버 검증).

## 담당

- 지휘/웹: 김규은
