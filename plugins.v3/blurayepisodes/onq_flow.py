"""Bounded numeric data-flow analysis for onQ, without executing menu effects.

Expressions retain their source (argument, global, array or call). Branches
are joined; disagreement is preserved, never replaced by one convenient path.
Unknown operations invalidate the affected analysis instead of guessing.
"""
from collections import deque

UNKNOWN = ("unknown",)
PUSH = {"push_u8", "push_i16", "push_u16", "push_i32"}


def const(value):
    return ("const", value)


def number(value):
    return value[1] if value and value[0] == "const" else None


def bounded(value):
    pending = [(value, 0)]
    size = 0
    while pending:
        item, depth = pending.pop()
        size += 1
        if size > 256 or depth > 24:
            return UNKNOWN
        if isinstance(item, tuple):
            pending.extend((child, depth + 1) for child in item)
    return value


def nodes(value, kind):
    found = []
    if isinstance(value, tuple) and value:
        if value[0] == kind:
            found.append(value)
        for child in (value[1:] if isinstance(value[0], str) else value):
            if isinstance(child, tuple):
                found.extend(nodes(child, kind))
    return found


def join(left, right):
    if left == right:
        return left
    if UNKNOWN in (left, right):
        return UNKNOWN
    choices = set(left[1:] if left[0] == "choice" else (left,))
    choices.update(right[1:] if right[0] == "choice" else (right,))
    return ("choice", *sorted(choices, key=repr)) if len(choices) <= 4 else UNKNOWN


def arithmetic(op, left, right):
    if op == "add":
        terms = list(left[1:] if left[0] == "add" else (left,))
        terms += list(right[1:] if right[0] == "add" else (right,))
        value = sum(t[1] for t in terms if t[0] == "const")
        terms = [t for t in terms if t[0] != "const"]
        if value:
            terms.append(const(value))
        if not terms:
            return const(0)
        if len(terms) == 1:
            return terms[0]
        return ("add", *sorted(terms, key=repr)) if len(terms) <= 8 else UNKNOWN
    if op == "sub" and right == const(0):
        return left
    if op == "mul" and const(1) in (left, right):
        return right if left == const(1) else left
    # Comparisons stay symbolic: caller must not treat one branch as executed.
    return (op, left, right) if len(repr((left, right))) < 2048 else UNKNOWN


def analyze(code):
    """Return stable reads/writes/calls after a bounded control-flow fixed point."""
    if not code:
        return []
    positions = {ins[0]: i for i, ins in enumerate(code)}
    if any(ins[1].startswith("jump") and ins[2] not in positions for ins in code):
        return []
    local_ids = {ins[2] for ins in code
                 if ins[1] in ("local_get", "local_set", "local_increment")}
    initial = {key: ("local", key) for key in local_ids}

    def step(index, state):
        stack, local = list(state[0]), dict(state[1])
        at, op, *args = code[index]
        events = []
        successors = [index + 1] if index + 1 < len(code) else []

        def pop():
            return stack.pop() if stack else UNKNOWN

        def effect(kind, *values):
            events.append((kind, at, *values))

        def identifier(value):
            n = number(value)
            return n & 65535 if n is not None else None

        if op in PUSH:
            stack.append(const(args[0]))
        elif op == "local_get":
            stack.append(local.get(args[0], UNKNOWN))
        elif op == "local_set":
            local[args[0]] = pop()
        elif op == "local_increment":
            local[args[0]] = arithmetic("add", local.get(args[0], UNKNOWN), const(args[1]))
        elif op == "global_get":
            key = identifier(pop())
            value = ("global", key) if key is not None else UNKNOWN
            stack.append(value)
            effect("global", key)
        elif op == "global_set":
            key, value = identifier(pop()), pop()
            effect("write", key, value)
        elif op in ("array_get", "array2_get"):
            key = identifier(pop())
            column = pop() if op == "array2_get" else None
            row = pop()
            value = ("array", key, row) if column is None else ("array2", key, row, column)
            stack.append(value)
            effect("array", key, value)
        elif op in ("add", "sub", "mul", "div", "mod", "eq", "ne", "lt", "le", "gt", "ge"):
            right, left = pop(), pop()
            stack.append(arithmetic(op, left, right))
        elif op == "not":
            stack.append(("not", pop()))
        elif op == "call":
            key = identifier(pop())
            values = tuple(reversed([pop() for _ in range(args[0])]))
            local["result"] = ("call", key, *values)
            effect("call", key, values)
        elif op == "object_call":
            key = identifier(pop())
            values = tuple(reversed([pop() for _ in range(args[1])]))
            local["result"] = ("object", key, args[0], *values)
            effect("object", key, args[0], values)
        elif op == "result_get":
            stack.append(local.get("result", UNKNOWN))
        elif op == "result_set":
            effect("result", pop())
        elif op == "system_call":
            for _ in range(args[1]):
                pop()
            local["result"] = UNKNOWN
        elif op == "drop":
            for _ in range(args[0]):
                pop()
        elif op == "check_scope":
            scope, value = pop(), pop()
            stack.append(("scope", scope, value))
        elif op == "return":
            successors = []
        elif op == "jump":
            successors = [positions[args[0]]] if args[0] in positions else []
        elif op in ("jump_zero_pop", "jump_nonzero_pop", "jump_zero_peek", "jump_nonzero_peek"):
            if op.endswith("_pop"):
                pop()
            if args[0] in positions:
                successors.append(positions[args[0]])
        elif op in ("nop", "push_string", "drop_string", "string_result_get",
                    "local_string_get", "local_string_set"):
            pass  # Separate string stack; never used as numeric evidence.
        else:
            # No guessed stack effects for unsupported operations.
            effect("unsupported", op)
            stack = []
            local = {key: UNKNOWN for key in local}
        if len(stack) > 128:
            stack = []
        return (tuple(bounded(v) for v in stack),
                {k: bounded(v) for k, v in local.items()}), events, successors

    states = {0: ((), initial)}
    pending = deque([0])
    visits = 0
    while pending:
        visits += 1
        if visits > 20000:
            return []  # Cannot prove a stable result within the budget.
        i = pending.popleft()
        outgoing, _, successors = step(i, states[i])
        for dest in successors:
            old = states.get(dest)
            if old is None:
                merged = outgoing
            else:
                stack = tuple(join(a, b) for a, b in zip(old[0], outgoing[0])) \
                    if len(old[0]) == len(outgoing[0]) else ()
                keys = old[1].keys() | outgoing[1].keys()
                local = {k: join(old[1].get(k, UNKNOWN), outgoing[1].get(k, UNKNOWN))
                         for k in keys}
                merged = stack, local
            if old != merged:
                states[dest] = merged
                pending.append(dest)
    events = []
    for i in sorted(states):
        events.extend(step(i, states[i])[1])
    return events
