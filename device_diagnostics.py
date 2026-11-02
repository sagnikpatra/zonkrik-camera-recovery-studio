"""Windows device/driver inventory and explicitly selected signed-driver install."""
from pathlib import Path
import ctypes,json,re,os
from studio_jobs import BASE,save_json
from recovery import Disc,optical_drives
from recovery_logging import error_details

SCRIPT=r'''
$ErrorActionPreference='Stop'
$devices=Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPClass -in @('CDROM','Camera','Image','1394','WPD') -or ($_.PNPClass -eq 'MEDIA' -and $_.Name -match 'capture|video|1394|DV|Handycam') }
$drivers=Get-CimInstance Win32_PnPSignedDriver
$items=@($devices | ForEach-Object { $entry=$_; $driver=$drivers | Where-Object {$_.DeviceID -eq $entry.PNPDeviceID} | Select-Object -First 1; [PSCustomObject]@{name=$entry.Name;device_id=$entry.PNPDeviceID;device_class=$entry.PNPClass;status=$entry.Status;problem_code=$entry.ConfigManagerErrorCode;hardware_ids=@($entry.HardwareID);provider=$driver.DriverProviderName;version=$driver.DriverVersion;inf=$driver.InfName;signed=$driver.IsSigned} })
ConvertTo-Json -InputObject $items -Depth 5 -Compress
'''

def device_inventory(job):
    result=job.command(['powershell.exe','-NoProfile','-NonInteractive','-Command',SCRIPT],'Windows device inventory',timeout=60)
    if result['returncode']:raise RuntimeError('Windows device enumeration failed. See the saved PowerShell error log.')
    entries=json.loads(result['stdout_text'].lstrip('\ufeff') or '[]')
    entries=entries if isinstance(entries,list) else [entries]
    job.report['devices']=entries;job.report['optical_checks']=[]
    for drive in optical_drives():
        device=None;info={'drive':drive}
        try:
            device=Disc(drive,logger=job.log)
            config=device.command([0x46,0,0,0,0,0,0,0,8,0],8)
            info['current_profile']=int.from_bytes(config[6:8],'big') if len(config)>=8 else None
            device.command([0x00,0,0,0,0,0],0);info['medium_ready']=True;info['tracks']=device.tracks()
        except Exception as error:
            info.update(medium_ready=False,error=error_details(error));job.log.exception('optical_device_status',error,drive=drive)
        finally:
            if device:device.close()
        job.report['optical_checks'].append(info)
    job.report['driver_changes']='None. Healthy signed Windows class drivers are retained.'
    job.report['summary']='Device check finished. Open the report for signed-driver status, device problems and exact optical media errors.'
    save_json(job.dest/'device_inventory.json',job.report)
    return entries

def rescan_devices(job):
    if not ctypes.windll.shell32.IsUserAnAdmin():raise PermissionError('Windows requires administrator access for a hardware rescan. Right-click this app and choose Run as administrator.')
    result=job.command([Path(os.environ['WINDIR'])/'System32'/'pnputil.exe','/scan-devices'],'Windows hardware rescan',timeout=90)
    if result['returncode']:raise RuntimeError('Windows could not rescan devices. See the command log.')
    device_inventory(job)

def validate_driver_package(inf,hardware_ids):
    inf=Path(inf).resolve()
    if not inf.is_file() or inf.suffix.lower()!='.inf':raise ValueError('Select an extracted manufacturer INF driver package.')
    raw=inf.read_bytes();text=raw.decode('utf-16') if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else raw.decode('utf-8-sig',errors='replace')
    text='\n'.join(line.split(';',1)[0] for line in text.splitlines())
    matches=[item for item in hardware_ids if item and item.casefold() in text.casefold()]
    if not matches:raise ValueError('This INF does not declare any hardware ID reported by the selected device. It will not be installed.')
    names=re.findall(r'^\s*CatalogFile(?:\.[\w.]+)?\s*=\s*"?([^"\r\n]+?)"?\s*$',text,re.MULTILINE|re.IGNORECASE)
    catalogs=[]
    for name in names:
        path=(inf.parent/name.strip()).resolve()
        if path.is_relative_to(inf.parent) and path.is_file():catalogs.append(path)
    if not catalogs:raise ValueError('The package has no local catalog file. A signed manufacturer package is required.')
    return inf,catalogs,matches

def install_selected_driver(inf,device_id,job):
    devices=device_inventory(job);device=next((d for d in devices if d['device_id']==device_id),None)
    if not device:raise ValueError('The selected device is no longer present. Scan devices again.')
    inf,catalogs,matches=validate_driver_package(inf,device.get('hardware_ids') or [])
    valid=[]
    signature_script=BASE/'scripts'/'catalog_signature.ps1'
    for catalog in catalogs:
        source=signature_script.read_text(encoding='utf-8-sig')
        command='& {\n'+source+"\n} -Catalog '"+str(catalog).replace("'","''")+"'"
        result=job.command(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],'Verify driver catalog',timeout=60)
        signature=json.loads(result['stdout_text'].lstrip('\ufeff') or '{}')
        if signature.get('Status')=='Valid':valid.append(signature)
    if not valid:raise ValueError('Windows could not validate the driver catalog signature. The package will not be installed.')
    if not ctypes.windll.shell32.IsUserAnAdmin():raise PermissionError('Installing a driver requires Windows administrator access. Right-click this app and choose Run as administrator.')
    job.report.update(selected_device=device,driver_inf=str(inf),matching_hardware_ids=matches,catalog_signatures=valid)
    result=job.command([Path(os.environ['WINDIR'])/'System32'/'pnputil.exe','/add-driver',inf,'/install'],'Install matching signed driver',timeout=180)
    job.report['driver_install_exit_code']=result['returncode']
    if result['returncode'] not in (0,3010):raise RuntimeError('Windows rejected or could not install the package. See the full PnPUtil log.')
    job.report['restart_required']=result['returncode']==3010
    job.report['summary']='Signed matching driver package processed by Windows. Restart Windows if the saved result requests it.'
