"""Original synthetic standard navigation metadata, without commercial assets."""
import struct


def app_string(text):
    data = text.encode()
    return bytes([len(data)]) + data + (b"\0" if len(data) % 2 == 0 else b"")


def index_blob(bdjo_name="00123", hdmv=False):
    if hdmv:
        entry = b"\x40\0\0\0" + bytes(8)
    else:
        entry = b"\x80\0\0\0\x80\0" + bdjo_name.encode() + b"\0"
    body = entry * 2 + b"\0\1" + entry
    return b"INDX0200" + struct.pack(">I", 40) + bytes(28) + struct.pack(">I", len(body)) + body


def bdjo_blob(jars=("00789",), base=None, extension="", initial_class="example.Menu"):
    base = base or jars[0]
    cache = bytes([len(jars), 0]) + b"".join(
        b"\1" + name.encode() + b"eng" + bytes(3) for name in jars)
    app = b"\1\x10" + struct.pack(">IH", 1234, 99) + bytes(10)
    app += bytes(6)  # no profiles, flags, or localized names
    app += app_string("") + bytes(2)
    app += app_string(base) + app_string(extension) + app_string(initial_class)
    app += bytes(2)  # empty parameters + alignment
    parts = [bytes(10), cache, bytes(4), b"\1\0" + app]
    chunks = [struct.pack(">I", len(p)) + p for p in parts]
    chunks += [bytes(4), bytes(2)]
    offsets, pos = [], 48
    for chunk in chunks:
        offsets.append(pos)
        pos += len(chunk)
    return b"BDJO0200" + struct.pack(">6I", *offsets) + bytes(16) + b"".join(chunks)


def write_entries(bdmv, jars=("00002",), name="00123", **kwargs):
    (bdmv / "BDJO").mkdir(exist_ok=True, parents=True)
    (bdmv / "index.bdmv").write_bytes(index_blob(name))
    (bdmv / "BDJO" / (name + ".bdjo")).write_bytes(bdjo_blob(jars, **kwargs))
