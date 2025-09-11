import base64
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP, Context

from surfari.util import surfari_logger as _surfari_logger
logger = _surfari_logger.getLogger(__name__)

def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout_s: float = 5.0) -> None:
    deadline = time.time() + timeout_s
    last_err = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return
        except OSError as e:
            last_err = e
            time.sleep(0.05)
    raise RuntimeError(f"Embedded MCP HTTP server didn't open {host}:{port}: {last_err}")


def _inside(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except Exception:
        return False


# ---------- path normalization (server-side) --------------------------------

def _normalize_subpath(p: Optional[str]) -> str:
    """
    Map a client-supplied path to a safe *relative* subpath under the server root.

      - None, "", ".", "./", or "/"  -> "."
      - Leading "/" is stripped ("/foo/bar" -> "foo/bar")
      - Collapses ".", ".." segments; attempts to go above root clamp to "."
      - Normalizes separators to "/"

    Always returns a *relative* string suitable for joining with the root.
    """
    if not p:
        return "."
    s = str(p).strip()
    if s in (".", "./", "/"):
        return "."
    # Normalize separators; treat leading "/" as "from root"
    s = s.replace("\\", "/")
    while s.startswith("/"):
        s = s[1:]

    parts: List[str] = []
    for seg in s.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            else:
                # would escape above root; clamp
                return "."
        else:
            parts.append(seg)

    return "/".join(parts) if parts else "."


# ---------- server factory --------------------------------------------------

def make_fs_mcp(root: str) -> FastMCP:
    """
    Build a FastMCP v2 server that exposes simple filesystem tools rooted at `root`.

    Path semantics (server-enforced):
      - "/", ".", "./"  -> the configured root
      - "/sub/child" or "sub/child" -> subpath under the configured root
      - Any attempt to traverse above root with ".." is clamped to root
    """
    base = Path(root).expanduser().resolve()
    logger.info(f"Starting embedded MCP HTTP server with root: {base}")
    mcp = FastMCP("Surfari FS (Embedded HTTP)")

    def _resolve_safe(p: str) -> Path:
        sub = _normalize_subpath(p)
        tgt = (base / sub).resolve()
        if not _inside(base, tgt):
            # Should be unreachable due to clamping, but keep as a guardrail.
            raise ValueError("Path escapes allowed root")
        return tgt

    @mcp.tool
    def list_directory(path: str = ".") -> List[str]:
        """List entries in a directory (names only). Path is interpreted relative to the server root."""
        p = _resolve_safe(path)
        if not p.exists():
            return []
        if not p.is_dir():
            # For non-dir, return the single name (loose behavior)
            return [p.name]
        return sorted([e.name for e in p.iterdir()])

    @mcp.tool
    def get_file_info(path: str) -> Dict[str, Any]:
        """Stat a file or directory. Path is interpreted relative to the server root."""
        p = _resolve_safe(path)
        try:
            st = p.stat()
        except FileNotFoundError:
            return {"exists": False}

        return {
            "exists": True,
            "is_dir": p.is_dir(),
            "is_file": p.is_file(),
            "size": st.st_size,
            "mtime": st.st_mtime,
            "path": str(p),
            "name": p.name,
        }

    @mcp.tool
    def search_files(path: str, pattern: str = "*") -> List[str]:
        """Glob under a directory with a simple pattern (non-recursive). Path is relative to the server root."""
        p = _resolve_safe(path)
        if not p.is_dir():
            return []
        return sorted([e.name for e in p.glob(pattern)])

    @mcp.tool
    def read_file(path: str, max_bytes: int = 2 * 1024 * 1024) -> Dict[str, Any]:
        """
        Read a file. If it's text-like, return 'text'. Otherwise return 'bytes_b64'.
        Caps at max_bytes. Path is interpreted relative to the server root.
        """
        p = _resolve_safe(path)
        if not p.is_file():
            return {"ok": False, "error": "Not a file"}

        data = p.read_bytes()
        if len(data) > max_bytes:
            data = data[:max_bytes]
            truncated = True
        else:
            truncated = False

        try:
            text = data.decode("utf-8")
            return {"ok": True, "type": "text", "text": text, "truncated": truncated}
        except UnicodeDecodeError:
            b64 = base64.b64encode(data).decode("ascii")
            return {"ok": True, "type": "bytes_b64", "data": b64, "truncated": truncated}

    # Example resource (optional)
    @mcp.resource("surfari://root", mime_type="text/plain", name="Root Path")
    def root_resource():
        return str(base)

    # Example tool that uses Context (optional)
    @mcp.tool
    async def echo_info(msg: str, ctx: Context) -> str:
        """Example tool demonstrating ctx logging."""
        await ctx.info(f"[Surfari FS] {msg}")
        return f"echo: {msg}"

    return mcp


# ---------- embedded runner -------------------------------------------------

def start_embedded_fs_server_http(
    *,
    root: str,
    host: str = "127.0.0.1",
    port: Optional[int] = None,
    path: str = "/mcp",
) -> str:
    """
    Start a standalone FastMCP v2 server over HTTP/SSE in a background thread.

    Returns:
        URL like "http://127.0.0.1:17321/mcp"
    """
    mcp = make_fs_mcp(root)

    if port is None:
        port = _pick_free_port()

    exc_holder: dict[str, BaseException] = {}

    def _serve():
        try:
            # FastMCP v2 (your standalone lib) signature:
            # mcp.run(transport="http", host="127.0.0.1", port=8000, path="/mcp")
            mcp.run(transport="http", host=host, port=port, path=path)
        except BaseException as e:  # pragma: no cover
            exc_holder["exc"] = e

    t = threading.Thread(target=_serve, name="Surfari-Embedded-MCP-HTTP", daemon=True)
    t.start()

    _wait_for_port(host, port, timeout_s=5.0)

    if "exc" in exc_holder:
        raise RuntimeError(f"Embedded MCP HTTP server failed: {exc_holder['exc']}")

    return f"http://{host}:{port}{path}"
