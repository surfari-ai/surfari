import os, json
from pathlib import Path
from typing import Dict, Any, List

import surfari.util.config as config
from surfari.model.mcp.manager import MCPClientManager
from surfari.model.mcp.tool_registry import MCPToolRegistry
from surfari.model.mcp.mcp_types import MCPServerInfo

mcp_config_path = os.path.join(config.PROJECT_ROOT, "model", "mcp", "mcp_config.json")

def _expand_path(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))

def _expand_args(args: List[str]) -> List[str]:
    return [_expand_path(a) for a in args]

async def build_mcp_registry_from_config(config_path: str | Path = mcp_config_path) -> MCPToolRegistry:
    """
    Load MCP servers from an mcp_config.json and return a ready manager + tool registry.

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
        raise ValueError("mcp_config.json has no 'servers' entries")

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
    return registry

async def _demo():
    registry = await build_mcp_registry_from_config()
    await registry.refresh()  # load all servers
    names = registry.list_function_names()
    print("Loaded tools:", names)

    # resolve tools by simple suffix/exact match (same style as your read_file)
    list_dir_tool = next((n for n in names if n.endswith("list_directory")), None)
    read_tool     = next((n for n in names if n.endswith("read_file")), None)
    stat_tool     = next((n for n in names if n.endswith("get_file_info")), None)
    search_tool   = next((n for n in names if n.endswith("search_files")), None)

    cfg_dir = os.path.dirname(mcp_config_path)

    # 1) list_directory (directory that contains mcp_config.json)
    if list_dir_tool:
        res = await registry.execute(list_dir_tool, {"path": cfg_dir}, timeout_s=10)
        print("list_directory ->", res.data if res.ok else res.error)

    # 2) read_file (your existing demo)
    if read_tool:
        res = await registry.execute(read_tool, {"path": mcp_config_path}, timeout_s=10)
        print("read_file ->", res.data if res.ok else res.error)

    # 3) get_file_info/stat
    if stat_tool:
        res = await registry.execute(stat_tool, {"path": mcp_config_path}, timeout_s=10)
        print("get_file_info/stat ->", res.data if res.ok else res.error)

    # 4) search_files (try a simple pattern under cfg_dir)
    if search_tool:
        res = await registry.execute(search_tool, {"path": cfg_dir, "pattern": "*.json"}, timeout_s=10)
        print("search_files ->", res.data if res.ok else res.error)

    await registry.aclose()

#import asyncio; asyncio.run(_demo())
