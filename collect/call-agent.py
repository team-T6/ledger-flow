"""collect 담당자(Claude)를 실제로 불러주는 자리.

화면에서 넘어온 글 한 덩어리를 받아 .claude/agents/collect.md의 역할 지시문을
시스템 프롬프트로 그대로 넘기고, 처리 결과를 돌려준다.

인증 폴백: .env에 ANTHROPIC_API_KEY가 없으면 claude CLI 헤드리스(claude -p,
CLI 로그인 세션)로 같은 프롬프트를 실행한다 — 구조화 출력 강제가 없으므로
JSON-only 지시를 얹고 코드로 파싱한다 (지휘 단계 문서 "도구·코드"의 인증 폴백과 동일).
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import anthropic
except ImportError:  # 키 없이 CLI 폴백만 쓰는 환경 — API 경로(_client)를 탈 때만 문제가 된다
    anthropic = None

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
ENV_PATH = REPO_ROOT / ".env"
AGENT_INSTRUCTION_PATH = REPO_ROOT / ".claude" / "agents" / "collect.md"

# 표 변환·영수증 판독은 서식 대조 위주의 기계적 추출이라 haiku로 충분하다
# (수집 단계 문서 "하는 단계" 확정 — 사용량 절감)
MODEL = "claude-haiku-4-5"

# collect.md "하는 단계 3" (영수증 이미지에서 거래일·금액·결제처·결제수단·구매 물품 목록 추출)에
# 맞춘 구조화 출력 스키마.
RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "날짜": {"type": "string", "description": "영수증에 적힌 거래일 (YYYY-MM-DD). 읽을 수 없으면 빈 문자열"},
        "금액": {"type": "integer", "description": "총 결제 금액(원). 읽을 수 없으면 0"},
        "결제처": {"type": "string", "description": "가맹점명. 읽을 수 없으면 빈 문자열"},
        "결제수단": {"type": "string", "description": "카드/현금 등. 읽을 수 없으면 빈 문자열"},
        "구매항목": {
            "type": "string",
            "description": "구매 물품 목록, 콤마로 구분. 영수증에 물품이 안 보이면 결제처 이름으로 "
                           "일반적인 구매 카테고리를 짧게 짐작해서 채운다 (예: 쿠팡이츠 → 음식 배달). "
                           "결제처로도 전혀 짐작할 수 없을 때만 빈 문자열",
        },
        "확인됨": {
            "type": "boolean",
            "description": "날짜·금액·결제처를 실제로 영수증에서 읽었으면 true. 그 세 값 중 하나라도 "
                           "지어냈거나 못 읽었으면 false. 구매항목이 결제처 기반 추정이라는 이유만으로는 false로 낮추지 않는다",
        },
    },
    "required": ["날짜", "금액", "결제처", "결제수단", "구매항목", "확인됨"],
    "additionalProperties": False,
}

# 표준 산출물 컬럼(날짜·지출·수익·결제처·비고·결제수단·결제자·구매항목)에 맞춘
# 표 변환용 구조화 출력 스키마 — 낯선 서식으로 붙여넣은 표를 줄 단위로 변환할 때 쓴다.
TABLE_ROW_SCHEMA = {
    "type": "object",
    "properties": {
        "날짜": {"type": "string", "description": "YYYY-MM-DD. 알 수 없으면 빈 문자열"},
        "지출": {"type": "integer", "description": "지출 금액(원). 알 수 없으면 0"},
        "수익": {"type": "integer", "description": "수익 금액(원). 없으면 0"},
        "결제처": {"type": "string", "description": "가맹점/거래처명. 알 수 없으면 빈 문자열"},
        "비고": {"type": "string", "description": "메모. 없으면 빈 문자열"},
        "결제수단": {"type": "string", "description": "카드/현금 등. 알 수 없으면 빈 문자열"},
        "결제자": {
            "type": "string",
            "description": "개인카드/법인카드 등. 등록된 카드 목록이 없어 원천적으로 판단할 수 없으면 빈 문자열 "
                           "(이 값이 비어도 확인됨을 false로 낮추지 않는다)",
        },
        "원거래통화": {
            "type": "string",
            "description": "해외결제면 원거래 통화 코드(예: USD) — 원본의 통화 컬럼이나 "
                           "가맹점명·메모의 '29.99 USD' 같은 표기에서 실제로 읽은 값만. 국내 결제는 빈 문자열",
        },
        "원거래금액": {
            "type": "number",
            "description": "해외결제면 원거래 통화 기준 금액(예: 39.99). 국내 결제는 0",
        },
        "구매항목": {
            "type": "string",
            "description": "구매 물품 목록, 콤마로 구분. 원본에 물품 정보가 없으면 결제처 이름으로 "
                           "일반적인 구매 카테고리를 짧게 짐작해서 채운다 (예: 쿠팡이츠 → 음식 배달). "
                           "결제처로도 전혀 짐작할 수 없을 때만 빈 문자열 (이 추정 자체는 확인됨을 false로 낮추지 않는다)",
        },
        "확인됨": {
            "type": "boolean",
            "description": "날짜·지출·결제처, 이 세 핵심 값을 실제로 원본에서 읽었으면 true. 그중 하나라도 "
                           "지어냈거나 못 읽었으면 false. 매핑 규칙이 없는 낯선 서식이라는 이유만으로, 또는 "
                           "결제자·구매항목처럼 보조 정보가 비었다는 이유만으로 false로 낮추지 않는다",
        },
    },
    "required": ["날짜", "지출", "수익", "결제처", "비고", "결제수단", "결제자",
                 "원거래통화", "원거래금액", "구매항목", "확인됨"],
    "additionalProperties": False,
}

TABLE_SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {"type": "array", "items": TABLE_ROW_SCHEMA},
    },
    "required": ["rows"],
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
    if anthropic is None:
        raise RuntimeError("anthropic 패키지가 설치되어 있지 않습니다 (pip install anthropic)")
    load_env_file(ENV_PATH)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(f"ANTHROPIC_API_KEY가 없습니다 ({ENV_PATH})")
    return anthropic.Anthropic(api_key=api_key)


# ---------- claude CLI 인증 폴백 (지휘 단계 문서 "도구·코드"의 인증 폴백과 동일 방식) ----------

CLI_TIMEOUT_SECONDS = 600  # 호출 1건 상한 — CLI가 매달리는 것 방지

# CLI 폴백 작업 디렉터리 — repo 안에서 실행하면 프로젝트 지침 파일(CLAUDE.md·AGENTS*.md)이
# 호출마다 통째로 주입되어 사용량을 낭비한다 (지휘 단계 문서 "도구·코드" 확정과 동일)
CLI_WORKDIR = tempfile.gettempdir()


def _have_api_key():
    load_env_file(ENV_PATH)
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _json_only(schema):
    return ("\n\n출력 형식(반드시 지켜라): 설명·인사·코드펜스 없이, 아래 JSON 스키마를 따르는 "
            "단일 JSON 객체만 출력한다.\n" + json.dumps(schema, ensure_ascii=False))


def _extract_json(text):
    """CLI 출력에서 JSON 본문만 추린다 — 코드펜스나 앞뒤 설명이 섞여도 복원한다."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def _call_cli(user_text, allowed_tools=None):
    """claude CLI 헤드리스 호출 — API 키 없이 CLI 로그인 세션으로 실행하는 인증 폴백."""
    if not shutil.which("claude"):
        raise RuntimeError("ANTHROPIC_API_KEY가 없고 claude CLI도 없습니다 — "
                           ".env에 키를 넣거나 claude CLI 로그인이 필요합니다")
    cmd = ["claude", "-p", "--model", MODEL,
           "--append-system-prompt", load_agent_instruction(AGENT_INSTRUCTION_PATH)]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    result = subprocess.run(cmd, input=user_text, capture_output=True, text=True,
                            timeout=CLI_TIMEOUT_SECONDS, cwd=CLI_WORKDIR)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise RuntimeError(f"claude CLI 실행 실패 (exit {result.returncode}): {detail}")
    return result.stdout


def call_agent(user_text):
    if not user_text or not user_text.strip():
        raise ValueError("넘길 글이 비어 있습니다")

    if not _have_api_key():
        return _call_cli(user_text)

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

    if not _have_api_key():
        # CLI는 base64 이미지 첨부가 없으므로 임시 파일로 두고 Read 도구로 읽게 한다
        suffix = ".jpg" if "jpeg" in (media_type or "") else ".png"
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            tmp.write(image_bytes)
            tmp.close()
            prompt = (
                f"Read 도구로 이미지 파일 {tmp.name} 을 읽어라. "
                "이 영수증 이미지에서 거래일·금액·결제처·결제수단·구매 물품 목록을 읽어줘. "
                "날짜·금액·결제처는 읽을 수 없는 값을 지어내지 말고 비워둬. "
                "구매 물품 목록이 영수증에 안 보이면 결제처 이름으로 일반적인 구매 카테고리를 "
                "짧게 짐작해서 적어줘(예: 쿠팡이츠 → 음식 배달) — 결제처로도 전혀 짐작할 수 없을 때만 비워둬."
                + _json_only(RECEIPT_SCHEMA)
            )
            text = _call_cli(prompt, allowed_tools="Read")
            return json.loads(_extract_json(text))
        finally:
            os.unlink(tmp.name)

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
                            "날짜·금액·결제처는 읽을 수 없는 값을 지어내지 말고 비워둬. "
                            "구매 물품 목록이 영수증에 안 보이면 결제처 이름으로 일반적인 구매 카테고리를 "
                            "짧게 짐작해서 적어줘(예: 쿠팡이츠 → 음식 배달) — 결제처로도 전혀 짐작할 수 없을 때만 비워둬.",
                },
            ],
        }],
        output_config={"format": {"type": "json_schema", "schema": RECEIPT_SCHEMA}},
    )

    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)


def call_agent_convert_table(raw_text):
    if not raw_text or not raw_text.strip():
        raise ValueError("넘길 표 내용이 없습니다")

    instruction = (
        "아래는 카드사·엑셀 등에서 그대로 복사해 붙여넣은, 매핑 규칙이 없는 낯선 서식의 표다. "
        "각 줄을 표준 거래 표(날짜·지출·수익·결제처·비고·결제수단·결제자·구매항목) 한 행으로 변환해줘.\n"
        "날짜·지출·결제처는 원본에서 실제로 읽은 값만 채우고, 지어내지 말고 그 값을 읽지 못했을 때만 "
        "그 행의 확인됨을 false로 표시해 — 매핑 규칙이 없는 낯선 서식이라는 이유만으로, 또는 결제자·구매항목 "
        "같은 보조 정보가 비었다는 이유만으로 false로 낮추지 마.\n"
        "구매항목은 원본에 물품 정보가 없으면 결제처 이름으로 일반적인 구매 카테고리를 짧게 짐작해서 채워줘"
        "(예: 쿠팡이츠 → 음식 배달) — 결제처로도 전혀 짐작할 수 없을 때만 비워둬. "
        "결제자처럼 등록된 카드 목록이 없어 원천적으로 판단할 수 없는 값은 빈칸으로 두면 돼.\n"
        "해외결제면 원거래통화(통화 코드)와 원거래금액을 채워줘 — 원본의 통화·해외이용금액 컬럼이나 "
        "가맹점명·메모의 '29.99 USD' 같은 표기에서 실제로 읽은 값만. 국내 결제는 원거래통화를 빈칸, "
        "원거래금액을 0으로 둬.\n\n"
        f"{raw_text}"
    )

    if not _have_api_key():
        text = _call_cli(instruction + _json_only(TABLE_SCHEMA))
        return json.loads(_extract_json(text))["rows"]

    client = _client()
    system_prompt = load_agent_instruction(AGENT_INSTRUCTION_PATH)

    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": instruction}],
        output_config={"format": {"type": "json_schema", "schema": TABLE_SCHEMA}},
    )

    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)["rows"]


if __name__ == "__main__":
    input_text = sys.stdin.read()
    result = call_agent(input_text)
    print(result)
    print(f"[이번 호출: 보낸 글자 {len(input_text)}자 · 받은 글자 {len(result)}자]")
