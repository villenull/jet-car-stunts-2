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
        self.assertEqual(axis('LT',.7)[0]['axis'],'BRAKE')
        self.assertEqual(axis('LT',.7)[0]['value'],.7)
        self.assertEqual(button('B','down'),[])
        self.assertEqual(button('B','up'),[])
        self.assertEqual(button('RB','down'),[dict(type='button',key='LB',action='down',t_ms=0)])
        self.assertEqual(button('RB','up'),[dict(type='button',key='LB',action='up',t_ms=0)])
        for k in ('DPAD_LEFT','DPAD_RIGHT','DPAD_UP','DPAD_DOWN'):
            self.assertEqual(button(k,'down'),[])
        self.assertEqual(button('A','down'),[])
        self.assertEqual(button('LB','down'),[dict(type='axis',axis='HANDBRAKE',value=1.0,t_ms=0)])
        self.assertEqual(button('LB','up'),[dict(type='axis',axis='HANDBRAKE',value=0.0,t_ms=0)])
        self.assertEqual(button('Y','down')[0]['key'],'Y')
        self.assertEqual(button('START','down')[0]['key'],'BACK')
        self.assertEqual(button('START','up')[0]['key'],'BACK')
        # The six driving controls must stay six distinct guest controls: the
        # physical shoulders collapsed onto one axis before (L1 and L2 both
        # pulled the handbrake), which is a defect a single shared value hides.
        c2 = b.DeckControls()
        driven = {
            'R2': c2.translate(dict(type='axis',axis='RT',value=1.0,t_ms=0)),
            'L2': c2.translate(dict(type='axis',axis='LT',value=0.6,t_ms=0)),
            'R1': c2.translate(dict(type='button',key='RB',action='down',t_ms=0)),
            'L1': c2.translate(dict(type='button',key='LB',action='down',t_ms=0)),
            'Y': c2.translate(dict(type='button',key='Y',action='down',t_ms=0)),
            'START': c2.translate(dict(type='button',key='START',action='down',t_ms=0)),
        }
        identities = {name: (out[0].get('axis') or out[0].get('key')) for name, out in driven.items()}
        self.assertEqual(len(set(identities.values())), 6, identities)
        self.assertEqual(identities['R2'], 'RB')
        self.assertEqual(identities['L2'], 'BRAKE')
        self.assertEqual(identities['R1'], 'LB')
        self.assertEqual(identities['L1'], 'HANDBRAKE')
        for action in ('down', 'up'):
            self.assertEqual(button('VIEW', action), [dict(type='button', key='VIEW', action=action, t_ms=0)])
        for v in (-1,-.8,-.15,0,.15,.8,1):
            self.assertEqual(axis('LX',v),[dict(type='axis',axis='LX',value=b.steering_curve(v),t_ms=0)])
            self.assertEqual(axis('LY',v),[dict(type='axis',axis='LY',value=v,t_ms=0)])
        self.assertEqual(b.steering_curve(0.1),0)
        self.assertAlmostEqual(b.steering_curve(1),1)
        self.assertAlmostEqual(b.steering_curve(-1),-1)
        self.assertLess(b.steering_curve(.5),.35)
        # Tuned 2026-09-18: slightly gentler mid-travel, same deadzone and lock.
        self.assertAlmostEqual(b.steering_curve(.5), .2681, places=4)
        samples=[b.steering_curve(i/100) for i in range(-100,101)]
        self.assertEqual(samples,sorted(samples))
        self.assertEqual(axis('RX',1),[])
        self.assertEqual(axis('RY',1),[])

if __name__ == '__main__': unittest.main()
