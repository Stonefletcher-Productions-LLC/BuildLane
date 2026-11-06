"""Self-repair: a session that stops making progress is stopped and its task requeued, never left holding a slot."""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone

import test_dispatch_resilience as fixtures
from graph import Task
from live import Live


class StallWatchdogTests(unittest.IsolatedAsyncioTestCase):
    engine = fixtures.DispatchResilienceTests.engine

    async def started(self, stall_minutes=60):
        engine, gh, _ = self.engine({7: Task(7, "Hung task")}, stall_minutes=stall_minutes)
        engine.ctx.live = Live()
        engine.ctx.live.stage(7, "Writing", "Hung task")
        hang = asyncio.get_running_loop().create_future()
        engine._start(hang, 7, None, None)
        return engine, gh, hang

    async def test_silent_session_is_stopped_and_requeued(self):
        engine, gh, hang = await self.started()
        later = (datetime.now(timezone.utc) + timedelta(minutes=61)).timestamp()
        self.assertEqual(engine.stop_stalled(later), [7])
        await asyncio.sleep(0)
        outcomes = engine._settle([j for j in engine.jobs if j.done()])
        self.assertEqual([(o.task, o.result) for o in outcomes], [(7, "requeue")])
        self.assertIn("no progress for 60 min", outcomes[0].detail)
        self.assertFalse(engine.jobs)

    async def test_recent_activity_and_disabled_watchdog_leave_it_running(self):
        engine, _, hang = await self.started()
        soon = (datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp()
        self.assertEqual(engine.stop_stalled(soon), [])
        engine.ctx.live.action(7, "Bash: build")
        self.assertEqual(engine.stop_stalled((datetime.now(timezone.utc) + timedelta(minutes=59)).timestamp()), [])
        off, _, hang2 = await self.started(stall_minutes=0)
        self.assertEqual(off.stop_stalled((datetime.now(timezone.utc) + timedelta(days=1)).timestamp()), [])
        for h in (hang, hang2):
            h.cancel()


if __name__ == "__main__":
    unittest.main()
