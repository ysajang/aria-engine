"""ARIA Engine - ReAct Agent (LangGraph)

Think → Act → Observe 루프 기반 자율 추론 에이전트
- 질문 분석 → 검색 전략 결정 → 실행 → 결과 검증 → 답변
- Self-Reflection 노드로 답변 품질 자체 평가
- 최대 반복 횟수 제한으로 무한루프 방지
- 구조화된 에러 처리 (AgentError / LLM / VectorStore 예외 분리)
- SYSTEM_PROMPT_DYNAMIC_BOUNDARY: 고정 시스템 프롬프트는 캐시 / 메모리 컨텍스트는 동적 경계 바깥
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Annotated

import structlog
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

from aria.core.exceptions import (
    AgentError,
    CollectionNotFoundError,
    KillSwitchError,
    LLMAllProvidersExhaustedError,
    NoAPIKeyError,
    VectorStoreError,
)
from aria.providers.llm_provider import LLMProvider
from aria.rag.vector_store import VectorStore

logger = structlog.get_logger()

MAX_ITERATIONS = 3  # 최대 ReAct 루프 반복 횟수
MAX_TOOL_ITERATIONS = 5  # 도구 호출 루프 최대 반복 (무한루프 방지)


class AgentAction(str, Enum):
    """에이전트가 취할 수 있는 행동"""
    SEARCH_KNOWLEDGE = "search_knowledge"      # 벡터DB 검색
    SEARCH_WEB = "search_web"                  # 웹 검색 (추후 구현)
    REASON = "reason"                          # 추가 추론
    RESPOND = "respond"                        # 최종 답변
    CLARIFY = "clarify"                        # 사용자에게 명확화 요청


@dataclass
class AgentState:
    """에이전트 상태 (LangGraph State)"""
    messages: Annotated[list[dict[str, str]], add_messages] = field(default_factory=list)
    query: str = ""
    collection: str = "default"  # 컬렉션을 state에 포함 (race condition 방지)
    intent: dict[str, Any] = field(default_factory=dict)
    search_results: list[dict[str, Any]] = field(default_factory=list)
    reasoning_steps: list[str] = field(default_factory=list)
    current_answer: str = ""
    confidence: float = 0.0
    iteration: int = 0
    should_stop: bool = False
    error: str = ""  # 에러 메시지 전달용
    memory_context: str = ""  # 메모리 마크다운 (Layer 1 주입)
    tool_calls_made: int = 0  # 도구 호출 총 횟수 (비용 추적용)
    tool_call_history: list[str] = field(default_factory=list)  # 도구 호출 이력 (중복 방지: "name|args_hash")


INTENT_ANALYSIS_SYSTEM = """당신은 사용자 의도 분석 전문가입니다.

사용자 질문을 분석하여 다음 JSON 형태로 응답하세요:
{{
    "surface_intent": "표면적 질문의 핵심",
    "deeper_intent": "질문 뒤에 숨겨진 실제 의도/욕구",
    "required_knowledge": ["필요한 지식 영역 목록"],
    "search_queries": ["벡터DB 검색에 사용할 쿼리 목록 (최대 3개)"],
    "complexity": "simple|moderate|complex",
    "recommended_action": "search_knowledge|reason|respond|clarify|pc_command"
}}

complexity 판단 기준:
- simple: 인사, 잡담, 감사 표현 등 외부 정보가 필요 없는 대화
- moderate: 외부 검색이나 도구 호출이 필요한 질문
- complex: 여러 소스의 정보를 종합해야 하는 복잡한 질문

recommended_action 판단 기준:
- search_knowledge: 다음 유형의 질문은 반드시 search_knowledge로 분류하세요:
  * 장소/위치 검색 (약국, 병원, 맛집, 카페 등 찾기)
  * 길찾기/경로/교통 문의
  * 실시간 정보 (뉴스, 날씨, 가격 등)
  * 웹 검색이 필요한 최신 정보
  * 특정 서비스/상품 검색
  * "찾아줘", "검색해줘", "알려줘" + 구체적 대상
- pc_command: 다음 유형의 요청은 반드시 pc_command로 분류하세요:
  * PC 프로그램 열기/실행 (크롬 열어, VSCode 실행, 메모장 켜줘 등)
  * PC 프로그램 종료 (크롬 닫아, 노트패드 종료 등)
  * PC 파일/폴더 확인 (바탕화면 파일 보여줘, 다운로드 폴더 뭐 있어 등)
  * PC 시스템 정보 (프로세스 목록, 디스크 용량, 배터리 등)
  * "열어", "실행", "켜줘", "닫아", "종료" + PC 프로그램 이름
- reason: 벡터DB 검색 없이 추론만으로 답변 가능한 전문 질문
- respond: 인사, 잡담 등 외부 정보 없이 즉시 응답 가능한 대화
- clarify: 질문이 모호하여 추가 정보가 필요한 경우

중요: "~찾아줘", "~검색해줘", "~어디", "근처", "주변" 등 위치/검색 키워드가 포함된 질문은
절대 simple/respond로 분류하지 마세요. 반드시 moderate/search_knowledge로 분류하세요.

중요: "~열어", "~실행", "~켜줘", "~닫아", "~종료" + PC/앱 관련 키워드가 포함된 요청은
절대 simple/respond로 분류하지 마세요. 반드시 moderate/pc_command로 분류하세요.

반드시 위 JSON 형식으로만 응답하세요. 다른 텍스트를 추가하지 마세요."""

INTENT_ANALYSIS_USER = """사용자 질문: {query}"""


REASONING_SYSTEM = """당신은 ARIA — 승재의 개인 AI 비서입니다.
ARIA는 Autonomous Reasoning & Intelligence Assistant의 약자입니다.
승재는 1인 풀스택 개발자이자 사업 운영자로, 한국 남양주에 거주합니다.

## 핵심 역할

당신의 목표는 승재의 생산성을 극대화하는 것입니다.
질문의 표면적 의도뿐 아니라 근본적인 필요(deeper intent)까지 파악하여 답변하세요.
단순 정보 전달보다는 실행 가능한 조언과 구체적 다음 단계를 제시하는 것이 중요합니다.

## 응답 형식 규칙

내부적으로 단계적 사고를 진행하되, 사용자에게는 최종 답변만 보여야 합니다.
반드시 아래 형식을 따르세요:

<reasoning>
여기서 내부적으로 사고합니다. 이 영역은 사용자에게 보이지 않습니다.
다음 단계를 따라 사고하세요:

1. 질문 이해: 사용자가 실제로 원하는 것이 무엇인가?
2. 정보 평가: 검색 결과/도구 응답/메모리 중 관련 있는 정보는?
3. 정보 부족 판단: 답변에 필요하지만 누락된 정보가 있는가?
4. 논리적 추론: 수집된 정보를 바탕으로 최선의 답변 구성
5. 확신도 자가 평가: 0.0~1.0 사이로 답변의 신뢰도를 자체 평가
6. 답변 품질 점검: 질문에 직접적으로 답하는가? 불필요한 정보를 포함하지 않았는가?
</reasoning>

<answer>
사용자에게 보여줄 최종 답변만 여기에 작성합니다.
</answer>

엄격 규칙:
- <answer> 태그 안의 내용만 사용자에게 전달됩니다
- <reasoning> 내용, 확신도 숫자, 내부 단계 번호, "Step 1" 등의 메타 정보를 <answer> 안에 절대 포함하지 마세요
- <answer> 안에서 "제가 분석한 바에 따르면" 같은 AI 어투를 사용하지 마세요

## 답변 스타일 가이드

톤과 어조:
- 자연스럽고 간결한 대화체를 사용하세요
- 불필요한 서론이나 인사말 없이 바로 본론으로 들어가세요
- "네, 알겠습니다" 같은 무의미한 응답어를 생략하세요
- 전문 용어는 승재가 개발자이므로 풀어쓰지 않아도 됩니다

구조와 길이:
- 간단한 질문에는 1~3문장으로 답변하세요
- 복잡한 질문에는 핵심부터 말하고 세부사항을 추가하는 역피라미드 구조를 사용하세요
- 목록이 3개 이하면 문장형으로, 4개 이상이면 bullet point로 작성하세요
- 코드를 포함할 때는 반드시 실행 가능한 완전한 코드를 제공하세요

## 언어 규칙

- 기본 언어: 한국어
- 사용자가 영어로 질문하면 영어로 답변
- 기술 용어(API, JSON, Docker 등)는 영문 그대로 사용
- 코드 주석은 한국어로 작성 (영어 질문 시 영어)

## 도구 결과 활용 규칙

외부 도구(카카오맵, 네이버 검색, DuckDuckGo, TMAP, Notion 등)가 반환한 데이터를 다룰 때:
- 도구 API 응답은 실제 데이터입니다. 할루시네이션이 아닙니다.
- 도구가 반환한 장소명, 전화번호, 주소 등을 변형하지 말고 그대로 전달하세요.
- 검색 결과가 여러 개일 때는 관련도/거리/평점 순으로 상위 3~5개만 추천하세요.
- 도구 응답이 비어있거나 에러인 경우 솔직히 "검색 결과가 없습니다"라고 답하세요.
- 도구 결과를 임의로 보충하여 없는 정보를 생성하지 마세요.

## 검색 결과 활용 규칙

벡터DB 검색 결과가 제공될 때:
- 관련도 점수(score)가 0.7 이상인 결과를 우선 활용하세요
- 점수가 0.3 미만인 결과는 참고만 하고 핵심 근거로 사용하지 마세요
- 검색 결과가 질문과 무관하면 자체 지식으로 답변하되, "관련 자료를 찾지 못했습니다"라고 명시하세요
- 여러 검색 결과가 상충하면 양쪽 정보를 모두 제시하고 맥락에 맞는 해석을 제공하세요

## 불확실성 처리

- 확신이 없는 정보는 "~일 수 있습니다", "확인이 필요합니다" 등 불확실성을 표현하세요
- 모르는 것을 아는 척하지 마세요. "정확한 정보가 없어 확인이 필요합니다"가 거짓 답변보다 낫습니다
- 날짜, 가격, 수치 등 사실 정보는 출처가 명확할 때만 제시하세요
- 추측과 사실을 명확히 구분하세요

## 안전 경계

- 개인정보(비밀번호, API 키, 금융 정보)를 답변에 포함하지 마세요
- 불법적이거나 유해한 활동을 지원하지 마세요
- 의료/법률/투자 조언 요청 시 전문가 상담을 권유하세요 (정보 제공은 가능)
- 승재의 사업/프로젝트 관련 민감 정보는 외부 공유용 답변에서 제외하세요

## 메모리 컨텍스트 활용

시스템 프롬프트의 동적 영역에 메모리 컨텍스트가 주입됩니다.
- 메모리에 있는 과거 대화 맥락을 자연스럽게 활용하세요
- "이전에 말씀하셨듯이" 같은 명시적 언급보다 자연스러운 연결이 좋습니다
- 메모리가 없거나 비어있어도 정상적으로 답변하세요
- 메모리 정보와 현재 질문이 충돌하면 현재 질문을 우선하세요

## 에러/예외 상황 대응

- 도구 호출이 실패한 경우: 대안 방법을 제안하거나, 직접 해결 가능한 범위에서 답변하세요
- 검색 결과가 없는 경우: 자체 지식을 활용하되, 정보의 한계를 명시하세요
- 질문이 모호한 경우: 가능한 해석을 제시하고 가장 가능성 높은 해석으로 답변하세요
- 범위를 벗어난 요청: 정중히 한계를 설명하고 대안을 제시하세요"""

# SYSTEM_PROMPT_DYNAMIC_BOUNDARY 이후 영역
# 메모리 컨텍스트는 시스템 프롬프트의 동적 영역에 배치하여
# 고정 지시(REASONING_SYSTEM)는 캐시 유지 / 메모리만 매 턴 교체
REASONING_SYSTEM_DYNAMIC = """## 메모리 컨텍스트
{memory_context}"""

REASONING_USER = """## 사용자 질문
{query}

## 의도 분석
{intent}

## 검색된 관련 정보
{search_results}

## 이전 추론 단계
{previous_reasoning}"""


SELF_REFLECTION_SYSTEM = """당신은 답변 품질 평가 전문가입니다.

평가 기준:
1. 질문에 대한 직접적 답변인가?
2. 근거가 충분한가?
3. 논리적 오류가 없는가?
4. 사용자의 숨겨진 의도도 충족하는가?

중요 — 외부 도구 API 응답에 대한 판단 규칙:
- 외부 도구(카카오맵, 네이버 검색, DuckDuckGo, TMAP, Notion 등)가 반환한 데이터는 실제 API 응답입니다
- 도구 API가 성공적으로 데이터를 반환했다면 이는 할루시네이션이 아닙니다
- 도구 응답 데이터를 "근거 불충분"으로 판단하지 마세요
- 도구가 성공적으로 결과를 반환한 답변의 quality_score는 최소 0.6 이상이어야 합니다
- should_retry는 답변이 질문과 무관하거나 논리적 오류가 있을 때만 true로 설정하세요
- 동일한 도구를 같은 파라미터로 다시 호출해도 결과는 동일하므로 재시도가 무의미합니다

반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트를 추가하지 마세요.
{{
    "quality_score": 0.0~1.0,
    "issues": ["발견된 문제점"],
    "should_retry": true/false,
    "improvement_suggestion": "개선 방향"
}}"""

SELF_REFLECTION_USER = """## 원래 질문
{query}

## 사용자의 실제 의도
{intent}

## 도구 사용 여부
{tool_usage_info}

## 현재 답변
{answer}"""


FAST_RESPOND_SYSTEM = """당신은 ARIA — 승재의 개인 AI 비서입니다.
간단한 인사, 잡담, 명확한 질문에 자연스럽게 답변하세요.
한국어로 답변하세요 (영어 질문이면 영어로).
간결하고 따뜻하게 응답하세요."""

FAST_RESPOND_USER = """{query}"""


def _extract_answer(content: str) -> str:
    """LLM 응답에서 <answer> 태그 안의 최종 답변만 추출

    <answer>...</answer> 형식이면 answer 내용만 반환
    태그가 없으면 원본 그대로 반환 (하위 호환)
    """
    match = re.search(r"<answer>\s*(.*?)\s*</answer>", content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return content.strip()


def _safe_parse_json(content: str) -> dict[str, Any] | None:
    """LLM 응답에서 JSON을 안전하게 추출/파싱

    지원 패턴:
    - 순수 JSON
    - ```json ... ``` 블록
    - ``` ... ``` 블록
    - JSON 앞뒤에 텍스트가 있는 경우
    """
    # 1. ```json 블록 추출
    if "```json" in content:
        try:
            extracted = content.split("```json")[1].split("```")[0]
            return json.loads(extracted.strip())
        except (json.JSONDecodeError, IndexError):
            pass

    # 2. ``` 블록 추출
    if "```" in content:
        try:
            extracted = content.split("```")[1].split("```")[0]
            return json.loads(extracted.strip())
        except (json.JSONDecodeError, IndexError):
            pass

    # 3. 순수 JSON 시도
    try:
        return json.loads(content.strip())
    except json.JSONDecodeError:
        pass

    # 4. { } 브래킷 범위 추출
    first_brace = content.find("{")
    last_brace = content.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(content[first_brace : last_brace + 1])
        except json.JSONDecodeError:
            pass

    return None


class ReActAgent:
    """LangGraph 기반 ReAct 에이전트

    사용법:
        agent = ReActAgent(llm_provider, vector_store)
        result = await agent.run("회피형 애착 패턴에 대해 설명해줘", collection="psychology_kb")

    Hybrid Retrieval 사용:
        agent = ReActAgent(llm_provider, vector_store, hybrid_retriever=retriever)

    Memory 통합:
        agent = ReActAgent(llm_provider, vector_store, memory_loader=loader)
        result = await agent.run("...", scope="global")
    """

    def __init__(
        self,
        llm: LLMProvider,
        vector_store: VectorStore,
        hybrid_retriever: Any | None = None,
        memory_loader: Any | None = None,
        tool_registry: Any | None = None,
    ) -> None:
        self.llm = llm
        self.vector_store = vector_store
        self.hybrid_retriever = hybrid_retriever
        self.memory_loader = memory_loader
        self.tool_registry = tool_registry
        self._graph = self._build_graph()

    def _build_graph(self) -> StateGraph:
        """LangGraph 워크플로우 구성"""
        workflow = StateGraph(AgentState)

        # 노드 등록
        workflow.add_node("analyze_intent", self._analyze_intent)
        workflow.add_node("fast_respond", self._fast_respond)
        workflow.add_node("search_knowledge", self._search_knowledge)
        workflow.add_node("reason", self._reason)
        workflow.add_node("self_reflect", self._self_reflect)
        workflow.add_node("respond", self._respond)

        # 엣지 연결
        workflow.set_entry_point("analyze_intent")
        workflow.add_conditional_edges(
            "analyze_intent",
            self._route_after_intent,
            {
                "fast_respond": "fast_respond",
                "search_knowledge": "search_knowledge",
                "reason": "reason",
                "respond": "respond",
            },
        )
        workflow.add_edge("fast_respond", END)
        workflow.add_edge("search_knowledge", "reason")
        workflow.add_edge("reason", "self_reflect")
        workflow.add_conditional_edges(
            "self_reflect",
            self._route_after_reflection,
            {
                "retry": "search_knowledge",
                "respond": "respond",
            },
        )
        workflow.add_edge("respond", END)

        return workflow.compile()

    async def _analyze_intent(self, state: AgentState) -> dict[str, Any]:
        """Step 1: 사용자 의도 분석"""
        user_prompt = INTENT_ANALYSIS_USER.format(query=state.query)

        try:
            result = await self.llm.complete(
                user_prompt,
                system_prompt=INTENT_ANALYSIS_SYSTEM,
                # Haiku 4.5 최소 캐시 요구: 4096 토큰 → 현재 프롬프트 미달 → 캐싱 불가
                model_tier="cheap",  # 의도 분석은 저비용 모델로 충분
                temperature=0.3,
            )
        except (KillSwitchError, LLMAllProvidersExhaustedError, NoAPIKeyError):
            raise  # 상위로 전파
        except Exception as e:
            logger.error("intent_analysis_failed", error=str(e))
            # 의도 분석 실패해도 기본값으로 계속 진행
            return {
                "intent": {
                    "surface_intent": state.query,
                    "deeper_intent": "",
                    "required_knowledge": [],
                    "search_queries": [state.query],
                    "complexity": "moderate",
                    "recommended_action": "search_knowledge",
                }
            }

        # JSON 파싱 시도
        intent = _safe_parse_json(result["content"])
        if intent is None:
            logger.warning(
                "intent_json_parse_failed",
                content_preview=result["content"][:200],
            )
            intent = {
                "surface_intent": state.query,
                "deeper_intent": "",
                "required_knowledge": [],
                "search_queries": [state.query],
                "complexity": "moderate",
                "recommended_action": "search_knowledge",
            }

        logger.info("intent_analyzed", intent=intent)
        return {"intent": intent}

    def _route_after_intent(self, state: AgentState) -> str:
        """의도 분석 후 라우팅 결정

        Fast path: simple 복잡도 + respond/clarify → LLM 1회로 즉답
        Normal path: 검색 → 추론 → 성찰 → 응답
        """
        action = state.intent.get("recommended_action", "search_knowledge")
        complexity = state.intent.get("complexity", "moderate")

        # Fast path: 간단한 인사/잡담은 즉시 응답 (LLM 1회)
        if complexity == "simple" and action in ("respond", "clarify"):
            return "fast_respond"

        # PC 명령은 검색 불필요 → 바로 추론 (도구 호출)
        if action == "pc_command":
            return "reason"

        if action in ("respond", "clarify"):
            return "reason"  # 검색 스킵 / 추론은 반드시 수행
        return "search_knowledge"

    async def _fast_respond(self, state: AgentState) -> dict[str, Any]:
        """Fast path: 간단한 질문에 LLM 1회 호출로 즉답

        simple 복잡도 + respond/clarify 의도일 때만 진입
        검색/추론/성찰 없이 cheap 모델로 즉시 답변 → 비용+시간 절약
        """
        user_prompt = FAST_RESPOND_USER.format(query=state.query)

        try:
            result = await self.llm.complete(
                user_prompt,
                system_prompt=FAST_RESPOND_SYSTEM,
                # Haiku 4.5 최소 캐시 요구: 4096 토큰 → 현재 프롬프트 미달 → 캐싱 불가
                model_tier="cheap",
                temperature=0.7,
            )
        except (KillSwitchError, LLMAllProvidersExhaustedError, NoAPIKeyError):
            raise
        except Exception as e:
            logger.warning("fast_respond_failed", error=str(e))
            # fast path 실패 시 빈 답변 → _respond에서 기본 메시지 출력
            return {
                "current_answer": "",
                "confidence": 0.5,
                "iteration": 1,
            }

        answer = result["content"]
        logger.info("fast_respond_success", answer_length=len(answer))

        return {
            "messages": [{"role": "assistant", "content": answer}],
            "current_answer": answer,
            "confidence": 0.9,
            "iteration": 1,
        }

    async def _search_knowledge(self, state: AgentState) -> dict[str, Any]:
        """Step 2: 지식 검색 (RAG — Hybrid Retrieval 지원)

        hybrid_retriever가 설정되면: 벡터 + BM25 → RRF 병합
        미설정이면: 기존 벡터 검색만 사용 (하위 호환)
        """
        # 이미 검색 결과가 있으면 재검색 안 함 (retry 루프 시)
        if state.search_results and state.iteration > 0:
            logger.debug("search_skipped", reason="already_has_results_in_retry")
            return {"search_results": state.search_results}

        queries = state.intent.get("search_queries", [state.query])
        all_results: list[dict[str, Any]] = []
        collection = state.collection  # state에서 컬렉션 참조 (race condition 방지)
        use_hybrid = self.hybrid_retriever is not None

        for query in queries[:3]:  # 최대 3개 쿼리
            try:
                if use_hybrid:
                    results = self.hybrid_retriever.search(
                        collection_name=collection,
                        query=query,
                        top_k=5,
                    )
                else:
                    results = self.vector_store.search(
                        collection_name=collection,
                        query=query,
                        top_k=3,
                    )
                all_results.extend(results)
            except CollectionNotFoundError:
                logger.warning("collection_not_found", collection=collection, query=query)
                # 컬렉션 없으면 검색 결과 없이 진행 (추론만으로 답변)
                break
            except VectorStoreError as e:
                logger.warning("search_failed", query=query, error=str(e))
                continue

        # 중복 제거 + 점수순 정렬
        seen_texts: set[str] = set()
        unique_results = []
        for r in sorted(all_results, key=lambda x: x["score"], reverse=True):
            text_hash = r["text"][:100]
            if text_hash not in seen_texts:
                seen_texts.add(text_hash)
                unique_results.append(r)

        return {"search_results": unique_results[:10]}

    async def _reason(self, state: AgentState) -> dict[str, Any]:
        """Step 3: 논리적 추론 (SYSTEM_PROMPT_DYNAMIC_BOUNDARY 적용)

        도구가 등록된 경우:
            LLM에 도구 목록을 전달하여 자율적 도구 선택/실행 루프 수행
            최대 MAX_TOOL_ITERATIONS회 도구 호출 후 최종 답변 생성

        도구가 없는 경우:
            기존 동작 유지 (단순 추론)

        시스템 프롬프트 구조:
        - REASONING_SYSTEM (고정 → cache_control + ephemeral → 캐시됨)
        - REASONING_SYSTEM_DYNAMIC (동적 → 메모리 컨텍스트 / 캐시 경계 바깥)
        """
        search_context = "\n\n".join(
            f"[관련도: {r['score']:.2f}] {r['text']}"
            for r in state.search_results
        ) if state.search_results else "검색 결과 없음"

        previous = "\n".join(state.reasoning_steps) if state.reasoning_steps else "첫 번째 추론 단계"

        # 동적 경계: 메모리 컨텍스트를 시스템 프롬프트 동적 영역에 배치
        memory_text = state.memory_context if state.memory_context else "메모리 없음"
        system_dynamic = REASONING_SYSTEM_DYNAMIC.format(memory_context=memory_text)

        user_prompt = REASONING_USER.format(
            query=state.query,
            intent=str(state.intent),
            search_results=search_context,
            previous_reasoning=previous,
        )

        # 도구 사용 가능 여부 판단
        has_tools = (
            self.tool_registry is not None
            and self.tool_registry.tool_count > 0
        )

        if has_tools:
            return await self._reason_with_tools(
                state, user_prompt, system_dynamic,
            )

        # === 기존 동작 (도구 없음) ===
        try:
            result = await self.llm.complete(
                user_prompt,
                system_prompt=REASONING_SYSTEM,
                system_prompt_dynamic=system_dynamic,
                cache_system_prompt=True,
                model_tier="default",
                temperature=0.5,
            )
        except (KillSwitchError, LLMAllProvidersExhaustedError, NoAPIKeyError):
            raise
        except Exception as e:
            logger.error("reasoning_failed", error=str(e), iteration=state.iteration)
            raise AgentError(
                f"추론 단계 실패: {e}",
                query=state.query,
                iteration=state.iteration,
            ) from e

        new_steps = state.reasoning_steps + [f"[Iteration {state.iteration + 1}] {result['content'][:500]}"]

        return {
            "current_answer": _extract_answer(result["content"]),
            "reasoning_steps": new_steps,
            "iteration": state.iteration + 1,
        }

    async def _reason_with_tools(
        self,
        state: AgentState,
        user_prompt: str,
        system_dynamic: str,
    ) -> dict[str, Any]:
        """도구 호출 루프가 포함된 추론

        LLM에 도구 목록을 전달 → tool_calls 응답 시 실행 → 결과 피드백 → 반복
        최대 MAX_TOOL_ITERATIONS회 반복 후 강제 텍스트 응답

        Flow:
            1. LLM 호출 (tools 포함)
            2. tool_calls 응답? → ToolRegistry.execute() → 결과를 메시지에 추가
            3. 2 반복 (최대 MAX_TOOL_ITERATIONS)
            4. 텍스트 응답 → 최종 답변으로 반환
        """
        tools = self.tool_registry.to_llm_tools()

        # 멀티턴 메시지 빌드 (시스템 + 사용자)
        system_content = f"{REASONING_SYSTEM}\n\n{system_dynamic}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt},
        ]

        tool_calls_made = state.tool_calls_made
        tool_observations: list[str] = []
        tool_call_history = list(state.tool_call_history)  # 이력 복사 (immutability)

        try:
            for tool_iter in range(MAX_TOOL_ITERATIONS):
                result = await self.llm.complete_with_messages(
                    messages,
                    tools=tools,
                    model_tier="default",
                    temperature=0.5,
                )

                tool_calls = result.get("tool_calls")
                if not tool_calls:
                    # LLM이 텍스트 응답 → 도구 루프 종료
                    break

                # === 도구 실행 ===
                # assistant 메시지 추가 (tool_calls 포함)
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": result.get("content") or "",
                    "tool_calls": tool_calls,
                }
                messages.append(assistant_msg)

                for tc in tool_calls:
                    func_name = tc["function"]["name"]
                    func_args_str = tc["function"]["arguments"]
                    tc_id = tc["id"]

                    # 파라미터 파싱
                    try:
                        func_args = json.loads(func_args_str) if func_args_str else {}
                    except json.JSONDecodeError:
                        func_args = {}

                    # === 동일 도구+파라미터 재호출 방지 ===
                    call_signature = f"{func_name}|{hashlib.md5(func_args_str.encode()).hexdigest()}"
                    if call_signature in tool_call_history:
                        logger.warning(
                            "duplicate_tool_call_blocked",
                            tool=func_name,
                            args_preview=func_args_str[:100],
                        )
                        # 중복 호출 시 이전 결과 안내 메시지로 대체
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": (
                                f"[중복 호출 차단] '{func_name}'에 동일한 파라미터로 "
                                f"이미 호출한 적이 있습니다. 이전 응답을 참고하세요."
                            ),
                        })
                        continue

                    tool_call_history.append(call_signature)

                    # ToolRegistry로 실행 (Critic 평가 포함)
                    tool_result = await self.tool_registry.execute(
                        func_name,
                        func_args,
                        context=f"사용자 질문: {state.query[:200]}",
                    )
                    tool_calls_made += 1

                    observation = tool_result.to_observation()
                    tool_observations.append(observation)

                    # tool result 메시지 추가
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": observation,
                    })

                    logger.info(
                        "agent_tool_executed",
                        tool=func_name,
                        success=tool_result.success,
                        pending=tool_result.pending_confirmation,
                        iteration=tool_iter + 1,
                    )

                    # NEEDS_CONFIRMATION → 루프 중단 (사용자 확인 대기)
                    if tool_result.pending_confirmation:
                        final_content = (
                            f"도구 '{func_name}' 실행을 위해 사용자 확인이 필요합니다.\n"
                            f"확인 ID: {tool_result.confirmation_id}"
                        )
                        new_steps = state.reasoning_steps + [
                            f"[Iteration {state.iteration + 1}] 도구 확인 대기: {func_name}"
                        ]
                        return {
                            "current_answer": final_content,
                            "reasoning_steps": new_steps,
                            "iteration": state.iteration + 1,
                            "tool_calls_made": tool_calls_made,
                            "tool_call_history": tool_call_history,
                        }

            # 최종 답변 (도구 루프 후 LLM의 마지막 텍스트 응답)
            final_content = result.get("content", "")

            # 도구 루프 상한 도달 시 마지막 LLM 호출 (도구 없이)
            if tool_calls and not final_content:
                result = await self.llm.complete_with_messages(
                    messages,
                    model_tier="default",
                    temperature=0.5,
                )
                final_content = result.get("content", "")

        except (KillSwitchError, LLMAllProvidersExhaustedError, NoAPIKeyError):
            raise
        except Exception as e:
            logger.error(
                "reasoning_with_tools_failed",
                error=str(e),
                iteration=state.iteration,
                tool_calls_made=tool_calls_made,
            )
            raise AgentError(
                f"도구 기반 추론 실패: {e}",
                query=state.query,
                iteration=state.iteration,
            ) from e

        # 도구 관찰 결과를 추론 단계에 포함
        step_summary = f"[Iteration {state.iteration + 1}]"
        if tool_observations:
            step_summary += f" 도구 {len(tool_observations)}회 호출"
        step_summary += f" {final_content[:500]}"

        new_steps = state.reasoning_steps + [step_summary]

        return {
            "current_answer": _extract_answer(final_content),
            "reasoning_steps": new_steps,
            "iteration": state.iteration + 1,
            "tool_calls_made": tool_calls_made,
            "tool_call_history": tool_call_history,
        }

    async def _self_reflect(self, state: AgentState) -> dict[str, Any]:
        """Step 4: 자기 성찰 - 답변 품질 평가

        도구 호출 성공 시:
        - confidence 하한선 0.6 적용 (API 응답을 할루시네이션으로 오판 방지)
        - 동일 도구+파라미터 재호출 방지 (should_retry 억제)
        """
        # 최대 반복 횟수 도달 시 성찰 스킵 (비용 절감)
        if state.iteration >= MAX_ITERATIONS:
            logger.info("self_reflect_skipped", reason="max_iterations_reached")
            return {"confidence": 0.6, "should_stop": True}

        # 도구 사용 여부 정보 생성 (LLM에게 컨텍스트 제공)
        tools_used = state.tool_calls_made > 0
        if tools_used:
            tool_usage_info = (
                f"이 답변은 외부 도구를 {state.tool_calls_made}회 호출하여 "
                f"실제 API 응답 데이터를 기반으로 생성되었습니다. "
                f"도구 응답 데이터는 할루시네이션이 아닌 실제 정보입니다."
            )
        else:
            tool_usage_info = "외부 도구 사용 없이 LLM 자체 지식으로 답변했습니다."

        user_prompt = SELF_REFLECTION_USER.format(
            query=state.query,
            intent=str(state.intent),
            tool_usage_info=tool_usage_info,
            answer=state.current_answer[:2000],  # 너무 긴 답변은 잘라서 평가
        )

        try:
            result = await self.llm.complete(
                user_prompt,
                system_prompt=SELF_REFLECTION_SYSTEM,
                # Haiku 4.5 최소 캐시 요구: 4096 토큰 → 현재 프롬프트 미달 → 캐싱 불가
                model_tier="cheap",
                temperature=0.2,
            )
        except (KillSwitchError, LLMAllProvidersExhaustedError, NoAPIKeyError):
            raise
        except Exception as e:
            # 성찰 실패해도 현재 답변으로 진행
            logger.warning("self_reflect_failed", error=str(e))
            return {"confidence": 0.6, "should_stop": True}

        reflection = _safe_parse_json(result["content"])
        if reflection is None:
            logger.warning(
                "reflection_json_parse_failed",
                content_preview=result["content"][:200],
            )
            return {"confidence": 0.7, "should_stop": True}

        confidence = reflection.get("quality_score", 0.7)
        should_retry = reflection.get("should_retry", False)

        # === 도구 호출 성공 시 confidence 하한선 + retry 억제 ===
        if tools_used:
            # 도구 API가 성공적으로 데이터를 반환했으면 최소 0.6 보장
            if confidence < 0.6:
                logger.info(
                    "confidence_floor_applied",
                    original=confidence,
                    adjusted=0.6,
                    reason="tool_api_response_is_factual",
                )
                confidence = 0.6

            # 동일 도구+파라미터 재호출은 같은 결과를 반환하므로 retry 무의미
            if should_retry:
                logger.info(
                    "retry_suppressed",
                    reason="tool_responses_are_deterministic",
                    tool_calls_made=state.tool_calls_made,
                )
                should_retry = False

        logger.info(
            "self_reflection_result",
            confidence=confidence,
            should_retry=should_retry,
            tools_used=tools_used,
            issues=reflection.get("issues", []),
        )

        return {
            "confidence": confidence,
            "should_stop": not should_retry,
        }

    def _route_after_reflection(self, state: AgentState) -> str:
        """자기 성찰 후 라우팅"""
        if state.should_stop or state.iteration >= MAX_ITERATIONS:
            return "respond"
        if state.confidence >= 0.6:
            return "respond"
        return "retry"

    async def _respond(self, state: AgentState) -> dict[str, Any]:
        """Step 5: 최종 답변 생성"""
        answer = state.current_answer
        if not answer and state.error:
            answer = f"요청을 처리하는 중 문제가 발생했습니다: {state.error}"
        elif not answer:
            answer = "죄송합니다. 질문에 대한 답변을 생성하지 못했습니다. 질문을 다시 표현해주세요."

        return {
            "messages": [{"role": "assistant", "content": answer}],
            "current_answer": answer,
        }

    async def run(
        self,
        query: str,
        collection: str = "default",
        context: list[dict[str, str]] | None = None,
        scope: str = "global",
        memory_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        """에이전트 실행

        Args:
            query: 사용자 질문
            collection: 검색 대상 벡터DB 컬렉션
            context: 이전 대화 이력
            scope: 메모리 스코프 (global/testorum/talksim/autotube)
            memory_domains: 명시적 토픽 지정 (None이면 전체)

        Returns:
            {"answer": str, "confidence": float, "reasoning_steps": list,
             "cost_summary": dict, "memory_loaded": list, ...}

        Raises:
            KillSwitchError: 비용 상한 초과
            LLMAllProvidersExhaustedError: 모든 LLM 프로바이더 실패
            AgentError: 에이전트 내부 에러
        """
        # 메모리 로딩 (memory_loader가 설정된 경우)
        memory_loaded: list[str] = []
        memory_context: str = ""
        if self.memory_loader is not None:
            try:
                load_result = self.memory_loader.load(
                    scope=scope,
                    domains=memory_domains,
                )
                memory_loaded = load_result.loaded_domains
                memory_context = load_result.prompt_markdown
                logger.info(
                    "memory_injected",
                    scope=scope,
                    domains_loaded=len(memory_loaded),
                    total_tokens=load_result.total_tokens,
                )
            except Exception as e:
                # 메모리 로딩 실패해도 에이전트는 계속 동작
                logger.warning("memory_load_failed", scope=scope, error=str(e))

        initial_state = AgentState(
            query=query,
            collection=collection,
            messages=context or [],
            memory_context=memory_context,
        )

        try:
            # LangGraph 실행
            final_state = await self._graph.ainvoke(initial_state)
        except (KillSwitchError, LLMAllProvidersExhaustedError, NoAPIKeyError):
            raise  # 그대로 전파 → API 레이어에서 적절한 HTTP status로 변환
        except Exception as e:
            logger.error("agent_run_failed", query=query[:100], error=str(e))
            raise AgentError(f"에이전트 실행 실패: {e}", query=query) from e

        return {
            "answer": final_state.get("current_answer", ""),
            "confidence": final_state.get("confidence", 0.0),
            "reasoning_steps": final_state.get("reasoning_steps", []),
            "iterations": final_state.get("iteration", 0),
            "search_results_count": len(final_state.get("search_results", [])),
            "cost_summary": self.llm.get_cost_summary(),
            "memory_loaded": memory_loaded,
            "tool_calls_made": final_state.get("tool_calls_made", 0),
        }
