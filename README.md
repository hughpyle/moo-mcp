# moo-mcp

A small stdio MCP server that lets Claude Code (or any MCP client) explore a
LambdaMOO database over telnet.

Read-only by default: `@show`, `@verbs`, `@properties`, `@list`, `@verb`,
`parent()`, `children()`, and direct property reads. Mutating tools (`;eval`,
raw command line) are gated behind `--allow-write`.

The server requires a character with the **programmer bit** — it frames each
command/response with a `notify(player, ...)` sentinel emitted via `;` eval.

## Install

```sh
uv tool install --from . moo-mcp
# or, in a venv:
pip install .
```

## Configure

Copy `config.example.toml` to `~/.config/moo-mcp/config.toml` and fill in your
host, port, character, and password. Or set `MOO_MCP_CONFIG=/path/to/file`.

```toml
[moo]
host = "lambda.moo.mud.org"
port = 8888
user = "YourCharacter"
password = "your-password"
```

## Run

```sh
moo-mcp                  # read-only
moo-mcp --allow-write    # enable ;eval and raw command tools
moo-mcp --log-level INFO
```

## Register with Claude Code

```sh
claude mcp add moo -- moo-mcp
# or with explicit config path:
claude mcp add moo -- moo-mcp --config /path/to/config.toml
```

Then in a session: `@moo:moo_show #1`, `@moo:moo_list_verb $thing initialize`, etc.

## Tools

Read-only:

| Tool | MOO command |
|---|---|
| `moo_show` | `@show <object>` |
| `moo_verbs` | `@verbs <object>` |
| `moo_props` | `@properties <object>` |
| `moo_verb_info` | `@verb <object>:<verb>` |
| `moo_list_verb` | `@list <object>:<verb> with numbers` |
| `moo_get_property` | `; <object>.<property>` |
| `moo_parent` | `; parent(<object>)` |
| `moo_children` | `; children(<object>)` |

Gated behind `--allow-write`:

| Tool | MOO command |
|---|---|
| `moo_eval` | `; <expression>` |
| `moo_raw` | arbitrary single-line command |

## How framing works

After every command, the client sends:

```
; notify(player, "##MOO_MCP_END_<nonce>##")
```

It reads the stream until it sees the sentinel line, returns everything before
it as the command's output, then drains the trailing `=> 0` from the eval.
This avoids relying on any particular core's `:tell` verb and works without
the in-band MOO Client Protocol negotiation.

## Notes / limitations

- One persistent connection per server process. Commands are serialized.
- A timeout drops the connection and the next call reconnects.
- Login failures are detected by scanning pre-sentinel output for common
  failure phrases ("either that character", "wrong password", etc.).
- `parent()` assumes single inheritance (LambdaMOO classic). Forks with
  multi-parent objects (Stunt, ToastStunt) may need a different builtin —
  use `moo_eval` with `parents(<obj>)` until proper support is added.
