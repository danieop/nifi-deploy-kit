"""Single-source workflow export. All server details come from dispatch inputs."""
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .api import Client
from .configuration import export_deployment
from .docker_export import capture_docker


CREDENTIAL_FIELDS = {'apiUsername', 'apiPassword', 'sshPassword', 'sshPrivateKey',
                     'sudoPassword', 'knownHosts', 'apiCaPem'}


def inputs_from_env(env):
    name = env.get('EXPORT_BUNDLE_NAME', '').strip()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', name):
        raise ValueError('bundle_name must contain lowercase letters, digits and hyphens (1-63 characters)')
    url = env.get('EXPORT_NIFI_URL', '').strip()
    try:
        parsed = urlsplit(url)
        valid_url = (parsed.scheme in ('http', 'https') and parsed.hostname and parsed.port != 0
                     and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
    except ValueError:
        valid_url = False
    if not valid_url or any(c.isspace() for c in url):
        raise ValueError('nifi_url must be HTTP(S), without embedded credentials, query or fragment')
    host = env.get('EXPORT_DOCKER_HOST', '').strip()
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*@[A-Za-z0-9][A-Za-z0-9_.-]*', host):
        raise ValueError('docker_host must be user@hostname or user@IPv4')
    compose = env.get('EXPORT_COMPOSE_FILE', '').strip()
    if not PurePosixPath(compose).is_absolute() or any(c in compose for c in '\r\n\0'):
        raise ValueError('compose_file must be an absolute path on the source')
    try:
        port = int(env.get('EXPORT_SSH_PORT', '22'))
    except ValueError:
        raise ValueError('ssh_port must be an integer between 1 and 65535') from None
    if not 1 <= port <= 65535:
        raise ValueError('ssh_port must be between 1 and 65535')
    sudo = env.get('EXPORT_DOCKER_SUDO', 'false').lower()
    if sudo not in ('true', 'false'):
        raise ValueError('docker_sudo must be true or false')
    return {'name': name, 'url': url, 'host': host, 'compose': compose, 'port': port,
            'sudo': sudo == 'true', 'project': env.get('EXPORT_COMPOSE_PROJECT', '').strip(),
            'group': env.get('EXPORT_GROUP_ID', '').strip() or None,
            'nifiService': env.get('EXPORT_NIFI_SERVICE', '').strip() or 'nifi',
            'gatewayService': env.get('EXPORT_GATEWAY_SERVICE', '').strip() or 'nginx'}


def credentials_from_json(raw):
    try:
        credentials = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('credentials_secret must name a GitHub Secret containing a JSON credential profile') from None
    if not isinstance(credentials, dict) or set(credentials) - CREDENTIAL_FIELDS:
        raise ValueError('Credential profile must be an object with documented credential fields only')
    if any(not isinstance(value, str) for value in credentials.values()):
        raise ValueError('Credential profile fields must be strings')
    if any(value in ('__REQUIRED_SECRET__', '__REQUIRED_LOCAL_FILE__') for value in credentials.values()):
        raise ValueError('Replace credential-profile placeholders before running the workflow')
    if not credentials.get('knownHosts', '').strip():
        raise ValueError('Credential profile requires verified knownHosts content')
    if bool(credentials.get('apiUsername')) != bool(credentials.get('apiPassword')):
        raise ValueError('Credential profile must supply both apiUsername/apiPassword or omit both')
    return credentials


def mask_credentials(credentials):
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        # GitHub masks the JSON secret as a whole; register each decoded value too.
        for value in credentials.values():
            if value:
                escaped = value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
                print('::add-mask::' + escaped, flush=True)


@contextmanager
def credential_environment(credentials):
    mapping = {'NIFI_SOURCE_SSH_PASSWORD': 'sshPassword', 'NIFI_SOURCE_SUDO_PASSWORD': 'sudoPassword'}
    original = {key: os.environ.get(key) for key in mapping}
    try:
        for key, field in mapping.items():
            if credentials.get(field):
                os.environ[key] = credentials[field]
            else:
                os.environ.pop(key, None)
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_export(env=None):
    env = os.environ if env is None else env
    options = inputs_from_env(env)
    credentials = credentials_from_json(env.get('EXPORT_CREDENTIALS_JSON', ''))
    mask_credentials(credentials)
    output = Path('bundles') / options['name']
    with tempfile.TemporaryDirectory(prefix='nifi-export-auth-', dir=env.get('RUNNER_TEMP') or None) as temp:
        directory = Path(temp)
        def private_file(name, content):
            path = directory / name
            path.write_text(content.rstrip('\n') + '\n')
            path.chmod(0o600)
            return str(path)
        known_hosts = private_file('known_hosts', credentials['knownHosts'])
        identity = private_file('identity', credentials['sshPrivateKey']) if credentials.get('sshPrivateKey') else None
        ca = private_file('api-ca.pem', credentials['apiCaPem']) if credentials.get('apiCaPem') else None
        with credential_environment(credentials):
            raw = capture_docker(options['host'], options['compose'], options['project'], options['port'],
                                 identity=identity, known_hosts=known_hosts, sudo=options['sudo'])
            client = Client(options['url'], credentials.get('apiUsername', ''), credentials.get('apiPassword', ''), ca)
            config = export_deployment(client, raw, output, options['name'], options['group'],
                                       options['nifiService'], options['gatewayService'])
    result = {'name': options['name'], 'bundlePath': str(output), 'schemaVersion': 2,
              'counts': config['flow']['counts'], 'assets': len(config['assets'])}
    if env.get('GITHUB_OUTPUT'):
        with Path(env['GITHUB_OUTPUT']).open('a') as stream:
            stream.write(f"bundle_name={options['name']}\nbundle_path={output.as_posix()}\n")
    print(json.dumps(result))
    return result
