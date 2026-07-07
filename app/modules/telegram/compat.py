def ensure_urllib3_header_param_compat() -> None:
    """
    pyTelegramBotAPI imports urllib3.fields.format_header_param at import time.
    Some urllib3-future builds only expose newer formatter names.
    RFC 2231 formatting is kept as the last fallback because it encodes
    non-ASCII values differently from urllib3's old default.
    """
    try:
        from urllib3 import fields
    except ImportError:
        return

    if hasattr(fields, "format_header_param"):
        return

    for fallback_name in (
        "format_header_param_html5",
        "format_multipart_header_param",
        "format_header_param_rfc2231",
    ):
        fallback = getattr(fields, fallback_name, None)
        if fallback is not None:
            fields.format_header_param = fallback
            return
