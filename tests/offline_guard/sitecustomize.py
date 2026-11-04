"""Loaded only by tests/run_offline.py, including its Python subprocesses."""

import os
from pathlib import Path
import socket
import subprocess
import sys


def deny(*args, **kwargs):
    marker = os.environ.get("LOFIN_OFFLINE_ATTEMPTS")
    if marker:
        with open(marker, "a", encoding="utf-8") as stream:
            stream.write("Blocked network attempt\n")
    raise RuntimeError("Network disabled by offline test runner")


socket.socket.connect = deny
socket.socket.connect_ex = deny
socket.socket.sendto = deny
socket.create_connection = deny
socket.getaddrinfo = deny
_original_popen = subprocess.Popen.__init__


def guarded_popen(self, args, *positional, **kwargs):
    # Allow only this Python interpreter for the SIGKILL regression children.
    if (
        isinstance(args, (str, bytes))
        or Path(os.fspath(args[0])).resolve() != Path(sys.executable).resolve()
        or kwargs.get("shell")
    ):
        deny()
    return _original_popen(self, args, *positional, **kwargs)


subprocess.Popen.__init__ = guarded_popen
