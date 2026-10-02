"""Whether a file is a compiled program, judged by its first bytes.

A program is never worth carrying between machines: it is built for one
platform and one version, the installer that put it there puts a newer one
there tomorrow, and a copy from another machine can only be useless (a Windows
`codex.exe` on a Mac) or harmful (an older `codex.exe` over a newer one). Codex
keeps several such files inside `.codex` -- `plugins/.plugin-appserver/`
held 416 MB of them on the machine this was found on -- so `sync` and the
`.codex` copies leave every one out, whatever the config says (D-021).

The test is the file's header, not its name: on macOS and Linux a program
usually has no extension at all. Recognised are Windows PE (``MZ`` plus the
``PE`` signature it points to, so a text file that merely starts with "MZ" is
not caught), ELF, and Mach-O in all its byte orders, including universal
binaries. A file that cannot be read is not called a program: whatever reads
it next reports the failure.
"""
from __future__ import annotations

from pathlib import Path

_ELF = b"\x7fELF"
_MACH_O = frozenset({
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",   # 32-bit
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",   # 64-bit
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",   # universal (also a Java class file)
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",   # universal, 64-bit offsets
})
_PE_HEADER_OFFSET = 0x3C
#: Where a real PE header can be; anything further is not an executable header.
_PE_HEADER_LIMIT = 64 * 1024


def is_native_program(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(_PE_HEADER_OFFSET + 4)
            if head[:4] == _ELF or head[:4] in _MACH_O:
                return True
            if head[:2] != b"MZ" or len(head) < _PE_HEADER_OFFSET + 4:
                return False
            offset = int.from_bytes(head[_PE_HEADER_OFFSET:_PE_HEADER_OFFSET + 4], "little")
            if offset < _PE_HEADER_OFFSET + 4 or offset > _PE_HEADER_LIMIT:
                return False
            handle.seek(offset)
            return handle.read(4) == b"PE\0\0"
    except OSError:
        return False
