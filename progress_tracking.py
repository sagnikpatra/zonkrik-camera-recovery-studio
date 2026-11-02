"""Measured elapsed/remaining estimates; stalls and phase changes invalidate ETA."""
from collections import deque
import math
import time

def clock_text(seconds):
    if seconds is None:return 'estimating…'
    seconds=max(0,int(round(seconds)))
    return f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}'

class ProgressTracker:
    def __init__(self,clock=time.monotonic):
        self.clock=clock;self.reset()
    def reset(self):
        self.started=None;self.finished=None;self.completed=False;self.percent=0;self.samples=deque(maxlen=90)
        self.phase='';self.phase_samples=deque(maxlen=90);self.phase_total=None;self.last_motion=None;self.phase_motion=None;self.phase_done=None
    def start(self):
        self.reset();self.started=self.clock();self.last_motion=self.started
    def observe(self,message,value):
        if self.started is None:self.start()
        now=self.clock();data=value if isinstance(value,dict) else {}
        percent=data.get('overall',self.percent if data else value)
        try:percent=min(100,max(0,float(percent)))
        except (TypeError,ValueError):percent=self.percent
        if not math.isfinite(percent):percent=self.percent
        if percent<self.percent-1:self.samples.clear()
        if percent>self.percent:self.last_motion=now
        self.percent=percent
        if not self.samples or now-self.samples[-1][0]>=.5 or percent>=100:self.samples.append((now,percent))
        phase=data.get('phase')
        if phase and phase!=self.phase:
            self.phase=phase;self.phase_samples.clear();self.phase_total=data.get('total');self.phase_motion=now;self.phase_done=None
        if phase and data.get('done') is not None:
            if self.phase_done is None or float(data['done'])>self.phase_done:self.phase_motion=now
            self.phase_done=float(data['done'])
            if not self.phase_samples or now-self.phase_samples[-1][0]>=.5:self.phase_samples.append((now,float(data['done'])))
            self.phase_total=data.get('total')
        while len(self.samples)>2 and now-self.samples[0][0]>45:self.samples.popleft()
        while len(self.phase_samples)>2 and now-self.phase_samples[0][0]>45:self.phase_samples.popleft()
    @staticmethod
    def estimate(samples,total,now):
        if total is None or len(samples)<2:return None
        a,b=samples[0],samples[-1];elapsed=b[0]-a[0];advance=b[1]-a[1]
        if elapsed<3 or advance<=0 or now-b[0]>12:return None
        remaining=max(0,float(total)-b[1])*elapsed/advance
        return remaining if math.isfinite(remaining) else None
    def finish(self,completed=True):self.finished=self.clock();self.completed=completed
    def snapshot(self):
        if self.started is None:return {'elapsed':0,'remaining':None,'phase_remaining':None,'percent':0,'active':False}
        now=self.finished if self.finished is not None else self.clock()
        remaining=self.estimate(self.samples,100,now)
        if self.last_motion is not None and now-self.last_motion>15:remaining=None
        return {'elapsed':max(0,now-self.started),'remaining':(0 if self.completed else None) if self.finished is not None else remaining,
                'phase_remaining':self.estimate(self.phase_samples,self.phase_total,now) if self.phase_motion is not None and now-self.phase_motion<=15 else None,'percent':self.percent,'active':self.finished is None}
    def text(self):
        data=self.snapshot()
        text=f"Elapsed {clock_text(data['elapsed'])}   •   Approx. remaining {clock_text(data['remaining'])}"
        if data['phase_remaining'] is not None and data['active']:text+=f"   •   Current step ~{clock_text(data['phase_remaining'])}"
        return text
