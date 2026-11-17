from scripts.wan_matrix import cases


def test_wan_matrices_cover_exact_combinations_and_six_item_batches():
    for category in ['kvm','services','agents']:
        matrix=cases(category)
        assert len(matrix)==18 and len(matrix)%6==0
        main=[case for case in matrix if not case['control']]
        assert len(main)==16
        assert len({(case['product'],case['image'],case['plan']) for case in main})==16
        if category!='kvm':assert {case['image'] for case in main}=={'ubuntu-2404','almalinux-9'}
