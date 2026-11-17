from scripts.wan_matrix import cases


def test_wan_matrices_cover_exact_combinations_and_six_item_batches():
    for category in ['kvm','services','agents']:
        matrix=cases(category)
        assert len(matrix)==18 and len(matrix)%6==0
        main=[case for case in matrix if not case['control']]
        assert len(main)==16
        assert len({(case['product'],case['image'],case['plan']) for case in main})==16
        if category!='kvm':assert {case['image'] for case in main}=={'ubuntu-2404','almalinux-9'}


def test_parallel_failure_contains_future_mutations():
    import threading
    import pytest
    from scripts.wan_matrix import Matrix
    matrix=object.__new__(Matrix)
    matrix.abort=threading.Event()
    def fail(_):raise RuntimeError('FALLO_CONTROLADO')
    with pytest.raises(RuntimeError):matrix.parallel(fail,range(6))
    assert matrix.abort.is_set()


def test_record_keeps_matrix_running_when_ssh_output_closes(tmp_path, monkeypatch):
    import json
    import sys
    from scripts.wan_matrix import Matrix

    class ClosedOutput:
        def write(self, _):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError

    matrix = object.__new__(Matrix)
    matrix.log = tmp_path / 'results.jsonl'
    monkeypatch.setattr(sys, 'stdout', ClosedOutput())
    matrix.record('verified', name='prueba-service-n8n-c1-01')
    assert json.loads(matrix.log.read_text())['event'] == 'verified'


def test_almalinux_matrix_pins_key_through_validated_helper(tmp_path):
    import json
    import pytest
    from types import SimpleNamespace
    from scripts.wan_matrix import Matrix
    matrix = object.__new__(Matrix)
    matrix.profile = 'lab_dhcp'
    matrix.folder = tmp_path
    matrix.proxmox = SimpleNamespace(vm_config=lambda *_: {
        'name': 'test', 'net0': 'e1000=aa:bb:cc:dd:ee:ff,bridge=vmbr0', 'ipconfig0': 'ip=dhcp',
    })
    item = {'vmid': 40000, 'name': 'test', 'instance_id': 'instance-test', 'image': 'almalinux-9', 'address': '192.0.2.10'}
    observed = {'bootstrap_protocol': 1, 'instance_id': 'instance-test', 'mac_address': 'aa:bb:cc:dd:ee:ff', 'host_key': 'ssh-ed25519 AAAAexample'}
    commands = []
    matrix.qga = lambda _vmid, argv: commands.append(argv) or json.dumps(observed)
    matrix.binding(item)
    assert commands[0] == ['/usr/local/libexec/iacp-guest-bootstrap', 'instance-test', 'aa:bb:cc:dd:ee:ff', 'dhcp', '', '']
    assert (tmp_path/'known-40000').read_text() == '192.0.2.10 ssh-ed25519 AAAAexample\n'
    observed['instance_id'] = 'another-instance'
    with pytest.raises(AssertionError):
        matrix.binding(item)
