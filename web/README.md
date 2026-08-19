# web — 데모 쇼케이스 (Ledger Flow · 월말결산 비서)

랜딩 + 데모 플로우(초대코드 → 월 선택 → 자료 넣기 → 결산 진행 → 샘플 결과)를 담은
정적 단일 페이지(`index.html`). 샘플 데이터 연출용이며 실제 결산 실행·업로드·다운로드는 하지 않는다.
테마 3종(장부 에디토리얼·클린 핀테크·딥 그린 다크)을 우측 상단 동그라미 버튼으로 전환한다.

## 데모 운용 (확정)

역할이 둘로 나뉜다 — PRD §2 확장(웹사이트) 항목 참고.

- **상시 데모 = GitHub Pages**: `demo` 브랜치에 `web/` 변경이 push되면 자동 배포
  (`.github/workflows/deploy-pages.yml`) → https://team-t6.github.io/ledger-flow/
  - 이 페이지(쇼케이스)는 `demo` 브랜치에서 관리한다. main에는 이 README만 있다
- **실행 시연 = 로컬 + Cloudflare quick tunnel** (진짜 파이프라인을 보여줄 때):
  1. `python3 orchestrator/server.py` 실행 (로컬 대시보드, 포트 8788)
  2. `cloudflared tunnel --url http://localhost:8788` — 출력되는 임시 URL을 참석자에게 공유
  3. 시연이 끝나면 터널 종료 (URL은 사라진다)

## 로컬에서 쇼케이스 확인

```
python3 -m http.server 8090 --directory web
```

http://localhost:8090 접속. 데모 초대코드는 `LEDGER`
(`index.html`에 하드코딩 — 정식 버전에서 서버 검증으로 대체 예정).

## 담당

- 지휘/웹: 김규은
