"""Privacy filter — redact sensitive information from visual context."""

import re
import logging

logger = logging.getLogger("ai_company.vision.privacy")

SENSITIVE_PATTERNS = [
    # API keys & tokens
    (r'sk-[a-zA-Z0-9]{20,}', '[REDACTED_API_KEY]'),
    (r'ghp_[a-zA-Z0-9]{20,}', '[REDACTED_GITHUB_TOKEN]'),
    (r'AIza[0-9A-Za-z\-_]{35}', '[REDACTED_GOOGLE_KEY]'),
    # Credit card numbers
    (r'\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b', '[REDACTED_CC]'),
    # Email addresses
    (r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '[REDACTED_EMAIL]'),
    # Common password/file patterns
    (r'(?:password|secret|token)[:=]\s*["\']?\S+["\']?', '[REDACTED_SECRET]'),
    # IP addresses (optional, uncomment if needed)
    # (r'\b(?:\d{1,3}\.){3}\d{1,3}\b', '[REDACTED_IP]'),
]


class PrivacyFilter:
    """Redact sensitive information from text before it enters context."""

    def __init__(self, patterns=None):
        self.patterns = patterns or SENSITIVE_PATTERNS

    def redact(self, text: str) -> str:
        """Apply all patterns and return sanitized text."""
        if not text:
            return text
        for pattern, replacement in self.patterns:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        return text

    def redact_dict(self, data: dict) -> dict:
        """Recursively redact all string values in a dict."""
        if not data:
            return data
        result = {}
        for key, value in data.items():
            if isinstance(value, str):
                result[key] = self.redact(value)
            elif isinstance(value, dict):
                result[key] = self.redact_dict(value)
            elif isinstance(value, list):
                result[key] = [
                    self.redact_dict(item) if isinstance(item, dict)
                    else self.redact(item) if isinstance(item, str)
                    else item
                    for item in value
                ]
            else:
                result[key] = value
        return result


# Re-export for convenience
__all__ = ["PrivacyFilter", "SENSITIVE_PATTERNS"]
