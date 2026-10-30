from virtual_self.services.email_body_clean import (
    gmail_labels_include_sent,
    is_auto_generated_mail,
    strip_email_body,
)


def test_strip_reply_chain_and_signature():
    raw = """Thanks for the update — I'll send the doc tonight.

Best,
Casey Example

On Mon, Jan 1, 2024 at 9:00 AM Friend <f@example.com> wrote:
> Can you send the doc?
> I need it asap.

--
Sent from my iPhone
"""
    out = strip_email_body(raw)
    assert "send the doc tonight" in out
    assert "Can you send" not in out
    assert "Sent from my iPhone" not in out
    assert not out.startswith(">")


def test_gmail_sent_label():
    assert gmail_labels_include_sent("Inbox,Sent,Opened")
    assert not gmail_labels_include_sent("Inbox,Category promotions")


def test_strip_gmail_on_wrote_inline():
    raw = "Here are my documents.\n\nOn Mon, 27 Jul, 2026, 10:23\u202fam Taylor Example, <\ntaylor@example.com> wrote:\n\n> old reply"
    out = strip_email_body(raw)
    assert out == "Here are my documents."
    assert "Taylor" not in out


def test_auto_generated_from_noreply():
    assert is_auto_generated_mail(
        from_header="Alerts <no-reply@example.com>",
        subject="Security alert",
        headers={"Precedence": "bulk"},
    )
