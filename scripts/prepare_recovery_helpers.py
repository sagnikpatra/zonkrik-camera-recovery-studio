import hashlib,json,re,urllib.request,zipfile
from pathlib import Path
root=Path(__file__).resolve().parents[1];downloads=root/'work'/'downloads';downloads.mkdir(exist_ok=True)
tools=root/'tools';tools.mkdir(exist_ok=True)
page=urllib.request.urlopen('https://www.cgsecurity.org/wiki/TestDisk_Download').read().decode()
links=re.findall(r'href="([^"]+\.win64\.zip)"',page)
stable=next((link for link in links if 'WIP' not in link),None)
if not stable:raise RuntimeError('No stable official PhotoRec Windows package found.')
if stable.startswith('/'):stable='https://www.cgsecurity.org'+stable
stable=stable.replace('/Download_and_donate.php/','/')
release=json.load(urllib.request.urlopen('https://api.github.com/repos/anthwlock/untrunc/releases/latest'))
asset=next(item for item in release['assets'] if item['name']=='untrunc_x64.zip')
packages=[('photorec',stable,None),('untrunc',asset['browser_download_url'],asset.get('digest'))]
manifest=[]
for name,url,expected in packages:
    path=downloads/(name+'.zip');urllib.request.urlretrieve(url,path)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    if expected and expected!='sha256:'+digest:raise RuntimeError('Download hash does not match published release metadata.')
    destination=tools/name;destination.mkdir(exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            target=(destination/entry.filename).resolve()
            if not target.is_relative_to(destination.resolve()):raise RuntimeError('Unsafe archive path.')
        archive.extractall(destination)
    manifest.append({'tool':name,'url':url,'sha256':digest,'published_sha256_verified':bool(expected),'archive_bytes':path.stat().st_size,
                     'executables':[str(p.relative_to(root)) for p in destination.rglob('*.exe')]})
    print(name,digest,flush=True)
(tools/'download_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
