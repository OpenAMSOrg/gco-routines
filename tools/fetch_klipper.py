#!/usr/bin/env python3
"""Fetch a complete source tree at an explicit ref and record its resolved SHA.

No installation or execution of fetched source. Ref defaults to master ONLY for
initial selection; the output SHA is the reproducible integration target. Refuse
to overwrite an existing destination. No fake pin on network failure.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys

FILES = ['klippy/gcode.py', 'klippy/reactor.py', 'klippy/klippy.py',
         'klippy/extras/gcode_macro.py', 'klippy/extras/virtual_sdcard.py',
         'klippy/extras/pause_resume.py', 'klippy/webhooks.py',
         'scripts/klippy-requirements.txt', 'COPYING']
URL = 'https://github.com/Klipper3d/klipper.git'

def run(args, cwd, timeout=120):
    return subprocess.run(['git', *args], cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout).stdout.strip()

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ref', default='master')
    p.add_argument('--dest', type=Path, default=Path('vendor/klipper'))
    p.add_argument('--manifest', type=Path, default=Path('evidence/klipper-pin.json'))
    args=p.parse_args(argv)
    if args.ref.startswith('-') or not args.ref:
        p.error('ref must be a nonempty ref/SHA, not a command option')
    dest=args.dest.resolve()
    if dest.exists():
        p.error('destination already exists; choose a new path, do not overwrite a checkout')
    if args.manifest.exists():
        p.error('manifest already exists; choose a new path rather than replacing an earlier pin')
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='gco-klipper-', dir=dest.parent) as temp:
            path=Path(temp)
            run(['init','--quiet'], path)
            run(['remote','add','origin',URL], path)
            run(['fetch','--depth=1','origin',args.ref], path)
            run(['checkout','--detach','FETCH_HEAD'], path)
            commit=run(['rev-parse','HEAD'], path)
            dirty=run(['status','--porcelain','--untracked-files=no'], path)
            if dirty:
                raise RuntimeError('fetched checkout unexpectedly has tracked changes')
            hashes={f:hashlib.sha256((path/f).read_bytes()).hexdigest() for f in FILES}
            pin={'repository': URL, 'requested_ref':args.ref, 'commit':commit,
                 'fetched_at_utc':datetime.now(timezone.utc).isoformat(),
                 'tracked_worktree_checked_out':True, 'submodules_initialized':False, 'git_history_shallow':True,
                 'selected_file_sha256':hashes,
                 'validation':'Downloaded and hashed only; no compatibility tests run by this script.'}
            # Preserve complete tree (including shallow git metadata) at requested path.
            shutil.copytree(path,dest)
            args.manifest.parent.mkdir(parents=True,exist_ok=True)
            with args.manifest.open('x', encoding='utf-8') as out:
                json.dump(pin,out,indent=2);out.write('\n')
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as exc:
        detail=getattr(exc,'stderr',None) or str(exc)
        print('Fetch/pin failed; no verified compatibility baseline: '+detail, file=sys.stderr)
        return 1
    print(f'Fetched {commit} into {dest}; pin: {args.manifest}')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
