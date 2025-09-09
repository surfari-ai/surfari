# surfari/mcp_client/manager.py
import asyncio
from typing import Dict, Optional, List, Any, Callable, Union

from surfari.model.mcp.mcp_types import MCPServerInfo, MCPTool, MCPResource, MCPCallResult
from surfari.model.mcp.session import MCPHTTPClientSession, MCPStdioFastMCPClientSession
from surfari.util import surfari_logger as _surfari_logger
logger = _surfari_logger.getLogger(__name__)

# Unified session type (both share the same public API)
MCPAnySession = Union[MCPHTTPClientSession, MCPStdioFastMCPClientSession]


class MCPClientManager:
    def __init__(self, progress_cb: Optional[Callable[[str, int, int, str], None]] = None):
        # progress_cb kept for API compatibility (not used by current sessions)
        self._sessions: Dict[str, MCPAnySession] = {}
        self._progress_cb = progress_cb

    async def add_server(self, info: MCPServerInfo) -> None:
        """
        Add a server based on MCPServerInfo:
          - If `info.url` is present -> HTTP/SSE using MCPHTTPClientSession
          - Else -> STDIO using MCPStdioFastMCPClientSession with command/args
        """
        url = getattr(info, "url", None)
        if url:
            logger.debug(f"Adding MCP HTTP server '{info.id}' at {url}")
            sess: MCPAnySession = MCPHTTPClientSession(url)
        else:
            logger.debug(f"Adding MCP STDIO server '{info.id}' with command: {info.command} {' '.join(info.args or [])}")
            sess = MCPStdioFastMCPClientSession(
                command=info.command,
                args=info.args or [],
                cwd=info.cwd or None,
                env=info.env or None,
            )
        logger.debug(f"Connecting to MCP server '{info.id}'...")
        await sess.connect()
        self._sessions[info.id] = sess

    def has_server(self, server_id: str) -> bool:
        return server_id in self._sessions

    async def list_tools(self, server_id: str) -> List[MCPTool]:
        return await self._sessions[server_id].list_tools()

    async def list_resources(self, server_id: str) -> List[MCPResource]:
        return await self._sessions[server_id].list_resources()

    async def read_resource(self, server_id: str, uri: str) -> MCPCallResult:
        return await self._sessions[server_id].read_resource(uri)

    async def call_tool(
        self,
        server_id: str,
        name: str,
        arguments: Dict[str, Any] | None = None,
        timeout_s: float | None = 10.0,
    ) -> MCPCallResult:
        logger.debug(f"Calling tool '{name}' on server '{server_id}' with args: {arguments} and timeout: {timeout_s}")
        return await self._sessions[server_id].call_tool(name, arguments, timeout_s)

    async def aclose(self):
        logger.debug("MCPClientManager closing...")
        await asyncio.gather(*(s.aclose() for s in self._sessions.values()), return_exceptions=True)
        self._sessions.clear()
