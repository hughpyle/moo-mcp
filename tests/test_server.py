import asyncio
import unittest

from moo_mcp.client import MOOError
from moo_mcp.config import Config
from moo_mcp.server import _dispatch


class FakeClient:
    def __init__(self):
        self.commands = []

    async def run(self, command: str) -> str:
        self.commands.append(command)
        return command


class DispatchValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Config(host="example.test", port=8888, user="u", password="p")

    def dispatch(self, name: str, args: dict) -> str:
        self.client = FakeClient()
        return asyncio.run(_dispatch(self.client, self.config, name, args))

    def test_read_eval_tools_accept_simple_refs(self) -> None:
        out = self.dispatch(
            "moo_get_property", {"object": "$thing", "property": "description"}
        )

        self.assertEqual(out, "; $thing.description")

    def test_get_property_rejects_object_expression_injection(self) -> None:
        with self.assertRaises(MOOError):
            self.dispatch(
                "moo_get_property",
                {"object": "#1); player:tell(\"oops\")", "property": "name"},
            )

        self.assertEqual(self.client.commands, [])

    def test_get_property_rejects_property_expression_injection(self) -> None:
        with self.assertRaises(MOOError):
            self.dispatch(
                "moo_get_property",
                {"object": "#1", "property": "name); player:tell(\"oops\")"},
            )

        self.assertEqual(self.client.commands, [])

    def test_verb_tools_reject_multi_token_verbs(self) -> None:
        with self.assertRaises(MOOError):
            self.dispatch("moo_list_verb", {"object": "#1", "verb": "look with"})

        self.assertEqual(self.client.commands, [])


if __name__ == "__main__":
    unittest.main()
