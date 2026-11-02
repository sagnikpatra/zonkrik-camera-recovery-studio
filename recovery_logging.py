"""Local, durable diagnostic logs for every recovery stage."""
from __future__ import annotations
import datetime,json,os,sys,threading,traceback,uuid
from pathlib import Path
from collections import Counter

DEFAULT_VIDEO_ROOT=Path(r'D:\Downloads\Handycam Video Recovery')
EMERGENCY_LOG_ROOT=Path(r'E:\Projects\Claude App\work\emergency-logs')

class LoggingFailure(RuntimeError):
    """Neither the requested directory nor the local emergency log is writable."""

def timestamp():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5,minutes=30))).isoformat(timespec='milliseconds')

def error_details(error):
    details={'type':type(error).__name__,'message':str(error)}
    for name in ('winerror','errno','sense_key','asc','ascq','sense_hex','cdb_hex','scsi_status','requested_bytes','transferred_bytes','transferred_bytes_reliable','duration_seconds','lba','sector_count','filename','filename2','returncode','cmd','stdout','stderr'):
        value=getattr(error,name,None)
        if value is not None:details[name]=value
    return details

def _default(value):
    if isinstance(value,Path):return str(value)
    if isinstance(value,bytes):return value.hex()
    if isinstance(value,Exception):return error_details(value)
    return repr(value)

class RunLogger:
    def __init__(self,directory,**context):
        self.directory=Path(directory);self.primary_directory=self.directory
        self.context=context;self.lock=threading.RLock();self.counts=Counter();self.sequence=0
        self.fallback_active=False;self.primary_failure=None;self.previous_log_directories=[]
        self._paths()
        try:self.directory.mkdir(parents=True,exist_ok=True)
        except OSError as error:self._fallback(error,'create_log_directory')
        self.event('INFO','session_started',**context)
    def __call__(self,level,event,**fields):return self.event(level,event,**fields)

    def _paths(self):
        self.events_path=self.directory/'events.jsonl';self.text_path=self.directory/'session.log'

    @staticmethod
    def _level(level):
        value=str(level or 'INFO').upper().strip()
        return {'WARN':'WARNING','FATAL':'CRITICAL'}.get(value,value)

    def _record(self,level,event,fields):
        self.sequence+=1;self.counts[level]+=1
        return {**self.context,**fields,'time':timestamp(),'sequence':self.sequence,'level':level,
                'event':event,'record_id':uuid.uuid4().hex}

    def _write_record(self,record,skip_json=False):
        if not skip_json:
            line=json.dumps(record,ensure_ascii=False,default=_default)
            with self.events_path.open('a',encoding='utf-8') as stream:
                stream.write(line+'\n');stream.flush()
                if record['level'] in ('ERROR','CRITICAL'):os.fsync(stream.fileno())
        with self.text_path.open('a',encoding='utf-8') as stream:
            stream.write(f"{record['time']} [{record['level']}] {record['event']}: "+json.dumps(record,ensure_ascii=False,default=_default)+'\n')
            stream.flush()
            if record['level'] in ('ERROR','CRITICAL'):os.fsync(stream.fileno())

    def _logging_failure(self,error,operation):
        primary=json.dumps(self.primary_failure or {},ensure_ascii=False,default=_default)
        raise LoggingFailure(f'Cannot save recovery diagnostics. Primary log directory {self.primary_directory} failed ({primary}); emergency log directory {self.directory} also failed during {operation}: {error}. Free space or restore write access before continuing.') from error

    def _fallback(self,error,operation):
        if self.fallback_active:self._logging_failure(error,operation)
        previous=self.directory
        self.primary_failure=error_details(error)
        self.previous_log_directories.append(str(previous))
        self.directory=EMERGENCY_LOG_ROOT/(previous.name+'_'+datetime.datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8])
        self.fallback_active=True;self._paths()
        copied_ids=set();history_errors=[];copied_events=0
        try:
            self.directory.mkdir(parents=True,exist_ok=False)
            # A full disk can leave the final JSONL line incomplete. Recover
            # individually valid records, never propagate malformed JSONL into
            # the new diagnostics directory.
            old_events=previous/'events.jsonl'
            try:
                if old_events.is_file():
                    with old_events.open(encoding='utf-8',errors='replace') as source,self.events_path.open('w',encoding='utf-8') as dest:
                        for line_number,line in enumerate(source,1):
                            if not line.strip():continue
                            try:record=json.loads(line)
                            except ValueError:
                                history_errors.append({'file':str(old_events),'line':line_number,'error':'Incomplete or invalid previous JSONL record'})
                                continue
                            dest.write(json.dumps(record,ensure_ascii=False,default=_default)+'\n');copied_events+=1
                            if isinstance(record,dict) and record.get('record_id'):copied_ids.add(record['record_id'])
                        dest.flush();os.fsync(dest.fileno())
            except OSError as history_error:
                history_errors.append({'file':str(old_events),'error':error_details(history_error)})
            old_text=previous/'session.log'
            try:
                if old_text.is_file():
                    with old_text.open(encoding='utf-8',errors='replace') as source,self.text_path.open('w',encoding='utf-8') as dest:
                        for chunk in iter(lambda:source.read(1024*1024),''):dest.write(chunk)
                        dest.flush()
            except OSError as history_error:
                history_errors.append({'file':str(old_text),'error':error_details(history_error)})
            marker=self._record('WARNING','logging_fallback_activated',{'operation':operation,
                'primary_directory':str(self.primary_directory),'previous_directory':str(previous),
                'emergency_directory':str(self.directory),'error':error_details(error),
                'copied_events':copied_events,'history_recovery_errors':history_errors})
            self._write_record(marker)
        except OSError as fallback_error:self._logging_failure(fallback_error,operation)
        return copied_ids

    def event(self,level,event,**fields):
        with self.lock:
            level=self._level(level)
            record=self._record(level,event,fields)
            try:self._write_record(record)
            except OSError as error:
                copied=self._fallback(error,'append_event')
                if record['record_id'] not in copied:
                    self.sequence+=1;record['sequence']=self.sequence
                try:self._write_record(record,skip_json=record['record_id'] in copied)
                except OSError as fallback_error:self._logging_failure(fallback_error,'append_event')
            return record
    def exception(self,stage,error,**fields):
        return self.event('ERROR','operation_failed',stage=stage,error=error_details(error),traceback=''.join(traceback.format_exception(type(error),error,error.__traceback__)),**fields)
    def close(self,status,**fields):
        with self.lock:
            self.event('INFO','session_finished',status=status,**fields)
            def summary():
                data={**self.context,**fields,'status':status,'counts':dict(self.counts),
                      'events':str(self.events_path),'human_log':str(self.text_path),
                      'primary_directory':str(self.primary_directory),'fallback_active':self.fallback_active,
                      'previous_log_directories':self.previous_log_directories}
                tmp=self.directory/'summary.json.tmp'
                with tmp.open('w',encoding='utf-8') as stream:
                    stream.write(json.dumps(data,indent=2,default=_default));stream.flush();os.fsync(stream.fileno())
                tmp.replace(self.directory/'summary.json')
                return data
            try:return summary()
            except OSError as error:
                self._fallback(error,'write_summary')
                try:return summary()
                except OSError as fallback_error:self._logging_failure(fallback_error,'write_summary')

def application_logger():
    return RunLogger(DEFAULT_VIDEO_ROOT/'Logs'/('application_'+datetime.datetime.now().strftime('%Y-%m-%d')),application='ZONKRIK Handycam Recovery')

def install_exception_hooks(logger):
    def sys_hook(kind,value,tb):
        value=value.with_traceback(tb);logger.exception('unhandled_main_thread',value)
    def thread_hook(args):logger.exception('unhandled_background_thread',args.exc_value.with_traceback(args.exc_traceback),thread=args.thread.name if args.thread else None)
    sys.excepthook=sys_hook;threading.excepthook=thread_hook
