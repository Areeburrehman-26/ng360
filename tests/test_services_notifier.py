import services.notifier as notifier
from tests.conftest import run_async


def test_notify_quote_success_builds_payload(monkeypatch):
    captured = {}

    async def fake_post(url, payload):
        captured["url"] = url
        captured["payload"] = payload

    monkeypatch.setattr(notifier, "_post_to_slack", fake_post)
    monkeypatch.setattr(notifier, "_now_formatted", lambda: "TIME")
    monkeypatch.setattr(notifier, "SLACK_WEBHOOK_QUOTES", "https://example.com")

    run_async(
        notifier.notify_quote_success(
            "John", "Doe", "GA", "$100", "$60", "$40", "http://drive", "c1"
        )
    )
    assert captured["url"] == "https://example.com"
    assert "NG360 Quote Success" in str(captured["payload"])


def test_notify_quote_failure_builds_payload(monkeypatch):
    captured = {}

    async def fake_post(url, payload):
        captured["url"] = url
        captured["payload"] = payload

    monkeypatch.setattr(notifier, "_post_to_slack", fake_post)
    monkeypatch.setattr(notifier, "_now_formatted", lambda: "TIME")
    monkeypatch.setattr(notifier, "SLACK_WEBHOOK_ALERTS", "https://example.com")

    run_async(notifier.notify_quote_failure("John", "Doe", "GA", "c1", "boom"))
    assert captured["url"] == "https://example.com"
    assert "NG360 Quote FAILED" in str(captured["payload"])


def test_notify_bot_health_critical(monkeypatch):
    captured = {}

    async def fake_post(url, payload):
        captured["url"] = url
        captured["payload"] = payload

    monkeypatch.setattr(notifier, "_post_to_slack", fake_post)
    monkeypatch.setattr(notifier, "_now_formatted", lambda: "TIME")
    monkeypatch.setattr(notifier, "SLACK_WEBHOOK_ALERTS", "https://example.com")

    run_async(notifier.notify_bot_health("msg", is_critical=True))
    assert captured["url"] == "https://example.com"
    assert "rotating_light" in str(captured["payload"])
