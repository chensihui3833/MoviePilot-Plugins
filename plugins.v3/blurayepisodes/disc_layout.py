"""Disc identity in real download folder names; no source renaming required."""
import re
import xml.etree.ElementTree as ET
from pathlib import Path

_TOKEN = re.compile(r"(?:^|[ ._\-])(?:disc|disk)[ ._\-]*0*(\d+)(?=$|[ ._\-])", re.I)


def disc_number(path):
    path = Path(path)
    numbers = {int(match[1]) for match in _TOKEN.finditer(path.name)}
    if len(numbers) == 1:
        number = next(iter(numbers))
        return number if number > 0 else None
    if numbers:
        return None
    # Untagged folders may identify the physical disc in the standard disc title.
    names = set()
    for xml in (path / "BDMV/META/DL").glob("bdmt_*.xml"):
        try:
            with xml.open("rb") as stream:
                data = stream.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024 or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                continue
            root = ET.fromstring(data)
            for element in root.iter():
                if element.tag.rsplit("}", 1)[-1] == "name" and element.text:
                    names.update(int(m[1]) for m in _TOKEN.finditer(element.text))
        except (OSError, ET.ParseError):
            continue
    return next(iter(names)) if len(names) == 1 and next(iter(names)) > 0 else None
