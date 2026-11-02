"""Read-only reuse tests with mocked live sectors and real local checkpoints."""
from pathlib import Path
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery
from disc_imaging import DiscImager
from resume_candidates import find_verified_previous

SECTOR = 2048
TEST_WORK = Path(r'D:\Downloads\Handycam Video Recovery\.test-work')
TEST_WORK.mkdir(parents=True, exist_ok=True)


class FakeDisc:
    def __init__(self, data, start=1000):
        self.data, self.start, self.calls = data, start, []
        self.error = None
        self.after_read = None

    def sectors(self, lba, count):
        self.calls.append((lba, count))
        if self.error:
            raise self.error
        offset = (lba - self.start) * SECTOR
        result = self.data[offset:offset + count * SECTOR]
        if self.after_read:
            self.after_read()
        return result


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEST_WORK)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.metadata = (Path(__file__).parent / 'fixtures' / 'original_disc_metadata.bin').read_bytes()
        current = self.root / 'current' / 'Original recovery'
        current.mkdir(parents=True)
        path = current / 'track_02.bin'
        path.write_bytes(self.metadata)
        self.current_images = [{'path': str(path), 'start': 800, 'count': len(self.metadata) // SECTOR,
                                'available_sectors': len(self.metadata) // SECTOR,
                                'complete': True, 'bad_sectors': [],
                                'metadata_sha256': hashlib.sha256(self.metadata).hexdigest()}]
        self.data = b''.join(bytes([i]) * SECTOR for i in range(64))
        self.track = {'track': 3, 'start': 1000, 'count': 64}
        self.disc = FakeDisc(self.data)
        self.events = []

    def log(self, level, event, **fields):
        self.events.append({'level': level, 'event': event, **fields})

    def checkpoint(self, name='older', available=32, bad=None, data=None,
                   start=1000, count=64, timestamp=100):
        archive = self.root / name / 'Original recovery'
        archive.mkdir(parents=True)
        metadata = archive / 'track_02.bin'
        metadata.write_bytes(self.metadata)
        meta_state = {'path': str(metadata), 'start': 800, 'count': len(self.metadata) // SECTOR,
                      'available_sectors': len(self.metadata) // SECTOR, 'bad_sectors': [],
                      'complete': True, 'track': {'start': 800, 'count': len(self.metadata) // SECTOR}}
        (archive / 'track_02.bin.state.json').write_text(json.dumps(meta_state))
        path = archive / 'track_03.bin.partial'
        path.write_bytes((self.data if data is None else data)[:available * SECTOR])
        state = {'path': str(path), 'start': start, 'count': count,
                 'available_sectors': available, 'bad_sectors': bad or [],
                 'complete': available == count and not bad,
                 'interrupted': {'type': 'DriveUnavailable', 'message': 'USB disconnected'},
                 'track': {'track': 3, 'start': start, 'count': count}}
        state_path = archive / 'track_03.bin.state.json'
        state_path.write_text(json.dumps(state))
        os.utime(state_path, (timestamp, timestamp))
        return path, state_path

    def find(self, cancel=None):
        return find_verified_previous(self.disc, self.track, self.current_images,
                                      self.root, self.log, cancel)

    def test_partial_prefix_verified_by_actual_samples_inside_saved_bytes(self):
        path, state = self.checkpoint(available=32)
        before = path.read_bytes()
        found, bad = self.find()
        self.assertEqual(found, path)
        self.assertEqual(bad, [])
        self.assertGreaterEqual(len(self.disc.calls), 3)
        self.assertTrue(all(1000 <= lba and lba + count <= 1032 for lba, count in self.disc.calls))
        self.assertEqual(path.read_bytes(), before)
        verified = [e for e in self.events if e['event'] == 'previous_image_identity_verified'][0]
        self.assertEqual(verified['sample_count'], len(self.disc.calls))
        self.assertEqual(verified['available_sectors'], 32)
        self.assertFalse(verified['source_modified'])

    def test_known_bad_samples_are_skipped_and_other_good_spans_compared(self):
        bad = [1000, 1007, 1014, 1021, 1028]
        path, _ = self.checkpoint(available=32, bad=bad)
        self.assertEqual(self.find(), (path, bad))
        self.assertGreaterEqual(len(self.disc.calls), 3)
        self.assertTrue(all(not any(lba <= b < lba + count for b in bad)
                            for lba, count in self.disc.calls))

    def test_newest_verified_candidate_is_preferred(self):
        self.checkpoint('older', timestamp=100)
        latest, _ = self.checkpoint('latest', available=48, timestamp=200)
        self.assertEqual(self.find()[0], latest)

    def test_newest_mismatch_falls_back_to_older_verified_candidate(self):
        older, _ = self.checkpoint('older', timestamp=100)
        self.checkpoint('latest', data=b'X' * len(self.data), timestamp=200)
        self.assertEqual(self.find()[0], older)
        self.assertTrue(any(e['event'] == 'resume_candidate_rejected' and
                            'Live sectors differ' in e['error']['message'] for e in self.events))

    def test_track_start_or_count_mismatch_is_never_sampled(self):
        self.checkpoint('wrong_start', start=999)
        self.checkpoint('wrong_count', count=65)
        self.assertEqual(self.find(), (None, None))
        self.assertEqual(self.disc.calls, [])

    def test_mismatched_or_invalid_metadata_is_rejected_before_live_reads(self):
        path, _ = self.checkpoint()
        meta = path.parent / 'track_02.bin'
        data = bytearray(meta.read_bytes())
        data[0x100] ^= 1  # Same valid layout, different exact metadata identity.
        meta.write_bytes(data)
        self.assertEqual(self.find(), (None, None))
        self.assertEqual(self.disc.calls, [])
        meta.write_bytes(b'INVALID' + bytes(len(self.metadata) - 7))
        self.assertEqual(self.find(), (None, None))

    def test_incomplete_metadata_state_cannot_authorize_resume(self):
        path, _ = self.checkpoint()
        state_path = path.parent / 'track_02.bin.state.json'
        state = json.loads(state_path.read_text())
        state['complete'] = False
        state_path.write_text(json.dumps(state))
        self.assertEqual(self.find(), (None, None))
        self.assertEqual(self.disc.calls, [])

    def test_current_metadata_claim_must_match_its_actual_bytes(self):
        self.checkpoint()
        self.current_images[0]['metadata_sha256'] = '0' * 64
        self.assertEqual(self.find(), (None, None))
        self.assertEqual(self.disc.calls, [])

    def test_checkpoint_must_match_complete_saved_sector_prefix(self):
        path, _ = self.checkpoint()
        with path.open('ab') as stream:
            stream.write(b'partial byte')
        self.assertEqual(self.find(), (None, None))
        self.assertEqual(self.disc.calls, [])

    def test_too_short_or_all_bad_prefix_cannot_reuse_without_three_samples(self):
        self.checkpoint('tiny', available=8)
        self.checkpoint('damaged', available=16, bad=list(range(1000, 1016)))
        self.assertEqual(self.find(), (None, None))
        self.assertEqual(self.disc.calls, [])

    def test_bad_map_outside_available_prefix_and_invalid_json_are_rejected(self):
        self.checkpoint('badmap', available=16, bad=[1030])
        _, broken = self.checkpoint('broken')
        broken.write_text('{broken')
        self.assertEqual(self.find(), (None, None))
        self.assertEqual(self.disc.calls, [])

    def test_short_or_failed_live_sample_is_rejected_and_logged(self):
        self.checkpoint()
        self.disc.data = b''
        self.assertEqual(self.find(), (None, None))
        self.disc.error = recovery.ReadError('sector checksum failed')
        self.assertEqual(self.find(), (None, None))
        self.assertTrue(any(e['event'] == 'resume_live_sample_read_failed' for e in self.events))

    def test_device_removal_and_cancel_propagate_instead_of_trying_other_disc(self):
        self.checkpoint()
        self.disc.error = recovery.DriveUnavailable('unplugged')
        with self.assertRaises(recovery.DriveUnavailable):
            self.find()
        self.disc.error = None
        def cancel():
            raise recovery.Cancelled('stop')
        with self.assertRaises(recovery.Cancelled):
            self.find(cancel)

    def test_source_changed_during_comparison_is_rejected_without_modifying_it(self):
        path, _ = self.checkpoint()
        def grow():
            self.disc.after_read = None
            with path.open('ab') as stream:
                stream.write(bytes(SECTOR))
        self.disc.after_read = grow
        self.assertEqual(self.find(), (None, None))

    def test_relocated_checkpoint_uses_sibling_file(self):
        path, state_path = self.checkpoint()
        state = json.loads(state_path.read_text())
        state['path'] = str(self.root / 'missing_old_location' / path.name)
        state_path.write_text(json.dumps(state))
        self.assertEqual(self.find()[0], path)

    def test_legacy_full_image_metadata_and_map_still_work(self):
        archive = self.root / 'legacy' / 'Original recovery'
        archive.mkdir(parents=True)
        path = archive / 'Handycam_original.mpg'
        path.write_bytes(self.data)
        (archive / 'metadata_track_02.bin').write_bytes(self.metadata)
        (archive.parent / 'read_errors.json').write_text('[1010]')
        self.assertEqual(self.find(), (path, [1010]))
        self.assertTrue(all(not lba <= 1010 < lba + count for lba, count in self.disc.calls))

    def test_legacy_truncated_image_or_missing_bad_map_is_not_reused(self):
        archive = self.root / 'legacy' / 'Original recovery'
        archive.mkdir(parents=True)
        path = archive / 'Handycam_original.mpg'
        path.write_bytes(self.data[:32 * SECTOR])
        (archive / 'metadata_track_02.bin').write_bytes(self.metadata)
        (archive.parent / 'read_errors.json').write_text('[]')
        self.assertEqual(self.find(), (None, None))
        path.write_bytes(self.data)
        (archive.parent / 'read_errors.json').unlink()
        self.assertEqual(self.find(), (None, None))
        self.assertEqual(self.disc.calls, [])

    def test_verified_partial_continues_imaging_without_rereading_prefix(self):
        path, _ = self.checkpoint(available=32, bad=[1010])
        found, bad = self.find()
        self.disc.calls.clear()
        target = self.root / 'new_copy' / 'track_03.bin'
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        imager = DiscImager(self.disc, self.log, lambda *args: None, threading.Event())
        result = imager.image(self.track, target, existing_source=found, existing_bad=bad)
        self.assertTrue(result['complete'])
        self.assertEqual(target.read_bytes(), self.data)
        self.assertIn((1032, 32), self.disc.calls)
        self.assertIn((1010, 1), self.disc.calls)  # Retry only the old known-bad sector.
        self.assertFalse(any(lba < 1032 and (lba, count) != (1010, 1) for lba, count in self.disc.calls))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
