from curator_agent.never_collect import NeverCollect, add_domain
from curator_agent.sources import prompt_text


def rules(tmp_path, text):
    p = tmp_path / "nc.yaml"
    p.write_text(text)
    return NeverCollect(p), p


def test_domains_match_subdomains_not_lookalikes(tmp_path):
    nc, _ = rules(tmp_path, "categories:\n  banks: [commbank.com.au]\n  government: [gov.au]\nurl_patterns: ['/login']\n")
    assert nc.blocks("https://www.my.commbank.com.au/netbank")
    assert nc.blocks("https://www.ato.gov.au/")
    assert not nc.blocks("https://notcommbank.com.au/")
    assert nc.blocks("https://example.com/login?next=/")
    assert nc.blocks("file:///Users/someone/secret.txt")
    assert not nc.blocks("https://www.abc.net.au/news")


def test_forget_appends_and_keeps_comments(tmp_path):
    nc, p = rules(tmp_path, "# top\ncategories: {}\ndomains: []        # forget appends here\n")
    add_domain("example.com", p)
    add_domain("example.com", p)
    assert "# top" in p.read_text() and "# forget appends here" in p.read_text()
    assert NeverCollect(p).blocks("https://a.example.com/")


def test_prompt_text_keeps_only_your_words():
    assert prompt_text({"content": "fix the flaky test"}) == "fix the flaky test"
    assert prompt_text({"content": "<command-name>/model</command-name>"}) is None
    assert prompt_text({"content": [{"type": "tool_result", "content": "x"}]}) is None
    assert prompt_text({"content": "key sk-ant-abcdefghijklmnopqrstu here"}) == "key [redacted] here"
