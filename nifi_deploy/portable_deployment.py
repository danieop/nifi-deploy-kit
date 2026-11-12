"""Deploy captured Compose and one customer-editable config.json."""
import copy
import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

from .api import Client, api_path
from .bundle import digest, read_json, write_json
from .configuration import component_key, configured_snapshot, safe_path, unresolved, validate_config
from .deployment import (enable_referenced_services, import_payload, run, select_source, status, wait_for_validation)
from .docker_export import SECRET


def literal_compose(value):
    # Compose interpolates dollars even in JSON. Values from API/admin are literal.
    if isinstance(value, str):
        return value.replace('$', '$$')
    if isinstance(value, list):
        return [literal_compose(v) for v in value]
    if isinstance(value, dict):
        return {k: literal_compose(v) for k, v in value.items()}
    return value


def apply_live_config(client, root_id, source, config):
    """Map native source identifiers to imported IDs; update only changed values."""
    overrides = {c['sourceId']: c for c in config.get('components', [])}
    def visit(gid, group):
        flow = client.get('/flow/process-groups/' + api_path(gid))['processGroupFlow']['flow']
        for key, resource in (('processors', 'processors'), ('controllerServices', 'controller-services')):
            entities = flow.get(key, []) if key == 'processors' else client.get('/flow/process-groups/' + gid + '/controller-services').get('controllerServices', [])
            for entity in entities:
                comp = entity['component']
                if key == 'controllerServices' and comp.get('parentGroupId') != gid:
                    continue
                original = select_source(comp, group.get(key, []))
                override = overrides.get(component_key(original))
                if not override:
                    continue
                existing = comp.get('config', {}).get('properties', {}) if key == 'processors' else comp.get('properties', {})
                changed = {k: v for k, v in override['properties'].items() if v != SECRET and existing.get(k) != v}
                if not changed:
                    continue
                if comp.get('state') not in ('STOPPED', 'DISABLED'):
                    raise ValueError('Stop/disable changed target components before updating configuration: ' + override['name'])
                current = client.get('/' + resource + '/' + entity['id'])
                change = {'config': {'properties': changed}} if key == 'processors' else {'properties': changed}
                client.request('PUT', '/' + resource + '/' + entity['id'],
                               {'revision': current['revision'], 'component': {'id': entity['id'], **change}})
        for child in flow.get('processGroups', []):
            visit(child['id'], select_source(child['component'], group.get('processGroups', [])))
    visit(root_id, source['flowContents'])
    # Avoid unnecessary async updates on a complete unmodified parameter context.
    known = {}
    for entity in client.get('/flow/parameter-contexts').get('parameterContexts', []):
        known.setdefault(entity['component']['name'], []).append(entity['id'])
    from .deployment import apply_parameters
    changes = {}
    for name, values in config.get('parameters', {}).items():
        ids = known.get(name, [])
        if len(ids) != 1:
            raise ValueError('Parameter context is missing or ambiguous: ' + name)
        entity = client.get('/parameter-contexts/' + ids[0])
        existing = {p['parameter']['name']: p['parameter'].get('value') for p in entity['component'].get('parameters', [])}
        changed = {k: v for k, v in values.items() if v != SECRET and v is not None and existing.get(k) != str(v)}
        if changed:
            changes[name] = changed
    apply_parameters(client, changes)


def materialize(config, root, target):
    for asset in config.get('assets', []):
        source, destination = safe_path(root, asset['file']), safe_path(target, asset['file'])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists() or digest(destination) != asset['sha256']:
            shutil.copyfile(source, destination)
            destination.chmod(asset.get('mode', 0o644))
    for relative, source_file in config.get('localFiles', {}).items():
        destination = safe_path(target, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = Path(source_file).expanduser()
        if not source.is_absolute():
            source = root / source
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
    compose = copy.deepcopy(config['docker']['compose'])
    for service in compose['services'].values():
        for mount in service.get('volumes', []):
            if mount.get('type') == 'bind' and not Path(mount['source']).is_absolute():
                destination = safe_path(target, mount['source'])
                if not destination.exists():
                    # Empty bind-mounted application data directories have no asset file.
                    destination.mkdir(parents=True, exist_ok=True)
                mount['source'] = str(destination)
    return compose


def resume_saved_stack(target, project, services):
    """Start only the saved managed stack before checking an overwrite target.

    Never materialize the incoming config or recreate existing containers here.
    The caller still checks running components, active tasks and queued data.
    """
    path = target / 'compose.json'
    if not path.is_file():
        return False
    saved = read_json(path)
    if any(name not in saved['services'] for name in services):
        raise ValueError('Restore the managed service names before redeploying')
    cli = ['docker', 'compose', '--project-name', project, '--file', str(path)]
    ids = run(cli + ['ps', '-a', '-q', *services]).split()
    running = [run(['docker', 'inspect', '--format', '{{.State.Running}}', cid]).strip() == 'true' for cid in ids]
    if len(ids) == len(services) and all(running):
        return False
    run(cli + ['up', '-d', '--no-recreate'])
    return True


def deploy_config(config_file, overwrite=False, start=False, timeout=600):
    config_file = Path(config_file).resolve()
    config = validate_config(config_file)
    root = config_file.parent
    deployment = config['deployment']
    project = deployment['projectName']
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', project):
        raise ValueError('Invalid deployment.projectName')
    missing = unresolved(config)
    blocking = [p for p in missing if p.startswith(('docker/', 'localFiles/'))]
    auth = deployment['gatewayAuth']
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+', auth['username']) or not auth.get('password') or auth['password'] == SECRET or any(c in auth['password'] for c in '\r\n'):
        raise ValueError('Set deployment.gatewayAuth username/password in the local config.json')
    if blocking or (start and missing):
        raise ValueError('Replace required placeholders in config.json: ' + ', '.join(blocking or missing))
    url = urlparse(deployment['apiUrl'])
    if url.scheme != 'http' or url.hostname not in ('localhost', '127.0.0.1') or not url.port:
        raise ValueError('deployment.apiUrl must point to the local HTTP gateway, e.g. http://127.0.0.1:9080/nifi')
    directory = Path(deployment['directory']).expanduser()
    target = (root / directory).resolve() if not directory.is_absolute() else directory.resolve()
    if target == root or root.is_relative_to(target):
        raise ValueError('Deployment directory must not contain the input bundle')
    owner_path, state_path = target / 'owner.json', target / 'deployment.json'
    owner = read_json(owner_path) if owner_path.exists() else None
    state = read_json(state_path) if state_path.exists() else None
    if target.exists() and any(target.iterdir()) and not owner:
        raise ValueError('Deployment directory is nonempty and is not managed by this kit')
    if owner and (owner.get('project') != project or owner.get('name') != config['name']):
        raise ValueError('Deployment directory is owned by another project/application')
    if state and state['nifiVersion'] != config['nifiVersion']:
        raise ValueError('NiFi version changes require a separate migration')
    if state and state['flowSha256'] != config['flow']['sha256'] and not overwrite:
        raise ValueError('Flow changed; use --overwrite-config on a stopped, drained target')
    run(['docker', 'compose', 'version'])
    existing = run(['docker', 'ps', '-a', '--filter', 'label=com.docker.compose.project=' + project, '--format', '{{.ID}}'])
    if existing.strip() and not owner:
        raise ValueError('Compose project already exists without this deployment state')
    # Check the running target BEFORE any file updates/container recreation.
    # Retain previous gateway access privately so auth/port changes can be checked.
    if state and overwrite:
        access_path = target / 'access.json'
        access = read_json(access_path) if access_path.exists() else {'apiUrl': deployment['apiUrl'], 'username': auth['username'], 'password': auth['password']}
        previous = Client(access['apiUrl'], access['username'], access['password'])
        if resume_saved_stack(target, project, [config['docker']['nifiService'], config['docker']['gatewayService']]):
            previous.wait_ready(timeout)
        before = status(previous, state['groupId'])
        current = previous.get('/process-groups/' + state['groupId'])
        if current.get('runningCount', 0) or before.get('flowFilesQueued', 0) or before.get('activeThreadCount', 0):
            raise ValueError('Stop and drain the target before replacing configuration')
    # Compute/validate the Docker model before materializing files. An unapproved
    # Docker change must not touch the active mount contents or gateway password.
    preview = copy.deepcopy(config['docker']['compose'])
    for service in preview['services'].values():
        for mount in service.get('volumes', []):
            if mount.get('type') == 'bind' and not Path(mount['source']).is_absolute():
                mount['source'] = str(safe_path(target, mount['source']))
    compose_content = literal_compose(preview)
    compose_hash = hashlib.sha256(json.dumps(compose_content, sort_keys=True).encode()).hexdigest()
    settings_hash = hashlib.sha256(json.dumps({k: config.get(k, {}) for k in ('parameters', 'components')}, sort_keys=True).encode()).hexdigest()
    assets_hash = hashlib.sha256(json.dumps(config.get('assets', []), sort_keys=True).encode()).hexdigest()
    if state and (target / 'compose.json').exists():
        old_compose = read_json(target / 'compose.json')
        role = config['docker']['nifiService']
        if old_compose['services'][role]['image'] != compose_content['services'][role]['image']:
            raise ValueError('Changing the NiFi image requires a separate migration')
    if state and state.get('assetsSha256', assets_hash) != assets_hash and not overwrite:
        raise ValueError('Mounted assets changed; use --overwrite-config on a stopped, drained target')
    if state and state.get('composeSha256') != compose_hash and not overwrite:
        raise ValueError('Docker config changed; use --overwrite-config to recreate the target containers')
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json(owner_path, {'project': project, 'name': config['name']}, private=True)
    auth_path = safe_path(target, auth['file'])
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    htpasswd = run(['docker', 'run', '--rm', '-i', 'httpd:2.4-alpine', 'htpasswd', '-niB', auth['username']], data=auth['password'] + '\n')
    auth_path.write_text(htpasswd)
    auth_path.chmod(0o644)
    materialize(config, root, target)
    compose_path = target / 'compose.json'
    write_json(compose_path, compose_content, private=True)
    cli = ['docker', 'compose', '--project-name', project, '--file', str(compose_path)]
    run(cli + ['config', '--quiet'])
    run(cli + ['up', '-d'])
    client = Client(deployment['apiUrl'], auth['username'], auth['password'])
    about = client.wait_ready(timeout)
    if about['version'] != config['nifiVersion']:
        raise ValueError('Target NiFi version differs from the exported flow')
    write_json(target / 'access.json', {'apiUrl': deployment['apiUrl'], 'username': auth['username'], 'password': auth['password']}, private=True)
    root_id = client.root_id()
    source = read_json(safe_path(root, config['flow']['file']))
    snapshot = configured_snapshot(source, config)
    snapshot['flowContents']['name'] = 'nifi-deploy-kit-' + config['name']
    fresh = not state
    if state:
        group_id = state['groupId']
        current = client.get('/process-groups/' + group_id)
        if overwrite:
            before = status(client, group_id)
            if current.get('runningCount', 0) or before.get('flowFilesQueued', 0) or before.get('activeThreadCount', 0):
                raise ValueError('Stop and drain the target before replacing configuration')
            write_json(target / 'backup-flow.json', client.get(f'/process-groups/{group_id}/download?includeReferencedServices=true&includeComponentState=false'), private=True)
            result = client.request('POST', '/process-groups/' + group_id + '/replace-requests',
                                    {'processGroupRevision': current['revision'], 'versionedFlowSnapshot': snapshot})
            client.poll('/process-groups/replace-requests/' + result['request']['requestId'], timeout)
    else:
        # Recover an import interrupted before its deployment state was recorded.
        flow = client.get('/flow/process-groups/' + root_id)['processGroupFlow']['flow']
        matches = [g for g in flow.get('processGroups', []) if g['component'].get('name') == snapshot['flowContents']['name']]
        if len(matches) > 1:
            raise ValueError('Ambiguous existing managed groups')
        group_id = matches[0]['id'] if matches else client.request('POST', '/process-groups/' + root_id + '/process-groups/import', import_payload(snapshot))['id']
        fresh = not matches
        state = {'project': project, 'name': config['name'], 'nifiVersion': config['nifiVersion'], 'groupId': group_id,
                 'flowSha256': config['flow']['sha256'], 'composeSha256': compose_hash}
        write_json(state_path, state, private=True)
    if not fresh and not overwrite and state.get('settingsSha256') != settings_hash:
        apply_live_config(client, group_id, source, config)
    check = wait_for_validation(client, group_id, config['flow']['counts'], source, timeout)
    if start:
        if check['typeMismatches']:
            raise ValueError('Target component types/bundles differ from the source')
        enable_referenced_services(client, group_id)
        check = wait_for_validation(client, group_id, config['flow']['counts'], source, timeout)
        if check['invalidProcessors']:
            raise ValueError('Flow has invalid processors; inspect the target UI before starting')
        client.request('PUT', '/flow/process-groups/' + group_id, {'id': group_id, 'state': 'RUNNING'})
    # Readiness of auxiliary HTTP NiFi Registry is independent of NiFi readiness.
    registry_health = {}
    for name, service in compose_content['services'].items():
        if 'nifi-registry' not in service.get('image', '') or name.endswith('-storage-init'):
            continue
        port = str(service.get('environment', {}).get('NIFI_REGISTRY_WEB_HTTP_PORT', '9090'))
        deadline = time.monotonic() + 120
        while True:
            try:
                run(cli + ['exec', '-T', name, 'curl', '--fail', '--silent', 'http://' + (service.get('hostname') or name) + ':' + port + '/nifi-registry-api/buckets'])
                registry_health[name] = True
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Target Registry API did not become ready: ' + name)
                time.sleep(3)
    state.update(flowSha256=config['flow']['sha256'], composeSha256=compose_hash, settingsSha256=settings_hash, assetsSha256=assets_hash)
    write_json(state_path, state, private=True)
    report = {'project': project, 'bundle': config['name'], 'nifiVersion': about['version'],
              'url': deployment['apiUrl'], 'groupId': group_id, 'flowSha256': config['flow']['sha256'],
              'mode': 'running' if start or check['runningProcessors'] else 'staged',
              'registryHealth': registry_health, 'unresolvedSecrets': missing, 'configurationComplete': not missing and not check['invalidProcessors'] and not check['typeMismatches'], **check}
    write_json(target / 'report.json', report, private=True)
    return report
