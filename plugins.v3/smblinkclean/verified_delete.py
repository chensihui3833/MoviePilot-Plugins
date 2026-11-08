"""仅删除已在同一个 SMB 文件句柄上重新核验的源文件。"""


def delete_verified(path, expected, recheck_library):
    """拒绝不同身份、链接、权限冲突及媒体库复核失败；异常交给调用方。"""
    from smbclient._io import SMBFileTransaction, SMBFileIO, query_info, set_info
    from smbprotocol.file_info import (FileAttributes, FileAttributeTagInformation, FileBasicInformation,
                                       FileDispositionInformation, FileFsVolumeInformation,
                                       FileInternalInformation, FileStandardInformation)
    from smbprotocol.open import CreateOptions, FilePipePrinterAccessMask
    from smbprotocol.structure import DateTimeField

    # 不允许并发写入或重命名已打开的源文件；有其他写入句柄时宁可失败。
    raw = SMBFileIO(path, mode="r", share_access="r",
                    desired_access=FilePipePrinterAccessMask.DELETE | FilePipePrinterAccessMask.FILE_READ_ATTRIBUTES,
                    create_options=CreateOptions.FILE_OPEN_REPARSE_POINT | CreateOptions.FILE_NON_DIRECTORY_FILE)
    raw.open()
    try:
        with SMBFileTransaction(raw) as transaction:
            query_info(transaction, FileBasicInformation)
            query_info(transaction, FileFsVolumeInformation, output_buffer_length=88)
            query_info(transaction, FileInternalInformation)
            query_info(transaction, FileStandardInformation)
            query_info(transaction, FileAttributeTagInformation)
        basic, volume, internal, standard, attributes = transaction.results
        epoch = DateTimeField.EPOCH_FILETIME
        observed = {
            "dev": volume["volume_serial_number"].get_value(),
            "ino": internal["index_number"].get_value(),
            "size": standard["end_of_file"].get_value(),
            "mtime_ns": (basic["last_write_time"].get_value() - epoch) * 100,
            "ctime_ns": (basic["creation_time"].get_value() - epoch) * 100,
        }
        if (attributes["reparse_tag"].get_value()
                or basic["file_attributes"].get_value() & FileAttributes.FILE_ATTRIBUTE_REPARSE_POINT
                or any(expected.get(k) != v for k, v in observed.items())):
            return False
        # 回调在句柄保持打开时再次完整检查媒体库；异常或否定均不删除。
        if not recheck_library():
            return False
        with SMBFileTransaction(raw) as transaction:
            disposition = FileDispositionInformation()
            disposition["delete_pending"] = True
            set_info(transaction, disposition)
        return True
    finally:
        raw.close()
