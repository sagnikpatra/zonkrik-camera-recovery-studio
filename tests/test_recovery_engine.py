"""Local-image integration tests: every title, backup identity, and exact sectors."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recovery_engine as engine_module
from recovery_engine import RecoveryEngine, SectorSpace
from test_dvd_layout import TEST_WORK, fixture, put32


SECTOR = 2048


class MemoryLogger:
    def __init__(self, directory, **context):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events = []

    def __call__(self, level, event, **fields):
        self.events.append({'level': level, 'event': event, **fields})

    def exception(self, stage, error, **fields):
        self('ERROR', 'operation_failed', stage=stage, error=str(error), **fields)

    def close(self, status, **fields):
        self('INFO', 'session_finished', status=status, **fields)


class FakePipeline:
    def __init__(self, *args, **kwargs):
        pass

    def convert(self, sources):
        return {'complete': True, 'validated_count': len(sources), 'partial_count': 0,
                'failed_count': 0, 'combined_video': 'test.mp4', 'report_path': 'test.json',
                'clips': []}


class LocalImageCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEST_WORK)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.logger = MemoryLogger(self.root / 'logs')
        with patch.object(engine_module, 'RunLogger', return_value=self.logger):
            self.engine = RecoveryEngine('saved', self.root / 'result')

    def image(self, name, start, data, bad=None):
        path = self.root / name
        path.write_bytes(data)
        item = {'path': str(path), 'start': start, 'count': len(data) // SECTOR,
                'available_sectors': len(data) // SECTOR, 'complete': not bad,
                'bad_sectors': bad or [], 'track': len(self.engine.images) + 1}
        self.engine.images.append(item)
        return item

    def metadata(self, cells, programs=None):
        data = fixture([{'cells': cells, 'programs': programs or [1]}],
                       physical=[(a, b) for a, b, seconds in cells])
        put32(data, 0xc4, 16)
        maximum = max(b for a, b, seconds in cells)
        # Standard VTS: IFO in sectors0..7, VOB starts16, BUP follows movie.
        put32(data, 0x0c, 16 + maximum + 8)
        return data

    def video(self, name, start, sectors, label=b'V'):
        return self.image(name, start,
                          b''.join(b'\x00\x00\x01\xba' + label * (SECTOR - 4)
                                   for _ in range(sectors)))


class DiscoveryTests(LocalImageCase):
    def test_two_real_world_title_sets_16_plus_17_are_all_retained(self):
        first = self.metadata([(i * 2, i * 2 + 1, 1) for i in range(16)], list(range(1, 17)))
        second = self.metadata([(i * 2, i * 2 + 1, 2) for i in range(17)], list(range(1, 18)))
        self.image('metadata1.bin', 100, first)
        self.video('video1.bin', 116, 32)
        self.image('metadata2.bin', 200, second)
        self.video('video2.bin', 216, 34)
        recordings = self.engine.discover_recordings()
        self.assertEqual(len(recordings), 33)
        self.assertEqual(recordings[0]['absolute_ranges'], [(116, 117)])
        self.assertEqual(recordings[15]['absolute_ranges'], [(146, 147)])
        self.assertEqual(recordings[16]['absolute_ranges'], [(216, 217)])
        self.assertEqual(recordings[-1]['absolute_ranges'], [(248, 249)])
        self.assertEqual(sum(r['duration_seconds'] for r in recordings), 50)

    def test_standard_backup_ifo_maps_to_primary_without_duplicate_recordings(self):
        metadata = self.metadata([(0, 2, 3)])
        self.image('primary.bin', 100, metadata)
        self.video('video.bin', 116, 3)
        self.image('backup.bin', 119, metadata)
        recordings = self.engine.discover_recordings()
        self.assertEqual(len(recordings), 1)
        self.assertEqual(recordings[0]['absolute_ranges'], [(116, 118)])
        self.assertTrue(any('duplicate' in e['event'] or 'backup' in e['event'] for e in self.logger.events))

    def test_identical_metadata_at_distinct_vts_locations_is_not_globally_deduplicated(self):
        metadata = self.metadata([(0, 2, 3)])
        self.image('primary1.bin', 100, metadata)
        self.video('video1.bin', 116, 3, b'A')
        self.image('primary2.bin', 200, metadata)
        self.video('video2.bin', 216, 3, b'B')
        recordings = self.engine.discover_recordings()
        self.assertEqual(len(recordings), 2)
        self.assertEqual([r['absolute_ranges'] for r in recordings], [[(116, 118)], [(216, 218)]])

    def test_backup_only_can_associate_to_prior_preserved_video(self):
        metadata = self.metadata([(0, 2, 3)])
        self.video('video.bin', 116, 3)
        self.image('backup_only.bin', 119, metadata)
        recordings = self.engine.discover_recordings()
        self.assertEqual(len(recordings), 1)
        self.assertEqual(recordings[0]['absolute_ranges'], [(116, 118)])
        self.assertTrue(recordings[0]['metadata_unverified'])
        self.assertTrue(recordings[0]['backup_association_inferred'])
        self.engine.dest.mkdir()
        sources=self.engine.extract_recordings(recordings)
        self.assertTrue(sources[0]['metadata_unverified'])
        self.assertTrue(sources[0]['backup_association_inferred'])

    def test_noncontiguous_program_keeps_individual_cell_addresses(self):
        metadata = self.metadata([(0, 1, 2), (5, 6, 3)])
        self.image('primary.bin', 100, metadata)
        self.video('video.bin', 116, 7)
        recordings = self.engine.discover_recordings()
        self.assertEqual(recordings[0]['absolute_ranges'], [(116, 117), (121, 122)])
        self.assertEqual(recordings[0]['duration_seconds'], 5)

    def test_invalid_candidate_does_not_turn_successful_salvage_into_exception(self):
        self.image('primary.bin', 100, self.metadata([(0, 2, 3)]))
        self.video('video.bin', 116, 3)
        invalid = bytearray(SECTOR)
        invalid[:12] = b'DVDVIDEO-VTS'
        self.image('invalid.bin', 400, invalid)
        self.engine.inventory = [{'track': i['track']} for i in self.engine.images]
        self.engine.saved_source = self.root
        def load():
            self.engine.dest.mkdir()
        with patch.object(self.engine, 'load_saved', side_effect=load), patch.object(engine_module, 'MediaPipeline', FakePipeline):
            report = self.engine.run()
        self.assertEqual(report['status'], 'partial')
        self.assertFalse(report['discovery_complete'])
        self.assertEqual(report['recordings'], 1)
        self.assertTrue(any(e['event'] == 'metadata_parse_failed' for e in self.logger.events))
        self.assertTrue((self.engine.dest / 'recovery_report.json').exists())

    def test_missing_video_title_is_retained_and_reported_as_missing(self):
        self.image('metadata1.bin', 100, self.metadata([(0, 2, 3)]))
        self.video('video1.bin', 116, 3)
        self.image('metadata2.bin', 200, self.metadata([(0, 3, 4)]))
        recordings = self.engine.discover_recordings()
        self.assertEqual(len(recordings), 2)
        self.engine.dest.mkdir()
        sources = self.engine.extract_recordings(recordings)
        self.assertEqual(len(sources), 1)
        self.assertEqual(self.engine.report['missing_recordings'], 1)
        self.assertEqual(self.engine.extraction[1]['missing_ranges'], [[216, 219]])

    def test_mpeg_track_without_metadata_is_salvaged_alongside_valid_titles(self):
        self.image('metadata.bin', 100, self.metadata([(0, 2, 3)]))
        self.video('known_video.bin', 116, 3)
        self.video('unknown_video.bin', 300, 4)
        recordings = self.engine.discover_recordings()
        self.assertEqual(len(recordings), 2)
        self.assertEqual(recordings[0]['absolute_ranges'], [(116, 118)])
        unknown = [r for r in recordings if r.get('metadata_unverified')]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]['absolute_ranges'], [(300, 303)])

    def test_raw_salvage_does_not_duplicate_already_referenced_video_ranges(self):
        self.image('metadata.bin', 100, self.metadata([(0, 1, 2)]))
        self.video('known_plus_extra_video.bin', 116, 5)
        recordings = self.engine.discover_recordings()
        self.assertEqual(len(recordings), 2)
        self.assertEqual(recordings[0]['absolute_ranges'], [(116, 117)])
        extra = [r for r in recordings if r.get('metadata_unverified')]
        self.assertEqual(extra[0]['absolute_ranges'], [(118, 120)])


class SectorSpaceTests(LocalImageCase):
    def test_each_iso_extent_is_trimmed_to_its_exact_file_byte_count(self):
        one=self.image('extent.bin',10,b'A'*SECTOR+b'B'*SECTOR+b'C'*SECTOR)
        target=self.root/'exact.mpg'
        result=SectorSpace([one]).extract([(10,10),(12,12)],target,extent_sizes=[37,91])
        self.assertEqual(target.read_bytes(),b'A'*37+b'C'*91)
        self.assertEqual(result['written_bytes'],128)
        self.assertFalse(result['truncated_source'])

    def test_exact_ranges_cross_track_boundary_in_playback_order(self):
        one = self.image('one.bin', 10, b'A' * SECTOR + b'B' * SECTOR)
        two = self.image('two.bin', 12, b'C' * SECTOR + b'D' * SECTOR)
        target = self.root / 'clip.vob'
        result = SectorSpace([one, two]).extract([(11, 12), (10, 10)], target)
        self.assertEqual(target.read_bytes(), b'B' * SECTOR + b'C' * SECTOR + b'A' * SECTOR)
        self.assertEqual(result['written_bytes'], 3 * SECTOR)
        self.assertEqual(result['missing_ranges'], [])

    def test_missing_middle_span_is_explicit_and_never_zero_filled(self):
        one = self.image('one.bin', 10, b'A' * SECTOR)
        two = self.image('two.bin', 13, b'D' * SECTOR)
        target = self.root / 'clip.vob'
        result = SectorSpace([one, two]).extract([(10, 13)], target)
        self.assertFalse(target.exists())
        self.assertTrue(result['truncated_source'])
        self.assertEqual(result['missing_ranges'], [[11, 12]])
        self.assertEqual(Path(result['path']).read_bytes(), b'A' * SECTOR + b'D' * SECTOR)

    def test_known_bad_sector_lbas_follow_actual_ranges_only(self):
        image = self.image('bad.bin', 10, b'A' * 5 * SECTOR, [10, 12, 14])
        result = SectorSpace([image]).extract([(11, 13)], self.root / 'clip.vob')
        self.assertEqual(result['unreadable_sectors'], [12])

    def test_declared_available_image_cannot_silently_truncate_recording(self):
        image = self.image('short.bin', 10, b'A' * SECTOR)
        image['available_sectors'] = 4
        with self.assertRaises(engine_module.low.RecoveryError):
            SectorSpace([image]).extract([(10, 13)], self.root / 'clip.vob')
        self.assertTrue((self.root / 'clip.partial.vob').exists())

    def test_source_images_and_existing_targets_are_not_modified(self):
        image = self.image('source.bin', 10, b'A' * SECTOR)
        target = self.root / 'clip.vob'
        target.write_bytes(b'original')
        with self.assertRaises(FileExistsError):
            SectorSpace([image]).extract([(10, 10)], target)
        self.assertEqual(target.read_bytes(), b'original')
        self.assertEqual(Path(image['path']).read_bytes(), b'A' * SECTOR)


if __name__ == '__main__':
    unittest.main()
