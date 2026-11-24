"""Owned Windows capture host; the interactive app handles display and learning."""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import signal
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--owner-pid',type=int,required=True)
    parser.add_argument('--owner-created',type=int,required=True)
    parser.add_argument('--game-log',required=True)
    parser.add_argument('--enet-dll',required=True)
    parser.add_argument('--dotnet',required=True)
    args=parser.parse_args()
    os.environ['ROBRUN_ENET_DLL']=args.enet_dll
    os.environ['ROBRUN_DOTNET']=args.dotnet
    os.environ['ROBRUN_GAME_LOG']=args.game_log
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    from data.qa_owner import OwnerProcess
    from robrun_realtime_runtime import run, write_json
    folder=args.run_dir.resolve()
    work=Path(__file__).resolve().parents[2]/'work'
    if not folder.is_relative_to(work):
        raise ValueError('Capture output must stay inside this fork work directory')
    folder.mkdir(parents=True,exist_ok=True)
    stop=folder/'app-stop.request'
    def signal_stop(*_): stop.touch()
    signal.signal(signal.SIGINT,signal_stop)
    signal.signal(signal.SIGTERM,signal_stop)
    owner=OwnerProcess(args.owner_pid,args.owner_created)
    def status(output,state,**fields):
        write_json(output/'console-status.json',{'pid':os.getpid(),'state':state,'utc':dt.datetime.now(dt.timezone.utc).isoformat(),**fields})
    with (folder/'host.log').open('a',encoding='utf-8',buffering=1) as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        try:
            result=run(folder,poll_keys=lambda:None,should_stop=lambda:stop.exists() or not owner.alive(),
                       should_reload=lambda:False,signature=lambda:0,on_event=lambda _:None,
                       line=lambda text:print(text,flush=True),status=status)
            return 0 if result=='stop' else 1
        except BaseException as error:
            (folder/'realtime-stop.request').touch()
            status(folder,'error',error=str(error))
            raise
        finally:owner.close()


if __name__=='__main__':
    raise SystemExit(main())
