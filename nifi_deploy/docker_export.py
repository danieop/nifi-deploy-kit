import base64
import copy
import json
import hashlib
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from .bundle import digest

SECRET = '__REQUIRED_SECRET__'
PRIVATE_FILE = '__REQUIRED_LOCAL_FILE__'
SECRET_KEY = re.compile(r'password|passwd|secret|token|credential|private.?key|api.?key|sensitive.props.key', re.I)


def capture_docker(host, compose_file, project='', port=22, identity=None, known_hosts=None, sudo=False):
    """None host = local. SSH credentials are never added to exported metadata."""
    probe = Path(__file__).with_name('docker_probe.py')
    sudo_password = os.environ.get('NIFI_SOURCE_SUDO_PASSWORD', os.environ.get('NIFI_SOURCE_SSH_PASSWORD')) if sudo else None
    sudo_mode = 'sudo-password' if sudo_password is not None else ('sudo' if sudo else 'user')
    payload = json.dumps({'sudoPassword': sudo_password}) if sudo_password is not None else None
    if host:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+', host):
            raise ValueError('--docker-host must be user@hostname')
        args = ['ssh', '-p', str(port), '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=20']
        if identity:
            args += ['-i', str(identity), '-o', 'IdentitiesOnly=yes']
        if known_hosts:
            args += ['-o', 'UserKnownHostsFile=' + str(Path(known_hosts).resolve())]
        args += [host, shlex.join(['python3', '-c', probe.read_text(), compose_file, project, sudo_mode])]
    else:
        args = ['python3', str(probe), compose_file, project, sudo_mode]
    env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix='nifi-export-ssh-') as temp:
        if host and env.get('NIFI_SOURCE_SSH_PASSWORD'):
            helper = Path(temp) / 'askpass'
            helper.write_text('#!/usr/bin/env python3\nimport os\nprint(os.environ["NIFI_SOURCE_SSH_PASSWORD"])\n')
            helper.chmod(0o700)
            env.update(SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE='force', DISPLAY='nifi-export')
        elif host:
            args[1:1] = ['-o', 'BatchMode=yes']
        try:
            result = subprocess.run(args, input=payload, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise RuntimeError('Read-only Docker source capture timed out') from None
    if result.returncode:
        raise RuntimeError('Docker source capture failed; check SSH trust/auth, Compose file and Docker read permissions')
    return json.loads(result.stdout)


def mask_environment(compose):
    """Mask declared sensitive env/label keys, not arbitrary shell/script text."""
    for service in compose.get('services', {}).values():
        for field in ('environment', 'labels'):
            values = service.get(field) or {}
            if isinstance(values, list):
                values = dict(v.split('=', 1) if '=' in v else (v, None) for v in values)
                service[field] = values
            for key, value in values.items():
                if value not in (None, '') and SECRET_KEY.search(key) and not key.lower().endswith(('_username', '_user')):
                    values[key] = SECRET


def portable_docker(raw, output, nifi_service='nifi', gateway_service='nginx'):
    """Preserve the resolved source Compose model, replace host-specific storage."""
    output = Path(output)
    compose = copy.deepcopy(raw['compose'])
    compose.pop('name', None)
    for key in ('secrets', 'configs'):
        if compose.get(key):
            raise ValueError('Compose secrets/configs require explicit local-file support; cannot silently export')
    runtime = raw['services']
    if nifi_service not in runtime or gateway_service not in runtime:
        raise ValueError('NiFi and gateway services must both be running in the selected project')
    if set(compose['services']) != set(runtime):
        raise ValueError('All Compose services must be running for an unambiguous Docker export')
    mounts = {(a.get('service'), a.get('target')): a for a in raw['assets'] if a.get('target')}
    assets, private_files, auth_files = [], {}, []
    for asset in raw['assets']:
        if asset['kind'] == 'content':
            path = output / asset['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(asset['data']))
            path.chmod(asset['mode'])
            assets.append({'file': asset['path'], 'sha256': digest(path), 'mode': asset['mode']})
        elif asset['kind'] == 'private-file':
            private_files[asset['path']] = PRIVATE_FILE
        elif asset['kind'] == 'gateway-auth':
            auth_files.append(asset['path'])
    if len(auth_files) != 1:
        raise ValueError('Expected exactly one .htpasswd bind mount for the HTTP Basic Auth baseline')
    volumes = compose.setdefault('volumes', {})
    for name, service in compose['services'].items():
        actual = runtime[name]
        service.pop('container_name', None)
        # Actual container launch settings take precedence over stale compose definitions.
        service['image'] = actual['digests'][0] if actual['digests'] else actual['image']
        service['entrypoint'], service['command'] = actual['entrypoint'], actual['command']
        if 'environment' in actual:
            service['environment'] = actual['environment']
        restart = actual['restart']
        service['restart'] = (restart['Name'] or 'no') + (':' + str(restart['MaximumRetryCount']) if restart.get('MaximumRetryCount') else '')
        if actual.get('memory'):
            service['mem_limit'] = actual['memory']
        if actual.get('nanoCpus'):
            service['cpus'] = actual['nanoCpus'] / 1_000_000_000
        bindings = actual.get('ports') or {}
        service['ports'] = [{'target': int(container.split('/')[0]), 'protocol': container.split('/')[1],
                             'published': b['HostPort'], 'host_ip': b['HostIp'] or '0.0.0.0'}
                            for container, bound in bindings.items() for b in (bound or [])]
        normalized = []
        for mount in sorted(actual['mounts'], key=lambda m: m['Destination']):
            target = mount['Destination']
            if mount['Type'] == 'bind':
                asset = mounts[(name, target)]
                normalized.append({'type': 'bind', 'source': './' + asset['path'], 'target': target, 'read_only': not mount['RW']})
            elif mount['Type'] == 'volume':
                volume = f'{name}_storage_' + hashlib.sha256(target.encode()).hexdigest()[:12]
                volumes[volume] = {}
                normalized.append({'type': 'volume', 'source': volume, 'target': target, 'read_only': not mount['RW']})
            else:
                raise ValueError('Unsupported source mount type: ' + mount['Type'])
        service['volumes'] = normalized
    for network in compose.get('networks', {}).values():
        if not network.get('external'):
            network.pop('name', None)
        else:
            raise ValueError('External Docker networks require an explicit deployment model')
    # Remove resolved host volume names; every target project owns fresh storage.
    used = {m['source'] for s in compose['services'].values() for m in s['volumes'] if m['type'] == 'volume'}
    compose['volumes'] = {name: {} for name in sorted(used)}
    # Fresh Registry image volumes need ownership initialization on the target.
    for name, service in list(compose['services'].items()):
        if 'nifi-registry' not in service['image']:
            continue
        mounts = [m for m in service['volumes'] if m['type'] == 'volume']
        if mounts:
            init = name + '-storage-init'
            targets = [m['target'] for m in mounts]
            compose['services'][init] = {'image': service['image'], 'user': '0:0', 'restart': 'no',
                'entrypoint': ['/bin/sh', '-c'], 'command': ['chown -R nifi:nifi ' + ' '.join(shlex.quote(t) for t in targets)],
                'volumes': copy.deepcopy(mounts)}
            depends = service.setdefault('depends_on', {})
            if isinstance(depends, list):
                depends = {n: {'condition': 'service_started'} for n in depends}
                service['depends_on'] = depends
            depends[init] = {'condition': 'service_completed_successfully'}
    mask_environment(compose)
    gateway_ports = compose['services'][gateway_service].get('ports', [])
    if not gateway_ports:
        raise ValueError('Gateway must expose an HTTP port')
    assets.sort(key=lambda a: a['file'])
    return {'nifiService': nifi_service, 'gatewayService': gateway_service, 'compose': compose}, assets, private_files, auth_files[0], gateway_ports[0]['published']
