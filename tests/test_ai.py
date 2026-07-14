"""Tests for the request shape of app/ai.py's API backend, using a fake
Anthropic client. The load-bearing assertion: email classification runs on
Haiku *without* an effort parameter (Haiku 4.5 rejects effort with a 400),
while the JD analysis keeps effort. No network or API key involved."""

import types

from app import ai


class _Recorder:
    """Stands in for client.messages; records the create() kwargs."""

    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return types.SimpleNamespace(
            stop_reason="end_turn",
            usage=types.SimpleNamespace(input_tokens=42, output_tokens=7),
            content=[types.SimpleNamespace(
                type="text",
                text=(
                    '{"classification":"rejection","confidence":90,'
                    '"company":"Acme","role":"Engineer","dates":"",'
                    '"notable_detail":"","reasoning":"a clear no"}'
                ),
            )],
        )


def _install_fake(monkeypatch):
    rec = _Recorder()

    class FakeClient:
        def __init__(self, api_key=None):
            self.messages = rec

    monkeypatch.setattr(ai.anthropic, "Anthropic", FakeClient)
    monkeypatch.setattr(ai.settings_store, "get_api_key", lambda: "sk-ant-test")
    return rec


def test_email_classification_uses_haiku_without_effort(monkeypatch):
    rec = _install_fake(monkeypatch)
    out = ai.classify_email({
        "sender_name": "R", "sender_email": "r@acme.com",
        "subject": "Update", "body": "we went with other candidates",
    })
    assert out["ok"] is True
    assert out["data"]["classification"] == "rejection"
    assert rec.kwargs["model"] == "claude-haiku-4-5"
    # The regression this guards: no effort key on the Haiku call.
    assert "effort" not in rec.kwargs["output_config"]
    assert rec.kwargs["output_config"]["format"]["type"] == "json_schema"


def test_jd_analysis_keeps_effort(monkeypatch):
    rec = _install_fake(monkeypatch)
    ai.analyze("Some job description", "Some resume")
    # analyze() runs on Opus with effort — that must still be sent.
    assert rec.kwargs["model"] == "claude-opus-4-8"
    assert rec.kwargs["output_config"]["effort"] == "medium"
