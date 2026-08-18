"""collect 담당자(Claude)를 실제로 불러주는 자리.

화면에서 넘어온 글 한 덩어리를 받아 .claude/agents/collect.md의 역할 지시문을
시스템 프롬프트로 그대로 넘기고, 처리 결과를 돌려준다.
"""

import base64
import json
import os
import sys
from pathlib import Path

import anthropic

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
ENV_PATH = REPO_ROOT / ".env"
AGENT_INSTRUCTION_PATH = REPO_ROOT / ".claude" / "agents" / "collect.md"

MODEL = "claude-opus-5"

# collect.md "하는 단계 3" (영수증 이미지에서 거래일·금액·결제처·결제수단·구매 물품 목록 추출)에
# 맞춘 구조화 출력 스키마.
RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "날짜": {"type": "string", "description": "영수증에 적힌 거래일 (YYYY-MM-DD). 읽을 수 없으면 빈 문자열"},
        "금액": {"type": "integer", "description": "총 결제 금액(원). 읽을 수 없으면 0"},
        "결제처": {"type": "string", "description": "가맹점명. 읽을 수 없으면 빈 문자열"},
        "결제수단": {"type": "string", "description": "카드/현금 등. 읽을 수 없으면 빈 문자열"},
        "구매항목": {"type": "string", "description": "구매 물품 목록, 콤마로 구분. 읽을 수 없으면 빈 문자열"},
        "확인됨": {"type": "boolean", "description": "위 값들을 실제로 영수증에서 읽었으면 true. 하나라도 지어냈거나 읽지 못했으면 false"},
    },
    "required": ["날짜", "금액", "결제처", "결제수단", "구매항목", "확인됨"],
    "additionalProperties": False,
}


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


def _client():
    load_env_file(ENV_PATH)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(f"ANTHROPIC_API_KEY가 없습니다 ({ENV_PATH})")
    return anthropic.Anthropic(api_key=api_key)


def call_agent(user_text):
    if not user_text or not user_text.strip():
        raise ValueError("넘길 글이 비어 있습니다")

    client = _client()
    system_prompt = load_agent_instruction(AGENT_INSTRUCTION_PATH)

    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_text}],
    )

    return "".join(block.text for block in response.content if block.type == "text")


def call_agent_with_image(image_bytes, media_type):
    if not image_bytes:
        raise ValueError("넘길 이미지가 없습니다")

    client = _client()
    system_prompt = load_agent_instruction(AGENT_INSTRUCTION_PATH)
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": image_b64},
                },
                {
                    "type": "text",
                    "text": "이 영수증 이미지에서 거래일·금액·결제처·결제수단·구매 물품 목록을 읽어줘. "
                            "읽을 수 없는 값은 지어내지 말고 비워둬.",
                },
            ],
        }],
        output_config={"format": {"type": "json_schema", "schema": RECEIPT_SCHEMA}},
    )

    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)


if __name__ == "__main__":
    input_text = sys.stdin.read()
    result = call_agent(input_text)
    print(result)
    print(f"[이번 호출: 보낸 글자 {len(input_text)}자 · 받은 글자 {len(result)}자]")
