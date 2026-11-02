"""All-track DVD recovery with persistent evidence and partial-data salvage."""
from __future__ import annotations
import hashlib,json,os,shutil,threading,time,traceback
from pathlib import Path
import recovery as low
from recovery_logging import RunLogger,DEFAULT_VIDEO_ROOT,error_details
from dvd_layout import scan_metadata_file,merge_ranges
from media_pipeline import MediaPipeline,PipelineCancelled
from resume_candidates import find_verified_previous

SECTOR=2048

def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp');temp.write_text(json.dumps(value,indent=2,default=str),encoding='utf-8');temp.replace(path)

def logical_size(path):
    with Path(path).open('rb') as stream:stream.seek(0,2);return stream.tell()

class SectorSpace:
    def __init__(self,images):self.images=sorted(images,key=lambda i:i['start'])
    def extract(self,ranges,target,cancel=None,extent_sizes=None):
        """Retain every available span; missing spans are never invented."""
        target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
        partial=target.with_suffix('.partial.vob');missing=[];bad=[];written=0
        with partial.open('xb') as output:
            if extent_sizes is not None and (len(extent_sizes)!=len(ranges) or any(not isinstance(size,int) or size<0 or size>(last-first+1)*SECTOR for (first,last),size in zip(ranges,extent_sizes))):
                raise low.RecoveryError('The data file has invalid extent byte sizes.')
            for extent_index,(first,last) in enumerate(ranges):
                extent_bytes=extent_sizes[extent_index] if extent_sizes is not None else (last-first+1)*SECTOR
                position=first
                while position<=last:
                    if cancel:cancel()
                    image=next((i for i in self.images if i['start']<=position<i['start']+i['available_sectors']),None)
                    if image is None:
                        following=[i['start'] for i in self.images if i['start']>position]
                        end=min(last,min(following)-1) if following else last
                        missing.append([position,end]);position=end+1;continue
                    count=min(last-position+1,image['start']+image['available_sectors']-position)
                    with Path(image['path']).open('rb') as source:
                        source.seek((position-image['start'])*SECTOR);remaining=min(count*SECTOR,max(0,extent_bytes-(position-first)*SECTOR))
                        while remaining:
                            if cancel:cancel()
                            block=source.read(min(1024*1024,remaining))
                            if not block:raise low.RecoveryError('Saved track ended while extracting a recording.')
                            output.write(block);remaining-=len(block);written+=len(block)
                    bad.extend(lba for lba in image.get('bad_sectors',[]) if position<=lba<position+count)
                    position+=count
        if not missing:partial.rename(target);saved=target
        else:saved=partial
        return {'path':str(saved),'written_bytes':written,'missing_ranges':missing,'unreadable_sectors':sorted(set(bad)),'truncated_source':bool(missing)}

class RecoveryEngine:
    def __init__(self,drive,destination,status=None,cancel=None,saved_source=None):
        self.drive=drive;self.dest=Path(destination);self.status=status or (lambda *a:None);self.cancel=cancel or threading.Event();self.saved_source=Path(saved_source) if saved_source else None
        self.log=RunLogger(DEFAULT_VIDEO_ROOT/'Logs'/self.dest.name,drive=drive,destination=str(self.dest),saved_source=str(self.saved_source) if self.saved_source else None)
        self.images=[];self.inventory=[];self.metadata=[];self.extraction=[];self.warnings=[];self.failures=[]
        self.saved_file_sources=None
        self.report={'schema_version':2,'status':'running','complete':False,'source_drive':drive,'disc_modified':False,'log_directory':str(self.log.directory),'destination':str(self.dest),'images':self.images,'metadata':self.metadata,'extraction':self.extraction,'warnings':self.warnings,'failures':self.failures}
    def check(self):
        if self.cancel.is_set():raise low.Cancelled('Stopped. All copied files and detailed logs remain saved.')
    def save(self):
        self.report['log_directory']=str(self.log.directory)
        write_json(self.log.directory/'recovery_report.json',self.report)
        if self.dest.exists():write_json(self.dest/'recovery_report.json',self.report)
    def failure(self,stage,error,**fields):
        item={'stage':stage,'error':error_details(error),**fields};self.failures.append(item);self.log.exception(stage,error,**fields);self.save()
    def open_disc(self):
        disc=low.Disc(self.drive);disc.logger=self.log;return disc
    def mounted_sources(self):
        """Keep separate files on ordinary mounted data DVDs, including UDF."""
        if len(self.drive)!=2 or self.drive[1]!=':':return None
        disc=self.open_disc()
        try:disc.wait_ready(self.status,self.cancel)
        finally:disc.close()
        from mounted_files import collect_videos,copy_videos
        inventory=collect_videos(Path(self.drive+'\\'),self.log,self.check)
        if not inventory:return None
        required=sum(item['size_bytes'] for item in inventory['files'])*3+512*1024*1024
        if shutil.disk_usage(self.dest.parent).free<required:raise low.RecoveryError('Not enough free space on D: to preserve all original files and recovered MP4 videos.')
        self.dest.mkdir(parents=True,exist_ok=False)
        self.report['mounted_filesystem']=inventory;self.report['expected_recordings']=len(inventory['files'])
        self.log('INFO','mounted_video_files_detected',inventory=inventory);self.save()
        sources=copy_videos(inventory,self.dest,self.log,self.status,self.check,self.save)
        self.report['missing_recordings']=inventory['missing_recordings'];self.save()
        return sources
    def _verified_previous(self,disc,track):
        """Reuse verified full or partial copies without changing the old source."""
        return find_verified_previous(disc,track,self.images,DEFAULT_VIDEO_ROOT,self.log,self.check)
    def image_disc(self):
        from disc_imaging import DiscImager
        disc=self.open_disc()
        try:
            disc.wait_ready(self.status,self.cancel)
            self.inventory=disc.tracks();self.report['track_inventory']=self.inventory
            if not self.inventory:raise low.RecoveryError('No recorded tracks were reported. Detailed device status is saved in Logs.')
            required=sum(t['count']*SECTOR for t in self.inventory)*3+512*1024*1024
            free=shutil.disk_usage(self.dest.parent).free
            self.log('INFO','storage_preflight',free_bytes=free,estimated_required_bytes=required)
            if free<required:raise low.RecoveryError(f'Not enough free space on D:. Approximately {required/1024**3:.1f} GB is needed to preserve originals, extracts, MP4s and logs.')
            self.dest.mkdir(parents=True,exist_ok=False);archive=self.dest/'Original recovery';archive.mkdir()
            write_json(archive/'track_inventory.json',self.inventory)
            self.log('INFO','disc_inventory',tracks=self.inventory,max_read_sectors=getattr(disc,'max_read_sectors',None))
            for number,track in enumerate(self.inventory,1):
                self.check();self.status(f"Reading track {number} of {len(self.inventory)}",0)
                target=archive/f"track_{track['track']:02}.bin"
                old,old_bad=self._verified_previous(disc,track)
                # Small scene indexes must not consume the whole recovery run
                # when the later video track can still be read independently.
                budgets={'repair_budget_seconds':15,'no_progress_timeout':20} if track['count']<=8192 else {'adaptive_reads':True}
                imager=DiscImager(disc,self.log,self.status,self.cancel,**budgets)
                image=imager.image(track,target,existing_source=old,existing_bad=old_bad)
                image['track']=track['track']
                if track['count']<=8192 and image['available_sectors']:
                    image['metadata_sha256']=hashlib.sha256(Path(image['path']).read_bytes()).hexdigest()
                self.images.append(image);self.save()
                if image.get('interrupted'):
                    self.failures.append({'stage':'imaging','track':track['track'],'error':image['interrupted']})
                    self.log('ERROR','disc_imaging_interrupted',track=track['track'],details=image['interrupted'])
                    if image['interrupted'].get('type') in ('NoReadProgress','RepairBudgetExceeded'):
                        self.warnings.append(f"Track {track['track']} has unreadable sectors; later recorded tracks are still being attempted. Scene coverage remains unverified.")
                        self.log('WARNING','continuing_after_track_read_timeout',track=track['track'])
                        continue
                    break
        finally:disc.close()
    def load_saved(self):
        source=self.saved_source
        archive=source/'Original recovery' if (source/'Original recovery').is_dir() else source
        if not archive.is_dir():raise low.RecoveryError('Select a saved recovery folder or its Original recovery folder.')
        self.dest.mkdir(parents=True,exist_ok=False);self.log('INFO','saved_image_recovery_started',source=str(archive))
        previous_report=archive.parent/'recovery_report.json'
        if previous_report.is_file():
            previous=json.loads(previous_report.read_text(encoding='utf-8'))
            mounted=previous.get('mounted_filesystem')
            if mounted:
                from mounted_files import safe_name
                sources=[];missing=0
                for index,item in enumerate(mounted.get('copies',[]),1):
                    path=archive/'Data disc files'/Path(item['target']).name
                    if not path.is_file() or not path.stat().st_size:
                        missing+=1;self.log('ERROR','saved_video_file_missing',source=str(path),original_filename=item['name']);continue
                    sources.append({'path':str(path),'recording_id':item['name'],'truncated_source':not item.get('complete') or path.stat().st_size!=item['size_bytes'],'output_filename':f'{index:03}_{safe_name(Path(item["name"]).stem)}.mp4'})
                mounted['complete']=bool(mounted.get('complete') and not missing and len(sources)==len(mounted['files']) and not any(item['truncated_source'] for item in sources))
                self.saved_file_sources=sources
                self.report['mounted_filesystem']=mounted
                self.report['expected_recordings']=len(mounted['files'])
                self.report['missing_recordings']=missing+max(0,len(mounted['files'])-len(mounted.get('copies',[])))
                self.save();return
        # New-format sidecars contain exact absolute track coordinates.
        for state_path in sorted(archive.glob('track_*.bin.state.json')):
            try:
                state=json.loads(state_path.read_text());path=Path(state.get('path',''))
                if not path.is_file():path=archive/('track_'+state_path.name.split('_')[1].split('.')[0]+'.bin')
                if not path.is_file():path=path.with_suffix(path.suffix+'.partial')
                if path.is_file():self.images.append({**state,'path':str(path),'available_sectors':logical_size(path)//SECTOR})
            except Exception as error:self.failure('saved_image_state',error,path=str(state_path))
        if self.images:
            self.inventory=[{'track':i.get('track',n+1),'start':i['start'],'count':i['count']} for n,i in enumerate(self.images)];return
        # Legacy Handycam copies stored VTS metadata and one VOB stream separately.
        metadata_files=sorted(archive.glob('metadata_track_*.bin'))
        raws=[p for pattern in ('Handycam_original.mpg','Handycam_original.mpg.partial','Handycam_recovered.mpg') for p in archive.glob(pattern)]
        if not metadata_files or not raws:raise low.RecoveryError('No saved DVD metadata/video image pair was found. Existing files were not changed.')
        if len(metadata_files)!=1:raise low.RecoveryError('This legacy folder has multiple title metadata files but no recorded track coordinates. Insert the DVD and use Recover Videos to image every track.')
        meta=metadata_files[0];data=meta.read_bytes()
        from dvd_layout import parse_ifo_all
        parsed=parse_ifo_all(data);start=12288
        self.report['legacy_saved_image']=True
        self.warnings.append('Legacy saved images have no track inventory; VTS relative offsets were used for local extraction. Disc-wide discovery cannot be proven from a partial saved image.')
        self.images.append({'path':str(meta),'start':start,'count':len(data)//SECTOR,'available_sectors':len(data)//SECTOR,'complete':True,'bad_sectors':[],'track':2})
        raw=max(raws,key=logical_size);bad_file=archive.parent/'read_errors.json'
        bad=json.loads(bad_file.read_text()) if bad_file.exists() else []
        expected=max(last for rec in parsed['recordings'] for first,last in rec['ranges'])+1
        self.images.append({'path':str(raw),'start':start+parsed['video_offset'],'count':expected,'available_sectors':logical_size(raw)//SECTOR,'complete':logical_size(raw)//SECTOR>=expected and not bad,'bad_sectors':bad,'track':3})
        self.inventory=[{'track':i['track'],'start':i['start'],'count':i['count']} for i in self.images]
    def _raw_carve(self,image,index,covered=None):
        """Preserve MPEG-PS sector runs when scene metadata cannot be parsed."""
        runs=[];current=None;position=0
        covered=merge_ranges(covered or []);cover_index=0
        with Path(image['path']).open('rb') as source:
            while True:
                self.check();block=source.read(4*1024*1024)
                if not block:break
                for offset in range(0,len(block)-SECTOR+1,SECTOR):
                    sector=position+offset//SECTOR
                    absolute=image['start']+sector
                    while cover_index<len(covered) and covered[cover_index][1]<absolute:cover_index+=1
                    already_covered=cover_index<len(covered) and covered[cover_index][0]<=absolute<=covered[cover_index][1]
                    if not already_covered and block[offset:offset+4]==b'\x00\x00\x01\xba':
                        if current and current[1]+1==sector:current[1]=sector
                        else:
                            if current:runs.append(current)
                            current=[sector,sector]
                    elif current:runs.append(current);current=None
                position+=len(block)//SECTOR
        if current:runs.append(current)
        # Keep all pack spans together; frame clocks are rebuilt by conversion.
        if not runs:return None
        ranges=[(image['start']+a,image['start']+b) for a,b in runs]
        return {'id':f'raw_track_{index:02}','absolute_ranges':ranges,'duration_seconds':None,'metadata_unverified':True,'range_count':len(ranges)}
    def discover_recordings(self):
        candidates=[]
        for image in self.images:
            self.check()
            if not image.get('available_sectors'):continue
            try:
                found=scan_metadata_file(image['path'],cancel=self.check)
                for item in found:
                    item['absolute_metadata_sector']=image['start']+item['sector'];item['image_path']=image['path'];candidates.append(item)
            except Exception as error:self.failure('metadata_scan',error,path=image['path'])
        self.log('INFO','all_title_sets_scanned',candidate_count=len(candidates))
        recordings=[];seen=set()
        from disc_filesystem import scan_iso9660
        from mounted_files import VIDEO_SUFFIXES,safe_name
        filesystem=scan_iso9660(self.images,logger=self.log,cancel_callback=self.check)
        self.report['filesystems']=filesystem
        data_files=[item for item in filesystem.get('files',[]) if Path(item['relative_path']).suffix.lower() in VIDEO_SUFFIXES and not any(part.upper() in ('VIDEO_TS','DVD_RTAV','BDMV') for part in Path(item['relative_path']).parts)]
        for index,item in enumerate(data_files,1):
            ranges=[tuple(pair) for pair in item['absolute_ranges']]
            recordings.append({'id':f'data_file_{index:03}_{safe_name(Path(item["relative_path"]).stem)}','absolute_ranges':ranges,'extent_sizes':item['extent_sizes'],'byte_size':item['byte_size'],'original_filename':item['relative_path'],'output_filename':f'{index:03}_{safe_name(Path(item["relative_path"]).stem)}.mp4','duration_seconds':None,'filesystem_indexed':True})
            seen.add(tuple(ranges))
        for number,candidate in enumerate(candidates,1):
            metadata=candidate.get('metadata');self.metadata.append(candidate)
            if not metadata:
                self.warnings.append('A DVD metadata candidate could not be parsed. See detailed logs.')
                self.log('ERROR','metadata_parse_failed',candidate=candidate);continue
            origin=candidate['absolute_metadata_sector']
            backup_offset=metadata.get('backup_offset')
            # A standard IFO backup belongs to the earlier VTS origin, not to
            # its own physical header position. Never merge unrelated titles.
            primary=next((other for other in candidates
                          if other is not candidate and other.get('metadata')
                          and other['metadata'].get('backup_offset')
                          and other['absolute_metadata_sector']+other['metadata']['backup_offset']==origin
                          and other['metadata'].get('vts_last_sector')==metadata.get('vts_last_sector')
                          and other['metadata'].get('vtsi_last_sector')==metadata.get('vtsi_last_sector')),None)
            if primary:
                origin=primary['absolute_metadata_sector']
                self.log('INFO','backup_ifo_associated',backup_sector=candidate['absolute_metadata_sector'],primary_sector=origin)
            elif backup_offset and origin>=backup_offset:
                alternate=origin-backup_offset
                maximum=max((end for rec in metadata['recordings'] for start,end in rec['ranges']),default=0)
                def available(base):
                    first=base+metadata['video_offset'];last=first+maximum
                    return any(i['start']<=first and last<i['start']+i['available_sectors'] for i in self.images)
                if not available(origin) and available(alternate):
                    origin=alternate;candidate['association_inferred']=True
                    self.warnings.append('Backup-only metadata association requires review; all referenced footage is retained.')
                    self.log('WARNING','backup_only_ifo_associated',backup_sector=candidate['absolute_metadata_sector'],inferred_primary_sector=origin)
            candidate['resolved_vts_origin']=origin
            base=origin+metadata['video_offset']
            # Backup IFO files point outside their actual VOB stream; skip only
            # when another exact range set was already retained.
            for recording in metadata['recordings']:
                ranges=[(base+a,base+b) for a,b in recording['ranges']];identity=tuple(ranges)
                if identity in seen:
                    self.log('INFO','duplicate_recording_reference',metadata_index=number,recording=recording);continue
                seen.add(identity)
                recordings.append({**recording,'id':f"title_{number:02}_{recording['id']}",'absolute_ranges':ranges,'metadata_complete':metadata['complete'],'metadata_index':number,'metadata_unverified':bool(candidate.get('association_inferred')),'backup_association_inferred':bool(candidate.get('association_inferred'))})
            for a,b in metadata.get('unreferenced_cell_ranges',[]):
                ranges=[(base+a,base+b)]
                if tuple(ranges) not in seen:
                    recordings.append({'id':f'title_{number:02}_unreferenced_{a}','absolute_ranges':ranges,'duration_seconds':None,'metadata_unverified':True})
                    seen.add(tuple(ranges))
            if metadata.get('errors'):self.warnings.extend(metadata['errors'])
        covered=merge_ranges(pair for recording in recordings for pair in recording['absolute_ranges'])
        raw_found=False
        for index,image in enumerate(self.images,1):
            self.check();rec=self._raw_carve(image,index,covered)
            if rec:
                recordings.append(rec);raw_found=True
                self.log('WARNING','unindexed_mpeg_video_salvaged',recording=rec,image=image['path'])
        if raw_found:
            self.warnings.append('Additional MPEG video was found outside indexed recordings. It is retained separately; scene completeness is unverified.')
        if not recordings:
            self.warnings.append('No usable scene metadata or MPEG video found in copied track images.')
        self.report['expected_recordings']=len(recordings);self.report['discovered_recordings']=recordings;self.save()
        return recordings
    def extract_recordings(self,recordings):
        space=SectorSpace(self.images);sources=[];directory=self.dest/'Original recovery'/'Extracted recordings'
        for index,recording in enumerate(recordings,1):
            self.check();target=directory/f"{index:03}_{recording['id']}.vob"
            try:
                extraction=space.extract(recording['absolute_ranges'],target,self.check,recording.get('extent_sizes'));extraction.update(recording_id=recording['id'],expected_duration_seconds=recording.get('duration_seconds'),original_filename=recording.get('original_filename'))
                self.extraction.append(extraction)
                if extraction['missing_ranges']:self.log('WARNING','recording_partially_present',recording=recording,extraction=extraction)
                if extraction['written_bytes']:
                    item={**extraction,'recording_id':recording['id'],'expected_duration_seconds':recording.get('duration_seconds'),'known_read_errors':extraction['unreadable_sectors'],'metadata_unverified':recording.get('metadata_unverified',False),'backup_association_inferred':recording.get('backup_association_inferred',False)}
                    if recording.get('output_filename'):item['output_filename']=recording['output_filename']
                    sources.append(item)
                else:self.log('ERROR','recording_not_copied',recording=recording,missing_ranges=extraction['missing_ranges'])
            except low.Cancelled:raise
            except Exception as error:self.failure('extract_recording',error,recording=recording)
        self.report['missing_recordings']=sum(not e.get('written_bytes') for e in self.extraction)+max(0,len(recordings)-len(self.extraction));self.save()
        return sources
    def run(self):
        try:
            if self.dest.drive.upper()!='D:':raise low.RecoveryError('Choose a destination on D:.')
            mounted=None
            if self.saved_source:self.load_saved();mounted=self.saved_file_sources
            else:mounted=self.mounted_sources()
            if mounted is None:
                if not self.saved_source:self.image_disc()
                recordings=self.discover_recordings()
                if not recordings:raise low.RecoveryError('No playable video records were found. Raw track images and detailed scan logs are saved for further recovery.')
                sources=self.extract_recordings(recordings)
            else:sources=mounted;recordings=[]
            if not sources:raise low.RecoveryError('The recording ranges are not present in the copied tracks. Images, metadata and detailed failure logs are preserved.')
            import sys
            base=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent
            pipeline=MediaPipeline(base/'ffmpeg.exe',base/'ffprobe.exe',self.dest,event=self.log,status=self.status,cancel=self.cancel)
            media=pipeline.convert(sources)
            read_complete=bool(self.images and all(i.get('complete') for i in self.images) and len(self.images)==len(self.inventory))
            discovery_complete=bool(self.metadata and all((m.get('metadata') or {}).get('complete') for m in self.metadata) and not any(r.get('metadata_unverified') for r in recordings) and not any(m.get('association_inferred') for m in self.metadata))
            if recordings and all(r.get('filesystem_indexed') for r in recordings):discovery_complete=bool(self.report.get('filesystems',{}).get('complete'))
            if mounted is not None:
                read_complete=bool(self.report['mounted_filesystem']['complete'])
                discovery_complete=not self.report['mounted_filesystem']['enumeration_errors']
            complete=bool(read_complete and discovery_complete and not self.report.get('legacy_saved_image') and media['complete'] and not self.failures and not self.report.get('missing_recordings'))
            outcome='complete' if complete else ('failed' if not (media['validated_count']+media['partial_count']) else 'partial')
            self.report.update(status=outcome,complete=complete,recordings=len(sources),validated_count=media['validated_count'],partial_count=media['partial_count'],failed_count=media['failed_count'],unreadable_sectors=len({b for i in self.images for b in i.get('bad_sectors',[])}),combined_video=media['combined_video'],media_report=media['report_path'],source_read_complete=read_complete,discovery_complete=discovery_complete,full_decode_check='passed' if complete else 'incomplete_or_damaged',clips=media['clips'])
            self.save();self.log.close(self.report['status'],recovery_report=str(self.dest/'recovery_report.json'))
            self.status(f"{media['validated_count']} validated, {media['partial_count']} partial, {media['failed_count']} failed. Detailed logs saved.",100)
            return self.report
        except (low.Cancelled,PipelineCancelled) as error:
            self.report.update(status='stopped',complete=False);self.failure('cancelled',error);self.log.close('stopped');raise low.Cancelled(str(error)) from error
        except Exception as error:
            self.report.update(status='failed',complete=False);self.failure('recovery',error);self.log.close('failed');setattr(error,'log_directory',str(self.log.directory));raise


def inspect_layout(drive,status,cancel,logger):
    disc=low.Disc(drive);disc.logger=logger
    try:
        disc.wait_ready(status,cancel);tracks=disc.tracks();counts=[];issues=[];raw_video_tracks=[]
        from dvd_layout import parse_ifo_all
        for track in tracks:
            if cancel.is_set():raise low.Cancelled('Disc inspection stopped.')
            if track['count']<=8192:
                try:
                    data=b''.join(disc.sectors(track['start']+p,min(32,track['count']-p)) for p in range(0,track['count'],32))
                    if data.startswith(b'DVDVIDEO-VTS'):counts.append(len(parse_ifo_all(data)['recordings']))
                except low.ReadError as error:logger.exception('inspect_metadata_read',error,track=track);issues.append(str(error))
            else:
                try:
                    header=disc.sectors(track['start'],1)
                    if header.startswith(b'\x00\x00\x01\xba'):raw_video_tracks.append(track['track'])
                except low.ReadError as error:logger.exception('inspect_video_track_read',error,track=track);issues.append(str(error))
        result={'tracks':tracks,'title_sets':len(counts),'recordings':sum(counts),'issues':issues,'readable_raw_video_tracks':raw_video_tracks}
        if not counts and len(drive)==2 and drive[1]==':':
            from mounted_files import collect_videos
            def check():
                if cancel.is_set():raise low.Cancelled('Disc inspection stopped.')
            mounted=collect_videos(Path(drive+'\\'),logger,check)
            if mounted:result['data_video_files']=len(mounted['files']);result['file_enumeration_errors']=mounted['enumeration_errors']
        logger('INFO','inspection_complete',**result);return result
    finally:disc.close()
