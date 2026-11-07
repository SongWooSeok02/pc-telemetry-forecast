"""Real TLS API lifecycle with client-scoped trust; never changes OS trust settings."""
import json
import os
from pathlib import Path
import socket
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('pwsh') and os.environ.get('TEST_DATABASE_URL'), 'PowerShell and PostgreSQL required')
class HTTPSRegistration(unittest.TestCase):
    def test_verified_tls_lifecycle(self):
        """Verified HTTPS enrolls/uploads/rotates/revokes/deletes; untrusted TLS and code reuse fail."""
        schema = 'test_live_' + uuid.uuid4().hex
        helper = str(ROOT/'tests/live_registration_server.py')
        process = None
        with tempfile.TemporaryDirectory(prefix='s203-tls-') as folder:
            folder = Path(folder)
            subprocess.run(['pwsh','-NoProfile','-File',str(ROOT/'tests/create_test_certificate.ps1'),
                '-Directory',str(folder)], check=True, capture_output=True)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0))
                port = listener.getsockname()[1]
            origin = f'https://127.0.0.1:{port}'
            try:
                with (folder/'server.log').open('w',encoding='utf-8') as output:
                    process = subprocess.Popen([sys.executable,helper,'--schema',schema,'--port',str(port),
                        '--cert',str(folder/'server.pem'),'--key',str(folder/'server.key')],
                        stdout=output,stderr=output)
                    context = ssl.create_default_context(cafile=str(folder/'ca.pem'))
                    with httpx.Client(base_url=origin,verify=context,trust_env=False,timeout=5) as client:
                        ready = False
                        deadline = time.monotonic()+15
                        while time.monotonic() < deadline:
                            if process.poll() is not None:
                                self.fail('TLS helper exited early (no secrets emitted)')
                            try:
                                ready = client.get('/health/partitions').status_code == 200
                                if ready:
                                    break
                            except httpx.ConnectError:
                                time.sleep(.1)
                        self.assertTrue(ready)
                        with httpx.Client(verify=True,trust_env=False,timeout=3) as untrusted:
                            with self.assertRaises(httpx.ConnectError):
                                untrusted.get(origin+'/health/partitions')
                        headers={'Authorization':'Bearer synthetic-live-'+schema}
                        issued=client.post('/v1/registration-codes',headers=headers)
                        self.assertEqual(issued.status_code,200)
                        self.assertEqual(issued.headers['cache-control'],'no-store')
                        code=issued.json()['registration_code']
                        if os.name == 'nt':
                            probe=subprocess.run(['pwsh','-NoProfile','-File',str(ROOT/'tests/probe_untrusted_registration.ps1'),
                                '-Server',origin+'/'],input=code+'\n',text=True,capture_output=True,timeout=30)
                            self.assertEqual(probe.returncode,0)
                            self.assertIn('rejects untrusted TLS',probe.stdout)
                            self.assertNotIn(code,probe.stdout+probe.stderr)
                        # Same code still works: native service's TLS rejection occurred before consumption.
                        registered=client.post('/v1/device/registrations',json={'registration_code':code})
                        self.assertEqual(registered.status_code,200)
                        self.assertEqual(registered.headers['cache-control'],'no-store')
                        device=registered.json(); machine=device['machine_id']; old=device['device_token']
                        batch=json.loads(subprocess.check_output([sys.executable,helper,'--schema',schema,
                            '--batch',machine],text=True))
                        def send(token):
                            return client.post('/v1/telemetry/batches',content=batch['body'],headers={
                                'Authorization':'Bearer '+token,'X-Content-SHA256':batch['sha256'],
                                'Content-Type':'application/json'})
                        self.assertEqual(send(old).status_code,200)
                        replacement=client.post(f'/v1/machines/{machine}/reissue',headers=headers)
                        self.assertEqual(replacement.status_code,200)
                        next_code=replacement.json()['registration_code']
                        rotated=client.post('/v1/device/registrations',json={'registration_code':next_code})
                        self.assertEqual(rotated.status_code,200)
                        self.assertEqual(rotated.json()['machine_id'],machine)
                        token=rotated.json()['device_token']
                        self.assertEqual(send(old).status_code,401)
                        self.assertEqual(send(token).status_code,200)
                        self.assertEqual(client.post(f'/v1/machines/{machine}/revoke',headers=headers).status_code,200)
                        self.assertEqual(send(token).status_code,401)
                        self.assertEqual(client.post(f'/v1/machines/{machine}/delete',headers=headers).status_code,200)
                        self.assertEqual(send(token).status_code,401)
                        self.assertEqual(client.post('/v1/device/registrations',json={'registration_code':code}).status_code,401)
                        self.assertEqual(client.post('/v1/device/registrations',json={'registration_code':next_code}).status_code,401)
                        output.flush()
                        logs=(folder/'server.log').read_text(encoding='utf-8')
                        for secret in (old,token,code,next_code,headers['Authorization']):
                            self.assertNotIn(secret,logs)
            finally:
                if process and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                subprocess.run([sys.executable,helper,'--schema',schema,'--cleanup'],
                    check=True,capture_output=True)
