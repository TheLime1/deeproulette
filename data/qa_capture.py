"""Start/stop only this app's bundled passive Windows QA recorder."""
import asyncio
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def preflight(root):
    if os.name!='nt' or ctypes.sizeof(ctypes.c_void_p)!=8:
        raise RuntimeError('ROB RUN QA Live requires 64-bit Python on Windows.')
    tools=Path(root)/'outputs/roulette-qa-investigation'
    codec=tools/'network-observer/codec'
    sys.path.insert(0,str(codec))
    from game_library import find_enet
    try:enet=find_enet()
    except FileNotFoundError as error:raise RuntimeError(str(error)) from error
    explicit=os.environ.get('ROBRUN_DOTNET')
    candidates=[explicit] if explicit else [r'C:\custom\bins\dotnet\10\dotnet.exe',shutil.which('dotnet')]
    dotnet=None
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():continue
        try:
            result=subprocess.run([candidate,'--list-runtimes'],capture_output=True,text=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError,subprocess.TimeoutExpired):continue
        if any(line.startswith('Microsoft.NETCore.App 10.') for line in result.stdout.splitlines()):
            dotnet=candidate;break
    if dotnet is None:
        raise RuntimeError('Install .NET 10 or set ROBRUN_DOTNET to a dotnet.exe with the .NET 10 runtime.')
    for folder,stem in (('receiver','LiveNdisFlex'),('guid-query','QueryGuidSession'),('flush-own-session','FlushOwnSession')):
        base=tools/'network-observer'/folder
        try:
            for suffix in ('.dll','.deps.json','.runtimeconfig.json'):
                if not (base/(stem+suffix)).is_file():raise FileNotFoundError(stem+suffix)
            deps=json.loads((base/(stem+'.deps.json')).read_text(encoding='utf-8-sig'))
            for target in deps['targets'].values():
                for library in target.values():
                    for asset in library.get('runtime',{}):
                        if asset.endswith('.dll') and not (base/Path(asset).name).is_file():raise FileNotFoundError(asset)
        except (OSError,ValueError,KeyError) as error:
            raise RuntimeError('QA helper files are incomplete. Run: powershell -File outputs/roulette-qa-investigation/Build-QA.ps1') from error
    return tools,enet,Path(dotnet)


class CaptureRuntime:
    def __init__(self,root,folder):
        self.root=Path(root);self.folder=Path(folder);self.process=None;self.lock=None

    async def start(self):
        tools,enet,dotnet=preflight(self.root)
        import msvcrt
        lock_path=self.root/'work/roulette-qa/live.lock'
        lock_path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=lock_path.open('a+b')
        self.lock.seek(0);self.lock.write(b'\0');self.lock.flush();self.lock.seek(0)
        try:msvcrt.locking(self.lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            self.lock.close();self.lock=None
            raise RuntimeError('Another ROB RUN QA Live session is already running.')
        self.folder.mkdir(parents=True,exist_ok=True)
        host=tools/'qa_host.py'
        from data.qa_owner import OwnerProcess
        owner=OwnerProcess(os.getpid())
        created=owner.created
        owner.close()
        game_log=Path(os.environ['USERPROFILE'])/'AppData/LocalLow/Kefir/ROB RUN/Player.log'
        arguments=[sys.executable,'-u',str(host),'--run-dir',str(self.folder),'--owner-pid',str(os.getpid()),
                   '--owner-created',str(created),'--enet-dll',str(enet),'--dotnet',str(dotnet),'--game-log',str(game_log)]
        if ctypes.windll.shell32.IsUserAnAdmin():
            self.process=await asyncio.create_subprocess_exec(*arguments,creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            # Normal Windows consent prompt, limited to the capture host.
            def quote(value):return "'"+str(value).replace("'","''")+"'"
            command=(f"$qa=Start-Process -FilePath {quote(arguments[0])} -ArgumentList {quote(subprocess.list2cmdline(arguments[1:]))} "
                     "-Verb RunAs -WindowStyle Hidden -PassThru -Wait; exit $qa.ExitCode")
            shell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
            self.process=await asyncio.create_subprocess_exec(str(shell),'-NoProfile','-Command',command,creationflags=subprocess.CREATE_NO_WINDOW)

    def check(self):
        if (self.folder/'app-stop.request').exists():return
        for name in ('console-status.json','driver-state.json'):
            try:state=json.loads((self.folder/name).read_text(encoding='utf-8-sig'))
            except (OSError,ValueError):continue
            if state.get('state')=='error' or state.get('phase')=='error':
                raise RuntimeError(str(state.get('error') or 'QA capture failed'))
            if state.get('phase')=='stopped':
                raise RuntimeError(f"QA recorder stopped unexpectedly: {state.get('stopReason','unknown reason')}")
        if self.process and self.process.returncode is not None:
            raise RuntimeError(f'QA capture host exited ({self.process.returncode}). Check {self.folder / "host.log"}')

    async def stop(self):
        self.folder.mkdir(parents=True,exist_ok=True)
        (self.folder/'app-stop.request').touch()
        if self.process:
            try:await asyncio.wait_for(self.process.wait(),25)
            except asyncio.TimeoutError:print('Capture cleanup is still finishing; its owner lease remains enforced.',flush=True)
        if self.lock:
            self.lock.close();self.lock=None
