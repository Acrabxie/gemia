from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_chat_scroll_follow_respects_creator_position_and_offers_jump_button() -> None:
    html = (ROOT / "static/v3/index.html").read_text(encoding="utf-8")
    css = (ROOT / "static/v3/v3.css").read_text(encoding="utf-8")
    source = (ROOT / "static/v3/v3.js").read_text(encoding="utf-8")

    assert 'id="chat-scroll-bottom"' in html
    assert 'aria-label="滚动到底部"' in html
    assert 'href="#i-chevron-d"' in html
    assert ".rail-history-shell { position: relative;" in css
    assert ".chat-scroll-bottom {" in css
    assert ".chat-scroll-bottom[hidden] { display: none; }" in css

    assert "_justSubmitted" not in source
    assert "_followChatBottom: true" in source
    assert 'els.railHistory?.addEventListener("scroll"' in source
    assert "state._followChatBottom = chatIsNearBottom();" in source
    assert 'els.chatScrollBottom?.addEventListener("click"' in source
    assert "scrollChatToBottom();" in source
    assert "if (state._followChatBottom)" in source
