#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest
s = importlib.util.spec_from_file_location('bridge', Path(__file__).with_name('joystick_bridge.py'))
b = importlib.util.module_from_spec(s)
s.loader.exec_module(b)

class Controls(unittest.TestCase):
    def test_layout(self):
        c = b.DeckControls()
        def axis(k,v): return c.translate(dict(type='axis',axis=k,value=v,t_ms=0))
        def button(k,a): return c.translate(dict(type='button',key=k,action=a,t_ms=0))
        self.assertEqual(axis('RT',0)[0]['action'],'up')
        self.assertEqual(axis('RT',.5)[0]['key'],'RB')
        self.assertEqual(axis('RT',.5),[])
        self.assertEqual(axis('RT',.15),[])
        self.assertEqual(axis('RT',.1)[0]['action'],'up')
        self.assertEqual(axis('LT',.7)[0]['axis'],'RT')
        self.assertEqual(axis('LT',.7)[0]['value'],.7)
        self.assertEqual(button('B','down'),[])
        self.assertEqual(button('B','up'),[])
        self.assertEqual(button('RB','down'),[dict(type='button',key='LB',action='down',t_ms=0)])
        self.assertEqual(button('RB','up'),[dict(type='button',key='LB',action='up',t_ms=0)])
        for k in ('DPAD_LEFT','DPAD_RIGHT','DPAD_UP','DPAD_DOWN'):
            self.assertEqual(button(k,'down'),[])
        self.assertEqual(button('A','down'),[])
        self.assertEqual(button('LB','down'),[dict(type='axis',axis='LT',value=1.0,t_ms=0)])
        self.assertEqual(button('LB','up'),[dict(type='axis',axis='LT',value=0.0,t_ms=0)])
        self.assertEqual(button('Y','down')[0]['key'],'Y')
        self.assertEqual(button('START','down')[0]['key'],'BACK')
        self.assertEqual(button('START','up')[0]['key'],'BACK')
        for action in ('down', 'up'):
            self.assertEqual(button('VIEW', action), [dict(type='button', key='VIEW', action=action, t_ms=0)])
        for v in (-1,-.8,-.15,0,.15,.8,1):
            self.assertEqual(axis('LX',v),[dict(type='axis',axis='LX',value=b.steering_curve(v),t_ms=0)])
            self.assertEqual(axis('LY',v),[dict(type='axis',axis='LY',value=v,t_ms=0)])
        self.assertEqual(b.steering_curve(0.1),0)
        self.assertAlmostEqual(b.steering_curve(1),1)
        self.assertAlmostEqual(b.steering_curve(-1),-1)
        self.assertLess(b.steering_curve(.5),.35)
        samples=[b.steering_curve(i/100) for i in range(-100,101)]
        self.assertEqual(samples,sorted(samples))
        self.assertEqual(axis('RX',1),[])
        self.assertEqual(axis('RY',1),[])

if __name__ == '__main__': unittest.main()
