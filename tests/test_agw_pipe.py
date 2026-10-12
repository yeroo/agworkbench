"""The control pipe is retried while it is busy or between instances, and only then (agw._open_pipe)."""
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))
import agw  # noqa: E402


def busy():
    err = OSError('All pipe instances are busy')
    err.winerror = agw.PIPE_BUSY
    return err


class OpenPipe(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.enterContext(patch.object(agw.time, 'sleep', side_effect=self.sleeps.append))

    def test_a_busy_pipe_is_retried_until_it_opens(self):
        handle = io.BytesIO()
        with patch('builtins.open', side_effect=[busy(), busy(), handle]) as opened:
            self.assertIs(handle, agw._open_pipe())
        self.assertEqual(3, opened.call_count)
        self.assertEqual(2, len(self.sleeps))

    def test_a_pipe_between_instances_is_retried(self):
        handle = io.BytesIO()
        with patch('builtins.open', side_effect=[FileNotFoundError(2, 'missing'), handle]):
            self.assertIs(handle, agw._open_pipe())

    def test_any_other_error_gives_up_at_once(self):
        with patch('builtins.open', side_effect=PermissionError(13, 'denied')) as opened:
            self.assertIsNone(agw._open_pipe())
        self.assertEqual(1, opened.call_count)
        self.assertEqual([], self.sleeps)

    def test_a_pipe_that_stays_busy_gives_up_at_the_deadline(self):
        clock = iter([0.0, 0.5, 1.0, 2.0])
        with patch.object(agw.time, 'monotonic', side_effect=lambda: next(clock)), \
                patch('builtins.open', side_effect=lambda *a, **k: (_ for _ in ()).throw(busy())) as opened:
            self.assertIsNone(agw._open_pipe(wait=1.5))
        self.assertEqual(3, opened.call_count)


if __name__ == '__main__':
    unittest.main()
