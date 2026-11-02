"""Bounds-checked DVD-Video title metadata and local-image extraction helpers.

All sector ranges are inclusive and relative to the title VOB stream, not the
disc. ``video_offset`` is the VTS title-VOB sector offset relative to VTS start.
Every program in every PGC is retained, including alternate/duplicate programs.
This is DVD-Video parsing; DVD-VR navigation is not claimed to be supported.

Field layout reference: libdvdread src/dvdread/ifo_types.h (PGC_SIZE=236,
PGCI_SRP_SIZE=8, 24-byte cell_playback_t and 12-byte cell_adr_t).
https://raw.githubusercontent.com/xbmc/libdvdread/master/src/dvdread/ifo_types.h
"""
from __future__ import annotations

import hashlib
from pathlib import Path


SECTOR_SIZE = 2048
SIGNATURE = b'DVDVIDEO-VTS'


class DvdMetadataError(ValueError):
    """No safe DVD-Video program table can be obtained from this buffer."""


def _u16(data, offset):
    return int.from_bytes(data[offset:offset + 2], 'big')


def _u32(data, offset):
    return int.from_bytes(data[offset:offset + 4], 'big')


def dvd_time_seconds(value):
    """Decode DVD BCD hh:mm:ss:ff, raising on invalid BCD/frame-rate values."""
    if len(value) != 4:
        raise DvdMetadataError('Truncated DVD playback time.')
    values = [value[0], value[1], value[2], value[3] & 0x3f]
    if any((v >> 4) > 9 or (v & 15) > 9 for v in values):
        raise DvdMetadataError('DVD playback time contains invalid BCD digits.')
    hours, minutes, seconds, frames = [(v >> 4) * 10 + (v & 15) for v in values]
    if minutes >= 60 or seconds >= 60:
        raise DvdMetadataError('DVD playback time contains invalid minutes/seconds.')
    rate_code = value[3] >> 6
    if rate_code not in (1, 3):
        # Some unfinished metadata uses all-zero duration fields. It does not
        # provide a known duration and must not be asserted as verified zero.
        raise DvdMetadataError('DVD playback time has no valid frame rate.')
    rate = 25 if rate_code == 1 else 30000 / 1001
    if frames >= (25 if rate_code == 1 else 30):
        raise DvdMetadataError('DVD playback time contains an invalid frame count.')
    return hours * 3600 + minutes * 60 + seconds + frames / rate


def merge_ranges(ranges):
    """Return sorted, disjoint union ranges without changing playback order elsewhere."""
    merged = []
    for first, last in sorted(ranges):
        if first < 0 or last < first:
            raise DvdMetadataError(f'Invalid sector range {first}..{last}.')
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
        else:
            merged.append((first, last))
    return merged


def subtract_ranges(ranges, covered):
    """Return physical sectors from ranges that are absent from covered."""
    result = []
    covers = merge_ranges(covered)
    for first, last in merge_ranges(ranges):
        cursor = first
        for a, b in covers:
            if b < cursor:
                continue
            if a > last:
                break
            if a > cursor:
                result.append((cursor, min(a - 1, last)))
            cursor = max(cursor, b + 1)
            if cursor > last:
                break
        if cursor <= last:
            result.append((cursor, last))
    return result


def _duration(data, offset, label, warnings):
    try:
        return dvd_time_seconds(data[offset:offset + 4])
    except DvdMetadataError as error:
        warnings.append(f'{label}: {error}')
        return None


def _table_bounds(data, offset, label, min_size=8):
    if offset < 0x100 or offset + min_size > len(data):
        raise DvdMetadataError(f'{label} pointer is outside the available metadata.')
    end = offset + _u32(data, offset + 4) + 1
    if end < offset + min_size:
        raise DvdMetadataError(f'{label} has an invalid declared length.')
    return end


def _parse_title_refs(data, offset, warnings):
    refs = {}
    if not offset:
        warnings.append('No title/chapter pointer table; program identifiers are used.')
        return refs
    try:
        end = _table_bounds(data, offset, 'Title/chapter table')
        count = _u16(data, offset)
        directory_end = offset + 8 + 4 * count
        if count > 4096 or directory_end > min(end, len(data)):
            raise DvdMetadataError('Incomplete title/chapter pointer directory.')
        starts = [offset + _u32(data, offset + 8 + 4 * i) for i in range(count)]
        if starts != sorted(starts) or any(p < directory_end or p > end for p in starts):
            raise DvdMetadataError('Invalid title/chapter table offsets.')
        for i, start in enumerate(starts):
            finish = starts[i + 1] if i + 1 < count else end
            if finish > len(data) or (finish - start) % 4:
                raise DvdMetadataError(f'Title {i + 1} chapter table is truncated.')
            for chapter, pos in enumerate(range(start, finish, 4), 1):
                pgc, program = _u16(data, pos), _u16(data, pos + 2)
                refs.setdefault((pgc, program), []).append({'title': i + 1, 'chapter': chapter})
    except DvdMetadataError as error:
        warnings.append(str(error))
    return refs


def _parse_cadt(data, offset, warnings):
    cells = []
    if not offset:
        warnings.append('No physical cell address table; unreferenced-cell coverage is unknown.')
        return cells, False
    try:
        end = _table_bounds(data, offset, 'Cell address table')
        if end > len(data) or (end - offset - 8) % 12:
            raise DvdMetadataError('Cell address table is truncated or has an invalid length.')
        for pos in range(offset + 8, end, 12):
            first, last = _u32(data, pos + 4), _u32(data, pos + 8)
            if last < first:
                raise DvdMetadataError(f'Physical cell has an invalid sector range {first}..{last}.')
            cells.append({'vob_id': _u16(data, pos), 'cell_id': data[pos + 2],
                          'first_sector': first, 'last_sector': last})
        return cells, True
    except DvdMetadataError as error:
        warnings.append(str(error))
        return cells, False


def _recording(pgc, program, cells, refs, orphan=False):
    label = f'pgc_{pgc:03}_' + (f'cell_{cells[0]["cell"]:03}' if orphan else f'program_{program:03}')
    durations = [c['duration_seconds'] for c in cells]
    warnings = []
    if any(c['block_type'] or c['interleaved'] for c in cells):
        warnings.append('Angle/interleaved cells are preserved; automatic playback order may need review.')
    if orphan:
        warnings.append('Cell is not assigned to a program; retained separately to avoid omission.')
    return {'id': label, 'pgc': pgc, 'program': program,
            'title_ids': sorted({r['title'] for r in refs}), 'title_references': refs,
            'ranges': [(c['first_sector'], c['last_sector']) for c in cells],
            'cell_numbers': [c['cell'] for c in cells],
            'duration_seconds': sum(durations) if all(d is not None for d in durations) else None,
            'sector_count': sum(c['last_sector'] - c['first_sector'] + 1 for c in cells),
            'orphan': orphan, 'duplicate_of': None, 'overlaps': [], 'warnings': warnings}


def _parse_pgc(data, pgc, start, end, refs, result, entry_id):
    errors, warnings = result['errors'], result['warnings']
    label = f'PGC {pgc}'
    if start + 236 > end:
        raise DvdMetadataError(f'{label} header is truncated.')
    programs, cell_count = data[start + 2:start + 4]
    pgc_info = {'pgc': pgc, 'programs': programs, 'cells': cell_count,
                'entry_id': entry_id, 'title_hint': entry_id & 0x7f,
                'duration_seconds': _duration(data, start + 4, label, warnings)}
    result['pgcs'].append(pgc_info)
    if not cell_count:
        if programs:
            raise DvdMetadataError(f'{label} declares programs but no cells.')
        warnings.append(f'{label} contains no video cells.')
        return
    map_offset = _u16(data, start + 230)
    playback_offset = _u16(data, start + 232)
    position_offset = _u16(data, start + 234)
    if playback_offset < 236 or start + playback_offset + cell_count * 24 > end:
        raise DvdMetadataError(f'{label} cell playback table is truncated or outside its PGC.')
    positions_valid = position_offset >= 236 and start + position_offset + cell_count * 4 <= end
    if positions_valid and max(position_offset, playback_offset) < min(position_offset + cell_count * 4, playback_offset + cell_count * 24):
        positions_valid = False
    if position_offset and not positions_valid:
        warnings.append(f'{label} cell-position table is invalid; sector addresses remain available.')
    cells = []
    for i in range(cell_count):
        pos = start + playback_offset + i * 24
        first, last = _u32(data, pos + 8), _u32(data, pos + 20)
        cell = {'pgc': pgc, 'cell': i + 1, 'first_sector': first, 'last_sector': last,
                'duration_seconds': _duration(data, pos + 4, f'{label} cell {i + 1}', warnings),
                'block_mode': data[pos] >> 6, 'block_type': (data[pos] >> 4) & 3,
                'interleaved': bool(data[pos] & 4), 'valid': last >= first}
        if positions_valid:
            cp = start + position_offset + i * 4
            cell.update(vob_id=_u16(data, cp), cell_id=data[cp + 3])
        if not cell['valid']:
            errors.append(f'{label} cell {i + 1} has invalid sector range {first}..{last}.')
        cells.append(cell)
    result['cell_details'].extend(cells)
    valid_map = bool(programs) and programs <= cell_count and map_offset >= 236 and start + map_offset + programs <= end
    if valid_map and max(map_offset, playback_offset) < min(map_offset + programs, playback_offset + cell_count * 24):
        valid_map = False
    starts = list(data[start + map_offset:start + map_offset + programs]) if valid_map else []
    if valid_map:
        valid_map = starts == sorted(set(starts)) and all(1 <= c <= cell_count for c in starts)
    if not valid_map:
        errors.append(f'{label} program map is invalid; valid cells are retained separately.')
        for cell in cells:
            if cell['valid']:
                result['recordings'].append(_recording(pgc, None, [cell], [], orphan=True))
        return
    for i, first_cell in enumerate(starts):
        last_cell = starts[i + 1] - 1 if i + 1 < programs else cell_count
        selection = cells[first_cell - 1:last_cell]
        invalid = [c['cell'] for c in selection if not c['valid']]
        if invalid:
            errors.append(f'{label} program {i + 1} contains invalid cells {invalid}; available cells retained.')
            selection = [c for c in selection if c['valid']]
        if selection:
            rec = _recording(pgc, i + 1, selection, refs.get((pgc, i + 1), []))
            rec['missing_cells'] = invalid
            result['recordings'].append(rec)
    for cell in cells[:starts[0] - 1]:
        if cell['valid']:
            result['recordings'].append(_recording(pgc, None, [cell], [], orphan=True))
            errors.append(f'{label} cell {cell["cell"]} precedes the first program; retained separately.')


def _relationships(recordings):
    """Mark duplicates and physical overlap without discarding alternate programs."""
    signatures = {}
    intervals = []
    by_id = {r['id']: r for r in recordings}
    for rec in recordings:
        signature = tuple(rec['ranges'])
        if signature in signatures:
            rec['duplicate_of'] = signatures[signature]
        else:
            signatures[signature] = rec['id']
        intervals.extend((a, b, rec['id']) for a, b in merge_ranges(rec['ranges']))
    active = []
    for first, last, identity in sorted(intervals):
        active = [(b, name) for b, name in active if b >= first]
        for _, other in active:
            if other != identity:
                if other not in by_id[identity]['overlaps']:
                    by_id[identity]['overlaps'].append(other)
                if identity not in by_id[other]['overlaps']:
                    by_id[other]['overlaps'].append(identity)
        active.append((last, identity))


def parse_ifo_all(data):
    """Return all recoverable DVD-Video PGC programs and coverage diagnostics.

    Malformed individual chains do not hide valid chains: they are recorded in
    ``errors`` and ``complete`` is false. Fatal signature/directory errors raise
    DvdMetadataError. ``complete`` concerns metadata parsing, not media recovery
    or proof that every recording on a damaged disc has been discovered.
    """
    if not data.startswith(SIGNATURE):
        raise DvdMetadataError('Missing DVD-Video title metadata (DVD-VR is not parsed).')
    if len(data) < 0xe4:
        raise DvdMetadataError('Truncated DVD title-set header.')
    pgcit = _u32(data, 0xcc) * SECTOR_SIZE
    end = _table_bounds(data, pgcit, 'Program chain table')
    count = _u16(data, pgcit)
    directory_end = pgcit + 8 + count * 8
    if not 0 < count <= 4096 or directory_end > min(end, len(data)):
        raise DvdMetadataError('Program chain directory is invalid or truncated.')
    result = {'format': 'DVD-Video', 'video_offset': _u32(data, 0xc4),
              'vts_last_sector': _u32(data, 0x0c), 'vtsi_last_sector': _u32(data, 0x1c),
              'recordings': [], 'cells': 0, 'cell_details': [], 'pgcs': [],
              'warnings': [], 'errors': [], 'complete': False}
    result['backup_offset'] = (result['vts_last_sector'] - result['vtsi_last_sector']
                               if result['vts_last_sector'] > result['vtsi_last_sector'] else None)
    if end > len(data):
        result['errors'].append('Program chain table is truncated; available chains are retained.')
    if result['video_offset'] == 0:
        result['warnings'].append('Title VOB offset is zero; raw-track association requires validation.')
    refs = _parse_title_refs(data, _u32(data, 0xc8) * SECTOR_SIZE, result['warnings'])
    pointers = [pgcit + _u32(data, pgcit + 12 + i * 8) for i in range(count)]
    unique_pointers = sorted({p for p in pointers if directory_end <= p < min(end, len(data))})
    for i, start in enumerate(pointers):
        if not directory_end <= start < min(end, len(data)):
            result['errors'].append(f'PGC {i + 1} pointer is outside its program chain table.')
            continue
        finish = next((p for p in unique_pointers if p > start), min(end, len(data)))
        try:
            _parse_pgc(data, i + 1, start, finish, refs, result, data[pgcit + 8 + i * 8])
        except DvdMetadataError as error:
            result['errors'].append(str(error))
    result['cells'] = len(result['cell_details'])
    _relationships(result['recordings'])
    result['coverage_ranges'] = merge_ranges(r for rec in result['recordings'] for r in rec['ranges'])
    result['coverage_sectors'] = sum(b - a + 1 for a, b in result['coverage_ranges'])
    physical, physical_complete = _parse_cadt(data, _u32(data, 0xe0) * SECTOR_SIZE, result['warnings'])
    result['physical_cells'] = physical
    result['physical_cell_table_complete'] = physical_complete
    physical_ranges = [(c['first_sector'], c['last_sector']) for c in physical]
    result['unreferenced_cell_ranges'] = subtract_ranges(physical_ranges, result['coverage_ranges'])
    if result['unreferenced_cell_ranges']:
        result['errors'].append('Physical cell table contains sectors absent from program ranges; retain/salvage these separately.')
    if any(c['block_type'] or c['interleaved'] for c in result['cell_details']):
        result['errors'].append('Angle/interleaved cells require playback review; all referenced cell ranges are retained.')
    if not result['recordings']:
        result['errors'].append('No recoverable recordings found in the DVD program table.')
    result['complete'] = bool(result['recordings']) and not result['errors']
    return result


def copy_recording_ranges(source, target, ranges, cancel=None, chunk_size=1024 * 1024):
    """Extract exactly the ordered cell ranges from a local preserved VOB stream.

    Source is never modified. Existing targets are not overwritten. A partial
    target is preserved for inspection if local I/O or cancellation fails.
    ``cancel`` may be a callback that raises the caller's cancellation exception.
    """
    source, target = Path(source), Path(target)
    ranges = list(ranges)
    if not ranges:
        raise DvdMetadataError('Recording has no cell ranges.')
    size = source.stat().st_size
    for first, last in ranges:
        if first < 0 or last < first or (last + 1) * SECTOR_SIZE > size:
            raise DvdMetadataError(f'Recording range {first}..{last} exceeds the preserved video stream ({size} bytes).')
    if chunk_size <= 0:
        raise ValueError('chunk_size must be positive.')
    partial = target.with_suffix(target.suffix + '.partial')
    if target.exists():
        raise FileExistsError(target)
    written = 0
    with source.open('rb') as src, partial.open('xb') as dst:
        for first, last in ranges:
            src.seek(first * SECTOR_SIZE)
            remaining = (last - first + 1) * SECTOR_SIZE
            while remaining:
                if cancel:
                    cancel()
                block = src.read(min(chunk_size, remaining))
                if not block:
                    raise DvdMetadataError('Preserved video stream ended inside a recording cell.')
                dst.write(block)
                written += len(block)
                remaining -= len(block)
    partial.rename(target)
    return written


def find_ifo_offsets(data, *, base_offset=0, aligned_only=True):
    """Find embedded DVD-Video title headers, including later sessions/backups."""
    offsets = []
    cursor = 0
    while True:
        cursor = data.find(SIGNATURE, cursor)
        if cursor < 0:
            return offsets
        absolute = base_offset + cursor
        if not aligned_only or absolute % SECTOR_SIZE == 0:
            offsets.append(absolute)
        cursor += len(SIGNATURE)


def scan_metadata_file(path, *, max_metadata_bytes=32 * 1024 * 1024,
                       chunk_size=4 * 1024 * 1024, cancel=None):
    """Scan a local track/image for all sector-aligned VTS metadata candidates.

    Returns candidates including parse failures and identical backup pointers,
    with offset, sector, size, sha256, metadata, and error. No disc access occurs.
    Bounded buffers avoid loading a whole DVD into memory.
    """
    if chunk_size < len(SIGNATURE) or max_metadata_bytes < SECTOR_SIZE:
        raise ValueError('Scan buffers are too small.')
    path = Path(path)
    found, candidates = set(), []
    size = path.stat().st_size
    with path.open('rb') as src:
        position, tail = 0, b''
        while position < size:
            if cancel:
                cancel()
            src.seek(position)
            block = src.read(chunk_size)
            if not block:
                break
            data = tail + block
            for offset in find_ifo_offsets(data, base_offset=position - len(tail)):
                if offset in found:
                    continue
                found.add(offset)
                src.seek(offset)
                header = src.read(SECTOR_SIZE)
                declared = (_u32(header, 0x1c) + 1) * SECTOR_SIZE if len(header) >= 0x20 else 0
                length = min(size - offset, max_metadata_bytes, max(SECTOR_SIZE, declared))
                # Unfinished Handycam files may leave the IFO-length field at
                # zero. Read bounded continuation so table offsets can be used.
                if declared <= SECTOR_SIZE:
                    length = min(size - offset, max_metadata_bytes)
                src.seek(offset)
                metadata_data = src.read(length)
                entry = {'offset': offset, 'sector': offset // SECTOR_SIZE,
                         'size': len(metadata_data), 'declared_size': declared,
                         'sha256': hashlib.sha256(metadata_data).hexdigest(),
                         'metadata': None, 'error': None}
                try:
                    entry['metadata'] = parse_ifo_all(metadata_data)
                except DvdMetadataError as error:
                    entry['error'] = str(error)
                candidates.append(entry)
            tail = data[-(len(SIGNATURE) - 1):]
            position += len(block)
    return candidates
