# -*- coding: utf-8 -*-
# Copyright 2018 Jan Korte
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import os
import string
from pathlib import Path

from gi.repository import Gtk, GLib
from quodlibet import _, app
from quodlibet import config
from quodlibet import get_user_dir
from quodlibet import qltk
from quodlibet.plugins import PluginConfigMixin
from quodlibet.plugins.events import EventPlugin
from quodlibet.qltk import Icons
from quodlibet.qltk.ccb import ConfigCheckButton
from quodlibet.query import Query

from shutil import copyfile


class NoSavedQueriesError(Exception):
    """
    Exception raised when there are no saved searches.
    """
    pass


class SyncToDevice(EventPlugin, PluginConfigMixin):
    PLUGIN_ICON = Icons.NETWORK_TRANSMIT
    PLUGIN_ID = "synchronize_to_device"
    PLUGIN_NAME = _("Synchronize to Device")
    PLUGIN_DESC = _(
        "Synchronizes all songs from the selected saved searches with the "
        "specified folder.\n"
        "All songs in the destination folder that aren't in the saved searches "
        "will be deleted."
    )
    config_path_key = __name__ + '_path'

    spacing_main = 20
    spacing_large = 6
    spacing_small = 3

    def PluginPreferences(self, parent):
        vbox = Gtk.VBox(spacing=self.spacing_main)

        query_path = os.path.join(get_user_dir(), 'lists', 'queries.saved')
        try:
            with open(query_path, 'r', encoding="utf-8") as query_file:
                # Define output window
                log = Gtk.TextView()
                log.set_left_margin(5)
                log.set_right_margin(5)
                log.props.editable = False
                buffer = log.get_buffer()
                scroll = Gtk.ScrolledWindow()
                scroll.set_size_request(-1, 100)
                scroll.add(log)

                def append(text):
                    """ Print text to the output TextView window. """
                    GLib.idle_add(lambda: buffer.insert(buffer.get_end_iter(),
                                                        text + "\n"))
                    GLib.idle_add(
                        lambda: log.scroll_to_mark(buffer.get_insert(), 0.0,
                                                   True, 0.5, 0.5))

                # Read saved searches from file
                queries = {}
                for query_string in query_file:
                    name = next(query_file).strip()
                    queries[name] = Query(query_string.strip())

                if not queries:
                    # query_file is empty
                    raise NoSavedQueriesError

                # Saved search selection frame
                saved_search_vbox = Gtk.VBox(spacing=self.spacing_large)
                for query_name, query in queries.items():
                    check_button = ConfigCheckButton(query_name, "plugins",
                                                     self._config_key(
                                                         query_name))
                    check_button.set_active(self.config_get_bool(query_name))
                    saved_search_vbox.pack_start(check_button, False, True, 0)
                frame = qltk.Frame(
                    label=_("Synchronize the following saved searches:"),
                    child=saved_search_vbox)
                vbox.pack_start(frame, True, True, 0)

                def synchronize():
                    """
                    Synchronize the songs from the selected saved searches
                    with the specified folder
                    """
                    enabled_queries = []
                    for query_name, query in queries.items():
                        if self.config_get_bool(query_name):
                            enabled_queries.append(query)

                    selected_songs = []
                    for song in app.library.itervalues():
                        if any(query.search(song) for query in
                               enabled_queries):
                            selected_songs.append(song)

                    filename_list = []
                    destination = destination_entry.get_text()
                    if destination == '':
                        append(_("Destination path is empty, please provide "
                                 "it!"))
                    else:
                        try:
                            append("Starting…")
                            for song in selected_songs:
                                if not self.running:
                                    append("Stopped the synchronization.")
                                    return

                                # Prevent the application from becoming
                                # unreponsive
                                while Gtk.events_pending():
                                    Gtk.main_iteration()

                                song_path = song['~filename']
                                file_ext = song("~filename").split('.')[-1]
                                song_file_name = filter_valid_chars(
                                    "{}.{}".format(song("title"), file_ext))

                                folder = os.path.join(destination,
                                                      filter_valid_chars(
                                                          song("artist")),
                                                      filter_valid_chars(
                                                          song("album")))
                                os.makedirs(folder, exist_ok=True)
                                dest_file = os.path.join(folder,
                                                         song_file_name)
                                filename_list.append(
                                    os.path.join(folder, song_file_name))

                                # Skip existing files
                                if os.path.exists(dest_file):
                                    append(
                                        "Skipped '{}' because it already "
                                        "exists.".format(dest_file))
                                else:
                                    append(
                                        "Writing '{}'…".format(dest_file))
                                    copyfile(song_path, dest_file)

                            # Delete files which are not
                            # in the saved searches anymore
                            for existing_file in Path(destination) \
                                    .rglob('*.' + file_ext):
                                if str(existing_file) not in filename_list:
                                    append(
                                        "Deleted '{}'.".format(existing_file))
                                    try:
                                        os.remove(str(existing_file))
                                    except IsADirectoryError:
                                        pass

                            remove_empty_dirs(destination)

                            append("Synchronization finished.")
                        except FileNotFoundError as e:
                            append(str(e))

                def remove_empty_dirs(path):
                    """ Delete all empty sub-directories from the given path """
                    for root, dirnames, filenames in os.walk(path,
                                                             topdown=False):
                        for dirname in dirnames:
                            try:
                                os.rmdir(os.path.realpath(
                                    os.path.join(root, dirname)))
                            except OSError:
                                pass

                def filter_valid_chars(str):
                    """ Remove invalid FAT32 filename characters """
                    valid_chars = "-_.() %s%s" % (
                        string.ascii_letters, string.digits)
                    return "".join(char for char in str if char in valid_chars)

                def start(button):
                    """ Start the song synchronization """
                    self.running = True
                    self._start_button.set_visible(False)
                    self._stop_button.set_visible(True)
                    synchronize()
                    self._start_button.set_visible(True)
                    self._stop_button.set_visible(False)

                def stop(button):
                    """ Stop the song synchronization """
                    append("Stopping…")
                    self.running = False
                    self._start_button.set_visible(True)
                    self._stop_button.set_visible(False)

                def path_changed(entry):
                    """ Save the destination path to the global config """
                    config.set('plugins', self.config_path_key,
                               entry.get_text())

                # Destination path entry field
                destination_entry = Gtk.Entry()
                destination_entry.set_text(
                    config.get('plugins', self.config_path_key, ''))
                destination_entry.connect('changed', path_changed)

                # Destination path hbox
                destination_path_box = Gtk.HBox(spacing=self.spacing_small)
                destination_path_box.pack_start(destination_entry, True, True,
                                                0)

                # Destination path hint
                dest_label = ("( e.g. the absolute path to your SD card. "
                             "If you mount your device with MTP, specify a "
                             "local destination folder and transfer it to your "
                             "device with rsync. )")
                label = Gtk.Label(label=_(dest_label))
                label.set_alignment(0.0, 0.5)

                # Destination path frame
                destination_vbox = Gtk.VBox(spacing=self.spacing_large)
                destination_vbox.pack_start(destination_path_box, True, True, 0)
                destination_vbox.pack_start(label, True, True, 0)
                frame = qltk.Frame(
                    label=_("Destination path:"),
                    child=destination_vbox)
                vbox.pack_start(frame, True, True, 0)

                # Start button
                start_button = qltk.Button(label=_("Start synchronization"),
                                           icon_name=Icons.DOCUMENT_SAVE)
                start_button.connect('clicked', start)
                start_button.set_visible(True)
                self._start_button = start_button

                # Stop button
                stop_button = qltk.Button(label=_("Stop synchronization"),
                                          icon_name=Icons.PROCESS_STOP)
                stop_button.connect('clicked', stop)
                stop_button.set_visible(False)
                stop_button.set_no_show_all(True)
                self._stop_button = stop_button

                # Section for the action buttons and output window
                run_vbox = Gtk.VBox(spacing=self.spacing_large)
                run_vbox.pack_start(start_button, True, True, 0)
                run_vbox.pack_start(stop_button, True, True, 0)
                run_vbox.pack_start(Gtk.Label(label=_("Progress:")), True, True, 0)
                run_vbox.pack_start(scroll, True, True, 0)
                vbox.pack_start(run_vbox, True, True, 0)

                return vbox
        except NoSavedQueriesError:
            return qltk.Frame(
                _("No saved searches yet, create some and come back!"))
