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


class SyncToDevice(EventPlugin, PluginConfigMixin):
    PLUGIN_ICON = Icons.NETWORK_TRANSMIT
    PLUGIN_ID = "synchronize_to_device"
    PLUGIN_NAME = _("Synchronize to Device")
    PLUGIN_DESC = _(
        "Synchronizes all songs from the selected saved searches with the "
        "specified folder."
    )
    config_path_key = __name__ + '_path'

    spacing_main = 20
    spacing_large = 6
    spacing_small = 3

    def PluginPreferences(self, parent):
        vbox = Gtk.VBox(spacing=self.spacing_main)

        # Define output window
        self.log_label = Gtk.Label(label=_("Progress:"),
                                   xalign=0.0, yalign=0.5, wrap=True,
                                   visible=False, no_show_all=True)
        self.log = Gtk.TextView(left_margin=5, right_margin=5, editable=False,
                                visible=False, no_show_all=True)
        log_buf = self.log.get_buffer()
        log_scroll = Gtk.ScrolledWindow(min_content_height=100,
                                        max_content_height=300,
                                        propagate_natural_height=True)
        log_scroll.add(self.log)

        def append(text):
            """ Print text to the output TextView window. """
            GLib.idle_add(lambda: log_buf.insert(log_buf.get_end_iter(),
                                                    text + "\n"))
            GLib.idle_add(lambda: self.log.scroll_to_mark(log_buf.get_insert(),
                                                          0.0, True, 0.5, 0.5))

        # Read saved searches from file
        queries = {}
        query_path = os.path.join(get_user_dir(), 'lists', 'queries.saved')
        with open(query_path, 'r', encoding="utf-8") as query_file:
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
            check_button = ConfigCheckButton(query_name, "plugins",
                                             self._config_key(query_name))
            check_button.set_active(self.config_get_bool(query_name))
            saved_search_vbox.pack_start(check_button, False, False, 0)
        saved_search_scroll = Gtk.ScrolledWindow(min_content_height=0,
                                                 max_content_height=300,
                                                 propagate_natural_height=True)
        saved_search_scroll.add(saved_search_vbox)
        frame = qltk.Frame(
            label=_("Synchronize the following saved searches:"),
            child=saved_search_scroll)
        vbox.pack_start(frame, False, False, 0)

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
                if any(query.search(song) for query in enabled_queries):
                    selected_songs.append(song)

            filename_list = []
            destination = destination_entry.get_text()
            if destination == '':
                append(_("Destination path is empty, please provide it!"))
                return

            try:
                append(_("Starting synchronization…"))
                for song in selected_songs:
                    if not self.running:
                        append(_("Stopped the synchronization."))
                        return

                    # Prevent the application from becoming unreponsive
                    while Gtk.events_pending():
                        Gtk.main_iteration()

                    song_path = song['~filename']
                    file_ext = song("~filename").split('.')[-1]
                    song_file_name = filter_valid_chars(
                        "{}.{}".format(song("title"), file_ext))

                    folder = os.path.join(destination,
                                          filter_valid_chars(song("artist")),
                                          filter_valid_chars(song("album")))
                    os.makedirs(folder, exist_ok=True)
                    dest_file = os.path.join(folder, song_file_name)
                    filename_list.append(dest_file)

                    # Skip existing files
                    if os.path.exists(dest_file):
                        append(_("Skipped '{}' because it already exists." \
                                 .format(dest_file)))
                    else:
                        append(_("Writing '{}'…".format(dest_file)))
                        copyfile(song_path, dest_file)

                # Delete files from the destination directory
                # which are not in the saved searches
                for existing_file in Path(destination).rglob('*.' + file_ext):
                    if str(existing_file) not in filename_list:
                        append(_("Deleting '{}'…".format(existing_file)))
                        try:
                            os.remove(str(existing_file))
                        except IsADirectoryError:
                            pass
                remove_empty_dirs(destination)

                append(_("Synchronization finished."))
            except FileNotFoundError as e:
                append(str(e))

        def remove_empty_dirs(path):
            """ Delete all empty sub-directories from the given path """
            for root, dirnames, filenames in os.walk(path, topdown=False):
                for dirname in dirnames:
                    try:
                        os.rmdir(os.path.realpath(os.path.join(root, dirname)))
                    except OSError:
                        pass

        def filter_valid_chars(str):
            """ Remove invalid FAT32 filename characters """
            valid_chars = "-_.() %s%s" % (string.ascii_letters, string.digits)
            return "".join(char for char in str if char in valid_chars)

        def path_changed(entry):
            """ Save the destination path to the global config """
            config.set('plugins', self.config_path_key, entry.get_text())

        # Destination path entry field
        destination_entry = Gtk.Entry(
            placeholder_text=_("e.g. the absolute path to your SD card"),
            text=config.get('plugins', self.config_path_key, '')
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

        # Start button
        start_button = qltk.Button(label=_("Start synchronization"),
                                   icon_name=Icons.DOCUMENT_SAVE)
        start_button.connect('clicked', start)
        start_button.set_visible(True)
        self.start_button = start_button

        # Stop button
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
