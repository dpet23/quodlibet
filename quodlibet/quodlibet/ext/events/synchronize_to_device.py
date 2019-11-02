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
from quodlibet.qltk.ccb import ConfigCheckButton
from quodlibet.query import Query

from shutil import copyfile


class SyncToDevice(EventPlugin, PluginConfigMixin):
    PLUGIN_ID = "synchronize_to_device"
    PLUGIN_NAME = _("Synchronize to Device")
    PLUGIN_DESC = _(
        "Synchronizes all songs from the selected saved searches with the "
        "specified folder. All songs in that folder, which are not in the "
        "saved searches, will be deleted.")
    config_path_key = __name__ + '_path'

    def PluginPreferences(self, parent):
        vbox = Gtk.VBox(spacing=6)

        query_path = os.path.join(get_user_dir(), 'lists', 'queries.saved')
        try:
            with open(query_path, 'r', encoding="utf-8") as query_file:
                if not query_file.read(1):
                    raise FileNotFoundError
                log = Gtk.TextView()
                log.set_left_margin(5)
                log.set_right_margin(5)
                log.props.editable = False
                buffer = log.get_buffer()
                scroll = Gtk.ScrolledWindow()
                scroll.set_size_request(-1, 100)
                scroll.add(log)

                def append(text):
                    GLib.idle_add(lambda: buffer.insert(buffer.get_end_iter(),
                                                        text + "\n"))
                    GLib.idle_add(
                        lambda: log.scroll_to_mark(buffer.get_insert(), 0.0,
                                                   True, 0.5, 0.5))

                queries = {}
                for query_string in query_file:
                    name = next(query_file).strip()
                    queries[name] = Query(query_string.strip())

                for query_name, query in queries.items():
                    check_button = ConfigCheckButton(query_name, "plugins",
                                                     self._config_key(
                                                         query_name))
                    check_button.set_active(self.config_get_bool(query_name))
                    vbox.pack_start(check_button, False, True, 0)

                def synchronize():
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
                            append("Starting...")
                            for song in selected_songs:
                                if not self.running:
                                    append("Stopped the synchronization.")
                                    return

                                # prevent the application from becoming
                                # unreponsive
                                while Gtk.events_pending():
                                    Gtk.main_iteration()

                                song_path = song['~filename']
                                file_ext = song("~filename").split('.')[-1]
                                song_file_name = "{}.{}".format(song("title"),
                                                                file_ext)
                                # workaround file naming limitations on fat32
                                # formated drives
                                valid_chars = "-_.() %s%s" % (
                                    string.ascii_letters, string.digits)
                                song_file_name = "".join(
                                    char for char in song_file_name if
                                    char in valid_chars)
                                folder = os.path.join(destination,
                                                      song("artist"),
                                                      song("album"))
                                os.makedirs(folder, exist_ok=True)
                                dest_file = os.path.join(folder,
                                                         song_file_name)
                                filename_list.append(
                                    os.path.join(folder, song_file_name))

                                # skip existing files
                                if os.path.exists(dest_file):
                                    append(
                                        "Skipped '{}' because it already "
                                        "exists.".format(dest_file))
                                else:
                                    append(
                                        "Writing '{}'...".format(dest_file))
                                    copyfile(song_path, dest_file)

                            # delete files which are not
                            # in the saved searches anymore
                            for existing_file in Path(destination).rglob('*.*'):
                                if str(existing_file) not in filename_list:
                                    append(
                                        "Deleted '{}'.".format(existing_file))
                                    os.remove(str(existing_file))

                            remove_empty_dirs(destination)

                            append("Synchronization finished.")
                        except FileNotFoundError as e:
                            append(str(e))

                def remove_empty_dirs(path):
                    for root, dirnames, filenames in os.walk(path,
                                                             topdown=False):
                        for dirname in dirnames:
                            try:
                                os.rmdir(os.path.realpath(
                                    os.path.join(root, dirname)))
                            except OSError:
                                pass

                def start(button):
                    self.running = True
                    synchronize()

                def stop(button):
                    append("Stopping...")
                    self.running = False

                def path_changed(entry):
                    config.set('plugins', self.config_path_key,
                               entry.get_text())

                destination_path_box = Gtk.HBox(spacing=3)
                destination_entry = Gtk.Entry()
                destination_entry.set_tooltip_text("The absolute path of your"
                                                   " device, e.g. '/run/media/"
                                                   "my-user/device-id/Music'")
                destination_entry.set_text(
                    config.get('plugins', self.config_path_key, ''))
                destination_entry.connect('changed', path_changed)
                destination_path_box.pack_start(
                    Gtk.Label(label=_("Destination path:")), False, False, 0)
                destination_path_box.pack_start(destination_entry, True, True,
                                                0)

                start_button = Gtk.Button(label=_("Start synchronization"))
                start_button.connect('clicked', start)

                stop_button = Gtk.Button(label=_("Stop synchronization"))
                stop_button.connect('clicked', stop)

                vbox.pack_start(destination_path_box, True, True, 0)
                vbox.pack_start(start_button, True, True, 0)
                vbox.pack_start(stop_button, True, True, 0)
                vbox.pack_start(Gtk.Label(label=_("Progress:")), True, True, 0)
                vbox.pack_start(scroll, True, True, 0)
                return qltk.Frame(
                    _("The following saved searches shall be synchronized:"),
                    child=vbox)
        except FileNotFoundError:
            return qltk.Frame(
                _("No saved searches yet, create some and come back!"))
