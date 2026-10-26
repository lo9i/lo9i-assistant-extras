"""The assistant's Markdown in WhatsApp's own marks; code is left as it is."""

from lo9i_whatsapp.formatting import to_whatsapp


def test_bold_italics_strikethrough_and_links():
    text = "**Done**: moved *two* events, ~~cancelled~~ one. See [the calendar](https://cal.example)."
    assert to_whatsapp(text) == "*Done*: moved _two_ events, ~cancelled~ one. See the calendar (https://cal.example)."


def test_headings_become_bold_lines_and_lists_use_dashes():
    assert to_whatsapp("## **Today**\n* gym\n+ dentist") == "*Today*\n- gym\n- dentist"


def test_code_is_left_alone():
    text = "Run `ls **/*.py` then:\n```bash\necho **hi**\n```"
    assert to_whatsapp(text) == "Run `ls **/*.py` then:\n```echo **hi**\n```"


def test_plain_text_and_lone_stars_stay():
    assert to_whatsapp("2 * 3 = 6, a link: https://a.example/x_y") == "2 * 3 = 6, a link: https://a.example/x_y"
