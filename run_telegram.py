#!/usr/bin/env python3
"""ARIA Telegram Bot 실행 스크립트

ARIA 서버(run.py)와 별도로 실행:
    # 1. ARIA 서버 시작
    python run.py

    # 2. 텔레그램 봇 시작 (별도 터미널)
    python run_telegram.py

필수 환경변수 (.env):
    ARIA_TELEGRAM_BOT_TOKEN=your-bot-token
    ARIA_TELEGRAM_CHAT_ID=your-chat-id
    ARIA_TELEGRAM_ARIA_API_KEY=your-aria-api-key

선택 환경변수:
    ARIA_TELEGRAM_ARIA_BASE_URL=http://localhost:8100  (기본값)
    ARIA_TELEGRAM_DEFAULT_SCOPE=global  (기본값)
    ARIA_TELEGRAM_DEFAULT_COLLECTION=default  (기본값)
    ARIA_TELEGRAM_REQUEST_TIMEOUT=120  (기본값)
"""

from __future__ import annotations

import sys

import structlog

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
)

logger = structlog.get_logger()


def main() -> None:
    from aria.core.config import TelegramConfig, STTConfig
    from aria.telegram.bot import run_bot

    config = TelegramConfig()
    stt_config = STTConfig()

    if not config.bot_token:
        logger.error("missing_bot_token", hint="ARIA_TELEGRAM_BOT_TOKEN을 .env에 설정하세요")
        sys.exit(1)

    if not config.chat_id:
        logger.error("missing_chat_id", hint="ARIA_TELEGRAM_CHAT_ID를 .env에 설정하세요")
        sys.exit(1)

    # STT 초기화 (faster-whisper)
    transcriber = None
    if stt_config.is_configured:
        try:
            from aria.stt import WhisperTranscriber
            transcriber = WhisperTranscriber(
                model_size=stt_config.model_size,
                device=stt_config.device,
                compute_type=stt_config.compute_type,
                language=stt_config.language_or_none,
                beam_size=stt_config.beam_size,
            )
            logger.info(
                "stt_initialized",
                model=stt_config.model_size,
                device=stt_config.device,
                compute_type=stt_config.compute_type,
            )
        except ImportError:
            logger.warning("stt_import_failed", hint="pip install faster-whisper")
        except Exception as e:
            logger.error("stt_init_failed", error=str(e)[:200])
    else:
        logger.info("stt_disabled", hint="ARIA_STT_ENABLED=false")

    logger.info(
        "aria_telegram_bot_config",
        aria_url=config.aria_base_url,
        chat_id=config.chat_id[:4] + "****",
        scope=config.default_scope,
        timeout=config.request_timeout,
        voice_enabled=transcriber is not None,
    )

    run_bot(config, transcriber=transcriber)


if __name__ == "__main__":
    main()
