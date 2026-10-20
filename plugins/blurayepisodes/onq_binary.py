"""Bounded readers for onQ resource tables and QCOF v5 instructions."""
import struct

def require(condition, message):
    if not condition:
        raise ValueError(message)

def u16(b, p):
    return struct.unpack_from('>H', b, p)[0]

def u32(b, p):
    return struct.unpack_from('>I', b, p)[0]

def string(b, p):
    return b[p:b.index(0, p)].decode('utf-8')

def table(b):
    require(b[:4] in (b'QCTF', b'QCSF') and u32(b, 4) == 1, 'unrecognized menu structure')
    rows, cols, types, offsets, data = struct.unpack_from('>5I', b, 8)
    require(0 < rows <= 4096 and 0 < cols <= 128, 'table dimensions')
    stride = u16(b, 28)
    result = []
    for r in range(rows):
        row = []
        for c in range(cols):
            p = data + r * stride + u16(b, offsets + c * 2)
            t = b[types + c]
            if t == 22:
                p = u16(b, p)
                v = string(b, p) if p else ''
            else:
                v = struct.unpack_from({17: '>B', 18: '>h', 19: '>H', 20: '>I', 21: '>i'}[t], b, p)[0]
            row.append(v)
        result.append(row)
    return result

def resources(b):
    require(b[:4] == b'QCDF' and u32(b, 4) in (3, 4), 'unrecognized menu structure')
    offsets = u32(b, 16)
    count = u16(b, 26)
    require(0 < count <= 4096, 'resource count')
    result = []
    for i in range(count):
        p = (u32(b, offsets + 4 * i) if u32(b, 4) == 4 else u16(b, offsets + 2 * i)) + 8
        t, flags = (b[p], u16(b, p + 1))
        p += 3
        if t == 16:
            p += 24
        name = string(b, p)
        p += len(name.encode('utf-8')) + 1
        uri = string(b, p)
        result.append(dict(id=i + 1, type=t, flags=flags, name=name, uri=uri))
    return result

def globals_and_functions(b, screen=False, with_objects=False):
    require(b[:4] == b'QCOF' and u32(b, 4) == 5, 'unrecognized menu structure')
    p = u32(b, 8) if screen else b[12]
    require(b[p] == (1 if screen else 15), 'unrecognized menu structure')
    p += 40 if screen else 6
    events = b[p]
    p += 1 + events * 6
    section_id, objects, functions, count = struct.unpack_from('>4H', b, p)
    p += 8
    require(count <= 4096 and functions <= 4096 and (objects <= 4096), 'QCO counts')
    types = b[p:p + count]
    p += count
    values = list(struct.unpack_from('>' + 'i' * count, b, p))
    p += count * 4 + 2
    variables = {}
    for i, (t, v) in enumerate(zip(types, values), 1):
        if t == 22:
            v = string(b, v) if v else ''
        elif t in (28, 29):
            rows, cols, populated = struct.unpack_from('>3H', b, p)
            rows, cols = (max(1, rows), max(1, cols))
            require(rows * cols <= 65536, 'array dimensions')
            p += 6
            a = []
            if populated:
                a = list(struct.unpack_from('>' + 'i' * (rows * cols), b, p))
                p += rows * cols * 4
                if t == 29:
                    a = [string(b, v) if v else '' for v in a]
            v = dict(rows=rows, cols=cols, values=a)
        elif t != 21:
            raise ValueError(t)
        variables[i | (16384 if screen else 49152)] = {'type': t, 'value': v}
    length = u32(b, p)
    p += 4
    require(length <= 8 * 1024 * 1024 and p + length <= len(b), 'code bounds')
    code = b[p:p + length]
    offsets = [u32(code, i * 4) for i in range(functions)]
    require(all((functions * 4 <= offset < length - 2 for offset in offsets)), 'function bounds')
    require(offsets == sorted(set(offsets)), 'function ordering')
    if with_objects:
        return (variables, code, offsets, parse_objects(b, p + length, objects))
    return (variables, code, offsets)

def parse_objects(b, p, count):
    result = []
    widths = {2: 3, 3: 7, 4: 7, 5: 35, 7: 9, 9: 5, 10: 19, 11: 3, 12: 21}
    for _ in range(count):
        start = p
        kind, ident, parent = (b[p], u16(b, p + 1), u16(b, p + 3))
        p += 5
        flags = b[p]
        if kind == 6:
            size = 1 + 1 + 8 + 2 + 4 + 2 + 4 + 4 + 1 + 4 + 2 + 4 + 5
            if flags & 16:
                size += 10
        elif kind == 8:
            size = 5 if flags & 2 else 3
        else:
            size = widths[kind]
        payload = b[p:p + size]
        p += size
        event_count = b[p]
        p += 1
        events = [struct.unpack_from('>3H', b, p + i * 6) for i in range(event_count)]
        p += event_count * 6
        result.append(dict(id=ident, parent=parent, type=kind, flags=flags, offset=start, data=payload.hex(), events=events))
    return result
OPS = {0: 'nop', 1: 'push_u8', 2: 'push_i16', 3: 'push_u16', 4: 'push_i32', 5: 'push_string', 6: 'add', 7: 'div', 8: 'mod', 9: 'mul', 10: 'sub', 12: 'eq', 13: 'gt', 14: 'ge', 15: 'lt', 16: 'le', 17: 'ne', 19: 'jump_zero_peek', 20: 'jump', 21: 'system_call', 22: 'call', 23: 'object_call', 24: 'local_array_get', 25: 'local_string_array_get', 26: 'local_array_set', 27: 'local_string_array_set', 28: 'local_array2_get', 29: 'local_string_array2_get', 30: 'local_array2_set', 31: 'local_string_array2_set', 32: 'local_get', 33: 'local_string_get', 34: 'local_set', 35: 'local_string_set', 36: 'array_get', 37: 'string_array_get', 38: 'array_set', 39: 'string_array_set', 40: 'array2_get', 41: 'string_array2_get', 42: 'array2_set', 43: 'string_array2_set', 44: 'global_get', 45: 'global_string_get', 46: 'global_set', 47: 'global_string_set', 48: 'property_get', 49: 'string_property_get', 50: 'property_set', 51: 'string_property_set', 52: 'result_get', 53: 'string_result_get', 54: 'result_set', 55: 'string_result_set', 56: 'local_array_new', 57: 'local_string_array_new', 58: 'local_array_clear', 59: 'local_string_array_clear', 60: 'local_string_clear', 61: 'drop', 62: 'drop_string', 63: 'return', 64: 'activate_screen', 65: 'local_increment', 66: 'global_increment', 67: 'jump_nonzero_peek', 68: 'jump_nonzero_pop', 69: 'jump_zero_pop', 70: 'local_array_push', 71: 'global_array_push', 72: 'event_get', 73: 'event_set', 74: 'function_reference', 75: 'array_size', 76: 'not', 77: 'and', 78: 'or', 79: 'invert', 80: 'xor', 81: 'shift_left', 82: 'shift_right', 83: 'check_scope', 84: 'indirect_call'}

def instructions(code, start, end):
    p = start + 3
    result = []
    one_byte = set(range(24, 36)) | {1, 22, 48, 49, 50, 51, 56, 57, 58, 59, 60, 61, 62, 66, 70, 72, 73, 75, 84}
    two_byte = {2, 3, 19, 20, 21, 23, 65, 67, 68, 69}
    while p < end:
        at = p
        op = code[p]
        p += 1
        args = []
        if op == 5:
            text = string(code, p)
            p += len(text.encode('utf-8')) + 1
            args = [text]
        elif op == 4:
            args = [struct.unpack_from('>i', code, p)[0]]
            p += 4
        elif op in one_byte:
            args = [code[p]]
            p += 1
        elif op in two_byte:
            if op in (2, 19, 20, 67, 68, 69):
                args = [struct.unpack_from('>h', code, p)[0]]
                if op != 2:
                    args = [p + args[0]]
            elif op == 3:
                args = [u16(code, p)]
            else:
                args = list(code[p:p + 2])
            p += 2
        elif op not in OPS:
            raise ValueError((at, op))
        require(p <= end, 'instruction bounds')
        result.append([at, OPS[op], *args])
    return result
