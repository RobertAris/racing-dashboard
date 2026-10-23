"""Lab 7 source/facade and simulator contract checks without importing the gym."""
import hashlib
import importlib.util
import math
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import unittest

import numpy as np
from PIL import Image

from racing_dashboard.mpc_controller import ControllerSource
from racing_dashboard.simulation import (RouteProgress, SimulationSession, command_geometry,
                                        load_source, local_to_world, run_worker,
                                        validate_changes, validate_map_route, validate_options)


MPC_SOURCE = '''
# ROS imports and the entrypoint are deliberately not executed by the facade.
import rclpy
from missing_ros_package import NeverImportThis
from dataclasses import dataclass, field
import numpy as np
@dataclass
class mpc_config:
    NXK: int = 4
    NU: int = 2
    TK: int = 8
    DTK: float = .1
    WB: float = .33
    LENGTH: float = .58
    WIDTH: float = .31
    MIN_STEER: float = -.4189
    MAX_STEER: float = .4189
    MAX_SPEED: float = 6.
    MAX_ACCEL: float = 3.
    Rk: object = field(default_factory=lambda: np.diag([.01,30.]))
@dataclass
class State:
    x: float = 0.
    y: float = 0.
    v: float = 0.
    yaw: float = 0.
class MPC(Node):
    def __init__(self):
        super().__init__('mpc_node')
        for name, default in {'track':'levine','pose_offset':0.,'waypoints_file':'',
                              'max_accel':3.,'profile_accel':1.5,'profile_decel':1.5}.items():
            self.declare_parameter(name,default)
            setattr(self,name,self.get_parameter(name).value)
        self.config=mpc_config()
        self.declare_parameter('MAX_SPEED',self.config.MAX_SPEED)
        self.config.MAX_SPEED=self.get_parameter('MAX_SPEED').value
        if self.max_accel>self.config.MAX_ACCEL or self.profile_accel>self.max_accel or self.profile_decel>self.max_accel:
            raise ValueError('Acceleration exceeds source model limit')
        self.waypoints=np.loadtxt(self.waypoints_file,delimiter=',',ndmin=2)
        if self.waypoints.shape[1]!=4:
            raise ValueError('Waypoints must contain x, y, yaw, speed')
        self.waypoints[:,3]=np.minimum(self.waypoints[:,3],self.config.MAX_SPEED)
        self.drive_pub=self.create_publisher(AckermannDriveStamped,'/drive',1)
        self.ref_pub=self.create_publisher(Path,'/mpc/reference',1)
        self.pred_pub=self.create_publisher(Path,'/mpc/prediction',1)
        self.oa=self.odelta_v=None
        self.last_input=np.zeros(2)
        self.MPC_prob=type('Problem',(),{'status':None,'solver_stats':None})()
        self.fail=False
    def odom_callback(self,message):
        self.last_v=math.hypot(message.twist.twist.linear.x,message.twist.twist.linear.y)
        self.last_input=np.array([1.,.1])
        drive=AckermannDriveStamped()
        drive.header.stamp=self.get_clock().now().to_msg()
        if self.fail:
            self.oa=self.odelta_v=None
            self.drive_pub.publish(drive)
            return
        self.oa=np.ones(8)
        self.odelta_v=np.zeros(8)
        drive.drive.speed=self.config.MAX_SPEED
        drive.drive.steering_angle=.1
        self.drive_pub.publish(drive)
        for publisher,offset in ((self.ref_pub,1.),(self.pred_pub,2.)):
            path=Path()
            for distance in (0.,offset):
                point=PoseStamped()
                point.pose.position.x=message.pose.pose.position.x+distance
                point.pose.position.y=message.pose.pose.position.y
                path.poses.append(point)
            publisher.publish(path)
        self.MPC_prob.status='optimal'
        self.MPC_prob.solver_stats=type('Stats',(),{'solve_time':.002,'num_iters':10})()

def main():
    raise AssertionError('ROS main must not execute')
'''
UTILS_SOURCE = '''
def nearest_point(*args): return None
def calc_interpolated_ref_trajectory(*args): return None
'''
HELPER_SOURCE = '''
import types
from pathlib import Path
import numpy as np
MAPS=Path(__file__).parent/'maps'
def build_env(rear_start,noise_std,seed,map_yaml):
    return Env(rear_start,seed,map_yaml)
def rear_pose(env): return env.pose.copy()
class Env:
    def __init__(self,start,seed,map_yaml):
        self.pose=np.array(start,dtype=float)
        self.seed,self.map_yaml=seed,map_yaml
        self.unwrapped=self
        self.closed=False
        self.vehicle_params=types.SimpleNamespace(lr=.17145,length=.58,width=.31)
        self.sim=types.SimpleNamespace(state=types.SimpleNamespace(
            scans=np.full((1,10),10.),collisions=np.array([False]),standard_state=np.zeros((1,7))))
        self.env_config=types.SimpleNamespace(
            simulation_config=types.SimpleNamespace(timestep=.01),
            lidar_config=types.SimpleNamespace(angle_max=1.,angle_min=-1.,num_beams=10,
                range_min=.05,range_max=25.,base_link_to_lidar_tf=(.275,0.,0.)))
    def step(self,action):
        speed=action[0,1]
        self.pose[:2]+=.01*speed*np.array([np.cos(self.pose[2]),np.sin(self.pose[2])])
        self.sim.state.standard_state[0,3]=speed
    def close(self): self.closed=True
'''


def fixture(root):
    for folder in ('mpc/scripts','mpc/waypoints','mpc/launch','maps'):
        (root/folder).mkdir(parents=True,exist_ok=True)
    (root/'mpc/scripts/mpc_node.py').write_text(MPC_SOURCE)
    (root/'mpc/scripts/utils.py').write_text(UTILS_SOURCE)
    for track in ('levine','spielberg'):
        center=np.array([-9.,0.]) if track=='levine' else np.array([14.59,6.92])
        angle=np.linspace(math.pi,3*math.pi,80,endpoint=False)
        xy=center+3*np.column_stack((np.cos(angle),np.sin(angle)))
        data=np.column_stack((xy,angle+math.pi/2,np.full(len(angle),5.)))
        np.savetxt(root/f'mpc/waypoints/{track}.csv',data,delimiter=',')
        (root/f'mpc/launch/{track}_launch.py').write_text("PARAMETERS={'track': '"+track+"', 'MAX_SPEED':3., 'profile_accel':2.2}\n")
    (root/'helper.py').write_text(HELPER_SOURCE)
    Image.new('L',(800,800),255).save(root/'maps/free.png')
    for name in ('levine_blocked','Spielberg'):
        (root/f'maps/{name}.yaml').write_text('image: free.png\nresolution: 0.1\norigin: [-40, -40, 0]\n')
    return {'repo':str(root),'helper':str(root/'helper.py'),'track':'levine','case':'lap','config':{}}


class ValidationTests(unittest.TestCase):
    def test_four_native_controls_and_options(self):
        self.assertEqual(validate_changes({'MAX_SPEED':3.,'profile_accel':2.}),{'MAX_SPEED':3.,'profile_accel':2.})
        for change in ({'max_speed':3.},{'TK':8},{'max_accel':float('nan')},{'MAX_SPEED':True},{'MAX_SPEED':20}):
            with self.subTest(change=change),self.assertRaises(ValueError): validate_changes(change)
        self.assertEqual(validate_options({'repo':'/tmp/lab7'})['control_dt'],.1)
        self.assertEqual(validate_options({'repo':'/tmp/lab7','track':'spielberg'})['track'],'spielberg')
        for change in ({'case':'turn'},{'track':'unknown'},{'rate':0},{'ref':3}):
            with self.subTest(change=change),self.assertRaises(ValueError):validate_options({'repo':'/tmp/lab7',**change})

    def test_geometry_projection_and_display_only_command_arc(self):
        self.assertTrue(np.allclose(local_to_world([[1,0]],[2,3,math.pi/2]),[[2,4]]))
        route=RouteProgress([[0,0],[2,0],[2,2],[0,2]],[0,0,0])
        self.assertAlmostEqual(route.update([1,0,0]),1.)
        config={'wheelbase':.33,'DTK':.1,'TK':8,'vehicle_front':.46,'vehicle_rear':.12,'vehicle_half_width':.155}
        arc,footprints=command_geometry(config,2.,0.,[2,3,0])
        self.assertTrue(np.allclose(arc[-1],[3.6,3]))
        self.assertTrue(footprints)

    def test_source_is_recompiled_fresh_without_timestamp_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'example.py';path.write_text('VALUE=1\n')
            first=load_source(path,'fixture');path.write_text('VALUE=2\n')
            second=load_source(path,'fixture')
            self.assertEqual((first.VALUE,second.VALUE),(1,2))
            self.assertNotEqual(first.__source_sha256__,second.__source_sha256__)

    def test_missing_or_mismatched_map_fails_clearly(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            route=np.array([[-12.,0.],[-11.,0.],[-11.,1.]])
            self.assertTrue(validate_map_route(root/'maps/levine_blocked.yaml',route,(-12,0,0)))
            with self.assertRaisesRegex(ValueError,'map is missing'):
                validate_map_route(root/'maps/missing.yaml',route,(-12,0,0))
            with self.assertRaisesRegex(ValueError,'does not match'):
                validate_map_route(root/'maps/levine_blocked.yaml',route+100,(-12,0,0))


@unittest.skipUnless(importlib.util.find_spec('cvxpy') and importlib.util.find_spec('numba'), 'Install .[mpc] for MPC source/facade tests')
class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.options=fixture(self.root)
        self.sessions=[]
    def tearDown(self):
        for session in self.sessions:session.close()
        self.temp.cleanup()
    def session(self,**changes):
        session=SimulationSession({**self.options,**changes});self.sessions.append(session);return session

    def test_actual_source_constructor_launch_layers_and_snapshot(self):
        session=self.session()
        self.assertEqual(session.config['MAX_SPEED'],3.)
        self.assertEqual(session.config['profile_accel'],2.2)
        self.assertEqual(session.start,(-12.,0.,0.))
        self.assertTrue(session.map_yaml.endswith('levine_blocked.yaml'))
        session.step();snapshot=session.snapshot()
        self.assertAlmostEqual(snapshot['time_s'],.1)
        self.assertEqual(snapshot['speed_command'],3.)
        self.assertEqual(snapshot['reference'][1][0],-11.)
        self.assertEqual(snapshot['path'][1][0],-10.)
        self.assertEqual(snapshot['diagnostics']['solver_status'],'optimal')
        self.assertEqual(snapshot['diagnostics']['solver_time_ms'],2.)
        self.assertNotIn('tree',snapshot);self.assertNotIn('grid',snapshot)
        self.assertEqual(snapshot['metadata']['controller'],'Lab 7 MPC')
        self.assertNotIn('racing-mpc-',snapshot['metadata']['planner_file'])
        self.assertEqual(snapshot['config']['TK'],8)
        self.assertEqual(snapshot['config']['Rk'],[[.01,0.],[0.,30.]])

    def test_apply_preserves_physics_pose_and_rebuilds_warmstart_atomically(self):
        session=self.session(config={'MAX_SPEED':2.})
        session.step();pose=session.env.pose.copy();environment=session.env;old=session.controller
        active=session.apply({'MAX_SPEED':2.5})
        self.assertIs(session.env,environment);self.assertTrue(np.array_equal(pose,session.env.pose))
        self.assertIsNot(session.controller,old);self.assertTrue(np.all(session.controller.node.last_input==0))
        self.assertEqual(active['MAX_SPEED'],2.5)
        with self.assertRaises(ValueError):session.apply({'max_accel':1.,'profile_accel':2.})
        self.assertEqual(session.controller.configuration()['max_accel'],3.)
        self.assertEqual(session.config_revision,1)

    def test_callback_uses_odometry_speed_and_failure_stop_without_stale_paths(self):
        session=self.session();session.env.sim.state.standard_state[0,3]=2.
        session.env.sim.state.standard_state[0,6]=.4
        session.step();self.assertAlmostEqual(session.controller.node.last_v,2.)
        self.assertTrue(session.snapshot()['path'])
        session.controller.node.fail=True;session.step();snapshot=session.snapshot()
        self.assertEqual(snapshot['speed_command'],0.)
        self.assertEqual(snapshot['path'],[]);self.assertEqual(snapshot['reference'],[])
        self.assertFalse(snapshot['diagnostics']['control_available'])
        self.assertIsNone(snapshot['diagnostics']['solver_status'])
        self.assertIsNone(snapshot['diagnostics']['solver_time_ms'])

    def test_collision_is_observer_only_and_cleanup(self):
        session=self.session();environment=session.env
        environment.sim.state.collisions[0]=True;session.step()
        self.assertEqual(session.status,'collision')
        self.assertEqual(session.controller.speed,3.)
        root=session.source.root;session.close()
        self.assertTrue(environment.closed);self.assertFalse(root.exists())

    def test_git_ref_snapshots_selected_bytes_without_checkout_or_writes(self):
        def git(*arguments):return subprocess.check_output(['git','-C',str(self.root),*arguments]).decode().strip()
        git('init','-q');git('add','.');git('-c','user.name=Fixture','-c','user.email=fixture@example.test','commit','-qm','fixture')
        commit=git('rev-parse','HEAD')
        path=self.root/'mpc/scripts/mpc_node.py'
        original=path.read_bytes();path.write_text(path.read_text().replace('TK: int = 8','TK: int = 12'))
        before=git('status','--porcelain')
        session=self.session(ref=commit)
        self.assertEqual(session.config['TK'],8)
        self.assertEqual(session.metadata['planner_sha256'],hashlib.sha256(original).hexdigest())
        self.assertEqual(session.metadata['commit'],commit);self.assertFalse(session.metadata['dirty'])
        self.assertEqual(git('rev-parse','HEAD'),commit);self.assertEqual(git('status','--porcelain'),before)
        self.assertEqual(self.session().config['TK'],12)
        self.assertEqual(git('status','--porcelain'),before)
        self.assertFalse((self.root/'mpc/scripts/__pycache__').exists())

    def test_lap7_source_required_and_csv_shape_is_native_validation(self):
        (self.root/'mpc/scripts/mpc_node.py').write_text('class LocalPlanner: pass\n')
        with self.assertRaisesRegex(ValueError,'Lab 7 MPC classes'):self.session()
        (self.root/'mpc/scripts/mpc_node.py').write_text(MPC_SOURCE)
        custom=self.root/'two-column.csv';custom.write_text('0,0\n1,0\n1,1\n')
        with self.assertRaisesRegex(ValueError,'x, y, yaw, speed'):self.session(waypoints=str(custom))

    def test_missing_stock_map_never_falls_back_to_previous_map(self):
        (self.root/'maps/levine_blocked.yaml').unlink()
        (self.root/'maps/levine_obs.yaml').write_text('image: free.png\nresolution: .1\norigin: [-40,-40,0]\n')
        with self.assertRaisesRegex(ValueError,'levine_blocked.yaml'):self.session()

    def test_explicit_stock_map_retains_stock_start_custom_map_uses_route_pose(self):
        stock=self.session(map_yaml=str(self.root/'maps/levine_blocked.yaml'))
        self.assertEqual(stock.start,(-12.,0.,0.))
        custom=self.root/'maps/custom.yaml'
        custom.write_text((self.root/'maps/levine_blocked.yaml').read_text())
        selected=self.session(map_yaml=str(custom))
        self.assertEqual(selected.start,tuple(selected.controller.node.waypoints[0,:3]))

    def test_worker_pause_step_apply_reset_protocol(self):
        commands,output=queue.Queue(),queue.Queue(maxsize=20)
        thread=threading.Thread(target=run_worker,args=(commands,output,self.options),daemon=True);thread.start()
        def until(predicate):
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                message=output.get(timeout=max(.1,deadline-time.monotonic()))
                if message.get('type')=='error':self.fail(message['message'])
                if predicate(message):return message
            self.fail('Worker did not produce expected state')
        try:
            until(lambda m:m.get('type')=='snapshot' and m['data'].get('status')=='paused')
            commands.put({'action':'step'})
            until(lambda m:m.get('type')=='snapshot' and m['data'].get('time_s',0)>0)
            commands.put({'action':'apply','config':{'MAX_SPEED':2.}})
            until(lambda m:m.get('type')=='event' and m['data'].get('event')=='config_applied')
            commands.put({'action':'reset','track':'levine'})
            until(lambda m:m.get('type')=='snapshot' and m['data'].get('time_s')==0)
        finally:
            commands.put({'action':'stop'});thread.join(3)
        self.assertFalse(thread.is_alive())


if __name__=='__main__':unittest.main()
