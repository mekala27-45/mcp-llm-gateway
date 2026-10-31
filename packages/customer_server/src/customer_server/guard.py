"""Keeps the stdio protocol stream clean at the file descriptor level."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Wire:
    """Private descriptors that carry the protocol once claim_stdio() has run."""

    stdin: int
    stdout: int


def claim_stdio() -> Wire:
    """Move the protocol off descriptors 0 and 1 and point the originals somewhere harmless.

    Call this before importing anything that might print.
    """
    # os.dup gives descriptors above 2 that child processes do not inherit.
    wire = Wire(stdin=os.dup(0), stdout=os.dup(1))
    # From here on a print, an os.write(1, ...), a C extension writing to stdout,
    # or a child process that inherits descriptor 1 all land on stderr.
    os.dup2(2, 1)
    # Stray reads from descriptor 0 see end of file instead of eating protocol bytes.
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    sys.stdout = sys.stderr
    return wire
