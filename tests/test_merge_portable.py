import unittest,tempfile,json
from pathlib import Path
from unittest.mock import patch
import studio_jobs
from studio_jobs import Job
from merge_images import merge_track_copies,SECTOR
from portable_camera import import_portable,script_command
ROOT=Path(r'D:\Downloads\Handycam Video Recovery\.test-work')
class CopyMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT);self.root=Path(self.temp.name);self.addCleanup(self.temp.cleanup)
        self.patcher=patch.object(studio_jobs,'DEFAULT_VIDEO_ROOT',self.root);self.patcher.start();self.addCleanup(self.patcher.stop)
    def track(self,name,bad=(),mutate=False,metadata=b'DVDVIDEO'+bytes(2040)):
        folder=self.root/name;folder.mkdir();path=folder/'track_02.bin';content=[]
        for i in range(12):
            block=(b'\x00\x00\x01\xba'+i.to_bytes(4,'big')+bytes(SECTOR-8))
            if mutate and i==8:block=b'x'*SECTOR
            content.append(bytes(SECTOR) if i in bad else block)
        path.write_bytes(b''.join(content));(folder/'track_02.bin.state.json').write_text(json.dumps({'start':0,'count':12,'available_sectors':12,'bad_sectors':list(bad),'track':2,'complete':not bad}))
        (folder/'track_01.bin').write_bytes(metadata);(folder/'track_01.bin.state.json').write_text(json.dumps({'start':20,'count':1,'available_sectors':1,'bad_sectors':[],'track':1,'complete':True}))
        return path
    def test_complementary_bad_ranges_merge_and_metadata_preserved(self):
        a=self.track('a',[2]);b=self.track('b',[3]);originals=(a.read_bytes(),b.read_bytes());job=Job('merge',self.root)
        job.run(lambda j:merge_track_copies([a,b],j,minimum_shared=8));self.assertTrue(job.report['complete']);self.assertEqual(job.report['remaining_bad_sectors'],0)
        self.assertEqual(len(job.report['preserved_metadata_tracks']),1);self.assertEqual((a.read_bytes(),b.read_bytes()),originals)
        self.assertNotEqual(Path(job.report['merged_image']).read_bytes()[2*SECTOR:3*SECTOR],bytes(SECTOR))
    def test_clean_mismatch_never_publishes_valid_image(self):
        a=self.track('a',[2]);b=self.track('b',[3],True);job=Job('merge',self.root)
        with self.assertRaises(ValueError):job.run(lambda j:merge_track_copies([a,b],j,minimum_shared=8))
        self.assertFalse((job.dest/'Original recovery'/'track_02.bin.state.json').exists());self.assertEqual(job.report['status'],'failed')
    def test_metadata_mismatch_never_publishes_valid_image(self):
        a=self.track('a',[2]);b=self.track('b',[3],metadata=b'DVDVIDEO'+b'x'*2040);job=Job('merge',self.root)
        with self.assertRaises(ValueError):job.run(lambda j:merge_track_copies([a,b],j,minimum_shared=8))
        self.assertFalse((job.dest/'Original recovery'/'track_02.bin').exists())
    def test_unrecoverable_sector_is_explicit_partial(self):
        a=self.track('a',[2]);b=self.track('b',[2]);job=Job('merge',self.root);job.run(lambda j:merge_track_copies([a,b],j,minimum_shared=8));self.assertEqual(job.report['status'],'partial');self.assertEqual(job.report['remaining_bad_sectors'],1)
    def test_portable_path_escape_and_size_mismatch_rejected(self):
        for escape in (True,False):
            job=Job('portable',self.root)
            def fake(*args,**kwargs):
                folder=job.dest/'Portable originals';folder.mkdir();target=self.root/'escape.jpg' if escape else folder/'file.jpg';target.write_bytes(b'abc')
                (folder/'portable_report.json').write_text(json.dumps({'files':[{'target':str(target),'bytes':4}],'failures':[]}));return {'returncode':0}
            with patch.object(job,'command',side_effect=fake),self.assertRaises((ValueError,OSError)):job.run(lambda j:import_portable('key',j))
    def test_portable_script_quotes_device_strings(self):
        args=script_command('Import',DeviceKey="camera';throw 'injected",Destination=self.root)
        self.assertIn("camera'';throw ''injected",args[-1]);self.assertNotIn('-ExecutionPolicy',args)
if __name__=='__main__':unittest.main()
