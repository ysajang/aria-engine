"""ARIA Engine - Server Monitoring

서버 자동 모니터링 시스템
- healthcheck: 응답시간 / 상태코드 / SSL 만료
- error_log: 에러 로그 분석 (404/500 패턴)
- traffic: 트래픽 이상 감지
- security: 보안 취약점 스캔 (헤더/포트/SSL)

플로우: cron 스크립트 → checks.py 실행 → /v1/events POST → AlertManager → 텔레그램
"""
