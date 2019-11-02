# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

from tests import TestCase, mkstemp, mkdtemp

import os
import sys
import shutil

from quodlibet import player
from quodlibet.library import SongLibrarian, SongLibrary
from quodlibet.plugins import PluginManager
from quodlibet.plugins.events import EventPluginHandler
from quodlibet.qltk.songlist import SongList


class TEventPlugins(TestCase):

    def setUp(self):
        self.tempdir = mkdtemp()
        self.pm = PluginManager(folders=[self.tempdir])
        self.lib = SongLibrarian()
        lib = SongLibrary()
        lib.librarian = self.lib
        self.songlist = SongList(library=lib)
        self.player = player.init_player("nullbe", self.lib)
        self.handler = EventPluginHandler(
            librarian=self.lib, player=self.player, songlist=self.songlist)
        self.pm.register_handler(self.handler)
        self.pm.rescan()
        self.assertEquals(self.pm.plugins, [])

    def tearDown(self):
        self.pm.quit()
        shutil.rmtree(self.tempdir)

    def create_plugin(self, name='', funcs=None):
        fd, fn = mkstemp(suffix='.py', text=True, dir=self.tempdir)
        file = os.fdopen(fd, 'w')

        file.write("from quodlibet.plugins.events import EventPlugin\n")
        file.write("log = []\n")
        file.write("class %s(EventPlugin):\n" % name)
        indent = '    '
        file.write("%spass\n" % indent)

        if name:
            file.write("%sPLUGIN_ID = %r\n" % (indent, name))
            file.write("%sPLUGIN_NAME = %r\n" % (indent, name))

        for f in (funcs or []):
            file.write("%sdef %s(s, *args): log.append((%r, args))\n" %
                       (indent, f, f))
        file.flush()
        file.close()

    def _get_calls(self, plugin):
        mod = sys.modules[plugin.cls.__module__]
        return mod.log

    def test_found(self):
        self.create_plugin(name='Name')
        self.pm.rescan()
        self.assertEquals(len(self.pm.plugins), 1)

    def test_player_paused(self):
        self.create_plugin(name='Name', funcs=["plugin_on_paused"])
        self.pm.rescan()
        self.assertEquals(len(self.pm.plugins), 1)
        plugin = self.pm.plugins[0]
        self.pm.enable(plugin, True)
        self.player.emit("paused")
        self.failUnlessEqual([("plugin_on_paused", tuple())],
                             self._get_calls(plugin))

    def test_lib_changed(self):
        self.create_plugin(name='Name', funcs=["plugin_on_changed"])
        self.pm.rescan()
        self.assertEquals(len(self.pm.plugins), 1)
        plugin = self.pm.plugins[0]
        self.pm.enable(plugin, True)
        self.lib.emit("changed", [None])
        self.failUnlessEqual([("plugin_on_changed", ([None],))],
                             self._get_calls(plugin))

    def test_songs_selected(self):
        self.create_plugin(name='Name', funcs=["plugin_on_songs_selected"])
        self.pm.rescan()
        self.assertEquals(len(self.pm.plugins), 1)
        plugin = self.pm.plugins[0]
        self.pm.enable(plugin, True)
        self.songlist.emit("selection-changed", self.songlist.get_selection())
        self.failUnlessEqual(self._get_calls(plugin),
                             [("plugin_on_songs_selected", ([], ))])
