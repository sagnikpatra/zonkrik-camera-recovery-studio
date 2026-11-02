"""Local signature recovery, damaged-container repair and capture workflows."""
from pathlib import Path
import json,os,re,shutil,threading
from studio_jobs import BASE,sha256
from media_pipeline import MediaPipeline

def helper(name):
    root=BASE/'tools'/name
    filename='photorec_win.exe' if name=='photorec' else 'untrunc.exe'
    found=list(root.rglob(filename))
    if len(found)!=1:raise FileNotFoundError(f'The bundled {name} helper is missing. Keep the tools folder beside the app.')
    return found[0]

def recover_deleted(image,job,all_formats=False,keep_damaged=False):
    image=Path(image).resolve()
    if not image.is_file() or image.stat().st_size==0:raise ValueError('Select a nonempty saved card/disc image. Create an image first for a physical card.')
    if image.suffix.lower() not in {'.img','.dd','.raw','.bin','.iso','.e01','.dmp','.partial'}:raise ValueError('Choose a saved disk/card image, not a normal video file.')
    before=image.stat();destination=job.dest/'Recovered candidates'
    commands='partition_none,'+('options,keep_corrupted_file,' if keep_damaged else '')
    commands+='fileopt,everything,'+('enable' if all_formats else 'disable,jpg,enable,tif,enable,png,enable,mov,enable,riff,enable,mpg,enable,m2ts,enable')+',wholespace,search'
    env=os.environ.copy();env['__COMPAT_LAYER']='RunAsInvoker'
    result=job.command([helper('photorec'),'/log','/logname',job.dest/'photorec.log','/d',destination,'/cmd',image,commands],'PhotoRec image recovery',env=env)
    helper_log=(job.dest/'photorec.log').read_text(encoding='utf-8',errors='replace') if (job.dest/'photorec.log').is_file() else ''
    if result['returncode'] or re.search('syntax error|unknown option|failed to open',result['stderr_text']+'\n'+helper_log,re.IGNORECASE):raise RuntimeError(f"PhotoRec did not complete successfully (exit {result['returncode']}). See {result['stderr']} and photorec.log.")
    after=image.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise OSError('The source image changed during recovery; results require review.')
    recovered=[]
    for folder in job.dest.glob('Recovered candidates*'):
        for file in folder.rglob('*'):
            job.check()
            if file.is_file() and file.name!='report.xml':recovered.append({'path':str(file),'bytes':file.stat().st_size,'sha256':sha256(file,job.check)})
    job.report.update(source_image=str(image),recovered_candidates=len(recovered),files=recovered,summary=f'{len(recovered)} signature candidates saved. Import them in Camera/cards for full video validation.',
                      validation='Signature matches are candidates, not proof of complete recordings. Import recovered files for full video decoding.',
                      fragmented_video_note='Fragmented camera MOV/MP4 can need a reference clip and separate ftyp/mdat reconstruction; overwritten bytes cannot be recovered.')
    if not recovered:job.report['incomplete_reasons']=['No recoverable signatures were found using these settings.']

def repair_video(broken,reference,job,use_reference=True):
    broken=Path(broken).resolve()
    if not broken.is_file() or broken.stat().st_size==0:raise ValueError('Select a nonempty damaged video file.')
    # Helpers receive copies only: all new indexes/headers stay in the new job.
    directory=job.dest/'Repair working copies';directory.mkdir();copy=directory/('damaged_input'+broken.suffix)
    shutil.copy2(broken,copy);source_hash=sha256(broken,job.check)
    if sha256(copy,job.check)!=source_hash:raise OSError('Working copy checksum failed.')
    job.report.update(source=str(broken),source_sha256=source_hash,repair_method='Reference-based untrunc' if use_reference else 'FFmpeg stream remux')
    if use_reference:
        reference=Path(reference).resolve()
        if not reference.is_file() or reference==broken:raise ValueError('Select a different healthy recording, preferably from the same camera and recording settings.')
        refcopy=directory/('healthy_reference'+reference.suffix);shutil.copy2(reference,refcopy)
        output=directory/'repaired.mp4'
        result=job.command([helper('untrunc'),'-n','-s','-dst',output,refcopy,copy],'Reference video reconstruction')
    else:
        output=directory/'repaired.mkv'
        result=job.command([BASE/'ffmpeg.exe','-hide_banner','-nostdin','-n','-v','warning','-fflags','+genpts','-err_detect','ignore_err','-i',copy,'-map','0:v?','-map','0:a?','-c','copy',output],'Container index reconstruction')
    if result['returncode'] or not output.is_file():raise RuntimeError(f"Repair did not produce a usable file. Full helper details: {result['stderr']}")
    pipeline=MediaPipeline(BASE/'ffmpeg.exe',BASE/'ffprobe.exe',job.dest,event=job.log,status=job.status,cancel=job.cancel)
    validation=pipeline.validate_media(output,'repaired_video')
    job.report.update(repaired_file=str(output),validation=validation,summary='Reconstructed video saved and fully decode-checked. Review the reported frame count and duration against the original recording.',
                      completeness='A decodable reconstructed file does not prove missing frames were recovered. Compare against the expected scene/tape length.')
    if not validation['readable'] or not validation['decode_clean']:job.report['incomplete_reasons']=['Reconstructed video still has decoding errors; copied sources and all logs are preserved.']
    elif validation.get('video_timestamp_discontinuity_count',0) or any(a.get('timestamp_discontinuity_count',0) for a in validation.get('audio',[])):
        job.report['incomplete_reasons']=['Reconstructed frames decode, but timestamps contain resets/gaps. Import the repaired file in Camera/cards to rebuild a continuous playback clock; compare recording length.']

def enumerate_capture(job):
    result=job.command([BASE/'ffmpeg.exe','-hide_banner','-list_devices','true','-f','dshow','-i','dummy'],'Enumerate capture devices',timeout=30)
    devices={'video':[],'audio':[]}
    for name,kind in re.findall(r'"([^"\r\n]+)"\s+\((video|audio)\)',result['stderr_text']):
        if name not in devices[kind]:devices[kind].append(name)
    job.report['capture_devices']=devices
    return devices

def capture_tape(video,audio,mode,minutes,standard,job):
    if not video:raise ValueError('Select the actual capture adapter or DV/HDV device first.')
    minutes=float(minutes)
    if not 0<=minutes<=720:raise ValueError('Capture duration must be between 0 (until Stop) and 720 minutes.')
    if any(char in video+audio for char in '\r\n'):raise ValueError('Invalid capture-device name.')
    specs={'Device default':[], 'PAL 720×576 / 25 fps':['-video_size','720x576','-framerate','25'],
           'NTSC 720×480 / 29.97 fps':['-video_size','720x480','-framerate','30000/1001']}
    if standard not in specs:raise ValueError('Select a supported capture standard.')
    inputs='video='+video+(':audio='+audio if audio else '')
    output=job.dest/'Tape archive.mkv'
    args=[BASE/'ffmpeg.exe','-hide_banner','-n','-v','warning','-thread_queue_size','1024','-rtbufsize','512M','-f','dshow',*specs[standard],'-i',inputs]
    args+=['-map','0:v:0','-map','0:a?']
    if minutes:args+=['-t',str(minutes*60)]
    if mode=='Preserve DV/HDV stream':args+=['-c','copy']
    elif mode=='Lossless archive (FFV1 + PCM)':args+=['-c:v','ffv1','-level','3','-c:a','pcm_s16le']
    elif mode=='MP4-compatible H.264 archive':args+=['-c:v','libx264','-preset','fast','-crf','18','-c:a','aac','-b:a','192k']
    else:raise ValueError('Unknown capture mode.')
    args+=[output]
    result=job.command(args,'Tape capture',expected_seconds=minutes*60 or None,graceful=True)
    job.report.update(captured_file=str(output),capture_end='user stopped' if job.cancel.is_set() else 'time limit/device end',
                      note='Play/rewind the tape on the camera or deck. Tape length and completeness cannot be inferred from live capture. Device-default capture preserves its reported scan standard.',
                      capture_mode=mode,capture_standard=standard,full_decode_check='Not run during live capture. Import the archive in Camera/cards to convert and fully validate it.')
    if result['returncode'] or not output.exists() or output.stat().st_size==0:raise RuntimeError(f"Capture failed. Check the adapter, camera playback and device options. Details: {result['stderr']}")
    job.report['captured_bytes']=output.stat().st_size
    job.report['summary']='Tape archive saved. Import it in Camera/cards for full decoding, MP4 conversion and validation.'
    # Stop finalizes a deliberate capture; it is not a discarded recovery.
    job.cancel.clear()

def inspect_capture_options(video,job):
    if not video:raise ValueError('Select a video capture device.')
    result=job.command([BASE/'ffmpeg.exe','-hide_banner','-list_options','true','-f','dshow','-i','video='+video],'Capture device formats',timeout=30)
    job.report['device_options']=result['stderr_text']
    return result['stderr_text']
