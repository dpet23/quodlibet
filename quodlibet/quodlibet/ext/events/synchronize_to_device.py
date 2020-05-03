# -*- coding: utf-8 -*-
# Copyright 2018 Jan Korte
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import os
import re
import unicodedata
from pathlib import Path
from shutil import copyfile

from gi.repository import Gtk, GLib
from senf import fsn2text

from quodlibet import _
from quodlibet import app
from quodlibet import config
from quodlibet import get_user_dir
from quodlibet import qltk
from quodlibet import util
from quodlibet.pattern import FileFromPattern
from quodlibet.plugins import PM
from quodlibet.plugins import PluginConfigMixin
from quodlibet.plugins.events import EventPlugin
from quodlibet.qltk import Icons
from quodlibet.qltk.cbes import ComboBoxEntrySave
from quodlibet.qltk.ccb import ConfigCheckButton
from quodlibet.qltk.models import ObjectStore
from quodlibet.qltk.views import TreeViewColumn
from quodlibet.query import Query

PLUGIN_CONFIG_SECTION = _('synchronize_to_device')


def _expandable_scroll(min_height=100, max_height=300, expand=True):
    """
    Create a ScrolledWindow that expands as content is added.

    :param min_height: The minimum height of the window, in pixels.
    :param max_height: The maximum height of the window, in pixels. It will grow
                       up to this height before it starts scrolling the content.
    :param expand:     Whether the window should expand.
    :return: A new ScrolledWindow.
    """
    return Gtk.ScrolledWindow(min_content_height=min_height,
                              max_content_height=max_height,
                              propagate_natural_height=expand)


class Entry:
    """
    An entry in the tree of previewed export paths.
    """

    def __init__(self, song):
        self.song = song
        self.export_path = None

    @property
    def basename(self):
        return fsn2text(self.song('~basename'))

    @property
    def filename(self):
        return fsn2text(self.song('~filename'))


class Tags:
    """
    Various tags that will be used to show sync issues.
    """
    SKIP = '[SKIP]'


class SyncToDevice(EventPlugin, PluginConfigMixin):
    PLUGIN_ICON = Icons.NETWORK_TRANSMIT
    PLUGIN_ID = PLUGIN_CONFIG_SECTION
    PLUGIN_NAME = PLUGIN_CONFIG_SECTION.replace('_', ' ').title()
    PLUGIN_DESC = _('Synchronizes all songs from the selected saved searches '
                    'with the specified folder.')

    CONFIG_SECTION = PLUGIN_CONFIG_SECTION
    CONFIG_QUERY_PREFIX = 'query_'
    CONFIG_PATH_KEY = '{}_{}'.format(PLUGIN_CONFIG_SECTION, 'path')
    CONFIG_PATTERN_KEY = '{}_{}'.format(PLUGIN_CONFIG_SECTION, 'pattern')

    path_query = os.path.join(get_user_dir(), 'lists', 'queries.saved')
    path_pattern = os.path.join(get_user_dir(), 'lists', 'renamepatterns')

    spacing_main = 20
    spacing_large = 6
    spacing_small = 3

    default_export_pattern = os.path.join('<artist>', '<album>', '<title>')
    unsafe_filename_chars = re.compile(r'[<>:"/\\|?*\u00FF-\uFFFF]')

    skip_duplicate_text = Tags.SKIP + ' Filename exists: {}'
    skip_none_text = Tags.SKIP + ' No export path for {}'

    def PluginPreferences(self, parent):
        # Read saved searches from file
        self.queries = {}
        with open(self.path_query, 'r', encoding='utf-8') as query_file:
            for query_string in query_file:
                name = next(query_file).strip()
                self.queries[name] = Query(query_string.strip())
        if not self.queries:
            # query_file is empty
            return qltk.Frame(
                _('No saved searches yet, create some and come back!'))

        main_vbox = Gtk.VBox(spacing=self.spacing_main)
        self.main_vbox = main_vbox

        # Saved search selection frame
        saved_search_vbox = Gtk.VBox(spacing=self.spacing_large)
        for query_name, query in self.queries.items():
            query_config = self.CONFIG_QUERY_PREFIX + query_name
            check_button = ConfigCheckButton(query_name, PM.CONFIG_SECTION,
                                             self._config_key(query_config))
            check_button.set_active(self.config_get_bool(query_config))
            saved_search_vbox.pack_start(check_button, False, False, 0)
        saved_search_scroll = _expandable_scroll(min_height=0)
        saved_search_scroll.add(saved_search_vbox)
        frame = qltk.Frame(label=_('Synchronize the following saved searches:'),
                           child=saved_search_scroll)
        main_vbox.pack_start(frame, False, False, 0)

        # Destination path entry field
        destination_entry = Gtk.Entry(
            placeholder_text=_('The absolute path to your export location'),
            text=config.get(PM.CONFIG_SECTION, self.CONFIG_PATH_KEY, '')
        )
        destination_entry.connect('changed', self._destination_path_changed)
        self.destination_entry = destination_entry

        # Destination path selection button
        destination_button = qltk.Button(label='', icon_name=Icons.FOLDER_OPEN)
        destination_button.connect('clicked', self._select_destination_path)

        # Destination path hbox
        destination_path_hbox = Gtk.HBox(spacing=self.spacing_small)
        destination_path_hbox.pack_start(destination_entry, True, True, 0)
        destination_path_hbox.pack_start(destination_button, False, False, 0)

        # Destination path information
        destination_warn_label = self._label_with_icon(
            _("All pre-existing songs in the destination folder that aren't in "
              "the saved searches will be deleted."),
            Icons.DIALOG_WARNING)
        destination_info_label = self._label_with_icon(
            _('For devices mounted with MTP, specify a local destination '
              'folder and transfer it to your device with rsync.'),
            Icons.DIALOG_INFORMATION)

        # Destination path frame
        destination_vbox = Gtk.VBox(spacing=self.spacing_large)
        destination_vbox.pack_start(destination_path_hbox, False, False, 0)
        destination_vbox.pack_start(destination_warn_label, False, False, 0)
        destination_vbox.pack_start(destination_info_label, False, False, 0)
        frame = qltk.Frame(label=_('Destination path:'), child=destination_vbox)
        main_vbox.pack_start(frame, False, False, 0)

        # Export pattern frame
        export_pattern_combo = ComboBoxEntrySave(
            self.path_pattern, [self.default_export_pattern],
            title=_('Path Patterns'), edit_title=_(u'Edit saved patterns…'))
        export_pattern_combo.enable_clear_button()
        export_pattern_combo.show_all()
        export_pattern_entry = export_pattern_combo.get_child()
        export_pattern_entry.set_placeholder_text(
            _('The structure of the exported filenames, based on their tags'))
        export_pattern_entry.set_text(config.get(PM.CONFIG_SECTION,
                                                 self.CONFIG_PATTERN_KEY,
                                                 self.default_export_pattern))
        export_pattern_entry.connect('changed', self._export_pattern_changed)
        self.export_pattern_entry = export_pattern_entry
        frame = qltk.Frame(label=_('Export pattern:'),
                           child=export_pattern_combo)
        main_vbox.pack_start(frame, False, False, 0)

        # Start preview button
        preview_start_button = qltk.Button(label=_('Preview'),
                                           icon_name=Icons.VIEW_REFRESH)
        preview_start_button.set_visible(True)
        preview_start_button.connect('clicked', self._start_preview)
        self.preview_start_button = preview_start_button

        # Stop preview button
        preview_stop_button = qltk.Button(label=_('Stop preview'),
                                          icon_name=Icons.PROCESS_STOP)
        preview_stop_button.set_visible(False)
        preview_stop_button.set_no_show_all(True)
        preview_stop_button.connect('clicked', self._stop_preview)
        self.preview_stop_button = preview_stop_button

        # Preview details view
        preview_tree = Gtk.TreeView(model=ObjectStore())
        preview_scroll = _expandable_scroll(min_height=50)
        preview_scroll.add(preview_tree)
        self.preview_model = preview_tree.get_model()

        # Preview column: file
        render = Gtk.CellRendererText()
        column = TreeViewColumn(title=_('File'))
        column.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        column.set_cell_data_func(render, self._set_cell_data_basename)
        column.pack_start(render, True)
        preview_tree.append_column(column)

        # Preview column: export path
        render = Gtk.CellRendererText()
        column = TreeViewColumn(title=_('Export Path'))
        column.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        column.set_cell_data_func(render, self._set_cell_data_export_path)
        render.set_property('editable', True)
        render.connect('edited', self._row_edited)
        column.pack_start(render, True)
        preview_tree.append_column(column)

        # Section for previewing exported files
        preview_vbox = Gtk.VBox(spacing=self.spacing_large)
        preview_vbox.pack_start(preview_start_button, False, False, 0)
        preview_vbox.pack_start(preview_stop_button, False, False, 0)
        preview_vbox.pack_start(preview_scroll, False, False, 0)
        main_vbox.pack_start(preview_vbox, False, False, 0)

        # Start sync button
        sync_start_button = qltk.Button(label=_('Start synchronization'),
                                        icon_name=Icons.DOCUMENT_SAVE)
        sync_start_button.set_sensitive(False)
        sync_start_button.set_visible(True)
        sync_start_button.connect('clicked', self._start_sync)
        self.sync_start_button = sync_start_button

        # Stop sync button
        sync_stop_button = qltk.Button(label=_('Stop synchronization'),
                                       icon_name=Icons.PROCESS_STOP)
        sync_stop_button.set_visible(False)
        sync_stop_button.set_no_show_all(True)
        sync_stop_button.connect('clicked', self._stop_sync)
        self.sync_stop_button = sync_stop_button

        # Section for the sync buttons
        sync_vbox = Gtk.VBox(spacing=self.spacing_large)
        sync_vbox.pack_start(sync_start_button, False, False, 0)
        sync_vbox.pack_start(sync_stop_button, False, False, 0)
        main_vbox.pack_start(sync_vbox, False, False, 0)

        # Define output log window
        self.log_label = Gtk.Label(label=_('Progress:'),
                                   xalign=0.0, yalign=0.5, wrap=True,
                                   visible=False, no_show_all=True)
        self.log = Gtk.TextView(left_margin=5, right_margin=5, editable=False,
                                visible=False, no_show_all=True)
        self.log_buf = self.log.get_buffer()
        log_scroll = _expandable_scroll(min_height=100)
        log_scroll.add(self.log)
        output_vbox = Gtk.VBox(spacing=self.spacing_large)
        output_vbox.pack_start(self.log_label, False, False, 0)
        output_vbox.pack_start(log_scroll, False, False, 0)
        main_vbox.pack_start(output_vbox, False, False, 0)

        return main_vbox

    def _label_with_icon(self, text, icon_name):
        """
        Create a new label with an icon to the left of the text.

        :param text:      The new text to set for the label.
        :param icon_name: An icon name or None.
        :return: A HBox containing an icon followed by a label.
        """
        image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
        label = Gtk.Label(label=text, xalign=0.0, yalign=0.5, wrap=True)

        hbox = Gtk.HBox(spacing=self.spacing_large)
        hbox.pack_start(image, False, False, 0)
        hbox.pack_start(label, True, True, 0)

        return hbox

    def _print_log(self, text):
        """
        Print text to the output TextView window.

        :param text: The text to add to the TextView.
        """
        GLib.idle_add(lambda: self.log_buf.insert(self.log_buf.get_end_iter(),
                                                  text + '\n'))
        GLib.idle_add(lambda: self.log.scroll_to_mark(self.log_buf.get_insert(),
                                                      0.0, True, 0.5, 0.5))

    def _destination_path_changed(self, entry):
        """
        Save the destination path to the global config when the path changes.

        :param entry: The destination path entry field.
        """
        config.set(PM.CONFIG_SECTION, self.CONFIG_PATH_KEY, entry.get_text())

    def _select_destination_path(self, button):
        """
        Show a folder selection dialog to select the destination path
        from the file system.

        :param button: The destination path selection button.
        """
        dialog = Gtk.FileChooserDialog(
            title=_('Choose destination path'),
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            select_multiple=False, create_folders=True,
            local_only=False, show_hidden=True)
        dialog.add_buttons(
            _('_Cancel'), Gtk.ResponseType.CANCEL,
            _('_Save'), Gtk.ResponseType.OK
        )
        dialog.set_default_response(Gtk.ResponseType.OK)

        # If there is an existing path in the entry field,
        # make that path the default
        destination_entry_text = self.destination_entry.get_text()
        if destination_entry_text != '':
            dialog.set_current_folder(destination_entry_text)

        # Show the dialog and get the selected path
        response = dialog.run()
        response_path = dialog.get_filename()

        # Close the dialog and save the selected path
        dialog.destroy()
        if response == Gtk.ResponseType.OK \
                and response_path != destination_entry_text:
            self.destination_entry.set_text(response_path)

    def _export_pattern_changed(self, entry):
        """
        Save the export pattern to the global config when the pattern changes.

        :param entry: The export pattern entry field.
        """
        config.set(PM.CONFIG_SECTION, self.CONFIG_PATTERN_KEY, entry.get_text())

    @staticmethod
    def _set_cell_data_basename(column, cell, model, iter_, data):
        """
        Handle entering data into the "File" column of the export path previews.
        This function acts as a Gtk.TreeCellDataFunc callback.

        :param column: The selected column of the preview tree.
        :param cell:   The cell that is being rendered by the column.
        :param model:  The model containing the tree's data.
        :param iter_:  The Gtk.TreeIter struct.
        :param data:   The user data.
        """
        entry = model.get_value(iter_)
        cell.set_property('text', entry.basename)

    @staticmethod
    def _set_cell_data_export_path(column, cell, model, iter_, data):
        """
        Handle entering data into the "Export Path" column of the export path
        previews.
        This function acts as a Gtk.TreeCellDataFunc callback.

        :param column: The selected column of the preview tree.
        :param cell:   The cell that is being rendered by the column.
        :param model:  The model containing the tree's data.
        :param iter_:  The Gtk.TreeIter struct.
        :param data:   The user data.
        """
        entry = model.get_value(iter_)
        cell.set_property('text', entry.export_path)

    def _row_edited(self, renderer, path, new_text):
        """
        Handle a manual edit of a previewed export path.

        :param renderer: The object which received the signal.
        :param path:     The path identifying the edited cell.
        :param new_text: The new text.
        """
        path = Gtk.TreePath.new_from_string(path)
        entry = self.preview_model[path][0]
        if entry.export_path != new_text:
            if new_text in self._get_previewed_paths():
                new_text = self.skip_duplicate_text.format(new_text)
            entry.export_path = new_text
            self.preview_model.path_changed(path)

    def _show_sync_error(self, title, message):
        qltk.ErrorMessage(self.main_vbox, title, message).run()
        self._print_log(_('Synchronization failed: {}').format(title))

    @staticmethod
    def _run_pending_events():
        """
        Prevent the application from becoming unresponsive.
        """
        while Gtk.events_pending():
            Gtk.main_iteration()

    def _start_preview(self, button):
        """
        Start the generation of export paths for all songs.

        :param button: The start preview button.
        """
        self._print_log('Starting synchronization preview.')
        self.running = True

        # Change button visibility
        self.preview_start_button.set_visible(False)
        self.preview_stop_button.set_visible(True)

        success = self._run_preview()
        self._stop_preview(None)

        if not success:
            return

        self.sync_start_button.set_sensitive(True)
        self._print_log('Finished synchronization preview.')

    def _stop_preview(self, button):
        """
        Stop the generation of export paths for all songs.

        :param button: The stop preview button.
        """
        if button is not None:
            self._print_log('Stopping synchronization preview.')
        self.running = False

        # Change button visibility
        self.preview_start_button.set_visible(True)
        self.preview_stop_button.set_visible(False)

    def _run_preview(self):
        """
        Show the export paths for all songs to be synchronized.

        :return: Whether the generation of preview paths was successful.
        """
        destination_path, pattern = self._get_valid_inputs()
        if None in {destination_path, pattern}:
            return False

        # Get a list containing all songs to export
        songs = self._get_songs_from_queries()
        export_paths = []

        self.preview_model.clear()

        for song in songs:
            if not self.running:
                self._print_log('Stopped synchronization preview.')
                return False
            self._run_pending_events()

            export_path = self._get_export_path(song, destination_path, pattern)
            if not export_path:
                return False
            if export_path in export_paths:
                export_path = self.skip_duplicate_text.format(export_path)

            entry = Entry(song)
            entry.export_path = export_path
            export_paths.append(export_path)
            self.preview_model.append(row=[entry])

        return True

    def _get_previewed_paths(self):
        """
        Build a list of all current export paths for the songs to be
        synchronized.
        """
        return [entry.export_path for entry in self.preview_model.values()]

    def _get_valid_inputs(self):
        """
        Ensure that all user inputs have been given. Shows a popup error message
        if values are not as expected.

        :return: The entered destination path and an fsnative pattern,
                 or None if an error occurred.
        """
        # Get text from the destination path entry
        destination_path = self.destination_entry.get_text()
        if not destination_path:
            self._show_sync_error(_('No destination path provided'),
                                  _('Please specify the directory where songs '
                                    'should be exported.'))
            return None, None

        # Get text from the export pattern entry
        export_pattern = self.export_pattern_entry.get_text()
        if not export_pattern:
            self._show_sync_error(_('No export pattern provided'),
                                  _('Please specify an export pattern for the '
                                    'names of the exported songs.'))
            return None, None

        # Combine destination path and export pattern to form the full pattern
        full_export_path = os.path.join(destination_path, export_pattern)
        try:
            pattern = FileFromPattern(full_export_path)
        except ValueError:
            self._show_sync_error(
                _('Export path is not absolute'),
                _('The pattern\n\n<b>{}</b>\n\ncontains "/" but does not start '
                  'from root. Please provide an absolute destination path by '
                  'making sure it starts with / or ~/.')
                .format(util.escape(full_export_path)))
            return None, None

        return destination_path, pattern

    def _get_songs_from_queries(self):
        """
        Build a list of songs to be synchronized, filtered using the
        selected saved searches.

        :return: A list of the selected songs.
        """
        enabled_queries = []
        for query_name, query in self.queries.items():
            query_config = self.CONFIG_QUERY_PREFIX + query_name
            if self.config_get_bool(query_config):
                enabled_queries.append(query)

        selected_songs = []
        for song in app.library.itervalues():
            if any(query.search(song) for query in enabled_queries):
                selected_songs.append(song)

        return selected_songs

    def _get_export_path(self, song, destination_path, export_pattern):
        """
        Use the given pattern of song tags to build the destination path
        for a song.

        :param song:             The song for which to build the export path.
        :param destination_path: The user-entered destination path.
        :param export_pattern:   An fsnative file path pattern.
        :return: A safe full destination path for the song.
        """
        new_name = Path(export_pattern.format(song))
        expanded_destination = os.path.expanduser(destination_path)

        try:
            relative_name = new_name.relative_to(expanded_destination)
        except ValueError as ex:
            self._show_sync_error(
                _('Mismatch between destination path and export '
                  'pattern'),
                _('The export pattern starts with a path that '
                  'differs from the destination path. Please '
                  'correct the pattern.\n\nError:\n{}').format(ex))
            return None

        return os.path.join(destination_path,
                            self._make_safe_name(relative_name))

    def _make_safe_name(self, input_path):
        """
        Make a file path safe by replacing unsafe characters.

        :param input_path: A relative Path.
        :return: The given path, with any unsafe characters replaced.
                 Returned as a string.
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

    def _start_sync(self, button):
        """
        Start the song synchronization.

        :param button: The start sync button.
        """
        self._print_log('Starting song synchronization.')
        self.running = True

        # Change button visibility
        self.sync_start_button.set_visible(False)
        self.sync_stop_button.set_visible(True)

        # Change log visibility
        self.log_label.set_visible(True)
        self.log.set_visible(True)

        if not self._run_sync():
            return

        self._stop_sync(None)
        self._print_log('Finished song synchronization.')

    def _stop_sync(self, button):
        """
        Stop the song synchronization.

        :param button: The stop sync button.
        """
        if button is not None:
            self._print_log('Stopping song synchronization.')
        self.running = False

        # Change button visibility
        self.sync_start_button.set_visible(True)
        self.sync_stop_button.set_visible(False)

    def _run_sync(self):
        """
        Synchronize the songs from the selected saved searches
        with the specified folder.

        :return: Whether the synchronization was successful.
        """
        for entry in self.preview_model.values():
            if not self.running:
                self._print_log('Stopped song synchronization.')
                return False
            self._run_pending_events()

            if entry.export_path is None:
                self._print_log(self.skip_none_text.format(entry.basename))
                continue  # to next entry
            elif Tags.SKIP in entry.export_path:
                self._print_log(entry.export_path)
                continue  # to next entry

            # Export, skipping existing files
            expanded_path = os.path.expanduser(entry.export_path)
            if os.path.exists(expanded_path):
                self._print_log(_('Skipped "{}" because it already exists.')
                                .format(expanded_path))
            else:
                self._print_log(_('Writing "{}".').format(entry.export_path))
                song_folders = os.path.dirname(expanded_path)
                os.makedirs(song_folders, exist_ok=True)
                copyfile(entry.filename, expanded_path)

        return True
