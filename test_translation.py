"""Tests for the translation pipeline: chunked long-text translation with an
explicit max_tokens budget, retry-and-keep-original failure handling, and the
persistent full-text TranslationCache.

Provider completion calls are mocked — no real APIs are contacted.
"""

import os
from types import SimpleNamespace

# tts -> employees -> config requires SECRET_KEY at import time. Set it here
# before importing anything that imports config.
os.environ.setdefault("SECRET_KEY", "test-secret-key")

import pytest

from providers.llm import (
    DeepSeekLLM,
    OpenAILLM,
    _split_translation_text,
    _translation_max_tokens,
    _TRANSLATE_CHUNK_MAX_CHARS,
)
from providers.storage import LocalStorage
from providers.translation_cache import TranslationCache


def _resp(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class _FakeCompletions:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _resp(self.handler(kwargs["messages"][0]["content"]))


class _FakeClient:
    def __init__(self, handler):
        self._completions = _FakeCompletions(handler)
        self.chat = SimpleNamespace(completions=self._completions)


def _install_openai(monkeypatch, handler):
    client = _FakeClient(handler)
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    return client._completions


def _chunk_from_prompt(prompt):
    return prompt.split("\n\n", 1)[1]


# ── Chunking helper ───────────────────────────────────────────────────────


class TestSplitTranslationText:
    def test_short_text_is_single_chunk(self):
        assert _split_translation_text("short text") == [("short text", "")]

    def test_split_reconstructs_exact_text(self):
        text = "Alpha one.\n\nBeta two.\n\nGamma three."
        parts = _split_translation_text(text, max_chars=10)
        assert "".join(chunk + sep for chunk, sep in parts) == text
        for chunk, _sep in parts:
            assert chunk
            assert len(chunk) <= 10

    def test_paragraph_breaks_survive_chunking(self):
        para = "First sentence here. Second sentence too."
        text = "\n\n".join([para] * 4)
        parts = _split_translation_text(text, max_chars=len(para))
        assert "".join(c + s for c, s in parts) == text
        # Every chunk boundary is a paragraph break (separators are the
        # original blank-line runs, never empty mid-paragraph cuts here).
        assert all(sep == "\n\n" or sep == "" for _, sep in parts)
        assert len(parts) >= 2


# ── Cache layer (mirrors the audio TTSCache contract) ─────────────────────


class TestTranslationCache:
    def test_miss_then_set_then_hit(self, tmp_path):
        cache = TranslationCache(LocalStorage(str(tmp_path)))
        key = TranslationCache.build_key("Hello", "French")
        assert cache.get(key) is None
        cache.set(key, "Bonjour")
        assert cache.get(key) == "Bonjour"

    def test_set_skips_empty_results(self, tmp_path):
        cache = TranslationCache(LocalStorage(str(tmp_path)))
        key = TranslationCache.build_key("Hello", "French")
        cache.set(key, "")
        assert cache.get(key) is None

    def test_key_covers_text_and_target_language(self):
        k1 = TranslationCache.build_key("Hello", "French")
        k2 = TranslationCache.build_key("Hello", "German")
        k3 = TranslationCache.build_key("Hi", "French")
        assert k1 != k2
        assert k1 != k3
        assert k1 == TranslationCache.build_key("Hello", "French")

    def test_key_normalises_whitespace(self):
        assert TranslationCache.build_key(" Hello  ", "French") == TranslationCache.build_key(
            "Hello", "French"
        )


# ── translate(): cache miss/hit, chunking, max_tokens, retry ──────────────


class TestTranslateCaching:
    def test_cache_miss_makes_one_call_and_populates_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIO_STORAGE_PATH", str(tmp_path))
        completions = _install_openai(monkeypatch, lambda prompt: "Bonjour le monde")

        llm = OpenAILLM()
        first = llm.translate("Hello world", "French")

        assert first == "Bonjour le monde"
        assert len(completions.calls) == 1
        assert completions.calls[0]["max_tokens"] == _translation_max_tokens(len("Hello world"))

        key = TranslationCache.build_key("Hello world", "French")
        cache = TranslationCache()
        assert cache.get(key) == "Bonjour le monde"

    def test_cache_hit_skips_llm_entirely(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIO_STORAGE_PATH", str(tmp_path))
        cache = TranslationCache()
        key = cache.build_key("Bonjour", "German")
        cache.set(key, "Guten Tag")

        constructed = []
        monkeypatch.setattr(
            "openai.OpenAI",
            lambda **kwargs: constructed.append(kwargs) or _FakeClient(lambda p: "unused"),
        )

        llm = OpenAILLM()
        assert llm.translate("Bonjour", "German") == "Guten Tag"
        assert constructed == []  # cached text returned with zero API calls

    def test_long_text_is_chunked_and_cached_under_full_text_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIO_STORAGE_PATH", str(tmp_path))

        paras = [
            " ".join([f"Sentence number {i} in the first paragraph."] * 25)
            for i in range(4)
        ]
        text = "\n\n".join(paras)
        assert len(text) > _TRANSLATE_CHUNK_MAX_CHARS

        expected_parts = _split_translation_text(text)

        def handler(prompt):
            return f"[{_chunk_from_prompt(prompt)}]"

        completions = _install_openai(monkeypatch, handler)

        llm = OpenAILLM()
        result = llm.translate(text, "Hindi")

        assert len(completions.calls) == len(expected_parts)
        assert len(completions.calls) > 1  # truly chunked
        for call, (chunk, _sep) in zip(completions.calls, expected_parts):
            assert len(chunk) <= _TRANSLATE_CHUNK_MAX_CHARS
            assert call["max_tokens"] == _translation_max_tokens(len(chunk))
        assert result == "".join(f"[{c}]{s}" for c, s in expected_parts)

        # Populated under the FULL original text key, not per-chunk.
        cache = TranslationCache()
        assert cache.get(TranslationCache.build_key(text, "Hindi")) == result

        # Second call with the same full text is served entirely from cache.
        again = llm.translate(text, "Hindi")
        assert again == result
        assert len(completions.calls) == len(expected_parts)  # unchanged

    def test_deepseek_translate_caches_and_budgets_tokens(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIO_STORAGE_PATH", str(tmp_path))
        completions = _install_openai(monkeypatch, lambda prompt: "कृपया मदद करें")
        llm = DeepSeekLLM()
        assert llm.translate("Please help", "Hindi") == "कृपया मदद करें"
        assert len(completions.calls) == 1
        assert completions.calls[0]["max_tokens"] > 0
        assert llm.translate("Please help", "Hindi") == "कृपया मदद करें"
        assert len(completions.calls) == 1


class TestTranslateFailureHandling:
    def test_chunk_keeps_original_text_after_retry(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("AUDIO_STORAGE_PATH", str(tmp_path))

        def boom(prompt):
            raise RuntimeError("provider exploded")

        completions = _install_openai(monkeypatch, boom)

        llm = OpenAILLM()
        result = llm.translate("Keep me as-is", "Spanish")

        # One chunk, retried once -> two attempts, then the original text.
        assert len(completions.calls) == 2
        assert result == "Keep me as-is"
        assert any("chunk 0" in r.message for r in caplog.records)

    def test_each_chunk_failure_retries_once(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("AUDIO_STORAGE_PATH", str(tmp_path))
        paras = ["Long paragraph " + "word " * 400 for _ in range(4)]
        text = "\n\n".join(paras)
        expected_parts = _split_translation_text(text)
        assert len(expected_parts) > 1

        def boom(prompt):
            raise RuntimeError("provider exploded")

        completions = _install_openai(monkeypatch, boom)

        llm = OpenAILLM()
        result = llm.translate(text, "Hindi")

        assert len(completions.calls) == len(expected_parts) * 2  # one retry each
        assert result == text  # every chunk fell back to its original text