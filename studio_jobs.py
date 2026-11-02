"""Shared local jobs, durable diagnostics and cancellable process execution."""
from pathlib import Path
import datetime,hashlib,json,os,subprocess,sys,time,threading,traceback
from recovery_logging import RunLogger,DEFAULT_VIDEO_ROOT,error_details
from recovery import Cancelled

BASE=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent
def save_json(path,value):
    path=Path(path);pending=path.with_suffix(path.suffix+'.pending')
    pending.write_text(json.dumps(value,indent=2,ensure_ascii=False,default=str),encoding='utf-8');pending.replace(path)
def sha256(path,check=lambda:None):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while True:
            check();block=stream.read(4*1024*1024)
            if not block:break
            digest.update(block)
    return digest.hexdigest()
class Job:
    def __init__(self,kind,parent=DEFAULT_VIDEO_ROOT,status=None,cancel=None):
        parent=Path(parent)
        if parent.drive.upper()!='D:':raise ValueError('Save recoveries to D:.')
        parent.mkdir(parents=True,exist_ok=True)
        self.dest=parent/(kind+'_'+datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f'))
        self.log=RunLogger(DEFAULT_VIDEO_ROOT/'Logs'/self.dest.name,kind=kind,destination=str(self.dest))
        self.dest.mkdir(exist_ok=False);self.status=status or (lambda *args:None);self.cancel=cancel or threading.Event()
        self.report={'kind':kind,'status':'running','complete':False,'destination':str(self.dest),'log_directory':str(self.log.directory),'failures':[],'source_modified':False}
        self.save()
    def check(self):
        if self.cancel.is_set():raise Cancelled('Stopped. Copied files and detailed logs remain saved.')
    def save(self):
        self.report['log_directory']=str(self.log.directory)
        save_json(self.dest/'recovery_report.json',self.report)
    def failure(self,stage,error,**fields):
        self.report['failures'].append({'stage':stage,'error':error_details(error),**fields})
        self.log.exception(stage,error,**fields);self.save()
    def run(self,operation):
        try:
            operation(self);complete=not self.report['failures'] and not self.report.get('incomplete_reasons');self.report.update(status='complete' if complete else 'partial',complete=complete)
            self.save();self.log.close(self.report['status']);self.save();return self.report
        except BaseException as error:
            self.report.update(status='stopped' if isinstance(error,Cancelled) else 'failed',complete=False)
            self.failure('job',error);self.log.close(self.report['status']);self.save();raise
    def command(self,args,label,timeout=None,expected_seconds=None,env=None,graceful=False,cwd=None):
        logs=self.dest/'logs';logs.mkdir(exist_ok=True)
        stem=logs/(label+'_'+str(time.time_ns()));out=stem.with_suffix('.stdout.log');err=stem.with_suffix('.stderr.log')
        record={'argv':[str(a) for a in args],'stdout':str(out),'stderr':str(err)};began=time.monotonic();process=None
        progress=stem.with_suffix('.progress.txt');argv=record['argv'][:]
        if Path(argv[0]).name.lower()=='ffmpeg.exe':argv[1:1]=['-progress',str(progress),'-stats_period','1']
        record['argv']=argv
        self.log('INFO','command_started',**record)
        try:
            with out.open('wb') as output,err.open('wb') as errors:
                process=subprocess.Popen(argv,stdin=subprocess.PIPE if graceful else subprocess.DEVNULL,stdout=output,stderr=errors,cwd=cwd or self.dest,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                last=0;stop_sent=None
                while process.poll() is None:
                    if self.cancel.is_set():
                        if not graceful:raise Cancelled('Stopped. Intermediate files remain saved.')
                        if stop_sent is None:
                            process.stdin.write(b'q\n');process.stdin.flush();stop_sent=time.monotonic()
                        elif time.monotonic()-stop_sent>15:raise Cancelled('The capture did not stop promptly. Its partial archive remains saved.')
                    elapsed=time.monotonic()-began
                    if timeout is not None and elapsed>timeout:raise TimeoutError(f'{label} exceeded {timeout}s. See {err}')
                    if elapsed-last>=1:
                        last=elapsed;done=None
                        if progress.exists():
                            try:
                                tail=progress.read_text(errors='replace')[-8192:]
                                values=[line.split('=',1)[1] for line in tail.splitlines() if line.startswith('out_time_us=')]
                                if values:done=float(values[-1])/1e6
                            except (OSError,ValueError):pass
                        self.status(f'{label} · {int(elapsed)}s elapsed',{'phase':label,'done':done,'total':expected_seconds,'overall':min(95,95*done/expected_seconds) if done is not None and expected_seconds else 0})
                    time.sleep(.1)
            record['returncode']=process.returncode
        except BaseException as error:
            record['error']=error_details(error);record['traceback']=traceback.format_exc();raise
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            record['elapsed_seconds']=round(time.monotonic()-began,3)
            if process is not None:record['returncode']=process.returncode
            save_json(stem.with_suffix('.command.json'),record)
            self.log('ERROR' if record.get('error') or record.get('returncode') else 'INFO','command_finished',**record)
        record['stdout_text']=out.read_text(encoding='utf-8',errors='replace');record['stderr_text']=err.read_text(encoding='utf-8',errors='replace')
        return record
