"""Applicant mailbox: parsing, code/link extraction, attachment guard, MCP wiring."""
from email.message import EmailMessage

import pytest

import applypilot.apply.email_mcp as email_mcp
import applypilot.apply.launcher as launcher
import applypilot.config as config
from applypilot.apply.mailbox import MailboxConfig, extract_codes, extract_links, parse_message


@pytest.mark.parametrize("text, expected", [
    ("Your verification code is 482913.", ["482913"]),
    ("Use code: AB12CD to sign in", ["AB12CD"]),
    ("739201 is your Workday code", ["739201"]),
    ("Enter the code below to continue", []),
    ("Order 2026 shipped", []),
])
def test_extract_codes(text, expected):
    assert extract_codes(text) == expected


def test_verify_links_ranked_first_and_unsubscribe_dropped():
    links = extract_links(
        "See https://acme.example/jobs and https://acme.example/unsubscribe",
        ["https://acme.example/account/verify?t=1"],
    )
    assert links[0] == "https://acme.example/account/verify?t=1"
    assert all("unsubscribe" not in u for u in links)


def test_parse_html_only_message():
    msg = EmailMessage()
    msg["From"] = "Acme <no-reply@acme.example>"
    msg["Subject"] = "=?utf-8?q?Confirm_your_email?="
    msg["Date"] = "Thu, 08 Oct 2026 20:00:00 -0000"
    msg.set_content('<p>Your code is <b>731904</b>. <a href="https://acme.example/confirm?x=9">Confirm</a></p>',
                    subtype="html")
    parsed = parse_message("INBOX:7", msg.as_bytes())
    assert parsed.subject == "Confirm your email"
    assert parsed.codes == ["731904"]
    assert parsed.links == ["https://acme.example/confirm?x=9"]


def test_config_requires_address_password_and_host(monkeypatch):
    assert MailboxConfig.from_env() is None
    monkeypatch.setenv("APPLY_EMAIL", "me@example.com")
    monkeypatch.setenv("APPLY_EMAIL_PASSWORD", "pw")
    monkeypatch.setenv("APPLY_EMAIL_IMAP_HOST", "mail.example.com")
    cfg = MailboxConfig.from_env()
    assert cfg.smtp_host == "mail.example.com" and cfg.smtp_port == 465


def test_attachments_limited_to_generated_pdfs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", tmp_path / "apply-workers")
    monkeypatch.setattr(config, "TAILORED_DIR", tmp_path / "tailored")
    monkeypatch.setattr(config, "COVER_LETTER_DIR", tmp_path / "covers")
    resume = tmp_path / "apply-workers" / "worker-0" / "current" / "Jane_Resume.pdf"
    resume.parent.mkdir(parents=True)
    resume.write_bytes(b"%PDF")
    secret = tmp_path / ".env"
    secret.write_text("APPLY_EMAIL_PASSWORD=x")
    outside_pdf = tmp_path / "other.pdf"
    outside_pdf.write_bytes(b"%PDF")

    assert email_mcp._allowed_attachment(str(resume)) == resume.resolve()
    for bad in (secret, outside_pdf, resume.parent / ".." / ".." / ".." / ".env"):
        with pytest.raises(email_mcp.ToolError):
            email_mcp._allowed_attachment(str(bad))


def test_mcp_config_uses_mailbox_server_when_configured(monkeypatch):
    assert "gmail" in launcher._make_mcp_config(9222)["mcpServers"]
    monkeypatch.setenv("APPLY_EMAIL", "me@example.com")
    monkeypatch.setenv("APPLY_EMAIL_PASSWORD", "pw")
    monkeypatch.setenv("APPLY_EMAIL_IMAP_HOST", "mail.example.com")
    servers = launcher._make_mcp_config(9222)["mcpServers"]
    assert "gmail" not in servers
    assert servers["email"]["args"] == ["-m", "applypilot.apply.email_mcp"]


def test_dry_run_blocks_both_send_tools():
    disallowed = launcher._build_claude_cmd("sonnet", "m.json", dry_run=True)
    tools = disallowed[disallowed.index("--disallowedTools") + 1]
    assert "mcp__email__send_email" in tools and "mcp__gmail__send_email" in tools
