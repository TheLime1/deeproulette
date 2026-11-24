"""Retain the original app process identity across the elevated helper lifetime."""
import ctypes
from ctypes import wintypes


class OwnerProcess:
    def __init__(self,pid,expected_created=None):
        self.api=ctypes.WinDLL('kernel32',use_last_error=True)
        self.api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        self.api.OpenProcess.restype=wintypes.HANDLE
        self.api.CloseHandle.argtypes=[wintypes.HANDLE]
        self.api.GetProcessTimes.argtypes=[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)]
        self.api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        self.api.WaitForSingleObject.restype=wintypes.DWORD
        self.handle=self.api.OpenProcess(0x1000|0x100000,False,pid)
        if not self.handle:raise RuntimeError('The original QA app process is unavailable.')
        times=[wintypes.FILETIME() for _ in range(4)]
        if not self.api.GetProcessTimes(self.handle,*[ctypes.byref(value) for value in times]):
            self.close();raise RuntimeError('Cannot confirm the QA app process identity.')
        self.created=(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
        if expected_created is not None and self.created!=expected_created:
            self.close();raise RuntimeError('The original QA app process has already exited.')

    def alive(self):
        return bool(self.handle and self.api.WaitForSingleObject(self.handle,0)==0x102)

    def close(self):
        if self.handle:self.api.CloseHandle(self.handle);self.handle=None
