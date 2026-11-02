"""Combine compatible DVD-track copies after exact shared-sector comparison."""
from pathlib import Path
import json,shutil
from studio_jobs import save_json,sha256

SECTOR=2048;ZERO=bytes(SECTOR)
def load_track(path):
    path=Path(path).resolve();normal=path.with_suffix('') if path.suffix=='.partial' else path
    sidecar=normal.with_suffix(normal.suffix+'.state.json')
    if not path.is_file() or not sidecar.is_file():raise ValueError('Select preserved track_XX.bin or .bin.partial files with their original .state.json sidecars.')
    state=json.loads(sidecar.read_text(encoding='utf-8'));start=int(state['start']);count=int(state['count']);available=path.stat().st_size//SECTOR
    if start<0 or not 0<count<=5000000 or available>count or path.stat().st_size%SECTOR or state.get('available_sectors',available)!=available:raise ValueError('Invalid or uncheckpointed track image size/coordinates.')
    bad=set(state.get('bad_sectors',[]))
    if any(not isinstance(value,int) or value<start or value>=start+available for value in bad):raise ValueError('Invalid source bad-sector map.')
    return {'path':path,'start':start,'count':count,'available':available,'bad':bad,'state':state,'identity':(path.stat().st_size,path.stat().st_mtime_ns)}

def merge_track_copies(paths,job,minimum_shared=8192):
    if len(paths)<2:raise ValueError('Select at least two interrupted copies of the same DVD track.')
    sources=[load_track(path) for path in paths]
    if len({str(source['path']) for source in sources})!=len(sources):raise ValueError('Choose distinct saved images.')
    first=max(sources,key=lambda source:source['available']);start=first['start'];count=first['count']
    if any(source['start']!=start or source['count']!=count for source in sources):raise ValueError('These copies have different track coordinates/capacities and will not be combined.')
    if shutil.disk_usage(job.dest).free<count*SECTOR+128*1024*1024:raise OSError('Not enough space on D: for the combined image.')
    track=first['state'].get('track',1);track=int(track.get('track',1) if isinstance(track,dict) else track)
    directory=job.dest/'Original recovery';directory.mkdir();target=directory/f'track_{track:02}.bin.partial'
    bad=first['bad']|set(range(start+first['available'],start+count));comparisons=[]
    job.report.update(sources=[str(source['path']) for source in sources],sector_count=count,source_track_start=start,comparisons=comparisons)
    with first['path'].open('rb') as original,target.open('xb+') as merged:
        for position in range(0,first['available'],1024):
            job.check();merged.write(original.read(min(1024,first['available']-position)*SECTOR))
        merged.truncate(count*SECTOR);merged.flush()
        for source in sources:
            if source is first:continue
            shared=0;markers=0
            with source['path'].open('rb') as other:
                # No sectors from this source are used until every shared clean
                # sector has compared exactly against the current merged copy.
                for position in range(0,source['available'],1024):
                    job.check();amount=min(1024,source['available']-position);other.seek(position*SECTOR);left=other.read(amount*SECTOR);merged.seek(position*SECTOR);right=merged.read(amount*SECTOR)
                    for index in range(amount):
                        lba=start+position+index
                        if lba in bad or lba in source['bad']:continue
                        offset=index*SECTOR;a=left[offset:offset+SECTOR];b=right[offset:offset+SECTOR]
                        if a!=b:raise ValueError(f'Clean sectors differ at LBA {lba}. These saved images are not safely interchangeable.')
                        shared+=1
                        if a[:4]==b'\x00\x00\x01\xba' or a.startswith((b'DVDVIDEO',b'DVD_RTR')):markers+=1
                    job.status('Comparing every shared readable sector',{'phase':'Verify image identity','done':position+amount,'total':source['available'],'overall':40*(position+amount)/max(1,source['available'])})
                if shared<minimum_shared or markers<1:raise ValueError('Not enough matching readable video/metadata sectors to establish a compatible copy.')
                repaired=[]
                for position in range(0,source['available'],1024):
                    job.check();amount=min(1024,source['available']-position);wanted=[index for index in range(amount) if start+position+index in bad and start+position+index not in source['bad']]
                    if not wanted:continue
                    other.seek(position*SECTOR);data=other.read(amount*SECTOR)
                    for index in wanted:
                        lba=start+position+index;merged.seek((position+index)*SECTOR);merged.write(data[index*SECTOR:(index+1)*SECTOR]);bad.remove(lba);repaired.append(lba)
            comparisons.append({'source':str(source['path']),'shared_clean_sectors_compared':shared,'matching_media_markers':markers,'repaired_sector_count':len(repaired)})
            job.log('INFO','saved_images_verified_and_combined',**comparisons[-1]);job.save()
        merged.flush()
    for source in sources:
        stat=source['path'].stat()
        if (stat.st_size,stat.st_mtime_ns)!=source['identity']:raise OSError('A source image changed during comparison; merged output remains unverified.')
    final=target.with_suffix('')
    metadata_copies=[];metadata_pending=[]
    for sidecar in first['path'].parent.glob('track_*.bin.state.json'):
        info=json.loads(sidecar.read_text(encoding='utf-8'))
        if not info.get('complete') or not 0<int(info.get('count',0))<=8192:continue
        original=sidecar.with_name(sidecar.name.removesuffix('.state.json'))
        if not original.is_file() or original==first['path']:continue
        header=original.read_bytes()
        if not header.startswith((b'DVDVIDEO',b'DVD_RTR')):continue
        digest=sha256(original,job.check)
        for source in sources:
            comparison=source['path'].parent/original.name
            if comparison.is_file() and sha256(comparison,job.check)!=digest:raise ValueError('Sibling DVD scene metadata differs between these copies. The combined image remains unverified.')
        saved=directory/original.name
        if saved==final:raise ValueError('Conflicting saved metadata/image track names.')
        metadata_pending.append((original,saved,sidecar.name,info,digest))
    # Publish a usable image/checkpoint only after all metadata identity checks.
    for original,saved,name,info,digest in metadata_pending:
        shutil.copy2(original,saved)
        if sha256(saved,job.check)!=digest:raise OSError('Copied DVD metadata checksum failed.')
        info['path']=str(saved);info['metadata_sha256']=digest;save_json(directory/name,info);metadata_copies.append(str(saved))
    final_digest=sha256(target,job.check)
    target.rename(final)
    state={'path':str(final),'start':start,'count':count,'available_sectors':count,'readable_sectors':count-len(bad),'bad_sectors':sorted(bad),'complete':not bad,'track':track,'interrupted':None,'source_modified':False,'merged_from':comparisons}
    save_json(final.with_suffix(final.suffix+'.state.json'),state);save_json(final.with_suffix(final.suffix+'.bad-sectors.json'),{'bad_sectors':sorted(bad),'start':start,'count':count})
    job.report['preserved_metadata_tracks']=metadata_copies
    job.report.update(merged_image=str(final),remaining_bad_sectors=len(bad),sha256=final_digest,summary=f'Matching saved copies combined; {len(bad)} sectors remain unavailable. Select this result folder in DVD / saved disc to extract and validate video.')
    if bad:job.report['incomplete_reasons']=['Some sectors remain unavailable in every selected copy. They remain explicitly mapped zero placeholders.']
