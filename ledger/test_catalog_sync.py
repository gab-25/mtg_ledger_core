from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from . import catalog_sync


class SyncCommandTest(TestCase):
    def test_command_only_enqueues(self):
        from . import tasks

        with mock.patch.object(tasks.sync_catalog, "delay", return_value=mock.Mock(id="abc")) as delay, \
                mock.patch.object(catalog_sync, "run_sync") as run:
            out = StringIO()
            call_command("sync_catalog", stdout=out)
        delay.assert_called_once_with()
        run.assert_not_called()
        self.assertIn("Catalog sync queued (task abc)", out.getvalue())

    def test_task_runs_sync(self):
        from . import tasks

        with mock.patch.object(catalog_sync, "run_sync") as run:
            tasks.sync_catalog.apply()
        run.assert_called_once_with()

    def test_no_beat_schedule(self):
        from config.celery import app

        self.assertFalse(app.conf.beat_schedule)
