import logging
import sys
import os
import time
import json
import io
import atexit
from typing import Any, Dict
import surfari.util.config as config

# ---- custom log levels ----
TRACE_LEVEL = 5
logging.addLevelName(TRACE_LEVEL, "TRACE")
logging.TRACE = TRACE_LEVEL

def trace(self, message, *args, **kwargs):
    if self.isEnabledFor(TRACE_LEVEL):
        self._log(TRACE_LEVEL, message, args, **kwargs)

SENSITIVE_LEVEL = 1
logging.addLevelName(SENSITIVE_LEVEL, "SENSITIVE")
logging.SENSITIVE = SENSITIVE_LEVEL

def sensitive(self, message, *args, **kwargs):
    if self.isEnabledFor(SENSITIVE_LEVEL):
        self._log(SENSITIVE_LEVEL, message, args, **kwargs)

logging.Logger.trace = trace
logging.Logger.sensitive = sensitive

log_level = config.CONFIG["app"].get("log_level", logging.DEBUG)
log_output = config.CONFIG["app"].get("log_output", "stdout").lower()

# ---- preserve original stdout for machine events (before any redirect) ----
def _clone_stdout() -> io.TextIOBase | None:
    try:
        dup_fd = os.dup(1)  # duplicate current stdout FD
        # line-buffered text wrapper
        return os.fdopen(dup_fd, "w", buffering=1, encoding="utf-8", errors="replace")
    except Exception:
        # as a best-effort fallback, use current sys.stdout
        return getattr(sys, "stdout", None)

_ORIGINAL_STDOUT: io.TextIOBase | None = _clone_stdout()

def emit_event(etype: str, **data: Any) -> None:
    """
    NDJSON events -> original stdout (clean machine channel)
    Includes numeric 'ts' and human-readable 'ts_local'.
    """
    now = time.time()
    payload: Dict[str, Any] = {
        "type": etype,
        "ts": now,
        "ts_local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        **data,
    }
    line = json.dumps(payload, ensure_ascii=False)
    # Prefer FD 3 if available; otherwise the preserved original stdout
    stream = _ORIGINAL_STDOUT or sys.stdout
    try:
        stream.write(line + "\n")
        stream.flush()
    except Exception:
        pass

# ---- sink selection ----
log_handlers = []

if log_output == "file":
    # Send all human logs/prints/native writes to file,
    # but keep emit_event going to the preserved original stdout.
    log_path = os.path.join(config.logs_folder_path, "app.log")

    _log_file = open(log_path, mode="a", buffering=1, encoding="utf-8", errors="replace")
    atexit.register(lambda: (_log_file.flush(), _log_file.close()))

    # Redirect current stdout/stderr FDs to the file so non-Python writes follow too
    try:
        os.dup2(_log_file.fileno(), 1)  # stdout -> file
        os.dup2(_log_file.fileno(), 2)  # stderr -> file
    except Exception:
        # Fallback: Python-level only
        sys.stdout = _log_file
        sys.stderr = _log_file

    # Logging to the same file handle
    fh = logging.StreamHandler(_log_file)
    fh.setLevel(log_level)
    log_handlers.append(fh)

else:
    # - stdout: machine (emit_event uses _ORIGINAL_STDOUT)
    # - stderr: human logs
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(log_level)
    log_handlers.append(sh)

# ---- configure logging ----
formatter = logging.Formatter(
    fmt="%(asctime)s - %(levelname)s - %(name)s  -  %(funcName)s -  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
root = logging.getLogger()
root.setLevel(log_level)
for h in list(root.handlers):
    root.removeHandler(h)
for h in log_handlers:
    h.setFormatter(formatter)
    root.addHandler(h)

# ---- public API ----
def getLogger(name):
    logger = logging.getLogger(name)
    logger.log_text_to_file = log_text_to_file      # type: ignore[attr-defined]
    logger.emit_event = emit_event                  # type: ignore[attr-defined]
    return logger

async def log_text_to_file(site_id, text, *args):
    logger = getLogger(__name__)
    if not logger.isEnabledFor(logging.SENSITIVE):
        return
    current_time = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    arg0 = args[0] if len(args) > 0 else "navigation"
    arg1 = args[1] if len(args) > 1 else ""
    filename = f"{current_time}_site_id_{site_id}_{arg0}_{arg1}.txt"
    filename = filename.replace(" ", "_").replace(":", "_").replace("/", "_")
    filename = os.path.join(config.debug_files_folder_path, filename)
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        logging.getLogger(__name__).debug(f"An error occurred while saving text: {e}")
