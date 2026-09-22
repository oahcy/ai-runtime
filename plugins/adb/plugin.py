"""ADB subprocess tools. Commands are never executed through a host shell."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import math
import time
from ai_runtime.sdk import save_artifact


class Adb:
    def __init__(self, context):
        self.context=context
        self.executable=shutil.which('adb')
        if not self.executable:
            roots=[os.environ.get('ANDROID_SDK_ROOT'),os.environ.get('ANDROID_HOME')]
            if os.environ.get('LOCALAPPDATA'):
                roots.append(str(Path(os.environ['LOCALAPPDATA'])/'Android'/'Sdk'))
            for root in roots:
                if root:
                    candidate=Path(root)/'platform-tools'/('adb.exe' if os.name=='nt' else 'adb')
                    if candidate.is_file(): self.executable=str(candidate);break
        self.serial=None

    def _run(self, args, seconds=15, binary=False):
        if not self.executable: raise ValueError('ADB not found; configure adb_path or install Android platform-tools')
        if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or not .1<=seconds<=120:
            raise ValueError('timeout must be 0.1..120 seconds')
        if not isinstance(args,list) or not args or any(not isinstance(x,str) or '\x00' in x for x in args):
            raise ValueError('args must be a nonempty string array')
        limit=16*1024*1024 if binary else 1024*1024
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                result=subprocess.run([self.executable,*args],stdout=out,stderr=err,
                    timeout=seconds,shell=False,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            except subprocess.TimeoutExpired:
                raise TimeoutError('ADB client timed out and was stopped; remote effects may remain, inspect before retry') from None
            out.seek(0);raw=out.read(limit+1)
            err.seek(0);errors=err.read(65537)
        if binary:
            if result.returncode: raise RuntimeError('ADB failed: '+errors[:65536].decode('utf-8','replace'))
            if len(raw)>limit: raise ValueError('ADB screenshot exceeds 16 MiB')
            return raw
        return {'returncode':result.returncode,'stdout':raw[:limit].decode('utf-8','replace'),
                'stderr':errors[:65536].decode('utf-8','replace'),
                'truncated':len(raw)>limit or len(errors)>65536}

    def _devices(self):
        result=self._run(['devices','-l'])
        if result['returncode']: raise RuntimeError('ADB devices failed: '+result['stderr'])
        devices=[]
        for line in result['stdout'].splitlines():
            if not line.strip() or line.startswith(('List of devices','*')): continue
            parts=line.split()
            if len(parts)>=2:
                devices.append({'serial':parts[0],'state':parts[1],'details':' '.join(parts[2:])})
        return devices

    def _select(self, value=None):
        serial=value if value is not None else self.serial
        devices=self._devices()
        if serial is not None:
            if not isinstance(serial,str) or not serial: raise ValueError('serial must be a nonempty string')
            match=next((d for d in devices if d['serial']==serial),None)
            if not match or match['state']!='device':
                raise ValueError('Selected device is absent, offline or unauthorized')
            return serial
        ready=[d for d in devices if d['state']=='device']
        if len(ready)!=1: raise ValueError('Specify serial: expected exactly one ready device')
        return ready[0]['serial']

    def _diagnose(self, arguments):
        package = arguments.get('package')
        if not isinstance(package, str) or not re.fullmatch(r'[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+', package):
            raise ValueError('package must be an Android package name')
        seconds = arguments.get('timeout', 12)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not .1 <= seconds <= 120:
            raise ValueError('timeout must be 0.1..120 seconds')
        serial = arguments.get('serial', self.serial)
        if serial is not None and (not isinstance(serial, str) or not serial):
            raise ValueError('serial must be a nonempty string')
        deadline = time.monotonic() + seconds
        report = {'package': package, 'serial': serial, 'errors': [], 'findings': []}

        def run(label, args):
            remaining = deadline - time.monotonic()
            if remaining < .1:
                report['errors'].append({'step': label, 'error': 'budget_exhausted'})
                return None
            try:
                result = self._run(args, min(remaining, 5))
            except Exception as exc:
                report['errors'].append({'step': label, 'error': type(exc).__name__})
                return None
            if result['returncode']:
                report['errors'].append({'step': label, 'returncode': result['returncode']})
                return None
            if result['truncated']:
                report['errors'].append({'step': label, 'error': 'output_truncated'})
            return result['stdout']

        devices = run('devices', ['devices', '-l'])
        if devices is None:
            return report
        ready = [p[0] for line in devices.splitlines() if len(p := line.split()) >= 2 and p[1] == 'device']
        if serial is None and len(ready) == 1:
            serial = ready[0]
        if serial not in ready:
            report['errors'].append({'step': 'device_selection', 'error': 'Specify exactly one ready device'})
            return report
        report['serial'] = serial
        prefix = ['-s', serial]
        power = run('power', prefix + ['shell', 'dumpsys', 'power'])
        report['power'] = [x.strip() for x in (power or '').splitlines() if 'mWakefulness=' in x][:2]
        window = run('foreground', prefix + ['shell', 'dumpsys', 'window'])
        report['foreground'] = [x.strip()[:500] for x in (window or '').splitlines() if 'mCurrentFocus=' in x][:2]
        pids = run('pid', prefix + ['shell', 'pidof', package])
        pid = (pids or '').split()
        report['pids'] = [x for x in pid if x.isdigit()]
        if not report['pids']:
            report['findings'].append('No running process found for package')
            return report
        # Collect the exact package process, not unrelated system/app logs.
        logs = run('logcat', prefix + ['logcat', '-d', '--pid=' + ','.join(report['pids']), '-t', '2000'])
        candidates = []
        for line in (logs or '').splitlines():
            lower = line.lower()
            if any(word in lower for word in ('authorization', 'token', 'password', 'secret', 'signature', 'cookie', 'decrypt:', 'okhttp')):
                continue
            if any(word in lower for word in ('error', 'exception', 'fatal', 'failure', 'not found', 'debugger', 'loadmodule', 'game script started')):
                # Avoid leaking URL query credentials in diagnostic excerpts.
                line = re.sub(r'(https?://[^\s?#]+)[?#][^\s]*', r'\1?<redacted>', line)
                candidates.append(line[:500])
                if 'download failure' in lower and 'not found' in lower:
                    report['findings'].append('Resource download reported Not Found; verify deployment path and resource URL')
        report['logs'] = candidates[-120:]
        report['findings'] = list(dict.fromkeys(report['findings']))
        report['log_scope'] = 'last 2000 lines for selected package PIDs; filtered and best-effort redacted'
        self.context.logger.info('ADB diagnosis completed', device=serial, package=package,
                                 findings=len(report['findings']), errors=len(report['errors']))
        return report

    def call(self, action, arguments):
        a=dict(arguments)
        allowed={'configure':{'adb_path','serial'},'status':set(),'devices':set(),
            'diagnose':{'package','serial','timeout'},
            'shell':{'command','serial','timeout'},'screenshot':{'serial','timeout'},
            'logcat':{'lines','serial','timeout'},'start_app':{'component','serial','timeout'},
            'forward':{'local','remote','serial','timeout'},'run':{'args','serial','timeout'}}
        if action not in allowed or set(a)-allowed[action]: raise ValueError('Unknown action or parameters')
        if action=='diagnose': return self._diagnose(a)
        if action=='configure':
            candidate=self.executable
            if 'adb_path' in a:
                value=a['adb_path']
                if not isinstance(value,str) or not Path(value).is_absolute() or not Path(value).is_file():
                    raise ValueError('adb_path must be an absolute executable file')
                candidate=str(Path(value).resolve())
            if 'serial' in a and a['serial'] is not None and (not isinstance(a['serial'],str) or not a['serial']):
                raise ValueError('serial must be a nonempty string or null')
            self.executable=candidate
            self.serial=a.get('serial',self.serial)
            return {'adb_path':self.executable,'serial':self.serial}
        if action=='status': return {'adb_path':self.executable,'serial':self.serial}
        if action=='devices': return {'devices':self._devices()}
        seconds=a.get('timeout',15)
        if action=='shell':
            command=a.get('command')
            if not isinstance(command,str) or not command or '\x00' in command: raise ValueError('command must be a nonempty string')
            args=['shell',command]
        elif action=='screenshot': args=['exec-out','screencap','-p']
        elif action=='logcat':
            lines=a.get('lines',200)
            if type(lines) is not int or not 1<=lines<=2000: raise ValueError('lines must be 1..2000')
            args=['logcat','-d','-t',str(lines)]
        elif action=='start_app':
            component=a.get('component')
            if not isinstance(component,str) or not re.fullmatch(r'[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+',component): raise ValueError('component must be package/activity')
            args=['shell','am','start','-W','-n',component]
        elif action=='forward':
            local,remote=a.get('local'),a.get('remote')
            if not isinstance(local,str) or not re.fullmatch(r'tcp:[0-9]{1,5}',local) or not 0<=int(local[4:])<=65535: raise ValueError('local must be tcp:0..65535')
            if not isinstance(remote,str) or not re.fullmatch(r'(tcp:[0-9]{1,5}|localabstract:[A-Za-z0-9_.-]+)',remote): raise ValueError('remote must be tcp:port or localabstract:name')
            if remote.startswith('tcp:') and not 1<=int(remote[4:])<=65535: raise ValueError('remote port out of range')
            args=['forward',local,remote]
        else:
            args=a.get('args')
            if not isinstance(args,list) or not args or any(not isinstance(x,str) or '\x00' in x for x in args) or args[0].startswith('-'):
                raise ValueError('args must start with an ADB command, without global flags')
        serial=self._select(a.get('serial'))
        self.context.logger.info('Executing ADB operation',device=serial,action=action)
        result=self._run(['-s',serial,*args],seconds,binary=action=='screenshot')
        if action=='screenshot':
            if not result.startswith(b'\x89PNG\r\n\x1a\n'): raise ValueError('ADB did not return a PNG screenshot')
            artifact=save_artifact(result,'adb-screen.png','image/png')
            self.context.logger.info('ADB screenshot saved',device=serial,bytes=len(result))
            return {'serial':serial,'artifact':artifact}
        if result['returncode']:
            self.context.logger.warning('ADB returned nonzero exit code',device=serial,returncode=result['returncode'])
        return {'serial':serial,**result}

    def close(self):
        # Commands are synchronous; never kill a shared adb server or disconnect devices.
        pass


def create(context):
    return Adb(context)
