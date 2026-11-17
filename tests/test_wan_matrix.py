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
