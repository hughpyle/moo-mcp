import asyncio
import unittest
from unittest import mock

from moo_mcp.client import MOOClient, MOOError
from moo_mcp.config import Config


class FakeReader:
    def __init__(self, chunks: list[str]):
        self.chunks = list(chunks)

    async def read(self, _size: int) -> str:
        if self.chunks:
            return self.chunks.pop(0)
        await asyncio.sleep(3600)
        return ""


class FakeWriter:
    def __init__(self):
        self.closed = False
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.append(text)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class ClientLoginTests(unittest.TestCase):
    def test_login_failure_cleans_up_connection(self) -> None:
        holder: dict[str, FakeWriter] = {}

        async def fake_open_connection(*_args, **_kwargs):
            writer = FakeWriter()
            holder["writer"] = writer
            return FakeReader(["Either that character does not exist.\n"]), writer

        async def run_case() -> str:
            client = MOOClient(
                Config(
                    host="example.test",
                    port=8888,
                    user="missing",
                    password="bad",
                    connect_timeout=0.01,
                )
            )
            with mock.patch(
                "moo_mcp.client.telnetlib3.open_connection", fake_open_connection
            ):
                with self.assertRaises(MOOError) as raised:
                    await client.ensure_connected()
            self.assertIsNone(client.reader)
            self.assertIsNone(client.writer)
            self.assertIsNone(client._reader_task)
            self.assertFalse(client._connected)
            return str(raised.exception)

        message = asyncio.run(run_case())

        self.assertIn("login failed", message)
        self.assertTrue(holder["writer"].closed)


if __name__ == "__main__":
    unittest.main()
