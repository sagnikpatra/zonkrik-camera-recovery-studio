"""Recovery completeness checks, including a real MPEG timestamp-reset fixture."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from media_pipeline import MediaPipeline, PipelineCancelled

WORK = Path(r'D:\Downloads\Processing and video Encoding\2026-10-15\i-x20\work\media-tests')
WORK.mkdir(parents=True, exist_ok=True)


def validation(frames=25, clean=True, duration=1.0):
    return {'path': 'fixture', 'readable': True, 'decode_clean': clean,
            'video_frames': frames, 'fps': 25.0, 'fps_fraction': '25/1',
            'video_duration_seconds': duration, 'video_timestamp_span_seconds': duration,
            'video_timestamp_discontinuity_count': 0, 'audio': [],
            'issues': [] if clean else ['Source decoder reported corruption.'],
            'metadata': {'streams': [{'codec_type': 'video', 'codec_name': 'h264',
                                     'width': 160, 'height': 120, 'r_frame_rate': '25/1'}]}}


class ComparisonTests(unittest.TestCase):
    def test_frame_loss_fails_even_with_clean_output_decode(self):
        issues = MediaPipeline.compare_media(validation(frames=100, duration=4), validation(frames=25))
        self.assertTrue(any('frame count' in issue for issue in issues))

    def test_timestamp_reset_fails_even_when_frame_count_matches(self):
        output = validation(frames=50, duration=2)
        output['video_timestamp_span_seconds'] = 1
        output['video_timestamp_discontinuity_count'] = 1
        self.assertTrue(MediaPipeline.compare_media(validation(frames=50, duration=2), output))

    def test_audio_truncation_fails(self):
        source = validation()
        output = validation()
        source['audio'] = [{'duration_seconds': 20}]
        output['audio'] = [{'duration_seconds': 1}]
        self.assertTrue(any('Audio' in issue for issue in MediaPipeline.compare_media(source, output)))

    def test_aac_padding_is_allowed(self):
        source = validation()
        output = validation()
        source['audio'] = [{'duration_seconds': 1}]
        output['audio'] = [{'duration_seconds': 1.024}]
        self.assertEqual(MediaPipeline.compare_media(source, output), [])


class FailureAccountingTests(unittest.TestCase):
    def test_failure_does_not_skip_later_recordings_and_combined_excludes_it(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            pipeline = MediaPipeline(PROJECT/'ffmpeg.exe', PROJECT/'ffprobe.exe', folder)
            calls, combined = [], []
            def inspect(path, label, expected=None):
                calls.append(str(path))
                if str(path).endswith('bad.vob'):
                    raise OSError('damaged input')
                return validation()
            def encode(source, target, original, label):
                Path(target).write_bytes(b'playable fixture')
                return {'returncode': 0, 'stdout': 'stdout.log', 'stderr': 'stderr.log'}
            def join(clips, report):
                combined.extend(c['recording_id'] for c in clips)
                report['combined_video'] = 'validated-combined.mp4'
            with patch.object(pipeline, 'validate_media', side_effect=inspect), \
                 patch.object(pipeline, '_encode', side_effect=encode), \
                 patch.object(pipeline, '_combine', side_effect=join):
                report = pipeline.convert(['bad.vob', 'good.vob', 'later.vob'])
            self.assertEqual(report['failed_count'], 1)
            self.assertEqual(report['validated_count'], 2)
            self.assertEqual(combined, [2, 3])
            self.assertFalse(report['complete'])
            saved = json.loads(Path(report['report_path']).read_text())
            self.assertIn('damaged input', saved['clips'][0]['issues'][0])
            self.assertIn('traceback', saved['clips'][0])

    def test_source_corruption_cannot_be_hidden_by_clean_mp4(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            pipeline = MediaPipeline(PROJECT/'ffmpeg.exe', PROJECT/'ffprobe.exe', folder)
            def inspect(path, label, expected=None):
                return validation(clean=not str(path).endswith('.vob'))
            def encode(source, target, original, label):
                Path(target).write_bytes(b'playable fixture')
                return {'returncode': 0, 'stdout': 'stdout.log', 'stderr': 'stderr.log'}
            with patch.object(pipeline, 'validate_media', side_effect=inspect), \
                 patch.object(pipeline, '_encode', side_effect=encode), \
                 patch.object(pipeline, '_combine') as join:
                report = pipeline.convert(['damaged.vob'])
            self.assertEqual(report['partial_count'], 1)
            self.assertFalse(report['complete'])
            self.assertIsNone(report['combined_video'])
            self.assertTrue(Path(report['clips'][0]['target']).exists())
            join.assert_not_called()

    def test_known_bad_sectors_remain_partial_when_decoder_conceals_damage(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            pipeline = MediaPipeline(PROJECT/'ffmpeg.exe', PROJECT/'ffprobe.exe', folder)
            def encode(source, target, original, label):
                Path(target).write_bytes(b'playable fixture')
                return {'returncode': 0, 'stdout': 'stdout.log', 'stderr': 'stderr.log'}
            with patch.object(pipeline, 'validate_media', side_effect=lambda *a: validation()), \
                 patch.object(pipeline, '_encode', side_effect=encode), \
                 patch.object(pipeline, '_combine') as join:
                report = pipeline.convert([{'path': 'damaged.vob', 'unreadable_sectors': [1234]}])
            self.assertEqual(report['partial_count'], 1)
            self.assertEqual(report['clips'][0]['known_read_errors'], [1234])
            self.assertTrue(report['clips'][0]['target'].endswith('_PARTIAL.mp4'))
            self.assertFalse(report['complete'])
            join.assert_not_called()

    def test_truncated_source_and_explicit_partial_filename(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            pipeline = MediaPipeline(PROJECT/'ffmpeg.exe', PROJECT/'ffprobe.exe', folder)
            def encode(source, target, original, label):
                Path(target).write_bytes(b'playable fixture')
                return {'returncode': 0, 'stdout': 'stdout.log', 'stderr': 'stderr.log'}
            with patch.object(pipeline, 'validate_media', side_effect=lambda *a: validation()), \
                 patch.object(pipeline, '_encode', side_effect=encode):
                report = pipeline.convert([{'path': 'truncated.vob', 'truncated_source': True,
                                             'output_filename': 'Handycam_035_PARTIAL.mp4'}])
            self.assertEqual(report['partial_count'], 1)
            self.assertEqual(Path(report['clips'][0]['target']).name, 'Handycam_035_PARTIAL.mp4')
            self.assertFalse(report['complete'])

    def test_unindexed_video_does_not_claim_physical_read_damage(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            pipeline = MediaPipeline(PROJECT/'ffmpeg.exe', PROJECT/'ffprobe.exe', folder)
            def encode(source, target, original, label):
                Path(target).write_bytes(b'playable fixture')
                return {'returncode': 0, 'stdout': 'stdout.log', 'stderr': 'stderr.log'}
            with patch.object(pipeline, 'validate_media', side_effect=lambda *a: validation()), \
                 patch.object(pipeline, '_encode', side_effect=encode):
                report = pipeline.convert([{'path': 'raw.vob', 'metadata_unverified': True}])
            clip = report['clips'][0]
            self.assertEqual(clip['status'], 'partial')
            self.assertTrue(clip['metadata_unverified'])
            self.assertNotIn('known_read_errors', clip)
            self.assertTrue(any('scene table' in issue for issue in clip['issues']))
            self.assertFalse(any('disc read failures' in issue for issue in clip['issues']))

    def test_cancelled_command_retains_command_and_error_logs(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            cancel = threading.Event()
            pipeline = MediaPipeline(PROJECT/'ffmpeg.exe', PROJECT/'ffprobe.exe', folder, cancel=cancel)
            timer = threading.Timer(.2, cancel.set)
            timer.start()
            try:
                with self.assertRaises(PipelineCancelled):
                    pipeline._command([sys.executable, '-c', 'import time; print("started", flush=True); time.sleep(20)'], 'cancel_fixture')
            finally:
                timer.cancel()
            commands = list(pipeline.logs.glob('*.command.json'))
            self.assertEqual(len(commands), 1)
            record = json.loads(commands[0].read_text())
            self.assertIn('Stopped', record['exception'])
            self.assertTrue(Path(record['stderr']).is_file())
            self.assertIn('started', Path(record['stdout']).read_text())


@unittest.skipUnless((PROJECT/'ffmpeg.exe').is_file(), 'bundled FFmpeg unavailable')
class RealMediaTests(unittest.TestCase):
    def test_repeated_mpeg_timestamps_preserve_all_video_frames_and_audio(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            folder = Path(folder)
            segment = folder/'segment.vob'
            subprocess.run([str(PROJECT/'ffmpeg.exe'), '-hide_banner', '-nostdin', '-v', 'error',
                            '-f', 'lavfi', '-i', 'testsrc2=size=160x120:rate=25:duration=0.64',
                            '-f', 'lavfi', '-i', 'sine=frequency=600:sample_rate=48000:duration=0.64',
                            '-c:v', 'mpeg2video', '-bf', '2', '-c:a', 'ac3', '-f', 'vob', str(segment)],
                           check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            source = folder/'reset_timestamps.vob'
            source.write_bytes(segment.read_bytes()*2)
            checksum = hashlib.sha256(source.read_bytes()).hexdigest()
            pipeline = MediaPipeline(PROJECT/'ffmpeg.exe', PROJECT/'ffprobe.exe', folder/'recovered')
            report = pipeline.convert([{'path': source, 'recording_id': 'reset', 'expected_duration_seconds': 1.28}])
            self.assertTrue(report['complete'], json.dumps(report['clips'], indent=2))
            clip = report['clips'][0]
            self.assertGreater(clip['source_validation']['video_timestamp_discontinuity_count'], 0)
            self.assertEqual(clip['source_validation']['video_frames'], 32)
            self.assertEqual(clip['output_validation']['video_frames'], 32)
            self.assertEqual(clip['output_validation']['video_timestamp_discontinuity_count'], 0)
            self.assertEqual(checksum, hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertTrue(list(pipeline.logs.glob('*.command.json')))

    def test_bad_file_reports_failure_and_keeps_full_stderr(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            folder = Path(folder)
            bad = folder/'unreadable.vob'
            bad.write_bytes(b'NOT A VIDEO' * 128)
            pipeline = MediaPipeline(PROJECT/'ffmpeg.exe', PROJECT/'ffprobe.exe', folder/'recovered')
            report = pipeline.convert([bad])
            self.assertEqual(report['failed_count'], 1)
            self.assertFalse(report['complete'])
            self.assertTrue(any(p.read_text().strip() for p in pipeline.logs.glob('*.stderr.log')))
            self.assertEqual(bad.read_bytes(), b'NOT A VIDEO' * 128)


if __name__ == '__main__':
    unittest.main()
