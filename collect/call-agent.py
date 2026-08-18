"""collect 담당자(Claude)를 실제로 불러주는 자리.

화면에서 넘어온 글 한 덩어리를 받아 .claude/agents/collect.md의 역할 지시문을
시스템 프롬프트로 그대로 넘기고, 처리 결과를 돌려준다.
"""

import os
import sys
from pathlib import Path

import anthropic

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
ENV_PATH = REPO_ROOT / ".env"
AGENT_INSTRUCTION_PATH = REPO_ROOT / ".claude" / "agents" / "collect.md"

MODEL = "claude-opus-5"


def load_env_file(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def load_agent_instruction(path):
    text = path.read_text(encoding="utf-8")
    # .claude/agents/collect.md는 YAML frontmatter(---로 감싼 부분)로 시작한다 —
    # Claude Code 도구 설정용이라 API에 넘길 역할 지시문에서는 제외한다.
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def call_agent(user_text):
    if not user_text or not user_text.strip():
        raise ValueError("넘길 글이 비어 있습니다")

    load_env_file(ENV_PATH)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(f"ANTHROPIC_API_KEY가 없습니다 ({ENV_PATH})")

    system_prompt = load_agent_instruction(AGENT_INSTRUCTION_PATH)
    client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_text}],
    )

    return "".join(block.text for block in response.content if block.type == "text")


if __name__ == "__main__":
    input_text = sys.stdin.read()
    result = call_agent(input_text)
    print(result)
    print(f"[이번 호출: 보낸 글자 {len(input_text)}자 · 받은 글자 {len(result)}자]")
