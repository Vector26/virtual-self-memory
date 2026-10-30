from virtual_self.cli.main import _chat_may_open, _pid_alive


def test_chat_opens_when_enrichment_complete() -> None:
    assert _chat_may_open({"enrichment_coverage": 1.0, "ready": False, "fully_indexed": False})
    assert _chat_may_open({"enrichment_coverage": 0.95})
    assert not _chat_may_open({"enrichment_coverage": 0.5})


def test_pid_alive_does_not_require_kill_binary() -> None:
    assert _pid_alive("1") is True
    assert _pid_alive("99999999") is False
    assert _pid_alive("not-a-pid") is False
