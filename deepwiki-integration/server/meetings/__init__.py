"""[r242] 회의록 자동작성 — Discord 음성(Craig 녹음) → STT → LLM 회의록.

서브모듈:
  craig_client  : Craig 녹음 다운로드(멀티트랙)
  transcribe    : ffmpeg + faster-whisper 트랙별 STT → 시간순 병합
  summarize     : LLM 회의록 요약
  store         : Supabase meeting_sessions CRUD
"""
