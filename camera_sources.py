"""Camera/card file import and read-only removable-card imaging."""
from pathlib import Path
import ctypes,hashlib,os,re,shutil,time
from ctypes import wintypes
from studio_jobs import sha256,save_json,BASE
from mounted_files import VIDEO_SUFFIXES
from media_pipeline import MediaPipeline
from recovery import Cancelled

VIDEO_TYPES=VIDEO_SUFFIXES|{'.m2t','.m2v','.mxf','.m2p','.asf','.mjpeg','.mj2','.r3d','.braw','.insv','.360','.mpv','.rec','.tod','.moi'}- {'.moi'}
PHOTO_TYPES={'.jpg','.jpeg','.jpe','.png','.tif','.tiff','.bmp','.gif','.heic','.heif','.avif','.dng','.raw','.arw','.srf','.sr2','.cr2','.cr3','.crw','.nef','.nrw','.raf','.orf','.rw2','.rwl','.pef','.ptx','.srw','.erf','.kdc','.dcr','.3fr','.iiq','.x3f','.mrw','.mos'}
SIDECARS={'.xml','.thm','.moi','.cpi','.mpl','.mpls','.clpi','.ifo','.bup','.srt','.txt','.gps','.xmp','.lrv','.dat','.idx','.bin'}

def scan_files(source,job,photos=True,full_backup=True):
    source=Path(source).resolve()
    if source.is_file():
        stat=source.stat();suffix=source.suffix.lower();kind='video' if suffix in VIDEO_TYPES else 'photo' if suffix in PHOTO_TYPES else 'metadata'
        return [{'source':str(source),'relative_path':source.name,'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,'kind':kind}]
    if not source.is_dir():raise ValueError('Select a readable camera/card folder or drive.')
    if job.dest.resolve().is_relative_to(source):raise ValueError('Choose a source that does not contain the recovery destination. Source and output must be separate.')
    result=[];folders=[source];seen=0
    while folders:
        job.check();folder=folders.pop()
        try:
            with os.scandir(folder) as entries:
                for item in entries:
                    job.check();seen+=1
                    if seen>250000:raise ValueError('Too many files to enumerate safely. Select the camera media folder instead of an entire PC drive.')
                    if item.is_symlink():continue
                    if item.is_dir(follow_symlinks=False):folders.append(Path(item.path));continue
                    suffix=Path(item.name).suffix.lower()
                    kind='video' if suffix in VIDEO_TYPES else 'photo' if suffix in PHOTO_TYPES else 'metadata'
                    if not photos and kind=='photo':continue
                    if full_backup or kind=='video' or (photos and kind=='photo') or suffix in SIDECARS:
                        stat=item.stat(follow_symlinks=False)
                        result.append({'source':item.path,'relative_path':str(Path(item.path).relative_to(source)),
                                       'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,'kind':kind})
        except OSError as error:job.failure('list_camera_folder',error,folder=str(folder))
    return sorted(result,key=lambda i:i['relative_path'].casefold())

def import_camera(source,job,photos=True,full_backup=True,convert=True):
    files=scan_files(source,job,photos,full_backup)
    if not files:raise ValueError('No selected media files were found in this source.')
    total=sum(i['bytes'] for i in files)
    if shutil.disk_usage(job.dest).free<total*(3 if convert else 1)+128*1024*1024:raise OSError('Not enough space on D: for the selected backup and conversion.')
    job.report.update(source=str(Path(source).resolve()),files=files,total_files=len(files),total_source_bytes=total)
    archive=job.dest/'Camera originals';archive.mkdir();done=0;videos=[]
    for index,item in enumerate(files,1):
        job.check();path=Path(item['source']);target=archive/item['relative_path'];target.parent.mkdir(parents=True,exist_ok=True)
        partial=target.with_suffix(target.suffix+'.partial');item.update(target=str(partial),status='copying',copied_bytes=0)
        digest=hashlib.sha256()
        try:
            with path.open('rb') as original,partial.open('xb') as output:
                while item['copied_bytes']<item['bytes']:
                    job.check();block=None
                    for attempt in range(1,4):
                        try:
                            original.seek(item['copied_bytes']);block=original.read(min(4*1024*1024,item['bytes']-item['copied_bytes']))
                            if not block:raise OSError('The source ended before its declared size.')
                            break
                        except OSError as error:
                            block=None;job.log.exception('camera_file_read',error,file=str(path),offset=item['copied_bytes'],attempt=attempt)
                            if attempt==3:raise
                    output.write(block);digest.update(block);item['copied_bytes']+=len(block);done+=len(block)
                    job.status(f'Copying {index}/{len(files)} · {path.name}',{'phase':'Copy and verify originals','done':done,'total':total,'overall':(65 if convert else 95)*done/max(1,total)})
                output.flush();os.fsync(output.fileno())
            after=path.stat()
            if after.st_size!=item['bytes'] or after.st_mtime_ns!=item['mtime_ns']:raise OSError('The source changed while copying; this file is unverified.')
            copied_hash=sha256(partial,job.check)
            if copied_hash!=digest.hexdigest():raise OSError('Copied file checksum does not match bytes read from the source.')
            partial.rename(target);shutil.copystat(path,target);item.update(target=str(target),status='verified',sha256=copied_hash)
            if item['kind']=='video':videos.append({'path':str(target),'recording_id':item['relative_path']})
            job.log('INFO','camera_file_verified',**item)
        except Cancelled:raise
        except OSError as error:
            item['status']='partial' if item['copied_bytes'] else 'failed';job.failure('copy_camera_file',error,file=str(path),copied_bytes=item['copied_bytes'])
            if getattr(error,'winerror',0) in (6,21,55,433,1005,1112,1167):raise
        job.save()
    job.report.update(verified_files=sum(i['status']=='verified' for i in files),photo_originals=sum(i['kind']=='photo' and i['status']=='verified' for i in files),video_originals=len(videos))
    if convert and videos:
        media=MediaPipeline(BASE/'ffmpeg.exe',BASE/'ffprobe.exe',job.dest,event=job.log,status=job.status,cancel=job.cancel).convert(videos)
        job.report.update(media_report=media['report_path'],validated_count=media['validated_count'],partial_count=media['partial_count'],failed_count=media['failed_count'],combined_video=media['combined_video'])
        if not media['complete']:job.report.setdefault('incomplete_reasons',[]).append('Some converted media or the combined video did not pass every check. See media_report.json.')
    else:job.report['media_validation']='Original bytes verified; no video conversion/full decode requested.'

def removable_cards():
    kernel=ctypes.WinDLL('kernel32',use_last_error=True);result=[]
    for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
        if kernel.GetDriveTypeW(letter+':\\')==2 and letter not in 'CDE':result.append(letter+':')
    return result

def image_card(drive,job):
    drive=drive.upper().rstrip('\\/')
    if not re.fullmatch('[A-Z]:',drive) or drive not in removable_cards():raise ValueError('Select a detected removable card drive. PC/system/output drives are excluded.')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.DeviceIoControl.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p]
    kernel.ReadFile.argtypes=[wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p]
    kernel.SetFilePointerEx.argtypes=[wintypes.HANDLE,ctypes.c_longlong,ctypes.POINTER(ctypes.c_longlong),wintypes.DWORD]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateFileW('\\\\.\\'+drive,0x80000000,3,None,3,0,None)
    if handle==ctypes.c_void_p(-1).value:
        code=ctypes.get_last_error()
        if code==5:raise PermissionError('Windows requires administrator access to read the raw card. Right-click this app and choose Run as administrator, or scan an existing image file.')
        raise ctypes.WinError(code)
    target=job.dest/'card.img.partial';bad=[];size=ctypes.c_longlong();returned=wintypes.DWORD();position=0
    try:
        if not kernel.DeviceIoControl(handle,0x7405c,None,0,ctypes.byref(size),8,ctypes.byref(returned),None):raise ctypes.WinError(ctypes.get_last_error())
        total=size.value
        if total<=0:raise OSError('The removable card reports no readable capacity.')
        if shutil.disk_usage(job.dest).free<total+128*1024*1024:raise OSError('Not enough space on D: for a full card image.')
        job.report.update(source_drive=drive,total_bytes=total,image=str(target),bad_ranges=bad,access='GENERIC_READ only; no lock, format or dismount commands')
        buffer=ctypes.create_string_buffer(1024*1024)
        with target.open('xb') as output:
            while position<total:
                job.check();amount=min(len(buffer),total-position)
                if not kernel.SetFilePointerEx(handle,position,None,0):raise ctypes.WinError(ctypes.get_last_error())
                read=wintypes.DWORD()
                if not kernel.ReadFile(handle,buffer,amount,ctypes.byref(read),None) or read.value!=amount:
                    error=ctypes.WinError(ctypes.get_last_error());job.log.exception('card_read',error,byte_offset=position,requested_bytes=amount,transferred_bytes=read.value)
                    if getattr(error,'winerror',0) in (6,21,55,433,1167):raise error
                    # Isolate failed blocks, preserving accessible neighbours.
                    for offset in range(0,amount,4096):
                        job.check();length=min(4096,amount-offset);data=None
                        for attempt in range(1,4):
                            if not kernel.SetFilePointerEx(handle,position+offset,None,0):raise ctypes.WinError(ctypes.get_last_error())
                            if kernel.ReadFile(handle,buffer,length,ctypes.byref(read),None) and read.value==length:data=buffer.raw[:length];break
                            error=ctypes.WinError(ctypes.get_last_error());job.log.exception('card_block_retry',error,byte_offset=position+offset,bytes=length,attempt=attempt)
                            if getattr(error,'winerror',0) in (6,21,55,433,1167):raise error
                        if data is None:bad.append({'offset':position+offset,'bytes':length});data=bytes(length)
                        output.write(data)
                else:output.write(buffer.raw[:amount])
                position+=amount;job.report['copied_bytes']=position;job.save()
                job.status('Creating read-only card image',{'phase':'Card image','done':position,'total':total,'overall':95*position/total})
            output.flush();os.fsync(output.fileno())
        final=target.with_suffix('');target.rename(final);job.report.update(image=str(final),sha256=sha256(final,job.check))
        if bad:job.report['incomplete_reasons']=['The image has unreadable blocks, represented by explicitly mapped zero placeholders.']
    finally:
        kernel.CloseHandle(handle);job.report['copied_bytes']=target.stat().st_size if target.exists() else position;job.save()
