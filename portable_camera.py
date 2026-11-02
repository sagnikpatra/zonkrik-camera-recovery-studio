"""Windows PTP/MTP transfer bridge; copies only, never deletes device objects."""
from pathlib import Path
import json
from studio_jobs import BASE,sha256

def script_command(mode,**parameters):
    # Run our bundled source as a command without changing Windows script policy.
    script=(BASE/'scripts'/'portable_camera.ps1').read_text(encoding='utf-8-sig')
    values=' '.join('-'+key+" '"+str(value).replace("'","''")+"'" for key,value in {'Mode':mode,**parameters}.items())
    return ['powershell.exe','-NoProfile','-NonInteractive','-STA','-Command','& {\n'+script+'\n} '+values]

def list_portable(job):
    result=job.command(script_command('List'),'Find PTP MTP cameras',timeout=45)
    if result['returncode']:raise RuntimeError('Windows could not enumerate portable cameras. See the command log.')
    devices=json.loads(result['stdout_text'].lstrip('\ufeff') or '{}').get('devices',[])
    job.report['portable_devices']=devices;job.report['summary']=f'{len(devices)} Windows portable camera/device entries found. Connect in PTP/MTP mode if your camera is absent.'
    return devices

def import_portable(key,job):
    if not key:raise ValueError('Choose a detected PTP/MTP camera first.')
    output=job.dest/'Portable originals'
    job.report['cancellation_note']='Windows may finish the current asynchronous file copy after Stop. No further files are requested; preserved output remains in this folder.'
    job.save()
    result=job.command(script_command('Import',DeviceKey=key,Destination=output),'Copy PTP MTP camera files')
    if result['returncode']:raise RuntimeError('The Windows portable-camera transfer failed. See the PowerShell log; partial copies remain saved.')
    manifest=output/'portable_report.json'
    data=json.loads(manifest.read_text(encoding='utf-8-sig'))
    for item in data['files']:
        job.check();target=Path(item['target']).resolve()
        if not target.is_relative_to(output.resolve()):raise ValueError('Invalid path in portable-camera transfer report.')
        if target.stat().st_size!=item['bytes']:raise OSError('A transferred file changed size after Windows completed the copy.')
        item['saved_sha256']=sha256(target,job.check)
    job.report.update(portable_transfer=data,verified_files=len(data['files']),summary=f"{len(data['files'])} portable-camera files transferred and size-checked. Saved-file hashes recorded; the camera does not provide a source checksum. Use Camera/cards to decode/convert videos.")
    if data['failures'] or not data['files']:job.report['incomplete_reasons']=['Some camera objects could not be copied or no supported media were listed. See portable_report.json.']
