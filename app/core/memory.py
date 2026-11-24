from typing import Protocol

from app.core.auth import Identity
from app.core.contracts import Evidence


class MemoryProvider(Protocol):
    def search(self, identity: Identity, query: str) -> list[Evidence]:
        """Return visible, currently verified memory with versioned references."""
