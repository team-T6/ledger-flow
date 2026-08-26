"""orchestrator 담당자(Claude)를 실제로 불러 쓰는 자리.

.claude/agents/orchestrator.md에 적힌 역할 지시문을 시스템 프롬프트로 그대로 쓰고,
화면에서 넘어온 글 한 덩어리(단계 결과 보고 JSON 등)를 사용자 메시지로 보내 처리 결과를 받는다.
API 키는 팀 폴더 .env의 ANTHROPIC_API_KEY를 읽어 쓴다 (키 값은 이 파일에 적지 않는다).
인증 폴백: 키가 없으면 claude CLI 헤드리스(claude -p, CLI 로그인 세션)로 실행한다.

사용법: echo "<처리할 글>" | python3 orchestrator/call-agent.py
"""

import os
import re
import shutil
import subprocess
import sys

try:
    from anthropic import Anthropic
except ImportError:  # 키 없이 CLI 폴백만 쓰는 환경 — API 경로를 탈 때만 문제가 된다
    Anthropic = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)
ENV_PATH = os.path.join(REPO_ROOT, ".env")
AGENT_SPEC_PATH = os.path.join(REPO_ROOT, ".claude", "agents", "orchestrator.md")

MODEL = "claude-sonnet-5"
MAX_TOKENS = 4096


def load_api_key() -> str:
    if not os.path.exists(ENV_PATH):
        raise RuntimeError(f".env 파일이 없습니다: {ENV_PATH}")
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                value = line.split("=", 1)[1].strip()
                if not value:
                    raise RuntimeError("ANTHROPIC_API_KEY 값이 비어 있습니다 (.env 확인)")
                return value
    raise RuntimeError("ANTHROPIC_API_KEY를 .env에서 찾을 수 없습니다")


def load_role_instruction() -> str:
    with open(AGENT_SPEC_PATH, encoding="utf-8") as f:
        content = f.read()
    # frontmatter(--- ... ---)를 걷어내고 본문(역할 지시문)만 그대로 쓴다
    match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
    return content[match.end():].strip() if match else content.strip()


def _have_api_key() -> bool:
    try:
        load_api_key()
        return True
    except RuntimeError:
        return False


def call_orchestrator_agent(input_text: str) -> str:
    if Anthropic is None or not _have_api_key():
        # 인증 폴백 — claude CLI 로그인 세션으로 실행 (단계 문서 "도구·코드")
        if not shutil.which("claude"):
            raise RuntimeError("ANTHROPIC_API_KEY가 없고 claude CLI도 없습니다 — "
                               ".env에 키를 넣거나 claude CLI 로그인이 필요합니다")
        result = subprocess.run(
            ["claude", "-p", "--model", MODEL, "--append-system-prompt", load_role_instruction()],
            input=input_text, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:300]
            raise RuntimeError(f"claude CLI 실행 실패 (exit {result.returncode}): {detail}")
        return result.stdout
    client = Anthropic(api_key=load_api_key())
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=load_role_instruction(),
        messages=[{"role": "user", "content": input_text}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


if __name__ == "__main__":
    input_text = sys.stdin.read().strip()
    if not input_text:
        print("확인 대상 없음")
    else:
        print(call_orchestrator_agent(input_text))
