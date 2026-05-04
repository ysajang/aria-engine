"""ARIA Engine - Telegram Bot

텔레그램 봇 초기화 및 실행
- polling 모드 (webhook 불필요 — 1인 사용)
- ARIA API 클라이언트 연결
- 핸들러 등록 (텍스트 + 음성)

실행: python run_telegram.py
"""

from __future__ import annotations

from typing import Any

import structlog
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from aria.core.config import TelegramConfig
from aria.telegram.client import ARIAClient
from aria.telegram.handlers import ARIAHandlers

logger = structlog.get_logger()


def create_bot(config: TelegramConfig, transcriber: Any | None = None) -> Application:
    """텔레그램 봇 애플리케이션 생성

    Args:
        config: TelegramConfig (봇 토큰 / 채팅 ID / ARIA 서버 주소)
        transcriber: WhisperTranscriber 인스턴스 (None이면 음성 비활성화)

    Returns:
        설정 완료된 telegram Application (아직 시작 안 됨)

    Raises:
        ValueError: 필수 설정 누락 (bot_token / chat_id)
    """
    if not config.bot_token:
        raise ValueError(
            "ARIA_TELEGRAM_BOT_TOKEN이 설정되지 않았습니다. "
            ".env 파일에 추가하세요."
        )
    if not config.chat_id:
        raise ValueError(
            "ARIA_TELEGRAM_CHAT_ID가 설정되지 않았습니다. "
            ".env 파일에 추가하세요. "
            "(텔레그램에서 @userinfobot으로 확인)"
        )

    # ARIA API 클라이언트
    aria_client = ARIAClient(
        base_url=config.aria_base_url,
        api_key=config.aria_api_key,
        timeout=config.request_timeout,
    )

    # 핸들러
    handlers = ARIAHandlers(
        aria_client=aria_client,
        allowed_chat_id=config.chat_id,
        default_scope=config.default_scope,
        default_collection=config.default_collection,
        transcriber=transcriber,
    )

    # 봇 생성
    app = Application.builder().token(config.bot_token).build()

    # 명령어 핸들러 등록
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("help", handlers.help_command))
    app.add_handler(CommandHandler("cost", handlers.cost))
    app.add_handler(CommandHandler("memory", handlers.memory))
    app.add_handler(CommandHandler("health", handlers.health))
    app.add_handler(CommandHandler("briefing", handlers.briefing))

    # HITL 콜백 핸들러 (인라인 키보드 승인/거부)
    app.add_handler(CallbackQueryHandler(handlers.handle_confirmation_callback))

    # 음성 메시지 핸들러 (Phase 4: STT → ARIA 질의)
    if transcriber is not None:
        app.add_handler(MessageHandler(
            filters.VOICE,
            handlers.handle_voice,
        ))
        logger.info("telegram_voice_handler_registered", stt="faster-whisper")
    else:
        logger.info("telegram_voice_handler_skipped", reason="transcriber not provided")

    # 일반 텍스트 메시지 핸들러 (명령어가 아닌 모든 텍스트)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handlers.handle_message,
    ))

    logger.info(
        "telegram_bot_created",
        chat_id=config.chat_id,
        aria_url=config.aria_base_url,
        default_scope=config.default_scope,
        voice_enabled=transcriber is not None,
    )

    return app


def run_bot(config: TelegramConfig, transcriber: Any | None = None) -> None:
    """텔레그램 봇 실행 (polling 모드 — 블로킹)

    Args:
        config: TelegramConfig
        transcriber: WhisperTranscriber 인스턴스 (선택)
    """
    app = create_bot(config, transcriber=transcriber)

    logger.info("telegram_bot_starting", mode="polling", voice_enabled=transcriber is not None)
    app.run_polling(
        drop_pending_updates=True,  # 서버 재시작 시 밀린 메시지 무시
        allowed_updates=["message", "callback_query"],  # 필요한 업데이트만 수신
    )
