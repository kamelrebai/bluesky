try:
    # Try Qt5 first
    from PyQt5.QtCore import QThread, QObject
except ImportError:
    # Else PyQt4 imports
    from PyQt4.QtCore import QThread, QObject
import time

# Local imports
import bluesky as bs
from bluesky import settings, stack
# from bluesky.traffic import Metric
from bluesky.tools import datalog, areafilter, plugin
from bluesky.tools.misc import txt2tim, tim2txt
from . import nodemanager as manager
from .simevents import StackTextEventType, BatchEventType, BatchEvent, \
    SimStateEvent, SimQuitEventType, StackInitEvent


onedayinsec = 24 * 3600  # [s] time of one day in seconds for clock time

# Register settings defaults
settings.set_variable_defaults(simdt=0.05)

class Simulation(QObject):
    # simulation modes
    init, op, hold, end = list(range(4))

    # =========================================================================
    # Functions
    # =========================================================================
    def __init__(self):
        super(Simulation, self).__init__()
        self.running     = True
        self.state       = Simulation.init
        self.prevstate   = None

        # Set starting system time [milliseconds]
        self.syst        = 0.0

        # Benchmark time and timespan [seconds]
        self.bencht      = 0.0
        self.benchdt     = -1.0

        # Starting simulation time [seconds]
        self.simt        = 0.0

        # Simulation timestep [seconds]
        self.simdt       = settings.simdt

        # Simulation timestep multiplier: run sim at n x speed
        self.dtmult      = 1.0

        # Simulated clock time
        self.deltclock   = 0.0
        self.simtclock   = self.simt

        # System timestep [milliseconds]
        self.sysdt       = int(self.simdt / self.dtmult * 1000)

        # Flag indicating running at fixed rate or fast time
        self.ffmode      = False
        self.ffstop      = None

        # Additional modules
        # self.metric      = Metric()

    def doWork(self):
        self.syst  = int(time.time() * 1000.0)

        # Send list of stack functions available in this sim to gui at start
        stackdict = {cmd : val[0][len(cmd) + 1:] for cmd, val in stack.cmddict.items()}
        manager.sendEvent(StackInitEvent(stackdict))

        while self.running:
            if self.state == Simulation.op:
                # Plugins pre-update
                plugin.preupdate(self.simt)
                # Datalog pre-update (communicate current sim time to loggers)
                datalog.preupdate(self.simt)

            # Update screen logic
            bs.scr.update()

            # Simulation starts as soon as there is traffic, or pending commands
            if self.state == Simulation.init:
                if bs.traf.ntraf > 0 or len(stack.get_scendata()[0]) > 0:
                    self.start()
                    if self.benchdt > 0.0:
                        self.fastforward(self.benchdt)
                        self.bencht = time.time()

            if self.state == Simulation.op:
                stack.checkfile(self.simt)

            # Always update stack
            stack.process()

            if self.state == Simulation.op:

                bs.traf.update(self.simt, self.simdt)

                # Update metrics
                # self.metric.update()

                # Update plugins
                plugin.update(self.simt)

                # Update loggers
                datalog.postupdate()

                # Update time for the next timestep
                self.simt += self.simdt

            # Update clock
            self.simtclock = (self.deltclock + self.simt) % onedayinsec

            # Process Qt events
            manager.processEvents()

            # When running at a fixed rate, or when in hold/init, increment
            # system time with sysdt and calculate remainder to sleep
            if not self.ffmode or not self.state == Simulation.op:
                self.syst += self.sysdt
                remainder = self.syst - int(1000.0 * time.time())

                if remainder > 0:
                    QThread.msleep(remainder)

            # If running in fast-time with an end-time that has passed, go back to
            # real-time running.
            elif self.ffstop is not None and self.simt >= self.ffstop:
                # If this fast-time section was part of a benchmark, send
                # message with benchmark results
                if self.benchdt > 0.0:
                    bs.scr.echo('Benchmark complete: %d samples in %.3f seconds.' % (bs.scr.samplecount, time.time() - self.bencht))
                    self.benchdt = -1.0
                    self.pause()
                else:
                    self.start()

            # Inform main of our state change
            if not self.state == self.prevstate:
                self.sendState()
                self.prevstate = self.state

    def stop(self):
        self.state = Simulation.end
        datalog.reset()
        if settings.is_headless:
            self.running = False

    def start(self):
        if self.ffmode:
            self.syst = int(time.time() * 1000.0)
        self.ffmode   = False
        self.state    = Simulation.op

    def pause(self):
        self.state = Simulation.hold

    def reset(self):
        self.simt      = 0.0
        self.deltclock = 0.0
        self.simtclock = self.simt
        self.state     = Simulation.init
        self.ffmode    = False
        plugin.reset()
        bs.navdb.reset()
        bs.traf.reset()
        stack.reset()
        datalog.reset()
        areafilter.reset()
        bs.scr.reset()

    def quit(self):
        self.running = False

    def setDt(self, dt):
        self.simdt = abs(dt)
        self.sysdt = int(self.simdt / self.dtmult * 1000)

    def setDtMultiplier(self, mult):
        self.dtmult = mult
        self.sysdt  = int(self.simdt / self.dtmult * 1000)

    def setFixdt(self, flag, nsec=None):
        if flag:
            self.fastforward(nsec)
        else:
            self.start()

    def fastforward(self, nsec=None):
        self.ffmode = True
        if nsec is not None:
            self.ffstop = self.simt + nsec
        else:
            self.ffstop = None

    def benchmark(self, fname='IC', dt=300.0):
        stack.ic(fname)
        self.bencht  = 0.0  # Start time will be set at next sim cycle
        self.benchdt = dt

    def sendState(self):
        manager.sendEvent(SimStateEvent(self.state))

    def addNodes(self, count):
        manager.addNodes(count)

    def batch(self, filename):
        # The contents of the scenario file are meant as a batch list: send to manager and clear stack
        result = stack.openfile(filename)
        scentime, scencmd = stack.get_scendata()
        if result is True:
            manager.sendEvent(BatchEvent(scentime, scencmd))
        self.reset()
        return result

    def event(self, event):
        # Keep track of event processing
        event_processed = False

        if event.type() == StackTextEventType:
            # We received a single stack command. Add it to the existing stack
            stack.stack(event.cmdtext, event.sender_id)
            event_processed = True

        elif event.type() == BatchEventType:
            # We are in a batch simulation, and received an entire scenario. Assign it to the stack.
            self.reset()
            stack.set_scendata(event.scentime, event.scencmd)
            self.start()
            event_processed     = True
        elif event.type() == SimQuitEventType:
            # BlueSky is quitting
            self.quit()
        else:
            # This is either an unknown event or a gui event.
            event_processed = bs.scr.event(event)

        return event_processed

    def setclock(self, txt=""):
        """ Set simulated clock time offset"""
        if txt == "":
            pass  # avoid error message, just give time

        elif txt.upper() == "RUN":
            self.deltclock = 0.0
            self.simtclock = self.simt

        elif txt.upper() == "REAL":
            tclock = time.localtime()
            self.simtclock = tclock.tm_hour * 3600. + tclock.tm_min * 60. + tclock.tm_sec
            self.deltclock = self.simtclock - self.simt

        elif txt.upper() == "UTC":
            utclock = time.gmtime()
            self.simtclock = utclock.tm_hour * 3600. + utclock.tm_min * 60. + utclock.tm_sec
            self.deltclock = self.simtclock - self.simt

        elif txt.replace(":", "").replace(".", "").isdigit():
            self.simtclock = txt2tim(txt)
            self.deltclock = self.simtclock - self.simt
        else:
            return False, "Time syntax error"

        return True, "Time is now " + tim2txt(self.simtclock)
