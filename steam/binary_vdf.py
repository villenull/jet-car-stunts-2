"""Fail-closed reader/writer for Steam's binary VDF (shortcuts.vdf).

Values are kept as raw bytes/integers in their original order so that
serialize(parse(data)) reproduces the input byte-for-byte. Unknown or
ambiguous node types are rejected instead of guessed. Python >= 3.9, stdlib.
"""
import struct

T_MAP = 0x00
T_STRING = 0x01
T_INT32 = 0x02
T_FLOAT32 = 0x03
T_POINTER = 0x04
T_COLOR = 0x06
T_UINT64 = 0x07
T_END = 0x08
T_INT64 = 0x0A

_FIXED = {T_INT32: 4, T_FLOAT32: 4, T_POINTER: 4, T_COLOR: 4, T_UINT64: 8, T_INT64: 8}
MAX_DEPTH = 32


class VdfError(ValueError):
    """Malformed or unsupported binary VDF data."""


class Node:
    """One key/value pair. For maps, value is a list of Node."""
    __slots__ = ('type', 'key', 'value')

    def __init__(self, type_, key, value):
        if not isinstance(key, bytes):
            raise TypeError('key must be bytes')
        self.type = type_
        self.key = key
        self.value = value

    def __repr__(self):
        return 'Node(%#x, %r, %r)' % (self.type, self.key, self.value)


def _cstring(data, pos):
    end = data.find(b'\x00', pos)
    if end < 0:
        raise VdfError('unterminated string at offset %d' % pos)
    return data[pos:end], end + 1


def _parse_map(data, pos, depth):
    if depth > MAX_DEPTH:
        raise VdfError('nesting too deep')
    children = []
    while True:
        if pos >= len(data):
            raise VdfError('truncated data: missing map end')
        type_ = data[pos]
        pos += 1
        if type_ == T_END:
            return children, pos
        key, pos = _cstring(data, pos)
        if type_ == T_MAP:
            value, pos = _parse_map(data, pos, depth + 1)
        elif type_ == T_STRING:
            value, pos = _cstring(data, pos)
        elif type_ in _FIXED:
            size = _FIXED[type_]
            if pos + size > len(data):
                raise VdfError('truncated value for key %r' % key)
            value = data[pos:pos + size]
            pos += size
        else:
            raise VdfError('unsupported node type %#x at offset %d' % (type_, pos - 1))
        children.append(Node(type_, key, value))


def parse(data):
    """Parse a whole file into a list of top-level Nodes; verify exact round trip."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError('data must be bytes')
    data = bytes(data)
    nodes, pos = _parse_map(data, 0, 0)
    if pos != len(data):
        raise VdfError('%d trailing bytes after final map end' % (len(data) - pos))
    if serialize(nodes) != data:
        raise VdfError('data does not round-trip exactly; refusing to edit')
    return nodes


def _emit(nodes, out):
    for node in nodes:
        if b'\x00' in node.key:
            raise VdfError('key contains NUL')
        out.append(bytes([node.type]))
        out.append(node.key + b'\x00')
        if node.type == T_MAP:
            _emit(node.value, out)
            out.append(bytes([T_END]))
        elif node.type == T_STRING:
            if not isinstance(node.value, bytes) or b'\x00' in node.value:
                raise VdfError('string value for %r must be bytes without NUL' % node.key)
            out.append(node.value + b'\x00')
        elif node.type in _FIXED:
            if not isinstance(node.value, bytes) or len(node.value) != _FIXED[node.type]:
                raise VdfError('fixed-size value for %r has wrong length' % node.key)
            out.append(node.value)
        else:
            raise VdfError('cannot serialize node type %#x' % node.type)


def serialize(nodes):
    out = []
    _emit(nodes, out)
    out.append(bytes([T_END]))
    return b''.join(out)


# Convenience helpers -------------------------------------------------------

def find(nodes, key):
    """Case-insensitive lookup of the first child with this key (str or bytes)."""
    if isinstance(key, str):
        key = key.encode('utf-8')
    low = key.lower()
    for node in nodes:
        if node.key.lower() == low:
            return node
    return None


def find_all(nodes, key):
    if isinstance(key, str):
        key = key.encode('utf-8')
    low = key.lower()
    return [node for node in nodes if node.key.lower() == low]


def int32(value):
    return struct.pack('<I', value & 0xFFFFFFFF)


def uint32_of(node):
    if node is None or node.type != T_INT32:
        return None
    return struct.unpack('<I', node.value)[0]


def string(nodes, key):
    node = find(nodes, key)
    if node is None or node.type != T_STRING:
        return None
    return node.value
