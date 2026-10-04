"""Build the checksum-pinned Additional machine static model assets."""
import argparse,hashlib,json,tempfile,urllib.request,zipfile
from pathlib import Path,PurePosixPath
ROOT=Path(__file__).resolve().parents[1]
def sha(p):
 with p.open('rb')as f:return hashlib.file_digest(f,'sha256').hexdigest()
def unpack(archive,target,info):
 if archive.stat().st_size!=info['bytes']or sha(archive)!=info['sha256']:raise ValueError('Bundle checksum mismatch')
 with zipfile.ZipFile(archive)as z:
  manifest=json.loads(z.read('VIEWER_ASSETS.json'));records=manifest['files'];names=z.namelist();expected={r['path']for r in records}
  if len(names)!=len(set(names))or len(expected)!=len(records)or set(names)!=expected|{'VIEWER_ASSETS.json'}or len(records)!=info['files']:raise ValueError('Bundle inventory mismatch')
  total=0
  for r in records:
   n=r['path'];p=PurePosixPath(n);total+=r['bytes']
   if p.is_absolute()or '..'in p.parts or '\\'in n or ':'in n or total>900_000_000:raise ValueError('Invalid asset path or size')
   if p.suffix not in ['.json','.gz','.html','.txt','.md']and p.name!='LICENSE'and n!='.nojekyll':raise ValueError('Unsupported asset type')
   dest=(target/p).resolve()
   if not dest.is_relative_to(target)or dest.exists()or z.getinfo(n).file_size!=r['bytes']:raise ValueError('Asset collision or size mismatch')
   dest.parent.mkdir(parents=True,exist_ok=True);h=hashlib.sha256()
   with z.open(n)as f,dest.open('wb')as o:
    for b in iter(lambda:f.read(1024*1024),b''):h.update(b);o.write(b)
   if h.hexdigest()!=r['sha256']:raise ValueError('Asset checksum mismatch')
  if total!=info['site_bytes']:raise ValueError('Site size mismatch')
 (target/'ASSET_BUNDLE.json').write_bytes((ROOT/'site/ASSET_BUNDLE.json').read_bytes())
 if sum(p.stat().st_size for p in target.rglob('*')if p.is_file())>900_000_000:raise ValueError('Site exceeds size budget')
 print(f"Built {len(records)} verified asset files ({total:,} bytes).")
def main():
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--assets',type=Path);a=p.parse_args();target=a.output.resolve()
 if target.exists():raise ValueError('Use a new output directory')
 target.mkdir(parents=True);info=json.loads((ROOT/'site/ASSET_BUNDLE.json').read_text(encoding='utf8'))
 if a.assets:unpack(a.assets,target,info);return
 if not info['url'].startswith('https://github.com/Psych0h3ad/3d-print-rig-extra-models/releases/download/'):raise ValueError('Unexpected release URL')
 with tempfile.TemporaryDirectory()as folder:
  archive=Path(folder)/'models.zip';req=urllib.request.Request(info['url'],headers={'User-Agent':'3D-Print-Rig'});total=0
  with urllib.request.urlopen(req,timeout=60)as f,archive.open('wb')as o:
   for b in iter(lambda:f.read(1024*1024),b''):
    total+=len(b)
    if total>info['bytes']:raise ValueError('Unexpected download size')
    o.write(b)
  unpack(archive,target,info)
if __name__=='__main__':main()
