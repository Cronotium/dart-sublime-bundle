import sublime
import sublime_plugin
import os
import subprocess
import threading
import re
import locale
from xml.etree import ElementTree

locale.setlocale(locale.LC_ALL, '')

GUTTER_Icon = {
    'dartlint_ERROR': 'Packages/Dart/gutter/dartlint-color-error3.png',
    'dartlint_WARNING': 'Packages/Dart/gutter/dartlint-color-warning3.png',
    'dartlint_INFO': 'Packages/Dart/gutter/dartlint-color-info3.png'}

SCOPES_Dartlint = {
    'dartlint.mark.error': {
        'flags':
        sublime.DRAW_EMPTY |
        sublime.DRAW_NO_OUTLINE |
        sublime.DRAW_NO_FILL |
        sublime.DRAW_SQUIGGLY_UNDERLINE |
        sublime.DRAW_EMPTY_AS_OVERWRITE,
        'style': '''
        <dict>
            <key>name</key>
            <string>Dartlint Error</string>
            <key>scope</key>
            <string>dartlint.mark.error</string>
            <key>settings</key>
            <dict>
                <key>foreground</key>
                <string>#C7321C</string>
            </dict>
        </dict>

    '''
    },
    'dartlint.mark.warning': {
        'flags':
        sublime.DRAW_EMPTY |
        sublime.DRAW_NO_OUTLINE |
        sublime.DRAW_NO_FILL |
        sublime.DRAW_SQUIGGLY_UNDERLINE |
        sublime.DRAW_EMPTY_AS_OVERWRITE,
        'style': '''
        <dict>
            <key>name</key>
            <string>Dartlint Warning</string>
            <key>scope</key>
            <string>dartlint.mark.warning</string>
            <key>settings</key>
            <dict>
                <key>foreground</key>
                <string>#F18512</string>
            </dict>
        </dict>

    '''
    },
    'dartlint.mark.info': {
        'flags':
        sublime.DRAW_EMPTY |
        sublime.DRAW_NO_OUTLINE |
        sublime.DRAW_NO_FILL |
        sublime.DRAW_SQUIGGLY_UNDERLINE |
        sublime.DRAW_EMPTY_AS_OVERWRITE,
        'style': '''
        <dict>
            <key>name</key>
            <string>Dartlint Info</string>
            <key>scope</key>
            <string>dartlint.mark.info</string>
            <key>settings</key>
            <dict>
                <key>foreground</key>
                <string>#0000FC</string>
            </dict>
        </dict>

    '''
    }
}

THEME_Head = '''<?xml version="1.0" encoding="{}"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
'''


class DartLint(sublime_plugin.EventListener):
    def __init__(self, *args, **kwargs):
        sublime_plugin.EventListener.__init__(self, *args, **kwargs)
        print("Dart lint active.")

    def on_post_save(self, view):
        self.check_theme()
        fileName = view.file_name()
        if view.file_name().endswith('.dart') is False:
            return
        print("Dart lint: Running dartanalyzer on ", fileName)
        # run dartanalyzer in its own thread
        RunDartanalyzer(view, fileName)

    def check_theme(self):
        # Get the current theme
        system_prefs = sublime.load_settings('Preferences.sublime-settings')
        theme = system_prefs.get('color_scheme')
        theme_xml = sublime.load_resource(theme)
        append_xml = False
        append_scopes = []
        # Check for required scopes
        for scopes in SCOPES_Dartlint:
            if theme_xml.find(scopes) is -1:
                # append to xml
                append_xml = True
                append_scopes.append(scopes)
                print('%s not found in theme' % scopes)
        plist = ElementTree.XML(theme_xml)
        styles = plist.find('./dict/array')
        # Add missing elements
        if append_xml:
            for s2append in append_scopes:
                styles.append(
                    ElementTree.fromstring(SCOPES_Dartlint[s2append]['style']))
        else:
            # No need to do anything
            return
        # write back to 'Packages/User/<theme> DL.tmTheme'
        original_name = os.path.splitext(os.path.basename(theme))[0]
        new_name = original_name + ' DL'
        theme_path = os.path.join(sublime.packages_path(),
                                  'User', new_name + '.tmTheme')
        with open(theme_path, 'w', encoding='utf8') as f:
            f.write(THEME_Head.format('UTF-8'))
            f.write(ElementTree.tostring(plist, encoding='unicode'))

        # Set the amended color scheme to the current color scheme
        path = os.path.join('User', os.path.basename(theme_path))
        prep_path = FormRelativePath(path)
        if prep_path is not False:
            system_prefs.set('color_scheme', prep_path)
            sublime.save_settings('Preferences.sublime-settings')
            print('Created: %s' % prep_path)


def FormRelativePath(path):
    new_path = '/'
    # Dir exists?
    if os.path.isabs(path) and \
            os.path.exists(os.path.dirname(path)) is False:
        print('%s Does not exist' % path)
        return False
    elif os.path.exists(os.path.join(sublime.packages_path(),
                        os.path.dirname(path))) is False:
        print('%s Does not exist' % os.path.join(sublime.packages_path(),
              os.path.dirname(path)))
        return False
    if os.path.isabs(path):
        new_path = os.path.relpath(path, sublime.packages_path())
    else:
        new_path = path
    # Preferences requires 'Packages' in the path
    new_path = os.path.join('Packages', new_path)
    return new_path


def RunDartanalyzer(view, fileName):
    settings = view.settings()
    dartsdk_path = settings.get('dartsdk_path')

    if dartsdk_path:
        DartLintThread(view, dartsdk_path, fileName).start()


class DartLintThread(threading.Thread):
    def __init__(self, view, dartsdk_path, fileName):
        super(DartLintThread, self).__init__()
        self.daemon = True
        self.view = view
        self.window = view.window()
        self.dartsdk_path = dartsdk_path
        self.fileName = fileName

    def run(self):
        analyzer_path = os.path.join(self.dartsdk_path, 'bin', 'dartanalyzer')
        # Clear all regions
        self.clear_all()
        if IsWindows():
            analyzer_path += '.bat'
        options = '--machine'
        proc = subprocess.Popen([analyzer_path, options, self.fileName],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        while not proc.poll():
            # Wait for it...
            pass
        msg_pattern_machine = re.compile(
            r'^(?P<severity>\w+)\|(?P<type>\w+)\|(?P<code>\w+)\|(?P<file_name>.+)\|(?P<line>\d+)\|(?P<col>\d+)\|(?P<err_length>\d+)\|(?P<message>.+)')

        lines = proc.stderr.read().decode('UTF-8').split('\n')
        # Collect data needed to generate error messages
        lint_data = []
        lines_out = ''
        err_count = 0
        culp_regions = {}
        for line in lines:
            line_out = ''
            line_data = {}
            line_groups = msg_pattern_machine.match(line)

            if line_groups is not None:
                if line_groups.group('file_name') != self.fileName:
                    # output is for a different file
                    continue
                line_out = '%s: %s on line %s, col %s: %s\n' % \
                    (line_groups.group('severity'),
                     line_groups.group('code'),
                     line_groups.group('line'),
                     line_groups.group('col'),
                     line_groups.group('message'))

                line_data['severity'] = line_groups.group('severity')
                line_data['col'] = line_groups.group('col')
                line_data['line'] = line_groups.group('line')
                line_data['msg'] = line_groups.group('message')
                line_data['code'] = line_groups.group('code')
                line_data['type'] = line_groups.group('type')
                line_data['err_length'] = line_groups.group('err_length')
                line_data['lint_out'] = line_out
                line_data['line_pt'] = self.view.text_point(
                    int(line_data['line']) - 1, 0)
                line_data['point'] = self.view.text_point(
                    int(line_data['line']) - 1, int(line_data['col']))
                next_line = self.view.text_point(int(line_data['line']), 0)

                # Add a region (set status, gutter mark and underline)
                if int(line_data['err_length']) > 0 and \
                        int(line_data['point']) + \
                        (int(line_data['err_length']) - 1) < next_line:
                    # Set the error region
                    line_data['culp_region'] = sublime.Region(
                        int(line_data['point']) - 1,
                        int(line_data['point']) +
                        (int(line_data['err_length']) - 1))
                else:
                    # Set the line as the error region
                    line_data['culp_region'] = sublime.Region(
                        line_data['line_pt'],
                        line_data['line_pt'])
                # Add the region to the apropriate region collection
                if ('dartlint-' + line_data['severity']) not in culp_regions:
                    culp_regions['dartlint_%s' % line_data['severity']] = []
                culp_regions['dartlint_%s' % line_data['severity']].append(
                    line_data['culp_region'])
                lines_out += line_out
                lint_data.append(line_data)
                err_count += 1
        for reg_id, reg_list in culp_regions.items():
            # set the scope name
            this_scope = 'dartlint.mark.warning'
            if reg_id.endswith('ERROR') is True:
                this_scope = 'dartlint.mark.error'
            if reg_id.endswith('INFO') is True:
                this_scope = 'dartlint.mark.info'
            self.view.add_regions(
                reg_id,
                reg_list,
                this_scope,
                GUTTER_Icon[reg_id],
                sublime.DRAW_EMPTY |
                sublime.DRAW_NO_OUTLINE |
                sublime.DRAW_NO_FILL |
                sublime.DRAW_SQUIGGLY_UNDERLINE |
                sublime.DRAW_EMPTY_AS_OVERWRITE)
        if lines_out is '':
            self.output = None
            print('No errors.')
            self.view.set_status('dartlint', 'Dartlint: No errors')
        else:
            self.output = lint_data
            # Out to console
            print('\n' + lines_out)
            # Output to a popup
            self.popup_errors(self.window, self.view, self.output)

    def popup_errors(self, window, view, ErrorData):
        # Process data into a list of errors
        # TODO: Sort by Line

        window.focus_view(view)
        dd_list = []
        for entry in ErrorData:
            dd_list.append(entry['lint_out'])
        window.show_quick_panel(
            dd_list,
            on_select=self.goto_error,
            on_highlight=self.goto_error)

    def goto_error(self, index):
        this_error = self.output[index]
        self.view.sel().clear()
        self.view.sel().add(this_error['culp_region'])
        self.view.show_at_center(this_error['point'])

    def clear_all(self):
        for region_name in \
                ('dartlint_INFO', 'dartlint_WARNING', 'dartlint_ERROR'):
            print('Clearing region: %s' % region_name)
            self.view.erase_regions('%s' % region_name)


def IsWindows():
        return sublime.platform() == 'windows'
