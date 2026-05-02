from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import re
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .client import MOOClient, MOOError, Observations, _is_eval_echo
from .config import Config


logger = logging.getLogger("moo_mcp")


OBSERVATION_HEADER = "--- since last call ---"
OBJECT_REF_RE = re.compile(r"^(?:#-?\d+|\$[A-Za-z_][A-Za-z0-9_]*|me|here|player)$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
VERB_RE = re.compile(r"^[A-Za-z0-9_@?*!+\-/<>=]+$")


SUMMARY_EXPR = (
    "o={obj};"
    ' n=`o.name ! E_PERM => "?"`;'
    " l=`o.location ! E_PERM => #-1`;"
    " w=`o.owner ! E_PERM => #-1`;"
    ' notify(player, tostr(o)+" name="+toliteral(n));'
    ' notify(player, "parent: "+tostr(parent(o)));'
    ' notify(player, "location: "+tostr(l));'
    ' notify(player, "owner: "+tostr(w));'
    ' notify(player, "flags: r="+tostr(o.r)+" w="+tostr(o.w)+'
    '" f="+tostr(o.f)+" player="+tostr(is_player(o)));'
    ' notify(player,'
    ' "verbs("+tostr(length(verbs(o)))+"): "+toliteral(verbs(o)));'
    ' notify(player,'
    ' "properties("+tostr(length(properties(o)))+"): "+toliteral(properties(o)))'
)


def _strip_trailing_echo(out: str) -> str:
    """Drop the `=> <value>` and `[used N ticks, ...]` lines that follow our
    multi-notify summary eval. They're inside the captured command output
    (not after the framing sentinel), so the reader's echo filter doesn't
    catch them.
    """
    lines = out.splitlines()
    while lines and _is_eval_echo(lines[-1]):
        lines.pop()
    return "\n".join(lines)


def _format_observations(obs: Observations) -> str:
    if not obs.lines and obs.dropped == 0:
        return ""
    parts = [OBSERVATION_HEADER]
    if obs.dropped:
        parts.append(f"[{obs.dropped} earlier line(s) dropped — buffer cap reached]")
    parts.extend(obs.lines)
    return "\n".join(parts)


def _string_arg(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise MOOError(f"{key} must be a non-empty string")
    return value


def _object_ref(args: dict, key: str = "object") -> str:
    value = _string_arg(args, key)
    if not OBJECT_REF_RE.fullmatch(value):
        raise MOOError(
            f"{key} must be a simple MOO object reference "
            "(#123, #-1, $thing, me, here, or player)"
        )
    return value


def _property_name(args: dict, key: str = "property") -> str:
    value = _string_arg(args, key)
    if not IDENT_RE.fullmatch(value):
        raise MOOError(f"{key} must be a simple MOO property identifier")
    return value


def _verb_name(args: dict, key: str = "verb") -> str:
    value = _string_arg(args, key)
    if not VERB_RE.fullmatch(value):
        raise MOOError(f"{key} must be a single MOO verb token")
    return value


READ_TOOLS: list[Tool] = [
    Tool(
        name="moo_summary",
        description=(
            "Lean introspection of an object: name, parent, location, owner, "
            "flags, verb names, and property names (no values). Use this "
            "first; reach for moo_show only when you need full property "
            "values. object: `#123`, `$thing`, `me`, `here`, etc."
        ),
        inputSchema={
            "type": "object",
            "properties": {"object": {"type": "string"}},
            "required": ["object"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="moo_show",
        description=(
            "Run `@show <object>` and return the raw output. "
            "Lists properties, verbs, parent, location, owner. "
            "object: a MOO object reference such as `#123`, `$thing`, `me`, `here`."
        ),
        inputSchema={
            "type": "object",
            "properties": {"object": {"type": "string"}},
            "required": ["object"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="moo_list_verb",
        description=(
            "Run `@list <object>:<verb> with numbers` and return the verb source. "
            "verb may include verb arg specifiers if needed (e.g. `look_self`)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "object": {"type": "string"},
                "verb": {"type": "string"},
            },
            "required": ["object", "verb"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="moo_verbs",
        description="Run `@verbs <object>` — list verbs defined on the object.",
        inputSchema={
            "type": "object",
            "properties": {"object": {"type": "string"}},
            "required": ["object"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="moo_props",
        description="Run `@properties <object>` — list properties defined on the object.",
        inputSchema={
            "type": "object",
            "properties": {"object": {"type": "string"}},
            "required": ["object"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="moo_get_property",
        description=(
            "Read a property value via `;<object>.<property>`. "
            "Returns the MOO eval output (`=> <value>`)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "object": {"type": "string"},
                "property": {"type": "string"},
            },
            "required": ["object", "property"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="moo_parent",
        description="Return the parent of an object via `;parent(<object>)`.",
        inputSchema={
            "type": "object",
            "properties": {"object": {"type": "string"}},
            "required": ["object"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="moo_children",
        description="Return the children of an object via `;children(<object>)`.",
        inputSchema={
            "type": "object",
            "properties": {"object": {"type": "string"}},
            "required": ["object"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="moo_verb_info",
        description=(
            "Run `@verb <object>:<verb>` — show verb owner, perms, and args. "
            "Useful for inspecting permissions before reading source."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "object": {"type": "string"},
                "verb": {"type": "string"},
            },
            "required": ["object", "verb"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="moo_poll",
        description=(
            "Return any pending ambient messages received since the last "
            "tool call (room chatter, pages, system notifications) without "
            "sending a command. Empty if nothing new."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
]


WRITE_TOOLS: list[Tool] = [
    Tool(
        name="moo_eval",
        description=(
            "Evaluate a MOO expression with `;<expression>`. "
            "Can mutate state — only available when --allow-write is set."
        ),
        inputSchema={
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="moo_raw",
        description=(
            "Send a raw command line to the MOO. "
            "Can mutate state — only available when --allow-write is set. "
            "The command must be a single line (no newlines)."
        ),
        inputSchema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    ),
]


def build_server(config: Config) -> Server:
    server = Server("moo-mcp")
    client = MOOClient(config)

    tools = list(READ_TOOLS)
    if config.allow_write:
        tools.extend(WRITE_TOOLS)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return tools

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            if name == "moo_poll":
                await client.ensure_connected()
                obs_text = _format_observations(client.drain_observations())
                return [
                    TextContent(
                        type="text", text=obs_text or "(no pending observations)"
                    )
                ]
            text = await _dispatch(client, config, name, arguments)
        except MOOError as exc:
            return [TextContent(type="text", text=f"error: {exc}")]
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool %s failed", name)
            return [TextContent(type="text", text=f"error: {exc}")]
        text = text or "(no output)"
        obs_text = _format_observations(client.drain_observations())
        if obs_text:
            text = f"{text}\n\n{obs_text}"
        return [TextContent(type="text", text=text)]

    return server


async def _dispatch(
    client: MOOClient,
    config: Config,
    name: str,
    args: dict,
) -> str:
    if name == "moo_summary":
        obj = _object_ref(args)
        expr = SUMMARY_EXPR.format(obj=obj)
        out = await client.run(f"; {expr}")
        return _strip_trailing_echo(out)
    if name == "moo_show":
        obj = _object_ref(args)
        return await client.run(f"@show {obj}")
    if name == "moo_list_verb":
        obj = _object_ref(args)
        verb = _verb_name(args)
        return await client.run(f"@list {obj}:{verb} with numbers")
    if name == "moo_verbs":
        obj = _object_ref(args)
        return await client.run(f"@verbs {obj}")
    if name == "moo_props":
        obj = _object_ref(args)
        return await client.run(f"@properties {obj}")
    if name == "moo_get_property":
        obj = _object_ref(args)
        prop = _property_name(args)
        return await client.run(f"; {obj}.{prop}")
    if name == "moo_parent":
        obj = _object_ref(args)
        return await client.run(f"; parent({obj})")
    if name == "moo_children":
        obj = _object_ref(args)
        return await client.run(f"; children({obj})")
    if name == "moo_verb_info":
        obj = _object_ref(args)
        verb = _verb_name(args)
        return await client.run(f"@verb {obj}:{verb}")

    if not config.allow_write:
        raise MOOError(f"tool {name!r} requires --allow-write")

    if name == "moo_eval":
        return await client.run(f"; {args['expression']}")
    if name == "moo_raw":
        return await client.run(args["command"])

    raise MOOError(f"unknown tool: {name}")


async def _serve(config: Config) -> None:
    server = build_server(config)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="moo-mcp",
        description="stdio MCP server for exploring a LambdaMOO over telnet.",
    )
    parser.add_argument("--config", help="Path to config.toml")
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="Enable mutating tools (eval, raw). Overrides config.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    cfg = Config.load(Path(args.config) if args.config else None)
    if args.allow_write:
        cfg = dataclasses.replace(cfg, allow_write=True)

    try:
        asyncio.run(_serve(cfg))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
