"""Verify previous DVD track images before copying them into a new recovery.

No file is modified here. Reuse needs matching metadata plus at least three
distinct live sample comparisons; a partial file is never treated as complete.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dvd_layout import parse_ifo_all
from recovery_logging import error_details


SECTOR = 2048
MIN_SAMPLES = 3
SAMPLE_SECTORS = 4
MAX_SAMPLES = 5
MAX_METADATA_BYTES = 8192 * SECTOR


class CandidateRejected(ValueError):
    pass


def _event(logger, level, event, **fields):
    if logger is None:
        return
    if callable(logger):
        logger(level, event, **fields)
    else:
        logger.event(level, event, **fields)


def _check(cancel_callback):
    if cancel_callback:
        cancel_callback()


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CandidateRejected(f'{name} is not a valid integer.')
    return value


def _bad_sectors(value, start, available):
    if not isinstance(value, list):
        raise CandidateRejected('The prior bad-sector map is not a list.')
    bad = sorted({_integer(v, 'bad-sector LBA') for v in value})
    if any(v < start or v >= start + available for v in bad):
        raise CandidateRejected('The prior bad-sector map lies outside the available image prefix.')
    return bad


def _json(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise CandidateRejected('Image state is not a JSON object.')
    return value


def _state_source(state_path, state):
    """Resolve only a sibling track file, allowing recovery folders to be moved."""
    state_path = Path(state_path)
    normal = state_path.with_name(state_path.name.removesuffix('.state.json'))
    allowed = (normal, normal.with_suffix(normal.suffix + '.partial'))
    declared = Path(state['path']) if isinstance(state.get('path'), str) else None
    if declared is not None:
        for path in allowed:
            if declared.resolve() == path.resolve() and path.is_file():
                return path
    for path in allowed:
        if path.is_file():
            return path
    raise CandidateRejected('The prior track file is missing.')


def _validated_state(state_path):
    state = _json(state_path)
    start = _integer(state.get('start'), 'track start')
    count = _integer(state.get('count'), 'track count', 1)
    available = _integer(state.get('available_sectors'), 'available sectors', 1)
    if available > count:
        raise CandidateRejected('Available sectors exceed the declared track count.')
    path = _state_source(state_path, state)
    size = path.stat().st_size
    if size != available * SECTOR:
        raise CandidateRejected('The prior track byte length does not match its checkpointed complete-sector prefix.')
    bad = _bad_sectors(state.get('bad_sectors'), start, available)
    nested = state.get('track')
    if isinstance(nested, dict) and (nested.get('start') != start or nested.get('count') != count):
        raise CandidateRejected('The nested track coordinates disagree with the prior image state.')
    # A non-null interruption records a failed device operation, not corrupt
    # local bytes. Its available prefix can be reused after identity checks.
    return state, path, start, count, available, bad


def _metadata_hash(path):
    path = Path(path)
    size = path.stat().st_size
    if not SECTOR <= size <= MAX_METADATA_BYTES or size % SECTOR:
        raise CandidateRejected('The metadata image has an invalid byte length.')
    data = path.read_bytes()
    metadata = parse_ifo_all(data)
    if not metadata.get('complete') or metadata.get('errors'):
        raise CandidateRejected('The metadata does not contain a complete validated DVD title table.')
    return hashlib.sha256(data).hexdigest()


def _current_metadata(current_images, logger, cancel_callback):
    hashes = set()
    paths = set()
    for image in current_images:
        _check(cancel_callback)
        digest = image.get('metadata_sha256')
        if not digest:
            continue
        try:
            if image.get('bad_sectors') or image.get('complete') is not True:
                raise CandidateRejected('Current metadata image is incomplete or contains read errors.')
            count = _integer(image.get('count'), 'current metadata count', 1)
            available = _integer(image.get('available_sectors'), 'current metadata available sectors', 1)
            if available != count or count > 8192:
                raise CandidateRejected('Current metadata prefix is not a complete small metadata track.')
            path = Path(image['path'])
            if path.stat().st_size != count * SECTOR:
                raise CandidateRejected('Current metadata byte length disagrees with its track coordinates.')
            actual = _metadata_hash(path)
            if actual != digest:
                raise CandidateRejected('Current metadata SHA does not match its saved bytes.')
            hashes.add(actual)
            paths.add(path.resolve())
        except (OSError, ValueError, TypeError, KeyError) as error:
            _event(logger, 'WARNING', 'resume_current_metadata_rejected',
                   path=image.get('path'), error=error_details(error))
    return hashes, paths


def _matching_metadata(archive, hashes, logger, cancel_callback):
    """Require a separate, intact metadata track from the same prior run."""
    for state_path in sorted(archive.glob('track_*.bin.state.json')):
        _check(cancel_callback)
        try:
            state, path, start, count, available, bad = _validated_state(state_path)
            if count > 8192 or available != count or bad or state.get('complete') is not True:
                continue
            digest = _metadata_hash(path)
            if digest in hashes:
                return path, digest
        except (OSError, ValueError, TypeError, KeyError) as error:
            _event(logger, 'WARNING', 'resume_prior_metadata_rejected',
                   path=str(state_path), error=error_details(error))
            continue
    for path in sorted(archive.glob('metadata_track_*.bin')):
        _check(cancel_callback)
        try:
            digest = _metadata_hash(path)
            if digest in hashes:
                return path, digest
        except (OSError, ValueError, TypeError, KeyError) as error:
            _event(logger, 'WARNING', 'resume_prior_metadata_rejected',
                   path=str(path), error=error_details(error))
            continue
    return None


def _sample_offsets(available, bad_relative):
    """Prefer spread-out, disjoint samples, then fill from known-good spans."""
    if available < MIN_SAMPLES * SAMPLE_SECTORS:
        return []
    bad = set(bad_relative)
    last_start = available - SAMPLE_SECTORS
    anchors = [0, last_start // 4, last_start // 2, 3 * last_start // 4, last_start]
    offsets = []
    def add(offset):
        if not 0 <= offset <= last_start:
            return
        if any(abs(offset - prior) < SAMPLE_SECTORS for prior in offsets):
            return
        if any(sector in bad for sector in range(offset, offset + SAMPLE_SECTORS)):
            return
        offsets.append(offset)
    for offset in anchors:
        add(offset)
    # A bad sector at a preferred anchor should not prevent reuse if enough
    # other intact sectors are available. Walk good intervals without issuing
    # speculative reads through the known damaged addresses.
    cursor = 0
    for boundary in sorted(bad) + [available]:
        while cursor + SAMPLE_SECTORS <= boundary and len(offsets) < MAX_SAMPLES:
            add(cursor)
            cursor += SAMPLE_SECTORS
        if len(offsets) >= MAX_SAMPLES:
            break
        cursor = max(cursor, boundary + 1)
    return sorted(offsets[:MAX_SAMPLES]) if len(offsets) >= MIN_SAMPLES else []


def _verify_samples(disc, path, start, available, bad, logger, cancel_callback):
    offsets = _sample_offsets(available, [lba - start for lba in bad])
    if len(offsets) < MIN_SAMPLES:
        raise CandidateRejected('Fewer than three disjoint intact live samples fit inside the available prefix.')
    compared = []
    with path.open('rb') as source:
        for offset in offsets:
            _check(cancel_callback)
            source.seek(offset * SECTOR)
            expected = source.read(SAMPLE_SECTORS * SECTOR)
            if len(expected) != SAMPLE_SECTORS * SECTOR:
                raise CandidateRejected('The prior source changed or ended during sample verification.')
            try:
                actual = disc.sectors(start + offset, SAMPLE_SECTORS)
            except Exception as error:
                # ReadError is a failed sample on this medium. Device removal,
                # reset and cancellation must stop the operation rather than
                # silently trying another local candidate against another disc.
                import recovery
                if not isinstance(error, recovery.ReadError):
                    raise
                _event(logger, 'WARNING', 'resume_live_sample_read_failed',
                       source=str(path), lba=start + offset, sector_count=SAMPLE_SECTORS,
                       error=error_details(error))
                raise CandidateRejected('A live identity sample could not be read.') from error
            if actual != expected:
                raise CandidateRejected(f'Live sectors differ from the prior image at LBA {start + offset}.')
            compared.append({'lba': start + offset, 'sector_count': SAMPLE_SECTORS})
    return compared


def find_verified_previous(disc, track, current_images, root, logger=None,
                           cancel_callback=None):
    """Return (read-only source path, absolute bad-sector list), or (None, None).

    New-format partial images need exact state coordinates and checkpointed
    prefix lengths. Legacy MPG files need the entire declared track. Both need
    an intact matching sibling metadata image and >=3 actual live comparisons.
    Candidates are tried newest first; every rejection has a detailed log.
    """
    _check(cancel_callback)
    start = _integer(track.get('start'), 'requested track start')
    count = _integer(track.get('count'), 'requested track count', 1)
    hashes, current_paths = _current_metadata(current_images, logger, cancel_callback)
    if not hashes:
        _event(logger, 'INFO', 'resume_not_available', reason='No complete validated current metadata track is available.')
        return None, None
    root = Path(root)
    candidates = []
    for archive in root.glob('*/Original recovery'):
        if not archive.is_dir():
            continue
        for state_path in archive.glob('track_*.bin.state.json'):
            candidates.append((state_path.stat().st_mtime_ns, 'state', state_path))
        for path in archive.glob('Handycam_original.mpg'):
            candidates.append((path.stat().st_mtime_ns, 'legacy', path))
    candidates.sort(key=lambda entry: (entry[0], str(entry[2])), reverse=True)
    metadata_cache = {}
    for _, kind, candidate in candidates:
        _check(cancel_callback)
        try:
            if kind == 'state':
                state, source, old_start, old_count, available, bad = _validated_state(candidate)
                if old_start != start or old_count != count:
                    continue
            else:
                source = candidate
                if source.stat().st_size != count * SECTOR:
                    continue
                available = count
                bad_path = source.parent.parent / 'read_errors.json'
                if not bad_path.is_file():
                    raise CandidateRejected('The legacy source has no saved bad-sector map.')
                bad = _bad_sectors(json.loads(bad_path.read_text(encoding='utf-8')), start, available)
            if source.resolve() in current_paths:
                continue
            archive = candidate.parent
            if archive not in metadata_cache:
                metadata_cache[archive] = _matching_metadata(archive, hashes, logger, cancel_callback)
            match = metadata_cache[archive]
            if not match or match[0].resolve() == source.resolve():
                raise CandidateRejected('No separate intact metadata track matches the current DVD metadata SHA.')
            original_stat = source.stat()
            original_identity = (original_stat.st_size, original_stat.st_mtime_ns)
            checkpoint_identity = candidate.stat().st_mtime_ns
            samples = _verify_samples(disc, source, start, available, bad, logger, cancel_callback)
            current_stat = source.stat()
            if (current_stat.st_size, current_stat.st_mtime_ns) != original_identity or candidate.stat().st_mtime_ns != checkpoint_identity:
                raise CandidateRejected('The prior image or its checkpoint changed during verification.')
            _event(logger, 'INFO', 'previous_image_identity_verified', source=str(source),
                   source_format=kind, track=track, available_sectors=available,
                   sample_count=len(samples), samples=samples, metadata_path=str(match[0]),
                   metadata_sha256=match[1], known_bad_sectors=bad, source_modified=False)
            return source, bad
        except (OSError, ValueError, TypeError, KeyError) as error:
            _event(logger, 'WARNING', 'resume_candidate_rejected', candidate=str(candidate),
                   source_format=kind, error=error_details(error))
    return None, None
