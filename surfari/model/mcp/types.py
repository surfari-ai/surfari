from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class MCPTool:
    name: str
    description: str | None = None
    input_schema: Dict[str, Any] | None = None

@dataclass
class MCPResource:
    uri: str
    name: str
    description: str | None = None
    mime_type: str | None = None

@dataclass
class MCPServerInfo:
    id: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None

@dataclass
class MCPCallResult:
    ok: bool
    data: Any = None
    error: Optional[str] = None
    elapsed_ms: Optional[int] = None
