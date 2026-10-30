from virtual_self.cli.remember import parse_remember_request


def test_parse_quoted_remember():
    assert parse_remember_request('remember that "there is a hail in nebraska"') == (
        "there is a hail in nebraska"
    )


def test_parse_slash_remember():
    assert parse_remember_request("/remember Nebraska has hail") == "Nebraska has hail"


def test_parse_no_remember():
    assert parse_remember_request("complete this line") is None
