"""DVD layout coverage tests: alternate PGCs, holes, corruption, local extraction."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dvd_layout import (DvdMetadataError, copy_recording_ranges, dvd_time_seconds,
                        find_ifo_offsets, merge_ranges, parse_ifo_all,
                        scan_metadata_file, subtract_ranges)

TEST_WORK = Path(r'D:\Downloads\Handycam Video Recovery\.test-work')
TEST_WORK.mkdir(parents=True, exist_ok=True)


def put16(data, offset, value):
    data[offset:offset + 2] = value.to_bytes(2, 'big')


def put32(data, offset, value):
    data[offset:offset + 4] = value.to_bytes(4, 'big')


def fixture(chains, physical=None, titles=None):
    """Create an independent DVD-Video table from semantic program/cell inputs."""
    data = bytearray(16384)
    data[:12] = b'DVDVIDEO-VTS'
    put32(data, 0x1c, 7)
    put32(data, 0xc4, 176)
    put32(data, 0xcc, 2)
    base = 4096
    put16(data, base, len(chains))
    cursor = 8 + 8 * len(chains)
    for i, chain in enumerate(chains):
        put32(data, base + 12 + i * 8, cursor)
        data[base + 8 + i * 8] = 0x81 + i
        start = base + cursor
        cells = chain['cells']
        programs = chain.get('programs', [1])
        data[start + 2:start + 4] = bytes([len(programs), len(cells)])
        data[start + 4:start + 8] = b'\0\0\x10\x40'
        put16(data, start + 230, 236)
        cp = 236 + len(programs) + len(programs) % 2
        pos = cp + 24 * len(cells)
        put16(data, start + 232, cp)
        put16(data, start + 234, pos)
        data[start + 236:start + 236 + len(programs)] = bytes(programs)
        for j, (first, last, seconds) in enumerate(cells):
            p = start + cp + 24 * j
            data[p + 4:p + 8] = bytes([0, 0, (seconds // 10) * 16 + seconds % 10, 0x40])
            put32(data, p + 8, first)
            put32(data, p + 20, last)
            put16(data, start + pos + 4 * j, i + 1)
            data[start + pos + 4 * j + 3] = j + 1
        cursor += pos + 4 * len(cells)
    put32(data, base + 4, cursor - 1)
    if physical is not None:
        put32(data, 0xe0, 6)
        table = 12288
        put16(data, table, len(physical))
        put32(data, table + 4, 8 + 12 * len(physical) - 1)
        for j, (first, last) in enumerate(physical):
            p = table + 8 + j * 12
            put16(data, p, j + 1)
            data[p + 2] = 1
            put32(data, p + 4, first)
            put32(data, p + 8, last)
    if titles is not None:
        put32(data, 0xc8, 1)
        table = 2048
        put16(data, table, len(titles))
        ptt = 8 + len(titles) * 4
        for i, chapters in enumerate(titles):
            put32(data, table + 8 + i * 4, ptt)
            for pgc, program in chapters:
                put16(data, table + ptt, pgc)
                put16(data, table + ptt + 2, program)
                ptt += 4
        put32(data, table + 4, ptt - 1)
    return data


class DvdLayoutTests(unittest.TestCase):
    def test_original_disc_24_programs_and_expected_duration(self):
        path = Path(__file__).parent / 'fixtures' / 'original_disc_metadata.bin'
        meta = parse_ifo_all(path.read_bytes())
        self.assertTrue(meta['complete'])
        self.assertEqual(len(meta['recordings']), 24)
        self.assertEqual(meta['cells'], 40)
        self.assertAlmostEqual(sum(r['duration_seconds'] for r in meta['recordings']), 608.16)
        self.assertEqual(meta['coverage_ranges'], [(0, 249711)])
        self.assertEqual(meta['unreferenced_cell_ranges'], [])
        self.assertEqual(meta['recordings'][0]['title_ids'], [1])

    def test_all_pgcs_and_programs_retained(self):
        data = fixture([{'cells': [(0, 1, 3), (7, 9, 4)], 'programs': [1, 2]},
                        {'cells': [(20, 24, 5)]}], titles=[[(1, 1), (1, 2)], [(2, 1)]])
        meta = parse_ifo_all(data)
        self.assertEqual([(r['pgc'], r['program']) for r in meta['recordings']], [(1, 1), (1, 2), (2, 1)])
        self.assertEqual(meta['recordings'][2]['title_references'], [{'title': 2, 'chapter': 1}])
        self.assertTrue(meta['complete'])

    def test_non_contiguous_cells_do_not_include_holes(self):
        meta = parse_ifo_all(fixture([{'cells': [(10, 11, 3), (20, 21, 4)]}]))
        rec = meta['recordings'][0]
        self.assertEqual(rec['ranges'], [(10, 11), (20, 21)])
        self.assertEqual(rec['sector_count'], 4)
        self.assertEqual(rec['duration_seconds'], 7)
        self.assertEqual(meta['coverage_ranges'], [(10, 11), (20, 21)])

    def test_duplicates_and_overlaps_are_reported_without_omission(self):
        meta = parse_ifo_all(fixture([{'cells': [(10, 20, 5)]}, {'cells': [(10, 20, 5)]},
                                     {'cells': [(18, 25, 3)]}]))
        recs = meta['recordings']
        self.assertEqual(len(recs), 3)
        self.assertEqual(recs[1]['duplicate_of'], recs[0]['id'])
        self.assertIn(recs[2]['id'], recs[0]['overlaps'])
        self.assertEqual(meta['coverage_sectors'], 16)

    def test_bad_pgc_does_not_hide_later_valid_chain(self):
        data = fixture([{'cells': [(0, 4, 1)]}, {'cells': [(20, 24, 2)]}])
        put32(data, 4096 + 12, 0xffffffff)
        meta = parse_ifo_all(data)
        self.assertFalse(meta['complete'])
        self.assertEqual([r['pgc'] for r in meta['recordings']], [2])
        self.assertIn('PGC 1 pointer', meta['errors'][0])

    def test_cell_table_cannot_run_into_next_pgc(self):
        data = fixture([{'cells': [(0, 4, 1)]}, {'cells': [(20, 24, 2)]}])
        first = 4096 + int.from_bytes(data[4108:4112], 'big')
        data[first + 3] = 255
        meta = parse_ifo_all(data)
        self.assertFalse(meta['complete'])
        self.assertEqual([r['pgc'] for r in meta['recordings']], [2])
        self.assertIn('outside its PGC', meta['errors'][0])

    def test_invalid_program_index_retains_each_valid_cell(self):
        meta = parse_ifo_all(fixture([{'cells': [(0, 4, 1), (10, 14, 2)], 'programs': [2, 1]}]))
        self.assertFalse(meta['complete'])
        self.assertEqual([r['ranges'] for r in meta['recordings']], [[(0, 4)], [(10, 14)]])
        self.assertTrue(all(r['orphan'] for r in meta['recordings']))

    def test_unindexed_first_cell_is_retained(self):
        meta = parse_ifo_all(fixture([{'cells': [(0, 4, 1), (10, 14, 2)], 'programs': [2]}]))
        self.assertFalse(meta['complete'])
        self.assertEqual(meta['coverage_ranges'], [(0, 4), (10, 14)])
        self.assertTrue(any(r['orphan'] for r in meta['recordings']))

    def test_invalid_cell_does_not_discard_other_programs(self):
        meta = parse_ifo_all(fixture([{'cells': [(10, 2, 1), (20, 25, 2)], 'programs': [1, 2]}]))
        self.assertFalse(meta['complete'])
        self.assertEqual(len(meta['recordings']), 1)
        self.assertEqual(meta['recordings'][0]['program'], 2)
        self.assertTrue(any('invalid sector range' in e for e in meta['errors']))

    def test_physical_unreferenced_cells_cannot_be_claimed_complete(self):
        meta = parse_ifo_all(fixture([{'cells': [(0, 4, 2)]}], physical=[(0, 4), (10, 15)]))
        self.assertFalse(meta['complete'])
        self.assertTrue(meta['physical_cell_table_complete'])
        self.assertEqual(meta['unreferenced_cell_ranges'], [(10, 15)])

    def test_angle_layout_is_preserved_but_requires_review(self):
        data = fixture([{'cells': [(0, 4, 2)]}])
        first = 4096 + int.from_bytes(data[4108:4112], 'big')
        cp = int.from_bytes(data[first + 232:first + 234], 'big')
        data[first + cp] = 0x50
        meta = parse_ifo_all(data)
        self.assertFalse(meta['complete'])
        self.assertEqual(meta['recordings'][0]['ranges'], [(0, 4)])
        self.assertIn('Angle/interleaved', meta['errors'][0])

    def test_fatal_and_truncated_headers_are_explicit(self):
        for data in (b'', b'DVDVIDEO-VTS', b'DVD_RTR_VMG' + bytes(4096)):
            with self.assertRaises(DvdMetadataError):
                parse_ifo_all(data)
        data = fixture([{'cells': [(0, 4, 2)]}])
        put32(data, 4100, 0xffffffff)
        meta = parse_ifo_all(data)
        self.assertFalse(meta['complete'])
        self.assertTrue(any('truncated' in e for e in meta['errors']))

    def test_time_decoding_and_invalid_bcd(self):
        self.assertAlmostEqual(dvd_time_seconds(bytes.fromhex('01234552')), 5025.48)
        self.assertAlmostEqual(dvd_time_seconds(bytes.fromhex('000001c1')), 1 + 1001 / 30000)
        for value in ('00600040', '00000000', '0000006a', '00000065'):
            with self.assertRaises(DvdMetadataError):
                dvd_time_seconds(bytes.fromhex(value))

    def test_union_and_subtraction_keep_gaps(self):
        self.assertEqual(merge_ranges([(7, 10), (0, 3), (3, 5)]), [(0, 5), (7, 10)])
        self.assertEqual(subtract_ranges([(0, 20)], [(2, 5), (10, 30)]), [(0, 1), (6, 9)])

    def test_ordered_range_extraction_and_no_overwrite(self):
        with tempfile.TemporaryDirectory(dir=TEST_WORK) as folder:
            source, target = Path(folder) / 'raw.vob', Path(folder) / 'clip.vob'
            source.write_bytes(b''.join(bytes([i]) * 2048 for i in range(8)))
            written = copy_recording_ranges(source, target, [(5, 6), (1, 1)], chunk_size=1000)
            self.assertEqual(written, 3 * 2048)
            self.assertEqual(target.read_bytes(), bytes([5]) * 2048 + bytes([6]) * 2048 + bytes([1]) * 2048)
            with self.assertRaises(FileExistsError):
                copy_recording_ranges(source, target, [(0, 1)])
            self.assertEqual(source.stat().st_size, 8 * 2048)

    def test_extraction_range_overrun_and_cancel_preserve_evidence(self):
        with tempfile.TemporaryDirectory(dir=TEST_WORK) as folder:
            source, target = Path(folder) / 'raw.vob', Path(folder) / 'clip.vob'
            source.write_bytes(bytes(2 * 2048))
            with self.assertRaises(DvdMetadataError):
                copy_recording_ranges(source, target, [(0, 2)])
            self.assertFalse(target.exists())
            def cancel():
                raise InterruptedError('test cancellation')
            with self.assertRaises(InterruptedError):
                copy_recording_ranges(source, target, [(0, 1)], cancel=cancel)
            self.assertTrue(target.with_suffix('.vob.partial').exists())

    def test_embedded_ifo_scanning_keeps_multiple_titles_and_bad_candidates(self):
        good = fixture([{'cells': [(0, 4, 1)]}])
        bad = bytearray(2048)
        bad[:12] = b'DVDVIDEO-VTS'
        # Signature crosses a deliberately small scan chunk boundary.
        with tempfile.TemporaryDirectory(dir=TEST_WORK) as folder:
            path = Path(folder) / 'track.bin'
            path.write_bytes(bytes(2048) + good + bytes(2048) + good + bad)
            found = scan_metadata_file(path, chunk_size=2053)
            self.assertEqual([x['offset'] for x in found], [2048, 20480, 36864])
            self.assertEqual(len(found[0]['metadata']['recordings']), 1)
            self.assertEqual(found[0]['sha256'], found[1]['sha256'])
            self.assertIsNone(found[2]['metadata'])
            self.assertIsNotNone(found[2]['error'])
        self.assertEqual(find_ifo_offsets(b'xDVDVIDEO-VTS' + bytes(100)), [])
        self.assertEqual(find_ifo_offsets(b'xDVDVIDEO-VTS', aligned_only=False), [1])


if __name__ == '__main__':
    unittest.main()
