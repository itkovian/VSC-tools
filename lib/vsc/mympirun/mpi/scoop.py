##
# Copyright 2009-2012 Stijn De Weirdt
#
# This file is part of VSC-tools,
# originally created by the HPC team of the University of Ghent (http://ugent.be/hpc).
#
#
# http://github.com/hpcugent/VSC-tools
#
# VSC-tools is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation v2.
#
# EasyBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with VSC-tools. If not, see <http://www.gnu.org/licenses/>.
##

"""
SCOOP support
    http://code.google.com/p/scoop/
    based on 0.5.3 code

This is not a MPI implementation at all.

Code is very lightweight.
"""
import sys
import os
from scoop.broker import Broker
from threading import Thread
from vsc.mympirun.mpi.mpi import MPI, _versioncheck
from vsc.utils.run import run_simple



from vsc.fancylogger import getLogger
_logger = getLogger("SCOOP")

try:
    import scoop
except:
    _logger.raiseException("SCOOP requires the scoop module and scoop requires (amongst others) pyzmq")

## requires Python 2.6 at least (str.format)
if sys.version_info[0] == 2 and sys.version_info[1] < 6:
    _logger.raiseException("SCOOP requires python 2.6 or later")

class SCOOP(MPI, MyLaunchScoop):
    """Re-implement the launchScoop class from scoop.__main__"""
    SCOOP_WORKER_DIGITS = 5 ## 100k workers
    SCOOP_DEFAULT_EXECUTABLE =
    SCOOP_BOOTSTRAP_MODULE = 'vsc.mympirun.mpi.scoop'  ## this module # used to be "scoop.bootstrap.__main__"

    _mpiscriptname_for = ['myscoop']

    RUNTIMEOPTION = {'options':{'tunnel':("Activate ssh tunnels to route toward the broker "
                                          "sockets over remote connections (may eliminate "
                                          "routing problems and activate encryption but "
                                          "slows down communications)", None, "store_true", False),
                                'broker':("The externally routable broker hostname / ip "
                                          "(defaults to the local hostname)", "str", "store", None),
                                },
                     'prefix':'scoop',
                     'description': ('SCOOP options', 'Advanced options specific for SCOOP'),
                     }
    def __init__(self, options, cmdargs, **kwargs):
        super(SCOOP, self).__init__()

        ## all SCOOP options are ready can be added on command line ? (add them to RUNTIMEOPTION)
        self.scoop_size = self.options.getattr('scoop_size', self.mpitotalppn * self.nr_uniquenodes)
        self.scoop_hosts = self.options.getattr('scoop_hosts', self.mpinodes)
        self.scoop_python = self.options.getattr('scoop_python', sys.executable)

        self.scoop_executable = self.options.getattr('scoop_executable', self.SCOOP_DEFAULT_EXECUTABLE)
        self.scoop_args = self.options.getattr('scoop_args', [])

        self.scoop_nice = self.options.getattr('scoop_nice', 0)
        self.scoop_affinity = self.options.getattr('scoop_affinity', None)
        self.scoop_affinity = self.options.getattr('scoop_path', os.getcwd())

        ## default broker is first of unique nodes ?
        self.scoop_broker = self.options.getattr('scoop_broker', self.uniquenodes[0])
        self.scoop_brokerport = self.options.getattr('scoop_brokerport', None)

        self.scoop_infobroker = self.options.getattr('scoop_infobroker', self.scoop_broker)
        self.scoop_infoport = self.options.getattr('scoop_brokerport', None)

        self.scoop_origin = self.options.getattr('scoop_origin', False)
        self.scoop_debug = self.options.getattr('scoop_debug', self.options.debug)

        self.scoop_tunnel = self.options.getattr('scoop_tunnel', False)

        self.scoop_remote = {}
        self.scoop_workers_free = None


    def main(self):
        """Main method"""
        self.prepare()

        self.scoop_prepare()
        self.scoop_run()

        self.cleanup()

    def scoop_prepare(self):
        """Prepare the scoop parameters and commands"""
        ## self.mpinodes is the node list to use
        if self.options.broker is None:
            if self.mpdboot_localhost_interface is None:
                self.mpdboot_set_localhost_interface()
            self.options.broker = self.mpdboot_localhost_interface[0]

        if self.scoop_size is None:
            self.scoop_size = self.mpitotalppn * self.nr_uniquenodes
        if self.scoop_hosts is None:
            self.scoop_hosts = self.mpinodes

        if self.scoop_broker is None:
            ## default broker is first of unique nodes ?
            self.scoop_broker = self.uniquenodes[0]

        if self.scoop_infobroker:
            self.scoop_infobroker = self.scoop_broker

    def scoop_get_origin(self):
        """origin"""
        if self.scoop_workers_free == 1:
            self.log.debug('scoop_get_origin: set origin on')
            return "--origin"

    def scoop_get_debug(self):
        """debug"""
        if self.options.debug or self.scoop_debug:
            self.log.debug('scoop_get_debug: set debug on')
            return "--debug"

    def scoop_launch_foreign(self, w_id, affintiy=None):
        """Create the foreign launch command
            similar to __main__.launchForeign
                assumes nodes can ssh into themself
            w_id is the workerid
        """
        if affinity is None:
            cmd_affinity = []
        else:
            cmd_affinity = ["--affinity", affinity]
        c = [self.scoop_python,
             "-m ", self.SCOOP_BOOTSTRAP_MODULE,
             "--workerName", "worker{0:0{width}}".format(w_id, width=self.WORKER_DIGITS),
             "--brokerName", "broker",
             "--brokerAddress", "tcp://{brokerHostname}:{brokerPort}".format(
                                        brokerHostname=self.scoop_broker,
                                        brokerPort=self.scoop_brokerport),
             "--metaAddress", "tcp://{infobrokerHostname}:{infoPort}".format(
                                        infobrokerHostname=self.scoop_infobroker,
                                        infoPort=self.scoop_infoport),
             "--size", str(self.scoop_size),
             "--startfrom", self.scoop_path,
             "--nice", self.scoop_nice,
             self.get_origin(),
             self.get_debug(),
             ] + cmd_affinity + [self.scoop_executable] + self.scoop_args
        self.log.debug("scoop_launch_foreign: command c %s" % c)
        return c


    def scoop_start_broker(self):
        """Starts a broker on random unoccupied port(s)"""
        if self.scoop_broker in self.uniquenodes:
            self.log.debug("scoop_start_broker: broker %s in current nodeset, starting locally" % self.scoop_broker)
            self.local_broker = Broker(debug=self.scoop_debug)
            self.scoop_brokerport, self.scoop_infoport = self.local_broker.getPorts()
            self.local_broker_process = Thread(target=self.local_broker.run)
            self.local_broker_process.daemon = True
            self.local_broker_process.start()
        else:
            ## try to start it remotely ?
            ## TODO: see if we can join an existing broker (better yet, lets assume it is running and try to guess the ports)
            self.log.raiseException("scoop_start_broker: remote code not implemented")


    def scoop_launch(self):
        # Launching the local broker, repeat until it works
        self.log.debug("scoop_run: initialising local broker.")
        self.start_broker()
        self.log.debug("scoop_run: local broker launched on borkerport {0}, infoport {1}"
                      ".".format(self.scoop_brokerport, self.scoop_infoport))

        # Launch the workers in mpitotalppn batches on each unique node
        if self.scoop_workers_free is None:
            self.scoop_workers_free = len(mpinodes)

        shell = None
        w_id = -1
        for host in self.uniquehosts:
            command = []
            for n in range(min(self.scoop_workers_free, self.mpitotalppn)):
                self.scoop_workers_free -= 1
                w_id += 1
                command.append(self.make_launch_foreign(w_id, affinity=affinity))
            # Launch every remote hosts at the same time
            if len(command) != 0:
                ssh_command = ['ssh', '-x', '-n', '-oStrictHostKeyChecking=no']
                if self.scoop_tunnel:
                    self.log.debug("run: adding ssh tunnels for broker and info port ")
                    ssh_command += ['-R {0}:127.0.0.1:{0}'.format(self.scoop_brokerport),
                                    '-R {0}:127.0.0.1:{0}'.format(self.scoop_infoport)
                                    ]
                print_bash_pgid = 'ps -o pgid= -p $BASHPID'  # print bash group id to track it for kill
                full_cmd = " ".join([" ".join(cmd + ['&']) for cmd in command]) ## join all commands as background process
                bash_cmd = " ".join([print_bash_pgid, '&&', full_cmd])

                shell = subprocess.Popen(sh_command + [host, "bash", "-c", "'%s'" % bash_cmd],
                                         stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE)
                self.scoop_remote[shell] = [host]
                if self.scoop_workers_free == 0:
                    break

        self.log.debug("scoop_run: started on %s remotes, free workers %s" % (len(self.scoop_remote), self.scoop_workers_free))

        # Get group id from remote connections
        for remote in self.scoop_remote.keys():
            gid = remote.stdout.readline().strip()
            self.scoop_remote[sub_process].append(gid)

        # Wait for the root program
        # shell is last one, containing the origin
        data = shell.stdout.read(1)
        while len(data) > 0:
            sys.stdout.write(data)
            sys.stdout.flush()
            ## one byte at a time ? TODO: use async reader
            data = rootProcess.stdout.read(1)

    def scoop_close(self):
        # Ensure everything is cleaned up on exit
        logging.debug('scoop_close: destroying remote elements...')
        self.local_broker_process

        for data in self.scoop_remote.values():
            if len(data) > 1:
                host, pid = data
                ssh_command = ['ssh', '-x', '-n', '-oStrictHostKeyChecking=no', host]
                kill_cmd = "kill -9 -%s &>/dev/null"

                self.log.debug("scoop_close: host %s kill %s" % (host, kill_cmd))
                subprocess.Popen(ssh_command + ["bash", "-c", "'%s'" % kill_cmd]).wait()
            else:
                self.log.error('scoop_close: zombie process left')

        self.log.info('scoop_close: finished destroying spawned subprocesses.')


    def run(self):
        """Run the launcher"""

        ## previous scoop.__main__ main()
        try:
            self.scoop_launch()
        finally:
            self.scoop_close()

