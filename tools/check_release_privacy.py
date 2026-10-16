"""Check indexed release bytes, without printing matching secret values.

Run after explicitly staging a reviewed public-only file list. This does not
read runtime environments, credential stores or untracked private files.
"""
import json
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]
CONTENT_RULES={
    'personal_path_or_host':re.compile(rb'(?:[A-Za-z]:[\\/]Users[\\/]Administrator[\\/]|D:[\\/]ocv[\\/]|/home/' rb'l/|192\.168\.\d+\.\d+)'),
    'provider_key':re.compile(rb'\bsk-[A-Za-z0-9_-]{24,}\b'),
    'jwt':re.compile(rb'\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b'),
    'private_key':re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'url_password':re.compile(rb'https?://[^\s/@:]+:[^\s/@]{12,}@'),
}
PRIVATE_PARTS={'.env','.session-secret','config.private.json','workspace','runtime','models','output','TTS_Output','node_modules'}
PRIVATE_EXTENSIONS={'.wav','.mp3','.mp4','.safetensors','.ckpt','.pfx','.p12','.pem'}

def findings(name,data):
    path=Path(name)
    result=[]
    if any(p in PRIVATE_PARTS or p.startswith('private_') or p.startswith('.env.') for p in path.parts) or path.suffix.lower() in PRIVATE_EXTENSIONS:
        result.append(dict(file=name,rule='private_release_path'))
    for rule,pattern in CONTENT_RULES.items():
        if pattern.search(data):result.append(dict(file=name,rule=rule))
    return result

def check():
    names=subprocess.check_output(['git','ls-files','--cached','-z'],cwd=ROOT).split(b'\0')
    issues=[];count=0
    for raw in names:
        if not raw:continue
        name=raw.decode('utf8')
        data=subprocess.check_output(['git','show',':'+name],cwd=ROOT)
        issues.extend(findings(name,data));count+=1
    return dict(indexed_files=count,issues=issues,passed=not issues,secret_values_printed=False)

if __name__=='__main__':
    result=check();print(json.dumps(result,indent=2))
    raise SystemExit(not result['passed'])
