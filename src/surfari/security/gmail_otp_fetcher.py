import re
import time
import asyncio
from typing import Optional, List, Dict, Any
import surfari.util.surfari_logger as surfari_logger
from surfari.agents.tools.google_tools import gmail_search_emails

logger = surfari_logger.getLogger(__name__)

class GmailOTPClientAsync:
    """
    OTP helper built on top of google_tools' high-level functions.
    - No direct OAuth/HTTP here; we rely on google_tools to handle scopes, tokens, retries.
    """

    def __init__(self):
        # retain a spot for future config if needed
        pass

    # ---------- Public API ----------
    async def get_otp_code(
        self,
        from_me: bool = True,
        within_seconds: int = 30,
        retry_interval: int = 10,
        max_retries: int = 6,
        max_results: int = 10,
        include_body_lookup: bool = True,
    ) -> Optional[str]:
        """
        Retry loop to fetch the most recent OTP code.
        - from_me: restrict to messages sent by me
        - within_seconds: only search recent messages (Gmail supports 'after:<unix_ts>')
        - retry_interval: seconds to wait between attempts
        - max_retries: number of attempts
        - max_results: how many messages to fetch per query (most recent first)
        - include_body_lookup: if true, fall back to reading message body/snippet if Subject has no code
        """
        for attempt in range(1, max_retries + 1):
            logger.debug(f"[🔄] OTP fetch attempt {attempt}/{max_retries}…")
            code = await self.get_latest_code(
                from_me=from_me,
                within_seconds=within_seconds,
                max_results=max_results,
                include_body_lookup=include_body_lookup,
            )
            if code:
                return code
            if attempt < max_retries:
                logger.debug(f"[⏳] No OTP yet; sleeping {retry_interval}s before next attempt…")
                await asyncio.sleep(retry_interval)
        return None

    async def get_latest_code(
        self,
        from_me: bool = True,
        within_seconds: int = 600,
        max_results: int = 10,
        include_body_lookup: bool = True,
    ) -> Optional[str]:
        """
        Single-shot attempt to fetch the latest OTP code within the time window.
        Returns the first matching code found (most recent first).
        """
        query = self._build_query(from_me=from_me, within_seconds=within_seconds)
        logger.debug(f"[>] Gmail OTP query: {query}")

        # Use the google_tools wrapper (handles auth and errors)
        resp = await gmail_search_emails(query=query, max_results=max_results)
        if not resp.get("ok"):
            logger.warning(f"[!] gmail_search_emails failed: {resp.get('error')}")
            return None

        messages: List[Dict[str, Any]] = resp.get("messages", []) or []
        if not messages:
            return None

        # Examine recent messages in order; prefer Subject-based code
        for m in messages:
            headers = (m.get("headers") or {})
            subject = (headers.get("Subject") or "").strip()
            snippet = (m.get("snippet") or "").strip()

            # 1) Try subject first (most reliable signal)
            code = self._extract_code_from_subject(subject)
            if code:
                logger.debug(f"[✓] OTP from Subject: {subject!r} -> {code}")
                return code

            # 2) Fall back to snippet (optional)
            if include_body_lookup:
                body_code = self._extract_code_from_text(snippet)
                if body_code:
                    logger.debug(f"[✓] OTP from snippet for msg {m.get('id')}: {body_code}")
                    return body_code

        logger.debug("[!] No OTP found in recent messages.")
        return None

    # ---------- Helpers ----------

    def _build_query(self, *, from_me: bool, within_seconds: int) -> str:
        """
        Build a Gmail search query limiting to recent messages,
        optionally those 'from:me', and nudging for OTP-like subjects.
        """
        since_ts = int(time.time()) - max(0, int(within_seconds))
        # Common patterns for OTP/verification emails—help Google rank the right messages first.
        # We keep it broad: subject filters improve relevance but aren't required.
        subject_hint = '(subject:code OR subject:verification OR subject:passcode OR subject:OTP)'
        base = f"after:{since_ts} label:inbox {subject_hint}"
        return (f"from:me {base}" if from_me else base).strip()

    def _extract_code_from_subject(self, subject: str) -> Optional[str]:
        """
        Extract a code when 'code' (or typical OTP words) appears in the subject.
        Prioritize subjects that contain those words; else return None.
        """
        s = subject.lower()
        if not any(w in s for w in ("code", "otp", "passcode", "verification")):
            return None
        return self._extract_code_from_text(subject)

    def _extract_code_from_text(self, text: str) -> Optional[str]:
        """
        Extract a 4–8 digit code from text. Adjust as needed.
        """
        match = re.search(r"\b(\d{4,8})\b", text or "")
        return match.group(1) if match else None


# Example usage (manual test)
async def _example():
    client = GmailOTPClientAsync()
    code = await client.get_otp_code(from_me=True, within_seconds=300, retry_interval=10, max_retries=6)
    logger.info(f"Latest OTP: {code}")

if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_example())
