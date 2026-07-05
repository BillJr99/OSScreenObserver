"""
mcp_server — MCP stdio server (package form of the former mcp_server.py).

Implements the Model Context Protocol (2024-11-05) as JSON-RPC 2.0 over
stdin/stdout so that any MCP-capable client (Claude Desktop, Claude Code,
etc.) can use this server as a tool provider.

ALL output to stdout is MCP protocol JSON.  All logging goes to stderr so
that the MCP framing on stdout is never polluted.

P3 decomposition: the implementation now lives in submodules
(tool_schemas, server).  This __init__ re-exports the pre-split surface
so `from mcp_server import MCPServer` / `mcp_server._TOOLS` keep working
unchanged.  (The package is deliberately named mcp_server — not mcp — to
avoid colliding with the external MCP SDK distribution name.)
"""

from __future__ import annotations

from mcp_server.server import MCPServer
from mcp_server.tool_schemas import _TOOLS

__all__ = ["MCPServer", "_TOOLS"]
