"""Non-destructive process checks for restart/restore hand-off."""
import os,sys,ctypes


def process_alive(pid):
    if sys.platform=='win32':
        from ctypes import wintypes as W
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];kernel.OpenProcess.restype=W.HANDLE
        kernel.GetExitCodeProcess.argtypes=[W.HANDLE,ctypes.POINTER(W.DWORD)];kernel.GetExitCodeProcess.restype=W.BOOL
        kernel.CloseHandle.argtypes=[W.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return ctypes.get_last_error()!=87
        try:
            status=W.DWORD()
            return not kernel.GetExitCodeProcess(handle,ctypes.byref(status)) or status.value==259
        finally:kernel.CloseHandle(handle)
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False
    except PermissionError:return True
