"""Size-bounded local diagnostics; a blocked log must not stall packet decoding.

Only exact sibling filenames owned by this writer are rotated. No recursion or
glob deletion. Runtime behavior is untested in this offline preparation.
"""
import os
from pathlib import Path


class BoundedJsonl:
    def __init__(self,path,*,max_bytes=2*1024*1024,keep=4):
        self.path=Path(path).absolute();self.root=self.path.parent.resolve()
        if not 4096<=max_bytes<=16*1024*1024 or not 1<=keep<=8:raise ValueError('Invalid diagnostic retention bounds')
        self.max_bytes=max_bytes;self.keep=keep;self.handle=None;self.size=0
        self.dropped_records=0;self.rotations=0;self.last_error=None
        self.needs_rotate=False

    def _owned(self,index=0):
        path=self.path if index==0 else self.path.with_name(self.path.name+f'.{index}')
        if path.is_symlink() or path.resolve().parent!=self.root:raise ValueError('Diagnostic path is not an owned sibling file')
        return path

    def _open(self):
        for index in range(self.keep):
            path=self._owned(index)
            if path.exists() and path.stat().st_size>self.max_bytes:
                raise OSError('Existing diagnostic sibling exceeds bounds; archive it before resuming this log')
        self.size=self.path.stat().st_size if self.path.exists() else 0
        self.handle=self.path.open('ab',buffering=0)

    def _close(self):
        if self.handle is not None:self.handle.close();self.handle=None

    def _rotate(self):
        self._close()
        for index in range(self.keep-1,0,-1):
            target=self._owned(index);source=self._owned(index-1)
            if source.exists():os.replace(source,target)
        if self.keep==1:self._owned().unlink(missing_ok=True)
        self.rotations+=1;self._open()

    def write(self,text):
        data=text.encode('utf-8')
        if len(data)>self.max_bytes:
            self.dropped_records+=1;self.last_error='record_exceeds_retention_limit';return False
        try:
            if self.handle is None:self._open()
            if self.needs_rotate or self.size+len(data)>self.max_bytes:
                self._rotate();self.needs_rotate=False
            if self.handle.write(data)!=len(data):raise OSError('Short diagnostic write')
            self.size+=len(data);self.last_error=None;return True
        except OSError as error:
            self.dropped_records+=1;self.last_error=str(error)[:180]
            self.needs_rotate=True
            try:self._close()
            except OSError:self.handle=None
            return False

    def flush(self):
        # Unbuffered writes already leave Python. No fsync on the packet path.
        return None

    def __enter__(self):return self

    def __exit__(self,*_):self._close()
