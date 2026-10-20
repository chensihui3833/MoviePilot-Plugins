"""Resolve supported onQ episode menus from script/data links, never file-name order."""
import zipfile
from urllib.parse import urlsplit
from pathlib import Path
from .onq_binary import globals_and_functions, instructions, resources, table, require


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
        for screen in (False, True):
            variables, code, offsets, objects = globals_and_functions(data, screen, True)
            self.variables.update(variables)
            for i, start in enumerate(offsets):
                ident = i + 1 | (16384 if screen else 49152)
                self.locals[ident] = code[start]
                self.functions[ident] = instructions(code, start, offsets[i + 1] if i + 1 < len(offsets) else len(code))
            self.objects.update({o['id']: o for o in objects})

    def find(self, marker):
        found = [ident for ident, code in self.functions.items() if any((ins[1:] == ['push_string', marker] for ins in code))]
        require(len(found) == 1, (marker, found))
        return found[0]

    def value(self, ident):
        return self.variables[ident & 65535]['value']

    def references(self, code, operation):
        return list(dict.fromkeys((code[i - 1][2] & 65535 for i, ins in enumerate(code) if i and ins[1] == operation and (code[i - 1][1] in PUSH))))

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
            elif op in ('add', 'sub', 'lt', 'le', 'gt', 'ge', 'eq', 'ne'):
                right, left = (stack.pop(), stack.pop())
                value = {'add': lambda: left + right, 'sub': lambda: left - right, 'lt': lambda: left < right, 'le': lambda: left <= right, 'gt': lambda: left > right, 'ge': lambda: left >= right, 'eq': lambda: left == right, 'ne': lambda: left != right}[op]()
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

def analyze(source):
    res = source.resources
    scripts = [r for r in res if r['type'] == 3]
    require(len(scripts) == 1, 'menu script selection')
    p = Program(source.read_resource(scripts[0]['uri']))
    play = p.find('playEpisode(epID = ')
    code = p.functions[play]
    first_call = next((i for i, ins in enumerate(code) if ins[1] == 'call'))
    translator = code[first_call - 1][2]
    require(code[first_call + 1][1] == "result_get"
            and code[first_call + 2][1] == "local_set", "无法追踪盘内集号计算结果")
    translated_local = code[first_call + 2][2]
    state_assignments = []
    for i in range(len(code) - 7):
        seq = code[i:i + 8]
        if ([r[1] for r in seq] == ["local_get", "push_i16", "global_get", "add",
                                   "local_get", "add", "push_i16", "global_set"]
                and seq[0][2] == translated_local):
            state_assignments.append((seq[1][2], seq[6][2]))
    require(len(state_assignments) == 1, "无法确认集号与区域状态的对应")
    region_variable, episode_variable = state_assignments[0]
    translate_code = p.functions[translator]
    count_arrays = p.references(translate_code, 'array_get')
    require(len(count_arrays) == 1, 'unrecognized menu structure')
    count_id = count_arrays[0]
    counts = p.value(count_id)['values']
    while counts and counts[-1] == 0:
        counts = counts[:-1]
    require(counts and len(counts) <= 100 and all(0 < n <= 100 for n in counts)
            and sum(counts) <= 1000, "盘内集数表无效")
    require([p.pure(translator, [e]) for e in range(sum(counts))] == [e for count in counts for e in range(count)], 'unrecognized menu structure')
    offsets = []
    for ident, body in p.functions.items():
        if p.references(body, 'array_get') != [count_id] or 'sub' not in [ins[1] for ins in body]:
            continue
        reads = p.references(body, 'global_get')
        if len(reads) != 2:
            continue
        for disc_id in reads:
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
    callers = []
    for ident, body in p.functions.items():
        for i, ins in enumerate(body):
            if i >= 3 and ins[1:] == ['call', 3] and (body[i - 1][1] in PUSH) and (body[i - 1][2] == play) and (body[i - 2][1] == 'global_get'):
                callers.append((ident, body[i - 3][2]))
    require(len(callers) == 1, callers)
    caller, selected = callers[0]
    title_bindings = []
    for obj in p.objects.values():
        for event, _, function in obj['events']:
            if event != 31 or function not in p.functions:
                continue
            body = p.functions[function]
            for i in range(len(body) - 5):
                seq = body[i:i + 6]
                if seq[0][1] in PUSH and seq[0][2] == selected and ([v[1] for v in seq[1:]] == ['global_get', 'push_u8', 'add', 'push_i16', 'object_call']) and (seq[2][2] == 1) and (seq[5][2:] == [1, 2]):
                    target = p.objects[seq[4][2]]
                    resource_id = int(target['data'][-4:], 16)
                    if resource_id:
                        title_bindings.append((function, resource_id))
    require(len(title_bindings) == 1, title_bindings)
    title_resource = res[title_bindings[0][1] - 1]
    title_table = table(source.read_resource(title_resource['uri']))
    titles = [' '.join(row[1].split()) for row in title_table[1:]]
    require(len(titles) == sum(counts), 'unrecognized menu structure')
    require([r[0] for r in title_table[1:]] == [f'BUTTON_{i:02d}' for i in range(len(titles))], 'unrecognized menu structure')
    store = p.find('storeEpisodePlaylists()')
    seqvars = p.references(p.functions[store], 'array2_get')
    require(len(seqvars) == 1, 'unrecognized menu structure')
    states = p.value(seqvars[0])['values']
    render = p.find("VideoSequencer.playEpisode()")
    require(p.references(p.functions[render], "array2_get") == seqvars,
            "播放函数没有使用同一份分集状态表")
    render_code = p.functions[render]
    require(any([r[1] for r in render_code[i:i + 5]] ==
                ["push_i16", "global_get", "push_u8", "push_i16", "array2_get"]
                and render_code[i][2] == episode_variable
                and render_code[i + 2][2] == 0
                and render_code[i + 3][2] == seqvars[0]
                for i in range(len(render_code) - 4)),
            "播放状态没有使用菜单传入的集号")
    state_objects = []
    body = p.functions[store]
    for i, ins in enumerate(body):
        if i >= 2 and ins[1:] == ['call', 4] and (body[i - 2][1] in PUSH):
            obj = p.objects.get(body[i - 2][2])
            if obj and obj['type'] == 8 and obj['flags'] & 2:
                state_objects.append(obj)
    require(len(state_objects) == 1, 'unrecognized menu structure')
    seq_resource = res[int(state_objects[0]['data'][-4:], 16) - 1]
    seq_table = table(source.read_resource(seq_resource['uri']))
    regions = {}
    for region in ('A', 'B'):
        marker = f'VideoSequencer.epRegionOffset = EpID.REG_{region}'
        code = p.functions[p.find(marker)]
        end = next((i for i, ins in enumerate(code) if ins[1:] == ['push_string', marker]))
        before = code[:end]
        assignment = max((i for i, ins in enumerate(before) if ins[1] == 'global_set'))
        seq = before[assignment - 5:assignment + 1]
        require([i[1] for i in seq] == ['push_u16', 'global_get', 'push_i32', 'check_scope', 'push_i16', 'global_set'], 'unrecognized menu structure')
        require(seq[4][2] == region_variable, "区域分支没有写入实际使用的状态变量")
        regions[region] = p.value(seq[0][2])
    require(regions["A"] == 0 and regions["B"] > 0, "不支持的区域分支")
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
    return dict(disc=disc_number, counts=counts, titles=titles, episodes=rows, evidence=dict(title_table=title_resource['name'], title_handler=title_bindings[0][0], selected_row_variable=selected, playback_caller=caller, playback_function=play, local_index_function=translator, disc_offset_function=offset_func, state_table=seq_resource['name'], regions=regions))
