import unittest,tempfile,threading
from pathlib import Path
from disc_imaging import DiscImager,SECTOR_BYTES
from recovery import ReadError,DriveUnavailable

ROOT=Path(r'D:\Downloads\Handycam Video Recovery\.test-work')
def block(lba):return lba.to_bytes(4,'big')*(SECTOR_BYTES//4)

class AdaptiveReadTests(unittest.TestCase):
    def test_real_bad_sector_exits_small_read_mode_and_reaches_later_footage(self):
        class Reader:
            max_read_sectors=32
            def sectors(self,lba,count):
                if any(1<=n<12 for n in range(lba,lba+count)):raise ReadError('damaged beginning')
                return b''.join(block(n) for n in range(lba,lba+count))
        events=[]
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            target=Path(folder)/'track.bin'
            report=DiscImager(Reader(),lambda level,event,**fields:events.append(event),None,threading.Event(),adaptive_reads=True).image({'start':0,'count':150},target)
            self.assertTrue(report['first_pass_complete']);self.assertIsNone(report['interrupted']);self.assertFalse(report['complete'])
            self.assertEqual(target.read_bytes()[64*SECTOR_BYTES:],b''.join(block(i) for i in range(64,150)))
            self.assertIn('fast_pass_restored_after_read_failure',events)
    def test_single_sector_only_reader_preserves_every_byte_without_skips(self):
        class Reader:
            max_read_sectors=32
            def sectors(self,lba,count):
                if count>1:raise ReadError('larger request rejected')
                return block(lba)
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            target=Path(folder)/'track.bin';events=[]
            imager=DiscImager(Reader(),lambda level,event,**fields:events.append(event),None,threading.Event(),adaptive_reads=True)
            report=imager.image({'start':100,'count':300},target)
            self.assertTrue(report['complete']);self.assertEqual(target.read_bytes(),b''.join(block(i) for i in range(100,400)))
            self.assertIn('adaptive_smaller_reads',events)
    def test_fatal_removal_is_not_retried_as_single_sector(self):
        class Reader:
            max_read_sectors=32
            calls=0
            def sectors(self,lba,count):self.calls+=1;raise DriveUnavailable('removed')
        reader=Reader()
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            report=DiscImager(reader,None,None,threading.Event(),adaptive_reads=True).image({'start':100,'count':300},Path(folder)/'track.bin')
            self.assertEqual(reader.calls,1);self.assertFalse(report['complete']);self.assertEqual(report['available_sectors'],0)

if __name__=='__main__':unittest.main()
