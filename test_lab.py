import math
import pytest
from lab import WindTunnel


def test_wind_changes_actual_particle_motion():
    a,b=WindTunnel(20),WindTunnel(20)
    b.configure(wind=[0,180],message='turn')
    for _ in range(90):
        a._advance(1/30);b._advance(1/30)
    assert sum(p[3] for p in b.points)/20 > 100
    assert a.points != b.points
    assert all(0<=p[0]<1000 and 0<=p[1]<600 for p in b.points)


def test_validation_is_atomic_and_vortex_affects_motion():
    a,b=WindTunnel(20),WindTunnel(20)
    old=a.snapshot()
    with pytest.raises(ValueError):
        a.configure(wind=[0,100],vortices=[[500,300,float('nan')]])
    assert a.snapshot()==old
    b.configure(vortices=[[400,300,200]])
    for _ in range(30):
        a._advance(1/30);b._advance(1/30)
    assert a.points != b.points
    assert all(math.isfinite(v) for p in b.points for v in p)


def test_particle_obstacle_projection():
    a=WindTunnel(10)
    a.points[0]=[490,300,0,0]
    a._advance(1/30)
    p=a.points[0]
    assert math.hypot(p[0]-490,p[1]-300)>=69.99
