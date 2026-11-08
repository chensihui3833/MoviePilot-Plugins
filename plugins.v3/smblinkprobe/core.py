"""只比较调用者提供的三个 SMB 文件身份，不处理认证或删除文件。"""


def _identity(info):
    device = getattr(info, "st_dev", None)
    inode = getattr(info, "st_ino", None)
    if not isinstance(device, int) or not isinstance(inode, int) or inode <= 0:
        raise ValueError("SMB 文件标识不可用，不能判定硬链接")
    return device, inode


def compare_identities(stat, source, hardlink, control):
    """要求硬链接相同、对照文件不同，并重复读取以检查一致性。"""
    first = tuple(_identity(stat(path)) for path in (source, hardlink, control))
    second = tuple(_identity(stat(path)) for path in (source, hardlink, control))
    return {
        "same_hardlink": first[0] == first[1],
        "control_distinct": first[0] != first[2] and first[1] != first[2],
        "stable_on_repeat": first == second,
    }
