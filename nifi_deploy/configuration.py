"""Portable two-JSON bundle contract and customer-editable NiFi settings."""
import copy
import re
import shutil
import tempfile
from pathlib import Path

from .bundle import digest, groups, prepare_snapshot, read_json, summary, write_json
from .docker_export import SECRET, PRIVATE_FILE, portable_docker


def component_key(component):
    # Versioned identifiers can repeat across copied process groups.
    return component.get('instanceIdentifier') or component['identifier']


def contexts(snapshot):
    value = snapshot.get('parameterContexts', {})
    return list(value.values()) if isinstance(value, dict) else value


def editable_settings(snapshot):
    """Mask native secrets in BOTH outputs; keep references and wiring native."""
    parameters = {}
    sensitive_parameters = {}
    for context in contexts(snapshot):
        parameters[context['name']] = {}
        for parameter in context.get('parameters', []):
            if parameter.get('sensitive'):
                parameters[context['name']][parameter['name']] = SECRET
                sensitive_parameters.setdefault(context['name'], []).append(parameter['name'])
                parameter.pop('value', None)
            elif parameter.get('value') is not None:
                parameters[context['name']][parameter['name']] = parameter['value']
    components = []
    service_ids = {c['identifier'] for group in groups(snapshot['flowContents']) for c in group.get('controllerServices', [])}
    def visit(group, path):
        for key, kind in (('processors', 'processor'), ('controllerServices', 'controller-service')):
            for component in group.get(key, []):
                properties, sensitive = {}, []
                descriptors = component.get('propertyDescriptors') or {}
                for name, value in (component.get('properties') or {}).items():
                    descriptor = descriptors.get(name, {})
                    if descriptor.get('identifiesControllerService') or (isinstance(value, str) and value in service_ids):
                        continue  # NiFi remaps service IDs during native import.
                    if descriptor.get('sensitive'):
                        sensitive.append(name)
                    if descriptor.get('sensitive') and value is not None and '#{' not in str(value):
                        properties[name] = SECRET
                        component['properties'][name] = None
                    elif value is not None:
                        properties[name] = value
                    elif descriptor.get('sensitive'):
                        properties[name] = None
                if properties:
                    components.append({'sourceId': component_key(component), 'groups': path,
                                       'kind': kind, 'name': component.get('name'),
                                       'properties': properties, 'sensitiveProperties': sensitive})
        for child in group.get('processGroups', []):
            visit(child, path + [child.get('name', '')])
    visit(snapshot['flowContents'], [])
    return parameters, sensitive_parameters, components


def enrich_sensitive_metadata(client, snapshot):
    # Native download omits even the configured/not-configured distinction for
    # sensitive properties. GET the affected components to recover presence only.
    for group in groups(snapshot['flowContents']):
        for key, resource in (('processors', 'processors'), ('controllerServices', 'controller-services')):
            for component in group.get(key, []):
                descriptors = component.get('propertyDescriptors') or {}
                names = [name for name, d in descriptors.items() if d.get('sensitive')]
                if not names:
                    continue
                source_id = component.get('instanceIdentifier') or component['identifier']
                live = client.get('/' + resource + '/' + source_id)['component']
                values = live.get('config', {}).get('properties', {}) if key == 'processors' else live.get('properties', {})
                for name in names:
                    current = (component.get('properties') or {}).get(name)
                    if current is None and values.get(name) is not None:
                        component.setdefault('properties', {})[name] = SECRET
    return snapshot


def export_deployment(client, raw_docker, output, name, group_id=None, nifi_service='nifi', gateway_service='nginx'):
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()) and not any((output / p).exists() for p in ('config.json', 'release.json')):
        raise ValueError('Refusing to replace a nonempty unmanaged export directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.nifi-export-', dir=output.parent) as temp:
        stage, previous = Path(temp) / 'bundle', Path(temp) / 'previous'
        config = _export_deployment(client, raw_docker, stage, name, group_id, nifi_service, gateway_service)
        validate_config(stage / 'config.json')
        if output.exists():
            output.rename(previous)
        try:
            stage.rename(output)
        except OSError:
            if previous.exists():
                previous.rename(output)
            raise
        if previous.exists():
            shutil.rmtree(previous)
    return config


def _export_deployment(client, raw_docker, output, name, group_id=None, nifi_service='nifi', gateway_service='nginx'):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', name):
        raise ValueError('Invalid bundle name')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    snapshot = prepare_snapshot(enrich_sensitive_metadata(client, client.get(f'/process-groups/{group_id or client.root_id()}/download?includeReferencedServices=true&includeComponentState=false')))
    parameters, sensitive_parameters, components = editable_settings(snapshot)
    docker, assets, files, auth_file, port = portable_docker(raw_docker, output, nifi_service, gateway_service)
    write_json(output / 'flow.json', snapshot)
    about = client.get('/flow/about')['about']
    inventories = client.get('/controller/registry-clients').get('registries', [])
    config = {'schemaVersion': 2, 'name': name, 'nifiVersion': about['version'],
              'flow': {'file': 'flow.json', 'sha256': digest(output / 'flow.json'), **summary(snapshot)},
              'deployment': {'projectName': 'nifi-' + name, 'directory': './runtime/' + name,
                             'apiUrl': f'http://127.0.0.1:{port}/nifi',
                             'gatewayAuth': {'username': 'nifiadmin', 'password': SECRET, 'file': auth_file}},
              'docker': docker, 'assets': assets, 'localFiles': files,
              'parameters': parameters, 'sensitiveParameters': sensitive_parameters,
              'components': components,
              'registryClientsInventory': [{k: e.get('component', {}).get(k) for k in ('name', 'type', 'bundle')} for e in inventories],
              'exportNotes': ['Docker model captured from resolved Compose and running containers.',
                              'Host binds relocated under assets; source volumes become fresh project-owned volumes.',
                              'Source htpasswd and private key/certificate files are not copied.',
                              'Volume data and bind-mounted application data are not exported.',
                              'Native sensitive values and sensitive Docker environment/label keys are masked.',
                              'Secrets embedded inside scripts, URLs or ordinary strings require manual review.']}
    write_json(output / 'config.json', config)
    # Schema 2 replaces the old multi-file metadata, not the packaged native flow.
    for obsolete in ('release.json', 'parameters.required.json', 'registry-clients.inventory.json'):
        (output / obsolete).unlink(missing_ok=True)
    return config


def safe_path(root, relative):
    relative = Path(relative)
    path = (root / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError('Bundle path must stay inside the bundle directory')
    return path


def validate_config(path):
    path = Path(path).resolve()
    config = read_json(path)
    root = path.parent
    if config.get('schemaVersion') != 2:
        raise ValueError('Expected schemaVersion 2 config.json')
    flow = safe_path(root, config['flow']['file'])
    if digest(flow) != config['flow']['sha256']:
        raise ValueError('Flow checksum mismatch')
    snapshot = read_json(flow)
    if 'flowContents' not in snapshot or summary(snapshot) != {k: config['flow'][k] for k in ('counts', 'bundles')}:
        raise ValueError('Native flow definition/counts do not match config')
    source_components = {component_key(c): c for g in groups(snapshot['flowContents'])
                         for key in ('processors', 'controllerServices') for c in g.get(key, [])}
    seen = set()
    for override in config.get('components', []):
        sid = override['sourceId']
        if sid not in source_components or sid in seen:
            raise ValueError('Unknown or duplicate component sourceId in config')
        seen.add(sid)
        for value in override['properties'].values():
            if value is not None and not isinstance(value, str):
                raise ValueError('NiFi property overrides must be strings or null')
    known_contexts = {c['name']: {p['name'] for p in c.get('parameters', [])} for c in contexts(snapshot)}
    for name, parameters in config.get('parameters', {}).items():
        if name not in known_contexts or any(k not in known_contexts[name] for k in parameters):
            raise ValueError('Unknown parameter context/key in config')
        if any(not isinstance(v, str) for v in parameters.values()):
            raise ValueError('Parameter overrides must be strings; omit keys to preserve values')
    for asset in config.get('assets', []):
        if digest(safe_path(root, asset['file'])) != asset['sha256']:
            raise ValueError('Asset checksum mismatch: ' + asset['file'])
    services = config['docker']['compose']['services']
    for key in ('nifiService', 'gatewayService'):
        if config['docker'][key] not in services:
            raise ValueError('Missing configured Docker service')
    for path in config.get('localFiles', {}):
        safe_path(root, path)
    safe_path(root, config['deployment']['gatewayAuth']['file'])
    # Local bind paths are portable relative paths. Absolute customer paths are allowed,
    # but relative ones must remain inside the copied bundle/runtime root.
    for service in services.values():
        for mount in service.get('volumes', []):
            if not isinstance(mount, dict):
                raise ValueError('Use native Compose long syntax for mounts')
            if mount.get('type') == 'bind' and not Path(mount['source']).is_absolute():
                safe_path(root, mount['source'])
    return config


def unresolved(config):
    found = []
    def walk(value, path):
        if isinstance(value, str) and value in (SECRET, PRIVATE_FILE):
            found.append(path)
        elif isinstance(value, dict):
            for key, child in value.items():
                walk(child, path + '/' + str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + '/' + str(index))
    for key in ('docker', 'parameters', 'components', 'localFiles'):
        walk(config.get(key, {}), key)
    return found


def configured_snapshot(snapshot, config):
    """Apply non-secret and supplied-secret values before native import/replace.

    Native controller-service references remain untouched; NiFi remaps those IDs.
    Used for first import only. Redeploy updates use source ID -> target ID mapping.
    """
    snapshot = copy.deepcopy(snapshot)
    by_id = {component_key(c): c for g in groups(snapshot['flowContents'])
             for key in ('processors', 'controllerServices') for c in g.get(key, [])}
    for override in config.get('components', []):
        component = by_id.get(override['sourceId'])
        if component is None:
            raise ValueError('Unknown component sourceId in config')
        for name, value in override['properties'].items():
            if value != SECRET:
                component.setdefault('properties', {})[name] = value
    for context in contexts(snapshot):
        values = config.get('parameters', {}).get(context['name'], {})
        for parameter in context.get('parameters', []):
            if parameter['name'] in values and values[parameter['name']] != SECRET:
                parameter['value'] = str(values[parameter['name']])
    return snapshot
