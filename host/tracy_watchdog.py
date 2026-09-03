#!/usr/bin/env python3
"""Check TRACY's process and authenticated control callback; never send messages."""
import fcntl
import json
import subprocess
import time
import urllib.request
from pathlib import Path

TENANT = 'wtyj-mermaid'
CONTROL = 'unboks-internal-control-panel-wtyj-admin-1'
STATE = Path('/var/lib/unboks-tracy-watchdog/status.json')
MAINTENANCE = Path('/root/clients/mermaid/.maintenance')
COOLDOWN = 600

# Exercise the same shared lifecycle lease and real HTTP callback which caused
# the outage. Keys stay inside the control container and never enter output.
BRIDGE_PROBE = '''
import json, urllib.request
from app.config import get_settings
from app.provisioning import tenant_read_lock
from app.routes.internal import _tenant_bridge_token
with tenant_read_lock('mermaid'):
    token = _tenant_bridge_token('mermaid', get_settings())
    if not token:
        raise RuntimeError('Tenant bridge token is missing')
    request = urllib.request.Request('http://127.0.0.1:8010/internal/tenants/mermaid/overrides', headers={
        'Authorization': 'Bearer ' + token, 'X-Tenant-Identity': 'mermaid',
    })
    with urllib.request.urlopen(request, timeout=3) as response:
        envelope = json.load(response)
    assert envelope.get('available') and envelope.get('tenant_id') == 'mermaid'
    toggles = envelope.get('feature_toggles', {})
    print(json.dumps({
        'ai_auto_reply': toggles.get('ai_auto_reply', {}).get('value') is True,
        'whatsapp_inbox': toggles.get('whatsapp_inbox', {}).get('value') is True,
        'whatsapp_connected': envelope.get('channel_connections', {}).get('whatsapp', {}).get('connected') is True,
    }))
'''


def command(args, *, input=None, timeout=8):
    return subprocess.run(args, input=input, capture_output=True, text=True, check=True, timeout=timeout).stdout


def inspect_runtime():
    data = json.loads(command(['docker', 'inspect', TENANT]))[0]
    return data['State']['Running'], data['HostConfig']['RestartPolicy']['Name']


def runtime_health():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8102/health', timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def bridge_health():
    return json.loads(command(['docker', 'exec', '-i', CONTROL, 'python', '-'], input=BRIDGE_PROBE))


def check(previous, now):
    state = {'checked_at': now, 'status': 'healthy', 'issues': [],
             'consecutive_runtime_failures': 0,
             'last_recovery_at': previous.get('last_recovery_at', 0)}
    if MAINTENANCE.exists():
        state['status'] = 'maintenance'
        return state
    try:
        running, restart_policy = inspect_runtime()
        healthy = running and runtime_health()
        if not healthy:
            failures = previous.get('consecutive_runtime_failures', 0) + 1
            state['consecutive_runtime_failures'] = failures
            if failures >= 2 and now - state['last_recovery_at'] >= COOLDOWN:
                if MAINTENANCE.exists():
                    state['status'] = 'maintenance'
                    return state
                state['last_recovery_at'] = now
                # Only the existing Mermaid runtime may be started/restarted.
                # No compose rebuild, image substitution or tenant mutation.
                command(['docker', 'restart' if running else 'start', TENANT], timeout=15)
                state['status'] = 'recovering'
            else:
                state['status'] = 'unhealthy'
            state['issues'].append('runtime_unavailable')
            return state
        if restart_policy not in {'always', 'unless-stopped'}:
            state['issues'].append('restart_policy_disabled')
        controls = bridge_health()
        for key, enabled in controls.items():
            if not enabled:
                state['issues'].append(key + '_disabled')
        # Human pause/tenant controls are never automatically overridden.
        if state['issues']:
            state['status'] = 'attention'
    except Exception as exc:
        state['status'] = 'unhealthy'
        state['issues'].append(type(exc).__name__)
    return state


def main():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            previous = json.loads(STATE.read_text())
        except (OSError, ValueError):
            previous = {}
        state = check(previous, time.time())
        pending = STATE.with_suffix('.tmp')
        pending.write_text(json.dumps(state, indent=2) + '\n')
        pending.replace(STATE)
        if (state['status'], state['issues']) != (previous.get('status'), previous.get('issues')):
            print(json.dumps(state), flush=True)
        return 0 if state['status'] in {'healthy', 'maintenance'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
