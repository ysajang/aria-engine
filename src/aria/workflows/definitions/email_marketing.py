"""#5 이메일 마케팅

세그먼트 정의(규칙) → 이메일 초안(템플릿+Haiku 개인화) → Gmail 드래프트 생성

트리거: /marketing email-campaign 명령 (수동 트리거만 — 스팸 방지)
LLM: Haiku (개인화 멘트 생성 스텝만)
도구: mcp_gmail_create_draft

플로우:
1. FUNCTION: 규칙 기반 세그먼트 정의 + 수신자 목록 구성
2. TEMPLATE: 이메일 기본 구조 렌더링 (캠페인별 템플릿)
3. LLM_CALL: Haiku로 개인화 인사/CTA 생성
4. FUNCTION: 최종 이메일 본문 조합 (템플릿 + 개인화)
5. TOOL_CALL: Gmail 초안 생성 (발송은 수동 — HITL)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aria.workflows.types import (
    OnError,
    StepType,
    WorkflowContext,
    WorkflowDefinition,
    WorkflowFunc,
    WorkflowStep,
)

# 사전 정의 세그먼트 규칙
SEGMENT_DEFINITIONS: dict[str, dict[str, Any]] = {
    "new_users": {
        "name": "신규 사용자 (7일 이내)",
        "description": "가입 후 7일 이내 사용자",
        "campaign_type": "onboarding",
        "default_subject_ko": "환영합니다! 첫 테스트를 시작해보세요",
        "default_subject_en": "Welcome! Start your first test",
    },
    "inactive_users": {
        "name": "비활성 사용자 (30일+)",
        "description": "30일 이상 미접속 사용자",
        "campaign_type": "reactivation",
        "default_subject_ko": "다시 만나서 반가워요 — 새로운 테스트가 기다리고 있어요",
        "default_subject_en": "We miss you — New tests are waiting",
    },
    "power_users": {
        "name": "파워 유저 (5회+ 완료)",
        "description": "테스트 5회 이상 완료한 활성 사용자",
        "campaign_type": "upsell",
        "default_subject_ko": "당신만을 위한 프리미엄 경험",
        "default_subject_en": "Unlock your premium experience",
    },
    "all_users": {
        "name": "전체 사용자",
        "description": "전체 뉴스레터 수신 동의 사용자",
        "campaign_type": "newsletter",
        "default_subject_ko": "이번 주 인기 테스트 소식",
        "default_subject_en": "This week's trending tests",
    },
}

# 이메일 개인화 시스템 프롬프트
EMAIL_PERSONALIZE_SYSTEM = """\
You are an email marketing specialist for a consumer psychology/quiz product.
Generate personalized email greeting and CTA text.

Rules:
- Write in the specified language
- Greeting: 1-2 sentences, warm and personal
- CTA: 1 sentence, clear action with benefit
- Tone: friendly, not salesy, authentic
- Do NOT use generic phrases like "Dear valued customer"
- Reference the segment context naturally
- Output ONLY two lines:
  GREETING: [your greeting text]
  CTA: [your CTA text]
"""


def build_email_marketing(
    tool_registry: Any = None,
    event_store: Any = None,
    **kwargs: Any,
) -> tuple[WorkflowDefinition, dict[str, WorkflowFunc]]:
    """이메일 마케팅 워크플로우 빌드"""

    async def define_segments(ctx: WorkflowContext) -> dict[str, Any]:
        """규칙 기반 세그먼트 정의 + 수신자 목록 구성

        initial_data에서:
        - segment: 세그먼트 키 (기본 "all_users")
        - recipients: 수신자 리스트 [{"email": ..., "name": ...}]
        - subject: 커스텀 제목 (없으면 세그먼트 기본값)
        - campaign_name: 캠페인 이름
        """
        segment_key = ctx.get("segment", "all_users")
        segment_def = SEGMENT_DEFINITIONS.get(segment_key)
        if not segment_def:
            segment_def = SEGMENT_DEFINITIONS["all_users"]
            segment_key = "all_users"

        recipients = ctx.get("recipients", [])
        language = ctx.get("language", "en")
        target_product = ctx.get("target_product", "testorum")

        # 제목 결정
        subject = ctx.get("subject", "")
        if not subject:
            subject_key = f"default_subject_{language[:2]}"
            subject = segment_def.get(subject_key, segment_def.get(
                "default_subject_en", "Update from us"
            ))

        # 캠페인 이름
        campaign_name = ctx.get("campaign_name", "")
        if not campaign_name:
            campaign_name = (
                f"{target_product} — {segment_def['campaign_type']} "
                f"({datetime.now(timezone.utc).strftime('%Y-%m-%d')})"
            )

        return {
            "segment_key": segment_key,
            "segment_name": segment_def["name"],
            "segment_description": segment_def["description"],
            "campaign_type": segment_def["campaign_type"],
            "campaign_name": campaign_name,
            "subject": subject,
            "recipients": recipients,
            "recipient_count": len(recipients),
            "language": language,
            "target_product": target_product,
        }

    async def compose_email(ctx: WorkflowContext) -> dict[str, Any]:
        """최종 이메일 본문 조합: 템플릿 + 개인화 멘트

        LLM 개인화 출력에서 GREETING/CTA 추출 → 이메일 본문에 삽입
        """
        template_body = ctx.get("template_body", "")
        personalized_raw = ctx.get("personalized_raw", "")
        subject = ctx.get("subject", "")
        campaign_name = ctx.get("campaign_name", "")
        recipients = ctx.get("recipients", [])
        target_product = ctx.get("target_product", "testorum")

        # 개인화 멘트 파싱
        greeting = ""
        cta_text = ""
        for line in personalized_raw.split("\n"):
            line = line.strip()
            if line.upper().startswith("GREETING:"):
                greeting = line[len("GREETING:"):].strip()
            elif line.upper().startswith("CTA:"):
                cta_text = line[len("CTA:"):].strip()

        # 폴백
        if not greeting:
            greeting = "안녕하세요!" if ctx.get("language", "en") == "ko" else "Hi there!"
        if not cta_text:
            cta_text = "지금 시작하기" if ctx.get("language", "en") == "ko" else "Get started now"

        # CTA URL
        product_urls = {
            "testorum": "https://testorum.app",
            "mystel": "https://mystel.app",
            "glowtype": "https://glowtype.app",
        }
        cta_url = ctx.get("cta_url", product_urls.get(target_product, ""))

        # 최종 이메일 본문
        email_body = f"{greeting}\n\n{template_body}"
        if cta_text and cta_url:
            email_body += f"\n\n{cta_text}: {cta_url}"

        # 수신 거부 안내 (법적 필수)
        unsubscribe_ko = "\n\n---\n수신 거부를 원하시면 이 이메일에 '수신거부'로 회신해주세요."
        unsubscribe_en = "\n\n---\nTo unsubscribe, reply to this email with 'unsubscribe'."
        unsubscribe = unsubscribe_ko if ctx.get("language") == "ko" else unsubscribe_en
        email_body += unsubscribe

        return {
            "email_body": email_body,
            "greeting": greeting,
            "cta_text": cta_text,
            "cta_url": cta_url,
            "subject": subject,
            "campaign_name": campaign_name,
            "recipients": recipients,
        }

    definition = WorkflowDefinition(
        workflow_id="email-campaign",
        name="이메일 마케팅 캠페인",
        description="세그먼트 → 템플릿 → Haiku 개인화 → Gmail 드래프트",
        category="marketing",
        scope="global",
        notify_on_complete=True,
        steps=[
            # Step 1: 세그먼트 정의 + 수신자 구성
            WorkflowStep(
                name="define-segment",
                step_type=StepType.FUNCTION,
                config={"func_name": "email_define_segments"},
                output_key="segment_data",
                on_error=OnError.ABORT,
                description="규칙 기반 세그먼트 정의 + 수신자 목록",
            ),
            # Step 2: 이메일 기본 구조 렌더링
            WorkflowStep(
                name="render-template",
                step_type=StepType.TEMPLATE,
                config={
                    "template_string": (
                        "{% if campaign_type == 'onboarding' %}"
                        "Welcome to {{ target_product | title }}! "
                        "We're excited to have you on board.\n\n"
                        "Here's how to get started:\n"
                        "1. Take your first personality test\n"
                        "2. Share your results with friends\n"
                        "3. Discover more about yourself\n"
                        "{% elif campaign_type == 'reactivation' %}"
                        "It's been a while since your last visit to {{ target_product | title }}.\n\n"
                        "Since you've been away, we've added new tests and features "
                        "that we think you'll love.\n"
                        "{% elif campaign_type == 'upsell' %}"
                        "You've been one of our most active users on {{ target_product | title }} — "
                        "thank you!\n\n"
                        "We'd love to offer you an exclusive look at our premium features.\n"
                        "{% else %}"
                        "Here's what's new at {{ target_product | title }} this week:\n\n"
                        "Check out our latest tests and see what's trending in the community.\n"
                        "{% endif %}"
                    ),
                },
                output_key="template_body",
                description="캠페인 타입별 이메일 본문 템플릿 렌더링",
            ),
            # Step 3: Haiku 개인화 멘트 생성
            WorkflowStep(
                name="personalize",
                step_type=StepType.LLM_CALL,
                config={
                    "system": EMAIL_PERSONALIZE_SYSTEM,
                    "prompt": (
                        "Generate personalized greeting and CTA for:\n"
                        "- Product: {target_product}\n"
                        "- Segment: {segment_name} ({segment_description})\n"
                        "- Campaign type: {campaign_type}\n"
                        "- Language: {language}\n\n"
                        "Respond with exactly two lines:\n"
                        "GREETING: [text]\n"
                        "CTA: [text]"
                    ),
                    "model": "cheap",
                    "max_tokens": 256,
                    "temperature": 0.7,
                },
                output_key="personalized_raw",
                on_error=OnError.SKIP,
                description="Haiku로 개인화 인사말 + CTA 생성",
            ),
            # Step 4: 최종 이메일 본문 조합
            WorkflowStep(
                name="compose-email",
                step_type=StepType.FUNCTION,
                config={"func_name": "email_compose"},
                output_key="email_data",
                on_error=OnError.ABORT,
                description="템플릿 + 개인화 → 최종 이메일 본문 + 수신거부 안내",
            ),
            # Step 5: Gmail 초안 생성 (수신자별)
            # 주의: 대량 발송은 Gmail 일일 한도(500통) 주의
            # 수신자가 있으면 첫 번째 수신자에게 초안 생성
            WorkflowStep(
                name="create-gmail-draft",
                step_type=StepType.TOOL_CALL,
                config={
                    "tool_name": "mcp_gmail_create_draft",
                    "args": {
                        "subject": "{subject}",
                        "body": "{email_body}",
                    },
                },
                output_key="gmail_result",
                on_error=OnError.SKIP,
                when="email_body is_not_empty",
                description="Gmail에 이메일 초안 생성 (발송은 수동 검토 후)",
            ),
        ],
    )

    functions = {
        "email_define_segments": define_segments,
        "email_compose": compose_email,
    }
    return definition, functions
