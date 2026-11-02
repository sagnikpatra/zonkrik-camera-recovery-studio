"""Bounded, resumable read-only optical imaging into a new destination file."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
import traceback

from recovery_logging import error_details

SECTOR_BYTES = 2048


class NoReadProgress(Exception):
    pass


class RepairBudgetExceeded(Exception):
    pass


def _ranges(sectors):
    """Compact absolute LBAs into adjacent ranges without implying readability."""
    result=[]
    for lba in sorted(sectors):
        if result and result[-1]['start']+result[-1]['count']==lba:
            result[-1]['count']+=1
        else:
            result.append({'start':lba,'count':1})
    return result


def _json(path, value, durable=False):
    pending = path.with_suffix(path.suffix + '.pending')
    with pending.open('w',encoding='utf-8') as stream:
        stream.write(json.dumps(value,indent=2,default=str))
        stream.flush()
        if durable:os.fsync(stream.fileno())
    pending.replace(path)


class DiscImager:
    """Retry damaged sectors, preserve every copied byte, never modify old images.

    A caller must verify disc identity before supplying ``existing_source``.
    ``existing_bad`` contains absolute LBAs, not file-relative offsets. Ordinary
    read errors are retried and isolated; removed/reset devices immediately stop
    without filling their remaining sectors with zeros. Cancel persists and
    raises recovery.Cancelled. Other interruptions return a partial result.
    """

    def __init__(self, disc, logger, status, cancel, max_attempts=3,
                 repair_budget_seconds=120, no_progress_timeout=120, adaptive_reads=False):
        self.disc = disc
        self.logger = logger
        self.status = status or (lambda *args: None)
        self.cancel = cancel if cancel is not None else threading.Event()
        self.max_attempts = max(1, min(int(max_attempts), 10))
        self.no_progress_timeout = max(.001,float(no_progress_timeout))
        self.repair_budget_seconds = max(0.,float(repair_budget_seconds))
        self.adaptive_reads=adaptive_reads
        # Keep one imaging request within the adapter's native transfer limit so
        # a later device removal cannot discard earlier successful subreads.
        self.block_sectors = max(1, min(64, int(getattr(disc, 'max_read_sectors', 64))))

    def _event(self, level, event, **fields):
        if self.logger is None:
            return
        if callable(self.logger):
            self.logger(level, event, **fields)
        else:
            self.logger.event(level, event, **fields)

    def image(self, track, target, existing_source=None, existing_bad=None):
        # Local import is deliberate: the main recovery module imports this
        # imager only after defining the device and exception classes.
        import recovery

        target = Path(target)
        partial = target.with_suffix(target.suffix + '.partial')
        state_path = target.with_suffix(target.suffix + '.state.json')
        bad_map_path = target.with_suffix(target.suffix + '.bad-sectors.json')
        start, count = int(track['start']), int(track['count'])
        if start < 0 or count <= 0:
            raise recovery.RecoveryError('Invalid optical image sector range.')
        # Unique run directories should make this impossible; refuse accidental
        # replacement even if the caller chooses a previous output by mistake.
        if any(path.exists() for path in (target, partial, state_path, bad_map_path)):
            raise recovery.RecoveryError('The image or its state already exists. Choose a new recovery folder to preserve it.')
        if existing_source is not None:
            existing_source = Path(existing_source)
            if existing_source.resolve() in (target.resolve(), partial.resolve()):
                raise recovery.RecoveryError('The previous image must be different from the new output.')
            if not existing_source.is_file() or existing_source.stat().st_size > count * SECTOR_BYTES:
                raise recovery.RecoveryError('The previous image size is inconsistent with this track.')
        bad = {int(lba) for lba in (existing_bad or []) if start <= int(lba) < start + count}
        target.parent.mkdir(parents=True, exist_ok=True)
        result = {'path': str(partial), 'start': start, 'count': count, 'available_sectors': 0,
                  'readable_sectors': 0, 'complete': False, 'bad_sectors': sorted(bad),
                  'interrupted': None, 'state_path': str(state_path), 'bad_map_path': str(bad_map_path),
                  'existing_source': str(existing_source) if existing_source else None,
                  'attempts_total': 0, 'failed_attempts': 0, 'recovered_bad_sectors': [],
                  'first_pass_complete': False, 'repair_budget_seconds': self.repair_budget_seconds,
                  'no_progress_timeout_seconds': self.no_progress_timeout,
                  'repair_budget_exhausted': False, 'confirmed_unreadable_sectors': [],
                  'track': dict(track), 'source_modified': False}
        last_success = time.monotonic()
        repair_started = None
        confirmed_bad = set()
        phase = 'initializing'
        output = None

        def checkpoint(durable=False):
            if output is not None and not output.closed:
                output.flush()
                if durable:
                    os.fsync(output.fileno())
                sectors = os.fstat(output.fileno()).st_size // SECTOR_BYTES
            else:
                path = Path(result['path'])
                sectors = path.stat().st_size // SECTOR_BYTES if path.exists() else 0
            result['available_sectors'] = sectors
            result['bad_sectors'] = sorted(bad)
            result['pending_ranges'] = _ranges(bad)
            result['unattempted_bad_sectors'] = sorted(bad-confirmed_bad)
            result['confirmed_unreadable_sectors'] = sorted(confirmed_bad & bad)
            result['readable_sectors'] = sectors - sum(start <= lba < start + sectors for lba in bad)
            result['phase'] = phase
            result['updated_utc'] = datetime.now(timezone.utc).isoformat()
            _json(bad_map_path, {'start': start, 'count': count, 'bad_sectors': sorted(bad),
                                'available_sectors': sectors, 'interrupted': result['interrupted'],
                                'first_pass_complete':result['first_pass_complete'],
                                'pending_ranges':result['pending_ranges'],
                                'unattempted_bad_sectors':result['unattempted_bad_sectors'],
                                'confirmed_unreadable_sectors':result['confirmed_unreadable_sectors'],
                                'repair_budget_exhausted':result['repair_budget_exhausted']},durable=durable)
            _json(state_path, result,durable=durable)

        def check():
            if self.cancel.is_set():
                raise recovery.Cancelled('Stopped. The partial image, sector map, and detailed logs remain saved.')
            if phase == 'copying_verified_previous_image':
                return
            if repair_started is not None and phase == 'repairing_pending_ranges':
                elapsed=time.monotonic()-repair_started
                if elapsed >= self.repair_budget_seconds:
                    error=RepairBudgetExceeded(f'The {self.repair_budget_seconds:.1f}-second repair budget was reached. Remaining sectors are preserved as pending; later healthy footage was copied first.')
                    error.duration_seconds=elapsed
                    raise error
            gap = time.monotonic() - last_success
            if gap >= self.no_progress_timeout:
                error = NoReadProgress(f'Stopped after {gap:.1f} seconds without a successful sector read. The accessible image is preserved; reconnect or change the reader before retrying.')
                error.duration_seconds = gap
                raise error

        def read(lba, amount, attempts):
            nonlocal last_success
            for attempt in range(1, attempts + 1):
                check()
                result['attempts_total'] += 1
                began = time.monotonic()
                try:
                    data = self.disc.sectors(lba, amount)
                    if len(data) != amount * SECTOR_BYTES:
                        error = recovery.ReadError(f'Short sector read: received {len(data)} of {amount * SECTOR_BYTES} bytes.')
                        error.requested_bytes = amount * SECTOR_BYTES
                        error.transferred_bytes = len(data)
                        raise error
                    last_success = time.monotonic()
                    if attempt > 1:
                        self._event('INFO', 'sector_read_recovered', lba=lba, sector_count=amount,
                                    attempt=attempt, phase=phase, elapsed_seconds=last_success-began)
                    return data
                except recovery.Cancelled:
                    raise
                except Exception as error:
                    result['failed_attempts'] += 1
                    self._event('ERROR', 'sector_read_failed', lba=lba, sector_count=amount,
                                attempt=attempt, max_attempts=attempts, phase=phase,
                                elapsed_seconds=time.monotonic()-began, error=error_details(error),
                                traceback=''.join(traceback.format_exception(type(error), error, error.__traceback__)))
                    checkpoint(durable=True)
                    # Missing disc, reset, or disconnected USB is never a sector
                    # defect and must not be converted to zero-filled content.
                    if not isinstance(error, recovery.ReadError):
                        raise
                    if attempt == attempts:
                        return None
                    check()
            return None

        def save(lba, data, durable=False):
            output.seek((lba-start) * SECTOR_BYTES)
            output.write(data)
            checkpoint(durable=durable)

        def repair_range(lba, amount):
            check()
            # Only the deferred repair phase descends into damaged ranges.
            # Read healthy subranges in one transfer and retry isolated sectors.
            data = read(lba, amount, self.max_attempts if amount == 1 else 1)
            if data is not None:
                repaired=sorted(bad.intersection(range(lba,lba+amount)))
                save(lba, data,durable=True)
                bad.difference_update(repaired)
                result['recovered_bad_sectors'].extend(repaired)
                checkpoint(durable=True)
                if repaired:
                    self._event('INFO','pending_range_repaired',lba=lba,sector_count=amount,
                                repaired_sector_count=len(repaired),source_modified=False)
                return
            if amount > 1:
                first = amount // 2
                self._event('WARNING', 'read_range_split', lba=lba, sector_count=amount,
                            left_count=first, right_count=amount-first, phase=phase)
                repair_range(lba, first)
                repair_range(lba+first, amount-first)
                return
            confirmed_bad.add(lba)
            checkpoint(durable=True)
            self._event('ERROR', 'sector_unreadable', lba=lba, sector_count=1,
                        attempts=self.max_attempts, phase=phase, replacement='zero placeholder in new image only')

        try:
            with partial.open('xb') as output:
                checkpoint()
                if existing_source is not None:
                    phase = 'copying_verified_previous_image'
                    self._event('INFO', 'previous_image_copy_started', source=str(existing_source), target=str(partial))
                    full_bytes = (existing_source.stat().st_size // SECTOR_BYTES) * SECTOR_BYTES
                    copied = 0
                    with existing_source.open('rb') as previous:
                        while copied < full_bytes:
                            check()
                            data = previous.read(min(4 * 1024 * 1024, full_bytes-copied))
                            if not data:
                                raise recovery.RecoveryError('The previous image became incomplete while copying.')
                            output.write(data)
                            copied += len(data)
                            checkpoint()
                    self._event('INFO', 'previous_image_copied', bytes_copied=copied,
                                ignored_partial_sector_bytes=existing_source.stat().st_size-full_bytes,
                                source_modified=False)
                    # Copying an existing local image is not a failing device
                    # timeout. Start the read-progress clock when disc I/O begins.
                    last_success = time.monotonic()
                phase = 'copying_disc_sectors'
                copied_sectors = os.fstat(output.fileno()).st_size // SECTOR_BYTES
                # A prior map cannot mark bytes beyond its copied prefix as
                # readable. Future holes will be accounted for by first pass.
                bad.intersection_update(lba for lba in tuple(bad) if start <= lba < start+copied_sectors)
                position=copied_sectors;read_limit=self.block_sectors;ceiling=read_limit;clean_run=0;allow_adaptive=self.adaptive_reads
                while position<count:
                    lba=start+position;amount=min(read_limit,count-position)
                    data=read(lba,amount,1)
                    if data is None and allow_adaptive and amount>1:
                        # Some damaged media/bridges reject a block containing
                        # otherwise readable sectors. Probe once, then advance
                        # only by bytes actually read; never skip the remainder.
                        probe=read(lba,1,1)
                        if probe is not None:
                            ceiling=min(ceiling,max(1,amount//2));read_limit=1;clean_run=0
                            self._event('WARNING','adaptive_smaller_reads',lba=lba,failed_sector_count=amount,new_sector_count=1,maximum_future_sector_count=ceiling)
                            amount=1;data=probe
                    if data is None:
                        clean_run=0
                        if allow_adaptive:
                            # A truly bad sector must not trap the fast pass in
                            # one-sector requests until its no-progress limit.
                            # Keep later footage moving; isolate holes in repair.
                            allow_adaptive=False;read_limit=self.block_sectors
                            self._event('WARNING','fast_pass_restored_after_read_failure',lba=lba,next_sector_count=read_limit)
                        bad.update(range(lba,lba+amount))
                        save(lba,bytes(amount*SECTOR_BYTES),durable=True)
                        self._event('WARNING','read_range_deferred',lba=lba,sector_count=amount,
                                    phase=phase,reason='First pass continues to later footage before any recursive retries.')
                    else:
                        save(lba,data)
                        clean_run+=amount
                        if allow_adaptive and clean_run>=64 and read_limit<ceiling:read_limit=min(ceiling,read_limit*2);clean_run=0
                    position+=amount
                    self.status(f'Copying disc sectors · {position*100/count:.1f}% · unreadable: {len(bad)} · read size {read_limit}',
                                min(70,70*position/count))
                result['first_pass_complete']=True
                checkpoint(durable=True)
                self._event('INFO','disc_first_pass_finished',available_sectors=count,
                            pending_sector_count=len(bad),pending_ranges=_ranges(bad))
                phase = 'repairing_pending_ranges'
                repair_started=time.monotonic()
                try:
                    for index,pending in enumerate(_ranges(bad),1):
                        # Never request a repair larger than the adapter limit,
                        # including contiguous bad ranges from a previous image.
                        for offset in range(0,pending['count'],self.block_sectors):
                            self.status(f'Repairing pending range {index} · {len(bad)} sectors remain · {max(0,self.repair_budget_seconds-(time.monotonic()-repair_started)):.0f}s budget',70)
                            repair_range(pending['start']+offset,min(self.block_sectors,pending['count']-offset))
                except (RepairBudgetExceeded,NoReadProgress) as error:
                    result['interrupted']=error_details(error)
                    result['repair_budget_exhausted']=isinstance(error,RepairBudgetExceeded)
                    self._event('WARNING','disc_repairs_deferred',error=error_details(error),
                                pending_sector_count=len(bad),pending_ranges=_ranges(bad),
                                unattempted_bad_sectors=sorted(bad-confirmed_bad),
                                first_pass_complete=True)
                    checkpoint(durable=True)
                result['repair_elapsed_seconds']=round(time.monotonic()-repair_started,3)
                phase = 'finished'
                checkpoint(durable=True)
            output = None
            if partial.stat().st_size == count * SECTOR_BYTES:
                partial.rename(target)
                result['path'] = str(target)
            result['complete'] = (Path(result['path']).stat().st_size == count * SECTOR_BYTES and not bad and not result['interrupted'])
            checkpoint()
            self._event('INFO' if result['complete'] else 'WARNING', 'disc_image_finished', **result)
            return result
        except recovery.Cancelled as error:
            phase = 'cancelled'
            result['interrupted'] = error_details(error)
            checkpoint(durable=True)
            self._event('WARNING', 'disc_image_cancelled', **result)
            raise
        except Exception as error:
            phase = 'interrupted'
            result['interrupted'] = error_details(error)
            result['complete'] = False
            checkpoint(durable=True)
            self._event('ERROR', 'disc_image_interrupted', **result,
                        traceback=''.join(traceback.format_exception(type(error), error, error.__traceback__)))
            return result
        finally:
            # Flush state on every exit, including callback errors or failures
            # while repairing previously unreadable sectors.
            checkpoint(durable=True)

