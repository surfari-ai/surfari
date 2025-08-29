import re
import random
from typing import Set, Optional, Dict

import surfari.util.surfari_logger as surfari_logger
logger = surfari_logger.getLogger(__name__)

class NumericMasker:
    """
    Enhanced version that:
    - Never masks configurable numbers/terms (e.g., 1099, 2024, W2)
    - Maintains all existing date/time detection logic
    - Makes exclusion list and min length configurable
    - Optimizes unmasking by building the reverse map only once.
    """
    # Splits on whitespace only
    TOKEN_PATTERN = re.compile(r"\S+")

    # Potential month names
    MONTH_NAMES = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Sept", "Oct", "Nov", "Dec",
        "January", "February", "March", "April", "June",
        "July", "August", "September", "October",
        "November", "December"
    )
    MONTH_PATTERN = "|".join(MONTH_NAMES)

    # Time pattern: HH:MM(:SS)? plus optional AM/PM
    TIME_PATTERN = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?(?:\s?[APMapm]{2})?$")

    # Default donot mask terms (case-insensitive)
    DEFAULT_DONOT_MASK_TERMS = {"1099", "2024", "2025", "2026", "401k"}

    def __init__(self, donot_mask_items: Optional[Set[str]] = None, min_token_length: int = 5):
        """
        Initialize with optional set of donot mask terms and min token length.
        """
        self.donot_mask_terms = {
            re.sub(r"[^\w]", "", term).lower() for term in (donot_mask_items if donot_mask_items is not None else self.DEFAULT_DONOT_MASK_TERMS)
        }
        self.replacement_map: Dict[str, str] = {}  # old_token -> masked_token
        self.reverse_map: Dict[str, str] = {}      # masked_token -> old_token
        self.normalized_reverse_map: Dict[str, str] = {}  # normalized_masked_token -> normalized_old_token
        self.used_masked: Set[str] = set()         # set of masked tokens to avoid collisions
        self.min_token_length: int = min_token_length

    def _clear(self):
        self.replacement_map.clear()
        self.used_masked.clear()
        self.reverse_map.clear()
        self.normalized_reverse_map.clear()
        
    def _is_donot_mask_term(self, token: str) -> bool:
        """
        Check if token matches any donot mask term (case-insensitive),
        after normalizing the token (removing punctuation/special characters).
        """
        normalized = re.sub(r"[^\w]", "", token).lower()
        return normalized in self.donot_mask_terms

    def add_donot_mask_terms_from_string(self, in_string: str):
        """
        Tokenize the in_string and add digit-containing tokens meeting min length
        to donot_mask_terms (after normalization and lowercasing).
        """
        logger.debug(f"Adding donot mask terms from string: {in_string}")
        tokens = self.TOKEN_PATTERN.findall(in_string)
        for token in tokens:
            if self._has_digit(token) and len(token.strip()) >= self.min_token_length:
                normalized = re.sub(r"[^\w]", "", token).lower()
                if normalized:
                    self.donot_mask_terms.add(normalized)

    def _is_dateish(self, token: str) -> bool:
        t = token.strip()
        t2 = t[1:].lstrip() if t.startswith('-') else t

        if self.TIME_PATTERN.match(t2):
            return True
        if re.search(r"[/-]", t2) and re.search(r"\d", t2):
            return True
        if re.compile(rf"(?i)\b(?:{self.MONTH_PATTERN})\b").search(t2) and re.search(r"\d", t2):
            return True
        return False

    def _has_digit(self, token: str) -> bool:
        return any(ch.isdigit() for ch in token)

    def _mask_digit_char(self, c: str) -> str:
        """
        '0' -> random [0-9], '1'..'9' -> random [1-9]
        else unchanged
        """
        if c.isdigit():
            if c == '0':
                return random.choice("0123456789")
            else:
                return random.choice("123456789")
        return c

    def _mask_token(self, token: str) -> str:
        """If we've masked this token before, reuse it. Else create new masked token."""
        if token in self.replacement_map:
            return self.replacement_map[token]

        attempt_count = 0
        while True:
            attempt_count += 1
            if attempt_count > 100:
                fallback = f"MASK{random.randint(100000,999999)}"
                logger.warning(f"Exceeded attempts for token={token}, using {fallback}")
                self.replacement_map[token] = fallback
                self.used_masked.add(fallback)
                return fallback

            candidate = "".join(self._mask_digit_char(c) for c in token)
            if candidate not in self.used_masked:
                self.replacement_map[token] = candidate
                self.used_masked.add(candidate)
                return candidate
    
    def _strip_brackets(self, text: str) -> str:
        if text.startswith(("[]", "()", "[X]", "(X)")):
            return text
        
        while text and text[0] in "{[(":
            text = text[1:]
        while text and text[-1] in "}])":
            text = text[:-1]
        return text
    
    def _normalize_number(self, num_str: str) -> str:
        """
        Normalize a numeric string for comparison, preserving + or - if present.
        Removes commas and dollar signs, then float-casts and formats for comparison.
        """
        has_dollar = "$" in num_str
        prefix = ""

        num_str = re.sub(r"[{}\[\]\(\):;,$']", "", num_str).strip().rstrip(".")
        tmp = num_str
        if tmp.startswith(("+", "-")):
            prefix = tmp[0]
            tmp = tmp[1:].lstrip()

        try:
            val = float(tmp)
        except ValueError:
            return num_str  # Not a valid number, return semi-normalized

        # Format numbers as float with two decimal places or int
        formatted = f"{val:.2f}" if "." in tmp else f"{int(val)}"
        # add back $ and sign
        return f"{prefix}${formatted}" if has_dollar else f"{prefix}{formatted}"

    def _build_reverse_map(self):
        """Builds the reverse map from the replacement map for efficient lookup."""
        logger.debug("Building reverse map for sensitive info unmasking")
        self.reverse_map = {}
        for original, masked in self.replacement_map.items():
            self.reverse_map[masked] = original
            norm_masked = self._normalize_number(masked)
            norm_original = self._normalize_number(original)
            self.normalized_reverse_map[norm_masked] = norm_original
            logger.sensitive(f"added reverse mapping {masked} -> {original} and normalized reverse mapping: {norm_masked} -> {norm_original}")
                
    def mask_sensitive_info(self, text: str, donot_mask=[]) -> str:
        """
        Enhanced version that:
        1. Skips donot mask terms
        2. Skips dateish tokens
        3. Masks other digit-containing tokens
        """     
        def replacer(m):
            token = m.group(0)
            norm_token = self._normalize_number(token)
            if len(norm_token) < self.min_token_length:
                return token
            if not self._has_digit(token):
                return token
            if self._is_donot_mask_term(token):
                return token
            if self._is_dateish(token):
                return token
            for item in donot_mask:
                if token in item:
                    return token
            return self._mask_token(token)

        logger.info(f"Masking sensitive info called for text of {len(text)} characters")
        self._clear()  # Clear previous state
        masked_text = self.TOKEN_PATTERN.sub(replacer, text)
        self._build_reverse_map()  # Build reverse map after masking is complete
        logger.debug(f"Reverse map size: {len(self.reverse_map)}")
        return masked_text

    def unmask_sensitive_info(self, masked_text: str):
        """
        Revert masked tokens if they appear in self.reverse_map.
        """
        if not masked_text:
            return ""
        
        logger.debug(f"unmasking sensitive info from: {masked_text}")
        
        def revert_func(m):
            token = m.group(0)
            if not self._has_digit(token) or self._is_dateish(token):
                return token
            original = self.reverse_map.get(token)
            if original:
                logger.sensitive(f"Revert token to original directly '{token}' -> '{original}'")
                return original
            norm_token = self._normalize_number(token)
            norm_original = self.normalized_reverse_map.get(norm_token)
            if norm_original:
                logger.sensitive(f"Revert token after normalizing '{token}' to {norm_token} -> '{norm_original}'")
                return norm_original
            logger.debug(f"Revert token FAILED to find original for '{token}'")
            return token

        unmasked_text = self.TOKEN_PATTERN.sub(revert_func, masked_text)
        logger.sensitive(f"unmasked text: {unmasked_text}")
        if not re.fullmatch(r"^(\[\[.*\]\]|\{\{.*\}\}|\[.*\]|\{.*\})\d*$", masked_text):
            # masked_text itself is not a proper target
            return re.sub(r"[{}\[\]\(\)]", "", unmasked_text)
        return unmasked_text
