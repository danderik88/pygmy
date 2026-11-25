import re
import unittest
from pathlib import Path

CONFIG = (Path(__file__).resolve().parents[1] / "deploy" / "go2rtc.yaml").read_text()


class TranscodeClock(unittest.TestCase):
    """The MJPEG printers send ~1.4 frames/s but ffmpeg counts 25 fps: without the wall clock a keyframe came every ~35 s
    and a client joining a running stream (Home Assistant) never got a picture."""

    def test_every_h264_template_puts_frames_on_the_wall_clock(self):
        templates = re.findall(r"^  (h264\w*): \"([^\"]*)\"", CONFIG, flags=re.M)
        self.assertEqual(len(templates), 8)
        for name, args in templates:
            self.assertIn("setpts=(RTCTIME-RTCSTART)/(TB*1000000)", args, name)
            self.assertIn("-r:v 10 -g:v 20", args, name)


if __name__ == "__main__":
    unittest.main()
