"""Non-blocking, local Windows result sounds. Audio failure never fails a job."""
from pathlib import Path
import winsound

class ResultSounds:
    def __init__(self,base,enabled=lambda:True,logger=None):
        self.base=Path(base);self.enabled=enabled;self.logger=logger
    def play(self,result):
        if result not in ('success','partial','failure','stopped'):raise ValueError('Unknown notification result.')
        if not self.enabled():return False
        name='partial' if result=='stopped' else result
        try:
            path=self.base/'sounds'/(name+'.wav')
            if not path.is_file():raise FileNotFoundError(f'Notification sound missing: {path}')
            winsound.PlaySound(str(path),winsound.SND_FILENAME|winsound.SND_ASYNC|winsound.SND_NODEFAULT)
            if self.logger:self.logger('INFO','result_sound_played',result=result)
            return True
        except Exception as error:
            if self.logger:self.logger.exception('result_sound',error,result=result)
            return False
