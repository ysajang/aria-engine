"""ARIA Engine - Speech-to-Text Module

faster-whisper 기반 로컬 STT (CPU INT8)
- 텔레그램 음성 메시지 → 텍스트 변환
- 외부 API 의존 없음 (완전 로컬 / 무료)
- MIT 라이선스 (faster-whisper + CTranslate2)
"""

from aria.stt.transcriber import WhisperTranscriber

__all__ = ["WhisperTranscriber"]
