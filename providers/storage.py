import os
import uuid
from pathlib import Path
from abc import ABC, abstractmethod


class BaseStorage(ABC):
    @abstractmethod
    def save(self, session_id: str, filename: str, data: bytes) -> str:
        ...

    @abstractmethod
    def get_url(self, path: str) -> str:
        ...


class LocalStorage(BaseStorage):
    # Default lives OUTSIDE static/: files under static/ are mounted at the
    # site root and served publicly with no auth check, so stored media must
    # never be written there. Honours the same AUDIO_STORAGE_PATH env var that
    # config.Config exposes; read at construction time so a runtime override
    # is respected without a restart.
    @staticmethod
    def _default_base_dir() -> str:
        return os.environ.get("AUDIO_STORAGE_PATH", "audio_storage")

    def __init__(self, base_dir: str | None = None):
        self.base_dir = Path(base_dir or self._default_base_dir())
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, session_id: str, filename: str, data: bytes) -> str:
        file_dir = self.base_dir / session_id
        file_dir.mkdir(parents=True, exist_ok=True)
        file_path = file_dir / filename
        file_path.write_bytes(data)
        return f"audio/sessions/{session_id}/{filename}"

    def get_url(self, path: str) -> str:
        return f"/{path}"
