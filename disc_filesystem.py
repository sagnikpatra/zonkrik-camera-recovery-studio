"""Read-only ISO9660/Joliet inventory from preserved optical-sector images.

Directory record and descriptor offsets follow Linux include/uapi/linux/iso_fs.h.
File ranges are absolute inclusive LBAs. extent_sizes retains the exact number
of file bytes in each range, so sector padding can be removed per extent.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

SECTOR = 2048
MAX_DIRECTORY_BYTES = 64 * 1024 * 1024
MAX_TREE_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 200000


class IsoError(ValueError):
    pass


def _both(data, offset, width, label):
    if offset + width * 2 > len(data):
        raise IsoError(f'Truncated {label}.')
    little = int.from_bytes(data[offset:offset + width], 'little')
    big = int.from_bytes(data[offset + width:offset + width * 2], 'big')
    if little != big:
        raise IsoError(f'Endian copies disagree for {label}.')
    return little


def _record(data):
    if len(data) < 34 or data[0] != len(data) or not data[32] or 33 + data[32] > len(data):
        raise IsoError('Invalid directory record length or file identifier length.')
    return {'extent': _both(data, 2, 4, 'extent'), 'byte_size': _both(data, 10, 4, 'file size'),
            'flags': data[25], 'unit': data[26], 'gap': data[27], 'xattr': data[1],
            'volume_sequence': _both(data, 28, 2, 'volume sequence'), 'raw_name': data[33:33 + data[32]]}


def safe_filename(name):
    """Keep Unicode while preventing Windows path/control/device-name escapes."""
    name = re.sub(r';\d+$', '', name)
    cleaned = ''.join('_' if ord(c) < 32 or c in '<>:"/\\|?*' else c for c in name).rstrip(' .')
    if cleaned in ('', '.', '..'):
        cleaned = '_unnamed'
    if cleaned.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        cleaned = '_' + cleaned
    if len(cleaned) > 180:
        suffix = Path(cleaned).suffix[:20]
        cleaned = cleaned[:140] + '_' + hashlib.sha256(name.encode('utf-8')).hexdigest()[:12] + suffix
    return cleaned


class _Space:
    def __init__(self, images):
        self.images = sorted(images, key=lambda image: image['start'])

    def read(self, start, size):
        result = bytearray()
        position = start
        while len(result) < size:
            image = next((i for i in self.images if i['start'] <= position < i['start'] + i['available_sectors']), None)
            if image is None:
                raise IsoError(f'Directory/descriptor sector {position} is absent from preserved images.')
            count = min((size - len(result) + SECTOR - 1) // SECTOR,
                        image['start'] + image['available_sectors'] - position)
            if any(position <= b < position + count for b in image.get('bad_sectors', [])):
                raise IsoError(f'Directory/descriptor sectors {position}..{position + count - 1} contain known read errors.')
            with Path(image['path']).open('rb') as source:
                source.seek((position - image['start']) * SECTOR)
                block = source.read(count * SECTOR)
            if len(block) != count * SECTOR:
                raise IsoError(f'Preserved image ended at directory/descriptor sector {position}.')
            result.extend(block)
            position += count
        return bytes(result[:size])

    def coverage(self, ranges):
        missing, bad = [], set()
        for first, last in ranges:
            position = first
            while position <= last:
                image = next((i for i in self.images if i['start'] <= position < i['start'] + i['available_sectors']), None)
                if image is None:
                    following = [i['start'] for i in self.images if i['start'] > position]
                    end = min(last, min(following) - 1) if following else last
                    missing.append((position, end)); position = end + 1
                    continue
                end = min(last, image['start'] + image['available_sectors'] - 1)
                bad.update(b for b in image.get('bad_sectors', []) if position <= b <= end)
                position = end + 1
        return missing, sorted(bad)


def _extents(records, address_base, volume_sectors):
    ranges, sizes = [], []
    for record in records:
        remaining = record['byte_size']
        relative = record['extent'] + record['xattr']
        if relative < 0 or relative >= volume_sectors and remaining:
            raise IsoError('File extent begins outside its declared volume.')
        unit, gap = record['unit'], record['gap']
        if bool(unit) != bool(gap):
            raise IsoError('Invalid interleaved file unit/gap combination.')
        chunks = 0
        while remaining:
            length = min(remaining, unit * SECTOR) if unit else remaining
            sectors = (length + SECTOR - 1) // SECTOR
            if relative + sectors > volume_sectors:
                raise IsoError('File extent exceeds its declared volume.')
            ranges.append((address_base + relative, address_base + relative + sectors - 1))
            sizes.append(length)
            remaining -= length
            relative += sectors + gap
            chunks += 1
            if chunks > MAX_ENTRIES:
                raise IsoError('Interleaved file exceeds the safe extent-count limit.')
    return ranges, sizes


def scan_iso9660(images_or_path, logger=None, cancel_callback=None, *, base_sector=0, max_depth=128):
    """Inventory every file in each available primary/Joliet directory tree.

    Accepts a file path, a list of SectorSpace image dicts, or an object exposing
    ``images``. A path has base_sector=0 by default. Descriptor sets are sought
    at each recorded track/session start+16. Both session-relative and absolute
    extent addressing are validated against the root directory's dot record.
    Broken entries/trees are logged and retained as partial diagnostics while
    healthy siblings continue. UDF is deliberately not parsed by this module.
    """
    if isinstance(images_or_path, (str, Path)):
        path = Path(images_or_path)
        images = [{'path': str(path), 'start': base_sector,
                   'available_sectors': path.stat().st_size // SECTOR, 'bad_sectors': []}]
    else:
        images = list(getattr(images_or_path, 'images', images_or_path))
    result = {'format': 'ISO9660/Joliet', 'detected': False, 'complete': False,
              'files': [], 'volumes': [], 'warnings': [], 'errors': [], 'directories': 0}
    space = _Space(images)
    def check():
        if cancel_callback:
            cancel_callback()
    def issue(message, **fields):
        result['errors'].append(message)
        if logger:
            logger('WARNING', 'iso9660_parse_issue', message=message, **fields)
    descriptors = []
    for session in sorted({i['start'] for i in images}):
        check()
        for relative in range(16, 16 + 64):
            try:
                block = space.read(session + relative, SECTOR)
            except (OSError, IsoError):
                break
            if block[1:6] != b'CD001' or block[6] != 1:
                break
            result['detected'] = True
            if block[0] == 255:
                break
            if block[0] == 1 or block[0] == 2 and block[88:91] in (b'%/@', b'%/C', b'%/E'):
                descriptors.append((session, session + relative, block))
    # Unicode namespaces are preferred; primary views still fill omissions.
    descriptors.sort(key=lambda d: (d[0], d[2][0] != 2, d[1]))
    physical_files = {}
    for session, descriptor_sector, descriptor in descriptors:
        check()
        joliet = descriptor[0] == 2
        try:
            sectors = _both(descriptor, 80, 4, 'volume size')
            if _both(descriptor, 128, 2, 'logical block size') != SECTOR or not sectors:
                raise IsoError('Unsupported ISO logical block size or empty volume.')
            root_length = descriptor[156]
            root = _record(descriptor[156:156 + root_length])
            if not root['flags'] & 2 or root['byte_size'] > MAX_DIRECTORY_BYTES:
                raise IsoError('Invalid root directory record.')
            valid_bases = []
            for address_base in dict.fromkeys((session, 0)):
                try:
                    ranges, sizes = _extents([root], address_base, sectors)
                    dot_data = space.read(ranges[0][0], min(SECTOR, root['byte_size']))
                    dot = _record(dot_data[:dot_data[0]])
                    if dot['raw_name'] == b'\0' and dot['flags'] & 2 and dot['extent'] == root['extent']:
                        valid_bases.append(address_base)
                except (OSError, IsoError, IndexError):
                    continue
            if not valid_bases:
                raise IsoError('Neither session-relative nor absolute root extent addresses are readable.')
            if len(valid_bases) > 1:
                issue('Ambiguous multisession extent addresses; both readable directory trees are retained.', descriptor_sector=descriptor_sector)
            for address_base in valid_bases:
                encoding = 'utf-16-be' if joliet else 'ascii'
                label = descriptor[40:72].decode(encoding, errors='replace').strip(' \0')
                volume = {'descriptor_sector': descriptor_sector, 'session_start': session,
                          'address_base': address_base, 'joliet': joliet, 'label': label,
                          'volume_sectors': sectors, 'files': 0}
                result['volumes'].append(volume)
                stack = [([], [root], 0)]
                visited, tree_bytes, entries_count = set(), 0, 0
                while stack:
                    check()
                    components, dir_records, depth = stack.pop()
                    directory_path = '/'.join(components)
                    try:
                        if depth > max_depth:
                            raise IsoError(f'Directory depth exceeds {max_depth}: {directory_path}')
                        ranges, sizes = _extents(dir_records, address_base, sectors)
                        identity = tuple(ranges)
                        if identity in visited:
                            raise IsoError(f'Directory extent cycle or repeated directory reference: {directory_path}')
                        visited.add(identity)
                        total = sum(sizes)
                        tree_bytes += total
                        if not 0 < total <= MAX_DIRECTORY_BYTES or tree_bytes > MAX_TREE_BYTES:
                            raise IsoError('Directory byte limit exceeded.')
                        data = b''.join(space.read(a, size) for (a, b), size in zip(ranges, sizes))
                        result['directories'] += 1
                    except (OSError, IsoError) as error:
                        issue(str(error), path=directory_path, descriptor_sector=descriptor_sector)
                        continue
                    records, pos = [], 0
                    while pos < len(data):
                        check()
                        length = data[pos]
                        if not length:
                            pos = min(len(data), ((pos // SECTOR) + 1) * SECTOR)
                            continue
                        try:
                            if pos + length > len(data) or pos % SECTOR + length > SECTOR:
                                raise IsoError('Directory record crosses a logical block boundary.')
                            record = _record(data[pos:pos + length])
                            records.append(record)
                            entries_count += 1
                            if entries_count > MAX_ENTRIES:
                                raise IsoError('Directory entry-count limit exceeded.')
                            pos += length
                        except IsoError as error:
                            issue(str(error), path=directory_path, directory_byte_offset=pos)
                            # Resynchronize within this block so a corrupt entry
                            # does not suppress otherwise healthy sibling files.
                            recovered = None
                            for trial in range(pos + 1, min(len(data), (pos // SECTOR + 1) * SECTOR)):
                                n = data[trial]
                                if n >= 34 and trial % SECTOR + n <= SECTOR and trial + n <= len(data):
                                    try:
                                        _record(data[trial:trial + n]); recovered = trial; break
                                    except IsoError:
                                        pass
                            pos = recovered if recovered is not None else min(len(data), (pos // SECTOR + 1) * SECTOR)
                            if entries_count > MAX_ENTRIES:
                                stack.clear(); break
                    index = 0
                    while index < len(records):
                        record = records[index]; index += 1
                        if record['raw_name'] in (b'\0', b'\1'):
                            continue
                        chain = [record]
                        unterminated = False
                        while chain[-1]['flags'] & 0x80:
                            if index >= len(records) or records[index]['raw_name'] != record['raw_name']:
                                issue('Unterminated/mismatched multi-extent file; available extents retained.', path=directory_path)
                                unterminated = True; break
                            chain.append(records[index]); index += 1
                        try:
                            original = record['raw_name'].decode(encoding)
                        except UnicodeDecodeError:
                            original = record['raw_name'].decode(encoding, errors='replace')
                            issue('Invalid file identifier encoding; replacement characters retained.', path=directory_path)
                        name = safe_filename(original)
                        relative_path = '/'.join(components + [name])
                        if record['flags'] & 2:
                            stack.append((components + [name], chain, depth + 1))
                            continue
                        try:
                            file_ranges, file_sizes = _extents(chain, address_base, sectors)
                            missing, bad = space.coverage(file_ranges)
                            file_complete = not missing and not bad and not unterminated
                            if not file_complete:
                                issue('File extents are incomplete or contain read errors.', path=relative_path,
                                      missing_ranges=missing, unreadable_sectors=bad)
                            item = {'name': name, 'original_name': original, 'relative_path': relative_path,
                                    'absolute_ranges': file_ranges, 'extent_sizes': file_sizes,
                                    'byte_size': sum(file_sizes), 'complete': file_complete,
                                    'missing_ranges': missing, 'unreadable_sectors': bad,
                                    'joliet': joliet, 'descriptor_sector': descriptor_sector,
                                    'multi_extent': len(chain) > 1, 'aliases': []}
                            identity = (tuple(file_ranges), tuple(file_sizes), name if not file_ranges else None)
                            existing = physical_files.get(identity)
                            if existing is not None and existing['descriptor_sector'] != descriptor_sector:
                                if relative_path != existing['relative_path'] and relative_path not in existing['aliases']:
                                    existing['aliases'].append(relative_path)
                            else:
                                result['files'].append(item); physical_files[identity] = item
                            volume['files'] += 1
                        except IsoError as error:
                            issue(str(error), path=relative_path)
                            # Preserve the declared file identity/count even
                            # when its unsafe extent cannot be extracted.
                            result['files'].append({'name': name, 'original_name': original,
                                'relative_path': relative_path, 'absolute_ranges': [],
                                'extent_sizes': [], 'byte_size': sum(r['byte_size'] for r in chain),
                                'complete': False, 'missing_ranges': [], 'unreadable_sectors': [],
                                'joliet': joliet, 'descriptor_sector': descriptor_sector,
                                'multi_extent': len(chain) > 1, 'aliases': [], 'extraction_error': str(error)})
                            volume['files'] += 1
        except (OSError, IsoError, IndexError) as error:
            issue(str(error), descriptor_sector=descriptor_sector)
    result['complete'] = bool(result['detected'] and result['volumes']) and not result['errors']
    if logger:
        logger('INFO', 'iso9660_inventory_finished', detected=result['detected'],
               complete=result['complete'], file_count=len(result['files']),
               total_file_bytes=sum(f['byte_size'] for f in result['files']), errors=result['errors'])
    return result
