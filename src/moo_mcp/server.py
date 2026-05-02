from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .client import MOOClient, MOOError
from .config import Config


logger = logging.getLogger("moo_mcp")


READ_TOOLS: list[Tool] = [
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
            text = await _dispatch(client, config, name, arguments)
        except MOOError as exc:
            return [TextContent(type="text", text=f"error: {exc}")]
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool %s failed", name)
            return [TextContent(type="text", text=f"error: {exc}")]
        return [TextContent(type="text", text=text or "(no output)")]

    return server


async def _dispatch(
    client: MOOClient,
    config: Config,
    name: str,
    args: dict,
) -> str:
    if name == "moo_show":
        return await client.run(f"@show {args['object']}")
    if name == "moo_list_verb":
        return await client.run(f"@list {args['object']}:{args['verb']} with numbers")
    if name == "moo_verbs":
        return await client.run(f"@verbs {args['object']}")
    if name == "moo_props":
        return await client.run(f"@properties {args['object']}")
    if name == "moo_get_property":
        return await client.run(f"; {args['object']}.{args['property']}")
    if name == "moo_parent":
        return await client.run(f"; parent({args['object']})")
    if name == "moo_children":
        return await client.run(f"; children({args['object']})")
    if name == "moo_verb_info":
        return await client.run(f"@verb {args['object']}:{args['verb']}")

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
