"""Read-only Docker host probe. Executed unchanged over SSH; stdlib only.

Never writes on the source, execs a container, or reads volume data/htpasswd.
The caller must keep this raw stdout private until normalization/masking.
"""
import base64
import json
import hashlib
import subprocess
import sys
from pathlib import Path

SUDO_PASSWORD = None


def command(args, cwd=None):
    password = None
    if len(sys.argv) > 3 and sys.argv[3].startswith('sudo'):
        if SUDO_PASSWORD is not None:
            args = ['sudo', '-S', '-p', '', *args]
            password = SUDO_PASSWORD + '\n'
        else:
            args = ['sudo', '-n', *args]
    result = subprocess.run(args, input=password, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError('Read-only Docker probe failed: ' + ' '.join(args[:3]))
    return result.stdout


def probe(compose_file, project):
    path = Path(compose_file)
    if not path.is_absolute():
        raise ValueError('Source Compose file must be absolute')
    args = ['docker', 'compose', '-f', str(path)]
    if project:
        args += ['-p', project]
    compose = json.loads(command(args + ['config', '--format', 'json'], cwd=path.parent))
    project = project or compose.get('name')
    ids = command(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=' + project]).split()
    if not ids:
        raise ValueError('No running containers for source Compose project')
    containers = sorted(json.loads(command(['docker', 'inspect', *ids])), key=lambda c: c['Config']['Labels']['com.docker.compose.service'])
    services, assets, total = {}, [], 0
    for container in containers:
        service = container['Config']['Labels']['com.docker.compose.service']
        if service not in compose['services']:
            continue
        if service in services:
            raise ValueError('Scaled services are not supported')
        images = json.loads(command(['docker', 'image', 'inspect', container['Image']]))
        image_env = dict(v.split('=', 1) for v in (images[0].get('Config', {}).get('Env') or []))
        actual_env = dict(v.split('=', 1) for v in (container['Config'].get('Env') or []))
        declared = compose['services'][service].get('environment') or {}
        services[service] = {
            'environment': {k: v for k, v in actual_env.items() if k in declared or image_env.get(k) != v},
            'image': container['Config']['Image'], 'imageId': container['Image'],
            'digests': images[0].get('RepoDigests', []),
            'entrypoint': container['Config'].get('Entrypoint'), 'command': container['Config'].get('Cmd'),
            'restart': container['HostConfig']['RestartPolicy'],
            'memory': container['HostConfig'].get('Memory', 0),
            'nanoCpus': container['HostConfig'].get('NanoCpus', 0),
            'ports': container['HostConfig'].get('PortBindings', {}),
            'mounts': sorted(container.get('Mounts', []), key=lambda m: m['Destination'])}
        for mount in services[service]['mounts']:
            if mount['Type'] != 'bind':
                continue
            source, target = Path(mount['Source']), mount['Destination']
            if 'nifi' in target and (target.endswith('/conf') or target.endswith('/nifi.properties')):
                raise ValueError('Bind-mounted NiFi conf requires an explicit migration')
            prefix = f'assets/{service}/mount-' + hashlib.sha256(target.encode()).hexdigest()[:12]
            is_data = any(x in target for x in ('_repository', '/flow_storage', '/database', '/state', '/logs', '/ex_data'))
            if source.name == '.htpasswd' or target.endswith('/.htpasswd'):
                assets.append({'service': service, 'target': target, 'path': prefix + '/.htpasswd', 'kind': 'gateway-auth'})
                continue
            if is_data:
                assets.append({'service': service, 'target': target, 'path': prefix, 'kind': 'empty-directory'})
                continue
            if source.is_symlink() or not source.exists():
                raise ValueError('Unsupported or missing bind mount')
            directory = source.is_dir()
            assets.append({'service': service, 'target': target, 'path': prefix if directory else prefix + '/' + source.name, 'kind': 'directory' if directory else 'file'})
            files = sorted(source.rglob('*')) if directory else [source]
            for file in files:
                if file.is_symlink():
                    raise ValueError('Symlink assets require explicit packaging')
                if not file.is_file():
                    continue
                relative = str(file.relative_to(source)) if directory else source.name
                destination = prefix + '/' + relative
                if file.suffix.lower() in ('.key', '.pem', '.p12', '.pfx', '.jks', '.keystore') or file.name in ('id_rsa', 'id_ed25519', 'id_ecdsa', 'id_dsa'):
                    assets.append({'path': destination, 'kind': 'private-file'})
                    continue
                if file.stat().st_size > 64 * 1024 * 1024:
                    raise ValueError('Asset exceeds 64 MiB capture limit')
                data = file.read_bytes()
                total += len(data)
                if total > 128 * 1024 * 1024 or len(assets) > 5000:
                    raise ValueError('Asset capture exceeds package limit')
                assets.append({'path': destination, 'kind': 'content', 'mode': file.stat().st_mode & 0o777,
                               'data': base64.b64encode(data).decode()})
    return {'compose': compose, 'services': services, 'assets': assets}


if __name__ == '__main__':
    try:
        if len(sys.argv) > 3 and sys.argv[3] == 'sudo-password':
            SUDO_PASSWORD = json.load(sys.stdin)['sudoPassword']
        print(json.dumps(probe(sys.argv[1], sys.argv[2])))
    except (ValueError, RuntimeError, OSError, KeyError):
        print('Read-only Docker capture failed; check source project, Docker permissions and mounts', file=sys.stderr)
        sys.exit(1)
