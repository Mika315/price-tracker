"""
Normalize pasted hotel booking URLs.

Runs for every save request (desktop and mobile). RTL / WhatsApp / email pastes
often inject invisible Unicode; clean URLs from the address bar are unchanged.
"""


def sanitize_url(url: str) -> str:
    if not url:
        return url
    s = str(url).strip()
    for ch in (
        "\ufeff",
        "\u200b",
        "\u200c",
        "\u200d",
        "\u2060",
    ):
        s = s.replace(ch, "")
    for cp in range(0x202A, 0x2030):
        s = s.replace(chr(cp), "")
    for cp in range(0x2066, 0x206A):
        s = s.replace(chr(cp), "")
    s = s.strip().strip("<>").strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    return s
