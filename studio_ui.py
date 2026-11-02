"""Additional recovery workflows; existing DVD recovery remains available."""
import tkinter as tk
from tkinter import ttk,filedialog,messagebox
import threading,os,webbrowser
from pathlib import Path
from studio_jobs import Job
from recovery import Cancelled
from camera_sources import import_camera,image_card,removable_cards
from recovery_tools import recover_deleted,repair_video,enumerate_capture,capture_tape,inspect_capture_options
from device_diagnostics import device_inventory,rescan_devices,install_selected_driver
from dvd_vr import split_vro
from research_catalog import METHODS
from portable_camera import list_portable,import_portable
from merge_images import merge_track_copies

class StudioPanels:
    def __init__(self,app,notebook):
        self.app=app;self.root=app.root;self.controls=[];self.devices=[];self.portable_devices=[]
        self.camera_tab(notebook);self.capture_tab(notebook);self.repair_tab(notebook);self.devices_tab(notebook);self.guide_tab(notebook)
    def tab(self,notebook,title):
        outer=ttk.Frame(notebook);notebook.add(outer,text=title)
        canvas=tk.Canvas(outer,bg='#101823',highlightthickness=0);scroll=ttk.Scrollbar(outer,orient='vertical',command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set);scroll.pack(side='right',fill='y');canvas.pack(side='left',fill='both',expand=True)
        frame=ttk.Frame(canvas,padding=22);window=canvas.create_window((0,0),window=frame,anchor='nw')
        frame.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=e.width))
        def wheel(event):
            if str(event.widget).startswith(str(frame)) and event.widget.winfo_class() not in ('Text','Listbox','TCombobox','Treeview'):
                canvas.yview_scroll(-int(event.delta/120),'units')
        self.root.bind_all('<MouseWheel>',wheel,add='+')
        return frame
    def label(self,parent,text,title=False):
        ttk.Label(parent,text=text,font=('Segoe UI Semibold',16 if title else 10),wraplength=950).pack(anchor='w',pady=(5,9))
    def button(self,parent,text,command):
        button=ttk.Button(parent,text=text,command=command);button.pack(side='left',padx=(0,8),pady=7);self.controls.append(button);return button
    def path(self,parent,label,folder=False):
        self.label(parent,label);row=ttk.Frame(parent);row.pack(fill='x',pady=(0,5));value=tk.StringVar()
        ttk.Entry(row,textvariable=value).pack(side='left',fill='x',expand=True,ipady=5)
        def browse():
            selected=filedialog.askdirectory() if folder else filedialog.askopenfilename()
            if selected:value.set(selected)
        self.button(row,'Browse',browse);return value
    def run(self,kind,operation):
        if self.app.busy:return
        if self.app.probing:self.root.after(200,lambda:self.run(kind,operation));return
        self.app.cancel.clear();self.app.set_busy(True);parent=self.app.folder.get()
        self.app.message.set(kind+' is starting…');self.app.append(kind+' requested.')
        def worker():
            job=None
            try:
                job=Job(kind,parent,status=lambda text,value:self.app.events.put(('progress',(text,value))),cancel=self.app.cancel)
                self.app.events.put(('logs',str(job.log.directory)));self.app.events.put(('job_folder',str(job.dest)))
                report=job.run(operation);self.app.events.put(('studio_done',(report,job.dest)))
            except Cancelled as error:
                if job:self.app.events.put(('logs',str(job.log.directory)))
                self.app.events.put(('stopped',str(error)))
            except Exception as error:
                self.app.app_log.exception('studio_job',error,kind=kind)
                logs=job.log.directory if job else self.app.app_log.directory
                self.app.events.put(('logs',str(logs)));self.app.events.put(('attention',str(error)+' Full details: '+str(logs)))
        threading.Thread(target=worker,daemon=True).start()
    def set_busy(self,busy):
        for widget in self.controls:widget.configure(state='disabled' if busy else 'normal')
    def done(self,report):
        if report.get('portable_devices') is not None:
            self.portable_devices=report['portable_devices'];self.portable_combo['values']=[item['name'] for item in self.portable_devices];self.portable_choice.set('')
        if report.get('capture_devices') is not None:
            devices=report['capture_devices'];self.video_combo['values']=devices['video'];self.audio_combo['values']=['(No separate audio)']+devices['audio']
            self.capture_info.set(f"{len(devices['video'])} video devices and {len(devices['audio'])} audio devices. Select the tape adapter; webcams may also be listed.")
        if report.get('devices') is not None:
            self.devices=report['devices']
            for item in self.device_tree.get_children():self.device_tree.delete(item)
            for index,item in enumerate(self.devices):
                self.device_tree.insert('', 'end',iid=str(index),values=(item['name'],item['device_class'],item['problem_code'],item['provider'],item['signed']))
            checks=report.get('optical_checks',[])
            self.device_info.set('\n'.join(f"{item['drive']}: "+('medium ready' if item.get('medium_ready') else item.get('error',{}).get('message','not ready')) for item in checks) or 'Device inventory saved. Select a row to inspect/install a matching manufacturer package.')
        if report.get('device_options'):
            self.options.configure(state='normal');self.options.delete('1.0','end');self.options.insert('end',report['device_options']);self.options.configure(state='disabled')
        label='Finished' if report.get('complete') else 'Finished with partial/unverified results'
        summary=report.get('summary') or (f"{label}: "+f"{report.get('verified_files',report.get('validated_count',report.get('recovered_candidates',0)))} files checked/recovered. Open results and the report for details.")
        if not report.get('complete') and not summary.startswith(label):summary=label+'. '+summary
        self.app.message.set(summary);self.app.append(summary+'\nSaved to '+str(report['destination']))

    def camera_tab(self,notebook):
        tab=self.tab(notebook,'Camera / cards');self.label(tab,'Import camera files, photographs and RAW originals',True)
        self.label(tab,'Use a memory-card reader, mounted camera USB/HDD storage, a local folder or one existing video file. Originals and companion metadata are preserved separately from converted MP4s.')
        self.source=self.path(tab,'Source folder / card drive / file',folder=True)
        self.photos=tk.BooleanVar(value=True);self.backup=tk.BooleanVar(value=True);self.convert=tk.BooleanVar(value=True)
        for text,var in [('Include photos and camera RAW originals',self.photos),('Preserve all files and camera folder structure',self.backup),('Convert videos to MP4 and fully check source/output frames and audio',self.convert)]:ttk.Checkbutton(tab,text=text,variable=var).pack(anchor='w',pady=6)
        row=ttk.Frame(tab);row.pack(fill='x')
        def start():
            source=self.source.get();photos=self.photos.get();backup=self.backup.get();convert=self.convert.get()
            self.run('Camera_import',lambda job:import_camera(source,job,photos,backup,convert))
        self.button(row,'Import and verify',start)
        def select_file():
            selected=filedialog.askopenfilename()
            if selected:self.source.set(selected)
        self.button(row,'Choose one existing file',select_file)
        self.label(tab,'PTP / MTP cameras with no drive letter',True)
        self.label(tab,'The Windows transfer bridge copies photos, videos and camera sidecars from a detected portable camera. Device-reported sizes are checked and saved-file hashes recorded; source checksums are unavailable. Windows may finish the current file after Stop. Convert/check transferred videos by selecting the resulting Portable originals folder above.')
        self.portable_choice=tk.StringVar();self.portable_combo=ttk.Combobox(tab,textvariable=self.portable_choice,state='readonly',width=70);self.portable_combo.pack(anchor='w',fill='x')
        row=ttk.Frame(tab);row.pack(fill='x');self.button(row,'Find PTP/MTP cameras',lambda:self.run('Portable_camera_list',lambda job:list_portable(job)))
        def portable():
            selection=self.portable_combo.current()
            if selection<0:return messagebox.showinfo('Portable camera','Scan and select your camera first.')
            key=self.portable_devices[selection]['key'];self.run('Portable_camera_import',lambda job:import_portable(key,job))
        self.button(row,'Import portable camera',portable)
        self.button(row,'Open cameras in Windows',lambda:os.startfile('shell:MyComputerFolder'))
        self.label(tab,'AVCHD / XAVC / HDD cameras: keep PRIVATE, BDMV, CLIPINF, PLAYLIST and other companion folders. Existing DV/HDV captures can be imported here too.')

    def capture_tab(self,notebook):
        tab=self.tab(notebook,'Tape capture');self.label(tab,'Capture from a tape deck, camera or A/V adapter',True)
        self.label(tab,'A working playback device and the correct connection are required. MiniDV/Digital8 usually use IEEE 1394; analogue Video8/Hi8/VHS use a compatible A/V capture adapter. Start playback on the device. A USB DVD writer cannot read tapes.')
        self.capture_info=tk.StringVar(value='Enumerate devices, then explicitly select your capture adapter and audio input.')
        ttk.Label(tab,textvariable=self.capture_info,wraplength=950).pack(anchor='w',pady=6)
        row=ttk.Frame(tab);row.pack(fill='x');self.button(row,'Find capture devices',lambda:self.run('Capture_devices',lambda job:enumerate_capture(job)))
        self.video=tk.StringVar();self.audio=tk.StringVar(value='(No separate audio)')
        self.label(tab,'Video capture device');self.video_combo=ttk.Combobox(tab,textvariable=self.video,state='readonly',width=78);self.video_combo.pack(anchor='w',fill='x')
        self.label(tab,'Audio input');self.audio_combo=ttk.Combobox(tab,textvariable=self.audio,state='readonly',values=['(No separate audio)'],width=78);self.audio_combo.pack(anchor='w',fill='x')
        self.mode=tk.StringVar(value='Lossless archive (FFV1 + PCM)');self.standard=tk.StringVar(value='Device default')
        self.label(tab,'Archive mode');ttk.Combobox(tab,textvariable=self.mode,state='readonly',values=['Lossless archive (FFV1 + PCM)','Preserve DV/HDV stream','MP4-compatible H.264 archive'],width=55).pack(anchor='w')
        self.label(tab,'Input standard');ttk.Combobox(tab,textvariable=self.standard,state='readonly',values=['Device default','PAL 720×576 / 25 fps','NTSC 720×480 / 29.97 fps'],width=55).pack(anchor='w')
        self.duration=tk.StringVar(value='0');row=ttk.Frame(tab);row.pack(fill='x',pady=9);ttk.Label(row,text='Capture limit in minutes (0 = until Stop): ').pack(side='left');ttk.Entry(row,textvariable=self.duration,width=10).pack(side='left')
        row=ttk.Frame(tab);row.pack(fill='x')
        def options():
            video=self.video.get();self.run('Capture_formats',lambda job:inspect_capture_options(video,job))
        def start():
            video=self.video.get();audio=self.audio.get();audio='' if audio=='(No separate audio)' else audio;mode=self.mode.get();minutes=self.duration.get();standard=self.standard.get()
            if not video:return messagebox.showinfo('Capture device','Select the actual tape/capture device first.')
            if not audio and mode!='Preserve DV/HDV stream' and not messagebox.askyesno('Capture without separate audio?','No audio input is selected. Continue without a separate audio input?'):return
            self.run('Tape_capture',lambda job:capture_tape(video,audio,mode,minutes,standard,job))
        self.button(row,'Show supported formats',options);self.button(row,'Start capture',start)
        self.label(tab,'Stop finalizes the archive. Convert/check that archive later in Camera / cards. Without a time limit, remaining tape duration is unknown.')
        self.options=tk.Text(tab,height=7,wrap='word',bg='#192534',fg='#dce6f2',state='disabled');self.options.pack(fill='x',pady=8)

    def repair_tab(self,notebook):
        tab=self.tab(notebook,'Deleted / repair');self.label(tab,'Recover deleted files from a saved image',True)
        self.label(tab,'PhotoRec reads a saved image and writes candidate files into a new D: folder. Names/folders may be lost; fragmented camera movies can need additional reconstruction. Recovered candidates require validation.')
        self.image=self.path(tab,'Card/disc image (.img / .dd / .bin / .iso / .e01)')
        self.all_formats=tk.BooleanVar();self.keep_corrupt=tk.BooleanVar()
        ttk.Checkbutton(tab,text='Scan every PhotoRec file family (otherwise camera photo/video families)',variable=self.all_formats).pack(anchor='w')
        ttk.Checkbutton(tab,text='Keep damaged candidates for further repair',variable=self.keep_corrupt).pack(anchor='w')
        row=ttk.Frame(tab);row.pack(fill='x')
        def recover():
            image=self.image.get();all_formats=self.all_formats.get();damaged=self.keep_corrupt.get();self.run('Deleted_recovery',lambda job:recover_deleted(image,job,all_formats,damaged))
        self.button(row,'Recover files from image',recover)
        def merge():
            paths=filedialog.askopenfilenames(title='Choose saved copies of the same DVD track',filetypes=[('Saved DVD tracks','*.bin *.partial'),('All files','*.*')])
            if paths:self.run('Merge_DVD_copies',lambda job:merge_track_copies(paths,job))
        self.button(row,'Combine interrupted DVD copies…',merge)
        self.label(tab,'Create an image of a removable card');row=ttk.Frame(tab);row.pack(fill='x');self.card=tk.StringVar();self.card_combo=ttk.Combobox(row,textvariable=self.card,state='readonly',width=12);self.card_combo.pack(side='left',padx=(0,8))
        def cards():
            values=removable_cards();self.card_combo['values']=values;self.card.set(values[0] if values else '')
            if not values:self.app.message.set('No removable card drive is detected. Insert a card/reader, or choose an existing image.')
        def make_image():
            drive=self.card.get();self.run('Card_image',lambda job:image_card(drive,job))
        self.button(row,'Refresh cards',cards);self.button(row,'Create read-only card image',make_image)
        self.label(tab,'Repair a damaged video container',True)
        self.broken=self.path(tab,'Damaged video');self.reference=self.path(tab,'Healthy reference clip (same camera/settings)')
        self.reference_mode=tk.BooleanVar(value=True);ttk.Checkbutton(tab,text='Use reference-based untrunc repair (otherwise stream remux)',variable=self.reference_mode).pack(anchor='w')
        row=ttk.Frame(tab);row.pack(fill='x')
        def repair():
            broken=self.broken.get();reference=self.reference.get();use=self.reference_mode.get();self.run('Video_repair',lambda job:repair_video(broken,reference,job,use))
        self.button(row,'Repair working copy and check',repair)
        self.label(tab,'Split DVD-VR physical program regions',True)
        self.manager=self.path(tab,'VR_MANGR.IFO / VR_MANGR.BUP');self.vro=self.path(tab,'VR_MOVIE.VRO')
        row=ttk.Frame(tab);row.pack(fill='x')
        def vr():
            manager=self.manager.get();vro=self.vro.get();self.run('DVD_VR_split',lambda job:split_vro(manager,vro,job))
        self.button(row,'Split indexed DVD-VR regions',vr)
        self.label(tab,'DVD-VR edited playlists/still-image groups are not interpreted. Keep the full original VRO; use raw disc recovery when its directory length is zero.')

    def devices_tab(self,notebook):
        tab=self.tab(notebook,'Devices / drivers');self.label(tab,'Check the actual connected hardware',True)
        self.label(tab,'Inspect signed-driver status, Windows device problems and exact DVD medium errors. A healthy Windows CD-ROM class driver is retained. A driver cannot create data when the drive cannot recognise the medium.')
        row=ttk.Frame(tab);row.pack(fill='x');self.button(row,'Check devices and drivers',lambda:self.run('Device_check',lambda job:device_inventory(job)))
        self.button(row,'Windows hardware rescan',lambda:self.run('Hardware_rescan',lambda job:rescan_devices(job)))
        self.device_tree=ttk.Treeview(tab,columns=('name','class','problem','provider','signed'),show='headings',height=10)
        for key,title,width in [('name','Device',320),('class','Class',90),('problem','Problem code',100),('provider','Driver provider',160),('signed','Signed',80)]:self.device_tree.heading(key,text=title);self.device_tree.column(key,width=width)
        self.device_tree.pack(fill='x',pady=10)
        self.device_info=tk.StringVar(value='Run Check devices and drivers. Reports include hardware IDs, INF, version and raw sense codes.')
        ttk.Label(tab,textvariable=self.device_info,wraplength=950).pack(anchor='w',pady=10)
        self.label(tab,'Install a manufacturer package only when it matches the selected device. Windows validates the signed catalog; administrator access may be required.')
        row=ttk.Frame(tab);row.pack(fill='x')
        def install():
            selection=self.device_tree.selection()
            if not selection:return messagebox.showinfo('Device','Select a device from the checked inventory first.')
            device_id=self.devices[int(selection[0])]['device_id'];inf=filedialog.askopenfilename(title='Select matching manufacturer INF',filetypes=[('Driver INF','*.inf')])
            if inf:self.run('Driver_install',lambda job:install_selected_driver(inf,device_id,job))
        self.button(row,'Install matching signed INF…',install)
        self.button(row,'Open manufacturer support',lambda:webbrowser.open('https://www.sony.com/electronics/support'))
    def guide_tab(self,notebook):
        tab=self.tab(notebook,'Transfer methods');self.label(tab,'Choose the method that matches the recording medium',True)
        self.label(tab,'Built-in tools and hardware-dependent/manual routes are identified separately. There is no universal software driver for every old camera, tape or damaged disc.')
        row=ttk.Frame(tab);row.pack(fill='both',expand=True)
        listing=tk.Listbox(row,width=43,height=20,bg='#192534',fg='#dce6f2',selectbackground='#276a62',exportselection=False,font=('Segoe UI',10));listing.pack(side='left',fill='y')
        details=tk.Text(row,width=64,height=23,wrap='word',bg='#192534',fg='#dce6f2',font=('Segoe UI',11),padx=14,pady=12,state='disabled');details.pack(side='left',fill='both',expand=True,padx=(12,0))
        for method in METHODS:listing.insert('end',method[0])
        def selected(event=None):
            index=listing.curselection()
            if not index:return
            title,status,description,url=METHODS[index[0]];details.configure(state='normal');details.delete('1.0','end');details.insert('end',title+'\n\n'+status+'\n\n'+description+'\n\nSource: '+url);details.configure(state='disabled')
        listing.bind('<<ListboxSelect>>',selected);listing.selection_set(0);selected()
        row=ttk.Frame(tab);row.pack(fill='x')
        self.button(row,'Open selected official/project source',lambda:webbrowser.open(METHODS[listing.curselection()[0]][3]) if listing.curselection() else None)
