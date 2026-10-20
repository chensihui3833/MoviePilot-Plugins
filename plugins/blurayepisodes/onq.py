"""Resolve supported onQ episode menus from script/data links, never file-name order."""
import zipfile
from urllib.parse import urlsplit
from pathlib import Path
from .onq_binary import globals_and_functions, instructions, resources, table, require
from . import onq_flow as flow


class MenuArchive:
    def __init__(self, archive):
        self.archive = archive
        self.names = archive.namelist()
        require(len(self.names) <= 10000 and len(set(self.names)) == len(self.names),
                "重复或过多的 JAR 条目")
        self.resources = resources(self.read("eng/RESDIR.QCD"))

    def read(self, name):
        info = self.archive.getinfo(name)
        require(info.file_size <= 8 * 1024 * 1024, "菜单资源超过大小限制")
        return self.archive.read(info)

    def read_resource(self, uri):
        path = urlsplit(uri)
        require(path.scheme == "file" and path.path.startswith("/jar/"),
                "不支持的菜单资源地址")
        name = path.path[len("/jar/"):]
        require(name and "/" not in name and "\\" not in name and name not in (".", ".."),
                "菜单资源路径无效")
        for prefix in ("eng/", "common/"):
            if prefix + name in self.names:
                return self.read(prefix + name)
        raise ValueError("缺少菜单资源：" + name)


def read_navigation(jar):
    with zipfile.ZipFile(jar) as archive:
        return analyze(MenuArchive(archive))
PUSH = {'push_u8', 'push_i16', 'push_u16', 'push_i32'}

class Program:

    def __init__(self, data):
        self.variables, self.functions, self.locals, self.objects = ({}, {}, {}, {})
        self._flows = {}
        for screen in (False, True):
            variables, code, offsets, objects = globals_and_functions(data, screen, True)
            self.variables.update(variables)
            for i, start in enumerate(offsets):
                ident = i + 1 | (16384 if screen else 49152)
                self.locals[ident] = code[start]
                self.functions[ident] = instructions(code, start, offsets[i + 1] if i + 1 < len(offsets) else len(code))
            self.objects.update({o['id']: o for o in objects})

    def value(self, ident):
        return self.variables[ident & 65535]['value']

    def references(self, code, operation):
        kind = "global" if operation == "global_get" else "array"
        return list(dict.fromkeys(e[2] for e in self.effects(code)
                                  if e[0] == kind and e[2] is not None
                                  and (kind == "global" or e[3][0] ==
                                       ("array2" if operation == "array2_get" else "array"))))

    def effects(self, code):
        key = id(code)
        if key not in self._flows:
            self._flows[key] = flow.analyze(code)
        return self._flows[key]

    def pure(self, ident, args=(), overrides=None):
        """Evaluate only arithmetic/data access; unknown instructions fail closed."""
        code = self.functions[ident]
        positions = {ins[0]: i for i, ins in enumerate(code)}
        stack, result = ([], None)
        local = {i: 0 for i in range(1, self.locals[ident] + 1)}
        local.update({self.locals[ident] + i + 1: v for i, v in enumerate(args)})
        pc = steps = 0
        overrides = overrides or {}
        while pc < len(code):
            steps += 1
            require(steps < 10000, 'pure expression exceeds instruction budget')
            at, op, *vals = code[pc]
            pc += 1
            if op in PUSH:
                stack.append(vals[0])
            elif op == 'nop':
                pass
            elif op == 'local_get':
                stack.append(local[vals[0]])
            elif op == 'local_set':
                local[vals[0]] = stack.pop()
            elif op == 'local_increment':
                local[vals[0]] += vals[1]
            elif op == 'global_get':
                key = stack.pop() & 65535
                stack.append(overrides.get(key, self.value(key)))
            elif op == 'array_get':
                key = stack.pop() & 65535
                array = overrides.get(key, self.value(key))
                stack[-1] = array['values'][stack[-1]]
            elif op == 'not':
                stack[-1] = int(not stack[-1])
            elif op in ('add', 'sub', 'mul', 'lt', 'le', 'gt', 'ge', 'eq', 'ne'):
                right, left = (stack.pop(), stack.pop())
                value = {'add': lambda: left + right, 'sub': lambda: left - right, 'mul': lambda: left * right, 'lt': lambda: left < right, 'le': lambda: left <= right, 'gt': lambda: left > right, 'ge': lambda: left >= right, 'eq': lambda: left == right, 'ne': lambda: left != right}[op]()
                stack.append(int(value))
            elif op == 'result_set':
                result = stack.pop()
            elif op == 'jump':
                pc = positions[vals[0]]
            elif op in ('jump_zero_pop', 'jump_nonzero_pop'):
                value = stack.pop()
                if (value == 0) == (op == 'jump_zero_pop'):
                    pc = positions[vals[0]]
            elif op == 'return':
                return result
            else:
                raise ValueError(('not a pure expression', ident, at, op))
        raise ValueError('no return')

def playback_dependencies(value):
    """Require every merged path to retain the same episode and region sources."""
    if value[0] == "choice":
        alternatives = [playback_dependencies(v) for v in value[1:]]
        return alternatives[0] if all(v == alternatives[0] for v in alternatives) else None
    if value[0] != "add":
        return None
    terms = value[1:]
    calls = [t for t in terms if t[0] == "call"]
    globals_ = [t for t in terms if t[0] == "global"]
    if len(calls) != 1 or len(globals_) != 1:
        return None
    call = calls[0]
    if len(call) != 3 or call[2][0] != "local":
        return None
    extra = [t for t in terms if t not in (call, globals_[0])]
    if any(flow.nodes(t, "unknown") or flow.nodes(t, "const") or call[2] in flow.nodes(t, "local")
           for t in extra):
        return None
    return call, globals_[0]


def episode_flow(p):
    """Locate the verified arithmetic/data flow, without diagnostics or symbol names."""
    matches = {}
    for play, code in p.functions.items():
        effects = p.effects(code)
        if any(e[0] == "unsupported" or (e[0] == "write" and e[2] is None)
               for e in effects):
            continue
        for event in effects:
            if event[0] != "write" or event[2] is None:
                continue
            dependencies = playback_dependencies(event[3])
            if not dependencies:
                continue
            # A later/alternative write to the same state must not contradict it.
            if any(playback_dependencies(e[3]) != dependencies for e in effects
                   if e[0] == "write" and e[2] == event[2]):
                continue
            call, region = dependencies
            translator = call[1]
            if translator not in p.functions:
                continue
            count_arrays = p.references(p.functions[translator], "array_get")
            if len(count_arrays) != 1:
                continue
            count_id = count_arrays[0]
            try:
                counts = list(p.value(count_id)["values"])
                while counts and counts[-1] == 0:
                    counts.pop()
                if not (counts and len(counts) <= 100 and all(0 < n <= 100 for n in counts)
                        and sum(counts) <= 1000):
                    continue
                actual = [p.pure(translator, [e]) for e in range(sum(counts))]
                if actual != [e for count in counts for e in range(count)]:
                    continue
            except (ValueError, IndexError, KeyError, TypeError):
                continue
            key = (play, translator, count_id, region[1], event[2])
            matches[key] = (play, translator, count_id, counts, region[1], event[2])
    require(len(matches) == 1, "无法唯一证明盘内集号到播放状态的计算关系")
    return next(iter(matches.values()))


def analyze(source):
    res = source.resources
    scripts = [r for r in res if r['type'] == 3]
    require(len(scripts) == 1, 'menu script selection')
    p = Program(source.read_resource(scripts[0]['uri']))
    play, translator, count_id, counts, region_variable, episode_variable = episode_flow(p)
    offsets = []
    for ident, body in p.functions.items():
        if p.references(body, 'array_get') != [count_id]:
            continue
        reads = p.references(body, 'global_get')
        for disc_id in reads:
            if not isinstance(p.value(disc_id), int):
                continue
            try:
                results = [p.pure(ident, overrides={disc_id: d}) for d in range(1, len(counts) + 1)]
            except (ValueError, AssertionError, IndexError, KeyError):
                continue
            if results == [sum(counts[:d]) for d in range(len(counts))]:
                offsets.append((ident, disc_id))
    require(len(offsets) == 1, offsets)
    offset_func, disc_id = offsets[0]
    disc_number = p.value(disc_id)
    require(1 <= disc_number <= len(counts), 'unrecognized menu structure')
    offset = p.pure(offset_func)
    callers = set()
    for ident, body in p.functions.items():
        for event in p.effects(body):
            if (event[0] == "call" and event[2] == play and len(event[3]) == 3
                    and event[3][-1][0] == "global"):
                callers.add((ident, event[3][-1][1]))
    require(len(callers) == 1, callers)
    caller, selected = next(iter(callers))
    title_bindings = set()
    for obj in p.objects.values():
        for event, _, function in obj['events']:
            if event != 31 or function not in p.functions:
                continue
            for binding in p.effects(p.functions[function]):
                if (binding[0] == "object" and binding[3] == 1 and len(binding[4]) == 2
                        and binding[4][-1] ==
                        flow.arithmetic("add", ("global", selected), flow.const(1))):
                    target = p.objects.get(binding[2])
                    if not target or target["type"] != 8:
                        continue
                    resource_id = int(target['data'][-4:], 16)
                    if resource_id:
                        title_bindings.add((function, resource_id))
    require(len(title_bindings) == 1, title_bindings)
    title_binding = next(iter(title_bindings))
    title_resource = res[title_binding[1] - 1]
    title_table = table(source.read_resource(title_resource['uri']))
    titles = [' '.join(row[1].split()) for row in title_table[1:]]
    require(len(titles) == sum(counts), 'unrecognized menu structure')
    require(all(titles), "菜单标题为空")
    state_arrays = set()
    for ident, body in p.functions.items():
        for event in p.effects(body):
            if (event[0] == "array" and event[3][0] == "array2"
                    and event[3][2:] == (("global", episode_variable), flow.const(0))):
                state_arrays.add(event[2])
    require(len(state_arrays) == 1, "无法证明播放函数使用菜单传入的集号")
    states_id = state_arrays.pop()
    states = p.value(states_id)['values']
    state_objects = {}
    for ident, body in p.functions.items():
        if p.references(body, "array2_get") != [states_id]:
            continue
        for event in p.effects(body):
            if (event[0] == "call" and len(event[3]) == 4
                    and any(n[1] == states_id for n in flow.nodes(event[3], "array2"))):
                obj = p.objects.get(flow.number(event[3][-1]))
                if obj and obj['type'] == 8 and obj['flags'] & 2:
                    state_objects[obj["id"]] = obj
    require(len(state_objects) == 1, "无法唯一证明播放状态表的来源")
    seq_resource = res[int(next(iter(state_objects.values()))['data'][-4:], 16) - 1]
    seq_table = table(source.read_resource(seq_resource['uri']))
    region_offsets = set()
    for body in p.functions.values():
        for event in p.effects(body):
            if (event[0] == "write" and event[2] == region_variable
                    and event[3][0] == "scope" and event[3][2][0] == "global"):
                region_offsets.add(p.value(event[3][2][1]))
    require(region_offsets and 0 in region_offsets
            and all(isinstance(v, int) and 0 <= v < len(states) for v in region_offsets),
            "无法确认播放状态分支")
    regions = {f"offset_{v}": v for v in sorted(region_offsets)}
    rows = []
    for local in range(counts[disc_number - 1]):
        variants = {}
        for region, region_offset in regions.items():
            require(0 <= region_offset + local < len(states), "分集状态索引越界")
            state = states[region_offset + local]
            require(0 <= state < len(seq_table), "播放状态索引越界")
            resource_id = seq_table[state][0]
            if resource_id:
                require(1 <= resource_id <= len(res), "播放资源索引越界")
                variants[region] = res[resource_id - 1]['uri']
        require(variants, 'unrecognized menu structure')
        rows.append(dict(episode=offset + local + 1, title=titles[offset + local], variants=variants))
    return dict(disc=disc_number, counts=counts, titles=titles, episodes=rows, evidence=dict(title_table=title_resource['name'], title_handler=title_binding[0], selected_row_variable=selected, playback_caller=caller, playback_function=play, local_index_function=translator, disc_offset_function=offset_func, state_table=seq_resource['name'], regions=regions, analysis="bounded-data-flow"))
