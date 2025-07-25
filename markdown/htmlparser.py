# Python Markdown

# A Python implementation of John Gruber's Markdown.

# Documentation: https://python-markdown.github.io/
# GitHub: https://github.com/Python-Markdown/markdown/
# PyPI: https://pypi.org/project/Markdown/

# Started by Manfred Stienstra (http://www.dwerg.net/).
# Maintained for a few years by Yuri Takhteyev (http://www.freewisdom.org).
# Currently maintained by Waylan Limberg (https://github.com/waylan),
# Dmitry Shachnev (https://github.com/mitya57) and Isaac Muse (https://github.com/facelessuser).

# Copyright 2007-2023 The Python Markdown Project (v. 1.7 and later)
# Copyright 2004, 2005, 2006 Yuri Takhteyev (v. 0.2-1.6b)
# Copyright 2004 Manfred Stienstra (the original version)

# License: BSD (see LICENSE.md for details).

"""
This module imports a copy of [`html.parser.HTMLParser`][] and modifies it heavily through monkey-patches.
A copy is imported rather than the module being directly imported as this ensures that the user can import
and  use the unmodified library for their own needs.
"""

from __future__ import annotations

import re
import importlib.util
import sys
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # pragma: no cover
    from markdown import Markdown


# Import a copy of the html.parser lib as `htmlparser` so we can monkeypatch it.
# Users can still do `from html import parser` and get the default behavior.
spec = importlib.util.find_spec('html.parser')
htmlparser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(htmlparser)
sys.modules['htmlparser'] = htmlparser

# This is a hack. We are sneaking in `</>` so we can capture it without the HTML parser
# throwing it away. When we see it, we will process it as data.
htmlparser.starttagopen = re.compile('<[a-zA-Z]|</>')

# Monkeypatch `HTMLParser` to only accept `?>` to close Processing Instructions.
htmlparser.piclose = re.compile(r'\?>')
# Monkeypatch `HTMLParser` to only recognize entity references with a closing semicolon.
htmlparser.entityref = re.compile(r'&([a-zA-Z][-.a-zA-Z0-9]*);')
# Monkeypatch `HTMLParser` to no longer support partial entities. We are always feeding a complete block,
# so the 'incomplete' functionality is unnecessary. As the `entityref` regex is run right before incomplete,
# and the two regex are the same, then incomplete will simply never match and we avoid the logic within.
htmlparser.incomplete = htmlparser.entityref
# Monkeypatch `HTMLParser` to not accept a backtick in a tag name, attribute name, or bare value.
htmlparser.locatestarttagend_tolerant = re.compile(r"""
  <[a-zA-Z][^`\t\n\r\f />\x00]*       # tag name <= added backtick here
  (?:[\s/]*                           # optional whitespace before attribute name
    (?:(?<=['"\s/])[^`\s/>][^\s/=>]*  # attribute name <= added backtick here
      (?:\s*=+\s*                     # value indicator
        (?:'[^']*'                    # LITA-enclosed value
          |"[^"]*"                    # LIT-enclosed value
          |(?!['"])[^`>\s]*           # bare value <= added backtick here
         )
         (?:\s*,)*                    # possibly followed by a comma
       )?(?:\s|/(?!>))*
     )*
   )?
  \s*                                 # trailing whitespace
""", re.VERBOSE)
htmlparser.locatetagend = re.compile(r"""
  [a-zA-Z][^`\t\n\r\f />]*           # tag name
  [\t\n\r\f /]*                     # optional whitespace before attribute name
  (?:(?<=['"\t\n\r\f /])[^`\t\n\r\f />][^\t\n\r\f /=>]*  # attribute name
    (?:=                            # value indicator
      (?:'[^']*'                    # LITA-enclosed value
        |"[^"]*"                    # LIT-enclosed value
        |(?!['"])[^>\t\n\r\f ]*     # bare value
       )
     )?
    [\t\n\r\f /]*                   # possibly followed by a space
   )*
   >?
""", re.VERBOSE)

# Match a blank line at the start of a block of text (two newlines).
# The newlines may be preceded by additional whitespace.
blank_line_re = re.compile(r'^([ ]*\n){2}')


class HTMLExtractor(htmlparser.HTMLParser):
    """
    Extract raw HTML from text.

    The raw HTML is stored in the [`htmlStash`][markdown.util.HtmlStash] of the
    [`Markdown`][markdown.Markdown] instance passed to `md` and the remaining text
    is stored in `cleandoc` as a list of strings.
    """

    def __init__(self, md: Markdown, *args, **kwargs):
        if 'convert_charrefs' not in kwargs:
            kwargs['convert_charrefs'] = False

        # Block tags that should contain no content (self closing)
        self.empty_tags = set(['hr'])

        self.lineno_start_cache = [0]

        self.override_comment_update = False

        # This calls self.reset
        super().__init__(*args, **kwargs)
        self.md = md

    def reset(self):
        """Reset this instance.  Loses all unprocessed data."""
        self.inraw = False
        self.intail = False
        self.stack: list[str] = []  # When `inraw==True`, stack contains a list of tags
        self._cache: list[str] = []
        self.cleandoc: list[str] = []
        self.lineno_start_cache = [0]

        super().reset()

    def close(self):
        """Handle any buffered data."""
        super().close()
        if len(self.rawdata):
            # Temp fix for https://bugs.python.org/issue41989
            # TODO: remove this when the bug is fixed in all supported Python versions.
            if self.convert_charrefs and not self.cdata_elem:  # pragma: no cover
                self.handle_data(htmlparser.unescape(self.rawdata))
            else:
                self.handle_data(self.rawdata)
        # Handle any unclosed tags.
        if len(self._cache):
            self.cleandoc.append(self.md.htmlStash.store(''.join(self._cache)))
            self._cache = []

    @property
    def line_offset(self) -> int:
        """Returns char index in `self.rawdata` for the start of the current line. """
        for ii in range(len(self.lineno_start_cache)-1, self.lineno-1):
            last_line_start_pos = self.lineno_start_cache[ii]
            lf_pos = self.rawdata.find('\n', last_line_start_pos)
            if lf_pos == -1:
                # No more newlines found. Use end of raw data as start of line beyond end.
                lf_pos = len(self.rawdata)
            self.lineno_start_cache.append(lf_pos+1)

        return self.lineno_start_cache[self.lineno-1]

    def at_line_start(self) -> bool:
        """
        Returns True if current position is at start of line.

        Allows for up to three blank spaces at start of line.
        """
        if self.offset == 0:
            return True
        if self.offset > 3:
            return False
        # Confirm up to first 3 chars are whitespace
        return self.rawdata[self.line_offset:self.line_offset + self.offset].strip() == ''

    def get_endtag_text(self, tag: str) -> str:
        """
        Returns the text of the end tag.

        If it fails to extract the actual text from the raw data, it builds a closing tag with `tag`.
        """
        # Attempt to extract actual tag from raw source text
        start = self.line_offset + self.offset
        m = htmlparser.endendtag.search(self.rawdata, start)
        if m:
            return self.rawdata[start:m.end()]
        else:  # pragma: no cover
            # Failed to extract from raw data. Assume well formed and lowercase.
            return '</{}>'.format(tag)

    def handle_starttag(self, tag: str, attrs: Sequence[tuple[str, str]]):
        # Handle tags that should always be empty and do not specify a closing tag
        if tag in self.empty_tags:
            self.handle_startendtag(tag, attrs)
            return

        if self.md.is_block_level(tag) and (self.intail or (self.at_line_start() and not self.inraw)):
            # Started a new raw block. Prepare stack.
            self.inraw = True
            self.cleandoc.append('\n')

        text = self.get_starttag_text()
        if self.inraw:
            self.stack.append(tag)
            self._cache.append(text)
        else:
            self.cleandoc.append(text)
            if tag in self.CDATA_CONTENT_ELEMENTS:
                # This is presumably a standalone tag in a code span (see #1036).
                self.clear_cdata_mode()

    def handle_endtag(self, tag: str):
        text = self.get_endtag_text(tag)

        if self.inraw:
            self._cache.append(text)
            if tag in self.stack:
                # Remove tag from stack
                while self.stack:
                    if self.stack.pop() == tag:
                        break
            if len(self.stack) == 0:
                # End of raw block.
                if blank_line_re.match(self.rawdata[self.line_offset + self.offset + len(text):]):
                    # Preserve blank line and end of raw block.
                    self._cache.append('\n')
                else:
                    # More content exists after `endtag`.
                    self.intail = True
                # Reset stack.
                self.inraw = False
                self.cleandoc.append(self.md.htmlStash.store(''.join(self._cache)))
                # Insert blank line between this and next line.
                self.cleandoc.append('\n\n')
                self._cache = []
        else:
            self.cleandoc.append(text)

    def handle_data(self, data: str):
        if self.intail and '\n' in data:
            self.intail = False
        if self.inraw:
            self._cache.append(data)
        else:
            self.cleandoc.append(data)

    def handle_empty_tag(self, data: str, is_block: bool):
        """ Handle empty tags (`<data>`). """
        if self.inraw or self.intail:
            # Append this to the existing raw block
            self._cache.append(data)
        elif self.at_line_start() and is_block:
            # Handle this as a standalone raw block
            if blank_line_re.match(self.rawdata[self.line_offset + self.offset + len(data):]):
                # Preserve blank line after tag in raw block.
                data += '\n'
            else:
                # More content exists after tag.
                self.intail = True
            item = self.cleandoc[-1] if self.cleandoc else ''
            # If we only have one newline before block element, add another
            if not item.endswith('\n\n') and item.endswith('\n'):
                self.cleandoc.append('\n')
            self.cleandoc.append(self.md.htmlStash.store(data))
            # Insert blank line between this and next line.
            self.cleandoc.append('\n\n')
        else:
            self.cleandoc.append(data)

    def handle_startendtag(self, tag: str, attrs):
        self.handle_empty_tag(self.get_starttag_text(), is_block=self.md.is_block_level(tag))

    def handle_charref(self, name: str):
        self.handle_empty_tag('&#{};'.format(name), is_block=False)

    def handle_entityref(self, name: str):
        self.handle_empty_tag('&{};'.format(name), is_block=False)

    def handle_comment(self, data: str):
        # Check if the comment is unclosed, if so, we need to override position
        i = self.line_offset + self.offset + len(data) + 4
        if self.rawdata[i:i + 3] != '-->':
            self.handle_data('<')
            self.override_comment_update = True
            return
        self.handle_empty_tag('<!--{}-->'.format(data), is_block=True)

    def updatepos(self, i: int, j: int) -> int:
        if self.override_comment_update:
            self.override_comment_update = False
            i = 0
            j = 1
        return super().updatepos(i, j)

    def handle_decl(self, data: str):
        self.handle_empty_tag('<!{}>'.format(data), is_block=True)

    def handle_pi(self, data: str):
        self.handle_empty_tag('<?{}?>'.format(data), is_block=True)

    def unknown_decl(self, data: str):
        end = ']]>' if data.startswith('CDATA[') else ']>'
        self.handle_empty_tag('<![{}{}'.format(data, end), is_block=True)

    def parse_pi(self, i: int) -> int:
        if self.at_line_start() or self.intail:
            return super().parse_pi(i)
        # This is not the beginning of a raw block so treat as plain data
        # and avoid consuming any tags which may follow (see #1066).
        self.handle_data('<?')
        return i + 2

    def parse_html_declaration(self, i: int) -> int:
        if self.at_line_start() or self.intail:
            if self.rawdata[i:i+3] == '<![' and not self.rawdata[i:i+9] == '<![CDATA[':
                # We have encountered the bug in #1534 (Python bug `gh-77057`).
                # Provide an override until we drop support for Python < 3.13.
                result = self.parse_bogus_comment(i)
                if result == -1:
                    self.handle_data(self.rawdata[i:i + 1])
                    return i + 1
                return result
            return super().parse_html_declaration(i)
        # This is not the beginning of a raw block so treat as plain data
        # and avoid consuming any tags which may follow (see #1066).
        self.handle_data('<!')
        return i + 2

    def parse_bogus_comment(self, i: int, report: int = 0) -> int:
        # Override the default behavior so that bogus comments get passed
        # through unaltered by setting `report` to `0` (see #1425).
        pos = super().parse_bogus_comment(i, report)
        if pos == -1:  # pragma: no cover
            return -1
        self.handle_empty_tag(self.rawdata[i:pos], is_block=False)
        return pos

    # The rest has been copied from base class in standard lib to address #1036.
    # As `__startag_text` is private, all references to it must be in this subclass.
    # The last few lines of `parse_starttag` are reversed so that `handle_starttag`
    # can override `cdata_mode` in certain situations (in a code span).
    __starttag_text: str | None = None

    def get_starttag_text(self) -> str:
        """Return full source of start tag: `<...>`."""
        return self.__starttag_text

    def parse_starttag(self, i: int) -> int:  # pragma: no cover
        # Treat `</>` as normal data as it is not a real tag.
        if self.rawdata[i:i + 3] == '</>':
            self.handle_data(self.rawdata[i:i + 3])
            return i + 3

        self.__starttag_text = None
        endpos = self.check_for_whole_start_tag(i)
        if endpos < 0:
            self.handle_data(self.rawdata[i:i + 1])
            return i + 1
        rawdata = self.rawdata
        self.__starttag_text = rawdata[i:endpos]

        # Now parse the data between `i+1` and `j` into a tag and `attrs`
        attrs = []
        match = htmlparser.tagfind_tolerant.match(rawdata, i+1)
        assert match, 'unexpected call to parse_starttag()'
        k = match.end()
        self.lasttag = tag = match.group(1).lower()
        while k < endpos:
            m = htmlparser.attrfind_tolerant.match(rawdata, k)
            if not m:
                break
            attrname, rest, attrvalue = m.group(1, 2, 3)
            if not rest:
                attrvalue = None
            elif attrvalue[:1] == '\'' == attrvalue[-1:] or \
                 attrvalue[:1] == '"' == attrvalue[-1:]:  # noqa: E127
                attrvalue = attrvalue[1:-1]
            if attrvalue:
                attrvalue = htmlparser.unescape(attrvalue)
            attrs.append((attrname.lower(), attrvalue))
            k = m.end()

        end = rawdata[k:endpos].strip()
        if end not in (">", "/>"):
            lineno, offset = self.getpos()
            if "\n" in self.__starttag_text:
                lineno = lineno + self.__starttag_text.count("\n")
                offset = len(self.__starttag_text) \
                         - self.__starttag_text.rfind("\n")  # noqa: E127
            else:
                offset = offset + len(self.__starttag_text)
            self.handle_data(rawdata[i:endpos])
            return endpos
        if end.endswith('/>'):
            # XHTML-style empty tag: `<span attr="value" />`
            self.handle_startendtag(tag, attrs)
        else:
            # *** set `cdata_mode` first so we can override it in `handle_starttag` (see #1036) ***
            if tag in self.CDATA_CONTENT_ELEMENTS:
                self.set_cdata_mode(tag)
            self.handle_starttag(tag, attrs)
        return endpos
