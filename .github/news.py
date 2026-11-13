#!/usr/bin/env python3
"""The <news> entry of one version in addon.xml, as Markdown, for a release's notes.

    python3 .github/news.py 2.7.0

The entry is the lines after "v2.7.0" up to the next version's line; they
already read as a Markdown list. Exits with an error when there is none.
"""

import os
import re
import sys
import xml.etree.ElementTree as ET

ADDON_XML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "addon.xml")
VERSION_LINE = re.compile(r"v\d+(\.\d+)+")


def entry(news, version):
    """The lines of version's entry, or None."""
    lines, found = [], False
    for line in news.splitlines():
        line = line.strip()
        if VERSION_LINE.fullmatch(line):
            if found:
                break
            found = line == "v" + version
        elif found and line:
            lines.append(line)
    return lines if found and lines else None


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    news = next((element.text or "" for element in ET.parse(ADDON_XML).getroot().iter("news")), "")
    lines = entry(news, sys.argv[1])
    if not lines:
        sys.exit("addon.xml has no news for version {}".format(sys.argv[1]))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
