import sublime
import sublime_plugin

from Dart import PluginLogger
from Dart.lib.plat import supress_window
from Dart.lib.sdk import DartFormat

from subprocess import PIPE
from subprocess import Popen

_logger = PluginLogger(__name__)


class DartFormatCommand(sublime_plugin.WindowCommand):
    '''Formats the selected text in Sublime Text using `dartfmt`.

    Notes:
    - Can be used as a build system.
    '''
    def run(self, **kwargs):
        view = self.window.active_view()

        for r in reversed(list(view.sel())):
            formatted_text = DartFormat().format(view.substr(r))
            # line_terminator = '\n' if text.endswith('\n') else ''
            view.run_command('dart_replace_region', {
                'region': [r.a, r.b],
                'text': formatted_text,
                'line_terminator': '\n'
                })


class DartReplaceRegion(sublime_plugin.TextCommand):
    def run(self, edit, region, text, line_terminator=''):
        text += line_terminator
        reg = sublime.Region(*region)
        self.view.replace(edit, reg, text)
        self.view.run_command('reindent')
