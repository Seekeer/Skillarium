"""Bridge of the legacy layout: a library clone that still carries the engine
installs Skillarium from the releases during its sync, configures the clone and
moves its schedule to the installed engine (stage 7 of the distribution plan).
"""
import json
import os
from pathlib import Path
import unittest

from fake_releases import FakeReleases
from fixture import HAS_BASH_CLIENT, POWERSHELL, Library

WINDOWS = os.name == 'nt'


class Bridge:
    def setUp(self):
        self.lib = Library(self)
        self.lib.register()
        self.releases = FakeReleases(self.lib.root / 'releases')
        self.addCleanup(self.releases.stop)
        self.engine_home = self.lib.root / 'skillarium home'
        self.lib.env.pop('SKILLARIUM_NO_BRIDGE')
        self.lib.env.update(SKILLARIUM_HOME=str(self.engine_home), SKILLARIUM_BIN=str(self.lib.root / 'bin'),
                            APPDATA=str(self.lib.home / 'AppData' / 'Roaming'),
                            XDG_CONFIG_HOME=str(self.lib.home / '.config'), **self.releases.env())
        self.lib.env.pop('SKILLARIUM_LIBRARY', None)
        self.clone = self.lib.clone('station', 'station')

    def config(self):
        raise NotImplementedError

    def sync(self, expected=0):
        raise NotImplementedError

    def downloads(self):
        return [path for path in self.releases.requests if 'skillarium-engine-' in path]

    def test_sync_installs_skillarium_once_and_configures_the_clone(self):
        self.releases.publish('0.1.0')
        self.sync()
        self.assertEqual((self.engine_home / 'current' / 'VERSION').read_text().strip(), '0.1.0')
        self.assertEqual(Path(json.loads(self.config().read_text(encoding='utf-8'))['library']).resolve(),
                         self.clone.resolve())
        self.assertTrue(self.lib.installed().exists())
        self.sync()
        self.assertEqual(len(self.downloads()), 1)

    def test_unreachable_releases_only_postpone_the_bridge(self):
        self.lib.env['SKILLARIUM_RELEASES'] = 'http://127.0.0.1:9/releases'
        output = self.sync()
        self.assertIn('bridge postponed', output)
        self.assertFalse((self.engine_home / 'current').exists())
        self.assertTrue(self.lib.installed().exists())

    def test_configured_library_is_kept(self):
        self.releases.publish('0.1.0')
        self.config().parent.mkdir(parents=True, exist_ok=True)
        self.config().write_text(json.dumps({'library': 'elsewhere'}), encoding='utf-8')
        self.sync()
        self.assertEqual(json.loads(self.config().read_text(encoding='utf-8'))['library'], 'elsewhere')

    def test_bridge_can_be_switched_off(self):
        self.releases.publish('0.1.0')
        self.lib.env['SKILLARIUM_NO_BRIDGE'] = '1'
        self.sync()
        self.assertEqual(self.downloads(), [])


@unittest.skipUnless(WINDOWS and POWERSHELL, 'Windows PowerShell unavailable')
class PowerShellBridgeTests(Bridge, unittest.TestCase):
    def config(self):
        return self.lib.home / 'AppData' / 'Roaming' / 'Skillarium' / 'config.json'

    def sync(self, expected=0):
        return self.lib.powershell(self.clone, 'Sync', expected=expected)


@unittest.skipUnless(HAS_BASH_CLIENT, 'Linux client tools unavailable')
class BashBridgeTests(Bridge, unittest.TestCase):
    def config(self):
        return self.lib.home / '.config' / 'skillarium' / 'config.json'

    def sync(self, expected=0):
        return self.lib.sync(self.clone, expected=expected)

    def test_managed_timer_moves_to_the_installed_engine(self):
        self.releases.publish('0.1.0')
        units = self.lib.home / '.config' / 'systemd' / 'user'
        units.mkdir(parents=True)
        service = units / 'agent-skills.service'
        service.write_text(f'# Managed by agent-skills\n[Service]\nExecStart=/usr/bin/bash "{self.clone}/install.sh" --sync-only\n')
        tools = self.lib.root / 'tools'
        tools.mkdir()
        (tools / 'systemctl').write_text('#!/bin/sh\nexit 0\n')
        (tools / 'systemctl').chmod(0o755)
        self.lib.env['PATH'] = f'{tools}{os.pathsep}{self.lib.env["PATH"]}'
        self.assertIn('Hourly sync now runs', self.sync())
        text = service.read_text()
        self.assertIn(f'ExecStart=/usr/bin/bash "{self.engine_home}/current/install.sh" --library "{self.clone.resolve()}" --sync-only', text)


if __name__ == '__main__':
    unittest.main()
