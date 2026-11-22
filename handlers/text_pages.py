"""Split trusted Telegram HTML without breaking tags or Unicode characters."""
from html import escape
from html.parser import HTMLParser


class TextPages(HTMLParser):
    def __init__(self, limit):
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.pages = []
        self.parts = []
        self.tags = []
        self.units = 0

    def flush(self):
        if not self.units:
            return
        self.pages.append("".join(self.parts) + "".join(f"</{tag}>" for tag, _ in reversed(self.tags)))
        self.parts = [opening for _, opening in self.tags]
        self.units = 0

    def handle_starttag(self, tag, attrs):
        opening = self.get_starttag_text()
        self.tags.append((tag, opening))
        self.parts.append(opening)

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")
        if self.tags and self.tags[-1][0] == tag:
            self.tags.pop()

    def handle_data(self, data):
        for character in data:
            units = len(character.encode("utf-16-le")) // 2
            if self.units + units > self.limit:
                self.flush()
            self.parts.append(escape(character))
            self.units += units


def split_html(text, limit=900):
    parser = TextPages(limit)
    parser.feed(text)
    parser.close()
    parser.flush()
    return parser.pages or [""]
