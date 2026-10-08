"""Offline hosting regression tests. No network requests or real credentials."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kazeros.config import ConfigError, data_dir, load_config, load_token
from kazeros.instance import InstanceLock
from kazeros.store import Store

SAMPLE = 'A' * 32 + '.' + 'B' * 8 + '.' + 'C' * 32  # synthetic test-only string

class HostingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_environment_only_install_needs_no_config_json(self):
        os.environ['DISCORD_GUILD_ID'] = '123456789012345678'
        c = load_config(self.root)
        self.assertEqual(c['discord_guild_id'], 123456789012345678)
        self.assertEqual(c['game_region'], 'eu')
        self.assertEqual(c['guild_name'], 'KAZEROS')
        self.assertFalse((self.root / 'config.json').exists())

    def test_environment_overrides_local_settings(self):
        (self.root / 'config.json').write_text(json.dumps({'discord_guild_id': 100, 'game_server_id': 99}))
        os.environ.update(DISCORD_GUILD_ID='200', GAME_SERVER_ID='0')
        c = load_config(self.root)
        self.assertEqual(c['discord_guild_id'], 200)
        self.assertIsNone(c['game_server_id'])

    def test_local_config_remains_supported(self):
        (self.root / 'config.json').write_text(json.dumps({'discord_guild_id': 100}))
        self.assertEqual(load_config(self.root)['discord_guild_id'], 100)

    def test_missing_id_is_actionable(self):
        with self.assertRaisesRegex(ConfigError, 'DISCORD_GUILD_ID'):
            load_config(self.root)

    def test_invalid_discord_id_is_rejected(self):
        for value in ['not-an-id', '0', '-100', str(2**64)]:
            with self.subTest(value=value):
                os.environ['DISCORD_GUILD_ID'] = value
                with self.assertRaises(ConfigError):
                    load_config(self.root)

    def test_game_fields_from_environment(self):
        os.environ.update(DISCORD_GUILD_ID='100', GAME_REGION='europe', GAME_SERVER_ID='1312', GAME_SERVER_NAME='Luteros')
        c = load_config(self.root)
        self.assertEqual((c['game_region'], c['game_server_id'], c['game_server_name']), ('eu', 1312, 'Luteros'))

    def test_invalid_json_is_not_silently_ignored(self):
        os.environ['DISCORD_GUILD_ID'] = '100'
        (self.root / 'config.json').write_text('{bad')
        with self.assertRaises(ConfigError):
            load_config(self.root)

    def test_bot_token_alias(self):
        os.environ['BOT_TOKEN'] = SAMPLE
        self.assertEqual(load_token(self.root), SAMPLE)

    def test_discord_bot_token_alias(self):
        os.environ['DISCORD_BOT_TOKEN'] = SAMPLE
        self.assertEqual(load_token(self.root), SAMPLE)

    def test_discord_token_alias_and_prefix(self):
        os.environ['DISCORD_TOKEN'] = 'Bot ' + SAMPLE
        self.assertEqual(load_token(self.root), SAMPLE)

    def test_token_precedence_is_documented_and_deterministic(self):
        os.environ.update(DISCORD_TOKEN=SAMPLE, BOT_TOKEN='X' * 40)
        self.assertEqual(load_token(self.root), SAMPLE)

    def test_token_error_does_not_echo_secret(self):
        secret = 'private\ncredential'
        os.environ['BOT_TOKEN'] = secret
        with self.assertRaises(ConfigError) as result:
            load_token(self.root)
        self.assertNotIn(secret, str(result.exception))

    def test_local_dotenv_still_works(self):
        (self.root / '.env').write_text('DISCORD_TOKEN="' + SAMPLE + '"\n')
        self.assertEqual(load_token(self.root), SAMPLE)

    def test_storage_override_is_respected(self):
        os.environ['KAZEROS_DATA_DIR'] = '/app/data'
        self.assertEqual(data_dir(self.root), Path('/app/data'))

    def test_host_data_dir_alias(self):
        os.environ['DATA_DIR'] = '/app/data'
        self.assertEqual(data_dir(self.root), Path('/app/data'))

    def test_bot_id_detects_bothost(self):
        os.environ['BOT_ID'] = 'test-deployment'
        self.assertEqual(data_dir(self.root), Path('/app/data'))

    def test_local_storage_default_unchanged(self):
        self.assertEqual(data_dir(self.root), self.root / 'runtime')

    def test_database_survives_close_and_reopen(self):
        os.environ['KAZEROS_DATA_DIR'] = str(self.root / 'persistent')
        path = data_dir(self.root) / 'coach.sqlite3'
        store = Store(path)
        ref = {'region':'eu', 'server_id':1312, 'character_id':'synthetic='}
        prefs = {'goal':'pve', 'role':'tank', 'budget':'low'}
        store.bind(100, 200, ref, prefs)
        store.close()
        reopened = Store(path)
        self.assertEqual(reopened.get(100,200)['ref'], ref)
        reopened.close()

    def test_lock_works_in_persistent_directory(self):
        path = self.root / 'persistent' / 'bot.lock'
        with InstanceLock(path):
            with self.assertRaises(RuntimeError):
                with InstanceLock(path):
                    pass
        with InstanceLock(path):
            pass

if __name__ == '__main__':
    unittest.main()
