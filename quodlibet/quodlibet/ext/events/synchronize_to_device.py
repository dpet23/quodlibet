# -*- coding: utf-8 -*-
# Copyright 2018 Jan Korte
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import os
import re
import string
import unicodedata
from pathlib import Path

from gi.repository import Gtk, GLib
from senf import fsn2text
from quodlibet import _, app
from quodlibet import config
from quodlibet import get_user_dir
from quodlibet import qltk
from quodlibet import util
from quodlibet.pattern import FileFromPattern
from quodlibet.plugins import PluginConfigMixin
from quodlibet.plugins import PM
from quodlibet.plugins.events import EventPlugin
from quodlibet.qltk import Icons
from quodlibet.qltk.cbes import ComboBoxEntrySave
from quodlibet.qltk.ccb import ConfigCheckButton
from quodlibet.qltk.models import ObjectStore
from quodlibet.qltk.views import TreeViewColumn
from quodlibet.query import Query

from shutil import copyfile

PLUGIN_CONFIG_SECTION = 'synchronize_to_device'


def expandible_scroll(min_height=0, max_height=300):
    """ Create a ScrolledWindow that expands as content is added """
    return Gtk.ScrolledWindow(min_content_height=min_height,
                              max_content_height=max_height,
                              propagate_natural_height=True)


class Entry():
    """ This class defines an entry in the tree of previewed export paths """

    def __init__(self, song):
        self.song = song
        self.export_path = None

    @property
    def basename(self):
        return fsn2text(self.song("~basename"))

    @property
    def filename(self):
        return fsn2text(self.song("~filename"))


class SyncToDevice(EventPlugin, PluginConfigMixin):
    PLUGIN_ICON = Icons.NETWORK_TRANSMIT
    PLUGIN_ID = PLUGIN_CONFIG_SECTION
    PLUGIN_NAME = _("Synchronize to Device")
    PLUGIN_DESC = _(
        "Synchronizes all songs from the selected saved searches with the "
        "specified folder."
    )

    CONFIG_SECTION = PLUGIN_CONFIG_SECTION
    CONFIG_QUERY_PREFIX = 'query_'
    CONFIG_PATH_KEY = '{}_{}'.format(PLUGIN_CONFIG_SECTION, 'path')
    CONFIG_PATTERN_KEY = '{}_{}'.format(PLUGIN_CONFIG_SECTION, 'pattern')

    path_query = os.path.join(get_user_dir(), 'lists', 'queries.saved')
    path_pattern = os.path.join(get_user_dir(), 'lists', 'renamepatterns')

    spacing_main = 20
    spacing_large = 6
    spacing_small = 3

    default_export_pattern = os.path.join("<artist>", "<album>", "<title>")
    unsafe_filename_chars = re.compile(r'[<>:"/\\|?*\u00FF-\uFFFF]')

    def PluginPreferences(self, parent):
        vbox = Gtk.VBox(spacing=self.spacing_main)

        # Define output window
        self.log_label = Gtk.Label(label=_("Progress:"),
                                   xalign=0.0, yalign=0.5, wrap=True,
                                   visible=False, no_show_all=True)
        self.log = Gtk.TextView(left_margin=5, right_margin=5, editable=False,
                                visible=False, no_show_all=True)
        log_buf = self.log.get_buffer()
        log_scroll = expandible_scroll(min_height=100)
        log_scroll.add(self.log)

        def append(text):
            """ Print text to the output TextView window. """
            GLib.idle_add(lambda: log_buf.insert(log_buf.get_end_iter(),
                                                    text + "\n"))
            GLib.idle_add(lambda: self.log.scroll_to_mark(log_buf.get_insert(),
                                                          0.0, True, 0.5, 0.5))

        # Read saved searches from file
        queries = {}
        with open(self.path_query, 'r', encoding='utf-8') as query_file:
            for query_string in query_file:
                name = next(query_file).strip()
                queries[name] = Query(query_string.strip())

        if not queries:
            # query_file is empty
            return qltk.Frame(
                _("No saved searches yet, create some and come back!"))

        # Saved search selection frame
        saved_search_vbox = Gtk.VBox(spacing=self.spacing_large)
        for query_name, query in queries.items():
            query_config = self.CONFIG_QUERY_PREFIX + query_name
            check_button = ConfigCheckButton(query_name, PM.CONFIG_SECTION,
                                             self._config_key(query_config))
            check_button.set_active(self.config_get_bool(query_config))
            saved_search_vbox.pack_start(check_button, False, False, 0)
        saved_search_scroll = expandible_scroll()
        saved_search_scroll.add(saved_search_vbox)
        frame = qltk.Frame(
            label=_("Synchronize the following saved searches:"),
            child=saved_search_scroll)
        vbox.pack_start(frame, False, False, 0)

        def show_sync_error(title, message):
            """ Show a popup error message during a synchronization error """
            qltk.ErrorMessage(vbox, title, message).run()

        def get_songs_from_queries():
            """
            Build a list of songs to be synchronized, filtered using the
            selected saved searches
            """
            enabled_queries = []
            for query_name, query in queries.items():
                query_config = self.CONFIG_QUERY_PREFIX + query_name
                if self.config_get_bool(query_config):
                    enabled_queries.append(query)

            selected_songs = []
            for song in app.library.itervalues():
                if any(query.search(song) for query in enabled_queries):
                    selected_songs.append(song)

            return selected_songs

        def get_export_path(song, destination_path, export_pattern):
            """
            Use the given pattern of song tags to build the destination path
            for a song
            """
            new_name = Path(export_pattern.format(song))
            expanded_destination = os.path.expanduser(destination_path)

            try:
                relative_name = new_name.relative_to(expanded_destination)
            except ValueError as ex:
                show_sync_error(
                    _('Mismatch between destination path and export '
                      'pattern'),
                    _('The export pattern starts with a path that '
                      'differs from the destination path. Please '
                      'correct the pattern.\n\nError:\n{}').format(ex))
                return None

            return os.path.join(destination_path,
                                make_safe_name(relative_name))

        def make_safe_name(input_path):
            """
            Make a file path safe by replacing unsafe characters.
            """
            # Remove diacritics (accents)
            safe_filename = unicodedata.normalize('NFKD', str(input_path))
            safe_filename = u''.join(
                [c for c in safe_filename if not unicodedata.combining(c)])

            # Replace unsafe chars in each path component
            safe_parts = []
            for i, component in enumerate(Path(safe_filename).parts):
                if i > 0:
                    component = self.unsafe_filename_chars.sub('_', component)
                    component = re.sub('_{2,}', '_', component)
                safe_parts.append(component)
            safe_filename = os.path.join(*safe_parts)

            return safe_filename

        def remove_empty_dirs(path):
            """ Delete all empty sub-directories from the given path """
            for root, dirnames, filenames in os.walk(path, topdown=False):
                for dirname in dirnames:
                    try:
                        os.rmdir(os.path.realpath(os.path.join(root, dirname)))
                    except OSError:
                        pass

        def path_changed(entry):
            """ Save the destination path to the global config """
            config.set(PM.CONFIG_SECTION, self.CONFIG_PATH_KEY,
                       entry.get_text())

        # Destination path entry field
        destination_entry = Gtk.Entry(
            placeholder_text=_("e.g. the absolute path to your SD card"),
            text=config.get(PM.CONFIG_SECTION, self.CONFIG_PATH_KEY, '')
        )
        destination_entry.connect('changed', path_changed)
        self.destination_entry = destination_entry

        def select_destination_path(button):
            """
            Show a folder selection dialog to select the destination path
            from the file system
            """
            dialog = Gtk.FileChooserDialog(
                title=_("Choose destination path"),
                action=Gtk.FileChooserAction.SELECT_FOLDER,
                select_multiple=False, create_folders=True,
                local_only=False, show_hidden=True)
            dialog.add_buttons(
                _("_Cancel"), Gtk.ResponseType.CANCEL,
                _("_Save"), Gtk.ResponseType.OK
            )
            dialog.set_default_response(Gtk.ResponseType.OK)

            # If there is an existing path in the entry field,
            # make that path the default
            destination_entry_text = self.destination_entry.get_text()
            if destination_entry_text != "":
                dialog.set_current_folder(destination_entry_text)

            # Show the dialog and get the selected path
            response = dialog.run()
            response_path = ""
            if response == Gtk.ResponseType.OK:
                response_path = dialog.get_filename()

            # Close the dialog and save the selected path
            dialog.destroy()
            self.destination_entry.set_text(response_path)

        # Destination path selection button
        destination_button = qltk.Button(label="", icon_name=Icons.FOLDER_OPEN)
        destination_button.connect('clicked', select_destination_path)

        # Destination path hbox
        destination_path_box = Gtk.HBox(spacing=self.spacing_small)
        destination_path_box.pack_start(destination_entry, True, True, 0)
        destination_path_box.pack_start(destination_button, False, False, 0)

        def make_label_with_icon(label, icon_name):
            """ Create a new label with an icon to the left of the text """
            hbox = Gtk.HBox(spacing=self.spacing_large)
            image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
            hbox.pack_start(image, False, False, 0)
            label = Gtk.Label(label=label, xalign=0.0, yalign=0.5, wrap=True)
            hbox.pack_start(label, True, True, 0)
            return hbox

        # Destination path information
        destination_warn_label = make_label_with_icon(
            _("All pre-existing songs in the destination folder that aren't in "
              "the saved searches will be deleted."),
            Icons.DIALOG_WARNING)
        destination_info_label = make_label_with_icon(
            _("For devices mounted with MTP, specify a local destination "
              "folder and transfer it to your device with rsync."),
            Icons.DIALOG_INFORMATION)

        # Destination path frame
        destination_vbox = Gtk.VBox(spacing=self.spacing_large)
        destination_vbox.pack_start(destination_path_box, False, False, 0)
        destination_vbox.pack_start(destination_warn_label, False, False, 0)
        destination_vbox.pack_start(destination_info_label, False, False, 0)
        frame = qltk.Frame(label=_("Destination path:"), child=destination_vbox)
        vbox.pack_start(frame, False, False, 0)

        def export_pattern_changed(entry):
            """ Save the export pattern to the global config """
            config.set(PM.CONFIG_SECTION, self.CONFIG_PATTERN_KEY,
                       entry.get_text())

        # Export pattern frame
        export_pattern_combo = ComboBoxEntrySave(
            self.path_pattern, [self.default_export_pattern],
            title=_("Path Patterns"),
            edit_title=_(u"Edit saved patterns…"))
        export_pattern_combo.enable_clear_button()
        export_pattern_combo.show_all()
        export_pattern_entry = export_pattern_combo.get_child()
        export_pattern_entry.set_placeholder_text(
            _('the structure of the exported filenames, based on their tags'))
        export_pattern_entry.set_text(config.get(PM.CONFIG_SECTION,
            self.CONFIG_PATTERN_KEY, self.default_export_pattern))
        export_pattern_entry.connect('changed', export_pattern_changed)
        self.export_pattern_entry = export_pattern_entry
        frame = qltk.Frame(label=_("Export pattern:"),
                           child=export_pattern_combo)
        vbox.pack_start(frame, False, False, 0)

        skip_tag = '[SKIP]'
        skip_display = skip_tag + ' Filename already exists ({})'

        def run_preview(button):
            """ Show the export paths for all songs to be synchronized """
            self.preview_button.set_visible(False)
            self.preview_stop_button.set_visible(True)
            self.running = True

            # Get text from the destination path entry
            destination_path = self.destination_entry.get_text()
            if not destination_path:
                show_sync_error(_('No destination path provided'),
                    _('Please specify the directory where songs should '
                        'be exported.'))
                return

            # Get text from the export pattern entry
            export_pattern = self.export_pattern_entry.get_text()
            if not export_pattern:
                show_sync_error(_('No export pattern provided'),
                    _('Please specify an export pattern for the names '
                        'of the exported songs.'))
                return

            # Combine destination path and export pattern to form
            # the full pattern
            full_export_path = os.path.join(destination_path,
                                            export_pattern)
            try:
                pattern = FileFromPattern(full_export_path)
            except ValueError:
                show_sync_error(_('Export path is not absolute'),
                    _('The pattern\n\n<b>%s</b>\n\ncontains / but does '
                        'not start from root. Please provide an absolute '
                        'destination path by making sure it starts with '
                        '/ or ~/.') % (util.escape(full_export_path)))
                return

            # Get a list containing all songs to export
            songs = get_songs_from_queries()
            export_paths = []

            model = self.preview_tree.get_model()
            model.clear()
            for song in songs:
                if not self.running:
                    # append(_("Stopped the synchronization."))
                    return

                # Prevent the application from becoming unreponsive
                while Gtk.events_pending():
                    Gtk.main_iteration()

                export_path = get_export_path(song, destination_path, pattern)
                if not export_path:
                    break
                if export_path in export_paths:
                    export_path = skip_display.format(export_path)

                entry = Entry(song)
                entry.export_path = export_path
                export_paths.append(export_path)
                model.append(row=[entry])

            stop_preview(self.preview_stop_button)
            start_button.set_sensitive(True)

        def stop_preview(button):
            """ Stop the generation export paths """
            self.running = False
            self.preview_button.set_visible(True)
            self.preview_stop_button.set_visible(False)

        # Start preview button
        preview_button = qltk.Button(label=_('Preview'),
                                     icon_name=Icons.VIEW_REFRESH)
        preview_button.set_visible(True)
        preview_button.connect('clicked', run_preview)
        self.preview_button = preview_button

        # Stop preview button
        preview_stop_button = qltk.Button(label=_("Stop preview"),
                                          icon_name=Icons.PROCESS_STOP)
        preview_stop_button.connect('clicked', stop_preview)
        preview_stop_button.set_visible(False)
        preview_stop_button.set_no_show_all(True)
        self.preview_stop_button = preview_stop_button

        # Preview details
        model = ObjectStore()
        preview_tree = Gtk.TreeView(model=model)
        self.preview_tree = preview_tree
        preview_scroll = expandible_scroll(min_height=50)
        preview_scroll.add(preview_tree)

        def cell_data_basename(column, cell, model, iter_, data):
            """
            Handle entering data into the "File" column
            of the export path previews
            """
            entry = model.get_value(iter_)
            cell.set_property('text', entry.basename)

        def cell_data_export_path(column, cell, model, iter_, data):
            """
            Handle entering data into the "Export Path" column
            of the export path previews
            """
            entry = model.get_value(iter_)
            cell.set_property('text', entry.export_path)

        def previewed_paths():
            """
            Build a list of all current export paths for the songs to be
            synchronized
            """
            model = self.preview_tree.get_model()
            return [entry.export_path for entry in model.values()]

        def row_edited(renderer, path, new):
            """ Handle a manual edit of a previewed export path """
            path = Gtk.TreePath.new_from_string(path)
            model = self.preview_tree.get_model()
            entry = model[path][0]
            if entry.export_path != new:
                if new in previewed_paths():
                    new = skip_display.format(new)
                entry.export_path = new
                model.path_changed(path)

        render = Gtk.CellRendererText()
        column = TreeViewColumn(title=_('File'))
        column.pack_start(render, True)
        column.set_cell_data_func(render, cell_data_basename)
        column.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        preview_tree.append_column(column)

        render = Gtk.CellRendererText()
        render.set_property('editable', True)
        render.connect('edited', row_edited)
        column = TreeViewColumn(title=_('Export Path'))
        column.pack_start(render, True)
        column.set_cell_data_func(render, cell_data_export_path)
        column.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        preview_tree.append_column(column)

        # Section for previewing exported files
        preview_vbox = Gtk.VBox(spacing=self.spacing_large)
        preview_vbox.pack_start(preview_button, False, False, 0)
        preview_vbox.pack_start(preview_stop_button, False, False, 0)
        preview_vbox.pack_start(preview_scroll, False, False, 0)
        vbox.pack_start(preview_vbox, False, False, 0)

        def synchronize():
            """
            Synchronize the songs from the selected saved searches
            with the specified folder
            """
            model = self.preview_tree.get_model()
            for entry in model.values():
                if not self.running:
                    append(_("Stopped the synchronization."))
                    return

                # Prevent the application from becoming unreponsive
                while Gtk.events_pending():
                    Gtk.main_iteration()

                if entry.export_path is None or skip_tag in entry.export_path:
                    append(entry.export_path)
                    continue  # to next entry

                # Export, skipping existing files
                expanded_path = os.path.expanduser(entry.export_path)
                if os.path.exists(expanded_path):
                    append(_("Skipped '{}' because it already exists." \
                             .format(expanded_path)))
                else:
                    append(_("Writing '{}'…".format(entry.export_path)))
                    song_folders = os.path.dirname(expanded_path)
                    os.makedirs(song_folders, exist_ok=True)
                    copyfile(entry.filename, expanded_path)

        def start(button):
            """ Start the song synchronization """
            self.running = True
            self.start_button.set_visible(False)
            self.stop_button.set_visible(True)
            self.log_label.set_visible(True)
            self.log.set_visible(True)
            synchronize()
            self.start_button.set_visible(True)
            self.stop_button.set_visible(False)

        def stop(button):
            """ Stop the song synchronization """
            append("Stopping…")
            self.running = False
            self.start_button.set_visible(True)
            self.stop_button.set_visible(False)

        # Start sync button
        start_button = qltk.Button(label=_("Start synchronization"),
                                   icon_name=Icons.DOCUMENT_SAVE)
        start_button.connect('clicked', start)
        start_button.set_sensitive(False)
        start_button.set_visible(True)
        self.start_button = start_button

        # Stop sync button
        stop_button = qltk.Button(label=_("Stop synchronization"),
                                  icon_name=Icons.PROCESS_STOP)
        stop_button.connect('clicked', stop)
        stop_button.set_visible(False)
        stop_button.set_no_show_all(True)
        self.stop_button = stop_button

        # Section for the action buttons
        run_vbox = Gtk.VBox(spacing=self.spacing_large)
        run_vbox.pack_start(start_button, False, False, 0)
        run_vbox.pack_start(stop_button, False, False, 0)
        vbox.pack_start(run_vbox, False, False, 0)

        # Section for the output window
        output_vbox = Gtk.VBox(spacing=self.spacing_large)
        output_vbox.pack_start(self.log_label, False, False, 0)
        output_vbox.pack_start(log_scroll, False, False, 0)
        vbox.pack_start(output_vbox, False, False, 0)

        return vbox
