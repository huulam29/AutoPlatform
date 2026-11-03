"""Off localhost the API always needs a bearer token (decision 82)."""

from __future__ import annotations

import pytest

from agentline.api.server import TOKEN_ENV, ensure_api_token, is_local


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.5"])
def test_local_hosts(host: str) -> None:
    assert is_local(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.4", "::", "myhost.lan"])
def test_other_hosts(host: str) -> None:
    assert not is_local(host)


def test_token_is_created_only_off_localhost_and_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentline.secrets as secrets_mod

    saved: dict[str, str] = {}

    def fake_set(name: str, value: str) -> None:
        saved[name] = value
        monkeypatch.setenv(name, value)

    monkeypatch.setattr(secrets_mod, "set_secret", fake_set)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    assert not ensure_api_token("127.0.0.1") and not saved
    assert ensure_api_token("0.0.0.0")
    assert len(saved[TOKEN_ENV]) >= 40
    assert not ensure_api_token("0.0.0.0")  # one is set now
