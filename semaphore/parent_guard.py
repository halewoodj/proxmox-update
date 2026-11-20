#!/usr/bin/env python3
"""Make the Linux worker receive TERM if the Semaphore runner dies abruptly."""
import ctypes
import os
import signal
import sys

parent = int(sys.argv[1])
if os.getppid() != parent:
    sys.exit(130)
if sys.platform.startswith('linux'):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'Cannot arm worker parent-death signal')
    if os.getppid() != parent or parent == 1:
        sys.exit(130)
os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
