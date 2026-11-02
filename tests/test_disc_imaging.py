import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
import recovery
from disc_imaging import DiscImager, SECTOR_BYTES

WORK = Path(r'D:\Downloads\Processing and video Encoding\2026-10-15\i-x20\work\imaging-tests')
WORK.mkdir(parents=True, exist_ok=True)


def sector(lba):
    return bytes([lba % 251 + 1]) * SECTOR_BYTES


class Logs:
    def __init__(self):
        self.records = []
    def __call__(self, level, event, **fields):
        self.records.append({'level': level, 'event': event, **fields})


class ImagingTests(unittest.TestCase):
    def setup_imager(self, disc, folder, cancel=None, max_attempts=3):
        logs = Logs()
        imager = DiscImager(disc, logs, lambda *args: None, cancel or threading.Event(), max_attempts)
        return imager, logs, Path(folder)/'original.mpg'

    def test_good_image_has_every_sector_and_persisted_success(self):
        class Disc:
            def sectors(self, lba, count):
                return b''.join(sector(n) for n in range(lba, lba+count))
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            imager, logs, target = self.setup_imager(Disc(), folder)
            report = imager.image({'start': 100, 'count': 130}, target)
            self.assertTrue(report['complete'])
            self.assertEqual(report['available_sectors'], 130)
            self.assertEqual(report['readable_sectors'], 130)
            self.assertEqual(target.read_bytes(), b''.join(sector(n) for n in range(100, 230)))
            self.assertTrue(json.loads(Path(report['state_path']).read_text())['complete'])

    def test_bad_sector_isolated_neighbors_saved_and_every_attempt_logged(self):
        class Disc:
            def __init__(self):
                self.failures = 0
            def sectors(self, lba, count):
                if lba <= 107 < lba+count:
                    self.failures += 1
                    error = recovery.TransientReadError('USB read timeout')
                    error.winerror = 121
                    raise error
                return b''.join(sector(n) for n in range(lba, lba+count))
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            disc = Disc()
            imager, logs, target = self.setup_imager(disc, folder)
            report = imager.image({'start': 100, 'count': 20}, target)
            self.assertFalse(report['complete'])
            self.assertEqual(report['bad_sectors'], [107])
            self.assertIsNone(report['interrupted'])
            data = target.read_bytes()
            self.assertEqual(data[7*SECTOR_BYTES:8*SECTOR_BYTES], bytes(SECTOR_BYTES))
            self.assertEqual(data[8*SECTOR_BYTES:9*SECTOR_BYTES], sector(108))
            failures = [r for r in logs.records if r['event'] == 'sector_read_failed']
            self.assertEqual(len(failures), disc.failures)
            self.assertTrue(all(r['error']['winerror'] == 121 for r in failures))
            self.assertEqual(json.loads(Path(report['bad_map_path']).read_text())['bad_sectors'], [107])

    def test_second_pass_repairs_zero_placeholder(self):
        class Disc:
            def __init__(self):
                self.single_attempts = 0
            def sectors(self, lba, count):
                if lba <= 2 < lba+count:
                    if count > 1:
                        raise recovery.ReadError('bulk failed')
                    self.single_attempts += 1
                    if self.single_attempts <= 2:
                        raise recovery.ReadError('first pass sector failed')
                return b''.join(sector(n) for n in range(lba, lba+count))
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            imager, logs, target = self.setup_imager(Disc(), folder)
            report = imager.image({'start': 0, 'count': 8}, target)
            self.assertTrue(report['complete'])
            self.assertIn(2,report['recovered_bad_sectors'])
            self.assertEqual(target.read_bytes()[2*SECTOR_BYTES:3*SECTOR_BYTES], sector(2))

    def test_disc_removal_preserves_prefix_and_never_invents_zero_sectors(self):
        class Disc:
            def sectors(self, lba, count):
                if lba >= 64:
                    raise recovery.DiscUnavailable('Disc removed')
                return b''.join(sector(n) for n in range(lba, lba+count))
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            imager, logs, target = self.setup_imager(Disc(), folder)
            report = imager.image({'start': 0, 'count': 128}, target)
            self.assertFalse(report['complete'])
            self.assertEqual(report['bad_sectors'], [])
            self.assertEqual(report['available_sectors'], 64)
            self.assertEqual(report['interrupted']['type'], 'DiscUnavailable')
            self.assertEqual(Path(report['path']).stat().st_size, 64*SECTOR_BYTES)
            self.assertFalse(target.exists())

    def test_disc_changed_interrupts_without_bad_sector(self):
        class Disc:
            def sectors(self, *args):
                raise recovery.DiscChanged('Writer reset or different disc')
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            imager, logs, target = self.setup_imager(Disc(), folder)
            report = imager.image({'start': 0, 'count': 10}, target)
            self.assertEqual(report['bad_sectors'], [])
            self.assertEqual(report['interrupted']['type'], 'DiscChanged')
            self.assertEqual(report['available_sectors'], 0)

    def test_resume_copies_old_file_then_repairs_new_file_only(self):
        class Disc:
            def __init__(self):
                self.calls = []
            def sectors(self, lba, count):
                self.calls.append((lba, count))
                return b''.join(sector(n) for n in range(lba, lba+count))
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            folder = Path(folder)
            old = folder/'old.mpg.partial'
            old_data = sector(100)+bytes(SECTOR_BYTES)+sector(102)
            old.write_bytes(old_data)
            before = hashlib.sha256(old.read_bytes()).hexdigest()
            disc = Disc()
            imager, logs, target = self.setup_imager(disc, folder)
            report = imager.image({'start': 100, 'count': 5}, target, existing_source=old, existing_bad=[101])
            self.assertTrue(report['complete'])
            self.assertEqual(disc.calls, [(103, 2), (101, 1)])
            self.assertEqual(before, hashlib.sha256(old.read_bytes()).hexdigest())
            self.assertEqual(target.read_bytes(), b''.join(sector(n) for n in range(100, 105)))

    def test_cancel_saves_state_before_raising(self):
        cancel = threading.Event()
        class Disc:
            def sectors(self, lba, count):
                cancel.set()
                return b''.join(sector(n) for n in range(lba, lba+count))
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            imager, logs, target = self.setup_imager(Disc(), folder, cancel=cancel)
            with self.assertRaises(recovery.Cancelled):
                imager.image({'start': 0, 'count': 128}, target)
            state = json.loads(target.with_suffix('.mpg.state.json').read_text())
            self.assertEqual(state['available_sectors'], 64)
            self.assertEqual(state['phase'], 'cancelled')
            self.assertEqual(state['bad_sectors'], [])

    def test_timeout_cascade_is_bounded_and_persisted(self):
        class Disc:
            now = 0.0
            def sectors(self, lba, count):
                self.now += 31
                raise recovery.TransientReadError('timeout')
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            disc = Disc()
            imager, logs, target = self.setup_imager(disc, folder)
            imager.no_progress_timeout = 90
            with patch('disc_imaging.time.monotonic', side_effect=lambda: disc.now):
                report = imager.image({'start': 0, 'count': 10000}, target)
            self.assertEqual(report['attempts_total'], 3)
            self.assertEqual(report['interrupted']['type'], 'NoReadProgress')
            self.assertEqual(report['available_sectors'], 192)
            self.assertEqual(report['readable_sectors'], 0)
            self.assertEqual(report['bad_sectors'],list(range(192)))
            self.assertFalse(report['complete'])

    def test_first_pass_reads_later_healthy_blocks_before_recursive_repairs(self):
        class Disc:
            def __init__(self):
                self.calls=[]
            def sectors(self,lba,count):
                self.calls.append((lba,count))
                if lba<=70<lba+count:
                    raise recovery.TransientReadError('damaged middle region')
                return b''.join(sector(n) for n in range(lba,lba+count))
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            disc=Disc();imager,logs,target=self.setup_imager(disc,folder)
            report=imager.image({'start':0,'count':192},target)
            self.assertEqual(disc.calls[:3],[(0,64),(64,64),(128,64)])
            self.assertTrue(report['first_pass_complete'])
            self.assertEqual(report['available_sectors'],192)
            self.assertEqual(report['bad_sectors'],[70])
            self.assertEqual(target.read_bytes()[128*SECTOR_BYTES:],b''.join(sector(n) for n in range(128,192)))
            self.assertTrue(any(r['event']=='read_range_deferred' for r in logs.records))

    def test_repair_budget_exhaustion_preserves_later_footage_and_pending_map(self):
        class Disc:
            now=0.
            def __init__(self):
                self.calls=[]
            def sectors(self,lba,count):
                self.calls.append((lba,count));self.now+=7.5
                if 64<=lba<128:
                    raise recovery.TransientReadError('slow damaged middle block')
                return b''.join(sector(n) for n in range(lba,lba+count))
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            disc=Disc();imager,logs,target=self.setup_imager(disc,folder)
            imager.repair_budget_seconds=15
            with patch('disc_imaging.time.monotonic',side_effect=lambda:disc.now):
                report=imager.image({'start':0,'count':192},target)
            self.assertEqual(disc.calls[:3],[(0,64),(64,64),(128,64)])
            self.assertTrue(report['first_pass_complete'])
            self.assertTrue(report['repair_budget_exhausted'])
            self.assertEqual(report['interrupted']['type'],'RepairBudgetExceeded')
            self.assertFalse(report['complete'])
            self.assertEqual(report['available_sectors'],192)
            self.assertEqual(report['unattempted_bad_sectors'],list(range(64,128)))
            self.assertEqual(target.read_bytes()[128*SECTOR_BYTES:],b''.join(sector(n) for n in range(128,192)))
            state=json.loads(Path(report['state_path']).read_text())
            mapping=json.loads(Path(report['bad_map_path']).read_text())
            self.assertTrue(state['repair_budget_exhausted'])
            self.assertEqual(mapping['pending_ranges'],[{'start':64,'count':64}])
            self.assertTrue(any(r['event']=='disc_repairs_deferred' for r in logs.records))

    def test_device_removal_during_repair_keeps_pending_ranges_without_more_reads(self):
        class Disc:
            def __init__(self):
                self.calls=[]
            def sectors(self,lba,count):
                self.calls.append((lba,count))
                if len(self.calls)>3:
                    raise recovery.DriveUnavailable('USB disconnected during repair')
                if lba==64:
                    raise recovery.ReadError('damaged middle block')
                return b''.join(sector(n) for n in range(lba,lba+count))
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            disc=Disc();imager,logs,target=self.setup_imager(disc,folder)
            report=imager.image({'start':0,'count':192},target)
            self.assertEqual(len(disc.calls),4)
            self.assertEqual(report['interrupted']['type'],'DriveUnavailable')
            self.assertEqual(report['bad_sectors'],list(range(64,128)))
            self.assertEqual(report['available_sectors'],192)
            self.assertFalse(report['complete'])

    def test_cancel_during_deferred_repair_keeps_full_first_pass_and_pending_map(self):
        cancel=threading.Event()
        class Disc:
            def __init__(self):
                self.calls=[]
            def sectors(self,lba,count):
                self.calls.append((lba,count))
                if len(self.calls)>3:
                    cancel.set()
                    raise recovery.TransientReadError('repair cancelled after attempted read')
                if lba==64:
                    raise recovery.ReadError('damaged middle block')
                return b''.join(sector(n) for n in range(lba,lba+count))
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            disc=Disc();imager,logs,target=self.setup_imager(disc,folder,cancel=cancel)
            with self.assertRaises(recovery.Cancelled):
                imager.image({'start':0,'count':192},target)
            state=json.loads(target.with_suffix('.mpg.state.json').read_text())
            self.assertTrue(state['first_pass_complete'])
            self.assertEqual(state['available_sectors'],192)
            self.assertEqual(state['bad_sectors'],list(range(64,128)))
            self.assertEqual(state['phase'],'cancelled')
            self.assertEqual(len(disc.calls),4)

    def test_existing_output_is_never_replaced(self):
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            imager, logs, target = self.setup_imager(None, folder)
            target.write_bytes(b'important existing data')
            with self.assertRaises(recovery.RecoveryError):
                imager.image({'start': 0, 'count': 1}, target)
            self.assertEqual(target.read_bytes(), b'important existing data')


if __name__ == '__main__':
    unittest.main()
