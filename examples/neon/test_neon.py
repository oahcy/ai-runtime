import math
import pytest
from neon import NeonPreview

def test_atomic_validation_and_bounded_history():
    p = NeonPreview()
    for changes in ({'hue': math.nan}, {'particles': 2.5}, {'rings': True},
                    {'unknown': 1}, {'speed': 9}, {'title': 'x' * 81}):
        before = p.snapshot()
        with pytest.raises(ValueError):
            p.update(**changes)
        assert p.snapshot() == before
    with pytest.raises(ValueError):
        p.update(hue=20, speed=-1)
    assert p.snapshot()['state']['hue'] == 190
    for i in range(35):
        p.update(hue=i, message=f'change {i}')
    snap = p.snapshot()
    assert snap['revision'] == 35
    assert len(snap['history']) == 30
    snap['history'][0]['changes']['hue'] = 200
    assert p.snapshot()['history'][0]['changes']['hue'] == 5
