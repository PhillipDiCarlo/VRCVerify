"""Unit tests for on_ready background task supervision.

on_ready fires again after every gateway reconnect. It used to call
bot.loop.create_task() unconditionally, so a long-lived process accumulated a
duplicate RabbitMQ consumer (and executor thread), a duplicate cleanup loop,
and duplicate trigger-file watchers on every reconnect. These tests pin the
guard: start once, restart only if dead, never repeat run_once work.
"""

import asyncio
from types import SimpleNamespace

import discord
import pytest

import bot


def run(coro):
    """Run an async bot helper from a sync test (no pytest-asyncio needed)."""
    return asyncio.run(coro)


async def drain(tasks):
    """Cancel tasks so the test loop closes without 'pending task' warnings."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture(autouse=True)
def clean_tasks(monkeypatch):
    monkeypatch.setattr(bot, "background_tasks", {})


@pytest.fixture
def stub_startup(monkeypatch):
    """Replace on_ready's background coroutines with inert stand-ins."""
    started = []

    def make(name):
        async def stub(*args, **kwargs):
            started.append(name)
            await asyncio.sleep(3600)

        return stub

    monkeypatch.setattr(discord.Client, "user", property(lambda self: SimpleNamespace(id=1)))
    for attr, name in [
        ("consume_results_queue", "results_consumer"),
        ("expired_pending_cleanup_task", "expired_pending_cleanup"),
        ("panel_nudge_sweep_task", "panel_nudge_sweep"),
        ("refresh_all_instruction_panels", "instruction_panel_refresh"),
        ("watch_update_trigger_file", "instructions_trigger_watcher"),
        ("watch_premium_cutover_trigger", "premium_cutover_watcher"),
    ]:
        monkeypatch.setattr(bot, attr, make(name))
    return started


# ---------------------------------------------------------------
# start_background_task
# ---------------------------------------------------------------
class TestStartBackgroundTask:
    def test_starts_and_registers_the_task(self):
        ran = []

        async def scenario():
            async def work():
                ran.append(1)
                await asyncio.sleep(3600)

            task = bot.start_background_task("worker", work())
            await asyncio.sleep(0)
            assert bot.background_tasks["worker"] is task
            await drain([task])

        run(scenario())
        assert ran == [1]

    def test_second_start_reuses_the_live_task(self):
        ran = []

        async def scenario():
            async def work():
                ran.append(1)
                await asyncio.sleep(3600)

            first = bot.start_background_task("worker", work())
            await asyncio.sleep(0)
            second = bot.start_background_task("worker", work())
            await asyncio.sleep(0)
            assert first is second
            await drain([first])

        run(scenario())
        # The duplicate coroutine was closed, not executed.
        assert ran == [1]

    def test_dead_task_is_restarted(self):
        ran = []

        async def scenario():
            async def work():
                ran.append(1)

            first = bot.start_background_task("worker", work())
            await first
            second = bot.start_background_task("worker", work())
            await second
            assert first is not second

        run(scenario())
        assert ran == [1, 1]

    def test_run_once_task_never_restarts(self):
        ran = []

        async def scenario():
            async def work():
                ran.append(1)

            first = bot.start_background_task("once", work(), run_once=True)
            await first
            second = bot.start_background_task("once", work(), run_once=True)
            assert first is second

        run(scenario())
        assert ran == [1]

    def test_crashed_task_is_reported_not_swallowed(self, caplog):
        async def scenario():
            async def boom():
                raise RuntimeError("kaboom")

            task = bot.start_background_task("worker", boom())
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        with caplog.at_level("ERROR"):
            run(scenario())

        assert "kaboom" in caplog.text

    def test_tasks_are_named_for_debuggability(self):
        async def scenario():
            async def work():
                await asyncio.sleep(3600)

            task = bot.start_background_task("results_consumer", work())
            assert task.get_name() == "results_consumer"
            await drain([task])

        run(scenario())


# ---------------------------------------------------------------
# on_ready re-entry (the reported bug)
# ---------------------------------------------------------------
class TestOnReadyReentry:
    EXPECTED = {
        "results_consumer",
        "expired_pending_cleanup",
        "panel_nudge_sweep",
        "instruction_panel_refresh",
        "instructions_trigger_watcher",
        "premium_cutover_watcher",
    }

    def test_first_ready_starts_every_task(self, stub_startup):
        async def scenario():
            await bot.on_ready()
            await asyncio.sleep(0)
            await drain(list(bot.background_tasks.values()))

        run(scenario())
        assert set(bot.background_tasks) == self.EXPECTED
        assert sorted(stub_startup) == sorted(self.EXPECTED)

    def test_reconnect_does_not_duplicate_tasks(self, stub_startup):
        async def scenario():
            await bot.on_ready()
            await asyncio.sleep(0)
            first = dict(bot.background_tasks)

            # Simulate three gateway reconnects.
            for _ in range(3):
                await bot.on_ready()
                await asyncio.sleep(0)

            second = dict(bot.background_tasks)
            await drain(list(second.values()))
            return first, second

        first, second = run(scenario())

        assert set(second) == self.EXPECTED
        # Same task objects: nothing was started a second time.
        assert all(first[name] is second[name] for name in self.EXPECTED)
        # Each coroutine body executed exactly once across four on_ready calls.
        assert sorted(stub_startup) == sorted(self.EXPECTED)

    def test_reconnect_revives_a_dead_consumer(self, stub_startup, monkeypatch):
        async def scenario():
            await bot.on_ready()
            await asyncio.sleep(0)
            consumer = bot.background_tasks["results_consumer"]

            # The consumer dies (e.g. an unrecoverable error escaped its loop).
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

            await bot.on_ready()
            await asyncio.sleep(0)
            revived = bot.background_tasks["results_consumer"]
            await drain(list(bot.background_tasks.values()))
            return consumer, revived

        dead, revived = run(scenario())

        assert revived is not dead
        assert stub_startup.count("results_consumer") == 2
        # The one-shot panel refresh was not dragged along with the restart.
        assert stub_startup.count("instruction_panel_refresh") == 1
