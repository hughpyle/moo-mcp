import tempfile
import unittest
from pathlib import Path

from moo_mcp.config import Config


class ConfigTests(unittest.TestCase):
    def test_loads_allow_write_from_hyphenated_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "\n".join(
                    [
                        "[moo]",
                        'host = "example.test"',
                        "port = 8888",
                        'user = "u"',
                        'password = "p"',
                        "allow-write = true",
                    ]
                )
            )

            config = Config.load(path)

        self.assertTrue(config.allow_write)


if __name__ == "__main__":
    unittest.main()
