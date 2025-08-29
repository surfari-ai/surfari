import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Callable, Tuple

from surfari.model.mcp.types import MCPTool, MCPResource, MCPCallResult, MCPServerInfo

# stdio transport
from fastmcp.client.transports import StdioTransport
from fastmcp import Client as FastMCPClient

# ---------- Shared base (caching + normalization) ----------
class _BaseMCPClientSession(ABC):
    def __init__(self, progress_cb: Optional[Callable[[int, int, str], None]] = None):
        self.progress_cb = progress_cb
        self._tools: List[MCPTool] = []
        self._resources: List[MCPResource] = []
        self._cap_lock = asyncio.Lock()

    # lifecycle
    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def aclose(self) -> None:  ...

    # low-level RPCs (implemented by transports)
    @abstractmethod
    async def _rpc_list_tools(self) -> List[Any]: ...
    @abstractmethod
    async def _rpc_list_resources(self) -> List[Any]: ...
    @abstractmethod
    async def _rpc_read_resource(self, uri: str) -> List[Any]: ...
    @abstractmethod
    async def _rpc_call_tool(self, name: str, args: Dict[str, Any]) -> Any: ...

    # normalization helpers (shared)
    @staticmethod
    def _norm_tools(raw: List[Any]) -> List[MCPTool]:
        out: List[MCPTool] = []
        for t in raw or []:
            # tolerate both attrs/dicts across libs
            name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
            desc = getattr(t, "description", None) or (t.get("description") if isinstance(t, dict) else None)
            schema = (
                getattr(t, "inputSchema", None)
                or getattr(t, "input_schema", None)
                or (t.get("inputSchema") if isinstance(t, dict) else None)
                or (t.get("input_schema") if isinstance(t, dict) else None)
            )
            if name:
                out.append(MCPTool(name=name, description=desc, input_schema=schema))
        return out

    @staticmethod
    def _norm_resources(raw: List[Any]) -> List[MCPResource]:
        out: List[MCPResource] = []
        for r in raw or []:
            uri = getattr(r, "uri", None) or (r.get("uri") if isinstance(r, dict) else None)
            if not uri:
                continue
            name = getattr(r, "name", None) or (r.get("name") if isinstance(r, dict) else uri)
            mime = (
                getattr(r, "mimeType", None)
                or getattr(r, "mime_type", None)
                or (r.get("mimeType") if isinstance(r, dict) else None)
                or (r.get("mime_type") if isinstance(r, dict) else None)
            )
            desc = getattr(r, "description", None) or (r.get("description") if isinstance(r, dict) else None)
            out.append(MCPResource(uri=uri, name=name, description=desc, mime_type=mime))
        return out

    @staticmethod
    def _norm_parts(parts: List[Any]) -> List[Dict[str, Any]]:
        payloads: List[Dict[str, Any]] = []
        for p in parts or []:
            ptype = getattr(p, "type", None) or (p.get("type") if isinstance(p, dict) else None) or "text"
            if ptype == "image":
                payloads.append({
                    "type": "image",
                    "mimeType": getattr(p, "mimeType", None) or (p.get("mimeType") if isinstance(p, dict) else None),
                    "data": getattr(p, "data", None) or (p.get("data") if isinstance(p, dict) else None),
                })
            else:
                payloads.append({
                    "type": "text",
                    "mimeType": getattr(p, "mimeType", None) or (p.get("mimeType") if isinstance(p, dict) else None),
                    "text": getattr(p, "text", None) if hasattr(p, "text") else (p.get("text") if isinstance(p, dict) else ""),
                })
        return payloads

    # public API (cached)
    async def refresh_capabilities(self) -> None:
        async with self._cap_lock:
            try:
                self._tools = self._norm_tools(await self._rpc_list_tools())
            except Exception:
                self._tools = []
            try:
                self._resources = self._norm_resources(await self._rpc_list_resources())
            except Exception:
                self._resources = []

    async def list_tools(self) -> List[MCPTool]:
        return list(self._tools)

    async def list_resources(self) -> List[MCPResource]:
        return list(self._resources)

    async def read_resource(self, uri: str) -> MCPCallResult:
        start = time.monotonic()
        try:
            parts = await self._rpc_read_resource(uri)
            payloads = self._norm_parts(parts)
            return MCPCallResult(ok=True, data=payloads, elapsed_ms=int((time.monotonic() - start) * 1000))
        except Exception as e:
            return MCPCallResult(ok=False, error=str(e))

    async def call_tool(self, name: str, arguments: Dict[str, Any] | None = None, timeout_s: Optional[float] = None) -> MCPCallResult:
        start = time.monotonic()
        try:
            coro = self._rpc_call_tool(name, arguments or {})
            result = await (asyncio.wait_for(coro, timeout=timeout_s) if timeout_s else coro)
            # Prefer .data (fastmcp CallToolResult), else return raw
            data = getattr(result, "data", None)
            return MCPCallResult(ok=True, data=(data if data is not None else result), elapsed_ms=int((time.monotonic() - start) * 1000))
        except asyncio.TimeoutError:
            return MCPCallResult(ok=False, error=f"Timed out after {timeout_s}s")
        except Exception as e:
            return MCPCallResult(ok=False, error=str(e))

    # hook for stdio progress
    def _on_progress(self, current: int, total: int, message: str):
        if self.progress_cb:
            try:
                self.progress_cb(current, total, message)
            except Exception:
                pass


# ---------- STDIO transport ----------

# Add this import at top

class MCPStdioFastMCPClientSession(_BaseMCPClientSession):
    """
    STDIO-backed session using fastmcp.Client + StdioTransport.
    This matches the behavior of your small working client.
    """
    def __init__(self, command: str, args: list[str], cwd: Optional[str] = None, env: Optional[dict] = None):
        super().__init__(progress_cb=None)  # no progress callbacks here
        self._client: Optional[FastMCPClient] = None
        self._transport = StdioTransport(
            command=command,
            args=args,
            cwd=cwd,
            env=env,
        )

    async def connect(self):
        self._client = FastMCPClient(self._transport)
        await self._client.__aenter__()
        await self.refresh_capabilities()

    async def aclose(self):
        if self._client:
            try:
                await self._client.__aexit__(None, None, None)
            finally:
                self._client = None
        self._tools.clear()
        self._resources.clear()

    # low-level RPCs
    async def _rpc_list_tools(self):
        return await self._client.list_tools()

    async def _rpc_list_resources(self):
        try:
            return await self._client.list_resources()
        except Exception:
            return []

    async def _rpc_read_resource(self, uri: str):
        return await self._client.read_resource(uri)

    async def _rpc_call_tool(self, name: str, args: Dict[str, Any]):
        return await self._client.call_tool(name, args)

# ---------- HTTP/SSE transport (FastMCP client) ----------

class MCPHTTPClientSession(_BaseMCPClientSession):
    """
    HTTP/SSE-backed session using fastmcp.Client(url).
    """
    def __init__(self, url: str):
        super().__init__(progress_cb=None)  # FastMCP HTTP client doesn't expose progress callbacks
        self.url = url
        self._client: Optional[FastMCPClient] = None

    async def connect(self):
        self._client = FastMCPClient(self.url)
        await self._client.__aenter__()
        await self.refresh_capabilities()

    async def aclose(self):
        if self._client:
            try:
                await self._client.__aexit__(None, None, None)
            finally:
                self._client = None
        self._tools.clear()
        self._resources.clear()

    # low-level RPCs
    async def _rpc_list_tools(self) -> List[Any]:
        return await self._client.list_tools()

    async def _rpc_list_resources(self) -> List[Any]:
        try:
            return await self._client.list_resources()
        except Exception:
            return []

    async def _rpc_read_resource(self, uri: str) -> List[Any]:
        return await self._client.read_resource(uri)

    async def _rpc_call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        return await asyncio.wait_for(self._client.call_tool(name, args), timeout=1.0)
