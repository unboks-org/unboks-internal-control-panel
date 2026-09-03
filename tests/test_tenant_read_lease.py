"""Read leases cooperate across callbacks without allowing lifecycle races."""
import os
import subprocess
import sys
import threading

import pytest
from app.provisioning import tenant_creation_lock, tenant_read_lock


def test_shared_readers_coexist_and_lifecycle_writer_waits():
    reader_done = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()

    def read():
        with tenant_read_lock('mermaid'):
            reader_done.set()

    def write():
        writer_started.set()
        with tenant_creation_lock('mermaid'):
            writer_done.set()

    with tenant_read_lock('mermaid'):
        reader = threading.Thread(target=read)
        writer = threading.Thread(target=write)
        reader.start()
        assert reader_done.wait(1)
        writer.start()
        assert writer_started.wait(1)
        assert not writer_done.wait(0.05)
    reader.join(2)
    writer.join(2)
    assert writer_done.is_set()


def test_read_lease_fences_other_processes():
    code = '''from app.provisioning import tenant_creation_lock, tenant_read_lock
import sys
with (tenant_read_lock if sys.argv[1] == 'read' else tenant_creation_lock)('mermaid'):
 print('acquired', flush=True)
'''
    with tenant_read_lock('mermaid'):
        result = subprocess.run([sys.executable, '-c', code, 'read'], capture_output=True, text=True, timeout=3)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'acquired'
        writer = subprocess.Popen([sys.executable, '-c', code, 'write'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                writer.communicate(timeout=0.2)
        except BaseException:
            writer.kill()
            writer.communicate()
            raise
    stdout, stderr = writer.communicate(timeout=3)
    assert writer.returncode == 0, stderr
    assert stdout.strip() == 'acquired'


def test_nesting_and_upgrade_protection():
    with tenant_creation_lock('mermaid'):
        with tenant_read_lock('mermaid'):
            with tenant_creation_lock('mermaid'):
                pass
    with tenant_read_lock('mermaid'):
        with tenant_read_lock('mermaid'):
            with pytest.raises(RuntimeError, match='Cannot upgrade'):
                with tenant_creation_lock('mermaid'):
                    pass
    for slug in ('../mermaid', '', 'Mermaid'):
        with pytest.raises(ValueError):
            with tenant_read_lock(slug):
                pass
