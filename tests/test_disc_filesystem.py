"""ISO/Joliet tests use independently constructed descriptors/directory records."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from disc_filesystem import scan_iso9660, safe_filename

SECTOR = 2048
TEST_WORK = Path(r'D:\Downloads\Handycam Video Recovery\.test-work')
TEST_WORK.mkdir(parents=True, exist_ok=True)


def both(value, width):
    return value.to_bytes(width, 'little') + value.to_bytes(width, 'big')


def record(name, extent, size, flags=0, joliet=False):
    identifier = name if isinstance(name, bytes) else name.encode('utf-16-be' if joliet else 'ascii')
    length = 33 + len(identifier) + (not len(identifier) % 2)
    data = bytearray(length)
    data[0] = length
    data[2:10] = both(extent, 4)
    data[10:18] = both(size, 4)
    data[25] = flags
    data[28:32] = both(1, 2)
    data[32] = len(identifier)
    data[33:33 + len(identifier)] = identifier
    return data


def directory(extent, entries, joliet=False):
    data = record(b'\0', extent, SECTOR, 2) + record(b'\1', extent, SECTOR, 2)
    data += b''.join(entries)
    if len(data) > SECTOR:
        raise AssertionError('Test directory too large')
    return data + bytes(SECTOR - len(data))


def descriptor(root=20, size=100, joliet=False):
    data = bytearray(SECTOR)
    data[:7] = bytes([2 if joliet else 1]) + b'CD001\1'
    data[80:88] = both(size, 4)
    data[128:132] = both(SECTOR, 2)
    if joliet:
        data[88:91] = b'%/E'
    root_record = record(b'\0', root, SECTOR, 2)
    data[156:156 + len(root_record)] = root_record
    return data


class IsoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEST_WORK)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def image(self, blocks, count=100, start=0):
        data = bytearray(count * SECTOR)
        for sector, block in blocks.items():
            local = sector - start
            data[local * SECTOR:local * SECTOR + len(block)] = block
        path = self.root / f'image_{start}.bin'
        path.write_bytes(data)
        return {'path': str(path), 'start': start, 'available_sectors': count, 'bad_sectors': []}

    def test_all_files_listed_with_exact_byte_size_and_versions_removed(self):
        image = self.image({16: descriptor(), 20: directory(20, [
            record('MOVIE.MPG;1', 40, 2051), record('README.TXT;1', 43, 17),
            record('EMPTY.TXT;1', 44, 0), record('OTHER.TXT;1', 44, 0)])})
        result = scan_iso9660([image])
        self.assertTrue(result['complete'])
        self.assertEqual([f['name'] for f in result['files']], ['MOVIE.MPG', 'README.TXT', 'EMPTY.TXT', 'OTHER.TXT'])
        self.assertEqual(result['files'][0]['absolute_ranges'], [(40, 41)])
        self.assertEqual(result['files'][0]['extent_sizes'], [2051])

    def test_joliet_unicode_preferred_and_primary_view_deduplicated(self):
        image = self.image({16: descriptor(), 17: descriptor(root=21, joliet=True),
            20: directory(20, [record('VIDEO_1.MPG;1', 40, 17)]),
            21: directory(21, [record('পারিবারিক video.mpg;1', 40, 17, joliet=True)])})
        result = scan_iso9660([image])
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(result['files'][0]['name'], 'পারিবারিক video.mpg')
        self.assertIn('VIDEO_1.MPG', result['files'][0]['aliases'])

    def test_twenty_nested_directories_are_not_mistaken_for_cycle(self):
        blocks = {16: descriptor()}
        for depth in range(21):
            extent = 20 + depth
            entries = [record('MOVIE.MPG;1', 70, 17)] if depth == 20 else [record('COPY', extent + 1, SECTOR, 2)]
            blocks[extent] = directory(extent, entries)
        result = scan_iso9660([self.image(blocks)])
        self.assertTrue(result['complete'])
        self.assertEqual(len(result['files']), 1)
        self.assertEqual(result['files'][0]['relative_path'].count('/'), 20)

    def test_cycle_is_reported_while_healthy_sibling_survives(self):
        image = self.image({16: descriptor(), 20: directory(20, [
            record('CYCLE', 20, SECTOR, 2), record('HEALTHY.MPG;1', 50, 17)])})
        result = scan_iso9660([image])
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['files']), 1)
        self.assertTrue(any('cycle' in e for e in result['errors']))

    def test_multi_extent_file_retains_noncontiguous_extent_byte_sizes(self):
        image = self.image({16: descriptor(), 20: directory(20, [
            record('SPLIT.MPG;1', 40, SECTOR, 0x80), record('SPLIT.MPG;1', 50, 17)])})
        result = scan_iso9660([image])
        self.assertTrue(result['complete'])
        file = result['files'][0]
        self.assertEqual(file['absolute_ranges'], [(40, 40), (50, 50)])
        self.assertEqual(file['extent_sizes'], [SECTOR, 17])
        self.assertEqual(file['byte_size'], SECTOR + 17)

    def test_unterminated_multi_extent_is_partial_but_available_prefix_retained(self):
        image = self.image({16: descriptor(), 20: directory(20, [record('SPLIT.MPG;1', 40, 17, 0x80)])})
        result = scan_iso9660([image])
        self.assertFalse(result['complete'])
        self.assertFalse(result['files'][0]['complete'])
        self.assertEqual(result['files'][0]['byte_size'], 17)

    def test_corrupted_entry_resynchronizes_to_healthy_siblings(self):
        damaged = record('DAMAGED.MPG;1', 40, 17)
        damaged[6] ^= 1  # Big-endian extent copy no longer agrees.
        image = self.image({16: descriptor(), 20: directory(20, [damaged, record('HEALTHY.MPG;1', 50, 17)])})
        events = []
        result = scan_iso9660([image], lambda *a, **k: events.append((a, k)))
        self.assertFalse(result['complete'])
        self.assertIn('HEALTHY.MPG', [f['name'] for f in result['files']])
        self.assertTrue(events)

    def test_missing_file_extent_is_listed_as_partial_without_losing_healthy_file(self):
        image = self.image({16: descriptor(), 20: directory(20, [
            record('MISSING.MPG;1', 80, 17), record('HEALTHY.MPG;1', 40, 17)])}, count=60)
        result = scan_iso9660([image])
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['files']), 2)
        self.assertEqual(result['files'][0]['missing_ranges'], [(80, 80)])
        self.assertTrue(result['files'][1]['complete'])

    def test_out_of_bounds_file_is_retained_as_failed_identity_for_counting(self):
        image = self.image({16: descriptor(), 20: directory(20, [
            record('INVALID.MPG;1', 101, 17), record('HEALTHY.MPG;1', 40, 17)])})
        result = scan_iso9660([image])
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['files']), 2)
        self.assertEqual(result['files'][0]['absolute_ranges'], [])
        self.assertIn('extraction_error', result['files'][0])
        self.assertTrue(result['files'][1]['complete'])

    def test_bad_directory_sector_is_logged_and_healthy_other_tree_retained(self):
        image = self.image({16: descriptor(), 20: directory(20, [
            record('BROKEN', 21, SECTOR, 2), record('HEALTHY.MPG;1', 40, 17)]), 21: directory(21, [])})
        image['bad_sectors'] = [21]
        result = scan_iso9660([image])
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['files']), 1)
        self.assertTrue(any('known read errors' in e for e in result['errors']))

    def test_session_relative_and_absolute_multisession_extents(self):
        for absolute in (False, True):
            with self.subTest(absolute=absolute):
                root_extent = 1020 if absolute else 20
                file_extent = 1040 if absolute else 40
                image = self.image({1016: descriptor(root_extent, size=1100 if absolute else 100),
                                    1020: directory(root_extent, [record('MOVIE.MPG;1', file_extent, 17)])}, start=1000)
                result = scan_iso9660([image])
                self.assertTrue(result['complete'])
                self.assertEqual(result['files'][0]['absolute_ranges'], [(1040, 1040)])
                self.assertEqual(result['volumes'][0]['address_base'], 0 if absolute else 1000)

    def test_invalid_names_are_safe_and_unicode_remains(self):
        self.assertEqual(safe_filename('../A:B?.mpg;1'), '.._A_B_.mpg')
        self.assertEqual(safe_filename('CON.mp4;1'), '_CON.mp4')
        self.assertEqual(safe_filename('বাংলা.mp4;1'), 'বাংলা.mp4')

    def test_absent_iso_is_quiet_and_cancellation_propagates(self):
        image = self.image({})
        result = scan_iso9660([image])
        self.assertFalse(result['detected'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['errors'], [])
        def cancel():
            raise InterruptedError('stop')
        with self.assertRaises(InterruptedError):
            scan_iso9660([image], cancel_callback=cancel)


if __name__ == '__main__':
    unittest.main()
