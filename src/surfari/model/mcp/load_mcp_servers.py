import os
import sys
import json
from pathlib import Path
from typing import Dict, Any, List, Optional

import surfari.util.config as config
import surfari.util.surfari_logger as _surfari_logger
from surfari.model.mcp.manager import MCPClientManager
from surfari.model.mcp.tool_registry import MCPToolRegistry
from surfari.model.mcp.mcp_types import MCPServerInfo

logger = _surfari_logger.getLogger(__name__)
mcp_config_path = os.path.join(config.PROJECT_ROOT, "model", "mcp", "mcp_config.json")


def _expand_path(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


def _expand_args(args: List[str]) -> List[str]:
    return [_expand_path(a) for a in args]


def _looks_like_fs_server(scfg: Dict[str, Any]) -> bool:
    cmd = (scfg.get("command") or "").lower()
    args = [str(a) for a in (scfg.get("args") or [])]
    if cmd in ("python", "python3", sys.executable.lower()):
        return len(args) >= 2 and args[0] == "-m" and "surfari.model.mcp.fs_server" in args[1]
    return False


def _derive_fs_root_from_args(scfg: Dict[str, Any]) -> str:
    args = scfg.get("args") or []
    if args:
        candidate = args[-1]
        if candidate == "-m" or "surfari.model.mcp.fs_server" in str(candidate):
            return "."
        return str(candidate)
    return scfg.get("cwd") or "."


def _maybe_start_embedded_http(sid: str, scfg: Dict[str, Any]) -> Optional[str]:
    """
    If embedded requested (or auto when frozen + FS stdio pattern), start in-process HTTP/SSE server.
    Returns URL or None if not applicable or if startup fails.
    """
    embedded_flag = scfg.get("embedded_http", None)

    # Explicit request
    wants_embed = embedded_flag is True

    # Auto-embed in PyInstaller if not explicitly set and looks like our FS stdio launcher
    if embedded_flag is None and getattr(sys, "frozen", False) and _looks_like_fs_server(scfg):
        wants_embed = True

    if not wants_embed:
        return None

    # Import lazily so frozen apps don't crash at import time if the helper wasn't bundled.
    try:
        from surfari.model.mcp.fs_http_embed import start_embedded_fs_server_http
    except Exception as e:
        logger.debug("[MCP] '%s': embedded_http requested but import failed: %s", sid, e)
        return None

    root = _expand_path(scfg.get("root") or _derive_fs_root_from_args(scfg))
    if not Path(root).expanduser().exists():
        logger.debug("[MCP] '%s': root '%s' not found; using config.upload_folder_path", sid, root)
        root = _expand_path(config.upload_folder_path)
        
    try:
        url = start_embedded_fs_server_http(root=root)  # e.g. http://127.0.0.1:17321/mcp
        logger.debug("[MCP] '%s': started embedded HTTP server at %s (root=%s)", sid, url, root)
        return url
    except Exception as e:
        logger.debug("[MCP] '%s': failed to start embedded HTTP server: %s", sid, e)
        return None


async def build_mcp_registry_from_config(config_path: str | Path = mcp_config_path) -> MCPToolRegistry:
    """
    Load MCP servers from an mcp_config.json and return a ready manager + tool registry.
    Transport precedence per server: URL > embedded_http > stdio.

    Path semantics are enforced by the *server*:
      - Clients may pass "/", ".", "/sub/sub", or "sub/sub".
      - The server normalizes these relative to its configured root.
    """
    config_path = Path(config_path)
    with config_path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    servers: Dict[str, Dict[str, Any]] = cfg.get("servers", {})
    if not servers:
        raise ValueError("mcp_config.json has no 'servers' entries")

    mgr = MCPClientManager()
    added_ids: List[str] = []
    failures: Dict[str, str] = {}

    for sid, scfg in servers.items():
        if scfg.get("disabled", False):
            logger.debug("[MCP] '%s': skipping disabled server", sid)
            continue
        try:
            explicit_url = scfg.get("url")
            if explicit_url and scfg.get("embedded_http") is True:
                logger.debug("[MCP] '%s': both 'url' and 'embedded_http' set; using 'url' and ignoring 'embedded_http'.", sid)

            url = explicit_url or _maybe_start_embedded_http(sid, scfg)

            if url:
                info = MCPServerInfo(id=sid, command="", args=[], env={}, cwd="")
                setattr(info, "url", url)
                try:
                    await mgr.add_server(info)
                    added_ids.append(sid)
                    continue
                except Exception as e:
                    failures[sid] = f"HTTP connect failed: {e}"
                    if not explicit_url and (scfg.get("command") or scfg.get("args")):
                        logger.debug("[MCP] '%s': HTTP connection failed; attempting stdio fallback...", sid)
                        url = None
                    else:
                        logger.debug("[MCP] '%s': Skipping stdio fallback because 'url' was explicitly configured.", sid)
                        continue

            # --- stdio fallback ---
            command = scfg.get("command")
            if not command:
                if sid not in failures:
                    failures[sid] = "No usable transport (no url/embedded_http success and no 'command')."
                continue

            args = scfg.get("args", [])
            env = {**os.environ, **scfg.get("env", {})}
            cwd = scfg.get("cwd")

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

            try:
                await mgr.add_server(info)  # stdio
                added_ids.append(sid)
            except Exception as e:
                failures[sid] = f"STDIO connect failed: {e}"

        except Exception as e:
            failures[sid] = f"Unhandled error: {e}"

    if failures:
        logger.debug("[MCP] Some servers failed to initialize:")
        for sid, msg in failures.items():
            logger.debug("  - %s: %s", sid, msg)

    registry = MCPToolRegistry(mgr)
    return registry


# --- demo (optional) ---
async def _demo():
    registry = await build_mcp_registry_from_config()
    await registry.refresh()  # load all servers
    names = registry.list_function_names()
    logger.debug("Loaded tools: %s", names)

    list_dir_tool = next((n for n in names if n.endswith("list_directory")), None)
    read_tool     = next((n for n in names if n.endswith("read_file")), None)
    stat_tool     = next((n for n in names if n.endswith("get_file_info")), None)
    search_tool   = next((n for n in names if n.endswith("search_files")), None)

    # Server normalizes paths: "/", ".", "/sub/child", "sub/child"
    if list_dir_tool:
        for p in ["/", ".", "/subfolder", "subfolder", "/nonexistent", "nonexistent"]:
            res = await registry.execute(list_dir_tool, {"path": p}, timeout_s=10)
            logger.debug("result: list_directory(%r) -> %s", p, res.data if res.ok else res.error)

    if read_tool:
        for p in ["/subfolder/testDocForUpload.txt", "subfolder/testDocForUpload.pdf"]:
            res = await registry.execute(read_tool, {"path": p}, timeout_s=10)
            logger.debug("result: read_file(%r) -> %s", p, res.data if res.ok else res.error)

    if stat_tool:
        for p in ["/testDocForUpload.pdf", "testDocForUpload.pdf"]:
            res = await registry.execute(stat_tool, {"path": p}, timeout_s=10)
            logger.debug("result: get_file_info(%r) -> %s", p, res.data if res.ok else res.error)

    if search_tool:
        res = await registry.execute(search_tool, {"path": "/", "pattern": "test*"}, timeout_s=10)
        logger.debug("result: search_files -> %s", res.data if res.ok else res.error)

    await registry.aclose()


if __name__ == "__main__":
    import asyncio
    asyncio.run(_demo())
