# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2014-2015 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Misc. utility commands exposed to the user."""

import functools
import types
import traceback
import getpass
import socket
import textwrap
import os.path
import cProfile
import sys

try:
    import hunter
except ImportError:
    hunter = None

from qutebrowser.browser.network import qutescheme
from qutebrowser.utils import (log, objreg, usertypes, message, debug,
                               standarddir)
from qutebrowser.commands import cmdutils, runners, cmdexc
from qutebrowser.config import style
from qutebrowser.misc import consolewidget, guiprocess

from PyQt5.QtCore import pyqtSlot, QUrl, QObject, QTimer
from PyQt5.QtWidgets import QApplication


def init():
    profiler = Profiler(app=QApplication.instance())
    objreg.register('profiler', profiler)


@cmdutils.register(maxsplit=1, no_cmd_split=True, win_id='win_id')
def later(ms: {'type': int}, command, win_id):
    """Execute a command after some time.

    Args:
        ms: How many milliseconds to wait.
        command: The command to run, with optional args.
    """
    if ms < 0:
        raise cmdexc.CommandError("I can't run something in the past!")
    commandrunner = runners.CommandRunner(win_id)
    app = objreg.get('app')
    timer = usertypes.Timer(name='later', parent=app)
    try:
        timer.setSingleShot(True)
        try:
            timer.setInterval(ms)
        except OverflowError:
            raise cmdexc.CommandError("Numeric argument is too large for "
                                      "internal int representation.")
        timer.timeout.connect(
            functools.partial(commandrunner.run_safely, command))
        timer.timeout.connect(timer.deleteLater)
        timer.start()
    except:
        timer.deleteLater()
        raise


@cmdutils.register(maxsplit=1, no_cmd_split=True, win_id='win_id')
def repeat(times: {'type': int}, command, win_id):
    """Repeat a given command.

    Args:
        times: How many times to repeat.
        command: The command to run, with optional args.
    """
    if times < 0:
        raise cmdexc.CommandError("A negative count doesn't make sense.")
    commandrunner = runners.CommandRunner(win_id)
    for _ in range(times):
        commandrunner.run_safely(command)


@cmdutils.register(hide=True, win_id='win_id')
def message_error(win_id, text):
    """Show an error message in the statusbar.

    Args:
        text: The text to show.
    """
    message.error(win_id, text)


@cmdutils.register(hide=True, win_id='win_id')
def message_info(win_id, text):
    """Show an info message in the statusbar.

    Args:
        text: The text to show.
    """
    message.info(win_id, text)


@cmdutils.register(hide=True, win_id='win_id')
def message_warning(win_id, text):
    """Show a warning message in the statusbar.

    Args:
        text: The text to show.
    """
    message.warning(win_id, text)


@cmdutils.register(debug=True)
def debug_crash(typ: {'type': ('exception', 'segfault')}='exception'):
    """Crash for debugging purposes.

    Args:
        typ: either 'exception' or 'segfault'.
    """
    if typ == 'segfault':
        # From python's Lib/test/crashers/bogus_code_obj.py
        co = types.CodeType(0, 0, 0, 0, 0, b'\x04\x71\x00\x00', (), (), (),
                            '', '', 1, b'')
        exec(co)
        raise Exception("Segfault failed (wat.)")
    else:
        raise Exception("Forced crash")


@cmdutils.register(debug=True)
def debug_all_objects():
    """Print a list of  all objects to the debug log."""
    s = debug.get_all_objects()
    log.misc.debug(s)


@cmdutils.register(debug=True)
def debug_cache_stats():
    """Print LRU cache stats."""
    config_info = objreg.get('config').get.cache_info()
    style_info = style.get_stylesheet.cache_info()
    log.misc.debug('config: {}'.format(config_info))
    log.misc.debug('style: {}'.format(style_info))


@cmdutils.register(debug=True)
def debug_console():
    """Show the debugging console."""
    try:
        con_widget = objreg.get('debug-console')
    except KeyError:
        con_widget = consolewidget.ConsoleWidget()
        objreg.register('debug-console', con_widget)
    con_widget.show()


@cmdutils.register(debug=True, maxsplit=0, no_cmd_split=True)
def debug_trace(expr=""):
    """Trace executed code via hunter.

    Args:
        expr: What to trace, passed to hunter.
    """
    if hunter is None:
        raise cmdexc.CommandError("You need to install 'hunter' to use this "
                                  "command!")
    try:
        eval('hunter.trace({})'.format(expr))
    except Exception as e:
        raise cmdexc.CommandError("{}: {}".format(e.__class__.__name__, e))


@cmdutils.register(maxsplit=0, debug=True, no_cmd_split=True)
def debug_pyeval(s):
    """Evaluate a python string and display the results as a web page.

    Args:
        s: The string to evaluate.
    """
    try:
        r = eval(s)
        out = repr(r)
    except Exception:
        out = traceback.format_exc()
    qutescheme.pyeval_output = out
    tabbed_browser = objreg.get('tabbed-browser', scope='window',
                                window='last-focused')
    tabbed_browser.openurl(QUrl('qute:pyeval'), newtab=True)


class Profiler(QObject):

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self._app = app
        self._proc = None
        self._tempdir = os.path.join(standarddir.temp(),
                                     'profiling-{}'.format(getpass.getuser()))
        try:
            os.mkdir(self._tempdir)
        except FileExistsError:
            pass

        snakeviz_script = textwrap.dedent("""
            import sys
            import tornado.ioloop
            from snakeviz import main

            main.app.listen(sys.argv[1])
            tornado.ioloop.IOLoop.instance().start()
        """.strip('\n'))
        self._snakeviz_script_path = os.path.join(self._tempdir, 'run.py')
        with open(self._snakeviz_script_path, 'w') as f:
            f.write(snakeviz_script)

    def _get_port(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('localhost', 0))
        addr, port = s.getsockname()
        s.close()
        return port

    @pyqtSlot()
    def cleanup_proc(self):
        self._proc.terminate()
        self._proc.deleteLater()
        self._proc = None

    def _show_snakeviz(self, win_id):
        """Helper for debug_profile to show the profile with SnakeViz."""
        try:
            import snakeviz
        except ImportError:
            raise cmdexc.CommandError("SnakeViz was not found!")

        if getattr(sys, 'frozen', False):
            raise cmdexc.CommandError("Can't run :profile-show when frozen!")
        elif self._app.profile is None:
            raise cmdexc.CommandError("No profile recorded!")

        port = self._get_port()

        self._proc = guiprocess.GUIProcess(win_id, 'SnakeViz',
                                     parent=QApplication.instance())
        self._proc.finished.connect(self.cleanup_proc)
        self._proc.start(
            sys.executable, [self._snakeviz_script_path, str(port)])

        filename = os.path.join(self._tempdir, 'profile')
        self._app.profile.dump_stats(filename)

        browser = objreg.get('tabbed-browser', scope='window', window=win_id)
        url = QUrl('http://localhost:{}/snakeviz/{}'.format(port, filename))
        QTimer.singleShot(500, lambda: browser.tabopen(url, explicit=True,
                                                       background=False))

    @cmdutils.register(debug=True, win_id='win_id', instance='profiler')
    def debug_profile(self, win_id, cmd, arg=None):
        """Profile qutebrowser's execution.

        Sub-commands:

        * :debug-profile start - Start profiling.
        * :debug-profile stop - Stop profiling.
        * :debug-profile reset - Reset collected profiling data.
        * :debug-profile dump <filename> - Dump profile data to the given file.
        * :debug-profile show - Show profile data (needs SnakeViz).
        * :debug-profile kill - Kill running SnakeViz process.

        Args:
            cmd: The command (start/stop/dump/show)
            arg: The filename for dump, otherwise unused.
            force: Force restarting profile/overriding file.
        """
        if self._app.profile is None and cmd != 'start':
            raise cmdexc.CommandError("Profiling was never started!")

        if cmd == 'start':
            if self._app.profile is None:
                self._app.profile = cProfile.Profile()
            self._app.profile.enable()
        elif cmd == 'stop':
            self._app.profile.disable()
        elif cmd == 'reset':
            self._app.profile = None
        elif cmd == 'dump':
            if arg is None:
                raise cmdexc.CommandError("No filename given!")
            self._app.profile.dump_stats(os.path.expanduser(arg))
        elif cmd == 'show':
            self._show_snakeviz(win_id)
        elif cmd == 'kill':
            if self._proc is None:
                raise cmdexc.CommandError("No process running!")
            self.cleanup_proc()
        else:
            raise cmdexc.commandError("Unknown sub-command {}!".format(cmd))
