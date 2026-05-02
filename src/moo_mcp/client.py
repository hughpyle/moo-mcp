from __future__ import annotations

import asyncio
import logging
import secrets
from collections import deque
from dataclasses import dataclass

import telnetlib3

from .config import Config


logger = logging.getLogger(__name__)


OBSERVATION_CAP = 1000


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


def _is_eval_echo(line: str) -> bool:
    """Recognise LambdaMOO's two trailing echo lines after `;<expr>`:
    the value line (`=> ...`) and the cost summary (`[used N ticks, ...]`).
    """
    if line.startswith("=> "):
        return True
    if line.startswith("[used ") and line.endswith("]"):
        return True
    return False


@dataclass
class Observations:
    lines: list[str]
    dropped: int


class MOOClient:
    """Persistent telnet connection with continuous reader.

    A background task reads the stream line-by-line and routes each line to
    one of two sinks based on state:

      - In-flight command: lines accumulate in a buffer until the command's
        sentinel is seen, at which point the buffered text resolves the
        awaiting future. The single `=> N` echo from our own sentinel-eval
        is then discarded.
      - Idle: lines accumulate in a capped deque of observations, which can
        be drained by `drain_observations()`.

    Requires the programmer bit so `;` eval works for the sentinel.
    """

    def __init__(self, config: Config):
        self.config = config
        self.reader: telnetlib3.TelnetReader | None = None
        self.writer: telnetlib3.TelnetWriter | None = None

        self._cmd_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._connected = False

        self._reader_task: asyncio.Task | None = None
        self._observations: deque[str] = deque()
        self._dropped = 0

        self._command_sentinel: str | None = None
        self._command_buffer: list[str] = []
        self._command_future: asyncio.Future[str] | None = None
        self._expect_echo = 0

    async def ensure_connected(self) -> None:
        if self._connected:
            return
        async with self._connect_lock:
            if self._connected:
                return
            try:
                await self._connect()
            except Exception:
                await self._drop_connection()
                raise
            else:
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

        self._observations.clear()
        self._dropped = 0
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name="moo-reader"
        )

        if cfg.connect_command:
            line = cfg.connect_command.format(user=cfg.user, password=cfg.password)
        else:
            line = f"connect {cfg.user} {cfg.password}"

        nonce = secrets.token_hex(6)
        sentinel = f"##MOO_MCP_LOGIN_{nonce}##"
        future = self._begin_command(sentinel)

        self._send(line)
        self._send(f'; notify(player, "{sentinel}")')
        await self.writer.drain()

        try:
            pre = await asyncio.wait_for(future, timeout=cfg.connect_timeout)
        except asyncio.TimeoutError as exc:
            pre = "".join(self._command_buffer)
            self._abort_command()
            for bad in LOGIN_FAILURE_MARKERS:
                if bad in pre.lower():
                    raise MOOError(f"login failed: {pre.strip()[:400]}") from exc
            detail = pre.strip()[:400]
            suffix = f"; last output: {detail}" if detail else ""
            raise MOOError(
                "login: sentinel not seen — check credentials, host/port, "
                "and that the user has the programmer bit"
                f"{suffix}"
            ) from exc

        lower = pre.lower()
        for bad in LOGIN_FAILURE_MARKERS:
            if bad in lower:
                raise MOOError(f"login failed: {pre.strip()[:400]}")

        # Surface the welcome banner / login output as observations so the
        # caller can see where they landed.
        for ln in pre.splitlines():
            self._add_observation(ln)
        logger.info("logged in as %s", cfg.user)

    async def _reader_loop(self) -> None:
        assert self.reader is not None
        buf = ""
        try:
            while True:
                chunk = await self.reader.read(4096)
                if not chunk:
                    raise MOOError("connection closed")
                buf += chunk
                while True:
                    nl = buf.find("\n")
                    if nl == -1:
                        break
                    line = buf[:nl]
                    if line.endswith("\r"):
                        line = line[:-1]
                    buf = buf[nl + 1 :]
                    self._on_line(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("reader exiting: %s", exc)
            self._handle_disconnect(exc)

    def _on_line(self, line: str) -> None:
        if self._command_sentinel and self._command_sentinel in line:
            full = "".join(self._command_buffer)
            self._command_buffer.clear()
            self._command_sentinel = None
            # LambdaMOO eval prints two trailing lines we want to swallow:
            #   => <value>
            #   [used N ticks, M seconds.]
            self._expect_echo = 2
            future = self._command_future
            self._command_future = None
            if future is not None and not future.done():
                future.set_result(full)
            return
        if self._command_sentinel is not None:
            self._command_buffer.append(line + "\n")
            return
        if self._expect_echo > 0:
            if _is_eval_echo(line):
                self._expect_echo -= 1
                return
            # Not an echo line; stop expecting and treat as observation.
            self._expect_echo = 0
        self._add_observation(line)

    def _add_observation(self, line: str) -> None:
        self._observations.append(line)
        while len(self._observations) > OBSERVATION_CAP:
            self._observations.popleft()
            self._dropped += 1

    def _handle_disconnect(self, exc: BaseException) -> None:
        self._connected = False
        if self._command_future is not None and not self._command_future.done():
            self._command_future.set_exception(MOOError(f"disconnected: {exc}"))
        self._command_future = None
        self._command_sentinel = None
        self._command_buffer.clear()
        self._expect_echo = 0

    def _begin_command(self, sentinel: str) -> asyncio.Future[str]:
        loop = asyncio.get_event_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._command_buffer.clear()
        self._command_sentinel = sentinel
        self._command_future = future
        self._expect_echo = 0
        return future

    def _abort_command(self) -> None:
        self._command_sentinel = None
        self._command_buffer.clear()
        if self._command_future is not None and not self._command_future.done():
            self._command_future.cancel()
        self._command_future = None
        self._expect_echo = 0

    async def _drop_connection(self) -> None:
        self._connected = False
        self._abort_command()
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass
        self.reader = None
        self.writer = None

    def _send(self, line: str) -> None:
        assert self.writer is not None
        self.writer.write(line + "\n")

    async def run(self, command: str) -> str:
        if "\n" in command or "\r" in command:
            raise MOOError("command must not contain newlines")
        async with self._cmd_lock:
            try:
                await self.ensure_connected()
                return await self._run_locked(command)
            except MOOError:
                await self._drop_connection()
                raise

    async def _run_locked(self, command: str) -> str:
        cfg = self.config
        assert self.writer is not None
        nonce = secrets.token_hex(6)
        sentinel = f"##MOO_MCP_END_{nonce}##"

        future = self._begin_command(sentinel)

        self._send(command)
        self._send(f'; notify(player, "{sentinel}")')
        await self.writer.drain()

        try:
            return await asyncio.wait_for(future, timeout=cfg.command_timeout)
        except asyncio.TimeoutError as exc:
            self._abort_command()
            raise MOOError(f"command timed out: {command!r}") from exc

    def drain_observations(self) -> Observations:
        lines = list(self._observations)
        dropped = self._dropped
        self._observations.clear()
        self._dropped = 0
        return Observations(lines=lines, dropped=dropped)

    async def close(self) -> None:
        if self.writer is not None:
            try:
                self._send("@quit")
                await self.writer.drain()
            except Exception:
                pass
        await self._drop_connection()
