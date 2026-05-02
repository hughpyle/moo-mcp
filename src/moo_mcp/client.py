from __future__ import annotations

import asyncio
import logging
import secrets

import telnetlib3

from .config import Config


logger = logging.getLogger(__name__)


class MOOError(Exception):
    pass


LOGIN_FAILURE_MARKERS = (
    "either that character",
    "wrong password",
    "incorrect",
    "no such player",
    "could not connect",
    "is not a valid",
)


class MOOClient:
    """Persistent telnet connection to a LambdaMOO server.

    Frames each command/response with a sentinel emitted via the MOO
    builtin notify(), which is available without requiring a particular
    core's $player_class:tell. Requires the programmer bit so `;` eval
    works.
    """

    def __init__(self, config: Config):
        self.config = config
        self.reader: telnetlib3.TelnetReader | None = None
        self.writer: telnetlib3.TelnetWriter | None = None
        self._cmd_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._connected = False

    async def ensure_connected(self) -> None:
        if self._connected:
            return
        async with self._connect_lock:
            if self._connected:
                return
            await self._connect()
            self._connected = True

    async def _connect(self) -> None:
        cfg = self.config
        logger.info("connecting to %s:%s", cfg.host, cfg.port)
        self.reader, self.writer = await asyncio.wait_for(
            telnetlib3.open_connection(
                cfg.host,
                cfg.port,
                connect_minwait=0.05,
                connect_maxwait=0.5,
            ),
            timeout=cfg.connect_timeout,
        )

        # Drain welcome banner.
        await self._drain_idle(0.5)

        if cfg.connect_command:
            line = cfg.connect_command.format(user=cfg.user, password=cfg.password)
        else:
            line = f"connect {cfg.user} {cfg.password}"
        self._send(line)

        nonce = secrets.token_hex(6)
        sentinel = f"##MOO_MCP_LOGIN_{nonce}##"
        self._send(f'; notify(player, "{sentinel}")')
        await self.writer.drain()

        try:
            pre = await asyncio.wait_for(
                self._read_until(sentinel),
                timeout=cfg.connect_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise MOOError(
                "login: sentinel not seen — check credentials, host/port, and "
                "that the character has the programmer bit"
            ) from exc
        except MOOError as exc:
            raise MOOError(f"login: connection closed before sentinel: {exc}") from exc

        lower = pre.lower()
        for bad in LOGIN_FAILURE_MARKERS:
            if bad in lower:
                raise MOOError(f"login failed: {pre.strip()[:400]}")

        await self._drain_idle(0.2)
        logger.info("logged in as %s", cfg.user)

    def _send(self, line: str) -> None:
        assert self.writer is not None
        self.writer.write(line + "\n")

    async def _drain_idle(self, idle_seconds: float) -> str:
        assert self.reader is not None
        buf: list[str] = []
        while True:
            try:
                chunk = await asyncio.wait_for(
                    self.reader.read(4096), timeout=idle_seconds
                )
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            buf.append(chunk)
        return "".join(buf)

    async def _read_until(self, marker: str) -> str:
        """Read until the marker substring appears.

        Returns text strictly before the line that contains the marker.
        Raises MOOError if the connection closes first.
        """
        assert self.reader is not None
        buf = ""
        while True:
            chunk = await self.reader.read(4096)
            if not chunk:
                raise MOOError("connection closed")
            buf += chunk
            idx = buf.find(marker)
            if idx != -1:
                line_start = buf.rfind("\n", 0, idx) + 1
                return buf[:line_start]

    async def run(self, command: str) -> str:
        """Send a single command line, return the captured output."""
        if "\n" in command or "\r" in command:
            raise MOOError("command must not contain newlines")
        await self.ensure_connected()
        async with self._cmd_lock:
            try:
                return await self._run_locked(command)
            except MOOError:
                # Force a reconnect on the next call.
                self._connected = False
                if self.writer is not None:
                    try:
                        self.writer.close()
                    except Exception:
                        pass
                raise

    async def _run_locked(self, command: str) -> str:
        cfg = self.config
        assert self.writer is not None
        nonce = secrets.token_hex(6)
        sentinel = f"##MOO_MCP_END_{nonce}##"

        self._send(command)
        self._send(f'; notify(player, "{sentinel}")')
        await self.writer.drain()

        try:
            pre = await asyncio.wait_for(
                self._read_until(sentinel),
                timeout=cfg.command_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise MOOError(f"command timed out: {command!r}") from exc

        # Drain the trailing "=> 0" from our own sentinel-eval.
        await self._drain_idle(0.15)
        return pre

    async def close(self) -> None:
        if self.writer is not None:
            try:
                self._send("@quit")
                await self.writer.drain()
            except Exception:
                pass
            try:
                self.writer.close()
            except Exception:
                pass
        self._connected = False
