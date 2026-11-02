import unittest,tempfile
from pathlib import Path
from unittest.mock import patch,Mock
from sound_notifications import ResultSounds
import winsound
BASE=Path(__file__).resolve().parents[1]
class SoundTests(unittest.TestCase):
    def test_success_partial_failure_use_distinct_local_async_files(self):
        with patch('sound_notifications.winsound.PlaySound') as play:
            sounds=ResultSounds(BASE)
            for status in ('success','partial','failure'):self.assertTrue(sounds.play(status))
            paths=[item.args[0] for item in play.call_args_list];self.assertEqual(len(set(paths)),3)
            self.assertTrue(all(Path(p).is_file() for p in paths));self.assertTrue(all(item.args[1]&winsound.SND_ASYNC for item in play.call_args_list))
    def test_mute_does_not_play(self):
        with patch('sound_notifications.winsound.PlaySound') as play:self.assertFalse(ResultSounds(BASE,lambda:False).play('failure'));play.assert_not_called()
    def test_audio_error_is_logged_without_failing_job(self):
        logger=Mock()
        with patch('sound_notifications.winsound.PlaySound',side_effect=RuntimeError('audio unavailable')):
            self.assertFalse(ResultSounds(BASE,logger=logger).play('success'));logger.exception.assert_called_once()
    def test_stop_uses_caution_not_success(self):
        with patch('sound_notifications.winsound.PlaySound') as play:
            ResultSounds(BASE).play('stopped');self.assertEqual(Path(play.call_args.args[0]).name,'partial.wav')
    def test_missing_asset_does_not_play_default_windows_sound(self):
        with patch('sound_notifications.winsound.PlaySound') as play:
            self.assertFalse(ResultSounds(BASE/'missing').play('failure'));play.assert_not_called()
if __name__=='__main__':unittest.main()
