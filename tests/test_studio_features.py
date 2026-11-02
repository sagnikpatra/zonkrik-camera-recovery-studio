from pathlib import Path
import hashlib,json,subprocess,sys,tempfile,threading,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from progress_tracking import ProgressTracker,clock_text
import studio_jobs
from studio_jobs import Job,BASE
from camera_sources import import_camera,image_card,scan_files
from recovery_tools import capture_tape,recover_deleted,repair_video
from device_diagnostics import validate_driver_package
from dvd_vr import parse_manager
from recovery import Cancelled

WORK=Path(r'D:\Downloads\Handycam Video Recovery\.test-work\v3-tests');WORK.mkdir(parents=True,exist_ok=True)
class ProgressTests(unittest.TestCase):
    def test_measured_eta_and_stall_recalibration(self):
        now=[0.0];tracker=ProgressTracker(lambda:now[0]);tracker.start();tracker.observe('',0)
        now[0]=10;tracker.observe('',20);self.assertAlmostEqual(tracker.snapshot()['remaining'],40)
        now[0]=30;self.assertIsNone(tracker.snapshot()['remaining'])
        tracker.finish(False);self.assertIsNone(tracker.snapshot()['remaining']);self.assertEqual(tracker.snapshot()['elapsed'],30)
    def test_fast_sampling_still_estimates_and_phase_reset_is_unknown(self):
        now=[0.0];tracker=ProgressTracker(lambda:now[0]);tracker.start()
        for index in range(1000):
            now[0]=index*.01;tracker.observe('',{'overall':index/20,'phase':'copy','done':index,'total':2000})
        self.assertGreater(tracker.snapshot()['remaining'],0);self.assertGreater(tracker.snapshot()['phase_remaining'],0)
        tracker.observe('',{'overall':50,'phase':'verify','done':None,'total':None});self.assertIsNone(tracker.snapshot()['phase_remaining'])
    def test_invalid_progress_no_nan_and_completed_timer_stops(self):
        now=[5.0];tracker=ProgressTracker(lambda:now[0]);tracker.start();tracker.observe('',float('nan'));now[0]=9;tracker.finish()
        now[0]=99;self.assertEqual(tracker.snapshot()['elapsed'],4);self.assertEqual(tracker.snapshot()['remaining'],0);self.assertEqual(clock_text(3661),'01:01:01')

class LocalJobs(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=WORK);self.root=Path(self.temp.name).resolve();self.assertTrue(self.root.is_relative_to(WORK.resolve()));self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall);patch.object(studio_jobs,'DEFAULT_VIDEO_ROOT',self.root).start()
    def job(self):return Job('test',self.root)
    def test_original_photos_raw_metadata_and_video_structure_preserved(self):
        source=self.root/'source';(source/'DCIM').mkdir(parents=True);(source/'PRIVATE').mkdir()
        payloads={'DCIM/a.jpg':b'jpeg bytes','DCIM/b.ARW':b'raw bytes','PRIVATE/c.MTS':b'video bytes','PRIVATE/info.xml':b'metadata'}
        for name,data in payloads.items():(source/name).write_bytes(data)
        before={name:(source/name).stat().st_mtime_ns for name in payloads};job=self.job();report=job.run(lambda j:import_camera(source,j,convert=False))
        self.assertTrue(report['complete']);self.assertEqual(report['verified_files'],4);self.assertEqual(report['photo_originals'],2)
        for name,data in payloads.items():
            self.assertEqual((job.dest/'Camera originals'/name).read_bytes(),data);self.assertEqual((source/name).stat().st_mtime_ns,before[name])
        self.assertTrue(all(i.get('sha256') for i in report['files']))
    def test_destination_inside_source_rejected_without_recursive_import(self):
        job=self.job()
        with self.assertRaises(ValueError):scan_files(self.root,job)
    def test_single_file_source_supported(self):
        source=self.root/'one.jpg';source.write_bytes(b'photo');job=self.job();report=job.run(lambda j:import_camera(source,j,convert=False))
        self.assertEqual(report['verified_files'],1)
    def test_cancel_preserves_report_and_source(self):
        source=self.root/'one.jpg';source.write_bytes(b'photo');job=self.job();job.cancel.set()
        with self.assertRaises(Cancelled):job.run(lambda j:import_camera(source,j,convert=False))
        self.assertEqual(json.loads((job.dest/'recovery_report.json').read_text())['status'],'stopped');self.assertEqual(source.read_bytes(),b'photo')
    def test_pc_and_output_drives_never_opened_for_card_imaging(self):
        job=self.job()
        with patch('camera_sources.removable_cards',return_value=['G:']):
            for drive in ('C:','D:','E:','\\\\.\\PhysicalDrive0'):
                with self.assertRaises(ValueError):image_card(drive,job)
    def test_driver_wrong_id_and_absent_catalog_rejected(self):
        inf=self.root/'test.inf';inf.write_text('[Version]\nCatalogFile=test.cat\n[Models]\nx=Install,USB\\VID_1234&PID_5678\n')
        with self.assertRaises(ValueError):validate_driver_package(inf,['USB\\VID_9999&PID_0000'])
        with self.assertRaises(ValueError):validate_driver_package(inf,['USB\\VID_1234&PID_5678'])
        (self.root/'test.cat').write_bytes(b'not signed');_,catalogs,matches=validate_driver_package(inf,['USB\\VID_1234&PID_5678']);self.assertEqual(len(catalogs),1);self.assertEqual(len(matches),1)
    def test_capture_arguments_preserve_stream_and_do_not_start_a_real_device(self):
        job=self.job();commands=[]
        def command(args,*a,**kw):commands.append((args,kw));Path(args[-1]).write_bytes(b'capture fixture');return {'returncode':0,'stderr':'log'}
        with patch.object(job,'command',side_effect=command):capture_tape('DV camera','', 'Preserve DV/HDV stream',2,'Device default',job)
        args,kwargs=commands[0];self.assertIn('copy',args);self.assertIn('120.0',args);self.assertTrue(kwargs['graceful']);self.assertEqual(job.report['capture_mode'],'Preserve DV/HDV stream')
    def test_capture_rejects_invalid_name_or_standard(self):
        job=self.job()
        with patch.object(job,'command') as command:
            for video,standard in [('', 'Device default'),('bad\nname','Device default'),('camera','unsupported')]:
                with self.assertRaises(ValueError):capture_tape(video,'','Lossless archive (FFV1 + PCM)',1,standard,job)
            command.assert_not_called()
    def test_photorec_zero_exit_syntax_error_is_failure(self):
        image=self.root/'x.img';image.write_bytes(b'raw');job=self.job()
        with patch('recovery_tools.helper',return_value=Path('helper.exe')),patch.object(job,'command',return_value={'returncode':0,'stdout_text':'','stderr_text':'PhotoRec syntax error','stderr':'stderr.log'}):
            with self.assertRaises(RuntimeError):recover_deleted(image,job)
    def test_unknown_candidate_family_and_source_image_are_reported_without_false_decode(self):
        image=self.root/'x.img';image.write_bytes(b'raw');job=self.job()
        def command(*args,**kw):
            directory=job.dest/'Recovered candidates.1';directory.mkdir();(directory/'file.jpg').write_bytes(b'data');(directory/'report.xml').write_bytes(b'report')
            return {'returncode':0,'stdout_text':'','stderr_text':'','stderr':'stderr.log'}
        with patch('recovery_tools.helper',return_value=Path('helper.exe')),patch.object(job,'command',side_effect=command):recover_deleted(image,job)
        self.assertEqual(job.report['recovered_candidates'],1);self.assertEqual(image.read_bytes(),b'raw');self.assertIn('candidates',job.report['validation'])

def vr_fixture():
    data=bytearray(2048);data[:12]=b'DVD_RTR_VMG\0';data[256:260]=(512).to_bytes(4,'big');data[514]=1;data[515]=0;data[516:520]=(1535).to_bytes(4,'big');data[520:522]=(2).to_bytes(2,'big')
    for index,record in enumerate((560,640)):
        data[522+index*4:526+index*4]=(record-512).to_bytes(4,'big');mapping=record+23;data[mapping+2:mapping+4]=(2).to_bytes(2,'big');data[mapping+6:mapping+10]=(index*10).to_bytes(4,'big')
        entries=mapping+10;data[entries+1:entries+3]=(4).to_bytes(2,'big');data[entries+4:entries+6]=(6).to_bytes(2,'big')
    return data
class VRTests(unittest.TestCase):
    def test_every_physical_program_and_sector_extent(self):
        parsed=parse_manager(vr_fixture());self.assertEqual(len(parsed['programs']),2);self.assertEqual([(p['first_sector'],p['last_sector']) for p in parsed['programs']],[(0,9),(10,19)]);self.assertFalse(parsed['complete'])
    def test_truncation_and_overlaps_do_not_hide_missing_programs(self):
        data=vr_fixture()
        with self.assertRaises(ValueError):parse_manager(data[:600])
        data[640+23+6:640+23+10]=(5).to_bytes(4,'big')
        with self.assertRaises(ValueError):parse_manager(data)
    def test_empty_manager_is_not_claimed_to_be_recovered_video(self):
        data=vr_fixture();data[514]=0;self.assertEqual(parse_manager(data)['programs'],[])
    def test_corrupt_offsets_fail_bounded(self):
        for position in (256,516,522):
            data=vr_fixture();data[position:position+4]=b'\xff'*4
            with self.assertRaises(ValueError):parse_manager(data)

if __name__=='__main__':unittest.main()
