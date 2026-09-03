import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('tracy_watchdog', Path(__file__).parents[1] / 'host/tracy_watchdog.py')
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


def setup_probe(monkeypatch, tmp_path, *, running=True, health=True):
    monkeypatch.setattr(watchdog, 'MAINTENANCE', tmp_path / '.maintenance')
    monkeypatch.setattr(watchdog, 'inspect_runtime', lambda: (running, 'unless-stopped'))
    monkeypatch.setattr(watchdog, 'runtime_health', lambda: health)
    monkeypatch.setattr(watchdog, 'bridge_health', lambda: {'ai_auto_reply': True, 'whatsapp_inbox': True, 'whatsapp_connected': True})
    calls = []
    monkeypatch.setattr(watchdog, 'command', lambda args, **kwargs: calls.append(args))
    return calls


def test_recovery_requires_repeated_failure_and_only_starts_mermaid(monkeypatch, tmp_path):
    calls = setup_probe(monkeypatch, tmp_path, running=False)
    first = watchdog.check({}, 1000)
    assert calls == []
    second = watchdog.check(first, 1060)
    assert calls == [['docker', 'start', 'wtyj-mermaid']]
    assert second['status'] == 'recovering'
    watchdog.check(second, 1120)
    assert len(calls) == 1


def test_hung_running_service_recovers_after_second_failure(monkeypatch, tmp_path):
    calls = setup_probe(monkeypatch, tmp_path, health=False)
    result = watchdog.check({'consecutive_runtime_failures': 1}, 1000)
    assert calls == [['docker', 'restart', 'wtyj-mermaid']]
    assert result['last_recovery_at'] == 1000


def test_maintenance_prevents_restart(monkeypatch, tmp_path):
    calls = setup_probe(monkeypatch, tmp_path, running=False)
    watchdog.MAINTENANCE.touch()
    result = watchdog.check({'consecutive_runtime_failures': 9}, 1000)
    assert result['status'] == 'maintenance'
    assert calls == []


def test_operator_pause_is_reported_without_overriding_it(monkeypatch, tmp_path):
    calls = setup_probe(monkeypatch, tmp_path)
    monkeypatch.setattr(watchdog, 'bridge_health', lambda: {'ai_auto_reply': False, 'whatsapp_inbox': True, 'whatsapp_connected': True})
    result = watchdog.check({}, 1000)
    assert result['issues'] == ['ai_auto_reply_disabled']
    assert result['status'] == 'attention'
    assert calls == []


def test_callback_failure_does_not_restart_healthy_runtime(monkeypatch, tmp_path):
    calls = setup_probe(monkeypatch, tmp_path)
    def timeout():
        raise TimeoutError()
    monkeypatch.setattr(watchdog, 'bridge_health', timeout)
    result = watchdog.check({}, 1000)
    assert result['status'] == 'unhealthy'
    assert calls == []
