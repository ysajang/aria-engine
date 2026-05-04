"""ARIA Engine - Whisper Transcriber

faster-whisper (CTranslate2) 기반 로컬 STT
- CPU INT8 양자화로 GPU 없이 고속 변환
- Lazy loading: 첫 호출 시 모델 다운로드 + 로딩
- 텔레그램 음성 메시지 (OGG/Opus) 직접 지원
- 한국어 포함 99+ 언어 지원

모델 크기별 성능 (CPU INT8 / 10초 음성 기준):
- small: ~461MB / ~2초 처리
- medium: ~1.5GB / ~5초 처리
- large-v3: ~2.9GB / ~12초 처리

보안: 음성 데이터가 외부로 전송되지 않음 (완전 로컬 처리)
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()


@dataclass(frozen=True)
class TranscriptionResult:
    """STT 변환 결과

    Attributes:
        text: 변환된 텍스트
        language: 감지된 언어 코드 (ko / en / ja 등)
        language_probability: 언어 감지 확률 (0.0~1.0)
        duration_seconds: 오디오 길이 (초)
        processing_ms: 처리 소요 시간 (밀리초)
    """
    text: str
    language: str = ""
    language_probability: float = 0.0
    duration_seconds: float = 0.0
    processing_ms: float = 0.0


class WhisperTranscriber:
    """faster-whisper 기반 로컬 STT

    Lazy loading: 첫 transcribe() 호출 시 모델 로딩
    Thread-safe: asyncio.Lock으로 동시 접근 방지

    Args:
        model_size: 모델 크기 (tiny / base / small / medium / large-v3)
        device: 실행 장치 (cpu / cuda / auto)
        compute_type: 양자화 타입 (int8 / float16 / float32)
        language: 강제 언어 지정 (None이면 자동 감지)
        beam_size: 빔 서치 크기 (정확도↑ / 속도↓)
        cpu_threads: CPU 스레드 수 (0이면 자동)
    """

    def __init__(
        self,
        model_size: str = "medium",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = None,
        beam_size: int = 5,
        cpu_threads: int = 0,
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._beam_size = beam_size
        self._cpu_threads = cpu_threads
        self._model = None
        self._lock = asyncio.Lock()
        self._loaded = False

    def _load_model(self) -> None:
        """모델 로딩 (동기 — 첫 호출 시 1회)

        모델이 로컬 캐시에 없으면 Hugging Face에서 자동 다운로드
        기본 캐시 경로: ~/.cache/huggingface/hub/
        """
        if self._loaded:
            return

        from faster_whisper import WhisperModel

        start = time.monotonic()

        self._model = WhisperModel(
            self._model_size,
            device=self._device,
            compute_type=self._compute_type,
            cpu_threads=self._cpu_threads if self._cpu_threads > 0 else os.cpu_count() or 4,
        )

        elapsed_ms = (time.monotonic() - start) * 1000
        self._loaded = True
        logger.info(
            "whisper_model_loaded",
            model_size=self._model_size,
            device=self._device,
            compute_type=self._compute_type,
            load_time_ms=round(elapsed_ms),
        )

    def _transcribe_sync(self, audio_path: str) -> TranscriptionResult:
        """동기 변환 (CPU 바운드 → run_in_executor에서 호출)

        Args:
            audio_path: 오디오 파일 경로 (OGG/MP3/WAV/M4A 등)

        Returns:
            TranscriptionResult
        """
        self._load_model()
        assert self._model is not None

        start = time.monotonic()

        segments, info = self._model.transcribe(
            audio_path,
            beam_size=self._beam_size,
            language=self._language,
            vad_filter=True,  # 무음 구간 자동 제거
            vad_parameters=dict(
                min_silence_duration_ms=300,  # 300ms 이상 무음 = 구간 분리
            ),
        )

        # segments는 제너레이터 — 실제 변환은 여기서 실행
        text_parts: list[str] = []
        for segment in segments:
            text_parts.append(segment.text.strip())

        full_text = " ".join(text_parts).strip()
        elapsed_ms = (time.monotonic() - start) * 1000

        logger.info(
            "whisper_transcription_done",
            language=info.language,
            language_prob=round(info.language_probability, 3),
            duration_s=round(info.duration, 1),
            processing_ms=round(elapsed_ms),
            text_length=len(full_text),
        )

        return TranscriptionResult(
            text=full_text,
            language=info.language,
            language_probability=info.language_probability,
            duration_seconds=info.duration,
            processing_ms=elapsed_ms,
        )

    async def transcribe(self, audio_path: str) -> TranscriptionResult:
        """비동기 변환 — CPU 바운드 작업을 thread pool에서 실행

        Args:
            audio_path: 오디오 파일 경로

        Returns:
            TranscriptionResult

        Raises:
            FileNotFoundError: 오디오 파일 없음
            RuntimeError: 변환 실패
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"오디오 파일을 찾을 수 없습니다: {audio_path}")

        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,  # default thread pool
                self._transcribe_sync,
                audio_path,
            )

    async def transcribe_bytes(self, audio_data: bytes, suffix: str = ".ogg") -> TranscriptionResult:
        """바이트 데이터에서 직접 변환

        텔레그램 음성 메시지 등 바이트 스트림 입력용
        임시 파일 생성 → 변환 → 삭제

        Args:
            audio_data: 오디오 바이트 데이터
            suffix: 파일 확장자 (텔레그램 음성 = .ogg)

        Returns:
            TranscriptionResult
        """
        if not audio_data:
            raise ValueError("빈 오디오 데이터입니다")

        # 임시 파일에 쓰고 변환 후 삭제
        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                suffix=suffix,
                delete=False,
            ) as tmp:
                tmp.write(audio_data)
                tmp_path = tmp.name

            return await self.transcribe(tmp_path)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @property
    def is_loaded(self) -> bool:
        """모델 로딩 여부"""
        return self._loaded

    @property
    def model_info(self) -> dict:
        """현재 모델 정보"""
        return {
            "model_size": self._model_size,
            "device": self._device,
            "compute_type": self._compute_type,
            "language": self._language or "auto",
            "beam_size": self._beam_size,
            "loaded": self._loaded,
        }
