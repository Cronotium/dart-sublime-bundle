import sublime
import sublime_plugin

from collections import defaultdict
from datetime import datetime
from subprocess import PIPE
from subprocess import Popen
import json
import os
import queue
import threading
import time

from . import PluginLogger
from .lib.analyzer import actions
from .lib.analyzer import requests
from .lib.analyzer.response import Response
from .lib.path import find_pubspec_path
from .lib.path import is_view_dart_script
from .lib.path import is_active
from .lib.plat import supress_window
from .lib.sdk import SDK


_logger = PluginLogger(__name__)


_SERVER_START_DELAY = 2500
_SIGNAL_STOP = object()


g_edits_lock = threading.Lock()
g_server = None
g_server_ready = threading.RLock()

# maps:
#   req_type => view_id
#   view_id => valid token for this type of request
g_req_to_resp = {
    "search": {},
}


def init():
    '''Start up core components of the analyzer plugin.
    '''
    global g_server
    _logger.debug('starting dart analyzer')

    try:
        with g_server_ready:
            g_server = AnalysisServer()
            g_server.start()
    except Exception as e:
        print('Dart: Exception occurred during init. Aborting')
        print('==============================================')
        print(e.message)
        print('==============================================')
        return

    print('Dart: Analyzer started.')


def plugin_loaded():
    # FIXME(guillermooo): Ignoring, then de-ignoring this package throws
    # errors.
    # Make ST more responsive on startup --- also helps the logger get ready.
    sublime.set_timeout(init, _SERVER_START_DELAY)


def plugin_unloaded():
    # The worker threads handling requests/responses block when reading their
    # queue, so give them something.
    g_server.requests.put({'_internal': _SIGNAL_STOP})
    g_server.responses.put({'_internal': _SIGNAL_STOP})
    g_server.stop()


class ActivityTracker(sublime_plugin.EventListener):
    """After ST has been idle for an interval, sends requests to the analyzer
    if the buffer has been saved or is dirty.
    """
    edits = defaultdict(lambda: 0)

    def on_idle(self, view):
        _logger.debug("active view was idle; could send requests")
        # FIXME(guillermooo): This does not correctly detect the active view
        # when there are multiple view groups open.
        if view.is_dirty() and is_active(view):
            _logger.debug('sending overlay data for %s', view.file_name())
            data = {'type': 'add', 'content': view.substr(sublime.Region(0,
                                                                view.size()))}
            g_server.send_update_content(view, data)


    def on_modified(self, view):
        if not is_view_dart_script(view):
            return

        if not view.file_name():
            _logger.debug(
                'aborting because file does not exist on disk: %s',
                view.file_name())
            return

        with g_edits_lock:
            ActivityTracker.edits[view.buffer_id()] += 1
            sublime.set_timeout(lambda: self.check_idle(view), 1000)

    def check_idle(self, view):
        # TODO(guillermooo): we need to send requests too if the buffer is
        # simply dirty but not yet saved.
        with g_edits_lock:
            self.edits[view.buffer_id()] -= 1
            if self.edits[view.buffer_id()] == 0:
                self.on_idle(view)

    def on_post_save(self, view):
        if not is_view_dart_script(view):
            _logger.debug('on_post_save - not a dart file %s',
                          view.file_name())
            return

        with g_edits_lock:
            # TODO(guillermooo): does .buffer_id() uniquely identify buffers
            # across windows?
            ActivityTracker.edits[view.buffer_id()] += 1
            sublime.set_timeout(lambda: self.check_idle(view), 1000)

        # The file has been saved, so force use of filesystem content.
        data = {"type": "remove"}
        g_server.send_update_content(view, data)

    def on_activated(self, view):
        # TODO(guillermooo): We need to updateContent here if the file is
        # dirty on_activated.
        if not is_view_dart_script(view):
            _logger.debug('on_activated - not a dart file %s',
                          view.file_name())
            return

        with g_server_ready:
            if g_server:
                g_server.add_root(view.file_name())
            else:
                sublime.set_timeout(
                    lambda: g_server.add_root(view.file_name()),
                                              _SERVER_START_DELAY + 1000)


class StdoutWatcher(threading.Thread):
    def __init__(self, server, path):
        super().__init__()
        self.path = path
        self.server = server

    def start(self):
        _logger.info("starting StdoutWatcher")
        while True:
            data = self.server.proc.stdout.readline().decode('utf-8')
            _logger.debug('data read from server: %s', repr(data))

            if not data:
                if self.server.proc.stdin.closed:
                    _logger.info(
                        'StdoutWatcher is exiting by internal request')
                    return

                _logger.debug("StdoutWatcher - no data")
                time.sleep(.25)
                continue

            self.server.responses.put(json.loads(data))
        _logger.error('StdoutWatcher exited unexpectedly')


class AnalysisServer(object):
    ping_lock = threading.Lock()
    server = None

    @staticmethod
    def ping(self):
        with AnalysisServer.ping_lock:
            return not self.proc.stdin.closed

    def __init__(self, path=None):
        super().__init__()
        self.path = path
        self.proc = None
        self.roots = []
        self.requests = queue.Queue()
        self.responses = queue.Queue()

        reqh = RequestHandler(self)
        reqh.daemon = True
        reqh.start()

        resh = ResponseHandler(self)
        resh.daemon = True
        resh.start()

    def new_token(self):
        w = sublime.active_window()
        v = w.active_view()
        now = datetime.now()
        token = w.id(), v.id(), '{}:{}:{}'.format(w.id(), v.id(),
                                  now.minute * 60 + now.second)
        return token

    def add_root(self, path):
        """Adds `path` to the monitored roots if it is unknown.

        If a `pubspec.yaml` is found in the path, its parent is monitored.
        Otherwise the passed-in directory name is monitored.

        @path
          Can be a directory or a file path.
        """
        if not path:
            _logger.debug('not a valid path: %s', path)
            return

        p, found = find_pubspec_path(path)
        if not found:
            _logger.debug('did not found pubspec.yaml in path: %s', path)

        if p not in self.roots:
            _logger.debug('adding new root: %s', p)
            self.roots.append(p)
            self.send_set_roots(self.roots)
            return

        _logger.debug('root already known: %s', p)

    def start(self):
        # TODO(guillermooo): create pushcd context manager in lib/path.py.
        old = os.curdir
        # TODO(guillermooo): catch errors
        sdk = SDK()
        with AnalysisServer.ping_lock:
            os.chdir(sdk.path_to_sdk)
            _logger.info('starting AnalysisServer')
            self.proc = Popen(['dart',
                               sdk.path_to_analysis_snapshot,
                               '--sdk={0}'.format(sdk.path_to_sdk)],
                               stdout=PIPE, stdin=PIPE, stderr=PIPE,
                               startupinfo=supress_window())
            os.chdir(old)
        t = StdoutWatcher(self, sdk.path_to_sdk)
        # Thread dies with the main thread.
        t.daemon = True
        sublime.set_timeout_async(t.start, 0)

    def stop(self):
        # TODO(guillermooo): Use the server's own shutdown mechanism.
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.proc.kill()

    def send(self, data):
        data = (json.dumps(data) + '\n').encode('utf-8')
        _logger.debug('sending %s', data)
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def send_set_roots(self, included=[], excluded=[]):
        _, _, token = self.new_token()
        req = requests.set_roots(token, included, excluded)
        _logger.info('sending set_roots request')
        self.requests.put(req)

    def send_find_top_level_decls(self, pattern):
        w_id, v_id, token = self.new_token()
        req = requests.find_top_level_decls(token, pattern)
        _logger.info('sending top level decls request')
        # track this type of req as it may expire
        g_req_to_resp['search']["{}:{}".format(w_id, v_id)] = token
        self.requests.put(req)

    def send_update_content(self, view, data):
        w_id, v_id, token = self.new_token()
        files = {view.file_name(): data}
        req = requests.update_content(token, files)
        _logger.info('sending update content request')
        # track this type of req as it may expire
        self.requests.put(req)


class ResponseHandler(threading.Thread):
    """ Handles responses from the analysis server.
    """
    def __init__(self, server):
        super().__init__()
        self.server = server

    def run(self):
        _logger.info('starting ResponseHandler')
        while True:
            time.sleep(.25)
            try:
                item = self.server.responses.get(0.1)

                if item.get('_internal') == _SIGNAL_STOP:
                    _logger.info(
                        'ResponseHandler is exiting by internal request')
                    continue

                try:
                    resp = Response(item)
                    if resp.type == '<unknown>':
                        _logger.info('received unknown type of response')
                        if resp.has_new_id:
                            _logger.debug('received new id for request: %s -> %s', resp.id, resp.new_id)
                            win_view = resp.id.index(":", resp.id.index(":") + 1)
                            g_req_to_resp["search"][resp.id[:win_view]] = \
                                                                resp.new_id

                        continue

                    if resp.type == 'search.results':
                        _logger.info('received search results')
                        _logger.debug('results: %s', resp.search_results)
                        continue

                    if resp.type == 'analysis.errors':
                        if resp.has_errors and len(resp.errors) > 0:
                            _logger.info('error data received from server')
                            sublime.set_timeout(
                                lambda: actions.display_error(resp.errors), 0)
                            continue
                        else:
                            v = sublime.active_window().active_view()
                            if resp.errors.file and (resp.errors.file == v.file_name()):
                                sublime.set_timeout(actions.clear_ui, 0)
                                continue

                    elif resp.type == 'server.status':
                        info = resp.status
                        sublime.set_timeout(lambda: sublime.status_message(
                                            "Dart: " + info.message))
                except Exception as e:
                    _logger.debug(e)
                    print('Dart: exception while handling response.')
                    print('========================================')
                    print(e.message)
                    print('========================================')

            except queue.Empty:
                pass


class RequestHandler(threading.Thread):
    """ Handles requests to the analysis server.
    """
    def __init__(self, server):
        super().__init__()
        self.server = server

    def run(self):
        _logger.info('starting RequestHandler')
        while True:
            time.sleep(.25)
            try:
                item = self.server.requests.get(0.1)

                if item.get('_internal') == _SIGNAL_STOP:
                    _logger.info(
                        'RequestHandler is exiting by internal request')
                    return

                self.server.send(item)
            except queue.Empty:
                pass
