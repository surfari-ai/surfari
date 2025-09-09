from pathlib import Path
from typing import List, Dict, Any
import os, json, mimetypes, time
from mcp.server.fastmcp import FastMCP  # from the official MCP Python SDK

mcp = FastMCP("Surfari FS (Python)")
ALLOWED_ROOTS: list[Path] = []

def _inside_roots(p: Path) -> bool:
    rp = p.resolve()
    return any(str(rp).startswith(str(root.resolve())) for root in ALLOWED_ROOTS)

def _resolve(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    if not _inside_roots(p):
        raise ValueError(f"path outside allowed roots: {p}")
    return p

@mcp.tool()
def list_directory(path: str) -> list[str]:
    """List entries in a directory (names only)."""
    p = _resolve(path)
    return sorted([e.name for e in p.iterdir()])

@mcp.tool()
def read_file(path: str) -> str:
    """Read a text file (UTF-8 best-effort)."""
    p = _resolve(path)
    if p.is_dir():
        raise ValueError("path is a directory")
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except UnicodeDecodeError:
        return p.read_bytes().decode("utf-8", "ignore")

@mcp.tool(structured_output=False)
def get_file_info(path: str) -> dict[str, Any]:
    p = _resolve(path)
    st = p.stat()
    return {
        "path": str(p),
        "is_dir": p.is_dir(),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "mime": mimetypes.guess_type(str(p))[0],
    }

if __name__ == "__main__":
    import sys, logging
    logging.basicConfig(level=logging.INFO)
    roots = sys.argv[1:] or [os.getcwd()]
    ALLOWED_ROOTS[:] = [Path(r).expanduser().resolve() for r in roots]
    mcp.run(transport="stdio")  # IMPORTANT: no prints to stdout
