import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nifi_deploy.bundle import read_json, write_json
from nifi_deploy.configuration import (editable_settings, configured_snapshot, enrich_sensitive_metadata,
    export_deployment, safe_path, unresolved, validate_config)
from nifi_deploy.docker_export import SECRET, PRIVATE_FILE, portable_docker
from nifi_deploy.portable_deployment import literal_compose, deploy_config, apply_live_config


SNAPSHOT = {
    'flowContents': {'identifier': 'root', 'name': 'root', 'processors': [
        {'identifier': 'processor', 'instanceIdentifier': 'source-runtime-p', 'name': 'HTTP', 'type': 'Test',
         'properties': {'URL': 'http://old:8080', 'Password': None, 'Optional Secret': None, 'Pool': 'service', 'Parameterized Secret': '#{Password}'},
         'propertyDescriptors': {'Password': {'sensitive': True}, 'Optional Secret': {'sensitive': True},
                                 'Pool': {'identifiesControllerService': True}, 'Parameterized Secret': {'sensitive': True}}}],
        'controllerServices': [{'identifier': 'service', 'name': 'pool', 'properties': {}}]},
    'parameterContexts': {'ctx': {'name': 'ctx', 'parameters': [
        {'name': 'Password', 'sensitive': True, 'value': 'DO_NOT_PUBLISH'},
        {'name': 'Host', 'sensitive': False, 'value': 'old-host'}]}}}


def raw_docker():
    return {'compose': {'name': 'source', 'services': {
        'nifi': {'image': 'stale:latest', 'environment': {'TOKEN': 'DO_NOT_PUBLISH', 'URL': 'http://source'}},
        'nginx': {'image': 'nginx:latest'}}, 'networks': {'default': {'name': 'source_default'}}},
        'services': {
            'nifi': {'image': 'nifi:2.12.0', 'digests': ['nifi@sha256:fixed'], 'entrypoint': ['start.sh'], 'command': None,
                     'restart': {'Name': 'unless-stopped'}, 'ports': {}, 'mounts': [
                         {'Type': 'volume', 'Destination': '/conf', 'RW': True},
                         {'Type': 'bind', 'Destination': '/scripts/start.sh', 'RW': False}], 'memory': 0, 'nanoCpus': 0},
            'nginx': {'image': 'nginx:1.27', 'digests': [], 'entrypoint': None, 'command': ['nginx'],
                      'restart': {'Name': 'always'}, 'ports': {'8080/tcp': [{'HostPort': '9080', 'HostIp': '0.0.0.0'}]},
                      'mounts': [{'Type': 'bind', 'Destination': '/etc/nginx/.htpasswd', 'RW': False}]}},
        'assets': [{'kind': 'file', 'service': 'nifi', 'target': '/scripts/start.sh', 'path': 'assets/start.sh'},
                   {'kind': 'content', 'path': 'assets/start.sh', 'data': 'IyEvYmluL3NoCg==', 'mode': 0o755},
                   {'kind': 'gateway-auth', 'service': 'nginx', 'target': '/etc/nginx/.htpasswd', 'path': 'assets/.htpasswd'}]}


class FakeExport:
    def root_id(self):
        return 'root'

    def get(self, path):
        if '/download?' in path:
            return copy.deepcopy(SNAPSHOT)
        if path == '/processors/source-runtime-p':
            return {'component': {'config': {'properties': {'Password': '********'}}}}
        if path == '/flow/about':
            return {'about': {'version': '2.12.0'}}
        return {'registries': []}


class PortableTests(unittest.TestCase):
    def test_native_download_null_recovers_presence_without_reading_secret(self):
        snapshot = enrich_sensitive_metadata(FakeExport(), copy.deepcopy(SNAPSHOT))
        params, sensitivity, components = editable_settings(snapshot)
        p = components[0]
        self.assertEqual(p['properties']['Password'], SECRET)
        self.assertIsNone(p['properties']['Optional Secret'])
        self.assertEqual(p['properties']['Parameterized Secret'], '#{Password}')
        self.assertNotIn('Pool', p['properties'])
        self.assertIsNone(snapshot['flowContents']['processors'][0]['properties']['Password'])
        self.assertEqual(params['ctx']['Password'], SECRET)
        self.assertNotIn('value', snapshot['parameterContexts']['ctx']['parameters'][0])

    def test_export_is_two_json_plus_assets_and_masks_both_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            config = export_deployment(FakeExport(), raw_docker(), temp, 'test')
            self.assertEqual(sorted(p.name for p in Path(temp).glob('*.json')), ['config.json', 'flow.json'])
            for path in Path(temp).glob('*.json'):
                self.assertNotIn('DO_NOT_PUBLISH', path.read_text())
            self.assertEqual(config['docker']['compose']['services']['nifi']['image'], 'nifi@sha256:fixed')
            self.assertEqual(config['docker']['compose']['services']['nifi']['entrypoint'], ['start.sh'])
            validate_config(Path(temp) / 'config.json')
            self.assertFalse((Path(temp) / 'assets/.htpasswd').exists())

    def test_actual_runtime_storage_and_ports_are_portable(self):
        with tempfile.TemporaryDirectory() as temp:
            docker, assets, files, auth, port = portable_docker(raw_docker(), temp)
            compose = docker['compose']
            self.assertNotIn('name', compose)
            self.assertNotIn('name', compose['networks']['default'])
            self.assertTrue(compose['services']['nifi']['volumes'][0]['source'].startswith('nifi_storage_'))
            self.assertEqual(compose['services']['nginx']['ports'][0]['published'], '9080')
            self.assertEqual(compose['services']['nifi']['environment']['TOKEN'], SECRET)
            self.assertEqual(compose['services']['nifi']['environment']['URL'], 'http://source')

    def test_source_mount_order_cannot_change_destination_volume_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            raw=raw_docker()
            raw['services']['nifi']['mounts'].append({'Type':'volume','Destination':'/content','RW':True})
            first=portable_docker(raw,temp)[0]
            raw['services']['nifi']['mounts'].reverse()
            second=portable_docker(raw,temp)[0]
            self.assertEqual(first,second)
            mounts=first['compose']['services']['nifi']['volumes']
            self.assertEqual(len({m['source'] for m in mounts if m['type']=='volume'}),2)

    def test_local_config_values_are_native_import_inputs(self):
        source = copy.deepcopy(SNAPSHOT)
        source['flowContents']['processors'][0]['properties']['Password'] = SECRET
        params, sensitive, components = editable_settings(source)
        components[0]['properties'].update(URL='http://new:9090', Password='customer-secret')
        params['ctx']['Password'] = 'customer-parameter'
        effective = configured_snapshot(source, {'parameters': params, 'components': components})
        props = effective['flowContents']['processors'][0]['properties']
        self.assertEqual(props['Password'], 'customer-secret')
        self.assertEqual(props['URL'], 'http://new:9090')
        self.assertEqual(props['Pool'], 'service')
        self.assertEqual(effective['parameterContexts']['ctx']['parameters'][0]['value'], 'customer-parameter')
        self.assertIsNone(source['flowContents']['processors'][0]['properties']['Password'])

    def test_dollar_values_remain_literal_in_compose(self):
        value = {'environment': {'PASSWORD': 'a${HOME}$!'}, 'command': ['echo $PATH']}
        self.assertEqual(literal_compose(value)['environment']['PASSWORD'], 'a$${HOME}$$!')
        self.assertEqual(value['command'], ['echo $PATH'])

    def test_paths_and_checksums_are_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for path in ('../outside', '/absolute'):
                with self.assertRaisesRegex(ValueError, 'inside'):
                    safe_path(root, path)
            export_deployment(FakeExport(), raw_docker(), temp, 'test')
            (root / 'assets/start.sh').write_text('tampered')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                validate_config(root / 'config.json')

    def test_unresolved_markers_are_paths_without_values(self):
        config = {'parameters': {'ctx': {'password': SECRET}}, 'components': [], 'docker': {},
                  'localFiles': {'assets/cert.key': PRIVATE_FILE}}
        self.assertEqual(unresolved(config), ['parameters/ctx/password', 'localFiles/assets/cert.key'])

    def test_gateway_placeholder_is_rejected_before_docker_or_files(self):
        with tempfile.TemporaryDirectory() as temp:
            export_deployment(FakeExport(), raw_docker(), temp, 'test')
            with patch('nifi_deploy.portable_deployment.run') as docker:
                with self.assertRaisesRegex(ValueError, 'gatewayAuth'):
                    deploy_config(Path(temp) / 'config.json')
                docker.assert_not_called()

    def test_start_rejects_native_placeholders_before_docker(self):
        with tempfile.TemporaryDirectory() as temp:
            config = export_deployment(FakeExport(), raw_docker(), temp, 'test')
            config['deployment']['gatewayAuth']['password'] = 'local-secret'
            config['docker']['compose']['services']['nifi']['environment']['TOKEN'] = 'local-token'
            write_json(Path(temp) / 'config.json', config)
            with patch('nifi_deploy.portable_deployment.run') as docker:
                with self.assertRaisesRegex(ValueError, 'Replace required placeholders'):
                    deploy_config(Path(temp) / 'config.json', start=True)
                docker.assert_not_called()

    def test_copied_groups_with_repeated_versioned_ids_have_distinct_overrides(self):
        snapshot=copy.deepcopy(SNAPSHOT)
        first=snapshot['flowContents']['processors'][0]
        second=copy.deepcopy(first)
        second['instanceIdentifier']='different-runtime-id'
        snapshot['flowContents']['processGroups']=[{'identifier':'child','name':'copy','processors':[second]}]
        params,sensitivity,components=editable_settings(snapshot)
        first_override=next(c for c in components if c['sourceId']=='source-runtime-p')
        second_override=next(c for c in components if c['sourceId']=='different-runtime-id')
        first_override['properties']['URL']='http://one'
        second_override['properties']['URL']='http://two'
        result=configured_snapshot(snapshot,{'parameters':params,'components':components})
        self.assertEqual(result['flowContents']['processors'][0]['properties']['URL'],'http://one')
        self.assertEqual(result['flowContents']['processGroups'][0]['processors'][0]['properties']['URL'],'http://two')

    def test_atomic_reexport_removes_stale_assets_and_retains_old_bundle_on_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'export'
            export_deployment(FakeExport(),raw_docker(),root,'test')
            (root/'assets/stale.txt').write_text('old')
            before=(root/'config.json').read_bytes()
            invalid=raw_docker();invalid['services'].pop('nginx')
            with self.assertRaises(ValueError):
                export_deployment(FakeExport(),invalid,root,'test')
            self.assertEqual((root/'config.json').read_bytes(),before)
            export_deployment(FakeExport(),raw_docker(),root,'test')
            self.assertFalse((root/'assets/stale.txt').exists())

    def test_invalid_parameter_and_source_id_fail_preflight(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            config=export_deployment(FakeExport(),raw_docker(),root/'export','test')
            config['parameters']['ctx']['Host']=None
            write_json(root/'export/config.json',config)
            with self.assertRaisesRegex(ValueError,'must be strings'):
                validate_config(root/'export/config.json')
            config['parameters']['ctx']['Host']='host'
            config['components'][0]['sourceId']='unknown'
            write_json(root/'export/config.json',config)
            with self.assertRaisesRegex(ValueError,'sourceId'):
                validate_config(root/'export/config.json')

    def test_docker_change_is_refused_without_touching_mounted_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            c=export_deployment(FakeExport(),raw_docker(),root/'bundle','test')
            target=root/'runtime';target.mkdir()
            c['deployment']['directory']=str(target)
            c['deployment']['gatewayAuth']['password']='local'
            c['docker']['compose']['services']['nifi']['environment']['TOKEN']='local'
            write_json(root/'bundle/config.json',c)
            write_json(target/'owner.json',{'project':c['deployment']['projectName'],'name':'test'})
            write_json(target/'deployment.json',{'project':c['deployment']['projectName'],'name':'test','nifiVersion':'2.12.0',
                'flowSha256':c['flow']['sha256'],'composeSha256':'old','groupId':'group'})
            old=target/'assets/start.sh';old.parent.mkdir();old.write_text('keep mounted file')
            with patch('nifi_deploy.portable_deployment.run',return_value='') as docker:
                with self.assertRaisesRegex(ValueError,'Docker config changed'):
                    deploy_config(root/'bundle/config.json')
                self.assertEqual(old.read_text(),'keep mounted file')
                self.assertEqual(docker.call_count,2)

    def test_overwrite_checks_queue_before_up_or_file_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            c=export_deployment(FakeExport(),raw_docker(),root/'bundle','test')
            target=root/'runtime';target.mkdir()
            c['deployment']['directory']=str(target)
            c['deployment']['gatewayAuth']['password']='local'
            c['docker']['compose']['services']['nifi']['environment']['TOKEN']='local'
            write_json(root/'bundle/config.json',c)
            write_json(target/'owner.json',{'project':c['deployment']['projectName'],'name':'test'})
            write_json(target/'deployment.json',{'nifiVersion':'2.12.0','flowSha256':c['flow']['sha256'],'groupId':'group'})
            old=target/'assets/start.sh';old.parent.mkdir();old.write_text('keep mounted file')
            with patch('nifi_deploy.portable_deployment.run',return_value='') as docker, \
                 patch('nifi_deploy.portable_deployment.Client') as client, \
                 patch('nifi_deploy.portable_deployment.status',return_value={'flowFilesQueued':1}):
                client.return_value.get.return_value={'runningCount':0}
                with self.assertRaisesRegex(ValueError,'Stop and drain'):
                    deploy_config(root/'bundle/config.json',overwrite=True)
                self.assertEqual(old.read_text(),'keep mounted file')
                self.assertEqual(docker.call_count,2)

    def test_source_id_mapping_does_not_depend_on_duplicate_names(self):
        source = {'flowContents': {'processors': [
            {'identifier': 'a', 'name': 'same'}, {'identifier': 'b', 'name': 'same'}]}}
        class Fake:
            payloads = []
            def get(self, path):
                if path == '/flow/process-groups/target':
                    return {'processGroupFlow': {'flow': {'processors': [
                        {'id': 'new-a', 'component': {'name': 'same', 'versionedComponentId': 'a', 'state': 'STOPPED', 'config': {'properties': {'URL': 'old'}}}},
                        {'id': 'new-b', 'component': {'name': 'same', 'versionedComponentId': 'b', 'state': 'STOPPED', 'config': {'properties': {'URL': 'old'}}}}]}}}
                if path.endswith('/controller-services'):
                    return {'controllerServices': []}
                if path == '/flow/parameter-contexts':
                    return {'parameterContexts': []}
                return {'revision': {'version': 2}}
            def request(self, method, path, payload):
                self.payloads.append((path,payload))
        client=Fake()
        apply_live_config(client,'target',source,{'components':[{'sourceId':'b','name':'same','properties':{'URL':'new'}}]})
        self.assertEqual(client.payloads[0][0], '/processors/new-b')
        self.assertEqual(client.payloads[0][1]['component']['config']['properties'], {'URL':'new'})


if __name__ == '__main__':
    unittest.main()
