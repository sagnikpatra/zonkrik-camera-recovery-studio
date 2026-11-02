"""Bounded DVD-VR VVOB-map reader.

Format research: Pádraig Brady's dvd-vr (GPL-2.0-or-later),
https://github.com/pixelb/dvd-vr . This reader inventories physical program
regions; it does not implement edited/merged playlists or still-image groups.
"""
from pathlib import Path
import shutil
from studio_jobs import BASE,sha256
from media_pipeline import MediaPipeline

def parse_manager(data):
    def integer(offset,length):
        if offset<0 or offset+length>len(data):raise ValueError('Truncated DVD-VR manager table.')
        return int.from_bytes(data[offset:offset+length],'big')
    if len(data)<512 or data[:12]!=b'DVD_RTR_VMG\x00'[:12]:
        # Actual signature is eleven printable bytes followed by its version/pad.
        if not data.startswith(b'DVD_RTR_VMG'):raise ValueError('This is not a DVD-VR VR_MANGR manager.')
    start=integer(0x100,4)
    if start<512 or start+8>len(data):raise ValueError('DVD-VR program table is absent or outside the manager file.')
    tables=integer(start+2,1);formats=integer(start+3,1);end=start+integer(start+4,4)+1
    if not 0<=formats<=32 or end>len(data) or end<start+8:raise ValueError('Invalid DVD-VR program table bounds.')
    if tables==0:return {'programs':[],'complete':False,'warnings':['No movie program entries are stored in this manager. A formatted disc or interrupted recording may need raw-sector scanning.']}
    if tables!=1:raise ValueError('Multiple DVD-VR movie information tables require raw salvage; the manager was preserved.')
    count_pos=start+8+formats*60;count=integer(count_pos,2)
    if count>10000 or count_pos+2+count*4>end:raise ValueError('Invalid DVD-VR physical program count.')
    programs=[]
    for index in range(count):
        record=start+integer(count_pos+2+index*4,4)
        if record<count_pos+2+count*4 or record+23>end:raise ValueError('DVD-VR program offset exceeds its table.')
        attributes=integer(record,2);mapping=record+21+(12 if attributes&0x80 else 0)+2
        times=integer(mapping,2);units=integer(mapping+2,2);first=integer(mapping+6,4)
        entries=mapping+10+times*7
        if entries+units*3>end:raise ValueError('Truncated DVD-VR VOBU extent map.')
        size=sum(integer(entries+entry*3+1,2)&0x3ff for entry in range(units))
        if not units or not size:continue
        programs.append({'id':index+1,'first_sector':first,'last_sector':first+size-1,'sector_count':size,'vobu_count':units,'attributes':attributes})
    ordered=sorted(programs,key=lambda p:p['first_sector'])
    if any(a['last_sector']>=b['first_sector'] for a,b in zip(ordered,ordered[1:])):raise ValueError('DVD-VR program regions overlap; automatic scene splitting would be ambiguous.')
    return {'programs':programs,'complete':False,'warnings':['Physical DVD-VR program regions are available. Edited/merged playlists, chapters and still-image groups are not interpreted; the full original movie is preserved.']}

def split_vro(manager,movie,job):
    manager=Path(manager);movie=Path(movie)
    if not manager.is_file() or manager.stat().st_size>16*1024*1024:raise ValueError('Choose the VR_MANGR.IFO or VR_MANGR.BUP manager file.')
    if not movie.is_file() or movie.stat().st_size==0:raise ValueError('The VRO file is empty or unavailable through Windows. Use DVD / saved disc for raw-sector recovery instead.')
    metadata=parse_manager(manager.read_bytes());job.report['dvd_vr']=metadata
    if not metadata['programs']:raise ValueError('No movie programs are indexed. Use raw disc recovery to search for unindexed footage.')
    total=movie.stat().st_size
    if max(p['last_sector'] for p in metadata['programs'])*2048+2048>total:raise ValueError('Indexed movie data extends beyond the available VRO copy. Preserve the partial source and use raw disc recovery.')
    originals=job.dest/'DVD-VR originals';originals.mkdir();shutil.copy2(manager,originals/manager.name);shutil.copy2(movie,originals/movie.name)
    job.report.update(source_movie=str(movie),source_sha256=sha256(movie,job.check),source_bytes=total)
    clips=job.dest/'Indexed DVD-VR regions';clips.mkdir();sources=[]
    with (originals/movie.name).open('rb') as original:
        for index,program in enumerate(metadata['programs'],1):
            job.check();target=clips/f"VR_program_{program['id']:03}.vob";remaining=program['sector_count']*2048
            original.seek(program['first_sector']*2048)
            with target.open('xb') as output:
                while remaining:
                    job.check();block=original.read(min(4*1024*1024,remaining))
                    if not block:raise OSError('The VRO source ended unexpectedly.')
                    output.write(block);remaining-=len(block)
            sources.append({'path':str(target),'recording_id':program['id']})
            job.status(f"Extracting DVD-VR region {index}/{len(metadata['programs'])}",65*index/len(metadata['programs']))
    media=MediaPipeline(BASE/'ffmpeg.exe',BASE/'ffprobe.exe',job.dest,event=job.log,status=job.status,cancel=job.cancel).convert(sources)
    job.report.update(validated_count=media['validated_count'],partial_count=media['partial_count'],failed_count=media['failed_count'],media_report=media['report_path'],combined_video=media['combined_video'])
    job.report['incomplete_reasons']=metadata['warnings']
