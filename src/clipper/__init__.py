"""Personal Clipper — Windows-side runner package for the n8n clip pipeline.

Modules:
- config:          single source of truth for paths / URLs (env-overridable via SG_CLIPPER_*)
- ledger:          idempotency ledger (sha1 of size+mtime+first MB) with atomic writes
- audio:           ffprobe + PCM decode + RMS helpers (stdlib + optional numpy)
- transcribe:      faster-whisper word-level transcription (lazy import)
- score_moments:   transparent candidate-window scoring (stage 3)
- captions:        word JSON -> clip-relative SRT and safe-zone ASS (burn-in)
- capcut_package:  CapCut hand-off package builder (stage 6B)
- runner:          stdlib HTTP server exposing every endpoint in n8n/CONTRACT.md
"""
