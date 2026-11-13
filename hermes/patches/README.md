# hermes/patches/

Files here bind-mount over specific paths inside the vendored `nousresearch/hermes-agent`
image (see `docker-compose.yml`'s `hermes` service `volumes:`), instead of patching the
Dockerfile. The base image is pulled via a floating `:latest` tag, so a `docker compose
build` can silently bring in an unreviewed upstream version; a bind-mounted file survives
container restarts/recreates without ever touching the image or triggering a build.

Remove the matching `volumes:` line (and this file) once the fix lands upstream and the
image is deliberately upgraded past it — otherwise the mount will keep shadowing whatever
the new image ships at that path.

## tts_tool_delivery.py

Full copy of `tools/tts_tool_delivery.py`, mounted over `/opt/hermes/tools/tts_tool_delivery.py`.

Fix: `_finalize_wav_output()` converted a WAV-native TTS engine's output (Piper, KittenTTS,
NeuTTS) to a `.ogg` target with a bare `ffmpeg -i wav output.ogg` — no codec flag, so ffmpeg
defaults to Vorbis. Telegram's `sendVoice` only decodes Opus in its native voice-bubble
player, so the file uploads fine (valid Ogg container, correct size) but plays back as
silence. The existing "repair mismatched container" safety net (`_repair_ogg_container`)
never caught it because it only sniffs the Ogg magic bytes, which are identical for Vorbis
and Opus payloads. A sibling function in the same file (`_write_wav_bytes_as`) already had
the correct guard for this exact case; `_finalize_wav_output` was just missing it. Fixed by
forcing `_OPUS_VOICE_ARGS` whenever the output path ends in `.ogg`, mirroring that sibling.

Confirmed live 2026-11-14: real voice replies via Telegram auto-TTS (Piper, voice_only mode)
went from silent to audible after this patch.
