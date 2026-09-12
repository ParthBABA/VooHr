"""File-backed cache for synthesized TTS audio.

Repeated synthesis of the same text/voice combination is the most common
fallback path in the narration UI (re-playing an already-heard snippet,
regenerating a page, etc.), and every miss re-hits the provider API. This
cache keys on a hash of ``(text, language_code, voice_name, voice_tier)`` and
stores the resulting audio bytes on disk using the existing
:class:`providers.storage.LocalStorage` abstraction, so repeated synthesis of
the same combo skips the API call entirely.

Each provider namespaces its entries under its own sub-directory, which both
avoids hash collisions across providers that happen to resolve the same voice
name and keeps structurally different audio (MP3 vs WAV) from ever being
served interchangeably.
"""

import hashlib

from providers.storage import LocalStorage


class TTSCache:
    """On-disk TTS cache layered over ``LocalStorage``.

    Keys are lowercase SHA-256 digests derived from the four request inputs
    that determine the output bytes, so repeated requests for the same
    text/voice combo resolve to the same file without any registry bookkeeping.
    """

    def __init__(self, namespace: str, storage: LocalStorage | None = None):
        self.namespace = namespace
        self._storage = storage or LocalStorage()

    @property
    def _dir(self):
        return self._storage.base_dir / self.namespace

    def _path(self, key: str):
        return self._dir / f"{key}.bin"

    @staticmethod
    def build_key(
        text: str,
        language_code: str,
        voice_name: str | None,
        voice_tier: str | None,
    ) -> str:
        """Return the storage key for a synthesis request.

        The hash is taken over exactly the four fields that determine the
        produced audio:
          * text                – the (stripped) input text
          * language_code       – BCP-47 locale
          * voice_name          – resolved provider voice/model name
          * voice_tier          – requested tier (unused by some providers)
        """
        raw = "\x1f".join(
            [
                text or "",
                language_code or "",
                voice_name or "",
                voice_tier or "",
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> bytes | None:
        """Return cached audio bytes for *key*, or ``None`` on a miss."""
        try:
            return self._path(key).read_bytes()
        except OSError:
            return None

    def set(self, key: str, data: bytes) -> None:
        """Store *data* (raw audio bytes) under *key*.

        Empty payloads are never cached — they are the "nothing to say"
        signal and should not shadow a later real result.
        """
        if not data:
            return
        self._storage.save(self.namespace, f"{key}.bin", data)