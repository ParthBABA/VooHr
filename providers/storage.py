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
    def __init__(self, base_dir: str | None = None):
        self.base_dir = Path(base_dir or "static/audio/sessions")
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, session_id: str, filename: str, data: bytes) -> str:
        file_dir = self.base_dir / session_id
        file_dir.mkdir(parents=True, exist_ok=True)
        file_path = file_dir / filename
        file_path.write_bytes(data)
        return f"audio/sessions/{session_id}/{filename}"

    def get_url(self, path: str) -> str:
        return f"/{path}"
