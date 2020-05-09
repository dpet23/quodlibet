# -*- coding: utf-8 -*-
# Copyright 2018 Jan Korte
#           2020 Daniel Petrescu
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import os
import re
import unicodedata
from math import floor, log10
from pathlib import Path
from shutil import copyfile

from gi.repository import Gtk, GLib, Pango
from senf import fsn2text

from quodlibet import _
from quodlibet import app
from quodlibet import config
from quodlibet import get_user_dir
from quodlibet import ngettext as ngt
from quodlibet import qltk
from quodlibet import util
from quodlibet.pattern import FileFromPattern
from quodlibet.plugins import PM
from quodlibet.plugins import PluginConfigMixin
from quodlibet.plugins.events import EventPlugin
from quodlibet.qltk import Icons
from quodlibet.qltk import Window
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

    class Tags:
        """
        Various tags that will be used in the output.
        """
        tag_start = '<'
        tag_end = '>'

        BLANK = ''
        DUPLICATE = tag_start + 'DUPLICATE' + tag_end
        DELETE = tag_start + 'DELETE' + tag_end

        STATUS_EXISTS = 'Skip existing file'
        STATUS_COPY = 'Writing'

    def __init__(self, song, export_path=None):
        self._song = song
        self._export_path = ''
        self._export_display = ''
        self._tag = self.Tags.BLANK
        self._basename = None
        self._filename = None

        if export_path:
            self._set_export_path(export_path)

    @property
    def basename(self):
        if self._song is not None:
            return fsn2text(self._song('~basename'))
        else:
            return self._basename

    @basename.setter
    def basename(self, name):
        if self._song is None:
            self._basename = name
        else:
            raise ValueError('Cannot set the basename of a song.')

    @property
    def filename(self):
        if self._song is not None:
            return fsn2text(self._song('~filename'))
        else:
            return self._filename

    @filename.setter
    def filename(self, name):
        if self._song is None:
            self._filename = name
        else:
            raise ValueError('Cannot set the filename of a song.')

    def set_duplicate_tag(self):
        self._tag = self.Tags.DUPLICATE
        self._set_export_display()

    def set_delete_tag(self):
        self._tag = self.Tags.DELETE
        self._set_export_display()

    def remove_tag(self):
        self._tag = self.Tags.BLANK
        self._set_export_display()

    def _get_tag(self):
        return self._tag

    def _set_export_path(self, path):
        self._export_path = path
        self._set_export_display()

    def _get_export_path(self):
        return self._export_path

    def _set_export_display(self):
        if self._export_path == '' or self._tag == self.Tags.BLANK:
            space = ''
        else:
            space = ' '
        self._export_display = self._tag + space + self._export_path

    def _get_export_display(self):
        return self._export_display

    export_path = property(_get_export_path, _set_export_path)
    export_display = property(_get_export_display)
    tag = property(_get_tag)


class SyncLog:
    """
    Store and show the current synchronization log in a new window.
    """
    _WIDTH = 600
    _HEIGHT = 300

    def __init__(self, title):
        self._title = title
        self._log = []

        self._create_window()

    def _create_window(self):
        """
        Create a new window to show the current log messages.
        """
        # Create the window
        self._window = Window()
        self._window.hide()
        self._window.connect("delete-event", self._on_delete_event)
        self._window.set_title(self._title)
        self._window.set_default_size(self._WIDTH, self._HEIGHT)

        # Create log text view
        view = Gtk.TextView(left_margin=5, right_margin=5, editable=False)
        view.modify_font(Pango.font_description_from_string('Monospace'))
        self.view = view
        self.buffer = view.get_buffer()
        log_scroll = Gtk.ScrolledWindow()
        log_scroll.add(view)
        self._window.add(log_scroll)

        # Show any pre-existing log entries
        for entry in self._log:
            self._show_log(entry)

    def show_window(self, button=None):
        """
        Show the window.

        :param button: The widget that called this function.
        """
        self._window.show_all()

    def _on_delete_event(self, widget, event):
        """
        When closing the window, hide it instead of destroying it.

        :param widget: The object which received the signal to close the window.
        :param event:  The event which triggered this signal.
        :return: True to stop other handlers from closing the window;
                 False to propagate the event and allow the window to be closed.
        """
        self._window.hide()
        return True

    def _show_log(self, text):
        """
        Add a log entry to the text view.

        :param text: The text to add.
        """
        GLib.idle_add(lambda: self.buffer.insert(self.buffer.get_end_iter(),
                                                  text + '\n'))
        GLib.idle_add(lambda: self.view.scroll_to_mark(self.buffer.get_insert(),
                                                       0.0, True, 0.5, 0.5))

    def print_log(self, text):
        """
        Print text to the output TextView window.

        :param text: The text to add.
        """
        self._log.append(text)
        self._show_log(text)


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
        preview_scroll = _expandable_scroll(min_height=50, max_height=500)
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

        # Preview summary labels
        self.prvw_summary_label = Gtk.Label(xalign=0.0, yalign=0.5, wrap=True,
                                            visible=False, no_show_all=True)
        self.prvw_info_label = Gtk.Label(xalign=0.0, yalign=0.5, wrap=True,
                                         visible=False, no_show_all=True,
                                         label=_('The export paths above can '
                                                 'be edited before starting '
                                                 'the synchronization.'))

        # Section for previewing exported files
        preview_vbox = Gtk.VBox(spacing=self.spacing_large)
        preview_vbox.pack_start(preview_start_button, False, False, 0)
        preview_vbox.pack_start(preview_stop_button, False, False, 0)
        preview_vbox.pack_start(preview_scroll, False, False, 0)
        preview_vbox.pack_start(self.prvw_summary_label, False, False, 0)
        preview_vbox.pack_start(self.prvw_info_label, False, False, 0)
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

        # Open log button
        self.log = SyncLog(self.PLUGIN_NAME + ': Log')
        open_log_button = qltk.Button(label=_('Open log window'),
                                      icon_name=Icons.EDIT)
        open_log_button.connect('clicked', self.log.show_window)
        main_vbox.pack_end(open_log_button, False, False, 0)

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
        cell.set_property('text', entry.export_display)

    def _row_edited(self, renderer, path, new_text):
        """
        Handle a manual edit of a previewed export path.

        :param renderer: The object which received the signal.
        :param path:     The path identifying the edited cell.
        :param new_text: The new text entered by the user.
        """

        def _make_duplicate(entry, old_unique):
            """ Mark the given entry as a duplicate. """
            entry.set_duplicate_tag()
            self.c_song_dupes += 1
            if old_unique:
                self.c_songs_copy -= 1

        def _make_unique(entry, old_duplicate):
            """ Mark the given entry as a unique file. """
            entry.remove_tag()
            self.c_songs_copy += 1
            if old_duplicate:
                self.c_song_dupes -= 1

        def _make_skip(entry, counter):
            """ Skip the given entry during synchronization. """
            entry.remove_tag()
            entry.export_path = ''
            return counter - 1

        def _update_others():
            """ Update all previewed paths based on the current change. """
            counter = 0
            for model_entry in self.preview_model.values():
                if model_entry is entry \
                        or model_entry.export_display == Entry.Tags.DELETE \
                        or model_entry.export_display == '':
                    continue
                elif model_entry.export_path == new_path \
                        and model_entry.tag == Entry.Tags.BLANK:
                    _make_duplicate(model_entry, True)
                    counter += 1
                elif model_entry.tag == Entry.Tags.DUPLICATE \
                        and model_entry.export_path != new_path \
                        and self._get_paths()[model_entry.export_path] == 1:
                    _make_unique(model_entry, True)
                    counter += 1
            return counter

        path = Gtk.TreePath.new_from_string(path)
        entry = self.preview_model[path][0]
        if entry.export_display != new_text:

            old_path_is_duplicate = entry.tag == Entry.Tags.DUPLICATE
            old_path_is_delete = entry.tag == Entry.Tags.DELETE
            old_path_is_blank = entry.export_display == ''
            old_path_is_unique = not (old_path_is_duplicate
                                      or old_path_is_delete
                                      or old_path_is_blank)

            previewed_paths = self._get_paths().keys()
            new_path = new_text.split(Entry.Tags.tag_end)[-1].strip()

            new_path_is_duplicate = new_path in previewed_paths
            new_path_is_delete = new_text == Entry.Tags.DELETE
            new_path_is_blank = new_text == ''
            new_path_is_unique = not (new_path_is_duplicate
                                      or new_path_is_delete
                                      or new_path_is_blank)

            other_songs_updated = 0

            # If the old path was blank...
            if old_path_is_blank and new_path_is_blank:
                pass
            elif old_path_is_blank and new_path_is_delete:
                try:
                    Path(entry.filename).relative_to(self.expanded_destination)
                    entry.set_delete_tag()
                    self.c_songs_delete += 1
                except ValueError:
                    pass
            elif old_path_is_blank and new_path_is_duplicate:
                _make_duplicate(entry, False)
                entry.export_path = new_path
            elif old_path_is_blank and new_path_is_unique:
                _make_unique(entry, False)
                entry.export_path = new_path

            # If the old path was a deletion...
            elif old_path_is_delete and new_path_is_blank:
                self.c_songs_delete = _make_skip(entry, self.c_songs_delete)
            elif old_path_is_delete and new_path_is_delete:
                pass
            elif old_path_is_delete and new_path_is_duplicate:
                pass
            elif old_path_is_delete and new_path_is_unique:
                pass

            # If the old path was a duplicate...
            elif old_path_is_duplicate and new_path_is_blank:
                self.c_song_dupes = _make_skip(entry, self.c_song_dupes)
                other_songs_updated = _update_others()
            elif old_path_is_duplicate and new_path_is_delete:
                self.c_song_dupes = _make_skip(entry, self.c_song_dupes)
                other_songs_updated = _update_others()
            elif old_path_is_duplicate and new_path_is_duplicate:
                entry.export_path = new_path
            elif old_path_is_duplicate and new_path_is_unique:
                _make_unique(entry, True)
                entry.export_path = new_path

            # If the old path was unique...
            elif old_path_is_unique and new_path_is_blank:
                self.c_songs_copy = _make_skip(entry, self.c_songs_copy)
                other_songs_updated = _update_others()
            elif old_path_is_unique and new_path_is_delete:
                self.c_songs_copy = _make_skip(entry, self.c_songs_copy)
                other_songs_updated = _update_others()
            elif old_path_is_unique and new_path_is_duplicate:
                _make_duplicate(entry, True)
                entry.export_path = new_path
            elif old_path_is_unique and new_path_is_unique:
                entry.export_path = new_path
                other_songs_updated = _update_others()

            # Log a message
            log_suffix = ''
            if other_songs_updated:
                log_suffix = _(' This also affected {} other {}.')\
                            .format(other_songs_updated, ngt('file', 'files', other_songs_updated))
            self.log.print_log(_('Entry path changed successfully.{}')\
                               .format(log_suffix))

            # Update the summary field
            self._update_preview_summary()

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
        self.log.print_log('Starting synchronization preview.')
        self.running = True

        # Change button visibility
        self.preview_start_button.set_visible(False)
        self.preview_stop_button.set_visible(True)

        success = self._run_preview()
        self._stop_preview(None)

        if not success:
            return

        self.sync_start_button.set_sensitive(True)
        self.log.print_log('Finished synchronization preview.')

    def _stop_preview(self, button):
        """
        Stop the generation of export paths for all songs.

        :param button: The stop preview button.
        """
        if button is not None:
            self.log.print_log('Stopping synchronization preview.')
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
        self.expanded_destination = os.path.expanduser(destination_path)

        # Get a list containing all songs to export
        songs = self._get_songs_from_queries()
        self.c_songs_copy = self.c_song_dupes = self.c_songs_delete = 0
        export_paths = []
        self.preview_model.clear()

        for song in songs:
            if not self.running:
                self.log.print_log('Stopped synchronization preview.')
                return False
            self._run_pending_events()

            export_path = self._get_export_path(song, destination_path, pattern)
            if not export_path:
                return False

            entry = Entry(song, export_path)

            expanded_path = os.path.expanduser(export_path)
            if expanded_path in export_paths:
                entry.set_duplicate_tag()
                self.c_song_dupes += 1
            else:
                self.c_songs_copy += 1
                export_paths.append(expanded_path)

            self.preview_model.append(row=[entry])

        # List files to delete
        for root, __, files in os.walk(self.expanded_destination):
            for name in files:
                file_path = os.path.join(root, name)
                if file_path not in export_paths:
                    entry = Entry(None)
                    entry.basename = os.path.relpath(file_path,
                                                     self.expanded_destination)
                    entry.filename = file_path
                    entry.set_delete_tag()
                    self.preview_model.append(row=[entry])
                    self.c_songs_delete += 1

        self._update_preview_summary()
        return True

    def _update_preview_summary(self):
        """
        Update the preview summary text field.
        """
        preview_summary_prefix = _('Synchronization will:  ')
        preview_summary = []

        if self.c_songs_copy > 0:
            counter = self.c_songs_copy
            preview_summary.append(
                _('write {} {}')\
                .format(counter, ngt('file', 'files', counter)))

        if self.c_song_dupes > 0:
            counter = self.c_song_dupes
            preview_summary.append(
                _('skip {} duplicate {}')\
                .format(counter, ngt('file', 'files', counter)))

        if self.c_songs_delete > 0:
            counter = self.c_songs_delete
            preview_summary.append(
                _('delete {} {}')\
                .format(counter, ngt('file', 'files', counter)))

        preview_summary_text = ',  '.join(preview_summary)
        preview_summary_text = preview_summary_prefix + preview_summary_text
        self.prvw_summary_label.set_label(preview_summary_text)
        self.prvw_summary_label.set_visible(True)
        self.prvw_info_label.set_visible(True)
        self.log.print_log(preview_summary_text)

    def _get_paths(self):
        """
        Build a list of all current export paths for the songs to be
        synchronized.
        """
        paths = {}
        for entry in self.preview_model.values():
            if entry.tag != Entry.Tags.DELETE and entry.export_display != '':
                if entry.export_path not in paths.keys():
                    paths[entry.export_path] = 1
                else:
                    paths[entry.export_path] += 1
        return paths

    def _show_sync_error(self, title, message):
        """
        Show an error message whenever a synchronization error occurs.

        :param title:   The title of the message popup.
        :param message: The error message.
        """
        qltk.ErrorMessage(self.main_vbox, title, message).run()
        self.log.print_log(_('Synchronization failed: {}').format(title))

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

        try:
            relative_name = new_name.relative_to(self.expanded_destination)
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
        self.log.print_log('Starting song synchronization.')
        self.running = True

        # Change button visibility
        self.sync_start_button.set_visible(False)
        self.sync_stop_button.set_visible(True)

        if not self._run_sync():
            return

        self._stop_sync(None)
        self.log.print_log('Finished song synchronization.')

    def _stop_sync(self, button):
        """
        Stop the song synchronization.

        :param button: The stop sync button.
        """
        if button is not None:
            self.log.print_log('Stopping song synchronization.')
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
        counter_written = counter_skipped = counter_deleted = 1

        counter_len = max(self._get_count_digits(self.c_songs_copy),
                          self._get_count_digits(self.c_song_dupes))
        counter_format = '{:0' + str(counter_len) + 'd}'
        log_template = '{0}/{0}'.format(counter_format) + '  {tag:18s}  {path:s}'
        self.log_template = log_template

        for entry in self.preview_model.values():
            if not self.running:
                self.log.print_log('Stopped song synchronization.')
                return False
            self._run_pending_events()

            if not entry.export_path and not entry.tag:
                continue  # to next entry

            expanded_path = os.path.expanduser(entry.export_path)
            if entry.tag == Entry.Tags.BLANK:
                # Export, skipping existing files
                if os.path.exists(expanded_path):
                    self.log.print_log(
                        log_template.format(counter_written, self.c_songs_copy,
                                            tag=Entry.Tags.STATUS_EXISTS,
                                            path=expanded_path))
                else:
                    self.log.print_log(
                        log_template.format(counter_written, self.c_songs_copy,
                                            tag=Entry.Tags.STATUS_COPY,
                                            path=expanded_path))
                    song_folders = os.path.dirname(expanded_path)
                    os.makedirs(song_folders, exist_ok=True)
                    copyfile(entry.filename, expanded_path)
                counter_written += 1

            elif entry.tag == Entry.Tags.DUPLICATE:
                # Skip duplicates
                self.log.print_log(
                    log_template.format(counter_skipped, self.c_song_dupes,
                                        tag=Entry.Tags.DUPLICATE,
                                        path=expanded_path))
                counter_skipped += 1

            elif entry.tag == Entry.Tags.DELETE:
                # Delete file
                self.log.print_log(
                    log_template.format(counter_deleted, self.c_songs_delete,
                                        tag=Entry.Tags.DELETE,
                                        path=entry.filename))
                try:
                    os.remove(entry.filename)
                    counter_deleted += 1
                except IsADirectoryError:
                    pass
                except OSError as ex:
                    self.log.print_log(_('Failed to delete: {}').format(ex))

        self._remove_empty_dirs()
        return True

    def _get_count_digits(self, number):
        """
        Get the number of digits in an integer.

        :param number: The number.
        """
        # Handle zero
        if number == 0:
            return 1

        # Handle negative numbers
        number = abs(number)

        if number <= 999999999999997:
            return floor(log10(number)) + 1

        # Avoid floating-point errors for large numbers
        return len(str(number))

    def _remove_empty_dirs(self):
        """
        Delete all empty sub-directories from the given path.
        """
        for root, dirs, __ in os.walk(self.expanded_destination, topdown=False):
            for dirname in dirs:
                dir_path = os.path.realpath(os.path.join(root, dirname))
                if not os.listdir(dir_path):
                    try:
                        self.log.print_log(
                            self.log_template.format(0, 0,
                                                     tag=Entry.Tags.DELETE,
                                                     path=dir_path))
                        os.rmdir(dir_path)
                    except OSError as ex:
                        self.log.print_log(_('Failed to delete: {}').format(ex))
