"""ARIA Engine - Context Analyzer (규칙 기반)

외부 대화 로그에서 인사이트를 추출하는 규칙 기반 분석기
LLM 호출 0 — 순수 Python 패턴 매칭 + 키워드 추출

설계 원칙:
    - LLM 호출 극히 최소화 (규칙 기반으로 가능한 로직에 LLM 안 붙임)
    - 패턴 매칭 + 키워드 기반 분류
    - 추출 실패해도 원본 데이터는 이벤트로 저장 (비차단)
"""

from __future__ import annotations

import re
from collections import Counter

import structlog

from aria.context.types import (
    AnalysisResult,
    ConversationMessage,
    ExtractedInsight,
)

logger = structlog.get_logger()


# === 패턴 정의 ===

# 결정사항 패턴 (한국어 + 영어)
_DECISION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(으로|로)\s*(결정|확정|가자|하자|진행)", re.IGNORECASE),
    re.compile(r"(하기로|하는\s*걸로|하는\s*것으로)\s*(했|하자|합의)", re.IGNORECASE),
    re.compile(r"최종\s*(결정|선택|확정)", re.IGNORECASE),
    re.compile(r"(decided|let's go with|final decision|agreed)", re.IGNORECASE),
    re.compile(r"(채택|선택했|선택함|확정함)", re.IGNORECASE),
]

# 선호 패턴
_PREFERENCE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(선호|좋겠|낫겠|이\s*게\s*더|이\s*쪽이|말고)", re.IGNORECASE),
    re.compile(r"(prefer|better|rather|instead of)", re.IGNORECASE),
    re.compile(r"(항상|매번|습관적으로|보통)\s+.{2,20}(으로|를|을)", re.IGNORECASE),
]

# TODO 패턴
_TODO_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(해야\s*(한다|함|됨|할|돼)|필요함|필요하다|해줘야)", re.IGNORECASE),
    re.compile(r"(다음에|나중에|추후|향후)\s+.{2,40}(하자|해야|필요)", re.IGNORECASE),
    re.compile(r"(TODO|FIXME|HACK|XXX)[\s:]+", re.IGNORECASE),
    re.compile(r"(need to|should|must|have to)\s+", re.IGNORECASE),
    re.compile(r"(잊지\s*말고|꼭|반드시)\s+.{2,30}", re.IGNORECASE),
]

# 학습/발견 패턴
_LEARNING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(알게\s*됐|알았다|발견했|찾았다|원인[은이])", re.IGNORECASE),
    re.compile(r"(근본\s*원인|root\s*cause|해결\s*방법|원인\s*파악)", re.IGNORECASE),
    re.compile(r"(learned|found out|realized|turns out)", re.IGNORECASE),
    re.compile(r"(이유[는가]|때문이|때문에|결국)", re.IGNORECASE),
]

# 문제/이슈 패턴
_ISSUE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(에러|오류|버그|실패|안\s*됨|안\s*돼|작동\s*안)", re.IGNORECASE),
    re.compile(r"(error|bug|fail|broken|crash|exception)", re.IGNORECASE),
    re.compile(r"(문제[가는]|이슈[가는]|장애|다운)", re.IGNORECASE),
]

# 사실/정보 패턴 (기술적 사실)
_FACT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(공식\s*문서|documentation|spec|사양)", re.IGNORECASE),
    re.compile(r"(버전\s*\d|v\d+\.\d+|API\s*키|엔드포인트)", re.IGNORECASE),
    re.compile(r"(포트\s*\d+|localhost:\d+|https?://)", re.IGNORECASE),
]

# 카테고리별 패턴 매핑
_CATEGORY_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "decision": _DECISION_PATTERNS,
    "preference": _PREFERENCE_PATTERNS,
    "todo": _TODO_PATTERNS,
    "learning": _LEARNING_PATTERNS,
    "issue": _ISSUE_PATTERNS,
    "fact": _FACT_PATTERNS,
}

# 토픽 키워드 → 도메인명 매핑
_TOPIC_KEYWORDS: dict[str, list[str]] = {
    "coding": ["코드", "코딩", "함수", "변수", "타입", "클래스", "모듈", "import",
               "code", "function", "class", "module", "api", "endpoint", "deploy"],
    "devops": ["서버", "배포", "도커", "docker", "nginx", "vps", "ssh", "ci/cd",
               "deploy", "server", "container", "kubernetes", "k8s"],
    "design": ["디자인", "UI", "UX", "레이아웃", "컴포넌트", "색상", "폰트",
               "design", "layout", "component", "tailwind", "css"],
    "business": ["매출", "비용", "수익", "마케팅", "고객", "전략", "KPI",
                 "revenue", "cost", "marketing", "strategy", "customer"],
    "product": ["기능", "요구사항", "스펙", "MVP", "로드맵", "피드백",
                "feature", "requirement", "spec", "roadmap", "feedback"],
    "debugging": ["버그", "에러", "디버깅", "수정", "패치", "핫픽스",
                  "bug", "error", "debug", "fix", "patch", "hotfix"],
}

# 요약 생성용: 불필요 접두사 제거
_STRIP_PREFIXES: list[str] = [
    "네, ", "네 ", "알겠습니다. ", "좋습니다. ", "물론이죠. ",
    "Sure, ", "Of course. ", "Absolutely. ", "Got it. ",
]


def analyze_conversation(
    messages: list[ConversationMessage],
    max_insights: int = 20,
) -> AnalysisResult:
    """대화 로그 규칙 기반 분석

    Args:
        messages: 대화 메시지 목록
        max_insights: 추출할 최대 인사이트 수

    Returns:
        AnalysisResult (인사이트 + 요약 + 토픽)
    """
    if not messages:
        return AnalysisResult()

    insights: list[ExtractedInsight] = []
    all_text = ""
    user_count = 0

    for idx, msg in enumerate(messages):
        if msg.role == "user":
            user_count += 1

        # system 메시지는 분석 대상에서 제외
        if msg.role == "system":
            continue

        all_text += msg.content + "\n"

        # 사용자 + 어시스턴트 메시지에서 인사이트 추출
        extracted = _extract_insights_from_message(msg.content, idx)
        insights.extend(extracted)

        if len(insights) >= max_insights:
            insights = insights[:max_insights]
            break

    # 중복 제거 (유사 내용)
    insights = _deduplicate_insights(insights)

    # 주요 토픽 결정
    primary_topic = _detect_primary_topic(all_text)

    # 요약 생성 (규칙 기반)
    summary = _generate_summary(messages, primary_topic)

    return AnalysisResult(
        insights=insights,
        summary=summary,
        primary_topic=primary_topic,
        message_count=len(messages),
        user_message_count=user_count,
        total_chars=len(all_text),
    )


def _extract_insights_from_message(
    content: str,
    message_index: int,
) -> list[ExtractedInsight]:
    """단일 메시지에서 인사이트 추출"""
    results: list[ExtractedInsight] = []

    # 짧은 메시지는 스킵 (인사/짧은 확인 등)
    if len(content.strip()) < 15:
        return results

    # 문장 단위로 분리
    sentences = _split_sentences(content)

    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 10:
            continue

        for category, patterns in _CATEGORY_PATTERNS.items():
            for pattern in patterns:
                if pattern.search(sentence):
                    # 문장을 요약 길이로 제한
                    trimmed = _trim_to_summary(sentence, max_len=300)
                    results.append(ExtractedInsight(
                        category=category,
                        content=trimmed,
                        confidence=0.8,  # 패턴 매칭 기본 신뢰도
                        source_index=message_index,
                    ))
                    break  # 한 문장에서 한 카테고리만

    return results


def _split_sentences(text: str) -> list[str]:
    """텍스트를 문장 단위로 분리"""
    # 줄바꿈 기반 분리 (코드/목록 구조)
    lines = text.split("\n")
    sentences: list[str] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 마크다운 헤더/목록 아이템 → 개별 문장 취급
        if line.startswith(("#", "-", "*", "•", ">")):
            # 접두사 제거
            cleaned = re.sub(r"^[#\-*•>\s]+", "", line).strip()
            if cleaned:
                sentences.append(cleaned)
            continue

        # 마침표/물음표/느낌표로 분리
        parts = re.split(r"(?<=[.?!])\s+", line)
        sentences.extend(p.strip() for p in parts if p.strip())

    return sentences


def _trim_to_summary(text: str, max_len: int = 300) -> str:
    """텍스트를 요약 길이로 제한"""
    text = text.strip()

    # 불필요 접두사 제거
    for prefix in _STRIP_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break

    if len(text) <= max_len:
        return text

    # 단어 경계에서 자르기
    truncated = text[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > max_len * 0.5:
        truncated = truncated[:last_space]

    return truncated + "..."


def _detect_primary_topic(text: str) -> str:
    """전체 텍스트에서 주요 토픽 감지"""
    text_lower = text.lower()
    scores: Counter[str] = Counter()

    for topic, keywords in _TOPIC_KEYWORDS.items():
        for keyword in keywords:
            count = text_lower.count(keyword.lower())
            if count > 0:
                scores[topic] += count

    if not scores:
        return "general"

    top_topic, top_score = scores.most_common(1)[0]
    # 최소 2회 이상 등장해야 유의미
    if top_score < 2:
        return "general"

    return top_topic


def _generate_summary(
    messages: list[ConversationMessage],
    topic: str,
) -> str:
    """규칙 기반 대화 요약 생성"""
    if not messages:
        return ""

    # 첫 번째 사용자 메시지에서 요약 추출
    first_user = ""
    for msg in messages:
        if msg.role == "user":
            first_user = msg.content.strip()
            break

    if not first_user:
        return f"{topic} 관련 대화 ({len(messages)}개 메시지)"

    # 첫 줄만 추출 + 제한
    first_line = first_user.split("\n")[0].strip()
    summary = _trim_to_summary(first_line, max_len=120)

    return summary


def _deduplicate_insights(
    insights: list[ExtractedInsight],
    similarity_threshold: float = 0.7,
) -> list[ExtractedInsight]:
    """유사 인사이트 중복 제거 (단순 접두사 비교)"""
    if len(insights) <= 1:
        return insights

    unique: list[ExtractedInsight] = []
    seen_prefixes: set[str] = set()

    for insight in insights:
        # 앞 50자를 핑거프린트로 사용
        prefix = insight.content[:50].lower().strip()
        if prefix not in seen_prefixes:
            seen_prefixes.add(prefix)
            unique.append(insight)

    return unique


def build_memory_content(
    analysis: AnalysisResult,
    source: str,
    existing_content: str | None = None,
) -> str:
    """분석 결과를 메모리 토픽 콘텐츠 마크다운으로 변환

    기존 콘텐츠가 있으면 append 방식으로 병합

    Args:
        analysis: 분석 결과
        source: 외부 도구 출처
        existing_content: 기존 메모리 토픽 콘텐츠 (있으면 병합)

    Returns:
        메모리 토픽용 마크다운 문자열
    """
    if not analysis.has_insights:
        return existing_content or ""

    # 새 인사이트를 카테고리별 그룹핑
    by_category: dict[str, list[str]] = {}
    for insight in analysis.insights:
        by_category.setdefault(insight.category, []).append(insight.content)

    # 마크다운 생성
    sections: list[str] = []

    category_labels: dict[str, str] = {
        "decision": "결정사항",
        "preference": "선호/방침",
        "fact": "사실 정보",
        "todo": "할 일",
        "learning": "학습/발견",
        "issue": "문제/이슈",
    }

    for cat, items in by_category.items():
        label = category_labels.get(cat, cat)
        section = f"## {label}\n"
        for item in items:
            section += f"- {item}\n"
        sections.append(section)

    new_block = f"### [{source}] {analysis.summary}\n\n" + "\n".join(sections)

    if existing_content:
        # 기존 콘텐츠 뒤에 추가 (최대 크기 제한)
        merged = existing_content.rstrip() + "\n\n---\n\n" + new_block
        # 50KB 제한
        if len(merged.encode("utf-8")) > 50_000:
            # 오래된 콘텐츠 앞부분 잘라내기
            while len(merged.encode("utf-8")) > 45_000:
                # 첫 번째 섹션 구분자까지 제거
                sep_idx = merged.find("\n---\n", 10)
                if sep_idx == -1:
                    break
                merged = merged[sep_idx + 5:]
            merged = "(이전 내용 일부 생략)\n\n" + merged
        return merged

    return f"# 외부 컨텍스트 ({source})\n\n{new_block}"
