import sys,unittest,tempfile,threading
from pathlib import Path
from unittest.mock import patch,Mock
from recovery_engine import RecoveryEngine,inspect_layout
ROOT=Path(r'D:\Downloads\Handycam Video Recovery\.test-work');ROOT.mkdir(exist_ok=True)
class LaterTrackTests(unittest.TestCase):
    def test_timeout_reaches_later_video_but_removal_does_not(self):
        for reason,expected in [('NoReadProgress',2),('RepairBudgetExceeded',2),('DriveUnavailable',1),('DiscChanged',1)]:
            with self.subTest(reason=reason),tempfile.TemporaryDirectory(dir=ROOT) as folder:
                disc=Mock();disc.max_read_sectors=32;disc.tracks.return_value=[{'track':2,'start':12288,'count':176},{'track':3,'start':12480,'count':10000}]
                engine=RecoveryEngine('F:',Path(folder)/'result');calls=[]
                def image(track,target,**kwargs):
                    calls.append(track['track']);target.write_bytes(bytes(2048));return {'path':str(target),'available_sectors':1,'complete':False,'bad_sectors':[],'interrupted':{'type':reason} if track['track']==2 else None}
                with patch.object(engine,'open_disc',return_value=disc),patch.object(engine,'_verified_previous',return_value=(None,[])),patch('disc_imaging.DiscImager.image',side_effect=image):engine.image_disc()
                self.assertEqual(len(calls),expected);self.assertFalse(engine.report['complete'])
    def test_detect_reports_readable_video_despite_bad_metadata(self):
        import recovery
        disc=Mock();disc.max_read_sectors=32;disc.tracks.return_value=[{'track':2,'start':12288,'count':176},{'track':3,'start':12480,'count':10000}]
        disc.sectors.side_effect=lambda start,count: (_ for _ in ()).throw(recovery.ReadError('bad metadata')) if start==12288 else b'\x00\x00\x01\xba'+bytes(2044)
        log=Mock()
        with patch('recovery_engine.low.Disc',return_value=disc):result=inspect_layout('CdRom0',lambda *a:None,threading.Event(),log)
        self.assertEqual(result['readable_raw_video_tracks'],[3]);self.assertEqual(result['recordings'],0);self.assertTrue(result['issues'])
if __name__=='__main__':unittest.main()

