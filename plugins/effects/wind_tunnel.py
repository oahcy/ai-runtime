"""A live Python particle field, visualized through ai-runtime Preview."""
import copy
import math
import random
import threading
import time
from pathlib import Path
from ai_runtime.preview import Preview


class WindTunnel:
    def __init__(self, count=600, seed=42, context=None):
        self.context = context
        self._worker_resource = None
        factory = context.preview.create if context else Preview
        self._view = factory(html=Path(__file__).with_name('wind.html').read_text(encoding="utf-8"))
        if not isinstance(count, int) or not 10 <= count <= 1500:
            raise ValueError('count must be an integer between 10 and 1500')
        rng = random.Random(seed)
        self.points = [[rng.uniform(0,1000), rng.uniform(0,600), 85.0, 0.0] for _ in range(count)]
        self.wind = [100.0, 0.0]
        self.vortices = []
        self.obstacles = [[490.0,300.0,68.0]]
        self.paused = False
        self.tick = 0
        self.crossings = 0
        self.compute_ms = 0
        self._stop = threading.Event()
        self._physics = None
        self._lock = threading.Lock()
        self._commands = []
        self._command = 0
        self._note('风洞启动：恒定水平风，观察粒子绕过中央障碍物')
        self._publish()

    def _note(self, text):
        self._command += 1
        self._commands.append(dict(revision=self._command,message=text,time=time.time()))
        self._commands = self._commands[-20:]

    def configure(self, *, wind=None, vortices=None, obstacles=None, paused=None, message='调整实验参数'):
        # Validate entire command before changing live state.
        def vectors(value, length, limits):
            if not isinstance(value,(list,tuple)) or len(value)>8:
                raise ValueError('at most 8 objects')
            output=[]
            for item in value:
                if not isinstance(item,(list,tuple)) or len(item)!=length:
                    raise ValueError('invalid vector length')
                if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or not lo<=x<=hi for x,(lo,hi) in zip(item,limits)):
                    raise ValueError('invalid or out-of-range value')
                output.append(list(item))
            return output
        if not isinstance(message,str) or len(message)>300: raise ValueError('invalid message')
        new_wind=vectors([wind],2,[(-250,250),(-250,250)])[0] if wind is not None else None
        new_vortices=vectors(vortices,3,[(0,1000),(0,600),(-250,250)]) if vortices is not None else None
        new_obstacles=vectors(obstacles,3,[(0,1000),(0,600),(10,120)]) if obstacles is not None else None
        if paused is not None and not isinstance(paused,bool): raise ValueError('paused must be boolean')
        with self._lock:
            if new_wind is not None: self.wind=new_wind
            if new_vortices is not None: self.vortices=new_vortices
            if new_obstacles is not None: self.obstacles=new_obstacles
            if paused is not None: self.paused=paused
            self._note(message)
            self._publish()
        return dict(command=self._command,wind=list(self.wind),vortices=copy.deepcopy(self.vortices),paused=self.paused)

    def _advance(self, dt):
        for p in self.points:
            x,y,vx,vy=p
            fx,fy=self.wind
            for ox,oy,power in self.vortices:
                dx,dy=x-ox,y-oy
                fall=math.exp(-(dx*dx+dy*dy)/(2*155**2))
                fx+=-dy/100*power*fall
                fy+= dx/100*power*fall
            blend=min(1,dt*2.6)
            vx+=(fx-vx)*blend
            vy+=(fy-vy)*blend
            x+=vx*dt; y+=vy*dt
            for ox,oy,r in self.obstacles:
                dx,dy=x-ox,y-oy
                dist=math.hypot(dx,dy)
                if dist<r+2:
                    nx,ny=(dx/dist,dy/dist) if dist>1e-8 else (-1,0)
                    x=ox+nx*(r+2);y=oy+ny*(r+2)
                    normal=vx*nx+vy*ny
                    if normal<0: vx-=1.7*normal*nx;vy-=1.7*normal*ny
            if x<0 or x>=1000: self.crossings+=1
            x%=1000;y%=600
            p[:]=x,y,vx,vy
        self.tick+=1

    def _publish(self):
        speeds=[math.hypot(p[2],p[3]) for p in self.points]
        state=dict(points=[[round(v,2) for v in p] for p in self.points],
            wind=list(self.wind), vortices=copy.deepcopy(self.vortices),
            obstacles=copy.deepcopy(self.obstacles),tick=self.tick,paused=self.paused,
            elapsed=round(self.tick/30,1),mean_speed=round(sum(speeds)/len(speeds),1),
            crossings=self.crossings,compute_ms=round(self.compute_ms,2),command=self._command,commands=copy.deepcopy(self._commands))
        self._view.publish(state)

    def snapshot(self):
        return self._view.snapshot()

    def info(self):
        return self._view.info()

    def _run(self):
        deadline=time.monotonic()
        while not self._stop.is_set():
            before=time.perf_counter()
            with self._lock:
                if not self.paused:
                    self._advance(1/30)
                    self.compute_ms=(time.perf_counter()-before)*1000
                    self._publish()
            deadline+=1/30
            delay=deadline-time.monotonic()
            if delay<0: deadline=time.monotonic();delay=0
            self._stop.wait(delay)

    def start(self,port=0):
        info=self._view.start(port)
        if not self._physics or not self._physics.is_alive():
            self._stop.clear()
            self._physics=threading.Thread(target=self._run,daemon=True,name='wind-physics')
            if self.context:
                self._worker_resource = self.context.resources.track_thread(
                    self._physics, stop=self._stop.set, name='wind-physics')
            self._physics.start()
        return info

    def stop(self):
        self._stop.set()
        if self._worker_resource:
            self._worker_resource.close()
            self._worker_resource = None
        elif self._physics:
            self._physics.join(3)
            if self._physics.is_alive():
                raise RuntimeError('Wind physics shutdown not confirmed')
        self._view.stop()
