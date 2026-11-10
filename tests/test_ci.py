import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nifi_deploy.ci import credentials_from_json, inputs_from_env, mask_credentials, run_export


def inputs():
    return {'EXPORT_BUNDLE_NAME': 'customer-west-prod', 'EXPORT_NIFI_URL': 'http://new-source.example:9080/nifi',
            'EXPORT_DOCKER_HOST': 'operator@new-source.example', 'EXPORT_COMPOSE_FILE': '/srv/customer/compose.yml',
            'EXPORT_CREDENTIALS_JSON': json.dumps({'apiUsername': 'api-user', 'apiPassword': 'api-private',
                'sshPassword': 'ssh-private', 'knownHosts': 'new-source.example ssh-ed25519 AAAATEST'})}


class WorkflowExportTests(unittest.TestCase):
    def test_arbitrary_source_does_not_require_host_inventory_or_numbered_secret(self):
        env=inputs()
        o=inputs_from_env(env)
        self.assertEqual(o['name'],'customer-west-prod')
        self.assertEqual(o['host'],'operator@new-source.example')
        self.assertEqual(o['project'],'')
        self.assertEqual(o['port'],22)
        self.assertFalse(o['sudo'])

    def test_bundle_paths_ssh_options_and_invalid_urls_are_rejected_before_io(self):
        for key,value in [('EXPORT_BUNDLE_NAME','../escape'),('EXPORT_BUNDLE_NAME','UPPERCASE'),
                          ('EXPORT_DOCKER_HOST','-oProxyCommand=bad@host'),
                          ('EXPORT_NIFI_URL','http://user:password@host/nifi'),
                          ('EXPORT_NIFI_URL','http://host:99999/nifi'),
                          ('EXPORT_NIFI_URL','http://host/nifi?token=bad'),
                          ('EXPORT_COMPOSE_FILE','relative/compose.yml'),
                          ('EXPORT_SSH_PORT','0'),('EXPORT_SSH_PORT','x'),('EXPORT_DOCKER_SUDO','yes')]:
            with self.subTest(key=key,value=value), patch('nifi_deploy.ci.capture_docker') as capture:
                env=inputs();env[key]=value
                with self.assertRaises(ValueError):run_export(env)
                capture.assert_not_called()

    def test_bad_credential_secret_reports_no_values(self):
        for raw in ('', '{"apiPassword":"private-fragment"', '[]', '{"unknown":"private-fragment"}',
                    '{"knownHosts":3}', '{"knownHosts":""}',
                    '{"knownHosts":"host key", "apiPassword":"__REQUIRED_SECRET__"}'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as error:credentials_from_json(raw)
                self.assertNotIn('private-fragment',str(error.exception))

    def test_export_passes_each_input_and_private_profile_and_outputs_only_one_bundle(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'GITHUB_ACTIONS':'false','NIFI_SOURCE_SUDO_PASSWORD':'stale'}):
            env=inputs();output=Path(temp)/'outputs'
            env.update(EXPORT_COMPOSE_PROJECT='custom-project',EXPORT_SSH_PORT='2222',EXPORT_DOCKER_SUDO='true',
                       EXPORT_NIFI_SERVICE='flow-engine',EXPORT_GATEWAY_SERVICE='web',EXPORT_GROUP_ID='group-a',
                       GITHUB_OUTPUT=str(output),RUNNER_TEMP=temp)
            profile=json.loads(env['EXPORT_CREDENTIALS_JSON']);profile.update(sshPrivateKey='private-key',sudoPassword='sudo-private',apiCaPem='CA PEM')
            env['EXPORT_CREDENTIALS_JSON']=json.dumps(profile)
            captured_paths=[]
            def capture(host,compose,project,port,**kw):
                self.assertEqual((host,compose,project,port),('operator@new-source.example','/srv/customer/compose.yml','custom-project',2222))
                self.assertTrue(kw['sudo'])
                self.assertEqual(os.environ['NIFI_SOURCE_SSH_PASSWORD'],'ssh-private')
                self.assertEqual(os.environ['NIFI_SOURCE_SUDO_PASSWORD'],'sudo-private')
                for key in ('known_hosts','identity'):
                    path=Path(kw[key]);captured_paths.append(path)
                    self.assertEqual(path.stat().st_mode & 0o777,0o600)
                self.assertEqual(Path(kw['identity']).read_text(),'private-key\n')
                return {'docker':'raw'}
            def client(url,username,password,ca):
                self.assertEqual((url,username,password),('http://new-source.example:9080/nifi','api-user','api-private'))
                captured_paths.append(Path(ca));self.assertEqual(Path(ca).read_text(),'CA PEM\n')
                return 'client'
            def export(client,raw,bundle,name,group,nifi,gateway):
                self.assertEqual((client,raw,bundle,name,group,nifi,gateway),('client',{'docker':'raw'},Path('bundles/customer-west-prod'),'customer-west-prod','group-a','flow-engine','web'))
                return {'flow':{'counts':{'processors':2}},'assets':[]}
            log=io.StringIO()
            with patch('nifi_deploy.ci.capture_docker',side_effect=capture), patch('nifi_deploy.ci.Client',side_effect=client), \
                 patch('nifi_deploy.ci.export_deployment',side_effect=export), contextlib.redirect_stdout(log):
                result=run_export(env)
            self.assertEqual(result['bundlePath'],'bundles/customer-west-prod')
            self.assertEqual(output.read_text(),'bundle_name=customer-west-prod\nbundle_path=bundles/customer-west-prod\n')
            for path in captured_paths:self.assertFalse(path.exists())
            self.assertEqual(os.environ['NIFI_SOURCE_SUDO_PASSWORD'],'stale')
            for value in ('api-private','ssh-private','sudo-private','private-key','CA PEM'):
                self.assertNotIn(value,log.getvalue())

    def test_credentials_cleanup_and_environment_restore_on_source_failure(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{'GITHUB_ACTIONS':'false','NIFI_SOURCE_SUDO_PASSWORD':'stale'}):
            env=inputs();env['RUNNER_TEMP']=temp
            paths=[]
            def fail(*args,**kw):
                paths.append(Path(kw['known_hosts']))
                self.assertNotIn('NIFI_SOURCE_SUDO_PASSWORD',os.environ)
                raise RuntimeError('source unavailable')
            with patch('nifi_deploy.ci.capture_docker',side_effect=fail):
                with self.assertRaisesRegex(RuntimeError,'source unavailable'):run_export(env)
            self.assertEqual(list(Path(temp).iterdir()),[])
            self.assertFalse(paths[0].exists())
            self.assertEqual(os.environ['NIFI_SOURCE_SUDO_PASSWORD'],'stale')

    def test_multiline_secret_mask_escapes_workflow_commands(self):
        log=io.StringIO()
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'true'}),contextlib.redirect_stdout(log):
            mask_credentials({'sshPrivateKey':'line1\n::error::hidden%\rline2'})
        self.assertEqual(log.getvalue(),'::add-mask::line1%0A::error::hidden%25%0Dline2\n')


if __name__=='__main__':unittest.main()
