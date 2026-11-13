"""Fixed catalog of selectable Piper TTS voices (mirrors personas.py's shape:
a small fixed library, no DB table, no admin-editable CRUD - these are real
Piper voice names from https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/VOICES.md,
not something this project invents). DEFAULT_VOICE is what every profile gets
when no one has explicitly chosen - set via _ensure_voice_tts_config
(tools_routines.py) for new profiles and tools_voice.py for an explicit
reset back to default.
"""

DEFAULT_VOICE = "en_US-ryan-medium"

VOICES = {
    "en_US-ryan-medium": {"label": "Ryan", "gender": "male", "locale": "US English"},
    "en_US-joe-medium": {"label": "Joe", "gender": "male", "locale": "US English"},
    "en_US-norman-medium": {"label": "Norman", "gender": "male", "locale": "US English"},
    "en_US-hfc_male-medium": {"label": "HFC Male", "gender": "male", "locale": "US English"},
    "en_US-lessac-medium": {"label": "Lessac", "gender": "female", "locale": "US English"},
    "en_US-amy-medium": {"label": "Amy", "gender": "female", "locale": "US English"},
    "en_US-kristin-medium": {"label": "Kristin", "gender": "female", "locale": "US English"},
    "en_US-hfc_female-medium": {"label": "HFC Female", "gender": "female", "locale": "US English"},
    "en_GB-alan-medium": {"label": "Alan", "gender": "male", "locale": "UK English"},
    "en_GB-northern_english_male-medium": {"label": "Northern English Male", "gender": "male", "locale": "UK English"},
    "en_GB-alba-medium": {"label": "Alba", "gender": "female", "locale": "UK English"},
}


def voice_options() -> list:
    """Every selectable voice, default first, for a tool/dropdown to present."""
    return [
        {"voice_key": key, **info, "is_default": key == DEFAULT_VOICE}
        for key, info in sorted(VOICES.items(), key=lambda kv: kv[0] != DEFAULT_VOICE)
    ]
