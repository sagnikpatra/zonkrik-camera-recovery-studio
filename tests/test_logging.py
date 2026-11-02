import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(r'E:\Projects\Claude App')
sys.path.insert(0, str(PROJECT))
import recovery_logging as logging
import recovery

WORK = Path(__file__).resolve().parent/'logging-tests'
WORK.mkdir(exist_ok=True)


def records(logger):
    return [json.loads(line) for line in logger.events_path.read_text().splitlines()]


class LoggingTests(unittest.TestCase):
    def test_lowercase_levels_normalized_and_protected_fields_valid(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            logger = logging.RunLogger(folder, application='ZONKRIK')
            logger.event('warning', 'warning_fixture', reason='test')
            logger.event('error', 'error_fixture')
            summary = logger.close('partial')
            self.assertEqual(summary['counts']['WARNING'], 1)
            self.assertEqual(summary['counts']['ERROR'], 1)
            self.assertEqual(records(logger)[1]['level'], 'WARNING')
            self.assertEqual(logger.counts['warning'], 0)

    def test_failed_preflight_saved_with_traceback_and_error_attributes(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            logger = logging.RunLogger(folder)
            try:
                error = recovery.TransientReadError('semaphore timeout')
                error.winerror = 121
                error.cdb_hex = '28000000012300000100'
                error.lba = 291
                error.sector_count = 1
                error.requested_bytes = 2048
                raise error
            except Exception as caught:
                logger.exception('preflight', caught)
            summary = logger.close('failed')
            failed = records(logger)[1]
            self.assertEqual(failed['stage'], 'preflight')
            self.assertEqual(failed['error']['winerror'], 121)
            self.assertEqual(failed['error']['lba'], 291)
            self.assertIn('raise error', failed['traceback'])
            self.assertEqual(summary['status'], 'failed')

    def test_command_failure_contains_argv_exit_and_full_stderr(self):
        error = subprocess.CalledProcessError(7, ['ffmpeg.exe', '-i', 'file.vob'], output='output', stderr='full decoder details')
        details = logging.error_details(error)
        self.assertEqual(details['returncode'], 7)
        self.assertEqual(details['cmd'], ['ffmpeg.exe', '-i', 'file.vob'])
        self.assertEqual(details['stderr'], 'full decoder details')

    def test_midrun_primary_append_failure_switches_and_retains_pending_event(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            primary = Path(folder)/'primary'
            fallback = Path(folder)/'emergency'
            logger = logging.RunLogger(primary)
            logger.event('info', 'earlier')
            original_open = Path.open
            def failing_open(path, mode='r', *args, **kwargs):
                if path.parent == primary and path.name == 'events.jsonl' and mode == 'a':
                    raise OSError(28, 'primary disk full')
                return original_open(path, mode, *args, **kwargs)
            with patch.object(logging, 'EMERGENCY_LOG_ROOT', fallback), patch.object(Path, 'open', failing_open):
                logger.event('error', 'must_be_saved', detail='full failure detail')
            summary = logger.close('partial')
            events = records(logger)
            self.assertTrue(logger.directory.is_relative_to(fallback))
            self.assertEqual(sum(r['event'] == 'must_be_saved' for r in events), 1)
            self.assertTrue(any(r['event'] == 'earlier' for r in events))
            self.assertTrue(any(r['event'] == 'logging_fallback_activated' for r in events))
            self.assertTrue(summary['fallback_active'])
            self.assertEqual(summary['events'], str(logger.events_path))
            sequence = [r['sequence'] for r in events]
            self.assertEqual(sequence, sorted(sequence))

    def test_text_failure_after_json_write_does_not_duplicate_json_event(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            primary = Path(folder)/'primary'
            logger = logging.RunLogger(primary)
            original_open = Path.open
            def failing_open(path, mode='r', *args, **kwargs):
                if path.parent == primary and path.name == 'session.log' and mode == 'a':
                    raise PermissionError('text log write denied')
                return original_open(path, mode, *args, **kwargs)
            with patch.object(logging, 'EMERGENCY_LOG_ROOT', Path(folder)/'emergency'), patch.object(Path, 'open', failing_open):
                logger.event('warning', 'pending_already_in_json')
            self.assertEqual(sum(r['event'] == 'pending_already_in_json' for r in records(logger)), 1)

    def test_midrun_summary_failure_switches_and_summary_uses_new_paths(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            primary = Path(folder)/'primary'
            logger = logging.RunLogger(primary)
            original_open = Path.open
            def failing_open(path, mode='r', *args, **kwargs):
                if path.parent == primary and path.name == 'summary.json.tmp':
                    raise OSError('summary write failed')
                return original_open(path, mode, *args, **kwargs)
            with patch.object(logging, 'EMERGENCY_LOG_ROOT', Path(folder)/'emergency'), patch.object(Path, 'open', failing_open):
                summary = logger.close('failed')
            self.assertTrue(summary['fallback_active'])
            saved = json.loads((logger.directory/'summary.json').read_text())
            self.assertEqual(saved['events'], str(logger.events_path))
            self.assertTrue(any(r['event'] == 'session_finished' for r in records(logger)))

    def test_initial_directory_failure_uses_emergency(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            primary = Path(folder)/'primary'
            original_mkdir = Path.mkdir
            def failing_mkdir(path, *args, **kwargs):
                if path == primary:
                    raise PermissionError('primary directory unavailable')
                return original_mkdir(path, *args, **kwargs)
            with patch.object(logging, 'EMERGENCY_LOG_ROOT', Path(folder)/'emergency'), patch.object(Path, 'mkdir', failing_mkdir):
                logger = logging.RunLogger(primary)
            self.assertTrue(logger.fallback_active)
            self.assertTrue(any(r['event'] == 'session_started' for r in records(logger)))

    def test_both_locations_failed_surfaces_clear_exception(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            primary = Path(folder)/'primary'
            logger = logging.RunLogger(primary)
            original_open = Path.open
            def failing_open(path, mode='r', *args, **kwargs):
                if mode in ('a', 'w'):
                    raise OSError('all writes fail')
                return original_open(path, mode, *args, **kwargs)
            with patch.object(logging, 'EMERGENCY_LOG_ROOT', Path(folder)/'emergency'), patch.object(Path, 'open', failing_open):
                with self.assertRaises(logging.LoggingFailure) as caught:
                    logger.event('error', 'cannot_save')
            self.assertIn('Primary log directory', str(caught.exception))
            self.assertIn('emergency log directory', str(caught.exception))

    def test_partial_old_json_line_is_not_copied_to_new_jsonl(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            primary = Path(folder)/'primary'
            logger = logging.RunLogger(primary)
            with logger.events_path.open('a') as stream:
                stream.write('{"incomplete":')
            original_open = Path.open
            def failing_open(path, mode='r', *args, **kwargs):
                if path.parent == primary and mode == 'a':
                    raise OSError('disk unavailable')
                return original_open(path, mode, *args, **kwargs)
            with patch.object(logging, 'EMERGENCY_LOG_ROOT', Path(folder)/'emergency'), patch.object(Path, 'open', failing_open):
                logger.event('error', 'pending')
            events = records(logger)
            marker = next(r for r in events if r['event'] == 'logging_fallback_activated')
            self.assertTrue(marker['history_recovery_errors'])
            self.assertTrue(any(r['event'] == 'pending' for r in events))

    def test_lowlevel_windows_timeout_logs_exact_cdb_and_lba_without_hardware(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            logger = logging.RunLogger(folder)
            disc = recovery.Disc.__new__(recovery.Disc)
            disc.h = None
            disc.logger = logger
            disc.drive = 'F:'
            cdb = [0x28, 0, 0, 0, 1, 35, 0, 0, 2, 0]
            with patch.object(recovery.k, 'DeviceIoControl', return_value=0), \
                 patch.object(recovery.ctypes, 'get_last_error', return_value=121):
                with self.assertRaises(recovery.TransientReadError):
                    disc.command(cdb, 4096)
            event = next(r for r in records(logger) if r['event'] == 'scsi_command_failed')
            self.assertEqual(event['error']['cdb_hex'], bytes(cdb).hex())
            self.assertEqual(event['error']['lba'], 291)
            self.assertEqual(event['error']['sector_count'], 2)
            self.assertEqual(event['error']['requested_bytes'], 4096)
            self.assertEqual(event['error']['winerror'], 121)

    def test_lowlevel_sense_failure_preserves_raw_sense_without_hardware(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            logger = logging.RunLogger(folder)
            disc = recovery.Disc.__new__(recovery.Disc)
            disc.h = None
            disc.logger = logger
            disc.drive = 'F:'
            sense = bytes.fromhex('700002000000000a000000003a01000000000000000000000000000000000000')
            def ioctl(handle, code, input_packet, *args):
                packet = input_packet._obj
                packet.s.ScsiStatus = 2
                for index, value in enumerate(sense):
                    packet.sense[index] = value
                return 1
            with patch.object(recovery.k, 'DeviceIoControl', side_effect=ioctl):
                with self.assertRaises(recovery.DiscUnavailable):
                    disc.command([0, 0, 0, 0, 0, 0], 0)
            event = next(r for r in records(logger) if r['event'] == 'scsi_command_failed')
            self.assertEqual(event['error']['sense_hex'], sense.hex())
            self.assertEqual(event['error']['sense_key'], 2)
            self.assertEqual(event['error']['asc'], 58)
            self.assertEqual(event['error']['ascq'], 1)


if __name__ == '__main__':
    unittest.main()
