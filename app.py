import base64
import html
import io
import json
import os
import re
import socket
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

# =========================================================
# NVIDIA API 기본 연결 정보
# PC 버전: 같은 폴더의 .env 파일에서 NVIDIA_API_KEY=YOUR_API_KEY 를 바꾸세요.
# 핸드폰/클라우드 버전: Streamlit Cloud의 Secrets에 NVIDIA_API_KEY를 넣으세요.
# 그래도 코드에 직접 넣고 싶다면 아래 기본값 YOUR_API_KEY를 실제 키로 바꾸면 됩니다.
# =========================================================
CONFIG_SECRET_ALIASES = {
    "NVIDIA_API_KEY": [
        ("NVIDIA_API_KEY",),
        ("nvidia", "api_key"),
        ("nvidia", "NVIDIA_API_KEY"),
    ],
    "NVIDIA_BASE_URL": [
        ("NVIDIA_BASE_URL",),
        ("nvidia", "base_url"),
        ("nvidia", "NVIDIA_BASE_URL"),
    ],
    "NVIDIA_MODEL": [
        ("NVIDIA_MODEL",),
        ("nvidia", "model"),
        ("nvidia", "NVIDIA_MODEL"),
    ],
    "NVIDIA_MAX_TOKENS": [
        ("NVIDIA_MAX_TOKENS",),
        ("nvidia", "max_tokens"),
        ("nvidia", "NVIDIA_MAX_TOKENS"),
    ],
    "NVIDIA_TIMEOUT_SECONDS": [
        ("NVIDIA_TIMEOUT_SECONDS",),
        ("nvidia", "timeout_seconds"),
        ("nvidia", "NVIDIA_TIMEOUT_SECONDS"),
    ],
}


def _read_streamlit_secret_path(path: tuple) -> Optional[str]:
    """Streamlit Cloud Secrets 값을 안전하게 읽습니다.

    flat 방식 예: NVIDIA_API_KEY = "nvapi-..."
    nested 방식 예: [nvidia] api_key = "nvapi-..."
    둘 다 지원합니다.
    """
    try:
        current: Any = st.secrets
        for key in path:
            if hasattr(current, "get"):
                current = current.get(key)
            else:
                current = current[key]
            if current is None:
                return None
        value = str(current).strip()
        return value or None
    except Exception:
        return None


def get_streamlit_secret_value(name: str) -> Optional[str]:
    for path in CONFIG_SECRET_ALIASES.get(name, [(name,)]):
        value = _read_streamlit_secret_path(path)
        if value:
            return value
    return None


def get_config_value_and_source(name: str, default: str) -> Tuple[str, str]:
    """Streamlit Cloud Secrets를 우선하고, 없으면 로컬 .env/환경변수를 사용합니다."""
    secret_value = get_streamlit_secret_value(name)
    if secret_value:
        return secret_value, "secrets"

    env_value = os.getenv(name)
    if env_value and str(env_value).strip():
        return str(env_value).strip(), "env"

    return default, "default"


def get_config_value(name: str, default: str) -> str:
    value, _source = get_config_value_and_source(name, default)
    return value


def get_int_config_value(name: str, default: int, min_value: int, max_value: int) -> int:
    """환경변수/Secrets에 들어간 숫자 설정을 안전하게 읽습니다."""
    raw = get_config_value(name, str(default))
    try:
        value = int(str(raw).strip())
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


DEFAULT_NVIDIA_API_KEY, DEFAULT_NVIDIA_API_KEY_SOURCE = get_config_value_and_source("NVIDIA_API_KEY", "YOUR_API_KEY")
DEFAULT_BASE_URL = get_config_value("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
DEFAULT_MODEL = get_config_value("NVIDIA_MODEL", "minimaxai/minimax-m3")
# MiniMax-M3는 max_tokens를 너무 크게 잡으면 무료 엔드포인트에서 응답이 늦어질 수 있습니다.
# 8192 대신 2500을 기본값으로 낮춰 타임아웃 가능성을 줄입니다.
DEFAULT_MAX_TOKENS = get_int_config_value("NVIDIA_MAX_TOKENS", 2500, 800, 4096)
DEFAULT_TIMEOUT_SECONDS = get_int_config_value("NVIDIA_TIMEOUT_SECONDS", 240, 60, 300)

APP_TITLE = "네이버 블로그 자동 작성/미리보기 도우미 - NVIDIA API + 네이버 자동입력"
DEFAULT_NAVER_WRITE_URL = "https://blog.naver.com/GoBlogWrite.naver"

REVIEW_TYPES = [
    "선택 안 함",
    "맛집",
    "카페",
    "제품",
    "숙소",
    "여행지",
    "병원/시술",
    "뷰티",
    "전시/공연",
    "강의/교육",
    "체험단",
    "기타",
]

TONE_OPTIONS = [
    "자연스러운 일상체",
    "깔끔한 존댓말",
    "정보형",
    "감성형",
    "솔직 후기형",
    "친근한 반말체",
]

DISCLOSURES = ["선택 안 함", "내돈내산", "제품 제공", "체험단", "원고료 있음", "광고/협찬"]
IMAGE_EXTENSIONS = ["png", "jpg", "jpeg", "webp"]
VIDEO_EXTENSIONS = ["mp4", "mov", "m4v", "webm", "avi", "mkv"]


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def has_text(value: Any) -> bool:
    return bool(clean_text(value))


def split_items(text: str) -> List[str]:
    text = clean_text(text)
    if not text:
        return []
    text = text.replace("ㆍ", ",").replace("·", ",").replace("/", ",")
    parts = []
    for chunk in re.split(r"[\n,]+", text):
        chunk = chunk.strip(" -•\t")
        if chunk:
            parts.append(chunk)
    return parts


def compact_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """빈 값은 제거해서 AI가 없는 정보를 억지로 만들지 않게 합니다."""
    result: Dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            nested = compact_dict(value)
            if nested:
                result[key] = nested
        elif isinstance(value, list):
            new_items = []
            for item in value:
                if isinstance(item, dict):
                    nested_item = compact_dict(item)
                    if nested_item:
                        new_items.append(nested_item)
                elif has_text(item):
                    new_items.append(clean_text(item))
            if new_items:
                result[key] = new_items
        elif isinstance(value, bool):
            result[key] = value
        elif has_text(value) and clean_text(value) not in ["선택 안 함", "없음"]:
            result[key] = clean_text(value)
    return result


def first_non_empty(*values: str, fallback: str = "리뷰 대상") -> str:
    for value in values:
        value = clean_text(value)
        if value and value != "선택 안 함":
            return value
    return fallback


def remove_hashtag_symbols(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_]+", "", text.replace("#", "")).strip()


def make_tag(text: str) -> str:
    tag = remove_hashtag_symbols(text)
    return f"#{tag}" if tag else ""


def make_safe_filename(title: str, fallback: str = "naver_blog_draft") -> str:
    title = clean_text(title)
    stem = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", title).strip("_")
    return (stem[:60] or fallback)


def safe_original_filename(name: str, fallback: str) -> str:
    name = Path(name or fallback).name
    name = re.sub(r"[^0-9A-Za-z가-힣_. -]+", "_", name).strip(" ._")
    return name or fallback


def unique_filename(name: str, used: set) -> str:
    base = Path(name).stem
    suffix = Path(name).suffix
    candidate = name
    i = 2
    while candidate in used:
        candidate = f"{base}_{i}{suffix}"
        i += 1
    used.add(candidate)
    return candidate


def markdown_title(markdown_text: str) -> str:
    for line in markdown_text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return re.sub(r"^#+\s*", "", line).strip()
    return "네이버 블로그 초안"


def image_to_data_url(uploaded_file, max_side: int = 1200, quality: int = 82) -> Optional[str]:
    try:
        try:
            uploaded_file.seek(0)
        except Exception:
            pass
        image = Image.open(uploaded_file)
        image = image.convert("RGB")
        image.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        return None


def copy_button(label: str, text: str, key: str) -> None:
    safe_text = json.dumps(text, ensure_ascii=False)
    safe_label = html.escape(label)
    safe_key = re.sub(r"[^0-9A-Za-z_]", "_", key)
    component = f"""
    <button id="{safe_key}" style="padding:10px 14px;border-radius:8px;border:1px solid #ddd;background:#fff;cursor:pointer;font-weight:700;">
      {safe_label}
    </button>
    <span id="{safe_key}_msg" style="margin-left:10px;color:#666;font-size:14px;"></span>
    <script>
    const btn_{safe_key} = document.getElementById('{safe_key}');
    const msg_{safe_key} = document.getElementById('{safe_key}_msg');
    btn_{safe_key}.onclick = async () => {{
      try {{
        await navigator.clipboard.writeText({safe_text});
        msg_{safe_key}.innerText = '복사 완료';
      }} catch (err) {{
        msg_{safe_key}.innerText = '복사가 안 되면 아래 텍스트 영역에서 직접 복사하세요.';
      }}
    }};
    </script>
    """
    st.components.v1.html(component, height=50)


def get_local_ip() -> str:
    """핸드폰 접속 안내용 내부 IP를 추정합니다."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "내_PC_IP주소"


# =========================================================
# 자동 임시저장: 새로고침해도 입력칸 값이 최대한 유지되게 합니다.
# 사진/동영상 파일 자체는 브라우저 보안 정책상 새로고침 후 다시 선택해야 할 수 있습니다.
# =========================================================
AUTOSAVE_DIR = Path.home() / ".naver_blog_writer"
AUTOSAVE_FILE = AUTOSAVE_DIR / "autosave.json"


def load_autosave_file() -> Dict[str, Any]:
    try:
        if AUTOSAVE_FILE.exists():
            data = json.loads(AUTOSAVE_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def write_autosave_file(data: Dict[str, Any]) -> None:
    try:
        AUTOSAVE_DIR.mkdir(parents=True, exist_ok=True)
        AUTOSAVE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def clear_autosave_file() -> None:
    try:
        if AUTOSAVE_FILE.exists():
            AUTOSAVE_FILE.unlink()
    except Exception:
        pass


def autosave_value(name: str, default: str = "") -> str:
    fields = st.session_state.get("autosave_fields", {})
    value = fields.get(name, default)
    return "" if value is None else str(value)


def autosave_index(name: str, options: List[str], default: str) -> int:
    fields = st.session_state.get("autosave_fields", {})
    value = fields.get(name, default)
    if value in options:
        return options.index(value)
    if default in options:
        return options.index(default)
    return 0


def save_autosave_snapshot(fields: Dict[str, Any], payload: Dict[str, Any]) -> None:
    # API 키는 보안상 자동 임시저장 파일에 저장하지 않습니다. .env 또는 Streamlit Secrets에 보관하세요.
    snapshot = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "fields": fields,
        "last_payload": payload,
        "generated_text": st.session_state.get("generated_text", ""),
        "edited_text": st.session_state.get("edited_text", ""),
        "selected_title": st.session_state.get("selected_title", ""),
        "prompt_text": st.session_state.get("prompt_text", ""),
        "external_ai_text": st.session_state.get("external_ai_text", ""),
        "drafts": st.session_state.get("drafts", []),
    }
    write_autosave_file(snapshot)
    st.session_state["autosave_last_saved_at"] = snapshot["saved_at"]




# =========================================================
# 블로그 템플릿 저장/불러오기
# 사용자가 만든 글 구조를 여러 개 저장하고, 블로그 링크를 구조 참고자료로 분석합니다.
# =========================================================
TEMPLATE_FILE = AUTOSAVE_DIR / "blog_templates.json"


def sanitize_template_name(name: str) -> str:
    name = clean_text(name)
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name).strip()
    return name[:60]


def load_blog_templates() -> Dict[str, Dict[str, Any]]:
    try:
        if TEMPLATE_FILE.exists():
            data = json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # 이전 형식/깨진 형식이 섞여도 안전하게 dict만 사용합니다.
                return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        return {}
    return {}


def write_blog_templates(templates: Dict[str, Dict[str, Any]]) -> None:
    try:
        AUTOSAVE_DIR.mkdir(parents=True, exist_ok=True)
        TEMPLATE_FILE.write_text(json.dumps(templates, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def save_blog_template(name: str, template_data: Dict[str, Any]) -> Tuple[bool, str]:
    name = sanitize_template_name(name)
    if not name:
        return False, "템플릿 이름을 입력해주세요."
    templates = load_blog_templates()
    template_data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    templates[name] = template_data
    write_blog_templates(templates)
    return True, f"'{name}' 템플릿을 저장했습니다."


def delete_blog_template(name: str) -> Tuple[bool, str]:
    name = sanitize_template_name(name)
    templates = load_blog_templates()
    if name in templates:
        templates.pop(name)
        write_blog_templates(templates)
        return True, f"'{name}' 템플릿을 삭제했습니다."
    return False, "삭제할 템플릿을 찾지 못했습니다."


def summarize_template_data(template_data: Dict[str, Any]) -> str:
    if not template_data:
        return ""
    parts = []
    for key in ["title_rule", "opening_rule", "section_structure", "photo_video_rule", "closing_rule", "hashtag_rule", "avoid_rule", "memo"]:
        value = clean_text(template_data.get(key, ""))
        if value:
            label = {
                "title_rule": "제목 규칙",
                "opening_rule": "도입부 규칙",
                "section_structure": "본문 구조",
                "photo_video_rule": "사진/동영상 배치",
                "closing_rule": "마무리 규칙",
                "hashtag_rule": "해시태그 규칙",
                "avoid_rule": "피할 표현",
                "memo": "메모",
            }.get(key, key)
            parts.append(f"- {label}: {value}")
    return "\n".join(parts)


def _looks_like_blog_heading(line: str) -> bool:
    line = clean_text(line)
    if not (3 <= len(line) <= 55):
        return False
    bad = [
        "로그인", "댓글", "공감", "이웃", "블로그", "카페", "메뉴", "본문", "공유", "신고", "검색",
        "naver", "copyright", "자세히 보기", "레이어 닫기", "입력 내용 삭제", "아이디", "변경", "개월간",
        "설정", "확인", "취소", "새로운 주소", "연결", "지원", "스마트에디터", "도움말",
    ]
    if any(x.lower() in line.lower() for x in bad):
        return False
    if re.search(r"[{}<>]", line):
        return False
    if re.match(r"^(https?://|www\.)", line.lower()):
        return False
    if re.fullmatch(r"[\W_]+", line):
        return False
    # 짧은 독립 문장/문구, 번호형 소제목, 이모지 소제목 등을 후보로 봅니다.
    if re.match(r"^(\d+|[①-⑩]|[-•✔✅⭐📌#])", line):
        return True
    if line.endswith(("?", "!", ":")):
        return True
    if len(line.split()) <= 8 and not line.endswith(("다", "요", "습니다")):
        return True
    return False


def _template_topic_from_text(title: str, body: str) -> str:
    text = f"{title} {body}".lower()
    if any(k in text for k in ["맛집", "순대", "국밥", "메뉴", "식당", "카페", "커피", "음식", "웨이팅"]):
        return "food"
    if any(k in text for k in ["제품", "구매", "쿠팡", "스마트스토어", "사용", "배송", "운동기구", "화장품", "리뷰"]):
        return "product"
    if any(k in text for k in ["숙소", "호텔", "여행", "관광", "전시", "공연", "장소", "방문"]):
        return "place"
    return "general"


def _general_section_roles(topic: str) -> List[str]:
    if topic == "food":
        return ["방문 계기와 첫인상", "위치와 기본 정보", "메뉴와 주문한 것", "맛과 분위기 후기", "좋았던 점과 아쉬운 점", "추천 대상", "총평"]
    if topic == "product":
        return ["사용/구매 계기", "제품 기본 정보", "핵심 특징과 혜택", "실제 사용감", "좋았던 점과 아쉬운 점", "추천 대상", "총평"]
    if topic == "place":
        return ["방문 계기", "위치와 이용 정보", "공간/분위기 소개", "직접 경험한 후기", "좋았던 점과 아쉬운 점", "추천 대상", "총평"]
    return ["시작하는 이야기", "기본 정보", "핵심 특징", "직접 경험한 후기", "좋았던 점과 아쉬운 점", "추천 대상", "총평"]


def analyze_blog_template_source(source: Dict[str, Any]) -> Dict[str, Any]:
    # 블로그 링크의 원문을 복사하지 않고 구조/톤 힌트만 뽑습니다.
    body = clean_text(source.get("본문 일부", ""))
    title = clean_text(source.get("페이지 제목", "")) or clean_text(source.get("상품명 후보", ""))
    url = clean_text(source.get("URL", ""))
    lines = [clean_text(x) for x in body.splitlines() if clean_text(x)]
    heading_candidates: List[str] = []
    for line in lines:
        normalized = re.sub(r"\s+", " ", line).strip()
        if _looks_like_blog_heading(normalized) and normalized not in heading_candidates:
            heading_candidates.append(normalized)
        if len(heading_candidates) >= 12:
            break

    intro_sample = " ".join(lines[:3])[:280]
    outro_sample = " ".join(lines[-4:])[:280] if len(lines) >= 4 else " ".join(lines[-2:])[:220]
    hashtag_count = len(re.findall(r"#[0-9A-Za-z가-힣_]+", body))
    bullet_like = sum(1 for x in lines if re.match(r"^[-•✔✅⭐📌]", x))
    avg_len = 0
    if lines:
        avg_len = int(sum(len(x) for x in lines[:60]) / min(len(lines), 60))
    joined = " ".join(lines[:60])
    if re.search(r"했어요|좋았어요|같아요|더라고요|거든요", joined):
        tone_hint = "자연스러운 후기체/존댓말"
    elif re.search(r"했다|좋았다|느꼈다|다녀왔다", joined):
        tone_hint = "담백한 기록체"
    elif re.search(r"했어|좋아|같아|더라", joined):
        tone_hint = "친근한 반말체"
    else:
        tone_hint = "정보형 또는 혼합형"

    topic_hint = _template_topic_from_text(title, body)
    role_hints = _general_section_roles(topic_hint)
    structure_hint = []
    if heading_candidates:
        structure_hint.append(f"원문에서 소제목 후보 {len(heading_candidates)}개를 감지했습니다. 저장 템플릿에는 일반화된 부제목 자리로 변환합니다.")
    structure_hint.append("일반화한 흐름: " + " → ".join(role_hints))
    if intro_sample:
        structure_hint.append("도입부 특징: " + intro_sample)
    if bullet_like:
        structure_hint.append(f"리스트/체크형 문장 사용 빈도: {bullet_like}개 후보")
    if hashtag_count:
        structure_hint.append(f"해시태그 사용: 약 {hashtag_count}개")
    if avg_len:
        structure_hint.append(f"문단 평균 길이 후보: 약 {avg_len}자")
    if outro_sample:
        structure_hint.append("마무리 흐름 특징: " + outro_sample)

    return {
        "구분": "블로그 템플릿 구조 참고 링크",
        "URL": url,
        "페이지 제목": title[:180],
        "톤 힌트": tone_hint,
        "주제 힌트": topic_hint,
        "소제목 후보": heading_candidates[:10],
        "일반화 부제목 역할": role_hints,
        "구조 분석": "\n".join(structure_hint)[:1400],
        "주의": "이 링크는 글 구조와 톤만 참고하고, 문장/표현은 그대로 복사하지 않습니다.",
    }


def collect_template_style_sources(template_links_text: str, max_links: int = 6) -> List[Dict[str, Any]]:
    urls = split_urls(template_links_text)[:max_links]
    results: List[Dict[str, Any]] = []
    for url in urls:
        source = fetch_public_page_summary(url, "블로그 템플릿 구조 참고 링크")
        analyzed = analyze_blog_template_source(source)
        if source.get("오류"):
            analyzed["오류"] = source.get("오류")
        results.append(analyzed)
    return results


def make_template_from_analyzed_links(sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    # 분석된 블로그 링크 자료를 실제 저장 가능한 템플릿 데이터로 변환합니다.
    valid_sources = [s for s in sources if isinstance(s, dict) and not s.get("오류")]
    tone_hints: List[str] = []
    source_urls: List[str] = []
    topic_votes: List[str] = []

    for source in valid_sources:
        url = clean_text(source.get("URL", ""))
        if url and url not in source_urls:
            source_urls.append(url)
        tone = clean_text(source.get("톤 힌트", ""))
        if tone:
            tone_hints.append(tone)
        topic = clean_text(source.get("주제 힌트", ""))
        if topic:
            topic_votes.append(topic)

    topic = "general"
    if topic_votes:
        topic = max(set(topic_votes), key=topic_votes.count)
    section_roles = _general_section_roles(topic)
    section_lines = [f"{i}. 부제목 {i} - {role}" for i, role in enumerate(section_roles, start=1)]

    tone_summary = " / ".join(dict.fromkeys(tone_hints[:3])) or "자연스러운 후기체"
    now = datetime.now().isoformat(timespec="seconds")
    return compact_dict({
        "title_rule": "제목 자리에는 메인 키워드 + 상황/대상 + 후기 느낌으로 제목 1개만 작성",
        "subtitle_rule": "부제목 자리에는 한 줄 요약 또는 기대감을 짧게 작성",
        "opening_rule": f"{tone_summary} 느낌으로 시작. 독자가 공감할 상황 → 리뷰 대상 소개 → 한 줄 총평 순서로 도입",
        "section_structure": "\n".join(section_lines),
        "photo_video_rule": "도입부 뒤 대표 사진 자리, 특징 설명 뒤 상세 이미지 자리, 사용감/방문 후기 중간에 실제 사진 또는 영상 자리, 총평 전 마무리 이미지 자리를 배치",
        "closing_rule": "추천 대상과 주의할 점을 정리한 뒤, 과장 없이 재사용/재방문/추천 의사를 자연스럽게 마무리",
        "hashtag_rule": "메인 키워드 + 제품/장소 키워드 + 후기/추천/사용상황 키워드로 8~12개 작성",
        "avoid_rule": "참고 블로그의 문장, 독특한 표현, 개인 경험을 그대로 복사하지 않기. 과장 광고처럼 보이는 표현 줄이기",
        "memo": "블로그 링크를 분석해 만든 템플릿입니다. 미리보기에는 제목/부제목/본문/사진 자리처럼 양식으로 표시하고, 실제 글 생성 때 사용자가 입력한 리뷰 정보로 채웁니다.",
        "source_links": source_urls,
        "created_from": "blog_link_analysis",
        "created_at": now,
    })


def _template_section_roles(template_data: Dict[str, Any]) -> List[str]:
    raw = clean_text(template_data.get("section_structure", ""))
    roles: List[str] = []
    for line in raw.splitlines():
        line = clean_text(line)
        if not line:
            continue
        line = re.sub(r"^\d+[\.\)]\s*", "", line).strip()
        line = re.sub(r"^부제목\s*\d+\s*[-–—:]\s*", "", line).strip()
        if line and line not in roles:
            roles.append(line)
    return roles or _general_section_roles("general")


def render_template_preview_markdown(template_data: Dict[str, Any], sources: Optional[List[Dict[str, Any]]] = None) -> str:
    # 저장 전에 사용자가 확인할 수 있는 템플릿 분석 요약입니다. 기본 화면에는 HTML 미리보기를 보여줍니다.
    sources = sources or []
    lines = ["### 템플릿 분석 요약", ""]
    if template_data.get("title_rule"):
        lines += ["**제목 규칙**", clean_text(template_data.get("title_rule")), ""]
    if template_data.get("opening_rule"):
        lines += ["**도입부 흐름**", clean_text(template_data.get("opening_rule")), ""]
    if template_data.get("section_structure"):
        lines += ["**본문 구조 / 부제목 역할**", clean_text(template_data.get("section_structure")), ""]
    if template_data.get("photo_video_rule"):
        lines += ["**사진·동영상 배치 방식**", clean_text(template_data.get("photo_video_rule")), ""]
    if template_data.get("closing_rule"):
        lines += ["**마무리 방식**", clean_text(template_data.get("closing_rule")), ""]
    if template_data.get("hashtag_rule"):
        lines += ["**해시태그 규칙**", clean_text(template_data.get("hashtag_rule")), ""]
    if template_data.get("avoid_rule"):
        lines += ["**주의할 표현**", clean_text(template_data.get("avoid_rule")), ""]

    valid_sources = [s for s in sources if isinstance(s, dict) and not s.get("오류")]
    failed_sources = [s for s in sources if isinstance(s, dict) and s.get("오류")]
    if valid_sources or failed_sources:
        lines += ["**분석 결과**", f"- 읽은 링크: {len(valid_sources)}개", f"- 읽지 못한 링크: {len(failed_sources)}개", ""]
    if valid_sources:
        lines += ["**참고한 링크**"]
        for idx, source in enumerate(valid_sources[:6], start=1):
            title = clean_text(source.get("페이지 제목", "")) or "제목 없음"
            url = clean_text(source.get("URL", ""))
            lines.append(f"{idx}. {title} — {url}")
        lines.append("")
    lines.append("> 저장하면 이후 글 생성 때 이 구조를 템플릿으로 불러와 사용할 수 있습니다. 원문 문장은 복사하지 않고 구조만 반영합니다.")
    return "\n".join(lines)


def render_visual_template_preview_html(template_data: Dict[str, Any], sources: Optional[List[Dict[str, Any]]] = None) -> str:
    # 분석된 구조를 실제 네이버 블로그에 적용했을 때처럼 양식 미리보기로 보여줍니다.
    sources = sources or []
    roles = _template_section_roles(template_data)[:7]
    today = datetime.now().strftime("%Y.%m.%d. %H:%M")
    source_count = len([s for s in sources if isinstance(s, dict) and not s.get("오류")])
    role_blocks = []
    for idx, role in enumerate(roles, start=1):
        safe_role = html.escape(role)
        media = ""
        if idx == 1:
            media = '<div class="template-media">대표 사진 자리</div>'
        elif idx == 3:
            media = '<div class="template-media detail">상세 이미지 / 제품 설명 이미지 자리</div>'
        elif idx == 4:
            media = '<div class="template-media video">실제 사진 또는 동영상 자리</div>'
        role_blocks.append(f"""
        <section class="tpl-section">
          <h2>부제목 {idx}</h2>
          <div class="role">역할: {safe_role}</div>
          {media}
          <p><span class="ghost">본문 문단 자리</span> — 여기에 사용자가 입력한 제품·장소 정보, 링크에서 읽은 특징, 직접 사용한 후기 내용이 들어갑니다.</p>
          <p><span class="ghost">경험/정보 자리</span> — 원문 문장을 복사하지 않고 새 리뷰 대상에 맞춰 자연스럽게 작성됩니다.</p>
        </section>
        """)
    source_note = f"분석 링크 {source_count}개 구조 반영" if source_count else "분석 링크 구조 반영"
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ margin:0; background:#f4f6f8; font-family:-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Malgun Gothic',Arial,sans-serif; color:#222; }}
  .wrap {{ max-width: 760px; margin: 0 auto; background:#fff; min-height:100vh; }}
  .topbar {{ height:46px; border-bottom:1px solid #edf0f2; display:flex; align-items:center; padding:0 18px; color:#03c75a; font-weight:800; }}
  .article {{ padding: 34px 22px 70px; }}
  .category {{ color:#03c75a; font-size:13px; font-weight:700; margin-bottom:10px; }}
  h1 {{ font-size:30px; line-height:1.35; margin: 0 0 10px; letter-spacing:-.4px; }}
  .subtitle {{ font-size:17px; color:#666; margin:0 0 12px; line-height:1.7; }}
  .meta {{ font-size:13px; color:#888; margin-bottom:28px; }}
  h2 {{ font-size:22px; margin:34px 0 8px; line-height:1.45; }}
  p {{ font-size:16px; line-height:1.95; margin: 10px 0; word-break:keep-all; }}
  .role {{ display:inline-block; margin:0 0 10px; padding:5px 9px; border-radius:999px; background:#eefbf4; color:#0c8f48; font-size:12px; font-weight:700; }}
  .ghost {{ color:#03a94f; font-weight:800; }}
  .template-media {{ border:1px dashed #b8e5ca; background:#f7fcf9; border-radius:12px; height:170px; margin:18px 0; display:flex; align-items:center; justify-content:center; color:#0c8f48; font-weight:800; }}
  .template-media.detail {{ background:#fbfcf7; border-color:#dce8b4; color:#718000; }}
  .template-media.video {{ background:#f8f9fc; border-color:#c9d2ee; color:#3450a4; }}
  .hash {{ margin-top:30px; color:#00a850; line-height:1.9; font-size:15px; }}
  .notice {{ margin-top:24px; padding:14px 16px; background:#f7f8fa; border-radius:10px; color:#666; font-size:13px; line-height:1.7; }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">NAVER Blog 템플릿 미리보기</div>
    <article class="article">
      <div class="category">템플릿 · {html.escape(source_note)}</div>
      <h1>제목 자리</h1>
      <div class="subtitle">부제목 자리 · 한 줄 요약 또는 기대감이 들어가는 영역</div>
      <div class="meta">작성자 · {today}</div>
      <p><span class="ghost">도입부 자리</span> — 독자가 공감할 상황을 먼저 보여주고, 리뷰 대상을 자연스럽게 소개합니다.</p>
      <p><span class="ghost">한 줄 총평 자리</span> — 글을 계속 읽고 싶게 만드는 짧은 기대감/요약이 들어갑니다.</p>
      {''.join(role_blocks)}
      <section class="tpl-section">
        <h2>마무리 부제목</h2>
        <p><span class="ghost">추천 대상 자리</span> — 어떤 사람에게 잘 맞는지 정리합니다.</p>
        <p><span class="ghost">주의할 점 자리</span> — 아쉬운 점이나 참고할 점을 과장 없이 적습니다.</p>
        <p><span class="ghost">총평 자리</span> — 재구매/재방문/추천 의사를 자연스럽게 마무리합니다.</p>
      </section>
      <div class="hash">#키워드1 #키워드2 #제품명또는장소명 #후기 #추천 #사용상황</div>
      <div class="notice">이 화면은 저장 전 템플릿 양식 미리보기입니다. 실제 글 생성 시에는 “제목 자리”, “부제목 자리”, “본문 자리”가 사용자가 입력한 정보와 링크 분석 자료로 자동 채워집니다.</div>
    </article>
  </div>
</body>
</html>"""


# =========================================================
# 네이버 블로그 미리보기 / 자동 입력 도우미
# =========================================================

def normalize_nvidia_base_url(base_url: str) -> Tuple[str, str]:
    """사용자가 NVIDIA 모델 페이지 주소를 넣어도 실제 OpenAI 호환 API 주소로 보정합니다."""
    raw = clean_text(base_url).rstrip("/")
    if not raw:
        return "https://integrate.api.nvidia.com/v1", "base_url이 비어 있어 NVIDIA OpenAI 호환 기본 API 주소로 보정했습니다."
    if raw in ["https://build.nvidia.com/models", "http://build.nvidia.com/models"]:
        return "https://integrate.api.nvidia.com/v1", "입력한 https://build.nvidia.com/models 는 모델 선택/소개 페이지라서, 실제 API 호출은 https://integrate.api.nvidia.com/v1 로 자동 보정했습니다."
    return raw, ""


def extract_title_candidates(text: str) -> List[str]:
    candidates: List[str] = []
    lines = [line.strip() for line in clean_text(text).splitlines()]
    bad_patterns = [r"제목\s*후보", r"출력\s*형식", r"네이버\s*블로그\s*복붙용", r"해시태그\s*10개", r"본문\s*:"]
    for line in lines:
        if not line:
            continue
        if any(re.search(pattern, line, flags=re.I) for pattern in bad_patterns):
            continue
        if re.match(r"^제목\s*[:：]", line):
            title = re.split(r"[:：]", line, maxsplit=1)[1].strip(" -*\t")
            if title:
                candidates.append(title)
            continue
        if re.match(r"^\d+[\).]\s*", line):
            title = re.sub(r"^\d+[\).]\s*", "", line).strip()
            if 5 <= len(title) <= 80 and not title.startswith("#"):
                candidates.append(title)
        if line.startswith("# "):
            candidates.append(line[2:].strip())
        if "최종" in line and "제목" in line:
            after = re.split(r"[:：]", line, maxsplit=1)
            if len(after) == 2:
                title = after[1].strip(" -*\t")
                if title and not re.search(r"제목\s*후보", title):
                    candidates.append(title)
    if not candidates:
        for first_line in lines:
            first_line = re.sub(r"[*_`#]", "", first_line).strip()
            if not first_line:
                continue
            if any(re.search(pattern, first_line, flags=re.I) for pattern in bad_patterns):
                continue
            if first_line.startswith("#") or first_line.startswith("-"):
                continue
            if re.match(r"^(안녕하세요|오늘은|이번에는|본문|해시태그)\b", first_line):
                continue
            if 5 <= len(first_line) <= 90 and not first_line.endswith(":") and not first_line.startswith("#"):
                candidates.append(first_line)
                break
    seen = set()
    result = []
    for c in candidates:
        c = re.sub(r"[*_`#]", "", c).strip()
        if c and not any(re.search(pattern, c, flags=re.I) for pattern in bad_patterns) and c not in seen:
            result.append(c)
            seen.add(c)
    return result[:5]


def extract_best_title(text: str) -> str:
    candidates = extract_title_candidates(text)
    if candidates:
        return candidates[0]
    title = markdown_title(text)
    if re.search(r"제목\s*후보|출력\s*형식", title):
        return "네이버 블로그 초안"
    return title

def strip_title_candidate_block(text: str) -> str:
    """AI 출력에서 '제목 후보' 안내 부분을 본문에서 최대한 제거합니다."""
    lines = clean_text(text).splitlines()
    kept: List[str] = []
    skip = False
    for line in lines:
        stripped = line.strip()
        if re.search(r"제목\s*후보", stripped):
            skip = True
            continue
        if skip:
            if stripped.startswith("# ") or re.search(r"본문|네이버 블로그 복붙용 본문", stripped):
                skip = False
            elif re.match(r"^\d+[\).]\s*", stripped) or not stripped or "최종" in stripped:
                continue
        if re.search(r"^\d+\.\s*제목\s*후보", stripped):
            continue
        if re.search(r"^\d+\.\s*최종\s*추천\s*제목", stripped):
            continue
        if re.match(r"^제목\s*[:：]", stripped):
            continue
        if re.match(r"^(본문|해시태그)\s*[:：]?\s*$", stripped):
            continue
        if re.search(r"네이버 블로그 복붙용 본문", stripped):
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def video_to_data_url(uploaded_file, max_mb: int = 18) -> str:
    """작은 영상만 HTML 미리보기 안에 직접 넣습니다. 큰 영상은 용량 때문에 별도 표시합니다."""
    try:
        data = uploaded_file_bytes(uploaded_file)
        if not data or len(data) > max_mb * 1024 * 1024:
            return ""
        suffix = Path(getattr(uploaded_file, "name", "video.mp4")).suffix.lower().lstrip(".") or "mp4"
        mime = "video/mp4"
        if suffix == "webm":
            mime = "video/webm"
        elif suffix in ["mov", "m4v"]:
            mime = "video/mp4"
        encoded = base64.b64encode(data).decode("utf-8")
        return f"data:{mime};base64,{encoded}"
    except Exception:
        return ""


def placeholder_media_html(kind: str, idx: int, photo_payload: List[Dict[str, str]], video_payload: List[Dict[str, str]], video_files: List[Any]) -> str:
    if kind == "photo":
        if idx - 1 < len(photo_payload):
            photo = photo_payload[idx - 1]
            caption = html.escape(photo.get("사진 설명") or photo.get("파일명") or f"사진 {idx}")
            data_url = photo.get("data_url", "")
            if data_url:
                return f'<figure class="se-figure"><img src="{data_url}" alt="{caption}"><figcaption>{caption}</figcaption></figure>'
            return f'<div class="se-media-empty">사진 {idx}: {caption}</div>'
    if kind == "video":
        caption = f"동영상 {idx}"
        filename = ""
        if idx - 1 < len(video_payload):
            video = video_payload[idx - 1]
            caption = video.get("동영상 설명") or video.get("파일명") or caption
            filename = video.get("파일명", "")
        caption_html = html.escape(caption)
        file_html = html.escape(filename)
        data_url = ""
        if idx - 1 < len(video_files):
            data_url = video_to_data_url(video_files[idx - 1])
        if data_url:
            return f'<figure class="se-figure"><video controls src="{data_url}"></video><figcaption>{caption_html}</figcaption></figure>'
        return f'<div class="se-video-box"><div class="play">▶</div><b>{caption_html}</b><br><span>{file_html}</span><p>큰 영상은 실제 네이버 작성 화면에서 업로드하면 됩니다.</p></div>'
    return ""


def markdown_to_naver_preview_html(markdown_text: str, title: str, photo_payload: List[Dict[str, str]], video_payload: List[Dict[str, str]], video_files: List[Any]) -> str:
    """네이버 블로그 글 화면 느낌에 가깝게 보여주는 HTML 미리보기입니다."""
    body_lines = strip_title_candidate_block(markdown_text).splitlines()
    html_lines: List[str] = []
    in_ul = False

    def close_ul() -> None:
        nonlocal in_ul
        if in_ul:
            html_lines.append("</ul>")
            in_ul = False

    for raw in body_lines:
        line = raw.strip()
        if not line:
            close_ul()
            html_lines.append('<div class="se-space"></div>')
            continue
        media_match = re.search(r"\[(사진|동영상)\s*(\d+)\]", line)
        if media_match:
            close_ul()
            kind = "photo" if media_match.group(1) == "사진" else "video"
            idx = int(media_match.group(2))
            html_lines.append(placeholder_media_html(kind, idx, photo_payload, video_payload, video_files))
            rest = re.sub(r"\[(사진|동영상)\s*\d+\]", "", line).strip(" -*")
            if rest:
                rest = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(rest))
                html_lines.append(f'<p class="se-caption-text">{rest}</p>')
            continue
        if line.startswith("# "):
            # 제목은 상단에 따로 표시하므로 본문 제목은 생략합니다.
            continue
        if line.startswith("## "):
            close_ul()
            html_lines.append(f'<h2>{html.escape(line[3:].strip())}</h2>')
            continue
        if line.startswith("### "):
            close_ul()
            html_lines.append(f'<h3>{html.escape(line[4:].strip())}</h3>')
            continue
        if line.startswith("- "):
            if not in_ul:
                html_lines.append("<ul>")
                in_ul = True
            item = html.escape(line[2:].strip())
            item = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", item)
            html_lines.append(f"<li>{item}</li>")
            continue
        close_ul()
        p = html.escape(line)
        p = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", p)
        if p.startswith("&gt;"):
            html_lines.append(f'<blockquote>{p.replace("&gt;", "", 1).strip()}</blockquote>')
        else:
            html_lines.append(f"<p>{p}</p>")
    close_ul()
    safe_title = html.escape(title or extract_best_title(markdown_text))
    today = datetime.now().strftime("%Y.%m.%d. %H:%M")
    body_html = "\n".join(html_lines)
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ margin:0; background:#f4f6f8; font-family:-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Malgun Gothic',Arial,sans-serif; color:#222; }}
  .phone-wrap {{ max-width: 760px; margin: 0 auto; background:#fff; min-height:100vh; }}
  .topbar {{ height:46px; border-bottom:1px solid #edf0f2; display:flex; align-items:center; padding:0 18px; color:#03c75a; font-weight:800; }}
  .article {{ padding: 34px 22px 70px; }}
  .category {{ color:#03c75a; font-size:13px; font-weight:700; margin-bottom:10px; }}
  h1 {{ font-size:30px; line-height:1.35; margin: 0 0 10px; letter-spacing:-.4px; }}
  .meta {{ font-size:13px; color:#888; margin-bottom:34px; }}
  h2 {{ font-size:22px; margin:34px 0 12px; line-height:1.45; }}
  h3 {{ font-size:18px; margin:28px 0 10px; }}
  p {{ font-size:16px; line-height:1.95; margin: 10px 0; word-break:keep-all; }}
  ul {{ padding-left: 22px; margin: 12px 0; }}
  li {{ font-size:16px; line-height:1.9; margin:4px 0; }}
  blockquote {{ border-left: 4px solid #03c75a; background:#f7fbf9; padding: 14px 16px; margin: 18px 0; color:#333; border-radius: 8px; }}
  .se-space {{ height: 10px; }}
  .se-figure {{ margin: 26px 0; }}
  .se-figure img, .se-figure video {{ width:100%; border-radius: 2px; display:block; background:#111; }}
  figcaption, .se-caption-text {{ text-align:center; color:#777; font-size:14px; line-height:1.6; margin-top:8px; }}
  .se-video-box, .se-media-empty {{ border:1px solid #e1e5e9; background:#f8fafb; border-radius:12px; padding:24px 18px; margin:24px 0; text-align:center; color:#555; }}
  .se-video-box .play {{ width:58px; height:58px; margin: 0 auto 10px; border-radius:50%; background:#111; color:#fff; display:flex; align-items:center; justify-content:center; }}
</style>
</head>
<body>
  <div class="phone-wrap">
    <div class="topbar">NAVER Blog 미리보기</div>
    <article class="article">
      <div class="category">리뷰 · 자동 작성 미리보기</div>
      <h1>{safe_title}</h1>
      <div class="meta">작성자 · {today}</div>
      {body_html}
    </article>
  </div>
</body>
</html>"""


def save_uploaded_files_to_temp(files: List[Any], folder_name: str) -> List[str]:
    base_dir = Path(tempfile.gettempdir()) / "naver_blog_writer_uploads" / folder_name
    base_dir.mkdir(parents=True, exist_ok=True)
    paths: List[str] = []
    used = set()
    for idx, file in enumerate(files, start=1):
        original = safe_original_filename(getattr(file, "name", ""), f"file_{idx}")
        filename = unique_filename(original, used)
        path = base_dir / filename
        data = uploaded_file_bytes(file)
        if data:
            path.write_bytes(data)
            paths.append(str(path))
    return paths


def find_elements_recursively(driver, css: str, max_depth: int = 3):
    """현재 페이지와 iframe 안에서 요소를 재귀적으로 찾습니다."""
    found = []
    try:
        found.extend(driver.find_elements("css selector", css))
    except Exception:
        pass
    if max_depth <= 0:
        return found
    try:
        frames = driver.find_elements("css selector", "iframe")
    except Exception:
        frames = []
    for frame in frames:
        try:
            driver.switch_to.frame(frame)
            found.extend(find_elements_recursively(driver, css, max_depth - 1))
            driver.switch_to.parent_frame()
        except Exception:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
    return found




def find_elements_with_frames(driver, css: str, max_depth: int = 3, path: Tuple[int, ...] = ()):
    """현재 페이지와 iframe 안에서 요소를 찾고, 해당 iframe 경로까지 같이 반환합니다."""
    results = []
    try:
        for el in driver.find_elements("css selector", css):
            results.append((path, el))
    except Exception:
        pass
    if max_depth <= 0:
        return results
    try:
        frames = driver.find_elements("css selector", "iframe")
    except Exception:
        frames = []
    for idx, frame in enumerate(frames):
        try:
            driver.switch_to.frame(frame)
            results.extend(find_elements_with_frames(driver, css, max_depth - 1, path + (idx,)))
            driver.switch_to.parent_frame()
        except Exception:
            try:
                driver.switch_to.default_content()
                for frame_idx in path:
                    current_frames = driver.find_elements("css selector", "iframe")
                    driver.switch_to.frame(current_frames[frame_idx])
            except Exception:
                pass
    return results


def switch_to_frame_path(driver, path: Tuple[int, ...]) -> None:
    driver.switch_to.default_content()
    for frame_idx in path:
        frames = driver.find_elements("css selector", "iframe")
        driver.switch_to.frame(frames[frame_idx])


def collect_visible_editables(driver):
    items = find_elements_with_frames(driver, "[contenteditable='true'], textarea, input[type='text']", 3)
    visible = []
    for path, el in items:
        try:
            switch_to_frame_path(driver, path)
            if el.is_displayed() and el.size.get("width", 0) > 30 and el.size.get("height", 0) > 5:
                attrs = " ".join([
                    el.get_attribute("placeholder") or "",
                    el.get_attribute("aria-label") or "",
                    el.get_attribute("title") or "",
                    el.get_attribute("class") or "",
                    el.get_attribute("id") or "",
                    el.get_attribute("name") or "",
                ]).lower()
                area = el.size.get("height", 0) * el.size.get("width", 0)
                visible.append({"path": path, "element": el, "attrs": attrs, "area": area})
        except Exception:
            pass
        finally:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
    return visible

def paste_to_element(driver, element, text: str) -> None:
    from selenium.webdriver import ActionChains
    from selenium.webdriver.common.keys import Keys
    import pyperclip

    element.click()
    time.sleep(0.3)
    pyperclip.copy(text)
    ActionChains(driver).key_down(Keys.CONTROL).send_keys("v").key_up(Keys.CONTROL).perform()
    time.sleep(0.5)


def write_to_naver_blog(title: str, body: str, photo_files: List[Any], video_files: List[Any], naver_write_url: str, attach_media: bool = False) -> str:
    """PC에서 Chrome을 띄워 네이버 블로그 글쓰기 화면에 제목/본문을 자동 입력합니다.
    로그인과 최종 발행 버튼은 사용자가 직접 합니다.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.support.ui import WebDriverWait
        from webdriver_manager.chrome import ChromeDriverManager
        import pyperclip
    except Exception as exc:
        raise RuntimeError("네이버 자동 입력 기능을 쓰려면 selenium, webdriver-manager, pyperclip 설치가 필요합니다. requirements.txt로 설치하세요.") from exc

    title = clean_text(title) or extract_best_title(body)
    body = strip_title_candidate_block(body)
    if not body:
        raise RuntimeError("네이버에 입력할 본문이 비어 있습니다. 먼저 글을 생성하거나 미리보기 본문을 수정하세요.")

    user_data_dir = Path.home() / ".naver_blog_writer_chrome"
    user_data_dir.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.add_argument(f"--user-data-dir={user_data_dir}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("detach", True)

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    driver.maximize_window()
    driver.get(naver_write_url)

    # 사용자가 직접 로그인할 시간을 줍니다. 이미 로그인되어 있으면 바로 진행됩니다.
    WebDriverWait(driver, 180).until(lambda d: len(collect_visible_editables(d)) > 0)
    time.sleep(2)

    visible = collect_visible_editables(driver)
    if not visible:
        pyperclip.copy(title + "\n\n" + body)
        return "네이버 편집기 입력칸을 자동으로 찾지 못했습니다. 대신 제목+본문을 클립보드에 복사했습니다. 열린 네이버 글쓰기 화면에 직접 붙여넣어 주세요."

    title_item = None
    for item in visible:
        attrs = item["attrs"]
        if "제목" in attrs or "title" in attrs:
            title_item = item
            break
    if title_item is None:
        title_item = visible[0]
    candidates = [item for item in visible if item is not title_item]
    body_item = max(candidates, key=lambda item: item["area"]) if candidates else visible[-1]

    switch_to_frame_path(driver, title_item["path"])
    paste_to_element(driver, title_item["element"], title)
    switch_to_frame_path(driver, body_item["path"])
    paste_to_element(driver, body_item["element"], body)
    driver.switch_to.default_content()

    upload_note = ""
    if attach_media and (photo_files or video_files):
        paths = save_uploaded_files_to_temp(photo_files + video_files, "naver_media")
        try:
            input_items = find_elements_with_frames(driver, "input[type='file']", 3)
            usable_inputs = []
            for path, inp in input_items:
                try:
                    switch_to_frame_path(driver, path)
                    accept = (inp.get_attribute("accept") or "").lower()
                    multiple = inp.get_attribute("multiple")
                    if accept or multiple is not None:
                        usable_inputs.append((path, inp))
                except Exception:
                    pass
                finally:
                    driver.switch_to.default_content()
            if usable_inputs and paths:
                switch_to_frame_path(driver, usable_inputs[0][0])
                usable_inputs[0][1].send_keys("\n".join(paths))
                driver.switch_to.default_content()
                upload_note = " 사진/동영상 업로드도 시도했습니다. 네이버 편집기 상태에 따라 일부 파일은 직접 다시 올려야 할 수 있습니다."
            else:
                upload_note = " 사진/동영상 자동 업로드 입력창은 찾지 못했습니다. 본문 안의 [사진 1], [동영상 1] 위치에 직접 업로드해 주세요."
        except Exception:
            upload_note = " 사진/동영상 자동 업로드는 실패했습니다. 본문 안의 위치 표시를 보고 직접 업로드해 주세요."

    return "네이버 블로그 글쓰기 화면에 제목과 본문을 입력했습니다. 최종 발행은 열린 네이버 화면에서 직접 확인 후 눌러주세요." + upload_note


# =========================================================
# 템플릿 기반 작성 엔진: API가 안 될 때도 초안을 만들 수 있게 유지
# =========================================================

def disclosure_line(disclosure: str) -> str:
    disclosure = clean_text(disclosure)
    if not disclosure or disclosure == "선택 안 함":
        return ""
    mapping = {
        "내돈내산": "> 이 글은 직접 비용을 지불하고 경험한 내돈내산 후기입니다.",
        "제품 제공": "> 이 글은 제품을 제공받아 직접 사용해본 후 작성한 후기입니다.",
        "체험단": "> 이 글은 체험단으로 참여해 직접 경험한 내용을 바탕으로 작성한 후기입니다.",
        "원고료 있음": "> 이 글은 원고료를 제공받아 작성한 후기이며, 실제 경험을 바탕으로 정리했습니다.",
        "광고/협찬": "> 이 글은 광고/협찬을 포함하고 있으며, 직접 경험한 내용을 바탕으로 작성했습니다.",
    }
    return mapping.get(disclosure, f"> 이 글은 {disclosure} 후기입니다.")


def intro_sentence(subject: str, one_line: str, visit_date: str, tone: str) -> str:
    visit_text = f" {visit_date}에" if has_text(visit_date) else ""
    if tone == "친근한 반말체":
        if one_line:
            return f"오늘은{visit_text} 경험해본 **{subject}** 후기를 정리해보려고 해. 한마디로는 {one_line}라는 느낌이었어."
        return f"오늘은{visit_text} 경험해본 **{subject}** 후기를 남겨보려고 해."
    if tone == "감성형":
        if one_line:
            return f"오늘은{visit_text} 기억에 남았던 **{subject}** 후기를 남겨봅니다. {one_line}라는 말이 잘 어울리는 경험이었어요."
        return f"오늘은{visit_text} 천천히 기억에 남았던 **{subject}** 후기를 남겨봅니다."
    if one_line:
        return f"오늘은{visit_text} 경험해본 **{subject}** 후기를 정리해보려고 합니다. 한마디로 표현하면, {one_line}라는 느낌이었어요."
    return f"오늘은{visit_text} 경험해본 **{subject}** 후기를 정리해보려고 합니다."


def make_title_candidates(subject: str, review_type: str, main_keyword: str, title_style: str, address: str = "") -> List[str]:
    keyword = first_non_empty(main_keyword, subject, fallback=subject)
    short_addr = ""
    if address:
        match = re.search(r"([가-힣A-Za-z0-9]+구|[가-힣A-Za-z0-9]+시|[가-힣A-Za-z0-9]+동|[가-힣A-Za-z0-9]+역)", address)
        short_addr = match.group(1) if match else ""

    templates_by_style = {
        "검색형": [
            f"{keyword} 찾는다면 {subject} 솔직 후기",
            f"{subject} {review_type} 후기, 방문/사용 전 알아둘 정보",
            f"{keyword} 추천 포인트와 아쉬운 점 정리",
            f"{subject} 실제 후기와 기본 정보 총정리",
            f"{short_addr + ' ' if short_addr else ''}{subject} 후기: 장점부터 정보까지",
        ],
        "감성형": [
            f"기억에 남았던 {subject}, 천천히 남겨보는 후기",
            f"{subject}에서 보낸 시간, 좋았던 순간들",
            f"다시 떠올려도 괜찮았던 {subject} 후기",
            f"{keyword}, 분위기까지 좋았던 곳/제품",
            f"나에게 잘 맞았던 {subject} 솔직 기록",
        ],
        "후기형": [
            f"{subject} 직접 경험해본 솔직 후기",
            f"{subject} 좋았던 점과 아쉬운 점",
            f"{subject} 다녀오고/사용하고 느낀 점",
            f"{keyword} 실제 후기, 이런 분께 추천해요",
            f"{subject} 리뷰: 기대했던 점과 실제 느낌",
        ],
        "정보형": [
            f"{subject} 정보 정리: 가격, 위치, 장단점",
            f"{keyword} 이용/사용 전 체크할 정보",
            f"{subject} 방문/구매 전 알아두면 좋은 점",
            f"{subject} 기본 정보와 솔직 후기",
            f"{keyword} 정보와 후기 한 번에 정리",
        ],
        "솔직리뷰형": [
            f"{subject} 솔직 리뷰, 좋았던 점과 아쉬운 점",
            f"{keyword} 솔직 후기: 재방문/재구매 생각은?",
            f"직접 경험한 {subject}, 솔직하게 적어본 후기",
            f"{subject} 과장 없이 남기는 리뷰",
            f"{subject} 후기, 이런 점은 좋고 이런 점은 아쉬웠어요",
        ],
    }
    titles = templates_by_style.get(title_style, templates_by_style["검색형"])
    result = []
    seen = set()
    for title in titles:
        title = re.sub(r"\s+", " ", title).strip()
        if title and title not in seen:
            result.append(title)
            seen.add(title)
    return result[:5]


def make_media_section(photos: List[Dict[str, str]], videos: List[Dict[str, str]], tone: str) -> str:
    lines = []
    if photos:
        lines.append("## 사진으로 보는 후기\n")
        for idx, photo in enumerate(photos, start=1):
            caption = first_non_empty(photo.get("사진 설명", ""), photo.get("파일명", ""), fallback=f"사진 {idx}")
            lines.append(f"[사진 {idx}] **{caption}**")
            if tone == "친근한 반말체":
                lines.append("사진 순서에 맞춰 실제 분위기를 떠올리면서 보면 좋아.\n")
            else:
                lines.append("사진 순서에 맞춰 실제 분위기를 함께 참고하시면 좋습니다.\n")
    if videos:
        lines.append("## 영상으로 보는 후기\n")
        for idx, video in enumerate(videos, start=1):
            caption = first_non_empty(video.get("동영상 설명", ""), video.get("파일명", ""), fallback=f"동영상 {idx}")
            lines.append(f"[동영상 {idx}] **{caption}**")
            if tone == "친근한 반말체":
                lines.append("움직임이나 현장감은 영상으로 보면 더 잘 느껴져.\n")
            else:
                lines.append("움직임이나 현장감은 영상으로 확인하면 더 이해하기 쉽습니다.\n")
    return "\n".join(lines).strip() + ("\n" if lines else "")


def make_feature_paragraph(features: str, strengths: str, experience: str, tone: str, length: str) -> str:
    feature_items = split_items(features)
    strength_items = split_items(strengths)
    paragraphs = []
    if experience:
        paragraphs.append(clean_text(experience))
    if feature_items:
        if tone == "친근한 반말체":
            paragraphs.append("특징을 간단히 정리하면 " + ", ".join(f"**{x}**" for x in feature_items[:6]) + " 이런 느낌이었어.")
        else:
            paragraphs.append("특징을 간단히 정리하면 " + ", ".join(f"**{x}**" for x in feature_items[:6]) + " 같은 느낌이었습니다.")
    if strength_items:
        if len(strength_items) == 1:
            paragraphs.append(f"가장 좋았던 점은 **{strength_items[0]}**였습니다.")
        else:
            bullet = "\n".join([f"- {item}" for item in strength_items])
            paragraphs.append(f"좋았던 점은 아래처럼 정리할 수 있습니다.\n\n{bullet}")
    if not paragraphs:
        paragraphs.append("입력한 특징과 장점을 기준으로 이 부분에 실제 후기가 자연스럽게 들어갑니다. 더 구체적인 경험을 입력할수록 글이 덜 반복적이고 더 생생해집니다.")
    if length in ["길게", "상세하게"]:
        paragraphs.append("개인적으로는 단순히 정보만 나열하는 것보다, 실제로 이용하거나 사용하면서 느낀 분위기와 흐름을 함께 적는 편이 블로그 글로 더 자연스럽게 읽혔습니다.")
    return "\n\n".join(paragraphs)


def make_weakness_section(weakness: str, tone: str) -> str:
    items = split_items(weakness)
    if not items:
        return ""
    if len(items) == 1:
        sentence = f"아쉬웠던 점은 **{items[0]}** 정도였습니다."
    else:
        sentence = "아쉬웠던 점도 솔직하게 적어보면 아래와 같습니다.\n\n" + "\n".join([f"- {item}" for item in items])
    if tone == "친근한 반말체":
        sentence = sentence.replace("였습니다", "였어").replace("습니다", "어")
    return f"## 아쉬웠던 점\n\n{sentence}\n"


def make_info_section(info: Dict[str, str]) -> str:
    labels = [
        ("주소", "주소"),
        ("지도 링크", "지도"),
        ("주차 정보", "주차"),
        ("영업시간/운영시간", "운영시간"),
        ("예약/문의", "예약/문의"),
        ("가격/메뉴/옵션", "가격/메뉴/옵션"),
        ("홈페이지/SNS", "홈페이지/SNS"),
        ("주변 정보", "주변 정보"),
        ("제품 스펙/구성/용량", "제품 정보"),
    ]
    lines = []
    for source_key, label in labels:
        value = clean_text(info.get(source_key, ""))
        if value:
            if "\n" in value:
                value = "\n  " + value.replace("\n", "\n  ")
            lines.append(f"- **{label}**: {value}")
    if not lines:
        return ""
    return "## 기본 정보\n\n" + "\n".join(lines) + "\n"


def make_recommend_section(recommended_for: str, cta: str, subject: str, tone: str) -> str:
    items = split_items(recommended_for)
    lines = []
    if items:
        lines.append("## 이런 분께 추천해요\n")
        lines.extend([f"- {item}" for item in items])
        lines.append("")
    if cta:
        lines.append(f"마지막으로, {cta}")
    else:
        if tone == "친근한 반말체":
            lines.append(f"{subject}가 궁금했다면 위 내용 참고해서 선택해보면 좋을 것 같아.")
        else:
            lines.append(f"{subject}가 궁금하셨던 분들은 위 내용을 참고해서 선택해보시면 좋겠습니다.")
    return "\n".join(lines).strip() + "\n"


def make_tags(subject: str, review_type: str, main_keyword: str, sub_keywords: str, desired_tags: str, features: str, address: str) -> List[str]:
    candidates: List[str] = []
    for raw in re.split(r"[\s,]+", clean_text(desired_tags)):
        if raw:
            candidates.append(raw)
    candidates.extend([subject, main_keyword, review_type, f"{review_type}후기", "솔직후기", "리뷰", "네이버블로그후기"])
    candidates.extend(split_items(sub_keywords))
    candidates.extend(split_items(features)[:4])
    if address:
        for token in re.findall(r"[가-힣A-Za-z0-9]+(?:시|구|동|역)", address):
            candidates.append(token)
    tags = []
    seen = set()
    for candidate in candidates:
        tag = make_tag(candidate)
        if tag and tag not in ["#선택안함", "#리뷰대상"] and tag not in seen:
            tags.append(tag)
            seen.add(tag)
        if len(tags) >= 10:
            break
    return tags


def generate_template_review(payload: Dict[str, Any]) -> str:
    basic = payload.get("기본 정보", {})
    info = payload.get("장소/제품 정보", {})
    review = payload.get("후기 내용", {})
    seo = payload.get("SEO", {})
    options = payload.get("작성 옵션", {})
    photos = payload.get("사진 정보", [])
    videos = payload.get("동영상 정보", [])
    reference_sources = payload.get("링크에서 가져온 참고자료", []) or []

    subject = first_non_empty(basic.get("제품명 또는 장소명", ""), basic.get("브랜드/업체명", ""), seo.get("메인 키워드", ""), fallback="리뷰 대상")
    review_type = first_non_empty(basic.get("리뷰 유형", ""), fallback="리뷰")
    one_line = clean_text(basic.get("한 줄 요약", ""))
    visit_date = clean_text(basic.get("방문/사용 날짜", ""))
    disclosure = clean_text(basic.get("광고/협찬 여부", ""))
    must_include = clean_text(basic.get("꼭 넣을 문장", ""))
    main_keyword = clean_text(seo.get("메인 키워드", ""))
    title_style = first_non_empty(options.get("제목 스타일", ""), fallback="검색형")
    tone = first_non_empty(options.get("말투", ""), fallback="자연스러운 일상체")
    length = first_non_empty(options.get("글 길이", ""), fallback="보통")
    cta = clean_text(options.get("마무리 유도 문장", ""))

    titles = make_title_candidates(subject, review_type, main_keyword, title_style, info.get("주소", ""))
    final_title = titles[0]

    lines: List[str] = []
    lines.append(f"# {final_title}")
    lines.append("")

    d_line = disclosure_line(disclosure)
    if d_line:
        lines.append(d_line)
        lines.append("")

    lines.append(intro_sentence(subject, one_line, visit_date, tone))
    lines.append("")
    if review_type not in ["선택 안 함", "리뷰"]:
        lines.append(f"처음 보는 분들도 참고하기 쉽도록 **{subject}**의 {review_type} 관련 정보와 실제 후기를 나눠서 정리했습니다.")
        lines.append("")

    reference_section = make_reference_section(reference_sources)
    if reference_section:
        lines.append(reference_section.strip())
        lines.append("")

    media_section = make_media_section(photos, videos, tone)
    if media_section:
        lines.append(media_section)
        lines.append("")

    lines.append("## 실제로 느낀 점")
    lines.append("")
    lines.append(make_feature_paragraph(review.get("특징 키워드", ""), review.get("좋았던 점", ""), review.get("실제 경험/에피소드", ""), tone, length))
    lines.append("")

    weakness_section = make_weakness_section(review.get("아쉬운 점", ""), tone)
    if weakness_section:
        lines.append(weakness_section.strip())
        lines.append("")

    info_section = make_info_section(info)
    if info_section:
        lines.append(info_section.strip())
        lines.append("")

    lines.append(make_recommend_section(review.get("추천 대상", ""), cta, subject, tone).strip())
    lines.append("")

    if must_include:
        lines.append("## 덧붙이고 싶은 말")
        lines.append("")
        lines.append(must_include)
        lines.append("")

    lines.append("## 총평")
    lines.append("")
    if tone == "친근한 반말체":
        lines.append(f"정리해보면 **{subject}**는 장점과 아쉬운 점이 비교적 분명해서, 내가 원하는 기준과 잘 맞는지 보고 선택하면 좋을 것 같아.")
    else:
        lines.append(f"정리해보면 **{subject}**는 장점과 아쉬운 점이 비교적 분명해서, 본인이 원하는 기준과 잘 맞는지 확인하고 선택하면 좋겠습니다.")
    lines.append("")

    tags = make_tags(subject, review_type, main_keyword, seo.get("서브 키워드", ""), seo.get("희망 태그", ""), review.get("특징 키워드", ""), info.get("주소", ""))
    if tags:
        lines.append("## 해시태그")
        lines.append(" ".join(tags))

    text = "\n".join(lines)
    for word in split_items(review.get("빼고 싶은 표현", "")):
        text = text.replace(word, "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()




# =========================================================
# 참고 링크 읽기 도우미
# 사용자가 제품 링크/장소 링크/다른 블로그 리뷰 링크를 여러 개 넣으면
# 공개 페이지에서 제목, 설명, 본문 일부를 가져와 AI 입력 참고자료로 사용합니다.
# 쿠팡은 일반 페이지보다 차단/동적 로딩이 잦아서 전용 추출 로직을 추가했습니다.
# =========================================================

def split_urls(text: str) -> List[str]:
    """여러 줄/공백/쉼표로 들어온 URL을 안전하게 분리합니다."""
    raw = clean_text(text)
    if not raw:
        return []
    found = re.findall(r'https?://[^\s,<>"]+', raw)
    result: List[str] = []
    seen = set()
    for url in found:
        url = url.strip().rstrip(".)],;")
        if url and url not in seen:
            result.append(url)
            seen.add(url)
    return result


def normalize_url_for_fetch(url: str) -> str:
    return clean_text(url).strip().rstrip(".)],;")


def is_coupang_url(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        return "coupang.com" in host
    except Exception:
        return "coupang.com" in clean_text(url).lower()


def normalize_coupang_url(url: str) -> str:
    """쿠팡 추적 파라미터를 줄여 상품 페이지를 더 안정적으로 읽습니다."""
    url = normalize_url_for_fetch(url)
    try:
        from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
        parsed = urlparse(url)
        if "coupang.com" not in parsed.netloc.lower():
            return url
        query = parse_qs(parsed.query)
        keep_keys = ["itemId", "vendorItemId", "sourceType"]
        keep = {}
        for key in keep_keys:
            if key in query and query[key]:
                keep[key] = query[key][0]
        new_query = urlencode(keep)
        # PC 상품 페이지 쪽이 공개 HTML 정보가 더 많이 들어오는 경우가 많습니다.
        return urlunparse(("https", "www.coupang.com", parsed.path, "", new_query, ""))
    except Exception:
        return url


def _absolute_url(base_url: str, url: str) -> str:
    """상대경로/프로토콜 생략 이미지 주소를 절대 URL로 바꿉니다."""
    try:
        from urllib.parse import urljoin
        url = html.unescape(clean_text(url)).strip().strip('"\'')
        if not url or url.startswith("data:"):
            return ""
        if url.startswith("//"):
            return "https:" + url
        return urljoin(base_url, url)
    except Exception:
        return clean_text(url)


def _extract_image_urls_from_html(source: str, page_url: str, limit: int = 10) -> List[str]:
    """링크 페이지의 대표/상세 이미지 URL 후보를 찾습니다.
    쿠팡 상세 설명이 이미지로 들어있는 경우가 많아서, 찾은 이미지 URL을 멀티모달 AI 입력에 함께 보냅니다.
    """
    candidates: List[str] = []

    def add_url(raw: str) -> None:
        raw = html.unescape(clean_text(raw))
        if not raw:
            return
        # srcset 형태면 가장 앞 URL만 우선 사용합니다.
        raw = raw.split()[0].strip().strip('"\'')
        url = _absolute_url(page_url, raw)
        if not url or url in candidates:
            return
        lower = url.lower()
        if lower.startswith("data:"):
            return
        if not re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", lower):
            # 확장자가 없어도 쿠팡/네이버 CDN 이미지는 허용합니다.
            if not any(host in lower for host in ["coupangcdn.com", "coupang.com/image", "pstatic.net", "naver.net"]):
                return
        bad_words = ["sprite", "logo", "icon", "blank", "loading", "profile", "favicon", "common/", "btn_"]
        if any(bad in lower for bad in bad_words):
            return
        candidates.append(url)

    # og:image, twitter:image 우선
    for meta_name in ["og:image", "twitter:image", "image"]:
        img = _extract_meta_content(source, [meta_name])
        if img:
            add_url(img)

    # img 태그의 여러 lazy-load 속성
    attr_patterns = [
        r'<img[^>]+(?:src|data-src|data-original|data-lazy-src|data-img-src|lazy-load|data-url|srcset)=["\']([^"\']+)["\']',
        r'(?:imageUrl|image_url|detailImage|vendorItemImage|originImage|productImage)["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        r'https?:\\/\\/[^"\']+?\.(?:jpg|jpeg|png|webp)(?:\\?[^"\']*)?',
        r'https?://[^"\'<>\s]+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\'<>\s]*)?',
    ]
    for pattern in attr_patterns:
        for m in re.findall(pattern, source, flags=re.I | re.S):
            raw = m[0] if isinstance(m, tuple) else m
            raw = raw.replace('\\/', '/')
            add_url(raw)

    # 상세/상품 관련 URL을 앞으로 정렬합니다.
    def score(url: str) -> int:
        lower = url.lower()
        score_value = 0
        for word in ["detail", "vendor_inventory", "product", "prod", "contents", "image", "coupangcdn", "review"]:
            if word in lower:
                score_value += 2
        for word in ["thumbnail", "thumb", "200x", "100x", "60x"]:
            if word in lower:
                score_value -= 1
        return score_value

    candidates = sorted(candidates, key=score, reverse=True)
    result: List[str] = []
    seen = set()
    for url in candidates:
        # URL이 너무 긴 추적 파라미터를 가진 경우 일부 정리
        short = url.split("#", 1)[0]
        if short not in seen:
            result.append(short)
            seen.add(short)
        if len(result) >= limit:
            break
    return result


def _extract_meta_content(source: str, names: List[str]) -> str:
    for name in names:
        patterns = [
            rf'<meta[^>]+property=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']*)["\']',
            rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']*)["\']',
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']{re.escape(name)}["\']',
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']{re.escape(name)}["\']',
        ]
        for pattern in patterns:
            m = re.search(pattern, source, flags=re.I | re.S)
            if m:
                return html.unescape(m.group(1)).strip()
    return ""


def _extract_title_from_html(source: str) -> str:
    title = _extract_meta_content(source, ["og:title", "twitter:title", "title"])
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", source, flags=re.I | re.S)
        title = html.unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else ""
    return clean_text(title)


def _html_to_readable_text(source: str, limit: int = 5000) -> str:
    source = re.sub(r"<script\b[^>]*>.*?</script>", " ", source, flags=re.I | re.S)
    source = re.sub(r"<style\b[^>]*>.*?</style>", " ", source, flags=re.I | re.S)
    source = re.sub(r"<(br|p|div|li|h[1-6]|tr|th|td)\b[^>]*>", "\n", source, flags=re.I)
    source = re.sub(r"<[^>]+>", " ", source)
    text = html.unescape(source)
    text = re.sub(r"\r", "\n", text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    lines = []
    seen = set()
    for line in text.splitlines():
        line = line.strip()
        if len(line) < 2:
            continue
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
        if sum(len(x) for x in lines) > limit:
            break
    return "\n".join(lines)[:limit]


def _fetch_html_with_browser_headers(url: str, timeout: int = 12) -> Tuple[str, str, str]:
    """브라우저에 가까운 헤더로 HTML을 가져옵니다. 반환: 실제 URL, HTML, 마지막 오류"""
    import requests

    url = normalize_url_for_fetch(url)
    urls = [url]
    if is_coupang_url(url):
        normalized = normalize_coupang_url(url)
        urls = [normalized]
        if normalized.replace("https://www.coupang.com", "") != normalized:
            urls.append(normalized.replace("https://www.coupang.com", "https://m.coupang.com"))
        if url not in urls:
            urls.append(url)

    desktop_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.coupang.com/",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    mobile_headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-G991N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://m.coupang.com/",
        "Upgrade-Insecure-Requests": "1",
    }
    header_list = [desktop_headers, mobile_headers] if is_coupang_url(url) else [desktop_headers]

    session = requests.Session()
    last_error = ""
    last_response_text = ""
    last_response_url = url
    for candidate_url in urls:
        for headers in header_list:
            try:
                response = session.get(candidate_url, headers=headers, timeout=timeout, allow_redirects=True)
                response.encoding = response.apparent_encoding or response.encoding
                last_response_text = response.text or ""
                last_response_url = response.url or candidate_url
                if response.status_code < 400 and len(last_response_text) > 300:
                    return last_response_url, last_response_text, ""
                last_error = f"HTTP {response.status_code}"
                # 일부 사이트는 403이어도 HTML 안에 최소 정보가 들어오는 경우가 있어 일단 보관합니다.
                if last_response_text and is_coupang_url(candidate_url) and any(k in last_response_text for k in ["상품평", "쿠팡상품번호", "productTitle", "og:title"]):
                    return last_response_url, last_response_text, last_error
            except Exception as exc:
                last_error = str(exc)
                continue
    if last_response_text:
        return last_response_url, last_response_text, last_error
    raise RuntimeError(last_error or "응답이 비어 있습니다.")


def _try_json_loads(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except Exception:
        return None


def _walk_json_values(obj: Any, keys: Tuple[str, ...]) -> List[str]:
    values: List[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key) in keys and isinstance(value, (str, int, float)):
                values.append(str(value))
            values.extend(_walk_json_values(value, keys))
    elif isinstance(obj, list):
        for item in obj:
            values.extend(_walk_json_values(item, keys))
    return values


def _extract_json_ld_product_info(source: str) -> Dict[str, str]:
    """JSON-LD가 있으면 상품명/설명/가격/브랜드/평점 등을 뽑습니다."""
    result: Dict[str, str] = {}
    scripts = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', source, flags=re.I | re.S)
    for script in scripts:
        data = _try_json_loads(html.unescape(script).strip())
        if data is None:
            continue
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            obj_type = obj.get("@type")
            if isinstance(obj_type, list):
                is_product = any(str(x).lower() == "product" for x in obj_type)
            else:
                is_product = str(obj_type).lower() == "product"
            if not is_product and not obj.get("name"):
                continue
            if obj.get("name") and not result.get("상품명"):
                result["상품명"] = clean_text(obj.get("name"))
            if obj.get("description") and not result.get("상품 설명"):
                result["상품 설명"] = clean_text(obj.get("description"))
            brand = obj.get("brand")
            if isinstance(brand, dict) and brand.get("name"):
                result["브랜드"] = clean_text(brand.get("name"))
            offers = obj.get("offers")
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("lowPrice")
                currency = offers.get("priceCurrency")
                if price and not result.get("가격"):
                    result["가격"] = f"{price} {currency or ''}".strip()
            rating = obj.get("aggregateRating")
            if isinstance(rating, dict):
                rating_value = rating.get("ratingValue")
                review_count = rating.get("reviewCount") or rating.get("ratingCount")
                if rating_value:
                    result["평점"] = str(rating_value)
                if review_count:
                    result["리뷰 수"] = str(review_count)
    return result


def _find_first_line(lines: List[str], patterns: List[str]) -> str:
    for line in lines:
        for pattern in patterns:
            if re.search(pattern, line):
                return line
    return ""


def _extract_coupang_product_info(source: str, url: str) -> Dict[str, str]:
    """쿠팡 상품 페이지에서 블로그에 유용한 정보만 정리합니다."""
    info = _extract_json_ld_product_info(source)
    title = clean_text(info.get("상품명", "")) or _extract_title_from_html(source)
    title = re.sub(r"\s*[-|]\s*.*쿠팡.*$", "", title).strip()
    title = title.replace(" | 쿠팡", "").strip()

    description = _extract_meta_content(source, ["og:description", "description", "twitter:description"])
    body_text = _html_to_readable_text(source, limit=10000)
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    joined = " ".join(lines)

    product_no_match = re.search(r"쿠팡상품번호\s*[:：]\s*([0-9\s\-]+)", joined)
    reviews = _find_first_line(lines, [r"[0-9,]+\s*개\s*상품평", r"상품평\s*\([0-9,]+\)"])
    purchase = _find_first_line(lines, [r"한 달간\s*[0-9,]+명\s*이상\s*구매", r"[0-9,]+명\s*이상\s*구매"])
    seller = _find_first_line(lines, [r"판매자\s*[:：]"])
    delivery = _find_first_line(lines, [r"도착", r"무료배송", r"배송"])
    discount = _find_first_line(lines, [r"^[0-9]{1,2}%$", r"[0-9]{1,2}%\s*할인"])

    price_candidates = []
    for m in re.findall(r"\b[0-9]{1,3}(?:,[0-9]{3})+원\b", joined):
        if m not in price_candidates:
            price_candidates.append(m)
    price = price_candidates[0] if price_candidates else clean_text(info.get("가격", ""))
    original_price = price_candidates[1] if len(price_candidates) > 1 else ""

    benefit_keywords = ["쿠폰", "쿠폰할인", "할인", "적립", "쿠팡캐시", "무료배송", "도착", "와우", "반품", "새 상품"]
    benefit_lines: List[str] = []
    for line in lines:
        if any(k in line for k in benefit_keywords) and line not in benefit_lines:
            # 너무 일반적인 푸터/고객센터 문구는 제외
            if any(skip in line for skip in ["고객센터", "이용약관", "문의해주세요", "취소/반품은"]):
                continue
            benefit_lines.append(line)
        if len(benefit_lines) >= 10:
            break

    # 상품명/카테고리 주변의 공개 텍스트를 요약 후보로 보냅니다. 상세설명 이미지의 글자는 사이트 구조상 못 읽을 수 있습니다.
    feature_candidates: List[str] = []
    title_tokens = [tok for tok in re.split(r"\s+", title) if len(tok) >= 2][:8]
    feature_words = set(title_tokens + ["특징", "구성", "용량", "사이즈", "사용", "운동", "헬스", "효과", "상세", "옵션", "상품상세"])
    blacklist = ["전체", "로그인", "회원가입", "장바구니", "고객센터", "최근본상품", "판매자 가입", "입점신청", "이용약관", "개인정보", "Copyright"]
    for line in lines:
        if len(line) < 4 or len(line) > 160:
            continue
        if any(bad in line for bad in blacklist):
            continue
        if any(word and word in line for word in feature_words):
            if line not in feature_candidates:
                feature_candidates.append(line)
        if len(feature_candidates) >= 14:
            break

    product_info_lines = []
    if title:
        product_info_lines.append(f"상품명: {title}")
    if purchase:
        product_info_lines.append(f"구매 반응: {purchase}")
    if reviews:
        product_info_lines.append(f"상품평: {reviews}")
    if discount:
        product_info_lines.append(f"할인 표시: {discount}")
    if price:
        product_info_lines.append(f"판매가/표시가: {price}")
    if original_price:
        product_info_lines.append(f"기존가/비교가 후보: {original_price}")
    if delivery:
        product_info_lines.append(f"배송 정보: {delivery}")
    if seller:
        product_info_lines.append(f"판매자: {seller}")
    if product_no_match:
        product_info_lines.append(f"쿠팡상품번호: {product_no_match.group(1).strip()}")

    if description and description not in product_info_lines:
        product_info_lines.append(f"페이지 설명: {description[:350]}")

    return {
        "상품명 후보": title[:220],
        "페이지 설명": clean_text(description)[:700],
        "쿠팡 상품정보": "\n".join(product_info_lines)[:1800],
        "제품 특징/혜택 후보": "\n".join([f"- {x}" for x in (benefit_lines + feature_candidates) if x])[:2500],
        "본문 일부": body_text[:2600],
    }


def fetch_public_page_summary(url: str, category: str, timeout: int = 12) -> Dict[str, str]:
    """공개 URL의 제목/설명/본문 일부를 읽습니다. 실패해도 앱이 멈추지 않도록 오류를 반환합니다."""
    url = normalize_url_for_fetch(url)
    if not url:
        return {}
    try:
        fetch_url, source, fetch_note = _fetch_html_with_browser_headers(url, timeout=timeout)

        # 네이버 블로그는 실제 본문이 mainFrame 안에 있는 경우가 많아서 한 번 더 따라갑니다.
        if "blog.naver.com" in fetch_url and "mainFrame" in source:
            m = re.search(r'<iframe[^>]+id=["\']mainFrame["\'][^>]+src=["\']([^"\']+)["\']', source, flags=re.I | re.S)
            if m:
                frame_url = html.unescape(m.group(1))
                if frame_url.startswith("/"):
                    frame_url = "https://blog.naver.com" + frame_url
                elif frame_url.startswith("http"):
                    pass
                else:
                    frame_url = "https://blog.naver.com/" + frame_url.lstrip("/")
                try:
                    fetch_url, source, _ = _fetch_html_with_browser_headers(frame_url, timeout=timeout)
                except Exception:
                    pass

        title = _extract_title_from_html(source)
        description = _extract_meta_content(source, ["og:description", "description", "twitter:description"])
        body_text = _html_to_readable_text(source, limit=5000)
        image_urls = _extract_image_urls_from_html(source, fetch_url, limit=10)

        result = {
            "구분": category,
            "URL": url,
            "실제 읽은 URL": fetch_url,
            "페이지 제목": clean_text(title)[:180],
            "페이지 설명": clean_text(description)[:700],
            "본문 일부": body_text[:2600],
            "상세/대표 이미지 URL 후보": image_urls,
        }

        if is_coupang_url(url):
            coupang_info = _extract_coupang_product_info(source, url)
            result.update({k: v for k, v in coupang_info.items() if has_text(v)})
            result["구분"] = category + " · 쿠팡 상품 링크"
            if fetch_note:
                result["읽기 참고"] = f"쿠팡 페이지 응답 참고: {fetch_note}. 공개 HTML에서 읽힌 정보만 반영했습니다."
            if not result.get("쿠팡 상품정보") and not result.get("본문 일부"):
                result["오류"] = "쿠팡이 자동 읽기를 차단했거나 상품 정보가 동적으로 로딩되어 텍스트를 충분히 읽지 못했습니다."

        # 너무 긴 원문 복사를 피하기 위해 앞부분만 참고자료로 넘깁니다.
        return result
    except Exception as exc:
        extra = ""
        if is_coupang_url(url):
            extra = " 쿠팡은 자동 수집을 막거나 상세 설명을 이미지로 제공하는 경우가 있어, 일부 상품은 가격/배송/상품평만 읽히고 상세 이미지 문구는 안 읽힐 수 있습니다."
        return {
            "구분": category,
            "URL": url,
            "오류": f"링크 내용을 읽지 못했습니다: {exc}.{extra}",
        }


def collect_reference_sources(product_links_text: str, review_links_text: str, max_links: int = 8) -> List[Dict[str, str]]:
    """제품/장소 정보 링크와 다른 리뷰어 링크를 모아 참고자료 리스트로 만듭니다."""
    product_links = split_urls(product_links_text)
    review_links = split_urls(review_links_text)
    sources: List[Dict[str, str]] = []
    combined: List[Tuple[str, str]] = []
    combined.extend(("리뷰 대상 공식/판매/정보 링크", url) for url in product_links)
    combined.extend(("다른 리뷰어 블로그/후기 링크", url) for url in review_links)
    for category, url in combined[:max_links]:
        sources.append(fetch_public_page_summary(url, category))
    return sources


def make_reference_section(reference_sources: List[Dict[str, str]]) -> str:
    if not reference_sources:
        return ""
    lines = ["## 링크에서 참고한 정보", ""]
    lines.append("아래 내용은 사용자가 입력한 참고 링크에서 확인한 공개 정보 일부를 바탕으로 정리했습니다.")
    lines.append("")
    for idx, source in enumerate(reference_sources, start=1):
        title = clean_text(source.get("상품명 후보", "")) or clean_text(source.get("페이지 제목", "")) or clean_text(source.get("URL", "")) or f"참고 링크 {idx}"
        category = clean_text(source.get("구분", ""))
        description = clean_text(source.get("페이지 설명", ""))
        coupang_product_info = clean_text(source.get("쿠팡 상품정보", ""))
        product_features = clean_text(source.get("제품 특징/혜택 후보", ""))
        image_candidates = source.get("상세/대표 이미지 URL 후보", []) or []
        if isinstance(image_candidates, str):
            image_candidates = [image_candidates]
        body = clean_text(source.get("본문 일부", ""))
        error = clean_text(source.get("오류", ""))
        note = clean_text(source.get("읽기 참고", ""))
        lines.append(f"- **{category or '참고 링크'} {idx}: {title}**")
        if coupang_product_info:
            short_info = re.sub(r"\n+", " / ", coupang_product_info)[:500]
            lines.append(f"  - 쿠팡 상품정보: {short_info}")
        if product_features:
            short_features = re.sub(r"\n+", " / ", product_features)[:500]
            lines.append(f"  - 특징/혜택 후보: {short_features}")
        if description:
            lines.append(f"  - 요약: {description[:260]}")
        elif body:
            # 템플릿 모드에서는 원문 전체를 복사하지 않고 짧은 정보만 표시합니다.
            short = re.sub(r"\s+", " ", body)[:260]
            lines.append(f"  - 확인된 내용: {short}")
        if note:
            lines.append(f"  - 참고: {note}")
        if error:
            lines.append(f"  - 참고: {error}")
    return "\n".join(lines).strip() + "\n"

# =========================================================
# NVIDIA API 연결
# =========================================================

def build_ai_prompt(payload: Dict[str, Any]) -> str:
    payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"""
너는 네이버 블로그 리뷰 글을 자연스럽게 작성하는 한국어 블로그 작가야.
아래 입력 정보, 참고 링크 자료, 저장된 블로그 템플릿, 템플릿 참고 블로그 링크 분석자료, 첨부된 제품 상세 설명/캡처 이미지를 분석해서 네이버 블로그에 바로 올릴 수 있는 리뷰 글을 작성해줘.

중요 규칙:
- 비어 있거나 입력되지 않은 정보는 절대 추측하지 말고 제외해.
- 저장한 블로그 템플릿이나 템플릿 참고 블로그 링크 분석자료가 있으면, 그 글의 문단 순서·소제목 흐름·도입부 방식·마무리 방식·사진/동영상 배치 방식만 참고해서 작성해.
- 템플릿 참고 링크가 여러 개면 공통적으로 반복되는 구조를 우선 적용해. 예: 도입부 → 제품/장소 기본 정보 → 특징/혜택 → 실제 사용감/방문감 → 아쉬운 점 → 추천 대상 → 총평 → 해시태그.
- 템플릿 링크의 원문 문장, 독특한 표현, 개인 경험은 그대로 베끼지 마. 구조만 따라가고 내용은 사용자가 입력한 리뷰 대상 정보로 새로 작성해.
- 사용자가 직접 저장한 템플릿이 있으면 템플릿 링크보다 우선 적용해.
- 사용자가 넣은 제품/장소 링크와 다른 리뷰어 링크에서 가져온 정보는 참고만 해. 문장을 그대로 베끼지 말고 새 문장으로 자연스럽게 재구성해.
- 공식/판매/지도/업체 링크 정보가 있으면 그 내용을 우선하고, 다른 리뷰어 글은 분위기·장단점·방문 팁을 풍부하게 만드는 보조자료로만 사용해.
- 쿠팡/스마트스토어/판매 페이지 링크에서 상품명, 가격, 할인, 쿠폰, 배송, 판매자, 상품평 수, 구매 반응, 상품번호, 제품 설명, 특징, 혜택이 읽히면 본문에 자연스럽게 반영해.
- 제품 상세 설명 이미지나 상세페이지 이미지가 첨부되어 있으면 이미지 속 문구, 제품 특징, 혜택, 구성품, 사용법, 주의사항을 읽고 본문에 반영해.
- 예를 들어 상세 이미지에 “운동선수가 영상으로 운동방법을 알려준다”처럼 쓰여 있다면, 구매/사용 포인트나 특징 섹션에 자연스럽게 넣어줘.
- 제품 상세 설명이나 상세페이지에서 읽힌 특징/혜택은 “제품 특징”, “혜택/구매 포인트”, “사용 전 참고할 점” 같은 섹션으로 정리해.
- 단, 링크나 이미지에서 읽히지 않은 성능·효능·가격·할인·혜택은 새로 지어내지 마.
- 주소, 주차, 가격, 영업시간, 지도 링크가 있으면 정보 섹션에 보기 좋게 정리해.
- 일반 후기 사진은 사용자가 입력한 사진 설명을 기준으로 글 흐름에 반영해. 제품 상세 설명/캡처 이미지는 분석 자료로 사용해.
- 동영상 설명이 있으면 동영상이 들어갈 위치를 [동영상 1], [동영상 2]처럼 표시하고, 설명을 자연스럽게 반영해.
- 광고/협찬/제품 제공/원고료가 있으면 글 첫 부분에 경제적 이해관계를 명확히 표시해.
- 과장된 광고 문구, 허위 경험, 없는 효능, 없는 성분, 없는 가격, 없는 할인 정보는 만들지 마.
- 제목 후보 목록을 만들지 마. 제목은 딱 1개만 만들어.
- 본문에는 [사진 1], [사진 2], [동영상 1]처럼 사용자가 올린 미디어가 들어갈 위치를 표시해줘.
- 말투와 글 길이는 입력 정보의 작성 옵션을 따라줘.
- 다른 리뷰어 글을 참고하더라도 저작권 문제가 없도록 표현을 새로 구성하고, 긴 문장을 그대로 복사하지 마.

출력 형식:
첫 줄: 실제 네이버 제목으로 쓸 제목 1개만 작성. 절대 '제목:'이라고 쓰지 마.
그 다음 줄부터: 바로 본문 작성. 절대 '본문:'이라고 쓰지 마.
마지막 줄: 해시태그만 작성. 절대 '해시태그:'이라고 쓰지 마.

입력 정보:
{payload_json}
""".strip()

def generate_with_nvidia(
    prompt: str,
    api_key: str,
    base_url: str,
    model: str,
    temperature: float = 0.7,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    image_urls: Optional[List[str]] = None,
    image_data_urls: Optional[List[str]] = None,
) -> str:
    """NVIDIA 예제 코드와 같은 requests 방식으로 호출합니다.
    base_url에는 https://integrate.api.nvidia.com/v1 를 넣고,
    실제 호출은 /chat/completions 로 보냅니다.
    """
    try:
        import requests
    except Exception as exc:
        raise RuntimeError("requests 패키지가 설치되어 있지 않습니다. 먼저 `pip install -r requirements.txt`를 실행하세요.") from exc

    api_key = clean_text(api_key)
    if api_key.lower().startswith("bearer "):
        # 사용자가 .env 또는 입력칸에 Bearer까지 붙여 넣어도 자동으로 제거합니다.
        api_key = api_key.split(None, 1)[1].strip()

    if not api_key or api_key == "YOUR_API_KEY":
        raise RuntimeError("NVIDIA API 키가 입력되지 않았습니다. Streamlit Cloud는 App settings → Secrets에 NVIDIA_API_KEY를 넣고, PC는 .env 파일의 NVIDIA_API_KEY를 실제 nvapi- 키로 바꿔주세요.")

    effective_base_url, _base_note = normalize_nvidia_base_url(base_url)
    effective_base_url = clean_text(effective_base_url).rstrip("/")
    if effective_base_url.endswith("/chat/completions"):
        invoke_url = effective_base_url
    else:
        invoke_url = f"{effective_base_url}/chat/completions"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        safe_max_tokens = int(max_tokens)
    except Exception:
        safe_max_tokens = DEFAULT_MAX_TOKENS
    safe_max_tokens = max(800, min(4096, safe_max_tokens))

    try:
        safe_timeout = int(timeout_seconds)
    except Exception:
        safe_timeout = DEFAULT_TIMEOUT_SECONDS
    safe_timeout = max(60, min(300, safe_timeout))

    user_content: Any = prompt
    image_parts: List[Dict[str, Any]] = []
    for url in (image_urls or [])[:8]:
        url = clean_text(url)
        if url.startswith("http://") or url.startswith("https://"):
            image_parts.append({"type": "image_url", "image_url": {"url": url}})
    for data_url in (image_data_urls or [])[:6]:
        data_url = clean_text(data_url)
        if data_url.startswith("data:image"):
            image_parts.append({"type": "image_url", "image_url": {"url": data_url}})
    if image_parts:
        user_content = [{"type": "text", "text": prompt}] + image_parts

    payload = {
        "model": clean_text(model) or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": "너는 한국어 네이버 블로그 글을 자연스럽게 작성하는 전문 작가야. 너무 장황하게 쓰지 말고 바로 블로그에 붙여넣을 수 있게 완성도 높은 초안을 작성해."},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": safe_max_tokens,
        "temperature": float(temperature),
        "top_p": 0.95,
        "stream": False,
    }

    try:
        # timeout=(연결 제한, 응답 읽기 제한)
        response = requests.post(invoke_url, headers=headers, json=payload, timeout=(20, safe_timeout))
    except requests.exceptions.ReadTimeout as exc:
        raise RuntimeError(
            f"NVIDIA API 응답이 {safe_timeout}초 안에 오지 않아 중단되었습니다. "
            "서버가 느리거나 max_tokens가 너무 클 때 생깁니다. "
            "왼쪽 NVIDIA API 설정에서 'AI 응답 길이 제한'을 1500~2500 정도로 낮춘 뒤 다시 생성해보세요."
        ) from exc
    except requests.exceptions.ConnectTimeout as exc:
        raise RuntimeError("NVIDIA API 서버에 연결하는 시간이 초과되었습니다. 인터넷 연결을 확인한 뒤 다시 시도하세요.") from exc
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(f"NVIDIA API에 연결하지 못했습니다. 인터넷 연결, 방화벽, base_url을 확인하세요. 상세: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"NVIDIA API 요청 중 오류가 발생했습니다. 상세: {exc}") from exc

    if response.status_code >= 400:
        try:
            err = response.json()
        except Exception:
            err = {"detail": response.text[:500]}
        detail = err.get("detail") or err.get("message") or err.get("title") or str(err)
        if response.status_code == 401:
            raise RuntimeError("401 Unauthorized: API 키 인증에 실패했습니다. .env에 Bearer 없이 nvapi- 키만 넣었는지, 새로 발급한 키인지 확인하세요.")
        if response.status_code == 404:
            raise RuntimeError("404 Not Found: base_url은 브라우저 주소가 아니라 API 주소여야 합니다. NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1 로 설정하세요.")
        if response.status_code == 429:
            raise RuntimeError("429: 요청이 너무 많거나 무료 사용량/제한에 걸렸습니다. 잠시 후 다시 시도하거나 새로 발급한 정상 키를 넣어주세요.")
        raise RuntimeError(f"NVIDIA API 오류 {response.status_code}: {detail}")

    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"NVIDIA 응답을 JSON으로 읽지 못했습니다. 응답 일부: {response.text[:500]}") from exc

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"NVIDIA 응답에 choices가 없습니다. 응답 일부: {str(data)[:500]}")

    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
            else:
                parts.append(str(part))
        content = "\n".join([p for p in parts if p.strip()])
    elif content is None:
        content = ""

    content = clean_text(content)
    if not content:
        raise RuntimeError("NVIDIA API 응답은 왔지만 본문 내용이 비어 있습니다. 입력 내용을 조금 더 넣거나 템플릿 생성 모드를 사용해보세요.")
    return content


# =========================================================
# NVIDIA API 상태 진단/키 확인 도우미
# =========================================================
def mask_api_key(api_key: str) -> str:
    """API 키를 화면에 안전하게 표시하기 위한 마스킹 문자열을 만듭니다."""
    key = clean_text(api_key)
    if not key or key == "YOUR_API_KEY":
        return "설정 안 됨"
    if key.lower().startswith("bearer "):
        key = key.split(None, 1)[1].strip()
    if len(key) <= 14:
        return key[:4] + "..." + key[-3:]
    return key[:8] + "..." + key[-6:]


def normalize_api_key_for_request(api_key: str) -> str:
    """사용자가 Bearer까지 붙여 넣어도 실제 요청에는 nvapi- 키만 사용합니다."""
    key = clean_text(api_key)
    if key.lower().startswith("bearer "):
        key = key.split(None, 1)[1].strip()
    return key


def key_source_label(source: str) -> str:
    mapping = {
        "secrets": "Streamlit Secrets",
        "env": ".env / 환경변수",
        "default": "기본값(YOUR_API_KEY)",
        "input": "화면 직접 입력",
    }
    return mapping.get(source, source or "알 수 없음")


def diagnose_nvidia_api(
    api_key: str,
    base_url: str,
    model: str,
    timeout_seconds: int = 45,
) -> Dict[str, Any]:
    """NVIDIA API 키/주소/모델 상태를 짧은 테스트 요청으로 진단합니다."""
    result: Dict[str, Any] = {
        "ok": False,
        "level": "error",
        "title": "진단 전",
        "message": "아직 API 연결 테스트를 실행하지 않았습니다.",
        "suggestion": "API 연결 테스트 버튼을 눌러주세요.",
        "status_code": None,
        "elapsed_seconds": None,
        "base_url": clean_text(base_url),
        "invoke_url": "",
        "model": clean_text(model) or DEFAULT_MODEL,
        "key_masked": mask_api_key(api_key),
        "raw_excerpt": "",
    }

    try:
        import requests
    except Exception as exc:
        result.update({
            "title": "requests 패키지 없음",
            "message": "requests 패키지가 설치되어 있지 않아 API 테스트를 할 수 없습니다.",
            "suggestion": "requirements.txt 설치 또는 `pip install requests`를 실행하세요.",
            "raw_excerpt": str(exc)[:500],
        })
        return result

    key = normalize_api_key_for_request(api_key)
    if not key or key == "YOUR_API_KEY":
        result.update({
            "title": "API 키 미설정",
            "message": "현재 적용된 NVIDIA_API_KEY가 없습니다.",
            "suggestion": "Streamlit Cloud에서는 App settings → Secrets에 NVIDIA_API_KEY를 넣고, PC에서는 .env에 nvapi-로 시작하는 키를 넣어주세요.",
        })
        return result

    if not key.startswith("nvapi-"):
        result.update({
            "level": "warning",
            "title": "API 키 형식 확인 필요",
            "message": "NVIDIA API 키는 보통 nvapi-로 시작합니다. 현재 키 형식이 다릅니다.",
            "suggestion": "NVIDIA에서 새 키를 복사할 때 Bearer 없이 nvapi-로 시작하는 값만 넣었는지 확인하세요. 그래도 테스트는 계속 시도합니다.",
        })

    effective_base_url, base_note = normalize_nvidia_base_url(base_url)
    effective_base_url = clean_text(effective_base_url).rstrip("/")
    if effective_base_url.endswith("/chat/completions"):
        invoke_url = effective_base_url
    else:
        invoke_url = f"{effective_base_url}/chat/completions"
    result["base_url"] = effective_base_url
    result["invoke_url"] = invoke_url
    if base_note:
        result["base_url_note"] = base_note

    try:
        safe_timeout = int(timeout_seconds)
    except Exception:
        safe_timeout = 45
    safe_timeout = max(15, min(90, safe_timeout))

    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {
        "model": clean_text(model) or DEFAULT_MODEL,
        "messages": [
            {"role": "user", "content": "API 연결 테스트입니다. 한국어로 '정상'이라고만 답해줘."}
        ],
        "max_tokens": 32,
        "temperature": 0.1,
        "top_p": 0.95,
        "stream": False,
    }

    start = time.time()
    try:
        response = requests.post(invoke_url, headers=headers, json=payload, timeout=(10, safe_timeout))
        elapsed = round(time.time() - start, 2)
        result["elapsed_seconds"] = elapsed
        result["status_code"] = response.status_code
        text_excerpt = response.text[:900] if response.text else ""
        result["raw_excerpt"] = text_excerpt

        if response.status_code == 200:
            try:
                data = response.json()
            except Exception:
                result.update({
                    "level": "warning",
                    "title": "응답은 왔지만 JSON 파싱 실패",
                    "message": "서버 응답은 200이지만 JSON으로 읽지 못했습니다.",
                    "suggestion": "잠시 후 다시 테스트하거나 NVIDIA 상태를 확인하세요.",
                })
                return result

            choices = data.get("choices") or []
            if choices:
                content = ((choices[0].get("message") or {}).get("content") or "").strip()
                usage = data.get("usage") or {}
                result.update({
                    "ok": True,
                    "level": "success",
                    "title": "API 정상 연결",
                    "message": f"NVIDIA API가 정상 응답했습니다. 응답시간: {elapsed}초",
                    "suggestion": "이 상태면 '생성하기'를 눌러 블로그 글을 만들 수 있습니다. 글 생성이 느리면 AI 응답 길이 제한을 낮춰보세요.",
                    "test_reply": content,
                    "usage": usage,
                })
                return result

            result.update({
                "level": "warning",
                "title": "응답은 왔지만 choices 없음",
                "message": "API 키와 주소는 어느 정도 맞지만, 모델 응답 형식이 예상과 다릅니다.",
                "suggestion": "모델명을 확인하거나 잠시 후 다시 테스트하세요.",
            })
            return result

        # 오류 응답 상세 분석
        detail = ""
        try:
            err = response.json()
            detail = err.get("detail") or err.get("message") or err.get("title") or str(err)
        except Exception:
            detail = response.text[:500]

        status_messages = {
            400: ("요청 형식 또는 모델명 문제", "모델명이 맞는지, payload 형식이 맞는지 확인하세요. NVIDIA_MODEL=minimaxai/minimax-m3 값을 먼저 사용해보세요."),
            401: ("API 키 인증 실패", "키가 틀렸거나 만료됐거나, Bearer를 잘못 넣었을 수 있습니다. 새 nvapi- 키를 발급받아 Secrets 또는 .env에 다시 넣으세요."),
            403: ("권한 또는 모델 접근 문제", "API 키는 맞지만 현재 계정/키가 해당 모델을 사용할 권한이 없을 수 있습니다. NVIDIA 모델 페이지에서 같은 모델로 새 키를 발급해보세요."),
            404: ("API 주소 또는 엔드포인트 문제", "base_url은 https://integrate.api.nvidia.com/v1 로 두세요. 브라우저로 접속하면 404가 뜰 수 있지만 프로그램 호출은 /chat/completions로 보내야 합니다."),
            408: ("요청 시간초과", "잠시 후 다시 시도하거나 API 대기시간을 늘려보세요."),
            429: ("요청 제한 또는 무료 사용량 문제", "짧은 시간에 너무 많이 요청했거나 무료 한도/속도 제한에 걸렸을 수 있습니다. 잠시 후 다시 시도하세요."),
        }
        title, suggestion = status_messages.get(response.status_code, (f"API 오류 {response.status_code}", "오류 상세를 확인하고 base_url, 모델명, API 키 상태를 점검하세요."))
        if 500 <= response.status_code <= 599:
            title = "NVIDIA 서버 측 오류"
            suggestion = "내 설정 문제가 아닐 수 있습니다. 잠시 후 다시 시도하세요."

        result.update({
            "level": "error",
            "title": title,
            "message": detail,
            "suggestion": suggestion,
        })
        return result

    except requests.exceptions.ReadTimeout as exc:
        result.update({
            "level": "error",
            "title": "응답 시간초과",
            "message": f"NVIDIA 서버가 {safe_timeout}초 안에 응답하지 않았습니다.",
            "suggestion": "API 자체가 느리거나 일시적으로 막힌 상태일 수 있습니다. 잠시 후 다시 시도하고, 글 생성 시 max_tokens를 낮춰보세요.",
            "raw_excerpt": str(exc)[:500],
        })
        return result
    except requests.exceptions.ConnectTimeout as exc:
        result.update({
            "level": "error",
            "title": "연결 시간초과",
            "message": "NVIDIA 서버에 연결하는 단계에서 시간이 초과되었습니다.",
            "suggestion": "인터넷 연결, 회사/학교 방화벽, VPN 상태를 확인하세요.",
            "raw_excerpt": str(exc)[:500],
        })
        return result
    except requests.exceptions.ConnectionError as exc:
        result.update({
            "level": "error",
            "title": "연결 실패",
            "message": "NVIDIA API 서버에 연결하지 못했습니다.",
            "suggestion": "인터넷 연결, base_url, 방화벽/VPN을 확인하세요.",
            "raw_excerpt": str(exc)[:500],
        })
        return result
    except Exception as exc:
        result.update({
            "level": "error",
            "title": "알 수 없는 테스트 오류",
            "message": str(exc),
            "suggestion": "오류 내용을 복사해서 확인해보세요.",
            "raw_excerpt": str(exc)[:500],
        })
        return result


def render_api_diagnostic_panel(
    api_key: str,
    api_key_source: str,
    base_url: str,
    effective_base_url: str,
    model: str,
    timeout_seconds: int,
) -> None:
    """사이드바에서 API 상태와 키 확인 UI를 보여줍니다."""
    clean_key = normalize_api_key_for_request(api_key)
    st.subheader("API 상태판")
    st.caption("키/주소/모델이 정상인지 생성 전에 확인할 수 있습니다.")

    st.write(f"키 출처: **{key_source_label(api_key_source)}**")
    st.write(f"키 상태: **{mask_api_key(clean_key)}**")
    if clean_key and clean_key != "YOUR_API_KEY":
        st.text_input(
            "현재 적용 API 키 확인",
            value=clean_key,
            type="password",
            help="오른쪽 눈 아이콘을 누르면 전체 키를 볼 수 있습니다. 공개 Streamlit 앱에서는 다른 사람에게 키가 보일 수 있으니 주의하세요.",
        )
        st.caption("눈 아이콘으로 전체 키를 볼 수 있습니다. 앱을 공개로 배포했다면 이 기능은 끄거나 사용자를 제한하는 것이 안전합니다.")
    else:
        st.warning("현재 적용된 API 키가 없습니다.")

    with st.expander("현재 API 설정 보기", expanded=False):
        st.write(f"base_url: `{effective_base_url or base_url}`")
        invoke_url = (effective_base_url or base_url or "").rstrip("/")
        if invoke_url and not invoke_url.endswith("/chat/completions"):
            invoke_url = invoke_url + "/chat/completions"
        st.write(f"실제 호출 주소: `{invoke_url}`")
        st.write(f"model: `{model}`")

    if st.button("API 연결 테스트", use_container_width=True, key="nvidia_api_diagnose_btn"):
        with st.spinner("NVIDIA API 연결을 테스트하는 중입니다..."):
            st.session_state["nvidia_api_diagnostic"] = diagnose_nvidia_api(
                api_key=clean_key,
                base_url=base_url,
                model=model,
                timeout_seconds=min(90, max(15, int(timeout_seconds or 45))),
            )

    diag = st.session_state.get("nvidia_api_diagnostic")
    if diag:
        level = diag.get("level")
        title = diag.get("title") or "진단 결과"
        message = diag.get("message") or ""
        suggestion = diag.get("suggestion") or ""
        if level == "success":
            st.success(f"{title}\n\n{message}")
        elif level == "warning":
            st.warning(f"{title}\n\n{message}")
        else:
            st.error(f"{title}\n\n{message}")
        if suggestion:
            st.info(suggestion)
        with st.expander("진단 상세 보기", expanded=False):
            safe_diag = dict(diag)
            safe_diag.pop("raw_excerpt", None)
            safe_diag["api_key"] = mask_api_key(clean_key)
            st.json(safe_diag)
            raw = clean_text(diag.get("raw_excerpt", ""))
            if raw:
                st.text_area("서버 응답 일부", value=raw, height=120)


# =========================================================
# 다운로드/백업 도우미
# =========================================================

def make_html_preview(title: str, markdown_text: str, photo_payload: List[Dict[str, str]], video_payload: List[Dict[str, str]]) -> str:
    safe_title = html.escape(title)
    safe_body = html.escape(markdown_text)
    image_blocks = []
    for idx, photo in enumerate(photo_payload, start=1):
        data_url = photo.get("data_url")
        caption = html.escape(photo.get("사진 설명") or photo.get("파일명") or f"사진 {idx}")
        if data_url:
            image_blocks.append(f'<figure><img src="{data_url}" alt="{caption}"/><figcaption>{caption}</figcaption></figure>')
    video_blocks = []
    for idx, video in enumerate(video_payload, start=1):
        caption = html.escape(video.get("동영상 설명") or video.get("파일명") or f"동영상 {idx}")
        filename = html.escape(video.get("파일명", f"video_{idx}"))
        video_blocks.append(f'<div class="video-box"><b>[동영상 {idx}] {caption}</b><br><span>{filename}</span><p>동영상 파일은 첨부 패키지 ZIP의 media/videos 폴더에 들어 있습니다.</p></div>')
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; line-height: 1.8; max-width: 820px; margin: 40px auto; padding: 0 20px; color: #222; word-break: keep-all; }}
    h1 {{ font-size: 30px; margin-bottom: 24px; }}
    pre {{ white-space: pre-wrap; background: #fafafa; border: 1px solid #eee; padding: 22px; border-radius: 14px; font-family: inherit; }}
    img {{ width: 100%; border-radius: 16px; margin-top: 20px; }}
    figcaption {{ color: #666; font-size: 14px; margin-top: 8px; }}
    .video-box {{ background: #f6f8fa; border: 1px solid #e5e7eb; padding: 16px; border-radius: 14px; margin: 16px 0; }}
  </style>
</head>
<body>
  <h1>{safe_title}</h1>
  {''.join(image_blocks)}
  {''.join(video_blocks)}
  <pre>{safe_body}</pre>
</body>
</html>"""


def uploaded_file_bytes(uploaded_file) -> bytes:
    try:
        return uploaded_file.getvalue()
    except Exception:
        try:
            uploaded_file.seek(0)
            return uploaded_file.read()
        except Exception:
            return b""


def make_media_package(
    title: str,
    markdown_text: str,
    payload: Dict[str, Any],
    photo_files: List[Any],
    video_files: List[Any],
    photo_payload: List[Dict[str, str]],
    video_payload: List[Dict[str, str]],
) -> bytes:
    buffer = io.BytesIO()
    used_names = set()
    html_doc = make_html_preview(title, markdown_text, photo_payload, video_payload)
    guide = f"""네이버 블로그 업로드 안내

1. blog_draft.txt 파일을 열어 제목/본문/해시태그를 복사합니다.
2. 네이버 블로그 글쓰기 화면을 엽니다.
3. 본문에 [사진 1], [동영상 1] 같은 위치 표시가 있으면 해당 위치에 media 폴더의 파일을 직접 업로드합니다.
4. 사진 파일은 media/photos 폴더에 있습니다.
5. 동영상 파일은 media/videos 폴더에 있습니다.
6. 최종 발행 전 주소, 가격, 영업시간, 광고/협찬 표기 등을 직접 확인하세요.

생성 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("blog_draft.txt", markdown_text)
        zf.writestr("input_data.json", json.dumps(payload, ensure_ascii=False, indent=2))
        zf.writestr("preview.html", html_doc)
        zf.writestr("naver_upload_guide.txt", guide)
        for idx, file in enumerate(photo_files, start=1):
            original = safe_original_filename(getattr(file, "name", ""), f"photo_{idx}.jpg")
            filename = unique_filename(original, used_names)
            data = uploaded_file_bytes(file)
            if data:
                zf.writestr(f"media/photos/{filename}", data)
        for idx, file in enumerate(video_files, start=1):
            original = safe_original_filename(getattr(file, "name", ""), f"video_{idx}.mp4")
            filename = unique_filename(original, used_names)
            data = uploaded_file_bytes(file)
            if data:
                zf.writestr(f"media/videos/{filename}", data)
    return buffer.getvalue()


def init_state() -> None:
    saved = load_autosave_file()
    st.session_state.setdefault("autosave_fields", saved.get("fields", {}))
    st.session_state.setdefault("autosave_last_saved_at", saved.get("saved_at", ""))
    st.session_state.setdefault("generated_text", saved.get("generated_text", ""))
    st.session_state.setdefault("edited_text", saved.get("edited_text", ""))
    st.session_state.setdefault("selected_title", saved.get("selected_title", ""))
    st.session_state.setdefault("naver_result", "")
    st.session_state.setdefault("last_generation_error", "")
    st.session_state.setdefault("prompt_text", saved.get("prompt_text", ""))
    st.session_state.setdefault("external_ai_text", saved.get("external_ai_text", ""))
    st.session_state.setdefault("last_payload", saved.get("last_payload", {}))
    st.session_state.setdefault("drafts", saved.get("drafts", []))
    st.session_state.setdefault("saved_blog_templates", load_blog_templates())


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="✍️", layout="wide")
    init_state()

    st.title("✍️ 네이버 블로그 자동 작성 도우미")
    st.caption("입력칸을 모두 채우지 않아도 AI가 초안을 만들고, 네이버 블로그처럼 미리보기 후 네이버 글쓰기 화면에 자동 입력합니다.")

    local_ip = get_local_ip()

    with st.sidebar:
        st.header("사용 모드")
        mode_options = [
            "NVIDIA API로 바로 생성",
            "API 없이 템플릿으로 생성",
            "ChatGPT 붙여넣기용 프롬프트 생성",
        ]
        mode = st.radio(
            "생성 방식",
            mode_options,
            index=autosave_index("mode", mode_options, "NVIDIA API로 바로 생성"),
            key="mode",
        )
        st.caption("API 키가 아직 없거나 테스트 중이면 'API 없이 템플릿으로 생성'을 먼저 써보세요.")
        st.divider()

        st.header("NVIDIA API 설정")
        api_key_is_saved = DEFAULT_NVIDIA_API_KEY_SOURCE in ["secrets", "env"] and DEFAULT_NVIDIA_API_KEY != "YOUR_API_KEY"
        if DEFAULT_NVIDIA_API_KEY_SOURCE == "secrets":
            st.success("Streamlit Secrets에서 NVIDIA_API_KEY를 불러왔습니다. 화면에 키를 노출하지 않습니다.")
        elif DEFAULT_NVIDIA_API_KEY_SOURCE == "env":
            st.info("로컬 .env/환경변수에서 NVIDIA_API_KEY를 불러왔습니다.")
        else:
            st.warning("아직 NVIDIA_API_KEY가 설정되지 않았습니다. Streamlit Cloud에서는 Secrets에 넣어주세요.")

        api_key_override = st.text_input(
            "NVIDIA API Key 직접 입력/임시 교체",
            value="",
            type="password",
            placeholder="Secrets 또는 .env에 저장했다면 비워두세요",
            help="Streamlit Cloud에서는 App settings → Secrets에 저장하는 것을 추천합니다. 여기에 입력한 값은 현재 실행 중인 화면에서만 우선 사용합니다.",
        )
        api_key = clean_text(api_key_override) or DEFAULT_NVIDIA_API_KEY
        active_api_key_source = "input" if clean_text(api_key_override) else DEFAULT_NVIDIA_API_KEY_SOURCE
        base_url = st.text_input("base_url", value=autosave_value("base_url", DEFAULT_BASE_URL), key="base_url")
        effective_base_url, base_note = normalize_nvidia_base_url(base_url)
        if base_note:
            st.warning(base_note)
        model = st.text_input("model", value=autosave_value("model", DEFAULT_MODEL), key="model")
        temperature = st.slider("창의성", 0.0, 1.2, 0.7, 0.1)
        max_tokens = st.slider(
            "AI 응답 길이 제한",
            min_value=800,
            max_value=4096,
            value=DEFAULT_MAX_TOKENS,
            step=100,
            help="시간초과가 나면 1500~2500 정도로 낮추세요. 숫자가 클수록 긴 글을 만들지만 응답이 느려질 수 있습니다.",
        )
        timeout_seconds = st.slider(
            "API 대기시간(초)",
            min_value=60,
            max_value=300,
            value=DEFAULT_TIMEOUT_SECONDS,
            step=30,
            help="NVIDIA 서버 응답을 기다리는 시간입니다. 기본값은 240초입니다.",
        )
        st.caption("PC 추천 위치: .env 파일 / 핸드폰 클라우드 추천 위치: Streamlit Cloud Secrets")
        render_api_diagnostic_panel(
            api_key=api_key,
            api_key_source=active_api_key_source,
            base_url=base_url,
            effective_base_url=effective_base_url,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        st.divider()

        st.header("네이버 글쓰기")
        naver_write_url = st.text_input("네이버 글쓰기 URL", value=autosave_value("naver_write_url", DEFAULT_NAVER_WRITE_URL), key="naver_write_url")
        st.markdown(f"[네이버 블로그 글쓰기 열기]({naver_write_url})")
        st.info("네이버 로그인과 최종 발행은 사용자가 직접 합니다. 프로그램은 미리보기 후 제목/본문 자동 입력까지만 수행합니다.")
        st.divider()

        st.header("핸드폰 접속")
        st.write(f"같은 와이파이에서 접속 주소:\n`http://{local_ip}:8501`")
        st.caption("PC에서 프로그램을 켜둔 상태여야 핸드폰 접속이 됩니다.")

    col_input, col_result = st.columns([1.05, 0.95], gap="large")

    with col_input:
        st.subheader("0. 블로그 템플릿")
        st.caption("블로그 링크를 넣으면 먼저 구조를 분석해서 템플릿 미리보기를 보여줍니다. 마음에 들면 저장하고, 그 템플릿으로 새 글을 만들 수 있습니다.")

        saved_templates = load_blog_templates()
        st.session_state["saved_blog_templates"] = saved_templates

        template_reference_links = st.text_area(
            "구조를 분석할 블로그 링크 여러 개",
            value=autosave_value("template_reference_links"),
            placeholder="예:\nhttps://blog.naver.com/...\nhttps://m.blog.naver.com/...\n마음에 드는 블로그 링크를 여러 개 넣으면 공통 구조를 분석합니다.",
            height=110,
            key="template_reference_links",
        )
        st.caption("이 링크는 제품 정보가 아니라 글 구조 참고용입니다. 원문 문장은 복사하지 않고, 문단 순서·소제목 흐름·도입/마무리 방식만 분석합니다.")

        analyzed_template_data = st.session_state.get("analyzed_template_data", {})
        analyzed_template_preview = st.session_state.get("analyzed_template_preview", "")
        analyzed_template_sources = st.session_state.get("template_style_sources", [])

        ac1, ac2 = st.columns([1.25, 1])
        with ac1:
            if st.button("링크 분석해서 템플릿 미리보기", type="secondary", use_container_width=True):
                template_urls = split_urls(template_reference_links)
                if not template_urls:
                    st.warning("분석할 블로그 링크를 먼저 입력해주세요.")
                else:
                    with st.spinner("블로그 링크의 구조를 분석하는 중입니다..."):
                        template_style_sources = collect_template_style_sources(template_reference_links, max_links=6)
                    analyzed_template_data = make_template_from_analyzed_links(template_style_sources)
                    analyzed_template_preview = render_template_preview_markdown(analyzed_template_data, template_style_sources)
                    st.session_state["template_style_sources"] = template_style_sources
                    st.session_state["analyzed_template_data"] = analyzed_template_data
                    st.session_state["analyzed_template_preview"] = analyzed_template_preview
                    st.session_state["last_analyzed_template_links"] = "\n".join(template_urls)
                    if not clean_text(st.session_state.get("template_save_name", "")):
                        st.session_state["template_save_name"] = f"링크분석템플릿_{datetime.now().strftime('%m%d_%H%M')}"
                    failed_template_count = sum(1 for item in template_style_sources if item.get("오류"))
                    if failed_template_count:
                        st.info(f"템플릿 링크 {len(template_urls)}개 중 {failed_template_count}개는 구조를 읽지 못했습니다. 읽힌 링크만 미리보기에 반영했습니다.")
                    else:
                        st.success("블로그 구조 분석이 완료되었습니다. 아래 미리보기를 확인해주세요.")
        with ac2:
            st.info("추천 흐름: 링크 입력 → 미리보기 확인 → 마음에 들면 저장 → 글 생성")

        use_analyzed_template = False
        if analyzed_template_data:
            with st.container(border=True):
                st.markdown("#### 네이버 블로그 적용 모습 미리보기")
                st.caption("원문 블로그의 실제 제목/소제목을 그대로 보여주지 않고, 저장할 템플릿 양식처럼 제목 자리·부제목 자리·본문 자리로 보여줍니다.")
                visual_template_html = render_visual_template_preview_html(analyzed_template_data, analyzed_template_sources)
                st.components.v1.html(visual_template_html, height=680, scrolling=True)
                with st.expander("분석 요약 보기", expanded=False):
                    st.markdown(analyzed_template_preview or render_template_preview_markdown(analyzed_template_data, analyzed_template_sources))
                use_analyzed_template = st.checkbox(
                    "이번 글 생성에 이 분석 템플릿 바로 적용",
                    value=bool(st.session_state.get("use_analyzed_template", True)),
                    key="use_analyzed_template",
                )
                save_cols = st.columns([1.3, 1])
                with save_cols[0]:
                    template_save_name = st.text_input(
                        "이 분석 결과를 저장할 템플릿 이름",
                        value=autosave_value("template_save_name", st.session_state.get("template_save_name", "")),
                        placeholder="예: 운동기구 리뷰형 / 맛집 상세후기형",
                        key="template_save_name",
                    )
                with save_cols[1]:
                    st.write("")
                    st.write("")
                    if st.button("미리보기 템플릿 저장", type="primary", use_container_width=True):
                        ok, msg = save_blog_template(template_save_name, analyzed_template_data)
                        if ok:
                            st.success(msg)
                            st.session_state["saved_blog_templates"] = load_blog_templates()
                        else:
                            st.warning(msg)
        else:
            template_save_name = st.text_input(
                "저장할 템플릿 이름",
                value=autosave_value("template_save_name"),
                placeholder="링크 분석 후 자동으로 채워지거나, 직접 입력 템플릿 저장 때 사용됩니다.",
                key="template_save_name",
            )

        with st.expander("저장한 템플릿 불러오기 / 직접 템플릿 작성", expanded=False):
            template_names = ["선택 안 함"] + sorted(saved_templates.keys())
            selected_template_name = st.selectbox(
                "저장된 템플릿 불러오기",
                template_names,
                index=autosave_index("selected_template_name", template_names, "선택 안 함"),
                key="selected_template_name",
            )
            selected_template_data = saved_templates.get(selected_template_name, {}) if selected_template_name != "선택 안 함" else {}
            if selected_template_data:
                st.info("저장된 템플릿을 선택했습니다. 위의 분석 템플릿 바로 적용을 끄면 이 저장 템플릿이 적용됩니다.")
                with st.expander("선택한 템플릿 내용 보기", expanded=False):
                    st.code(summarize_template_data(selected_template_data) or json.dumps(selected_template_data, ensure_ascii=False, indent=2), language="markdown")

            st.markdown("**직접 템플릿을 만들고 싶을 때만 아래 칸을 작성하세요.**")
            t1, t2 = st.columns(2)
            with t1:
                template_title_rule = st.text_input("제목 규칙", value=autosave_value("template_title_rule"), placeholder="예: 키워드 + 솔직 후기 + 추천 대상", key="template_title_rule")
                template_opening_rule = st.text_area("도입부 규칙", value=autosave_value("template_opening_rule"), placeholder="예: 공감 문장으로 시작 → 사용/방문 계기 → 한 줄 총평", height=78, key="template_opening_rule")
                template_photo_video_rule = st.text_area("사진/동영상 배치 규칙", value=autosave_value("template_photo_video_rule"), placeholder="예: 도입 후 대표사진, 특징 설명 뒤 사용 영상, 총평 전 비교 사진", height=78, key="template_photo_video_rule")
            with t2:
                template_section_structure = st.text_area("본문 구조/소제목 순서", value=autosave_value("template_section_structure"), placeholder="예:\n1. 한 줄 총평\n2. 제품 기본 정보\n3. 특징과 혜택\n4. 실제 사용감\n5. 아쉬운 점\n6. 추천 대상\n7. 총평", height=154, key="template_section_structure")
                template_closing_rule = st.text_area("마무리 규칙", value=autosave_value("template_closing_rule"), placeholder="예: 추천 대상 정리 → 재구매/재방문 의사 → 주의할 점", height=78, key="template_closing_rule")
                template_hashtag_rule = st.text_input("해시태그 규칙", value=autosave_value("template_hashtag_rule"), placeholder="예: 메인키워드 1개 + 지역/제품 키워드 + 후기 키워드 총 8~10개", key="template_hashtag_rule")
                template_avoid_rule = st.text_input("템플릿에서 피할 표현", value=autosave_value("template_avoid_rule"), placeholder="예: 과장 광고, 무조건 추천, 인생템", key="template_avoid_rule")

            template_memo = st.text_area("템플릿 메모/고정 문구", value=autosave_value("template_memo"), placeholder="고정으로 넣고 싶은 안내 문구나 말투 메모가 있으면 입력", height=80, key="template_memo")

            bt1, bt2 = st.columns(2)
            with bt1:
                if st.button("직접 입력한 템플릿 저장", use_container_width=True):
                    ok, msg = save_blog_template(template_save_name, {
                        "title_rule": template_title_rule,
                        "opening_rule": template_opening_rule,
                        "section_structure": template_section_structure,
                        "photo_video_rule": template_photo_video_rule,
                        "closing_rule": template_closing_rule,
                        "hashtag_rule": template_hashtag_rule,
                        "avoid_rule": template_avoid_rule,
                        "memo": template_memo,
                    })
                    if ok:
                        st.success(msg)
                    else:
                        st.warning(msg)
                    st.session_state["saved_blog_templates"] = load_blog_templates()
            with bt2:
                if selected_template_name != "선택 안 함" and st.button("선택한 템플릿 삭제", use_container_width=True):
                    ok, msg = delete_blog_template(selected_template_name)
                    if ok:
                        st.success(msg)
                        st.session_state["selected_template_name"] = "선택 안 함"
                    else:
                        st.warning(msg)
                    st.session_state["saved_blog_templates"] = load_blog_templates()
                    st.rerun()

        manual_template_data = compact_dict({
            "title_rule": template_title_rule,
            "opening_rule": template_opening_rule,
            "section_structure": template_section_structure,
            "photo_video_rule": template_photo_video_rule,
            "closing_rule": template_closing_rule,
            "hashtag_rule": template_hashtag_rule,
            "avoid_rule": template_avoid_rule,
            "memo": template_memo,
        })
        if use_analyzed_template and analyzed_template_data:
            active_template_data = analyzed_template_data
            selected_template_name = "링크 분석 미리보기 템플릿"
        else:
            active_template_data = selected_template_data or manual_template_data

        st.divider()
        st.subheader("1. 기본 정보")
        c1, c2 = st.columns(2)
        with c1:
            review_type = st.selectbox("리뷰 유형", REVIEW_TYPES, index=autosave_index("review_type", REVIEW_TYPES, "선택 안 함"), key="review_type")
            subject_name = st.text_input("제품명 또는 장소명", value=autosave_value("subject_name"), placeholder="예: 광교 ○○카페 / ○○ 선스틱", key="subject_name")
            one_line = st.text_input("한 줄 요약", value=autosave_value("one_line"), placeholder="예: 조용하고 사진 잘 나오는 광교 브런치 카페", key="one_line")
            visit_date = st.text_input("방문/사용 날짜", value=autosave_value("visit_date"), placeholder="예: 2026.07.01", key="visit_date")
        with c2:
            disclosure = st.selectbox("광고/협찬 여부", DISCLOSURES, index=autosave_index("disclosure", DISCLOSURES, "선택 안 함"), key="disclosure")
            brand = st.text_input("브랜드/업체명", value=autosave_value("brand"), placeholder="선택", key="brand")
            category = st.text_input("세부 카테고리", value=autosave_value("category"), placeholder="예: 브런치, 선크림, 파스타, 피부관리", key="category")
            must_include = st.text_area("꼭 넣을 문장", value=autosave_value("must_include"), placeholder="반드시 들어갔으면 하는 표현이 있다면 입력", height=84, key="must_include")

        st.subheader("2. 장소/제품 정보")
        i1, i2 = st.columns(2)
        with i1:
            address = st.text_input("주소", value=autosave_value("address"), placeholder="예: 경기 수원시 영통구 ...", key="address")
            map_link = st.text_input("네이버지도/지도 링크", value=autosave_value("map_link"), placeholder="지도 공유 URL", key="map_link")
            parking = st.text_input("주차 정보", value=autosave_value("parking"), placeholder="예: 건물 지하주차장 2시간 무료", key="parking")
            hours = st.text_input("영업시간/운영시간", value=autosave_value("hours"), placeholder="예: 매일 10:00~21:00", key="hours")
            reservation = st.text_input("예약/문의", value=autosave_value("reservation"), placeholder="예: 네이버예약 가능 / 전화번호", key="reservation")
        with i2:
            price = st.text_area("가격/메뉴/옵션", value=autosave_value("price"), placeholder="예: 아메리카노 4,500원\n브런치 세트 12,000원", height=116, key="price")
            homepage = st.text_input("홈페이지/SNS", value=autosave_value("homepage"), placeholder="인스타그램, 공식 홈페이지 등", key="homepage")
            nearby = st.text_input("주변 정보", value=autosave_value("nearby"), placeholder="예: 광교중앙역 도보 5분", key="nearby")
            product_specs = st.text_area("제품 스펙/구성/용량", value=autosave_value("product_specs"), placeholder="제품 리뷰일 때 성분, 용량, 구성품 등", height=80, key="product_specs")

        st.subheader("3. 참고 링크")
        st.caption("제품/장소 공식 페이지, 판매 페이지, 쿠팡/스마트스토어 링크, 지도, 다른 블로그 리뷰 링크를 여러 개 넣으면 글 생성 시 참고합니다. 링크는 한 줄에 하나씩 넣으면 가장 좋습니다.")
        product_info_links = st.text_area(
            "리뷰할 제품/장소 정보 링크 여러 개",
            value=autosave_value("product_info_links"),
            placeholder="예:\nhttps://smartstore.naver.com/...\nhttps://map.naver.com/...\nhttps://www.instagram.com/...",
            height=92,
            key="product_info_links",
        )
        reference_review_links = st.text_area(
            "참고할 다른 블로그/리뷰 링크 여러 개",
            value=autosave_value("reference_review_links"),
            placeholder="예:\nhttps://blog.naver.com/...\nhttps://m.blog.naver.com/...",
            height=92,
            key="reference_review_links",
        )
        st.info("쿠팡/판매 페이지는 상품명·가격·할인·쿠폰·배송·상품평·제품 특징/혜택을 가능한 범위에서 읽습니다. 다른 리뷰어 글은 그대로 복사하지 않고, 분위기·장단점·방문 팁을 참고해서 새 글로 재구성합니다.")

        st.subheader("3-1. 제품 상세 설명/캡처 이미지")
        st.caption("상세페이지에 제품 설명이 이미지로 들어가 있으면, 캡처 이미지를 여기에 올려주세요. NVIDIA API 모드에서는 이미지 속 문구와 특징도 분석해서 글에 반영합니다.")
        uploaded_detail_images = st.file_uploader(
            "제품 상세 설명 이미지 또는 상세페이지 캡처 여러 장 업로드",
            type=IMAGE_EXTENSIONS,
            accept_multiple_files=True,
            key="detail_image_uploader",
        )
        detail_image_payload: List[Dict[str, str]] = []
        detail_image_data_urls: List[str] = []
        if uploaded_detail_images:
            for idx, uploaded in enumerate(uploaded_detail_images, start=1):
                with st.expander(f"상세 설명 이미지 {idx}: {uploaded.name}", expanded=False):
                    st.image(uploaded, width=260)
                    note = st.text_input(
                        f"상세 설명 이미지 {idx} 메모",
                        key=f"detail_image_note_{idx}_{uploaded.name}",
                        placeholder="예: 사용법 설명 / 혜택 설명 / 구성품 설명 / 운동방법 안내",
                    )
                    data_url = image_to_data_url(uploaded, max_side=1600, quality=86)
                    if data_url:
                        detail_image_data_urls.append(data_url)
                    detail_image_payload.append({"파일명": uploaded.name, "메모": note})

        st.subheader("4. 후기 내용")
        h1, h2 = st.columns(2)
        with h1:
            features = st.text_area("특징 키워드", value=autosave_value("features"), placeholder="예: 깔끔함, 감성적, 가성비, 고급스러움", height=90, key="features")
            strengths = st.text_area("좋았던 점", value=autosave_value("strengths"), placeholder="예: 조용함, 직원 친절, 양이 많음", height=100, key="strengths")
            recommended_for = st.text_area("추천 대상", value=autosave_value("recommended_for"), placeholder="예: 데이트, 부모님과 식사, 혼카페", height=80, key="recommended_for")
        with h2:
            weakness = st.text_area("아쉬운 점", value=autosave_value("weakness"), placeholder="예: 주말 웨이팅, 좌석 간격 좁음", height=90, key="weakness")
            experience = st.text_area("실제 경험/에피소드", value=autosave_value("experience"), placeholder="현장에서 느낀 점, 사용감, 기억나는 장면 등", height=100, key="experience")
            avoid_words = st.text_area("빼고 싶은 표현", value=autosave_value("avoid_words"), placeholder="예: 무조건 추천, 인생템, 대박 등", height=80, key="avoid_words")

        st.subheader("5. 사진/동영상 업로드")
        uploaded_photos = st.file_uploader("사진 여러 장 업로드", type=IMAGE_EXTENSIONS, accept_multiple_files=True, key="photo_uploader")
        photo_payload: List[Dict[str, str]] = []
        if uploaded_photos:
            st.caption("사진은 미리보기와 첨부 패키지에 들어갑니다. AI 글에는 아래에 입력한 사진 설명이 반영됩니다.")
            for idx, uploaded in enumerate(uploaded_photos, start=1):
                with st.expander(f"사진 {idx}: {uploaded.name}", expanded=False):
                    st.image(uploaded, width=260)
                    caption = st.text_input(
                        f"사진 {idx} 설명",
                        key=f"caption_{idx}_{uploaded.name}",
                        placeholder="예: 입구 외관 / 대표 메뉴 / 제품 패키지 / 사용 전후",
                    )
                    data_url = image_to_data_url(uploaded)
                    photo_payload.append({"파일명": uploaded.name, "사진 설명": caption, "data_url": data_url or ""})

        uploaded_videos = st.file_uploader("동영상 여러 개 업로드", type=VIDEO_EXTENSIONS, accept_multiple_files=True, key="video_uploader")
        video_payload: List[Dict[str, str]] = []
        if uploaded_videos:
            st.caption("동영상은 AI가 직접 분석하지는 않습니다. 설명을 적으면 글 흐름에 [동영상 1] 위치로 반영됩니다.")
            for idx, uploaded in enumerate(uploaded_videos, start=1):
                with st.expander(f"동영상 {idx}: {uploaded.name}", expanded=False):
                    try:
                        st.video(uploaded)
                    except Exception:
                        st.info("이 형식은 브라우저 미리보기가 안 될 수 있지만, 첨부 패키지에는 저장됩니다.")
                    v_caption = st.text_input(
                        f"동영상 {idx} 설명",
                        key=f"video_caption_{idx}_{uploaded.name}",
                        placeholder="예: 매장 내부 분위기 / 제품 발림성 / 음식이 나오는 장면",
                    )
                    video_payload.append({
                        "파일명": uploaded.name,
                        "동영상 설명": v_caption,
                        "파일 크기": f"{len(uploaded_file_bytes(uploaded)) / (1024 * 1024):.1f}MB",
                    })

        st.subheader("6. SEO/작성 옵션")
        s1, s2 = st.columns(2)
        with s1:
            main_keyword = st.text_input("메인 키워드", value=autosave_value("main_keyword"), placeholder="예: 광교 브런치 카페", key="main_keyword")
            sub_keywords = st.text_area("서브 키워드", value=autosave_value("sub_keywords"), placeholder="예: 광교 데이트, 수원 카페 추천", height=75, key="sub_keywords")
            tags = st.text_input("희망 태그", value=autosave_value("tags"), placeholder="#광교카페 #수원브런치", key="tags")
        with s2:
            tone = st.selectbox("말투", TONE_OPTIONS, index=autosave_index("tone", TONE_OPTIONS, "자연스러운 일상체"), key="tone")
            length_options = ["보통", "짧게", "길게", "상세하게"]
            length = st.selectbox("글 길이", length_options, index=autosave_index("length", length_options, "보통"), key="length")
            title_style_options = ["검색형", "감성형", "후기형", "정보형", "솔직리뷰형"]
            title_style = st.selectbox("제목 스타일", title_style_options, index=autosave_index("title_style", title_style_options, "검색형"), key="title_style")
            cta = st.text_input("마무리 유도 문장", value=autosave_value("cta"), placeholder="예: 근처 가시면 한 번 들러보세요", key="cta")

        payload = compact_dict({
            "기본 정보": {
                "리뷰 유형": review_type,
                "제품명 또는 장소명": subject_name,
                "한 줄 요약": one_line,
                "방문/사용 날짜": visit_date,
                "광고/협찬 여부": disclosure,
                "브랜드/업체명": brand,
                "세부 카테고리": category,
                "꼭 넣을 문장": must_include,
            },
            "장소/제품 정보": {
                "주소": address,
                "지도 링크": map_link,
                "주차 정보": parking,
                "영업시간/운영시간": hours,
                "예약/문의": reservation,
                "가격/메뉴/옵션": price,
                "홈페이지/SNS": homepage,
                "주변 정보": nearby,
                "제품 스펙/구성/용량": product_specs,
            },
            "참고 링크": {
                "리뷰할 제품/장소 정보 링크": split_urls(product_info_links),
                "참고할 다른 블로그/리뷰 링크": split_urls(reference_review_links),
            },
            "블로그 템플릿": {
                "선택한 저장 템플릿 이름": selected_template_name,
                "저장/직접 입력 템플릿 내용": active_template_data,
                "구조 참고 블로그 링크": split_urls(template_reference_links),
            },
            "후기 내용": {
                "특징 키워드": features,
                "좋았던 점": strengths,
                "아쉬운 점": weakness,
                "추천 대상": recommended_for,
                "실제 경험/에피소드": experience,
                "빼고 싶은 표현": avoid_words,
            },
            "사진 정보": [{k: v for k, v in p.items() if k != "data_url"} for p in photo_payload],
            "제품 상세 설명/캡처 이미지": detail_image_payload,
            "동영상 정보": video_payload,
            "SEO": {
                "메인 키워드": main_keyword,
                "서브 키워드": sub_keywords,
                "희망 태그": tags,
            },
            "작성 옵션": {
                "말투": tone,
                "글 길이": length,
                "제목 스타일": title_style,
                "마무리 유도 문장": cta,
            },
        })

        autosave_fields = {
            "mode": mode,
            "base_url": effective_base_url,
            "model": model,
            "naver_write_url": naver_write_url,
            "review_type": review_type,
            "subject_name": subject_name,
            "one_line": one_line,
            "visit_date": visit_date,
            "disclosure": disclosure,
            "brand": brand,
            "category": category,
            "must_include": must_include,
            "address": address,
            "map_link": map_link,
            "parking": parking,
            "hours": hours,
            "reservation": reservation,
            "price": price,
            "homepage": homepage,
            "nearby": nearby,
            "product_specs": product_specs,
            "product_info_links": product_info_links,
            "reference_review_links": reference_review_links,
            "selected_template_name": selected_template_name,
            "template_save_name": template_save_name,
            "template_title_rule": template_title_rule,
            "template_opening_rule": template_opening_rule,
            "template_section_structure": template_section_structure,
            "template_photo_video_rule": template_photo_video_rule,
            "template_closing_rule": template_closing_rule,
            "template_hashtag_rule": template_hashtag_rule,
            "template_avoid_rule": template_avoid_rule,
            "template_memo": template_memo,
            "template_reference_links": template_reference_links,
            "features": features,
            "strengths": strengths,
            "recommended_for": recommended_for,
            "weakness": weakness,
            "experience": experience,
            "avoid_words": avoid_words,
            "main_keyword": main_keyword,
            "sub_keywords": sub_keywords,
            "tags": tags,
            "tone": tone,
            "length": length,
            "title_style": title_style,
            "cta": cta,
        }

        st.caption("💾 입력값은 자동 임시저장됩니다. 새로고침해도 대부분의 입력칸이 복원됩니다. 단, 사진/동영상 파일은 브라우저 보안상 다시 선택해야 할 수 있습니다.")
        if st.button("입력값 임시저장 초기화", use_container_width=True):
            clear_autosave_file()
            for key in ["autosave_fields", "generated_text", "edited_text", "selected_title", "prompt_text", "external_ai_text", "last_payload", "drafts"]:
                st.session_state.pop(key, None)
            st.success("임시저장 값을 초기화했습니다. 화면을 새로고침하면 빈 상태로 시작합니다.")
            st.rerun()

        st.divider()
        generate = st.button("생성하기", type="primary", use_container_width=True)
        if generate:
            if not payload:
                st.warning("최소한 제품명/장소명, 특징, 주소, 사진 설명, 동영상 설명 중 하나는 입력해주세요.")
            else:
                payload_for_generation = json.loads(json.dumps(payload, ensure_ascii=False))
                link_count = len(split_urls(product_info_links)) + len(split_urls(reference_review_links))
                template_link_count = len(split_urls(template_reference_links))
                reference_image_urls: List[str] = []
                if link_count:
                    with st.spinner("입력한 참고 링크 내용을 읽는 중입니다..."):
                        reference_sources = collect_reference_sources(product_info_links, reference_review_links, max_links=8)
                    payload_for_generation["링크에서 가져온 참고자료"] = reference_sources
                    st.session_state["reference_sources"] = reference_sources
                    failed_count = sum(1 for item in reference_sources if item.get("오류"))
                    for item in reference_sources:
                        imgs = item.get("상세/대표 이미지 URL 후보", []) or []
                        if isinstance(imgs, str):
                            imgs = [imgs]
                        for img_url in imgs:
                            if img_url not in reference_image_urls:
                                reference_image_urls.append(img_url)
                    if failed_count:
                        st.info(f"참고 링크 {link_count}개 중 {failed_count}개는 내용을 읽지 못했습니다. 읽힌 링크만 글 생성에 반영합니다.")
                    if reference_image_urls and mode == "NVIDIA API로 바로 생성":
                        st.info(f"링크에서 찾은 상세/대표 이미지 후보 {min(len(reference_image_urls), 8)}장을 AI 분석에 함께 보냅니다.")
                else:
                    st.session_state["reference_sources"] = []

                if template_link_count:
                    current_template_links_key = "\n".join(split_urls(template_reference_links))
                    cached_template_sources = st.session_state.get("template_style_sources", [])
                    cached_template_links_key = clean_text(st.session_state.get("last_analyzed_template_links", ""))
                    if cached_template_sources and cached_template_links_key == current_template_links_key:
                        template_style_sources = cached_template_sources
                    else:
                        with st.spinner("블로그 템플릿 링크의 구조를 분석하는 중입니다..."):
                            template_style_sources = collect_template_style_sources(template_reference_links, max_links=6)
                        st.session_state["template_style_sources"] = template_style_sources
                        st.session_state["last_analyzed_template_links"] = current_template_links_key
                    payload_for_generation["블로그 템플릿 링크 분석자료"] = template_style_sources
                    failed_template_count = sum(1 for item in template_style_sources if item.get("오류"))
                    if failed_template_count:
                        st.info(f"템플릿 링크 {template_link_count}개 중 {failed_template_count}개는 구조를 읽지 못했습니다. 읽힌 링크만 반영합니다.")
                else:
                    st.session_state["template_style_sources"] = []
                    st.session_state["last_analyzed_template_links"] = ""

                if detail_image_data_urls and mode == "NVIDIA API로 바로 생성":
                    st.info(f"업로드한 상세 설명/캡처 이미지 {min(len(detail_image_data_urls), 6)}장을 AI 분석에 함께 보냅니다.")

                st.session_state["last_payload"] = payload_for_generation
                prompt = build_ai_prompt(payload_for_generation)
                st.session_state["prompt_text"] = prompt
                st.session_state["last_generation_error"] = ""

                try:
                    if mode == "NVIDIA API로 바로 생성":
                        with st.spinner("NVIDIA API로 블로그 초안을 생성하는 중입니다..."):
                            generated = generate_with_nvidia(
                                prompt,
                                api_key,
                                base_url,
                                model,
                                temperature,
                                max_tokens=max_tokens,
                                timeout_seconds=timeout_seconds,
                                image_urls=reference_image_urls,
                                image_data_urls=detail_image_data_urls,
                            )
                        st.success("NVIDIA API로 초안을 생성했습니다.")
                    elif mode == "API 없이 템플릿으로 생성":
                        generated = generate_template_review(payload_for_generation)
                        st.success("API 없이 템플릿 초안을 생성했습니다.")
                    else:
                        # 프롬프트 생성 모드에서도 사용자가 바로 확인할 수 있게 템플릿 미리보기를 같이 만듭니다.
                        generated = generate_template_review(payload_for_generation)
                        st.success("ChatGPT 붙여넣기용 프롬프트와 템플릿 미리보기를 생성했습니다.")

                except Exception as exc:
                    # API 키 오류/한도 오류가 나도 화면이 비어 있지 않도록 템플릿 미리보기를 자동 생성합니다.
                    st.session_state["last_generation_error"] = str(exc)
                    generated = generate_template_review(payload_for_generation)
                    st.warning(f"NVIDIA API 생성은 실패했지만, 미리보기 확인용 템플릿 초안을 대신 만들었습니다. 오류: {exc}")

                if not clean_text(generated):
                    generated = generate_template_review(payload_for_generation)
                    st.session_state["last_generation_error"] = "생성 결과가 비어 있어서 템플릿 초안으로 대체했습니다."
                    st.warning("생성 결과가 비어 있어서 템플릿 초안으로 대체했습니다.")

                st.session_state["generated_text"] = generated
                st.session_state["edited_text"] = strip_title_candidate_block(generated)
                st.session_state["selected_title"] = extract_best_title(generated)
                st.session_state["drafts"].append({
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "mode": mode,
                    "payload": payload,
                    "content": generated,
                })
                # Streamlit이 다음 영역을 즉시 갱신하도록 한 번 다시 실행합니다.
                st.rerun()

    with col_result:
        st.subheader("미리보기 / 결과")
        tabs = st.tabs(["네이버식 미리보기", "수정/작성하기", "AI 프롬프트", "외부 AI 결과 붙여넣기", "백업/첨부 패키지"])

        with tabs[0]:
            generated_text = st.session_state.get("generated_text", "")
            if st.session_state.get("last_generation_error"):
                st.warning("최근 NVIDIA API 생성 오류가 있어서 템플릿 미리보기를 표시 중입니다. API 키를 고치면 다시 NVIDIA 생성 결과를 받을 수 있습니다.")
                with st.expander("오류 내용 보기"):
                    st.code(st.session_state.get("last_generation_error"))
            if generated_text:
                current_text = st.session_state.get("edited_text") or strip_title_candidate_block(generated_text)
                current_title = st.session_state.get("selected_title") or extract_best_title(generated_text)
                preview_html = markdown_to_naver_preview_html(current_text, current_title, photo_payload, video_payload, uploaded_videos or [])
                st.components.v1.html(preview_html, height=760, scrolling=True)
                st.caption("실제 네이버 블로그와 100% 동일하진 않지만, 제목/본문/사진/동영상 배치가 어떻게 보일지 확인하는 용도입니다.")
                if st.session_state.get("reference_sources"):
                    with st.expander("이번 글에 참고한 링크 정보 보기"):
                        for item in st.session_state.get("reference_sources", []):
                            imgs = item.get("상세/대표 이미지 URL 후보", []) or []
                            if isinstance(imgs, str):
                                imgs = [imgs]
                            img_note = f" / 이미지 후보 {len(imgs)}장" if imgs else ""
                            st.write(f"- {item.get('구분', '참고 링크')}: {item.get('페이지 제목') or item.get('URL')}{img_note}")
                if st.session_state.get("template_style_sources"):
                    with st.expander("이번 글에 참고한 템플릿 구조 보기"):
                        for item in st.session_state.get("template_style_sources", []):
                            st.write(f"- {item.get('페이지 제목') or item.get('URL')}: {item.get('톤 힌트', '')}")
                            if item.get("소제목 후보"):
                                st.caption("소제목 후보: " + " → ".join(item.get("소제목 후보", [])[:6]))
                            if item.get("오류"):
                                st.caption(item.get("오류"))
            else:
                st.info("생성하기를 누르면 네이버 블로그 스타일 미리보기가 표시됩니다. 입력칸을 전부 채우지 않아도 작성할 수 있습니다.")

        with tabs[1]:
            generated_text = st.session_state.get("generated_text", "")
            if generated_text:
                title_candidates = extract_title_candidates(generated_text)
                default_title = st.session_state.get("selected_title") or extract_best_title(generated_text)
                if title_candidates:
                    picked = st.selectbox("제목 후보 선택", title_candidates, index=0 if default_title not in title_candidates else title_candidates.index(default_title))
                    st.session_state["selected_title"] = picked
                selected_title = st.text_input("네이버에 들어갈 제목", value=st.session_state.get("selected_title") or default_title)
                edited_text = st.text_area("네이버에 들어갈 본문 - 여기서 직접 수정 가능", value=st.session_state.get("edited_text") or strip_title_candidate_block(generated_text), height=430)
                st.session_state["selected_title"] = selected_title
                st.session_state["edited_text"] = edited_text
                st.divider()
                st.warning("핸드폰 단독/Streamlit Cloud 버전은 보안상 네이버 편집기 안에 자동으로 글을 주입할 수 없습니다. 대신 제목/본문을 복사하고 네이버 글쓰기 화면을 바로 여는 방식으로 사용하세요. PC버전은 Chrome 자동입력을 지원합니다.")
                st.markdown(f"[네이버 글쓰기 화면 열기]({naver_write_url})")
                copy_button("제목 복사", selected_title, "copy_title")
                copy_button("본문 복사", edited_text, "copy_edited_body")
                st.caption("핸드폰에서는 제목 복사 → 네이버 글쓰기 제목칸 붙여넣기 → 본문 복사 → 본문칸 붙여넣기 순서로 쓰면 됩니다. 사진/동영상은 네이버 앱이나 모바일 웹 편집기에서 직접 업로드하세요.")
            else:
                st.info("먼저 왼쪽에서 정보를 입력하고 생성하기를 눌러주세요.")

        with tabs[2]:
            prompt_text = st.session_state.get("prompt_text", "")
            if prompt_text:
                st.write("아래 내용을 다른 AI에 그대로 붙여넣어도 같은 입력값으로 블로그 글을 받을 수 있습니다.")
                copy_button("프롬프트 복사", prompt_text, "copy_prompt")
                st.text_area("AI에 붙여넣을 프롬프트", prompt_text, height=420)
            else:
                st.info("왼쪽에서 입력 후 생성하기를 누르면 프롬프트가 만들어집니다.")

        with tabs[3]:
            st.write("다른 AI에서 생성한 결과를 여기에 붙여넣으면 미리보기/네이버 작성/다운로드 기능을 그대로 쓸 수 있습니다.")
            external_text = st.text_area("외부 AI 결과 붙여넣기", value=st.session_state.get("external_ai_text", ""), height=260)
            if st.button("붙여넣은 결과를 미리보기로 사용", use_container_width=True):
                if external_text.strip():
                    st.session_state["external_ai_text"] = external_text
                    st.session_state["generated_text"] = external_text
                    st.session_state["edited_text"] = strip_title_candidate_block(external_text)
                    st.session_state["selected_title"] = extract_best_title(external_text)
                    st.success("붙여넣은 결과를 초안 미리보기로 적용했습니다.")
                else:
                    st.warning("먼저 AI 결과를 붙여넣어주세요.")

        with tabs[4]:
            generated_text = st.session_state.get("generated_text", "")
            final_text = st.session_state.get("edited_text") or strip_title_candidate_block(generated_text)
            final_title = st.session_state.get("selected_title") or extract_best_title(generated_text)
            if generated_text:
                file_stem = make_safe_filename(final_title)
                package = make_media_package(final_title, final_text, st.session_state.get("last_payload", {}), uploaded_photos or [], uploaded_videos or [], photo_payload, video_payload)
                preview_html = markdown_to_naver_preview_html(final_text, final_title, photo_payload, video_payload, uploaded_videos or [])
                st.download_button("본문 TXT 다운로드", data=final_text.encode("utf-8"), file_name=f"{file_stem}.txt", mime="text/plain", use_container_width=True)
                st.download_button("네이버식 HTML 미리보기 다운로드", data=preview_html.encode("utf-8"), file_name=f"{file_stem}_preview.html", mime="text/html", use_container_width=True)
                st.download_button(
                    "본문+사진+동영상 첨부 패키지 ZIP 다운로드",
                    data=package,
                    file_name=f"{file_stem}_첨부패키지.zip",
                    mime="application/zip",
                    use_container_width=True,
                )
                st.caption("이 ZIP 안에 blog_draft.txt, preview.html, input_data.json, media/photos, media/videos가 들어갑니다.")
            else:
                st.info("초안을 먼저 생성하면 사진/동영상까지 묶은 첨부 패키지를 다운로드할 수 있습니다.")

            with st.expander("입력 데이터 JSON 확인", expanded=False):
                st.json(st.session_state.get("last_payload", {}))

            if st.session_state.get("drafts"):
                draft_json = json.dumps(st.session_state["drafts"], ensure_ascii=False, indent=2)
                st.download_button("작성 기록 JSON 다운로드", data=draft_json.encode("utf-8"), file_name="review_blog_drafts.json", mime="application/json", use_container_width=True)


    try:
        save_autosave_snapshot(autosave_fields, st.session_state.get("last_payload", payload))
    except Exception:
        pass


if __name__ == "__main__":
    main()
