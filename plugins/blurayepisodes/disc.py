"""Read standard navigation entry metadata without running disc applications."""
import re
import struct
from pathlib import Path


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


class Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def take(self, count):
        require(0 <= count <= len(self.data) - self.pos, "导航数据截断")
        start = self.pos
        self.pos += count
        return self.data[start:self.pos]

    def number(self, size):
        return int.from_bytes(self.take(size), "big")

    def text(self, count):
        return self.take(count).decode("utf-8")

    def app_string(self):
        size = self.number(1)
        value = self.text(size)
        if size % 2 == 0:
            self.take(1)
        return value


def index_entries(data):
    require(data[:4] == b"INDX" and data[4:8] in (b"0100", b"0200", b"0300"),
            "无效的光盘索引")
    header = Reader(data)
    header.take(8)
    offset = header.number(4)
    require(40 <= offset <= len(data) - 4, "索引表偏移越界")
    header.pos = offset
    size = header.number(4)
    r = Reader(header.take(size))
    entries = []

    def entry(label):
        flags = r.number(4)
        kind = flags >> 30
        playback = r.number(2) >> 14
        require(kind in (1, 2), "未知的导航入口类型")
        if kind == 1:
            reference = r.number(2)
            r.take(4)
            reference = None if reference == 65535 else reference
        else:
            reference = r.text(5)
            r.take(1)
            require(re.fullmatch(r"\d{5}", reference), "无效的 BDJO 引用")
        entries.append(dict(entry=label, kind="HDMV" if kind == 1 else "BDJ",
                            reference=reference, playback_type=playback))

    entry("first_play")
    entry("top_menu")
    count = r.number(2)
    require(count <= 4096, "导航标题数量异常")
    for number in range(1, count + 1):
        entry(f"title_{number}")
    require(r.pos == len(r.data), "索引表含未解析数据")
    return entries


def bdjo(data):
    require(data[:4] == b"BDJO" and data[4:8] in (b"0100", b"0200", b"0300"),
            "无效的 BD-J 对象")
    require(len(data) >= 48, "BDJO 头部截断")
    offsets = struct.unpack_from(">6I", data, 8)
    require(all(48 <= p < len(data) for p in offsets)
            and list(offsets) == sorted(set(offsets)), "BDJO 区块偏移无效")

    def section(index):
        start = offsets[index]
        end = offsets[index + 1] if index < 5 else len(data)
        r = Reader(data[start:end])
        size = r.number(4)
        payload = r.take(size)
        require(r.pos == len(r.data), "BDJO 区块长度不一致")
        return Reader(payload)

    cache_reader = section(1)
    cache_count = cache_reader.number(1)
    cache_reader.take(1)
    caches = {}
    for _ in range(cache_count):
        kind = cache_reader.number(1)
        name = cache_reader.text(5)
        language = cache_reader.text(3)
        cache_reader.take(3)
        require(kind in (1, 2) and re.fullmatch(r"\d{5}", name)
                and name not in caches, "BDJO 缓存引用无效或重复")
        caches[name] = dict(kind=kind, language=language)
    require(cache_reader.pos == len(cache_reader.data), "BDJO 缓存表长度不一致")

    table = section(3)
    count = table.number(1)
    table.take(1)
    apps = []
    for _ in range(count):
        control = table.number(1)
        kind = table.number(1) >> 4
        organization = table.number(4)
        ident = table.number(2)
        table.take(10)
        profiles = table.number(2) >> 12
        table.take(profiles * 6)
        table.take(2)
        names_size = table.number(2)
        names = Reader(table.take(names_size))
        labels = {}
        while names.pos < len(names.data):
            language = names.text(3)
            labels[language] = names.text(names.number(1))
        if names_size % 2:
            table.take(1)
        table.app_string()  # icon locator
        table.take(2)  # icon flags
        base = table.app_string()
        extension = table.app_string()
        initial_class = table.app_string()
        params_size = table.number(1)
        params_reader = Reader(table.take(params_size))
        params = []
        while params_reader.pos < len(params_reader.data):
            params.append(params_reader.text(params_reader.number(1)))
        if params_size % 2 == 0:
            table.take(1)
        apps.append(dict(control=control, kind=kind, organization=organization,
                         id=ident, names=labels, base=base, classpath=extension,
                         initial_class=initial_class, parameters=params))
    require(table.pos == len(table.data), "BDJO 应用表长度不一致")
    return dict(cache=caches, applications=apps)


def safe_read(root, relative, maximum=4 * 1024 * 1024):
    root = Path(root).resolve(strict=True)
    path = (root / relative).resolve(strict=True)
    require(path.is_relative_to(root) and path.is_file(), "导航引用越出原盘")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    require(len(data) <= maximum, "导航文件超过大小限制")
    return path, data


def discover(disc):
    """List only applications registered by the index; never guess a JAR number."""
    disc = Path(disc).resolve(strict=True)
    index_path, data = safe_read(disc, "BDMV/index.bdmv")
    entries = index_entries(data)
    descriptors, candidates, files = {}, {}, [index_path]
    for entry in entries:
        if entry["kind"] != "BDJ":
            continue
        name = entry["reference"]
        if name in descriptors:
            continue
        descriptor_path, data = safe_read(disc, f"BDMV/BDJO/{name}.bdjo")
        descriptor = bdjo(data)
        descriptors[name] = descriptor
        files.append(descriptor_path)
        for app in descriptor["applications"]:
            # Only Java applications that can run; destroyed/killed entries aren't candidates.
            if app["kind"] != 1 or app["control"] not in (1, 2):
                continue
            for extension in [None] + app["classpath"].split(";"):
                base = app["base"]
                value = base if not extension else (
                    extension[1:] if extension.startswith("/") else base + "/" + extension)
                require(re.fullmatch(r"\d{5}(?:/[A-Za-z0-9_./-]*)?", value)
                        and ".." not in value.split("/"), "BDJO 应用路径不受支持")
                jar_name = value[:5]
                cache = descriptor["cache"].get(jar_name)
                require(cache is not None, "应用引用未登记的缓存")
                if cache["kind"] != 1:
                    continue
                path = (disc / "BDMV/JAR" / (jar_name + ".jar")).resolve(strict=True)
                require(path.is_relative_to(disc) and path.is_file(), "JAR 引用越界")
                info = candidates.setdefault(str(path), dict(path=str(path), applications=[]))
                binding = dict(bdjo=name, class_name=app["initial_class"],
                               classpath=value, control=app["control"])
                if binding not in info["applications"]:
                    info["applications"].append(binding)
    return dict(entries=entries, descriptors=descriptors,
                jars=list(candidates.values()), files=[str(p) for p in files])
