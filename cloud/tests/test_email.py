"""The outgoing-email sender: gating, message building, and a stubbed send.

The SMTP connection is the only impure step; it goes through email._connect,
which these tests replace with a fake transport, so nothing touches a real
mail server."""
from app import email as mailer
from app.config import settings


def test_email_dark_by_default():
    # With no host or from address set, the whole feature is off.
    assert mailer.email_configured() is False


def test_configured_needs_host_and_from(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.resend.com")
    monkeypatch.setattr(settings, "smtp_from", "")
    assert mailer.email_configured() is False
    monkeypatch.setattr(settings, "smtp_from", "noreply@forager.test")
    assert mailer.email_configured() is True
    monkeypatch.setattr(settings, "smtp_host", "")
    assert mailer.email_configured() is False


def test_base_url_strips_trailing_slash(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url",
                        "https://forager.pantryraider.app/")
    assert mailer.base_url() == "https://forager.pantryraider.app"


def test_build_message_plain(monkeypatch):
    monkeypatch.setattr(settings, "smtp_from", "noreply@forager.test")
    msg = mailer.build_message("dan@example.com", "Hello", "Just text.")
    assert msg["From"] == "noreply@forager.test"
    assert msg["To"] == "dan@example.com"
    assert msg["Subject"] == "Hello"
    assert not msg.is_multipart()
    assert msg.get_content().strip() == "Just text."


def test_build_message_multipart_alternative(monkeypatch):
    monkeypatch.setattr(settings, "smtp_from", "noreply@forager.test")
    msg = mailer.build_message("dan@example.com", "Hi", "text", "<p>html</p>")
    assert msg.is_multipart()
    subtypes = {p.get_content_subtype() for p in msg.iter_parts()}
    assert subtypes == {"plain", "html"}


class _FakeSMTP:
    def __init__(self, record):
        self.record = record

    def login(self, user, password):
        self.record.append(("login", user, password))

    def send_message(self, msg):
        self.record.append(("send", msg))

    def quit(self):
        self.record.append(("quit",))


def _configure(monkeypatch, user="", password=""):
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "noreply@forager.test")
    monkeypatch.setattr(settings, "smtp_user", user)
    monkeypatch.setattr(settings, "smtp_password", password)


def test_send_email_false_when_unconfigured():
    assert mailer.send_email("dan@example.com", "s", "b") is False


def test_send_email_delivers_and_logs_in(monkeypatch):
    _configure(monkeypatch, user="resend", password="re_key")
    record = []
    monkeypatch.setattr(mailer, "_connect", lambda: _FakeSMTP(record))
    ok = mailer.send_email("dan@example.com", "Subject", "Body")
    assert ok is True
    kinds = [r[0] for r in record]
    assert kinds == ["login", "send", "quit"]
    assert record[0] == ("login", "resend", "re_key")


def test_send_email_skips_login_without_credentials(monkeypatch):
    _configure(monkeypatch)  # no user/password
    record = []
    monkeypatch.setattr(mailer, "_connect", lambda: _FakeSMTP(record))
    assert mailer.send_email("dan@example.com", "s", "b") is True
    assert [r[0] for r in record] == ["send", "quit"]


def test_send_email_swallows_transport_errors(monkeypatch):
    _configure(monkeypatch)

    def boom():
        raise OSError("connection refused")

    monkeypatch.setattr(mailer, "_connect", boom)
    # A dead mail server must never raise into the caller.
    assert mailer.send_email("dan@example.com", "s", "b") is False


def test_redact_address_keeps_three_chars_and_the_domain():
    assert mailer.redact_address("dan.marafino@example.com") == "dan***@example.com"
    assert mailer.redact_address("ab@example.com") == "ab***@example.com"
    assert mailer.redact_address("not-an-address") == "not***"
    assert mailer.redact_address("") == "***"


def test_send_failure_log_redacts_the_recipient(monkeypatch, caplog):
    _configure(monkeypatch)

    def boom():
        raise OSError("connection refused")

    monkeypatch.setattr(mailer, "_connect", boom)
    with caplog.at_level("WARNING", logger="forager.email"):
        assert mailer.send_email("dan.marafino@example.com", "s", "b") is False
    text = caplog.text
    assert "dan***@example.com" in text
    assert "dan.marafino@example.com" not in text
