"""to_telegram_html: whitelisted tags pass balanced; everything else renders
literally — Telegram 400-rejects the whole message on any invalid markup."""

from app.telegram.format import to_telegram_html


def test_plain_text_is_escaped():
    assert to_telegram_html("2 < 3 & \"so\"") == "2 &lt; 3 &amp; &quot;so&quot;"


def test_allowed_tags_pass_through():
    text = '<b>Dune</b>: <i>8.0/10</i> — <a href="https://imdb.com/x">IMDb</a>'
    assert to_telegram_html(text) == (
        '<b>Dune</b>: <i>8.0/10</i> — <a href="https://imdb.com/x">IMDb</a>'
    )


def test_disallowed_tags_render_literally():
    assert to_telegram_html("<div>hi</div><script>x()</script>") == (
        "&lt;div&gt;hi&lt;script&gt;x()"
    )


def test_disallowed_attributes_are_stripped():
    assert to_telegram_html('<a href="https://x.y" onclick="evil()">t</a>') == (
        '<a href="https://x.y">t</a>'
    )


def test_unclosed_tag_is_auto_closed():
    assert to_telegram_html("<b>bold to the end") == "<b>bold to the end</b>"


def test_stray_close_tag_is_dropped():
    assert to_telegram_html("oops</b> fine") == "oops fine"


def test_interleaved_tags_are_rebalanced():
    # <b>x<i>y</b>z</i> would 400 at Telegram; nesting is repaired
    assert to_telegram_html("<b>x<i>y</b>z") == "<b>x<i>y</i></b>z"


def test_spoiler_span_kept_other_spans_literal():
    assert to_telegram_html('<span class="tg-spoiler">shh</span>') == (
        '<span class="tg-spoiler">shh</span>'
    )
    assert to_telegram_html('<span style="x">hi</span>') == '&lt;span style=&quot;x&quot;&gt;hi'


def test_br_becomes_newline():
    assert to_telegram_html("a<br/>b<br>c") == "a\nb\nc"


def test_code_block():
    assert to_telegram_html("<pre>if a < b:\n  run()</pre>") == (
        "<pre>if a &lt; b:\n  run()</pre>"
    )
