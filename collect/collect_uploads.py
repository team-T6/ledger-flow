"""collect 웹 업로드 수집기 — uploads/inbox/의 사용자 업로드 파일을 표준 거래 표로 만든다.

파일 유형별 처리 (단계 문서 collect.md "판단 기준"의 AI 판단/일반 코드 구분 그대로):
- 하나카드 정형 CSV (매핑 규칙 있는 서식): collect.py의 normalize 재사용 — 일반 코드
- 낯선 서식 CSV/TXT/XLSX: call-agent.py call_agent_convert_table — AI 판단
- 영수증·결제 문자 캡처 이미지 (PNG/JPG): call-agent.py call_agent_with_image — AI 판단

계약은 collect.run()과 동일 — collect/result.csv 작성 + {"out_path", "rows", "envelope"} 반환.
지휘(orchestrator/run-pipeline.py)가 웹 실행 시 upload_dir와 함께 부른다.
개별 파일의 AI 처리 실패는 전체를 중단하지 않고 envelope flags에 오류로 남긴다
(collect.md "못 할 때" — 인식 실패 건은 버리지 않는다).

사용법(단독 실행): python3 collect/collect_uploads.py 2026-07 [업로드 폴더]
"""

import csv
import importlib.util
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)
DEFAULT_UPLOAD_DIR = os.path.join(REPO_ROOT, "uploads", "inbox")

TEXT_EXTS = {".csv", ".txt"}
IMAGE_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

# 하나카드 정형 CSV 판별용 필수 헤더 (collect.py normalize가 읽는 컬럼)
HANA_HEADER = {"이용일자", "이용가맹점", "이용금액", "할부기간", "회차",
               "원금", "수수료", "이용혜택", "혜택금액"}

AI_WORKERS = 3  # 이미지·낯선 서식 병렬 호출 수 (개별 실패는 flags로 흡수)


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BASE_DIR, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collect = _load("collect_base", "collect.py")
call_agent = _load("collect_call_agent", "call-agent.py")


def read_text_any(path):
    """업로드 텍스트 파일 디코딩 — 카드사마다 인코딩이 다르다 (KB 명세서는 EUC-KR)."""
    raw = open(path, "rb").read()
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def is_hana_csv(text):
    for line in text.splitlines():
        line = line.strip()
        if line:
            return HANA_HEADER <= {col.strip() for col in line.split(",")}
    return False


def xlsx_to_text(path):
    """엑셀을 탭 구분 텍스트로 펼친다 — 낯선 서식 변환(AI 판단)의 입력으로 쓴다."""
    from openpyxl import load_workbook  # merge 단계와 같은 기존 의존성
    wb = load_workbook(path, read_only=True, data_only=True)
    lines = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                lines.append("\t".join(cells))
    wb.close()
    return "\n".join(lines)


def map_table_row(r):
    """call_agent_convert_table 출력 행 → 거래 표 스키마 v1 (transaction_id는 나중에 부여)."""
    income = r.get("수익") or 0
    expense = r.get("지출") or 0
    amount = income if income > 0 else -expense
    payer_hint = f"{r.get('결제자') or ''} {r.get('결제수단') or ''}"
    return {
        "transaction_id": "",
        "날짜": r.get("날짜") or "",
        "금액": amount,
        "결제처": r.get("결제처") or "",
        "카테고리": "",           # 가공(refine) 몫
        "비고": r.get("비고") or "",
        "결제수단": r.get("결제수단") or "",
        "결제구분": "법인결제" if "법인" in payer_hint else "개인결제",
        "원거래통화": "",         # 해외 원거래 정보는 원본 비고 보존으로 대신한다 — 판정은 검증 2 몫
        "원거래금액": "",
        "source_type": "card_excel",
        "collect_status": "확인됨" if r.get("확인됨") else "확인 필요",
        "구매항목": r.get("구매항목") or "",
    }


def map_receipt(r):
    """call_agent_with_image 출력 → 거래 표 스키마 v1 한 행."""
    amount = r.get("금액") or 0
    return {
        "transaction_id": "",
        "날짜": r.get("날짜") or "",
        "금액": -amount if amount > 0 else amount,  # 영수증 금액은 총 결제액 → 지출 음수
        "결제처": r.get("결제처") or "",
        "카테고리": "",
        "비고": "",
        "결제수단": r.get("결제수단") or "",
        "결제구분": "개인결제",   # 영수증만으로 법인 여부 판단 근거 없음 — 기본값
        "원거래통화": "",
        "원거래금액": "",
        "source_type": "receipt",
        "collect_status": "확인됨" if r.get("확인됨") else "확인 필요",
        "구매항목": r.get("구매항목") or "",
    }


def process_file(path):
    """파일 1건 처리 — (rows, error) 반환. AI 실패는 error 문자열로 흡수한다."""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    try:
        if ext in IMAGE_MEDIA_TYPES:
            data = call_agent.call_agent_with_image(open(path, "rb").read(), IMAGE_MEDIA_TYPES[ext])
            return [map_receipt(data)], None
        if ext == ".xlsx":
            text = xlsx_to_text(path)
        else:  # .csv / .txt
            text = read_text_any(path)
        if not text.strip():
            return [], f"{name}: 내용이 비어 있음"
        rows = call_agent.call_agent_convert_table(text)
        return [map_table_row(r) for r in rows], None
    except Exception as e:
        return [], f"{name}: 처리 실패 — {e}"


def assign_ids(rows, month):
    """하나카드 정규화가 이미 쓴 tx_{YYMMDD}_{NN} 순번을 이어서 나머지 행에 id를 부여한다.

    날짜를 못 읽은 행은 대상 월 기반 자리표 키(YYMM00)를 쓴다 — 행을 버리지 않는다.
    """
    day_seq = {}
    id_pat = re.compile(r"tx_(\d{6})_(\d+)")
    for r in rows:
        m = id_pat.fullmatch(r.get("transaction_id") or "")
        if m:
            day_seq[m.group(1)] = max(day_seq.get(m.group(1), 0), int(m.group(2)))
    for r in rows:
        if r.get("transaction_id"):
            continue
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", r.get("날짜") or "")
        key = f"{m.group(1)[2:]}{m.group(2)}{m.group(3)}" if m else month.replace("-", "")[2:] + "00"
        day_seq[key] = day_seq.get(key, 0) + 1
        r["transaction_id"] = f"tx_{key}_{day_seq[key]:02d}"


def build_envelope(rows, stats, errors):
    """interface-spec.md "단계 결과 보고" 규격 — 상태 미확인 행은 확인 필요, 파일 실패는 오류."""
    total = len(rows)
    message = (f"업로드 처리 — 카드사 파일 {stats['card']}건 · 이미지 {stats['image']}장"
               + (f" · 실패 {len(errors)}건" if errors else ""))
    if total == 0:
        return {"stage": "collect", "status": "empty", "output": "",
                "counts": {"total": 0, "ok": 0, "flagged": 0},
                "flags": [{"row": 0, "type": "오류", "reason": e} for e in errors],
                "message": message if errors else "수집 대상 없음"}
    row_flags = []
    for idx, r in enumerate(rows, start=1):
        if r["collect_status"] != "확인됨":
            core_missing = not r["날짜"] or not r["금액"] or not r["결제처"]
            row_flags.append({
                "row": idx,
                "type": "오류" if (r["source_type"] == "receipt" and core_missing) else "확인 필요",
                "reason": f"{r['결제처'] or '결제처 미상'} 거래 — 핵심 값 확인 필요",
            })
    # 파일 단위 실패는 flags에만 싣고 counts에는 넣지 않는다 (counts는 행 단위 — merge 관례와 동일)
    flags = [{"row": 0, "type": "오류", "reason": e} for e in errors] + row_flags
    return {
        "stage": "collect",
        "status": "ok" if not flags else "partial",
        "output": "collect/result.csv",
        "counts": {"total": total, "ok": total - len(row_flags), "flagged": len(row_flags)},
        "flags": flags,
        "message": message,
    }


def run(month, upload_dir=DEFAULT_UPLOAD_DIR):
    """파이프라인 진입점 — collect.run()과 동일 계약."""
    paths = sorted(e.path for e in os.scandir(upload_dir) if e.is_file()) if os.path.isdir(upload_dir) else []
    if not paths:
        return {"out_path": None, "rows": [], "envelope": build_envelope([], {"card": 0, "image": 0}, [])}

    hana_paths, ai_paths = [], []
    for path in paths:
        ext = os.path.splitext(path)[1].lower()
        if ext in TEXT_EXTS and is_hana_csv(read_text_any(path)):
            hana_paths.append(path)
        else:
            ai_paths.append(path)

    rows, errors = [], []
    if hana_paths:
        by_month = collect.normalize(hana_paths, default_month=month)
        rows += [r for month_rows in by_month.values() for r in month_rows]

    if ai_paths:
        with ThreadPoolExecutor(max_workers=AI_WORKERS) as pool:
            for file_rows, error in pool.map(process_file, ai_paths):
                rows += file_rows
                if error:
                    errors.append(error)

    assign_ids(rows, month)
    rows.sort(key=lambda r: (r["날짜"], r["transaction_id"]))

    out_path = os.path.join(BASE_DIR, "result.csv")
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=collect.OUT_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    stats = {"card": len(hana_paths) + sum(1 for p in ai_paths
                                           if os.path.splitext(p)[1].lower() not in IMAGE_MEDIA_TYPES),
             "image": sum(1 for p in ai_paths if os.path.splitext(p)[1].lower() in IMAGE_MEDIA_TYPES)}
    return {"out_path": out_path if rows else None, "rows": rows,
            "envelope": build_envelope(rows, stats, errors)}


if __name__ == "__main__":
    if len(sys.argv) < 2 or not re.fullmatch(r"\d{4}-\d{2}", sys.argv[1]):
        print("사용법: python3 collect/collect_uploads.py <대상 월 YYYY-MM> [업로드 폴더]")
        sys.exit(1)
    result = run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else DEFAULT_UPLOAD_DIR)
    env = result["envelope"]
    print(f"{env['status']}: {env['counts']['total']}건 — {env['message']}")
