"""Regression for router -> tenant -> authenticated control bridge deadlock."""
import hashlib
import hmac
import json
import threading

from fastapi.testclient import TestClient
from app import channel_connections
from app.main import app


def test_forward_can_read_authenticated_controls_while_generation_is_leased(monkeypatch, tmp_path):
    tenant = tmp_path / 'tenants' / 'mermaid' / 'config'
    tenant.mkdir(parents=True)
    (tenant / 'client.json').write_text(json.dumps({
        'slug': 'mermaid', 'name': 'Mermaid', 'status': 'active',
        'channel_account_allowlist': {'mode': 'strict', 'zernio_accounts': ['account_mermaid']},
    }))
    tokens = tmp_path / 'tokens'
    tokens.mkdir()
    token = 'mermaid-test-token-32-characters-long'
    (tokens / 'mermaid').write_text(token)
    monkeypatch.setenv('NR3_DB_PATH', str(tmp_path / 'db.sqlite'))
    monkeypatch.setenv('NR3_TENANTS_CLIENT_DIR', str(tmp_path / 'tenants'))
    monkeypatch.setenv('NR3_TENANT_BRIDGE_TOKEN_DIR', str(tokens))
    monkeypatch.setenv('NR3_ICP_STATE_PATH', str(tmp_path / 'icp.json'))
    monkeypatch.setenv('ZERNIO_WEBHOOK_SECRET', 'test-secret')
    channel_connections.upsert_tenant_channel_connection(
        tenant_id='mermaid', status='connected', zernio_profile_id='profile_mermaid',
        zernio_account_id='account_mermaid', zernio_account_verified=True,
    )
    completed = threading.Event()
    result = {}
    threads = []

    async def tenant_callback(**kwargs):
        def read_controls():
            with TestClient(app) as bridge:
                result['response'] = bridge.get('/internal/tenants/mermaid/overrides', headers={
                    'Authorization': f'Bearer {token}', 'X-Tenant-Identity': 'mermaid',
                })
            completed.set()
        worker = threading.Thread(target=read_controls)
        threads.append(worker)
        worker.start()
        return (200, 'OK') if completed.wait(1.5) else (503, 'controls timed out')

    monkeypatch.setattr('app.routes.connect._forward_zernio_webhook_to_tenant', tenant_callback)
    body = json.dumps({'event': 'message.received', 'data': {'accountId': 'account_mermaid'}}).encode()
    signature = hmac.new(b'test-secret', body, hashlib.sha256).hexdigest()
    with TestClient(app) as router:
        response = router.post('/internal/api/zernio/webhook-router', content=body, headers={
            'X-Zernio-Signature': f'sha256={signature}', 'Content-Type': 'application/json',
        })
    for worker in threads:
        worker.join(3)
    assert response.status_code == 200, 'Tenant callback blocked behind the forwarding lifecycle lock'
    assert result['response'].status_code == 200
    assert result['response'].json()['tenant_id'] == 'mermaid'
