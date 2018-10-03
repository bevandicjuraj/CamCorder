import time
import logging
import threading
from queue import Full
from collections import deque

import cv2
import numpy as np

from camcorder.util.defaults import *
from camcorder.util.utilities import buf_to_numpy
from camcorder.lib.framesources import Frame


class Grabber(threading.Thread):
    def __init__(self, source, arr, out_queue, trigger_event, idx=0):  # , in_queue, out_queue
        super().__init__()
        self.id = idx
        self.name = 'Grabber ' + str(self.id)
        try:
            self.source = int(source)
        except ValueError:
            self.source = source

        self.n_frames = 0
        self.capture = None
        self.frame = None

        self.width = FRAME_WIDTH
        self.height = FRAME_HEIGHT

        shape = (FRAME_HEIGHT + FRAME_METADATA, FRAME_WIDTH, FRAME_COLORS)
        num_bytes = int(np.prod(shape))

        with arr.get_lock():
            self._shared_arr = arr
            logging.debug('Grabber shared array: {}'.format(arr))
            self._fresh_frame = buf_to_numpy(arr, shape=shape, offset=self.id * num_bytes, count=num_bytes)
            logging.debug('Numpy shared buffer at {}'.format(hex(self._fresh_frame.ctypes.data)))

        self._write_queue = out_queue
        self._trigger = trigger_event
        self._avg_fps = FRAME_FPS
        self._t_loop = deque(maxlen=N_FRAMES_LOG_WINDOW)

        logging.debug('Grabber initialization done!')

    def run(self):
        logging.debug('Starting loop in {}!'.format(self.name))
        self.capture = cv2.VideoCapture(self.source)

        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)

        # MAXIMUM fps. This is more a recommendation, most cameras don't listen to this.
        self.capture.set(cv2.CAP_PROP_FPS, FRAME_FPS)

        t0 = cv2.getTickCount()
        while not self._trigger.is_set():
            rt, frame = self.capture.read()
            if not rt:
                continue
            self.frame = Frame(self.n_frames, frame, 'Grabber', add_timestamp=FRAME_ADD_TIMESTAMP,
                               add_tickstamp=FRAME_ADD_TICKSTAMP)

            # Send frames to attached threads/processes
            self.relay_frames()

            # Slow down "replay" if the image source is a video file to emulate realtime replay
            if isinstance(self.source, str):
                time.sleep(1 / self.capture.get(cv2.CAP_PROP_FPS) / PLAYBACK_SPEEDUP)
            self.n_frames += 1

            self._t_loop.appendleft((cv2.getTickCount() - t0) / cv2.getTickFrequency())
            t0 = cv2.getTickCount()

            # Every now and then show fps
            if not self.n_frames % N_FRAMES_LOG_WINDOW:
                avg_fps = 1 / (sum(self._t_loop) / len(self._t_loop))
                logging.debug(
                    'Grabbing frame {}... {}, avg. {:.1f} fps'.format(self.n_frames, 'OK' if rt else 'FAIL', avg_fps))

        logging.debug('Stopping loop in {}!'.format(self.name))

    def relay_frames(self):
        # Forward frame to Writer via Queue
        try:
            self._write_queue.put(self.frame, timeout=.5)
        except Full:
            logging.warning('Dropped frame {}'.format(self.frame.index))

        # Forward frame for tracking and display
        # NOTE: [:] indicates to reuse the buffer
        with self._shared_arr.get_lock():
            self._fresh_frame[:-FRAME_METADATA, :] = self.frame.img
            self._fresh_frame[-FRAME_METADATA:, -22:] = (255, 128, 0)

            # Embed timestamp and frame index
            tickstamp = np.zeros(9, dtype=np.uint8)
            tickstamp[1:] = np.array([self.frame.tickstamp], dtype=np.uint64).view(np.uint8)

            index = np.zeros(9, dtype=np.uint8)
            index[1:] = np.array([self.frame.index], dtype=np.uint64).view(np.uint8)

            self._fresh_frame[-FRAME_METADATA:-FRAME_METADATA + 1, -22:-19] = tickstamp.reshape(1, 3, 3)
            self._fresh_frame[-FRAME_METADATA:-FRAME_METADATA + 1, -19:-16] = index.reshape(1, 3, 3)