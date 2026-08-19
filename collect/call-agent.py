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
        "구매항목": {
            "type": "string",
            "description": "구매 물품 목록, 콤마로 구분. 영수증에 물품이 안 보이면 결제처 이름으로 "
                           "일반적인 구매 카테고리를 짧게 짐작해서 채운다 (예: 쿠팡이츠 → 음식 배달). "
                           "결제처로도 전혀 짐작할 수 없을 때만 빈 문자열. 단, 결제처가 결제대행사(PG사, "
                           "예: KG이니시스·NHN한국사이버결제·토스페이먼츠·KCP·KICC 등)나 '온라인쇼핑', 또는 쿠팡·G마켓처럼 취급 품목이 정해져 있지 않은 종합 오픈마켓(쿠팡이츠처럼 "
                           "업종 자체가 좁혀지는 특정 목적 서비스는 제외)이면 짐작하지 말고 빈 문자열로 둔다",
        },
        "확인됨": {
            "type": "boolean",
            "description": "날짜·금액·결제처를 실제로 영수증에서 읽었으면 true. 그 세 값 중 하나라도 "
                           "지어냈거나 못 읽었으면 false. 구매항목이 결제처 기반 추정이라는 이유만으로는 false로 낮추지 않는다. "
                           "단, 결제처가 결제대행사(PG사)나 '온라인쇼핑', 또는 쿠팡·G마켓처럼 취급 품목이 정해져 있지 "
                           "않은 종합 오픈마켓이면 날짜·금액을 읽었어도 false로 한다 — 무엇을 샀는지 원천적으로 알 수 없기 때문이다",
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
        "구매항목": {
            "type": "string",
            "description": "구매 물품 목록, 콤마로 구분. 원본에 물품 정보가 없으면 결제처 이름으로 "
                           "일반적인 구매 카테고리를 짧게 짐작해서 채운다 (예: 쿠팡이츠 → 음식 배달). "
                           "결제처로도 전혀 짐작할 수 없을 때만 빈 문자열 (이 추정 자체는 확인됨을 false로 낮추지 않는다). "
                           "단, 결제처가 결제대행사(PG사, 예: KG이니시스·NHN한국사이버결제·토스페이먼츠·KCP·KICC 등)나 "
                           "'온라인쇼핑'처럼 그 자체로 실제 가맹점을 특정하지 못하는 이름이면 짐작하지 말고 빈 문자열로 둔다",
        },
        "확인됨": {
            "type": "boolean",
            "description": "날짜·지출·결제처, 이 세 핵심 값을 실제로 원본에서 읽었으면 true. 그중 하나라도 "
                           "지어냈거나 못 읽었으면 false. 매핑 규칙이 없는 낯선 서식이라는 이유만으로, 또는 "
                           "결제자·구매항목처럼 보조 정보가 비었다는 이유만으로 false로 낮추지 않는다. 단, 결제처가 "
                           "결제대행사(PG사)나 '온라인쇼핑', 또는 쿠팡·G마켓처럼 취급 품목이 정해져 있지 않은 "
                           "종합 오픈마켓이면 날짜·지출을 읽었어도 false로 한다 — 무엇을 샀는지 원천적으로 알 수 없기 때문이다",
        },
    },
    "required": ["날짜", "지출", "수익", "결제처", "비고", "결제수단", "결제자", "구매항목", "확인됨"],
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
                            "날짜·금액·결제처는 읽을 수 없는 값을 지어내지 말고 비워둬. "
                            "구매 물품 목록이 영수증에 안 보이면 결제처 이름으로 일반적인 구매 카테고리를 "
                            "짧게 짐작해서 적어줘(예: 쿠팡이츠 → 음식 배달) — 결제처로도 전혀 짐작할 수 없을 때만 비워둬. "
                            "단, 결제처 자체가 결제대행사(PG사, 예: KG이니시스·NHN한국사이버결제·토스페이먼츠·KCP·KICC 등)나 "
                            "'온라인쇼핑', 또는 쿠팡·G마켓처럼 취급 품목이 정해져 있지 않은 종합 오픈마켓(쿠팡이츠처럼 "
                            "업종 자체가 좁혀지는 특정 목적 서비스는 제외)이면 짐작하지 말고 구매항목은 비워두고 "
                            "확인됨도 false로 표시해줘 — 무엇을 샀는지 원천적으로 알 수 없으니까.",
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

    client = _client()
    system_prompt = load_agent_instruction(AGENT_INSTRUCTION_PATH)
    instruction = (
        "아래는 카드사·엑셀 등에서 그대로 복사해 붙여넣은, 매핑 규칙이 없는 낯선 서식의 표다. "
        "각 줄을 표준 거래 표(날짜·지출·수익·결제처·비고·결제수단·결제자·구매항목) 한 행으로 변환해줘.\n"
        "날짜·지출·결제처는 원본에서 실제로 읽은 값만 채우고, 지어내지 말고 그 값을 읽지 못했을 때만 "
        "그 행의 확인됨을 false로 표시해 — 매핑 규칙이 없는 낯선 서식이라는 이유만으로, 또는 결제자·구매항목 "
        "같은 보조 정보가 비었다는 이유만으로 false로 낮추지 마.\n"
        "구매항목은 원본에 물품 정보가 없으면 결제처 이름으로 일반적인 구매 카테고리를 짧게 짐작해서 채워줘"
        "(예: 쿠팡이츠 → 음식 배달) — 결제처로도 전혀 짐작할 수 없을 때만 비워둬. "
        "단, 결제처 자체가 결제대행사(PG사, 예: KG이니시스·NHN한국사이버결제·토스페이먼츠·KCP·KICC 등)나 "
        "'온라인쇼핑', 또는 쿠팡·G마켓처럼 취급 품목이 정해져 있지 않은 종합 오픈마켓(쿠팡이츠처럼 업종 자체가 "
        "좁혀지는 특정 목적 서비스는 제외)이면 짐작하지 말고 그 행의 구매항목은 비워두고 "
        "확인됨도 false로 표시해줘 — 무엇을 샀는지 원천적으로 알 수 없으니까. "
        "결제자처럼 등록된 카드 목록이 없어 원천적으로 판단할 수 없는 값은 빈칸으로 두면 돼.\n\n"
        f"{raw_text}"
    )

    # max_tokens가 커서 스트리밍 필수 (SDK 장시간 요청 가드) — orchestrator/run-pipeline.py와 같은 이유.
    # 실제 카드사 표는 행이 많고 한글 비고까지 있어 16000 토큰으로는 응답이 중간에 잘려
    # (JSON 문자열 미종결) 파싱이 깨지는 사례가 있었다 — 32000으로 올린다.
    with client.messages.stream(
        model=MODEL,
        max_tokens=32000,
        system=system_prompt,
        messages=[{"role": "user", "content": instruction}],
        output_config={"format": {"type": "json_schema", "schema": TABLE_SCHEMA}},
    ) as stream:
        response = stream.get_final_message()

    text = next(block.text for block in response.content if block.type == "text")
    if response.stop_reason == "max_tokens":
        raise RuntimeError(f"모델 응답이 max_tokens(32000)에서 잘렸습니다 — 표를 나눠서 다시 시도해 주세요 (받은 글자 {len(text)}자)")
    return json.loads(text)["rows"]


if __name__ == "__main__":
    input_text = sys.stdin.read()
    result = call_agent(input_text)
    print(result)
    print(f"[이번 호출: 보낸 글자 {len(input_text)}자 · 받은 글자 {len(result)}자]")
