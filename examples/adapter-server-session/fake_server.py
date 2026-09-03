"""Pure-standard-library fake server for the adapter-server-session example."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
import hashlib

class ServerConnectionError(ConnectionError):
    pass

@dataclass
class _Session:
    token: str
    polls: int = 0
    disconnected: bool = False
    status: str = "running"
    # Structured solve-time response, analogous to a solver log's solve time.
    solve_elapsed_seconds: float | None = 0.375
class FakeServer:
    """A process-local server; disconnecting transport does not release a license."""
    def __init__(self, endpoint: str = "fake://solver") -> None:
        self.endpoint = endpoint
        self.sessions: dict[str, _Session] = {}
        self.license_count = 0
        self.create_count = 0
        self._next_token = 1
        self._invalid_connections: set[str] = set()

    def connect(self, token: str | None = None) -> dict[str, str]:
        connection = f"conn-{self._next_token}"
        self._next_token += 1
        if connection in self._invalid_connections:
            raise ServerConnectionError("connection unavailable")
        return {"connection": connection, "endpoint": self.endpoint}

    def create_session(self, connection: dict[str, str], session_ref: str) -> str:
        self._check(connection)
        if session_ref in self.sessions:
            return self.sessions[session_ref].token
        token = f"session-token-{self._next_token}"
        self._next_token += 1
        self.sessions[session_ref] = _Session(token)
        self.create_count += 1
        self.license_count += 1
        return token

    def query(self, connection: dict[str, str], session_ref: str, token: str) -> str:
        self._check(connection)
        session = self.sessions.get(session_ref)
        if session is None or session.token != token or session.disconnected:
            raise KeyError(session_ref)
        session.polls += 1
        if session.polls >= 2:
            session.status = "completed"
        return session.status

    def export(self, connection: dict[str, str], session_ref: str, token: str) -> dict[str, Any]:
        self._check(connection)
        session = self.sessions.get(session_ref)
        if session is None or session.token != token or session.disconnected:
            raise KeyError(session_ref)
        return {
            "value": 42,
            "session_ref": session_ref,
            "solve": {"elapsed_seconds": session.solve_elapsed_seconds},
        }

    def disconnect(self, connection: dict[str, str], session_ref: str, token: str) -> None:
        self._check(connection)
        session = self.sessions.get(session_ref)
        if session is None or session.token != token:
            raise KeyError(session_ref)
        if not session.disconnected:
            session.disconnected = True
            self.license_count -= 1

    def invalidate_connections(self) -> None:
        """Make all currently issued transports unusable (sessions remain server-side)."""
        self._invalid_connections.update(
            f"conn-{n}" for n in range(1, self._next_token)
        )

    def _check(self, connection: dict[str, str]) -> None:
        if connection.get("connection") in self._invalid_connections:
            raise ServerConnectionError("transport lost")

def artifact_id(payload: Any) -> str:
    return "artifact:" + hashlib.sha256(repr(payload).encode()).hexdigest()
