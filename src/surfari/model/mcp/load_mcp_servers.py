import os, json
from pathlib import Path
from typing import Tuple, Dict, Any, List

import surfari.util.config as config
from surfari.model.mcp.manager import MCPClientManager
from surfari.model.mcp.tool_registry import MCPToolRegistry
from surfari.model.mcp.mcp_types import MCPServerInfo

mcp_config_path = os.path.join(config.PROJECT_ROOT, "model", "mcp", "mcp.json")

def _expand_path(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))

def _expand_args(args: List[str]) -> List[str]:
    return [_expand_path(a) for a in args]

async def load_mcp_from_config(config_path: str | Path = mcp_config_path) -> MCPToolRegistry:
    """
    Load MCP servers from an mcp.json and return a ready manager + tool registry.

    Schema (examples):
    {
      "servers": {
        "filesystem": {
          "command": "mcp-server-filesystem",
          "args": ["/Users/you/Projects"],     // prefer positional roots
          "env": {"FOO":"bar"},                // optional
          "cwd": "/some/dir"                   // optional
        },
        "http_server": {
          "url": "http://localhost:8000/mcp"   // HTTP server, no command/args needed
        }
      }
    }
    """
    config_path = Path(config_path)
    with config_path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    servers: Dict[str, Dict[str, Any]] = cfg.get("servers", {})
    if not servers:
        raise ValueError("mcp.json has no 'servers' entries")

    mgr = MCPClientManager()
    added_ids: List[str] = []

    for sid, scfg in servers.items():
        url = scfg.get("url")
        if url:
            # HTTP mode
            info = MCPServerInfo(id=sid, command="", args=[], env={}, cwd="")
            # Some versions store url on the info object dynamically:
            setattr(info, "url", url)
            await mgr.add_server(info)
            added_ids.append(sid)
            continue

        command = scfg.get("command")
        if not command:
            raise ValueError(f"Server '{sid}' is missing 'command' or 'url'")

        args = scfg.get("args", [])
        env = {**os.environ, **scfg.get("env", {})}
        cwd = scfg.get("cwd")

        # Expand ~ and $VARS in args/cwd
        args = _expand_args(args)
        if cwd:
            cwd = _expand_path(cwd)

        info = MCPServerInfo(
            id=sid,
            command=command,
            args=args,
            env=env,
            cwd=cwd or ""
        )
        await mgr.add_server(info)  # stdio
        added_ids.append(sid)

    registry = MCPToolRegistry(mgr)
    await registry.refresh(server_ids=added_ids)
    return registry

async def _demo():
    registry = await load_mcp_from_config()
    print("Loaded tools:", registry.list_function_names())
    # Try a filesystem read if present:
    read_tool = next((n for n in registry.list_function_names() if n.endswith("__read_file") or n.endswith(".read_file") or n == "read_file"), None)
    if read_tool:
        res = await registry.execute(read_tool, {"path": mcp_config_path}, timeout_s=10)
        print("read_file ->", res.data if res.ok else res.error)
    await registry.aclose()

import asyncio; asyncio.run(_demo())
