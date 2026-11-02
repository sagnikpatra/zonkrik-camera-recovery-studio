from pathlib import Path
TEST_WORK=Path(r'D:\Downloads\Handycam Video Recovery\.test-work')
TEST_WORK.mkdir(parents=True,exist_ok=True)
import sys,unittest,tempfile,threading,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import recovery as r

SENSE=bytes.fromhex('700002000000000a000000003a01000000000000000000000000000000000000')
class RecoveryTests(unittest.TestCase):
 def test_screenshot_no_medium(self):
  e=r.sense_error(SENSE);self.assertIsInstance(e,r.DiscUnavailable);self.assertIn('cannot detect',str(e));self.assertNotIn('700002',str(e));self.assertEqual((e.sense_key,e.asc,e.ascq),(2,0x3a,1))
 def test_descriptor_and_tray_open(self):
  self.assertIsInstance(r.sense_error(bytes.fromhex('72023a0100000000')),r.DiscUnavailable)
  sense=bytearray(SENSE);sense[13]=2;self.assertIn('tray is open',str(r.sense_error(sense)))
 def test_genuine_medium_error_is_sector_error(self):
  sense=bytearray(SENSE);sense[2]=3;sense[12]=0x11;sense[13]=0
  self.assertIsInstance(r.sense_error(sense),r.ReadError)
 def test_disc_change_is_not_sector_damage(self):
  sense=bytearray(SENSE);sense[2]=6;sense[12]=0x28;sense[13]=0
  e=r.sense_error(sense);self.assertIsInstance(e,r.DiscChanged);self.assertNotIsInstance(e,r.ReadError)
 def test_loading_wait_then_ready(self):
  d=r.Disc.__new__(r.Disc);d.h=None;events=[]
  with patch.object(d,'command',side_effect=[r.DiscUnavailable('empty'),r.DiscNotReady('loading'),r.DiscChanged('changed'),b'']) as command:
   d.wait_ready(lambda s,p:events.append(s),threading.Event(),timeout=1,interval=.001)
  self.assertEqual(command.call_count,4);self.assertEqual(len(events),3)
 def test_no_medium_timeout(self):
  d=r.Disc.__new__(r.Disc);d.h=None
  with patch.object(d,'command',side_effect=r.DiscUnavailable('No readable disc')):
   with self.assertRaises(r.DiscUnavailable):d.wait_ready(timeout=0)
 def test_cancel_wait(self):
  d=r.Disc.__new__(r.Disc);d.h=None;cancel=threading.Event();cancel.set()
  with patch.object(d,'command') as command:
   with self.assertRaises(r.Cancelled):d.wait_ready(cancel=cancel)
   command.assert_not_called()
 def test_write_guard(self):
  d=r.Disc.__new__(r.Disc);d.h=None
  for opcode in (0x2a,0x04,0x5b,0x53,0x1b):
   with self.assertRaises(r.RecoveryError):d.command([opcode]+[0]*9,2048)
 def test_removed_disc_does_not_fill_zeros(self):
  with tempfile.TemporaryDirectory(dir=TEST_WORK) as folder:
   path=Path(folder).resolve();engine=r.Recovery('F:',path,lambda *a:None,threading.Event())
   class Removed:
    def sectors(self,start,count):
     if start==0:return b'A'*(count*2048)
     raise r.DiscUnavailable('Disc removed')
   with self.assertRaises(r.DiscUnavailable):engine.dump(Removed(),0,128,path/'original.mpg')
   self.assertEqual((path/'original.mpg.partial').stat().st_size,64*2048);self.assertEqual(engine.bad,[])
 def test_single_bad_sector_recorded(self):
  with tempfile.TemporaryDirectory(dir=TEST_WORK) as folder:
   path=Path(folder).resolve();engine=r.Recovery('F:',path,lambda *a:None,threading.Event())
   class Damaged:
    def sectors(self,start,count):
     if count>1 or start==7:raise r.ReadError('Damaged sector')
     return b'A'*2048
   engine.dump(Damaged(),0,10,path/'original.mpg')
   self.assertEqual(engine.bad,[7]);self.assertEqual((path/'original.mpg').read_bytes()[7*2048:8*2048],bytes(2048))
 def test_no_ready_disc_creates_no_recovery_folder(self):
  with tempfile.TemporaryDirectory(dir=TEST_WORK) as folder:
   dest=Path(folder).resolve()/'not-created';engine=r.Recovery('F:',dest,lambda *a:None,threading.Event())
   class Missing:
    def __init__(self,drive):pass
    def wait_ready(self,*a):raise r.DiscUnavailable('no disc')
    def close(self):pass
   with patch.object(r,'Disc',Missing):
    with self.assertRaises(r.DiscUnavailable):engine.run()
   self.assertFalse(dest.exists())
 def test_original_metadata_still_has_24_clips(self):
  meta=r.parse_ifo((Path(__file__).resolve().parent/'fixtures'/'original_disc_metadata.bin').read_bytes());self.assertEqual(len(meta['clips']),24)


import sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import recovery as r
class RawDiscoveryTests(unittest.TestCase):
 def test_no_letter_detects_raw_writer(self):
  with patch.object(r.k,'GetDriveTypeW',return_value=0),patch.object(r,'raw_optical_names',return_value=['CdRom0']),patch.object(r,'dos_device_target',return_value='\\Device\\CdRom0'):
   self.assertEqual(r.optical_drives(),['CdRom0'])
 def test_letter_and_raw_same_writer_appear_once(self):
  with patch.object(r.k,'GetDriveTypeW',side_effect=lambda path:5 if path=='F:\\' else 0),patch.object(r,'raw_optical_names',return_value=['CdRom0']),patch.object(r,'dos_device_target',return_value='\\Device\\CdRom0'):
   self.assertEqual(r.optical_drives(),['F:'])
 def test_two_different_writers_preserved(self):
  def target(name):return '\\Device\\CdRom0' if name in ('F:','CdRom0') else '\\Device\\CdRom1'
  with patch.object(r.k,'GetDriveTypeW',side_effect=lambda path:5 if path=='F:\\' else 0),patch.object(r,'raw_optical_names',return_value=['CdRom0','CdRom1']),patch.object(r,'dos_device_target',side_effect=target):
   self.assertEqual(r.optical_drives(),['F:','CdRom1'])
 def test_raw_name_is_never_a_relative_filesystem_path(self):
  self.assertIsNone(r.video_directory('CdRom0'));self.assertEqual(r.video_directory('F:'),Path('F:\\VIDEO_TS'))
 def test_non_optical_device_cannot_be_opened(self):
  with self.assertRaises(r.DriveUnavailable):r.Disc('PhysicalDrive0')

import sys,unittest,ctypes,struct
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import recovery as r
class TransferLimitTests(unittest.TestCase):
 def test_read_split_at_adapter_limit(self):
  d=r.Disc.__new__(r.Disc);d.h=None;d.max_read_sectors=32;seen=[]
  def command(cdb,size):seen.append((int.from_bytes(bytes(cdb[2:6]),'big'),int.from_bytes(bytes(cdb[7:9]),'big')));return b'A'*size
  with patch.object(d,'command',side_effect=command):self.assertEqual(len(d.sectors(100,70)),70*2048)
  self.assertEqual(seen,[(100,32),(132,32),(164,6)])
 def test_smaller_transfer_fallback(self):
  d=r.Disc.__new__(r.Disc);d.h=None;d.max_read_sectors=64;seen=[]
  def command(cdb,size):
   seen.append(size)
   if size>32768:raise r.TransferSizeError('too large')
   return b'A'*size
  with patch.object(d,'command',side_effect=command):self.assertEqual(len(d.sectors(100,64)),64*2048)
  self.assertEqual(d.max_read_sectors,16);self.assertEqual(seen[:3],[131072,65536,32768])
 def test_unfinished_disc_filesystem_error_falls_back(self):
  with patch.object(Path,'is_dir',side_effect=OSError(1,'Incorrect function')):
   self.assertIsNone(r.mounted_video_directory('F:'))
 def test_query_adapter_limits(self):
  d=r.Disc.__new__(r.Disc);d.h=None;d.max_read_sectors=32;d.alignment_mask=4095
  def ioctl(handle,code,query,qlen,output,olen,returned,overlapped):
   data=struct.pack('<IIIII',32,32,65536,17,0);ctypes.memmove(output,data,len(data));returned._obj.value=32;return 1
  with patch.object(r.k,'DeviceIoControl',side_effect=ioctl):d.query_transfer_limits()
  self.assertEqual(d.max_read_sectors,32);self.assertEqual(d.alignment_mask,4095)
if __name__=='__main__':unittest.main(verbosity=2)
