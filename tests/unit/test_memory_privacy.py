from openhachimi_agent.memory.privacy import PrivacyGuard
from openhachimi_agent.memory.models import MemorySensitivity


def test_privacy_guard_rejects_secret():
    guard = PrivacyGuard()
    decision = guard.should_store("api_key=sk-abcdefghijklmnopqrstuvwxyz")

    assert decision.action == "reject"
    assert decision.sensitivity == MemorySensitivity.SECRET


def test_privacy_guard_redacts_pii():
    guard = PrivacyGuard()
    decision = guard.should_store("我的邮箱是 test@example.com")

    assert decision.action == "redact"
    assert "[REDACTED_PII]" in decision.text
