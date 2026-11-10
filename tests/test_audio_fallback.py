"""
Tests for the ``missing_audio_policy`` option (issue #22).

When a requested audio language is unavailable for an episode:
  - ``skip_if_none`` (default): skip only if none of the requested locales exists.
  - ``skip_if_any``: skip as soon as one requested locale is missing.
  - ``fallback``: never skip; use the first available version (legacy behavior).

Skips are signalled with ``AudioUnavailableError``.
"""

import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crunchyroll.downloader import (
    AudioUnavailableError,
    _resolve_dub_versions,
    download_episode,
    download_season,
    normalize_missing_audio_policy,
)
from crunchyroll.queue import DownloadQueue, DownloadTask, QueueItem
from crunchyroll.state_store import StateStore
from crunchyroll.types import DubVersion, EpisodeInfo, EpisodeMetadata, SeasonEpisode


def make_info(versions, audio_locale="ja-JP"):
    return EpisodeInfo(
        title="Ep 1",
        episode_metadata=EpisodeMetadata(
            audio_locale=audio_locale,
            episode_number=1,
            season_number=1,
            series_title="Mock Series",
            availability_starts="",
            versions=versions,
        ),
        subtitles={},
    )


def only_ja():
    return make_info([DubVersion(audio_locale="ja-JP", guid="ja-guid", media_guid="ja-media")])


def ja_and_en():
    return make_info(
        [
            DubVersion(audio_locale="ja-JP", guid="ja-guid"),
            DubVersion(audio_locale="en-US", guid="en-guid"),
        ]
    )


class NormalizePolicyTests(unittest.TestCase):
    def test_valid_values_and_dash_spelling(self):
        self.assertEqual(normalize_missing_audio_policy("skip_if_any"), "skip_if_any")
        self.assertEqual(normalize_missing_audio_policy("skip-if-any"), "skip_if_any")
        self.assertEqual(normalize_missing_audio_policy("FALLBACK"), "fallback")

    def test_unknown_values_use_default(self):
        for value in (None, "", "nope", True):
            self.assertEqual(normalize_missing_audio_policy(value), "skip_if_none")


class ResolveDubVersionsTests(unittest.TestCase):
    def test_all_available_never_skips(self):
        for policy in ("skip_if_none", "skip_if_any", "fallback"):
            versions, missing = _resolve_dub_versions(ja_and_en(), ["en-US"], policy)
            self.assertEqual([v.audio_locale for v in versions], ["en-US"])
            self.assertEqual(missing, [])

    def test_none_available_skips_unless_fallback(self):
        for policy in ("skip_if_none", "skip_if_any"):
            with self.assertRaises(AudioUnavailableError) as ctx:
                _resolve_dub_versions(only_ja(), ["en-US"], policy)
            self.assertEqual(ctx.exception.missing, ["en-US"])
            self.assertEqual(ctx.exception.available, ["ja-JP"])
            self.assertIn("en-US", str(ctx.exception))

    def test_none_available_fallback_uses_first_version(self):
        versions, missing = _resolve_dub_versions(only_ja(), ["en-US"], "fallback")
        self.assertEqual([v.audio_locale for v in versions], ["ja-JP"])
        self.assertEqual(missing, ["en-US"])

    def test_partial_skip_if_none_keeps_found_tracks(self):
        versions, missing = _resolve_dub_versions(only_ja(), ["fr-FR", "ja-JP"], "skip_if_none")
        self.assertEqual([v.audio_locale for v in versions], ["ja-JP"])
        self.assertEqual(missing, ["fr-FR"])

    def test_partial_skip_if_any_skips(self):
        with self.assertRaises(AudioUnavailableError) as ctx:
            _resolve_dub_versions(only_ja(), ["fr-FR", "ja-JP"], "skip_if_any")
        self.assertEqual(ctx.exception.missing, ["fr-FR"])

    def test_partial_fallback_keeps_found_tracks(self):
        versions, missing = _resolve_dub_versions(only_ja(), ["fr-FR", "ja-JP"], "fallback")
        self.assertEqual([v.audio_locale for v in versions], ["ja-JP"])
        self.assertEqual(missing, ["fr-FR"])

    def test_no_version_metadata_uses_episode_locale(self):
        # Requested locale matches the episode's own audio: never skipped.
        versions, missing = _resolve_dub_versions(make_info([]), ["ja-JP"], "skip_if_any")
        self.assertEqual([v.audio_locale for v in versions], ["ja-JP"])
        self.assertEqual(missing, [])
        # Requested locale differs: skipped by default, synthetic version on fallback.
        with self.assertRaises(AudioUnavailableError):
            _resolve_dub_versions(make_info([]), ["en-US"], "skip_if_none")
        versions, _ = _resolve_dub_versions(make_info([]), ["en-US"], "fallback")
        self.assertEqual(versions[0].audio_locale, "ja-JP")


class DownloadEpisodeSkipTests(unittest.TestCase):
    def _call(self, cfg, **kwargs):
        with patch("crunchyroll.auth.load_config", return_value=cfg):
            with redirect_stdout(io.StringIO()):
                return download_episode(
                    client=object(),
                    base_content_id="ep-1",
                    info=only_ja(),
                    audio_langs=["en-US"],
                    subs_langs=["en-US"],
                    video_quality="1080p",
                    audio_quality="192k",
                    **kwargs,
                )

    def test_raises_with_default_config(self):
        with self.assertRaises(AudioUnavailableError):
            self._call({"use_n_m3u8dl_re": False})

    def test_raises_on_n_m3u8dl_re_path(self):
        with self.assertRaises(AudioUnavailableError):
            self._call({"use_n_m3u8dl_re": True})

    def test_explicit_policy_overrides_config(self):
        with self.assertRaises(AudioUnavailableError):
            self._call(
                {"use_n_m3u8dl_re": False, "missing_audio_policy": "fallback"},
                missing_audio_policy="skip_if_none",
            )


class DownloadSeasonSkipTests(unittest.TestCase):
    def test_skipped_episode_does_not_stop_the_season(self):
        episodes = [
            SeasonEpisode(
                id=f"ep-{n}",
                versions=[DubVersion(audio_locale="en-US")],
                season_number=1,
                episode_number=n,
                series_title="Mock Series",
                audio_locale="ja-JP",
                title=f"Ep {n}",
                availability_starts="",
            )
            for n in (1, 2)
        ]
        calls = []

        def fake_download(client, ep_id, *args, **kwargs):
            calls.append(ep_id)
            if ep_id == "ep-1":
                raise AudioUnavailableError("Ep 1", ["en-US"], ["ja-JP"])
            return "out.mkv"

        with patch("crunchyroll.downloader.download_episode", side_effect=fake_download), \
             patch("crunchyroll.downloader.time.sleep"), \
             redirect_stdout(io.StringIO()):
            download_season(
                client=MagicMock(),
                video_quality="1080p",
                audio_quality="192k",
                audio_langs=["en-US"],
                subs_langs=["en-US"],
                episodes=episodes,
                missing_audio_policy="skip_if_none",
            )
        self.assertEqual(calls, ["ep-1", "ep-2"])


class TaskStatusWithSkippedTests(unittest.TestCase):
    def _task(self, *statuses):
        items = {
            f"i{n}": QueueItem(id=f"i{n}", ep_id=f"e{n}", status=st)
            for n, st in enumerate(statuses)
        }
        return DownloadTask(id="t", series_title="x", item_ids=list(items)), items

    def test_all_skipped_task_is_finished(self):
        task, items = self._task("skipped", "skipped")
        self.assertEqual(task.get_computed_status(items), "completed")
        self.assertTrue(task.is_finished(items))

    def test_mixed_statuses(self):
        task, items = self._task("skipped", "completed")
        self.assertEqual(task.get_computed_status(items), "completed")
        task, items = self._task("skipped", "failed")
        self.assertEqual(task.get_computed_status(items), "failed")
        task, items = self._task("skipped", "canceled")
        self.assertEqual(task.get_computed_status(items), "canceled")

    def test_queue_item_round_trip_keeps_policy(self):
        item = QueueItem(id="i", ep_id="e", missing_audio_policy="skip_if_any")
        self.assertEqual(QueueItem.from_dict(item.to_dict()).missing_audio_policy, "skip_if_any")
        # Items persisted before the option existed get the default policy.
        legacy = item.to_dict()
        legacy.pop("missing_audio_policy")
        self.assertEqual(QueueItem.from_dict(legacy).missing_audio_policy, "skip_if_none")


class QueueWorkerSkipTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.store = StateStore(
            state_path=os.path.join(self.tmp_dir, "queue_state.json"), debounce_s=0.05
        )
        self.purge_patcher = patch("crunchyroll.queue.purge_orphan_streams")
        self.purge_patcher.start()

    def tearDown(self):
        self.purge_patcher.stop()
        self.store.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_skipped_episode_is_not_a_failure(self):
        q = DownloadQueue(state_store=self.store, cooldown_range=(0.01, 0.02))
        q.client_factory = MagicMock()
        seen_policies = []

        def skipping_downloader(*args, **kwargs):
            seen_policies.append(kwargs.get("missing_audio_policy"))
            raise AudioUnavailableError("Ep 1", ["en-US"], ["ja-JP"])

        with patch("crunchyroll.queue.download_episode", side_effect=skipping_downloader), \
             patch("crunchyroll.queue.get_episode_info", return_value=only_ja()):
            enq = q.enqueue_batch(
                [{"id": "ep-skip", "title": "Ep 1"}],
                {"missing_audio_policy": "skip_if_any"},
                task_title="Mock Series",
            )
            timeout = time.time() + 3.0
            while q.worker_thread and q.worker_thread.is_alive() and time.time() < timeout:
                time.sleep(0.05)

        self.assertEqual(len(enq), 1)
        job = enq[0]
        self.assertEqual(seen_policies, ["skip_if_any"])  # frozen at enqueue time
        self.assertEqual(job.status, "skipped")
        self.assertIn("en-US", job.error)
        self.assertEqual(q.failed_batch_count, 0)
        self.assertEqual(q.skipped_batch_count, 1)
        self.assertEqual(q.last_status, "completed")
        task = q.tasks[job.task_id]
        self.assertTrue(task.is_finished(q.all_items_map))


if __name__ == "__main__":
    unittest.main()
