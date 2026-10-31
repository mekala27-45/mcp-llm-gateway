import subprocess
import sys

PROGRAM = """
import os
import subprocess
import sys

from customer_server.guard import claim_stdio

wire = claim_stdio()
print("stray print")
os.write(1, b"stray write\\n")
subprocess.run([sys.executable, "-c", "print('stray child')"], check=True)
print("stray read saw", os.read(0, 100))
os.write(wire.stdout, b"protocol saw " + os.read(wire.stdin, 100) + b"\\n")
"""


def test_claim_moves_stray_output_to_stderr_and_keeps_the_wire() -> None:
    finished = subprocess.run(
        [sys.executable, "-c", PROGRAM],
        input=b"request",
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert finished.stdout == b"protocol saw request\n"
    assert b"stray print" in finished.stderr
    assert b"stray write" in finished.stderr
    assert b"stray child" in finished.stderr
    assert b"stray read saw b''" in finished.stderr
