"""Shared best-effort secret/control-character redaction."""
import re


def redact(text: str) -> str:
    # Whole sensitive lines, including multiline credential blocks, are suppressed.
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|\Z)',
                  '[REDACTED PRIVATE KEY]', text)
    text = re.sub(r'(?im)^.*(?:password|passwd|secret|token|credential|api[_-]?key|authorization|cookie|비밀번호)\s*["\x27]?\s*[:=].*$',
                  '[REDACTED SENSITIVE LINE]', text)
    text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [REDACTED]', text)
    text = re.sub(r'\b(?:GOCSPX-|sk-|ghp_|github_pat_)[A-Za-z0-9_-]+', '[REDACTED TOKEN]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED JWT]', text)
    text = re.sub(r'(?i)(https?://)[^\s/@:]+:[^\s/@]+@', r'\1[REDACTED]@', text)
    text = re.sub(r'(?i)([?&](?:token|key|secret|password|code)=)[^\s&#]+', r'\1[REDACTED]', text)
    return ''.join(c for c in text if c in '\n\t' or ord(c) >= 32 and ord(c) != 127)

