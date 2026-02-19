#  hammer-vlsi
#  CLI script - by default, it just uses the default CLIDriver.
#
#  See LICENSE for licence details.
'''
from hammer.vlsi import CLIDriver

def main():
    CLIDriver().main()

'''
import re
import os
import subprocess
import sys
import json

from airflow.models.dag import DAG
from airflow.operators.python import PythonOperator
from airflow.models.param import Param
from airflow.utils.trigger_rule import TriggerRule
from datetime import datetime
from airflow.exceptions import AirflowSkipException
from airflow.decorators import task, dag
from airflow.models import Variable

import pendulum

# Add the parent directory to the Python path to allow imports from 'vlsi'
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'vlsi')))

from hammer.vlsi import CLIDriver
from hammer.vlsi.cli_driver import import_task_to_dag
#import pdb
#pdb.set_trace()
class AIRFlow:
    def __init__(self):
        # minimal flow configuration variables
        self.design = os.getenv('design', 'gcd')
        self.pdk = os.getenv('pdk', 'sky130')
        self.tools = os.getenv('tools', 'cm')
        self.env = os.getenv('env', 'bwrc')
        self.extra = os.getenv('extra', '')  # extra configs
        self.args = os.getenv('args', '')  # command-line args (including step flow control)
        
        # Directory structure
        self.vlsi_dir = os.path.abspath('../e2e/')
        self.e2e_dir = os.getenv('e2e_dir', self.vlsi_dir)
        self.OBJ_DIR = os.getenv('OBJ_DIR', f"{self.e2e_dir}/build-{self.pdk}-{self.tools}/{self.design}")
        
        # non-overlapping default configs
        self.ENV_YML = os.getenv('ENV_YML', f"{self.e2e_dir}/configs-env/{self.env}-env.yml")
        self.PDK_CONF = os.getenv('PDK_CONF', f"{self.e2e_dir}/configs-pdk/{self.pdk}.yml")
        self.TOOLS_CONF = os.getenv('TOOLS_CONF', f"{self.e2e_dir}/configs-tool/{self.tools}.yml")

        # design-specific overrides of default configs
        self.DESIGN_CONF = os.getenv('DESIGN_CONF', f"{self.e2e_dir}/configs-design/{self.design}/common.yml")
        self.DESIGN_PDK_CONF = os.getenv('DESIGN_PDK_CONF', f"{self.e2e_dir}/configs-design/{self.design}/{self.pdk}.yml")
        
        # synthesis and par configurations
        self.SYN_CONF = os.getenv('SYN_CONF', f"{self.e2e_dir}/configs-design/{self.design}/syn.yml")
        self.PAR_CONF = os.getenv('PAR_CONF', f"{self.e2e_dir}/configs-design/{self.design}/par.yml")
        
        # This should be your target, build is passed in
        self.makecmdgoals = os.getenv('MAKECMDGOALS', "build")
        
        # simulation and power configurations
        self.SIM_RTL_CONF = os.getenv('SIM_RTL_CONF', f"{self.e2e_dir}/configs-design/{self.design}/sim-rtl.yml")

        self.SIM_SYN_CONF = os.getenv('SIM_SYN_CONF', f"{self.e2e_dir}/configs-design/{self.design}/sim-syn.yml")

        self.SIM_PAR_CONF = os.getenv('SIM_PAR_CONF', f"{self.e2e_dir}/configs-design/{self.design}/sim-par.yml")

        self.POWER_RTL_CONF = os.getenv('POWER_RTL_CONF', f"{self.e2e_dir}/configs-design/{self.design}/power-rtl-{self.pdk}.yml")

        self.POWER_SYN_CONF = os.getenv('POWER_SYN_CONF', f"{self.e2e_dir}/configs-design/{self.design}/power-syn-{self.pdk}.yml")

        self.POWER_PAR_CONF = os.getenv('POWER_PAR_CONF', f"{self.e2e_dir}/configs-design/{self.design}/power-par-{self.pdk}.yml")

        # create project configuration
        self.PROJ_YMLS = [
            self.PDK_CONF, 
            self.TOOLS_CONF, 
            self.DESIGN_CONF, 
            self.DESIGN_PDK_CONF,
            self.SYN_CONF, 
            self.PAR_CONF,
            self.extra
        ]
        
        self.HAMMER_EXTRA_ARGS = ' '.join([f"-p {conf}" for conf in self.PROJ_YMLS if conf]) + f" {self.args}"
        self.HAMMER_D_MK = os.getenv('HAMMER_D_MK', f"{self.OBJ_DIR}/hammer.d")

        # Set up system arguments
        airflow_command = sys.argv[1]
        sys.argv = []
        for arg in [airflow_command, self.makecmdgoals, '--obj_dir', self.OBJ_DIR, '-e', self.ENV_YML]:
            sys.argv.append(arg)
        for arg in self.HAMMER_EXTRA_ARGS.split():
            sys.argv.append(arg)

    def build(self):
        print("Executing build")
        print(f"Using config files:")
        print(f"ENV_YML: {self.ENV_YML}")
        print(f"PDK_CONF: {self.PDK_CONF}")
        print(f"TOOLS_CONF: {self.TOOLS_CONF}")
        print(f"DESIGN_CONF: {self.DESIGN_CONF}")
        print(f"DESIGN_PDK_CONF: {self.DESIGN_PDK_CONF}")
        print(f"SYN_CONF: {self.SYN_CONF}")
        print(f"PAR_CONF: {self.PAR_CONF}")
        print(f"SIM_RTL_CONF: {self.SIM_RTL_CONF}")
        print(f"SIM_SYN_CONF: {self.SIM_SYN_CONF}")
        print(f"SIM_PAR_CONF: {self.SIM_PAR_CONF}")
        print(f"POWER_RTL_CONF: {self.POWER_RTL_CONF}")
        print(f"POWER_SYN_CONF: {self.POWER_SYN_CONF}")
        print(f"POWER_PAR_CONF: {self.POWER_PAR_CONF}")
        
        sys.argv = [
            'hammer-vlsi',
            'build',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML
        ]

        # Add all project configs
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def sim_rtl(self):
        print("Executing sim-rtl")
        sys.argv = [
            'hammer-vlsi',
            'sim-rtl',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML
        ]

        # Add all project configs
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
            
        sys.argv.extend(['-p', self.SIM_RTL_CONF])

        if self.args:
            sys.argv.extend(self.args.split())

        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def sim_rtl_to_power(self):
        print("Executing sim-rtl-to-power")
        sys.argv = [
            'hammer-vlsi',
            'sim-rtl-to-power',
            '--obj_dir', self.OBJ_DIR,
            '-o', self.OBJ_DIR + '/sim-rtl-to-power_input.json',
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/sim-rundir/sim-rtl-output.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
            
        sys.argv.extend(['-p', self.POWER_RTL_CONF])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()
        
    def power_rtl(self):
        print("Executing power-rtl")
        sys.argv = [
            'hammer-vlsi',
            'power-rtl',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/sim-rtl-to-power_input.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
            
        sys.argv.extend(['-p', self.POWER_RTL_CONF])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def syn(self):
        print("Executing synthesis")
        sys.argv = [
            'hammer-vlsi',
            'syn',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def syn_to_par(self):
        print("Executing syn-to-par")
        sys.argv = [
            'hammer-vlsi',
            'syn-to-par',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/syn-rundir/syn-output.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()
    
    def syn_to_sim(self):
        print("Executing syn-to-sim")
        sys.argv = [
            'hammer-vlsi',
            'syn-to-sim',
            '--obj_dir', self.OBJ_DIR,
            '-o', self.OBJ_DIR + '/syn-to-sim_input.json',
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/syn-rundir/syn-output.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
            
        sys.argv.extend(['-p', self.SIM_SYN_CONF])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def sim_syn(self):
        print("Executing sim-syn")
        sys.argv = [
            'hammer-vlsi',
            'sim',
            '--obj_dir', self.OBJ_DIR, #bwrc env yml
            '-e', self.ENV_YML
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
            
        sys.argv.extend(['-p', self.SIM_SYN_CONF])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def sim_syn_to_power(self):
        print("Executing syn-to-sim")
        sys.argv = [
            'hammer-vlsi',
            'sim-syn-to-power',
            '--obj_dir', self.OBJ_DIR,
            '-o', self.OBJ_DIR + '/sim-syn-to-power_input.json',
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/syn-rundir/sim-syn-output.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
            
        sys.argv.extend(['-p', self.POWER_SYN_CONF])
        sys.argv.extend(['-p', self.SIM_SYN_CONF])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def syn_to_power(self):
        print("Executing syn-to-sim")
        sys.argv = [
            'hammer-vlsi',
            'syn-to-power',
            '--obj_dir', self.OBJ_DIR,
            '-o', self.OBJ_DIR + '/syn-to-power_input.json',
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/syn-rundir/syn-output.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
            
        sys.argv.extend(['-p', self.POWER_SYN_CONF])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def power_syn(self):
        print("Executing power_syn")
        sys.argv = [
            'hammer-vlsi',
            'power-syn',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/syn-to-power_input.json',
            '-p', self.OBJ_DIR + '/sim-syn-to-power_input.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
            
        sys.argv.extend(['-p', self.POWER_SYN_CONF])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def syn_to_formal(self):
        print("Executing syn-to-formal")
        sys.argv = [
            'hammer-vlsi',
            'syn-to-formal',
            '--obj_dir', self.OBJ_DIR,
            '-o', self.OBJ_DIR + '/syn-to-formal_input.json',
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/syn-rundir/syn-output.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def formal_syn(self):
        print("Executing formal_syn")
        sys.argv = [
            'hammer-vlsi',
            'formal',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/syn-to-formal_input.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def syn_to_timing(self):
        print("Executing syn-to-timing")
        sys.argv = [
            'hammer-vlsi',
            'syn-to-timing',
            '--obj_dir', self.OBJ_DIR,
            '-o', self.OBJ_DIR + '/syn-to-timing_input.json',
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/syn-rundir/syn-output.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def timing_syn(self):
        print("Executing timing_syn")
        sys.argv = [
            'hammer-vlsi',
            'timing',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/syn-to-timing_input.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def par(self):
        print("Executing par")
        sys.argv = [
            'hammer-vlsi',
            'par',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/par-input.json'
        ]
        
        # Add all project configs
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
        
        if self.args:
            sys.argv.extend(self.args.split())
        
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def par_to_sim(self):
        print("Executing par-to-sim")
        sys.argv = [
            'hammer-vlsi',
            'par-to-sim',
            '--obj_dir', self.OBJ_DIR,
            '-o', self.OBJ_DIR + '/par-to-sim_input.json',
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/par-rundir/par-output.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
            
        sys.argv.extend(['-p', self.SIM_PAR_CONF])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def sim_par(self):
        print("Executing sim-par")
        sys.argv = [
            'hammer-vlsi',
            'sim',
            '--obj_dir', self.OBJ_DIR, #bwrc env yml
            '-e', self.ENV_YML
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
            
        sys.argv.extend(['-p', self.SIM_PAR_CONF])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def sim_par_to_power(self):
        print("Executing par-to-sim")
        sys.argv = [
            'hammer-vlsi',
            'sim-par-to-power',
            '--obj_dir', self.OBJ_DIR,
            '-o', self.OBJ_DIR + '/sim-par-to-power_input.json',
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/par-rundir/sim-par-output.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
            
        sys.argv.extend(['-p', self.POWER_PAR_CONF])
        sys.argv.extend(['-p', self.SIM_PAR_CONF])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def par_to_power(self):
        print("Executing par-to-sim")
        sys.argv = [
            'hammer-vlsi',
            'par-to-power',
            '--obj_dir', self.OBJ_DIR,
            '-o', self.OBJ_DIR + '/par-to-power_input.json',
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/par-rundir/par-output.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
            
        sys.argv.extend(['-p', self.POWER_PAR_CONF])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def power_par(self):
        print("Executing power_par")
        sys.argv = [
            'hammer-vlsi',
            'power-par',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/par-to-power_input.json',
            '-p', self.OBJ_DIR + '/sim-par-to-power_input.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
            
        sys.argv.extend(['-p', self.POWER_PAR_CONF])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def par_to_formal(self):
        print("Executing par-to-formal")
        sys.argv = [
            'hammer-vlsi',
            'par-to-formal',
            '--obj_dir', self.OBJ_DIR,
            '-o', self.OBJ_DIR + '/par-to-formal_input.json',
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/par-rundir/par-output.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def formal_par(self):
        print("Executing formal_par")
        sys.argv = [
            'hammer-vlsi',
            'formal',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/par-to-formal_input.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def par_to_timing(self):
        print("Executing par-to-timing")
        sys.argv = [
            'hammer-vlsi',
            'par-to-timing',
            '--obj_dir', self.OBJ_DIR,
            '-o', self.OBJ_DIR + '/par-to-timing_input.json',
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/par-rundir/par-output.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def timing_par(self):
        print("Executing timing_par")
        sys.argv = [
            'hammer-vlsi',
            'timing',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/par-to-timing_input.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def par_to_drc(self):
        print("Executing par-to-drc")
        sys.argv = [
            'hammer-vlsi',
            'par-to-drc',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/par-rundir/par-output.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def drc(self):
        print("Executing drc")
        sys.argv = [
            'hammer-vlsi',
            'drc',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/drc-input.json'
        ]

        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
        
        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def par_to_lvs(self):
        print("Executing par-to-lvs")
        sys.argv = [
            'hammer-vlsi',
            'par-to-lvs',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/par-rundir/par-output.json'
        ]
        
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])

        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def lvs(self):
        print("Executing lvs")
        sys.argv = [
            'hammer-vlsi',
            'lvs',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.OBJ_DIR + '/drc-input.json'
        ]

        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
        
        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def clean(self):
        print("Executing clean")
        if os.path.exists(self.OBJ_DIR):
            subprocess.run(f"rm -rf {self.OBJ_DIR} hammer-vlsi-*.log", shell=True, check=True)




@dag(
    dag_id='Sledgehammer_demo_gcd',
    start_date=pendulum.datetime(2024, 1, 1, tz="America/Los_Angeles"),
    schedule=None,
    catchup=False,
    tags=["gcd"],
    params={
        'clean': Param(
            default=False,
            type='boolean',
            title='Clean Build Directory',
            description='Clean the build directory before running'
        ),
        'build': Param(
            default=False,
            type='boolean',
            title='Build Design',
            description='Run the build step'
        ),
        'sim_rtl': Param(
            default=False,
            type='boolean',
            title='RTL Simulation',
            description='Run RTL simulation'
        ),
        'power_rtl': Param(
            default=False,
            type='boolean',
            title='RTL Power Simulation',
            description='Run RTL Power simulation'
        ),
        'syn': Param(
            default=False,
            type='boolean',
            title='Synthesis',
            description='Run logic synthesis'
        ),
        'sim_syn': Param(
            default=False,
            type='boolean',
            title='Simulation Synthesis',
            description='Run synthesis simulation'
        ),
        'timing_syn': Param(
            default=False,
            type='boolean',
            title='Timing Synthesis',
            description='Get timing from synthesis'
        ),
        'formal_syn': Param(
            default=False,
            type='boolean',
            title='Formal Synthesis',
            description='Get formal from synthesis'
        ),
        'power_syn': Param(
            default=False,
            type='boolean',
            title='Power Synthesis',
            description='Get power from synthesis'
        ),
        'par': Param(
            default=False,
            type='boolean',
            title='Place and Route',
            description='Run place and route'
        ),
        'drc': Param(
            default=False,
            type='boolean',
            title='Design Rule Check',
            description='Run design rule check'
        ),
        'lvs': Param(
            default=False,
            type='boolean',
            title='Layout Versus Schematic',
            description='Run layout versus schematic'
        ),
        'sim_par': Param(
            default=False,
            type='boolean',
            title='Simulation Place and Route',
            description='Run place and route simulation'
        ),
        'timing_par': Param(
            default=False,
            type='boolean',
            title='Timing Place and Route',
            description='get timing from place and route'
        ),
        'formal_par': Param(
            default=False,
            type='boolean',
            title='Formal Place and Route',
            description='Get formal from place and route'
        ),
        'power_par': Param(
            default=False,
            type='boolean',
            title='Power Place and Route',
            description='Get power from place and route'
        )
    },
    render_template_as_native_obj=True
)
def create_hammer_dag_gcd():
    #@task.branch(trigger_rule=TriggerRule.NONE_FAILED)
    @task.branch(trigger_rule=TriggerRule.ALL_SUCCESS)
    def start(**context):
        """Start task"""
        if context['dag_run'].conf.get('clean', False):
            return "clean"
        elif (context['dag_run'].conf.get('build', False) or 
            context['dag_run'].conf.get('sim_rtl', False) or
            context['dag_run'].conf.get('power_rtl', False) or
            context['dag_run'].conf.get('syn', False) or
            context['dag_run'].conf.get('formal_syn', False) or
            context['dag_run'].conf.get('timing_syn', False) or
            context['dag_run'].conf.get('power_syn', False) or
            context['dag_run'].conf.get('sim_syn', False) or
            context['dag_run'].conf.get('par', False) or
            context['dag_run'].conf.get('power_par', False) or
            context['dag_run'].conf.get('sim_par', False) or
            context['dag_run'].conf.get('formal_par', False) or
            context['dag_run'].conf.get('timing_par', False) or
            context['dag_run'].conf.get('drc', False) or
            context['dag_run'].conf.get('lvs', False)):
            # return "build_decider"
            return "build"
        else:
            return "exit_"

    @task
    def clean(**context):
        """Clean the build directory"""
        print("Starting clean task")
        flow = AIRFlow()
        if os.path.exists(flow.OBJ_DIR):
            subprocess.run(f"rm -rf {flow.OBJ_DIR} hammer-vlsi-*.log", shell=True, check=True)
    
    @task
    def build(**context):
        """Execute build task"""
        print("Starting build task")
        if context['dag_run'].conf.get('build', False):
            print("Build parameter is True, executing build")
            flow = AIRFlow()
            flow.build()
        else:
            print("Build parameter is False, skipping")
            raise AirflowSkipException("Build task skipped")

    #Bug where sim_or_syn_decide is being triggered, even when clean is passed in.
    #Cannot use ONE_SUCCESS bc of start
    #Cannot use NONE_FAILED bc of clean
    #Cannot use ALL_SUCCESS bc of build
    #Cannot use NONE_SKIPPED bc of build
    #Need to either find trigger flag to pass in, so this task runs if build_decider is success or change flow graph
    #@task
    #@task.branch(trigger_rule=TriggerRule.ONE_SUCCESS)
    # @task.branch(trigger_rule=TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS)
    # def sim_or_syn_decide(**context):
    #     """Decide whether to run sim_rtl or syn"""
    #     if context['dag_run'].conf.get('sim_rtl', False):
    #         return 'sim_rtl'
    #     elif (context['dag_run'].conf.get('syn', False) or 
    #         context['dag_run'].conf.get('par', False)):
    #         return 'syn'
    #     return 'exit_'

    @task
    def sim_rtl(**context):
        """Execute RTL simulation task"""
        print("Starting sim_rtl task")
        if context['dag_run'].conf.get('sim_rtl', False):
            print("Sim-RTL parameter is True, executing sim_rtl")
            flow = AIRFlow()
            flow.sim_rtl()
        else:
            print("Sim-RTL parameter is False, skipping")
            raise AirflowSkipException("Sim-RTL task skipped")

    @task
    def power_rtl(**context):
        """Execute power_RTL simulation task"""
        print("Starting power_rtl task")
        if context['dag_run'].conf.get('power_rtl', False):
            print("power_RTL parameter is True, executing power_rtl")
            flow = AIRFlow()
            flow.sim_rtl_to_power()
            flow.power_rtl()
        else:
            print("power_RTL parameter is False, skipping")
            raise AirflowSkipException("power_RTL task skipped")

    @task
    def syn(**context):
        """Execute synthesis task"""
        print("Starting syn task")
        if context['dag_run'].conf.get('syn', False):
            print("Synthesis parameter is True, executing syn")
            flow = AIRFlow()
            flow.syn()
        else:
            print("Synthesis parameter is False, skipping")
            raise AirflowSkipException("Synthesis task skipped")

    @task
    def power_syn(**context):
        """Execute power_synthesis task"""
        print("Starting power_syn task")
        if context['dag_run'].conf.get('power_syn', False):
            print("power_Synthesis parameter is True, executing power_syn")
            flow = AIRFlow()
            flow.syn_to_power()
            flow.sim_syn_to_power()
            flow.power_syn()
        else:
            print("power_Synthesis parameter is False, skipping")
            raise AirflowSkipException("power_Synthesis task skipped")

    @task
    def timing_syn(**context):
        """Execute timing_synthesis task"""
        print("Starting timing_syn task")
        if context['dag_run'].conf.get('timing_syn', False):
            print("timing_Synthesis parameter is True, executing timing_syn")
            flow = AIRFlow()
            flow.syn_to_timing()
            flow.timing_syn()
        else:
            print("timing_Synthesis parameter is False, skipping")
            raise AirflowSkipException("timing_Synthesis task skipped")

    @task
    def formal_syn(**context):
        """Execute formal_synthesis task"""
        print("Starting formal_syn task")
        if context['dag_run'].conf.get('formal_syn', False):
            print("formal_Synthesis parameter is True, executing formal_syn")
            flow = AIRFlow()
            flow.syn_to_formal()
            flow.formal_syn()
        else:
            print("formal_Synthesis parameter is False, skipping")
            raise AirflowSkipException("formal_Synthesis task skipped")

    @task
    def sim_syn(**context):
        """Execute sim_synthesis task"""
        print("Starting sim_syn task")
        if context['dag_run'].conf.get('sim_syn', False):
            print("sim_Synthesis parameter is True, executing sim_syn")
            flow = AIRFlow()
            flow.syn_to_sim()
            flow.sim_syn()
        else:
            print("sim_Synthesis parameter is False, skipping")
            raise AirflowSkipException("sim_Synthesis task skipped")

    @task
    def par(**context):
        """Execute PAR task"""
        print("Starting par task")
        if context['dag_run'].conf.get('par', False):
            print("PAR parameter is True, executing par")
            flow = AIRFlow()
            flow.syn_to_par()
            flow.par()
        else:
            print("PAR parameter is False, skipping")
            raise AirflowSkipException("PAR task skipped")

    @task
    def formal_par(**context):
        """Execute formal_PAR task"""
        print("Starting formal_par task")
        if context['dag_run'].conf.get('formal_par', False):
            print("formal_PAR parameter is True, executing formal_par")
            flow = AIRFlow()
            flow.par_to_formal()
            flow.formal_par()
        else:
            print("formal_PAR parameter is False, skipping")
            raise AirflowSkipException("formal_PAR task skipped")
        
    @task
    def timing_par(**context):
        """Execute timing_PAR task"""
        print("Starting timing_par task")
        if context['dag_run'].conf.get('timing_par', False):
            print("timing_PAR parameter is True, executing timing_par")
            flow = AIRFlow()
            flow.par_to_timing()
            flow.timing_par()
        else:
            print("timing_PAR parameter is False, skipping")
            raise AirflowSkipException("timing_PAR task skipped")
        
    @task
    def sim_par(**context):
        """Execute sim_PAR task"""
        print("Starting sim_par task")
        if context['dag_run'].conf.get('sim_par', False):
            print("sim_PAR parameter is True, executing sim_par")
            flow = AIRFlow()
            flow.par_to_sim()
            flow.sim_par()
        else:
            print("sim_PAR parameter is False, skipping")
            raise AirflowSkipException("sim_PAR task skipped")
        
    @task
    def power_par(**context):
        """Execute Power_PAR task"""
        print("Starting Power_Par task")
        if context['dag_run'].conf.get('power_par', False):
            print("Power_PAR parameter is True, executing Power_Par")
            flow = AIRFlow()
            flow.sim_par_to_power()
            flow.par_to_power()
            flow.power_par()
        else:
            print("Power_PAR parameter is False, skipping")
            raise AirflowSkipException("Power_PAR task skipped")
        
    @task
    def drc(**context):
        """Execute DRC task"""
        print("Starting DRC task")
        if context['dag_run'].conf.get('drc', False):
            print("DRC parameter is True, executing DRC")
            flow = AIRFlow()
            flow.par_to_drc()
            flow.drc()
        else:
            print("DRC parameter is False, skipping")
            raise AirflowSkipException("DRC task skipped")
        
    @task
    def lvs(**context):
        """Execute LVS task"""
        print("Starting LVS task")
        if context['dag_run'].conf.get('lvs', False):
            print("LVS parameter is True, executing LVS")
            flow = AIRFlow()
            flow.par_to_lvs()
            flow.lvs()
        else:
            print("LVS parameter is False, skipping")
            raise AirflowSkipException("LVS task skipped")

    #@task.branch(trigger_rule=TriggerRule.NONE_FAILED)
    # @task.branch(trigger_rule=TriggerRule.ALL_SUCCESS)
    # def build_decider(**context):
    #     """Decide whether to run build"""
    #     if context['dag_run'].conf.get('build', True):
    #         return 'build'
    #     elif (context['dag_run'].conf.get('sim_rtl', False) or
    #         context['dag_run'].conf.get('syn', False) or
    #         context['dag_run'].conf.get('par', False)):
    #         return "sim_or_syn_decide"
    #     return 'exit_'

    # #@task.branch(trigger_rule=TriggerRule.NONE_FAILED)
    # @task.branch(trigger_rule=TriggerRule.ALL_SUCCESS)
    # def syn_decider(**context):
    #     """Decide whether to run synthesis"""
    #     if context['dag_run'].conf.get('syn', False):
    #         return 'syn'
    #     elif (context['dag_run'].conf.get('par', False)):
    #         return "par_decider"
    #     else:
    #         return "exit_"

    # #@task.branch(trigger_rule=TriggerRule.NONE_FAILED)
    # @task.branch(trigger_rule=TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS)
    # def par_decider(**context):
    #     """Decide whether to run par"""
    #     if context['dag_run'].conf.get('par', False):
    #         return 'par'
    #     return 'exit_'

    @task(trigger_rule=TriggerRule.NONE_FAILED)
    def exit_():
        """Exit task"""
        print("Exiting")
        #sys.exit(0)

    # Create task instances
    start = start()
    clean = clean()
    # build_decide = build_decider()
    build = build()
    # sim_or_syn_decide = sim_or_syn_decide()
    sim_rtl = sim_rtl()
    power_rtl = power_rtl()
    # syn_decide = syn_decider()
    syn = syn()
    power_syn = power_syn()
    timing_syn = timing_syn()
    formal_syn = formal_syn()
    sim_syn = sim_syn()
    # par_decide = par_decider()
    par = par()
    power_par = power_par()
    timing_par = timing_par()
    formal_par = formal_par()
    sim_par = sim_par()
    drc = drc()
    lvs = lvs()
    exit_ = exit_()

    # Set up dependencies to ensure deciders always run
    start >> [clean, build, exit_]
    clean >> exit_
    # build_decide >> [build, sim_or_syn_decide, exit_]
    build >> [sim_rtl, syn, exit_]
    sim_rtl >> [power_rtl, exit_]
    power_rtl >> exit_
    # syn_decide >> [syn, par_decide, exit_]
    syn >> [timing_syn, power_syn, formal_syn, sim_syn, par, exit_]
    timing_syn >> exit_
    sim_syn >> [power_syn, exit_]
    formal_syn >> exit_
    power_syn >> exit_
    # par_decide >> [par, exit_]
    par >> [timing_par, power_par, formal_par, sim_par, drc, lvs, exit_]
    power_par >> exit_
    formal_par >> exit_
    sim_par >> [power_par, exit_]
    timing_par >> exit_
    lvs >> exit_
    drc >> exit_

    return {
        'clean': clean,
        # 'build_decide': build_decide,
        'build': build,
        # 'sim_or_syn_decide': sim_or_syn_decide,
        'sim_rtl': sim_rtl,
        'power_rtl': power_rtl,
        # 'syn_decide': syn_decide,
        'syn': syn,
        'power_syn': power_syn,
        'timing_syn': timing_syn,
        'formal_syn': formal_syn,
        'sim_syn': sim_syn,
        # 'par_decide': par_decide,
        'par': par,
        'power_par': power_par,
        'sim_par': sim_par,
        'timing_par': timing_par,
        'formal_par': formal_par,
        'lvs': lvs,
        'drc': drc
    }

# Create the DAG
hammer_dag_gcd = create_hammer_dag_gcd()

class AIRFlow_rocket:
    def __init__(self):
        # minimal flow configuration variables
        self.design = os.getenv('design', 'intel2x2')
        self.pdk = os.getenv('pdk', 'intech22')
        self.tools = os.getenv('tools', 'cm')
        self.env = os.getenv('env', 'intel2x2')
        self.extra = os.getenv('extra', '')  # extra configs
        self.args = os.getenv('args', '')  # command-line args (including step flow control)
        
        # Directory structure
        self.vlsi_dir = os.path.abspath('../e2e/')
        self.specs_abs = os.path.abspath('../../specs')
        self.e2e_dir = os.getenv('e2e_dir', self.vlsi_dir)
        self.specs_dir = os.getenv('specs_dir', self.specs_abs) #Point to specs directory for intel2x2 yml files
        self.OBJ_DIR = os.getenv('OBJ_DIR', f"{self.e2e_dir}/build-{self.pdk}-{self.tools}/{self.design}")
        
        # non-overlapping default configs
        self.ENV_YML = os.getenv('ENV_YML', f"{self.specs_dir}/{self.env}-env.yml") #Point to intel2x2-env.yml
        self.PDK_CONF = os.getenv('PDK_CONF', f"{self.specs_dir}/{self.env}-tech.yml") #Kind of confusing, but the pdk yml file is named intel2x2 instead of intech22
        self.TOOLS_CONF = os.getenv('TOOLS_CONF', f"{self.e2e_dir}/configs-tool/{self.tools}.yml") #Can keep the same for genus/innovus

        # design-specific overrides of default configs
        self.DESIGN_CONF = os.getenv('DESIGN_CONF', f"{self.specs_dir}/{self.design}-design-common.yml") #Point to intel2x2-design-common.yml
        self.DESIGN_PDK_CONF = os.getenv('DESIGN_PDK_CONF', f"{self.specs_dir}/rockettile-design.yml") #Point to rockettile-design.yml, which contains same info as syn.yml (intel2x2)
        
        # synthesis and par configurations
        #self.SYN_CONF = os.getenv('SYN_CONF', f"{self.specs_dir}/rockettile-design.yml") #Point to rockettile-design.yml, which contains same info as syn.yml (intel2x2)
        #self.PAR_CONF = os.getenv('PAR_CONF', f"{self.e2e_dir}/configs-design/{self.design}/par.yml")
        
        # This should be your target, build is passed in
        
        #self.makecmdgoals = os.getenv('MAKECMDGOALS', "build")
        
        ## simulation and power configurations
        #self.SIM_CONF = os.getenv('SIM_CONF',
        #    f"{self.e2e_dir}/configs-design/{self.design}/sim-rtl.yml" if '-rtl' in self.makecmdgoals else
        #    f"{self.e2e_dir}/configs-design/{self.design}/sim-syn.yml" if '-syn' in self.makecmdgoals else
        #    f"{self.e2e_dir}/configs-design/{self.design}/sim-par.yml" if '-par' in self.makecmdgoals else ''
        #)
        #self.POWER_CONF = os.getenv('POWER_CONF',
        #    f"{self.e2e_dir}/configs-design/{self.design}/power-rtl-{self.pdk}.yml" if 'power-rtl' in self.makecmdgoals else
        #    f"{self.e2e_dir}/configs-design/{self.design}/power-syn-{self.pdk}.yml" if 'power-syn' in self.makecmdgoals else
        #    f"{self.e2e_dir}/configs-design/{self.design}/power-par-{self.pdk}.yml" if 'power-par' in self.makecmdgoals else ''
        #)
        
        # create project configuration
        self.PROJ_YMLS = [
            self.PDK_CONF, 
            self.TOOLS_CONF, 
            self.DESIGN_CONF, 
            self.DESIGN_PDK_CONF,
            #self.SYN_CONF, 
            #self.SIM_CONF, 
            #self.POWER_CONF, 
            self.extra
        ]
        
        self.HAMMER_EXTRA_ARGS = ' '.join([f"-p {conf}" for conf in self.PROJ_YMLS if conf]) + f" {self.args}"
        self.HAMMER_D_MK = os.getenv('HAMMER_D_MK', f"{self.OBJ_DIR}/hammer.d")

        # Set up system arguments
        
        #airflow_command = sys.argv[1]
        #sys.argv = []
        #for arg in [airflow_command, self.makecmdgoals, '--obj_dir', self.OBJ_DIR, '-e', self.ENV_YML]:
        #    sys.argv.append(arg)
        #for arg in self.HAMMER_EXTRA_ARGS.split():
        #    sys.argv.append(arg)

    def build(self):
        print("Executing build")
        print(f"Using config files:")
        print(f"ENV_YML: {self.ENV_YML}")
        print(f"PDK_CONF: {self.PDK_CONF}")
        print(f"TOOLS_CONF: {self.TOOLS_CONF}")
        print(f"DESIGN_CONF: {self.DESIGN_CONF}")
        print(f"DESIGN_PDK_CONF: {self.DESIGN_PDK_CONF}")
        
        sys.argv = [
            'hammer-vlsi',
            'build',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.PDK_CONF,
            '-p', self.TOOLS_CONF,
            '-p', self.DESIGN_CONF,
            '-p', self.DESIGN_PDK_CONF
        ]
        
        if self.extra:
            sys.argv.extend(['-p', self.extra])
        
        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def sim_rtl(self):
        print("Executing sim-rtl")
        print(f"Using config files:")
        print(f"ENV_YML: {self.ENV_YML}")
        print(f"PDK_CONF: {self.PDK_CONF}")
        print(f"TOOLS_CONF: {self.TOOLS_CONF}")
        print(f"DESIGN_CONF: {self.DESIGN_CONF}")
        print(f"DESIGN_PDK_CONF: {self.DESIGN_PDK_CONF}")
        
        # Add simulation config
        self.SIM_CONF = (f"{self.specs_dir}/rockettile-inputs.yml") #Point to rockettile-inputs.yml
        print(f"SIM_CONF: {self.SIM_CONF}")
        
        sys.argv = [
            'hammer-vlsi',
            'sim',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.PDK_CONF,
            '-p', self.TOOLS_CONF,
            '-p', self.DESIGN_CONF,
            '-p', self.DESIGN_PDK_CONF,
            '-p', self.SIM_CONF
        ]
        
        if self.extra:
            sys.argv.extend(['-p', self.extra])
        
        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def syn(self):
        print("Executing synthesis")
        print(f"Using config files:")
        print(f"ENV_YML: {self.ENV_YML}")
        print(f"PDK_CONF: {self.PDK_CONF}")
        print(f"TOOLS_CONF: {self.TOOLS_CONF}")
        print(f"DESIGN_CONF: {self.DESIGN_CONF}")
        print(f"DESIGN_PDK_CONF: {self.DESIGN_PDK_CONF}")
        
        # Add synthesis config
        self.SYN_CONF = (f"{self.specs_dir}/rockettile-inputs.yml") #Point to rockettile-inputs.yml
        self.SRAM_CONF = (f"{self.OBJ_DIR}/sram_generator-output.json") #Point to sram_generator-output.json
        print(f"SYN_CONF: {self.SYN_CONF}")
        print(f"SRAM_CONF: {self.SRAM_CONF}")
        
        sys.argv = [
            'hammer-vlsi',
            'syn-RocketTile',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.PDK_CONF,
            '-p', self.TOOLS_CONF,
            '-p', self.DESIGN_CONF,
            '-p', self.DESIGN_PDK_CONF,
            '-p', self.SYN_CONF,
            '-p', self.SRAM_CONF
        ]
        
        if self.extra:
            sys.argv.extend(['-p', self.extra])
        
        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def sram_generator(self):
        print("Executing sram_generator")
        print(f"Using config files:")
        print(f"ENV_YML: {self.ENV_YML}")
        print(f"PDK_CONF: {self.PDK_CONF}")
        print(f"TOOLS_CONF: {self.TOOLS_CONF}")
        print(f"DESIGN_CONF: {self.DESIGN_CONF}")
        print(f"DESIGN_PDK_CONF: {self.DESIGN_PDK_CONF}")
    
        # Add synthesis config
        self.SYN_CONF = (f"{self.specs_dir}/rockettile-inputs.yml") #Point to rockettile-inputs.yml
        self.SRAM_GENERATOR_CONF = (f"{self.OBJ_DIR}/sram_generator-input.yml") #Point to sram_generator-input.yml
        self.SRAM_CONF = (f"{self.OBJ_DIR}/sram_generator-output.json") #Point to sram_generator-output.json
        
        print(f"SYN_CONF: {self.SYN_CONF}")
        
        sys.argv = [
            'hammer-vlsi',
            'sram_generator',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.PDK_CONF,
            '-p', self.TOOLS_CONF,
            '-p', self.DESIGN_CONF,
            '-p', self.DESIGN_PDK_CONF,
            '-p', self.SYN_CONF,
            '-p', self.SRAM_GENERATOR_CONF
        ]
        
        if self.extra:
            sys.argv.extend(['-p', self.extra])
        
        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def syn_to_par(self):
        """
        Generate par-input.json from synthesis outputs if it doesn't exist
        """
        #par_input_json = f"{self.OBJ_DIR}/par-input.json"
        par_input_json = f"{self.OBJ_DIR}/par-RocketTile-input.json"
        
        print("Executing syn-to-par")
        print(f"Using config files:")
        print(f"ENV_YML: {self.ENV_YML}")
        print(f"PDK_CONF: {self.PDK_CONF}")
        print(f"TOOLS_CONF: {self.TOOLS_CONF}")
        print(f"DESIGN_CONF: {self.DESIGN_CONF}")
        print(f"DESIGN_PDK_CONF: {self.DESIGN_PDK_CONF}")
    
        # Add synthesis config
        self.SYN_CONF = (f"{self.specs_dir}/rockettile-inputs.yml") #Point to rockettile-inputs.yml
        #self.SRAM_GENERATOR_CONF = (f"{self.OBJ_DIR}/sram_generator-input.yml") #Point to sram_generator-input.yml
        #self.SRAM_CONF = (f"{self.OBJ_DIR}/sram_generator-output.json") #Point to sram_generator-output.json
        
        print(f"SYN_CONF: {self.SYN_CONF}")
        #par_input_json = "/bwrcq/C/andre_green/chipyard-sledgehammer/vlsi/hammer/e2e/build-intech22-cm/intel2x2/par-RocketTile-input.json"
        syn_output = f"{self.OBJ_DIR}/syn-RocketTile/syn-output-full.json"
        #syn_output = "/bwrcq/C/andre_green/chipyard-sledgehammer/vlsi/hammer/e2e/build-intech22-cm/intel2x2/syn-RocketTile/syn-output-full.json"
        
        sys.argv = [
            'hammer-vlsi',
            'syn-to-par',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.PDK_CONF,
            '-p', self.TOOLS_CONF,
            '-p', self.DESIGN_CONF,
            '-p', self.DESIGN_PDK_CONF,
            '-p', self.SYN_CONF,
            #'-p', self.SRAM_GENERATOR_CONF,
            '-p', syn_output,
            '-o', par_input_json
        ]
        
        if self.extra:
            sys.argv.extend(['-p', self.extra])
        
        if self.args:
            sys.argv.extend(self.args.split())
            
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()
        
        
        '''
        # Only generate if file doesn't exist
        if not os.path.exists(par_input_json):
            print("Generating par-input.json")
            par_config = {
                "vlsi.inputs.placement_constraints": [],
                "vlsi.inputs.gds_merge": True,
                "par.inputs": {
                    #"top_module": self.design,
                    "top_module": "RocketTile",
                    "input_files": [f"{self.OBJ_DIR}/syn-rundir/RocketTile.mapped.v"]
                }
            }
            
            # Write configuration to par-input.json
            with open(par_input_json, 'w') as f:
                json.dump(par_config, f, indent=2)
        '''        
        return par_input_json

    def par(self):
        """Execute PAR flow."""
        # Generate par-input.json
        #par_input_json = self.syn_to_par()

        #self.PAR_CONF = (f"{self.specs_dir}/rockettile-inputs.yml") #Point to rockettile-inputs.yml
        par_input_json = f"{self.OBJ_DIR}/par-RocketTile-input.json"
        
        # Set up command line arguments
        sys.argv = [
            'hammer-vlsi',
            'par-RocketTile',
            '--obj_dir', self.OBJ_DIR,
            '-e', self.ENV_YML,
            '-p', self.PDK_CONF,
            '-p', self.TOOLS_CONF,
            '-p', self.DESIGN_CONF,
            '-p', self.DESIGN_PDK_CONF,
            #'-p', self.PAR_CONF,
            '-p', par_input_json
        ]
        
        # Add all project configs
        for conf in self.PROJ_YMLS:
            if conf:
                sys.argv.extend(['-p', conf])
        
        if self.args:
            sys.argv.extend(self.args.split())
        
        print(f"Running command: {' '.join(sys.argv)}")
        CLIDriver().main()

    def clean(self):
        print("Executing clean")
        if os.path.exists(self.OBJ_DIR):
            subprocess.run(f"rm -rf {self.OBJ_DIR} hammer-vlsi-*.log", shell=True, check=True)




@dag(
    dag_id='Sledgehammer_demo_rocket',
    start_date=pendulum.datetime(2024, 1, 1, tz="America/Los_Angeles"),
    schedule=None,
    catchup=False,
    tags=["rocket"],
    params={
        'clean': Param(
            default=False,
            type='boolean',
            title='Clean Build Directory',
            description='Clean the build directory before running'
        ),
        'build': Param(
            default=False,
            type='boolean',
            title='Build Design',
            description='Run the build step'
        ),
        'sim-rtl': Param(
            default=False,
            type='boolean',
            title='RTL Simulation',
            description='Run RTL simulation'
        ),
        'power-rtl': Param(
            default=False,
            type='boolean',
            title='RTL Power Simulation',
            description='Run RTL Power simulation'
        ),
        'sram_generator': Param(
            default=False,
            type='boolean',
            title='SRAM Generator',
            description='Generate SRAM macros'
        ),
        'syn': Param(
            default=False,
            type='boolean',
            title='Synthesis',
            description='Run logic synthesis'
        ),
        'sim-syn': Param(
            default=False,
            type='boolean',
            title='Simulation Synthesis',
            description='Run synthesis simulation'
        ),
        'timing-syn': Param(
            default=False,
            type='boolean',
            title='Timing Synthesis',
            description='Get timing from synthesis'
        ),
        'formal-syn': Param(
            default=False,
            type='boolean',
            title='Formal Synthesis',
            description='Get formal from synthesis'
        ),
        'power-syn': Param(
            default=False,
            type='boolean',
            title='Power Synthesis',
            description='Get power from synthesis'
        ),
        'par': Param(
            default=False,
            type='boolean',
            title='Place and Route',
            description='Run place and route'
        ),
        'drc': Param(
            default=False,
            type='boolean',
            title='Design Rule Check',
            description='Run design rule check'
        ),
        'lvs': Param(
            default=False,
            type='boolean',
            title='Layout Versus Schematic',
            description='Run layout versus schematic'
        ),
        'sim-par': Param(
            default=False,
            type='boolean',
            title='Simulation Place and Route',
            description='Run place and route simulation'
        ),
        'timing-par': Param(
            default=False,
            type='boolean',
            title='Timing Place and Route',
            description='get timing from place and route'
        ),
        'formal-par': Param(
            default=False,
            type='boolean',
            title='Formal Place and Route',
            description='Get formal from place and route'
        ),
        'power-par': Param(
            default=False,
            type='boolean',
            title='Power Place and Route',
            description='Get power from place and route'
        )
    },
    render_template_as_native_obj=True
)
def create_hammer_dag_rocket():
    #@task.branch(trigger_rule=TriggerRule.NONE_FAILED)
    @task.branch(trigger_rule=TriggerRule.ALL_SUCCESS)
    def start(**context):
        """Start task"""
        if context['dag_run'].conf.get('clean', False):
            return "clean"
        elif (context['dag_run'].conf.get('build', False) or 
            context['dag_run'].conf.get('sim_rtl', False) or
            context['dag_run'].conf.get('sram_generator', False) or
            context['dag_run'].conf.get('syn', False) or
            context['dag_run'].conf.get('par', False)):
            return "build_decider"
        else:
            return "exit_"

    @task
    def clean(**context):
        """Clean the build directory"""
        print("Starting clean task")
        flow = AIRFlow_rocket()
        if os.path.exists(flow.OBJ_DIR):
            subprocess.run(f"rm -rf {flow.OBJ_DIR} hammer-vlsi-*.log", shell=True, check=True)
    
    @task
    def build(**context):
        """Execute build task"""
        print("Starting build task")
        if context['dag_run'].conf.get('build', False):
            print("Build parameter is True, executing build")
            flow = AIRFlow_rocket()
            flow.build()
        else:
            print("Build parameter is False, skipping")
            raise AirflowSkipException("Build task skipped")

    #Bug where sim_or_syn_decide is being triggered, even when clean is passed in.
    #Cannot use ONE_SUCCESS bc of start
    #Cannot use NONE_FAILED bc of clean
    #Cannot use ALL_SUCCESS bc of build
    #Cannot use NONE_SKIPPED bc of build
    #Need to either find trigger flag to pass in, so this task runs if build_decider is success or change flow graph
    #@task
    #@task.branch(trigger_rule=TriggerRule.ONE_SUCCESS)
    @task.branch(trigger_rule=TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS)
    def sim_or_syn_decide(**context):
        """Decide whether to run sim_rtl or syn"""
        if context['dag_run'].conf.get('sim_rtl', False):
            return 'sim_rtl'
        elif (context['dag_run'].conf.get('sram_generator', False) or 
            context['dag_run'].conf.get('syn', False) or
            context['dag_run'].conf.get('par', False)):
            return 'sram_decider'
        return 'exit_'

    @task
    def sim_rtl(**context):
        """Execute RTL simulation task"""
        print("Starting sim_rtl task")
        if context['dag_run'].conf.get('sim_rtl', False):
            print("Sim-RTL parameter is True, executing sim_rtl")
            flow = AIRFlow_rocket()
            flow.sim_rtl()
        else:
            print("Sim-RTL parameter is False, skipping")
            raise AirflowSkipException("Sim-RTL task skipped")

    @task
    def sram_generator(**context):
        """Execute sram generator task"""
        print("Starting sram task")
        if context['dag_run'].conf.get('sram_generator', False):
            print("SRAM parameter is True, executing sram_generator")
            flow = AIRFlow_rocket()
            flow.sram_generator()
        else:
            print("SRAM parameter is False, skipping")
            raise AirflowSkipException("SRAM task skipped")

    #@task.branch(trigger_rule=TriggerRule.NONE_FAILED)
    @task.branch(trigger_rule=TriggerRule.ALL_SUCCESS)
    def sram_decider(**context):
        """Decide whether to run sram generator"""
        if context['dag_run'].conf.get('sram_generator', False):
            return 'sram_generator'
        elif (context['dag_run'].conf.get('syn', False) or
            context['dag_run'].conf.get('par', False)):
            return 'syn_decider'
        return 'exit_'

    @task(trigger_rule=TriggerRule.NONE_FAILED)
    def syn(**context):
        """Execute synthesis task"""
        print("Starting syn task")
        if (context['dag_run'].conf.get('sram_generator', False) or
         context['dag_run'].conf.get('syn', False)):
            print("Synthesis parameter is True, executing syn")
            flow = AIRFlow_rocket()
            flow.syn()
        else:
            print("Synthesis parameter is False, skipping")
            raise AirflowSkipException("Synthesis task skipped")

    @task
    def syn_to_par(**context):
        """Execute PAR task"""
        print("Starting syn-to-par task")
        if context['dag_run'].conf.get('par', False):
            print("PAR parameter is True, executing syn-to-par")
            flow = AIRFlow_rocket()
            flow.syn_to_par()
        else:
            print("PAR parameter is False, skipping")
            raise AirflowSkipException("PAR task skipped")
    
    @task
    def par(**context):
        """Execute PAR task"""
        print("Starting par task")
        if context['dag_run'].conf.get('par', False):
            print("PAR parameter is True, executing par")
            flow = AIRFlow_rocket()
            flow.par()
        else:
            print("PAR parameter is False, skipping")
            raise AirflowSkipException("PAR task skipped")

    #@task.branch(trigger_rule=TriggerRule.NONE_FAILED)
    @task.branch(trigger_rule=TriggerRule.ALL_SUCCESS)
    def build_decider(**context):
        """Decide whether to run build"""
        if context['dag_run'].conf.get('build', True):
            return 'build'
        elif (context['dag_run'].conf.get('sim_rtl', False) or
            context['dag_run'].conf.get('sram_generator', False) or
            context['dag_run'].conf.get('syn', False) or
            context['dag_run'].conf.get('par', False)):
            return "sim_or_syn_decide"
        return 'exit_'

    #@task.branch(trigger_rule=TriggerRule.NONE_FAILED)
    @task.branch(trigger_rule=TriggerRule.ALL_SUCCESS)
    def syn_decider(**context):
        """Decide whether to run synthesis"""
        if context['dag_run'].conf.get('syn', False):
            return 'syn'
        elif (context['dag_run'].conf.get('par', False)):
            return "par_decider"
        else:
            return "exit_"

    #@task.branch(trigger_rule=TriggerRule.NONE_FAILED)
    @task.branch(trigger_rule=TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS)
    def par_decider(**context):
        """Decide whether to run par"""
        if context['dag_run'].conf.get('par', False):
            return 'syn_to_par'
        return 'exit_'

    @task(trigger_rule=TriggerRule.NONE_FAILED)
    def exit_():
        """Exit task"""
        print("Exiting")
        sys.exit(0)

    # Create task instances
    start = start()
    clean = clean()
    build_decide = build_decider()
    build = build()
    sim_or_syn_decide = sim_or_syn_decide()
    sim_rtl = sim_rtl()
    syn_decide = syn_decider()
    sram_decide = sram_decider()
    sram_generator = sram_generator()
    syn = syn()
    par_decide = par_decider()
    syn_to_par = syn_to_par()
    par = par()
    exit_ = exit_()

    # Set up dependencies to ensure deciders always run
    start >> [clean, build_decide, exit_]
    clean >> exit_
    build_decide >> [build, sim_or_syn_decide, exit_]
    build >> sim_or_syn_decide
    sim_or_syn_decide >> [sim_rtl, sram_decide, exit_]
    sim_rtl >> exit_
    sram_decide >> [sram_generator, syn_decide, exit_]
    sram_generator >> syn_decide
    syn_decide >> [syn, par_decide, exit_]
    syn >> par_decide
    par_decide >> [syn_to_par, exit_]
    syn_to_par >> par
    par >> exit_

    return {
        'clean': clean,
        'build_decide': build_decide,
        'build': build,
        'sim_or_syn_decide': sim_or_syn_decide,
        'sim_rtl': sim_rtl,
        'syn_decide': syn_decide,
        'sram_decide': sram_decide,
        'sram_generator': sram_generator,
        'syn': syn,
        'par_decide': par_decide,
        'syn_to_par': syn_to_par,
        'par': par
    }

# Create the DAG
hammer_dag_rocket = create_hammer_dag_rocket()


def main():
    CLIDriver().main()
