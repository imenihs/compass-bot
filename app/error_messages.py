from __future__ import annotations

import asyncio


TRANSIENT_ERROR_MARKERS = (
    "408",
    "429",
    "500",
    "502",
    "503",
    "504",
    "deadline",
    "temporarily",
    "temporary",
    "timeout",
    "timed out",
    "unavailable",
    "resource_exhausted",
    "too many requests",
    "rate limit",
)


def is_likely_transient_error(error: Exception) -> bool:
    """Return True when retrying later is a reasonable user instruction."""
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return True
    text = f"{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in TRANSIENT_ERROR_MARKERS)


def ai_failure_message(error: Exception, action: str = "返事") -> str:
    if is_likely_transient_error(error):
        return (
            f"ごめん、今AI側が混み合っているか応答が遅いみたいで、{action}できなかったよ。"
            "少し時間をおいてもう一度送ってね。何度も続くときは管理者に連絡してね。"
        )
    return (
        f"ごめん、AIの設定確認が必要かもしれなくて、{action}できなかったよ。"
        "待っても直らない可能性があるから、管理者に連絡してね。"
    )


def processing_failure_message() -> str:
    return (
        "ごめん、処理を続けられなかったよ。"
        "待っても直らない設定やデータの問題かもしれないから、管理者に連絡してね。"
    )


def operation_failure_message(operation: str = "操作") -> str:
    return (
        f"{operation}に失敗したよ。"
        "設定や権限の確認が必要かもしれないから、管理者に連絡してね。"
    )


def web_failure_message() -> str:
    return (
        "処理中にエラーが起きました。"
        "待っても直らない設定やデータの問題かもしれないため、管理者に連絡してください。"
    )
