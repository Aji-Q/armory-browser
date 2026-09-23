#!/usr/bin/env python3
"""Build a deterministic, source-only Chrome MV3 ZIP; never include profiles/secrets."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

def build(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    manifest=json.loads((source/'manifest.json').read_text())
    if manifest.get('manifest_version') != 3:
        raise ValueError('Manifest V3 required')
    banned={'cookies','debugger','webRequest','webRequestBlocking','nativeMessaging'}
    if banned.intersection(manifest.get('permissions',[])):
        raise ValueError('Unexpected privileged extension permission')
    if manifest.get('host_permissions'):
        raise ValueError('Use runtime optional host permissions, not default host access')
    for relative in [manifest['background']['service_worker'],manifest['side_panel']['default_path']]:
        file=(source/relative).resolve()
        if not file.is_relative_to(source) or not file.is_file():
            raise ValueError('Missing or invalid manifest resource')
    files=[]
    for file in sorted(source.rglob('*')):
        relative=file.relative_to(source)
        if file.is_symlink():raise ValueError('Symlinks not allowed in extension package')
        if file.is_file() and file.suffix in {'.json','.js','.mjs','.css','.html','.svg','.png'} and not any(p.startswith('.') for p in relative.parts):
            if any(x in file.name.lower() for x in ['client.json','config.json','credentials','secret','token.json']):
                raise ValueError('Unexpected credential/config file in extension source')
            files.append(file)
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for file in files:
            info=zipfile.ZipInfo(file.relative_to(source).as_posix(),date_time=(2026,9,23,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=0o100644 << 16
            z.writestr(info,file.read_bytes())
    with zipfile.ZipFile(output) as z:
        if z.testzip() is not None:raise ValueError('ZIP validation failed')
        if json.loads(z.read('manifest.json')) != manifest:raise ValueError('Manifest mismatch')
        for file in files:
            if z.read(file.relative_to(source).as_posix()) != file.read_bytes():raise ValueError('ZIP source mismatch')
    return {'path':str(output),'files':len(files),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'manifest_version':3,'version':manifest['version']}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--extension',type=Path,default=Path(__file__).resolve().parents[1]/'extension')
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();print(json.dumps(build(a.extension,a.output),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
