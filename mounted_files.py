"""Read-only copying of ordinary video files from Windows-mounted data discs."""
from __future__ import annotations
import os
from pathlib import Path
import re
import time
from recovery_logging import error_details

VIDEO_SUFFIXES={'.mpg','.mpeg','.vob','.vro','.mp4','.m4v','.avi','.mov','.mts','.m2ts','.ts','.mod','.tod','.wmv','.mkv','.3gp','.dv'}

def safe_name(value):
    text=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',str(value)).strip(' .')[:140]
    return text or 'recording'

def collect_videos(root,log,check):
    root=Path(root);pending=[root];found=[];errors=[];ignored=0;seen=0
    while pending:
        check();folder=pending.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    check();seen+=1
                    if seen>100000:raise OSError('The disc contains too many directory entries to enumerate safely.')
                    if entry.is_symlink():continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.upper() in ('VIDEO_TS','DVD_RTAV','BDMV'):return None
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        if Path(entry.name).suffix.lower() in VIDEO_SUFFIXES:
                            stat=entry.stat(follow_symlinks=False)
                            found.append({'path':entry.path,'name':str(Path(entry.path).relative_to(root)),
                                          'size_bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns})
                        else:ignored+=1
        except OSError as error:
            errors.append({'directory':str(folder),'error':error_details(error)})
            log.exception('mounted_directory_read',error,directory=str(folder))
            if getattr(error,'winerror',None) in (6,21,55,433,1005,1112,1167):
                import recovery
                raise recovery.DiscChanged('The disc or USB connection changed while listing files. Reinsert the same disc before retrying.') from error
    if not found:return None
    found.sort(key=lambda item:item['name'].casefold())
    return {'type':'Windows mounted data disc','files':found,'enumeration_errors':errors,'ignored_nonvideo_files':ignored,'complete':not errors}

def copy_videos(inventory,destination,log,status,check,checkpoint=None):
    import recovery
    directory=Path(destination)/'Original recovery'/'Data disc files';directory.mkdir(parents=True,exist_ok=False)
    sources=[];results=[];inventory['copies']=results
    for index,item in enumerate(inventory['files'],1):
        check();source=Path(item['path']);name=f"{index:03}_{safe_name(Path(item['name']).name)}"
        target=directory/name;partial=target.with_suffix(target.suffix+'.partial');copied=0;failure=None
        status(f"Copying video file {index} of {len(inventory['files'])}: {source.name}",70*(index-1)/len(inventory['files']))
        result={**item,'target':str(partial),'copied_bytes':0,'complete':False};results.append(result)
        try:
            with source.open('rb') as original,partial.open('xb') as output:
                while copied<item['size_bytes']:
                    check();data=None
                    for attempt in range(1,4):
                        try:
                            original.seek(copied);data=original.read(min(1024*1024,item['size_bytes']-copied))
                            if not data:raise OSError('The optical file ended before its directory-declared size.')
                            break
                        except OSError as error:
                            data=None
                            log.exception('mounted_file_read',error,source=str(source),byte_offset=copied,attempt=attempt,max_attempts=3)
                            failure=error
                            if getattr(error,'winerror',None) in (6,21,55,433,1005,1112,1167):raise
                    if data is None:raise failure
                    output.write(data);copied+=len(data);result['copied_bytes']=copied
                output.flush();os.fsync(output.fileno())
            after=source.stat()
            if after.st_size!=item['size_bytes'] or after.st_mtime_ns!=item['mtime_ns']:
                raise OSError('The source file changed while copying. This copy remains unverified.')
            partial.rename(target);result.update(target=str(target),complete=True)
        except recovery.Cancelled:raise
        except OSError as error:
            result['error']=error_details(error);inventory['complete']=False
            log.exception('mounted_file_copy',error,source=str(source),target=str(partial),copied_bytes=copied,expected_bytes=item['size_bytes'])
            if getattr(error,'winerror',None) in (6,21,55,433,1005,1112,1167):
                raise recovery.DiscChanged('The disc or USB connection changed while copying. Existing copies and detailed logs remain saved. Reinsert the same disc before retrying.') from error
        if copied:
            sources.append({'path':result['target'],'recording_id':item['name'],'truncated_source':not result['complete'],
                            'output_filename':f"{index:03}_{safe_name(Path(item['name']).stem)}.mp4"})
        log('INFO' if result['complete'] else 'ERROR','mounted_file_copy_finished',**result)
        if checkpoint:checkpoint()
    inventory['missing_recordings']=sum(not item['copied_bytes'] for item in results)
    inventory['complete']=bool(inventory['complete'] and all(item['complete'] for item in results))
    return sources
