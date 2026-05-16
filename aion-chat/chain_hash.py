import struct

_CRC32_TABLE = None


def _ensure_table():
    global _CRC32_TABLE
    if _CRC32_TABLE is not None:
        return
    _CRC32_TABLE = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
        _CRC32_TABLE.append(crc & 0xFFFFFFFF)


def crc32(data: bytes) -> int:
    _ensure_table()
    crc = 0xFFFFFFFF
    for b in data:
        crc = _CRC32_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF


def compute_chain_hash(prev_hash: str, msg_id: str, content: str, created_at: float) -> str:
    payload = f"{prev_hash}|{msg_id}|{content}|{created_at:.6f}".encode('utf-8')
    return format(crc32(payload), '08x')
