#!/usr/bin/env python3
"""Trusted default-branch CI entry point. Credentials are environment only."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nifi_deploy.api import Client
from nifi_deploy.bundle import read_json
from nifi_deploy.configuration import export_deployment
from nifi_deploy.docker_export import capture_docker


def main():
    sources = read_json('sources/hosts.json')
    selected = os.environ.get('SELECTED_SOURCE', 'both')
    if selected != 'both' and selected not in sources:
        raise ValueError('Unknown source selection')
    for name, source in sources.items():
        if selected not in ('both', name):
            continue
        number = name.removeprefix('node')
        password = os.environ.get('NIFI_SOURCE_' + number + '_SSH_PASSWORD', '')
        if password:
            os.environ['NIFI_SOURCE_SSH_PASSWORD'] = password
        else:
            os.environ.pop('NIFI_SOURCE_SSH_PASSWORD', None)
        raw = capture_docker(source['dockerHost'], source['composeFile'], source.get('composeProject', ''),
                             identity=os.environ.get('NIFI_SOURCE_SSH_IDENTITY') or None,
                             known_hosts='sources/known_hosts', sudo=source.get('dockerSudo', False))
        client = Client(os.environ.get('SOURCE_' + number + '_URL') or source['url'],
                        os.environ['NIFI_SOURCE_USERNAME'], os.environ['NIFI_SOURCE_PASSWORD'])
        config = export_deployment(client, raw, 'bundles/' + name, name,
                                   nifi_service=source.get('nifiService', 'nifi'), gateway_service=source.get('gatewayService', 'nginx'))
        print(json.dumps({'name': name, 'counts': config['flow']['counts'], 'assets': len(config['assets']), 'schemaVersion': 2}))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
