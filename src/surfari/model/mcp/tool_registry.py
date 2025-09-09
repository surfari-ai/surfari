from typing import Any, Dict, List, Optional, Tuple, Callable
import json
import jsonschema
import time

from surfari.model.mcp.manager import MCPClientManager
from surfari.model.mcp.mcp_types import MCPTool, MCPCallResult
from surfari.util import surfari_logger as _surfari_logger
logger = _surfari_logger.getLogger(__name__)

SAFE_NAME = str.maketrans({c: "_" for c in " /:\\|@#?&%$!^*()[]{}<>,=+~`\""})

def _jsonschema_to_gemini(schema: Dict[str, Any]) -> Dict[str, Any]:
    if not schema:
        return {"type": "OBJECT"}
    t = (schema.get("type") or "object").lower()
    if t == "object":
        props = {}
        for k, v in (schema.get("properties") or {}).items():
            props[k] = _jsonschema_to_gemini(v)
            if "description" in v:
                props[k]["description"] = v["description"]
        out = {"type": "OBJECT", "properties": props}
        if "required" in schema:
            out["required"] = schema["required"]
        return out
    if t == "array":
        return {"type": "ARRAY", "items": _jsonschema_to_gemini(schema.get("items") or {"type": "string"})}
    if t in ("string",):
        return {"type": "STRING"}
    if t in ("integer",):
        return {"type": "INTEGER"}
    if t in ("number",):
        return {"type": "NUMBER"}
    if t in ("boolean",):
        return {"type": "BOOLEAN"}
    return {"type": "STRING"}

def _fn_name(server_id: str, tool_name: str) -> str:
    return f"mcp__{server_id.translate(SAFE_NAME)}__{tool_name.translate(SAFE_NAME)}"


class MCPToolRegistry:
    def __init__(self, manager: MCPClientManager):
        self.manager = manager
        self._by_fn: Dict[str, Tuple[str, MCPTool]] = {}
        self._closed = False

    # ---- lifecycle ---------------------------------------------------------
    async def aclose(self) -> None:
        """Async cleanup: close underlying MCP sessions."""
        logger.debug("MCPToolRegistry closing...")
        if self._closed:
            return
        try:
            await self.manager.aclose()
        finally:
            self._closed = True

    async def __aenter__(self) -> "MCPToolRegistry":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # ---- existing API (refresh/execute/etc.) ------------------------------
    async def refresh(self, server_ids: Optional[List[str]] = None) -> None:
        logger.debug("MCPToolRegistry refreshing...")
        if self._closed:
            raise RuntimeError("MCPToolRegistry is closed")
        self._by_fn.clear()
        targets = server_ids or list(self.manager._sessions.keys())
        for sid in targets:
            tools = await self.manager.list_tools(sid)
            for t in tools:
                fn = _fn_name(sid, t.name)
                self._by_fn[fn] = (sid, t)

    def as_openai_tools(self) -> List[Dict[str, Any]]:
        out = []
        for fn, (_, tool) in self._by_fn.items():
            schema = tool.input_schema or {"type": "object", "properties": {}, "additionalProperties": True}
            out.append({
                "type": "function",
                "function": {
                    "name": fn[:64],
                    "description": (tool.description or "")[:512],
                    "parameters": schema
                }
            })
        return out

    def as_anthropic_tools(self) -> List[Dict[str, Any]]:
        out = []
        for fn, (_, tool) in self._by_fn.items():
            schema = tool.input_schema or {"type": "object", "properties": {}, "additionalProperties": True}
            out.append({
                "name": fn,
                "description": tool.description or "",
                "input_schema": schema
            })
        return out

    def as_gemini_function_declarations(self) -> List[Dict[str, Any]]:
        decls = []
        for fn, (_, tool) in self._by_fn.items():
            schema = tool.input_schema or {"type": "object", "properties": {}, "additionalProperties": True}
            decls.append({
                "name": fn[:64],
                "description": (tool.description or "")[:512],
                "parameters": _jsonschema_to_gemini(schema)
            })
        return decls

    def as_async_python_proxy_tools(self) -> list[Callable[..., Any]]:
        """
        Return async wrappers for each MCP tool as a **list** so callers can
        concatenate with other tool lists.

        Example:
            await registry.refresh()
            tools = registry.as_async_python_proxy_tools()
            by_name = {t.tool_name: t for t in tools}
            res = await by_name["mcp__filesystem__read_file"](path="...")

        Each wrapper accepts optional `_timeout_s=<float>` to override per-call timeout.
        """
        out: list[Callable[..., Any]] = []

        for fn_name, (_, mcp_tool) in self._by_fn.items():
            schema = mcp_tool.input_schema or {"type": "object", "properties": {}, "additionalProperties": True}

            def make_wrapper(bound_name: str, bound_schema: dict, bound_tool: MCPTool):
                async def _mcp_proxy(**kwargs):
                    timeout = kwargs.pop("_timeout_s", None)
                    logger.debug("MCP proxy %s called with args=%s, timeout=%s", bound_name, kwargs, timeout)
                    res: MCPCallResult = await self.execute(bound_name, kwargs, timeout_s=timeout)
                    if res.ok:
                        # ensure JSON-serializable
                        try:
                            json.dumps(res.data)
                            return res.data
                        except Exception:
                            return json.loads(json.dumps(res.data, default=lambda o: repr(o)))
                    return {"ok": False, "error": res.error}

                # Helpful metadata for introspection / adapters
                setattr(_mcp_proxy, "tool_name", bound_name)
                _mcp_proxy.__doc__ = (bound_tool.description or f"MCP tool '{bound_name}'")[:512]
                _mcp_proxy.__annotations__ = {k: Any for k in (bound_schema.get("properties") or {}).keys()}
                _mcp_proxy.__name__ = bound_name[:64]
                _mcp_proxy.__parameters_schema__ = bound_schema  # <— key line
                _mcp_proxy.__mcp_tool__ = bound_tool
                return _mcp_proxy

            out.append(make_wrapper(fn_name, schema, mcp_tool))

        return out

    async def execute(self, fn_name: str, arguments: Dict[str, Any] | None = None, timeout_s: Optional[float] = None) -> MCPCallResult:
        if fn_name not in self._by_fn:
            candidates = [k for k in self._by_fn if k.startswith(fn_name)]
            if len(candidates) == 1:
                fn_name = candidates[0]
            else:
                return MCPCallResult(ok=False, error=f"Unknown MCP tool: {fn_name}")

        server_id, tool = self._by_fn[fn_name]
        args = arguments or {}

        schema = tool.input_schema or {"type": "object", "properties": {}, "additionalProperties": True}
        try:
            jsonschema.validate(instance=args, schema=schema)
        except Exception as e:
            return MCPCallResult(ok=False, error=f"Schema validation failed: {e}")

        logger.debug("Delegating to MCPClientManager to call '%s' on server '%s' with args=%s timeout=%s",
                    tool.name, server_id, args, timeout_s)
        t0 = time.perf_counter()
        result = await self.manager.call_tool(server_id, tool.name, args, timeout_s)
        dt = (time.perf_counter() - t0) * 1000
        logger.debug("Call to MCP tool '%s' finished in %.1f ms", tool.name, dt)
        return result


    def has(self, fn_name: str) -> bool:
        return fn_name in self._by_fn

    def list_function_names(self) -> List[str]:
        return list(self._by_fn.keys())
