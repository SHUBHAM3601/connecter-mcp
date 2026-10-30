"""Talk to a running Connecter through its two integration channels.

* JSON over TCP (request/response): each request is one JSON object sent on its own
  connection to Connecter's JSON port. Replies arrive as a new connection to the
  ``ResponseEndpoint`` we name, matched by ``CorrelationID``.
* Message folder (fire and forget): a JSON file dropped into Connecter's
  ``integrationBuffer`` folder, used by its DCC plugins to catalogue assets.
"""

from __future__ import annotations

import json
import socket
import time
import uuid
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from .config import Paths, discover


class ConnecterError(ToolError):
    """A failure worth showing to the user; MCP clients receive its message."""


class ConnecterClient:
    def __init__(self, paths: Paths | None = None):
        self.paths = paths or discover()

    def refresh(self) -> None:
        self.paths = discover()

    def _port(self) -> int:
        if not self.paths.json_port:
            self.refresh()
        if not self.paths.json_port:
            raise ConnecterError("Connecter's JSON port was not found. Is Connecter installed and running?")
        return self.paths.json_port

    def request(self, type_: str, payload: dict | None = None, timeout: float = 15.0) -> dict:
        """Send a request and return the reply payload. Raises ConnecterError on failure."""
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(0.5)
            guid = str(uuid.uuid4())
            message = {
                "Guid": guid,
                "Type": type_,
                "Payload": payload or {},
                "ResponseEndpoint": {"ChannelType": "tcp", "Address": f"127.0.0.1:{listener.getsockname()[1]}"},
            }
            try:
                with socket.create_connection(("127.0.0.1", self._port()), timeout=5) as s:
                    s.sendall(json.dumps(message).encode("utf-8"))
            except OSError as e:
                raise ConnecterError(f"Could not reach Connecter on port {self.paths.json_port}: {e}. Is it running?") from e

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                with conn:
                    conn.settimeout(timeout)
                    data = b""
                    while chunk := conn.recv(65536):
                        data += chunk
                reply = json.loads(data.decode("utf-8-sig"))
                if reply.get("CorrelationID") != guid:
                    continue
                body = reply.get("Payload") or {}
                if reply.get("Type") == "ErrorResponse":
                    if body.get("ErrorCode") == 3:  # no handler registered for this message type
                        raise ConnecterError(f"This Connecter workspace doesn't support '{type_}' "
                                             "(some features exist only in Connecter's team/hosted workspaces).")
                    raise ConnecterError(body.get("ErrorMessage") or "Connecter returned an error")
                if body.get("Status", 0) not in (0, None):
                    raise ConnecterError(body.get("ErrorMessage") or f"{type_} failed with status {body.get('Status')}")
                return body
        raise ConnecterError(f"No reply from Connecter to {type_} within {timeout:.0f}s")

    def send(self, type_: str, payload: dict) -> None:
        """Send a one-way message (no reply expected)."""
        message = {"Guid": str(uuid.uuid4()), "Type": type_, "Payload": payload}
        with socket.create_connection(("127.0.0.1", self._port()), timeout=5) as s:
            s.sendall(json.dumps(message).encode("utf-8"))

    def drop_message(self, type_: str, payload: dict, wait: float = 15.0) -> bool:
        """Write a message into the integration folder; return True once Connecter consumes it."""
        if not self.paths.buffer_dir:
            raise ConnecterError("Connecter's message folder was not found in the registry")
        path = Path(self.paths.buffer_dir) / f"{uuid.uuid4().hex}.json"
        path.write_text(json.dumps({"Version": 1, "Guid": str(uuid.uuid4()), "Type": type_, "Payload": payload}, indent=2), "utf-8")
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not path.exists():
                return True
            time.sleep(0.25)
        return False

    def is_running(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self._port()), timeout=1):
                return True
        except (OSError, ConnecterError):
            return False
