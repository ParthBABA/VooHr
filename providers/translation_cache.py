"""File-backed cache for LLM-translated text.

Mirrors :class:`providers.tts_cache.TTSCache` one step earlier in the
narration pipeline: TTSCache caches the *audio* generated for an already
translated text, while TranslationCache caches the *translated text* itself.
Caching the full original ``(text, target_language)`` pair means a cache hit
returns a complete, previously-validated translation with zero LLM calls —
never a partial chunk-level result — and repeated synthesis for the same
text/voice combo never re-runs the translator either.
"""

import hashlib

from providers.storage import LocalStorage


class TranslationCache:
    """On-disk cache of translated text.

    Keys are lowercase SHA-256 digests of ``(text, target_language)``; values
    are the translated strings stored as UTF-8 bytes under the "translations"
    namespace of the shared :class:`providers.storage.LocalStorage` directory.
    """

    NAMESPACE = "translations"

    def __init__(self, storage: LocalStorage | None = None):
        self._storage = storage or LocalStorage()

    @property
    def _dir(self):
        return self._storage.base_dir / self.NAMESPACE

    def _path(self, key: str):
        return self._dir / f"{key}.txt"

    @staticmethod
    def build_key(text: str, target_language: str) -> str:
        """Return the storage key for a translation request.

        The hash covers exactly the inputs that determine the output: the
        whitespace-normalised source text and the target language.
        """
        raw = "\x1f".join([(text or "").strip(), (target_language or "").strip()])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> str | None:
        """Return the cached translation for *key*, or ``None`` on a miss."""
        try:
            return self._path(key).read_text(encoding="utf-8")
        except OSError:
            return None

    def set(self, key: str, text: str) -> None:
        """Store *text* under *key*.

        Empty results are never cached — an empty string usually signals a
        failed translation and must not shadow a later real result.
        """
        if not text:
            return
        self._storage.save(self.NAMESPACE, f"{key}.txt", text.encode("utf-8"))