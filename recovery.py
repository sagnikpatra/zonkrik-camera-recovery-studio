"""ZONKRIK read-only DVD recovery. Only inquiry/read SCSI commands are permitted."""
import ctypes, json, os, shutil, subprocess, struct, threading, sys, time
from ctypes import wintypes
from pathlib import Path

class Cancelled(Exception): pass
class RecoveryError(Exception): pass
class ReadError(RecoveryError):
    """A specific unreadable sector, rather than a missing disc or USB device."""
class DiscUnavailable(RecoveryError): pass
class DriveUnavailable(RecoveryError): pass
class DiscChanged(RecoveryError): pass
class DiscNotReady(DiscUnavailable): pass
class TransferSizeError(RecoveryError): pass
class TransientReadError(ReadError): pass

def sense_error(sense):
    """Decode both fixed and descriptor SCSI sense formats."""
    form=sense[0]&0x7f if sense else 0
    if form in (0x72,0x73) and len(sense)>=4:
        key,asc,ascq=sense[1]&15,sense[2],sense[3]
    elif form in (0x70,0x71) and len(sense)>=14:
        key,asc,ascq=sense[2]&15,sense[12],sense[13]
    else:
        return RecoveryError('The DVD writer returned an unreadable status. Reconnect its USB cable and try again.')
    if key==2 and asc==0x3a:
        text=('The DVD tray is open. Close it with the disc securely seated.' if ascq==2 else
              'The DVD tray is closed, but the drive cannot detect this disc. Reseat it securely on the centre hub and close the tray. If it spins and stops without being detected, compare with a previously readable disc using the same connection.' if ascq==1 else
              'The DVD writer is connected, but it cannot detect a readable disc. Reseat the disc securely on the centre hub and close the tray.')
        error=DiscUnavailable(text)
    elif key==2:
        error=DiscNotReady('The DVD writer is still loading the disc. Keep the tray closed and wait for it to finish spinning.')
    elif key==6:
        error=DiscChanged('The disc changed or the DVD writer reset. The partial copy is preserved. Start recovery again after the disc finishes loading.')
    elif key==0x0b:
        error=TransientReadError('The DVD drive aborted a read temporarily; recovery will retry it.')
    elif key==3:
        error=ReadError('A recorded sector could not be read from the disc.')
    elif key==5 and asc==0x21:
        error=ReadError('The DVD writer cannot access this recorded sector.')
    else:
        error=RecoveryError(f'The DVD writer could not complete the read (status {key:02X}/{asc:02X}/{ascq:02X}). Try reconnecting the writer or another disc.')
    error.sense_key=key;error.asc=asc;error.ascq=ascq;error.sense_hex=sense.hex()
    return error
class SPTD(ctypes.Structure):
    _fields_ = [('Length',ctypes.c_ushort),('ScsiStatus',ctypes.c_ubyte),('PathId',ctypes.c_ubyte),('TargetId',ctypes.c_ubyte),('Lun',ctypes.c_ubyte),('CdbLength',ctypes.c_ubyte),('SenseInfoLength',ctypes.c_ubyte),('DataIn',ctypes.c_ubyte),('DataTransferLength',ctypes.c_ulong),('TimeOutValue',ctypes.c_ulong),('DataBuffer',ctypes.c_void_p),('SenseInfoOffset',ctypes.c_ulong),('Cdb',ctypes.c_ubyte*16)]
class Packet(ctypes.Structure):
    _fields_ = [('s',SPTD),('sense',ctypes.c_ubyte*32)]
k = ctypes.WinDLL('kernel32',use_last_error=True)
k.CreateFileW.restype = wintypes.HANDLE
k.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
k.DeviceIoControl.argtypes = [wintypes.HANDLE,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p]
k.CloseHandle.argtypes = [wintypes.HANDLE]
k.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
k.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR,wintypes.LPWSTR,wintypes.DWORD]

def dos_device_target(name):
    buffer=ctypes.create_unicode_buffer(4096)
    return buffer.value if k.QueryDosDeviceW(name,buffer,len(buffer)) else None

def raw_optical_names():
    buffer=ctypes.create_unicode_buffer(65536)
    length=k.QueryDosDeviceW(None,buffer,len(buffer))
    if not length:return []
    return sorted(name for name in buffer[:length].split('\0')
                  if name.lower().startswith('cdrom') and name[5:].isdigit()
                  and (dos_device_target(name) or '').lower()=='\\device\\'+name.lower())

def video_directory(drive):
    # Raw optical devices do not have a filesystem path. Do not resolve them
    # as folders relative to the app's working directory.
    return Path(drive+'\\VIDEO_TS') if len(drive)==2 and drive[1]==':' else None

def mounted_video_directory(drive):
    path=video_directory(drive)
    if path is not None:
        try:
            if path.is_dir():return path
        except OSError:
            # An unfinished DVD may reject filesystem calls with WinError 1,
            # rather than simply returning "path not found". Raw tracks can
            # still be completely readable, so continue with sector recovery.
            pass
    return None

def optical_drives():
    letters=[chr(c)+':' for c in range(65,91) if k.GetDriveTypeW(chr(c)+':\\') == 5]
    mapped={(dos_device_target(letter) or '').lower() for letter in letters}
    raw=[name for name in raw_optical_names()
         if (dos_device_target(name) or '').lower() not in mapped]
    return letters+raw

class Disc:
    def __init__(self, drive, logger=None):
        self.drive=drive;self.logger=logger
        if drive not in optical_drives(): raise DriveUnavailable('The DVD writer is disconnected. Connect it directly to a USB port on the PC.')
        # Windows SCSI pass-through requires this access mask even for read commands.
        self.h=k.CreateFileW('\\\\.\\'+drive,0xC0000000,3,None,3,0,None)
        if self.h == ctypes.c_void_p(-1).value:
            winerror=ctypes.get_last_error();self.h=None
            failure=DriveUnavailable('Cannot access the DVD writer. Reconnect its USB cable. '+ctypes.FormatError(winerror));failure.winerror=winerror
            raise failure
        self.max_read_sectors=32;self.alignment_mask=4095
        self.query_transfer_limits()
    def query_transfer_limits(self):
        query=ctypes.create_string_buffer(struct.pack('<III',1,0,0))
        descriptor=ctypes.create_string_buffer(128);returned=wintypes.DWORD()
        ok=k.DeviceIoControl(self.h,0x2d1400,query,len(query),descriptor,len(descriptor),ctypes.byref(returned),None)
        if ok and returned.value>=20:
            maximum,pages,mask=struct.unpack_from('<III',descriptor,8)
            limits=[128*1024]
            if maximum:limits.append(maximum)
            if pages:limits.append(max(1,pages-1)*4096)
            self.max_read_sectors=max(1,min(limits)//2048)
            if mask<=65535 and not mask&(mask+1):self.alignment_mask=max(mask,4095)
    def close(self):
        if self.h is not None:k.CloseHandle(self.h);self.h=None
    def command(self,cdb,size):
        if cdb[0] not in (0x00,0x28,0x51,0x52,0x46):raise RecoveryError('Non-read SCSI command blocked.')
        mask=getattr(self,'alignment_mask',4095)
        p=Packet();b=ctypes.create_string_buffer(max(1,size)+mask)
        address=(ctypes.addressof(b)+mask)&~mask
        p.s.Length=ctypes.sizeof(SPTD);p.s.CdbLength=len(cdb);p.s.SenseInfoLength=32;p.s.DataIn=1;p.s.DataTransferLength=size;p.s.TimeOutValue=10;p.s.DataBuffer=address;p.s.SenseInfoOffset=Packet.sense.offset
        for i,x in enumerate(cdb):p.s.Cdb[i]=x
        n=wintypes.DWORD();started=time.monotonic()
        ok=k.DeviceIoControl(self.h,0x4D014,ctypes.byref(p),ctypes.sizeof(p),ctypes.byref(p),ctypes.sizeof(p),ctypes.byref(n),None)
        failure=None
        if not ok:
            winerror=ctypes.get_last_error()
            if winerror in (6,55,433,1167):failure=DriveUnavailable('The USB DVD writer disconnected. Copied data is preserved.')
            elif winerror in (21,1112):failure=DiscUnavailable('The DVD writer cannot detect a readable disc. Copied data is preserved.')
            elif winerror==23:failure=ReadError('A recorded sector failed its checksum.')
            elif winerror==87 and cdb[0]==0x28:failure=TransferSizeError('The USB connection requires smaller read transfers.')
            elif winerror in (121,1117,1460):failure=TransientReadError('The DVD read timed out or had a temporary I/O error: '+ctypes.FormatError(winerror))
            else:failure=RecoveryError('Windows could not read from the DVD writer: '+ctypes.FormatError(winerror))
            failure.winerror=winerror
        elif p.s.ScsiStatus:failure=sense_error(bytes(p.sense))
        if failure is not None:
            failure.cdb_hex=bytes(cdb).hex();failure.scsi_status=int(p.s.ScsiStatus)
            failure.requested_bytes=size;failure.transferred_bytes=int(p.s.DataTransferLength) if ok else 0;failure.transferred_bytes_reliable=bool(ok)
            failure.duration_seconds=round(time.monotonic()-started,3)
            if cdb[0]==0x28:
                failure.lba=int.from_bytes(bytes(cdb[2:6]),'big');failure.sector_count=int.from_bytes(bytes(cdb[7:9]),'big')
            if getattr(self,'logger',None):
                from recovery_logging import error_details
                self.logger('ERROR','scsi_command_failed',drive=getattr(self,'drive',None),error=error_details(failure))
            raise failure
        return ctypes.string_at(address,min(size,p.s.DataTransferLength))
    def wait_ready(self,status=None,cancel=None,timeout=35,interval=1):
        cancel=cancel if cancel is not None else threading.Event()
        deadline=time.monotonic()+timeout
        while True:
            if cancel.is_set():raise Cancelled('Stopped while waiting for the disc. No files were changed.')
            try:
                self.command([0x00,0,0,0,0,0],0)
                return
            except (DiscUnavailable,DiscChanged) as error:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise error
                if status:
                    state='Loading the disc' if isinstance(error,(DiscNotReady,DiscChanged)) else 'Waiting for a readable disc'
                    status(f'{state} · {remaining:.0f}s remaining. Stop is available.',0)
                if cancel.wait(min(interval,remaining)):raise Cancelled('Stopped while waiting for the disc. No files were changed.')
    def sectors(self,start,count):
        if not 0<count<=65535 or start<0:raise RecoveryError('Invalid optical sector range.')
        result=[];position=0
        while position<count:
            amount=min(count-position,getattr(self,'max_read_sectors',32))
            try:
                b=self.command([0x28,0]+list((start+position).to_bytes(4,'big'))+[0]+list(amount.to_bytes(2,'big'))+[0],amount*2048)
            except TransferSizeError:
                if amount<=1:raise
                self.max_read_sectors=max(1,amount//2);continue
            if len(b)!=amount*2048:
                failure=ReadError('Short sector read');failure.lba=start+position;failure.sector_count=amount;failure.requested_bytes=amount*2048;failure.transferred_bytes=len(b);failure.transferred_bytes_reliable=True
                failure.cdb_hex=bytes([0x28,0]+list((start+position).to_bytes(4,'big'))+[0]+list(amount.to_bytes(2,'big'))+[0]).hex()
                if getattr(self,'logger',None):
                    from recovery_logging import error_details
                    self.logger('ERROR','short_sector_read',error=error_details(failure))
                raise failure
            result.append(b);position+=amount
        return b''.join(result)
    def tracks(self):
        info=self.command([0x51,0,0,0,0,0,0,1,0,0],256)
        if len(info)<12:raise RecoveryError('Disc information is unavailable.')
        first=info[3];last=info[6] | ((info[11]<<8) if len(info)>11 else 0)
        if not 0<last<=256:raise RecoveryError('Unsupported disc track layout.')
        tracks=[]
        for t in range(first,last+1):
            b=self.command([0x52,1]+list(t.to_bytes(4,'big'))+[0,1,0,0],256)
            if len(b)<32 or b[6]&0x40:continue # blank/reserved track
            start=int.from_bytes(b[8:12],'big');count=int.from_bytes(b[24:28],'big')
            if b[7]&2:count=int.from_bytes(b[28:32],'big')-start+1
            if count>0:tracks.append({'track':t,'start':start,'count':count})
        return tracks

def probe_disc(drive):
    from recovery_logging import error_details
    disc=None
    try:
        disc=Disc(drive);disc.command([0x00,0,0,0,0,0],0)
        return {'state':'ready','message':'Disc detected. Ready to inspect or recover recordings.'}
    except DiscNotReady as e:return {'state':'loading','message':str(e),'details':error_details(e)}
    except DiscChanged as e:return {'state':'loading','message':'Disc inserted or writer reset. Waiting for it to finish loading.','details':error_details(e)}
    except DiscUnavailable as e:return {'state':'empty','message':str(e),'details':error_details(e)}
    except DriveUnavailable as e:return {'state':'disconnected','message':str(e),'details':error_details(e)}
    except RecoveryError as e:return {'state':'attention','message':str(e),'details':error_details(e)}
    finally:
        if disc:disc.close()

def parse_ifo(b):
    if not b.startswith(b'DVDVIDEO-VTS'):raise RecoveryError('Missing DVD title metadata.')
    pgcit=int.from_bytes(b[0xcc:0xd0],'big')*2048
    if pgcit+16>len(b):raise RecoveryError('Incomplete DVD title metadata.')
    count=int.from_bytes(b[pgcit:pgcit+2],'big')
    if count<1:raise RecoveryError('DVD metadata contains no recordings.')
    # First title PGC, as used by this Handycam. Refuse silent omission of other PGCs.
    if count!=1:raise RecoveryError('Multiple DVD title chains found; raw backup is preserved. This layout needs additional recovery support.')
    p=pgcit+int.from_bytes(b[pgcit+12:pgcit+16],'big')
    if p+236>len(b):raise RecoveryError('Invalid DVD program table.')
    programs,cells=b[p+2:p+4];pm=p+int.from_bytes(b[p+230:p+232],'big');cp=p+int.from_bytes(b[p+232:p+234],'big')
    if not programs or cp+cells*24>len(b) or pm+programs>len(b):raise RecoveryError('Incomplete DVD cell table.')
    starts=list(b[pm:pm+programs]);table=[]
    for i in range(cells):
        off=cp+i*24;first=int.from_bytes(b[off+8:off+12],'big');last=int.from_bytes(b[off+20:off+24],'big')
        if last<first:raise RecoveryError('Invalid DVD cell range.')
        table.append((first,last))
    if starts!=sorted(set(starts)) or not all(1<=s<=cells for s in starts):raise RecoveryError('Invalid DVD recording index.')
    clips=[]
    for i,c in enumerate(starts):
        clips.append((table[c-1][0],table[(starts[i+1]-2) if i+1<programs else -1][1]))
    return {'clips':clips,'video_offset':int.from_bytes(b[0xc4:0xc8],'big'),'cells':cells}

class LegacyRecovery:
    def __init__(self,drive,destination,status,cancel):
        self.drive=drive;self.dest=Path(destination);self.status=status;self.cancel=cancel;self.bad=[]
        base=Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
        self.ffmpeg=base/'ffmpeg.exe';self.ffprobe=base/'ffprobe.exe'
    def check(self):
        if self.cancel.is_set():raise Cancelled('Stopped. Partial copies remain in the recovery folder.')
    def dump(self,disc,start,count,target):
        partial=target.with_suffix(target.suffix+'.partial')
        with partial.open('wb') as f:
            for pos in range(0,count,64):
                self.check();n=min(64,count-pos)
                try:data=disc.sectors(start+pos,n)
                except ReadError:
                    chunks=[]
                    for i in range(n):
                        self.check()
                        try:d=disc.sectors(start+pos+i,1)
                        except ReadError:self.bad.append(start+pos+i);d=bytes(2048)
                        chunks.append(d)
                    data=b''.join(chunks)
                f.write(data)
                if pos%1024==0:self.status(f'Copying {target.name} · {(pos+n)*100/count:.0f}% · unreadable sectors: {len(self.bad)}',(pos+n)*70/count)
        partial.rename(target)
    def run_ffmpeg(self,args,log):
        self.check()
        with log.open('w',encoding='utf-8') as f:
            p=subprocess.Popen([str(self.ffmpeg),'-hide_banner','-nostdin','-y','-v','warning']+args,stdout=f,stderr=f,creationflags=subprocess.CREATE_NO_WINDOW)
            while p.poll() is None:
                if self.cancel.wait(.2):p.terminate();p.wait();raise Cancelled('Stopped. Completed copies remain saved.')
        if p.returncode:raise RecoveryError('Video conversion failed. See '+str(log))
    def convert(self,sources):
        dest=self.dest/'MP4 clips';dest.mkdir();files=[]
        for i,source in enumerate(sources):
            self.status(f'Converting recording {i+1} of {len(sources)}',70+25*i/len(sources))
            target=dest/f'Handycam_{i+1:02}.mp4'
            self.run_ffmpeg(['-i',str(source),'-map','0:v:0','-map','0:a:0?','-vf','bwdif=mode=send_frame','-c:v','libx264','-preset','fast','-crf','18','-c:a','aac','-b:a','192k','-movflags','+faststart',str(target)],self.dest/f'conversion_{i+1:02}.log')
            files.append(target)
        listing=self.dest/'concat.txt';listing.write_text('\n'.join("file '"+str(t.resolve()).replace('\\','/').replace("'","'\\''")+"'" for t in files),encoding='utf-8')
        combined=self.dest/'Handycam_all_videos.mp4'
        self.status('Joining clips and checking the complete video',96)
        self.run_ffmpeg(['-f','concat','-safe','0','-i',str(listing),'-c','copy','-movflags','+faststart',str(combined)],self.dest/'join.log')
        self.run_ffmpeg(['-v','error','-i',str(combined),'-map','0:v','-map','0:a?','-f','null','-'],self.dest/'verification.log')
        errors=(self.dest/'verification.log').read_text(encoding='utf-8',errors='replace').strip()
        if errors:raise RecoveryError('Video saved, but decoding reported errors. See verification.log.')
        return combined,len(files)
    def run(self):
        if self.dest.drive.upper()!='D:':raise RecoveryError('Choose a destination on D:.')
        if not self.ffmpeg.exists():raise RecoveryError('Keep ffmpeg.exe beside the app.')
        if shutil.disk_usage(self.dest.parent).free<5*1024**3:raise RecoveryError('At least 5 GB free space on D: is required.')
        # Check the physical medium before creating a folder or trying Explorer paths.
        ready=Disc(self.drive)
        try:ready.wait_ready(self.status,self.cancel)
        finally:ready.close()
        self.dest.mkdir();archive=self.dest/'Original recovery';archive.mkdir();sources=[]
        # Finalized discs readable by Windows: preserve video files before converting.
        video_dir=mounted_video_directory(self.drive)
        if video_dir is not None:
            originals=sorted(p for p in video_dir.iterdir() if p.suffix.upper() in ('.VOB','.IFO','.BUP'))
            for p in originals:
                self.check();self.status('Copying '+p.name,20);shutil.copy2(p,archive/p.name)
            sources=[archive/p.name for p in originals if p.suffix.upper()=='.VOB' and not p.stem.upper().endswith('_0') and p.stem.upper()!='VIDEO_TS']
            if not sources:raise RecoveryError('No movie VOB files were found.')
        else:
            disc=Disc(self.drive)
            try:
                self.status('Inspecting recorded DVD tracks',0);tracks=disc.tracks()
                if not tracks:raise RecoveryError('No readable recorded tracks. Check seating, disc side, USB power, or another disc.')
                ifo_candidates=[];video_tracks=[]
                for t in tracks:
                    self.check()
                    try:head=disc.sectors(t['start'],1)
                    except ReadError:continue
                    if head.startswith(b'DVDVIDEO-VTS'):
                        if t['count']>8192:raise RecoveryError('Unexpected metadata size; stopped safely.')
                        target=archive/f"metadata_track_{t['track']:02}.bin";self.dump(disc,t['start'],t['count'],target);ifo_candidates.append((t,target))
                    elif head.startswith(b'\x00\x00\x01\xba'):video_tracks.append(t)
                if len(ifo_candidates)!=1:raise RecoveryError('This disc layout is not supported automatically. No changes were made to the disc.')
                mt,mp=ifo_candidates[0];meta=parse_ifo(mp.read_bytes());absolute=mt['start']+meta['video_offset']
                selected=next((t for t in video_tracks if t['start']==absolute),None)
                if selected is None:raise RecoveryError('The video track referenced by the metadata is not readable.')
                raw=archive/'Handycam_original.mpg';self.dump(disc,selected['start'],selected['count'],raw)
                self.status('Separating original recordings',70)
                for i,(first,last) in enumerate(meta['clips']):
                    self.check()
                    if (last+1)*2048>raw.stat().st_size:raise RecoveryError('Recording range exceeds readable track.')
                    target=archive/f'original_{i+1:02}.vob'
                    with raw.open('rb') as src,target.open('wb') as dst:
                        src.seek(first*2048);remaining=(last-first+1)*2048
                        while remaining:
                            data=src.read(min(1024*1024,remaining))
                            if not data:raise RecoveryError('Incomplete recovered recording.')
                            dst.write(data);remaining-=len(data)
                    sources.append(target)
            finally:disc.close()
        (self.dest/'read_errors.json').write_text(json.dumps(self.bad))
        combined,count=self.convert(sources)
        report={'recordings':count,'unreadable_sectors':len(self.bad),'combined_video':str(combined),'source_drive':self.drive,'disc_modified':False,'full_decode_check':'passed'}
        (self.dest/'recovery_report.json').write_text(json.dumps(report,indent=2))
        self.status(f'Saved {count} clips and combined video. Unreadable sectors: {len(self.bad)}',100)
        return report



class Recovery(LegacyRecovery):
    def run(self):
        from recovery_engine import RecoveryEngine
        engine=RecoveryEngine(self.drive,self.dest,self.status,self.cancel)
        self.log_directory=engine.log.directory
        return engine.run()
