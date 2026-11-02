"""Real-file checks for mounted data-disc enumeration and safe copying."""
from pathlib import Path
import contextlib
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mounted_files as mounted

TEST_WORK = Path(r'D:\Downloads\Handycam Video Recovery\.test-work')
TEST_WORK.mkdir(parents=True, exist_ok=True)


class CaptureLog:
    def __init__(self):
        self.events = []
        self.exceptions = []

    def __call__(self, level, event, **fields):
        self.events.append((level, event, fields))

    def exception(self, stage, error, **fields):
        self.exceptions.append((stage, error, fields))


class MountedFilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEST_WORK)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.disc = self.base / 'disc'
        self.disc.mkdir()
        self.log = CaptureLog()
        self.checks = 0

    def check(self):
        self.checks += 1

    def add_file(self, relative, data):
        path = self.disc / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def collect(self):
        return mounted.collect_videos(self.disc, self.log, self.check)

    def copy(self, inventory):
        checkpoints = []
        sources = mounted.copy_videos(inventory, self.base / 'saved', self.log,
                                     lambda *args: None, self.check,
                                     checkpoint=lambda: checkpoints.append(True))
        return sources, checkpoints

    def test_nested_video_names_sizes_and_nonvideo_filter(self):
        self.add_file('Group A/holiday.MPG', b'original-mpeg')
        self.add_file('Group B/holiday.MPG', b'different-file')
        self.add_file('Group B/Bengali স্মৃতি.mp4', b'original-mp4')
        self.add_file('DSCN0065.JPG', b'photo')
        inventory = self.collect()
        self.assertTrue(inventory['complete'])
        self.assertEqual(inventory['ignored_nonvideo_files'], 1)
        expected = {
            str(Path('Group A/holiday.MPG')): len(b'original-mpeg'),
            str(Path('Group B/holiday.MPG')): len(b'different-file'),
            str(Path('Group B/Bengali স্মৃতি.mp4')): len(b'original-mp4'),
        }
        self.assertEqual({entry['name']: entry['size_bytes'] for entry in inventory['files']}, expected)
        self.assertGreater(self.checks, 0)

    def test_every_healthy_file_copied_exactly_and_originals_unchanged(self):
        first = self.add_file('AP4/AP4 (1).mpg', b'A' * (1024 * 1024 + 17))
        second = self.add_file('AP5/AP4 (1).mpg', b'B' * 8193)
        originals = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in [first, second]}
        inventory = self.collect()
        sources, checkpoints = self.copy(inventory)
        self.assertTrue(inventory['complete'])
        self.assertEqual(inventory['missing_recordings'], 0)
        self.assertEqual(len(sources), 2)
        self.assertEqual(len(checkpoints), 2)
        self.assertEqual(len({item['path'] for item in sources}), 2)
        self.assertEqual({item['recording_id'] for item in sources}, {entry['name'] for entry in inventory['files']})
        for entry, source in zip(inventory['files'], sources):
            original = Path(entry['path'])
            target = Path(source['path'])
            self.assertEqual(target.read_bytes(), originals[original][0])
            self.assertEqual(target.stat().st_size, entry['size_bytes'])
            self.assertFalse(source['truncated_source'])
            self.assertIn(original.stem, source['output_filename'])
            self.assertEqual((original.read_bytes(), original.stat().st_mtime_ns), originals[original])
        self.assertFalse(self.log.exceptions)

    def test_read_failure_keeps_prefix_logs_retries_and_continues_next_file(self):
        damaged = self.add_file('a_damaged.mpg', b'D' * (1024 * 1024 + 37))
        healthy = self.add_file('b_healthy.mpg', b'complete-second-recording')
        inventory = self.collect()
        real_open = Path.open

        class FailingReader:
            def __init__(self, original):
                self.original = original

            def __enter__(self):
                self.original.__enter__()
                return self

            def __exit__(self, *args):
                return self.original.__exit__(*args)

            def seek(self, *args):
                return self.original.seek(*args)

            def read(self, amount):
                if self.original.tell() >= 1024 * 1024:
                    error = OSError('Simulated optical sector checksum failure')
                    error.winerror = 23
                    raise error
                return self.original.read(amount)

        def source_open(path, mode='r', *args, **kwargs):
            stream = real_open(path, mode, *args, **kwargs)
            return FailingReader(stream) if path == damaged and mode == 'rb' else stream

        with patch.object(Path, 'open', source_open):
            sources, checkpoints = self.copy(inventory)
        self.assertFalse(inventory['complete'])
        self.assertEqual(inventory['missing_recordings'], 0)
        self.assertEqual(len(sources), 2)
        self.assertTrue(sources[0]['truncated_source'])
        self.assertEqual(Path(sources[0]['path']).read_bytes(), b'D' * (1024 * 1024))
        self.assertTrue(str(sources[0]['path']).endswith('.partial'))
        self.assertFalse(sources[1]['truncated_source'])
        self.assertEqual(Path(sources[1]['path']).read_bytes(), healthy.read_bytes())
        retries = [fields for stage, error, fields in self.log.exceptions if stage == 'mounted_file_read']
        self.assertEqual([entry['attempt'] for entry in retries], [1, 2, 3])
        self.assertTrue(all(entry['byte_offset'] == 1024 * 1024 for entry in retries))
        self.assertTrue(any(stage == 'mounted_file_copy' for stage, error, fields in self.log.exceptions))
        self.assertEqual(len(checkpoints), 2)

    def test_unopenable_file_is_missing_but_later_video_still_copies(self):
        damaged = self.add_file('a_unopenable.mpg', b'disc file')
        healthy = self.add_file('b_healthy.mpg', b'later video')
        inventory = self.collect()
        real_open = Path.open

        def source_open(path, mode='r', *args, **kwargs):
            if path == damaged and mode == 'rb':
                raise PermissionError('Simulated ordinary file access failure')
            return real_open(path, mode, *args, **kwargs)

        with patch.object(Path, 'open', source_open):
            sources, checkpoints = self.copy(inventory)
        self.assertFalse(inventory['complete'])
        self.assertEqual(inventory['missing_recordings'], 1)
        self.assertEqual(len(sources), 1)
        self.assertEqual(Path(sources[0]['path']).read_bytes(), healthy.read_bytes())
        self.assertEqual(inventory['copies'][0]['copied_bytes'], 0)
        self.assertIn('error', inventory['copies'][0])
        self.assertTrue(any(stage == 'mounted_file_copy' for stage, error, fields in self.log.exceptions))

    def test_unreadable_directory_is_logged_while_other_folder_is_enumerated(self):
        blocked = self.disc / 'unreadable'
        blocked.mkdir()
        self.add_file('healthy/holiday.mpg', b'video')
        real_scandir = os.scandir

        def scandir(path):
            if Path(path) == blocked:
                raise PermissionError('Simulated directory read failure')
            return real_scandir(path)

        with patch.object(mounted.os, 'scandir', scandir):
            inventory = self.collect()
        self.assertEqual(len(inventory['files']), 1)
        self.assertFalse(inventory['complete'])
        self.assertEqual(len(inventory['enumeration_errors']), 1)
        self.assertEqual(inventory['enumeration_errors'][0]['directory'], str(blocked))
        self.assertTrue(any(stage == 'mounted_directory_read' for stage, error, fields in self.log.exceptions))

    def test_dvd_video_and_camcorder_directories_defer_to_disc_reader(self):
        for special in ['VIDEO_TS', 'DVD_RTAV', 'BDMV']:
            with self.subTest(special=special):
                root = self.base / special
                root.mkdir()
                (root / special).mkdir()
                (root / 'unrelated.mpg').write_bytes(b'ordinary-video')
                self.assertIsNone(mounted.collect_videos(root, self.log, self.check))

    def test_photo_only_disc_is_not_misidentified_as_video_inventory(self):
        self.add_file('DSCN0065.JPG', b'photo-only')
        self.assertIsNone(self.collect())

    def test_device_disconnect_stops_copying_before_any_later_file(self):
        import recovery
        removed = self.add_file('a_removed.mpg', b'A' * (1024 * 1024 + 33))
        later = self.add_file('b_new_disc.mpg', b'must-not-be-copied')
        inventory = self.collect()
        real_open = Path.open
        opened_sources = []

        class DisconnectedReader:
            def __init__(self, original):
                self.original = original

            def __enter__(self):
                self.original.__enter__()
                return self

            def __exit__(self, *args):
                return self.original.__exit__(*args)

            def seek(self, *args):
                return self.original.seek(*args)

            def read(self, amount):
                if self.original.tell() >= 1024 * 1024:
                    error = OSError('Simulated DVD writer disconnected')
                    error.winerror = 1167
                    raise error
                return self.original.read(amount)

        def source_open(path, mode='r', *args, **kwargs):
            if path in [removed, later] and mode == 'rb':
                opened_sources.append(path)
            stream = real_open(path, mode, *args, **kwargs)
            return DisconnectedReader(stream) if path == removed and mode == 'rb' else stream

        with patch.object(Path, 'open', source_open):
            with self.assertRaises(recovery.DiscChanged):
                self.copy(inventory)
        self.assertEqual(opened_sources, [removed])
        self.assertEqual(len(inventory['copies']), 1)
        result = inventory['copies'][0]
        self.assertFalse(result['complete'])
        self.assertEqual(result['copied_bytes'], 1024 * 1024)
        self.assertEqual(Path(result['target']).stat().st_size, 1024 * 1024)
        self.assertTrue(str(result['target']).endswith('.partial'))
        retries = [entry for entry in self.log.exceptions if entry[0] == 'mounted_file_read']
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0][2]['attempt'], 1)
        self.assertTrue(any(stage == 'mounted_file_copy' for stage, error, fields in self.log.exceptions))

    def test_device_change_during_directory_scan_is_fatal_before_next_folder(self):
        import recovery
        later = self.disc / 'a_later'
        removed = self.disc / 'z_removed'
        later.mkdir(); removed.mkdir()
        (later / 'different_disc.mpg').write_bytes(b'must-not-enumerate')
        real_scandir = os.scandir
        scanned = []

        @contextlib.contextmanager
        def scandir(path):
            path = Path(path)
            scanned.append(path)
            if path == removed:
                error = OSError('Simulated disc changed during enumeration')
                error.winerror = 1005
                raise error
            with real_scandir(path) as entries:
                yield sorted(list(entries), key=lambda entry: entry.name)

        with patch.object(mounted.os, 'scandir', scandir):
            with self.assertRaises(recovery.DiscChanged):
                self.collect()
        self.assertNotIn(later, scanned)
        self.assertTrue(any(stage == 'mounted_directory_read' for stage, error, fields in self.log.exceptions))


class SavedMountedLogger(CaptureLog):
    def __init__(self, directory):
        super().__init__()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True)

    def close(self, status, **fields):
        self('info', 'session_finished', status=status, **fields)


class SavedMountedPipeline:
    last_sources = None

    def __init__(self, *args, **kwargs):
        pass

    def convert(self, sources):
        type(self).last_sources = sources
        partial = sum(bool(source.get('truncated_source')) for source in sources)
        return {'complete': not partial, 'validated_count': len(sources) - partial,
                'partial_count': partial, 'failed_count': 0, 'combined_video': 'test.mp4',
                'report_path': 'test_media_report.json', 'clips': []}


class SavedMountedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEST_WORK)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.saved = self.base / 'earlier_recovery'
        self.archive = self.saved / 'Original recovery'
        self.data = self.archive / 'Data disc files'
        self.data.mkdir(parents=True)
        self.source_report = self.saved / 'recovery_report.json'

    def write_saved(self, actual, declared_size=None, complete=True, add_missing=False):
        path = self.data / '001_holiday.mpg'
        path.write_bytes(actual)
        declared_size = len(actual) if declared_size is None else declared_size
        entry = {'name': str(Path('AP4/holiday.mpg')), 'path': 'F:\\AP4\\holiday.mpg',
                 'size_bytes': declared_size, 'mtime_ns': 12345}
        copy = {**entry, 'target': str(path), 'copied_bytes': len(actual), 'complete': complete}
        inventory = {'type': 'Windows mounted data disc', 'files': [entry], 'copies': [copy],
                     'complete': complete, 'enumeration_errors': [], 'ignored_nonvideo_files': 0}
        if add_missing:
            missing = {'name': 'missing.mpg', 'path': 'F:\\missing.mpg', 'size_bytes': 99, 'mtime_ns': 12345}
            inventory['files'].append(missing)
            inventory['copies'].append({**missing, 'target': str(self.data / '002_missing.mpg'),
                                        'copied_bytes': 99, 'complete': True})
        self.source_report.write_text(json.dumps({'mounted_filesystem': inventory}), encoding='utf-8')
        return path

    def run_saved(self, selected=None):
        import recovery_engine as engine_module
        logger = SavedMountedLogger(self.base / 'logs')
        with patch.object(engine_module, 'RunLogger', return_value=logger):
            engine = engine_module.RecoveryEngine('saved', self.base / 'result',
                                                   saved_source=selected or self.saved)
        with patch.object(engine_module, 'MediaPipeline', SavedMountedPipeline), \
             patch.object(engine, 'open_disc', side_effect=AssertionError('Saved replay must not open a DVD')), \
             patch.object(engine, 'image_disc', side_effect=AssertionError('Saved replay must not image a DVD')), \
             patch.object(engine, 'discover_recordings', side_effect=AssertionError('Mounted file replay must not carve raw tracks')):
            report = engine.run()
        return engine, report, logger

    def test_complete_saved_mounted_files_restore_without_disc_access_or_source_changes(self):
        path = self.write_saved(b'complete-original-video')
        before_file = path.read_bytes(), path.stat().st_mtime_ns
        before_report = self.source_report.read_bytes(), self.source_report.stat().st_mtime_ns
        engine, report, logger = self.run_saved()
        self.assertEqual(report['status'], 'complete')
        self.assertTrue(report['complete'])
        self.assertTrue(report['source_read_complete'])
        self.assertTrue(report['discovery_complete'])
        self.assertEqual(report['expected_recordings'], 1)
        self.assertEqual(report['missing_recordings'], 0)
        self.assertEqual(SavedMountedPipeline.last_sources[0]['path'], str(path))
        self.assertFalse(SavedMountedPipeline.last_sources[0]['truncated_source'])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before_file)
        self.assertEqual((self.source_report.read_bytes(), self.source_report.stat().st_mtime_ns), before_report)
        self.assertTrue((engine.dest / 'recovery_report.json').exists())

    def test_shortened_saved_file_is_partial_even_when_prior_report_said_complete(self):
        path = self.write_saved(b'available-prefix', declared_size=100, complete=True)
        before_file = path.read_bytes(), path.stat().st_mtime_ns
        before_report = self.source_report.read_bytes()
        engine, report, logger = self.run_saved(selected=self.archive)
        self.assertEqual(report['status'], 'partial')
        self.assertFalse(report['complete'])
        self.assertFalse(report['source_read_complete'])
        self.assertTrue(SavedMountedPipeline.last_sources[0]['truncated_source'])
        self.assertEqual(report['partial_count'], 1)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before_file)
        self.assertEqual(self.source_report.read_bytes(), before_report)

    def test_missing_saved_file_is_counted_and_other_original_still_restores(self):
        path = self.write_saved(b'available-recording', add_missing=True)
        before_report = self.source_report.read_bytes()
        engine, report, logger = self.run_saved()
        self.assertEqual(report['status'], 'partial')
        self.assertFalse(report['complete'])
        self.assertFalse(report['source_read_complete'])
        self.assertEqual(report['expected_recordings'], 2)
        self.assertEqual(report['missing_recordings'], 1)
        self.assertEqual(len(SavedMountedPipeline.last_sources), 1)
        self.assertEqual(SavedMountedPipeline.last_sources[0]['path'], str(path))
        self.assertEqual(path.read_bytes(), b'available-recording')
        self.assertEqual(self.source_report.read_bytes(), before_report)


if __name__ == '__main__':
    unittest.main()
