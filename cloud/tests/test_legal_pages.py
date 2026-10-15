"""The legal pages (privacy policy, terms of service) and the links to them."""

FOOTER_PAGES = ("/", "/pricing", "/features", "/status", "/signup", "/login")


def test_privacy_page_renders(client):
    resp = client.get("/privacy")
    assert resp.status_code == 200
    text = resp.text
    assert "Privacy policy" in text
    assert "Effective October 14, 2026" in text
    # Who runs it and how to reach them.
    assert "Dan Marafino" in text and "Syracuse, New York" in text
    assert "mailto:support@pantryraider.app" in text
    # Where the data lives and who else handles it.
    assert "United States" in text
    for provider in ("Stripe", "Gemini", "Google (Sign-In)", "Resend",
                     "Cloudflare", "hosting provider"):
        assert provider in text, provider
    # Shared recipe photos are served from Forager itself.
    assert "forager.pantryraider.app" in text
    # Rights and how to use them.
    for right in ("See", "Correct", "Delete", "Export"):
        assert f"<strong>{right}</strong>" in text, right


def test_privacy_page_renders_signed_in(client):
    client.post("/signup", data={"email": "dan@example.com",
                                 "password": "hunter2222",
                                 "confirm_password": "hunter2222"})
    resp = client.get("/privacy")
    assert resp.status_code == 200
    assert "My account" in resp.text


def test_footer_links_to_the_privacy_page(client):
    for path in FOOTER_PAGES:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert 'href="/privacy"' in resp.text, path


def test_terms_page_renders(client):
    resp = client.get("/terms")
    assert resp.status_code == 200
    text = " ".join(resp.text.split())
    assert "Terms of Service" in text
    assert "Effective October 15, 2026" in text
    assert "Dan Marafino" in text and "State of New York" in text
    # The refund and cancellation section, reachable by its anchor.
    assert 'id="refunds"' in text and "Refunds and cancellation" in text
    assert "14 days of your first charge" in text
    assert "end of the period you already paid for" in text
    # Beta, takedowns, and the links to the account and privacy pages.
    assert "beta" in text
    assert "Copyright notices" in text
    assert 'href="/account"' in text and 'href="/privacy"' in text


def test_footer_links_to_the_terms_page(client):
    for path in FOOTER_PAGES:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert 'href="/terms"' in resp.text, path


def test_signup_shows_the_agreement_line(client):
    text = client.get("/signup").text
    assert "By creating an account you agree to the" in text
    # The line sits under the signup button, inside the form.
    form = text[text.index('action="/signup"'):text.index("</form>")]
    button = form.index('type="submit"')
    agree = form.index("By creating an account")
    assert agree > button
    assert 'href="/terms"' in form[agree:] and 'href="/privacy"' in form[agree:]


def test_pricing_page_points_at_the_refund_policy(client):
    text = client.get("/pricing").text
    assert 'href="/terms#refunds"' in text


def test_checkout_buttons_carry_the_terms_note(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "stripe_checkout_url_premium_month",
                        "https://buy.stripe.com/test_premium")
    client.post("/signup", data={"email": "dan@example.com",
                                 "password": "hunter2222",
                                 "confirm_password": "hunter2222"})
    text = client.get("/account").text
    # A browser signup without the app's install key has no trial, so the
    # account page offers the checkout buttons, and the note sits with them.
    assert "buy.stripe.com/test_premium" in text
    assert "By subscribing you agree to the" in text
    assert 'href="/terms#refunds"' in text
