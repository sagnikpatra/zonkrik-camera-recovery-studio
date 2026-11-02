import os,sys,queue,threading,datetime,traceback,json,tkinter as tk
from tkinter import ttk,filedialog,messagebox
from pathlib import Path
from recovery import Disc,optical_drives,probe_disc,Cancelled,DiscUnavailable,DriveUnavailable,DiscChanged
from recovery_engine import RecoveryEngine,inspect_layout
from recovery_logging import RunLogger,application_logger,install_exception_hooks
from progress_tracking import ProgressTracker
from studio_ui import StudioPanels
from sound_notifications import ResultSounds

VERSION='3.0.5'
BASE=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
DEFAULT=Path(r'D:\Downloads\Handycam Video Recovery')

class App:
    def __init__(self,root):
        self.root=root;root.title(f'ZONKRIK Camera Recovery Studio {VERSION}');root.geometry(f"{min(1160,root.winfo_screenwidth()-60)}x{min(940,root.winfo_screenheight()-90)}");root.minsize(900,700);root.configure(bg='#101823')
        self.events=queue.Queue();self.cancel=threading.Event();self.busy=False;self.probing=False;self.latest=None;self.closed=False
        self.last_probe=None
        self.timer=ProgressTracker();self.timing=tk.StringVar(value=self.timer.text())
        self.app_log=application_logger();install_exception_hooks(self.app_log);self.last_log_directory=self.app_log.directory
        root.report_callback_exception=self.callback_exception
        try:self.settings=json.loads((BASE/'settings.json').read_text(encoding='utf-8'))
        except (OSError,ValueError) as error:
            self.settings={};self.app_log.exception('read_settings',error,path=str(BASE/'settings.json'))
        self.sound_enabled=tk.BooleanVar(value=self.settings.get('sound_enabled',True))
        self.sounds=ResultSounds(BASE,lambda:self.sound_enabled.get(),self.app_log)
        if self.settings.get('latest') and Path(self.settings['latest']).is_dir():
            self.latest=Path(self.settings['latest'])
            try:
                prior=json.loads((self.latest/'recovery_report.json').read_text(encoding='utf-8'))
                prior_logs=Path(prior.get('log_directory',''))
                if prior.get('log_directory') and prior_logs.is_dir():self.last_log_directory=prior_logs
            except (OSError,ValueError) as error:self.app_log.exception('read_previous_recovery_report',error,path=str(self.latest))
        elif (BASE.parent/'Handycam_all_videos.mp4').is_file():self.latest=BASE.parent
        if self.settings.get('last_log_directory') and Path(self.settings['last_log_directory']).is_dir():self.last_log_directory=Path(self.settings['last_log_directory'])
        style=ttk.Style();style.theme_use('clam')
        style.configure('TFrame',background='#101823');style.configure('TLabel',background='#101823',foreground='#dce6f2',font=('Segoe UI',11));style.configure('TButton',font=('Segoe UI',11),padding=9)
        style.configure('Accent.TButton',background='#52c8b0',foreground='#10232a',font=('Segoe UI Semibold',12),padding=12)
        style.configure('TProgressbar',background='#52c8b0',troughcolor='#253444');style.configure('TCombobox',padding=6)
        style.configure('Failure.Horizontal.TProgressbar',background='#e86c65',troughcolor='#253444')
        footer=ttk.Frame(root,padding=(20,8));footer.pack(side='bottom',fill='x')
        self.notebook=ttk.Notebook(root);self.notebook.pack(fill='both',expand=True,padx=8,pady=(8,0))
        body=ttk.Frame(self.notebook,padding=22);self.notebook.add(body,text='DVD / saved disc')
        ttk.Label(body,text=f'ZONKRIK  /  HANDYCAM RECOVERY {VERSION}',font=('Segoe UI Semibold',12),foreground='#52c8b0').pack(anchor='w')
        ttk.Label(body,text='Bring your Handycam videos back',font=('Segoe UI Semibold',23)).pack(anchor='w',pady=(8,7))
        ttk.Label(body,text='Recover all recorded tracks and check every clip. Incomplete results are clearly marked.',wraplength=740).pack(anchor='w')
        row=ttk.Frame(body);row.pack(fill='x',pady=(24,10));ttk.Label(row,text='DVD writer').pack(side='left',padx=(0,12))
        self.drive=tk.StringVar();self.combo=ttk.Combobox(row,textvariable=self.drive,width=13,state='readonly');self.combo.pack(side='left')
        self.combo.bind('<<ComboboxSelected>>',lambda event:self.watch_drives())
        self.refresh_button=ttk.Button(row,text='Refresh',command=self.refresh);self.refresh_button.pack(side='left',padx=8)
        self.inspect_button=ttk.Button(row,text='Detect Disc',command=self.inspect);self.inspect_button.pack(side='left')
        self.device_status=tk.StringVar(value='Checking the DVD writer and disc…')
        self.device_label=ttk.Label(body,textvariable=self.device_status,font=('Segoe UI Semibold',11),foreground='#e7b96b',wraplength=740)
        self.device_label.pack(anchor='w',pady=(0,12))
        ttk.Label(body,text='Save recovered videos to').pack(anchor='w',pady=(4,6))
        row=ttk.Frame(body);row.pack(fill='x');self.folder=tk.StringVar(value=self.settings.get('folder',str(DEFAULT)))
        self.entry=ttk.Entry(row,textvariable=self.folder,font=('Segoe UI',10));self.entry.pack(side='left',fill='x',expand=True,ipady=8)
        self.browse_button=ttk.Button(row,text='Choose folder',command=self.browse);self.browse_button.pack(side='left',padx=(8,0))
        self.message=tk.StringVar(value='Insert your mini DVD and let it finish loading. You can then inspect or recover it.')
        ttk.Label(body,textvariable=self.message,wraplength=740).pack(anchor='w',pady=(16,10))
        self.progress=ttk.Progressbar(body,maximum=100);self.progress.pack(fill='x')
        row=ttk.Frame(body);row.pack(fill='x',pady=14)
        self.go=ttk.Button(row,text='Recover Videos',style='Accent.TButton',command=self.start);self.go.pack(side='left')
        self.stop=ttk.Button(row,text='Stop',command=self.stop_operation,state='disabled');self.stop.pack(side='left',padx=8)
        self.logs_button=ttk.Button(row,text='Open logs',command=self.open_logs);self.logs_button.pack(side='left',padx=4)
        self.open=ttk.Button(row,text='Open saved videos',command=self.open_folder);self.open.pack(side='right')
        self.saved_button=ttk.Button(body,text='Recover from a saved / interrupted copy',command=self.start_saved);self.saved_button.pack(anchor='w',pady=(0,8))
        self.log=tk.Text(body,height=5,bg='#192534',fg='#dce6f2',font=('Segoe UI',10),relief='flat',wrap='word',state='disabled');self.log.pack(fill='both',expand=True)
        ttk.Label(body,text='Local and free  •  No formatting or finalization  •  Each recovery gets a new folder',font=('Segoe UI',9),foreground='#95a8bc').pack(anchor='w',pady=(10,0))
        ttk.Label(footer,textvariable=self.message,wraplength=1050).pack(anchor='w')
        ttk.Label(footer,textvariable=self.timing,font=('Segoe UI Semibold',10),foreground='#52c8b0').pack(anchor='w',pady=5)
        self.global_progress=ttk.Progressbar(footer,maximum=100);self.global_progress.pack(fill='x')
        row=ttk.Frame(footer);row.pack(fill='x',pady=(5,0))
        self.footer_stop=ttk.Button(row,text='Stop current task',command=self.stop_operation,state='disabled');self.footer_stop.pack(side='left')
        ttk.Button(row,text='Open logs',command=self.open_logs).pack(side='left',padx=8)
        ttk.Button(row,text='Open results',command=self.open_folder).pack(side='left')
        ttk.Label(row,textvariable=self.folder,font=('Segoe UI',9),foreground='#95a8bc').pack(side='right')
        sound_row=ttk.Frame(footer);sound_row.pack(fill='x',pady=(4,0))
        ttk.Checkbutton(sound_row,text='Result sounds',variable=self.sound_enabled,command=self.store_settings).pack(side='left')
        for result,label in [('success','Test success'),('partial','Test partial / stopped'),('failure','Test failure')]:
            ttk.Button(sound_row,text=label,command=lambda result=result:self.sounds.play(result)).pack(side='left',padx=4)
        self.studio=StudioPanels(self,self.notebook)
        self.refresh();root.protocol('WM_DELETE_WINDOW',self.close);root.after(100,self.poll);root.after(200,self.watch_drives);root.after(500,self.tick_timing)
    def tick_timing(self):
        if self.closed:return
        self.timing.set(self.timer.text());self.root.after(500,self.tick_timing)
    def refresh(self):
        values=optical_drives();self.combo['values']=values
        if self.drive.get() not in values:self.drive.set(values[0] if values else '')
        if not values:
            self.device_status.set('DVD writer disconnected');self.device_label.configure(foreground='#e7b96b')
            self.message.set('Connect the writer directly to a USB port on the PC. The app checks automatically.')
        self.go.configure(state='normal' if values and not self.busy else 'disabled')
        self.inspect_button.configure(state='normal' if values and not self.busy else 'disabled')
    def watch_drives(self):
        if self.closed:return
        if not self.busy and not self.probing:
            values=optical_drives()
            if tuple(values)!=tuple(self.combo['values']):self.refresh()
            drive=self.drive.get()
            if drive:
                self.probing=True
                def work():
                    try:result=probe_disc(drive)
                    except Exception as error:
                        self.app_log.exception('automatic_drive_check',error,drive=drive);result={'state':'attention','message':'Could not check the writer. Details are saved in Logs.'}
                    self.events.put(('probe',(drive,result)))
                threading.Thread(target=work,daemon=True).start()
        self.root.after(3000,self.watch_drives)
    def browse(self):
        selected=filedialog.askdirectory(initialdir=str(DEFAULT if DEFAULT.exists() else Path('D:/')))
        if selected:self.folder.set(selected)
    def store_settings(self):
        data={'folder':self.folder.get(),'latest':str(self.latest) if self.latest else None,'last_log_directory':str(self.last_log_directory),'sound_enabled':self.sound_enabled.get()}
        try:(BASE/'settings.json').write_text(json.dumps(data,indent=2),encoding='utf-8')
        except OSError as error:self.app_log.exception('save_settings',error,path=str(BASE/'settings.json'))
    def set_busy(self,value):
        if value:
            self.progress.configure(style='Horizontal.TProgressbar');self.global_progress.configure(style='Horizontal.TProgressbar')
        if value and not self.busy:self.timer.start();self.progress['value']=0;self.global_progress['value']=0
        if not value and self.busy:self.timer.finish()
        self.busy=value
        for widget in [self.go,self.refresh_button,self.inspect_button,self.browse_button,self.entry,self.saved_button]:widget.configure(state='disabled' if value else 'normal')
        self.combo.configure(state='disabled' if value else 'readonly');self.stop.configure(state='normal' if value else 'disabled')
        self.footer_stop.configure(state='normal' if value else 'disabled')
        if hasattr(self,'studio'):self.studio.set_busy(value)
        if not value:self.refresh()
    def stop_operation(self):
        self.cancel.set();self.stop.configure(state='disabled');self.footer_stop.configure(state='disabled');self.message.set('Stopping safely… Saved copies will remain on D:.')
    def append(self,s):
        self.log.configure(state='normal');self.log.insert('end',s+'\n');self.log.see('end');self.log.configure(state='disabled')
    def callback_exception(self,kind,value,tb):
        self.app_log.exception('gui_callback',value.with_traceback(tb))
        self.message.set('An unexpected error occurred. Click Open logs for full details.')
        self.sounds.play('failure')
    def open_logs(self):
        self.last_log_directory.mkdir(parents=True,exist_ok=True);os.startfile(self.last_log_directory)
    def inspect(self):
        if self.busy:return
        if self.probing:self.root.after(200,self.inspect);return
        drive=self.drive.get()
        if not drive:self.message.set('Connect the DVD writer first.');return
        self.set_busy(True);self.cancel.clear();self.message.set('Inspecting every recorded track and title set…')
        log=RunLogger(DEFAULT/'Logs'/('inspect_'+datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f')),drive=drive)
        self.last_log_directory=log.directory
        def work():
            try:
                info=inspect_layout(drive,lambda text,p:self.events.put(('progress',(text,p))),self.cancel,log)
                text=f"Detected {len(info['tracks'])} recorded tracks, {info['title_sets']} title sets and {info['recordings']} indexed recordings."
                if info.get('data_video_files'):text=f"Detected {info['data_video_files']} video files on a data disc. Each file will be recovered separately."
                elif info.get('readable_raw_video_tracks') and not info['title_sets']:text=f"Video data is readable on {len(info['readable_raw_video_tracks'])} track(s), but scene metadata is unavailable. Recover Videos will attempt raw video salvage; scene completeness cannot be verified."
                elif not info['title_sets']:text+=' Recovery will scan raw images for video and scene metadata.'
                log.close('inspected',inventory=info);self.events.put(('detected',text))
            except Cancelled as error:log.exception('inspect_cancelled',error);log.close('stopped');self.events.put(('stopped',str(error)))
            except Exception as error:
                log.exception('inspect_disc',error);log.close('failed')
                self.events.put(('attention',str(error)+' Logs: '+str(log.directory)))
        threading.Thread(target=work,daemon=True).start()
    def start_saved(self):
        if self.busy:return
        selected=filedialog.askdirectory(title='Select the interrupted recovery folder',initialdir=self.folder.get())
        if selected:self.start(saved_source=selected)
    def start(self,saved_source=None):
        if self.busy:return
        if self.probing:self.root.after(200,lambda:self.start(saved_source));return
        drive=self.drive.get();parent=Path(self.folder.get())
        if not drive and not saved_source:self.message.set('Connect the DVD writer first.');return
        if parent.drive.upper()!='D:':
            self.app_log('ERROR','invalid_save_location',path=str(parent));return messagebox.showinfo('Save location','Choose a folder on D:.')
        try:parent.mkdir(parents=True,exist_ok=True)
        except OSError as error:
            self.app_log.exception('create_save_folder',error,path=str(parent));return messagebox.showerror('Save location',str(error))
        destination=parent/('Handycam_'+datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f'))
        self.cancel.clear();self.set_busy(True);self.progress['value']=0
        self.append('Recovery requested. Every track and recording will be checked.')
        def work():
            engine=None
            try:
                engine=RecoveryEngine(drive,destination,lambda text,p:self.events.put(('progress',(text,p))),self.cancel,saved_source=saved_source)
                self.events.put(('logs',str(engine.log.directory)))
                report=engine.run();self.events.put(('done',(report,destination)))
            except Cancelled as error:
                self.app_log.exception('recovery_cancelled',error,destination=str(destination))
                if engine:self.events.put(('logs',str(engine.log.directory)))
                self.events.put(('stopped',str(error)))
            except Exception as error:
                self.app_log.exception('start_or_recovery_failure',error,destination=str(destination))
                logs=engine.log.directory if engine else self.app_log.directory
                self.events.put(('logs',str(logs)))
                self.events.put(('attention',str(error)+' Full details: '+str(logs)))
        threading.Thread(target=work,daemon=True).start()
    def poll(self):
        if self.closed:return
        try:
            while True:
                kind,data=self.events.get_nowait()
                if kind=='logs':self.last_log_directory=Path(data);self.store_settings()
                elif kind=='probe':
                    self.probing=False;drive,result=data
                    if not self.busy and drive==self.drive.get():
                        labels={'ready':'Disc recognised · Detect Disc checks recorded tracks','empty':'Writer connected · no readable disc detected','loading':'Writer connected · disc is loading','disconnected':'DVD writer disconnected','attention':'Writer needs attention'}
                        label=labels.get(result['state'],result['state'])
                        if drive.lower().startswith('cdrom') and result['state']!='disconnected':label+=' (no drive letter required)'
                        self.device_status.set(label)
                        self.device_label.configure(foreground='#52c8b0' if result['state']=='ready' else '#e7b96b')
                        identity=(drive,result['state'])
                        if identity!=self.last_probe:
                            self.last_probe=identity;self.message.set(result['message']);self.app_log('INFO' if result['state']=='ready' else 'WARNING','drive_status_changed',drive=drive,**result)
                            if result['state']!='ready':
                                self.last_log_directory=self.app_log.directory;self.store_settings()
                elif kind=='progress':
                    self.message.set(data[0]);self.timer.observe(data[0],data[1]);self.progress['value']=self.timer.percent;self.global_progress['value']=self.timer.percent
                elif kind=='job_folder':self.latest=Path(data);self.store_settings()
                elif kind=='studio_done':
                    report,destination=data;self.latest=destination;self.last_log_directory=Path(report['log_directory']);self.store_settings();self.set_busy(False);self.studio.done(report)
                    self.progress['value']=100;self.global_progress['value']=100
                    self.sounds.play('success' if report.get('complete') else 'partial')
                elif kind=='done':
                    report,destination=data;self.latest=destination;self.store_settings();self.set_busy(False)
                    failed=report.get('status')=='failed' or not (report.get('validated_count',0)+report.get('partial_count',0))
                    label='RECOVERY FAILED — no playable video recovered' if failed else ('Complete recovery' if report.get('complete') else 'Partial recovery')
                    s=f"{label}: {report.get('validated_count',0)} validated, {report.get('partial_count',0)} partial/unverified, {report.get('failed_count',0)} failed, {report.get('missing_recordings',0)} missing."
                    self.last_log_directory=Path(report['log_directory'])
                    self.message.set(s);self.append(s+'\nSaved to '+str(destination));self.progress['value']=100;self.global_progress['value']=100
                    if failed:
                        self.progress.configure(style='Failure.Horizontal.TProgressbar');self.global_progress.configure(style='Failure.Horizontal.TProgressbar')
                    self.sounds.play('failure' if failed else ('success' if report.get('complete') else 'partial'))
                else:
                    self.set_busy(False);self.message.set(data);self.append(data)
                    if kind in ('stopped','attention','error'):self.timer.finish(completed=False)
                    if kind in ('attention','error'):self.sounds.play('failure')
                    elif kind=='stopped':self.sounds.play('stopped')
                    elif kind=='detected':self.sounds.play('success')
                    if kind=='error':messagebox.showerror('Recovery needs attention',data)
        except queue.Empty:pass
        self.root.after(100,self.poll)
    def open_folder(self):
        target=self.latest if self.latest and self.latest.exists() else Path(self.folder.get())
        if target.exists():os.startfile(target)
        else:self.message.set('Recover a disc first, or choose an existing save folder.')
    def close(self):
        if self.busy:self.stop_operation();return
        self.closed=True;self.store_settings();self.root.destroy()

if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--diagnose':
        result={'version':VERSION,'drives':{d:probe_disc(d) for d in optical_drives()},'ffmpeg_present':(BASE/'ffmpeg.exe').is_file()}
        Path(sys.argv[2]).write_text(json.dumps(result,indent=2),encoding='utf-8')
    else:
        root=tk.Tk();App(root);root.mainloop()
