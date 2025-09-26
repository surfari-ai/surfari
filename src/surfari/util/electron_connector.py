
import socket
import json

from urllib.parse import urlparse
import surfari.util.surfari_logger as surfari_logger
logger = surfari_logger.getLogger(__name__)

async def send_to_electron(cmd):
    s = socket.create_connection(("127.0.0.1", 32123))
    s.sendall((json.dumps(cmd) + "\n").encode("utf-8"))
    data = s.recv(65536).decode("utf-8")
    s.close()
    return json.loads(data.strip())


def _norm(u: str | None) -> str:
    if not u:
        return ""
    return u.strip().rstrip("/")

def _host(u: str | None) -> str:
    """Return lowercase netloc (without leading www.). Empty for non-HTTP(S) schemes."""
    if not u:
        return ""
    try:
        p = urlparse(u)
        if p.scheme in ("data", "about", "chrome", "chrome-extension", ""):
            return ""
        h = (p.netloc or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""

async def _is_ui_page(p) -> bool:
    """Detect your Electron UI window so Playwright won’t ‘take it over’."""
    # window.name marker (set in your renderer preload)
    try:
        name = await p.evaluate("window.name")
        if name == "surfari-electron-ui":
            logger.debug("Detected Main UI Window by window.name")
            return True
    except Exception:
        pass
    # optional URL flag if you decide to use one
    try:
        if "surfari_ui=1" in (p.url or ""):
            return True
    except Exception:
        pass
    return False

async def pick_existing_page_for_url(context, url: str | None):
    """
    In an attached BrowserContext, pick the best existing page for `url`.

    Priority:
      1) exact URL match
      2) same host (domain) match
      3) blank-like (about:blank, chrome://newtab/, data:text/html..., start.surfari.local)
      4) first non-UI candidate

    Returns:
      playwright.async_api.Page | None
    """
    target = _norm(url)
    target_host = _host(target)

    exact_match = None
    host_match = None
    blank_like = None
    first_candidate = None

    logger.info("Attach mode: scanning %d pages", len(context.pages))

    for p in context.pages:
        try:
            cur = p.url or ""
        except Exception:
            cur = ""
        is_closed = False
        try:
            is_closed = p.is_closed()
        except Exception:
            pass

        logger.debug("Page has URL: %s (closed=%s)", cur, is_closed)

        # Skip the UI window and any closed pages
        try:
            if await _is_ui_page(p) or is_closed:
                logger.debug("Skipping page: %s (closed=%s)", cur, is_closed)
                continue
        except Exception:
            # If evaluate fails, continue with normal checks
            pass

        if first_candidate is None:
            first_candidate = p

        ncur = _norm(cur)

        # 1) exact URL match
        if target and ncur and ncur == target:
            exact_match = p
            logger.debug("Selected existing page by exact URL match: %s", cur)
            break

        # 2) host (domain) match
        if target_host and host_match is None:
            cur_host = _host(ncur)
            if cur_host and cur_host == target_host:
                host_match = p
                logger.debug("Remembering host-match page: %s (host=%s)", cur, cur_host)

        # 3) blank-like page
        if blank_like is None:
            low = ncur.lower()
            if (
                low == "about:blank"
                or low == "chrome://newtab/"
                or low.startswith("data:text/html")
                or "start.surfari.local" in low
            ):
                blank_like = p
                logger.debug("Remembering blank-like page: %s", cur)

    chosen = exact_match or host_match or blank_like or first_candidate
    if chosen:
        logger.debug(
            "Final chosen page URL: %s (exact=%s, host=%s, blank=%s)",
            getattr(chosen, "url", None),
            bool(exact_match), bool(host_match), bool(blank_like)
        )
    return chosen