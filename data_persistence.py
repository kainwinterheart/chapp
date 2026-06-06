"""File-per-conversation JSON persistence backed by an index file."""

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from settings import _SETTINGS_DIR, read_settings


class DataPersistenceManager:
    """Manages conversation persistence using per-conversation JSON files
    stored in a subdirectory of the settings directory, with an index file for ordering."""

    MAX_CONVERSATIONS = 50

    def __init__(self, data_dir: str | None = None) -> None:
        if data_dir is not None:
            self._data_dir = Path(data_dir)
        else:
            self._data_dir = Path(_SETTINGS_DIR) / "conversations"
        self._conversations: dict[str, dict] = {}
        self._index: list[dict] = []
        self._active_conversation_id: Optional[str] = None

    # ── Internal utilities ────────────────────────────────────────────

    def _ensure_dir(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def _index_path(self) -> Path:
        return self._data_dir / "index.json"

    def _conversation_path(self, conversation_id: str) -> Path:
        return self._data_dir / f"{conversation_id}.json"

    def _atomic_write(self, path: Path, data: object) -> None:
        """Write JSON to a temp file in the same directory, then os.replace."""
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), suffix=".tmp", prefix=path.name + "."
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _load_json_safe(self, path: Path) -> Optional[object]:
        """Read and parse JSON, returning None on any failure."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    # ── Public API ─────────────────────────────────────────────────────

    def load_all(self) -> None:
        """Load all conversations from disk into memory."""
        self._conversations.clear()
        self._active_conversation_id = None

        index_data = self._load_json_safe(self._index_path())
        if not isinstance(index_data, list):
            return

        # Only load the newest MAX_CONVERSATIONS entries from the index.
        # Oldest entries sit at the end of the list.
        trimmed = index_data[:self.MAX_CONVERSATIONS]

        self._index = trimmed

        for entry in self._index:
            conv_id = entry.get("id")
            if not conv_id:
                continue
            conv_path = self._conversation_path(conv_id)
            conv_data = self._load_json_safe(conv_path)
            if isinstance(conv_data, dict):
                self._conversations[conv_id] = conv_data

        # Active conversation is the newest (first) entry, most likely
        # what the user was last working on. Only set if the conversation
        # actually exists in memory (may have been corrupted/missing).
        if self._index:
            first_entry = self._index[0]
            if isinstance(first_entry, dict):
                first_id = first_entry.get("id")
                if first_id and first_id in self._conversations:
                    self._active_conversation_id = first_id

    def generate_conversation_id(self) -> str:
        """Generate a unique conversation ID.

        This ID is ephemeral — it is not persisted until the caller
        invokes create_conversation() with it.
        """
        return str(uuid.uuid4())

    def _evict_from_index(self) -> None:
        """Remove oldest entries from index if MAX_CONVERSATIONS is exceeded.

        Oldest entries are at the END of the index (index[0] is newest).
        Files are left on disk untouched.
        """
        while len(self._index) > self.MAX_CONVERSATIONS:
            oldest = self._index.pop()
            if not isinstance(oldest, dict):
                continue
            oldest_id = oldest.get("id")
            if not oldest_id:
                continue
            self._conversations.pop(oldest_id, None)
            # Reset active if the evicted conversation was the active one.
            if self._active_conversation_id == oldest_id:
                if self._index:
                    self._active_conversation_id = self._index[0].get("id")
                else:
                    self._active_conversation_id = None

    def create_conversation(
        self,
        conversation_id: str,
        session_id: Optional[str] = None,
    ) -> None:
        """Create a new conversation and persist it to disk."""
        now = time.time()
        settings = read_settings()
        conv_data: dict = {
            "id": conversation_id,
            "session_id": session_id,
            "messages": [],
            "created_at": now,
            "updated_at": now,
            "binary_path": settings.get("binary_path"),
            "working_directory": settings.get("working_directory"),
        }
        self._index.insert(0, {"id": conversation_id, "timestamp": now})
        self._conversations[conversation_id] = conv_data
        self._active_conversation_id = conversation_id
        self._ensure_dir()
        self._evict_from_index()
        self._atomic_write(self._conversation_path(conversation_id), conv_data)
        self._atomic_write(self._index_path(), self._index)

    def create_conversation_and_add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        session_id: Optional[str] = None,
    ) -> None:
        """Create a new conversation with an initial message in a single write."""
        now = time.time()
        settings = read_settings()
        message: dict = {"role": role, "content": content}
        conv_data: dict = {
            "id": conversation_id,
            "session_id": session_id,
            "messages": [message],
            "created_at": now,
            "updated_at": now,
            "binary_path": settings.get("binary_path"),
            "working_directory": settings.get("working_directory"),
        }
        self._index.insert(0, {"id": conversation_id, "timestamp": now})
        self._conversations[conversation_id] = conv_data
        self._active_conversation_id = conversation_id
        self._ensure_dir()
        self._evict_from_index()
        # Write conversation file first so it's never in a zero-message state.
        self._atomic_write(self._conversation_path(conversation_id), conv_data)
        self._atomic_write(self._index_path(), self._index)

    def add_message(
        self, conversation_id: str, role: str, content: str
    ) -> None:
        """Append a message to an existing conversation and persist it.

        Does NOT modify the index—ordering is purely positional
        (insertion order), and message appends never change position.
        """
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return
        message: dict = {"role": role, "content": content}
        conv["messages"].append(message)
        conv["updated_at"] = time.time()
        self._ensure_dir()
        self._atomic_write(self._conversation_path(conversation_id), conv)

    def get_active_session_id(self, conversation_id: str) -> Optional[str]:
        """Return the session_id for the named conversation."""
        conv = self._conversations.get(conversation_id)
        if conv:
            return conv.get("session_id")
        return None

    def set_active_session_id(self, conversation_id: str, session_id: str) -> None:
        """Set the session_id on the named conversation and persist."""
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return
        conv["session_id"] = session_id
        conv["updated_at"] = time.time()
        self._ensure_dir()
        self._atomic_write(
            self._conversation_path(conversation_id), conv
        )

    def resolve_subprocess_parameters(
        self, conversation_id: str
    ) -> dict:
        """Resolve subprocess parameters for a conversation.

        For each parameter independently: use the persisted conversation
        value if present, otherwise fall back to live settings.
        Also returns session_id from the conversation record.
        """
        settings = read_settings()
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return {
                "session_id": None,
                "binary_path": settings.get("binary_path"),
                "working_directory": settings.get("working_directory"),
            }

        resolved: dict = {}

        # Include session_id from the conversation record
        resolved["session_id"] = conv.get("session_id")

        # Resolve binary_path: persisted value first, live settings second
        persisted_bp = conv.get("binary_path")
        if persisted_bp is not None:
            resolved["binary_path"] = persisted_bp
        else:
            resolved["binary_path"] = settings.get("binary_path")

        # Resolve working_directory: persisted value first, live settings second
        persisted_wd = conv.get("working_directory")
        if persisted_wd is not None:
            resolved["working_directory"] = persisted_wd
        else:
            resolved["working_directory"] = settings.get("working_directory")

        return resolved

    def get_conversations(self) -> list[dict]:
        """Return a list of conversation summaries ordered newest-first.

        Index[0] is newest; iterate forward (no reversal).
        """
        results: list[dict] = []
        for entry in self._index:
            if not isinstance(entry, dict):
                continue
            conv_id = entry.get("id")
            if not conv_id:
                continue
            conv = self._conversations.get(conv_id)
            if conv is None:
                continue
            messages = conv.get("messages", [])
            preview = ""
            if messages:
                first = messages[0]
                if isinstance(first, dict):
                    preview = first.get("content", "") or ""
            results.append({
                "conversation_id": conv_id,
                "session_id": conv.get("session_id"),
                "preview": preview,
                "timestamp": conv.get("updated_at", 0),
            })
        return results

    def get_messages(self, conversation_id: str) -> list[dict]:
        """Return a copy of the messages list for a conversation."""
        conv = self._conversations.get(conversation_id)
        if conv is None:
            return []
        return list(conv.get("messages", []))

    def activate_conversation(self, conversation_id: str) -> None:
        """Activate a conversation by updating _active_conversation_id.

        Does NOT reorder the index—position is immutable after insert.
        """
        if conversation_id not in self._conversations:
            return
        self._active_conversation_id = conversation_id
        self._ensure_dir()
        self._atomic_write(self._index_path(), self._index)

    def clear_active_conversation(self) -> None:
        """Clear the active conversation without activating a new one."""
        self._active_conversation_id = None

    def get_active_conversation_id(self) -> Optional[str]:
        """Return the currently active conversation ID."""
        return self._active_conversation_id
