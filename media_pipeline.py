"""Read-only-source conversion with full per-recording decode and count checks.

Every external command retains its argv, result, full stdout, and full stderr.
No successful encode is treated as proof that the source was undamaged.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
from fractions import Fraction


class PipelineError(Exception):
    pass


class PipelineCancelled(PipelineError):
    pass


def _number(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (ValueError, TypeError, ZeroDivisionError):
        return default


def _rate(value):
    try:
        result = float(Fraction(str(value)))
        return result if 0 < result <= 240 else 0.0
    except (ValueError, TypeError, ZeroDivisionError):
        return 0.0


def _unused(path):
    path = Path(path)
    if not path.exists():
        return path
    for index in range(2, 100000):
        candidate = path.with_name(f'{path.stem}_{index:02}{path.suffix}')
        if not candidate.exists():
            return candidate
    raise PipelineError('Cannot choose a new output name without replacing an existing file.')


def _write_json(path, data):
    path = Path(path)
    pending = path.with_suffix(path.suffix + '.pending')
    pending.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    pending.replace(path)


class MediaPipeline:
    """Convert copied files only; never open or modify the optical disc.

    ``event`` is callable(level, event_name, **fields). ``status`` accepts
    (message, percent). Sources may be paths or dictionaries containing path,
    expected_duration_seconds, and recording_id. A report is saved after every
    clip so cancellation/failure still leaves the earlier validation evidence.
    """

    def __init__(self, ffmpeg, ffprobe, destination, event=None, status=None, cancel=None):
        self.ffmpeg = Path(ffmpeg)
        self.ffprobe = Path(ffprobe)
        self.dest = Path(destination)
        self.event = event or (lambda *args, **kwargs: None)
        self.status = status or (lambda *args: None)
        self.cancel = cancel if cancel is not None else threading.Event()
        self.dest.mkdir(parents=True, exist_ok=True)
        self.logs = _unused(self.dest / 'logs' / 'media')
        self.logs.mkdir(parents=True, exist_ok=True)
        self.report_path = _unused(self.dest / 'media_report.json')
        self.command_index = 0
        self.stage_percent = 70
        self.progress_expected = None
        self.progress_fps = None

    def check(self):
        if self.cancel.is_set():
            raise PipelineCancelled('Stopped. Originals, completed clips, and detailed logs remain saved.')

    def _command(self, argv, label, timeout=None):
        self.check()
        self.command_index += 1
        stem = self.logs / f'{self.command_index:04}_{label}'
        stdout_path = stem.with_suffix('.stdout.log')
        stderr_path = stem.with_suffix('.stderr.log')
        command_path = stem.with_suffix('.command.json')
        argv = [str(arg) for arg in argv]
        progress_path = stem.with_suffix('.progress.log')
        is_ffmpeg=Path(argv[0]).name.lower()=='ffmpeg.exe'
        if is_ffmpeg:argv[1:1]=['-progress',str(progress_path),'-stats_period','1']
        record = {'argv': argv, 'started_utc': datetime.now(timezone.utc).isoformat(),
                  'stdout': str(stdout_path), 'stderr': str(stderr_path)}
        started = time.monotonic()
        process = None
        last_update=0;counted_frames=0;read_offset=0;carry=b''
        self.event('info', 'media_command_started', label=label, **record)
        _write_json(command_path, record)
        try:
            with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                while process.poll() is None:
                    if self.cancel.wait(.1):
                        raise PipelineCancelled('Stopped. Original copies and completed videos remain saved.')
                    if timeout is not None and time.monotonic() - started > timeout:
                        raise PipelineError(f'Media command timed out after {timeout} seconds. See {stderr_path}')
                    elapsed=time.monotonic()-started
                    if elapsed-last_update>=1:
                        last_update=elapsed;done=None
                        try:
                            if is_ffmpeg and progress_path.exists():
                                values=[line.split('=',1)[1] for line in progress_path.read_text(errors='replace')[-8192:].splitlines() if line.startswith('out_time_us=')]
                                if values:done=float(values[-1])/1e6
                            elif label.endswith('_full_decode') and self.progress_fps:
                                with stdout_path.open('rb') as decoded:
                                    decoded.seek(read_offset);chunk=decoded.read(4*1024*1024);read_offset+=len(chunk)
                                lines=(carry+chunk).split(b'\n');carry=lines.pop()
                                counted_frames+=sum(b'media_type=video' in line for line in lines);done=counted_frames/self.progress_fps
                        except (OSError,ValueError):pass
                        self.status(f"{label.replace('_',' ')} · {int(elapsed)}s elapsed",{'phase':label,'done':done,'total':self.progress_expected,'overall':self.stage_percent})
            record['returncode'] = process.returncode
        except BaseException as error:
            record['exception'] = str(error)
            record['traceback'] = traceback.format_exc()
            raise
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process is not None:
                record['returncode'] = process.returncode
            record['elapsed_seconds'] = round(time.monotonic() - started, 3)
            record['finished_utc'] = datetime.now(timezone.utc).isoformat()
            _write_json(command_path, record)
            self.event('error' if record.get('exception') or record.get('returncode') else 'info',
                       'media_command_finished', label=label, command_log=str(command_path), **record)
        record['stderr_text'] = stderr_path.read_text(encoding='utf-8', errors='replace')
        return record

    def validate_media(self, path, log_prefix=None, expected_duration_seconds=None):
        """Decode the entire file, counting frames and audio samples independently.

        The returned source duration comes from decoded video frame count at the
        recorded rate, not MPEG-PS container duration, which can cross PTS resets.
        Timestamp discontinuities are retained for diagnosis. All streams are
        decoded; the first video and every audio stream have separate counters.
        """
        self.check()
        path = Path(path)
        label = log_prefix or path.stem
        result = {'path': str(path), 'size_bytes': path.stat().st_size, 'readable': False,
                  'decode_clean': False, 'issues': [], 'logs': [], 'video_frames': 0,
                  'video_duration_seconds': 0.0, 'audio': [], 'timestamp_discontinuities': []}
        header = self._command([self.ffprobe, '-hide_banner', '-v', 'error', '-show_streams',
                                '-show_format', '-of', 'json', path], f'{label}_metadata')
        result['logs'].append({k: header[k] for k in ('stdout', 'stderr', 'returncode')})
        try:
            metadata = json.loads(Path(header['stdout']).read_text(encoding='utf-8', errors='replace'))
        except (ValueError, OSError) as error:
            result['issues'].append('Metadata could not be read: ' + str(error))
            return result
        result['metadata'] = metadata
        video = [s for s in metadata.get('streams', []) if s.get('codec_type') == 'video']
        audio = [s for s in metadata.get('streams', []) if s.get('codec_type') == 'audio']
        if not video:
            result['issues'].append('No decodable video stream was found.')
            return result
        if len(video) != 1:
            result['issues'].append(f'{len(video)} video streams found; conversion preserves only the first. Additional streams remain in the source.')
        selected = video[0]
        fps = _rate(selected.get('r_frame_rate')) or _rate(selected.get('avg_frame_rate'))
        result.update({'video_stream_index': selected['index'], 'fps': fps,
                       'fps_fraction': selected.get('r_frame_rate') if _rate(selected.get('r_frame_rate')) else selected.get('avg_frame_rate'),
                       'width': selected.get('width'), 'height': selected.get('height'),
                       'sample_aspect_ratio': selected.get('sample_aspect_ratio'),
                       'container_duration_seconds': _number(metadata.get('format', {}).get('duration'))})
        if not fps:
            result['issues'].append('The recorded frame rate is unavailable; completeness cannot be verified.')
        counts = {s['index']: {'stream_index': s['index'], 'frames': 0, 'samples': 0,
                              'sample_rate': _number(s.get('sample_rate')), 'first_pts': None,
                              'last_pts': None, 'timestamp_discontinuity_count': 0} for s in [selected] + audio}
        self.progress_expected=_number(expected_duration_seconds) or result['container_duration_seconds'] or None
        self.progress_fps=fps or None
        decoded = self._command([self.ffprobe, '-hide_banner', '-v', 'warning', '-show_frames',
                                 '-show_entries', 'frame=media_type,stream_index,best_effort_timestamp_time,pts_time,duration_time,pkt_duration_time,nb_samples',
                                 '-of', 'compact=p=0:nk=0', path], f'{label}_full_decode')
        result['logs'].append({k: decoded[k] for k in ('stdout', 'stderr', 'returncode')})
        with Path(decoded['stdout']).open(encoding='utf-8', errors='replace') as frames:
            for line_number, line in enumerate(frames):
                if line_number % 4096 == 0:
                    self.check()
                fields = dict(piece.split('=', 1) for piece in line.rstrip().split('|') if '=' in piece)
                index = int(fields.get('stream_index', '-1'))
                if index not in counts or fields.get('media_type') not in ('audio', 'video'):
                    continue
                count = counts[index]
                count['frames'] += 1
                count['samples'] += int(fields.get('nb_samples', '0'))
                stamp = fields.get('best_effort_timestamp_time', fields.get('pts_time'))
                pts = _number(stamp, None)
                if pts is not None:
                    if count['first_pts'] is None:
                        count['first_pts'] = pts
                    previous = count['last_pts']
                    if previous is not None:
                        delta = pts - previous
                        # Frame reordering is already resolved by best-effort PTS.
                        if delta < -.001 or delta > max(.5, 5 / fps if fps else .5):
                            count['timestamp_discontinuity_count'] += 1
                            if len(result['timestamp_discontinuities']) < 1000:
                                result['timestamp_discontinuities'].append({'stream_index': index,
                                    'frame': count['frames'], 'previous_seconds': previous,
                                    'current_seconds': pts, 'delta_seconds': delta})
                    count['last_pts'] = pts
        vcount = counts[selected['index']]
        result['video_frames'] = vcount['frames']
        result['video_duration_seconds'] = vcount['frames'] / fps if fps else 0.0
        result['video_timestamp_discontinuity_count'] = vcount['timestamp_discontinuity_count']
        result['video_timestamp_span_seconds'] = ((vcount['last_pts'] - vcount['first_pts'] + 1 / fps)
            if fps and vcount['last_pts'] is not None else 0.0)
        for stream in audio:
            count = counts[stream['index']]
            count['duration_seconds'] = count['samples'] / count['sample_rate'] if count['sample_rate'] else 0.0
            result['audio'].append(count)
        result['readable'] = result['video_frames'] > 0 and all(a['samples'] > 0 for a in result['audio'])
        # FFprobe's show_frames also attempts to open DVD navigation packets as
        # a decoder. Their absence of a decoder does not indicate video damage.
        # Retain that diagnostic in full stderr, but ignore only a precisely
        # identified non-audiovisual data stream for completeness accounting.
        meaningful, ignored = [], []
        data_indices = {s['index'] for s in metadata.get('streams', []) if s.get('codec_type') == 'data'}
        for line in decoded['stderr_text'].splitlines():
            match = re.fullmatch(r'Unsupported codec with id \d+ for input stream (\d+)', line.strip())
            if match and int(match.group(1)) in data_indices:
                ignored.append(line)
            elif line.strip():
                meaningful.append(line)
        result['ignored_non_video_diagnostics'] = ignored
        result['decode_clean'] = (header['returncode'] == 0 and decoded['returncode'] == 0
                                  and not header['stderr_text'].strip() and not meaningful)
        if not result['readable']:
            result['issues'].append('Video or an expected audio stream did not fully decode into frames.')
        if not result['decode_clean']:
            result['issues'].append('The decoder reported errors or warnings; inspect the complete FFprobe stderr logs. A clean MP4 cannot prove missing source data were recovered.')
        if expected_duration_seconds is not None:
            expected = _number(expected_duration_seconds)
            result['expected_duration_seconds'] = expected
            tolerance = max(.5, expected * .02)
            if expected > 0 and abs(result['video_duration_seconds'] - expected) > tolerance:
                result['issues'].append(f'Decoded duration {result["video_duration_seconds"]:.3f}s differs from DVD metadata {expected:.3f}s (tolerance {tolerance:.3f}s).')
        self.event('info' if not result['issues'] else 'warning', 'media_validation',
                   path=str(path), video_frames=result['video_frames'],
                   video_duration_seconds=result['video_duration_seconds'],
                   decode_clean=result['decode_clean'], issues=result['issues'], logs=result['logs'],
                   timestamp_discontinuity_count=sum(c['timestamp_discontinuity_count'] for c in counts.values()))
        return result

    @staticmethod
    def compare_media(source, output):
        """Return count/timeline mismatches; never approve by encode exit alone."""
        issues = []
        if not output.get('readable') or not output.get('decode_clean'):
            issues.append('Converted output did not pass the complete decode check.')
        if output.get('video_frames', 0) != source.get('video_frames', 0):
            issues.append(f'Video frame count changed: source {source.get("video_frames", 0)}, output {output.get("video_frames", 0)}.')
        original_duration = source.get('video_duration_seconds', 0.0)
        tolerance = max(.1, 2 / source['fps'] if source.get('fps') else .1)
        if abs(output.get('video_duration_seconds', 0.0) - original_duration) > tolerance:
            issues.append('Decoded video duration differs from the source frame count and rate.')
        # A valid frame count with repeated/reset timestamps is still incomplete playback.
        if output.get('video_timestamp_discontinuity_count', 0):
            issues.append('Converted output still contains video timestamp resets or gaps.')
        if abs(output.get('video_timestamp_span_seconds', 0.0) - original_duration) > tolerance:
            issues.append('Converted video playback timeline does not cover every decoded source frame at its recorded rate.')
        original_audio, converted_audio = source.get('audio', []), output.get('audio', [])
        if len(original_audio) != len(converted_audio):
            issues.append(f'Audio stream count changed: source {len(original_audio)}, output {len(converted_audio)}.')
        for index, (a, b) in enumerate(zip(original_audio, converted_audio), 1):
            # AAC encoder delay and end padding may add less than two codec frames.
            if abs(a['duration_seconds'] - b['duration_seconds']) > .15:
                issues.append(f'Audio {index} decoded duration changed: source {a["duration_seconds"]:.3f}s, output {b["duration_seconds"]:.3f}s.')
        return issues

    def _encode(self, source, target, validation, label):
        # MPEG-PS timestamps can restart inside a single DVD program/cell. Explicit
        # frame/sample clocks preserve every decoded frame instead of FFmpeg's
        # automatic frame sync dropping everything after the first PTS reset.
        rate = validation['fps_fraction']
        args = [self.ffmpeg, '-hide_banner', '-nostdin', '-n', '-v', 'warning',
                '-i', source, '-map', '0:v:0', '-map', '0:a?',
                '-vf', f'bwdif=mode=send_frame,setpts=N/({rate}*TB)',
                '-af', 'asetpts=N/SR/TB', '-fps_mode', 'passthrough',
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', target]
        self.progress_expected=validation['video_duration_seconds'];self.progress_fps=validation.get('fps')
        return self._command(args, label)

    def convert(self, sources):
        for tool in (self.ffmpeg, self.ffprobe):
            if not tool.is_file():
                raise PipelineError('Required bundled media tool is missing: ' + str(tool))
        sources = [dict(source) if isinstance(source, dict) else {'path': source} for source in sources]
        report = {'schema_version': 1, 'complete': False, 'clips': [], 'validated_count': 0,
                  'partial_count': 0, 'failed_count': 0, 'combined_video': None,
                  'report_path': str(self.report_path), 'logs_directory': str(self.logs),
                  'source_files_modified': False, 'issues': [],
                  'timestamp_policy': 'Every decoded video frame and audio sample is kept using continuous frame/sample clocks; original timing and files are preserved in source validation.'}
        output_dir = self.dest / 'MP4 clips'
        output_dir.mkdir(exist_ok=True)
        _write_json(self.report_path, report)
        try:
            for index, item in enumerate(sources, 1):
                self.check()
                source = Path(item['path'])
                clip = {'recording_id': item.get('recording_id', index), 'source': str(source),
                        'target': None, 'status': 'failed', 'issues': [], 'logs': []}
                report['clips'].append(clip)
                try:
                    self.stage_percent=70+24*(index-1)/max(1,len(sources))
                    self.status(f'Checking and converting recording {index} of {len(sources)}',
                                70 + 24 * (index - 1) / max(1, len(sources)))
                    original = self.validate_media(source, f'clip_{index:03}_source', item.get('expected_duration_seconds'))
                    known = item.get('unreadable_sectors') or item.get('known_read_errors')
                    if known:
                        clip['known_read_errors'] = known
                        original['known_read_errors'] = known
                        amount = len(known) if isinstance(known, (list, tuple, dict)) else known
                        original['issues'].append(f'The source contains known disc read failures ({amount}); decoded frames may conceal damaged or missing data. This recording is partial.')
                    if item.get('truncated_source'):
                        original['issues'].append('Only part of the original recording was copied. Missing source ranges remain listed in the recovery report.')
                    if item.get('metadata_unverified'):
                        clip['metadata_unverified'] = True
                        if item.get('backup_association_inferred'):
                            clip['backup_association_inferred']=True
                            original['issues'].append('This clip uses an inferred backup scene index. It may overlap current recordings; its scene boundaries and completeness are unverified.')
                        else:
                            original['issues'].append('Video decoded from a raw image without a verified scene table. Scene boundaries and disc-wide recording coverage remain unverified.')
                    clip['source_validation'] = original
                    if not original['readable'] or not original.get('fps'):
                        raise PipelineError('The original recording has no usable video/audio or frame rate. Its copied file is preserved.')
                    output_name = item.get('output_filename', item.get('output_name', f'Handycam_{index:03}.mp4'))
                    if (not isinstance(output_name, str) or Path(output_name).name != output_name
                        or any(character in output_name for character in '<>:"/\\|?*')
                        or Path(output_name).suffix.lower() != '.mp4'):
                        raise PipelineError('The requested output filename must be a simple MP4 filename, without a path.')
                    target = _unused(output_dir / output_name)
                    pending = _unused(target.with_name(target.stem + '.incomplete.mp4'))
                    clip['target'] = str(pending)
                    command = self._encode(source, pending, original, f'clip_{index:03}_conversion')
                    clip['logs'].append({k: command[k] for k in ('stdout', 'stderr', 'returncode')})
                    if command['returncode']:
                        raise PipelineError('Video conversion failed; full FFmpeg stderr is saved at ' + command['stderr'])
                    converted = self.validate_media(pending, f'clip_{index:03}_output')
                    clip['output_validation'] = converted
                    clip['issues'].extend(self.compare_media(original, converted))
                    clip['issues'].extend(original['issues'])
                    if self.compare_media(original, converted):
                        clip['status'] = 'failed'
                    else:
                        if clip['issues'] and not target.stem.upper().endswith('_PARTIAL'):
                            target = _unused(target.with_name(target.stem + '_PARTIAL.mp4'))
                        pending.rename(target)
                        clip['target'] = str(target)
                        converted['validated_from_path'] = converted['path']
                        converted['path'] = str(target)
                        clip['status'] = 'partial' if clip['issues'] else 'validated'
                    self.event('info' if clip['status'] == 'validated' else 'warning',
                               'recording_finished', **clip)
                except PipelineCancelled:
                    clip['status'] = 'cancelled'
                    clip['issues'].append('Stopped by the user. Completed source copies and any incomplete output remain saved.')
                    raise
                except Exception as error:
                    clip['status'] = 'failed'
                    clip['issues'].append(str(error))
                    clip['traceback'] = traceback.format_exc()
                    self.event('error', 'recording_failed', **clip)
                finally:
                    self._counts(report)
                    _write_json(self.report_path, report)
            validated = [clip for clip in report['clips'] if clip['status'] == 'validated']
            if validated:
                self.stage_percent=95
                self.status('Joining validated recordings and checking every combined frame', 95)
                try:
                    self._combine(validated, report)
                except PipelineCancelled:
                    raise
                except Exception as error:
                    report['issues'].append('Combined video: ' + str(error))
                    report['combined_traceback'] = traceback.format_exc()
                    self.event('error', 'combined_video_failed', error=str(error), traceback=traceback.format_exc())
            else:
                report['issues'].append('No recording passed all source and output checks. Preserved sources and any partial playable MP4s remain available.')
            report['complete'] = bool(sources and report['validated_count'] == len(sources)
                                      and report.get('combined_video') and not report['issues'])
            _write_json(self.report_path, report)
            return report
        except PipelineCancelled:
            report['cancelled'] = True
            _write_json(self.report_path, report)
            raise

    @staticmethod
    def _counts(report):
        report['validated_count'] = sum(c['status'] == 'validated' for c in report['clips'])
        report['partial_count'] = sum(c['status'] == 'partial' for c in report['clips'])
        report['failed_count'] = sum(c['status'] in ('failed', 'cancelled') for c in report['clips'])

    def _combine(self, clips, report):
        # Concatenation with stream copy requires identical stream layouts.
        layouts = []
        for clip in clips:
            streams = clip['output_validation']['metadata']['streams']
            layouts.append([(s.get('codec_type'), s.get('codec_name'), s.get('width'), s.get('height'),
                             s.get('sample_rate'), s.get('channels'), s.get('r_frame_rate'),
                             s.get('sample_aspect_ratio')) for s in streams if s.get('codec_type') in ('video', 'audio')])
        if any(layout != layouts[0] for layout in layouts[1:]):
            raise PipelineError('Recordings have different video/audio formats. Each validated clip is saved individually; automatic joining was skipped to prevent stream loss.')
        listing = _unused(self.dest / 'concat_validated.txt')
        listing.write_text('\n'.join("file '" + str(Path(c['target']).resolve()).replace('\\', '/').replace("'", "'\\''") + "'" for c in clips), encoding='utf-8')
        target = _unused(self.dest / 'Handycam_all_videos.mp4')
        pending = _unused(target.with_name(target.stem + '.incomplete.mp4'))
        joined = self._command([self.ffmpeg, '-hide_banner', '-nostdin', '-n', '-v', 'warning',
                                '-f', 'concat', '-safe', '0', '-i', listing, '-map', '0:v:0',
                                '-map', '0:a?', '-c', 'copy', '-movflags', '+faststart', pending], 'join_validated')
        report['combined_log'] = {k: joined[k] for k in ('stdout', 'stderr', 'returncode')}
        if joined['returncode']:
            raise PipelineError('Joining failed; full details are saved at ' + joined['stderr'])
        validation = self.validate_media(pending, 'combined_output')
        report['combined_validation'] = validation
        total_frames = sum(c['output_validation']['video_frames'] for c in clips)
        duration = sum(c['output_validation']['video_duration_seconds'] for c in clips)
        report['combined_recording_ids'] = [c['recording_id'] for c in clips]
        report['combined_expected_frames'] = total_frames
        if not validation['readable'] or not validation['decode_clean'] or validation['video_frames'] != total_frames:
            raise PipelineError('Joined video failed complete decoding or its frame count does not match the sum of validated clips. Individual clips are preserved.')
        # Per-clip AAC padding can add a few milliseconds of video timeline per join.
        if abs(validation['video_timestamp_span_seconds'] - duration) > max(.25, .05 * len(clips)):
            raise PipelineError('Joined playback duration does not match all validated clips. Individual clips are preserved.')
        audio_expected = [sum(c['output_validation']['audio'][i]['duration_seconds'] for c in clips)
                          for i in range(len(clips[0]['output_validation']['audio']))]
        if len(validation['audio']) != len(audio_expected) or any(
            abs(actual['duration_seconds'] - expected) > max(.15, .03 * len(clips))
            for actual, expected in zip(validation['audio'], audio_expected)):
            raise PipelineError('Joined audio duration or stream count differs from the validated clips. Individual clips are preserved.')
        pending.rename(target)
        validation['validated_from_path'] = validation['path']
        validation['path'] = str(target)
        report['combined_video'] = str(target)
        report['combined_includes_only_validated_clips'] = True

