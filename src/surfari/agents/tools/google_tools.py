import os
import json
import base64
import asyncio
import aiofiles
import httpx
from email.message import EmailMessage
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, Any, List
from urllib.parse import quote

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

from surfari.util.config import PROJECT_ROOT
import surfari.util.surfari_logger as surfari_logger
logger = surfari_logger.getLogger(__name__)


# --- Sheets scopes (reused via the generic scope upgrader) ---
SHEETS_SCOPE_READONLY = "https://www.googleapis.com/auth/spreadsheets.readonly"
SHEETS_SCOPE_RW = "https://www.googleapis.com/auth/spreadsheets"


class GmailClientAsync:
    """
    Minimal async Gmail client supporting send + read via Gmail REST API,
    plus lightweight Google Sheets helpers.

    - Tokens stored at:   PROJECT_ROOT/security/google_auth_token.json
    - Client secrets at:  PROJECT_ROOT/security/google_client_secret.json

    This version supports:
      * Automatic scope union/upgrade when an API requires broader permissions
      * Automatic single-retry on 401 (expired/invalid) and 403 insufficientPermissions
    """
    SCOPES = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
    ]

    def __init__(
        self,
        token_file: str = "google_auth_token.json",
        secrets_file: str = "google_client_secret.json",
        *,
        prefer_console: Optional[bool] = None,
    ):
        self.token_file = os.path.join(PROJECT_ROOT, "security", token_file)
        self.secrets_file = os.path.join(PROJECT_ROOT, "security", secrets_file)
        self.creds: Optional[Credentials] = None
        self.executor = ThreadPoolExecutor(max_workers=2)

        # Serialize all auth/refresh so only one flow occurs at a time
        self._auth_lock = asyncio.Lock()

        # Console-flow toggle (env wins if not explicitly provided)
        if prefer_console is None:
            env_val = os.getenv("GMAIL_OAUTH_CONSOLE", "0").lower()
            self.prefer_console = env_val in ("1", "true", "yes", "on")
        else:
            self.prefer_console = bool(prefer_console)

    async def _refresh_creds_async(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.executor, self.creds.refresh, Request())

    async def _run_oauth_flow(self, scopes: Optional[List[str]] = None) -> Credentials:
        """
        Launches the installed-app OAuth flow. Prefer an ephemeral local server,
        fall back to console flow if the port is unavailable or console is requested.
        Runs in a worker thread to avoid blocking the event loop.
        """
        scopes = scopes or self.SCOPES
        logger.debug("[🔐] Starting OAuth flow for scopes: %s", scopes)
        flow = InstalledAppFlow.from_client_secrets_file(self.secrets_file, scopes)

        loop = asyncio.get_running_loop()

        # If explicitly preferring console, go straight there.
        if self.prefer_console:
            logger.debug("[🔐] Using console OAuth flow (GMAIL_OAUTH_CONSOLE=1 or prefer_console=True)")
            return await loop.run_in_executor(self.executor, flow.run_console)

        def _local_server():
            # Ephemeral port (0) + loopback host; open browser if possible.
            return flow.run_local_server(host="127.0.0.1", port=0, open_browser=True, timeout_seconds=300)

        try:
            return await loop.run_in_executor(self.executor, _local_server)
        except OSError as e:
            # Typical: OSError: [Errno 48] Address already in use, or environment without browser
            logger.warning("Local OAuth server failed (%s). Falling back to console flow.", e)
            return await loop.run_in_executor(self.executor, flow.run_console)

    async def _ensure_scopes(self, desired_scopes: List[str]) -> None:
        """
        Ensure credentials include desired_scopes.
        If scopes are missing, re-run OAuth with the union of existing+desired scopes.
        Also handles refresh when token is expired.
        """
        desired = sorted(set(desired_scopes))
        async with self._auth_lock:
            existing_scopes: List[str] = []

            # Load existing token if present
            if os.path.exists(self.token_file):
                async with aiofiles.open(self.token_file, "r") as f:
                    token_data = await f.read()
                    try:
                        self.creds = Credentials.from_authorized_user_info(json.loads(token_data))
                        existing_scopes = list(self.creds.scopes or [])
                    except Exception as e:
                        logger.warning("Failed to load existing google_auth_token.json; will re-consent. Error: %s", e)
                        self.creds = None

            have = set(existing_scopes)
            need_upgrade = not set(desired).issubset(have)

            # If no creds OR invalid, try refresh (when appropriate) or re-consent
            if not self.creds or not self.creds.valid or need_upgrade:
                all_scopes = sorted(set(existing_scopes) | set(desired))
                if self.creds and self.creds.expired and self.creds.refresh_token and not need_upgrade:
                    logger.debug("[🔄] Refreshing access token…")
                    try:
                        await self._refresh_creds_async()
                    except Exception as e:
                        logger.error("Token refresh failed: %s", e)
                        self.creds = await self._run_oauth_flow(all_scopes)
                else:
                    logger.info("[🔐] Running OAuth flow for scopes: %s", all_scopes)
                    self.creds = await self._run_oauth_flow(all_scopes)

                # Persist updated credentials
                async with aiofiles.open(self.token_file, "w") as f:
                    await f.write(self.creds.to_json())

    async def authenticate(self) -> None:
        # Keep existing call sites working: ensure base Gmail scopes.
        await self._ensure_scopes(self.SCOPES)

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        need_scopes: Optional[List[str]] = None,
        timeout: float = 30.0,
        retry_on_401_403: bool = True,
    ) -> Dict[str, Any]:
        """
        Make an HTTP request to Google APIs with current creds, parse JSON, and
        auto-recover once from 401 invalid/expired token and 403 insufficientPermissions.

        - need_scopes: scopes that this endpoint requires (used to auto-upgrade on 403).
        - Returns: {"ok": True, "json": <parsed_json>} on success OR {"ok": False, "status": <int>, "error": <str>} on error.
        """
        # Ensure at least base Gmail scopes so self.creds is set (and token refreshed if needed).
        # If need_scopes is provided and goes beyond base, _ensure_scopes will union-upgrade as necessary.
        scopes = list(set((need_scopes or []) + self.SCOPES))
        await self._ensure_scopes(scopes)

        headers = {"Authorization": f"Bearer {self.creds.token}"}
        attempt = 0

        async with httpx.AsyncClient(timeout=timeout) as client:
            while True:
                attempt += 1
                try:
                    r = await client.request(method.upper(), url, headers=headers, params=params, json=json_body)
                    r.raise_for_status()
                    try:
                        return {"ok": True, "json": r.json()}
                    except Exception:
                        return {"ok": True, "json": {}}

                except httpx.HTTPStatusError as e:
                    status = e.response.status_code
                    text = e.response.text[:500]

                    # Try to parse Google error to detect reason
                    reason = ""
                    try:
                        ej = e.response.json()
                        reason = (ej.get("error", {}) or {}).get("status") or (ej.get("error", {}) or {}).get("message", "")
                        if not reason:
                            errs = (ej.get("error", {}) or {}).get("errors", [])
                            if errs and isinstance(errs, list):
                                reason = errs[0].get("reason", "") or errs[0].get("message", "")
                    except Exception:
                        pass

                    can_retry = retry_on_401_403 and attempt == 1  # retry only once
                    if can_retry and status == 401:
                        # Common: invalid/expired token; try refresh or full flow with current scopes
                        logger.info("[🔁] 401 received; attempting token refresh and retry…")
                        try:
                            await self._refresh_creds_async()
                            headers["Authorization"] = f"Bearer {self.creds.token}"
                            continue
                        except Exception as e2:
                            logger.warning("Refresh failed; re-consenting with current scopes. Error: %s", e2)
                            await self._ensure_scopes(scopes)
                            headers["Authorization"] = f"Bearer {self.creds.token}"
                            continue

                    if can_retry and status == 403 and "insufficient" in (reason or "").lower():
                        # We need to upgrade scopes for this operation
                        upgrade_scopes = need_scopes or []
                        if upgrade_scopes:
                            logger.info("[🔁] 403 insufficientPermissions; upgrading scopes: %s", upgrade_scopes)
                            await self._ensure_scopes(list(set(self.SCOPES + upgrade_scopes)))
                            headers["Authorization"] = f"Bearer {self.creds.token}"
                            continue

                    # If we reach here, no more retries
                    return {"ok": False, "status": status, "error": text}

                except Exception as e:
                    # Network/parse etc. Non-retryable here.
                    return {"ok": False, "status": -1, "error": str(e)}

    # ---------------------- READ ----------------------

    async def search_emails(self, query: str, max_results: int = 5) -> Dict[str, Any]:
        """
        Search messages by Gmail query. Returns lightweight info:
          {"ok": True, "messages": [{"id","threadId","snippet","headers":{…}}]}
        """
        await self.authenticate()
        list_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
        list_params = {"q": query, "maxResults": max(1, int(max_results))}
        logger.debug("[>] Gmail search: %s", list_params)

        r1 = await self._request_json("GET", list_url, params=list_params, timeout=30.0)
        if not r1.get("ok"):
            return {"ok": False, "error": r1.get("error", ""), "status": r1.get("status")}
        items = r1["json"].get("messages", []) or []
        out: List[Dict[str, Any]] = []

        # Fetch metadata for each message
        for m in items:
            mid = m.get("id")
            if not mid:
                continue
            msg_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}"
            params = {"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]}
            r2 = await self._request_json("GET", msg_url, params=params, timeout=30.0)
            if not r2.get("ok"):
                out.append({"id": mid, "error": r2.get("error", ""), "status": r2.get("status")})
                continue
            j = r2["json"]
            hdrs = {h["name"]: h["value"] for h in j.get("payload", {}).get("headers", [])}
            out.append({
                "id": j.get("id"),
                "threadId": j.get("threadId"),
                "snippet": j.get("snippet"),
                "headers": {
                    "From": hdrs.get("From"),
                    "To": hdrs.get("To"),
                    "Subject": hdrs.get("Subject"),
                    "Date": hdrs.get("Date"),
                },
            })

        return {"ok": True, "messages": out}

    async def get_message(self, message_id: str) -> Dict[str, Any]:
        """
        Get a single message (metadata + snippet). Returns {"ok": True, "message": {...}}.
        """
        await self.authenticate()
        url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}"
        params = {"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]}

        r = await self._request_json("GET", url, params=params, timeout=30.0)
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error", ""), "status": r.get("status")}
        j = r["json"]
        hdrs = {h["name"]: h["value"] for h in j.get("payload", {}).get("headers", [])}
        out = {
            "id": j.get("id"),
            "threadId": j.get("threadId"),
            "snippet": j.get("snippet"),
            "headers": {
                "From": hdrs.get("From"),
                "To": hdrs.get("To"),
                "Subject": hdrs.get("Subject"),
                "Date": hdrs.get("Date"),
            },
        }
        return {"ok": True, "message": out}

    # ---------------------- SEND ----------------------

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        cc: Optional[str] = None,
        bcc: Optional[str] = None,
        html: bool = False,
        from_addr: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send an email via Gmail. Returns {"ok": True, "id": "<gmail_message_id>"} on success.
        """
        await self.authenticate()
        msg = EmailMessage()
        if from_addr:
            msg["From"] = from_addr
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc
        msg["Subject"] = subject

        if html:
            msg.add_alternative(body, subtype="html")
        else:
            msg.set_content(body)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8").rstrip("=")

        url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
        payload = {"raw": raw}

        r = await self._request_json("POST", url, json_body=payload, timeout=30.0)
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error", ""), "status": r.get("status")}
        return {"ok": True, "id": r["json"].get("id")}

    # ---------------------- SHEETS: READ → JSON ----------------------

    async def sheets_read_to_json(
        self,
        spreadsheet_id: str,
        range_a1: str,
        header_row: int = 1,
    ) -> Dict[str, Any]:
        """
        Read a Sheets range and convert tabular data to list-of-dicts.
        Assumes the first row of the fetched range is the header.
        """
        need_scopes = [SHEETS_SCOPE_READONLY]

        url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{quote(range_a1, safe='!$:,A-Za-z0-9')}"
        params = {"majorDimension": "ROWS"}

        r = await self._request_json("GET", url, params=params, need_scopes=need_scopes, timeout=30.0)
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error", ""), "status": r.get("status")}

        payload = r["json"]
        values = payload.get("values", []) or []
        if not values:
            return {"ok": True, "rows": [], "columns": [], "count": 0}

        # Take the first row in the returned values as header
        columns = [str(c).strip() for c in (values[0] if values else [])]
        data_rows = values[1:] if len(values) > 1 else []

        # Build list-of-dicts; pad/truncate rows to header length
        rows: List[Dict[str, Any]] = []
        for raw in data_rows:
            row = list(raw)
            if len(row) < len(columns):
                row += [""] * (len(columns) - len(row))
            elif len(row) > len(columns):
                row = row[: len(columns)]
            rows.append({col: cell for col, cell in zip(columns, row)})

        return {"ok": True, "rows": rows, "columns": columns, "count": len(rows)}

    # ---------------------- SHEETS: CREATE from JSON ----------------------

    async def sheets_create_from_json(
        self,
        title: str,
        records: List[Dict[str, Any]],
        sheet_name: str = "Sheet1",
    ) -> Dict[str, Any]:
        """
        Create a new Google Sheet and populate it from a list of dicts.
        - 'title' creates the spreadsheet with that name.
        - 'records' is a list of dicts; columns are the union of keys (first-seen order).
        - 'sheet_name' sets the first sheet's title (default: Sheet1).
        """
        need_scopes = [SHEETS_SCOPE_RW]

        # 1) Create spreadsheet
        create_url = "https://sheets.googleapis.com/v4/spreadsheets"
        body_create = {"properties": {"title": title}, "sheets": [{"properties": {"title": sheet_name}}]}

        cr = await self._request_json("POST", create_url, json_body=body_create, need_scopes=need_scopes, timeout=60.0)
        if not cr.get("ok"):
            return {"ok": False, "error": cr.get("error", ""), "status": cr.get("status")}

        created = cr["json"]
        spreadsheet_id = created.get("spreadsheetId")
        if not spreadsheet_id:
            return {"ok": False, "error": "Create returned no spreadsheetId"}

        # 2) Prepare values (header + rows)
        columns: List[str] = []
        for rec in (records or []):
            for k in rec.keys():
                if k not in columns:
                    columns.append(k)

        values: List[List[Any]] = [columns]
        for rec in (records or []):
            values.append([rec.get(c, "") for c in columns])

        # 3) Write values via values.update
        rng = f"{sheet_name}!A1"
        update_url = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{quote(rng, safe='!$:,A-Za-z0-9')}"
        params = {"valueInputOption": "USER_ENTERED"}  # respect user formatting
        body_update = {"range": rng, "majorDimension": "ROWS", "values": values}

        ur = await self._request_json("PUT", update_url, params=params, json_body=body_update, need_scopes=need_scopes, timeout=60.0)
        if not ur.get("ok"):
            return {"ok": False, "error": ur.get("error", ""), "status": ur.get("status")}

        return {
            "ok": True,
            "spreadsheetId": spreadsheet_id,
            "sheetTitle": sheet_name,
            "columns": columns,
            "count": len(records or []),
        }


# ---------------------- Tool callables ----------------------

_gmail_singleton: Optional[GmailClientAsync] = None

def _client() -> GmailClientAsync:
    global _gmail_singleton
    if _gmail_singleton is None:
        _gmail_singleton = GmailClientAsync()
    return _gmail_singleton

# Keep signatures simple (AFC/OpenAI friendly) and return JSON-serializable dicts.

async def gmail_send_email(to: str, subject: str, body: str, cc: str = "", bcc: str = "", html: bool = False) -> Dict[str, Any]:
    """
    Send an email using the authenticated Gmail account.
    - to: comma-separated recipients
    - subject: subject line
    - body: plain text (or HTML if html=True)
    - cc, bcc: optional comma-separated lists
    - html: set True to send HTML body
    """
    return await _client().send_email(to=to, subject=subject, body=body, cc=(cc or None), bcc=(bcc or None), html=bool(html))

async def gmail_search_emails(query: str, max_results: int = 5) -> Dict[str, Any]:
    """
    Search inbox with a Gmail query (e.g., 'from:me after:1723950000', 'subject:OTP').
    Returns compact message list.
    """
    return await _client().search_emails(query=query, max_results=max_results)

async def gmail_get_message(message_id: str) -> Dict[str, Any]:
    """
    Fetch a single message by ID (metadata + snippet).
    """
    return await _client().get_message(message_id=message_id)

# --- Sheets tools ---

async def sheets_read_json(spreadsheet_id: str, range_a1: str, header_row: int = 1) -> Dict[str, Any]:
    """
    Read a Google Sheets range (A1 notation) and return JSON rows.
    - spreadsheet_id: the Sheet's ID (from URL)
    - range_a1: e.g., 'Sheet1!A1:D200' (first row is treated as header)
    - header_row: kept for API parity; header is taken from the first returned row
    """
    return await _client().sheets_read_to_json(spreadsheet_id=spreadsheet_id, range_a1=range_a1, header_row=header_row)

async def sheets_create_from_json(title: str, records: List[Dict[str, Any]], sheet_name: str = "Sheet1") -> Dict[str, Any]:
    """
    Create a Google Sheet and populate it with records (list of dicts).
    - title: spreadsheet title
    - records: list of JSON dicts (keys become columns)
    - sheet_name: first sheet/tab name
    """
    return await _client().sheets_create_from_json(title=title, records=records, sheet_name=sheet_name)

TOOLS = [gmail_send_email, gmail_search_emails, gmail_get_message, sheets_read_json, sheets_create_from_json]

if __name__ == "__main__":
    from surfari.model.tool_helper import _normalize_tools_for_openai

    normalized = _normalize_tools_for_openai(TOOLS)
    import json
    print(json.dumps(normalized, indent=2))